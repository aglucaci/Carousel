/** Typed client for the CAROUSEL API v1 (see openapi/carousel-v1.json), through this app's /api/v1 proxy. */
"use client";

import { useCallback, useEffect, useState } from "react";

export type Coverage = {
  source: string;
  status: string;
  /** "verification": looked up the DOIs other sources found (Crossref), rather than searching. */
  role?: string;
  note?: string;
  error?: string;
  total_available?: number | null;
  retrieved?: number;
  truncated?: boolean;
  truncation_reason?: string | null;
  native_query?: string;
};

export type RunSummary = {
  run_id: string;
  radar_id: string;
  executed_at: string;
  finished_at?: string | null;
  as_of: string;
  status: string;
  ranking_version: string;
  model_version?: string | null;
  mode: string;
  config_hash: string;
  n_observed: number;
  n_candidates: number;
  n_items: number;
  coverage: Coverage[];
  notes: string[];
  trigger?: "manual" | "scheduled" | null;
  /** Version of the explanations stored with this run; null for runs recorded before versioning. */
  explanation_version?: string | null;
  /** What the radar's feedback changed, against the same run ranked without it; null without feedback. */
  feedback_effect?: FeedbackEffect | null;
  profile?: { feedback_positive?: number; feedback_negative?: number; shift?: number | null } | null;
};

export type FeedbackEffect = {
  entered: string[];
  left: string[];
  moved_up: number;
  moved_down: number;
  profile_shift?: number | null;
};

export type ExplanationReason = { kind: string; text: string; share?: number | null };
export type StoredExplanation = { version: string; summary: string; reasons: ExplanationReason[] };

export type RadarConfig = {
  radar_id: string;
  radar_name: string;
  description: string;
  research_question: string;
  sources: string[];
  queries: string[];
  exclude: string[];
  methods_profile: string[];
  seed_papers: string[];
  publication_types: string[];
  mode: string;
  filters: { date_range: string; language: string };
  [key: string]: unknown;
};

export type Schedule = "daily" | "weekly";

export type RadarPatch = { schedule?: Schedule | null; state?: "active" | "paused"; digest_emails?: string[] };

export type DigestDelivery = {
  id: number;
  radar_id: string;
  run_id?: string | null;
  channel: "email" | "rss" | "markdown";
  status: "delivered" | "failed";
  generated_at: string;
  recipient?: string | null;
  error?: string | null;
  characters?: number | null;
};

export type Radar = {
  radar_id: string;
  title: string;
  description: string;
  state: string;
  schedule?: Schedule | null;
  next_scan_at?: string | null;
  created_at: string;
  updated_at: string;
  config?: RadarConfig | null;
  n_runs: number;
  latest_run?: RunSummary | null;
  digest_emails?: string[];
};

export type PaperSummary = {
  title?: string | null;
  authors?: string[];
  venue?: string | null;
  earliest_public_date?: string | null;
  url?: string | null;
  doi?: string | null;
  pmid?: string | null;
  is_retracted?: boolean | null;
  [key: string]: unknown;
};

export type Features = { relevance?: number | null; novelty?: number | null; method_match?: number | null };

export type RankedItem = {
  rank: number;
  paper_id: string;
  score: number;
  features: Features;
  flags: string[];
  paper: PaperSummary;
  dismissed: boolean;
  saved: boolean;
  feedback: string[];
  /** Relative to the radar's earlier runs; null on its first run. */
  arrival?: Arrival | null;
  /** One-line summary of the stored explanation; null for runs recorded before versioning. */
  why?: string | null;
};

export type Arrival = "newly_published" | "newly_observed" | "seen_before";

export type RankedPapers = {
  run: RunSummary;
  items: RankedItem[];
  previous_run_id?: string | null;
  arrivals?: Record<Arrival, number> | null;
  /** Research Delta events this run produced, by label (new_publication, retraction, ...). */
  events?: Record<string, number>;
};

export type TrendWeek = { week: string; count: number; denominator: number; share: number | null; provisional?: boolean };
export type TrendTopic = {
  topic_id: string;
  label?: string | null;
  status: string;
  flagged: boolean;
  failed: string[];
  rate_ratio?: number | null;
  ci_low?: number | null;
  ci_high?: number | null;
  q_value?: number | null;
  recent_count: number;
  expected_recent?: number | null;
  method?: string | null;
  baseline?: string | null;
  weekly: TrendWeek[];
  baseline_share?: { week: string; share: number; low: number; high: number }[] | null;
  note?: string | null;
};
export type TrendReport = {
  source: string;
  as_of: string;
  trend_model_version: string;
  checks_version: string;
  evaluated_through: string;
  provisional_weeks: string[];
  checks: { check: string; status: string; detail: string }[];
  criteria: Record<string, number>;
  topics: TrendTopic[];
};
export type TopicTrends = {
  radar_id: string;
  classifier_version: string;
  as_of: string;
  fitted_at?: string | null;
  previous_version?: string | null;
  versions: string[];
  reports: TrendReport[];
};

export type ResearchEvent = {
  event_id: string;
  type:
    | "new_publication"
    | "publication_revision"
    | "topic_acceleration"
    | "new_methodological_connection"
    | "research_landscape_shift";
  kind?: string | null;
  radar_id: string;
  run_id?: string | null;
  date: string;
  paper_id?: string | null;
  title?: string | null;
  venue?: string | null;
  earliest_public_date?: string | null;
  doi?: string | null;
  url?: string | null;
  is_retracted?: boolean | null;
  evidence: Record<string, unknown>;
  events_version?: string | null;
  /** The publications the event rests on (at least one). */
  papers?: { paper_id: string; title?: string | null; doi?: string | null; earliest_public_date?: string | null; confidence?: number }[];
  /** New publications: findings from the grounded extraction, each with its quote. */
  findings?: { text: string; quote: string; section?: string | null; provider: string; coverage?: string | null }[];
};

export type Delta = {
  run_id: string;
  previous_run_id?: string | null;
  entered: string[];
  left: string[];
  moved: { paper_id: string; rank: number; previous_rank: number; change: number }[];
  newly_published: string[];
  newly_observed: string[];
};

export type Explanation = {
  explanation_version?: string | null;
  explanation?: StoredExplanation | null;
  paper_id: string;
  run_id: string;
  radar_id: string;
  rank: number;
  score: number;
  features: Features;
  details: {
    retrieved_by?: string[];
    similarity?: number | null;
    bm25?: number | null;
    bm25_norm?: number | null;
    matched_terms?: string[];
    novelty_raw?: number | null;
    novelty_gate?: number | null;
    novelty?: { history_size?: number; nearest?: string[]; normalization?: string };
    methods_matched?: string[];
    signals_matched?: Record<string, string[]>;
    publication_kind?: string;
    sources?: string[];
    first_seen?: string;
    earliest_public_date?: string | null;
    paper?: PaperSummary;
    graph?: GraphFeature;
    [key: string]: unknown;
  };
  flags: string[];
  ranking_version: string;
  model_version?: string | null;
  mode: string;
  weights: Record<string, number>;
  inactive_signals: Record<string, string>;
  source_coverage: Coverage[];
  executed_at: string;
};

export type GraphLink = { paper_id: string; title?: string | null; anchor: "seed" | "feedback" };
export type GraphFeature = {
  status: string;
  work?: string;
  cites?: GraphLink[];
  cited_by?: GraphLink[];
  shared_references?: number;
  value: number;
};

export type PaperUpdate = { type: string; notice_doi?: string | null; date?: string | null; asserted_by?: string | null; label?: string | null; via?: string | null };
export type PaperTextInfo = {
  source: string;
  source_id: string;
  kind: string;
  license?: string | null;
  license_url?: string | null;
  license_source: "text" | "metadata";
  reuse: "open" | "noncommercial" | "no_derivatives" | "restricted" | "unknown";
  url?: string | null;
  sections: string[];
  characters: number;
};
export type PaperAnnotation = { type: string; name: string; uri?: string | null; mentions: number; sections?: string[] };

export type PaperVersion = {
  paper_id: string;
  role: "preprint" | "published";
  title?: string | null;
  venue?: string | null;
  earliest_public_date?: string | null;
  doi?: string | null;
  url?: string | null;
  is_retracted?: boolean;
  linked_by: string[];
  evidence?: { method: string; relation?: string; asserted_by?: string; note?: string }[];
};

export type ExtractionClaim = { text: string; evidence: { passage_id: string; quote: string }[] };
export type PaperExtraction = {
  provider: string;
  model_version: string;
  schema_version: string;
  location: "local" | "external";
  coverage: { scope: "abstract_only" | "full_text" | "user_text"; statement: string; passages: number };
  fields: Record<string, { status: "reported" | "not_reported"; claims: ExtractionClaim[] }>;
  passages: { id: string; section: string; text: string }[];
  rejected: number;
  created_at: string;
};

export type Paper = {
  paper_id: string;
  title?: string | null;
  venue?: string | null;
  url?: string | null;
  identifiers: Record<string, string>;
  license?: string | null;
  open_access?: boolean | null;
  updates?: PaperUpdate[];
  texts?: PaperTextInfo[];
  annotations?: PaperAnnotation[];
  versions?: PaperVersion[];
  extractions?: PaperExtraction[];
};

export type StrategyStep = { source: string; status: string; query?: string | null; filters?: string | null; notes: string[] };

export type Proposal = {
  config: RadarConfig;
  window: { start: string; end: string; days: number };
  strategy: StrategyStep[];
  ranking: { mode: string; signals: string[]; weights: Record<string, number> };
  notes: string[];
};

export type ProposalInput = {
  topic: string;
  research_question?: string;
  terms?: string[];
  exclude?: string[];
  methods?: string[];
  seed_papers?: string[];
  sources?: string[];
  mode?: string;
  window_days?: number;
};

export type Job = {
  job_id: string;
  radar_id?: string | null;
  status: string;
  run_id?: string | null;
  error?: string | null;
  status_url: string;
};

export type ScanOptions = { top_n?: number; max_results?: number; as_of?: string; mode?: string };

export type LibraryItem = { radar_id: string; radar_title?: string | null; paper_id: string; saved_at: string; paper: PaperSummary };

export type FeedbackAction = "save" | "dismiss" | "relevant" | "irrelevant";

export class ApiError extends Error {
  constructor(public status: number, message: string) {
    super(message);
  }
}

function detail(body: unknown, status: number): string {
  const d = (body as { detail?: unknown } | null)?.detail;
  if (typeof d === "string") return d;
  if (Array.isArray(d)) return d.map((e) => `${(e.loc ?? []).slice(1).join(".")}: ${e.msg}`).join("; ");
  return `Request failed (${status})`;
}

export async function api<T>(path: string, init?: { method?: string; body?: unknown }): Promise<T> {
  const res = await fetch(`/api/v1${path}`, {
    method: init?.method ?? "GET",
    headers: init?.body !== undefined ? { "content-type": "application/json" } : undefined,
    body: init?.body !== undefined ? JSON.stringify(init.body) : undefined,
    cache: "no-store",
  });
  const text = await res.text();
  const body = text ? JSON.parse(text) : null;
  if (!res.ok) throw new ApiError(res.status, detail(body, res.status));
  return body as T;
}

export type ExploreNode = {
  paper_id: string;
  title?: string | null;
  doi?: string | null;
  date?: string | null;
  roles: string[];
  rank?: number | null;
  topic_id?: string | null;
  topic_label?: string | null;
  citation_data: boolean;
  links: number;
};
export type ExploreLink = {
  kind: "citation" | "shared_references";
  source: string;
  target: string;
  observed_at?: string | null;
  shared?: number | null;
};
export type ExploreGraph = {
  radar_id: string;
  run_id?: string | null;
  as_of: string;
  graph_version: string;
  classifier_version?: string | null;
  nodes: ExploreNode[];
  links: ExploreLink[];
  topics: { topic_id: string | null; label: string | null; papers: number }[];
  stats: Record<string, number>;
};

export const client = {
  radars: () => api<Radar[]>("/radars"),
  radar: (id: string) => api<Radar>(`/radars/${encodeURIComponent(id)}`),
  propose: (input: ProposalInput) => api<Proposal>("/radars/proposal", { method: "POST", body: input }),
  createRadar: (config: RadarConfig, schedule?: Schedule | null) =>
    api<Radar>("/radars", { method: "POST", body: schedule ? { config, schedule } : { config } }),
  updateRadar: (id: string, patch: RadarPatch) =>
    api<Radar>(`/radars/${encodeURIComponent(id)}`, { method: "PATCH", body: patch }),
  events: (id: string) => api<ResearchEvent[]>(`/radars/${encodeURIComponent(id)}/events`),
  graph: (id: string) => api<ExploreGraph>(`/radars/${encodeURIComponent(id)}/graph`),
  event: (id: string) => api<ResearchEvent>(`/events/${encodeURIComponent(id)}`),
  trends: (id: string, asOf?: string) =>
    api<TopicTrends>(`/radars/${encodeURIComponent(id)}/topics/trends${asOf ? `?as_of=${encodeURIComponent(asOf)}` : ""}`),
  digests: (id: string) => api<DigestDelivery[]>(`/radars/${encodeURIComponent(id)}/digests?limit=10`),
  sendDigest: (id: string) =>
    api<DigestDelivery[]>(`/radars/${encodeURIComponent(id)}/digests/email`, { method: "POST" }),
  scan: (id: string, opts: ScanOptions) => api<Job>(`/radars/${encodeURIComponent(id)}/runs`, { method: "POST", body: opts }),
  job: (id: string) => api<Job>(`/jobs/${encodeURIComponent(id)}`),
  runs: (id: string) => api<RunSummary[]>(`/radars/${encodeURIComponent(id)}/runs`),
  papers: (id: string, runId?: string) =>
    api<RankedPapers>(`/radars/${encodeURIComponent(id)}/papers${runId ? `?run_id=${encodeURIComponent(runId)}` : ""}`),
  delta: (id: string) => api<Delta>(`/radars/${encodeURIComponent(id)}/delta`),
  paper: (id: string) => api<Paper>(`/papers/${encodeURIComponent(id)}`),
  explanation: (paperId: string, radarId: string, runId?: string) =>
    api<Explanation>(
      `/papers/${encodeURIComponent(paperId)}/explanation?radar_id=${encodeURIComponent(radarId)}${
        runId ? `&run_id=${encodeURIComponent(runId)}` : ""
      }`,
    ),
  feedback: (radarId: string, paperId: string, action: FeedbackAction, remove = false) =>
    api<{ state: Record<string, string[]> }>("/feedback", {
      method: "POST",
      body: { radar_id: radarId, paper_id: paperId, action, remove },
    }),
  library: (radarId?: string) => api<LibraryItem[]>(`/library${radarId ? `?radar_id=${encodeURIComponent(radarId)}` : ""}`),
};

/** Load data on mount (and when `deps` change); `reload()` refetches without clearing what is shown. */
export function useApi<T>(load: () => Promise<T>, deps: unknown[]) {
  const [data, setData] = useState<T | undefined>();
  const [error, setError] = useState<ApiError | Error | undefined>();
  const [loading, setLoading] = useState(true);
  // eslint-disable-next-line react-hooks/exhaustive-deps
  const run = useCallback(load, deps);
  const reload = useCallback(async () => {
    setLoading(true);
    try {
      setData(await run());
      setError(undefined);
    } catch (e) {
      setError(e as Error);
    } finally {
      setLoading(false);
    }
  }, [run]);
  useEffect(() => {
    setData(undefined);
    void reload();
  }, [reload]);
  return { data, error, loading, reload, setData };
}

/** Poll a scan job until it finishes or fails. */
export function useJob(jobId: string | undefined, onDone?: (job: Job) => void) {
  const [job, setJob] = useState<Job | undefined>();
  useEffect(() => {
    if (!jobId) return;
    let stop = false;
    let timer: ReturnType<typeof setTimeout>;
    const tick = async () => {
      try {
        const j = await client.job(jobId);
        if (stop) return;
        setJob(j);
        if (j.status === "finished" || j.status === "failed" || j.status === "stopped" || j.status === "canceled") {
          onDone?.(j);
          return;
        }
      } catch (e) {
        if (stop) return;
        setJob({ job_id: jobId, status: "error", error: (e as Error).message, status_url: "" });
        return;
      }
      timer = setTimeout(tick, 1000);
    };
    void tick();
    return () => {
      stop = true;
      clearTimeout(timer);
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [jobId]);
  return job;
}
