import { defineConfig } from "@playwright/test";

// Runs against the docker-compose stack: `docker compose up -d`, then `npm run test:e2e -w apps/admin`.
export default defineConfig({
  testDir: "e2e",
  timeout: 90_000,
  // one shared stack (one schedule draft per plant, shared line machines): run spec files one after another
  workers: 1,
  use: {
    baseURL: process.env.MES_ADMIN_URL ?? "http://localhost:3080",
    viewport: { width: 1600, height: 950 },
    screenshot: "only-on-failure",
    trace: "retain-on-failure",
  },
});
