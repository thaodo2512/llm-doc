#!/usr/bin/env python3
"""Deterministic Markdown -> HTML renderer + report assembler for the doc-html-report skill.

Why this exists: the skill must turn the doc store's Markdown (often derived from
PDFs, with wide tables and long rows) into safe, structured HTML without the agent
hand-rolling a fragile parser per run. This script makes rendering deterministic and
escapes all content, so it never accidentally alters or injects markup.

Usage:
    python render.py manifest.json [out.html]      # or pipe the manifest on stdin
    cat manifest.json | python render.py - report.html

Manifest (JSON) — the agent fills `content` verbatim from read_doc, never summarized:
    {
      "title": "Internal Documentation",   # optional hero title
      "kicker": "Documentation Report",      # optional hero kicker
      "scope": "/",                          # optional scope label
      "company": "YOUR COMPANY",            # optional brand-bar label
      "confidential": "Confidential",        # optional footer-right label
      "date": "2026-06-09",                  # optional; defaults to today
      "docs": [
        {
          "title": "Flash Protocol",
          "type": "markdown",               # badge text
          "path": "/team-fw/flash.md",      # real doc path (for citation)
          "content": "<markdown from read_doc>",
          "truncated": false,                # show the server-capped warning
          "excerpt": false,                  # label as a topical excerpt (not full doc)
          "collapsed": false                 # start the <details> collapsed
        }
      ]
    }

It reuses the <style> from templates/report.html so styling stays in one place.
Supported Markdown: ATX headings, fenced code blocks, GFM pipe tables, ordered/
unordered lists, blockquotes, paragraphs, and inline code/bold/italic/links.
"""
from __future__ import annotations

import datetime
import html
import json
import os
import re
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
_TABLE_SEP = re.compile(r"^\s*\|?\s*:?-{2,}:?\s*(\|\s*:?-{2,}:?\s*)+\|?\s*$")


def esc(s: str) -> str:
    return html.escape(s, quote=True)


def _inline(text: str) -> str:
    """Inline Markdown -> HTML on a single line. Code spans are protected, then the
    text is escaped, then emphasis/links are applied to the escaped text (so a `<`
    in the source can never produce live markup)."""
    spans: list[str] = []

    def stash(m: "re.Match") -> str:
        spans.append(m.group(1))
        return "\x00%d\x00" % (len(spans) - 1)

    text = re.sub(r"`([^`]+)`", stash, text)
    text = esc(text)
    text = re.sub(r"\[([^\]]+)\]\(([^)\s]+)\)", r'<a href="\2">\1</a>', text)
    text = re.sub(r"\*\*([^*]+)\*\*", r"<strong>\1</strong>", text)
    text = re.sub(r"(?<!\*)\*(?!\*)([^*]+?)\*(?!\*)", r"<em>\1</em>", text)
    text = re.sub(r"(?<!\w)_([^_]+)_(?!\w)", r"<em>\1</em>", text)
    text = re.sub(r"\x00(\d+)\x00", lambda m: "<code>%s</code>" % esc(spans[int(m.group(1))]), text)
    return text


def _split_row(line: str) -> list[str]:
    return [c.strip() for c in line.strip().strip("|").split("|")]


def md_to_html(src: str) -> str:
    """Render the doc-store Markdown subset to escaped HTML."""
    lines = (src or "").replace("\r\n", "\n").replace("\r", "\n").split("\n")
    out: list[str] = []
    i, n = 0, len(lines)
    para: list[str] = []

    def flush_para() -> None:
        if para:
            out.append("<p>%s</p>" % _inline(" ".join(para)).strip())
            para.clear()

    while i < n:
        line = lines[i]
        stripped = line.strip()

        # fenced code block
        if stripped.startswith("```"):
            flush_para()
            i += 1
            code: list[str] = []
            while i < n and not lines[i].strip().startswith("```"):
                code.append(lines[i])
                i += 1
            i += 1  # consume closing fence
            out.append("<pre><code>%s</code></pre>" % esc("\n".join(code)))
            continue

        # GFM pipe table: a header line, then a separator line
        if "|" in line and i + 1 < n and _TABLE_SEP.match(lines[i + 1]):
            flush_para()
            header = _split_row(line)
            rows: list[list[str]] = []
            i += 2
            while i < n and "|" in lines[i] and lines[i].strip():
                rows.append(_split_row(lines[i]))
                i += 1
            cells = "".join("<th>%s</th>" % _inline(c) for c in header)
            body = ["<tr>%s</tr>" % cells]
            for r in rows:
                tds = "".join("<td>%s</td>" % _inline(c) for c in r)
                body.append("<tr>%s</tr>" % tds)
            # Wrap so a wide spec table scrolls instead of overflowing the page.
            out.append('<div class="md-table-wrap"><table>%s</table></div>' % "".join(body))
            continue

        # ATX heading
        m = re.match(r"^(#{1,6})\s+(.*)$", line)
        if m:
            flush_para()
            level = len(m.group(1))
            out.append("<h%d>%s</h%d>" % (level, _inline(m.group(2).strip()), level))
            i += 1
            continue

        # blockquote
        if stripped.startswith(">"):
            flush_para()
            quote: list[str] = []
            while i < n and lines[i].strip().startswith(">"):
                quote.append(re.sub(r"^\s*>\s?", "", lines[i]))
                i += 1
            out.append("<blockquote>%s</blockquote>" % _inline(" ".join(q.strip() for q in quote)))
            continue

        # lists (flat)
        if re.match(r"^\s*[-*+]\s+", line) or re.match(r"^\s*\d+\.\s+", line):
            flush_para()
            ordered = bool(re.match(r"^\s*\d+\.\s+", line))
            items: list[str] = []
            item_re = r"^\s*\d+\.\s+(.*)$" if ordered else r"^\s*[-*+]\s+(.*)$"
            while i < n and re.match(item_re, lines[i]):
                items.append(re.match(item_re, lines[i]).group(1))
                i += 1
            tag = "ol" if ordered else "ul"
            out.append("<%s>%s</%s>" % (tag, "".join("<li>%s</li>" % _inline(it) for it in items), tag))
            continue

        # blank line ends a paragraph
        if not stripped:
            flush_para()
            i += 1
            continue

        para.append(stripped)
        i += 1

    flush_para()
    return "\n".join(out)


def _load_style() -> str:
    tpl = os.path.join(HERE, "templates", "report.html")
    try:
        text = open(tpl, encoding="utf-8").read()
    except OSError:
        return "<style></style>"
    # Drop HTML comments first: the template's design note literally contains the
    # text "<style> block verbatim", which would otherwise be matched instead of
    # the real <style> element.
    text = re.sub(r"<!--.*?-->", "", text, flags=re.S)
    m = re.search(r"<style>.*?</style>", text, re.S)
    return m.group(0) if m else "<style></style>"


def render_report(manifest: dict) -> str:
    docs = manifest.get("docs") or []
    title = esc(manifest.get("title") or "Internal Documentation")
    kicker = esc(manifest.get("kicker") or "Documentation Report")
    scope = esc(str(manifest.get("scope") or "/"))
    company = esc(manifest.get("company") or "YOUR COMPANY")
    confidential = esc(manifest.get("confidential") or "Confidential")
    date = esc(manifest.get("date") or datetime.date.today().isoformat())
    year = (manifest.get("date") or "")[:4] or str(datetime.date.today().year)

    toc, sections = [], []
    for idx, d in enumerate(docs, 1):
        did = "doc-%d" % idx
        dtitle = esc(d.get("title") or d.get("path") or ("Document %d" % idx))
        dtype = esc(d.get("type") or "doc")
        dpath = esc(d.get("path") or "")
        toc.append('      <li><a href="#%s">%s</a> <span class="badge">%s</span></li>' % (did, dtitle, dtype))
        notice = ""
        if d.get("truncated"):
            notice = '<p class="truncated">Content may be incomplete because the server capped this read.</p>'
        elif d.get("excerpt"):
            notice = ('<p class="truncated">Topical excerpt &mdash; selected sections around the '
                      'matches, not the full document.</p>')
        open_attr = "" if d.get("collapsed") else " open"
        sections.append(
            '  <section class="doc" id="%s">\n'
            "    <details%s>\n"
            "      <summary>\n"
            '        <span class="doc-title">%s</span>\n'
            '        <span class="badge">%s</span>\n'
            '        <span class="doc-path">%s</span>\n'
            "      </summary>\n"
            '      <div class="doc-body">\n%s%s\n      </div>\n'
            "    </details>\n"
            "  </section>" % (did, open_attr, dtitle, dtype, dpath, notice, md_to_html(d.get("content") or ""))
        )

    return (
        "<!doctype html>\n<html lang=\"en\">\n<head>\n<meta charset=\"utf-8\">\n"
        '<meta name="viewport" content="width=device-width, initial-scale=1">\n'
        "<title>%s</title>\n%s\n</head>\n<body>\n" % (title, _load_style())
        + '<div class="page" id="top">\n'
        + '  <div class="brandbar">\n    <div class="logo"><span class="mark"></span> %s</div>\n'
          '    <div class="partner">Documentation</div>\n  </div>\n' % company
        + '  <header class="hero">\n    <div class="kicker">%s</div>\n    <h1>%s</h1>\n'
          '    <div class="sub">Generated <b>%s</b> &middot; <b>%d</b> documents &middot; scope <b>%s</b></div>\n'
          "  </header>\n" % (kicker, title, date, len(docs), scope)
        + '  <nav class="toc">\n    <h2>Contents</h2>\n    <ol>\n%s\n    </ol>\n  </nav>\n' % "\n".join(toc)
        + "\n".join(sections) + "\n"
        + '  <footer class="report">\n    <span>&copy; %s %s &middot; Internal documentation</span>\n'
          "    <span>%s</span>\n  </footer>\n" % (year, company, confidential)
        + '</div>\n<a class="top" href="#top">&uarr; Top</a>\n</body>\n</html>\n'
    )


def main(argv: list[str]) -> int:
    src = argv[1] if len(argv) > 1 else "-"
    raw = sys.stdin.read() if src == "-" else open(src, encoding="utf-8").read()
    manifest = json.loads(raw)
    out_html = render_report(manifest)
    if len(argv) > 2:
        with open(argv[2], "w", encoding="utf-8") as fh:
            fh.write(out_html)
        print(argv[2])
    else:
        sys.stdout.write(out_html)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
