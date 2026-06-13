import { useState } from "react";
import { api } from "../api";
import { Alert, FullSpinner, PageTitle, useAsync } from "../components/ui";

export function Access() {
  const tree = useAsync(() => api.accessTree(), []);
  const [user, setUser] = useState("");
  const [path, setPath] = useState("");
  const [result, setResult] = useState<{ result: string; scope?: string[] } | null>(null);
  const [err, setErr] = useState("");

  const check = async (e: React.FormEvent) => {
    e.preventDefault();
    setErr("");
    setResult(null);
    try {
      setResult(await api.accessCheck(user.trim(), path.trim()));
    } catch (x: any) {
      setErr(x.message);
    }
  };

  return (
    <div>
      <PageTitle title="Access" subtitle="Who can read/write what — and a quick allow/deny check" />

      <div className="card">
        <h2 className="mb-3 font-semibold">Check access</h2>
        {err && <Alert kind="err">{err}</Alert>}
        <form onSubmit={check} className="flex flex-wrap items-end gap-2">
          <div><label className="label">User</label><input className="input" value={user} onChange={(e) => setUser(e.target.value)} placeholder="alice" /></div>
          <div className="flex-1"><label className="label">Path</label><input className="input mono" value={path} onChange={(e) => setPath(e.target.value)} placeholder="/public/guide.md" /></div>
          <button className="btn" disabled={!user.trim() || !path.trim()}>Check</button>
        </form>
        {result && (
          <div className="mt-3">
            {result.result === "ALLOW" ? (
              <span className="badge-ok">ALLOW</span>
            ) : result.result === "DENY" ? (
              <span className="badge-bad">DENY</span>
            ) : (
              <span className="chip">UNKNOWN — no tokens for this user</span>
            )}
            {result.scope && result.scope.length > 0 && (
              <div className="mt-2 text-sm text-muted">scope: {result.scope.map((p) => <span key={p} className="chip mr-1">{p}</span>)}</div>
            )}
          </div>
        )}
      </div>

      <div className="grid gap-4 md:grid-cols-2">
        <div className="card mt-4">
          <h2 className="mb-2 font-semibold">Groups</h2>
          {tree.loading ? <FullSpinner /> : (tree.data?.groups.length ? (
            <div className="space-y-2">
              {tree.data.groups.map((g) => (
                <div key={g.name} className="border-b border-dashed border-line pb-2 last:border-0">
                  <span className="font-medium">{g.name}</span>
                  <span className="ml-2">{g.prefixes.map((p) => <span key={p} className="chip mr-1">{p}</span>)}</span>
                  <div className="text-xs text-muted">members: {g.members.join(", ") || "none"}</div>
                </div>
              ))}
            </div>
          ) : <p className="text-sm text-muted">No groups.</p>)}
        </div>
        <div className="card mt-4">
          <h2 className="mb-2 font-semibold">Users</h2>
          {tree.loading ? <FullSpinner /> : (
            <div className="space-y-2">
              {(tree.data?.users || []).map((u: any) => (
                <div key={u.user} className="border-b border-dashed border-line pb-2 last:border-0">
                  <span className="font-medium">{u.user}</span> <span className="text-xs text-muted">({u.tokens} token{u.tokens === 1 ? "" : "s"})</span>
                  <div className="mt-1 text-sm">read: {u.read.map((p: string) => <span key={p} className="chip mr-1">{p}</span>) || "—"}</div>
                  {u.write.length > 0 && <div className="mt-1 text-sm">write: {u.write.map((p: string) => <span key={p} className="chip mr-1">{p}</span>)}</div>}
                </div>
              ))}
            </div>
          )}
        </div>
      </div>
    </div>
  );
}
