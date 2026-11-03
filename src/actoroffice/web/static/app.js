/* actor-office web UI — no build step, no external code. Every piece of text is inserted with
   textContent (never innerHTML) so messages written by actors or crawled from the web cannot inject HTML. */
"use strict";

// ---------------------------------------------------------------------------------------------
// helpers
// ---------------------------------------------------------------------------------------------

function h(tag, props, ...children) {
  const el = document.createElement(tag);
  for (const [k, v] of Object.entries(props || {})) {
    if (v === undefined || v === null || v === false) continue;
    if (k === "class") el.className = v;
    else if (k === "text") el.textContent = v;
    else if (k.startsWith("on")) el.addEventListener(k.slice(2), v);
    else if (k === "value") el.value = v;
    else if (k === "checked") el.checked = !!v;
    else el.setAttribute(k, v === true ? "" : v);
  }
  for (const c of children.flat(Infinity)) {
    if (c === undefined || c === null || c === false) continue;
    el.append(c instanceof Node ? c : document.createTextNode(String(c)));
  }
  return el;
}

// Like el.replaceChildren(), but flattens arrays and skips null/false (the native one stringifies them).
function put(el, ...children) {
  el.replaceChildren(...children.flat(Infinity).filter((c) => c !== null && c !== undefined && c !== false));
}

async function api(method, path, body) {
  const res = await fetch(path, {
    method,
    headers: { "Content-Type": "application/json", "X-Requested-With": "actoroffice" },
    body: body === undefined ? undefined : JSON.stringify(body),
    credentials: "same-origin",
  });
  let data = null;
  try { data = await res.json(); } catch (_) { /* empty body */ }
  if (!res.ok) {
    const msg = (data && (data.error || data.detail)) || `HTTP ${res.status}`;
    throw new Error(typeof msg === "string" ? msg : JSON.stringify(msg));
  }
  return data;
}

function toast(text, error) {
  const t = h("div", { class: "toast" + (error ? " error" : ""), text });
  document.getElementById("toast-root").append(t);
  setTimeout(() => t.remove(), error ? 6000 : 3000);
}

async function act(fn) {
  try { return await fn(); } catch (e) { toast(e.message, true); return null; }
}

function time(iso) {
  if (!iso) return "";
  const d = new Date(iso);
  const now = new Date();
  const hm = d.toLocaleTimeString("vi-VN", { hour: "2-digit", minute: "2-digit" });
  return d.toDateString() === now.toDateString() ? hm : `${d.toLocaleDateString("vi-VN")} ${hm}`;
}

function tokens(n) {
  n = Number(n || 0);
  if (n >= 1e6) return (n / 1e6).toFixed(1) + "M";
  if (n >= 1e3) return (n / 1e3).toFixed(1) + "k";
  return String(n);
}

function usageText(u, short) {
  if (!u) return "";
  const total = (u.input_tokens || 0) + (u.output_tokens || 0) + (u.cache_read_tokens || 0) + (u.cache_write_tokens || 0);
  const cost = Number(u.cost_usd || 0);
  if (short) return `${tokens(total)} tok · ≈$${cost.toFixed(3)}`;
  return `${tokens(total)} token (vào ${tokens(u.input_tokens)}, ra ${tokens(u.output_tokens)}, cache đọc ${tokens(u.cache_read_tokens)}, cache ghi ${tokens(u.cache_write_tokens)}) · ≈$${cost.toFixed(3)} quy đổi API`;
}

const store = {
  get(k) { try { return localStorage.getItem(k); } catch (_) { return null; } },
  set(k, v) { try { localStorage.setItem(k, v); } catch (_) { /* private mode */ } },
};

const PALETTE = ["#7c6fd8", "#1d9e75", "#d85a30", "#d4537e", "#378add", "#639922", "#ba7517", "#5f5e5a"];
function colorFor(id) {
  let n = 0;
  for (const ch of id || "") n = (n * 31 + ch.charCodeAt(0)) >>> 0;
  return PALETTE[n % PALETTE.length];
}

function avatar(actor, small) {
  if (!actor) return h("span", { class: "avatar user" + (small ? " sm" : ""), text: "B" });
  const el = h("span", { class: "avatar" + (small ? " sm" : ""), text: actor.emoji || actor.name.slice(0, 1).toUpperCase() });
  if (!actor.emoji) { el.style.background = colorFor(actor.id); el.style.color = "#fff"; }
  else { el.style.background = "var(--panel-2)"; }
  return el;
}

const STATUS_VI = {
  backlog: "backlog", todo: "cần làm", in_progress: "đang làm", in_review: "chờ bạn",
  blocked: "bị chặn", done: "xong", cancelled: "huỷ",
};
const statusPill = (s) => h("span", { class: `status ${s}`, text: STATUS_VI[s] || s });
const ACCESS_VI = { read_only: "chỉ đọc", worktree: "git worktree", direct: "sửa trực tiếp" };
const SCOPE_VI = { shared: "chung", position: "vị trí", actor: "riêng" };
const KIND_VI = { lesson: "bài học", fact: "thông tin", preference: "sở thích", decision: "quyết định" };
const TYPE_VI = {
  task: "việc", feature: "tính năng", design: "giải pháp", bug: "lỗi", change_request: "thay đổi yêu cầu",
  research: "nghiên cứu", analysis: "phân tích", content: "nội dung", automation: "automation", automation_fix: "sửa automation",
};

// Minimal, safe markdown → DOM: code fences, headings, lists, paragraphs, inline code, bold, italic, links, @mentions.
function md(text) {
  const frag = document.createDocumentFragment();
  const parts = String(text || "").split(/```/);
  parts.forEach((part, i) => {
    if (i % 2 === 1) {
      const code = part.replace(/^[\w+-]*\n/, "");
      frag.append(h("pre", {}, h("code", { text: code.replace(/\n$/, "") })));
      return;
    }
    let list = null;
    let para = [];
    const flush = () => {
      if (para.length) { frag.append(h("p", {}, inline(para.join("\n")))); para = []; }
    };
    for (const line of part.split("\n")) {
      const item = line.match(/^\s*(?:[-*•]|\d+[.)])\s+(.*)$/);
      const heading = line.match(/^#{1,4}\s+(.*)$/);
      if (item) {
        flush();
        if (!list) { list = h(/^\s*\d/.test(line) ? "ol" : "ul"); frag.append(list); }
        list.append(h("li", {}, inline(item[1])));
      } else if (heading) {
        flush(); list = null;
        frag.append(h("h4", {}, inline(heading[1])));
      } else if (!line.trim()) {
        flush(); list = null;
      } else {
        list = null;
        para.push(line);
      }
    }
    flush();
  });
  return frag;
}

function inline(text) {
  const out = [];
  const re = /(`[^`]+`)|(\*\*[^*]+\*\*)|(\*[^*\s][^*]*\*)|(\[[^\]]+\]\((https?:\/\/[^\s)]+)\))|(https?:\/\/[^\s<>()]+)|(@[\w.\-À-ỹ]+)/g;
  let last = 0;
  let m;
  while ((m = re.exec(text))) {
    if (m.index > last) out.push(...lines(text.slice(last, m.index)));
    if (m[1]) out.push(h("code", { text: m[1].slice(1, -1) }));
    else if (m[2]) out.push(h("strong", { text: m[2].slice(2, -2) }));
    else if (m[3]) out.push(h("em", { text: m[3].slice(1, -1) }));
    else if (m[4]) out.push(link(m[4].slice(1, m[4].indexOf("]")), m[5]));
    else if (m[6]) out.push(link(m[6], m[6]));
    else if (m[7]) out.push(h("span", { class: "mention", text: m[7] }));
    last = re.lastIndex;
  }
  if (last < text.length) out.push(...lines(text.slice(last)));
  return out;
}

function lines(text) {
  const out = [];
  text.split("\n").forEach((part, i) => { if (i) out.push(h("br")); out.push(part); });
  return out;
}

function link(label, url) {
  return h("a", { href: url, target: "_blank", rel: "noopener noreferrer", text: label });
}

// ---------------------------------------------------------------------------------------------
// state
// ---------------------------------------------------------------------------------------------

const S = {
  actors: [], positions: [], conversations: [], pending: [], runs: {}, projects: [],
  project: null, rateLimit: null, interactions: {}, unread: {}, connected: false,
  conv: null, messages: [], convTickets: [], convUsage: null, runUsage: {},
  timeline: {}, timelineLoaded: {},  // run id → [icon, text, at] lines of what the actor is doing
  view: null, viewId: null,
};

const actorById = (id) => S.actors.find((a) => a.id === id);
const positionById = (id) => S.positions.find((p) => p.id === id);
const projectById = (id) => S.projects.find((p) => p.id === id);
const handleOf = (a) => a.name.replace(/\s+/g, "");
const currentProject = () => projectById(S.project) || S.projects[0];

function setProject(id) {
  if (!id || id === S.project) return;
  S.project = id;
  store.set("ao.project", id);
  renderSidebar();
}

function actorState(id) {
  const a = actorById(id);
  if (a && a.status === "paused") return "paused";
  const runs = Object.values(S.runs).filter((r) => r.actor_id === id);
  if (runs.some((r) => r.kind === "ticket")) return "ticket";
  if (runs.some((r) => r.kind === "chat")) return "chat";
  if (forUser().some((i) => i.actor_id === id)) return "waiting";
  if (S.pending.some((i) => i.actor_id === id && i.kind === "ask_colleague")) return "colleague";
  return "idle";
}
const STATE_VI = { ticket: "đang làm", chat: "đang trả lời", waiting: "chờ bạn", colleague: "chờ đồng nghiệp", paused: "tạm dừng", idle: "rảnh" };
// What actually needs the user (colleague questions are answered by colleagues; the user may step in).
const forUser = () => S.pending.filter((i) => i.kind !== "ask_colleague");

async function loadState() {
  const st = await api("GET", "/api/state");
  S.actors = st.actors;
  S.positions = st.positions;
  S.projects = st.projects;
  S.conversations = st.conversations;
  S.pending = st.pending;
  S.rateLimit = st.rate_limit;
  S.suggestedName = st.suggested_name || "";
  S.runs = {};
  for (const r of st.runs) S.runs[r.id] = { ...r, step: "" };
  for (const i of st.pending) S.interactions[i.id] = i;
  const saved = store.get("ao.project");
  S.project = projectById(saved) ? saved : st.default_project_id;
}

// ---------------------------------------------------------------------------------------------
// live events
// ---------------------------------------------------------------------------------------------

let socket = null;
let retry = 0;

function connect() {
  const proto = location.protocol === "https:" ? "wss" : "ws";
  socket = new WebSocket(`${proto}://${location.host}/api/ws`);
  socket.onopen = async () => {
    retry = 0; S.connected = true;
    if (!S.booted) { renderSidebar(); return; }  // the first load renders once, below; this is for reconnects
    await act(loadState);
    render();
  };
  socket.onclose = () => {
    S.connected = false; renderSidebar();
    setTimeout(connect, Math.min(10000, 500 * 2 ** retry++));
  };
  socket.onmessage = (e) => onEvent(JSON.parse(e.data));
}

function onEvent({ kind, data }) {
  switch (kind) {
    case "chat.message": {
      const m = data.message;
      if (S.conv && m.conversation_id === S.conv.id) {
        if (!S.messages.some((x) => x.id === m.id)) { S.messages.push(m); appendMessage(m); }
      } else if (m.author_kind !== "user") {
        S.unread[m.conversation_id] = (S.unread[m.conversation_id] || 0) + 1;
      }
      const c = S.conversations.find((x) => x.id === m.conversation_id);
      if (c) { c.updated_at = m.created_at; c.last_message_id = m.id; }
      renderSidebar();
      break;
    }
    case "chat.conversation": {
      const conv = data.conversation;
      const i = S.conversations.findIndex((c) => c.id === conv.id);
      if (i >= 0) S.conversations[i] = { ...S.conversations[i], ...conv }; else S.conversations.unshift(conv);
      if (S.conv && S.conv.id === conv.id) { S.conv = conv; renderConversationHeader(); renderDetails(); }
      renderSidebar();
      break;
    }
    case "run.started": {
      const r = data.run;
      S.runs[r.id] = { ...r, step: "bắt đầu…" };
      S.timeline[r.id] = [];
      refreshLive(); renderSidebar();
      if (S.view === "activity") renderMain();
      break;
    }
    case "run.event": {
      const r = S.runs[data.run_id];
      if (!r) break;
      const p = data.payload || {};
      if (data.kind === "tool_use") r.step = describeTool(p.tool, p.input);
      else if (data.kind === "text" && !p.subagent) r.step = String(p.text || "").replace(/\s+/g, " ").slice(0, 120);
      else if (data.kind === "rate_limit") { S.rateLimit = p; renderSidebar(); }
      const line = eventLine(data.kind, p);
      if (line) {
        const tl = (S.timeline[data.run_id] = S.timeline[data.run_id] || []);
        tl.push([...line, new Date().toISOString()]);
        if (tl.length > 300) tl.splice(0, tl.length - 300);
        refreshTimeline(data.run_id);
      }
      refreshLive();
      break;
    }
    case "run.finished": {
      delete S.runs[data.run_id];
      const run = data.run;
      if (run && S.conv) {
        const mine = run.conversation_id === S.conv.id || S.convTickets.some((t) => t.id === run.ticket_id);
        if (mine) {
          S.runUsage[run.id] = runUsageOf(run);
          addUsage(S.convUsage, run);
          for (const key of Object.keys(S.ticketWork || {})) delete S.ticketWork[key];  // totals changed: reload lazily
          renderConversationHeader(); refreshUsageLabels();
        }
      }
      delete S.timeline[data.run_id];
      refreshLive(); renderSidebar();
      if (S.view === "ticket" || S.view === "tickets" || S.view === "actor" || S.view === "activity") renderMain();
      break;
    }
    case "interaction.created": {
      const i = data.interaction;
      S.interactions[i.id] = i;
      if (!S.pending.some((x) => x.id === i.id)) S.pending.push(i);
      renderSidebar(); refreshCards();
      if (S.view === "inbox" || (S.view === "ticket" && S.ticketId === i.ticket_id)) renderMain();
      break;
    }
    case "interaction.resolved": {
      const i = data.interaction;
      S.interactions[i.id] = i;
      S.pending = S.pending.filter((x) => x.id !== i.id);
      renderSidebar(); refreshCards();
      if (S.view === "inbox") renderMain();
      break;
    }
    case "ticket.created":
    case "ticket.updated": {
      const t = data.ticket;
      if (S.conv && t.origin_conversation_id === S.conv.id) {
        const i = S.convTickets.findIndex((x) => x.id === t.id);
        if (i >= 0) S.convTickets[i] = t; else S.convTickets.unshift(t);
        renderDetails();
      }
      if (S.view === "tickets" || (S.view === "ticket" && (S.ticketKey === t.key || S.ticketId === t.id))) renderMain();
      break;
    }
    case "ticket.comment":
    case "ticket.document":
      if (S.view === "ticket" && S.ticketId === data.ticket_id) renderMain();
      break;
    case "memory.deleted":
      document.querySelectorAll(`[data-memory="${data.memory_id}"]`).forEach((n) => n.classList.add("undone"));
      if (S.view === "actor") renderMain();
      break;
    case "memory.saved":
    case "skill.saved":
    case "skill.deleted":
      if (S.view === "actor" || S.view === "skills") renderMain();
      break;
    case "team.changed":
    case "project.changed":
      act(async () => { await loadState(); render(); });
      break;
    case "automation.changed":
    case "automation.run":
      if (S.view === "automations" || S.view === "automation") renderMain();
      break;
    case "ticket.change":
      if (S.view === "ticket") renderMain();
      break;
  }
  document.title = (forUser().length ? `(${forUser().length}) ` : "") + "Văn phòng AI";
}

function addUsage(total, run) {
  if (!total || !run || !run.usage) return;
  total.runs = (total.runs || 0) + 1;
  for (const k of ["input_tokens", "output_tokens", "cache_read_tokens", "cache_write_tokens"]) total[k] = (total[k] || 0) + (run.usage[k] || 0);
  total.cost_usd = (total.cost_usd || 0) + (run.cost_usd || 0);
}

function describeTool(tool, input) {
  const name = String(tool || "").replace(/^mcp__office__/, "");
  const target = input && (input.command || input.file_path || input.path || input.url || input.pattern || input.title || input.key || input.ticket || input.name || input.query);
  const vi = {
    ticket_get: "đọc ticket", ticket_comment: "ghi comment", ticket_set_status: "cập nhật trạng thái",
    ticket_create: "tạo ticket", doc_get: "đọc tài liệu", doc_put: "viết tài liệu", ask_user: "hỏi bạn",
    list_actors: "xem đồng nghiệp", tickets_list: "xem danh sách ticket", propose_ticket: "đề xuất ticket",
    chat_create_group: "tạo nhóm", work_product_add: "ghi kết quả", memory_save: "ghi nhớ", memory_search: "lục trí nhớ",
    skill_list: "xem skill", skill_read: "đọc skill", skill_save: "lưu skill",
    ask_colleague: "hỏi đồng nghiệp", change_notify: "báo thay đổi yêu cầu",
    automation_save: "lưu automation", automation_stop: "dừng automation",
    automation_list: "xem automation", automation_run: "chạy automation", db_schema: "xem cấu trúc DB", db_query: "truy vấn DB",
    list_dir: "xem thư mục", history_search: "tìm lịch sử", todo: "cập nhật việc cần làm", TodoWrite: "lập danh sách việc",
    Read: "đọc", Write: "ghi file", Edit: "sửa file", Bash: "chạy lệnh", Glob: "tìm file", Grep: "tìm nội dung",
    WebFetch: "đọc web", WebSearch: "tìm web",
  }[name] || name;
  return target ? `${vi}: ${String(target).slice(0, 90)}` : vi;
}

// ---- activity: what actors are doing, step by step -------------------------------------------

function eventLine(kind, p) {
  p = p || {};
  if (p.subagent) return null;
  const one = (t, n) => { t = String(t || "").replace(/\s+/g, " ").trim(); return t.length > n ? t.slice(0, n) + "…" : t; };
  if (kind === "thinking") return ["💭", one(p.text, 400)];
  if (kind === "text") { const t = one(p.text, 300); return t ? ["💬", t] : null; }
  if (kind === "tool_use") return ["🔧", describeTool(p.tool, p.input)];
  if (kind === "tool_result" && p.error) return ["⚠️", "lỗi: " + one(p.text, 200)];
  if (kind === "compacted") return ["🗜", "gọn lại ngữ cảnh để làm tiếp"];
  return null;
}

function linesOf(events) {
  return (events || []).map((e) => { const l = eventLine(e.kind, e.payload); return l ? [...l, e.at] : null; }).filter(Boolean);
}

function timelineNode(lines, runId, last) {
  const shown = last ? lines.slice(-last) : lines;
  return h("div", { class: "timeline", "data-tl": runId || null },
    shown.length ? shown.map(([icon, text, at]) => h("div", { class: `tl ${icon === "💭" ? "think" : icon === "⚠️" ? "err" : ""}` },
      h("span", { class: "at", text: at ? time(at) : "" }), h("span", { class: "ic", text: icon }), h("span", { class: "tx", text })))
      : h("div", { class: "sub", text: "Chưa có bước nào." }));
}

function refreshTimeline(runId) {
  const node = document.querySelector(`[data-tl="${runId}"]`);
  if (node) node.replaceWith(timelineNode(S.timeline[runId] || [], runId, 40));
}

function runWhat(r) {
  if (r.ticket_key) return `${r.ticket_key} · ${r.ticket_title || ""}`;
  if (r.conversation_title) return `chat: ${r.conversation_title}`;
  return r.kind === "system" ? "việc hệ thống" : r.kind;
}

const WORK_RUN_VI = { running: "đang chạy", succeeded: "xong", failed: "lỗi", cancelled: "đã dừng", deferred: "chờ bạn duyệt", rate_limited: "hết quota", queued: "chờ" };

async function renderActivity() {
  const main = document.getElementById("main");
  const seq = S.gen;
  const recent = (await act(() => api("GET", "/api/runs?limit=50"))) || [];
  for (const id of Object.keys(S.runs)) {
    if (S.timelineLoaded[id]) continue;
    const events = await api("GET", `/api/runs/${id}/events`).catch(() => []);
    S.timeline[id] = [...linesOf(events), ...(S.timeline[id] || [])];
    S.timelineLoaded[id] = true;
  }
  if (S.view !== "activity" || seq !== S.gen) return;
  const info = Object.fromEntries(recent.map((r) => [r.id, r]));
  const running = Object.values(S.runs);
  const runningCard = (r) => {
    const a = actorById(r.actor_id);
    const meta = { ...r, ...(info[r.id] || {}) };
    const secs = r.started_at ? Math.round((Date.now() - new Date(r.started_at)) / 1000) : null;
    return h("div", { class: "box run-live" },
      h("div", { class: "row-inline" }, h("span", { class: "spinner" }), avatar(a),
        h("strong", { text: a ? a.name : "?" }), h("span", { class: "sub", text: `${runWhat(meta)} · ${duration(secs)}` }),
        h("button", { class: "btn sm danger", text: "Dừng", onclick: () => act(() => api("POST", `/api/runs/${r.id}/stop`)) })),
      timelineNode(S.timeline[r.id] || [], r.id, 40));
  };
  const recentRow = (r) => {
    const a = actorById(r.actor_id);
    const holder = h("div", { class: "events", text: "Đang tải…" });
    const secs = r.started_at && r.finished_at ? Math.round((new Date(r.finished_at) - new Date(r.started_at)) / 1000) : null;
    const det = h("details", { class: "doc" },
      h("summary", { text: `${time(r.started_at)} · ${a ? a.name : "?"} · ${runWhat(r)} · ${WORK_RUN_VI[r.status] || r.status} · ${workParts({ ...r.usage, cost_usd: r.cost_usd, duration_s: secs }, r.stats || {}).join(" · ")}${r.error ? " · " + r.error : ""}` }),
      holder);
    det.addEventListener("toggle", async () => {
      if (!det.open || holder.dataset.loaded) return;
      holder.dataset.loaded = "1";
      holder.replaceWith(timelineNode(linesOf(await act(() => api("GET", `/api/runs/${r.id}/events`)))));
    });
    return det;
  };
  put(main,
    h("div", { class: "header" }, h("h1", { text: "Hoạt động" }), h("span", { class: "meta", text: "Nhân viên đang làm gì, từng bước; bấm một lượt cũ để xem lại" })),
    h("div", { class: "page" },
      h("h4", { text: `Đang làm (${running.length})` }),
      running.length ? running.map(runningCard) : h("div", { class: "sub", text: "Không ai đang chạy." }),
      h("h4", { text: "Gần đây" }),
      h("div", { class: "box" }, recent.filter((r) => r.status !== "running").map(recentRow))));
}

// ---------------------------------------------------------------------------------------------
// routing
// ---------------------------------------------------------------------------------------------

function route() {
  const [, kind, id] = location.hash.match(/^#\/(\w+)\/?(.*)$/) || [];
  return { kind: kind || "", id: decodeURIComponent(id || "") };
}

function go(hash) { if (location.hash !== hash) location.hash = hash; else render(); }

window.addEventListener("hashchange", () => render());

function render() {
  renderSidebar();
  renderMain();
}

// ---------------------------------------------------------------------------------------------
// sidebar
// ---------------------------------------------------------------------------------------------

function dmFor(actorId, projectId) {
  return S.conversations.find((c) => c.kind === "dm" && c.project_id === projectId && c.members.some((m) => m.kind === "actor" && m.id === actorId));
}

function renderSidebar() {
  const r = route();
  const sb = document.getElementById("sidebar");
  const project = currentProject();
  const nav = (hash, children, active) => h("a", { class: "nav" + (active ? " active" : ""), href: hash }, children);

  const projectSel = h("select", { class: "project-select", title: "Dự án",
    onchange: (e) => { setProject(e.target.value); if (S.view === "conv" || S.view === "tickets") go(S.view === "tickets" ? "#/tickets" : "#/"); } },
    S.projects.map((p) => h("option", { value: p.id, text: p.name, selected: project && p.id === project.id })));

  const dms = S.actors.map((a) => {
    const conv = project ? dmFor(a.id, project.id) : null;
    const st = actorState(a.id);
    const unread = conv ? S.unread[conv.id] : 0;
    const pos = positionById(a.position_id);
    return h("a", {
      class: "nav" + (conv && r.kind === "c" && r.id === conv.id ? " active" : ""),
      href: conv ? `#/c/${conv.id}` : `#/dm/${a.id}`,
      title: STATE_VI[st],
    },
      avatar(a, true),
      h("span", { class: "label" }, a.name, " ", h("span", { class: "sub", text: pos ? pos.title : "" })),
      unread ? h("span", { class: "badge", text: unread }) : null,
      h("span", { class: `dot ${st}` }));
  });

  const groups = S.conversations.filter((c) => c.kind === "group" && project && c.project_id === project.id).map((c) =>
    nav(`#/c/${c.id}`, [
      h("span", { class: "label", text: `# ${c.title}` }),
      S.unread[c.id] ? h("span", { class: "badge", text: S.unread[c.id] }) : null,
    ], r.kind === "c" && r.id === c.id));

  const otherUnread = Object.entries(S.unread).filter(([id, n]) => n && (S.conversations.find((c) => c.id === id) || {}).project_id !== (project && project.id)).length;

  put(sb,
    h("div", { class: "brand" }, h("span", { class: "conn" + (S.connected ? " on" : ""), title: S.connected ? "đã kết nối" : "mất kết nối" }), "Văn phòng AI"),
    h("div", { class: "project-row" }, projectSel,
      h("button", { class: "icon-btn", title: "Sửa dự án", text: "⚙", onclick: () => projectModal(project) }),
      h("button", { class: "icon-btn", title: "Dự án mới", text: "+", onclick: () => projectModal(null) })),
    project && project.path ? h("div", { class: "project-path", title: project.path, text: `${ACCESS_VI[project.access]} · ${project.path}` }) : null,
    otherUnread ? h("div", { class: "project-path", text: `${otherUnread} cuộc trò chuyện ở dự án khác có tin mới` }) : null,
    nav("#/inbox", [h("span", { class: "label", text: "Đang chờ bạn" }), forUser().length ? h("span", { class: "badge warn", text: forUser().length }) : null], r.kind === "inbox"),
    nav("#/activity", [h("span", { class: "label", text: "Hoạt động" }), Object.keys(S.runs).length ? h("span", { class: "badge", text: Object.keys(S.runs).length }) : null], r.kind === "activity"),
    h("div", { class: "section" }, h("span", { text: "Tin nhắn" })),
    dms.length ? dms : h("div", { class: "nav sub", text: "Chưa có nhân viên" }),
    h("div", { class: "section" }, h("span", { text: "Nhóm" }), h("button", { title: "Tạo nhóm", text: "+", onclick: newGroupModal })),
    groups.length ? groups : h("div", { class: "nav sub", text: "Chưa có nhóm" }),
    h("div", { class: "section" }, h("span", { text: "Quản lý" })),
    nav("#/tickets", [h("span", { class: "label", text: "Tickets" })], r.kind === "tickets" || r.kind === "t"),
    nav("#/team", [h("span", { class: "label", text: "Nhân sự" })], r.kind === "team" || r.kind === "a"),
    nav("#/skills", [h("span", { class: "label", text: "Skills" })], r.kind === "skills"),
    nav("#/automations", [h("span", { class: "label", text: "Automation" })], r.kind === "automations" || r.kind === "au"),
    quotaLine(),
  );
}

function quotaLine() {
  const rl = S.rateLimit;
  if (!rl) return h("div", { class: "quota", text: "Quota: chưa có dữ liệu" });
  const st = { allowed: "ổn", allowed_warning: "sắp hết", rejected: "đã hết" }[rl.status] || rl.status;
  const reset = rl.resets_at ? ` · reset ${time(new Date(rl.resets_at * 1000).toISOString())}` : "";
  const pct = rl.utilization != null ? ` ${Math.round(rl.utilization * 100)}%` : "";
  return h("div", { class: `quota ${rl.status}`, text: `Quota ${rl.kind === "five_hour" ? "5 giờ" : rl.kind || ""}: ${st}${pct}${reset}` });
}

async function openDm(actorId, fromRoute) {
  const project = currentProject();
  const conv = await act(() => api("POST", "/api/conversations", { kind: "dm", actor_id: actorId, project_id: project && project.id }));
  if (!conv) return;
  if (!S.conversations.some((c) => c.id === conv.id)) S.conversations.unshift(conv);
  if (fromRoute) location.replace(`#/c/${conv.id}`);  // so "back" does not bounce through #/dm/
  else go(`#/c/${conv.id}`);
}

// ---------------------------------------------------------------------------------------------
// main area
// ---------------------------------------------------------------------------------------------

async function renderMain() {
  S.gen = (S.gen || 0) + 1;  // only a new main render makes the in-flight ones obsolete
  S.loadingSince = Date.now();
  const r = route();
  const app = document.getElementById("app");
  if (r.kind === "dm") { openDm(r.id, true); return; }
  app.classList.toggle("no-details", r.kind !== "c");
  const previous = `${S.view}:${S.viewId}`;
  S.view = {
    c: "conv", inbox: "inbox", tickets: "tickets", t: "ticket", team: "team", a: "actor", skills: "skills",
    automations: "automations", au: "automation", activity: "activity",
  }[r.kind] || "home";
  S.viewId = r.id;
  if (S.view !== "conv") {
    S.conv = null;
    document.getElementById("details").replaceChildren();
    if (previous !== `${S.view}:${S.viewId}`) put(document.getElementById("main"), h("div", { class: "empty", text: "Đang tải…" }));
  }
  if (S.view === "conv") return renderConversation(r.id);
  if (S.view === "inbox") return renderInbox();
  if (S.view === "tickets") return renderTickets();
  if (S.view === "ticket") return renderTicket(r.id);
  if (S.view === "team") return renderTeam();
  if (S.view === "actor") return renderActor(r.id);
  if (S.view === "skills") return renderSkills();
  if (S.view === "automations") return renderAutomations();
  if (S.view === "automation") return renderAutomation(r.id);
  if (S.view === "activity") return renderActivity();
  return renderHome();
}

function renderHome() {
  const main = document.getElementById("main");
  const project = currentProject();
  const first = S.conversations.find((c) => project && c.project_id === project.id);
  if (first) { location.replace(`#/c/${first.id}`); return; }
  put(main,
    h("div", { class: "header" }, h("h1", { text: project ? project.name : "Chào mừng" })),
    h("div", { class: "empty" },
      S.actors.length
        ? h("p", { text: "Chọn một nhân viên ở cột trái để nhắn tin, hoặc tạo nhóm cho dự án này." })
        : [h("p", { text: "Văn phòng chưa có ai. Hãy tuyển nhân viên đầu tiên (ví dụ một PM hoặc Writer), rồi nhắn tin cho họ." }),
           h("button", { class: "btn primary", text: "Tuyển nhân viên", onclick: () => go("#/team") })]));
}

// ---- conversation ---------------------------------------------------------------------------

async function renderConversation(id) {
  const main = document.getElementById("main");
  if (!S.conv || S.conv.id !== id) {
    put(main, h("div", { class: "empty", text: "Đang tải…" }));
    const data = await act(() => api("GET", `/api/conversations/${encodeURIComponent(id)}`));
    if (!data) return;
    S.conv = data.conversation;
    S.messages = data.messages;
    S.convTickets = data.tickets;
    S.convUsage = data.usage;
    S.runUsage = data.run_usage;
    S.ticketWork = data.ticket_work || {};
    if (S.conv.project_id) setProject(S.conv.project_id);
  }
  S.unread[id] = 0;
  renderSidebar();

  const list = h("div", { class: "scroll", id: "messages" });
  const live = h("div", { class: "live", id: "live" });
  put(main, h("div", { class: "header", id: "conv-header" }), list, live, composer());
  renderConversationHeader();
  for (const m of S.messages) list.append(messageNode(m));
  if (!S.messages.length) list.append(h("div", { class: "empty", text: S.conv.kind === "dm" ? "Hãy nói gì đó để bắt đầu." : "Dùng @tên để gọi ai đó trong nhóm." }));
  list.scrollTop = list.scrollHeight;
  refreshLive();
  renderDetails();
  document.getElementById("composer-input")?.focus();
}

function renderConversationHeader() {
  const head = document.getElementById("conv-header");
  if (!head || !S.conv) return;
  const actors = S.conv.members.filter((m) => m.kind === "actor").map((m) => actorById(m.id)).filter(Boolean);
  const usage = S.convUsage && S.convUsage.runs ? h("span", { class: "usage-chip", title: usageText(S.convUsage), text: `Σ ${usageText(S.convUsage, true)}` }) : null;
  if (S.conv.kind === "dm" && actors[0]) {
    const a = actors[0];
    const pos = positionById(a.position_id);
    put(head, avatar(a), h("h1", {}, h("a", { href: `#/a/${a.id}`, class: "plain", text: a.name })),
      h("span", { class: "meta", text: [pos && pos.title, a.specialty, STATE_VI[actorState(a.id)]].filter(Boolean).join(" · ") }), usage);
  } else {
    put(head, h("h1", { text: `# ${S.conv.title}` }), h("span", { class: "meta", text: `Bạn, ${actors.map((a) => a.name).join(", ")}` }),
      S.conv.ticket_id ? h("a", { class: "btn sm", href: `#/t/${S.conv.ticket_id}`, text: "Mở ticket" }) : null, usage);
  }
}

function appendMessage(m) {
  const list = document.getElementById("messages");
  if (!list) return;
  const nearBottom = list.scrollHeight - list.scrollTop - list.clientHeight < 120;
  list.querySelector(".empty")?.remove();
  list.append(messageNode(m));
  if (nearBottom || m.author_kind === "user") list.scrollTop = list.scrollHeight;
}

function messageNode(m) {
  if (m.kind === "interaction") return interactionCard(m.meta.interaction_id);
  if (m.kind === "memory") return memoryCard(m);
  if (m.kind === "ticket") {
    const note = m.meta && m.meta.note;
    return h("div", { class: "ticket-event" },
      h("div", {},
        h("a", { class: "chip", href: `#/t/${m.meta.ticket_key}` }, statusPill(m.meta.status), m.body),
        note ? h("span", { class: "note", text: note.length > 220 ? note.slice(0, 220) + "…" : note }) : null));
  }
  if (m.author_kind === "system") return h("div", { class: "sys", text: m.body });
  const a = m.author_kind === "actor" ? actorById(m.author_id) : null;
  const pos = a ? positionById(a.position_id) : null;
  const runId = m.meta && m.meta.run_id;
  const report = m.meta && m.meta.report;
  return h("div", { class: "msg" + (report ? " report" : "") },
    avatar(a),
    h("div", { class: "body" },
      h("div", { class: "who" },
        a ? h("a", { href: `#/a/${a.id}`, class: "plain", text: a.name }) : "Bạn",
        pos ? h("span", { class: "tag", text: pos.title }) : null,
        h("span", { class: "time", text: time(m.created_at) }),
        null,
        report ? h("a", { class: "tag", href: `#/t/${m.meta.ticket_key}`, text: `báo cáo ${m.meta.ticket_key}` }) : null),
      h("div", { class: "text" }, md(m.body)),
      runId ? workNode(runId) : report ? ticketWorkNode(m.meta.ticket_key) : null));
}

// ---- what each message cost and did -----------------------------------------------------------

function runUsageOf(run) {
  const secs = run.started_at && run.finished_at ? Math.round((new Date(run.finished_at) - new Date(run.started_at)) / 1000) : null;
  return { ...run.usage, cost_usd: run.cost_usd, stats: run.stats || {}, duration_s: secs };
}

function duration(secs) {
  if (secs == null) return "";
  if (secs < 60) return `${secs}s`;
  if (secs < 3600) return `${Math.floor(secs / 60)} phút ${secs % 60 ? (secs % 60) + "s" : ""}`.trim();
  return `${Math.floor(secs / 3600)} giờ ${Math.round((secs % 3600) / 60)} phút`;
}

function workParts(u, s) {
  const parts = [];
  if (s.files_changed) parts.push(`📄 ${s.files_changed} file · +${s.lines_added || 0} −${s.lines_removed || 0} dòng`);
  if (s.commands) parts.push(`⌘ ${s.commands} lệnh`);
  if (s.web) parts.push(`🌐 ${s.web} lần đọc web`);
  if (s.reads) parts.push(`🔎 ${s.reads} lần đọc file`);
  if (u.duration_s) parts.push(`⏱ ${duration(u.duration_s)}`);
  parts.push(usageText(u, true));
  return parts;
}

function workTitle(s) {
  const files = Object.entries(s.files || {}).map(([path, c]) => `${path}  +${c.added} −${c.removed}`);
  const cmds = (s.command_samples || []).map((c) => `$ ${c}`);
  return [...files, ...(cmds.length ? ["", ...cmds] : [])].join("\n");
}

function workNode(runId) {
  const u = S.runUsage[runId];
  if (!u) return h("div", { class: "work", "data-run": runId });
  const s = u.stats || {};
  return h("div", { class: "work", "data-run": runId, title: workTitle(s) || null }, workParts(u, s).join(" · "));
}

function ticketWorkNode(key) {
  const node = h("div", { class: "work", "data-work": key });
  const fill = (w) => {
    const who = w.actors > 1 ? ` · ${w.actors} người` : "";
    put(node, `Σ ${key}: ${w.runs} lượt chạy${who} · ` + workParts(w, w).join(" · "));
  };
  S.ticketWork = S.ticketWork || {};
  if (S.ticketWork[key]) fill(S.ticketWork[key]);
  else api("GET", `/api/tickets/${encodeURIComponent(key)}/work`).then((w) => { S.ticketWork[key] = w; fill(w); }).catch(() => {});
  return node;
}

function refreshUsageLabels() {
  document.querySelectorAll("[data-run]").forEach((n) => { if (S.runUsage[n.dataset.run]) n.replaceWith(workNode(n.dataset.run)); });
  document.querySelectorAll("[data-work]").forEach((n) => n.replaceWith(ticketWorkNode(n.dataset.work)));
}

function memoryCard(m) {
  const id = m.meta && m.meta.memory_id;
  const node = h("div", { class: "sys memory", "data-memory": id },
    h("span", { text: m.body }), " ",
    h("button", { class: "linkish", text: "Hoàn tác", onclick: () => act(async () => {
      await api("DELETE", `/api/memories/${id}`);
      node.classList.add("undone");
      toast("Đã xoá ghi nhớ");
    }) }));
  return node;
}

function refreshLive() {
  const live = document.getElementById("live");
  if (!live || !S.conv) return;
  const ticketIds = new Set(S.convTickets.map((t) => t.id));
  const runs = Object.values(S.runs).filter((r) =>
    (r.kind === "chat" && r.conversation_id === S.conv.id) || (r.kind === "ticket" && ticketIds.has(r.ticket_id)));
  put(live, runs.map((r) => {
    const a = actorById(r.actor_id);
    const t = S.convTickets.find((x) => x.id === r.ticket_id);
    const what = r.kind === "chat" ? "đang trả lời" : `đang làm ${t ? t.key : ""}`;
    return h("div", { class: "row" },
      h("span", { class: "spinner" }),
      h("strong", { text: a ? a.name : "?" }), h("span", { text: what }),
      h("span", { class: "step", text: r.step ? `· ${r.step}` : "" }),
      h("a", { class: "btn sm", href: "#/activity", text: "Xem tiến độ" }),
      h("button", { class: "btn sm danger", text: "Dừng", onclick: () => act(() => api("POST", `/api/runs/${r.id}/stop`)) }));
  }));
}

// ---- interaction cards (approvals, questions, ticket proposals) -----------------------------

function refreshCards() {
  document.querySelectorAll("[data-interaction]").forEach((node) => node.replaceWith(interactionCard(node.dataset.interaction)));
}

function interactionCard(id) {
  const item = S.interactions[id];
  const holder = h("div", { "data-interaction": id });
  if (!item) {
    holder.append(h("div", { class: "sys", text: "…" }));
    api("GET", `/api/interactions/${id}`).then((i) => { S.interactions[id] = i; refreshCards(); }).catch(() => {});
    return holder;
  }
  holder.append(cardBody(item));
  return holder;
}

function cardBody(item) {
  const p = item.payload || {};
  const pending = item.status === "pending";
  const answer = (body) => act(async () => {
    const resolved = await api("POST", `/api/interactions/${item.id}/answer`, body);
    S.interactions[item.id] = resolved;
    S.pending = S.pending.filter((x) => x.id !== item.id);
    refreshCards(); renderSidebar();
    if (body.remember) toast(`Từ giờ ${p.actor || "actor"} được phép: ${body.remember}`);
  });
  const doneLine = (text) => h("div", { class: "done", text });

  if (item.kind === "approve_tool") {
    let verdict = item.status;
    if (item.response) verdict = item.response.approved ? (item.response.remember ? `Đã cho phép và ghi nhớ: ${item.response.remember}` : "Đã cho phép 1 lần") : "Đã từ chối";
    const label = { exact: "Luôn cho phép lệnh này", prefix: "Luôn cho phép", tool: "Luôn cho phép mọi" };
    return h("div", { class: "card warn" },
      h("div", { class: "title", text: `${p.actor || ""} xin phép: ${p.tool}` }),
      h("pre", {}, h("code", { text: p.target || JSON.stringify(p.input || {}, null, 1) })),
      h("div", { class: "reason", text: `${p.reason || ""}${p.ticket ? " · " + p.ticket : ""}` }),
      pending ? [
        h("div", { class: "actions" },
          h("button", { class: "btn primary", text: "Cho phép 1 lần", onclick: () => answer({ approved: true }) }),
          h("button", { class: "btn danger", text: "Từ chối", onclick: () => answer({ approved: false }) })),
        (p.suggestions || []).length ? h("div", { class: "actions remember" },
          (p.suggestions || []).map((s) => h("button", {
            class: "btn sm", title: s.rule,
            text: s.scope === "prefix" ? `${label.prefix} ${s.rule.replace(/^\w+\(|\)$/g, "")}` : s.scope === "tool" ? `${label.tool} ${s.rule}` : label.exact,
            onclick: () => answer({ approved: true, remember: s.rule }),
          }))) : null,
      ] : doneLine(verdict));
  }

  if (item.kind === "question") {
    const input = h("input", { type: "text", placeholder: "Câu trả lời của bạn" });
    const send = () => { const v = input.value.trim(); if (v) answer({ answer: v }); };
    input.addEventListener("keydown", (e) => { if (e.key === "Enter") send(); });
    return h("div", { class: "card ask" },
      h("div", { class: "title", text: `${p.asked_by || ""} hỏi bạn` }),
      h("div", { class: "md" }, md(p.question || "")),
      pending ? [
        (p.options || []).length ? h("div", { class: "actions" }, p.options.map((o) => h("button", { class: "btn", text: o, onclick: () => answer({ answer: o }) }))) : null,
        h("div", { class: "row-inline" }, input, h("button", { class: "btn primary", text: "Gửi", onclick: send })),
      ] : doneLine(`Bạn trả lời: ${item.response ? item.response.answer : item.status}`));
  }

  if (item.kind === "confirm_ticket") {
    const title = h("input", { type: "text", value: p.title || "" });
    const desc = h("textarea", { rows: 4 });
    desc.value = p.description || "";
    return h("div", { class: "card ask" },
      h("div", { class: "title", text: `${p.proposed_by} đề xuất ticket → ${p.assignee}` }),
      pending ? [
        title, desc,
        h("div", { class: "actions" },
          h("button", { class: "btn primary", text: "Tạo ticket", onclick: () => answer({ confirmed: true, title: title.value.trim(), description: desc.value }) }),
          h("button", { class: "btn", text: "Bỏ", onclick: () => answer({ confirmed: false }) })),
      ] : [
        h("div", { class: "md" }, md(`**${p.title}**`)),
        item.response && item.response.ticket_key
          ? h("div", { class: "done" }, "Đã tạo ", h("a", { href: `#/t/${item.response.ticket_key}`, text: item.response.ticket_key }))
          : doneLine(item.status === "answered" ? "Đã bỏ" : item.status),
      ]);
  }
  if (item.kind === "propose_tickets") return proposalCard(item, answer);
  if (item.kind === "approve_automation") {
    let verdict = item.status;
    if (item.response) verdict = item.response.approved ? "Đã duyệt và bật" : "Đã từ chối";
    return h("div", { class: "card warn" },
      h("div", { class: "title", text: `${p.owner || ""} xin duyệt automation: ${p.name}` }),
      h("div", { class: "md" }, md(`${p.description || ""}\n\nLịch: **${p.schedule_text || "chạy tay"}**`)),
      h("ol", { class: "steps" }, (p.steps || []).map((st) => h("li", { text: stepLabel(st) }))),
      Object.entries(p.code || {}).map(([file, code]) => h("details", { class: "doc", open: pending }, h("summary", { text: `Code ${file} (đọc trước khi duyệt)` }), h("pre", {}, h("code", { text: code })))),
      h("div", { class: "reason", text: "Script chạy không cần LLM, có giới hạn thời gian và môi trường đã lọc biến bí mật; bước giao việc tạo ticket cho nhân viên mỗi lần chạy. Chỉ duyệt khi bạn hiểu flow." }),
      pending ? h("div", { class: "actions" },
        h("button", { class: "btn primary", text: "Duyệt & chạy thử", onclick: () => answer({ approved: true, run_now: true }) }),
        h("button", { class: "btn", text: "Duyệt, chưa chạy", onclick: () => answer({ approved: true, run_now: false }) }),
        h("button", { class: "btn danger", text: "Từ chối", onclick: () => answer({ approved: false }) }))
        : h("div", { class: "done" }, verdict, " · ", h("a", { href: `#/au/${p.name}`, text: "mở automation" })));
  }

  if (item.kind === "approve_access") {
    const mode = { worktree: "🌿 Git worktree (an toàn: mỗi việc một nhánh, bạn duyệt rồi merge)", direct: "✏️ Sửa trực tiếp trong thư mục" };
    let verdict = item.status;
    if (item.response) verdict = item.response.approved ? `Đã cho phép (${item.response.access === "worktree" ? "git worktree" : "sửa trực tiếp"})` : "Không cho phép";
    return h("div", { class: "card warn" },
      h("div", { class: "title", text: `${p.actor || ""} xin quyền sửa dự án ${p.project || ""}` }),
      h("pre", {}, h("code", { text: p.path || "" })),
      h("div", { class: "md" }, md(p.reason || "")),
      h("div", { class: "reason", text: `Dự án đang ở chế độ chỉ đọc${p.ticket ? " · " + p.ticket : ""}. Đổi lại được trong cài đặt dự án.` }),
      pending ? h("div", { class: "actions" },
        (p.options || []).map((o) => h("button", { class: "btn primary", text: mode[o] || o, onclick: () => answer({ approved: true, access: o }) })),
        h("button", { class: "btn danger", text: "Không cho phép", onclick: () => answer({ approved: false }) }))
        : doneLine(verdict));
  }

  if (item.kind === "ask_colleague") {
    const input = h("input", { type: "text", placeholder: `Trả lời thay ${p.to || ""}` });
    const send = () => { const v = input.value.trim(); if (v) answer({ answer: v }); };
    return h("div", { class: "card ask" },
      h("div", { class: "title", text: `${p.asked_by || ""} hỏi ${p.to || ""} (${p.ticket || ""})` }),
      h("div", { class: "md" }, md(p.question || "")),
      pending ? [
        h("div", { class: "sub" }, "Đang chờ trong ", h("a", { href: `#/c/${p.conversation_id}`, text: "phòng họp" }), ". Bạn có thể trả lời thay:"),
        h("div", { class: "row-inline" }, input, h("button", { class: "btn primary", text: "Gửi", onclick: send })),
      ] : doneLine(`${(item.response && item.response.answered_by) || ""} trả lời: ${item.response ? item.response.answer : item.status}`));
  }
  return h("div", { class: "card", text: item.kind });
}

// A list of proposed tickets (SI-24): choose, edit, run now or backlog per item, then create them in one go.
function proposalCard(item, answer) {
  const p = item.payload || {};
  const items = p.items || [];
  const head = h("div", { class: "title", text: `${p.proposed_by || ""} đề xuất ${items.length} ticket` });
  if (item.status !== "pending") {
    const r = item.response || {};
    const done = r.approved
      ? h("div", { class: "done" }, "Đã tạo ", (r.created || []).map((k, i) => [i ? ", " : "", h("a", { href: `#/t/${k}`, text: k })]))
      : h("div", { class: "done", text: item.status === "answered" ? "Đã bỏ hết" : item.status });
    return h("div", { class: "card ask" }, head, done);
  }
  const rows = items.map((it) => {
    const sel = h("input", { type: "checkbox", checked: true, "aria-label": `Chọn mục ${it.n}` });
    const title = h("input", { type: "text", value: it.title || "" });
    const desc = h("textarea", { rows: 3 });
    desc.value = it.description || "";
    const workflow = h("select", {}, [["code", "code (đầy đủ)"], ["quick", "quick (sửa nhỏ)"]].map(([v, l]) => h("option", { value: v, text: l, selected: v === it.workflow })));
    const projects = S.projects.some((x) => x.id === it.project_id) ? S.projects : [...S.projects, { id: it.project_id || "", name: it.project || "—" }];
    const project = h("select", {}, projects.map((x) => h("option", { value: x.id, text: x.name, selected: x.id === it.project_id })));
    const runNow = h("select", {}, h("option", { value: "1", text: "Chạy ngay", selected: it.run_now }), h("option", { value: "0", text: "Để backlog", selected: !it.run_now }));
    const deps = [...(it.depends_on || []).map((n) => `mục ${n}`), ...(it.blocked_by || [])];
    const warn = h("div", { class: "proposal-warn", hidden: true });
    const node = h("div", { class: "proposal" },
      h("label", { class: "proposal-head" }, sel, h("b", { text: `${it.n}.` }), title),
      desc,
      h("div", { class: "reason", text: `Vì sao: ${it.why || ""}` }),
      h("div", { class: "row-inline" }, workflow, project, runNow),
      deps.length ? h("div", { class: "sub", text: `Phụ thuộc: ${deps.join(", ")}` }) : null,
      warn);
    return { it, sel, title, desc, workflow, project, runNow, warn, node };
  });
  const create = h("button", { class: "btn primary" });
  const update = () => {
    const chosen = new Set(rows.filter((r) => r.sel.checked).map((r) => r.it.n));
    for (const r of rows) {
      r.node.classList.toggle("off", !r.sel.checked);
      const lost = (r.it.depends_on || []).filter((n) => !chosen.has(n));
      r.warn.hidden = !(r.sel.checked && lost.length);
      r.warn.textContent = lost.length ? `⚠ Bỏ phụ thuộc vào ${lost.map((n) => `"${(items[n - 1] || {}).title}"`).join(", ")} (không được chọn)` : "";
    }
    create.textContent = `Tạo ${chosen.size} ticket`;
    create.disabled = !chosen.size;
  };
  rows.forEach((r) => r.sel.addEventListener("change", update));
  update();
  create.addEventListener("click", () => answer({
    approved: true,
    items: rows.map((r) => ({ n: r.it.n, selected: r.sel.checked, title: r.title.value.trim(), description: r.desc.value, workflow: r.workflow.value, project_id: r.project.value || null, run_now: r.runNow.value === "1" })),
  }));
  return h("div", { class: "card ask proposals" }, head, rows.map((r) => r.node),
    h("div", { class: "actions" }, create, h("button", { class: "btn", text: "Bỏ hết", onclick: () => answer({ approved: false }) })));
}

// ---- composer with @mention autocomplete ----------------------------------------------------

function composer() {
  const ta = h("textarea", { id: "composer-input", rows: 1, placeholder: S.conv && S.conv.kind === "dm" ? "Nhắn tin…" : "Nhắn vào nhóm, @tên để gọi ai đó…" });
  const wrap = h("div", { class: "composer" }, ta, h("div", { class: "hint", text: "Enter để gửi · Shift+Enter xuống dòng · @ để nhắc tên" }));
  let popup = null;
  let options = [];
  let sel = 0;

  const closePopup = () => { popup?.remove(); popup = null; };
  const currentToken = () => {
    const upto = ta.value.slice(0, ta.selectionStart);
    const m = upto.match(/@([\w.\-À-ỹ]*)$/);
    return m ? { start: upto.length - m[0].length, query: m[1].toLowerCase() } : null;
  };
  const choose = (a) => {
    const tok = currentToken();
    if (!tok) return;
    const before = ta.value.slice(0, tok.start);
    const after = ta.value.slice(ta.selectionStart);
    const insert = a === "all" ? "@all " : `@${handleOf(a)} `;
    ta.value = before + insert + after;
    ta.selectionStart = ta.selectionEnd = before.length + insert.length;
    closePopup(); ta.focus();
  };
  const showPopup = () => {
    const tok = currentToken();
    if (!tok) return closePopup();
    const isGroup = S.conv && S.conv.kind === "group";
    options = S.actors.filter((a) => handleOf(a).toLowerCase().startsWith(tok.query) || a.name.toLowerCase().includes(tok.query)).slice(0, 8);
    if (isGroup && "all".startsWith(tok.query)) options.push("all");
    if (!options.length) return closePopup();
    sel = Math.min(sel, options.length - 1);
    closePopup();
    popup = h("div", { class: "popup" }, options.map((a, i) => {
      const pos = a !== "all" ? positionById(a.position_id) : null;
      return h("div", { class: "item" + (i === sel ? " sel" : ""), onmousedown: (e) => { e.preventDefault(); choose(a); } },
        a === "all" ? h("span", { text: "@all — mọi người trong nhóm" }) : [avatar(a, true), h("span", { text: a.name }), h("span", { class: "sub", text: pos ? pos.title : "" })]);
    }));
    wrap.append(popup);
  };
  const send = async () => {
    const body = ta.value.trim();
    if (!body || !S.conv) return;
    ta.value = ""; autosize();
    const m = await act(() => api("POST", `/api/conversations/${S.conv.id}/messages`, { body }));
    if (m && !S.messages.some((x) => x.id === m.id)) { S.messages.push(m); appendMessage(m); }
  };
  const autosize = () => { ta.style.height = "auto"; ta.style.height = Math.min(200, ta.scrollHeight) + "px"; };

  ta.addEventListener("input", () => { sel = 0; showPopup(); autosize(); });
  ta.addEventListener("blur", () => setTimeout(closePopup, 100));
  ta.addEventListener("keydown", (e) => {
    if (popup) {
      if (e.key === "ArrowDown") { sel = (sel + 1) % options.length; showPopup(); e.preventDefault(); return; }
      if (e.key === "ArrowUp") { sel = (sel - 1 + options.length) % options.length; showPopup(); e.preventDefault(); return; }
      if (e.key === "Enter" || e.key === "Tab") { choose(options[sel]); e.preventDefault(); return; }
      if (e.key === "Escape") { closePopup(); return; }
    }
    if (e.key === "Enter" && !e.shiftKey && !e.isComposing) { e.preventDefault(); send(); }
  });
  return wrap;
}

// ---- details panel --------------------------------------------------------------------------

function renderDetails() {
  const d = document.getElementById("details");
  if (!S.conv) { d.replaceChildren(); return; }
  const actors = S.conv.members.filter((m) => m.kind === "actor").map((m) => actorById(m.id)).filter(Boolean);
  const project = projectById(S.conv.project_id);
  const members = [
    h("div", { class: "member" }, avatar(null, true), h("span", { text: "Bạn" })),
    ...actors.map((a) => {
      const pos = positionById(a.position_id);
      return h("a", { class: "member plain", href: `#/a/${a.id}` }, avatar(a, true), h("span", { text: a.name }), h("span", { class: "sub", text: `${pos ? pos.title : ""} · ${STATE_VI[actorState(a.id)]}` }));
    }),
  ];
  const tickets = S.convTickets.map((t) => h("div", { class: "trow" },
    statusPill(t.status), h("a", { class: "t", href: `#/t/${t.key}`, text: `${t.key} ${t.title}` })));
  const u = S.convUsage;
  put(d,
    project ? [h("h3", { text: "Dự án" }), h("div", { class: "sub", text: project.name + (project.path ? ` · ${ACCESS_VI[project.access]}` : "") })] : null,
    h("h3", { text: "Thành viên" }), members,
    S.conv.kind === "group" ? h("button", { class: "btn sm", text: "+ Thêm người", onclick: addMembersModal }) : null,
    h("h3", { text: "Tickets từ cuộc trò chuyện" }),
    tickets.length ? tickets : h("div", { class: "sub", text: "Chưa có. Actor sẽ đề xuất ticket khi cần làm việc thật." }),
    h("h3", { text: "Usage" }),
    h("div", { class: "sub", text: u && u.runs ? `${u.runs} lượt chạy · ${usageText(u)}` : "Chưa dùng token nào." }),
    h("h3", { text: "Phiên làm việc" }),
    h("button", { class: "btn sm", text: "Bắt đầu phiên mới", title: "Như /clear: bỏ lịch sử phiên chat cũ, giữ memory dài hạn",
      onclick: () => act(async () => { await api("POST", `/api/conversations/${S.conv.id}/new-session`); toast("Đã bắt đầu phiên mới"); }) }));
}

// ---- inbox ----------------------------------------------------------------------------------

function renderInbox() {
  const main = document.getElementById("main");
  const byTime = (a, b) => a.created_at.localeCompare(b.created_at);
  const items = forUser().sort(byTime);
  const colleagues = S.pending.filter((i) => i.kind === "ask_colleague").sort(byTime);
  const row = (i) => {
    const where = i.ticket_id ? h("a", { href: `#/t/${i.ticket_id}`, text: "mở ticket" }) : null;
    return h("div", {}, h("div", { class: "sub" }, time(i.created_at), " ", where), interactionCard(i.id));
  };
  put(main,
    h("div", { class: "header" }, h("h1", { text: "Đang chờ bạn" }), h("span", { class: "meta", text: `${items.length} mục` })),
    h("div", { class: "page" },
      items.length ? items.map(row) : h("div", { class: "empty", text: "Không có gì cần bạn quyết định." }),
      colleagues.length ? [h("h3", { text: `Đồng nghiệp đang hỏi nhau (${colleagues.length})` }),
        h("p", { class: "sub", text: "Không cần bạn làm gì; bạn có thể trả lời thay nếu muốn." }), colleagues.map(row)] : null));
}

// ---- tickets --------------------------------------------------------------------------------

let ticketFilterAll = false;

async function renderTickets() {
  const main = document.getElementById("main");
  const project = currentProject();
  const seq = S.gen;
  const tickets = await act(() => api("GET", `/api/tickets?all=${ticketFilterAll}&project=${project ? project.id : ""}`));
  if (!tickets || S.view !== "tickets" || seq !== S.gen) return;
  const cols = ["todo", "in_progress", "in_review", "blocked", "done"];
  const byStatus = Object.fromEntries(cols.map((c) => [c, []]));
  for (const t of tickets) (byStatus[t.status === "backlog" ? "todo" : t.status] || byStatus.done).push(t);
  const running = new Set(Object.values(S.runs).filter((r) => r.kind === "ticket").map((r) => r.ticket_id));
  put(main,
    h("div", { class: "header" },
      h("h1", { text: `Tickets · ${project ? project.name : ""}` }),
      h("div", { class: "tabs" },
        h("button", { class: "btn sm" + (!ticketFilterAll ? " on" : ""), text: "Đang mở", onclick: () => { ticketFilterAll = false; renderTickets(); } }),
        h("button", { class: "btn sm" + (ticketFilterAll ? " on" : ""), text: "Tất cả", onclick: () => { ticketFilterAll = true; renderTickets(); } })),
      h("button", { class: "btn primary", text: "+ Ticket", onclick: newTicketModal })),
    h("div", { class: "page" },
      h("div", { class: "board" }, cols.map((c) => h("div", { class: "col" },
        h("h4", {}, h("span", { text: STATUS_VI[c] }), h("span", { text: byStatus[c].length })),
        byStatus[c].map((t) => {
          const a = actorById(t.assignee_actor_id);
          const overdue = t.due_at && t.status !== "done" && t.due_at < new Date().toISOString().slice(0, 10);
          return h("a", { class: "tcard", href: `#/t/${t.key}` },
            h("div", { class: "k" }, t.key, t.type && t.type !== "task" ? ` · ${TYPE_VI[t.type] || t.type}` : "", running.has(t.id) ? " · đang chạy" : ""),
            h("div", { text: t.title }),
            h("div", { class: "who" }, a ? [avatar(a, true), a.name] : "chưa giao",
              t.due_at ? h("span", { class: "due" + (overdue ? " overdue" : ""), text: `hạn ${t.due_at.slice(5)}` }) : null));
        }))))));
}

async function renderTicket(key) {
  const main = document.getElementById("main");
  S.ticketKey = key;  // set before fetching so events that arrive meanwhile trigger a re-render
  const seq = S.gen;
  const data = await act(() => api("GET", `/api/tickets/${encodeURIComponent(key)}`));
  if (!data || S.view !== "ticket" || seq !== S.gen) return;  // a newer render superseded this one
  const t = data.ticket;
  S.ticketKey = t.key; S.ticketId = t.id;
  if (t.project_id) setProject(t.project_id);
  const assignee = h("select", { onchange: (e) => act(async () => { await api("POST", `/api/tickets/${t.key}/assign`, { actor_id: e.target.value || null }); renderTicket(t.key); }) },
    h("option", { value: "", text: "— chưa giao —" }),
    S.actors.map((a) => h("option", { value: a.id, text: a.name, selected: a.id === t.assignee_actor_id })));
  const statusSel = h("select", {}, ["todo", "in_review", "blocked", "done", "cancelled", "backlog"].map((s) => h("option", { value: s, text: STATUS_VI[s], selected: s === t.status })));
  const commentBox = h("textarea", { rows: 3, class: "full", placeholder: "Comment cho người phụ trách (sẽ đánh thức họ)…" });
  const runningRun = Object.values(S.runs).find((r) => r.ticket_id === t.id);
  const project = projectById(t.project_id);
  const due = h("input", { type: "date", value: t.due_at || "", title: "Hạn chót",
    onchange: (e) => act(async () => { await api("POST", `/api/tickets/${t.key}/due`, { due_at: e.target.value || null }); toast("Đã đổi hạn"); }) });
  const mergeInfo = project && project.access === "worktree" ? await act(() => api("GET", `/api/tickets/${t.key}/merge`)) : null;
  if (S.view !== "ticket" || seq !== S.gen) return;
  const mergeBox = mergeInfo && mergeInfo.available ? h("div", { class: "box" }, h("h4", { text: "Git" }),
    h("div", { class: "sub", text: `Nhánh ${mergeInfo.branch} · ${mergeInfo.ahead} commit chưa có trên ${mergeInfo.main}` + (mergeInfo.merged ? " · đã merge" : "") }),
    !mergeInfo.merged && mergeInfo.ahead ? h("button", { class: "btn primary", text: `Merge vào ${mergeInfo.main}`, onclick: () => {
      if (!confirm(`Merge nhánh ${mergeInfo.branch} vào ${mergeInfo.main} của thư mục dự án?`)) return;
      act(async () => { const r = await api("POST", `/api/tickets/${t.key}/merge`); toast(`Đã merge (${r.commit})`); renderTicket(t.key); });
    } }) : null) : null;
  const changeBox = h("textarea", { rows: 3, class: "full", placeholder: "Yêu cầu thay đổi thế nào? (ví dụ: bỏ xuất PDF, thay bằng Excel; thêm cột lợi nhuận)" });
  const reopen = h("input", { type: "checkbox" });

  put(main,
    h("div", { class: "header" }, h("a", { href: "#/tickets", text: "← Tickets" }), h("h1", { text: `${t.key} · ${t.title}` })),
    h("div", { class: "page" },
      data.root || data.parent ? h("div", { class: "crumbs" },
        data.root ? [h("a", { href: `#/t/${data.root.key}`, text: `${data.root.key} ${data.root.title}` })] : null,
        data.parent && (!data.root || data.parent.key !== data.root.key) ? [" › ", h("a", { href: `#/t/${data.parent.key}`, text: `${data.parent.key} ${data.parent.title}` })] : null,
        " › ", t.key) : null,
      h("div", { class: "ticket-head" },
        statusPill(t.status), h("span", { class: "status", text: TYPE_VI[t.type] || t.type }), h("span", { class: "sub", text: `ưu tiên ${t.priority}` }),
        project ? h("span", { class: "sub", text: `dự án ${project.name}` }) : null,
        h("span", { class: "sub", text: "Hạn:" }), due,
        data.meeting_room ? h("a", { class: "btn sm", href: `#/c/${data.meeting_room}`, text: "Phòng họp" }) : null,
        h("span", { class: "sub", text: "Người làm:" }), assignee,
        runningRun ? h("span", { class: "row-inline" }, h("span", { class: "spinner" }), h("span", { text: runningRun.step || "đang chạy" }),
          h("button", { class: "btn sm danger", text: "Dừng", onclick: () => act(() => api("POST", `/api/runs/${runningRun.id}/stop`)) })) : null),
      data.pending.map((i) => { S.interactions[i.id] = i; return interactionCard(i.id); }),
      h("div", { class: "box" }, h("h4", { text: "Mô tả" }), h("div", { class: "md" }, md(t.description || "(trống)"))),
      data.blockers.length ? h("div", { class: "box" }, h("h4", { text: "Bị chặn bởi" }), data.blockers.map((k) => h("a", { class: "chip", href: `#/t/${k}`, text: k }))) : null,
      data.children.length ? h("div", { class: "box" }, h("h4", { text: "Ticket con" }),
        data.children.map((c) => {
          const who = actorById(c.assignee_actor_id);
          return h("div", { class: "trow" }, statusPill(c.status), h("a", { class: "t", href: `#/t/${c.key}`, text: `${c.key} ${c.title}` }),
            h("span", { class: "sub", text: [who && who.name, c.due_at && `hạn ${c.due_at}`].filter(Boolean).join(" · ") }));
        })) : null,
      data.documents.length ? h("div", { class: "box" }, h("h4", { text: "Tài liệu" }),
        data.documents.map((d) => h("details", { class: "doc" }, h("summary", { text: `${d.key} · rev ${d.revision} · ${d.updated_by}` }), h("div", { class: "md" }, md(d.body))))) : null,
      data.work_products.length ? h("div", { class: "box" }, h("h4", { text: "Kết quả bàn giao" }),
        data.work_products.map((w) => h("div", { class: "trow" }, h("span", { class: "status", text: w.kind }), h("code", { text: w.ref }), h("span", { class: "sub", text: (w.metadata && w.metadata.note) || "" })))) : null,
      h("div", { class: "box activity" }, h("h4", { text: "Hoạt động" }),
        data.comments.map((c) => c.author_kind === "system"
          ? h("div", { class: "c system", text: `${time(c.created_at)} · ${c.body}` })
          : h("div", { class: "c" + (c.author_kind === "change" ? " change" : "") },
            h("div", { class: "h", text: `${c.author_kind === "user" ? "Bạn" : c.author_id} · ${time(c.created_at)}` }), h("div", { class: "md" }, md(c.body)))),
        commentBox,
        h("div", { class: "row-inline" },
          h("button", { class: "btn primary", text: "Gửi comment", onclick: () => act(async () => {
            if (!commentBox.value.trim()) return;
            await api("POST", `/api/tickets/${t.key}/comments`, { body: commentBox.value });
            commentBox.value = ""; renderTicket(t.key);
          }) }),
          h("span", { class: "sub", text: "Đổi trạng thái:" }), statusSel,
          h("button", { class: "btn", text: "Áp dụng", onclick: () => act(async () => {
            await api("POST", `/api/tickets/${t.key}/status`, { status: statusSel.value, comment: commentBox.value.trim() });
            commentBox.value = ""; renderTicket(t.key);
          }) }))),
      mergeBox,
      h("div", { class: "box" }, h("h4", { text: "Thay đổi yêu cầu" }),
        h("p", { class: "sub", text: "Ghi nhận thay đổi lên cả cây ticket: ai đang làm sẽ bị dừng ở bước kế tiếp để đọc lại, ai chờ sẽ thấy ngay khi bắt đầu. Với thay đổi lớn, nên nhắn BA để BA cập nhật tài liệu yêu cầu và đánh giá ảnh hưởng." }),
        changeBox,
        h("div", { class: "row-inline" },
          h("label", { class: "sub" }, reopen, " Mở lại các ticket đã xong"),
          h("button", { class: "btn", text: "Gửi thay đổi", onclick: () => act(async () => {
            if (!changeBox.value.trim()) return;
            const r = await api("POST", `/api/tickets/${t.key}/change`, { summary: changeBox.value.trim(), reopen_done: reopen.checked });
            toast(`Đã báo: ${[...r.notified, ...r.reopened].join(", ") || "không có ticket mở"}`);
            renderTicket(t.key);
          }) }))),
      h("div", { class: "box" }, h("h4", { text: "Usage (ticket và ticket con)" }), h("div", { class: "sub", text: data.usage.runs ? `${data.usage.runs} lượt chạy · ${usageText(data.usage)}` : "Chưa chạy lần nào." })),
      data.runs.length ? h("div", { class: "box" }, h("h4", { text: "Các lần chạy" }), data.runs.map(runRow)) : null));
}

function runRow(r) {
  const a = actorById(r.actor_id);
  const events = h("div", { class: "events", text: "Đang tải…" });
  const det = h("details", { class: "doc" },
    h("summary", { text: `${time(r.started_at)} · ${a ? a.name : "?"} · ${r.kind} · ${r.status} · ${usageText({ ...r.usage, cost_usd: r.cost_usd }, true)}${r.error ? " · " + r.error : ""}` }),
    events);
  det.addEventListener("toggle", async () => {
    if (!det.open || events.dataset.loaded) return;
    const list = await act(() => api("GET", `/api/runs/${r.id}/events`));
    events.dataset.loaded = "1";
    events.replaceWith(timelineNode(linesOf(list)));
  });
  return det;
}

// ---- team -----------------------------------------------------------------------------------

async function renderTeam() {
  const main = document.getElementById("main");
  const seq = S.gen;
  const usage = await act(() => api("GET", "/api/usage"));
  if (S.view !== "team" || seq !== S.gen) return;
  const byActor = (rows, id) => (rows || []).find((r) => r.actor_id === id);
  put(main,
    h("div", { class: "header" }, h("h1", { text: "Nhân sự" }),
      h("button", { class: "btn", text: "Cập nhật vị trí mẫu", title: "Nạp lại các vị trí và skill chuẩn đi kèm ứng dụng (ghi đè vị trí cùng mã)",
        onclick: () => { if (confirm("Ghi đè các vị trí mẫu (ba, lead, dev, qa, pm, researcher, analyst, assistant, consultant, collector, writer) và skill chuẩn bằng bản mới nhất?")) act(async () => { await api("POST", "/api/positions/sync"); await loadState(); render(); toast("Đã cập nhật vị trí mẫu"); }); } }),
      h("button", { class: "btn", text: "+ Vị trí", onclick: () => positionModal(null) }),
      h("button", { class: "btn primary", text: "+ Tuyển nhân viên", onclick: () => hireModal() })),
    h("div", { class: "page" },
      h("h3", { text: "Nhân viên" }),
      S.actors.length ? h("table", { class: "list" },
        h("tr", {}, ["", "Tên", "Vị trí", "Chuyên môn", "Model", "Quyền", "Trạng thái", "Hôm nay", "7 ngày", ""].map((t) => h("th", { text: t }))),
        S.actors.map((a) => {
          const pos = positionById(a.position_id);
          const today = byActor(usage && usage.today, a.id);
          const week = byActor(usage && usage.week, a.id);
          return h("tr", {},
            h("td", {}, avatar(a, true)), h("td", {}, h("a", { href: `#/a/${a.id}`, text: a.name })), h("td", { text: pos ? pos.title : "" }),
            h("td", { text: a.specialty || "" }), h("td", { text: a.model || (pos ? pos.model : "") }),
            h("td", { text: a.permission_mode === "trust_all" ? "cho phép tất cả" : `hỏi khi cần${a.allow_rules.length ? ` +${a.allow_rules.length} luật` : ""}` }),
            h("td", { text: STATE_VI[actorState(a.id)] }),
            h("td", { text: today ? usageText(today, true) : "—" }),
            h("td", { text: week ? usageText(week, true) : "—" }),
            h("td", {},
              h("button", { class: "btn sm", text: "Nhắn tin", onclick: () => openDm(a.id) }), " ",
              h("a", { class: "btn sm", href: `#/a/${a.id}`, text: "Hồ sơ" })));
        })) : h("p", { class: "sub", text: "Chưa có nhân viên nào." }),
      h("h3", { text: "Vị trí công việc" }),
      h("div", { class: "grid" }, S.positions.map((p) => h("div", { class: "pcard" },
        h("div", { class: "t", text: p.title }),
        h("div", { class: "s", text: `${p.name} · model ${p.model} · ${p.prompt_preset === "claude_code" ? "preset code" : "prompt gọn"}` }),
        h("div", { class: "s", text: (p.instructions || "").split("\n")[0] }),
        h("div", { class: "tools", text: `tools: ${(p.tools || []).join(", ")} · shell: ${p.policy.exec_default} · web: ${p.policy.web}` }),
        h("div", { class: "actions" },
          h("button", { class: "btn sm", text: "Tuyển", onclick: () => hireModal(p.id) }),
          h("button", { class: "btn sm", text: "Sửa", onclick: () => positionModal(p) })))))));
}

// ---- actor profile --------------------------------------------------------------------------

async function renderActor(id) {
  const main = document.getElementById("main");
  const seq = S.gen;
  const data = await act(() => api("GET", `/api/actors/${encodeURIComponent(id)}`));
  if (!data || S.view !== "actor" || seq !== S.gen) return;
  const a = data.actor;
  const pos = data.position;
  const patch = (body, msg) => act(async () => { await api("PATCH", `/api/actors/${a.id}`, body); toast(msg || "Đã lưu"); await loadState(); renderActor(a.id); });

  const nameInput = h("input", { type: "text", value: a.name });
  const emoji = h("input", { type: "text", value: a.emoji, class: "w-sm" });
  const specialty = h("input", { type: "text", value: a.specialty });
  const model = modelSelect(a.model || "");
  const posSel = h("select", {}, S.positions.map((p) => h("option", { value: p.id, text: p.title, selected: p.id === a.position_id })));

  const ruleInput = h("input", { type: "text", placeholder: "Bash(npm install *) · WebFetch · Bash" });
  const modeSel = h("select", {},
    h("option", { value: "default", text: "Hỏi khi cần (mặc định)", selected: a.permission_mode === "default" }),
    h("option", { value: "trust_all", text: "Cho phép tất cả (trừ luật an toàn cứng)", selected: a.permission_mode === "trust_all" }));

  const memText = h("input", { type: "text", placeholder: "Điều nhân viên này nên nhớ…" });
  const memKind = h("select", {}, Object.entries(KIND_VI).map(([k, v]) => h("option", { value: k, text: v, selected: k === "fact" })));
  const memScope = h("select", {}, h("option", { value: "actor", text: "riêng nhân viên" }), h("option", { value: "position", text: `cả vị trí ${pos.title}` }), h("option", { value: "shared", text: "cả văn phòng" }));

  const u = data.usage;
  put(main,
    h("div", { class: "header" }, avatar(a), h("h1", { text: a.name }), h("span", { class: "meta", text: `${pos.title} · ${STATE_VI[actorState(a.id)]}` }),
      h("button", { class: "btn", text: "Nhắn tin", onclick: () => openDm(a.id) }),
      h("button", { class: "btn", text: a.status === "paused" ? "Tiếp tục" : "Tạm dừng", onclick: () => patch({ status: a.status === "paused" ? "active" : "paused" }) })),
    h("div", { class: "page" },
      h("div", { class: "box" }, h("h4", { text: "Hồ sơ" }),
        h("div", { class: "form-grid" },
          field("Tên (@gọi)", nameInput), field("Emoji", emoji), field("Chuyên môn", specialty), field("Model", model), field("Vị trí", posSel)),
        h("button", { class: "btn primary", text: "Lưu hồ sơ", onclick: () => patch({ name: nameInput.value.trim(), emoji: emoji.value, specialty: specialty.value, model: model.value || "", position_id: posSel.value }) })),

      h("div", { class: "box" }, h("h4", { text: "Quyền (giống Claude Code / Kiro)" }),
        h("div", { class: "row-inline" }, modeSel, h("button", { class: "btn", text: "Áp dụng", onclick: () => patch({ permission_mode: modeSel.value }, "Đã đổi chế độ quyền") })),
        h("p", { class: "sub", text: "Luật an toàn cứng luôn được giữ: không đọc ~/.ssh, ~/.claude, dữ liệu văn phòng; không chạy lệnh phá huỷ (rm -rf /, curl | sh, force push…); không ghi ra ngoài workspace." }),
        h("div", { class: "sub", text: "Luật luôn cho phép:" }),
        a.allow_rules.length ? a.allow_rules.map((r) => h("div", { class: "trow" }, h("code", { class: "t", text: r }),
          h("button", { class: "btn sm danger", text: "Xoá", onclick: () => patch({ allow_rules: a.allow_rules.filter((x) => x !== r) }, "Đã xoá luật") }))) : h("div", { class: "sub", text: "(chưa có — thêm từ thẻ duyệt hoặc ở đây)" }),
        h("div", { class: "row-inline" }, ruleInput, h("button", { class: "btn", text: "Thêm luật", onclick: () => {
          if (ruleInput.value.trim()) patch({ allow_rules: [...a.allow_rules, ruleInput.value.trim()] }, "Đã thêm luật");
        } }))),

      h("div", { class: "box" }, h("h4", {}, `Skills (${data.skills.length})`, " ",
        h("button", { class: "btn sm", text: "+ Skill riêng", onclick: () => skillModal({ scope: "actor", owner: a.name }) }),
        " ", h("a", { class: "btn sm", href: "#/skills", text: "Quản lý tất cả skills" })),
        data.skills.length ? data.skills.map((s) => h("div", { class: "trow" },
          h("span", { class: "status", text: SCOPE_VI[s.ref.scope] }),
          h("a", { class: "t", href: "#", text: `${s.ref.name} — ${s.description}`, onclick: (e) => { e.preventDefault(); skillModal(s.ref); } }),
          s.files.length ? h("span", { class: "sub", text: `${s.files.length} file` }) : null)) : h("div", { class: "sub", text: "Chưa có skill nào. Nhân viên sẽ tự tạo khi gặp quy trình lặp lại, hoặc bạn thêm ở đây." })),

      h("div", { class: "box" }, h("h4", {}, `Memory (${data.memories.length})`, " ",
        data.memories.filter((m) => m.scope === "actor" && !m.pinned).length >= 6 ? h("button", { class: "btn sm", text: "Gom gọn", title: "Gộp ghi nhớ trùng / lỗi thời bằng model rẻ (ghi nhớ đã ghim được giữ nguyên)",
          onclick: (e) => { e.target.disabled = true; act(async () => { const r = await api("POST", `/api/actors/${a.id}/memories/compact`); toast(`Gom gọn: ${r.before} → ${r.after} ghi nhớ`); renderActor(a.id); }).finally(() => { e.target.disabled = false; }); } }) : null, " ",
        data.memories.some((m) => m.scope === "actor") ? h("button", { class: "btn sm danger", text: "Xoá toàn bộ memory riêng", onclick: () => {
          if (confirm(`Xoá toàn bộ memory riêng của ${a.name}?`)) act(async () => { const r = await api("POST", `/api/actors/${a.id}/memories/clear`); toast(`Đã xoá ${r.cleared} mục`); renderActor(a.id); });
        } }) : null),
        data.memories.length ? data.memories.map((m) => h("div", { class: "trow" },
          h("span", { class: "status", text: `${KIND_VI[m.kind] || m.kind} · ${SCOPE_VI[m.scope]}` }),
          h("span", { class: "t", title: `bởi ${m.created_by} · ${time(m.updated_at)} · dùng ${m.use_count} lần`, text: m.content }),
          h("button", { class: "btn sm", text: m.pinned ? "Bỏ ghim" : "Ghim", onclick: () => act(async () => { await api("PATCH", `/api/memories/${m.id}`, { pinned: !m.pinned }); renderActor(a.id); }) }),
          h("button", { class: "btn sm", text: "Sửa", onclick: () => {
            const v = prompt("Sửa ghi nhớ", m.content);
            if (v !== null && v.trim()) act(async () => { await api("PATCH", `/api/memories/${m.id}`, { content: v }); renderActor(a.id); });
          } }),
          h("button", { class: "btn sm danger", text: "Xoá", onclick: () => act(async () => { await api("DELETE", `/api/memories/${m.id}`); renderActor(a.id); }) }))) : h("div", { class: "sub", text: "Chưa nhớ gì. Nhân viên tự ghi nhớ bài học khi làm việc." }),
        h("div", { class: "row-inline" }, memText, memKind, memScope, h("button", { class: "btn", text: "Thêm", onclick: () => act(async () => {
          if (!memText.value.trim()) return;
          await api("POST", `/api/actors/${a.id}/memories`, { content: memText.value, kind: memKind.value, scope: memScope.value });
          renderActor(a.id);
        }) }))),

      data.automations && data.automations.length ? h("div", { class: "box" }, h("h4", { text: `Automation đang chạy (${data.automations.length})` }),
        data.automations.map((x) => h("div", { class: "trow" },
          h("span", { class: `status ${x.status}`, text: AUTO_STATUS_VI[x.status] }),
          h("a", { class: "t", href: `#/au/${x.name}`, text: `${x.name} — ${x.description}` }),
          h("span", { class: "sub", text: `${stepsShort(x.steps)} · ${whenText(x)}` })))) : null,

      h("div", { class: "box" }, h("h4", { text: "Usage" }),
        h("div", { class: "sub", text: `Hôm nay: ${u.today.runs ? usageText(u.today) : "—"}` }),
        h("div", { class: "sub", text: `7 ngày: ${u.week.runs ? usageText(u.week) : "—"}` }),
        h("div", { class: "sub", text: `Tổng: ${u.all.runs} lượt · ${usageText(u.all)}` }),
        data.runs.length ? [h("h4", { text: "Lần chạy gần đây" }), data.runs.map(runRow)] : null)));
}

function modelSelect(value) {
  const choices = [["", "Theo vị trí"], ["haiku", "haiku (rẻ, nhanh)"], ["sonnet", "sonnet"], ["opus", "opus (mạnh nhất)"], ["kiro", "Kiro CLI (model mặc định của Kiro)"]];
  if (value && !choices.some(([v]) => v === value)) choices.push([value, value]);  // e.g. "kiro:<model id>"
  return h("select", {}, choices.map(([v, t]) => h("option", { value: v, text: t, selected: v === value })));
}

// ---- skills ---------------------------------------------------------------------------------

async function renderSkills() {
  const main = document.getElementById("main");
  const seq = S.gen;
  const skills = await act(() => api("GET", "/api/skills"));
  if (!skills || S.view !== "skills" || seq !== S.gen) return;
  const groups = {};
  for (const s of skills) {
    const key = s.ref.scope === "shared" ? "Chung (mọi nhân viên)" : s.ref.scope === "position" ? `Vị trí: ${s.ref.owner}` : `Nhân viên: ${s.ref.owner}`;
    (groups[key] = groups[key] || []).push(s);
  }
  put(main,
    h("div", { class: "header" }, h("h1", { text: "Skills" }), h("span", { class: "meta", text: "Lưu dạng SKILL.md, mỗi thay đổi là một git commit" }),
      h("button", { class: "btn primary", text: "+ Skill", onclick: () => skillModal({ scope: "shared", owner: "" }, true) })),
    h("div", { class: "page" },
      Object.keys(groups).length ? Object.entries(groups).map(([title, list]) => h("div", { class: "box" }, h("h4", { text: title }),
        list.map((s) => h("div", { class: "trow" },
          h("a", { class: "t", href: "#", text: `${s.ref.name} — ${s.description}`, onclick: (e) => { e.preventDefault(); skillModal(s.ref); } }),
          s.files.length ? h("span", { class: "sub", text: `${s.files.length} file` }) : null,
          h("span", { class: "sub", text: s.created_by ? `bởi ${s.created_by}` : "" }))))) : h("div", { class: "empty", text: "Chưa có skill nào." })));
}

// ---- automations ----------------------------------------------------------------------------

const AUTO_STATUS_VI = { pending_review: "chờ duyệt", enabled: "đang bật", disabled: "đã tắt", finished: "đã chạy xong" };
const STEP_ICON = { notify: "💬", script: "⚙️", agent: "🤖" };

function stepLabel(st) {
  if (st.type === "notify") return `💬 nhắn: ${st.text}`;
  if (st.type === "script") return `⚙️ chạy script ${st.file}`;
  return `🤖 giao ${st.actor}: ${st.prompt}`;
}

function stepsShort(steps) {
  return (steps || []).map((st) => STEP_ICON[st.type] + (st.type === "script" ? ` ${st.file}` : st.type === "agent" ? ` ${st.actor}` : "")).join(" → ");
}

function whenText(a) {
  if (a.schedule) return scheduleText(a.schedule);
  if (a.next_at) return `một lần · ${time(a.next_at)}`;
  return a.status === "finished" ? "một lần (đã chạy)" : "chạy tay";
}
const RUN_STATUS_VI = { running: "đang chạy", succeeded: "thành công", failed: "lỗi" };

function scheduleText(s) {
  if (!s) return "chạy tay";
  if (s.startsWith("every:")) { const m = Number(s.split(":")[1]); return m % 60 ? `mỗi ${m} phút` : `mỗi ${m / 60} giờ`; }
  const [kind, clock] = s.split("@");
  if (kind === "daily") return `hằng ngày ${clock}`;
  if (kind === "weekdays") return `thứ 2–6 ${clock}`;
  const day = { mon: "thứ 2", tue: "thứ 3", wed: "thứ 4", thu: "thứ 5", fri: "thứ 6", sat: "thứ 7", sun: "chủ nhật" }[kind.split(":")[1]];
  return `${day} hằng tuần ${clock}`;
}

async function renderAutomations() {
  const main = document.getElementById("main");
  const seq = S.gen;
  const list = await act(() => api("GET", "/api/automations"));
  if (!list || S.view !== "automations" || seq !== S.gen) return;
  put(main,
    h("div", { class: "header" }, h("h1", { text: "Automation" }), h("span", { class: "meta", text: "Flow do nhân viên thiết kế (gửi tin · script · giao việc), văn phòng tự chạy đúng giờ" })),
    h("div", { class: "page" },
      list.length ? h("table", { class: "list" },
        h("tr", {}, ["Tên", "Mô tả", "Các bước", "Chủ", "Lịch", "Trạng thái", "Lần chạy cuối", "Lần tới", ""].map((t) => h("th", { text: t }))),
        list.map((a) => {
          const owner = actorById(a.owner_actor_id);
          const last = a.last_run;
          return h("tr", {},
            h("td", {}, h("a", { href: `#/au/${a.name}`, text: a.name })),
            h("td", { text: a.description }),
            h("td", { text: stepsShort(a.steps) }),
            h("td", { text: owner ? owner.name : "" }),
            h("td", { text: whenText(a) }),
            h("td", {}, h("span", { class: `status ${a.status}`, text: AUTO_STATUS_VI[a.status] || a.status }), a.running ? " ⏳" : "",
              a.failures ? h("span", { class: "sub", text: ` ${a.failures} lỗi liên tiếp` }) : null),
            h("td", { text: last ? `${RUN_STATUS_VI[last.status] || last.status} · ${time(last.started_at)}` : "—" }),
            h("td", { text: a.next_at ? time(a.next_at) : "—" }),
            h("td", {}, !["pending_review", "finished"].includes(a.status) ? h("button", { class: "btn sm", text: "Chạy ngay",
              onclick: () => act(async () => { await api("POST", `/api/automations/${a.name}/run`); toast("Đã bắt đầu chạy"); renderAutomations(); }) }) : null));
        })) : h("div", { class: "empty" },
          h("p", { text: "Chưa có automation nào." }),
          h("p", { class: "sub", text: "Khi việc lặp lại hoặc cần làm đúng giờ (nhắc lịch, lấy dữ liệu, báo cáo định kỳ, theo dõi…), nhân viên tự thiết kế một flow gồm các bước: 💬 gửi tin, ⚙️ chạy script, 🤖 giao việc cho đồng nghiệp. Flow có script hoặc giao việc sẽ chờ bạn duyệt." }))));
}

async function renderAutomation(name) {
  const main = document.getElementById("main");
  const seq = S.gen;
  const data = await act(() => api("GET", `/api/automations/${encodeURIComponent(name)}`));
  if (!data || S.view !== "automation" || seq !== S.gen) return;
  const a = data.automation;
  const owner = actorById(a.owner_actor_id);
  const reviewed = a.status !== "pending_review";
  put(main,
    h("div", { class: "header" }, h("a", { href: "#/automations", text: "← Automation" }), h("h1", { text: a.name }),
      h("span", { class: `status ${a.status}`, text: AUTO_STATUS_VI[a.status] }),
      reviewed && a.status !== "finished" ? h("button", { class: "btn primary", text: data.running ? "Đang chạy…" : "Chạy ngay", disabled: data.running,
        onclick: () => act(async () => { await api("POST", `/api/automations/${a.name}/run`); toast("Đã bắt đầu chạy"); }) }) : null,
      reviewed && a.status !== "finished" ? h("button", { class: "btn", text: a.status === "enabled" ? "Tắt" : "Bật",
        onclick: () => act(async () => { await api("POST", `/api/automations/${a.name}/enabled`, { enabled: a.status !== "enabled" }); renderAutomation(a.name); }) }) : null,
      h("button", { class: "btn danger", text: "Xoá", onclick: () => {
        if (confirm(`Xoá automation ${a.name} và kết quả của nó?`)) act(async () => { await api("DELETE", `/api/automations/${a.name}`); go("#/automations"); });
      } })),
    h("div", { class: "page" },
      h("div", { class: "box" }, h("h4", { text: "Thông tin" }),
        h("div", { class: "sub", text: a.description }),
        h("div", { class: "sub", text: `Chủ: ${owner ? owner.name : "—"} · lịch: ${whenText(a)} · lần tới: ${a.next_at ? time(a.next_at) : "—"}` }),
        h("h4", { text: "Các bước" }),
        h("ol", { class: "steps" }, a.steps.map((st) => h("li", { text: stepLabel(st) }))),
        a.ticket_id ? h("a", { href: `#/t/${a.ticket_id}`, text: "ticket tạo ra automation" }) : null,
        !reviewed ? h("p", { class: "sub", text: "Đang chờ bạn duyệt: mở mục 'Đang chờ bạn' để xem thẻ duyệt." }) : null),
      h("div", { class: "box" }, h("h4", { text: "Kết quả (thư mục out/)" }),
        data.outputs.length ? data.outputs.map((f) => h("div", { class: "trow" }, h("code", { class: "t", text: f.path }), h("span", { class: "sub", text: `${f.size} bytes` }),
          h("button", { class: "btn sm", text: "Xem", onclick: () => act(async () => {
            const file = await api("GET", `/api/automations/${encodeURIComponent(a.name)}/files/${f.path}`);
            modal(`${a.name} / ${f.path}`, h("pre", { class: "file-view" }, h("code", { text: file.content })), null, null, true);
          }) })))
          : h("div", { class: "sub", text: "Chưa có file kết quả." })),
      h("div", { class: "box" }, h("h4", { text: "Các lần chạy" }),
        data.runs.length ? data.runs.map((r) => h("details", { class: "doc" },
          h("summary", { text: `#${r.id} · ${time(r.started_at)} · ${r.trigger} · ${RUN_STATUS_VI[r.status] || r.status}${r.exit_code != null ? ` (exit ${r.exit_code})` : ""} · ${((r.duration_ms || 0) / 1000).toFixed(1)}s` }),
          r.output ? h("pre", {}, h("code", { text: r.output })) : null,
          r.error ? h("pre", { class: "err" }, h("code", { text: r.error })) : null)) : h("div", { class: "sub", text: "Chưa chạy lần nào." })),
      Object.entries(data.code || {}).map(([file, code]) => h("div", { class: "box" }, h("h4", { text: `Code: ${file}` }), h("pre", {}, h("code", { text: code }))))));
}

// ---- modals ---------------------------------------------------------------------------------

function modal(title, body, onOk, okText, wide) {
  const root = document.getElementById("modal-root");
  const close = () => root.replaceChildren();
  const ok = onOk ? h("button", { class: "btn primary", text: okText || "OK" }) : null;
  ok?.addEventListener("click", async () => { ok.disabled = true; const done = await act(onOk); ok.disabled = false; if (done !== null && done !== false) close(); });
  const back = h("div", { class: "modal-back", onmousedown: (e) => { if (e.target === back) close(); } },
    h("div", { class: "modal" + (wide ? " wide" : "") }, h("h2", { text: title }), body, h("div", { class: "actions" }, h("button", { class: "btn", text: onOk ? "Huỷ" : "Đóng", onclick: close }), ok)));
  root.replaceChildren(back);
  back.querySelector("input,textarea,select")?.focus();
  return close;
}

const field = (label, input) => h("label", { class: "field" }, h("span", { text: label }), input);

function hireModal(positionId) {
  const pos = h("select", {}, S.positions.map((p) => h("option", { value: p.id, text: p.title, selected: p.id === positionId })));
  const name = h("input", { type: "text", value: S.suggestedName || "", placeholder: "Emma (một từ, dùng để @mention)" });
  const specialty = h("input", { type: "text", placeholder: "Backend Java, viết blog công nghệ…" });
  const emoji = h("input", { type: "text", placeholder: "👨‍💻 (tuỳ chọn)" });
  const model = modelSelect("");
  modal("Tuyển nhân viên", h("div", {}, field("Vị trí", pos), field("Tên", name), field("Chuyên môn", specialty), field("Emoji", emoji), field("Model", model)), async () => {
    const a = await api("POST", "/api/actors", { position_id: pos.value, name: name.value.trim(), specialty: specialty.value, emoji: emoji.value, model: model.value || null });
    await loadState(); render();
    toast(`Đã tuyển ${a.name}`);
    return true;
  }, "Tuyển");
}

const TOOL_CHOICES = ["Read", "Write", "Edit", "Glob", "Grep", "Bash", "WebFetch", "WebSearch"];

function positionModal(p) {
  const name = h("input", { type: "text", value: p ? p.name : "", placeholder: "seo (mã ngắn, không dấu)" });
  const title = h("input", { type: "text", value: p ? p.title : "", placeholder: "SEO Specialist" });
  const instructions = h("textarea", { rows: 7, class: "mono", placeholder: "Vai trò, cách làm việc, tiêu chí chất lượng…" });
  instructions.value = p ? p.instructions : "";
  const model = h("select", {}, ["haiku", "sonnet", "opus"].map((m) => h("option", { value: m, text: m, selected: (p ? p.model : "sonnet") === m })));
  const preset = h("select", {},
    h("option", { value: "custom", text: "Prompt gọn (viết lách, phân tích, quản lý)", selected: !p || p.prompt_preset !== "claude_code" }),
    h("option", { value: "claude_code", text: "Preset Claude Code (lập trình)", selected: p && p.prompt_preset === "claude_code" }));
  const tools = h("div", { class: "checks inline" }, TOOL_CHOICES.map((t) => h("label", {}, h("input", { type: "checkbox", value: t, checked: p ? p.tools.includes(t) : ["Read", "Write", "Edit", "Glob", "Grep"].includes(t) }), t)));
  const rule = (v) => h("select", {}, [["allow", "cho phép"], ["ask", "hỏi"], ["deny", "cấm"]].map(([k, t]) => h("option", { value: k, text: t, selected: k === v })));
  const exec = rule(p ? p.policy.exec_default : "ask");
  const web = rule(p ? p.policy.web : "deny");
  const external = rule(p ? p.policy.external : "ask");
  const bashAllow = h("textarea", { rows: 4, class: "mono", placeholder: "git status*\nuv run *\nnpm test*" });
  bashAllow.value = p ? p.policy.bash_allow.join("\n") : "";
  const maxTurns = h("input", { type: "number", value: p ? p.policy.max_turns : 40, min: 5, max: 200 });
  const doneDocs = h("input", { type: "text", value: p ? (p.done_docs || []).join(", ") : "", placeholder: "handoff, test-report (để trống = không bắt buộc)" });
  modal(p ? `Sửa vị trí: ${p.title}` : "Vị trí mới", h("div", {},
    h("div", { class: "form-grid" }, field("Mã", name), field("Tên hiển thị", title), field("Model", model), field("Kiểu prompt", preset)),
    field("Hướng dẫn vai trò", instructions),
    h("span", { class: "sub", text: "Công cụ được dùng" }), tools,
    h("div", { class: "form-grid" }, field("Lệnh shell", exec), field("Đọc web", web), field("Hành động ra ngoài", external), field("Số bước tối đa", maxTurns)),
    field("Lệnh shell luôn cho phép (mỗi dòng một mẫu)", bashAllow),
    field("Tài liệu bắt buộc trước khi báo xong (Definition of Done)", doneDocs)), async () => {
    const body = {
      name: name.value.trim(), title: title.value.trim(), instructions: instructions.value, model: model.value,
      prompt_preset: preset.value, workspace_kind: p ? p.workspace_kind : "folder",
      tools: [...tools.querySelectorAll("input:checked")].map((i) => i.value),
      exec_default: exec.value, web: web.value, external: external.value,
      bash_allow: bashAllow.value.split("\n"), max_turns: Number(maxTurns.value || 40),
      done_docs: doneDocs.value.split(",").map((x) => x.trim()).filter(Boolean),
    };
    await api(p ? "PUT" : "POST", p ? `/api/positions/${p.id}` : "/api/positions", body);
    await loadState(); render();
    toast(p ? "Đã lưu vị trí" : "Đã tạo vị trí");
    return true;
  }, p ? "Lưu" : "Tạo", true);
}

function projectModal(p) {
  const name = h("input", { type: "text", value: p ? p.name : "", placeholder: "GoRoute" });
  const desc = h("input", { type: "text", value: p ? p.description : "" });
  const path = h("input", { type: "text", value: p && p.path ? p.path : "", placeholder: "F:\\00_goroute (để trống nếu không gắn thư mục)" });
  const access = h("select", {},
    h("option", { value: "read_only", text: "Chỉ đọc — ticket làm ở thư mục riêng, được đọc thư mục dự án", selected: !p || p.access === "read_only" }),
    h("option", { value: "worktree", text: "Git worktree — mỗi ticket một branch (an toàn, cần git repo)", selected: p && p.access === "worktree" }),
    h("option", { value: "direct", text: "Sửa trực tiếp — sửa ngay trong thư mục, mỗi lúc một ticket", selected: p && p.access === "direct" }));
  modal(p ? `Dự án: ${p.name}` : "Dự án mới", h("div", {},
    field("Tên", name), field("Mô tả", desc), field("Thư mục", path), field("Cách làm việc với thư mục", access),
    h("p", { class: "sub", text: "Mỗi dự án có tin nhắn, nhóm và tickets riêng; nhân sự dùng chung. Trong chat, nhân viên được đọc thư mục dự án (không sửa). Không được chọn gốc ổ đĩa, thư mục home hay dữ liệu văn phòng." })), async () => {
    const body = { name: name.value.trim(), description: desc.value, path: path.value.trim() || null, access: access.value };
    const saved = await api(p ? "PUT" : "POST", p ? `/api/projects/${p.id}` : "/api/projects", body);
    await loadState();
    setProject(saved.id);
    go("#/");
    toast(p ? "Đã lưu dự án" : `Đã tạo dự án ${saved.name}`);
    return true;
  }, p ? "Lưu" : "Tạo");
}

async function skillModal(ref, isNew) {
  let skill = null;
  if (!isNew && ref.name) skill = await act(() => api("GET", `/api/skills/${ref.scope}/${encodeURIComponent(ref.owner || "-")}/${ref.name}`));
  const scopeSel = h("select", {},
    h("option", { value: "shared|", text: "Chung (mọi nhân viên)", selected: ref.scope === "shared" }),
    S.positions.map((p) => h("option", { value: `position|${p.name}`, text: `Vị trí: ${p.title}`, selected: ref.scope === "position" && ref.owner === p.name })),
    S.actors.map((a) => h("option", { value: `actor|${a.name}`, text: `Nhân viên: ${a.name}`, selected: ref.scope === "actor" && ref.owner === a.name })));
  const name = h("input", { type: "text", value: skill ? skill.ref.name : "", placeholder: "release-checklist" });
  const desc = h("input", { type: "text", value: skill ? skill.description : "", placeholder: "Dùng khi nào (Claude dựa vào đây để chọn skill)" });
  const body = h("textarea", { rows: 14, class: "mono", placeholder: "Các bước, checklist, mẫu…" });
  body.value = skill ? skill.body : "";
  if (skill) { scopeSel.disabled = true; name.disabled = true; }
  const refOf = () => {
    const [scope, owner] = scopeSel.value.split("|");
    return { scope, owner: owner || "-", name: name.value.trim() };
  };
  const files = skill ? h("div", { class: "box" }, h("h4", { text: "Sub-skill / file tham khảo" }),
    skill.files.length ? skill.files.map((f) => h("div", { class: "trow" }, h("code", { class: "t", text: f }),
      h("button", { class: "btn sm", text: "Mở", onclick: () => fileModal(skill.ref, f) }),
      h("button", { class: "btn sm danger", text: "Xoá", onclick: () => act(async () => {
        const r = refOf();
        await api("DELETE", `/api/skills/${r.scope}/${encodeURIComponent(r.owner)}/${r.name}/files/${f}`);
        skillModal(skill.ref);
      }) }))) : h("div", { class: "sub", text: "Chưa có. Ví dụ: checklists/release.md" }),
    h("button", { class: "btn sm", text: "+ File", onclick: () => fileModal(skill.ref, "") })) : null;
  const del = skill ? h("button", { class: "btn danger", text: "Xoá skill", onclick: () => {
    if (!confirm(`Xoá skill ${skill.ref.name}?`)) return;
    act(async () => { const r = refOf(); await api("DELETE", `/api/skills/${r.scope}/${encodeURIComponent(r.owner)}/${r.name}`); document.getElementById("modal-root").replaceChildren(); renderMain(); });
  } }) : null;
  modal(skill ? `Skill: ${skill.ref.name}` : "Skill mới", h("div", {},
    h("div", { class: "form-grid" }, field("Phạm vi", scopeSel), field("Tên (chữ thường, dấu -)", name)),
    field("Mô tả", desc), field("Nội dung SKILL.md", body), files, del,
    skill && skill.created_by ? h("p", { class: "sub", text: `Tạo bởi ${skill.created_by} · cập nhật ${time(skill.updated_at)}` }) : null), async () => {
    const r = refOf();
    await api("PUT", `/api/skills/${r.scope}/${encodeURIComponent(r.owner)}/${r.name}`, { description: desc.value, body: body.value });
    toast("Đã lưu skill");
    renderMain();
    return true;
  }, "Lưu", true);
}

async function fileModal(ref, path) {
  const owner = encodeURIComponent(ref.owner || "-");
  const data = path ? await act(() => api("GET", `/api/skills/${ref.scope}/${owner}/${ref.name}/files/${path}`)) : null;
  const name = h("input", { type: "text", value: path, placeholder: "checklists/release.md" });
  const content = h("textarea", { rows: 16, class: "mono" });
  content.value = data ? data.content : "";
  if (path) name.disabled = true;
  modal(path ? `${ref.name} / ${path}` : `File mới trong ${ref.name}`, h("div", {}, field("Đường dẫn trong skill", name), field("Nội dung", content)), async () => {
    await api("PUT", `/api/skills/${ref.scope}/${owner}/${ref.name}/files/${name.value.trim()}`, { content: content.value });
    toast("Đã lưu file");
    skillModal(ref);
    return false;  // skillModal replaced this dialog already
  }, "Lưu", true);
}

function actorChecks(exclude) {
  return h("div", { class: "checks" }, S.actors.filter((a) => !(exclude || []).includes(a.id)).map((a) => {
    const pos = positionById(a.position_id);
    return h("label", {}, h("input", { type: "checkbox", value: a.id }), avatar(a, true), a.name, h("span", { class: "sub", text: pos ? pos.title : "" }));
  }));
}

function newGroupModal() {
  const project = currentProject();
  const title = h("input", { type: "text", placeholder: "Tính năng đăng nhập" });
  const checks = actorChecks();
  modal(`Tạo nhóm trong ${project ? project.name : ""}`, h("div", {}, field("Tên nhóm", title), h("span", { class: "sub", text: "Thành viên (bạn luôn có mặt)" }), checks), async () => {
    const ids = [...checks.querySelectorAll("input:checked")].map((i) => i.value);
    const conv = await api("POST", "/api/conversations", { kind: "group", title: title.value.trim(), actor_ids: ids, project_id: project && project.id });
    if (!S.conversations.some((c) => c.id === conv.id)) S.conversations.unshift(conv);
    go(`#/c/${conv.id}`);
    return true;
  }, "Tạo");
}

function addMembersModal() {
  const checks = actorChecks(S.conv.members.filter((m) => m.kind === "actor").map((m) => m.id));
  modal("Thêm người vào nhóm", checks, async () => {
    const ids = [...checks.querySelectorAll("input:checked")].map((i) => i.value);
    if (ids.length) S.conv = await api("POST", `/api/conversations/${S.conv.id}/members`, { actor_ids: ids });
    renderConversationHeader(); renderDetails();
    return true;
  }, "Thêm");
}

function newTicketModal() {
  const project = currentProject();
  const title = h("input", { type: "text" });
  const desc = h("textarea", { rows: 5, placeholder: "Mục tiêu, phạm vi, tiêu chí hoàn thành…" });
  const who = h("select", {}, h("option", { value: "", text: "— chưa giao —" }), S.actors.map((a) => h("option", { value: a.id, text: a.name })));
  const prio = h("select", {}, ["medium", "high", "critical", "low"].map((p) => h("option", { value: p, text: p })));
  const type = h("select", {}, Object.entries(TYPE_VI).filter(([k]) => k !== "automation_fix").map(([k, v]) => h("option", { value: k, text: v })));
  const due = h("input", { type: "date" });
  modal(`Ticket mới · ${project ? project.name : ""}`, h("div", {}, field("Tiêu đề", title), field("Mô tả", desc),
    h("div", { class: "form-grid" }, field("Giao cho", who), field("Loại", type), field("Ưu tiên", prio), field("Hạn", due))), async () => {
    if (!title.value.trim()) throw new Error("Cần tiêu đề");
    const t = await api("POST", "/api/tickets", { title: title.value.trim(), description: desc.value, assignee_id: who.value || null, priority: prio.value, type: type.value, due_at: due.value || null, project_id: project && project.id });
    go(`#/t/${t.key}`);
    return true;
  }, "Tạo");
}

// ---------------------------------------------------------------------------------------------

act(async () => { await loadState(); S.booted = true; render(); });
// Safety net: if a page is still "loading" after a few seconds (a fetch was superseded or lost), render again.
setInterval(() => {
  const main = document.getElementById("main");
  const loading = main && main.children.length === 1 && main.firstElementChild.classList.contains("empty") && main.textContent === "Đang tải…";
  if (S.booted && loading && Date.now() - (S.loadingSince || 0) > 5000) renderMain();
}, 2000);
connect();
