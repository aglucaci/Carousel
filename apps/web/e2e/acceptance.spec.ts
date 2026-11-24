/**
 * Spec Phase 1 acceptance: a user can define a topic, receive a deduplicated
 * ranked paper list, save or dismiss papers, and repeat the scan without
 * losing previous history. Plus the plan's task 13 pieces: approving the
 * proposed search strategy and the "Why this paper?" drawer.
 */
import { expect, test, type Page } from "@playwright/test";
import scenario from "./scenario.json";

const norm = (s: string) => s.toLowerCase().replace(/[^a-z0-9]+/g, " ").trim();

async function setScanOptions(page: Page) {
  await page.getByText("Scan options").click();
  await page.getByLabel("Results per source").fill(String(scenario.max_results));
  await page.getByLabel("Papers to recommend").fill(String(scenario.top_n));
  await page.getByLabel("Window ends on (replay)").fill(scenario.as_of);
}

async function waitForScan(page: Page) {
  await expect(page.getByTestId("job-status")).toHaveAttribute("data-status", "finished", { timeout: 120_000 });
}

test("define a topic, get a ranked list, save and dismiss, rescan without losing history", async ({ page }) => {
  // ---- define a topic ----
  await page.goto("/");
  await expect(page.getByText("No radars yet")).toBeVisible();
  await page.getByRole("link", { name: "Create a radar" }).click();

  await page.getByLabel("Topic", { exact: true }).fill(scenario.topic);
  await page.getByText("More options").click();
  await page.getByLabel("Window", { exact: true }).selectOption(String(scenario.window_days));
  await page.getByRole("button", { name: "Propose search strategy" }).click();

  // ---- approve the proposed search strategy ----
  await expect(page.getByRole("heading", { name: "Review the search strategy" })).toBeVisible();
  await expect(page.getByTestId("query-pubmed")).toHaveText(`(${scenario.topic})`);
  await expect(page.getByTestId("query-openalex")).toHaveText(`("${scenario.topic}")`);
  await setScanOptions(page);
  await page.getByRole("button", { name: "Approve and create radar" }).click();

  await expect(page.getByRole("heading", { name: scenario.topic })).toBeVisible();
  await waitForScan(page);
  await page.getByRole("link", { name: "See recommendations" }).click();

  // ---- a deduplicated, ranked list ----
  const cards = page.getByTestId("paper-card");
  await expect(cards).toHaveCount(scenario.top_n);
  const titles = await cards.locator("h2").allTextContents();
  expect(new Set(titles.map(norm)).size, "each paper appears once").toBe(titles.length);
  await expect(page.getByText("Latest run from")).toBeVisible();
  await expect(page.getByTestId("arrivals")).toContainText("first scan");
  const [first, second] = titles;

  // ---- why this paper? (from the stored run) ----
  await cards.nth(0).getByRole("button", { name: "Why this paper?" }).click();
  const drawer = page.getByRole("dialog", { name: "Why this paper?" });
  await expect(drawer.getByText("Ranked #1")).toBeVisible();
  await expect(drawer.getByRole("cell", { name: "Relevance" })).toBeVisible();
  await expect(drawer.getByText("From the stored run")).toBeVisible();
  // Phase 2: the explanation stored with the run, with its version.
  await expect(drawer.getByTestId("explanation")).toContainText("Matches the radar's terms");
  await expect(drawer.getByText("Explanation version 1,")).toBeVisible();
  await drawer.getByRole("button", { name: "Close" }).click();
  await expect(drawer).toBeHidden();
  await expect(cards.nth(0).getByTestId("why-summary")).toContainText("Matches");

  // Phase 2: a journal article whose preprint was found is linked to it; both stay separate papers.
  const withPreprint = cards.filter({ has: page.getByText("Has preprint", { exact: true }) }).first();
  await withPreprint.getByRole("button", { name: "Why this paper?" }).click();
  const versions = drawer.getByTestId("paper-versions");
  await expect(versions).toContainText("Preprint");
  await expect(versions).toContainText("Linked by Europe PMC");
  await expect(versions).toContainText("kept as its own paper");
  await drawer.getByRole("button", { name: "Close" }).click();
  await expect(drawer).toBeHidden();

  // ---- Phase 2: judge two papers; the next scan's profile moves toward one and away from the other ----
  for (const [i, answer] of [[2, "Relevant"], [3, "Not relevant"]] as const) {
    await cards.nth(i).getByRole("button", { name: "Why this paper?" }).click();
    await drawer.getByRole("button", { name: answer, exact: true }).click();
    await expect(drawer.getByRole("button", { name: answer, exact: true })).toHaveAttribute("aria-pressed", "true");
    await drawer.getByRole("button", { name: "Close" }).click();
    await expect(drawer).toBeHidden();
  }

  // ---- save one, dismiss another ----
  await cards.nth(1).getByRole("button", { name: "Save" }).click();
  await expect(cards.nth(1).getByRole("button", { name: "Saved" })).toBeVisible();
  await cards.nth(0).getByRole("button", { name: "Dismiss" }).click();
  await expect(cards.nth(0)).toHaveAttribute("data-dismissed", "true");

  const nav = page.getByRole("navigation", { name: "Main" });
  await nav.getByRole("link", { name: "Library" }).click();
  await expect(page.getByTestId("library-item")).toHaveCount(1);
  await expect(page.getByTestId("library-item").locator("h2")).toHaveText(second);

  // ---- repeat the scan ----
  await nav.getByRole("link", { name: "Radars" }).click();
  await page.getByRole("link", { name: scenario.topic }).click();
  await setScanOptions(page);
  await page.getByRole("button", { name: "Run scan now" }).click();
  await waitForScan(page);
  await expect(page.getByTestId("run-row")).toHaveCount(2);

  await page.getByRole("link", { name: "See recommendations" }).click();
  await expect(cards.first()).toBeVisible();
  const after = await cards.locator("h2").allTextContents();
  expect(after).not.toContain(first); // dismissed: not recommended again
  expect(after).not.toContain(second); // saved: kept in the Library, not re-recommended
  // Phase 2: arrivals against the previous scan (the same recorded responses, so nothing is new).
  await expect(page.getByTestId("arrivals")).toContainText("Nothing new since the previous scan");
  // The feedback measurably changed this ranking, compared with the same run ranked without it.
  await expect(page.getByTestId("feedback-effect")).toContainText("Your feedback (2 saved or relevant, 1 not relevant) changed this list");
  // Research Delta: the same recorded responses again, so no new publications and no revisions.
  await expect(page.getByTestId("events-line")).toHaveText("No new publications or revisions in this scan.");
  await page.getByRole("navigation", { name: "Main" }).getByRole("link", { name: "What's New" }).click();
  await expect(page.getByRole("heading", { name: "What's New", level: 1 })).toBeVisible();
  await expect(page.getByText("Nothing has changed yet")).toBeVisible();

  // ---- without losing history ----
  await nav.getByRole("link", { name: "Library" }).click();
  await expect(page.getByTestId("library-item").locator("h2")).toHaveText(second);
  await nav.getByRole("link", { name: "Radars" }).click();
  await page.getByRole("link", { name: scenario.topic }).click();
  await page.getByTestId("run-row").nth(1).getByRole("link", { name: "View" }).click();
  await expect(page.getByText("Earlier run from")).toBeVisible();
  await expect(cards.first()).toHaveAttribute("data-dismissed", "true"); // the first run still has it, now dismissed
  await expect(cards.nth(1).locator("h2")).toHaveText(second);

  // ---- Phase 2: schedule, pause, and resume, keeping history ----
  await nav.getByRole("link", { name: "Radars" }).click();
  await expect(page.getByRole("heading", { name: "Radars", level: 1 })).toBeVisible();
  await page.getByRole("link", { name: scenario.topic, exact: true }).click();
  await expect(page.getByTestId("schedule-status")).toHaveText("Scans run only when you start them.");
  await page.getByLabel("Repeat").selectOption("weekly");
  await page.getByRole("button", { name: "Pause" }).click();
  await expect(page.getByTestId("schedule-status")).toContainText("Paused");
  await page.reload();
  await expect(page.getByLabel("Repeat")).toHaveValue("weekly");
  await expect(page.getByTestId("run-row")).toHaveCount(2); // pausing keeps every run
  await page.getByRole("button", { name: "Resume" }).click();
  await expect(page.getByTestId("schedule-status")).toContainText("Scans weekly");
  await page.getByLabel("Repeat").selectOption("");
  await expect(page.getByTestId("schedule-status")).toHaveText("Scans run only when you start them.");

  // ---- Phase 2: each scan produced its digests; a failed email delivery is recorded with the reason ----
  const deliveries = page.getByTestId("digest-delivery");
  await expect(deliveries.filter({ hasText: "Markdown" }).filter({ hasText: "Delivered" })).toHaveCount(2);
  await expect(deliveries.filter({ hasText: "RSS feed" }).filter({ hasText: "Delivered" })).toHaveCount(2);
  const feed = await page.request.get("/api/v1/radars/viral-evolution/feed.xml");
  expect(feed.headers()["content-type"]).toContain("application/rss+xml");
  expect(await feed.text()).toContain("<rss version=\"2.0\">");
  await page.getByLabel("Email the digest to").fill("reader@example.org");
  await page.getByRole("button", { name: "Save", exact: true }).click();
  await page.getByRole("button", { name: "Send latest now" }).click();
  await expect(deliveries.filter({ hasText: "to reader@example.org" }).first()).toContainText("no SMTP server configured");
});
