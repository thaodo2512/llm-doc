import { useEffect, useRef, useState } from "react";
import { api } from "../api";

// Streams a job's output over SSE, falling back to polling if the stream drops. Job
// state lives on the server, so re-mounting (navigating back) re-attaches cleanly.
export function JobLog({ jobId, onDone }: { jobId: string; onDone?: (status: string) => void }) {
  const [lines, setLines] = useState<string[]>([]);
  const [status, setStatus] = useState<string>("running");
  const boxRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    setLines([]);
    setStatus("running");
    let es: EventSource | null = null;
    let poll: ReturnType<typeof setInterval> | null = null;
    let cursor = 0;
    let stopped = false;

    const finish = (s: string) => {
      if (stopped) return;
      stopped = true;
      setStatus(s);
      onDone?.(s);
      es?.close();
      if (poll) clearInterval(poll);
    };

    const startPoll = () => {
      if (poll) return;
      poll = setInterval(async () => {
        try {
          const r = await api.jobLog(jobId, cursor);
          cursor = r.cursor;
          if (r.lines.length) setLines((p) => [...p, ...r.lines]);
          if (r.status !== "running") finish(r.status);
        } catch {
          /* keep polling */
        }
      }, 800);
    };

    try {
      es = new EventSource(`/api/jobs/${jobId}/stream`);
      es.addEventListener("line", (e) => setLines((p) => [...p, JSON.parse((e as MessageEvent).data)]));
      es.addEventListener("done", (e) => finish(JSON.parse((e as MessageEvent).data).status));
      es.onerror = () => {
        es?.close();
        es = null;
        if (!stopped) startPoll();
      };
    } catch {
      startPoll();
    }
    return () => {
      stopped = true;
      es?.close();
      if (poll) clearInterval(poll);
    };
  }, [jobId]);

  useEffect(() => {
    const el = boxRef.current;
    if (el) el.scrollTop = el.scrollHeight;
  }, [lines]);

  return (
    <div className="mt-3">
      <div className="mb-2 flex items-center gap-2 text-xs">
        {status === "running" ? (
          <span className="chip">running…</span>
        ) : status === "done" ? (
          <span className="badge-ok">done</span>
        ) : (
          <span className="badge-bad">failed</span>
        )}
        <span className="text-muted">{lines.length} lines</span>
      </div>
      <div
        ref={boxRef}
        className="mono h-80 overflow-auto whitespace-pre-wrap rounded-lg border border-line bg-bg2 p-3 text-[12.5px] leading-relaxed text-ink2"
      >
        {lines.length ? lines.join("\n") : <span className="text-muted">waiting for output…</span>}
      </div>
    </div>
  );
}
