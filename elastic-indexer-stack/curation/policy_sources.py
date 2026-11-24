"""Curation compatibility imports for the shared offline source resolver."""
from indexer.source_files import Sources, SourceError, relative


class ServiceError(ValueError):
    """The maintenance service could not complete a verified operation."""
