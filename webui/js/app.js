/* ═══════════════ WebView 版前端逻辑 ═══════════════
   原则（学自 Agent2API app.js）：
   - 页面 DOM 常驻，切页只切 class，零重建；
   - 数据"切入页面才拉"，离开不轮询；
   - 桥失败永远有兜底显示，绝不白屏。 */

"use strict";

const $ = (sel) => document.querySelector(sel);
const $$ = (sel) => document.querySelectorAll(sel);

/* ── 切页（display:none / active 切换）── */
let currentPage = "connection";

function showPage(name) {
  currentPage = name;
  $$(".page").forEach((p) => p.classList.toggle("active", p.dataset.page === name));
  $$(".nav-item").forEach((n) => n.classList.toggle("active", n.dataset.page === name));
  // 切入页面才拉数据（一次性，不轮询）
  if (name === "connection") refreshConnection();
  if (name === "stats") refreshStats();
  if (name === "search") setTimeout(() => $("#search-input").focus(), 50);
}

$("#nav").addEventListener("click", (e) => {
  const btn = e.target.closest(".nav-item");
  if (btn) showPage(btn.dataset.page);
});

/* ── 桥访问器：pywebview 注入前的安全兜底 ── */
function api() {
  return (window.pywebview && window.pywebview.api) || null;
}

function log(line, cls = "") {
  const box = $("#console");
  const el = document.createElement("div");
  el.className = "console-line" + (cls ? " " + cls : "");
  el.textContent = line;
  box.appendChild(el);
  box.scrollTop = box.scrollHeight;
  // 控制台只留 200 行，防长跑膨胀
  while (box.children.length > 200) box.removeChild(box.firstChild);
}

/* ── 连接状态页 ── */
function setBadge(id, online) {
  const b = $(id);
  b.classList.remove("checking", "online", "offline");
  b.classList.add(online ? "online" : "offline");
  b.textContent = online ? "在线" : "离线";
}

async function refreshConnection() {
  const bridge = api();
  if (!bridge) return; // pywebview 尚未注入
  try {
    const res = await bridge.get_db_status();
    if (!res || res.ok !== true) throw new Error(res && res.error);
    $("#d-addr").textContent = res.d.addr;
    $("#e-addr").textContent = res.e.addr;
    $("#d-msg").textContent = res.d.msg;
    $("#e-msg").textContent = res.e.msg;
    setBadge("#badge-d", res.d.online);
    setBadge("#badge-e", res.e.online);
    log(`双库检测完成 D:${res.d.online ? "✓" : "✗"} E:${res.e.online ? "✓" : "✗"}`,
        res.d.online && res.e.online ? "ok" : "");
  } catch (err) {
    setBadge("#badge-d", false);
    setBadge("#badge-e", false);
    log("双库检测失败: " + err, "err");
  }
}

/* ── 统计页（读缓存，秒开）── */
const STAT_LABELS = {
  comments: ["总评论", "条"],
  danmaku: ["总弹幕", "条"],
  videos: ["视频数", "个"],
  users: ["用户数", "人"],
  crc_hits: ["UID 反查命中", "次"],
};

async function refreshStats() {
  const bridge = api();
  if (!bridge) return;
  try {
    const res = await bridge.get_overview();
    const grid = $("#stats-grid");
    grid.innerHTML = "";
    const stats = res && res.stats;
    if (!stats) {
      grid.innerHTML = '<div class="stat-card"><div class="stat-label">暂无缓存统计</div>' +
        '<div class="stat-sub">在 Tk 版「统计洞察」点一次刷新即可生成缓存</div></div>';
      return;
    }
    for (const [key, [label, unit]] of Object.entries(STAT_LABELS)) {
      if (!(key in stats)) continue;
      const card = document.createElement("div");
      card.className = "stat-card";
      const v = Number(stats[key]) || 0;
      card.innerHTML = `<div class="stat-label">${label}</div>` +
        `<div class="stat-value">${v.toLocaleString()}</div>` +
        `<div class="stat-sub">${unit}</div>`;
      grid.appendChild(card);
    }
  } catch (err) {
    log("统计读取失败: " + err, "err");
  }
}

/* ── 全局搜索页 ── */
const SEARCH_COLS = {
  comments: [
    ["ctime", "时间", 130], ["sender", "发送者", 170],
    ["ip_location", "属地", 90], ["like_num", "赞", 60],
    ["video", "视频", 260], ["content", "内容", 0],
  ],
  danmaku: [
    ["ctime", "时间", 130], ["sender", "发送者", 170],
    ["progress", "进度", 80], ["video", "视频", 260], ["content", "内容", 0],
  ],
  videos: [
    ["pubdate", "发布时间", 130], ["title", "标题", 0],
    ["bvid", "BV", 140], ["owner", "UP", 170],
    ["comment_count", "评论", 70], ["danmaku_count", "弹幕", 70],
  ],
};

function fmtRow(kind, r) {
  const sender = r.uname
    ? (r.uid ? `${r.uname}(${r.uid})` : r.uname)
    : (r.uid || r.mid_hash ? `#${r.mid_hash || r.uid}` : "-");
  if (kind === "comments") {
    return {
      ctime: r.ctime || "", sender,
      ip_location: r.ip_location || "-",
      like_num: r.like_num ?? 0,
      video: `${r.bvid || ""} ${String(r.title || "").slice(0, 24)}`,
      content: String(r.content || "").slice(0, 120),
    };
  }
  if (kind === "danmaku") {
    const prog = r.progress != null ? `${Number(r.progress).toFixed(1)}s` : "-";
    return { ctime: r.ctime || "", sender, progress: prog,
             video: `${r.bvid || ""} ${String(r.title || "").slice(0, 24)}`,
             content: String(r.content || "").slice(0, 120) };
  }
  const owner = r.owner_name
    ? (r.owner_uid ? `${r.owner_name}(${r.owner_uid})` : r.owner_name)
    : (r.owner_uid || "-");
  return { pubdate: r.pubdate || "", title: String(r.title || "").slice(0, 48),
           bvid: r.bvid || "", owner,
           comment_count: r.comment_count ?? 0, danmaku_count: r.danmaku_count ?? 0 };
}

let searching = false;

async function doSearch() {
  if (searching) return;
  const bridge = api();
  if (!bridge) return;
  const kw = $("#search-input").value.trim();
  if (!kw) { $("#search-status").textContent = "请输入关键词"; return; }
  const kind = $("#search-kind").value;

  searching = true;
  const btn = $("#search-btn");
  btn.disabled = true;
  btn.textContent = "检索中…";
  $("#search-status").textContent = "正在跨仓检索（E 库为 HDD，可能数秒）…";
  $("#result-body").innerHTML = "";
  $("#search-empty").style.display = "none";
  const t0 = performance.now();

  try {
    const res = await bridge.search(kw, kind, 100);
    const ms = Math.round(performance.now() - t0);
    if (!res || res.ok !== true) throw new Error(res && res.error || "检索失败");
    const rows = res.rows || [];
    renderResults(kind, rows);
    const failedNote = (res.failed && res.failed.length) ? `，${res.failed.length} 个仓不可达` : "";
    $("#search-status").textContent =
      `共 ${rows.length} 条 · 耗时 ${ms}ms${failedNote}（缓存命中则近乎瞬时）`;
  } catch (err) {
    $("#search-status").textContent = "检索失败: " + err;
    log("搜索失败: " + err, "err");
  } finally {
    searching = false;
    btn.disabled = false;
    btn.textContent = "搜索";
  }
}

function renderResults(kind, rows) {
  const head = $("#result-head");
  const body = $("#result-body");
  head.innerHTML = "";
  body.innerHTML = "";
  const cols = SEARCH_COLS[kind] || SEARCH_COLS.comments;
  head.innerHTML = "<tr>" + cols.map(([k, label]) => `<th>${label}</th>`).join("") + "</tr>";
  if (!rows.length) {
    $("#search-empty").style.display = "block";
    return;
  }
  $("#search-empty").style.display = "none";
  const frag = document.createDocumentFragment();
  for (const r of rows) {
    const data = fmtRow(kind, r);
    const tr = document.createElement("tr");
    tr.innerHTML = cols.map(([k, , w]) => {
      const v = String(data[k] ?? "");
      const cls = k === "content" || k === "title" ? ' class="cell-content"' : "";
      const title = v.length > 40 ? ` title="${v.replace(/"/g, "&quot;")}"` : "";
      return `<td${cls}${title}>${escapeHtml(v) || '<span class="cell-muted">-</span>'}</td>`;
    }).join("");
    frag.appendChild(tr);
  }
  body.appendChild(frag);
}

function escapeHtml(s) {
  return s.replace(/[&<>"]/g, (c) =>
    ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));
}

$("#search-btn").addEventListener("click", doSearch);
$("#search-input").addEventListener("keydown", (e) => {
  if (e.key === "Enter") doSearch();
});

/* ── 研报页 ── */
let reportTask = null;
let lastReportPath = "";

function rpBadge(text, cls) {
  const b = $("#rp-badge");
  b.textContent = text;
  b.className = "badge" + (cls ? " " + cls : "");
}

function rpSections() {
  const out = {};
  $$("#rp-sections input[data-sec]").forEach((el) => { out[el.dataset.sec] = el.checked; });
  return out;
}

/* 通用长任务轮询：每 600ms 问一次，完成/失败即停 */
async function pollTask(taskId, onTick) {
  const bridge = api();
  while (bridge) {
    await new Promise((r) => setTimeout(r, 600));
    let st;
    try {
      st = await bridge.task_status(taskId);
    } catch (e) {
      return { state: "failed", error: String(e) };
    }
    if (onTick) onTick(st);
    if (st && st.state !== "running") return st;
  }
  return { state: "failed", error: "桥不可用" };
}

async function startReport() {
  const bridge = api();
  if (!bridge || reportTask) return;
  const bvid = $("#rp-bvid").value.trim();
  if (!bvid) { $("#rp-status").textContent = "请输入要生成研报的视频 BV 号（如 BV1tJ4m1T7QL）"; return; }

  reportTask = "starting";
  $("#rp-generate").disabled = true;
  $("#rp-open").disabled = true;
  $("#rp-folder").disabled = true;
  rpBadge("生成中", "checking");
  $("#rp-status").textContent = "正在装配研报…";
  $("#rp-progress-wrap").style.display = "block";
  $("#rp-progress").className = "progress-bar indeterminate";

  try {
    const started = await bridge.start_report(
      bvid, $("#rp-out").value.trim(), Number($("#rp-limit").value) || 0,
      rpSections(), $("#rp-sync").checked);
    if (!started || started.ok !== true) throw new Error(started && started.error || "启动失败");

    const st = await pollTask(started.task_id, (s) => {
      if (s.progress) $("#rp-status").textContent = s.progress;
    });
    reportTask = null;
    $("#rp-progress-wrap").style.display = "none";
    $("#rp-generate").disabled = false;

    if (st.state === "done" && st.result && st.result.ok) {
      lastReportPath = st.result.path;
      rpBadge("已生成", "online");
      $("#rp-open").disabled = false;
      $("#rp-folder").disabled = false;
      $("#rp-status").textContent =
        `✓ 研报生成成功！单文件 ${st.result.size_kb} KB | 路径: ${st.result.path}`;
      log(`研报生成完成: ${st.result.path}`, "ok");
    } else {
      const msg = (st.result && st.result.error) || st.error || "未知错误";
      rpBadge("生成失败", "offline");
      $("#rp-status").textContent = "生成失败: " + msg;
      log("研报生成失败: " + msg, "err");
    }
  } catch (err) {
    reportTask = null;
    $("#rp-generate").disabled = false;
    $("#rp-progress-wrap").style.display = "none";
    rpBadge("生成失败", "offline");
    $("#rp-status").textContent = "生成失败: " + err;
  }
}

async function startBundle(fmt) {
  const bridge = api();
  if (!bridge) return;
  const bvid = $("#rp-bvid").value.trim();
  if (!bvid) { $("#rp-status").textContent = "请输入要打包导出的视频 BV 号"; return; }
  const ext = fmt === "sqlite" ? ".sqlite" : ".xlsx";
  const path = bvid + "_bundle" + ext;  // 相对路径，后端解析到工作目录

  rpBadge("导出中", "checking");
  $("#rp-status").textContent = `正在打包 ${bvid} 全部数据到 ${fmt.toUpperCase()} …`;
  $("#rp-progress-wrap").style.display = "block";
  $("#rp-progress").className = "progress-bar indeterminate";
  try {
    const started = await bridge.start_export(bvid, path, fmt);
    if (!started || started.ok !== true) throw new Error(started && started.error || "启动失败");
    const st = await pollTask(started.task_id, (s) => {
      if (s.progress) $("#rp-status").textContent = s.progress;
    });
    $("#rp-progress-wrap").style.display = "none";
    if (st.state === "done" && st.result && st.result.ok) {
      rpBadge("导出完成", "online");
      $("#rp-status").textContent =
        `✓ 已成功打包 ${fmt.toUpperCase()}: ${st.result.path}（${st.result.total_records.toLocaleString()} 条记录）`;
    } else {
      const msg = (st.result && st.result.error) || st.error || "未知错误";
      rpBadge("导出失败", "offline");
      $("#rp-status").textContent = "导出失败: " + msg;
    }
  } catch (err) {
    $("#rp-progress-wrap").style.display = "none";
    rpBadge("导出失败", "offline");
    $("#rp-status").textContent = "导出失败: " + err;
  }
}

$("#rp-generate").addEventListener("click", startReport);
$("#rp-sqlite").addEventListener("click", () => startBundle("sqlite"));
$("#rp-excel").addEventListener("click", () => startBundle("xlsx"));
$("#rp-all").addEventListener("click", () =>
  $$("#rp-sections input[data-sec]").forEach((el) => { el.checked = true; }));
$("#rp-none").addEventListener("click", () =>
  $$("#rp-sections input[data-sec]").forEach((el) => { el.checked = false; }));
$("#rp-open").addEventListener("click", () => {
  if (!lastReportPath) return;
  // WebView2 内直接打开本地研报文件
  window.open("file:///" + lastReportPath.replace(/\\/g, "/"), "_blank");
});

/* ── 启动 ── */
window.addEventListener("pywebviewready", () => {
  log("WebView2 引擎就绪 (GPU 渲染)", "ok");
  refreshConnection();
});

// pywebview 注入晚于 DOMContentLoaded 时由事件驱动；反之兜底拉一次
document.addEventListener("DOMContentLoaded", () => {
  showPage("connection");
  setTimeout(() => { if (api()) refreshConnection(); }, 800);
});
