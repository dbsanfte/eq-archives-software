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

# ----- Production Stage (without dev dependencies) -----
FROM builder AS production-stage

# Forces tests to run every time:
COPY --from=test-stage /tmp/test-results.txt /tmp/test-results.txt
ENTRYPOINT ["/usr/local/bin/python", "-u", "main.py"]