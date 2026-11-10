"""Validated research filters and bounded, deterministic keyword queries."""

from datetime import date
import re
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, field_validator, model_validator
from mcp.server.mcpserver.exceptions import ToolError

from .models import SearchResult
from .limits import TextBudget


MAX_RESEARCH_RESULTS = 1000
FilterValue = Annotated[str, StringConstraints(strict=True, strip_whitespace=True, min_length=1, max_length=255)]
CalendarDate = Annotated[str, Field(strict=True, pattern=r"^\d{4}-\d{2}-\d{2}$")]
SourceType = Literal["domain", "mailing_list", "file_type", "mime_type", "theme", "tag", "collection"]
Collection = Literal["websites", "mailing_lists", "newsgroups"]
COLLECTION_PREFIXES = {"websites": "websites/", "mailing_lists": "mailing-lists/", "newsgroups": "newsgroups/"}
GroupBy = Literal["documents", "page", "discussion"]
SortOrder = Literal["relevance", "capture_date_asc", "capture_date_desc",
                    "estimated_publication_date_asc", "estimated_publication_date_desc",
                    "last_indexed_asc", "last_indexed_desc"]
SOURCE_KEYS = {"domain": "domain_name", "mailing_list": "mailing_list_name", "file_type": "file_type",
               "mime_type": "mime_type", "theme": "llm_content_flavour", "tag": "llm_tags"}
DATE_KEYS = {"capture_date": "capture_date", "estimated_publication_date": "llm_guessed_date", "last_indexed": "last_indexed"}


class URLScope(BaseModel):
    """A literal URL boundary, never a URL to fetch or caller-supplied regex."""
    model_config = ConfigDict(extra="forbid")
    url: Annotated[str, StringConstraints(strict=True, min_length=1, max_length=1024)]
    kind: Literal["host", "directory", "page", "ezboard_board"] = "directory"

    @model_validator(mode="after")
    def valid_scope(self):
        from .identity import scope_query
        scope_query(self)
        return self


class ResearchFilters(BaseModel):
    """Values within a category are alternatives; different categories must all match."""

    model_config = ConfigDict(extra="forbid")
    domains: list[FilterValue] = Field(default_factory=list, max_length=10, description="Exact domain values from list_sources; www prefixes are significant.")
    mailing_lists: list[FilterValue] = Field(default_factory=list, max_length=10, description="Exact mailing-list values from list_sources.")
    file_types: list[FilterValue] = Field(default_factory=list, max_length=10, description="Exact file-type values from list_sources.")
    mime_types: list[FilterValue] = Field(default_factory=list, max_length=10)
    themes: list[FilterValue] = Field(default_factory=list, max_length=10, description="Model-generated content flavours; not human classifications.")
    tags: list[FilterValue] = Field(default_factory=list, max_length=10, description="Model-generated tags; not human classifications.")
    collections: list[Collection] = Field(default_factory=list, max_length=3)
    url_scopes: list[URLScope] = Field(default_factory=list, max_length=3, description="Literal host, directory/account, page or source-backed Ezboard board scopes. Never fetches remote URLs.")
    has_text: Annotated[bool, Field(strict=True)] | None = Field(default=None, description="Indexed extracted-text field presence. Legacy empty fields may still match.")
    has_enrichment: Annotated[bool, Field(strict=True)] | None = Field(default=None, description="Presence of an enrichment model/signature; does not imply human validation.")
    has_ocr: Annotated[bool, Field(strict=True)] | None = Field(default=None, description="Indexed image-transcription field presence. Legacy empty fields may still match.")
    capture_from: CalendarDate | None = None
    capture_to: CalendarDate | None = None
    indexed_from: CalendarDate | None = None
    indexed_to: CalendarDate | None = None
    publication_from: CalendarDate | None = None
    publication_to: CalendarDate | None = None
    date_field: Literal["capture_date", "estimated_publication_date", "last_indexed"] = Field(default="capture_date", description="Capture dates are archive timestamps; estimated publication dates are model-generated and may be wrong.")
    date_from: CalendarDate | None = Field(default=None, description="Inclusive UTC calendar day, YYYY-MM-DD.")
    date_to: CalendarDate | None = Field(default=None, description="Inclusive UTC calendar day, YYYY-MM-DD.")

    @field_validator("date_from", "date_to", "capture_from", "capture_to", "indexed_from", "indexed_to", "publication_from", "publication_to")
    @classmethod
    def valid_calendar_date(cls, value):
        if value is not None:
            date.fromisoformat(value)
        return value

    @model_validator(mode="after")
    def ordered_dates(self):
        for _, low, high in self.date_windows():
            if low and high and low > high:
                raise ValueError("Date windows must intersect and start on or before their end")
        return self

    def date_windows(self):
        windows = {"capture_date": [self.capture_from, self.capture_to],
                   "last_indexed": [self.indexed_from, self.indexed_to],
                   "llm_guessed_date": [self.publication_from, self.publication_to]}
        window = windows[DATE_KEYS[self.date_field]]
        window[0] = max(filter(None, [window[0], self.date_from]), default=None)
        window[1] = min(filter(None, [window[1], self.date_to]), default=None)
        return [(field, *bounds) for field, bounds in windows.items()]


class GroupInfo(BaseModel):
    kind: Literal["page", "discussion", "record"]
    key: str
    matching_captures_in_window: int


class ResearchResult(SearchResult):
    metadata: dict[str, str]
    group: GroupInfo | None = None


class ResultCount(BaseModel):
    value: Annotated[int, Field(ge=0, strict=True)]
    relation: Literal["eq", "gte"]


class ResearchPage(BaseModel):
    results: list[ResearchResult]
    total: ResultCount
    offset: int
    limit: int
    next_offset: int | None
    has_more: bool
    limit_reached: bool
    note: str
    group_by: GroupBy = "documents"
    scanned_documents: int | None = None


class SourceValue(BaseModel):
    value: str
    document_count: Annotated[int, Field(ge=0, strict=True)]


class SourcePage(BaseModel):
    source_type: SourceType
    sources: list[SourceValue]
    next_after: str | None
    note: str


def has_constraints(query: str) -> bool:
    return any(c in query for c in '"|+()') or bool(re.search(r"(^|\s)-\S", query))


def keyword_query(query: str) -> dict:
    query = query.strip()
    if not query:
        return {"match_all": {}}
    try:
        TextBudget().accept(query)
    except ValueError:
        raise ToolError("Use a shorter research query (maximum 100 conservative token units).") from None
    return {"simple_query_string": {
        "query": query, "fields": ["title^3", "text_full", "llm_image_text_full"],
        # OR can turn cyclops -ring into cyclops OR anything without ring.
        "default_operator": "and" if has_constraints(query) else "or",
        "minimum_should_match": "2<60%",
        "flags": "AND|OR|NOT|PHRASE|PRECEDENCE|WHITESPACE|ESCAPE",
    }}


def filtered_query(query: str, filters: ResearchFilters) -> dict:
    clauses = []
    for attribute, field in (("domains", "domain_name"), ("mailing_lists", "mailing_list_name"),
                             ("file_types", "file_type"), ("mime_types", "mime_type"),
                             ("themes", "llm_content_flavour"), ("tags", "llm_tags")):
        values = getattr(filters, attribute)
        if values:
            clauses.append({"terms": {field: values}})
    if filters.collections:
        clauses.append({"bool": {"minimum_should_match": 1, "should": [
            {"prefix": {"id": COLLECTION_PREFIXES[value]}} for value in filters.collections]}})
    for field, low, high in filters.date_windows():
        dates = {}
        if low:
            dates["gte"] = low + "T00:00:00.000Z"
        if high:
            dates["lte"] = high + "T23:59:59.999Z"
        if dates:
            clauses.append({"range": {field: dates}})
    for attribute, fields in (("has_text", ["text_full"]), ("has_ocr", ["llm_image_text_full"]),
                              ("has_enrichment", ["llm_model_name", "llm_enrichment_signature"])):
        present = getattr(filters, attribute)
        if present is not None:
            existence = {"bool": {"minimum_should_match": 1, "should": [{"exists": {"field": f}} for f in fields]}}
            clauses.append(existence if present else {"bool": {"must_not": [existence]}})
    if filters.url_scopes:
        from .identity import scope_query
        clauses.append({"bool": {"minimum_should_match": 1, "should": [scope_query(scope) for scope in filters.url_scopes]}})
    return {"bool": {"must": [keyword_query(query)], "filter": clauses}}


def research_sort(order: SortOrder) -> list[dict]:
    if order == "relevance":
        primary = {"_score": "desc"}
    else:
        name, direction = order.rsplit("_", 1)
        primary = {DATE_KEYS[name]: {"order": direction, "missing": "_last"}}
    # Legacy records can lack id. A fixed replica preference plus _doc also
    # orders those ties consistently until indexing changes the live index.
    return [primary, {"id": {"order": "asc", "missing": "_last"}}, {"_doc": "asc"}]
