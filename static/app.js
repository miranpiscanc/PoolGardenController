let lastStatus = null;
let refreshTimer = null;
let refreshMs = 4000;
const relayCache={};
const pumpDisplayCache={info:null,bar:null};
function fmtSec(s){s=Math.max(0,Number(s||0));let h=Math.floor(s/3600), m=Math.floor((s%3600)/60);return `${h}h ${String(m).padStart(2,'0')}m`;}
function fmtDateTime(value){
 if(!value) return '-';
 const d=new Date(value);
 if(Number.isNaN(d.getTime())) return value;
 return d.toLocaleString('it-IT',{day:'2-digit',month:'2-digit',year:'numeric',hour:'2-digit',minute:'2-digit',second:'2-digit'});
}
function setLastRefresh(value){
 const el=document.getElementById('lastRefresh');
 if(el) el.textContent=fmtDateTime(value);
}
function setAppMeta(s){
 const el=document.getElementById('appMeta');
 if(!el) return;
 const appInfo=s.app||{};
 el.textContent=`${appInfo.version||s.version||''} · server su porta ${appInfo.port||s.port||''}`;
}
function relayLabel(key,active){
 if(active===true){relayCache[key]=true; return ['ACCESO','on'];}
 if(active===false){relayCache[key]=false; return ['SPENTO','off'];}
 if(key in relayCache){
   return relayCache[key] ? ['ACCESO','on']:['SPENTO','off'];
 }
 return ['SINCRONIZZAZIONE','unknown'];
}
function confirmedRelay(active){ return active===true || active===false; }
function pumpInfoText(pump,s){
 return `Spegnimento previsto: ${pump.computed_stop_time || '--:--'} · rimanenti ${fmtSec(pump.remaining_seconds)}${s.runtime.scheduler.manual_run_until?' · timer manuale attivo':''}${s.runtime.scheduler.auto_suspended_date?' · automatico sospeso oggi':''}`;
}
function pumpProgress(pump){
 if(pump.active===true && pump.duration_hours){
   const perc=100-(pump.remaining_seconds/(pump.duration_hours*3600)*100);
   return Math.min(100,Math.max(0,perc));
 }
 return 0;
}
async function api(url, options){ const r=await fetch(url, options); return await r.json(); }
function scheduleRefresh(s){
  const appConfig=(s.config&&s.config.app)||{};
  const safeStop=((s.runtime||{}).heater_safe_stop)||{};
  const nextRefreshMs=(safeStop.running || safeStop.state==='COMPLETED') ? 1000 : Number(appConfig.poll_seconds||4)*1000;
  if(nextRefreshMs!==refreshMs || !refreshTimer){
    refreshMs=nextRefreshMs;
    if(refreshTimer) clearInterval(refreshTimer);
    refreshTimer=setInterval(refresh, refreshMs);
  }
}
async function refresh(){
  try{
    const s=await api('/api/status'); lastStatus=s;
    scheduleRefresh(s);
    setAppMeta(s);
    setLastRefresh((s.runtime||{}).last_poll);
    const devices=s.devices||{};
    const allOk=Object.values(devices).length && Object.values(devices).every(d=>d.ok);
    const gs=document.getElementById('globalStatus'); gs.textContent=allOk?'🟢 Online':'🔴 Verifica rete'; gs.className='pill '+(allOk?'ok':'bad');
    const pump=s.pump;
    const startTimeField = document.getElementById('startTime'); if (document.activeElement !== startTimeField)
    startTimeField.value = pump.start_time || '09:00';
    const durationField = document.getElementById('duration'); if (document.activeElement !== durationField)
    durationField.value = pump.duration_hours || 6;
    document.getElementById('mode').value=pump.mode||'auto';
    document.getElementById('pumpMode').textContent=(pump.mode||'auto').toUpperCase();
    const [pl,pc]=relayLabel('pump',pump.active); const ps=document.getElementById('pumpState'); ps.textContent=pl; ps.className='state '+pc;
    if(confirmedRelay(pump.active) || !pumpDisplayCache.info){
      pumpDisplayCache.info=pumpInfoText(pump,s);
    }
    document.getElementById('pumpInfo').textContent=pumpDisplayCache.info;
    if(confirmedRelay(pump.active) || pumpDisplayCache.bar===null){
      pumpDisplayCache.bar=pumpProgress(pump);
    }
    document.getElementById('bar').style.width=pumpDisplayCache.bar+'%';
    renderCards(s);
    renderDiag(s);
  }catch(e){ const gs=document.getElementById('globalStatus'); gs.textContent='🔴 Server non raggiungibile'; gs.className='pill bad'; }
}
function renderCards(s){
  const root=document.getElementById('relayCards'); root.innerHTML='';
  const cfg=s.config;
  const safeStop=((s.runtime||{}).heater_safe_stop)||{};
  for(const [devId,dev] of Object.entries(cfg.devices)){
    for(const [relayNo,relay] of Object.entries(dev.relays)){
      if(devId==='pool' && relayNo==='2') continue;
      const st=(s.relays||{})[`${devId}:${relayNo}`]||{};
      const [label,cls]=relayLabel(`${devId}:${relayNo}`,st.active);
      const heater=isHeaterRelay(devId,relayNo,relay);
      const disabled=heater && safeStop.running ? ' disabled' : '';
      const stopButton=heater
        ? `<button class="off" onclick="safeStopHeater()"${disabled}>🔥 Arresto sicuro</button>`
        : `<button class="off" onclick="setRelay('${devId}','${relayNo}',false)">Spegni</button>`;
      const safetyStatus=heater ? safeStopHtml(safeStop) : '';
      const el=document.createElement('section'); el.className='card';
      el.innerHTML=`<div class="relay-title"><span class="icon">${relay.icon||'🔌'}</span><div><h2>${relay.name}</h2><div class="muted">${dev.name} · ${dev.ip}:${dev.port} · relè ${relayNo}</div></div></div><div class="state ${cls}">${label}</div><div class="muted">Risposta: ${st.response||'-'} · ${st.elapsed_ms||0} ms</div>${safetyStatus}<div class="relay-actions"><button class="on" onclick="setRelay('${devId}','${relayNo}',true)"${disabled}>Accendi</button>${stopButton}</div>`;
      root.appendChild(el);
    }
  }
}
function isHeaterRelay(devId,relayNo,relay){
  return (devId==='pool' && relayNo==='1') || relay.icon==='🔥' || (relay.name||'').toLowerCase().includes('riscaldatore');
}
function safeStopHtml(safeStop){
  if(safeStop.running){
    let detail=safeStop.phase||'Arresto sicuro in corso';
    if(safeStop.state==='COOLDOWN_RUNNING'){
      detail=`Spegnimento riscaldatore ✓<br>Raffreddamento in corso (${safeStop.remaining_seconds||0} s)`;
    }else if(safeStop.state==='STOP_PUMP'){
      detail='Spegnimento riscaldatore ✓<br>Raffreddamento completato ✓<br>Arresto pompa...';
    }
    return `<div class="muted">⚙ Arresto sicuro in corso...<br>${detail}</div>`;
  }
  if(safeStop.state==='ERROR'){
    return `<div class="muted">Errore arresto sicuro: ${safeStop.error||'errore sconosciuto'}</div>`;
  }
  if(safeStop.state==='COMPLETED'){
    return `<div class="muted">✓ Arresto sicuro completato</div>`;
  }
  return '';
}
function renderDiag(s){
  const lines=[]; lines.push(`Ora server: ${s.now}`); lines.push(`Ultimo polling: ${s.runtime.last_poll||'-'}`); lines.push('');
  for(const [id,d] of Object.entries(s.devices||{})){ lines.push(`${d.ok?'OK':'KO'} ${d.name} ${d.ip}:${d.port} ${d.elapsed_ms||0} ms ${d.error||''}`); }
  lines.push(''); lines.push(`Ultimo comando: ${s.runtime.last_command?JSON.stringify(s.runtime.last_command,null,2):'-'}`);
  document.getElementById('diag').textContent=lines.join('\n');
}
async function refreshStatistics(){
  const meta=await api('/api/status'); setAppMeta(meta);
  const s=await api('/api/statistics');
  const root=document.getElementById('statisticsCards'); root.innerHTML='';
  for(const stat of Object.values(s.statistics||{})){
    const el=document.createElement('section'); el.className='card stat-card';
    el.innerHTML=`<div class="relay-title"><span class="icon">${stat.icon||'🔌'}</span><div><h2>${stat.name}</h2><div class="muted">${stat.device_name} · ${stat.relay_label}</div></div></div><div class="stat-grid"><div><span>Today</span><strong>${fmtSec(stat.daily_seconds)}</strong></div><div><span>Season</span><strong>${fmtSec(stat.seasonal_seconds)}</strong></div><div><span>Total</span><strong>${fmtSec(stat.total_seconds)}</strong></div><div><span>Starts</span><strong>${stat.starts||0}</strong></div></div>`;
    root.appendChild(el);
  }
  setLastRefresh(s.last_successful_refresh);
}
async function refreshEvents(){
  const meta=await api('/api/status'); setAppMeta(meta);
  const s=await api('/api/events');
  const root=document.getElementById('eventLog');
  const events=s.events||[];
  root.innerHTML=events.length ? events.map(e=>`<div class="event-row"><div><strong>${e.date} ${e.time}</strong><span>${e.device||'-'} · ${e.relay||'-'}</span></div><div><strong>${e.event}</strong><span>${e.source||'-'} · ${e.reason||'-'}</span></div></div>`).join('') : '<div class="muted">Nessun evento registrato.</div>';
  setLastRefresh(s.last_successful_refresh);
}
async function setRelay(device,relay,active){ await api('/api/relay',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({device,relay,active})}); await refresh(); }
async function safeStopHeater(){ await api('/api/heater/safe_stop',{method:'POST'}); await refresh(); }
async function savePumpConfig(){ await api('/api/pump/config',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({start_time:document.getElementById('startTime').value,duration_hours:parseFloat(document.getElementById('duration').value),mode:document.getElementById('mode').value})}); await refresh(); }
async function resetAuto(){ await api('/api/pump/reset_auto',{method:'POST'}); await refresh(); }
if(document.getElementById('relayCards')){ refresh(); refreshTimer=setInterval(refresh, refreshMs); }
if(document.getElementById('statisticsCards')){ refreshStatistics(); refreshTimer=setInterval(refreshStatistics, refreshMs); }
if(document.getElementById('eventLog')){ refreshEvents(); refreshTimer=setInterval(refreshEvents, refreshMs); }
