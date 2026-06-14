---
name: doc-find
description: >-
  Locate a document or the exact section from a fuzzy, half-remembered human
  description rather than exact keywords. Use when the user does not know the
  precise term and asks things like "I remember there's something that defines
  multipart transfer", "where do we describe the flashing retry logic", or "find
  the section about token expiry — help me fix it". Trigger words: I remember,
  somewhere, find the doc, which doc, where is, what defines, the part about,
  roughly about.
---
Prerequisite: the `docs` MCP server is connected (`list_docs`, `search_docs`,
`read_doc`; `semantic_search` only if the server has vector search enabled).
`search_docs` is keyword/full-text, so the whole job is to bridge the user's
vague wording to the LITERAL terms the corpus uses — never run a single fuzzy
query and give up. You only ever see docs the caller's token allows.

1. Restate in one line the concept the user is after, then EXPAND it into a set
   of candidate literal terms the corpus is likely to contain:
   - normalized spelling/spacing ("multiple part transfer" → "multipart
     transfer", "multipart", "multi-part");
   - domain/spec jargon, symbols, acronyms and their expansions (e.g. "transfer
     handle", "transfer flag", "MultipartReceive", "PLDM");
   - the words a section HEADING would likely use.
2. Call `list_docs` first (the whole index, or the prefix the user named) to see
   what actually exists and to learn the corpus vocabulary/areas before searching.
3. If `semantic_search` is available, call it once with the user's natural-language
   description. If it returns a disabled error, ignore it and rely on step 4.
4. Run `search_docs` once PER candidate term from step 1 — several narrow queries,
   not one vague one. Collect every `{path, line, snippet, score}` hit.
5. Merge and dedupe the hits; rank by how many distinct queries landed on the same
   doc/section and by score. Pick the top 1–3 candidate locations.
6. `read_doc` a focused line range around each top hit to CONFIRM it matches the
   description and to pin the exact heading/section. (`read_doc` returns raw
   `content` with NO line numbers — use it only to confirm and quote the section,
   not to derive a line.) If nothing matches, refine the terms (synonyms, broader
   or narrower) and repeat steps 4–6 up to ~3 rounds.
7. Report: the doc `path`, the section/heading, and the line number **from the
   matching `search_docs` hit** (cite `path:line` using that hit's `line` — never
   invent one from `read_doc`), plus a one-line "why this matches". Always cite. If
   the user asked you to fix or act on it, work from the CONFIRMED text you just
   read — never from memory — and quote the exact section.
8. If nothing matches confidently after refining, say so plainly, list the closest
   candidate paths, and suggest the exact terms to try next (or that the operator
   enable semantic search for concept-level recall).
