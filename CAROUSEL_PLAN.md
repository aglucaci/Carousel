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
| 21 | ✅ First Research Delta events: new publication, publication revision | Events are reproducible from stored runs and link their evidence (done 2026-11-24: `intelligence/events.py`: `new_publication` = recommended, never observed by an earlier run of the radar, and published within the run's window; `publication_revision` = a new notice (retraction, correction, expression of concern, ...), a newly set retraction flag, or a newly linked version on a **watched** paper (recommended by any earlier run, or saved/relevant), from a per-run snapshot of each watched paper's state (store schema v8: `run_paper_states`, `research_events`); a radar's first run is its baseline; `derive_events` is a pure function of stored runs and snapshots, and a test deletes the stored events and recomputes them identically; each event's evidence names the runs, the window, the notice DOI and who reported it, or the version link; Alembic `0007` completes spec §8 `research_events` (stable key, run, paper, kind); API `GET /radars/{id}/events` and per-run counts on the ranked list; web: What's New (in the nav) and a line on For You; digests list revisions and count new publications) |
| 22 | ✅ Grounded extraction schema (spec §7) behind a provider-agnostic interface | Abstract-only coverage stated; each claim cites its passage; cached per paper and model version; consent required before private text leaves (done 2026-11-24: `summarization/`: schema v1 (objective, study design, population, sample size, methods, findings, limitations, data availability), each field a list of claims citing sentence passages with verbatim quotes; a deterministic grounding check drops any claim whose quote isn't in its cited passage (recorded in `rejected`), and unreported fields are `not_reported`, never inferred; `coverage` states what was read (title and abstract only, with what that means; a licensed full text; or the user's own text); providers behind one interface: `rules` (local, deterministic, runs on every scan's recommendations) and `anthropic` (Claude `claude-opus-5-5`, structured JSON output, server-side refusal fallback; `[llm]` extra); external calls need recorded consent per provider and per text class (`public`, `private`), checked before anything is sent; cache keyed by paper, provider, requested model, schema version, and input hash; store schema v9 (`paper_extractions`, `model_consents`), Alembic `0008` (`paper_extractions`; private extractions are never imported); CLI `carousel extract` and `carousel consent`; paper API `extractions`; drawer "Extracted facts". The spec's §7 field list isn't in this repo; the fields here are this task's choice, versioned so they can change) |
| 23 | ✅ arXiv connector | Recorded-HTTP fixture tests (done 2026-11-24: `connectors/arxiv.py` searches titles and abstracts (`ti:`/`abs:` phrases, `ANDNOT` exclusions, or a radar's native query) within the window by `submittedDate`, newest first, paged, at one request every 3.1 s, with a request budget; records keep the version and its date, carry arXiv's DataCite DOI (`10.48550/arXiv.<id>`) so OpenAlex's record of the same preprint merges, and turn the listed journal DOI into an `is-preprint-of` relation (a version link, never a merge); `fetch` by ID in any form; query errors reported; in the proposal step and the new-radar form (opt-in); fixtures recorded live: a paged search, records by ID (one with a journal DOI), and Crossref's record of that DOI for the link) |

### Phase 3 — Research Delta

**Acceptance (spec):** topic events are reproducible, explainable, supported by
traceable publications, and can be benchmarked against annotated examples.

Topic clustering, `topic_observations`, the negative-binomial trend model
(§5), the **What's New** change log (new findings, emerging methods, topic
growth, new connections, publication updates), **Trends** with baselines and
uncertainty, evidence-linked event pages, and checks for indexing or
classifier artifacts.

**How Phase 3 builds on Phase 2.** Paper-level events (new publication,
revision; task 21) already come from stored runs. Topic events need two
things Phase 2 does not have:

- **Topics with stable identities.** A topic must mean the same thing next
  month, or a "trend" is just a relabelled cluster. Topics are versioned
  classifiers; papers keep the version they were assigned with; a refit maps
  old topics to new ones explicitly.
- **Unbiased counts over time.** A scan is capped (`max_results`) and
  windowed, so counting a radar's own recommendations measures the radar, not
  the literature. Counts and their denominators come from the sources' own
  count queries (OpenAlex `meta.count`, PubMed `esearch` counts), recorded as
  append-only observations with when they were made, so a past trend can be
  recomputed exactly as it looked then.

| # | Task | Done when |
| --- | --- | --- |
| 24 | ✅ Topics v1: cluster each radar's papers into topics from stored embeddings (deterministic clustering), label them from distinctive terms (c-TF-IDF over titles and abstracts) with key-term queries per topic; assign new papers to the nearest topic with a confidence (or *unassigned*); every assignment records its `classifier_version` (embedding model, clustering parameters, fit corpus); a refit is a new version with an explicit old→new topic mapping (centroid similarity and member overlap) | Same papers and model version give the same topics; a fixture corpus with known groups is recovered; a refit maps unchanged topics to themselves and reports splits and merges; `carousel topics` lists them (done 2026-11-25: `intelligence/topics.py`: spherical k-means, best of 6 seeded k-means++ starts, `k` by silhouette over 2..12 on a fixed sample, clusters under 5 papers dropped, and **no model when the best silhouette is below 0.05** (an honest "no structure" rather than arbitrary topics); c-TF-IDF labels preferring phrases, generic scientific words excluded; per-topic thresholds (5th percentile of member similarity) leave far papers unassigned; `classifier_version` hashes the algorithm, embedder, parameters, and fit corpus; refits map shared members and centroids to `same` (keeps the lineage ID), `split`, `merge`, `new`, `retired`, and an identical refit is a no-op; scans fit a radar's first model once it has 15 papers and then assign each scan's papers (refits only on request); store schema v10 and Alembic `0009` (`topic_models`, `topic_mappings`, and the spec's `topics` / `paper_topics` extended); `carousel topics list|fit|papers`; API `GET /radars/{id}/topics`. Topics are per radar, the recommended answer to open decision 6) |
| 25 | ✅ `topic_observations`: per period (weekly), each topic's count and the radar field's denominator `N_t`, from source count queries (the topic's key-term query and the radar's base query, per source), with coverage, `observed_at`, and classifier version; past periods are re-observed to measure indexing lag; observations are append-only, and `counts_as_of(t)` rebuilds what was known at `t` | Recorded-HTTP fixture tests; a leakage test fails if `counts_as_of(t)` uses an observation made after `t`; no full-corpus queries (count requests only, budgeted) (done 2026-11-25: `intelligence/observations.py`; `SearchQuery.require` adds an AND group to every source's query builder (existing queries unchanged) and each search connector has `count()` (PubMed `rettype=count`, Europe PMC and OpenAlex one-result pages, arXiv `max_results=1`, since 0 makes arXiv answer HTTP 500); a topic's count is the field query AND any of its top 4 key terms; ISO weeks, complete only, newest first within a request budget; append-only rows with the exact query, status, cost, and `observed_at`, failures included; `counts_as_of(rows, t)` uses the latest successful row observed by `t`, with a leakage test; `lag_profile` from re-observations; store schema v11 and Alembic `0010` (the spec's `topic_observations` extended); `carousel topics observe`; API `GET /radars/{id}/topics/series?as_of=`; fixture recorded live from all four sources, including arXiv rate-limit failures, which are recorded and never read as counts. Observing isn't run by scans yet: scheduling it is part of task 27's indexing-lag work) |
| 26 | ✅ Trend model: smoothed share `s = (n + α)/(N + αK)`; `n_{c,t} ~ NegBin(μ, φ_c)`, `log μ = log N_t + β₀ + f_c(t) + δ_{c,t}` with a baseline `f_c` fitted on earlier periods and the excess `δ` tested over recent ones; rate ratio with a confidence interval; Benjamini–Hochberg across a radar's topics; flagged only with effect size, adjusted significance, a minimum count, and persistence over consecutive periods; `trend_model_version` on every result | Simulation tests: injected accelerations are detected at the target power, and null series stay within the nominal false-discovery rate; results are deterministic for the same observations (done 2026-11-25: `intelligence/trends.py` with statsmodels (`[trends]` extra: numpy, statsmodels; in CI and the Docker image): NB2 with the field count as exposure, `f_c` a constant level until 26 baseline weeks support a linear drift (a fitted slope over 8 weeks dominated the test in simulation: flat topics read 0.6), the excess over the last 4 weeks tested one-sided with a t reference (the Wald z-test was anti-conservative in simulation: 5/30 null runs flagged, now about 4/60), Poisson fallback recorded; flags need rate ratio ≥ 1.5, BH q ≤ 0.10, ≥ 10 recent papers, and the same a week earlier (so 13+ observed weeks; `observe` now defaults to 16); smoothed shares reported; design point and results: ≥ 80% power for a 2.5-fold rise at about 15 papers a week (measured 1.0; a doubling about 0.63), global-null runs with any false flag about 0.07; deterministic; CLI `carousel topics trends`; API `GET /radars/{id}/topics/trends?as_of=` (501 without the extra). Results are computed from stored observations on demand; task 28 stores them with the events they produce) |
| 27 | ✅ Artifact checks, before any trend is reported: indexing lag (recent periods stay *provisional* until their counts stop changing, as measured by re-observation), denominator shifts (a source's volume or coverage jumps, a source added or removed), classifier changes (no comparison across classifier versions without the task-24 mapping), and duplicate surges (one work counted by several sources) | Each check has a fixture where it fires; a trend it affects is withheld or marked, and the report names the check (done 2026-11-25: `intelligence/checks.py`, `checked_trends` used by the CLI and API: indexing lag via per-source settling time, measured from weeks seen incomplete then complete (default 21-28 days, 3 for arXiv, until 3 weeks are measured), evaluating only settled weeks and listing the provisional ones; denominator shift when the field's recent mean leaves 2/3-1.5x its baseline median (all flags of that source withheld); classifier change named when a refit leaves too little history and earlier versions exist (never mixed); duplicate surge when a flagged topic's recent records are under 70% distinct works (version links collapsed; from the store or Postgres); `checks_version`; `carousel scheduler` enqueues one `run_observe` per radar with a topic model per ISO week (16 weeks first, then new weeks plus the last 6 again) |
| 28 | Topic events, derived from stored observations and versions only (like task 21): `topic_acceleration` (from 26 after 27), `new_methodological_connection` (a method term persistently co-occurring with a topic for the first time, in more than one paper), and `research_landscape_shift` (a topic splitting, merging, or newly appearing between classifier versions, or a large centroid drift); each event's evidence names the supporting papers, the counts and denominators, the model fit, and the checks it passed | A test deletes stored topic events and recomputes them identically; every event links at least one traceable publication |
| 29 | **What's New** v2: the change log grouped as the spec lists (new findings from grounded extraction of new publications, emerging methods, topic growth, new connections, publication updates), with an evidence page per event (papers, counts with denominators, the fitted baseline, the checks); digests include topic events | Playwright on fixture data: every item opens its evidence page, and every claim on it links a stored observation or paper |
| 30 | **Trends**: per topic, share over time with the fitted baseline and its uncertainty band, provisional periods marked, classifier-version boundaries shown, and the radar's denominator; API for the series | Rendered from stored observations and model results only; a test checks the plotted values against the model output; the chart is readable without colour and has a table view |
| 31 | Study C harness (§7): an annotated benchmark format for topic events (topic, event type, the date experts would place it, supporting papers), a starter set from documented public history in this lab's areas, and a time-forward replay that runs tasks 25–28 using only what was observable at each date; metrics: event precision and recall, detection latency, trend stability across refits, and explanation faithfulness (does the cited evidence support the event) | `carousel eval delta` runs the starter set end to end and writes a report; the leakage tests from 25 hold during replay |

**Order.** 24 → 25 → 26 → 27 → 28; then 29, 30, and 31 can proceed in
parallel (31 can start its benchmark format and annotations as soon as 25
lands, since it needs only observations to replay).

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
5. **Phase 3 numerics.** The trend model (task 26) needs a negative-binomial GLM. Either add `numpy` + `statsmodels` as a `[trends]` extra (recommended: tested implementations, confidence intervals included), or write a small NB2 IRLS fit on `numpy` alone. The engine stays installable without either; topic events then report the model as inactive, as momentum is today. *Task 26 took the recommended `[trends]` extra.*
6. **Topic scope.** Topics per radar (recommended first: small, interpretable, matches the radar's field and denominator) or one shared topic map across radars (needed later for cross-field Method Transfer in Phase 4). *Task 24 implements per-radar topics; a shared map can be added as another classifier without changing these.*
7. **Benchmark annotations (task 31).** Who annotates the starter set (the lab, recommended, with two annotators and agreement reported), and which documented episodes to start from (for example variant waves and host jumps already covered by the lab's radars).
