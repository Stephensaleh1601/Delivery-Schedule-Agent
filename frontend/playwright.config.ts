import { defineConfig, devices } from "@playwright/test";

const python = process.platform === "win32" ? "..\\.venv\\Scripts\\python.exe" : "python";

export default defineConfig({
  testDir: "./e2e",
  timeout: 90_000,
  fullyParallel: false,
  retries: process.env.CI ? 1 : 0,
  reporter: process.env.CI ? "github" : "list",
  use: {
    baseURL: "http://127.0.0.1:3765",
    screenshot: "only-on-failure",
    trace: "retain-on-failure",
  },
  projects: [
    {
      name: "chromium",
      use: { ...devices["Desktop Chrome"], viewport: { width: 1360, height: 674 } },
    },
  ],
  webServer: [
    {
      command: `${python} ../scripts/start_e2e.py`,
      url: "http://127.0.0.1:8765/api/bootstrap",
      reuseExistingServer: false,
      timeout: 120_000,
    },
    {
      command: "npm run start:e2e",
      url: "http://127.0.0.1:3765/chat",
      env: { BACKEND_ORIGIN: "http://127.0.0.1:8765" },
      reuseExistingServer: false,
      timeout: 120_000,
    },
  ],
});
