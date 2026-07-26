(function () {
  "use strict";
  const byId = (id) => document.getElementById(id);
  const number = (value, suffix, decimals = 1) => value == null || !Number.isFinite(Number(value)) ? "--" : `${Number(value).toFixed(decimals)} ${suffix}`;
  const temp = (value) => number(value, "°C");
  const watts = (value) => number(value, "W", 0);
  const set = (id, value) => { const node = byId(id); if (node) node.textContent = value; };
  const trend = (value) => value == null ? "Trend: --" : `Trend: ${value > 0 ? "+" : ""}${Number(value).toFixed(2)} °C/h`;
  const timeLabel = (value) => value ? new Date(value).toLocaleString() : "--";

  async function json(url) {
    const response = await fetch(url, {cache: "no-store"});
    const payload = await response.json();
    if (!response.ok || payload.ok === false) throw new Error(payload.error || `HTTP ${response.status}`);
    return payload;
  }

  function renderStatus(data) {
    const sources = data.sources || {};
    const sourceLabel = (value) => value === "online" ? "Online" : value === "partial" ? "Partial" : "Offline";
    const contextLabels = {POOL_ACTIVE: "Pool active", POOL_DISABLED: "Pool disabled", UNKNOWN: "Unknown"};
    set("winterMode", `${data.mode || "learning"} · simulation`);
    set("winterEnergyContext", contextLabels[data.energy_context] || contextLabels.UNKNOWN);
    set("winterNetatmo", sourceLabel(sources.netatmo));
    set("winterGoodwe", sourceLabel(sources.goodwe));
    set("winterIntesis", sourceLabel(sources.intesis));
    set("winterLastSample", timeLabel(data.last_sample));
    const badge = byId("winterCollector");
    badge.textContent = data.collector_running ? "Learning active" : (data.enabled ? "Collector stopped" : "Disabled");
    badge.className = `pill ${data.collector_running ? "ok" : "gray"}`;
  }

  function renderCurrent(data) {
    const t = data.temperatures || {}, e = data.energy || {}, a = data.thermal_analysis || {}, d = data.decision || {};
    set("winterOutdoor", temp(t.outdoor)); set("winterMaster", temp(t.master_bedroom)); set("winterKids", temp(t.kids_room));
    set("winterAverage", temp(t.indoor_average)); set("winterDelta", temp(t.indoor_outdoor_delta));
    set("winterColdest", `Coldest: ${t.coldest_room || "--"} ${t.coldest_room_temp == null ? "" : temp(t.coldest_room_temp)}`);
    const trends = t.trends_c_per_hour || {};
    set("winterOutdoorTrend", trend(trends.outdoor)); set("winterMasterTrend", trend(trends.master_bedroom)); set("winterKidsTrend", trend(trends.kids_room));
    set("winterPv", watts(e.pv_production)); set("winterLoad", watts(e.house_consumption)); set("winterSoc", number(e.battery_soc, "%"));
    set("winterBattery", watts(e.battery_power)); set("winterGrid", watts(e.grid_power));
    set("winterIndoorTrend", trend(a.indoor_trend_c_per_hour).replace("Trend: ", ""));
    set("winterInertia", a.estimated_thermal_inertia_hours == null ? "--" : `${a.estimated_thermal_inertia_hours} h`);
    set("winterSamples", a.learning_samples == null ? "--" : String(a.learning_samples)); set("winterQuality", a.quality || "--");
    set("winterDecision", d.recommendation || "--"); set("winterDecisionReason", d.reason || "Waiting for data.");
    renderHvac(data.hvac || []);
  }

  function renderHvac(devices) {
    const host = byId("winterHvac");
    host.replaceChildren();
    if (!devices.length) { const card = document.createElement("article"); card.className = "card muted"; card.textContent = "No HVAC data available."; host.append(card); return; }
    devices.forEach((device) => {
      const card = document.createElement("article"); card.className = "card winter-value-card";
      const title = document.createElement("span"); title.textContent = device.name || device.id || "HVAC";
      const labels = {on: "ON", off: "OFF", standby: "STANDBY", offline: "OFFLINE", unknown: "UNKNOWN"};
      const state = document.createElement("strong"); state.textContent = `${labels[device.state] || "UNKNOWN"} · ${device.mode || "--"}`;
      const detail = document.createElement("small"); detail.textContent = `Room ${temp(device.temperature)} · Target ${temp(device.target_temperature)}`;
      card.append(title, state, detail); host.append(card);
    });
  }

  function drawChart(canvas, samples, series) {
    const ratio = window.devicePixelRatio || 1, width = Math.max(300, canvas.clientWidth), height = 230;
    canvas.width = width * ratio; canvas.height = height * ratio;
    const ctx = canvas.getContext("2d"); ctx.scale(ratio, ratio); ctx.clearRect(0, 0, width, height);
    const validNumber = (value) => value !== null && value !== undefined && value !== "" && Number.isFinite(Number(value));
    const all = series.flatMap((item) => samples.map((row) => row[item.key]).filter(validNumber).map(Number));
    if (!all.length || samples.length < 2) return false;
    let min = Math.min(...all), max = Math.max(...all); if (min === max) { min -= 1; max += 1; }
    const pad = 32; ctx.strokeStyle = "#d9e1e8"; ctx.beginPath(); ctx.moveTo(pad, 10); ctx.lineTo(pad, height - pad); ctx.lineTo(width - 8, height - pad); ctx.stroke();
    series.forEach((item) => { ctx.strokeStyle = item.color; ctx.lineWidth = 2; ctx.beginPath(); let open = false;
      samples.forEach((row, index) => { const raw = row[item.key]; if (!validNumber(raw)) { open = false; return; } const value = Number(raw);
        const x = pad + index * (width - pad - 10) / (samples.length - 1), y = 10 + (max - value) * (height - pad - 20) / (max - min);
        if (!open) ctx.moveTo(x, y); else ctx.lineTo(x, y); open = true;
      }); ctx.stroke(); });
    ctx.font = "12px sans-serif"; series.forEach((item, i) => { ctx.fillStyle = item.color; ctx.fillText(item.label, pad + i * 120, height - 8); });
    return true;
  }

  async function loadHistory() {
    try {
      const data = await json(`/api/winter/history?hours=${encodeURIComponent(byId("winterPeriod").value)}&limit=5000`), samples = data.samples || [];
      const temperatures = drawChart(byId("winterTemperatureChart"), samples, [{key:"outdoor_temp",label:"Outdoor",color:"#2474b5"},{key:"master_bedroom_temp",label:"Master",color:"#e06b36"},{key:"kids_room_temp",label:"Kids",color:"#6d56b3"}]);
      const energy = drawChart(byId("winterEnergyChart"), samples, [{key:"pv_production",label:"PV",color:"#d6a400"},{key:"house_consumption",label:"House",color:"#d94d4d"},{key:"grid_power",label:"Grid",color:"#2b8b68"}]);
      set("winterChartEmpty", temperatures || energy ? `${samples.length} samples` : "Historical data will appear after learning samples are collected.");
    } catch (error) { set("winterChartEmpty", error.message); }
  }

  async function refresh() {
    try { const [status, current] = await Promise.all([json("/api/winter/status"), json("/api/winter/current")]); renderStatus(status); renderCurrent(current); }
    catch (error) { const badge = byId("winterCollector"); badge.textContent = "Unavailable"; badge.className = "pill gray"; }
  }
  byId("winterPeriod").addEventListener("change", loadHistory);
  window.addEventListener("resize", loadHistory);
  refresh(); loadHistory(); setInterval(refresh, 60000);
}());
