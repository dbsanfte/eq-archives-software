"""Build-time pinned local tokenizer; the serving process never downloads models."""
import hashlib
from pathlib import Path
import urllib.request

url = "https://huggingface.co/Qwen/Qwen3-Reranker-0.6B/resolve/e61197ed45024b0ed8a2d74b80b4d909f1255473/tokenizer.json"
with urllib.request.urlopen(url, timeout=60) as response:
    raw = response.read(16 * 1024 * 1024 + 1)
assert hashlib.sha256(raw).hexdigest() == "aeb13307a71acd8fe81861d94ad54ab689df773318809eed3cbe794b4492dae4", "Tokenizer checksum differs"
Path("/app/qwen-tokenizer.json").write_bytes(raw)
