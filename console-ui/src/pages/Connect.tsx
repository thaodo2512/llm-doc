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
              <h2 className="font-semibold">Codex config</h2>
              <CopyButton text={conn.data?.codex_toml || ""} label="Copy TOML" />
            </div>
            <Pre text={conn.data?.codex_toml || ""} />
            <p className="mt-2 text-sm text-muted">
              Mint a scoped token on the <a href="/tokens">Tokens</a> page and paste it as the bearer token.
            </p>
          </div>
        </>
      )}
    </div>
  );
}
