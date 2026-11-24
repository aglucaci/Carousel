"use client";

import Link from "next/link";
import { useRouter, useSearchParams } from "next/navigation";
import { Suspense, useMemo } from "react";
import { Badge, Empty, ErrorNote, Loading, PageHeader, fmtDate, sourceName } from "@/components/ui";
import { client, useApi, type ResearchEvent } from "@/lib/api";
import { GROUPS, WHO, kindOf } from "./groups";

/** One line saying what the event is, before its evidence page. */
function Summary({ e }: { e: ResearchEvent }) {
  const ev = e.evidence as Record<string, any>;
  switch (e.type) {
    case "new_publication":
      return (
        <>
          Published {fmtDate(ev.earliest_public_date)}; never seen by this radar before.
          {e.findings?.length ? (
            <span className="mt-1 block text-text">
              &ldquo;{e.findings[0].quote}&rdquo;{" "}
              <span className="text-muted">({e.findings[0].section ?? "abstract"})</span>
            </span>
          ) : null}
        </>
      );
    case "topic_acceleration":
      return (
        <>
          {Number(ev.rate_ratio).toFixed(1)}&times; its usual share of the field in {sourceName(ev.source)} (95% interval{" "}
          {Number(ev.ci?.[0]).toFixed(1)}&ndash;{ev.ci?.[1] == null ? "no upper bound" : Number(ev.ci[1]).toFixed(1)}), weeks through {fmtDate(ev.evaluated_through)};{" "}
          {ev.recent_count} papers where about {Math.round(ev.expected_recent)} were expected.
        </>
      );
    case "new_methodological_connection":
      return (
        <>
          In {ev.recent_papers_with_term} recent papers of the topic, and none of its {ev.earlier_papers_without_term} earlier
          ones.
        </>
      );
    case "research_landscape_shift":
      return <>After the topic model was refitted ({ev.relation}); {(e.papers ?? []).length} papers shown.</>;
    default: {
      const n = ev.notice;
      if (n) {
        return (
          <>
            Notice {n.notice_doi ?? "without a DOI"}
            {n.asserted_by && <> reported by {WHO[n.asserted_by] ?? n.asserted_by}</>}. Not known at the previous scan.
          </>
        );
      }
      if (ev.version) return <>Its {ev.version.role === "preprint" ? "preprint" : "published version"} is now linked.</>;
      return <>A source now marks it as retracted.</>;
    }
  }
}

function Events({ radarId }: { radarId: string }) {
  const { data, error, loading } = useApi(() => client.events(radarId), [radarId]);
  if (error) return <ErrorNote error={error} />;
  if (loading && !data) return <Loading />;
  if (!data?.length) {
    return (
      <Empty title="Nothing has changed yet">
        A radar&apos;s first scan is its baseline. From the next scan on, this page lists what changed: new findings,
        emerging methods, topic growth, new connections between topics, and updates to papers it has shown you. Topic changes
        need the radar&apos;s topics and a few weeks of counts.
      </Empty>
    );
  }
  return (
    <div className="space-y-8">
      {GROUPS.map((g) => {
        const items = data.filter((e) => g.types.includes(e.type));
        return (
          <section key={g.key} data-testid={`group-${g.key}`} aria-labelledby={`h-${g.key}`}>
            <h2 id={`h-${g.key}`} className="font-serif text-lg font-semibold">
              {g.title} <span className="text-sm font-normal text-muted">({items.length})</span>
            </h2>
            <p className="mb-2 text-sm text-muted">{g.help}</p>
            {items.length === 0 ? (
              <p className="text-sm text-muted">Nothing in this group yet.</p>
            ) : (
              <ul className="space-y-2">
                {items.map((e) => {
                  const k = kindOf(e);
                  return (
                    <li key={e.event_id} data-testid="event" className="rounded-lg border border-border bg-surface px-4 py-3 text-sm">
                      <p>
                        <Badge tone={k.tone}>{k.label}</Badge>{" "}
                        <Link
                          href={`/whats-new/${encodeURIComponent(e.event_id)}?radar=${encodeURIComponent(radarId)}`}
                          className="font-serif text-base hover:underline"
                        >
                          {e.title ?? e.paper_id}
                        </Link>
                        <span className="ml-2 text-xs text-muted">{fmtDate(e.date)}</span>
                      </p>
                      <p className="mt-1 text-muted">
                        <Summary e={e} />
                      </p>
                    </li>
                  );
                })}
              </ul>
            )}
          </section>
        );
      })}
    </div>
  );
}

function WhatsNew() {
  const params = useSearchParams();
  const router = useRouter();
  const { data: radars, error, loading } = useApi(() => client.radars(), []);
  const withRuns = useMemo(() => (radars ?? []).filter((r) => r.n_runs > 0), [radars]);
  const selected = params.get("radar") ?? withRuns[0]?.radar_id;
  if (error) return <ErrorNote error={error} />;
  if (loading && !radars) return <Loading />;
  return (
    <>
      <PageHeader title="What's New" subtitle="What changed in your radars' areas since earlier scans. Every item opens the evidence for it." />
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
                  onClick={() => router.push(`/whats-new?radar=${encodeURIComponent(r.radar_id)}`)}
                  className={`rounded-full border px-3 py-1 text-sm ${
                    r.radar_id === selected ? "border-accent bg-accent-soft text-accent" : "border-border text-muted hover:text-text"
                  }`}
                >
                  {r.title}
                </button>
              ))}
            </div>
          )}
          <Events radarId={selected} />
        </>
      )}
    </>
  );
}

export default function Page() {
  return (
    <Suspense fallback={<Loading />}>
      <WhatsNew />
    </Suspense>
  );
}
