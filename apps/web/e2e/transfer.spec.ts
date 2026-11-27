/**
 * Method Transfer (plan task 35) in the UI, on real API responses (e2e/fixtures/transfer.json, built by
 * make_transfer_fixture.py from the task-35 test scenario): a transfer is labelled a hypothesis on its card, and the
 * drawer states it as a hypothesis with the paper's own quote; papers using the method in the radar's own field are not.
 */
import { expect, test } from "@playwright/test";
import fixture from "./fixtures/transfer.json" with { type: "json" };

const fx = fixture as unknown as {
  radars: unknown;
  papers: { items: { paper_id: string }[] };
  paper: Record<string, unknown>;
  explanation: Record<string, { details: { transfer: { quote: string } } }>;
  transfers: string[];
  near: string[];
};

test.beforeEach(async ({ page }) => {
  await page.route("**/api/v1/health", (r) => r.fulfill({ json: { status: "ok" } }));
  await page.route("**/api/v1/radars", (r) => r.fulfill({ json: fx.radars }));
  await page.route("**/api/v1/radars/escape/papers*", (r) => r.fulfill({ json: fx.papers }));
  await page.route(/\/api\/v1\/papers\/([^/?]+)\/explanation/, (r) => {
    const id = decodeURIComponent(new URL(r.request().url()).pathname.split("/").at(-2)!);
    return r.fulfill({ json: fx.explanation[id] });
  });
  await page.route(/\/api\/v1\/papers\/([^/?]+)$/, (r) => {
    const id = decodeURIComponent(new URL(r.request().url()).pathname.split("/").at(-1)!);
    return r.fulfill({ json: fx.paper[id] });
  });
});

test("a method transfer is labelled a hypothesis, with the paper's own words", async ({ page }) => {
  await page.goto("/?radar=escape");
  const cards = page.getByTestId("paper-card");
  await expect(cards).toHaveCount(fx.papers.items.length);
  const card = (id: string) => page.locator(`[data-testid="paper-card"][data-paper="${id}"]`);
  for (const id of fx.transfers) {
    await expect(card(id)).toContainText("Method transfer · hypothesis");
    await expect(card(id).getByTestId("why-summary")).toContainText("Method transfer (a hypothesis): deep mutational scanning from");
  }
  for (const id of fx.near) await expect(card(id)).not.toContainText("Method transfer");
  await expect(page.getByText("Method transfer · hypothesis")).toHaveCount(fx.transfers.length);

  const id = fx.transfers[0];
  await card(id).getByRole("button", { name: "Why this paper?" }).click();
  const drawer = page.getByRole("dialog", { name: "Why this paper?" });
  await expect(drawer.getByRole("heading", { name: "Method transfer: a hypothesis" })).toBeVisible();
  await expect(drawer.getByTestId("transfer")).toContainText("an idea to consider, not a finding");
  await expect(drawer.getByTestId("transfer-quote")).toContainText(fx.explanation[id].details.transfer.quote);
  await expect(drawer.getByTestId("explanation")).toContainText("Hypothesis, not a finding");
});
