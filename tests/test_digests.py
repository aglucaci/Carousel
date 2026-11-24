"""Digests: Markdown, RSS, email (plan task 20).

Done when: a scan produces each digest; failed deliveries are recorded.
"""

import contextlib
import datetime as dt
import io
import os
import smtplib
import tempfile
import unittest
import xml.etree.ElementTree as ET
from pathlib import Path
from unittest import mock

from carousel.notifications import (DeliveryError, SmtpSettings, digest_from_run, send_email, to_email, to_markdown,
                                    to_rss)
from carousel.notifications.mail import smtp_transport

UTC = dt.timezone.utc
T1 = dt.datetime(2026, 11, 20, 6, tzinfo=UTC)
T2 = dt.datetime(2026, 11, 21, 6, tzinfo=UTC)


def item(rank, pid, title, **kw):
    paper = {"title": title, "authors": ["Ana Lee", "Bo Chen"], "venue": "Virus Evolution",
             "earliest_public_date": "2026-11-18", "doi": kw.pop("doi", None), "url": kw.pop("url", None)}
    return {"rank": rank, "paper_id": pid, "flags": kw.pop("flags", []),
            "details": {"paper": paper, "explanation": {"summary": kw.pop("why", "Matches antigenic drift")}}}


RUN = {"run_id": "run_1", "radar_id": "flu", "as_of": "2026-11-20", "n_candidates": 120, "explanation_version": "1",
       "coverage": [{"source": "pubmed", "status": "ok", "truncated": True, "retrieved": 200, "total_available": 950,
                     "truncation_reason": "max_results"}, {"source": "openalex", "status": "ok"}],
       "ranking": {"stats": {"feedback_effect": {"entered": ["cp_c"], "left": [], "moved_up": 1, "moved_down": 0},
                             "profile": {"feedback_positive": 2, "feedback_negative": 1}}}}
ITEMS = [item(1, "cp_a", "H3N2 [clade] *drift* in 2026", doi="10.1/a", flags=["has_preprint", "low_relevance"]),
         item(2, "cp_b", "Antigenic cartography <of> B", url="https://example.org/b", flags=["corrected"])]


def digest(run=RUN, items=ITEMS, arrivals={"cp_a": "newly_published", "cp_b": "seen_before"}, at=T1, **kw):
    return digest_from_run(run, items, radar_name="Flu drift", arrivals=arrivals, executed_at=at, **kw)


class RenderTests(unittest.TestCase):
    def test_markdown(self) -> None:
        md = to_markdown(digest(app_url="https://carousel.example.org"))
        self.assertTrue(md.startswith("# Flu drift: 2 papers, 20 Nov 2026\n"))
        self.assertIn("## 1. [H3N2 \\[clade\\] \\*drift\\* in 2026](https://doi.org/10.1/a)", md)  # markup escaped
        self.assertIn("**Newly published** · **Has preprint**", md)
        self.assertIn("**Corrected**", md)
        self.assertNotIn("low_relevance", md)
        self.assertIn("Why: Matches antigenic drift.", md)
        self.assertIn("Since the previous scan: 1 newly published, 0 newly observed", md)
        self.assertIn("changed this list: 1 moved up, 1 brought in", md)
        self.assertIn("- PubMed: 200 of 950 retrieved (max\\_results)", md)
        self.assertIn("[open in CAROUSEL](https://carousel.example.org/?radar=flu&run=run_1)", md)
        first = to_markdown(digest(arrivals=None, items=ITEMS[:1]))
        self.assertIn("The radar's first scan", first)
        self.assertTrue(first.startswith("# Flu drift: 1 paper, 20 Nov 2026"))

    def test_rss_lists_each_paper_once_from_its_first_run(self) -> None:
        later = digest(run={**RUN, "run_id": "run_2"}, at=T2,
                       items=[item(1, "cp_b", "Antigenic cartography <of> B"), item(2, "cp_c", "New one")])
        xml = to_rss([later, digest()], radar_id="flu", radar_name="Flu drift", link="https://carousel.example.org/")
        root = ET.fromstring(xml.split("\n", 1)[1])
        items = root.findall("./channel/item")
        self.assertEqual([i.findtext("guid") for i in items],
                         ["carousel:flu:cp_c", "carousel:flu:cp_a", "carousel:flu:cp_b"])  # newest run first
        by_guid = {i.findtext("guid"): i for i in items}
        self.assertEqual(by_guid["carousel:flu:cp_b"].findtext("pubDate"), "Fri, 20 Nov 2026 06:00:00 +0000")  # first run
        self.assertEqual(by_guid["carousel:flu:cp_a"].findtext("link"), "https://doi.org/10.1/a")
        self.assertIn("Why: Matches antigenic drift.", by_guid["carousel:flu:cp_a"].findtext("description"))
        self.assertEqual([c.text for c in by_guid["carousel:flu:cp_a"].findall("category")], ["Newly published", "Has preprint"])
        self.assertEqual(root.findtext("./channel/title"), "CAROUSEL: Flu drift")

    def test_email(self) -> None:
        msg = to_email(digest(), sender="carousel@example.org", recipient="reader@example.org")
        self.assertEqual(msg["Subject"], "CAROUSEL · Flu drift: 1 new of 2 papers (20 Nov 2026)")
        self.assertEqual((msg["To"], msg["X-Carousel-Run"]), ("reader@example.org", "run_1"))
        text = msg.get_body(("plain",)).get_content()
        html = msg.get_body(("html",)).get_content()
        self.assertIn("1. H3N2 [clade] *drift* in 2026", text)
        self.assertIn("Antigenic cartography &lt;of&gt; B", html)  # escaped
        self.assertIn('href="https://doi.org/10.1/a"', html)


class MailTests(unittest.TestCase):
    MSG = to_email(digest(), sender="carousel@example.org", recipient="reader@example.org")

    def test_unconfigured_is_a_failure_with_a_reason(self) -> None:
        with self.assertRaisesRegex(DeliveryError, "no SMTP server configured"):
            send_email(self.MSG, SmtpSettings.from_env({}))
        with self.assertRaisesRegex(DeliveryError, "CAROUSEL_SMTP_SECURITY"):
            SmtpSettings.from_env({"CAROUSEL_SMTP_SECURITY": "tls13"})

    def test_server_errors_become_readable_failures(self) -> None:
        settings = SmtpSettings.from_env({"CAROUSEL_SMTP_HOST": "smtp.example.org", "CAROUSEL_SMTP_PASSWORD": "s3cret"})
        self.assertNotIn("s3cret", repr(settings))

        def refuse(_s, _m):
            raise smtplib.SMTPRecipientsRefused({"reader@example.org": (550, b"no such user")})

        with self.assertRaisesRegex(DeliveryError, "recipient refused: reader@example.org: 550 no such user"):
            send_email(self.MSG, settings, refuse)

        def down(_s, _m):
            raise ConnectionRefusedError(61, "Connection refused")

        with self.assertRaisesRegex(DeliveryError, "ConnectionRefusedError"):
            send_email(self.MSG, settings, down)

    def test_smtp_session(self) -> None:
        calls = []

        class FakeSMTP:
            def __init__(self, host, port, timeout):
                calls.append(("connect", host, port))

            def __enter__(self):
                return self

            def __exit__(self, *a):
                calls.append(("quit",))

            def starttls(self, context):
                calls.append(("starttls",))

            def login(self, user, password):
                calls.append(("login", user))

            def send_message(self, msg):
                calls.append(("send", msg["To"]))

        settings = SmtpSettings.from_env({"CAROUSEL_SMTP_HOST": "smtp.example.org", "CAROUSEL_SMTP_USER": "u",
                                          "CAROUSEL_SMTP_PASSWORD": "p"})
        with mock.patch("smtplib.SMTP", FakeSMTP):
            smtp_transport(settings, self.MSG)
        self.assertEqual(calls, [("connect", "smtp.example.org", 587), ("starttls",), ("login", "u"),
                                 ("send", "reader@example.org"), ("quit",)])


class EngineTests(unittest.TestCase):
    """`carousel scan --digest-dir` and `carousel digest` on the local store (recorded HTTP, no network)."""

    def test_scan_writes_markdown_and_rss_and_digest_command(self) -> None:
        from carousel import cli
        from test_scan import RADAR_YML, connectors

        with tempfile.TemporaryDirectory() as d, \
                mock.patch.dict("carousel.radar.CONNECTORS", {"pubmed": lambda: connectors()["pubmed"],
                                                              "openalex": lambda: connectors()["openalex"]}, clear=True):
            radar = Path(d) / "escape.yml"
            radar.write_text(RADAR_YML)
            store, out = str(Path(d) / "s.db"), Path(d) / "digests"
            for day in ("2026-10-14", "2026-10-14"):
                with contextlib.redirect_stdout(io.StringIO()):
                    code = cli.main(["scan", str(radar), "--store", store, "--as-of", day, "--max-results", "5",
                                     "--embedder", "hashing", "--digest-dir", str(out), "-q"])
                self.assertEqual(code, 0)
            mds = sorted(out.glob("escape-2026-10-14-run_*.md"))
            self.assertEqual(len(mds), 2)
            text = mds[0].read_text()
            self.assertIn("# Escape radar:", text)
            feed = ET.fromstring((out / "escape.xml").read_text().split("\n", 1)[1])
            guids = [i.findtext("guid") for i in feed.findall("./channel/item")]
            self.assertEqual(len(guids), len(set(guids)))  # two runs, each paper once

            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                self.assertEqual(cli.main(["digest", "escape", "--store", store]), 0)
            self.assertIn("Nothing new since the previous scan.", buf.getvalue())  # the rescan saw the same papers
            err = io.StringIO()
            with contextlib.redirect_stderr(err), mock.patch.dict(os.environ, {}, clear=False):
                os.environ.pop("CAROUSEL_SMTP_HOST", None)
                code = cli.main(["digest", "escape", "--store", store, "--format", "email", "--to", "reader@example.org"])
            self.assertEqual(code, 1)
            self.assertIn("reader@example.org: no SMTP server configured", err.getvalue())


try:
    from test_api import ApiTestCase, RADAR
except Exception:  # pragma: no cover - server extras missing
    ApiTestCase = None


if ApiTestCase is not None:
    class DigestApiTests(ApiTestCase):
        """Through the API and the scan worker: each scan produces every digest; failures are recorded."""

        def test_a_scan_produces_each_digest_and_records_failures(self) -> None:
            self.post("/radars", {"config": RADAR})
            r = self.client.patch("/api/v1/radars/escape", json={"digest_emails": ["reader@example.org", " Reader@example.org "]},
                                  headers=self.auth)
            self.assertEqual(r.json()["digest_emails"], ["reader@example.org"])
            bad = self.client.patch("/api/v1/radars/escape", json={"digest_emails": ["not-an-address"]}, headers=self.auth)
            self.assertEqual(bad.status_code, 422)
            self.assertIn("not an email address: not-an-address", bad.text)

            with mock.patch.dict(os.environ, {}, clear=False):
                os.environ.pop("CAROUSEL_SMTP_HOST", None)
                run_id = self.scan()  # no SMTP server configured: the email delivery fails, and says why
            rows = self.get("/radars/escape/digests").json()
            by = {r["channel"]: r for r in rows}
            self.assertEqual({r["channel"] for r in rows}, {"markdown", "rss", "email"})
            self.assertEqual((by["markdown"]["status"], by["rss"]["status"]), ("delivered", "delivered"))
            self.assertEqual((by["email"]["status"], by["email"]["recipient"], by["email"]["error"]),
                             ("failed", "reader@example.org", "no SMTP server configured (set CAROUSEL_SMTP_HOST)"))
            self.assertTrue(all(r["run_id"] == run_id for r in rows))

            md = self.get("/radars/escape/digest.md")
            self.assertTrue(md.headers["content-type"].startswith("text/markdown"))
            self.assertIn("# Escape radar:", md.text)
            feed = self.get("/radars/escape/feed.xml")
            self.assertTrue(feed.headers["content-type"].startswith("application/rss+xml"))
            self.assertGreater(len(ET.fromstring(feed.text.split("\n", 1)[1]).findall("./channel/item")), 0)

            # Sending again with a server that accepts it: delivered, and recorded next to the failure.
            sent = []
            with mock.patch("carousel.notifications.mail.smtp_transport", lambda s, m: sent.append(m)), \
                    mock.patch.dict(os.environ, {"CAROUSEL_SMTP_HOST": "smtp.example.org", "CAROUSEL_SMTP_FROM": "c@example.org"}):
                r = self.post("/radars/escape/digests/email")
            self.assertEqual([(x["status"], x["recipient"]) for x in r.json()], [("delivered", "reader@example.org")])
            self.assertEqual((sent[0]["To"], sent[0]["From"]), ("reader@example.org", "c@example.org"))
            statuses = [(r["channel"], r["status"]) for r in self.get("/radars/escape/digests").json()]
            self.assertEqual(statuses[0], ("email", "delivered"))
            self.assertIn(("email", "failed"), statuses)

        def test_unknown_radar(self) -> None:
            self.assertEqual(self.get("/radars/nope/feed.xml").status_code, 404)
            self.assertEqual(self.get("/radars/nope/digests").status_code, 404)


if __name__ == "__main__":
    unittest.main()
