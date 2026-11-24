"use client";

import { useRouter, useSearchParams } from "next/navigation";
import { Suspense, useMemo, useState } from "react";
import { TrendChart } from "@/components/TrendChart";
import { Badge, Button, Card, Empty, ErrorNote, Loading, PageHeader, fmtDate, sourceName } from "@/components/ui";
import { client, useApi, type TrendReport, type TrendTopic } from "@/lib/api";

function pct(x?: number | null) {
  return x == null ? "—" : `${(100 * x).toFixed(2)}%`;
}

const WHY: Record<string, string> = {
  effect_size: "the rise is small",
  significance: "not significant after correction",
  min_count: "too few papers",
  persistence: "not yet in two consecutive weeks",
  insufficient_history: "too few weeks of counts",
  no_counts: "no papers in these weeks",
  denominator_shift: "the field's own count shifted",
  duplicate_surge: "versions of the same works",
  failed: "the model could not be fitted",
};

function TopicCard({ t }: { t: TrendTopic }) {
  const [table, setTable] = useState(false);
  const base = Object.fromEntries((t.baseline_share ?? []).map((b) => [b.week, b]));
  return (
    <Card className="space-y-2">
      <div data-testid="trend-topic" data-topic={t.topic_id} className="flex flex-wrap items-baseline justify-between gap-2">
        <h2 className="font-serif text-lg">{t.label ?? t.topic_id}</h2>
        {t.flagged ? <Badge tone="accent">Growing</Badge> : null}
      </div>
      <p className="text-sm text-muted">
        {t.status === "ok" ? (
          <>
            {Number(t.rate_ratio).toFixed(2)}&times; its baseline share in the tested weeks (95% interval{" "}
            {Number(t.ci_low).toFixed(2)}–{t.ci_high == null ? "no upper bound" : Number(t.ci_high).toFixed(2)}, q ={" "}
            {Number(t.q_value).toFixed(3)}); {t.recent_count} papers, about{" "}
            {Math.round(t.expected_recent ?? 0)} expected.
          </>
        ) : (
          <>Not evaluated: {WHY[t.status] ?? t.status}.</>
        )}
        {!t.flagged && t.failed.length > 0 && t.status === "ok" && <> Not flagged: {t.failed.map((f) => WHY[f] ?? f).join("; ")}.</>}
      </p>
      <TrendChart topic={t} />
      <p className="text-xs text-muted">
        Solid line and filled circles: observed share; hollow circles: provisional weeks; dashed line with crosses: fitted baseline,
        shaded band its 95% interval.
      </p>
      <Button variant="ghost" aria-expanded={table} onClick={() => setTable((v) => !v)}>
        {table ? "Hide table" : "Show as table"}
      </Button>
      {table && (
        <table className="w-full text-left text-sm tabular-nums" data-testid="trend-table">
          <thead className="text-xs text-muted">
            <tr>
              <th className="py-1 font-normal">Week from</th>
              <th className="py-1 font-normal">Topic</th>
              <th className="py-1 font-normal">Field</th>
              <th className="py-1 font-normal">Share</th>
              <th className="py-1 font-normal">Baseline (95% band)</th>
              <th className="py-1 font-normal">Status</th>
            </tr>
          </thead>
          <tbody>
            {t.weekly.map((w) => (
              <tr key={w.week} className="border-t border-border" data-week={w.week}>
                <td className="py-1">{w.week}</td>
                <td className="py-1">{w.count}</td>
                <td className="py-1">{w.denominator}</td>
                <td className="py-1">{pct(w.share)}</td>
                <td className="py-1">{base[w.week] ? `${pct(base[w.week].share)} (${pct(base[w.week].low)}–${pct(base[w.week].high)})` : "—"}</td>
                <td className="py-1">{w.provisional ? "provisional" : "settled"}</td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
    </Card>
  );
}

function Report({ r }: { r: TrendReport }) {
  const fired = r.checks.filter((c) => c.status === "fired");
  const topics = [...r.topics].sort((a, b) => Number(b.flagged) - Number(a.flagged) || (b.rate_ratio ?? 0) - (a.rate_ratio ?? 0));
  return (
    <div className="space-y-4">
      <p className="text-sm text-muted" data-testid="trend-scope">
        Counts from {sourceName(r.source)}, as observed by {fmtDate(r.as_of)}; evaluated through {fmtDate(r.evaluated_through)}
        {r.provisional_weeks.length ? ` (${r.provisional_weeks.length} later week${r.provisional_weeks.length === 1 ? "" : "s"} still being indexed: provisional)` : ""}.
        Model {r.trend_model_version}, checks {r.checks_version}.
      </p>
      {fired.map((c) => (
        <p key={c.check} className="rounded-md bg-accent-soft px-3 py-2 text-sm">
          <strong>{c.check.replaceAll("_", " ")}:</strong> {c.detail}
        </p>
      ))}
      {topics.map((t) => (
        <TopicCard key={t.topic_id} t={t} />
      ))}
    </div>
  );
}

function Trends({ radarId }: { radarId: string }) {
  const { data, error, loading } = useApi(() => client.trends(radarId), [radarId]);
  const [source, setSource] = useState<string | undefined>();
  if (error) {
    return (
      <Empty title="No trends yet">
        {error.message.includes("topic model") ? "This radar has no topics yet: they are fitted once its scans have found enough papers." : error.message}
      </Empty>
    );
  }
  if (loading && !data) return <Loading />;
  if (!data) return null;
  if (!data.reports.length) {
    return (
      <Empty title="No counts yet">
        Topics are counted weekly by the scheduler (or <code>carousel topics observe</code>); trends need about 13 weeks of counts.
      </Empty>
    );
  }
  const current = data.reports.find((r) => r.source === source) ?? data.reports[0];
  return (
    <>
      <p className="mb-3 text-sm text-muted" data-testid="version-note">
        Topics {data.classifier_version}, fitted {fmtDate(data.fitted_at)}.
        {data.previous_version
          ? ` Counts under the earlier topic model (${data.previous_version}) are not mixed in: shares before this version are not shown.`
          : ""}
      </p>
      {data.reports.length > 1 && (
        <div role="tablist" aria-label="Sources" className="mb-4 flex flex-wrap gap-2">
          {data.reports.map((r) => (
            <button
              key={r.source}
              role="tab"
              aria-selected={r.source === current.source}
              onClick={() => setSource(r.source)}
              className={`rounded-full border px-3 py-1 text-sm ${
                r.source === current.source ? "border-accent bg-accent-soft text-accent" : "border-border text-muted hover:text-text"
              }`}
            >
              {sourceName(r.source)}
            </button>
          ))}
        </div>
      )}
      <Report r={current} />
    </>
  );
}

function TrendsPage() {
  const params = useSearchParams();
  const router = useRouter();
  const { data: radars, error, loading } = useApi(() => client.radars(), []);
  const withRuns = useMemo(() => (radars ?? []).filter((r) => r.n_runs > 0), [radars]);
  const selected = params.get("radar") ?? withRuns[0]?.radar_id;
  if (error) return <ErrorNote error={error} />;
  if (loading && !radars) return <Loading />;
  return (
    <>
      <PageHeader
        title="Trends"
        subtitle="Each topic's share of your field per week, against its fitted baseline. Growth is flagged only when it is large, significant, persistent, and passes checks for indexing and counting artifacts."
      />
      {!selected ? (
        <Empty title="No scans yet">Run a scan from Radars first.</Empty>
      ) : (
        <>
          {withRuns.length > 1 && (
            <div role="tablist" aria-label="Radars" className="mb-4 flex flex-wrap gap-2">
              {withRuns.map((r) => (
                <button
                  key={r.radar_id}
                  role="tab"
                  aria-selected={r.radar_id === selected}
                  onClick={() => router.push(`/trends?radar=${encodeURIComponent(r.radar_id)}`)}
                  className={`rounded-full border px-3 py-1 text-sm ${
                    r.radar_id === selected ? "border-accent bg-accent-soft text-accent" : "border-border text-muted hover:text-text"
                  }`}
                >
                  {r.title}
                </button>
              ))}
            </div>
          )}
          <Trends radarId={selected} />
        </>
      )}
    </>
  );
}

export default function Page() {
  return (
    <Suspense fallback={<Loading />}>
      <TrendsPage />
    </Suspense>
  );
}
