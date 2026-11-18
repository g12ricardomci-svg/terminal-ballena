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
# ⚙️ CONFIGURACIÓN GLOBAL (EXCLUSIVO CRUCE DORADO)
# ==========================================
TELEGRAM_TOKEN = "9148434925:AAH_AuswS-GGPHadFBwcrDOUUIwBFbXvtDs"
TELEGRAM_CHAT_ID = "1496321437"

st.set_page_config(page_title="Institutional Quant Terminal - Cruce Dorado Puro", layout="wide", page_icon="🟡")

NASDAQ_MARKETS = ["NVDA", "AAPL", "MSFT", "TSLA", "AMZN", "GOOGL", "META", "NFLX", "AMD"]
FOREX_MARKETS = ["EURUSD=X", "GBPUSD=X", "USDJPY=X", "AUDUSD=X", "USDCAD=X", "NZDUSD=X"]
INDEX_MARKETS = ["^IXIC", "QQQ", "^GSPC", "^DJI"]
COMMODITY_MARKETS = ["GC=F", "SI=F", "CL=F", "NG=F"]

thread_lock = threading.Lock()
ALERTAS_ENVIADAS_GLOBAL = {}
AUTOPILOTO_STATUS = "Radar purgado: Únicamente Cruce Dorado (5m) activo..."

def enviar_telegram(mensaje):
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    payload = {"chat_id": TELEGRAM_CHAT_ID, "text": mensaje, "parse_mode": "Markdown"}
    try: 
        requests.post(url, json=payload, timeout=5)
    except: 
        pass

def calcular_vwap(df):
    if df['volume'].sum() == 0: return df['close']
    return (df['volume'] * (df['high'] + df['low'] + df['close']) / 3).cumsum() / df['volume'].cumsum()

def clasificar_tipo_mercado(symbol):
    if "/" in symbol and "USDT" in symbol: return "CRIPTO"
    if symbol in NASDAQ_MARKETS: return "ACCION"
    if symbol in FOREX_MARKETS: return "FOREX"
    if symbol in INDEX_MARKETS: return "INDICE"
    if symbol in COMMODITY_MARKETS: return "COMMODITY"
    return "GLOBAL"

def obtener_datos(symbol, timeframe="5m", limit=300):
    df = pd.DataFrame()
    if "/" in symbol and "USDT" in symbol:
        exchange = ccxt.okx({'enableRateLimit': True, 'timeout': 8000})
        try:
            ohlcv = exchange.fetch_ohlcv(symbol, timeframe, limit=limit)
            df = pd.DataFrame(ohlcv, columns=['timestamp', 'open', 'high', 'low', 'close', 'volume'])
            df['timestamp'] = pd.to_datetime(df['timestamp'], unit='ms')
        except: return pd.DataFrame()
    else:
        try:
            ticker = yf.Ticker(symbol)
            hist = ticker.history(period="5d", interval="5m", timeout=8)
            if hist.empty: return pd.DataFrame()
            df = hist.reset_index()
            df.rename(columns={'Datetime': 'timestamp', 'Date': 'timestamp'}, inplace=True)
            df['timestamp'] = pd.to_datetime(df['timestamp']).dt.tz_localize(None)
            df['volume'] = df['volume'].fillna(0)
            df = df.tail(limit)
        except: return pd.DataFrame()

    if not df.empty:
        df['vwap'] = calcular_vwap(df)
        df['sma_rapida'] = df['close'].rolling(window=50).mean()
        df['sma_lenta'] = df['close'].rolling(window=200).mean()
        df['vol_ma'] = df['volume'].rolling(window=20).mean()
    return df

# ==========================================
# 📡 MOTOR PURGADO: SOLO CRUCE DORADO
# ==========================================
def ejecutar_escaneo_cruce_dorado_puro(es_hilo_fondo=False):
    global AUTOPILOTO_STATUS, ALERTAS_ENVIADAS_GLOBAL
    
    if not es_hilo_fondo:
        AUTOPILOTO_STATUS = "Escaneando puramente Cruce Dorado..."
    
    exchange = ccxt.okx({'enableRateLimit': True, 'timeout': 8000})
    try:
        tickers = exchange.fetch_tickers()
        mercados_usdt = [(sym, data['quoteVolume']) for sym, data in tickers.items() if sym.endswith('/USDT') and data.get('quoteVolume')]
        top15_cripto = [sym for sym, vol in sorted(mercados_usdt, key=lambda x: x[1], reverse=True)[:15]]
    except:
        top15_cripto = ["BTC/USDT", "ETH/USDT", "SOL/USDT"]

    mercados_totales = top15_cripto + NASDAQ_MARKETS + FOREX_MARKETS + INDEX_MARKETS + COMMODITY_MARKETS
    alertas_nuevas = 0

    for i, symbol in enumerate(mercados_totales):
        if not es_hilo_fondo:
            AUTOPILOTO_STATUS = f"Analizando [{i+1}/{len(mercados_totales)}]: {symbol}..."
            
        try:
            df = obtener_datos(symbol, '5m', limit=300)
            if df.empty or len(df) < 200: continue
            
            actual = df.iloc[-1]
            anterior = df.iloc[-2]
            precio = actual['close']
            tipo_mercado = clasificar_tipo_mercado(symbol)
            
            # --- CONDICIÓN ÚNICA: CRUCE DORADO (SMA 50 / SMA 200)[cite: 1, 2] ---
            cruce_dorado = (anterior['sma_rapida'] <= anterior['sma_lenta']) and (actual['sma_rapida'] > actual['sma_lenta'])
            volumen_valido = actual['volume'] > (actual['vol_ma'] * 1.5)
            precio_sobre_vwap = precio > actual['vwap']

            if cruce_dorado and volumen_valido and precio_sobre_vwap:
                mensaje = (f"🟡 *ESTRATEGIA: CRUCE DORADO (5M)*\n"
                           f"🌐 Mercado: `{tipo_mercado}`\n"
                           f"🪙 Activo: `{symbol}`\n"
                           f"💲 Precio Actual: `${precio:,.4f}`\n\n"
                           f"📊 *Detalle:* SMA 50 cruzó al alza la SMA 200 con volumen institucional y soporte de VWAP[cite: 1, 2].\n"
                           f"🚀 *Aplicación:* **OPERAR ARRIBA (LONG)** 🟢\n"
                           f"💡 *Instrucción:* Ejecutar orden manual en Vantage / OKX.")

                clave_alerta = f"{symbol}_CRUCE_DORADO_PURGADO"
                ahora = time.time()
                with thread_lock:
                    if ahora - ALERTAS_ENVIADAS_GLOBAL.get(clave_alerta, 0) > 14400:
                        enviar_telegram(mensaje)
                        ALERTAS_ENVIADAS_GLOBAL[clave_alerta] = ahora
                        alertas_nuevas += 1

            time.sleep(0.02)
        except: 
            pass

    with thread_lock:
        if not es_hilo_fondo:
            AUTOPILOTO_STATUS = f"✅ Escaneo limpio finalizado. {alertas_nuevas} alertas."
        else:
            AUTOPILOTO_STATUS = "✅ Radar activo exclusivamente para Cruce Dorado..."
    return alertas_nuevas

@st.cache_resource
def iniciar_hilo_puro():
    def loop():
        while True:
            try: ejecutar_escaneo_cruce_dorado_puro(es_hilo_fondo=True)
            except: pass
            time.sleep(300)
    thread = threading.Thread(target=loop, daemon=True)
    thread.start()
    return thread

iniciar_hilo_puro()

# ==========================================
# 📊 INTERFAZ GRÁFICA LIMPIA
# ==========================================
st.markdown("<h1 style='text-align: center; color: #FFD700;'>🟡 TERMINAL CUANTITATIVA - CRUCE DORADO PURO (5M)</h1>", unsafe_allow_html=True)
st.markdown("<p style='text-align: center; color: gray;'>Sistema 100% Exclusivo de Cruce Dorado (Sin rastro de otras estrategias)</p>", unsafe_allow_html=True)
st.markdown("---")

st.sidebar.header("🌐 Selector de Activos")
lista_global = ["BTC/USDT", "ETH/USDT", "SOL/USDT"] + NASDAQ_MARKETS + FOREX_MARKETS + INDEX_MARKETS + COMMODITY_MARKETS
symbol = st.sidebar.selectbox("Seleccionar Activo para Ver Gráfico:", list(dict.fromkeys(lista_global)))

st.sidebar.markdown("---")
st.sidebar.info(AUTOPILOTO_STATUS)

if st.sidebar.button("⚡ Forzar Escaneo Limpio"):
    with st.spinner("Buscando Cruces Dorados puros..."):
        alertas = ejecutar_escaneo_cruce_dorado_puro(es_hilo_fondo=False)
        st.sidebar.success(f"Escaneo completado. Alertas enviadas: {alertas}")

# --- GRÁFICO EN VIVO ---
@st.fragment(run_every="10s")
def render_panel_puro(symbol):
    df = obtener_datos(symbol, '5m', limit=300)
    if df.empty:
        st.error(f"No hay datos disponibles para {symbol}.")
        return

    col1, col2 = st.columns([2.5, 1])
    with col1:
        fig = make_subplots(rows=2, cols=1, shared_xaxes=True, vertical_spacing=0.03, row_width=[0.2,0.8])
        fig.add_trace(go.Candlestick(x=df['timestamp'], open=df['open'], high=df['high'], low=df['low'], close=df['close'], name='Precio'), row=1, col=1)
        fig.add_trace(go.Scatter(x=df['timestamp'], y=df['sma_rapida'], line=dict(color='#00FFAA', width=2), name='SMA 50'), row=1, col=1)
        fig.add_trace(go.Scatter(x=df['timestamp'], y=df['sma_lenta'], line=dict(color='#FFD700', width=2), name='SMA 200'), row=1, col=1)
        fig.add_trace(go.Scatter(x=df['timestamp'], y=df['vwap'], line=dict(color='#FF00FF', width=1.5, dash='dash'), name='VWAP'), row=1, col=1)
        
        colors = ['#00FFAA' if r['close']>=r['open'] else '#FF3333' for _,r in df.iterrows()]
        fig.add_trace(go.Bar(x=df['timestamp'], y=df['volume'], marker_color=colors, name='Volumen'), row=2, col=1)
        fig.update_layout(template='plotly_dark', xaxis_rangeslider_visible=False, height=600, margin=dict(l=10,r=10,t=10,b=10))
        st.plotly_chart(fig, use_container_width=True)

    with col2:
        st.markdown(f"### ⚙️ Estado Exclusivo")
        actual = df.iloc[-1]
        st.metric("Precio Actual", f"${actual['close']:,.4f}")
        st.metric("SMA 50", f"${actual['sma_rapida']:,.4f}")
        st.metric("SMA 200", f"${actual['sma_lenta']:,.4f}")
        
        if actual['sma_rapida'] > actual['sma_lenta']:
            st.success("🟢 SMA 50 sobre SMA 200 (Alcista)")
        else:
            st.error("🔴 SMA 50 bajo SMA 200 (Bajista)")

render_panel_puro(symbol)
