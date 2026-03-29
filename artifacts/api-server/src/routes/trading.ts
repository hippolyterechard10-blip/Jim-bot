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
           FROM trades ORDER BY entry_at DESC LIMIT 20`,
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
          `SELECT symbol, decision, confidence, reasoning, decided_at
           FROM agent_decisions
           ORDER BY decided_at DESC LIMIT 40`,
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

const FLASK_BASE = "http://localhost:5000";

router.get("/movers", async (_req, res) => {
  try {
    const r = await fetch(`${FLASK_BASE}/api/movers`);
    const data = await r.json();
    res.json(data);
  } catch {
    res.json({ movers: [], error: "scanner unavailable" });
  }
});

router.get("/sentiment", async (_req, res) => {
  try {
    const r = await fetch(`${FLASK_BASE}/api/sentiment`);
    const data = await r.json();
    res.json(data);
  } catch {
    res.json({ sentiment: "neutral", score: 0, headlines: [], alerts: [] });
  }
});

router.get("/calendar", async (_req, res) => {
  try {
    const r = await fetch(`${FLASK_BASE}/api/calendar`);
    const data = await r.json();
    res.json(data);
  } catch {
    res.json({ event: null, note: "" });
  }
});

export default router;
