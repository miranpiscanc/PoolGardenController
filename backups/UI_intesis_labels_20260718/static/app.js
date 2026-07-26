let lastStatus = null;
let refreshTimer = null;
let refreshMs = 60000;
let refreshCountdownTimer = null;
let refreshDeadline = Date.now() + refreshMs;
let currentLanguage = 'en';
let translations = {};
let availableLanguages = {};
const relayCache = {};
const relayStatusCache = {};
const pumpDisplayCache = { info: null, bar: null };
const intesisDeviceCache = new Map();
const intesisDeviceCards = new Map();
const intesisGroups = new Map();
const intesisDeviceGenerations = new Map();
const intesisModeOrder = ['auto', 'cool', 'heat', 'dry', 'fan'];
const intesisFanOrder = ['auto', 'quiet', 'low', 'medium', 'high'];
const intesisVaneOrder = [
  'auto/stop', 'manual1', 'manual2', 'manual3', 'manual4', 'manual5',
  'manual6', 'manual7', 'manual8', 'manual9', 'swing'
];

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
    value = value.split(`{${key}}`).join(replacement);
  }
  return value;
}

function optionalT(path) {
  const value = getPath(translations, path);
  return value === undefined || value === null ? null : String(value);
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

function applyTranslations() {
  document.documentElement.lang = currentLanguage;
  document.querySelectorAll('[data-i18n]').forEach(el => {
    el.textContent = t(el.dataset.i18n);
  });
  document.querySelectorAll('[data-i18n-title]').forEach(el => {
    el.title = t(el.dataset.i18nTitle);
    el.setAttribute('aria-label', t(el.dataset.i18nTitle));
  });
  const title = document.querySelector('title');
  const titleKey = title ? title.dataset.i18n : null;
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

function gridDirectionLabel(value) {
  const number = numericValue(value);
  if (number === null) return '--';
  if (number > 0) return '🟢 ⬆ Esportazione';
  if (number < 0) return '🔴 ⬇ Prelievo';
  return '⚪ Bilanciato';
}

function batteryPowerLabel(value) {
  const number = numericValue(value);
  if (number === null) return '--';
  return fmtPower(Math.abs(number));
}

function batteryDirectionLabel(value) {
  const mode = String(value || '').trim().toLowerCase();
  if (mode === 'charge') return '⬇ Carica';
  if (mode === 'discharge') return '⬆ Scarica';
  if (mode === 'idle') return '⚪ Inattiva';
  return '--';
}

function batteryTemperatureLabel(value) {
  return fmtCelsius(value);
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
    console.error(`Dashboard binding failed: ${label}`, error);
  }
}

function portalSection() {
  return String(window.location.hash || '').replace('#', '') || 'home';
}

function renderPortalRoute() {
  const section = portalSection();
  const pageMap = {
    netatmo: 'pageNetatmo',
    pool: 'pagePool',
    energy: 'pageEnergy',
    weather: 'pageWeather',
    'air-conditioning': 'pageAirConditioning',
    settings: 'pageSettings'
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

function renderPortalSummary(s) {
  const netatmo = s.netatmo || {};
  const netatmoHomes = Object.keys(netatmo.homes || {}).length;
  setText('portalNetatmoSummary', netatmoHomes ? `${netatmoHomes} case · ${fmtTime(netatmo.last_successful_poll)}` : t('netatmo.noHomes'));
  if (netatmo.online || netatmo.status === 'online') setPortalBadge('portalNetatmoBadge', t('netatmo.status.online'), 'ok');
  else if (netatmo.authorization_required) setPortalBadge('portalNetatmoBadge', t('netatmo.status.authorizationRequired'), 'warn');
  else setPortalBadge('portalNetatmoBadge', t('netatmo.status.offline'), 'bad');

  const pump = s.pump || {};
  const pumpLabel = pump.active === true ? t('status.on') : pump.active === false ? t('status.off') : t('status.syncing');
  setText('portalPoolSummary', `${t('pump.title')} ${pumpLabel} · ${fmtCelsius((s.solar_heating || {}).water_temperature)}`);
  setPortalBadge('portalPoolBadge', pumpLabel, pump.active === true ? 'ok' : pump.active === false ? 'gray' : 'warn');

  const goodwe = s.goodwe || {};
  const pvProduction = numericValue(goodwe.pv_production);
  const houseConsumption = numericValue(goodwe.house_consumption);
  const availableSurplus = goodwe.available_surplus !== null && goodwe.available_surplus !== undefined ? goodwe.available_surplus : (
    pvProduction === null || houseConsumption === null ? null : Math.max(0, pvProduction - houseConsumption)
  );
  setText('portalEnergySummary', `${t('energy.pvProduction')}: ${fmtPower(goodwe.pv_production)} · ${t('energy.availableSurplus')}: ${fmtPower(availableSurplus)}`);
  setPortalBadge('portalEnergyBadge', goodwe.status === 'online' ? 'GoodWe online' : 'GoodWe offline', goodwe.status === 'online' ? 'ok' : 'bad');

  const weather = s.weather || {};
  const weatherStatus = (weather.communication || {}).status;
  setText('portalWeatherSummary', `${Object.keys(weather.locations || {}).length} località · ${fmtTime(weather.last_update)}`);
  if (weatherStatus === 'online') setPortalBadge('portalWeatherBadge', t('weather.status.online'), 'ok');
  else if (weatherStatus === 'partial') setPortalBadge('portalWeatherBadge', t('weather.status.partial'), 'warn');
  else if (weatherStatus === 'offline') setPortalBadge('portalWeatherBadge', t('weather.status.offline'), 'bad');
  else setPortalBadge('portalWeatherBadge', t('weather.status.unconfigured'), 'gray');
}

function intesisGroup(device) {
  const name = String((device || {}).name || '').trim().toLowerCase();
  if (name.startsWith('cezklanc')) return 'cesclans';
  return 'opicina';
}

function ensureIntesisGroups() {
  const root = document.getElementById('airConditioningGroups');
  if (!root || intesisGroups.size) return;
  const groups = [
    ['cesclans', 'airConditioning.groups.cesclans'],
    ['opicina', 'airConditioning.groups.opicina']
  ];
  for (const [key, titleKey] of groups) {
    const section = document.createElement('section');
    section.className = 'air-group hidden';
    section.innerHTML = `<h2 data-i18n="${esc(titleKey)}"></h2><div class="air-device-grid"></div>`;
    root.appendChild(section);
    intesisGroups.set(key, {
      section,
      grid: section.querySelector('.air-device-grid')
    });
  }
  applyTranslations();
}

function intesisMetric(labelKey, field, extraClass = '') {
  return `<div class="air-metric ${esc(extraClass)}"><span data-i18n="${esc(labelKey)}"></span><strong data-air-field="${esc(field)}">--</strong></div>`;
}

function createIntesisDeviceCard(deviceId) {
  const card = document.createElement('article');
  card.className = 'card air-device-card';
  card.dataset.deviceId = deviceId;
  card.innerHTML = `
    <div class="air-device-head">
      <div>
        <h3 data-air-field="name">--</h3>
      </div>
      <div data-air-field="power" class="pill gray">--</div>
    </div>
    <div class="air-room-temperature">
      <span data-i18n="airConditioning.roomTemperature"></span>
      <strong data-air-field="room_temperature">--</strong>
    </div>
    <div class="air-metrics">
      ${intesisMetric('airConditioning.mode', 'mode')}
      ${intesisMetric('airConditioning.targetTemperature', 'target_temperature')}
      ${intesisMetric('airConditioning.fanSpeed', 'fan_speed')}
      ${intesisMetric('airConditioning.verticalVane', 'vertical_vane')}
      ${intesisMetric('airConditioning.horizontalVane', 'horizontal_vane')}
      ${intesisMetric('airConditioning.wifi', 'wifi')}
      ${intesisMetric('airConditioning.workingHours', 'working_hours')}
      ${intesisMetric('airConditioning.currentError', 'error', 'air-error-metric')}
    </div>
    <fieldset class="air-controls" data-air-controls>
      <div class="air-control-group">
        <span class="air-control-label" data-i18n="airConditioning.power"></span>
        <div class="air-button-group">
          <button class="on air-power-button" type="button" data-air-power="true" data-i18n="airConditioning.turnOn">${esc(t('airConditioning.turnOn'))}</button>
          <button class="off air-power-button" type="button" data-air-power="false" data-i18n="airConditioning.turnOff">${esc(t('airConditioning.turnOff'))}</button>
        </div>
      </div>
      <div class="air-control-group" data-air-control="mode">
        <span class="air-control-label" data-i18n="airConditioning.mode"></span>
        <div class="air-button-group">
          ${intesisModeOrder.map(value => `<button type="button" data-air-mode="${value}">${value.toUpperCase()}</button>`).join('')}
        </div>
      </div>
      <div class="air-control-group" data-air-control="temperature">
        <span class="air-control-label" data-i18n="airConditioning.targetTemperature"></span>
        <div class="air-temperature-control">
          <button type="button" data-air-temperature="-1" aria-label="−1 °C">−</button>
          <strong data-air-field="temperature_control">--</strong>
          <button type="button" data-air-temperature="1" aria-label="+1 °C">+</button>
        </div>
      </div>
      <div class="air-control-group" data-air-control="fan">
        <span class="air-control-label" data-i18n="airConditioning.fanSpeed"></span>
        <div class="air-button-group">
          ${intesisFanOrder.map(value => `<button type="button" data-air-fan="${value}">${value.toUpperCase()}</button>`).join('')}
        </div>
      </div>
      <div class="air-vane-controls">
        <div class="air-control-group" data-air-control="vertical_vane">
          <span class="air-control-label" data-i18n="airConditioning.verticalVane"></span>
          <div class="air-button-group">
            ${intesisVaneOrder.map(value => `<button type="button" data-air-vertical-vane="${value}">${intesisVaneLabel(value)}</button>`).join('')}
          </div>
        </div>
        <div class="air-control-group" data-air-control="horizontal_vane">
          <span class="air-control-label" data-i18n="airConditioning.horizontalVane"></span>
          <div class="air-button-group">
            ${intesisVaneOrder.map(value => `<button type="button" data-air-horizontal-vane="${value}">${intesisVaneLabel(value)}</button>`).join('')}
          </div>
        </div>
      </div>
    </fieldset>
    <div class="air-command-status" data-air-command-status aria-live="polite"></div>
    <div class="air-device-footer"><span data-i18n="common.lastUpdate"></span> <strong data-air-field="last_update">--:--:--</strong></div>
    <div class="air-device-spinner hidden" data-air-spinner><span class="spinner"></span><span data-i18n="airConditioning.sending"></span></div>`;

  card.querySelectorAll('[data-air-power]').forEach(button => {
    button.addEventListener('click', () => sendIntesisCommand(
      deviceId,
      '/api/intesis/power',
      { power: button.dataset.airPower === 'true' }
    ));
  });
  card.querySelectorAll('[data-air-mode]').forEach(button => {
    button.addEventListener('click', () => sendIntesisCommand(
      deviceId,
      '/api/intesis/mode',
      { mode: button.dataset.airMode }
    ));
  });
  card.querySelectorAll('[data-air-fan]').forEach(button => {
    button.addEventListener('click', () => sendIntesisCommand(
      deviceId,
      '/api/intesis/fan',
      { speed: button.dataset.airFan }
    ));
  });
  card.querySelectorAll('[data-air-temperature]').forEach(button => {
    button.addEventListener('click', () => adjustIntesisTemperature(
      deviceId,
      Number(button.dataset.airTemperature)
    ));
  });
  card.querySelectorAll('[data-air-vertical-vane]').forEach(button => {
    button.addEventListener('click', () => sendIntesisCommand(
      deviceId,
      '/api/intesis/vertical_vane',
      { position: button.dataset.airVerticalVane }
    ));
  });
  card.querySelectorAll('[data-air-horizontal-vane]').forEach(button => {
    button.addEventListener('click', () => sendIntesisCommand(
      deviceId,
      '/api/intesis/horizontal_vane',
      { position: button.dataset.airHorizontalVane }
    ));
  });
  intesisDeviceCards.set(deviceId, card);
  applyTranslations();
  return card;
}

function setIntesisField(card, field, value) {
  const element = card.querySelector(`[data-air-field="${field}"]`);
  if (element) element.textContent = value;
}

function intesisText(value) {
  if (value === null || value === undefined || value === '') return '--';
  return String(value).split('_').join(' ').toUpperCase();
}

function intesisVaneLabel(value) {
  if (value === 'auto/stop') return 'AUTO';
  if (value === 'swing') return 'SWING';
  return value.replace('manual', '');
}

function intesisWifiSignal(value) {
  const rssi = numericValue(value);
  if (rssi === null) return '--';
  if (rssi >= -50) return `📶 █████ · ${t('airConditioning.wifiLevels.excellent')}`;
  if (rssi >= -60) return `📶 ████ · ${t('airConditioning.wifiLevels.good')}`;
  if (rssi >= -70) return `📶 ███ · ${t('airConditioning.wifiLevels.medium')}`;
  if (rssi >= -80) return `📶 ██ · ${t('airConditioning.wifiLevels.weak')}`;
  return `📶 █ · ${t('airConditioning.wifiLevels.veryWeak')}`;
}

function intesisCapabilities(values, allowed, current = null) {
  const supported = new Set(
    Array.isArray(values)
      ? values.map(value => String(value).toLowerCase()).filter(value => allowed.includes(value))
      : []
  );
  current = String(current || '').toLowerCase();
  if (allowed.includes(current)) supported.add(current);
  return supported;
}

function updateIntesisControlButtons(card, selector, supported, current) {
  card.querySelectorAll(selector).forEach(button => {
    const value = String(
      button.dataset.airMode
      || button.dataset.airFan
      || button.dataset.airVerticalVane
      || button.dataset.airHorizontalVane
      || ''
    ).toLowerCase();
    button.classList.toggle('hidden', !supported.has(value));
    button.classList.toggle('active', value === current);
  });
}

function updateIntesisDevice(device) {
  if (!device || device.id === null || device.id === undefined) return;
  ensureIntesisGroups();
  const deviceId = String(device.id);
  const previous = intesisDeviceCache.get(deviceId) || {};
  const previousUpdate = new Date(previous.last_update || 0).getTime();
  const incomingUpdate = new Date(device.last_update || 0).getTime();
  if (
    Number.isFinite(previousUpdate)
    && Number.isFinite(incomingUpdate)
    && incomingUpdate < previousUpdate
  ) return;
  const merged = { ...previous, ...device };
  if (device.supported_modes == null && previous.supported_modes != null) merged.supported_modes = previous.supported_modes;
  if (device.supported_fan_speeds == null && previous.supported_fan_speeds != null) merged.supported_fan_speeds = previous.supported_fan_speeds;
  intesisDeviceCache.set(deviceId, merged);

  const card = intesisDeviceCards.get(deviceId) || createIntesisDeviceCard(deviceId);
  const group = intesisGroups.get(intesisGroup(merged));
  if (group && card.parentElement !== group.grid) group.grid.appendChild(card);
  card.classList.remove('hidden');
  card.classList.toggle('air-device-on', merged.power === true);
  card.classList.toggle('air-device-off', merged.power === false);

  setIntesisField(card, 'name', merged.name || t('airConditioning.unnamedDevice'));
  const power = card.querySelector('[data-air-field="power"]');
  if (power) {
    power.textContent = merged.power === true ? `🟢 ${t('status.on')}` : merged.power === false ? `⚪ ${t('status.off')}` : t('status.syncing');
    power.className = 'pill ' + (merged.power === true ? 'ok' : merged.power === false ? 'gray' : 'warn');
  }
  card.querySelectorAll('[data-air-power]').forEach(button => {
    const buttonPower = button.dataset.airPower === 'true';
    button.classList.toggle('active', merged.power === buttonPower);
  });
  setIntesisField(card, 'room_temperature', fmtCelsius(merged.room_temperature));
  setIntesisField(card, 'target_temperature', fmtCelsius(merged.target_temperature));
  setIntesisField(card, 'temperature_control', fmtCelsius(merged.target_temperature));
  setIntesisField(card, 'mode', intesisText(merged.mode));
  setIntesisField(card, 'fan_speed', intesisText(merged.fan_speed));
  setIntesisField(card, 'vertical_vane', intesisText(merged.vertical_vane));
  setIntesisField(card, 'horizontal_vane', intesisText(merged.horizontal_vane));
  setIntesisField(card, 'wifi', intesisWifiSignal(
    merged.rssi !== undefined ? merged.rssi : merged.wifi
  ));
  const hours = numericValue(merged.working_hours);
  setIntesisField(card, 'working_hours', hours === null ? '--' : `${hours} h`);
  const errorMessage = String(merged.error || '').trim();
  const noErrorCode = merged.error_code !== null && merged.error_code !== undefined && Number(merged.error_code) === 0;
  const noErrorMessage = errorMessage.toLowerCase() === 'h00: no abnormality detected';
  const hasErrorCode = merged.error_code !== null && merged.error_code !== undefined && Number(merged.error_code) !== 0;
  const hasActualError = !noErrorCode && !noErrorMessage && (Boolean(errorMessage) || hasErrorCode);
  const error = hasActualError
    ? (errorMessage || `${t('airConditioning.errorCode')} ${merged.error_code}`)
    : t('airConditioning.noError');
  setIntesisField(card, 'error', error);
  const errorMetric = card.querySelector('.air-error-metric');
  const errorField = card.querySelector('[data-air-field="error"]');
  if (errorMetric) {
    errorMetric.classList.toggle('has-error', hasActualError);
    errorMetric.classList.toggle('no-error', !hasActualError);
  }
  if (errorField) errorField.classList.toggle('has-error', hasActualError);
  setIntesisField(card, 'last_update', fmtTime(merged.last_update));

  const mode = String(merged.mode || '').toLowerCase();
  const fan = String(merged.fan_speed || '').toLowerCase();
  const verticalVane = String(merged.vertical_vane || '').toLowerCase();
  const horizontalVane = String(merged.horizontal_vane || '').toLowerCase();
  const modeValues = intesisCapabilities(merged.supported_modes, intesisModeOrder, mode);
  const fanValues = intesisCapabilities(merged.supported_fan_speeds, intesisFanOrder, fan);
  const verticalVaneValues = intesisCapabilities(
    merged.supported_vertical_vanes,
    intesisVaneOrder,
    verticalVane
  );
  const horizontalVaneValues = intesisCapabilities(
    merged.supported_horizontal_vanes,
    intesisVaneOrder,
    horizontalVane
  );
  const modeControl = card.querySelector('[data-air-control="mode"]');
  const fanControl = card.querySelector('[data-air-control="fan"]');
  const verticalVaneControl = card.querySelector('[data-air-control="vertical_vane"]');
  const horizontalVaneControl = card.querySelector('[data-air-control="horizontal_vane"]');
  modeControl.classList.toggle('hidden', modeValues.size === 0);
  fanControl.classList.toggle('hidden', fanValues.size === 0);
  verticalVaneControl.classList.toggle(
    'hidden',
    merged.supports_vertical_vane !== true || verticalVaneValues.size === 0
  );
  horizontalVaneControl.classList.toggle(
    'hidden',
    merged.supports_horizontal_vane !== true || horizontalVaneValues.size === 0
  );
  updateIntesisControlButtons(card, '[data-air-mode]', modeValues, mode);
  updateIntesisControlButtons(card, '[data-air-fan]', fanValues, fan);
  updateIntesisControlButtons(card, '[data-air-vertical-vane]', verticalVaneValues, verticalVane);
  updateIntesisControlButtons(card, '[data-air-horizontal-vane]', horizontalVaneValues, horizontalVane);
  const targetTemperature = numericValue(merged.target_temperature);
  const minimumTemperature = numericValue(merged.minimum_target_temperature);
  const maximumTemperature = numericValue(merged.maximum_target_temperature);
  card.querySelector('[data-air-control="temperature"]').classList.toggle(
    'hidden',
    targetTemperature === null
  );
  card.querySelector('[data-air-temperature="-1"]').disabled = (
    minimumTemperature !== null && targetTemperature <= minimumTemperature
  );
  card.querySelector('[data-air-temperature="1"]').disabled = (
    maximumTemperature !== null && targetTemperature >= maximumTemperature
  );
}

function latestIntesisUpdate(devices) {
  return devices.reduce((latest, device) => {
    if (!device || !device.last_update) return latest;
    const timestamp = new Date(device.last_update).getTime();
    return Number.isNaN(timestamp) || timestamp <= latest.time ? latest : { time: timestamp, value: device.last_update };
  }, { time: 0, value: null }).value;
}

function renderIntesisSummary(devices) {
  const total = devices.length;
  const on = devices.filter(device => device.power === true).length;
  const off = devices.filter(device => device.power === false).length;
  const lastUpdate = latestIntesisUpdate(devices);
  setText('portalAirTotal', total);
  setText('portalAirOn', on);
  setText('portalAirOff', off);
  setText('portalAirLastUpdate', fmtTime(lastUpdate));
  const status = document.getElementById('airConditioningStatus');
  if (status) {
    status.textContent = total ? t('airConditioning.available', { count: total }) : t('airConditioning.noDevices');
    status.className = 'pill ' + (total ? 'ok' : 'warn');
  }
}

function renderIntesisDevices(devices, requestGenerations = null) {
  ensureIntesisGroups();
  const seen = new Set();
  for (const device of devices) {
    if (!device || device.id === null || device.id === undefined) continue;
    const deviceId = String(device.id);
    seen.add(deviceId);
    const card = intesisDeviceCards.get(deviceId);
    const requestGeneration = requestGenerations
      ? (requestGenerations.get(deviceId) || 0)
      : null;
    const currentGeneration = intesisDeviceGenerations.get(deviceId) || 0;
    if (
      (card && card.classList.contains('is-busy'))
      || (requestGeneration !== null && requestGeneration !== currentGeneration)
    ) continue;
    updateIntesisDevice(device);
  }
  intesisDeviceCards.forEach((card, deviceId) => card.classList.toggle('hidden', !seen.has(deviceId)));
  intesisGroups.forEach(group => {
    const visibleCards = Array.from(group.grid.children).some(card => !card.classList.contains('hidden'));
    group.section.classList.toggle('hidden', !visibleCards);
  });
  const empty = document.getElementById('airConditioningEmpty');
  if (empty) {
    empty.textContent = t(devices.length ? 'airConditioning.loading' : 'airConditioning.noDevices');
    empty.classList.toggle('hidden', devices.length > 0);
  }
  renderIntesisSummary(devices.map(device => (
    intesisDeviceCache.get(String(device.id)) || device
  )));
}

function renderIntesisFailure(error) {
  const status = document.getElementById('airConditioningStatus');
  if (status) {
    status.textContent = t('airConditioning.unavailable');
    status.className = 'pill bad';
  }
  const empty = document.getElementById('airConditioningEmpty');
  if (empty && intesisDeviceCache.size === 0) {
    empty.textContent = t('airConditioning.loadError', { error: error.message || error });
    empty.classList.remove('hidden');
  }
}

async function refreshIntesis() {
  const requestGenerations = new Map(intesisDeviceGenerations);
  const devices = await api('/api/intesis/status');
  if (!Array.isArray(devices)) throw new Error(t('airConditioning.invalidResponse'));
  renderIntesisDevices(devices, requestGenerations);
}

function setIntesisCardBusy(card, busy) {
  card.classList.toggle('is-busy', busy);
  card.querySelector('[data-air-controls]').disabled = busy;
  card.querySelector('[data-air-spinner]').classList.toggle('hidden', !busy);
}

async function sendIntesisCommand(deviceId, endpoint, values) {
  deviceId = String(deviceId);
  const card = intesisDeviceCards.get(deviceId);
  if (!card || card.classList.contains('is-busy')) return;
  intesisDeviceGenerations.set(
    deviceId,
    (intesisDeviceGenerations.get(deviceId) || 0) + 1
  );
  const status = card.querySelector('[data-air-command-status]');
  setIntesisCardBusy(card, true);
  status.textContent = '';
  status.className = 'air-command-status';
  try {
    const result = await api(endpoint, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ device_id: deviceId, ...values })
    });
    if (!result || result.ok !== true) throw new Error(t('airConditioning.commandFailed'));
    if (result.device) {
      updateIntesisDevice(result.device);
      renderIntesisSummary(Array.from(intesisDeviceCache.values()));
    } else {
      await refreshIntesis();
    }
    status.textContent = t('airConditioning.commandCompleted');
    status.className = 'air-command-status success';
  } catch (error) {
    status.textContent = error.message || t('airConditioning.commandFailed');
    status.className = 'air-command-status error';
  } finally {
    setIntesisCardBusy(card, false);
    intesisDeviceGenerations.set(
      deviceId,
      (intesisDeviceGenerations.get(deviceId) || 0) + 1
    );
  }
}

function adjustIntesisTemperature(deviceId, delta) {
  const device = intesisDeviceCache.get(String(deviceId)) || {};
  const current = numericValue(device.target_temperature);
  if (current === null) return;
  const minimum = numericValue(device.minimum_target_temperature);
  const maximum = numericValue(device.maximum_target_temperature);
  const currentTenths = Math.round(current * 10);
  const deltaTenths = Math.round(Number(delta) * 10);
  let targetTenths = currentTenths + deltaTenths;
  if (minimum !== null) {
    targetTenths = Math.max(targetTenths, Math.round(minimum * 10));
  }
  if (maximum !== null) {
    targetTenths = Math.min(targetTenths, Math.round(maximum * 10));
  }
  const temperature = targetTenths / 10;
  if (temperature === current) return;
  sendIntesisCommand(deviceId, '/api/intesis/temperature', { temperature });
}

function renderSmartLoadsState(state = 'enabled') {
  const el = document.getElementById('goodweSmartLoads');
  if (!el) return;
  const states = {
    enabled: ['🟢', t('energy.smartLoads.enabled'), 'enabled'],
    waiting: ['🟡', t('energy.smartLoads.waiting'), 'waiting'],
    disabled: ['🔴', t('energy.smartLoads.disabled'), 'disabled']
  };
  const current = states[state] || states.enabled;
  el.textContent = `${current[0]} ${t('energy.smartLoads.title')} ${current[1]}`;
  el.className = `smart-loads ${current[2]}`;
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
  const r = await fetch(url, { cache: 'no-store', ...(options || {}) });
  return await r.json();
}

function scheduleRefresh(s) {
  const nextRefreshMs = Math.max(60000, Number(s.refresh_seconds || 60) * 1000);
  if (nextRefreshMs !== refreshMs) {
    refreshMs = nextRefreshMs;
  }
}

function scheduleNextRefresh() {
  if (refreshTimer) clearTimeout(refreshTimer);
  resetRefreshCountdown();
  refreshTimer = setTimeout(async () => {
    refreshTimer = null;
    refreshDeadline = Date.now();
    renderRefreshCountdown();
    try {
      await refreshCurrentPage();
    } finally {
      scheduleNextRefresh();
    }
  }, refreshMs);
}

function renderRefreshCountdown() {
  const el = document.getElementById('refreshCountdown');
  if (!el) return;
  const seconds = Math.max(0, Math.ceil((refreshDeadline - Date.now()) / 1000));
  el.textContent = t('energy.refreshCountdown', { seconds });
}

function resetRefreshCountdown() {
  refreshDeadline = Date.now() + refreshMs;
  renderRefreshCountdown();
  if (!refreshCountdownTimer) refreshCountdownTimer = setInterval(renderRefreshCountdown, 1000);
}

async function refresh() {
  try {
    const s = await api('/api/status');
    lastStatus = s;
    safeRender('schedule refresh', () => scheduleRefresh(s));
    safeRender('app metadata', () => setAppMeta(s));
    safeRender('last refresh', () => setLastRefresh((s.runtime || {}).last_poll));
    safeRender('global status', () => {
      const devices = s.devices || {};
      const allOk = Object.values(devices).length && Object.values(devices).every(d => d.ok);
      const gs = document.getElementById('globalStatus');
      if (!gs) return;
      gs.textContent = allOk ? t('status.online') : t('status.checkNetwork');
      gs.className = 'pill ' + (allOk ? 'ok' : 'bad');
    });
    safeRender('pump', () => renderPump(s));
    safeRender('temperatures', () => renderTemperatures(s));
    safeRender('weather', () => renderWeather(s));
    safeRender('netatmo', () => renderNetatmo(s));
    safeRender('home summary', () => renderPortalSummary(s));
    safeRender('portal route', renderPortalRoute);
    safeRender('solar heating', () => renderSolarHeating(s));
    safeRender('goodwe dashboard', () => renderGoodWeDashboard(s));
    safeRender('telegram notifications', () => renderTelegramNotifications(s));
    safeRender('relay cards', () => renderCards(s));
    safeRender('diagnostics', () => renderDiag(s));
    try {
      await refreshIntesis();
    } catch (error) {
      console.error('Intesis refresh failed', error);
      renderIntesisFailure(error);
    }
  } catch (e) {
    const gs = document.getElementById('globalStatus');
    if (gs) {
      gs.textContent = t('status.serverUnreachable');
      gs.className = 'pill bad';
    }
  }
}

function renderPump(s) {
  const pump = s.pump || {};
  const startTimeField = document.getElementById('startTime');
  if (startTimeField && document.activeElement !== startTimeField) startTimeField.value = pump.start_time || '09:00';
  const durationField = document.getElementById('duration');
  if (durationField && document.activeElement !== durationField) durationField.value = pump.duration_hours || 6;
  const mode = document.getElementById('mode');
  if (mode) mode.value = pump.mode || 'auto';
  setText('pumpMode', t(`pump.modeBadge.${pump.mode || 'auto'}`));
  const pumpKey = `${pump.device || 'pool'}:${pump.relay || '2'}`;
  const [pl, pc] = relayLabel(pumpKey, pump.active);
  const ps = document.getElementById('pumpState');
  if (ps) {
    ps.textContent = pl;
    ps.className = 'state ' + pc;
  }
  if (confirmedRelay(pump.active) || !pumpDisplayCache.info) pumpDisplayCache.info = pumpInfoText(pump, s);
  setText('pumpInfo', pumpDisplayCache.info);
  if (confirmedRelay(pump.active) || pumpDisplayCache.bar === null) pumpDisplayCache.bar = pumpProgress(pump);
  const bar = document.getElementById('bar');
  if (bar) bar.style.width = pumpDisplayCache.bar + '%';
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

function fmtWeatherMetric(metric) {
  const number = numericValue(metric && metric.value);
  if (number === null) return '--';
  const unit = metric.unit || '';
  return number.toFixed(1) + (unit ? ` ${unit}` : '');
}

function weatherMetricLabel(key, metric) {
  return optionalT(`weather.metrics.${key}`) || (metric && metric.label) || key;
}

function weatherCodeInfo(code) {
  if (code === 0) return ['☀️', 'clear'];
  if ([1, 2, 3].includes(code)) return ['⛅', 'partlyCloudy'];
  if ([45, 48].includes(code)) return ['🌫️', 'fog'];
  if ([51, 53, 55, 56, 57].includes(code)) return ['🌦️', 'drizzle'];
  if ([61, 63, 65, 66, 67].includes(code)) return ['🌧️', 'rain'];
  if ([71, 73, 75, 77, 85, 86].includes(code)) return ['🌨️', 'snow'];
  if ([80, 81, 82].includes(code)) return ['🌦️', 'showers'];
  if ([95, 96, 99].includes(code)) return ['⛈️', 'thunderstorm'];
  return ['🌤️', 'unknown'];
}

function renderDailyForecast(forecast) {
  if (!forecast || !forecast.online) {
    return `<div class="weather-forecast offline">${esc(t('weather.forecastUnavailable'))}</div>`;
  }
  const info = weatherCodeInfo(Number(forecast.weather_code));
  const min = `${Number(forecast.temperature_min).toFixed(1)} °C`;
  const max = `${Number(forecast.temperature_max).toFixed(1)} °C`;
  const probability = `${Math.round(Number(forecast.precipitation_probability || 0))} %`;
  const amount = `${Number(forecast.precipitation_sum || 0).toFixed(1)} mm`;
  const speed = `${Number(forecast.wind_speed_max || 0).toFixed(1)} km/h`;
  return `<div class="weather-forecast"><div class="weather-forecast-title">${esc(t('weather.todayForecast'))}</div><div class="weather-forecast-condition"><span>${info[0]}</span><strong>${esc(t(`weather.conditions.${info[1]}`))}</strong></div><div class="weather-forecast-details"><span>🌡 ${esc(t('weather.temperatureRange', { min, max }))}</span><span>🌧 ${esc(t('weather.rainForecast', { probability, amount }))}</span><span>💨 ${esc(t('weather.windForecast', { speed }))}</span></div></div>`;
}

function renderWeather(s) {
  const data = s.weather || {};
  const root = document.getElementById('weatherLocations');
  if (root) {
    const locations = data.locations || {};
    root.innerHTML = Object.entries(locations).map(([locationKey, location]) => {
      const metrics = Object.entries((location || {}).metrics || {}).map(([metricKey, metric]) => {
        const offline = !metric.online && !metric.placeholder;
        const value = metric.placeholder ? t('weather.placeholder') : (offline ? t('status.offline') : fmtWeatherMetric(metric));
        const cls = offline ? ' class="offline"' : '';
        return `<div class="weather-metric"><span>${esc(weatherMetricLabel(metricKey, metric))}</span><strong${cls}>${esc(value)}</strong></div>`;
      }).join('');
      return `<section class="weather-location"><h3>${esc(t('weather.location', { location: location.label || locationKey }))}</h3><div class="weather-metrics">${metrics}</div>${renderDailyForecast(location.forecast)}</section>`;
    }).join('');
  }
  const last = document.getElementById('weatherLastUpdate');
  if (last) last.textContent = fmtTime(data.last_update);
  const comm = document.getElementById('weatherCommunication');
  if (!comm) return;
  const status = (data.communication || {}).status;
  if (status === 'online') {
    comm.textContent = t('weather.status.online');
    comm.className = 'pill ok';
  } else if (status === 'partial') {
    comm.textContent = t('weather.status.partial');
    comm.className = 'pill warn';
  } else if (status === 'offline') {
    comm.textContent = t('weather.status.offline');
    comm.className = 'pill bad';
  } else {
    comm.textContent = t('weather.status.unconfigured');
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
      salon: ['🛋️', t('netatmo.rooms.livingRoom')],
      soggiorno: ['🛋️', t('netatmo.rooms.livingRoom')],
      spalnica: ['🛏️', t('netatmo.rooms.masterBedroom')],
      grascina: ['👧', t('netatmo.rooms.childrensRoom')],
      balkon: ['🌳', t('netatmo.rooms.outdoor')],
      outdoor: ['🌳', t('netatmo.rooms.outdoor')],
      pluviometro: ['🌧️', t('netatmo.rooms.rainGauge')],
      rain: ['🌧️', t('netatmo.rooms.rainGauge')],
      anemometro: ['💨', t('netatmo.rooms.windSensor')],
      wind: ['💨', t('netatmo.rooms.windSensor')]
    },
    cesclans: {
      spalnica: ['🛏️', t('netatmo.rooms.masterBedroom')],
      'soba otroci': ['👦', t('netatmo.rooms.childrensRoom')],
      terasa: ['🌳', t('netatmo.rooms.outdoor')],
      outdoor: ['🌳', t('netatmo.rooms.outdoor')],
      pluviometro: ['🌧️', t('netatmo.rooms.rainGauge')],
      rain: ['🌧️', t('netatmo.rooms.rainGauge')]
    }
  };
  const byHome = friendly[home] || {};
  for (const [key, value] of Object.entries(byHome)) {
    if (name.includes(key)) return { icon: value[0], label: value[1] };
  }
  if (type === 'NAModule1') return { icon: '🌳', label: t('netatmo.rooms.outdoor') };
  if (type === 'NAModule3') return { icon: '🌧️', label: t('netatmo.rooms.rainGauge') };
  if (type === 'NAModule2') return { icon: '💨', label: t('netatmo.rooms.windSensor') };
  if (name.includes('bed') || name.includes('spalnica')) return { icon: '🛏️', label: t('netatmo.rooms.bedroom') };
  if (name.includes('child') || name.includes('otrok') || name.includes('otroci')) return { icon: '👧', label: t('netatmo.rooms.childrensRoom') };
  if (type === 'NAMain') return { icon: '🛋️', label: t('netatmo.rooms.livingRoom') };
  return { icon: '🏠', label: device.name || t('netatmo.device') };
}

function netatmoMetricInfo(key) {
  return {
    temperature: ['🌡', t('netatmo.metrics.temperature')],
    humidity: ['💧', t('netatmo.metrics.humidity')],
    co2: ['🫁', 'CO₂'],
    pressure: ['🌡', t('netatmo.metrics.pressure')],
    noise: ['🌬', t('netatmo.metrics.noise')],
    rain_today: ['🌧️', t('netatmo.metrics.rainToday')],
    wind_speed: ['💨', t('netatmo.metrics.windSpeed')],
    gust: ['💨', t('netatmo.metrics.windGust')],
    direction: ['🧭', t('netatmo.metrics.windDirection')]
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
  const homeName = home.name || t('netatmo.home');
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
        <span>🟢 ${esc(t('netatmo.devicesOnline', { count: onlineCount }))}</span>
        <span>🌡 ${esc(t('netatmo.metrics.indoor'))} ${esc(fmtNetatmoMetric(netatmoDeviceMetric(indoor, ['temperature'])))}</span>
        <span>🌳 ${esc(t('netatmo.metrics.outdoor'))} ${esc(fmtNetatmoMetric(netatmoDeviceMetric(outdoor, ['temperature'])))}</span>
        ${wind ? `<span>💨 ${esc(t('netatmo.metrics.wind'))} ${esc(fmtNetatmoMetric(netatmoDeviceMetric(wind, ['wind_speed'])))}</span>` : ''}
        ${rain ? `<span>🌧 ${esc(t('netatmo.metrics.rainToday'))} ${esc(fmtNetatmoMetric(netatmoDeviceMetric(rain, ['rain_today'])))}</span>` : ''}
        <span>${esc(t('common.lastUpdate'))} ${esc(fmtTime(lastUpdate))}</span>
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
        ? `<div class="netatmo-device-metric"><span>${esc(t('netatmo.metrics.daysSinceLastRain'))}</span><strong>${device.days_since_last_rain === null || device.days_since_last_rain === undefined ? esc(t('netatmo.metrics.notYetAvailable')) : esc(device.days_since_last_rain)}</strong></div>`
        : '';
      return `<div class="netatmo-device-metric"><span>${esc(info[0])} ${esc(info[1])}</span><strong>${esc(fmtNetatmoMetric(metric))}</strong></div>${rainAge}`;
    })
    .join('');
  const battery = netatmoBattery(device);
  const rawName = device.name || '';
  const subtitle = rawName && rawName !== friendly.label
    ? `<div class="netatmo-device-subtitle">${esc(t('netatmo.module'))}: ${esc(rawName)}</div>`
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
        ${metrics || `<div class="netatmo-device-metric"><span>${esc(t('netatmo.noValues'))}</span><strong>--</strong></div>`}
        ${battery ? `<div class="netatmo-device-metric"><span>🔋 ${esc(t('netatmo.metrics.battery'))}</span><strong class="pill ${esc(battery.cls)}">${esc(battery.label)}</strong></div>` : ''}
      </div>
      <div class="netatmo-device-footer">${esc(t('common.lastUpdate'))}: ${esc(fmtTime(device.last_update || device.updated_at))}</div>
    </article>`;
}

function renderNetatmo(s) {
  const data = s.netatmo || {};
  const root = document.getElementById('netatmoLocations');
  if (root) {
    const homes = Object.values(data.homes || {});
    const summary = homes.map(home => renderNetatmoSummaryCard(home, netatmoDevicesForHome(data, home), data)).join('');
    const details = homes.map(home => {
      const devices = netatmoDevicesForHome(data, home);
      const homeIcon = netatmoSlug(home.name).includes('cesclans') ? '🏡' : '🏠';
      return `
        <section class="netatmo-home-column">
          <h3>${homeIcon} ${esc(String(home.name || t('netatmo.home')).toUpperCase())}</h3>
          <div class="netatmo-device-grid">${devices.map(renderNetatmoDeviceCard).join('')}</div>
        </section>`;
    }).join('');
    root.innerHTML = homes.length
      ? `<div class="netatmo-summary-grid">${summary}</div><div class="netatmo-home-grid">${details}</div>`
      : `<section class="weather-location"><h3>${esc(t('netatmo.noHomes'))}</h3></section>`;
  }
  const last = document.getElementById('netatmoLastUpdate');
  if (last) last.textContent = fmtTime(data.last_successful_poll);
  const comm = document.getElementById('netatmoCommunication');
  if (!comm) return;
  if (data.online || data.status === 'online') {
    comm.textContent = t('netatmo.status.online');
    comm.className = 'pill ok';
  } else if (data.authorization_required) {
    comm.textContent = t('netatmo.status.authorizationRequired');
    comm.className = 'pill warn';
  } else if (data.enabled === false) {
    comm.textContent = t('netatmo.status.disabled');
    comm.className = 'pill gray';
  } else {
    comm.textContent = t('netatmo.status.offline');
    comm.className = 'pill bad';
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
  const minimumPv = document.getElementById('solarMinimumPvProduction');
  if (minimumPv && document.activeElement !== minimumPv) minimumPv.value = Number(solar.minimum_pv_production_watts || 2700);
  const pvRunningInterval = document.getElementById('solarPvRunningCheckInterval');
  if (pvRunningInterval && document.activeElement !== pvRunningInterval) pvRunningInterval.value = Number(solar.pv_running_check_interval_minutes || 30);
  const pvConfirmationDelay = document.getElementById('solarPvConfirmationDelay');
  if (pvConfirmationDelay && document.activeElement !== pvConfirmationDelay) pvConfirmationDelay.value = Number(solar.pv_confirmation_delay_minutes || 15);
}

function renderGoodWeDashboard(s) {
  const goodwe = s.goodwe || {};
  const pvProduction = numericValue(goodwe.pv_production);
  const houseConsumption = numericValue(goodwe.house_consumption);
  const batteryPower = goodwe.pbattery1 !== null && goodwe.pbattery1 !== undefined ? goodwe.pbattery1 : goodwe.battery_power;
  const availableSurplus = pvProduction === null || houseConsumption === null
    ? null
    : Math.max(0, pvProduction - houseConsumption);

  setText('goodweHouseConsumption', fmtPower(goodwe.house_consumption));
  setText('goodweNormalLoads', fmtPower(goodwe.normal_loads));
  setText('goodweBackupLoads', fmtPower(goodwe.backup_loads));
  setText('goodwePvProduction', fmtPower(goodwe.pv_production));
  setText('goodweManagerHouse', fmtPower(goodwe.house_consumption));
  setText('goodweBatterySoc', fmtPercent(goodwe.battery_soc));
  setText('goodweBatteryDirection', batteryDirectionLabel(goodwe.battery_mode_label));
  setText('goodweBatteryPower', batteryPowerLabel(batteryPower));
  setText('goodweBatteryTemperature', batteryTemperatureLabel(goodwe.battery_temperature));
  const gridPower = numericValue(goodwe.grid_power);
  setText('goodweGridPower', fmtPower(gridPower === null ? null : Math.abs(gridPower)));
  setText('goodweGridDirection', gridDirectionLabel(goodwe.grid_power));
  setText('goodweAvailableSurplus', fmtPower(availableSurplus));
  setText('goodweTemperature', fmtCelsius(goodwe.temperature));
  renderSmartLoadsState('enabled');
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

function heaterReasonLabel(reason) {
  if (!reason) return '--';
  return optionalT(`heater.reason.${reason.key}`) || reason.label || '--';
}

function heaterReasonHtml(reason) {
  if (!reason) return '';
  const cls = reason.class || 'gray';
  return `<div class="heater-reason pill ${esc(cls)}">${esc(heaterReasonLabel(reason))}</div>`;
}

function renderCards(s) {
  const root = document.getElementById('relayCards');
  root.innerHTML = '';
  const cfg = s.config;
  for (const [devId, dev] of Object.entries(cfg.devices)) {
    for (const [relayNo, relay] of Object.entries(dev.relays)) {
      if (devId === 'pool' && relayNo === '2') continue;
      const key = `${devId}:${relayNo}`;
      const st = relayStatusForDisplay(key, (s.relays || {})[key] || {});
      const [label, cls] = relayLabel(key, st.active);
      const heater = isHeaterRelay(devId, relayNo, relay);
      const stopButton = `<button class="off" onclick="setRelay('${esc(devId)}','${esc(relayNo)}',false)">${esc(t('relay.turnOff'))}</button>`;
      const heaterReason = heater ? heaterReasonHtml((s.heater || {}).reason) : '';
      const el = document.createElement('section');
      el.className = 'card';
      el.innerHTML = `<div class="relay-title"><span class="icon">${esc(relay.icon || '🔌')}</span><div><h2>${esc(relayName(devId, relayNo, relay))}</h2><div class="muted">${esc(deviceName(devId, dev))} · ${esc(dev.ip)}:${esc(dev.port)} · ${esc(t('relay.number', { number: relayNo }))}</div></div></div><div class="state ${cls}">${esc(label)}</div>${heaterReason}<div class="muted">${esc(t('relay.response', { response: st.response || '-', ms: st.elapsed_ms || 0 }))}</div><div class="relay-actions"><button class="on" onclick="setRelay('${esc(devId)}','${esc(relayNo)}',true)">${esc(t('relay.turnOn'))}</button>${stopButton}</div>`;
      root.appendChild(el);
    }
  }
}

function isHeaterRelay(devId, relayNo, relay) {
  return (devId === 'pool' && relayNo === '1') || relay.icon === '🔥' || (relay.name || '').toLowerCase().includes('riscaldatore');
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
    minimum_pv_production_watts: Math.max(0, Math.round(parseFloat(document.getElementById('solarMinimumPvProduction').value || '2700'))),
    pv_running_check_interval_minutes: Math.max(1, Math.round(parseFloat(document.getElementById('solarPvRunningCheckInterval').value || '30'))),
    pv_confirmation_delay_minutes: Math.max(1, Math.round(parseFloat(document.getElementById('solarPvConfirmationDelay').value || '15'))),
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
  if (document.getElementById('historyPage') && typeof refreshHistory === 'function') return refreshHistory();
}

async function startPage() {
  await loadI18n();
  renderPortalRoute();
  window.addEventListener('hashchange', renderPortalRoute);
  await refreshCurrentPage();
  scheduleNextRefresh();
}

startPage();
