/**
 * What's New v2 (plan task 29) on fixture data: the API responses come from a real engine computation
 * (e2e/fixtures/whats-new.json, built by make_whats_new_fixture.py) and are served by mocked routes.
 * Every item opens its evidence page, and every claim on it links a stored observation or paper.
 */
import { expect, test } from "@playwright/test";
import fixture from "./fixtures/whats-new.json" with { type: "json" };

type Ev = (typeof fixture.events)[number];

test.beforeEach(async ({ page }) => {
  await page.route("**/api/v1/health", (r) => r.fulfill({ json: { status: "ok" } }));
  await page.route("**/api/v1/radars", (r) => r.fulfill({ json: fixture.radars }));
  await page.route("**/api/v1/radars/lab/events", (r) => r.fulfill({ json: fixture.events }));
  await page.route("**/api/v1/events/*", (r) => {
    const id = decodeURIComponent(new URL(r.request().url()).pathname.split("/").pop() ?? "");
    const e = fixture.events.find((x: Ev) => x.event_id === id);
    return e ? r.fulfill({ json: e }) : r.fulfill({ status: 404, json: { detail: "not found" } });
  });
});

const GROUP_OF: Record<string, string> = {
  new_publication: "findings",
  new_methodological_connection: "methods",
  topic_acceleration: "growth",
  research_landscape_shift: "connections",
  publication_revision: "updates",
};

test("What's New groups every change and each opens its evidence", async ({ page }) => {
  await page.goto("/whats-new?radar=lab");
  for (const [type, group] of Object.entries(GROUP_OF)) {
    const n = fixture.events.filter((e: Ev) => e.type === type).length;
    await expect(page.getByTestId(`group-${group}`).getByTestId("event"), `${group}`).toHaveCount(n);
  }
  // A new finding shows the paper's own words.
  const pub = fixture.events.find((e: Ev) => e.type === "new_publication")!;
  await expect(page.getByTestId("group-findings")).toContainText(pub.findings[0].quote);

  for (const e of fixture.events as Ev[]) {
    await page.goto("/whats-new?radar=lab");
    await page.getByTestId(`group-${GROUP_OF[e.type]}`).getByRole("link", { name: e.title! }).click();
    await expect(page.getByRole("heading", { level: 1 })).toHaveText(e.title!);
    const papers = page.getByTestId("evidence-paper");
    await expect(papers).toHaveCount(e.papers.length);
    expect(e.papers.length, `${e.type} rests on at least one publication`).toBeGreaterThan(0);
    for (const [i, p] of e.papers.entries()) {
      await expect(papers.nth(i)).toContainText(p.paper_id); // each publication is a stored paper
    }
    if (e.type === "topic_acceleration") {
      const weeks = (e.evidence as { weekly: { week: string; count: number; denominator: number }[] }).weekly;
      const rows = page.getByTestId("evidence-week");
      await expect(rows).toHaveCount(weeks.length);
      for (const [i, w] of weeks.entries()) {
        // each number is a stored observation: the week, the topic's count, and the field's count
        await expect(rows.nth(i)).toHaveAttribute("data-week", w.week);
        await expect(rows.nth(i).locator("td").nth(1)).toHaveText(String(w.count));
        await expect(rows.nth(i).locator("td").nth(2)).toHaveText(String(w.denominator));
      }
      await expect(page.getByText("Checks passed")).toBeVisible();
    }
    if (e.type === "new_publication") {
      await expect(page.getByTestId("findings")).toContainText(e.findings[0].quote);
    }
  }
});
