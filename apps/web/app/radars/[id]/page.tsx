"use client";

import Link from "next/link";
import { useParams, useRouter, useSearchParams } from "next/navigation";
import { Suspense, useState } from "react";
import { JobStatus, ScanOptionsFields } from "@/components/Scan";
import { Badge, Button, ButtonLink, Card, ErrorNote, Loading, PageHeader, coverageLine, fmtDate, fmtDateTime, sourceName } from "@/components/ui";
import { client, useApi, type ScanOptions } from "@/lib/api";

function Row({ label, children }: { label: string; children: React.ReactNode }) {
  return (
    <>
      <dt className="text-muted">{label}</dt>
      <dd>{children}</dd>
    </>
  );
}

function RadarDetail() {
  const { id } = useParams<{ id: string }>();
  const params = useSearchParams();
  const router = useRouter();
  const radar = useApi(() => client.radar(id), [id]);
  const runs = useApi(() => client.runs(id), [id]);
  const [jobId, setJobId] = useState<string | undefined>(params.get("job") ?? undefined);
  const [opts, setOpts] = useState<ScanOptions>({});
  const [error, setError] = useState<Error | undefined>();
  const [starting, setStarting] = useState(false);

  async function scan() {
    setStarting(true);
    setError(undefined);
    try {
      const job = await client.scan(id, opts);
      setJobId(job.job_id);
      router.replace(`/radars/${encodeURIComponent(id)}?job=${encodeURIComponent(job.job_id)}`);
    } catch (e) {
      setError(e as Error);
    } finally {
      setStarting(false);
    }
  }

  if (radar.error) return <ErrorNote error={radar.error} />;
  if (!radar.data) return <Loading />;
  const r = radar.data;
  const c = r.config;
  return (
    <>
      <PageHeader
        title={r.title}
        subtitle={r.description || c?.research_question || undefined}
        actions={r.n_runs > 0 && <ButtonLink href={`/?radar=${encodeURIComponent(r.radar_id)}`}>Recommendations</ButtonLink>}
      />
      <div className="space-y-4">
        <Card className="space-y-3">
          <div className="flex flex-wrap items-center gap-3">
            <Button variant="primary" onClick={scan} disabled={starting}>
              Run scan now
            </Button>
            {jobId && (
              <JobStatus
                key={jobId}
                jobId={jobId}
                radarId={r.radar_id}
                onDone={() => {
                  void radar.reload();
                  void runs.reload();
                }}
              />
            )}
          </div>
          <ScanOptionsFields value={opts} onChange={setOpts} />
          <ErrorNote error={error} />
        </Card>

        {c && (
          <Card>
            <h2 className="mb-2 font-medium">Search strategy</h2>
            <dl className="grid grid-cols-[8rem_1fr] gap-x-3 gap-y-1.5 text-sm">
              <Row label="Searches">{c.queries.join("; ")}</Row>
              {c.exclude.length > 0 && <Row label="Excluding">{c.exclude.join("; ")}</Row>}
              <Row label="Sources">{c.sources.map(sourceName).join(", ")}</Row>
              <Row label="Window">{c.filters.date_range.replace(/^last_(\d+)_days$/, "last $1 days")}</Row>
              <Row label="Mode">{c.mode}</Row>
              {c.methods_profile.length > 0 && <Row label="Methods">{c.methods_profile.join(", ")}</Row>}
              {c.seed_papers.length > 0 && <Row label="Seed papers">{c.seed_papers.join(", ")}</Row>}
            </dl>
          </Card>
        )}

        <section aria-labelledby="history">
          <h2 id="history" className="mb-2 font-medium">
            Scan history
          </h2>
          {runs.error && <ErrorNote error={runs.error} />}
          {!runs.data ? (
            <Loading />
          ) : runs.data.length === 0 ? (
            <p className="text-sm text-muted">No scans yet.</p>
          ) : (
            <ul data-testid="run-history" className="divide-y divide-border rounded-lg border border-border bg-surface">
              {runs.data.map((run, i) => (
                <li key={run.run_id} data-testid="run-row" className="flex flex-wrap items-start justify-between gap-3 px-4 py-3 text-sm">
                  <div className="min-w-0">
                    <p>
                      <strong>{fmtDateTime(run.executed_at)}</strong>{" "}
                      {run.status === "ok" ? (
                        <Badge tone="good">complete</Badge>
                      ) : (
                        <Badge tone="warn" title="At least one source returned fewer results than it has">
                          {run.status}
                        </Badge>
                      )}
                      {i === 0 && (
                        <span className="ml-1">
                          <Badge tone="accent">latest</Badge>
                        </span>
                      )}
                    </p>
                    <p className="mt-0.5 text-muted">
                      Up to {fmtDate(run.as_of)} · {run.n_observed.toLocaleString()} observed ·{" "}
                      {run.n_candidates.toLocaleString()} candidates · {run.n_items} recommended · ranking {run.ranking_version}
                    </p>
                    <ul className="mt-0.5 text-xs text-muted">
                      {run.coverage.map((cv) => (
                        <li key={cv.source}>{coverageLine(cv)}</li>
                      ))}
                    </ul>
                  </div>
                  <Link
                    href={`/?radar=${encodeURIComponent(r.radar_id)}${i === 0 ? "" : `&run=${encodeURIComponent(run.run_id)}`}`}
                    className="shrink-0 text-accent hover:underline"
                  >
                    View
                  </Link>
                </li>
              ))}
            </ul>
          )}
        </section>
      </div>
    </>
  );
}

export default function Page() {
  return (
    <Suspense fallback={<Loading />}>
      <RadarDetail />
    </Suspense>
  );
}
