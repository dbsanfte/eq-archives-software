// SPDX-License-Identifier: AGPL-3.0-only
// SM86 causal attention, called inside the cooperative full-layer kernel.
// A CTA owns 32 queries and streams 64 keys at a time. FP32 output
// accumulators stay in registers; no sequence-squared global workspace.
constexpr int ATTENTION_SMEM = (32 * 144 + 64 * 144 + 32 * 80) * 2 + (32 * 64 + 96) * 4;
static_assert(WARPS == 8 && MODEL_KIND == 1, "Causal attention requires the Qwen SM86 profile");

__device__ __forceinline__ void load_a(uint32_t (&r)[4], const half *p, int stride) {
    int lane = threadIdx.x % 32;
    unsigned address = __cvta_generic_to_shared(p + (lane % 16) * stride + (lane / 16) * 8);
    asm volatile("ldmatrix.sync.aligned.m8n8.x4.shared.b16 {%0,%1,%2,%3}, [%4];"
                 : "=r"(r[0]), "=r"(r[1]), "=r"(r[2]), "=r"(r[3]) : "r"(address));
}

template <bool TRANSPOSE>
__device__ __forceinline__ void load_b(uint32_t (&r)[2], const half *p, int stride) {
    int lane = threadIdx.x % 32;
    unsigned address = __cvta_generic_to_shared(
        p + (TRANSPOSE ? (lane % 16) * stride : (lane % 8) * stride + ((lane / 8) % 2) * 8));
    if constexpr (TRANSPOSE)
        asm volatile("ldmatrix.sync.aligned.m8n8.x2.trans.shared.b16 {%0,%1}, [%2];"
                     : "=r"(r[0]), "=r"(r[1]) : "r"(address));
    else
        asm volatile("ldmatrix.sync.aligned.m8n8.x2.shared.b16 {%0,%1}, [%2];"
                     : "=r"(r[0]), "=r"(r[1]) : "r"(address));
}

__device__ __forceinline__ void mma16816(float (&c)[4], const uint32_t (&a)[4], const uint32_t (&b)[2]) {
    asm volatile("mma.sync.aligned.m16n8k16.row.col.f32.f16.f16.f32 "
                 "{%0,%1,%2,%3}, {%4,%5,%6,%7}, {%8,%9}, {%0,%1,%2,%3};"
                 : "+f"(c[0]), "+f"(c[1]), "+f"(c[2]), "+f"(c[3])
                 : "r"(a[0]), "r"(a[1]), "r"(a[2]), "r"(a[3]), "r"(b[0]), "r"(b[1]));
}

__device__ __forceinline__ void causal_attention(const Layer &p, half *sm) {
    constexpr int D = 128, STRIDE = 144, PSTRIDE = 80;
    half *query = sm, *kv = query + 32 * STRIDE, *prob = kv + 64 * STRIDE;
    float *scores = reinterpret_cast<float *>(prob + 32 * PSTRIDE);
    float *maxima = scores + 32 * 64, *sums = maxima + 32, *alpha = sums + 32;
    int warp = threadIdx.x / 32, lane = threadIdx.x % 32;
    for (int task = blockIdx.x; task < p.batch * p.heads * (p.seq / 32); task += gridDim.x) {
        int bh = task / (p.seq / 32), first = (task % (p.seq / 32)) * 32;
        int batch = bh / p.heads, head = bh % p.heads;
        if (first >= p.lengths[batch])
            continue;
        int token = p.block_offsets[batch * (p.seq / 32) + first / 32];
        // A complete shared causal path has identical attention in every copy.
        if (p.batch_ids[token] != batch)
            continue;
        int kh = batch * p.kv_heads + head / (p.heads / p.kv_heads);
        for (int i = threadIdx.x; i < 32 * D / 8; i += blockDim.x) {
            int row = i / (D / 8), col = (i % (D / 8)) * 8;
            *reinterpret_cast<uint4 *>(query + row * STRIDE + col) =
                *reinterpret_cast<const uint4 *>(p.q + (bh * p.seq + first + row) * D + col);
        }
        if (threadIdx.x < 32) {
            maxima[threadIdx.x] = -INFINITY;
            sums[threadIdx.x] = 0.f;
        }
        float output[4][4] = {};
        int qr = (warp / 4) * 16, qc = (warp % 4) * 16, oc = (warp % 4) * 32;
        __syncthreads();
        int end = min(p.lengths[batch], first + 32);
        for (int start = 0; start < end; start += 64) {
            for (int i = threadIdx.x; i < 64 * D / 8; i += blockDim.x) {
                int row = i / (D / 8), col = (i % (D / 8)) * 8;
                *reinterpret_cast<uint4 *>(kv + row * STRIDE + col) = start + row < p.lengths[batch]
                    ? *reinterpret_cast<const uint4 *>(p.k + (kh * p.seq + start + row) * D + col)
                    : make_uint4(0, 0, 0, 0);
            }
            __syncthreads();
            float score[2][4] = {};
#pragma unroll
            for (int k = 0; k < D; k += 16) {
                uint32_t a[4], b[2];
                load_a(a, query + qr * STRIDE + k, STRIDE);
#pragma unroll
                for (int n = 0; n < 2; ++n) {
                    load_b<false>(b, kv + (qc + n * 8) * STRIDE + k, STRIDE);
                    mma16816(score[n], a, b);
                }
            }
#pragma unroll
            for (int n = 0; n < 2; ++n) {
                int row = qr + lane / 4, col = qc + n * 8 + (lane % 4) * 2;
                *reinterpret_cast<float2 *>(scores + row * 64 + col) = make_float2(score[n][0], score[n][1]);
                *reinterpret_cast<float2 *>(scores + (row + 8) * 64 + col) = make_float2(score[n][2], score[n][3]);
            }
            __syncthreads();
            int row = threadIdx.x / 8, col = threadIdx.x % 8;
            float values[8], maximum = -INFINITY;
#pragma unroll
            for (int n = 0; n < 8; ++n) {
                int key = start + col + n * 8;
                values[n] = key <= first + row && key < p.lengths[batch]
                    ? scores[row * 64 + col + n * 8] * .08838834764831845f : -INFINITY;
                maximum = fmaxf(maximum, values[n]);
            }
#pragma unroll
            for (int shift = 4; shift; shift /= 2)
                maximum = fmaxf(maximum, __shfl_xor_sync(0xffffffff, maximum, shift, 8));
            maximum = fmaxf(maximum, maxima[row]);
            float correction = expf(maxima[row] - maximum), sum = 0.f;
#pragma unroll
            for (int n = 0; n < 8; ++n) {
                float value = expf(values[n] - maximum);
                sum += value;
                prob[row * PSTRIDE + col + n * 8] = __float2half(value);
            }
#pragma unroll
            for (int shift = 4; shift; shift /= 2)
                sum += __shfl_xor_sync(0xffffffff, sum, shift, 8);
            if (col == 0) {
                maxima[row] = maximum;
                sums[row] = sums[row] * correction + sum;
                alpha[row] = correction;
            }
            // All QK reads finished before K's shared tile is reused for V.
            for (int i = threadIdx.x; i < 64 * D / 8; i += blockDim.x) {
                int row = i / (D / 8), col = (i % (D / 8)) * 8;
                *reinterpret_cast<uint4 *>(kv + row * STRIDE + col) = start + row < p.lengths[batch]
                    ? *reinterpret_cast<const uint4 *>(p.vt + (kh * p.seq + start + row) * D + col)
                    : make_uint4(0, 0, 0, 0);
            }
            __syncthreads();
#pragma unroll
            for (int n = 0; n < 4; ++n) {
                output[n][0] *= alpha[qr + lane / 4];
                output[n][1] *= alpha[qr + lane / 4];
                output[n][2] *= alpha[qr + lane / 4 + 8];
                output[n][3] *= alpha[qr + lane / 4 + 8];
            }
#pragma unroll
            for (int k = 0; k < 64; k += 16) {
                uint32_t a[4], b[2];
                load_a(a, prob + qr * PSTRIDE + k, PSTRIDE);
#pragma unroll
                for (int n = 0; n < 4; ++n) {
                    load_b<true>(b, kv + k * STRIDE + oc + n * 8, STRIDE);
                    mma16816(output[n], a, b);
                }
            }
            __syncthreads();
        }
#pragma unroll
        for (int n = 0; n < 4; ++n) {
#pragma unroll
            for (int r = 0; r < 2; ++r) {
                int row = qr + lane / 4 + r * 8, col = head * D + oc + n * 8 + (lane % 4) * 2;
                float inv = first + row < p.lengths[batch] ? 1.f / sums[row] : 0.f;
                *reinterpret_cast<uint32_t *>(p.proj + (token + row) * p.heads * D + col) =
                    pack_half2(output[n][r * 2] * inv, output[n][r * 2 + 1] * inv);
            }
        }
        __syncthreads();
    }
}
