import { useEffect, useState, useCallback } from "react";

const BASE = import.meta.env.BASE_URL.replace(/\/$/, "");
const INITIAL_CAPITAL      = 1_000;
const CLAUDE_COST_PER_CALL = 0.003;
const REPLIT_MONTHLY_COST  = 20;

const EARNINGS_CALENDAR: { symbol: string; date: string; whisper: string }[] = [
  { symbol: "TSLA",  date: "2026-04-22", whisper: "Delivery miss risk — analyst estimates range wide" },
  { symbol: "GOOGL", date: "2026-04-29", whisper: "Search market share vs AI threat — key narrative" },
  { symbol: "MSFT",  date: "2026-04-30", whisper: "Azure growth rate — any deceleration = selloff" },
  { symbol: "META",  date: "2026-04-30", whisper: "Ad revenue + AI spend balance — guidance critical" },
  { symbol: "AAPL",  date: "2026-05-01", whisper: "Services revenue key — hardware expected flat" },
  { symbol: "AMD",   date: "2026-05-06", whisper: "MI300 AI chip demand vs NVDA — market share story" },
  { symbol: "NVDA",  date: "2026-05-28", whisper: "Bar is extremely high — any China export concern = miss" },
];

const MACRO_EVENTS = [
  { event: "FOMC Meeting", date: "2026-04-29", note: "Rate decision + press conference" },
  { event: "CPI Release",  date: "2026-04-10", note: "Core CPI YoY — key inflation gauge" },
  { event: "NFP Report",   date: "2026-04-03", note: "Non-Farm Payrolls — labor market" },
];

// ── Interfaces ────────────────────────────────────────────────────────────────
interface Trade {
  symbol: string; action: string; price: number; qty: number;
  timestamp: string; pnl: number | null; status: string; close_reason: string | null;
}
interface Decision {
  symbol: string; decision: string; confidence: number;
  reasoning: string; market_data?: string; decided_at: string;
  strategy_source?: string | null;
}
interface Status {
  agent: string; timestamp: string; db_connected: boolean;
  total_trades: number; recent_trades: Trade[];
}
interface Position {
  symbol: string; side: string; qty: number;
  entry_price: number; current_price: number;
  market_value: number; unrealized_pl: number; unrealized_plpc: number;
  cost_basis: number;
}
interface Mover { symbol: string; price: number; change_pct: number; direction: "up" | "down"; }
interface SentimentResponse { sentiment: string; score: number; headlines?: string[]; alerts?: string[]; ts?: string; }
interface RegimeResponse { regime: string; params?: Record<string, unknown>; context?: string; }
interface StatsResponse {
  total_trades: number; win_rate: number | null; profit_factor: number | null;
  total_pnl: number | null; max_drawdown: number; best_asset: string | null; asset_pnl: Record<string, number>;
}
interface PartialProfits { [symbol: string]: { secured_pnl: number; count: number } }
interface Stops { [symbol: string]: number }
interface PositionLive {
  symbol: string; qty: number; entry_price: number;
  live_price: number; alpaca_mark: number; unrealized: number;
}
interface AccountResponse {
  equity: number; cash: number; buying_power: number;
  portfolio_value: number; last_equity: number;
  live_equity?: number; live_unrealized?: number;
  positions_live?: PositionLive[];
}
interface ExpertStats {
  total_trades: number; total_pnl: number; win_rate: number;
  avg_win: number; avg_loss: number;
  capital_start: number; capital_now: number; capital_return: number;
  open_trades: number; live_unrealized: number;
}
interface ExpertsResponse {
  gapper?: ExpertStats;
  geometric?: ExpertStats;
}
interface ClosedTodayItem {
  symbol: string; pnl: number; trade_count: number;
  qty_sold: number; last_exit: string; reasons: string;
}
interface IndividualTrade {
  trade_id: string; symbol: string; side: string; qty: number;
  entry_price: number | null; exit_price: number | null;
  pnl: number | null; pnl_pct: number | null; hold_min: number | null;
  close_reason: string | null; entry_at: string; exit_at: string;
  exit_vs_target: number | null;
  score: number | null; regime: string | null; session: string | null;
  patterns: string[]; rr: number | null; confidence: number | null;
}

type Page = "HOME" | "MARKET" | "SIGNALS" | "TRADES" | "ANALYSIS";
type ClosedPeriod = "today" | "week" | "month" | "ytd" | "all";
type ExpertFilter = "all" | "gap" | "geo";

interface AnalysisData {
  total_trades: number; winning_trades: number; losing_trades: number;
  win_rate: number; profit_factor: number; expectancy: number;
  gross_win: number; gross_loss: number; total_pnl: number;
  avg_win: number; avg_loss: number; avg_hold_min: number;
  avg_trades_per_day: number;
  best_trade:  { symbol: string; pnl: number; reason: string } | null;
  worst_trade: { symbol: string; pnl: number; reason: string } | null;
  current_streak: { type: string; count: number } | null;
  max_win_streak: number; max_loss_streak: number;
  daily_pnl: { date: string; pnl: number; trades: number }[];
  by_asset:  { symbol: string; pnl: number; trades: number; avg_pnl: number; avg_hold_min: number }[];
  by_reason: { reason: string; trades: number; pnl: number }[];
}

// ── Helpers ───────────────────────────────────────────────────────────────────
function fmtTime(s: string) {
  const d = new Date(s.includes("T") ? s : s.replace(" ", "T") + "Z");
  return d.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
}
function fmtDateTime(s: string) {
  const d = new Date(s.includes("T") ? s : s.replace(" ", "T") + "Z");
  return d.toLocaleString([], { month: "short", day: "numeric", hour: "2-digit", minute: "2-digit" });
}
function fmtPrice(n: number) {
  if (n >= 1000) return n.toLocaleString("en-US", { minimumFractionDigits: 2, maximumFractionDigits: 2 });
  if (n >= 1)    return n.toFixed(2);
  return n.toFixed(4);
}
function fmtPnl(n: number) {
  const abs = Math.abs(n);
  const str = abs >= 1 ? abs.toFixed(2) : abs >= 0.01 ? abs.toFixed(3) : abs.toFixed(4);
  return (n >= 0 ? "+" : "-") + "$" + str;
}
function fmtPct(n: number) { return (n >= 0 ? "+" : "") + n.toFixed(2) + "%"; }
function fmtHoldMin(m: number | null) {
  if (m == null) return "—";
  return m < 60 ? `${Math.round(m)}m` : `${(m / 60).toFixed(1)}h`;
}
function daysUntil(dateStr: string) {
  const diff = Math.round((new Date(dateStr + "T12:00:00Z").getTime() - Date.now()) / 86400_000);
  if (diff < 0)   return `${Math.abs(diff)}d ago`;
  if (diff === 0) return "today";
  if (diff === 1) return "tomorrow";
  return `in ${diff}d`;
}

function inferHeadlineTier(text: string): 1 | 2 | 3 {
  const t = text.toLowerCase();
  const t1 = ["fed rate cut", "rate hike", "bank failure", "default", "war declaration", "nuclear", "circuit breaker", "flash crash", "cpi beat", "jobs miss", "gdp miss", "recession"];
  const t2 = ["fed dovish", "fed hawkish", "tariff", "trade war", "earnings", "guidance", "layoffs", "bankruptcy", "sec investigation", "fed pivot", "rate cut"];
  if (t1.some(k => t.includes(k))) return 1;
  if (t2.some(k => t.includes(k))) return 2;
  return 3;
}

function parseSynthesisBreakdown(reasoning: string) {
  const m = reasoning.match(/Breakdown: Base: (-?\d+) \| Regime: ([+-]?\d+) \| RelStr: ([+-]?\d+) \| DXY: ([+-]?\d+) \| Corr: ([+-]?\d+) \| Geo: ([+-]?\d+) \| News: ([+-]?\d+) \| FINAL: (\d+)/);
  if (!m) return null;
  return { base: +m[1], regime: +m[2], relStr: +m[3], dxy: +m[4], corr: +m[5], geo: +m[6], news: +m[7], final: +m[8] };
}

function parseRegimeFromReasoning(reasoning: string): string {
  const m = reasoning.match(/\b(BEAR_MARKET|BULL_MARKET|CHOPPY|TRENDING_BULL|TRENDING_BEAR|VOLATILE)\b/);
  if (m) return m[1].replace("_MARKET", "").replace("TRENDING_", "TRENDING ");
  if (reasoning.toLowerCase().includes("bear")) return "BEAR";
  if (reasoning.toLowerCase().includes("bull")) return "BULL";
  return "—";
}

function parsePatternsFromMarketData(md?: string): string[] {
  if (!md) return [];
  try {
    const parsed = JSON.parse(md);
    return (parsed.patterns_detected ?? parsed.patterns ?? []) as string[];
  } catch { return []; }
}

function regimeBadgeStyle(regime: string): { bg: string; text: string; emoji: string } {
  const r = regime.toUpperCase();
  if (r.includes("BEAR")) return { bg: "bg-red-900/60 border-red-700",   text: "text-red-400",   emoji: "🔴" };
  if (r.includes("BULL")) return { bg: "bg-emerald-900/60 border-emerald-700", text: "text-emerald-400", emoji: "🟢" };
  return { bg: "bg-yellow-900/60 border-yellow-700", text: "text-yellow-400", emoji: "🟡" };
}

// ── Shared Small Components ───────────────────────────────────────────────────
function DecisionBadge({ decision }: { decision: string }) {
  const d = decision.toUpperCase();
  const cls = d === "BUY"  ? "bg-emerald-900/70 text-emerald-400 border-emerald-700" :
              d === "SELL" ? "bg-red-900/70 text-red-400 border-red-700" :
                             "bg-slate-700/70 text-slate-400 border-slate-600";
  return <span className={`inline-block px-2 py-0.5 rounded text-xs font-bold border ${cls}`}>{d}</span>;
}

function TierBadge({ tier }: { tier: 1 | 2 | 3 }) {
  const cls = tier === 1 ? "bg-red-900/60 text-red-400 border-red-700" :
              tier === 2 ? "bg-amber-900/60 text-amber-400 border-amber-700" :
                           "bg-slate-700/60 text-slate-500 border-slate-600";
  return <span className={`text-[9px] font-bold border rounded px-1 py-0 ${cls}`}>T{tier}</span>;
}

function SideBadge({ side }: { side: string }) {
  const isLong = side.toLowerCase() === "long" || side.toLowerCase() === "buy";
  return (
    <span className={`text-[10px] font-bold px-1.5 py-0.5 rounded border ${isLong ? "bg-emerald-900/50 text-emerald-400 border-emerald-700" : "bg-red-900/50 text-red-400 border-red-700"}`}>
      {isLong ? "LONG" : "SHORT"}
    </span>
  );
}

// ── Top Navigation Bar ────────────────────────────────────────────────────────
function TopNav({ activePage, setActivePage, regime, portfolioValue, portfolioDelta, positionsCount, lastRefresh, error }: {
  activePage: Page; setActivePage: (p: Page) => void;
  regime: string; portfolioValue: number; portfolioDelta: number;
  positionsCount: number; lastRefresh: Date; error: boolean;
}) {
  const rb   = regimeBadgeStyle(regime);
  const tabs: Page[] = ["HOME", "MARKET", "SIGNALS", "TRADES", "ANALYSIS"];
  const pPos = portfolioDelta >= 0;

  return (
    <nav className="fixed top-0 left-0 right-0 z-50 bg-slate-900/95 backdrop-blur-sm border-b border-slate-800 h-14 flex items-center px-3 gap-2 sm:gap-4">
      {/* Brand */}
      <div className="flex items-center gap-1.5 flex-shrink-0">
        <span className="text-sky-400 font-bold text-sm sm:text-base">⚡ Jim Bot</span>
        <span className={`hidden sm:inline-flex items-center gap-1 text-[10px] font-bold px-1.5 py-0.5 rounded border ${rb.bg} ${rb.text}`}>
          {rb.emoji} {regime === "UNKNOWN" ? "—" : regime.replace("_MARKET","").replace("TRENDING_","")}
        </span>
      </div>

      {/* Portfolio value */}
      <div className="flex-shrink-0 hidden sm:block">
        <div className="text-[10px] text-slate-600 leading-none">Portfolio</div>
        <div className={`text-sm font-bold leading-tight ${pPos ? "text-emerald-400" : "text-red-400"}`}>
          ${portfolioValue.toFixed(0)}
          <span className="text-[10px] font-semibold ml-1 opacity-80">({fmtPct(portfolioDelta)})</span>
        </div>
      </div>

      {/* Mobile portfolio */}
      <div className={`flex-shrink-0 sm:hidden text-sm font-bold ${pPos ? "text-emerald-400" : "text-red-400"}`}>
        ${portfolioValue.toFixed(0)}
      </div>

      {/* Tabs */}
      <div className="flex items-center gap-0 flex-1 justify-center">
        {tabs.map(tab => (
          <button
            key={tab}
            onClick={() => setActivePage(tab)}
            className={`px-2 sm:px-4 py-1.5 text-xs sm:text-sm font-semibold rounded transition-colors ${activePage === tab ? "bg-sky-600/20 text-sky-400" : "text-slate-500 hover:text-slate-300"}`}
          >
            {tab}
          </button>
        ))}
      </div>

      {/* Right info */}
      <div className="flex items-center gap-2 flex-shrink-0 text-right">
        <div className="hidden sm:flex items-center gap-1.5">
          <span className="text-[10px] text-slate-600">{positionsCount} pos</span>
          <span className={`w-2 h-2 rounded-full ${error ? "bg-red-400" : "bg-emerald-400"}`} title={error ? "Error" : "Running"} />
        </div>
        <div className="text-[10px] text-slate-600 hidden sm:block">{lastRefresh.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" })}</div>
      </div>
    </nav>
  );
}

// ── HOME PAGE ────────────────────────────────────────────────────────────────
function PositionRow({ pos, decisions, partialProfits, stops, totalPortfolio }: {
  pos: Position; decisions: Decision[];
  partialProfits: PartialProfits; stops: Stops; totalPortfolio: number;
}) {
  const [expanded, setExpanded] = useState(false);
  const pnl     = pos.unrealized_pl;
  const pnlPct  = pos.unrealized_plpc;
  const isPos   = pnl >= 0;
  const secured = partialProfits[pos.symbol]?.secured_pnl ?? 0;
  const stopVal = stops[pos.symbol] ?? (pos.side === "long"
    ? pos.current_price * 0.95
    : pos.current_price * 1.03);
  const sizePct = (pos.cost_basis / Math.max(totalPortfolio, INITIAL_CAPITAL)) * 100;
  const latestDec = decisions.find(d => d.symbol === pos.symbol || d.symbol === pos.symbol.replace("/",""));
  const breakdown = latestDec ? parseSynthesisBreakdown(latestDec.reasoning) : null;
  const patterns  = latestDec ? parsePatternsFromMarketData(latestDec.market_data) : [];
  const ticker    = pos.symbol.replace("/USD","");

  return (
    <>
      <tr
        className={`border-l-2 cursor-pointer hover:bg-slate-800/60 transition-colors ${isPos ? "border-emerald-600/70" : "border-red-600/70"}`}
        onClick={() => setExpanded(v => !v)}
      >
        <td className="px-3 py-2.5">
          <div className="flex items-center gap-2">
            <span className="font-bold text-white text-sm">{ticker}</span>
            <SideBadge side={pos.side} />
          </div>
        </td>
        <td className="px-3 py-2.5 text-xs text-slate-300 font-mono">${fmtPrice(pos.entry_price)}</td>
        <td className="px-3 py-2.5 text-xs text-slate-200 font-mono">${fmtPrice(pos.current_price)}</td>
        <td className="px-3 py-2.5">
          <div className={`text-xs font-bold ${isPos ? "text-emerald-400" : "text-red-400"}`}>{fmtPnl(pnl)}</div>
          <div className={`text-[10px] ${isPos ? "text-emerald-500" : "text-red-500"}`}>{fmtPct(pnlPct)}</div>
        </td>
        <td className="px-3 py-2.5">
          {secured > 0
            ? <span className="text-[10px] font-semibold bg-emerald-900/40 border border-emerald-700/50 text-emerald-400 rounded px-1.5 py-0.5">✅ +${secured.toFixed(2)}</span>
            : <span className="text-[10px] text-slate-700">—</span>}
        </td>
        <td className="px-3 py-2.5 text-xs font-mono text-amber-400">${fmtPrice(stopVal)}</td>
        <td className="px-3 py-2.5 text-xs text-slate-400">{sizePct.toFixed(1)}%</td>
        <td className="px-3 py-2.5 text-slate-600 text-xs">{expanded ? "▲" : "▼"}</td>
      </tr>
      {expanded && (
        <tr className={`border-l-2 ${isPos ? "border-emerald-600/40" : "border-red-600/40"}`}>
          <td colSpan={8} className="px-4 pb-4 pt-1 bg-slate-800/40">
            <div className="grid grid-cols-1 sm:grid-cols-2 gap-4 mt-2">
              {/* Position details */}
              <div className="space-y-2">
                <div className="text-[10px] text-slate-500 uppercase tracking-wider font-semibold mb-1">Position Details</div>
                {[
                  ["Qty",        pos.qty.toString()],
                  ["Cost Basis", "$" + fmtPrice(pos.cost_basis)],
                  ["Market Val", "$" + fmtPrice(pos.market_value)],
                  ["~Stop",      "$" + fmtPrice(stopVal)],
                  ["Size %",     sizePct.toFixed(1) + "% of portfolio"],
                ].map(([k, v]) => (
                  <div key={k} className="flex justify-between text-xs">
                    <span className="text-slate-500">{k}</span>
                    <span className="text-slate-300 font-mono">{v}</span>
                  </div>
                ))}
                {patterns.length > 0 && (
                  <div className="flex items-center gap-1 flex-wrap mt-1">
                    {patterns.map(p => (
                      <span key={p} className="text-[9px] bg-violet-900/40 text-violet-400 border border-violet-700/50 rounded px-1.5 py-0.5">{p}</span>
                    ))}
                  </div>
                )}
              </div>
              {/* Score breakdown + Claude reasoning */}
              <div>
                {breakdown && (
                  <div className="mb-2">
                    <div className="text-[10px] text-slate-500 uppercase tracking-wider font-semibold mb-1">Synthesis Score</div>
                    <div className="grid grid-cols-4 gap-1">
                      {[
                        ["Base", breakdown.base],
                        ["Regime", breakdown.regime],
                        ["RelStr", breakdown.relStr],
                        ["DXY", breakdown.dxy],
                        ["Corr", breakdown.corr],
                        ["Geo", breakdown.geo],
                        ["News", breakdown.news],
                        ["FINAL", breakdown.final],
                      ].map(([label, val]) => (
                        <div key={label as string} className={`text-center rounded p-1 ${label === "FINAL" ? "bg-sky-900/40 col-span-2" : "bg-slate-800"}`}>
                          <div className="text-[8px] text-slate-600 uppercase">{label as string}</div>
                          <div className={`text-xs font-bold ${typeof val === "number" && val > 0 ? "text-emerald-400" : typeof val === "number" && val < 0 ? "text-red-400" : "text-slate-300"}`}>
                            {typeof val === "number" && label !== "Base" && label !== "FINAL" && val > 0 ? "+" : ""}{val}
                          </div>
                        </div>
                      ))}
                    </div>
                  </div>
                )}
                {latestDec && (
                  <div>
                    <div className="text-[10px] text-slate-500 uppercase tracking-wider font-semibold mb-1">
                      Claude — {fmtDateTime(latestDec.decided_at)}
                    </div>
                    <div className="bg-slate-900/60 rounded p-2 max-h-28 overflow-y-auto border-l-2 border-sky-700/40">
                      <p className="text-[10px] text-slate-400 leading-relaxed whitespace-pre-wrap">
                        {latestDec.reasoning.replace(/Breakdown:.*$/m, "").trim().slice(0, 400)}
                        {latestDec.reasoning.length > 400 ? "…" : ""}
                      </p>
                    </div>
                  </div>
                )}
              </div>
            </div>
          </td>
        </tr>
      )}
    </>
  );
}

function ReasonBadge({ reasons }: { reasons: string }) {
  const r = reasons.toLowerCase();
  if (r.includes("partial"))  return <span className="text-[9px] bg-sky-900/40 text-sky-400 border border-sky-700/40 rounded px-1.5 py-0.5">partial</span>;
  if (r.includes("stop"))     return <span className="text-[9px] bg-red-900/40 text-red-400 border border-red-700/40 rounded px-1.5 py-0.5">stop</span>;
  if (r.includes("target"))   return <span className="text-[9px] bg-emerald-900/40 text-emerald-400 border border-emerald-700/40 rounded px-1.5 py-0.5">target</span>;
  return <span className="text-[9px] bg-slate-700/40 text-slate-500 border border-slate-600/40 rounded px-1.5 py-0.5">{reasons.split(",")[0]}</span>;
}

function EquityCurve({ data }: { data: { date: string; pnl: number }[] }) {
  if (data.length < 2) return null;
  let cum = 0;
  const points = data.map(d => { cum += d.pnl; return cum; });
  const maxV = Math.max(...points.map(Math.abs), 0.01);
  const W = 400; const H = 64;
  const xs = points.map((_, i) => (i / Math.max(points.length - 1, 1)) * W);
  const ys = points.map(p => H / 2 - (p / maxV) * (H / 2 - 5));
  const pathD = xs.map((x, i) => `${i === 0 ? "M" : "L"} ${x.toFixed(1)} ${ys[i].toFixed(1)}`).join(" ");
  const areaD = `${pathD} L ${W} ${H / 2} L 0 ${H / 2} Z`;
  const lastVal = points[points.length - 1];
  const col = lastVal >= 0 ? "#10b981" : "#ef4444";
  const gradId = `eq-${data.length}`;
  return (
    <svg viewBox={`0 0 ${W} ${H}`} width="100%" height={H} preserveAspectRatio="none">
      <defs>
        <linearGradient id={gradId} x1="0" y1="0" x2="0" y2="1">
          <stop offset="0%" stopColor={col} stopOpacity="0.25" />
          <stop offset="100%" stopColor={col} stopOpacity="0" />
        </linearGradient>
      </defs>
      <line x1={0} y1={H / 2} x2={W} y2={H / 2} stroke="#334155" strokeWidth="1" />
      <path d={areaD} fill={`url(#${gradId})`} />
      <path d={pathD} stroke={col} strokeWidth="1.5" fill="none" strokeLinecap="round" strokeLinejoin="round" />
      {xs.map((x, i) => (
        <circle key={i} cx={x} cy={ys[i]} r="2" fill={col} opacity="0.7" />
      ))}
    </svg>
  );
}

// ── ANALYSIS PAGE ────────────────────────────────────────────────────────────
function StatCard({ label, value, sub, color }: { label: string; value: string; sub?: string; color?: string }) {
  return (
    <div className="bg-slate-800 rounded-xl p-4 border border-slate-700/50">
      <div className="text-[10px] uppercase tracking-wider text-slate-500 font-semibold mb-1">{label}</div>
      <div className={`text-xl font-bold font-mono ${color ?? "text-white"}`}>{value}</div>
      {sub && <div className="text-[10px] text-slate-600 mt-0.5">{sub}</div>}
    </div>
  );
}

function AnalysisPage() {
  const [expert,  setExpert]  = useState<ExpertFilter>("all");
  const [data,    setData]    = useState<AnalysisData | null>(null);
  const [loading, setLoading] = useState(true);

  useEffect(() => {
    setLoading(true);
    fetch(`${BASE}/api/analysis?expert=${expert}`)
      .then(r => r.json())
      .then(d => { setData((d as AnalysisData).total_trades !== undefined ? d as AnalysisData : null); })
      .catch(() => setData(null))
      .finally(() => setLoading(false));
  }, [expert]);

  const accentLabel = expert === "gap"
    ? <span className="text-amber-400 font-semibold">🚀 Gap — Stocks</span>
    : expert === "geo"
    ? <span className="text-violet-400 font-semibold">📐 Geo — Crypto</span>
    : <span className="text-slate-400">Tous les experts</span>;

  if (loading) {
    return (
      <div className="p-4 sm:p-6 space-y-4">
        <div className="flex items-center gap-3"><ExpertPills value={expert} onChange={setExpert} />{accentLabel}</div>
        <div className="p-6 flex items-center justify-center min-h-[200px] text-slate-600 text-sm">Chargement…</div>
      </div>
    );
  }

  if (!data || data.total_trades === 0) {
    return (
      <div className="p-4 sm:p-6 space-y-4">
        <div className="flex items-center gap-3"><ExpertPills value={expert} onChange={setExpert} />{accentLabel}</div>
        <div className="p-6 flex items-center justify-center min-h-[200px] text-slate-600 text-sm">
          Aucun trade fermé pour {expert === "all" ? "ce portefeuille" : expert === "gap" ? "l'expert Gap" : "l'expert Geo"} — l'analyse apparaîtra après le premier trade.
        </div>
      </div>
    );
  }

  const pfLabel   = data.profit_factor === 999 ? "∞" : data.profit_factor.toFixed(2);
  const exSign    = data.expectancy >= 0 ? "+" : "";
  const holdLabel = data.avg_hold_min >= 60
    ? `${(data.avg_hold_min / 60).toFixed(1)}h`
    : `${data.avg_hold_min.toFixed(0)}m`;
  const streakCol = data.current_streak?.type === "win" ? "text-emerald-400" : "text-red-400";
  const streakLbl = data.current_streak
    ? `${data.current_streak.count} ${data.current_streak.type} streak`
    : "—";

  const maxAssetPnl = Math.max(...data.by_asset.map(a => Math.abs(a.pnl)), 0.01);

  return (
    <div className="p-4 sm:p-6 space-y-6">
      <div className="flex items-center gap-3 flex-wrap">
        <ExpertPills value={expert} onChange={setExpert} />
        {accentLabel}
      </div>

      {/* ── KPI Row 1 ─────────────────────────────────────────────── */}
      <div className="grid grid-cols-2 sm:grid-cols-4 gap-3">
        <StatCard label="Win Rate"      value={`${data.win_rate.toFixed(1)}%`}
          sub={`${data.winning_trades}W / ${data.losing_trades}L`}
          color={data.win_rate >= 50 ? "text-emerald-400" : "text-red-400"} />
        <StatCard label="Profit Factor" value={pfLabel}
          sub={`Gross win $${data.gross_win.toFixed(2)}`}
          color={data.profit_factor >= 1 ? "text-emerald-400" : "text-red-400"} />
        <StatCard label="Expectancy"    value={`${exSign}$${data.expectancy.toFixed(4)}`}
          sub="avg $ per trade"
          color={data.expectancy >= 0 ? "text-sky-400" : "text-red-400"} />
        <StatCard label="Avg Hold"      value={holdLabel}
          sub={`${data.avg_trades_per_day.toFixed(1)} trades/day`} />
      </div>

      {/* ── KPI Row 2 ─────────────────────────────────────────────── */}
      <div className="grid grid-cols-2 sm:grid-cols-4 gap-3">
        <StatCard label="Total Trades"  value={`${data.total_trades}`}
          sub={`${data.avg_trades_per_day.toFixed(1)} per active day`} />
        <StatCard label="Best Trade"
          value={data.best_trade ? `+$${data.best_trade.pnl.toFixed(4)}` : "—"}
          sub={data.best_trade?.symbol}  color="text-emerald-400" />
        <StatCard label="Worst Trade"
          value={data.worst_trade ? `$${data.worst_trade.pnl.toFixed(4)}` : "—"}
          sub={data.worst_trade?.symbol} color={data.worst_trade && data.worst_trade.pnl < 0 ? "text-red-400" : "text-emerald-400"} />
        <StatCard label="Win Streak"    value={streakLbl}
          sub={`Max: ${data.max_win_streak}W / ${data.max_loss_streak}L`}
          color={streakCol} />
      </div>

      {/* ── P&L by Asset ──────────────────────────────────────────── */}
      <div className="bg-slate-800 rounded-xl border border-slate-700/50 overflow-hidden">
        <div className="px-4 py-3 border-b border-slate-700/50">
          <span className="text-sm font-bold text-white">P&L by Asset</span>
        </div>
        <div className="p-4 space-y-3">
          {data.by_asset.map(a => {
            const isPos  = a.pnl >= 0;
            const barPct = (Math.abs(a.pnl) / maxAssetPnl) * 100;
            return (
              <div key={a.symbol}>
                <div className="flex items-center justify-between mb-1">
                  <div className="flex items-center gap-2">
                    <span className="text-sm font-bold text-white">{a.symbol.replace("/USD", "")}</span>
                    <span className="text-[10px] text-slate-500">{a.trades} trades · avg {a.avg_hold_min >= 60 ? `${(a.avg_hold_min/60).toFixed(1)}h` : `${a.avg_hold_min.toFixed(0)}m`}</span>
                  </div>
                  <div className="text-right">
                    <span className={`text-sm font-bold font-mono ${isPos ? "text-emerald-400" : "text-red-400"}`}>
                      {isPos ? "+" : ""}${a.pnl.toFixed(4)}
                    </span>
                    <span className="text-[10px] text-slate-500 ml-2">avg {isPos ? "+" : ""}${a.avg_pnl.toFixed(4)}</span>
                  </div>
                </div>
                <div className="h-1.5 bg-slate-700 rounded-full overflow-hidden">
                  <div
                    className={`h-full rounded-full transition-all ${isPos ? "bg-emerald-500/70" : "bg-red-500/70"}`}
                    style={{ width: `${barPct}%` }}
                  />
                </div>
              </div>
            );
          })}
        </div>
      </div>

      {/* ── Win / Loss Profile ────────────────────────────────────── */}
      <div className="bg-slate-800 rounded-xl border border-slate-700/50 overflow-hidden">
        <div className="px-4 py-3 border-b border-slate-700/50">
          <span className="text-sm font-bold text-white">Win / Loss Profile</span>
        </div>
        <div className="p-4 space-y-3">
          <div>
            <div className="flex justify-between text-xs mb-1">
              <span className="text-emerald-400 font-semibold">Avg Win</span>
              <span className="text-emerald-400 font-mono font-bold">+${data.avg_win.toFixed(4)}</span>
            </div>
            <div className="h-2 bg-slate-700 rounded-full overflow-hidden">
              <div className="h-full bg-emerald-500/70 rounded-full"
                style={{ width: `${Math.min((data.avg_win / Math.max(data.avg_win, Math.abs(data.avg_loss || 0.0001))) * 100, 100)}%` }} />
            </div>
          </div>
          {data.avg_loss !== 0 && (
            <div>
              <div className="flex justify-between text-xs mb-1">
                <span className="text-red-400 font-semibold">Avg Loss</span>
                <span className="text-red-400 font-mono font-bold">${data.avg_loss.toFixed(4)}</span>
              </div>
              <div className="h-2 bg-slate-700 rounded-full overflow-hidden">
                <div className="h-full bg-red-500/70 rounded-full"
                  style={{ width: `${Math.min((Math.abs(data.avg_loss) / Math.max(data.avg_win, Math.abs(data.avg_loss))) * 100, 100)}%` }} />
              </div>
            </div>
          )}
          <div className="flex justify-between text-xs text-slate-500 pt-1 border-t border-slate-700/40">
            <span>
              Reward/Risk <span className="text-white font-semibold">
                {data.avg_loss !== 0 ? (data.avg_win / Math.abs(data.avg_loss)).toFixed(2) : "∞"}x
              </span>
            </span>
            <span>
              Expectancy <span className={`font-semibold ${data.expectancy >= 0 ? "text-emerald-400" : "text-red-400"}`}>
                {data.expectancy >= 0 ? "+" : ""}${data.expectancy.toFixed(4)}
              </span>
            </span>
          </div>
        </div>
      </div>

      {/* ── Equity Curve ──────────────────────────────────────────── */}
      {data.daily_pnl.length >= 2 && (
        <div className="bg-slate-800 rounded-xl border border-slate-700/50 overflow-hidden">
          <div className="px-4 py-3 border-b border-slate-700/50 flex items-center justify-between">
            <span className="text-sm font-bold text-white">Equity Curve</span>
            <span className={`text-sm font-bold font-mono ${data.total_pnl >= 0 ? "text-emerald-400" : "text-red-400"}`}>
              {data.total_pnl >= 0 ? "+" : ""}${data.total_pnl.toFixed(4)} cumulative
            </span>
          </div>
          <div className="p-4">
            <EquityCurve data={data.daily_pnl} />
          </div>
        </div>
      )}

      {/* ── Close Reason + Daily P&L side-by-side ─────────────────── */}
      <div className="grid grid-cols-1 sm:grid-cols-2 gap-4">
        {/* Close Reasons */}
        <div className="bg-slate-800 rounded-xl border border-slate-700/50 overflow-hidden">
          <div className="px-4 py-3 border-b border-slate-700/50">
            <span className="text-sm font-bold text-white">Exit Reasons</span>
          </div>
          <div className="p-3 space-y-2">
            {data.by_reason.map(r => (
              <div key={r.reason} className="flex items-center justify-between">
                <div className="flex items-center gap-2">
                  <ReasonBadge reasons={r.reason} />
                  <span className="text-xs text-slate-400">{r.trades} trades</span>
                </div>
                <span className={`text-xs font-mono font-bold ${r.pnl >= 0 ? "text-emerald-400" : "text-red-400"}`}>
                  {r.pnl >= 0 ? "+" : ""}${r.pnl.toFixed(4)}
                </span>
              </div>
            ))}
          </div>
        </div>

        {/* Daily P&L */}
        <div className="bg-slate-800 rounded-xl border border-slate-700/50 overflow-hidden">
          <div className="px-4 py-3 border-b border-slate-700/50">
            <span className="text-sm font-bold text-white">Daily P&L</span>
          </div>
          <div className="divide-y divide-slate-700/30">
            {data.daily_pnl.length === 0 ? (
              <div className="p-4 text-xs text-slate-600">No data</div>
            ) : data.daily_pnl.map(d => (
              <div key={d.date} className="flex items-center justify-between px-4 py-2">
                <div className="flex items-center gap-2">
                  <span className="text-xs text-slate-400">{d.date}</span>
                  <span className="text-[10px] text-slate-600">{d.trades} trades</span>
                </div>
                <span className={`text-xs font-mono font-bold ${d.pnl >= 0 ? "text-emerald-400" : "text-red-400"}`}>
                  {d.pnl >= 0 ? "+" : ""}${d.pnl.toFixed(4)}
                </span>
              </div>
            ))}
          </div>
        </div>
      </div>
    </div>
  );
}

// ── TRADES PAGE ───────────────────────────────────────────────────────────────
function TradesPage({ positions, decisions, partialProfits, stops, totalPortfolio, closedToday, closedPeriod, setClosedPeriod }: {
  positions: Position[]; decisions: Decision[];
  partialProfits: PartialProfits; stops: Stops; totalPortfolio: number;
  closedToday: ClosedTodayItem[];
  closedPeriod: ClosedPeriod; setClosedPeriod: (p: ClosedPeriod) => void;
}) {
  const closedTotalPnl      = (closedToday ?? []).reduce((s, c) => s + c.pnl, 0);
  const unrealizedTotalPos  = positions.reduce((s, p) => s + p.unrealized_pl, 0);
  const securedTotalPos     = Object.values(partialProfits).reduce((s, p) => s + p.secured_pnl, 0);
  const allocatedTotalPct   = positions.reduce((s, p) => s + p.cost_basis, 0) / Math.max(totalPortfolio, INITIAL_CAPITAL) * 100;
  const PERIODS: { key: ClosedPeriod; label: string }[] = [
    { key: "today", label: "Today" },
    { key: "week",  label: "Week"  },
    { key: "month", label: "Month" },
    { key: "ytd",   label: "YTD"   },
    { key: "all",   label: "All"   },
  ];

  const [tradeView,        setTradeView]        = useState<"grouped" | "individual">("individual");
  const [closedIndividual, setClosedIndividual] = useState<IndividualTrade[]>([]);

  useEffect(() => {
    const load = async () => {
      try {
        const res = await fetch(`${BASE}/api/trades/individual?period=${closedPeriod}`);
        if (res.ok) { const d = await res.json(); setClosedIndividual(d.trades ?? []); }
      } catch { /* silent */ }
    };
    load();
    const id = setInterval(load, 15_000);
    return () => clearInterval(id);
  }, [closedPeriod]);

  const indTotalPnl = closedIndividual.reduce((s, t) => s + (t.pnl ?? 0), 0);
  const indWins     = closedIndividual.filter(t => (t.pnl ?? 0) > 0).length;
  const indLosses   = closedIndividual.filter(t => (t.pnl ?? 0) < 0).length;

  return (
    <div className="p-4 sm:p-6 space-y-5">
      <div className="flex items-center justify-between">
        <h2 className="text-base font-bold text-white">Open Positions</h2>
        <span className="text-xs text-slate-600">{positions.length} active</span>
      </div>
      {positions.length === 0 ? (
        <div className="bg-slate-800/50 rounded-xl p-10 text-center text-slate-600 text-sm">
          No open positions
        </div>
      ) : (
        <div className="bg-slate-800 rounded-xl overflow-hidden border border-slate-700/50">
          <div className="overflow-x-auto">
            <table className="w-full">
              <thead>
                <tr className="border-b border-slate-700">
                  {["Asset","Entry","Current","Unrealized","Secured","~Stop","Size",""].map(h => (
                    <th key={h} className="px-3 py-2 text-left text-[10px] uppercase tracking-wider text-slate-500 font-semibold">{h}</th>
                  ))}
                </tr>
              </thead>
              <tbody className="divide-y divide-slate-700/50">
                {positions.map(p => (
                  <PositionRow key={p.symbol} pos={p} decisions={decisions}
                    partialProfits={partialProfits} stops={stops} totalPortfolio={totalPortfolio} />
                ))}
              </tbody>
              <tfoot>
                <tr className="border-t-2 border-slate-600 bg-slate-900/50">
                  <td className="px-3 py-2 text-[10px] text-slate-500 uppercase font-semibold tracking-wider" colSpan={2}>Total</td>
                  <td />
                  <td className="px-3 py-2">
                    <div className={`text-xs font-bold ${unrealizedTotalPos >= 0 ? "text-emerald-400" : "text-red-400"}`}>{fmtPnl(unrealizedTotalPos)}</div>
                  </td>
                  <td className="px-3 py-2">
                    {securedTotalPos > 0 && <span className="text-[10px] font-semibold text-emerald-400">✅ +${securedTotalPos.toFixed(2)}</span>}
                  </td>
                  <td />
                  <td className="px-3 py-2 text-xs font-semibold text-slate-400">{allocatedTotalPct.toFixed(1)}%</td>
                  <td />
                </tr>
              </tfoot>
            </table>
          </div>
        </div>
      )}

      {/* ── Closed Trades ─────────────────────────────────────────── */}
      <div>
        {/* Header row: title + period pills + view toggle + total */}
        <div className="flex flex-wrap items-center gap-2 mb-3">
          <h2 className="text-base font-bold text-white">Closed</h2>

          {/* Period pills */}
          <div className="flex items-center gap-0.5 bg-slate-800 rounded-lg p-0.5 border border-slate-700/50">
            {PERIODS.map(p => (
              <button key={p.key} onClick={() => setClosedPeriod(p.key)}
                className={`px-2 py-0.5 text-[10px] font-semibold rounded transition-colors ${closedPeriod === p.key ? "bg-sky-600/30 text-sky-400" : "text-slate-500 hover:text-slate-300"}`}>
                {p.label}
              </button>
            ))}
          </div>

          {/* View toggle */}
          <div className="flex items-center gap-0.5 bg-slate-800 rounded-lg p-0.5 border border-slate-700/50">
            <button onClick={() => setTradeView("individual")}
              className={`px-2 py-0.5 text-[10px] font-semibold rounded transition-colors ${tradeView === "individual" ? "bg-violet-600/30 text-violet-400" : "text-slate-500 hover:text-slate-300"}`}>
              Each Trade
            </button>
            <button onClick={() => setTradeView("grouped")}
              className={`px-2 py-0.5 text-[10px] font-semibold rounded transition-colors ${tradeView === "grouped" ? "bg-violet-600/30 text-violet-400" : "text-slate-500 hover:text-slate-300"}`}>
              By Asset
            </button>
          </div>

          {/* Summary totals */}
          <div className="ml-auto flex items-center gap-3">
            {tradeView === "individual" && closedIndividual.length > 0 && (
              <>
                <span className="text-[10px] text-slate-500">
                  <span className="text-emerald-500">{indWins}W</span>
                  {" / "}
                  <span className="text-red-500">{indLosses}L</span>
                </span>
                <span className={`text-sm font-bold font-mono ${indTotalPnl >= 0 ? "text-emerald-400" : "text-red-400"}`}>
                  {indTotalPnl >= 0 ? "+" : "-"}${Math.abs(indTotalPnl).toFixed(4)}
                </span>
              </>
            )}
            {tradeView === "grouped" && (closedToday ?? []).length > 0 && (
              <span className={`text-sm font-bold font-mono ${closedTotalPnl >= 0 ? "text-emerald-400" : "text-red-400"}`}>
                {closedTotalPnl >= 0 ? "+" : "-"}${Math.abs(closedTotalPnl).toFixed(4)}
              </span>
            )}
          </div>
        </div>

        {/* ── Individual view ── */}
        {tradeView === "individual" && (
          closedIndividual.length === 0 ? (
            <div className="bg-slate-800/50 rounded-xl p-8 text-center text-slate-600 text-sm">
              No closed trades for this period
            </div>
          ) : (
            <div className="bg-slate-800 rounded-xl overflow-hidden border border-slate-700/50">
              <div className="overflow-x-auto">
                <table className="w-full">
                  <thead>
                    <tr className="border-b border-slate-700">
                      {["Asset","Realized P&L","Entry → Exit","Hold","Exit Reason","Detail","Time"].map(h => (
                        <th key={h} className="px-3 py-2 text-left text-[10px] uppercase tracking-wider text-slate-500 font-semibold">{h}</th>
                      ))}
                    </tr>
                  </thead>
                  <tbody className="divide-y divide-slate-700/50">
                    {closedIndividual.map(t => {
                      const isPos   = (t.pnl ?? 0) >= 0;
                      const isLong  = t.side === "buy" || t.side === "long";
                      return (
                        <tr key={t.trade_id} className={`border-l-2 ${isPos ? "border-emerald-600/70" : "border-red-600/70"}`}>
                          <td className="px-3 py-2">
                            <span className="font-bold text-white text-sm">{t.symbol.replace("/USD","")}</span>
                            <span className={`ml-1.5 text-[10px] font-bold ${isLong ? "text-emerald-500" : "text-red-500"}`}>
                              {isLong ? "↑LONG" : "↓SHORT"}
                            </span>
                          </td>
                          <td className="px-3 py-2">
                            <span className={`text-sm font-bold font-mono ${isPos ? "text-emerald-400" : "text-red-400"}`}>
                              {t.pnl != null ? ((isPos ? "+" : "-") + "$" + Math.abs(t.pnl).toFixed(4)) : "—"}
                            </span>
                            {t.pnl_pct != null && (
                              <span className={`ml-1 text-[10px] ${isPos ? "text-emerald-500" : "text-red-500"}`}>
                                ({t.pnl_pct >= 0 ? "+" : ""}{t.pnl_pct.toFixed(2)}%)
                              </span>
                            )}
                          </td>
                          <td className="px-3 py-2 text-[11px] text-slate-400 font-mono whitespace-nowrap">
                            {t.entry_price != null ? `$${t.entry_price}` : "—"}
                            <span className="text-slate-600"> → </span>
                            {t.exit_price != null ? `$${t.exit_price}` : "—"}
                          </td>
                          <td className="px-3 py-2 text-xs text-slate-400">{fmtHoldMin(t.hold_min)}</td>
                          <td className="px-3 py-2"><ReasonBadge reasons={t.close_reason ?? ""} /></td>
                          <td className="px-3 py-2 text-[10px] text-slate-500 whitespace-nowrap">
                            {t.exit_vs_target != null && (
                              <span className={`mr-1.5 font-semibold ${t.exit_vs_target >= 100 ? "text-emerald-400" : t.exit_vs_target >= 50 ? "text-sky-400" : "text-red-400"}`}>
                                {t.exit_vs_target}%obj
                              </span>
                            )}
                            {t.score != null && <span className="mr-1.5">{Math.round(t.score)}/100</span>}
                            {t.regime && <span className="opacity-50">{t.regime}</span>}
                          </td>
                          <td className="px-3 py-2 text-[10px] text-slate-600 whitespace-nowrap">
                            {t.exit_at ? (closedPeriod === "today" ? fmtTime(t.exit_at) : fmtDateTime(t.exit_at)) : "—"}
                          </td>
                        </tr>
                      );
                    })}
                  </tbody>
                </table>
              </div>
            </div>
          )
        )}

        {/* ── Grouped view ── */}
        {tradeView === "grouped" && (
          (closedToday ?? []).length === 0 ? (
            <div className="bg-slate-800/50 rounded-xl p-8 text-center text-slate-600 text-sm">
              No closed trades for this period
            </div>
          ) : (
            <div className="bg-slate-800 rounded-xl overflow-hidden border border-slate-700/50">
              <table className="w-full">
                <thead>
                  <tr className="border-b border-slate-700">
                    {["Asset","Realized P&L","# Trades","Qty Sold","Last Exit","Type"].map(h => (
                      <th key={h} className="px-3 py-2 text-left text-[10px] uppercase tracking-wider text-slate-500 font-semibold">{h}</th>
                    ))}
                  </tr>
                </thead>
                <tbody className="divide-y divide-slate-700/50">
                  {closedToday.map(c => {
                    const isPos = c.pnl >= 0;
                    return (
                      <tr key={c.symbol} className={`border-l-2 ${isPos ? "border-emerald-600/70" : "border-red-600/70"}`}>
                        <td className="px-3 py-2.5">
                          <span className="font-bold text-white text-sm">{c.symbol.replace("/USD","")}</span>
                        </td>
                        <td className="px-3 py-2.5">
                          <span className={`text-sm font-bold font-mono ${isPos ? "text-emerald-400" : "text-red-400"}`}>
                            {isPos ? "+" : "-"}${Math.abs(c.pnl).toFixed(4)}
                          </span>
                        </td>
                        <td className="px-3 py-2.5 text-xs text-slate-400">{c.trade_count}</td>
                        <td className="px-3 py-2.5 text-xs text-slate-400 font-mono">{c.qty_sold.toFixed(6)}</td>
                        <td className="px-3 py-2.5 text-xs text-slate-500">{c.last_exit ? (closedPeriod === "today" ? fmtTime(c.last_exit) : fmtDateTime(c.last_exit)) : "—"}</td>
                        <td className="px-3 py-2.5"><ReasonBadge reasons={c.reasons} /></td>
                      </tr>
                    );
                  })}
                </tbody>
              </table>
            </div>
          )
        )}
      </div>
    </div>
  );
}

// ── MARKET PAGE ───────────────────────────────────────────────────────────────
function MarketPage({ movers, sentiment, regime }: {
  movers: Mover[]; sentiment: SentimentResponse | null; regime: RegimeResponse | null;
}) {
  const [calHovered, setCalHovered] = useState<string | null>(null);
  const [mobileSection, setMobileSection] = useState<"movers" | "calendar" | null>(null);

  const regimeLabel = regime?.regime ?? "UNKNOWN";
  const rb          = regimeBadgeStyle(regimeLabel);
  const sentColor   = sentiment?.sentiment?.includes("bull") ? "text-emerald-400" :
                      sentiment?.sentiment?.includes("bear") ? "text-red-400" : "text-slate-400";
  const headlines   = sentiment?.headlines ?? [];
  const alerts      = sentiment?.alerts ?? [];
  const trumpSignal = alerts.find(a => a.toLowerCase().includes("trump")) ?? null;
  const upcoming    = EARNINGS_CALENDAR
    .filter(e => { const d = new Date(e.date + "T12:00:00Z"); return d >= new Date(Date.now() - 86400_000); })
    .sort((a, b) => a.date.localeCompare(b.date))
    .slice(0, 6);

  return (
    <div className="p-4 sm:p-6">
      {/* Desktop: 3 equal columns */}
      <div className="hidden sm:grid grid-cols-3 gap-4">
        {/* LEFT: Top Movers */}
        <div className="bg-slate-800 rounded-xl border border-slate-700/50 overflow-hidden">
          <div className="px-4 py-3 border-b border-slate-700 flex items-center justify-between">
            <span className="text-xs font-bold text-slate-300 uppercase tracking-wider">Top Movers</span>
            <span className="text-[9px] text-slate-600">60s refresh</span>
          </div>
          <div className="p-3 space-y-1.5">
            {movers.length === 0
              ? <div className="text-xs text-slate-600 text-center py-6">Fetching movers…</div>
              : movers.map(m => (
                <div key={m.symbol} className="flex items-center justify-between py-1.5 border-b border-slate-700/30 last:border-0">
                  <span className="text-sm font-bold text-white">{m.symbol.replace("/USD","")}</span>
                  <div className="flex items-center gap-2">
                    <span className="text-xs font-mono text-slate-400">${fmtPrice(m.price)}</span>
                    <span className={`text-xs font-bold ${m.direction === "up" ? "text-emerald-400" : "text-red-400"}`}>
                      {m.direction === "up" ? "▲" : "▼"} {Math.abs(m.change_pct).toFixed(2)}%
                    </span>
                  </div>
                </div>
              ))}
          </div>
        </div>

        {/* CENTER: Market Sentiment + Regime */}
        <SentimentColumn sentiment={sentiment} regime={regime} headlines={headlines}
          trumpSignal={trumpSignal} rb={rb} regimeLabel={regimeLabel} sentColor={sentColor} />

        {/* RIGHT: Calendar */}
        <div className="bg-slate-800 rounded-xl border border-slate-700/50 overflow-hidden">
          <div className="px-4 py-3 border-b border-slate-700">
            <span className="text-xs font-bold text-slate-300 uppercase tracking-wider">Calendar</span>
          </div>
          <div className="p-3 space-y-2">
            <div className="text-[10px] text-slate-500 uppercase tracking-wider mb-1">Macro Events</div>
            {MACRO_EVENTS.map(e => (
              <div key={e.event} className="bg-slate-900/40 rounded-lg p-2.5 border border-slate-700/30">
                <div className="flex items-center justify-between">
                  <span className="text-xs font-semibold text-slate-300">{e.event}</span>
                  <span className="text-[10px] text-amber-400 font-mono">{daysUntil(e.date)}</span>
                </div>
                <div className="text-[10px] text-slate-600 mt-0.5">{e.note}</div>
              </div>
            ))}
            <div className="text-[10px] text-slate-500 uppercase tracking-wider mt-3 mb-1">Earnings</div>
            {upcoming.map(e => (
              <div key={e.symbol}
                className="bg-slate-900/40 rounded-lg p-2.5 border border-slate-700/30 cursor-pointer hover:border-amber-700/50 transition-colors"
                onMouseEnter={() => setCalHovered(e.symbol)}
                onMouseLeave={() => setCalHovered(null)}
              >
                <div className="flex items-center justify-between">
                  <span className="text-xs font-bold text-white">{e.symbol}</span>
                  <span className="text-[10px] text-amber-400 font-mono">{daysUntil(e.date)}</span>
                </div>
                {calHovered === e.symbol && (
                  <div className="text-[10px] text-slate-400 mt-1 leading-snug">{e.whisper}</div>
                )}
              </div>
            ))}
          </div>
        </div>
      </div>

      {/* Mobile: sentiment first, then collapsibles */}
      <div className="sm:hidden space-y-4">
        <SentimentColumn sentiment={sentiment} regime={regime} headlines={headlines}
          trumpSignal={trumpSignal} rb={rb} regimeLabel={regimeLabel} sentColor={sentColor} />
        <button className="w-full flex items-center justify-between bg-slate-800 rounded-xl px-4 py-3 border border-slate-700/50"
          onClick={() => setMobileSection(mobileSection === "movers" ? null : "movers")}>
          <span className="text-xs font-semibold text-slate-300">📊 Top Movers</span>
          <span className="text-slate-600">{mobileSection === "movers" ? "▲" : "▼"}</span>
        </button>
        {mobileSection === "movers" && (
          <div className="bg-slate-800 rounded-xl border border-slate-700/50 p-3 space-y-1.5">
            {movers.map(m => (
              <div key={m.symbol} className="flex items-center justify-between py-1.5 border-b border-slate-700/30 last:border-0">
                <span className="text-sm font-bold text-white">{m.symbol.replace("/USD","")}</span>
                <span className={`text-xs font-bold ${m.direction === "up" ? "text-emerald-400" : "text-red-400"}`}>
                  {m.direction === "up" ? "▲" : "▼"} {Math.abs(m.change_pct).toFixed(2)}%
                </span>
              </div>
            ))}
          </div>
        )}
        <button className="w-full flex items-center justify-between bg-slate-800 rounded-xl px-4 py-3 border border-slate-700/50"
          onClick={() => setMobileSection(mobileSection === "calendar" ? null : "calendar")}>
          <span className="text-xs font-semibold text-slate-300">📅 Calendar</span>
          <span className="text-slate-600">{mobileSection === "calendar" ? "▲" : "▼"}</span>
        </button>
        {mobileSection === "calendar" && (
          <div className="bg-slate-800 rounded-xl border border-slate-700/50 p-3 space-y-2">
            {upcoming.map(e => (
              <div key={e.symbol} className="bg-slate-900/40 rounded-lg p-2.5 border border-slate-700/30"
                onClick={() => setCalHovered(calHovered === e.symbol ? null : e.symbol)}>
                <div className="flex items-center justify-between">
                  <span className="text-xs font-bold text-white">{e.symbol}</span>
                  <span className="text-[10px] text-amber-400">{daysUntil(e.date)}</span>
                </div>
                {calHovered === e.symbol && <div className="text-[10px] text-slate-400 mt-1">{e.whisper}</div>}
              </div>
            ))}
          </div>
        )}
      </div>
    </div>
  );
}

function SentimentColumn({ sentiment, regime, headlines, trumpSignal, rb, regimeLabel, sentColor }: {
  sentiment: SentimentResponse | null; regime: RegimeResponse | null;
  headlines: string[]; trumpSignal: string | null;
  rb: ReturnType<typeof regimeBadgeStyle>; regimeLabel: string; sentColor: string;
}) {
  const sentimentLabel = sentiment?.sentiment?.replace("_", " ").toUpperCase() ?? "—";
  const params = regime?.params as Record<string, unknown> | undefined;

  return (
    <div className="bg-slate-800 rounded-xl border border-slate-700/50 overflow-hidden">
      <div className="px-4 py-3 border-b border-slate-700">
        <span className="text-xs font-bold text-slate-300 uppercase tracking-wider">Market Sentiment</span>
      </div>
      <div className="p-4 space-y-3">
        {/* Regime */}
        <div className={`flex items-center gap-2 px-3 py-2 rounded-lg border ${rb.bg}`}>
          <span className="text-base">{rb.emoji}</span>
          <div>
            <div className={`text-sm font-bold ${rb.text}`}>
              {regimeLabel === "UNKNOWN" ? "Loading…" : regimeLabel.replace("_MARKET","").replace("TRENDING_","")}
            </div>
            <div className="text-[10px] text-slate-500">Market Regime</div>
          </div>
          {params && (
            <div className="ml-auto text-right">
              {params["vix"] !== undefined && params["vix"] !== null && (
                <div className="text-[10px] text-slate-500">VIX <span className="text-slate-300">{String(params["vix"])}</span></div>
              )}
              {params["position_size_multiplier"] !== undefined && (
                <div className="text-[10px] text-slate-500">Size <span className="text-slate-300">{((params["position_size_multiplier"] as number) * 100).toFixed(0)}%</span></div>
              )}
            </div>
          )}
        </div>

        {/* VIX Fear Gauge */}
        {params?.["vix"] !== undefined && params?.["vix"] !== null && (() => {
          const vix = params["vix"] as number;
          const vixColor = vix < 15 ? "text-emerald-400" : vix < 25 ? "text-yellow-400" : vix < 35 ? "text-orange-400" : "text-red-400";
          const barColor = vix < 15 ? "bg-emerald-500" : vix < 25 ? "bg-yellow-500" : vix < 35 ? "bg-orange-500" : "bg-red-500";
          const zone = vix < 15 ? "Calm" : vix < 25 ? "Normal" : vix < 35 ? "Fear" : "Panic";
          return (
            <div>
              <div className="flex items-center justify-between mb-1.5">
                <div className="text-[10px] text-slate-500 uppercase tracking-wider">VIX Fear Gauge</div>
                <div className="flex items-center gap-1.5">
                  <span className={`text-[10px] font-semibold ${vixColor}`}>{zone}</span>
                  <span className={`text-xs font-bold font-mono ${vixColor}`}>{vix.toFixed(1)}</span>
                </div>
              </div>
              <div className="h-2.5 bg-slate-700 rounded-full overflow-hidden">
                <div className={`h-full rounded-full transition-all ${barColor}`}
                  style={{ width: `${Math.min(vix / 50 * 100, 100)}%` }} />
              </div>
              <div className="flex justify-between text-[8px] text-slate-600 mt-0.5">
                <span>15</span><span>25</span><span>35</span><span>50+</span>
              </div>
            </div>
          );
        })()}

        {/* Sentiment */}
        <div className="flex items-center justify-between">
          <div>
            <div className="text-[10px] text-slate-500 uppercase tracking-wider">News Sentiment</div>
            <div className={`text-base font-bold ${sentColor}`}>{sentimentLabel}</div>
          </div>
          <div className="text-right">
            <div className="text-[10px] text-slate-500">Score</div>
            <div className={`text-lg font-bold font-mono ${(sentiment?.score ?? 0) >= 0 ? "text-emerald-400" : "text-red-400"}`}>
              {(sentiment?.score ?? 0) >= 0 ? "+" : ""}{sentiment?.score ?? "—"}
            </div>
          </div>
        </div>

        {/* Trump signal */}
        {trumpSignal && (
          <div className="bg-amber-900/20 border border-amber-700/40 rounded-lg px-3 py-2 flex items-center gap-2">
            <span className="text-base">🇺🇸</span>
            <span className="text-[11px] text-amber-300">{trumpSignal}</span>
          </div>
        )}

        {/* Headlines */}
        {headlines.length > 0 && (
          <div className="space-y-1.5">
            <div className="text-[10px] text-slate-500 uppercase tracking-wider">Top Headlines</div>
            {headlines.slice(0, 3).map((hl, i) => {
              const tier = inferHeadlineTier(hl);
              return (
                <div key={i} className="flex items-start gap-1.5 py-1.5 border-b border-slate-700/30 last:border-0">
                  <TierBadge tier={tier} />
                  <span className="text-[11px] text-slate-400 leading-snug line-clamp-2">{hl}</span>
                </div>
              );
            })}
          </div>
        )}

        {sentiment?.ts && (
          <div className="text-[9px] text-slate-700 text-right">Updated {fmtTime(sentiment.ts)}</div>
        )}
      </div>
    </div>
  );
}

// ── SIGNALS PAGE ──────────────────────────────────────────────────────────────
function SignalRow({ dec }: { dec: Decision }) {
  const [expanded, setExpanded] = useState(false);
  const breakdown = parseSynthesisBreakdown(dec.reasoning);
  const patterns  = parsePatternsFromMarketData(dec.market_data);
  const regimeStr = parseRegimeFromReasoning(dec.reasoning);
  const conf      = Math.round(dec.confidence * 100);
  const finalScore = breakdown?.final ?? null;

  return (
    <>
      <tr className="hover:bg-slate-800/50 cursor-pointer transition-colors" onClick={() => setExpanded(v => !v)}>
        <td className="px-3 py-2.5 text-xs text-slate-500 whitespace-nowrap">{fmtDateTime(dec.decided_at)}</td>
        <td className="px-3 py-2.5 font-bold text-white text-sm">{dec.symbol.replace("/USD","")}</td>
        <td className="px-3 py-2.5"><DecisionBadge decision={dec.decision} /></td>
        <td className="px-3 py-2.5">
          <div className="flex items-center gap-2">
            <div className="w-16 h-1.5 bg-slate-700 rounded-full overflow-hidden">
              <div className={`h-full rounded-full ${conf >= 80 ? "bg-emerald-500" : conf >= 60 ? "bg-sky-500" : "bg-slate-500"}`} style={{ width: `${conf}%` }} />
            </div>
            <span className="text-xs text-slate-400">{conf}%</span>
          </div>
        </td>
        <td className="px-3 py-2.5 text-xs font-bold font-mono text-sky-400">{finalScore ?? "—"}</td>
        <td className="px-3 py-2.5">
          <span className={`text-[10px] font-semibold ${regimeStr.includes("BEAR") ? "text-red-400" : regimeStr.includes("BULL") ? "text-emerald-400" : "text-slate-500"}`}>
            {regimeStr}
          </span>
        </td>
        <td className="px-3 py-2.5 text-slate-600 text-xs">{expanded ? "▲" : "▼"}</td>
      </tr>
      {expanded && (
        <tr>
          <td colSpan={7} className="px-4 pb-4 pt-1 bg-slate-800/30">
            <div className="grid grid-cols-1 sm:grid-cols-2 gap-4 mt-2">
              {/* Score breakdown */}
              {breakdown && (
                <div>
                  <div className="text-[10px] text-slate-500 uppercase tracking-wider font-semibold mb-2">Synthesis Breakdown</div>
                  <div className="grid grid-cols-4 gap-1">
                    {[
                      ["Base", breakdown.base, false],
                      ["Regime", breakdown.regime, true],
                      ["RelStr", breakdown.relStr, true],
                      ["DXY", breakdown.dxy, true],
                      ["Corr", breakdown.corr, true],
                      ["Geo", breakdown.geo, true],
                      ["News", breakdown.news, true],
                      ["FINAL", breakdown.final, false],
                    ].map(([label, val, signed]) => (
                      <div key={label as string} className={`text-center rounded p-1.5 ${label === "FINAL" ? "bg-sky-900/40 col-span-2" : "bg-slate-800"}`}>
                        <div className="text-[8px] text-slate-600 uppercase">{label as string}</div>
                        <div className={`text-xs font-bold ${typeof val === "number" && val > 0 ? "text-emerald-400" : typeof val === "number" && val < 0 ? "text-red-400" : "text-slate-300"}`}>
                          {signed && typeof val === "number" && val > 0 ? "+" : ""}{val as number}
                        </div>
                      </div>
                    ))}
                  </div>
                  {patterns.length > 0 && (
                    <div className="flex items-center gap-1 flex-wrap mt-2">
                      <span className="text-[9px] text-slate-600">Patterns:</span>
                      {patterns.map(p => (
                        <span key={p} className="text-[9px] bg-violet-900/40 text-violet-400 border border-violet-700/50 rounded px-1.5 py-0.5">{p}</span>
                      ))}
                    </div>
                  )}
                </div>
              )}
              {/* Claude reasoning */}
              <div>
                <div className="text-[10px] text-slate-500 uppercase tracking-wider font-semibold mb-2">Claude Reasoning</div>
                <div className="bg-slate-900/60 rounded p-2.5 max-h-36 overflow-y-auto border-l-2 border-sky-700/40">
                  <p className="text-[10px] text-slate-400 leading-relaxed whitespace-pre-wrap">
                    {dec.reasoning.replace(/Breakdown:.*$/m, "").trim()}
                  </p>
                </div>
              </div>
            </div>
          </td>
        </tr>
      )}
    </>
  );
}

function ExpertPills({ value, onChange }: { value: ExpertFilter; onChange: (v: ExpertFilter) => void }) {
  return (
    <div className="flex items-center gap-1 bg-slate-800/60 border border-slate-700/50 rounded-lg p-0.5">
      {(["all", "gap", "geo"] as ExpertFilter[]).map(e => (
        <button key={e} onClick={() => onChange(e)}
          className={`px-3 py-1 text-xs font-semibold rounded transition-colors ${value === e
            ? e === "gap" ? "bg-amber-900/60 text-amber-400 shadow-sm"
            : e === "geo" ? "bg-violet-900/60 text-violet-400 shadow-sm"
            : "bg-slate-700 text-slate-200"
            : "text-slate-500 hover:text-slate-300"}`}>
          {e === "all" ? "Tous" : e === "gap" ? "🚀 Gap" : "📐 Geo"}
        </button>
      ))}
    </div>
  );
}

function SignalsPage({ decisions }: { decisions: Decision[] }) {
  const [actionFilter, setActionFilter] = useState<"ALL" | "BUY" | "SELL" | "HOLD">("ALL");
  const [expertFilter, setExpertFilter] = useState<ExpertFilter>("all");
  const [showAll,      setShowAll]      = useState(false);

  const byExpert = expertFilter === "all" ? decisions
    : expertFilter === "gap" ? decisions.filter(d => d.strategy_source === "gapper")
    : decisions.filter(d => d.strategy_source === "geometric");

  const latestBySymbol: Record<string, Decision> = {};
  byExpert.forEach(d => { if (!latestBySymbol[d.symbol]) latestBySymbol[d.symbol] = d; });
  const deduped  = Object.values(latestBySymbol).sort((a, b) => b.decided_at.localeCompare(a.decided_at));
  const baseList = showAll ? [...byExpert].sort((a, b) => b.decided_at.localeCompare(a.decided_at)) : deduped;
  const filtered = actionFilter === "ALL" ? baseList : baseList.filter(d => d.decision.toUpperCase() === actionFilter);

  return (
    <div className="p-4 sm:p-6 space-y-3">
      {/* Expert selector */}
      <div className="flex items-center gap-3 flex-wrap">
        <ExpertPills value={expertFilter} onChange={setExpertFilter} />
        <span className="text-[10px] text-slate-600">
          {expertFilter === "gap" ? "strategy_source: gapper" : expertFilter === "geo" ? "strategy_source: geometric" : "Tous les experts"}
        </span>
      </div>

      {/* Action filter bar */}
      <div className="flex items-center gap-2 flex-wrap">
        {(["ALL", "BUY", "SELL", "HOLD"] as const).map(f => (
          <button key={f} onClick={() => setActionFilter(f)}
            className={`px-3 py-1.5 text-xs font-semibold rounded transition-colors ${actionFilter === f
              ? f === "BUY"  ? "bg-emerald-900/50 text-emerald-400 border border-emerald-700"
              : f === "SELL" ? "bg-red-900/50 text-red-400 border border-red-700"
              : f === "HOLD" ? "bg-slate-700 text-slate-300 border border-slate-600"
              : "bg-sky-900/40 text-sky-400 border border-sky-700"
              : "text-slate-500 hover:text-slate-300 border border-transparent"}`}>
            {f}
          </button>
        ))}
        <button onClick={() => setShowAll(v => !v)}
          className={`px-2.5 py-1.5 text-[10px] rounded border font-semibold transition-colors ml-1 ${showAll ? "bg-violet-900/30 text-violet-400 border-violet-700" : "text-slate-500 border-slate-700 hover:text-slate-400"}`}>
          {showAll ? "History" : "Latest"}
        </button>
        <span className="ml-auto text-xs text-slate-600">
          {filtered.length} signal{filtered.length !== 1 ? "s" : ""}{showAll ? " (full history)" : " (latest par asset)"}
        </span>
      </div>

      {filtered.length === 0 ? (
        <div className="bg-slate-800/50 rounded-xl p-10 text-center text-slate-600 text-sm">Aucun signal pour ce filtre</div>
      ) : (
        <div className="bg-slate-800 rounded-xl overflow-hidden border border-slate-700/50">
          <div className="overflow-x-auto">
            <table className="w-full">
              <thead>
                <tr className="border-b border-slate-700">
                  {["Time","Asset","Signal","Confidence","Score","Regime",""].map(h => (
                    <th key={h} className="px-3 py-2 text-left text-[10px] uppercase tracking-wider text-slate-500 font-semibold">{h}</th>
                  ))}
                </tr>
              </thead>
              <tbody className="divide-y divide-slate-700/50">
                {filtered.map(d => <SignalRow key={`${d.symbol}-${d.decided_at}`} dec={d} />)}
              </tbody>
            </table>
          </div>
        </div>
      )}
    </div>
  );
}

// ── HOME PAGE (Performance Overview) ─────────────────────────────────────────
function DailyBarChart({ trades }: { trades: Trade[] }) {
  const closed = trades.filter(t => t.pnl != null);
  if (closed.length === 0) return <div className="text-xs text-slate-600 text-center py-6">No closed trades yet</div>;
  const byDay: Record<string, number> = {};
  closed.forEach(t => {
    const d = new Date(t.timestamp.includes("T") ? t.timestamp : t.timestamp.replace(" ", "T") + "Z");
    const k = d.toISOString().slice(0, 10);
    byDay[k] = (byDay[k] ?? 0) + (t.pnl ?? 0);
  });
  const days = Object.keys(byDay).sort();
  const vals = days.map(k => byDay[k]);
  const maxAbs = Math.max(...vals.map(Math.abs), 0.01);
  const BW = 18, GAP = 6, CH = 56, LH = 14, MY = CH / 2;
  const TW = Math.max(days.length * (BW + GAP) - GAP, 260);
  const SVG_H = CH + LH + 12;
  const dayLabel = (k: string) => { const d = new Date(k + "T00:00:00Z"); return `${d.getDate()}/${d.getMonth()+1}`; };
  return (
    <div className="overflow-x-auto">
      <svg viewBox={`-4 -4 ${TW + 8} ${SVG_H}`} width="100%" height={SVG_H} style={{ minWidth: `${TW}px` }} preserveAspectRatio="none">
        <line x1={-4} y1={MY} x2={TW+4} y2={MY} stroke="#334155" strokeWidth="1" />
        {days.map((day, i) => {
          const val = vals[i];
          const barH = Math.max(Math.abs(val) / maxAbs * (MY - 3), val !== 0 ? 2 : 1);
          const x = i * (BW + GAP);
          const y = val >= 0 ? MY - barH : MY;
          const fill = val > 0 ? "#10b981" : val < 0 ? "#ef4444" : "#475569";
          return (
            <g key={day}>
              <rect x={x} y={y} width={BW} height={barH} fill={fill} rx="1" />
              <text x={x + BW/2} y={CH + LH} textAnchor="middle" fill="#475569" fontSize="7">{dayLabel(day)}</text>
              {val !== 0 && <text x={x + BW/2} y={val >= 0 ? y - 2 : y + barH + 7} textAnchor="middle" fill={fill} fontSize="7" fontWeight="bold">{val >= 0 ? "+" : ""}{val.toFixed(1)}</text>}
            </g>
          );
        })}
      </svg>
    </div>
  );
}

function MonthlyBarChart({ trades }: { trades: Trade[] }) {
  const closed = trades.filter(t => t.pnl != null);
  if (closed.length === 0) return <div className="text-xs text-slate-600 text-center py-6">No closed trades yet</div>;

  const now = new Date();
  const startYear = now.getFullYear();
  const allMonths: string[] = [];
  for (let m = 0; m < 12; m++) {
    allMonths.push(`${startYear}-${String(m+1).padStart(2,"0")}`);
  }
  const byMonth: Record<string, number> = {};
  allMonths.forEach(k => { byMonth[k] = 0; });
  closed.forEach(t => {
    const d = new Date(t.timestamp.includes("T") ? t.timestamp : t.timestamp.replace(" ", "T") + "Z");
    const k = `${d.getFullYear()}-${String(d.getMonth()+1).padStart(2,"0")}`;
    if (k in byMonth) byMonth[k] = (byMonth[k] ?? 0) + (t.pnl ?? 0);
  });
  const ck     = `${now.getFullYear()}-${String(now.getMonth()+1).padStart(2,"0")}`;
  const months = allMonths;
  const vals   = months.map(k => byMonth[k]);

  const activeVals = vals.filter(v => v !== 0);
  const maxAbs = Math.max(...activeVals.map(Math.abs), 0.01);

  const ytd: number[] = [];
  let cum = 0;
  vals.forEach(v => { cum += v; ytd.push(cum); });

  const BW = 22, GAP = 8, CH = 52, LH = 14, MY = CH / 2;
  const TW = months.length * (BW + GAP) - GAP;
  const SVG_H = CH + LH + 24;
  const ML = (k: string) => ["Jan","Feb","Mar","Apr","May","Jun","Jul","Aug","Sep","Oct","Nov","Dec"][parseInt(k.split("-")[1],10)-1];

  return (
    <div className="overflow-x-auto">
      <svg viewBox={`-4 -18 ${TW + 8} ${SVG_H}`} width="100%" height={SVG_H} style={{ minWidth: `${Math.max(TW, 260)}px` }} preserveAspectRatio="none">
        <line x1={-4} y1={MY} x2={TW+4} y2={MY} stroke="#334155" strokeWidth="1" />
        {months.map((m, i) => {
          const val   = vals[i];
          const ytdV  = ytd[i];
          const barH  = Math.max(Math.abs(val) / maxAbs * (MY - 4), val !== 0 ? 2 : 1);
          const x     = i * (BW + GAP);
          const y     = val >= 0 ? MY - barH : MY;
          const fill  = val > 0 ? "#10b981" : val < 0 ? "#ef4444" : "#334155";
          const cur   = m === ck;
          const ytdFill = ytdV > 0 ? "#22d3ee" : ytdV < 0 ? "#f87171" : "#475569";
          const isFuture = m > ck;
          return (
            <g key={m} opacity={isFuture ? 0.25 : 1}>
              <rect x={x} y={y} width={BW} height={barH} fill={fill} rx="1" opacity={cur ? 0.65 : 1} />
              {cur && <rect x={x} y={y} width={BW} height={barH} fill="none" stroke={fill} strokeWidth="1" rx="1" strokeDasharray="3 2" />}
              <text x={x + BW/2} y={CH + LH} textAnchor="middle" fill={cur ? "#94a3b8" : "#475569"} fontSize="8">{ML(m)}</text>
              {val !== 0 && (
                <text x={x + BW/2} y={val >= 0 ? y - 2 : y + barH + 7} textAnchor="middle" fill={fill} fontSize="6.5" fontWeight="bold">
                  {val >= 0 ? "+" : ""}{val.toFixed(1)}
                </text>
              )}
              {ytdV !== 0 && !isFuture && (
                <text x={x + BW/2} y={-8} textAnchor="middle" fill={ytdFill} fontSize="6.5" fontWeight="bold">
                  {ytdV >= 0 ? "+" : ""}{ytdV.toFixed(1)}
                </text>
              )}
            </g>
          );
        })}
      </svg>
      <div className="flex items-center gap-3 mt-1">
        <div className="flex items-center gap-1"><span className="w-2 h-2 rounded-full bg-cyan-400 inline-block" /><span className="text-[9px] text-slate-600">YTD running total</span></div>
        <div className="text-[9px] text-slate-600 ml-auto">* current month</div>
      </div>
    </div>
  );
}

function ExpertCard({ name, icon, data, tagline, accent }: {
  name: string; icon: string; data: ExpertStats | undefined;
  tagline: string; accent: "amber" | "violet";
}) {
  const C = accent === "amber"
    ? { border: "border-amber-700/40", bg: "bg-amber-950/20", hdr: "bg-amber-900/20 border-amber-800/30",
        text: "text-amber-400", bar: "bg-amber-500", pill: "bg-amber-500/20 text-amber-400", dot: "bg-amber-400" }
    : { border: "border-violet-700/40", bg: "bg-violet-950/20", hdr: "bg-violet-900/20 border-violet-800/30",
        text: "text-violet-400", bar: "bg-violet-500", pill: "bg-violet-500/20 text-violet-400", dot: "bg-violet-400" };

  if (!data) return (
    <div className={`rounded-xl border ${C.border} bg-slate-800/40 flex flex-col items-center justify-center gap-2 py-10`}>
      <span className="text-3xl">{icon}</span>
      <span className={`text-sm font-bold ${C.text}`}>{name}</span>
      <span className="text-[10px] text-slate-600">{tagline}</span>
      <span className="text-[10px] text-slate-700 mt-1">En attente de données…</span>
    </div>
  );

  const capPos = data.capital_now >= data.capital_start;
  const retPos = data.capital_return >= 0;
  const pnlPos = data.total_pnl >= 0;
  const unrPos = data.live_unrealized >= 0;
  const rr     = data.avg_win !== 0 && data.avg_loss !== 0
    ? (data.avg_win / Math.abs(data.avg_loss)).toFixed(2) + "x" : "—";
  const barW   = Math.min(100, Math.max(2, (data.capital_now / Math.max(data.capital_start, data.capital_now, 1)) * 100));

  return (
    <div className={`rounded-xl border ${C.border} ${C.bg} overflow-hidden flex flex-col`}>
      {/* ── Header */}
      <div className={`px-4 py-3 border-b ${C.hdr} flex items-center justify-between`}>
        <div className="flex items-center gap-2.5">
          <span className="text-xl">{icon}</span>
          <div>
            <div className={`text-sm font-bold ${C.text}`}>{name}</div>
            <div className="text-[9px] text-slate-600 leading-none mt-0.5">{tagline}</div>
          </div>
        </div>
        <div className="text-right">
          <div className={`text-lg font-bold font-mono ${retPos ? "text-emerald-400" : "text-red-400"}`}>
            {retPos ? "+" : ""}{data.capital_return.toFixed(2)}%
          </div>
          <div className="text-[9px] text-slate-600">retour capital</div>
        </div>
      </div>

      {/* ── Capital */}
      <div className="px-4 pt-3 pb-3 border-b border-slate-700/20">
        <div className="flex items-end justify-between mb-2">
          <div>
            <div className="text-[9px] uppercase text-slate-600 tracking-wider mb-0.5">Capital actuel</div>
            <div className={`text-2xl font-bold font-mono ${capPos ? "text-emerald-400" : "text-red-400"}`}>
              ${data.capital_now.toFixed(2)}
            </div>
          </div>
          <div className="text-right space-y-0.5">
            <div>
              <span className="text-[9px] text-slate-600">Départ </span>
              <span className="text-xs font-mono text-slate-400">${data.capital_start.toFixed(0)}</span>
            </div>
            <div>
              <span className="text-[9px] text-slate-600">Réalisé </span>
              <span className={`text-xs font-mono font-semibold ${pnlPos ? "text-emerald-400" : "text-red-400"}`}>
                {pnlPos ? "+" : ""}${data.total_pnl.toFixed(2)}
              </span>
            </div>
          </div>
        </div>
        <div className="h-1.5 bg-slate-700/50 rounded-full overflow-hidden">
          <div className={`h-full rounded-full transition-all ${capPos ? C.bar : "bg-red-500"}`} style={{ width: `${barW}%` }} />
        </div>
      </div>

      {/* ── 3-col stats */}
      <div className="grid grid-cols-3 divide-x divide-slate-700/20 flex-1">
        <div className="px-3 py-3 text-center">
          <div className="text-[9px] uppercase text-slate-600 mb-1">Win rate</div>
          <div className={`text-base font-bold ${(data.win_rate ?? 0) >= 50 ? "text-emerald-400" : "text-amber-400"}`}>
            {data.win_rate != null ? data.win_rate.toFixed(0) + "%" : "—"}
          </div>
          <div className="text-[9px] text-slate-600 mt-0.5">{data.total_trades} trades</div>
        </div>
        <div className="px-3 py-3 text-center">
          <div className="text-[9px] uppercase text-slate-600 mb-1">Unrealized</div>
          <div className={`text-base font-bold font-mono ${data.live_unrealized > 0 ? "text-emerald-400" : data.live_unrealized < 0 ? "text-red-400" : "text-slate-500"}`}>
            {data.live_unrealized !== 0 ? (unrPos ? "+" : "") + data.live_unrealized.toFixed(2) : "—"}
          </div>
          <div className="text-[9px] text-slate-600 mt-0.5">{data.open_trades > 0 ? `${data.open_trades} pos` : "flat"}</div>
        </div>
        <div className="px-3 py-3 text-center">
          <div className="text-[9px] uppercase text-slate-600 mb-1">R:R</div>
          <div className="text-base font-bold font-mono text-slate-300">{rr}</div>
          <div className="text-[9px] text-slate-600 mt-0.5">avg win/loss</div>
        </div>
      </div>

      {/* ── Footer avg */}
      {(data.avg_win !== 0 || data.avg_loss !== 0) && (
        <div className="px-4 py-2 border-t border-slate-700/20 flex items-center justify-between text-[10px] bg-slate-900/20">
          <span className="text-slate-600">Avg win <span className="text-emerald-400 font-mono font-semibold">+${data.avg_win.toFixed(3)}</span></span>
          <span className="text-slate-700">·</span>
          <span className="text-slate-600">Avg loss <span className="text-red-400 font-mono font-semibold">${data.avg_loss.toFixed(3)}</span></span>
        </div>
      )}
    </div>
  );
}

function HomePage({ trades, decisions, stats, positions, portfolioValue, account, analysis, experts = {} }: {
  trades: Trade[]; decisions: Decision[];
  stats: StatsResponse | null; positions: Position[]; portfolioValue: number;
  account: AccountResponse | null; analysis: AnalysisData | null;
  experts?: ExpertsResponse;
}) {
  const totalReturn    = ((portfolioValue - INITIAL_CAPITAL) / INITIAL_CAPITAL) * 100;
  const grossPnl       = portfolioValue - INITIAL_CAPITAL;
  const now            = new Date();
  const mDecisions     = decisions.filter(d => {
    const dt = new Date(d.decided_at.includes("T") ? d.decided_at : d.decided_at.replace(" ", "T") + "Z");
    return dt.getMonth() === now.getMonth() && dt.getFullYear() === now.getFullYear();
  });
  const claudeCost     = mDecisions.length * CLAUDE_COST_PER_CALL;
  const totalCost      = REPLIT_MONTHLY_COST + claudeCost;
  const netPnl         = grossPnl - totalCost;
  const unrealizedTotal = positions.reduce((s, p) => s + p.unrealized_pl, 0);
  const allocatedPct    = positions.reduce((s, p) => s + p.cost_basis, 0) / Math.max(portfolioValue, INITIAL_CAPITAL) * 100;

  const kpis = [
    {
      label: "Total Return",
      value: (totalReturn >= 0 ? "+" : "") + totalReturn.toFixed(2) + "%",
      sub: (grossPnl >= 0 ? "+" : "−") + "$" + Math.abs(grossPnl).toFixed(2),
      color: totalReturn >= 0 ? "text-emerald-400" : "text-red-400",
    },
    {
      label: "Win Rate",
      value: stats?.win_rate != null ? stats.win_rate.toFixed(1) + "%" : "—",
      sub: analysis ? `${analysis.winning_trades}W / ${analysis.losing_trades}L` : undefined,
      color: "text-sky-400",
    },
    {
      label: "Profit Factor",
      value: stats?.profit_factor != null ? (stats.profit_factor >= 999 ? "∞" : stats.profit_factor.toFixed(2)) : "—",
      sub: analysis ? `$${analysis.gross_win.toFixed(2)} gross` : undefined,
      color: "text-violet-400",
    },
    {
      label: "Best Asset",
      value: stats?.best_asset ?? "—",
      sub: stats?.best_asset && stats.asset_pnl[stats.best_asset] !== undefined
        ? `+$${(stats.asset_pnl[stats.best_asset] ?? 0).toFixed(2)}`
        : undefined,
      color: "text-amber-400",
    },
  ];

  const gapCap  = experts.gapper?.capital_now   ?? 0;
  const geoCap  = experts.geometric?.capital_now ?? 0;
  const totalCap = gapCap + geoCap;

  return (
    <div className="p-4 sm:p-6 space-y-5">

      {/* ── HERO: Two Expert Accounts ───────────────────────────── */}
      <div>
        <div className="flex items-center justify-between mb-3">
          <div>
            <h2 className="text-sm font-bold text-slate-200">🧠 Mastermind V2</h2>
            <p className="text-[10px] text-slate-600 mt-0.5">Deux comptes indépendants · capital qui compound séparément</p>
          </div>
          {totalCap > 0 && (
            <div className="text-right">
              <div className="text-[9px] text-slate-600 uppercase tracking-wider">Total</div>
              <div className={`text-base font-bold font-mono ${totalCap >= INITIAL_CAPITAL ? "text-emerald-400" : "text-red-400"}`}>
                ${totalCap.toFixed(2)}
              </div>
            </div>
          )}
        </div>
        <div className="grid grid-cols-1 sm:grid-cols-2 gap-4">
          <ExpertCard
            name="Gap" icon="🚀" data={experts.gapper}
            tagline="Stocks gappers · 9h35–10h45 ET · max 3 trades/j"
            accent="amber"
          />
          <ExpertCard
            name="Geo" icon="📐" data={experts.geometric}
            tagline="Crypto · RSI divergence · patterns 24/7"
            accent="violet"
          />
        </div>
      </div>

      {/* ── Portfolio strip ─────────────────────────────────────── */}
      <div className="grid grid-cols-2 sm:grid-cols-4 gap-3">
        <div className="bg-slate-800/60 rounded-xl p-3 border border-slate-700/30">
          <div className="text-[9px] uppercase text-slate-600 tracking-wider mb-1">Portefeuille ⚡</div>
          <div className={`text-base font-bold font-mono ${portfolioValue >= INITIAL_CAPITAL ? "text-emerald-400" : "text-red-400"}`}>
            ${portfolioValue.toFixed(0)}
          </div>
          <div className={`text-[9px] mt-0.5 ${totalReturn >= 0 ? "text-emerald-600" : "text-red-600"}`}>
            {totalReturn >= 0 ? "+" : ""}{totalReturn.toFixed(2)}%
          </div>
        </div>
        <div className="bg-slate-800/60 rounded-xl p-3 border border-slate-700/30">
          <div className="text-[9px] uppercase text-slate-600 tracking-wider mb-1">Unrealized ⚡</div>
          <div className={`text-base font-bold font-mono ${unrealizedTotal >= 0 ? "text-emerald-400" : "text-red-400"}`}>
            {fmtPnl(unrealizedTotal)}
          </div>
          <div className="text-[9px] text-slate-600 mt-0.5">{positions.length} pos ouvertes</div>
        </div>
        <div className="bg-slate-800/60 rounded-xl p-3 border border-slate-700/30">
          <div className="text-[9px] uppercase text-slate-600 tracking-wider mb-1">Cash dispo</div>
          <div className="text-base font-bold font-mono text-slate-300">
            ${account ? account.cash.toFixed(0) : "—"}
          </div>
          <div className="text-[9px] text-slate-600 mt-0.5">{allocatedPct.toFixed(1)}% déployé</div>
        </div>
        <div className="bg-slate-800/60 rounded-xl p-3 border border-slate-700/30">
          <div className="text-[9px] uppercase text-slate-600 tracking-wider mb-1">Win rate global</div>
          <div className={`text-base font-bold ${stats?.win_rate != null && stats.win_rate >= 50 ? "text-sky-400" : "text-amber-400"}`}>
            {stats?.win_rate != null ? stats.win_rate.toFixed(1) + "%" : "—"}
          </div>
          <div className="text-[9px] text-slate-600 mt-0.5">
            {analysis ? `${analysis.winning_trades}W / ${analysis.losing_trades}L` : "tous trades"}
          </div>
        </div>
      </div>

      {/* ── Live positions (if any) ──────────────────────────────── */}
      {account?.positions_live && account.positions_live.length > 0 && (
        <div className="bg-slate-800/40 rounded-xl border border-slate-700/30 overflow-hidden">
          <div className="px-4 py-2 border-b border-slate-700/30 flex items-center gap-2">
            <span className="text-[10px] font-bold uppercase tracking-wider text-slate-400">Positions ouvertes — Live</span>
            <span className="text-[9px] text-amber-500/80 bg-amber-500/10 px-1.5 py-0.5 rounded">⚡ notre feed vs Alpaca</span>
          </div>
          <div className="divide-y divide-slate-700/20">
            {account.positions_live.map(pos => {
              const diff    = pos.live_price - pos.alpaca_mark;
              const diffPct = (diff / pos.alpaca_mark) * 100;
              const pnlPos  = pos.unrealized >= 0;
              return (
                <div key={pos.symbol} className="px-4 py-2.5 flex items-center gap-3 text-xs">
                  <span className="font-bold text-slate-200 w-20">{pos.symbol}</span>
                  <div className="flex-1 grid grid-cols-3 gap-3 text-center">
                    <div>
                      <div className="text-[9px] text-slate-600">Live</div>
                      <div className="font-mono font-semibold text-sky-400">${pos.live_price.toFixed(4)}</div>
                    </div>
                    <div>
                      <div className="text-[9px] text-slate-600">Alpaca</div>
                      <div className={`font-mono text-slate-400 ${Math.abs(diffPct) > 0.5 ? "line-through opacity-50" : ""}`}>
                        ${pos.alpaca_mark.toFixed(4)}
                      </div>
                      {Math.abs(diffPct) > 0.1 && (
                        <div className={`text-[9px] ${diffPct >= 0 ? "text-emerald-500" : "text-red-500"}`}>
                          {diffPct >= 0 ? "+" : ""}{diffPct.toFixed(2)}% lag
                        </div>
                      )}
                    </div>
                    <div>
                      <div className="text-[9px] text-slate-600">Unrealized</div>
                      <div className={`font-mono font-semibold ${pnlPos ? "text-emerald-400" : "text-red-400"}`}>
                        {pnlPos ? "+" : ""}{pos.unrealized.toFixed(2)}
                      </div>
                    </div>
                  </div>
                </div>
              );
            })}
          </div>
        </div>
      )}

      {/* ── KPI row ─────────────────────────────────────────────── */}
      <div className="grid grid-cols-2 sm:grid-cols-4 gap-3">
        {kpis.map(k => (
          <div key={k.label} className="bg-slate-800 rounded-xl p-4 border border-slate-700/50">
            <div className="text-[10px] uppercase tracking-widest text-slate-500 mb-1">{k.label}</div>
            <div className={`text-2xl font-bold ${k.color}`}>{k.value}</div>
            {k.sub && <div className="text-[10px] text-slate-600 mt-0.5">{k.sub}</div>}
          </div>
        ))}
      </div>

      {/* ── Daily P&L ───────────────────────────────────────────── */}
      <div className="bg-slate-800 rounded-xl border border-slate-700/50 overflow-hidden">
        <div className="px-4 py-3 border-b border-slate-700 flex items-center justify-between">
          <span className="text-xs font-bold text-slate-300 uppercase tracking-wider">Daily P&L</span>
          <span className="text-xs text-slate-600">closed trades · per day</span>
        </div>
        <div className="p-4">
          <DailyBarChart trades={trades} />
        </div>
      </div>

      {/* ── Monthly P&L ─────────────────────────────────────────── */}
      <div className="bg-slate-800 rounded-xl border border-slate-700/50 overflow-hidden">
        <div className="px-4 py-3 border-b border-slate-700 flex items-center justify-between">
          <span className="text-xs font-bold text-slate-300 uppercase tracking-wider">Monthly P&L</span>
          <span className="text-xs text-slate-600">closed trades · by month</span>
        </div>
        <div className="p-4">
          <MonthlyBarChart trades={trades} />
        </div>
      </div>

      {/* ── Costs ───────────────────────────────────────────────── */}
      <div className="bg-slate-800 rounded-xl border border-slate-700/50 overflow-hidden">
        <div className="px-4 py-3 border-b border-slate-700">
          <span className="text-xs font-bold text-slate-300 uppercase tracking-wider">💸 Coûts — {now.toLocaleString("default", { month: "long", year: "numeric" })}</span>
        </div>
        <div className="p-4 space-y-2 max-w-sm">
          <div className="flex justify-between text-xs border-b border-slate-700 pb-2 mb-1">
            <span className="text-slate-400">Gross P&L (Alpaca)</span>
            <span className={`font-mono font-bold ${grossPnl >= 0 ? "text-emerald-400" : "text-red-400"}`}>
              {grossPnl >= 0 ? "+" : "-"}${Math.abs(grossPnl).toFixed(2)}
            </span>
          </div>
          {[
            ["Replit Core", `-$${REPLIT_MONTHLY_COST.toFixed(2)}`, "text-red-400"],
            [`Claude API (${mDecisions.length} calls × $0.003)`, `-$${claudeCost.toFixed(3)}`, "text-red-400"],
            ["Total Coûts", `-$${totalCost.toFixed(2)}`, "text-red-400 font-bold"],
          ].map(([label, value, cls]) => (
            <div key={label as string} className={`flex justify-between text-xs ${cls as string}`}>
              <span className="text-slate-400">{label as string}</span>
              <span className="font-mono">{value as string}</span>
            </div>
          ))}
          <div className="flex justify-between text-sm border-t border-slate-700 pt-2 mt-1">
            <span className="text-slate-300 font-semibold">Net P&L (après coûts)</span>
            <span className={`font-mono font-bold text-base ${netPnl >= 0 ? "text-emerald-400" : "text-red-400"}`}>
              {netPnl >= 0 ? "+" : "-"}${Math.abs(netPnl).toFixed(2)}
            </span>
          </div>
        </div>
      </div>
    </div>
  );
}

// ── App ───────────────────────────────────────────────────────────────────────
export default function App() {
  const [activePage, setActivePage] = useState<Page>("HOME");
  const [status,     setStatus]     = useState<Status | null>(null);
  const [decisions,  setDecisions]  = useState<Decision[]>([]);
  const [positions,  setPositions]  = useState<Position[]>([]);
  const [movers,     setMovers]     = useState<Mover[]>([]);
  const [sentiment,  setSentiment]  = useState<SentimentResponse | null>(null);
  const [regime,     setRegime]     = useState<RegimeResponse | null>(null);
  const [stats,      setStats]      = useState<StatsResponse | null>(null);
  const [partialProfits, setPartialProfits] = useState<PartialProfits>({});
  const [stops,      setStops]      = useState<Stops>({});
  const [account,      setAccount]      = useState<AccountResponse | null>(null);
  const [closedToday,  setClosedToday]  = useState<ClosedTodayItem[]>([]);
  const [closedPeriod, setClosedPeriod] = useState<ClosedPeriod>("today");
  const [analysis,     setAnalysis]     = useState<AnalysisData | null>(null);
  const [experts,      setExperts]      = useState<ExpertsResponse>({});
  const [lastRefresh,  setLastRefresh]  = useState<Date>(new Date());
  const [error,        setError]        = useState(false);

  const fetchAll = useCallback(async () => {
    try {
      const [sRes, dRes, pRes, mRes, senRes, regRes, stRes, ppRes, stopsRes, accRes, anlRes, expRes] = await Promise.all([
        fetch(`${BASE}/api/status`),
        fetch(`${BASE}/api/decisions`),
        fetch(`${BASE}/api/positions`),
        fetch(`${BASE}/api/movers`),
        fetch(`${BASE}/api/sentiment`),
        fetch(`${BASE}/api/regime`),
        fetch(`${BASE}/api/stats`),
        fetch(`${BASE}/api/partial-profits`),
        fetch(`${BASE}/api/stops`),
        fetch(`${BASE}/api/account`),
        fetch(`${BASE}/api/analysis`),
        fetch(`${BASE}/api/experts/stats`),
      ]);
      if (sRes.ok)     { const d = await sRes.json() as Status;   setStatus(d); }
      if (dRes.ok)     { const d = await dRes.json();             setDecisions(d.decisions ?? []); }
      if (pRes.ok)     { const d = await pRes.json();             setPositions(d.positions ?? []); }
      if (mRes.ok)     { const d = await mRes.json();             setMovers(d.movers ?? []); }
      if (senRes.ok)   setSentiment(await senRes.json() as SentimentResponse);
      if (regRes.ok)   setRegime(await regRes.json() as RegimeResponse);
      if (stRes.ok)    setStats(await stRes.json() as StatsResponse);
      if (ppRes.ok)    { const d = await ppRes.json(); setPartialProfits(d.partial_profits ?? {}); }
      if (stopsRes.ok) { const d = await stopsRes.json(); setStops(d.stops ?? {}); }
      if (accRes.ok)   { const d = await accRes.json() as AccountResponse; if (d.portfolio_value > 0) setAccount(d); }
      if (anlRes.ok)   { const d = await anlRes.json(); if (d.total_trades !== undefined) setAnalysis(d as AnalysisData); }
      if (expRes.ok)   { const d = await expRes.json() as ExpertsResponse; setExperts(d); }
      setError(false);
    } catch { setError(true); }
    setLastRefresh(new Date());
  }, []);

  // ── Period-aware closed fetch — re-runs when period changes ────────────────
  const fetchClosed = useCallback(async (period: ClosedPeriod) => {
    try {
      const res = await fetch(`${BASE}/api/closed-today?period=${period}`);
      if (res.ok) { const d = await res.json(); setClosedToday(d.closed ?? []); }
    } catch { /* silent */ }
  }, []);

  useEffect(() => {
    fetchAll();
    const id = setInterval(fetchAll, 15_000);
    return () => clearInterval(id);
  }, [fetchAll]);

  // Initial + period-change fetch for closed trades
  useEffect(() => { fetchClosed(closedPeriod); }, [closedPeriod, fetchClosed]);
  // Also re-fetch closed on every main refresh cycle
  useEffect(() => { fetchClosed(closedPeriod); }, [lastRefresh]); // eslint-disable-line

  // Derived state — prefer live_equity (our bar feed) over Alpaca's delayed marks
  const unrealizedTotal = account?.live_unrealized ?? positions.reduce((s, p) => s + p.unrealized_pl, 0);
  const closedPnl       = stats?.total_pnl ?? status?.recent_trades.filter(t => t.pnl != null).reduce((s,t) => s + (t.pnl ?? 0), 0) ?? 0;
  const portfolioValue  = (account?.live_equity && account.live_equity > 0)
    ? account.live_equity
    : account?.portfolio_value && account.portfolio_value > 0
      ? account.portfolio_value
      : INITIAL_CAPITAL + closedPnl + unrealizedTotal;
  const portfolioDelta  = ((portfolioValue - INITIAL_CAPITAL) / INITIAL_CAPITAL) * 100;
  const allTrades       = status?.recent_trades ?? [];

  return (
    <div className="min-h-screen bg-slate-900 text-slate-200">
      <TopNav
        activePage={activePage}
        setActivePage={setActivePage}
        regime={regime?.regime ?? "UNKNOWN"}
        portfolioValue={portfolioValue}
        portfolioDelta={portfolioDelta}
        positionsCount={positions.length}
        lastRefresh={lastRefresh}
        error={error}
      />
      {/* Page content — push below fixed nav */}
      <div className="pt-14">
        {activePage === "HOME"     && <HomePage trades={allTrades} decisions={decisions} stats={stats} positions={positions} portfolioValue={portfolioValue} account={account} analysis={analysis} experts={experts} />}
        {activePage === "MARKET"   && <MarketPage movers={movers} sentiment={sentiment} regime={regime} />}
        {activePage === "SIGNALS"  && <SignalsPage decisions={decisions} />}
        {activePage === "TRADES"   && <TradesPage positions={positions} decisions={decisions} partialProfits={partialProfits} stops={stops} totalPortfolio={portfolioValue} closedToday={closedToday} closedPeriod={closedPeriod} setClosedPeriod={setClosedPeriod} />}
        {activePage === "ANALYSIS" && <AnalysisPage />}
      </div>
    </div>
  );
}
