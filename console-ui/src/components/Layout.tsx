import { type ReactNode } from "react";
import { NavLink, useNavigate } from "react-router-dom";
import { useAuth } from "../auth";

interface NavItem {
  to: string;
  label: string;
  icon: string;
}

const ADMIN_NAV: NavItem[] = [
  { to: "/", label: "Dashboard", icon: "▦" },
  { to: "/tokens", label: "Tokens", icon: "🔑" },
  { to: "/groups", label: "Groups", icon: "👥" },
  { to: "/access", label: "Access", icon: "🌲" },
  { to: "/ingest", label: "Ingest & lifecycle", icon: "⟲" },
  { to: "/schedule", label: "Schedule", icon: "⏱" },
  { to: "/health", label: "Health", icon: "✚" },
  { to: "/config", label: "Config", icon: "⚙" },
  { to: "/connect", label: "Connect", icon: "🔌" },
  { to: "/audit", label: "Audit", icon: "📜" },
  { to: "/wizard", label: "Setup wizard", icon: "✨" },
];

export function Layout({ children }: { children: ReactNode }) {
  const auth = useAuth();
  const nav = useNavigate();
  const items = auth.role === "admin" ? ADMIN_NAV : [{ to: "/wizard", label: "Setup wizard", icon: "✨" }];

  return (
    <div className="flex min-h-full">
      <aside className="sticky top-0 hidden h-screen w-60 shrink-0 flex-col border-r border-line bg-bg2/60 p-4 md:flex">
        <div className="mb-6 px-2">
          <div className="text-lg font-semibold tracking-tight">
            doc<span className="text-accent">mcp</span>
          </div>
          <div className="text-xs text-muted">admin console</div>
        </div>
        <nav className="flex flex-1 flex-col gap-1">
          {items.map((it) => (
            <NavLink
              key={it.to}
              to={it.to}
              end={it.to === "/"}
              className={({ isActive }) =>
                `flex items-center gap-2.5 rounded-lg px-3 py-2 text-sm transition ${
                  isActive ? "bg-accent2/20 text-accent" : "text-ink2 hover:bg-panel hover:text-ink"
                }`
              }
            >
              <span className="w-4 text-center text-xs opacity-80">{it.icon}</span>
              {it.label}
            </NavLink>
          ))}
        </nav>
        {auth.authenticated && (
          <div className="mt-4 border-t border-line pt-4">
            <div className="px-2 text-xs text-muted">
              signed in as <span className="text-ink2">{auth.user}</span>
              {auth.role === "bootstrap" && <span className="ml-1 chip">bootstrap</span>}
            </div>
            <button
              className="btn btn-ghost mt-2 w-full justify-center"
              onClick={() => auth.logout().then(() => nav("/login"))}
            >
              Sign out
            </button>
          </div>
        )}
      </aside>
      <main className="mx-auto w-full max-w-5xl flex-1 px-5 py-8 md:px-8">{children}</main>
    </div>
  );
}
