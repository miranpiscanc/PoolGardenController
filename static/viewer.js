let viewerRefreshTimer = null;
let viewerRefreshMs = 30000;

function queryToken() {
  return new URLSearchParams(window.location.search).get('token') || '';
}

function viewerStatusUrl() {
  const params = new URLSearchParams();
  const token = queryToken();
  if (token) params.set('token', token);
  const query = params.toString();
  return '/api/viewer/status' + (query ? `?${query}` : '');
}

function applyViewerTokenLinks() {
  const token = queryToken();
  if (!token) return;
  document.querySelectorAll('.viewer-token-link').forEach(link => {
    const url = new URL(link.getAttribute('href'), window.location.origin);
    url.searchParams.set('token', token);
    link.href = url.pathname + url.search;
  });
}

async function viewerApi() {
  const response = await fetch(viewerStatusUrl(), { cache: 'no-store' });
  if (!response.ok) throw new Error(response.status === 403 ? 'Accesso non autorizzato' : 'Dati non disponibili');
  return response.json();
}

function esc(value) {
  return String(value ?? '').replace(/[&<>"']/g, c => ({
    '&': '&amp;',
    '<': '&lt;',
    '>': '&gt;',
    '"': '&quot;',
    "'": '&#39;'
  }[c]));
}

function numericValue(value) {
  if (value === null || value === undefined || value === '') return null;
  const number = Number(value);
  return Number.isFinite(number) ? number : null;
}

function fmtSec(s) {
  s = Math.max(0, Number(s || 0));
  const h = Math.floor(s / 3600);
  const m = Math.floor((s % 3600) / 60);
  return `${h}h ${String(m).padStart(2, '0')}m`;
}

function fmtPower(value) {
  const number = numericValue(value);
  if (number === null) return '--';
  const absolute = Math.abs(number);
  if (absolute < 1000) return Math.round(absolute) + ' W';
  return (absolute / 1000).toFixed(2) + ' kW';
}

function fmtPercent(value) {
  const number = numericValue(value);
  return number === null ? '--' : Math.round(number) + ' %';
}

function fmtCelsius(value) {
  const number = numericValue(value);
  return number === null ? '--' : number.toFixed(1) + ' °C';
}

function fmtDateTime(value) {
  if (!value) return '--';
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return String(value);
  return date.toLocaleString('it-IT', {
    day: '2-digit',
    month: '2-digit',
    year: 'numeric',
    hour: '2-digit',
    minute: '2-digit',
    second: '2-digit'
  });
}

function fmtTime(value) {
  if (!value) return '--';
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) {
    const parts = String(value).split('T');
    return (parts[1] || value || '--').slice(0, 5);
  }
  return date.toLocaleTimeString('it-IT', { hour: '2-digit', minute: '2-digit' });
}

function setText(id, value) {
  const el = document.getElementById(id);
  if (el) el.textContent = value;
}

function relayLabel(active) {
  if (active === true) return ['ACCESO', 'on'];
  if (active === false) return ['SPENTO', 'off'];
  return ['SINCRONIZZAZIONE', 'unknown'];
}

function batteryDirectionLabel(value) {
  const mode = String(value || '').trim().toLowerCase();
  if (mode === 'charge') return '⬇ Carica';
  if (mode === 'discharge') return '⬆ Scarica';
  if (mode === 'idle') return '⚪ Inattiva';
  return '--';
}

function gridDirectionLabel(value) {
  const number = numericValue(value);
  if (number === null) return '--';
  if (number > 0) return '🟢 ⬆ Esportazione';
  if (number < 0) return '🔴 ⬇ Prelievo';
  return '⚪ Bilanciato';
}

function pumpProgress(pump) {
  if (pump.active === true && pump.duration_hours) {
    const perc = 100 - (Number(pump.remaining_seconds || 0) / (Number(pump.duration_hours) * 3600) * 100);
    return Math.min(100, Math.max(0, perc));
  }
  return 0;
}

function solarStatusLabel(status, enabled) {
  const key = String(status || (enabled ? 'ACTIVE' : 'DISABLED')).toLowerCase();
  if (key === 'active') return 'ATTIVA';
  if (key === 'disabled') return 'DISABILITATA';
  return status || '--';
}

function solarMessage(solar) {
  const key = solar.status_message_key;
  const vars = solar.status_message_vars || {};
  const messages = {
    'solar.message.disabled': 'Automazione disabilitata',
    'solar.message.waitingForStart': `In attesa dell'ora di avvio (${vars.time || '--:--'})`,
    'solar.message.heatingStartedAt': `Riscaldamento avviato automaticamente alle ${vars.time || '--:--'}`,
    'solar.message.completedToday': 'Automazione completata per oggi',
    'solar.message.safeStopRunning': 'Arresto sicuro in corso...',
    'solar.message.safeStopScheduled': 'Arresto sicuro programmato',
    'solar.message.temperatureOk': 'Temperatura OK\n(acqua già sopra la soglia configurata)',
    'solar.message.checkingWaterTemperature': 'Controllo temperatura acqua...',
    'solar.message.nextTemperatureCheck': `Prossimo controllo temperatura alle ${vars.time || '--:--'}`,
    'solar.message.waitingForNextCheck': `In attesa del prossimo controllo temperatura (${vars.time || '--:--'})`,
    'solar.message.heatingAlreadyRunning': 'Riscaldamento già in funzione',
    'solar.message.maintainingTarget': 'Mantenimento temperatura obiettivo...',
    'solar.message.earlyCompletionTimer': `Timer completamento anticipato:\n${vars.minutes || 0} min rimanenti`,
    'solar.message.completedEarly': 'Automazione completata in anticipo',
    'solar.message.pvInsufficientConfirmation': `⚠ Produzione FV insufficiente\n\nSecondo controllo alle ${vars.time || '--:--'}`
  };
  return messages[key] || solar.status_message || '--';
}

function renderTemperatureValue(id, sensor) {
  const el = document.getElementById(id);
  if (!el || !sensor) return;
  if (sensor.online) {
    el.textContent = fmtCelsius(sensor.value);
    el.className = 'temperature-value';
    return;
  }
  el.textContent = 'OFFLINE';
  el.className = 'temperature-value offline';
}

function renderEnergy(energy) {
  const pvProduction = numericValue(energy.pv_production);
  const houseConsumption = numericValue(energy.house_consumption);
  const availableSurplus = energy.available_surplus ?? (
    pvProduction === null || houseConsumption === null ? null : Math.max(0, pvProduction - houseConsumption)
  );
  const batteryPower = energy.pbattery1 ?? energy.battery_power;
  const gridPower = numericValue(energy.grid_power);

  setText('viewerHouseConsumption', fmtPower(energy.house_consumption));
  setText('viewerNormalLoads', fmtPower(energy.normal_loads));
  setText('viewerBackupLoads', fmtPower(energy.backup_loads));
  setText('viewerPvProduction', fmtPower(energy.pv_production));
  setText('viewerManagerHouse', fmtPower(energy.house_consumption));
  setText('viewerBatterySoc', fmtPercent(energy.battery_soc));
  setText('viewerBatteryDirection', batteryDirectionLabel(energy.battery_mode_label));
  setText('viewerBatteryPower', fmtPower(batteryPower));
  setText('viewerBatteryTemperature', fmtCelsius(energy.battery_temperature));
  setText('viewerGridPower', fmtPower(gridPower === null ? null : Math.abs(gridPower)));
  setText('viewerGridDirection', gridDirectionLabel(energy.grid_power));
  setText('viewerAvailableSurplus', fmtPower(availableSurplus));
  setText('viewerInverterTemperature', fmtCelsius(energy.temperature));
  setText('viewerGoodweUpdate', fmtDateTime(energy.last_update));

  const status = document.getElementById('viewerGoodweStatus');
  if (status) {
    status.textContent = energy.online ? '🟢 GoodWe Online' : '🔴 GoodWe Offline';
    status.className = 'smart-loads ' + (energy.online ? 'enabled' : 'disabled');
  }
}

function renderPool(pool, temperatures) {
  const pump = pool.pump || {};
  const [pumpLabel, pumpClass] = relayLabel(pump.active);
  const pumpState = document.getElementById('viewerPumpState');
  if (pumpState) {
    pumpState.textContent = pumpLabel;
    pumpState.className = 'state ' + pumpClass;
  }
  setText('viewerPumpMode', (pump.mode || 'auto').toUpperCase());
  setText('viewerPumpInfo', `Spegnimento previsto: ${pump.computed_stop_time || '--:--'} · rimanenti ${fmtSec(pump.remaining_seconds)}`);
  const bar = document.getElementById('viewerPumpBar');
  if (bar) bar.style.width = pumpProgress(pump) + '%';

  const [heaterLabel, heaterClass] = relayLabel(pool.heater_active);
  const heaterState = document.getElementById('viewerHeaterState');
  if (heaterState) {
    heaterState.textContent = heaterLabel;
    heaterState.className = 'state ' + heaterClass;
  }

  const sensors = (temperatures || {}).sensors || {};
  renderTemperatureValue('viewerWaterTemperature', sensors.water);
  setText('viewerTemperatureLastUpdate', fmtTime((temperatures || {}).last_update));

  const comm = document.getElementById('viewerTemperatureCommunication');
  const status = ((temperatures || {}).communication || {}).status;
  if (!comm) return;
  if (status === 'online') {
    comm.textContent = '🟢 Termometro online';
    comm.className = 'pill ok';
  } else if (status === 'partial') {
    comm.textContent = '🟡 Un termometro offline';
    comm.className = 'pill warn';
  } else if (status === 'offline') {
    comm.textContent = '🔴 Termometro offline';
    comm.className = 'pill bad';
  } else {
    comm.textContent = 'Caricamento...';
    comm.className = 'pill gray';
  }
}

function fmtWeatherMetric(metric) {
  const number = numericValue(metric && metric.value);
  if (number === null) return '--';
  const unit = metric.unit || '';
  return number.toFixed(1) + (unit ? ` ${unit}` : '');
}

function weatherMetricLabel(key, metric) {
  const labels = { temperature: 'Temperatura', humidity: 'Umidità' };
  return labels[key] || (metric && metric.label) || key;
}

function renderWeather(weather) {
  const root = document.getElementById('viewerWeatherLocations');
  if (root) {
    root.innerHTML = Object.entries((weather || {}).locations || {}).map(([locationKey, location]) => {
      const metrics = Object.entries((location || {}).metrics || {}).map(([metricKey, metric]) => {
        const offline = !metric.online && !metric.placeholder;
        const value = metric.placeholder ? '--' : (offline ? 'OFFLINE' : fmtWeatherMetric(metric));
        const cls = offline ? ' class="offline"' : '';
        return `<div class="weather-metric"><span>${esc(weatherMetricLabel(metricKey, metric))}</span><strong${cls}>${esc(value)}</strong></div>`;
      }).join('');
      return `<section class="weather-location"><h3>${esc('Località: ' + (location.label || locationKey))}</h3><div class="weather-metrics">${metrics}</div></section>`;
    }).join('');
  }
  setText('viewerWeatherLastUpdate', fmtTime((weather || {}).last_update));
  const comm = document.getElementById('viewerWeatherCommunication');
  if (!comm) return;
  const status = (((weather || {}).communication) || {}).status;
  if (status === 'online') {
    comm.textContent = '🟢 Sensori meteo online';
    comm.className = 'pill ok';
  } else if (status === 'partial') {
    comm.textContent = '🟡 Alcuni sensori meteo offline';
    comm.className = 'pill warn';
  } else if (status === 'offline') {
    comm.textContent = '🔴 Sensori meteo offline';
    comm.className = 'pill bad';
  } else {
    comm.textContent = 'Nessun sensore meteo configurato';
    comm.className = 'pill gray';
  }
}

function renderSolar(solar) {
  const enabled = !!solar.enabled;
  const state = document.getElementById('viewerSolarHeatingState');
  if (state) {
    state.textContent = solarStatusLabel(solar.status_label || solar.status, enabled);
    state.className = 'pill solar-status-badge ' + (enabled ? 'ok' : 'gray');
  }
  setText('viewerSolarHeatingMessage', solarMessage(solar));
  setText('viewerSolarWaterTemperature', fmtCelsius(solar.water_temperature));
  setText('viewerSolarThreshold', fmtCelsius(solar.water_temperature_threshold));
  setText('viewerSolarOperatingTime', `${solar.operating_start_time || '--:--'} → ${solar.operating_stop_time || '--:--'}`);
  setText('viewerSolarHeatingToday', fmtSec(solar.heating_today_seconds));
  setText('viewerSolarNextCheck', fmtTime(solar.next_temperature_check));
}

function renderGarden(garden) {
  const [label, cls] = relayLabel((garden || {}).lights_active);
  const el = document.getElementById('viewerGardenLights');
  if (el) {
    el.textContent = label;
    el.className = 'state ' + cls;
  }
}

function renderStatistics(statistics) {
  const root = document.getElementById('viewerStatisticsCards');
  if (!root) return;
  root.innerHTML = '';
  for (const stat of Object.values(statistics || {})) {
    const el = document.createElement('section');
    el.className = 'card stat-card viewer-stat-card';
    el.innerHTML = `<div class="relay-title"><span class="icon">${esc(stat.icon || '🔌')}</span><div><h2>${esc(stat.name || 'Relay')}</h2><div class="muted">${esc(stat.device_name || '')} · relè ${esc(stat.relay || '')}</div></div></div><div class="stat-grid"><div><span>Oggi</span><strong>${esc(fmtSec(stat.daily_seconds))}</strong></div><div><span>Stagione</span><strong>${esc(fmtSec(stat.seasonal_seconds))}</strong></div><div><span>Totale</span><strong>${esc(fmtSec(stat.total_seconds))}</strong></div><div><span>Avvii</span><strong>${esc(stat.starts || 0)}</strong></div></div>`;
    root.appendChild(el);
  }
}

function renderGeneral(general) {
  setText('viewerVersion', general.version || '--');
  setText('viewerCurrentTime', fmtDateTime(general.current_time));
  setText('viewerLastDashboardUpdate', fmtDateTime(general.last_dashboard_update || general.last_successful_refresh));
}

function renderViewer(data) {
  const refreshSeconds = Number(data.refresh_seconds || 30);
  setText('viewerRefreshLine', `Aggiornamento automatico ogni ${refreshSeconds} secondi`);
  renderEnergy(data.energy || {});
  renderPool(data.pool || {}, data.temperatures || {});
  renderWeather(data.weather || {});
  renderSolar(data.solar_heating || {});
  renderGarden(data.garden || {});
  renderStatistics(data.statistics || {});
  renderGeneral(data.general || {});

  const status = document.getElementById('viewerStatus');
  if (status) {
    status.textContent = 'Online';
    status.className = 'pill ok';
  }

  const nextRefreshMs = Math.max(5000, refreshSeconds * 1000);
  if (nextRefreshMs !== viewerRefreshMs || !viewerRefreshTimer) {
    viewerRefreshMs = nextRefreshMs;
    if (viewerRefreshTimer) clearInterval(viewerRefreshTimer);
    viewerRefreshTimer = setInterval(refreshViewer, viewerRefreshMs);
  }
}

async function refreshViewer() {
  try {
    renderViewer(await viewerApi());
  } catch (error) {
    const status = document.getElementById('viewerStatus');
    if (status) {
      status.textContent = error.message || 'Errore';
      status.className = 'pill bad';
    }
  }
}

applyViewerTokenLinks();
refreshViewer();
