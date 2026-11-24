"use client";

import Link from "next/link";
import { useJob, type Job, type ScanOptions } from "@/lib/api";
import { Badge } from "./ui";

/** Optional scan settings, collapsed by default. */
export function ScanOptionsFields({ value, onChange }: { value: ScanOptions; onChange: (v: ScanOptions) => void }) {
  const set = (k: keyof ScanOptions, v: string) =>
    onChange({ ...value, [k]: v === "" ? undefined : k === "as_of" || k === "mode" ? v : Number(v) });
  return (
    <details className="group text-sm">
      <summary className="cursor-pointer select-none text-muted hover:text-text">Scan options</summary>
      <div className="mt-3 grid gap-3 sm:grid-cols-3">
        <label className="block">
          <span className="text-xs text-muted">Results per source</span>
          <input
            type="number"
            min={1}
            max={2000}
            placeholder="200"
            value={value.max_results ?? ""}
            onChange={(e) => set("max_results", e.target.value)}
            className="mt-1 w-full rounded-md border border-border bg-surface px-2 py-1.5"
          />
        </label>
        <label className="block">
          <span className="text-xs text-muted">Papers to recommend</span>
          <input
            type="number"
            min={1}
            max={200}
            placeholder="25"
            value={value.top_n ?? ""}
            onChange={(e) => set("top_n", e.target.value)}
            className="mt-1 w-full rounded-md border border-border bg-surface px-2 py-1.5"
          />
        </label>
        <label className="block">
          <span className="text-xs text-muted">Window ends on (replay)</span>
          <input
            type="date"
            value={value.as_of ?? ""}
            onChange={(e) => set("as_of", e.target.value)}
            className="mt-1 w-full rounded-md border border-border bg-surface px-2 py-1.5"
          />
        </label>
      </div>
      <p className="mt-2 text-xs text-muted">
        Leave blank for the defaults. A past end date re-runs the radar as of that day.
      </p>
    </details>
  );
}

const STATUS: Record<string, { label: string; tone: "neutral" | "accent" | "good" | "bad" }> = {
  queued: { label: "Queued", tone: "neutral" },
  deferred: { label: "Queued", tone: "neutral" },
  scheduled: { label: "Queued", tone: "neutral" },
  started: { label: "Scanning sources…", tone: "accent" },
  finished: { label: "Scan finished", tone: "good" },
  failed: { label: "Scan failed", tone: "bad" },
  error: { label: "Lost track of the scan", tone: "bad" },
};

/** Live status of a scan job. */
export function JobStatus({ jobId, radarId, onDone }: { jobId: string; radarId: string; onDone?: (job: Job) => void }) {
  const job = useJob(jobId, onDone);
  const s = STATUS[job?.status ?? "queued"] ?? { label: job?.status ?? "", tone: "neutral" as const };
  return (
    <div role="status" aria-live="polite" data-testid="job-status" data-status={job?.status ?? "queued"} className="flex flex-wrap items-center gap-2 text-sm">
      <Badge tone={s.tone}>{s.label}</Badge>
      {job?.status === "finished" && job.run_id && (
        <Link href={`/?radar=${encodeURIComponent(radarId)}`} className="font-medium text-accent hover:underline">
          See recommendations
        </Link>
      )}
      {job?.error && <span className="text-bad">{job.error}</span>}
    </div>
  );
}
