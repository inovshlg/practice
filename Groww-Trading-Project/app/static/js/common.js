/* Shared helpers for every page. */
const $ = (s, el = document) => el.querySelector(s);
const $$ = (s, el = document) => Array.from(el.querySelectorAll(s));

const fmtINR = (v, dp = 0) => {
  if (v === null || v === undefined || Number.isNaN(v)) return "—";
  const s = Math.abs(v).toLocaleString("en-IN", { minimumFractionDigits: dp, maximumFractionDigits: dp });
  return (v < 0 ? "−₹" : "₹") + s;
};
const fmtNum = (v, dp = 2) => (v === null || v === undefined || Number.isNaN(v)) ? "—"
  : Number(v).toLocaleString("en-IN", { minimumFractionDigits: dp, maximumFractionDigits: dp });
const pnlClass = v => v > 0 ? "pos" : (v < 0 ? "neg" : "");
const esc = s => String(s ?? "").replace(/[&<>"']/g, c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const hhmm = iso => iso ? iso.slice(11, 16) : "—";
const fmtDate = iso => iso ? new Date(iso.slice(0, 10) + "T00:00:00").toLocaleDateString("en-IN", { day: "2-digit", month: "short", year: "2-digit" }) : "—";
// candle times are IST wall-clock sent "as UTC" so the chart axis shows IST
const isoToChartTs = iso => Math.floor(Date.parse(iso.slice(0, 19) + "Z") / 1000);

async function api(path, opts = {}) {
  const init = { headers: { "Content-Type": "application/json" }, ...opts };
  if (init.body && typeof init.body !== "string") init.body = JSON.stringify(init.body);
  let res;
  try { res = await fetch(path, init); }
  catch (e) { throw new Error("Network error — is the server running?"); }
  let data = null;
  try { data = await res.json(); } catch (e) { /* non-JSON */ }
  if (!res.ok) throw new Error((data && data.error) || `HTTP ${res.status}`);
  return data;
}

function toast(msg, isError = false, ms = 4500) {
  const el = document.createElement("div");
  el.className = "toast" + (isError ? " error" : "");
  el.textContent = msg;
  document.body.appendChild(el);
  setTimeout(() => el.remove(), ms);
}

function setSeg(seg, value) {
  $$("button", seg).forEach(b => b.classList.toggle("on", b.dataset.v === String(value)));
}
function bindSeg(seg, onChange) {
  seg.addEventListener("click", e => {
    const b = e.target.closest("button");
    if (!b) return;
    setSeg(seg, b.dataset.v);
    onChange(b.dataset.v);
  });
}

function stateHTML(kind, title, body, extra = "") {
  const icon = { empty: "∅", error: "⚠", loading: "" }[kind] || "";
  return `<div class="state">${kind === "loading" ? '<div class="spinner"></div>' : `<div class="big">${icon}</div>`}
    <h4>${esc(title)}</h4><div>${body || ""}</div>${extra}</div>`;
}

function isLight() { return document.documentElement.dataset.theme === "light"; }
function chartColors() {
  const cs = getComputedStyle(document.documentElement);
  const v = n => cs.getPropertyValue(n).trim();
  return { bg: v("--panel"), text: v("--muted"), grid: v("--border"), up: v("--green"), down: v("--red"),
           accent: v("--accent"), amber: v("--amber"), blue: v("--blue") };
}

(function initShell() {
  $("#themeBtn").addEventListener("click", () => {
    const next = isLight() ? "dark" : "light";
    document.documentElement.dataset.theme = next;
    try { localStorage.setItem("theme", next); } catch (e) {}
    window.dispatchEvent(new Event("themechange"));
  });
  api("/api/health").then(h => {
    const ok = h.db && h.keys_file;
    $("#healthDot").className = "dot " + (h.db ? (h.keys_file ? "ok" : "warn") : "bad");
    $("#healthText").textContent = !h.db ? "Database down" : (!h.keys_file ? "No keys.txt" : "Ready");
    $("#healthPill").title = `Postgres: ${h.db ? "connected" : "unavailable"} · keys.txt: ${h.keys_file ? "found" : "missing"} · server ${h.server_time_ist} IST`;
  }).catch(() => { $("#healthDot").className = "dot bad"; $("#healthText").textContent = "Server offline"; });
})();
