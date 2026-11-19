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
from datetime import datetime

# ==========================================
# ⚙️ CONFIGURACIÓN GLOBAL & LISTA CERRADA ESTRICTA
# ==========================================
TELEGRAM_TOKEN = "9148434925:AAH_AuswS-GGPHadFBwcrDOUUIwBFbXvtDs"
TELEGRAM_CHAT_ID = "1496321437"

st.set_page_config(page_title="Institutional Quant Terminal - Unified Engine", layout="wide", page_icon="🏦")

# ÚNICOS ACTIVOS PERMITIDOS
CRYPTO_MARKETS = ["BTC/USDT", "ETH/USDT", "BNB/USDT"]
FOREX_MARKETS = ["EURUSD=X", "GBPUSD=X", "AUDUSD=X", "XAUUSD=X", "XAUUSD"]
NASDAQ_MARKETS = [
    "NVDA", "AAPL", "MSFT", "TSLA", "AMZN", "GOOGL", "META",
    "NFLX", "AMD", "AVGO", "COST", "PEP", "ADBE", "QCOM",
    "INTC", "CSCO", "TMUS", "CMCSA", "TXN", "HON", "AMGN",
    "SBUX", "INTU", "AMAT", "BKNG", "ISRG", "MDLZ", "PYPL", "ADI"
]

ALL_TRACKED_SYMBOLS = list(dict.fromkeys(CRYPTO_MARKETS + FOREX_MARKETS + NASDAQ_MARKETS))

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
AUTOPILOTO_STATUS = "Motor unificado activo: Escaneando cada 5s..."

# ==========================================
# 🧠 FUNCIONES MATEMÁTICAS Y DE ESTRUCTURA
# ==========================================
def enviar_telegram(mensaje):
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    payload = {"chat_id": TELEGRAM_CHAT_ID, "text": mensaje, "parse_mode": "Markdown"}
    try:
        requests.post(url, json=payload, timeout=5)
    except Exception:
        pass

def _normalizar_timestamp(serie):
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

def detectar_exhaustacion(df):
    if len(df) < 22:
        return False
    rango_actual = df["high"].iloc[-1] - df["low"].iloc[-1]
    rangos_previos = (df["high"].iloc[-21:-1] - df["low"].iloc[-21:-1])
    max_previo = rangos_previos.max()
    mov_previo = abs(df["close"].iloc[-2] - df["close"].iloc[-10])
    atr_aprox = rangos_previos.mean()
    return (rango_actual > max_previo * 1.15) and (mov_previo > atr_aprox * 3.0)

def evaluar_estrategia_unificada(df):
    if df.empty or len(df) < 25:
        return None

    ultima = df.iloc[-1]
    anterior = df.iloc[-2]
    precio = float(ultima["close"])
    adx_val = float(ultima.get("adx", 0.0))
    vwap_val = float(ultima.get("vwap", precio))
    ema_fast = float(ultima.get("ema_fast", precio))
    ema_slow = float(ultima.get("ema_slow", precio))
    prev_ema_fast = float(anterior.get("ema_fast", precio))
    prev_ema_slow = float(anterior.get("ema_slow", precio))

    vol_actual = float(ultima.get("volume", 0.0))
    vol_ma = float(ultima.get("vol_ma", 0.0))
    desbalance_vol = (vol_ma > 0) and (vol_actual > vol_ma * 1.4)

    cvd_actual = float(ultima.get("cvd", 0.0))
    cvd_prev = float(anterior.get("cvd", 0.0))
    cvd_alcista = cvd_actual > cvd_prev
    cvd_bajista = cvd_actual < cvd_prev

    es_exhaustacion = detectar_exhaustacion(df)

    rangos_previos = (df["high"].iloc[-21:-1] - df["low"].iloc[-21:-1])
    atr_prom = float(rangos_previos.mean()) if len(rangos_previos) > 0 else 0.0
    atr_ref = atr_prom if (pd.notna(atr_prom) and atr_prom > 0) else max(abs(float(ultima["high"]) - float(ultima["low"])), precio * 0.0025)

    swing_low_ref = float(df["low"].iloc[-7:-1].min()) if len(df) >= 8 else float(ultima["low"])
    swing_high_ref = float(df["high"].iloc[-7:-1].max()) if len(df) >= 8 else float(ultima["high"])
    rr_mult = 2.5

    rango_max = float(df["high"].iloc[-20:].max())
    rango_min = float(df["low"].iloc[-20:].min())
    amplitud = max(rango_max - rango_min, 1e-9)
    descuento_compra = ((rango_max - precio) / amplitud) * 100.0
    descuento_venta = ((precio - rango_min) / amplitud) * 100.0

    ob_alcista = (anterior["close"] < anterior["open"]) and (ultima["close"] > ultima["open"]) and (precio > anterior["high"])
    ob_bajista = (anterior["close"] > anterior["open"]) and (ultima["close"] < ultima["open"]) and (precio < anterior["low"])

    cruce_alcista = (ema_fast > ema_slow) and (prev_ema_fast <= prev_ema_slow)
    cruce_bajista = (ema_fast < ema_slow) and (prev_ema_fast >= prev_ema_slow)

    if adx_val <= 50.0:
        return None

    if es_exhaustacion:
        sl_exh = float(swing_low_ref - atr_ref * 0.5) if precio >= ultima["open"] else float(swing_high_ref + atr_ref * 0.5)
        return {
            "id_senal": "EXHAUSTION_CAUTION",
            "estrategia": "Filtro de Exhaustación (Larry Williams)",
            "direccion": "ESPERAR / NO PERSEGUIR ⏸️",
            "sl": sl_exh,
            "tp": vwap_val,
            "plan": "Vela de gran amplitud tras movimiento extendido. No compres en techo ni vendas en suelo; espera retroceso o estabilización."
        }

    if (cruce_alcista or (precio > vwap_val and cvd_alcista)) and (desbalance_vol or ob_alcista or descuento_compra >= 40.0):
        base_low = min(swing_low_ref, float(ultima["low"]))
        sl_sugerido = float(base_low - atr_ref * 0.6)
        riesgo = max(abs(precio - sl_sugerido), atr_ref * 0.8)
        sl_sugerido = float(precio - riesgo)
        tp_sugerido = float(precio + riesgo * rr_mult)
        return {
            "id_senal": "LONG_OUT_OF_BALANCE",
            "estrategia": "Fuera de Balance + ADX Extremo + Order Flow (LONG)",
            "direccion": "COMPRA (LONG) 🟢",
            "sl": sl_sugerido,
            "tp": tp_sugerido,
            "plan": (
                f"1) Confirma cierre de vela por encima de VWAP (${vwap_val:,.4f}) con CVD positivo.\n"
                f"2) Entra en LONG con riesgo controlado (0.5% - 1% max de cuenta).\n"
                f"3) Stop Loss con colchón ATR/estructura en ${sl_sugerido:,.4f}.\n"
                f"4) Take Profit objetivo (1:{rr_mult:.1f}) en ${tp_sugerido:,.4f} o gestiona parciales."
            )
        }

    if (cruce_bajista or (precio < vwap_val and cvd_bajista)) and (desbalance_vol or ob_bajista or descuento_venta >= 40.0):
        base_high = max(swing_high_ref, float(ultima["high"]))
        sl_sugerido = float(base_high + atr_ref * 0.6)
        riesgo = max(abs(sl_sugerido - precio), atr_ref * 0.8)
        sl_sugerido = float(precio + riesgo)
        tp_sugerido = float(precio - riesgo * rr_mult)
        return {
            "id_senal": "SHORT_OUT_OF_BALANCE",
            "estrategia": "Fuera de Balance + ADX Extremo + Order Flow (SHORT)",
            "direccion": "VENTA (SHORT) 🔴",
            "sl": sl_sugerido,
            "tp": tp_sugerido,
            "plan": (
                f"1) Confirma cierre de vela por debajo de VWAP (${vwap_val:,.4f}) con CVD negativo.\n"
                f"2) Entra en SHORT con riesgo controlado (0.5% - 1% max de cuenta).\n"
                f"3) Stop Loss con colchón ATR/estructura en ${sl_sugerido:,.4f}.\n"
                f"4) Take Profit objetivo (1:{rr_mult:.1f}) en ${tp_sugerido:,.4f} (evita promediar en contra)."
            )
        }

    alcista = precio >= vwap_val
    direccion_base = "COMPRA / CONTINUACIÓN ALCISTA 🟢" if alcista else "VENTA / CONTINUACIÓN BAJISTA 🔴"
    if alcista:
        base_low = min(swing_low_ref, float(ultima["low"]))
        sl_base = float(min(base_low - atr_ref * 0.5, precio - atr_ref * 1.0))
        riesgo_base = max(abs(precio - sl_base), atr_ref * 1.0)
        sl_base = float(precio - riesgo_base)
        tp_base = float(precio + riesgo_base * rr_mult)
    else:
        base_high = max(swing_high_ref, float(ultima["high"]))
        sl_base = float(max(base_high + atr_ref * 0.5, precio + atr_ref * 1.0))
        riesgo_base = max(abs(sl_base - precio), atr_ref * 1.0)
        sl_base = float(precio + riesgo_base)
        tp_base = float(precio - riesgo_base * rr_mult)

    return {
        "id_senal": f"ADX50_CORE_{'UP' if alcista else 'DN'}",
        "estrategia": "Super Tendencia ADX > 50 + Estructura / VWAP",
        "direccion": direccion_base,
        "sl": sl_base,
        "tp": tp_base,
        "plan": (
            f"1) Opera a favor de la fuerza dominante (ADX {adx_val:.1f} > 50).\n"
            f"2) Valida retroceso hacia zona de valor/VWAP o bloque cercano sin perseguir picos.\n"
            f"3) Stop Loss en ${sl_base:,.4f} | Take Profit (1:{rr_mult:.1f}) en ${tp_base:,.4f}."
        )
    }

# ==========================================
# 🛠️ OBTENCIÓN DE DATOS CON CACHÉ Y VALIDACIÓN REAL
# ==========================================
def obtener_datos(symbol, timeframe, limit=120, ttl_segundos=25):
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
    else:
        interval_map = {"5m": "5m", "15m": "15m", "1h": "60m"}
        yf_interval = interval_map.get(timeframe, "60m")
        periodos_a_probar = ["5d", "1mo"] if timeframe == "5m" else ["1mo", "3mo"]

        simbolos_yf = [symbol]
        if symbol in ("XAUUSD", "XAUUSD=X"):
            simbolos_yf = ["XAUUSD=X", "GC=F"]

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
        freq_map = {"5m": "5min", "15m": "15min", "1h": "1h"}
        fechas = pd.date_range(end=datetime.now(), periods=limit, freq=freq_map.get(timeframe, "15min"))
        base_val = 2650.0 if "XAU" in symbol else (1.085 if "EUR" in symbol else 150.0)
        rng = np.random.default_rng(abs(hash(symbol + timeframe)) % (2**32))
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
# 🤖 MOTOR UNIFICADO EN FONDO (CADA 5 SEGUNDOS)
# ==========================================
def ejecutar_logica_autopiloto(es_hilo_fondo=False):
    global AUTOPILOTO_STATUS, ALERTAS_ENVIADAS_GLOBAL

    if not es_hilo_fondo:
        AUTOPILOTO_STATUS = "Ejecutando barrido unificado..."

    temporalidades = ["5m", "15m", "1h"]
    alertas_nuevas = 0

    for symbol in ALL_TRACKED_SYMBOLS:
        tipo = "CRIPTO" if symbol in CRYPTO_MARKETS else ("FOREX/METAL" if symbol in FOREX_MARKETS else "NASDAQ")

        for tf in temporalidades:
            if not es_hilo_fondo:
                AUTOPILOTO_STATUS = f"Analizando {symbol} [{tf}]..."

            try:
                df = obtener_datos(symbol, tf, limit=120, ttl_segundos=25)
                if df.empty or not df.attrs.get("es_real", False):
                    continue

                resultado = evaluar_estrategia_unificada(df)
                if resultado:
                    ultima = df.iloc[-1]
                    precio = float(ultima["close"])
                    adx_val = float(ultima.get("adx", 0.0))

                    msg = (
                        f"🏦 *SEÑAL UNIFICADA (MOTOR 5s)*\n"
                        f"🌐 *Mercado:* `{tipo}` | *Activo:* `{symbol}` | *TF:* `{tf}`\n"
                        f"💲 *Precio Real:* `${precio:,.4f}` | *ADX:* `{adx_val:.1f}`\n"
                        f"🎯 *Estrategia:* {resultado['estrategia']}\n"
                        f"🧭 *Acción:* *{resultado['direccion']}*\n"
                        f"🛡️ *SL Ref:* `${resultado['sl']:,.4f}` | *TP Ref:* `${resultado['tp']:,.4f}`\n"
                        f"📋 *Qué hacer:*\n{resultado['plan']}"
                    )

                    clave_alerta = f"{symbol}_{tf}_{resultado['id_senal']}"
                    ahora = time.time()

                    if ahora - ALERTAS_ENVIADAS_GLOBAL.get(clave_alerta, 0) > 600:
                        enviar_telegram(msg)
                        ALERTAS_ENVIADAS_GLOBAL[clave_alerta] = ahora
                        alertas_nuevas += 1

                time.sleep(0.03)
            except Exception:
                pass

    if not es_hilo_fondo:
        AUTOPILOTO_STATUS = f"✅ Barrido listo. Alertas reales enviadas: {alertas_nuevas}"
    else:
        AUTOPILOTO_STATUS = "⚡ Motor unificado activo (cada 5s) solo con datos reales..."

@st.cache_resource
def iniciar_hilo_autopiloto():
    def loop():
        while True:
            try:
                ejecutar_logica_autopiloto(es_hilo_fondo=True)
            except Exception:
                pass
            time.sleep(5)
    thread = threading.Thread(target=loop, daemon=True)
    thread.start()
    return thread

iniciar_hilo_autopiloto()

# ==========================================
# 📊 INTERFAZ GRÁFICA PRINCIPAL
# ==========================================
st.markdown("<h1 style='text-align: center; color: #00FFAA;'>🏦 INSTITUTIONAL QUANT TERMINAL — UNIFIED ENGINE</h1>", unsafe_allow_html=True)
st.markdown("<p style='text-align: center; color: gray;'>Escaneo Cada 5s • ADX + Desbalance/CVD + Order Blocks/Descuento + Filtro Exhaustación</p>", unsafe_allow_html=True)
st.markdown("---")

st.sidebar.header("🌐 Configuración del Gráfico")
symbol = st.sidebar.selectbox("Ver Gráfico de:", ALL_TRACKED_SYMBOLS)
timeframe = st.sidebar.selectbox("Temporalidad", ["5m", "15m", "1h"])

st.sidebar.markdown("---")
st.sidebar.subheader("📡 Estado del Motor (5s)")
st.sidebar.info(AUTOPILOTO_STATUS)

if st.sidebar.button("⚡ Forzar Escaneo Unificado Ahora"):
    with st.spinner("Ejecutando motor unificado en todos los activos..."):
        ejecutar_logica_autopiloto(es_hilo_fondo=False)
        st.sidebar.success("¡Escaneo unificado completado!")

with st.sidebar.expander("🔍 Diagnóstico Cripto / Oro"):
    for sym_diag in ["BTC/USDT", "ETH/USDT", "BNB/USDT", "XAUUSD=X"]:
        df_d = obtener_datos(sym_diag, timeframe, limit=120, ttl_segundos=20)
        real_txt = "REAL ✅" if df_d.attrs.get("es_real", False) else "SIN DATO REAL ❌"
        adx_d = float(df_d["adx"].iloc[-1]) if not df_d.empty and "adx" in df_d.columns else 0.0
        st.caption(f"{sym_diag}: {real_txt} | ADX: {adx_d:.1f}")

@st.fragment(run_every="5s")
def render_trading_panel(symbol, timeframe):
    df = obtener_datos(symbol, timeframe, limit=150, ttl_segundos=15)
    if df.empty:
        st.error(f"No se pudieron cargar los datos para {symbol}.")
        return

    if not df.attrs.get("es_real", False):
        st.warning("⚠️ Datos de respaldo activos por límite temporal de proveedor externo. No se enviarán alertas de Telegram con estos datos.")

    col_grafico, col_paneles = st.columns([2.5, 1])
    with col_grafico:
        fig = make_subplots(rows=2, cols=1, shared_xaxes=True, vertical_spacing=0.03, row_width=[0.2, 0.8])
        fig.add_trace(go.Candlestick(
            x=df["timestamp"], open=df["open"], high=df["high"], low=df["low"], close=df["close"],
            increasing_line_color="#00FFAA", decreasing_line_color="#FF3333", name="Precio"
        ), row=1, col=1)
        fig.add_trace(go.Scatter(x=df["timestamp"], y=df["vwap"], line=dict(color="#FF00FF", width=2), name="VWAP"), row=1, col=1)
        fig.add_trace(go.Scatter(x=df["timestamp"], y=df["ema_fast"], line=dict(color="#FFFF00", width=1.5), name="EMA 9"), row=1, col=1)
        fig.add_trace(go.Scatter(x=df["timestamp"], y=df["ema_slow"], line=dict(color="#00BFFF", width=1.5), name="EMA 21"), row=1, col=1)
        fig.add_trace(go.Scatter(x=df["timestamp"], y=df["soporte"], line=dict(color="#00FF00", width=1, dash="dot"), name="Soporte 20"), row=1, col=1)
        fig.add_trace(go.Scatter(x=df["timestamp"], y=df["resistencia"], line=dict(color="#FF0000", width=1, dash="dot"), name="Resistencia 20"), row=1, col=1)
        colors = ["#00FFAA" if r["close"] >= r["open"] else "#FF3333" for _, r in df.iterrows()]
        fig.add_trace(go.Bar(x=df["timestamp"], y=df["volume"], marker_color=colors, name="Volumen"), row=2, col=1)
        fig.update_layout(template="plotly_dark", xaxis_rangeslider_visible=False, height=600, margin=dict(l=10, r=10, t=10, b=10), showlegend=True)
        st.plotly_chart(fig, use_container_width=True)

    with col_paneles:
        st.markdown("### ⚙️ Lectura de Flujo y Estructura")
        ultima = df.iloc[-1]
        anterior = df.iloc[-2]
        precio_actual = float(ultima["close"])
        st.metric(label=f"{symbol} [{timeframe}]", value=f"${precio_actual:,.4f}", delta=f"{precio_actual - float(anterior['close']):,.4f}")

        if precio_actual < float(ultima["vwap"]):
            st.error("🔴 SESGO VS VWAP: BAJISTA")
        else:
            st.success("🟢 SESGO VS VWAP: ALCISTA")

        adx_val = float(ultima.get("adx", 25.0))
        if adx_val > 50:
            st.success(f"🔥 ADX: {adx_val:.1f} (>50 Fuerza Extrema)")
        elif adx_val > 25:
            st.warning(f"📈 ADX: {adx_val:.1f} (Tendencia activa)")
        else:
            st.info(f"⚖️ ADX: {adx_val:.1f} (Rango / Consolidación)")

        cvd_actual = float(ultima.get("cvd", 0.0))
        cvd_prev = float(anterior.get("cvd", 0.0))
        st.caption(f"CVD Delta: {'🟢 Presión compradora' if cvd_actual >= cvd_prev else '🔴 Presión vendedora'}")

        st.markdown("---")
        st.markdown("### 🎯 Plan Sugerido por Motor")
        senal = evaluar_estrategia_unificada(df)
        if senal:
            st.info(f"**{senal['estrategia']}**\n\n**Acción:** {senal['direccion']}\n\n{senal['plan']}")
        else:
            st.caption("Sin señal activa bajo el filtro unificado en esta vela.")

render_trading_panel(symbol, timeframe)
