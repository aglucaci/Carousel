/**
 * End-to-end test of the spec's Phase 1 acceptance flow, against the real stack:
 * Postgres + Redis (yours, disposable), the CAROUSEL API and scan worker (e2e/stack.py,
 * replaying recorded HTTP), and this app (`next start`, so run `npm run build` first).
 *
 *   CAROUSEL_E2E_DATABASE_URL=postgresql+psycopg://…/carousel_test \
 *   CAROUSEL_E2E_REDIS_URL=redis://127.0.0.1:6379/15 \
 *   CAROUSEL_PYTHON=/path/to/venv/bin/python  npm run e2e
 *
 * Or against a stack that is already running (docker-compose.e2e.yml at the repo root):
 *
 *   CAROUSEL_E2E_BASE_URL=http://127.0.0.1:3000  npm run e2e
 */
import { defineConfig, devices } from "@playwright/test";

const API_PORT = process.env.CAROUSEL_E2E_API_PORT ?? "18765";
const WEB_PORT = process.env.CAROUSEL_E2E_WEB_PORT ?? "13765";
const PYTHON = process.env.CAROUSEL_PYTHON ?? "python";
const BASE_URL = process.env.CAROUSEL_E2E_BASE_URL; // set: use that stack instead of starting one

export default defineConfig({
  testDir: "e2e",
  timeout: 180_000,
  expect: { timeout: 15_000 },
  fullyParallel: false,
  workers: 1,
  retries: 0, // the flow is stateful: a retry would meet the first attempt's radar
  forbidOnly: !!process.env.CI,
  reporter: process.env.CI ? [["list"], ["html", { open: "never" }]] : "list",
  use: { baseURL: BASE_URL ?? `http://127.0.0.1:${WEB_PORT}`, actionTimeout: 15_000, trace: "retain-on-failure", screenshot: "only-on-failure" },
  projects: [{ name: "chromium", use: { ...devices["Desktop Chrome"] } }],
  webServer: BASE_URL
    ? []
    : [
        {
          command: `${PYTHON} e2e/stack.py`,
          url: `http://127.0.0.1:${API_PORT}/api/v1/health`,
          env: { CAROUSEL_E2E_API_PORT: API_PORT },
          reuseExistingServer: false,
          timeout: 120_000,
          stdout: "pipe",
        },
        {
          command: `npx next start --hostname 127.0.0.1 --port ${WEB_PORT}`,
          url: `http://127.0.0.1:${WEB_PORT}`,
          env: { CAROUSEL_API_URL: `http://127.0.0.1:${API_PORT}` },
          reuseExistingServer: false,
          timeout: 120_000,
        },
      ],
});
