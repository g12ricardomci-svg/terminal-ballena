import os
import streamlit as st
import ccxt
import yfinance as yf
import pandas as pd
import numpy as np
import plotly.graph_objects as go
from plotly.subplots import make_subplots
import requests
import time
import threading
from datetime import datetime, timezone

# ==========================================
# ⚙️ CONFIGURACIÓN GLOBAL & GESTIÓN PARA CUENTA DE $10 USD
# ==========================================
def _leer_secreto(nombre, defecto=""):
    """Lee credenciales de .streamlit/secrets.toml o de variables de entorno (nunca del código)."""
    try:
        if nombre in st.secrets:
            return str(st.secrets[nombre])
    except Exception:
        pass
    return os.environ.get(nombre, defecto)

TELEGRAM_TOKEN = _leer_secreto("TELEGRAM_TOKEN", "9148434925:AAH_AuswS-GGPHadFBwcrDOUUIwBFbXvtDs")
TELEGRAM_CHAT_ID = _leer_secreto("TELEGRAM_CHAT_ID", "1496321437")

SALDO_CUENTA_USD = 10.0
RIESGO_MAX_PCT_CUENTA = 15.0        # Bloquea alertas si 0.01 lotes arriesga más del 15% ($1.50 USD)
DESCUENTO_MIN_FIBO = 50.0           # Regla El Sensei: exigir >= 50% de descuento antes de alertar
FILTRAR_SOLO_RIESGO_APTO_TG = True  # Evita alertas con SL demasiado ancho para $10 USD

# --- 🆕 EL SENSEI: fractalidad 4H -> entrada en temporalidad baja ---
USAR_CONTEXTO_4H = True             # 4H manda la dirección y marca el Order Block principal
FIBO_NIVELES = [50, 60, 70, 80, 90] # Niveles de descuento institucional (más profundo = mejor entrada)
CALIDAD_MINIMA = 0                  # 0 = solo informativa. Sube a 2-3 para alertas más exigentes (escala 0-5)

# --- 🆕 FABIO VALENTINO: ubicación, agresión y sesiones ---
USAR_LVN_COMO_ZONA = True           # Un Nodo de Bajo Volumen del impulso también vale como zona de entrada (Modelo 1)
USAR_FILTRO_SESIONES = False        # True = Modelo 1 solo en Nueva York, Modelo 2 solo en Londres (horas en UTC)
SESION_NY_UTC = (13.5, 21.0)        # Apertura NY ~13:30 UTC (varía 1h con el cambio de horario de verano)
SESION_LONDRES_UTC = (7.0, 16.0)
SOLO_VELA_CERRADA = True            # Las alertas evalúan la última vela CERRADA (evita señales que desaparecen)

# --- 🆕 Mercado cerrado / datos viejos (evita alertas con precios de la sesión anterior) ---
BLOQUEAR_SI_MERCADO_CERRADO = True  # Forex/oro: sin alertas de viernes ~22:00 UTC a domingo ~22:00 UTC o con velas viejas

# --- 🆕 ADX mínimo por mercado (punto de partida; no está calibrado con backtest) ---
ADX_MIN_POR_MERCADO = {"FOREX": 25.0, "CRYPTO": 28.0, "METAL": 30.0}

# --- 🆕 Freno de exhaustación (Larry Williams) como VETO ---
EXHAUSTACION_VETA_MODELO_2 = False  # Modelos 1 y 3 (tendencia) siempre vetados; el 2 (reversión) solo si pones True

st.set_page_config(page_title="Institutional Quant Terminal — AMT & Smart Money", layout="wide", page_icon="🏦")

CRYPTO_MARKETS = ["BTC/USDT", "ETH/USDT", "BNB/USDT"]
FOREX_MARKETS = [
    "EURUSD=X", "GBPUSD=X", "USDJPY=X", "USDCHF=X", "AUDUSD=X",
    "USDCAD=X", "NZDUSD=X", "EURJPY=X", "GBPJPY=X", "EURGBP=X",
    "XAUUSD=X"
]
NASDAQ_MARKETS = []

MAPA_CANONICO = {
    "XAUUSD": "XAUUSD",
    "XAUUSD=X": "XAUUSD",
    "GC=F": "XAUUSD",
}

def canonizar_simbolo(symbol):
    s = str(symbol or "").strip().upper()
    return MAPA_CANONICO.get(s, s)

def clasificar_mercado(symbol):
    canon = canonizar_simbolo(symbol)
    if symbol in CRYPTO_MARKETS or "/" in canon:
        return "CRYPTO"
    if canon == "XAUUSD":
        return "METAL"
    return "FOREX"

def tamano_pip(symbol):
    canon = canonizar_simbolo(symbol)
    if canon == "XAUUSD":
        return 0.10
    if "JPY" in canon:
        return 0.01
    return 0.0001

def calcular_riesgo_001_lotes(symbol, precio, sl):
    canon = canonizar_simbolo(symbol)
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
    elif canon.endswith("USD"):
        pips = dist / 0.0001
        riesgo_usd = pips * 0.10
    elif "JPY" in canon:
        pips = dist / 0.01
        riesgo_usd = pips * (10.0 / float(precio))
    else:
        pips = dist / 0.0001
        riesgo_usd = pips * (0.10 / float(precio))

    riesgo_usd = round(float(riesgo_usd), 2)
    riesgo_pct = round((riesgo_usd / max(SALDO_CUENTA_USD, 0.01)) * 100.0, 1)
    return round(float(pips_o_pts), 1), riesgo_usd, riesgo_pct

ALL_TRACKED_SYMBOLS = list(dict.fromkeys(CRYPTO_MARKETS + FOREX_MARKETS))

def _crear_exchange(ex_id):
    try:
        cls = getattr(ccxt, ex_id)
        return cls({"enableRateLimit": True})
    except Exception:
        return None

EXCHANGE_OKX = _crear_exchange("okx")
EXCHANGE_BINANCE = _crear_exchange("binance")

ALERTAS_ENVIADAS_GLOBAL = {}
CACHE_DATOS_GLOBAL = {}
CACHE_LOCK = threading.Lock()
ALERTAS_LOCK = threading.Lock()
AUTOPILOTO_STATUS = "Motor AMT + Order Blocks activo: Analizando contexto 4h/1h + entrada 15m/5m..."
ULTIMO_ERROR_TG = ""
DIAGNOSTICO = {}

# ==========================================
# 🧠 FUNCIONES MATEMÁTICAS: AMT (FABIO VALENTINO) + OB/FVG (EL SENSEI)
# ==========================================
def enviar_telegram(mensaje):
    global ULTIMO_ERROR_TG
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        ULTIMO_ERROR_TG = "Falta TELEGRAM_TOKEN / TELEGRAM_CHAT_ID (secrets o variables de entorno)"
        return False
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    payload = {"chat_id": TELEGRAM_CHAT_ID, "text": mensaje, "parse_mode": "Markdown"}
    try:
        resp = requests.post(url, json=payload, timeout=8)
        if resp.status_code == 200:
            ULTIMO_ERROR_TG = ""
            return True
        ULTIMO_ERROR_TG = f"HTTP {resp.status_code}: {resp.text[:140]}"
        return False
    except Exception as e:
        ULTIMO_ERROR_TG = str(e).replace(TELEGRAM_TOKEN, "***")[:140]
        return False

def _normalizar_timestamp(serie):
    ts = pd.to_datetime(serie)
    if getattr(ts.dt, "tz", None) is not None:
        ts = ts.dt.tz_convert(None)
    return ts

def _resamplear_a_4h(df):
    """yfinance no ofrece 4h: se construye agrupando velas de 1h."""
    d = df.set_index("timestamp")
    agg = d.resample("4h").agg({
        "open": "first", "high": "max", "low": "min", "close": "last", "volume": "sum"
    }).dropna(subset=["open", "high", "low", "close"])
    return agg.reset_index()

def obtener_sl_minimo(symbol, timeframe, precio):
    mercado = clasificar_mercado(symbol)
    pip = tamano_pip(symbol)
    if mercado == "FOREX":
        pips_min = {"5m": 8.0, "15m": 11.0, "1h": 16.0}.get(timeframe, 10.0)
        return pips_min * pip
    if mercado == "METAL":
        pts_min = {"5m": 4.0, "15m": 7.0, "1h": 12.0}.get(timeframe, 7.0)
        return pts_min
    pct_min = {"5m": 0.0040, "15m": 0.0070, "1h": 0.0120}.get(timeframe, 0.0070)
    return float(precio) * pct_min

def calcular_vwap(df):
    if df.empty:
        return pd.Series(dtype=float)
    if "volume" not in df.columns or float(df["volume"].sum()) <= 0:
        return df["close"].rolling(20, min_periods=1).mean()
    tp = (df["high"] + df["low"] + df["close"]) / 3.0
    vol_roll = df["volume"].rolling(24, min_periods=1).sum().replace(0, np.nan)
    vwap = (tp * df["volume"]).rolling(24, min_periods=1).sum() / vol_roll
    return vwap.fillna(df["close"].rolling(20, min_periods=1).mean())

def calcular_adx(df, period=14):
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
            np.maximum(abs(d["high"] - d["close"].shift()), abs(d["low"] - d["close"].shift()))
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

def calcular_cvd_proxy(df):
    rango = (df["high"] - df["low"]).replace(0, np.nan)
    delta_ratio = ((df["close"] - df["open"]) / rango).fillna(0.0)
    delta_vol = df["volume"] * delta_ratio
    return delta_vol.cumsum()

def calcular_perfil_volumen_amt(df, lookback=36, bins=18):
    """
    Calcula POC (Point of Control), VAH (Value Area High), VAL (Value Area Low)
    y estado AMT (BALANCE vs FUERA DE BALANCE) según Fabio Valentino.
    """
    sub = df.iloc[-lookback:].copy() if len(df) >= lookback else df.copy()
    p_min = float(sub["low"].min())
    p_max = float(sub["high"].max())
    precio_actual = float(sub["close"].iloc[-1])

    if p_max <= p_min:
        return {
            "poc": precio_actual,
            "vah": precio_actual,
            "val": precio_actual,
            "estado": "BALANCE",
        }

    # Perfil previo (excluyendo las últimas 3 velas para detectar ruptura real fuera de balance)
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

    # Construir Value Area (~70% del volumen alrededor del POC)
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

    return {
        "poc": poc,
        "vah": vah,
        "val": val,
        "estado": estado,
    }

def calcular_lvns(df, lookback=18, bins=12):
    """
    🆕 FABIO (Modelo 1, Paso 2 - Ubicación): Nodos de Bajo Volumen (LVN) dentro del impulso reciente.
    Son zonas por donde el precio pasó rápido (poco volumen); suelen reaccionar al volver.
    Si el activo no tiene volumen (forex en yfinance), usa tiempo-en-precio como aproximación.
    """
    sub = df.iloc[-lookback:] if len(df) >= lookback else df
    if len(sub) < 6:
        return []
    p_min, p_max = float(sub["low"].min()), float(sub["high"].max())
    if p_max <= p_min:
        return []

    edges = np.linspace(p_min, p_max, bins + 1)
    centers = (edges[:-1] + edges[1:]) / 2.0
    vol_bins = np.zeros(bins, dtype=float)
    tiene_vol = ("volume" in sub.columns) and (float(sub["volume"].sum()) > 0)

    for _, row in sub.iterrows():
        i0 = int(np.clip(np.digitize(float(row["low"]), edges) - 1, 0, bins - 1))
        i1 = int(np.clip(np.digitize(float(row["high"]), edges) - 1, 0, bins - 1))
        peso = float(row["volume"]) if tiene_vol else 1.0
        vol_bins[i0:i1 + 1] += peso / (i1 - i0 + 1)

    media = float(vol_bins.mean())
    if media <= 0:
        return []

    lvns = []
    for i in range(1, bins - 1):
        if vol_bins[i] <= vol_bins[i - 1] and vol_bins[i] <= vol_bins[i + 1] and vol_bins[i] < media * 0.6:
            lvns.append(float(centers[i]))
    return lvns

def lvn_mas_cercano(lvns, precio, tolerancia):
    candidatos = [x for x in lvns if abs(x - precio) <= tolerancia]
    if not candidatos:
        return None
    return min(candidatos, key=lambda x: abs(x - precio))

def detectar_fvg_3_velas(df):
    """Detecta Imbalance / Fair Value Gap de 3 velas."""
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

def detectar_order_block_institucional(df):
    """
    Regla El Sensei:
    - OB Alcista: última vela bajista antes de un impulso alcista con imbalance.
    - OB Bajista: última vela alcista antes de un impulso bajista con imbalance.
    La zona se marca completa: de la mecha máxima a la mecha mínima de la vela.
    """
    if len(df) < 12:
        return None
    for idx in range(len(df) - 2, max(2, len(df) - 10), -1):
        vela = df.iloc[idx]
        sig1 = df.iloc[idx + 1]
        cuerpo_impulso = abs(float(sig1["close"]) - float(sig1["open"]))
        rango_prev = (df["high"].iloc[max(0, idx - 10):idx] - df["low"].iloc[max(0, idx - 10):idx]).mean()
        atr_local = float(rango_prev) if pd.notna(rango_prev) and rango_prev > 0 else cuerpo_impulso

        # OB Alcista: vela roja seguida de vela verde impulsiva
        if float(vela["close"]) < float(vela["open"]) and float(sig1["close"]) > float(sig1["open"]):
            if float(sig1["close"]) > float(vela["high"]) and cuerpo_impulso >= atr_local * 0.8:
                return {
                    "tipo": "OB_ALCISTA",
                    "low": float(vela["low"]),
                    "high": float(vela["high"]),
                    "pos": idx,
                }

        # OB Bajista: vela verde seguida de vela roja impulsiva
        if float(vela["close"]) > float(vela["open"]) and float(sig1["close"]) < float(sig1["open"]):
            if float(sig1["close"]) < float(vela["low"]) and cuerpo_impulso >= atr_local * 0.8:
                return {
                    "tipo": "OB_BAJISTA",
                    "low": float(vela["low"]),
                    "high": float(vela["high"]),
                    "pos": idx,
                }
    return None

def analizar_contexto_macro_1h(df_1h, symbol):
    """
    Temporalidad Alta (1h): define el sesgo direccional y estado AMT macro.
    No envía alertas sueltas de 1h; filtra las entradas de 15m/5m.
    """
    if df_1h is None or df_1h.empty or len(df_1h) < 30:
        return {"sesgo": "NEUTRAL", "amt": "BALANCE", "poc": 0.0, "vah": 0.0, "val": 0.0}

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

# ---------- 🆕 EL SENSEI: 4H manda la dirección y marca la zona principal ----------
def analizar_contexto_4h(df_4h):
    """
    Temporalidad Alta (4h): sesgo de tendencia + Order Block / FVG principal.
    Devuelve None si no hay datos suficientes (el motor sigue funcionando sin 4h).
    """
    if df_4h is None or df_4h.empty or len(df_4h) < 25:
        return None
    ultima = df_4h.iloc[-1]
    precio = float(ultima["close"])
    ema9 = float(ultima.get("ema_fast", precio))
    ema21 = float(ultima.get("ema_slow", precio))

    if ema9 > ema21:
        sesgo = "COMPRA"
    elif ema9 < ema21:
        sesgo = "VENTA"
    else:
        sesgo = "NEUTRAL"

    rangos = df_4h["high"].iloc[-21:-1] - df_4h["low"].iloc[-21:-1]
    atr = float(rangos.mean()) if len(rangos) > 0 and pd.notna(rangos.mean()) else precio * 0.005

    return {
        "sesgo": sesgo,
        "ob": detectar_order_block_institucional(df_4h),
        "fvg": detectar_fvg_3_velas(df_4h),
        "atr": atr,
        "precio": precio,
    }

def alineado_con_4h(ctx_4h, lado):
    """No operar en contra de la tendencia 4H (si no hay dato 4H, no bloquea)."""
    if not USAR_CONTEXTO_4H or not ctx_4h:
        return True
    contrario = "VENTA" if lado == "LONG" else "COMPRA"
    return ctx_4h["sesgo"] != contrario

def zona_4h_activa(ctx_4h, precio, lado):
    """¿El precio está dentro (o muy cerca) del OB/FVG 4H a favor de la dirección?"""
    if not ctx_4h:
        return None
    tol = ctx_4h["atr"] * 0.25
    if lado == "LONG":
        tipo_ob, tipo_fvg, etiqueta = "OB_ALCISTA", "FVG_ALCISTA", "Alcista"
    else:
        tipo_ob, tipo_fvg, etiqueta = "OB_BAJISTA", "FVG_BAJISTA", "Bajista"

    ob, fvg = ctx_4h.get("ob"), ctx_4h.get("fvg")
    if ob and ob["tipo"] == tipo_ob and (ob["low"] - tol) <= precio <= (ob["high"] + tol):
        return {"nombre": f"Order Block {etiqueta} 4H", "low": ob["low"], "high": ob["high"]}
    if fvg and fvg["tipo"] == tipo_fvg and (fvg["zona_low"] - tol) <= precio <= (fvg["zona_high"] + tol):
        return {"nombre": f"FVG {etiqueta} 4H", "low": fvg["zona_low"], "high": fvg["zona_high"]}
    return None

def nivel_fibo(pct):
    """Nivel de descuento institucional alcanzado: 0, 50, 60, 70, 80 o 90."""
    nivel = 0
    for n in FIBO_NIVELES:
        if pct >= n:
            nivel = n
    return nivel

def puntuar_calidad(nivel, zona_htf, lvn_cerca, agresion):
    """Calidad de la señal (0-5): descuento profundo + zona 4H + LVN + agresión de flujo."""
    pts = 0
    if nivel >= 60:
        pts += 1
    if nivel >= 80:
        pts += 1
    pts += int(bool(zona_htf)) + int(bool(lvn_cerca)) + int(bool(agresion))
    return pts

def en_sesion(nombres):
    """🆕 FABIO: Modelo 1 -> Nueva York | Modelo 2 -> Londres. Si el filtro está apagado, siempre True."""
    if not USAR_FILTRO_SESIONES:
        return True
    ahora = datetime.now(timezone.utc)
    h = ahora.hour + ahora.minute / 60.0
    rangos = {"NY": SESION_NY_UTC, "LONDRES": SESION_LONDRES_UTC}
    return any(rangos[n][0] <= h < rangos[n][1] for n in nombres)

def detectar_exhaustacion(df):
    """
    🆕 Freno de exhaustación (Larry Williams): vela de rango gigante después de un movimiento largo.
    Comprar ahí es comprar el techo; vender ahí es vender el suelo.
    """
    if len(df) < 22:
        return False
    rango_actual = df["high"].iloc[-1] - df["low"].iloc[-1]
    rangos_previos = (df["high"].iloc[-21:-1] - df["low"].iloc[-21:-1])
    max_previo = rangos_previos.max()
    mov_previo = abs(df["close"].iloc[-2] - df["close"].iloc[-10])
    atr_aprox = rangos_previos.mean()
    return bool((rango_actual > max_previo * 1.15) and (mov_previo > atr_aprox * 3.0))

def umbral_adx(symbol):
    """🆕 Fuerza mínima de tendencia según el mercado (oro > cripto > forex)."""
    return ADX_MIN_POR_MERCADO.get(clasificar_mercado(symbol), 25.0)

def mercado_abierto(symbol):
    """
    🆕 Cripto: 24/7. Forex y oro: cerrados desde el viernes ~22:00 UTC hasta el domingo ~22:00 UTC
    (en horario de verano de EE. UU. el cierre/apertura pasa a las 21:00 UTC).
    """
    if not BLOQUEAR_SI_MERCADO_CERRADO or clasificar_mercado(symbol) == "CRYPTO":
        return True
    ahora = datetime.now(timezone.utc)
    dia = ahora.weekday()  # lunes=0 ... domingo=6
    h = ahora.hour + ahora.minute / 60.0
    if dia == 5:
        return False
    if dia == 4 and h >= 22.0:
        return False
    if dia == 6 and h < 22.0:
        return False
    return True

MINUTOS_TF = {"5m": 5, "15m": 15, "1h": 60, "4h": 240}

def datos_frescos(df, timeframe):
    """🆕 La última vela debe ser reciente; si no, el proveedor está mostrando una sesión vieja."""
    if not BLOQUEAR_SI_MERCADO_CERRADO:
        return True
    try:
        ultimo = pd.to_datetime(df["timestamp"].iloc[-1]).to_pydatetime()
        ahora = datetime.now(timezone.utc).replace(tzinfo=None)
        edad_min = (ahora - ultimo).total_seconds() / 60.0
        return edad_min <= max(MINUTOS_TF.get(timeframe, 15) * 2.5, 20.0)
    except Exception:
        return True

def evaluar_modelo_fabio_sensei(df, symbol, timeframe, ctx_1h, ctx_4h=None):
    """
    Combina:
    1. Modelo 1 Fabio Valentino (Fuera de Balance + LVN/OB/FVG + Flujo CVD/Volumen -> objetivo POC/extremo).
    2. Modelo 2 Fabio Valentino (Consolidación / Balance -> Fakeout en VAH/VAL -> retorno al POC).
    3. Modelo 3 El Sensei (OB/FVG en 4H -> nuevo OB en 15m/5m + Descuento Fibonacci >= 50%).
    Filtros comunes: alineación 4H/1h, descuento escalonado, sesiones, break-even por CVD.
    """
    if df is None or df.empty or len(df) < 30:
        return None

    mercado = clasificar_mercado(symbol)
    ultima = df.iloc[-1]
    anterior = df.iloc[-2]
    precio = float(ultima["close"])
    vwap_val = float(ultima.get("vwap", precio))
    ema_fast = float(ultima.get("ema_fast", precio))
    ema_slow = float(ultima.get("ema_slow", precio))
    adx_val = float(ultima.get("adx", 20.0))

    rangos_previos = (df["high"].iloc[-21:-1] - df["low"].iloc[-21:-1])
    atr_ref = float(rangos_previos.mean()) if len(rangos_previos) > 0 and pd.notna(rangos_previos.mean()) else max(precio * 0.002, tamano_pip(symbol) * 8)

    perfil = calcular_perfil_volumen_amt(df, lookback=36)
    poc, vah, val, estado_amt = perfil["poc"], perfil["vah"], perfil["val"], perfil["estado"]
    ob = detectar_order_block_institucional(df)
    fvg = detectar_fvg_3_velas(df)
    lvns = calcular_lvns(df, lookback=18, bins=12)
    lvn_cerca = lvn_mas_cercano(lvns, precio, atr_ref * 0.5)

    vol_actual = float(ultima.get("volume", 0.0))
    vol_ma = float(ultima.get("vol_ma", 0.0))
    cvd_actual = float(ultima.get("cvd", 0.0))
    cvd_prev = float(anterior.get("cvd", 0.0))

    # 🆕 Agresión de flujo (proxy del "order flow" de Fabio: volumen anormal + CVD a favor)
    volumen_fuerte = vol_ma > 0 and vol_actual > vol_ma * 1.5
    agresion_compra = volumen_fuerte and cvd_actual > cvd_prev
    agresion_venta = volumen_fuerte and cvd_actual < cvd_prev

    # Impulso reciente (últimas 18 velas) para medir el Descuento Fibonacci (regla El Sensei >= 50%)
    impulso_high = float(df["high"].iloc[-18:].max())
    impulso_low = float(df["low"].iloc[-18:].min())
    rango_impulso = max(impulso_high - impulso_low, 1e-9)
    descuento_long_pct = ((impulso_high - precio) / rango_impulso) * 100.0
    descuento_short_pct = ((precio - impulso_low) / rango_impulso) * 100.0

    sl_min_dist = obtener_sl_minimo(symbol, timeframe, precio)
    pip = tamano_pip(symbol)

    vela_alcista = float(ultima["close"]) > float(ultima["open"])
    vela_bajista = float(ultima["close"]) < float(ultima["open"])

    exhaustado = detectar_exhaustacion(df)  # 🆕 veto: no comprar techo / vender suelo
    adx_min = umbral_adx(symbol)            # 🆕 ADX mínimo según mercado

    # =========================================================================
    # MODELO 1: TENDENCIA FUERA DE BALANCE (FABIO) + LVN/OB/FVG + DESCUENTO >= 50% (SENSEI)
    # =========================================================================
    if (estado_amt == "OUT_OF_BALANCE_UP" and ctx_1h["sesgo"] == "COMPRA" and adx_val >= adx_min and not exhaustado
            and alineado_con_4h(ctx_4h, "LONG") and en_sesion(("NY",))
            and descuento_long_pct >= DESCUENTO_MIN_FIBO):  # Regla anti-pico: descuento mínimo

        en_ob = ob and ob["tipo"] == "OB_ALCISTA" and (ob["low"] - atr_ref * 0.35) <= precio <= (ob["high"] + atr_ref * 0.35)
        en_fvg = fvg and fvg["tipo"] == "FVG_ALCISTA" and (fvg["zona_low"] - atr_ref * 0.35) <= precio <= (fvg["zona_high"] + atr_ref * 0.35)
        en_lvn = USAR_LVN_COMO_ZONA and lvn_cerca is not None
        reaccion_alcista = vela_alcista and (precio >= ema_slow or precio >= vwap_val)
        flujo_ok = (cvd_actual > cvd_prev) or (vol_ma > 0 and vol_actual > vol_ma * 1.15) or (mercado in ("FOREX", "METAL") and ema_fast >= ema_slow)

        nivel = nivel_fibo(descuento_long_pct)
        zona4 = zona_4h_activa(ctx_4h, precio, "LONG")
        calidad = puntuar_calidad(nivel, zona4, lvn_cerca is not None, agresion_compra)

        if (en_ob or en_fvg or en_lvn) and reaccion_alcista and flujo_ok and calidad >= CALIDAD_MINIMA:
            if en_ob:
                base_zona, zona_txt = ob["low"], "Order Block Alcista"
            elif en_fvg:
                base_zona, zona_txt = fvg["zona_low"], "FVG / Imbalance Alcista"
            else:
                base_zona, zona_txt = lvn_cerca, "Nodo de Bajo Volumen (LVN)"
            sl_tecnico = float(base_zona - max(atr_ref * 0.35, pip * 2))
            riesgo_dist = max(abs(precio - sl_tecnico), sl_min_dist)
            sl_final = float(precio - riesgo_dist)
            tp_final = float(max(impulso_high, precio + riesgo_dist * 2.5))
            be_nivel = float(precio + riesgo_dist)

            pips_pts, r_usd, r_pct = calcular_riesgo_001_lotes(symbol, precio, sl_final)
            apto = r_pct <= RIESGO_MAX_PCT_CUENTA

            return {
                "id_senal": "AMT_TREND_OB_LONG",
                "modelo": "Modelo 1: Fuera de Balance + LVN/OB/FVG (Descuento ≥50%)",
                "direccion": "COMPRA (LONG) 🟢",
                "sl": sl_final,
                "tp": tp_final,
                "be": be_nivel,
                "riesgo_usd": r_usd,
                "riesgo_pct": r_pct,
                "apto_cuenta": apto,
                "descuento_pct": round(descuento_long_pct, 1),
                "nivel_fibo": nivel,
                "calidad": calidad,
                "poc": poc,
                "vah": vah,
                "val": val,
                "plan": (
                    f"1) Contexto ALCISTA + Mercado Fuera de Balance arriba de VAH (${vah:,.4f}).\n"
                    f"2) Retroceso validado en {zona_txt} con descuento Fibonacci del {descuento_long_pct:.1f}% (nivel {nivel}%).\n"
                    f"3) SL técnico detrás de la zona en ${sl_final:,.4f} ({pips_pts} pips/pts -> ~${r_usd:.2f} / {r_pct:.1f}% de $10).\n"
                    f"4) TP objetivo en liquidez/máximo (${tp_final:,.4f}).\n"
                    f"5) Break-Even: sube el SL a entrada cuando el precio toque ${be_nivel:,.4f} (+1R) y el CVD siga a favor."
                ),
            }

    if (estado_amt == "OUT_OF_BALANCE_DOWN" and ctx_1h["sesgo"] == "VENTA" and adx_val >= adx_min and not exhaustado
            and alineado_con_4h(ctx_4h, "SHORT") and en_sesion(("NY",))
            and descuento_short_pct >= DESCUENTO_MIN_FIBO):

        en_ob = ob and ob["tipo"] == "OB_BAJISTA" and (ob["low"] - atr_ref * 0.35) <= precio <= (ob["high"] + atr_ref * 0.35)
        en_fvg = fvg and fvg["tipo"] == "FVG_BAJISTA" and (fvg["zona_low"] - atr_ref * 0.35) <= precio <= (fvg["zona_high"] + atr_ref * 0.35)
        en_lvn = USAR_LVN_COMO_ZONA and lvn_cerca is not None
        reaccion_bajista = vela_bajista and (precio <= ema_slow or precio <= vwap_val)
        flujo_ok = (cvd_actual < cvd_prev) or (vol_ma > 0 and vol_actual > vol_ma * 1.15) or (mercado in ("FOREX", "METAL") and ema_fast <= ema_slow)

        nivel = nivel_fibo(descuento_short_pct)
        zona4 = zona_4h_activa(ctx_4h, precio, "SHORT")
        calidad = puntuar_calidad(nivel, zona4, lvn_cerca is not None, agresion_venta)

        if (en_ob or en_fvg or en_lvn) and reaccion_bajista and flujo_ok and calidad >= CALIDAD_MINIMA:
            if en_ob:
                base_zona, zona_txt = ob["high"], "Order Block Bajista"
            elif en_fvg:
                base_zona, zona_txt = fvg["zona_high"], "FVG / Imbalance Bajista"
            else:
                base_zona, zona_txt = lvn_cerca, "Nodo de Bajo Volumen (LVN)"
            sl_tecnico = float(base_zona + max(atr_ref * 0.35, pip * 2))
            riesgo_dist = max(abs(sl_tecnico - precio), sl_min_dist)
            sl_final = float(precio + riesgo_dist)
            tp_final = float(min(impulso_low, precio - riesgo_dist * 2.5))
            be_nivel = float(precio - riesgo_dist)

            pips_pts, r_usd, r_pct = calcular_riesgo_001_lotes(symbol, precio, sl_final)
            apto = r_pct <= RIESGO_MAX_PCT_CUENTA

            return {
                "id_senal": "AMT_TREND_OB_SHORT",
                "modelo": "Modelo 1: Fuera de Balance + LVN/OB/FVG (Descuento ≥50%)",
                "direccion": "VENTA (SHORT) 🔴",
                "sl": sl_final,
                "tp": tp_final,
                "be": be_nivel,
                "riesgo_usd": r_usd,
                "riesgo_pct": r_pct,
                "apto_cuenta": apto,
                "descuento_pct": round(descuento_short_pct, 1),
                "nivel_fibo": nivel,
                "calidad": calidad,
                "poc": poc,
                "vah": vah,
                "val": val,
                "plan": (
                    f"1) Contexto BAJISTA + Mercado Fuera de Balance debajo de VAL (${val:,.4f}).\n"
                    f"2) Retroceso validado en {zona_txt} con descuento Fibonacci del {descuento_short_pct:.1f}% (nivel {nivel}%).\n"
                    f"3) SL técnico detrás de la zona en ${sl_final:,.4f} ({pips_pts} pips/pts -> ~${r_usd:.2f} / {r_pct:.1f}% de $10).\n"
                    f"4) TP objetivo en liquidez/mínimo (${tp_final:,.4f}).\n"
                    f"5) Break-Even: baja el SL a entrada cuando el precio toque ${be_nivel:,.4f} (+1R) y el CVD siga a favor."
                ),
            }

    # =========================================================================
    # MODELO 2: REVERSIÓN A LA MEDIA EN CONSOLIDACIÓN (FABIO: FAKEOUT -> RETORNO AL POC)
    # =========================================================================
    if (estado_amt == "BALANCE" and (vah - val) > sl_min_dist * 1.5 and en_sesion(("LONDRES",))
            and not (exhaustado and EXHAUSTACION_VETA_MODELO_2)):
        # Fakeout alcista fallido encima de VAH -> Vuelta al POC
        fakeout_arriba = (
            (float(anterior["high"]) > vah or float(ultima["high"]) > vah)
            and float(ultima["close"]) < vah
            and vela_bajista
            and (poc < precio)
        )
        if fakeout_arriba and ctx_1h["sesgo"] != "COMPRA":
            max_fake = max(float(anterior["high"]), float(ultima["high"]))
            sl_tecnico = float(max_fake + max(pip * 2, atr_ref * 0.25))
            riesgo_dist = max(abs(sl_tecnico - precio), sl_min_dist)
            sl_final = float(precio + riesgo_dist)
            tp_final = float(poc)
            if (precio - tp_final) >= riesgo_dist * 1.3:
                pips_pts, r_usd, r_pct = calcular_riesgo_001_lotes(symbol, precio, sl_final)
                apto = r_pct <= RIESGO_MAX_PCT_CUENTA
                be_nivel = float(precio - riesgo_dist)
                return {
                    "id_senal": "AMT_MEAN_REV_SHORT",
                    "modelo": "Modelo 2: Reversión al POC tras Fakeout en VAH (AMT)",
                    "direccion": "VENTA (SHORT) 🔴",
                    "sl": sl_final,
                    "tp": tp_final,
                    "be": be_nivel,
                    "riesgo_usd": r_usd,
                    "riesgo_pct": r_pct,
                    "apto_cuenta": apto,
                    "descuento_pct": 0.0,
                    "nivel_fibo": 0,
                    "calidad": None,
                    "poc": poc,
                    "vah": vah,
                    "val": val,
                    "plan": (
                        f"1) Mercado en Balance/Rango: ruptura fallida (fakeout) sobre VAH (${vah:,.4f}). No se opera la primera ruptura.\n"
                        f"2) Segundo impulso de rechazo entrando de nuevo al Área de Valor.\n"
                        f"3) SL ajustado con colchón sobre el fakeout en ${sl_final:,.4f} (~${r_usd:.2f} / {r_pct:.1f}% de $10).\n"
                        f"4) TP objetivo directo en el Punto de Control (POC) en ${tp_final:,.4f}.\n"
                        f"5) Break-Even: baja el SL a entrada al tocar ${be_nivel:,.4f} (+1R) si el CVD acompaña."
                    ),
                }

        # Fakeout bajista fallido debajo de VAL -> Vuelta al POC
        fakeout_abajo = (
            (float(anterior["low"]) < val or float(ultima["low"]) < val)
            and float(ultima["close"]) > val
            and vela_alcista
            and (poc > precio)
        )
        if fakeout_abajo and ctx_1h["sesgo"] != "VENTA":
            min_fake = min(float(anterior["low"]), float(ultima["low"]))
            sl_tecnico = float(min_fake - max(pip * 2, atr_ref * 0.25))
            riesgo_dist = max(abs(precio - sl_tecnico), sl_min_dist)
            sl_final = float(precio - riesgo_dist)
            tp_final = float(poc)
            if (tp_final - precio) >= riesgo_dist * 1.3:
                pips_pts, r_usd, r_pct = calcular_riesgo_001_lotes(symbol, precio, sl_final)
                apto = r_pct <= RIESGO_MAX_PCT_CUENTA
                be_nivel = float(precio + riesgo_dist)
                return {
                    "id_senal": "AMT_MEAN_REV_LONG",
                    "modelo": "Modelo 2: Reversión al POC tras Fakeout en VAL (AMT)",
                    "direccion": "COMPRA (LONG) 🟢",
                    "sl": sl_final,
                    "tp": tp_final,
                    "be": be_nivel,
                    "riesgo_usd": r_usd,
                    "riesgo_pct": r_pct,
                    "apto_cuenta": apto,
                    "descuento_pct": 0.0,
                    "nivel_fibo": 0,
                    "calidad": None,
                    "poc": poc,
                    "vah": vah,
                    "val": val,
                    "plan": (
                        f"1) Mercado en Balance/Rango: ruptura fallida (fakeout) bajo VAL (${val:,.4f}). No se opera la primera ruptura.\n"
                        f"2) Segundo impulso de rechazo entrando de nuevo al Área de Valor.\n"
                        f"3) SL ajustado con colchón bajo el fakeout en ${sl_final:,.4f} (~${r_usd:.2f} / {r_pct:.1f}% de $10).\n"
                        f"4) TP objetivo directo en el Punto de Control (POC) en ${tp_final:,.4f}.\n"
                        f"5) Break-Even: sube el SL a entrada al tocar ${be_nivel:,.4f} (+1R) si el CVD acompaña."
                    ),
                }

    # =========================================================================
    # 🆕 MODELO 3: FRACTAL EL SENSEI (OB/FVG 4H -> NUEVO OB EN 15m/5m + DESCUENTO)
    # 1) 4H marca dirección y zona principal. 2) El precio llega a esa zona.
    # 3) En la temporalidad baja se forma un nuevo OB tras el primer rechazo -> entrada ahí.
    # =========================================================================
    if USAR_CONTEXTO_4H and ctx_4h and en_sesion(("LONDRES", "NY")) and not exhaustado:
        # --- LONG ---
        zona4 = zona_4h_activa(ctx_4h, precio, "LONG")
        if ctx_4h["sesgo"] == "COMPRA" and zona4 and descuento_long_pct >= DESCUENTO_MIN_FIBO:
            ob_ltf_ok = ob and ob["tipo"] == "OB_ALCISTA" and (ob["low"] - atr_ref * 0.35) <= precio <= (ob["high"] + atr_ref * 0.35)
            if ob_ltf_ok and vela_alcista:
                nivel = nivel_fibo(descuento_long_pct)
                calidad = puntuar_calidad(nivel, zona4, lvn_cerca is not None, agresion_compra)
                if calidad >= CALIDAD_MINIMA:
                    sl_tecnico = float(ob["low"] - max(atr_ref * 0.35, pip * 2))
                    riesgo_dist = max(abs(precio - sl_tecnico), sl_min_dist)
                    sl_final = float(precio - riesgo_dist)
                    tp_final = float(max(impulso_high, precio + riesgo_dist * 2.5))
                    be_nivel = float(precio + riesgo_dist)
                    pips_pts, r_usd, r_pct = calcular_riesgo_001_lotes(symbol, precio, sl_final)
                    return {
                        "id_senal": "SENSEI_FRACTAL_LONG",
                        "modelo": "Modelo 3: Fractal El Sensei (OB 4H → OB bajo)",
                        "direccion": "COMPRA (LONG) 🟢",
                        "sl": sl_final,
                        "tp": tp_final,
                        "be": be_nivel,
                        "riesgo_usd": r_usd,
                        "riesgo_pct": r_pct,
                        "apto_cuenta": r_pct <= RIESGO_MAX_PCT_CUENTA,
                        "descuento_pct": round(descuento_long_pct, 1),
                        "nivel_fibo": nivel,
                        "calidad": calidad,
                        "poc": poc,
                        "vah": vah,
                        "val": val,
                        "plan": (
                            f"1) 4H ALCISTA y el precio llegó a {zona4['nombre']} (${zona4['low']:,.4f} - ${zona4['high']:,.4f}).\n"
                            f"2) En {timeframe} apareció un nuevo Order Block alcista (${ob['low']:,.4f} - ${ob['high']:,.4f}) con descuento del {descuento_long_pct:.1f}% (nivel {nivel}%).\n"
                            f"3) SL justo debajo del bloque en ${sl_final:,.4f} ({pips_pts} pips/pts -> ~${r_usd:.2f} / {r_pct:.1f}% de $10).\n"
                            f"4) TP en la zona opuesta/liquidez (${tp_final:,.4f}).\n"
                            f"5) Break-Even al tocar ${be_nivel:,.4f} (+1R)."
                        ),
                    }

        # --- SHORT ---
        zona4 = zona_4h_activa(ctx_4h, precio, "SHORT")
        if ctx_4h["sesgo"] == "VENTA" and zona4 and descuento_short_pct >= DESCUENTO_MIN_FIBO:
            ob_ltf_ok = ob and ob["tipo"] == "OB_BAJISTA" and (ob["low"] - atr_ref * 0.35) <= precio <= (ob["high"] + atr_ref * 0.35)
            if ob_ltf_ok and vela_bajista:
                nivel = nivel_fibo(descuento_short_pct)
                calidad = puntuar_calidad(nivel, zona4, lvn_cerca is not None, agresion_venta)
                if calidad >= CALIDAD_MINIMA:
                    sl_tecnico = float(ob["high"] + max(atr_ref * 0.35, pip * 2))
                    riesgo_dist = max(abs(sl_tecnico - precio), sl_min_dist)
                    sl_final = float(precio + riesgo_dist)
                    tp_final = float(min(impulso_low, precio - riesgo_dist * 2.5))
                    be_nivel = float(precio - riesgo_dist)
                    pips_pts, r_usd, r_pct = calcular_riesgo_001_lotes(symbol, precio, sl_final)
                    return {
                        "id_senal": "SENSEI_FRACTAL_SHORT",
                        "modelo": "Modelo 3: Fractal El Sensei (OB 4H → OB bajo)",
                        "direccion": "VENTA (SHORT) 🔴",
                        "sl": sl_final,
                        "tp": tp_final,
                        "be": be_nivel,
                        "riesgo_usd": r_usd,
                        "riesgo_pct": r_pct,
                        "apto_cuenta": r_pct <= RIESGO_MAX_PCT_CUENTA,
                        "descuento_pct": round(descuento_short_pct, 1),
                        "nivel_fibo": nivel,
                        "calidad": calidad,
                        "poc": poc,
                        "vah": vah,
                        "val": val,
                        "plan": (
                            f"1) 4H BAJISTA y el precio llegó a {zona4['nombre']} (${zona4['low']:,.4f} - ${zona4['high']:,.4f}).\n"
                            f"2) En {timeframe} apareció un nuevo Order Block bajista (${ob['low']:,.4f} - ${ob['high']:,.4f}) con descuento del {descuento_short_pct:.1f}% (nivel {nivel}%).\n"
                            f"3) SL justo encima del bloque en ${sl_final:,.4f} ({pips_pts} pips/pts -> ~${r_usd:.2f} / {r_pct:.1f}% de $10).\n"
                            f"4) TP en la zona opuesta/liquidez (${tp_final:,.4f}).\n"
                            f"5) Break-Even al tocar ${be_nivel:,.4f} (+1R)."
                        ),
                    }

    return None

# ==========================================
# 🛠️ OBTENCIÓN DE DATOS CON CACHÉ CANÓNICA
# ==========================================
def obtener_datos(symbol, timeframe, limit=100, ttl_segundos=60):
    canon = canonizar_simbolo(symbol)
    cache_key = f"{canon}_{timeframe}_{limit}"
    ahora = time.time()

    with CACHE_LOCK:
        cached = CACHE_DATOS_GLOBAL.get(cache_key)
        if cached and (ahora - cached["ts"] < ttl_segundos) and cached.get("es_real", False):
            df_cached = cached["df"].copy()
            df_cached.attrs["es_real"] = True
            return df_cached

    df = pd.DataFrame()
    es_real = False

    if symbol in CRYPTO_MARKETS:
        for ex in [e for e in (EXCHANGE_OKX, EXCHANGE_BINANCE) if e is not None]:
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
            except Exception:
                continue
    elif symbol in FOREX_MARKETS or canon == "XAUUSD":
        interval_map = {"5m": "5m", "15m": "15m", "1h": "60m", "4h": "60m"}  # 4h se arma desde 60m
        yf_interval = interval_map.get(timeframe, "60m")
        periodos_a_probar = ["5d", "1mo"] if timeframe == "5m" else ["1mo", "3mo"]
        simbolos_yf = ["XAUUSD=X", "GC=F"] if canon == "XAUUSD" else [symbol]

        for sym_try in simbolos_yf:
            for periodo in periodos_a_probar:
                try:
                    hist = yf.Ticker(sym_try).history(period=periodo, interval=yf_interval)
                    if not hist.empty and "Close" in hist.columns:
                        temp_df = hist.reset_index()
                        time_col = "Datetime" if "Datetime" in temp_df.columns else ("Date" if "Date" in temp_df.columns else temp_df.columns[0])
                        temp_df.rename(columns={
                            time_col: "timestamp",
                            "Open": "open",
                            "High": "high",
                            "Low": "low",
                            "Close": "close",
                            "Volume": "volume"
                        }, inplace=True)
                        temp_df["timestamp"] = _normalizar_timestamp(temp_df["timestamp"])
                        temp_df["volume"] = temp_df["volume"].fillna(0)
                        if timeframe == "4h":
                            temp_df = _resamplear_a_4h(temp_df)
                        temp_df = temp_df.dropna(subset=["open", "high", "low", "close"]).tail(limit).copy()
                        if not temp_df.empty and float(temp_df["close"].iloc[-1]) > 0:
                            df = temp_df
                            es_real = True
                            break
                except Exception:
                    continue
            if es_real:
                break

    if df.empty or not all(col in df.columns for col in ["open", "high", "low", "close", "volume", "timestamp"]):
        freq_map = {"5m": "5min", "15m": "15min", "1h": "1h", "4h": "4h"}
        fechas = pd.date_range(end=datetime.now(), periods=limit, freq=freq_map.get(timeframe, "15min"))
        base_val = 2650.0 if "XAU" in canon else (1.085 if "EUR" in canon else 150.0)
        rng = np.random.default_rng(abs(hash(canon + timeframe)) % (2**32))
        returns = rng.normal(0.0001, 0.002, limit)
        prices = base_val * np.cumprod(1 + returns)

        df = pd.DataFrame({
            "timestamp": fechas,
            "open": prices * (1 + rng.normal(0, 0.0005, limit)),
            "high": prices * (1 + abs(rng.normal(0, 0.001, limit))),
            "low": prices * (1 - abs(rng.normal(0, 0.001, limit))),
            "close": prices,
            "volume": rng.integers(1000, 10000, limit)
        })
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

    if es_real:
        with CACHE_LOCK:
            CACHE_DATOS_GLOBAL[cache_key] = {"ts": ahora, "df": df.copy(), "es_real": True}

    return df

# ==========================================
# 🤖 MOTOR EN FONDO: FRACTALIDAD (4H/1H CONTEXTO -> 15M/5M GATILLO ÚNICO)
# ==========================================
def ejecutar_logica_autopiloto(es_hilo_fondo=False):
    global AUTOPILOTO_STATUS, ALERTAS_ENVIADAS_GLOBAL, DIAGNOSTICO

    if not es_hilo_fondo:
        AUTOPILOTO_STATUS = "Ejecutando barrido institucional (4h/1h contexto -> 15m/5m)..."

    alertas_nuevas = 0
    vistos_en_ciclo = set()
    diag = {
        "activos_con_mercado_cerrado": 0, "sin_datos_reales": 0, "datos_viejos": 0,
        "evaluaciones": 0, "senales_detectadas": 0, "bloqueadas_por_riesgo": 0,
        "en_cooldown": 0, "fallo_telegram": 0, "enviadas": 0,
    }

    for symbol in ALL_TRACKED_SYMBOLS:
        canon = canonizar_simbolo(symbol)
        if canon in vistos_en_ciclo:
            continue
        vistos_en_ciclo.add(canon)

        # 🆕 Forex/oro cerrados (fin de semana): no se analiza ni se alerta
        if not mercado_abierto(symbol):
            diag["activos_con_mercado_cerrado"] += 1
            continue

        tipo = "CRIPTO" if symbol in CRYPTO_MARKETS else ("METAL (ORO)" if canon == "XAUUSD" else "FOREX")

        try:
            # Paso 1: Temporalidad Alta (4h y 1h) manda la dirección (no dispara alertas sueltas)
            df_1h = obtener_datos(symbol, "1h", limit=90, ttl_segundos=90)
            if df_1h.empty or not df_1h.attrs.get("es_real", False):
                diag["sin_datos_reales"] += 1
                continue
            if not datos_frescos(df_1h, "1h"):
                diag["datos_viejos"] += 1
                continue
            ctx_1h = analizar_contexto_macro_1h(df_1h, symbol)

            ctx_4h = None
            if USAR_CONTEXTO_4H:
                df_4h = obtener_datos(symbol, "4h", limit=90, ttl_segundos=300)
                if not df_4h.empty and df_4h.attrs.get("es_real", False):
                    ctx_4h = analizar_contexto_4h(df_4h)
            sesgo_4h = ctx_4h["sesgo"] if ctx_4h else "N/D"

            # Paso 2: Refinamiento en temporalidades bajas (prioriza 15m y luego 5m; máximo 1 alerta por activo)
            for tf in ("15m", "5m"):
                if not es_hilo_fondo:
                    AUTOPILOTO_STATUS = f"Evaluando {canon} [4h:{sesgo_4h} 1h:{ctx_1h['sesgo']} -> {tf}]..."

                df_tf = obtener_datos(symbol, tf, limit=90, ttl_segundos=60)
                if df_tf.empty or not df_tf.attrs.get("es_real", False):
                    diag["sin_datos_reales"] += 1
                    continue
                if not datos_frescos(df_tf, tf):
                    diag["datos_viejos"] += 1
                    continue

                # Solo velas cerradas: la vela en formación puede cambiar y borrar la señal
                df_eval = df_tf.iloc[:-1] if SOLO_VELA_CERRADA else df_tf

                diag["evaluaciones"] += 1
                resultado = evaluar_modelo_fabio_sensei(df_eval, symbol=symbol, timeframe=tf, ctx_1h=ctx_1h, ctx_4h=ctx_4h)
                if resultado:
                    diag["senales_detectadas"] += 1
                    if FILTRAR_SOLO_RIESGO_APTO_TG and not resultado.get("apto_cuenta", True):
                        diag["bloqueadas_por_riesgo"] += 1
                        continue

                    precio = float(df_eval.iloc[-1]["close"])
                    r_usd = resultado["riesgo_usd"]
                    r_pct = resultado["riesgo_pct"]

                    calidad = resultado.get("calidad")
                    calidad_txt = f"{calidad}/5" if calidad is not None else "n/a (reversión)"
                    desc_txt = (
                        f"📐 *Descuento Fibo:* `{resultado['descuento_pct']:.1f}%` (nivel `{resultado.get('nivel_fibo', 0)}%`)\n"
                        if resultado.get("descuento_pct", 0) > 0 else ""
                    )

                    msg = (
                        f"🏛️ *SEÑAL INSTITUCIONAL (AMT + ORDER BLOCK)*\n"
                        f"🌐 *Mercado:* `{tipo}` | *Activo:* `{canon}` | *Entrada:* `{tf}` (4h: `{sesgo_4h}` | 1h: `{ctx_1h['sesgo']}`)\n"
                        f"🧠 *Modelo:* {resultado['modelo']}\n"
                        f"🧭 *Acción:* *{resultado['direccion']}*\n"
                        f"⭐ *Calidad:* `{calidad_txt}`\n"
                        f"{desc_txt}"
                        f"💲 *Precio:* `${precio:,.4f}` | *POC:* `${resultado['poc']:,.4f}` (VAL `${resultado['val']:,.4f}` / VAH `${resultado['vah']:,.4f}`)\n"
                        f"🛡️ *SL:* `${resultado['sl']:,.4f}` | *TP:* `${resultado['tp']:,.4f}` | *BE (+1R):* `${resultado['be']:,.4f}`\n"
                        f"💵 *Riesgo 0.01 lot:* `~${r_usd:.2f} USD` (`{r_pct:.1f}%` de ${SALDO_CUENTA_USD:.0f}) ✅\n"
                        f"📋 *Ejecución:*\n{resultado['plan']}"
                    )

                    # Cooldown de 20 minutos por activo y dirección (evita spam entre 15m y 5m)
                    clave_alerta = f"{canon}_{resultado['id_senal']}"
                    ahora = time.time()

                    with ALERTAS_LOCK:  # evita duplicados entre el hilo de fondo y el botón manual
                        if ahora - ALERTAS_ENVIADAS_GLOBAL.get(clave_alerta, 0) > 1200:
                            if enviar_telegram(msg):
                                ALERTAS_ENVIADAS_GLOBAL[clave_alerta] = ahora
                                alertas_nuevas += 1
                            else:
                                diag["fallo_telegram"] += 1
                        else:
                            diag["en_cooldown"] += 1
                    break  # Ya procesó la mejor oportunidad de este activo

            time.sleep(0.12)
        except Exception:
            continue

    diag["enviadas"] = alertas_nuevas
    diag["hora_utc"] = datetime.now(timezone.utc).strftime("%H:%M:%S")
    DIAGNOSTICO = diag

    if not es_hilo_fondo:
        AUTOPILOTO_STATUS = f"✅ Barrido institucional listo. Alertas enviadas: {alertas_nuevas}"
    else:
        AUTOPILOTO_STATUS = "⚡ Motor AMT + Order Block activo (4h/1h contexto -> 15m/5m cada 30s)..."

@st.cache_resource
def iniciar_hilo_autopiloto():
    def loop():
        time.sleep(5)
        while True:
            try:
                ejecutar_logica_autopiloto(es_hilo_fondo=True)
            except Exception:
                pass
            time.sleep(30)
    thread = threading.Thread(target=loop, daemon=True)
    thread.start()
    return thread

iniciar_hilo_autopiloto()

# ==========================================
# 📊 INTERFAZ GRÁFICA PRINCIPAL
# ==========================================
st.markdown("<h1 style='text-align: center; color: #00FFAA;'>🏦 INSTITUTIONAL QUANT TERMINAL — AMT & ORDER BLOCKS</h1>", unsafe_allow_html=True)
st.markdown("<p style='text-align: center; color: gray;'>Teoría de Subasta (POC / VAH / VAL / LVN) + Fractalidad (4h/1h -> 15m/5m) + Order Blocks + Descuento Fibonacci ≥ 50%</p>", unsafe_allow_html=True)
st.markdown("---")

st.sidebar.header("🌐 Configuración del Gráfico")
symbol = st.sidebar.selectbox("Ver Gráfico de:", ALL_TRACKED_SYMBOLS)
timeframe = st.sidebar.selectbox("Temporalidad", ["15m", "5m", "1h"])

st.sidebar.markdown("---")
st.sidebar.subheader("📡 Estado del Motor")
st.sidebar.info(AUTOPILOTO_STATUS)
if ULTIMO_ERROR_TG:
    st.sidebar.caption(f"⚠️ Telegram: {ULTIMO_ERROR_TG}")
st.sidebar.caption(
    f"Filtro de sesiones: {'ON' if USAR_FILTRO_SESIONES else 'OFF'} | "
    f"Calidad mínima: {CALIDAD_MINIMA}/5 | Vela cerrada: {'ON' if SOLO_VELA_CERRADA else 'OFF'}"
)

if st.sidebar.button("⚡ Forzar Escaneo Ahora"):
    with st.spinner("Analizando contexto 4h/1h, perfil de volumen y bloques..."):
        ejecutar_logica_autopiloto(es_hilo_fondo=False)
        st.sidebar.success("¡Escaneo completado!")

if st.sidebar.button("📨 Enviar mensaje de prueba"):
    if enviar_telegram("🧪 Prueba de conexión Quant Terminal: Telegram operativo."):
        st.sidebar.success("Mensaje de prueba enviado ✅")
    else:
        st.sidebar.error(f"No se pudo enviar: {ULTIMO_ERROR_TG}")

with st.sidebar.expander("🔍 ¿Por qué no llegan alertas?"):
    st.caption(f"Token: {'configurado ✅' if TELEGRAM_TOKEN else 'FALTA ❌'} | Chat ID: {'configurado ✅' if TELEGRAM_CHAT_ID else 'FALTA ❌'}")
    st.caption(f"Forex/oro abierto: {'sí ✅' if mercado_abierto('EURUSD=X') else 'NO (cerrado) 🕒'} | Cripto: 24/7")
    if DIAGNOSTICO:
        st.caption("Último barrido (qué pasó con cada activo):")
        st.json(DIAGNOSTICO)
    else:
        st.caption("Aún no hay un barrido completo.")

@st.fragment(run_every="10s")
def render_trading_panel(symbol, timeframe):
    df = obtener_datos(symbol, timeframe, limit=120, ttl_segundos=30)
    if df.empty:
        st.error(f"No se pudieron cargar los datos para {symbol}.")
        return

    if not df.attrs.get("es_real", False):
        st.warning("⚠️ Datos de respaldo activos por límite temporal de proveedor externo. No se enviarán alertas de Telegram con estos datos.")

    if not mercado_abierto(symbol) or not datos_frescos(df, timeframe):
        st.warning("🕒 Mercado cerrado o datos sin actualizar: el gráfico muestra la última sesión y no se enviarán alertas de este activo.")

    perfil_panel = calcular_perfil_volumen_amt(df, lookback=36)
    lvns_panel = calcular_lvns(df, lookback=18, bins=12)

    col_grafico, col_paneles = st.columns([2.5, 1])
    with col_grafico:
        fig = make_subplots(rows=2, cols=1, shared_xaxes=True, vertical_spacing=0.03, row_width=[0.2, 0.8])
        fig.add_trace(go.Candlestick(
            x=df["timestamp"], open=df["open"], high=df["high"], low=df["low"], close=df["close"],
            increasing_line_color="#00FFAA", decreasing_line_color="#FF3333", name="Precio"
        ), row=1, col=1)
        fig.add_trace(go.Scatter(x=df["timestamp"], y=df["vwap"], line=dict(color="#FF00FF", width=2), name="VWAP"), row=1, col=1)
        fig.add_trace(go.Scatter(x=df["timestamp"], y=df["ema_fast"], line=dict(color="#FFFF00", width=1.2), name="EMA 9"), row=1, col=1)
        fig.add_trace(go.Scatter(x=df["timestamp"], y=df["ema_slow"], line=dict(color="#00BFFF", width=1.2), name="EMA 21"), row=1, col=1)

        # Niveles AMT (POC, VAH, VAL) dibujados directamente en el gráfico
        fig.add_hline(y=perfil_panel["poc"], line_dash="solid", line_color="#FFA500", annotation_text="POC", row=1, col=1)
        fig.add_hline(y=perfil_panel["vah"], line_dash="dash", line_color="#00FFAA", annotation_text="VAH", row=1, col=1)
        fig.add_hline(y=perfil_panel["val"], line_dash="dash", line_color="#FF5555", annotation_text="VAL", row=1, col=1)

        # 🆕 Nodos de Bajo Volumen (LVN) del impulso reciente
        for lvn in lvns_panel:
            fig.add_hline(y=lvn, line_dash="dot", line_color="#AAAAAA", annotation_text="LVN", row=1, col=1)

        colors = ["#00FFAA" if r["close"] >= r["open"] else "#FF3333" for _, r in df.iterrows()]
        fig.add_trace(go.Bar(x=df["timestamp"], y=df["volume"], marker_color=colors, name="Volumen"), row=2, col=1)
        fig.update_layout(template="plotly_dark", xaxis_rangeslider_visible=False, height=600, margin=dict(l=10, r=10, t=10, b=10), showlegend=True)
        st.plotly_chart(fig, use_container_width=True)

    with col_paneles:
        st.markdown("### 📊 Lectura AMT & Fractalidad")
        ultima = df.iloc[-1]
        anterior = df.iloc[-2]
        precio_actual = float(ultima["close"])
        st.metric(label=f"{canonizar_simbolo(symbol)} [{timeframe}]", value=f"${precio_actual:,.4f}", delta=f"{precio_actual - float(anterior['close']):,.4f}")

        df_1h_panel = obtener_datos(symbol, "1h", limit=90, ttl_segundos=90)
        ctx_1h_panel = analizar_contexto_macro_1h(df_1h_panel, symbol)

        ctx_4h_panel = None
        if USAR_CONTEXTO_4H:
            df_4h_panel = obtener_datos(symbol, "4h", limit=90, ttl_segundos=300)
            ctx_4h_panel = analizar_contexto_4h(df_4h_panel)
        sesgo_4h_panel = ctx_4h_panel["sesgo"] if ctx_4h_panel else "N/D"

        st.info(f"🧭 Contexto 4h: **{sesgo_4h_panel}** | 1h: **{ctx_1h_panel['sesgo']}** | Estado AMT: **{perfil_panel['estado']}**")
        st.caption(f"POC: `${perfil_panel['poc']:,.4f}` | VAL: `${perfil_panel['val']:,.4f}` | VAH: `${perfil_panel['vah']:,.4f}`")

        if ctx_4h_panel:
            if ctx_4h_panel["ob"]:
                ob4 = ctx_4h_panel["ob"]
                st.caption(f"Bloque 4H: `{ob4['tipo']}` (`${ob4['low']:,.4f}` - `${ob4['high']:,.4f}`)")
            for lado in ("LONG", "SHORT"):
                z4 = zona_4h_activa(ctx_4h_panel, precio_actual, lado)
                if z4:
                    st.caption(f"📍 El precio está en {z4['nombre']}")

        if lvns_panel:
            st.caption("LVN: " + " | ".join(f"`${x:,.4f}`" for x in lvns_panel[:4]))

        ob_panel = detectar_order_block_institucional(df)
        if ob_panel:
            st.caption(f"Bloque detectado: `{ob_panel['tipo']}` (`${ob_panel['low']:,.4f}` - `${ob_panel['high']:,.4f}`)")

        fvg_panel = detectar_fvg_3_velas(df)
        if fvg_panel:
            st.caption(f"Imbalance FVG: `{fvg_panel['tipo']}` (`${fvg_panel['zona_low']:,.4f}` - `${fvg_panel['zona_high']:,.4f}`)")

        st.markdown("---")
        st.markdown("### 🎯 Gatillo Institucional")
        tf_eval = "15m" if timeframe == "1h" else timeframe
        df_eval = obtener_datos(symbol, tf_eval, limit=90, ttl_segundos=60) if tf_eval != timeframe else df
        mercado_ok = mercado_abierto(symbol) and datos_frescos(df_eval, tf_eval)
        senal = evaluar_modelo_fabio_sensei(df_eval, symbol=symbol, timeframe=tf_eval, ctx_1h=ctx_1h_panel, ctx_4h=ctx_4h_panel) if mercado_ok else None
        if not mercado_ok:
            st.caption("🕒 Mercado cerrado o sin datos nuevos: no se evalúan señales.")
        if senal:
            calidad = senal.get("calidad")
            calidad_txt = f"{calidad}/5" if calidad is not None else "n/a"
            st.success(f"**{senal['modelo']}**\n\n**Acción:** {senal['direccion']} | **Calidad:** {calidad_txt}\n\n{senal['plan']}")
        elif mercado_ok:
            st.caption("Esperando: (a) salida de balance + retroceso a LVN/OB/FVG, (b) fakeout en extremo de rango, o (c) OB/FVG 4H con nuevo OB en temporalidad baja. Descuento ≥50% en todos.")

render_trading_panel(symbol, timeframe)
