import { useState } from "react";
import { api, ApiError, type JobRef } from "../api";
import { JobLog } from "../components/JobLog";
import { Alert, PageTitle } from "../components/ui";

export function Ingest() {
  const [job, setJob] = useState<JobRef | null>(null);
  const [err, setErr] = useState("");
  const [full, setFull] = useState(false);
  const [target, setTarget] = useState("server");
  const [pending, setPending] = useState<string | null>(null);

  const start = async (label: string, fn: () => Promise<JobRef>) => {
    setErr("");
    setPending(label);
    try {
      setJob(await fn());
    } catch (e) {
      setErr(e instanceof ApiError && e.status === 409 ? "A build/ingest/serve job is already running — wait for it to finish." : (e as Error).message);
    } finally {
      setPending(null);
    }
  };

  const Btn = ({ label, fn, danger }: { label: string; fn: () => Promise<JobRef>; danger?: boolean }) => (
    <button className={`btn ${danger ? "btn-danger" : "btn-ghost"}`} disabled={!!pending} onClick={() => start(label, fn)}>
      {pending === label ? "starting…" : label}
    </button>
  );

  return (
    <div>
      <PageTitle title="Ingest & lifecycle" subtitle="Build images, ingest the corpus, start/stop the server" />
      {err && <Alert kind="err">{err}</Alert>}

      <div className="card">
        <h2 className="mb-3 font-semibold">Ingest</h2>
        <div className="flex flex-wrap items-center gap-3">
          <label className="flex items-center gap-2 text-sm text-ink2">
            <input type="checkbox" checked={full} onChange={(e) => setFull(e.target.checked)} /> full re-ingest (<span className="mono">--full</span>)
          </label>
          <Btn label="Run ingest" fn={() => api.ingest(full)} />
        </div>
        <p className="mt-2 text-xs text-muted">Rebuilds the searchable store from <span className="mono">raw/</span>. May take minutes; the log streams below.</p>
      </div>

      <div className="card mt-4">
        <h2 className="mb-3 font-semibold">Images & server</h2>
        <div className="flex flex-wrap items-center gap-3">
          <select className="input w-auto" value={target} onChange={(e) => setTarget(e.target.value)}>
            <option value="server">server</option>
            <option value="ingest">ingest</option>
            <option value="all">all</option>
          </select>
          <Btn label="Build image(s)" fn={() => api.build(target)} />
          <Btn label="Serve" fn={() => api.serve()} />
          <Btn label="Stop" fn={() => api.stop()} danger />
          <Btn label="Backup" fn={() => api.backup()} />
        </div>
      </div>

      {job && (
        <div className="card mt-4">
          <h2 className="font-semibold">{job.label}</h2>
          <JobLog jobId={job.job_id} />
        </div>
      )}
    </div>
  );
}
