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

# ==========================================
# ⚙️ CONFIGURACIÓN GLOBAL
# ==========================================
TELEGRAM_TOKEN = "9148434925:AAH_AuswS-GGPHadFBwcrDOUUIwBFbXvtDs"
TELEGRAM_CHAT_ID = "1496321437"

st.set_page_config(page_title="Institutional Quant Terminal", layout="wide", page_icon="🏦")

# --- DEFINICIÓN DE MERCADOS ---
CRYPTO_MARKETS = ["BTC/USDT", "ETH/USDT", "SOL/USDT", "BNB/USDT", "XRP/USDT", "ADA/USDT", "AVAX/USDT", "DOT/USDT", "LINK/USDT", "MATIC/USDT"]
NASDAQ_MARKETS = ["NVDA", "AAPL", "MSFT", "TSLA", "AMZN", "GOOGL", "META"]
DOLLAR_MARKETS = ["DX-Y.NYB", "EURUSD=X", "GBPUSD=X", "USDJPY=X", "USDMXN=X", "USDBRL=X"]

ALL_SYMBOLS = CRYPTO_MARKETS + NASDAQ_MARKETS + DOLLAR_MARKETS

# ==========================================
# 🧠 FUNCIONES DE APOYO Y HORARIOS
# ==========================================
def enviar_telegram(mensaje):
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    payload = {"chat_id": TELEGRAM_CHAT_ID, "text": mensaje, "parse_mode": "Markdown"}
    try: requests.post(url, json=payload)
    except: pass

def calcular_vwap(df):
    if df['volume'].sum() == 0:
        return df['close'] # Fallback para Forex
    return (df['volume'] * (df['high'] + df['low'] + df['close']) / 3).cumsum() / df['volume'].cumsum()

def mercado_abierto(tipo_mercado):
    """Verifica si el mercado está abierto basado en la hora de Nueva York"""
    ny_tz = pytz.timezone('America/New_York')
    ahora = datetime.now(ny_tz)
    dia = ahora.weekday() # 0=Lunes, 6=Domingo
    hora = ahora.time()

    if tipo_mercado == "CRIPTO":
        return True # 24/7
    
    if tipo_mercado == "ACCION":
        if dia >= 5: return False # Fin de semana
        # Horario de Wall Street: 9:30 AM a 4:00 PM
        return dt_time(9, 30) <= hora <= dt_time(16, 0)
        
    if tipo_mercado == "FOREX":
        # Forex cierra viernes 5 PM y abre domingo 5 PM
        if dia == 4 and hora > dt_time(17, 0): return False
        if dia == 5: return False
        if dia == 6 and hora < dt_time(17, 0): return False
        return True
    return False

def obtener_datos(symbol, timeframe, limit=100):
    df = pd.DataFrame()
    if "/" in symbol and "USDT" in symbol: # CRIPTO
        exchange = ccxt.okx()
        try:
            ohlcv = exchange.fetch_ohlcv(symbol, timeframe, limit=limit)
            df = pd.DataFrame(ohlcv, columns=['timestamp', 'open', 'high', 'low', 'close', 'volume'])
            df['timestamp'] = pd.to_datetime(df['timestamp'], unit='ms')
        except: return pd.DataFrame()
    else: # ACCIONES Y FOREX
        interval_map = {"1m": "1m", "5m": "5m", "15m": "15m", "1h": "60m"} 
        yf_interval = interval_map.get(timeframe, "5m")
        period = "1d" if yf_interval in ["1m", "5m", "15m"] else "1mo"
        try:
            ticker = yf.Ticker(symbol)
            hist = ticker.history(period=period, interval=yf_interval)
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
    return df

# ==========================================
# 🤖 AUTOPILOTO INTELIGENTE (3 Módulos)
# ==========================================
def escanear_mercados():
    # --- MÓDULO 1: CRIPTOMONEDAS (24/7, OBI, Volumen Clímax) ---
    for symbol in CRYPTO_MARKETS:
        try:
            df_5m = obtener_datos(symbol, '5m', limit=50)
            df_1h = obtener_datos(symbol, '1h', limit=50)
            if df_5m.empty or df_1h.empty: continue
            
            # OBI
            exchange = ccxt.okx()
            ob = exchange.fetch_order_book(symbol, limit=20)
            qb = sum(a for _, a in ob["bids"][:20]); qa = sum(a for _, a in ob["asks"][:20])
            obi = (qb - qa) / (qb + qa) if (qb + qa) > 0 else 0.0
            
            ultima = df_5m.iloc[-1]
            precio = ultima['close']
            macro = "ALCISTA" if df_1h['close'].iloc[-1] > df_1h['close'].ewm(span=50).mean().iloc[-1] else "BAJISTA"
            
            # Lógica Cripto
            if (ultima['low'] < ultima['soporte'] and ultima['close'] > ultima['soporte']) and macro == "ALCISTA" and obi > -0.4:
                enviar_telegram(f"🚀 *SETUP A+ COMPRA (CRIPTO)*\n🪙 {symbol} | ${precio:,.2f}\nOBI: {obi:+.2f}")
            elif (ultima['high'] > ultima['resistencia'] and ultima['close'] < ultima['resistencia']) and macro == "BAJISTA":
                enviar_telegram(f"🔻 *SETUP A+ VENTA (CRIPTO)*\n🪙 {symbol} | ${precio:,.2f}")
            elif ultima['volume'] > (ultima['vol_ma'] * 2.5):
                enviar_telegram(f"⚠️ *VOLUMEN CLÍMAX (CRIPTO)*\n🪙 {symbol} | ${precio:,.2f}")
        except: pass

    # --- MÓDULO 2: ACCIONES NASDAQ (Horario de NY, Volumen Real, Sin OBI) ---
    if mercado_abierto("ACCION"):
        for symbol in NASDAQ_MARKETS:
            try:
                df_5m = obtener_datos(symbol, '5m', limit=50)
                df_1h = obtener_datos(symbol, '1h', limit=50)
                if df_5m.empty or df_1h.empty: continue
                
                ultima = df_5m.iloc[-1]
                precio = ultima['close']
                macro = "ALCISTA" if df_1h['close'].iloc[-1] > df_1h['close'].ewm(span=50).mean().iloc[-1] else "BAJISTA"
                
                if (ultima['low'] < ultima['soporte'] and ultima['close'] > ultima['soporte']) and macro == "ALCISTA":
                    enviar_telegram(f"🚀 *SETUP A+ COMPRA (NASDAQ)*\n🪙 {symbol} | ${precio:,.2f}")
                elif (ultima['high'] > ultima['resistencia'] and ultima['close'] < ultima['resistencia']) and macro == "BAJISTA":
                    enviar_telegram(f"🔻 *SETUP A+ VENTA (NASDAQ)*\n🪙 {symbol} | ${precio:,.2f}")
                elif ultima['volume'] > (ultima['vol_ma'] * 2.5):
                    enviar_telegram(f"⚠️ *VOLUMEN CLÍMAX (NASDAQ)*\n🪙 {symbol} | ${precio:,.2f}")
            except: pass

    # --- MÓDULO 3: FOREX (Horario Global, Sin Volumen, Price Action Puro) ---
    if mercado_abierto("FOREX"):
        for symbol in DOLLAR_MARKETS:
            try:
                df_5m = obtener_datos(symbol, '5m', limit=50)
                df_1h = obtener_datos(symbol, '1h', limit=50)
                if df_5m.empty or df_1h.empty: continue
                
                ultima = df_5m.iloc[-1]
                precio = ultima['close']
                macro = "ALCISTA" if df_1h['close'].iloc[-1] > df_1h['close'].ewm(span=50).mean().iloc[-1] else "BAJISTA"
                
                # EN FOREX NO HAY VOLUMEN CLÍMAX (Se ignora el volumen por completo)
                if (ultima['low'] < ultima['soporte'] and ultima['close'] > ultima['soporte']) and macro == "ALCISTA":
                    enviar_telegram(f"🚀 *SETUP A+ COMPRA (FOREX)*\n🪙 {symbol} | ${precio:,.2f}")
                elif (ultima['high'] > ultima['resistencia'] and ultima['close'] < ultima['resistencia']) and macro == "BAJISTA":
                    enviar_telegram(f"🔻 *SETUP A+ VENTA (FOREX)*\n🪙 {symbol} | ${precio:,.2f}")
            except: pass

@st.cache_resource
def iniciar_autopiloto():
    def loop():
        while True:
            escanear_mercados()
            time.sleep(300) # Escanea cada 5 minutos
    thread = threading.Thread(target=loop, daemon=True)
    thread.start()
    return thread

iniciar_autopiloto()

# ==========================================
# 🦅 ESCÁNER TOP 50 (Manual - Solo Cripto)
# ==========================================
def ejecutar_escaner_top50(tf):
    exchange = ccxt.okx()
    try: tickers = exchange.fetch_tickers()
    except: return pd.DataFrame()

    mercados_usdt = []
    for sym, data in tickers.items():
        if sym.endswith('/USDT') and data.get('quoteVolume') is not None:
            mercados_usdt.append((sym, data['quoteVolume'], data.get('percentage', 0)))
            
    top50 = sorted(mercados_usdt, key=lambda x: x[1], reverse=True)[:50]
    resultados = []
    progress_bar = st.progress(0); status_text = st.empty()
    
    for i, (sym, vol, cambio) in enumerate(top50):
        status_text.text(f"Escaneando {sym} ({i+1}/50)...")
        try:
            ohlcv = exchange.fetch_ohlcv(sym, tf, limit=30)
            df = pd.DataFrame(ohlcv, columns=['timestamp', 'open', 'high', 'low', 'close', 'volume'])
            df['vwap'] = calcular_vwap(df)
            df['soporte'] = df['low'].rolling(window=20).min()
            df['resistencia'] = df['high'].rolling(window=20).max()
            df['vol_ma'] = df['volume'].rolling(window=20).mean()
            
            ultima = df.iloc[-1]
            precio = ultima['close']
            sesgo = "ALCISTA" if precio > ultima['vwap'] else "BAJISTA"
            señal = "NEUTRAL"
            
            if ultima['low'] < ultima['soporte'] and ultima['close'] > ultima['soporte']: señal = "🟢 SPRING"
            elif ultima['high'] > ultima['resistencia'] and ultima['close'] < ultima['resistencia']: señal = "🔴 UPTHRUST"
            elif ultima['volume'] > (ultima['vol_ma'] * 2.5): señal = "⚠️ VOL CLÍMAX"
                
            resultados.append({"Par": sym, "Precio": precio, "Sesgo": sesgo, "Señal": señal, "Cambio 24h %": cambio})
            time.sleep(0.05)
        except: pass
        progress_bar.progress((i + 1) / 50)
    
    status_text.empty(); progress_bar.empty()
    df_final = pd.DataFrame(resultados)
    if not df_final.empty:
        df_final['Orden'] = df_final['Señal'].apply(lambda x: 1 if x != "NEUTRAL" else 2)
        df_final = df_final.sort_values(by=['Orden', 'Cambio 24h %'], ascending=[True, False]).drop(columns=['Orden'])
    return df_final

# ==========================================
# 📊 INTERFAZ GRÁFICA
# ==========================================
st.markdown("<h1 style='text-align: center; color: #00FFAA;'>🏦 INSTITUTIONAL QUANT TERMINAL</h1>", unsafe_allow_html=True)
st.markdown("<p style='text-align: center; color: gray;'>Multi-Asset: Crypto + NASDAQ + Forex</p>", unsafe_allow_html=True)
st.markdown("---")

st.sidebar.header("🌐 Configuración del Gráfico")
symbol = st.sidebar.selectbox("Ver Gráfico de:", ALL_SYMBOLS)
timeframe = st.sidebar.selectbox("Temporalidad", ["5m","15m","1h"])

st.sidebar.markdown("---")
st.sidebar.header("🔔 Alertas")
if st.sidebar.button("🚀 Probar Alerta Telegram"):
    enviar_telegram("✅ *SISTEMA EN LÍNEA:* Multi-Asset Terminal conectado.")
    st.sidebar.success("¡Mensaje enviado!")

# 🔍 BOTÓN DEL ESCÁNER TOP 50
st.sidebar.markdown("---")
st.sidebar.header("🦅 Escáner Global (Top 50 Cripto)")
if st.sidebar.button("🔍 Escanear Top 50 Mercados"):
    with st.spinner("Analizando los 50 mercados con más volumen..."):
        st.session_state['scan_results'] = ejecutar_escaner_top50(timeframe)

if 'scan_results' in st.session_state and not st.session_state['scan_results'].empty:
    st.success("✅ Resultados del Escáner Top 50 (Ordenados por oportunidad):")
    st.dataframe(st.session_state['scan_results'], use_container_width=True)
    st.markdown("---")

# --- GRÁFICO EN VIVO ---
@st.fragment(run_every="10s")
def render_trading_panel(symbol, timeframe):
    df = obtener_datos(symbol, timeframe, limit=150)
    if df.empty:
        st.error(f"No se pudieron cargar los datos para {symbol}.")
        return

    col_grafico, col_paneles = st.columns([2.5, 1])
    with col_grafico:
        fig = make_subplots(rows=2, cols=1, shared_xaxes=True, vertical_spacing=0.03, row_width=[0.2,0.8])
        fig.add_trace(go.Candlestick(x=df['timestamp'], open=df['open'], high=df['high'], low=df['low'], close=df['close'], increasing_line_color='#00FFAA', decreasing_line_color='#FF3333'), row=1, col=1)
        fig.add_trace(go.Scatter(x=df['timestamp'], y=df['vwap'], line=dict(color='#FF00FF', width=2), name='VWAP'), row=1, col=1)
        fig.add_trace(go.Scatter(x=df['timestamp'], y=df['soporte'], line=dict(color='#00FF00', width=1, dash='dot'), name='Soporte'), row=1, col=1)
        fig.add_trace(go.Scatter(x=df['timestamp'], y=df['resistencia'], line=dict(color='#FF0000', width=1, dash='dot'), name='Resistencia'), row=1, col=1)
        colors = ['#00FFAA' if r['close']>=r['open'] else '#FF3333' for _,r in df.iterrows()]
        fig.add_trace(go.Bar(x=df['timestamp'], y=df['volume'], marker_color=colors, name='Volumen'), row=2, col=1)
        fig.update_layout(template='plotly_dark', xaxis_rangeslider_visible=False, height=600, margin=dict(l=10,r=10,t=10,b=10), showlegend=False)
        st.plotly_chart(fig, use_container_width=True)

    with col_paneles:
        st.markdown("### ⚙️ Señales de Flujo")
        ultima = df.iloc[-1]
        anterior = df.iloc[-2]
        precio_actual = ultima['close']
        st.metric(label=f"{symbol}", value=f"${precio_actual:,.2f}", delta=f"{precio_actual - anterior['close']:,.2f}")
        
        if precio_actual < ultima['vwap']: st.error("🔴 SESGO: BAJISTA")
        else: st.success("🟢 SESGO: ALCISTA")
            
        if "USDT" in symbol and ultima['volume'] > (ultima['vol_ma']*2.5): 
            st.warning("🚨 Volumen Clímax.")
        elif symbol in NASDAQ_MARKETS and ultima['volume'] > (ultima['vol_ma']*2.5):
            st.warning("🚨 Volumen Clímax.")

        if ultima['low'] < ultima['soporte'] and ultima['close'] > ultima['soporte']: st.success("🎯 SPRING (Trampa de Compra)")
        elif ultima['high'] > ultima['resistencia'] and ultima['close'] < ultima['resistencia']: st.error("🎯 UPTHRUST (Trampa de Venta)")

render_trading_panel(symbol, timeframe)

st.markdown("---")
st.markdown("### 📰 Contexto Global")
@st.cache_data(ttl=600)
def obtener_noticias():
    noticias = []
    try:
        feed = feedparser.parse("https://www.coindesk.com/arc/outboundfeeds/rss/")
        for entry in feed.entries[:4]: noticias.append({"titulo": entry.title, "fuente": "CoinDesk"})
        return noticias
    except: return []

noticias = obtener_noticias()
if noticias:
    cols = st.columns(2)
    for i, n in enumerate(noticias):
        with cols[i % 2]:
            st.markdown(f"<span style='color:gray; font-size: 12px;'>● {n['fuente']}</span><br><span style='font-size: 13px;'>{n['titulo']}</span>", unsafe_allow_html=True)
            st.markdown("---")
