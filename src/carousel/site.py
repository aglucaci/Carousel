"""Static GitHub Pages dashboard built from scan exports (plan task 10).

    carousel scan radars/lab_default.yml --json out/lab-default.json
    carousel site out/*.json --out docs

Writes, for every run export:

    docs/index.html                       all radars
    docs/radars/<radar_id>/index.html     ranked list with "Why this paper?" for each item
    docs/radars/<radar_id>/latest.json    the full run (what snapshots are made from)
    docs/radars/<radar_id>/latest.csv     the recommendations as CSV
    docs/latest.json                      index of radars and their latest runs
    docs/assets/...                       logo and favicon

The pages are static (no build step, no external requests). Every number on
the page comes from the stored run.
"""

from __future__ import annotations

import datetime as dt
import html
import json
import shutil
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Union

from carousel import __version__

REPO_URL = "https://github.com/aglucaci/Carousel"
LOGO_DIR = Path(__file__).resolve().parent / "site_assets"  # copies of logo/ shipped with the package
ASSETS = {
    "carousel-logo.svg": "carousel-logo.svg",
    "carousel-logo-dark.svg": "carousel-logo-dark.svg",
    "carousel-icon.svg": "carousel-icon.svg",
    "carousel-favicon.png": "favicon.png",
}
DISCLAIMER = ("A CAROUSEL score is an estimated priority under the radar's stated configuration. "
              "It is not a measure of a paper's quality or future impact.")


def e(x: Any) -> str:
    return html.escape("" if x is None else str(x), quote=True)


CSS = """
:root{--navy:#0B2545;--teal:#13A89E;--gold:#E3A72F;--slate:#8DA2B8;--paper:#F7F5F0;
--bg:#F7F5F0;--card:#fff;--ink:#0B2545;--muted:#5b6b80;--line:#e3e0d8;--chip:#eef3f6;--bad:#b3261e;--badbg:#fdecea}
@media (prefers-color-scheme:dark){:root{--bg:#0a1626;--card:#10213a;--ink:#eef2f7;--muted:#9fb0c4;--line:#203553;--chip:#173050;--bad:#ffb4ab;--badbg:#3b1512}}
*{box-sizing:border-box}html{-webkit-text-size-adjust:100%}
body{margin:0;background:var(--bg);color:var(--ink);font:16px/1.5 -apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,Helvetica,Arial,sans-serif}
a{color:var(--teal)}a:hover{text-decoration-thickness:2px}
.wrap{max-width:960px;margin:0 auto;padding:0 16px}
header.top{border-bottom:1px solid var(--line);background:var(--card)}
header.top .wrap{display:flex;align-items:center;justify-content:space-between;gap:12px;padding-top:12px;padding-bottom:12px}
header.top img{height:34px;display:block}
header.top nav a{margin-left:16px;color:var(--muted);text-decoration:none;font-size:14px}
h1{font-size:28px;line-height:1.2;margin:28px 0 6px}h2{font-size:18px;margin:28px 0 10px}
.lede{color:var(--muted);margin:0 0 16px;max-width:70ch}
.meta{display:flex;flex-wrap:wrap;gap:6px 14px;color:var(--muted);font-size:13px;margin:8px 0}
.meta b{color:var(--ink);font-weight:600}
.panel{background:var(--card);border:1px solid var(--line);border-radius:12px;padding:14px 16px;margin:12px 0}
table{border-collapse:collapse;width:100%;font-size:14px}th,td{text-align:left;padding:6px 8px;border-bottom:1px solid var(--line)}
th{color:var(--muted);font-weight:600;font-size:12px;text-transform:uppercase;letter-spacing:.04em}
.chip{display:inline-block;background:var(--chip);border-radius:999px;padding:2px 9px;font-size:12px;margin:2px 4px 2px 0;color:var(--ink)}
.chip.theme{background:color-mix(in srgb,var(--teal) 16%,transparent)}
.chip.flag{background:var(--badbg);color:var(--bad)}
.chip.new{background:color-mix(in srgb,var(--gold) 28%,transparent);font-weight:600}
.filters{display:flex;flex-wrap:wrap;gap:6px;margin:8px 0 4px}
.filters button{font:inherit;font-size:13px;border:1px solid var(--line);background:var(--card);color:var(--ink);border-radius:999px;padding:4px 12px;cursor:pointer}
.filters button[aria-pressed=true]{background:var(--navy);color:#fff;border-color:var(--navy)}
@media (prefers-color-scheme:dark){.filters button[aria-pressed=true]{background:var(--teal);border-color:var(--teal);color:#06121f}}
ol.items{list-style:none;padding:0;margin:0}
.item{background:var(--card);border:1px solid var(--line);border-radius:12px;padding:14px 16px;margin:10px 0;display:grid;grid-template-columns:44px 1fr;gap:4px 12px}
.rank{font-size:22px;font-weight:700;color:var(--slate);text-align:right;line-height:1.2}
.item h3{font-size:17px;line-height:1.35;margin:0}.item h3 a{color:var(--ink);text-decoration:none}.item h3 a:hover{color:var(--teal)}
.byline{color:var(--muted);font-size:13px;margin:4px 0}
.bars{display:flex;flex-wrap:wrap;gap:4px 16px;align-items:center;font-size:12px;color:var(--muted);margin:6px 0}
.bar{display:inline-flex;align-items:center;gap:6px}.bar i{display:inline-block;width:70px;height:6px;border-radius:3px;background:var(--line);position:relative;overflow:hidden}
.bar i b{position:absolute;inset:0 auto 0 0;background:var(--teal)}.bar.score i b{background:var(--gold)}
details{margin-top:6px;font-size:13px}summary{cursor:pointer;color:var(--teal)}
details dl{display:grid;grid-template-columns:max-content 1fr;gap:2px 12px;margin:8px 0 0}details dt{color:var(--muted)}details dd{margin:0;word-break:break-word}
.ids a{margin-right:10px;font-size:13px}
.cards{display:grid;grid-template-columns:repeat(auto-fit,minmax(280px,1fr));gap:12px}
.card{background:var(--card);border:1px solid var(--line);border-radius:12px;padding:16px;display:block;color:inherit;text-decoration:none}
.card:hover{border-color:var(--teal)}.card h3{margin:0 0 6px;font-size:17px}.card ol{margin:8px 0 0;padding-left:18px;font-size:13px;color:var(--muted)}
.hero{display:flex;align-items:center;gap:16px;margin:28px 0 8px}.hero img{height:64px}
footer{color:var(--muted);font-size:12px;border-top:1px solid var(--line);margin-top:36px;padding:16px 0 32px}
.note{font-size:13px;color:var(--muted)}
@media (max-width:560px){.item{grid-template-columns:1fr}.rank{text-align:left;font-size:16px}h1{font-size:24px}header.top nav a{margin-left:10px}}
"""

JS = """
document.querySelectorAll('.filters').forEach(function(bar){
  var buttons=bar.querySelectorAll('button');
  buttons.forEach(function(b){b.addEventListener('click',function(){
    buttons.forEach(function(x){x.setAttribute('aria-pressed',x===b?'true':'false')});
    var t=b.dataset.theme, shown=0;
    document.querySelectorAll('.item').forEach(function(li){
      var ok=!t||(' '+li.dataset.themes+' ').indexOf(' '+t+' ')>=0; li.hidden=!ok; if(ok)shown++;});
    var c=document.getElementById('shown'); if(c)c.textContent=shown;
  });});
});
"""


def _page(title: str, body: str, root: str, description: str = "") -> str:
    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>{e(title)}</title><meta name="description" content="{e(description)}">
<link rel="icon" href="{root}assets/favicon.png"><style>{CSS}</style></head>
<body><header class="top"><div class="wrap"><a href="{root}index.html" aria-label="CAROUSEL home"><picture>
<source media="(prefers-color-scheme: dark)" srcset="{root}assets/carousel-logo-dark.svg">
<img src="{root}assets/carousel-logo.svg" alt="CAROUSEL"></picture></a>
<nav><a href="{root}index.html">Radars</a><a href="{REPO_URL}">GitHub</a></nav></div></header>
<main class="wrap">{body}</main>
<footer class="wrap">{e(DISCLAIMER)} Retractions and corrections are flagged, never scored.
Generated by CAROUSEL {e(__version__)} · <a href="{REPO_URL}">source</a> · MIT license.</footer>
<script>{JS}</script></body></html>
"""


def _fmt_time(iso: Optional[str]) -> str:
    if not iso:
        return ""
    try:
        t = dt.datetime.strptime(iso[:19], "%Y-%m-%dT%H:%M:%S")
        return t.strftime("%Y-%m-%d %H:%M UTC")
    except ValueError:
        return iso


def _bar(label: str, value: Optional[float], cls: str = "") -> str:
    if value is None:
        return ""
    pct = max(0.0, min(1.0, value)) * 100
    return f'<span class="bar {cls}" title="{e(label)} {value:.3f}">{e(label)} <i><b style="width:{pct:.0f}%"></b></i> {value:.2f}</span>'


def _ids(paper: Dict[str, Any]) -> str:
    links = []
    if paper.get("doi"):
        links.append(f'<a href="https://doi.org/{e(paper["doi"])}">DOI</a>')
    if paper.get("pmid"):
        links.append(f'<a href="https://pubmed.ncbi.nlm.nih.gov/{e(paper["pmid"])}/">PubMed</a>')
    if paper.get("pmcid"):
        links.append(f'<a href="https://www.ncbi.nlm.nih.gov/pmc/articles/{e(paper["pmcid"])}/">PMC</a>')
    if paper.get("openalex"):
        links.append(f'<a href="https://openalex.org/{e(paper["openalex"])}">OpenAlex</a>')
    if paper.get("arxiv"):
        links.append(f'<a href="https://arxiv.org/abs/{e(paper["arxiv"])}">arXiv</a>')
    return f'<div class="ids">{"".join(links)}</div>' if links else ""


def _why(it: Dict[str, Any]) -> str:
    d = it["details"]
    nov = d.get("novelty") or {}
    rows = [
        ("Found by", ", ".join(d.get("retrieved_by", [])) or "-"),
        ("Matched radar terms", ", ".join(d.get("matched_terms", [])) or "none"),
        ("Methods matched", ", ".join(d.get("methods_matched", [])) or "none"),
        ("Similarity to radar profile", "-" if d.get("similarity") is None else f'{d["similarity"]:.3f}'),
        ("Keyword score (BM25, scaled)", "-" if d.get("bm25_norm") is None else f'{d["bm25_norm"]:.3f}'),
        ("Novelty", "not enough earlier papers yet" if d.get("novelty_raw") is None else
         f'{d["novelty_raw"]:.3f} raw vs {nov.get("history_size", 0)} earlier papers ({nov.get("normalization", "raw")}), '
         f'credited × {d.get("novelty_gate", 0):.2f} for relevance'),
        ("Publication kind", d.get("publication_kind", "-")),
        ("Sources", ", ".join(d.get("sources", []))),
        ("First seen by CAROUSEL", _fmt_time(d.get("first_seen"))),
        ("Paper ID", it["paper_id"]),
    ]
    dl = "".join(f"<dt>{e(k)}</dt><dd>{e(v)}</dd>" for k, v in rows)
    return f"<details><summary>Why this paper?</summary><dl>{dl}</dl></details>"


def _item(it: Dict[str, Any], themes: Dict[str, str], entered: set) -> str:
    d = it["details"]
    paper = d.get("paper", {})
    url = paper.get("url") or (f'https://doi.org/{paper["doi"]}' if paper.get("doi") else "")
    title = e(paper.get("title") or "(untitled)")
    title_html = f'<a href="{e(url)}">{title}</a>' if url else title
    byline = " · ".join(e(x) for x in ["; ".join(paper.get("authors") or []), paper.get("venue"),
                                         paper.get("earliest_public_date")] if x)
    sig = d.get("signals_matched", {})
    chips = "".join(f'<span class="chip theme">{e(themes.get(k, k))}</span>' for k in sig)
    chips += "".join(f'<span class="chip flag">{e(f.replace("_", " "))}</span>' for f in it.get("flags", [])
                     if f != "novelty_insufficient_history")
    if it["paper_id"] in entered:
        chips = '<span class="chip new">new on this list</span>' + chips
    comp = it.get("components", {})
    bars = _bar("score", it.get("score"), "score") + "".join(
        _bar(k.replace("_", " "), comp.get(k)) for k in ("relevance", "novelty", "method_match"))
    return (f'<li class="item" data-themes="{e(" ".join(sig))}"><div class="rank">{it["rank"]}</div><div>'
            f'<h3>{title_html}</h3><div class="byline">{byline}</div><div>{chips}</div>'
            f'<div class="bars">{bars}</div>{_ids(paper)}{_why(it)}</div></li>')


def _coverage(run: Dict[str, Any]) -> str:
    rows = []
    for c in run.get("coverage", []):
        if c.get("status") == "ok":
            got = f'{c.get("retrieved")} of {c.get("total_available") if c.get("total_available") is not None else "?"}'
            note = f'truncated: {c["truncation_reason"]}' if c.get("truncated") else "complete"
            if c.get("cost_unit") == "credits":
                note += f' · {c.get("cost", 0):g} credits'
        else:
            got, note = "-", c.get("error") or c.get("note") or ""
        rows.append(f"<tr><td>{e(c.get('source'))}</td><td>{e(c.get('status'))}</td><td>{e(got)}</td><td>{e(note)}</td></tr>")
    return ("<table><thead><tr><th>Source</th><th>Status</th><th>Retrieved</th><th>Notes</th></tr></thead>"
            f"<tbody>{''.join(rows)}</tbody></table>")


def radar_page(run: Dict[str, Any]) -> str:
    radar = run.get("radar") or {}
    themes = radar.get("themes") or {}
    ranking = run.get("ranking", {})
    window = ranking.get("window") or {}
    delta = run.get("delta") or {}
    entered = set(delta.get("entered") or []) if delta.get("previous_run_id") else set()
    items = run.get("items", [])
    used = [k for k in themes if any(k in (i["details"].get("signals_matched") or {}) for i in items)]
    filters = ""
    if used:
        buttons = '<button type="button" aria-pressed="true" data-theme="">All</button>' + "".join(
            f'<button type="button" aria-pressed="false" data-theme="{e(k)}">{e(themes[k])}</button>' for k in used)
        filters = f'<div class="filters" role="group" aria-label="Filter by theme">{buttons}</div>'
    weights = ", ".join(f"{k.replace('_', ' ')} {v:.2f}" for k, v in (ranking.get("weights") or {}).items())
    inactive = "; ".join(f"{k.replace('_', ' ')}: {v}" for k, v in (ranking.get("inactive_signals") or {}).items())
    delta_txt = ""
    if delta.get("previous_run_id"):
        delta_txt = (f'<span>Since the previous run: <b>{len(delta.get("entered", []))}</b> new, '
                     f'<b>{len(delta.get("left", []))}</b> dropped off, <b>{len(delta.get("moved", []))}</b> moved</span>')
    notes = "".join(f'<p class="note">Note: {e(n)}</p>' for n in run.get("notes", []))
    body = f"""
<h1>{e(run.get("radar_name"))}</h1>
<p class="lede">{e(radar.get("description"))}</p>
<div class="meta"><span>Updated <b>{e(_fmt_time(run.get("started_at")))}</b></span>
<span>Window <b>{e(window.get("start"))} → {e(window.get("end") or run.get("as_of"))}</b></span>
<span>Mode <b>{e(run.get("mode"))}</b></span><span>Ranking <b>{e(run.get("ranking_version"))}</b></span>
<span>Model <b>{e(run.get("model_version") or "none")}</b></span>{delta_txt}</div>
{notes}
<h2>Recommendations <span class="note">(<span id="shown">{len(items)}</span> shown)</span></h2>
{filters}
<ol class="items">{"".join(_item(i, themes, entered) for i in items)}</ol>
<h2>How this list was made</h2>
<div class="panel">{_coverage(run)}
<p class="note">Observed {e(run.get("n_observed"))} source records → {e(run.get("n_candidates"))} candidates → {len(items)} recommended.
Weights: {e(weights)}. Not yet scored: {e(inactive)}.</p>
<p class="note">Run <code>{e(run.get("run_id"))}</code> · config <code>{e(run.get("config_hash"))}</code> ·
<a href="latest.json">latest.json</a> · <a href="latest.csv">latest.csv</a></p></div>
"""
    return _page(f"{run.get('radar_name')} · CAROUSEL", body, "../../", radar.get("description") or "")


def index_page(runs: Sequence[Dict[str, Any]]) -> str:
    cards = []
    for run in runs:
        radar = run.get("radar") or {}
        top = "".join(f'<li>{e((i["details"].get("paper") or {}).get("title"))}</li>' for i in run.get("items", [])[:3])
        cards.append(
            f'<a class="card" href="radars/{e(run["radar_id"])}/index.html"><h3>{e(run.get("radar_name"))}</h3>'
            f'<div class="note">{e(radar.get("description"))}</div>'
            f'<div class="meta"><span>Updated <b>{e(_fmt_time(run.get("started_at")))}</b></span>'
            f'<span><b>{len(run.get("items", []))}</b> papers</span></div><ol>{top}</ol></a>'
        )
    body = f"""
<div class="hero"><img src="assets/carousel-icon.svg" alt="" aria-hidden="true">
<div><h1 style="margin:0">Discover the research that matters</h1>
<p class="lede" style="margin:4px 0 0">Live literature radars, rebuilt daily by CAROUSEL from PubMed and OpenAlex,
ranked by relevance, novelty, and method match, with the reasons shown for every paper.</p></div></div>
<h2>Radars</h2><div class="cards">{"".join(cards)}</div>
<p class="note">Want your own? Define a radar in YAML and run <code>carousel scan</code>; see the
<a href="{REPO_URL}#readme">README</a>.</p>
"""
    return _page("CAROUSEL · literature radars", body, "", "CAROUSEL literature radars")


def build(run_files: Iterable[Union[str, Path]], out: Union[str, Path], clean: bool = True) -> List[Path]:
    """Write the site for the given run exports into ``out``. Returns the files written."""
    from carousel.radar import ScanResults

    out = Path(out)
    runs = []
    for f in run_files:
        runs.append(json.loads(Path(f).read_text(encoding="utf-8")))
    if not runs:
        raise ValueError("no run exports given")
    ids = [r["radar_id"] for r in runs]
    if len(set(ids)) != len(ids):
        raise ValueError(f"more than one run per radar: {sorted(ids)}")
    runs.sort(key=lambda r: r["radar_id"])
    if clean and out.exists():
        shutil.rmtree(out)
    written: List[Path] = []

    def write(rel: str, text: str) -> None:
        p = out / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text, encoding="utf-8")
        written.append(p)

    for src, dest in ASSETS.items():
        (out / "assets").mkdir(parents=True, exist_ok=True)
        shutil.copyfile(LOGO_DIR / src, out / "assets" / dest)
        written.append(out / "assets" / dest)
    write(".nojekyll", "")
    write("index.html", index_page(runs))
    for run in runs:
        rid = run["radar_id"]
        write(f"radars/{rid}/index.html", radar_page(run))
        write(f"radars/{rid}/latest.json", json.dumps(run, indent=1, ensure_ascii=False) + "\n")
        csv_path = out / "radars" / rid / "latest.csv"
        ScanResults({**run, "items": run.get("items", [])}).to_csv(csv_path)
        written.append(csv_path)
    index = {
        "schema": "carousel.site/1",
        "generated_at_utc": dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "radars": [{"radar_id": r["radar_id"], "radar_name": r.get("radar_name"), "run_id": r["run_id"],
                    "started_at": r.get("started_at"), "ranking_version": r.get("ranking_version"),
                    "n_items": len(r.get("items", [])), "path": f"radars/{r['radar_id']}/latest.json"} for r in runs],
    }
    write("latest.json", json.dumps(index, indent=1) + "\n")
    return written
