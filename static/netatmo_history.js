(function () {
  const state = { open: false, period: '24h', data: null };
  const colors = ['#1377b8', '#d46b08', '#2c9a58', '#8b5fbf', '#d43f57'];

  function houseId() {
    return window.MeMHouseUI && window.MeMHouseUI.context ? window.MeMHouseUI.context().id : 'cesclans';
  }

  async function load() {
    const message = document.getElementById('netatmoHistoryMessage');
    if (message) message.textContent = 'Caricamento dati…';
    try {
      const response = await fetch(`/api/netatmo/history?period=${encodeURIComponent(state.period)}&house=${encodeURIComponent(houseId())}`);
      if (!response.ok) throw new Error(`HTTP ${response.status}`);
      state.data = await response.json();
      const count = (state.data.series || []).reduce((sum, item) => sum + (item.points || []).length, 0);
      if (message) message.textContent = count ? '' : 'Nessun dato disponibile. Le registrazioni iniziano con questa versione.';
      draw();
    } catch (error) {
      if (message) message.textContent = 'Storico Netatmo temporaneamente non disponibile.';
    }
  }

  function draw() {
    const canvas = document.getElementById('netatmoHistoryChart');
    const panel = document.getElementById('netatmoHistoryPanel');
    if (!canvas || !panel || !state.open) return;
    const series = ((state.data || {}).series || []).map(item => ({
      ...item,
      points: (item.points || []).map(point => ({
        x: new Date(point.timestamp).getTime(),
        y: Number(point.value)
      })).filter(point => Number.isFinite(point.x) && Number.isFinite(point.y))
    }));
    const all = series.flatMap(item => item.points);
    const width = Math.max(320, panel.clientWidth - 24);
    const height = 380;
    const scale = window.devicePixelRatio || 1;
    canvas.width = Math.round(width * scale);
    canvas.height = Math.round(height * scale);
    canvas.style.width = `${width}px`;
    canvas.style.height = `${height}px`;
    const ctx = canvas.getContext('2d');
    ctx.setTransform(scale, 0, 0, scale, 0, 0);
    ctx.clearRect(0, 0, width, height);
    const chart = { x: 54, y: 48, w: width - 72, h: height - 94 };
    ctx.fillStyle = '#fff';
    ctx.fillRect(0, 0, width, height);
    if (!all.length) return;
    const xMin = Math.min(...all.map(point => point.x));
    const xMax = Math.max(...all.map(point => point.x));
    const rawMin = Math.min(...all.map(point => point.y));
    const rawMax = Math.max(...all.map(point => point.y));
    const pad = Math.max(1, (rawMax - rawMin) * 0.12);
    const yMin = rawMin - pad;
    const yMax = rawMax + pad;
    ctx.font = '12px system-ui';
    ctx.strokeStyle = '#e5edf2';
    ctx.fillStyle = '#5b6870';
    for (let i = 0; i <= 4; i += 1) {
      const y = chart.y + chart.h * i / 4;
      ctx.beginPath(); ctx.moveTo(chart.x, y); ctx.lineTo(chart.x + chart.w, y); ctx.stroke();
      ctx.fillText(`${(yMax - (yMax - yMin) * i / 4).toFixed(1)}°`, 7, y + 4);
      const x = chart.x + chart.w * i / 4;
      ctx.beginPath(); ctx.moveTo(x, chart.y); ctx.lineTo(x, chart.y + chart.h); ctx.stroke();
      const stamp = new Date(xMin + (xMax - xMin) * i / 4);
      const label = state.period === '24h'
        ? stamp.toLocaleTimeString('it-IT', { hour: '2-digit', minute: '2-digit' })
        : stamp.toLocaleDateString('it-IT', { day: '2-digit', month: '2-digit' });
      ctx.fillText(label, x - 16, chart.y + chart.h + 22);
    }
    series.forEach((item, index) => {
      if (!item.points.length) return;
      ctx.strokeStyle = colors[index % colors.length];
      ctx.lineWidth = 2.4;
      ctx.beginPath();
      item.points.forEach((point, pointIndex) => {
        const x = chart.x + (point.x - xMin) / Math.max(1, xMax - xMin) * chart.w;
        const y = chart.y + chart.h - (point.y - yMin) / Math.max(1, yMax - yMin) * chart.h;
        if (!pointIndex) ctx.moveTo(x, y); else ctx.lineTo(x, y);
      });
      ctx.stroke();
      const legendX = chart.x + (index % 2) * Math.max(145, chart.w / 2);
      const legendY = 14 + Math.floor(index / 2) * 18;
      ctx.fillStyle = colors[index % colors.length];
      ctx.fillRect(legendX, legendY, 18, 4);
      ctx.fillStyle = '#122';
      ctx.fillText(item.label || item.device_id, legendX + 25, legendY + 4);
    });
  }

  function init() {
    const toggle = document.getElementById('netatmoHistoryToggle');
    const panel = document.getElementById('netatmoHistoryPanel');
    if (!toggle || !panel) return;
    toggle.addEventListener('click', async () => {
      state.open = !state.open;
      panel.classList.toggle('hidden', !state.open);
      toggle.textContent = state.open ? 'Chiudi grafico' : 'Apri grafico';
      if (state.open) await load();
    });
    document.querySelectorAll('#netatmoHistoryPeriods [data-period]').forEach(button => {
      button.addEventListener('click', async () => {
        state.period = button.dataset.period;
        document.querySelectorAll('#netatmoHistoryPeriods [data-period]').forEach(item => {
          item.classList.toggle('active', item === button);
        });
        await load();
      });
    });
    window.addEventListener('resize', draw);
  }

  document.addEventListener('DOMContentLoaded', init);
}());
