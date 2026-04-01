import logging
import os
import smtplib
import threading
from datetime import datetime, timezone
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from typing import Optional
from memory import TradingMemory
from analyzer import TradeAnalyzer

logger = logging.getLogger(__name__)

def _get_cfg():
    return {
        "host": os.getenv("SMTP_HOST","smtp.gmail.com"),
        "port": int(os.getenv("SMTP_PORT","587")),
        "user": os.getenv("SMTP_USER",""),
        "pass": os.getenv("SMTP_PASS",""),
        "to":   os.getenv("NOTIFY_EMAIL",""),
    }

def _send(subject, html):
    cfg = _get_cfg()
    if not cfg["user"] or not cfg["pass"] or not cfg["to"]:
        logger.warning(f"[EMAIL SKIPPED] {subject}")
        return False
    try:
        msg = MIMEMultipart("alternative")
        msg["Subject"] = subject
        msg["From"] = f"Trading Agent <{cfg['user']}>"
        msg["To"] = cfg["to"]
        msg.attach(MIMEText(html,"html","utf-8"))
        with smtplib.SMTP(cfg["host"], cfg["port"]) as s:
            s.ehlo(); s.starttls()
            s.login(cfg["user"], cfg["pass"])
            s.sendmail(cfg["user"], cfg["to"], msg.as_string())
        logger.info(f"✅ Email sent: {subject}")
        return True
    except Exception as e:
        logger.error(f"Email error: {e}")
        return False

def _html(title, content):
    return f"""<!DOCTYPE html><html><head><meta charset="UTF-8">
<style>
body{{margin:0;padding:0;background:#080c10;font-family:'Courier New',monospace;color:#c8d6e5}}
.w{{max-width:600px;margin:0 auto;padding:24px 16px}}
.h{{border-bottom:2px solid #00ff88;padding-bottom:16px;margin-bottom:24px}}
.logo{{font-size:20px;font-weight:700;color:#00ff88;letter-spacing:0.15em}}
.sub{{font-size:11px;color:#4a5568;margin-top:4px}}
.st{{font-size:10px;letter-spacing:0.2em;text-transform:uppercase;color:#4a5568;border-bottom:1px solid #1a2030;padding-bottom:6px;margin:20px 0 12px}}
.krow{{display:flex;gap:12px;flex-wrap:wrap;margin-bottom:16px}}
.k{{background:#0d1117;border:1px solid #1a2030;border-radius:4px;padding:12px 16px;flex:1;min-width:100px;text-align:center}}
.kl{{font-size:9px;letter-spacing:0.12em;text-transform:uppercase;color:#4a5568}}
.kv{{font-size:22px;font-weight:700;margin-top:4px}}
.pos{{color:#00ff88}} .neg{{color:#ff3860}} .neu{{color:#4fc3f7}}
.alert{{background:rgba(255,56,96,0.1);border:1px solid #ff3860;border-radius:4px;padding:16px;color:#ff3860;margin-bottom:16px}}
.lesson{{padding:6px 0 6px 12px;border-left:2px solid #00ff88;font-size:11px;color:#4fc3f7;margin-bottom:6px}}
.foot{{font-size:10px;color:#4a5568;text-align:center;border-top:1px solid #1a2030;padding-top:16px;margin-top:24px}}
</style></head>
<body><div class="w">
<div class="h"><div class="logo">AGENT/TERMINAL</div>
<div class="sub">{title} — {datetime.now(timezone.utc).strftime('%d %b %Y %H:%M UTC')}</div></div>
{content}
<div class="foot">Email automatique — Paper trading uniquement</div>
</div></body></html>"""

def _pcolor(v):
    if v is None: return ""
    return "pos" if v >= 0 else "neg"

def _fpnl(v):
    if v is None: return "—"
    return f"{'+' if v>=0 else ''}${v:.2f}"

class TradingNotifier:
    def __init__(self, memory: TradingMemory, analyzer: TradeAnalyzer):
        self.memory = memory
        self.analyzer = analyzer
        self._stop = threading.Event()
        # In-memory guards — supplement SQLite dedup within the same process
        self._daily_sent_date:  Optional[str] = None
        self._weekly_sent_week: Optional[str] = None
        logger.info("✅ TradingNotifier ready")

    def send_daily_summary(self):
        import json
        stats = self.memory.compute_performance_stats()
        recent = self.memory.get_recent_trades(limit=10)
        today = datetime.now(timezone.utc).date()
        daily = [t for t in recent if t.get("entry_at") and datetime.fromisoformat(t["entry_at"]).date()==today]
        daily_pnl = sum(t.get("pnl") or 0 for t in daily if t.get("pnl") is not None)
        pnl = stats.get("total_pnl",0) or 0
        analyses = self.memory.get_analyses(limit=3)
        lessons = []
        for a in analyses:
            if a.get("lessons"):
                try:
                    ls = json.loads(a["lessons"]) if isinstance(a["lessons"],str) else a["lessons"]
                    lessons.extend(ls[:2])
                except: pass
        anomalies = self.analyzer.detect_performance_anomalies()
        alert_html = ""
        if anomalies:
            alert_html = f'<div class="alert"><strong>⚠ ALERTES</strong><br><br>{"<br>".join(anomalies)}</div>'
        real_daily = [t for t in daily if t.get("close_reason") != "partial_profit_remainder"]
        cards = ""
        for t in real_daily[:8]:
            p = t.get("pnl")
            cls = _pcolor(p)
            snap = {}
            raw_snap = t.get("entry_snapshot")
            if raw_snap:
                try:
                    snap = json.loads(raw_snap) if isinstance(raw_snap, str) else (raw_snap or {})
                except Exception:
                    snap = {}
            strategy  = snap.get("strategy_used") or "—"
            score_val = snap.get("final_score") or snap.get("base_score")
            score_str = str(int(score_val)) + "/100" if score_val is not None else "—"
            session   = snap.get("session") or "—"
            regime    = snap.get("regime")  or "—"
            conf      = snap.get("confidence")
            conf_str  = str(int(conf * 100)) + "%" if conf else "—"
            vol       = snap.get("volume_ratio")
            vol_str   = str(round(vol, 1)) + "x" if vol else "—"
            pats      = snap.get("patterns") or []
            pats_str  = ", ".join(pats) if pats else "—"
            rr        = snap.get("risk_reward")
            rr_str    = str(round(rr, 1)) + "x" if rr else "—"
            exit_reason = t.get("close_reason") or "—"
            evs = t.get("exit_vs_target")
            if evs is not None:
                evs_cls = "pos" if evs >= 100 else ("neu" if evs >= 50 else "neg")
                evs_str = '<span class="' + evs_cls + '">' + str(evs) + "% obj.</span>"
            else:
                evs_str = "—"
            cards += (
                '<div style="border:1px solid #1a2030;border-radius:4px;padding:10px 12px;margin-bottom:8px;background:#0d1117">'
                '<div style="display:flex;justify-content:space-between;margin-bottom:5px">'
                "<span><strong>" + t["symbol"] + "</strong> " + t["side"].upper() + "</span>"
                '<span class="' + cls + '">' + _fpnl(p) + " | " + exit_reason + " | " + evs_str + "</span>"
                "</div>"
                '<div style="font-size:9px;color:#4a5568;display:flex;flex-wrap:wrap;gap:8px">'
                "<span>Strat: "   + strategy  + "</span>"
                "<span>Score: "   + score_str + "</span>"
                "<span>Session: " + session   + "</span>"
                "<span>Regime: "  + regime    + "</span>"
                "<span>Conf: "    + conf_str  + "</span>"
                "<span>Vol: "     + vol_str   + "</span>"
                "<span>Patterns: "+ pats_str  + "</span>"
                "<span>R:R: "     + rr_str    + "</span>"
                "</div>"
                "</div>"
            )
        if not cards:
            cards = '<p style="color:#4a5568;text-align:center;font-size:11px">Aucun trade aujourd\'hui</p>'
        lessons_html = "".join(f'<div class="lesson">{l}</div>' for l in lessons[:5])
        content = f"""{alert_html}
<div class="st">Aujourd'hui</div>
<div class="krow">
<div class="k"><div class="kl">P&L aujourd'hui</div><div class="kv {_pcolor(daily_pnl)}">{_fpnl(daily_pnl)}</div></div>
<div class="k"><div class="kl">Trades</div><div class="kv neu">{len(daily)}</div></div>
</div>
<div class="st">Global</div>
<div class="krow">
<div class="k"><div class="kl">P&L Total</div><div class="kv {_pcolor(pnl)}">{_fpnl(pnl)}</div></div>
<div class="k"><div class="kl">Win Rate</div><div class="kv neu">{stats.get('win_rate',0):.1f}%</div></div>
<div class="k"><div class="kl">Trades</div><div class="kv">{stats.get('total_trades',0)}</div></div>
<div class="k"><div class="kl">Drawdown</div><div class="kv neg">-${stats.get('max_drawdown',0):.2f}</div></div>
</div>
<div class="st">Trades du jour</div>
{cards}
{f'<div class="st">Leçons récentes</div>{lessons_html}' if lessons_html else ''}"""
        return _send(f"[Trading Agent] Résumé {today.strftime('%d/%m/%Y')} — {_fpnl(daily_pnl)}", _html("Résumé quotidien", content))

    def send_stop_loss_alert(self, current_capital, initial_capital=1000.0):
        loss = initial_capital - current_capital
        loss_pct = (loss / initial_capital) * 100
        content = f"""<div class="alert"><strong>🔴 STOP LOSS GLOBAL DÉCLENCHÉ</strong><br><br>
Capital en baisse de <strong>{loss_pct:.1f}%</strong>. Toutes les positions ont été fermées.</div>
<div class="krow">
<div class="k"><div class="kl">Capital initial</div><div class="kv">${initial_capital:.2f}</div></div>
<div class="k"><div class="kl">Capital actuel</div><div class="kv neg">${current_capital:.2f}</div></div>
<div class="k"><div class="kl">Perte</div><div class="kv neg">-${loss:.2f}</div></div>
<div class="k"><div class="kl">Perte %</div><div class="kv neg">-{loss_pct:.1f}%</div></div>
</div>"""
        return _send(f"🚨 [Trading Agent] STOP LOSS — Perte: -${loss:.2f} (-{loss_pct:.1f}%)", _html("🚨 STOP LOSS GLOBAL", content))

    def send_test_email(self):
        content = """<div class="st">Configuration OK</div>
<div class="lesson">Résumé quotidien chaque soir à 20h UTC</div>
<div class="lesson">Rapport hebdomadaire chaque lundi matin</div>
<div class="lesson">Alerte immédiate si stop loss global déclenché</div>
<div class="lesson">Alerte si 3 pertes consécutives détectées</div>"""
        return _send("[Trading Agent] ✅ Email de test — OK", _html("Test de configuration", content))

    def _already_sent_today(self, key: str, date_str: str) -> bool:
        """Check SQLite so restarts don't re-send emails for the same date."""
        return self.memory.get_memory(key) == date_str

    def _mark_sent(self, key: str, date_str: str):
        self.memory.set_memory(key, date_str, category="email_scheduler")

    def start_scheduler(self, daily_hour_utc=20):
        self._stop.clear()
        def loop():
            while not self._stop.is_set():
                now = datetime.now(timezone.utc)
                today = now.strftime("%Y-%m-%d")
                week  = now.strftime("%Y-W%W")

                if now.hour == daily_hour_utc:
                    # Layer 1: in-process memory guard (survives nothing across restarts, but
                    #          prevents double-fire within the same process lifetime)
                    # Layer 2: SQLite guard written BEFORE sending so any restart/overlap
                    #          finds it already marked and skips
                    if self._daily_sent_date != today and not self._already_sent_today("email.last_daily_sent", today):
                        self._daily_sent_date = today           # in-memory lock acquired
                        self._mark_sent("email.last_daily_sent", today)  # SQLite lock written first
                        self.send_daily_summary()              # now safe to send

                if now.weekday() == 0 and now.hour == 8:
                    if self._weekly_sent_week != week and not self._already_sent_today("email.last_weekly_sent", week):
                        self._weekly_sent_week = week
                        self._mark_sent("email.last_weekly_sent", week)
                        report = self.analyzer.generate_performance_report("weekly")
                        _send("[Trading Agent] Rapport hebdomadaire",
                              _html("Rapport hebdo",
                                    f'<div class="st">Analyse Claude</div>'
                                    f'<p style="font-size:12px;line-height:1.7;white-space:pre-wrap">{report}</p>'))

                self._stop.wait(60)  # 60s tick is plenty — emails only fire once per day
        threading.Thread(target=loop, daemon=True, name="notifier").start()
        logger.info(f"📅 Scheduler started — daily at {daily_hour_utc}h UTC (persisted dedup via SQLite)")
