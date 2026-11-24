"use client";

import { useEffect, useRef, useState } from "react";
import { client, useApi, type Explanation } from "@/lib/api";
import { PaperMeta, PaperTitle } from "./PaperCard";
import { Badge, Button, FLAGS, Flag, Loading, coverageLine, fmtDate, fmtDateTime, sourceName } from "./ui";

const SIGNAL_LABELS: Record<string, string> = {
  relevance: "Relevance",
  novelty: "Novelty",
  method_match: "Method match",
  momentum: "Momentum",
  discovery: "Discovery",
  evidence: "Evidence",
};

const CHANNELS: Record<string, string> = {
  lexical: "keyword match (BM25)",
  semantic: "meaning match (embeddings)",
  source_query: "a source's own search",
};

function channel(c: string) {
  return CHANNELS[c] ?? c;
}

function num(v?: number | null, digits = 2) {
  return v == null ? "—" : v.toFixed(digits);
}

function Section({ title, children }: { title: string; children: React.ReactNode }) {
  return (
    <section className="border-t border-border pt-4">
      <h3 className="mb-2 text-xs font-semibold uppercase tracking-wider text-muted">{title}</h3>
      {children}
    </section>
  );
}

function Terms({ items }: { items?: string[] }) {
  if (!items?.length) return <span className="text-muted">none</span>;
  return (
    <span className="flex flex-wrap gap-1">
      {items.map((t) => (
        <Badge key={t}>{t}</Badge>
      ))}
    </span>
  );
}

function Nearest({ ids }: { ids: string[] }) {
  const [titles, setTitles] = useState<Record<string, string>>({});
  useEffect(() => {
    let stop = false;
    Promise.all(ids.slice(0, 3).map((id) => client.paper(id).then((p) => [id, p.title ?? id] as const).catch(() => [id, id] as const))).then(
      (pairs) => !stop && setTitles(Object.fromEntries(pairs)),
    );
    return () => {
      stop = true;
    };
  }, [ids]);
  return (
    <ul className="mt-1 list-disc space-y-0.5 pl-5 text-muted">
      {ids.slice(0, 3).map((id) => (
        <li key={id}>{titles[id] ?? "…"}</li>
      ))}
    </ul>
  );
}

function Body({ e }: { e: Explanation }) {
  const d = e.details;
  const signals = Object.keys(e.weights).filter((k) => e.weights[k] > 0);
  const values: Record<string, number | null | undefined> = { ...e.features };
  return (
    <div className="space-y-4 text-sm">
      {d.paper && (
        <div>
          <PaperTitle paper={d.paper} as="h3" />
          <PaperMeta paper={d.paper} />
        </div>
      )}
      <p>
        Ranked <strong>#{e.rank}</strong> with a score of <strong>{e.score.toFixed(3)}</strong> in the{" "}
        <strong>{e.mode}</strong> mode. The score sets this radar&apos;s priorities; it does not rate the paper&apos;s
        quality.
      </p>

      <Section title="Score">
        <table className="w-full text-left tabular-nums">
          <thead className="text-xs text-muted">
            <tr>
              <th className="py-1 font-normal">Signal</th>
              <th className="py-1 font-normal">Value</th>
              <th className="py-1 font-normal">Weight</th>
              <th className="py-1 font-normal">Contribution</th>
            </tr>
          </thead>
          <tbody>
            {signals.map((k) => (
              <tr key={k} className="border-t border-border">
                <td className="py-1">{SIGNAL_LABELS[k] ?? k}</td>
                <td className="py-1">{num(values[k])}</td>
                <td className="py-1">{num(e.weights[k])}</td>
                <td className="py-1">{values[k] == null ? "—" : (values[k]! * e.weights[k]).toFixed(3)}</td>
              </tr>
            ))}
          </tbody>
        </table>
        <p className="mt-2 text-xs text-muted">
          The selection then balances score against similarity to papers already chosen (MMR), so the order can differ
          slightly from the score alone.
        </p>
        {Object.keys(e.inactive_signals).length > 0 && (
          <div className="mt-2 text-xs text-muted">
            <p>Not used in this run:</p>
            <ul className="list-disc pl-5">
              {Object.entries(e.inactive_signals).map(([k, why]) => (
                <li key={k}>
                  {SIGNAL_LABELS[k] ?? k}: {why}
                </li>
              ))}
            </ul>
          </div>
        )}
      </Section>

      <Section title="How it was found">
        <dl className="grid grid-cols-[8rem_1fr] gap-x-3 gap-y-2">
          <dt className="text-muted">Found by</dt>
          <dd>{(d.retrieved_by ?? []).map(channel).join(", ") || "—"}</dd>
          <dt className="text-muted">Sources</dt>
          <dd>{(d.sources ?? []).map(sourceName).join(", ") || "—"}</dd>
          <dt className="text-muted">Matched terms</dt>
          <dd>
            <Terms items={d.matched_terms} />
          </dd>
          <dt className="text-muted">Methods</dt>
          <dd>
            <Terms items={d.methods_matched} />
          </dd>
          {d.signals_matched && Object.keys(d.signals_matched).length > 0 && (
            <>
              <dt className="text-muted">Themes</dt>
              <dd className="space-y-1">
                {Object.entries(d.signals_matched).map(([theme, terms]) => (
                  <div key={theme}>
                    <span className="mr-1">{theme.replaceAll("_", " ")}:</span>
                    <Terms items={terms} />
                  </div>
                ))}
              </dd>
            </>
          )}
        </dl>
      </Section>

      <Section title="Relevance">
        <p>
          Meaning similarity to the radar&apos;s interest profile <strong>{num(d.similarity)}</strong>; keyword score{" "}
          <strong>{num(d.bm25_norm)}</strong> of this run&apos;s best.
        </p>
      </Section>

      <Section title="Novelty">
        {d.novelty_raw == null ? (
          <p className="text-muted">
            Not judged: too few papers were seen before this one ({d.novelty?.history_size ?? 0} so far).
          </p>
        ) : (
          <div className="space-y-1">
            <p>
              Distance from the {d.novelty?.history_size?.toLocaleString()} papers observed <em>before</em> it:{" "}
              <strong>{num(d.novelty_raw)}</strong>
              {d.novelty?.normalization?.startsWith("percentile_within:") &&
                `, ranked within ${d.novelty.normalization.split(":")[1]}s`}
              . Credited at {Math.round((d.novelty_gate ?? 0) * 100)}% because novelty counts in proportion to relevance.
            </p>
            {d.novelty?.nearest && d.novelty.nearest.length > 0 && (
              <div>
                <p className="text-muted">Closest earlier papers:</p>
                <Nearest ids={d.novelty.nearest} />
              </div>
            )}
          </div>
        )}
      </Section>

      {e.flags.length > 0 && (
        <Section title="Flags">
          <ul className="space-y-1">
            {e.flags.map((f) => (
              <li key={f} className="flex gap-2">
                <Flag flag={f} />
                <span className="text-muted">{FLAGS[f]?.help}</span>
              </li>
            ))}
          </ul>
        </Section>
      )}

      <Section title="This run">
        <ul className="space-y-0.5 text-muted">
          {e.source_coverage.map((c) => (
            <li key={c.source}>{coverageLine(c)}</li>
          ))}
        </ul>
        <p className="mt-2 text-xs text-muted">
          From the stored run <code className="font-mono">{e.run_id}</code> ({fmtDateTime(e.executed_at)}), ranking{" "}
          {e.ranking_version}, embeddings {e.model_version ?? "none"}. Nothing here is recomputed. First seen{" "}
          {fmtDate(d.first_seen)}.
        </p>
      </Section>
    </div>
  );
}

export function WhyDrawer({
  paperId,
  radarId,
  runId,
  onClose,
}: {
  paperId: string;
  radarId: string;
  runId?: string;
  onClose: () => void;
}) {
  const { data, error } = useApi(() => client.explanation(paperId, radarId, runId), [paperId, radarId, runId]);
  const closeRef = useRef<HTMLButtonElement>(null);
  useEffect(() => {
    closeRef.current?.focus();
    const onKey = (ev: KeyboardEvent) => ev.key === "Escape" && onClose();
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [onClose]);
  return (
    <div className="fixed inset-0 z-40 flex justify-end">
      <div className="absolute inset-0 bg-black/30" onClick={onClose} aria-hidden />
      <aside
        role="dialog"
        aria-modal="true"
        aria-labelledby="why-title"
        className="relative h-full w-full max-w-xl overflow-y-auto border-l border-border bg-surface p-6 shadow-xl"
      >
        <div className="mb-4 flex items-center justify-between gap-3">
          <h2 id="why-title" className="font-serif text-xl font-semibold">
            Why this paper?
          </h2>
          <Button ref={closeRef} variant="ghost" onClick={onClose} aria-label="Close">
            Close
          </Button>
        </div>
        {error ? <p className="text-sm text-bad">{error.message}</p> : data ? <Body e={data} /> : <Loading />}
      </aside>
    </div>
  );
}
