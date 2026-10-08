# Ezboard capture

Use `ezboard_capture.py` for an operator capture, or select **Whole Ezboard** in
the curation portal. Ezboard candidates represent the top-level board, not a
forum or an individual message. The board identity is shared across numbered
`server`, `pub`, `p` and `b` Ezboard hosts. Original source URLs and dates remain
separate, exact document identities.

## Evidence from the existing archive

The archive already has a special case in
[`capture-links.sh`](https://github.com/dbsanfte/eq-archives/blob/c16953583a5b8ae7d17432b5dfe45a6d4d796c43/capture-links.sh):
it removes the first path character and filters a server's captures by the
remaining name. That avoids treating the board as a directory, but uses five
concurrent downloads, does not discover server moves, and can match unrelated
board-name prefixes. The new implementation retains the useful board-name
approach with source verification, resumable listings and serial transport.

Examined archive sources include:

| Archive path under `websites/` | Structure observed |
| --- | --- |
| `pub4.ezboard.com/20000511120232/beqasylum/index.html` | EQAsylum board index with 22 forum links |
| `pub4.ezboard.com/20000510052216/feqasylumgeneral/index.html` | General forum, `?page=2` through `?page=11`, and message links |
| `pub110.ezboard.com/20020419220745/beqasylum/index.html` | Lanys Community Forum, same board name on another server; links back to `pub4` |
| `pub110.ezboard.com/20020712205705/feqasylumgeneral.showMessage?topicID=11898.topic&index=28` | An individual reply, including its board/forum breadcrumbs |
| `pub110.ezboard.com/20020602023020/feqasylumfrm25.showMessageRange?topicID=2358.topic&start=21&stop=37` | A later range of replies and a link to replies 1–20 |
| `pub199.ezboard.com/20030619045917/bcolossusoflanys/index.html` | Another Lanys board using numbered `frm` forum names |

These are sibling paths: `/beqasylum/` does **not** contain `/feqasylumgeneral`.
The board/forum split in a concatenated `f…` token cannot be inferred reliably
from its spelling. A candidate deep link must have archived parent-board
navigation before it can become a board candidate. No candidate or approval is
created by guessing a split. HTTP-200 Ezboard system/error pages also occur in
the archive; these are excluded and reported, not counted as recovered posts.

## Operator commands

Python 3.10+, Ruby 3.0+, Git and the pinned downloader are sufficient. No API key,
Python package or Ruby gem is needed. Run from the software repository:

```bash
EZBOARD_WORK=/var/tmp/eqarchives-ezboard/eqasylum
python3 scripts/archive-crawler/ezboard_capture.py --work-dir "$EZBOARD_WORK" plan \
  --url http://pub4.ezboard.com/beqasylum \
  --host pub110.ezboard.com \
  --archive-repo /path/to/existing/eq-archives
python3 scripts/archive-crawler/ezboard_capture.py --work-dir "$EZBOARD_WORK" capture
python3 scripts/archive-crawler/ezboard_capture.py --work-dir "$EZBOARD_WORK" status
python3 scripts/archive-crawler/ezboard_capture.py --work-dir "$EZBOARD_WORK" verify
```

`plan` makes no Wayback requests. It accepts a board URL or dated Wayback board
link. `--host` is repeatable. Optional `--archive-repo` adds the numbered Ezboard
hosts found in the archive's **root host tree only**, without reading sources,
walking a checkout or fetching missing objects. Board/forum links in verified
captures can add further historical hosts. There is no claim that these are all
servers on which a board ever existed; add any other known shard with `--host`
when planning. The operator command permits examination of an already archived
board; the portal's separate duplicate check blocks it from new-site approval.

`capture` resumes the same plan and durable budgets. It writes
`ezboard-manifest.json`, a SQLite checkpoint and raw sources under
`$EZBOARD_WORK/sources/websites/<host>/<timestamp>/<decoded path>`. Extensionless
pages use `index.html`; message query strings stay in the filename, following
the archive's existing Linux convention. The manifest retains the original CDX
URL spelling, actual replay timestamp, requested timestamp, raw SHA-256, CDX
metadata and source path. It refuses ambiguous path/content collisions.

| Resource | Default cumulative limit |
| --- | --- |
| Downloaded captures, including excluded source pages | 2,000 |
| CDX rows examined | 100,000 |
| Historical hosts | 512 |
| HTTP requests, including retries and redirects | 4,000 |
| Response size / total transfer | 1 MiB / 256 MiB |
| Active downloader time across resumes | 3,600 seconds |
| Request spacing / transfer rate | 3 seconds / 128 KiB/s |

New captures cover **1999-01-01 through 2006-12-31 inclusive UTC**, retaining all
available dated versions in both **1999–2001** and **2002–2006**, even when the
first tier has results. Early availability gives sites discovery priority; it
does not exclude later snapshots from an approved capture. Each known host
gets narrow `b<board>` and `f<board>` CDX prefix queries. The downloader follows
[CDX resumption keys](https://github.com/internetarchive/wayback/tree/master/wayback-cdx-server#resumption-key)
in 200-row pages, without collapsing different pages or dated versions. It
captures available HTTP-200 HTML board/forum pages and `showMessage`,
`showMessageRange`, `showNextMessage` and `showPrevMessage` views. Pagination and
reply query parameters are preserved. Login, reply forms, profiles, advertising
and binary assets are outside this discussion capture. Requests go only to
Wayback, through the existing single persistent client and bounded 422/429
backoff; original live servers are never contacted.

Limits can be set on `plan`. To explicitly extend an operator run, increase its
**cumulative** limits; usage is never reset:

```bash
python3 scripts/archive-crawler/ezboard_capture.py --work-dir "$EZBOARD_WORK" extend \
  --max-captures 4000 --max-requests 8000 --max-bytes 536870912 --max-seconds 7200
python3 scripts/archive-crawler/ezboard_capture.py --work-dir "$EZBOARD_WORK" capture
```

`bounded` means more work remains. `paused` means an error needs attention;
resume retains sources and catalog position. Exit status 2 signals either
condition. `complete` means the saved catalogs for the known hosts were exhausted,
**not** that Wayback preserved the entire board. Notes and counts identify missing
captures, error pages and unverified membership. `status` reads metadata only;
`verify` explicitly reads the manifest's sources and checks hashes. Simultaneous
commands for the same work directory are refused.
The date window is saved with the plan. A legacy operator plan keeps its former
1999–2007 window on resume rather than silently changing captured evidence.

This command does not grade, publish, commit, push or index. Its operator manifest
is not a portal publication approval. Keep all state outside both repositories.

## Portal behavior

Discovery and manual submissions resolve Ezboard forum/message links to their
parent board before creating or grading a candidate. Resolution uses bounded
Wayback samples and reuses exact verified source evidence. The saved parent
mapping avoids creating another candidate for a different thread or historical
server. Coverage must be verified before Luna runs. Unresolvable manual links
pause with a request for the top-level board URL; discovery retains unresolved
resolution evidence and continues without creating thread candidates.

Archive coverage uses a shared, resumable inventory of board identities across
numbered Ezboard servers, independently of the capped page inventory. It reads
only bounded Git tree metadata (the existing 4,096-tree/32-MiB/15-second slice),
with no clone, checkout walk or automatic fetch. Cached positive matches are
immediate. A forum-only match without verified ownership remains uncertain and
blocks approval.

New board candidates default to the explicit **Whole Ezboard** scope. Each
approved board receives its own capture budget and whole-site review; the worker
preserves queue order and the Undo grace period. The portal uses the defaults
above and also seeds its historical host list from the archive's root metadata.
A bounded result can be reviewed as a partial capture. A transport failure stays
paused in Capturing and requires an explicit resume. Source membership and exact
archive destinations are verified again before publication. Existing page,
directory and custom-folder approvals retain their original scope. Indexing
still requires separate whole-site approval and uses the existing $2/site
source-bound enrichment budget.

On deployment, unapproved legacy thread/forum suggestions with valid saved
parent evidence consolidate into one board suggestion without copying sources
or replaying approved work. Their old rows remain in History. Unresolved legacy
suggestions move to Saved for later with a parent-board explanation. Approved,
capturing, review, publication and indexing records are preserved.
