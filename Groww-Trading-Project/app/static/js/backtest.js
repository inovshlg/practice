/* Backtest page: run, filter, audit trades on a chart. */
(() => {
  const S = {
    result: null, mode: "ITM5_BUY", outcome: "all", cross: "all", exit: "all", tf: 15,
    list: [], sel: -1, chartMode: "spot", chart: null, series: {}, eqChart: null, eqSeries: null,
  };
  const chartCache = new Map();   // "day|tf|symbol" -> Promise(chart payload)
  const resultCache = new Map();  // client-side cache of backtest runs

  const MODE_SHORT = { ITM5_BUY: "ITM5 buy", ATM_BUY: "ATM buy", ATM_SELL: "ATM sell" };
  const REASON = { REVERSAL: "Reversal", OPPOSITE_CROSS: "Opposite cross", "SQUARE_OFF_15:00": "Square-off 15:00" };

  // ------------------------------------------------------------------ setup
  bindSeg($("#segTf"), v => { S.tf = +v; run(); });
  bindSeg($("#segMode"), v => { S.mode = v; refresh(true); });
  bindSeg($("#segOut"), v => { S.outcome = v; refresh(true); });
  bindSeg($("#segCross"), v => { S.cross = v; refresh(true); });
  $("#fExit").addEventListener("change", e => { S.exit = e.target.value; refresh(true); });
  $("#fStart").addEventListener("change", run);
  $("#fCut").addEventListener("change", run);
  $("#runBtn").addEventListener("click", run);
  document.addEventListener("keydown", e => {
    if (!S.result || ["INPUT", "SELECT", "TEXTAREA"].includes(document.activeElement.tagName)) return;
    if (e.key === "ArrowDown" || e.key === "j") { e.preventDefault(); select(Math.min(S.sel + 1, S.list.length - 1)); }
    if (e.key === "ArrowUp" || e.key === "k") { e.preventDefault(); select(Math.max(S.sel - 1, 0)); }
  });
  window.addEventListener("themechange", () => { if (S.chart) { applyTheme(); } });

  function showEmpty(title, body) {
    $("#btBody").innerHTML = `<div class="card">${stateHTML("empty", title, body)}</div>`;
  }

  async function init() {
    try {
      const c = await api("/api/data/coverage");
      $("#dataBadge").innerHTML = c.source === "demo"
        ? '<span class="badge warn" title="Synthetic market — for trying the app only">DEMO DATA</span>'
        : (c.source === "groww" ? '<span class="badge good">Groww data</span>' : "");
      if (!c.spot_days) {
        showEmpty("No market data yet", 'Download candles from Groww (or generate demo data) on the <a href="/data">Data page</a>.');
        $("#runBtn").disabled = true;
        return;
      }
      const from = new Date(c.spot_from + "T00:00:00Z"); from.setUTCDate(from.getUTCDate() + 14);
      const f = from.toISOString().slice(0, 10);
      $("#fFrom").value = f > c.spot_to ? c.spot_from : f;
      $("#fTo").value = c.spot_to;
      $("#fFrom").min = $("#fTo").min = c.spot_from; $("#fFrom").max = $("#fTo").max = c.spot_to;
      run();
    } catch (e) {
      $("#btError").innerHTML = `<div class="banner error">${esc(e.message)}</div>`;
      showEmpty("Cannot read the database", "Check that PostgreSQL is running.");
    }
  }

  // ------------------------------------------------------------------ run
  async function run() {
    const body = { from: $("#fFrom").value, to: $("#fTo").value, timeframe: S.tf,
                   entry_start: $("#fStart").value, entry_cutoff: $("#fCut").value, lots: +$("#fLots").value || 1 };
    const key = JSON.stringify(body);
    $("#btError").innerHTML = "";
    $("#loading").classList.remove("hidden");
    try {
      if (!resultCache.has(key)) resultCache.set(key, api("/api/backtest", { method: "POST", body }));
      const res = await resultCache.get(key);
      S.result = res;
      mount();
    } catch (e) {
      resultCache.delete(key);
      $("#btError").innerHTML = `<div class="banner error"><b>Backtest failed.</b>&nbsp;${esc(e.message)}</div>`;
    } finally { $("#loading").classList.add("hidden"); }
  }

  function mount() {
    if (!$("#kpis")) {
      $("#btBody").innerHTML = "";
      $("#btBody").appendChild($("#tplBody").content.cloneNode(true));
      $("#tradeList").addEventListener("click", e => { const r = e.target.closest(".tl-row"); if (r) select(+r.dataset.i); });
      bindSeg($("#segChart"), v => { S.chartMode = v; drawChart(); });
      $("#cmpTable").addEventListener("click", e => { const r = e.target.closest("tr[data-m]"); if (r) { S.mode = r.dataset.m; setSeg($("#segMode"), S.mode); refresh(true); } });
      initCharts();
    }
    renderNoTrade();
    refresh(true);
  }

  // ------------------------------------------------------------------ filtering + KPIs
  function filtered() {
    return S.result.trades.filter(t => {
      if (t.mode !== S.mode) return false;
      if (S.outcome === "win" && !(t.net_pnl > 0)) return false;
      if (S.outcome === "loss" && t.net_pnl > 0) return false;
      if (S.cross !== "all") { const n = +S.cross; if (n === 4 ? t.entry_cross_no < 4 : t.entry_cross_no !== n) return false; }
      if (S.exit === "whipsaw") { if (!t.whipsaw) return false; }
      else if (S.exit !== "all" && t.exit_reason !== S.exit) return false;
      return true;
    });
  }

  function stats(ts) {
    const n = ts.length, nets = ts.map(t => t.net_pnl);
    const wins = nets.filter(x => x > 0), losses = nets.filter(x => x <= 0);
    const gw = wins.reduce((a, b) => a + b, 0), gl = -losses.reduce((a, b) => a + b, 0);
    return { n, winRate: n ? wins.length / n * 100 : null, net: nets.reduce((a, b) => a + b, 0),
             gross: ts.reduce((a, t) => a + t.gross_pnl, 0), friction: ts.reduce((a, t) => a + t.friction, 0),
             pf: gl > 0 ? gw / gl : null, wins: wins.length, losses: losses.length };
  }

  function refresh(resetSel) {
    S.list = filtered();
    renderKpis(); renderCompare(); renderEquity(); renderList();
    if (resetSel) select(S.list.length ? 0 : -1, true);
  }

  function kpi(label, value, sub, cls = "", accent = false) {
    return `<div class="kpi ${accent ? "accent" : ""}"><div class="label">${label}</div><div class="value ${cls}">${value}</div><div class="sub">${sub}</div></div>`;
  }
  function renderKpis() {
    const s = stats(S.list), t = S.list[S.sel];
    const filt = S.list.length !== S.result.metrics[S.mode].trades ? ` · filtered from ${S.result.metrics[S.mode].trades}` : "";
    $("#kpis").innerHTML = [
      kpi("Total trades", s.n, `${s.wins} W / ${s.losses} L${filt}`),
      kpi("Win rate", s.winRate === null ? "—" : s.winRate.toFixed(1) + "%", "net PnL > 0"),
      kpi("Net PnL", fmtINR(s.net), `after ${fmtINR(s.friction)} friction`, pnlClass(s.net)),
      kpi("Gross PnL", fmtINR(s.gross), "before slippage & charges", pnlClass(s.gross)),
      kpi("Profit factor", s.pf === null ? (s.n ? "∞" : "—") : s.pf.toFixed(2), "gross win ÷ gross loss (net)"),
      kpi("Selected trade", t ? fmtINR(t.net_pnl) : "—", t ? `#${t.id} · ${fmtDate(t.day)} ${hhmm(t.entry_time)}` : "none selected", t ? pnlClass(t.net_pnl) : "", true),
    ].join("");
  }

  function renderCompare() {
    const M = S.result.metrics;
    const rows = [["Trades", m => m.trades], ["Win rate", m => m.win_rate === null ? "—" : m.win_rate + "%"],
      ["Net PnL", m => `<span class="${pnlClass(m.net_pnl)}">${fmtINR(m.net_pnl)}</span>`],
      ["Gross PnL", m => `<span class="${pnlClass(m.gross_pnl)}">${fmtINR(m.gross_pnl)}</span>`],
      ["Friction", m => fmtINR(m.friction)], ["Profit factor", m => fmtNum(m.profit_factor)],
      ["Payoff ratio", m => fmtNum(m.payoff_ratio)], ["Expectancy", m => fmtINR(m.expectancy)],
      ["Max DD", m => `${fmtINR(m.max_dd)} <span class="faint">(${fmtNum(m.max_dd_pct, 1)}%)</span>`],
      ["Whipsaws", m => m.whipsaws]];
    const modes = ["ITM5_BUY", "ATM_BUY", "ATM_SELL"].filter(m => M[m]);
    // transpose: modes as rows so the click-to-select maps naturally
    $("#cmpTable").innerHTML = `<thead><tr><th>Mode</th>${rows.map(r => `<th>${r[0]}</th>`).join("")}</tr></thead><tbody>${
      modes.map(m => `<tr data-m="${m}" class="${m === S.mode ? "sel" : ""}"><td><b>${MODE_SHORT[m]}</b></td>${rows.map(r => `<td>${r[1](M[m])}</td>`).join("")}</tr>`).join("")
    }</tbody>`;
    const p = S.result.params;
    $("#eqLabel").textContent = `${MODE_SHORT[S.mode]} · ${p.timeframe}m · ${p.entry_start}–${p.entry_cutoff} · ${S.result.trading_days} days`;
  }

  // ------------------------------------------------------------------ trade list
  function renderList() {
    $("#tlCount").textContent = `(${S.list.length})`;
    if (!S.list.length) {
      $("#tradeList").innerHTML = stateHTML("empty", "No trades match", "Change the filters or the date range.");
      return;
    }
    $("#tradeList").innerHTML = S.list.map((t, i) => {
      const up = t.signal === "BULLISH";
      const badges = [`<span class="badge">#${t.entry_cross_no}</span>`];
      if (t.whipsaw) badges.push('<span class="badge warn">whipsaw</span>');
      if (t.exit_reason === "REVERSAL") badges.push('<span class="badge info">rev</span>');
      if (t.flags.length) badges.push(`<span class="badge bad" title="${esc(t.flags.join(", "))}">!</span>`);
      return `<div class="tl-row" data-i="${i}">
        <div class="tl-icon ${up ? "up" : "down"}">${up ? "↗" : "↘"}</div>
        <div class="tl-main"><div class="t1">${t.side === "SELL" ? "Sell" : "Buy"} ${t.strike} ${t.opt_type}</div>
          <div class="t2">${fmtDate(t.day)} · ${hhmm(t.entry_time)}→${hhmm(t.exit_time)} ${badges.join("")}</div></div>
        <div class="tl-pnl ${pnlClass(t.net_pnl)}">${fmtINR(t.net_pnl)}<small>gross ${fmtINR(t.gross_pnl)}</small></div></div>`;
    }).join("");
  }

  function select(i, silent = false) {
    if (!S.list.length) { S.sel = -1; renderDetail(null); renderKpis(); return; }
    if (i < 0) i = 0;
    S.sel = i;
    $$(".tl-row").forEach(r => r.classList.toggle("sel", +r.dataset.i === i));
    const row = $(`.tl-row[data-i="${i}"]`);
    if (row && !silent) row.scrollIntoView({ block: "nearest" });
    renderKpis();
    renderDetail(S.list[i]);
    // prefetch neighbours so arrow-key navigation is instant
    [i - 1, i + 1, i + 2].forEach(j => { const t = S.list[j]; if (t) getChart(t); });
  }

  // ------------------------------------------------------------------ detail
  function meta(label, value, cls = "", sans = false) {
    return `<div class="meta"><div class="label">${label}</div><div class="value ${cls} ${sans ? "sans" : ""}">${value}</div></div>`;
  }
  function renderDetail(t) {
    if (!t) {
      $("#metabar").innerHTML = `<div style="grid-column:1/-1">${stateHTML("empty", "No trade selected", "")}</div>`;
      $("#costCard").innerHTML = $("#execCard").innerHTML = '<div class="muted">—</div>';
      clearChart();
      return;
    }
    const dir = `${t.signal === "BULLISH" ? '<span class="pos">▲ Bullish</span>' : '<span class="neg">▼ Bearish</span>'} · ${t.side}`;
    $("#metabar").innerHTML = [
      meta("Symbol", esc(t.label), "", true),
      meta("Direction", dir, "", true),
      meta("NIFTY entry → exit", `${fmtNum(t.spot_entry)} → ${fmtNum(t.spot_exit)}`),
      meta("Option entry → exit", `₹${fmtNum(t.entry_raw)} → ₹${fmtNum(t.exit_raw)}`),
      meta("Expiry / DTE", `${fmtDate(t.expiry)} · ${t.dte}d`),
      meta("Exit reason", REASON[t.exit_reason] || t.exit_reason, "", true),
      meta("Net PnL", fmtINR(t.net_pnl, 2), pnlClass(t.net_pnl)),
      meta("Flags", (t.whipsaw ? '<span class="badge warn">WHIPSAW</span> ' : "") + `<span class="badge">cross #${t.entry_cross_no}</span> ` +
        (t.flags.length ? t.flags.map(f => `<span class="badge bad">${esc(f)}</span>`).join(" ") : (t.whipsaw ? "" : '<span class="badge good">clean</span>')), "", true),
    ].join("");
    const c = t.charges;
    const line = (l, v, cls = "") => `<div class="${cls}">${l}</div><div class="num ${cls}">${v}</div>`;
    $("#costCard").innerHTML = `<div class="costs">
      ${line("Gross PnL", `<span class="${pnlClass(t.gross_pnl)}">${fmtINR(t.gross_pnl, 2)}</span>`)}
      <div class="sep"></div>
      ${line(`Slippage <span class="faint">(0.5% + pts)</span>`, fmtINR(-t.slippage_cost, 2))}
      ${line("Brokerage", fmtINR(-c.brokerage, 2))}
      ${line(`STT <span class="faint">(sell side)</span>`, fmtINR(-c.stt, 2))}
      ${line("Exchange txn", fmtINR(-c.exchange_txn, 2))}
      ${line("SEBI fee", fmtINR(-c.sebi_fee, 2))}
      ${line(`Stamp duty <span class="faint">(buy side)</span>`, fmtINR(-c.stamp_duty, 2))}
      ${line("GST 18%", fmtINR(-c.gst, 2))}
      <div class="sep"></div>
      ${line("Total friction", fmtINR(-t.friction, 2))}
      ${line("Net PnL", `<span class="${pnlClass(t.net_pnl)}">${fmtINR(t.net_pnl, 2)}</span>`, "total")}
    </div>`;
    $("#execCard").innerHTML = `<dl class="kv">
      <dt>Mode</dt><dd>${MODE_SHORT[t.mode]}</dd>
      <dt>Signal bar</dt><dd>${hhmm(t.signal_bar)} (${t.timeframe}m)</dd>
      <dt>Entry fill</dt><dd>${hhmm(t.entry_time)} · ₹${fmtNum(t.entry_fill)}</dd>
      <dt>Exit fill</dt><dd>${hhmm(t.exit_time)} · ₹${fmtNum(t.exit_fill)}</dd>
      <dt>Quantity</dt><dd>${t.qty} (${t.qty / window.LOT_SIZE} lot)</dd>
      <dt>Bars held</dt><dd>${fmtNum(t.bars_held, 1)}</dd>
      <dt>Day cross #</dt><dd>${t.day_cross_no} (window #${t.entry_cross_no})</dd>
      <dt>Contract</dt><dd style="font-size:11px">${esc(t.symbol)}</dd>
    </dl>`;
    drawChart();
  }

  // ------------------------------------------------------------------ charts
  function initCharts() {
    if (!window.LightweightCharts) {
      $("#chart").innerHTML = stateHTML("error", "Chart library failed to load", "Check your internet connection .");
      return;
    }
    const opts = () => {
      const c = chartColors();
      return { layout: { background: { color: c.bg }, textColor: c.text, fontFamily: "ui-monospace, Menlo, monospace" },
               grid: { vertLines: { color: c.grid }, horzLines: { color: c.grid } },
               rightPriceScale: { borderColor: c.grid }, timeScale: { borderColor: c.grid, timeVisible: true, secondsVisible: false },
               crosshair: { mode: 0 }, autoSize: true };
    };
    S.opts = opts;
    S.chart = LightweightCharts.createChart($("#chart"), opts());
    const c = chartColors();
    S.series.candles = S.chart.addCandlestickSeries({ upColor: c.up, downColor: c.down, borderVisible: false, wickUpColor: c.up, wickDownColor: c.down });
    S.series.ema9 = S.chart.addLineSeries({ color: c.amber, lineWidth: 2, priceLineVisible: false, lastValueVisible: false, crosshairMarkerVisible: false });
    S.series.ema21 = S.chart.addLineSeries({ color: c.blue, lineWidth: 2, priceLineVisible: false, lastValueVisible: false, crosshairMarkerVisible: false });
    S.eqChart = LightweightCharts.createChart($("#eqChart"), { ...opts(), timeScale: { ...opts().timeScale, timeVisible: false } });
    S.eqSeries = S.eqChart.addAreaSeries({ priceFormat: { type: "price", precision: 0, minMove: 1 }, lineColor: c.accent, topColor: c.accent + "55", bottomColor: c.accent + "05", lineWidth: 2 });
  }
  function applyTheme() {
    S.chart.applyOptions(S.opts()); S.eqChart.applyOptions(S.opts());
    const c = chartColors();
    S.series.candles.applyOptions({ upColor: c.up, downColor: c.down, wickUpColor: c.up, wickDownColor: c.down });
    S.series.ema9.applyOptions({ color: c.amber }); S.series.ema21.applyOptions({ color: c.blue });
    S.eqSeries.applyOptions({ lineColor: c.accent, topColor: c.accent + "55", bottomColor: c.accent + "05" });
    drawChart();
  }
  function renderEquity() {
    if (!S.eqSeries) return;
    const pts = new Map();
    for (const p of S.result.metrics[S.mode].equity) pts.set(isoToChartTs(p.t), p.equity);
    S.eqSeries.setData([...pts.entries()].sort((a, b) => a[0] - b[0]).map(([time, value]) => ({ time, value })));
    S.eqChart.timeScale().fitContent();
  }
  function clearChart() {
    if (!S.chart) return;
    Object.values(S.series).forEach(s => { s.setData([]); s.setMarkers([]); });
  }
  function getChart(t) {
    const key = `${t.day}|${t.timeframe}|${t.symbol}`;
    if (!chartCache.has(key)) {
      const p = api(`/api/chart?day=${t.day}&tf=${t.timeframe}&symbol=${encodeURIComponent(t.symbol)}`);
      p.catch(() => chartCache.delete(key));
      chartCache.set(key, p);
    }
    return chartCache.get(key);
  }
  const barAtOrBefore = (bars, ts) => { let best = bars.length ? bars[0].time : ts; for (const b of bars) { if (b.time <= ts) best = b.time; else break; } return best; };

  async function drawChart() {
    const t = S.list[S.sel];
    if (!t || !S.chart) return;
    $("#chartNote").textContent = "loading…";
    let d;
    try { d = await getChart(t); }
    catch (e) { $("#chartNote").textContent = ""; $("#chart").title = e.message; toast("Chart: " + e.message, true); return; }
    if (S.list[S.sel] !== t) return; // user moved on
    const c = chartColors();
    const entryTs = isoToChartTs(t.entry_time), exitTs = isoToChartTs(t.exit_time), sigTs = isoToChartTs(t.signal_bar);
    const long = t.signal === "BULLISH";
    if (S.chartMode === "spot") {
      S.series.candles.setData(d.spot);
      S.series.ema9.setData(d.ema9); S.series.ema21.setData(d.ema21);
      const mk = [
        { time: sigTs, position: long ? "belowBar" : "aboveBar", color: c.accent, shape: "circle", text: "" },
        { time: barAtOrBefore(d.spot, entryTs), position: long ? "belowBar" : "aboveBar", color: c.up, shape: long ? "arrowUp" : "arrowDown", text: `IN ${hhmm(t.entry_time)}` },
        { time: barAtOrBefore(d.spot, exitTs), position: long ? "aboveBar" : "belowBar", color: c.down, shape: long ? "arrowDown" : "arrowUp", text: `OUT ${hhmm(t.exit_time)}` },
      ].sort((a, b) => a.time - b.time);
      S.series.candles.setMarkers(mk);
      $("#chartNote").textContent = `NIFTY ${t.timeframe}m · ${fmtDate(t.day)}`;
    } else {
      S.series.ema9.setData([]); S.series.ema21.setData([]);
      if (!d.option.length) { S.series.candles.setData([]); S.series.candles.setMarkers([]); $("#chartNote").textContent = "no option candles stored"; return; }
      S.series.candles.setData(d.option);
      const buy = t.side === "BUY";
      S.series.candles.setMarkers([
        { time: barAtOrBefore(d.option, entryTs), position: buy ? "belowBar" : "aboveBar", color: c.up, shape: buy ? "arrowUp" : "arrowDown", text: `${t.side} ₹${fmtNum(t.entry_raw)}` },
        { time: barAtOrBefore(d.option, exitTs), position: buy ? "aboveBar" : "belowBar", color: c.down, shape: buy ? "arrowDown" : "arrowUp", text: `EXIT ₹${fmtNum(t.exit_raw)}` },
      ].sort((a, b) => a.time - b.time));
      $("#chartNote").textContent = `${t.label} · 1m`;
    }
    S.chart.timeScale().fitContent();
  }

  function renderNoTrade() {
    const nt = S.result.no_trade_days, sk = S.result.skipped;
    $("#ntCount").textContent = `(${nt.length} days, ${sk.length} skipped entries)`;
    $("#noTrade").innerHTML = (nt.length || sk.length) ? `<div class="table-wrap"><table class="table"><thead><tr><th>Date</th><th>Mode</th><th>Time</th><th style="text-align:left">Reason</th></tr></thead><tbody>${
      nt.map(r => `<tr><td>${fmtDate(r.day)}</td><td>all</td><td>—</td><td style="text-align:left;font-family:var(--sans)">${esc(r.reason)}</td></tr>`).join("") +
      sk.map(r => `<tr><td>${fmtDate(r.day)}</td><td>${MODE_SHORT[r.mode]}</td><td>${r.time}</td><td style="text-align:left;font-family:var(--sans)">Skipped: ${esc(r.reason)}</td></tr>`).join("")
    }</tbody></table></div>` : '<div class="muted">Every trading day produced at least one trade.</div>';
  }

  init();
})();
