const form = document.querySelector("#scene-form");
const promptInput = document.querySelector("#prompt");
const seedInput = document.querySelector("#seed");
const countInput = document.querySelector("#count");
const usdInput = document.querySelector("#export-usd");
const generateButton = document.querySelector("#generate");
const message = document.querySelector("#form-message");
const emptyState = document.querySelector("#empty-state");
const resultContent = document.querySelector("#result-content");
const badge = document.querySelector("#validation-badge");
const preview = document.querySelector("#preview");
const variantsSection = document.querySelector("#variants-section");
const variantGrid = document.querySelector("#variant-grid");
const inspector = document.querySelector("#inspector");
const objectTable = document.querySelector("#object-table");
const fileLinksContainer = document.querySelector("#file-links");
const workerState = document.querySelector("#worker-state b");
const llmStatusDot = document.querySelector("#llm-status-dot");
const llmStatusLabel = document.querySelector("#llm-status-label");
const llmStatusDetail = document.querySelector("#llm-status-detail");
const llmTestButton = document.querySelector("#llm-test");
const llmConfigPath = document.querySelector("#llm-config-path");
const llmKeyEnv = document.querySelector("#llm-key-env");
const llmTestResult = document.querySelector("#llm-test-result");
const revisionForm = document.querySelector("#revision-form");
const revisionInput = document.querySelector("#revision-instruction");
const revisionButton = document.querySelector("#revise");
const revisionMessage = document.querySelector("#revision-message");
const revisionSource = document.querySelector("#revision-source");
let currentItem = null;

const escapeHtml = (value) => String(value ?? "")
  .replaceAll("&", "&amp;")
  .replaceAll("<", "&lt;")
  .replaceAll(">", "&gt;")
  .replaceAll('"', "&quot;")
  .replaceAll("'", "&#039;");

document.querySelectorAll("[data-prompt]").forEach((button) => {
  button.addEventListener("click", () => {
    promptInput.value = button.dataset.prompt;
    promptInput.focus();
  });
});

document.querySelectorAll("[data-revision]").forEach((button) => {
  button.addEventListener("click", () => {
    revisionInput.value = button.dataset.revision;
    revisionInput.focus();
  });
});

async function loadLLMStatus() {
  try {
    const response = await fetch("/api/llm/status");
    const status = await response.json();
    if (!response.ok) throw new Error(status.error || "LLM 状态读取失败");
    workerState.textContent = `Parser: ${status.parser || "keyword"}`;
    llmConfigPath.textContent = status.config_path;
    llmKeyEnv.textContent = status.api_key_env;
    llmTestButton.disabled = !status.active;
    llmStatusDot.className = `llm-status-dot ${status.active ? "active" : "offline"}`;
    if (status.active) {
      llmStatusLabel.textContent = `已接入 ${status.model}`;
      llmStatusDetail.textContent = `${status.mode} · ${status.endpoint}`;
      llmTestButton.textContent = "测试连接";
    } else {
      llmStatusLabel.textContent = "离线关键词模式";
      llmStatusDetail.textContent = "填写配置文件后重启服务即可启用 LLM";
      llmTestButton.textContent = "配置后可测试";
    }
  } catch (error) {
    workerState.textContent = "Local worker unavailable";
    llmStatusDot.className = "llm-status-dot error";
    llmStatusLabel.textContent = "状态读取失败";
    llmStatusDetail.textContent = error.message;
  }
}

loadLLMStatus();

llmTestButton.addEventListener("click", async () => {
  llmTestButton.disabled = true;
  llmTestButton.textContent = "测试中…";
  llmTestResult.className = "llm-test-result";
  llmTestResult.textContent = "正在发送一次真实的结构化 SceneIntent 请求…";
  try {
    const response = await fetch("/api/llm/test", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: "{}",
    });
    const payload = await response.json();
    if (!response.ok) throw new Error(payload.error || "连接测试失败");
    llmTestResult.className = "llm-test-result success";
    llmTestResult.textContent = `连接成功：${payload.model}，${payload.elapsed_ms} ms，返回 ${payload.sample.object_count} 个物体。`;
  } catch (error) {
    llmTestResult.className = "llm-test-result error";
    llmTestResult.textContent = error.message;
  } finally {
    llmTestButton.disabled = false;
    llmTestButton.textContent = "重新测试";
  }
});

function setBusy(busy) {
  generateButton.disabled = busy;
  generateButton.querySelector("span").textContent = busy ? "正在编译场景…" : "生成仿真场景";
  generateButton.querySelector("b").textContent = busy ? "···" : "↗";
}

function fileLinks(files, sceneId) {
  const labels = { intent: "SceneIntent", revision: "Revision", scene_spec: "SceneSpec", layout: "Layout JSON", validation: "Validation", preview: "SVG Preview", usd: "USD" };
  const links = Object.entries(files).map(([name, url]) => `<a href="${escapeHtml(url)}" target="_blank" rel="noreferrer">${labels[name] || escapeHtml(name)} ↗</a>`).join("");
  const launcher = files.usd
    ? `<button class="open-isaac-button" type="button" data-scene-id="${escapeHtml(sceneId)}">在 Isaac Sim 中看 3D 实体</button>`
    : "";
  return `${launcher}${links}`;
}

fileLinksContainer.addEventListener("click", async (event) => {
  const button = event.target.closest("[data-scene-id]");
  if (!button) return;
  button.disabled = true;
  const original = button.textContent;
  button.textContent = "正在启动 Isaac Sim…";
  try {
    const response = await fetch("/api/open-isaac", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ scene_id: button.dataset.sceneId }),
    });
    const payload = await response.json();
    if (!response.ok) throw new Error(payload.error || "Isaac Sim 启动失败");
    button.textContent = "Isaac Sim 正在打开（首次约 1 分钟）";
  } catch (error) {
    button.disabled = false;
    button.textContent = original;
    message.className = "form-message error";
    message.textContent = error.message;
  }
});

function renderScene(item) {
  currentItem = item;
  const scene = item.scene;
  const valid = item.validation.valid;
  emptyState.hidden = true;
  resultContent.hidden = false;
  inspector.hidden = false;
  badge.className = `validation-badge ${valid ? "valid" : "invalid"}`;
  badge.textContent = valid ? "✓ 几何校验通过" : "! 需要检查";
  preview.src = `${item.files.preview}?v=${Date.now()}`;
  document.querySelector("#matched-recipe").textContent = item.matched_recipe.name;
  document.querySelector("#scene-id").textContent = scene.scene_id;
  document.querySelector("#object-count").textContent = String(scene.objects.length);
  document.querySelector("#room-size").textContent = `${scene.room_dimensions_m[0]} × ${scene.room_dimensions_m[1]} m`;
  document.querySelector("#parser-name").textContent = item.prompt_parser;
  fileLinksContainer.innerHTML = fileLinks(item.files, scene.scene_id);
  revisionForm.hidden = !item.files.intent;
  revisionSource.textContent = item.revision
    ? `修改自 ${item.revision.source_scene_id}`
    : `当前版本 ${scene.scene_id}`;

  objectTable.innerHTML = scene.objects.map((object) => {
    const xyz = object.pose.position.map((number) => Number(number).toFixed(3)).join(", ");
    return `<tr><td>${escapeHtml(object.object_id)}</td><td>${escapeHtml(object.category)}</td><td>${escapeHtml(object.support || "fixed")}</td><td>${xyz}</td><td>${Number(object.pose.yaw_deg).toFixed(1)}°</td></tr>`;
  }).join("");
}

function setRevisionBusy(busy) {
  revisionButton.disabled = busy;
  revisionButton.querySelector("span").textContent = busy ? "LLM 正在修改场景…" : "应用修改并生成新版本";
  revisionButton.querySelector("b").textContent = busy ? "···" : "↻";
}

revisionForm.addEventListener("submit", async (event) => {
  event.preventDefault();
  if (!currentItem) return;
  revisionMessage.className = "revision-message";
  revisionMessage.textContent = "正在保留未提及内容，并重新解算修改后的布局…";
  setRevisionBusy(true);
  try {
    const response = await fetch("/api/revise", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        scene_id: currentItem.scene.scene_id,
        instruction: revisionInput.value,
        seed: currentItem.scene.seed,
        export_usd: Boolean(currentItem.files.usd),
      }),
    });
    const payload = await response.json();
    if (!response.ok) throw new Error(payload.error || "场景修改失败");
    renderScene(payload.item);
    renderVariants([]);
    revisionInput.value = "";
    revisionMessage.className = "revision-message success";
    revisionMessage.textContent = `已生成新版本；原场景 ${payload.source_scene_id} 保持不变。`;
  } catch (error) {
    revisionMessage.className = "revision-message error";
    revisionMessage.textContent = error.message;
  } finally {
    setRevisionBusy(false);
  }
});

function renderVariants(items) {
  variantsSection.hidden = items.length < 2;
  variantGrid.innerHTML = "";
  if (items.length < 2) return;
  items.forEach((item, index) => {
    const button = document.createElement("button");
    button.type = "button";
    button.className = "variant-card";
    button.innerHTML = `<img src="${escapeHtml(item.files.preview)}?v=${Date.now()}" alt="Seed ${item.scene.seed} 预览"><div><strong>SEED ${item.scene.seed}</strong><small>${escapeHtml(item.scene.scene_id)}</small></div>`;
    button.addEventListener("click", () => {
      renderScene(item);
      document.querySelector("#result-card").scrollIntoView({ behavior: "smooth", block: "start" });
    });
    variantGrid.appendChild(button);
  });
}

form.addEventListener("submit", async (event) => {
  event.preventDefault();
  message.className = "form-message";
  message.textContent = "正在匹配事件配方并解算空间约束…";
  setBusy(true);
  try {
    const response = await fetch("/api/generate", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        prompt: promptInput.value,
        seed: Number(seedInput.value),
        count: Number(countInput.value),
        export_usd: usdInput.checked,
      }),
    });
    const payload = await response.json();
    if (!response.ok) throw new Error(payload.error || "生成失败");
    renderScene(payload.items[0]);
    renderVariants(payload.items);
    const warning = payload.items[0].parser_warning ? `；LLM 降级原因：${payload.items[0].parser_warning}` : "";
    message.textContent = `已生成 ${payload.count} 个场景，${payload.valid_count} 个通过校验。解析：${payload.items[0].prompt_parser}；配方：${payload.items[0].matched_recipe.name}${warning}`;
    if (window.matchMedia("(max-width: 1120px)").matches) {
      document.querySelector("#result-card").scrollIntoView({ behavior: "smooth", block: "start" });
    }
  } catch (error) {
    message.className = "form-message error";
    message.textContent = error.message;
  } finally {
    setBusy(false);
  }
});
