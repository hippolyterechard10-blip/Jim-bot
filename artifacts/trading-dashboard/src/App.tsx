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
  total_trades: number; win_rate: number; profit_factor: number;
  total_pnl: number; max_drawdown: number; best_asset: string | null; asset_pnl: Record<string, number>;
}
interface PartialProfits { [symbol: string]: { secured_pnl: number; count: number } }
interface Stops { [symbol: string]: number }

type Page = "HOME" | "MARKET" | "SIGNALS" | "PERFORMANCE";

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
function fmtPnl(n: number) { return (n >= 0 ? "+" : "") + "$" + Math.abs(n).toFixed(2); }
function fmtPct(n: number) { return (n >= 0 ? "+" : "") + n.toFixed(2) + "%"; }
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
  const tabs: Page[] = ["HOME", "MARKET", "SIGNALS", "PERFORMANCE"];
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

function HomePage({ positions, decisions, partialProfits, stops, totalPortfolio }: {
  positions: Position[]; decisions: Decision[];
  partialProfits: PartialProfits; stops: Stops; totalPortfolio: number;
}) {
  return (
    <div className="p-4 sm:p-6">
      <div className="flex items-center justify-between mb-4">
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
            </table>
          </div>
        </div>
      )}
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

function SignalsPage({ decisions }: { decisions: Decision[] }) {
  const [filter, setFilter] = useState<"ALL" | "BUY" | "SELL" | "HOLD">("ALL");

  const latestBySymbol: Record<string, Decision> = {};
  decisions.forEach(d => { if (!latestBySymbol[d.symbol]) latestBySymbol[d.symbol] = d; });
  const deduped = Object.values(latestBySymbol).sort((a, b) => b.decided_at.localeCompare(a.decided_at));
  const filtered = filter === "ALL" ? deduped : deduped.filter(d => d.decision.toUpperCase() === filter);

  return (
    <div className="p-4 sm:p-6">
      {/* Filter bar */}
      <div className="flex items-center gap-2 mb-4">
        {(["ALL", "BUY", "SELL", "HOLD"] as const).map(f => (
          <button key={f} onClick={() => setFilter(f)}
            className={`px-3 py-1.5 text-xs font-semibold rounded transition-colors ${filter === f
              ? f === "BUY"  ? "bg-emerald-900/50 text-emerald-400 border border-emerald-700"
              : f === "SELL" ? "bg-red-900/50 text-red-400 border border-red-700"
              : f === "HOLD" ? "bg-slate-700 text-slate-300 border border-slate-600"
              : "bg-sky-900/40 text-sky-400 border border-sky-700"
              : "text-slate-500 hover:text-slate-300 border border-transparent"}`}>
            {f}
          </button>
        ))}
        <span className="ml-auto text-xs text-slate-600">{filtered.length} signals (latest per asset)</span>
      </div>

      {filtered.length === 0 ? (
        <div className="bg-slate-800/50 rounded-xl p-10 text-center text-slate-600 text-sm">No signals yet</div>
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

// ── PERFORMANCE PAGE ──────────────────────────────────────────────────────────
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
  const BW = 32, GAP = 8, CH = 80, LH = 16, MY = CH / 2;
  const TW = Math.max(days.length * (BW + GAP) - GAP, 300);
  const dayLabel = (k: string) => { const d = new Date(k + "T00:00:00Z"); return `${d.getDate()}/${d.getMonth()+1}`; };
  return (
    <div className="overflow-x-auto">
      <svg viewBox={`-4 -8 ${TW + 8} ${CH + LH + 16}`} width="100%" style={{ minWidth: `${TW}px` }}>
        <line x1={-4} y1={MY} x2={TW+4} y2={MY} stroke="#334155" strokeWidth="1" />
        {days.map((day, i) => {
          const val = vals[i];
          const barH = Math.max(Math.abs(val) / maxAbs * (MY - 4), val !== 0 ? 2 : 1);
          const x = i * (BW + GAP);
          const y = val >= 0 ? MY - barH : MY;
          const fill = val > 0 ? "#10b981" : val < 0 ? "#ef4444" : "#475569";
          return (
            <g key={day}>
              <rect x={x} y={y} width={BW} height={barH} fill={fill} rx="2" />
              <text x={x + BW/2} y={CH + LH} textAnchor="middle" fill="#64748b" fontSize="8">{dayLabel(day)}</text>
              {val !== 0 && <text x={x + BW/2} y={val >= 0 ? y - 2 : y + barH + 8} textAnchor="middle" fill={fill} fontSize="8" fontWeight="bold">{val >= 0 ? "+" : ""}{val.toFixed(1)}</text>}
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
  const byMonth: Record<string, number> = {};
  closed.forEach(t => {
    const d = new Date(t.timestamp.includes("T") ? t.timestamp : t.timestamp.replace(" ", "T") + "Z");
    const k = `${d.getFullYear()}-${String(d.getMonth()+1).padStart(2,"0")}`;
    byMonth[k] = (byMonth[k] ?? 0) + (t.pnl ?? 0);
  });
  const now = new Date();
  const ck  = `${now.getFullYear()}-${String(now.getMonth()+1).padStart(2,"0")}`;
  if (!(ck in byMonth)) byMonth[ck] = 0;
  const months = Object.keys(byMonth).sort();
  const vals   = months.map(k => byMonth[k]);
  const maxAbs = Math.max(...vals.map(Math.abs), 0.01);
  const BW = 42, GAP = 12, CH = 80, LH = 16, MY = CH / 2;
  const TW = months.length * (BW + GAP) - GAP;
  const ML = (k: string) => ["Jan","Feb","Mar","Apr","May","Jun","Jul","Aug","Sep","Oct","Nov","Dec"][parseInt(k.split("-")[1],10)-1];
  return (
    <div className="overflow-x-auto">
      <svg viewBox={`-4 -8 ${TW + 8} ${CH + LH + 16}`} width="100%" style={{ minWidth: `${Math.max(TW, 200)}px` }}>
        <line x1={-4} y1={MY} x2={TW+4} y2={MY} stroke="#334155" strokeWidth="1" />
        {months.map((m, i) => {
          const val = vals[i];
          const barH = Math.max(Math.abs(val) / maxAbs * (MY - 6), val !== 0 ? 3 : 1);
          const x = i * (BW + GAP);
          const y = val >= 0 ? MY - barH : MY;
          const fill = val > 0 ? "#10b981" : val < 0 ? "#ef4444" : "#475569";
          const cur = m === ck;
          return (
            <g key={m}>
              <rect x={x} y={y} width={BW} height={barH} fill={fill} rx="2" opacity={cur ? 0.6 : 1} />
              {cur && <rect x={x} y={y} width={BW} height={barH} fill="none" stroke={fill} strokeWidth="1" rx="2" strokeDasharray="3 2" />}
              <text x={x + BW/2} y={CH + LH} textAnchor="middle" fill="#64748b" fontSize="9">{ML(m)}{cur ? "*" : ""}</text>
              {val !== 0 && <text x={x + BW/2} y={val >= 0 ? y - 3 : y + barH + 9} textAnchor="middle" fill={fill} fontSize="8" fontWeight="bold">{val >= 0 ? "+" : ""}{val.toFixed(1)}</text>}
            </g>
          );
        })}
      </svg>
      <div className="text-[9px] text-slate-600 text-right mt-0.5">* current month (partial)</div>
    </div>
  );
}

function PerformancePage({ trades, decisions, stats, positions }: {
  trades: Trade[]; decisions: Decision[];
  stats: StatsResponse | null; positions: Position[];
}) {
  const unrealizedTotal = positions.reduce((s, p) => s + p.unrealized_pl, 0);
  const closedPnl       = stats?.total_pnl ?? trades.filter(t => t.pnl != null).reduce((s,t) => s + (t.pnl ?? 0), 0);
  const totalReturn     = ((closedPnl + unrealizedTotal) / INITIAL_CAPITAL) * 100;
  const now             = new Date();
  const mDecisions      = decisions.filter(d => {
    const dt = new Date(d.decided_at.includes("T") ? d.decided_at : d.decided_at.replace(" ", "T") + "Z");
    return dt.getMonth() === now.getMonth() && dt.getFullYear() === now.getFullYear();
  });
  const claudeCost = mDecisions.length * CLAUDE_COST_PER_CALL;
  const totalCost  = REPLIT_MONTHLY_COST + claudeCost;
  const netPnl     = closedPnl - totalCost;

  const kpis = [
    { label: "Total Return", value: (totalReturn >= 0 ? "+" : "") + totalReturn.toFixed(2) + "%", color: totalReturn >= 0 ? "text-emerald-400" : "text-red-400" },
    { label: "Win Rate",     value: stats ? stats.win_rate.toFixed(1) + "%" : "—",               color: "text-sky-400" },
    { label: "Profit Factor",value: stats ? (stats.profit_factor >= 999 ? "∞" : stats.profit_factor.toFixed(2)) : "—", color: "text-violet-400" },
    { label: "Best Asset",   value: stats?.best_asset ?? "—",                                    color: "text-amber-400" },
  ];

  return (
    <div className="p-4 sm:p-6 space-y-5">
      {/* KPI row */}
      <div className="grid grid-cols-2 sm:grid-cols-4 gap-3">
        {kpis.map(k => (
          <div key={k.label} className="bg-slate-800 rounded-xl p-4 border border-slate-700/50">
            <div className="text-[10px] uppercase tracking-widest text-slate-500 mb-1">{k.label}</div>
            <div className={`text-2xl font-bold ${k.color}`}>{k.value}</div>
          </div>
        ))}
      </div>

      {/* Daily P&L chart */}
      <div className="bg-slate-800 rounded-xl border border-slate-700/50 overflow-hidden">
        <div className="px-4 py-3 border-b border-slate-700 flex items-center justify-between">
          <span className="text-xs font-bold text-slate-300 uppercase tracking-wider">Daily P&L</span>
          <span className="text-xs text-slate-600">closed trades · per day</span>
        </div>
        <div className="p-4">
          <DailyBarChart trades={trades} />
        </div>
      </div>

      {/* Monthly P&L chart */}
      <div className="bg-slate-800 rounded-xl border border-slate-700/50 overflow-hidden">
        <div className="px-4 py-3 border-b border-slate-700 flex items-center justify-between">
          <span className="text-xs font-bold text-slate-300 uppercase tracking-wider">Monthly P&L</span>
          <span className="text-xs text-slate-600">closed trades · by month</span>
        </div>
        <div className="p-4">
          <MonthlyBarChart trades={trades} />
        </div>
      </div>

      {/* Costs */}
      <div className="bg-slate-800 rounded-xl border border-slate-700/50 overflow-hidden">
        <div className="px-4 py-3 border-b border-slate-700">
          <span className="text-xs font-bold text-slate-300 uppercase tracking-wider">💸 Monthly Costs — {now.toLocaleString("default", { month: "long", year: "numeric" })}</span>
        </div>
        <div className="p-4 space-y-2 max-w-sm">
          {[
            ["Replit Core", `-$${REPLIT_MONTHLY_COST.toFixed(2)}`, "text-red-400"],
            [`Claude API (${mDecisions.length} calls × $0.003)`, `-$${claudeCost.toFixed(3)}`, "text-red-400"],
            ["Total Costs", `-$${totalCost.toFixed(2)}`, "text-red-400 font-bold border-t border-slate-700 pt-2"],
            ["Net P&L (after costs)", (netPnl >= 0 ? "+" : "") + `$${netPnl.toFixed(2)}`, netPnl >= 0 ? "text-emerald-400 font-bold" : "text-red-400 font-bold"],
          ].map(([label, value, cls]) => (
            <div key={label as string} className={`flex justify-between text-xs ${cls as string}`}>
              <span className="text-slate-400">{label as string}</span>
              <span className="font-mono">{value as string}</span>
            </div>
          ))}
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
  const [lastRefresh, setLastRefresh] = useState<Date>(new Date());
  const [error,      setError]      = useState(false);

  const fetchAll = useCallback(async () => {
    try {
      const [sRes, dRes, pRes, mRes, senRes, regRes, stRes, ppRes, stopsRes] = await Promise.all([
        fetch(`${BASE}/api/status`),
        fetch(`${BASE}/api/decisions`),
        fetch(`${BASE}/api/positions`),
        fetch(`${BASE}/api/movers`),
        fetch(`${BASE}/api/sentiment`),
        fetch(`${BASE}/api/regime`),
        fetch(`${BASE}/api/stats`),
        fetch(`${BASE}/api/partial-profits`),
        fetch(`${BASE}/api/stops`),
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
      setError(false);
    } catch { setError(true); }
    setLastRefresh(new Date());
  }, []);

  useEffect(() => {
    fetchAll();
    const id = setInterval(fetchAll, 15_000);
    return () => clearInterval(id);
  }, [fetchAll]);

  // Derived state
  const unrealizedTotal = positions.reduce((s, p) => s + p.unrealized_pl, 0);
  const closedPnl       = stats?.total_pnl ?? status?.recent_trades.filter(t => t.pnl != null).reduce((s,t) => s + (t.pnl ?? 0), 0) ?? 0;
  const portfolioValue  = INITIAL_CAPITAL + closedPnl + unrealizedTotal;
  const portfolioDelta  = ((closedPnl + unrealizedTotal) / INITIAL_CAPITAL) * 100;
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
        {activePage === "HOME"        && <HomePage positions={positions} decisions={decisions} partialProfits={partialProfits} stops={stops} totalPortfolio={portfolioValue} />}
        {activePage === "MARKET"      && <MarketPage movers={movers} sentiment={sentiment} regime={regime} />}
        {activePage === "SIGNALS"     && <SignalsPage decisions={decisions} />}
        {activePage === "PERFORMANCE" && <PerformancePage trades={allTrades} decisions={decisions} stats={stats} positions={positions} />}
      </div>
    </div>
  );
}
