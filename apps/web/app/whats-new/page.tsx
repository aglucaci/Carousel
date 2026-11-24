"use client";

import Link from "next/link";
import { useRouter, useSearchParams } from "next/navigation";
import { Suspense, useMemo } from "react";
import { Badge, Empty, ErrorNote, Loading, PageHeader, fmtDate, sourceName } from "@/components/ui";
import { client, useApi, type ResearchEvent } from "@/lib/api";

const KIND: Record<string, { label: string; tone: "accent" | "warn" | "bad" | "neutral" }> = {
  new_publication: { label: "New publication", tone: "accent" },
  retraction: { label: "Retracted", tone: "bad" },
  removal: { label: "Removed", tone: "bad" },
  withdrawal: { label: "Withdrawn", tone: "bad" },
  expression_of_concern: { label: "Expression of concern", tone: "bad" },
  correction: { label: "Corrected", tone: "warn" },
  erratum: { label: "Erratum", tone: "warn" },
  partial_retraction: { label: "Partially retracted", tone: "bad" },
  new_version: { label: "New version linked", tone: "neutral" },
};

function kindOf(e: ResearchEvent) {
  const k = e.type === "new_publication" ? "new_publication" : (e.kind ?? "revision");
  return KIND[k] ?? { label: k.replaceAll("_", " "), tone: "warn" as const };
}

/** The evidence an event rests on, in words, with links a reader can check. */
function Evidence({ e }: { e: ResearchEvent }) {
  const ev = e.evidence as Record<string, any>;
  if (e.type === "new_publication") {
    return (
      <>
        Published {fmtDate(ev.earliest_public_date)}, within the window {fmtDate(ev.window?.start)} – {fmtDate(ev.window?.end)},
        and never seen by this radar before (found by {(ev.sources ?? []).map(sourceName).join(", ") || "its sources"}; ranked #
        {ev.rank}).
      </>
    );
  }
  if (ev.notice) {
    const n = ev.notice;
    return (
      <>
        Notice{" "}
        {n.notice_doi ? (
          <a href={`https://doi.org/${n.notice_doi}`} target="_blank" rel="noreferrer" className="text-accent hover:underline">
            {n.notice_doi}
          </a>
        ) : (
          "without a DOI"
        )}
        {n.asserted_by && <> reported by {n.asserted_by === "retraction-watch" ? "Retraction Watch" : n.asserted_by}</>}
        {n.via && <> via {sourceName(n.via)}</>}. Not known at the previous scan.
      </>
    );
  }
  if (ev.version) {
    return (
      <>
        Its {ev.version.role === "preprint" ? "preprint" : "published version"} is now linked (by{" "}
        {(ev.version.linked_by ?? []).map((m: string) => (m === "manual" ? "a reviewer" : sourceName(m))).join(", ")}); both
        stay separate papers.
      </>
    );
  }
  return <>A source now marks it as retracted.</>;
}

function Events({ radarId }: { radarId: string }) {
  const { data, error, loading } = useApi(() => client.events(radarId), [radarId]);
  if (error) return <ErrorNote error={error} />;
  if (loading && !data) return <Loading />;
  if (!data?.length) {
    return (
      <Empty title="Nothing has changed yet">
        A radar&apos;s first scan is its baseline. From the next scan on, this page lists new publications (never seen by the
        radar and published in its window) and revisions to papers it has shown you: corrections, retractions, and newly
        linked versions.
      </Empty>
    );
  }
  const days = [...new Set(data.map((e) => e.date))];
  return (
    <div className="space-y-6">
      {days.map((day) => (
        <section key={day}>
          <h2 className="mb-2 text-xs font-semibold uppercase tracking-wider text-muted">Scan of {fmtDate(day)}</h2>
          <ul className="space-y-2">
            {data
              .filter((e) => e.date === day)
              .map((e) => {
                const k = kindOf(e);
                const href = e.doi ? `https://doi.org/${e.doi}` : e.url;
                return (
                  <li key={e.event_id} data-testid="event" className="rounded-lg border border-border bg-surface px-4 py-3 text-sm">
                    <p>
                      <Badge tone={k.tone}>{k.label}</Badge>{" "}
                      {href ? (
                        <a href={href} target="_blank" rel="noreferrer" className="font-serif text-base hover:underline">
                          {e.title ?? e.paper_id}
                        </a>
                      ) : (
                        <span className="font-serif text-base">{e.title ?? e.paper_id}</span>
                      )}
                    </p>
                    <p className="mt-1 text-muted">
                      <Evidence e={e} />
                    </p>
                    {e.run_id && (
                      <p className="mt-1 text-xs text-muted">
                        From the stored run{" "}
                        <Link href={`/?radar=${encodeURIComponent(radarId)}&run=${encodeURIComponent(e.run_id)}`} className="font-mono hover:underline">
                          {e.run_id}
                        </Link>
                      </p>
                    )}
                  </li>
                );
              })}
          </ul>
        </section>
      ))}
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
      <PageHeader title="What's New" subtitle="What changed in your radars' areas since earlier scans, with the evidence for each change." />
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
