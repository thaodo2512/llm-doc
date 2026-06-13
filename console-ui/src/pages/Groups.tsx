import { useState } from "react";
import { api } from "../api";
import { Alert, Field, FullSpinner, PageTitle, useAsync } from "../components/ui";

function splitList(s: string): string[] {
  return s.split(/[\s,]+/).map((x) => x.trim()).filter(Boolean);
}

export function Groups() {
  const list = useAsync(() => api.groups(), []);
  const [name, setName] = useState("");
  const [prefixes, setPrefixes] = useState("");
  const [msg, setMsg] = useState<{ kind: "ok" | "err"; text: string } | null>(null);
  const [busy, setBusy] = useState(false);

  const run = async (fn: () => Promise<any>, label: string) => {
    setBusy(true);
    setMsg(null);
    try {
      const r = await fn();
      setMsg({ kind: r.ok ? "ok" : "err", text: r.ok ? `${label} ✓` : r.output });
      list.reload();
    } catch (e: any) {
      setMsg({ kind: "err", text: e.message });
    } finally {
      setBusy(false);
    }
  };

  const define = (e: React.FormEvent) => {
    e.preventDefault();
    run(() => api.defineGroup(name.trim(), splitList(prefixes)), `Group ${name} saved`).then(() => {
      setName("");
      setPrefixes("");
    });
  };

  return (
    <div>
      <PageTitle title="Groups" subtitle="Named sets of read prefixes that tokens can reference" />
      {msg && <Alert kind={msg.kind}>{msg.text}</Alert>}

      <div className="card">
        <h2 className="mb-3 font-semibold">Define / update a group</h2>
        <form onSubmit={define} className="grid gap-3 md:grid-cols-2">
          <Field label="Name" hint="letters, digits, _ -"><input className="input mono" value={name} onChange={(e) => setName(e.target.value)} placeholder="team-a" /></Field>
          <Field label="Prefixes" hint="space/comma separated (a group can't grant the whole corpus)"><input className="input mono" value={prefixes} onChange={(e) => setPrefixes(e.target.value)} placeholder="/team/a /shared" /></Field>
          <div className="md:col-span-2">
            <button className="btn" disabled={busy || !name.trim() || !prefixes.trim()}>Save group</button>
          </div>
        </form>
      </div>

      <div className="card mt-4">
        <div className="mb-2 flex items-center justify-between">
          <h2 className="font-semibold">Groups</h2>
          <button className="btn btn-ghost" onClick={() => list.reload()}>Refresh</button>
        </div>
        {list.loading ? (
          <FullSpinner />
        ) : !list.data?.groups.length ? (
          <p className="text-sm text-muted">No groups defined.</p>
        ) : (
          <div className="space-y-3">
            {list.data.groups.map((g) => (
              <div key={g.name} className="flex flex-wrap items-center justify-between gap-3 border-b border-dashed border-line pb-3 last:border-0">
                <div>
                  <div className="font-medium">{g.name}</div>
                  <div className="mt-1">
                    {g.prefixes.map((p) => <span key={p} className="chip mr-1 mb-1">{p}</span>)}
                  </div>
                  <div className="mt-1 text-xs text-muted">
                    members: {g.members.length ? g.members.join(", ") : "none"}
                  </div>
                </div>
                <button className="btn btn-danger" disabled={busy} onClick={() => confirm(`Delete group ${g.name}?`) && run(() => api.removeGroup(g.name), `Group ${g.name} removed`)}>
                  Delete
                </button>
              </div>
            ))}
          </div>
        )}
      </div>
    </div>
  );
}
