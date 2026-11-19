// Xcode 插件「群发通知」页面。
// 群名、通知内容、QQ 返回的报错都只用 textContent 渲染，不拼接 HTML。

const bridge = window.AstrBotPluginView;
const $ = (id) => document.getElementById(id);
const POLL_MS = 1500;

const state = {
  targets: [], // [{ session, name, enabled, sendable, at_all_used_today }]
  selected: new Set(),
  history: [],
  job: null,
  running: false,
  interval: 2.5,
  maxText: 2000,
  maxImage: 10 * 1024 * 1024,
  image: null, // { id, name, size, dataUrl }
  uploading: false,
  sending: false,
  pollTimer: 0,
};

// ---------------------------------------------------------------------------
// 工具
// ---------------------------------------------------------------------------

function el(tag, className, text) {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text !== undefined) node.textContent = text;
  return node;
}

function formatTime(ts) {
  if (!ts) return "";
  const d = new Date(ts * 1000);
  const pad = (n) => String(n).padStart(2, "0");
  return `${pad(d.getMonth() + 1)}-${pad(d.getDate())} ${pad(d.getHours())}:${pad(d.getMinutes())}`;
}

function formatSize(bytes) {
  if (bytes >= 1024 * 1024) return `${(bytes / 1024 / 1024).toFixed(1)} MB`;
  return `${Math.max(1, Math.round(bytes / 1024))} KB`;
}

const targetName = (t) => t.name || "未命名群";

let toastTimer = 0;
function toast(message, isError = false) {
  const node = $("toast");
  node.textContent = message;
  node.classList.toggle("is-error", isError);
  node.hidden = false;
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => (node.hidden = true), isError ? 4500 : 2500);
}

function showStatus(text, isError = false, retry = false) {
  $("status").hidden = false;
  $("status").classList.toggle("is-error", isError);
  $("status-text").textContent = text;
  $("retry-load").hidden = !retry;
}

function readAsDataUrl(file) {
  return new Promise((resolve, reject) => {
    const reader = new FileReader();
    reader.onload = () => resolve(String(reader.result));
    reader.onerror = () => reject(reader.error);
    reader.readAsDataURL(file);
  });
}

// ---------------------------------------------------------------------------
// 选择群
// ---------------------------------------------------------------------------

function renderTargets() {
  const list = $("target-list");
  list.replaceChildren(
    ...state.targets.map((t) => {
      const li = el("li", "target");
      li.classList.toggle("is-unsendable", !t.sendable);
      const label = el("label");
      const box = el("input");
      box.type = "checkbox";
      box.checked = state.selected.has(t.session);
      box.disabled = !t.sendable;
      box.addEventListener("change", () => {
        if (box.checked) state.selected.add(t.session);
        else state.selected.delete(t.session);
        renderSelectionInfo();
      });
      label.append(box, el("span", "target-name", targetName(t)));
      if (!t.sendable) label.append(el("span", "tag", "无法确定所属机器人"));
      else if (t.at_all_used_today) label.append(el("span", "tag tag-warn", "今天已 @全体"));
      if (!t.enabled) label.append(el("span", "tag", "状态检查已停用"));
      li.append(label);
      return li;
    }),
  );
  $("target-empty").hidden = state.targets.length > 0;
  list.hidden = state.targets.length === 0;
  renderSelectionInfo();
}

function renderSelectionInfo() {
  const sendable = state.targets.filter((t) => t.sendable).length;
  $("selected-count").textContent = `已选 ${state.selected.size} / ${sendable}`;
  renderActions();
}

// ---------------------------------------------------------------------------
// 内容
// ---------------------------------------------------------------------------

function textValue() {
  return $("text").value.trim();
}

function renderTextCount() {
  const len = $("text").value.trim().length;
  const over = len > state.maxText;
  $("text-count").textContent = `${len} / ${state.maxText}`;
  $("text-count").classList.toggle("text-danger", over);
  $("text").classList.toggle("invalid", over);
  renderActions();
}

function renderImage() {
  const img = state.image;
  $("image-preview").hidden = !img && !state.uploading;
  $("image-add").hidden = Boolean(img) || state.uploading;
  if (state.uploading) {
    $("image-name").textContent = "上传中…";
    $("image-meta").textContent = "";
    $("image-thumb").removeAttribute("src");
  } else if (img) {
    $("image-name").textContent = img.name || "图片";
    $("image-meta").textContent = img.size ? formatSize(img.size) : "";
    if (img.dataUrl) $("image-thumb").src = img.dataUrl;
    else $("image-thumb").removeAttribute("src");
  }
  renderActions();
}

async function onImagePicked() {
  const file = $("image-input").files?.[0];
  $("image-input").value = "";
  if (!file) return;
  if (!["image/png", "image/jpeg", "image/gif"].includes(file.type)) {
    toast("只支持 PNG、JPG、GIF 格式的图片", true);
    return;
  }
  if (file.size > state.maxImage) {
    toast(`图片不能超过 ${formatSize(state.maxImage)}`, true);
    return;
  }
  state.uploading = true;
  renderImage();
  try {
    const [dataUrl, result] = await Promise.all([
      readAsDataUrl(file),
      bridge.upload("broadcast/upload", file),
    ]);
    state.image = { ...result.image, dataUrl };
  } catch (error) {
    state.image = null;
    toast(`图片上传失败：${error.message}`, true);
  } finally {
    state.uploading = false;
    renderImage();
  }
}

// ---------------------------------------------------------------------------
// 发送
// ---------------------------------------------------------------------------

function contentProblem() {
  if (state.uploading) return "图片还在上传";
  if (!textValue() && !state.image) return "请填写通知内容或添加图片";
  if (textValue().length > state.maxText) return `文字不能超过 ${state.maxText} 个字`;
  return "";
}

function renderActions() {
  const busy = state.running || state.sending;
  $("running-hint").hidden = !state.running;
  const n = state.selected.size;
  $("send-btn").textContent = n > 0 ? `发送到 ${n} 个群` : "发送";
  $("send-btn").disabled = busy || n === 0 || Boolean(contentProblem());
  $("test-btn").disabled =
    busy || Boolean(contentProblem()) || !state.targets.some((t) => t.sendable);
}

function selectedTargets() {
  return state.targets.filter((t) => t.sendable && state.selected.has(t.session));
}

function imageBlock() {
  const block = el("div");
  if (state.image?.dataUrl) {
    const img = el("img");
    img.src = state.image.dataUrl;
    img.alt = "";
    block.append(img);
  }
  block.append(el("div", "muted small", `图片：${state.image?.name || "已添加"}`));
  return block;
}

/** 消息预览：按实际拆分方式展示每个群会收到的内容。 */
function renderPreview(container, atAll) {
  const text = textValue();
  const parts = [];
  const first = el("div");
  if (atAll) first.append(el("span", "at-all", "@全体成员"), document.createTextNode(text ? "\n" : ""));
  if (text) first.append(document.createTextNode(text));
  if (state.image && !atAll && text) first.append(imageBlock());
  if (atAll || text || !state.image) parts.push(first);
  // @全体成员 只能用在纯文本消息里，图片单独一条；只有图片时也是单独一条
  if (state.image && (atAll || !text)) parts.push(imageBlock());
  container.replaceChildren();
  parts.forEach((part, i) => {
    if (i > 0) container.append(el("div", "preview-split"));
    container.append(part);
  });
}

function openConfirm() {
  const problem = contentProblem();
  const targets = selectedTargets();
  if (problem || targets.length === 0) return toast(problem || "请至少选择一个群", true);

  const atAll = $("at-all").checked;
  const skipped = atAll ? targets.filter((t) => t.at_all_used_today).length : 0;
  const atAllGroups = atAll ? targets.length - skipped : 0;
  const splitCount = atAll && state.image ? atAllGroups : 0;
  const messages = targets.length + splitCount;
  const seconds = Math.ceil(Math.max(0, messages - 1) * state.interval);

  const rows = [
    ["发送到", `${targets.length} 个群`],
    ["@全体成员", atAll ? `${atAllGroups} 个群${skipped ? `（${skipped} 个群今天已 @过，改为普通发送）` : ""}` : "不 @"],
    ["图片", state.image ? "有" : "无"],
    ["共发送", `${messages} 条消息，约 ${seconds < 60 ? `${seconds} 秒` : `${Math.ceil(seconds / 60)} 分钟`}`],
  ];
  $("confirm-summary").replaceChildren(
    ...rows.map(([k, v]) => {
      const li = el("li");
      li.append(el("span", "muted", k), el("span", "", v));
      return li;
    }),
  );
  renderPreview($("confirm-preview"), atAll && atAllGroups > 0);
  $("confirm-dialog").hidden = false;
  $("confirm-ok").focus();
}

async function submit(targets, atAll, isTest) {
  state.sending = true;
  renderActions();
  try {
    const data = await bridge.apiPost("broadcast/send", {
      targets: targets.map((t) => t.session),
      text: textValue(),
      image_id: state.image?.id || null,
      image_name: state.image?.name || "",
      at_all: atAll,
      test: isTest,
    });
    toast(isTest ? "测试消息已开始发送" : "群发已开始");
    applyJob(data.job, data.running);
    $("job-card").scrollIntoView({ behavior: "smooth", block: "start" });
  } catch (error) {
    toast(`发送失败：${error.message}`, true);
  } finally {
    state.sending = false;
    renderActions();
  }
}

function openTest() {
  const problem = contentProblem();
  if (problem) return toast(problem, true);
  const options = state.targets.filter((t) => t.sendable);
  const preferred = options.find((t) => state.selected.has(t.session)) || options[0];
  $("test-target").replaceChildren(
    ...options.map((t) => {
      const option = el("option", "", t.at_all_used_today ? `${targetName(t)}（今天已 @全体）` : targetName(t));
      option.value = t.session;
      option.selected = t === preferred;
      return option;
    }),
  );
  $("test-at-all").checked = false;
  $("test-dialog").hidden = false;
}

// ---------------------------------------------------------------------------
// 进度 / 结果
// ---------------------------------------------------------------------------

const STATE_ICON = { ok: "✓", failed: "!", partial: "!", sending: "…", pending: "", cancelled: "–" };

function targetDetail(t) {
  const errors = t.parts.filter((p) => p.error).map((p) => p.error);
  const unique = [...new Set(errors)];
  const atAllOk = t.parts.some((p) => p.at_all && p.status === "ok");
  switch (t.state) {
    case "ok":
      if (atAllOk) return "已发送（已 @全体成员）";
      if (t.at_all_skipped) return "已发送（今天已 @过全体成员，本次未 @）";
      return "已发送";
    case "sending":
      return "发送中…";
    case "pending":
      return "等待中";
    case "partial": {
      const okKinds = t.parts.filter((p) => p.status === "ok").map((p) => (p.kind === "image" ? "图片" : "文字"));
      return `${okKinds.join("、")}已发送；其余失败：${unique.join("；")}`;
    }
    case "cancelled":
      return unique[0] || "未发送";
    default:
      return unique.join("；") || "发送失败";
  }
}

function applyJob(job, running) {
  state.job = job;
  state.running = Boolean(running);
  renderJob();
  renderActions();
  clearTimeout(state.pollTimer);
  if (job && running && job.status === "running") {
    state.pollTimer = setTimeout(poll, POLL_MS);
  }
}

function renderJob() {
  const job = state.job;
  $("job-card").hidden = !job;
  if (!job) return;
  const s = job.summary;
  const done = s.total - s.pending;
  const isLive = state.running && job.status === "running";
  const label = job.is_test ? "测试发送" : job.retry_of ? "重试发送" : "群发";
  const titles = {
    running: `正在发送 ${done} / ${s.total}`,
    done: s.failed ? `${label}完成：成功 ${s.ok}，失败 ${s.failed}` : `${label}完成：全部 ${s.ok} 个群已发送`,
    cancelled: `已停止：成功 ${s.ok}，未发送 ${s.cancelled + s.pending}`,
    interrupted: `已中断：成功 ${s.ok}，未发送 ${s.cancelled + s.pending}`,
  };
  $("job-title").textContent = titles[job.status] || label;
  const meta = [formatTime(job.created_at), job.operator, job.at_all ? "@全体成员" : "", job.image ? "含图片" : ""].filter(Boolean);
  $("job-meta").textContent = meta.join(" · ");
  $("progress-bar").style.width = `${s.total ? Math.round((done / s.total) * 100) : 0}%`;
  $("cancel-btn").hidden = !isLive;
  $("retry-btn").hidden = isLive || state.running || s.failed + s.cancelled === 0;

  $("job-targets").replaceChildren(
    ...job.targets.map((t) => {
      const li = el("li", `job-target state-${t.state}`);
      li.append(el("span", "state-icon", STATE_ICON[t.state] ?? ""));
      const main = el("div", "job-target-main");
      main.append(el("div", "job-target-name", targetName(t)), el("div", "job-target-detail", targetDetail(t)));
      li.append(main);
      return li;
    }),
  );
}

async function poll() {
  if (!state.job) return;
  try {
    const data = await bridge.apiGet(`broadcast/jobs/${state.job.id}`);
    applyJob(data.job, data.running);
    if (!data.running || data.job.status !== "running") await refreshState(false);
  } catch {
    state.pollTimer = setTimeout(poll, POLL_MS * 2);
  }
}

// ---------------------------------------------------------------------------
// 记录
// ---------------------------------------------------------------------------

function renderHistory() {
  $("history-empty").hidden = state.history.length > 0;
  $("history-list").replaceChildren(
    ...state.history.slice(0, 30).map((h) => {
      const li = el("li", "history-item");
      const main = el("div", "history-main");
      const title = el("span", "history-title");
      const kind = h.is_test ? "测试 · " : "";
      title.append(document.createTextNode(`${formatTime(h.created_at)} · ${kind}${h.total} 个群 · `));
      title.append(el("span", h.failed ? "text-danger" : "text-ok", h.failed ? `成功 ${h.ok}，失败 ${h.failed}` : `成功 ${h.ok}`));
      const extras = [h.status === "running" ? "发送中" : "", h.status === "cancelled" ? "已停止" : "", h.status === "interrupted" ? "已中断" : "", h.at_all ? "@全体" : "", h.has_image ? "图片" : "", h.operator].filter(Boolean);
      if (extras.length) title.append(document.createTextNode(` · ${extras.join(" · ")}`));
      main.append(title, el("span", "history-text", h.text || (h.has_image ? "[图片]" : "")));
      const actions = el("div", "history-actions");
      const view = el("button", "btn btn-text btn-sm", "查看");
      view.type = "button";
      view.addEventListener("click", () => viewJob(h.id));
      const reuse = el("button", "btn btn-text btn-sm", "复用");
      reuse.type = "button";
      reuse.addEventListener("click", () => reuseJob(h.id));
      actions.append(view, reuse);
      li.append(main, actions);
      return li;
    }),
  );
}

async function viewJob(id) {
  try {
    const data = await bridge.apiGet(`broadcast/jobs/${id}`);
    applyJob(data.job, data.running);
    $("job-card").scrollIntoView({ behavior: "smooth", block: "start" });
  } catch (error) {
    toast(`读取记录失败：${error.message}`, true);
  }
}

async function reuseJob(id) {
  try {
    const { job } = await bridge.apiGet(`broadcast/jobs/${id}`);
    $("text").value = job.text || "";
    $("at-all").checked = Boolean(job.at_all);
    state.image = null;
    if (job.image) {
      try {
        const img = await bridge.apiGet(`broadcast/images/${job.image.id}`);
        state.image = { id: job.image.id, name: job.image.name, size: 0, dataUrl: img.data_url };
      } catch {
        toast("原图片已被清理，请重新添加图片", true);
      }
    }
    renderTextCount();
    renderImage();
    window.scrollTo({ top: 0, behavior: "smooth" });
    toast("已填入这条记录的内容，确认后再发送");
  } catch (error) {
    toast(`读取记录失败：${error.message}`, true);
  }
}

// ---------------------------------------------------------------------------
// 加载
// ---------------------------------------------------------------------------

async function refreshState(first) {
  const data = await bridge.apiGet("broadcast/state");
  state.targets = Array.isArray(data.targets) ? data.targets : [];
  state.history = Array.isArray(data.history) ? data.history : [];
  state.interval = Number(data.interval) || 2.5;
  state.maxText = Number(data.max_text_length) || 2000;
  state.maxImage = Number(data.max_image_bytes) || state.maxImage;
  // 首次进入默认勾选启用的群；之后保留用户的选择（去掉已不在群配置里的群）
  const sessions = new Set(state.targets.map((t) => t.session));
  if (first) {
    state.selected = new Set(state.targets.filter((t) => t.sendable && t.enabled).map((t) => t.session));
  } else {
    state.selected = new Set([...state.selected].filter((s) => sessions.has(s)));
  }
  renderTargets();
  renderHistory();
  renderTextCount();
  if (first || !state.job || state.job.id === data.job?.id) applyJob(data.job, data.running);
}

async function load() {
  showStatus("加载中…");
  try {
    await refreshState(true);
    $("status").hidden = true;
    $("content").hidden = false;
  } catch (error) {
    showStatus(`加载失败：${error.message}`, true, true);
  }
}

// ---------------------------------------------------------------------------
// 事件
// ---------------------------------------------------------------------------

$("retry-load").addEventListener("click", load);
$("select-all").addEventListener("click", () => {
  state.selected = new Set(state.targets.filter((t) => t.sendable).map((t) => t.session));
  renderTargets();
});
$("select-none").addEventListener("click", () => {
  state.selected.clear();
  renderTargets();
});
$("text").addEventListener("input", renderTextCount);
$("image-add").addEventListener("click", () => $("image-input").click());
$("image-input").addEventListener("change", onImagePicked);
$("image-remove").addEventListener("click", () => {
  state.image = null;
  renderImage();
});
$("send-btn").addEventListener("click", openConfirm);
$("confirm-cancel").addEventListener("click", () => ($("confirm-dialog").hidden = true));
$("confirm-ok").addEventListener("click", () => {
  $("confirm-dialog").hidden = true;
  submit(selectedTargets(), $("at-all").checked, false);
});
$("test-btn").addEventListener("click", openTest);
$("test-cancel").addEventListener("click", () => ($("test-dialog").hidden = true));
$("test-ok").addEventListener("click", () => {
  const target = state.targets.find((t) => t.session === $("test-target").value);
  $("test-dialog").hidden = true;
  if (target) submit([target], $("test-at-all").checked, true);
});
$("cancel-btn").addEventListener("click", async () => {
  const btn = $("cancel-btn");
  if (!btn.classList.contains("confirm")) {
    btn.classList.add("confirm");
    btn.textContent = "确认停止";
    setTimeout(() => {
      btn.classList.remove("confirm");
      btn.textContent = "停止发送";
    }, 4000);
    return;
  }
  try {
    await bridge.apiPost("broadcast/cancel", {});
    toast("正在停止，当前这条发完后不再继续");
  } catch (error) {
    toast(`停止失败：${error.message}`, true);
  }
});
$("retry-btn").addEventListener("click", async () => {
  if (!state.job) return;
  try {
    const data = await bridge.apiPost("broadcast/retry", { job_id: state.job.id });
    toast("开始重试失败的群");
    applyJob(data.job, data.running);
  } catch (error) {
    toast(`重试失败：${error.message}`, true);
  }
});
for (const id of ["confirm-dialog", "test-dialog"]) {
  $(id).addEventListener("click", (event) => {
    if (event.target === $(id)) $(id).hidden = true;
  });
}
document.addEventListener("keydown", (event) => {
  if (event.key === "Escape") {
    $("confirm-dialog").hidden = true;
    $("test-dialog").hidden = true;
  }
});

if (!bridge) {
  showStatus("请在 AstrBot 后台的插件详情页中打开此页面。", true);
} else {
  await bridge.ready();
  await load();
}
