"use strict";

const $ = (sel, root = document) => root.querySelector(sel);
const $$ = (sel, root = document) => [...root.querySelectorAll(sel)];
const esc = (v) => String(v ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const main = $("#main");

const LEAD_STATUSES = ["new", "calling", "retry", "callback", "contacted", "interested", "meeting_booked", "not_interested", "do_not_call", "wrong_number", "invalid", "exhausted", "blocked"];
const DAYS = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"];
const VOICES = ["marin", "cedar", "alloy", "ash", "ballad", "coral", "echo", "sage", "shimmer", "verse"];

let token = localStorage.getItem("agentvoice_token") || "";
let refreshTimer = null;
let currentView = "overview";

// ------------------------------------------------------------------ helpers
async function api(path, { method = "GET", body, raw } = {}) {
  const headers = { Authorization: `Bearer ${token}` };
  let payload;
  if (raw !== undefined) { payload = raw; headers["Content-Type"] = "text/csv"; }
  else if (body !== undefined) { payload = JSON.stringify(body); headers["Content-Type"] = "application/json"; }
  const res = await fetch(path, { method, headers, body: payload });
  if (res.status === 401) { showLogin(); throw new Error("Not signed in"); }
  if (res.status === 204) return null;
  const data = await res.json().catch(() => ({}));
  if (!res.ok) {
    const detail = Array.isArray(data.detail) ? data.detail.map((d) => d.msg).join("; ") : data.detail;
    throw new Error(detail || `Request failed (${res.status})`);
  }
  return data;
}

function toast(msg, bad = false) {
  const t = $("#toast");
  t.textContent = msg;
  t.className = "toast" + (bad ? " bad" : "");
  clearTimeout(t._timer);
  t._timer = setTimeout(() => t.classList.add("hidden"), 4500);
}

function fmtTime(iso) {
  if (!iso) return "";
  const d = new Date(iso);
  return d.toLocaleString(undefined, { month: "short", day: "numeric", hour: "numeric", minute: "2-digit" });
}

function fmtDur(s) {
  s = Number(s || 0);
  return s ? `${Math.floor(s / 60)}:${String(s % 60).padStart(2, "0")}` : "";
}

function statusBadge(s) {
  const cls = { meeting_booked: "good", interested: "good", hot: "good", completed: "info", "in-progress": "info", calling: "info", ringing: "info", initiated: "info",
    callback: "warn", retry: "warn", warm: "warn", contacted: "info", not_interested: "bad", do_not_call: "bad", blocked: "bad", failed: "bad", wrong_number: "bad", cold: "bad", exhausted: "bad" }[s] || "";
  return s ? `<span class="badge ${cls}">${esc(String(s).replace(/_/g, " "))}</span>` : "";
}

function openModal(html) {
  $("#modal-body").innerHTML = html;
  $("#modal").classList.remove("hidden");
}
function closeModal() {
  $("#modal").classList.add("hidden");
  $("#modal-body").innerHTML = "";
  if (window._modalTimer) { clearInterval(window._modalTimer); window._modalTimer = null; }
}
$("#modal").addEventListener("click", (e) => { if (e.target.id === "modal") closeModal(); });
document.addEventListener("keydown", (e) => { if (e.key === "Escape") closeModal(); });

function setRefresh(fn, ms) {
  clearInterval(refreshTimer);
  refreshTimer = fn ? setInterval(() => fn().catch(() => {}), ms) : null;
}

function transcriptHtml(items) {
  if (!items.length) return `<p class="muted">No transcript yet.</p>`;
  return `<div class="transcript">${items.map((t) =>
    `<div class="msg ${esc(t.role)}">${t.role === "tool" ? "" : `<div class="who">${t.role === "agent" ? "Agent" : "Prospect"}</div>`}${esc(t.text)}</div>`).join("")}</div>`;
}

// -------------------------------------------------------------------- auth
function showLogin() {
  $("#app").classList.add("hidden");
  $("#login").classList.remove("hidden");
  setRefresh(null);
}
$("#login-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  token = $("#login-token").value.trim();
  try {
    await api("/api/status");
    localStorage.setItem("agentvoice_token", token);
    $("#login").classList.add("hidden");
    $("#app").classList.remove("hidden");
    show("overview");
  } catch (err) {
    $("#login-error").textContent = "Invalid token";
  }
});
$("#logout").addEventListener("click", () => { localStorage.removeItem("agentvoice_token"); token = ""; showLogin(); });

$("#nav").addEventListener("click", (e) => { const v = e.target.dataset.view; if (v) show(v); });

function show(view) {
  currentView = view;
  $$("#nav a").forEach((a) => a.classList.toggle("active", a.dataset.view === view));
  setRefresh(null);
  if (view !== "practice") Practice.stop();
  const fn = Views[view];
  main.innerHTML = `<p class="muted">Loading…</p>`;
  fn().catch((err) => { main.innerHTML = `<div class="card error">${esc(err.message)}</div>`; });
}

// ------------------------------------------------------------------- views
const Views = {};

Views.overview = async () => {
  const render = async () => {
    const [st, active, meetings] = await Promise.all([
      api("/api/status"), api("/api/calls?active=true&limit=50"), api("/api/meetings?upcoming=true"),
    ]);
    $("#health").textContent = `v${st.version} · ${st.running_campaigns.length} campaign(s) running`;
    const warn = [];
    if (!st.voice_available) warn.push("No voice backend: set GEMINI_API_KEY, start Ollama, or set OPENAI_API_KEY.");
    else if (st.voice_backend === "gemini") warn.push("Voice is Gemini (" + (st.gemini_model || "flash") + ") + local Whisper/Piper. Practice agent opens first.");
    else if (st.voice_backend === "openai_chat") warn.push("Voice is OpenAI chat (" + ((st.llm || "").split(":")[1] || "gpt-4o-mini") + ") + local Whisper/Piper. Practice agent opens first.");
    else if (st.voice_backend === "ollama") warn.push("Voice is local Ollama (" + (st.ollama_model || "llama3.2") + "). Free, a bit slower, fine for practice.");
    if (!st.twilio_configured) warn.push("Twilio is not configured in .env (TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN, TWILIO_FROM_NUMBER, PUBLIC_BASE_URL): phone calls are disabled. Practice mode still works.");
    const stat = (v, l) => `<div class="stat"><div class="v">${esc(v)}</div><div class="l">${esc(l)}</div></div>`;
    const t = st.today, w = st.last_7_days;
    main.innerHTML = `
      <h1>Overview</h1>
      ${warn.map((w) => `<div class="card" style="border-color:var(--warn)">${esc(w)}</div>`).join("")}
      <h2>Today</h2>
      <div class="grid">${stat(t.dials, "Dials")}${stat(t.conversations, "Conversations")}${stat(t.meetings_booked, "Meetings booked")}${stat(t.voicemails, "Voicemails")}${stat(t.talk_minutes, "Talk minutes")}${stat(t.active_calls, "Live calls")}</div>
      <h2 style="margin-top:22px">Last 7 days</h2>
      <div class="grid">${stat(w.dials, "Dials")}${stat(w.conversations, "Conversations")}${stat(w.meetings_booked, "Meetings")}${stat(w.conversations ? Math.round((100 * w.meetings_booked) / w.conversations) + "%" : "–", "Conv → meeting")}</div>
      <div class="card" style="margin-top:22px"><h2>Live calls</h2>
        ${active.length ? `<table><tr><th>Call</th><th>Business</th><th>Phone</th><th>Status</th><th>Started</th><th></th></tr>
          ${active.map((c) => `<tr class="click" data-call="${c.id}"><td>#${c.id}</td><td>${esc(c.business_name || c.kind)}</td><td>${esc(c.phone)}</td><td>${statusBadge(c.status)}</td><td>${fmtTime(c.created_at)}</td><td><button data-hangup="${c.id}" class="danger">Hang up</button></td></tr>`).join("")}</table>`
          : `<p class="muted">No calls in progress.</p>`}
      </div>
      <div class="card"><h2>Upcoming meetings</h2>
        ${meetings.length ? `<table><tr><th>When</th><th>Business</th><th>Contact</th><th>Email</th></tr>
          ${meetings.slice(0, 10).map((m) => `<tr><td>${fmtTime(m.starts_at)}</td><td>${esc(m.business_name)}</td><td>${esc(m.contact_name)}</td><td>${esc(m.email)}</td></tr>`).join("")}</table>`
          : `<p class="muted">No upcoming meetings yet.</p>`}
      </div>`;
    bindCallRows();
  };
  await render();
  setRefresh(render, 4000);
};

function bindCallRows(root = main) {
  $$("[data-hangup]", root).forEach((b) => b.addEventListener("click", async (e) => {
    e.stopPropagation();
    await api(`/api/calls/${b.dataset.hangup}/hangup`, { method: "POST" }).catch((err) => toast(err.message, true));
    toast("Hanging up");
  }));
  $$("tr[data-call]", root).forEach((r) => r.addEventListener("click", () => openCall(r.dataset.call)));
}

async function openCall(id) {
  const render = async () => {
    const c = await api(`/api/calls/${id}`);
    const lead = c.lead || {};
    $("#modal-body").innerHTML = `
      <div class="row"><h2>Call #${c.id} ${c.live ? '<span class="badge info">LIVE</span>' : ""}</h2><div class="spacer"></div>
        ${c.live ? `<button class="danger" id="call-hangup">Hang up</button>` : ""}<button id="close-modal">Close</button></div>
      <p>${esc(lead.business_name || c.kind)} · ${esc(c.phone)} · ${esc(c.direction)} · ${statusBadge(c.status)} ${statusBadge(c.outcome)} ${statusBadge(c.lead_score)}</p>
      <p class="muted small">${fmtTime(c.created_at)} · duration ${fmtDur(c.duration_s) || "–"} ${c.answered_by ? "· answered by " + esc(c.answered_by) : ""}</p>
      ${c.error ? `<p class="error">${esc(c.error)}</p>` : ""}
      ${c.summary ? `<div class="card"><h2>Summary</h2><p style="white-space:pre-wrap">${esc(c.summary)}</p>${c.next_step ? `<p><b>Next step:</b> ${esc(c.next_step)}</p>` : ""}</div>` : ""}
      <h2>Transcript</h2>${transcriptHtml(c.transcript)}`;
    $("#close-modal").onclick = closeModal;
    if ($("#call-hangup")) $("#call-hangup").onclick = () => api(`/api/calls/${id}/hangup`, { method: "POST" }).then(() => toast("Hanging up"));
    const tr = $(".transcript");
    if (tr && c.live) tr.scrollTop = tr.scrollHeight;
    return c.live;
  };
  openModal("Loading…");
  const live = await render();
  if (live) window._modalTimer = setInterval(() => render().then((still) => { if (!still) { clearInterval(window._modalTimer); window._modalTimer = null; } }), 2000);
}

// --------------------------------------------------------------- campaigns
Views.campaigns = async () => {
  const list = await api("/api/campaigns");
  main.innerHTML = `
    <div class="row"><h1>Campaigns</h1><div class="spacer"></div><button class="primary" id="new-campaign">New campaign</button></div>
    <div class="card">${list.length ? `<table><tr><th>Name</th><th>Status</th><th>Leads</th><th>Remaining</th><th>Meetings</th><th>Concurrent</th><th>Attempts</th><th></th></tr>
      ${list.map((c) => `<tr><td>${esc(c.name)}</td><td>${statusBadge(c.status)}</td><td>${c.lead_count}</td><td>${c.remaining}</td><td>${c.meetings}</td><td>${c.max_concurrent}</td><td>${c.max_attempts}</td>
        <td class="row">${c.status === "running" ? `<button data-pause="${c.id}">Pause</button>` : `<button class="primary" data-start="${c.id}">Start</button>`}
        <button data-edit="${c.id}">Edit</button><button data-leads="${c.id}">Leads</button><button class="danger" data-del="${c.id}">Delete</button></td></tr>`).join("")}</table>`
      : `<p class="muted">No campaigns yet. Create one, then import leads into it.</p>`}</div>`;
  $("#new-campaign").onclick = () => campaignForm();
  $$("[data-start]").forEach((b) => b.onclick = () => api(`/api/campaigns/${b.dataset.start}/start`, { method: "POST" }).then(Views.campaigns).catch((e) => toast(e.message, true)));
  $$("[data-pause]").forEach((b) => b.onclick = () => api(`/api/campaigns/${b.dataset.pause}/pause`, { method: "POST" }).then(Views.campaigns));
  $$("[data-edit]").forEach((b) => b.onclick = () => campaignForm(list.find((c) => c.id == b.dataset.edit)));
  $$("[data-leads]").forEach((b) => b.onclick = () => { LeadsState.campaign = b.dataset.leads; show("leads"); });
  $$("[data-del]").forEach((b) => b.onclick = async () => {
    if (!confirm("Delete this campaign? Uncalled leads are removed; called leads keep their history.")) return;
    await api(`/api/campaigns/${b.dataset.del}`, { method: "DELETE" });
    Views.campaigns();
  });
};

function campaignForm(c = null) {
  openModal(`<h2>${c ? "Edit campaign" : "New campaign"}</h2>
    <form id="cf"><div class="form-grid">
      <div><label>Name</label><input name="name" required value="${esc(c?.name || "")}"></div>
      <div><label>Simultaneous calls</label><input name="max_concurrent" type="number" min="1" max="20" value="${c?.max_concurrent ?? 1}"></div>
      <div><label>Max attempts per lead</label><input name="max_attempts" type="number" min="1" max="6" value="${c?.max_attempts ?? 3}"></div>
      <div><label>Minutes between attempts</label><input name="retry_minutes" type="number" min="60" value="${c?.retry_minutes ?? 1440}"></div>
    </div>
    <label>Campaign focus (extra instructions for the agent, e.g. "These are restaurants; lead with online ordering.")</label>
    <textarea name="extra_instructions">${esc(c?.extra_instructions || "")}</textarea>
    <div class="row" style="margin-top:14px"><button class="primary">Save</button><button type="button" id="cancel">Cancel</button></div></form>`);
  $("#cancel").onclick = closeModal;
  $("#cf").onsubmit = async (e) => {
    e.preventDefault();
    const f = Object.fromEntries(new FormData(e.target));
    ["max_concurrent", "max_attempts", "retry_minutes"].forEach((k) => (f[k] = Number(f[k])));
    try {
      await api(c ? `/api/campaigns/${c.id}` : "/api/campaigns", { method: c ? "PATCH" : "POST", body: f });
      closeModal(); Views.campaigns();
    } catch (err) { toast(err.message, true); }
  };
}

// ------------------------------------------------------------------- leads
const LeadsState = { campaign: "", status: "", q: "", offset: 0, limit: 100 };

Views.leads = async () => {
  const campaigns = await api("/api/campaigns");
  const p = new URLSearchParams({ limit: LeadsState.limit, offset: LeadsState.offset });
  if (LeadsState.campaign) p.set("campaign_id", LeadsState.campaign);
  if (LeadsState.status) p.set("status", LeadsState.status);
  if (LeadsState.q) p.set("q", LeadsState.q);
  const data = await api(`/api/leads?${p}`);
  const cOpts = (sel) => `<option value="">All campaigns</option>${campaigns.map((c) => `<option value="${c.id}" ${String(sel) === String(c.id) ? "selected" : ""}>${esc(c.name)}</option>`).join("")}`;
  main.innerHTML = `
    <div class="row"><h1>Leads</h1><div class="spacer"></div><button id="import">Import CSV</button><button class="primary" id="add">Add lead</button></div>
    <div class="card row">
      <select id="f-campaign">${cOpts(LeadsState.campaign)}</select>
      <select id="f-status"><option value="">Any status</option>${LEAD_STATUSES.map((s) => `<option ${s === LeadsState.status ? "selected" : ""}>${s}</option>`).join("")}</select>
      <input id="f-q" placeholder="Search name, phone, city…" value="${esc(LeadsState.q)}">
      <span class="muted">${data.total} lead(s)</span>
    </div>
    <div class="card"><table><tr><th>Business</th><th>Contact</th><th>Phone</th><th>Location</th><th>Status</th><th>Score</th><th>Attempts</th><th>Updated</th></tr>
      ${data.items.map((l) => `<tr class="click" data-lead="${l.id}"><td>${esc(l.business_name)}</td><td>${esc(l.contact_name)}</td><td>${esc(l.phone)}</td><td>${esc([l.city, l.state].filter(Boolean).join(", "))}</td>
        <td>${statusBadge(l.status)}${l.consent ? "" : ' <span class="badge warn" title="No recorded consent">no consent</span>'}</td><td>${statusBadge(l.score)}</td><td>${l.attempts}</td><td>${fmtTime(l.updated_at)}</td></tr>`).join("")}
    </table>
    <div class="row pager"><button id="prev" ${LeadsState.offset ? "" : "disabled"}>Previous</button><button id="next" ${LeadsState.offset + LeadsState.limit < data.total ? "" : "disabled"}>Next</button></div></div>`;
  $("#f-campaign").onchange = (e) => { LeadsState.campaign = e.target.value; LeadsState.offset = 0; Views.leads(); };
  $("#f-status").onchange = (e) => { LeadsState.status = e.target.value; LeadsState.offset = 0; Views.leads(); };
  let qt; $("#f-q").oninput = (e) => { clearTimeout(qt); qt = setTimeout(() => { LeadsState.q = e.target.value; LeadsState.offset = 0; Views.leads().then(() => { const i = $("#f-q"); i.focus(); i.setSelectionRange(i.value.length, i.value.length); }); }, 350); };
  $("#prev").onclick = () => { LeadsState.offset = Math.max(0, LeadsState.offset - LeadsState.limit); Views.leads(); };
  $("#next").onclick = () => { LeadsState.offset += LeadsState.limit; Views.leads(); };
  $$("tr[data-lead]").forEach((r) => r.onclick = () => openLead(r.dataset.lead));
  $("#add").onclick = () => leadForm(campaigns);
  $("#import").onclick = () => importForm(campaigns);
};

function importForm(campaigns) {
  openModal(`<h2>Import leads from CSV</h2>
    <p class="muted">Columns are detected automatically: phone (required), business, contact/name, email, website, industry, city, state, timezone, consent, consent_source, notes.</p>
    <label>Campaign</label><select id="imp-campaign"><option value="">(none)</option>${campaigns.map((c) => `<option value="${c.id}">${esc(c.name)}</option>`).join("")}</select>
    <label>CSV file</label><input type="file" id="imp-file" accept=".csv,text/csv,text/plain">
    <label><input type="checkbox" id="imp-consent"> Every row without a consent column has documented prior express written consent to receive AI calls (TCPA).</label>
    <div class="row" style="margin-top:14px"><button class="primary" id="imp-go">Import</button><button id="cancel">Cancel</button></div><pre id="imp-result" class="muted small"></pre>`);
  $("#cancel").onclick = closeModal;
  $("#imp-go").onclick = async () => {
    const file = $("#imp-file").files[0];
    if (!file) return toast("Choose a file", true);
    const p = new URLSearchParams();
    if ($("#imp-campaign").value) p.set("campaign_id", $("#imp-campaign").value);
    if ($("#imp-consent").checked) p.set("consent_confirmed", "true");
    try {
      const r = await api(`/api/leads/import?${p}`, { method: "POST", raw: await file.text() });
      $("#imp-result").textContent = `Imported ${r.imported}, updated ${r.updated}, skipped ${r.skipped_count ?? r.skipped.length}\n` + r.skipped.slice(0, 20).map((s) => `row ${s.row}: ${s.reason}`).join("\n");
      if (currentView === "leads") Views.leads();
    } catch (err) { toast(err.message, true); }
  };
}

function leadForm(campaigns, lead = null) {
  const v = (k) => esc(lead?.[k] ?? "");
  openModal(`<h2>${lead ? "Edit lead" : "Add lead"}</h2><form id="lf"><div class="form-grid">
    <div><label>Phone *</label><input name="phone" required value="${v("phone")}"></div>
    <div><label>Business name</label><input name="business_name" value="${v("business_name")}"></div>
    <div><label>Contact name</label><input name="contact_name" value="${v("contact_name")}"></div>
    <div><label>Email</label><input name="email" value="${v("email")}"></div>
    <div><label>Website</label><input name="website" value="${v("website")}"></div>
    <div><label>Industry</label><input name="industry" value="${v("industry")}"></div>
    <div><label>City</label><input name="city" value="${v("city")}"></div>
    <div><label>State (e.g. TX)</label><input name="state" value="${v("state")}"></div>
    <div><label>Time zone (optional, e.g. America/Chicago)</label><input name="timezone" value="${v("timezone")}"></div>
    <div><label>Campaign</label><select name="campaign_id"><option value="">(none)</option>${campaigns.map((c) => `<option value="${c.id}" ${lead?.campaign_id == c.id ? "selected" : ""}>${esc(c.name)}</option>`).join("")}</select></div>
    <div><label>Consent source (how/when they opted in)</label><input name="consent_source" value="${v("consent_source")}"></div>
    <div><label>&nbsp;</label><label><input type="checkbox" name="consent" ${lead?.consent ? "checked" : ""}> Has prior express written consent</label></div>
    </div><label>Notes</label><textarea name="notes">${v("notes")}</textarea>
    <div class="row" style="margin-top:14px"><button class="primary">Save</button><button type="button" id="cancel">Cancel</button></div></form>`);
  $("#cancel").onclick = closeModal;
  $("#lf").onsubmit = async (e) => {
    e.preventDefault();
    const f = Object.fromEntries(new FormData(e.target));
    f.consent = !!e.target.consent.checked;
    f.campaign_id = f.campaign_id ? Number(f.campaign_id) : null;
    try {
      await api(lead ? `/api/leads/${lead.id}` : "/api/leads", { method: lead ? "PATCH" : "POST", body: f });
      closeModal(); if (currentView === "leads") Views.leads();
    } catch (err) { toast(err.message, true); }
  };
}

async function openLead(id) {
  const [l, campaigns] = await Promise.all([api(`/api/leads/${id}`), api("/api/campaigns")]);
  const q = l.qualification || {};
  const research = l.research || {};
  openModal(`
    <div class="row"><h2>${esc(l.business_name || l.phone)}</h2><div class="spacer"></div>
      <button class="primary" id="l-call" ${l.callable_now ? "" : "disabled"} title="${esc(l.callable_reason)}">Call now</button>
      <button id="l-practice">Practice with this lead</button><button id="l-edit">Edit</button><button id="close-modal">Close</button></div>
    <p>${statusBadge(l.status)} ${statusBadge(l.score)} ${l.dnc ? '<span class="badge bad">DNC</span>' : ""} ${esc(l.status_reason)}</p>
    <p class="muted">${esc(l.phone)} · ${esc(l.contact_name)} ${l.email ? "· " + esc(l.email) : ""} · ${esc([l.city, l.state].filter(Boolean).join(", "))} · local zone ${esc(l.local_timezone)}</p>
    ${l.callable_now ? "" : `<p class="small" style="color:var(--warn)">Not callable right now: ${esc(l.callable_reason)}</p>`}
    <div class="form-grid">
      <div class="card"><h2>What the agent learned</h2>${Object.keys(q).length ? `<table>${Object.entries(q).map(([k, v]) => `<tr><td class="muted">${esc(k.replace(/_/g, " "))}</td><td>${esc(v)}</td></tr>`).join("")}</table>` : `<p class="muted">Nothing yet.</p>`}</div>
      <div class="card"><h2>Website research</h2>${(research.talking_points || []).map((p) => `<p>• ${esc(p)}</p>`).join("") || `<p class="muted">Runs automatically before the first call.</p>`}</div>
    </div>
    <div class="card"><h2>Calls</h2>${l.calls.length ? `<table><tr><th>When</th><th>Status</th><th>Outcome</th><th>Duration</th><th>Summary</th></tr>${l.calls.map((c) => `<tr class="click" data-call="${c.id}"><td>${fmtTime(c.created_at)}</td><td>${statusBadge(c.status)}</td><td>${statusBadge(c.outcome)}</td><td>${fmtDur(c.duration_s)}</td><td class="small">${esc((c.summary || "").slice(0, 160))}</td></tr>`).join("")}</table>` : `<p class="muted">No calls yet.</p>`}</div>
    <div class="row"><select id="l-status">${LEAD_STATUSES.map((s) => `<option ${s === l.status ? "selected" : ""}>${s}</option>`).join("")}</select><button id="l-set-status">Set status</button>
      <div class="spacer"></div><button class="danger" id="l-dnc">Add to do-not-call</button><button class="danger" id="l-del">Delete</button></div>`);
  $("#close-modal").onclick = closeModal;
  $("#l-edit").onclick = () => leadForm(campaigns, l);
  $("#l-practice").onclick = () => { closeModal(); Practice.leadId = l.id; show("practice"); };
  $("#l-call").onclick = async () => {
    try { const c = await api(`/api/leads/${id}/call`, { method: "POST" }); toast(`Dialing… (call #${c.id})`); closeModal(); }
    catch (err) { toast(err.message, true); }
  };
  $("#l-set-status").onclick = () => api(`/api/leads/${id}`, { method: "PATCH", body: { status: $("#l-status").value } }).then(() => { toast("Saved"); openLead(id); }).catch((e) => toast(e.message, true));
  $("#l-dnc").onclick = async () => { if (confirm("Never call this number again?")) { await api("/api/dnc", { method: "POST", body: { phone: l.phone, reason: "manual" } }); openLead(id); } };
  $("#l-del").onclick = async () => { if (confirm("Delete this lead?")) { await api(`/api/leads/${id}`, { method: "DELETE" }); closeModal(); Views.leads(); } };
  bindCallRows($("#modal-body"));
}

// ------------------------------------------------------------------- calls
Views.calls = async () => {
  const render = async () => {
    const calls = await api("/api/calls?limit=200");
    main.innerHTML = `<div class="row"><h1>Calls</h1><div class="spacer"></div><button id="test-call">Test call to my phone</button></div>
      <div class="card"><table><tr><th>#</th><th>When</th><th>Business</th><th>Phone</th><th>Type</th><th>Status</th><th>Outcome</th><th>Score</th><th>Duration</th></tr>
      ${calls.map((c) => `<tr class="click" data-call="${c.id}"><td>${c.id}</td><td>${fmtTime(c.created_at)}</td><td>${esc(c.business_name)}</td><td>${esc(c.phone)}</td><td>${esc(c.kind)}</td><td>${statusBadge(c.status)}</td><td>${statusBadge(c.outcome)}</td><td>${statusBadge(c.lead_score)}</td><td>${fmtDur(c.duration_s)}</td></tr>`).join("")}</table></div>`;
    bindCallRows();
    $("#test-call").onclick = async () => {
      try { const c = await api("/api/test-call", { method: "POST", body: {} }); toast(`Calling your test number (call #${c.id})`); }
      catch (err) { toast(err.message, true); }
    };
  };
  await render();
  setRefresh(render, 5000);
};

// ---------------------------------------------------------------- meetings
Views.meetings = async () => {
  const list = await api("/api/meetings");
  main.innerHTML = `<h1>Meetings</h1><div class="card"><table><tr><th>When</th><th>Business</th><th>Contact</th><th>Email</th><th>Phone</th><th>Notes</th><th>Status</th></tr>
    ${list.map((m) => `<tr><td>${fmtTime(m.starts_at)}<div class="muted small">${esc(m.timezone)}</div></td><td>${esc(m.business_name)}<div class="muted small">${esc(m.website || "")}</div></td><td>${esc(m.contact_name)}</td><td>${esc(m.email)}</td><td>${esc(m.phone)}</td><td class="small">${esc(m.notes)}</td>
      <td><select data-meeting="${m.id}">${["booked", "completed", "no_show", "cancelled"].map((s) => `<option ${s === m.status ? "selected" : ""}>${s}</option>`).join("")}</select></td></tr>`).join("")}</table>
    ${list.length ? "" : `<p class="muted">Meetings the agent books show up here.</p>`}</div>`;
  $$("[data-meeting]").forEach((s) => s.onchange = () => api(`/api/meetings/${s.dataset.meeting}`, { method: "PATCH", body: { status: s.value } }).then(() => toast("Saved")));
};

// --------------------------------------------------------------------- dnc
Views.dnc = async () => {
  const data = await api("/api/dnc?limit=500");
  main.innerHTML = `<h1>Do-not-call list</h1>
    <div class="card row"><input id="dnc-phone" placeholder="Phone number"><input id="dnc-reason" placeholder="Reason"><button class="primary" id="dnc-add">Add</button>
      <div class="spacer"></div><input type="file" id="dnc-file" accept=".txt,.csv"><button id="dnc-import">Import registry file</button></div>
    <div class="card"><p class="muted">${data.total} number(s). Numbers here are never dialed. Opt-outs from calls are added automatically.</p>
    <table><tr><th>Phone</th><th>Source</th><th>Reason</th><th>Added</th><th></th></tr>
    ${data.items.map((d) => `<tr><td>${esc(d.phone)}</td><td>${esc(d.source)}</td><td>${esc(d.reason)}</td><td>${fmtTime(d.added_at)}</td><td><button data-rm="${esc(d.phone)}">Remove</button></td></tr>`).join("")}</table></div>`;
  $("#dnc-add").onclick = () => api("/api/dnc", { method: "POST", body: { phone: $("#dnc-phone").value, reason: $("#dnc-reason").value } }).then(Views.dnc).catch((e) => toast(e.message, true));
  $("#dnc-import").onclick = async () => {
    const f = $("#dnc-file").files[0]; if (!f) return toast("Choose a file", true);
    const r = await api("/api/dnc/import", { method: "POST", raw: await f.text() });
    toast(`Parsed ${r.parsed} numbers`); Views.dnc();
  };
  $$("[data-rm]").forEach((b) => b.onclick = async () => {
    if (!confirm("Remove from do-not-call? Only do this if the person explicitly asked to be called again.")) return;
    await api(`/api/dnc/${encodeURIComponent(b.dataset.rm)}`, { method: "DELETE" }); Views.dnc();
  });
};

// ------------------------------------------------------------------- agent
Views.agent = async () => {
  const s = await api("/api/settings/agent");
  const days = (name) => DAYS.map((d, i) => `<label style="display:inline-block;margin-right:10px"><input type="checkbox" name="${name}" value="${i}" ${s[name].includes(i) ? "checked" : ""}> ${d}</label>`).join("");
  main.innerHTML = `<h1>Agent settings</h1><form id="af">
    <div class="card"><h2>Voice</h2><div class="form-grid">
      <div><label>Voice</label><select name="voice">${VOICES.map((v) => `<option ${v === s.voice ? "selected" : ""}>${v}</option>`).join("")}</select><div class="muted small">marin and cedar sound the most natural.</div></div>
      <div><label>Speaking speed (0.8 – 1.2)</label><input name="speech_speed" type="number" step="0.05" min="0.8" max="1.2" value="${s.speech_speed}"></div>
      <div><label>Turn-taking eagerness</label><select name="turn_eagerness">${["auto", "low", "medium", "high"].map((v) => `<option ${v === s.turn_eagerness ? "selected" : ""}>${v}</option>`).join("")}</select><div class="muted small">Low = waits longer before replying (fewer interruptions).</div></div>
      <div><label>Speak first after N seconds of silence</label><input name="silence_kickoff_seconds" type="number" step="0.5" min="1" max="10" value="${s.silence_kickoff_seconds}"></div>
    </div></div>
    <div class="card"><h2>Calling rules (prospect's local time)</h2><div class="form-grid">
      <div><label>Window start</label><input name="call_window_start" value="${s.call_window_start}"></div>
      <div><label>Window end (max 20:00)</label><input name="call_window_end" value="${s.call_window_end}"></div>
      <div><label>Max calls per number per 24h</label><input name="max_calls_per_phone_per_day" type="number" min="1" max="3" value="${s.max_calls_per_phone_per_day}"></div>
      <div><label>Seconds between dials</label><input name="dial_interval_seconds" type="number" min="3" value="${s.dial_interval_seconds}"></div>
      <div><label>Max call length (minutes)</label><input name="max_call_minutes" type="number" min="2" max="30" value="${s.max_call_minutes}"></div>
      <div><label>On voicemail</label><select name="voicemail_action"><option value="hangup" ${s.voicemail_action === "hangup" ? "selected" : ""}>Hang up</option><option value="leave_message" ${s.voicemail_action === "leave_message" ? "selected" : ""}>Leave a short message</option></select></div>
    </div>
    <label>Calling days</label><div>${days("call_days")}</div>
    <label><input type="checkbox" name="skip_us_holidays" ${s.skip_us_holidays ? "checked" : ""}> Skip major US holidays</label>
    <label><input type="checkbox" name="require_consent" ${s.require_consent ? "checked" : ""}> Only call leads with recorded prior express consent (strongly recommended: AI voices count as "artificial voice" under the TCPA)</label>
    </div>
    <div class="card"><h2>Phones</h2><div class="form-grid">
      <div><label>Transfer hot leads to (your phone)</label><input name="transfer_number" value="${esc(s.transfer_number)}"></div>
      <div><label>Test phone number (your phone)</label><input name="test_phone_number" value="${esc(s.test_phone_number)}"></div>
    </div></div>
    <div class="card"><h2>Meeting calendar (your time zone)</h2><div class="form-grid">
      <div><label>Time zone</label><input name="meeting_timezone" value="${esc(s.meeting_timezone)}"></div>
      <div><label>Hours start</label><input name="meeting_hours_start" value="${s.meeting_hours_start}"></div>
      <div><label>Hours end</label><input name="meeting_hours_end" value="${s.meeting_hours_end}"></div>
      <div><label>Minimum notice (hours)</label><input name="meeting_min_notice_hours" type="number" min="0" max="72" value="${s.meeting_min_notice_hours}"></div>
      <div><label>Booking webhook URL (Zapier, Make, CRM…)</label><input name="booking_webhook_url" value="${esc(s.booking_webhook_url)}"></div>
    </div><label>Meeting days</label><div>${days("meeting_days")}</div></div>
    <button class="primary">Save settings</button></form>`;
  $("#af").onsubmit = async (e) => {
    e.preventDefault();
    const f = e.target, body = {};
    for (const el of f.elements) {
      if (!el.name || el.name.endsWith("_days")) continue;
      body[el.name] = el.type === "checkbox" ? el.checked : el.type === "number" ? Number(el.value) : el.value.trim();
    }
    body.call_days = $$('input[name="call_days"]:checked', f).map((x) => Number(x.value));
    body.meeting_days = $$('input[name="meeting_days"]:checked', f).map((x) => Number(x.value));
    try { await api("/api/settings/agent", { method: "PUT", body }); toast("Settings saved"); }
    catch (err) { toast(err.message, true); }
  };
};

// ---------------------------------------------------------------- playbook
const PB_TEXT = [
  ["agent_name", "Agent's first name"], ["company_name", "Company name"], ["company_website", "Company website"], ["company_location", "Company location"],
  ["callback_number", "Callback number (said on voicemails)"], ["meeting_goal", "What the agent books"], ["meeting_length_minutes", "Meeting length (minutes)"],
];
const PB_AREA = [
  ["disclosure", "AI + recording disclosure said in every opening (required)"], ["lead_source_explanation", "Answer to 'how did you get my number?'"],
  ["offer_summary", "Offer summary"], ["ideal_customer", "Ideal customer"], ["pricing_guidance", "Pricing guidance"], ["meeting_value", "Why the meeting is worth their time"],
];
const PB_LISTS = [
  ["services", "Services"], ["differentiators", "Why us"], ["proof_points", "Real proof points / case studies (the agent will never invent any)"],
  ["discovery_questions", "Discovery questions"], ["extra_rules", "Extra rules from the sales manager"],
];

Views.playbook = async () => {
  const pb = await api("/api/settings/playbook");
  const listEditor = (key, items) => `<div class="list-editor" data-list="${key}">${items.map((v) => `<div class="item"><input value="${esc(v)}"><button type="button" class="rm">✕</button></div>`).join("")}<button type="button" class="add">+ Add</button></div>`;
  main.innerHTML = `<div class="row"><h1>Sales playbook</h1><div class="spacer"></div><button id="pb-preview">Preview full prompt</button><button id="pb-reset" class="danger">Reset to default</button></div>
    <p class="muted">This is how the agent is trained: who it is, what it sells, how it handles objections. Changes apply to the next call.</p>
    <form id="pbf">
    <div class="card"><div class="form-grid">${PB_TEXT.map(([k, l]) => `<div><label>${l}</label><input name="${k}" value="${esc(pb[k])}"></div>`).join("")}</div>
    ${PB_AREA.map(([k, l]) => `<label>${l}</label><textarea name="${k}">${esc(pb[k])}</textarea>`).join("")}</div>
    ${PB_LISTS.map(([k, l]) => `<div class="card"><h2>${l}</h2>${listEditor(k, pb[k])}</div>`).join("")}
    <div class="card"><h2>Objection handling</h2><div id="objections">${pb.objections.map((o) => objectionRow(o)).join("")}</div><button type="button" id="obj-add">+ Add objection</button></div>
    <button class="primary">Save playbook</button></form>`;
  $$(".list-editor").forEach((ed) => {
    ed.addEventListener("click", (e) => {
      if (e.target.classList.contains("rm")) e.target.parentElement.remove();
      if (e.target.classList.contains("add")) e.target.insertAdjacentHTML("beforebegin", `<div class="item"><input><button type="button" class="rm">✕</button></div>`);
    });
  });
  $("#objections").addEventListener("click", (e) => { if (e.target.classList.contains("rm")) e.target.closest(".obj").remove(); });
  $("#obj-add").onclick = () => $("#objections").insertAdjacentHTML("beforeend", objectionRow({ objection: "", response: "" }));
  $("#pb-reset").onclick = async () => { if (confirm("Reset the whole playbook to the default?")) { await api("/api/settings/playbook/reset", { method: "POST" }); Views.playbook(); } };
  $("#pb-preview").onclick = async () => {
    const r = await api("/api/prompt/preview");
    openModal(`<div class="row"><h2>Agent instructions (${r.characters} chars)</h2><div class="spacer"></div><button id="close-modal">Close</button></div><pre style="white-space:pre-wrap;max-height:70vh;overflow:auto">${esc(r.instructions)}</pre>`);
    $("#close-modal").onclick = closeModal;
  };
  $("#pbf").onsubmit = async (e) => {
    e.preventDefault();
    const body = {};
    PB_TEXT.concat(PB_AREA).forEach(([k]) => (body[k] = e.target.elements[k].value));
    body.meeting_length_minutes = Number(body.meeting_length_minutes) || 20;
    PB_LISTS.forEach(([k]) => (body[k] = $$(`[data-list="${k}"] input`).map((i) => i.value.trim()).filter(Boolean)));
    body.objections = $$(".obj").map((o) => ({ objection: $(".o", o).value.trim(), response: $(".r", o).value.trim() })).filter((o) => o.objection && o.response);
    try { await api("/api/settings/playbook", { method: "PUT", body }); toast("Playbook saved"); }
    catch (err) { toast(err.message, true); }
  };
};

function objectionRow(o) {
  return `<div class="obj card" style="background:var(--panel-2)"><div class="row"><input class="o" style="flex:1" placeholder="Objection" value="${esc(o.objection)}"><button type="button" class="rm">✕</button></div>
    <textarea class="r" placeholder="How to respond">${esc(o.response)}</textarea></div>`;
}

// ---------------------------------------------------------------- practice
function resampleFloat32(input, fromRate, toRate) {
  if (fromRate === toRate) return input;
  const outLen = Math.max(1, Math.round(input.length * toRate / fromRate));
  const out = new Float32Array(outLen);
  const ratio = fromRate / toRate;
  for (let i = 0; i < outLen; i++) {
    const x = i * ratio;
    const i0 = Math.floor(x);
    const i1 = Math.min(i0 + 1, input.length - 1);
    const t = x - i0;
    out[i] = input[i0] * (1 - t) + input[i1] * t;
  }
  return out;
}

function floatToPcm16B64(f32) {
  const pcm = new Int16Array(f32.length);
  for (let i = 0; i < f32.length; i++) pcm[i] = Math.max(-1, Math.min(1, f32[i])) * 0x7fff;
  const bytes = new Uint8Array(pcm.buffer);
  let bin = "";
  const chunk = 0x8000;
  for (let i = 0; i < bytes.length; i += chunk) {
    bin += String.fromCharCode.apply(null, bytes.subarray(i, i + chunk));
  }
  return btoa(bin);
}

let httpsPort = 0;

function micBlockedReason() {
  if (navigator.mediaDevices?.getUserMedia && window.isSecureContext) return "";
  if (!window.isSecureContext) {
    return "This browser only allows the microphone on HTTPS pages. Open the secure link below (accept the certificate warning once).";
  }
  return "This browser does not support microphone capture. Try Chrome or Safari, or use the Android app's Talk tab.";
}

function secureUrl() {
  if (!httpsPort) return "";
  return `https://${location.hostname}:${httpsPort}/#token=${encodeURIComponent(token)}`;
}

const Practice = {
  ws: null, ctx: null, stream: null, node: null, src: null, sources: [], playhead: 0, leadId: null, micLevel: 0,

  async start() {
    const blocked = micBlockedReason();
    if (blocked) throw new Error(blocked);
    const p = new URLSearchParams({ token });
    if (this.leadId) p.set("lead_id", this.leadId);
    const proto = location.protocol === "https:" ? "wss" : "ws";

    // Create + resume AudioContext in the click gesture BEFORE awaiting getUserMedia,
    // otherwise Chrome leaves it suspended and neither mic nor speaker work.
    const Ctx = window.AudioContext || window.webkitAudioContext;
    try {
      this.ctx = new Ctx({ sampleRate: 24000 });
    } catch {
      this.ctx = new Ctx();
    }
    if (this.ctx.state === "suspended") await this.ctx.resume();
    const captureRate = this.ctx.sampleRate;

    try {
      this.stream = await navigator.mediaDevices.getUserMedia({
        audio: { echoCancellation: true, noiseSuppression: true, autoGainControl: true, channelCount: 1 },
      });
    } catch (err) {
      if (err.name === "NotAllowedError") throw new Error("Microphone permission was denied. Allow it in the browser's site settings and try again.");
      if (err.name === "NotFoundError") throw new Error("No microphone found on this device.");
      throw err;
    }
    if (this.ctx.state === "suspended") await this.ctx.resume();

    const worklet = `class Cap extends AudioWorkletProcessor{constructor(){super();this.buf=[];this.n=0}process(i){const c=i[0][0];if(c){this.buf.push(new Float32Array(c));this.n+=c.length;if(this.n>=960){const o=new Float32Array(this.n);let k=0;for(const b of this.buf){o.set(b,k);k+=b.length}this.port.postMessage(o,[o.buffer]);this.buf=[];this.n=0}}return true}}registerProcessor("cap",Cap)`;
    await this.ctx.audioWorklet.addModule(URL.createObjectURL(new Blob([worklet], { type: "application/javascript" })));
    this.src = this.ctx.createMediaStreamSource(this.stream);
    this.node = new AudioWorkletNode(this.ctx, "cap");
    // Keep the graph alive; mute local monitor so you don't hear yourself.
    const mute = this.ctx.createGain();
    mute.gain.value = 0;
    this.src.connect(this.node);
    this.node.connect(mute);
    mute.connect(this.ctx.destination);

    this.ws = new WebSocket(`${proto}://${location.host}/ws/practice?${p}`);
    this.node.port.onmessage = (e) => {
      if (this.ws?.readyState !== 1) return;
      const f = e.data;
      let sum = 0;
      for (let i = 0; i < f.length; i++) sum += f[i] * f[i];
      this.micLevel = Math.sqrt(sum / Math.max(1, f.length));
      const orb = $(".practice-orb");
      if (orb) orb.classList.toggle("listening", this.micLevel > 0.01);
      const pcm24 = resampleFloat32(f, captureRate, 24000);
      this.ws.send(JSON.stringify({ type: "audio", audio: floatToPcm16B64(pcm24) }));
    };
    this.ws.onmessage = (e) => this.onMessage(JSON.parse(e.data));
    this.ws.onclose = () => { this.cleanup(); renderPracticeState(); };
    this.ws.onerror = () => toast("Practice WebSocket failed", true);
    $("#pr-status").textContent = `Mic ready (${captureRate} Hz → 24 kHz). Waiting for server…`;
  },

  onMessage(m) {
    if (m.type === "audio") this.play(m.audio);
    else if (m.type === "clear") this.clear();
    else if (m.type === "mark") {
      const wait = Math.max(0, (this.playhead - this.ctx.currentTime) * 1000);
      setTimeout(() => this.ws?.readyState === 1 && this.ws.send(JSON.stringify({ type: "mark", name: m.name })), wait);
    } else if (m.type === "event" && m.event) {
      if (m.event.type === "transcript") {
        addPracticeLine(m.event.role, m.event.text);
        if (m.event.role === "prospect") $("#pr-status").textContent = "Heard you — thinking…";
        if (m.event.role === "agent") $("#pr-status").textContent = "Agent speaking…";
      } else if (m.event.type === "tool") {
        addPracticeLine("tool", `${m.event.name} → ${JSON.stringify(m.event.result)}`);
      } else if (m.event.type === "error") {
        toast(m.event.message || "Practice error", true);
        $("#pr-status").textContent = m.event.message || "Error";
      }
    } else if (m.type === "ready") {
      $("#pr-status").textContent = "Live. Say \"Hello?\" clearly, then pause — use headphones.";
    } else if (m.type === "error") {
      toast(m.message, true);
      $("#pr-status").textContent = m.message || "Error";
    } else if (m.type === "hangup") {
      $("#pr-status").textContent = "The agent ended the call.";
    }
  },

  async play(b64) {
    if (!this.ctx) return;
    if (this.ctx.state === "suspended") await this.ctx.resume();
    const bin = atob(b64), n = bin.length / 2, f = new Float32Array(n);
    for (let i = 0; i < n; i++) {
      let v = bin.charCodeAt(2 * i) | (bin.charCodeAt(2 * i + 1) << 8);
      if (v >= 0x8000) v -= 0x10000;
      f[i] = v / 0x8000;
    }
    // Always create the buffer at 24 kHz; the browser resamples to ctx.sampleRate.
    const buf = this.ctx.createBuffer(1, n, 24000);
    buf.copyToChannel(f, 0);
    const s = this.ctx.createBufferSource();
    s.buffer = buf;
    s.connect(this.ctx.destination);
    this.playhead = Math.max(this.playhead, this.ctx.currentTime + 0.05);
    s.start(this.playhead);
    this.playhead += buf.duration;
    this.sources.push(s);
    s.onended = () => {
      this.sources = this.sources.filter((x) => x !== s);
      $(".practice-orb")?.classList.toggle("talking", this.sources.length > 0);
    };
    $(".practice-orb")?.classList.add("talking");
  },

  clear() { this.sources.forEach((s) => { try { s.stop(); } catch {} }); this.sources = []; this.playhead = 0; },

  stop() { if (this.ws) { try { this.ws.send(JSON.stringify({ type: "stop" })); } catch {} this.ws.close(); } this.cleanup(); },

  cleanup() {
    this.clear();
    this.stream?.getTracks().forEach((t) => t.stop());
    this.ctx?.close().catch(() => {});
    this.ws = this.ctx = this.stream = this.node = this.src = null;
    this.micLevel = 0;
  },
};

function addPracticeLine(role, text) {
  const box = $("#pr-transcript"); if (!box) return;
  box.insertAdjacentHTML("beforeend", `<div class="msg ${esc(role)}">${role === "tool" ? "" : `<div class="who">${role === "agent" ? "Agent" : "You"}</div>`}${esc(text)}</div>`);
  box.scrollTop = box.scrollHeight;
}

function renderPracticeState() {
  const b = $("#pr-toggle"); if (!b) return;
  b.textContent = Practice.ws ? "End practice call" : "Start practice call";
  b.className = Practice.ws ? "danger" : "primary";
}

Views.practice = async () => {
  const [leadPage, st] = await Promise.all([api("/api/leads?limit=200"), api("/api/status")]);
  const leads = leadPage.items;
  httpsPort = st.https_port || 0;
  const blocked = micBlockedReason();
  const link = secureUrl();
  const micWarn = blocked
    ? `<div class="card warn"><p>${esc(blocked)}</p>${!window.isSecureContext && link
      ? `<p><a href="${esc(link)}">${esc(link.split("#")[0])}</a></p>`
      : !window.isSecureContext ? `<p class="muted">Start the server with HTTPS enabled (HTTPS_PORT in .env).</p>` : ""}</div>`
    : "";
  main.innerHTML = `<h1>Practice mode</h1>${micWarn}
    <div class="card"><p class="muted">Rehearse with the agent through this device's microphone (use headphones). You play the business owner; the agent runs the exact same playbook it uses on real calls. Nothing is booked or saved to leads.</p>
    <div class="row"><label style="margin:0">Pretend to be:</label><select id="pr-lead"><option value="">A generic small business</option>${leads.map((l) => `<option value="${l.id}" ${Practice.leadId == l.id ? "selected" : ""}>${esc(l.business_name || l.phone)}</option>`).join("")}</select>
    <div class="spacer"></div><button id="pr-toggle" class="primary">Start practice call</button></div>
    <div class="practice-orb"></div><p id="pr-status" class="muted" style="text-align:center"></p></div>
    <div class="card"><h2>Live transcript</h2><div id="pr-transcript" class="transcript"></div></div>`;
  $("#pr-lead").onchange = (e) => (Practice.leadId = e.target.value || null);
  $("#pr-toggle").onclick = async () => {
    if (Practice.ws) { Practice.stop(); renderPracticeState(); return; }
    $("#pr-transcript").innerHTML = "";
    $("#pr-status").textContent = "Connecting…";
    try { await Practice.start(); } catch (err) { toast(err.message, true); Practice.cleanup(); }
    renderPracticeState();
  };
  renderPracticeState();
};

// -------------------------------------------------------------------- boot
(async () => {
  const hashToken = new URLSearchParams(location.hash.slice(1)).get("token");
  if (hashToken) {
    token = hashToken;
    localStorage.setItem("agentvoice_token", token);
    history.replaceState(null, "", location.pathname);
  }
  if (!token) return showLogin();
  try {
    await api("/api/status");
    $("#app").classList.remove("hidden");
    show("overview");
  } catch { showLogin(); }
})();
