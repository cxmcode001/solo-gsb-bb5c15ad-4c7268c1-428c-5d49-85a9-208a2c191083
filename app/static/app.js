/* Webhook Replay Lab — frontend (vanilla JS) */
"use strict";

const $ = (id) => document.getElementById(id);
const esc = (s) => String(s ?? "").replace(/[&<>"']/g,
  (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));

const SAMPLE = [
  { event_id: "evt-001", type: "invoice.created", customer: "Acme", amount: 100, currency: "CNY" },
  { event_id: "evt-002", type: "invoice.paid", customer: "Acme", amount: 100, currency: "CNY", paid_at: "2026-09-22T10:00:00Z" },
  { event_id: "evt-003", type: "invoice.refunded", customer: "Acme", amount: 50, currency: "CNY" },
];

let lastEvents = [];

async function api(path, opts = {}) {
  const resp = await fetch(path, {
    headers: { "Content-Type": "application/json" },
    ...opts,
  });
  const text = await resp.text();
  const data = text ? JSON.parse(text) : {};
  if (!resp.ok) throw new Error(typeof data.detail === "string" ? data.detail : JSON.stringify(data.detail));
  return data;
}

function flash(el, text, ok = true) {
  el.textContent = text;
  el.style.color = ok ? "var(--green)" : "var(--red)";
  setTimeout(() => { el.textContent = ""; }, 4000);
}

// ---------- settings ----------

async function loadSettings() {
  const s = await api("/api/settings");
  $("target_url").value = s.target_url;
  $("hmac_secret").value = s.hmac_secret;
  $("max_retries").value = s.max_retries;
  $("timeout_seconds").value = s.timeout_seconds;
}

$("save-settings").addEventListener("click", async () => {
  try {
    await api("/api/settings", {
      method: "POST",
      body: JSON.stringify({
        target_url: $("target_url").value.trim(),
        hmac_secret: $("hmac_secret").value,
        max_retries: Number($("max_retries").value),
        timeout_seconds: Number($("timeout_seconds").value),
      }),
    });
    flash($("settings-msg"), "已保存 ✓");
  } catch (e) { flash($("settings-msg"), e.message, false); }
});

// ---------- import ----------

$("load-sample").addEventListener("click", () => {
  $("payload").value = JSON.stringify(SAMPLE, null, 2);
});

$("file").addEventListener("change", async (ev) => {
  const f = ev.target.files[0];
  if (!f) return;
  $("payload").value = await f.text();
});

$("import").addEventListener("click", async () => {
  let doc;
  try {
    doc = JSON.parse($("payload").value);
  } catch (e) {
    flash($("import-msg"), "JSON 解析失败: " + e.message, false);
    return;
  }
  try {
    const r = await api("/api/events/import", { method: "POST", body: JSON.stringify(doc) });
    flash($("import-msg"), `入库 ${r.imported} 条（新增 ${r.created.length} / 覆盖 ${r.updated.length}）✓`);
    refreshAll();
  } catch (e) { flash($("import-msg"), e.message, false); }
});

// ---------- events ----------

async function refreshEvents() {
  const { events } = await api("/api/events");
  lastEvents = events;
  const body = $("events-body");
  if (!events.length) {
    body.innerHTML = '<tr><td colspan="6" class="muted center">暂无事件</td></tr>';
    return;
  }
  body.innerHTML = events.map((e) => {
    const busy = e.status === "queued" || e.status === "running";
    const label = { pending: "待发送", queued: "排队中", running: "发送中…", success: "成功", failed: "失败" }[e.status] || e.status;
    const count = e.attempt_count ? `${e.attempt_count} 次` : "—";
    let result = "—";
    if (e.status === "success") result = '<span style="color:var(--green)">已送达</span>';
    else if (e.status === "failed") result = '<span style="color:var(--red)">重试耗尽 / 不可重试</span>';
    return `<tr>
      <td>${e.seq}</td>
      <td class="mono"><a class="link" onclick="showEvent('${encodeURIComponent(e.event_id)}')">${esc(e.event_id)}</a></td>
      <td><span class="badge ${e.status}">${label}</span></td>
      <td>${count}</td>
      <td>${result}</td>
      <td>
        <button class="mini" onclick="showEvent('${encodeURIComponent(e.event_id)}')">详情</button>
        <button class="mini" ${busy ? "disabled" : ""} onclick="replayOne('${encodeURIComponent(e.event_id)}')">重放</button>
      </td>
    </tr>`;
  }).join("");
}

async function replayOne(encId) {
  try {
    await api(`/api/events/${encId}/replay`, { method: "POST" });
    refreshAll();
  } catch (e) { alert(e.message); }
}

async function showEvent(encId) {
  const d = await api(`/api/events/${encId}`);
  $("modal-title").textContent = `事件 ${d.event_id}（#${d.seq}，${d.status}）`;
  const out = {
    event_id: d.event_id,
    status: d.status,
    created_at: d.created_at,
    updated_at: d.updated_at,
    raw_payload: d.payload_parsed,
    attempts: d.timeline.map((t) => ({
      replay: t.replay_no, attempt: t.attempt_no,
      at: t.request_at, http_status: t.http_status, outcome: t.outcome,
      elapsed_ms: t.elapsed_ms, idempotency_key: t.idem_key,
      response: t.resp_excerpt,
    })),
  };
  $("modal-body").textContent = JSON.stringify(out, null, 2);
  $("modal").classList.remove("hidden");
}

$("modal-close").addEventListener("click", () => $("modal").classList.add("hidden"));
$("modal").addEventListener("click", (e) => { if (e.target.id === "modal") $("modal").classList.add("hidden"); });

$("replay-all").addEventListener("click", async () => {
  const r = await api("/api/replay", { method: "POST", body: "{}" });
  alert(`已入队 ${r.queued.length} 条（跳过 ${r.skipped} 条进行中的事件）`);
  refreshAll();
});
$("replay-failed").addEventListener("click", async () => {
  await api("/api/replay", { method: "POST", body: JSON.stringify({ only_failed: true }) });
  refreshAll();
});
$("refresh").addEventListener("click", refreshAll);

$("clear").addEventListener("click", async () => {
  if (!confirm("确定删除全部事件、尝试记录？（目标配置保留）")) return;
  await api("/api/events", { method: "DELETE" });
  await fetch("/recv/logs", { method: "DELETE" });
  refreshAll();
});

// ---------- snapshot backup / restore ----------

let restoreFile = null;

$("restore-file").addEventListener("change", (ev) => {
  restoreFile = ev.target.files[0] || null;
  $("snapshot-file").textContent = restoreFile ? `已选择：${restoreFile.name}（${restoreFile.size} 字节）` : "";
  $("restore-upload").disabled = !restoreFile;
});

$("restore-upload").addEventListener("click", async () => {
  if (!restoreFile) return;
  if (!confirm(
    `将用快照「${restoreFile.name}」原子替换当前全部事件与尝试记录。\n`
    + "当前目标配置（URL / HMAC / 重试 / 超时）会保留。\n\n"
    + "校验不通过时不会写入任何数据。确定继续？")) return;
  const btn = $("restore-upload");
  btn.disabled = true;
  btn.textContent = "校验并恢复中…";
  try {
    const form = new FormData();
    form.append("file", restoreFile);
    const resp = await fetch("/api/snapshot/restore", { method: "POST", body: form });
    const data = await resp.json();
    if (!resp.ok) {
      showReport(false, data.detail, restoreFile.name);
    } else {
      showReport(true, data, restoreFile.name);
      refreshAll();
    }
  } catch (e) {
    showReport(false, { message: "请求失败: " + e.message, checks: [] }, restoreFile.name);
  } finally {
    btn.disabled = false;
    btn.textContent = "从快照恢复（原子替换）";
  }
});

function checkRows(checks) {
  if (!checks || !checks.length) {
    return '<p class="muted">没有返回校验明细。</p>';
  }
  return '<table class="checks"><thead><tr><th>校验项</th><th>结果</th><th>明细</th></tr></thead><tbody>'
    + checks.map((c) => `<tr>
        <td class="mono">${esc(c.name)}</td>
        <td>${c.ok
          ? '<span class="badge success">通过</span>'
          : '<span class="badge failed">失败 · 拒绝写入</span>'}</td>
        <td class="muted">${esc(c.detail || "")}</td>
      </tr>`).join("")
    + "</tbody></table>";
}

function showReport(ok, data, fileName) {
  $("report-title").textContent = ok ? "✅ 快照恢复成功" : "⛔ 快照恢复被拒绝";
  let html = "";
  if (ok) {
    const m = data.manifest || {};
    const c = data.counts || {};
    const snap = data.snapshot_settings || {};
    const cur = data.settings_preserved || {};
    html += `
      <div class="report-hero ${ok ? "ok" : "bad"}">
        已在单事务内完成替换：事件 <b>${c.events ?? "-"}</b> 条 ·
        尝试 <b>${c.attempts ?? "-"}</b> 条 ·
        其中 <b>${c.queued_running_reset_to_pending ?? 0}</b> 条进行中事件已回到「待发送」。
      </div>
      <div class="report-grid">
        <div><span class="muted">快照文件</span><div class="mono">${esc(fileName)}</div></div>
        <div><span class="muted">备份时间</span><div class="mono">${esc(m.created_at || "-")}</div></div>
        <div><span class="muted">数据库版本</span><div class="mono">v${esc(String(m.db_version ?? "-"))}</div></div>
        <div><span class="muted">格式</span><div class="mono">${esc(m.format || "-")}</div></div>
      </div>
      <h4>配置保留情况（当前配置未被修改）</h4>
      <table class="checks">
        <thead><tr><th>字段</th><th>快照中的值</th><th>恢复后当前值</th></tr></thead>
        <tbody>
          <tr><td>target_url</td><td class="mono">${esc(snap.target_url ?? "-")}</td><td class="mono">${esc(cur.target_url ?? "-")}</td></tr>
          <tr><td>hmac_secret</td><td class="mono">${esc(snap.hmac_secret ?? "-")}</td><td class="mono">（已保留，不显示）</td></tr>
          <tr><td>max_retries</td><td class="mono">${esc(String(snap.max_retries ?? "-"))}</td><td class="mono">${esc(String(cur.max_retries ?? "-"))}</td></tr>
          <tr><td>timeout_seconds</td><td class="mono">${esc(String(snap.timeout_seconds ?? "-"))}</td><td class="mono">${esc(String(cur.timeout_seconds ?? "-"))}</td></tr>
        </tbody>
      </table>`;
  } else {
    html += `<div class="report-hero bad">${esc(data.message || "校验失败，未写入任何数据。")}</div>`;
  }
  html += `<h4>${ok ? "校验清单（全部通过后才执行写入）" : "校验清单（在首个失败项中止）"}</h4>`;
  html += checkRows(data.checks);
  $("report-body").innerHTML = html;
  $("report-modal").classList.remove("hidden");
}

$("report-close").addEventListener("click", () => $("report-modal").classList.add("hidden"));
$("report-modal").addEventListener("click", (e) => {
  if (e.target.id === "report-modal") $("report-modal").classList.add("hidden");
});

// ---------- timeline ----------

function outcomeBadge(o) {
  const map = {
    delivered: ["success", "已送达"],
    retriable: ["running", "可重试错误"],
    non_retriable: ["failed", "不可重试"],
  };
  const [cls, label] = map[o] || ["pending", o];
  return `<span class="badge ${cls}">${label}</span>`;
}

async function refreshTimeline() {
  const { attempts } = await api("/api/timeline?limit=200");
  $("timeline-body").innerHTML = attempts.length ? attempts.map((t) => {
    const short = t.idem_key ? t.idem_key.slice(0, 10) + "…" : "";
    const status = t.http_status == null
      ? '<span class="badge failed">无响应</span>'
      : `<span class="badge ${t.http_status < 300 ? "success" : "failed"}">${t.http_status}</span>`;
    return `<tr>
      <td class="mono">${esc((t.request_at || "").replace("T", " ").slice(11, 23))}</td>
      <td class="mono"><a class="link" onclick="showEvent('${encodeURIComponent(t.event_id)}')">${esc(t.event_id)}</a></td>
      <td>${t.replay_no}-${t.attempt_no}<div class="muted" title="${esc(t.idem_key)}">idem ${esc(short)}</div></td>
      <td>${status}</td>
      <td>${outcomeBadge(t.outcome)}</td>
      <td>${t.elapsed_ms ?? ""} ms</td>
      <td><div class="excerpt" title="${esc(t.resp_excerpt || "")}">${esc(t.resp_excerpt || "")}</div></td>
    </tr>`;
  }).join("") : '<tr><td colspan="7" class="muted center">暂无请求记录</td></tr>';
}

async function refreshStatus() {
  const s = await api("/api/status");
  const c = s.counts;
  $("status-pills").innerHTML =
    `<span class="pill">总数 <b>${s.total}</b></span>` +
    `<span class="pill">待发送 <b>${c.pending}</b></span>` +
    `<span class="pill">队列/发送中 <b>${c.queued + c.running}</b></span>` +
    `<span class="pill">成功 <b style="color:var(--green)">${c.success}</b></span>` +
    `<span class="pill">失败 <b style="color:var(--red)">${c.failed}</b></span>`;
}

function refreshAll() {
  refreshStatus();
  refreshEvents();
  refreshTimeline();
}

refreshAll();
loadSettings();
setInterval(() => { refreshStatus(); refreshEvents(); refreshTimeline(); }, 2500);
