"use client";

import { Fragment, useEffect, useRef, useState } from "react";
import { client, useApi, type Explanation, type GraphFeature, type GraphLink, type PaperExtraction, type PaperVersion } from "@/lib/api";
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
  citation: "cites your seed or saved papers (OpenAlex)",
  method: "names one of the radar's methods (Method Transfer)",
};

function channel(c: string) {
  return CHANNELS[c] ?? c;
}

function GraphLine({ g }: { g: GraphFeature }) {
  const who = (l: GraphLink) => `“${l.title ?? l.paper_id}” (${l.anchor === "seed" ? "seed paper" : "you saved or marked it relevant"})`;
  if (g.status !== "ok") {
    return <p className="mt-1 text-muted">Citation links: no data for this paper ({g.status === "no_openalex_id" ? "not in OpenAlex" : "its references were not read"}).</p>;
  }
  const parts = [
    ...(g.cites ?? []).map((l) => `cites ${who(l)}`),
    ...(g.cited_by ?? []).map((l) => `is cited by ${who(l)}`),
  ];
  if (!parts.length && g.shared_references) parts.push(`shares ${g.shared_references} reference${g.shared_references === 1 ? "" : "s"} with your seed and saved papers`);
  return (
    <p className="mt-1" data-testid="graph-line">
      Citation links: {parts.length ? parts.join("; ") : "none to your seed or saved papers"} (graph term <strong>{num(g.value)}</strong>).
    </p>
  );
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

      <Section title="In short">
        {e.explanation ? (
          <>
            <ul data-testid="explanation" className="list-disc space-y-1 pl-5">
              {e.explanation.reasons.map((r, i) => (
                <li key={i}>{r.text}</li>
              ))}
            </ul>
            <p className="mt-2 text-xs text-muted">
              Explanation version {e.explanation.version}, written when this run ranked the paper and stored with it; it is
              never regenerated.
            </p>
          </>
        ) : (
          <p className="text-muted">
            This run was recorded before explanations were versioned. The details below come from its stored features.
          </p>
        )}
      </Section>

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
        {d.graph && <GraphLine g={d.graph} />}
      </Section>

      {d.transfer?.status === "transfer" && (
        <Section title="Method transfer: a hypothesis">
          <p data-testid="transfer">
            It uses <strong>{d.transfer.method}</strong>, one of this radar&apos;s methods, in {d.transfer.field}, a field far from
            this radar&apos;s own papers (field distance <strong>{num(d.transfer.distance)}</strong>). It may apply to{" "}
            {d.transfer.question ? <>your question “{d.transfer.question}”</> : "this radar's work"}: an idea to consider, not a
            finding.
          </p>
          <blockquote className="mt-2 border-l-2 border-border pl-2 text-muted" data-testid="transfer-quote">
            &ldquo;{d.transfer.quote}&rdquo; ({d.transfer.section ?? "abstract"}; quote checked against the paper&apos;s text)
          </blockquote>
        </Section>
      )}

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

const REUSE: Record<string, string> = {
  open: "reuse allowed",
  noncommercial: "non-commercial reuse",
  no_derivatives: "no derivatives",
  restricted: "restricted",
  unknown: "licence not stated",
};

function licenceName(l?: string | null) {
  if (!l) return "no licence stated";
  return l.startsWith("cc") ? l.toUpperCase().replace(/^CC-/, "CC ").replace(/-(\d)/, " $1") : l;
}

const EXTRACTION_FIELDS: [string, string][] = [
  ["objective", "Objective"],
  ["study_design", "Study design"],
  ["population", "Population"],
  ["sample_size", "Sample size"],
  ["methods", "Methods"],
  ["findings", "Findings"],
  ["limitations", "Limitations"],
  ["data_availability", "Data availability"],
];
const PROVIDERS: Record<string, string> = { rules: "rule-based extraction", anthropic: "Claude" };

/** Grounded extraction: each claim with the passage it quotes (current record, not from the stored run). */
function Extracted({ paperId }: { paperId: string }) {
  const { data } = useApi(() => client.paper(paperId), [paperId]);
  const e: PaperExtraction | undefined = (data?.extractions ?? []).find((x) => x.provider !== "rules") ?? data?.extractions?.[0];
  if (!e) return null;
  const section = Object.fromEntries(e.passages.map((p) => [p.id, p.section]));
  const missing = EXTRACTION_FIELDS.filter(([k]) => e.fields[k]?.status !== "reported").map(([, label]) => label);
  return (
    <Section title="Extracted facts (current record)">
      <div data-testid="extraction" className="space-y-3">
        <p className={e.coverage.scope === "abstract_only" ? "rounded-md bg-accent-soft px-3 py-2 text-sm" : "text-sm text-muted"}>
          {e.coverage.statement}
        </p>
        <dl className="space-y-2">
          {EXTRACTION_FIELDS.filter(([k]) => e.fields[k]?.status === "reported").map(([k, label]) => (
            <div key={k}>
              <dt className="text-xs font-medium uppercase tracking-wide text-muted">{label}</dt>
              {e.fields[k].claims.map((c, i) => (
                <dd key={i} className="mt-1">
                  {c.evidence.length === 1 && c.evidence[0].quote === c.text ? null : <p>{c.text}</p>}
                  {c.evidence.map((ev) => (
                    <blockquote key={ev.passage_id} className="mt-0.5 border-l-2 border-border pl-2 text-xs text-muted">
                      &ldquo;{ev.quote}&rdquo; <span className="whitespace-nowrap">({section[ev.passage_id] ?? ev.passage_id})</span>
                    </blockquote>
                  ))}
                </dd>
              ))}
            </div>
          ))}
        </dl>
        {missing.length > 0 && (
          <p className="text-sm text-muted">
            Not reported{e.coverage.scope === "abstract_only" && e.coverage.passages > 1 ? " in the abstract" : ""}:{" "}
            {missing.join(", ").toLowerCase()}.
          </p>
        )}
        <p className="text-xs text-muted">
          {PROVIDERS[e.provider] ?? e.provider} ({e.model_version}, schema {e.schema_version}
          {e.location === "local" ? ", ran locally" : ", external model"}). Every claim quotes its passage verbatim
          {e.rejected > 0 ? `; ${e.rejected} claim${e.rejected === 1 ? "" : "s"} that didn't were dropped` : ""}.
        </p>
      </div>
    </Section>
  );
}

const ROLE: Record<string, string> = { preprint: "Preprint", published: "Published version" };

function linkedBy(v: PaperVersion) {
  const how = v.linked_by.map((m) => (m === "manual" ? "a reviewer" : sourceName(m)));
  const matched = (v.evidence ?? []).some((e) => e.asserted_by === "title_author_match");
  return `Linked by ${how.join(" and ")}${matched ? " (title and first-author match)" : ""}`;
}

/** A preprint and its publication: separate papers that point at each other (current, not from the stored run). */
function Versions({ paperId }: { paperId: string }) {
  const { data } = useApi(() => client.paper(paperId), [paperId]);
  const versions = data?.versions ?? [];
  if (!versions.length) return null;
  return (
    <Section title="Other versions (current record)">
      <ul data-testid="paper-versions" className="space-y-2">
        {versions.map((v) => {
          const href = v.doi ? `https://doi.org/${v.doi}` : v.url;
          return (
            <li key={v.paper_id}>
              <p>
                <Badge tone={v.role === "published" ? "accent" : "neutral"}>{ROLE[v.role] ?? v.role}</Badge>{" "}
                {href ? (
                  <a href={href} target="_blank" rel="noreferrer" className="text-accent hover:underline">
                    {v.title ?? v.paper_id}
                  </a>
                ) : (
                  (v.title ?? v.paper_id)
                )}{" "}
                {v.is_retracted && <Flag flag="retracted" />}
              </p>
              <p className="text-xs text-muted">
                {[v.venue, fmtDate(v.earliest_public_date), `${linkedBy(v)}; kept as its own paper`]
                  .filter(Boolean)
                  .join(" · ")}
              </p>
            </li>
          );
        })}
      </ul>
    </Section>
  );
}

/** The paper record's access, licences, corrections, and annotations (current, not from the stored run). */
function Access({ paperId }: { paperId: string }) {
  const { data } = useApi(() => client.paper(paperId), [paperId]);
  if (!data) return null;
  const texts = data.texts ?? [];
  const updates = data.updates ?? [];
  const notes = data.annotations ?? [];
  if (!data.license && data.open_access == null && !texts.length && !updates.length && !notes.length) return null;
  return (
    <Section title="Access and annotations (current record)">
      <dl data-testid="paper-access" className="grid grid-cols-[8rem_1fr] gap-x-3 gap-y-2">
        <dt className="text-muted">Licence</dt>
        <dd>
          {licenceName(data.license)}
          {data.open_access != null && <span className="text-muted"> · {data.open_access ? "open access" : "not open access"}</span>}
        </dd>
        {texts.map((t) => (
          <Fragment key={`${t.source}:${t.source_id}`}>
            <dt className="text-muted">Full text kept</dt>
            <dd>
              {t.url ? (
                <a href={t.url} target="_blank" rel="noreferrer" className="text-accent hover:underline">
                  {t.source_id}
                </a>
              ) : (
                t.source_id
              )}{" "}
              under {licenceName(t.license)} ({REUSE[t.reuse] ?? t.reuse}; licence read from the{" "}
              {t.license_source === "text" ? "article itself" : "source's metadata"}) · {t.sections.length} sections
            </dd>
          </Fragment>
        ))}
        {updates.length > 0 && (
          <>
            <dt className="text-muted">Updates</dt>
            <dd className="space-y-0.5">
              {updates.map((u, i) => (
                <p key={i}>
                  {u.type.replaceAll("_", " ")}
                  {u.date && ` (${fmtDate(u.date)})`}
                  {u.asserted_by && <span className="text-muted"> · reported by {u.asserted_by}</span>}
                  {u.via && <span className="text-muted"> via {sourceName(u.via)}</span>}
                </p>
              ))}
            </dd>
          </>
        )}
        {notes.length > 0 && (
          <>
            <dt className="text-muted">Mentions</dt>
            <dd className="flex flex-wrap gap-1">
              {notes.slice(0, 10).map((a) => (
                <Badge key={`${a.type}:${a.name}`} title={`${a.type}, ${a.mentions} mention${a.mentions === 1 ? "" : "s"}`}>
                  {a.name}
                </Badge>
              ))}
            </dd>
          </>
        )}
      </dl>
    </Section>
  );
}

function Judge({ feedback, busy, onJudge }: { feedback: string[]; busy?: boolean; onJudge: (a: "relevant" | "irrelevant") => void }) {
  const relevant = feedback.includes("relevant");
  const irrelevant = feedback.includes("irrelevant");
  return (
    <div data-testid="judge" className="mb-4 rounded-lg border border-border px-4 py-3 text-sm">
      <div className="flex flex-wrap items-center gap-2">
        <span className="mr-1">Right for this radar?</span>
        <Button variant={relevant ? "primary" : "secondary"} aria-pressed={relevant} disabled={busy} onClick={() => onJudge("relevant")}>
          Relevant
        </Button>
        <Button variant={irrelevant ? "primary" : "secondary"} aria-pressed={irrelevant} disabled={busy} onClick={() => onJudge("irrelevant")}>
          Not relevant
        </Button>
      </div>
      <p className="mt-1 text-xs text-muted">
        {relevant || irrelevant
          ? "Applied from the next scan: the radar's profile moves " + (relevant ? "toward" : "away from") + " papers like this one, and this paper leaves the list."
          : "Either answer applies from the next scan: it moves the radar's interest profile, and this paper leaves the list."}
      </p>
    </div>
  );
}

export function WhyDrawer({
  paperId,
  radarId,
  runId,
  feedback,
  busy,
  onJudge,
  onClose,
}: {
  paperId: string;
  radarId: string;
  runId?: string;
  feedback?: string[];
  busy?: boolean;
  onJudge?: (action: "relevant" | "irrelevant") => void;
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
        {onJudge && <Judge feedback={feedback ?? []} busy={busy} onJudge={onJudge} />}
        {error ? <p className="text-sm text-bad">{error.message}</p> : data ? <Body e={data} /> : <Loading />}
        {data && (
          <div className="mt-4 text-sm">
            <Extracted paperId={paperId} />
            <Versions paperId={paperId} />
            <Access paperId={paperId} />
          </div>
        )}
      </aside>
    </div>
  );
}
