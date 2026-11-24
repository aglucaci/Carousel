"""Digests of a radar's runs: Markdown, RSS 2.0, and email (plan task 20).

A digest is built from a **stored run** only (its items, their stored
explanations, and the run's coverage), so it says exactly what the run said:

- **Markdown**: one document per run;
- **RSS**: a feed over the radar's recent runs, one entry per paper (the run
  that first recommended it), so a feed reader shows each paper once;
- **email**: the same digest as plain text and HTML (sent by
  :mod:`carousel.notifications.mail`).

Each item carries how it arrived relative to the radar's previous run (newly
published, newly observed, seen before), its flags (corrections, retractions,
linked versions), and the one-line summary of its stored explanation.
"""

from __future__ import annotations

import dataclasses
import datetime as dt
import html
from email.message import EmailMessage
from email.utils import format_datetime
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple
from xml.etree import ElementTree as ET

DIGEST_VERSION = "1"

FLAG_LABELS = {
    "retracted": "Retracted",
    "corrected": "Corrected",
    "expression_of_concern": "Expression of concern",
    "correction_or_notice": "Correction or notice",
    "has_preprint": "Has preprint",
    "published_version": "Since published",
}
ARRIVAL_LABELS = {"newly_published": "Newly published", "newly_observed": "Newly observed"}
SOURCE_NAMES = {"pubmed": "PubMed", "openalex": "OpenAlex", "europepmc": "Europe PMC", "crossref": "Crossref"}


@dataclasses.dataclass(frozen=True)
class DigestItem:
    rank: int
    paper_id: str
    title: str
    url: Optional[str]
    authors: Tuple[str, ...]
    venue: Optional[str]
    date: Optional[str]
    why: Optional[str]
    flags: Tuple[str, ...]
    arrival: Optional[str]

    @property
    def labels(self) -> List[str]:
        out = [ARRIVAL_LABELS[self.arrival]] if self.arrival in ARRIVAL_LABELS else []
        return out + [FLAG_LABELS[f] for f in self.flags if f in FLAG_LABELS]

    @property
    def meta(self) -> str:
        return " · ".join(x for x in (", ".join(self.authors), self.venue, _date(self.date)) if x)


@dataclasses.dataclass(frozen=True)
class Digest:
    radar_id: str
    radar_name: str
    run_id: str
    as_of: str
    executed_at: dt.datetime
    n_candidates: int
    items: Tuple[DigestItem, ...]
    arrivals: Optional[Dict[str, int]]   # None on the radar's first run
    coverage: Tuple[str, ...]            # one line per source that was cut short or failed
    feedback: Optional[str]
    explanation_version: Optional[str]
    app_url: Optional[str] = None        # link back to the run in the app, when CAROUSEL_PUBLIC_URL is set
    changes: Tuple[str, ...] = ()        # revisions to papers the radar watches (Research Delta), one line each
    new_publications: int = 0            # new_publication events in this run

    @property
    def title(self) -> str:
        return f"{self.radar_name}: {_papers(len(self.items))}, {_date(self.as_of)}"

    @property
    def intro(self) -> List[str]:
        lines = [f"Scan of {_date(self.executed_at.date().isoformat())} covering papers up to {_date(self.as_of)}: "
                 f"{self.n_candidates:,} candidates, {len(self.items)} recommended."]
        if self.arrivals is None:
            lines.append("The radar's first scan: nothing earlier to compare with.")
        elif not self.arrivals.get("newly_published") and not self.arrivals.get("newly_observed"):
            lines.append("Nothing new since the previous scan.")
        else:
            lines.append(f"Since the previous scan: {self.arrivals.get('newly_published', 0)} newly published, "
                         f"{self.arrivals.get('newly_observed', 0)} newly observed (older, but new to this radar).")
        if self.new_publications:
            lines.append(f"New publications (never seen by this radar, published in the window): {self.new_publications}.")
        if self.feedback:
            lines.append(self.feedback)
        return lines


def _papers(n: int) -> str:
    return f"{n} paper" if n == 1 else f"{n} papers"


def _date(iso: Optional[str]) -> str:
    if not iso:
        return ""
    try:
        if len(iso) == 10:
            d = dt.date.fromisoformat(iso)
            return f"{d.day} {d:%b %Y}"
        if len(iso) == 7:
            return dt.date.fromisoformat(iso + "-01").strftime("%b %Y")
    except ValueError:
        pass
    return iso


def _coverage_line(c: Mapping[str, Any]) -> Optional[str]:
    name = SOURCE_NAMES.get(c.get("source", ""), c.get("source", "?"))
    if c.get("status") != "ok":
        return f"{name}: {c.get('status')}" + (f" ({c['error']})" if c.get("error") else "")
    if c.get("truncated"):
        return f"{name}: {c.get('retrieved')} of {c.get('total_available')} retrieved ({c.get('truncation_reason')})"
    return None


def _feedback_line(stats: Mapping[str, Any]) -> Optional[str]:
    e = stats.get("feedback_effect")
    if not e:
        return None
    p = stats.get("profile") or {}
    given = f"{p.get('feedback_positive', 0)} saved or relevant, {p.get('feedback_negative', 0)} not relevant"
    parts = [f"{e[k]} moved {k.split('_')[1]}" for k in ("moved_up", "moved_down") if e.get(k)]
    if e.get("entered"):
        parts.append(f"{len(e['entered'])} brought in")
    if not parts:
        return f"Your feedback ({given}) was applied but didn't change this list's order."
    return f"Your feedback ({given}) changed this list: {', '.join(parts)}, compared with the same run ranked without it."


REVISION_LABELS = {"retraction": "Retracted", "correction": "Corrected", "erratum": "Erratum",
                   "expression_of_concern": "Expression of concern", "new_version": "New version linked",
                   "partial_retraction": "Partially retracted", "withdrawal": "Withdrawn", "removal": "Removed"}


def change_line(e: Mapping[str, Any]) -> str:
    """One revision event as a sentence, with the evidence a reader can check."""
    ev = e.get("evidence") or {}
    label = REVISION_LABELS.get(e.get("kind") or "", (e.get("kind") or "Updated").replace("_", " ").capitalize())
    text = f"{label}: {e.get('title') or e.get('paper_id')}"
    n = ev.get("notice") or {}
    if n.get("notice_doi"):
        who = {"retraction-watch": "Retraction Watch"}.get(n.get("asserted_by") or "", n.get("asserted_by"))
        text += f" (notice https://doi.org/{n['notice_doi']}" + (f", reported by {who}" if who else "") + ")"
    v = ev.get("version") or {}
    if v:
        text += f" (its {'preprint' if v.get('role') == 'preprint' else 'published version'} is now linked"
        text += f" by {', '.join(v.get('linked_by') or ['a source'])})"
    return text


def _url(paper: Mapping[str, Any]) -> Optional[str]:
    if paper.get("doi"):
        return f"https://doi.org/{paper['doi']}"
    return paper.get("url")


def digest_from_run(run: Mapping[str, Any], items: Sequence[Mapping[str, Any]], *, radar_name: str,
                    arrivals: Optional[Mapping[str, str]], executed_at: dt.datetime,
                    app_url: Optional[str] = None, events: Sequence[Mapping[str, Any]] = ()) -> Digest:
    """A digest from a stored run.

    ``run`` holds run_id, radar_id, as_of, n_candidates, coverage, ranking (with stats), explanation_version;
    each item rank, paper_id, flags, and ``details`` (``paper`` summary and ``explanation``). ``arrivals`` maps
    paper IDs to newly_published | newly_observed | seen_before, or is None on the radar's first run.
    """
    out = []
    for it in sorted(items, key=lambda x: x["rank"]):
        d = it.get("details") or {}
        paper = d.get("paper") or {}
        expl = d.get("explanation") or {}
        out.append(DigestItem(
            rank=it["rank"], paper_id=it["paper_id"], title=(paper.get("title") or it.get("title") or it["paper_id"]).strip(),
            url=_url(paper), authors=tuple(paper.get("authors") or ()), venue=paper.get("venue"),
            date=paper.get("earliest_public_date"), why=expl.get("summary"), flags=tuple(it.get("flags") or ()),
            arrival=(arrivals or {}).get(it["paper_id"])))
    stats = (run.get("ranking") or {}).get("stats") or {}
    counts = None if arrivals is None else {a: sum(1 for v in arrivals.values() if v == a)
                                            for a in ("newly_published", "newly_observed", "seen_before")}
    cov = tuple(x for x in (_coverage_line(c) for c in run.get("coverage") or []) if x)
    link = f"{app_url.rstrip('/')}/?radar={run['radar_id']}&run={run['run_id']}" if app_url else None
    return Digest(radar_id=run["radar_id"], radar_name=radar_name, run_id=run["run_id"], as_of=str(run["as_of"]),
                  executed_at=executed_at, n_candidates=run.get("n_candidates") or 0, items=tuple(out), arrivals=counts,
                  coverage=cov, feedback=_feedback_line(stats), explanation_version=run.get("explanation_version"),
                  app_url=link,
                  changes=tuple(change_line(e) for e in events if e.get("type") == "publication_revision"),
                  new_publications=sum(1 for e in events if e.get("type") == "new_publication"))


def arrivals_from_store(store: Any, run: Mapping[str, Any]) -> Optional[Dict[str, str]]:
    """Arrival of each of ``run``'s items relative to the radar's earlier runs (the API's rule, on the local store)."""
    from carousel.core.paper import partial_date_bounds

    earlier = [r for r in store.list_runs(run["radar_id"]) if r["started_at"] < run["started_at"]]
    if not earlier:
        return None
    previous = max(earlier, key=lambda r: r["started_at"])
    seen: set = set()
    for r in earlier:
        full = store.get_run(r["run_id"]) or {}
        seen |= {store.resolve(it["paper_id"]) for it in full.get("items", [])}
        seen |= {store.resolve(o[2]) for o in full.get("observations", [])}
    out = {}
    as_of = dt.date.fromisoformat(previous["as_of"])
    for it in run["items"]:
        pid = store.resolve(it["paper_id"])
        date = ((it.get("details") or {}).get("paper") or {}).get("earliest_public_date")
        if pid in seen:
            out[it["paper_id"]] = "seen_before"
        elif date and partial_date_bounds(date)[0] > as_of:
            out[it["paper_id"]] = "newly_published"
        else:
            out[it["paper_id"]] = "newly_observed"
    return out


# -------------------------
# Markdown
# -------------------------

def _md(text: str) -> str:
    """Escape what Markdown would read as markup in a title or name."""
    for ch in "\\`*_[]<>#|":
        text = text.replace(ch, "\\" + ch)
    return text


def to_markdown(d: Digest) -> str:
    lines = [f"# {_md(d.title)}", ""] + [_md(x) for x in d.intro] + [""]
    if d.changes:
        lines += ["## Changes to papers you've seen", ""] + [f"- {_md(c)}" for c in d.changes] + [""]
    for it in d.items:
        title = f"[{_md(it.title)}]({it.url})" if it.url else _md(it.title)
        lines.append(f"## {it.rank}. {title}")
        lines.append("")
        if it.meta:
            lines.append(_md(it.meta) + "  ")
        if it.labels:
            lines.append(" · ".join(f"**{x}**" for x in it.labels) + "  ")
        if it.why:
            lines.append(f"Why: {_md(it.why)}.")
        lines.append("")
    if d.coverage:
        lines += ["---", "", "Sources cut short or unavailable in this scan:", ""] + [f"- {_md(c)}" for c in d.coverage] + [""]
    lines += ["---", "",
              f"Run `{d.run_id}` · explanations v{d.explanation_version or '—'} · digest v{DIGEST_VERSION}"
              + (f" · [open in CAROUSEL]({d.app_url})" if d.app_url else ""), ""]
    return "\n".join(lines)


# -------------------------
# RSS 2.0
# -------------------------

def to_rss(digests: Iterable[Digest], *, radar_id: str, radar_name: str, link: Optional[str] = None,
           max_items: int = 100) -> str:
    """One feed over several runs' digests; each paper appears once, from the run that first recommended it."""
    first: Dict[str, Tuple[Digest, DigestItem]] = {}
    for d in sorted(digests, key=lambda x: x.executed_at):
        for it in d.items:
            first.setdefault(it.paper_id, (d, it))
    entries = sorted(first.values(), key=lambda x: (x[0].executed_at, -x[1].rank), reverse=True)[:max_items]
    rss = ET.Element("rss", version="2.0")
    ch = ET.SubElement(rss, "channel")
    ET.SubElement(ch, "title").text = f"CAROUSEL: {radar_name}"
    ET.SubElement(ch, "link").text = link or "http://localhost:3000/"
    ET.SubElement(ch, "description").text = f"Papers recommended by the CAROUSEL radar “{radar_name}”."
    ET.SubElement(ch, "generator").text = f"CAROUSEL digest v{DIGEST_VERSION}"
    if entries:
        ET.SubElement(ch, "lastBuildDate").text = format_datetime(max(d.executed_at for d, _ in entries))
    for d, it in entries:
        item = ET.SubElement(ch, "item")
        ET.SubElement(item, "title").text = it.title
        if it.url:
            ET.SubElement(item, "link").text = it.url
        body = [html.escape(it.meta)] if it.meta else []
        if it.labels:
            body.append("<strong>" + html.escape(" · ".join(it.labels)) + "</strong>")
        if it.why:
            body.append("Why: " + html.escape(it.why) + ".")
        body.append(html.escape(f"Ranked #{it.rank} in the scan of {_date(d.executed_at.date().isoformat())}."))
        ET.SubElement(item, "description").text = "<p>" + "<br/>".join(body) + "</p>"
        ET.SubElement(item, "guid", isPermaLink="false").text = f"carousel:{radar_id}:{it.paper_id}"
        ET.SubElement(item, "pubDate").text = format_datetime(d.executed_at)
        for label in it.labels:
            ET.SubElement(item, "category").text = label
    return '<?xml version="1.0" encoding="UTF-8"?>\n' + ET.tostring(rss, encoding="unicode") + "\n"


# -------------------------
# Email
# -------------------------

def _text(d: Digest) -> str:
    lines = [d.title, "=" * len(d.title), ""] + d.intro + [""]
    if d.changes:
        lines += ["Changes to papers you've seen:"] + [f"- {c}" for c in d.changes] + [""]
    for it in d.items:
        lines.append(f"{it.rank}. {it.title}")
        for x in (it.meta, " · ".join(it.labels), f"Why: {it.why}." if it.why else "", it.url or ""):
            if x:
                lines.append(f"   {x}")
        lines.append("")
    if d.coverage:
        lines += ["Sources cut short or unavailable:"] + [f"- {c}" for c in d.coverage] + [""]
    if d.app_url:
        lines.append(f"Open in CAROUSEL: {d.app_url}")
    lines.append(f"Run {d.run_id}. You get this because your address is on this radar's digest list.")
    return "\n".join(lines) + "\n"


def _html(d: Digest) -> str:
    e = html.escape
    out = [f'<div style="font-family:Georgia,serif;max-width:640px;margin:auto;color:#1f2328">',
           f"<h1 style=\"font-size:22px\">{e(d.title)}</h1>"]
    out += [f'<p style="font-family:sans-serif;font-size:14px;color:#57606a">{e(x)}</p>' for x in d.intro]
    if d.changes:
        out.append('<h2 style="font-size:17px">Changes to papers you\'ve seen</h2><ul style="font-family:sans-serif;font-size:14px">'
                   + "".join(f"<li>{e(c)}</li>" for c in d.changes) + "</ul>")
    for it in d.items:
        title = f'<a href="{e(it.url)}" style="color:#1f4e79">{e(it.title)}</a>' if it.url else e(it.title)
        out.append(f'<h2 style="font-size:17px;margin:20px 0 4px">{it.rank}. {title}</h2>')
        out.append(f'<p style="font-family:sans-serif;font-size:13px;color:#57606a;margin:0">{e(it.meta)}</p>')
        if it.labels:
            out.append(f'<p style="font-family:sans-serif;font-size:12px;margin:4px 0"><strong>{e(" · ".join(it.labels))}</strong></p>')
        if it.why:
            out.append(f'<p style="font-family:sans-serif;font-size:13px;margin:4px 0">Why: {e(it.why)}.</p>')
    if d.coverage:
        out.append('<p style="font-family:sans-serif;font-size:12px;color:#57606a">Sources cut short or unavailable: '
                   + e("; ".join(d.coverage)) + "</p>")
    foot = f"Run {e(d.run_id)}. You get this because your address is on this radar's digest list."
    if d.app_url:
        foot = f'<a href="{e(d.app_url)}">Open in CAROUSEL</a>. ' + foot
    out.append(f'<p style="font-family:sans-serif;font-size:12px;color:#57606a;margin-top:24px">{foot}</p></div>')
    return "\n".join(out)


def to_email(d: Digest, *, sender: str, recipient: str) -> EmailMessage:
    """The digest as one email to one recipient (plain text with an HTML alternative)."""
    msg = EmailMessage()
    new = (d.arrivals or {}).get("newly_published", 0) + (d.arrivals or {}).get("newly_observed", 0)
    msg["Subject"] = f"CAROUSEL · {d.radar_name}: " + (f"{new} new of {_papers(len(d.items))}" if d.arrivals else
                                                       _papers(len(d.items))) + f" ({_date(d.as_of)})"
    msg["From"] = sender
    msg["To"] = recipient
    msg["X-Carousel-Run"] = d.run_id
    msg.set_content(_text(d))
    msg.add_alternative(_html(d), subtype="html")
    return msg


__all__ = ["ARRIVAL_LABELS", "DIGEST_VERSION", "Digest", "DigestItem", "FLAG_LABELS", "arrivals_from_store",
           "digest_from_run", "to_email", "to_markdown", "to_rss"]
