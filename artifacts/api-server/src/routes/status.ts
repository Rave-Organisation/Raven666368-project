import { Router, type IRouter } from "express";

const router: IRouter = Router();

const SOL_MINT = "So11111111111111111111111111111111111111112";
const USDC_MINT = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v";

const RPC_URL =
  process.env.HELIUS_RPC_URL ||
  process.env.RPC_URL ||
  "https://api.mainnet-beta.solana.com";
const WALLET = process.env.WALLET_PUBKEY || process.env.WALLET_PRIVATE_KEY || "";
const DRY_RUN = (process.env.DRY_RUN ?? "true").toLowerCase() === "true";

async function fetchSolBalance(pubkey: string): Promise<number> {
  try {
    const resp = await fetch(RPC_URL, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        jsonrpc: "2.0",
        id: 1,
        method: "getBalance",
        params: [pubkey, { commitment: "confirmed" }],
      }),
      signal: AbortSignal.timeout(5000),
    });
    const data = (await resp.json()) as { result?: { value?: number } };
    const lamports = data?.result?.value ?? 0;
    return lamports / 1e9;
  } catch {
    return 0;
  }
}

async function fetchSolPrice(): Promise<number> {
  try {
    const resp = await fetch(
      `https://lite.dataseed.io/price?ids=${SOL_MINT}`,
      { signal: AbortSignal.timeout(4000) }
    );
    if (resp.ok) {
      const data = (await resp.json()) as { data?: Record<string, { price?: number }> };
      const p = data?.data?.[SOL_MINT]?.price;
      if (p) return p;
    }
  } catch { /* fall through */ }
  try {
    const resp = await fetch(
      "https://api.coingecko.com/api/v3/simple/price?ids=solana&vs_currencies=usd",
      { signal: AbortSignal.timeout(5000) }
    );
    const data = (await resp.json()) as { solana?: { usd?: number } };
    return data?.solana?.usd ?? 0;
  } catch {
    return 0;
  }
}

async function fetchEngineStatus(): Promise<{
  engine_active: boolean;
  mode: string;
  open_positions: number;
  trades_today: number;
  daily_pnl: number;
  drawdown_pct: number;
  capital_sol: number;
}> {
  const base = process.env.ENGINE_URL || "http://localhost:8080";
  try {
    const resp = await fetch(`${base}/`, {
      signal: AbortSignal.timeout(3000),
    });
    if (!resp.ok) throw new Error(`Engine HTTP ${resp.status}`);
    const data = (await resp.json()) as Record<string, unknown>;
    return {
      engine_active:  data.status !== "halted",
      mode:           DRY_RUN ? "DRY_RUN" : "LIVE",
      open_positions: (data.open_positions as number) ?? 0,
      trades_today:   (data.trades_today as number) ?? 0,
      daily_pnl:      (data.daily_pnl as number) ?? 0,
      drawdown_pct:   (data.drawdown_pct as number) ?? 0,
      capital_sol:    (data.capital_sol as number) ?? 0,
    };
  } catch {
    return {
      engine_active:  false,
      mode:           DRY_RUN ? "DRY_RUN" : "LIVE",
      open_positions: 0,
      trades_today:   0,
      daily_pnl:      0,
      drawdown_pct:   0,
      capital_sol:    0,
    };
  }
}

router.get("/status", async (_req, res) => {
  try {
    const [balance, sol_price, engine] = await Promise.all([
      WALLET ? fetchSolBalance(WALLET) : Promise.resolve(0),
      fetchSolPrice(),
      fetchEngineStatus(),
    ]);

    res.json({
      balance:        parseFloat(balance.toFixed(6)),
      sol_price:      parseFloat(sol_price.toFixed(4)),
      balance_usd:    parseFloat((balance * sol_price).toFixed(2)),
      engine_active:  engine.engine_active,
      mode:           engine.mode,
      open_positions: engine.open_positions,
      trades_today:   engine.trades_today,
      daily_pnl:      engine.daily_pnl,
      drawdown_pct:   engine.drawdown_pct,
      capital_sol:    engine.capital_sol,
      timestamp:      new Date().toISOString(),
    });
  } catch (err) {
    res.status(500).json({ error: String(err) });
  }
});

export default router;
