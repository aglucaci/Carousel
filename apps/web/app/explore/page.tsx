"use client";

import { useRouter, useSearchParams } from "next/navigation";
import { Suspense, useMemo, useState } from "react";
import { LiteratureGraph, describe, roleText, shortTitle } from "@/components/LiteratureGraph";
import { Button, Card, Empty, ErrorNote, Loading, PageHeader, fmtDate, fmtDateTime } from "@/components/ui";
import { client, useApi, type ExploreGraph, type ExploreNode } from "@/lib/api";

/** How one paper is connected: every stored link it has, each naming the other paper (a button that selects it). */
function Connections({ g, n, onSelect }: { g: ExploreGraph; n: ExploreNode; onSelect: (id: string) => void }) {
  const byId = Object.fromEntries(g.nodes.map((x) => [x.paper_id, x]));
  const links = g.links.filter((l) => l.source === n.paper_id || l.target === n.paper_id);
  if (!links.length) {
    return (
      <p className="text-sm text-muted">
        {n.citation_data
          ? "No citation links or shared references with the other papers here."
          : "Its references have not been read (it has no OpenAlex record), so its links are unknown."}
      </p>
    );
  }
  return (
    <ul className="space-y-1 text-sm" data-testid="connections">
      {links.map((l) => {
        const other = l.source === n.paper_id ? l.target : l.source;
        return (
          <li key={`${l.kind}:${l.source}:${l.target}`} data-testid="connection" data-kind={l.kind} data-other={other}>
            <button type="button" className="text-left hover:underline focus-visible:underline" onClick={() => onSelect(other)}>
              {describe(l, n.paper_id, byId[other])}
            </button>
          </li>
        );
      })}
    </ul>
  );
}

function Selected({ g, id, onSelect }: { g: ExploreGraph; id: string | null; onSelect: (id: string) => void }) {
  const n = g.nodes.find((x) => x.paper_id === id);
  if (!n) return <p className="text-sm text-muted">Select a paper (click it, or Tab to it and press Enter) to see how it is connected.</p>;
  return (
    <div className="space-y-2" data-testid="selected" aria-live="polite">
      <h2 className="font-serif text-lg">
        {n.doi ? (
          <a href={`https://doi.org/${n.doi}`} target="_blank" rel="noreferrer" className="hover:underline">
            {n.title ?? n.paper_id}
          </a>
        ) : (
          (n.title ?? n.paper_id)
        )}
      </h2>
      <p className="text-sm text-muted">
        {roleText(n)} · {n.topic_label ? `topic “${n.topic_label}”` : "no topic"}
        {n.date ? ` · ${fmtDate(n.date)}` : ""}
      </p>
      <Connections g={g} n={n} onSelect={onSelect} />
    </div>
  );
}

function Graph({ radarId }: { radarId: string }) {
  const { data, error, loading } = useApi(() => client.graph(radarId), [radarId]);
  const [selected, setSelected] = useState<string | null>(null);
  const [list, setList] = useState(false);
  if (error) return <ErrorNote error={error} />;
  if (loading && !data) return <Loading />;
  if (!data) return null;
  if (!data.nodes.length) return <Empty title="Nothing to draw yet">Run a scan of this radar first.</Empty>;
  const s = data.stats;
  return (
    <div className="space-y-4">
      <p className="text-sm text-muted" data-testid="graph-scope">
        {s.nodes} papers: the latest run&apos;s recommendations and the radar&apos;s seed and saved papers. {s.citation_links} citation
        link{s.citation_links === 1 ? "" : "s"} and {s.shared_reference_links} shared-reference link
        {s.shared_reference_links === 1 ? "" : "s"} (at least {s.min_shared} references in common), as stored by{" "}
        {fmtDateTime(data.as_of)}
        {data.classifier_version ? `; topics ${data.classifier_version}` : "; no topic model yet"}.
        {s.without_citation_data ? ` ${s.without_citation_data} paper${s.without_citation_data === 1 ? " has" : "s have"} no reference list read.` : ""}
      </p>
      {!s.citation_links && !s.shared_reference_links && (
        <p className="rounded-md bg-accent-soft px-3 py-2 text-sm">
          No links yet. Citations are read for radars with seed papers and OpenAlex among their sources.
        </p>
      )}
      <div className="grid gap-4 lg:grid-cols-[minmax(0,3fr)_minmax(0,2fr)]">
        <Card>
          <LiteratureGraph graph={data} selected={selected} onSelect={setSelected} />
          <p className="mt-2 text-xs text-muted">
            Squares: seed papers; diamonds: saved or marked relevant; circles: the run&apos;s other papers, numbered by rank; dotted
            outline: references not read. Arrows point from the citing paper to the cited one; dashed lines join papers that
            share references. Papers are grouped by topic around the circle.
          </p>
        </Card>
        <Card>
          <Selected g={data} id={selected} onSelect={setSelected} />
        </Card>
      </div>
      <Button variant="ghost" aria-expanded={list} onClick={() => setList((v) => !v)}>
        {list ? "Hide list" : "Show as list"}
      </Button>
      {list && (
        <ol className="space-y-3" data-testid="graph-list">
          {data.nodes.map((n) => (
            <li key={n.paper_id} data-testid="graph-list-item" data-paper={n.paper_id} className="rounded-lg border border-border px-4 py-3">
              <p className="font-serif">{shortTitle(n, 160)}</p>
              <p className="mb-1 text-xs text-muted">
                {roleText(n)} · {n.topic_label ? `topic “${n.topic_label}”` : "no topic"}
              </p>
              <Connections g={data} n={n} onSelect={setSelected} />
            </li>
          ))}
        </ol>
      )}
    </div>
  );
}

function ExplorePage() {
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
        title="Explore"
        subtitle="How a radar's papers connect: who cites whom, which papers share references, and which topic each belongs to. Every link is a stored citation."
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
                  onClick={() => router.push(`/explore?radar=${encodeURIComponent(r.radar_id)}`)}
                  className={`rounded-full border px-3 py-1 text-sm ${
                    r.radar_id === selected ? "border-accent bg-accent-soft text-accent" : "border-border text-muted hover:text-text"
                  }`}
                >
                  {r.title}
                </button>
              ))}
            </div>
          )}
          <Graph key={selected} radarId={selected} />
        </>
      )}
    </>
  );
}

export default function Page() {
  return (
    <Suspense fallback={<Loading />}>
      <ExplorePage />
    </Suspense>
  );
}
