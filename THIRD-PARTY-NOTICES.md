# Third-Party Notices

This product — **docmcp** (the Documentation MCP Server) — is licensed under the
**Apache License, Version 2.0**. See the [`LICENSE`](./LICENSE) and [`NOTICE`](./NOTICE)
files for the full text and attribution.

docmcp bundles and/or depends on the third-party components listed below. To the
best of our determination, **every bundled or runtime-required component is offered
under a permissive open-source license** (Apache-2.0, MIT, BSD-2/3-Clause, ISC, PSF,
Unlicense, HPND, or CDLA-Permissive-2.0). The few weak-copyleft or
data/build-only exceptions (`certifi` = MPL-2.0; `lightningcss` = MPL-2.0 and
`caniuse-lite` = CC-BY-4.0, both build-time only) are called out explicitly in
their sections. NVIDIA CUDA / cuDNN runtime wheels appear in the locked dependency
graph only as conditional, non-`darwin` resolution targets; **they are not bundled**
in the published images (see section 6).

The lists below are derived from the resolved lockfile (`uv.lock`,
182 third-party Python packages), the vendored model directories under `models/`,
the frontend lockfile (`console-ui/package-lock.json`), and the container
definitions under `docker/`. Per-package license text is not reproduced in full
here; it ships inside each installed distribution (the `*.dist-info/` `METADATA`
and license files) and inside each vendored model directory as noted.

---

## 1. Overview

| Layer | What it is | License posture |
|-------|-----------|-----------------|
| docmcp | This project | Apache-2.0 |
| Vendored model weights (`models/`) | 4 pre-trained model sets, redistributed unmodified | MIT / Apache-2.0 / CDLA-Permissive-2.0 |
| Python runtime dependencies (`uv.lock`) | server + `[parse]` + `[vector]` extras | Permissive (MIT/BSD/Apache/ISC/PSF/Unlicense); `certifi` is MPL-2.0 |
| Frontend / build tooling (`console-ui/`) | React SPA + Vite/Tailwind toolchain | MIT/ISC/BSD/Apache; `lightningcss` MPL-2.0 and `caniuse-lite` CC-BY-4.0 are build-time only |
| Container base images (`docker/`) | python-slim, caddy, qdrant, docker CLI | PSF+Debian / Apache-2.0 |

---

## 2. Vendored model weights

The following pre-trained model weights are redistributed **unmodified** under
`models/` (via Git LFS) so the ingestion image can build and run fully offline.

| Model set | Upstream | License | Where the full license text lives |
|-----------|----------|---------|-----------------------------------|
| BGE small English embedding model (semantic search) | `BAAI/bge-small-en-v1.5` | **MIT** | [`models/bge-small-en-v1.5/LICENSE`](./models/bge-small-en-v1.5/LICENSE) (vendored alongside the weights) |
| Docling layout model | `docling-project/docling-layout-heron` | **Apache-2.0** | Root [`LICENSE`](./LICENSE) of this repo (Apache-2.0) |
| Docling TableFormer table-structure models | `docling-project/docling-models` | **CDLA-Permissive-2.0** | [`models/docling-project--docling-models/LICENSE`](./models/docling-project--docling-models/LICENSE) (full CDLA-2.0 text vendored alongside the weights) |
| RapidOCR PP-OCRv4 OCR models — text detection / classification / recognition (ONNX, from `RapidAI/RapidOCR`, PaddleOCR PP-OCRv4 weights) | `RapidAI/RapidOCR` / PaddleOCR (Baidu) | **Apache-2.0** | [`models/RapidOcr/LICENSE`](./models/RapidOcr/LICENSE) + attribution in [`models/RapidOcr/NOTICE`](./models/RapidOcr/NOTICE) |

Attribution for all four sets is consolidated in the repo-root [`NOTICE`](./NOTICE) file.

---

## 3. Python dependencies (grouped by license)

The resolved dependency graph in `uv.lock` contains **182 third-party packages**
(excluding the `docmcp` project package itself), spanning the base server install
plus the `[parse]`, `[vector]`, and `[vector-openai]` extras. The full license text
for each ships inside its installed distribution's `*.dist-info/` directory.

### Apache-2.0
`accelerate`, `aiofile`, `caio`,
`cyclopts`, `docling-parse`, `fastmcp`, `fastmcp-slim`, `filelock`, `grpcio`,
`hf-xet`, `huggingface-hub`, `importlib-metadata`, `jsonschema-path`, `mail-parser`,
`mpire`, `multiprocess` (BSD-3 in some releases; Apache/BSD-permissive),
`opentelemetry-api`, `pathable`, `portalocker`, `py-key-value-aio`, `pypdfium2`
(Apache-2.0 OR BSD-3-Clause; bundled PDFium is BSD-3), `pyperclip` (BSD),
`python-multipart`, `pytest-asyncio`, `qdrant-client`, `rapidocr`,
`rapidocr-onnxruntime` (extra), `safetensors`, `tokenizers`, `transformers`,
`tqdm`→`tqdm` (MPL-2.0 OR MIT; treated as MIT), `watchfiles` (MIT — listed here for
the Rust/maturin toolchain note only)

> Note: `cuda-bindings`, `cuda-pathfinder`, `cuda-toolkit`, and the `nvidia-*` family
> are NVIDIA-licensed (not Apache) and appear only as conditional
> (`sys_platform != 'darwin'`) CUDA-build resolution targets. They are **not
> installed** in the published images, which pin CPU-only `torch`/`torchvision`
> (section 6).

### MIT
`annotated-doc`, `annotated-types`, `anyio`, `attrs`, `backports-tarfile`,
`beartype`, `cachetools`, `cffi`, `colorlog`, `dill`, `distro`, `docling`,
`docling-core`, `docling-ibm-models`, `docling-slim`, `docstring-parser`,
`et-xmlfile`, `exceptiongroup`, `faker`, `filetype`, `fsspec`, `h11`, `h2`, `hpack`,
`httpx-sse`, `hyperframe`, `iniconfig`, `jaraco-classes`, `jaraco-context`,
`jaraco-functools`, `jinja2`, `jiter`, `jsonlines`, `jsonref`, `jsonschema`,
`jsonschema-specifications`, `keyring`, `latex2mathml`, `lxml` (BSD-3),
`markdown-it-py`, `marko`, `markupsafe` (BSD-3), `mcp`, `mdurl`, `more-itertools`,
`omegaconf` (BSD-3), `onnxruntime` (extra), `openai`, `openapi-pydantic`,
`opencv-python` (MIT wrapper; OpenCV core Apache-2.0), `openpyxl`, `pillow`
(MIT-CMU/HPND), `platformdirs`, `pluggy`, `polyfactory`, `psutil` (BSD-3),
`pyclipper`, `pydantic`, `pydantic-core`, `pydantic-settings`, `pyjwt`,
`pylatexenc`, `pyperclip` (BSD), `pytest`, `python-docx`, `python-dotenv` (BSD-3),
`python-pptx`, `pyyaml`, `referencing`, `regex` (Apache-2.0), `rich`, `rich-rst`,
`rpds-py`, `rtree`, `safetensors` (Apache-2.0), `secretstorage` (BSD-3), `semchunk`,
`setuptools`, `shellingham` (ISC), `sniffio`, `tabulate`, `tree-sitter`,
`tree-sitter-c`, `tree-sitter-javascript`, `tree-sitter-language-pack`,
`tree-sitter-python`, `tree-sitter-typescript`, `triton`, `typer`,
`typing-inspection`, `tzdata` (Apache-2.0), `uncalled-for`, `xlsxwriter` (BSD-3),
`zipp`

### BSD (2-Clause / 3-Clause)
`antlr4-python3-runtime`, `authlib`, `beautifulsoup4` (MIT), `click`, `httpcore`,
`httpx`, `idna`, `joserfc`, `mpmath`, `networkx`, `numpy`, `pandas`,
`protobuf`, `pycparser`, `pygments` (BSD-2), `python-dateutil` (Apache-2.0 OR BSD),
`scipy`, `shapely`, `soupsieve`, `sse-starlette`, `starlette`, `sympy`, `torch`,
`torchvision`, `uvicorn`, `websockets`

### ISC
`dnspython`, `griffelib`, `jeepney`

### PSF / Python Software Foundation
`typing-extensions`

### Unlicense (public domain)
`email-validator`

### Apache-2.0 OR BSD (dual permissive)
`cryptography` (Apache-2.0 OR BSD-3-Clause), `packaging` (Apache-2.0 OR BSD-2-Clause)

### MPL-2.0 (weak copyleft — file-level)
- **`certifi`** — Mozilla Public License 2.0. `certifi` is a leaf dependency that
  ships a curated bundle of Mozilla's root CA certificates. MPL-2.0 is a
  file-level weak copyleft license; using it as an unmodified dependency imposes no
  obligation on docmcp's own source. **No `certifi` source is modified.**

### Charset / encoding
`charset-normalizer` — MIT. (Note: docmcp does **not** depend on `chardet`, avoiding
its LGPL terms.)

> **Non-permissive / proprietary, NOT bundled:** the `nvidia-*` CUDA runtime
> wheels — `nvidia-cublas`, `nvidia-cuda-cupti`, `nvidia-cuda-nvrtc`,
> `nvidia-cuda-runtime`, `nvidia-cudnn-cu13`, `nvidia-cufft`, `nvidia-cufile`,
> `nvidia-curand`, `nvidia-cusolver`, `nvidia-cusparse`, `nvidia-cusparselt-cu13`,
> `nvidia-nccl-cu13`, `nvidia-nvjitlink`, `nvidia-nvshmem-cu13`, `nvidia-nvtx` —
> plus `cuda-bindings`, `cuda-pathfinder`, and `cuda-toolkit` are governed by the
> **NVIDIA proprietary CUDA EULA**, not an OSI-approved license. They appear in
> `uv.lock` only as conditional (`sys_platform != 'darwin'`) targets reachable via
> the default GPU `torch` build. The published images install **CPU-only**
> `torch`/`torchvision` and pin them, so none of these wheels are present in any
> shipped artifact (see section 6).

---

## 4. Frontend / build tooling (`console-ui/`)

The console SPA is built from React + Vite + Tailwind. Its runtime bundle
(`console-ui/dist/`) and the build toolchain resolve (`console-ui/package-lock.json`)
to permissive licenses:

| Package | Role | License |
|---------|------|---------|
| `react`, `react-dom`, `react-router-dom` | Runtime UI (shipped in the SPA bundle) | MIT |
| `typescript` | Build-time compiler | Apache-2.0 |
| `vite`, `@vitejs/plugin-react` | Build-time bundler/plugin | MIT |
| `tailwindcss`, `@tailwindcss/vite` | Build-time CSS | MIT |
| `@types/react`, `@types/react-dom` | Build-time type stubs (DefinitelyTyped) | MIT |

The vast majority of transitive dev/build dependencies are **MIT**, with a smaller
number under **ISC**, **BSD-2/3-Clause**, and **Apache-2.0**.

**Build-time-only exceptions (not shipped in the SPA runtime bundle):**

- **`lightningcss`** (pulled in by `@tailwindcss/vite`) — **MPL-2.0**. It is a
  Rust-based CSS transformer used at build time only; no `lightningcss` source is
  redistributed in the application bundle.
- **`caniuse-lite`** (browserslist data) — **CC-BY-4.0**. This is a build-time
  browser-compatibility **data** package, not application code, and is not shipped
  in the runtime bundle.

---

## 5. Container base images (`docker/`)

| Image | Used for | License |
|-------|----------|---------|
| `python:3.11-slim` | Base for all server/ingest images | **PSF License** (Python) over a **Debian** base (individual Debian packages under their own DFSG-compatible licenses — mostly GPL/LGPL/MIT/BSD as system libraries, used unmodified) |
| `caddy:2` | Reverse proxy / TLS termination | **Apache-2.0** |
| `qdrant/qdrant:latest` | Vector database (optional `[vector]` layer) | **Apache-2.0** |
| `docker:27-cli` | Source of the static `docker` CLI + compose plugin copied into the console image | **Apache-2.0** |

---

## 6. CPU-only torch — no NVIDIA-proprietary CUDA wheels bundled

The `[parse]` ingestion image installs **CPU-only** `torch` and `torchvision` from
the official PyTorch CPU index (`https://download.pytorch.org/whl/cpu`) and pins
those exact versions as an install constraint, so **no NVIDIA-proprietary CUDA/cuDNN
wheels (`nvidia-*`, `cuda-*`) are pulled in or bundled** in any published image.
