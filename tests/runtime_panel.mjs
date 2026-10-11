import assert from "node:assert/strict";
import fs from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";

const root = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..");
const source = fs.readFileSync(path.join(root, "web/runtime-panel.js"), "utf8")
  .replace('import { RuntimeReplay } from "/runtime-replay.js";', "const RuntimeReplay = globalThis.__testRuntimeReplay;");
const { describeRuntimeJob, describeReplayFrame, initializeRuntimePanel } = await import(
  `data:text/javascript;base64,${Buffer.from(source).toString("base64")}`,
);

// Finishing an execution and validating its evidence does not turn a failed task into a success.
const baseline = describeRuntimeJob({
  status: "completed", limit_layouts: 20, progress: { completed: 40, total: 40 },
  summary: { case_count: 40, physics_status_counts: { passed: 40 }, task_status_counts: { passed: 4, failed: 36 }, evidence_complete_count: 40 },
  validation: { valid: true, errors: [] },
});
assert.equal(baseline.status, "运行完成");
assert.equal(baseline.tasks.pass, 4);
assert.equal(baseline.physics.pass, 40);
assert.equal(baseline.tasks.fail, 36);
assert.equal(baseline.validation, "有效");
assert.equal(baseline.evidenceCount, 40);
assert.equal(baseline.unverified, 0);

// Partial jobs keep planned tasks in the denominator and missing outcomes remain unverified.
const partial = describeRuntimeJob({
  status: "running", limit_layouts: 3, progress: { completed: 2, total: 6 },
  cases: [{ physics_status: "PASS", task_status: "PASS", evidence_complete: true },
    { physics_status: "PASS", task_status: "FAIL", evidence_complete: true }],
});
assert.equal(partial.total, 6);
assert.equal(partial.completed, 2);
assert.equal(partial.tasks.pass, 1);
assert.equal(partial.tasks.fail, 1);
assert.equal(partial.unverified, 4);
assert.equal(partial.validation, "等待校验");
assert.equal(describeRuntimeJob({ ...partial, validation: { valid: false } }).validation, "校验未通过");

// Contact availability and the different arm/finger units must survive presentation.
const frame = describeReplayFrame({
  step: 220, timestamp: 220 / 60, phase: "LIFT",
  contacts: { left: true, right: true, available: true },
  robot: { joint_positions: [0, 1, 2, 3, 4, 5, 6, 0.013, 0.012], finger_positions: { left: [1, 2, 3], right: [1, 2.1, 3] } },
});
assert.equal(frame.contact, "双指接触");
assert.equal(frame.phase, "抬起杯子");
assert.equal(frame.time, "3.67 s");
assert.equal(frame.joints.length, 7);
assert.deepEqual(frame.fingers, [0.013, 0.012]);
assert.equal(describeReplayFrame({ contacts: { available: false, left: true, right: true } }).contact, "未记录");
assert.equal(describeReplayFrame({}).contact, "未记录");
assert.equal(describeReplayFrame({ phase: "TRANSFER" }).phase, "移动到目标");
assert.equal(describeReplayFrame({ phase: "LOWER" }).phase, "放下杯子");

class Element {
  constructor() { this.listeners = new Map(); this.attributes = new Map(); this.children = []; this.value = ""; this.max = "20"; }
  addEventListener(name, fn) { this.listeners.set(name, fn); }
  emit(name, event = {}) { return this.listeners.get(name)?.(event); }
  replaceChildren(...children) { this.children = children; }
  querySelector() { return this.child ||= new Element(); }
  setAttribute(name, value) { this.attributes.set(name, value); }
  removeAttribute(name) { this.attributes.delete(name); }
}
const ids = ["section", "form", "profile", "limit", "start", "config-status", "task-count", "message", "job-select", "refresh",
  "connection", "empty", "job-detail", "job-title", "job-status", "progress", "progress-text", "summary", "job-error", "cases",
  "report-link", "replay-panel", "replay-title", "replay-stage", "replay-status", "replay-play", "replay-seek", "replay-speed",
  "replay-frame", "replay-phase", "replay-contact", "replay-step", "replay-joints", "replay-notes"];
const elements = new Map(ids.map((id) => [`#runtime-${id}`, new Element()]));
elements.get("#runtime-profile").value = "tracking";
elements.get("#runtime-limit").value = "1";
const documentStub = { querySelector: (id) => elements.get(id), createElement: () => new Element() };
globalThis.localStorage = { getItem: () => "A", setItem() {} };
const records = [
  { job_id: "A", label: "旧任务", status: "completed", created_at: "2026-10-11T00:00:00Z", cases: [] },
  { job_id: "B", label: "新任务", status: "completed", created_at: "2026-10-11T00:01:00Z", cases: [
    { case_id: "B-lift", event: "robot_mug_lift", seed: 1004, physics_status: "passed", task_status: "failed",
      evidence_complete: true, replay_available: true, failure_reasons: ["task_physical_grasp_observed_failed", "grasp_failure"] },
  ] },
];
let resolveOld;
let requestedOld = false;
const oldRequest = new Promise((resolve) => { resolveOld = resolve; });
const response = (payload) => ({ ok: true, json: async () => payload });
const fetchStub = async (url) => {
  if (url === "/api/runtime/config") return response({ available: true, profiles: [{ id: "tracking", label: "优化方案" }], max_layouts: 20 });
  if (url === "/api/runtime/jobs") return response({ jobs: records });
  if (url.endsWith("/A")) { requestedOld = true; return oldRequest; }
  if (url.endsWith("/B")) return response(records[1]);
  throw new Error(`unexpected request: ${url}`);
};
const flush = () => new Promise((resolve) => setImmediate(resolve));
const panel = initializeRuntimePanel({ root: documentStub, fetch: fetchStub });
try {
  await flush();
  assert.equal(requestedOld, true);
  // The earlier fetch deliberately ignores abort. Its eventual response must not replace the new selection.
  elements.get("#runtime-job-select").value = "B";
  await elements.get("#runtime-job-select").emit("change");
  assert.equal(elements.get("#runtime-job-title").textContent, "新任务");
  assert.match(elements.get("#runtime-cases").innerHTML, /抓起杯子/);
  assert.match(elements.get("#runtime-cases").innerHTML, /物理 通过/);
  assert.match(elements.get("#runtime-cases").innerHTML, /任务 失败/);
  assert.match(elements.get("#runtime-cases").innerHTML, /未能稳定夹住杯子/);
  assert.match(elements.get("#runtime-cases").innerHTML, /data-runtime-case="B-lift" >回放/);
  resolveOld(response(records[0]));
  await flush();
  assert.equal(elements.get("#runtime-job-title").textContent, "新任务");
  assert.equal(elements.get("#runtime-job-select").value, "B");
} finally { panel.dispose(); delete globalThis.localStorage; }
console.log("runtime panel: result semantics, contact units, and selection race passed");
