# No `# syntax=` directive on purpose: this Dockerfile uses only built-in frontend
# features, so BuildKit's bundled frontend builds it WITHOUT fetching docker/dockerfile
# from Docker Hub — one fewer network dependency on a locked-down or slow-registry host
# (that fetch fails first with a TLS handshake timeout when Docker Hub is unreachable).
# Note: the base image, apt, and pip still need network; only the ML models under
# models/ are vendored for offline use.
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
FROM base AS ingest
# CPU-only torch + torchvision (no proprietary NVIDIA CUDA wheels): install them
# from the PyTorch CPU index, then PIN those exact versions as a constraint so the
# `.[parse,vector]` install (Docling pulls torch/torchvision) can't re-resolve them
# back to the default CUDA wheels off PyPI. Pinning is required — without it pip
# silently upgrades torch to the +cuXXX build and drags in ~15 nvidia-* wheels.
RUN pip install torch torchvision --index-url https://download.pytorch.org/whl/cpu \
 && pip freeze | grep -iE '^(torch|torchvision)==' > /tmp/torch-cpu.txt \
 && pip install ".[parse,vector]" -c /tmp/torch-cpu.txt \
 && rm -f /tmp/torch-cpu.txt
# Models are vendored in the repo under models/ (via Git LFS) and copied in at
# build time — no network is used for models. DOCLING_ARTIFACTS_PATH points at the
# parent dir holding the repo-named folders; HF_HUB_OFFLINE forbids any Hub calls.
ENV DOCLING_ARTIFACTS_PATH=/opt/docling/models \
    HF_HUB_OFFLINE=1 \
    HF_HUB_DISABLE_TELEMETRY=1 \
    DO_NOT_TRACK=1
COPY models /opt/docling/models
# Fail the build if any vendored model is still an un-materialized Git LFS pointer
# (a clone without `git lfs pull`). Otherwise Docling json.loads() a pointer text
# file at ingest time and dies with a cryptic
#   JSONDecodeError: Expecting value: line 1 column 1 (char 0)
# on PDFs with tables/OCR. `grep -I` skips the large binary weights; pointer files
# are small text starting with "version https://git-lfs.github.com/spec/...".
RUN if grep -rIl 'git-lfs.github.com/spec' /opt/docling/models >/dev/null 2>&1; then \
        echo 'ERROR: models/ contains un-materialized Git LFS pointers — run "git lfs install && git lfs pull" before building. Offending files:' >&2; \
        grep -rIl 'git-lfs.github.com/spec' /opt/docling/models 2>/dev/null | sed 's,^/opt/docling/models/,  models/,' >&2; \
        exit 1; \
    fi
# OpenCV (pulled in by Docling's tableformer model) needs these native libraries
# at runtime; the slim base lacks them, so `import cv2` fails with
# "libxcb.so.1: cannot open shared object file" — which otherwise surfaces as
# PDFs silently never getting indexed. Installed after the model COPY so the
# heavy torch + model layers stay cached on rebuild.
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
       libgl1 libglib2.0-0 libsm6 libxext6 libxrender1 libxcb1 \
    && rm -rf /var/lib/apt/lists/*
ENTRYPOINT ["docmcp-ingest"]
