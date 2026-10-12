"use strict";
const fs = require("node:fs");
const path = require("node:path");
const assert = require("node:assert/strict");
const { launchBrowser } = require("./qa_browser.cjs");
const base = process.env.SCENE_FACTORY_QA_URL || "http://127.0.0.1:8765/";
const output = path.resolve(process.env.SCENE_FACTORY_QA_OUTPUT || "outputs/kitchen_dining_20261012/browser");
fs.mkdirSync(output, { recursive: true });

async function main() {
  const browser = await launchBrowser();
  const page = await browser.newPage({ viewport: { width: 1500, height: 1150 } });
  const errors = [];
  page.on("pageerror", (error) => errors.push(error.message));
  const reports = [];
  try {
    await page.goto(base, { waitUntil: "domcontentloaded" });
    for (const [name, label, room] of [["kitchen", "完整厨房", "kitchen"], ["storage", "厨房收纳", "kitchen"], ["dining", "餐厅布置", "dining_room"]]) {
      await page.getByRole("button", { name: label, exact: true }).click();
      if (!(await page.locator("#export-usd").isChecked())) {
        await page.locator("label:has(#export-usd)").click();
      }
      const responsePromise = page.waitForResponse((r) => r.url().endsWith("/api/generate") && r.request().method() === "POST", { timeout: 180000 });
      await page.locator("#generate").click();
      const response = await responsePromise;
      const payload = await response.json();
      fs.writeFileSync(path.join(output, `${name}.json`), JSON.stringify(payload, null, 2));
      assert.equal(response.status(), 200, JSON.stringify(payload));
      const item = payload.items[0];
      assert.equal(item.validation.valid, true);
      assert.equal(item.scene.room_type, room);
      assert.ok(item.scene.clearance_zones.length > 0);
      assert.ok(item.files.usd);
      assert.ok(item.scene.objects.some((o) => o.relations.some((r) => r.kind === "inside")));
      if (name === "dining") {
        assert.equal(item.scene.objects.filter((o) => o.category === "chair").length, 4);
        assert.ok(item.scene.objects.filter((o) => o.category === "chair").every((o) => o.relations.some((r) => r.kind === "around")));
      }
      const modeled = item.scene.objects.filter((o) => item.assets[o.asset_id].visual_url).length;
      assert.equal(modeled, item.scene.objects.length);
      await page.waitForFunction(() => document.querySelector("#viewer-status").dataset.state === "ready", null, { timeout: 60000 });
      const viewer = await page.locator("#viewer-status").textContent();
      assert.ok(viewer.includes("0 个占位"), viewer);
      await page.locator("#result-card").screenshot({ path: path.join(output, `${name}.png`) });
      const download = await page.request.get(new URL(item.files.usd, base).href);
      assert.equal(download.status(), 200);
      await page.setViewportSize({ width: 390, height: 844 });
      await page.locator("#result-card").screenshot({ path: path.join(output, `${name}_mobile.png`) });
      assert.equal(await page.evaluate(() => document.documentElement.scrollWidth > innerWidth + 1), false);
      await page.setViewportSize({ width: 1500, height: 1150 });
      reports.push({ name, scene_id: item.scene.scene_id, parser: item.prompt_parser, objects: modeled,
        room: item.scene.room_type, viewer, validation: item.validation, files: item.files });
    }
    assert.deepEqual(errors, []);
    fs.writeFileSync(path.join(output, "validation.json"), JSON.stringify({ passed: true, errors, reports }, null, 2));
    console.log(JSON.stringify(reports.map(({ name, objects, parser }) => ({ name, objects, parser }))));
  } finally { await browser.close(); }
}
main().catch((error) => { console.error(error); process.exitCode = 1; });
