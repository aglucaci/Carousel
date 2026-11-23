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
  <img src="https://img.shields.io/badge/status-v0.1%20in%20progress-E3A72F" alt="Status: v0.1 in progress">
</p>

CAROUSEL is an open-source platform for personalized scientific literature
discovery. It monitors the research topics you define, ranks new papers with a
transparent and reproducible scoring model, and tells you what has changed in
your field since you last looked.

The question it answers is not *"What papers were published?"* but **"What
changed in my field, why does it matter, and what should I read next?"**

> **Status: early development (v0.1 in progress).** A daily PubMed radar,
> its run history, and the radar config tooling work today. Multi-source
> retrieval, the five-signal ranker, and Research Delta are being built. See
> [What works today](#what-works-today) and the [roadmap](#roadmap).
> CAROUSEL grew out of the ETHER prototype, so you will still see that name in
> the manuscript drafts and the current dashboard.

---

## What works today

| Capability | Where | Notes |
| --- | --- | --- |
| **Daily literature radar** | [Live dashboard](https://aglucaci.github.io/Carousel/) | PubMed, 14-day rolling window, about 10 themes, ranked with per-paper score breakdowns. Rebuilt every day at 13:00 UTC |
| **Run history (snapshots)** | [`snapshots` branch](https://github.com/aglucaci/Carousel/tree/snapshots) | Every run is stored as an immutable, versioned snapshot. History starts 2026-01-21, and earlier runs were recovered from git history |
| **Radar configs** | [`examples/`](examples/), [`carousel/config.py`](carousel/config.py) | YAML radars with strict validation and `file:line` error messages |
| **`carousel` CLI** | [`carousel/cli.py`](carousel/cli.py) | `validate` and `show` today. `build`, `delta`, and `export` are next |
| **Ranking evaluation** | [`scripts/validate_ranking.py`](scripts/validate_ranking.py) | Precision@K, Recall@K, and nDCG@K over expert-labeled rankings |

## Quick start

Requires Python 3.10+.

```bash
git clone https://github.com/aglucaci/Carousel.git
cd Carousel
pip install -e ".[dev]"
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

ranking:            # relative priority of the five CAROUSEL signals
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
- Sources: `pubmed`, `europepmc`, `biorxiv`, `medrxiv`, `crossref`, `openalex`, `arxiv`. The config schema accepts all of these, but only PubMed is fetched today. The other connectors are part of v0.1.

## How ranking works

CAROUSEL ranks with independently measurable signals rather than an opaque AI
rating. Every recommendation can show why it ranked where it did.

| Signal | Meaning | Default weight |
| --- | --- | --- |
| **Relevance** | Match to the radar's queries and signal profile | 40% |
| **Novelty** | Distinctiveness relative to the literature the radar has already seen | 20% |
| **Momentum** | Recent, field-normalized growth in attention | 15% |
| **Discovery** | Cross-disciplinary connections and useful diversity | 10% |
| **Evidence** | Available methodological detail: methods, data and code availability, open access | 15% |

These are starting values, not tuned ones. Signals that are not available for
a run, such as Momentum before enough history exists, are left out, and the
other weights are rescaled to sum to 1. They are not counted as zero.

Retractions and corrections are handled as **explicit flags or exclusions**,
never as a score penalty that a high-ranking paper could outweigh.

> A CAROUSEL score is an estimated *priority under a stated configuration*. It
> is **not** a measure of a paper's scientific quality or future impact.

**Current state:** the live dashboard still uses the ETHER-era score
(`ranking_version: 0.0.1-ether`). That score combines theme priority, regex
signal matches, a venue boost, recency, and a cross-domain bonus. The
five-signal ranker replaces it during v0.1.

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
(`write`, `backfill`, `reindex`). The same history will drive Momentum and
time-forward evaluation, which scores papers using only what was known on the
day they were recommended.

## Automation

| Workflow | Trigger | What it does |
| --- | --- | --- |
| [`carousel-radar.yml`](.github/workflows/carousel-radar.yml) | Daily 13:00 UTC, or manual (`days`, `max`) | Runs the pipeline, writes a snapshot to `snapshots`, and commits the dashboard to `docs/` (served by GitHub Pages) |
| [`ci.yml`](.github/workflows/ci.yml) | Pull requests, pushes to `main` | Runs pytest and `carousel validate examples/*.yml` on Python 3.10 and 3.12 |

## Repository layout

```text
carousel/            Python package: radar config models and CLI
examples/            Starter radar configs (validated in CI)
scripts/
  daily_pubmed_watch_v2.py   current daily pipeline (being split into carousel/)
  snapshot.py                run snapshots: write / backfill / reindex
  validate_ranking.py        P@K, R@K, nDCG@K evaluation
tests/               unittest/pytest suite
docs/                Generated GitHub Pages site (do not edit by hand)
logo/                Logo SVG/PNG and the generator script
manuscript/          Paper draft and validation plan
CAROUSEL_PLAN.md     Implementation plan and v0.1 task list
MANUSCRIPT_PLAN.md   Publication plan
```

## Roadmap

The detailed plan, including what to keep from ETHER, sequencing, and
done-when checks for each task, is in [`CAROUSEL_PLAN.md`](CAROUSEL_PLAN.md).

| Release | Focus | Status |
| --- | --- | --- |
| **v0.1 Discovery MVP** | Radar configs, multi-source retrieval (PubMed, Europe PMC, Crossref), dedup, integrity flags, five-signal ranker, snapshots and Research Delta, "Why this paper?" explanations, exports (CSV, BibTeX, RIS, Markdown) | In progress: scheduling, snapshots, and the config/CLI are done |
| **v0.2 Intelligence** | FastAPI + Postgres/pgvector backend, accounts and feedback, embeddings (relevance and novelty), email digests, Next.js frontend, OpenAlex and arXiv | Planned |
| **v0.3 Research Radar** | Topic clusters, Momentum, Trends view, Research Bridges | Planned |
| **v0.4 Workflow** | Zotero sync, shared radars, collections, reproducibility bundles | Planned |
| **v1.0** | Validated early-signal methods, benchmarks across disciplines, public API | Planned |

## Evaluation

CAROUSEL is designed to be evaluated, not just used. The validation plan
([`manuscript/VALIDATION_PLAN.md`](manuscript/VALIDATION_PLAN.md)):

- Benchmarks rankings against date-only, keyword-count, and PubMed-relevance baselines, plus ablations.
- Uses expert-labeled case studies in cancer evolution, viral evolution, and metagenomics.
- Reports Precision@K, nDCG@K, Recall@K, discovery latency, and time-forward performance.

CAROUSEL is a surveillance and prioritization layer. It does not replace
systematic review.

## Contributing

Issues and pull requests are welcome. Before opening a PR:

```bash
python -m pytest -q
carousel validate examples/*.yml
```

New example radars must pass `carousel validate`.

## License

[MIT](LICENSE)
