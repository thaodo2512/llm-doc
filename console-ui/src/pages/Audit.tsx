import { api } from "../api";
import { FullSpinner, PageTitle, useAsync } from "../components/ui";

function fmt(ts: number | undefined) {
  return ts ? new Date(ts * 1000).toLocaleString() : "—";
}

export function Audit() {
  const data = useAsync(() => api.audit(100), []);

  return (
    <div>
      <PageTitle
        title="Audit"
        subtitle="Token create/revoke/rotate events + console actions"
        actions={<button className="btn btn-ghost" onClick={() => data.reload()}>Refresh</button>}
      />
      {data.loading ? (
        <FullSpinner />
      ) : (
        <div className="grid gap-4 md:grid-cols-2">
          <div className="card">
            <h2 className="mb-2 font-semibold">Token events</h2>
            <Trail rows={data.data?.tokens || []} fields={["action", "user", "by"]} />
          </div>
          <div className="card">
            <h2 className="mb-2 font-semibold">Console actions</h2>
            <Trail rows={data.data?.console || []} fields={["action", "user", "result"]} />
          </div>
        </div>
      )}
    </div>
  );
}

function Trail({ rows, fields }: { rows: any[]; fields: string[] }) {
  if (!rows.length) return <p className="text-sm text-muted">No entries yet.</p>;
  return (
    <div className="max-h-[480px] space-y-1 overflow-auto">
      {[...rows].reverse().map((r, i) => (
        <div key={i} className="border-b border-dashed border-line py-1.5 text-sm last:border-0">
          <span className="text-muted">{fmt(r.ts)}</span>{" "}
          {fields.map((f) => r[f] && <span key={f} className="mr-2 text-ink2">{f}=<span className="text-ink">{String(r[f])}</span></span>)}
        </div>
      ))}
    </div>
  );
}
