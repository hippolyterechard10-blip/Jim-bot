import express, { type Express } from "express";
import cors from "cors";
import pinoHttp from "pino-http";
import router from "./routes";
import { logger } from "./lib/logger";

const app: Express = express();

app.use(
  pinoHttp({
    logger,
    serializers: {
      req(req) {
        return {
          id: req.id,
          method: req.method,
          url: req.url?.split("?")[0],
        };
      },
      res(res) {
        return {
          statusCode: res.statusCode,
        };
      },
    },
  }),
);
app.use(cors());
app.use(express.json());
app.use(express.urlencoded({ extended: true }));

app.use("/api", router);

app.get("/", (_req, res) => {
  res.send(`<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8"/>
  <meta http-equiv="refresh" content="30"/>
  <title>Trading Agent Dashboard</title>
  <style>
    body{font-family:system-ui,sans-serif;background:#0f172a;color:#e2e8f0;margin:0;padding:2rem;}
    h1{color:#38bdf8;margin-bottom:0.25rem;}
    .sub{color:#64748b;margin-bottom:2rem;font-size:0.9rem;}
    .cards{display:flex;gap:1rem;flex-wrap:wrap;margin-bottom:2rem;}
    .card{background:#1e293b;border-radius:12px;padding:1.25rem 1.75rem;min-width:160px;}
    .card .label{font-size:0.75rem;color:#64748b;text-transform:uppercase;letter-spacing:.05em;}
    .card .value{font-size:1.5rem;font-weight:700;margin-top:0.25rem;}
    .green{color:#22c55e;} .yellow{color:#eab308;} .red{color:#ef4444;} .blue{color:#38bdf8;}
    table{width:100%;border-collapse:collapse;background:#1e293b;border-radius:12px;overflow:hidden;}
    th{background:#0f172a;padding:.75rem 1rem;text-align:left;font-size:.75rem;color:#64748b;text-transform:uppercase;}
    td{padding:.75rem 1rem;border-top:1px solid #334155;font-size:.9rem;}
    .badge{display:inline-block;padding:.15rem .5rem;border-radius:4px;font-size:.75rem;font-weight:600;}
    .badge-buy{background:#14532d;color:#22c55e;} .badge-sell{background:#450a0a;color:#ef4444;} .badge-hold{background:#1e3a5f;color:#38bdf8;}
    footer{margin-top:2rem;font-size:.75rem;color:#334155;}
  </style>
</head>
<body>
  <h1>⚡ Trading Agent</h1>
  <p class="sub">Live paper trading • Auto-refreshes every 30s</p>
  <div id="cards" class="cards">
    <div class="card"><div class="label">Status</div><div class="value green" id="status">Loading…</div></div>
    <div class="card"><div class="label">Total Trades</div><div class="value blue" id="trades">—</div></div>
    <div class="card"><div class="label">Last Update</div><div class="value" style="font-size:1rem" id="ts">—</div></div>
  </div>
  <h2 style="color:#94a3b8;font-size:.9rem;margin-bottom:.5rem;">RECENT TRADES</h2>
  <table>
    <thead><tr><th>Time</th><th>Symbol</th><th>Action</th><th>Price</th><th>Qty</th><th>P&amp;L</th></tr></thead>
    <tbody id="tbody"><tr><td colspan="6" style="color:#64748b">Loading…</td></tr></tbody>
  </table>
  <footer>Trading Agent • Paper Trading Only • Not Financial Advice</footer>
  <script>
    fetch('/api/status').then(r=>r.json()).then(d=>{
      document.getElementById('status').textContent = d.agent === 'running' ? '🟢 Running' : '🔴 Stopped';
      document.getElementById('trades').textContent = d.total_trades ?? '—';
      document.getElementById('ts').textContent = new Date(d.timestamp).toLocaleTimeString();
      const rows = (d.recent_trades||[]).map(t=>{
        const badgeClass = t.action==='BUY'?'badge-buy':t.action==='SELL'?'badge-sell':'badge-hold';
        const pnl = t.pnl!=null?(t.pnl>=0?'<span class="green">+'+t.pnl.toFixed(2)+'</span>':'<span class="red">'+t.pnl.toFixed(2)+'</span>'):'—';
        const ts = t.timestamp ? new Date(t.timestamp).toLocaleString() : '—';
        return '<tr><td>'+ts+'</td><td><strong>'+t.symbol+'</strong></td><td><span class="badge '+badgeClass+'">'+t.action+'</span></td><td>$'+Number(t.price).toFixed(2)+'</td><td>'+t.qty+'</td><td>'+pnl+'</td></tr>';
      });
      document.getElementById('tbody').innerHTML = rows.length ? rows.join('') : '<tr><td colspan="6" style="color:#64748b">No trades yet</td></tr>';
    }).catch(()=>{
      document.getElementById('status').textContent = '🟡 Connecting…';
    });
  </script>
</body>
</html>`);
});

export default app;
