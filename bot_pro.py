import logging
import threading
import time
from datetime import datetime

import ccxt
import numpy as np
import pandas as pd
import plotly.graph_objects as go
from plotly.subplots import make_subplots
import requests
import streamlit as st
import yfinance as yf

# ==========================================
# 🧾 LOGGING PROFESIONAL
# ==========================================
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
logger = logging.getLogger("QuantTerminal")

# ==========================================
# ⚙️ CONFIGURACIÓN GLOBAL & CONTROL DE RIESGO
# ==========================================
TELEGRAM_TOKEN = "9148434925:AAH_AuswS-GGPHadFBwcrDOUUIwBFbXvtDs"
TELEGRAM_CHAT_ID = "1496321437"

SALDO_CUENTA_USD = 10.0
RIESGO_MAX_PCT_CUENTA = 10.0        # Máx. % del saldo en 0.01 lotes (~$1.00 USD en cuenta de $10)
DESCUENTO_MIN_FIBO = 50.0           # Descuento Fibonacci mínimo exigido en tendencia
FILTRAR_SOLO_RIESGO_APTO_TG = True  # Evita enviar señales con SL excesivo para la cuenta
COOLDOWN_ALERTA_SEG = 1200          # 20 min entre alertas repetidas del mismo activo/señal
INTERVALO_CICLO_SEG = 30            # Frecuencia del barrido en segundo plano
TTL_FALLO_SEG = 25                  # Evita reintentar sin pausa un proveedor que acaba de fallar

st.set_page_config(
    page_title="Institutional Quant Terminal — Pro Architecture",
    layout="wide",
    page_icon="🏦",
)

CRYPTO_MARKETS = ["BTC/USDT", "ETH/USDT", "BNB/USDT"]
FOREX_MARKETS = [
    "EURUSD=X", "GBPUSD=X", "USDJPY=X", "USDCHF=X", "AUDUSD=X",
    "USDCAD=X", "NZDUSD=X", "EURJPY=X", "GBPJPY=X", "EURGBP=X",
    "XAUUSD=X",
]
NASDAQ_MARKETS = []
ALL_TRACKED_SYMBOLS = list(dict.fromkeys(CRYPTO_MARKETS + FOREX_MARKETS))

MAPA_CANONICO = {
    "XAUUSD": "XAUUSD",
    "XAUUSD=X": "XAUUSD",
    "GC=F": "XAUUSD",
}


def canonizar_simbolo(symbol: str) -> str:
    s = str(symbol or "").strip().upper()
    return MAPA_CANONICO.get(s, s)


def clasificar_mercado(symbol: str) -> str:
    canon = canonizar_simbolo(symbol)
    if symbol in CRYPTO_MARKETS or "/" in canon:
        return "CRYPTO"
    if canon == "XAUUSD":
        return "METAL"
    return "FOREX"


def tamano_pip(symbol: str) -> float:
    canon = canonizar_simbolo(symbol)
    if canon == "XAUUSD":
        return 0.10
    if "JPY" in canon:
        return 0.01
    return 0.0001


def obtener_umbral_adx(symbol: str) -> float:
    canon = canonizar_simbolo(symbol)
    if canon == "XAUUSD":
        return 35.0
    if clasificar_mercado(symbol) == "CRYPTO":
        return 35.0
    return 30.0


def _crear_exchange(ex_id: str):
    try:
        cls = getattr(ccxt, ex_id)
        return cls({"enableRateLimit": True, "timeout": 6000})
    except Exception as exc:
        logger.warning("No se pudo inicializar exchange %s: %s", ex_id, exc)
        return None


EXCHANGES_CRYPTO = [
    ex for ex in (
        _crear_exchange("okx"),
        _crear_exchange("binance"),
        _crear_exchange("kucoin"),
        _crear_exchange("bybit"),
    ) if ex is not None
]

STATE_LOCK = threading.Lock()
ALERTAS_ENVIADAS_GLOBAL: dict[str, float] = {}
CACHE_DATOS_GLOBAL: dict[str, dict] = {}
TELEMETRIA_MOTOR = {
    "estado": "Listo para escanear...",
    "ultimo_ciclo": "Pendiente",
    "activos_reales": 0,
    "candidatas": 0,
    "bloqueadas_riesgo": 0,
    "alertas_enviadas_total": 0,
    "ultimo_error_tg": "",
    "latido_hilo": 0.0,
    "ciclo_en_marcha": False,
}


def _actualizar_telemetria(**kwargs):
    with STATE_LOCK:
        TELEMETRIA_MOTOR.update(kwargs)


def _leer_telemetria() -> dict:
    with STATE_LOCK:
        return dict(TELEMETRIA_MOTOR)


# ==========================================
# 📩 NOTIFICACIONES TELEGRAM CON DIAGNÓSTICO
# ==========================================
def enviar_telegram(mensaje: str) -> bool:
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    payload = {
        "chat_id": TELEGRAM_CHAT_ID,
        "text": mensaje,
        "parse_mode": "Markdown",
    }
    try:
        resp = requests.post(url, json=payload, timeout=8)
        if resp.status_code == 200:
            _actualizar_telemetria(ultimo_error_tg="")
            return True
        err = f"HTTP {resp.status_code}: {resp.text[:140]}"
        _actualizar_telemetria(ultimo_error_tg=err)
        logger.error("Fallo Telegram: %s", err)
        return False
    except Exception as exc:
        err = str(exc)[:140]
        _actualizar_telemetria(ultimo_error_tg=err)
        logger.error("Excepción Telegram: %s", err)
        return False


# ==========================================
# 📐 CÁLCULO DE RIESGO EN 0.01 LOTES
# ==========================================
def _obtener_fx_a_usd(quote_ccy: str) -> float:
    if quote_ccy == "USD":
        return 1.0
    try:
        if quote_ccy == "JPY":
            df_jpy = obtener_datos("USDJPY=X", "1h", limit=5, ttl_segundos=120)
            if not df_jpy.empty and df_jpy.attrs.get("es_real", False):
                px = float(df_jpy["close"].iloc[-1])
                if px > 0:
                    return 1.0 / px
            return 1.0 / 155.0
        if quote_ccy == "CHF":
            df_chf = obtener_datos("USDCHF=X", "1h", limit=5, ttl_segundos=120)
            if not df_chf.empty and df_chf.attrs.get("es_real", False):
                px = float(df_chf["close"].iloc[-1])
                if px > 0:
                    return 1.0 / px
            return 1.12
        if quote_ccy == "CAD":
            df_cad = obtener_datos("USDCAD=X", "1h", limit=5, ttl_segundos=120)
            if not df_cad.empty and df_cad.attrs.get("es_real", False):
                px = float(df_cad["close"].iloc[-1])
                if px > 0:
                    return 1.0 / px
            return 0.72
        if quote_ccy == "GBP":
            df_gbp = obtener_datos("GBPUSD=X", "1h", limit=5, ttl_segundos=120)
            if not df_gbp.empty and df_gbp.attrs.get("es_real", False):
                return float(df_gbp["close"].iloc[-1])
            return 1.30
    except Exception:
        pass
    return 1.0


def calcular_riesgo_001_lotes(symbol: str, precio: float, sl: float, saldo_usd: float = SALDO_CUENTA_USD):
    canon = canonizar_simbolo(symbol).replace("=X", "")
    mercado = clasificar_mercado(symbol)
    dist = abs(float(precio) - float(sl))
    if dist <= 0 or float(precio) <= 0:
        return 0.0, 0.0, 0.0

    pip = tamano_pip(symbol)
    pips_o_pts = dist / pip

    if mercado == "CRYPTO":
        riesgo_usd = dist * 0.01
    elif canon == "XAUUSD":
        riesgo_usd = dist * 1.0
    elif len(canon) == 6:
        quote_ccy = canon[3:]
        valor_en_quote = dist * 1000.0
        riesgo_usd = valor_en_quote * _obtener_fx_a_usd(quote_ccy)
    else:
        riesgo_usd = pips_o_pts * 0.10

    riesgo_usd = round(float(riesgo_usd), 2)
    riesgo_pct = round((riesgo_usd / max(float(saldo_usd), 0.01)) * 100.0, 1)
    return round(float(pips_o_pts), 1), riesgo_usd, riesgo_pct


# ==========================================
# 🧠 INDICADORES, AMT Y MODELO UNIFICADO
# ==========================================
def _normalizar_timestamp(serie):
    ts = pd.to_datetime(serie)
    if getattr(ts.dt, "tz", None) is not None:
        ts = ts.dt.tz_convert(None)
    return ts


def obtener_sl_minimo(symbol: str, timeframe: str, precio: float) -> float:
    mercado = clasificar_mercado(symbol)
    pip = tamano_pip(symbol)
    if mercado == "FOREX":
        pips_min = {"5m": 10.0, "15m": 12.0, "1h": 18.0}.get(timeframe, 12.0)
        return pips_min * pip
    if mercado == "METAL":
        pts_min = {"5m": 6.0, "15m": 8.0, "1h": 14.0}.get(timeframe, 8.0)
        return pts_min
    pct_min = {"5m": 0.0045, "15m": 0.0075, "1h": 0.0120}.get(timeframe, 0.0075)
    return float(precio) * pct_min


def calcular_vwap(df: pd.DataFrame) -> pd.Series:
    if df.empty:
        return pd.Series(dtype=float)
    if "volume" not in df.columns or float(df["volume"].sum()) <= 0:
        return df["close"].rolling(20, min_periods=1).mean()
    tp = (df["high"] + df["low"] + df["close"]) / 3.0
    vol_roll = df["volume"].rolling(24, min_periods=1).sum().replace(0, np.nan)
    vwap = (tp * df["volume"]).rolling(24, min_periods=1).sum() / vol_roll
    return vwap.fillna(df["close"].rolling(20, min_periods=1).mean())


def calcular_adx(df: pd.DataFrame, period: int = 14) -> float:
    if len(df) < period + 2:
        return 25.0
    try:
        d = df.copy()
        d["up_move"] = d["high"] - d["high"].shift()
        d["down_move"] = d["low"].shift() - d["low"]
        d["plus_dm"] = np.where((d["up_move"] > d["down_move"]) & (d["up_move"] > 0), d["up_move"], 0.0)
        d["minus_dm"] = np.where((d["down_move"] > d["up_move"]) & (d["down_move"] > 0), d["down_move"], 0.0)
        d["tr"] = np.maximum(
            d["high"] - d["low"],
            np.maximum(abs(d["high"] - d["close"].shift()), abs(d["low"] - d["close"].shift())),
        )
        d["atr"] = d["tr"].ewm(span=period, adjust=False).mean()
        plus_di = 100.0 * (d["plus_dm"].ewm(span=period, adjust=False).mean() / d["atr"].replace(0, np.nan))
        minus_di = 100.0 * (d["minus_dm"].ewm(span=period, adjust=False).mean() / d["atr"].replace(0, np.nan))
        dx = 100.0 * (abs(plus_di - minus_di) / (plus_di + minus_di).replace(0, np.nan))
        adx_series = dx.ewm(span=period, adjust=False).mean()
        val = adx_series.iloc[-1]
        return 25.0 if pd.isna(val) else float(val)
    except Exception:
        return 25.0


def calcular_cvd_proxy(df: pd.DataFrame) -> pd.Series:
    rango = (df["high"] - df["low"]).replace(0, np.nan)
    delta_ratio = ((df["close"] - df["open"]) / rango).fillna(0.0)
    delta_vol = df["volume"] * delta_ratio
    return delta_vol.cumsum()


def detectar_exhaustacion(df: pd.DataFrame) -> bool:
    if len(df) < 22:
        return False
    rango_actual = float(df["high"].iloc[-1] - df["low"].iloc[-1])
    rangos_previos = df["high"].iloc[-21:-1] - df["low"].iloc[-21:-1]
    max_previo = float(rangos_previos.max())
    mov_previo = abs(float(df["close"].iloc[-2] - df["close"].iloc[-10]))
    atr_aprox = float(rangos_previos.mean())
    return (rango_actual > max_previo * 1.15) and (mov_previo > atr_aprox * 3.0)


def calcular_perfil_volumen_amt(df: pd.DataFrame, lookback: int = 36, bins: int = 18) -> dict:
    sub = df.iloc[-lookback:].copy() if len(df) >= lookback else df.copy()
    p_min = float(sub["low"].min())
    p_max = float(sub["high"].max())
    precio_actual = float(sub["close"].iloc[-1])

    if p_max <= p_min:
        return {"poc": precio_actual, "vah": precio_actual, "val": precio_actual, "estado": "BALANCE"}

    sub_prev = sub.iloc[:-3] if len(sub) > 12 else sub
    prev_min = float(sub_prev["low"].min())
    prev_max = float(sub_prev["high"].max())
    if prev_max <= prev_min:
        prev_min, prev_max = p_min, p_max

    edges = np.linspace(prev_min, prev_max, bins + 1)
    centers = (edges[:-1] + edges[1:]) / 2.0
    vol_bins = np.zeros(bins, dtype=float)

    tiene_vol = ("volume" in sub_prev.columns) and (float(sub_prev["volume"].sum()) > 0)
    for _, row in sub_prev.iterrows():
        tp = (float(row["high"]) + float(row["low"]) + float(row["close"])) / 3.0
        peso = float(row["volume"]) if tiene_vol else (float(row["high"]) - float(row["low"]) + 1e-9)
        idx = int(np.clip(np.digitize(tp, edges) - 1, 0, bins - 1))
        vol_bins[idx] += max(peso, 1e-9)

    poc_idx = int(np.argmax(vol_bins))
    poc = float(centers[poc_idx])

    total_vol = float(vol_bins.sum())
    acum = float(vol_bins[poc_idx])
    low_i = poc_idx
    high_i = poc_idx
    while acum < total_vol * 0.70 and (low_i > 0 or high_i < bins - 1):
        v_low = vol_bins[low_i - 1] if low_i > 0 else -1.0
        v_high = vol_bins[high_i + 1] if high_i < bins - 1 else -1.0
        if v_high >= v_low and high_i < bins - 1:
            high_i += 1
            acum += v_high
        elif low_i > 0:
            low_i -= 1
            acum += v_low
        else:
            break

    val = float(edges[low_i])
    vah = float(edges[high_i + 1])

    if precio_actual > vah:
        estado = "OUT_OF_BALANCE_UP"
    elif precio_actual < val:
        estado = "OUT_OF_BALANCE_DOWN"
    else:
        estado = "BALANCE"

    return {"poc": poc, "vah": vah, "val": val, "estado": estado}


def detectar_fvg_3_velas(df: pd.DataFrame):
    if len(df) < 5:
        return None
    for idx in range(len(df) - 1, max(2, len(df) - 8), -1):
        v1_high = float(df["high"].iloc[idx - 2])
        v1_low = float(df["low"].iloc[idx - 2])
        v3_low = float(df["low"].iloc[idx])
        v3_high = float(df["high"].iloc[idx])

        if v3_low > v1_high:
            return {"tipo": "FVG_ALCISTA", "zona_low": v1_high, "zona_high": v3_low, "pos": idx}
        if v3_high < v1_low:
            return {"tipo": "FVG_BAJISTA", "zona_low": v3_high, "zona_high": v1_low, "pos": idx}
    return None


def detectar_order_block_institucional(df: pd.DataFrame):
    if len(df) < 12:
        return None
    for idx in range(len(df) - 2, max(2, len(df) - 10), -1):
        vela = df.iloc[idx]
        sig1 = df.iloc[idx + 1]
        cuerpo_impulso = abs(float(sig1["close"]) - float(sig1["open"]))
        rango_prev = (df["high"].iloc[max(0, idx - 10):idx] - df["low"].iloc[max(0, idx - 10):idx]).mean()
        atr_local = float(rango_prev) if pd.notna(rango_prev) and rango_prev > 0 else cuerpo_impulso

        if float(vela["close"]) < float(vela["open"]) and float(sig1["close"]) > float(sig1["open"]):
            if float(sig1["close"]) > float(vela["high"]) and cuerpo_impulso >= atr_local * 0.8:
                return {"tipo": "OB_ALCISTA", "low": float(vela["low"]), "high": float(vela["high"]), "pos": idx}

        if float(vela["close"]) > float(vela["open"]) and float(sig1["close"]) < float(sig1["open"]):
            if float(sig1["close"]) < float(vela["low"]) and cuerpo_impulso >= atr_local * 0.8:
                return {"tipo": "OB_BAJISTA", "low": float(vela["low"]), "high": float(vela["high"]), "pos": idx}
    return None


def analizar_contexto_macro_1h(df_1h: pd.DataFrame) -> dict:
    if df_1h is None or df_1h.empty or len(df_1h) < 30:
        return {"sesgo": "NEUTRAL", "amt": "BALANCE", "poc": 0.0, "vah": 0.0, "val": 0.0, "adx": 20.0}

    ultima = df_1h.iloc[-1]
    precio = float(ultima["close"])
    vwap = float(ultima.get("vwap", precio))
    ema9 = float(ultima.get("ema_fast", precio))
    ema21 = float(ultima.get("ema_slow", precio))
    adx = float(ultima.get("adx", 20.0))
    perfil = calcular_perfil_volumen_amt(df_1h, lookback=36)

    if precio > vwap and ema9 > ema21 and adx >= 22.0:
        sesgo = "COMPRA"
    elif precio < vwap and ema9 < ema21 and adx >= 22.0:
        sesgo = "VENTA"
    else:
        sesgo = "NEUTRAL"

    return {
        "sesgo": sesgo,
        "amt": perfil["estado"],
        "poc": perfil["poc"],
        "vah": perfil["vah"],
        "val": perfil["val"],
        "adx": adx,
    }


def evaluar_motor_unificado(
    df: pd.DataFrame,
    symbol: str,
    timeframe: str,
    ctx_1h: dict,
    saldo_usd: float = SALDO_CUENTA_USD,
    riesgo_max_pct: float = RIESGO_MAX_PCT_CUENTA,
):
    if df is None or df.empty or len(df) < 30:
        return None

    canon = canonizar_simbolo(symbol)
    if canon == "XAUUSD" and timeframe == "5m":
        return None

    if detectar_exhaustacion(df):
        return None

    mercado = clasificar_mercado(symbol)
    ultima = df.iloc[-1]
    anterior = df.iloc[-2]
    precio = float(ultima["close"])
    vwap_val = float(ultima.get("vwap", precio))
    ema_fast = float(ultima.get("ema_fast", precio))
    ema_slow = float(ultima.get("ema_slow", precio))
    adx_val = float(ultima.get("adx", 20.0))
    adx_min = obtener_umbral_adx(symbol)

    rangos_previos = df["high"].iloc[-21:-1] - df["low"].iloc[-21:-1]
    atr_ref = float(rangos_previos.mean()) if len(rangos_previos) > 0 and pd.notna(rangos_previos.mean()) else max(precio * 0.002, tamano_pip(symbol) * 8)

    perfil = calcular_perfil_volumen_amt(df, lookback=36)
    poc, vah, val, estado_amt = perfil["poc"], perfil["vah"], perfil["val"], perfil["estado"]
    ob = detectar_order_block_institucional(df)
    fvg = detectar_fvg_3_velas(df)

    vol_actual = float(ultima.get("volume", 0.0))
    vol_ma = float(ultima.get("vol_ma", 0.0))
    desbalance_vol = (vol_ma > 0) and (vol_actual > vol_ma * 1.25)
    cvd_actual = float(ultima.get("cvd", 0.0))
    cvd_prev = float(anterior.get("cvd", 0.0))

    impulso_high = float(df["high"].iloc[-18:].max())
    impulso_low = float(df["low"].iloc[-18:].min())
    rango_impulso = max(impulso_high - impulso_low, 1e-9)
    descuento_long_pct = ((impulso_high - precio) / rango_impulso) * 100.0
    descuento_short_pct = ((precio - impulso_low) / rango_impulso) * 100.0

    sl_min_dist = obtener_sl_minimo(symbol, timeframe, precio)
    pip = tamano_pip(symbol)

    if estado_amt == "OUT_OF_BALANCE_UP" and ctx_1h["sesgo"] == "COMPRA" and adx_val >= min(adx_min, 28.0):
        if descuento_long_pct < DESCUENTO_MIN_FIBO:
            return None

        en_ob = ob and ob["tipo"] == "OB_ALCISTA" and (ob["low"] - atr_ref * 0.35) <= precio <= (ob["high"] + atr_ref * 0.35)
        en_fvg = fvg and fvg["tipo"] == "FVG_ALCISTA" and (fvg["zona_low"] - atr_ref * 0.35) <= precio <= (fvg["zona_high"] + atr_ref * 0.35)
        estructura_ok = float(ultima["close"]) > float(ultima["open"]) and precio >= vwap_val and ema_fast >= ema_slow
        flujo_ok = (cvd_actual > cvd_prev) or desbalance_vol or (mercado in ("FOREX", "METAL"))

        if (en_ob or en_fvg) and estructura_ok and flujo_ok:
            base_ob = ob["low"] if en_ob else fvg["zona_low"]
            sl_tecnico = float(base_ob - max(atr_ref * 0.35, pip * 2))
            riesgo_dist = max(abs(precio - sl_tecnico), sl_min_dist)
            sl_final = float(precio - riesgo_dist)
            tp_final = float(max(impulso_high, precio + riesgo_dist * 2.2))

            pips_pts, r_usd, r_pct = calcular_riesgo_001_lotes(symbol, precio, sl_final, saldo_usd=saldo_usd)
            zona_txt = "Order Block Alcista" if en_ob else "FVG / Imbalance Alcista"

            return {
                "id_senal": "UNIFIED_TREND_LONG",
                "modelo": "Unificado M1: Fuera de Balance + OB/FVG + Desc ≥50%",
                "direccion": "COMPRA (LONG) 🟢",
                "sl": sl_final,
                "tp": tp_final,
                "riesgo_usd": r_usd,
                "riesgo_pct": r_pct,
                "apto_cuenta": r_pct <= riesgo_max_pct,
                "poc": poc,
                "vah": vah,
                "val": val,
                "plan": (
                    f"1) 1h ALCISTA + Fuera de Balance sobre VAH (${vah:,.4f}) con ADX {adx_val:.1f}.\n"
                    f"2) Retroceso en {zona_txt} con descuento Fibonacci {descuento_long_pct:.1f}% (≥50%).\n"
                    f"3) SL en ${sl_final:,.4f} ({pips_pts} pips/pts -> ~${r_usd:.2f} / {r_pct:.1f}%).\n"
                    f"4) TP en ${tp_final:,.4f}; protege a Break-Even si el impulso continúa."
                ),
            }

    if estado_amt == "OUT_OF_BALANCE_DOWN" and ctx_1h["sesgo"] == "VENTA" and adx_val >= min(adx_min, 28.0):
        if descuento_short_pct < DESCUENTO_MIN_FIBO:
            return None

        en_ob = ob and ob["tipo"] == "OB_BAJISTA" and (ob["low"] - atr_ref * 0.35) <= precio <= (ob["high"] + atr_ref * 0.35)
        en_fvg = fvg and fvg["tipo"] == "FVG_BAJISTA" and (fvg["zona_low"] - atr_ref * 0.35) <= precio <= (fvg["zona_high"] + atr_ref * 0.35)
        estructura_ok = float(ultima["close"]) < float(ultima["open"]) and precio <= vwap_val and ema_fast <= ema_slow
        flujo_ok = (cvd_actual < cvd_prev) or desbalance_vol or (mercado in ("FOREX", "METAL"))

        if (en_ob or en_fvg) and estructura_ok and flujo_ok:
            base_ob = ob["high"] if en_ob else fvg["zona_high"]
            sl_tecnico = float(base_ob + max(atr_ref * 0.35, pip * 2))
            riesgo_dist = max(abs(sl_tecnico - precio), sl_min_dist)
            sl_final = float(precio + riesgo_dist)
            tp_final = float(min(impulso_low, precio - riesgo_dist * 2.2))

            pips_pts, r_usd, r_pct = calcular_riesgo_001_lotes(symbol, precio, sl_final, saldo_usd=saldo_usd)
            zona_txt = "Order Block Bajista" if en_ob else "FVG / Imbalance Bajista"

            return {
                "id_senal": "UNIFIED_TREND_SHORT",
                "modelo": "Unificado M1: Fuera de Balance + OB/FVG + Desc ≥50%",
                "direccion": "VENTA (SHORT) 🔴",
                "sl": sl_final,
                "tp": tp_final,
                "riesgo_usd": r_usd,
                "riesgo_pct": r_pct,
                "apto_cuenta": r_pct <= riesgo_max_pct,
                "poc": poc,
                "vah": vah,
                "val": val,
                "plan": (
                    f"1) 1h BAJISTA + Fuera de Balance bajo VAL (${val:,.4f}) con ADX {adx_val:.1f}.\n"
                    f"2) Retroceso en {zona_txt} con descuento Fibonacci {descuento_short_pct:.1f}% (≥50%).\n"
                    f"3) SL en ${sl_final:,.4f} ({pips_pts} pips/pts -> ~${r_usd:.2f} / {r_pct:.1f}%).\n"
                    f"4) TP en ${tp_final:,.4f}; protege a Break-Even si el impulso continúa."
                ),
            }

    min_rango_amt = sl_min_dist * (2.2 if timeframe == "5m" else 1.6)
    if estado_amt == "BALANCE" and (vah - val) >= min_rango_amt:
        fakeout_arriba = (
            (float(anterior["high"]) > vah or float(ultima["high"]) > vah)
            and float(ultima["close"]) < vah
            and float(ultima["close"]) < float(ultima["open"])
            and (poc < precio)
        )
        if fakeout_arriba and ctx_1h["sesgo"] != "COMPRA":
            max_fake = max(float(anterior["high"]), float(ultima["high"]))
            sl_tecnico = float(max_fake + max(pip * 3, atr_ref * 0.30))
            riesgo_dist = max(abs(sl_tecnico - precio), sl_min_dist)
            sl_final = float(precio + riesgo_dist)
            tp_final = float(poc)
            if (precio - tp_final) >= riesgo_dist * 1.4:
                pips_pts, r_usd, r_pct = calcular_riesgo_001_lotes(symbol, precio, sl_final, saldo_usd=saldo_usd)
                return {
                    "id_senal": "UNIFIED_MEAN_REV_SHORT",
                    "modelo": "Unificado M2: Reversión al POC tras Fakeout en VAH",
                    "direccion": "VENTA (SHORT) 🔴",
                    "sl": sl_final,
                    "tp": tp_final,
                    "riesgo_usd": r_usd,
                    "riesgo_pct": r_pct,
                    "apto_cuenta": r_pct <= riesgo_max_pct,
                    "poc": poc,
                    "vah": vah,
                    "val": val,
                    "plan": (
                        f"1) Balance/Rango con fakeout sobre VAH (${vah:,.4f}) y rechazo hacia el área de valor.\n"
                        f"2) SL con colchón sobre fakeout en ${sl_final:,.4f} ({pips_pts} pips/pts -> ~${r_usd:.2f} / {r_pct:.1f}%).\n"
                        f"3) TP objetivo en POC (${tp_final:,.4f}); cuida el spread antes de entrar."
                    ),
                }

        fakeout_abajo = (
            (float(anterior["low"]) < val or float(ultima["low"]) < val)
            and float(ultima["close"]) > val
            and float(ultima["close"]) > float(ultima["open"])
            and (poc > precio)
        )
        if fakeout_abajo and ctx_1h["sesgo"] != "VENTA":
            min_fake = min(float(anterior["low"]), float(ultima["low"]))
            sl_tecnico = float(min_fake - max(pip * 3, atr_ref * 0.30))
            riesgo_dist = max(abs(precio - sl_tecnico), sl_min_dist)
            sl_final = float(precio - riesgo_dist)
            tp_final = float(poc)
            if (tp_final - precio) >= riesgo_dist * 1.4:
                pips_pts, r_usd, r_pct = calcular_riesgo_001_lotes(symbol, precio, sl_final, saldo_usd=saldo_usd)
                return {
                    "id_senal": "UNIFIED_MEAN_REV_LONG",
                    "modelo": "Unificado M2: Reversión al POC tras Fakeout en VAL",
                    "direccion": "COMPRA (LONG) 🟢",
                    "sl": sl_final,
                    "tp": tp_final,
                    "riesgo_usd": r_usd,
                    "riesgo_pct": r_pct,
                    "apto_cuenta": r_pct <= riesgo_max_pct,
                    "poc": poc,
                    "vah": vah,
                    "val": val,
                    "plan": (
                        f"1) Balance/Rango con fakeout bajo VAL (${val:,.4f}) y rechazo hacia el área de valor.\n"
                        f"2) SL con colchón bajo fakeout en ${sl_final:,.4f} ({pips_pts} pips/pts -> ~${r_usd:.2f} / {r_pct:.1f}%).\n"
                        f"3) TP objetivo en POC (${tp_final:,.4f}); cuida el spread antes de entrar."
                    ),
                }

    return None


# ==========================================
# 🛠️ OBTENCIÓN DE DATOS CON CACHÉ RÁPIDA
# ==========================================
def obtener_datos(symbol: str, timeframe: str, limit: int = 100, ttl_segundos: int = 60) -> pd.DataFrame:
    canon = canonizar_simbolo(symbol)
    cache_key = f"{canon}_{timeframe}_{limit}"
    ahora = time.time()

    with STATE_LOCK:
        cached = CACHE_DATOS_GLOBAL.get(cache_key)
        if cached:
            if cached.get("es_real", False) and (ahora - cached["ts"] < ttl_segundos):
                df_cached = cached["df"].copy()
                df_cached.attrs["es_real"] = True
                return df_cached
            if (not cached.get("es_real", False)) and (ahora - cached["ts"] < TTL_FALLO_SEG):
                df_cached = cached["df"].copy()
                df_cached.attrs["es_real"] = False
                return df_cached

    df = pd.DataFrame()
    es_real = False

    if symbol in CRYPTO_MARKETS:
        for ex in EXCHANGES_CRYPTO:
            try:
                ohlcv = ex.fetch_ohlcv(symbol, timeframe, limit=limit)
                if ohlcv:
                    temp_df = pd.DataFrame(ohlcv, columns=["timestamp", "open", "high", "low", "close", "volume"])
                    temp_df["timestamp"] = _normalizar_timestamp(pd.to_datetime(temp_df["timestamp"], unit="ms"))
                    temp_df = temp_df.dropna(subset=["open", "high", "low", "close"]).tail(limit).copy()
                    if not temp_df.empty and float(temp_df["close"].iloc[-1]) > 0:
                        df = temp_df
                        es_real = True
                        break
            except Exception as exc:
                logger.debug("Error CCXT %s [%s]: %s", symbol, timeframe, exc)
                continue
    elif symbol in FOREX_MARKETS or canon == "XAUUSD":
        interval_map = {"5m": "5m", "15m": "15m", "1h": "60m"}
        yf_interval = interval_map.get(timeframe, "60m")
        periodo = "5d" if timeframe in ("5m", "15m") else "1mo"
        simbolos_yf = ["XAUUSD=X", "GC=F"] if canon == "XAUUSD" else [symbol]

        for sym_try in simbolos_yf:
            try:
                hist = yf.Ticker(sym_try).history(period=periodo, interval=yf_interval, timeout=8)
                if not hist.empty and "Close" in hist.columns:
                    temp_df = hist.reset_index()
                    time_col = "Datetime" if "Datetime" in temp_df.columns else ("Date" if "Date" in temp_df.columns else temp_df.columns[0])
                    temp_df.rename(
                        columns={
                            time_col: "timestamp",
                            "Open": "open",
                            "High": "high",
                            "Low": "low",
                            "Close": "close",
                            "Volume": "volume",
                        },
                        inplace=True,
                    )
                    temp_df["timestamp"] = _normalizar_timestamp(temp_df["timestamp"])
                    temp_df["volume"] = temp_df["volume"].fillna(0)
                    temp_df = temp_df.dropna(subset=["open", "high", "low", "close"]).tail(limit).copy()
                    if not temp_df.empty and float(temp_df["close"].iloc[-1]) > 0:
                        df = temp_df
                        es_real = True
                        break
            except Exception as exc:
                logger.debug("Error YF %s (%s) [%s]: %s", sym_try, periodo, timeframe, exc)
                continue

    if df.empty or not all(col in df.columns for col in ["open", "high", "low", "close", "volume", "timestamp"]):
        freq_map = {"5m": "5min", "15m": "15min", "1h": "1h"}
        fechas = pd.date_range(end=datetime.now(), periods=limit, freq=freq_map.get(timeframe, "15min"))
        base_val = 2650.0 if "XAU" in canon else (1.085 if "EUR" in canon else 150.0)
        rng = np.random.default_rng(abs(hash(canon + timeframe)) % (2**32))
        returns = rng.normal(0.0001, 0.002, limit)
        prices = base_val * np.cumprod(1 + returns)

        df = pd.DataFrame(
            {
                "timestamp": fechas,
                "open": prices * (1 + rng.normal(0, 0.0005, limit)),
                "high": prices * (1 + abs(rng.normal(0, 0.001, limit))),
                "low": prices * (1 - abs(rng.normal(0, 0.001, limit))),
                "close": prices,
                "volume": rng.integers(1000, 10000, limit),
            }
        )
        es_real = False

    if not df.empty and len(df) > 5:
        df["vwap"] = calcular_vwap(df)
        window_val = min(20, len(df))
        df["soporte"] = df["low"].rolling(window=window_val).min()
        df["resistencia"] = df["high"].rolling(window=window_val).max()
        df["vol_ma"] = df["volume"].rolling(window=window_val).mean()
        df["ema_fast"] = df["close"].ewm(span=9, adjust=False).mean()
        df["ema_slow"] = df["close"].ewm(span=21, adjust=False).mean()
        df["adx"] = calcular_adx(df)
        df["cvd"] = calcular_cvd_proxy(df)

    df.attrs["es_real"] = es_real

    with STATE_LOCK:
        CACHE_DATOS_GLOBAL[cache_key] = {"ts": ahora, "df": df.copy(), "es_real": es_real}

    return df


# ==========================================
# 🤖 MOTOR CON PROGRESO EN VIVO Y OBSERVABILIDAD
# ==========================================
def ejecutar_logica_autopiloto(es_hilo_fondo: bool = False) -> int:
    with STATE_LOCK:
        if TELEMETRIA_MOTOR["ciclo_en_marcha"]:
            return 0
        TELEMETRIA_MOTOR["ciclo_en_marcha"] = True
        TELEMETRIA_MOTOR["estado"] = "Iniciando barrido..."
        TELEMETRIA_MOTOR["ultimo_ciclo"] = "En curso..."
        TELEMETRIA_MOTOR["latido_hilo"] = time.time()

    unicos = list(dict.fromkeys(canonizar_simbolo(s) for s in ALL_TRACKED_SYMBOLS))
    total_unicos = len(unicos)
    alertas_nuevas = 0
    activos_reales = 0
    candidatas = 0
    bloqueadas_riesgo = 0
    vistos_en_ciclo = set()

    try:
        for idx_sym, symbol in enumerate(ALL_TRACKED_SYMBOLS, start=1):
            canon = canonizar_simbolo(symbol)
            if canon in vistos_en_ciclo:
                continue
            vistos_en_ciclo.add(canon)
            _actualizar_telemetria(
                estado=f"Escaneando {canon} ({len(vistos_en_ciclo)}/{total_unicos})...",
                latido_hilo=time.time(),
            )

            tipo = "CRIPTO" if symbol in CRYPTO_MARKETS else ("METAL (ORO)" if canon == "XAUUSD" else "FOREX")

            try:
                df_1h = obtener_datos(symbol, "1h", limit=85, ttl_segundos=90)
                if df_1h.empty or not df_1h.attrs.get("es_real", False):
                    continue
                activos_reales += 1
                ctx_1h = analizar_contexto_macro_1h(df_1h)

                for tf in ("15m", "5m"):
                    df_tf = obtener_datos(symbol, tf, limit=85, ttl_segundos=60)
                    if df_tf.empty or not df_tf.attrs.get("es_real", False):
                        continue

                    resultado = evaluar_motor_unificado(df_tf, symbol=symbol, timeframe=tf, ctx_1h=ctx_1h)
                    if resultado:
                        candidatas += 1
                        if FILTRAR_SOLO_RIESGO_APTO_TG and not resultado.get("apto_cuenta", True):
                            bloqueadas_riesgo += 1
                            logger.info(
                                "Señal bloqueada por riesgo en %s [%s]: %.1f%% > %.1f%%",
                                canon, tf, resultado["riesgo_pct"], RIESGO_MAX_PCT_CUENTA,
                            )
                            continue

                        ultima = df_tf.iloc[-1]
                        precio = float(ultima["close"])
                        adx_val = float(ultima.get("adx", 0.0))
                        r_usd = resultado["riesgo_usd"]
                        r_pct = resultado["riesgo_pct"]

                        msg = (
                            f"🏦 *SEÑAL UNIFICADA PRO (AMT + FLUJO + OB/FVG)*\n"
                            f"🌐 *Mercado:* `{tipo}` | *Activo:* `{canon}` | *TF:* `{tf}` (1h: `{ctx_1h['sesgo']}`)\n"
                            f"💲 *Precio:* `${precio:,.4f}` | *ADX:* `{adx_val:.1f}`\n"
                            f"🧠 *Modelo:* {resultado['modelo']}\n"
                            f"🧭 *Acción:* *{resultado['direccion']}*\n"
                            f"📊 *POC:* `${resultado['poc']:,.4f}` (VAL `${resultado['val']:,.4f}` / VAH `${resultado['vah']:,.4f}`)\n"
                            f"🛡️ *SL:* `${resultado['sl']:,.4f}` | *TP:* `${resultado['tp']:,.4f}`\n"
                            f"💵 *Riesgo 0.01 lot:* `~${r_usd:.2f} USD` (`{r_pct:.1f}%` de ${SALDO_CUENTA_USD:.0f}) ✅\n"
                            f"📋 *Ejecución:*\n{resultado['plan']}"
                        )

                        clave_alerta = f"{canon}_{resultado['id_senal']}"
                        ahora = time.time()

                        with STATE_LOCK:
                            ultimo_envio = ALERTAS_ENVIADAS_GLOBAL.get(clave_alerta, 0.0)

                        if ahora - ultimo_envio > COOLDOWN_ALERTA_SEG:
                            if enviar_telegram(msg):
                                with STATE_LOCK:
                                    ALERTAS_ENVIADAS_GLOBAL[clave_alerta] = ahora
                                    TELEMETRIA_MOTOR["alertas_enviadas_total"] += 1
                                alertas_nuevas += 1
                        break

                time.sleep(0.05)
            except Exception as exc:
                logger.exception("Error procesando %s: %s", canon, exc)
                continue
    finally:
        _actualizar_telemetria(
            ciclo_en_marcha=False,
            estado="⚡ Motor activo y auditado" if es_hilo_fondo else f"✅ Barrido listo ({alertas_nuevas} alertas nuevas)",
            ultimo_ciclo=datetime.now().strftime("%H:%M:%S"),
            activos_reales=activos_reales,
            candidatas=candidatas,
            bloqueadas_riesgo=bloqueadas_riesgo,
        )

    return alertas_nuevas


@st.cache_resource
def iniciar_hilo_autopiloto():
    def loop():
        while True:
            try:
                ejecutar_logica_autopiloto(es_hilo_fondo=True)
            except Exception as exc:
                logger.exception("Error en hilo autopiloto: %s", exc)
                _actualizar_telemetria(ciclo_en_marcha=False)
            time.sleep(INTERVALO_CICLO_SEG)

    thread = threading.Thread(target=loop, daemon=True, name="QuantAutopilotThread")
    thread.start()
    return thread


iniciar_hilo_autopiloto()

# ==========================================
# 📊 INTERFAZ DE VISUALIZACIÓN & AUDITORÍA
# ==========================================
st.markdown("<h1 style='text-align: center; color: #00FFAA;'>🏦 INSTITUTIONAL QUANT TERMINAL — PRO</h1>", unsafe_allow_html=True)
st.markdown(
    "<p style='text-align: center; color: gray;'>AMT (POC/VAH/VAL) + Contexto 1h -> Gatillo 15m/5m + Flujo (ADX/VWAP/EMAs/CVD) + OB/FVG + Auditoría de Riesgo</p>",
    unsafe_allow_html=True,
)
st.markdown("---")

st.sidebar.header("🌐 Configuración del Gráfico")
symbol = st.sidebar.selectbox("Ver Gráfico de:", ALL_TRACKED_SYMBOLS)
timeframe = st.sidebar.selectbox("Temporalidad", ["15m", "5m", "1h"])

st.sidebar.markdown("---")
st.sidebar.subheader("📡 Estado y Diagnóstico")

col_b1, col_b2 = st.sidebar.columns(2)
if col_b1.button("⚡ Escanear"):
    ejecutar_logica_autopiloto(es_hilo_fondo=False)
    st.rerun()

if col_b2.button("📨 Test TG"):
    ok = enviar_telegram("🧪 *Test de conexión Quant Terminal*: Canal Telegram operativo.")
    if ok:
        st.sidebar.success("Mensaje enviado ✅")
    else:
        st.sidebar.error("Falló envío ❌")

tel = _leer_telemetria()
st.sidebar.info(tel["estado"])
if tel["ultimo_error_tg"]:
    st.sidebar.error(f"Telegram: {tel['ultimo_error_tg']}")

total_unicos = len(set(canonizar_simbolo(s) for s in ALL_TRACKED_SYMBOLS))
c1, c2, c3, c4, c5 = st.columns(5)
c1.metric("Activos Reales (1h)", f"{tel['activos_reales']}/{total_unicos}")
c2.metric("Candidatas Detectadas", tel["candidatas"])
c3.metric("Bloqueadas por Riesgo", tel["bloqueadas_riesgo"])
c4.metric("Alertas Enviadas", tel["alertas_enviadas_total"])
c5.metric("Último Ciclo", tel["ultimo_ciclo"])

with st.sidebar.expander("🔍 Diagnóstico Cripto / Oro"):
    for sym_diag in ["BTC/USDT", "ETH/USDT", "BNB/USDT", "XAUUSD=X"]:
        df_d = obtener_datos(sym_diag, timeframe, limit=85, ttl_segundos=60)
        real_txt = "REAL ✅" if df_d.attrs.get("es_real", False) else "RESPALDO ⚠️"
        adx_d = float(df_d["adx"].iloc[-1]) if not df_d.empty and "adx" in df_d.columns else 0.0
        umbral_d = obtener_umbral_adx(sym_diag)
        st.caption(f"{sym_diag}: {real_txt} | ADX: {adx_d:.1f} (min >{umbral_d:.0f})")


@st.fragment(run_every="10s")
def render_trading_panel(sym: str, tf: str):
    df = obtener_datos(sym, tf, limit=120, ttl_segundos=30)
    if df.empty:
        st.error(f"No se pudieron cargar los datos para {sym}.")
        return

    if not df.attrs.get("es_real", False):
        st.warning("⚠️ Datos de respaldo activos por límite temporal del proveedor. No se emiten alertas de Telegram con estos datos.")

    perfil_panel = calcular_perfil_volumen_amt(df, lookback=36)

    col_grafico, col_paneles = st.columns([2.5, 1])
    with col_grafico:
        fig = make_subplots(rows=2, cols=1, shared_xaxes=True, vertical_spacing=0.03, row_width=[0.2, 0.8])
        fig.add_trace(
            go.Candlestick(
                x=df["timestamp"],
                open=df["open"],
                high=df["high"],
                low=df["low"],
                close=df["close"],
                increasing_line_color="#00FFAA",
                decreasing_line_color="#FF3333",
                name="Precio",
            ),
            row=1,
            col=1,
        )
        fig.add_trace(go.Scatter(x=df["timestamp"], y=df["vwap"], line=dict(color="#FF00FF", width=2), name="VWAP"), row=1, col=1)
        fig.add_trace(go.Scatter(x=df["timestamp"], y=df["ema_fast"], line=dict(color="#FFFF00", width=1.2), name="EMA 9"), row=1, col=1)
        fig.add_trace(go.Scatter(x=df["timestamp"], y=df["ema_slow"], line=dict(color="#00BFFF", width=1.2), name="EMA 21"), row=1, col=1)
        fig.add_hline(y=perfil_panel["poc"], line_dash="solid", line_color="#FFA500", annotation_text="POC", row=1, col=1)
        fig.add_hline(y=perfil_panel["vah"], line_dash="dash", line_color="#00FFAA", annotation_text="VAH", row=1, col=1)
        fig.add_hline(y=perfil_panel["val"], line_dash="dash", line_color="#FF5555", annotation_text="VAL", row=1, col=1)

        colors = ["#00FFAA" if r["close"] >= r["open"] else "#FF3333" for _, r in df.iterrows()]
        fig.add_trace(go.Bar(x=df["timestamp"], y=df["volume"], marker_color=colors, name="Volumen"), row=2, col=1)
        fig.update_layout(
            template="plotly_dark",
            xaxis_rangeslider_visible=False,
            height=600,
            margin=dict(l=10, r=10, t=10, b=10),
            showlegend=True,
        )
        st.plotly_chart(fig, use_container_width=True)

    with col_paneles:
        st.markdown("### 📊 Lectura Unificada")
        ultima = df.iloc[-1]
        anterior = df.iloc[-2]
        precio_actual = float(ultima["close"])
        st.metric(
            label=f"{canonizar_simbolo(sym)} [{tf}]",
            value=f"${precio_actual:,.4f}",
            delta=f"{precio_actual - float(anterior['close']):,.4f}",
        )

        df_1h_panel = obtener_datos(sym, "1h", limit=85, ttl_segundos=90)
        ctx_1h_panel = analizar_contexto_macro_1h(df_1h_panel)
        st.info(f"🧭 Contexto 1h: **{ctx_1h_panel['sesgo']}** | AMT: **{perfil_panel['estado']}**")
        st.caption(f"POC: `${perfil_panel['poc']:,.4f}` | VAL: `${perfil_panel['val']:,.4f}` | VAH: `${perfil_panel['vah']:,.4f}`")

        adx_val = float(ultima.get("adx", 25.0))
        cvd_actual = float(ultima.get("cvd", 0.0))
        cvd_prev = float(anterior.get("cvd", 0.0))
        st.caption(f"ADX: `{adx_val:.1f}` | CVD: {'🟢 Comprador' if cvd_actual >= cvd_prev else '🔴 Vendedor'}")

        ob_panel = detectar_order_block_institucional(df)
        if ob_panel:
            st.caption(f"OB: `{ob_panel['tipo']}` (`${ob_panel['low']:,.4f}` - `${ob_panel['high']:,.4f}`)")
        fvg_panel = detectar_fvg_3_velas(df)
        if fvg_panel:
            st.caption(f"FVG: `{fvg_panel['tipo']}` (`${fvg_panel['zona_low']:,.4f}` - `${fvg_panel['zona_high']:,.4f}`)")

        st.markdown("---")
        st.markdown("### 🎯 Gatillo Unificado")
        tf_eval = "15m" if tf == "1h" else tf
        df_eval = obtener_datos(sym, tf_eval, limit=85, ttl_segundos=60) if tf_eval != tf else df
        senal = evaluar_motor_unificado(df_eval, symbol=sym, timeframe=tf_eval, ctx_1h=ctx_1h_panel)
        if senal:
            estado_riesgo = "✅ Apto para cuenta" if senal["apto_cuenta"] else f"⚠️ Supera tope ({senal['riesgo_pct']}%)"
            st.success(
                f"**{senal['modelo']}** ({estado_riesgo})\n\n"
                f"**Acción:** {senal['direccion']}\n\n{senal['plan']}"
            )
        else:
            st.caption("Sin setup unificado válido en esta vela.")


render_trading_panel(symbol, timeframe)
