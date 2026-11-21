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
from datetime import datetime

# ==========================================
# ⚙️ CONFIGURACIÓN GLOBAL & CREDENCIALES
# ==========================================
TELEGRAM_TOKEN = "9148434925:AAH_AuswS-GGPHadFBwcrDOUUIwBFbXvtDs"
TELEGRAM_CHAT_ID = "1496321437"

st.set_page_config(page_title="Institutional Quant Terminal PRO", layout="wide", page_icon="🏦")

CRYPTO_MARKETS = ["BTC/USDT", "ETH/USDT", "BNB/USDT"]
FOREX_MARKETS = [
    "EURUSD=X", "GBPUSD=X", "USDJPY=X", "USDCHF=X", "AUDUSD=X",
    "USDCAD=X", "NZDUSD=X", "EURJPY=X", "GBPJPY=X", "EURGBP=X",
    "XAUUSD=X", "XAUUSD"
]
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
CACHE_NOTICIAS_GLOBAL = {"ts": 0, "items": []}
CACHE_LOCK = threading.Lock()
AUTOPILOTO_STATUS = "Motor PRO activo: Escaneando cada 30s..."


# ==========================================
# 📰 NOTICIAS DE MERCADO EN VIVO (RSS)
# ==========================================
def obtener_noticias_en_vivo(ttl_segundos=180):
    ahora = time.time()
    with CACHE_LOCK:
        if CACHE_NOTICIAS_GLOBAL["items"] and (ahora - CACHE_NOTICIAS_GLOBAL["ts"] < ttl_segundos):
            return CACHE_NOTICIAS_GLOBAL["items"]

    feeds = [
        ("Investing Forex", "https://www.investing.com/rss/news_1.rss"),
        ("Investing Oro/Commodities", "https://www.investing.com/rss/news_11.rss"),
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
        requests.post(url, data=data, headers=headers, timeout=5)
    except Exception:
        pass


# ==========================================
# 🧠 MOTOR CUANTITATIVO & ESTRUCTURA
# ==========================================
def _normalizar_timestamp(serie):
    ts = pd.to_datetime(serie)
    if getattr(ts.dt, "tz", None) is not None:
        ts = ts.dt.tz_convert(None)
    return ts


def obtener_umbral_adx(symbol):
    if symbol in ("XAUUSD", "XAUUSD=X"):
        return 35.0
    if symbol in CRYPTO_MARKETS:
        return 32.0
    return 30.0


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


def calcular_volume_profile_amt(df, bins=24):
    if df.empty or len(df) < 15:
        return None
    p_min, p_max = float(df["low"].min()), float(df["high"].max())
    if p_max <= p_min:
        return None

    edges = np.linspace(p_min, p_max, bins + 1)
    vol_bin = np.zeros(bins)
    tp = ((df["high"] + df["low"] + df["close"]) / 3.0).values
    indices = np.clip(np.digitize(tp, edges) - 1, 0, bins - 1)
    for idx, v in zip(indices, df["volume"].values):
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
    media_vol = vol_bin.mean()
    lvn_levels = [float((edges[i] + edges[i + 1]) / 2.0) for i in range(bins) if vol_bin[i] < media_vol * 0.35]

    precio = float(df["close"].iloc[-1])
    estado = "OUT_OF_BALANCE" if (precio > vah_price or precio < val_price) else "BALANCE"
    return {"poc": poc_price, "vah": vah_price, "val": val_price, "lvn": lvn_levels, "estado": estado}


def detectar_exhaustacion(df):
    if len(df) < 22:
        return False
    rango_actual = df["high"].iloc[-1] - df["low"].iloc[-1]
    rangos_previos = df["high"].iloc[-21:-1] - df["low"].iloc[-21:-1]
    return (rango_actual > rangos_previos.max() * 1.20) and (abs(df["close"].iloc[-2] - df["close"].iloc[-10]) > rangos_previos.mean() * 3.0)


def detectar_fvg_3_velas(df):
    if len(df) < 4:
        return None
    for idx in (-1, -2):
        pos = len(df) + idx
        if pos < 2:
            continue
        v1_h, v1_l = float(df["high"].iloc[pos - 2]), float(df["low"].iloc[pos - 2])
        v3_l, v3_h = float(df["low"].iloc[pos]), float(df["high"].iloc[pos])
        if v3_l > v1_h:
            return {"tipo": "FVG_ALCISTA", "zona_low": v1_h, "zona_high": v3_l}
        if v3_h < v1_l:
            return {"tipo": "FVG_BAJISTA", "zona_low": v3_h, "zona_high": v1_l}
    return None


# ==========================================
# 🎯 MI ESTRATEGIA INSTITUCIONAL (AMT + SMART MONEY)
# ==========================================
def evaluar_estrategia_pro(df, symbol="", sesgo_htf="NEUTRO", vp_info=None):
    """
    Estrategia Dual Institucional:
    - MODELO A (Tendencia Out-of-Balance): Precio fuera del Value Area + alineado con 4H + ADX fuerte + CVD + OB/FVG/Fib>=50%.
    - MODELO B (Reversión al POC / Fakeout): Mercado en Balance + barrida de liquidez (Sweep) sobre VAH o bajo VAL con rechazo hacia el POC.
    """
    if df.empty or len(df) < 25 or not vp_info:
        return None

    if detectar_exhaustacion(df):
        return None

    ultima = df.iloc[-1]
    anterior = df.iloc[-2]
    precio = float(ultima["close"])
    adx_val = float(ultima.get("adx", 0.0))
    vwap_val = float(ultima.get("vwap", precio))
    ema_fast = float(ultima.get("ema_fast", precio))
    ema_slow = float(ultima.get("ema_slow", precio))

    cvd_alcista = float(ultima.get("cvd", 0.0)) > float(anterior.get("cvd", 0.0))
    cvd_bajista = float(ultima.get("cvd", 0.0)) < float(anterior.get("cvd", 0.0))

    atr_ref = float((df["high"].iloc[-15:-1] - df["low"].iloc[-15:-1]).mean())
    atr_ref = max(atr_ref, precio * 0.0015)

    fvg = detectar_fvg_3_velas(df)
    ob_alcista = (anterior["close"] < anterior["open"]) and (ultima["close"] > ultima["open"]) and (precio > anterior["high"])
    ob_bajista = (anterior["close"] > anterior["open"]) and (ultima["close"] < ultima["open"]) and (precio < anterior["low"])

    rango_max, rango_min = float(df["high"].iloc[-20:].max()), float(df["low"].iloc[-20:].min())
    amplitud = max(rango_max - rango_min, 1e-9)
    descuento_long = ((rango_max - precio) / amplitud) * 100.0
    descuento_short = ((precio - rango_min) / amplitud) * 100.0

    poc, vah, val = vp_info["poc"], vp_info["vah"], vp_info["val"]
    adx_min = obtener_umbral_adx(symbol)

    # ---------------------------------------------------------
    # MODELO A: TENDENCIA FUERA DE BALANCE (Fabio + El Sensei)
    # ---------------------------------------------------------
    if vp_info["estado"] == "OUT_OF_BALANCE" and adx_val >= adx_min:
        # LONG OUT-OF-BALANCE
        if precio > vah and precio > vwap_val and ema_fast > ema_slow and sesgo_htf == "ALCISTA" and cvd_alcista:
            if ob_alcista or (fvg and fvg["tipo"] == "FVG_ALCISTA") or descuento_long >= 50.0:
                sl = min(float(df["low"].iloc[-5:].min()), precio - atr_ref * 1.1)
                riesgo = max(precio - sl, atr_ref * 0.8)
                sl = precio - riesgo
                tp = precio + riesgo * 2.5
                return {
                    "id_senal": "MODELO_A_LONG",
                    "modelo": "MODELO A: Tendencia Out-of-Balance",
                    "direccion": "COMPRA (LONG) 🟢",
                    "sl": sl,
                    "tp": tp,
                    "plan": (
                        f"1) Precio rompió VAH (${vah:,.4f}) con HTF 4H Alcista, ADX {adx_val:.1f} y CVD comprador.\n"
                        f"2) Entrada en retroceso sobre VWAP/OB/FVG (Descuento {descuento_long:.0f}%).\n"
                        f"3) Mueve SL a Break-Even al alcanzar 1:1 (${precio + riesgo:,.4f})."
                    )
                }

        # SHORT OUT-OF-BALANCE
        if precio < val and precio < vwap_val and ema_fast < ema_slow and sesgo_htf == "BAJISTA" and cvd_bajista:
            if ob_bajista or (fvg and fvg["tipo"] == "FVG_BAJISTA") or descuento_short >= 50.0:
                sl = max(float(df["high"].iloc[-5:].max()), precio + atr_ref * 1.1)
                riesgo = max(sl - precio, atr_ref * 0.8)
                sl = precio + riesgo
                tp = precio - riesgo * 2.5
                return {
                    "id_senal": "MODELO_A_SHORT",
                    "modelo": "MODELO A: Tendencia Out-of-Balance",
                    "direccion": "VENTA (SHORT) 🔴",
                    "sl": sl,
                    "tp": tp,
                    "plan": (
                        f"1) Precio rompió VAL (${val:,.4f}) con HTF 4H Bajista, ADX {adx_val:.1f} y CVD vendedor.\n"
                        f"2) Entrada en retroceso sobre VWAP/OB/FVG (Descuento {descuento_short:.0f}%).\n"
                        f"3) Mueve SL a Break-Even al alcanzar 1:1 (${precio - riesgo:,.4f})."
                    )
                }

    # ---------------------------------------------------------
    # MODELO B: REVERSIÓN AL POC / FAKEOUT EN BALANCE
    # ---------------------------------------------------------
    if vp_info["estado"] == "BALANCE":
        # Fakeout alcista fallido (Sweep sobre VAH -> Short hacia POC)
        if float(ultima["high"]) > vah and precio < vah and cvd_bajista:
            sl = float(ultima["high"]) + atr_ref * 0.4
            riesgo = max(sl - precio, atr_ref * 0.6)
            tp = poc
            if (precio - tp) / riesgo >= 1.8:
                return {
                    "id_senal": "MODELO_B_FAKEOUT_SHORT",
                    "modelo": "MODELO B: Reversión al POC (Fakeout VAH)",
                    "direccion": "VENTA (SHORT) 🔴",
                    "sl": sl,
                    "tp": tp,
                    "plan": (
                        f"1) Trampa de compradores (Fakeout) sobre VAH (${vah:,.4f}) con rechazo en CVD.\n"
                        f"2) Objetivo institucional: regreso al POC (${poc:,.4f}).\n"
                        f"3) Ratio R:B >= 1:1.8 validado. No operar si cierra fuerte sobre VAH."
                    )
                }

        # Fakeout bajista fallido (Sweep bajo VAL -> Long hacia POC)
        if float(ultima["low"]) < val and precio > val and cvd_alcista:
            sl = float(ultima["low"]) - atr_ref * 0.4
            riesgo = max(precio - sl, atr_ref * 0.6)
            tp = poc
            if (tp - precio) / riesgo >= 1.8:
                return {
                    "id_senal": "MODELO_B_FAKEOUT_LONG",
                    "modelo": "MODELO B: Reversión al POC (Fakeout VAL)",
                    "direccion": "COMPRA (LONG) 🟢",
                    "sl": sl,
                    "tp": tp,
                    "plan": (
                        f"1) Trampa de vendedores (Fakeout) bajo VAL (${val:,.4f}) con absorción en CVD.\n"
                        f"2) Objetivo institucional: regreso al POC (${poc:,.4f}).\n"
                        f"3) Ratio R:B >= 1:1.8 validado. No operar si cierra fuerte bajo VAL."
                    )
                }

    return None


# ==========================================
# 🛠️ OBTENCIÓN DE DATOS CON CACHÉ
# ==========================================
def obtener_datos(symbol, timeframe, limit=100, ttl_segundos=60):
    cache_key = f"{symbol}_{timeframe}_{limit}"
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
    elif symbol in FOREX_MARKETS:
        interval_map = {"5m": "5m", "15m": "15m", "1h": "60m", "4h": "60m"}
        yf_interval = interval_map.get(timeframe, "60m")
        periodos = ["5d", "1mo"] if timeframe == "5m" else ["1mo", "3mo"]
        simbolos_yf = ["XAUUSD=X", "GC=F"] if symbol in ("XAUUSD", "XAUUSD=X") else [symbol]

        for sym_try in simbolos_yf:
            for periodo in periodos:
                try:
                    hist = yf.Ticker(sym_try).history(period=periodo, interval=yf_interval)
                    if not hist.empty and "Close" in hist.columns:
                        temp_df = hist.reset_index()
                        time_col = "Datetime" if "Datetime" in temp_df.columns else ("Date" if "Date" in temp_df.columns else temp_df.columns[0])
                        temp_df.rename(columns={
                            time_col: "timestamp", "Open": "open", "High": "high",
                            "Low": "low", "Close": "close", "Volume": "volume"
                        }, inplace=True)
                        temp_df["timestamp"] = _normalizar_timestamp(temp_df["timestamp"])
                        temp_df["volume"] = temp_df["volume"].fillna(0)
                        temp_df = temp_df.dropna(subset=["open", "high", "low", "close"]).tail(limit).copy()
                        if not temp_df.empty and float(temp_df["close"].iloc[-1]) > 0:
                            df = temp_df
                            es_real = True
                            break
                except Exception:
                    continue
            if es_real:
                break

    if df.empty:
        return df

    if len(df) > 5:
        df["vwap"] = calcular_vwap(df)
        df["vol_ma"] = df["volume"].rolling(window=min(20, len(df))).mean()
        df["ema_fast"] = df["close"].ewm(span=9, adjust=False).mean()
        df["ema_slow"] = df["close"].ewm(span=21, adjust=False).mean()
        df["adx"] = calcular_adx(df)
        df["cvd"] = calcular_cvd_proxy(df)

    df.attrs["es_real"] = es_real
    if es_real:
        with CACHE_LOCK:
            CACHE_DATOS_GLOBAL[cache_key] = {"ts": ahora, "df": df.copy(), "es_real": True}
    return df


def obtener_sesgo_htf(symbol):
    df_htf = obtener_datos(symbol, "4h", limit=60, ttl_segundos=180)
    if df_htf.empty or len(df_htf) < 20:
        return "NEUTRO"
    u = df_htf.iloc[-1]
    if float(u["close"]) > float(u["ema_slow"]) and float(u["ema_fast"]) > float(u["ema_slow"]):
        return "ALCISTA"
    if float(u["close"]) < float(u["ema_slow"]) and float(u["ema_fast"]) < float(u["ema_slow"]):
        return "BAJISTA"
    return "NEUTRO"


# ==========================================
# 🤖 MOTOR EN FONDO
# ==========================================
def ejecutar_logica_autopiloto(es_hilo_fondo=False):
    global AUTOPILOTO_STATUS, ALERTAS_ENVIADAS_GLOBAL
    if not es_hilo_fondo:
        AUTOPILOTO_STATUS = "Ejecutando barrido PRO..."

    alertas_nuevas = 0
    for symbol in ALL_TRACKED_SYMBOLS:
        tipo = "CRIPTO" if symbol in CRYPTO_MARKETS else "FOREX/METAL"
        sesgo_htf = obtener_sesgo_htf(symbol)

        for tf in ["5m", "15m", "1h"]:
            try:
                df = obtener_datos(symbol, tf, limit=85, ttl_segundos=60)
                if df.empty or not df.attrs.get("es_real", False):
                    continue

                vp_info = calcular_volume_profile_amt(df)
                res = evaluar_estrategia_pro(df, symbol=symbol, sesgo_htf=sesgo_htf, vp_info=vp_info)
                if res:
                    u = df.iloc[-1]
                    msg = (
                        f"🏦 *SEÑAL INSTITUCIONAL PRO*\n"
                        f"🌐 *Mercado:* `{tipo}` | *Activo:* `{symbol}` | *TF:* `{tf}` (HTF: `{sesgo_htf}`)\n"
                        f"💲 *Precio:* `${float(u['close']):,.4f}` | *ADX:* `{float(u['adx']):.1f}`\n"
                        f"🎯 *{res['modelo']}*\n"
                        f"🧭 *Acción:* *{res['direccion']}*\n"
                        f"🛡️ *SL:* `${res['sl']:,.4f}` | *TP:* `${res['tp']:,.4f}`\n"
                        f"📋 *Plan:*\n{res['plan']}"
                    )
                    clave = f"{symbol}_{tf}_{res['id_senal']}"
                    ahora = time.time()
                    if ahora - ALERTAS_ENVIADAS_GLOBAL.get(clave, 0) > 600:
                        enviar_telegram(msg)
                        ALERTAS_ENVIADAS_GLOBAL[clave] = ahora
                        alertas_nuevas += 1
                time.sleep(0.12)
            except Exception:
                continue

    AUTOPILOTO_STATUS = (
        f"✅ Barrido PRO listo. Alertas enviadas: {alertas_nuevas}"
        if not es_hilo_fondo else "⚡ Motor PRO activo (ciclo cada 30s)"
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
st.markdown("<h1 style='text-align: center; color: #00FFAA;'>🏦 INSTITUTIONAL QUANT TERMINAL PRO</h1>", unsafe_allow_html=True)
st.markdown("<p style='text-align: center; color: gray;'>Modelo A (Out-of-Balance + 4H + OB/FVG) • Modelo B (Fakeout + Reversión al POC) • Noticias en Vivo</p>", unsafe_allow_html=True)
st.markdown("---")

st.sidebar.header("🌐 Configuración")
symbol = st.sidebar.selectbox("Activo:", ALL_TRACKED_SYMBOLS)
timeframe = st.sidebar.selectbox("Temporalidad:", ["5m", "15m", "1h"])
st.sidebar.info(AUTOPILOTO_STATUS)

if st.sidebar.button("⚡ Forzar Escaneo Ahora"):
    with st.spinner("Escaneando mercados..."):
        ejecutar_logica_autopiloto(es_hilo_fondo=False)
        st.sidebar.success("¡Escaneo completado!")


@st.fragment(run_every="10s")
def render_panel(symbol, timeframe):
    df = obtener_datos(symbol, timeframe, limit=120, ttl_segundos=30)
    if df.empty:
        st.error(f"Sin datos reales disponibles para {symbol}.")
        return

    vp_info = calcular_volume_profile_amt(df)
    sesgo_htf = obtener_sesgo_htf(symbol)

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
        st.markdown("### ⚙️ Lectura Institucional")
        u = df.iloc[-1]
        st.metric(f"{symbol} [{timeframe}]", f"${float(u['close']):,.4f}", f"{float(u['close']) - float(df.iloc[-2]['close']):,.4f}")
        st.caption(f"🧭 **HTF (4H):** `{sesgo_htf}`")
        if vp_info:
            st.caption(f"📊 **Estado AMT:** `{vp_info['estado']}`")
            st.caption(f"🎯 **POC:** `${vp_info['poc']:,.4f}` | **VA:** `${vp_info['val']:,.4f}` - `${vp_info['vah']:,.4f}`")
        senal = evaluar_estrategia_pro(df, symbol=symbol, sesgo_htf=sesgo_htf, vp_info=vp_info)
        if senal:
            st.info(f"**{senal['modelo']}**\n\n**{senal['direccion']}**\n\n{senal['plan']}")
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
