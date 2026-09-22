"""Validated research filters and bounded, deterministic keyword queries."""

from datetime import date
import re
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, field_validator, model_validator

from .models import SearchResult


MAX_RESEARCH_RESULTS = 1000
FilterValue = Annotated[str, StringConstraints(strict=True, strip_whitespace=True, min_length=1, max_length=255)]
CalendarDate = Annotated[str, Field(strict=True, pattern=r"^\d{4}-\d{2}-\d{2}$")]
SourceType = Literal["domain", "mailing_list", "file_type"]
SortOrder = Literal["relevance", "capture_date_asc", "capture_date_desc",
                    "estimated_publication_date_asc", "estimated_publication_date_desc"]
SOURCE_KEYS = {"domain": "domain_name", "mailing_list": "mailing_list_name", "file_type": "file_type"}
DATE_KEYS = {"capture_date": "capture_date", "estimated_publication_date": "llm_guessed_date"}


class ResearchFilters(BaseModel):
    """Values within a category are alternatives; different categories must all match."""

    model_config = ConfigDict(extra="forbid")
    domains: list[FilterValue] = Field(default_factory=list, max_length=10, description="Exact domain values from list_sources; www prefixes are significant.")
    mailing_lists: list[FilterValue] = Field(default_factory=list, max_length=10, description="Exact mailing-list values from list_sources.")
    file_types: list[FilterValue] = Field(default_factory=list, max_length=10, description="Exact file-type values from list_sources.")
    date_field: Literal["capture_date", "estimated_publication_date"] = Field(default="capture_date", description="Capture dates are archive timestamps; estimated publication dates are model-generated and may be wrong.")
    date_from: CalendarDate | None = Field(default=None, description="Inclusive UTC calendar day, YYYY-MM-DD.")
    date_to: CalendarDate | None = Field(default=None, description="Inclusive UTC calendar day, YYYY-MM-DD.")

    @field_validator("date_from", "date_to")
    @classmethod
    def valid_calendar_date(cls, value):
        if value is not None:
            date.fromisoformat(value)
        return value

    @model_validator(mode="after")
    def ordered_dates(self):
        if self.date_from and self.date_to and self.date_from > self.date_to:
            raise ValueError("date_from must be on or before date_to")
        return self


class ResearchResult(SearchResult):
    metadata: dict[str, str]


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
    return {"simple_query_string": {
        "query": query, "fields": ["title^3", "text_full", "llm_image_text_full"],
        # OR can turn cyclops -ring into cyclops OR anything without ring.
        "default_operator": "and" if has_constraints(query) else "or",
        "minimum_should_match": "2<60%",
        "flags": "AND|OR|NOT|PHRASE|PRECEDENCE|WHITESPACE|ESCAPE",
    }}


def filtered_query(query: str, filters: ResearchFilters) -> dict:
    clauses = []
    for attribute, field in (("domains", "domain_name"), ("mailing_lists", "mailing_list_name"), ("file_types", "file_type")):
        values = getattr(filters, attribute)
        if values:
            clauses.append({"terms": {field: values}})
    dates = {}
    if filters.date_from:
        dates["gte"] = filters.date_from + "T00:00:00.000Z"
    if filters.date_to:
        dates["lte"] = filters.date_to + "T23:59:59.999Z"
    if dates:
        clauses.append({"range": {DATE_KEYS[filters.date_field]: dates}})
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
