import { Link } from "react-router-dom";
import { api } from "../api";
import { FullSpinner, PageTitle, Pre, Spinner, useAsync } from "../components/ui";

export function Dashboard() {
  const status = useAsync(() => api.status(), []);
  const doctor = useAsync(() => api.doctor(), []);

  return (
    <div>
      <PageTitle
        title="Dashboard"
        subtitle="Live state of the docmcp deployment"
        actions={
          <button className="btn btn-ghost" onClick={() => { status.reload(); doctor.reload(); }}>
            Refresh
          </button>
        }
      />

      <div className="grid gap-4 md:grid-cols-2">
        <div className="card">
          <div className="mb-2 flex items-center justify-between">
            <h2 className="font-semibold">Health</h2>
            {doctor.loading ? (
              <Spinner />
            ) : doctor.data?.ok ? (
              <span className="badge-ok">healthy</span>
            ) : (
              <span className="badge-bad">attention</span>
            )}
          </div>
          {doctor.loading ? <FullSpinner /> : <Pre text={doctor.data?.output || doctor.error} />}
          <Link to="/health" className="mt-2 inline-block text-sm">
            Full health report →
          </Link>
        </div>

        <div className="card">
          <h2 className="mb-2 font-semibold">Status</h2>
          {status.loading ? <FullSpinner /> : <Pre text={status.data?.output || status.error} />}
        </div>
      </div>

      <div className="card mt-4">
        <h2 className="mb-3 font-semibold">Quick actions</h2>
        <div className="flex flex-wrap gap-2">
          <Link className="btn btn-ghost" to="/ingest">Ingest / serve / build</Link>
          <Link className="btn btn-ghost" to="/tokens">Mint a token</Link>
          <Link className="btn btn-ghost" to="/connect">Connect a client</Link>
          <Link className="btn btn-ghost" to="/wizard">Re-run setup wizard</Link>
        </div>
      </div>
    </div>
  );
}
