import asyncio
import aiohttp
from dataclasses import dataclass


MIN_LP_SOL = 2.0
MAX_CREATOR_SUPPLY_PCT = 0.6


@dataclass
class CandidateMint:
    mint: str
    creator: str
    tx_sig: str


async def check_external_intel(mint):
    is_clustered = await get_bubblemap_data(mint)
    if is_clustered:
        print("Bubblemap Alert: Supply is heavily clustered!")
        return True

    creator_address = await get_token_creator(mint)
    is_blacklisted = await check_arkham_tags(creator_address)

    if is_blacklisted:
        print("Arkham Alert: Creator is a tagged scammer!")
        return True

    return False


async def is_metadata_locked(mint_address, helius_url):
    payload = {
        "jsonrpc": "2.0",
        "id": "lock-check",
        "method": "getAsset",
        "params": {"id": mint_address}
    }

    async with aiohttp.ClientSession() as session:
        async with session.post(helius_url, json=payload) as response:
            data = await response.json()
            return not data.get("result", {}).get("mutable", True)


async def check_liquidity_sol(mint_address, client):
    pool_vault_address = "Paste_Bonding_Curve_Address_Here"

    response = await client.get_balance(pool_vault_address)
    sol_balance = response.value / 10**9

    if sol_balance < 2.0:
        return False, f"Low Liquidity: {sol_balance} SOL"

    return True, f"Healthy Liquidity: {sol_balance} SOL"


async def check_metadata_authorities(mint_address, helius_url):
    payload = {
        "jsonrpc": "2.0",
        "id": "authority-check",
        "method": "getAsset",
        "params": {"id": mint_address}
    }

    async with aiohttp.ClientSession() as session:
        async with session.post(helius_url, json=payload) as response:
            data = await response.json()
            result = data.get("result", {})

            authorities = result.get("authorities", [])
            has_freeze = any(auth.get("scopes", []) for auth in authorities if "freeze" in str(auth).lower())
            mint_auth = result.get("mint_extensions", {}).get("mint_authority")

            if has_freeze or mint_auth:
                return False, "Active Authority Found"

            return True, "Authorities Renounced"


async def passes_rug_checks(c: CandidateMint) -> bool:
    mint_authority_exists = False
    lp_sol = 5.0

    if mint_authority_exists:
        return False
    if lp_sol < MIN_LP_SOL:
        return False

    creator_bal = await get_token_balance(c.mint, c.creator)
    total_supply = 1_000_000_000

    if total_supply == 0:
        return False

    if creator_bal / total_supply > MAX_CREATOR_SUPPLY_PCT:
        return False

    risk_score = await query_external_rug_api(c.mint)
    if risk_score > 80:
        return False

    return True


async def rug_check_worker(cand_queue: asyncio.Queue, buy_queue: asyncio.Queue):
    bouncer = asyncio.Semaphore(3)

    print("Rug checker ready...")
    while True:
        cand: CandidateMint = await cand_queue.get()
        async with bouncer:
            try:
                ok = await passes_rug_checks(cand)
                if ok:
                    print(f"{cand.mint} passed rug check. Sending to Executioner!")
                    await buy_queue.put(cand)
                else:
                    print(f"{cand.mint} failed rug check. Discarding.")
            finally:
                cand_queue.task_done()


async def metadata_watcher(watchlist: asyncio.Queue, buy_queue: asyncio.Queue, helius_url):
    print("Watcher is active and monitoring the pending list...")
    while True:
        cand = await watchlist.get()

        is_locked = await is_metadata_locked(cand.mint, helius_url)

        if is_locked:
            print(f"{cand.mint} just locked metadata! Moving to Buy Queue.")
            await buy_queue.put(cand)
        else:
            await asyncio.sleep(3)
            await watchlist.put(cand)

        watchlist.task_done()


async def post_entry_guard(mint: str, entry_lp_sol: float):
    print(f"Guarding position for {mint}...")
    while True:
        current_lp = 5.0  # PLACEHOLDER: await fetch_lp_info_for_mint(mint)
        if current_lp < entry_lp_sol * 0.4:
            print("LP dropping fast! Executing market sell!")
            break

        await asyncio.sleep(1.0)
