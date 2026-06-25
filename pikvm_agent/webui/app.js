// Minimal PiKVM Agent console — polls the daemon, renders the live frame, the
// event feed, and the approval queue, and drives continue/approve/reject/abort.
"use strict";

let current = null;       // selected session id
let pendingApproval = null;
let timer = null;

const $ = (id) => document.getElementById(id);

async function jget(path) { const r = await fetch(path); return r.ok ? r.json() : null; }
async function jpost(path, body) {
  const r = await fetch(path, { method: "POST", headers: { "content-type": "application/json" },
                                body: JSON.stringify(body || {}) });
  return r.ok ? r.json() : null;
}

async function loadSessions() {
  const rows = (await jget("/sessions")) || [];
  const box = $("sessions");
  box.innerHTML = "";
  for (const s of rows) {
    const el = document.createElement("div");
    el.className = "session" + (s.id === current ? " active" : "");
    el.innerHTML = `<div class="task">${escape(s.task)}</div><div class="id">${s.id} · ${s.live_status || s.status}</div>`;
    el.onclick = () => select(s.id);
    box.appendChild(el);
  }
}

function select(id) {
  current = id;
  $("memory").classList.remove("show");
  loadSessions();
  refresh();
}

function setBadge(status) {
  const b = $("status");
  b.textContent = status || "—";
  b.className = "badge b-" + (status || "done");
}

function escape(s) { const d = document.createElement("div"); d.textContent = s == null ? "" : String(s); return d.innerHTML; }

async function refresh() {
  if (!current) return;
  const obs = await jget(`/sessions/${current}`);
  if (!obs) return;
  setBadge(obs.status);
  $("meta").textContent = `frame ${obs.frame_id ?? "?"} · world ${obs.world_version ?? "?"}` +
                          (obs.error ? ` · ${obs.error}` : "");
  $("frame").src = `/sessions/${current}/frame?t=${Date.now()}`;

  const trace = (await jget(`/sessions/${current}/trace`)) || [];
  $("events").innerHTML = trace.slice(-40).reverse().map((e) =>
    `<div class="ev"><span class="k">${escape(e.kind)}</span> ${escape(summarize(e))}</div>`).join("");

  const approvals = (await jget(`/sessions/${current}/approvals`)) || [];
  pendingApproval = approvals[0] || null;
  const panel = $("approval");
  if (pendingApproval) {
    const req = pendingApproval.request || {};
    panel.querySelector(".reason").textContent =
      `⚠ ${req.risk || "approval"}: ${req.reason || "human approval required"}`;
    panel.classList.add("show");
  } else {
    panel.classList.remove("show");
  }
}

function summarize(e) {
  const parts = [];
  for (const k of ["intent", "reason", "status", "mode", "risk", "actions", "from_status"]) {
    if (e[k] != null) parts.push(`${k}=${Array.isArray(e[k]) ? e[k].join(",") : e[k]}`);
  }
  return parts.join("  ");
}

$("continue").onclick = async () => { if (current) { await jpost(`/sessions/${current}/continue`); refresh(); } };
$("abort").onclick = async () => { if (current) { await jpost(`/sessions/${current}/abort`, { reason: "stopped from console" }); refresh(); } };
$("approve").onclick = async () => {
  if (current && pendingApproval)
    await jpost(`/sessions/${current}/approvals/${pendingApproval.id}`, { type: "approve" });
  refresh();
};
$("reject").onclick = async () => {
  if (current && pendingApproval)
    await jpost(`/sessions/${current}/approvals/${pendingApproval.id}`, { type: "reject", reason: "rejected from console" });
  refresh();
};
$("export").onclick = async () => {
  if (!current) return;
  const mu = await jget(`/sessions/${current}/memory-update`);
  const pre = $("memory");
  pre.textContent = mu ? (mu.markdown || JSON.stringify(mu, null, 2)) : "(no export)";
  pre.classList.add("show");
};

loadSessions();
timer = setInterval(() => { loadSessions(); refresh(); }, 1500);
