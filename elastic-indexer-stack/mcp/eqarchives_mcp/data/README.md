# English frequency reference

`english-frequency.json` is a filtered, reformatted derivative of **wordfreq
3.1.1**, copyright 2022 **Robyn Speer**. This data file remains licensed under
[Creative Commons Attribution-ShareAlike 4.0](https://creativecommons.org/licenses/by-sa/4.0/).
It is separate from the repository's AGPL application code. Preserve the
accompanying [upstream attribution notice](wordfreq-NOTICE.txt), including credit
to Marc Brysbaert et al. for SUBTLEX, which is freely available data.

Upstream: <https://github.com/rspeer/wordfreq/tree/355ef19cfa40e8a691966c5224ee50b4f096f8fe>.
The reference combines general English usage through approximately 2021; it is
not an EverQuest vocabulary or a historical language model. Explore uses it only
to rank literal source terms. It neither invents topics nor changes page counts.

Only `small_en.msgpack.gz` from the `wordfreq==3.1.1` distribution is used:

- Source SHA-256: `f94a80cba6a3857b260d0666b5432bb7ea9b85315574dee9c306e87f61298247`.
- Derived SHA-256: `a82d7ded92fcfbff031b3340a1eec56fb12a253ff62f72d183a715ac7baa1253`.
- 27,957 lowercase tokens, lengths 3–24, matching Explore's token alphabet.
- Values are Zipf frequencies (log10 occurrences per billion), grouped into
  hundredth-wide bins. The source floor is 3.0, also used for absent tokens.

The application loads this 232 KB JSON once using the standard library. There
are no new runtime dependencies, remote lookups or language-model calls.

To reproduce in a disposable Python environment with `wordfreq==3.1.1` installed:

```python
import gzip, hashlib, json, re
from pathlib import Path
import msgpack, wordfreq

source = Path(wordfreq.__file__).parent / 'data' / 'small_en.msgpack.gz'
assert hashlib.sha256(source.read_bytes()).hexdigest() == 'f94a80cba6a3857b260d0666b5432bb7ea9b85315574dee9c306e87f61298247'
data = msgpack.unpackb(gzip.decompress(source.read_bytes()), raw=False)
assert data[0] == {'format': 'cB', 'version': 1}
bins = {
    f'{9 + (1-i)/100:.2f}': ' '.join(sorted(
        w for w in words
        if re.fullmatch(r"[a-z]+(?:['’-][a-z]+)*", w) and 3 <= len(w) <= 24
    )) for i, words in enumerate(data[1:], 1)
}
result = json.dumps({k: v for k, v in bins.items() if v}, ensure_ascii=False, indent=2) + '\n'
assert hashlib.sha256(result.encode()).hexdigest() == 'a82d7ded92fcfbff031b3340a1eec56fb12a253ff62f72d183a715ac7baa1253'
Path('english-frequency.json').write_text(result, encoding='utf-8')
```
