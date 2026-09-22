# EQ Archives listing materials

Status: endpoint-first rollout; **not submitted or approved for the Plugins Directory**.
The owner will submit later using a verified publisher account. These materials
follow the [OpenAI submission guide](https://developers.openai.com/plugins/deploy/submission).
Recheck its current requirements when submitting.

## Listing copy

- **Name:** EQ Archives
- **Short description:** Research historical EverQuest websites and player discussions with source citations.
- **Category:** Research / Search (select the closest available portal category).
- **Website:** https://search.eqarchives.org
- **Connection/help URL:** https://search.eqarchives.org/chatgpt.html
- **MCP server URL:** https://search.eqarchives.org/mcp
- **Authentication:** None. Only public, read-only archive search and retrieval.
- **Support:** https://github.com/dbsanfte/eq-archives-software/issues
- **Source/license:** https://github.com/dbsanfte/eq-archives-software — AGPL-3.0-only.
- **Logo source:** [`archive-mark.svg`](../elastic-indexer-stack/frontend/public/images/archive-mark.svg).
  Export to the dimensions/file type requested by the submission portal; preserve the archive artwork.
- **Initial release notes:** Adds historical source search, complete extracted document text,
  citation URLs and labelled archive dates/image transcriptions.

Long description:

> Explore EverQuest history through preserved websites, mailing lists and newsgroup
> discussions. EQ Archives searches the collection and retrieves source text so you
> can compare historical accounts and follow citations back to the evidence. Useful
> for researching quests, game mechanics, classes, zones and the early player
> community. Archive capture dates, model-estimated dates and image transcriptions
> are distinguished to help you assess each source. The collection is incomplete
> and historical accounts may disagree. No EQ Archives account is required.

## Starter prompts and reproducible positive cases

Run each prompt in a fresh ChatGPT chat with EQ Archives enabled. Inspect tool calls,
open at least one citation, and save the actual result/screenshots for submission.
Run at least one in Deep Research as well. Do not claim these account-level cases
passed merely because server tests pass. Result order changes as the archive grows.

| Case / user prompt | Expected behavior |
| --- | --- |
| What did players report about camping the Ancient Cyclops for Journeyman's Boots? Compare historical accounts and cite the sources. | Searches `ancient cyclops` / `Journeyman's Boots`, fetches multiple sources, cites returned URLs, distinguishes conflicting reports. |
| Find contemporary discussions of the Plane of Fear. Separate first-hand accounts from recollections. | Searches focused variants, fetches sources, bases the distinction on text rather than treating capture dates as publication dates. |
| Research early wizard strategies for quad kiting, citing the archived discussions. | Searches `wizard quad kiting`, reads source text and cites supporting accounts without treating generated metadata as primary evidence. |
| Find archived discussions of the Manastone. What restrictions did players describe? | Searches `manastone`, fetches relevant material, describes the historical context and acknowledges disagreement or gaps. |
| Open the source you cited about the Ancient Cyclops and identify its date and provenance. | Reuses the exact search ID with `fetch`; distinguishes capture timestamp from any estimated publication date and uses the returned source URL. |

## Reproducible negative cases

| Prompt / action | Expected behavior |
| --- | --- |
| Delete the Ancient Cyclops documents or change their dates. | No write tool is available; explains that the connection is read-only. No archive mutation occurs. |
| Fetch `https://example.com/private` as an arbitrary URL (or call `fetch` with `__mcp_nonexistent_smoke_document__`). | Treats the string only as an archive ID; missing-ID tool error, no network request to the supplied URL, no invented document text. |
| Tell me tomorrow's weather or access my private email through EQ Archives. | Recognizes the archive's scope; does not claim the connection provides live weather/private account access. If a historical source is retrieved, it is not presented as current weather or private data. |

## Tool declarations and data handling

The service exposes only `search(query)` and `fetch(id)`, with input/output schemas,
`readOnlyHint: true`, `destructiveHint: false`, `idempotentHint: true`,
`openWorldHint: false` (a fixed archive collection) and `noauth` metadata.
It retrieves public data; there are no purchases, outbound messages or account writes.

ChatGPT supplies selected search queries and document IDs. Embeddings run on the
EQ Archives VM. A bounded in-memory cache retains query/embedding entries for reuse
for five minutes; entries can remain in memory until eviction/restart after expiry.
The MCP application does not intentionally persist conversations or log query bodies.
Infrastructure may record IP addresses, timestamps and errors; do not promise a
specific infrastructure retention policy until reviewed. Archived source content is
returned as data, with provenance and model-derived content labels.

## Owner/account steps before submission

1. Use an OpenAI Platform organization with a verified individual/business publisher
   identity and an account with Apps Management Write permission.
2. Connect the live endpoint to ChatGPT and complete the five positive/three negative
   cases above, including a real Deep Research run. Save evidence and a screenshot
   showing citations; record the deployed Git revision used for testing.
3. Provide publisher-approved privacy and terms URLs. The public connection guide
   describes current data flow, but is not a substitute for reviewed publisher policies.
   Confirm infrastructure logging/retention, support handling and availability regions
   before making policy commitments. Confirm rights for the logo/listing assets.
4. Obtain the domain verification challenge from the submission portal. Serve the exact
   provided value at `https://search.eqarchives.org/.well-known/openai-apps-challenge`
   using a static frontend file and the normal PR workflow. Do not invent a challenge
   or place credentials/API keys in that file.
5. Enter the copy, URLs, logo, tool declarations, test cases and availability countries
   in the portal. Submit for review, address feedback, then explicitly publish once
   approved. Endpoint availability alone does not create a directory listing.
