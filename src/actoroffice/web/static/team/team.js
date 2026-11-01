// Team copilot UI: teams and tickets on the left (herdr-like), tabs on top, one or two ticket panes, and a side
// panel with Files / Changes / Dossier / Rules / Access. Everything is live through the office's WebSocket.

const HEADERS = { "X-Requested-With": "actoroffice", "Content-Type": "application/json" };
const ICON = {
  narration: "›", thinking: "∴", read: "📖", search: "🔎", source: "🌐", edit: "✎", command: "$", office: "▪",
  question: "?", step: "☑", check: "✓", deviation: "↪", stage: "◆", usage: "⏱", health: "⚠", error: "✕", rule: "§", compacted: "≈",
};
const STATUS_LABEL = {
  not_started: "chưa bắt đầu", queued: "đang xếp hàng", working: "đang làm", needs_you: "cần người", waiting_external: "chờ bên ngoài",
  paused: "tạm dừng", blocked: "bị chặn", failed: "lỗi", idle: "đang chờ", done: "xong",
};

const state = {
  board: { teams: [], tickets: [], needs_you: 0 },
  team: null, // filter: team id or null = all
  open: JSON.parse(localStorage.getItem("ao.open") || "[]"),
  focus: localStorage.getItem("ao.focus") || null,
  split: localStorage.getItem("ao.split") === "1",
  side: localStorage.getItem("ao.side") || "files",
  details: {}, // key → {ticket, stage, detail}
  acts: {}, // run id → activities
  runTicket: {}, // run id → ticket key
};

// ---- helpers -------------------------------------------------------------------------------------------------

const $ = (sel, root = document) => root.querySelector(sel);
const esc = (v) => String(v ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[c]);

async function api(path, opts = {}) {
  const res = await fetch(path, { credentials: "same-origin", headers: HEADERS, ...opts, body: opts.body ? JSON.stringify(opts.body) : undefined });
  if (res.status === 401) { location.href = "/"; throw new Error("not signed in"); }
  const text = await res.text();
  const data = text && (res.headers.get("content-type") || "").includes("json") ? JSON.parse(text) : text;
  if (!res.ok) throw new Error((data && (data.error || (typeof data.detail === "string" ? data.detail : null))) || res.statusText);
  return data;
}

function toast(text, err = false) {
  const el = document.createElement("div");
  el.className = "toast" + (err ? " err" : "");
  el.textContent = text;
  $("#toasts").append(el);
  setTimeout(() => el.remove(), err ? 7000 : 3500);
}

async function act(fn) {
  try { await fn(); } catch (e) { toast(e.message || String(e), true); }
}

function ago(ts) {
  if (!ts) return "";
  const s = Math.max(0, (Date.now() - new Date(ts).getTime()) / 1000);
  if (s < 60) return "<1m";
  if (s < 3600) return `${Math.round(s / 60)}m`;
  if (s < 86400) return `${Math.round(s / 3600)}h`;
  return `${Math.round(s / 86400)}d`;
}

function tokens(u) {
  if (!u) return 0;
  return ["input_tokens", "output_tokens", "cache_read_tokens", "cache_write_tokens"].reduce((n, k) => n + (u[k] || 0), 0);
}

function md(text) {
  // Small, safe markdown: headings, lists, code fences, bold, inline code, tables as text.
  const out = [];
  let code = false;
  for (const raw of String(text || "").split("\n")) {
    if (raw.trim().startsWith("```")) { out.push(code ? "</pre>" : "<pre>"); code = !code; continue; }
    if (code) { out.push(esc(raw)); continue; }
    let line = esc(raw).replace(/\*\*(.+?)\*\*/g, "<b>$1</b>").replace(/`([^`]+)`/g, "<code>$1</code>");
    const h = line.match(/^(#{1,4})\s+(.*)/);
    if (h) out.push(`<h${h[1].length}>${h[2]}</h${h[1].length}>`);
    else if (/^\s*[-*]\s+/.test(line)) out.push(`<li>${line.replace(/^\s*[-*]\s+/, "")}</li>`);
    else if (line.trim()) out.push(`<p>${line}</p>`);
  }
  if (code) out.push("</pre>");
  return out.join("\n");
}

function save() {
  localStorage.setItem("ao.open", JSON.stringify(state.open));
  localStorage.setItem("ao.focus", state.focus || "");
  localStorage.setItem("ao.split", state.split ? "1" : "0");
  localStorage.setItem("ao.side", state.side || "");
}

// ---- data ----------------------------------------------------------------------------------------------------

async function loadBoard() {
  state.board = await api("/api/board?viewer=user");
  renderSidebar();
  renderTabs();
}

async function loadTicket(key) {
  const [detail, stage] = await Promise.all([api(`/api/tickets/${key}`), api(`/api/tickets/${key}/stage`).catch(() => null)]);
  state.details[key] = { detail, stage };
  const runs = (detail.runs || []).slice(0, 4).reverse();
  for (const run of runs) {
    state.runTicket[run.id] = key;
    state.acts[run.id] = await api(`/api/runs/${run.id}/activities`);
  }
}

function boardItem(key) {
  return state.board.tickets.find((t) => t.ticket === key);
}

// ---- sidebar -------------------------------------------------------------------------------------------------

function renderSidebar() {
  const { teams, tickets, needs_you } = state.board;
  const visible = tickets.filter((t) => !state.team || t.team_id === state.team);
  const byTeam = {};
  for (const t of visible) (byTeam[t.team_id || ""] ||= []).push(t);
  const teamName = (id) => (teams.find((t) => t.id === id) || {}).name || "Chung";
  $("#sidebar").innerHTML = `
    <div class="brand"><span class="dots"><i></i><i></i><i></i></span> Copilot
      ${needs_you ? `<span class="badge" title="Việc đang chờ bạn">${needs_you} cần bạn</span>` : ""}</div>
    <div class="side-scroll">
      <div class="side-title">Spaces</div>
      <button class="space ${state.team ? "" : "on"}" data-team=""><span class="ico">∗</span><span class="name">Tất cả</span><span class="badge soft">${tickets.length}</span></button>
      ${teams.map((t) => `<button class="space ${state.team === t.id ? "on" : ""}" data-team="${esc(t.id)}"><span class="ico">${esc(t.name[0] || "?")}</span><span class="name">${esc(t.name)}</span>${t.needs_you ? `<span class="badge">Cần bạn</span>` : ""}</button>`).join("")}
      <div class="side-title">Việc <button data-new title="Việc mới">+</button></div>
      ${Object.entries(byTeam).map(([team, list]) => `
        <div class="side-title team-head">${esc(teamName(team))}</div>
        ${list.map((t) => `
          <button class="tk ${state.focus === t.ticket ? "on" : ""}" data-open="${esc(t.ticket)}" title="${esc(t.title)}">
            <span class="dot ${esc(t.status)}"></span>
            <span class="col"><span class="name">${esc(t.title)}</span>
              <span class="sub">${esc(t.ticket)} · ${esc(t.stage_title)}${t.activity ? " · " + esc(t.activity.title) : ""}</span></span>
            ${t.needs_viewer ? `<span class="badge">Cần bạn</span>` : `<span class="meta">${ago(t.activity?.at || t.updated_at)}</span>`}
          </button>`).join("")}`).join("") || `<div class="empty">Chưa có việc. Bấm + để giao việc cho AI.</div>`}
    </div>
    <div class="side-foot">
      <button data-page="inbox">Cần tôi ${needs_you ? `(${needs_you})` : ""}</button>
      <button data-page="rules">Rules</button><button data-page="kb">Kiến thức</button><button data-page="people">Người</button><button data-page="setup">Cài đặt</button>
    </div>`;
}

// ---- tabs ----------------------------------------------------------------------------------------------------

function renderTabs() {
  const sides = [["files", "Files"], ["changes", "Changes"], ["dossier", "Dossier"], ["rules", "Rules"], ["access", "Access"]];
  $("#tabs").innerHTML = `
    ${state.open.map((key) => {
      const t = boardItem(key);
      return `<button class="tab ${state.focus === key ? "on" : ""}" data-tab="${esc(key)}"><span class="dot ${esc(t?.status || "")}"></span>
        <span class="name">${esc(t ? t.title : key)}</span><span class="x" data-close="${esc(key)}">×</span></button>`;
    }).join("")}
    <div class="tabs-right">
      <button class="toggle ${state.split ? "on" : ""}" data-split title="Xem 2 việc cạnh nhau">⫴</button>
      ${sides.map(([id, label]) => `<button class="toggle ${state.side === id ? "on" : ""}" data-side="${id}">${label}</button>`).join("")}
    </div>`;
}

// ---- panes ---------------------------------------------------------------------------------------------------

function paneKeys() {
  if (!state.focus) return [];
  if (!state.split) return [state.focus];
  const other = state.open.find((k) => k !== state.focus);
  return other ? [state.focus, other] : [state.focus];
}

function renderPanes() {
  const keys = paneKeys();
  $("#panes").innerHTML = keys.length ? keys.map((k) => `<section class="pane ${k === state.focus ? "focus" : ""}" data-pane="${esc(k)}"></section>`).join("")
    : `<div class="welcome" id="welcome"><div class="empty">Chọn một việc bên trái, hoặc bấm + để giao việc mới cho AI.</div></div>`;
  if (!keys.length) act(renderWelcome);
  for (const key of keys) renderPane(key);
  renderSide();
}

function timeline(key) {
  const d = state.details[key];
  if (!d) return [];
  const runs = (d.detail.runs || []).slice(0, 4).reverse();
  const items = [];
  for (const run of runs) {
    const byCall = new Map();
    for (const a of state.acts[run.id] || []) {
      if (a.call_id && byCall.has(a.call_id)) items[byCall.get(a.call_id)] = a; // the call ended: replace "running"
      else { if (a.call_id) byCall.set(a.call_id, items.length); items.push(a); }
    }
  }
  return items;
}

function renderTimeline(items) {
  const out = [];
  let i = 0;
  while (i < items.length) {
    const a = items[i];
    if (a.kind === "read" || a.kind === "search") {
      let j = i;
      while (j < items.length && (items[j].kind === "read" || items[j].kind === "search")) j++;
      if (j - i >= 3) {
        out.push(`<div class="ev group" data-group><span class="ic">${ICON.read}</span><span class="tx">Đã xem ${j - i} file / kết quả tìm</span></div>
          <div class="group-items">${items.slice(i, j).map(line).join("")}</div>`);
        i = j;
        continue;
      }
    }
    out.push(line(a));
    i++;
  }
  return out.join("");

  function line(a) {
    const cls = [a.kind, a.status === "running" ? "running" : "", a.status === "error" ? "error" : ""].join(" ");
    return `<div class="ev ${cls}"><span class="ic">${a.status === "running" ? "◌" : ICON[a.kind] || "·"}</span><span class="tx">${esc(a.title)}</span></div>`;
  }
}

function card(item) {
  const p = item.payload || {};
  const who = (p.approver_names || []).join(", ") || p.roles_label || "bạn";
  if (item.kind === "question") {
    return `<div class="card" data-card="${esc(item.id)}"><div class="who">${esc(p.asked_by || "AI")} hỏi ${esc(who)}</div>
      <div class="q">${esc(p.question)}</div>${p.context ? `<div class="who">${esc(p.context)}</div>` : ""}
      <div class="opts">${(p.options || []).map((o) => `<button class="btn ${o === p.recommended ? "rec" : ""}" data-answer="${esc(o)}">${o === p.recommended ? "⭐ " : ""}${esc(o)}</button>`).join("")}</div>
      <div class="composer inline"><textarea placeholder="Hoặc trả lời bằng chữ…" data-text></textarea><button class="btn" data-answer-text>Trả lời</button></div></div>`;
  }
  if (item.kind === "approve_stage") {
    const checks = (p.checks || []).map((c) => `<li>${c.status === "passed" ? "✅" : c.status === "failed" ? "❌" : "⏭"} ${esc(c.id)}: ${esc(c.summary)}</li>`).join("");
    const kb = (p.kb || []).map((k) => `<li>${esc(k)}</li>`).join("");
    return `<div class="card" data-card="${esc(item.id)}"><div class="who">✋ ${esc(p.stage_title)} xong — cần ${esc(who)} duyệt</div>
      <div class="q">${esc(p.summary)}</div>${checks ? `<ul>${checks}</ul>` : ""}${kb ? `<div class="who">Kiến thức AI ghi lại:</div><ul>${kb}</ul>` : ""}
      ${(p.access || []).length ? `<div class="who">Duyệt kèm quyền: ${esc(p.access.join(", "))}</div>` : ""}
      <div class="composer inline"><textarea placeholder="${p.gate === "knowledge" ? "Bỏ mục sai: 'bỏ 2, 5'" : "Ghi chú khi trả lại…"}" data-text></textarea></div>
      <div class="opts"><button class="btn good" data-approve="1">👍 ${p.gate === "knowledge" ? "Xác nhận" : "Duyệt"}</button><button class="btn bad" data-approve="0">👎 ${p.gate === "knowledge" ? "Bỏ (theo ghi chú)" : "Trả lại"}</button></div></div>`;
  }
  if (item.kind === "env_access") {
    return `<div class="card" data-card="${esc(item.id)}"><div class="who">🔑 AI xin quyền ${esc(p.level_label)} vào ${esc(p.resource)} (${esc(p.env)}) trong ${esc(p.ttl_h)}h — cần ${esc(who)}</div>
      <div class="q">${esc(p.reason)}</div><div class="opts"><button class="btn good" data-approve="1">Cho phép</button><button class="btn bad" data-approve="0">Không</button></div></div>`;
  }
  if (item.kind === "approve_tool") {
    return `<div class="card" data-card="${esc(item.id)}"><div class="who">🔐 Xin phép: ${esc(p.tool)}</div><pre>${esc(p.target || "")}</pre>
      <div class="opts"><button class="btn good" data-approve="1">Cho phép 1 lần</button><button class="btn bad" data-approve="0">Từ chối</button></div></div>`;
  }
  return `<div class="card"><div class="who">${esc(item.kind)}</div></div>`;
}

function renderPane(key) {
  const el = $(`.pane[data-pane="${CSS.escape(key)}"]`);
  const d = state.details[key];
  if (!el) return;
  if (!d) { el.innerHTML = `<div class="empty">Đang tải ${esc(key)}…</div>`; return; }
  const t = d.detail.ticket;
  const st = d.stage?.status || {};
  const stages = d.stage?.template?.stages || [];
  const idx = stages.findIndex((s) => s.id === t.stage);
  const steps = st.steps || [];
  const cur = steps.findIndex((s) => !s.done);
  const pending = d.detail.pending || [];
  el.innerHTML = `
    <div class="pane-head">
      <div class="pane-title"><span class="key">${esc(t.key)}</span><span class="title">${esc(t.title)}</span>
        <span class="chip ${esc(st.status)}">${esc(st.stage_title || "")} · ${esc(STATUS_LABEL[st.status] || st.status || "")}</span></div>
      <div class="stepper">${stages.map((s, i) => `<span class="${i === idx ? "cur" : i < idx ? "past" : ""}">${esc(s.id)}${s.gate ? ' <b class="gate">✋</b>' : ""}</span>`).join("→")}</div>
      <div class="pane-meta">
        ${st.who?.length ? `<span>chờ ${esc(st.who.join(", "))}</span>` : ""}
        ${steps.length ? `<span>bước ${st.steps_done}/${steps.length}</span>` : ""}
        <span>lần ${st.attempt || 1}</span><span>${(tokens(st.usage) / 1000).toFixed(1)}k tok</span>
        ${t.jira_url ? `<a href="${esc(t.jira_url)}" target="_blank" rel="noreferrer">${esc(t.jira_key)}</a>` : ""}
        ${t.pr_url ? `<a href="${esc(t.pr_url)}" target="_blank" rel="noreferrer">PR</a>` : ""}
        <span class="pane-controls">
          ${st.status === "paused" ? `<button class="btn small" data-cmd="resume">▶ Tiếp</button>` : `<button class="btn small" data-cmd="pause">⏸ Dừng</button>`}
          <button class="btn small" data-cmd="retry" title="Làm lại giai đoạn">↻</button>
          ${t.stage === "pr" ? `<button class="btn small" data-cmd="merged">PR đã merge</button>` : ""}
        </span>
      </div>
    </div>
    ${steps.length ? `<div class="steps">${steps.map((s, i) => `<div class="s ${s.done ? "done" : i === cur ? "cur" : ""}">${s.done ? "☑" : i === cur ? "▶" : "☐"} ${esc(s.text)}</div>`).join("")}</div>` : ""}
    <div class="timeline">${renderTimeline(timeline(key)) || `<div class="empty">Chưa có hoạt động.</div>`}</div>
    ${pending.length ? `<div class="cards">${pending.map(card).join("")}</div>` : ""}
    <div class="composer"><textarea placeholder="Chỉ dẫn cho AI (AI đang chạy sẽ nghe ngay ở bước kế tiếp)…" data-steer></textarea><button class="btn primary" data-send>Gửi</button></div>`;
  const tl = $(".timeline", el);
  tl.scrollTop = tl.scrollHeight;
}

// ---- side panel ----------------------------------------------------------------------------------------------

async function renderSide() {
  const side = $("#side");
  const key = state.focus;
  side.hidden = !state.side || !key;
  if (side.hidden) return;
  side.innerHTML = `<div class="side-tabs"><b>${esc(state.side)}</b> · ${esc(key)}</div><div class="side-body">Đang tải…</div>`;
  const body = $(".side-body", side);
  try {
    if (state.side === "files" || state.side === "changes") {
      const c = await api(`/api/tickets/${key}/changes`);
      if (!c.git) { body.innerHTML = `<div class="empty">Workspace không phải git worktree.</div>`; return; }
      if (state.side === "files") {
        body.innerHTML = `<div class="kv"><dt>branch</dt><dd><code>${esc(c.branch)}</code></dd><dt>base</dt><dd>${esc(c.base)}</dd></div><hr>` +
          (c.files.map((f) => `<div class="file"><span class="p">${esc(f.path)}</span>${f.untracked ? `<span class="add">mới</span>` : `<span class="add">+${f.added}</span><span class="del">−${f.removed}</span>`}</div>`).join("") || `<div class="empty">Chưa có thay đổi.</div>`);
      } else {
        body.innerHTML = `<pre class="diff">${c.diff.split("\n").map((l) => `<span class="${l.startsWith("+") && !l.startsWith("+++") ? "a" : l.startsWith("-") && !l.startsWith("---") ? "d" : l.startsWith("@@") ? "h" : ""}">${esc(l) || " "}</span>`).join("\n")}</pre>` || `<div class="empty">Chưa có thay đổi.</div>`;
      }
    } else if (state.side === "dossier") {
      const docs = state.details[key]?.detail.documents || [];
      const order = ["dossier", "requirements", "plan", "handoff", "report", "document", "kb-draft"];
      docs.sort((a, b) => order.indexOf(a.key) - order.indexOf(b.key));
      body.innerHTML = docs.map((d) => `<div class="doc"><h2>${esc(d.key)} <span class="chip">rev ${d.revision}${d.approved_revision ? ` · duyệt rev ${d.approved_revision}` : ""}</span></h2>${md(d.body)}</div>`).join("<hr>") || `<div class="empty">Chưa có tài liệu.</div>`;
    } else if (state.side === "rules") {
      const r = await api(`/api/tickets/${key}/rules`);
      const block = (title, list) => list.length ? `<h3>${title}</h3>` + list.map((x) => `<div class="rule"><div class="h"><b>${esc(x.id)}</b><span class="chip">${esc(x.scope)}</span><span>${esc(x.title)}</span></div>${x.body ? `<div class="b">${esc(x.body)}</div>` : ""}</div>`).join("") : "";
      body.innerHTML = block("Guidelines", r.guidelines) + block("Checks", r.checks) + block("Policies", r.policies) + block("Gates", r.gates) || `<div class="empty">Chưa có rule nào áp dụng.</div>`;
    } else if (state.side === "access") {
      const [grants, artifacts] = await Promise.all([api(`/api/tickets/${key}/access`), api(`/api/tickets/${key}/artifacts`)]);
      body.innerHTML = `<h3>Quyền đang có</h3>` + (grants.map((g) => `<div class="rule"><div class="h"><b>${esc(g.resource.name)}</b><span class="chip">${esc(g.grant.level)}</span><span>đến ${esc(g.grant.expires_at.slice(0, 16).replace("T", " "))}</span>
        <button class="btn small bad" data-revoke="${esc(g.grant.id)}">Thu hồi</button></div></div>`).join("") || `<div class="empty">Không có.</div>`) +
        `<h3>Bằng chứng (ảnh, video, báo cáo)</h3>` + (artifacts.map((a) => `<div class="file"><span class="p">${esc(a.kind)} · ${esc(String(a.ref).split(/[\\/]/).pop())}</span><span>${esc((a.metadata || {}).note || "")}</span></div>`).join("") || `<div class="empty">Không có.</div>`);
    }
  } catch (e) {
    body.innerHTML = `<div class="empty">${esc(e.message)}</div>`;
  }
}

// ---- first run checklist -------------------------------------------------------------------------------------

async function renderWelcome() {
  const box = $("#welcome");
  if (!box) return;
  const s = await api("/api/setup");
  const steps = [
    ["Thêm người và team (BA, Dev…)", s.people > 0 && s.teams.length > 0, "people"],
    ["Thêm repo/dự án cho AI làm việc", s.projects.length > 0, "setup"],
    ["Kết nối Telegram (tuỳ chọn)", s.connections.telegram.configured, "setup"],
    ["Kết nối Jira + Confluence (tuỳ chọn)", s.connections.jira.configured, "setup"],
    ["Giao việc đầu tiên cho AI", state.board.tickets.length > 0, "new"],
  ];
  if (steps.every(([, ok]) => ok)) return;
  box.innerHTML = `<div class="checklist"><h2>Bắt đầu</h2>
    ${steps.map(([label, ok, page]) => `<div class="step ${ok ? "ok" : ""}"><span class="tick">${ok ? "✓" : "○"}</span><span class="name">${esc(label)}</span>
      ${ok ? "" : page === "new" ? `<button class="btn primary" data-new>Giao việc</button>` : `<button class="btn" data-page="${page}">Mở</button>`}</div>`).join("")}
    <div class="hint">AI: ${esc(s.ai.name)} · model ${esc(s.ai.model)}. Mọi thứ đều chỉnh được trong Cài đặt.</div></div>`;
}

// ---- overlay pages -------------------------------------------------------------------------------------------

function closeSheet() {
  $("#overlay").hidden = true;
  if (!paneKeys().length) act(async () => { await loadBoard(); renderPanes(); }); // the start checklist reflects what was just set up
}

function sheet(title, html) {
  const o = $("#overlay");
  o.hidden = false;
  o.innerHTML = `<div class="sheet"><div class="sheet-head"><h2>${esc(title)}</h2><button class="btn" data-close-sheet>Đóng</button></div><div class="sheet-body">${html}</div></div>`;
  return $(".sheet-body", o);
}

async function pageNewTask() {
  const [people, setup] = await Promise.all([api("/api/people"), api("/api/setup")]);
  const body = sheet("Giao việc mới cho AI", `
    <div class="form">
      <label>Tiêu đề<input data-f="title" placeholder="vd. Hoàn tiền cho đơn VIP trong 60 ngày"></label>
      <label>Loại việc<select data-f="template">${people.templates.map((t) => `<option>${esc(t)}</option>`).join("")}</select></label>
      <label>Repo / dự án<select data-f="project_id"><option value="">— (không có repo)</option>${setup.projects.map((p) => `<option value="${esc(p.id)}">${esc(p.name)}</option>`).join("")}</select></label>
      <label>Team<select data-f="team_id"><option value="">—</option>${people.teams.map((t) => `<option value="${esc(t.id)}">${esc(t.name)}</option>`).join("")}</select></label>
      <label>Người yêu cầu<select data-f="owner_user_id"><option value="">—</option>${people.users.filter((u) => u.id !== "user").map((u) => `<option value="${esc(u.id)}">${esc(u.name)}</option>`).join("")}</select></label>
      <label>Khi người chưa trả lời<select data-f="mode"><option value="wait">Chờ</option><option value="assume">Làm theo giả định</option></select></label>
    </div>
    <label>Mô tả<textarea data-f="description" rows="6" placeholder="Bối cảnh, mong muốn, link Jira/Confluence…"></textarea></label>
    <div><button class="btn primary" data-create-task>Giao cho AI</button></div>`);
  body.dataset.page = "new";
}

async function pageInbox() {
  const mine = state.board.tickets.filter((t) => t.needs_viewer || t.status === "needs_you");
  sheet("Cần tôi", mine.length ? `<table class="list"><tr><th>Việc</th><th>Giai đoạn</th><th>Đang chờ</th></tr>${mine.map((t) => `<tr><td><a href="#" data-open="${esc(t.ticket)}">${esc(t.ticket)} · ${esc(t.title)}</a></td><td>${esc(t.stage_title)}</td><td>${esc((t.who || []).join(", "))}</td></tr>`).join("")}</table>` : `<div class="empty">Không có gì đang chờ bạn.</div>`);
}

async function pageRules() {
  const [rules, people] = await Promise.all([api("/api/rules"), api("/api/people")]);
  const scopes = [["global", "Toàn bộ"], ...people.teams.map((t) => [`team:${t.id}`, `Team ${t.name}`])];
  const scopeName = (scope) => (scopes.find(([v]) => v === scope) || [scope, scope])[1];
  sheet("Rules", `
    <div class="form">
      <label>Tiêu đề<input data-r="title" placeholder="vd. Test Java dùng JUnit 5 + AssertJ"></label>
      <label>Loại<select data-r="kind"><option>guideline</option><option>check</option><option>policy</option><option>gate</option></select></label>
      <label>Phạm vi<select data-r="scope">${scopes.map(([v, l]) => `<option value="${esc(v)}">${esc(l)}</option>`).join("")}</select></label>
      <label>Giai đoạn<input data-r="stage" value="any"></label>
    </div>
    <label>Nội dung (guideline) / spec JSON (check, policy, gate)<textarea data-r="body" rows="4" placeholder='guideline: viết thường · check: {"cmd": ["mvn","-q","verify"]} · policy: {"deny_patterns": ["--force"]} · gate: {"roles": ["lead"], "when": {"paths": ["db/migrations/**"]}}'></textarea></label>
    <div><button class="btn primary" data-add-rule>Thêm rule</button></div>
    <table class="list"><tr><th>Id</th><th>Loại</th><th>Phạm vi · stage</th><th>Tiêu đề</th><th>Trạng thái</th><th></th></tr>
    ${rules.map((r) => `<tr><td>${esc(r.id)} v${r.version}</td><td>${esc(r.kind)}</td><td>${esc(scopeName(r.scope))} · ${esc(r.stage)}</td><td>${esc(r.title)}${r.source ? ` <span class="chip">${esc(r.source)}</span>` : ""}</td><td>${esc(r.status)}</td>
      <td>${r.status !== "active" ? `<button class="btn small good" data-rule-approve="${esc(r.id)}">Duyệt</button>` : ""}${r.status !== "disabled" ? `<button class="btn small" data-rule-disable="${esc(r.id)}">Tắt</button>` : ""}</td></tr>`).join("")}</table>`);
}

async function pageKb() {
  const items = await api("/api/kb");
  sheet("Kiến thức của team", `<table class="list"><tr><th>#</th><th>Loại</th><th>Tiêu đề · nội dung</th><th>Trạng thái</th><th></th></tr>
    ${items.map((i) => `<tr><td>${i.id}</td><td>${esc(i.kind)}</td><td><b>${esc(i.title)}</b><div>${esc(i.content)}</div>${i.sources.length ? `<div class="chip">${esc(i.sources.join("; "))}</div>` : ""}</td><td>${esc(i.status)}</td>
      <td>${i.status !== "verified" ? `<button class="btn small good" data-kb="${i.id}" data-kb-status="verified">Đúng</button>` : ""}${i.status !== "rejected" ? `<button class="btn small bad" data-kb="${i.id}" data-kb-status="rejected">Sai</button>` : ""}</td></tr>`).join("")}</table>` || `<div class="empty">Chưa có kiến thức.</div>`);
}

async function pagePeople() {
  const p = await api("/api/people");
  sheet("Người & team", `
    <div class="form">
      <label>Tên<input data-p="name"></label><label>Telegram id<input data-p="telegram_id" type="number"></label>
      <label>Email<input data-p="email"></label><label>Vai chung<input data-p="roles" placeholder="ba dev qe lead admin"></label>
      <button class="btn primary" data-add-person>Thêm người</button>
    </div>
    <div class="form"><label>Team mới<input data-t="name"></label><label>Jira project<input data-t="jira_project_key"></label><label>Confluence space<input data-t="confluence_space"></label><button class="btn primary" data-add-team>Thêm team</button></div>
    ${p.teams.map((t) => `<div class="rule"><div class="h"><b>${esc(t.name)}</b>${t.jira_project_key ? `<span class="chip">Jira ${esc(t.jira_project_key)}</span>` : ""}</div>
      <div class="b">${t.members.map((m) => `${esc(m.user.name)} (${esc(m.roles.join(", "))})`).join(" · ") || "chưa có thành viên"}</div>
      <div class="form"><label>Người<select data-m-user="${esc(t.id)}">${p.users.filter((u) => u.id !== "user").map((u) => `<option value="${esc(u.id)}">${esc(u.name)}</option>`).join("")}</select></label>
        <label>Vai trong team<input data-m-roles="${esc(t.id)}" placeholder="ba dev"></label><button class="btn" data-add-member="${esc(t.id)}">Gán</button></div></div>`).join("")}`);
}

const MODELS = [["sonnet", "Claude Sonnet (khuyên dùng)"], ["opus", "Claude Opus (mạnh nhất, tốn hơn)"], ["haiku", "Claude Haiku (nhanh, rẻ)"], ["kiro", "Kiro CLI (cần cài kiro-cli)"]];

async function pageSetup() {
  const s = await api("/api/setup");
  const c = s.connections;
  const status = (ok, text) => `<span class="chip ${ok ? "working" : ""}">${ok ? "✓ " : ""}${esc(text)}</span>`;
  const teamOpts = (sel) => `<option value="">—</option>${s.teams.map((t) => `<option value="${esc(t.id)}" ${t.id === sel ? "selected" : ""}>${esc(t.name)}</option>`).join("")}`;
  const models = MODELS.some(([v]) => v === s.ai.model) ? MODELS : [[s.ai.model, s.ai.model], ...MODELS];
  sheet("Cài đặt", `
    <h3>AI</h3>
    <div class="form">
      <label>Model<select data-ai="model">${models.map(([v, l]) => `<option value="${esc(v)}" ${v === s.ai.model ? "selected" : ""}>${esc(l)}</option>`).join("")}</select></label>
      <button class="btn primary" data-save-ai>Lưu</button>
    </div>

    <h3>Repo / dự án</h3>
    <p class="hint">Thư mục repo trên máy này. AI làm trên git worktree riêng cho mỗi việc, không sửa thẳng nhánh của bạn. Lệnh test/build để AI tự kiểm tra (để trống nếu chưa có).</p>
    ${s.projects.map((p) => projectForm(p, teamOpts)).join("")}
    ${projectForm({ id: "", name: "", path: "", team_id: "", repo_url: "", test: "", build: "", lint: "" }, teamOpts)}

    <h3>Kết nối</h3>
    <p class="hint">Token được lưu trên máy chủ (thư mục secrets), không hiện lại và AI không bao giờ thấy.</p>
    <div class="rule"><div class="h"><b>Telegram</b>${status(c.telegram.running, c.telegram.running ? `@${c.telegram.bot} · ${c.telegram.bound ? "đã nối group" : "chờ /start trong group"}` : c.telegram.configured ? "có token, chưa chạy" : "chưa kết nối")}</div>
      <div class="b">@BotFather → /newbot → dán token. Sau đó tạo group, bật Topics, thêm bot làm admin và nhắn /start trong group.
Đổi sang bot khác: dán token mới rồi bấm Kết nối, bot cũ dừng ngay. Nếu bot mới được thêm vào group cũ thì các topic cũ dùng tiếp.</div>
      ${c.telegram.env_override ? `<div class="b">⚠ Biến môi trường ${esc(c.telegram.env_override)} đang chứa token và được ưu tiên khi khởi động. Hãy xoá biến này nếu muốn dùng token nhập ở đây.</div>` : ""}
      <div class="form"><label>Bot token${c.telegram.running ? " mới" : ""}<input data-c="tg_token" type="password" placeholder="123456789:AA…"></label><button class="btn primary" data-save-telegram>${c.telegram.running ? "Đổi bot" : "Kết nối"}</button>
        ${c.telegram.configured ? `<button class="btn bad" data-telegram-off>Tắt Telegram</button>` : ""}</div></div>
    <div class="rule"><div class="h"><b>Jira + Confluence (Cloud)</b>${status(c.jira.configured, c.jira.configured ? `${c.jira.email} · ${c.jira.base_url}` : "chưa kết nối")}</div>
      <div class="b">Tạo API token tại id.atlassian.com → Security → API tokens.</div>
      <div class="form"><label>URL<input data-c="jira_url" placeholder="vd. https://acme.atlassian.net" value="${esc(c.jira.base_url || "")}"></label>
        <label>Email<input data-c="jira_email" value="${esc(c.jira.email || "")}"></label><label>API token<input data-c="jira_token" type="password"></label>
        <button class="btn primary" data-save-jira>Kiểm tra & lưu</button></div></div>
    <div class="rule"><div class="h"><b>Bitbucket Cloud</b>${status(c.bitbucket.configured, c.bitbucket.configured ? `workspace ${c.bitbucket.workspace}` : "chưa kết nối")}</div>
      <div class="b">Dùng để AI tạo PR và đọc review. Không có cũng được: AI sẽ nhờ Dev tạo PR.</div>
      <div class="form"><label>Email<input data-c="bb_email"></label><label>API token<input data-c="bb_token" type="password"></label>
        <label>Workspace<input data-c="bb_workspace" value="${esc(c.bitbucket.workspace || "")}"></label><button class="btn primary" data-save-bitbucket>Kiểm tra & lưu</button></div></div>`);
}

function projectForm(p, teamOpts) {
  const k = esc(p.id || "new");
  return `<div class="rule" data-proj="${k}"><div class="h"><b>${p.id ? esc(p.name) : "Thêm repo mới"}</b>${p.path ? `<span class="chip">${esc(p.path)}</span>` : ""}</div>
    <div class="form">
      <label>Tên<input data-pf="name" value="${esc(p.name)}" placeholder="vd. billing"></label>
      <label>Thư mục repo<input data-pf="path" value="${esc(p.path || "")}" placeholder="vd. F:/code/billing"></label>
      <label>Team<select data-pf="team_id">${teamOpts(p.team_id)}</select></label>
      <label>Repo trên Bitbucket<input data-pf="repo_url" value="${esc(p.repo_url || "")}" placeholder="vd. acme/billing"></label>
      <label>Lệnh test<input data-pf="test" value="${esc(p.test)}" placeholder="vd. mvn -q test"></label>
      <label>Lệnh build<input data-pf="build" value="${esc(p.build)}" placeholder="vd. mvn -q package -DskipTests"></label>
      <label>Lệnh lint<input data-pf="lint" value="${esc(p.lint)}"></label>
      <button class="btn primary" data-save-project="${k}">${p.id ? "Lưu" : "Thêm"}</button>
    </div></div>`;
}

// ---- events --------------------------------------------------------------------------------------------------

function openTicket(key) {
  if (!state.open.includes(key)) state.open.push(key);
  state.focus = key;
  save();
  renderTabs();
  renderSidebar();
  renderPanes();
  act(async () => { await loadTicket(key); renderPanes(); });
}

document.addEventListener("click", (e) => {
  const t = e.target.closest("button, a, [data-group]");
  if (!t) return;
  const ds = t.dataset;
  if (ds.open) { e.preventDefault(); $("#overlay").hidden = true; openTicket(ds.open); return; }
  if (ds.team !== undefined) { state.team = ds.team || null; renderSidebar(); return; }
  if (ds.close) { e.stopPropagation(); state.open = state.open.filter((k) => k !== ds.close); if (state.focus === ds.close) state.focus = state.open[0] || null; save(); renderTabs(); renderPanes(); return; }
  if (ds.tab) { state.focus = ds.tab; save(); renderTabs(); renderSidebar(); renderPanes(); return; }
  if (ds.split !== undefined) { state.split = !state.split; save(); renderTabs(); renderPanes(); return; }
  if (ds.side) { state.side = state.side === ds.side ? "" : ds.side; save(); renderTabs(); renderSide(); return; }
  if (t.hasAttribute("data-group")) { t.classList.toggle("open"); return; }
  if (ds.closeSheet !== undefined) { closeSheet(); return; }
  if (ds.new !== undefined) return act(pageNewTask);
  if (ds.page) return act({ inbox: pageInbox, rules: pageRules, kb: pageKb, people: pagePeople, setup: pageSetup }[ds.page]);
  if (ds.saveAi !== undefined) return act(async () => { await api("/api/setup/ai", { method: "POST", body: { model: $('[data-ai="model"]').value } }); toast("Đã lưu model"); });
  if (ds.saveProject) return act(async () => {
    const box = t.closest("[data-proj]");
    const v = (n) => ($(`[data-pf="${n}"]`, box).value || "").trim();
    const r = await api("/api/setup/project", { method: "POST", body: { id: ds.saveProject === "new" ? null : ds.saveProject, name: v("name"), path: v("path"), team_id: v("team_id") || null, repo_url: v("repo_url") || null, test: v("test"), build: v("build"), lint: v("lint") } });
    toast(r.imported_rules.length ? `Đã lưu · nhập ${r.imported_rules.length} quy ước từ repo` : "Đã lưu");
    await pageSetup();
  });
  const conn = (n) => ($(`[data-c="${n}"]`)?.value || "").trim();
  if (ds.saveTelegram !== undefined) return act(async () => { const r = await api("/api/setup/telegram", { method: "POST", body: { token: conn("tg_token") } }); toast(`Đã nối bot @${r.bot}. ${r.next}`); if (r.warning) toast(r.warning, true); await pageSetup(); });
  if (ds.telegramOff !== undefined) return act(async () => { const r = await api("/api/setup/telegram/off", { method: "POST" }); toast("Đã tắt Telegram"); if (r.warning) toast(r.warning, true); await pageSetup(); });
  if (ds.saveJira !== undefined) return act(async () => { const r = await api("/api/setup/jira", { method: "POST", body: { base_url: conn("jira_url"), email: conn("jira_email"), token: conn("jira_token") } }); toast(`Jira OK: ${r.user || ""}`); await pageSetup(); });
  if (ds.saveBitbucket !== undefined) return act(async () => { await api("/api/setup/bitbucket", { method: "POST", body: { email: conn("bb_email"), token: conn("bb_token"), workspace: conn("bb_workspace") } }); toast("Bitbucket OK"); await pageSetup(); });
  const pane = t.closest("[data-pane]");
  const key = pane?.dataset.pane;
  if (ds.cmd && key) return act(async () => { await api(`/api/tickets/${key}/control`, { method: "POST", body: { cmd: ds.cmd } }); await refresh(key); });
  if (ds.send !== undefined && key) {
    const box = $("[data-steer]", pane);
    const text = box.value.trim();
    if (!text) return;
    return act(async () => { await api(`/api/tickets/${key}/control`, { method: "POST", body: { cmd: "steer", text } }); box.value = ""; toast("Đã gửi chỉ dẫn"); await refresh(key); });
  }
  const c = t.closest("[data-card]");
  if (c) {
    const id = c.dataset.card;
    const note = ($("[data-text]", c)?.value || "").trim();
    if (ds.answer !== undefined) return act(async () => { await api(`/api/interactions/${id}/answer`, { method: "POST", body: { answer: ds.answer } }); await refresh(key); });
    if (ds.answerText !== undefined && note) return act(async () => { await api(`/api/interactions/${id}/answer`, { method: "POST", body: { answer: note } }); await refresh(key); });
    if (ds.approve !== undefined) return act(async () => { await api(`/api/interactions/${id}/answer`, { method: "POST", body: { approved: ds.approve === "1", note: note || undefined } }); await refresh(key); });
  }
  if (ds.revoke) return act(async () => { await api(`/api/grants/${ds.revoke}/revoke`, { method: "POST" }); renderSide(); });
  if (ds.createTask !== undefined) return act(async () => {
    const f = (n) => ($(`[data-f="${n}"]`).value || "").trim();
    const ticket = await api("/api/tasks", { method: "POST", body: { title: f("title"), description: f("description"), template: f("template"), team_id: f("team_id") || null, project_id: f("project_id") || null, owner_user_id: f("owner_user_id") || null, mode: f("mode") } });
    $("#overlay").hidden = true;
    await loadBoard();
    openTicket(ticket.key);
  });
  if (ds.addRule !== undefined) return act(async () => {
    const r = (n) => ($(`[data-r="${n}"]`).value || "").trim();
    const kind = r("kind");
    const text = r("body");
    const body = { title: r("title"), kind, scope: r("scope"), stage: r("stage") || "any", body: kind === "guideline" ? text : "", spec: kind === "guideline" ? {} : JSON.parse(text || "{}") };
    await api("/api/rules", { method: "POST", body });
    await pageRules();
  });
  if (ds.ruleApprove) return act(async () => { await api(`/api/rules/${ds.ruleApprove}/approve`, { method: "POST" }); await pageRules(); });
  if (ds.ruleDisable) return act(async () => { await api(`/api/rules/${ds.ruleDisable}/disable`, { method: "POST" }); await pageRules(); });
  if (ds.kb) return act(async () => { await api(`/api/kb/${ds.kb}/status`, { method: "POST", body: { status: ds.kbStatus } }); await pageKb(); });
  if (ds.addPerson !== undefined) return act(async () => {
    const p = (n) => ($(`[data-p="${n}"]`).value || "").trim();
    await api("/api/people", { method: "POST", body: { name: p("name"), email: p("email") || null, telegram_id: p("telegram_id") ? Number(p("telegram_id")) : null, roles: p("roles").split(/[\s,]+/).filter(Boolean) } });
    await pagePeople();
  });
  if (ds.addTeam !== undefined) return act(async () => {
    const v = (n) => ($(`[data-t="${n}"]`).value || "").trim();
    await api("/api/teams", { method: "POST", body: { name: v("name"), jira_project_key: v("jira_project_key") || null, confluence_space: v("confluence_space") || null } });
    await pagePeople();
    await loadBoard();
  });
  if (ds.addMember) return act(async () => {
    const user = $(`[data-m-user="${CSS.escape(ds.addMember)}"]`).value;
    const roles = $(`[data-m-roles="${CSS.escape(ds.addMember)}"]`).value.split(/[\s,]+/).filter(Boolean);
    await api(`/api/teams/${ds.addMember}/members`, { method: "POST", body: { user_id: user, roles } });
    await pagePeople();
  });
});

document.addEventListener("keydown", (e) => {
  if (e.key === "Enter" && (e.ctrlKey || e.metaKey) && e.target.matches("[data-steer]")) $("[data-send]", e.target.closest("[data-pane]"))?.click();
  if (e.key === "Escape" && !$("#overlay").hidden) closeSheet();
});

async function refresh(key) {
  await loadBoard();
  if (key && state.open.includes(key)) await loadTicket(key);
  renderPanes();
}

let boardTimer = null;
const refreshing = new Set();
function soon(key) {
  if (key) refreshing.add(key);
  clearTimeout(boardTimer);
  boardTimer = setTimeout(async () => {
    const keys = [...refreshing].filter((k) => state.open.includes(k));
    refreshing.clear();
    await act(loadBoard);
    for (const k of keys) await act(() => loadTicket(k));
    if (keys.some((k) => paneKeys().includes(k))) renderPanes();
  }, 400);
}

function keyOfTicketId(id) {
  return (state.board.tickets.find((t) => t.id === id) || {}).ticket;
}

function connect() {
  const ws = new WebSocket(`${location.protocol === "https:" ? "wss" : "ws"}://${location.host}/api/ws`);
  ws.onmessage = (msg) => {
    const { kind, data } = JSON.parse(msg.data);
    if (kind === "run.event" && data.activity) {
      const key = state.runTicket[data.run_id];
      if (key) {
        (state.acts[data.run_id] ||= []).push({ seq: data.seq, at: data.at, ...data.activity });
        if (paneKeys().includes(key)) renderPane(key);
      }
      return;
    }
    if (kind === "run.started" && data.run?.ticket_id) {
      const key = keyOfTicketId(data.run.ticket_id);
      if (key) { state.runTicket[data.run.id] = key; state.acts[data.run.id] = []; }
      soon(key);
      return;
    }
    if (["ticket.updated", "ticket.stage", "ticket.steps", "interaction.created", "interaction.resolved", "interaction.updated", "run.finished", "access.changed", "ticket.artifact"].includes(kind)) {
      const id = data.ticket?.id || data.ticket_id || data.interaction?.ticket_id || data.run?.ticket_id;
      soon(id ? keyOfTicketId(id) : null);
    }
  };
  ws.onclose = () => setTimeout(connect, 2000);
}

(async () => {
  await act(loadBoard);
  state.open = state.open.filter((k) => state.board.tickets.some((t) => t.ticket === k));
  if (state.focus && !state.open.includes(state.focus)) state.focus = state.open[0] || null;
  renderTabs();
  renderPanes();
  for (const key of paneKeys()) await act(() => loadTicket(key));
  renderPanes();
  connect();
  setInterval(() => act(loadBoard), 30000);
})();
