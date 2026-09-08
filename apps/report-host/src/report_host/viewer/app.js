// Report viewer + chat sidebar. Plain JS, no build step.
const slug = location.pathname.split("/")[2];
const api = (p, opts) => fetch(`/api/${slug}${p}`, opts).then(async (r) => {
  if (!r.ok) throw new Error((await r.json().catch(() => ({}))).detail || r.statusText);
  return r.json();
});
const $ = (id) => document.getElementById(id);
let state = null, thread = null, lastId = 0, pollTimer = null, shownPdf = null, seenVersions = new Set();

// --- pdf ----------------------------------------------------------------
async function showPdf(name) {
  if (!window.pdfjsLib) return window.addEventListener("pdfjs-ready", () => showPdf(name), { once: true });
  if (name === shownPdf) return;
  shownPdf = name;
  const pages = $("pages");
  pages.innerHTML = "";
  const doc = await pdfjsLib.getDocument(`/files/${slug}/${name}`).promise;
  const scale = Math.min(1.6, (pages.clientWidth - 60) / 595);
  for (let i = 1; i <= doc.numPages; i++) {
    const page = await doc.getPage(i);
    const vp = page.getViewport({ scale: scale * window.devicePixelRatio });
    const canvas = document.createElement("canvas");
    canvas.width = vp.width; canvas.height = vp.height;
    canvas.style.width = `${vp.width / window.devicePixelRatio}px`;
    pages.appendChild(canvas);
    await page.render({ canvasContext: canvas.getContext("2d"), viewport: vp }).promise;
  }
  renderVersions();
}
function renderVersions() {
  const box = $("versions");
  box.innerHTML = "";
  for (const v of state.revisions) {
    const b = document.createElement("button");
    b.textContent = v.note === "published" ? "original" : `revision ${v.name.replace(/\D/g, "")}`;
    b.title = `${v.name} · ${new Date(v.created_at).toLocaleString()}`;
    if (v.name === shownPdf) b.classList.add("current");
    else if (!seenVersions.has(v.name)) b.classList.add("new");
    b.onclick = () => { seenVersions.add(v.name); showPdf(v.name); };
    box.appendChild(b);
  }
}

// --- state --------------------------------------------------------------
function applyState(s) {
  const prevCurrent = state?.current_pdf;
  state = s;
  $("title").textContent = s.title;
  if (s.recipient_name) $("reader").textContent = `Reading as ${s.recipient_name}`;
  const b = s.budget, frac = b.cap_usd ? Math.min(1, b.spent_usd / b.cap_usd) : 1;
  $("meter").innerHTML = `Question budget · $${b.spent_usd.toFixed(2)} of $${b.cap_usd.toFixed(2)} · up to $${b.reserve_usd.toFixed(2)} per answer` +
    `<div class="bar"><div class="fill ${frac >= 1 ? "over" : frac >= 0.75 ? "warn" : ""}" style="width:${(frac * 100).toFixed(0)}%"></div></div>`;
  const banner = $("banner");
  if (s.seam_status === "unauthorized") {
    banner.textContent = "This report needs to be re-published before it can answer new questions.";
    banner.hidden = false;
  } else banner.hidden = true;
  if (s.threads) renderThreads(s.threads);
  if (shownPdf === null) { for (const v of s.revisions) seenVersions.add(v.name); showPdf(s.current_pdf); }
  else if (s.current_pdf !== prevCurrent && s.current_pdf !== shownPdf) {
    // daimon uploaded a revision: swap the viewer to it, keep the old one a click away.
    showPdf(s.current_pdf);
    setStatus(`daimon revised the report · now showing revision ${s.current_pdf.replace(/\D/g, "")}`);
  } else renderVersions();
}
function renderThreads(threads) {
  const box = $("threads");
  box.innerHTML = "";
  const nb = document.createElement("button");
  nb.textContent = "+ new thread";
  nb.onclick = () => selectThread(null);
  if (thread === null) nb.classList.add("active");
  box.appendChild(nb);
  for (const t of threads) {
    const b = document.createElement("button");
    b.textContent = t.title;
    b.className = (t.id === thread ? "active " : "") + (t.status === "running" ? "running" : "");
    b.onclick = () => selectThread(t.id);
    box.appendChild(b);
  }
}
function setStatus(html, busy = false) { $("status").innerHTML = (busy ? '<span class="spin"></span>' : "") + html; }

// --- threads ------------------------------------------------------------
async function selectThread(id) {
  thread = id; lastId = 0;
  $("messages").innerHTML = id ? "" : '<div class="empty">Ask why any number is what it is. daimon answers from the analysis files behind this report.</div>';
  setStatus("");
  $("stop").hidden = true;
  clearInterval(pollTimer); pollTimer = null;
  const s = await api("/state"); applyState(s);
  if (id) await poll(true);
}
function addMessage(m) {
  const box = $("messages");
  box.querySelector(".empty")?.remove();
  const div = document.createElement("div");
  div.className = `msg ${m.role}`;
  div.innerHTML = m.role === "assistant" ? renderMd(m.text) : m.text.replace(/</g, "&lt;");
  box.appendChild(div);
  box.scrollTop = box.scrollHeight;
}
function renderMd(t) {
  // ponytail: enough markdown for daimon's answers (bold, code, tables, bullets); swap for marked if it grows.
  let h = t.replace(/</g, "&lt;");
  h = h.replace(/```[\s\S]*?```/g, (m) => `<pre><code>${m.slice(3, -3)}</code></pre>`);
  h = h.replace(/`([^`]+)`/g, "<code>$1</code>").replace(/\*\*([^*]+)\*\*/g, "<b>$1</b>");
  h = h.replace(/((?:^\|.*\|\s*$\n?)+)/gm, (block) => {
    const rows = block.trim().split("\n").filter((r) => !/^\|\s*-/.test(r));
    return "<table>" + rows.map((r, i) => "<tr>" + r.split("|").slice(1, -1).map((c) => `<${i ? "td" : "th"}>${c.trim()}</${i ? "td" : "th"}>`).join("") + "</tr>").join("") + "</table>";
  });
  return h.replace(/^\s*[-*] (.*)$/gm, "• $1");
}
async function poll(first = false) {
  if (!thread) return;
  const t = await api(`/threads/${thread}?after=${lastId}`);
  for (const m of t.messages) { addMessage(m); lastId = m.id; }
  const s = await api("/state"); applyState(s);
  if (t.status === "running") {
    const secs = Math.round(t.elapsed_seconds || 0);
    setStatus(`daimon is working · ${Math.floor(secs / 60)}:${String(secs % 60).padStart(2, "0")} elapsed${secs > 90 ? " · long answers can take several minutes; you can keep reading or come back later" : ""}`, true);
    $("send").disabled = true;
    $("stop").hidden = false;
    if (!pollTimer) pollTimer = setInterval(poll, 2000);
  } else {
    if (!first || t.messages.length) setStatus(pollTimer ? "answered" : "");
    if (pollTimer && !document.body.classList.contains("dock-open") && !$("dock-open").querySelector(".badge")) $("dock-open").insertAdjacentHTML("beforeend", '<span class="badge"></span>');
    $("send").disabled = false;
    $("stop").hidden = true;
    clearInterval(pollTimer); pollTimer = null;
  }
}
$("composer").onsubmit = async (e) => {
  e.preventDefault();
  const message = $("input").value.trim();
  if (!message) return;
  $("input").value = "";
  try {
    const r = await api("/ask", { method: "POST", headers: { "content-type": "application/json" }, body: JSON.stringify({ message, thread }) });
    if (!thread) { thread = r.thread; lastId = 0; $("messages").innerHTML = ""; }
    await poll();
  } catch (err) { setStatus(err.message); }
};
$("stop").onclick = async () => {
  if (!thread) return;
  try { await api(`/threads/${thread}/cancel`, { method: "POST" }); }
  catch (err) { setStatus(err.message); }
};
// Mobile: the sidebar is a sheet. Open on demand, close back to the report; a running
// thread keeps polling while closed and the open button shows a dot when it answers.
const openDock = () => { document.body.classList.add("dock-open"); $("dock-open").querySelector(".badge")?.remove(); };
const closeDock = () => document.body.classList.remove("dock-open");
$("dock-open").onclick = openDock;
$("dock-close").onclick = closeDock;
document.addEventListener("keydown", (e) => { if (e.key === "Escape") closeDock(); });
$("input").addEventListener("keydown", (e) => { if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); $("composer").requestSubmit(); } });

api("/state").then((s) => { applyState(s); const running = s.threads.find((t) => t.status === "running"); if (running) selectThread(running.id); }).catch((e) => setStatus(e.message));
