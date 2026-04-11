"""
Arkham OSINT — Integration Test
=================================
Run this first to verify your API key works and the caching layer
is functioning correctly. Uses only 3-5 API calls total.

Usage
-----
    python -m engine.execution.test_arkham
"""

import asyncio
import os
import sys

from engine.execution.arkham_osint import ArkhamOSINT


# ── Well-known Solana addresses used as test data ────────────────────────────
# These are public, widely-known addresses — safe for any read-only test
TEST_WHALES = [
    ("9WzDXwBbmkg8ZTbNMqUxvQRAyrZzDsGYdLVL9zYtAWWM", "Binance Hot Wallet"),
    ("5tzFkiKscXHK5ZXCGbOfB78oPkJi2KNieotzYkDB6KkV", "FTX Estate"),
    ("CuieVDEDtLo7FypA9SbLM9saXFdb1dsshEkyErMqkRQq", "Jump Trading"),
]

# Raydium (RAY) — always has inbound transfers, safe for testing
TEST_TOKEN_MINT = "4k3Dyjzvzp8eMZWUXbBCjEvwSkkk59S5iCNLY3QrkX6R"


async def main():
    print("\n" + "=" * 55)
    print("  Arkham OSINT — Integration Test")
    print("=" * 55)

    # ── Init ──────────────────────────────────────────────────────────────────
    try:
        arkham = ArkhamOSINT.from_env()
        print("OK  Client initialised")
    except RuntimeError as e:
        print(f"FAIL  {e}")
        print("    -> Set ARKHAM_API_KEY in Secrets / environment")
        return

    # ── Load test watchlist ───────────────────────────────────────────────────
    print("\n[Stream A] Loading whale watchlist...")
    for addr, label in TEST_WHALES:
        arkham.add_tracked_whale(addr, label=label)
    print(f"  Added {len(TEST_WHALES)} test whale addresses")

    # ── Test transfer fetch (1 API call) ─────────────────────────────────────
    print(f"\n[Stream A+B] Fetching transfers for RAY token...")
    transfers = await arkham._get_transfers(TEST_TOKEN_MINT)
    print(f"  Found {len(transfers)} recent inbound transfers")

    # ── Test cache (0 API calls — should be served from cache) ───────────────
    print("\n[Cache] Re-fetching same token (should use cache, 0 additional API calls)...")
    calls_before = arkham._calls
    await arkham._get_transfers(TEST_TOKEN_MINT)
    if arkham._calls == calls_before:
        print("  OK  Cache working — 0 additional API calls")
    else:
        print("  WARN  Cache miss — check SQLite setup")

    # ── Test wallet labelling (1-3 API calls) ────────────────────────────────
    print("\n[Stream B] Labelling first 3 unique buyers...")
    buyers = arkham._extract_unique_buyers(transfers)[:3]
    if buyers:
        intels = await arkham._batch_label_wallets(buyers, set())
        for w in intels:
            tag = f"({w.entity_name})" if w.entity_name else "(unknown)"
            sm  = "SMART MONEY" if w.is_smart_money else "unknown"
            print(f"  {w.address[:8]}... {tag} -> {sm}")
    else:
        print("  No buyers found in transfer data")

    # ── Full enrichment test (uses cache where possible) ─────────────────────
    print(f"\n[Stream C] Full enrichment pipeline...")
    enrichment = await arkham.enrich_token(TEST_TOKEN_MINT, pre_score=65.0)
    print(f"  Total boost:   +{enrichment.total_boost:.1f}")
    print(f"  Signals found: {len(enrichment.signals)}")
    print(f"  API calls:     {enrichment.api_calls_made}")
    print(f"  From cache:    {enrichment.served_from_cache}")
    for s in enrichment.signals:
        print(f"    -> {s}")

    # ── Budget report ──────────────────────────────────────────────────────────
    print("\n[Budget] Session usage:")
    report = arkham.budget_report()
    for k, v in report.items():
        print(f"  {k:<28} {v}")

    print("\n" + "=" * 55)
    print("  Test complete. Arkham layer is ready.")
    print("=" * 55 + "\n")


if __name__ == "__main__":
    asyncio.run(main())
