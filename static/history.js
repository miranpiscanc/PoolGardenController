const historyPageConfig = document.getElementById('historyPage');
const historyState = {
  period: '24h',
  location: (historyPageConfig && historyPageConfig.dataset.initialLocation) || 'opicina',
  data: null,
  graphVisible: false,
  viewMin: null,
  viewMax: null,
  dragStart: null,
  dragRange: null
};

function historyMetricLabel(metric) {
  return optionalT(`history.metrics.${metric}`) || metric;
}

function historyFormatValue(value, unit) {
  const number = numericValue(value);
  if (number === null) return '--';
  return number.toFixed(1) + (unit ? ` ${unit}` : '');
}

function historyUnits(metric) {
  if (metric === 'temperature') return '°C';
  if (metric === 'humidity') return '%';
  return '';
}

function historyStatsAvailable(stats) {
  return ['today', 'period', 'all_time'].some(scope => {
    const values = stats[scope] || {};
    return numericValue(values.min) !== null || numericValue(values.max) !== null;
  });
}

function historyScopeValuesHtml(values, unit, showRecordedOn = false) {
  const minimum = numericValue(values.min);
  const maximum = numericValue(values.max);
  if (minimum === null && maximum === null) {
    return `<div class="history-stat-empty muted">${esc(t('history.noRecords'))}</div>`;
  }
  const timestampHtml = (timestamp) => showRecordedOn && timestamp
    ? `<small>${esc(t('history.recordedOn', { date: fmtDateTime(timestamp) }))}</small>`
    : '';
  return `
    <div><span>${esc(t('history.minimum'))}</span><strong>${esc(historyFormatValue(values.min, unit))}</strong>${timestampHtml(values.min_timestamp)}</div>
    <div><span>${esc(t('history.maximum'))}</span><strong>${esc(historyFormatValue(values.max, unit))}</strong>${timestampHtml(values.max_timestamp)}</div>
  `;
}

function setHistoryActiveButtons() {
  document.querySelectorAll('#historyPeriodButtons [data-period]').forEach(button => {
    button.classList.toggle('active', button.dataset.period === historyState.period);
  });
  document.querySelectorAll('#historyLocationButtons [data-location]').forEach(button => {
    button.classList.toggle('active', button.dataset.location === historyState.location);
  });
}

function bindHistoryControls() {
  document.querySelectorAll('#historyPeriodButtons [data-period]').forEach(button => {
    button.addEventListener('click', async () => {
      historyState.period = button.dataset.period;
      historyState.viewMin = null;
      historyState.viewMax = null;
      setHistoryActiveButtons();
      await refreshHistory();
    });
  });
  document.querySelectorAll('#historyLocationButtons [data-location]').forEach(button => {
    button.addEventListener('click', async () => {
      historyState.location = button.dataset.location;
      historyState.viewMin = null;
      historyState.viewMax = null;
      setHistoryActiveButtons();
      await refreshHistory();
    });
  });
  const toggle = document.getElementById('historyGraphToggle');
  if (toggle) {
    toggle.addEventListener('click', () => {
      historyState.graphVisible = !historyState.graphVisible;
      renderHistoryGraph();
    });
  }
  const canvas = document.getElementById('historyChart');
  if (!canvas) return;
  canvas.addEventListener('wheel', historyChartWheel, { passive: false });
  canvas.addEventListener('mousedown', historyChartMouseDown);
  canvas.addEventListener('mousemove', historyChartMouseMove);
  canvas.addEventListener('mouseleave', hideHistoryTooltip);
  window.addEventListener('mouseup', historyChartMouseUp);
  window.addEventListener('resize', drawHistoryChart);
}

function renderHistoryStats() {
  const root = document.getElementById('historyStats');
  if (!root) return;
  const data = (historyState.data || {}).statistics || {};
  const locations = data.by_location || {};
  const visibleLocations = Object.keys(locations).length ? Object.entries(locations) : [[historyState.location, data.metrics || {}]];
  root.innerHTML = visibleLocations.map(([locationKey, metrics]) => `
    <section class="card history-stat-card">
      <h2>${esc(locationKey === 'opicina' ? 'Opicina' : locationKey === 'cesclans' ? 'Cesclans' : locationKey)}</h2>
      ${Object.entries(metrics || {})
        .filter(([, stats]) => historyStatsAvailable(stats || {}))
        .map(([metric, stats]) => historyMetricStatsHtml(metric, stats || {}))
        .join('') || `<div class="muted">${esc(t('history.noData'))}</div>`}
    </section>
  `).join('');
}

function historyMetricStatsHtml(metric, stats) {
  const unit = historyUnits(metric);
  const periodTitle = optionalT(`history.periods.${historyState.period}`) || historyState.period;
  return `
    <div class="history-metric-block">
      <h3>${esc(historyMetricLabel(metric))}</h3>
      <div class="history-stat-section">
        <h4>${esc(t('history.todayTitle'))}</h4>
        <div class="history-stat-grid">${historyScopeValuesHtml(stats.today || {}, unit)}</div>
      </div>
      <div class="history-stat-section">
        <h4>📊 ${esc(periodTitle)}</h4>
        <div class="history-stat-grid">${historyScopeValuesHtml(stats.period || {}, unit)}</div>
      </div>
      <div class="history-stat-section">
        <h4>${esc(t('history.allTimeTitle'))}</h4>
        <div class="history-stat-grid">${historyScopeValuesHtml(stats.all_time || {}, unit, true)}</div>
      </div>
    </div>
  `;
}

function renderHistoryGraph() {
  const panel = document.getElementById('historyGraphPanel');
  const toggle = document.getElementById('historyGraphToggle');
  if (panel) panel.classList.toggle('hidden', !historyState.graphVisible);
  if (toggle) toggle.textContent = historyState.graphVisible ? t('history.hideGraph') : t('history.showGraph');
  if (historyState.graphVisible) drawHistoryChart();
}

function historyGraphPoints() {
  const graph = (historyState.data || {}).graph || {};
  return (graph.series || []).map(series => ({
    ...series,
    points: (series.points || []).map(point => ({
      ...point,
      x: new Date(point.timestamp).getTime(),
      y: Number(point.value)
    })).filter(point => Number.isFinite(point.x) && Number.isFinite(point.y))
  }));
}

function historyVisibleBounds(series) {
  const allX = series.flatMap(item => item.points.map(point => point.x));
  if (!allX.length) return null;
  const min = Math.min(...allX);
  const max = Math.max(...allX);
  if (historyState.viewMin === null || historyState.viewMax === null) {
    historyState.viewMin = min;
    historyState.viewMax = max;
  }
  return { min: historyState.viewMin, max: historyState.viewMax, dataMin: min, dataMax: max };
}

function drawHistoryChart() {
  const canvas = document.getElementById('historyChart');
  if (!canvas || !historyState.graphVisible) return;
  const panel = document.getElementById('historyGraphPanel');
  const width = Math.max(320, (panel || canvas).clientWidth || 640);
  const height = 380;
  const scale = window.devicePixelRatio || 1;
  canvas.width = Math.round(width * scale);
  canvas.height = Math.round(height * scale);
  canvas.style.width = `${width}px`;
  canvas.style.height = `${height}px`;
  const ctx = canvas.getContext('2d');
  ctx.setTransform(scale, 0, 0, scale, 0, 0);
  ctx.clearRect(0, 0, width, height);

  const series = historyGraphPoints();
  const bounds = historyVisibleBounds(series);
  const padding = { left: 56, right: 18, top: 24, bottom: 44 };
  const chart = { x: padding.left, y: padding.top, w: width - padding.left - padding.right, h: height - padding.top - padding.bottom };

  ctx.fillStyle = '#fff';
  ctx.fillRect(0, 0, width, height);
  ctx.strokeStyle = '#dce5eb';
  ctx.lineWidth = 1;
  ctx.strokeRect(chart.x, chart.y, chart.w, chart.h);

  if (!bounds) {
    ctx.fillStyle = '#5b6870';
    ctx.font = '16px system-ui';
    ctx.fillText(t('history.noData'), chart.x + 16, chart.y + 36);
    return;
  }

  const visibleSeries = series.map(item => ({
    ...item,
    points: item.points.filter(point => point.x >= bounds.min && point.x <= bounds.max)
  }));
  const allY = visibleSeries.flatMap(item => item.points.map(point => point.y));
  if (!allY.length) {
    ctx.fillStyle = '#5b6870';
    ctx.font = '16px system-ui';
    ctx.fillText(t('history.noData'), chart.x + 16, chart.y + 36);
    return;
  }
  const yMin = Math.min(...allY);
  const yMax = Math.max(...allY);
  const yPad = Math.max(1, (yMax - yMin) * 0.12);
  const yLo = yMin - yPad;
  const yHi = yMax + yPad;
  const colors = ['#1377b8', '#d46b08'];

  drawHistoryGrid(ctx, chart, bounds.min, bounds.max, yLo, yHi);
  visibleSeries.forEach((item, index) => drawHistorySeries(ctx, chart, item.points, bounds.min, bounds.max, yLo, yHi, colors[index % colors.length]));
  drawHistoryLegend(ctx, chart, visibleSeries, colors);
}

function drawHistoryGrid(ctx, chart, xMin, xMax, yMin, yMax) {
  ctx.strokeStyle = '#edf1f4';
  ctx.fillStyle = '#5b6870';
  ctx.font = '12px system-ui';
  for (let i = 0; i <= 4; i += 1) {
    const y = chart.y + (chart.h / 4) * i;
    ctx.beginPath();
    ctx.moveTo(chart.x, y);
    ctx.lineTo(chart.x + chart.w, y);
    ctx.stroke();
    const value = yMax - ((yMax - yMin) / 4) * i;
    ctx.fillText(value.toFixed(1), 8, y + 4);
  }
  for (let i = 0; i <= 4; i += 1) {
    const x = chart.x + (chart.w / 4) * i;
    ctx.beginPath();
    ctx.moveTo(x, chart.y);
    ctx.lineTo(x, chart.y + chart.h);
    ctx.stroke();
    const ts = xMin + ((xMax - xMin) / 4) * i;
    ctx.fillText(fmtTime(new Date(ts).toISOString()).slice(0, 5), x - 14, chart.y + chart.h + 24);
  }
}

function drawHistorySeries(ctx, chart, points, xMin, xMax, yMin, yMax, color) {
  if (!points.length) return;
  ctx.strokeStyle = color;
  ctx.lineWidth = 2.5;
  ctx.beginPath();
  points.forEach((point, index) => {
    const x = chart.x + ((point.x - xMin) / Math.max(1, xMax - xMin)) * chart.w;
    const y = chart.y + chart.h - ((point.y - yMin) / Math.max(1, yMax - yMin)) * chart.h;
    if (index === 0) ctx.moveTo(x, y);
    else ctx.lineTo(x, y);
  });
  ctx.stroke();
}

function drawHistoryLegend(ctx, chart, series, colors) {
  ctx.font = '13px system-ui';
  series.forEach((item, index) => {
    const x = chart.x + 12 + index * 130;
    const y = chart.y + 18;
    ctx.fillStyle = colors[index % colors.length];
    ctx.fillRect(x, y - 9, 18, 4);
    ctx.fillStyle = '#122';
    ctx.fillText(item.label || item.location, x + 26, y - 4);
  });
}

function historyChartWheel(event) {
  if (!historyState.graphVisible || historyState.viewMin === null || historyState.viewMax === null) return;
  event.preventDefault();
  const canvas = event.currentTarget;
  const rect = canvas.getBoundingClientRect();
  const ratio = (event.clientX - rect.left) / Math.max(1, rect.width);
  const range = historyState.viewMax - historyState.viewMin;
  const factor = event.deltaY < 0 ? 0.8 : 1.25;
  const nextRange = Math.max(60 * 1000, range * factor);
  const anchor = historyState.viewMin + range * ratio;
  historyState.viewMin = anchor - nextRange * ratio;
  historyState.viewMax = historyState.viewMin + nextRange;
  drawHistoryChart();
}

function historyChartMouseDown(event) {
  if (!historyState.graphVisible) return;
  historyState.dragStart = event.clientX;
  historyState.dragRange = { min: historyState.viewMin, max: historyState.viewMax };
}

function historyChartMouseMove(event) {
  if (historyState.dragStart !== null && historyState.dragRange) {
    const canvas = event.currentTarget;
    const rect = canvas.getBoundingClientRect();
    const delta = event.clientX - historyState.dragStart;
    const range = historyState.dragRange.max - historyState.dragRange.min;
    const shift = (delta / Math.max(1, rect.width)) * range;
    historyState.viewMin = historyState.dragRange.min - shift;
    historyState.viewMax = historyState.dragRange.max - shift;
    drawHistoryChart();
    return;
  }
  showHistoryTooltip(event);
}

function historyChartMouseUp() {
  historyState.dragStart = null;
  historyState.dragRange = null;
}

function showHistoryTooltip(event) {
  const tooltip = document.getElementById('historyTooltip');
  const canvas = document.getElementById('historyChart');
  if (!tooltip || !canvas || historyState.viewMin === null || historyState.viewMax === null) return;
  const rect = canvas.getBoundingClientRect();
  const xTime = historyState.viewMin + ((event.clientX - rect.left) / Math.max(1, rect.width)) * (historyState.viewMax - historyState.viewMin);
  const points = historyGraphPoints().flatMap(series => series.points.map(point => ({ ...point, label: series.label, unit: series.unit })));
  if (!points.length) return;
  const nearest = points.reduce((best, point) => Math.abs(point.x - xTime) < Math.abs(best.x - xTime) ? point : best, points[0]);
  tooltip.innerHTML = `<strong>${esc(nearest.label)}</strong><br>${esc(fmtDateTime(nearest.timestamp))}<br>${esc(historyFormatValue(nearest.value, nearest.unit || ''))}`;
  tooltip.style.left = `${Math.min(rect.width - 180, Math.max(8, event.clientX - rect.left + 12))}px`;
  tooltip.style.top = `${Math.max(8, event.clientY - rect.top - 12)}px`;
  tooltip.classList.remove('hidden');
}

function hideHistoryTooltip() {
  const tooltip = document.getElementById('historyTooltip');
  if (tooltip) tooltip.classList.add('hidden');
}

async function refreshHistory() {
  if (!document.getElementById('historyPage')) return;
  bindHistoryControlsOnce();
  const meta = await api('/api/status');
  setAppMeta(meta);
  const result = await api(`/api/history?period=${encodeURIComponent(historyState.period)}&location=${encodeURIComponent(historyState.location)}`);
  historyState.data = result || {};
  renderHistoryStats();
  renderHistoryGraph();
  setLastRefresh((result || {}).now);
}

let historyControlsBound = false;
function bindHistoryControlsOnce() {
  if (historyControlsBound) return;
  historyControlsBound = true;
  bindHistoryControls();
}
