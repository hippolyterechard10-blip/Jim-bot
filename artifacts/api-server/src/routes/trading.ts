import { Router, type IRouter } from "express";
import { existsSync } from "fs";
import Database from "better-sqlite3";

const router: IRouter = Router();

const DB_PATH =
  process.env["TRADING_DB_PATH"] ??
  "/home/runner/workspace/trading-agent/trading_memory.db";

function getDb(): Database.Database | null {
  if (!existsSync(DB_PATH)) {
    console.warn("[trading] DB not found at:", DB_PATH);
    return null;
  }
  try {
    return new Database(DB_PATH, { readonly: true });
  } catch (e) {
    console.error("[trading] Failed to open DB:", e);
    return null;
  }
}

router.get("/status", (_req, res) => {
  const db = getDb();
  let trades: unknown[] = [];
  let stats = { total_trades: 0, db_connected: false };

  if (db) {
    try {
      trades = db
        .prepare(
          `SELECT symbol, side as action, entry_price as price, qty,
           entry_at as timestamp, pnl, status, close_reason
           FROM trades ORDER BY entry_at DESC LIMIT 50`,
        )
        .all();
      const row = db
        .prepare("SELECT COUNT(*) as count FROM trades")
        .get() as { count: number };
      stats = { total_trades: row.count, db_connected: true };
      db.close();
    } catch (e) {
      console.error("[trading] status query error:", e);
      try { db.close(); } catch { /* ignore */ }
    }
  }

  res.json({
    agent: "running",
    timestamp: new Date().toISOString(),
    db_connected: stats.db_connected,
    total_trades: stats.total_trades,
    recent_trades: trades,
  });
});

router.get("/decisions", (_req, res) => {
  const db = getDb();
  let decisions: unknown[] = [];
  let connected = false;

  if (db) {
    try {
      decisions = db
        .prepare(
          `SELECT symbol, decision, confidence, reasoning, market_data, decided_at
           FROM agent_decisions
           ORDER BY decided_at DESC LIMIT 60`,
        )
        .all();
      connected = true;
      db.close();
    } catch (e) {
      console.error("[trading] decisions query error:", e);
      try { db.close(); } catch { /* ignore */ }
    }
  }

  res.json({ db_connected: connected, decisions });
});

router.get("/partial-profits", (_req, res) => {
  const db = getDb();
  if (!db) {
    res.json({ partial_profits: {} });
    return;
  }
  try {
    const rows = db
      .prepare(
        `SELECT symbol, SUM(ABS(pnl)) as secured_pnl, COUNT(*) as count
         FROM trades
         WHERE close_reason = 'partial_profit' AND pnl IS NOT NULL
         GROUP BY symbol`,
      )
      .all() as { symbol: string; secured_pnl: number; count: number }[];
    const result: Record<string, { secured_pnl: number; count: number }> = {};
    rows.forEach(r => { result[r.symbol] = { secured_pnl: r.secured_pnl, count: r.count }; });
    db.close();
    res.json({ partial_profits: result });
  } catch (e) {
    console.error("[trading] partial-profits query error:", e);
    try { db.close(); } catch { /* ignore */ }
    res.json({ partial_profits: {} });
  }
});

const ALPACA_BASE = "https://paper-api.alpaca.markets";
const ALPACA_KEY  = process.env["ALPACA_API_KEY"]  ?? "";
const ALPACA_SEC  = process.env["ALPACA_SECRET_KEY"] ?? "";

router.get("/positions", async (_req, res) => {
  try {
    const r = await fetch(`${ALPACA_BASE}/v2/positions`, {
      headers: {
        "APCA-API-KEY-ID":     ALPACA_KEY,
        "APCA-API-SECRET-KEY": ALPACA_SEC,
      },
    });
    if (!r.ok) {
      res.json({ positions: [], error: `Alpaca HTTP ${r.status}` });
      return;
    }
    const raw = (await r.json()) as Record<string, string>[];
    const positions = raw.map(p => ({
      symbol:          p.symbol,
      side:            p.side,
      qty:             parseFloat(p.qty),
      entry_price:     parseFloat(p.avg_entry_price),
      current_price:   parseFloat(p.current_price),
      market_value:    parseFloat(p.market_value),
      unrealized_pl:   parseFloat(p.unrealized_pl),
      unrealized_plpc: parseFloat(p.unrealized_plpc) * 100,
      cost_basis:      parseFloat(p.cost_basis),
    }));
    res.json({ positions });
  } catch (err) {
    res.json({ positions: [], error: String(err) });
  }
});

const FLASK_BASE = "http://localhost:5000";

async function proxyFlask(url: string, fallback: unknown) {
  try {
    const r = await fetch(url);
    if (!r.ok) return fallback;
    return await r.json();
  } catch {
    return fallback;
  }
}

router.get("/movers", async (_req, res) => {
  res.json(await proxyFlask(`${FLASK_BASE}/api/movers`, { movers: [], error: "scanner unavailable" }));
});

router.get("/sentiment", async (_req, res) => {
  res.json(await proxyFlask(`${FLASK_BASE}/api/sentiment`, { sentiment: "neutral", score: 0, headlines: [], alerts: [] }));
});

router.get("/calendar", async (_req, res) => {
  res.json(await proxyFlask(`${FLASK_BASE}/api/calendar`, { event: null, note: "" }));
});

router.get("/stats", async (_req, res) => {
  res.json(await proxyFlask(`${FLASK_BASE}/api/stats`, {
    total_trades: 0, win_rate: 0, profit_factor: 0,
    total_pnl: 0, max_drawdown: 0, best_asset: null, asset_pnl: {}
  }));
});

router.get("/regime", async (_req, res) => {
  res.json(await proxyFlask(`${FLASK_BASE}/api/regime`, { regime: "UNKNOWN" }));
});

router.get("/stops", async (_req, res) => {
  res.json(await proxyFlask(`${FLASK_BASE}/api/stops`, { stops: {} }));
});

router.get("/account", async (_req, res) => {
  res.json(await proxyFlask(`${FLASK_BASE}/api/account`, {
    equity: 0, cash: 0, buying_power: 0, portfolio_value: 0, last_equity: 0
  }));
});

router.get("/closed-today", async (req, res) => {
  const period = (req.query.period as string) || "today";
  res.json(await proxyFlask(`${FLASK_BASE}/api/closed-today?period=${period}`, { closed: [], date: "" }));
});

router.get("/analysis", async (_req, res) => {
  res.json(await proxyFlask(`${FLASK_BASE}/api/analysis`, {}));
});

router.get("/source", async (_req, res) => {
  try {
    const r = await fetch(`${FLASK_BASE}/source`);
    const text = await r.text();
    res.setHeader("Content-Type", "text/plain; charset=utf-8");
    res.setHeader("Cache-Control", "no-cache");
    res.send(text);
  } catch (e) {
    res.status(503).type("text/plain").send("Source unavailable — trading agent not running.");
  }
});

export default router;
