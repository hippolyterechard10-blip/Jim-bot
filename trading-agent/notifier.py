import json
import logging
import os
import smtplib
import threading
from datetime import datetime, timezone
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from typing import Optional
import config
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
        stats      = self.memory.compute_performance_stats()
        today_utc  = datetime.now(timezone.utc).date()
        recent     = self.memory.get_recent_trades(limit=30)

        # ── Today's closed trades (exclude bookkeeping partials) ────────────────
        def _is_today(t):
            try:
                return datetime.fromisoformat(t["entry_at"]).replace(tzinfo=timezone.utc).date() == today_utc
            except Exception:
                return False

        daily      = [t for t in recent if t.get("entry_at") and _is_today(t)]
        real_daily = [t for t in daily  if t.get("close_reason") != "partial_profit_remainder"]
        daily_pnl  = sum(t.get("pnl") or 0 for t in real_daily if t.get("pnl") is not None)
        pnl        = stats.get("total_pnl", 0) or 0

        # ── Helper: parse strategy_source from market_context ──────────────────
        def _src(t):
            mc = t.get("market_context")
            if isinstance(mc, str):
                try: mc = json.loads(mc)
                except: mc = {}
            return (mc or {}).get("strategy_source", "")

        # ── Per-expert capital breakdown ────────────────────────────────────────
        def _expert_stats(source_key):
            base       = config.STRATEGY_CAPITAL.get(source_key, 500.0)
            all_trades = self.memory.get_recent_trades(limit=500)
            src_closed = [
                t for t in all_trades
                if t.get("status") == "closed"
                and t.get("close_reason") != "partial_profit_remainder"
                and _src(t) == source_key
            ]
            src_pnl = sum(t.get("pnl") or 0 for t in src_closed if t.get("pnl") is not None)
            wins    = sum(1 for t in src_closed if (t.get("pnl") or 0) > 0)
            wr      = round(wins / len(src_closed) * 100, 1) if src_closed else 0.0
            return base, src_pnl, len(src_closed), wr

        gap_base, gap_pnl, gap_n, gap_wr = _expert_stats("gapper")
        geo_base, geo_pnl, geo_n, geo_wr = _expert_stats("geometric")
        gap_now = gap_base + gap_pnl
        geo_now = geo_base + geo_pnl
        gap_ret = (gap_pnl / gap_base * 100) if gap_base else 0.0
        geo_ret = (geo_pnl / geo_base * 100) if geo_base else 0.0

        # ── Alerts ──────────────────────────────────────────────────────────────
        anomalies  = self.analyzer.detect_performance_anomalies()
        alert_html = ""
        if anomalies:
            alert_html = f'<div class="alert"><strong>⚠ ALERTES</strong><br><br>{"<br>".join(anomalies)}</div>'

        # ── Trade cards (V2 fields only) ────────────────────────────────────────
        def _hold_dur(t):
            try:
                ea = t.get("entry_at"); xa = t.get("exit_at")
                if not ea or not xa:
                    return "—"
                entry_dt = datetime.fromisoformat(ea).replace(tzinfo=timezone.utc)
                exit_dt  = datetime.fromisoformat(xa).replace(tzinfo=timezone.utc)
                mins = int((exit_dt - entry_dt).total_seconds() / 60)
                if mins < 60:
                    return f"{mins}m"
                elif mins < 1440:
                    return f"{mins // 60}h{mins % 60:02d}m"
                else:
                    return f"{mins // 1440}d{(mins % 1440) // 60}h"
            except Exception:
                return "—"

        cards = ""
        for t in real_daily[:8]:
            p   = t.get("pnl")
            cls = _pcolor(p)

            src    = _src(t)
            if src == "gapper":
                tag, tag_col = "GAP", "#f59e0b"
            elif src == "geometric":
                tag, tag_col = "GEO", "#4fc3f7"
            else:
                tag, tag_col = "—",  "#6b7280"

            ep   = t.get("entry_price")
            xp   = t.get("exit_price")
            if ep and xp:
                price_str = f"${ep:.4f} → ${xp:.4f}"
            elif ep:
                price_str = f"${ep:.4f} → open"
            else:
                price_str = "—"

            dur         = _hold_dur(t)
            exit_reason = (t.get("close_reason") or "—").replace("_", " ")

            cards += (
                f'<div style="border:1px solid #1a2030;border-radius:4px;padding:10px 12px;margin-bottom:8px;background:#0d1117">'
                f'<div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:6px">'
                f'<div style="display:flex;align-items:center;gap:8px">'
                f'<span style="font-size:9px;font-weight:700;background:{tag_col}22;color:{tag_col};border:1px solid {tag_col}44;border-radius:3px;padding:1px 6px;letter-spacing:.06em">{tag}</span>'
                f'<strong style="font-size:13px">{t["symbol"]}</strong>'
                f'<span style="color:#6b7280;font-size:11px">{t.get("side","").upper()}</span>'
                f'</div>'
                f'<span class="{cls}" style="font-size:14px;font-weight:700">{_fpnl(p)}</span>'
                f'</div>'
                f'<div style="font-size:9px;color:#4a5568;display:flex;flex-wrap:wrap;gap:12px">'
                f'<span>Prix: <span style="color:#8892a4">{price_str}</span></span>'
                f'<span>Durée: <span style="color:#8892a4">{dur}</span></span>'
                f'<span>Sortie: <span style="color:#8892a4">{exit_reason}</span></span>'
                f'</div>'
                f'</div>'
            )

        if not cards:
            cards = '<p style="color:#4a5568;text-align:center;font-size:11px">Aucun trade aujourd\'hui</p>'

        # ── Email body ──────────────────────────────────────────────────────────
        def _ret_str(ret):
            return f'{"+" if ret >= 0 else ""}{ret:.1f}%'

        content = f"""{alert_html}
<div class="st">Aujourd'hui</div>
<div class="krow">
<div class="k"><div class="kl">P&L aujourd'hui</div><div class="kv {_pcolor(daily_pnl)}">{_fpnl(daily_pnl)}</div></div>
<div class="k"><div class="kl">Trades</div><div class="kv neu">{len(real_daily)}</div></div>
</div>
<div class="st">Experts Capital</div>
<div class="krow">
<div class="k" style="border-color:#f59e0b55">
  <div class="kl" style="color:#f59e0b">&#9650; GAP Expert</div>
  <div class="kv {_pcolor(gap_pnl)}">${gap_now:.2f}</div>
  <div style="font-size:9px;color:#4a5568;margin-top:3px">{_ret_str(gap_ret)} &nbsp;|&nbsp; {gap_n} trades &nbsp;|&nbsp; {gap_wr:.0f}% WR</div>
</div>
<div class="k" style="border-color:#4fc3f755">
  <div class="kl" style="color:#4fc3f7">&#9670; GEO Expert</div>
  <div class="kv {_pcolor(geo_pnl)}">${geo_now:.2f}</div>
  <div style="font-size:9px;color:#4a5568;margin-top:3px">{_ret_str(geo_ret)} &nbsp;|&nbsp; {geo_n} trades &nbsp;|&nbsp; {geo_wr:.0f}% WR</div>
</div>
</div>
<div class="st">Global</div>
<div class="krow">
<div class="k"><div class="kl">P&L Total</div><div class="kv {_pcolor(pnl)}">{_fpnl(pnl)}</div></div>
<div class="k"><div class="kl">Win Rate</div><div class="kv neu">{stats.get('win_rate', 0):.1f}%</div></div>
<div class="k"><div class="kl">Trades Total</div><div class="kv">{stats.get('total_trades', 0)}</div></div>
<div class="k"><div class="kl">Drawdown</div><div class="kv neg">-${stats.get('max_drawdown', 0):.2f}</div></div>
</div>
<div class="st">Trades du jour</div>
{cards}"""
        return _send(
            f"[Trading Agent] Résumé {today_utc.strftime('%d/%m/%Y')} — {_fpnl(daily_pnl)}",
            _html("Résumé quotidien", content)
        )

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
