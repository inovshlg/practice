/* Live / paper engine page. */
(() => {
  const S = { exec: "PAPER", tf: 15, hist: "positions", histData: null, chart: null, series: {}, timer: null };
  const MODE_SHORT = { ITM5_BUY: "ITM5 buy", ATM_BUY: "ATM buy", ATM_SELL: "ATM sell" };

  bindSeg($("#segExec"), v => {
    S.exec = v;
    $("#confirmWrap").classList.toggle("hidden", v !== "LIVE");
    $("#startBtn").textContent = v === "LIVE" ? "▶ Start LIVE" : "▶ Start paper";
    $("#startBtn").className = "btn " + (v === "LIVE" ? "danger" : "primary");
  });
  bindSeg($("#segLTf"), v => { S.tf = +v; });
  bindSeg($("#segHist"), v => { S.hist = v; renderHist(); });

  const cfg = () => ({ exec_mode: S.exec, strategy_mode: $("#lMode").value, timeframe: S.tf,
    entry_start: $("#lStart").value, entry_cutoff: $("#lCut").value, lots: +$("#lLots").value,
    max_trades: +$("#lMaxT").value, max_loss: +$("#lMaxL").value, confirm: $("#lConfirm").value.trim() });

  $("#startBtn").addEventListener("click", async () => {
    const c = cfg();
    if (c.exec_mode === "LIVE" && !confirm(`Start LIVE trading with ${c.lots} lot(s) of ${MODE_SHORT[c.strategy_mode]}? Real orders will be placed in your Groww account.`)) return;
    $("#startBtn").disabled = true;
    try { render(await api("/api/live/start", { method: "POST", body: c })); toast("Engine started"); }
    catch (e) { toast(e.message, true); }
    finally { $("#startBtn").disabled = false; poll(); }
  });
  async function stop(flatten) {
    if (flatten && !confirm("Stop the engine and square off any open position now?")) return;
    try { render(await api("/api/live/stop", { method: "POST", body: { flatten } })); toast("Engine stopped"); loadHist(); }
    catch (e) { toast(e.message, true); }
  }
  $("#stopFlatBtn").addEventListener("click", () => stop(true));
  $("#stopKeepBtn").addEventListener("click", () => stop(false));

  // ------------------------------------------------------------------ chart
  function initChart() {
    if (!window.LightweightCharts) { $("#liveChart").innerHTML = stateHTML("error", "Chart library failed to load", ""); return; }
    const c = chartColors();
    S.chart = LightweightCharts.createChart($("#liveChart"), {
      layout: { background: { color: c.bg }, textColor: c.text }, autoSize: true,
      grid: { vertLines: { color: c.grid }, horzLines: { color: c.grid } },
      timeScale: { timeVisible: true, secondsVisible: false, borderColor: c.grid }, rightPriceScale: { borderColor: c.grid } });
    S.series.c = S.chart.addCandlestickSeries({ upColor: c.up, downColor: c.down, borderVisible: false, wickUpColor: c.up, wickDownColor: c.down });
    S.series.e9 = S.chart.addLineSeries({ color: c.amber, lineWidth: 2, priceLineVisible: false, lastValueVisible: false });
    S.series.e21 = S.chart.addLineSeries({ color: c.blue, lineWidth: 2, priceLineVisible: false, lastValueVisible: false });
  }
  window.addEventListener("themechange", () => {
    if (!S.chart) return;
    const c = chartColors();
    S.chart.applyOptions({ layout: { background: { color: c.bg }, textColor: c.text }, grid: { vertLines: { color: c.grid }, horzLines: { color: c.grid } } });
  });

  // ------------------------------------------------------------------ render
  function kpi(label, value, sub = "", cls = "") {
    return `<div class="kpi"><div class="label">${label}</div><div class="value ${cls}" style="font-size:19px">${value}</div><div class="sub">${sub}</div></div>`;
  }
  function render(st) {
    const status = st.status || "IDLE";
    const live = st.config && st.config.exec_mode === "LIVE";
    $("#engStatus").textContent = status + (st.config ? ` · ${st.config.exec_mode}` : "");
    $("#engDot").className = "dot " + ({ RUNNING: live ? "live" : "ok", HALTED: "bad", DONE: "warn", STOPPED: "", IDLE: "" }[status] || "");
    const running = status === "RUNNING" || (status === "HALTED" && st.position);
    $("#stopFlatBtn").disabled = $("#stopKeepBtn").disabled = !(running || st.position);
    $("#startBtn").disabled = status === "RUNNING";
    let banner = "";
    if (status === "HALTED") banner = `<div class="banner error"><b>HALTED.</b>&nbsp;${esc(st.halt_reason || "")}</div>`;
    else if (status === "RUNNING" && live) banner = `<div class="banner error"><b>LIVE trading is ON.</b>&nbsp;Real orders are being placed in your Groww account.</div>`;
    else if (st.last_error) banner = `<div class="banner warn">Last error: ${esc(st.last_error)}</div>`;
    $("#liveBanner").innerHTML = banner;

    const snap = st.snapshot || {}, pos = st.position, cfgd = st.config || {};
    const diff = (snap.ema9 != null && snap.ema21 != null) ? snap.ema9 - snap.ema21 : null;
    $("#liveKpis").innerHTML = [
      kpi("NIFTY", snap.spot ? fmtNum(snap.spot) : "—", snap.spot_time ? `as of ${hhmm(snap.spot_time)}` : (snap.note || "")),
      kpi("EMA 9 / 21", snap.ema9 != null ? `${fmtNum(snap.ema9, 1)}` : "—", snap.ema21 != null ? `21: ${fmtNum(snap.ema21, 1)} · ${diff > 0 ? "9 above" : "9 below"}` : "", diff == null ? "" : pnlClass(diff)),
      kpi("Position", pos ? `${pos.side} ${pos.strike} ${pos.opt_type}` : "Flat", pos ? `${pos.qty} qty @ ₹${fmtNum(pos.entry_price)}` : (cfgd.strategy_mode ? MODE_SHORT[cfgd.strategy_mode] : "")),
      kpi("Unrealised", pos && pos.unrealised != null ? fmtINR(pos.unrealised) : "—", pos ? `LTP ₹${fmtNum(pos.ltp)}` : "", pos ? pnlClass(pos.unrealised) : ""),
      kpi("Realised net", fmtINR(st.realised_net || 0), `${(st.closed || []).length} closed · ${st.entries || 0} entries`, pnlClass(st.realised_net)),
    ].join("");
    $("#lastTick").textContent = st.last_tick ? `last tick ${st.last_tick.slice(11)}` : "";
    $("#liveNote").textContent = snap.note || (cfgd.timeframe ? `${cfgd.timeframe}m bars · today` : "");

    if (S.chart && snap.bars) {
      S.series.c.setData(snap.bars.map(b => ({ time: b.time, open: b.open, high: b.high, low: b.low, close: b.close })));
      S.series.e9.setData(snap.bars.filter(b => b.ema9 != null).map(b => ({ time: b.time, value: b.ema9 })));
      S.series.e21.setData(snap.bars.filter(b => b.ema21 != null).map(b => ({ time: b.time, value: b.ema21 })));
      if (snap.bars.length !== S.nBars) { S.nBars = snap.bars.length; S.chart.timeScale().fitContent(); }
    }

    $("#posCard").innerHTML = pos ? `<dl class="kv">
      <dt>Contract</dt><dd>${esc(pos.label)}</dd><dt>Trading symbol</dt><dd>${esc(pos.trading_symbol)}</dd>
      <dt>Side / qty</dt><dd>${pos.side} ${pos.qty}</dd><dt>Entry</dt><dd>${hhmm(pos.entry_time)} @ ₹${fmtNum(pos.entry_price)}</dd>
      <dt>LTP</dt><dd>₹${fmtNum(pos.ltp)}</dd><dt>Unrealised (gross)</dt><dd class="${pnlClass(pos.unrealised)}">${fmtINR(pos.unrealised, 2)}</dd>
      <dt>Signal</dt><dd>${pos.direction > 0 ? "▲ bullish" : "▼ bearish"} @ ${hhmm(pos.signal_time)}</dd>
      ${pos.exit_failed ? '<dt class="neg">Exit</dt><dd class="neg">FAILED – square off manually</dd>' : ""}</dl>`
      : stateHTML("empty", "Flat", status === "RUNNING" ? "Waiting for the next 9/21 cross inside the window." : "Engine not running.");

    const ev = st.events || [];
    $("#events").innerHTML = ev.length ? ev.map(e => `<div class="event"><span class="mono faint">${hhmm(e.ts)}:${e.ts.slice(17, 19)}</span><span class="lvl ${e.level}">${e.level}</span><span>${esc(e.message)}</span></div>`).join("")
      : stateHTML("empty", "No events yet", "");

    const cl = st.closed || [];
    const sum = cl.reduce((a, t) => a + t.net_pnl, 0);
    $("#closedSum").innerHTML = cl.length ? `<span class="mono ${pnlClass(sum)}">${fmtINR(sum, 2)}</span>` : "";
    $("#closed").innerHTML = cl.length ? tradeTable(cl) : stateHTML("empty", "No closed trades", "");
  }

  function tradeTable(rows) {
    return `<table class="table"><thead><tr><th>Contract</th><th>Side</th><th>Entry</th><th>Exit</th><th>Reason</th><th>Gross</th><th>Friction</th><th>Net</th></tr></thead><tbody>${
      rows.map(t => `<tr><td>${esc(t.label)}</td><td>${t.side}</td><td>${hhmm(t.entry_time)} ₹${fmtNum(t.entry_price ?? t.entry_raw)}</td>
        <td>${hhmm(t.exit_time)} ₹${fmtNum(t.exit_price ?? t.exit_raw)}</td><td style="font-family:var(--sans)">${esc(t.exit_reason)}</td>
        <td class="${pnlClass(t.gross_pnl)}">${fmtINR(t.gross_pnl, 2)}</td><td>${fmtINR(t.friction, 2)}</td><td class="${pnlClass(t.net_pnl)}"><b>${fmtINR(t.net_pnl, 2)}</b></td></tr>`).join("")
    }</tbody></table>`;
  }

  // ------------------------------------------------------------------ history
  async function loadHist() {
    try { S.histData = await api("/api/live/history"); renderHist(); }
    catch (e) { $("#hist").innerHTML = `<div class="banner error">${esc(e.message)}</div>`; }
  }
  function renderHist() {
    const d = S.histData;
    if (!d) return;
    if (S.hist === "positions") {
      const rows = d.positions;
      $("#hist").innerHTML = rows.length ? `<table class="table"><thead><tr><th>Day</th><th>Exec</th><th>Mode</th><th>Contract</th><th>Status</th><th>Entry</th><th>Exit</th><th>Net</th></tr></thead><tbody>${
        rows.map(p => `<tr><td>${p.trading_day}</td><td>${p.exec_mode === "LIVE" ? '<span class="badge bad">LIVE</span>' : '<span class="badge">PAPER</span>'}</td><td>${MODE_SHORT[p.strategy_mode] || p.strategy_mode}</td>
          <td>${esc(p.label || "")}</td><td>${p.status}</td><td>${hhmm(p.entry_time)} ₹${fmtNum(p.entry_price)}</td><td>${p.exit_time ? hhmm(p.exit_time) + " ₹" + fmtNum(p.exit_price) : "—"}</td>
          <td class="${pnlClass(p.net_pnl)}">${p.net_pnl != null ? fmtINR(p.net_pnl, 2) : "—"}</td></tr>`).join("")}</tbody></table>`
        : stateHTML("empty", "No trades journaled yet", "");
    } else if (S.hist === "orders") {
      const rows = d.orders;
      $("#hist").innerHTML = rows.length ? `<table class="table"><thead><tr><th>Time</th><th>Symbol</th><th>Side</th><th>Qty</th><th>Status</th><th>Fill</th><th>Order id</th></tr></thead><tbody>${
        rows.map(o => `<tr><td>${o.ts.replace("T", " ").slice(0, 19)}</td><td>${esc(o.trading_symbol)}</td><td>${o.side}</td><td>${o.qty}</td><td>${esc(o.status)}</td><td>${o.fill_price != null ? "₹" + fmtNum(o.fill_price) : "—"}</td><td class="faint">${esc(o.broker_order_id || "")}</td></tr>`).join("")}</tbody></table>`
        : stateHTML("empty", "No orders yet", "");
    } else {
      const rows = d.sessions;
      $("#hist").innerHTML = rows.length ? `<table class="table"><thead><tr><th>#</th><th>Day</th><th>Exec</th><th>Mode</th><th>TF</th><th>Window</th><th>Started</th><th>Status</th></tr></thead><tbody>${
        rows.map(s => `<tr><td>${s.id}</td><td>${s.trading_day}</td><td>${s.mode}</td><td>${MODE_SHORT[s.config.strategy_mode] || ""}</td><td>${s.config.timeframe}m</td><td>${s.config.entry_start}–${s.config.entry_cutoff}</td><td>${s.started_at.slice(11, 19)}</td><td>${s.status}</td></tr>`).join("")}</tbody></table>`
        : stateHTML("empty", "No sessions yet", "");
    }
  }
  $("#histBtn").addEventListener("click", loadHist);

  // ------------------------------------------------------------------ replay
  $("#replayBtn").addEventListener("click", async () => {
    const day = $("#rDay").value;
    if (!day) { toast("Pick a day", true); return; }
    const c = cfg();
    $("#replayOut").innerHTML = stateHTML("loading", "Replaying…", "");
    try {
      const st = await api("/api/live/replay", { method: "POST", body: { ...c, day, max_trades: c.max_trades, max_loss: c.max_loss } });
      const cl = st.closed || [], sum = cl.reduce((a, t) => a + t.net_pnl, 0);
      const sig = (st.events || []).filter(e => e.level === "SIGNAL").length;
      $("#replayOut").innerHTML = `<div class="muted" style="font-size:13px;margin-bottom:6px">${sig} signal(s), ${cl.length} trade(s), net <b class="${pnlClass(sum)}">${fmtINR(sum, 2)}</b></div>` +
        (cl.length ? cl.map(t => `<div class="event" style="grid-template-columns:auto 1fr auto"><span class="mono">${hhmm(t.entry_time)}→${hhmm(t.exit_time)}</span><span>${esc(t.label)} <span class="faint">${esc(t.exit_reason)}</span></span><b class="mono ${pnlClass(t.net_pnl)}">${fmtINR(t.net_pnl)}</b></div>`).join("") : "");
    } catch (e) { $("#replayOut").innerHTML = `<div class="banner error">${esc(e.message)}</div>`; }
  });
  api("/api/data/coverage").then(c => { if (c.spot_to) { $("#rDay").value = c.spot_to; $("#rDay").max = c.spot_to; $("#rDay").min = c.spot_from; } }).catch(() => {});

  // ------------------------------------------------------------------ poll
  async function poll() {
    clearTimeout(S.timer);
    try { render(await api("/api/live/state")); } catch (e) { $("#liveBanner").innerHTML = `<div class="banner error">${esc(e.message)}</div>`; }
    S.timer = setTimeout(poll, 3000);
  }
  initChart(); poll(); loadHist();
})();
