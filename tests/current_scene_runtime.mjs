import assert from "node:assert/strict";
import fs from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";

const root = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..");
const source = fs.readFileSync(path.join(root, "web/runtime-panel.js"), "utf8")
  .replace('import { RuntimeReplay } from "/runtime-replay.js";', "const RuntimeReplay = globalThis.__testRuntimeReplay;");
const { initializeRuntimePanel } = await import(`data:text/javascript;base64,${Buffer.from(source).toString("base64")}`);

class Element {
  constructor() { this.listeners = new Map(); this.children = []; this.value = ""; this.max = "20"; this.hidden = false; }
  addEventListener(name, handler) { this.listeners.set(name, handler); }
  emit(name, event = {}) { return this.listeners.get(name)?.(event); }
  replaceChildren(...children) { this.children = children; this.innerHTML = ""; this.textContent = ""; }
  querySelector() { return this.child ||= new Element(); }
  setAttribute() {}
  removeAttribute() {}
  scrollIntoView() { this.scrollCount = (this.scrollCount || 0) + 1; }
}
const ids = ["section", "form", "profile", "limit", "start", "config-status", "task-count", "message", "job-select", "refresh",
  "connection", "empty", "job-detail", "job-title", "job-status", "progress", "progress-text", "summary", "job-error", "cases",
  "report-link", "replay-panel", "replay-title", "replay-stage", "replay-status", "replay-play", "replay-seek", "replay-speed",
  "replay-frame", "replay-phase", "replay-contact", "replay-step", "replay-joints", "replay-notes", "job-source", "scene-panel",
  "scene-status", "scene-reasons", "scene-plan", "scene-start", "scene-recheck", "scene-message"];
const response = (payload) => ({ ok: true, json: async () => payload });
const flush = () => new Promise((resolve) => setImmediate(resolve));
const deferred = () => { let resolve; const promise = new Promise((done) => { resolve = done; }); return { promise, resolve }; };

function makePanel(available = true) {
  const elements = new Map(ids.map((id) => [id, new Element()]));
  elements.get("profile").value = "tracking";
  elements.get("limit").value = "1";
  const checks = new Map();
  const posts = [];
  const accepted = deferred();
  const doc = { querySelector: (id) => elements.get(id.replace("#runtime-", "")), createElement: () => new Element() };
  const panel = initializeRuntimePanel({ root: doc, fetch: async (url, init = {}) => {
    if (url === "/api/runtime/config") return response({ available, profiles: [{ id: "tracking", label: "优化方案" }], max_layouts: 20 });
    if (url === "/api/runtime/scene-check") {
      const body = JSON.parse(init.body);
      assert.deepEqual(Object.keys(body).sort(), ["profile", "scene_id"]);
      assert.equal(body.profile, "tracking");
      const check = checks.get(body.scene_id);
      if (!check) throw new Error(`unexpected scene check: ${body.scene_id}`);
      return check.promise;
    }
    if (url === "/api/runtime/jobs" && init.method === "POST") {
      posts.push(JSON.parse(init.body));
      return accepted.promise;
    }
    if (url === "/api/runtime/jobs") return response({ jobs: [] });
    throw new Error(`unexpected URL: ${url}`);
  } });
  return { panel, elements, checks, posts, accepted };
}

globalThis.localStorage = { getItem: () => "", setItem() {} };
const { panel, elements, checks, posts, accepted } = makePanel();
try {
  await flush();
  const old = deferred();
  const unsupported = deferred();
  checks.set("old-scene", old);
  checks.set("unsupported-scene", unsupported);
  const oldCheck = panel.setScene("old-scene");
  assert.equal(elements.get("scene-start").disabled, true);
  const currentCheck = panel.setScene("unsupported-scene");
  unsupported.resolve(response({ scene_id: "unsupported-scene", eligible: false, task_count: 0, reasons: ["需要厨房单杯场景 <script>"] }));
  await currentCheck;
  assert.equal(elements.get("scene-status").textContent, "暂不支持");
  assert.match(elements.get("scene-reasons").innerHTML, /&lt;script&gt;/);
  // Even if the fetch ignores abort, an earlier supported scene cannot enable this unsupported scene.
  old.resolve(response({ scene_id: "old-scene", eligible: true, task_count: 2, reasons: [] }));
  await oldCheck;
  assert.equal(elements.get("scene-start").disabled, true);
  assert.equal(elements.get("scene-status").textContent, "暂不支持");
  await elements.get("scene-start").emit("click");
  assert.equal(posts.length, 0);

  const eligible = deferred();
  checks.set("eligible-scene", eligible);
  const eligibleCheck = panel.setScene("eligible-scene");
  eligible.resolve(response({ scene_id: "eligible-scene", eligible: true, task_count: 2, reasons: [],
    robot_base_position_m: [1, 2, 0.9], target_position_m: [1.4, 2.1, 0.97], plan: { added_objects: [{ object_id: "robot_mount" }] } }));
  await eligibleCheck;
  assert.equal(elements.get("scene-start").disabled, false);
  assert.equal(elements.get("scene-plan").hidden, false);
  assert.match(elements.get("scene-plan").textContent, /增加 1 个固定支撑物/);
  assert.match(elements.get("scene-plan").textContent, /1\.000, 2\.000, 0\.900 m/);
  const submit = elements.get("scene-start").emit("click");
  await elements.get("scene-start").emit("click");
  assert.deepEqual(posts, [{ scene_id: "eligible-scene", profile: "tracking" }]);

  // The source id is captured before submission. Switching scenes never changes the submitted layout.
  const newUnsupported = deferred();
  checks.set("new-scene", newUnsupported);
  const newCheck = panel.setScene("new-scene");
  newUnsupported.resolve(response({ scene_id: "new-scene", eligible: false, task_count: 0, reasons: ["场景中有多个杯子"] }));
  await newCheck;
  accepted.resolve(response({ job_id: "scene-job", kind: "scene", source_scene_id: "eligible-scene", label: "当前场景验收",
    status: "queued", progress: { total: 2, completed: 0 }, cases: [] }));
  await submit;
  assert.equal(elements.get("job-select").value, "scene-job");
  assert.match(elements.get("job-source").textContent, /eligible-scene/);
  assert.equal(elements.get("job-source").hidden, false);
  assert.equal(elements.get("section").scrollCount, 1);
  assert.equal(elements.get("scene-start").disabled, true);
  assert.match(elements.get("scene-reasons").innerHTML, /多个杯子/);
  assert.equal(elements.get("scene-message").textContent, "");

  const mismatch = deferred();
  checks.set("mismatch", mismatch);
  const mismatchCheck = panel.setScene("mismatch");
  mismatch.resolve(response({ scene_id: "different-id", eligible: true, task_count: 2, reasons: [] }));
  await mismatchCheck;
  assert.equal(elements.get("scene-start").disabled, true);
  assert.match(elements.get("scene-message").textContent, /不一致/);
  await panel.setScene(null);
  assert.equal(elements.get("scene-panel").hidden, true);
} finally { panel.dispose(); }

const unavailable = makePanel(false);
try {
  await flush();
  const check = deferred();
  unavailable.checks.set("scene-without-isaac", check);
  const checking = unavailable.panel.setScene("scene-without-isaac");
  check.resolve(response({ scene_id: "scene-without-isaac", eligible: true, task_count: 2, reasons: [] }));
  await checking;
  assert.equal(unavailable.elements.get("scene-status").textContent, "环境未就绪");
  assert.equal(unavailable.elements.get("scene-start").disabled, true);
  await unavailable.elements.get("scene-start").emit("click");
  assert.equal(unavailable.posts.length, 0);
} finally { unavailable.panel.dispose(); delete globalThis.localStorage; }

console.log("current scene runtime: eligibility races, source identity, submission snapshots and unavailable simulator passed");
