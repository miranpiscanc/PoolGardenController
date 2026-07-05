let lastStatus = null;
let refreshTimer = null;
let refreshMs = 4000;
let currentLanguage = 'en';
let translations = {};
let availableLanguages = {};
const relayCache = {};
const relayStatusCache = {};
const pumpDisplayCache = { info: null, bar: null };

function getPath(source, path) {
  return String(path).split('.').reduce((value, key) => (
    value && Object.prototype.hasOwnProperty.call(value, key) ? value[key] : undefined
  ), source);
}

function t(path, vars = {}) {
  let value = getPath(translations, path);
  if (value === undefined || value === null) value = path;
  value = String(value);
  for (const [key, replacement] of Object.entries(vars)) {
    value = value.replaceAll(`{${key}}`, replacement);
  }
  return value;
}

function optionalT(path) {
  const value = getPath(translations, path);
  return value === undefined || value === null ? null : String(value);
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

function applyTranslations() {
  document.documentElement.lang = currentLanguage;
  document.querySelectorAll('[data-i18n]').forEach(el => {
    el.textContent = t(el.dataset.i18n);
  });
  const titleKey = document.querySelector('title')?.dataset.i18n;
  if (titleKey) document.title = t(titleKey);
  renderLanguageSelector();
}

function renderLanguageSelector() {
  const root = document.getElementById('languageSelector');
  if (!root) return;
  root.innerHTML = Object.values(availableLanguages).map(lang => {
    const active = lang.code === currentLanguage ? ' active' : '';
    return `<button class="language-option${active}" type="button" onclick="changeLanguage('${esc(lang.code)}')" title="${esc(lang.nativeName)}">${esc(lang.flag)}<span>${esc(lang.code.toUpperCase())}</span></button>`;
  }).join('');
}

async function loadI18n() {
  const data = await api('/api/i18n');
  currentLanguage = data.language;
  translations = data.translations || {};
  availableLanguages = data.languages || {};
  applyTranslations();
}

async function changeLanguage(language) {
  const data = await api('/api/language', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ language })
  });
  currentLanguage = data.language;
  translations = data.translations || {};
  availableLanguages = data.languages || {};
  pumpDisplayCache.info = null;
  applyTranslations();
  await refreshCurrentPage();
}

function fmtSec(s) {
  s = Math.max(0, Number(s || 0));
  const h = Math.floor(s / 3600);
  const m = Math.floor((s % 3600) / 60);
  return t('time.hoursMinutes', { hours: h, minutes: String(m).padStart(2, '0') });
}

function locale() {
  return getPath(translations, 'meta.locale') || currentLanguage;
}

function fmtDateTime(value) {
  if (!value) return '-';
  const d = new Date(value);
  if (Number.isNaN(d.getTime())) return value;
  return d.toLocaleString(locale(), {
    day: '2-digit',
    month: '2-digit',
    year: 'numeric',
    hour: '2-digit',
    minute: '2-digit',
    second: '2-digit'
  });
}

function fmtTime(value) {
  if (!value) return '--:--:--';
  const d = new Date(value);
  if (Number.isNaN(d.getTime())) {
    const parts = String(value).split('T');
    return (parts[1] || value || '--:--:--').slice(0, 8);
  }
  return d.toLocaleTimeString(locale(), { hour: '2-digit', minute: '2-digit', second: '2-digit' });
}

function numericValue(value) {
  if (value === null || value === undefined || value === '') return null;
  const number = Number(value);
  return Number.isFinite(number) ? number : null;
}

function fmtPower(value) {
  const number = numericValue(value);
  if (number === null) return '--';
  const sign = number < 0 ? '−' : '';
  const absolute = Math.abs(number);
  if (absolute < 1000) return sign + Math.round(absolute) + ' W';
  return sign + (absolute / 1000).toFixed(2) + ' kW';
}

function fmtPercent(value) {
  const number = numericValue(value);
  return number === null ? '--' : Math.round(number) + ' %';
}

function fmtCelsius(value) {
  const number = numericValue(value);
  return number === null ? '--' : number.toFixed(1) + ' °C';
}

function setText(id, value) {
  const el = document.getElementById(id);
  if (el) el.textContent = value;
}

function setLastRefresh(value) {
  const el = document.getElementById('lastRefresh');
  if (el) el.textContent = fmtDateTime(value);
}

function setAppMeta(s) {
  const el = document.getElementById('appMeta');
  if (!el) return;
  const appInfo = s.app || {};
  el.textContent = t('app.meta', { version: appInfo.version || s.version || '', port: appInfo.port || s.port || '' });
}

function relayLabel(key, active) {
  if (active === true) {
    relayCache[key] = true;
    return [t('status.on'), 'on'];
  }
  if (active === false) {
    relayCache[key] = false;
    return [t('status.off'), 'off'];
  }
  if (key in relayCache) {
    return relayCache[key] ? [t('status.on'), 'on'] : [t('status.off'), 'off'];
  }
  return [t('status.syncing'), 'unknown'];
}

function confirmedRelay(active) {
  return active === true || active === false;
}

function relayStatusForDisplay(key, status) {
  const current = status || {};
  if (confirmedRelay(current.active)) {
    relayStatusCache[key] = { ...current };
    return current;
  }
  const cached = relayStatusCache[key];
  if (!cached) return current;
  return {
    ...current,
    active: cached.active,
    response: current.response || cached.response,
    elapsed_ms: current.elapsed_ms || cached.elapsed_ms
  };
}

function pumpInfoText(pump, s) {
  const flags = [];
  if (s.runtime.scheduler.manual_run_until) flags.push(t('pump.manualTimerActive'));
  if (s.runtime.scheduler.auto_suspended_date) flags.push(t('pump.autoSuspendedToday'));
  const suffix = flags.length ? ` · ${flags.join(' · ')}` : '';
  return t('pump.info', {
    stopTime: pump.computed_stop_time || '--:--',
    remaining: fmtSec(pump.remaining_seconds)
  }) + suffix;
}

function pumpProgress(pump) {
  if (pump.active === true && pump.duration_hours) {
    const perc = 100 - (pump.remaining_seconds / (pump.duration_hours * 3600) * 100);
    return Math.min(100, Math.max(0, perc));
  }
  return 0;
}

async function api(url, options) {
  const r = await fetch(url, options);
  return await r.json();
}

function scheduleRefresh(s) {
  const appConfig = (s.config && s.config.app) || {};
  const safeStop = ((s.runtime || {}).heater_safe_stop) || {};
  const heaterRunning = !!((s.heater_statistics || {}).active_start);
  const nextRefreshMs = (safeStop.running || safeStop.state === 'COMPLETED' || heaterRunning) ? 1000 : Number(appConfig.poll_seconds || 30) * 1000;
  if (nextRefreshMs !== refreshMs || !refreshTimer) {
    refreshMs = nextRefreshMs;
    if (refreshTimer) clearInterval(refreshTimer);
    refreshTimer = setInterval(refreshCurrentPage, refreshMs);
  }
}

async function refresh() {
  try {
    const s = await api('/api/status');
    lastStatus = s;
    scheduleRefresh(s);
    setAppMeta(s);
    setLastRefresh((s.runtime || {}).last_poll);
    const devices = s.devices || {};
    const allOk = Object.values(devices).length && Object.values(devices).every(d => d.ok);
    const gs = document.getElementById('globalStatus');
    gs.textContent = allOk ? t('status.online') : t('status.checkNetwork');
    gs.className = 'pill ' + (allOk ? 'ok' : 'bad');
    const pump = s.pump;
    const startTimeField = document.getElementById('startTime');
    if (document.activeElement !== startTimeField) startTimeField.value = pump.start_time || '09:00';
    const durationField = document.getElementById('duration');
    if (document.activeElement !== durationField) durationField.value = pump.duration_hours || 6;
    document.getElementById('mode').value = pump.mode || 'auto';
    document.getElementById('pumpMode').textContent = t(`pump.modeBadge.${pump.mode || 'auto'}`);
    const pumpKey = `${pump.device}:${pump.relay}`;
    const [pl, pc] = relayLabel(pumpKey, pump.active);
    const ps = document.getElementById('pumpState');
    ps.textContent = pl;
    ps.className = 'state ' + pc;
    if (confirmedRelay(pump.active) || !pumpDisplayCache.info) pumpDisplayCache.info = pumpInfoText(pump, s);
    document.getElementById('pumpInfo').textContent = pumpDisplayCache.info;
    if (confirmedRelay(pump.active) || pumpDisplayCache.bar === null) pumpDisplayCache.bar = pumpProgress(pump);
    document.getElementById('bar').style.width = pumpDisplayCache.bar + '%';
    renderTemperatures(s);
    renderSolarHeating(s);
    renderGoodWeDashboard(s);
    renderTelegramNotifications(s);
    renderCards(s);
    renderDiag(s);
  } catch (e) {
    const gs = document.getElementById('globalStatus');
    gs.textContent = t('status.serverUnreachable');
    gs.className = 'pill bad';
  }
}

function renderTemperatureValue(id, sensor) {
  const el = document.getElementById(id);
  if (!el || !sensor) return;
  if (sensor.online) {
    el.textContent = Number(sensor.value).toFixed(1) + ' °C';
    el.className = 'temperature-value';
    return;
  }
  el.textContent = t('status.offline');
  el.className = 'temperature-value offline';
}

function renderTemperatures(s) {
  const tData = s.temperatures || {};
  const sensors = tData.sensors || {};
  renderTemperatureValue('waterTemperature', sensors.water);
  renderTemperatureValue('outsideTemperature', sensors.outside);
  const last = document.getElementById('temperatureLastUpdate');
  if (last) last.textContent = fmtTime(tData.last_update);
  const comm = document.getElementById('temperatureCommunication');
  if (!comm) return;
  const status = (tData.communication || {}).status;
  if (status === 'online') {
    comm.textContent = t('temperature.status.online');
    comm.className = 'pill ok';
  } else if (status === 'partial') {
    comm.textContent = t('temperature.status.partial');
    comm.className = 'pill warn';
  } else if (status === 'offline') {
    comm.textContent = t('temperature.status.offline');
    comm.className = 'pill bad';
  } else {
    comm.textContent = t('common.loading');
    comm.className = 'pill gray';
  }
}

function translatedSolarStatus(status, enabled) {
  const key = String(status || (enabled ? 'ACTIVE' : 'DISABLED')).toLowerCase();
  return t(`solar.status.${key}`);
}

function translatedSolarMessage(message, solar, enabled) {
  if (solar.status_message_key) {
    return t(solar.status_message_key, solar.status_message_vars || {});
  }
  const mapped = optionalT(`solar.messageMap.${message}`);
  if (mapped) return mapped;
  return enabled ? t('solar.message.waitingForStart', { time: solar.start_time || '08:00' }) : t('solar.message.disabled');
}

function renderSolarHeating(s) {
  const solar = s.solar_heating || {};
  const enabled = !!solar.enabled;
  const state = document.getElementById('solarHeatingState');
  if (state) {
    state.textContent = translatedSolarStatus(solar.status_label || solar.status, enabled);
    state.className = 'pill solar-status-badge ' + (enabled ? 'ok' : 'gray');
  }
  const message = document.getElementById('solarHeatingMessage');
  if (message) message.textContent = translatedSolarMessage(solar.status_message, solar, enabled);
  const toggle = document.getElementById('solarHeatingToggle');
  if (toggle) {
    toggle.textContent = enabled ? t('solar.disable') : t('solar.enable');
    toggle.className = enabled ? 'off' : 'on';
  }
  const water = document.getElementById('solarWaterTemperature');
  if (water) water.textContent = solar.water_temperature === null || solar.water_temperature === undefined ? '--' : Number(solar.water_temperature).toFixed(1) + ' °C';
  const thresholdDisplay = document.getElementById('solarThresholdDisplay');
  if (thresholdDisplay) thresholdDisplay.textContent = Number(solar.water_temperature_threshold || 29).toFixed(1) + ' °C';
  const operating = document.getElementById('solarOperatingTime');
  if (operating) operating.textContent = `${solar.operating_start_time || solar.start_time || '08:00'} → ${solar.operating_stop_time || solar.forced_stop_time || '17:00'}`;
  const heatingToday = document.getElementById('solarHeatingToday');
  if (heatingToday) heatingToday.textContent = fmtSec((s.heater_statistics || {}).daily_seconds);
  const nextTemperatureCheck = document.getElementById('solarNextTemperatureCheck');
  if (nextTemperatureCheck) nextTemperatureCheck.textContent = fmtTime((solar.runtime || {}).next_temperature_check_at).slice(0, 5);
  const threshold = document.getElementById('solarThreshold');
  if (threshold && document.activeElement !== threshold) threshold.value = Number(solar.water_temperature_threshold || 29).toFixed(1);
  const start = document.getElementById('solarStartTime');
  if (start && document.activeElement !== start) start.value = solar.start_time || '08:00';
  const stop = document.getElementById('solarStopTime');
  if (stop && document.activeElement !== stop) stop.value = solar.forced_stop_time || '17:00';
  const interval = document.getElementById('solarTemperatureCheckInterval');
  if (interval && document.activeElement !== interval) {
    interval.value = Math.max(5, Math.min(60, Math.round(Number(solar.temperature_check_interval_seconds || 900) / 60)));
  }
  const earlyTemperature = document.getElementById('solarEarlyCompletionTemperature');
  if (earlyTemperature && document.activeElement !== earlyTemperature) earlyTemperature.value = Number(solar.early_completion_temperature || 31).toFixed(1);
  const earlyConfirmation = document.getElementById('solarEarlyCompletionConfirmation');
  if (earlyConfirmation && document.activeElement !== earlyConfirmation) earlyConfirmation.value = Number(solar.early_completion_confirmation_minutes || 120);
}

function renderGoodWeDashboard(s) {
  const goodwe = s.goodwe || {};
  const pvProduction = numericValue(goodwe.pv_production);
  const houseConsumption = numericValue(goodwe.house_consumption);
  const availableSurplus = pvProduction === null || houseConsumption === null
    ? null
    : Math.max(0, pvProduction - houseConsumption);

  setText('goodweHouseConsumption', fmtPower(goodwe.house_consumption));
  setText('goodweNormalLoads', fmtPower(goodwe.normal_loads));
  setText('goodweBackupLoads', fmtPower(goodwe.backup_loads));
  setText('goodwePvProduction', fmtPower(goodwe.pv_production));
  setText('goodweManagerHouse', fmtPower(goodwe.house_consumption));
  setText('goodweBatterySoc', fmtPercent(goodwe.battery_soc));
  setText('goodweGridPower', fmtPower(goodwe.grid_power));
  setText('goodweAvailableSurplus', fmtPower(availableSurplus));
  setText('goodweTemperature', fmtCelsius(goodwe.temperature));
}

function renderTelegramNotifications(s) {
  const telegram = (((s.config || {}).notifications || {}).telegram) || {};
  const enabled = document.getElementById('telegramEnabled');
  if (enabled && document.activeElement !== enabled) enabled.checked = !!telegram.enabled;
  const chatId = document.getElementById('telegramChatId');
  if (chatId && document.activeElement !== chatId) chatId.value = telegram.chat_id || '';
  const tokenHint = document.getElementById('telegramTokenHint');
  if (tokenHint) {
    tokenHint.textContent = telegram.bot_token_configured
      ? t('telegram.tokenConfigured', { token: telegram.bot_token_masked || '••••' })
      : t('telegram.tokenNotConfigured');
  }
}

function toggleTelegramTokenVisibility() {
  const token = document.getElementById('telegramBotToken');
  if (!token) return;
  token.type = token.type === 'password' ? 'text' : 'password';
}

async function saveTelegramConfig() {
  const token = document.getElementById('telegramBotToken');
  const payload = {
    enabled: document.getElementById('telegramEnabled').checked,
    bot_token: token.value,
    chat_id: document.getElementById('telegramChatId').value
  };
  const result = await api('/api/notifications/telegram/config', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(payload)
  });
  if (token) token.value = '';
  const status = document.getElementById('telegramStatus');
  if (status) status.textContent = result.ok ? t('telegram.saved') : (result.error || t('telegram.saveFailed'));
  await refresh();
  return result;
}

async function testTelegramConnection() {
  await saveTelegramConfig();
  const status = document.getElementById('telegramStatus');
  if (status) status.textContent = t('telegram.testing');
  const result = await api('/api/notifications/telegram/test', { method: 'POST' });
  if (status) status.textContent = result.ok ? result.message : (result.error || t('telegram.testFailed'));
}

function deviceName(devId, dev) {
  return optionalT(`devices.${devId}.name`) || dev.name;
}

function relayName(devId, relayNo, relay) {
  return optionalT(`devices.${devId}.relays.${relayNo}`) || relay.name;
}

function renderCards(s) {
  const root = document.getElementById('relayCards');
  root.innerHTML = '';
  const cfg = s.config;
  const safeStop = ((s.runtime || {}).heater_safe_stop) || {};
  for (const [devId, dev] of Object.entries(cfg.devices)) {
    for (const [relayNo, relay] of Object.entries(dev.relays)) {
      if (devId === 'pool' && relayNo === '2') continue;
      const key = `${devId}:${relayNo}`;
      const st = relayStatusForDisplay(key, (s.relays || {})[key] || {});
      const [label, cls] = relayLabel(key, st.active);
      const heater = isHeaterRelay(devId, relayNo, relay);
      const disabled = heater && safeStop.running ? ' disabled' : '';
      const stopButton = heater
        ? `<button class="off" onclick="safeStopHeater()"${disabled}>${esc(t('safeStop.button'))}</button>`
        : `<button class="off" onclick="setRelay('${esc(devId)}','${esc(relayNo)}',false)">${esc(t('relay.turnOff'))}</button>`;
      const safetyStatus = heater ? safeStopHtml(safeStop) : '';
      const el = document.createElement('section');
      el.className = 'card';
      el.innerHTML = `<div class="relay-title"><span class="icon">${esc(relay.icon || '🔌')}</span><div><h2>${esc(relayName(devId, relayNo, relay))}</h2><div class="muted">${esc(deviceName(devId, dev))} · ${esc(dev.ip)}:${esc(dev.port)} · ${esc(t('relay.number', { number: relayNo }))}</div></div></div><div class="state ${cls}">${esc(label)}</div><div class="muted">${esc(t('relay.response', { response: st.response || '-', ms: st.elapsed_ms || 0 }))}</div>${safetyStatus}<div class="relay-actions"><button class="on" onclick="setRelay('${esc(devId)}','${esc(relayNo)}',true)"${disabled}>${esc(t('relay.turnOn'))}</button>${stopButton}</div>`;
      root.appendChild(el);
    }
  }
}

function isHeaterRelay(devId, relayNo, relay) {
  return (devId === 'pool' && relayNo === '1') || relay.icon === '🔥' || (relay.name || '').toLowerCase().includes('riscaldatore');
}

function safeStopPhase(phase) {
  const mapped = optionalT(`safeStop.phaseMap.${phase}`);
  return mapped || phase || t('safeStop.inProgress');
}

function safeStopHtml(safeStop) {
  if (safeStop.running) {
    let detail = safeStopPhase(safeStop.phase);
    if (safeStop.state === 'COOLDOWN_RUNNING') {
      detail = t('safeStop.cooldownDetail', { seconds: safeStop.remaining_seconds || 0 });
    } else if (safeStop.state === 'STOP_PUMP') {
      detail = t('safeStop.stopPumpDetail');
    }
    return `<div class="muted">${esc(t('safeStop.running'))}<br>${detail}</div>`;
  }
  if (safeStop.state === 'ERROR') {
    return `<div class="muted">${esc(t('safeStop.error', { error: safeStop.error || t('safeStop.unknownError') }))}</div>`;
  }
  if (safeStop.state === 'COMPLETED') {
    return `<div class="muted">${esc(t('safeStop.completed'))}</div>`;
  }
  return '';
}

function renderDiag(s) {
  const lines = [];
  lines.push(t('diagnostics.serverTime', { time: s.now }));
  lines.push(t('diagnostics.lastPolling', { time: s.runtime.last_poll || '-' }));
  lines.push('');
  for (const [id, d] of Object.entries(s.devices || {})) {
    lines.push(`${d.ok ? t('diagnostics.ok') : t('diagnostics.ko')} ${deviceName(id, d)} ${d.ip}:${d.port} ${t('diagnostics.elapsedMs', { ms: d.elapsed_ms || 0 })} ${d.error || ''}`);
  }
  lines.push('');
  lines.push(t('diagnostics.lastCommand', { command: s.runtime.last_command ? JSON.stringify(s.runtime.last_command, null, 2) : '-' }));
  document.getElementById('diag').textContent = lines.join('\n');
}

async function refreshStatistics() {
  const meta = await api('/api/status');
  setAppMeta(meta);
  const s = await api('/api/statistics');
  const root = document.getElementById('statisticsCards');
  root.innerHTML = '';
  for (const stat of Object.values(s.statistics || {})) {
    const [devId, relayNo] = String(stat.key || '').split(':');
    const el = document.createElement('section');
    el.className = 'card stat-card';
    el.innerHTML = `<div class="relay-title"><span class="icon">${esc(stat.icon || '🔌')}</span><div><h2>${esc(optionalT(`devices.${devId}.relays.${relayNo}`) || stat.name)}</h2><div class="muted">${esc(optionalT(`devices.${devId}.name`) || stat.device_name)} · ${esc(t('relay.number', { number: stat.relay }))}</div></div></div><div class="stat-grid"><div><span>${esc(t('statistics.today'))}</span><strong>${esc(fmtSec(stat.daily_seconds))}</strong></div><div><span>${esc(t('statistics.season'))}</span><strong>${esc(fmtSec(stat.seasonal_seconds))}</strong></div><div><span>${esc(t('statistics.total'))}</span><strong>${esc(fmtSec(stat.total_seconds))}</strong></div><div><span>${esc(t('statistics.starts'))}</span><strong>${esc(stat.starts || 0)}</strong></div></div>`;
    root.appendChild(el);
  }
  setLastRefresh(s.last_successful_refresh);
}

function translateKnown(value, mapPath) {
  return optionalT(`${mapPath}.${value}`) || value || '-';
}

async function refreshEvents() {
  const meta = await api('/api/status');
  setAppMeta(meta);
  const s = await api('/api/events');
  const root = document.getElementById('eventLog');
  const events = s.events || [];
  root.innerHTML = events.length ? events.map(e => `<div class="event-row"><div><strong>${esc(e.date)} ${esc(e.time)}</strong><span>${esc(translateKnown(e.device, 'events.deviceMap'))} · ${esc(translateKnown(e.relay, 'events.relayMap'))}</span></div><div><strong>${esc(translateKnown(e.event, 'events.eventMap'))}</strong><span>${esc(translateKnown(e.source, 'events.sourceMap'))} · ${esc(translateKnown(e.reason, 'events.reasonMap'))}</span></div></div>`).join('') : `<div class="muted">${esc(t('events.empty'))}</div>`;
  setLastRefresh(s.last_successful_refresh);
}

async function setRelay(device, relay, active) {
  await api('/api/relay', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ device, relay, active }) });
  await refresh();
}

async function safeStopHeater() {
  await api('/api/heater/safe_stop', { method: 'POST' });
  await refresh();
}

async function savePumpConfig() {
  await api('/api/pump/config', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({
      start_time: document.getElementById('startTime').value,
      duration_hours: parseFloat(document.getElementById('duration').value),
      mode: document.getElementById('mode').value
    })
  });
  await refresh();
}

async function saveSolarHeatingConfig(extra = {}) {
  const payload = {
    water_temperature_threshold: parseFloat(document.getElementById('solarThreshold').value),
    start_time: document.getElementById('solarStartTime').value,
    forced_stop_time: document.getElementById('solarStopTime').value,
    temperature_check_interval_seconds: Math.max(5, Math.min(60, Math.round(parseFloat(document.getElementById('solarTemperatureCheckInterval').value || '15') / 5) * 5)) * 60,
    early_completion_temperature: parseFloat(document.getElementById('solarEarlyCompletionTemperature').value),
    early_completion_confirmation_minutes: Math.max(30, Math.min(240, Math.round(parseFloat(document.getElementById('solarEarlyCompletionConfirmation').value || '120')))),
    ...extra
  };
  await api('/api/solar_heating/config', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(payload) });
  await refresh();
}

async function toggleSolarHeating() {
  const enabled = !((lastStatus && lastStatus.solar_heating && lastStatus.solar_heating.enabled) || false);
  await saveSolarHeatingConfig({ enabled });
}

async function resetAuto() {
  await api('/api/pump/reset_auto', { method: 'POST' });
  await refresh();
}

async function refreshRelays() {
  await api('/api/relays/refresh', { method: 'POST' });
  await refresh();
}

async function refreshCurrentPage() {
  if (document.getElementById('relayCards')) return refresh();
  if (document.getElementById('statisticsCards')) return refreshStatistics();
  if (document.getElementById('eventLog')) return refreshEvents();
}

async function startPage() {
  await loadI18n();
  await refreshCurrentPage();
  if (!refreshTimer) refreshTimer = setInterval(refreshCurrentPage, refreshMs);
}

startPage();
