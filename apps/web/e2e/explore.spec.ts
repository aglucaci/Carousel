/**
 * Explore (plan task 33) on fixture data from a real engine computation (e2e/fixtures/explore.json, built by
 * make_explore_fixture.py from the recorded task-32 scan): every node and link drawn matches the graph, every citation
 * drawn is a stored edge (and every stored edge between these papers is drawn), the graph works from the keyboard,
 * and the list view shows the same neighbourhood.
 */
import { expect, test } from "@playwright/test";
import fixture from "./fixtures/explore.json" with { type: "json" };

const g = fixture.graph;
const touching = (id: string) => g.links.filter((l) => l.source === id || l.target === id);

test.beforeEach(async ({ page }) => {
  await page.route("**/api/v1/health", (r) => r.fulfill({ json: { status: "ok" } }));
  await page.route("**/api/v1/radars", (r) => r.fulfill({ json: fixture.radars }));
  await page.route("**/api/v1/radars/escape/graph*", (r) => r.fulfill({ json: g }));
});

test("draws the stored graph: every node, every link, and only stored citations", async ({ page }) => {
  await page.goto("/explore?radar=escape");
  await expect(page.getByRole("link", { name: "Explore" })).toHaveAttribute("aria-current", "page");
  const nodes = page.getByTestId("graph-node");
  await expect(nodes).toHaveCount(g.nodes.length);
  expect((await nodes.evaluateAll((els) => els.map((e) => e.getAttribute("data-paper")))).sort()).toEqual(
    g.nodes.map((n) => n.paper_id).sort(),
  );
  // seeds are squares, saved papers diamonds, the rest circles: shape, not colour
  for (const n of g.nodes) {
    const shape = n.roles.includes("seed") ? "rect" : n.roles.includes("saved") || n.roles.includes("relevant") ? "path" : "circle:not(.ring)";
    await expect(page.locator(`[data-testid="graph-node"][data-paper="${n.paper_id}"] ${shape}`).first()).toBeAttached();
  }
  const drawn = await page.getByTestId("graph-link").evaluateAll((els) =>
    els.map((e) => [e.getAttribute("data-kind"), e.getAttribute("data-source"), e.getAttribute("data-target")]),
  );
  expect(drawn.sort()).toEqual(g.links.map((l) => [l.kind, l.source, l.target]).sort());
  const cites = drawn.filter((d) => d[0] === "citation").map((d) => [d[1], d[2]]);
  expect(cites.sort()).toEqual([...fixture.stored_citations].sort()); // the drawing is the store's edge table
  expect(g.topics.length).toBeGreaterThan(1);
  await expect(page.getByTestId("graph-topic")).toHaveCount(g.topics.length);
  await expect(page.getByTestId("graph")).toHaveAttribute("aria-label", new RegExp(`${g.nodes.length} papers and ${g.stats.citation_links} citation links`));
});

test("works from the keyboard and explains each connection", async ({ page }) => {
  await page.goto("/explore?radar=escape");
  const nodes = page.getByTestId("graph-node");
  await nodes.first().focus();
  await page.keyboard.press("ArrowRight");
  await expect(nodes.nth(1)).toBeFocused();
  await page.keyboard.press("ArrowLeft");
  await expect(nodes.first()).toBeFocused();

  // a seed: the papers that cite it
  const seed = g.nodes.find((n) => n.roles.includes("seed") && touching(n.paper_id).length > 0)!;
  const node = page.locator(`[data-testid="graph-node"][data-paper="${seed.paper_id}"]`);
  await node.focus();
  await page.keyboard.press("Enter");
  await expect(node).toHaveAttribute("aria-pressed", "true");
  const panel = page.getByTestId("selected");
  await expect(panel).toContainText(seed.title!);
  await expect(panel).toContainText("seed paper");
  await expect(panel.getByTestId("connection")).toHaveCount(touching(seed.paper_id).length);
  const citer = touching(seed.paper_id).find((l) => l.kind === "citation" && l.target === seed.paper_id)!;
  const row = panel.locator(`[data-testid="connection"][data-other="${citer.source}"][data-kind="citation"]`);
  await expect(row).toContainText("is cited by");
  // follow the link to the citing paper: it now says it cites the seed
  await row.getByRole("button").click();
  await expect(page.locator(`[data-testid="graph-node"][data-paper="${citer.source}"]`)).toHaveAttribute("aria-pressed", "true");
  await expect(panel.locator(`[data-testid="connection"][data-other="${seed.paper_id}"][data-kind="citation"]`)).toContainText("cites");
});

test("the list view shows the same neighbourhood", async ({ page }) => {
  await page.goto("/explore?radar=escape");
  await page.getByRole("button", { name: "Show as list" }).click();
  const items = page.getByTestId("graph-list-item");
  await expect(items).toHaveCount(g.nodes.length);
  for (const n of g.nodes) {
    const item = page.locator(`[data-testid="graph-list-item"][data-paper="${n.paper_id}"]`);
    await expect(item.getByTestId("connection")).toHaveCount(touching(n.paper_id).length);
  }
});
