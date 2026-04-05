import asyncio
import time


async def elite_autonomous_trader(mint, total_sol, trail_percent=0.05):
    intel = await check_triad_intel(mint)

    if intel['is_scam'] or intel['cluster_size'] > 0.15:
        print(f"Intel Alert: High Risk Cluster ({intel['cluster_size']*100}%). Aborting.")
        return "FILTERED"

    chunks = 6
    sol_per_chunk = total_sol / chunks
    tokens_bought = 0
    start_price = await fetch_price(mint)

    print(f"Starting Stealth Entry. Monitoring Volume/FDV ratio...")

    for i in range(chunks):
        stats = await fetch_market_stats(mint)
        volume_to_fdv_ratio = stats['volume_1m'] / stats['fdv']

        if volume_to_fdv_ratio < 0.02:
            print("Volume/FDV Ratio too low. Pausing entry to prevent 'Slow Rug'.")
            await asyncio.sleep(5)
            continue

        success, amount = await execute_market_buy(mint, sol_per_chunk)
        if success:
            tokens_bought += amount
            print(f"Chunk {i+1} complete. Position: {tokens_bought} tokens.")

        if not await check_price_survival(mint, start_price, trail_percent):
            print("EMERGENCY EXIT: 5% drop during entry. Selling everything.")
            await execute_market_sell(mint, tokens_bought)
            return "EXIT_LOSS_PREVENTED"

        await asyncio.sleep(1.2)

    await manage_trade_lifecycle(mint, start_price, tokens_bought)
    return "SUCCESS"


async def triad_intelligence_entry(mint, total_sol, trail_percent=0.05):
    creator_address = await get_token_creator(mint)

    if await check_arkham_blacklist(creator_address):
        print("Arkham Alert: Known scammer detected. Aborting.")
        return "REJECTED_ARKHAM"

    if await check_bubblemap_clusters(mint):
        print("Bubblemap Alert: Dangerous clusters found. Aborting.")
        return "REJECTED_CLUSTERS"

    chunks = 5
    tokens_bought = 0
    entry_price = await fetch_latest_price(mint)

    for i in range(chunks):
        if not await validate_volume_quality(mint):
            print("Volume Warning: Activity looks suspicious or artificial. Pausing.")
            await asyncio.sleep(3)
            continue

        success, amount = await execute_market_buy(mint, total_sol / chunks)
        if success:
            tokens_bought += amount

        if not await check_price_survival(mint, entry_price, trail_percent):
            print("Emergency Exit: 5% drop hit during TWAP.")
            await execute_market_sell(mint, tokens_bought)
            return "KILLED_BY_PRICE"

        await asyncio.sleep(1.5)

    return "SUCCESS"


async def twap_with_survival_guard(mint, total_sol, trail_percent=0.05):
    abort_event = asyncio.Event()

    guard_task = asyncio.create_task(monitor_price(mint, trail_percent, abort_event))

    chunks = 5
    for i in range(chunks):
        if abort_event.is_set():
            print("TWAP Aborted: Survival Guard triggered an exit.")
            break

        await execute_market_buy(mint, total_sol / chunks)
        print(f"Chunk {i+1}/{chunks} complete.")

        await asyncio.sleep(2)


async def master_autonomous_entry(mint, total_sol, trail_percent=0.05):
    chunks = 5
    sol_per_chunk = total_sol / chunks
    tokens_bought = 0

    print(f"Analyzing {mint} with the Triad Mind (Nodes, Bubblemap, Arkham)...")

    for i in range(chunks):
        if not await check_node_latency():
            print("Node lag detected. Pausing entry.")
            await asyncio.sleep(1)
            continue

        is_scam_detected = await check_external_intel(mint)

        if is_scam_detected:
            print("SCAM DETECTED by Arkham/Bubblemap! Aborting and selling.")
            await execute_market_sell(mint, tokens_bought)
            return "KILLED_BY_INTEL"

        success, amount = await execute_market_buy(mint, sol_per_chunk)
        if success:
            tokens_bought += amount

        if not await check_price_survival(mint, trail_percent):
            print("Price floor breached. Emergency Exit.")
            await execute_market_sell(mint, tokens_bought)
            return "KILLED_BY_PRICE"

        await asyncio.sleep(1.5)

    return "SUCCESS"


async def guarded_stealth_entry(mint, total_sol, trail_percent=0.05):
    chunks = 5
    sol_per_chunk = total_sol / chunks
    bought_amount = 0

    print(f"Starting Guarded Entry for {mint}...")

    for i in range(chunks):
        success, amount_received = await execute_market_buy(mint, sol_per_chunk)

        if success:
            bought_amount += amount_received
            print(f"Chunk {i+1} bought. Total: {bought_amount} tokens.")

            is_safe = await check_survival_threshold(mint, trail_percent)

            if not is_safe:
                print("THRESHOLD BREACHED during entry! Aborting and exiting.")
                await execute_market_sell(mint, bought_amount)
                return "ABORTED"

        await asyncio.sleep(2)

    print("Entry Complete. Full position secured.")
    return "SUCCESS"


async def stealth_entry_with_guard(mint, total_sol, trail_percent=0.05):
    chunks = 5
    sol_per_chunk = total_sol / chunks

    print(f"Starting TWAP entry for {mint}...")
    first_buy_success = await execute_market_buy(mint, sol_per_chunk)

    if not first_buy_success:
        return print("Initial buy failed. Aborting.")

    entry_price = await fetch_latest_price(mint)

    guard_task = asyncio.create_task(trailing_stop_guard(mint, entry_price, trail_percent))

    for i in range(1, chunks):
        await asyncio.sleep(2)
        await execute_market_buy(mint, sol_per_chunk)
        print(f"TWAP Chunk {i+1}/{chunks} complete.")

    await guard_task


async def execute_strategic_buy(mint, amount, regime):
    if regime == "BREAKOUT":
        await execute_market_buy(mint, amount)
    elif regime == "STABLE":
        chunks = 5
        for i in range(chunks):
            await execute_market_buy(mint, amount / chunks)
            await asyncio.sleep(2)
