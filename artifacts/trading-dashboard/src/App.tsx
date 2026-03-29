import { useEffect, useState } from "react";

const BASE = import.meta.env.BASE_URL.replace(/\/$/, "");

const CRYPTO_SYMBOLS = ["BTC/USD","ETH/USD","SOL/USD","AVAX/USD","DOGE/USD","XRP/USD","LINK/USD","SHIB/USD"];

const ALERT_KEYWORDS: [string, string][] = [
  ["trump", "TRUMP"],
  ["federal reserve", "FED"],
  ["emergency rate", "RATE"],
  ["war", "WAR"],
  ["sanctions", "SNCT"],
  ["default", "DFLT"],
  ["collapse", "CLPS"],
  ["crisis", "CRSS"],
];

const STOCK_SYMBOLS  = ["AAPL","NVDA","TSLA","META","GOOGL","MSFT","AMD"];
const ETF_SYMBOLS    = ["QQQ","SPY","ARKK"];
const ALL_SYMBOLS    = [...CRYPTO_SYMBOLS, ...STOCK_SYMBOLS, ...ETF_SYMBOLS];

interface Trade {
  symbol: string;
  action: string;
  price: number;
  qty: number;
  timestamp: string;
  pnl: number | null;
  status: string;
  close_reason: string | null;
}

interface Decision {
  symbol: string;
  decision: string;
  confidence: number;
  reasoning: string;
  decided_at: string;
}

interface Status {
  agent: string;
  timestamp: string;
  db_connected: boolean;
  total_trades: number;
  recent_trades: Trade[];
}

interface DecisionsResponse {
  db_connected: boolean;
  decisions: Decision[];
}

interface Mover {
  symbol: string;
  price: number;
  change_pct: number;
  volume: number;
  direction: "up" | "down";
}

interface MoversResponse { movers: Mover[]; ts?: string; error?: string; }
interface SentimentResponse { sentiment: string; score: number; headlines?: string[]; alerts?: string[]; ts?: string; error?: string; }
interface CalendarResponse { event: string | null; note: string; timing?: string; error?: string; }

// ── Small reusable components ────────────────────────────────────

function DecisionBadge({ decision }: { decision: string }) {
  const d = decision.toUpperCase();
  const colors: Record<string, string> = {
    BUY:  "bg-emerald-900/60 text-emerald-400 border border-emerald-700",
    SELL: "bg-red-900/60 text-red-400 border border-red-700",
    HOLD: "bg-slate-700/60 text-slate-400 border border-slate-600",
  };
  return (
    <span className={`inline-block px-2 py-0.5 rounded text-xs font-bold ${colors[d] ?? colors.HOLD}`}>{d}</span>
  );
}

function ConfBar({ value }: { value: number }) {
  const pct = Math.round(value * 100);
  const color = pct >= 85 ? "bg-emerald-500" : pct >= 60 ? "bg-sky-500" : "bg-slate-600";
  return (
    <div className="flex items-center gap-2">
      <div className="flex-1 bg-slate-700 rounded-full h-1.5 overflow-hidden">
        <div className={`${color} h-full rounded-full transition-all`} style={{ width: `${pct}%` }} />
      </div>
      <span className="text-xs text-slate-400 w-8 text-right">{pct}%</span>
    </div>
  );
}

function SymbolCard({ symbol, decision }: { symbol: string; decision?: Decision }) {
  const isCrypto = symbol.includes("/");
  const ticker   = symbol.replace("/USD", "").replace("/", "");
  const d        = decision?.decision?.toUpperCase() ?? null;
  const conf     = decision ? Math.round(decision.confidence * 100) : null;

  const signalColor = d === "BUY"  ? "border-emerald-600/60 bg-emerald-900/20"
                    : d === "SELL" ? "border-red-600/60 bg-red-900/20"
                    : decision     ? "border-slate-700 bg-slate-800/60"
                    :                "border-slate-800 bg-slate-800/30 opacity-50";
  const textColor   = d === "BUY"  ? "text-emerald-400"
                    : d === "SELL" ? "text-red-400"
                    : "text-slate-400";

  return (
    <div className={`rounded-lg border px-3 py-2.5 flex flex-col gap-1 transition-colors ${signalColor}`}>
      <div className="flex items-center justify-between">
        <span className="text-xs font-bold text-white">{ticker}</span>
        <span className="text-[9px] text-slate-600 uppercase">{isCrypto ? "crypto" : "stock"}</span>
      </div>
      {d ? (
        <>
          <span className={`text-[11px] font-semibold ${textColor}`}>{d}</span>
          <div className="flex items-center gap-1">
            <div className="flex-1 bg-slate-700 rounded-full h-1 overflow-hidden">
              <div className={`h-full rounded-full ${d === "BUY" ? "bg-emerald-500" : d === "SELL" ? "bg-red-500" : "bg-slate-500"}`} style={{ width: `${conf}%` }} />
            </div>
            <span className="text-[9px] text-slate-500">{conf}%</span>
          </div>
        </>
      ) : (
        <span className="text-[10px] text-slate-600">en attente…</span>
      )}
    </div>
  );
}

function StatCard({ label, value, sub, color = "text-white" }: { label: string; value: string; sub?: string; color?: string }) {
  return (
    <div className="bg-slate-800 rounded-xl p-4 sm:p-5 flex-1 min-w-[130px]">
      <div className="text-[10px] sm:text-xs uppercase tracking-widest text-slate-500 mb-1">{label}</div>
      <div className={`text-2xl sm:text-2xl font-bold ${color}`}>{value}</div>
      {sub && <div className="text-xs text-slate-600 mt-0.5">{sub}</div>}
    </div>
  );
}

// ── Monthly P&L bar chart (SVG) ──────────────────────────────────

function MonthlyPnlChart({ trades }: { trades: Trade[] }) {
  const closed = trades.filter(t => t.pnl != null);
  if (closed.length === 0) {
    return <div className="text-xs text-slate-600 text-center py-6">No closed trades yet — P&L chart will appear here.</div>;
  }

  const byMonth: Record<string, number> = {};
  closed.forEach(t => {
    const raw = t.timestamp.includes("T") ? t.timestamp : t.timestamp.replace(" ", "T") + "Z";
    const d   = new Date(raw);
    const key = `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, "0")}`;
    byMonth[key] = (byMonth[key] ?? 0) + (t.pnl ?? 0);
  });

  const now = new Date();
  const currentKey = `${now.getFullYear()}-${String(now.getMonth() + 1).padStart(2, "0")}`;
  if (!(currentKey in byMonth)) byMonth[currentKey] = 0;

  const months = Object.keys(byMonth).sort();
  const values = months.map(k => byMonth[k]);
  const maxAbs = Math.max(...values.map(Math.abs), 0.01);

  const BAR_W = 36;
  const GAP   = 10;
  const CH    = 72;
  const LABEL_H = 18;
  const TOTAL_W = months.length * (BAR_W + GAP) - GAP;
  const MID_Y   = CH / 2;

  const monthLabel = (key: string) => {
    const [, mm] = key.split("-");
    return ["Jan","Feb","Mar","Apr","May","Jun","Jul","Aug","Sep","Oct","Nov","Dec"][parseInt(mm,10)-1];
  };

  return (
    <div className="overflow-x-auto">
      <svg
        viewBox={`-4 -8 ${TOTAL_W + 8} ${CH + LABEL_H + 16}`}
        width="100%"
        style={{ minWidth: `${Math.max(TOTAL_W, 200)}px` }}
      >
        <line x1={-4} y1={MID_Y} x2={TOTAL_W + 4} y2={MID_Y} stroke="#334155" strokeWidth="1" />
        {months.map((m, i) => {
          const val  = values[i];
          const barH = Math.max(Math.abs(val) / maxAbs * (MID_Y - 6), val !== 0 ? 3 : 1);
          const x    = i * (BAR_W + GAP);
          const y    = val >= 0 ? MID_Y - barH : MID_Y;
          const fill = val > 0 ? "#10b981" : val < 0 ? "#ef4444" : "#475569";
          const isCurrentMonth = m === currentKey;

          return (
            <g key={m}>
              <rect x={x} y={y} width={BAR_W} height={barH} fill={fill} rx="2" opacity={isCurrentMonth ? 0.6 : 1} />
              {isCurrentMonth && (
                <rect x={x} y={y} width={BAR_W} height={barH} fill="none" stroke={fill} strokeWidth="1" rx="2" strokeDasharray="3 2" />
              )}
              <text x={x + BAR_W / 2} y={CH + LABEL_H} textAnchor="middle" fill="#64748b" fontSize="9">
                {monthLabel(m)}{isCurrentMonth ? "*" : ""}
              </text>
              {val !== 0 && (
                <text
                  x={x + BAR_W / 2}
                  y={val >= 0 ? y - 3 : y + barH + 9}
                  textAnchor="middle"
                  fill={fill}
                  fontSize="8"
                  fontWeight="bold"
                >
                  {val >= 0 ? "+" : ""}{val.toFixed(1)}
                </text>
              )}
            </g>
          );
        })}
      </svg>
      <div className="text-[9px] text-slate-600 text-right pr-1 mt-0.5">* current month (partial)</div>
    </div>
  );
}

// ── Headline modal ───────────────────────────────────────────────

function HeadlineModal({ headline, overallSentiment, onClose }: {
  headline: string;
  overallSentiment: string;
  onClose: () => void;
}) {
  const hl      = headline.toLowerCase();
  const tags    = ALERT_KEYWORDS.filter(([kw]) => hl.includes(kw)).map(([, label]) => label);
  const bull    = overallSentiment.includes("bullish");
  const bear    = overallSentiment.includes("bearish");
  const impact  = bull ? "Bullish signal" : bear ? "Bearish signal" : "Neutral / mixed";
  const impCol  = bull ? "text-emerald-400" : bear ? "text-red-400" : "text-slate-400";
  const newsUrl = `https://www.google.com/search?q=${encodeURIComponent(headline)}&tbm=nws`;

  return (
    <div
      className="fixed inset-0 z-50 flex items-end sm:items-center justify-center p-3 sm:p-6 bg-black/65 backdrop-blur-sm"
      onClick={onClose}
    >
      <div
        className="bg-slate-800 border border-slate-700 rounded-xl p-5 w-full max-w-md shadow-2xl"
        onClick={e => e.stopPropagation()}
      >
        <div className="flex items-start justify-between gap-3 mb-3">
          <h3 className="text-sm font-semibold text-white leading-snug">{headline}</h3>
          <button onClick={onClose} className="text-slate-500 hover:text-white transition-colors text-xl leading-none flex-shrink-0">✕</button>
        </div>

        {tags.length > 0 && (
          <div className="flex flex-wrap gap-1 mb-3">
            {tags.map(t => (
              <span key={t} className="text-[9px] font-bold text-amber-400">[{t}]</span>
            ))}
          </div>
        )}

        <div className="bg-slate-900/50 rounded-lg p-3 mb-4 space-y-2 border border-slate-700/40">
          <p className="text-xs text-slate-400 leading-relaxed">
            Detected by the market scanner from MarketWatch, CNBC, and Google Finance RSS feeds.
            This headline is weighted in the sentiment score used by the AI trading agent.
          </p>
          <p className="text-xs text-slate-400 leading-relaxed">
            Financial news is filtered for relevance using 40+ market keywords. High-alert terms
            (FED, TRUMP, WAR…) carry extra weight and can override technical signals.
          </p>
          <p className="text-xs text-slate-400 leading-relaxed">
            Sentiment is scored on a scale of −10 to +10. The overall score feeds directly into
            the Claude prompt as market context before each analysis cycle.
          </p>
        </div>

        <div className="flex items-center justify-between">
          <div>
            <div className="text-[10px] text-slate-500 uppercase tracking-wide mb-0.5">Market impact</div>
            <div className={`text-xs font-semibold ${impCol}`}>{impact}</div>
          </div>
          <a
            href={newsUrl}
            target="_blank"
            rel="noopener noreferrer"
            className="text-xs text-sky-400 hover:text-sky-200 underline transition-colors"
          >
            Read more →
          </a>
        </div>
      </div>
    </div>
  );
}

// ── Signal reasoning modal ───────────────────────────────────────

function SignalModal({ signal, onClose }: { signal: Decision; onClose: () => void }) {
  return (
    <div
      className="fixed inset-0 z-50 flex items-end sm:items-center justify-center p-3 sm:p-6 bg-black/65 backdrop-blur-sm"
      onClick={onClose}
    >
      <div
        className="bg-slate-800 border border-slate-700 rounded-xl p-5 w-full max-w-md shadow-2xl"
        onClick={e => e.stopPropagation()}
      >
        <div className="flex items-center justify-between mb-4">
          <div className="flex items-center gap-2">
            <span className="text-base font-bold text-white">{signal.symbol}</span>
            <DecisionBadge decision={signal.decision} />
          </div>
          <button onClick={onClose} className="text-slate-500 hover:text-white transition-colors text-xl leading-none">✕</button>
        </div>

        <div className="mb-3">
          <ConfBar value={signal.confidence} />
        </div>
        <div className="text-[10px] text-slate-600 mb-3">{fmtDateTime(signal.decided_at)}</div>

        <div className="bg-slate-900/50 rounded-lg p-3 border-l-2 border-sky-700/50 max-h-52 overflow-y-auto">
          <p className="text-xs text-slate-300 leading-relaxed whitespace-pre-wrap">{signal.reasoning}</p>
        </div>

        <button
          onClick={onClose}
          className="mt-4 w-full text-xs text-slate-500 hover:text-slate-300 transition-colors border border-slate-700 rounded-lg py-2"
        >
          Close
        </button>
      </div>
    </div>
  );
}

// ── Helpers ──────────────────────────────────────────────────────

function fmtTime(s: string) {
  const d = new Date(s.includes("T") ? s : s.replace(" ", "T") + "Z");
  return d.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
}

function fmtDateTime(s: string) {
  const d = new Date(s.includes("T") ? s : s.replace(" ", "T") + "Z");
  return d.toLocaleString([], { month: "short", day: "numeric", hour: "2-digit", minute: "2-digit" });
}

// ── App ──────────────────────────────────────────────────────────

export default function App() {
  const [status, setStatus]       = useState<Status | null>(null);
  const [decisions, setDecisions] = useState<Decision[]>([]);
  const [error, setError]         = useState(false);
  const [lastRefresh, setLastRefresh] = useState<Date>(new Date());
  const [expandedIdx, setExpandedIdx] = useState<number | null>(null);
  const [movers, setMovers]       = useState<Mover[]>([]);
  const [sentiment, setSentiment] = useState<SentimentResponse | null>(null);
  const [calendar, setCalendar]   = useState<CalendarResponse | null>(null);

  const [headlineModal, setHeadlineModal] = useState<{ text: string; overallSentiment: string } | null>(null);
  const [signalModal, setSignalModal]     = useState<Decision | null>(null);

  async function fetchAll() {
    try {
      const [sRes, dRes] = await Promise.all([
        fetch(`${BASE}/api/status`),
        fetch(`${BASE}/api/decisions`),
      ]);
      if (sRes.ok) setStatus(await sRes.json() as Status);
      if (dRes.ok) {
        const data: DecisionsResponse = await dRes.json();
        setDecisions(data.decisions ?? []);
      }
      setError(false);
    } catch { setError(true); }
    setLastRefresh(new Date());
  }

  async function fetchMarket() {
    try {
      const [mRes, sRes, cRes] = await Promise.all([
        fetch(`${BASE}/api/movers`),
        fetch(`${BASE}/api/sentiment`),
        fetch(`${BASE}/api/calendar`),
      ]);
      if (mRes.ok) { const d: MoversResponse = await mRes.json(); setMovers(d.movers ?? []); }
      if (sRes.ok) setSentiment(await sRes.json() as SentimentResponse);
      if (cRes.ok) setCalendar(await cRes.json() as CalendarResponse);
    } catch { /* non-critical */ }
  }

  useEffect(() => {
    fetchAll();
    fetchMarket();
    const id1 = setInterval(fetchAll, 30_000);
    const id2 = setInterval(fetchMarket, 60_000);
    return () => { clearInterval(id1); clearInterval(id2); };
  }, []);

  const totalPnl = status?.recent_trades
    .filter(t => t.pnl != null)
    .reduce((sum, t) => sum + (t.pnl ?? 0), 0) ?? 0;

  const latestBySymbol: Record<string, Decision> = {};
  decisions.forEach(d => { if (!latestBySymbol[d.symbol]) latestBySymbol[d.symbol] = d; });
  const latestDecisions = Object.values(latestBySymbol).sort((a, b) => b.decided_at.localeCompare(a.decided_at));

  const PREVIEW_LEN = 120;

  return (
    <div className="min-h-screen bg-slate-900 text-slate-200">

      {/* ── Sticky mobile mini-header ── */}
      <div className="sm:hidden sticky top-0 z-40 bg-slate-900/95 backdrop-blur-sm border-b border-slate-800 px-4 py-2.5 flex items-center justify-between">
        <div className="flex items-center gap-2">
          <span className={`w-2 h-2 rounded-full flex-shrink-0 ${error ? "bg-red-400" : "bg-emerald-400"}`} />
          <span className={`text-sm font-bold ${totalPnl >= 0 ? "text-emerald-400" : "text-red-400"}`}>
            {totalPnl >= 0 ? "+" : ""}{totalPnl.toFixed(2)} $
          </span>
        </div>
        {decisions.length > 0 && (
          <div className="text-[11px] text-slate-500">
            Last signal {fmtTime(decisions[0].decided_at)}
          </div>
        )}
      </div>

      <div className="p-4 sm:p-6 md:p-10">

        {/* ── Header ── */}
        <div className="flex items-center justify-between mb-6 sm:mb-8">
          <div>
            <h1 className="text-xl sm:text-2xl font-bold text-sky-400">⚡ Trading Agent</h1>
            <p className="text-slate-500 text-xs sm:text-sm mt-0.5">AI-powered paper trading · Claude + Alpaca · crypto weekend mode</p>
          </div>
          <div className="text-right">
            <div className="text-[10px] text-slate-600">Last refresh</div>
            <div className="text-xs sm:text-sm text-slate-400">{lastRefresh.toLocaleTimeString()}</div>
            <button onClick={fetchAll} className="mt-1 text-xs text-sky-500 hover:text-sky-300 transition-colors">
              Refresh ↻
            </button>
          </div>
        </div>

        {/* ── KPI cards ── */}
        <div className="flex flex-wrap gap-3 mb-6 sm:mb-8">
          <StatCard label="Agent"        value={error ? "⚠ Error" : status ? "🟢 Running" : "⏳ Loading"} color={error ? "text-red-400" : "text-emerald-400"} />
          <StatCard label="Total Trades" value={status ? String(status.total_trades) : "—"} color="text-sky-400" />
          <StatCard label="Closed P&L"  value={status ? (totalPnl >= 0 ? "+" : "") + totalPnl.toFixed(2) + " $" : "—"} color={totalPnl >= 0 ? "text-emerald-400" : "text-red-400"} />
          <StatCard label="AI Signals"  value={decisions.length > 0 ? String(decisions.length) : "—"} sub={decisions.length > 0 ? `last: ${fmtTime(decisions[0].decided_at)}` : undefined} color="text-violet-400" />
          <StatCard label="DB"          value={status?.db_connected ? "Connected" : "—"} color={status?.db_connected ? "text-emerald-400" : "text-slate-500"} />
        </div>

        {/* ── Watchlist — mobile summary / desktop grid ── */}
        <div className="mb-6">

          {/* Mobile: single-line summary */}
          <div className="sm:hidden bg-slate-800/50 rounded-lg px-4 py-3 flex items-center justify-between">
            <span className="text-xs text-slate-400">
              <span className="font-semibold text-white">{CRYPTO_SYMBOLS.length} crypto</span> watching — weekend mode
            </span>
            <span className="text-[10px] text-slate-600">{ALL_SYMBOLS.length} total assets</span>
          </div>

          {/* Desktop: full grid */}
          <div className="hidden sm:block">
            <div className="flex items-center justify-between mb-3">
              <h2 className="text-xs font-semibold text-slate-500 uppercase tracking-wider">Watchlist</h2>
              <span className="text-xs text-slate-700">{ALL_SYMBOLS.length} actifs · weekend = crypto seulement</span>
            </div>
            <div className="mb-2">
              <p className="text-[10px] text-slate-600 mb-2 uppercase tracking-wider">Crypto</p>
              <div className="grid grid-cols-4 sm:grid-cols-8 gap-2">
                {CRYPTO_SYMBOLS.map(s => <SymbolCard key={s} symbol={s} decision={latestBySymbol[s]} />)}
              </div>
            </div>
            <div className="mt-3">
              <p className="text-[10px] text-slate-600 mb-2 uppercase tracking-wider">Actions &amp; ETF · marché fermé le weekend</p>
              <div className="grid grid-cols-4 sm:grid-cols-10 gap-2">
                {[...STOCK_SYMBOLS, ...ETF_SYMBOLS].map(s => <SymbolCard key={s} symbol={s} decision={latestBySymbol[s]} />)}
              </div>
            </div>
          </div>
        </div>

        {/* ── Market Intelligence row ── */}
        <div className="grid grid-cols-1 md:grid-cols-3 gap-4 mb-6">

          {/* TOP MOVERS */}
          <div className="bg-slate-800 rounded-xl overflow-hidden">
            <div className="px-4 py-3 border-b border-slate-700 flex items-center justify-between">
              <h2 className="text-xs font-semibold text-slate-300 uppercase tracking-wider">🔥 Top Movers Today</h2>
              <span className="text-[10px] text-slate-600">60s refresh</span>
            </div>
            <div className="p-3 space-y-1.5">
              {movers.length === 0 && (
                <div className="text-xs text-slate-600 text-center py-4">
                  {status ? "Market closed or no movers" : "Loading…"}
                </div>
              )}
              {movers.map(m => (
                <div key={m.symbol} className="flex items-center justify-between gap-2 px-2 py-1.5 rounded bg-slate-700/40">
                  <div className="flex items-center gap-2 min-w-0">
                    <span className={`text-sm font-bold ${m.direction === "up" ? "text-emerald-400" : "text-red-400"}`}>
                      {m.direction === "up" ? "↑" : "↓"}
                    </span>
                    <span className="text-xs font-semibold text-white truncate">{m.symbol}</span>
                  </div>
                  <span className={`text-sm sm:text-xs font-bold flex-shrink-0 ${m.direction === "up" ? "text-emerald-400" : "text-red-400"}`}>
                    {m.change_pct > 0 ? "+" : ""}{m.change_pct.toFixed(1)}%
                  </span>
                </div>
              ))}
            </div>
          </div>

          {/* MARKET SENTIMENT */}
          <div className="bg-slate-800 rounded-xl overflow-hidden">
            <div className="px-4 py-3 border-b border-slate-700 flex items-center justify-between">
              <h2 className="text-xs font-semibold text-slate-300 uppercase tracking-wider">📰 Market Sentiment</h2>
              <span className="text-[10px] text-slate-600">60s refresh</span>
            </div>
            <div className="p-4">
              {!sentiment ? (
                <div className="text-[10px] text-slate-600 text-center py-4">Loading…</div>
              ) : (() => {
                const s         = sentiment.sentiment;
                const scoreColor = s.includes("bullish") ? "text-emerald-400" : s.includes("bearish") ? "text-red-400" : "text-slate-500";
                const badgeColor = s.includes("bullish") ? "text-emerald-400" : s.includes("bearish") ? "text-red-400" : "text-slate-400";
                const emoji      = s === "very_bullish" ? "🚀" : s === "bullish" ? "🟢" : s === "very_bearish" ? "💀" : s === "bearish" ? "🔴" : "⚪";
                const headlines  = (sentiment.headlines ?? []).slice(0, 3);
                return (
                  <div className="space-y-2.5">
                    <div className="flex items-center gap-2">
                      <span className={`text-[10px] font-bold uppercase tracking-wide ${badgeColor}`}>{emoji} {s.replace(/_/g, " ")}</span>
                      <span className="text-slate-700 text-[10px]">·</span>
                      <span className={`text-[10px] font-semibold ${scoreColor}`}>score {sentiment.score > 0 ? "+" : ""}{sentiment.score}</span>
                    </div>

                    <div className="space-y-1.5">
                      {headlines.length === 0 ? (
                        <div className="text-[10px] text-slate-600">No market headlines available</div>
                      ) : headlines.map((h, i) => {
                        const hl   = h.toLowerCase();
                        const tags = ALERT_KEYWORDS.filter(([kw]) => hl.includes(kw)).map(([, label]) => label);
                        return (
                          <button
                            key={i}
                            onClick={() => setHeadlineModal({ text: h, overallSentiment: s })}
                            className="w-full flex items-baseline gap-1 min-w-0 text-left group hover:opacity-80 transition-opacity cursor-pointer"
                          >
                            <span className="text-slate-600 flex-shrink-0 text-[10px]">•</span>
                            {tags.map(t => (
                              <span key={t} className="text-[8px] font-bold text-amber-400 flex-shrink-0 leading-none">[{t}]</span>
                            ))}
                            <span className="text-[10px] text-slate-400 group-hover:text-slate-200 truncate min-w-0 leading-snug transition-colors">{h}</span>
                          </button>
                        );
                      })}
                    </div>

                    {sentiment.ts && (
                      <div className="text-[9px] text-slate-600 pt-1.5 border-t border-slate-700/40">
                        Updated {sentiment.ts} · tap headline for details
                      </div>
                    )}
                  </div>
                );
              })()}
            </div>
          </div>

          {/* ECONOMIC CALENDAR */}
          <div className="bg-slate-800 rounded-xl overflow-hidden">
            <div className="px-4 py-3 border-b border-slate-700 flex items-center justify-between">
              <h2 className="text-xs font-semibold text-slate-300 uppercase tracking-wider">📅 Economic Calendar</h2>
              <span className="text-[10px] text-slate-600">60s refresh</span>
            </div>
            <div className="p-4">
              {!calendar ? (
                <div className="text-xs text-slate-600 text-center py-4">Loading…</div>
              ) : calendar.event ? (
                <div className="space-y-3">
                  <div className="bg-amber-900/20 border border-amber-600/50 rounded-lg px-4 py-3">
                    <div className="flex items-start gap-2">
                      <span className="text-amber-400 text-lg leading-none">⚡</span>
                      <div>
                        <div className="text-sm font-bold text-amber-300">{calendar.event}</div>
                        <div className="text-[10px] text-amber-500 mt-0.5 uppercase tracking-wide">
                          {calendar.timing === "today" ? "TODAY" : "UPCOMING"}
                        </div>
                      </div>
                    </div>
                  </div>
                  <p className="text-[10px] text-slate-500 leading-relaxed">{calendar.note}</p>
                </div>
              ) : (
                <div className="space-y-2 py-2">
                  <div className="flex items-center gap-2 text-emerald-400">
                    <span className="text-lg">✅</span>
                    <span className="text-xs font-semibold">No high-impact events</span>
                  </div>
                  <p className="text-[10px] text-slate-600">Next events: Fed Meeting Apr 2 · NFP Apr 3 · CPI Apr 14</p>
                </div>
              )}
            </div>
          </div>
        </div>

        {/* ── AI Analysis + Signal History ── */}
        <div className="grid grid-cols-1 lg:grid-cols-2 gap-6 mb-6">

          {/* Latest AI Analysis */}
          <div className="bg-slate-800 rounded-xl overflow-hidden">
            <div className="px-6 py-4 border-b border-slate-700 flex items-center justify-between">
              <h2 className="text-sm font-semibold text-slate-300 uppercase tracking-wider">Latest AI Analysis</h2>
              <span className="text-xs text-slate-600">per symbol · most recent</span>
            </div>
            <div className="divide-y divide-slate-700/50">
              {latestDecisions.length === 0 && (
                <div className="px-6 py-8 text-center text-slate-600 text-sm">
                  No signals yet — agent is analyzing markets.
                </div>
              )}
              {latestDecisions.map((d, i) => {
                const preview  = d.reasoning.length > PREVIEW_LEN
                  ? d.reasoning.slice(0, PREVIEW_LEN).trimEnd() + "…"
                  : d.reasoning;
                const hasMore  = d.reasoning.length > PREVIEW_LEN;
                const expanded = expandedIdx === i;

                return (
                  <div key={d.symbol} className="px-5 py-4">
                    <div className="flex items-center justify-between mb-2">
                      <div className="flex items-center gap-2">
                        <span className="font-semibold text-white text-sm">{d.symbol}</span>
                        <DecisionBadge decision={d.decision} />
                      </div>
                      <span className="text-xs text-slate-600">{fmtDateTime(d.decided_at)}</span>
                    </div>
                    <ConfBar value={d.confidence} />
                    <p className="mt-2 text-xs text-slate-400 leading-relaxed border-l-2 border-slate-700 pl-3">
                      {expanded ? d.reasoning : preview}
                    </p>
                    {hasMore && (
                      <button
                        onClick={() => setExpandedIdx(expanded ? null : i)}
                        className="mt-1 text-[10px] text-slate-600 hover:text-slate-400 transition-colors"
                      >
                        {expanded ? "▲ less" : "▼ more"}
                      </button>
                    )}
                  </div>
                );
              })}
            </div>
          </div>

          {/* Signal History */}
          <div className="bg-slate-800 rounded-xl overflow-hidden">
            <div className="px-6 py-4 border-b border-slate-700 flex items-center justify-between">
              <h2 className="text-sm font-semibold text-slate-300 uppercase tracking-wider">Signal History</h2>
              <span className="text-xs text-slate-600">tap row for reasoning</span>
            </div>
            <div className="overflow-y-auto max-h-[420px]">
              <table className="w-full text-sm">
                <thead className="sticky top-0 bg-slate-900 z-10">
                  <tr className="text-slate-500 text-xs uppercase tracking-wider">
                    <th className="hidden sm:table-cell px-4 py-2 text-left">Time</th>
                    <th className="px-4 py-2 text-left">Symbol</th>
                    <th className="px-4 py-2 text-left">Signal</th>
                    <th className="px-4 py-2 text-right">Conf.</th>
                  </tr>
                </thead>
                <tbody>
                  {decisions.length === 0 && (
                    <tr><td colSpan={4} className="px-4 py-8 text-center text-slate-600">No signals yet.</td></tr>
                  )}
                  {decisions.slice(0, 20).map((d, i) => (
                    <tr
                      key={i}
                      onClick={() => setSignalModal(d)}
                      className="border-t border-slate-700/40 hover:bg-slate-700/30 active:bg-slate-700/50 transition-colors cursor-pointer"
                    >
                      <td className="hidden sm:table-cell px-4 py-2.5 text-slate-500 text-xs whitespace-nowrap">{fmtTime(d.decided_at)}</td>
                      <td className="px-4 py-2.5 font-medium text-white text-xs">{d.symbol}</td>
                      <td className="px-4 py-2.5"><DecisionBadge decision={d.decision} /></td>
                      <td className="px-4 py-2.5 text-right text-xs text-slate-400">{Math.round(d.confidence * 100)}%</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          </div>
        </div>

        {/* ── Executed Trades ── */}
        <div className="bg-slate-800 rounded-xl overflow-hidden mb-6">
          <div className="px-6 py-4 border-b border-slate-700 flex items-center justify-between">
            <h2 className="text-sm font-semibold text-slate-300 uppercase tracking-wider">Executed Trades</h2>
            <span className="text-xs text-slate-600">paper trading only</span>
          </div>
          <div className="overflow-x-auto">
            <table className="w-full text-sm">
              <thead>
                <tr className="bg-slate-900 text-slate-500 text-xs uppercase tracking-wider">
                  <th className="px-6 py-3 text-left">Time</th>
                  <th className="px-6 py-3 text-left">Symbol</th>
                  <th className="px-6 py-3 text-left">Side</th>
                  <th className="px-6 py-3 text-right">Price</th>
                  <th className="px-6 py-3 text-right">Qty</th>
                  <th className="px-6 py-3 text-left">Status</th>
                  <th className="px-6 py-3 text-right">P&amp;L</th>
                </tr>
              </thead>
              <tbody>
                {!status && !error && (
                  <tr><td colSpan={7} className="px-6 py-8 text-center text-slate-600">Loading…</td></tr>
                )}
                {error && (
                  <tr><td colSpan={7} className="px-6 py-8 text-center text-red-500">Could not reach the API.</td></tr>
                )}
                {status && status.recent_trades.length === 0 && (
                  <tr>
                    <td colSpan={7} className="px-6 py-10 text-center text-slate-600">
                      No trades executed yet.
                      <br />
                      <span className="text-xs mt-1 block">
                        L'agent requiert ≥70% de confiance pour passer un ordre. Les signaux sont enregistrés ci-dessus.
                      </span>
                    </td>
                  </tr>
                )}
                {status?.recent_trades.map((t, i) => (
                  <tr key={i} className="border-t border-slate-700/50 hover:bg-slate-700/30 transition-colors">
                    <td className="px-6 py-3 text-slate-400 whitespace-nowrap text-xs">{t.timestamp ? fmtDateTime(t.timestamp) : "—"}</td>
                    <td className="px-6 py-3 font-semibold text-white">{t.symbol}</td>
                    <td className="px-6 py-3"><DecisionBadge decision={t.action} /></td>
                    <td className="px-6 py-3 text-right text-slate-300">${Number(t.price).toFixed(2)}</td>
                    <td className="px-6 py-3 text-right text-slate-300">{t.qty}</td>
                    <td className="px-6 py-3 text-xs text-slate-500 capitalize">{t.status}</td>
                    <td className="px-6 py-3 text-right">
                      {t.pnl != null ? (
                        <span className={t.pnl >= 0 ? "text-emerald-400" : "text-red-400"}>
                          {t.pnl >= 0 ? "+" : ""}{t.pnl.toFixed(2)}
                        </span>
                      ) : <span className="text-slate-600">—</span>}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </div>

        {/* ── Monthly P&L chart ── */}
        <div className="bg-slate-800 rounded-xl overflow-hidden mb-6">
          <div className="px-6 py-4 border-b border-slate-700 flex items-center justify-between">
            <h2 className="text-sm font-semibold text-slate-300 uppercase tracking-wider">Monthly P&amp;L</h2>
            <span className="text-xs text-slate-600">closed trades · by month</span>
          </div>
          <div className="p-4 sm:p-6">
            <MonthlyPnlChart trades={status?.recent_trades ?? []} />
          </div>
        </div>

        <footer className="mt-4 text-center text-xs text-slate-700">
          Trading Agent · Paper Trading Only · Not Financial Advice
        </footer>
      </div>

      {/* ── Modals ── */}
      {headlineModal && (
        <HeadlineModal
          headline={headlineModal.text}
          overallSentiment={headlineModal.overallSentiment}
          onClose={() => setHeadlineModal(null)}
        />
      )}
      {signalModal && (
        <SignalModal
          signal={signalModal}
          onClose={() => setSignalModal(null)}
        />
      )}
    </div>
  );
}
