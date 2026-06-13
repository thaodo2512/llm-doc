import { useState } from "react";
import { useNavigate } from "react-router-dom";
import { api, ApiError, type JobRef } from "../api";
import { useAuth } from "../auth";
import { JobLog } from "../components/JobLog";
import { Alert, Field, PageTitle } from "../components/ui";

type Profile = "local" | "vpn" | "https";

const PROFILES: { id: Profile; title: string; blurb: string }[] = [
  { id: "local", title: "Local", blurb: "Loopback only (127.0.0.1) — plain HTTP for this machine." },
  { id: "vpn", title: "VPN / internal", blurb: "Raw IP over a trusted private network (plaintext)." },
  { id: "https", title: "HTTPS", blurb: "Public hostname with automatic TLS via Caddy." },
];

export function Wizard() {
  const auth = useAuth();
  const nav = useNavigate();
  const [step, setStep] = useState(0);
  const [profile, setProfile] = useState<Profile>("local");
  const [f, setF] = useState({ port: "8080", ip: "", bind: "", domain: "", portal: false, vector: false, vector_key: "", schedule: "" });
  const [job, setJob] = useState<JobRef | null>(null);
  const [err, setErr] = useState("");
  const [done, setDone] = useState(false);
  const set = (k: string, v: any) => setF((s) => ({ ...s, [k]: v }));

  const apply = async () => {
    setErr("");
    const body: Record<string, unknown> = { profile, portal: f.portal, schedule: f.schedule.trim() || undefined };
    if (profile === "local") body.port = f.port.trim() || undefined;
    if (profile === "vpn") { body.ip = f.ip.trim(); if (f.bind.trim()) body.bind = f.bind.trim(); if (f.port.trim()) body.port = f.port.trim(); }
    if (profile === "https") body.domain = f.domain.trim();
    if (f.vector && f.vector_key.trim()) { body.vector = true; body.vector_key = f.vector_key.trim(); }
    try {
      setJob(await api.wizardApply(body));
    } catch (e) {
      setErr(e instanceof ApiError && e.status === 409 ? "A job is already running — wait for it to finish." : (e as Error).message);
    }
  };

  const onDone = (status: string) => {
    if (status === "done") {
      setDone(true);
      auth.refresh();
    }
  };

  const Steps = () => (
    <div className="mb-5 flex items-center gap-2 text-xs">
      {["Profile", "Details", "Review"].map((s, i) => (
        <div key={s} className={`flex items-center gap-2 ${i === step ? "text-accent" : "text-muted"}`}>
          <span className={`flex h-6 w-6 items-center justify-center rounded-full border ${i === step ? "border-accent text-accent" : "border-line"}`}>{i + 1}</span>
          {s}
          {i < 2 && <span className="mx-1 text-line2">→</span>}
        </div>
      ))}
    </div>
  );

  if (job) {
    return (
      <div>
        <PageTitle title="Setup wizard" subtitle="Applying your configuration" />
        <div className="card">
          <JobLog jobId={job.job_id} onDone={onDone} />
          {done && (
            <Alert kind="ok">
              Setup complete. {auth.role === "bootstrap" ? (
                <>The admin token is printed in the log above — copy it, then <button className="underline" onClick={() => nav("/login")}>sign in</button>.</>
              ) : (
                <>Your deployment is configured. <button className="underline" onClick={() => nav("/")}>Go to dashboard</button>.</>
              )}
            </Alert>
          )}
        </div>
      </div>
    );
  }

  return (
    <div>
      <PageTitle title="Setup wizard" subtitle="Configure a deployment profile, then build + serve" />
      {err && <Alert kind="err">{err}</Alert>}
      {auth.import_dir && (
        <Alert kind="ok">
          📁 <span className="mono">{auth.import_dir}</span> will be imported and indexed during this setup.
        </Alert>
      )}
      <div className="card">
        <Steps />

        {step === 0 && (
          <div className="grid gap-3">
            {PROFILES.map((p) => (
              <button
                key={p.id}
                onClick={() => setProfile(p.id)}
                className={`rounded-lg border p-4 text-left transition ${profile === p.id ? "border-accent bg-accent2/10" : "border-line hover:border-line2"}`}
              >
                <div className="font-medium">{p.title}</div>
                <div className="text-sm text-muted">{p.blurb}</div>
              </button>
            ))}
            <div><button className="btn" onClick={() => setStep(1)}>Next</button></div>
          </div>
        )}

        {step === 1 && (
          <div className="grid gap-3 md:grid-cols-2">
            {profile === "local" && (
              <Field label="Host port" hint="published on 127.0.0.1"><input className="input" value={f.port} onChange={(e) => set("port", e.target.value)} /></Field>
            )}
            {profile === "vpn" && (
              <>
                <Field label="Server IP" hint="the IP clients use over the VPN"><input className="input mono" value={f.ip} onChange={(e) => set("ip", e.target.value)} placeholder="10.0.0.5" /></Field>
                <Field label="Bind interface" hint="default 0.0.0.0"><input className="input mono" value={f.bind} onChange={(e) => set("bind", e.target.value)} placeholder="0.0.0.0" /></Field>
                <Field label="HTTP port"><input className="input" value={f.port} onChange={(e) => set("port", e.target.value)} placeholder="80" /></Field>
              </>
            )}
            {profile === "https" && (
              <Field label="Public hostname" hint="Caddy obtains TLS for this"><input className="input mono" value={f.domain} onChange={(e) => set("domain", e.target.value)} placeholder="docs.example.com" /></Field>
            )}

            <label className="flex items-center gap-2 text-sm text-ink2 md:col-span-2">
              <input type="checkbox" checked={f.portal} onChange={(e) => set("portal", e.target.checked)} /> Enable the upload/manage portal
            </label>
            <label className="flex items-center gap-2 text-sm text-ink2 md:col-span-2">
              <input type="checkbox" checked={f.vector} onChange={(e) => set("vector", e.target.checked)} /> Enable vector (semantic) search
            </label>
            {f.vector && (
              <Field label="OpenAI API key" hint="sent via env, never on the command line"><input className="input mono" type="password" value={f.vector_key} onChange={(e) => set("vector_key", e.target.value)} placeholder="sk-…" /></Field>
            )}
            <Field label="Auto-ingest schedule" hint="optional: 30m · daily · off"><input className="input mono" value={f.schedule} onChange={(e) => set("schedule", e.target.value)} /></Field>

            <div className="flex gap-2 md:col-span-2">
              <button className="btn btn-ghost" onClick={() => setStep(0)}>Back</button>
              <button className="btn" onClick={() => setStep(2)}>Review</button>
            </div>
          </div>
        )}

        {step === 2 && (
          <div>
            <ul className="mb-4 space-y-1 text-sm">
              <li><span className="text-muted">Profile:</span> {profile}</li>
              {profile === "local" && <li><span className="text-muted">Port:</span> {f.port}</li>}
              {profile === "vpn" && <li><span className="text-muted">IP / bind / port:</span> {f.ip || "?"} / {f.bind || "0.0.0.0"} / {f.port || "80"}</li>}
              {profile === "https" && <li><span className="text-muted">Domain:</span> {f.domain || "?"}</li>}
              <li><span className="text-muted">Portal:</span> {f.portal ? "on" : "off"}</li>
              <li><span className="text-muted">Vector:</span> {f.vector ? "on" : "off"}</li>
              <li><span className="text-muted">Schedule:</span> {f.schedule.trim() || "none"}</li>
            </ul>
            <Alert kind="warn">This runs the deploy wizard end-to-end: build → (configure) → serve. It can take several minutes; the log streams live.</Alert>
            <div className="flex gap-2">
              <button className="btn btn-ghost" onClick={() => setStep(1)}>Back</button>
              <button className="btn" onClick={apply}>Apply &amp; deploy</button>
            </div>
          </div>
        )}
      </div>
    </div>
  );
}
