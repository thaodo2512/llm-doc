import { useState } from "react";
import { api } from "../api";
import { Alert, FullSpinner, PageTitle, useAsync } from "../components/ui";

export function Config() {
  const cfg = useAsync(() => api.config(), []);
  const [editing, setEditing] = useState<string | null>(null);
  const [value, setValue] = useState("");
  const [msg, setMsg] = useState<{ kind: "ok" | "err"; text: string } | null>(null);
  const [busy, setBusy] = useState(false);

  const save = async (key: string) => {
    setBusy(true);
    setMsg(null);
    try {
      const r = await api.setConfig(key, value);
      setMsg({ kind: r.ok ? "ok" : "err", text: r.ok ? `${key} updated — restart Serve to apply` : r.output });
      setEditing(null);
      cfg.reload();
    } catch (e: any) {
      setMsg({ kind: "err", text: e.message });
    } finally {
      setBusy(false);
    }
  };

  return (
    <div>
      <PageTitle title="Config" subtitle="Resolved settings + editable .env keys" />
      {msg && <Alert kind={msg.kind}>{msg.text}</Alert>}
      {cfg.loading ? (
        <FullSpinner />
      ) : (
        <div className="card overflow-hidden p-0">
          <table className="w-full text-sm">
            <thead className="bg-bg2 text-left text-xs uppercase tracking-wide text-muted">
              <tr>
                <th className="px-4 py-2">Key</th>
                <th className="px-4 py-2">Value</th>
                <th className="px-4 py-2"></th>
              </tr>
            </thead>
            <tbody>
              {(cfg.data?.env || []).map((row) => (
                <tr key={row.key} className="border-t border-line">
                  <td className="px-4 py-2 mono text-ink2">{row.key}</td>
                  <td className="px-4 py-2">
                    {editing === row.key ? (
                      <input
                        className="input mono"
                        autoFocus
                        type={row.secret ? "password" : "text"}
                        value={value}
                        onChange={(e) => setValue(e.target.value)}
                      />
                    ) : (
                      <span className="mono">{row.value || <span className="text-muted">—</span>}</span>
                    )}
                  </td>
                  <td className="px-4 py-2 text-right">
                    {row.editable ? (
                      editing === row.key ? (
                        <span className="flex justify-end gap-2">
                          <button className="btn" disabled={busy} onClick={() => save(row.key)}>Save</button>
                          <button className="btn btn-ghost" onClick={() => setEditing(null)}>Cancel</button>
                        </span>
                      ) : (
                        <button
                          className="btn btn-ghost"
                          onClick={() => { setEditing(row.key); setValue(row.secret ? "" : row.value); }}
                        >
                          Edit
                        </button>
                      )
                    ) : (
                      <span className="text-xs text-muted">{row.secret ? "secret" : "read-only"}</span>
                    )}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
      <p className="mt-3 text-sm text-muted">
        Only an allowlisted set of keys is editable here. Network posture (HTTP_BIND, DOMAIN, TLS) and
        the session secret are set by the setup wizard, not by ad-hoc edits.
      </p>
    </div>
  );
}
