# CAROUSEL Implementation Plan

How to get from the current ETHER codebase to the CAROUSEL blueprint
(November 2026). It covers what exists, what to keep, what to rebuild, and the
order to build it in. v0.1 is broken into tasks that each have a done-when
check.

---

## 1. Where the repo is today

| Area | Current state | Gap vs. CAROUSEL |
| --- | --- | --- |
| Pipeline | One 1,557-line script (`scripts/daily_pubmed_watch_v2.py`) does fetching, scoring, and HTML generation | Not importable, not testable in parts, and no package or CLI |
| Topics | 10+ themes and 6 signal classes hardcoded as Python constants, tuned to one lab's interests | The 8 `examples/*.yml` radar configs are **not read by any code** |
| Sources | PubMed E-utilities only | The example YAMLs list `biorxiv`/`medrxiv`, which aren't fetched. No OpenAlex, Crossref, or arXiv |
| Identity | Records keyed by PMID within a theme | No cross-theme or cross-source dedup, no DOI normalization, no preprint↔journal linking |
| Scoring | Additive, unnormalized: `theme_priority + regex signals + venue boost + recency + cross_domain` | The blueprint calls for 5 normalized signals (R, N, M, D, E) plus separate integrity flags. Venue boost is a quality proxy the blueprint warns against |
| History | **Snapshots started.** Every run is saved to the `snapshots` branch, and 350 past runs (2026-01-21 → 2026-09-13) were backfilled from git history | Backfilled runs mix 1/7/14/30-day windows and are `unversioned` |
| Scheduling | **Fixed (v0.1 tasks 1–2).** The two clobbering workflows (both `disabled_manually` since 2026-09-13) are replaced by one `carousel-radar.yml` | Becomes a matrix over `radars/*.yml` once task 3 lands |
| Site | Static GitHub Pages site with 12 pages, generated from Python string templates | Several pages describe features that don't exist (citation velocity, API, paper detail pages) |
| Evaluation | `scripts/validate_ranking.py` (P@K, R@K, nDCG@K) and `manuscript/VALIDATION_PLAN.md` | Good foundation. It needs baselines, labeled data, and time-forward evaluation (which depends on snapshots) |
| Deps | `requirements.txt` lists pandas, numpy, duckdb, lxml | The code uses only the stdlib plus `requests`, and the workflow installs only `requests` |
| Tests | 2 unit tests | Thin |

**What to keep:** the PubMed connector logic, the `_element_text` XML handling,
the radar/signal-profile concepts and YAML schema, the 8 example radars, the
validation metrics, the manuscript plan, and the GitHub Actions + Pages
delivery model.

---

## 2. Key sequencing decision: ship v0.1 without the full web stack

The blueprint's architecture (Next.js, FastAPI, Postgres/pgvector, Redis) is the
right **destination**. It is the wrong **first step**. I recommend building
v0.1 on the existing static-site + GitHub Actions model, rebuilt as a proper
Python package, and adding the backend in v0.2.

Reasons:

1. **Research Delta and the ranking engine are the contribution, and neither
   needs a server.** Both are batch computations over snapshots: fetch, score,
   diff, render.
2. **Snapshots have to start accumulating now.** Time-forward evaluation and
   Momentum both need weeks of history. Every week spent building auth and a
   frontend first is a week of lost data.
3. **The JOSS / application-note path rewards a pip-installable, tested,
   reproducible tool.** A hosted multi-user app adds maintenance cost without
   strengthening that paper.
4. **The package becomes the backend's core.** If `carousel/` is designed
   cleanly (connectors → normalize → score → snapshot → delta), FastAPI later
   just wraps it, so none of the work is thrown away.

The real limit of a static v0.1 is **per-user feedback** (save, dismiss,
relevant/irrelevant). v0.1 handles it in two ways: `localStorage` in the
browser, plus a `feedback.yml` file per radar that a user can commit back. That
matches the README's "Level 2: config user". Multi-user accounts are what
justify the backend in v0.2.

---

## 3. Target package layout

```text
carousel/
  config.py          # Pydantic models: RadarConfig, SignalProfile, RankingWeights
  models.py          # Paper, Identifier set, ScoredPaper, Snapshot, Delta
  connectors/
    base.py          # Connector protocol: search(query, window) -> Iterable[RawRecord]
    pubmed.py        # ported from daily_pubmed_watch_v2.py
    europepmc.py     # bioRxiv/medRxiv preprints + PubMed abstracts
    openalex.py      # cross-disciplinary records, topics, referenced works
    crossref.py      # DOI metadata, update/retraction notices, preprint relations
    arxiv.py
  normalize.py       # DOI/PMID/PMCID/arXiv/OpenAlex ID canonicalization
  dedupe.py          # identifier merge + fuzzy title/first-author fallback; version linking
  signals/
    relevance.py     # R
    novelty.py       # N
    momentum.py      # M
    discovery.py     # D
    evidence.py      # E
  integrity.py       # retraction/correction/EoC flags: separate from score
  rank.py            # normalize signals, apply weights, record ranking_version
  snapshot.py        # write/read immutable run snapshots
  delta.py           # compare snapshot t vs t-1 -> Delta
  explain.py         # "Why this paper?" payload from signal contributions
  render/
    templates/       # Jinja2 templates (replace Python string HTML)
    site.py
  export.py          # CSV, BibTeX, RIS, Markdown briefing, RSS
  cli.py             # carousel build | delta | export | eval
radars/              # user radar configs (examples/ stays as templates)
data/snapshots/      # see §5 for where this lives
tests/
pyproject.toml
```

---

## 4. Ranking model: from today's score to the 5-signal model

All signals are normalized to [0, 1] before weighting. Missing signals are
**excluded, and the remaining weights are renormalized**, not scored as zero.
Every snapshot records which signals were active.

| Signal | v0.1 implementation | v0.2+ upgrade | Today's equivalent |
| --- | --- | --- | --- |
| **R** Relevance | BM25 over title+abstract against the radar's queries and signal-profile terms, scaled per run | Sentence-embedding cosine to the radar centroid plus seed papers; feedback-adjusted centroid (Rocchio) | `theme_priority` + regex signal classes |
| **N** Novelty | — (inactive) | 1 − max cosine to the radar's historical corpus (the snapshot backlog) | none |
| **M** Momentum | — (inactive until ≥4 weeks of snapshots) | Field-normalized growth of the paper's cluster/topic; OpenAlex counts where available | none |
| **D** Discovery | Fraction of distinct signal-profile categories matched (generalizes `cross_domain`) | Embedding distance from the user's core cluster combined with relevance (the "bridge" score) | `CROSS_DOMAIN_BONUS` |
| **E** Evidence | Checklist: abstract present, methods terms, data/code availability (GitHub, Zenodo, "data availability"), open access, structured abstract | Add full-text availability where licensed | partly the `methods` signal class |
| *Integrity* | Crossref `update-to` (retraction, correction, expression of concern). **Flag or exclude, never a score penalty** | Retraction Watch data via Crossref | none |

**Dropped:**

- **Venue boost.** It is a prestige proxy. Users who want it can add a `journals:` preference in their radar config.
- **Recency as a score term.** The blueprint's model has no recency term on purpose: Research Delta handles "new since last visit" structurally, and Momentum covers attention. The time window still filters candidates.

**`ranking_version`.** Use a semver string, bumped on any change to formulas or
defaults, and stored in every snapshot and every exported recommendation. This
is the core of Reproducible Discovery.

---

## 5. Snapshots and Research Delta

**Snapshot.** One per radar per run, immutable, containing:

- `run_id`, `radar_id`, and the UTC timestamp
- the config hash and the full resolved config
- the source coverage per connector (query sent, count returned, any errors)
- `ranking_version`
- every candidate paper with its canonical ID, metadata, all signal values, the final score and rank, and integrity flags

Candidates that rank below the visible cut are included too, which is needed to
evaluate recall.

**Storage.** Use gzipped JSONL. Commit it to a separate `snapshots` branch, or
attach it as release assets, so `main` history stays clean. A rough estimate:
150 KB/run × 8 radars × 365 days ≈ 440 MB/year uncompressed, or about 10× less
gzipped. DuckDB, already in `requirements.txt`, reads JSONL directly for
analysis.

**Delta (v0.1, deterministic, no LLM).** Comparing snapshot *t* with the
previous snapshot of the same radar:

- **New:** canonical IDs not seen in any prior snapshot of this radar
- **Updated:** a preprint linked to a newly published journal version, or a correction or retraction notice on a previously recommended paper (this always surfaces, regardless of score)
- **Movers:** papers whose rank changed by ≥ N
- **Signal shifts:** changes in per-category match counts against a trailing 4-week mean, reported with counts rather than adjectives
- **Briefing text:** generated from templates using only the numbers above, for example "4 new papers matched *immune escape* (trailing mean 1.5/run)". LLM summaries come in v0.2, behind a provider-agnostic interface, and must be grounded in the abstract text

Both the dashboard and the email/RSS briefing lead with the delta, not the full
feed.

---

## 6. Releases

### v0.1 — Discovery MVP (static, package-based)

Workflow to prove: **define a topic → ranked recommendations → understand the
ranking → save papers → see what changed next time.**

| # | Task | Done when |
| --- | --- | --- |
| 1 | ✅ **Fix scheduling first:** merge the two workflows into one matrix job over `radars/*.yml`, with distinct output paths, and re-enable it | One run per radar per day, no clobbering, and a green run in Actions |
| 2 | ✅ Start snapshotting immediately: a thin wrapper that saves each run's JSON to the snapshots branch, even before the refactor | Snapshots accumulate daily (this starts the clock on Momentum and evaluation) |
| 3 | ✅ Create the `pyproject.toml` package skeleton, the `carousel` CLI, and Pydantic `RadarConfig` that loads `examples/*.yml` | `carousel validate examples/*.yml` passes, and bad YAML fails with file:line errors (unknown fields, sources, duplicate keys/terms). CI runs tests and validation on every PR. `carousel build` arrives with task 4 |
| 4 | Port the PubMed connector into `connectors/pubmed.py` and add Europe PMC (bioRxiv/medRxiv) | Example configs' `sources:` are honored, and connector tests run against recorded HTTP fixtures |
| 5 | Add `normalize.py` + `dedupe.py` (DOI > PMID > PMCID > arXiv > OpenAlex, then title+first-author fallback) | A paper hit by 3 themes and 2 sources appears once, and the tests cover the merge rules |
| 6 | Add the Crossref connector for DOI enrichment, integrity flags, and preprint↔journal relations | A known retracted DOI fixture is flagged and excluded by default |
| 7 | Build `signals/` R, D, E plus `rank.py` with weight renormalization and `ranking_version` | Unit tests per signal, and the ranking is deterministic given a snapshot |
| 8 | Build `snapshot.py` + `delta.py` | Given two fixture snapshots, the delta reports correct new / updated / movers / shifts |
| 9 | Build `explain.py` and the "Why this paper?" panel | Each card shows per-signal contributions, matched terms, source records, and `ranking_version` |
| 10 | Rebuild the site with Jinja2 in the CAROUSEL design system: For You (delta + top picks), My Radars, Library (localStorage save/dismiss), Briefings (Markdown + RSS) | Pages render from snapshot data only. Remove or label as "planned" any page describing unbuilt features |
| 11 | Add exports: CSV, BibTeX, RIS, Markdown briefing, and a reproducibility bundle (config + coverage + `ranking_version` + timestamp) | `carousel export --format ris` imports cleanly into Zotero |
| 12 | Write `feedback.yml` per radar (relevant / irrelevant / read / important), read by R as a pinned boost or exclusion | Dismissed papers stay dismissed across runs |
| 13 | Add baselines to `validate_ranking.py` (date-only, keyword-count, PubMed relevance, ablations without each signal) | One command produces a metrics table for the three case studies |

Not in v0.1: accounts, a database server, embeddings, Momentum, Novelty, graphs,
or LLM summaries.

### v0.2 — Intelligence (backend arrives)

- FastAPI + Postgres/pgvector wrapping the `carousel` package. Import historical snapshots into Postgres.
- Accounts, server-side feedback, and per-user radars.
- Embeddings with Sentence Transformers (pin the model ID in `ranking_version`). Replace BM25 for R and turn on N.
- Email digests and a provider-agnostic LLM summary interface (grounded, abstract-only, with a citation per claim).
- Next.js frontend. Port the Jinja templates' information architecture rather than redesigning it.
- Add OpenAlex (budget for its usage-based API: batch, cache, and never full-corpus scans) and arXiv.

### v0.3 — Research Radar

Topic clustering per radar, Momentum (needs ≥8 weeks of snapshots, available
by then if task 2 ships now), the Trends page with uncertainty bands, and
Research Bridges.

### v0.4 — Workflow

Zotero two-way sync, shared radars, collections, and the public reproducibility
bundle format.

### v1.0

Time-forward evaluation on accumulated snapshots, expert-labeled benchmarks
across ≥3 disciplines, a public API, and the methods paper if Early Signal beats
the baselines.

---

## 7. Evaluation hooks to build in from day one

- **Snapshots include below-cut candidates**, so recall and "missed paper" analysis are possible later.
- **Time-forward evaluation:** score with only the information available at snapshot time, then judge against labels collected later. This is only possible because snapshots are immutable and versioned.
- **Discovery latency:** the gap between a paper's first appearance in any source and its first appearance in the top-K. Snapshots provide this for free.
- **Label collection:** the dashboard's relevant/irrelevant buttons write the same 0/1/2 scale as `manuscript/validation_rankings_template.csv`, so feedback doubles as evaluation data (with consent).

---

## 8. Decisions needed from you

1. ~~**Name and URL.**~~ Settled: the repo is already `aglucaci/Carousel`, with Pages at `aglucaci.github.io/Carousel/`. What's left is renaming the remaining ETHER strings in the site, README, and manuscript.
2. **The lab-specific themes in v2.** Either keep them as a `radars/lucaci_lab.yml` radar (recommended, as a dogfooding case study) or drop them.
3. ~~**Snapshot storage.**~~ Settled: the `snapshots` branch (about 6 MB for 8 months of history).
4. **Manuscript framing.** Either retitle the JOSS draft for CAROUSEL now, or publish ETHER-as-is first and treat CAROUSEL as the successor.
