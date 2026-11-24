/** Typed client for the CAROUSEL API v1 (see openapi/carousel-v1.json), through this app's /api/v1 proxy. */
"use client";

import { useCallback, useEffect, useState } from "react";

export type Coverage = {
  source: string;
  status: string;
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
};

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

export type Radar = {
  radar_id: string;
  title: string;
  description: string;
  state: string;
  schedule?: string | null;
  created_at: string;
  updated_at: string;
  config?: RadarConfig | null;
  n_runs: number;
  latest_run?: RunSummary | null;
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
};

export type RankedPapers = { run: RunSummary; items: RankedItem[] };

export type Delta = {
  run_id: string;
  previous_run_id?: string | null;
  entered: string[];
  left: string[];
  moved: { paper_id: string; rank: number; previous_rank: number; change: number }[];
};

export type Explanation = {
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

export type Paper = {
  paper_id: string;
  title?: string | null;
  venue?: string | null;
  url?: string | null;
  identifiers: Record<string, string>;
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

export const client = {
  radars: () => api<Radar[]>("/radars"),
  radar: (id: string) => api<Radar>(`/radars/${encodeURIComponent(id)}`),
  propose: (input: ProposalInput) => api<Proposal>("/radars/proposal", { method: "POST", body: input }),
  createRadar: (config: RadarConfig) => api<Radar>("/radars", { method: "POST", body: { config } }),
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
