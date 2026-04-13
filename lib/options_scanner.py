"""TARS Options Day Trade Scanner — finds 0DTE/weekly options plays."""
from __future__ import annotations

import logging
import math
import time
from datetime import datetime, timezone, timedelta
from typing import Optional

import numpy as np
import pandas as pd
import requests
import yfinance as yf

logger = logging.getLogger("tars.options_scanner")

# Day trade options watchlist — high liquidity options chains only
OPTIONS_WATCHLIST = [
    "SPY", "QQQ", "IWM",           # Index ETFs (tightest spreads, most liquid)
    "AAPL", "MSFT", "NVDA", "TSLA", "META", "AMZN", "GOOGL",  # Mega-cap
    "AMD", "COIN", "PLTR", "SOFI",  # High IV / momentum
    "XLE", "XLF", "XLK",           # Sector ETFs
    "IBIT", "MSTR",                # Crypto proxy
]

COLOR_CALL = 0x2ECC71   # Green
COLOR_PUT = 0xE74C3C    # Red
COLOR_HEADER = 0x9B59B6  # Purple


def get_intraday_momentum(ticker: str) -> Optional[dict]:
    """Analyze intraday momentum using 5-min and 15-min data."""
    try:
        stock = yf.Ticker(ticker)

        # 5-min data for last 2 days (intraday momentum)
        df_5m = stock.history(period="2d", interval="5m")
        # Daily data for context
        df_daily = stock.history(period="1mo", interval="1d")

        if df_5m is None or len(df_5m) < 20 or df_daily is None or len(df_daily) < 10:
            return None

        close_5m = df_5m["Close"]
        volume_5m = df_5m["Volume"]
        close_daily = df_daily["Close"]
        high_daily = df_daily["High"]
        low_daily = df_daily["Low"]

        current = close_5m.iloc[-1]
        prev_close = close_daily.iloc[-2] if len(close_daily) > 1 else close_daily.iloc[-1]
        today_open = close_daily.iloc[-1] if len(df_daily) > 0 else current

        # Intraday change
        intraday_chg = ((current - prev_close) / prev_close) * 100

        # VWAP approximation
        typical_price = (df_5m["High"] + df_5m["Low"] + df_5m["Close"]) / 3
        cumvol = volume_5m.cumsum()
        vwap = (typical_price * volume_5m).cumsum() / cumvol.replace(0, np.nan)
        vwap_now = vwap.iloc[-1]
        above_vwap = current > vwap_now

        # RSI on 5-min
        delta = close_5m.diff()
        gain = delta.where(delta > 0, 0.0)
        loss = -delta.where(delta < 0, 0.0)
        avg_gain = gain.rolling(14, min_periods=14).mean()
        avg_loss = loss.rolling(14, min_periods=14).mean()
        rs = avg_gain / avg_loss.replace(0, np.nan)
        rsi_5m = (100 - (100 / (1 + rs))).iloc[-1]

        # EMA 9 and 21 on 5-min
        ema9 = close_5m.ewm(span=9, adjust=False).mean().iloc[-1]
        ema21 = close_5m.ewm(span=21, adjust=False).mean().iloc[-1]

        # Volume surge (last 3 bars vs avg)
        recent_vol = volume_5m.iloc[-3:].mean()
        avg_vol = volume_5m.iloc[-40:-3].mean() if len(volume_5m) > 43 else volume_5m.mean()
        vol_surge = recent_vol / avg_vol if avg_vol > 0 else 1

        # Daily ATR for strike selection
        tr = pd.concat([
            high_daily - low_daily,
            (high_daily - close_daily.shift()).abs(),
            (low_daily - close_daily.shift()).abs(),
        ], axis=1).max(axis=1)
        atr_daily = tr.rolling(14).mean().iloc[-1]
        atr_pct = (atr_daily / current) * 100

        # Support/resistance from daily
        day_high = high_daily.iloc[-1]
        day_low = low_daily.iloc[-1]
        prev_high = high_daily.iloc[-2]
        prev_low = low_daily.iloc[-2]

        # Implied Volatility from options chain
        iv = None
        try:
            expirations = stock.options
            if expirations:
                nearest_exp = expirations[0]
                chain = stock.option_chain(nearest_exp)
                atm_calls = chain.calls[
                    (chain.calls["strike"] - current).abs() < current * 0.02
                ]
                if len(atm_calls) > 0:
                    iv = atm_calls["impliedVolatility"].mean() * 100
        except Exception:
            pass

        return {
            "ticker": ticker,
            "current": current,
            "prev_close": prev_close,
            "intraday_chg": intraday_chg,
            "vwap": vwap_now,
            "above_vwap": above_vwap,
            "rsi_5m": rsi_5m,
            "ema9": ema9,
            "ema21": ema21,
            "vol_surge": vol_surge,
            "atr_daily": atr_daily,
            "atr_pct": atr_pct,
            "day_high": day_high,
            "day_low": day_low,
            "prev_high": prev_high,
            "prev_low": prev_low,
            "iv": iv,
        }
    except Exception as e:
        logger.warning("Failed intraday analysis for %s: %s", ticker, e)
        return None


def find_option_play(data: dict) -> Optional[dict]:
    """Determine if there's a day trade options play and recommend strikes."""
    ticker = data["ticker"]
    current = data["current"]
    atr = data["atr_daily"]
    rsi = data["rsi_5m"]
    above_vwap = data["above_vwap"]
    vol_surge = data["vol_surge"]
    ema9 = data["ema9"]
    ema21 = data["ema21"]
    intraday_chg = data["intraday_chg"]

    signals = []
    score = 0

    # --- Bullish setups (CALL) ---

    # 1. VWAP reclaim + EMA alignment
    if above_vwap and ema9 > ema21:
        signals.append("Above VWAP + EMA9 > EMA21")
        score += 25

    # 2. RSI momentum (not overbought)
    if 45 < rsi < 65 and above_vwap:
        signals.append("RSI momentum zone")
        score += 15

    # 3. Oversold bounce
    if rsi < 30:
        signals.append("RSI oversold bounce")
        score += 20

    # 4. Volume confirmation
    if vol_surge > 1.8 and score > 0:
        signals.append(f"Volume surge ({vol_surge:.1f}x)")
        score += 15

    # 5. Gap up holding
    if intraday_chg > 1.0 and above_vwap:
        signals.append(f"Gap up holding (+{intraday_chg:.1f}%)")
        score += 20

    # 6. Breaking previous day high
    if current > data["prev_high"]:
        signals.append("Breaking prev day high")
        score += 20

    # --- Bearish setups (PUT) ---

    # 7. Below VWAP + EMA death cross
    if not above_vwap and ema9 < ema21:
        signals.append("Below VWAP + EMA9 < EMA21")
        score -= 25

    # 8. Overbought rejection
    if rsi > 75:
        signals.append("RSI overbought rejection")
        score -= 20

    # 9. Volume dump
    if vol_surge > 1.8 and score < 0:
        signals.append(f"Volume dump ({vol_surge:.1f}x)")
        score -= 15

    # 10. Gap down failing
    if intraday_chg < -1.0 and not above_vwap:
        signals.append(f"Gap down failing ({intraday_chg:.1f}%)")
        score -= 20

    # 11. Losing previous day low
    if current < data["prev_low"]:
        signals.append("Breaking prev day low")
        score -= 20

    # Need minimum conviction
    if abs(score) < 30 or not signals:
        return None

    # --- Build the options play ---
    direction = "CALL" if score > 0 else "PUT"
    strength = abs(score)

    # Strike selection: slightly OTM for leverage, not too far for theta
    if direction == "CALL":
        # Strike 1-2% OTM
        raw_strike = current * 1.01
        stop_price = data["vwap"] - (atr * 0.2)  # Stop if loses VWAP
        target_price = current + (atr * 0.5)       # Half ATR move
        target_2 = current + (atr * 0.75)
    else:
        raw_strike = current * 0.99
        stop_price = data["vwap"] + (atr * 0.2)
        target_price = current - (atr * 0.5)
        target_2 = current - (atr * 0.75)

    # Round strike to nearest $0.50 or $1 depending on price
    if current < 50:
        strike = round(raw_strike * 2) / 2  # $0.50 increments
    elif current < 200:
        strike = round(raw_strike)           # $1 increments
    else:
        strike = round(raw_strike / 5) * 5   # $5 increments

    # Estimated premium (rough Black-Scholes-ish)
    iv = data.get("iv") or (atr / current * 100 * 16)  # annualized from ATR
    # 0DTE premium is mostly intrinsic + small time value
    otm_amount = abs(strike - current)
    estimated_premium = max(0.10, atr * 0.15 - otm_amount * 0.5)

    # P&L estimates
    move_to_target = abs(target_price - current)
    estimated_gain_pct = (move_to_target / estimated_premium) * 100 if estimated_premium > 0 else 0
    estimated_gain_pct = min(estimated_gain_pct, 300)  # Cap at 300%

    # Timing
    now_hour = datetime.now().hour
    if now_hour < 10:
        timing = "Enter on first 15min candle confirmation"
        exit_by = "11:30 AM ET or at target"
        hold = "30 min - 2 hours"
    elif now_hour < 13:
        timing = "Enter on pullback to VWAP or EMA9"
        exit_by = "2:00 PM ET or at target"
        hold = "30 min - 2 hours"
    else:
        timing = "Power hour play — enter on momentum break"
        exit_by = "3:45 PM ET (before close)"
        hold = "15 min - 1 hour"

    return {
        "ticker": ticker,
        "direction": direction,
        "strike": strike,
        "strength": strength,
        "signals": signals,
        "current_price": round(current, 2),
        "stop_price": round(stop_price, 2),
        "target_1": round(target_price, 2),
        "target_2": round(target_2, 2),
        "estimated_premium": round(estimated_premium, 2),
        "estimated_gain_pct": round(estimated_gain_pct, 0),
        "vwap": round(data["vwap"], 2),
        "rsi_5m": round(rsi, 1),
        "vol_surge": round(vol_surge, 1),
        "atr_pct": round(data["atr_pct"], 2),
        "iv": round(iv, 1) if iv else None,
        "intraday_chg": round(intraday_chg, 2),
        "timing": timing,
        "exit_by": exit_by,
        "hold": hold,
    }


def scan_options() -> list[dict]:
    """Scan watchlist for day trade options plays."""
    results = []
    for ticker in OPTIONS_WATCHLIST:
        data = get_intraday_momentum(ticker)
        if data:
            play = find_option_play(data)
            if play:
                results.append(play)
        time.sleep(0.3)

    results.sort(key=lambda x: x["strength"], reverse=True)
    return results


def format_options_embed(play: dict) -> dict:
    """Format an options play as Discord embed."""
    d = play["direction"]
    color = COLOR_CALL if d == "CALL" else COLOR_PUT
    emoji = "🟢" if d == "CALL" else "🔴"
    exp_label = "0DTE / Weekly"

    signals_text = "\n".join(f"• {s}" for s in play["signals"])

    premium_str = f"~${play['estimated_premium']:.2f}" if play["estimated_premium"] > 0 else "Check chain"
    iv_str = f"{play['iv']:.0f}%" if play["iv"] else "N/A"

    return {
        "embeds": [{
            "title": f"{emoji} {play['ticker']} ${play['strike']:.0f} {d} — Day Trade",
            "color": color,
            "fields": [
                {"name": "Type", "value": f"{d} ({exp_label})", "inline": True},
                {"name": "Strike", "value": f"${play['strike']:.0f}", "inline": True},
                {"name": "Stock Price", "value": f"${play['current_price']:.2f}", "inline": True},
                {"name": "Est. Premium", "value": premium_str, "inline": True},
                {"name": "Target Move", "value": f"${play['target_1']:.2f}", "inline": True},
                {"name": "Stop (stock)", "value": f"${play['stop_price']:.2f}", "inline": True},
                {"name": "Est. Gain", "value": f"~{play['estimated_gain_pct']:.0f}%", "inline": True},
                {"name": "VWAP", "value": f"${play['vwap']:.2f}", "inline": True},
                {"name": "RSI (5m)", "value": f"{play['rsi_5m']:.0f}", "inline": True},
                {"name": "Timing", "value": play["timing"], "inline": False},
                {"name": "Exit By", "value": play["exit_by"], "inline": True},
                {"name": "Hold", "value": play["hold"], "inline": True},
                {"name": "Strength", "value": f"{play['strength']}/100", "inline": True},
                {"name": "Signals", "value": signals_text, "inline": False},
            ],
            "footer": {
                "text": f"Elephant Options Scanner | IV: {iv_str} | ATR: {play['atr_pct']:.1f}% | Vol: {play['vol_surge']:.1f}x | NOT financial advice"
            },
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }]
    }


def post_options_to_discord(plays: list[dict], webhook_url: str, max_plays: int = 5):
    """Post options plays to Discord."""
    if not webhook_url:
        return

    header = {
        "embeds": [{
            "title": "Elephant Options Day Trade Scanner",
            "description": f"Scanned {len(OPTIONS_WATCHLIST)} tickers — found **{len(plays)}** setups",
            "color": COLOR_HEADER,
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "footer": {"text": "0DTE / weekly options • high risk • for informational purposes only"},
        }]
    }
    requests.post(webhook_url, json=header, timeout=5)
    time.sleep(1)

    if not plays:
        no_plays = {
            "embeds": [{
                "title": "No Options Plays Found",
                "description": "Market conditions don't show clear setups right now. Will scan again next interval.",
                "color": 0x95A5A6,
            }]
        }
        requests.post(webhook_url, json=no_plays, timeout=5)
        return

    for play in plays[:max_plays]:
        embed = format_options_embed(play)
        try:
            requests.post(webhook_url, json=embed, timeout=5)
            time.sleep(1)
        except Exception as e:
            logger.warning("Failed to post options play for %s: %s", play["ticker"], e)


def run_options_scan(webhook_url: str = "", max_plays: int = 5) -> list[dict]:
    """Run options scan and post results."""
    logger.info("Starting options day trade scan of %d tickers...", len(OPTIONS_WATCHLIST))
    plays = scan_options()
    logger.info("Found %d options plays", len(plays))

    if webhook_url:
        post_options_to_discord(plays, webhook_url, max_plays)

    return plays


if __name__ == "__main__":
    import sys
    logging.basicConfig(level=logging.INFO)

    webhook = ""
    if len(sys.argv) > 1:
        webhook = sys.argv[1]

    plays = run_options_scan(webhook_url=webhook, max_plays=8)
    for p in plays:
        print(f"{p['direction']:4s} {p['ticker']:6s} ${p['strike']:.0f} "
              f"@ ${p['current_price']:.2f} "
              f"premium~${p['estimated_premium']:.2f} "
              f"target=${p['target_1']:.2f} "
              f"gain~{p['estimated_gain_pct']:.0f}% "
              f"hold={p['hold']} "
              f"strength={p['strength']} | {', '.join(p['signals'])}")
