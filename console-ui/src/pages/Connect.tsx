import { api } from "../api";
import { CopyButton, FullSpinner, PageTitle, Pre, useAsync } from "../components/ui";

export function Connect() {
  const conn = useAsync(() => api.connect(), []);

  return (
    <div>
      <PageTitle title="Connect a client" subtitle="Point an MCP client (e.g. Codex) at this server" />
      {conn.loading ? (
        <FullSpinner />
      ) : (
        <>
          <div className="card">
            <h2 className="mb-2 font-semibold">Endpoint</h2>
            <div className="flex items-center gap-3">
              <code className="mono rounded-lg border border-line bg-bg2 px-3 py-2 text-accent">
                {conn.data?.url}
              </code>
              <CopyButton text={conn.data?.url || ""} label="Copy URL" />
            </div>
          </div>
          <div className="card mt-4">
            <div className="mb-2 flex items-center justify-between">
              <h2 className="font-semibold">Add to Codex</h2>
              <CopyButton text={conn.data?.codex_cmd || ""} label="Copy command" />
            </div>
            <Pre text={conn.data?.codex_cmd || ""} />
            {conn.data?.has_token ? (
              <p className="mt-2 text-sm text-muted">
                Ready to run — the token is already filled in. Paste these two lines into your
                terminal, then run <code className="mono">codex</code> and{" "}
                <code className="mono">/mcp</code> to confirm it connected.
              </p>
            ) : (
              <p className="mt-2 text-sm text-muted">
                Finish the setup wizard first — it mints the token that gets filled in here.
              </p>
            )}
          </div>
        </>
      )}
    </div>
  );
}
