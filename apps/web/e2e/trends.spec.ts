/**
 * Trends (plan task 30) on fixture data from a real engine computation (e2e/fixtures/trends.json): the plotted
 * values are the model's output, provisional weeks are marked, and the same numbers are in a table view.
 */
import { expect, test } from "@playwright/test";
import fixture from "./fixtures/trends.json" with { type: "json" };

const report = fixture.trends.reports[0];

test.beforeEach(async ({ page }) => {
  await page.route("**/api/v1/health", (r) => r.fulfill({ json: { status: "ok" } }));
  await page.route("**/api/v1/radars", (r) => r.fulfill({ json: fixture.radars }));
  await page.route("**/api/v1/radars/lab/topics/trends*", (r) => r.fulfill({ json: fixture.trends }));
});

test("charts show the model's output, provisional weeks, and a table of the same numbers", async ({ page }) => {
  await page.goto("/trends?radar=lab");
  await expect(page.getByTestId("trend-topic")).toHaveCount(report.topics.length);
  await expect(page.getByTestId("trend-scope")).toContainText("evaluated through");
  // flagged first
  await expect(page.getByTestId("trend-topic").first()).toContainText("Growing");

  const cards = page.locator("div.rounded-lg", { has: page.getByTestId("trend-topic") });
  for (const t of report.topics) {
    const card = cards.filter({ has: page.locator(`[data-topic="${t.topic_id}"]`) });
    const points = card.getByTestId("share-point");
    await expect(points).toHaveCount(t.weekly.length);
    for (const [i, w] of t.weekly.entries()) {
      await expect(points.nth(i)).toHaveAttribute("data-week", w.week);
      expect(Number(await points.nth(i).getAttribute("data-share"))).toBeCloseTo(w.share!, 10); // the observed share
      await expect(points.nth(i)).toHaveAttribute("data-provisional", w.provisional ? "true" : "false");
    }
    const base = card.getByTestId("baseline-point");
    await expect(base).toHaveCount(t.baseline_share?.length ?? 0);
    for (const [i, b] of (t.baseline_share ?? []).entries()) {
      expect(Number(await base.nth(i).getAttribute("data-share"))).toBeCloseTo(b.share, 10); // the fitted baseline
    }
    if (report.provisional_weeks.length) await expect(card.getByTestId("provisional-region")).toBeVisible();
    // the table view has the same numbers
    await card.getByRole("button", { name: "Show as table" }).click();
    const rows = card.getByTestId("trend-table").locator("tbody tr");
    await expect(rows).toHaveCount(t.weekly.length);
    const last = t.weekly[t.weekly.length - 1];
    await expect(rows.last()).toContainText(String(last.count));
    await expect(rows.last()).toContainText(last.provisional ? "provisional" : "settled");
  }
  // the chart has a text alternative
  await expect(page.getByTestId("trend-chart").first()).toHaveAttribute("aria-label", /share .* in the week of/);
});
