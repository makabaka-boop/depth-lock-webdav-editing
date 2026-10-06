"use strict";
/* 轻量编辑页：取得文件与 ETag -> 锁定 -> 编辑 -> 保存。
 * 冲突时保留本地草稿，同时拉取并展示服务器权威版本。 */

const LOCK_TIMEOUT = "Second-120";

const el = (id) => document.getElementById(id);
const ui = {
  path: el("path"), btnLoad: el("btnLoad"),
  btnLock: el("btnLock"), btnRefresh: el("btnRefresh"),
  btnUnlock: el("btnUnlock"), btnSave: el("btnSave"),
  lockBar: el("lockBar"), lockState: el("lockState"), countdown: el("countdown"),
  etag: el("etag"), token: el("token"), tokenLine: el("tokenLine"),
  draft: el("draft"), banner: el("banner"),
  authorityCard: el("authorityCard"), authority: el("authority"),
  authorityEtag: el("authorityEtag"), saveAsHint: el("saveAsHint"),
};

const state = {
  etag: "",          // 最近一次与服务器同步的强 ETag
  token: "",         // 本客户端持有的锁令牌
  expiresAt: null,   // ISO 时间
  savedDraft: "",    // 已保存或已加载的正文，用于区分是否有未保存改动
  timer: null,
};

function banner(kind, message) {
  ui.banner.className = kind;
  ui.banner.textContent = message;
}

function clearBanner() {
  ui.banner.className = "";
  ui.banner.textContent = "";
}

async function req(method, path, { body, headers } = {}) {
  const opts = { method, headers: Object.assign({}, headers) };
  if (body !== undefined) opts.body = body;
  const resp = await fetch(path, opts);
  const text = await resp.text();
  return {
    status: resp.status,
    etag: resp.headers.get("ETag"),
    lockToken: (resp.headers.get("Lock-Token") || "").replace(/^<|>$/g, ""),
    text,
  };
}

function lockinfoXml() {
  return '<?xml version="1.0" encoding="utf-8"?>' +
    '<D:lockinfo xmlns:D="DAV:">' +
    "<D:lockscope><D:exclusive/></D:lockscope>" +
    "<D:locktype><D:write/></D:locktype>" +
    "<D:owner><D:href>web-editor</D:href></D:owner>" +
    "</D:lockinfo>";
}

function renderLockState() {
  const held = Boolean(state.token);
  ui.lockBar.classList.toggle("active", held);
  ui.lockState.textContent = held ? "持有独占写锁" : "未锁定";
  ui.btnLock.disabled = held;
  ui.btnRefresh.disabled = !held;
  ui.btnUnlock.disabled = !held;
  ui.btnSave.disabled = !held;
  ui.tokenLine.hidden = !held;
  ui.token.value = state.token;
  updateCountdown();
}

function updateCountdown() {
  if (!state.token || !state.expiresAt) { ui.countdown.textContent = ""; return; }
  const left = Math.max(0, Math.round(
    (new Date(state.expiresAt).getTime() - Date.now()) / 1000));
  ui.countdown.textContent = "剩余 " + left + " 秒";
  if (left === 0) {
    // 锁到期：令牌已失效，保存不可能再成功。
    state.token = "";
    state.expiresAt = null;
    renderLockState();
    banner("warn", "锁已到期，令牌失效。请重新锁定后再保存（草稿已保留）。");
  }
}

setInterval(updateCountdown, 1000);

// ---- 取得文件 ------------------------------------------------------------

async function loadFile() {
  clearBanner();
  const path = ui.path.value.trim();
  if (!path) { banner("error", "请填写资源路径。"); return; }
  const r = await req("GET", encodeURI(path));
  if (r.status === 404) {
    banner("error", "资源不存在：本工作区只允许打开建库时已有的资源。"); return;
  }
  if (r.status === 405) {
    banner("error", "集合（目录）不能作为文件打开；它只用于 LOCK / UNLOCK。");
    return;
  }
  if (r.status !== 200) {
    banner("error", "GET 失败：" + r.status + " " + r.text); return;
  }
  state.etag = r.etag || "";
  ui.etag.value = state.etag;
  // 新载入会丢弃未保存草稿——若本地有改动先提醒。
  if (ui.draft.value !== state.savedDraft && ui.draft.value !== "") {
    if (!confirm("载入将覆盖当前草稿，确定继续？")) return;
  }
  ui.draft.value = r.text;
  state.savedDraft = r.text;
  ui.authorityCard.hidden = true;
  ui.saveAsHint.textContent = "";
  renderLockState();
  banner("info", "已取得正文与 ETag=" + state.etag +
    "。现在可以尝试锁定并编辑。");
}

// ---- LOCK / 刷新 / UNLOCK ------------------------------------------------

async function lock(kind) {
  clearBanner();
  const path = ui.path.value.trim();
  const headers = {
    Depth: "0",
    Timeout: LOCK_TIMEOUT,
    "Content-Type": "application/xml; charset=utf-8",
  };
  let body = lockinfoXml();
  if (kind === "refresh") {
    if (!state.token) return;
    headers["If"] = "(<" + state.token + ">)";
    body = "";  // 刷新时 RFC4918 允许空请求体
  }
  const r = await req("LOCK", encodeURI(path), { body, headers });
  if (r.status === 423) {
    banner("error", "锁定失败（423）：资源或其祖先/后代已被冲突的独占锁持有。");
    return;
  }
  if (r.status === 412) {
    banner("error", "刷新失败（412）：原令牌已失效（可能已到期并被他人重新锁定）。");
    state.token = ""; state.expiresAt = null; renderLockState();
    return;
  }
  if (!(r.status === 200 || r.status === 201)) {
    banner("error", "LOCK 失败：" + r.status + " " + r.text); return;
  }
  if (kind !== "refresh") {
    state.token = r.lockToken;
    banner("info", "已取得独占写锁。请在锁到期前编辑并保存。");
  } else {
    banner("info", "锁已刷新，令牌保持不变：" + state.token);
  }
  const m = /<D?:timeout[^>]*>([^<]+)<\/D?:timeout>/.exec(r.text)
    || /<timeout[^>]*>([^<]+)<\/timeout>/.exec(r.text);
  if (m && m[1].toLowerCase() !== "infinite") {
    const secs = parseInt(m[1].replace(/^Second-/i, ""), 10);
    state.expiresAt = new Date(Date.now() + secs * 1000).toISOString();
  } else {
    state.expiresAt = null;
  }
  renderLockState();
}

async function unlock() {
  clearBanner();
  if (!state.token) return;
  const r = await req("UNLOCK", encodeURI(ui.path.value.trim()), {
    headers: { "Lock-Token": "<" + state.token + ">" },
  });
  if (r.status === 204) {
    state.token = ""; state.expiresAt = null;
    renderLockState();
    banner("info", "已解锁。");
  } else {
    banner("error", "UNLOCK 失败：" + r.status +
      "（过期/他人令牌无法解开当前锁） " + r.text);
  }
}

// ---- 保存：锁令牌 AND ETag，强 If-Match ---------------------------------

async function save() {
  clearBanner();
  if (!state.token) { banner("error", "尚未持有锁，无法保存。"); return; }
  const path = ui.path.value.trim();
  const draft = ui.draft.value;
  // 无标签条件列表：正向锁令牌 + ETag，同列表 AND；另加强 If-Match。
  const headers = {
    "Content-Type": "text/plain; charset=utf-8",
    "If-Match": state.etag,
    "If": "(<" + state.token + "> " + state.etag + ")",
  };
  const r = await req("PUT", encodeURI(path),
    { body: new TextEncoder().encode(draft), headers });

  if (r.status === 204) {
    state.etag = r.etag || state.etag;
    ui.etag.value = state.etag;
    state.savedDraft = draft;
    ui.authorityCard.hidden = true;
    ui.saveAsHint.textContent = "";
    banner("info", "保存成功，新 ETag=" + state.etag + "。");
    return;
  }

  if (r.status === 412) {
    // 版本已被别人推进：草稿保留，拉取权威版本对照。
    banner("conflict",
      "保存被拒绝（412 Precondition Failed）：服务器版本已更新，你的 ETag 过期。\n" +
      "本地草稿原样保留在编辑框中，下方是服务器上的权威版本，请人工合并后再试。");
  } else if (r.status === 423) {
    banner("error",
      "保存被拒绝（423 Locked）：锁令牌无效或未覆盖全部锁（可能锁已到期、" +
      "或祖先集合被他人深度锁定）。草稿已保留。");
  } else {
    banner("error", "保存失败：" + r.status + " " + r.text + "（正文未改动）。");
    return;
  }

  // 冲突后取回权威版本（只读展示，不碰本地草稿）。
  const g = await req("GET", encodeURI(path));
  if (g.status === 200) {
    ui.authorityCard.hidden = false;
    ui.authority.value = g.text;
    ui.authorityEtag.textContent = "权威 ETag：" + g.etag;
  }
}

ui.btnLoad.addEventListener("click", loadFile);
ui.btnLock.addEventListener("click", () => lock("new"));
ui.btnRefresh.addEventListener("click", () => lock("refresh"));
ui.btnUnlock.addEventListener("click", unlock);
ui.btnSave.addEventListener("click", save);
ui.path.addEventListener("keydown", (e) => {
  if (e.key === "Enter") loadFile();
});

renderLockState();
banner("info", "流程：取得文件与 ETag → 锁定 → 编辑 → 保存。" +
  " 可打开第二个浏览器窗口模拟另一个客户端。");
