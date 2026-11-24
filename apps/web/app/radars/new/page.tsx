"use client";

import { useRouter } from "next/navigation";
import { useState, type FormEvent } from "react";
import { ScanOptionsFields } from "@/components/Scan";
import { Badge, Button, Card, ErrorNote, PageHeader, fmtDate, sourceName } from "@/components/ui";
import { client, type Proposal, type ScanOptions, type Schedule } from "@/lib/api";

const lines = (s: string) =>
  s
    .split(/\n|;/)
    .map((x) => x.trim())
    .filter(Boolean);

const MODES = [
  { value: "focused", label: "Focused", help: "Closest matches first; little exploration." },
  { value: "balanced", label: "Balanced", help: "Relevance first, with room for novel work." },
  { value: "exploratory", label: "Exploratory", help: "More weight on novel and adjacent work." },
];

const SOURCES = [
  { value: "pubmed", label: "PubMed" },
  { value: "openalex", label: "OpenAlex" },
  { value: "crossref", label: "Crossref (checks corrections and retractions)" },
];
const SEARCHED = ["pubmed", "openalex"]; // Crossref verifies what these find, so at least one is needed

const field = "mt-1 w-full rounded-md border border-border bg-surface px-3 py-2 text-sm";

function Label({ children, hint }: { children: React.ReactNode; hint?: string }) {
  return (
    <span className="block text-sm font-medium">
      {children}
      {hint && <span className="ml-1 font-normal text-muted">{hint}</span>}
    </span>
  );
}

export default function NewRadarPage() {
  const router = useRouter();
  const [topic, setTopic] = useState("");
  const [question, setQuestion] = useState("");
  const [terms, setTerms] = useState("");
  const [exclude, setExclude] = useState("");
  const [methods, setMethods] = useState("");
  const [seeds, setSeeds] = useState("");
  const [sources, setSources] = useState<string[]>(["pubmed", "openalex", "crossref"]);
  const [windowDays, setWindowDays] = useState(90);
  const [mode, setMode] = useState("balanced");
  const [proposal, setProposal] = useState<Proposal | undefined>();
  const [scanNow, setScanNow] = useState(true);
  const [schedule, setSchedule] = useState<Schedule | "">("");
  const [scanOpts, setScanOpts] = useState<ScanOptions>({});
  const [error, setError] = useState<Error | undefined>();
  const [working, setWorking] = useState(false);

  async function propose(e: FormEvent) {
    e.preventDefault();
    setWorking(true);
    setError(undefined);
    try {
      setProposal(
        await client.propose({
          topic,
          research_question: question,
          terms: lines(terms),
          exclude: lines(exclude),
          methods: lines(methods),
          seed_papers: lines(seeds),
          sources,
          mode,
          window_days: windowDays,
        }),
      );
      window.scrollTo({ top: 0 });
    } catch (err) {
      setError(err as Error);
    } finally {
      setWorking(false);
    }
  }

  async function approve() {
    if (!proposal) return;
    setWorking(true);
    setError(undefined);
    try {
      const radar = await client.createRadar(proposal.config, schedule || null);
      const path = `/radars/${encodeURIComponent(radar.radar_id)}`;
      if (scanNow) {
        const job = await client.scan(radar.radar_id, scanOpts);
        router.push(`${path}?job=${encodeURIComponent(job.job_id)}`);
      } else {
        router.push(path);
      }
    } catch (err) {
      setError(err as Error);
      setWorking(false);
    }
  }

  if (proposal) {
    const c = proposal.config;
    return (
      <>
        <PageHeader title="Review the search strategy" subtitle="Nothing is saved or searched until you approve it." />
        <ErrorNote error={error} />
        <div className="space-y-4">
          <Card>
            <h2 className="font-serif text-lg font-semibold">{c.radar_name}</h2>
            {c.research_question && <p className="mt-1 text-sm text-muted">{c.research_question}</p>}
            <p className="mt-2 text-sm">
              Papers published from <strong>{fmtDate(proposal.window.start)}</strong> to{" "}
              <strong>{fmtDate(proposal.window.end)}</strong> (the last {proposal.window.days} days, moving with each scan).
            </p>
          </Card>
          <section aria-label="Searches" className="space-y-3">
            {proposal.strategy.map((s) => (
              <Card key={s.source}>
                <div className="flex items-center justify-between gap-2">
                  <h3 className="font-medium">{sourceName(s.source)}</h3>
                  {s.status === "ready" ? <Badge tone="good">Will be searched</Badge> : <Badge>Not available yet</Badge>}
                </div>
                {s.query && (
                  <pre data-testid={`query-${s.source}`} className="mt-2 overflow-x-auto whitespace-pre-wrap break-words rounded-md bg-surface-2 px-3 py-2 font-mono text-xs">
                    {s.query}
                  </pre>
                )}
                {s.filters && <p className="mt-2 text-xs text-muted">Filters: {s.filters}</p>}
                {s.notes.map((n) => (
                  <p key={n} className="mt-1 text-xs text-warn">
                    {n}
                  </p>
                ))}
              </Card>
            ))}
          </section>
          <Card>
            <h3 className="font-medium">Ranking</h3>
            <p className="mt-1 text-sm">
              {MODES.find((m) => m.value === proposal.ranking.mode)?.label} mode, using{" "}
              {Object.entries(proposal.ranking.weights)
                .map(([k, w]) => `${k.replace("_", " ")} (${w})`)
                .join(", ")}
              .
            </p>
            {(c.methods_profile.length > 0 || c.exclude.length > 0 || c.seed_papers.length > 0) && (
              <dl className="mt-2 grid grid-cols-[7rem_1fr] gap-1 text-sm">
                {c.methods_profile.length > 0 && (
                  <>
                    <dt className="text-muted">Methods</dt>
                    <dd>{c.methods_profile.join(", ")}</dd>
                  </>
                )}
                {c.exclude.length > 0 && (
                  <>
                    <dt className="text-muted">Excluded</dt>
                    <dd>{c.exclude.join(", ")}</dd>
                  </>
                )}
                {c.seed_papers.length > 0 && (
                  <>
                    <dt className="text-muted">Seed papers</dt>
                    <dd className="break-all">{c.seed_papers.join(", ")}</dd>
                  </>
                )}
              </dl>
            )}
            {proposal.notes.map((n) => (
              <p key={n} className="mt-1 text-xs text-muted">
                {n}
              </p>
            ))}
          </Card>
          <Card className="space-y-3">
            <div className="flex flex-wrap items-center gap-2 text-sm">
              <label htmlFor="schedule">Scan automatically</label>
              <select
                id="schedule"
                value={schedule}
                onChange={(e) => setSchedule(e.target.value as Schedule | "")}
                className="rounded-md border border-border bg-surface px-2 py-1.5"
              >
                <option value="">No, only when I start a scan</option>
                <option value="daily">Daily</option>
                <option value="weekly">Weekly (Mondays)</option>
              </select>
              <span className="text-xs text-muted">You can change this or pause it later.</span>
            </div>
            <label className="flex items-center gap-2 text-sm">
              <input type="checkbox" checked={scanNow} onChange={(e) => setScanNow(e.target.checked)} />
              Run the first scan now
            </label>
            {scanNow && <ScanOptionsFields value={scanOpts} onChange={setScanOpts} />}
          </Card>
          <div className="flex flex-wrap gap-2">
            <Button variant="primary" onClick={approve} disabled={working}>
              Approve and create radar
            </Button>
            <Button onClick={() => setProposal(undefined)} disabled={working}>
              Back to edit
            </Button>
          </div>
        </div>
      </>
    );
  }

  return (
    <>
      <PageHeader title="New radar" subtitle="Describe what to follow. CAROUSEL proposes the searches; you approve them." />
      <ErrorNote error={error} />
      <form onSubmit={propose} className="space-y-5">
        <Card className="space-y-4">
          <label className="block">
            <Label>Topic</Label>
            <input
              required
              minLength={2}
              maxLength={200}
              value={topic}
              onChange={(e) => setTopic(e.target.value)}
              placeholder="e.g. SARS-CoV-2 recombination"
              className={field}
            />
          </label>
          <label className="block">
            <Label hint="(optional)">Research question</Label>
            <textarea
              rows={2}
              value={question}
              onChange={(e) => setQuestion(e.target.value)}
              placeholder="What do you want to learn? It shapes relevance, not the searches."
              className={field}
            />
          </label>
        </Card>
        <details className="rounded-lg border border-border bg-surface p-4">
          <summary className="cursor-pointer select-none text-sm font-medium">More options</summary>
          <div className="mt-4 grid gap-4 sm:grid-cols-2">
            <label className="block">
              <Label hint="(one per line; default: the topic)">Search terms</Label>
              <textarea rows={3} value={terms} onChange={(e) => setTerms(e.target.value)} className={field} />
            </label>
            <label className="block">
              <Label hint="(one per line)">Exclude</Label>
              <textarea rows={3} value={exclude} onChange={(e) => setExclude(e.target.value)} className={field} />
            </label>
            <label className="block">
              <Label hint="(one per line)">Methods to match</Label>
              <textarea
                rows={3}
                value={methods}
                onChange={(e) => setMethods(e.target.value)}
                placeholder={"single-cell RNA-seq\nphylogenetics"}
                className={field}
              />
            </label>
            <label className="block">
              <Label hint="(DOIs, one per line)">Seed papers</Label>
              <textarea rows={3} value={seeds} onChange={(e) => setSeeds(e.target.value)} className={field} />
            </label>
            <fieldset>
              <legend className="text-sm font-medium">Sources</legend>
              <div className="mt-1 flex flex-wrap gap-4">
                {SOURCES.map((s) => (
                  <label key={s.value} className="flex items-center gap-2 text-sm">
                    <input
                      type="checkbox"
                      checked={sources.includes(s.value)}
                      onChange={(e) =>
                        setSources((cur) => (e.target.checked ? [...cur, s.value] : cur.filter((x) => x !== s.value)))
                      }
                    />
                    {s.label}
                  </label>
                ))}
              </div>
              <p className="mt-1 text-xs text-muted">Europe PMC and preprint servers arrive later in Phase 2.</p>
            </fieldset>
            <div>
              <label htmlFor="window" className="block text-sm font-medium">
                Window
              </label>
              <select
                id="window"
                value={windowDays}
                onChange={(e) => setWindowDays(Number(e.target.value))}
                className={field}
              >
                {[7, 14, 30, 90, 180, 365].map((d) => (
                  <option key={d} value={d}>
                    Last {d} days
                  </option>
                ))}
              </select>
            </div>
            <fieldset className="sm:col-span-2">
              <legend className="text-sm font-medium">Mode</legend>
              <div className="mt-1 grid gap-2 sm:grid-cols-3">
                {MODES.map((m) => (
                  <label
                    key={m.value}
                    className={`flex cursor-pointer gap-2 rounded-md border p-2 text-sm ${
                      mode === m.value ? "border-accent bg-accent-soft" : "border-border"
                    }`}
                  >
                    <input type="radio" name="mode" value={m.value} checked={mode === m.value} onChange={() => setMode(m.value)} />
                    <span>
                      <span className="font-medium">{m.label}</span>
                      <span className="block text-xs text-muted">{m.help}</span>
                    </span>
                  </label>
                ))}
              </div>
            </fieldset>
          </div>
        </details>
        <Button type="submit" variant="primary" disabled={working || topic.trim().length < 2 || !sources.some((s) => SEARCHED.includes(s))}>
          Propose search strategy
        </Button>
      </form>
    </>
  );
}
