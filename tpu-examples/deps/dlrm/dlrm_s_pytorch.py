# Copyright (c) Facebook, Inc. and its affiliates.
#
# This source code is licensed under the MIT license found in the
# LICENSE file in the root directory of this source tree.
#
# Description: an implementation of a deep learning recommendation model (DLRM)
# The model input consists of dense and sparse features. The former is a vector
# of floating point values. The latter is a list of sparse indices into
# embedding tables, which consist of vectors of floating point values.
# The selected vectors are passed to mlp networks denoted by triangles,
# in some cases the vectors are interacted through operators (Ops).
#
# output:
#                         vector of values
# model:                        |
#                              /\
#                             /__\
#                               |
#       _____________________> Op  <___________________
#     /                         |                      \
#    /\                        /\                      /\
#   /__\                      /__\           ...      /__\
#    |                          |                       |
#    |                         Op                      Op
#    |                    ____/__\_____           ____/__\____
#    |                   |_Emb_|____|__|    ...  |_Emb_|__|___|
# input:
# [ dense features ]     [sparse indices] , ..., [sparse indices]
#
# More precise definition of model layers:
# 1) fully connected layers of an mlp
# z = f(y)
# y = Wx + b
#
# 2) embedding lookup (for a list of sparse indices p=[p1,...,pk])
# z = Op(e1,...,ek)
# obtain vectors e1=E[:,p1], ..., ek=E[:,pk]
#
# 3) Operator Op can be one of the following
# Sum(e1,...,ek) = e1 + ... + ek
# Dot(e1,...,ek) = [e1'e1, ..., e1'ek, ..., ek'e1, ..., ek'ek]
# Cat(e1,...,ek) = [e1', ..., ek']'
# where ' denotes transpose operation
#
# References:
# [1] Maxim Naumov, Dheevatsa Mudigere, Hao-Jun Michael Shi, Jianyu Huang,
# Narayanan Sundaram, Jongsoo Park, Xiaodong Wang, Udit Gupta, Carole-Jean Wu,
# Alisson G. Azzolini, Dmytro Dzhulgakov, Andrey Mallevich, Ilia Cherniavskii,
# Yinghai Lu, Raghuraman Krishnamoorthi, Ansha Yu, Volodymyr Kondratenko,
# Stephanie Pereira, Xianjie Chen, Wenlin Chen, Vijay Rao, Bill Jia, Liang Xiong,
# Misha Smelyanskiy, "Deep Learning Recommendation Model for Personalization and
# Recommendation Systems", CoRR, arXiv:1906.00091, 2019

from __future__ import absolute_import, division, print_function, unicode_literals

# miscellaneous
import builtins
import functools
# import bisect
# import shutil
import sys
from datetime import datetime
import time
import json
# data generation
import dlrm_data_pytorch as dp

# numpy
import numpy as np

# onnx
# The onnx import causes deprecation warnings every time workers
# are spawned during testing. So, we filter out those warnings.
import warnings
with warnings.catch_warnings():
    warnings.filterwarnings("ignore", category=DeprecationWarning)
import onnx

# pytorch
import torch
import torch.nn as nn
from torch.nn.parallel.parallel_apply import parallel_apply
from torch.nn.parallel.replicate import replicate
from torch.nn.parallel.scatter_gather import gather, scatter
# quotient-remainder trick
from tricks.qr_embedding_bag import QREmbeddingBag
# mixed-dimension trick
from tricks.md_embedding_bag import PrEmbeddingBag, md_solver

import sklearn.metrics

# from torchviz import make_dot
# import torch.nn.functional as Functional
# from torch.nn.parameter import Parameter

from torch.optim.lr_scheduler import _LRScheduler


exc = getattr(builtins, "IOError", "FileNotFoundError")

class LRPolicyScheduler(_LRScheduler):
    def __init__(
        self, optimizer, num_warmup_steps, decay_start_step, num_decay_steps,
        use_tpu=False, local_optimizer=None, local_lr_scale=None
    ):
        self.num_warmup_steps = num_warmup_steps
        self.decay_start_step = decay_start_step
        self.decay_end_step = decay_start_step + num_decay_steps
        self.num_decay_steps = num_decay_steps

        if self.decay_start_step < self.num_warmup_steps:
            sys.exit("Learning rate warmup must finish before the decay starts")

        self.use_tpu = use_tpu
        self.sparse_feature_local_optimizer = local_optimizer
        self.local_lr_scale = local_lr_scale

        super(LRPolicyScheduler, self).__init__(optimizer)

    def get_lr(self):
        step_count = self._step_count
        if step_count < self.num_warmup_steps:
            # warmup
            scale = 1.0 - (self.num_warmup_steps - step_count) / self.num_warmup_steps
            lr = [base_lr * scale for base_lr in self.base_lrs]
        elif self.decay_start_step <= step_count and step_count < self.decay_end_step:
            # decay
            decayed_steps = step_count - self.decay_start_step
            scale = ((self.num_decay_steps - decayed_steps) / self.num_decay_steps) ** 2
            min_lr = 0.0000001
            lr = [max(min_lr, base_lr * scale) for base_lr in self.base_lrs]
            self.last_lr = lr
        else:
            if self.num_decay_steps > 0:
                # freeze at last
                lr = self.last_lr
            else:
                # do not adjust
                lr = self.base_lrs
        return lr

    def set_sparse_feature_local_optimizer_lr(self):
        if self.sparse_feature_local_optimizer is not None:
            lr = self.get_lr()[0] / self.local_lr_scale
            for param_group in self.sparse_feature_local_optimizer.param_groups:
                param_group['lr'] = lr

    def step(self):
        super().step()
        self.set_sparse_feature_local_optimizer_lr()


### define dlrm in PyTorch ###
class DLRM_Net(nn.Module):

    def create_mlp(self, ln, sigmoid_layer):
        # build MLP layer by layer
        layers = nn.ModuleList()
        for i in range(0, ln.size - 1):
            n = ln[i]
            m = ln[i + 1]

            # construct fully connected operator
            LL = nn.Linear(int(n), int(m), bias=True)

            # initialize the weights
            # with torch.no_grad():
            # custom Xavier input, output or two-sided fill
            mean = 0.0  # std_dev = np.sqrt(variance)
            std_dev = np.sqrt(2 / (m + n))  # np.sqrt(1 / m) # np.sqrt(1 / n)
            W = np.random.normal(mean, std_dev, size=(m, n)).astype(np.float32)
            std_dev = np.sqrt(1 / m)  # np.sqrt(2 / (m + 1))
            bt = np.random.normal(mean, std_dev, size=m).astype(np.float32)
            # approach 1
            LL.weight.data = torch.tensor(W, requires_grad=True)
            LL.bias.data = torch.tensor(bt, requires_grad=True)
            # approach 2
            # LL.weight.data.copy_(torch.tensor(W))
            # LL.bias.data.copy_(torch.tensor(bt))
            # approach 3
            # LL.weight = Parameter(torch.tensor(W),requires_grad=True)
            # LL.bias = Parameter(torch.tensor(bt),requires_grad=True)
            layers.append(LL)

            # construct sigmoid or relu operator
            if i == sigmoid_layer:
                layers.append(nn.Sigmoid())
            else:
                layers.append(nn.ReLU())

        # approach 1: use ModuleList
        # return layers
        # approach 2: use Sequential container to wrap all layers
        return torch.nn.Sequential(*layers)

    def _set_up_sparse_feature_info(self, ln):
        self._group_table_count = ln.size
        self._device_table_count = sum(
            self._tpu_index_belongs_to_ordinal(i)
            for i in range(ln.size)
        )
        self._pad_embedding_lookup = (
            len(self._xla_replica_group) * self._device_table_count <
            self._group_table_count
        )
        self._max_device_table_count_in_group = int(np.ceil(
            self._group_table_count / len(self._xla_replica_group)
        ))
        self._table_count_padded = (
            len(self._xla_replica_group) *
            self._max_device_table_count_in_group
        )
        self._reorder_sparse_feat_indices = np.array([
            i + self._max_device_table_count_in_group*ordinal
            for i in range(self._max_device_table_count_in_group)
            for ordinal in range(len(self._xla_replica_group))
        ])[:self._group_table_count]


    def create_emb(self, m, ln):
        emb_l = nn.ModuleList()
        if self.use_tpu and self.ndevices > 1:
            self._set_up_sparse_feature_info(ln)
        for i in range(0, ln.size):
            n = ln[i]
            if (
                self.use_tpu and
                self.ndevices > 1 and
                not self._tpu_index_belongs_to_ordinal(i)
            ):
                # tpu model-parallel mode. only create this ordinal's tables.
                continue
            # construct embedding operator
            if self.qr_flag and n > self.qr_threshold:
                assert not self.use_tpu, \
                    'QR trick not implemented for tpus'.upper()
                EE = QREmbeddingBag(
                    n, m, self.qr_collisions,
                    operation=self.qr_operation,
                    mode="sum", sparse=self.sparse,
                    xla=self.use_tpu,
                )
            elif self.md_flag:
                assert not self.use_tpu, \
                    'MD trick not implemented for tpus'.upper()
                base = max(m)
                _m = m[i] if n > self.md_threshold else base
                EE = PrEmbeddingBag(n, _m, base)
                # use np initialization as below for consistency...
                W = np.random.uniform(
                    low=-np.sqrt(1 / n), high=np.sqrt(1 / n), size=(n, _m)
                ).astype(np.float32)
                EE.embs.weight.data = torch.tensor(W, requires_grad=True)

            else:
                if self.use_tpu:
                    # TODO: remove when xla supports `nn.EmbeddingBag`
                    from tools.xla_embedding_bag import XlaEmbeddingBag
                    errmsg = (
                        '`use_tpu` was specified. XLA currently only supports '
                        'fixed length sparse groups.'
                    )
                    assert self.offset is not None, errmsg
                    EE = XlaEmbeddingBag(
                        n, m, mode="sum",
                        sparse=self.sparse, offset=self.offset,
                    )
                else:
                    EE = nn.EmbeddingBag(n, m, mode="sum", sparse=self.sparse)

                # initialize embeddings
                # nn.init.uniform_(EE.weight, a=-np.sqrt(1 / n), b=np.sqrt(1 / n))
                W = np.random.uniform(
                    low=-np.sqrt(1 / n), high=np.sqrt(1 / n), size=(n, m)
                ).astype(np.float32)
                # approach 1
                EE.weight.data = torch.tensor(W, requires_grad=True)
                # approach 2
                # EE.weight.data.copy_(torch.tensor(W))
                # approach 3
                # EE.weight = Parameter(torch.tensor(W),requires_grad=True)

            emb_l.append(EE)

        return emb_l

    def set_xla_replica_groups(self, groups):
        self._xla_replica_groups = groups
        for g in groups:
            if self._ordinal in g:
                self._xla_replica_group = g
                assert self.ndevices == len(g), \
                    'MP replica group does not match ndevices, {} vs {}'.format(
                        len(g), self.ndevices
                    )
                self._xla_replica_index = g.index(self._ordinal)
                break
        else:
            raise ValueError(
                'Ordinal {} not in replica groups!'.format(self._ordinal)
            )

    def _tpu_index_belongs_to_ordinal(self, i, ordinal=None):
        ordinal = self._xla_replica_index if ordinal is None else ordinal
        return i % len(self._xla_replica_group) == ordinal

    def __init__(
        self,
        m_spa=None,
        ln_emb=None,
        ln_bot=None,
        ln_top=None,
        arch_interaction_op=None,
        arch_interaction_itself=False,
        sigmoid_bot=-1,
        sigmoid_top=-1,
        sync_dense_params=True,
        loss_threshold=0.0,
        ndevices=-1,
        qr_flag=False,
        qr_operation="mult",
        qr_collisions=0,
        qr_threshold=200,
        md_flag=False,
        md_threshold=200,
        sparse=True,
        use_tpu=False,
        offset=None
    ):
        super(DLRM_Net, self).__init__()

        if (
            (m_spa is not None)
            and (ln_emb is not None)
            and (ln_bot is not None)
            and (ln_top is not None)
            and (arch_interaction_op is not None)
        ):

            # save arguments
            self.ndevices = ndevices
            self.output_d = 0
            self.parallel_model_batch_size = -1
            self.parallel_model_is_not_prepared = True
            self.arch_interaction_op = arch_interaction_op
            self.arch_interaction_itself = arch_interaction_itself
            self.sync_dense_params = sync_dense_params
            self.loss_threshold = loss_threshold
            self.sparse = (not use_tpu) and sparse
            self.use_tpu = use_tpu
            self.offset = offset
            # create variables for QR embedding if applicable
            self.qr_flag = qr_flag
            if self.qr_flag:
                self.qr_collisions = qr_collisions
                self.qr_operation = qr_operation
                self.qr_threshold = qr_threshold
            # create variables for MD embedding if applicable
            self.md_flag = md_flag
            if self.md_flag:
                self.md_threshold = md_threshold
            if self.use_tpu:
                self._init_tpu()
            # create operators
            if ndevices <= 1:
                self.emb_l = self.create_emb(m_spa, ln_emb)
            self.bot_l = self.create_mlp(ln_bot, sigmoid_bot)
            self.top_l = self.create_mlp(ln_top, sigmoid_top)

    def _init_tpu(self):
        # no need to be ordinal aware in full data parallel mode.
        if self.ndevices > 1:
            import torch_xla.core.xla_model as xm
            self._ordinal = xm.get_ordinal()
            self._all_gather = xm.all_gather

    def _filter_params(self, f):
        for name, p in self.named_parameters():
            if f(name):
                yield p

    def mlp_parameters(self):
        return self._filter_params(lambda name: not name.startswith('emb_l'))

    def emb_parameters(self):
        return self._filter_params(lambda name: name.startswith('emb_l'))

    def apply_mlp(self, x, layers):
        # approach 1: use ModuleList
        # for layer in layers:
        #     x = layer(x)
        # return x
        # approach 2: use Sequential container to wrap all layers
        return layers(x)

    def apply_emb(self, lS_o, lS_i, emb_l):
        # WARNING: notice that we are processing the batch at once. We implicitly
        # assume that the data is laid out such that:
        # 1. each embedding is indexed with a group of sparse indices,
        #   corresponding to a single lookup
        # 2. for each embedding the lookups are further organized into a batch
        # 3. for a list of embedding tables there is a list of batched lookups

        ly = []
        for k, sparse_index_group_batch in enumerate(lS_i):
            sparse_offset_group_batch = lS_o[k]

            # embedding lookup
            # We are using EmbeddingBag, which implicitly uses sum operator.
            # The embeddings are represented as tall matrices, with sum
            # happening vertically across 0 axis, resulting in a row vector
            E = emb_l[k]
            V = E(sparse_index_group_batch, sparse_offset_group_batch)

            ly.append(V)

        if self.use_tpu and self.ndevices > 1 and self._pad_embedding_lookup:
            # tpu-comment: this device holds fewer tables compared to some other
            #   devices in the xrt_world. In order to do the `all_to_all`
            #   correctly, pad the embeddings with a dummy tensor that's going
            #   to be dropped later.
            ly.append(torch.zeros(V.shape, device=V.device))

        # print(ly)
        return ly

    def interact_features(self, x, ly):
        if self.arch_interaction_op == "dot":
            # concatenate dense and sparse features
            (batch_size, d) = x.shape
            T = torch.cat([x] + ly, dim=1).view((batch_size, -1, d))
            # perform a dot product
            Z = torch.bmm(T, torch.transpose(T, 1, 2))
            # append dense feature with the interactions (into a row vector)
            # approach 1: all
            # Zflat = Z.view((batch_size, -1))
            # approach 2: unique
            _, ni, nj = Z.shape
            # approach 1: tril_indices
            # offset = 0 if self.arch_interaction_itself else -1
            # li, lj = torch.tril_indices(ni, nj, offset=offset)
            # approach 2: custom
            offset = 1 if self.arch_interaction_itself else 0
            li = torch.tensor([i for i in range(ni) for j in range(i + offset)])
            lj = torch.tensor([j for i in range(nj) for j in range(i + offset)])
            Zflat = Z[:, li, lj]
            # concatenate dense features and interactions
            R = torch.cat([x] + [Zflat], dim=1)
        elif self.arch_interaction_op == "cat":
            # concatenation features (into a row vector)
            R = torch.cat([x] + ly, dim=1)
        else:
            sys.exit(
                "ERROR: --arch-interaction-op="
                + self.arch_interaction_op
                + " is not supported"
            )

        return R

    def forward(self, dense_x, lS_o, lS_i):
        if self.ndevices <= 1:
            return self.sequential_forward(dense_x, lS_o, lS_i)
        elif self.use_tpu:
            return self.tpu_parallel_forward(dense_x, lS_o, lS_i)
        else:
            return self.parallel_forward(dense_x, lS_o, lS_i)

    def clamp_output(self, p):
        z = p
        if 0.0 < self.loss_threshold and self.loss_threshold < 1.0:
            z = torch.clamp(p, min=self.loss_threshold, max=(1.0 - self.loss_threshold))
        return z

    def sequential_forward(self, dense_x, lS_o, lS_i):
        # process dense features (using bottom mlp), resulting in a row vector
        x = self.apply_mlp(dense_x, self.bot_l)
        # debug prints
        # print("intermediate")
        # print(x.detach().cpu().numpy())

        # process sparse features(using embeddings), resulting in a list of row vectors
        ly = self.apply_emb(lS_o, lS_i, self.emb_l)
        # for y in ly:
        #     print(y.detach().cpu().numpy())

        # interact features (dense and sparse)
        z = self.interact_features(x, ly)
        # print(z.detach().cpu().numpy())

        # obtain probability of a click (using top mlp)
        p = self.apply_mlp(z, self.top_l)

        # clamp output if needed
        z = self.clamp_output(p)
        return z

    def _partition_to_device(self, iterable):
        return [
            obj for k, obj in enumerate(iterable)
            if self._tpu_index_belongs_to_ordinal(k)
        ]

    def _collect_distribute_embeddings(self, ordinal_data, local_bsz=None):
        # TODO: once the bug in XLA's alltoall is gone, use that instead of
        #   allgather+narrow, which is 10% slower.
        if local_bsz is None:
            local_bsz = ordinal_data[0].size(0) // len(self._xla_replica_group)
        full_data = self._gather_other_samples(ordinal_data)
        full_data = full_data[self._reorder_sparse_feat_indices]
        return self.narrow(local_bsz, full_data, dim=1)

    def _gather_other_samples(self, array, dim=0):
        out = torch.stack(array)
        out = self._all_gather(out, dim=dim, groups=self._xla_replica_groups)
        return out

    def narrow(self, local_bsz, tensor, dim=1):
        if local_bsz >= tensor.size(dim):
            return tensor
        return torch.narrow(
            tensor, dim, self._xla_replica_index*local_bsz, local_bsz
        )

    def tpu_parallel_forward(self, dense_x, lS_o, lS_i):
        batch_size = dense_x.size()[0]
        ndevices = self.ndevices
        assert not batch_size % ndevices, \
            f"{batch_size} is bsz, {ndevices} devices"
        local_bsz = batch_size // ndevices
        dense_x = self.narrow(local_bsz, dense_x, dim=0)

        #bottom mlp
        x = self.apply_mlp(dense_x, self.bot_l)
        # embeddings
        lS_i = self._partition_to_device(lS_i)
        # offset is assumed to be constant for tpus
        lS_o = [self.offset for _ in lS_i]
        ly_local = self.apply_emb(lS_o, lS_i, self.emb_l)

        # at this point, each device have the embeddings belonging to itself.
        # we do an all_to_all to acquire all embeddings, i.e. full input.
        ly = self._collect_distribute_embeddings(ly_local)
        ly = [_.clone().detach().requires_grad_(True) for _ in ly]

        # interactions
        z = self.interact_features(x, ly)

        # top mlp
        p = self.apply_mlp(z, self.top_l)

        # clamp output if needed
        z = self.clamp_output(p)

        return (
            z,
            ly_local if not self._pad_embedding_lookup else ly_local[:-1],
            ly
        ) # extra return args needed during the custom bwd.

    def tpu_local_backward(self, fullbatch_localembs, localbatch_fullembs):
        localbatch_fullgrads = [_.grad for _ in localbatch_fullembs]
        # inv to narrow
        grad = self._gather_other_samples(localbatch_fullgrads, dim=1)
        grad = self._partition_to_device(grad)
        assert len(fullbatch_localembs) == len(grad), \
            '{} vs {}'.format(len(fullbatch_localembs), len(grad))
        for e, g in zip(fullbatch_localembs, grad):
            e.backward(g)

    def parallel_forward(self, dense_x, lS_o, lS_i):
        ### prepare model (overwrite) ###
        # WARNING: # of devices must be >= batch size in parallel_forward call
        batch_size = dense_x.size()[0]
        ndevices = min(self.ndevices, batch_size, len(self.emb_l))
        device_ids = range(ndevices)
        # WARNING: must redistribute the model if mini-batch size changes(this is common
        # for last mini-batch, when # of elements in the dataset/batch size is not even
        if self.parallel_model_batch_size != batch_size:
            self.parallel_model_is_not_prepared = True

        if self.parallel_model_is_not_prepared or self.sync_dense_params:
            # replicate mlp (data parallelism)
            self.bot_l_replicas = replicate(self.bot_l, device_ids)
            self.top_l_replicas = replicate(self.top_l, device_ids)
            self.parallel_model_batch_size = batch_size

        if self.parallel_model_is_not_prepared:
            # distribute embeddings (model parallelism)
            t_list = []
            for k, emb in enumerate(self.emb_l):
                d = torch.device("cuda:" + str(k % ndevices))
                emb.to(d)
                t_list.append(emb.to(d))
            self.emb_l = nn.ModuleList(t_list)
            self.parallel_model_is_not_prepared = False

        ### prepare input (overwrite) ###
        # scatter dense features (data parallelism)
        # print(dense_x.device)
        dense_x = scatter(dense_x, device_ids, dim=0)
        # distribute sparse features (model parallelism)
        if (len(self.emb_l) != len(lS_o)) or (len(self.emb_l) != len(lS_i)):
            sys.exit("ERROR: corrupted model input detected in parallel_forward call")

        t_list = []
        i_list = []
        for k, _ in enumerate(self.emb_l):
            d = torch.device("cuda:" + str(k % ndevices))
            t_list.append(lS_o[k].to(d))
            i_list.append(lS_i[k].to(d))
        lS_o = t_list
        lS_i = i_list

        ### compute results in parallel ###
        # bottom mlp
        # WARNING: Note that the self.bot_l is a list of bottom mlp modules
        # that have been replicated across devices, while dense_x is a tuple of dense
        # inputs that has been scattered across devices on the first (batch) dimension.
        # The output is a list of tensors scattered across devices according to the
        # distribution of dense_x.
        x = parallel_apply(self.bot_l_replicas, dense_x, None, device_ids)
        # debug prints
        # print(x)

        # embeddings
        ly = self.apply_emb(lS_o, lS_i, self.emb_l)
        # debug prints
        # print(ly)

        # butterfly shuffle (implemented inefficiently for now)
        # WARNING: Note that at this point we have the result of the embedding lookup
        # for the entire batch on each device. We would like to obtain partial results
        # corresponding to all embedding lookups, but part of the batch on each device.
        # Therefore, matching the distribution of output of bottom mlp, so that both
        # could be used for subsequent interactions on each device.
        if len(self.emb_l) != len(ly):
            sys.exit("ERROR: corrupted intermediate result in parallel_forward call")

        t_list = []
        for k, _ in enumerate(self.emb_l):
            d = torch.device("cuda:" + str(k % ndevices))
            y = scatter(ly[k], device_ids, dim=0)
            t_list.append(y)
        # adjust the list to be ordered per device
        ly = list(map(lambda y: list(y), zip(*t_list)))
        # debug prints
        # print(ly)

        # interactions
        z = []
        for k in range(ndevices):
            zk = self.interact_features(x[k], ly[k])
            z.append(zk)
        # debug prints
        # print(z)

        # top mlp
        # WARNING: Note that the self.top_l is a list of top mlp modules that
        # have been replicated across devices, while z is a list of interaction results
        # that by construction are scattered across devices on the first (batch) dim.
        # The output is a list of tensors scattered across devices according to the
        # distribution of z.
        p = parallel_apply(self.top_l_replicas, z, None, device_ids)

        ### gather the distributed results ###
        p0 = gather(p, self.output_d, dim=0)

        # clamp output if needed
        z0 = self.clamp_output(p0)

        return z0


def parse_args():
    import argparse

    parser = argparse.ArgumentParser(
        description="Train Deep Learning Recommendation Model (DLRM)"
    )
    # model related parameters
    parser.add_argument("--arch-sparse-feature-size", type=int, default=2)
    parser.add_argument("--arch-embedding-size", type=str, default="4-3-2")
    # j will be replaced with the table number
    parser.add_argument("--arch-mlp-bot", type=str, default="4-3-2")
    parser.add_argument("--arch-mlp-top", type=str, default="4-2-1")
    parser.add_argument("--arch-interaction-op", type=str, default="dot")
    parser.add_argument("--arch-interaction-itself", action="store_true", default=False)
    # embedding table options
    parser.add_argument("--md-flag", action="store_true", default=False)
    parser.add_argument("--md-threshold", type=int, default=200)
    parser.add_argument("--md-temperature", type=float, default=0.3)
    parser.add_argument("--md-round-dims", action="store_true", default=False)
    parser.add_argument("--qr-flag", action="store_true", default=False)
    parser.add_argument("--qr-threshold", type=int, default=200)
    parser.add_argument("--qr-operation", type=str, default="mult")
    parser.add_argument("--qr-collisions", type=int, default=4)
    # activations and loss
    parser.add_argument("--activation-function", type=str, default="relu")
    parser.add_argument("--loss-function", type=str, default="mse")  # or bce or wbce
    parser.add_argument("--loss-weights", type=str, default="1.0-1.0")  # for wbce
    parser.add_argument("--loss-threshold", type=float, default=0.0)  # 1.0e-7
    parser.add_argument("--round-targets", type=bool, default=False)
    parser.add_argument("--pred-threshold", type=float, default=0.5)
    # data
    parser.add_argument("--data-size", type=int, default=1)
    parser.add_argument("--num-batches", type=int, default=0)
    parser.add_argument(
        "--data-generation", type=str, default="random"
    )  # synthetic or dataset
    parser.add_argument("--drop-last", action="store_true")
    parser.add_argument("--data-trace-file", type=str, default="./input/dist_emb_j.log")
    parser.add_argument("--data-set", type=str, default="kaggle")  # or terabyte
    parser.add_argument("--raw-data-file", type=str, default="")
    parser.add_argument("--processed-data-file", type=str, default="")
    parser.add_argument("--data-randomize", type=str, default="total")  # or day or none
    parser.add_argument("--data-trace-enable-padding", action='store_true')
    parser.add_argument("--max-ind-range", type=int, default=-1)
    parser.add_argument("--data-sub-sample-rate", type=float, default=0.0)  # in [0, 1]
    parser.add_argument("--num-indices-per-lookup", type=int, default=10)
    parser.add_argument("--num-indices-per-lookup-fixed", action='store_true')
    parser.add_argument("--num-workers", type=int, default=0)
    parser.add_argument("--memory-map", action="store_true", default=False)
    # training
    parser.add_argument("--mini-batch-size", type=int, default=1)
    parser.add_argument("--nepochs", type=int, default=1)
    parser.add_argument("--learning-rate", type=float, default=0.01)
    parser.add_argument("--print-precision", type=int, default=5)
    parser.add_argument("--numpy-rand-seed", type=int, default=123)
    parser.add_argument("--sync-dense-params", type=bool, default=True)
    # inference
    parser.add_argument("--inference-only", action="store_true", default=False)
    # onnx
    parser.add_argument("--save-onnx", action="store_true", default=False)
    # accelerators
    parser.add_argument("--use-gpu", action="store_true", default=False)
    parser.add_argument("--use-tpu", action="store_true", default=False)
    parser.add_argument("--tpu-model-parallel-group-len", type=int, default=1)
    parser.add_argument("--tpu-data-parallel", action="store_true")
    parser.add_argument("--tpu-metrics-debug", action="store_true")
    # debugging and profiling
    parser.add_argument("--print-freq", type=int, default=1)
    parser.add_argument("--test-freq", type=int, default=-1)
    parser.add_argument("--test-mini-batch-size", type=int, default=-1)
    parser.add_argument("--test-num-workers", type=int, default=-1)
    parser.add_argument("--print-time", action="store_true", default=False)
    parser.add_argument("--debug-mode", action="store_true", default=False)
    parser.add_argument("--enable-profiling", action="store_true", default=False)
    parser.add_argument("--plot-compute-graph", action="store_true", default=False)
    # store/load model
    parser.add_argument("--save-model", type=str, default="")
    parser.add_argument("--load-model", type=str, default="")
    # mlperf logging (disables other output and stops early)
    parser.add_argument("--mlperf-logging", action="store_true", default=False)
    # stop at target accuracy Kaggle 0.789, Terabyte (sub-sampled=0.875) 0.8107
    parser.add_argument("--mlperf-acc-threshold", type=float, default=0.0)
    # stop at target AUC Terabyte (no subsampling) 0.8025
    parser.add_argument("--mlperf-auc-threshold", type=float, default=0.0)
    parser.add_argument("--mlperf-bin-loader", action='store_true', default=False)
    parser.add_argument("--mlperf-bin-shuffle", action='store_true', default=False)
    # LR policy
    parser.add_argument("--lr-num-warmup-steps", type=int, default=0)
    parser.add_argument("--lr-decay-start-step", type=int, default=0)
    parser.add_argument("--lr-num-decay-steps", type=int, default=0)
    args, _ = parser.parse_known_args()

    if (args.test_mini_batch_size < 0):
        # if the parameter is not set, use the training batch size
        args.test_mini_batch_size = args.mini_batch_size
    if (args.test_num_workers < 0):
        # if the parameter is not set, use the same parameter for training
        args.test_num_workers = args.num_workers

    return args


def tpu_get_xla_replica_groups(args):
    import torch_xla.core.xla_model as xm
    # XXX: would it make sense to sort by size here -- to evenly dist. tables?
    world_size = xm.xrt_world_size()
    if args.tpu_data_parallel:
        return [[i] for i in range(world_size)], [list(range(world_size))]
    num_tables = args.arch_embedding_size.count("-") + 1
    len_mp_group = args.tpu_model_parallel_group_len
    #assert not num_tables % len_mp_group, \
    #    'Model parallel group size has to divide number of emb tables evenly.'
    assert not world_size % len_mp_group, \
        'Length of model parallel groups has to evenly divide `xrt_world_size`'
    len_dp_group = world_size // len_mp_group
    mp_groups = [
        [len_mp_group*d+m for m in range(len_mp_group)]
        for d in range(len_dp_group)
    ]
    dp_groups = [
        [len_dp_group*d+m for m in range(len_dp_group)]
        for d in range(len_mp_group)
    ]
    # now get data sharding arguments
    ordinal = xm.get_ordinal()
    data_n_replicas, data_rank = None, None
    if len(mp_groups) > 1:
        for i, group in enumerate(mp_groups):
            if ordinal in group:
                data_n_replicas, data_rank = len(mp_groups), i
                break
        else:
            raise ValueError(
                'Ordinal {} not in replica groups!'.format(ordinal)
            )

    return mp_groups, dp_groups, data_n_replicas, data_rank


def main(*_args):
    ### import packages ###
    import sys

    args = parse_args()
    ### some basic setup ###
    np.random.seed(args.numpy_rand_seed)
    np.set_printoptions(precision=args.print_precision)
    torch.set_printoptions(precision=args.print_precision)
    torch.manual_seed(args.numpy_rand_seed)

    use_gpu = args.use_gpu and torch.cuda.is_available()
    use_tpu = args.use_tpu
    print = builtins.print
    if use_tpu:
        use_gpu = False
        import torch_xla.core.xla_model as xm
        import torch_xla.debug.metrics as met
        import torch_xla.distributed.parallel_loader as pl
        print = xm.master_print
        device = xm.xla_device()
        print("Using {} TPU core(s)...".format(xm.xrt_world_size()))
        if args.data_set in ['kaggle', 'terabyte']:
            print('Criteo datasets have offset = 1, forcing the arguments..')
            args.num_indices_per_lookup_fixed = True
            args.num_indices_per_lookup = 1
        if args.enable_profiling:
            print("Profiling was enabled. Turning it off for TPUs.")
            args.enable_profiling = False
        if not args.num_indices_per_lookup_fixed:
            # XXX: does this lead to recompilations?
            raise NotImplementedError('This will lead to recompilations.')
        if args.mlperf_logging:
            raise NotImplementedError('This codepath is not tested.')
        mp_replica_groups, dp_replica_groups, tpu_n_replicas, tpu_group_rank = (
            tpu_get_xla_replica_groups(args)
        )
        print('XLA replica groups for Model Parallel:\n\t', mp_replica_groups)
        print('XLA replica groups for Data Parallel:\n\t', dp_replica_groups)
        if tpu_n_replicas is not None:
            print(
                'Data will be sharded across {} groups.'.format(tpu_n_replicas)
            )
        if len(dp_replica_groups) == 1:
            # i.e. no allgather etc in the emb layer.
            print("TPU data-parallel mode, setting --tpu-data-parallel to True")
            args.tpu_data_parallel = True
        else:
            print("TPU model-parallel mode, setting --drop-last=True")
            args.drop_last = True
        if args.print_time:
            print(
                '`torch_xla` async execution is not compatible with '
                'ms/it reporting, turning --print-time off.'
            )
            args.print_time = False
    elif use_gpu:
        torch.cuda.manual_seed_all(args.numpy_rand_seed)
        torch.backends.cudnn.deterministic = True
        device = torch.device("cuda", 0)
        ngpus = torch.cuda.device_count()  # 1
        print("Using {} GPU(s)...".format(ngpus))
    else:
        device = torch.device("cpu")
        print("Using CPU...")

    if args.mlperf_logging:
        print('command line args: ', json.dumps(vars(args)))

    ### prepare training data ###
    ln_bot = np.fromstring(args.arch_mlp_bot, dtype=int, sep="-")
    # input data
    if (args.data_generation == "dataset"):

        data_args = [args]
        if use_tpu:
            data_args.extend([tpu_n_replicas, tpu_group_rank])
        train_data, train_ld, test_data, test_ld = \
            dp.make_criteo_data_and_loaders(*data_args)
        nbatches = args.num_batches if args.num_batches > 0 else len(train_ld)
        nbatches_test = len(test_ld)

        ln_emb = train_data.counts
        # enforce maximum limit on number of vectors per embedding
        if args.max_ind_range > 0:
            ln_emb = np.array(list(map(
                lambda x: x if x < args.max_ind_range else args.max_ind_range,
                ln_emb
            )))
        m_den = train_data.m_den
        ln_bot[0] = m_den
    else:
        # input and target at random
        ln_emb = np.fromstring(args.arch_embedding_size, dtype=int, sep="-")
        m_den = ln_bot[0]
        data_args = [args, ln_emb, m_den]
        if use_tpu:
            data_args.extend([tpu_n_replicas, tpu_group_rank])
        train_data, train_ld = dp.make_random_data_and_loader(*data_args)
        #nbatches = args.num_batches if args.num_batches > 0 else len(train_ld)
        nbatches = len(train_ld)
    if use_tpu:
        # Wrap w/ torch_xla's loader
        train_ld = pl.MpDeviceLoader(train_ld, device)
        if 'test_ld' in locals():
            test_ld = pl.MpDeviceLoader(test_ld, device)

    ### parse command line arguments ###
    m_spa = args.arch_sparse_feature_size
    num_fea = ln_emb.size + 1  # num sparse + num dense features
    m_den_out = ln_bot[ln_bot.size - 1]
    if args.arch_interaction_op == "dot":
        # approach 1: all
        # num_int = num_fea * num_fea + m_den_out
        # approach 2: unique
        if args.arch_interaction_itself:
            num_int = (num_fea * (num_fea + 1)) // 2 + m_den_out
        else:
            num_int = (num_fea * (num_fea - 1)) // 2 + m_den_out
    elif args.arch_interaction_op == "cat":
        num_int = num_fea * m_den_out
    else:
        sys.exit(
            "ERROR: --arch-interaction-op="
            + args.arch_interaction_op
            + " is not supported"
        )
    arch_mlp_top_adjusted = str(num_int) + "-" + args.arch_mlp_top
    ln_top = np.fromstring(arch_mlp_top_adjusted, dtype=int, sep="-")
    # sanity check: feature sizes and mlp dimensions must match
    if m_den != ln_bot[0]:
        sys.exit(
            "ERROR: arch-dense-feature-size "
            + str(m_den)
            + " does not match first dim of bottom mlp "
            + str(ln_bot[0])
        )
    if args.qr_flag:
        if args.qr_operation == "concat" and 2 * m_spa != m_den_out:
            sys.exit(
                "ERROR: 2 arch-sparse-feature-size "
                + str(2 * m_spa)
                + " does not match last dim of bottom mlp "
                + str(m_den_out)
                + " (note that the last dim of bottom mlp must be 2x the embedding dim)"
            )
        if args.qr_operation != "concat" and m_spa != m_den_out:
            sys.exit(
                "ERROR: arch-sparse-feature-size "
                + str(m_spa)
                + " does not match last dim of bottom mlp "
                + str(m_den_out)
            )
    else:
        if m_spa != m_den_out:
            sys.exit(
                "ERROR: arch-sparse-feature-size "
                + str(m_spa)
                + " does not match last dim of bottom mlp "
                + str(m_den_out)
            )
    if num_int != ln_top[0]:
        sys.exit(
            "ERROR: # of feature interactions "
            + str(num_int)
            + " does not match first dimension of top mlp "
            + str(ln_top[0])
        )

    # assign mixed dimensions if applicable
    if args.md_flag:
        m_spa = md_solver(
            torch.tensor(ln_emb),
            args.md_temperature,  # alpha
            d0=m_spa,
            round_dim=args.md_round_dims
        ).tolist()

    # test prints (model arch)
    if args.debug_mode:
        print("model arch:")
        print(
            "mlp top arch "
            + str(ln_top.size - 1)
            + " layers, with input to output dimensions:"
        )
        print(ln_top)
        print("# of interactions")
        print(num_int)
        print(
            "mlp bot arch "
            + str(ln_bot.size - 1)
            + " layers, with input to output dimensions:"
        )
        print(ln_bot)
        print("# of features (sparse and dense)")
        print(num_fea)
        print("dense feature size")
        print(m_den)
        print("sparse feature size")
        print(m_spa)
        print(
            "# of embeddings (= # of sparse features) "
            + str(ln_emb.size)
            + ", with dimensions "
            + str(m_spa)
            + "x:"
        )
        print(ln_emb)

        print("data (inputs and targets):")
        for j, (X, lS_o, lS_i, T) in enumerate(train_ld):
            # early exit if nbatches was set by the user and has been exceeded
            if nbatches > 0 and j >= nbatches:
                break

            print("mini-batch: %d" % j)
            print(X.detach().cpu().numpy())
            # transform offsets to lengths when printing
            print(
                [
                    np.diff(
                        S_o.detach().cpu().tolist() + list(lS_i[i].shape)
                    ).tolist()
                    for i, S_o in enumerate(lS_o)
                ]
            )
            print([S_i.detach().cpu().tolist() for S_i in lS_i])
            print(T.detach().cpu().numpy())

    ndevices = -1
    if use_tpu:
        ndevices = len(dp_replica_groups)
        if args.mini_batch_size % ndevices:
            raise NotImplementedError(
                '`ndevices` needs to evenly divide --mini-batch-size. '
                'bsz is {}, ndevices is {}'.format(
                    args.mini_batch_size, ndevices,
                )
            )

    elif use_gpu:
        ndevices = min(ngpus, args.mini_batch_size, num_fea - 1)

    ### construct the neural network specified above ###
    # WARNING: to obtain exactly the same initialization for
    # the weights we need to start from the same random seed.
    # np.random.seed(args.numpy_rand_seed)
    dlrm = DLRM_Net(
        m_spa,
        ln_emb,
        ln_bot,
        ln_top,
        arch_interaction_op=args.arch_interaction_op,
        arch_interaction_itself=args.arch_interaction_itself,
        sigmoid_bot=-1,
        sigmoid_top=ln_top.size - 2,
        sync_dense_params=args.sync_dense_params,
        loss_threshold=args.loss_threshold,
        ndevices=ndevices,
        qr_flag=args.qr_flag,
        qr_operation=args.qr_operation,
        qr_collisions=args.qr_collisions,
        qr_threshold=args.qr_threshold,
        md_flag=args.md_flag,
        md_threshold=args.md_threshold,
        sparse=device.type != 'xla',
        use_tpu=use_tpu,
        offset=args.num_indices_per_lookup,
    )
    # test prints
    if args.debug_mode:
        print("initial parameters (weights and bias):")
        for param in dlrm.parameters():
            print(param.detach().cpu().numpy())
        # print(dlrm)

    if use_gpu:
        # Custom Model-Data Parallel
        # the mlps are replicated and use data parallelism, while
        # the embeddings are distributed and use model parallelism
        dlrm = dlrm.to(device)  # .cuda()
        if dlrm.ndevices > 1:
            dlrm.emb_l = dlrm.create_emb(m_spa, ln_emb)
    if use_tpu:
        # XXX: ndevices is redundant, same info contained in dp_replica_groups
        if dlrm.ndevices > 1:
            dlrm.set_xla_replica_groups(mp_replica_groups)
            dlrm.emb_l = dlrm.create_emb(m_spa, ln_emb)
        dlrm.device = device
        dlrm = dlrm.to(device)

    # specify the loss function
    if args.loss_function == "mse":
        loss_fn = torch.nn.MSELoss(reduction="mean")
    elif args.loss_function == "bce":
        loss_fn = torch.nn.BCELoss(reduction="mean")
    elif args.loss_function == "wbce":
        loss_ws = torch.tensor(np.fromstring(args.loss_weights, dtype=float, sep="-"))
        loss_fn = torch.nn.BCELoss(reduction="none")
    else:
        sys.exit("ERROR: --loss-function=" + args.loss_function + " is not supported")

    if not args.inference_only:
        # specify the optimizer algorithm

        if use_tpu and not args.tpu_data_parallel:
            # tpu's paradigm is different than gpu/cpu. Each process here runs
            #   on its own and all reduces need to happen in a particular way.
            # Data parallel part, i.e. the MLP part will be allreduced.
            # EmbeddingBag part will not be allreduced.
            optimizer = torch.optim.SGD(
                dlrm.mlp_parameters(), lr=args.learning_rate,
            )
            local_lr_scale = xm.xrt_world_size() / len(mp_replica_groups)
            emb_local_optimizer = torch.optim.SGD(
                dlrm.emb_parameters(), lr=args.learning_rate/local_lr_scale,
            )
            lr_scheduler = LRPolicyScheduler(
                optimizer, args.lr_num_warmup_steps, args.lr_decay_start_step,
                args.lr_num_decay_steps, use_tpu=True,
                local_optimizer=emb_local_optimizer,
                local_lr_scale=local_lr_scale,
            )
        else:
            optimizer = torch.optim.SGD(
                dlrm.parameters(), lr=args.learning_rate
            )
            lr_scheduler = LRPolicyScheduler(
                optimizer, args.lr_num_warmup_steps, args.lr_decay_start_step,
                args.lr_num_decay_steps
            )

    ### main loop ###

    def time_wrap(use_gpu):
        if use_gpu:
            torch.cuda.synchronize()
        return time.time()

    def dlrm_wrap(X, lS_o, lS_i, use_gpu, device, tpu_return_val_only=False):
        if use_gpu:  # .cuda()
            # lS_i can be either a list of tensors or a stacked tensor.
            # Handle each case below:
            lS_i = [S_i.to(device) for S_i in lS_i] if isinstance(lS_i, list) \
                else lS_i.to(device)
            lS_o = [S_o.to(device) for S_o in lS_o] if isinstance(lS_o, list) \
                else lS_o.to(device)
            return dlrm(X.to(device), lS_o, lS_i)
        elif tpu_return_val_only:
            return dlrm(X, lS_o, lS_i)[0]
        else:
            return dlrm(X, lS_o, lS_i)

    def loss_fn_wrap(Z, T, use_gpu, device):
        if T.size(0) > Z.size(0):
            # This happens for tpus in model-parallel mode.
            # Target tensor is likely for global batch. Narrow to this device.
            T = dlrm.narrow(Z.size(0), T, dim=0)

        if args.loss_function == "mse" or args.loss_function == "bce":
            if use_gpu:
                return loss_fn(Z, T.to(device))
            else:
                return loss_fn(Z, T)
        elif args.loss_function == "wbce":
            if use_gpu:
                loss_ws_ = loss_ws[T.data.view(-1).long()].view_as(T).to(device)
                loss_fn_ = loss_fn(Z, T.to(device))
            else:
                loss_ws_ = loss_ws[T.data.view(-1).long()].view_as(T)
                loss_fn_ = loss_fn(Z, T.to(device))
            loss_sc_ = loss_ws_ * loss_fn_
            # debug prints
            # print(loss_ws_)
            # print(loss_fn_)
            return loss_sc_.mean()

    def accuracy_wrap(Z, T, use_tpu=False, threshold=0.5):
        if use_tpu:
            S = (Z >= 0.5).to(torch.int32)
            T = dlrm.narrow(Z.size(0), T, dim=0)
            A = T.eq(S).sum()
        else:
            S = Z.detach().cpu().numpy()  # numpy array
            T = T.detach().cpu().numpy()  # numpy array
            A = np.sum((np.round(S, 0) == T).astype(np.uint8))
        return A

    # training or inference
    best_gA_test = 0
    best_auc_test = 0
    skip_upto_epoch = 0
    skip_upto_batch = 0
    total_time = 0
    total_loss = 0
    total_accu = 0
    total_iter = 0
    total_samp = 0
    k = 0

    # Load model is specified
    if args.load_model != "":
        print("Loading saved model {}".format(args.load_model))
        if use_tpu:
            # XXX: add tpu capabilities to load.
            raise NotImplementedError('Add tpu capabilities to load.')
        elif use_gpu:
            if dlrm.ndevices > 1:
                # NOTE: when targeting inference on multiple GPUs,
                # load the model as is on CPU or GPU, with the move
                # to multiple GPUs to be done in parallel_forward
                ld_model = torch.load(args.load_model)
            else:
                # NOTE: when targeting inference on single GPU,
                # note that the call to .to(device) has already happened
                ld_model = torch.load(
                    args.load_model,
                    map_location=torch.device('cuda')
                    # map_location=lambda storage, loc: storage.cuda(0)
                )
        else:
            # when targeting inference on CPU
            ld_model = torch.load(args.load_model, map_location=torch.device('cpu'))
        dlrm.load_state_dict(ld_model["state_dict"])
        ld_j = ld_model["iter"]
        ld_k = ld_model["epoch"]
        ld_nepochs = ld_model["nepochs"]
        ld_nbatches = ld_model["nbatches"]
        ld_nbatches_test = ld_model["nbatches_test"]
        ld_gA = ld_model["train_acc"]
        ld_gL = ld_model["train_loss"]
        ld_total_loss = ld_model["total_loss"]
        ld_total_accu = ld_model["total_accu"]
        ld_gA_test = ld_model["test_acc"]
        ld_gL_test = ld_model["test_loss"]
        if not args.inference_only:
            optimizer.load_state_dict(ld_model["opt_state_dict"])
            best_gA_test = ld_gA_test
            total_loss = ld_total_loss
            total_accu = ld_total_accu
            skip_upto_epoch = ld_k  # epochs
            skip_upto_batch = ld_j  # batches
        else:
            args.print_freq = ld_nbatches
            args.test_freq = 0

        print(
            "Saved at: epoch = {:d}/{:d}, batch = {:d}/{:d}, ntbatch = {:d}".format(
                ld_k, ld_nepochs, ld_j, ld_nbatches, ld_nbatches_test
            )
        )
        print(
            "Training state: loss = {:.6f}, accuracy = {:3.3f} %".format(
                ld_gL, ld_gA * 100
            )
        )
        print(
            "Testing state: loss = {:.6f}, accuracy = {:3.3f} %".format(
                ld_gL_test, ld_gA_test * 100
            )
        )

    print(
        "time/loss/accuracy (if enabled):  {}".format(datetime.now(),
        flush=True),
    )
    with torch.autograd.profiler.profile(args.enable_profiling and use_gpu) as prof:
        while k < args.nepochs:
            if k < skip_upto_epoch:
                continue

            accum_time_begin = time_wrap(use_gpu)

            if args.mlperf_logging:
                previous_iteration_time = None

            for j, (X, lS_o, lS_i, T) in enumerate(train_ld):
                if j < skip_upto_batch:
                    continue

                if args.mlperf_logging:
                    current_time = time_wrap(use_gpu)
                    if previous_iteration_time:
                        iteration_time = current_time - previous_iteration_time
                    else:
                        iteration_time = 0
                    previous_iteration_time = current_time
                else:
                    t1 = time_wrap(use_gpu)

                # early exit if nbatches was set by the user and has been exceeded
                # XXX: what about j being reset every epoch?
                if nbatches > 0 and j >= nbatches:
                    break
                '''
                # debug prints
                print("input and targets")
                print(X.detach().cpu().numpy())
                print([np.diff(S_o.detach().cpu().tolist()
                       + list(lS_i[i].shape)).tolist() for i, S_o in enumerate(lS_o)])
                print([S_i.detach().cpu().numpy().tolist() for S_i in lS_i])
                print(T.detach().cpu().numpy())
                '''

                # forward pass
                if use_tpu and not args.tpu_data_parallel:
                    # args[1:] below will be used in the custom backward
                    Z, fullbatch_localembs, localbatch_fullembs = \
                        dlrm_wrap(X, lS_o, lS_i, use_gpu, device)
                else:
                    Z = dlrm_wrap(X, lS_o, lS_i, use_gpu, device)

                # loss
                E = loss_fn_wrap(Z, T, use_gpu, device)
                '''
                # debug prints
                print("output and loss")
                print(Z.detach().cpu().numpy())
                print(E.detach().cpu().numpy())
                '''

                A = accuracy_wrap(Z, T, use_tpu)
                mbs = Z.shape[0]
                if not args.inference_only:
                    # scaled error gradient propagation
                    # (where we do not accumulate gradients across mini-batches)
                    optimizer.zero_grad()
                    # backward pass
                    E.backward()
                    # debug prints (check gradient norm)
                    # for l in mlp.layers:
                    #     if hasattr(l, 'weight'):
                    #          print(l.weight.grad.norm().item())

                    # backward + optimizer
                    if use_tpu and not args.tpu_data_parallel:
                        # XXX: no clip grad?
                        # Full allreduce across all devices for the MLP parts.
                        xm.optimizer_step(optimizer, groups=None)
                        # bwd pass for the embedding tables.
                        emb_local_optimizer.zero_grad()
                        dlrm.tpu_local_backward(
                            fullbatch_localembs, localbatch_fullembs,
                        )
                        if len(dp_replica_groups[0]) > 1:
                            xm.optimizer_step(
                                emb_local_optimizer, groups=dp_replica_groups,
                            )
                        else:
                            # no allreduce, just step
                            emb_local_optimizer.step()
                    else:
                        optimizer.step()
                    lr_scheduler.step()


                if args.mlperf_logging:
                    total_time += iteration_time
                else:
                    t2 = time_wrap(use_gpu)
                    total_time += t2 - t1
                total_accu += A
                total_loss += E.detach() * mbs
                total_iter += 1
                total_samp += mbs

                should_print = ((j + 1) % args.print_freq == 0) or (j + 1 == nbatches)
                should_test = (
                    (args.test_freq > 0)
                    and (args.data_generation == "dataset")
                    and (((j + 1) % args.test_freq == 0) or (j + 1 == nbatches))
                )

                # print time, loss and accuracy
                if should_print or should_test:
                    # For tpus, reduce the per-core metrics before reporting.
                    if use_tpu:
                        xm.mark_step()
                        total_samp = xm.mesh_reduce('samp', total_samp, np.sum)
                        total_accu = xm.mesh_reduce('accu', total_accu, np.sum)
                        total_loss = xm.mesh_reduce('loss', total_loss, np.sum)
                    gT = 1000.0 * total_time / total_iter if args.print_time else -1
                    total_time = 0

                    gA = total_accu / float(total_samp)
                    total_accu = 0

                    gL = total_loss / float(total_samp)
                    total_loss = 0

                    str_run_type = "inference" if args.inference_only else "training"
                    print(
                        (
                            "Finished {} it {}/{} of epoch {}, {:.2f} ms/it, "
                            "loss {:.6f}, accuracy {:3.3f} %, {} samples, @ {}"
                        ).format(
                            str_run_type, j + 1, nbatches, k, gT,
                            gL, gA * 100, total_samp, datetime.now()
                        ),
                        flush=True,
                    )
                    # Uncomment the line below to print out the total time with overhead
                    # print("Accumulated time so far: {}" \
                    # .format(time_wrap(use_gpu) - accum_time_begin))
                    total_iter = 0
                    total_samp = 0

                # testing
                if should_test and not args.inference_only:
                    # don't measure training iter time in a test iteration
                    if args.mlperf_logging:
                        previous_iteration_time = None

                    test_accu = 0
                    test_loss = 0
                    test_samp = 0

                    accum_test_time_begin = time_wrap(use_gpu)
                    if args.mlperf_logging:
                        scores = []
                        targets = []

                    for i, (X_test, lS_o_test, lS_i_test, T_test) in enumerate(test_ld):
                        # early exit if nbatches was set by the user and was exceeded
                        if nbatches > 0 and i >= nbatches:
                            break

                        t1_test = time_wrap(use_gpu)

                        # forward pass
                        Z_test = dlrm_wrap(
                            X_test, lS_o_test, lS_i_test, use_gpu, device,
                            tpu_return_val_only=True,
                        )
                        if args.mlperf_logging:
                            S_test = Z_test.detach().cpu().numpy()  # numpy array
                            T_test = T_test.detach().cpu().numpy()  # numpy array
                            scores.append(S_test)
                            targets.append(T_test)
                        else:
                            # loss
                            E_test = loss_fn_wrap(Z_test, T_test, use_gpu, device)
                            A_test = accuracy_wrap(Z_test, T_test, use_tpu)
                            if use_tpu:
                                 L_test = E_test.detach()
                            else:
                                 L_test = E_test.detach().cpu().numpy()  # numpy array
                            mbs_test = Z_test.shape[0]
                            test_accu += A_test
                            test_loss += L_test * mbs_test
                            test_samp += mbs_test

                        t2_test = time_wrap(use_gpu)

                    if args.mlperf_logging:
                        scores = np.concatenate(scores, axis=0)
                        targets = np.concatenate(targets, axis=0)

                        metrics = {
                            'loss' : sklearn.metrics.log_loss,
                            'recall' : lambda y_true, y_score:
                            sklearn.metrics.recall_score(
                                y_true=y_true,
                                y_pred=np.round(y_score)
                            ),
                            'precision' : lambda y_true, y_score:
                            sklearn.metrics.precision_score(
                                y_true=y_true,
                                y_pred=np.round(y_score)
                            ),
                            'f1' : lambda y_true, y_score:
                            sklearn.metrics.f1_score(
                                y_true=y_true,
                                y_pred=np.round(y_score)
                            ),
                            'ap' : sklearn.metrics.average_precision_score,
                            'roc_auc' : sklearn.metrics.roc_auc_score,
                            'accuracy' : lambda y_true, y_score:
                            sklearn.metrics.accuracy_score(
                                y_true=y_true,
                                y_pred=np.round(y_score)
                            ),
                            # 'pre_curve' : sklearn.metrics.precision_recall_curve,
                            # 'roc_curve' :  sklearn.metrics.roc_curve,
                        }

                        # print("Compute time for validation metric : ", end="")
                        # first_it = True
                        validation_results = {}
                        for metric_name, metric_function in metrics.items():
                            # if first_it:
                            #     first_it = False
                            # else:
                            #     print(", ", end="")
                            # metric_compute_start = time_wrap(False)
                            validation_results[metric_name] = metric_function(
                                targets,
                                scores
                            )
                            # metric_compute_end = time_wrap(False)
                            # met_time = metric_compute_end - metric_compute_start
                            # print("{} {:.4f}".format(metric_name, 1000 * (met_time)),
                            #      end="")
                        # print(" ms")
                        gA_test = validation_results['accuracy']
                        gL_test = validation_results['loss']
                    else:
                        if use_tpu:
                            xm.mark_step()
                            test_samp = xm.mesh_reduce('samp', test_samp, np.sum)
                            test_accu = xm.mesh_reduce('accu', test_accu, np.sum)
                            test_loss = xm.mesh_reduce('loss', test_loss, np.sum)
                        gA_test = test_accu / test_samp
                        gL_test = test_loss / test_samp

                    is_best = gA_test > best_gA_test
                    if is_best:
                        best_gA_test = gA_test
                        if not (args.save_model == ""):
                            assert not use_tpu, "Saving model w/ tpus should use xm.save"
                            print("Saving model to {}".format(args.save_model))
                            torch.save(
                                {
                                    "epoch": k,
                                    "nepochs": args.nepochs,
                                    "nbatches": nbatches,
                                    "nbatches_test": nbatches_test,
                                    "iter": j + 1,
                                    "state_dict": dlrm.state_dict(),
                                    "train_acc": gA,
                                    "train_loss": gL,
                                    "test_acc": gA_test,
                                    "test_loss": gL_test,
                                    "total_loss": total_loss,
                                    "total_accu": total_accu,
                                    "opt_state_dict": optimizer.state_dict(),
                                },
                                args.save_model,
                            )

                    if args.mlperf_logging:
                        is_best = validation_results['roc_auc'] > best_auc_test
                        if is_best:
                            best_auc_test = validation_results['roc_auc']

                        print(
                            "Testing at - {}/{} of epoch {},".format(j + 1, nbatches, k)
                            + " loss {:.6f}, recall {:.4f}, precision {:.4f},".format(
                                validation_results['loss'],
                                validation_results['recall'],
                                validation_results['precision']
                            )
                            + " f1 {:.4f}, ap {:.4f},".format(
                                validation_results['f1'],
                                validation_results['ap'],
                            )
                            + " auc {:.4f}, best auc {:.4f},".format(
                                validation_results['roc_auc'],
                                best_auc_test
                            )
                            + " accuracy {:3.3f} %, best accuracy {:3.3f} %".format(
                                validation_results['accuracy'] * 100,
                                best_gA_test * 100
                            )
                        )
                    else:
                        print(
                            "Testing at - {}/{} of epoch {},".format(j + 1, nbatches, 0)
                            + " loss {:.6f}, accuracy {:3.3f} %, best {:3.3f} %".format(
                                gL_test, gA_test * 100, best_gA_test * 100
                            )
                        )
                        if use_tpu and args.tpu_metrics_debug:
                            print('VALIDATION-METRICS')
                            print(met.metrics_report())
                    # Uncomment the line below to print out the total time with overhead
                    # print("Total test time for this group: {}" \
                    # .format(time_wrap(use_gpu) - accum_test_time_begin))

                    if (args.mlperf_logging
                        and (args.mlperf_acc_threshold > 0)
                        and (best_gA_test > args.mlperf_acc_threshold)):
                        print("MLPerf testing accuracy threshold "
                              + str(args.mlperf_acc_threshold)
                              + " reached, stop training")
                        break

                    if (args.mlperf_logging
                        and (args.mlperf_auc_threshold > 0)
                        and (best_auc_test > args.mlperf_auc_threshold)):
                        print("MLPerf testing auc threshold "
                              + str(args.mlperf_auc_threshold)
                              + " reached, stop training")
                        break

            k += 1  # nepochs
            if use_tpu and args.tpu_metrics_debug:
                print(met.metrics_report())

    # profiling
    if args.enable_profiling:
        with open("dlrm_s_pytorch.prof", "w") as prof_f:
            prof_f.write(prof.key_averages().table(sort_by="cpu_time_total"))
            prof.export_chrome_trace("./dlrm_s_pytorch.json")
        # print(prof.key_averages().table(sort_by="cpu_time_total"))

    # plot compute graph
    if args.plot_compute_graph:
        sys.exit(
            "ERROR: Please install pytorchviz package in order to use the"
            + " visualization. Then, uncomment its import above as well as"
            + " three lines below and run the code again."
        )
        # V = Z.mean() if args.inference_only else E
        # dot = make_dot(V, params=dict(dlrm.named_parameters()))
        # dot.render('dlrm_s_pytorch_graph') # write .pdf file

    # test prints
    if not args.inference_only and args.debug_mode:
        print("updated parameters (weights and bias):")
        for param in dlrm.parameters():
            print(param.detach().cpu().numpy())

    # export the model in onnx
    if args.save_onnx:
        dlrm_pytorch_onnx_file = "dlrm_s_pytorch.onnx"
        (X, lS_o, lS_i, _) = train_data[0]  # get first batch of elements
        torch.onnx.export(
            dlrm, (X, lS_o, lS_i), dlrm_pytorch_onnx_file, verbose=True, use_external_data_format=True
        )
        # recover the model back
        dlrm_pytorch_onnx = onnx.load("dlrm_s_pytorch.onnx")
        # check the onnx model
        onnx.checker.check_model(dlrm_pytorch_onnx)


if __name__ == "__main__":
    main()
