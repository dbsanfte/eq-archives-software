# SitePowerUp capture

SitePowerUp boards use `/mb/view.asp` and query parameters rather than separate
directories. Capture one numeric **BoardID**, never the entire `/mb/` directory.
The portal groups discovery and manual submissions by this identity; source URLs
and dated documents remain exact and separate.

## Observed format

The existing January 9, 2000 capture of
`http://www.sitepowerup.com:80/mb/view.asp?Action=Display&BoardID=102010`
is **Words of Enchantment**, an EQ Enchanter board. Its nested message list has
203 `Action=Reply` links, message titles, authors and dates. It links to another
board (104254) before its own navigation, so the first board link cannot determine
ownership. The submitted message `Reply=12155` belongs to BoardID 102010; a bounded
exact Wayback lookup found no 1999–2006 replay for that message. An index link is
evidence of a message URL, not proof that its body was captured.

A saved August 27, 1999 message from board 104905 confirms that `Action=Reply`
displays the original message **and** a reply form. Hidden `BoardID` and `Reply`
fields identify the message, and a Return link leads to `Action=Display`.
`Action=Post`, deletion/admin views, login endpoints and form submissions are
outside this capture. No form is submitted or original live site contacted.

The archive also contains older downloader filenames such as
`www.sitepowerup.com/19990830011719/mb/view.asp_BoardID=102010` and
`www.sitepowerup.com/19990827033718/mb/view.asp_Action=Reply_and_BoardID=104905_and_Reply=2`.
These establish board coverage using Git tree metadata. Their lossy filename
substitutions are never used to reconstruct an exact original URL. Board 102010
is already archived, so the portal retires its message suggestions as duplicates
instead of grading or approving another new-site capture.

## Acquisition and source identity

The engine handles the observed `Action=Display`, omitted-Action board indexes,
and `Action=Reply&Reply=<number>` messages, including pagination parameters.
Names/action values are compared without case and parameter order does not change
board identity. Ambiguous duplicate parameters and invalid IDs are rejected.
The original query spelling/order, timestamp and raw bytes remain in each capture
record. Hidden identity fields must agree when present; an own-board navigation
link or hidden board identity must verify source membership. Error pages and
unverified sources are excluded with a reason.

For `www.sitepowerup.com` and `sitepowerup.com`, narrow CDX prefixes enumerate
Display, Reply and omitted-Action views for the requested BoardID. CDX canonicalizes
query order; every returned URL is checked against the **exact** numeric BoardID
so a prefix such as 102010 cannot admit 1020100. Listings use 200-row pages and
[CDX resumption keys](https://github.com/internetarchive/wayback/tree/master/wayback-cdx-server#resumption-key),
without collapsing versions. New plans request all available HTTP-200 HTML
captures from **1999-01-01 through 2006-12-31 inclusive UTC**. Availability in
1999–2001 affects discovery priority, not the approved capture window.

Only the observed read-view grammar is supported; unknown actions/endpoints,
uncaptured messages and binary attachments are not recovered by this engine.
Exhausting its catalogs does not mean Wayback preserved the whole board.

## Operator commands

Python 3.10+, Ruby 3.0+ and the pinned downloader are sufficient. No API key,
Python package or Ruby gem is required. Keep state outside both repositories:

```bash
SITEPOWERUP_WORK=/var/tmp/eqarchives-sitepowerup/102010
python3 scripts/archive-crawler/sitepowerup_capture.py --work-dir "$SITEPOWERUP_WORK" plan \
  --url 'http://www.sitepowerup.com/mb/view.asp?Action=Reply&BoardID=102010&Reply=12155'
python3 scripts/archive-crawler/sitepowerup_capture.py --work-dir "$SITEPOWERUP_WORK" capture
python3 scripts/archive-crawler/sitepowerup_capture.py --work-dir "$SITEPOWERUP_WORK" status
python3 scripts/archive-crawler/sitepowerup_capture.py --work-dir "$SITEPOWERUP_WORK" verify
```

`plan` accepts a board, message or wrapped Wayback URL and makes no network
requests. The operator may examine an already archived board. The portal separately
blocks duplicate new-site approval. `capture` resumes durable catalogs, downloaded
receipts and cumulative budgets; it shares the tested Ezboard capture engine with
a SitePowerUp-specific URL/source adapter. Different platforms cannot share a
work directory. Simultaneous commands for the same directory are refused.

Output includes `sitepowerup-manifest.json` and sources under
`sources/websites/<host>/<timestamp>/<decoded path>`. For example:

```text
websites/www.sitepowerup.com/20000109050003/mb/view.asp?Action=Display&BoardID=102010
```

New files follow the existing Linux archive convention with literal query strings,
not the historical `_and_` substitution. The manifest retains the exact URL, actual
and requested replay timestamps, SHA-256, CDX metadata and archive path. Unsafe
paths and content/identity collisions are refused rather than overwritten.

Default cumulative bounds are 2,000 downloaded captures, 100,000 CDX rows,
4,000 HTTP requests including retries/redirects, 256 MiB transfer/source bytes,
one hour of active downloader time, and 1 MiB per response. All requests share a
serial persistent client, at least three seconds apart, at most 128 KiB/s, with
bounded 422/429 backoff. Limits can be reduced on `plan`, or explicitly increased
without resetting usage:

```bash
python3 scripts/archive-crawler/sitepowerup_capture.py --work-dir "$SITEPOWERUP_WORK" extend \
  --max-captures 4000 --max-requests 8000 --max-bytes 536870912 --max-seconds 7200
python3 scripts/archive-crawler/sitepowerup_capture.py --work-dir "$SITEPOWERUP_WORK" capture
```

`bounded` means saved work remains; `paused` means an error requires attention.
Both return exit status 2. `status` reads metadata only; `verify` checks source
hashes and destination paths. No command grades, publishes, commits, pushes or
indexes automatically.

## Portal integration

Whole-board scope is explicit and defaults for new board candidates. Already issued
page/directory/custom approvals retain their scope. Unapproved legacy messages
consolidate into one board candidate without copying sources, changing human
decisions or replaying publication/import work. In-progress candidate checks are
left intact. Duplicate suggestions remain in History.

Coverage uses bounded shared Git metadata across boards, including older filename
forms, without an archive checkout walk, blob read or automatic fetch. Incomplete
checks retain progress and block paid grading/approval. Manual submissions retain
the original submitted deep link in their provenance; archived boards retire before
any paid call.

Each approved board receives an independent capture and review. The portal shows
the requested date window, pending catalogs and incomplete-coverage reason.
Newly claimed portal work uses
[complete-files-v1](../../elastic-indexer-stack/curation/README.md) with larger
cumulative allowances and source-verified supporting-file downloads. An unfinished
catalog or download remains paused in Capturing until explicitly resumed. Existing
claimed work and standalone operator plans retain their saved discussion-only
bounds, including explicitly labelled partial results. Publication revalidates source membership, paths and
hashes. Separate whole-site review approval is still required before publication
and the standard AI-enriched indexing flow.
