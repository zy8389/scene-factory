"use strict";

// A local npm install is the default; preserve existing external installations.
let chromium;
try {
  ({ chromium } = require(process.env.SCENE_FACTORY_PLAYWRIGHT || "playwright"));
} catch (error) {
  throw new Error("Browser QA requires: npm ci && npx playwright install chromium. " +
    "SCENE_FACTORY_PLAYWRIGHT may point to an existing Playwright module.", { cause: error });
}

function launchBrowser() {
  const executablePath = process.env.SCENE_FACTORY_BROWSER || process.env.SCENE_FACTORY_CHROME;
  return chromium.launch({
    headless: true,
    ...(executablePath ? { executablePath } : {}),
    args: ["--use-angle=swiftshader", "--enable-unsafe-swiftshader", "--ignore-gpu-blocklist"],
  });
}

module.exports = { launchBrowser };
