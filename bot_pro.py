import streamlit as st
import ccxt
import yfinance as yf
import pandas as pd
import plotly.graph_objects as go
from plotly.subplots import make_subplots
import requests
import feedparser
import time
import threading
from datetime import datetime, time as dt_time
import pytz

try:
    import MetaTrader5 as mt5
    MT5_DISPONIBLE = True
except ImportError:
    MT5_DISPONIBLE = False

# ==========================================
# ⚙️ CONFIGURACIÓN GLOBAL & VANTAGE MARKETS
# ==========================================
TELEGRAM_TOKEN = "9148434925:AAH_AuswS-GGPHadFBwcrDOUUIwBFbXvtDs"
TELEGRAM_CHAT_ID = "1496321437"

st.set_page_config(page_title="Institutional Quant Terminal - Multi-TF", layout="wide", page_icon="🏦")

NASDAQ_MARKETS = ["NVDA", "AAPL", "MSFT", "TSLA", "AMZN", "GOOGL", "META", "NFLX", "AMD"]
FOREX_MARKETS = ["EURUSD=X", "GBPUSD=X", "USDJPY=X", "AUDUSD=X", "USDCAD=X", "NZDUSD=X", "USDCHF=X", "EURGBP=X", "EURJPY=X", "GBPJPY=X"]
INDEX_MARKETS = ["^IXIC", "QQQ", "^GSPC", "^DJI", "^GDAXI", "^FTSE", "^N225", "DX-Y.NYB"]
COMMODITY_MARKETS = ["GC=F", "SI=F", "CL=F", "BZ=F", "NG=F"]

thread_lock = threading.Lock()
ALERTAS_ENVIADAS_GLOBAL = {}
AUTOPILOTO_STATUS = "Iniciando motor multi-temporalidad..."

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

def mercado_abierto(tipo_mercado):
    ny_tz = pytz.timezone('America/New_York')
    ahora = datetime.now(ny_tz)
    dia = ahora.weekday()
    hora = ahora.time()
    if tipo_mercado == "CRIPTO": return True
    if tipo_mercado in ["ACCION", "INDICE", "COMMODITY"]:
        if dia >= 5: return False
        return dt_time(9, 30) <= hora <= dt_time(16, 0)
    if tipo_mercado == "FOREX":
        if dia == 4 and hora > dt_time(17, 0): return False
        if dia == 5: return False
        if dia == 6 and hora < dt_time(17, 0): return False
        return True
    return False

def clasificar_tipo_mercado(symbol):
    if "/" in symbol and "USDT" in symbol: return "CRIPTO"
    if symbol in NASDAQ_MARKETS: return "ACCION"
    if symbol in FOREX_MARKETS: return "FOREX"
    if symbol in INDEX_MARKETS: return "INDICE"
    if symbol in COMMODITY_MARKETS: return "COMMODITY"
    return "CRIPTO"

def obtener_datos(symbol, timeframe, limit=100):
    df = pd.DataFrame()
    if "/" in symbol and "USDT" in symbol:
        exchange = ccxt.okx({'enableRateLimit': True, 'timeout': 8000})
        try:
            ohlcv = exchange.fetch_ohlcv(symbol, timeframe, limit=limit)
            df = pd.DataFrame(ohlcv, columns=['timestamp', 'open', 'high', 'low', 'close', 'volume'])
            df['timestamp'] = pd.to_datetime(df['timestamp'], unit='ms')
        except: return pd.DataFrame()
    else:
        interval_map = {"5m": "5m", "15m": "15m", "1h": "60m"} 
        yf_interval = interval_map.get(timeframe, "15m")
        period = "1d" if yf_interval in ["5m", "15m"] else "5d"
        try:
            ticker = yf.Ticker(symbol)
            hist = ticker.history(period=period, interval=yf_interval, timeout=8)
            if hist.empty: return pd.DataFrame()
            df = hist.reset_index()
            df.rename(columns={'Datetime': 'timestamp', 'Date': 'timestamp'}, inplace=True)
            df['timestamp'] = pd.to_datetime(df['timestamp']).dt.tz_localize(None)
            df['volume'] = df['volume'].fillna(0)
            df = df.tail(limit)
        except: return pd.DataFrame()

    if not df.empty:
        df['vwap'] = calcular_vwap(df)
        df['soporte'] = df['low'].rolling(window=20).min()
        df['resistencia'] = df['high'].rolling(window=20).max()
        df['vol_ma'] = df['volume'].rolling(window=20).mean()
        df['ema_50'] = df['close'].ewm(span=50).mean()
    return df

# ==========================================
# 🤖 MOTOR MULTI-TEMPORALIDAD (1h + 15m + 5m)
# ==========================================
def ejecutar_logica_autopiloto(es_hilo_fondo=False):
    global AUTOPILOTO_STATUS, ALERTAS_ENVIADAS_GLOBAL
    
    if not es_hilo_fondo:
        AUTOPILOTO_STATUS = "Buscando confluencia en 3 temporalidades..."
    
    exchange = ccxt.okx({'enableRateLimit': True, 'timeout': 8000})
    try:
        tickers = exchange.fetch_tickers()
        mercados_usdt = [(sym, data['quoteVolume']) for sym, data in tickers.items() if sym.endswith('/USDT') and data.get('quoteVolume')]
        top10_cripto = [sym for sym, vol in sorted(mercados_usdt, key=lambda x: x[1], reverse=True)[:10]]
    except:
        top10_cripto = ["BTC/USDT", "ETH/USDT", "SOL/USDT"]

    mercados_a_escanear = top10_cripto + NASDAQ_MARKETS[:3] + ["EURUSD=X", "^IXIC", "GC=F"]
    alertas_nuevas = 0

    for i, symbol in enumerate(mercados_a_escanear):
        if not es_hilo_fondo:
            AUTOPILOTO_STATUS = f"Analizando {symbol} (Multi-TF {i+1}/{len(mercados_a_escanear)})..."
            
        try:
            tipo = clasificar_tipo_mercado(symbol)
            if not mercado_abierto(tipo): continue

            # 1. CARGAR LAS TRES TEMPORALIDADES
            df_1h = obtener_datos(symbol, '1h', limit=50)
            df_15m = obtener_datos(symbol, '15m', limit=50)
            df_5m = obtener_datos(symbol, '5m', limit=50)
            
            if df_1h.empty or df_15m.empty or df_5m.empty: continue
            
            # Últimas velas de cada temporalidad
            u_1h = df_1h.iloc[-1]
            u_15m = df_15m.iloc[-1]
            u_5m = df_5m.iloc[-1]
            
            precio = u_5m['close']
            
            # --- FILTRO DE CONFLUENCIA MULTI-TEMPORALIDAD ---
            # Tendencia Macro (1h)
            tendencia_1h = "ALCISTA" if u_1h['close'] > u_1h['ema_50'] else "BAJISTA"
            
            # Estructura en 15m (Clímax o Spring/Upthrust)
            volumen_anomalo_15m = u_15m['volume'] > (u_15m['vol_ma'] * 2.0)
            cerca_soporte_15m = u_15m['low'] <= (u_15m['soporte'] * 1.015)
            cerca_resistencia_15m = u_15m['high'] >= (u_15m['resistencia'] * 0.985)
            
            # Gatillo en 5m (Rechazo o cruce de VWAP)
            respeta_vwap_5m = u_5m['close'] > u_5m['vwap'] if tendencia_1h == "ALCISTA" else u_5m['close'] < u_5m['vwap']
            
            señal = None
            tipo_orden = ""
            msg = ""
            
            # REGLA DE CONFLUENCIA COMPLETA:
            # Si 1h es alcista, 15m hace clímax en soporte y 5m confirma sobre el VWAP -> COMPRA
            if tendencia_1h == "ALCISTA" and volumen_anomalo_15m and cerca_soporte_15m and respeta_vwap_5m:
                señal = "MULTITF_COMPRA"
                tipo_orden = "COMPRA"
                msg = (f"🚀 *CONFLUENCIA 3-TF: COMPRA INSTITUCIONAL*\n"
                       f"🪙 Activo: `{symbol}` ({tipo})\n"
                       f"💲 Precio: `${precio:,.4f}`\n"
                       f"📈 *1h:* Alcista | *15m:* Clímax Soporte | *5m:* Confirmado en VWAP.\n"
                       f"💡 *Acción:* Zona ideal para pullback sin pagar spread excesivo.")

            # Si 1h es bajista, 15m hace clímax en resistencia y 5m confirma bajo el VWAP -> VENTA
            elif tendencia_1h == "BAJISTA" and volumen_anomalo_15m and cerca_resistencia_15m and respeta_vwap_5m:
                señal = "MULTITF_VENTA"
                tipo_orden = "VENTA"
                msg = (f"🔻 *CONFLUENCIA 3-TF: VENTA INSTITUCIONAL*\n"
                       f"🪙 Activo: `{symbol}` ({tipo})\n"
                       f"💲 Precio: `${precio:,.4f}`\n"
                       f"📉 *1h:* Bajista | *15m:* Clímax Resistencia | *5m:* Confirmado bajo VWAP.\n"
                       f"💡 *Acción:* Giro bajista validado por las 3 temporalidades.")

            if señal and msg:
                clave_alerta = f"{symbol}_{señal}"
                ahora = time.time()
                with thread_lock:
                    if ahora - ALERTAS_ENVIADAS_GLOBAL.get(clave_alerta, 0) > 1800:
                        enviar_telegram(msg)
                        ALERTAS_ENVIADAS_GLOBAL[clave_alerta] = ahora
                        alertas_nuevas += 1

            time.sleep(0.05)
        except: 
            pass

    with thread_lock:
        if not es_hilo_fondo:
            AUTOPILOTO_STATUS = f"✅ Escaneo Multi-TF completado. {alertas_nuevas} alertas."
        else:
            AUTOPILOTO_STATUS = "✅ Radar 3-TF activo. Esperando ciclo..."
    return alertas_nuevas

@st.cache_resource
def iniciar_hilo_autopiloto():
    def loop():
        while True:
            try: ejecutar_logica_autopiloto(es_hilo_fondo=True)
            except: pass
            time.sleep(180)
    thread = threading.Thread(target=loop, daemon=True)
    thread.start()
    return thread

iniciar_hilo_autopiloto()

# ==========================================
# 📊 INTERFAZ GRÁFICA
# ==========================================
st.markdown("<h1 style='text-align: center; color: #00FFAA;'>🏦 INSTITUTIONAL QUANT TERMINAL (MULTI-TF)</h1>", unsafe_allow_html=True)
st.markdown("<p style='text-align: center; color: gray;'>Sistema de Confluencia Simultánea: 1 Hora + 15 Minutos + 5 Minutos</p>", unsafe_allow_html=True)
st.markdown("---")

st.sidebar.header("🌐 Configuración")
lista_grafico = ["EURUSD=X", "^IXIC", "QQQ", "BTC/USDT", "ETH/USDT", "SOL/USDT"] + NASDAQ_MARKETS
symbol = st.sidebar.selectbox("Seleccionar Activo:", list(dict.fromkeys(lista_grafico)))
timeframe = st.sidebar.selectbox("Temporalidad Gráfico", ["5m","15m","1h"], index=1)

st.sidebar.markdown("---")
st.sidebar.info(AUTOPILOTO_STATUS)

if st.sidebar.button("⚡ Forzar Ciclo Multi-TF Ahora"):
    with st.spinner("Analizando 1h + 15m + 5m..."):
        alertas = ejecutar_logica_autopiloto(es_hilo_fondo=False)
        st.sidebar.success(f"Escaneo finalizado. Alertas enviadas: {alertas}")

# --- GRÁFICO EN VIVO ---
@st.fragment(run_every="10s")
def render_trading_panel(symbol, timeframe):
    df = obtener_datos(symbol, timeframe, limit=150)
    if df.empty:
        st.error(f"No hay datos para {symbol}.")
        return

    col1, col2 = st.columns([2.5, 1])
    with col1:
        fig = make_subplots(rows=2, cols=1, shared_xaxes=True, vertical_spacing=0.03, row_width=[0.2,0.8])
        fig.add_trace(go.Candlestick(x=df['timestamp'], open=df['open'], high=df['high'], low=df['low'], close=df['close'], increasing_line_color='#00FFAA', decreasing_line_color='#FF3333'), row=1, col=1)
        fig.add_trace(go.Scatter(x=df['timestamp'], y=df['vwap'], line=dict(color='#FF00FF', width=2), name='VWAP'), row=1, col=1)
        fig.add_trace(go.Scatter(x=df['timestamp'], y=df['soporte'], line=dict(color='#00FF00', width=1, dash='dot'), name='Soporte'), row=1, col=1)
        fig.add_trace(go.Scatter(x=df['timestamp'], y=df['resistencia'], line=dict(color='#FF0000', width=1, dash='dot'), name='Resistencia'), row=1, col=1)
        colors = ['#00FFAA' if r['close']>=r['open'] else '#FF3333' for _,r in df.iterrows()]
        fig.add_trace(go.Bar(x=df['timestamp'], y=df['volume'], marker_color=colors, name='Volumen'), row=2, col=1)
        fig.update_layout(template='plotly_dark', xaxis_rangeslider_visible=False, height=600, margin=dict(l=10,r=10,t=10,b=10), showlegend=False)
        st.plotly_chart(fig, use_container_width=True)

    with col2:
        st.markdown(f"### ⚙️ Estado Multi-TF")
        df_1h = obtener_datos(symbol, '1h', limit=20)
        df_15m = obtener_datos(symbol, '15m', limit=20)
        df_5m = obtener_datos(symbol, '5m', limit=20)
        
        if not df_1h.empty and not df_15m.empty and not df_5m.empty:
            t_1h = "ALCISTA 🟢" if df_1h.iloc[-1]['close'] > df_1h.iloc[-1]['ema_50'] else "BAJISTA 🔴"
            st.metric("Tendencia 1h (Macro)", t_1h)
            
            vol_15m = "ANÓMALO ⚠️" if df_15m.iloc[-1]['volume'] > (df_15m.iloc[-1]['vol_ma'] * 2.0) else "NORMAL 📊"
            st.metric("Setup 15m (Volumen)", vol_15m)
            
            vwap_5m = "SOBRE VWAP 🟢" if df_5m.iloc[-1]['close'] > df_5m.iloc[-1]['vwap'] else "BAJO VWAP 🔴"
            st.metric("Gatillo 5m (VWAP)", vwap_5m)

render_trading_panel(symbol, timeframe)
