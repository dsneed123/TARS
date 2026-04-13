"""TARS Stock Swing Trade Scanner — scans for swing trade setups and posts to Discord."""
from __future__ import annotations

import json
import logging
import time
from datetime import datetime, timedelta
from typing import Optional

import numpy as np
import pandas as pd
import requests
import yfinance as yf

logger = logging.getLogger("tars.stock_scanner")

# Watchlist: high-volume, swing-tradeable stocks + ETFs
WATCHLIST = [
    # Mega-cap tech
    "AAPL", "MSFT", "GOOGL", "AMZN", "NVDA", "META", "TSLA",
    # Semiconductors
    "AMD", "INTC", "MU", "AVGO", "QCOM",
    # Financials
    "JPM", "BAC", "GS", "V", "MA",
    # Energy
    "XOM", "CVX", "OXY", "SLB",
    # Consumer
    "DIS", "NKE", "SBUX", "MCD",
    # Biotech/Health
    "JNJ", "PFE", "UNH", "ABBV",
    # ETFs
    "SPY", "QQQ", "IWM", "XLF", "XLE", "XLK", "ARKK",
    # High beta / meme
    "PLTR", "SOFI", "COIN", "MARA", "RIOT",
    # Crypto-adjacent
    "IBIT", "MSTR",
    # Crypto (Robinhood supported)
    "BTC-USD", "ETH-USD", "SOL-USD", "XRP-USD", "DOGE-USD",
    "ADA-USD", "AVAX-USD", "DOT-USD", "LINK-USD", "SHIB-USD",
    "UNI-USD", "AAVE-USD", "LTC-USD", "BCH-USD", "ETC-USD",
    "COMP-USD", "MKR-USD", "ATOM-USD", "XLM-USD", "ALGO-USD",
    "FIL-USD", "ICP-USD", "HBAR-USD", "VET-USD", "MANA-USD",
    "SAND-USD", "AXS-USD", "CRV-USD", "LDO-USD", "GRT-USD",
    "SNX-USD", "SUSHI-USD", "YFI-USD", "BAT-USD", "ZRX-USD",
    "ENJ-USD", "CHZ-USD", "STORJ-USD", "CELO-USD",
    "RNDR-USD", "FET-USD", "INJ-USD", "SEI-USD", "TIA-USD",
    "BONK-USD", "FLOKI-USD", "PEPE24478-USD", "WIF-USD",
    "JUP-USD", "PYTH-USD", "ONDO-USD", "STRK-USD",
]

# Discord embed colors
COLOR_BULLISH = 0x2ECC71   # Green
COLOR_BEARISH = 0xE74C3C   # Red
COLOR_NEUTRAL = 0xF39C12   # Yellow


def compute_rsi(series: pd.Series, period: int = 14) -> pd.Series:
    """Compute RSI."""
    delta = series.diff()
    gain = delta.where(delta > 0, 0.0)
    loss = -delta.where(delta < 0, 0.0)
    avg_gain = gain.rolling(window=period, min_periods=period).mean()
    avg_loss = loss.rolling(window=period, min_periods=period).mean()
    rs = avg_gain / avg_loss.replace(0, np.nan)
    return 100 - (100 / (1 + rs))


def compute_macd(series: pd.Series) -> tuple[pd.Series, pd.Series, pd.Series]:
    """Compute MACD, signal line, and histogram."""
    ema12 = series.ewm(span=12, adjust=False).mean()
    ema26 = series.ewm(span=26, adjust=False).mean()
    macd = ema12 - ema26
    signal = macd.ewm(span=9, adjust=False).mean()
    histogram = macd - signal
    return macd, signal, histogram


def compute_bollinger(series: pd.Series, period: int = 20) -> tuple[pd.Series, pd.Series, pd.Series]:
    """Compute Bollinger Bands."""
    sma = series.rolling(window=period).mean()
    std = series.rolling(window=period).std()
    upper = sma + (std * 2)
    lower = sma - (std * 2)
    return upper, sma, lower


def analyze_stock(ticker: str) -> Optional[dict]:
    """Analyze a single stock for swing trade setups.

    Returns a signal dict or None if no setup found.
    """
    try:
        stock = yf.Ticker(ticker)
        # Get 3 months of daily data for swing analysis
        df = stock.history(period="3mo", interval="1d")
        if df is None or len(df) < 30:
            return None

        close = df["Close"]
        volume = df["Volume"]
        high = df["High"]
        low = df["Low"]

        current_price = close.iloc[-1]
        prev_price = close.iloc[-2]

        # Technical indicators
        rsi = compute_rsi(close)
        rsi_now = rsi.iloc[-1]
        rsi_prev = rsi.iloc[-2]

        macd_line, signal_line, macd_hist = compute_macd(close)
        macd_now = macd_hist.iloc[-1]
        macd_prev = macd_hist.iloc[-2]

        bb_upper, bb_mid, bb_lower = compute_bollinger(close)

        # Moving averages
        sma20 = close.rolling(20).mean().iloc[-1]
        sma50 = close.rolling(50).mean().iloc[-1]
        ema9 = close.ewm(span=9, adjust=False).mean().iloc[-1]

        # Volume analysis
        avg_vol = volume.rolling(20).mean().iloc[-1]
        vol_now = volume.iloc[-1]
        vol_ratio = vol_now / avg_vol if avg_vol > 0 else 1

        # ATR for stop-loss and target calculation
        tr = pd.concat([
            high - low,
            (high - close.shift()).abs(),
            (low - close.shift()).abs(),
        ], axis=1).max(axis=1)
        atr = tr.rolling(14).mean().iloc[-1]
        atr_pct = (atr / current_price) * 100

        # --- Signal Detection ---
        signals = []
        score = 0

        # 1. RSI oversold bounce (bullish)
        if rsi_now < 35 and rsi_now > rsi_prev:
            signals.append("RSI oversold bounce")
            score += 25

        # 2. RSI overbought reversal (bearish)
        if rsi_now > 70 and rsi_now < rsi_prev:
            signals.append("RSI overbought reversal")
            score -= 25

        # 3. MACD bullish crossover
        if macd_prev < 0 and macd_now > 0:
            signals.append("MACD bullish crossover")
            score += 30

        # 4. MACD bearish crossover
        if macd_prev > 0 and macd_now < 0:
            signals.append("MACD bearish crossover")
            score -= 30

        # 5. Price bouncing off lower Bollinger Band
        if close.iloc[-1] <= bb_lower.iloc[-1] * 1.01:
            signals.append("Bollinger Band support")
            score += 20

        # 6. Price hitting upper Bollinger Band
        if close.iloc[-1] >= bb_upper.iloc[-1] * 0.99:
            signals.append("Bollinger Band resistance")
            score -= 20

        # 7. Golden cross (SMA20 > SMA50 recent crossover)
        sma20_prev = close.rolling(20).mean().iloc[-5]
        sma50_prev = close.rolling(50).mean().iloc[-5]
        if sma20_prev < sma50_prev and sma20 > sma50:
            signals.append("Golden cross (20/50)")
            score += 25

        # 8. Death cross
        if sma20_prev > sma50_prev and sma20 < sma50:
            signals.append("Death cross (20/50)")
            score -= 25

        # 9. Volume spike (confirms move)
        if vol_ratio > 1.5:
            signals.append(f"Volume spike ({vol_ratio:.1f}x avg)")
            score += 10 if score > 0 else -10

        # 10. Price above all MAs (strong uptrend)
        if current_price > ema9 > sma20 > sma50:
            signals.append("Strong uptrend (above all MAs)")
            score += 15

        # 11. Price below all MAs (strong downtrend)
        if current_price < ema9 < sma20 < sma50:
            signals.append("Strong downtrend (below all MAs)")
            score -= 15

        # --- Only return if there's a meaningful signal ---
        if abs(score) < 25 or not signals:
            return None

        # Determine direction and parameters
        direction = "LONG" if score > 0 else "SHORT"
        strength = abs(score)

        if direction == "LONG":
            entry = current_price
            stop_loss = current_price - (atr * 1.5)
            target_1 = current_price + (atr * 2)
            target_2 = current_price + (atr * 3)
            risk = entry - stop_loss
            reward = target_1 - entry
        else:
            entry = current_price
            stop_loss = current_price + (atr * 1.5)
            target_1 = current_price - (atr * 2)
            target_2 = current_price - (atr * 3)
            risk = stop_loss - entry
            reward = entry - target_1

        rr_ratio = reward / risk if risk > 0 else 0

        # Estimate hold duration based on ATR and targets
        avg_daily_move = atr
        days_to_target = int((abs(target_1 - entry)) / avg_daily_move) if avg_daily_move > 0 else 5
        days_to_target = max(2, min(days_to_target, 15))

        if days_to_target <= 3:
            hold_duration = "2-3 days"
        elif days_to_target <= 5:
            hold_duration = "3-5 days"
        elif days_to_target <= 8:
            hold_duration = "5-8 days"
        else:
            hold_duration = "1-2 weeks"

        # 52-week context
        try:
            info = stock.info
            week52_high = info.get("fiftyTwoWeekHigh", 0)
            week52_low = info.get("fiftyTwoWeekLow", 0)
            pct_from_high = ((current_price - week52_high) / week52_high * 100) if week52_high else 0
        except Exception:
            week52_high = week52_low = pct_from_high = 0

        return {
            "ticker": ticker,
            "direction": direction,
            "strength": strength,
            "signals": signals,
            "entry": round(entry, 2),
            "stop_loss": round(stop_loss, 2),
            "target_1": round(target_1, 2),
            "target_2": round(target_2, 2),
            "rr_ratio": round(rr_ratio, 2),
            "hold_duration": hold_duration,
            "rsi": round(rsi_now, 1),
            "atr_pct": round(atr_pct, 2),
            "vol_ratio": round(vol_ratio, 1),
            "current_price": round(current_price, 2),
            "pct_from_52w_high": round(pct_from_high, 1),
        }

    except Exception as e:
        logger.warning("Failed to analyze %s: %s", ticker, e)
        return None


def scan_all(watchlist: list[str] = None) -> list[dict]:
    """Scan entire watchlist and return sorted signals."""
    if watchlist is None:
        watchlist = WATCHLIST

    results = []
    for ticker in watchlist:
        signal = analyze_stock(ticker)
        if signal:
            results.append(signal)
        time.sleep(0.2)  # Rate limit yfinance

    # Sort by strength descending
    results.sort(key=lambda x: x["strength"], reverse=True)
    return results


def format_discord_embed(signal: dict) -> dict:
    """Format a signal as a Discord embed."""
    direction = signal["direction"]
    color = COLOR_BULLISH if direction == "LONG" else COLOR_BEARISH
    arrow = "LONG" if direction == "LONG" else "SHORT"

    signals_text = "\n".join(f"- {s}" for s in signal["signals"])

    return {
        "embeds": [{
            "title": f"{'📈' if direction == 'LONG' else '📉'} {signal['ticker']} — {arrow} Swing Trade",
            "color": color,
            "fields": [
                {"name": "Entry", "value": f"${signal['entry']:.2f}", "inline": True},
                {"name": "Stop Loss", "value": f"${signal['stop_loss']:.2f}", "inline": True},
                {"name": "Target 1", "value": f"${signal['target_1']:.2f}", "inline": True},
                {"name": "Target 2", "value": f"${signal['target_2']:.2f}", "inline": True},
                {"name": "Risk/Reward", "value": f"{signal['rr_ratio']:.1f}:1", "inline": True},
                {"name": "Hold Duration", "value": signal["hold_duration"], "inline": True},
                {"name": "RSI", "value": f"{signal['rsi']:.0f}", "inline": True},
                {"name": "Volume", "value": f"{signal['vol_ratio']:.1f}x avg", "inline": True},
                {"name": "Strength", "value": f"{signal['strength']}/100", "inline": True},
                {"name": "Signals", "value": signals_text, "inline": False},
            ],
            "footer": {"text": f"Elephant Swing Scanner | {signal['pct_from_52w_high']:.1f}% from 52w high"},
            "timestamp": datetime.utcnow().isoformat(),
        }]
    }


def post_signals_to_discord(signals: list[dict], webhook_url: str, max_signals: int = 5):
    """Post top signals to Discord webhook."""
    if not webhook_url or not signals:
        return

    # Post header
    header = {
        "embeds": [{
            "title": "Elephant Swing Trade Scanner",
            "description": f"Scanned {len(WATCHLIST)} stocks — found **{len(signals)}** setups",
            "color": 0x3498DB,
            "timestamp": datetime.utcnow().isoformat(),
            "footer": {"text": "Signals are informational only — not financial advice"},
        }]
    }
    requests.post(webhook_url, json=header, timeout=5)
    time.sleep(1)

    # Post top signals
    for signal in signals[:max_signals]:
        embed = format_discord_embed(signal)
        try:
            requests.post(webhook_url, json=embed, timeout=5)
            time.sleep(1)
        except Exception as e:
            logger.warning("Failed to post signal for %s: %s", signal["ticker"], e)


def run_scan(webhook_url: str = "", max_signals: int = 5):
    """Run a full scan and post results."""
    logger.info("Starting swing trade scan of %d stocks...", len(WATCHLIST))
    signals = scan_all()
    logger.info("Found %d signals", len(signals))

    if webhook_url:
        post_signals_to_discord(signals, webhook_url, max_signals)

    return signals


if __name__ == "__main__":
    import sys
    logging.basicConfig(level=logging.INFO)

    webhook = ""
    if len(sys.argv) > 1:
        webhook = sys.argv[1]

    signals = run_scan(webhook_url=webhook, max_signals=8)
    for s in signals:
        print(f"{s['direction']:5s} {s['ticker']:6s} entry=${s['entry']:.2f} "
              f"SL=${s['stop_loss']:.2f} T1=${s['target_1']:.2f} "
              f"RR={s['rr_ratio']:.1f}:1 hold={s['hold_duration']} "
              f"strength={s['strength']} | {', '.join(s['signals'])}")
