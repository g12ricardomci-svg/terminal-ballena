# -*- coding: utf-8 -*-
import streamlit as st
import ccxt
import yfinance as yf
import pandas as pd
import numpy as np
import plotly.graph_objects as go
from plotly.subplots import make_subplots
import requests
import json
import xml.etree.ElementTree as ET
import time
import threading
from datetime import datetime, timezone

# ==========================================
# ⚙️ CONFIGURACIÓN GLOBAL, BANK ($10 USD) & CREDENCIALES
# ==========================================
TELEGRAM_TOKEN = "9148434925:AAH_AuswS-GGPHadFBwcrDOUUIwBFbXvtDs"
TELEGRAM_CHAT_ID = "1496321437"
BANK_USD = 10.0
RIESGO_POR_OPERACION = 0.02  # 2% = $0.20 USD por operación

st.set_page_config(page_title="Institutional Quant Terminal — Intradía 1H", layout="wide", page_icon="🏦")

CRYPTO_MARKETS = [
    "BTC/USDT", "ETH/USDT", "SOL/USDT", "BNB/USDT",
    "XRP/USDT", "ADA/USDT", "AVAX/USDT", "DOGE/USDT", "LINK/USDT"
]
FOREX_MARKETS = [
    "EURUSD=X", "GBPUSD=X", "USDJPY=X", "USDCHF=X", "AUDUSD=X",
    "USDCAD=X", "NZDUSD=X", "EURJPY=X", "GBPJPY=X", "EURGBP=X"
]
ORO_MARKETS = ["XAUUSD=X"]
ALL_TRACKED_SYMBOLS = CRYPTO_MARKETS + FOREX_MARKETS + ORO_MARKETS


def _crear_exchange(ex_id):
    try:
        return getattr(ccxt, ex_id)({"enableRateLimit": True})
    except Exception:
        return None


EXCHANGES_CRIPTO = [e for e in (_crear_exchange("okx"), _crear_exchange("binance")) if e is not None]

ALERTAS_ENVIADAS_GLOBAL = {}
CACHE_DATOS_GLOBAL = {}
CACHE_NOTICIAS_GLOBAL = {"ts": 0, "items": []}
CACHE_LOCK = threading.Lock()
AUTOPILOTO_STATUS = "Motor Intradía 1H (Bank $10) activo: Escaneando cada 30s..."


# ==========================================
# 🕒 SESIONES DE BOLSAS DE VALORES (UTC)
# ==========================================
def obtener_sesion_actual():
    ahora_utc = datetime.now(timezone.utc)
    dia = ahora_utc.weekday()
    hora = ahora_utc.hour

    if dia == 5 or (dia == 6 and hora < 22) or (dia == 4 and hora >= 22):
        return "FIN DE SEMANA (Forex/Oro cerrado • Cripto 24/7)", False

    sesiones = []
    if 0 <= hora < 9:
        sesiones.append("Tokio")
    if 8 <= hora < 17:
        sesiones.append("Londres")
    if 13 <= hora < 22:
        sesiones.append("Nueva York")
    if hora >= 22 or hora < 7:
        sesiones.append("Sídney")

    if 13 <= hora < 17:
        return "🔥 SOLAPAMIENTO LONDRES + NY (Máxima Liquidez)", True

    texto = " + ".join(sesiones) if sesiones else "Transición"
    return f"Sesión Activa: {texto}", True


# ==========================================
# 📰 NOTICIAS EN VIVO
# ==========================================
def obtener_noticias_en_vivo(ttl_segundos=180):
    ahora = time.time()
    with CACHE_LOCK:
        if CACHE_NOTICIAS_GLOBAL["items"] and (ahora - CACHE_NOTICIAS_GLOBAL["ts"] < ttl_segundos):
            return CACHE_NOTICIAS_GLOBAL["items"]

    feeds = [
        ("Investing Forex", "https://www.investing.com/rss/news_1.rss"),
        ("Investing Oro", "https://www.investing.com/rss/news_11.rss"),
        ("Cointelegraph", "https://cointelegraph.com/rss"),
        ("FXStreet", "https://www.fxstreet.com/rss/news"),
    ]
    noticias = []
    headers = {"User-Agent": "Mozilla/5.0"}

    for fuente, url in feeds:
        try:
            resp = requests.get(url, headers=headers, timeout=5)
            if resp.status_code != 200:
                continue
            root = ET.fromstring(resp.content)
            for item in root.findall(".//item")[:5]:
                titulo = (item.findtext("title") or "").strip()
                link = (item.findtext("link") or "").strip()
                pub_date = (item.findtext("pubDate") or "").strip()
                if titulo:
                    noticias.append({
                        "fuente": fuente,
                        "titulo": titulo,
                        "link": link,
                        "fecha": pub_date[:22] if pub_date else "Reciente"
                    })
        except Exception:
            continue

    if noticias:
        with CACHE_LOCK:
            CACHE_NOTICIAS_GLOBAL["ts"] = ahora
            CACHE_NOTICIAS_GLOBAL["items"] = noticias
    return noticias


# ==========================================
# 📲 TELEGRAM UTF-8
# ==========================================
def enviar_telegram(mensaje):
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    payload = {"chat_id": TELEGRAM_CHAT_ID, "text": mensaje, "parse_mode": "Markdown"}
    headers = {"Content-Type": "application/json; charset=utf-8"}
    try:
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        resp = requests.post(url, data=data, headers=headers, timeout=5)
        return resp.status_code == 200
    except Exception:
        return False


# ==========================================
# 🧠 MOTOR MATEMÁTICO INTRADÍA (1H -> 5m/15m)
# ==========================================
def normalizar_timestamp(serie):
    ts = pd.to_datetime(serie)
    if getattr(ts.dt, "tz", None) is not None:
        ts = ts.dt.tz_convert(None)
    return ts


def calcular_vwap(df):
    if df.empty or "volume" not in df.columns or df["volume"].sum() == 0:
        return df["close"] if not df.empty else pd.Series(dtype=float)
    tp = (df["high"] + df["low"] + df["close"]) / 3.0
    return (df["volume"] * tp).cumsum() / df["volume"].cumsum()


def calcular_rsi(series, period=14):
    delta = series.diff()
    gain = delta.clip(lower=0).ewm(alpha=1 / period, adjust=False).mean()
    loss = (-delta.clip(upper=0)).ewm(alpha=1 / period, adjust=False).mean()
    rs = gain / loss.replace(0, np.nan)
    return (100 - (100 / (1 + rs))).fillna(50.0)


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
        val = dx.ewm(span=period, adjust=False).mean().iloc[-1]
        return 25.0 if pd.isna(val) else float(val)
    except Exception:
        return 25.0


def calcular_cvd_proxy(df):
    rango = (df["high"] - df["low"]).replace(0, np.nan)
    delta_ratio = ((df["close"] - df["open"]) / rango).fillna(0.0)
    return (df["volume"] * delta_ratio).cumsum()


def enriquecer_indicadores(df):
    if df.empty or len(df) <= 5:
        return df
    df["vwap"] = calcular_vwap(df)
    df["vol_ma"] = df["volume"].rolling(window=min(20, len(df))).mean()
    df["ema_fast"] = df["close"].ewm(span=9, adjust=False).mean()
    df["ema_slow"] = df["close"].ewm(span=21, adjust=False).mean()
    df["rsi"] = calcular_rsi(df["close"], 14)
    sma20 = df["close"].rolling(window=20).mean()
    std20 = df["close"].rolling(window=20).std()
    df["bb_upper"] = sma20 + (std20 * 2.0)
    df["bb_lower"] = sma20 - (std20 * 2.0)
    df["adx"] = calcular_adx(df)
    df["cvd"] = calcular_cvd_proxy(df)
    return df


def calcular_volume_profile_amt(df, bins=24):
    if df.empty or len(df) < 15:
        return None
    p_min, p_max = float(df["low"].min()), float(df["high"].max())
    if p_max <= p_min:
        return None

    edges = np.linspace(p_min, p_max, bins + 1)
    vol_bin = np.zeros(bins)
    tp = ((df["high"] + df["low"] + df["close"]) / 3.0).values
    vols = df["volume"].values
    if float(np.sum(vols)) <= 0:
        vols = np.maximum((df["high"] - df["low"]).values, 1e-6)
    indices = np.clip(np.digitize(tp, edges) - 1, 0, bins - 1)
    for idx, v in zip(indices, vols):
        vol_bin[idx] += float(v)

    total_vol = vol_bin.sum()
    if total_vol <= 0:
        return None

    poc_idx = int(np.argmax(vol_bin))
    poc_price = float((edges[poc_idx] + edges[poc_idx + 1]) / 2.0)

    target_vol = total_vol * 0.70
    acc_vol = vol_bin[poc_idx]
    low_idx, high_idx = poc_idx, poc_idx
    while acc_vol < target_vol and (low_idx > 0 or high_idx < bins - 1):
        v_down = vol_bin[low_idx - 1] if low_idx > 0 else -1
        v_up = vol_bin[high_idx + 1] if high_idx < bins - 1 else -1
        if v_up >= v_down and high_idx < bins - 1:
            high_idx += 1
            acc_vol += vol_bin[high_idx]
        elif low_idx > 0:
            low_idx -= 1
            acc_vol += vol_bin[low_idx]
        else:
            break

    val_price = float(edges[low_idx])
    vah_price = float(edges[high_idx + 1])
    precio = float(df["close"].iloc[-1])
    estado = "OUT_OF_BALANCE" if (precio > vah_price or precio < val_price) else "BALANCE"
    return {"poc": poc_price, "vah": vah_price, "val": val_price, "estado": estado}


def calcular_gestion_bank_10(precio, sl, mercado):
    riesgo_usd = BANK_USD * RIESGO_POR_OPERACION
    dist_sl = max(abs(precio - sl), 1e-9)
    if mercado == "CRIPTO":
        tamano = riesgo_usd / dist_sl
        return f"Riesgo máx: `${riesgo_usd:.2f}` (2% de $10) | Tamaño aprox: `{tamano:.4f}` unidades"
    return f"Riesgo máx: `${riesgo_usd:.2f}` (2% de $10) | Usa lotaje mínimo `0.01`"


def evaluar_estrategia_intradia_1h(df, adx_min=25.0, sesgo_1h="NEUTRO", vp_info=None, rr_mult=2.0):
    """
    Estrategia Intradía Máximo 1H (1H dirección -> 5m/15m/1h entrada):
    1) Seguimiento de Tendencia + Ruptura (ADX >= adx_min): solo a favor del sesgo 1H.
    2) Operativa en Rangos (ADX < adx_min): RSI + Bandas de Bollinger / VAH-VAL hacia POC.
    """
    if df.empty or len(df) < 25 or not vp_info:
        return None

    u, ant = df.iloc[-1], df.iloc[-2]
    precio = float(u["close"])
    adx_val = float(u.get("adx", 25.0))
    rsi_val = float(u.get("rsi", 50.0))
    vwap_val = float(u.get("vwap", precio))
    ema_fast, ema_slow = float(u.get("ema_fast", precio)), float(u.get("ema_slow", precio))
    bb_up = float(u.get("bb_upper", precio * 1.01))
    bb_low = float(u.get("bb_lower", precio * 0.99))

    atr_ref = max(float((df["high"].iloc[-15:-1] - df["low"].iloc[-15:-1]).mean()), precio * 0.0015)
    cvd_alcista = float(u.get("cvd", 0.0)) >= float(ant.get("cvd", 0.0))
    cvd_bajista = float(u.get("cvd", 0.0)) <= float(ant.get("cvd", 0.0))
    poc, vah, val = vp_info["poc"], vp_info["vah"], vp_info["val"]

    # 1) SEGUIMIENTO DE TENDENCIA / RUPTURA A FAVOR DE 1H
    if adx_val >= adx_min:
        if sesgo_1h == "ALCISTA" and precio > ema_fast > ema_slow and precio >= vwap_val and cvd_alcista:
            sl = min(float(df["low"].iloc[-5:].min()), precio - atr_ref * 1.0)
            riesgo = max(precio - sl, atr_ref * 0.8)
            sl = precio - riesgo
            tp = precio + riesgo * rr_mult
            return {
                "id_senal": "TENDENCIA_1H_LONG",
                "modelo": "Tendencia / Ruptura Intradía 1H",
                "direccion": "COMPRA (LONG) 🟢",
                "sl": sl,
                "tp": tp,
                "plan": (
                    f"1) Dirección 1H Alcista + EMA9>EMA21 sobre VWAP (ADX {adx_val:.1f}).\n"
                    f"2) Mover SL a Break-Even al llegar a 1:1 (${precio + riesgo:,.4f})."
                )
            }

        if sesgo_1h == "BAJISTA" and precio < ema_fast < ema_slow and precio <= vwap_val and cvd_bajista:
            sl = max(float(df["high"].iloc[-5:].max()), precio + atr_ref * 1.0)
            riesgo = max(sl - precio, atr_ref * 0.8)
            sl = precio + riesgo
            tp = precio - riesgo * rr_mult
            return {
                "id_senal": "TENDENCIA_1H_SHORT",
                "modelo": "Tendencia / Ruptura Intradía 1H",
                "direccion": "VENTA (SHORT) 🔴",
                "sl": sl,
                "tp": tp,
                "plan": (
                    f"1) Dirección 1H Bajista + EMA9<EMA21 bajo VWAP (ADX {adx_val:.1f}).\n"
                    f"2) Mover SL a Break-Even al llegar a 1:1 (${precio - riesgo:,.4f})."
                )
            }

    # 2) OPERATIVA EN RANGOS (ADX BAJO: RSI + BOLLINGER + POC)
    if adx_val < adx_min:
        if (precio <= bb_low or precio <= val) and rsi_val <= 35.0 and cvd_alcista:
            sl = float(u["low"]) - atr_ref * 0.5
            riesgo = max(precio - sl, atr_ref * 0.6)
            tp = max(poc, precio + riesgo * 1.5)
            return {
                "id_senal": "RANGO_LONG",
                "modelo": "Rango Intradía (RSI + Bollinger + POC)",
                "direccion": "COMPRA (LONG) 🟢",
                "sl": sl,
                "tp": tp,
                "plan": f"1) Rango lateral (ADX {adx_val:.1f}) en sobreventa (RSI {rsi_val:.1f}).\n2) Objetivo hacia POC (${poc:,.4f})."
            }

        if (precio >= bb_up or precio >= vah) and rsi_val >= 65.0 and cvd_bajista:
            sl = float(u["high"]) + atr_ref * 0.5
            riesgo = max(sl - precio, atr_ref * 0.6)
            tp = min(poc, precio - riesgo * 1.5)
            return {
                "id_senal": "RANGO_SHORT",
                "modelo": "Rango Intradía (RSI + Bollinger + POC)",
                "direccion": "VENTA (SHORT) 🔴",
                "sl": sl,
                "tp": tp,
                "plan": f"1) Rango lateral (ADX {adx_val:.1f}) en sobrecompra (RSI {rsi_val:.1f}).\n2) Objetivo hacia POC (${poc:,.4f})."
            }

    return None


# ==========================================
# 🌐 MÓDULOS ESPECIALIZADOS POR MERCADO
# ==========================================
def obtener_datos_mercado(symbol, timeframe, limit=100, ttl_segundos=60):
    cache_key = f"{symbol}_{timeframe}_{limit}"
    ahora = time.time()
    with CACHE_LOCK:
        cached = CACHE_DATOS_GLOBAL.get(cache_key)
        if cached and (ahora - cached["ts"] < ttl_segundos) and cached.get("es_real", False):
            df_c = cached["df"].copy()
            df_c.attrs["es_real"] = True
            return df_c

    df = pd.DataFrame()
    es_real = False

    if symbol in CRYPTO_MARKETS:
        for ex in EXCHANGES_CRIPTO:
            try:
                ohlcv = ex.fetch_ohlcv(symbol, timeframe, limit=limit)
                if ohlcv:
                    temp = pd.DataFrame(ohlcv, columns=["timestamp", "open", "high", "low", "close", "volume"])
                    temp["timestamp"] = normalizar_timestamp(pd.to_datetime(temp["timestamp"], unit="ms"))
                    temp = temp.dropna(subset=["open", "high", "low", "close"]).tail(limit).copy()
                    if not temp.empty and float(temp["close"].iloc[-1]) > 0:
                        df, es_real = temp, True
                        break
            except Exception:
                continue
    else:
        interval_map = {"5m": "5m", "15m": "15m", "1h": "60m"}
        yf_interval = interval_map.get(timeframe, "60m")
        periodos = ["5d", "1mo"] if timeframe == "5m" else ["1mo", "3mo"]
        simbolos_yf = ["XAUUSD=X", "GC=F"] if symbol in ORO_MARKETS else [symbol]

        for sym_try in simbolos_yf:
            for periodo in periodos:
                try:
                    hist = yf.Ticker(sym_try).history(period=periodo, interval=yf_interval)
                    if not hist.empty and "Close" in hist.columns:
                        temp = hist.reset_index()
                        time_col = "Datetime" if "Datetime" in temp.columns else ("Date" if "Date" in temp.columns else temp.columns[0])
                        temp.rename(columns={
                            time_col: "timestamp", "Open": "open", "High": "high",
                            "Low": "low", "Close": "close", "Volume": "volume"
                        }, inplace=True)
                        temp["timestamp"] = normalizar_timestamp(temp["timestamp"])
                        temp["volume"] = temp["volume"].fillna(0)
                        temp = temp.dropna(subset=["open", "high", "low", "close"]).tail(limit).copy()
                        if not temp.empty and float(temp["close"].iloc[-1]) > 0:
                            df, es_real = temp, True
                            break
                except Exception:
                    continue
            if es_real:
                break

    if not df.empty:
        df = enriquecer_indicadores(df)
        df.attrs["es_real"] = es_real
        if es_real:
            with CACHE_LOCK:
                CACHE_DATOS_GLOBAL[cache_key] = {"ts": ahora, "df": df.copy(), "es_real": True}
    return df


def obtener_sesgo_1h(symbol):
    """Dirección principal en gráfico de 1H (máximo 1 hora)."""
    df_1h = obtener_datos_mercado(symbol, "1h", limit=60, ttl_segundos=120)
    if df_1h.empty or len(df_1h) < 20:
        return "NEUTRO"
    u = df_1h.iloc[-1]
    if float(u["close"]) > float(u["ema_slow"]) and float(u["ema_fast"]) > float(u["ema_slow"]):
        return "ALCISTA"
    if float(u["close"]) < float(u["ema_slow"]) and float(u["ema_fast"]) < float(u["ema_slow"]):
        return "BAJISTA"
    return "NEUTRO"


def analizar_por_mercado(symbol, timeframe, sesgo_1h="NEUTRO"):
    df = obtener_datos_mercado(symbol, timeframe, limit=85, ttl_segundos=60)
    if df.empty or not df.attrs.get("es_real", False):
        return "DESCONOCIDO", 25.0, df, None, None

    vp_info = calcular_volume_profile_amt(df)

    if symbol in CRYPTO_MARKETS:
        adx_min = 25.0
        senal = evaluar_estrategia_intradia_1h(df, adx_min=adx_min, sesgo_1h=sesgo_1h, vp_info=vp_info, rr_mult=2.5)
        return "CRIPTO", adx_min, df, vp_info, senal

    if symbol in ORO_MARKETS:
        adx_min = 28.0
        senal = evaluar_estrategia_intradia_1h(df, adx_min=adx_min, sesgo_1h=sesgo_1h, vp_info=vp_info, rr_mult=2.5)
        return "ORO", adx_min, df, vp_info, senal

    adx_min = 25.0
    senal = evaluar_estrategia_intradia_1h(df, adx_min=adx_min, sesgo_1h=sesgo_1h, vp_info=vp_info, rr_mult=2.0)
    return "FOREX", adx_min, df, vp_info, senal


# ==========================================
# 🤖 MOTOR EN FONDO
# ==========================================
def ejecutar_logica_autopiloto(es_hilo_fondo=False):
    global AUTOPILOTO_STATUS, ALERTAS_ENVIADAS_GLOBAL
    if not es_hilo_fondo:
        AUTOPILOTO_STATUS = "Ejecutando barrido Intradía 1H..."

    sesion_txt, forex_abierto = obtener_sesion_actual()
    alertas_nuevas = 0

    for symbol in ALL_TRACKED_SYMBOLS:
        if symbol not in CRYPTO_MARKETS and not forex_abierto:
            continue

        sesgo_1h = obtener_sesgo_1h(symbol)
        for tf in ["5m", "15m", "1h"]:
            try:
                mercado, _, df, _, res = analizar_por_mercado(symbol, tf, sesgo_1h=sesgo_1h)
                if res and not df.empty:
                    u = df.iloc[-1]
                    precio = float(u["close"])
                    gestion_10 = calcular_gestion_bank_10(precio, res["sl"], mercado)
                    msg = (
                        f"🏦 *SEÑAL INTRADÍA 1H (BANK $10)*\n"
                        f"🕒 *Bolsa:* {sesion_txt if mercado != 'CRIPTO' else 'Cripto 24/7'}\n"
                        f"🌐 *Mercado:* `{mercado}` | *Activo:* `{symbol}` | *TF:* `{tf}` (1H: `{sesgo_1h}`)\n"
                        f"💲 *Precio:* `${precio:,.4f}` | *ADX:* `{float(u['adx']):.1f}` | *RSI:* `{float(u['rsi']):.1f}`\n"
                        f"🎯 *{res['modelo']}*\n"
                        f"🧭 *Acción:* *{res['direccion']}*\n"
                        f"🛡️ *SL:* `${res['sl']:,.4f}` | *TP:* `${res['tp']:,.4f}`\n"
                        f"💰 *Gestión $10:* {gestion_10}\n"
                        f"📋 *Plan:*\n{res['plan']}"
                    )
                    clave = f"{symbol}_{tf}_{res['id_senal']}"
                    ahora = time.time()
                    if ahora - ALERTAS_ENVIADAS_GLOBAL.get(clave, 0) > 600:
                        if enviar_telegram(msg):
                            ALERTAS_ENVIADAS_GLOBAL[clave] = ahora
                            alertas_nuevas += 1
                time.sleep(0.12)
            except Exception:
                continue

    AUTOPILOTO_STATUS = (
        f"✅ Barrido listo. Alertas enviadas: {alertas_nuevas}"
        if not es_hilo_fondo else f"⚡ {sesion_txt}"
    )


@st.cache_resource
def iniciar_hilo_autopiloto():
    def loop():
        time.sleep(3)
        while True:
            try:
                ejecutar_logica_autopiloto(es_hilo_fondo=True)
            except Exception:
                pass
            time.sleep(30)
    t = threading.Thread(target=loop, daemon=True)
    t.start()
    return t


iniciar_hilo_autopiloto()


# ==========================================
# 📊 INTERFAZ STREAMLIT
# ==========================================
sesion_ui, _ = obtener_sesion_actual()
st.markdown("<h1 style='text-align: center; color: #00FFAA;'>🏦 INSTITUTIONAL QUANT TERMINAL — INTRADÍA 1H</h1>", unsafe_allow_html=True)
st.markdown(f"<p style='text-align: center; color: gray;'>{sesion_ui} • 9 Top Criptos + Forex + Oro • Estrategia 1H (5m/15m/1h)</p>", unsafe_allow_html=True)
st.markdown("---")

st.sidebar.header("🌐 Configuración")
symbol = st.sidebar.selectbox("Activo:", ALL_TRACKED_SYMBOLS)
timeframe = st.sidebar.selectbox("Temporalidad:", ["5m", "15m", "1h"])
st.sidebar.info(AUTOPILOTO_STATUS)

if st.sidebar.button("🔔 Probar Telegram Ahora"):
    ok = enviar_telegram("✅ *Prueba de conexión exitosa*\nTu bot Intradía 1H (9 Criptos + Forex + Oro) está activo.")
    if ok:
        st.sidebar.success("¡Mensaje de prueba enviado a Telegram!")
    else:
        st.sidebar.error("No se pudo enviar. Revisa conexión o token.")

if st.sidebar.button("⚡ Forzar Escaneo Ahora"):
    with st.spinner("Escaneando 9 Criptos, Forex y Oro..."):
        ejecutar_logica_autopiloto(es_hilo_fondo=False)
        st.sidebar.success("¡Escaneo completado!")


@st.fragment(run_every="10s")
def render_panel(symbol, timeframe):
    sesgo_1h = obtener_sesgo_1h(symbol)
    mercado, adx_min, df, vp_info, senal = analizar_por_mercado(symbol, timeframe, sesgo_1h=sesgo_1h)

    if df.empty:
        st.error(f"Sin datos reales disponibles para {symbol}.")
        return

    col_g, col_p = st.columns([2.5, 1])
    with col_g:
        fig = make_subplots(rows=2, cols=1, shared_xaxes=True, vertical_spacing=0.03, row_width=[0.2, 0.8])
        fig.add_trace(go.Candlestick(
            x=df["timestamp"], open=df["open"], high=df["high"], low=df["low"], close=df["close"],
            increasing_line_color="#00FFAA", decreasing_line_color="#FF3333", name="Precio"
        ), row=1, col=1)
        fig.add_trace(go.Scatter(x=df["timestamp"], y=df["vwap"], line=dict(color="#FF00FF", width=2), name="VWAP"), row=1, col=1)
        if vp_info:
            fig.add_hline(y=vp_info["poc"], line_dash="dash", line_color="#FFA500", annotation_text="POC", row=1, col=1)
            fig.add_hline(y=vp_info["vah"], line_dash="dot", line_color="#8888FF", annotation_text="VAH", row=1, col=1)
            fig.add_hline(y=vp_info["val"], line_dash="dot", line_color="#8888FF", annotation_text="VAL", row=1, col=1)
        colors = ["#00FFAA" if r["close"] >= r["open"] else "#FF3333" for _, r in df.iterrows()]
        fig.add_trace(go.Bar(x=df["timestamp"], y=df["volume"], marker_color=colors, name="Volumen"), row=2, col=1)
        fig.update_layout(template="plotly_dark", xaxis_rangeslider_visible=False, height=580, margin=dict(l=10, r=10, t=10, b=10))
        st.plotly_chart(fig, use_container_width=True)

    with col_p:
        st.markdown(f"### ⚙️ Mercado: {mercado}")
        u = df.iloc[-1]
        precio = float(u["close"])
        st.metric(f"{symbol} [{timeframe}]", f"${precio:,.4f}", f"{precio - float(df.iloc[-2]['close']):,.4f}")
        st.caption(f"🧭 **Sesgo 1H:** `{sesgo_1h}` | **ADX:** `{float(u['adx']):.1f}` | **RSI:** `{float(u['rsi']):.1f}`")
        if vp_info:
            st.caption(f"📊 **Estado AMT:** `{vp_info['estado']}` | **POC:** `${vp_info['poc']:,.4f}`")
        if senal:
            st.info(f"**{senal['modelo']}**\n\n**{senal['direccion']}**\n\n{calcular_gestion_bank_10(precio, senal['sl'], mercado)}\n\n{senal['plan']}")
        else:
            st.caption("Sin setup intradía válido en esta vela.")

    st.markdown("---")
    st.markdown("### 📰 Noticias de Mercados en Vivo")
    noticias = obtener_noticias_en_vivo()
    if noticias:
        cols = st.columns(2)
        for i, n in enumerate(noticias[:8]):
            with cols[i % 2]:
                st.markdown(f"- **[{n['fuente']}]** [{n['titulo']}]({n['link']}) *({n['fecha']})*")


render_panel(symbol, timeframe)
