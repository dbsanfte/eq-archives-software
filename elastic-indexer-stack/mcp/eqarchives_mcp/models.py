"""Public result models shared by the compatible and extended research tools."""

from pydantic import BaseModel


class SearchResult(BaseModel):
    id: str
    title: str
    url: str


class SearchResults(BaseModel):
    results: list[SearchResult]


class Document(SearchResult):
    text: str
    metadata: dict[str, str]
