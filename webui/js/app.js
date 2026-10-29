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
  if (name === "collect") { loadUpStats(); loadVideoOptions(); }
  if (name === "update") loadUpdateVideos();
  if (name === "user") setTimeout(() => $("#uq-uid").focus(), 50);
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

/* ═══════════ 采集页 ═══════════ */
let clMode = "video";
let clRunning = false;
let clCounts = { comments: 0, danmaku: 0 };
const STREAM_MAX = 1500;   // 与 Tk 版对齐：超过截掉头部，防长列表拖慢滚动

const MODE_LABELS = {
  video: "视频 BV / 链接", up: "UP主 UID", fav: "收藏夹用户 UID",
  rel: "关系用户 UID", post: "帖子链接 / ID",
};

$("#cl-modes").addEventListener("click", (e) => {
  const btn = e.target.closest(".pill");
  if (!btn) return;
  $$("#cl-modes .pill").forEach((p) => p.classList.toggle("active", p === btn));
  clMode = btn.dataset.mode;
  $("#cl-target-label").textContent = MODE_LABELS[clMode] || "目标";
  $("#cl-target").placeholder = clMode === "video"
    ? "BV1xx4y1z7Ab 或 https://b23.tv/xxxx"
    : (clMode === "post" ? "https://www.bilibili.com/read/cv123456" : "输入数字 UID");
});

function clBadge(text, cls) {
  const b = $("#cl-badge");
  b.textContent = text;
  b.className = "badge" + (cls ? " " + cls : "");
}

function fmtStreamLine(item) {
  const d = item.data || {};
  const kind = item.kind === "danmaku" ? "dmk" : "cmt";
  const tag = kind === "dmk" ? "弹幕" : "评论";
  const time = d.ctime || "";
  let meta = "";
  if (kind === "dmk") {
    const prog = d.progress != null ? `${Number(d.progress).toFixed(1)}s` : "-";
    const who = d.mid_hash ? `#${d.mid_hash}` : (d.uid || "-");
    meta = `进度 ${prog} · ${who}`;
  } else {
    const who = d.uid ? `uid ${d.uid}` : "-";
    const loc = d.ip_location ? ` · ${d.ip_location}` : "";
    meta = `赞 ${d.like_num ?? 0} · ${who}${loc}`;
  }
  return { tag, kind, time, meta, content: String(d.content || "").slice(0, 300) };
}

/* 后端批量推送入口（由 _CollectRunner.evaluate_js 调用） */
window.onCollectBatch = function (payload) {
  const box = $("#cl-stream");
  if (!payload) return;

  // 1. 流式内容：DocumentFragment 一次性上屏（关键性能点）
  const items = payload.items || [];
  if (items.length) {
    const empty = box.querySelector(".stream-empty");
    if (empty) empty.remove();
    const frag = document.createDocumentFragment();
    for (const it of items) {
      const L = fmtStreamLine(it);
      const div = document.createElement("div");
      div.className = "stream-line";
      const spanTag = document.createElement("span");
      spanTag.className = "k-" + L.kind;
      spanTag.textContent = `[${L.tag}]`;
      const spanTime = document.createElement("span");
      spanTime.className = "t";
      spanTime.textContent = ` ${L.time}`;
      const spanMeta = document.createElement("span");
      spanMeta.className = "meta";
      spanMeta.textContent = ` ${L.meta} | `;
      div.append(spanTag, spanTime, spanMeta, document.createTextNode(L.content));
      frag.appendChild(div);
    }
    box.appendChild(frag);
    // 上限截断：移除头部多余节点，保持 DOM 轻量
    const overflow = box.children.length - STREAM_MAX;
    if (overflow > 0) {
      for (let i = 0; i < overflow; i++) box.removeChild(box.firstElementChild);
    }
    // 贴底跟随（用户上翻后不强制跳）
    const nearBottom = box.scrollHeight - box.scrollTop - box.clientHeight < 80;
    if (nearBottom) box.scrollTop = box.scrollHeight;
  }

  // 2. 计数更新
  if (payload.stats) {
    clCounts = payload.stats;
    $("#cl-cnt-cmt").textContent = (clCounts.comments || 0).toLocaleString();
    $("#cl-cnt-dmk").textContent = (clCounts.danmaku || 0).toLocaleString();
  }

  // 3. 事件消息（进度/元信息/错误/完成）
  for (const ev of payload.events || []) {
    handleCollectEvent(ev);
  }
};

function handleCollectEvent(ev) {
  const t = ev.type;
  const status = $("#cl-stream-status");
  if (t === "progress") {
    if (ev.text) status.textContent = ev.text;
  } else if (t === "meta") {
    status.textContent = `采集中：${ev.bvid || ""} ${ev.title || ""}`.trim();
  } else if (t === "error") {
    appendStreamLine("err", "✗ " + (ev.text || "采集失败"));
    status.textContent = "采集失败";
  } else if (t === "done") {
    const s = (ev.stats || {});
    appendStreamLine("ok",
      `✓ 采集完成：评论 ${s.comments || 0} 条，弹幕 ${s.danmaku || 0} 条`);
    clBadge("已完成", "online");
    setClRunning(false);
  } else if (t === "runner_done") {
    setClRunning(false);
  }
}

function appendStreamLine(cls, text) {
  const box = $("#cl-stream");
  const empty = box.querySelector(".stream-empty");
  if (empty) empty.remove();
  const div = document.createElement("div");
  div.className = "stream-line " + cls;
  div.textContent = text;
  box.appendChild(div);
  box.scrollTop = box.scrollHeight;
}

function setClRunning(on) {
  clRunning = on;
  $("#cl-start").disabled = on;
  $("#cl-stop").disabled = !on;
  $("#cl-start").textContent = on ? "采集中…" : "开始采集";
}

async function startCollect() {
  const bridge = api();
  if (!bridge || clRunning) return;
  const target = $("#cl-target").value.trim();
  if (!target) { $("#cl-stream-status").textContent = "请输入目标"; return; }

  $("#cl-stream").innerHTML = '<div class="stream-empty">正在启动…</div>';
  clCounts = { comments: 0, danmaku: 0 };
  $("#cl-cnt-cmt").textContent = "0";
  $("#cl-cnt-dmk").textContent = "0";
  clBadge("采集中", "checking");
  setClRunning(true);

  try {
    let res = await bridge.start_collect(
      target, clMode, $("#cl-comments").checked, $("#cl-danmaku").checked);

    // 短链需要先解析
    if (res && res.need_resolve) {
      $("#cl-stream-status").textContent = "正在解析短链…";
      const r2 = await bridge.resolve_link(res.link);
      if (!r2 || !r2.ok) throw new Error((r2 && r2.error) || "短链解析失败");
      res = await bridge.start_collect(
        r2.bvid, clMode, $("#cl-comments").checked, $("#cl-danmaku").checked);
    }

    if (!res || res.ok !== true) throw new Error((res && res.error) || "启动失败");
    $("#cl-stream-status").textContent = `采集中：${res.target || target}`;
  } catch (err) {
    clBadge("失败", "offline");
    $("#cl-stream-status").textContent = "启动失败: " + err;
    appendStreamLine("err", "✗ " + err);
    setClRunning(false);
  }
}

async function stopCollect() {
  const bridge = api();
  if (!bridge) return;
  $("#cl-stream-status").textContent = "正在停止…";
  try {
    await bridge.stop_collect();
    appendStreamLine("progress", "已请求停止（已采数据仍会落库）");
  } catch (e) {
    $("#cl-stream-status").textContent = "停止失败: " + e;
  }
}

$("#cl-start").addEventListener("click", startCollect);
$("#cl-stop").addEventListener("click", stopCollect);
$("#cl-target").addEventListener("keydown", (e) => { if (e.key === "Enter") startCollect(); });

/* ── 已采集统计与视频列表 ── */
async function loadUpStats(filter = "") {
  const bridge = api();
  if (!bridge) return;
  try {
    const res = await bridge.get_up_stats(filter);
    const body = $("#cl-up-body");
    body.innerHTML = "";
    if (!res || !res.ok) return;
    const frag = document.createDocumentFragment();
    for (const r of res.rows || []) {
      const tr = document.createElement("tr");
      [r.uid, r.total, r.fetched, r.comment_total].forEach((v) => {
        const td = document.createElement("td");
        td.textContent = v;
        tr.appendChild(td);
      });
      frag.appendChild(tr);
    }
    body.appendChild(frag);
  } catch (e) { /* 静默：统计表非关键路径 */ }
}

async function loadVideoOptions() {
  const bridge = api();
  if (!bridge) return;
  try {
    const res = await bridge.get_video_options();
    if (!res || !res.ok) return;
    const sel = $("#cl-videos");
    sel.innerHTML = "";
    for (const it of res.items || []) {
      const opt = document.createElement("option");
      opt.value = it.bvid;
      opt.textContent = it.label;
      sel.appendChild(opt);
    }
    $("#cl-total").textContent = `共 ${res.total || 0} 个已采集视频`;
  } catch (e) { /* 静默 */ }
}

$("#cl-filter").addEventListener("click", () => loadUpStats($("#cl-uid-filter").value.trim()));
$("#cl-filter-clear").addEventListener("click", () => {
  $("#cl-uid-filter").value = "";
  loadUpStats();
});

$("#cl-view-stored").addEventListener("click", async () => {
  const bridge = api();
  const bvid = $("#cl-videos").value;
  if (!bridge || !bvid) return;
  $("#cl-stream-status").textContent = `正在读取 ${bvid} 库内数据…`;
  try {
    const res = await bridge.get_stored_content(bvid, 200);
    if (!res || !res.ok) throw new Error((res && res.error) || "读取失败");
    $("#cl-stream").innerHTML = "";
    appendStreamLine("ok",
      `==== ${bvid} ${res.title || ""} | 评论 ${res.comments.length} 条 / 弹幕 ${res.danmaku.length} 条 ====`);
    for (const c of res.comments) {
      appendStreamLine("", `[评论] ${c.ctime || ""} 赞${c.like_num ?? 0} uid${c.uid ?? "-"} | ${c.content || ""}`);
    }
    for (const d of res.danmaku) {
      appendStreamLine("", `[弹幕] ${d.ctime || ""} ${Number(d.progress || 0).toFixed(1)}s | ${d.content || ""}`);
    }
    $("#cl-stream-status").textContent = `已显示 ${bvid} 的库内数据`;
  } catch (err) {
    appendStreamLine("err", "✗ " + err);
    $("#cl-stream-status").textContent = "读取失败: " + err;
  }
});

/* ═══════════ 增量轮询页 ═══════════ */
let upRunning = false;
let upTimer = null;

function upBadge(text, cls) {
  const b = $("#up-badge");
  b.textContent = text;
  b.className = "badge" + (cls ? " " + cls : "");
}

function upLog(text, cls = "") {
  const box = $("#up-log");
  const empty = box.querySelector(".stream-empty");
  if (empty) empty.remove();
  const div = document.createElement("div");
  div.className = "stream-line " + cls;
  div.textContent = text;
  box.appendChild(div);
  while (box.children.length > 500) box.removeChild(box.firstElementChild);
  box.scrollTop = box.scrollHeight;
}

/* 后端增量任务推送入口 */
window.onIncrementLog = function (ev) {
  if (!ev) return;
  if (ev.type === "log") {
    upLog(ev.text || "");
  } else if (ev.type === "error") {
    upLog("✗ " + (ev.text || "失败"), "err");
    upBadge("失败", "offline");
    setUpRunning(false);
  } else if (ev.type === "finished") {
    const r = ev.result || {};
    upLog("✓ " + (r.msg || "任务完成"), "ok");
    upBadge("已完成", "online");
    setUpRunning(false);
    loadUpdateVideos();
  } else if (ev.type === "runner_done") {
    setUpRunning(false);
  }
};

function setUpRunning(on) {
  upRunning = on;
  $("#up-stop").disabled = !on;
  $("#up-progress-wrap").style.display = on ? "block" : "none";
  $("#up-once").disabled = on;
  $("#up-ups").disabled = on;
}

async function runIncrement(mode) {
  const bridge = api();
  if (!bridge || upRunning) return;
  const pages = Number($("#up-maxpages").value) || 100;
  upBadge("运行中", "checking");
  $("#up-state").textContent = mode === "ups" ? "正在批量更新博主…" : "正在增量补采…";
  setUpRunning(true);
  try {
    const res = await bridge.start_increment(mode, pages);
    if (!res || res.ok !== true) throw new Error((res && res.error) || "启动失败");
  } catch (err) {
    upBadge("失败", "offline");
    $("#up-state").textContent = "启动失败: " + err;
    upLog("✗ " + err, "err");
    setUpRunning(false);
  }
}

function togglePolling() {
  if (upRunning) {
    stopIncrement();
    return;
  }
  const mins = Math.max(1, Number($("#up-interval").value) || 30);
  upBadge("轮询中", "checking");
  $("#up-toggle").textContent = "停止轮询";
  $("#up-state").textContent = `每 ${mins} 分钟自动补采一次`;
  $("#up-next").textContent = `下次: ${new Date(Date.now() + mins * 60000).toLocaleTimeString()}`;
  runIncrement("all");
  upTimer = setInterval(() => {
    if (!upRunning) { runIncrement("all"); }
    $("#up-next").textContent =
      `下次: ${new Date(Date.now() + mins * 60000).toLocaleTimeString()}`;
  }, mins * 60000);
}

function stopPolling() {
  if (upTimer) { clearInterval(upTimer); upTimer = null; }
  $("#up-toggle").textContent = "开始轮询";
  $("#up-next").textContent = "";
  stopIncrement();
}

async function stopIncrement() {
  const bridge = api();
  if (!bridge) return;
  $("#up-state").textContent = "正在停止…";
  try {
    await bridge.stop_increment();
    upLog("已请求停止（已采数据仍会落库）");
  } catch (e) { /* 忽略 */ }
}

async function loadUpdateVideos() {
  const bridge = api();
  if (!bridge) return;
  try {
    const res = await bridge.get_update_videos();
    const body = $("#up-videos");
    body.innerHTML = "";
    if (!res || !res.ok) return;
    $("#up-total").textContent = `共 ${res.total || 0} 个已采集视频`;
    const frag = document.createDocumentFragment();
    for (const r of res.rows || []) {
      const tr = document.createElement("tr");
      [r.bvid, r.title, r.comment_count, r.danmaku_count, r.flag].forEach((v) => {
        const td = document.createElement("td");
        td.textContent = v;
        tr.appendChild(td);
      });
      frag.appendChild(tr);
    }
    body.appendChild(frag);
  } catch (e) { /* 静默 */ }
}

$("#up-toggle").addEventListener("click", togglePolling);
$("#up-once").addEventListener("click", () => runIncrement("all"));
$("#up-ups").addEventListener("click", () => runIncrement("ups"));
$("#up-stop").addEventListener("click", () => { stopPolling(); });
$("#up-refresh").addEventListener("click", loadUpdateVideos);

/* ═══════════ 用户查询页 ═══════════ */
let uqBusy = false;

function uqBadge(text, cls) {
  const b = $("#uq-badge");
  b.textContent = text;
  b.className = "badge" + (cls ? " " + cls : "");
}

async function queryUser() {
  const bridge = api();
  if (!bridge || uqBusy) return;
  const raw = $("#uq-uid").value.trim();
  if (!/^\d+$/.test(raw)) {
    uqBadge("UID 需为纯数字", "offline");
    return;
  }
  uqBusy = true;
  uqBadge("查询中", "checking");
  $("#uq-empty").style.display = "none";
  try {
    const res = await bridge.query_user(Number(raw), Number($("#uq-limit").value) || 500);
    if (!res || res.ok !== true) throw new Error((res && res.error) || "查询失败");

    // 画像卡
    const card = $("#uq-profile-card");
    if (res.profile) {
      card.style.display = "block";
      $("#uq-title").textContent = `${res.profile.uname} (UID: ${res.profile.uid})`;
      $("#uq-subtitle").textContent =
        `Lv.${res.profile.level} · ${res.profile.vip} · 性别: ${res.profile.sex} · 签名: ${res.profile.signature || "-"}`;
    } else {
      card.style.display = "block";
      $("#uq-title").textContent = `用户 UID: ${res.uid}`;
      $("#uq-subtitle").textContent = "未入库该用户的维度信息（仅展示发言历史）";
    }

    // 发言表
    const all = [...(res.comments || []), ...(res.danmaku || [])];
    $("#uq-count").textContent =
      `评论 ${(res.comments || []).length} 条，弹幕 ${(res.danmaku || []).length} 条`;
    const body = $("#uq-rows");
    body.innerHTML = "";
    if (!all.length) {
      $("#uq-empty").style.display = "block";
      $("#uq-empty").textContent = "该用户暂无入库发言记录";
      uqBadge("无记录", "muted");
      return;
    }
    const frag = document.createDocumentFragment();
    for (const r of all) {
      const tr = document.createElement("tr");
      const src = r.kind === "弹幕"
        ? `${r.bvid} ${Number(r.progress || 0).toFixed(1)}s ${r.title}`
        : `${r.bvid} 赞${r.like_num ?? 0} ${r.title}`;
      [r.kind, r.ctime, r.content, r.ip_location, src].forEach((v, i) => {
        const td = document.createElement("td");
        td.textContent = v;
        if (i === 2) td.className = "cell-content";
        tr.appendChild(td);
      });
      frag.appendChild(tr);
    }
    body.appendChild(frag);
    uqBadge("查询完成", "online");
  } catch (err) {
    uqBadge("查询失败", "offline");
    $("#uq-count").textContent = "查询失败: " + err;
  } finally {
    uqBusy = false;
  }
}

$("#uq-search").addEventListener("click", queryUser);
$("#uq-uid").addEventListener("keydown", (e) => { if (e.key === "Enter") queryUser(); });

/* ═══════════ 广泛采集页 ═══════════ */
$("#br-switch-tk")?.addEventListener("click", () => {
  alert("请关闭当前窗口后运行：\n\npython main.py gui --engine tk\n\n或在命令行直接使用：python main.py up <UID>");
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
