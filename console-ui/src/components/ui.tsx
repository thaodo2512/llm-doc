import { useCallback, useEffect, useState, type ReactNode } from "react";

// Small data-fetch hook: runs `fn` on mount, exposes loading/error/data + reload().
export function useAsync<T>(fn: () => Promise<T>, deps: unknown[] = []) {
  const [data, setData] = useState<T | null>(null);
  const [error, setError] = useState("");
  const [loading, setLoading] = useState(true);
  // eslint-disable-next-line react-hooks/exhaustive-deps
  const run = useCallback(() => {
    setLoading(true);
    setError("");
    return fn()
      .then(setData)
      .catch((e) => setError(e?.message || String(e)))
      .finally(() => setLoading(false));
  }, deps);
  useEffect(() => {
    run();
  }, [run]);
  return { data, error, loading, reload: run };
}

export function Spinner({ className = "" }: { className?: string }) {
  return (
    <span
      className={`inline-block h-4 w-4 animate-spin rounded-full border-2 border-line2 border-t-accent ${className}`}
      aria-label="loading"
    />
  );
}

export function FullSpinner() {
  return (
    <div className="flex h-full min-h-[60vh] items-center justify-center gap-3 text-muted">
      <Spinner /> loading…
    </div>
  );
}

export function Alert({ kind = "info", children }: { kind?: "ok" | "err" | "warn" | "info"; children: ReactNode }) {
  const styles: Record<string, string> = {
    ok: "border-[#1f6f54] bg-okbg text-ok",
    err: "border-[#7a2a3a] bg-dangerbg text-danger",
    warn: "border-[#c97f17] bg-warnbg text-warn",
    info: "border-line2 bg-inset text-ink2",
  };
  return <div className={`my-2 rounded-lg border px-3 py-2 text-sm ${styles[kind]}`}>{children}</div>;
}

export function PageTitle({ title, subtitle, actions }: { title: string; subtitle?: string; actions?: ReactNode }) {
  return (
    <div className="mb-5 flex items-end justify-between gap-4 border-b border-line pb-4">
      <div>
        <h1 className="text-xl font-semibold tracking-tight">{title}</h1>
        {subtitle && <p className="mt-0.5 text-sm text-muted">{subtitle}</p>}
      </div>
      {actions && <div className="flex shrink-0 gap-2">{actions}</div>}
    </div>
  );
}

export function Field({ label, hint, children }: { label: string; hint?: string; children: ReactNode }) {
  return (
    <label className="block">
      <span className="label">{label}</span>
      {children}
      {hint && <span className="mt-1 block text-xs text-muted">{hint}</span>}
    </label>
  );
}

export function CopyButton({ text, label = "Copy" }: { text: string; label?: string }) {
  const [done, setDone] = useState(false);
  return (
    <button
      type="button"
      className="btn btn-ghost"
      onClick={() => {
        navigator.clipboard?.writeText(text).then(() => {
          setDone(true);
          setTimeout(() => setDone(false), 1200);
        });
      }}
    >
      {done ? "Copied ✓" : label}
    </button>
  );
}

export function Pre({ text }: { text: string }) {
  return (
    <pre className="mono max-h-[420px] overflow-auto whitespace-pre-wrap rounded-lg border border-line bg-bg2 p-3 text-[12.5px] leading-relaxed text-ink2">
      {text || "—"}
    </pre>
  );
}
