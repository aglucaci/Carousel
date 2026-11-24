# CAROUSEL Implementation Plan

This plan turns the CAROUSEL product specification (2026) into ordered,
checkable work on this repository. It covers where the code is today, how the
spec maps onto it, and the four phases with a done-when check for each task.

**Product in one line:** a personal scientific research analyst that
**discovers** important new papers (including from outside your field), helps
you **understand** them with traceable evidence, and **monitors** what changed.
The signature feature is **Research Delta**: *"What has meaningfully changed in
my research area since my last visit?"*

---

## 1. Where the repo is today

_Updated at the end of Phase 1A (task 10)._

| Area | State |
| --- | --- |
| Daily radar | ✅ `carousel-radar.yml` runs `carousel scan` over `radars/*.yml` (matrix) daily and publishes the `carousel site` dashboard to Pages; paper stores persist on the `carousel-state` branch. The ETHER-era script is retired (task 10). |
| Run history | ✅ Every run is snapshotted to the `snapshots` branch (`scripts/snapshot.py`). 350 runs from 2026-01-21 onward were backfilled. These runs mix 1/7/14/30-day windows and are `unversioned`. |
| Package + CLI | ✅ `src/carousel/` with a strict `RadarConfig` (schema v2) and the `carousel` CLI: `scan`, `runs`, `delta`, `export`, `feedback`, `review`, `site`, `validate`, `show`. CI runs on 3.10 and 3.12. |
| Radar configs | ✅ 8 example radars plus `radars/lab_default.yml` (the lab's ten themes, scanned daily) validate. |
| Connectors | ✅ PubMed (POST for long queries) and OpenAlex (credit budget, scholarly types), with recorded-HTTP fixtures and per-query coverage. |
| Identity / dedup | ✅ Canonical papers from per-source records; exact-ID then cautious title/author/year matching; review queue, merge log, reversible splits. |
| Ranking | ✅ `ranking_version` 0.1.0: relevance, leakage-free novelty, method match, modes, MMR. Momentum, discovery, and evidence are reported as not yet scored. |
| Site | ✅ `carousel site`: a CAROUSEL-branded index plus one page per radar (ranked list, theme filter, "Why this paper?", coverage). The ETHER pages are gone. |
| Evaluation | ⚠️ `scripts/validate_ranking.py` (P@K, R@K, nDCG@K) and `manuscript/VALIDATION_PLAN.md`. No labels or baselines yet. |

**Keep from ETHER:** the PubMed E-utilities logic, the radar and signal-profile
concepts, the example radars, the validation metrics, the snapshots, and the
Actions + Pages public demo.

---

## 2. How the spec changes the earlier plan

| Topic | Earlier plan | Spec | Plan now |
| --- | --- | --- | --- |
| Web app timing | Static site for v0.1, backend in v0.2 | Phase 1 includes a working website and Docker | **Phase 1 includes the website.** It is split into 1A (engine, CLI, Python API) and 1B (FastAPI + Postgres + Next.js + Docker). The engine comes first, matching the spec's own priority #1 (ingestion and dedup). The Pages demo keeps running throughout. |
| First connectors | PubMed + Europe PMC + Crossref | OpenAlex + PubMed in Phase 1; Europe PMC + Crossref in Phase 2 | Follow the spec. **Consequence:** retraction and correction flags arrive in Phase 2 with Crossref. |
| Ranking | Five weighted signals (R, N, M, D, E) | Staged pipeline: candidate channels → relevance → novelty → method match → diversity selection (MMR), with Focused / Balanced / Exploratory modes | Follow the spec (§4). The config's `ranking:` weights become the weights of the interpretable ranker, and a `mode:` preset is added. |
| Dates | Publication date only | Store publication, first-online, preprint, version, source-updated, **first observed**, and **last verified** dates separately | Follow the spec. "New" in Research Delta means newly *observed* **and** recently *published*, so an old paper that was only just indexed doesn't count as a discovery. |
| Dedup | Merge by identifier, then fuzzy fallback | DOI → trusted IDs → verified preprint links → cautious fuzzy matching. Ambiguous matches are **reviewable**. Preprints are **linked, not collapsed**. Per-field provenance is kept | Follow the spec. |
| Code layout | `carousel/` at the repo root | `src/carousel/{api,core,connectors,ingestion,search,ranking,intelligence,summarization,personalization,evaluation,notifications,database,workers}` + `apps/web` | Move to the `src/` layout in Phase 1A, before the package grows. |
| Navigation | For You / My Radars / Explore / Trends / Library / Briefings | **For You, Radars, What's New, Explore, Trends, Library, Digests** | Follow the spec. "Research Delta" and "Method Transfer" are used in docs and papers only. |
| Trends | Field-normalized growth | Smoothed topic share; a negative-binomial model with a publication-volume offset; multiple-testing control; persistence rules; classifier version recorded | Follow the spec (Phase 3). |

---

## 3. Target architecture

```text
src/carousel/
  core/            config (RadarConfig), schemas, versions
  connectors/      openalex, pubmed (Phase 1); europepmc, crossref, arxiv (Phase 2)
  ingestion/       normalize identifiers, canonical records, dedup, version links, provenance
  search/          lexical (BM25 / Postgres FTS), semantic (embeddings, pgvector), citation expansion
  ranking/         relevance, novelty, method match, interpretable ranker, MMR, modes
  intelligence/    topic clusters, observations, trend models, Research Delta events
  summarization/   grounded extraction (Phase 2+), cached per paper and model version
  personalization/ radar interest profile, seed papers, positive/negative feedback
  evaluation/      metrics, baselines, time-forward harness (no temporal leakage)
  notifications/   digests (email, RSS, Markdown)
  database/        SQLAlchemy models + Alembic migrations (spec §8 tables)
  workers/         scheduled scans (RQ on Redis)
  api/             FastAPI v1 routes (spec §9)
  cli.py           carousel validate | show | scan | delta | export | eval
apps/web/          Next.js + Tailwind + shadcn/ui
docker-compose.yml postgres(pgvector), redis, api, worker, web
```

**Storage across phases.** In Phase 1A, a local store (SQLite or DuckDB file)
holds canonical papers, identifiers, sources (with `first_seen` /
`last_checked`), and runs, so CLI scans and the Actions demo work without a
server. The snapshot branch stays the immutable run log. In Phase 1B, the same
entities move to Postgres using the spec §8 tables, and historical snapshots
are imported.

**Python API** (spec §9), delivered in Phase 1A:

```python
from carousel import Radar
radar = Radar.from_yaml("examples/viral_evolution.yml")   # or Radar(topic=..., sources=[...], mode="exploratory")
results = radar.scan()          # retrieve → dedup → rank → persist run
results.to_csv("recommendations.csv")
results.delta()                 # vs the previous run of this radar
```

---

## 4. Recommendation engine (spec §5)

| Stage | Phase 1 | Later |
| --- | --- | --- |
| **A. Candidates** | Lexical (source queries + BM25) and semantic (sentence-transformer embeddings), merged and deduplicated, with hard filters (types, dates, exclusions) | Citation and reference expansion from seed papers (OpenAlex); exploratory retrieval from neighboring fields |
| **B. Relevance** | `R = α·sim(e_p, e_u) + β·BM25 (+ γ·graph once citations exist)`. `e_u` is built from the radar description, queries, seed papers, and ± feedback | Reranker on the top candidates only |
| **C. Novelty** | `N = 1 − mean kNN similarity` to `H_t` = papers observed **before** the candidate's observation time, normalized within field and publication type | Concept-combination and cross-community novelty |
| **D. Method match** | `methods_profile` in the radar config (sequencing, omics, computational methods, data types); match against title and abstract | **Method Transfer**: a method-relevant paper from a distant field, explained as a hypothesis |
| **E. Selection** | Interpretable weighted score, then MMR: `λ·S − (1−λ)·max sim(p, selected)`. `mode` sets λ and the weights: **Focused**, **Balanced** (default), **Exploratory** | Learned ranker once enough labeled data exists |

**Rules carried through every phase:**

- Every recommendation stores its features, `ranking_version`, `model_version`, source coverage, and explanation version.
- Integrity status is a flag or exclusion, never a score term.
- Citation count, venue, and AI scores are never presented as validity.

**Config additions** (`schema_version: 2`, with v1 still accepted):

- `mode`
- `methods_profile`
- `seed_papers` (DOIs)
- `publication_types`
- `research_question`
- per-source query overrides

---

## 5. Research Delta (spec §6)

| Event | Phase |
| --- | --- |
| New publication (newly observed **and** within the publication window) | 2 |
| Publication revision: new version, correction, retraction | 2 (needs Crossref) |
| Topic acceleration | 3 |
| New methodological connection | 3 |
| Research landscape shift | 3 |
| Scientific evidence update (question tracking) | 4 / research |

**Topic acceleration model.**

- Topic share is `s = (n + α)/(N + αK)`.
- Counts follow `n_{c,t} ~ NegBin(μ, φ_c)` with `log μ = log N_t + β₀ + f_c(t) + δ_{c,t}`.
- A topic is flagged only on effect size, statistical support after multiple-testing correction, a minimum count, and **persistence**.
- Each observation records the classifier or embedding version.
- Before a trend is reported, it is checked against source-indexing changes.

---

## 6. Phases and tasks

### Phase 1 — Functional MVP

**Acceptance (spec):** a user can define a topic, receive a deduplicated ranked
paper list, save or dismiss papers, and repeat the scan without losing
previous history.

**1A — Engine (CLI, Python API, Actions demo)**

| # | Task | Done when |
| --- | --- | --- |
| 1 | ✅ One scheduled workflow, and snapshots on every run | Daily green run; snapshot is written (done 2026-11-23) |
| 2 | ✅ Package, strict `RadarConfig`, `carousel validate` / `show`, CI | 8 examples validate; bad YAML gives file:line errors (done 2026-11-23) |
| 3 | ✅ Move to the `src/carousel/` layout; config into `core/`; add `CITATION.cff` | CI green; `pip install -e .` and the CLI unchanged (done 2026-11-23) |
| 4 | ✅ Canonical paper model and local store: identifiers, per-source records, all spec §4.1 dates, `first_seen` / `last_verified`, per-field provenance | Round-trip tests; one paper seen from two sources keeps both source records (done 2026-11-23: `core/identifiers.py`, `core/paper.py`, SQLite `database/local.py`; exact-ID matching only, and conflicting IDs are logged, never merged) |
| 5 | ✅ Connectors: **OpenAlex** (budget counter, cache, cursor paging, never full-corpus scans) and **PubMed** (ported from the legacy script) | Recorded-HTTP fixture tests; the source coverage of each query is recorded (done 2026-11-23: `connectors/`; OpenAlex budget in credits, charged from `X-RateLimit-Credits-Used`; paging past page 1 needs `OPENALEX_API_KEY`, else coverage says `no_api_key`) |
| 6 | ✅ Dedup: DOI → PMID/PMCID/OpenAlex ID → (Phase 2: verified preprint links) → cautious title/author/year matching. Ambiguous pairs go to a review queue | Fixture suite of true merges, true non-merges, and ambiguous pairs; no automatic merge below threshold (done 2026-11-23: `ingestion/dedup.py`, 23 labelled pairs in `tests/fixtures/dedup/pairs.yml`, store schema v2 with review queue + merge log, `carousel review list/merge/distinct/split`) |
| 7 | ✅ Lexical + semantic candidates; embeddings cached per `(paper, model_version)` | The same paper is never re-embedded under the same model (done 2026-11-23: `search/`; BM25 + cached embeddings + source-query hits with hard filters; `[semantic]` extra for sentence-transformers, `HashingEmbedder` fallback; store schema v3) |
| 8 | ✅ Ranker: relevance, novelty (`H_t` without leakage), method match, MMR, three modes; `ranking_version` 0.1.0; config `schema_version: 2` | Deterministic given a stored run; a leakage test fails if `H_t` contains later papers (done 2026-11-23: `ranking/ranker.py`; novelty credited in proportion to relevance and MMR over a relevance-qualified pool, both found necessary with the real model) |
| 9 | ✅ `carousel scan` + `Radar` API; persist runs (`recommendation_runs` / `items` shape); feedback file (save / dismiss / relevant / irrelevant) applied on the next scan | Rescanning keeps history, dismissed papers stay dismissed, and `results.to_csv()` works (done 2026-11-23: `radar.py`, `feedback.py`, store schema v4; also `carousel runs / delta / export / feedback`; verified with a live PubMed + OpenAlex scan and the real model) |
| 10 | ✅ Switch the Actions demo to `carousel scan` over `radars/*.yml` (matrix); retire `daily_pubmed_watch_v2.py`; migrate the lab themes to `radars/lab_default.yml` | Pages shows the new pipeline's output; snapshots continue under `ranking_version` 0.1.0 (done 2026-11-23: workflow plan → scan matrix → publish; `carousel site`; snapshots read run exports; live preview found and fixed PubMed 414 (now POST) and OpenAlex datasets/software in results (now scholarly types only)) |

**1B — Application**

| # | Task | Done when |
| --- | --- | --- |
| 11 | ✅ Postgres + pgvector schema (spec §8 tables) via Alembic; import the 1A store and historical snapshots | Migration tests; snapshot import counts match (done 2026-11-23: `database/models.py` + Alembic `0001`, `[server]` extra, `carousel db upgrade / import-store / import-snapshots`; real snapshots branch: 352/352 snapshots, 21,518/21,518 items) |
| 12 | ✅ FastAPI v1: radars, runs (asynchronous job IDs), papers, explanation, feedback | OpenAPI schema plus API tests; scans run in a Redis/RQ worker (done 2026-11-23: `api/` (12 routes, contract in `openapi/carousel-v1.json`), `workers/scan.py` (RQ; runs the engine scan, then imports into Postgres), optional `CAROUSEL_API_TOKEN` for writes; verified end to end with real Redis, uvicorn, a separate worker, and a live scan) |
| 13 | ✅ Next.js app: **For You**, **Radars** (create with progressive disclosure; approve the proposed search strategy), paper card + **"Why this paper?"** drawer (features from the stored run only), save/dismiss, **Library** (saved list) | Playwright test of the spec acceptance flow (done 2026-11-24: `apps/web` (Next.js 16, Tailwind 4; a server-side proxy keeps `CAROUSEL_API_TOKEN` out of the browser); API additions `POST /radars/proposal` (deterministic: the exact per-source queries, nothing saved) and `GET /library`, plus `saved` on ranked items; the Playwright test runs the real API, worker, Postgres, and Redis with scans replaying recorded HTTP (`CAROUSEL_HTTP_REPLAY`); jobs no longer carry the database URL, so credentials stay out of Redis) |
| 14 | ✅ `docker-compose up` brings up postgres, redis, api, worker, and web | A fresh clone reaches the acceptance flow with one command (done 2026-11-24: `docker-compose.yml` (pgvector Postgres, Redis, a one-shot `migrate`, api, worker, web; health-gated start order; ports on localhost; keys and token from `.env`), `Dockerfile` (API + worker; `CAROUSEL_EXTRAS=server,semantic` for real embeddings) and `apps/web/Dockerfile`; `docker-compose.e2e.yml` replays the recorded HTTP so the Playwright test runs against the stack (`CAROUSEL_E2E_BASE_URL`), in CI as the `compose` job) |

### Phase 2 — Continuous literature intelligence

**Acceptance (spec):** the system identifies new papers after a previous scan,
distinguishes newly observed from newly published work, and records why papers
were recommended.

- Scheduled ingestion per radar (daily or weekly), with pause/resume that keeps history.
- **Europe PMC** (abstracts, open-access full text where licensed, annotations) and **Crossref** (DOI metadata, `update-to` corrections and retractions including Retraction Watch data, preprint relations).
- Preprint↔publication linking, without collapsing the records.
- Recommendation explanations, versioned.
- Feedback-adjusted interest profile.
- **Digests**: email, RSS, Markdown.
- First Research Delta events: new publication and publication revision.
- Grounded extraction schema (spec §7) behind a provider-agnostic interface:
  - abstract-only coverage is stated explicitly, and nothing absent from the source is invented
  - each claim cites its passage
  - results are cached per paper and model version
  - explicit consent is required before private text leaves the deployment
- arXiv connector.

| # | Task | Done when |
| --- | --- | --- |
| 15 | ✅ Scheduled scans per radar (daily or weekly), pause/resume that keeps history; label each recommendation newly published vs newly observed | A paused radar is never scanned on schedule and keeps its runs; scheduled runs are recorded as such; arrivals are tested against the previous run (done 2026-11-24: `workers/schedule.py` + `carousel scheduler` (UTC periods; a manual scan in the period counts; a per-period Redis guard and a queued/running check, so ticks are idempotent across schedulers), Alembic `0002` (`radars.schedule` daily/weekly, `recommendation_runs.trigger`), `PATCH /radars/{id}`, `next_scan_at`, `arrival` on ranked items and `newly_published` / `newly_observed` on delta; web: schedule and pause controls, arrival badges; Compose `scheduler` service; verified live: a daily radar got its scheduled scan on the first tick) |
| 16 | ✅ Crossref connector: DOI metadata, `update-to` corrections and retractions (incl. Retraction Watch), preprint relations | Recorded-HTTP fixture tests; a retraction flags the paper (done 2026-11-24: `connectors/crossref.py` verifies rather than searches (its free-text search has no boolean operators): with `crossref` in `sources` (default for new radars) a scan looks up its papers' DOIs, 20 per request, budgeted, coverage role `verification`; `updated-by` → `SourceRecord.updates` (type, notice DOI, date, publisher vs `retraction-watch`), retraction/removal/withdrawal → `is_retracted` (flagged, excluded from candidates), `update-to` → notice types, `has-preprint`/`is-preprint-of` → `relations` (stored for task 18); canonical papers union both with `via`; ranker flags `corrected` and `expression_of_concern`; paper API returns `updates` and `relations`; fixtures recorded live, including the e2e scenario's 40 DOIs) |
| 17 | ✅ Europe PMC connector: abstracts, open-access full text where licensed, annotations | Recorded-HTTP fixture tests; licence recorded per text (done 2026-11-24: `connectors/europepmc.py` searches `TITLE_ABS` over MED/PMC/PPR in the first-publication window (preprints included), records each article's licence and open-access status, and corrections/retractions from `commentCorrectionList`; for a scan's recommended papers it keeps JATS full texts only under a Creative Commons or public-domain licence, with the licence on the text (`core/fulltext.py`: normalized id, URL, read from the text or the metadata, `reuse` class) and summarizes text-mined annotations; checks are remembered for 30 days; store schema v5 and Alembic `0003` (`paper_texts`, `paper_annotations`); paper API reports texts and licences (not the text) and annotations; the drawer shows them; on by default for new radars; fixtures recorded live, e2e over both scans) |
| 18 | ✅ Preprint↔publication linking without collapsing the records | Fixture pairs; linked records stay separate and show each other (done 2026-11-24: `ingestion/versions.py` turns Crossref `has-preprint`/`is-preprint-of` and Europe PMC "Preprint in"/"Preprint of" relations into links between two papers (never merged), keeping every piece of evidence (which source, which side asserted it, Europe PMC's title-and-first-author matches marked as such); a scan links what is in the store, then looks up the missing other version of its recommended papers (Crossref DOIs, Europe PMC IDs in batches of 20; a work not found is not asked for again for 30 days); items carry `has_preprint` / `published_version` flags and the linked versions in their stored details; store schema v6 (`paper_links`, `link_checks`, review status `linked`, which settles a pending preprint/article review pair); `carousel review link` / `unlink` (a reviewer's unlink is never overridden by a source); Alembic `0004` fills spec §8 `paper_versions` (one row per direction); paper API `versions`; the drawer's "Other versions"; fixture pairs recorded live (two Europe PMC, one Crossref) and the e2e scenario's three linked preprints) |
| 19 | ✅ Versioned recommendation explanations; feedback-adjusted interest profile | Explanations carry a version; relevant/irrelevant feedback measurably changes the next ranking (done 2026-11-24: `ranking/explain.py` writes each item's explanation at ranking time (`version` 1, summary, reasons each traceable to a stored feature) and stores it with the run, never regenerated; `explanation_version` on runs (store schema v7, Alembic `0005`); the ranker measures feedback against the same run ranked with the pre-feedback profile: per item `relevance_without_feedback`, `relevance_change`, `rank_without_feedback`, and the nearest positive/negative feedback paper; per run `feedback_effect` (moved up/down, entered/left) and the profile `shift`; tested against a control rescan that only hides the same papers, so the change is the profile's; API `explanation`, `why`, `feedback_effect`; web: "In short" in the drawer, a one-line why on each card, Relevant / Not relevant in the drawer, and what feedback changed on For You) |
| 20 | ✅ Digests: email, RSS, Markdown | A scan produces each digest; failed deliveries are recorded (done 2026-11-24: `notifications/` builds a digest from a stored run only (arrivals, flags, each item's stored explanation, cut-short sources, what feedback changed) and renders Markdown, RSS 2.0 (over the last 20 runs, each paper once, from the run that first recommended it), and email (text + HTML, one message per recipient, stdlib SMTP with STARTTLS/SSL/none from `CAROUSEL_SMTP_*`; the password is never logged or stored); the scan worker produces all three after every run and records each delivery in spec §8 `digest_runs` (Alembic `0006`: run link, recipients on `radars.digest_emails`, channel/status checks), a failure with its reason, never failing the scan; API `digest.md`, `feed.xml`, `digests`, `POST digests/email`, `digest_emails` on `PATCH /radars`; CLI `carousel digest` and `scan --digest-dir`; web: the radar page's Digests card (feed links, recipients, send now, delivery log); Compose passes the SMTP settings; checked against a real SMTP exchange with a local test server) |
| 21 | First Research Delta events: new publication, publication revision | Events are reproducible from stored runs and link their evidence |
| 22 | Grounded extraction schema (spec §7) behind a provider-agnostic interface | Abstract-only coverage stated; each claim cites its passage; cached per paper and model version; consent required before private text leaves |
| 23 | arXiv connector | Recorded-HTTP fixture tests |

### Phase 3 — Research Delta

**Acceptance (spec):** topic events are reproducible, explainable, supported by
traceable publications, and can be benchmarked against annotated examples.

Topic clustering, `topic_observations`, the negative-binomial trend model
(§5), the **What's New** change log (new findings, emerging methods, topic
growth, new connections, publication updates), **Trends** with baselines and
uncertainty, evidence-linked event pages, and checks for indexing or
classifier artifacts.

### Phase 4 — Advanced discovery and open-source maturity

**Method Transfer**, literature graph views, Zotero integration, public API
docs, collaborative Radars, benchmark dataset release, and a software paper.
Scientific question tracking is a research feature.

### Deliberately excluded at first (spec §13)

- full AI research agent
- automatic systematic reviews
- contradiction detection (research feature only)
- institutional collaboration
- a full corpus mirror
- AI quality scores
- microservices

---

## 7. Evaluation (spec §11), built in from Phase 1

- **Study A, usefulness:** P@5, P@10, nDCG@10, Recall@K, MRR, novelty, intra-list diversity, and expert-rated usefulness against baselines (keyword, recency, citation count, embedding-only). Explicit judgments outweigh clicks.
- **Study B, early discovery:** time-forward replays at historical dates using only data available then. This needs the immutable snapshots and `first_seen` dates from tasks 1 and 4.
- **Study C, Delta accuracy:** event precision and recall, detection latency, explanation faithfulness, trend stability, and expert judgment of method transfers.
- **Leakage guards are tested code, not convention:**
  - novelty `H_t` is cut off at the observation time
  - no future citations, versions, or topic assignments are used
  - each measurement records its classifier version

---

## 8. Operations and privacy (spec §10)

| Area | Targets and rules |
| --- | --- |
| **Speed** | p95 under 2 s for cached dashboard, search, and paper detail; under 1 s for sort and filter. Long scans run in the background with visible status. |
| **Freshness and availability** | Freshness documented per source; 99.5% availability initially. |
| **Monitoring** | Ingestion failures, OpenAlex spend, queue age, duplicate rate, stale radars, failed digests, and model errors. |
| **Privacy** | Private radars by default; consent before external model calls; a local-model option; deletion and export; tenant-isolation tests; no training on private content. |

---

## 9. Open decisions

1. ~~**Phase 1 split.**~~ Settled (2026-11-23): engine first (1A), then the web app (1B).
2. **Lab themes.** Keep the current hardcoded themes as `radars/lab_default.yml` (recommended, as a dogfooding case study), or drop them.
3. **Manuscript framing.** Either retitle the JOSS draft for CAROUSEL now, or publish ETHER first and treat CAROUSEL as its successor.
4. **Hosting for 1B.** Self-hosted only at first, or also a public instance (which needs quotas for embeddings, LLM calls, and the OpenAlex budget).
