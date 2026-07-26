(function () {
  'use strict';

  function slug(value) {
    return String(value || '')
      .normalize('NFD')
      .replace(/[\u0300-\u036f]/g, '')
      .toLowerCase()
      .replace(/[^a-z0-9]+/g, '')
      .trim();
  }

  function context() {
    const source = document.body ? document.body.dataset : {};
    return {
      id: slug(source.houseId) || 'cesclans',
      name: String(source.houseName || 'Cesclans'),
      viewer: source.viewer === 'true'
    };
  }

  function houseId() {
    return context().id;
  }

  function isCesclans() {
    return houseId() === 'cesclans';
  }

  const netatmoHomeHouses = Object.freeze({
    '5984356be6da232ab78b4a22': 'opicina',
    '659339314bc6fec2770fff76': 'cesclans'
  });

  function netatmoHouseId(homeId, home) {
    const configured = netatmoHomeHouses[String(homeId || '')];
    if (configured) return configured;
    const name = slug((home || {}).name);
    if (name.includes('cezklanc') || name.includes('cesclans')) return 'cesclans';
    if (name.includes('opcine') || name.includes('opicina')) return 'opicina';
    return null;
  }

  function netatmoForHouse(data, selectedHouseId = houseId()) {
    const source = data || {};
    const selected = slug(selectedHouseId);
    const homes = {};
    const stations = {};
    const modules = {};

    Object.entries(source.homes || {}).forEach(([homeId, home]) => {
      if (netatmoHouseId(homeId, home) !== selected) return;
      homes[homeId] = home;
      ((home || {}).stations || []).forEach(stationId => {
        const station = (source.stations || {})[stationId];
        if (!station) return;
        stations[stationId] = station;
        (station.modules || []).forEach(moduleId => {
          if ((source.modules || {})[moduleId]) modules[moduleId] = source.modules[moduleId];
        });
      });
    });

    const ownedDevices = [...Object.values(stations), ...Object.values(modules)];
    const online = Object.keys(homes).length > 0
      && ownedDevices.some(device => (device || {}).online !== false)
      && source.online !== false;
    return { ...source, homes, stations, modules, online, status: online ? 'online' : 'offline' };
  }

  function weatherForHouse(data, selectedHouseId = houseId()) {
    const source = data || {};
    const selected = slug(selectedHouseId);
    const locations = {};
    Object.entries(source.locations || {}).forEach(([key, location]) => {
      if (slug(key) === selected || slug((location || {}).label) === selected) locations[key] = location;
    });
    const metrics = Object.values(locations).flatMap(location => Object.values((location || {}).metrics || {}));
    const usable = metrics.filter(metric => !(metric || {}).placeholder);
    const onlineCount = usable.filter(metric => (metric || {}).online).length;
    const status = !usable.length ? 'unconfigured' : onlineCount === usable.length ? 'online' : onlineCount ? 'partial' : 'offline';
    return { ...source, locations, communication: { ...(source.communication || {}), status } };
  }

  const intesisDeviceHouses = Object.freeze({
    '224571441890950': 'cesclans',
    '224571441736783': 'cesclans',
    '224571441773013': 'opicina',
    '127937099454': 'opicina',
    '127936941362': 'opicina'
  });

  // Stable runtime IDs are primary; the established name rule is only a legacy fallback.
  function intesisHouseId(device) {
    const configured = intesisDeviceHouses[String((device || {}).id || '')];
    if (configured) return configured;
    const name = slug((device || {}).name);
    if (name.startsWith('cezklanc') || name.startsWith('cesclans')) return 'cesclans';
    if (name.includes('opcine') || name.includes('opicina')) return 'opicina';
    return null;
  }

  function intesisForHouse(devices, selectedHouseId = houseId()) {
    const selected = slug(selectedHouseId);
    return (Array.isArray(devices) ? devices : []).filter(device => intesisHouseId(device) === selected);
  }

  const intesisControlLabels = Object.freeze({
    mode: Object.freeze({ auto: 'Auto', cool: 'Freddo', heat: 'Caldo', dry: 'Deumid.', fan: 'Vent.' }),
    fan: Object.freeze({ auto: 'Auto', quiet: 'Silenz.', low: 'Bassa', medium: 'Media', high: 'Alta' })
  });

  function intesisControlLabel(kind, value) {
    const normalized = String(value || '').toLowerCase();
    return (intesisControlLabels[kind] || {})[normalized] || value || '--';
  }

  window.MeMHouseUI = Object.freeze({
    context,
    houseId,
    isCesclans,
    netatmoHouseId,
    netatmoForHouse,
    weatherForHouse,
    intesisHouseId,
    intesisForHouse,
    intesisControlLabels,
    intesisControlLabel
  });
}());
