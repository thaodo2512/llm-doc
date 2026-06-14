"""Server-rendered HTML for the portal — one inline-CSS shell, every dynamic value
HTML-escaped, no external assets (offline), plain forms (no JS required)."""

from __future__ import annotations

from html import escape

_CSS = """
*{box-sizing:border-box} body{margin:0;background:#0f1320;color:#e7ecf6;
font:15px/1.55 ui-sans-serif,system-ui,-apple-system,Segoe UI,Roboto,Arial,sans-serif}
.wrap{max-width:880px;margin:0 auto;padding:28px 20px 70px}
a{color:#7aa2ff} h1{font-size:22px;margin:0 0 4px} h2{font-size:15px;margin:22px 0 8px;color:#9fb0d0}
.top{display:flex;justify-content:space-between;align-items:baseline;border-bottom:1px solid #243049;padding-bottom:12px}
.muted{color:#8a99b8;font-size:13px}
.card{background:#161c2e;border:1px solid #243049;border-radius:12px;padding:16px 18px;margin:14px 0}
input,select,button{font:inherit;border-radius:8px;border:1px solid #2c3a5a;background:#0e1422;color:#e7ecf6;padding:7px 10px}
button{background:#2f5fd0;border-color:#2f5fd0;cursor:pointer;font-weight:600}
button.danger{background:#3a1620;border-color:#7a2a3a;color:#ff9bab}
button.ghost{background:#0e1422}
form.inline{display:inline-flex;gap:6px;align-items:center;margin:0}
.chip{display:inline-block;background:#13203f;color:#7aa2ff;border:1px solid #2f5fd0;border-radius:99px;padding:3px 10px;font-size:12px;margin:2px}
.row{display:flex;flex-wrap:wrap;gap:10px;align-items:center;justify-content:space-between;
padding:10px 0;border-bottom:1px dashed #243049}
.row:last-child{border-bottom:none} .path{font-family:ui-monospace,Menlo,Consolas,monospace;font-size:13px}
.ok{background:#0f2a22;border:1px solid #1f6f54;color:#36d399;border-radius:8px;padding:9px 12px;margin:10px 0}
.err{background:#2c1620;border:1px solid #7a2a3a;color:#ff8aa0;border-radius:8px;padding:9px 12px;margin:10px 0}
.banner{background:#2b2113;border:1px solid #c97f17;color:#ffce86;border-radius:8px;padding:9px 12px;margin:10px 0;font-size:13px}
label{font-size:13px;color:#9fb0d0}
"""


def page(
    title: str, inner: str, *, user: str | None = None, insecure: bool = False, csrf: str | None = None
) -> str:
    head = (
        f'<div class="top"><div><h1>{escape(title)}</h1>'
        f'<div class="muted">Documentation hub — upload &amp; manage</div></div>'
    )
    if user is not None:
        csrf_field = f'<input type="hidden" name="csrf" value="{escape(csrf)}">' if csrf else ""
        head += (
            f'<form class="inline" method="post" action="/portal/logout">{csrf_field}'
            f'<span class="muted">{escape(user)}</span> '
            f'<button class="ghost" type="submit">Sign out</button></form>'
        )
    head += "</div>"
    warn = (
        '<div class="banner">⚠ Served over plain HTTP — your session cookie is not '
        "encrypted. Use only on a trusted network (VPN).</div>"
        if insecure
        else ""
    )
    return (
        "<!doctype html><html lang=en><head><meta charset=utf-8>"
        '<meta name=viewport content="width=device-width,initial-scale=1">'
        f"<title>{escape(title)}</title><style>{_CSS}</style></head><body><div class=wrap>"
        f"{head}{warn}{inner}</div></body></html>"
    )


def login_page(*, error: str | None = None, insecure: bool = False) -> str:
    err = f'<div class="err">{escape(error)}</div>' if error else ""
    inner = (
        f"{err}"
        '<div class="card"><h2>Sign in</h2>'
        '<p class="muted">Paste the bearer token your operator gave you (the same '
        "<code>tok_…</code> you use in Codex).</p>"
        '<form method="post" action="/portal/login">'
        '<input type="password" name="token" placeholder="tok_…" autofocus '
        'style="width:100%;margin-bottom:10px" autocomplete="off">'
        '<button type="submit">Sign in</button></form></div>'
    )
    return page("Sign in", inner, insecure=insecure)


def _folder_select(name: str, prefixes: list[str]) -> str:
    opts = "".join(f'<option value="{escape(p)}">{escape(p)}</option>' for p in prefixes)
    return f'<select name="{escape(name)}">{opts}</select>'


def dashboard(
    *,
    user: str,
    writable: list[str],
    files: list[str],
    csrf: str,
    message: str | None = None,
    error: str | None = None,
    insecure: bool = False,
) -> str:
    csrf_field = f'<input type="hidden" name="csrf" value="{escape(csrf)}">'
    msg = f'<div class="ok">{escape(message)}</div>' if message else ""
    err = f'<div class="err">{escape(error)}</div>' if error else ""

    if writable:
        chips = "".join(f'<span class="chip">{escape(p)}</span>' for p in writable)
        upload = (
            '<div class="card"><h2>Upload</h2>'
            '<form method="post" action="/portal/upload" enctype="multipart/form-data">'
            f"{csrf_field}"
            f'<label>Folder</label> {_folder_select("folder", writable)} '
            '<input type="file" name="file" multiple required> '
            '<button type="submit">Upload</button></form></div>'
        )
    else:
        chips = '<span class="muted">none — your token has no write access</span>'
        upload = ""

    rows = ""
    for path in files:
        rows += (
            '<div class="row">'
            f'<span class="path">{escape(path)}</span>'
            '<span style="display:flex;gap:8px;flex-wrap:wrap">'
            f'<form class="inline" method="post" action="/portal/rename">{csrf_field}'
            f'<input type="hidden" name="src" value="{escape(path)}">'
            '<input name="newname" placeholder="new name" size="14">'
            '<label class="muted"><input type="checkbox" name="replace"> overwrite</label>'
            '<button class="ghost" type="submit">Rename</button></form>'
            f'<form class="inline" method="post" action="/portal/move">{csrf_field}'
            f'<input type="hidden" name="src" value="{escape(path)}">'
            f'{_folder_select("folder", writable)}'
            '<label class="muted"><input type="checkbox" name="replace"> overwrite</label>'
            '<button class="ghost" type="submit">Move</button></form>'
            f'<form class="inline" method="post" action="/portal/delete">{csrf_field}'
            f'<input type="hidden" name="src" value="{escape(path)}">'
            '<label><input type="checkbox" name="confirm" required> </label>'
            '<button class="danger" type="submit">Delete</button></form>'
            "</span></div>"
        )
    files_card = (
        '<div class="card"><h2>Your files</h2>'
        + (rows or '<p class="muted">No files yet — upload one above.</p>')
        + "</div>"
    )

    inner = (
        f"{msg}{err}"
        f'<div class="card"><h2>You can publish to</h2>{chips}</div>'
        f"{upload}{files_card}"
    )
    return page("Documentation hub", inner, user=user, insecure=insecure, csrf=csrf)
