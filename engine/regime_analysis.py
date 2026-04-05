import pandas_ta as ta
import pandas as pd


def analyze_regime(df: pd.DataFrame) -> str:
    df['RSI'] = ta.rsi(df['close'], length=14)
    df['EMA_20'] = ta.ema(df['close'], length=20)

    current_price = df['close'].iloc[-1]
    current_rsi = df['RSI'].iloc[-1]

    if current_price > df['EMA_20'].iloc[-1] and current_rsi > 60:
        return "BREAKOUT"
    elif current_rsi < 30:
        return "OVERSOLD"
    else:
        return "STABLE"


async def autonomous_executioner(mint, target_rr, regime_data, amount_sol, support_level=None):
    regime = analyze_regime(regime_data)

    if regime == "Breakout":
        await execute_market_buy(mint, amount_sol)
    elif regime == "Ranging":
        await place_limit_order(mint, price=support_level)

    await setup_exit_points(mint, target_rr)
