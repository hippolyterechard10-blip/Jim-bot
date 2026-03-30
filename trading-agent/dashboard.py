import json
import logging
import os
import threading
from datetime import datetime, timezone
from typing import Optional
from flask import Flask, jsonify, render_template_string
from flask_cors import CORS
from memory import TradingMemory
from analyzer import TradeAnalyzer

logger = logging.getLogger(__name__)
app = Flask(__name__)
CORS(app)
_memory: Optional[TradingMemory] = None
_analyzer: Optional[TradeAnalyzer] = None
_scanner = None
_regime  = None
_agent   = None

def init_dashboard(memory, analyzer, scanner=None, regime=None, agent=None):
    global _memory, _analyzer, _scanner, _regime, _agent
    _memory  = memory
    _analyzer = analyzer
    _scanner = scanner
    _regime  = regime
    _agent   = agent

@app.route("/api/stats")
def api_stats():
    if not _memory: return jsonify({})
    return jsonify(_memory.compute_performance_stats())

@app.route("/api/trades/open")
def api_open_trades():
    if not _memory: return jsonify([])
    return jsonify(_memory.get_open_trades())

@app.route("/api/trades/recent")
def api_recent_trades():
    if not _memory: return jsonify([])
    return jsonify(_memory.get_recent_trades(limit=20))

@app.route("/api/decisions/recent")
def api_recent_decisions():
    if not _memory: return jsonify([])
    return jsonify(_memory.get_recent_decisions(limit=15))

@app.route("/api/analyses/recent")
def api_recent_analyses():
    if not _memory: return jsonify([])
    analyses = _memory.get_analyses(limit=5)
    for a in analyses:
        for field in ["lessons","mistakes"]:
            if a.get(field) and isinstance(a[field], str):
                try: a[field] = json.loads(a[field])
                except: pass
    return jsonify(analyses)

@app.route("/api/anomalies")
def api_anomalies():
    if not _analyzer: return jsonify([])
    return jsonify(_analyzer.detect_performance_anomalies())

@app.route("/api/movers")
def api_movers():
    if not _scanner:
        return jsonify({"movers": [], "error": "scanner not initialized"})
    try:
        movers = _scanner.get_top_movers(top_n=6)
        return jsonify({"movers": movers, "ts": datetime.now(timezone.utc).isoformat()})
    except Exception as e:
        return jsonify({"movers": [], "error": str(e)})

@app.route("/api/sentiment")
def api_sentiment():
    if not _scanner:
        return jsonify({"sentiment": "neutral", "score": 0, "headlines": [], "alerts": []})
    try:
        result = _scanner.analyze_sentiment()
        return jsonify(result)
    except Exception as e:
        return jsonify({"sentiment": "neutral", "score": 0, "headlines": [], "alerts": [], "error": str(e)})

@app.route("/api/calendar")
def api_calendar():
    if not _scanner:
        return jsonify({"event": None, "note": ""})
    try:
        result = _scanner.check_economic_calendar()
        return jsonify(result)
    except Exception as e:
        return jsonify({"event": None, "note": "", "error": str(e)})

@app.route("/api/regime")
def api_regime():
    if not _regime:
        return jsonify({"regime": "UNKNOWN", "vix": None, "dxy": None, "score_long_threshold": 60, "score_short_threshold": 30})
    try:
        params  = _regime.get_params()
        context = _regime.build_regime_context()
        return jsonify({
            "regime":   params.get("regime", "UNKNOWN"),
            "params":   params,
            "context":  context,
        })
    except Exception as e:
        return jsonify({"regime": "UNKNOWN", "error": str(e)})

@app.route("/api/account")
def api_account():
    if not _agent:
        return jsonify({"equity": 0, "cash": 0, "buying_power": 0, "portfolio_value": 0})
    try:
        account = _agent.broker.get_account()
        return jsonify({
            "equity":          float(account.equity),
            "cash":            float(account.cash),
            "buying_power":    float(account.buying_power),
            "portfolio_value": float(account.portfolio_value),
            "last_equity":     float(account.last_equity),
        })
    except Exception as e:
        logger.error(f"api_account error: {e}")
        return jsonify({"equity": 0, "cash": 0, "buying_power": 0, "portfolio_value": 0, "error": str(e)})

@app.route("/api/stops")
def api_stops():
    """Return trailing stop prices per symbol from agent's in-memory state."""
    if not _agent:
        return jsonify({"stops": {}})
    try:
        stops = {}
        high   = getattr(_agent, "_trailing_high", {})
        trail  = getattr(_agent, "_score_trail_pct", {})
        low    = getattr(_agent, "_trailing_low",  {})
        s_trail = getattr(_agent, "_short_score_trail_pct", {})
        for sym, h in high.items():
            pct = trail.get(sym, 0.05)
            stops[sym] = round(h * (1 - pct), 4)
        for sym, l in low.items():
            pct = s_trail.get(sym, 0.03)
            stops[sym] = round(l * (1 + pct), 4)
        return jsonify({"stops": stops})
    except Exception as e:
        return jsonify({"stops": {}, "error": str(e)})

@app.route("/api/health")
def api_health():
    return jsonify({"status":"ok","timestamp":datetime.now(timezone.utc).isoformat()})

@app.route("/")
def dashboard():
    return render_template_string(DASHBOARD_HTML)

def start_dashboard(memory, analyzer, scanner=None, regime=None, agent=None, port=8080):
    init_dashboard(memory, analyzer, scanner=scanner, regime=regime, agent=agent)
    def run():
        app.run(host="0.0.0.0", port=port, debug=False, use_reloader=False)
    thread = threading.Thread(target=run, daemon=True)
    thread.start()
    logger.info(f"🌐 Dashboard running on port {port}")
    return thread

DASHBOARD_HTML = """<!DOCTYPE html>
<html lang="fr">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>TRADING AGENT</title>
<link href="https://fonts.googleapis.com/css2?family=Space+Mono:wght@400;700&family=Syne:wght@400;800&display=swap" rel="stylesheet">
<style>
*{box-sizing:border-box;margin:0;padding:0}
:root{--bg:#080c10;--surface:#0d1117;--border:#1a2030;--text:#c8d6e5;--muted:#4a5568;--green:#00ff88;--red:#ff3860;--blue:#4fc3f7;--mono:'Space Mono',monospace;--display:'Syne',sans-serif}
body{background:var(--bg);color:var(--text);font-family:var(--mono);font-size:12px;line-height:1.6}
body::before{content:'';position:fixed;inset:0;background:repeating-linear-gradient(0deg,transparent,transparent 2px,rgba(0,255,136,0.015) 2px,rgba(0,255,136,0.015) 4px);pointer-events:none;z-index:9999}
header{display:flex;justify-content:space-between;align-items:center;padding:16px 24px;border-bottom:1px solid var(--border);background:var(--surface);position:sticky;top:0;z-index:100}
.logo{font-family:var(--display);font-size:18px;font-weight:800;letter-spacing:0.15em;color:var(--green);text-transform:uppercase}
.logo span{color:var(--muted);font-weight:400}
.status-dot{width:8px;height:8px;border-radius:50%;background:var(--green);box-shadow:0 0 8px var(--green);animation:pulse 2s infinite}
@keyframes pulse{0%,100%{opacity:1}50%{opacity:0.3}}
#clock{color:var(--muted);font-size:11px}
.stats-grid{display:grid;grid-template-columns:repeat(6,1fr);gap:1px;background:var(--border)}
.stat-card{background:var(--surface);padding:20px 16px;text-align:center}
.stat-label{font-size:9px;letter-spacing:0.15em;text-transform:uppercase;color:var(--muted);margin-bottom:8px}
.stat-value{font-family:var(--display);font-size:28px;font-weight:800;line-height:1}
.pos{color:var(--green)} .neg{color:var(--red)} .neu{color:var(--blue)}
.main{display:grid;grid-template-columns:1fr 1fr 1fr;gap:1px;background:var(--border)}
.panel{background:var(--surface);padding:20px;overflow:hidden}
.panel-full{grid-column:1/-1} .panel-half{grid-column:span 2}
.panel-title{font-family:var(--display);font-size:10px;font-weight:700;letter-spacing:0.2em;text-transform:uppercase;color:var(--muted);margin-bottom:16px;display:flex;align-items:center;gap:8px}
.panel-title::after{content:'';flex:1;height:1px;background:var(--border)}
.data-table{width:100%;border-collapse:collapse}
.data-table th{font-size:9px;letter-spacing:0.12em;text-transform:uppercase;color:var(--muted);text-align:left;padding:6px 8px;border-bottom:1px solid var(--border)}
.data-table td{padding:8px;border-bottom:1px solid rgba(26,32,48,0.5)}
.tag{display:inline-block;padding:2px 7px;border-radius:3px;font-size:10px;font-weight:700}
.tag-buy{background:rgba(0,255,136,0.1);color:var(--green)}
.tag-sell{background:rgba(255,56,96,0.1);color:var(--red)}
.tag-open{background:rgba(79,195,247,0.1);color:var(--blue)}
.asset-bars{display:flex;flex-direction:column;gap:10px}
.asset-row{display:flex;align-items:center;gap:10px}
.asset-name{width:70px;font-size:11px;flex-shrink:0}
.bar-wrap{flex:1;height:14px;background:var(--bg);border-radius:2px;overflow:hidden}
.bar-fill{height:100%;border-radius:2px;transition:width 0.8s ease}
.bar-fill.pos{background:linear-gradient(90deg,#00cc6a,var(--green))}
.bar-fill.neg{background:linear-gradient(90deg,#cc2040,var(--red))}
.asset-pnl{width:60px;text-align:right;font-size:11px;flex-shrink:0}
.decision-item{padding:12px 0;border-bottom:1px solid var(--border)}
.decision-header{display:flex;justify-content:space-between;align-items:center;margin-bottom:6px}
.decision-time{font-size:10px;color:var(--muted)}
.decision-reasoning{font-size:11px;color:var(--muted);line-height:1.5;max-height:3.5em;overflow:hidden;cursor:pointer}
.decision-reasoning.expanded{max-height:200px}
.analysis-item{background:var(--bg);border:1px solid var(--border);border-radius:4px;padding:14px;margin-bottom:10px}
.analysis-text{font-size:11px;color:var(--muted);line-height:1.6;margin-bottom:8px}
.lesson{font-size:10px;color:var(--blue);padding-left:12px;position:relative;margin-bottom:4px}
.lesson::before{content:'→';position:absolute;left:0;color:var(--muted)}
.empty{text-align:center;color:var(--muted);padding:30px;font-size:11px}
.alert-box{background:rgba(255,56,96,0.1);border:1px solid var(--red);border-radius:4px;padding:10px 16px;color:var(--red);font-size:11px;margin-bottom:12px}
.refresh-bar{height:2px;background:var(--border);width:100%;position:fixed;bottom:0;left:0}
.refresh-progress{height:100%;background:var(--green);width:0%}
@media(max-width:1024px){.main{grid-template-columns:1fr 1fr}.stats-grid{grid-template-columns:repeat(3,1fr)}.panel-half{grid-column:span 1}}
@media(max-width:640px){.main{grid-template-columns:1fr}.stats-grid{grid-template-columns:repeat(2,1fr)}}
</style>
</head>
<body>
<header>
  <div class="logo">AGENT<span>/</span>TERMINAL</div>
  <div style="display:flex;align-items:center;gap:20px">
    <div class="status-dot"></div>
    <div id="clock"></div>
  </div>
</header>
<div class="stats-grid">
  <div class="stat-card"><div class="stat-label">P&L Total</div><div class="stat-value" id="kpi-pnl">—</div></div>
  <div class="stat-card"><div class="stat-label">Win Rate</div><div class="stat-value neu" id="kpi-wr">—</div></div>
  <div class="stat-card"><div class="stat-label">Trades</div><div class="stat-value" id="kpi-trades">—</div></div>
  <div class="stat-card"><div class="stat-label">Profit Factor</div><div class="stat-value neu" id="kpi-pf">—</div></div>
  <div class="stat-card"><div class="stat-label">Max Drawdown</div><div class="stat-value neg" id="kpi-dd">—</div></div>
  <div class="stat-card"><div class="stat-label">Positions Open</div><div class="stat-value neu" id="kpi-open">—</div></div>
</div>
<div class="main">
  <div class="panel panel-half">
    <div class="panel-title">Positions ouvertes</div>
    <div id="open-trades-container"><div class="empty">Aucune position ouverte</div></div>
  </div>
  <div class="panel">
    <div class="panel-title">P&L par asset</div>
    <div class="asset-bars" id="asset-bars"><div class="empty">Pas de données</div></div>
  </div>
  <div class="panel panel-full">
    <div class="panel-title">Historique des trades</div>
    <div style="overflow-x:auto">
      <table class="data-table">
        <thead><tr><th>Symbol</th><th>Dir.</th><th>Entrée</th><th>Sortie</th><th>Qté</th><th>P&L $</th><th>P&L %</th><th>Durée</th><th>Raison</th><th>Statut</th></tr></thead>
        <tbody id="trades-body"><tr><td colspan="10" class="empty">Chargement...</td></tr></tbody>
      </table>
    </div>
  </div>
  <div class="panel panel-half" style="max-height:500px;overflow-y:auto">
    <div class="panel-title">Décisions de l'agent</div>
    <div id="decisions-container"><div class="empty">Aucune décision</div></div>
  </div>
  <div class="panel" style="max-height:500px;overflow-y:auto">
    <div class="panel-title">Analyses post-trade</div>
    <div id="analyses-container"><div class="empty">Aucune analyse</div></div>
  </div>
</div>
<div class="refresh-bar"><div class="refresh-progress" id="refresh-progress"></div></div>
<script>
const REFRESH=15000;
function fmt(v,p='$',d=2){if(v===null||v===undefined||isNaN(v))return'—';return p+parseFloat(v).toFixed(d)}
function fmtPct(v){if(v===null||v===undefined||isNaN(v))return'—';const n=parseFloat(v);return(n>0?'+':'')+n.toFixed(2)+'%'}
function pnlCls(v){if(!v||isNaN(v))return'';return parseFloat(v)>=0?'pos':'neg'}
function fmtDur(m){if(!m)return'—';const n=Math.round(m);return n<60?n+'min':Math.floor(n/60)+'h'+(n%60>0?n%60+'min':'')}
async function fetchJSON(url){try{const r=await fetch(url);return await r.json()}catch(e){return null}}
function updateClock(){document.getElementById('clock').textContent=new Date().toUTCString().split(' ')[4]+' UTC'}
setInterval(updateClock,1000);updateClock();
async function updateStats(){
  const s=await fetchJSON('/api/stats');
  if(!s||s.total_trades===0)return;
  const pnl=s.total_pnl||0;
  const el=document.getElementById('kpi-pnl');
  el.textContent=(pnl>=0?'+':'')+' $'+Math.abs(pnl).toFixed(2);
  el.className='stat-value '+(pnl>=0?'pos':'neg');
  document.getElementById('kpi-wr').textContent=(s.win_rate||0).toFixed(1)+'%';
  document.getElementById('kpi-trades').textContent=s.total_trades;
  const pf=document.getElementById('kpi-pf');
  pf.textContent=s.profit_factor>=999?'∞':(s.profit_factor||0).toFixed(2);
  pf.className='stat-value '+((s.profit_factor||0)>=1?'pos':'neg');
  document.getElementById('kpi-dd').textContent='-$'+(s.max_drawdown||0).toFixed(2);
  renderAssetBars(s.asset_pnl||{});
}
async function updateOpenTrades(){
  const trades=await fetchJSON('/api/trades/open');
  const el=document.getElementById('open-trades-container');
  document.getElementById('kpi-open').textContent=trades?trades.length:0;
  if(!trades||trades.length===0){el.innerHTML='<div class="empty">Aucune position ouverte</div>';return}
  el.innerHTML='<table class="data-table"><thead><tr><th>Symbol</th><th>Dir.</th><th>Entrée</th><th>SL</th><th>TP</th></tr></thead><tbody>'+
    trades.map(t=>`<tr><td><strong>${t.symbol}</strong></td><td><span class="tag tag-${t.side}">${t.side.toUpperCase()}</span></td><td>${fmt(t.entry_price)}</td><td style="color:var(--red)">${fmt(t.stop_loss)}</td><td style="color:var(--green)">${fmt(t.take_profit)}</td></tr>`).join('')+
    '</tbody></table>';
}
function renderAssetBars(ap){
  const c=document.getElementById('asset-bars');
  const e=Object.entries(ap);
  if(!e.length){c.innerHTML='<div class="empty">Pas de données</div>';return}
  const max=Math.max(...e.map(([,v])=>Math.abs(v)),1);
  c.innerHTML=e.sort(([,a],[,b])=>b-a).map(([s,p])=>{
    const pct=Math.abs(p)/max*100;const cls=p>=0?'pos':'neg';
    return`<div class="asset-row"><div class="asset-name">${s}</div><div class="bar-wrap"><div class="bar-fill ${cls}" style="width:${pct}%"></div></div><div class="asset-pnl ${cls}">${p>=0?'+':''} $${Math.abs(p).toFixed(2)}</div></div>`;
  }).join('');
}
async function updateTradesHistory(){
  const trades=await fetchJSON('/api/trades/recent');
  const body=document.getElementById('trades-body');
  if(!trades||!trades.length){body.innerHTML='<tr><td colspan="10" class="empty">Aucun trade</td></tr>';return}
  body.innerHTML=trades.map(t=>{
    const pnl=t.pnl;const cls=pnl===null?'':(pnl>=0?'pos':'neg');const isOpen=t.status==='open';
    return`<tr><td><strong>${t.symbol}</strong></td><td><span class="tag tag-${t.side}">${t.side.toUpperCase()}</span></td><td>${fmt(t.entry_price)}</td><td>${isOpen?'<span class="tag tag-open">OUVERT</span>':fmt(t.exit_price)}</td><td>${t.qty}</td><td class="${cls}">${pnl!==null?(pnl>=0?'+':'')+'$'+Math.abs(pnl).toFixed(2):'—'}</td><td class="${cls}">${t.pnl_pct!==null?fmtPct(t.pnl_pct):'—'}</td><td>${fmtDur(t.hold_duration_min)}</td><td style="color:var(--muted)">${t.close_reason||'—'}</td><td><span class="tag ${isOpen?'tag-open':(pnl>=0?'tag-buy':'tag-sell')}">${t.status}</span></td></tr>`;
  }).join('');
}
async function updateDecisions(){
  const d=await fetchJSON('/api/decisions/recent');
  const c=document.getElementById('decisions-container');
  if(!d||!d.length){c.innerHTML='<div class="empty">Aucune décision</div>';return}
  c.innerHTML=d.map(x=>`<div class="decision-item"><div class="decision-header"><span class="tag tag-${x.decision==='buy'?'buy':x.decision==='sell'?'sell':'open'}">${x.decision.toUpperCase()}</span> <strong>${x.symbol||''}</strong><span class="decision-time">${new Date(x.decided_at).toLocaleTimeString('fr-FR')}</span></div><div class="decision-reasoning" onclick="this.classList.toggle('expanded')">${x.reasoning||'—'}</div></div>`).join('');
}
async function updateAnalyses(){
  const a=await fetchJSON('/api/analyses/recent');
  const c=document.getElementById('analyses-container');
  if(!a||!a.length){c.innerHTML='<div class="empty">Aucune analyse</div>';return}
  c.innerHTML=a.map(x=>{
    const lessons=Array.isArray(x.lessons)?x.lessons:[];
    const col=x.outcome==='win'?'var(--green)':x.outcome==='loss'?'var(--red)':'var(--blue)';
    return`<div class="analysis-item"><div style="display:flex;justify-content:space-between;margin-bottom:8px"><strong>${x.symbol}</strong><span style="color:${col};font-weight:700">${x.outcome.toUpperCase()} ${x.pnl?(x.pnl>=0?'+':'')+'$'+Math.abs(x.pnl).toFixed(2):''}</span></div><div class="analysis-text">${x.analysis||'—'}</div>${lessons.map(l=>`<div class="lesson">${l}</div>`).join('')}</div>`;
  }).join('');
}
async function refreshAll(){
  await Promise.all([updateStats(),updateOpenTrades(),updateTradesHistory(),updateDecisions(),updateAnalyses()]);
  const bar=document.getElementById('refresh-progress');
  bar.style.transition='none';bar.style.width='0%';
  requestAnimationFrame(()=>{bar.style.transition=`width ${REFRESH}ms linear`;bar.style.width='100%'});
}
refreshAll();
setInterval(refreshAll,REFRESH);
</script>
</body>
</html>"""
