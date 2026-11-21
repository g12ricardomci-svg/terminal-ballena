from __future__ import annotations

import re
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from typing import Any
from urllib.parse import quote, urlencode

import numpy as np
import pandas as pd
import plotly.graph_objects as go
import requests
import streamlit as st


APP_NAME = "MESA / AMT · Terminal de mercado"
USER_AGENT = "Mozilla/5.0 (compatible; AMTMarketDashboard/1.0)"
TIMEFRAME_MINUTES = {"5m": 5, "15m": 15, "1h": 60, "4h": 240}
STALE_AFTER_MINUTES = {"5m": 12.5, "15m": 37.5, "1h": 150, "4h": 600}

INSTRUMENTS: dict[str, dict[str, str]] = {
    "BTC/USDT": {
        "label": "Bitcoin / USDT",
        "market": "Cripto",
        "provider": "okx",
        "source": "OKX",
        "provider_symbol": "BTC-USDT",
        "class": "crypto",
    },
    "ETH/USDT": {
        "label": "Ethereum / USDT",
        "market": "Cripto",
        "provider": "okx",
        "source": "OKX",
        "provider_symbol": "ETH-USDT",
        "class": "crypto",
    },
    "BNB/USDT": {
        "label": "BNB / USDT",
        "market": "Cripto",
        "provider": "okx",
        "source": "OKX",
        "provider_symbol": "BNB-USDT",
        "class": "crypto",
    },
    "NQ=F": {
        "label": "Nasdaq 100 Futures",
        "market": "Índices",
        "provider": "yahoo",
        "source": "Yahoo Finance",
        "provider_symbol": "NQ=F",
        "class": "index",
    },
    "ES=F": {
        "label": "S&P 500 Futures",
        "market": "Índices",
        "provider": "yahoo",
        "source": "Yahoo Finance",
        "provider_symbol": "ES=F",
        "class": "index",
    },
    "EURUSD=X": {
        "label": "EUR / USD",
        "market": "FX",
        "provider": "yahoo",
        "source": "Yahoo Finance",
        "provider_symbol": "EURUSD=X",
        "class": "forex",
    },
    "GBPUSD=X": {
        "label": "GBP / USD",
        "market": "FX",
        "provider": "yahoo",
        "source": "Yahoo Finance",
        "provider_symbol": "GBPUSD=X",
        "class": "forex",
    },
    "USDJPY=X": {
        "label": "USD / JPY",
        "market": "FX",
        "provider": "yahoo",
        "source": "Yahoo Finance",
        "provider_symbol": "USDJPY=X",
        "class": "forex",
    },
    "AUDUSD=X": {
        "label": "AUD / USD",
        "market": "FX",
        "provider": "yahoo",
        "source": "Yahoo Finance",
        "provider_symbol": "AUDUSD=X",
        "class": "forex",
    },
    "USDCAD=X": {
        "label": "USD / CAD",
        "market": "FX",
        "provider": "yahoo",
        "source": "Yahoo Finance",
        "provider_symbol": "USDCAD=X",
        "class": "forex",
    },
    "USDCHF=X": {
        "label": "USD / CHF",
        "market": "FX",
        "provider": "yahoo",
        "source": "Yahoo Finance",
        "provider_symbol": "USDCHF=X",
        "class": "forex",
    },
    "NZDUSD=X": {
        "label": "NZD / USD",
        "market": "FX",
        "provider": "yahoo",
        "source": "Yahoo Finance",
        "provider_symbol": "NZDUSD=X",
        "class": "forex",
    },
    "EURJPY=X": {
        "label": "EUR / JPY",
        "market": "FX",
        "provider": "yahoo",
        "source": "Yahoo Finance",
        "provider_symbol": "EURJPY=X",
        "class": "forex",
    },
    "GBPJPY=X": {
        "label": "GBP / JPY",
        "market": "FX",
        "provider": "yahoo",
        "source": "Yahoo Finance",
        "provider_symbol": "GBPJPY=X",
        "class": "forex",
    },
    "EURGBP=X": {
        "label": "EUR / GBP",
        "market": "FX",
        "provider": "yahoo",
        "source": "Yahoo Finance",
        "provider_symbol": "EURGBP=X",
        "class": "forex",
    },
    "XAUUSD=X": {
        "label": "Oro · Gold Futures",
        "market": "Metales",
        "provider": "yahoo",
        "source": "Yahoo Finance · GC=F",
        "provider_symbol": "GC=F",
        "class": "metal",
    },
}

WATCHLIST = ["BTC/USDT", "ETH/USDT", "NQ=F", "EURUSD=X", "XAUUSD=X"]

NEWS_TOPICS: dict[str, str] = {
    "Todos los temas": (
        "economía OR mercados financieros OR bolsa OR inflación OR bancos centrales "
        "OR tasas de interés OR empleo OR petróleo OR geopolítica OR bitcoin "
        "OR criptomonedas"
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

HEADLINE_RULES: list[tuple[str, re.Pattern[str], list[str]]] = [
    (
        "Cripto",
        re.compile(r"bitcoin|ethereum|criptomon|cripto|blockchain", re.IGNORECASE),
        ["activos digitales"],
    ),
    (
        "Energía y materias primas",
        re.compile(
            r"petróleo|petroleo|gas natural|energía|energia|oro|materias primas|minería|mineras",
            re.IGNORECASE,
        ),
        ["energía", "materias primas", "divisas"],
    ),
    (
        "Política monetaria",
        re.compile(
            r"fed|reserva federal|banco central|tasas de interés|tipos de interés|"
            r"tipos de interes|tasas",
            re.IGNORECASE,
        ),
        ["divisas", "bonos", "índices", "oro"],
    ),
    (
        "Renta fija",
        re.compile(r"renta fija|bonos|bono|deuda pública|deuda publica", re.IGNORECASE),
        ["bonos", "divisas", "índices"],
    ),
    (
        "Inflación y empleo",
        re.compile(r"inflac|ipc|empleo|nóminas|nominas|salarios", re.IGNORECASE),
        ["divisas", "bonos", "índices", "oro"],
    ),
    (
        "Riesgo soberano",
        re.compile(
            r"riesgo país|riesgo pais|riesgo soberano",
            re.IGNORECASE,
        ),
        ["bonos", "divisas", "índices"],
    ),
    (
        "Geopolítica",
        re.compile(
            r"geopol|guerra|conflicto|sanciones|ataque|irán|iran|china|aranceles",
            re.IGNORECASE,
        ),
        ["oro", "energía", "divisas", "índices"],
    ),
    (
        "Mercados globales",
        re.compile(r"mercados|bolsa|acciones|índices|indices|dólar|dolar|divisas", re.IGNORECASE),
        ["índices", "divisas"],
    ),
]


def _ensure_supported(symbol: str) -> dict[str, str]:
    instrument = INSTRUMENTS.get(symbol)
    if instrument is None:
        raise ValueError(f"Símbolo no soportado: {symbol}")
    return instrument


def _drop_incomplete_bars(frame: pd.DataFrame, timeframe: str) -> pd.DataFrame:
    if frame.empty:
        return frame
    duration = pd.Timedelta(minutes=TIMEFRAME_MINUTES[timeframe])
    now = pd.Timestamp.now(tz="UTC")
    return frame.loc[frame.index + duration <= now]


def _fetch_okx_candles(symbol: str, timeframe: str) -> pd.DataFrame:
    instrument = _ensure_supported(symbol)
    bars = {"5m": "5m", "15m": "15m", "1h": "1H", "4h": "4H"}
    params = {
        "instId": instrument["provider_symbol"],
        "bar": bars[timeframe],
        "limit": "200",
    }
    response = requests.get(
        "https://www.okx.com/api/v5/market/candles",
        params=params,
        headers={"User-Agent": USER_AGENT},
        timeout=15,
    )
    response.raise_for_status()
    payload = response.json()
    if payload.get("code") != "0":
        raise RuntimeError(payload.get("msg") or "OKX no entregó cotizaciones.")

    rows = payload.get("data", [])
    if not rows:
        raise RuntimeError("OKX no devolvió velas para este instrumento.")
    frame = pd.DataFrame(
        rows,
        columns=[
            "timestamp",
            "open",
            "high",
            "low",
            "close",
            "volume",
            "volume_currency",
            "volume_quote",
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
    for column in frame.columns:
        frame[column] = pd.to_numeric(frame[column], errors="coerce")
    frame = frame.dropna(subset=["open", "high", "low", "close"])
    frame["volume"] = frame["volume"].fillna(0)
    frame.index.name = "timestamp"
    return _drop_incomplete_bars(frame, timeframe).tail(180)


def _fetch_yahoo_candles(symbol: str, timeframe: str) -> pd.DataFrame:
    instrument = _ensure_supported(symbol)
    intervals = {
        "5m": ("5m", "5d"),
        "15m": ("15m", "60d"),
        "1h": ("60m", "1mo"),
        "4h": ("60m", "1mo"),
    }
    interval, period = intervals[timeframe]
    ticker = quote(instrument["provider_symbol"], safe="")
    query = urlencode({"interval": interval, "range": period, "events": "div,splits"})
    url = f"https://query1.finance.yahoo.com/v8/finance/chart/{ticker}?{query}"
    response = requests.get(
        url,
        headers={"User-Agent": USER_AGENT},
        timeout=15,
    )
    response.raise_for_status()
    payload = response.json().get("chart", {})
    if payload.get("error"):
        raise RuntimeError("Yahoo Finance informó un error al consultar el símbolo.")
    result_list = payload.get("result") or []
    if not result_list:
        raise RuntimeError("Yahoo Finance no devolvió velas para este instrumento.")

    result = result_list[0]
    timestamps = result.get("timestamp") or []
    quote_data = (
        result.get("indicators", {})
        .get("quote", [{}])[0]
    )
    if not timestamps:
        raise RuntimeError("Yahoo Finance no devolvió horarios de cotización.")

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
    for column in frame.columns:
        frame[column] = pd.to_numeric(frame[column], errors="coerce")
    frame = frame.dropna(subset=["open", "high", "low", "close"])
    frame = frame.loc[~frame.index.duplicated(keep="last")].sort_index()
    frame["volume"] = frame["volume"].fillna(0)

    if timeframe == "4h":
        frame = _drop_incomplete_bars(frame, "1h")
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
        frame = _drop_incomplete_bars(frame, "4h")
    else:
        frame = _drop_incomplete_bars(frame, timeframe)

    frame.index.name = "timestamp"
    return frame.tail(180)


@st.cache_data(ttl=300, show_spinner=False)
def fetch_candles(symbol: str, timeframe: str) -> pd.DataFrame:
    """Carga velas cerradas de un proveedor público y las guarda cinco minutos."""
    instrument = _ensure_supported(symbol)
    if timeframe not in TIMEFRAME_MINUTES:
        raise ValueError("Temporalidad no soportada.")
    if instrument["provider"] == "okx":
        frame = _fetch_okx_candles(symbol, timeframe)
    else:
        frame = _fetch_yahoo_candles(symbol, timeframe)
    if len(frame) < 30:
        raise RuntimeError(
            f"El proveedor solo entregó {len(frame)} velas cerradas; "
            "no hay suficiente historial para calcular el contexto."
        )
    return frame


def _volume_weights(frame: pd.DataFrame) -> tuple[np.ndarray, bool]:
    volume = frame["volume"].fillna(0).to_numpy(dtype=float)
    approximate = bool(np.any(volume <= 0))
    if np.sum(volume) <= 0:
        volume = np.maximum(
            (frame["high"] - frame["low"]).to_numpy(dtype=float),
            1e-9,
        )
        approximate = True
    return volume, approximate


def _calculate_profile(frame: pd.DataFrame, bins: int = 18) -> dict[str, Any]:
    selected = frame.iloc[:-3].tail(36)
    if len(selected) < 6:
        selected = frame.tail(min(36, len(frame)))
    current_price = float(frame["close"].iloc[-1])
    low = float(selected["low"].min())
    high = float(selected["high"].max())
    if not np.isfinite(low) or not np.isfinite(high) or high <= low:
        return {
            "poc": current_price,
            "vah": current_price,
            "val": current_price,
            "state": "balance",
            "approximate": True,
        }

    width = (high - low) / bins
    weights, _ = _volume_weights(selected)
    typical = (
        selected["high"].to_numpy(dtype=float)
        + selected["low"].to_numpy(dtype=float)
        + selected["close"].to_numpy(dtype=float)
    ) / 3
    indexes = np.clip(((typical - low) / width).astype(int), 0, bins - 1)
    distribution = np.bincount(indexes, weights=weights, minlength=bins)
    poc_index = int(np.argmax(distribution))
    total = float(distribution.sum())
    accumulated = float(distribution[poc_index])
    low_index = high_index = poc_index
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
        if current_price > vah
        else "out-of-balance-down"
        if current_price < val
        else "balance"
    )
    return {
        "poc": low + (poc_index + 0.5) * width,
        "vah": vah,
        "val": val,
        "state": state,
        "approximate": True,
    }


def _calculate_lvns(frame: pd.DataFrame, bins: int = 12) -> list[float]:
    selected = frame.tail(18)
    if len(selected) < 6:
        return []
    low = float(selected["low"].min())
    high = float(selected["high"].max())
    if high <= low:
        return []

    width = (high - low) / bins
    weights, _ = _volume_weights(selected)
    distribution = np.zeros(bins, dtype=float)
    for position, (_, row) in enumerate(selected.iterrows()):
        start = int(np.clip((float(row["low"]) - low) / width, 0, bins - 1))
        end = int(np.clip((float(row["high"]) - low) / width, 0, bins - 1))
        share = weights[position] / max(end - start + 1, 1)
        distribution[start : end + 1] += share

    average = float(distribution.mean())
    if average <= 0:
        return []
    levels: list[float] = []
    for index in range(1, bins - 1):
        if (
            distribution[index] <= distribution[index - 1]
            and distribution[index] <= distribution[index + 1]
            and distribution[index] < average * 0.6
        ):
            levels.append(low + (index + 0.5) * width)
    return levels[:6]


def _detect_order_block(frame: pd.DataFrame) -> dict[str, Any] | None:
    first_index = max(2, len(frame) - 10)
    for index in range(len(frame) - 2, first_index - 1, -1):
        base = frame.iloc[index]
        impulse = frame.iloc[index + 1]
        body = abs(float(impulse["close"]) - float(impulse["open"]))
        prior = frame.iloc[max(0, index - 10) : index]
        average_range = (
            float((prior["high"] - prior["low"]).mean()) if len(prior) else 0
        )
        threshold = average_range * 0.8 if average_range > 0 else body * 0.8
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
                "timestamp": frame.index[index].isoformat(),
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
                "timestamp": frame.index[index].isoformat(),
            }
    return None


def _detect_fair_value_gap(frame: pd.DataFrame) -> dict[str, Any] | None:
    first_index = max(2, len(frame) - 7)
    for index in range(len(frame) - 1, first_index - 1, -1):
        first = frame.iloc[index - 2]
        third = frame.iloc[index]
        if third["low"] > first["high"]:
            return {
                "type": "FVG_ALCISTA",
                "low": float(first["high"]),
                "high": float(third["low"]),
                "timestamp": frame.index[index].isoformat(),
            }
        if third["high"] < first["low"]:
            return {
                "type": "FVG_BAJISTA",
                "low": float(third["high"]),
                "high": float(first["low"]),
                "timestamp": frame.index[index].isoformat(),
            }
    return None


def calculate_technicals(frame: pd.DataFrame) -> dict[str, Any]:
    candles = frame.copy()
    close = candles["close"].astype(float)
    high = candles["high"].astype(float)
    low = candles["low"].astype(float)
    volume = candles["volume"].fillna(0).astype(float)

    candles["ema9"] = close.ewm(span=9, adjust=False).mean()
    candles["ema21"] = close.ewm(span=21, adjust=False).mean()
    typical = (high + low + close) / 3
    rolling_volume = volume.rolling(24, min_periods=1).sum()
    rolling_weighted = (typical * volume).rolling(24, min_periods=1).sum()
    fallback_vwap = close.rolling(24, min_periods=1).mean()
    candles["vwap"] = (rolling_weighted / rolling_volume.replace(0, np.nan)).fillna(
        fallback_vwap
    )

    candle_range = (high - low).replace(0, np.nan)
    candle_delta = ((close - candles["open"]) / candle_range).fillna(0) * volume
    candles["cvd_proxy"] = candle_delta.cumsum()

    previous_close = close.shift(1)
    true_range = pd.concat(
        [
            high - low,
            (high - previous_close).abs(),
            (low - previous_close).abs(),
        ],
        axis=1,
    ).max(axis=1)
    candles["atr"] = true_range.rolling(14, min_periods=1).mean()

    up_move = high.diff()
    down_move = -low.diff()
    positive_dm = pd.Series(
        np.where((up_move > down_move) & (up_move > 0), up_move, 0),
        index=candles.index,
        dtype=float,
    )
    negative_dm = pd.Series(
        np.where((down_move > up_move) & (down_move > 0), down_move, 0),
        index=candles.index,
        dtype=float,
    )
    smoothed_tr = true_range.ewm(alpha=1 / 14, adjust=False, min_periods=14).mean()
    plus_di = (
        100 * positive_dm.ewm(alpha=1 / 14, adjust=False, min_periods=14).mean()
        / smoothed_tr.replace(0, np.nan)
    )
    minus_di = (
        100 * negative_dm.ewm(alpha=1 / 14, adjust=False, min_periods=14).mean()
        / smoothed_tr.replace(0, np.nan)
    )
    denominator = (plus_di + minus_di).replace(0, np.nan)
    dx = (100 * (plus_di - minus_di).abs() / denominator).fillna(0)
    candles["adx"] = dx.ewm(alpha=1 / 14, adjust=False, min_periods=1).mean()

    volume_weights, volume_approximate = _volume_weights(frame)
    del volume_weights
    return {
        "candles": candles,
        "atr": float(candles["atr"].iloc[-1]),
        "adx": float(candles["adx"].iloc[-1]),
        "profile": _calculate_profile(frame),
        "lvns": _calculate_lvns(frame),
        "order_block": _detect_order_block(frame),
        "fair_value_gap": _detect_fair_value_gap(frame),
        "cvd_proxy": float(candles["cvd_proxy"].iloc[-1]),
        "volume_approximate": volume_approximate,
    }


def _bias(technicals: dict[str, Any], require_adx: bool = False) -> str:
    last = technicals["candles"].iloc[-1]
    adx_ok = not require_adx or technicals["adx"] >= 22
    if last["close"] > last["vwap"] and last["ema9"] > last["ema21"] and adx_ok:
        return "Alcista"
    if last["close"] < last["vwap"] and last["ema9"] < last["ema21"] and adx_ok:
        return "Bajista"
    return "Neutral"


def _four_hour_bias(technicals: dict[str, Any]) -> str:
    last = technicals["candles"].iloc[-1]
    if last["ema9"] > last["ema21"]:
        return "Alcista"
    if last["ema9"] < last["ema21"]:
        return "Bajista"
    return "Neutral"


def _near_zone(price: float, zone: dict[str, Any] | None, tolerance: float) -> bool:
    return bool(
        zone
        and price >= float(zone["low"]) - tolerance
        and price <= float(zone["high"]) + tolerance
    )


def _impulse_retracements(candles: pd.DataFrame) -> tuple[float, float, float, float]:
    recent = candles.tail(18)
    high = float(recent["high"].max())
    low = float(recent["low"].min())
    price = float(candles["close"].iloc[-1])
    width = max(high - low, 1e-12)
    long_discount = (high - price) / width * 100
    short_discount = (price - low) / width * 100
    return high, low, long_discount, short_discount


def _evaluate_signal(
    symbol: str,
    data: dict[str, Any],
    context_1h: dict[str, Any],
    context_4h: dict[str, Any],
    stale: bool,
) -> dict[str, Any] | None:
    candles = data["candles"]
    if stale or len(candles) < 30:
        return None

    instrument = _ensure_supported(symbol)
    last = candles.iloc[-1]
    previous = candles.iloc[-2]
    price = float(last["close"])
    high, low, long_discount, short_discount = _impulse_retracements(candles)
    profile = data["profile"]
    poc, vah, val = profile["poc"], profile["vah"], profile["val"]
    atr = max(float(data["atr"]), 0)
    tolerance = max(atr * 0.35, price * 0.00001)
    bias_1h = _bias(context_1h, require_adx=True)
    bias_4h = _four_hour_bias(context_4h)
    adx_minimum = {"crypto": 28, "forex": 25, "metal": 30, "index": 25}[
        instrument["class"]
    ]
    bullish = float(last["close"]) > float(last["open"])
    bearish = float(last["close"]) < float(last["open"])
    order_block = data["order_block"]
    fair_value_gap = data["fair_value_gap"]
    lvn = next(
        (
            level
            for level in data["lvns"]
            if abs(level - price) <= max(atr * 0.5, 1e-12)
        ),
        None,
    )

    local_bullish_zone = (
        order_block is not None
        and order_block["type"] == "OB_ALCISTA"
        and _near_zone(price, order_block, tolerance)
    ) or (
        fair_value_gap is not None
        and fair_value_gap["type"] == "FVG_ALCISTA"
        and _near_zone(price, fair_value_gap, tolerance)
    )
    local_bearish_zone = (
        order_block is not None
        and order_block["type"] == "OB_BAJISTA"
        and _near_zone(price, order_block, tolerance)
    ) or (
        fair_value_gap is not None
        and fair_value_gap["type"] == "FVG_BAJISTA"
        and _near_zone(price, fair_value_gap, tolerance)
    )
    recent_volumes = candles["volume"].tail(20)
    average_volume = float(recent_volumes.mean()) if len(recent_volumes) else 0
    volume_expansion = average_volume > 0 and float(last["volume"]) > average_volume * 1.15

    def make_signal(
        *,
        code: str,
        model: str,
        direction: str,
        zone_price: float,
        target: float,
        discount: float,
        higher_timeframe_zone: bool,
        near_lvn: bool,
        notes: list[str],
    ) -> dict[str, Any]:
        buffer = max(atr * 0.35, price * 0.00001)
        if direction == "LONG":
            stop = min(zone_price - buffer, price - buffer)
            risk = price - stop
            take_profit = max(target, price + risk * 2.5)
        else:
            stop = max(zone_price + buffer, price + buffer)
            risk = stop - price
            take_profit = min(target, price - risk * 2.5)
        reward = abs(take_profit - price)
        quality = (
            int(discount >= 60)
            + int(discount >= 80)
            + int(higher_timeframe_zone)
            + int(near_lvn)
            + int(volume_expansion)
        )
        return {
            "code": code,
            "model": model,
            "direction": direction,
            "quality": min(quality, 5),
            "entry": price,
            "stop_loss": stop,
            "take_profit": take_profit,
            "risk_reward": reward / risk if risk > 0 else None,
            "discount_percent": round(discount, 1),
            "notes": notes,
        }

    # Modelo 1: continuación fuera de balance con retroceso a una zona local.
    if (
        profile["state"] == "out-of-balance-up"
        and bias_1h == "Alcista"
        and data["adx"] >= adx_minimum
        and bias_4h != "Bajista"
        and long_discount >= 50
        and (local_bullish_zone or lvn is not None)
        and bullish
        and (price >= float(last["ema21"]) or price >= float(last["vwap"]))
    ):
        selected_zone = (
            order_block
            if order_block
            and order_block["type"] == "OB_ALCISTA"
            and _near_zone(price, order_block, tolerance)
            else fair_value_gap
            if fair_value_gap
            and fair_value_gap["type"] == "FVG_ALCISTA"
            and _near_zone(price, fair_value_gap, tolerance)
            else None
        )
        return make_signal(
            code="AMT_TREND_LONG",
            model="Tendencia fuera de balance",
            direction="LONG",
            zone_price=float(selected_zone["low"]) if selected_zone else lvn or price - atr,
            target=high,
            discount=long_discount,
            higher_timeframe_zone=bias_4h == "Alcista",
            near_lvn=lvn is not None,
            notes=[
                "Precio sobre el área de valor previa con sesgo 1H alineado.",
                "Zona y retroceso calculados con velas OHLCV cerradas.",
            ],
        )

    if (
        profile["state"] == "out-of-balance-down"
        and bias_1h == "Bajista"
        and data["adx"] >= adx_minimum
        and bias_4h != "Alcista"
        and short_discount >= 50
        and (local_bearish_zone or lvn is not None)
        and bearish
        and (price <= float(last["ema21"]) or price <= float(last["vwap"]))
    ):
        selected_zone = (
            order_block
            if order_block
            and order_block["type"] == "OB_BAJISTA"
            and _near_zone(price, order_block, tolerance)
            else fair_value_gap
            if fair_value_gap
            and fair_value_gap["type"] == "FVG_BAJISTA"
            and _near_zone(price, fair_value_gap, tolerance)
            else None
        )
        return make_signal(
            code="AMT_TREND_SHORT",
            model="Tendencia fuera de balance",
            direction="SHORT",
            zone_price=float(selected_zone["high"]) if selected_zone else lvn or price + atr,
            target=low,
            discount=short_discount,
            higher_timeframe_zone=bias_4h == "Bajista",
            near_lvn=lvn is not None,
            notes=[
                "Precio bajo el área de valor previa con sesgo 1H alineado.",
                "Zona y retroceso calculados con velas OHLCV cerradas.",
            ],
        )

    # Modelo 2: ruptura fallida del área de valor y retorno al POC.
    fakeout_above = (
        (float(previous["high"]) > vah or float(last["high"]) > vah)
        and price < vah
        and bearish
        and poc < price
    )
    if profile["state"] == "balance" and fakeout_above and bias_1h != "Alcista":
        fakeout_high = max(float(previous["high"]), float(last["high"]))
        stop = fakeout_high + max(atr * 0.25, price * 0.00001)
        risk = stop - price
        if risk > 0 and price - poc >= risk * 1.3:
            return {
                "code": "AMT_MEAN_REVERSION_SHORT",
                "model": "Reversión al POC tras ruptura fallida",
                "direction": "SHORT",
                "quality": 3,
                "entry": price,
                "stop_loss": stop,
                "take_profit": poc,
                "risk_reward": (price - poc) / risk,
                "discount_percent": 0,
                "notes": [
                    "Ruptura fallida sobre VAH; objetivo técnico en el POC.",
                    "Regla heurística sin probabilidad validada.",
                ],
            }

    fakeout_below = (
        (float(previous["low"]) < val or float(last["low"]) < val)
        and price > val
        and bullish
        and poc > price
    )
    if profile["state"] == "balance" and fakeout_below and bias_1h != "Bajista":
        fakeout_low = min(float(previous["low"]), float(last["low"]))
        stop = fakeout_low - max(atr * 0.25, price * 0.00001)
        risk = price - stop
        if risk > 0 and poc - price >= risk * 1.3:
            return {
                "code": "AMT_MEAN_REVERSION_LONG",
                "model": "Reversión al POC tras ruptura fallida",
                "direction": "LONG",
                "quality": 3,
                "entry": price,
                "stop_loss": stop,
                "take_profit": poc,
                "risk_reward": (poc - price) / risk,
                "discount_percent": 0,
                "notes": [
                    "Ruptura fallida bajo VAL; objetivo técnico en el POC.",
                    "Regla heurística sin probabilidad validada.",
                ],
            }

    # Modelo 3: zona 4H con confirmación de order block local.
    four_hour_zones = [
        zone
        for zone in (context_4h["order_block"], context_4h["fair_value_gap"])
        if zone is not None
    ]
    four_hour_tolerance = max(float(context_4h["atr"]) * 0.25, tolerance)
    bullish_4h_zone = next(
        (
            zone
            for zone in four_hour_zones
            if zone["type"].endswith("ALCISTA")
            and _near_zone(price, zone, four_hour_tolerance)
        ),
        None,
    )
    if (
        bias_4h == "Alcista"
        and bullish_4h_zone
        and order_block
        and order_block["type"] == "OB_ALCISTA"
        and _near_zone(price, order_block, tolerance)
        and bullish
        and long_discount >= 50
    ):
        return make_signal(
            code="SENSEI_FRACTAL_LONG",
            model="Fractal 4H con confirmación de temporalidad baja",
            direction="LONG",
            zone_price=float(order_block["low"]),
            target=high,
            discount=long_discount,
            higher_timeframe_zone=True,
            near_lvn=lvn is not None,
            notes=[
                "Sesgo 4H alcista y precio dentro de una zona 4H detectada.",
                "El order block local usa velas cerradas.",
            ],
        )

    bearish_4h_zone = next(
        (
            zone
            for zone in four_hour_zones
            if zone["type"].endswith("BAJISTA")
            and _near_zone(price, zone, four_hour_tolerance)
        ),
        None,
    )
    if (
        bias_4h == "Bajista"
        and bearish_4h_zone
        and order_block
        and order_block["type"] == "OB_BAJISTA"
        and _near_zone(price, order_block, tolerance)
        and bearish
        and short_discount >= 50
    ):
        return make_signal(
            code="SENSEI_FRACTAL_SHORT",
            model="Fractal 4H con confirmación de temporalidad baja",
            direction="SHORT",
            zone_price=float(order_block["high"]),
            target=low,
            discount=short_discount,
            higher_timeframe_zone=True,
            near_lvn=lvn is not None,
            notes=[
                "Sesgo 4H bajista y precio dentro de una zona 4H detectada.",
                "El order block local usa velas cerradas.",
            ],
        )
    return None


def _is_stale(frame: pd.DataFrame, timeframe: str) -> bool:
    if frame.empty:
        return True
    age_minutes = (
        pd.Timestamp.now(tz="UTC") - frame.index[-1]
    ).total_seconds() / 60
    return age_minutes > STALE_AFTER_MINUTES[timeframe]


def build_analysis(symbol: str, timeframe: str) -> dict[str, Any]:
    _ensure_supported(symbol)
    if timeframe not in ("5m", "15m", "1h"):
        raise ValueError("Selecciona una temporalidad de gráfica válida.")
    evaluation_timeframe = "15m" if timeframe == "1h" else timeframe

    chart_frame = fetch_candles(symbol, timeframe)
    hourly_frame = chart_frame if timeframe == "1h" else fetch_candles(symbol, "1h")
    four_hour_frame = fetch_candles(symbol, "4h")
    evaluation_frame = (
        chart_frame
        if evaluation_timeframe == timeframe
        else fetch_candles(symbol, evaluation_timeframe)
    )

    chart_technicals = calculate_technicals(chart_frame)
    hourly_technicals = (
        chart_technicals
        if timeframe == "1h"
        else calculate_technicals(hourly_frame)
    )
    four_hour_technicals = calculate_technicals(four_hour_frame)
    evaluation_technicals = (
        chart_technicals
        if evaluation_timeframe == timeframe
        else calculate_technicals(evaluation_frame)
    )
    stale = _is_stale(chart_frame, timeframe)
    signal_stale = stale or _is_stale(evaluation_frame, evaluation_timeframe)
    signal = _evaluate_signal(
        symbol,
        evaluation_technicals,
        hourly_technicals,
        four_hour_technicals,
        signal_stale,
    )

    latest = chart_technicals["candles"].iloc[-1]
    previous = chart_technicals["candles"].iloc[-2]
    change_percent = (
        (float(latest["close"]) - float(previous["close"]))
        / float(previous["close"])
        * 100
        if float(previous["close"]) != 0
        else 0
    )
    bias_1h = _bias(hourly_technicals, require_adx=True)
    bias_4h = _four_hour_bias(four_hour_technicals)
    _, _, long_discount, short_discount = _impulse_retracements(
        evaluation_technicals["candles"]
    )
    selected_discount = (
        short_discount
        if bias_1h == "Bajista" or (bias_1h == "Neutral" and bias_4h == "Bajista")
        else long_discount
    )
    notes = [
        "POC, VAH, VAL y LVN se estiman con velas OHLCV; no son volumen negociado por nivel.",
        "El CVD es un proxy por dirección de vela; no representa flujo bid/ask real.",
        "Las reglas no están validadas con backtesting y no son recomendaciones de inversión.",
    ]
    if chart_technicals["volume_approximate"]:
        notes.append(
            "El proveedor reporta volumen cero o incompleto; los indicadores de volumen son aproximados."
        )
    if timeframe == "1h":
        notes.append(
            "La gráfica usa 1H y la evaluación de señal usa 15m, como en la configuración original."
        )
    if stale:
        notes.append(
            "La última vela está retrasada; no se presenta una señal con datos desactualizados."
        )

    return {
        "symbol": symbol,
        "instrument": INSTRUMENTS[symbol],
        "timeframe": timeframe,
        "evaluation_timeframe": evaluation_timeframe,
        "source": INSTRUMENTS[symbol]["source"],
        "chart_frame": chart_technicals["candles"],
        "chart_technicals": chart_technicals,
        "evaluation_technicals": evaluation_technicals,
        "bias_1h": bias_1h,
        "bias_4h": bias_4h,
        "stale": stale,
        "signal": signal,
        "current_price": float(latest["close"]),
        "change_percent": change_percent,
        "as_of": chart_frame.index[-1],
        "market_state": evaluation_technicals["profile"]["state"],
        "adx": evaluation_technicals["adx"],
        "discount_percent": round(selected_discount, 1),
        "data_notes": notes,
    }


@st.cache_data(ttl=300, show_spinner=False)
def fetch_news(topic: str, limit: int = 12) -> list[dict[str, Any]]:
    if topic not in NEWS_TOPICS:
        raise ValueError("Tema de noticias no válido.")
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
            "Accept": "application/rss+xml, application/xml, text/xml",
        },
        timeout=15,
    )
    response.raise_for_status()
    root = ET.fromstring(response.content)
    news_items: list[dict[str, Any]] = []
    seen_urls: set[str] = set()
    fallback_scope = (
        ["mercados globales"]
        if topic == "Todos los temas"
        else ["cripto"]
        if topic == "Cripto"
        else ["energía", "materias primas", "divisas"]
        if topic == "Energía y materias primas"
        else ["oro", "energía", "divisas", "índices"]
        if topic == "Geopolítica"
        else ["divisas", "bonos", "índices", "oro"]
    )

    for item in root.findall("./channel/item"):
        title = (item.findtext("title") or "").strip()
        url = (item.findtext("link") or "").strip()
        source = item.findtext("source") or "Fuente enlazada"
        published_raw = item.findtext("pubDate") or ""
        if not title or not url or not url.startswith("https://") or url in seen_urls:
            continue
        seen_urls.add(url)

        published_at = None
        if published_raw:
            try:
                published_at = parsedate_to_datetime(published_raw)
                if published_at.tzinfo is None:
                    published_at = published_at.replace(tzinfo=timezone.utc)
                published_at = published_at.astimezone(timezone.utc)
            except (TypeError, ValueError, OverflowError):
                published_at = None

        matches = [
            (category, markets)
            for category, pattern, markets in HEADLINE_RULES
            if pattern.search(title)
        ]
        if matches:
            category = matches[0][0]
            market_scope = list(
                dict.fromkeys(
                    market
                    for _, markets in matches
                    for market in markets
                )
            )
        else:
            category = topic if topic != "Todos los temas" else "Mercados globales"
            market_scope = fallback_scope

        news_items.append(
            {
                "title": title[:360],
                "url": url,
                "source": source[:120],
                "published_at": published_at,
                "category": category,
                "market_scope": market_scope,
            }
        )

    news_items.sort(
        key=lambda item: item["published_at"] or datetime.min.replace(tzinfo=timezone.utc),
        reverse=True,
    )
    return news_items[: min(max(limit, 5), 30)]


@st.cache_data(ttl=300, show_spinner=False)
def fetch_watchlist() -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for symbol in WATCHLIST:
        instrument = INSTRUMENTS[symb **…**

_This response is too long to display in full._