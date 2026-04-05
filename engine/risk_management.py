import asyncio


async def trailing_stop_guard(mint, entry_price, trail_percent=0.05):
    highest_price = entry_price

    while True:
        current_price = await fetch_latest_price(mint)

        if current_price > highest_price:
            highest_price = current_price
            print(f"New Peak! Trailing stop is now {highest_price * (1 - trail_percent)}")

        if current_price <= highest_price * (1 - trail_percent):
            print("EMERGENCY EXIT: 5% drop from peak. Selling now.")
            await execute_market_sell(mint)
            break

        await asyncio.sleep(0.5)


async def manage_risk_free_moonshot(mint, entry_price, total_tokens):
    tokens_held = total_tokens
    highest_price = entry_price
    current_trail = 0.05
    is_de_risked = False

    print(f"Monitoring {mint} | Entry: {entry_price} | Goal: 5x")

    while tokens_held > 0:
        current_price = await fetch_latest_price(mint)

        if current_price > highest_price:
            highest_price = current_price

        if not is_de_risked and current_price >= entry_price * 5:
            print("5x Hit! Selling 70% to secure the win.")

            tokens_to_sell = total_tokens * 0.70
            await execute_market_sell(mint, tokens_to_sell)

            tokens_held -= tokens_to_sell
            is_de_risked = True

            current_trail = 0.15
            print("Moonbag active. Stop set to Entry or 15% Trail.")

        trail_stop = highest_price * (1 - current_trail)
        break_even_floor = entry_price if is_de_risked else 0

        exit_trigger = max(trail_stop, break_even_floor)

        if current_price <= exit_trigger:
            print(f"Exit Triggered at {current_price}. Selling remainder.")
            await execute_market_sell(mint, tokens_held)
            break

        await asyncio.sleep(0.5)


async def manage_trade_lifecycle(mint, entry_price, total_tokens):
    highest_price = entry_price
    current_trail = 0.05
    tokens_held = total_tokens
    take_profit_done = False

    print(f"Trade started for {mint}. Entry: {entry_price}")

    while tokens_held > 0:
        current_price = await fetch_price(mint)

        if current_price > highest_price:
            highest_price = current_price

        if not take_profit_done and current_price >= entry_price * 5:
            print("Target x5 Hit! Selling 70% to lock in daily profit.")

            amount_to_sell = total_tokens * 0.70
            await execute_market_sell(mint, amount_to_sell)

            tokens_held -= amount_to_sell
            take_profit_done = True

            current_trail = 0.15
            print("Profit secured. Moving stop to Entry + 15% Trail.")

        stop_price = highest_price * (1 - current_trail)
        hard_floor = entry_price * 1.01 if take_profit_done else 0

        if current_price <= max(stop_price, hard_floor):
            reason = "Trail Hit" if current_price <= stop_price else "Break-even Hit"
            print(f"{reason}: Selling remaining {tokens_held} tokens.")
            await execute_market_sell(mint, tokens_held)
            break

        await asyncio.sleep(0.5)


async def exit_manager(mint, entry_price, total_tokens):
    tokens_remaining = total_tokens
    has_taken_initial_profit = False

    while tokens_remaining > 0:
        current_price = await fetch_price(mint)

        if not has_taken_initial_profit and current_price >= entry_price * 5:
            print("Target x5 Hit! Selling 70% to lock in daily profit.")
            amount_to_sell = total_tokens * 0.70
            await execute_market_sell(mint, amount_to_sell)

            tokens_remaining -= amount_to_sell
            has_taken_initial_profit = True

        await asyncio.sleep(0.5)


async def emergency_exit_guard(mint, entry_price, client):
    print(f"High-Alert Guard engaged for {mint}. Exit set at -10%.")
    while True:
        current_price = await fetch_price(mint, client)

        if current_price <= entry_price * 0.90:
            print(f"CRITICAL DROP: Selling {mint} immediately.")
            await execute_market_sell(mint)
            break

        await asyncio.sleep(0.5)


async def high_alert_monitor(mint, entry_price):
    while True:
        current_price = await get_current_price(mint)
        if current_price <= entry_price * 0.90:
            print(f"High Alert: {mint} dropped 10%. Panic selling!")
            await execute_sell(mint)
            break
        await asyncio.sleep(0.5)


async def execute_twap_buy(mint, total_sol, chunks=5, interval=2):
    sol_per_chunk = total_sol / chunks
    for i in range(chunks):
        print(f"Executing TWAP chunk {i+1}/{chunks}...")
        await execute_market_buy(mint, sol_per_chunk)
        if i < chunks - 1:
            await asyncio.sleep(interval)


async def manage_ladder_sales(mint, entry_price, total_tokens):
    targets = {5: 0.20, 10: 0.25, 20: 0.30, 40: 1.0}
    tokens_held = total_tokens

    for multiplier, sell_weight in targets.items():
        target_price = entry_price * multiplier
        # Monitor price and sell at each target
        # amount_to_sell = tokens_held * sell_weight
        # await execute_market_sell(mint, amount_to_sell)
        # tokens_held -= amount_to_sell
        pass


async def manage_exit_ladder(mint, entry_price):
    targets = [5, 10, 20, 30, 40]
    remaining_position = 100

    for multiplier in targets:
        target_price = entry_price * multiplier
        # If price >= target_price:
        #    Sell 20% of position
        #    Update Trailing Stop-Loss to lock in gains
        pass
