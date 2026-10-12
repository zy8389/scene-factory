"use strict";

const fs = require("node:fs");
const path = require("node:path");
const assert = require("node:assert/strict");
const { chromium } = require(process.env.SCENE_FACTORY_PLAYWRIGHT);

const base = process.env.SCENE_FACTORY_QA_URL || "http://127.0.0.1:8765/";
const output = path.resolve(process.env.SCENE_FACTORY_QA_OUTPUT || "outputs/current-scene-runtime-qa");
const mode = process.env.SCENE_FACTORY_CURRENT_SCENE_QA_MODE || "check";
fs.mkdirSync(output, { recursive: true });

async function main() {
  const browser = await chromium.launch({ headless: true, args: ["--use-angle=swiftshader", "--enable-unsafe-swiftshader"] });
  const page = await browser.newPage({ viewport: { width: 1440, height: 1000 } });
  const errors = [];
  page.on("pageerror", (error) => errors.push(error.message));
  const result = { mode, page_errors: errors };
  try {
    await page.goto(base, { waitUntil: "domcontentloaded" });
    await page.waitForSelector("#runtime-config-status.ready", { timeout: 30000 });
    if (mode === "inspect") {
      const jobId = process.env.SCENE_FACTORY_RUNTIME_QA_JOB_ID;
      assert.ok(jobId, "inspect requires SCENE_FACTORY_RUNTIME_QA_JOB_ID");
      const response = await page.request.get(new URL(`/api/runtime/jobs/${encodeURIComponent(jobId)}`, base).href);
      assert.equal(response.status(), 200);
      const job = await response.json();
      assert.equal(job.kind, "scene");
      assert.ok(job.source_scene_id);
      assert.equal(job.status, "completed");
      assert.equal(job.validation.valid, true);
      assert.equal(job.cases.length, 2);
      await page.waitForFunction((id) => [...document.querySelector("#runtime-job-select").options].some((option) => option.value === id), jobId);
      await page.selectOption("#runtime-job-select", jobId);
      await page.waitForFunction((id) => document.querySelector("#runtime-job-source").textContent.includes(id), job.source_scene_id);
      const replay = job.cases.find((item) => item.replay_available);
      assert.ok(replay, "validated case must have recorded-state replay");
      await page.click(`[data-runtime-case="${replay.case_id}"]`);
      await page.waitForFunction(() => !document.querySelector("#runtime-replay-play").disabled, { timeout: 30000 });
      await page.locator("#runtime-replay-seek").evaluate((element) => {
        element.value = element.max;
        element.dispatchEvent(new Event("input", { bubbles: true }));
      });
      result.job = job;
      result.replay = { case_id: replay.case_id, frame_count: Number(await page.locator("#runtime-replay-seek").getAttribute("max")) + 1,
        final_phase: await page.locator("#runtime-replay-phase").textContent() };
      await page.locator("#runtime-section").screenshot({ path: path.join(output, "scene_job.png") });
    } else {
      const prompt = process.env.SCENE_FACTORY_QA_PROMPT;
      assert.ok(prompt, "check and submit require an explicit SCENE_FACTORY_QA_PROMPT");
      await page.fill("#prompt", prompt);
      await page.fill("#seed", process.env.SCENE_FACTORY_QA_SEED || "42");
      await page.fill("#count", "1");
      // Eligibility is triggered by renderScene immediately after generation, so start both listeners before clicking.
      const generation = page.waitForResponse((response) => response.url().endsWith("/api/generate") && response.request().method() === "POST");
      const checking = page.waitForResponse((response) => response.url().endsWith("/api/runtime/scene-check") && response.request().method() === "POST");
      await page.click("#generate");
      const generated = await generation;
      assert.equal(generated.status(), 200);
      result.generation = await generated.json();
      const checked = await checking;
      assert.equal(checked.status(), 200);
      result.check = await checked.json();
      const sceneId = result.generation.items[0].scene.scene_id;
      assert.equal(result.check.scene_id, sceneId);
      await page.waitForFunction(() => !document.querySelector("#runtime-scene-recheck").disabled);
      assert.equal(await page.locator("#runtime-scene-start").isDisabled(), !result.check.eligible);
      result.check_plan_text = await page.locator("#runtime-scene-plan").textContent();
      await page.locator("#runtime-scene-panel").screenshot({ path: path.join(output, "scene_check.png") });
      if (mode === "submit") {
        assert.equal(result.check.eligible, true);
        assert.equal(result.check.task_count, 2);
        const posted = page.waitForResponse((response) => response.url().endsWith("/api/runtime/jobs") && response.request().method() === "POST");
        await page.click("#runtime-scene-start");
        const queued = await posted;
        assert.equal(queued.status(), 202);
        result.job = await queued.json();
        assert.equal(result.job.status, "queued");
        assert.equal(result.job.source_scene_id, sceneId);
        assert.deepEqual(JSON.parse(queued.request().postData()), { scene_id: sceneId, profile: "tracking" });
        await page.waitForFunction((id) => document.querySelector("#runtime-job-select").value === id, result.job.job_id);
        await page.locator("#runtime-section").screenshot({ path: path.join(output, "scene_submission.png") });
      } else assert.equal(mode, "check", "supported modes are check, submit and inspect");
    }
    await page.setViewportSize({ width: 390, height: 844 });
    const mobileTarget = mode === "inspect" ? "#runtime-section" : "#runtime-scene-panel";
    await page.locator(mobileTarget).screenshot({ path: path.join(output, "mobile.png") });
    result.mobile_overflow = await page.evaluate(() => document.documentElement.scrollWidth > innerWidth + 1);
    assert.equal(result.mobile_overflow, false);
    assert.deepEqual(errors, []);
    fs.writeFileSync(path.join(output, `browser_${mode}.json`), JSON.stringify(result, null, 2));
    console.log(JSON.stringify({ mode, scene_id: result.check?.scene_id || result.job?.source_scene_id,
      eligible: result.check?.eligible, job_id: result.job?.job_id, page_errors: errors, mobile_overflow: result.mobile_overflow }));
  } finally { await browser.close(); }
}

main().catch((error) => { console.error(error); process.exitCode = 1; });
