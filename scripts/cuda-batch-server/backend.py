"""Pinned GGUF weights, FP32 residuals, FP16 Tensor Cores, full-layer megakernels."""

import ctypes as C
from dataclasses import dataclass
import hashlib
from itertools import chain
import math
from pathlib import Path

EMBED_MODEL = "embeddinggemma-2-768-v1"
RERANK_MODEL = "qwen3-reranker-0.6b-q8-v1"
PINS = {
    "embed": (
        "embeddinggemma-2-Q8_0.gguf",
        "2188ac1deca4b77dffefd603c2776a9d76d9d74ec01841392982ebb840b09135",
        "gemma-tokenizer.json",
        "4d777ef5bdc1aa36227abdfb77c3e49e7b9c892d16e1b6bda41c393504828be4",
    ),
    "rerank": (
        "qwen3-reranker-0.6b-q8_0.gguf",
        "afa78a25c135ea248a103cf6b681ba4bd627a0e0b6d4b1e4322b55950246908f",
        "qwen-tokenizer.json",
        "aeb13307a71acd8fe81861d94ad54ab689df773318809eed3cbe794b4492dae4",
    ),
}
PREFIX = (
    "<|im_start|>system\nJudge whether the Document meets the requirements based on the Query and the Instruct provided. "
    'Note that the answer can only be "yes" or "no".<|im_end|>\n<|im_start|>user\n'
    "<Instruct>: Given a web search query, retrieve relevant passages that answer the query\n<Query>: "
)
SUFFIX = "<|im_end|>\n<|im_start|>assistant\n<think>\n\n</think>\n\n"


def verified(path, expected):
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while data := stream.read(1024 * 1024):
            digest.update(data)
    if digest.hexdigest() != expected:
        raise ValueError("Pinned model/tokenizer checksum differs")
    return path


POINTERS = "qkv out gateup down ple_gate ple_out in_norm q_norm k_norm attn_norm ffn_norm ffn_post ple_norm scalar ple x norm proj q k vt scores attn hidden result qkv_buffer prob lengths block_offsets batch_ids positions".split()
INTEGERS = "batch seq width heads kv_heads dim ffn gemma tokens".split()


@dataclass
class BatchLayout:
    padded: list
    block_offsets: list
    ids: list
    batch_ids: list
    positions: list
    last_indices: list
    owners: list


def packed_layout(rows, share_prefix=False):
    """Share only complete causal paths, preserving each query group's prefix.

    Keys include the parent block and the unpadded token tuple. Equal suffixes
    after different contexts cannot alias, nor can padding alias real tokens.
    All state is local to this forward pass. Bidirectional models disable sharing.
    """
    padded = [math.ceil(len(row) / 32) * 32 for row in rows]
    layout = BatchLayout(padded, [[0] * (max(padded) // 32) for _ in rows], [], [], [], [], [])
    blocks = {}
    for batch, row in enumerate(rows):
        parent = -1
        for position in range(0, len(row), 32):
            values = row[position : position + 32]
            key = (parent, tuple(values))
            offset = blocks.get(key) if share_prefix else None
            if offset is None:
                offset = len(layout.ids)
                layout.ids.extend(values + [0] * (32 - len(values)))
                layout.batch_ids.extend([batch] * 32)
                layout.positions.extend(range(position, position + 32))
                layout.owners.append((batch, position))
                if share_prefix:
                    blocks[key] = offset
            layout.block_offsets[batch][position // 32] = offset
            parent = offset
        last = len(row) - 1
        layout.last_indices.append(layout.block_offsets[batch][last // 32] + last % 32)
    return layout


class Layer(C.Structure):
    _fields_ = (
        [(name, C.c_void_p) for name in POINTERS]
        + [(name, C.c_int) for name in INTEGERS]
        + [("theta", C.c_float)]
    )


def load_library(path):
    lib = C.CDLL(str(path))
    if not hasattr(lib, "layer_abi_version") or lib.layer_abi_version() != 3:
        raise RuntimeError("CUDA layer ABI differs; rebuild all selected profiles")
    lib.launch_layer.argtypes = [C.POINTER(Layer), C.c_void_p, C.POINTER(C.c_int), C.c_int]
    lib.launch_layer.restype = C.c_int
    lib.error_string.argtypes = [C.c_int]
    lib.error_string.restype = C.c_char_p
    return lib


class Model:
    def __init__(self, kind, root, library=None):
        import numpy as np
        import torch
        from gguf import GGUFReader
        from gguf.quants import dequantize
        from tokenizers import Tokenizer

        self.torch, self.kind = torch, kind
        self.gemma = kind == "embed"
        self.lib = load_library(library or f"/app/lib{kind}.so")
        self.small = load_library("/app/libsmall.so") if library is None else None
        filename, digest, tokenizer, tok_digest = PINS[kind]
        reader = GGUFReader(str(verified(Path(root) / filename, digest)))
        self.tokenizer = Tokenizer.from_file(str(verified(Path(root) / tokenizer, tok_digest)))
        self.tokenizer.no_padding()
        self.tokenizer.no_truncation()
        self.weights = {}
        for tensor in reader.tensors:
            values = dequantize(tensor.data, tensor.tensor_type).reshape(tuple(reversed(tensor.shape)))
            # Unquantized normalization/scalar parameters remain FP32.
            dtype = torch.float32 if len(tensor.shape) == 1 else torch.float16
            self.weights[tensor.name] = torch.from_numpy(np.array(values, copy=True)).to(
                device="cuda", dtype=dtype
            )
        self.width, self.heads, self.ffn = (512, 4, 2048) if self.gemma else (1024, 16, 3072)
        self.count = 24 if self.gemma else 28
        self.layers = []
        for i in range(self.count):
            get = lambda suffix: self.weights.get(f"blk.{i}.{suffix}.weight")
            row = {
                "qkv": torch.cat([get("attn_q"), get("attn_k"), get("attn_v")]),
                "gateup": torch.cat([get("ffn_gate"), get("ffn_up")]),
                "out": get("attn_output"),
                "down": get("ffn_down"),
                "ple_gate": get("inp_gate"),
                "ple_out": get("proj"),
                "in_norm": get("attn_norm"),
                "q_norm": get("attn_q_norm"),
                "k_norm": get("attn_k_norm"),
                "attn_norm": get("post_attention_norm"),
                "ffn_norm": get("ffn_norm"),
                "ffn_post": get("post_ffw_norm"),
                "ple_norm": get("post_norm"),
                "scalar": get("layer_output_scale"),
            }
            self.layers.append(row)
            for name in ("attn_q", "attn_k", "attn_v", "ffn_gate", "ffn_up"):
                del self.weights[f"blk.{i}.{name}.weight"]

        self.blocks = 0

    def encode(self, inputs):
        result = self.tokenizer.encode_batch(inputs, add_special_tokens=self.gemma)
        bound = 512 if self.gemma else 768
        if any(not row.ids or len(row.ids) > bound for row in result):
            raise ValueError(f"Each input must contain 1–{bound} tokens; inputs are never truncated")
        return [row.ids for row in result]

    def norm(self, x, weight=None):
        x = x.float()
        y = x * self.torch.rsqrt(x.square().mean(-1, keepdim=True) + 1e-6)
        return y if weight is None else y * weight

    def reference_layer(self, x, row, lengths, dim, kv, theta, ple):
        """Independent PyTorch oracle for every stage; not the serving backend."""
        t, F = self.torch, self.torch.nn.functional
        b, s, _ = x.shape
        h, w = self.heads, self.width

        def linear(a, weight):
            return t.mm(a.reshape(-1, a.shape[-1]).half(), weight.T, out_dtype=t.float32).reshape(
                *a.shape[:-1], weight.shape[0]
            )

        q, k, v = linear(self.norm(x, row["in_norm"]), row["qkv"]).split([h * dim, kv * dim, kv * dim], -1)
        q, k, v = [a.reshape(b, s, -1, dim).transpose(1, 2) for a in (q, k, v)]
        q, k = self.norm(q, row["q_norm"]), self.norm(k, row["k_norm"])
        if self.gemma:
            v = self.norm(v)
        angles = t.arange(s, device="cuda").float()[:, None] * theta ** (
            -t.arange(0, dim, 2, device="cuda").float() / dim
        )
        angles = t.cat([angles, angles], -1)

        def rope(a):
            return a * angles.cos() + t.cat([-a[..., dim // 2 :], a[..., : dim // 2]], -1) * angles.sin()

        q, k = rope(q).half(), rope(k).half()
        k, v = k.repeat_interleave(h // kv, 1), v.half().repeat_interleave(h // kv, 1)
        mask = t.arange(s, device="cuda")[None, :] < lengths[:, None]
        mask = mask[:, None, None, :]
        if not self.gemma:
            mask = mask & t.ones((s, s), dtype=t.bool, device="cuda").tril()
        # FP16 Tensor Core operands with FP32 accumulators and logits. The
        # independent bmm/softmax path checks the custom attention implementation.
        logits = t.bmm(q.flatten(0, 1), k.transpose(-1, -2).flatten(0, 1), out_dtype=t.float32).reshape(
            b, h, s, s
        ) * (1 if self.gemma else dim**-0.5)
        prob = logits.masked_fill(~mask, -t.inf).softmax(-1).half()
        attn = (prob @ v).transpose(1, 2).reshape(b, s, h * dim)
        out = linear(attn, row["out"])
        x = x + (self.norm(out, row["attn_norm"]) if self.gemma else out)
        gu = linear(self.norm(x, row["ffn_norm"]), row["gateup"])
        gate, up = gu.chunk(2, -1)
        act = F.gelu(gate, approximate="tanh") if self.gemma else F.silu(gate)
        out = linear(act * up, row["down"])
        x = x + (self.norm(out, row["ffn_post"]) if self.gemma else out)
        if self.gemma:
            out = linear(F.gelu(linear(x, row["ple_gate"]), approximate="tanh") * ple, row["ple_out"])
            x = (x + self.norm(out, row["ple_norm"])) * row["scalar"]
        return x

    def run(self, tokens, reference=False, compare_layers=False, block_limit=None, share_prefix=True):
        t, F = self.torch, self.torch.nn.functional
        b, s = len(tokens), math.ceil(max(map(len, tokens)) / 32) * 32
        if not 1 <= b <= 32 or not 32 <= s <= (512 if self.gemma else 768):
            raise ValueError("GPU batch shape exceeds its fixed bounds")
        # Qwen's short-input WMMA path rounds differently from its CUTLASS path.
        # Keep causal scoring on the same arithmetic when other requests join
        # the batch; a queue timing change must not change a document's score.
        lib = self.small if self.gemma and self.small is not None and b * s <= 128 else self.lib
        layout = packed_layout(tokens, share_prefix=not self.gemma and share_prefix)
        total = len(layout.ids)
        # Transfer bounded CPU-known metadata together. Separate tiny CUDA casts,
        # repeat_interleave and additions cost more than this packing for queries.
        parts = [
            layout.ids,
            list(map(len, tokens)),
            list(chain.from_iterable(layout.block_offsets)),
            layout.batch_ids,
            layout.positions,
            layout.last_indices,
        ]
        metadata = t.tensor(list(chain.from_iterable(parts)), device="cuda", dtype=t.int32)
        ids, lengths, block_offsets, batch_ids, positions, last_indices = metadata.split(
            list(map(len, parts))
        )
        x = F.embedding(ids, self.weights["token_embd.weight"]).float()
        ple = None
        if self.gemma:
            x *= math.sqrt(self.width)
            projection = t.mm(
                x.reshape(-1, self.width).half(),
                self.weights["per_layer_model_proj.weight"].T,
                out_dtype=t.float32,
            ) / math.sqrt(self.width)
            ple = F.rms_norm(
                projection.reshape(total, self.count, self.width),
                (self.width,),
                self.weights["per_layer_proj_norm.weight"],
                1e-6,
            )
            ple = ple.permute(1, 0, 2).contiguous().half()
        maxdim = 512 if self.gemma else 128
        H = self.heads * maxdim
        alloc = lambda n, dtype=t.float16: t.empty(n, dtype=dtype, device="cuda")
        # Every workspace has a fixed upper shape, reused by all layers. No cache
        # grows with request history and no source text survives the batch.
        work = dict(
            x=x,
            lengths=lengths,
            block_offsets=block_offsets,
            batch_ids=batch_ids,
            positions=positions,
            norm=alloc(total * self.width),
            proj=alloc(total * max(H, self.ffn)),
            q=alloc(b * s * H),
            k=alloc(b * s * H),
            vt=alloc(b * s * H),
            scores=alloc(b * self.heads * s * s, t.float32),
            attn=alloc(b * s * H),
            hidden=alloc(total * 2 * self.ffn, t.float32),
            result=alloc(total * self.width, t.float32),
            qkv_buffer=alloc(total * 3 * H, t.float32),
            prob=alloc(b * self.heads * s * s),
        )
        comparisons = []

        def sequence(values, batch, length):
            return t.cat(
                [
                    values[
                        layout.block_offsets[batch][pos // 32] : layout.block_offsets[batch][pos // 32]
                        + min(32, length - pos)
                    ]
                    for pos in range(0, length, 32)
                ]
            )

        for i, row in enumerate(self.layers):
            dim = (512 if i % 6 == 5 else 256) if self.gemma else 128
            kv = (1 if i % 6 == 5 else 2) if self.gemma else 8
            theta = 10000.0 if self.gemma and i % 6 != 5 else 1000000.0
            layer_ple = ple[i] if ple is not None else None
            if reference or compare_layers:

                expected_rows = [
                    self.reference_layer(
                        sequence(x, j, n).clone().unsqueeze(0),
                        row,
                        lengths[j : j + 1],
                        dim,
                        kv,
                        theta,
                        sequence(layer_ple, j, n).unsqueeze(0) if layer_ple is not None else None,
                    ).squeeze(0)
                    for j, n in enumerate(layout.padded)
                ]
                expected = t.cat([expected_rows[owner][pos : pos + 32] for owner, pos in layout.owners])
            if reference:
                x.copy_(expected)
            else:
                values = dict(row, **work, ple=layer_ple)
                args = Layer(
                    **{
                        name: values[name].data_ptr() if values.get(name) is not None else None
                        for name in POINTERS
                    },
                    batch=b,
                    seq=s,
                    width=self.width,
                    heads=self.heads,
                    kv_heads=kv,
                    dim=dim,
                    ffn=self.ffn,
                    gemma=int(self.gemma),
                    tokens=total,
                    theta=theta,
                )
                blocks = C.c_int()
                status = lib.launch_layer(
                    C.byref(args),
                    t.cuda.current_stream().cuda_stream,
                    C.byref(blocks),
                    block_limit() if block_limit else 0,
                )
                if status:
                    raise RuntimeError("CUDA layer launch failed: " + lib.error_string(status).decode())
                self.blocks = blocks.value
                if compare_layers:
                    # Padding carries no model semantics and may be skipped by
                    # attention. Compare every real token, never padded rows.
                    actual_valid = t.cat([sequence(x, j, len(row)) for j, row in enumerate(tokens)])
                    expected_valid = t.cat([sequence(expected, j, len(row)) for j, row in enumerate(tokens)])
                    delta = actual_valid - expected_valid
                    comparisons.append(
                        {
                            "layer": i,
                            "max_abs": delta.abs().max().item(),
                            "relative_l2": (delta.norm() / expected_valid.norm()).item(),
                        }
                    )
        output = F.rms_norm(x, (self.width,), self.weights["output_norm.weight"], 1e-6)
        if self.gemma:
            # Bidirectional Gemma has contiguous, unshared sequence blocks.
            pooled = t.stack(
                [
                    output[offsets[0] : offsets[0] + len(row)].mean(0)
                    for offsets, row in zip(layout.block_offsets, tokens)
                ]
            )
            output = F.normalize(
                t.mm(pooled.half(), self.weights["output.weight"].T, out_dtype=t.float32), dim=-1
            )
        else:
            output = output.index_select(0, last_indices)
            output = t.mm(output.half(), self.weights["cls.output.weight"].T, out_dtype=t.float32).softmax(
                -1
            )[:, 0]
        output = output.cpu()
        if not t.isfinite(output).all().item():
            raise RuntimeError("Nonfinite inference output")
        result = output.tolist()
        return (result, comparisons) if compare_layers else result
