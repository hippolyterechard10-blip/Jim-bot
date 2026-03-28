import { useEffect, useState } from "react";

const BASE = import.meta.env.BASE_URL.replace(/\/$/, "");

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

function DecisionBadge({ decision }: { decision: string }) {
  const d = decision.toUpperCase();
  const colors: Record<string, string> = {
    BUY: "bg-emerald-900/60 text-emerald-400 border border-emerald-700",
    SELL: "bg-red-900/60 text-red-400 border border-red-700",
    HOLD: "bg-slate-700/60 text-slate-400 border border-slate-600",
  };
  return (
    <span className={`inline-block px-2 py-0.5 rounded text-xs font-bold ${colors[d] ?? colors.HOLD}`}>
      {d}
    </span>
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

function StatCard({ label, value, sub, color = "text-white" }: { label: string; value: string; sub?: string; color?: string }) {
  return (
    <div className="bg-slate-800 rounded-xl p-5 min-w-[150px]">
      <div className="text-xs uppercase tracking-widest text-slate-500 mb-1">{label}</div>
      <div className={`text-2xl font-bold ${color}`}>{value}</div>
      {sub && <div className="text-xs text-slate-600 mt-0.5">{sub}</div>}
    </div>
  );
}

function fmtTime(s: string) {
  const d = new Date(s.includes("T") ? s : s.replace(" ", "T") + "Z");
  return d.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
}

function fmtDateTime(s: string) {
  const d = new Date(s.includes("T") ? s : s.replace(" ", "T") + "Z");
  return d.toLocaleString([], { month: "short", day: "numeric", hour: "2-digit", minute: "2-digit" });
}

export default function App() {
  const [status, setStatus] = useState<Status | null>(null);
  const [decisions, setDecisions] = useState<Decision[]>([]);
  const [error, setError] = useState(false);
  const [lastRefresh, setLastRefresh] = useState<Date>(new Date());
  const [expandedIdx, setExpandedIdx] = useState<number | null>(null);

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
    } catch {
      setError(true);
    }
    setLastRefresh(new Date());
  }

  useEffect(() => {
    fetchAll();
    const id = setInterval(fetchAll, 30_000);
    return () => clearInterval(id);
  }, []);

  const totalPnl = status?.recent_trades
    .filter((t) => t.pnl != null)
    .reduce((sum, t) => sum + (t.pnl ?? 0), 0) ?? 0;

  const latestBySymbol: Record<string, Decision> = {};
  decisions.forEach((d) => {
    if (!latestBySymbol[d.symbol]) latestBySymbol[d.symbol] = d;
  });
  const latestDecisions = Object.values(latestBySymbol).sort((a, b) =>
    b.decided_at.localeCompare(a.decided_at)
  );

  return (
    <div className="min-h-screen bg-slate-900 text-slate-200 p-6 md:p-10">
      {/* Header */}
      <div className="flex items-center justify-between mb-8">
        <div>
          <h1 className="text-2xl font-bold text-sky-400">⚡ Trading Agent</h1>
          <p className="text-slate-500 text-sm mt-0.5">AI-powered paper trading · Claude + Alpaca · crypto weekend mode</p>
        </div>
        <div className="text-right">
          <div className="text-xs text-slate-600">Last refresh</div>
          <div className="text-sm text-slate-400">{lastRefresh.toLocaleTimeString()}</div>
          <button
            onClick={fetchAll}
            className="mt-1 text-xs text-sky-500 hover:text-sky-300 transition-colors"
          >
            Refresh ↻
          </button>
        </div>
      </div>

      {/* Stat cards */}
      <div className="flex flex-wrap gap-4 mb-8">
        <StatCard
          label="Agent"
          value={error ? "⚠ Error" : status ? "🟢 Running" : "⏳ Loading"}
          color={error ? "text-red-400" : "text-emerald-400"}
        />
        <StatCard
          label="Total Trades"
          value={status ? String(status.total_trades) : "—"}
          color="text-sky-400"
        />
        <StatCard
          label="Closed P&L"
          value={status ? (totalPnl >= 0 ? "+" : "") + totalPnl.toFixed(2) + " $" : "—"}
          color={totalPnl >= 0 ? "text-emerald-400" : "text-red-400"}
        />
        <StatCard
          label="AI Signals"
          value={decisions.length > 0 ? String(decisions.length) : "—"}
          sub={decisions.length > 0 ? `last: ${fmtTime(decisions[0].decided_at)}` : undefined}
          color="text-violet-400"
        />
        <StatCard
          label="DB"
          value={status?.db_connected ? "Connected" : "—"}
          color={status?.db_connected ? "text-emerald-400" : "text-slate-500"}
        />
      </div>

      {/* Two-column layout */}
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
            {latestDecisions.map((d, i) => (
              <div key={d.symbol} className="px-5 py-4">
                <div className="flex items-center justify-between mb-2">
                  <div className="flex items-center gap-2">
                    <span className="font-semibold text-white text-sm">{d.symbol}</span>
                    <DecisionBadge decision={d.decision} />
                  </div>
                  <span className="text-xs text-slate-600">{fmtDateTime(d.decided_at)}</span>
                </div>
                <ConfBar value={d.confidence} />
                <button
                  onClick={() => setExpandedIdx(expandedIdx === i ? null : i)}
                  className="mt-2 text-xs text-slate-500 hover:text-slate-300 transition-colors text-left w-full"
                >
                  {expandedIdx === i ? "▲ hide reasoning" : "▼ show reasoning"}
                </button>
                {expandedIdx === i && (
                  <p className="mt-2 text-xs text-slate-400 leading-relaxed border-l-2 border-slate-700 pl-3">
                    {d.reasoning}
                  </p>
                )}
              </div>
            ))}
          </div>
        </div>

        {/* Signal History */}
        <div className="bg-slate-800 rounded-xl overflow-hidden">
          <div className="px-6 py-4 border-b border-slate-700 flex items-center justify-between">
            <h2 className="text-sm font-semibold text-slate-300 uppercase tracking-wider">Signal History</h2>
            <span className="text-xs text-slate-600">last 20</span>
          </div>
          <div className="overflow-y-auto max-h-[420px]">
            <table className="w-full text-sm">
              <thead className="sticky top-0 bg-slate-900 z-10">
                <tr className="text-slate-500 text-xs uppercase tracking-wider">
                  <th className="px-4 py-2 text-left">Time</th>
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
                  <tr key={i} className="border-t border-slate-700/40 hover:bg-slate-700/20 transition-colors">
                    <td className="px-4 py-2 text-slate-500 text-xs whitespace-nowrap">{fmtTime(d.decided_at)}</td>
                    <td className="px-4 py-2 font-medium text-white text-xs">{d.symbol}</td>
                    <td className="px-4 py-2"><DecisionBadge decision={d.decision} /></td>
                    <td className="px-4 py-2 text-right text-xs text-slate-400">{Math.round(d.confidence * 100)}%</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </div>
      </div>

      {/* Trades */}
      <div className="bg-slate-800 rounded-xl overflow-hidden">
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
                <th className="px-6 py-3 text-right">P&L</th>
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
                      Agent needs ≥85% confidence (weekend) to place orders. Signals are being logged above.
                    </span>
                  </td>
                </tr>
              )}
              {status?.recent_trades.map((t, i) => (
                <tr key={i} className="border-t border-slate-700/50 hover:bg-slate-700/30 transition-colors">
                  <td className="px-6 py-3 text-slate-400 whitespace-nowrap text-xs">
                    {t.timestamp ? fmtDateTime(t.timestamp) : "—"}
                  </td>
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

      <footer className="mt-8 text-center text-xs text-slate-700">
        Trading Agent · Paper Trading Only · Not Financial Advice
      </footer>
    </div>
  );
}
