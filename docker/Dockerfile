# syntax=docker/dockerfile:1
#
# Two targets from one file (build for x86 with: --platform linux/amd64):
#   * server  — slim runtime you deploy/expose (fastmcp + ripgrep, no ML).
#   * ingest  — heavy build-path image (Docling + tree-sitter [+ vector]) used
#               to (re)build the doc store. Run on demand, not exposed.
ARG PYTHON_VERSION=3.11

# --------------------------------------------------------------------------- #
FROM python:${PYTHON_VERSION}-slim AS base
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1
WORKDIR /app
COPY pyproject.toml README.md LICENSE NOTICE ./
COPY src ./src

# --------------------------------------------------------------------------- #
# Serving image: keyword search only — small surface, no torch/Docling.
FROM base AS server
RUN apt-get update \
    && apt-get install -y --no-install-recommends ripgrep \
    && rm -rf /var/lib/apt/lists/*
RUN pip install .
# Bind 0.0.0.0 *inside* the container; the port is never published — only the
# reverse proxy reaches it over the compose network (preserves "bind localhost").
ENV BIND_HOST=0.0.0.0 BIND_PORT=8080
EXPOSE 8080
CMD ["docmcp-server"]

# --------------------------------------------------------------------------- #
# Ingestion image: Docling (PDF/PPTX/DOCX/HTML) + tree-sitter (code) + vector.
# NOTE: default torch pulled here is the full wheel. For a leaner x86 image,
# prepend: RUN pip install torch --index-url https://download.pytorch.org/whl/cpu
FROM base AS ingest
RUN pip install ".[parse,vector]"
# Prefetch Docling's PDF models so the deployed host needs no internet (air-gapped).
ENV DOCLING_ARTIFACTS_PATH=/opt/docling/models \
    HF_HUB_OFFLINE=0
RUN docling-tools models download -o /opt/docling/models layout tableformer rapidocr
# OpenCV (pulled in by Docling's tableformer model) needs these native libraries
# at runtime; the slim base lacks them, so `import cv2` fails with
# "libxcb.so.1: cannot open shared object file" — which otherwise surfaces as
# PDFs silently never getting indexed. Installed after the model download to keep
# the heavy torch/model layers cached on rebuild.
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
       libgl1 libglib2.0-0 libsm6 libxext6 libxrender1 libxcb1 \
    && rm -rf /var/lib/apt/lists/*
ENTRYPOINT ["docmcp-ingest"]
