# Fifty-site chunking audit

The curation portal serves the mobile-friendly, read-only dashboard at
`/chunking-audit`, linked from **More**. It inherits the portal's LAN-only
peer/Host/origin restrictions. Opening, filtering or refreshing it does not query
the archive, run inference, start Jobs or change approval/discovery state.

The report covers the fifty site/platform groups in the 9 November 2026 census,
ranked by indexed `text/html` captures under `websites/`. Subdomains, `www` and
default-port aliases are grouped; all Ezboard shards are one platform. Different
capture dates count separately in the census; images and pending imports do not.

## Evidence and decisions

The operator collected 837 distinct URL-path samples, using deterministic random,
earliest/latest and largest indexed-passage strata, including separate major
subdomain strata. Duplicate dated versions of the same host/path were not allowed
to dominate the sample. Collection used bounded serial metadata queries and
four-record source batches, excluding vectors. It did not traverse or clone the
archive. This is a purposive sample, not a statistical estimate of corpus quality.

Each available source was read by exact path from the existing prepared checkout.
Nine newer sources absent there were recovered from local publication Git objects
and verified against their indexed hashes, using the existing remote-free reader.
The same source and unchanged archival reader input were independently planned
with baseline `eq-passages-480-v11` and candidate `eq-passages-480-v12` and the
pinned Gemma tokenizer. **Before** means the v11 projection, not necessarily the
older policy currently stored in Elasticsearch. No embedding API or Luna calls
were made. Every plan checks complete selected-view offsets and the 480-token
request budget, including title and context. Structural tests verify retained
messages, dates, entity values, quotes, code, tables and attachments.

Full sources, ES samples and generated plans stay in the operator's private audit
directory, outside both repositories. The checked-in receipt contains counts,
decisions, hashes, reconstructed Wayback links and short comparison excerpts;
never copy complete captures, paid judgments, vectors, credentials or local paths
into it. A changed plan is not automatically a relevance improvement: the
per-site finding explains the observed correction. Lower chunk counts are not a
quality score. Some valid entity boundaries intentionally increase chunk counts.

An unplanned oversized source stays visible in that site's limitations. Missing
content in an original capture cannot be recovered by inventing data. Unknown or
damaged layouts retain content. Not every site needs a dedicated hostname rule;
shared forum engines and conservative generic policies are explicit decisions.

## Rebuild the dashboard receipt

Keep the manually reviewed `finding` and `policies` fields in
`elastic-indexer-stack/curation/chunking-audit.json`. Given the private per-site
before/after plan files, run:

```bash
python3 scripts/chunk-audit/build-report.py \
  --before /private/audit/before --after /private/audit/after \
  --report elastic-indexer-stack/curation/chunking-audit.json
python3 -m unittest discover -s scripts/chunk-audit -v
```

The builder refuses changed source identities, incomplete view coverage, excessive
tokens or fewer than four validated samples per site. It exports bounded excerpts
and source-linked receipts, never full plans. Curation tests check all fifty rows
and the LAN-only routes; Chromium checks filtering, mobile layouts, both themes,
escaping, touch targets and load-error recovery in the normal CI smoke test.

New imports use the deployed policy. Older records move through ordinary bounded
maintenance after the active broad writer finishes. Deploying this dashboard or
policy must leave existing Jobs, prepared sources and checkpoints untouched.
