# Build from the software repository root; no archive content enters the image.
FROM python:3.13-slim-bookworm@sha256:2325bb286ec344af3e5898cc224b5844e2707ac6e26b1632516fd3edc84a5e26 AS dependencies
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PYTHONPATH=/app/scripts/archive-crawler:/app/curation:/app \
    NOMIC_TOKENIZER_PATH=/opt/eqarchives/nomic-tokenizer.json
RUN apt-get update && apt-get install -y --no-install-recommends git ruby openssh-client libmagic1 ca-certificates && rm -rf /var/lib/apt/lists/*
# OpenSSH requires getpwuid() to resolve the configured non-root runtime UID.
RUN groupadd --gid 10001 curation && useradd --uid 10001 --gid 10001 --home-dir /tmp --no-create-home --no-log-init --shell /usr/sbin/nologin curation
WORKDIR /app
COPY elastic-indexer-stack/curation/requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir --require-hashes -r /app/requirements.txt
COPY scripts/archive-crawler /app/scripts/archive-crawler
COPY elastic-indexer-stack/curation /app/curation
COPY elastic-indexer-stack/indexer/src/indexer/__init__.py elastic-indexer-stack/indexer/src/indexer/chunking.py elastic-indexer-stack/indexer/src/indexer/html_extraction.py /app/indexer/
COPY elastic-indexer-stack/indexer/src/indexer/capture_enrichment.py /app/indexer/
COPY elastic-indexer-stack/indexer/src/indexer/resources /app/indexer/resources
COPY elastic-indexer-stack/indexer/src/index_captures.py /app/index_captures.py
RUN python -m indexer.chunking --download-tokenizer /opt/eqarchives/nomic-tokenizer.json

FROM dependencies AS test
COPY elastic-indexer-stack/curation/requirements-dev.txt /app/requirements-dev.txt
RUN pip install --no-cache-dir --require-hashes -r /app/requirements-dev.txt
WORKDIR /app/curation
RUN python -m pytest --cov=server --cov=worker --cov=capture_progress --cov=capture_continuation --cov=review_actions --cov=discovery_run --cov=state --cov=publisher --cov=captures --cov=full_capture --cov=source_assets --cov=capture_queue --cov=capture_flow --cov=site_reviews --cov=portal --cov=candidate_actions --cov=candidate_checks --cov=jobs --cov=seed --cov=coverage_check --cov=site_inventory --cov=manual --cov=spending --cov=index_captures --cov=indexer.capture_enrichment --cov=import_status --cov=ezboard_portal --cov=ezboard --cov=ezboard_capture --cov=sitepowerup --cov=sitepowerup_capture --cov=sitepowerup_inventory --cov=sitepowerup_portal --cov=ezboard_discovery --cov=ezboard_inventory --cov=archive_layout --cov-report=term-missing --cov-fail-under=90 tests /app/scripts/archive-crawler/test_ezboard.py /app/scripts/archive-crawler/test_sitepowerup.py && touch /tests-passed

FROM dependencies AS runtime
COPY --from=test /tests-passed /tests-passed
ARG GIT_SHA=unknown
ENV GIT_SHA=$GIT_SHA
USER 10001:10001
EXPOSE 8090
CMD ["python", "/app/curation/server.py"]
