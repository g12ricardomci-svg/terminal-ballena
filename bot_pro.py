from __future__ import annotations

import re
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from urllib.parse import quote, urlencode

import numpy as np
import pandas as pd
import plotly.graph_objects as go
import requests
import streamlit as st


st.set_page_config(page_title="Mesa AMT", layout="wide")

USER_AGENT = "Mozilla/5.0 (compatible; MesaAMT/1.0)"
MINUTES = {"5m": 5, "15m": 15, "1h": 60, "4h": 240}
STALE_MINUTES = {"5m": 13, "15m": 38, "1h": 150, "4h": 600}

# Símbolo: nombre, categoría, proveedor, símbolo del proveedor, clase de activo.
RAW_INSTRUMENTS = {
    "BTC/USDT": ("Bitcoin / USDT", "Cripto", "okx", "BTC-USDT", "crypto"),
    "ETH/USDT": ("Ethereum / USDT", "Cripto", "okx", "ETH-USDT", "crypto"),
    "BNB/USDT": ("BNB / USDT", "Cripto", "okx", "BNB-USDT", "crypto"),
    "NQ=F": ("Nasdaq 100 Futures", "Índices", "yahoo", "NQ=F", "index"),
    "ES=F": ("S&P 500 Futures", "Índices", "yahoo", "ES=F", "index"),
    "EURUSD=X": ("EUR / USD", "FX", "yahoo", "EURUSD=X", "forex"),
    "GBPUSD=X": ("GBP / USD", "FX", "yahoo", "GBPUSD=X", "forex"),
    "USDJPY=X": ("USD / JPY", "FX", "yahoo", "USDJPY=X", "forex"),
    "AUDUSD=X": ("AUD / USD", "FX", "yahoo", "AUDUSD=X", "forex"),
    "USDCAD=X": ("USD / CAD", "FX", "yahoo", "USDCAD=X", "forex"),
    "USDCHF=X": ("USD / CHF", "FX", "yahoo", "USDCHF=X", "forex"),
    "NZDUSD=X": ("NZD / USD", "FX", "yahoo", "NZDUSD=X", "forex"),
    "EURJPY=X": ("EUR / JPY", "FX", "yahoo", "EURJPY=X", "forex"),
    "GBPJPY=X": ("GBP / JPY", "FX", "yahoo", "GBPJPY=X", "forex"),
    "EURGBP=X": ("EUR / GBP", "FX", "yahoo", "EURGBP=X", "forex"),
    "XAUUSD=X": ("Oro · Gold Futures", "Metales", "yahoo", "GC=F", "metal"),
}

INSTRUMENTS = {
    symbol: {
        "label": values[0],
        "market": values[1],
        "provider": values[2],
        "ticker": values[3],
        "class": values[4],
        "source": "OKX" if values[2] == "okx" else "Yahoo Finance",
    }
    for symbol, values in RAW_INSTRUMENTS.items()
}
INSTRUMENTS["XAUUSD=X"]["source"] = "Yahoo Finance · GC=F"
WATCHLIST = ["BTC/USDT", "ETH/USDT", "NQ=F", "EURUSD=X", "XAUUSD=X"]

NEWS_TOPICS = {
    "Todos los temas": (
        "economía OR mercados financieros OR bolsa OR inflación OR bancos centrales "
        "OR tasas de interés OR empleo OR petróleo OR geopolítica OR bitcoin OR criptomonedas"
    ),
    "Política monetaria": (
        "bancos centrales OR tasas de interés OR Reserva Federal OR Fed "
        "OR Banco Central Europeo OR ECB"
    ),
    "Inflación y empleo": (
        "inflación OR IPC OR empleo OR nóminas OR salarios OR crecimiento económico"
    ),
    "Geopolítica": (
        "geopolítica OR conflicto internacional OR sanciones OR guerra OR comercio mundial"
    ),
    "Energía y materias primas": "petróleo OR energía OR gas natural OR oro OR materias primas",
    "Cripto": "bitcoin OR criptomonedas OR ethereum OR regulación cripto",
}

NEWS_RULES = [
    ("Cripto", r"bitcoin|ethereum|criptomon|cripto|blockchain", ["activos digitales"]),
    (
        "Energía y materias primas",
        r"petróleo|petroleo|gas natural|energía|energia|oro|materias primas|minería|mineras",
        ["energía", "materias primas", "divisas"],
    ),
    (
        "Política monetaria",
        r"fed|reserva federal|banco central|tasas de interés|tipos de interés|tasas",
        ["divisas", "bonos", "índices", "oro"],
    ),
    (
        "Renta fija",
        r"renta fija|bonos|bono|deuda pública|deuda publica",
        ["bonos", "divisas", "índices"],
    ),
    (
        "Inflación y empleo",
        r"inflac|ipc|empleo|nóminas|nominas|salarios",
        ["divisas", "bonos", "índices", "oro"],
    ),
    (
        "Riesgo soberano",
        r"riesgo país|riesgo pais|riesgo soberano",
        ["bonos", "divisas", "índices"],
    ),
    (
        "Geopolítica",
        r"geopol|guerra|conflicto|sanciones|ataque|irán|iran|china|aranceles",
        ["oro", "energía", "divisas", "índices"],
    ),
    (
        "Mercados globales",
        r"mercados|bolsa|acciones|índices|indices|dólar|dolar|divisas",
        ["índices", "divisas"],
    ),
]


def drop_open_candles(frame: pd.DataFrame, timeframe: str) -> pd.DataFrame:
    if frame.empty:
        return frame
    duration = pd.Timedelta(minutes=MINUTES[timeframe])
    return frame.loc[frame.index + duration <= pd.Timestamp.now(tz="UTC")]


def fetch_okx(symbol: str, timeframe: str) -> pd.DataFrame:
    bars = {"5m": "5m", "15m": "15m", "1h": "1H", "4h": "4H"}
    response = requests.get(
        "https://www.okx.com/api/v5/market/candles",
        params={
            "instId": INSTRUMENTS[symbol]["ticker"],
            "bar": bars[timeframe],
            "limit": "200",
        },
        headers={"User-Agent": USER_AGENT},
        timeout=15,
    )
    response.raise_for_status()
    payload = response.json()

    if payload.get("code") != "0" or not payload.get("data"):
        raise RuntimeError(payload.get("msg") or "OKX no devolvió cotizaciones.")

    frame = pd.DataFrame(
        payload["data"],
        columns=[
            "timestamp",
            "open",
            "high",
            "low",
            "close",
            "volume",
            "vol_ccy",
            "vol_quote",
            "confirmed",
        ],
    )
    frame["timestamp"] = pd.to_datetime(
        pd.to_numeric(frame["timestamp"], errors="coerce"),
        unit="ms",
        utc=True,
    )
    frame = frame.loc[frame["confirmed"].astype(str) == "1"].copy()
    frame = frame.set_index("timestamp").sort_index()
    frame = frame[["open", "high", "low", "close", "volume"]]
    frame = frame.apply(pd.to_numeric, errors="coerce").dropna(
        subset=["open", "high", "low", "close"]
    )
    frame["volume"] = frame["volume"].fillna(0)
    return drop_open_candles(frame, timeframe).tail(180)


def fetch_yahoo(symbol: str, timeframe: str) -> pd.DataFrame:
    intervals = {
        "5m": ("5m", "5d"),
        "15m": ("15m", "60d"),
        "1h": ("60m", "1mo"),
        "4h": ("60m", "1mo"),
    }
    interval, period = intervals[timeframe]
    ticker = quote(INSTRUMENTS[symbol]["ticker"], safe="")
    query = urlencode({"interval": interval, "range": period, "events": "div,splits"})
    url = f"https://query1.finance.yahoo.com/v8/finance/chart/{ticker}?{query}"
    response = requests.get(url, headers={"User-Agent": USER_AGENT}, timeout=15)
    response.raise_for_status()

    chart = response.json().get("chart", {})
    results = chart.get("result") or []
    if chart.get("error") or not results:
        raise RuntimeError("Yahoo Finance no devolvió cotizaciones para este símbolo.")

    result = results[0]
    timestamps = result.get("timestamp") or []
    quote_data = result.get("indicators", {}).get("quote", [{}])[0]
    if not timestamps:
        raise RuntimeError("Yahoo Finance no devolvió velas.")

    frame = pd.DataFrame(
        {
            "open": quote_data.get("open") or [None] * len(timestamps),
            "high": quote_data.get("high") or [None] * len(timestamps),
            "low": quote_data.get("low") or [None] * len(timestamps),
            "close": quote_data.get("close") or [None] * len(timestamps),
            "volume": quote_data.get("volume") or [0] * len(timestamps),
        },
        index=pd.to_datetime(timestamps, unit="s", utc=True),
    )
    frame.index.name = "timestamp"
    frame = frame.apply(pd.to_numeric, errors="coerce")
    frame = frame.dropna(subset=["open", "high", "low", "close"])
    frame = frame.loc[~frame.index.duplicated(keep="last")].sort_index()
    frame["volume"] = frame["volume"].fillna(0)

    if timeframe == "4h":
        frame = drop_open_candles(frame, "1h")
        frame = (
            frame.resample("4h", origin="start_day", label="left", closed="left")
            .agg(
                {
                    "open": "first",
                    "high": "max",
                    "low": "min",
                    "close": "last",
                    "volume": "sum",
                }
            )
            .dropna(subset=["open", "high", "low", "close"])
        )

    frame = drop_open_candles(frame, timeframe)
    return frame.tail(180)


@st.cache_data(ttl=300, show_spinner=False)
def fetch_candles(symbol: str, timeframe: str) -> pd.DataFrame:
    if symbol not in INSTRUMENTS or timeframe not in MINUTES:
        raise ValueError("Símbolo o temporalidad no soportados.")

    if INSTRUMENTS[symbol]["provider"] == "okx":
        frame = fetch_okx(symbol, timeframe)
    else:
        frame = fetch_yahoo(symbol, timeframe)

    if len(frame) < 30:
        raise RuntimeError("El proveedor entregó menos de 30 velas cerradas.")
    return frame


def volume_weights(frame: pd.DataFrame) -> tuple[np.ndarray, bool]:
    volume = frame["volume"].fillna(0).to_numpy(dtype=float)
    approximate = bool(np.any(volume <= 0))

    if volume.sum() <= 0:
        volume = np.maximum(
            (frame["high"] - frame["low"]).to_numpy(dtype=float),
            1e-9,
        )
        approximate = True

    return volume, approximate


def volume_profile(frame: pd.DataFrame, bins: int = 18) -> dict:
    selected = frame.iloc[:-3].tail(36)
    if len(selected) < 6:
        selected = frame.tail(min(36, len(frame)))

    price = float(frame["close"].iloc[-1])
    low = float(selected["low"].min())
    high = float(selected["high"].max())

    if not np.isfinite(low) or high <= low:
        return {
            "poc": price,
            "vah": price,
            "val": price,
            "state": "balance",
            "approx": True,
        }

    width = (high - low) / bins
    weights, _ = volume_weights(selected)
    typical = (
        selected["high"].to_numpy()
        + selected["low"].to_numpy()
        + selected["close"].to_numpy()
    ) / 3
    indexes = np.clip(((typical - low) / width).astype(int), 0, bins - 1)
    distribution = np.bincount(indexes, weights=weights, minlength=bins)
    poc_index = int(np.argmax(distribution))
    low_index = high_index = poc_index
    accumulated = float(distribution[poc_index])
    total = float(distribution.sum())

    while accumulated < total * 0.7 and (low_index > 0 or high_index < bins - 1):
        below = distribution[low_index - 1] if low_index > 0 else -1
        above = distribution[high_index + 1] if high_index < bins - 1 else -1

        if above >= below and high_index < bins - 1:
            high_index += 1
            accumulated += float(above)
        elif low_index > 0:
            low_index -= 1
            accumulated += float(below)
        else:
            break

    val = low + low_index * width
    vah = low + (high_index + 1) * width
    state = (
        "out-of-balance-up"
        if price > vah
        else "out-of-balance-down"
        if price < val
        else "balance"
    )

    return {
        "poc": low + (poc_index + 0.5) * width,
        "vah": vah,
        "val": val,
        "state": state,
        "approx": True,
    }


def calculate_lvns(frame: pd.DataFrame, bins: int = 12) -> list[float]:
    selected = frame.tail(18)
    if len(selected) < 6:
        return []

    low = float(selected["low"].min())
    high = float(selected["high"].max())
    if high <= low:
        return []

    width = (high - low) / bins
    weights, _ = volume_weights(selected)
    distribution = np.zeros(bins)

    for pos, (_, row) in enumerate(selected.iterrows()):
        start = int(np.clip((row["low"] - low) / width, 0, bins - 1))
        end = int(np.clip((row["high"] - low) / width, 0, bins - 1))
        distribution[start : end + 1] += weights[pos] / (end - start + 1)

    average = float(distribution.mean())
    return [
        low + (i + 0.5) * width
        for i in range(1, bins - 1)
        if distribution[i] <= distribution[i - 1]
        and distribution[i] <= distribution[i + 1]
        and distribution[i] < average * 0.6
    ][:6]


def find_order_block(frame: pd.DataFrame) -> dict | None:
    for i in range(len(frame) - 2, max(1, len(frame) - 10) - 1, -1):
        base = frame.iloc[i]
        impulse = frame.iloc[i + 1]
        body = abs(float(impulse["close"] - impulse["open"]))
        prior = frame.iloc[max(0, i - 10) : i]
        average_range = (
            float((prior["high"] - prior["low"]).mean()) if len(prior) else 0
        )
        threshold = average_range * 0.8 if average_range else body * 0.8

        if (
            base["close"] < base["open"]
            and impulse["close"] > impulse["open"]
            and impulse["close"] > base["high"]
            and body >= threshold
        ):
            return {
                "type": "OB_ALCISTA",
                "low": float(base["low"]),
                "high": float(base["high"]),
                "timestamp": frame.index[i],
            }

        if (
            base["close"] > base["open"]
            and impulse["close"] < impulse["open"]
            and impulse["close"] < base["low"]
            and body >= threshold
        ):
            return {
                "type": "OB_BAJISTA",
                "low": float(base["low"]),
                "high": float(base["high"]),
                "timestamp": frame.index[i],
            }

    return None


def find_fvg(frame: pd.DataFrame) -> dict | None:
    for i in range(len(frame) - 1, max(2, len(frame) - 7) - 1, -1):
        first = frame.iloc[i - 2]
        third = frame.iloc[i]

        if third["low"] > first["high"]:
            return {
                "type": "FVG_ALCISTA",
                "low": float(first["high"]),
                "high": float(third["low"]),
                "timestamp": frame.index[i],
            }

        if third["high"] < first["low"]:
            return {
                "type": "FVG_BAJISTA",
                "low": float(third["high"]),
                "high": float(first["low"]),
                "timestamp": frame.index[i],
            }

    return None


def technicals(frame: pd.DataFrame) -> dict:
    candles = frame.copy()
    close = candles["close"].astype(float)
    high = candles["high"].astype(float)
    low = candles["low"].astype(float)
    volume = candles["volume"].fillna(0).astype(float)

    candles["ema9"] = close.ewm(span=9, adjust=False).mean()
    candles["ema21"] = close.ewm(span=21, adjust=False).mean()

    typical = (high + low + close) / 3
    vol_sum = volume.rolling(24, min_periods=1).sum()
    weighted = (typical * volume).rolling(24, min_periods=1).sum()
    fallback = close.rolling(24, min_periods=1).mean()
    candles["vwap"] = (
        weighted / vol_sum.replace(0, np.nan)
    ).fillna(fallback)

    spread = (high - low).replace(0, np.nan)
    candles["cvd"] = (((close - candles["open"]) / spread).fillna(0) * volume).cumsum()

    previous = close.shift(1)
    true_range = pd.concat(
        [
            high - low,
            (high - previous).abs(),
            (low - previous).abs(),
        ],
        axis=1,
    ).max(axis=1)
    candles["atr"] = true_range.rolling(14, min_periods=1).mean()

    up = high.diff()
    down = -low.diff()
    plus_dm = pd.Series(
        np.where((up > down) & (up > 0), up, 0),
        index=candles.index,
    )
    minus_dm = pd.Series(
        np.where((down > up) & (down > 0), down, 0),
        index=candles.index,
    )
    smooth_tr = true_range.ewm(alpha=1 / 14, adjust=False, min_periods=14).mean()
    plus_di = (
        100
        * plus_dm.ewm(alpha=1 / 14, adjust=False, min_periods=14).mean()
        / smooth_tr.replace(0, np.nan)
    )
    minus_di = (
        100
        * minus_dm.ewm(alpha=1 / 14, adjust=False, min_periods=14).mean()
        / smooth_tr.replace(0, np.nan)
    )
    dx = (
        100 * (plus_di - minus_di).abs() / (plus_di + minus_di).replace(0, np.nan)
    ).fillna(0)
    candles["adx"] = dx.ewm(alpha=1 / 14, adjust=False, min_periods=1).mean()

    _, approximate = volume_weights(frame)
    return {
        "candles": candles,
        "profile": volume_profile(frame),
        "lvns": calculate_lvns(frame),
        "ob": find_order_block(frame),
        "fvg": find_fvg(frame),
        "atr": float(candles["atr"].iloc[-1]),
        "adx": float(candles["adx"].iloc[-1]),
        "cvd": float(candles["cvd"].iloc[-1]),
        "volume_approx": approximate,
    }


def bias(data: dict, require_adx: bool = False) -> str:
    last = data["candles"].iloc[-1]
    adx_ok = not require_adx or data["adx"] >= 22

    if last["close"] > last["vwap"] and last["ema9"] > last["ema21"] and adx_ok:
        return "Alcista"
    if last["close"] < last["vwap"] and last["ema9"] < last["ema21"] and adx_ok:
        return "Bajista"
    return "Neutral"


def four_hour_bias(data: dict) -> str:
    last = data["candles"].iloc[-1]
    if last["ema9"] > last["ema21"]:
        return "Alcista"
    if last["ema9"] < last["ema21"]:
        return "Bajista"
    return "Neutral"


def stale(frame: pd.DataFrame, timeframe: str) -> bool:
    if frame.empty:
        return True
    age = (pd.Timestamp.now(tz="UTC") - frame.index[-1]).total_seconds() / 60
    return age > STALE_MINUTES[timeframe]


def near_zone(price: float, zone: dict | None, tolerance: float) -> bool:
    return bool(zone and zone["low"] - tolerance <= price <= zone["high"] + tolerance)


def evaluate_signal(
    symbol: str,
    data: dict,
    h1: dict,
    h4: dict,
    is_stale: bool,
) -> dict | None:
    candles = data["candles"]
    if is_stale or len(candles) < 30:
        return None

    last = candles.iloc[-1]
    previous = candles.iloc[-2]
    price = float(last["close"])
    atr = max(float(data["atr"]), 0)
    profile = data["profile"]
    poc, vah, val = profile["poc"], profile["vah"], profile["val"]
    bias_1h = bias(h1, True)
    bias_4h = four_hour_bias(h4)
    adx_floor = {
        "crypto": 28,
        "forex": 25,
        "metal": 30,
        "index": 25,
    }[INSTRUMENTS[symbol]["class"]]

    recent = candles.tail(18)
    swing_high = float(recent["high"].max())
    swing_low = float(recent["low"].min())
    swing = max(swing_high - swing_low, 1e-12)
    long_discount = (swing_high - price) / swing * 100
    short_discount = (price - swing_low) / swing * 100
    bullish = last["close"] > last["open"]
    bearish = last["close"] < last["open"]
    tolerance = max(atr * 0.35, price * 0.00001)

    ob = data["ob"]
    fvg = data["fvg"]
    lvn = next(
        (
            level
            for level in data["lvns"]
            if abs(level - price) <= max(atr * 0.5, 1e-12)
        ),
        None,
    )

    long_zone = (
        (ob and ob["type"] == "OB_ALCISTA" and near_zone(price, ob, tolerance))
        or (
            fvg
            and fvg["type"] == "FVG_ALCISTA"
            and near_zone(price, fvg, tolerance)
        )
    )
    short_zone = (
        (ob and ob["type"] == "OB_BAJISTA" and near_zone(price, ob, tolerance))
        or (
            fvg
            and fvg["type"] == "FVG_BAJISTA"
            and near_zone(price, fvg, tolerance)
        )
    )
    average_volume = float(candles["volume"].tail(20).mean())
    volume_expansion = (
        average_volume > 0 and float(last["volume"]) > average_volume * 1.15
    )

    def make_signal(
        code: str,
        model: str,
        direction: str,
        zone_price: float,
        target: float,
        discount: float,
        htf: bool,
        notes: list[str],
    ) -> dict:
        buffer = max(atr * 0.35, price * 0.00001)

        if direction == "LONG":
            stop_loss = min(zone_price - buffer, price - buffer)
            risk = price - stop_loss
            take_profit = max(target, price + risk * 2.5)
        else:
            stop_loss = max(zone_price + buffer, price + buffer)
            risk = stop_loss - price
            take_profit = min(target, price - risk * 2.5)

        score = (
            int(discount >= 60)
            + int(discount >= 80)
            + int(htf)
            + int(lvn is not None)
            + int(volume_expansion)
        )
        return {
            "code": code,
            "model": model,
            "direction": direction,
            "quality": min(score, 5),
            "entry": price,
            "stop": stop_loss,
            "target": take_profit,
            "rr": abs(take_profit - price) / risk if risk > 0 else None,
            "notes": notes,
        }

    # Modelo 1: tendencia fuera de balance con retroceso a una zona.
    if (
        profile["state"] == "out-of-balance-up"
        and bias_1h == "Alcista"
        and data["adx"] >= adx_floor
        and bias_4h != "Bajista"
        and long_discount >= 50
        and (long_zone or lvn is not None)
        and bullish
        and (price >= last["ema21"] or price >= last["vwap"])
    ):
        zone = (
            ob
            if ob
            and ob["type"] == "OB_ALCISTA"
            and near_zone(price, ob, tolerance)
            else fvg
            if fvg
            and fvg["type"] == "FVG_ALCISTA"
            and near_zone(price, fvg, tolerance)
            else None
        )
        return make_signal(
            "AMT_TREND_LONG",
            "Tendencia fuera de balance",
            "LONG",
            zone["low"] if zone else lvn or price - atr,
            swing_high,
            long_discount,
            bias_4h == "Alcista",
            [
                "Precio sobre el área de valor con sesgo 1H alineado.",
                "Zona calculada con velas cerradas.",
            ],
        )

    if (
        profile["state"] == "out-of-balance-down"
        and bias_1h == "Bajista"
        and data["adx"] >= adx_floor
        and bias_4h != "Alcista"
        and short_discount >= 50
        and (short_zone or lvn is not None)
        and bearish
        and (price <= last["ema21"] or price <= last["vwap"])
    ):
        zone = (
            ob
            if ob
            and ob["type"] == "OB_BAJISTA"
            and near_zone(price, ob, tolerance)
            else fvg
            if fvg
            and fvg["type"] == "FVG_BAJISTA"
            and near_zone(price, fvg, tolerance)
            else None
        )
        return make_signal(
            "AMT_TREND_SHORT",
            "Tendencia fuera de balance",
            "SHORT",
            zone["high"] if zone else lvn or price + atr,
            swing_low,
            short_discount,
            bias_4h == "Bajista",
            [
                "Precio bajo el área de valor con sesgo 1H alineado.",
                "Zona calculada con velas cerradas.",
            ],
        )

    # Modelo 2: ruptura fallida del área de valor y retorno al POC.
    failed_high = (
        (previous["high"] > vah or last["high"] > vah)
        and price < vah
        and bearish
        and poc < price
    )
    if profile["state"] == "balance" and failed_high and bias_1h != "Alcista":
        stop = max(float(previous["high"]), float(last["high"])) + max(
            atr * 0.25, price * 0.00001
        )
        risk = stop - price
        if risk > 0 and price - poc >= risk * 1.3:
            return {
                "code": "AMT_MEAN_REVERSION_SHORT",
                "model": "Reversión al POC",
                "direction": "SHORT",
                "quality": 3,
                "entry": price,
                "stop": stop,
                "target": poc,
                "rr": (price - poc) / risk,
                "notes": [
                    "Ruptura fallida sobre VAH; objetivo técnico en POC.",
                    "Regla heurística sin backtesting.",
                ],
            }

    failed_low = (
        (previous["low"] < val or last["low"] < val)
        and price > val
        and bullish
        and poc > price
    )
    if profile["state"] == "balance" and failed_low and bias_1h != "Bajista":
        stop = min(float(previous["low"]), float(last["low"])) - max(
            atr * 0.25, price * 0.00001
        )
        risk = price - stop
        if risk > 0 and poc - price >= risk * 1.3:
            return {
                "code": "AMT_MEAN_REVERSION_LONG",
                "model": "Reversión al POC",
                "direction": "LONG",
                "quality": 3,
                "entry": price,
                "stop": stop,
                "target": poc,
                "rr": (poc - price) / risk,
                "notes": [
                    "Ruptura fallida bajo VAL; objetivo técnico en POC.",
                    "Regla heurística sin backtesting.",
                ],
            }

    # Modelo 3: zona 4H con confirmación local.
    h4_zones = [zone for zone in (h4["ob"], h4["fvg"]) if zone]
    h4_tolerance = max(float(h4["atr"]) * 0.25, tolerance)

    if (
        bias_4h == "Alcista"
        and ob
        and ob["type"] == "OB_ALCISTA"
        and near_zone(price, ob, tolerance)
        and bullish
        and long_discount >= 50
        and any(
            zone["type"].endswith("ALCISTA")
            and near_zone(price, zone, h4_tolerance)
            for zone in h4_zones
        )
    ):
        return make_signal(
            "SENSEI_FRACTAL_LONG",
            "Fractal 4H",
            "LONG",
            ob["low"],
            swing_high,
            long_discount,
            True,
            ["Sesgo 4H alcista con zona 4H y confirmación local.", "Regla heurística."],
        )

    if (
        bias_4h == "Bajista"
        and ob
        and ob["type"] == "OB_BAJISTA"
        and near_zone(price, ob, tolerance)
        and bearish
        and short_discount >= 50
        and any(
            zone["type"].endswith("BAJISTA")
            and near_zone(price, zone, h4_tolerance)
            for zone in h4_zones
        )
    ):
        return make_signal(
            "SENSEI_FRACTAL_SHORT",
            "Fractal 4H",
            "SHORT",
            ob["high"],
            swing_low,
            short_discount,
            True,
            ["Sesgo 4H bajista con zona 4H y confirmación local.", "Regla heurística."],
        )

    return None


def build_analysis(symbol: str, timeframe: str) -> dict:
    if symbol not in INSTRUMENTS:
        raise ValueError("Símbolo no soportado.")
    if timeframe not in ("5m", "15m", "1h"):
        raise ValueError("Temporalidad no válida.")

    eval_tf = "15m" if timeframe == "1h" else timeframe
    chart_frame = fetch_candles(symbol, timeframe)
    h1_frame = chart_frame if timeframe == "1h" else fetch_candles(symbol, "1h")
    h4_frame = fetch_candles(symbol, "4h")
    eval_frame = chart_frame if eval_tf == timeframe else fetch_candles(symbol, eval_tf)

    chart_data = technicals(chart_frame)
    h1 = chart_data if timeframe == "1h" else technicals(h1_frame)
    h4 = technicals(h4_frame)
    eval_data = chart_data if eval_tf == timeframe else technicals(eval_frame)

    is_stale = stale(chart_frame, timeframe) or stale(eval_frame, eval_tf)
    signal = evaluate_signal(symbol, eval_data, h1, h4, is_stale)
    last = chart_data["candles"].iloc[-1]
    previous = chart_data["candles"].iloc[-2]
    change = (
        (float(last["close"]) / float(previous["close"]) - 1) * 100
        if previous["close"]
        else 0
    )

    bias_1h = bias(h1, True)
    bias_4h = four_hour_bias(h4)
    recent = eval_data["candles"].tail(18)
    swing = max(float(recent["high"].max() - recent["low"].min()), 1e-12)
    close = float(eval_data["candles"]["close"].iloc[-1])
    long_discount = (float(recent["high"].max()) - close) / swing * 100
    short_discount = (close - float(recent["low"].min())) / swing * 100
    discount = (
        short_discount
        if bias_1h == "Bajista"
        or (bias_1h == "Neutral" and bias_4h == "Bajista")
        else long_discount
    )

    notes = [
        "POC, VAH, VAL y LVN se estiman con velas OHLCV; no son volumen negociado por nivel.",
        "CVD es un proxy por dirección de vela, no flujo bid/ask real.",
        "Las reglas son heurísticas sin backtesting; no constituyen recomendaciones.",
    ]
    if chart_data["volume_approx"]:
        notes.append(
            "El proveedor reporta volumen cero o incompleto; los indicadores de volumen son aproximados."
        )
    if timeframe == "1h":
        notes.append("Gráfica y niveles en 1H; el setup se evalúa en 15m.")
    if is_stale:
        notes.append("Datos retrasados: no se muestra una señal activa.")

    return {
        "symbol": symbol,
        "label": INSTRUMENTS[symbol]["label"],
        "source": INSTRUMENTS[symbol]["source"],
        "timeframe": timeframe,
        "eval_tf": eval_tf,
        "candles": chart_data["candles"],
        "chart_data": chart_data,
        "data": eval_data,
        "bias1h": bias_1h,
        "bias4h": bias_4h,
        "state": eval_data["profile"]["state"],
        "adx": eval_data["adx"],
        "discount": round(discount, 1),
        "stale": is_stale,
        "signal": signal,
        "price": float(last["close"]),
        "change": change,
        "as_of": chart_frame.index[-1],
        "notes": notes,
    }


@st.cache_data(ttl=300, show_spinner=False)
def fetch_news(topic: str) -> list[dict]:
    params = {
        "q": f"{NEWS_TOPICS[topic]} when:1d",
        "hl": "es-419",
        "gl": "CO",
        "ceid": "CO:es-419",
    }
    response = requests.get(
        "https://news.google.com/rss/search",
        params=params,
        headers={
            "User-Agent": USER_AGENT,
            "Accept": "application/rss+xml, application/xml",
        },
        timeout=15,
    )
    response.raise_for_status()

    root = ET.fromstring(response.content)
    items = []
    seen = set()

    for item in root.findall("./channel/item"):
        title = (item.findtext("title") or "").strip()
        url = (item.findtext("link") or "").strip()
        if not title or not url.startswith("https://") or url in seen:
            continue
        seen.add(url)

        date = None
        try:
            date = parsedate_to_datetime(item.findtext("pubDate") or "")
            if date.tzinfo is None:
                date = date.replace(tzinfo=timezone.utc)
            date = date.astimezone(timezone.utc)
        except (TypeError, ValueError, OverflowError):
            pass

        matches = [
            (name, markets)
            for name, pattern, markets in NEWS_RULES
            if re.search(pattern, title, re.IGNORECASE)
        ]
        if matches:
            category = matches[0][0]
            markets = list(dict.fromkeys(m for _, values in matches for m in values))
        else:
            category = topic if topic != "Todos los temas" else "Mercados globales"
            markets = ["mercados globales"]

        items.append(
            {
                "title": title[:360],
                "url": url,
                "source": item.findtext("source") or "Fuente enlazada",
                "date": date,
                "category": category,
                "markets": markets,
            }
        )

    items.sort(
        key=lambda item: item["date"]
        or datetime.min.replace(tzinfo=timezone.utc),
        reverse=True,
    )
    return items[:12]


@st.cache_data(ttl=300, show_spinner=False)
def fetch_watchlist() -> list[dict]:
    rows = []

    for symbol in WATCHLIST:
        instrument = INSTRUMENTS[symbol]
        try:
            frame = fetch_candles(symbol, "1h")
            last = float(frame["close"].iloc[-1])
            previous = float(frame["close"].iloc[-2])
            change = (last / previous - 1) * 100 if previous else 0

            rows.append(
                {
                    "Activo": instrument["label"],
                    "Símbolo": symbol,
                    "Precio": last,
                    "Cambio 1h": change,
                    "Estado": "Desactualizado" if stale(frame, "1h") else "Disponible",
                    "Fuente": instrument["source"],
                }
            )
        except Exception:
            rows.append(
                {
                    "Activo": instrument["label"],
                    "Símbolo": symbol,
                    "Precio": np.nan,
                    "Cambio 1h": np.nan,
                    "Estado": "Sin cotización",
                    "Fuente": instrument["source"],
                }
            )

    return rows


def format_price(value: float, symbol: str) -> str:
    if symbol in ("XAUUSD=X", "BTC/USDT", "ETH/USDT", "BNB/USDT", "NQ=F", "ES=F"):
        decimals = 2
    elif "JPY" in symbol:
        decimals = 3
    else:
        decimals = 5

    return f"{value:,.{decimals}f}" if np.isfinite(value) else "—"


def build_chart(analysis: dict) -> go.Figure:
    candles = analysis["candles"].tail(180)
    profile = analysis["chart_data"]["profile"]
    fig = go.Figure()

    fig.add_hrect(
        y0=profile["val"],
        y1=profile["vah"],
        fillcolor="rgba(196,153,79,0.12)",
        line_width=0,
        layer="below",
    )
    fig.add_trace(
        go.Candlestick(
            x=candles.index,
            open=candles["open"],
            high=candles["high"],
            low=candles["low"],
            close=candles["close"],
            name="Velas",
            increasing_line_color="#27725d",
            decreasing_line_color="#c2664f",
        )
    )

    for column, label, color in (
        ("ema9", "EMA 9", "#27725d"),
        ("ema21", "EMA 21", "#846c98"),
        ("vwap", "VWAP", "#496f92"),
    ):
        fig.add_trace(
            go.Scatter(
                x=candles.index,
                y=candles[column],
                mode="lines",
                name=label,
                line={"width": 1.4, "color": color},
            )
        )

    for value, name, color, dash in (
        (profile["poc"], "POC", "#6b6455", "solid"),
        (profile["vah"], "VAH", "#b18c47", "dot"),
        (profile["val"], "VAL", "#b18c47", "dot"),
    ):
        fig.add_hline(
            y=value,
            line_color=color,
            line_dash=dash,
            line_width=1,
            annotation_text=name,
            annotation_position="right",
        )

    for value in analysis["chart_data"]["lvns"]:
        fig.add_hline(
            y=value,
            line_color="#b7a36e",
            line_dash="dash",
            line_width=1,
            opacity=0.6,
        )

    fig.update_layout(
        template="plotly_white",
        height=460,
        margin={"l": 10, "r": 42, "t": 30, "b": 10},
        xaxis_rangeslider_visible=False,
        hovermode="x unified",
        legend={"orientation": "h", "y": 1.05, "x": 0},
        yaxis={"side": "right"},
    )
    return fig


def render_signal(signal: dict | None, symbol: str) -> None:
    if not signal:
        st.info("Sin señal activa: no se cumplen todas las reglas del modelo.")
        return

    st.markdown(f"**{signal['direction']} · {signal['model']}**")
    st.caption(f"Calidad de reglas: {signal['quality']}/5. No es una probabilidad.")
    cols = st.columns(3)
    cols[0].metric("Entrada teórica", format_price(signal["entry"], symbol))
    cols[1].metric("Stop técnico", format_price(signal["stop"], symbol))
    cols[2].metric("Objetivo técnico", format_price(signal["target"], symbol))
    st.metric("R:R heurístico", f"{signal['rr']:.2f}" if signal["rr"] is not None else "—")

    for note in signal["notes"]:
        st.write(f"- {note}")

    st.warning("Niveles informativos. No calcula lotaje ni envía órdenes.")


def main() -> None:
    st.title("MESA / AMT · Terminal de mercado")
    st.caption("Contexto de subasta 1H/4H · Solo lectura · Sin ejecución de operaciones")

    with st.sidebar:
        st.subheader("Configuración")
        symbol = st.selectbox(
            "Instrumento",
            list(INSTRUMENTS),
            format_func=lambda value: f"{value} · {INSTRUMENTS[value]['label']}",
        )
        timeframe = st.selectbox("Temporalidad", ["5m", "15m", "1h"], index=2)

        if st.button("Actualizar datos", use_container_width=True):
            st.cache_data.clear()
            st.rerun()

        st.caption("Datos en caché durante cinco minutos.")
        st.info("No requiere claves API. No se conecta al broker y no opera.")

    try:
        with st.spinner("Cargando velas cerradas y calculando análisis..."):
            analysis = build_analysis(symbol, timeframe)
    except Exception as error:
        st.error(f"No se pudo cargar el análisis: {error}")
        st.caption("El proveedor puede limitar o retrasar datos. Intenta actualizar.")
        st.stop()

    cols = st.columns(4)
    cols[0].metric("Último cierre", format_price(analysis["price"], symbol))
    cols[1].metric("Cambio de vela", f"{analysis['change']:+.2f}%")
    cols[2].metric("Fuente", analysis["source"])
    cols[3].metric(
        "Última vela",
        pd.Timestamp(analysis["as_of"]).strftime("%d %b %Y, %H:%M UTC"),
    )

    if analysis["stale"]:
        st.warning("Cotización retrasada. No se muestra una señal activa.")

    st.subheader(f"{analysis['label']} · {symbol} · {timeframe}")
    st.plotly_chart(build_chart(analysis), use_container_width=True)

    csv_data = (
        analysis["candles"]
        .tail(180)
        .reset_index()
        .to_csv(index=False)
        .encode("utf-8")
    )
    st.download_button(
        "Descargar velas CSV",
        csv_data,
        file_name=f"{symbol.replace('/', '-')}-{timeframe}.csv",
        mime="text/csv",
    )

    if timeframe == "1h":
        st.caption("Gráfica y niveles en 1H; la evaluación de setups se hace en 15m.")

    context, setups = st.columns([1.1, 1])
    with context:
        st.subheader("Estructura de mercado")
        metrics = st.columns(2)
        metrics[0].metric("Sesgo 4H", analysis["bias4h"])
        metrics[1].metric("Sesgo 1H", analysis["bias1h"])
        metrics[0].metric(
            "Estado AMT",
            {
                "balance": "Equilibrio",
                "out-of-balance-up": "Fuera de balance · arriba",
                "out-of-balance-down": "Fuera de balance · abajo",
            }.get(analysis["state"], "Sin datos"),
        )
        metrics[1].metric(
            "ADX / descuento",
            f"{analysis['adx']:.1f} / {analysis['discount']:.1f}%",
        )

        profile = analysis["chart_data"]["profile"]
        levels = st.columns(4)
        levels[0].metric("POC", format_price(profile["poc"], symbol))
        levels[1].metric("VAH", format_price(profile["vah"], symbol))
        levels[2].metric("VAL", format_price(profile["val"], symbol))
        levels[3].metric(
            "ATR",
            format_price(analysis["chart_data"]["atr"], symbol),
        )

        st.caption(
            f"CVD proxy: {analysis['chart_data']['cvd']:,.2f}; "
            "no es flujo bid/ask real."
        )
        with st.expander("Limitaciones de datos"):
            for note in analysis["notes"]:
                st.write(f"- {note}")

    with setups:
        st.subheader("Señal basada en reglas")
        st.caption(f"Temporalidad evaluada: {analysis['eval_tf']}") **…**

_This response is too long to display in full._