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
  if (name === "search") {
    setTimeout(() => $("#search-input").focus(), 50);
    loadHistory();
  }
  if (name === "collect") { loadUpStats(); loadVideoOptions(); }
  if (name === "update") loadUpdateVideos();
  if (name === "broad") { loadBroadUps(); loadDbSize(); }
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
async function refreshStats() {
  await loadStatsOverview(false);
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
  const limit = Math.max(1, Number($("#search-limit").value) || 100);

  searching = true;
  const btn = $("#search-btn");
  btn.disabled = true;
  btn.textContent = "检索中…";
  $("#search-status").textContent = "正在跨仓检索（E 库为 HDD，可能数秒）…";
  $("#result-body").innerHTML = "";
  $("#search-empty").style.display = "none";
  const t0 = performance.now();

  try {
    const res = await bridge.search(
      kw, kind, limit,
      $("#search-start").value || "",
      $("#search-end").value || "",
      $("#search-exclude").value.trim(),
      Number($("#search-minlike").value) || 0);
    const ms = Math.round(performance.now() - t0);
    if (!res || res.ok !== true) throw new Error(res && res.error || "检索失败");

    lastSearchRows = res.rows || [];
    lastSearchKind = kind;
    searchPage = 0;
    pushHistory(kw);
    $("#search-export").disabled = !lastSearchRows.length;

    renderResults(kind, pageRows());
    if (searchView === "text") renderTextView(kind, pageRows());
    updatePager();

    const failedNote = (res.failed && res.failed.length)
      ? `，${res.failed.length} 个仓不可达` : "";
    $("#search-status").textContent =
      `共 ${lastSearchRows.length.toLocaleString()} 条 · 耗时 ${ms}ms${failedNote}（缓存命中则近乎瞬时）`;
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
  // 关系模式显示方向/人数；批量模式显示多行 UID 区
  $("#cl-rel-row").style.display = clMode === "rel" ? "flex" : "none";
  $("#cl-batch-area").style.display = clMode === "batch" ? "block" : "none";
  $("#cl-target").style.display = clMode === "batch" ? "none" : "block";
  $("#cl-target-label").style.display = clMode === "batch" ? "none" : "block";
});

$("#cl-clear").addEventListener("click", () => {
  if (!confirm("清空实时流显示？（已采数据不受影响）")) return;
  $("#cl-stream").innerHTML = '<div class="stream-empty">实时内容将在此流式显示</div>';
  clCounts = { comments: 0, danmaku: 0 };
  $("#cl-cnt-cmt").textContent = "0";
  $("#cl-cnt-dmk").textContent = "0";
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

  // 批量模式：走批量 UID 流程
  if (clMode === "batch") {
    return startBatch();
  }

  const target = $("#cl-target").value.trim();
  if (!target) { $("#cl-stream-status").textContent = "请输入目标"; return; }

  $("#cl-stream").innerHTML = '<div class="stream-empty">正在启动…</div>';
  clCounts = { comments: 0, danmaku: 0 };
  $("#cl-cnt-cmt").textContent = "0";
  $("#cl-cnt-dmk").textContent = "0";
  clBadge("采集中", "checking");
  setClRunning(true);
  $("#cl-progress").className = "progress-bar indeterminate";

  try {
    let res = await bridge.start_collect(
      target, clMode, $("#cl-comments").checked, $("#cl-danmaku").checked,
      $("#cl-incremental").checked,
      $("#cl-dir").value, Number($("#cl-max-users").value) || 0);

    // 短链需要先解析
    if (res && res.need_resolve) {
      $("#cl-stream-status").textContent = "正在解析短链…";
      const r2 = await bridge.resolve_link(res.link);
      if (!r2 || !r2.ok) throw new Error((r2 && r2.error) || "短链解析失败");
      res = await bridge.start_collect(
        r2.bvid, "video", $("#cl-comments").checked, $("#cl-danmaku").checked,
        $("#cl-incremental").checked);
    }

    if (!res || res.ok !== true) throw new Error((res && res.error) || "启动失败");
    $("#cl-stream-status").textContent = `采集中：${res.target || target}`;
  } catch (err) {
    clBadge("失败", "offline");
    $("#cl-stream-status").textContent = "启动失败: " + err;
    appendStreamLine("err", "✗ " + err);
    setClRunning(false);
    $("#cl-progress").className = "progress-bar";
  }
}

/* 批量 UID 采集 */
async function startBatch(append) {
  const bridge = api();
  if (!bridge) return;
  const raw = $("#cl-batch-uids").value;
  const uids = String(raw).split(/[,\s\n;；]+/).map((s) => s.trim())
    .filter((s) => /^\d+$/.test(s)).map(Number);
  if (!uids.length) {
    $("#cl-stream-status").textContent = "请粘贴至少一个 UID";
    return;
  }
  if (!append) {
    $("#cl-stream").innerHTML = '<div class="stream-empty">正在启动批量…</div>';
    clCounts = { comments: 0, danmaku: 0 };
    $("#cl-cnt-cmt").textContent = "0";
    $("#cl-cnt-dmk").textContent = "0";
  }
  clBadge(append ? "追加中" : "批量中", "checking");
  setClRunning(true);
  try {
    const res = await bridge.start_batch(
      uids,
      $("#cl-comments").checked, $("#cl-danmaku").checked,
      $("#cl-ck-up").checked, $("#cl-ck-fav").checked, $("#cl-ck-rel").checked,
      $("#cl-rel-dir").value, append);
    if (!res || res.ok !== true) throw new Error((res && res.error) || "启动失败");
    $("#cl-stream-status").textContent =
      `${append ? "已追加" : "批量启动"}：${uids.length} 个 UID`;
    $("#cl-batch-add").disabled = false;
  } catch (err) {
    clBadge("失败", "offline");
    $("#cl-stream-status").textContent = "批量启动失败: " + err;
    setClRunning(false);
  }
}

$("#cl-batch-add").addEventListener("click", () => startBatch(true));

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
let uqLastRows = [];
let uqLastUid = 0;

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
    uqLastRows = all;
    uqLastUid = res.uid;
    $("#uq-export").disabled = !all.length;
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

$("#uq-export").addEventListener("click", async () => {
  const bridge = api();
  if (!bridge || !uqLastRows.length) return;
  uqBadge("导出中", "checking");
  try {
    const res = await bridge.export_user_rows(uqLastUid, uqLastRows);
    if (!res || !res.ok) throw new Error((res && res.error) || "导出失败");
    uqBadge("已导出", "online");
    $("#uq-count").textContent =
      `已导出 ${res.rows.toLocaleString()} 行 → ${res.path}`;
  } catch (err) {
    uqBadge("导出失败", "offline");
    $("#uq-count").textContent = "导出失败: " + err;
  }
});

/* ═══════════ 广泛采集页 ═══════════ */
let brRunning = false;
let brPaused = false;

function brBadge(text, cls) {
  const b = $("#br-badge");
  b.textContent = text;
  b.className = "badge" + (cls ? " " + cls : "");
}

function brLog(text, cls = "") {
  if ($("#br-pause-stream").checked) return;   // 暂停实时流
  const box = $("#br-stream");
  const empty = box.querySelector(".stream-empty");
  if (empty) empty.remove();
  const div = document.createElement("div");
  div.className = "stream-line " + cls;
  div.textContent = text;
  box.appendChild(div);
  while (box.children.length > 1500) box.removeChild(box.firstElementChild);
  const nearBottom = box.scrollHeight - box.scrollTop - box.clientHeight < 80;
  if (nearBottom) box.scrollTop = box.scrollHeight;
}

/* 后端广泛采集推送入口（与采集页同构，复用 _CollectRunner） */
window.onBroadBatch = function (payload) {
  if (!payload) return;
  const box = $("#br-stream");
  const items = payload.items || [];
  if (items.length && !$("#br-pause-stream").checked) {
    const empty = box.querySelector(".stream-empty");
    if (empty) empty.remove();
    const frag = document.createDocumentFragment();
    for (const it of items) {
      const d = it.data || {};
      const div = document.createElement("div");
      div.className = "stream-line";
      const tag = document.createElement("span");
      tag.className = "k-" + (it.kind === "danmaku" ? "dmk" : "cmt");
      tag.textContent = it.kind === "danmaku" ? "[弹幕]" : "[评论]";
      const meta = document.createElement("span");
      meta.className = "meta";
      const who = d.uid ? ` uid${d.uid}` : (d.mid_hash ? ` #${d.mid_hash}` : "");
      const loc = d.ip_location ? ` · ${d.ip_location}` : "";
      meta.textContent = ` ${d.ctime || ""}${who}${loc} | `;
      div.append(tag, meta, document.createTextNode(String(d.content || "").slice(0, 300)));
      frag.appendChild(div);
    }
    box.appendChild(frag);
    const overflow = box.children.length - 1500;
    for (let i = 0; i < overflow; i++) box.removeChild(box.firstElementChild);
    const nearBottom = box.scrollHeight - box.scrollTop - box.clientHeight < 80;
    if (nearBottom) box.scrollTop = box.scrollHeight;
  }

  for (const ev of payload.events || []) {
    const t = ev.type;
    if (t === "progress") {
      if (ev.text) $("#br-stream-status").textContent = ev.text;
      if (ev.vpos != null) $("#br-up-count").textContent = `${ev.vpos} 个`;
    } else if (t === "meta") {
      $("#br-stream-status").textContent = ev.title || "采集中…";
    } else if (t === "error") {
      brLog("✗ " + (ev.text || "失败"), "err");
      brBadge("失败", "offline");
    } else if (t === "done") {
      brLog("✓ 广泛采集完成", "ok");
      brBadge("已完成", "online");
      setBrRunning(false);
      loadBroadUps();
    } else if (t === "runner_done") {
      setBrRunning(false);
      loadBroadUps();
    }
  }
};

function setBrRunning(on) {
  brRunning = on;
  $("#br-start").disabled = on;
  $("#br-stop").disabled = !on;
  $("#br-pause").disabled = !on;
  $("#br-progress-wrap").style.display = on ? "block" : "none";
  $("#br-start").textContent = on ? "采集中…" : "开始广泛采集";
}

function parseSeeds(text) {
  return String(text || "").split(/[,\s\n;；]+/).map((s) => s.trim())
    .filter((s) => /^\d+$/.test(s)).map(Number);
}

function brParams() {
  const num = (id) => Number($(id).value) || 0;
  return {
    max_depth: num("#br-max_depth"), max_ups: num("#br-max_ups"),
    max_pages: num("#br-max_pages"), top_n: num("#br-top_n"),
    min_comments: num("#br-min_comments"),
    max_storage_mb: num("#br-max_storage_mb"),
    perpetual: $("#br-perpetual").checked,
  };
}

async function startBroad() {
  const bridge = api();
  if (!bridge || brRunning) return;
  const seeds = parseSeeds($("#br-seeds").value);
  if (!seeds.length) {
    $("#br-status").textContent = "请先输入至少一个种子 UID";
    return;
  }
  $("#br-stream").innerHTML = '<div class="stream-empty">正在启动…</div>';
  brBadge("采集中", "checking");
  $("#br-status").textContent = `种子 ${seeds.length} 个，正在启动 BFS 扩散…`;
  setBrRunning(true);
  try {
    const res = await bridge.start_broad(seeds, brParams());
    if (!res || res.ok !== true) throw new Error((res && res.error) || "启动失败");
    $("#br-status").textContent = `已启动：种子 ${res.seeds} 个`;
  } catch (err) {
    brBadge("失败", "offline");
    $("#br-status").textContent = "启动失败: " + err;
    setBrRunning(false);
  }
}

async function startHotCrawl() {
  const bridge = api();
  if (!bridge || brRunning) return;
  const n = Math.max(1, Math.min(100, Number($("#br-hot-count").value) || 100));
  brBadge("采集中", "checking");
  $("#br-status").textContent = `正在采集热搜前 ${n} 个视频的评论…`;
  setBrRunning(true);
  try {
    const res = await bridge.start_hot_crawl(n);
    if (!res || res.ok !== true) throw new Error((res && res.error) || "启动失败");
  } catch (err) {
    brBadge("失败", "offline");
    $("#br-status").textContent = "启动失败: " + err;
    setBrRunning(false);
  }
}

async function pauseBroad() {
  const bridge = api();
  if (!bridge) return;
  try {
    const res = await bridge.pause_broad();
    if (res && res.ok) {
      brPaused = !!res.paused;
      $("#br-pause").textContent = brPaused ? "继续" : "暂停";
      $("#br-status").textContent = brPaused ? "已暂停" : "继续采集中…";
    }
  } catch (e) { /* 忽略 */ }
}

async function stopBroad() {
  const bridge = api();
  if (!bridge) return;
  $("#br-status").textContent = "正在停止…";
  try {
    await bridge.stop_broad();
    brLog("已请求停止（已采数据仍会落库）");
  } catch (e) { /* 忽略 */ }
}

async function loadBroadUps() {
  const bridge = api();
  if (!bridge) return;
  try {
    const res = await bridge.get_up_stats("");
    const body = $("#br-up-rows");
    body.innerHTML = "";
    if (!res || !res.ok) return;
    const rows = (res.rows || []).slice(0, 200);
    $("#br-up-count").textContent = `${(res.rows || []).length} 个`;
    const frag = document.createDocumentFragment();
    for (const r of rows) {
      const tr = document.createElement("tr");
      [r.uid, r.total, r.fetched, r.comment_total].forEach((v) => {
        const td = document.createElement("td");
        td.textContent = v;
        tr.appendChild(td);
      });
      frag.appendChild(tr);
    }
    body.appendChild(frag);
  } catch (e) { /* 静默 */ }
}

async function loadDbSize() {
  const bridge = api();
  if (!bridge) return;
  try {
    const res = await bridge.get_db_size();
    $("#br-dbsize").textContent = res && res.ok
      ? `当前库占用：${res.mb} MB` : "库占用：未知（DB不可达）";
  } catch (e) { /* 静默 */ }
}

async function seedFrom(kind) {
  const bridge = api();
  if (!bridge) return;
  const n = Math.max(1, Math.min(100, Number($("#br-seed-count").value) || 10));
  $("#br-status").textContent = kind === "lib" ? "正在从库内精选 UP…" : "正在获取热搜 UP…";
  try {
    const res = kind === "lib"
      ? await bridge.get_library_seeds(n)
      : await bridge.get_hot_seeds(n);
    if (!res || !res.ok) throw new Error((res && res.error) || "获取失败");
    $("#br-seeds").value = (res.uids || []).join("\n");
    $("#br-status").textContent =
      `已载入 ${res.uids.length} 个种子，点击「开始广泛采集」启动`;
  } catch (err) {
    $("#br-status").textContent = "获取种子失败: " + err;
  }
}

$("#br-start").addEventListener("click", startBroad);
$("#br-hot").addEventListener("click", startHotCrawl);
$("#br-pause").addEventListener("click", pauseBroad);
$("#br-stop").addEventListener("click", stopBroad);
$("#br-seed-lib").addEventListener("click", () => seedFrom("lib"));
$("#br-seed-hot").addEventListener("click", () => seedFrom("hot"));
$("#br-clear").addEventListener("click", () => {
  $("#br-stream").innerHTML = '<div class="stream-empty">实时内容将在此显示</div>';
  $("#br-up-rows").innerHTML = "";
});

/* ═══════════ 连接页运维操作 ═══════════ */
let arcRunning = false;

function arcBadge(text, cls) {
  const b = $("#arc-badge");
  b.textContent = text;
  b.className = "badge" + (cls ? " " + cls : "");
}

function arcTip(text) { $("#arc-tip").textContent = text; }

/* 归档进度推送入口 */
window.onArchiveLog = function (ev) {
  if (!ev) return;
  const t = ev.type;
  if (t === "precheck") {
    const d = ev.data || {};
    const c = d.counts || {};
    log(`预检 D: 评论 ${(c.raw_comments || 0).toLocaleString()} / ` +
        `弹幕 ${(c.raw_danmaku || 0).toLocaleString()} / ` +
        `视频 ${(c.meta_video || 0).toLocaleString()} / ` +
        `用户 ${(c.dim_user || 0).toLocaleString()}`);
    if (d.count_err) log("预检：D 表统计失败 " + d.count_err, "err");
    if (d.total_est < d.threshold) {
      log(`预检提示: 当前 D 库评论 ${(d.total_est || 0).toLocaleString()} 行 ` +
          `< 门槛 ${(d.threshold || 0).toLocaleString()} 行，若无断点将被拒绝`, "err");
    }
    $("#arc-progress-wrap").style.display = "block";
    $("#arc-progress").className = "progress-bar indeterminate";
  } else if (t === "log") {
    const text = ev.text || "";
    log(text);
    arcTip(text);
    // 解析清D百分比 [CLEARPCT nn]
    const m = /\[CLEARPCT\s*(\d+)\]/.exec(text);
    if (m) {
      const pct = Math.max(0, Math.min(100, parseInt(m[1], 10)));
      $("#arc-progress").className = "progress-bar";
      $("#arc-progress").style.width = pct + "%";
    }
  } else if (t === "error") {
    log("✗ " + (ev.text || "归档失败"), "err");
    arcBadge("归档失败", "offline");
    arcTip("归档失败: " + (ev.text || ""));
    setArcRunning(false);
  } else if (t === "finished") {
    const r = ev.result || {};
    if (r.ok) {
      log(r.msg || "归档完成", "ok");
      arcBadge("已归档", "online");
      arcTip(r.msg || "归档完成");
      $("#arc-progress").className = "progress-bar";
      $("#arc-progress").style.width = "100%";
    } else {
      log(r.msg || r.error || "归档未完成", "err");
      arcBadge("未完成", "offline");
      arcTip(r.msg || r.error || "归档未完成");
    }
    setArcRunning(false);
    refreshConnection();
  } else if (t === "runner_done") {
    setArcRunning(false);
  }
};

function setArcRunning(on) {
  arcRunning = on;
  $("#arc-start").disabled = on;
  $("#arc-cancel").disabled = !on;
  $("#arc-start").textContent = on ? "归档中…" : "手动归档 D → E";
  if (!on) {
    setTimeout(() => {
      $("#arc-progress-wrap").style.display = "none";
      $("#arc-progress").style.width = "0%";
    }, 1200);
  }
}

async function startArchive() {
  const bridge = api();
  if (!bridge || arcRunning) return;
  arcBadge("归档中", "checking");
  arcTip("正在计算分仓行数与门槛…");
  setArcRunning(true);
  try {
    const res = await bridge.start_archive();
    if (!res || res.ok !== true) throw new Error((res && res.error) || "启动失败");
  } catch (err) {
    arcBadge("归档失败", "offline");
    arcTip("启动失败: " + err);
    log("✗ 归档启动失败: " + err, "err");
    setArcRunning(false);
  }
}

async function cancelArchive() {
  const bridge = api();
  if (!bridge) return;
  arcTip("已请求取消归档…");
  try {
    await bridge.stop_archive();
    log("已请求取消归档（逐批路径可随时中断）");
  } catch (e) { /* 忽略 */ }
}

async function serviceAction(role, act) {
  const bridge = api();
  if (!bridge) return;
  const name = role === "write" ? "D" : "E";
  if (act === "stop" && !confirm(`确定断开 ${name} 库？\n\n采集/查询将暂停。`)) return;
  arcTip(`正在申请管理员权限${act === "start" ? "启动" : "停止"} MySQL（${name}）…`);
  try {
    const res = act === "start"
      ? await bridge.start_service(role)
      : await bridge.stop_service(role);
    if (res && res.ok) {
      arcTip(res.msg || "已发起操作");
      log(res.msg || "已发起操作", "ok");
      setTimeout(refreshConnection, 3500);
    } else {
      arcTip((res && res.error) || "操作失败");
      log("✗ " + ((res && res.error) || "操作失败"), "err");
    }
  } catch (e) {
    arcTip("操作失败: " + e);
  }
}

async function killStuck() {
  const bridge = api();
  if (!bridge) return;
  arcTip("正在排查卡死查询…");
  try {
    const res = await bridge.kill_stuck();
    if (res && res.ok) {
      arcTip(res.msg);
      log(res.msg, "ok");
    } else {
      arcTip((res && res.error) || "清理失败");
      log("✗ " + ((res && res.error) || "清理失败"), "err");
    }
  } catch (e) {
    arcTip("清理失败: " + e);
  }
}

async function showDbConfig() {
  const bridge = api();
  if (!bridge) return;
  try {
    const res = await bridge.get_db_config();
    if (!res || !res.ok) throw new Error((res && res.error) || "读取失败");
    const w = res.write, r = res.read;
    alert(
      `当前数据库连接配置\n\n` +
      `【写库 D】\n${w.user}@${w.host}:${w.port}/${w.database}\n\n` +
      `【归档库 E】\n${r.user}@${r.host}:${r.port}/${r.database}\n\n` +
      `如需修改，请使用经典 Tk 引擎的「数据库连接配置」对话框：\n` +
      `python main.py gui --engine tk`);
  } catch (e) {
    alert("读取配置失败: " + e);
  }
}

document.querySelectorAll("[data-svc]").forEach((btn) => {
  btn.addEventListener("click", () =>
    serviceAction(btn.dataset.svc, btn.dataset.act));
});
$("#arc-start").addEventListener("click", startArchive);
$("#arc-cancel").addEventListener("click", cancelArchive);
$("#arc-kill").addEventListener("click", killStuck);
$("#arc-config").addEventListener("click", showDbConfig);
$("#arc-refresh").addEventListener("click", refreshConnection);

/* ═══════════ 统计页扩展 ═══════════ */
const STAT_LABELS_FULL = {
  videos: ["视频数", "个"], comments: ["总评论", "条"],
  danmaku: ["总弹幕", "条"], users: ["用户数", "人"],
  video_with_comments: ["有评论视频", "个"], danmaku_parsed: ["弹幕已解析", "条"],
  danmaku_unparsed: ["弹幕未解析", "条"], crc_hits: ["UID反查命中", "次"],
  comment_uids: ["评论独立UID", "个"], crc_total: ["反查总量", "条"],
};

/* 缓存过期阈值：超过这个天数就提醒用户刷新 */
const STALE_DAYS = 3;

function showStaleBanner(cacheTime, ageDays) {
  const banner = $("#st-stale");
  if (ageDays === null || ageDays === undefined) {
    banner.style.display = "none";
    return;
  }
  if (ageDays < STALE_DAYS) {
    banner.style.display = "none";
    return;
  }
  const ageText = ageDays === 0 ? "今天" : `${ageDays} 天前`;
  $("#st-stale-text").textContent =
    `数据更新于${ageText}${cacheTime ? `（${cacheTime}）` : ""}，` +
    `归档新分仓后统计不会自动更新，看到的可能不是最新总量。`;
  banner.style.display = "flex";
}

async function loadStatsOverview(refresh) {
  const bridge = api();
  if (!bridge) return;
  const grid = $("#stats-grid");
  if (refresh) {
    grid.innerHTML = '<div class="stat-card"><div class="stat-label">全表聚合中…</div></div>';
    $("#st-updated").textContent = "正在统计（大库可能较久）";
    $("#st-stale").style.display = "none";
  }
  try {
    const res = await bridge.get_stats_overview(!!refresh);
    const stats = res && res.stats;
    if (!stats) throw new Error((res && res.error) || "无数据");
    grid.innerHTML = "";
    const frag = document.createDocumentFragment();
    for (const [key, [label, unit]] of Object.entries(STAT_LABELS_FULL)) {
      if (!(key in stats)) continue;
      const card = document.createElement("div");
      card.className = "stat-card";
      const v = Number(stats[key]) || 0;
      card.innerHTML = `<div class="stat-label">${label}</div>` +
        `<div class="stat-value">${v.toLocaleString()}</div>` +
        `<div class="stat-sub">${unit}</div>`;
      frag.appendChild(card);
    }
    grid.appendChild(frag);

    if (res.cached) {
      const age = res.cache_age_days;
      const ageText = age === null || age === undefined ? "未知时间"
        : (age === 0 ? "今天" : `${age} 天前`);
      $("#st-updated").textContent = `缓存数据 · 更新于${ageText}`;
      showStaleBanner(res.cache_time, age);
    } else {
      $("#st-updated").textContent = "刚刚统计完成";
      $("#st-stale").style.display = "none";
    }
  } catch (err) {
    grid.innerHTML = `<div class="stat-card"><div class="stat-label">读取失败</div>` +
      `<div class="stat-sub">${escapeHtml(String(err))}</div></div>`;
    $("#st-stale").style.display = "none";
  }
}

/* 排行榜：长任务轮询 */
async function loadRankings() {
  const bridge = api();
  if (!bridge) return;
  const topN = Math.max(5, Math.min(200, Number($("#rk-topn").value) || 20));
  $("#rk-status").textContent = "正在统计排行榜（大库可能数十秒）…";
  $("#rk-refresh").disabled = true;
  try {
    const started = await bridge.get_rankings(topN);
    if (!started || !started.ok) throw new Error((started && started.error) || "启动失败");
    const st = await pollTask(started.task_id, (s) => {
      if (s.progress) $("#rk-status").textContent = s.progress;
    });
    if (st.state !== "done" || !st.result || !st.result.ok) {
      throw new Error((st.result && st.result.error) || st.error || "统计失败");
    }
    renderRankings(st.result.rankings);
    const total = Object.values(st.result.rankings || {})
      .reduce((n, b) => n + (b.rows || []).length, 0);
    $("#rk-status").textContent = `已统计 ${Object.keys(st.result.rankings || {}).length} 个榜单，共 ${total} 行`;
  } catch (err) {
    $("#rk-status").textContent = "统计失败: " + err;
  } finally {
    $("#rk-refresh").disabled = false;
  }
}

function renderRankings(rankings) {
  const grid = $("#rank-grid");
  grid.innerHTML = "";
  for (const [key, block] of Object.entries(rankings || {})) {
    const wrap = document.createElement("div");
    wrap.className = "rank-block";
    const cols = block.columns || [];
    const head = `<h4><span>${escapeHtml(block.title || key)}</span>` +
      `<span>${(block.rows || []).length} 行</span></h4>`;
    const rowsHtml = (block.rows || []).map((r) => {
      const cells = cols.map((c, i) => {
        const v = r[c.key];
        const text = typeof v === "number" ? v.toLocaleString() : String(v ?? "");
        return `<td class="${i > 0 ? "num" : ""}">${escapeHtml(text)}</td>`;
      }).join("");
      return `<tr>${cells}</tr>`;
    }).join("");
    const table = `<div class="rank-body"><table>` +
      `<thead><tr>${cols.map((c, i) =>
        `<th style="text-align:${i > 0 ? "right" : "left"}">${escapeHtml(c.label)}</th>`).join("")}</tr></thead>` +
      `<tbody>${rowsHtml || '<tr><td colspan="9" style="text-align:center;color:#8c877d">暂无数据</td></tr>'}</tbody>` +
      `</table></div>`;
    wrap.innerHTML = head + table;
    grid.appendChild(wrap);
  }
}

/* 趋势：纯 CSS 柱状图（GPU 友好，无需 matplotlib） */
async function loadTrends() {
  const bridge = api();
  if (!bridge) return;
  const days = Math.max(1, Math.min(365, Number($("#tr-days").value) || 30));
  $("#tr-status").textContent = "正在统计（HDD 大库可能较久）…";
  $("#tr-refresh").disabled = true;
  try {
    const started = await bridge.get_trends(days);
    if (!started || !started.ok) throw new Error((started && started.error) || "启动失败");
    const st = await pollTask(started.task_id, (s) => {
      if (s.progress) $("#tr-status").textContent = s.progress;
    });
    if (st.state !== "done" || !st.result || !st.result.ok) {
      throw new Error((st.result && st.result.error) || st.error || "加载失败");
    }
    const res = st.result;
    const wrap = $("#trend-wrap");
    wrap.innerHTML = "";

    const daily = res.daily || [];
    if (daily.length) {
      const maxV = Math.max(1, ...daily.map((d) => Number(d.cnt || d.count || 0)));
      const chart = document.createElement("div");
      chart.className = "bar-chart";
      const labels = document.createElement("div");
      labels.className = "bar-labels";
      for (const d of daily) {
        const v = Number(d.cnt || d.count || 0);
        const bar = document.createElement("div");
        bar.className = "bar";
        bar.style.height = Math.max(2, (v / maxV) * 100) + "%";
        bar.title = `${d.day || d.date || ""}: ${v.toLocaleString()}`;
        chart.appendChild(bar);
        const lb = document.createElement("span");
        lb.textContent = String(d.day || d.date || "").slice(5);
        labels.appendChild(lb);
      }
      const t1 = document.createElement("div");
      t1.innerHTML = `<div class="card-sub">每日评论量（近 ${daily.length} 天，峰值 ${maxV.toLocaleString()}）</div>`;
      wrap.append(t1, chart, labels);
    }

    const hourly = res.hourly || [];
    if (hourly.length) {
      const maxH = Math.max(1, ...hourly.map((h) => Number(h.cnt || h.count || 0)));
      const chart2 = document.createElement("div");
      chart2.className = "bar-chart";
      const labels2 = document.createElement("div");
      labels2.className = "bar-labels";
      for (const h of hourly) {
        const v = Number(h.cnt || h.count || 0);
        const bar = document.createElement("div");
        bar.className = "bar";
        bar.style.height = Math.max(2, (v / maxH) * 100) + "%";
        bar.title = `${h.hour ?? ""}时: ${v.toLocaleString()}`;
        chart2.appendChild(bar);
        const lb = document.createElement("span");
        lb.textContent = String(h.hour ?? "");
        labels2.appendChild(lb);
      }
      const t2 = document.createElement("div");
      t2.innerHTML = `<div class="card-sub">24 小时活跃分布</div>`;
      wrap.append(t2, chart2, labels2);
    }
    if (!daily.length && !hourly.length) {
      wrap.innerHTML = '<div class="empty-hint">暂无趋势数据</div>';
    }
    $("#tr-status").textContent = `已加载 ${daily.length} 天趋势 / ${hourly.length} 个小时段`;
  } catch (err) {
    $("#tr-status").textContent = "加载失败: " + err;
  } finally {
    $("#tr-refresh").disabled = false;
  }
}

/* 洞察：高频词 + 情感 */
async function loadInsights() {
  const bridge = api();
  if (!bridge) return;
  $("#ins-status").textContent = "正在抽样并计算…";
  $("#ins-refresh").disabled = true;
  try {
    const started = await bridge.get_insights(5000, 20);
    if (!started || !started.ok) throw new Error((started && started.error) || "启动失败");
    const st = await pollTask(started.task_id);
    if (st.state !== "done" || !st.result || !st.result.ok) {
      throw new Error((st.result && st.result.error) || st.error || "计算失败");
    }
    const ins = st.result.insights || {};
    const body = $("#ins-body");
    body.innerHTML = "";

    const senti = ins.sentiment || ins.sentiments || {};
    if (Object.keys(senti).length) {
      const row = document.createElement("div");
      row.className = "senti-row";
      const map = [["pos", "正面", "senti-pos"], ["neu", "中立", "senti-neu"],
                   ["neg", "负面", "senti-neg"]];
      for (const [k, label, cls] of map) {
        const v = Number(senti[k] || senti[label] || 0);
        const div = document.createElement("div");
        div.className = "senti-item " + cls;
        div.innerHTML = `<div class="n">${v.toLocaleString()}</div><div class="l">${label}</div>`;
        row.appendChild(div);
      }
      body.appendChild(row);
    }

    const terms = ins.terms || [];
    if (terms.length) {
      const t = document.createElement("div");
      t.innerHTML = '<div class="card-sub" style="margin:10px 0 6px">高频词 Top</div>';
      const wall = document.createElement("div");
      wall.className = "term-wall";
      for (const item of terms.slice(0, 24)) {
        const chip = document.createElement("span");
        chip.className = "term-chip";
        chip.innerHTML = `${escapeHtml(String(item.term || item.word || ""))} <b>${item.cnt || item.count || 0}</b>`;
        wall.appendChild(chip);
      }
      body.append(t, wall);
    }
    if (!body.children.length) {
      body.innerHTML = '<div class="empty-hint">样本不足，暂无洞察结果</div>';
    }
    $("#ins-status").textContent = st.result.cached
      ? "来自缓存" : `样本 ${ins.total || "-"} 条，计算完成`;
  } catch (err) {
    $("#ins-status").textContent = "计算失败: " + err;
  } finally {
    $("#ins-refresh").disabled = false;
  }
}

/* 水军检测 */
async function runDetect() {
  const bridge = api();
  if (!bridge) return;
  const bvid = $("#dt-bvid").value.trim();
  const limit = Math.max(100, Number($("#dt-limit").value) || 5000);
  $("#dt-status").textContent = "正在分析复读率/爆发/属地聚集…";
  $("#dt-run").disabled = true;
  try {
    const started = await bridge.run_detect(bvid, limit, 50);
    if (!started || !started.ok) throw new Error((started && started.error) || "启动失败");
    const st = await pollTask(started.task_id);
    if (st.state !== "done" || !st.result || !st.result.ok) {
      throw new Error((st.result && st.result.error) || st.error || "检测失败");
    }
    const results = st.result.results || [];
    $("#dt-summary").textContent = st.result.summary || "";
    const body = $("#dt-rows");
    body.innerHTML = "";
    const frag = document.createDocumentFragment();
    for (const r of results) {
      const tr = document.createElement("tr");
      const level = { high: "高危", suspect: "存疑" }[r.level] || "正常";
      const cells = [
        r.uid, r.uname || "", r.total, r.repeat_rate, r.burst_max,
        `${r.ip_top || "-"} ${r.ip_ratio || 0}`, r.score, level,
        (r.hits || []).join("+") || "-",
      ];
      cells.forEach((v) => {
        const td = document.createElement("td");
        td.textContent = v;
        tr.appendChild(td);
      });
      if (r.level === "high") tr.style.background = "#fdf0ef";
      else if (r.level === "suspect") tr.style.background = "#fff8e6";
      frag.appendChild(tr);
    }
    body.appendChild(frag);
    $("#dt-status").textContent =
      `检测完成：${results.length} 个可疑账号（样本 ${st.result.total || 0} 条）`;
  } catch (err) {
    $("#dt-status").textContent = "检测失败: " + err;
  } finally {
    $("#dt-run").disabled = false;
  }
}

$("#st-refresh").addEventListener("click", () => loadStatsOverview(true));
$("#st-stale-refresh").addEventListener("click", () => loadStatsOverview(true));
$("#rk-refresh").addEventListener("click", loadRankings);
$("#tr-refresh").addEventListener("click", loadTrends);
$("#ins-refresh").addEventListener("click", loadInsights);
$("#dt-run").addEventListener("click", runDetect);

/* ═══════════ 搜索页扩展 ═══════════ */
let searchView = "table";
let lastSearchRows = [];
let lastSearchKind = "comments";
let searchPage = 0;
const SEARCH_PAGE_SIZE = 100;
const HISTORY_KEY = "bili-search-history";

function loadHistory() {
  try {
    const list = JSON.parse(localStorage.getItem(HISTORY_KEY) || "[]");
    const dl = $("#search-history");
    dl.innerHTML = "";
    for (const h of list.slice(0, 20)) {
      const opt = document.createElement("option");
      opt.value = h;
      dl.appendChild(opt);
    }
  } catch (e) { /* 忽略 */ }
}

function pushHistory(kw) {
  try {
    const list = JSON.parse(localStorage.getItem(HISTORY_KEY) || "[]");
    const next = [kw, ...list.filter((x) => x !== kw)].slice(0, 20);
    localStorage.setItem(HISTORY_KEY, JSON.stringify(next));
    loadHistory();
  } catch (e) { /* 忽略 */ }
}

$("#search-view").addEventListener("click", (e) => {
  const btn = e.target.closest(".pill");
  if (!btn) return;
  $$("#search-view .pill").forEach((p) => p.classList.toggle("active", p === btn));
  searchView = btn.dataset.view;
  const isText = searchView === "text";
  $("#search-table-wrap").style.display = isText ? "none" : "block";
  $("#search-text-wrap").style.display = isText ? "block" : "none";
  $("#search-pager").style.display = isText ? "none" : "flex";
  if (lastSearchRows.length) renderResults(lastSearchKind, pageRows());
});

function pageRows() {
  const start = searchPage * SEARCH_PAGE_SIZE;
  return lastSearchRows.slice(start, start + SEARCH_PAGE_SIZE);
}

function updatePager() {
  const pages = Math.max(1, Math.ceil(lastSearchRows.length / SEARCH_PAGE_SIZE));
  $("#search-pager").style.display = searchView === "text" ? "none" : "flex";
  $("#pg-info").textContent =
    `第 ${searchPage + 1}/${pages} 页 · 共 ${lastSearchRows.length.toLocaleString()} 条`;
  $("#pg-prev").disabled = searchPage <= 0;
  $("#pg-next").disabled = searchPage >= pages - 1;
}

$("#pg-prev").addEventListener("click", () => {
  if (searchPage > 0) {
    searchPage--;
    renderResults(lastSearchKind, pageRows());
    updatePager();
  }
});
$("#pg-next").addEventListener("click", () => {
  const pages = Math.ceil(lastSearchRows.length / SEARCH_PAGE_SIZE);
  if (searchPage < pages - 1) {
    searchPage++;
    renderResults(lastSearchKind, pageRows());
    updatePager();
  }
});

/* 全文视图：把当前页结果渲染为可读文本 */
function renderTextView(kind, rows) {
  const box = $("#search-text");
  box.innerHTML = "";
  if (!rows.length) {
    box.innerHTML = '<div class="stream-empty">暂无匹配内容</div>';
    return;
  }
  const frag = document.createDocumentFragment();
  for (const r of rows) {
    const d = fmtRow(kind, r);
    const div = document.createElement("div");
    div.className = "stream-line";
    const tag = document.createElement("span");
    tag.className = "k-" + (kind === "danmaku" ? "dmk" : "cmt");
    tag.textContent = kind === "danmaku" ? "[弹幕]" : (kind === "videos" ? "[视频]" : "[评论]");
    const meta = document.createElement("span");
    meta.className = "meta";
    meta.textContent = ` ${d.ctime || d.pubdate || ""} ${d.sender || d.owner || ""} ` +
      `${d.ip_location && d.ip_location !== "-" ? "· " + d.ip_location : ""} | `;
    const body = document.createElement("span");
    body.textContent = d.content || d.title || "";
    div.append(tag, meta, body);
    frag.appendChild(div);
  }
  box.appendChild(frag);
}

$("#search-export").addEventListener("click", async () => {
  const bridge = api();
  if (!bridge || !lastSearchRows.length) return;
  $("#search-status").textContent = "正在导出 CSV…";
  try {
    const res = await bridge.export_search(lastSearchKind, lastSearchRows);
    if (!res || !res.ok) throw new Error((res && res.error) || "导出失败");
    $("#search-status").textContent =
      `✓ 已导出 ${res.rows.toLocaleString()} 行到 ${res.path}`;
  } catch (err) {
    $("#search-status").textContent = "导出失败: " + err;
  }
});

/* 研报：系统保存对话框 */
$("#rp-browse").addEventListener("click", async () => {
  const bridge = api();
  if (!bridge) return;
  const bvid = $("#rp-bvid").value.trim() || "video";
  try {
    const res = await bridge.pick_save_path(`report_${bvid}.html`,
                                            ["HTML 网页文件 (*.html)"]);
    if (res && res.ok && res.path) {
      $("#rp-out").value = res.path;
      $("#rp-status").textContent = `输出路径已选择: ${res.path}`;
    } else if (res && !res.ok) {
      $("#rp-status").textContent = "选择路径失败: " + res.error;
    }
  } catch (e) {
    $("#rp-status").textContent = "选择路径失败: " + e;
  }
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
