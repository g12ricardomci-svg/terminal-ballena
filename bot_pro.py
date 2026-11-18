import streamlit as st
import ccxt
import yfinance as yf
import pandas as pd
import numpy as np
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

NASDAQ_MARKETS = ["NVDA", "AAPL", "MSFT", "TSLA", "AMZN", "GOOGL", "META"]
DOLLAR_MARKETS = ["DX-Y.NYB", "EURUSD=X", "GBPUSD=X", "USDJPY=X", "USDMXN=X", "USDBRL=X"]
ALL_SYMBOLS = NASDAQ_MARKETS + DOLLAR_MARKETS

ALERTAS_ENVIADAS_GLOBAL = {}
AUTOPILOTO_STATUS = "Iniciando motor de escaneo..."

# ==========================================
# 🧠 FUNCIONES MATEMÁTICAS
# ==========================================
def enviar_telegram(mensaje):
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    payload = {"chat_id": TELEGRAM_CHAT_ID, "text": mensaje, "parse_mode": "Markdown"}
    try: 
        requests.post(url, json=payload, timeout=5)
    except: pass

def calcular_vwap(df):
    if df['volume'].sum() == 0: return df['close']
    return (df['volume'] * (df['high'] + df['low'] + df['close']) / 3).cumsum() / df['volume'].cumsum()

def calcular_adx(df, period=14):
    df['up_move'] = df['high'] - df['high'].shift()
    df['down_move'] = df['low'].shift() - df['low']
    df['plus_dm'] = np.where((df['up_move'] > df['down_move']) & (df['up_move'] > 0), df['up_move'], 0.0)
    df['minus_dm'] = np.where((df['down_move'] > df['up_move']) & (df['down_move'] > 0), df['down_move'], 0.0)
    df['tr'] = np.maximum(df['high'] - df['low'], np.maximum(abs(df['high'] - df['close'].shift()), abs(df['low'] - df['close'].shift())))
    df['atr'] = df['tr'].ewm(span=period, adjust=False).mean()
    df['plus_di'] = 100 * (df['plus_dm'].ewm(span=period, adjust=False).mean() / df['atr'])
    df['minus_di'] = 100 * (df['minus_dm'].ewm(span=period, adjust=False).mean() / df['atr'])
    df['dx'] = 100 * (abs(df['plus_di'] - df['minus_di']) / (df['plus_di'] + df['minus_di']))
    df['adx'] = df['dx'].ewm(span=period, adjust=False).mean()
    return df['adx'].iloc[-1]

def mercado_abierto(tipo_mercado):
    ny_tz = pytz.timezone('America/New_York')
    ahora = datetime.now(ny_tz)
    dia = ahora.weekday()
    hora = ahora.time()
    if tipo_mercado == "CRIPTO": return True
    if tipo_mercado == "ACCION":
        if dia >= 5: return False
        return dt_time(9, 30) <= hora <= dt_time(16, 0)
    if tipo_mercado == "FOREX":
        if dia == 4 and hora > dt_time(17, 0): return False
        if dia == 5: return False
        if dia == 6 and hora < dt_time(17, 0): return False
        return True
    return False

# 🛠️ FUNCIÓN OBTENER_DATOS CORREGIDA Y ROBUSTA PARA YFINANCE
def obtener_datos(symbol, timeframe, limit=100):
    df = pd.DataFrame()
    if "/" in symbol and "USDT" in symbol:
        exchange = ccxt.okx()
        try:
            ohlcv = exchange.fetch_ohlcv(symbol, timeframe, limit=limit)
            df = pd.DataFrame(ohlcv, columns=['timestamp', 'open', 'high', 'low', 'close', 'volume'])
            df['timestamp'] = pd.to_datetime(df['timestamp'], unit='ms')
        except: return pd.DataFrame()
    else:
        # Mapeo seguro para Yahoo Finance
        interval_map = {"1m": "1m", "5m": "5m", "15m": "15m", "1h": "60m"} 
        yf_interval = interval_map.get(timeframe, "5m")
        
        # Para Forex/Acciones en yfinance, usar periodos seguros de 5 días para intradía
        period = "5d" if yf_interval in ["1m", "5m", "15m"] else "1mo"
        try:
            ticker = yf.Ticker(symbol)
            hist = ticker.history(period=period, interval=yf_interval)
            if hist.empty:
                # Intento de respaldo con periodo diario si falla el intradía
                hist = ticker.history(period="1mo", interval="1d")
            
            if hist.empty: return pd.DataFrame()
            
            df = hist.reset_index()
            # yfinance puede devolver 'Datetime' o 'Date' como columna de tiempo
            time_col = 'Datetime' if 'Datetime' in df.columns else ('Date' if 'Date' in df.columns else df.columns[0])
            df.rename(columns={time_col: 'timestamp'}, inplace=True)
            
            df['timestamp'] = pd.to_datetime(df['timestamp']).dt.tz_localize(None)
            df['volume'] = df['volume'].fillna(0)
            df = df.tail(limit)
        except Exception as e:
            return pd.DataFrame()

    if not df.empty:
        df['vwap'] = calcular_vwap(df)
        df['soporte'] = df['low'].rolling(window=20).min()
        df['resistencia'] = df['high'].rolling(window=20).max()
        df['vol_ma'] = df['volume'].rolling(window=20).mean()
        df['ema_fast'] = df['close'].ewm(span=9, adjust=False).mean()
        df['ema_slow'] = df['close'].ewm(span=21, adjust=False).mean()
        df['adx'] = calcular_adx(df)
    return df

# ==========================================
# 🤖 LÓGICA DEL AUTOPILOTO
# ==========================================
def ejecutar_logica_autopiloto(es_hilo_fondo=False):
    global AUTOPILOTO_STATUS, ALERTAS_ENVIADAS_GLOBAL
    
    if not es_hilo_fondo: AUTOPILOTO_STATUS = "Buscando mercados..."
    
    exchange = ccxt.okx()
    try:
        tickers = exchange.fetch_tickers()
        mercados_usdt = []
        for sym, data in tickers.items():
            if sym.endswith('/USDT') and data.get('quoteVolume'):
                precio = data.get('last', 1)
                if not (0.99 <= precio <= 1.01): 
                    mercados_usdt.append((sym, data['quoteVolume']))
        top50_cripto = [sym for sym, vol in sorted(mercados_usdt, key=lambda x: x[1], reverse=True)[:50]]
    except:
        top50_cripto = ["BTC/USDT", "ETH/USDT", "SOL/USDT"]

    mercados_a_escanear = top50_cripto + NASDAQ_MARKETS + DOLLAR_MARKETS
    alertas_nuevas = 0

    for i, symbol in enumerate(mercados_a_escanear):
        if not es_hilo_fondo: AUTOPILOTO_STATUS = f"Analizando {symbol} ({i+1}/{len(mercados_a_escanear)})..."
            
        try:
            tipo = "CRIPTO" if "/" in symbol else ("ACCION" if symbol in NASDAQ_MARKETS else "FOREX")
            if not mercado_abierto(tipo): continue

            df_5m = obtener_datos(symbol, '5m', limit=50)
            df_1h = obtener_datos(symbol, '1h', limit=50)
            if df_5m.empty or df_1h.empty: continue
            
            ultima = df_5m.iloc[-1]
            precio = ultima['close']
            macro = "ALCISTA" if df_1h['close'].iloc[-1] > df_1h['close'].ewm(span=50).mean().iloc[-1] else "BAJISTA"
            
            señal = None
            msg = ""
            
            cruce_alcista_reciente = False
            cruce_bajista_reciente = False
            
            for j in range(1, 4):
                if (df_5m['ema_fast'].iloc[-j] > df_5m['ema_slow'].iloc[-j]) and (df_5m['ema_fast'].iloc[-j-1] <= df_5m['ema_slow'].iloc[-j-1]):
                    cruce_alcista_reciente = True
                    break
                if (df_5m['ema_fast'].iloc[-j] < df_5m['ema_slow'].iloc[-j]) and (df_5m['ema_fast'].iloc[-j-1] >= df_5m['ema_slow'].iloc[-j-1]):
                    cruce_bajista_reciente = True
                    break

            if (ultima['low'] < ultima['soporte'] and ultima['close'] > ultima['soporte']) and macro == "ALCISTA":
                señal = "SPRING"
                msg = f"🚀 *AUTOPILOTO WYCKOFF: COMPRA ({tipo})*\n🪙 {symbol} | ${precio:,.2f}\n📊 *Dirección:* COMPRA (Largo) 🟢"
            elif (ultima['high'] > ultima['resistencia'] and ultima['close'] < ultima['resistencia']) and macro == "BAJISTA":
                señal = "UPTHRUST"
                msg = f"🔻 *AUTOPILOTO WYCKOFF: VENTA ({tipo})*\n🪙 {symbol} | ${precio:,.2f}\n📊 *Dirección:* VENTA (Corto) 🔴"
            elif cruce_alcista_reciente and ultima['adx'] > 25:
                señal = "GOLDEN_CROSS"
                sl = df_5m['low'].tail(5).min() * 0.998
                tp = precio + ((precio - sl) * 2)
                msg = f"🌟 *CRUCE DORADO RECIENTE ({tipo})*\n🪙 {symbol} | ${precio:,.2f}\n📊 *Dirección:* COMPRA (Largo) 🟢"
            elif cruce_bajista_reciente and ultima['adx'] > 25:
                señal = "DEATH_CROSS"
                sl = df_5m['high'].tail(5).max() * 1.002
                tp = precio - ((sl - precio) * 2)
                msg = f"💀 *CRUCE DE LA MUERTE RECIENTE ({tipo})*\n🪙 {symbol} | ${precio:,.2f}\n📊 *Dirección:* VENTA (Corto) 🔴"
            elif tipo != "FOREX" and ultima['volume'] > (ultima['vol_ma'] * 2.5):
                señal = "CLIMAX"
                direccion = "COMPRA (Largo) 🟢" if ultima['close'] > ultima['open'] else "VENTA (Corto) 🔴"
                msg = f"⚠️ *AUTOPILOTO CLÍMAX ({tipo})*\n🪙 {symbol} | ${precio:,.2f}\n📊 *Dirección:* {direccion}"

            if señal and msg:
                clave_alerta = f"{symbol}_{señal}"
                ahora = time.time()
                if ahora - ALERTAS_ENVIADAS_GLOBAL.get(clave_alerta, 0) > 1800:
                    enviar_telegram(msg)
                    ALERTAS_ENVIADAS_GLOBAL[clave_alerta] = ahora
                    alertas_nuevas += 1
                    time.sleep(1)

            time.sleep(0.3)
        except: pass

    if not es_hilo_fondo:
        AUTOPILOTO_STATUS = f"✅ Completado. {alertas_nuevas} alertas enviadas."
    else:
        AUTOPILOTO_STATUS = "✅ Escaneo de fondo completado."

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
# 📊 INTERFAZ GRÁFICA
# ==========================================
st.markdown("<h1 style='text-align: center; color: #00FFAA;'>🏦 INSTITUTIONAL QUANT TERMINAL</h1>", unsafe_allow_html=True)
st.markdown("<p style='text-align: center; color: gray;'>Multi-Asset + Wyckoff + Medias Móviles</p>", unsafe_allow_html=True)
st.markdown("---")

st.sidebar.header("🌐 Configuración del Gráfico")
lista_grafico = ["BTC/USDT", "ETH/USDT", "SOL/USDT"] + NASDAQ_MARKETS + DOLLAR_MARKETS
symbol = st.sidebar.selectbox("Ver Gráfico de:", lista_grafico)
timeframe = st.sidebar.selectbox("Temporalidad", ["5m","15m","1h"])

st.sidebar.markdown("---")
st.sidebar.subheader("📡 Estado del Autopiloto")
st.sidebar.info(AUTOPILOTO_STATUS)

if st.sidebar.button("⚡ Forzar Escaneo Autopiloto Ahora"):
    with st.spinner("Ejecutando lógica..."):
        alertas = ejecutar_logica_autopiloto(es_hilo_fondo=False)
        if alertas > 0: st.sidebar.success(f"¡Se enviaron {alertas} alertas!")
        else: st.sidebar.warning("Escaneo completado. Sin señales nuevas.")

@st.fragment(run_every="10s")
def render_trading_panel(symbol, timeframe):
    df = obtener_datos(symbol, timeframe, limit=150)
    if df.empty:
        st.error(f"No se pudieron cargar los datos para {symbol} (Verifica si el mercado está abierto o el símbolo en Yahoo Finance).")
        return

    col_grafico, col_paneles = st.columns([2.5, 1])
    with col_grafico:
        fig = make_subplots(rows=2, cols=1, shared_xaxes=True, vertical_spacing=0.03, row_width=[0.2,0.8])
        fig.add_trace(go.Candlestick(x=df['timestamp'], open=df['open'], high=df['high'], low=df['low'], close=df['close'], increasing_line_color='#00FFAA', decreasing_line_color='#FF3333'), row=1, col=1)
        fig.add_trace(go.Scatter(x=df['timestamp'], y=df['vwap'], line=dict(color='#FF00FF', width=2), name='VWAP'), row=1, col=1)
        fig.add_trace(go.Scatter(x=df['timestamp'], y=df['ema_fast'], line=dict(color='#FFFF00', width=1.5), name='EMA 9'), row=1, col=1)
        fig.add_trace(go.Scatter(x=df['timestamp'], y=df['ema_slow'], line=dict(color='#00BFFF', width=1.5), name='EMA 21'), row=1, col=1)
        fig.add_trace(go.Scatter(x=df['timestamp'], y=df['soporte'], line=dict(color='#00FF00', width=1, dash='dot'), name='Soporte'), row=1, col=1)
        fig.add_trace(go.Scatter(x=df['timestamp'], y=df['resistencia'], line=dict(color='#FF0000', width=1, dash='dot'), name='Resistencia'), row=1, col=1)
        colors = ['#00FFAA' if r['close']>=r['open'] else '#FF3333' for _,r in df.iterrows()]
        fig.add_trace(go.Bar(x=df['timestamp'], y=df['volume'], marker_color=colors, name='Volumen'), row=2, col=1)
        fig.update_layout(template='plotly_dark', xaxis_rangeslider_visible=False, height=600, margin=dict(l=10,r=10,t=10,b=10), showlegend=True)
        st.plotly_chart(fig, use_container_width=True)

    with col_paneles:
        st.markdown("### ⚙️ Señales de Flujo")
        ultima = df.iloc[-1]
        anterior = df.iloc[-2]
        precio_actual = ultima['close']
        st.metric(label=f"{symbol}", value=f"${precio_actual:,.4f}", delta=f"{precio_actual - anterior['close']:,.4f}")
        
        if precio_actual < ultima['vwap']: st.error("🔴 SESGO: BAJISTA")
        else: st.success("🟢 SESGO: ALCISTA")
            
        st.markdown("---")
        st.markdown("### 📊 Filtro ADX")
        if ultima['adx'] > 25: st.success(f"ADX: {ultima['adx']:.1f} (Tendencia)")
        else: st.warning(f"ADX: {ultima['adx']:.1f} (Lateral - No operar cruces)")
            
        st.markdown("---")
        st.markdown("### 🎯 Estrategias")
        if "USDT" in symbol and ultima['volume'] > (ultima['vol_ma']*2.5): st.warning("⚠️ Volumen Clímax.")
        if ultima['low'] < ultima['soporte'] and ultima['close'] > ultima['soporte']: st.success("🎯 SPRING (Wyckoff)")
        elif ultima['high'] > ultima['resistencia'] and ultima['close'] < ultima['resistencia']: st.error("🎯 UPTHRUST (Wyckoff)")
        
        if (ultima['ema_fast'] > ultima['ema_slow']) and (anterior['ema_fast'] <= anterior['ema_slow']) and ultima['adx'] > 25:
            st.success("🌟 CRUCE DORADO (EMA 9 sobre EMA 21)")
        elif (ultima['ema_fast'] < ultima['ema_slow']) and (anterior['ema_fast'] >= anterior['ema_slow']) and ultima['adx'] > 25:
            st.error("💀 CRUCE DE LA MUERTE (EMA 9 bajo EMA 21)")

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
