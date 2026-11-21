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

st.set_page_config(page_title="Institutional Quant Terminal — Bank $10", layout="wide", page_icon="🏦")

CRYPTO_MARKETS = ["BTC/USDT", "ETH/USDT", "SOL/USDT", "BNB/USDT"]
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
AUTOPILOTO_STATUS = "Motor Bank $10 activo: Escaneando cada 30s..."


# ==========================================
# 🕒 SESIONES DE BOLSAS DE VALORES (UTC)
# ==========================================
def obtener_sesion_actual():
    ahora_utc = datetime.now(timezone.utc)
    dia = ahora_utc.weekday()  # 0=Lunes ... 5=Sábado, 6=Domingo
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
# 🧠 MOTOR MATEMÁTICO (AMT + SMART MONEY)
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


def detectar_exhaustacion(df):
    if len(df) < 22:
        return False
    rango_actual = df["high"].iloc[-1] - df["low"].iloc[-1]
    rangos_previos = df["high"].iloc[-21:-1] - df["low"].iloc[-21:-1]
    return (rango_actual > rangos_previos.max() * 1.25) and (abs(df["close"].iloc[-2] - df["close"].iloc[-10]) > rangos_previos.mean() * 3.0)


def calcular_gestion_bank_10(precio, sl, mercado):
    riesgo_usd = BANK_USD * RIESGO_POR_OPERACION
    dist_sl = max(abs(precio - sl), 1e-9)
    if mercado == "CRIPTO":
        tamano = riesgo_usd / dist_sl
        return f"Riesgo máx: `${riesgo_usd:.2f}` (2% de $10) | Tamaño aprox: `{tamano:.4f}` unidades"
    return f"Riesgo máx: `${riesgo_usd:.2f}` (2% de $10) | Usa lotaje mínimo `0.01`"


def evaluar_estrategia_dual(df, adx_min=28.0, sesgo_htf="NEUTRO", vp_info=None, rr_mult=2.5):
    if df.empty or len(df) < 25 or not vp_info or detectar_exhaustacion(df):
        return None

    ultima, anterior = df.iloc[-1], df.iloc[-2]
    precio = float(ultima["close"])
    adx_val = float(ultima.get("adx", 0.0))
    vwap_val = float(ultima.get("vwap", precio))
    ema_fast, ema_slow = float(ultima.get("ema_fast", precio)), float(ultima.get("ema_slow", precio))

    cvd_alcista = float(ultima.get("cvd", 0.0)) >= float(anterior.get("cvd", 0.0))
    cvd_bajista = float(ultima.get("cvd", 0.0)) <= float(anterior.get("cvd", 0.0))

    atr_ref = max(float((df["high"].iloc[-15:-1] - df["low"].iloc[-15:-1]).mean()), precio * 0.0015)
    ob_alcista = (anterior["close"] < anterior["open"]) and (ultima["close"] > ultima["open"]) and (precio > anterior["high"])
    ob_bajista = (anterior["close"] > anterior["open"]) and (ultima["close"] < ultima["open"]) and (precio < anterior["low"])

    poc, vah, val = vp_info["poc"], vp_info["vah"], vp_info["val"]

    if vp_info["estado"] == "OUT_OF_BALANCE" and adx_val >= adx_min:
        if precio > vah and precio >= vwap_val and ema_fast >= ema_slow and sesgo_htf != "BAJISTA" and cvd_alcista:
            sl = min(float(df["low"].iloc[-5:].min()), precio - atr_ref * 1.1)
            riesgo = max(precio - sl, atr_ref * 0.8)
            sl = precio - riesgo
            tp = precio + riesgo * rr_mult
            return {
                "id_senal": "MODELO_A_LONG",
                "modelo": "MODELO A: Tendencia Out-of-Balance",
                "direccion": "COMPRA (LONG) 🟢",
                "sl": sl,
                "tp": tp,
                "plan": (
                    f"1) Ruptura sobre VAH (${vah:,.4f}) con ADX {adx_val:.1f} y CVD comprador.\n"
                    f"2) Mover SL a Break-Even en 1:1 (${precio + riesgo:,.4f})."
                )
            }

        if precio < val and precio <= vwap_val and ema_fast <= ema_slow and sesgo_htf != "ALCISTA" and cvd_bajista:
            sl = max(float(df["high"].iloc[-5:].max()), precio + atr_ref * 1.1)
            riesgo = max(sl - precio, atr_ref * 0.8)
            sl = precio + riesgo
            tp = precio - riesgo * rr_mult
            return {
                "id_senal": "MODELO_A_SHORT",
                "modelo": "MODELO A: Tendencia Out-of-Balance",
                "direccion": "VENTA (SHORT) 🔴",
                "sl": sl,
                "tp": tp,
                "plan": (
                    f"1) Ruptura bajo VAL (${val:,.4f}) con ADX {adx_val:.1f} y CVD vendedor.\n"
                    f"2) Mover SL a Break-Even en 1:1 (${precio - riesgo:,.4f})."
                )
            }

    if vp_info["estado"] == "BALANCE":
        if float(ultima["high"]) > vah and precio < vah and (cvd_bajista or ob_bajista):
            sl = float(ultima["high"]) + atr_ref * 0.4
            riesgo = max(sl - precio, atr_ref * 0.6)
            if (precio - poc) / riesgo >= 1.5:
                return {
                    "id_senal": "MODELO_B_FAKEOUT_SHORT",
                    "modelo": "MODELO B: Reversión al POC (Fakeout VAH)",
                    "direccion": "VENTA (SHORT) 🔴",
                    "sl": sl,
                    "tp": poc,
                    "plan": f"1) Fakeout sobre VAH (${vah:,.4f}).\n2) Objetivo: POC (${poc:,.4f})."
                }

        if float(ultima["low"]) < val and precio > val and (cvd_alcista or ob_alcista):
            sl = float(ultima["low"]) - atr_ref * 0.4
            riesgo = max(precio - sl, atr_ref * 0.6)
            if (poc - precio) / riesgo >= 1.5:
                return {
                    "id_senal": "MODELO_B_FAKEOUT_LONG",
                    "modelo": "MODELO B: Reversión al POC (Fakeout VAL)",
                    "direccion": "COMPRA (LONG) 🟢",
                    "sl": sl,
                    "tp": poc,
                    "plan": f"1) Fakeout bajo VAL (${val:,.4f}).\n2) Objetivo: POC (${poc:,.4f})."
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
        interval_map = {"5m": "5m", "15m": "15m", "1h": "60m", "4h": "60m"}
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


def obtener_sesgo_htf(symbol):
    df_htf = obtener_datos_mercado(symbol, "4h", limit=60, ttl_segundos=180)
    if df_htf.empty or len(df_htf) < 20:
        return "NEUTRO"
    u = df_htf.iloc[-1]
    if float(u["close"]) > float(u["ema_slow"]) and float(u["ema_fast"]) > float(u["ema_slow"]):
        return "ALCISTA"
    if float(u["close"]) < float(u["ema_slow"]) and float(u["ema_fast"]) < float(u["ema_slow"]):
        return "BAJISTA"
    return "NEUTRO"


def analizar_por_mercado(symbol, timeframe, sesgo_htf="NEUTRO"):
    df = obtener_datos_mercado(symbol, timeframe, limit=85, ttl_segundos=60)
    if df.empty or not df.attrs.get("es_real", False):
        return "DESCONOCIDO", 28.0, df, None, None

    vp_info = calcular_volume_profile_amt(df)

    if symbol in CRYPTO_MARKETS:
        adx_min = 30.0
        senal = evaluar_estrategia_dual(df, adx_min=adx_min, sesgo_htf=sesgo_htf, vp_info=vp_info, rr_mult=2.5)
        return "CRIPTO", adx_min, df, vp_info, senal

    if symbol in ORO_MARKETS:
        adx_min = 32.0
        senal = evaluar_estrategia_dual(df, adx_min=adx_min, sesgo_htf=sesgo_htf, vp_info=vp_info, rr_mult=3.0)
        return "ORO", adx_min, df, vp_info, senal

    adx_min = 28.0
    senal = evaluar_estrategia_dual(df, adx_min=adx_min, sesgo_htf=sesgo_htf, vp_info=vp_info, rr_mult=2.0)
    return "FOREX", adx_min, df, vp_info, senal


# ==========================================
# 🤖 MOTOR EN FONDO
# ==========================================
def ejecutar_logica_autopiloto(es_hilo_fondo=False):
    global AUTOPILOTO_STATUS, ALERTAS_ENVIADAS_GLOBAL
    if not es_hilo_fondo:
        AUTOPILOTO_STATUS = "Ejecutando barrido (Bank $10)..."

    sesion_txt, forex_abierto = obtener_sesion_actual()
    alertas_nuevas = 0

    for symbol in ALL_TRACKED_SYMBOLS:
        if symbol not in CRYPTO_MARKETS and not forex_abierto:
            continue

        sesgo_htf = obtener_sesgo_htf(symbol)
        for tf in ["5m", "15m", "1h"]:
            try:
                mercado, _, df, _, res = analizar_por_mercado(symbol, tf, sesgo_htf=sesgo_htf)
                if res and not df.empty:
                    u = df.iloc[-1]
                    precio = float(u["close"])
                    gestion_10 = calcular_gestion_bank_10(precio, res["sl"], mercado)
                    msg = (
                        f"🏦 *SEÑAL INSTITUCIONAL (BANK $10)*\n"
                        f"🕒 *Bolsa:* {sesion_txt if mercado != 'CRIPTO' else 'Cripto 24/7'}\n"
                        f"🌐 *Mercado:* `{mercado}` | *Activo:* `{symbol}` | *TF:* `{tf}` (HTF: `{sesgo_htf}`)\n"
                        f"💲 *Precio:* `${precio:,.4f}` | *ADX:* `{float(u['adx']):.1f}`\n"
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
st.markdown("<h1 style='text-align: center; color: #00FFAA;'>🏦 INSTITUTIONAL QUANT TERMINAL — BANK $10 USD</h1>", unsafe_allow_html=True)
st.markdown(f"<p style='text-align: center; color: gray;'>{sesion_ui} • BTC, ETH, SOL, BNB, Forex & Oro • Noticias en Vivo</p>", unsafe_allow_html=True)
st.markdown("---")

st.sidebar.header("🌐 Configuración")
symbol = st.sidebar.selectbox("Activo:", ALL_TRACKED_SYMBOLS)
timeframe = st.sidebar.selectbox("Temporalidad:", ["5m", "15m", "1h"])
st.sidebar.info(AUTOPILOTO_STATUS)

if st.sidebar.button("🔔 Probar Telegram Ahora"):
    ok = enviar_telegram("✅ *Prueba de conexión exitosa*\nTu bot (BTC, ETH, SOL, Forex y Oro) está activo.")
    if ok:
        st.sidebar.success("¡Mensaje de prueba enviado a Telegram!")
    else:
        st.sidebar.error("No se pudo enviar. Revisa conexión o token.")

if st.sidebar.button("⚡ Forzar Escaneo Ahora"):
    with st.spinner("Escaneando Cripto (BTC, ETH, SOL, BNB), Forex y Oro..."):
        ejecutar_logica_autopiloto(es_hilo_fondo=False)
        st.sidebar.success("¡Escaneo completado!")


@st.fragment(run_every="10s")
def render_panel(symbol, timeframe):
    sesgo_htf = obtener_sesgo_htf(symbol)
    mercado, adx_min, df, vp_info, senal = analizar_por_mercado(symbol, timeframe, sesgo_htf=sesgo_htf)

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
        st.caption(f"🧭 **HTF (4H):** `{sesgo_htf}` | **ADX min:** `>{adx_min:.0f}`")
        if vp_info:
            st.caption(f"📊 **Estado AMT:** `{vp_info['estado']}`")
            st.caption(f"🎯 **POC:** `${vp_info['poc']:,.4f}` | **VA:** `${vp_info['val']:,.4f}` - `${vp_info['vah']:,.4f}`")
        if senal:
            st.info(f"**{senal['modelo']}**\n\n**{senal['direccion']}**\n\n{calcular_gestion_bank_10(precio, senal['sl'], mercado)}\n\n{senal['plan']}")
        else:
            st.caption("Sin setup institucional válido en esta vela.")

    st.markdown("---")
    st.markdown("### 📰 Noticias de Mercados en Vivo")
    noticias = obtener_noticias_en_vivo()
    if noticias:
        cols = st.columns(2)
        for i, n in enumerate(noticias[:8]):
            with cols[i % 2]:
                st.markdown(f"- **[{n['fuente']}]** [{n['titulo']}]({n['link']}) *({n['fecha']})*")


render_panel(symbol, timeframe)
