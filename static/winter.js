(function () {
  "use strict";
  const byId = (id) => document.getElementById(id);
  const italianNumber = (value, decimals = 1) => new Intl.NumberFormat("it-IT", {minimumFractionDigits: decimals, maximumFractionDigits: decimals}).format(Number(value));
  const number = (value, suffix, decimals = 1) => value == null || !Number.isFinite(Number(value)) ? "--" : `${italianNumber(value, decimals)} ${suffix}`;
  const temp = (value) => number(value, "°C");
  const watts = (value) => number(value, "W", 0);
  const set = (id, value) => { const node = byId(id); if (node) node.textContent = value; };
  const trend = (value) => value == null ? "Andamento: --" : `Andamento: ${value > 0 ? "+" : ""}${Number(value).toFixed(2)} °C/h`;
  const timeLabel = (value) => value ? new Date(value).toLocaleString("it-IT") : "--";
  const I18N = {
    decisions: {
      WAIT_FOR_DATA: "IN ATTESA DEI DATI",
      SIMULATE_PREHEAT: "PRERISCALDAMENTO CONSIGLIATO",
      DEFER_HEATING: "RIMANDA IL RISCALDAMENTO",
      NO_HEATING: "NESSUN RISCALDAMENTO NECESSARIO",
      HOLD: "MANTIENI LO STATO ATTUALE"
    },
    compactDecisions: {
      WAIT_FOR_DATA: "ATTESA",
      SIMULATE_PREHEAT: "PRERISCALDAMENTO",
      DEFER_HEATING: "RINVIO",
      NO_HEATING: "NESSUN RISCALDAMENTO",
      HOLD: "MANTIENI STATO"
    },
    reasons: {
      INPUT_DATA_MISSING: "I dati necessari non sono disponibili.",
      INPUT_DATA_STALE: "I dati necessari non sono aggiornati.",
      INPUT_DATA_PARTIAL: "La temperatura interna è inferiore al valore obiettivo, ma il surplus fotovoltaico non può essere calcolato.",
      INDOOR_BELOW_TARGET: "La temperatura interna è inferiore al valore obiettivo.",
      INDOOR_ABOVE_TARGET: "La temperatura interna è superiore al valore obiettivo.",
      INDOOR_WITHIN_TARGET_BAND: "La temperatura interna rientra nella fascia di comfort configurata.",
      PV_SURPLUS_AVAILABLE: "Il surplus fotovoltaico è sufficiente per il preriscaldamento simulato.",
      PV_SURPLUS_INSUFFICIENT: "Il surplus fotovoltaico non è sufficiente.",
      BATTERY_SOC_ACCEPTABLE: "La carica della batteria è pari o superiore alla soglia configurata.",
      BATTERY_SOC_LOW: "La carica della batteria è inferiore alla soglia configurata.",
      TEMPERATURE_STABLE: "La variazione della temperatura interna nell’ultima ora risulta stabile.",
      TREND_DATA_INSUFFICIENT: "Lo storico continuo non è sufficiente per calcolare la variazione dell’ultima ora.",
      DECISION_RULE_HOLD: "La temperatura interna rientra nella fascia di comfort configurata.",
      NO_RECOMMENDATION_INPUT_UNAVAILABLE: "Non viene suggerita alcuna decisione perché i dati necessari non sono disponibili o aggiornati.",
      NO_PREHEAT_INDOOR_ABOVE_TARGET: "Non viene consigliato il preriscaldamento perché la temperatura interna è già superiore al valore obiettivo.",
      NO_PREHEAT_SURPLUS_UNKNOWN: "Non viene consigliato il preriscaldamento perché il surplus fotovoltaico non è disponibile.",
      NO_PREHEAT_PV_INSUFFICIENT: "Non viene consigliato il preriscaldamento perché il surplus fotovoltaico è insufficiente.",
      NO_PREHEAT_BATTERY_LOW: "Non viene consigliato il preriscaldamento perché la carica della batteria è inferiore alla soglia richiesta.",
      NO_ACTION_WITHIN_TARGET_BAND: "Non viene suggerita alcuna azione immediata perché la temperatura interna rientra nella fascia obiettivo.",
      NO_ACTION_TEMPERATURE_STABLE: "Non viene suggerita alcuna azione immediata perché la temperatura interna risulta stabile."
    },
    quality: {
      fresh: "Dati aggiornati", partial: "Dati parziali", missing: "Dati mancanti",
      stale: "Dati non aggiornati", online: "Dati aggiornati",
      driver_offline: "Collegamento non disponibile", stale_snapshot: "Dati non aggiornati",
      device_error: "Errore dispositivo", missing_telemetry: "Telemetria mancante",
      unknown_state: "Stato sconosciuto", unsupported_mode: "Modalità non supportata",
      missing_snapshot: "Dati mancanti"
    },
    states: {on:"Acceso", off:"Spento", standby:"In attesa", offline:"Non in linea", unknown:"Sconosciuto"},
    modes: {heat:"riscaldamento", cool:"raffrescamento", auto:"automatico", dry:"deumidificazione", fan:"ventilazione", fan_only:"ventilazione", off:"spento"},
    fan: {high:"alta", medium:"media", med:"media", low:"bassa", quiet:"silenziosa", auto:"automatica"}
  };
  const localized = (dictionary, code, fallback = "Sconosciuto") => dictionary[String(code || "")] || fallback;
  const qualityLabel = (value) => localized(I18N.quality, value, "Qualità non disponibile");
  const reasonLabel = (reason) => localized(I18N.reasons, reason && reason.code, reason && reason.text ? reason.text : "Motivazione non disponibile.");
  const DECISION_HISTORY_COMPACT_ROWS = 5;
  let decisionHistoryRows = [];
  let decisionHistoryExpanded = false;

  async function json(url, options = {}) {
    const response = await fetch(url, {cache: "no-store", ...options});
    const payload = await response.json();
    if (!response.ok || payload.ok === false) throw new Error(payload.error || `HTTP ${response.status}`);
    return payload;
  }

  function renderStatus(data) {
    const sources = data.sources || {};
    const sourceLabel = (value) => value === "online" ? "In linea" : value === "partial" ? "Parziale" : "Non in linea";
    const contextLabels = {POOL_ACTIVE: "Piscina attiva", POOL_DISABLED: "Piscina disabilitata", UNKNOWN: "Sconosciuto"};
    const modeLabels = {learning: "apprendimento"};
    set("winterMode", `${modeLabels[data.mode] || "sconosciuta"} · simulazione`);
    set("winterEnergyContext", contextLabels[data.energy_context] || contextLabels.UNKNOWN);
    set("winterNetatmo", sourceLabel(sources.netatmo));
    set("winterGoodwe", sourceLabel(sources.goodwe));
    set("winterIntesis", sourceLabel(sources.intesis));
    set("winterLastSample", timeLabel(data.last_sample));
    set("winterOperatingMode", data.automation === true ? "Modalità attuale: AUTOMAZIONE ATTIVA" : "Modalità attuale: SOLO SIMULAZIONE / APPRENDIMENTO");
    const badge = byId("winterCollector");
    badge.textContent = data.collector_running ? "Apprendimento attivo" : (data.enabled ? "Raccolta arrestata" : "Disabilitato");
    badge.className = `pill ${data.collector_running ? "ok" : "gray"}`;
    byId("winterAdvisorDot").classList.toggle("inactive", !data.collector_running);
  }

  function renderCurrent(data) {
    const t = data.temperatures || {}, e = data.energy || {}, a = data.thermal_analysis || {}, d = data.decision || {};
    set("winterOutdoor", temp(t.outdoor)); set("winterMaster", temp(t.master_bedroom)); set("winterKids", temp(t.kids_room));
    set("winterAverage", temp(t.indoor_average)); set("winterDelta", temp(t.indoor_outdoor_delta));
    set("winterColdest", `Più fredda: ${t.coldest_room || "--"} ${t.coldest_room_temp == null ? "" : temp(t.coldest_room_temp)}`);
    const trends = t.trends_c_per_hour || {};
    set("winterOutdoorTrend", trend(trends.outdoor)); set("winterMasterTrend", trend(trends.master_bedroom)); set("winterKidsTrend", trend(trends.kids_room));
    set("winterPv", watts(e.pv_production)); set("winterLoad", watts(e.house_consumption)); set("winterSoc", number(e.battery_soc, "%"));
    set("winterBattery", watts(e.battery_power)); set("winterGrid", watts(e.grid_power));
    set("winterIndoorTrend", trend(a.indoor_trend_c_per_hour).replace("Andamento: ", ""));
    set("winterInertia", a.estimated_thermal_inertia_hours == null ? "--" : `${a.estimated_thermal_inertia_hours} h`);
    const qualityLabels = {learning: "In apprendimento", preliminary: "Preliminare"};
    set("winterSamples", a.learning_samples == null ? "--" : String(a.learning_samples));
    set("winterQuality", qualityLabels[a.quality] || "Sconosciuta");
    set("winterDecision", localized(I18N.decisions, d.decision_code || d.recommendation, "Decisione non disponibile"));
    set("winterDecisionReason", localized(I18N.reasons, d.primary_reason_code, d.primary_reason_text || d.reason || "Motivazione non disponibile."));
    renderReasonList("winterSupportingReasons", d.supporting_reasons, "Nessuna motivazione aggiuntiva.");
    renderReasonList("winterAlternativeReasons", d.alternative_reasons, "Nessun’altra decisione pertinente.");
    renderAdvisorInputs(d.inputs || {});
    renderHvac(data.hvac || []);
    renderHvacLearning(data.hvac || [], data.hvac_learning || {});
  }

  function renderReasonList(id, reasons, emptyText) {
    const host = byId(id); host.replaceChildren();
    (reasons && reasons.length ? reasons : [{text: emptyText}]).forEach((reason) => {
      const item = document.createElement("li");
      item.textContent = reason.code ? reasonLabel(reason) : reason.text;
      host.append(item);
    });
  }

  function renderAdvisorInputs(inputs) {
    const host = byId("winterAdvisorInputs"); host.replaceChildren();
    const values = [
      ["Temperatura media interna", temp(inputs.indoor_average)],
      ["Stanza più fredda", `${inputs.coldest_room_name || "--"} · ${temp(inputs.coldest_room_temperature)}`],
      ["Temperatura obiettivo", temp(inputs.target_temperature)],
      ["Temperatura esterna", temp(inputs.outdoor_temperature)],
      ["Differenza interno / esterno", temp(inputs.indoor_outdoor_delta)],
      ["Produzione fotovoltaica", watts(inputs.pv_production)],
      ["Consumo abitazione", watts(inputs.house_consumption)],
      ["Surplus fotovoltaico", watts(inputs.pv_surplus)],
      ["Carica batteria", number(inputs.battery_soc, "%")],
      ["Variazione ultima ora", inputs.indoor_trend_c_per_hour == null ? "Storico insufficiente" : `${Number(inputs.indoor_trend_c_per_hour).toFixed(2)} °C/h`],
      ["Osservazione termica", inputs.thermal_observation_hours == null ? "Dati insufficienti" : `Preliminare · ${Number(inputs.thermal_observation_hours).toFixed(1)} h`],
      ["Qualità dei dati", qualityLabel(inputs.data_quality)]
    ];
    values.forEach(([label, value]) => {
      const row = document.createElement("div"), dt = document.createElement("dt"), dd = document.createElement("dd");
      dt.textContent = label; dd.textContent = value; row.append(dt, dd); host.append(row);
    });
  }

  function renderHvac(devices) {
    const host = byId("winterHvac");
    host.replaceChildren();
    if (!devices.length) { const card = document.createElement("article"); card.className = "card muted"; card.textContent = "Nessun dato della climatizzazione disponibile."; host.append(card); return; }
    devices.forEach((device) => {
      const card = document.createElement("article"); card.className = "card winter-value-card";
      const title = document.createElement("span"); title.textContent = device.name || device.id || "HVAC";
      const mode = localized(I18N.modes, String(device.mode || "").toLowerCase(), "modalità sconosciuta");
      const state = document.createElement("strong");
      const dot = document.createElement("span"); dot.className = `winter-device-dot ${device.state === "on" ? "active" : device.state === "off" ? "idle" : "unknown"}`;
      state.append(dot, document.createTextNode(`${localized(I18N.states, device.state)} · ${mode}`));
      const detail = document.createElement("small"); detail.textContent = `Ambiente ${temp(device.temperature)} · Obiettivo ${temp(device.target_temperature)}`;
      card.append(title, state, detail); host.append(card);
    });
  }

  function renderHvacLearning(currentDevices, learning) {
    const host = byId("winterHvacLearning");
    host.replaceChildren();
    const currentById = Object.fromEntries((currentDevices || []).map(device => [String(device.id), device]));
    const learningDevices = learning.devices || [];
    if (!learningDevices.length) {
      const card = document.createElement("article");
      card.className = "card muted";
      card.textContent = "I dati di apprendimento HVAC appariranno con i nuovi campioni.";
      host.append(card);
      return;
    }
    const rateLabel = (rate, label) => rate && rate.status === "preliminary"
      ? `${label}: ${Number(rate.c_per_hour).toFixed(2)} °C/h (${rate.valid_samples} campioni)`
      : `${label}: Dati insufficienti`;
    learningDevices.forEach(item => {
      const current = currentById[String(item.device_id)] || {};
      const rates = item.observed_rates || {};
      const card = document.createElement("article");
      card.className = "card winter-value-card";
      const title = document.createElement("span");
      title.textContent = item.device_name || item.device_id;
      const state = document.createElement("strong");
      state.textContent = `${localized(I18N.states, current.state)} · ${localized(I18N.modes, String(current.mode || "").toLowerCase(), "modalità sconosciuta")}`;
      const settings = document.createElement("small");
      settings.textContent = `Setpoint ${temp(current.target_temperature)} · Ventola ${localized(I18N.fan, String(current.fan_speed || "").toLowerCase(), current.fan_speed || "--")} · Qualità ${qualityLabel(current.data_quality)}`;
      const samples = document.createElement("small");
      samples.textContent = `Campioni HVAC validi: ${item.valid_historical_samples || 0}`;
      const observations = document.createElement("small");
      observations.textContent = [
        rateLabel(rates.hvac_off, "Variazione naturale (HVAC spento)"),
        rateLabel(rates.heating, "Risposta in riscaldamento"),
        rateLabel(rates.cooling, "Risposta in raffrescamento")
      ].join(" · ");
      card.append(title, state, settings, samples, observations);
      host.append(card);
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
      const temperatures = drawChart(byId("winterTemperatureChart"), samples, [{key:"outdoor_temp",label:"Esterno",color:"#2474b5"},{key:"master_bedroom_temp",label:"Matrimoniale",color:"#e06b36"},{key:"kids_room_temp",label:"Bambini",color:"#6d56b3"}]);
      const energy = drawChart(byId("winterEnergyChart"), samples, [{key:"pv_production",label:"FV",color:"#d6a400"},{key:"house_consumption",label:"Abitazione",color:"#d94d4d"},{key:"grid_power",label:"Rete",color:"#2b8b68"}]);
      const sampleLabel = samples.length === 1 ? "campione" : "campioni";
      set("winterChartEmpty", temperatures || energy ? `${samples.length} ${sampleLabel}` : "I dati storici appariranno dopo la raccolta dei campioni di apprendimento.");
    } catch (error) { set("winterChartEmpty", "Dati storici temporaneamente non disponibili."); }
  }

  function renderDecisionHistory() {
    const body = byId("winterDecisionHistory"), toggle = byId("winterDecisionHistoryToggle");
    body.replaceChildren();
    if (!decisionHistoryRows.length) {
      const row = document.createElement("tr"), cell = document.createElement("td");
      cell.colSpan = 8; cell.textContent = "Non è stata ancora registrata alcuna decisione simulata."; row.append(cell); body.append(row);
    }
    const visibleRows = decisionHistoryExpanded
      ? decisionHistoryRows
      : decisionHistoryRows.slice(0, DECISION_HISTORY_COMPACT_ROWS);
    visibleRows.forEach((item) => {
        const row = document.createElement("tr");
        [timeLabel(item.timestamp), localized(I18N.decisions, item.decision_code, "Decisione non disponibile"), localized(I18N.reasons, item.primary_reason_code, item.primary_reason_text || "Motivazione non disponibile."),
          temp(item.indoor_average), temp(item.target_temperature), temp(item.outdoor_temperature),
          watts(item.pv_surplus), number(item.battery_soc, "%")].forEach((value) => {
          const cell = document.createElement("td"); cell.textContent = value; row.append(cell);
        });
        body.append(row);
    });
    toggle.hidden = decisionHistoryRows.length <= DECISION_HISTORY_COMPACT_ROWS;
    toggle.textContent = decisionHistoryExpanded ? "Nascondi" : `Mostra tutte (${decisionHistoryRows.length})`;
    toggle.setAttribute("aria-expanded", String(decisionHistoryExpanded));
  }

  function toggleDecisionHistory() {
    decisionHistoryExpanded = !decisionHistoryExpanded;
    renderDecisionHistory();
  }

  async function loadDecisionHistory() {
    try {
      const [history, summary] = await Promise.all([
        json("/api/winter/decisions?hours=8760&limit=500"),
        json("/api/winter/decisions/daily")
      ]);
      decisionHistoryRows = history.decisions || [];
      renderDecisionHistory();
      const counts = summary.counts || {};
      const percent = (value) => value == null ? "--" : `${Number(value).toFixed(1)}%`;
      set("winterDecisionSummary",
        `Oggi: ${summary.total || 0} · ATTESA ${counts.WAIT_FOR_DATA || 0} · PRERISCALDAMENTO ${counts.SIMULATE_PREHEAT || 0} · RINVIO ${counts.DEFER_HEATING || 0} · NESSUN RISCALDAMENTO ${counts.NO_HEATING || 0} · MANTIENI STATO ${counts.HOLD || 0} · Ingressi validi ${percent(summary.valid_input_percentage)} · HVAC validi ${percent(summary.valid_hvac_observation_percentage)}`
      );
    } catch (error) {
      set("winterDecisionSummary", "La cronologia delle decisioni non è temporaneamente disponibile.");
    }
  }

  async function loadSettings() {
    const input = byId("winterTargetInput");
    if (!input) return;
    try {
      const settings = await json("/api/winter/settings");
      input.min = String(settings.minimum);
      input.max = String(settings.maximum);
      input.step = String(settings.step);
      input.value = Number(settings.target_temperature).toFixed(1);
    } catch (error) {
      set("winterTargetMessage", "Impossibile caricare la configurazione.");
    }
  }

  async function saveSettings(event) {
    event.preventDefault();
    const input = byId("winterTargetInput"), message = byId("winterTargetMessage");
    const value = String(input.value || "").trim().replace(",", ".");
    message.textContent = "Salvataggio…";
    message.className = "winter-target-message";
    try {
      const saved = await json("/api/winter/settings", {
        method: "POST",
        headers: {"Content-Type": "application/json"},
        body: JSON.stringify({target_temperature: value})
      });
      input.value = Number(saved.target_temperature).toFixed(1);
      message.textContent = `Temperatura obiettivo salvata: ${temp(saved.target_temperature)}.`;
      message.className = "winter-target-message success";
      await refresh();
    } catch (error) {
      message.textContent = error.message || "Valore non valido.";
      message.className = "winter-target-message error";
    }
  }

  async function refresh() {
    try { const [status, current] = await Promise.all([json("/api/winter/status"), json("/api/winter/current")]); renderStatus(status); renderCurrent(current); }
    catch (error) { const badge = byId("winterCollector"); badge.textContent = "Non disponibile"; badge.className = "pill gray"; }
  }
  byId("winterPeriod").addEventListener("change", loadHistory);
  byId("winterDecisionHistoryToggle").addEventListener("click", toggleDecisionHistory);
  if (byId("winterTargetForm")) byId("winterTargetForm").addEventListener("submit", saveSettings);
  window.addEventListener("resize", loadHistory);
  refresh(); loadSettings(); loadHistory(); loadDecisionHistory(); setInterval(refresh, 60000); setInterval(loadDecisionHistory, 300000);
}());
