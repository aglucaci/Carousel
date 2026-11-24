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
  const [first, second] = titles;

  // ---- why this paper? (from the stored run) ----
  await cards.nth(0).getByRole("button", { name: "Why this paper?" }).click();
  const drawer = page.getByRole("dialog", { name: "Why this paper?" });
  await expect(drawer.getByText("Ranked #1")).toBeVisible();
  await expect(drawer.getByRole("cell", { name: "Relevance" })).toBeVisible();
  await expect(drawer.getByText("From the stored run")).toBeVisible();
  await drawer.getByRole("button", { name: "Close" }).click();
  await expect(drawer).toBeHidden();

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

  // ---- without losing history ----
  await nav.getByRole("link", { name: "Library" }).click();
  await expect(page.getByTestId("library-item").locator("h2")).toHaveText(second);
  await nav.getByRole("link", { name: "Radars" }).click();
  await page.getByRole("link", { name: scenario.topic }).click();
  await page.getByTestId("run-row").nth(1).getByRole("link", { name: "View" }).click();
  await expect(page.getByText("Earlier run from")).toBeVisible();
  await expect(cards.first()).toHaveAttribute("data-dismissed", "true"); // the first run still has it, now dismissed
  await expect(cards.nth(1).locator("h2")).toHaveText(second);
});
