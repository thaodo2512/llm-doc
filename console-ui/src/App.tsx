import { useEffect, type ReactNode } from "react";
import { Navigate, Route, Routes, useLocation, useNavigate } from "react-router-dom";
import { useAuth } from "./auth";
import { Layout } from "./components/Layout";
import { FullSpinner } from "./components/ui";
import { Login } from "./pages/Login";
import { Wizard } from "./pages/Wizard";
import { Dashboard } from "./pages/Dashboard";
import { Tokens } from "./pages/Tokens";
import { Groups } from "./pages/Groups";
import { Access } from "./pages/Access";
import { Ingest } from "./pages/Ingest";
import { Schedule } from "./pages/Schedule";
import { Health } from "./pages/Health";
import { Config } from "./pages/Config";
import { Connect } from "./pages/Connect";
import { Audit } from "./pages/Audit";

function RequireAdmin({ children }: { children: ReactNode }) {
  const a = useAuth();
  if (a.loading) return <FullSpinner />;
  if (a.authenticated && a.role === "admin") return <Layout>{children}</Layout>;
  if (a.bootstrap_active && !a.setup_done) return <Navigate to="/wizard" replace />;
  return <Navigate to="/login" replace />;
}

function RequireSession({ children }: { children: ReactNode }) {
  const a = useAuth();
  if (a.loading) return <FullSpinner />;
  if (a.authenticated) return <Layout>{children}</Layout>;
  return <Navigate to="/login" replace />;
}

export default function App() {
  const a = useAuth();
  const nav = useNavigate();
  const loc = useLocation();

  // First-run: auto-consume the ?bootstrap=… token printed by `./docmcp.sh console`.
  useEffect(() => {
    if (a.loading || a.authenticated) return;
    const token = new URLSearchParams(loc.search).get("bootstrap");
    if (token) {
      a.bootstrapLogin(token)
        .then(() => nav("/wizard", { replace: true }))
        .catch(() => nav("/login", { replace: true }));
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [a.loading, a.authenticated]);

  return (
    <Routes>
      <Route path="/login" element={<Login />} />
      <Route path="/wizard" element={<RequireSession><Wizard /></RequireSession>} />
      <Route path="/" element={<RequireAdmin><Dashboard /></RequireAdmin>} />
      <Route path="/tokens" element={<RequireAdmin><Tokens /></RequireAdmin>} />
      <Route path="/groups" element={<RequireAdmin><Groups /></RequireAdmin>} />
      <Route path="/access" element={<RequireAdmin><Access /></RequireAdmin>} />
      <Route path="/ingest" element={<RequireAdmin><Ingest /></RequireAdmin>} />
      <Route path="/schedule" element={<RequireAdmin><Schedule /></RequireAdmin>} />
      <Route path="/health" element={<RequireAdmin><Health /></RequireAdmin>} />
      <Route path="/config" element={<RequireAdmin><Config /></RequireAdmin>} />
      <Route path="/connect" element={<RequireAdmin><Connect /></RequireAdmin>} />
      <Route path="/audit" element={<RequireAdmin><Audit /></RequireAdmin>} />
      <Route path="*" element={<Navigate to="/" replace />} />
    </Routes>
  );
}
