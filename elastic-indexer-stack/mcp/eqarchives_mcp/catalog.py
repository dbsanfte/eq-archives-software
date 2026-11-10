"""Bounded metadata directories and dated capture navigation; no remote browsing."""
from collections import OrderedDict

from mcp.server.mcpserver.exceptions import ToolError
from pydantic import BaseModel

from .identity import (VERSION, board_forums, discussion_identity, group_identity,
                       literal, page_identity, scope_query, site_scope_url, id_regex)
from .limits import TextBudget
from .research import (MAX_RESEARCH_RESULTS, COLLECTION_PREFIXES, GroupInfo, ResearchFilters,
                       ResearchPage, ResultCount, SourcePage, SourceValue, filtered_query,
                       research_sort)


class Site(BaseModel):
    domain: str
    scope_url: str | None
    latest_indexed: str
    first_indexed: str | None
    document_count: int
    earliest_capture: str | None
    latest_capture: str | None


class SitePage(BaseModel):
    sites: list[Site]
    limit: int
    has_more: bool
    note: str


class Catalog:
    async def selection(self, query, filters):
        selection = filtered_query(query, filters)
        boards = [scope for scope in filters.url_scopes if scope.kind == "ezboard_board"]
        if boards:
            scopes = []
            for scope in filters.url_scopes:
                if scope.kind != "ezboard_board":
                    scopes.append(scope_query(scope))
                    continue
                data = await self.execute({"size": 10, "timeout": "8s", "track_total_hits": False,
                    "_source": ["id", "text_full"], "sort": research_sort("capture_date_desc"),
                    "query": scope_query(scope)})
                try:
                    forums = board_forums(scope, data["hits"]["hits"])
                except ValueError:
                    raise ToolError("Board evidence is too broad; choose a narrower URL scope.") from None
                if not forums:
                    raise ToolError("No archived board-index forum links verify this board scope. Use an exact page/directory scope instead.")
                scopes.append(scope_query(scope, forums))
            selection["bool"]["filter"][-1] = {"bool": {"minimum_should_match": 1, "should": scopes}}
        return selection

    async def collection_sources(self, query, filters, after, limit):
        data = await self.execute({"size": 0, "timeout": "8s", "track_total_hits": False, "_source": False,
            "query": await self.selection(query, filters),
            "aggs": {"collections": {"filters": {"filters": {
                k: {"prefix": {"id": v}} for k, v in COLLECTION_PREFIXES.items()}}}}})
        try:
            buckets = data["aggregations"]["collections"]["buckets"]
            values = [SourceValue(value=k, document_count=buckets[k]["doc_count"])
                      for k in sorted(COLLECTION_PREFIXES) if k > (after or "") and buckets[k]["doc_count"]]
            return SourcePage(source_type="collection", sources=values[:limit],
                next_after=values[limit-1].value if len(values) > limit else None,
                note="Fixed archive collections with matching record counts; websites include pages and assets.")
        except (KeyError, TypeError, ValueError):
            raise ToolError("Archive sources are temporarily unavailable. Please retry.") from None

    async def list_sites(self, query="", filters=None, limit=20):
        if self.requests.locked():
            raise ToolError("Archive research is busy. Please retry shortly.")
        async with self.requests:
            selection = await self.selection(query, filters or ResearchFilters())
            selection["bool"]["filter"] += [{"prefix": {"id": "websites/"}}, {"exists": {"field": "last_indexed"}}]
            selection["bool"]["must_not"] = [{"term": {"domain_name": ""}}]
            body = {"size": 0, "timeout": "8s", "track_total_hits": False, "_source": False, "query": selection}
            data = await self.execute({**body, "aggs": {"sites": {"terms": {
                "field": "domain_name", "size": limit+1, "order": [{"latest": "desc"}, {"_key": "asc"}]},
                "aggs": {"latest": {"max": {"field": "last_indexed"}}}}}})
            try:
                buckets = data["aggregations"]["sites"]["buckets"]
                domains = [b["key"] for b in buckets[:limit]]
                if not domains:
                    return SitePage(sites=[], limit=limit, has_more=False, note=SITE_NOTE)
                # Terms ordered by a max reliably select newest domains, but counts
                # can omit shards. A second bounded filters aggregation gives exact
                # matching counts/ranges for those selected domains on every shard.
                metrics = {"latest": {"max": {"field": "last_indexed"}},
                           "first": {"min": {"field": "site_first_indexed_at"}},
                           "known_first": {"value_count": {"field": "site_first_indexed_at"}},
                           "earliest": {"min": {"field": "capture_date"}},
                           "newest": {"max": {"field": "capture_date"}}}
                details = await self.execute({**body, "aggs": {"details": {"filters": {"filters": {
                    domain: {"term": {"domain_name": domain}} for domain in domains}}, "aggs": metrics}}})
                rows = details["aggregations"]["details"]["buckets"]
                sites = []
                for domain in domains:
                    row = rows[domain]
                    first = row["first"].get("value_as_string") if row["known_first"]["value"] == row["doc_count"] else None
                    sites.append(Site(domain=domain, scope_url=site_scope_url(domain),
                        latest_indexed=row["latest"]["value_as_string"], first_indexed=first,
                        document_count=row["doc_count"], earliest_capture=row["earliest"].get("value_as_string"),
                        latest_capture=row["newest"].get("value_as_string")))
                return SitePage(sites=sites, limit=limit, has_more=len(buckets)>limit, note=SITE_NOTE)
            except (KeyError, TypeError, ValueError):
                raise ToolError("Archive sites are temporarily unavailable. Please retry.") from None

    async def grouped_page(self, query, filters, offset, limit, sort, group_by):
        from .archive import PROVENANCE_FIELDS, SEARCH_FIELDS, provenance, result_from_hit
        from .research import ResearchResult
        data = await self.execute({"size": MAX_RESEARCH_RESULTS, "timeout": "8s", "track_total_hits": MAX_RESEARCH_RESULTS+1,
            "_source": SEARCH_FIELDS+PROVENANCE_FIELDS+["discussion"],
            "query": await self.selection(query, filters), "sort": research_sort(sort)}, stable_order=True)
        try:
            raw_total = ResultCount.model_validate(data["hits"]["total"])
            groups = OrderedDict()
            for hit in data["hits"]["hits"]:
                key = group_identity(hit, group_by)
                if key not in groups:
                    groups[key] = ResearchResult(**result_from_hit(hit).model_dump(), metadata=provenance(hit["_source"]),
                        group=GroupInfo(kind=key.split(":", 1)[0], key=key, matching_captures_in_window=0))
                groups[key].group.matching_captures_in_window += 1
            scanned = len(data["hits"]["hits"])
            bounded = raw_total.relation == "gte" or scanned < raw_total.value
            results = list(groups.values())[offset:offset+limit]
            end = offset+len(results)
            more = end < len(groups)
            note = ("Grouped over at most 1,000 matching documents; group counts describe this window only. "
                    "Discussion groups require stored source-verified identity; others fall back to exact pages or individual records. "
                    "Use list_captures with a representative ID, then fetch an exact dated ID. " + SCOPE_NOTE)
            if bounded:
                note += " Scan limit reached; additional groups/captures may exist. Narrow the filters."
            return ResearchPage(results=results, total=ResultCount(value=len(groups), relation="gte" if bounded else "eq"),
                offset=offset, limit=limit, next_offset=end if more else None, has_more=more or bounded,
                limit_reached=bounded and not more, note=note, group_by=group_by, scanned_documents=scanned)
        except (KeyError, TypeError, ValueError):
            raise ToolError("Archive groups are temporarily unavailable. Please retry.") from None

    async def list_captures(self, document_id, group_by="page", offset=0, limit=20):
        from .archive import PROVENANCE_FIELDS, SEARCH_FIELDS, provenance, result_from_hit
        from .research import ResearchResult
        if self.requests.locked():
            raise ToolError("Archive research is busy. Please retry shortly.")
        async with self.requests:
            fields = SEARCH_FIELDS+PROVENANCE_FIELDS+["discussion"]
            anchor = await self.query({"size": 1, "timeout": "8s", "_source": fields,
                                       "query": {"ids": {"values": [document_id]}}})
            if not anchor:
                raise ToolError("Document not found. Use an exact returned ID.")
            discussion = discussion_identity(anchor[0]) if group_by == "discussion" else None
            page = page_identity(anchor[0])
            identity = discussion or (page[0] if page else "record:"+document_id)
            if discussion:
                selection = {"bool": {"filter": [{"term": {"discussion.version": VERSION}},
                                                  {"term": {"discussion.key": discussion.split(":", 1)[1]}}]}}
            elif page:
                try:
                    exact = id_regex(literal(page[1]), literal(page[2]))
                except ValueError:
                    raise ToolError("This page path exceeds capture-history query limits. Fetch its exact ID or narrow a search by date.") from None
                branches = [exact, {"ids": {"values": [document_id]}}]
                try:
                    original = TextBudget().accept(page[0][5:], limit=2048)
                except ValueError:
                    pass  # No analyzed URL fallback when its token budget is unknown.
                else:
                    branches.append({"bool": {"must_not": [{"exists": {"field": "id"}}],
                                               "must": [{"match_phrase": {"url": original}}]}})
                selection = {"bool": {"minimum_should_match": 1, "should": branches}}
            else:
                selection = {"ids": {"values": [document_id]}}
            # A bounded scan verifies every member again and gives honest grouped
            # totals even when stale/invalid proofs or differing origins are present.
            data = await self.execute({"size": MAX_RESEARCH_RESULTS, "timeout": "8s", "track_total_hits": MAX_RESEARCH_RESULTS+1,
                "_source": fields, "query": selection, "sort": research_sort("capture_date_desc")}, stable_order=True)
            try:
                total = ResultCount.model_validate(data["hits"]["total"])
                hits = [h for h in data["hits"]["hits"] if group_identity(h, "discussion" if discussion else "page") == identity]
                bounded = total.relation == "gte" or total.value > len(data["hits"]["hits"])
                results = [ResearchResult(**result_from_hit(h).model_dump(), metadata=provenance(h["_source"]))
                           for h in hits[offset:offset+limit]]
                end = offset+len(results)
                more = end < len(hits)
                return ResearchPage(results=results, total=ResultCount(value=len(hits), relation="gte" if bounded else "eq"),
                    offset=offset, limit=limit, next_offset=end if more else None, has_more=more or bounded,
                    limit_reached=bounded and not more, group_by="discussion" if discussion else "page",
                    scanned_documents=len(data["hits"]["hits"]), note=(
                        "Newest capture first. Fetch an exact returned ID to read that version. "
                        "Unverified discussions fall back to exact page identity. Legacy captures without indexed IDs may be absent. "
                        + ("The 1,000-document scan limit was reached; history is incomplete." if bounded else "")))
            except (KeyError, TypeError, ValueError):
                raise ToolError("Archive capture history is temporarily unavailable. Please retry.") from None


SCOPE_NOTE = "URL scopes include bare/www and default-port aliases; nondefault ports stay separate. Ezboard membership uses links from at most ten archived board indices; older/unlinked forums may be absent."
SITE_NOTE = ("Newest matching website domains, including assets; document counts/ranges describe the supplied filters, not unique pages. "
             "Shared-host accounts remain within their URL scopes. At most the requested newest domains are returned; narrow filters if has_more. "
             "last_indexed is a search-index timestamp, not capture/publication or Git commit time. "
             "first_indexed is recorded conservatively for new site imports only; legacy/partially known history is unknown. "
             "Listings use a live index and may change between the directory and count queries. " + SCOPE_NOTE)
