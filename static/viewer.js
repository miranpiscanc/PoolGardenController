let viewerRefreshTimer = null;
let viewerRefreshMs = 60000;
let viewerCountdownTimer = null;
let viewerRefreshDeadline = Date.now() + viewerRefreshMs;
const houseUI = window.MeMHouseUI;

function renderViewerCountdown() {
  const seconds = Math.max(0, Math.ceil((viewerRefreshDeadline - Date.now()) / 1000));
  setText('viewerRefreshCountdown', `Prossimo aggiornamento tra ${seconds} s`);
}

function resetViewerCountdown() {
  viewerRefreshDeadline = Date.now() + viewerRefreshMs;
  renderViewerCountdown();
  if (!viewerCountdownTimer) viewerCountdownTimer = setInterval(renderViewerCountdown, 1000);
}

function scheduleNextViewerRefresh() {
  if (viewerRefreshTimer) clearTimeout(viewerRefreshTimer);
  resetViewerCountdown();
  viewerRefreshTimer = setTimeout(async () => {
    viewerRefreshTimer = null;
    viewerRefreshDeadline = Date.now();
    renderViewerCountdown();
    try {
      await refreshViewer();
    } finally {
      scheduleNextViewerRefresh();
    }
  }, viewerRefreshMs);
}

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

async function viewerIntesisApi() {
  const response = await fetch('/api/intesis/status', { cache: 'no-store' });
  if (!response.ok) throw new Error('Stato Intesis non disponibile');
  return response.json();
}

function esc(value) {
  return String(value === null || value === undefined ? '' : value).replace(/[&<>"']/g, c => ({
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

function setPortalBadge(id, label, cls) {
  const el = document.getElementById(id);
  if (!el) return;
  el.textContent = label;
  el.className = 'pill ' + cls;
}

function safeRender(label, fn) {
  try {
    fn();
  } catch (error) {
    console.error(`Viewer binding failed: ${label}`, error);
  }
}

function portalSection() {
  return String(window.location.hash || '').replace('#', '') || 'home';
}

function renderPortalRoute() {
  const section = portalSection();
  const pageMap = {
    netatmo: 'pageNetatmo',
    weather: 'pageWeather',
    'air-conditioning': 'pageAirConditioning',
    ...(houseUI.isCesclans() ? { pool: 'pagePool', energy: 'pageEnergy' } : {}),
    report: 'pageReport'
  };
  const activePage = pageMap[section];
  const home = document.getElementById('portalHome');
  if (home) home.classList.toggle('hidden', !!activePage);
  document.querySelectorAll('.portal-page').forEach(page => {
    page.classList.toggle('hidden', page.id !== activePage);
  });
  document.querySelectorAll('.portal-section-nav .button-link').forEach(link => {
    const target = (link.getAttribute('href') || '#').replace('#', '') || 'home';
    link.classList.toggle('active', target === section);
  });
}

function renderPortalSummary(data) {
  const netatmo = data.netatmo || {};
  const homes = Object.keys(netatmo.homes || {}).length;
  setText('portalNetatmoSummary', homes ? `${homes} case · ${fmtTime(netatmo.last_successful_poll)}` : 'Nessuna casa Netatmo');
  if (netatmo.online || netatmo.status === 'online') setPortalBadge('portalNetatmoBadge', '🟢 Netatmo online', 'ok');
  else if (netatmo.authorization_required) setPortalBadge('portalNetatmoBadge', '🟡 Autorizzazione richiesta', 'warn');
  else setPortalBadge('portalNetatmoBadge', '🔴 Netatmo offline', 'bad');

  const pump = ((data.pool || {}).pump) || {};
  const solar = data.solar_heating || {};
  const pumpLabel = pump.active === true ? 'ACCESO' : pump.active === false ? 'SPENTO' : 'SINCRONIZZAZIONE';
  setText('portalPoolSummary', `Pompa ${pumpLabel} · acqua ${fmtCelsius(solar.water_temperature)}`);
  setPortalBadge('portalPoolBadge', pumpLabel, pump.active === true ? 'ok' : pump.active === false ? 'gray' : 'warn');

  const energy = data.energy || {};
  const pvProduction = numericValue(energy.pv_production);
  const houseConsumption = numericValue(energy.house_consumption);
  const availableSurplus = energy.available_surplus !== null && energy.available_surplus !== undefined ? energy.available_surplus : (
    pvProduction === null || houseConsumption === null ? null : Math.max(0, pvProduction - houseConsumption)
  );
  setText('portalEnergySummary', `FV ${fmtPower(energy.pv_production)} · surplus ${fmtPower(availableSurplus)}`);
  setPortalBadge('portalEnergyBadge', energy.online ? 'GoodWe online' : 'GoodWe offline', energy.online ? 'ok' : 'bad');

  const weather = data.weather || {};
  const status = (weather.communication || {}).status;
  setText('portalWeatherSummary', `${Object.keys(weather.locations || {}).length} località · ${fmtTime(weather.last_update)}`);
  if (status === 'online') setPortalBadge('portalWeatherBadge', '🟢 Sensori meteo online', 'ok');
  else if (status === 'partial') setPortalBadge('portalWeatherBadge', '🟡 Alcuni sensori offline', 'warn');
  else if (status === 'offline') setPortalBadge('portalWeatherBadge', '🔴 Sensori meteo offline', 'bad');
  else setPortalBadge('portalWeatherBadge', 'Nessun sensore configurato', 'gray');
}

function relayLabel(active) {
  if (active === true) return ['ACCESO', 'on'];
  if (active === false) return ['SPENTO', 'off'];
  return ['SINCRONIZZAZIONE', 'unknown'];
}

function heaterReasonLabel(reason) {
  const labels = {
    heating: '🟢 Riscaldamento',
    waiting_check: '🔵 In attesa del controllo programmato',
    waiting_pv: '🟡 In attesa surplus FV',
    pump_off: '🔵 Pompa piscina OFF',
    disabled: '⚪ Disabilitato',
    communication_error: '🔴 Errore comunicazione'
  };
  return labels[(reason || {}).key] || (reason || {}).label || '--';
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
  const availableSurplus = energy.available_surplus !== null && energy.available_surplus !== undefined ? energy.available_surplus : (
    pvProduction === null || houseConsumption === null ? null : Math.max(0, pvProduction - houseConsumption)
  );
  const batteryPower = energy.pbattery1 !== null && energy.pbattery1 !== undefined ? energy.pbattery1 : energy.battery_power;
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

function renderHeater(heater) {
  const [label, cls] = relayLabel((heater || {}).active);
  const state = document.getElementById('viewerHeaterState');
  if (state) {
    state.textContent = label;
    state.className = 'state ' + cls;
  }
  const reason = document.getElementById('viewerHeaterReason');
  if (reason) {
    const reasonData = (heater || {}).reason || {};
    reason.textContent = heaterReasonLabel(reasonData);
    reason.className = 'heater-reason pill ' + (reasonData.class || 'gray');
  }
  setText('viewerHeaterMode', (heater || {}).mode || '--');
  setText('viewerHeaterLastUpdate', fmtDateTime((heater || {}).last_update));
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

function weatherCodeInfo(code) {
  if (code === 0) return ['☀️', 'Sereno'];
  if ([1, 2, 3].includes(code)) return ['⛅', 'Parzialmente nuvoloso'];
  if ([45, 48].includes(code)) return ['🌫️', 'Nebbia'];
  if ([51, 53, 55, 56, 57].includes(code)) return ['🌦️', 'Pioviggine'];
  if ([61, 63, 65, 66, 67].includes(code)) return ['🌧️', 'Pioggia'];
  if ([71, 73, 75, 77, 85, 86].includes(code)) return ['🌨️', 'Neve'];
  if ([80, 81, 82].includes(code)) return ['🌦️', 'Rovesci'];
  if ([95, 96, 99].includes(code)) return ['⛈️', 'Temporale'];
  return ['🌤️', 'Tempo variabile'];
}

function renderDailyForecast(forecast) {
  if (!forecast || !forecast.online) return '<div class="weather-forecast offline">Previsione non disponibile</div>';
  const info = weatherCodeInfo(Number(forecast.weather_code));
  return `<div class="weather-forecast"><div class="weather-forecast-title">Previsione di oggi</div><div class="weather-forecast-condition"><span>${info[0]}</span><strong>${esc(info[1])}</strong></div><div class="weather-forecast-details"><span>🌡 Min ${Number(forecast.temperature_min).toFixed(1)} °C · Max ${Number(forecast.temperature_max).toFixed(1)} °C</span><span>🌧 Pioggia ${Math.round(Number(forecast.precipitation_probability || 0))} % · ${Number(forecast.precipitation_sum || 0).toFixed(1)} mm</span><span>💨 Vento fino a ${Number(forecast.wind_speed_max || 0).toFixed(1)} km/h</span></div></div>`;
}

function renderWeather(weather) {
  weather = houseUI.weatherForHouse(weather || {});
  const root = document.getElementById('viewerWeatherLocations');
  if (root) {
    root.innerHTML = Object.entries((weather || {}).locations || {}).map(([locationKey, location]) => {
      const metrics = Object.entries((location || {}).metrics || {}).map(([metricKey, metric]) => {
        const offline = !metric.online && !metric.placeholder;
        const value = metric.placeholder ? '--' : (offline ? 'OFFLINE' : fmtWeatherMetric(metric));
        const cls = offline ? ' class="offline"' : '';
        return `<div class="weather-metric"><span>${esc(weatherMetricLabel(metricKey, metric))}</span><strong${cls}>${esc(value)}</strong></div>`;
      }).join('');
      return `<section class="weather-location"><h3>${esc('Località: ' + (location.label || locationKey))}</h3><div class="weather-metrics">${metrics}</div>${renderDailyForecast(location.forecast)}</section>`;
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

function fmtNetatmoMetric(metric) {
  const value = metric && metric.value;
  if (value === null || value === undefined || value === '') return '--';
  const number = numericValue(value);
  const unit = (metric && metric.unit) || '';
  if (number === null) return String(value) + (unit ? ` ${unit}` : '');
  const decimals = unit === 'ppm' || unit === '%' || unit === '°' || unit === 'dB' ? 0 : 1;
  return number.toFixed(decimals) + (unit ? ` ${unit}` : '');
}

function netatmoSlug(value) {
  return String(value || '')
    .normalize('NFD')
    .replace(/[\u0300-\u036f]/g, '')
    .toLowerCase()
    .replace(/[^a-z0-9]+/g, ' ')
    .trim();
}

function netatmoFriendlyDevice(device) {
  const home = netatmoSlug(device.home_name);
  const name = netatmoSlug(device.name);
  const type = String(device.type || '');
  const friendly = {
    opicina: {
      salon: ['🛋️', 'Soggiorno'],
      soggiorno: ['🛋️', 'Soggiorno'],
      spalnica: ['🛏️', 'Camera matrimoniale'],
      grascina: ['👧', 'Camera dei bambini'],
      balkon: ['🌳', 'Esterno'],
      outdoor: ['🌳', 'Esterno'],
      pluviometro: ['🌧️', 'Pluviometro'],
      rain: ['🌧️', 'Pluviometro'],
      anemometro: ['💨', 'Anemometro'],
      wind: ['💨', 'Anemometro']
    },
    cesclans: {
      spalnica: ['🛏️', 'Camera matrimoniale'],
      'soba otroci': ['👦', 'Camera dei bambini'],
      terasa: ['🌳', 'Esterno'],
      outdoor: ['🌳', 'Esterno'],
      pluviometro: ['🌧️', 'Pluviometro'],
      rain: ['🌧️', 'Pluviometro']
    }
  };
  const byHome = friendly[home] || {};
  for (const [key, value] of Object.entries(byHome)) {
    if (name.includes(key)) return { icon: value[0], label: value[1] };
  }
  if (type === 'NAModule1') return { icon: '🌳', label: 'Esterno' };
  if (type === 'NAModule3') return { icon: '🌧️', label: 'Pluviometro' };
  if (type === 'NAModule2') return { icon: '💨', label: 'Anemometro' };
  if (name.includes('bed') || name.includes('spalnica')) return { icon: '🛏️', label: 'Camera da letto' };
  if (name.includes('child') || name.includes('otrok') || name.includes('otroci')) return { icon: '👧', label: 'Camera dei bambini' };
  if (type === 'NAMain') return { icon: '🛋️', label: 'Soggiorno' };
  return { icon: '🏠', label: device.name || 'Dispositivo Netatmo' };
}

function netatmoMetricInfo(key) {
  return {
    temperature: ['🌡', 'Temperatura'],
    humidity: ['💧', 'Umidità'],
    co2: ['🫁', 'CO₂'],
    pressure: ['🌡', 'Pressione'],
    noise: ['🌬', 'Rumore'],
    rain_today: ['🌧️', 'Pioggia oggi'],
    wind_speed: ['💨', 'Velocità del vento'],
    gust: ['💨', 'Raffica di vento'],
    direction: ['🧭', 'Direzione del vento']
  }[key] || ['•', key];
}

function netatmoBattery(device) {
  const value = numericValue(device && device.battery);
  if (value === null || value < 0 || value > 100) return null;
  if (value > 70) return { label: `🟢 ${Math.round(value)} %`, cls: 'ok' };
  if (value >= 40) return { label: `🟡 ${Math.round(value)} %`, cls: 'warn' };
  if (value >= 20) return { label: `🟠 ${Math.round(value)} %`, cls: 'orange' };
  return { label: `🔴 ${Math.round(value)} %`, cls: 'bad' };
}

function netatmoDevicesForHome(data, home) {
  const stations = data.stations || {};
  const modules = data.modules || {};
  const devices = [];
  (home.stations || []).forEach(stationId => {
    const station = stations[stationId];
    if (!station) return;
    devices.push(station);
    (station.modules || []).forEach(moduleId => {
      if (modules[moduleId]) devices.push(modules[moduleId]);
    });
  });
  return devices;
}

function netatmoDeviceMetric(device, keys) {
  for (const key of keys) {
    const metric = ((device || {}).metrics || {})[key];
    if (metric && metric.value !== null && metric.value !== undefined && metric.value !== '') return metric;
  }
  return null;
}

function netatmoLatestUpdate(devices, fallback) {
  let latest = fallback || null;
  let latestMs = latest ? new Date(latest).getTime() : 0;
  devices.forEach(device => {
    const stamp = device.last_update || device.updated_at;
    const ms = stamp ? new Date(stamp).getTime() : 0;
    if (Number.isFinite(ms) && ms > latestMs) {
      latestMs = ms;
      latest = stamp;
    }
  });
  return latest;
}

function renderNetatmoSummaryCard(home, devices, data) {
  const homeName = home.name || 'Casa Netatmo';
  const onlineCount = devices.filter(device => device.online !== false).length;
  const indoor = devices.find(device => device.type !== 'NAModule1' && device.type !== 'NAModule2' && device.type !== 'NAModule3' && netatmoDeviceMetric(device, ['temperature']));
  const outdoor = devices.find(device => device.type === 'NAModule1' && netatmoDeviceMetric(device, ['temperature']));
  const wind = devices.find(device => netatmoDeviceMetric(device, ['wind_speed']));
  const rain = devices.find(device => netatmoDeviceMetric(device, ['rain_today']));
  const lastUpdate = netatmoLatestUpdate(devices, data.last_successful_poll);
  const homeIcon = netatmoSlug(homeName).includes('cesclans') ? '🏡' : '🏠';
  return `
    <section class="netatmo-summary-card">
      <h3>${homeIcon} ${esc(homeName)}</h3>
      <div class="netatmo-summary-lines">
        <span>🟢 ${esc(onlineCount)} dispositivi online</span>
        <span>🌡 Interno ${esc(fmtNetatmoMetric(netatmoDeviceMetric(indoor, ['temperature'])))}</span>
        <span>🌳 Esterno ${esc(fmtNetatmoMetric(netatmoDeviceMetric(outdoor, ['temperature'])))}</span>
        ${wind ? `<span>💨 Vento ${esc(fmtNetatmoMetric(netatmoDeviceMetric(wind, ['wind_speed'])))}</span>` : ''}
        ${rain ? `<span>🌧 Pioggia oggi ${esc(fmtNetatmoMetric(netatmoDeviceMetric(rain, ['rain_today'])))}</span>` : ''}
        <span>Ultimo aggiornamento ${esc(fmtTime(lastUpdate))}</span>
      </div>
    </section>`;
}

function renderNetatmoDeviceCard(device) {
  const friendly = netatmoFriendlyDevice(device);
  const metrics = ['temperature', 'humidity', 'co2', 'pressure', 'noise', 'rain_today', 'wind_speed', 'gust', 'direction']
    .map(key => {
      const metric = ((device || {}).metrics || {})[key];
      if (!metric || metric.value === null || metric.value === undefined || metric.value === '') return '';
      const info = netatmoMetricInfo(key);
      const rainAge = key === 'rain_today'
        ? `<div class="netatmo-device-metric"><span>Giorni dall'ultima giornata di pioggia</span><strong>${device.days_since_last_rain === null || device.days_since_last_rain === undefined ? 'Dato non ancora disponibile' : esc(device.days_since_last_rain)}</strong></div>`
        : '';
      return `<div class="netatmo-device-metric"><span>${esc(info[0])} ${esc(info[1])}</span><strong>${esc(fmtNetatmoMetric(metric))}</strong></div>${rainAge}`;
    })
    .join('');
  const battery = netatmoBattery(device);
  const rawName = device.name || '';
  const subtitle = rawName && rawName !== friendly.label
    ? `<div class="netatmo-device-subtitle">Modulo Netatmo: ${esc(rawName)}</div>`
    : '';
  return `
    <article class="netatmo-device-card">
      <div class="netatmo-device-head">
        <div>
          <h4>${esc(friendly.icon)} ${esc(friendly.label)}</h4>
          ${subtitle}
        </div>
        <span class="pill ${device.online === false ? 'bad' : 'ok'}">${device.online === false ? '🔴 Offline' : '🟢 Online'}</span>
      </div>
      <div class="netatmo-device-metrics">
        ${metrics || '<div class="netatmo-device-metric"><span>Nessun valore</span><strong>--</strong></div>'}
        ${battery ? `<div class="netatmo-device-metric"><span>🔋 Batteria</span><strong class="pill ${esc(battery.cls)}">${esc(battery.label)}</strong></div>` : ''}
      </div>
      <div class="netatmo-device-footer">Ultimo aggiornamento: ${esc(fmtTime(device.last_update || device.updated_at))}</div>
    </article>`;
}

function renderNetatmo(netatmo) {
  netatmo = houseUI.netatmoForHouse(netatmo || {});
  const root = document.getElementById('viewerNetatmoLocations');
  if (root) {
    const homes = Object.values((netatmo || {}).homes || {});
    const summary = homes.map(home => renderNetatmoSummaryCard(home, netatmoDevicesForHome(netatmo || {}, home), netatmo || {})).join('');
    const details = homes.map(home => {
      const devices = netatmoDevicesForHome(netatmo || {}, home);
      const homeIcon = netatmoSlug(home.name).includes('cesclans') ? '🏡' : '🏠';
      return `
        <section class="netatmo-home-column">
          <h3>${homeIcon} ${esc(String(home.name || 'Casa Netatmo').toUpperCase())}</h3>
          <div class="netatmo-device-grid">${devices.map(renderNetatmoDeviceCard).join('')}</div>
        </section>`;
    }).join('');
    root.innerHTML = homes.length
      ? `<div class="netatmo-summary-grid">${summary}</div><div class="netatmo-home-grid">${details}</div>`
      : '<section class="weather-location"><h3>Nessuna casa Netatmo</h3></section>';
  }
  setText('viewerNetatmoLastUpdate', fmtTime((netatmo || {}).last_successful_poll));
  const comm = document.getElementById('viewerNetatmoCommunication');
  if (!comm) return;
  if ((netatmo || {}).online || (netatmo || {}).status === 'online') {
    comm.textContent = '🟢 Netatmo online';
    comm.className = 'pill ok';
  } else if ((netatmo || {}).authorization_required) {
    comm.textContent = '🟡 Autorizzazione Netatmo richiesta';
    comm.className = 'pill warn';
  } else if ((netatmo || {}).enabled === false) {
    comm.textContent = 'Netatmo disabilitato';
    comm.className = 'pill gray';
  } else {
    comm.textContent = '🔴 Netatmo offline';
    comm.className = 'pill bad';
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

function renderViewerIntesis(devices) {
  const scoped = houseUI.intesisForHouse(devices);
  const root = document.getElementById('viewerAirDevices');
  if (root) {
    root.innerHTML = scoped.map(device => `
      <article class="card air-device-card ${device.power === true ? 'air-device-on' : 'air-device-off'}">
        <div class="air-device-head"><h3>${esc(device.name || 'Unità Intesis')}</h3><span class="pill ${device.power === true ? 'ok' : 'gray'}">${device.power === true ? 'ON' : device.power === false ? 'OFF' : '--'}</span></div>
        <div class="air-room-temperature"><span>Temperatura ambiente</span><strong>${esc(fmtCelsius(device.room_temperature))}</strong></div>
        <div class="air-metrics">
          <div class="air-metric"><span>Modalità</span><strong>${esc(houseUI.intesisControlLabel('mode', device.mode))}</strong></div>
          <div class="air-metric"><span>Temperatura impostata</span><strong>${esc(fmtCelsius(device.target_temperature))}</strong></div>
          <div class="air-metric"><span>Ventola</span><strong>${esc(houseUI.intesisControlLabel('fan', device.fan_speed))}</strong></div>
          <div class="air-metric"><span>Ultimo aggiornamento</span><strong>${esc(fmtTime(device.last_update))}</strong></div>
        </div>
      </article>`).join('');
  }
  setText('viewerAirSummary', `${scoped.length} unità · ${scoped.filter(device => device.power === true).length} ON`);
  setPortalBadge('viewerAirBadge', scoped.length ? 'Intesis disponibile' : 'Nessuna unità', scoped.length ? 'ok' : 'gray');
  const status = document.getElementById('viewerAirStatus');
  if (status) {
    status.textContent = scoped.length ? `${scoped.length} unità` : 'Nessuna unità';
    status.className = `pill ${scoped.length ? 'ok' : 'gray'}`;
  }
}

function renderViewer(data) {
  data = {
    ...data,
    weather: houseUI.weatherForHouse(data.weather || {}),
    netatmo: houseUI.netatmoForHouse(data.netatmo || {})
  };
  const refreshSeconds = Number(data.refresh_seconds || 60);
  safeRender('refresh line', () => setText('viewerRefreshLine', `Aggiornamento automatico ogni ${refreshSeconds} secondi`));
  if (houseUI.isCesclans()) safeRender('energy', () => renderEnergy(data.energy || {}));
  if (houseUI.isCesclans()) safeRender('pool', () => renderPool(data.pool || {}, data.temperatures || {}));
  if (houseUI.isCesclans()) safeRender('heater', () => renderHeater(data.heater || {}));
  safeRender('weather', () => renderWeather(data.weather || {}));
  safeRender('netatmo', () => renderNetatmo(data.netatmo || {}));
  safeRender('home summary', () => renderPortalSummary(data));
  safeRender('portal route', renderPortalRoute);
  if (houseUI.isCesclans()) safeRender('solar', () => renderSolar(data.solar_heating || {}));
  if (houseUI.isCesclans()) safeRender('garden', () => renderGarden(data.garden || {}));
  if (houseUI.isCesclans()) safeRender('statistics', () => renderStatistics(data.statistics || {}));
  safeRender('general', () => renderGeneral(data.general || {}));
  safeRender('viewer status', () => {
    const status = document.getElementById('viewerStatus');
    if (status) {
      status.textContent = 'Online';
      status.className = 'pill ok';
    }
  });
  safeRender('viewer timer', () => {
    const nextRefreshMs = Math.max(5000, refreshSeconds * 1000);
    if (nextRefreshMs !== viewerRefreshMs) {
      viewerRefreshMs = nextRefreshMs;
    }
  });
}

async function refreshViewer() {
  try {
    renderViewer(await viewerApi());
    try {
      renderViewerIntesis(await viewerIntesisApi());
    } catch (intesisError) {
      console.error('Viewer Intesis refresh failed', intesisError);
      renderViewerIntesis([]);
    }
  } catch (error) {
    const status = document.getElementById('viewerStatus');
    if (status) {
      status.textContent = error.message || 'Errore';
      status.className = 'pill bad';
    }
  }
}

applyViewerTokenLinks();
renderPortalRoute();
window.addEventListener('hashchange', renderPortalRoute);
refreshViewer().then(scheduleNextViewerRefresh);
