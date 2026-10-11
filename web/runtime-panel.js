import { RuntimeReplay } from "/runtime-replay.js";

const activeStatuses = new Set(["queued", "preparing", "running", "validating"]);
const statusNames = {
  queued: "已排队", preparing: "准备场景", running: "仿真运行中", validating: "校验记录中",
  completed: "运行完成", failed: "运行失败", interrupted: "运行已中断",
};
const phaseNames = {
  PRE_GRASP: "移动到杯子上方", APPROACH: "接近杯子", GRASP: "闭合夹爪",
  VERIFY_GRASP: "确认稳定抓持", LIFT: "抬起杯子", TRANSFER: "移动到目标", TRANSPORT: "移动到目标",
  LOWER: "放下杯子", PLACE: "放下杯子", RELEASE: "松开夹爪", VERIFY_PLACE: "确认放置结果",
  RETREAT: "移开夹爪", DONE: "任务结束", FAILED: "任务失败", RESET: "初始化",
};
const failureNames = {
  pre_grasp_timeout: "未能及时到达抓取准备位置", approach_timeout: "接近杯子超时",
  grasp_failure: "未能稳定夹住杯子", lift_failure: "杯子未达到要求的抬升高度",
  task_timeout: "任务未在限定步数内完成", timeout: "运行超时", place_failure: "杯子未放到目标位置",
  object_dropped: "杯子在移动过程中掉落", unstable: "物体未稳定下来",
  physics_unstable: "物体未稳定下来", evidence_incomplete: "运行记录不完整",
  object_lost: "杯子在移动过程中掉落", transfer_failure: "未能将杯子移动到目标上方",
  release_failure: "夹爪未正常松开", ik_failure: "机械臂未能求得可执行姿态",
  grasp_diagnostics_unavailable: "缺少抓持接触记录", task_not_satisfied: "任务成功条件未满足",
  task_physical_grasp_observed_failed: "未观察到有效的实际抓持",
  task_acceptance_passed_failed: "任务终点未达到验收要求",
  task_runtime_no_failure_failed: "控制器报告执行失败",
};
const escapeHtml = (value) => String(value ?? "").replaceAll("&", "&amp;").replaceAll("<", "&lt;")
  .replaceAll(">", "&gt;").replaceAll('"', "&quot;").replaceAll("'", "&#039;");
const positiveCount = (value) => Number.isFinite(Number(value)) ? Math.max(0, Math.floor(Number(value))) : 0;
const verdict = (value) => String(value || "").toUpperCase();

function countVerdicts(counts, cases, field) {
  const result = { pass: 0, fail: 0 };
  const values = counts && Object.keys(counts).length ? Object.entries(counts)
    : cases.map((item) => [item[field], 1]);
  for (const [status, count] of values) {
    if (["PASS", "PASSED"].includes(verdict(status))) result.pass += positiveCount(count);
    if (["FAIL", "FAILED", "ERROR"].includes(verdict(status))) result.fail += positiveCount(count);
  }
  return result;
}

// A completed worker can contain failed tasks or invalid evidence. Keep those outcomes separate.
export function describeRuntimeJob(job) {
  const cases = Array.isArray(job?.cases) ? job.cases : [];
  const summary = job?.summary || {};
  const total = Math.max(positiveCount(job?.progress?.total), positiveCount(summary.case_count), cases.length,
    positiveCount(job?.limit_layouts) * 2);
  const physics = countVerdicts(summary.physics_status_counts, cases, "physics_status");
  const tasks = countVerdicts(summary.task_status_counts, cases, "task_status");
  const evidenceCount = summary.evidence_complete_count == null
    ? cases.filter((item) => item.evidence_complete === true).length : positiveCount(summary.evidence_complete_count);
  return {
    status: statusNames[job?.status] || "状态未知", active: activeStatuses.has(job?.status), total,
    completed: Math.min(total, positiveCount(job?.progress?.completed)), physics, tasks,
    unverified: Math.max(0, total - tasks.pass - tasks.fail), evidenceCount,
    validation: job?.validation?.valid === true ? "有效" : job?.validation?.valid === false ? "校验未通过" : "等待校验",
  };
}

export function describeReplayFrame(frame = {}) {
  const contacts = frame.contacts || {};
  const contact = contacts.available === false ? "未记录"
    : contacts.left && contacts.right ? "双指接触" : contacts.left ? "仅左指接触"
      : contacts.right ? "仅右指接触" : "无双指接触";
  const phase = String(frame.phase || "");
  return {
    phase: phaseNames[phase.toUpperCase()] || phase || "未记录",
    contact: !frame.contacts ? "未记录" : contact,
    step: Number.isFinite(frame.step) ? String(frame.step) : "—",
    time: Number.isFinite(frame.timestamp) ? `${frame.timestamp.toFixed(2)} s` : "—",
    joints: Array.isArray(frame.robot?.joint_positions) ? frame.robot.joint_positions.slice(0, 7) : [],
    fingers: Array.isArray(frame.robot?.joint_positions) ? frame.robot.joint_positions.slice(7, 9) : [],
  };
}

function friendlyReason(value) {
  const reason = typeof value === "string" ? value : value?.reason || value?.code || value?.message || "";
  return failureNames[reason] || reason || "未提供原因";
}

function caseVerdict(value, physics = false) {
  const status = verdict(value);
  if (["PASS", "PASSED"].includes(status)) return { label: physics ? "通过" : "成功", tone: "pass" };
  if (["FAIL", "FAILED", "ERROR"].includes(status)) return { label: physics ? "未通过" : "失败", tone: "fail" };
  return { label: "未验证", tone: "pending" };
}

function jobLabel(job) {
  const label = job.label || (job.profile === "baseline" ? "原始控制方案" : "优化后的控制方案");
  const date = new Date(job.created_at);
  const created = Number.isNaN(date.getTime()) ? "" : date.toLocaleString("zh-CN", {
    month: "2-digit", day: "2-digit", hour: "2-digit", minute: "2-digit", hour12: false,
  });
  return `${label}${created ? ` · ${created}` : ""} · ${statusNames[job.status] || "状态未知"}`;
}

function taskEventName(event) {
  return ["lift", "robot_mug_lift"].includes(event) ? "抓起杯子"
    : ["place", "placement", "pick_place", "robot_mug_pick_place"].includes(event) ? "放置杯子" : "机器人任务";
}

function safeReportUrl(value) {
  if (typeof value !== "string" || !value.startsWith("/api/runtime/jobs/") || value.startsWith("//")) return null;
  return value;
}

export function initializeRuntimePanel(options = {}) {
  const root = options.root || document;
  if (!root.querySelector("#runtime-section")) return null;
  const byId = (name) => root.querySelector(`#runtime-${name}`);
  const ui = {
    form: byId("form"), profile: byId("profile"), limit: byId("limit"), start: byId("start"),
    config: byId("config-status"), count: byId("task-count"), message: byId("message"),
    select: byId("job-select"), refresh: byId("refresh"), connection: byId("connection"), empty: byId("empty"),
    detail: byId("job-detail"), title: byId("job-title"), status: byId("job-status"),
    progress: byId("progress"), progressText: byId("progress-text"), summary: byId("summary"),
    error: byId("job-error"), cases: byId("cases"), report: byId("report-link"),
    replayPanel: byId("replay-panel"), replayTitle: byId("replay-title"), replayContainer: byId("replay-stage"),
    replayStatus: byId("replay-status"), play: byId("replay-play"), seek: byId("replay-seek"),
    speed: byId("replay-speed"), frame: byId("replay-frame"), phase: byId("replay-phase"),
    contact: byId("replay-contact"), step: byId("replay-step"), joints: byId("replay-joints"), notes: byId("replay-notes"),
  };
  const requestFetch = options.fetch || globalThis.fetch.bind(globalThis);
  const storageKey = "sceneFactory.runtime.selectedJob";
  let selectedId = "";
  try { selectedId = localStorage.getItem(storageKey) || ""; } catch { /* Storage is optional. */ }
  let config = null;
  let jobs = [];
  let currentJob = null;
  let selectedRevision = 0;
  let selectedRequest = null;
  let listRevision = 0;
  let disposed = false;
  let refreshing = false;
  let submitting = false;
  let timer = null;
  let replayRevision = 0;
  let replayRequest = null;
  let replay = null;
  let replayPayload = null;
  let selectedCaseId = "";
  let frameIndex = 0;
  let playing = false;
  let animation = null;
  let lastFrameTime = 0;
  let frameRemainder = 0;

  async function api(url, init = {}, controller = new AbortController()) {
    const timeout = setTimeout(() => controller.abort(), 12000);
    try {
      const response = await requestFetch(url, { ...init, signal: controller.signal });
      const payload = await response.json();
      if (!response.ok) throw new Error(payload.error || "请求未完成，请稍后重试");
      return payload;
    } finally { clearTimeout(timeout); }
  }

  function setMessage(text, error = false) {
    ui.message.textContent = text;
    ui.message.className = `runtime-message${error ? " error" : ""}`;
  }

  function updateStartButton() {
    ui.start.disabled = !config?.available || submitting;
    ui.start.querySelector("span").textContent = submitting ? "正在提交…" : "开始仿真验收";
  }

  async function loadConfig() {
    const payload = await api("/api/runtime/config");
    if (disposed) return;
    config = payload;
    ui.config.textContent = payload.message || (payload.available ? "仿真环境就绪" : "仿真环境暂不可用，仍可查看历史记录");
    ui.config.className = `runtime-config-status ${payload.available ? "ready" : "unavailable"}`;
    const previous = ui.profile.value;
    if (Array.isArray(payload.profiles) && payload.profiles.length) {
      ui.profile.replaceChildren(...payload.profiles.map((profile) => {
        const option = root.createElement ? root.createElement("option") : document.createElement("option");
        option.value = profile.id;
        option.textContent = profile.label;
        return option;
      }));
      if (payload.profiles.some((profile) => profile.id === previous)) ui.profile.value = previous;
    }
    ui.limit.max = String(Math.min(20, positiveCount(payload.max_layouts) || 20));
    updateStartButton();
  }

  function storeSelection() {
    try { localStorage.setItem(storageKey, selectedId); } catch { /* Storage is optional. */ }
  }

  function pause() {
    playing = false;
    if (animation != null) cancelAnimationFrame(animation);
    animation = null;
    ui.play.textContent = "播放";
    ui.play.setAttribute("aria-pressed", "false");
  }

  function resetReplay() {
    pause();
    replayRevision += 1;
    replayRequest?.abort();
    replayRequest = null;
    replay?.dispose();
    replay = null;
    replayPayload = null;
    selectedCaseId = "";
    ui.replayPanel.hidden = true;
    ui.play.disabled = true;
    ui.seek.disabled = true;
    ui.replayContainer.replaceChildren();
  }

  function setSelection(id) {
    if (id === selectedId && currentJob) return;
    selectedRevision += 1;
    selectedRequest?.abort();
    selectedRequest = null;
    selectedId = id;
    currentJob = null;
    storeSelection();
    resetReplay();
    ui.select.value = selectedId;
    ui.detail.hidden = true;
    ui.empty.hidden = false;
    ui.empty.querySelector("strong").textContent = id ? "正在读取任务…" : "从一个布局开始";
  }

  function renderJobList() {
    const options = jobs.map((job) => `<option value="${escapeHtml(job.job_id)}">${escapeHtml(jobLabel(job))}</option>`);
    ui.select.innerHTML = options.join("") || '<option value="">暂无运行记录</option>';
    ui.select.disabled = !jobs.length;
    if (!jobs.some((job) => job.job_id === selectedId)) setSelection(jobs[0]?.job_id || "");
    ui.select.value = selectedId;
  }

  async function loadJobs() {
    const revision = ++listRevision;
    const payload = await api("/api/runtime/jobs");
    if (disposed || revision !== listRevision) return;
    jobs = Array.isArray(payload.jobs) ? payload.jobs : [];
    renderJobList();
  }

  function renderCases(job) {
    const cases = Array.isArray(job.cases) ? job.cases : [];
    if (!cases.length) {
      ui.cases.innerHTML = '<p class="runtime-case-empty">逐项结果会在运行过程中更新。</p>';
      return;
    }
    ui.cases.innerHTML = cases.map((item, index) => {
      const physics = caseVerdict(item.physics_status, true);
      const task = caseVerdict(item.task_status);
      const reasons = Array.isArray(item.failure_reasons) ? item.failure_reasons : item.failure_reasons ? [item.failure_reasons] : [];
      const event = taskEventName(item.event);
      const primaryReason = reasons.find((reason) => typeof reason === "string" && failureNames[reason] && !reason.startsWith("task_")) || reasons[0];
      const details = reasons.length ? `<details class="runtime-failure"><summary>${escapeHtml(friendlyReason(primaryReason))}${reasons.length > 1 ? ` 等 ${reasons.length} 项` : ""}</summary><ul>${reasons.map((reason) => `<li>${escapeHtml(friendlyReason(reason))}</li>`).join("")}</ul></details>` : "";
      return `<article class="runtime-case${selectedCaseId === item.case_id ? " selected" : ""}">
        <div class="runtime-case-main"><strong>${escapeHtml(event)} <small>${item.seed != null ? `布局种子 ${escapeHtml(item.seed)}` : `任务 ${index + 1}`}</small></strong><div class="runtime-case-badges"><span class="runtime-pill ${physics.tone}">物理 ${physics.label}</span><span class="runtime-pill ${task.tone}">任务 ${task.label}</span><span class="runtime-evidence-label">${item.evidence_complete === true ? "记录完整" : "记录待补全"}</span></div>${details}</div>
        <button type="button" class="runtime-secondary" data-runtime-case="${escapeHtml(item.case_id)}" ${item.replay_available ? "" : "disabled"}>${item.replay_available ? "回放" : "等待轨迹"}</button>
      </article>`;
    }).join("");
  }

  function renderJob(job) {
    const model = describeRuntimeJob(job);
    currentJob = job;
    if (selectedCaseId && !job.cases?.some((item) => item.case_id === selectedCaseId && item.replay_available)) resetReplay();
    ui.detail.hidden = false;
    ui.empty.hidden = true;
    ui.title.textContent = job.label || (job.profile === "baseline" ? "原始控制方案验收" : "优化控制方案验收");
    ui.title.title = ui.title.textContent;
    ui.status.textContent = model.status;
    ui.status.className = `runtime-pill ${job.status === "failed" || job.status === "interrupted" ? "fail" : model.active ? "pending" : "neutral"}`;
    ui.progress.max = Math.max(1, model.total);
    ui.progress.value = model.completed;
    const progress = job.progress || {};
    const stage = { export: "导出场景", settling: "物理稳定性检查", physics: "物理稳定性检查", task: "机器人执行", validation: "校验运行记录", validating: "校验运行记录", prepare: "准备场景" }[progress.stage] || (phaseNames[String(progress.stage || "").toUpperCase()]) || "";
    const step = Number.isFinite(progress.step) ? ` · 当前仿真步 ${progress.step}${progress.total_steps ? ` / ${progress.total_steps}` : ""}` : "";
    ui.progressText.textContent = `${model.completed} / ${model.total} 项完成${stage && model.active ? ` · ${stage}${step}` : ""}`;
    ui.summary.innerHTML = `<div><small>物理通过</small><strong>${model.physics.pass} <em>/ ${model.total}</em></strong><span>${model.physics.fail ? `${model.physics.fail} 项未通过` : "稳定性独立检查"}</span></div>
      <div><small>任务成功</small><strong>${model.tasks.pass} <em>/ ${model.total}</em></strong><span>${model.tasks.fail} 项失败 · ${model.unverified} 项未验证</span></div>
      <div><small>证据校验</small><strong class="runtime-validity ${job.validation?.valid === false ? "fail" : ""}">${model.validation}</strong><span>${model.evidenceCount} / ${model.total} 项记录完整</span></div>`;
    const validationErrors = job.validation?.valid === false && Array.isArray(job.validation.errors) ? job.validation.errors : [];
    ui.error.hidden = !job.error && !validationErrors.length;
    ui.error.textContent = job.error || (validationErrors.length ? `运行记录未通过校验：${validationErrors.map(friendlyReason).join("；")}` : "");
    const report = safeReportUrl(job.report_url);
    ui.report.hidden = !report;
    if (report) ui.report.href = report;
    else ui.report.removeAttribute("href");
    renderCases(job);
  }

  async function loadSelectedJob() {
    if (!selectedId) return;
    const id = selectedId;
    const revision = selectedRevision;
    selectedRequest?.abort();
    const controller = new AbortController();
    selectedRequest = controller;
    try {
      const payload = await api(`/api/runtime/jobs/${encodeURIComponent(id)}`, {}, controller);
      if (!disposed && revision === selectedRevision && id === selectedId && selectedRequest === controller) renderJob(payload);
    } catch (error) {
      if (revision === selectedRevision && selectedRequest === controller) throw error;
    } finally { if (selectedRequest === controller) selectedRequest = null; }
  }

  async function refresh() {
    if (disposed || refreshing) return;
    refreshing = true;
    ui.refresh.disabled = true;
    try {
      if (!config) await loadConfig();
      await loadJobs();
      await loadSelectedJob();
      ui.connection.textContent = "";
    } catch (error) {
      if (!disposed) {
        ui.connection.textContent = "暂时无法读取进度，将自动重试。任务仍由后台执行。";
        if (!config) {
          ui.config.textContent = "暂时无法检查仿真环境，将自动重试。";
          ui.config.className = "runtime-config-status unavailable";
          // History can still be available when configuration inspection fails.
          try { await loadJobs(); await loadSelectedJob(); } catch { /* The next poll retries. */ }
        }
      }
    } finally {
      refreshing = false;
      ui.refresh.disabled = false;
    }
  }

  async function poll() {
    await refresh();
    if (!disposed) timer = setTimeout(poll, 2000);
  }

  function renderFrame(index) {
    if (!replayPayload?.frames?.length || !replay) return;
    frameIndex = Math.min(replayPayload.frames.length - 1, Math.max(0, Math.floor(index)));
    const frame = replay.seek(frameIndex) || replayPayload.frames[frameIndex];
    const model = describeReplayFrame(frame);
    ui.seek.value = String(frameIndex);
    ui.frame.textContent = `${frameIndex + 1} / ${replayPayload.frames.length} 帧`;
    ui.phase.textContent = model.phase;
    ui.contact.textContent = model.contact;
    ui.step.textContent = `${model.step} / ${model.time}`;
    const joints = model.joints.map((value, i) => `<span>关节 ${i + 1}<b>${Number.isFinite(value) ? value.toFixed(3) : "—"} rad</b></span>`);
    const fingers = model.fingers.map((value, i) => `<span>${i === 0 ? "左指" : "右指"}<b>${Number.isFinite(value) ? (value * 1000).toFixed(1) : "—"} mm</b></span>`);
    ui.joints.innerHTML = joints.concat(fingers).join("") || "此帧未记录关节数值";
  }

  function animate(time) {
    if (!playing || !replayPayload) return;
    if (!lastFrameTime) lastFrameTime = time;
    const dt = Number.isFinite(replayPayload.dt) && replayPayload.dt > 0 ? replayPayload.dt : 1 / 60;
    frameRemainder += Math.max(0, Math.min(250, time - lastFrameTime)) * Number(ui.speed.value) / (dt * 1000);
    lastFrameTime = time;
    const advance = Math.floor(frameRemainder);
    if (advance) {
      frameRemainder -= advance;
      renderFrame(frameIndex + advance);
    }
    if (frameIndex >= replayPayload.frames.length - 1) { pause(); return; }
    animation = requestAnimationFrame(animate);
  }

  async function loadReplay(caseId) {
    const item = currentJob?.cases?.find((entry) => entry.case_id === caseId);
    if (!item?.replay_available) return;
    resetReplay();
    selectedCaseId = caseId;
    const revision = replayRevision;
    const jobId = selectedId;
    ui.replayPanel.hidden = false;
    ui.replayTitle.textContent = `${taskEventName(item.event)} · 布局种子 ${item.seed ?? "—"} · 状态轨迹回放`;
    ui.replayStatus.textContent = "正在载入记录…";
    ui.frame.textContent = "0 / 0 帧";
    ui.phase.textContent = "—";
    ui.contact.textContent = "—";
    ui.step.textContent = "—";
    ui.joints.textContent = "—";
    ui.notes.textContent = "";
    renderCases(currentJob);
    const controller = new AbortController();
    replayRequest = controller;
    let instance = null;
    try {
      const payload = await api(`/api/runtime/jobs/${encodeURIComponent(jobId)}/cases/${encodeURIComponent(caseId)}/replay`, {}, controller);
      if (disposed || revision !== replayRevision || selectedId !== jobId) return;
      if (!Array.isArray(payload.frames) || !payload.frames.length) throw new Error("当前记录没有可回放的状态帧");
      instance = new RuntimeReplay(ui.replayContainer, ui.replayStatus);
      replay = instance;
      await instance.load(payload);
      if (disposed || revision !== replayRevision || selectedId !== jobId) { instance.dispose(); return; }
      replayPayload = payload;
      ui.seek.max = String(payload.frames.length - 1);
      ui.seek.disabled = false;
      ui.play.disabled = payload.frames.length < 2;
      ui.notes.textContent = Array.isArray(payload.notes) ? payload.notes.join(" ") : payload.notes || "";
      renderFrame(0);
    } catch (error) {
      if (!disposed && revision === replayRevision && selectedId === jobId) {
        instance?.dispose();
        replay = null;
        ui.replayStatus.textContent = `回放未载入：${error.name === "AbortError" ? "读取超时，可再次点击回放" : error.message}`;
      }
    } finally { if (replayRequest === controller) replayRequest = null; }
  }

  ui.limit.addEventListener("input", () => {
    const value = Number(ui.limit.value);
    ui.count.textContent = Number.isInteger(value) && value >= 1 && value <= Number(ui.limit.max)
      ? `${value} 个布局 · ${value * 2} 项任务` : `请输入 1–${ui.limit.max} 的整数`;
  });
  ui.form.addEventListener("submit", async (event) => {
    event.preventDefault();
    if (submitting || !config?.available) return;
    const limit = Number(ui.limit.value);
    if (!Number.isInteger(limit) || limit < 1 || limit > Number(ui.limit.max)) {
      setMessage(`测试布局数需为 1–${ui.limit.max} 的整数`, true);
      return;
    }
    submitting = true;
    updateStartButton();
    setMessage("正在提交任务…");
    try {
      const payload = await api("/api/runtime/jobs", {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ profile: ui.profile.value, limit_layouts: limit }),
      });
      if (disposed) return;
      listRevision += 1;
      setSelection(payload.job_id);
      // Keep the accepted job visible even if the next list refresh is briefly unavailable.
      jobs = [payload, ...jobs.filter((job) => job.job_id !== payload.job_id)];
      renderJobList();
      renderJob(payload);
      setMessage("任务已提交。可在这里查看进度和逐项结果。");
    } catch (error) {
      setMessage(`提交结果未确认：${error.name === "AbortError" ? "读取超时" : error.message}。请先刷新历史记录，确认是否已创建任务。`, true);
    } finally { submitting = false; updateStartButton(); }
  });
  ui.select.addEventListener("change", async () => {
    setSelection(ui.select.value);
    try { await loadSelectedJob(); ui.connection.textContent = ""; }
    catch { ui.connection.textContent = "暂时无法读取任务，将自动重试。"; }
  });
  ui.refresh.addEventListener("click", () => { if (!config?.available) config = null; refresh(); });
  ui.cases.addEventListener("click", (event) => {
    const button = event.target.closest("[data-runtime-case]");
    if (button && !button.disabled) loadReplay(button.dataset.runtimeCase);
  });
  ui.play.addEventListener("click", () => {
    if (playing) { pause(); return; }
    if (!replayPayload?.frames?.length) return;
    if (frameIndex >= replayPayload.frames.length - 1) renderFrame(0);
    playing = true;
    lastFrameTime = 0;
    frameRemainder = 0;
    ui.play.textContent = "暂停";
    ui.play.setAttribute("aria-pressed", "true");
    animation = requestAnimationFrame(animate);
  });
  ui.seek.addEventListener("input", () => { pause(); renderFrame(Number(ui.seek.value)); });
  poll();
  return {
    refresh,
    dispose() {
      disposed = true;
      clearTimeout(timer);
      selectedRequest?.abort();
      resetReplay();
    },
  };
}
