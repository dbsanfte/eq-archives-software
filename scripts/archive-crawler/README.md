# Website candidate crawler

The [production intranet service](../../elastic-indexer-stack/curation/README.md)
adds persistent review, explicit scopes, bounded site capture, a second batch
publication approval and targeted indexing through standard CI/CD.

This operator tool spiders outward from selected local website captures, checks
archive coverage, stages historical Wayback samples, grades complete extracted
sources with Luna, and produces an offline approval queue. Commands need Python
3.10+ and Ruby 3.0+, with no Python packages or Ruby gems.

The standalone CLI pilot ends at a manifest of explicitly approved page samples. It
does not download an entire site, publish archive Git changes, or write index
documents. Subsequent operator work can expand approved site scopes, publish one
archive commit/push and index just the changed files. Surviving live sites can
appear as candidates; this pilot acquires historical captures only. Live capture
needs separate provenance and URL mapping before ingestion.

## Ezboard boards

The [Ezboard guide](EZBOARD.md) documents the archive research, custom
`ezboard_capture.py` operator command, whole-board portal scope, server moves,
resumable CDX pagination and exact archive filenames. Ezboard discovery creates
board candidates; forum and message links must resolve to a verified parent.

## Bounded pilot

Keep state outside both repositories. Use an existing archive checkout whose
Git trees and source blobs are already available locally. Missing objects remain
explicit; discovery never fetches them automatically.

```bash
CRAWL_WORK=/var/tmp/eqarchives-crawl/pilot
python3 scripts/archive-crawler/crawler.py --work-dir "$CRAWL_WORK" discover \
  --archive-repo /path/to/existing/eq-archives
python3 scripts/archive-crawler/crawler.py --work-dir "$CRAWL_WORK" sample
# Explicit paid opt-in; the private file contains one API key.
python3 scripts/archive-crawler/crawler.py --work-dir "$CRAWL_WORK" grade \
  --api-key-file /private/path/openai-key --max-usd 2
python3 scripts/archive-crawler/crawler.py --work-dir "$CRAWL_WORK" review
```

Open `$CRAWL_WORK/review.html`. It contains referring-link evidence, coverage
uncertainty, source quotes, complete extracted text and links to Wayback captures.
The default view shows grades 2–3; **All candidates** also shows low grades and
unresolved acquisition/grading. Grades are model judgments about supplied
captures. User approval is separate.

| Resource | Default limit |
| --- | --- |
| Candidate shortlist | 50; frozen when acquisition starts |
| Seed reads / local blob probes | 66 reads, 6 per host; 660 probes per discovery command |
| Seed bytes | 16 MiB total; 512 KiB per page |
| Cached tree inventory | 15,000 entries per host; 200,000 overall |
| Wayback samples | At most 2 exact-page captures per candidate |
| Wayback requests | 240 cumulatively, including CDX, redirects and retries |
| Wayback bytes / time | 1 MiB per response; 24 MiB and 900 seconds cumulatively |
| Request spacing / transfer rate | At least 3 seconds / at most 128 KiB per second |
| Complete extracted source for Luna | 120,000 characters per candidate; larger sources stay unjudged |
| Luna spend | Explicit `--max-usd`; durable reservations precede requests |

Inclusive UTC ranges are **1999-01-01 through 2001-12-31** first, then
**2002-01-01 through 2006-12-31** if no exact tier-1 HTML capture is listed. The
tool samples the earliest and latest of at most 80 digest-collapsed CDX rows.
Limited listings are labelled and are not complete capture histories. Network
failures do not establish absence. Replay redirects must retain the exact
original URL and stay inside the selected tier. Date provenance records Memento
headers when available, otherwise the replay URL.

These tiers select **discovery evidence**, not the approved capture date range.
The production portal captures all available dated versions from 1999-01-01
through 2006-12-31 within the approved scope, using paginated, uncollapsed CDX
listings and explicit cumulative bounds. Earlier saved evidence and decisions
remain valid. See the [curation guide](../../elastic-indexer-stack/curation/README.md).

Re-running resumes cached work. Discovery refreshes the shortlist before
acquisition, then preserves it. Sampling skips completed candidates and earlier
failures/unavailability; `sample --retry-unresolved` explicitly retries unresolved
candidates within the same cumulative budgets. A staged first sample survives
a failed second sample and remains usable for review; that second sample is not
automatically retried. Grading skips unchanged valid judgments and retains
reservations after ambiguous paid failures. Increasing a limit is an explicit
operator choice. Use a new work directory for a new pilot.

## Review and approved batch

Select **Approve staged captures**, **Defer**, or **Reject**, then download the
decision file. Browser selections alone do not change stored approval state.

```bash
python3 scripts/archive-crawler/crawler.py --work-dir "$CRAWL_WORK" decide \
  --file /path/to/review-decisions.json
python3 scripts/archive-crawler/crawler.py --work-dir "$CRAWL_WORK" batch
```

Every decision is validated before any are saved. Decisions bind to a manifest
hash of the exact URL, site scope, captures and judgment. Source hashes and
judgment signatures are checked again before approval and export. Changed
evidence requires grading/review again. `approved-batch.json` contains only
explicit approvals and marks publication pending. It never triggers Git or
indexing. An empty approval set exports an empty batch. Preserve raw HTML,
original URLs, hashes, capture dates and provenance in a subsequent publisher.

## Archive I/O and identity

Discovery pins archive HEAD and caches the website root/selected host trees in
private SQLite state. A separate remote-free Git reader accesses the existing
object database without loading archive Git configuration. This prevents
implicit promisor fetching even on older Git. No clone, pull, checkout, status,
working-tree walk, archive commit or push is part of the tool. The inventory cap
persists with the work directory, including older cached tree versions.

Website/account novelty is checked separately from that capped page inventory.
Existing ordinary hosts are excluded before sampling/grading, including `www`
and default-port aliases. Shared hosts use targeted account-tree probes. Missing
or budget-limited metadata never establishes absence and is excluded from new
discovery runs. The production review queue blocks these unverified entries and
provides a bounded resumable recheck. These checks read Git trees only, without
fetching missing objects, reading page blobs or walking the checkout.

Legacy filenames lose protocol and some escaping information. Coverage is a
dated legacy-path check: missing host/page, tier-2-only page, tier-1 match,
partial inventory or uncertain legacy identity. HTTPS/encoded URLs remain
uncertain. Coverage cannot establish index completeness. Outgoing URLs retain
protocol, path case, escapes and query order; fragments and explicit default
ports are normalized. Captures retain the original spelling returned by CDX.
Known shared hosts use account paths as proposed review scopes, including
GeoCities neighborhood numbers. A proposed scope does not establish whole-site
relevance.

## Downloader and grading provenance

The MIT-licensed public [Wayback Machine Downloader](https://github.com/hartator/wayback-machine-downloader)
is vendored unmodified at `653b94ba128cd209d0d4b345e4bff6e714e833fc`
(version 2.3.1, checked 2026-10-06). The vendor `UPSTREAM.json` records hashes.
`downloader.rb` subclasses its timestamp-preserving list/download interfaces,
adds one serial persistent HTTP client, pacing, transfer limits and bounded
422/429/5xx retries, and avoids URL-unescaping collisions and swallowed download
failures. A server-closed connection can reconnect within limits; only one is
active at a time. Wayback is the sole production destination.

Upstream [PR #280](https://github.com/hartator/wayback-machine-downloader/pull/280)
proposes persistent Net::HTTP connections for rate limiting;
[issue #275](https://github.com/hartator/wayback-machine-downloader/issues/275)
also discusses spacing. These reports informed the adapter and do not establish
that every HTTP 422 has the same cause. The
[CDX guide](https://github.com/internetarchive/wayback/tree/master/wayback-cdx-server)
documents inclusive date filters, digest collapsing, limits and resume markers.

Paid grading uses Responses, `gpt-6-luna`, low reasoning, strict structured output
and `store: false`. Captured instructions are untrusted data. Grades value
substantive EQ content, including small guilds and personal sites; supporting
quotes must occur verbatim in complete extracted sources. Incomplete or invalid
responses leave candidates unjudged. Reservation pricing is $0.10 input / $0.50
output per million tokens, verified against the
[official model page](https://developers.openai.com/api/docs/models/gpt-6-luna)
on 2026-10-06. Verify it before later paid runs. Reservations use input UTF-8
bytes plus overhead and the full output-token cap. Ambiguous failures retain
their full reservation; usage-based cost totals are estimates, not account bills.

Work directories are private, exports are mode 0600, and API errors omit upstream
details. Keep keys, state, captured content and decisions out of Git. The offline
review page renders source as text without loading captured scripts, images or
third-party assets.

## Verification

```bash
ruby -c scripts/archive-crawler/downloader.rb
python3 -m unittest discover -s scripts/archive-crawler -v
# Use scripts/browser-test-requirements.txt and its installed Chromium:
/path/to/browser-venv/bin/python scripts/archive-crawler/verify_review_browser.py
```

Fixtures exercise real persistent HTTP/1.1 requests, 422 backoff, pacing,
transfer limits, date/URL rejection, pinned vendor hashes, cached discovery,
missing promisor objects without fetching, paid-call caching/budgets, complete
source checks, stale approvals, atomic decisions, and mobile review/export.
Tests never call Wayback, OpenAI, ingestion or production services.
