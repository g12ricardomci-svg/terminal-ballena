import streamlit as st
import ccxt
import yfinance as yf
import pandas as pd
import plotly.graph_objects as go
from plotly.subplots import make_subplots
import requests
import time
import threading
from datetime import datetime, time as dt_time
import pytz

# ==========================================
# ⚙️ CONFIGURACIÓN GLOBAL (Usa st.secrets o tus datos nuevos)
# ==========================================
TELEGRAM_TOKEN = st.secrets.get("TELEGRAM_TOKEN", "TU_NUEVO_TOKEN_AQUI")
TELEGRAM_CHAT_ID = st.secrets.get("TELEGRAM_CHAT_ID", "TU_CHAT_ID_AQUI")

st.set_page_config(page_title="Institutional Quant Terminal", layout="wide", page_icon="🏦")

NASDAQ_MARKETS = ["NVDA", "AAPL", "MSFT", "TSLA", "AMZN", "GOOGL", "META"]
DOLLAR_MARKETS = ["DX-Y.NYB", "EURUSD=X", "GBPUSD=X", "USDJPY=X", "USDMXN=X", "USDBRL=X"]

ALERTAS_ENVIADAS_GLOBAL = {}
AUTOPILOTO_STATUS = "Iniciando motor de escaneo..."

def enviar_telegram(mensaje):
    if "TU_NUEVO_TOKEN" in TELEGRAM_TOKEN:
        return
    url = f"https://telegram.org{TELEGRAM_TOKEN}/sendMessage"
    payload = {"chat_id": TELEGRAM_CHAT_ID, "text": mensaje, "parse_mode": "Markdown"}
    try:
        requests.post(url, json=payload, timeout=5)
    except Exception:
        pass

def calcular_vwap(df):
    if df['volume'].sum() == 0:
        return df['close']
    df_temp = df.copy()
    df_temp['fecha'] = df_temp['timestamp'].dt.date
    tp = (df_temp['high'] + df_temp['low'] + df_temp['close']) / 3
    df_temp['pv'] = tp * df_temp['volume']
    return df_temp.groupby('fecha')['pv'].cumsum() / df_temp.groupby('fecha')['volume'].cumsum()

def mercado_abierto(tipo_mercado):
    ny_tz = pytz.timezone('America/New_York')
    ahora = datetime.now(ny_tz)
    dia = ahora.weekday()
    hora = ahora.time()
    if tipo_mercado == "CRIPTO":
        return True
    if tipo_mercado == "ACCION":
        if dia >= 5:
            return False
        return dt_time(9, 30) <= hora <= dt_time(16, 0)
    if tipo_mercado == "FOREX":
        if dia == 4 and hora > dt_time(17, 0):
            return False
        if dia == 5:
            return False
        if dia == 6 and hora < dt_time(17, 0):
            return False
        return True
    return False

def obtener_datos(symbol, timeframe, limit=100):
    df = pd.DataFrame()
    if "/" in symbol and "USDT" in symbol:
        exchange = ccxt.okx()
        try:
            ohlcv = exchange.fetch_ohlcv(symbol, timeframe, limit=limit)
            df = pd.DataFrame(ohlcv, columns=['timestamp', 'open', 'high', 'low', 'close', 'volume'])
            df['timestamp'] = pd.to_datetime(df['timestamp'], unit='ms')
        except Exception:
            return pd.DataFrame()
    else:
        interval_map = {"1m": "1m", "5m": "5m", "15m": "15m", "1h": "60m"}
        yf_interval = interval_map.get(timeframe, "5m")
        period = "5d" if yf_interval in ["1m", "5m", "15m"] else "1mo"
        try:
            ticker = yf.Ticker(symbol)
            hist = ticker.history(period=period, interval=yf_interval)
            if hist.empty:
                return pd.DataFrame()
            df = hist.reset_index()
            df.rename(columns={'Datetime': 'timestamp', 'Date': 'timestamp'}, inplace=True)
            df['timestamp'] = pd.to_datetime(df['timestamp']).dt.tz_localize(None)
            df['volume'] = df['volume'].fillna(0)
            df = df.tail(limit)
        except Exception:
            return pd.DataFrame()

    if not df.empty:
        df['vwap'] = calcular_vwap(df)
        df['soporte'] = df['low'].rolling(window=20).min()
        df['resistencia'] = df['high'].rolling(window=20).max()
        df['vol_ma'] = df['volume'].rolling(window=20).mean()
    return df

# ==========================================
# 🤖 LÓGICA DEL AUTOPILOTO (Solo detección de Clímax y Estructura)
# ==========================================
def ejecutar_logica_autopiloto(es_hilo_fondo=False):
    global AUTOPILOTO_STATUS, ALERTAS_ENVIADAS_GLOBAL

    if not es_hilo_fondo:
        AUTOPILOTO_STATUS = "Buscando los 50 mercados..."

    exchange = ccxt.okx()
    try:
        tickers = exchange.fetch_tickers()
        mercados_usdt = [
            (sym, data['quoteVolume'])
            for sym, data in tickers.items()
            if sym.endswith('/USDT') and data.get('quoteVolume')
        ]
        top50_cripto = [sym for sym, vol in sorted(mercados_usdt, key=lambda x: x[1], reverse=True)[:50]]
    except Exception:
        top50_cripto = ["BTC/USDT", "ETH/USDT", "SOL/USDT"]

    mercados_a_escanear = top50_cripto + NASDAQ_MARKETS + DOLLAR_MARKETS
    alertas_nuevas = 0

    for i, symbol in enumerate(mercados_a_escanear):
        if not es_hilo_fondo:
            AUTOPILOTO_STATUS = f"Analizando {symbol} ({i+1}/{len(mercados_a_escanear)})..."

        try:
            tipo = "CRIPTO" if "/" in symbol else ("ACCION" if symbol in NASDAQ_MARKETS else "FOREX")
            if not mercado_abierto(tipo):
                continue

            df_5m = obtener_datos(symbol, '5m', limit=50)
            df_1h = obtener_datos(symbol, '1h', limit=50)
            if len(df_5m) < 25 or len(df_1h) < 25:
                continue

            # Usamos la última vela CERRADA (-2) para evitar señales falsas en curso
            vela_cerrada = df_5m.iloc[-2]
            precio_actual = df_5m.iloc[-1]['close']
            ema50_1h = df_1h['close'].ewm(span=50).mean().iloc[-1]
            macro = "ALCISTA" if df_1h['close'].iloc[-1] > ema50_1h else "BAJISTA"

            señal = None
            msg = ""

            # 1. SPRING
            if (vela_cerrada['low'] <= vela_cerrada['soporte'] and vela_cerrada['close'] > vela_cerrada['soporte']) and macro == "ALCISTA":
                señal = "SPRING"
                msg = f"🚀 *AUTOPILOTO: SPRING ALCISTA ({tipo})*\n🪙 {symbol} | ${precio_actual:,.4f}"

            # 2. UPTHRUST
            elif (vela_cerrada['high'] >= vela_cerrada['resistencia'] and vela_cerrada['close'] < vela_cerrada['resistencia']) and macro == "BAJISTA":
                señal = "UPTHRUST"
                msg = f"🔻 *AUTOPILOTO: UPTHRUST BAJISTA ({tipo})*\n🪙 {symbol} | ${precio_actual:,.4f}"

            # 3. VOLUMEN DE CLÍMAX (Sin Fibonacci, solo alerta de agotamiento)
            elif tipo != "FOREX" and vela_cerrada['volume'] > (vela_cerrada['vol_ma'] * 2.5):
                en_techo = vela_cerrada['high'] >= vela_cerrada['resistencia']
                en_suelo = vela_cerrada['low'] <= vela_cerrada['soporte']
                ratio_vol = vela_cerrada['volume'] / vela_cerrada['vol_ma']

                if en_suelo and vela_cerrada['close'] < vela_cerrada['open']:
                    señal = "SELLING_CLIMAX"
                    msg = (
                        f"⚠️ *AUTOPILOTO: SELLING CLIMAX ({tipo})*\n"
                        f"🪙 *{symbol}* | Precio: ${precio_actual:,.4f}\n"
                        f"📊 *Volumen:* {ratio_vol:.1f}x sobre la media\n"
                        f"📉 *Posible agotamiento bajista* (Mínimo vela: ${vela_cerrada['low']:,.4f})"
                    )
                elif en_techo and vela_cerrada['close'] > vela_cerrada['open']:
                    señal = "BUYING_CLIMAX"
                    msg = (
                        f"⚠️ *AUTOPILOTO: BUYING CLIMAX ({tipo})*\n"
                        f"🪙 *{symbol}* | Precio: ${precio_actual:,.4f}\n"
                        f"📊 *Volumen:* {ratio_vol:.1f}x sobre la media\n"
                        f"📈 *Posible agotamiento alcista* (Máximo vela: ${vela_cerrada['high']:,.4f})"
                    )

            if señal and msg:
                clave_alerta = f"{symbol}_{señal}"
                ahora = time.time()
                ultimo_envio = ALERTAS_ENVIADAS_GLOBAL.get(clave_alerta, 0)

                if ahora - ultimo_envio > 1800:
                    enviar_telegram(msg)
                    ALERTAS_ENVIADAS_GLOBAL[clave_alerta] = ahora
                    alertas_nuevas += 1

            time.sleep(0.3)
        except Exception:
            pass

    if not es_hilo_fondo:
        AUTOPILOTO_STATUS = f"✅ Completado. {alertas_nuevas} alertas enviadas."
    else:
        AUTOPILOTO_STATUS = "✅ Escaneo de fondo completado. Durmiendo 5 min..."
        return alertas_nuevas

# ==========================================
# 🧵 HILO DE FONDO
# ==========================================
@st.cache_resource
def iniciar_hilo_autopiloto():
    def loop():
        while True:
            ejecutar_logica_autopiloto(es_hilo_fondo=True)
            time.sleep(300)
    thread = threading.Thread(target=loop, daemon=True)
    thread.start()
    return thread

iniciar_hilo_autopiloto()

# ==========================================
# 🖥️ INTERFAZ DE STREAMLIT
# ==========================================
st.title("🏦 Institutional Quant Terminal")
st.info(f"Estado del motor: **{AUTOPILOTO_STATUS}**")

col1, col2, col3 = st.columns([2, 1, 1])
with col1:
    simbolo_seleccionado = st.selectbox("Selecciona activo para analizar", ["BTC/USDT", "ETH/USDT", "SOL/USDT"] + NASDAQ_MARKETS + DOLLAR_MARKETS)
with col2:
    tf_seleccionado = st.selectbox("Temporalidad", ["5m", "15m", "1h"], index=0)
with col3:
    st.write("")
    st.write("")
    if st.button("🔄 Ejecutar Escaneo Manual"):
        with st.spinner("Escaneando mercados..."):
            ejecutar_logica_autopiloto(es_hilo_fondo=False)
        st.rerun()

df_grafico = obtener_datos(simbolo_seleccionado, tf_seleccionado, limit=80)

if not df_grafico.empty:
    fig = make_subplots(rows=2, cols=1, shared_xaxes=True, vertical_spacing=0.05, row_heights=[0.75, 0.25])

    fig.add_trace(go.Candlestick(
        x=df_grafico['timestamp'],
        open=df_grafico['open'],
        high=df_grafico['high'],
        low=df_grafico['low'],
        close=df_grafico['close'],
        name="Precio"
    ), row=1, col=1)

    fig.add_trace(go.Scatter(x=df_grafico['timestamp'], y=df_grafico['vwap'], line=dict(color='orange', width=1.5), name="VWAP"), row=1, col=1)

