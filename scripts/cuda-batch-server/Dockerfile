FROM nvidia/cuda@sha256:520292dbb4f755fd360766059e62956e9379485d9e073bbd2f6e3c20c270ed66 AS tools
ENV DEBIAN_FRONTEND=noninteractive PIP_DISABLE_PIP_VERSION_CHECK=1 PYTHONUNBUFFERED=1
RUN apt-get update && apt-get install -y --no-install-recommends python3 python3-venv \
    ca-certificates g++ ninja-build cuda-nsight-compute-12-8 cuda-nsight-systems-12-8 \
    && rm -rf /var/lib/apt/lists/*
RUN python3 -m venv /opt/venv
ENV PATH=/opt/venv/bin:/usr/local/cuda/bin:$PATH
RUN pip install --no-cache-dir torch==2.9.1 --index-url https://download.pytorch.org/whl/cu128
RUN pip install --no-cache-dir numpy==2.2.6 gguf==0.19.0 tokenizers==0.23.3 \
    uvicorn==0.54.0 httpx==0.28.1 pytest==9.2.0
COPY fetch_cutlass.py /tmp/fetch_cutlass.py
RUN python /tmp/fetch_cutlass.py --destination /opt/cutlass && rm /tmp/fetch_cutlass.py
WORKDIR /app
COPY . /app
RUN python build_kernels.py && python -m unittest discover -s /app -v
ENV OMP_NUM_THREADS=2 TOKENIZERS_PARALLELISM=false
USER 1000:1000
ENTRYPOINT ["python", "server.py"]

FROM ubuntu@sha256:fe81560990fae86dc4743202b2cb4d3df98d0bcf949173cecef15ce1127ef852 AS runtime
ENV DEBIAN_FRONTEND=noninteractive PYTHONUNBUFFERED=1 OMP_NUM_THREADS=2 TOKENIZERS_PARALLELISM=false \
    PATH=/opt/venv/bin:$PATH NVIDIA_DRIVER_CAPABILITIES=compute,utility
RUN apt-get update && apt-get install -y --no-install-recommends python3 libgomp1 ca-certificates \
    && rm -rf /var/lib/apt/lists/*
COPY --from=tools /opt/venv /opt/venv
WORKDIR /app
COPY --from=tools /app/libembed.so /app/librerank.so /app/libsmall.so /app/
COPY --from=tools /app/compiler-report /app/compiler-report
COPY --from=tools /opt/cutlass/LICENSE.txt /app/third_party/CUTLASS-LICENSE.txt
COPY backend.py scheduler.py server.py /app/
ARG GIT_SHA=development
ENV GIT_SHA=$GIT_SHA
LABEL org.opencontainers.image.source=https://github.com/dbsanfte/eq-archives-software \
      org.opencontainers.image.revision=$GIT_SHA
USER 1000:1000
ENTRYPOINT ["python", "server.py"]
