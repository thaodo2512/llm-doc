import { useEffect, useState } from "react";
import { useNavigate } from "react-router-dom";
import { ApiError } from "../api";
import { useAuth } from "../auth";
import { Alert, Field } from "../components/ui";

export function Login() {
  const a = useAuth();
  const nav = useNavigate();
  const [token, setToken] = useState("");
  const [err, setErr] = useState("");
  const [busy, setBusy] = useState(false);

  useEffect(() => {
    if (a.authenticated && a.role === "admin") nav("/", { replace: true });
  }, [a.authenticated, a.role, nav]);

  const submit = async (e: React.FormEvent) => {
    e.preventDefault();
    setErr("");
    setBusy(true);
    try {
      await a.login(token.trim());
      nav("/", { replace: true });
    } catch (x) {
      setErr(
        x instanceof ApiError && x.status === 403
          ? "This console requires the admin (whole-corpus) token."
          : "Invalid or expired token.",
      );
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="flex min-h-screen items-center justify-center p-6">
      <div className="card w-full max-w-md">
        <div className="mb-1 text-lg font-semibold tracking-tight">
          doc<span className="text-accent">mcp</span> console
        </div>
        <p className="mb-4 text-sm text-muted">Sign in with the admin bearer token.</p>
        {a.bootstrap_active && !a.setup_done && (
          <Alert kind="warn">
            First run — no admin token yet. Open the bootstrap link printed by{" "}
            <span className="mono">./docmcp.sh console</span> to run the setup wizard.
          </Alert>
        )}
        {err && <Alert kind="err">{err}</Alert>}
        <form onSubmit={submit}>
          <Field label="Admin token">
            <input
              className="input mono"
              type="password"
              autoFocus
              autoComplete="off"
              placeholder="tok_…"
              value={token}
              onChange={(e) => setToken(e.target.value)}
            />
          </Field>
          <button className="btn mt-4 w-full justify-center" disabled={busy || !token.trim()}>
            {busy ? "Signing in…" : "Sign in"}
          </button>
        </form>
      </div>
    </div>
  );
}
