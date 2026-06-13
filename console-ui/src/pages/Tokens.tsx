import { useState } from "react";
import { api, type TokenRow } from "../api";
import { Alert, CopyButton, Field, FullSpinner, PageTitle, useAsync } from "../components/ui";

function splitList(s: string): string[] {
  return s.split(/[\s,]+/).map((x) => x.trim()).filter(Boolean);
}
function fmtExpiry(t: TokenRow) {
  if (t.expires_at == null) return "never";
  const d = new Date(t.expires_at * 1000).toLocaleDateString();
  return t.expired ? `expired (${d})` : d;
}

export function Tokens() {
  const list = useAsync(() => api.tokens(), []);
  const [form, setForm] = useState({ user: "", prefixes: "", groups: "", writes: "", expires: "90d", comment: "", admin: false });
  const [minted, setMinted] = useState<string | null>(null);
  const [msg, setMsg] = useState<{ kind: "ok" | "err"; text: string } | null>(null);
  const [busy, setBusy] = useState(false);

  const set = (k: string, v: any) => setForm((f) => ({ ...f, [k]: v }));

  const mint = async (e: React.FormEvent) => {
    e.preventDefault();
    setBusy(true);
    setMsg(null);
    setMinted(null);
    try {
      const body: Record<string, unknown> = { user: form.user.trim(), expires: form.expires.trim() || undefined };
      if (form.comment.trim()) body.comment = form.comment.trim();
      if (form.admin) body.admin = true;
      else {
        body.prefixes = splitList(form.prefixes);
        body.groups = splitList(form.groups);
        body.writes = splitList(form.writes);
      }
      const r = await api.mintToken(body);
      if (r.ok && r.token) {
        setMinted(r.token);
        setForm((f) => ({ ...f, user: "", prefixes: "", groups: "", writes: "", comment: "" }));
        list.reload();
      } else {
        setMsg({ kind: "err", text: r.output || "mint failed" });
      }
    } catch (e: any) {
      setMsg({ kind: "err", text: e.message });
    } finally {
      setBusy(false);
    }
  };

  const act = async (fn: () => Promise<any>, label: string) => {
    if (!confirm(`${label}?`)) return;
    setMsg(null);
    try {
      const r = await fn();
      setMsg({ kind: r.ok ? "ok" : "err", text: r.ok ? `${label} ✓` : r.output });
      if (r.token) setMinted(r.token);
      list.reload();
    } catch (e: any) {
      setMsg({ kind: "err", text: e.message });
    }
  };

  return (
    <div>
      <PageTitle title="Tokens" subtitle="Mint, revoke, and rotate scoped bearer tokens" />

      {minted && (
        <Alert kind="ok">
          <div className="mb-1 font-medium">New token — copy it now, it is shown only once:</div>
          <div className="flex items-center gap-2">
            <code className="mono break-all rounded bg-bg2 px-2 py-1 text-ink">{minted}</code>
            <CopyButton text={minted} />
          </div>
        </Alert>
      )}
      {msg && <Alert kind={msg.kind}>{msg.text}</Alert>}

      <div className="card">
        <h2 className="mb-3 font-semibold">Mint a token</h2>
        <form onSubmit={mint} className="grid gap-3 md:grid-cols-2">
          <Field label="User"><input className="input" value={form.user} onChange={(e) => set("user", e.target.value)} placeholder="alice" /></Field>
          <Field label="Expires" hint="Nd · Nh · Nm · never"><input className="input" value={form.expires} onChange={(e) => set("expires", e.target.value)} /></Field>
          {!form.admin && (
            <>
              <Field label="Read prefixes" hint="space/comma separated, e.g. /public /team/a"><input className="input mono" value={form.prefixes} onChange={(e) => set("prefixes", e.target.value)} /></Field>
              <Field label="Groups" hint="optional group names"><input className="input mono" value={form.groups} onChange={(e) => set("groups", e.target.value)} /></Field>
              <Field label="Write prefixes" hint="optional portal upload scope"><input className="input mono" value={form.writes} onChange={(e) => set("writes", e.target.value)} /></Field>
            </>
          )}
          <Field label="Comment"><input className="input" value={form.comment} onChange={(e) => set("comment", e.target.value)} /></Field>
          <label className="flex items-center gap-2 text-sm text-ink2">
            <input type="checkbox" checked={form.admin} onChange={(e) => set("admin", e.target.checked)} />
            Admin (whole-corpus <span className="mono">--all</span>) — break-glass
          </label>
          <div className="md:col-span-2">
            <button className="btn" disabled={busy || !form.user.trim()}>{busy ? "Minting…" : "Mint token"}</button>
          </div>
        </form>
      </div>

      <div className="card mt-4">
        <div className="mb-2 flex items-center justify-between">
          <h2 className="font-semibold">Tokens</h2>
          <button className="btn btn-ghost" onClick={() => list.reload()}>Refresh</button>
        </div>
        {list.loading ? (
          <FullSpinner />
        ) : !list.data?.tokens.length ? (
          <p className="text-sm text-muted">No tokens yet.</p>
        ) : (
          <div className="overflow-x-auto">
            <table className="w-full text-sm">
              <thead className="text-left text-xs uppercase tracking-wide text-muted">
                <tr>
                  <th className="py-2 pr-3">User</th>
                  <th className="py-2 pr-3">Read</th>
                  <th className="py-2 pr-3">Write</th>
                  <th className="py-2 pr-3">Expires</th>
                  <th className="py-2 pr-3">Token</th>
                  <th className="py-2"></th>
                </tr>
              </thead>
              <tbody>
                {list.data.tokens.map((t, i) => (
                  <tr key={i} className="border-t border-line align-top">
                    <td className="py-2 pr-3 font-medium">{t.user}</td>
                    <td className="py-2 pr-3">{t.read.map((p) => <span key={p} className="chip mr-1 mb-1">{p}</span>) || "—"}</td>
                    <td className="py-2 pr-3">{t.write.length ? t.write.map((p) => <span key={p} className="chip mr-1 mb-1">{p}</span>) : <span className="text-muted">—</span>}</td>
                    <td className="py-2 pr-3">{t.expired ? <span className="badge-bad">{fmtExpiry(t)}</span> : fmtExpiry(t)}</td>
                    <td className="py-2 pr-3 mono text-muted">{t.id}</td>
                    <td className="py-2 text-right">
                      <span className="flex justify-end gap-2">
                        <button className="btn btn-ghost" onClick={() => act(() => api.rotateToken(t.user), `Rotate ${t.user}`)}>Rotate</button>
                        <button className="btn btn-danger" onClick={() => act(() => api.revokeToken(t.user), `Revoke all of ${t.user}`)}>Revoke</button>
                      </span>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </div>
    </div>
  );
}
