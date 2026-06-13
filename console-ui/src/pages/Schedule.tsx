import { useState } from "react";
import { api } from "../api";
import { Alert, FullSpinner, PageTitle, Pre, useAsync } from "../components/ui";

const PRESETS = ["30m", "hourly", "daily", "weekly", "off"];

export function Schedule() {
  const show = useAsync(() => api.scheduleShow(), []);
  const [spec, setSpec] = useState("daily");
  const [msg, setMsg] = useState<{ kind: "ok" | "err"; text: string } | null>(null);
  const [busy, setBusy] = useState(false);

  const apply = async (value: string) => {
    setBusy(true);
    setMsg(null);
    try {
      const r = await api.scheduleSet(value);
      setMsg({ kind: r.ok ? "ok" : "err", text: r.output || (r.ok ? "applied" : "failed") });
      show.reload();
    } catch (e: any) {
      setMsg({ kind: "err", text: e.message });
    } finally {
      setBusy(false);
    }
  };

  return (
    <div>
      <PageTitle title="Schedule" subtitle="Recurring ingest via cron / systemd timer" />
      <div className="card">
        <h2 className="mb-2 font-semibold">Current</h2>
        {show.loading ? <FullSpinner /> : <Pre text={show.data?.output || show.error} />}
      </div>

      <div className="card mt-4">
        <h2 className="mb-3 font-semibold">Set schedule</h2>
        {msg && <Alert kind={msg.kind}>{msg.text}</Alert>}
        <div className="mb-3 flex flex-wrap gap-2">
          {PRESETS.map((p) => (
            <button key={p} className="btn btn-ghost" disabled={busy} onClick={() => apply(p)}>
              {p}
            </button>
          ))}
        </div>
        <div className="flex items-end gap-2">
          <div className="flex-1">
            <label className="label">Custom (Nm · Nh · daily · or a 5-field cron)</label>
            <input className="input mono" value={spec} onChange={(e) => setSpec(e.target.value)} />
          </div>
          <button className="btn" disabled={busy || !spec.trim()} onClick={() => apply(spec.trim())}>
            Apply
          </button>
        </div>
      </div>
    </div>
  );
}
