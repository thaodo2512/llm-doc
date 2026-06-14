# Authoring documentation for the MCP server

How to write and organize docs so coding agents (Codex, Claude Code) **find and cite
the right thing**. The server is keyword-first: an agent calls `list_docs` to see what
exists, `search_docs` with exact terms, then `read_doc` the best hit and cites its
path. Docs that *contain the terms people search* and are *easy to navigate* retrieve
well; large unstructured blobs don't.

> TL;DR: one topic per Markdown file, a clear `#` title, the **exact terms** people
> search (product/service/repo names, commands, config keys, error strings), a short
> `index.md` per top-level folder, and a Markdown summary in front of any big PDF.

---

## 1. Folder layout = your access boundaries

Top-level folders under `raw/` become the logical prefixes (`/public`, `/team-fw`, …)
that **RBAC is granted on**. A token scoped to `/team-fw` can read everything under it
and nothing else. So organize by *who should see what*, then by area:

```
raw/
├── public/            # everyone with a token can read
│   ├── index.md
│   └── onboarding.md
└── team-fw/           # only /team-fw-scoped tokens
    ├── index.md
    ├── flashing.md
    └── specs/DSP0240.pdf
```

The curated store mirrors this exactly (`raw/team-fw/flashing.md` → `/team-fw/flashing.md`),
so the path an agent cites is predictable. **Keep a doc close to the thing it
describes** — `/team-fw/flashing.md`, not `/misc/notes-7.md`.

## 2. One `index.md` per top-level folder

A short index per area gives the agent (and humans) a map, and seeds search with the
right vocabulary. Drop this in each top-level folder as `index.md`:

```markdown
# Firmware (team-fw)

What lives here and the terms to search for.

- [Flashing runbook](flashing.md) — flash a device, recover a bricked unit
  (terms: `dfu`, `bootloader`, `flash`, `J-Link`, error `E_FLASH_TIMEOUT`)
- [DSP0240 spec](specs/DSP0240.pdf.md) — PLDM base spec (terms: `PLDM`, `MCTP`)

Contacts: #firmware-help · owner: @jane
```

Keep it to a screen. List each doc with a one-line "what + when" and the **literal
terms** someone would grep for.

## 3. Write so search finds it

Retrieval is literal substring/phrase matching (ripgrep/FTS5), so the words matter:

- **Give every file a `#` title on line 1.** The first heading becomes the doc's
  title in `list_docs` — make it specific (`# Firmware flashing runbook`, not `# Notes`).
- **Use headings** (`##`, `###`) for sub-topics so an agent can request a narrow line
  range instead of the whole file.
- **Include the exact terms people search**, verbatim: product/service/repo names,
  CLI commands, config keys (`DEPLOY_TOKEN`, `max_retries`), error strings
  (`ECONNREFUSED`, `E_FLASH_TIMEOUT`), API/symbol names. If a term has variants, name
  them once ("also called …").
- **One topic per file.** Many small, well-titled files beat one giant file — they
  retrieve precisely and cite cleanly.
- **Spell out acronyms once** next to the acronym so both forms are searchable.

## 4. Prefer Markdown; put a summary in front of big PDFs

The ingester converts PDF/DOCX/PPTX/HTML (and OCRs scanned pages), but conversion is
lossy and large blobs retrieve poorly. So:

- For knowledge **you author**, write **Markdown** — it diffs in git, converts 1:1, and
  ranks best.
- Keep **authoritative PDFs/specs** (you can't rewrite a standard), but add a short
  Markdown **summary + pointer** next to them so agents find the concept, then open the
  spec for the exact wording:

  ```markdown
  # PLDM base spec (DSP0240) — summary
  Defines PLDM over MCTP: message types, completion codes, discovery.
  Full normative text: [DSP0240.pdf](DSP0240.pdf.md). Search terms: PLDM, MCTP,
  completion code, PLDM_BASE.
  ```

- Avoid scanned-image PDFs where a text version exists — OCR is best-effort.

## 5. Naming

- Descriptive, kebab-case filenames: `flashing-runbook.md`, not `doc1.md`. The filename
  shows in the path an agent cites.
- Converted files get `.md` appended to the curated path (`design.pdf` →
  `/team-fw/design.pdf.md`) so a PDF and a DOCX of the same name can't collide.

## 6. Publish & verify

```bash
./docmcp.sh add /path/to/new/docs    # stage into raw/ (then: git add raw/ && git commit)
./docmcp.sh ingest                    # build the store (unchanged files are skipped)
./docmcp.sh inventory                 # see the corpus by type + folder — check coverage
./docmcp.sh doctor                    # index valid? last ingest clean?
```

Then confirm an agent can find it: in Codex, *"search our docs for `E_FLASH_TIMEOUT`
and cite the path"* should return your new doc. (See the README's "Using the docs from
Codex" section.)

## Checklist

- [ ] In the right top-level folder (matches who should read it)
- [ ] Specific `#` title on line 1; `##` headings for sub-topics
- [ ] Contains the exact terms people will search (commands, keys, errors, names)
- [ ] One topic per file; Markdown (or a Markdown summary in front of a PDF)
- [ ] Linked from the folder's `index.md`
- [ ] `./docmcp.sh ingest` clean; findable via `search_docs`
