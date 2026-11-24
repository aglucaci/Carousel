"use client";

import Link from "next/link";
import { useParams, useRouter, useSearchParams } from "next/navigation";
import { Suspense, useState } from "react";
import { JobStatus, ScanOptionsFields } from "@/components/Scan";
import { Badge, Button, ButtonLink, Card, ErrorNote, Loading, PageHeader, coverageLine, fmtDate, fmtDateTime, sourceName } from "@/components/ui";
import { client, useApi, type DigestDelivery, type Radar, type RadarPatch, type ScanOptions } from "@/lib/api";

function Row({ label, children }: { label: string; children: React.ReactNode }) {
  return (
    <>
      <dt className="text-muted">{label}</dt>
      <dd>{children}</dd>
    </>
  );
}

const SCHEDULES = [
  { value: "", label: "Manual only" },
  { value: "daily", label: "Daily" },
  { value: "weekly", label: "Weekly (Mondays)" },
];

function ScheduleCard({ radar, onChange }: { radar: Radar; onChange: (r: Radar) => void }) {
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState<Error | undefined>();
  const paused = radar.state === "paused";

  async function update(patch: RadarPatch) {
    setSaving(true);
    setError(undefined);
    try {
      onChange(await client.updateRadar(radar.radar_id, patch));
    } catch (e) {
      setError(e as Error);
    } finally {
      setSaving(false);
    }
  }

  return (
    <Card className="space-y-2">
      <div className="flex flex-wrap items-center justify-between gap-3">
        <h2 className="font-medium">
          Automatic scans {paused && <Badge tone="warn">paused</Badge>}
        </h2>
        <div className="flex flex-wrap items-center gap-2">
          <label htmlFor="schedule" className="text-sm text-muted">
            Repeat
          </label>
          <select
            id="schedule"
            value={radar.schedule ?? ""}
            disabled={saving}
            onChange={(e) => update({ schedule: (e.target.value || null) as RadarPatch["schedule"] })}
            className="rounded-md border border-border bg-surface px-2 py-1.5 text-sm"
          >
            {SCHEDULES.map((s) => (
              <option key={s.value} value={s.value}>
                {s.label}
              </option>
            ))}
          </select>
          {radar.schedule && (
            <Button onClick={() => update({ state: paused ? "active" : "paused" })} disabled={saving}>
              {paused ? "Resume" : "Pause"}
            </Button>
          )}
        </div>
      </div>
      <p data-testid="schedule-status" className="text-sm text-muted">
        {!radar.schedule
          ? "Scans run only when you start them."
          : paused
            ? "Paused: no automatic scans until you resume. All runs, saved papers, and feedback are kept, and you can still scan by hand."
            : radar.next_scan_at && new Date(radar.next_scan_at).getTime() <= Date.now() + 60_000
              ? `Scans ${radar.schedule} (UTC). The next scan starts within a minute or two.`
              : `Scans ${radar.schedule} (UTC). Next scan: ${radar.next_scan_at ? fmtDateTime(radar.next_scan_at) : "not scheduled"}.`}
      </p>
      <ErrorNote error={error} />
    </Card>
  );
}

const CHANNELS: Record<DigestDelivery["channel"], string> = { markdown: "Markdown", rss: "RSS feed", email: "Email" };

function DigestsCard({ radar, onChange }: { radar: Radar; onChange: (r: Radar) => void }) {
  const id = radar.radar_id;
  const deliveries = useApi(() => client.digests(id), [id, radar.n_runs]);
  const [emails, setEmails] = useState((radar.digest_emails ?? []).join(", "));
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<Error | undefined>();
  const base = `/api/v1/radars/${encodeURIComponent(id)}`;
  const saved = (radar.digest_emails ?? []).join(", ");

  async function run(action: () => Promise<void>) {
    setBusy(true);
    setError(undefined);
    try {
      await action();
    } catch (e) {
      setError(e as Error);
    } finally {
      setBusy(false);
    }
  }

  const save = () =>
    run(async () => {
      const updated = await client.updateRadar(id, { digest_emails: emails.split(/[,;\s]+/).filter(Boolean) });
      onChange(updated);
      setEmails((updated.digest_emails ?? []).join(", "));
    });
  const send = () =>
    run(async () => {
      await client.sendDigest(id);
      await deliveries.reload();
    });

  return (
    <Card className="space-y-3">
      <h2 className="font-medium">Digests</h2>
      <p className="text-sm">
        Every scan produces a digest:{" "}
        <a href={`${base}/digest.md`} target="_blank" rel="noreferrer" className="text-accent hover:underline">
          the latest as Markdown
        </a>
        ,{" "}
        <a href={`${base}/feed.xml`} target="_blank" rel="noreferrer" className="text-accent hover:underline">
          an RSS feed
        </a>{" "}
        for your feed reader, and an email to the addresses below.
      </p>
      <div className="flex flex-wrap items-end gap-2">
        <label className="min-w-0 flex-1 text-sm">
          <span className="mb-1 block text-muted">Email the digest to</span>
          <input
            id="digest-emails"
            value={emails}
            onChange={(e) => setEmails(e.target.value)}
            placeholder="you@example.org, colleague@example.org"
            className="w-full rounded-md border border-border bg-surface px-2 py-1.5"
          />
        </label>
        <Button onClick={save} disabled={busy || emails === saved}>
          Save
        </Button>
        {saved && radar.n_runs > 0 && (
          <Button onClick={send} disabled={busy}>
            Send latest now
          </Button>
        )}
      </div>
      <ErrorNote error={error} />
      {deliveries.data && deliveries.data.length > 0 && (
        <ul className="space-y-1 text-sm" aria-label="Digest deliveries">
          {deliveries.data.map((d) => (
            <li key={d.id} data-testid="digest-delivery" className="flex flex-wrap items-baseline gap-x-2">
              <span className="text-muted">{fmtDateTime(d.generated_at)}</span>
              <span>{CHANNELS[d.channel]}</span>
              {d.recipient && <span className="text-muted">to {d.recipient}</span>}
              <Badge tone={d.status === "delivered" ? "good" : "bad"}>{d.status === "delivered" ? "Delivered" : "Failed"}</Badge>
              {d.error && <span className="text-bad">{d.error}</span>}
            </li>
          ))}
        </ul>
      )}
    </Card>
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

        <ScheduleCard radar={r} onChange={(updated) => radar.setData(updated)} />
        <DigestsCard radar={r} onChange={(updated) => radar.setData(updated)} />

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
                      {run.trigger === "scheduled" && (
                        <span className="ml-1">
                          <Badge title="Started by the scheduler">scheduled</Badge>
                        </span>
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
