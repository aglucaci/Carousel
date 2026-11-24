"use client";

import Link from "next/link";
import { useParams, useSearchParams } from "next/navigation";
import { Suspense } from "react";
import { Badge, Card, ErrorNote, Loading, PageHeader, fmtDate, sourceName } from "@/components/ui";
import { client, useApi, type ResearchEvent } from "@/lib/api";
import { WHO, kindOf } from "../groups";

function pct(x?: number | null, digits = 1) {
  return x == null ? "—" : `${(100 * x).toFixed(digits)}%`;
}

function Row({ label, children }: { label: string; children: React.ReactNode }) {
  return (
    <>
      <dt className="text-muted">{label}</dt>
      <dd>{children}</dd>
    </>
  );
}

function Papers({ e }: { e: ResearchEvent }) {
  const papers = e.papers ?? [];
  return (
    <Card>
      <h2 className="mb-2 font-medium">Publications this rests on ({papers.length})</h2>
      <ul className="space-y-1 text-sm">
        {papers.map((p) => (
          <li key={p.paper_id} data-testid="evidence-paper">
            {p.doi ? (
              <a href={`https://doi.org/${p.doi}`} target="_blank" rel="noreferrer" className="text-accent hover:underline">
                {p.title ?? p.paper_id}
              </a>
            ) : (
              <span>{p.title ?? p.paper_id}</span>
            )}{" "}
            <span className="text-muted">
              {fmtDate(p.earliest_public_date)}
              {p.confidence != null && <> · topic similarity {p.confidence.toFixed(2)}</>} · <code className="font-mono text-xs">{p.paper_id}</code>
            </span>
          </li>
        ))}
      </ul>
    </Card>
  );
}

function Acceleration({ ev }: { ev: Record<string, any> }) {
  const base: Record<string, any> = Object.fromEntries((ev.baseline_share ?? []).map((b: any) => [b.week, b]));
  return (
    <>
      <Card>
        <dl className="grid grid-cols-[11rem_1fr] gap-x-3 gap-y-1 text-sm">
          <Row label="Rate ratio">
            {Number(ev.rate_ratio).toFixed(2)}&times; (95% interval {Number(ev.ci?.[0]).toFixed(2)}&ndash;
            {ev.ci?.[1] == null ? "no upper bound" : Number(ev.ci[1]).toFixed(2)})
          </Row>
          <Row label="Significance">
            q = {Number(ev.q_value).toFixed(4)} after Benjamini–Hochberg across the radar&apos;s topics (p = {Number(ev.p_value).toFixed(4)})
          </Row>
          <Row label="Recent weeks">
            {ev.recent_window?.[0]} to {ev.recent_window?.[1]}: {ev.recent_count} papers, about {Math.round(ev.expected_recent)} expected
          </Row>
          <Row label="Model">
            {ev.trend_model_version}, {ev.method === "negbin" ? "negative binomial" : ev.method}, {ev.baseline} baseline; counts from{" "}
            {sourceName(ev.source)}; topics {ev.classifier_version}
          </Row>
        </dl>
      </Card>
      <Card>
        <h2 className="mb-2 font-medium">Weekly counts</h2>
        <table className="w-full text-left text-sm tabular-nums">
          <thead className="text-xs text-muted">
            <tr>
              <th className="py-1 font-normal">Week from</th>
              <th className="py-1 font-normal">Topic</th>
              <th className="py-1 font-normal">Field</th>
              <th className="py-1 font-normal">Share</th>
              <th className="py-1 font-normal">Baseline share (95% band)</th>
            </tr>
          </thead>
          <tbody>
            {(ev.weekly ?? []).map((w: any) => (
              <tr key={w.week} data-testid="evidence-week" data-week={w.week} className="border-t border-border">
                <td className="py-1">{w.week}</td>
                <td className="py-1">{w.count}</td>
                <td className="py-1">{w.denominator}</td>
                <td className="py-1">{pct(w.share)}</td>
                <td className="py-1">
                  {base[w.week] ? (
                    <>
                      {pct(base[w.week].share)} ({pct(base[w.week].low)}–{pct(base[w.week].high)})
                    </>
                  ) : (
                    "—"
                  )}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
        <p className="mt-2 text-xs text-muted">Counts are as observed by {fmtDate(ev.as_of)}; each is a stored observation.</p>
      </Card>
      <Card>
        <h2 className="mb-2 font-medium">Checks passed</h2>
        <ul className="space-y-1 text-sm">
          {(ev.checks ?? []).map((c: any, i: number) => (
            <li key={i}>
              <Badge tone={c.status === "fired" ? "bad" : "good"}>{c.check.replaceAll("_", " ")}</Badge> <span className="text-muted">{c.detail}</span>
            </li>
          ))}
        </ul>
      </Card>
    </>
  );
}

function Detail({ e }: { e: ResearchEvent }) {
  const ev = e.evidence as Record<string, any>;
  const k = kindOf(e);
  return (
    <div className="space-y-4">
      <p>
        <Badge tone={k.tone}>{k.label}</Badge> <span className="text-sm text-muted">{fmtDate(e.date)} · {e.events_version}</span>
      </p>
      {e.type === "topic_acceleration" && <Acceleration ev={ev} />}
      {e.type === "new_methodological_connection" && (
        <Card>
          <p className="text-sm">
            <strong>{ev.method_term}</strong> appears in {ev.recent_papers_with_term} of this topic&apos;s papers from {ev.recent_window?.[0]} to{" "}
            {ev.recent_window?.[1]}, and in none of its {ev.earlier_papers_without_term} earlier papers (topics {ev.classifier_version}).
          </p>
        </Card>
      )}
      {e.type === "research_landscape_shift" && (
        <Card>
          <dl className="grid grid-cols-[11rem_1fr] gap-x-3 gap-y-1 text-sm">
            <Row label="Change">{ev.relation}</Row>
            <Row label="Topic model">
              {ev.from_version ?? "—"} → {ev.to_version}
            </Row>
            <Row label="Shared papers">{ev.shared_members}</Row>
            <Row label="Centroid similarity">{ev.centroid_similarity == null ? "—" : Number(ev.centroid_similarity).toFixed(3)}</Row>
          </dl>
        </Card>
      )}
      {e.type === "new_publication" && (
        <Card>
          <p className="text-sm">
            Published {fmtDate(ev.earliest_public_date)}, inside the window {fmtDate(ev.window?.start)} – {fmtDate(ev.window?.end)}; never
            seen by this radar before (ranked #{ev.rank}, found by {(ev.sources ?? []).map(sourceName).join(", ")}).
          </p>
          {e.findings?.length ? (
            <ul data-testid="findings" className="mt-3 space-y-2 text-sm">
              {e.findings.map((f, i) => (
                <li key={i}>
                  {f.text !== f.quote && <p>{f.text}</p>}
                  <blockquote className="border-l-2 border-border pl-2 text-muted">
                    &ldquo;{f.quote}&rdquo; ({f.section ?? "abstract"}; {f.coverage === "abstract_only" ? "from the abstract" : "from the full text"})
                  </blockquote>
                </li>
              ))}
            </ul>
          ) : null}
        </Card>
      )}
      {e.type === "publication_revision" && (
        <Card>
          <p className="text-sm">
            {ev.notice ? (
              <>
                Notice{" "}
                {ev.notice.notice_doi ? (
                  <a href={`https://doi.org/${ev.notice.notice_doi}`} target="_blank" rel="noreferrer" className="text-accent hover:underline">
                    {ev.notice.notice_doi}
                  </a>
                ) : (
                  "without a DOI"
                )}
                {ev.notice.asserted_by && <> reported by {WHO[ev.notice.asserted_by] ?? ev.notice.asserted_by}</>}
                {ev.notice.via && <> via {sourceName(ev.notice.via)}</>}. Not known at the previous scan.
              </>
            ) : ev.version ? (
              <>Its {ev.version.role === "preprint" ? "preprint" : "published version"} is now linked; both stay separate papers.</>
            ) : (
              <>A source now marks it as retracted.</>
            )}
          </p>
        </Card>
      )}
      <Papers e={e} />
      {e.run_id && (
        <p className="text-xs text-muted">
          Derived from stored data during run <code className="font-mono">{e.run_id}</code>.
        </p>
      )}
    </div>
  );
}

function EventPage() {
  const { id } = useParams<{ id: string }>();
  const radar = useSearchParams().get("radar");
  const { data, error, loading } = useApi(() => client.event(id), [id]);
  return (
    <>
      <PageHeader title={data?.title ?? "Event"} subtitle="The evidence for this change, from stored observations and papers." />
      <p className="mb-4 text-sm">
        <Link href={`/whats-new${radar ? `?radar=${encodeURIComponent(radar)}` : ""}`} className="text-accent hover:underline">
          ← What&apos;s New
        </Link>
      </p>
      {error ? <ErrorNote error={error} /> : loading && !data ? <Loading /> : data ? <Detail e={data} /> : null}
    </>
  );
}

export default function Page() {
  return (
    <Suspense fallback={<Loading />}>
      <EventPage />
    </Suspense>
  );
}
