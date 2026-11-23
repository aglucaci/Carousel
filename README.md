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

> **Status: early development (Phase 1 in progress).** A daily PubMed radar,
> its run history, and the radar config tooling work today. Multi-source
> retrieval, the recommendation engine, and Research Delta are being built. See
> [What works today](#what-works-today) and the [roadmap](#roadmap).
> CAROUSEL grew out of the ETHER prototype, so you will still see that name in
> the manuscript drafts and the current dashboard.

---

## What works today

| Capability | Where | Notes |
| --- | --- | --- |
| **Daily literature radar** | [Live dashboard](https://aglucaci.github.io/Carousel/) | PubMed, 14-day rolling window, about 10 themes, ranked with per-paper score breakdowns. Rebuilt every day at 13:00 UTC |
| **Run history (snapshots)** | [`snapshots` branch](https://github.com/aglucaci/Carousel/tree/snapshots) | Every run is stored as an immutable, versioned snapshot. History starts 2026-01-21, and earlier runs were recovered from git history |
| **Paper store** | [`src/carousel/core/paper.py`](src/carousel/core/paper.py), [`src/carousel/database/local.py`](src/carousel/database/local.py) | Canonical paper records built from per-source records, with per-field provenance, separate publication / first-online / preprint dates, and CAROUSEL's own first-seen and last-verified times. Local SQLite store. Not yet wired into the daily radar |
| **Connectors** | [`src/carousel/connectors/`](src/carousel/connectors/) | PubMed and OpenAlex searches that return source records plus a coverage report (matches available, retrieved, truncated and why, requests, OpenAlex credits). Bounded windows only, with a per-run OpenAlex credit budget and an on-disk cache. Not yet wired into the daily radar |
| **Duplicate detection** | [`src/carousel/ingestion/dedup.py`](src/carousel/ingestion/dedup.py), `carousel review` | Exact identifiers first (DOI, PMID, PMCID, OpenAlex, arXiv), then cautious title/author/year matching that merges automatically only when every check passes. Uncertain pairs, preprint/published versions, and identifier clashes go to a review queue; every merge is logged and can be split again |
| **Candidate retrieval** | [`src/carousel/search/`](src/carousel/search/) | BM25 over titles and abstracts, semantic similarity to the radar's profile, and source-query hits, after recorded hard filters (retracted, date window, type, excluded terms). Embeddings are cached per paper and model version. Real semantic search needs `pip install "carousel-research[semantic]"` |
| **Radar configs** | [`examples/`](examples/), [`src/carousel/core/config.py`](src/carousel/core/config.py) | YAML radars with strict validation and `file:line` error messages |
| **`carousel` CLI** | [`src/carousel/cli.py`](src/carousel/cli.py) | `validate`, `show`, and `review` (duplicate review queue) today. `scan`, `delta`, and `export` are next |
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
ok   examples/viral_evolution.yml  radar_id=viral-evolution sources=pubmed,biorxiv,medrxiv queries=5 signal_categories=3 window=90d hash=47e1d2439758
...
8/8 valid
```

Print a radar's fully resolved config, with defaults filled in:

```bash
carousel show examples/viral_evolution.yml
```

Run the current daily pipeline locally. It writes `index.html`,
`latest.json`, and `latest.md`:

```bash
python scripts/daily_pubmed_watch_v2.py --days 14 --max 25 --docs-dir results/demo
```

Run the tests:

```bash
python -m pytest -q
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
```

Validation rules:

- Unknown fields, unknown sources, duplicate YAML keys, and duplicate terms are rejected with the line number.
- `radar_id` defaults to the file name, so `viral_evolution.yml` becomes `viral-evolution`.
- Each resolved config gets a short `config_hash`, which is recorded with results.
- Sources: `pubmed`, `europepmc`, `biorxiv`, `medrxiv`, `crossref`, `openalex`, `arxiv`. The config schema accepts all of these. PubMed and OpenAlex connectors exist (the daily radar still uses the legacy PubMed script until `carousel scan` lands); Europe PMC, Crossref, and arXiv arrive in Phase 2. Optional keys: `NCBI_API_KEY` raises PubMed's rate limit, and `OPENALEX_API_KEY` is needed to page past the first 100 OpenAlex results.

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

**Current state:** the live dashboard still uses the ETHER-era score
(`ranking_version: 0.0.1-ether`). That score combines theme priority, regex
signal matches, a venue boost, recency, and a cross-domain bonus. The
pipeline above replaces it in Phase 1.

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
| [`carousel-radar.yml`](.github/workflows/carousel-radar.yml) | Daily 13:00 UTC, or manual (`days`, `max`) | Runs the pipeline, writes a snapshot to `snapshots`, and commits the dashboard to `docs/` (served by GitHub Pages) |
| [`ci.yml`](.github/workflows/ci.yml) | Pull requests, pushes to `main` | Runs pytest and `carousel validate examples/*.yml` on Python 3.10 and 3.12 |

## Repository layout

```text
src/carousel/        Python package: core/ (radar config, identifiers, paper model),
                     connectors/ (PubMed, OpenAlex), ingestion/ (dedup), search/ (candidates),
                     database/ (local SQLite paper store), cli.py
examples/            Starter radar configs (validated in CI)
scripts/
  daily_pubmed_watch_v2.py   current daily pipeline (being replaced by `carousel scan`)
  snapshot.py                run snapshots: write / backfill / reindex
  validate_ranking.py        P@K, R@K, nDCG@K evaluation
tests/               unittest/pytest suite
docs/                Generated GitHub Pages site (do not edit by hand)
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
| **1. Functional MVP** | **Engine:** canonical paper records, OpenAlex + PubMed, careful dedup, lexical + semantic candidates, relevance/novelty/methods ranking with modes, `carousel scan` and a Python `Radar` API. **App:** FastAPI + Postgres/pgvector, Next.js (For You, Radars, Library, "Why this paper?"), save/dismiss, Docker Compose | In progress: scheduling, snapshots, and the config/CLI are done |
| **2. Continuous intelligence** | Scheduled ingestion, Europe PMC + Crossref (retractions, corrections, preprint links), arXiv, versioned explanations, feedback learning, email/RSS digests, evidence-grounded summaries | Planned |
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
