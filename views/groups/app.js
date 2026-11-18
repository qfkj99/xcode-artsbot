// Xcode 插件「群配置」页面。
// 群名等内容来自 QQ，属于不可信数据，只用 textContent 渲染，不拼接 HTML。

const bridge = window.AstrBotPluginView;
const $ = (id) => document.getElementById(id);
const MAX_COOLDOWN = 86400;
const SVG_NS = "http://www.w3.org/2000/svg";

const state = {
  loaded: false,
  groups: [], // [{ session, name, enabled, cooldown_seconds }]
  savedGroups: [],
  onlyWhitelisted: true,
  defaultCooldown: 5,
  expanded: new Set(),
  invalid: new Set(),
  confirmRemove: null,
  candidates: [],
  saving: false,
};

// ---------------------------------------------------------------------------
// 工具函数
// ---------------------------------------------------------------------------

const clone = (groups) => groups.map((g) => ({ ...g }));
const snapshot = (groups) =>
  JSON.stringify(groups.map((g) => [g.session, g.enabled, g.cooldown_seconds]));
const isDirty = () => snapshot(state.groups) !== snapshot(state.savedGroups);

function el(tag, className, text) {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text !== undefined) node.textContent = text;
  return node;
}

function chevronIcon() {
  const svg = document.createElementNS(SVG_NS, "svg");
  svg.setAttribute("viewBox", "0 0 24 24");
  svg.setAttribute("class", "chevron");
  svg.setAttribute("aria-hidden", "true");
  const path = document.createElementNS(SVG_NS, "path");
  path.setAttribute("d", "M9.3 6.3 7.9 7.7l4.3 4.3-4.3 4.3 1.4 1.4 5.7-5.7z");
  svg.append(path);
  return svg;
}

/** 解析 "<平台ID>:GroupMessage:<会话ID>"，返回 [平台ID, 会话ID] 或 null。 */
function parseSession(value) {
  const parts = String(value || "").split(":");
  if (parts.length < 3 || parts[1] !== "GroupMessage") return null;
  const platform = parts[0];
  const sid = parts.slice(2).join(":");
  return platform && sid ? [platform, sid] : null;
}

function shortId(session) {
  const parsed = parseSession(session);
  const id = parsed ? parsed[1] : String(session || "");
  return id.length > 10 ? `…${id.slice(-8)}` : id;
}

/** 两个会话是否指向同一个群（兼容会话隔离的「用户ID_群ID」和直接填写的群 ID）。 */
function sameGroup(a, b) {
  if (a === b) return true;
  const pa = parseSession(a);
  const pb = parseSession(b);
  const covers = (x, y) => x === y || x.endsWith(`_${y}`) || y.endsWith(`_${x}`);
  if (pa && pb) return pa[0] === pb[0] && covers(pa[1], pb[1]);
  if (pa && !pb) return covers(pa[1], String(b));
  if (!pa && pb) return covers(pb[1], String(a));
  return false;
}

const displayName = (g) => g.name || `未命名群（${shortId(g.session)}）`;

let toastTimer = 0;
function toast(message, isError = false) {
  const node = $("toast");
  node.textContent = message;
  node.classList.toggle("is-error", isError);
  node.hidden = false;
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => (node.hidden = true), isError ? 4000 : 2200);
}

function showStatus(text, isError = false, retry = false) {
  $("status").hidden = false;
  $("status").classList.toggle("is-error", isError);
  $("status-text").textContent = text;
  $("retry-btn").hidden = !retry;
}

// ---------------------------------------------------------------------------
// 渲染
// ---------------------------------------------------------------------------

function render() {
  $("scope-hint").textContent = state.onlyWhitelisted
    ? "只有列表中已启用的群会响应「状态检查」。"
    : "白名单已关闭，所有群都会响应。这里可以单独调整某个群的冷却时间，或停用某个群。";
  $("add-btn").disabled = !state.loaded;
  $("group-list").hidden = !state.loaded || state.groups.length === 0;
  $("empty").hidden = !state.loaded || state.groups.length > 0;
  $("group-list").replaceChildren(...state.groups.map(renderGroup));
  renderSaveBar();
}

function renderGroup(group) {
  const open = state.expanded.has(group.session);
  const item = el("li", "group");
  item.classList.toggle("is-open", open);
  item.classList.toggle("is-disabled", !group.enabled);

  const head = el("button", "group-head");
  head.type = "button";
  head.setAttribute("aria-expanded", String(open));
  const name = el("span", "group-name", displayName(group));
  name.title = group.enabled ? displayName(group) : `${displayName(group)}（已停用）`;
  head.append(chevronIcon(), name);
  head.addEventListener("click", () => {
    if (state.expanded.has(group.session)) state.expanded.delete(group.session);
    else state.expanded.add(group.session);
    state.confirmRemove = null;
    render();
  });
  item.append(head);
  if (!open) return item;

  const body = el("div", "group-body");

  // 冷却时间
  const cooldown = el("input", "input input-sm");
  cooldown.type = "number";
  cooldown.min = "0";
  cooldown.max = String(MAX_COOLDOWN);
  cooldown.step = "1";
  cooldown.inputMode = "numeric";
  cooldown.value = String(group.cooldown_seconds);
  cooldown.classList.toggle("invalid", state.invalid.has(group.session));
  cooldown.addEventListener("input", () => {
    const raw = cooldown.value.trim();
    const ok = /^\d+$/.test(raw) && Number(raw) <= MAX_COOLDOWN;
    cooldown.classList.toggle("invalid", !ok);
    if (ok) {
      group.cooldown_seconds = Number(raw);
      state.invalid.delete(group.session);
    } else {
      state.invalid.add(group.session);
    }
    renderSaveBar();
  });
  body.append(field("冷却时间（秒）", cooldown, el("span", "muted small", "0 表示不限制")));

  // 启用开关
  const sw = el("label", "switch");
  const checkbox = el("input");
  checkbox.type = "checkbox";
  checkbox.checked = group.enabled;
  checkbox.setAttribute("aria-label", "启用");
  checkbox.addEventListener("change", () => {
    group.enabled = checkbox.checked;
    item.classList.toggle("is-disabled", !group.enabled);
    renderSaveBar();
  });
  sw.append(checkbox, el("span"));
  body.append(field("启用", sw));

  // 会话 ID（只读，方便排查）
  body.append(field("会话 ID", el("span", "session-id", group.session)));

  // 移除：点两次确认（iframe 沙箱里不能用 confirm 弹窗）
  const actions = el("div", "row-actions");
  const confirming = state.confirmRemove === group.session;
  const remove = el("button", "btn btn-danger", confirming ? "确认移除" : "移除");
  remove.type = "button";
  remove.classList.toggle("confirm", confirming);
  remove.addEventListener("click", () => {
    if (state.confirmRemove !== group.session) {
      state.confirmRemove = group.session;
      render();
      return;
    }
    state.groups = state.groups.filter((g) => g.session !== group.session);
    state.expanded.delete(group.session);
    state.invalid.delete(group.session);
    state.confirmRemove = null;
    render();
  });
  actions.append(remove);
  body.append(actions);

  item.append(body);
  return item;
}

function field(label, ...controls) {
  const row = el("div", "field");
  const control = el("div", "field-control");
  control.append(...controls);
  row.append(el("span", "field-label", label), control);
  return row;
}

function renderSaveBar() {
  const invalid = state.invalid.size > 0;
  const show = state.loaded && (isDirty() || invalid);
  $("save-bar").hidden = !show;
  $("save-bar").classList.toggle("has-error", invalid);
  $("save-bar-text").textContent = invalid
    ? `冷却时间需要是 0 ~ ${MAX_COOLDOWN} 之间的整数`
    : "有未保存的修改";
  $("save-btn").disabled = invalid || state.saving;
  $("save-btn").textContent = state.saving ? "保存中…" : "保存";
  $("discard-btn").disabled = state.saving;
}

// ---------------------------------------------------------------------------
// 数据读写
// ---------------------------------------------------------------------------

function applyServerData(data) {
  const groups = Array.isArray(data?.groups) ? data.groups : [];
  state.groups = groups.map((g) => ({
    session: String(g.session || ""),
    name: String(g.name || ""),
    enabled: g.enabled !== false,
    cooldown_seconds: Number.isInteger(g.cooldown_seconds) ? g.cooldown_seconds : 5,
  }));
  state.savedGroups = clone(state.groups);
  state.onlyWhitelisted = data?.only_whitelisted !== false;
  state.defaultCooldown = Number.isInteger(data?.default_cooldown) ? data.default_cooldown : 5;
  state.invalid.clear();
  state.confirmRemove = null;
  state.loaded = true;
}

async function load() {
  showStatus("加载中…");
  try {
    applyServerData(await bridge.apiGet("groups"));
    $("status").hidden = true;
  } catch (error) {
    showStatus(`加载失败：${error.message}`, true, true);
  }
  render();
}

async function save() {
  if (state.invalid.size > 0 || state.saving) return;
  state.saving = true;
  renderSaveBar();
  try {
    const data = await bridge.apiPost("groups", {
      groups: state.groups.map((g) => ({
        session: g.session,
        name: g.name,
        enabled: g.enabled,
        cooldown_seconds: g.cooldown_seconds,
      })),
    });
    applyServerData(data);
    render();
    toast("已保存");
  } catch (error) {
    toast(`保存失败：${error.message}`, true);
  } finally {
    state.saving = false;
    renderSaveBar();
  }
}

function discard() {
  state.groups = clone(state.savedGroups);
  state.invalid.clear();
  state.confirmRemove = null;
  render();
}

// ---------------------------------------------------------------------------
// 添加群
// ---------------------------------------------------------------------------

const isAdded = (session) => state.groups.some((g) => sameGroup(g.session, session));

function addGroup(session, name) {
  state.groups.push({
    session,
    name: name || "",
    enabled: true,
    cooldown_seconds: state.defaultCooldown,
  });
  closeDialog();
  render();
  toast(`已添加「${name || shortId(session)}」，记得点保存`);
}

async function openDialog() {
  $("dialog").hidden = false;
  $("search").value = "";
  $("manual-input").value = "";
  $("manual-error").hidden = true;
  $("manual").open = false;
  state.candidates = [];
  $("candidate-list").replaceChildren();
  $("candidate-empty").hidden = false;
  $("candidate-empty").textContent = "加载中…";
  $("search").focus();
  try {
    const data = await bridge.apiGet("group-candidates");
    state.candidates = Array.isArray(data?.groups) ? data.groups : [];
    renderCandidates();
  } catch (error) {
    $("candidate-empty").textContent = `加载失败：${error.message}`;
  }
}

function closeDialog() {
  $("dialog").hidden = true;
}

function renderCandidates() {
  const query = $("search").value.trim().toLowerCase();
  const items = state.candidates
    .map((c) => ({ session: String(c.session || ""), name: String(c.name || "") }))
    .filter((c) => c.session)
    .filter(
      (c) => !query || c.name.toLowerCase().includes(query) || c.session.toLowerCase().includes(query),
    )
    .map((c) => ({ ...c, added: isAdded(c.session) }))
    .sort((a, b) => a.added - b.added);

  $("candidate-list").replaceChildren(
    ...items.map((c) => {
      const li = el("li", "candidate");
      const button = el("button");
      button.type = "button";
      button.disabled = c.added;
      const main = el("span", "candidate-main");
      main.append(
        el("span", "candidate-name", c.name || "未命名群"),
        el("span", "candidate-id", c.session),
      );
      button.append(
        main,
        c.added ? el("span", "candidate-tag", "已添加") : el("span", "candidate-add", "添加"),
      );
      button.addEventListener("click", () => addGroup(c.session, c.name));
      li.append(button);
      return li;
    }),
  );

  const empty = $("candidate-empty");
  if (items.length > 0) {
    empty.hidden = true;
  } else {
    empty.hidden = false;
    empty.textContent =
      state.candidates.length === 0
        ? "还没有记录到任何群。在目标群里发一条消息（比如「状态检查」）后，再打开这里。"
        : "没有匹配的群。";
  }
}

function addManual() {
  const value = $("manual-input").value.trim();
  const error = $("manual-error");
  const showError = (message) => {
    error.textContent = message;
    error.hidden = false;
  };
  if (!value) return showError("请输入会话 ID");
  if (/\s/.test(value) || value.length > 512) return showError("会话 ID 格式不正确");
  if (value.includes(":") && !parseSession(value)) {
    return showError("只能添加群聊会话，格式为 机器人ID:GroupMessage:群ID");
  }
  if (isAdded(value)) return showError("这个群已经在列表里了");
  const known = state.candidates.find((c) => sameGroup(c.session, value));
  addGroup(value, known?.name || "");
}

// ---------------------------------------------------------------------------
// 初始化
// ---------------------------------------------------------------------------

$("add-btn").addEventListener("click", openDialog);
$("save-btn").addEventListener("click", save);
$("discard-btn").addEventListener("click", discard);
$("retry-btn").addEventListener("click", load);
$("dialog-close").addEventListener("click", closeDialog);
$("dialog").addEventListener("click", (event) => {
  if (event.target === $("dialog")) closeDialog();
});
document.addEventListener("keydown", (event) => {
  if (event.key === "Escape" && !$("dialog").hidden) closeDialog();
});
$("search").addEventListener("input", renderCandidates);
$("manual-add").addEventListener("click", addManual);
$("manual-input").addEventListener("keydown", (event) => {
  if (event.key === "Enter") addManual();
});

if (!bridge) {
  showStatus("请在 AstrBot 后台的插件详情页中打开此页面。", true);
} else {
  await bridge.ready();
  await load();
}
