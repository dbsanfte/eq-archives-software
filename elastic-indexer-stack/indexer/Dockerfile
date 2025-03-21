# indexer/Dockerfile

# ----- Base Stage (with production dependencies) -----
FROM python:3.13-slim AS builder

# Install any required OS packages
RUN export DEBIAN_FRONTEND=noninteractive DEBCONF_NONINTERACTIVE_SEEN=true && \
    apt-get update && \
    apt-get install --no-install-recommends -y \
    file \
    git \
    libmagic-dev \
    tzdata \
    && \
    ln -fs /usr/share/zoneinfo/UTC /etc/localtime && \
    # Workaround for a weird Datetime bug in python:
    ln -s /usr /usr/share/zoneinfo/usr && \
    dpkg-reconfigure -f noninteractive tzdata && \
    apt-get autoremove -y && \
    apt-get clean

WORKDIR /app/src

# Copy production requirements and install them
COPY ./src/indexer/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt 

# Copy the project files
COPY ./src /app/src

FROM builder AS test-stage

# Copy dev requirements and install them
COPY ./src/indexer/requirements-dev.txt .
COPY ./pyproject.toml .
RUN pip install --no-cache-dir -r requirements-dev.txt && \
    python -m pytest && \
    echo "Unit tests passed" > /tmp/test-results.txt

# ----- Compilation Stage -----
FROM builder AS compile-stage

# Precompile Python code with optimization
RUN python -m compileall -b -f -o 2 /app/src && \
    # Remove original .py files
    find /app/src -name "*.py" -type f -delete

# ----- Production Stage (with precompiled code) -----
FROM python:3.13-slim AS production-stage

# Install runtime dependencies only
RUN export DEBIAN_FRONTEND=noninteractive DEBCONF_NONINTERACTIVE_SEEN=true && \
    apt-get update && \
    apt-get install --no-install-recommends -y \
    file \
    libmagic-dev \
    tzdata \
    && \
    ln -fs /usr/share/zoneinfo/UTC /etc/localtime && \
    ln -s /usr /usr/share/zoneinfo/usr && \
    dpkg-reconfigure -f noninteractive tzdata && \
    apt-get autoremove -y && \
    apt-get clean

WORKDIR /app/src

# Copy requirements and install
COPY ./src/indexer/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy the precompiled code
COPY --from=compile-stage /app/src /app/src
COPY --from=test-stage /tmp/test-results.txt /tmp/test-results.txt

WORKDIR /app/src
ENTRYPOINT ["python", "-u", "main.pyc"]