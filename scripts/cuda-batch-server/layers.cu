// SPDX-License-Identifier: AGPL-3.0-only
// One cooperative launch executes a complete encoder/decoder layer. No device
// dynamic launches, inter-kernel synchronization, or KV cache. SM86 Tensor Cores.
#include <algorithm>
#include <cmath>
#include <cooperative_groups.h>
#include <cstdint>
#include <cuda_fp16.h>
#include <cuda_pipeline.h>
#include <cuda_runtime.h>
#include <mma.h>
#include <mutex>
#include <type_traits>
#if USE_CUTLASS
#include "cutlass/epilogue/thread/linear_combination.h"
#include "cutlass/gemm/kernel/default_gemm.h"
#include "cutlass/gemm/threadblock/threadblock_swizzle.h"
#endif
namespace cg = cooperative_groups;
namespace wmma = nvcuda::wmma;

#ifndef GROUP_M
#define GROUP_M 8
#endif
#ifndef USE_CUTLASS
#define USE_CUTLASS 0
#endif
using linear_input = half;
struct Layer {
    const half *qkv, *out, *gateup, *down, *ple_gate, *ple_out;
    const float *in_norm, *q_norm, *k_norm, *attn_norm, *ffn_norm, *ffn_post, *ple_norm, *scalar;
    const half *ple;
    float *x;
    linear_input *norm, *proj;
    half *q, *k, *vt;
    float *scores;
    half *attn;
    float *hidden, *result, *qkv_buffer;
    half *prob;
    const int *lengths, *block_offsets, *batch_ids, *positions;
    int batch, seq, width, heads, kv_heads, dim, ffn, gemma, tokens;
    float theta;
};

__device__ __forceinline__ float warp_sum(float v) {
    for (int n = 16; n; n /= 2)
        v += __shfl_xor_sync(0xffffffff, v, n);
    return v;
}
__device__ __forceinline__ float warp_max(float v) {
    for (int n = 16; n; n /= 2)
        v = fmaxf(v, __shfl_xor_sync(0xffffffff, v, n));
    return v;
}
__device__ __forceinline__ float activation(float x, bool gemma) {
    return gemma ? .5f * x * (1.f + tanhf(.7978845608028654f * (x + .044715f * x * x * x)))
                 : x / (1.f + expf(-x));
}

__device__ __forceinline__ uint32_t pack_half2(float x, float y) {
    __half2_raw bits = __floats2half2_rn(x, y);
    return uint32_t(bits.x) | (uint32_t(bits.y) << 16);
}
__device__ __forceinline__ void store_linear4(linear_input *dst, float4 value) {
    *reinterpret_cast<uint2 *>(dst) = make_uint2(pack_half2(value.x, value.y), pack_half2(value.z, value.w));
}

#ifndef TILE_M
#define TILE_M 64
#endif
#ifndef TILE_N
#define TILE_N 128
#endif
#ifndef TILE_K
#define TILE_K 32
#endif
#ifndef THREAD_WARPS
#define THREAD_WARPS 8
#endif
#ifndef ASYNC_COPY
#define ASYNC_COPY 0
#endif
constexpr int STAGES = 1 + ASYNC_COPY;
constexpr int WARPS = THREAD_WARPS, WC = WARPS / 2, TM = TILE_M, TN = TILE_N, TK = TILE_K, SKEW = 16;
#if USE_CUTLASS
#ifndef CUTLASS_K
#define CUTLASS_K 32
#endif
#ifndef CUTLASS_STAGES
#define CUTLASS_STAGES 3
#endif
using CutlassCore = typename cutlass::gemm::kernel::DefaultGemm<
    cutlass::half_t, cutlass::layout::RowMajor, 8, cutlass::half_t, cutlass::layout::ColumnMajor, 8, float,
    cutlass::layout::RowMajor, float, cutlass::arch::OpClassTensorOp, cutlass::arch::Sm80,
    cutlass::gemm::GemmShape<TM, TN, CUTLASS_K>, cutlass::gemm::GemmShape<TM / 2, TN / WC, CUTLASS_K>,
    cutlass::gemm::GemmShape<16, 8, 16>, cutlass::epilogue::thread::LinearCombination<float, 4, float, float>,
    cutlass::gemm::threadblock::GemmIdentityThreadblockSwizzle<>, CUTLASS_STAGES, false,
    cutlass::arch::OpMultiplyAdd>::GemmKernel;
static_assert(CutlassCore::kThreadCount == WARPS * 32);
#endif
static_assert(TM % 32 == 0 && TK % 32 == 0 && TM <= 128 && TK <= 64 && TN % (WC * 16) == 0 && TN <= 128 &&
              (WARPS == 4 || WARPS == 8));

__device__ __forceinline__ void copy16(half *dst, const half *src, bool valid) {
    if constexpr (ASYNC_COPY) {
        unsigned address = __cvta_generic_to_shared(dst);
        asm volatile("cp.async.cg.shared.global [%0], [%1], 16, %2;" ::"r"(address), "l"(src),
                     "r"(valid ? 16 : 0));
    } else
        *reinterpret_cast<uint4 *>(dst) =
            valid ? *reinterpret_cast<const uint4 *>(src) : make_uint4(0, 0, 0, 0);
}

template <bool BT>
__device__ __forceinline__ void load_tiles(const half *a, const half *b, half *sa, half *sb, int M, int N,
                                           int K, int astride, int bstride, int g, int bg, int mr, int nr,
                                           int kk) {
    constexpr int STRIDE = TK + SKEW;
    for (int i = threadIdx.x; i < TM * (TK / 8); i += blockDim.x) {
        int m = mr + i / (TK / 8), k = kk + (i % (TK / 8)) * 8;
        copy16(sa + (i / (TK / 8)) * STRIDE + (i % (TK / 8)) * 8, a + g * astride + m * K + k,
               m < M && k < K);
    }
    for (int i = threadIdx.x; i < TN * (TK / 8); i += blockDim.x) {
        if constexpr (BT) {
            int n = nr + i / (TK / 8), k = kk + (i % (TK / 8)) * 8;
            copy16(sb + (i / (TK / 8)) * STRIDE + (i % (TK / 8)) * 8, b + bg * bstride + n * K + k,
                   n < N && k < K);
        } else {
            int k = kk + i / (TN / 8), n = nr + (i % (TN / 8)) * 8;
            copy16(sb + (i / (TN / 8)) * (TN + SKEW) + (i % (TN / 8)) * 8, b + bg * bstride + k * N + n,
                   n < N && k < K);
        }
    }
}

// Eight warps compute a 64x128 tile. Four accumulators per warp remain
// in registers. Vectorized 128-bit loads stage A/B once per CTA; each warp
// reuses its private 16x16 shared output scratch across the four accumulators.
template <bool BT = true, typename Out>
__device__ __forceinline__ void mm(const half *a, const half *b, Out *c, int M, int N, int K, int groups,
                                   int astride, int bstride, int cstride, half *sm, int causal = 0,
                                   int bheads = 0, int aheads = 0, const int *lengths = nullptr) {
    int lane = threadIdx.x % 32, warp = threadIdx.x / 32;
    int tiles_m = (M + TM - 1) / TM, tiles_n = (N + TN - 1) / TN;
    constexpr int STRIDE = TK + SKEW; // 16-half skew removes WMMA shared-bank conflicts.
    half *sa = sm, *sb = sm + TM * STRIDE;
    float *sc = reinterpret_cast<float *>(sm + STAGES * (TM + TN) * STRIDE);
    for (int task = blockIdx.x; task < groups * tiles_m * tiles_n; task += gridDim.x) {
        int g = task / (tiles_m * tiles_n), t = task % (tiles_m * tiles_n);
        int bg = bheads ? g / aheads * bheads + (g % aheads) / (aheads / bheads) : g;
        int length = lengths ? lengths[g / aheads] : M;
        // Group M tiles so concurrent CTAs reuse the same weight tile in L2.
        // Row-major traversal evicts large matrices before their next reuse.
        int first_m = (t / (GROUP_M * tiles_n)) * GROUP_M;
        int group_m = min(tiles_m - first_m, GROUP_M);
        int within = t % (GROUP_M * tiles_n);
        int mr = (first_m + within % group_m) * TM, nr = (within / group_m) * TN;
        if (lengths && (mr >= length || (BT && nr >= length)))
            continue;
        if (causal == 1 && nr >= mr + TM)
            continue;
        int limit_k = causal == 2 ? min(K, mr + TM) : K;
        if constexpr (!BT)
            if (lengths)
                limit_k = min(limit_k, length);
        wmma::fragment<wmma::accumulator, 16, 16, 16, float> acc[TM / 32][TN / (WC * 16)];
#pragma unroll
        for (int m = 0; m < TM / 32; ++m) {
#pragma unroll
            for (int n = 0; n < TN / (WC * 16); ++n)
                wmma::fill_fragment(acc[m][n], 0.f);
        }
        if constexpr (ASYNC_COPY) {
            load_tiles<BT>(a, b, sm, sm + TM * STRIDE, M, N, K, astride, bstride, g, bg, mr, nr, 0);
            __pipeline_commit();
            __pipeline_wait_prior(0);
            __syncthreads();
        }
        for (int kk = 0; kk < limit_k; kk += TK) {
            if constexpr (ASYNC_COPY) {
                sa = sm + ((kk / TK) % 2) * (TM + TN) * STRIDE;
                sb = sa + TM * STRIDE;
                if (kk + TK < limit_k) {
                    half *next = sm + ((kk / TK + 1) % 2) * (TM + TN) * STRIDE;
                    load_tiles<BT>(a, b, next, next + TM * STRIDE, M, N, K, astride, bstride, g, bg, mr, nr,
                                   kk + TK);
                    __pipeline_commit();
                }
            } else {
                load_tiles<BT>(a, b, sa, sb, M, N, K, astride, bstride, g, bg, mr, nr, kk);
                __syncthreads();
            }
#pragma unroll
            for (int k = 0; k < TK; k += 16) {
                wmma::fragment<wmma::matrix_a, 16, 16, 16, half, wmma::row_major> af[TM / 32];
                wmma::fragment<wmma::matrix_b, 16, 16, 16, half,
                               typename std::conditional<BT, wmma::col_major, wmma::row_major>::type>
                    bf[TN / (WC * 16)];
#pragma unroll
                for (int m = 0; m < TM / 32; ++m)
                    wmma::load_matrix_sync(af[m], sa + ((warp / WC) * (TM / 2) + m * 16) * STRIDE + k,
                                           STRIDE);
#pragma unroll
                for (int n = 0; n < TN / (WC * 16); ++n) {
                    if constexpr (BT)
                        wmma::load_matrix_sync(bf[n], sb + ((warp % WC) * (TN / WC) + n * 16) * STRIDE + k,
                                               STRIDE);
                    else
                        wmma::load_matrix_sync(bf[n], sb + k * (TN + SKEW) + (warp % WC) * (TN / WC) + n * 16,
                                               TN + SKEW);
                }
#pragma unroll
                for (int m = 0; m < TM / 32; ++m) {
#pragma unroll
                    for (int n = 0; n < TN / (WC * 16); ++n)
                        wmma::mma_sync(acc[m][n], af[m], bf[n], acc[m][n]);
                }
            }
            if constexpr (ASYNC_COPY)
                __pipeline_wait_prior(0);
            __syncthreads();
        }
#pragma unroll
        for (int mi = 0; mi < TM / 32; ++mi) {
#pragma unroll
            for (int ni = 0; ni < TN / (WC * 16); ++ni) {
                wmma::store_matrix_sync(sc + warp * 256, acc[mi][ni], 16, wmma::mem_row_major);
                __syncwarp();
                int i = lane * 8, m = mr + (warp / WC) * (TM / 2) + mi * 16 + i / 16,
                    n = nr + (warp % WC) * (TN / WC) + ni * 16 + i % 16;
                if (m < M && n < N) {
                    float4 a = *reinterpret_cast<float4 *>(sc + warp * 256 + i);
                    float4 b = *reinterpret_cast<float4 *>(sc + warp * 256 + i + 4);
                    if constexpr (std::is_same<Out, half>::value) {
                        uint4 packed = make_uint4(pack_half2(a.x, a.y), pack_half2(a.z, a.w),
                                                  pack_half2(b.x, b.y), pack_half2(b.z, b.w));
                        *reinterpret_cast<uint4 *>(c + g * cstride + m * N + n) = packed;
                    } else {
                        *reinterpret_cast<float4 *>(c + g * cstride + m * N + n) = a;
                        *reinterpret_cast<float4 *>(c + g * cstride + m * N + n + 4) = b;
                    }
                }
                __syncwarp();
            }
        }
        __syncthreads();
    }
}

__device__ __forceinline__ void norm_x(const Layer &p, const float *weight, linear_input *dst) {
    int lane = threadIdx.x % 32;
    for (int row = blockIdx.x * WARPS + threadIdx.x / 32; row < p.tokens; row += gridDim.x * WARPS) {
        float sq = 0;
        float4 values[8];
#pragma unroll
        for (int i = 0; i < 8; ++i) {
            int j = lane * 4 + i * 128;
            values[i] = j < p.width ? *reinterpret_cast<const float4 *>(p.x + row * p.width + j)
                                    : make_float4(0, 0, 0, 0);
            float4 v = values[i];
            sq += v.x * v.x + v.y * v.y + v.z * v.z + v.w * v.w;
        }
        float r = rsqrtf(warp_sum(sq) / p.width + 1e-6f);
#pragma unroll
        for (int i = 0; i < 8; ++i) {
            int j = lane * 4 + i * 128;
            if (j < p.width) {
                float4 v = values[i], w = *reinterpret_cast<const float4 *>(weight + j);
                store_linear4(dst + row * p.width + j,
                              make_float4(v.x * r * w.x, v.y * r * w.y, v.z * r * w.z, v.w * r * w.w));
            }
        }
    }
}

#if USE_CUTLASS
__device__ __forceinline__ void linear(const Layer &p, const half *a, const half *b, float *c, int M, int N,
                                       int K, half *sm) {
    using Mma = CutlassCore::Mma;
    using Epilogue = CutlassCore::Epilogue;
    using Output = CutlassCore::OutputOp;
    auto &shared = *reinterpret_cast<CutlassCore::SharedStorage *>(sm);
    typename Mma::IteratorA::Params pa{cutlass::layout::RowMajor(K)};
    typename Mma::IteratorB::Params pb{cutlass::layout::ColumnMajor(K)};
    typename Epilogue::OutputTileIterator::Params pc{cutlass::layout::RowMajor(N)};
    int tid = threadIdx.x, warp = __shfl_sync(0xffffffff, tid / 32, 0), lane = tid % 32;
    int tiles_m = (M + TM - 1) / TM, tiles_n = (N + TN - 1) / TN;
    for (int task = blockIdx.x; task < tiles_m * tiles_n; task += gridDim.x) {
        int first_m = (task / (GROUP_M * tiles_n)) * GROUP_M, group_m = min(tiles_m - first_m, GROUP_M),
            within = task % (GROUP_M * tiles_n);
        int mr = (first_m + within % group_m) * TM, nr = (within / group_m) * TN;
        typename Mma::IteratorA ia(pa, reinterpret_cast<cutlass::half_t *>(const_cast<half *>(a)), {M, K},
                                   tid, {mr, 0});
        typename Mma::IteratorB ib(pb, reinterpret_cast<cutlass::half_t *>(const_cast<half *>(b)), {K, N},
                                   tid, {0, nr});
        Mma mma(shared.main_loop, tid, warp, lane);
        typename Mma::FragmentC acc;
        acc.clear();
        mma((K + CUTLASS_K - 1) / CUTLASS_K, acc, ia, ib, acc);
        __syncthreads();
        typename Epilogue::OutputTileIterator output(pc, c, {M, N}, tid, {mr, nr});
        Epilogue epilogue(shared.epilogue, tid, warp, lane);
        epilogue(Output(1.f, 0.f), output, acc, output);
        __syncthreads();
    }
}
#else
__device__ __forceinline__ void linear(const Layer &p, const half *a, const half *b, float *c, int M, int N,
                                       int K, half *sm) {
    mm(a, b, c, M, N, K, 1, 0, 0, 0, sm);
}
#endif

__device__ __forceinline__ void residual(const Layer &p, const float *src, const float *weight, bool scale,
                                         const float *next_norm = nullptr, linear_input *norm_dst = nullptr) {
    int lane = threadIdx.x % 32;
    for (int row = blockIdx.x * WARPS + threadIdx.x / 32; row < p.tokens; row += gridDim.x * WARPS) {
        float r = 1;
        float4 values[8];
        float sq = 0;
#pragma unroll
        for (int n = 0; n < 8; ++n) {
            int j = lane * 4 + n * 128;
            values[n] = j < p.width ? *reinterpret_cast<const float4 *>(src + row * p.width + j)
                                    : make_float4(0, 0, 0, 0);
            if (weight) {
                float4 v = values[n];
                sq += v.x * v.x + v.y * v.y + v.z * v.z + v.w * v.w;
            }
        }
        if (weight)
            r = rsqrtf(warp_sum(sq) / p.width + 1e-6f);
        float factor = scale ? *p.scalar : 1.f;
        float next_sq = 0;
#pragma unroll
        for (int n = 0; n < 8; ++n) {
            int j = lane * 4 + n * 128;
            if (j < p.width) {
                int i = row * p.width + j;
                float4 v = values[n],
                       w = weight ? *reinterpret_cast<const float4 *>(weight + j) : make_float4(1, 1, 1, 1);
                float4 x = *reinterpret_cast<const float4 *>(p.x + i);
                v = make_float4((x.x + v.x * r * w.x) * factor, (x.y + v.y * r * w.y) * factor,
                                (x.z + v.z * r * w.z) * factor, (x.w + v.w * r * w.w) * factor);
                *reinterpret_cast<float4 *>(p.x + i) = v;
                values[n] = v;
                if (next_norm)
                    next_sq += v.x * v.x + v.y * v.y + v.z * v.z + v.w * v.w;
                else if (norm_dst)
                    store_linear4(norm_dst + i, v);
            }
        }
        if (next_norm) {
            float nr = rsqrtf(warp_sum(next_sq) / p.width + 1e-6f);
#pragma unroll
            for (int n = 0; n < 8; ++n) {
                int j = lane * 4 + n * 128;
                if (j < p.width) {
                    float4 v = values[n], w = *reinterpret_cast<const float4 *>(next_norm + j);
                    store_linear4(norm_dst + row * p.width + j, make_float4(v.x * nr * w.x, v.y * nr * w.y,
                                                                            v.z * nr * w.z, v.w * nr * w.w));
                }
            }
        }
    }
}

__device__ __forceinline__ void qkv_rope(const Layer &p) {
    int lane = threadIdx.x % 32, hd = p.heads * p.dim, kd = p.kv_heads * p.dim;
    for (int row = blockIdx.x * WARPS + threadIdx.x / 32; row < p.batch * p.seq * p.heads;
         row += gridDim.x * WARPS) {
        int head = row % p.heads, pos = (row / p.heads) % p.seq, batch = row / (p.heads * p.seq);
        int kv = head / (p.heads / p.kv_heads);
        bool owner = head % (p.heads / p.kv_heads) == 0;
        if (pos >= (p.lengths[batch] + 31) / 32 * 32) {
            for (int j = lane; j < p.dim; j += 32) {
                int oi = ((batch * p.heads + head) * p.seq + pos) * p.dim + j;
                p.q[oi] = __float2half(0.f);
                if (owner) {
                    int ki = ((batch * p.kv_heads + kv) * p.seq + pos) * p.dim + j;
                    p.k[ki] = __float2half(0.f);
                    p.vt[ki] = __float2half(0.f);
                }
            }
            continue;
        }
        int token = p.block_offsets[batch * (p.seq / 32) + pos / 32] + pos % 32;
        int base = token * (hd + 2 * kd);
        int qi = base + head * p.dim, ki = base + hd + kv * p.dim, vi = base + hd + kd + kv * p.dim;
        float qs = 0, ks = 0, vs = 0;
        for (int j = lane; j < p.dim; j += 32) {
            float q = p.qkv_buffer[qi + j];
            qs += q * q;
            if (owner) {
                float k = p.qkv_buffer[ki + j], v = p.qkv_buffer[vi + j];
                ks += k * k;
                vs += v * v;
            }
        }
        float qr = rsqrtf(warp_sum(qs) / p.dim + 1e-6f),
              kr = owner ? rsqrtf(warp_sum(ks) / p.dim + 1e-6f) : 0.f;
        float vr = p.gemma && owner ? rsqrtf(warp_sum(vs) / p.dim + 1e-6f) : 1.f;
        for (int j = lane; j < p.dim; j += 32) {
            int other = (j + p.dim / 2) % p.dim;
            // Positions are bounded to 0..767. The intrinsic avoids libdevice's
            // large-angle reduction stack while retaining ample RoPE accuracy.
            float angle = pos * exp2f(-2.f * (j % (p.dim / 2)) / p.dim * __log2f(p.theta)), s, c;
            __sincosf(angle, &s, &c);
            float q = p.qkv_buffer[qi + j] * qr * p.q_norm[j];
            float qo = p.qkv_buffer[qi + other] * qr * p.q_norm[other];
            int oi = ((batch * p.heads + head) * p.seq + pos) * p.dim + j;
            p.q[oi] = __float2half(q * c + (j < p.dim / 2 ? -qo : qo) * s);
            if (owner) {
                float k = p.qkv_buffer[ki + j] * kr * p.k_norm[j],
                      ko = p.qkv_buffer[ki + other] * kr * p.k_norm[other];
                int kiout = ((batch * p.kv_heads + kv) * p.seq + pos) * p.dim + j;
                p.k[kiout] = __float2half(k * c + (j < p.dim / 2 ? -ko : ko) * s);
                p.vt[kiout] = __float2half(p.qkv_buffer[vi + j] * vr);
            }
        }
    }
}

__device__ __forceinline__ void softmax(const Layer &p) {
    int lane = threadIdx.x % 32;
    for (int row = blockIdx.x * WARPS + threadIdx.x / 32; row < p.batch * p.heads * p.seq;
         row += gridDim.x * WARPS) {
        int pos = row % p.seq, b = row / (p.seq * p.heads);
        if (pos >= p.lengths[b]) {
            for (int j = lane; j < p.seq; j += 32)
                p.prob[row * p.seq + j] = __float2half(0.f);
            continue;
        }
        int limit = min(p.lengths[b], p.gemma ? p.seq : pos + 1);
        float mx = -INFINITY, scale = p.gemma ? 1.f : rsqrtf(float(p.dim)), values[24];
// The 768-token ceiling bounds each lane to 24 logits. Retain them
// through max/sum normalization instead of reading scores three times.
#pragma unroll
        for (int i = 0; i < 24; ++i) {
            int j = lane + i * 32;
            values[i] = j < limit ? p.scores[row * p.seq + j] * scale : -INFINITY;
            mx = fmaxf(mx, values[i]);
        }
        mx = warp_max(mx);
        float sum = 0;
#pragma unroll
        for (int i = 0; i < 24; ++i) {
            values[i] = expf(values[i] - mx);
            sum += values[i];
        }
        float inv = 1.f / warp_sum(sum);
#pragma unroll
        for (int i = 0; i < 24; ++i) {
            int j = lane + i * 32;
            if (j < p.seq)
                p.prob[row * p.seq + j] = __float2half(values[i] * inv);
        }
    }
}

#ifndef MIN_BLOCKS
#define MIN_BLOCKS 1
#endif
__global__ __launch_bounds__(WARPS * 32, MIN_BLOCKS) void layer_forward(Layer p) {
#if MODEL_KIND == 1
    p.gemma = 0;
    p.width = 1024;
    p.heads = 16;
    p.kv_heads = 8;
    p.dim = 128;
    p.ffn = 3072;
    p.theta = 1000000.f;
#elif MODEL_KIND == 2
    p.gemma = 1;
    p.width = 512;
    p.heads = 4;
    p.ffn = 2048;
#endif
    extern __shared__ __align__(32) half sm[];
    auto grid = cg::this_grid();
    int T = p.tokens, H = p.heads * p.dim, K = p.kv_heads * p.dim;
    norm_x(p, p.in_norm, p.norm);
    grid.sync();
    linear(p, p.norm, p.qkv, p.qkv_buffer, T, H + 2 * K, p.width, sm);
    grid.sync();
    qkv_rope(p);
    grid.sync();
    mm(p.q, p.k, p.scores, p.seq, p.seq, p.dim, p.batch * p.heads, p.seq * p.dim, p.seq * p.dim,
       p.seq * p.seq, sm, p.gemma ? 0 : 1, p.kv_heads, p.heads, p.lengths);
    grid.sync();
    softmax(p);
    grid.sync();
    mm<false>(p.prob, p.vt, p.attn, p.seq, p.dim, p.seq, p.batch * p.heads, p.seq * p.seq, p.dim * p.seq,
              p.seq * p.dim, sm, p.gemma ? 0 : 2, p.kv_heads, p.heads, p.lengths);
    grid.sync();
    for (int i = blockIdx.x * blockDim.x + threadIdx.x; i < T * H; i += gridDim.x * blockDim.x) {
        int j = i % p.dim, head = (i / p.dim) % p.heads, token = i / H;
        int batch = p.batch_ids[token], pos = p.positions[token];
        p.proj[i] = pos < p.lengths[batch]
                        ? linear_input(p.attn[((batch * p.heads + head) * p.seq + pos) * p.dim + j])
                        : linear_input(0.f);
    }
    grid.sync();
    linear(p, p.proj, p.out, p.result, T, p.width, H, sm);
    grid.sync();
    residual(p, p.result, p.gemma ? p.attn_norm : nullptr, false, p.ffn_norm, p.norm);
    grid.sync();
    linear(p, p.norm, p.gateup, p.hidden, T, 2 * p.ffn, p.width, sm);
    grid.sync();
    for (int v = blockIdx.x * blockDim.x + threadIdx.x; v < T * p.ffn / 4; v += gridDim.x * blockDim.x) {
        int i = v * 4, r = i / p.ffn, j = i % p.ffn;
        float4 gate = *reinterpret_cast<const float4 *>(p.hidden + r * 2 * p.ffn + j);
        float4 up = *reinterpret_cast<const float4 *>(p.hidden + r * 2 * p.ffn + p.ffn + j);
        store_linear4(p.proj + i,
                      make_float4(activation(gate.x, p.gemma) * up.x, activation(gate.y, p.gemma) * up.y,
                                  activation(gate.z, p.gemma) * up.z, activation(gate.w, p.gemma) * up.w));
    }
    grid.sync();
    linear(p, p.proj, p.down, p.result, T, p.width, p.ffn, sm);
    grid.sync();
    residual(p, p.result, p.gemma ? p.ffn_post : nullptr, false, nullptr, p.gemma ? p.norm : nullptr);
    grid.sync();
    if (p.gemma) {
        // The residual stage already staged the unnormalized PLE input.
        linear(p, p.norm, p.ple_gate, p.result, T, p.width, p.width, sm);
        grid.sync();
        for (int i = blockIdx.x * blockDim.x + threadIdx.x; i < T * p.width; i += gridDim.x * blockDim.x)
            p.norm[i] = linear_input(activation(p.result[i], true) * __half2float(p.ple[i]));
        grid.sync();
        linear(p, p.norm, p.ple_out, p.result, T, p.width, p.width, sm);
        grid.sync();
        residual(p, p.result, p.ple_norm, true);
        grid.sync();
    }
}

extern "C" int launch_layer(Layer *params, cudaStream_t stream, int *blocks_out, int block_limit) {
    constexpr int threads = WARPS * 32, regular_smem = STAGES * (TM + TN) * (TK + SKEW) * 2 + WARPS * 256 * 4;
#if USE_CUTLASS
    constexpr int smem = std::max(regular_smem, int(sizeof(CutlassCore::SharedStorage)));
#else
    constexpr int smem = regular_smem;
#endif
    // Device properties are expensive on this driver. Cache the occupancy-safe
    // cooperative grid once; the service deliberately owns one CUDA device.
    static std::once_flag once;
    static int blocks = 0, device = -1;
    static cudaError_t status = cudaSuccess;
    std::call_once(once, [&]() {
        cudaDeviceProp prop{};
        status = cudaGetDevice(&device);
        if (status != cudaSuccess)
            return;
        status = cudaGetDeviceProperties(&prop, device);
        if (status != cudaSuccess)
            return;
        if (!prop.cooperativeLaunch || prop.major != 8 || prop.minor != 6) {
            status = cudaErrorNotSupported;
            return;
        }
        if (smem > 49152) {
            status = cudaFuncSetAttribute(layer_forward, cudaFuncAttributeMaxDynamicSharedMemorySize, smem);
            if (status != cudaSuccess)
                return;
        }
        status = cudaOccupancyMaxActiveBlocksPerMultiprocessor(&blocks, layer_forward, threads, smem);
        blocks *= prop.multiProcessorCount;
    });
    if (status != cudaSuccess)
        return status;
    int current;
    auto e = cudaGetDevice(&current);
    if (e != cudaSuccess)
        return e;
    if (current != device)
        return cudaErrorInvalidDevice;
    int grid_blocks = block_limit > 0 ? std::min(blocks, block_limit) : blocks;
    *blocks_out = grid_blocks;
    void *args[] = {params};
    return cudaLaunchCooperativeKernel((void *)layer_forward, dim3(grid_blocks), dim3(threads), args, smem,
                                       stream);
}
extern "C" const char *error_string(int e) { return cudaGetErrorString((cudaError_t)e); }
extern "C" int layer_abi_version() { return 3; }
