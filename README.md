<p align="center">
  <picture>
    <source media="(prefers-color-scheme: dark)" srcset="logo/carousel-logo-dark.svg">
    <img src="logo/carousel-logo.svg" alt="CAROUSEL" width="560">
  </picture>
</p>

<p align="center"><b>Discover the research that matters.</b></p>

<p align="center">
  <a href="https://github.com/aglucaci/Carousel/actions/workflows/ci.yml"><img src="https://github.com/aglucaci/Carousel/actions/workflows/ci.yml/badge.svg" alt="CI"></a>
  <a href="https://github.com/aglucaci/Carousel/actions/workflows/carousel-radar.yml"><img src="https://github.com/aglucaci/Carousel/actions/workflows/carousel-radar.yml/badge.svg" alt="CAROUSEL radar"></a>
  <a href="https://aglucaci.github.io/Carousel/"><img src="https://img.shields.io/badge/dashboard-live-13A89E" alt="Live dashboard"></a>
  <img src="https://img.shields.io/badge/status-phase%201%20in%20progress-E3A72F" alt="Status: Phase 1 in progress">
</p>

CAROUSEL is an open-source platform for personalized scientific literature
discovery. It monitors the research topics you define, ranks new papers with a
transparent and reproducible scoring model, and tells you what has changed in
your field since you last looked.

The question it answers is not *"What papers were published?"* but **"What
changed in my field, why does it matter, and what should I read next?"**

> **Status: early development (Phase 1 done, Phase 2 in progress).** The engine (Phase 1A)
> works end to end: PubMed and OpenAlex retrieval, careful deduplication,
> lexical and semantic candidates, an interpretable ranker, stored runs, and
> feedback, running daily on GitHub Actions. The web app (Phase 1B) runs
> locally with one `docker compose up` (Postgres, Redis, the REST API, a scan
> worker, a scheduler, and the web app). Phase 2 is done: scheduled scans
> (daily or weekly, with pause and resume), labels that separate newly
> published from newly observed papers, Crossref checks for corrections and
> retractions, Europe PMC (preprints, licensed open-access full texts, and
> annotations), links between a preprint and its journal article,
> versioned explanations with a measured effect of your feedback, digests
> (Markdown, RSS, email), the first Research Delta events (new
> publications and revisions such as retractions), grounded extraction
> (structured facts that each quote their source passage), and arXiv. See [What works today](#what-works-today) and the
> [roadmap](#roadmap). CAROUSEL grew out of the ETHER prototype, so you will
> still see that name in the manuscript drafts.

---

## What works today

| Capability | Where | Notes |
| --- | --- | --- |
| **Daily literature radar** | [Live dashboard](https://aglucaci.github.io/Carousel/), [`radars/`](radars/) | `carousel scan` runs every radar in `radars/` daily at 13:00 UTC (the lab's ten themes are [`radars/lab_default.yml`](radars/lab_default.yml)) and publishes a ranked list with "Why this paper?" for each item, filterable by theme |
| **Run history (snapshots)** | [`snapshots` branch](https://github.com/aglucaci/Carousel/tree/snapshots) | Every run is stored as an immutable, versioned snapshot. History starts 2026-01-21, and earlier runs were recovered from git history |
| **Paper store** | [`src/carousel/core/paper.py`](src/carousel/core/paper.py), [`src/carousel/database/local.py`](src/carousel/database/local.py) | Canonical paper records built from per-source records, with per-field provenance, separate publication / first-online / preprint dates, and CAROUSEL's own first-seen and last-verified times. Local SQLite store |
| **Connectors** | [`src/carousel/connectors/`](src/carousel/connectors/) | PubMed and OpenAlex searches that return source records plus a coverage report (matches available, retrieved, truncated and why, requests, OpenAlex credits). Bounded windows only, with a per-run OpenAlex credit budget and an on-disk cache. OpenAlex searches scholarly work types only |
| **Duplicate detection** | [`src/carousel/ingestion/dedup.py`](src/carousel/ingestion/dedup.py), `carousel review` | Exact identifiers first (DOI, PMID, PMCID, OpenAlex, arXiv), then cautious title/author/year matching that merges automatically only when every check passes. Uncertain pairs, preprint/published versions, and identifier clashes go to a review queue; every merge is logged and can be split again |
| **Candidate retrieval** | [`src/carousel/search/`](src/carousel/search/) | BM25 over titles and abstracts, semantic similarity to the radar's profile, and source-query hits, after recorded hard filters (retracted, date window, type, excluded terms). Embeddings are cached per paper and model version. Real semantic search needs `pip install "carousel-research[semantic]"` |
| **Ranker** | [`src/carousel/ranking/`](src/carousel/ranking/) | `ranking_version` 0.1.0: relevance, novelty against only earlier-observed papers (guarded against temporal leakage), and method match; weighted by mode (focused / balanced / exploratory), then diversified with MMR. Every item carries its features and flags |
| **Postgres schema (Phase 1B)** | [`src/carousel/database/models.py`](src/carousel/database/models.py), [`migrations/`](src/carousel/database/migrations/) | The spec's production tables on PostgreSQL + pgvector (papers with full-text search, identifiers, sources, authors, embeddings, runs and items, feedback, collections, topics, events), managed with Alembic. `carousel db import-store` and `import-snapshots` copy the engine's history in and verify counts. Needs `pip install "carousel-research[server]"` |
| **Web app (Phase 1B)** | [`apps/web/`](apps/web/) | Next.js: **For You** (ranked papers per radar, new since the last run), **Radars** (describe a topic, review and approve the exact searches it proposes, scan, and see each run's history and source coverage), the **"Why this paper?"** drawer (from the stored run only), save and dismiss, and the **Library** of saved papers. A Playwright test runs the spec's acceptance flow end to end in CI |
| **Preprint ↔ publication links (Phase 2)** | [`src/carousel/ingestion/versions.py`](src/carousel/ingestion/versions.py), `carousel review link` / `unlink` | A preprint and the article it became stay **two separate papers that point at each other**, never merged. Links come from Crossref (`has-preprint` / `is-preprint-of`) and Europe PMC ("Preprint in" / "Preprint of"), with each link's evidence kept; for recommended papers a scan looks up the other version when it isn't stored yet (once a month at most per missing work). Recommendations are flagged "Has preprint" or "Since published", and the drawer lists the other version. A reviewer can link or unlink a pair, and a source never overrides a reviewer's "not the same work" |
| **Europe PMC (Phase 2)** | [`src/carousel/connectors/europepmc.py`](src/carousel/connectors/europepmc.py), [`core/fulltext.py`](src/carousel/core/fulltext.py) | Searched in titles and abstracts across MEDLINE, PubMed Central, and preprints (bioRxiv, medRxiv, and others), with each article's licence and open-access status. For a scan's recommended papers it keeps open-access full texts **only under a Creative Commons or public-domain licence, with that licence recorded on the text** (read from the article itself when it states one, and what it permits), plus Europe PMC's text-mined annotations (organisms, diseases, genes, chemicals). The API reports texts and licences but does not serve the text |
| **arXiv (Phase 2)** | [`src/carousel/connectors/arxiv.py`](src/carousel/connectors/arxiv.py) | Searched in titles and abstracts by first submission date, newest first, at arXiv's pace (one request every three seconds). Each record keeps its version (`v2`) and version date and carries arXiv's own DOI, so OpenAlex's record of the same preprint merges with it; the journal DOI arXiv lists for a published version becomes a version link, never a merge. Opt in by adding `arxiv` to a radar's sources |
| **Crossref verification (Phase 2)** | [`src/carousel/connectors/crossref.py`](src/carousel/connectors/crossref.py) | With `crossref` in a radar's `sources` (the default for new radars), each scan looks up the DOIs its searches found. Corrections, retractions, and expressions of concern (including Retraction Watch data) are recorded on the paper with who reported them; a retraction flags the paper and keeps it out of recommendations. Preprint links (`has-preprint` / `is-preprint-of`) become version links. Crossref is not searched: its free-text search cannot express a radar's boolean queries |
| **Explanations and feedback (Phase 2)** | [`src/carousel/ranking/explain.py`](src/carousel/ranking/explain.py), [`ranker.py`](src/carousel/ranking/ranker.py) | Each recommendation stores a **versioned explanation** written when it was ranked (short reasons: matched terms, how it was found, feedback, novelty, methods, integrity flags, linked versions), never regenerated. Saved and relevant papers pull the radar's interest profile toward them and not-relevant papers push it away; each run records **what that changed**, against the same run ranked without feedback (papers moved up, moved down, brought in, and each paper's relevance with and without it). In the app, "Relevant" / "Not relevant" in the drawer |
| **Grounded extraction (Phase 2)** | [`src/carousel/summarization/`](src/carousel/summarization/), `carousel extract`, `carousel consent` | Structured facts about a paper (objective, design, population, sample size, methods, findings, limitations, data availability). **Every claim quotes the passage it rests on**, verbatim, and a deterministic check drops any claim whose quote isn't there; a field the text doesn't report is "not reported", never filled in. Each extraction **states what it read** (title and abstract only, a licensed full text, or your own text). Providers sit behind one interface: a local rule-based extractor runs on every scan's recommendations, and Claude (`pip install "carousel-research[llm]"`) runs only after **recorded consent** for each kind of text sent (public, and separately, private text you supply, whose extractions are never exported). Cached per paper, model version, schema version, and input |
| **Research Delta: first events (Phase 2)** | [`src/carousel/intelligence/events.py`](src/carousel/intelligence/events.py), What's New in the app | Each scan reports **new publications** (papers the radar never observed before, published within its window) and **revisions** to papers it has shown you or you saved: corrections, retractions, expressions of concern, and newly linked preprint/published versions. Every run stores a snapshot of the papers it watches, so events are recomputed from stored runs exactly, and each event links its evidence (the runs, the notice DOI and who reported it, the window). A radar's first scan is its baseline. Digests list the changes too |
| **Digests (Phase 2)** | [`src/carousel/notifications/`](src/carousel/notifications/), `carousel digest`, `carousel scan --digest-dir` | Every scan produces a **Markdown** digest, updates the radar's **RSS** feed (each paper once), and **emails** the digest to the radar's recipients over SMTP (`CAROUSEL_SMTP_HOST` and friends). Each item says how it arrived, its flags, and its stored explanation. Every delivery is recorded, and a failed one keeps its reason (shown on the radar page); a digest failure never fails the scan |
| **Scheduled scans (Phase 2)** | [`src/carousel/workers/schedule.py`](src/carousel/workers/schedule.py), `carousel scheduler` | Radars scan daily or weekly (UTC periods; a manual scan in the period counts). Pausing stops scheduled scans and keeps every run, saved paper, and feedback event. Each recommendation is labelled against the radar's previous scan: **newly published** (after that scan's window) or **newly observed** (older, but new to this radar, e.g. indexed late) |
| **REST API v1 (Phase 1B)** | [`src/carousel/api/`](src/carousel/api/), [`openapi/carousel-v1.json`](openapi/carousel-v1.json) | FastAPI over Postgres: radars, scans as background jobs (RQ on Redis, `carousel worker`), ranked papers, delta, paper records, "Why this paper?" explanations from the stored run, full-text search, and feedback. Writes need `CAROUSEL_API_TOKEN` when it is set |
| **Radar configs** | [`examples/`](examples/), [`src/carousel/core/config.py`](src/carousel/core/config.py) | YAML radars with strict validation and `file:line` error messages |
| **`carousel scan` and the `Radar` API** | [`src/carousel/radar.py`](src/carousel/radar.py), [`src/carousel/cli.py`](src/carousel/cli.py) | One command runs the whole engine: query PubMed and OpenAlex, dedup into the local store, find candidates, rank, and save the run with its coverage. Rescans keep history; feedback in `RADAR.feedback.yml` (save / dismiss / relevant / irrelevant) applies on the next scan. Also `runs`, `delta`, `export`, `feedback`, `review`, `site`, `validate`, `show` |
| **Ranking evaluation** | [`scripts/validate_ranking.py`](scripts/validate_ranking.py) | Precision@K, Recall@K, and nDCG@K over expert-labeled rankings |

## Quick start

Requires Python 3.10+.

```bash
git clone https://github.com/aglucaci/Carousel.git
cd Carousel
pip install -e ".[dev]"
```

For real semantic retrieval, add the optional extra (pulls in sentence-transformers and PyTorch):

```bash
pip install -e ".[dev,semantic]"
```

Check every example radar:

```bash
carousel validate examples/*.yml
```

```text
ok   examples/viral_evolution.yml  radar_id=viral-evolution sources=pubmed,biorxiv,medrxiv queries=5 signal_categories=3 window=90d hash=ca11145c42f2
...
8/8 valid
```

Print a radar's fully resolved config, with defaults filled in:

```bash
carousel show examples/viral_evolution.yml
```

Scan a radar. This queries PubMed and OpenAlex, stores papers and the run in
`carousel.db`, and prints the ranked list with each source's coverage:

```bash
carousel scan examples/viral_evolution.yml --top 10 --csv recommendations.csv
```

Mark papers for the next scan (by `paper_id` from the output, or `doi:` / `pmid:`),
then rescan and see what changed:

```bash
carousel feedback examples/viral_evolution.yml dismiss cp_3f2a9c1e5b7d4a60
```

```bash
carousel scan examples/viral_evolution.yml && carousel delta examples/viral_evolution.yml
```

Or from Python:

```python
from carousel import Radar

results = Radar.from_yaml("examples/viral_evolution.yml").scan()
results.to_csv("recommendations.csv")
results.delta()
```

Without the `[semantic]` extra, scans use a non-semantic fallback embedder and
say so in the run's notes. Set `OPENALEX_API_KEY` to page past the first 100
OpenAlex results, and `NCBI_API_KEY` for faster PubMed access. Crossref needs
no key; `CROSSREF_MAILTO` (an email address) moves lookups to its faster
"polite" pool, and is never stored.

Build the dashboard locally from one or more scan exports:

```bash
carousel scan radars/lab_default.yml --json out/lab-default.json && carousel site out/*.json --out site
```

### The web app with Docker Compose

From a fresh clone, one command builds and starts Postgres (pgvector), Redis,
the API, the scan worker, and the web app, then open http://localhost:3000:

```bash
docker compose up
```

Put optional settings in a `.env` file next to `docker-compose.yml` (it is
git-ignored): `OPENALEX_API_KEY` (OpenAlex is skipped without one),
`NCBI_API_KEY`, `CROSSREF_MAILTO`, `CAROUSEL_API_TOKEN`, `POSTGRES_PASSWORD`, `CAROUSEL_WEB_PORT`,
`CAROUSEL_SMTP_HOST` / `_PORT` / `_SECURITY` / `_USER` / `_PASSWORD` / `_FROM` for digest emails,
`CAROUSEL_PUBLIC_URL` for links back to the app,
and `CAROUSEL_EXTRAS=server,semantic` for real embeddings (adds PyTorch to the
image; without it scans use the hashing embedder). Ports are bound to
localhost. Data lives in Docker volumes; `docker compose down -v` deletes it.

### Running the pieces yourself

Load everything into Postgres (Phase 1B; needs the `[server]` extra and a
database with pgvector):

```bash
export CAROUSEL_DATABASE_URL=postgresql://user:pass@localhost/carousel
carousel db upgrade && carousel db import-store carousel.db
```

Then serve the API and run the scan worker (Redis required); interactive docs
are at `http://127.0.0.1:8000/api/v1/docs`:

```bash
carousel api serve
```

```bash
carousel worker
```

```bash
carousel scheduler
```

```bash
curl -X POST -H "Authorization: Bearer $CAROUSEL_API_TOKEN" http://127.0.0.1:8000/api/v1/radars/lab-default/runs
```

The worker reads `CAROUSEL_DATABASE_URL` and `CAROUSEL_STORE` from its own
environment, so set them for it too. The scheduler (`carousel scheduler`, or
`--once` from cron) needs `CAROUSEL_DATABASE_URL` and `CAROUSEL_REDIS_URL`; it
enqueues each daily or weekly radar's scan once per UTC period, and is safe to
run more than once.

The web app (Node 20.9 or later) talks to the API through its own server, which
adds `CAROUSEL_API_TOKEN` to writes, so the token never reaches the browser.
Anyone who can open the web app can therefore write, so keep it local or put it
behind your own access control:

```bash
cd apps/web && npm install && npm run build
```

```bash
CAROUSEL_API_URL=http://127.0.0.1:8000 npm run start
```

Run the tests (the Postgres and API tests run when `CAROUSEL_TEST_DATABASE_URL`
points at a disposable database):

```bash
python -m pytest -q
```

The web app's end-to-end test starts its own API and worker on a disposable
database, which it **wipes** (the name must contain `test` or `e2e`), and
replays recorded PubMed and OpenAlex responses:

```bash
cd apps/web && npx playwright install chromium && npm run build
```

```bash
CAROUSEL_E2E_DATABASE_URL=postgresql://user:pass@localhost/carousel_e2e CAROUSEL_E2E_REDIS_URL=redis://localhost:6379/15 npm run e2e
```

The same test runs against the Compose stack, under its own project name and
volumes, with scans replaying the recorded responses
([`docker-compose.e2e.yml`](docker-compose.e2e.yml)):

```bash
docker compose -p carousel-e2e -f docker-compose.yml -f docker-compose.e2e.yml up -d --build --wait
```

```bash
cd apps/web && CAROUSEL_E2E_BASE_URL=http://127.0.0.1:3000 npm run e2e
```

```bash
docker compose -p carousel-e2e -f docker-compose.yml -f docker-compose.e2e.yml down -v
```

## Defining a radar

A **radar** is a saved literature monitor: what to search, what to prioritize,
and how to rank. Start from one of the [example radars](examples/):

| | | | |
| --- | --- | --- | --- |
| Cancer evolution | Viral evolution | Metagenomics | Urban microbiome |
| AI drug discovery | Protein design | Wastewater surveillance | Single-cell genomics |

```yaml
radar_name: Viral Evolution Radar
description: >
  Tracks viral mutation, immune escape, recombination, host jumps,
  phylodynamics, and genomic surveillance.

sources: [pubmed, biorxiv, medrxiv]

queries:
  - "viral evolution"
  - "immune escape"
  - "antigenic drift"

exclude:            # optional: drop candidates matching these terms
  - "case report"

filters:
  date_range: "last_90_days"

signal_profile:     # what counts as interesting, by category
  immune_escape:
    description: "Immune evasion and antigenic change"
    terms: [immune escape, antigenic drift, neutralization escape]
    weight: 2.5
  methods:
    terms: [phylodynamics, molecular clock, genomic epidemiology]
    weight: 1.8

ranking:            # weights for the interpretable ranker (evolving; see CAROUSEL_PLAN.md §4)
  relevance: 0.40
  novelty: 0.20
  momentum: 0.15
  discovery: 0.10
  evidence: 0.15

outputs: [dashboard, csv, bibtex]

# schema_version 2 (optional; v1 files keep working)
mode: balanced      # focused | balanced | exploratory
research_question: "Which lineages escape current vaccines?"
methods_profile: [deep mutational scanning, phylodynamics]   # default: signal_profile.methods
seed_papers: ["10.1038/s41586-020-2012-7"]                  # DOIs that define the radar
publication_types: [Journal Article, Preprint]               # keep only these
```

Validation rules:

- Unknown fields, unknown sources, duplicate YAML keys, and duplicate terms are rejected with the line number.
- `radar_id` defaults to the file name, so `viral_evolution.yml` becomes `viral-evolution`.
- Each resolved config gets a short `config_hash`, which is recorded with results.
- Sources: `pubmed`, `europepmc`, `biorxiv`, `medrxiv`, `crossref`, `openalex`, `arxiv`. The config schema accepts all of these. PubMed, OpenAlex, Europe PMC, and arXiv are searched. `crossref` does not search: it checks the DOIs the searches found for corrections, retractions, and preprint links. Preprints from bioRxiv and medRxiv come through Europe PMC; `biorxiv` / `medrxiv` as sources of their own are not implemented (a scan records them as `no_connector`). Optional keys: `NCBI_API_KEY` raises PubMed's rate limit, and `OPENALEX_API_KEY` is needed to page past the first 100 OpenAlex results.

## How recommendations work

CAROUSEL ranks with independently measurable stages rather than an opaque AI
rating, and every recommendation records the features that put it where it is.

| Stage | What it does |
| --- | --- |
| **Candidates** | Lexical retrieval (your terminology), semantic retrieval (embeddings), and later citation expansion from seed papers plus exploratory retrieval from neighboring fields. Results are merged, deduplicated, and hard-filtered |
| **Relevance** | Similarity to the radar's interest profile (description, queries, seed papers, and your feedback), combined with lexical match |
| **Novelty** | Distance from papers observed *before* this one, normalized within field and publication type. Novelty is not the same as relevance |
| **Method match** | Overlap with the radar's **methods profile** (sequencing, omics, computational methods, data types). This later powers **Method Transfer**: useful methods from other fields |
| **Selection** | An interpretable weighted score, then a diversity step (maximal marginal relevance) so the top 10 aren't near-duplicates. **Focused**, **Balanced**, and **Exploratory** modes set the precision/discovery trade-off |

Two other rules apply to every ranking:

- Retractions and corrections are **explicit flags or exclusions**, never a score penalty that a high-ranking paper could outweigh.
- A paper counts as *new* only if CAROUSEL first observed it recently **and** it was recently published. An old paper that a database only just indexed is not a new discovery.

> A CAROUSEL score is an estimated *priority under a stated configuration*. It
> is **not** a measure of a paper's scientific quality or future impact.
> Citation counts, venues, and AI scores are never presented as evidence of
> validity.

**Current state:** the daily dashboard uses this pipeline as `ranking_version`
0.1.0 (relevance, novelty, and method match; momentum, discovery, and evidence
are reported as not yet scored). Runs before 2026-11 used the ETHER-era score,
`0.0.1-ether`, and are kept in the run history under that version.

## Run history and Research Delta

Research Delta needs history: you can only say what changed if you kept what
was there before. Every pipeline run is therefore written to the
[`snapshots`](https://github.com/aglucaci/Carousel/tree/snapshots) branch as
an immutable record. It includes the full payload, window, `ranking_version`,
and provenance (commit and workflow run).

```text
snapshots/<radar_id>/<YYYY>/<YYYY-MM-DDTHHMMSSZ>.json.gz
snapshots/<radar_id>/index.jsonl
```

```sql
-- DuckDB, on a checkout of the snapshots branch
SELECT captured_at_utc, window_days, n_items
FROM read_json_auto('snapshots/lab-default/index.jsonl')
ORDER BY captured_at_utc;
```

Snapshots are managed with [`scripts/snapshot.py`](scripts/snapshot.py)
(`write`, `backfill`, `reindex`). The same history will drive trend detection and
time-forward evaluation, which scores papers using only what was known on the
day they were recommended.

## Automation

| Workflow | Trigger | What it does |
| --- | --- | --- |
| [`carousel-radar.yml`](.github/workflows/carousel-radar.yml) | Daily 13:00 UTC, or manual (`top`, `max_results`) | Runs `carousel scan` for every radar in `radars/` (one matrix job each, real semantic model), then builds the dashboard into `docs/` (GitHub Pages), writes a snapshot of each run to `snapshots`, and saves each radar's paper store to the `carousel-state` branch so the next scan keeps its history. Optional secrets: `OPENALEX_API_KEY`, `NCBI_API_KEY` |
| [`ci.yml`](.github/workflows/ci.yml) | Pull requests, pushes to `main` | Runs pytest and validates `examples/` and `radars/` on Python 3.10 and 3.12, and checks the built wheel |

## Repository layout

```text
src/carousel/        Python package: radar.py (Radar API), feedback.py, core/ (radar config, identifiers, paper model),
                     connectors/ (PubMed, OpenAlex, Europe PMC; Crossref verification), ingestion/ (dedup, version links), search/ (candidates),
                     ranking/ (ranker),
                     database/ (SQLite store; Postgres models, migrations, importer),
                     api/ (REST API v1), workers/ (scan jobs), site.py (dashboard), cli.py
apps/web/            Next.js web app (For You, Radars, Library); e2e/ holds the Playwright acceptance test
docker-compose.yml   The Phase 1B stack (Dockerfile = API + worker image; apps/web/Dockerfile = web app)
radars/              Radars scanned daily by Actions (lab_default.yml = the lab's ten themes)
examples/            Starter radar configs (validated in CI)
openapi/             The committed API contract (checked against the app in CI)
scripts/
  snapshot.py                run snapshots: write / backfill / reindex
  validate_ranking.py        P@K, R@K, nDCG@K evaluation
tests/               unittest/pytest suite
docs/                GitHub Pages dashboard, generated daily by `carousel site` (do not edit by hand)
logo/                Logo SVG/PNG and the generator script
manuscript/          Paper draft and validation plan
CAROUSEL_PLAN.md     Implementation plan: phases, tasks, done-when checks
CITATION.cff         How to cite CAROUSEL
MANUSCRIPT_PLAN.md   Publication plan
```

## Roadmap

The detailed plan, including what to keep from ETHER, sequencing, and
done-when checks for each task, is in [`CAROUSEL_PLAN.md`](CAROUSEL_PLAN.md).

| Phase | Focus | Status |
| --- | --- | --- |
| **1. Functional MVP** | **Engine:** canonical paper records, OpenAlex + PubMed, careful dedup, lexical + semantic candidates, relevance/novelty/methods ranking with modes, `carousel scan` and a Python `Radar` API. **App:** FastAPI + Postgres/pgvector, Next.js (For You, Radars, Library, "Why this paper?"), save/dismiss, Docker Compose | Done for local use: the engine, Postgres, the REST API, the web app, and Docker Compose |
| **2. Continuous intelligence** | Scheduled ingestion, Europe PMC + Crossref (retractions, corrections, preprint links), arXiv, versioned explanations, feedback learning, email/RSS digests, evidence-grounded summaries | Done: scheduled scans with pause/resume, newly published vs newly observed labels, Crossref corrections/retractions, Europe PMC (licensed full texts, annotations), arXiv, preprint-publication linking, versioned explanations, the feedback-adjusted profile, digests, the first Research Delta events, and grounded extraction |
| **3. Research Delta** | Topic clusters and observations, statistical trend detection (negative binomial, multiple-testing control), the **What's New** change log, **Trends** with baselines and uncertainty | Planned |
| **4. Advanced discovery** | Method Transfer, literature graphs, Zotero, public API docs, collaborative radars, benchmark dataset, software paper | Planned |

Deliberately out of scope at first: autonomous research agents, automatic
systematic reviews, contradiction detection, a full corpus mirror, AI quality
scores, and microservices.

## Evaluation

CAROUSEL is designed to be evaluated, not just used. Three studies are
planned (details in [`CAROUSEL_PLAN.md`](CAROUSEL_PLAN.md) §7 and
[`manuscript/VALIDATION_PLAN.md`](manuscript/VALIDATION_PLAN.md)):

- **Recommendation usefulness:** P@5/10, nDCG@10, Recall@K, MRR, novelty, diversity, and expert-rated usefulness against keyword, recency, citation, and embedding-only baselines. Explicit judgments count for more than clicks.
- **Early discovery:** replays at historical dates that use only the data available at that moment. Temporal leakage guards are tested code, and the immutable snapshots make the replays possible.
- **Research Delta accuracy:** event precision and recall, detection latency, explanation faithfulness, and trend stability.

CAROUSEL is a surveillance and prioritization layer. It does not replace
systematic review.

## Contributing

Issues and pull requests are welcome. Before opening a PR:

```bash
python -m pytest -q
carousel validate examples/*.yml
```

New example radars must pass `carousel validate`.

## Citing

If you use CAROUSEL in your research, please cite it with the metadata in
[`CITATION.cff`](CITATION.cff). GitHub's "Cite this repository" button reads it.

## License

[MIT](LICENSE)
