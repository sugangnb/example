// Auto-generated file. Do not edit!
//   Template: src/f32-velu/sse-rr2-p6.c.in
//   Generator: tools/xngen
//
// Copyright 2020 Google LLC
//
// This source code is licensed under the BSD-style license found in the
// LICENSE file in the root directory of this source tree.

#include <assert.h>

#include <smmintrin.h>

#include <xnnpack/vunary.h>
#include <xnnpack/common.h>


void xnn_f32_velu_ukernel__sse41_rr2_p6_x16(
    size_t n,
    const float* x,
    float* y,
    const union xnn_f32_elu_params params[restrict XNN_MIN_ELEMENTS(1)]) XNN_DISABLE_TSAN
{
  assert(n != 0);
  assert(n % sizeof(float) == 0);
  assert(x != NULL);
  assert(y != NULL);

  const __m128 vprescale = _mm_load_ps(params->sse.prescale);
  const __m128 valpha = _mm_load_ps(params->sse.alpha);
  const __m128 vbeta = _mm_load_ps(params->sse.beta);

  const __m128 vsat_cutoff = _mm_set1_ps(-0x1.154246p+4f);
  const __m128 vmagic_bias = _mm_set1_ps(0x1.8000FEp23f);
  const __m128 vlog2e = _mm_set1_ps(0x1.715476p+0f);
  const __m128 vminus_ln2_hi = _mm_set1_ps(-0x1.62E440p-1f);
  const __m128 vminus_ln2_lo = _mm_set1_ps(0x1.0105C6p-21f);
  const __m128 vc6 = _mm_set1_ps(0x1.6b7338p-10f);
  const __m128 vc5 = _mm_set1_ps(0x1.12278Ep-7f);
  const __m128 vc4 = _mm_set1_ps(0x1.555716p-5f);
  const __m128 vc3 = _mm_set1_ps(0x1.5554B0p-3f);
  const __m128 vc2 = _mm_set1_ps(0x1.FFFFFEp-2f);
  const __m128 vone = _mm_set1_ps(1.0f);

  for (; n >= 16 * sizeof(float); n -= 16 * sizeof(float)) {
    __m128 vx0123 = _mm_loadu_ps(x);
    __m128 vx4567 = _mm_loadu_ps(x + 4);
    __m128 vx89AB = _mm_loadu_ps(x + 8);
    __m128 vxCDEF = _mm_loadu_ps(x + 12);
    x += 16;

    const __m128 vz0123 = _mm_max_ps(vsat_cutoff, _mm_mul_ps(vx0123, vprescale));
    const __m128 vz4567 = _mm_max_ps(vsat_cutoff, _mm_mul_ps(vx4567, vprescale));
    const __m128 vz89AB = _mm_max_ps(vsat_cutoff, _mm_mul_ps(vx89AB, vprescale));
    const __m128 vzCDEF = _mm_max_ps(vsat_cutoff, _mm_mul_ps(vxCDEF, vprescale));

    __m128 vn0123 = _mm_add_ps(_mm_mul_ps(vz0123, vlog2e), vmagic_bias);
    __m128 vn4567 = _mm_add_ps(_mm_mul_ps(vz4567, vlog2e), vmagic_bias);
    __m128 vn89AB = _mm_add_ps(_mm_mul_ps(vz89AB, vlog2e), vmagic_bias);
    __m128 vnCDEF = _mm_add_ps(_mm_mul_ps(vzCDEF, vlog2e), vmagic_bias);

    __m128 vs0123 = _mm_castsi128_ps(_mm_slli_epi32(_mm_castps_si128(vn0123), 23));
    __m128 vs4567 = _mm_castsi128_ps(_mm_slli_epi32(_mm_castps_si128(vn4567), 23));
    __m128 vs89AB = _mm_castsi128_ps(_mm_slli_epi32(_mm_castps_si128(vn89AB), 23));
    __m128 vsCDEF = _mm_castsi128_ps(_mm_slli_epi32(_mm_castps_si128(vnCDEF), 23));

    vn0123 = _mm_sub_ps(vn0123, vmagic_bias);
    vn4567 = _mm_sub_ps(vn4567, vmagic_bias);
    vn89AB = _mm_sub_ps(vn89AB, vmagic_bias);
    vnCDEF = _mm_sub_ps(vnCDEF, vmagic_bias);

    __m128 vt0123 = _mm_add_ps(_mm_mul_ps(vn0123, vminus_ln2_hi), vz0123);
    __m128 vt4567 = _mm_add_ps(_mm_mul_ps(vn4567, vminus_ln2_hi), vz4567);
    __m128 vt89AB = _mm_add_ps(_mm_mul_ps(vn89AB, vminus_ln2_hi), vz89AB);
    __m128 vtCDEF = _mm_add_ps(_mm_mul_ps(vnCDEF, vminus_ln2_hi), vzCDEF);

    vt0123 = _mm_add_ps(_mm_mul_ps(vn0123, vminus_ln2_lo), vt0123);
    vt4567 = _mm_add_ps(_mm_mul_ps(vn4567, vminus_ln2_lo), vt4567);
    vt89AB = _mm_add_ps(_mm_mul_ps(vn89AB, vminus_ln2_lo), vt89AB);
    vtCDEF = _mm_add_ps(_mm_mul_ps(vnCDEF, vminus_ln2_lo), vtCDEF);

    __m128 vp0123 = _mm_add_ps(_mm_mul_ps(vc6, vt0123), vc5);
    __m128 vp4567 = _mm_add_ps(_mm_mul_ps(vc6, vt4567), vc5);
    __m128 vp89AB = _mm_add_ps(_mm_mul_ps(vc6, vt89AB), vc5);
    __m128 vpCDEF = _mm_add_ps(_mm_mul_ps(vc6, vtCDEF), vc5);

    vp0123 = _mm_add_ps(_mm_mul_ps(vp0123, vt0123), vc4);
    vp4567 = _mm_add_ps(_mm_mul_ps(vp4567, vt4567), vc4);
    vp89AB = _mm_add_ps(_mm_mul_ps(vp89AB, vt89AB), vc4);
    vpCDEF = _mm_add_ps(_mm_mul_ps(vpCDEF, vtCDEF), vc4);

    vp0123 = _mm_add_ps(_mm_mul_ps(vp0123, vt0123), vc3);
    vp4567 = _mm_add_ps(_mm_mul_ps(vp4567, vt4567), vc3);
    vp89AB = _mm_add_ps(_mm_mul_ps(vp89AB, vt89AB), vc3);
    vpCDEF = _mm_add_ps(_mm_mul_ps(vpCDEF, vtCDEF), vc3);

    vp0123 = _mm_add_ps(_mm_mul_ps(vp0123, vt0123), vc2);
    vp4567 = _mm_add_ps(_mm_mul_ps(vp4567, vt4567), vc2);
    vp89AB = _mm_add_ps(_mm_mul_ps(vp89AB, vt89AB), vc2);
    vpCDEF = _mm_add_ps(_mm_mul_ps(vpCDEF, vtCDEF), vc2);

    vp0123 = _mm_mul_ps(vp0123, vt0123);
    vp4567 = _mm_mul_ps(vp4567, vt4567);
    vp89AB = _mm_mul_ps(vp89AB, vt89AB);
    vpCDEF = _mm_mul_ps(vpCDEF, vtCDEF);

    vt0123 = _mm_mul_ps(vt0123, vs0123);
    vs0123 = _mm_sub_ps(vs0123, vone);
    vt4567 = _mm_mul_ps(vt4567, vs4567);
    vs4567 = _mm_sub_ps(vs4567, vone);
    vt89AB = _mm_mul_ps(vt89AB, vs89AB);
    vs89AB = _mm_sub_ps(vs89AB, vone);
    vtCDEF = _mm_mul_ps(vtCDEF, vsCDEF);
    vsCDEF = _mm_sub_ps(vsCDEF, vone);

    vp0123 = _mm_add_ps(_mm_mul_ps(vp0123, vt0123), vt0123);
    vp4567 = _mm_add_ps(_mm_mul_ps(vp4567, vt4567), vt4567);
    vp89AB = _mm_add_ps(_mm_mul_ps(vp89AB, vt89AB), vt89AB);
    vpCDEF = _mm_add_ps(_mm_mul_ps(vpCDEF, vtCDEF), vtCDEF);

    const __m128 ve0123 = _mm_mul_ps(_mm_add_ps(vp0123, vs0123), valpha);
    const __m128 ve4567 = _mm_mul_ps(_mm_add_ps(vp4567, vs4567), valpha);
    const __m128 ve89AB = _mm_mul_ps(_mm_add_ps(vp89AB, vs89AB), valpha);
    const __m128 veCDEF = _mm_mul_ps(_mm_add_ps(vpCDEF, vsCDEF), valpha);

    vx0123 = _mm_mul_ps(vx0123, vbeta);
    vx4567 = _mm_mul_ps(vx4567, vbeta);
    vx89AB = _mm_mul_ps(vx89AB, vbeta);
    vxCDEF = _mm_mul_ps(vxCDEF, vbeta);

    const __m128 vy0123 = _mm_blendv_ps(vx0123, ve0123, vx0123);
    const __m128 vy4567 = _mm_blendv_ps(vx4567, ve4567, vx4567);
    const __m128 vy89AB = _mm_blendv_ps(vx89AB, ve89AB, vx89AB);
    const __m128 vyCDEF = _mm_blendv_ps(vxCDEF, veCDEF, vxCDEF);

    _mm_storeu_ps(y, vy0123);
    _mm_storeu_ps(y + 4, vy4567);
    _mm_storeu_ps(y + 8, vy89AB);
    _mm_storeu_ps(y + 12, vyCDEF);
    y += 16;
  }
  for (; n >= 4 * sizeof(float); n -= 4 * sizeof(float)) {
    __m128 vx = _mm_loadu_ps(x);
    x += 4;

    const __m128 vz = _mm_max_ps(vsat_cutoff, _mm_mul_ps(vx, vprescale));

    __m128 vn = _mm_add_ps(_mm_mul_ps(vz, vlog2e), vmagic_bias);
    __m128 vs = _mm_castsi128_ps(_mm_slli_epi32(_mm_castps_si128(vn), 23));
    vn = _mm_sub_ps(vn, vmagic_bias);

    __m128 vt = _mm_add_ps(_mm_mul_ps(vn, vminus_ln2_hi), vz);
    vt = _mm_add_ps(_mm_mul_ps(vn, vminus_ln2_lo), vt);

    __m128 vp = _mm_add_ps(_mm_mul_ps(vc6, vt), vc5);
    vp = _mm_add_ps(_mm_mul_ps(vp, vt), vc4);
    vp = _mm_add_ps(_mm_mul_ps(vp, vt), vc3);
    vp = _mm_add_ps(_mm_mul_ps(vp, vt), vc2);
    vp = _mm_mul_ps(vp, vt);

    vt = _mm_mul_ps(vt, vs);
    vs = _mm_sub_ps(vs, vone);
    vp = _mm_add_ps(_mm_mul_ps(vp, vt), vt);
    const __m128 ve = _mm_mul_ps(_mm_add_ps(vp, vs), valpha);

    vx = _mm_mul_ps(vx, vbeta);
    const __m128 vy = _mm_blendv_ps(vx, ve, vx);

    _mm_storeu_ps(y, vy);
    y += 4;
  }
  if XNN_UNLIKELY(n != 0) {
    __m128 vx = _mm_loadu_ps(x);

    const __m128 vz = _mm_max_ps(vsat_cutoff, _mm_mul_ps(vx, vprescale));

    __m128 vn = _mm_add_ps(_mm_mul_ps(vz, vlog2e), vmagic_bias);
    __m128 vs = _mm_castsi128_ps(_mm_slli_epi32(_mm_castps_si128(vn), 23));
    vn = _mm_sub_ps(vn, vmagic_bias);

    __m128 vt = _mm_add_ps(_mm_mul_ps(vn, vminus_ln2_hi), vz);
    vt = _mm_add_ps(_mm_mul_ps(vn, vminus_ln2_lo), vt);

    __m128 vp = _mm_add_ps(_mm_mul_ps(vc6, vt), vc5);
    vp = _mm_add_ps(_mm_mul_ps(vp, vt), vc4);
    vp = _mm_add_ps(_mm_mul_ps(vp, vt), vc3);
    vp = _mm_add_ps(_mm_mul_ps(vp, vt), vc2);
    vp = _mm_mul_ps(vp, vt);

    vt = _mm_mul_ps(vt, vs);
    vs = _mm_sub_ps(vs, vone);
    vp = _mm_add_ps(_mm_mul_ps(vp, vt), vt);
    const __m128 ve = _mm_mul_ps(_mm_add_ps(vp, vs), valpha);

    vx = _mm_mul_ps(vx, vbeta);
    __m128 vy = _mm_blendv_ps(vx, ve, vx);

    if (n & (2 * sizeof(float))) {
      _mm_storel_pi((__m64*) y, vy);
      vy = _mm_movehl_ps(vy, vy);
      y += 2;
    }
    if (n & (1 * sizeof(float))) {
      _mm_store_ss(y, vy);
    }
  }
}
