FROM python:3.13-slim-bookworm@sha256:2325bb286ec344af3e5898cc224b5844e2707ac6e26b1632516fd3edc84a5e26 AS dependencies
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PIP_DISABLE_PIP_VERSION_CHECK=1
WORKDIR /app
COPY requirements.txt ./
RUN pip install --no-cache-dir --require-hashes -r requirements.txt

FROM dependencies AS test
COPY requirements-dev.txt ./
RUN pip install --no-cache-dir --require-hashes -r requirements-dev.txt
COPY . .
RUN python -m pytest && touch /tests-passed

FROM dependencies AS runtime
COPY --from=test /tests-passed /tests-passed
COPY eqarchives_mcp ./eqarchives_mcp
USER 10001:10001
EXPOSE 8080
CMD ["uvicorn", "eqarchives_mcp.server:create_app", "--factory", "--host", "0.0.0.0", "--port", "8080", "--no-access-log", "--log-level", "warning"]
