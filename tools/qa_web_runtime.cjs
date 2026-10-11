"use strict";

const fs = require("node:fs");
const path = require("node:path");
const assert = require("node:assert/strict");
const { chromium } = require(process.env.SCENE_FACTORY_PLAYWRIGHT);

const base = process.env.SCENE_FACTORY_QA_URL || "http://127.0.0.1:8765/";
const output = path.resolve(process.env.SCENE_FACTORY_QA_OUTPUT || "outputs/web-runtime-qa");
const mode = process.env.SCENE_FACTORY_RUNTIME_QA_MODE || "inspect";
fs.mkdirSync(output, { recursive: true });

async function main() {
  const browser = await chromium.launch({
    headless: true,
    args: ["--use-angle=swiftshader", "--enable-unsafe-swiftshader"],
  });
  const page = await browser.newPage({ viewport: { width: 1440, height: 1000 } });
  const errors = [];
  page.on("pageerror", (error) => errors.push(error.message));
  const results = { mode, page_errors: errors };
  try {
    await page.goto(base, { waitUntil: "domcontentloaded" });
    await page.waitForSelector("#runtime-config-status.ready", { timeout: 30000 });
    if (mode === "submit") {
      await page.selectOption("#runtime-profile", "tracking");
      await page.fill("#runtime-limit", "1");
      const response = page.waitForResponse((item) =>
        item.url().endsWith("/api/runtime/jobs") && item.request().method() === "POST");
      await page.click("#runtime-start");
      const accepted = await response;
      assert.equal(accepted.status(), 202);
      results.job = await accepted.json();
      assert.equal(results.job.status, "queued");
      assert.equal(results.job.limit_layouts, 1);
      await page.waitForFunction((id) => document.querySelector("#runtime-job-select").value === id, results.job.job_id);
      await page.locator("#runtime-section").screenshot({ path: path.join(output, "submission.png") });
      fs.writeFileSync(path.join(output, "web_submission.json"), JSON.stringify(results, null, 2));
    } else {
      const response = await page.request.get(new URL("/api/runtime/jobs", base).href);
      const jobs = (await response.json()).jobs;
      const tracking = jobs.find((job) => job.history && job.profile === "tracking");
      const baseline = jobs.find((job) => job.history && job.profile === "baseline");
      assert.equal(tracking?.validation?.valid, true);
      assert.equal(baseline?.validation?.valid, true);
      assert.equal(tracking.summary.task_status_counts.passed, 40);
      assert.equal(baseline.summary.task_status_counts.passed, 4);
      assert.equal(baseline.summary.task_status_counts.failed, 36);
      results.history = { tracking: tracking.job_id, baseline: baseline.job_id };
      await page.selectOption("#runtime-job-select", tracking.job_id);
      await page.waitForFunction(() => document.querySelectorAll("#runtime-cases button:not([disabled])").length === 40);
      const success = tracking.cases.find((item) => item.event === "robot_mug_pick_place");
      await page.click(`[data-runtime-case="${success.case_id}"]`);
      await page.waitForFunction(() => !document.querySelector("#runtime-replay-play").disabled, { timeout: 30000 });
      const frameCount = Number(await page.locator("#runtime-replay-seek").getAttribute("max")) + 1;
      assert.ok(frameCount > 300);
      const details0 = await page.locator("#runtime-replay-step").textContent();
      await page.click("#runtime-replay-play");
      await page.waitForFunction(() => Number(document.querySelector("#runtime-replay-seek").value) > 10);
      await page.click("#runtime-replay-play");
      await page.locator("#runtime-replay-seek").evaluate((element) => {
        element.value = element.max;
        element.dispatchEvent(new Event("input", { bubbles: true }));
      });
      const detailsEnd = await page.locator("#runtime-replay-step").textContent();
      assert.notEqual(details0, detailsEnd);
      results.success_replay = { case_id: success.case_id, frame_count: frameCount, final_step: detailsEnd };
      await page.locator("#runtime-replay-panel").screenshot({ path: path.join(output, "success_replay.png") });
      await page.selectOption("#runtime-job-select", baseline.job_id);
      await page.waitForFunction(() => document.querySelector("#runtime-job-title").textContent.includes("mug_batch"));
      const failed = baseline.cases.find((item) => item.task_status === "failed");
      await page.click(`[data-runtime-case="${failed.case_id}"]`);
      await page.waitForFunction(() => !document.querySelector("#runtime-replay-play").disabled, { timeout: 30000 });
      await page.locator("#runtime-replay-seek").evaluate((element) => {
        element.value = element.max;
        element.dispatchEvent(new Event("input", { bubbles: true }));
      });
      assert.match(await page.locator("#runtime-replay-phase").textContent(), /失败/);
      results.failure_replay = { case_id: failed.case_id, reason: failed.failure_reasons };
      await page.locator("#runtime-replay-panel").screenshot({ path: path.join(output, "failure_replay.png") });
      await page.locator("#runtime-section").screenshot({ path: path.join(output, "desktop.png") });
      await page.setViewportSize({ width: 390, height: 844 });
      await page.locator("#runtime-section").screenshot({ path: path.join(output, "mobile.png") });
      results.mobile_overflow = await page.evaluate(() => document.documentElement.scrollWidth > innerWidth + 1);
      assert.equal(results.mobile_overflow, false);
      await page.reload({ waitUntil: "domcontentloaded" });
      await page.waitForFunction((id) => document.querySelector("#runtime-job-select").value === id, baseline.job_id);
      results.selection_restored = true;
      const newJobId = process.env.SCENE_FACTORY_RUNTIME_QA_JOB_ID;
      if (newJobId) {
        await page.setViewportSize({ width: 1440, height: 1000 });
        const jobResponse = await page.request.get(new URL(`/api/runtime/jobs/${newJobId}`, base).href);
        const job = await jobResponse.json();
        assert.equal(job.status, "completed");
        assert.equal(job.validation.valid, true);
        assert.equal(job.summary.task_status_counts.passed, 2);
        await page.selectOption("#runtime-job-select", newJobId);
        await page.waitForFunction(() => document.querySelectorAll("#runtime-cases button:not([disabled])").length === 2);
        await page.click(`[data-runtime-case="${job.cases[0].case_id}"]`);
        await page.waitForFunction(() => !document.querySelector("#runtime-replay-play").disabled, { timeout: 30000 });
        results.new_web_job = { job_id: newJobId, task_passed: 2, physics_passed: job.summary.physics_status_counts.passed,
          replay_frame_count: Number(await page.locator("#runtime-replay-seek").getAttribute("max")) + 1 };
        await page.locator("#runtime-section").screenshot({ path: path.join(output, "new_web_job.png") });
      }
      fs.writeFileSync(path.join(output, "browser_validation.json"), JSON.stringify(results, null, 2));
    }
    assert.deepEqual(errors, []);
    console.log(JSON.stringify(results));
  } finally {
    await browser.close();
  }
}

main().catch((error) => { console.error(error); process.exitCode = 1; });
