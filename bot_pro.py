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
# ⚙️ CONFIGURACIÓN GLOBAL & VANTAGE MARKETS
# ==========================================
TELEGRAM_TOKEN = "9148434925:AAH_AuswS-GGPHadFBwcrDOUUIwBFbXvtDs"
TELEGRAM_CHAT_ID = "1496321437"

st.set_page_config(page_title="Institutional Quant Terminal", layout="wide", page_icon="🏦")

GLOBAL_STOCKS = ["NVDA", "AAPL", "MSFT", "TSLA", "AMZN", "GOOGL", "META", "NFLX", "AMD"]
FOREX_MARKETS = ["EURUSD=X", "GBPUSD=X", "USDJPY=X", "AUDUSD=X", "USDCAD=X", "NZDUSD=X", "USDCHF=X", "EURGBP=X", "EURJPY=X", "GBPJPY=X"]
INDEX_MARKETS = ["^GSPC", "^DJI", "^IXIC", "^GDAXI", "^FTSE", "^N225", "DX-Y.NYB"]
COMMODITY_MARKETS = ["GC=F", "SI=F", "CL=F", "BZ=F", "NG=F"]

thread_lock = threading.Lock()
ALERTAS_ENVIADAS_GLOBAL = {}
AUTOPILOTO_STATUS = "Iniciando motor de escaneo institucional..."

def enviar_telegram(mensaje):
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    payload = {"chat_id": TELEGRAM_CHAT_ID, "text": mensaje, "parse_mode": "Markdown"}
    try: 
        requests.post(url, json=payload, timeout=5)
    except Exception: 
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

def obtener_datos(symbol, timeframe, limit=100):
    df = pd.DataFrame()
    if "/" in symbol and "USDT" in symbol:
        exchange = ccxt.okx({'enableRateLimit': True, 'timeout': 10000})
        try:
            ohlcv = exchange.fetch_ohlcv(symbol, timeframe, limit=limit)
            df = pd.DataFrame(ohlcv, columns=['timestamp', 'open', 'high', 'low', 'close', 'volume'])
            df['timestamp'] = pd.to_datetime(df['timestamp'], unit='ms')
        except Exception: 
            return pd.DataFrame()
    else:
        interval_map = {"1m": "1m", "5m": "5m", "15m": "15m", "1h": "60m"} 
        yf_interval = interval_map.get(timeframe, "5m")
        period = "1d" if yf_interval in ["1m", "5m", "15m"] else "1mo"
        try:
            ticker = yf.Ticker(symbol)
            hist = ticker.history(period=period, interval=yf_interval, timeout=10)
            if hist.empty: return pd.DataFrame()
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

def clasificar_tipo_mercado(symbol):
    if "/" in symbol and "USDT" in symbol: return "CRIPTO"
    if symbol in GLOBAL_STOCKS: return "ACCION"
    if symbol in FOREX_MARKETS: return "FOREX"
    if symbol in INDEX_MARKETS: return "INDICE"
    if symbol in COMMODITY_MARKETS: return "COMMODITY"
    return "CRIPTO"

# ==========================================
# 🤖 LÓGICA DEL AUTOPILOTO
# ==========================================
def ejecutar_logica_autopiloto(es_hilo_fondo=False):
    global AUTOPILOTO_STATUS, ALERTAS_ENVIADAS_GLOBAL
    
    if not es_hilo_fondo:
        AUTOPILOTO_STATUS = "Buscando mercados principales..."
    
    exchange = ccxt.okx({'enableRateLimit': True, 'timeout': 10000})
    try:
        tickers = exchange.fetch_tickers()
        mercados_usdt = [(sym, data['quoteVolume']) for sym, data in tickers.items() if sym.endswith('/USDT') and data.get('quoteVolume')]
        top50_cripto = [sym for sym, vol in sorted(mercados_usdt, key=lambda x: x[1], reverse=True)[:50]]
    except Exception:
        top50_cripto = ["BTC/USDT", "ETH/USDT", "SOL/USDT"]

    mercados_a_escanear = top50_cripto + GLOBAL_STOCKS + FOREX_MARKETS + INDEX_MARKETS + COMMODITY_MARKETS
    alertas_nuevas = 0

    for i, symbol in enumerate(mercados_a_escanear):
        if not es_hilo_fondo:
            AUTOPILOTO_STATUS = f"Analizando {symbol} ({i+1}/{len(mercados_a_escanear)})..."
            
        try:
            tipo = clasificar_tipo_mercado(symbol)
            if not mercado_abierto(tipo): continue

            df_5m = obtener_datos(symbol, '5m', limit=50)
            df_1h = obtener_datos(symbol, '1h', limit=50)
            if df_5m.empty or df_1h.empty: continue
            
            ultima = df_5m.iloc[-1]
            precio = ultima['close']
            macro = "ALCISTA" if df_1h['close'].iloc[-1] > df_1h['close'].ewm(span=50).mean().iloc[-1] else "BAJISTA"
            
            señal = None
            msg = ""
            
            cuerpo_vela = abs(ultima['close'] - ultima['open'])
            mecha_inferior = min(ultima['close'], ultima['open']) - ultima['low']
            mecha_superior = ultima['high'] - max(ultima['close'], ultima['open'])
            multiplicador_volumen = 2.0 if tipo in ["FOREX", "INDICE"] else 2.5
            
            if (ultima['low'] < ultima['soporte'] and ultima['close'] > ultima['soporte']) and macro == "ALCISTA":
                señal = "SPRING"
                msg = f"🚀 *AUTOPILOTO: SPRING COMPRA ({tipo})*\n🪙 {symbol} | ${precio:,.2f}"
            elif (ultima['high'] > ultima['resistencia'] and ultima['close'] < ultima['resistencia']) and macro == "BAJISTA":
                señal = "UPTHRUST"
                msg = f"🔻 *AUTOPILOTO: UPTHRUST VENTA ({tipo})*\n🪙 {symbol} | ${precio:,.2f}"
            elif ultima['volume'] > (ultima['vol_ma'] * multiplicador_volumen):
                cerca_soporte = ultima['low'] <= (ultima['soporte'] * 1.015)
                cerca_resistencia = ultima['high'] >= (ultima['resistencia'] * 0.985)
                
                if cerca_soporte and (mecha_inferior > cuerpo_vela * 1.1 or ultima['close'] > ultima['open']):
                    señal = "CLIMAX_ABSORCION"
                    msg = f"⚠️ *AUTOPILOTO: CLÍMAX ABSORCIÓN ({tipo})*\n🪙 {symbol} | ${precio:,.2f}"
                elif cerca_resistencia and (mecha_superior > cuerpo_vela * 1.1 or ultima['close'] < ultima['open']):
                    señal = "CLIMAX_EXHAUSCION"
                    msg = f"⚠️ *AUTOPILOTO: CLÍMAX EXHAUSCIÓN ({tipo})*\n🪙 {symbol} | ${precio:,.2f}"

            if señal and msg:
                clave_alerta = f"{symbol}_{señal}"
                ahora = time.time()
                with thread_lock:
                    if ahora - ALERTAS_ENVIADAS_GLOBAL.get(clave_alerta, 0) > 1800:
                        enviar_telegram(msg)
                        ALERTAS_ENVIADAS_GLOBAL[clave_alerta] = ahora
                        alertas_nuevas += 1

            time.sleep(0.1)
        except Exception: 
            pass

    with thread_lock:
        if not es_hilo_fondo:
            AUTOPILOTO_STATUS = f"✅ Completado. {alertas_nuevas} alertas enviadas."
        else:
            AUTOPILOTO_STATUS = "✅ Escaneo de fondo completado. Durmiendo 5 min..."
    return alertas_nuevas

# ==========================================
# 🧵 HILO DE FONDO SEGURO
# ==========================================
@st.cache_resource
def iniciar_hilo_autopiloto():
    def loop():
        while True:
            try: ejecutar_logica_autopiloto(es_hilo_fondo=True)
            except Exception: pass
            time.sleep(300)
    thread = threading.Thread(target=loop, daemon=True)
    thread.start()
    return thread

iniciar_hilo_autopiloto()

# ==========================================
# 🦅 ESCÁNER MANUAL ROBUSTO
# ==========================================
def ejecutar_escaner_general(tf):
    exchange = ccxt.okx({'enableRateLimit': True, 'timeout': 10000})
    try: 
        tickers = exchange.fetch_tickers()
    except Exception: 
        tickers = {}

    mercados_usdt = [(sym, data['quoteVolume'], data.get('percentage', 0)) for sym, data in tickers.items() if sym.endswith('/USDT') and data.get('quoteVolume')]
    top50_cripto = sorted(mercados_usdt, key=lambda x: x[1], reverse=True)[:50]
    
    lista_total = [item[0] for item in top50_cripto] + GLOBAL_STOCKS + FOREX_MARKETS + INDEX_MARKETS + COMMODITY_MARKETS
    resultados = []
    
    progress_bar = st.progress(0)
    status_text = st.empty()
    total_mercados = len(lista_total)
    
    for i, sym in enumerate(lista_total):
        status_text.text(f"Analizando [{i+1}/{total_mercados}]: {sym}...")
        try:
            tipo = clasificar_tipo_mercado(sym)
            df = obtener_datos(sym, tf, limit=30)
            if df.empty: 
                progress_bar.progress((i + 1) / total_mercados)
                continue
            
            ultima = df.iloc[-1]
            precio = ultima['close']
            sesgo = "ALCISTA" if precio > ultima['vwap'] else "BAJISTA"
            señal = "NEUTRAL"
            
            mecha_inf = min(ultima['close'], ultima['open']) - ultima['low']
            mecha_sup = ultima['high'] - max(ultima['close'], ultima['open'])
            cuerpo = abs(ultima['close'] - ultima['open'])
            cerca_soporte = ultima['low'] <= (ultima['soporte'] * 1.015)
            cerca_resistencia = ultima['high'] >= (ultima['resistencia'] * 0.985)
            cambio_24h = 0.0
            
            if "/" in sym:
                match = [item for item in top50_cripto if item[0] == sym]
                if match: cambio_24h = match[0][2]

            if ultima['low'] < ultima['soporte'] and ultima['close'] > ultima['soporte']:
                señal = "🟢 SPRING"
            elif ultima['high'] > ultima['resistencia'] and ultima['close'] < ultima['resistencia']:
                señal = "🔴 UPTHRUST"
            elif ultima['volume'] > (ultima['vol_ma'] * 2.0) and cerca_soporte and (mecha_inf > cuerpo * 1.1 or ultima['close'] > ultima['open']):
                señal = "⚠️ CLÍMAX ABSORCIÓN"
            elif ultima['volume'] > (ultima['vol_ma'] * 2.0) and cerca_resistencia and (mecha_sup > cuerpo * 1.1 or ultima['close'] < ultima['open']):
                señal = "⚠️ CLÍMAX EXHAUSCIÓN"
                
            resultados.append({"Activo": sym, "Tipo": tipo, "Precio": precio, "Sesgo": sesgo, "Señal": señal, "Cambio %": cambio_24h})
        except Exception: 
            pass
        
        progress_bar.progress((i + 1) / total_mercados)
        time.sleep(0.02)
    
    status_text.empty()
    progress_bar.empty()
    
    df_final = pd.DataFrame(resultados)
    if not df_final.empty:
        df_final['Orden'] = df_final['Señal'].apply(lambda x: 1 if x != "NEUTRAL" else 2)
        df_final = df_final.sort_values(by=['Orden', 'Cambio %'], ascending=[True, False]).drop(columns=['Orden'])
    return df_final

# ==========================================
# 📊 INTERFAZ GRÁFICA VANTAGE QUANT TERMINAL
# ==========================================
st.markdown("<h1 style='text-align: center; color: #00FFAA;'>🏦 INSTITUTIONAL QUANT TERMINAL (VANTAGE)</h1>", unsafe_allow_html=True)
st.markdown("<p style='text-align: center; color: gray;'>Multi-Asset Engine: Cripto + Acciones NASDAQ + Forex + Índices + Commodities</p>", unsafe_allow_html=True)
st.markdown("---")

# --- BARRA LATERAL ---
st.sidebar.header("🌐 Configuración del Gráfico")
lista_grafico = [
    "BTC/USDT", "ETH/USDT", "SOL/USDT", "BNB/USDT", "XRP/USDT"
] + GLOBAL_STOCKS + FOREX_MARKETS + INDEX_MARKETS + COMMODITY_MARKETS

symbol = st.sidebar.selectbox("Seleccionar Activo:", lista_grafico)
timeframe = st.sidebar.selectbox("Temporalidad", ["5m","15m","1h"])

st.sidebar.markdown("---")
st.sidebar.subheader("📡 Estado del Autopiloto")
st.sidebar.info(AUTOPILOTO_STATUS)

if st.sidebar.button("⚡ Forzar Escaneo Autopiloto"):
    with st.spinner("Ejecutando motor adaptativo..."):
        alertas = ejecutar_logica_autopiloto(es_hilo_fondo=False)
        st.sidebar.success(f"Escaneo finalizado. Alertas enviadas: {alertas}")

st.sidebar.markdown("---")
st.sidebar.header("🦅 Escáner Global Masivo")
if st.sidebar.button("🔍 Escanear Todos los Mercados (Manual)"):
    with st.spinner("Analizando activos adaptados por volatilidad..."):
        st.session_state['scan_results'] = ejecutar_escaner_general(timeframe)

if 'scan_results' in st.session_state and not st.session_state['scan_results'].empty:
    st.success("✅ Resultados del Escáner Global:")
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
        tipo_actual = clasificar_tipo_mercado(symbol)
        st.markdown(f"### ⚙️ Flujo ({tipo_actual})")
        ultima = df.iloc[-1]
        anterior = df.iloc[-2]
        precio_actual = ultima['close']
        st.metric(label=f"{symbol}", value=f"{precio_actual:,.2f}", delta=f"{precio_actual - anterior['close']:,.2f}")
        
        if precio_actual < ultima['vwap']: st.error("🔴 SESGO: BAJISTA")
        else: st.success("🟢 SESGO: ALCISTA")
            
        cerca_soporte = ultima['low'] <= (ultima['soporte'] * 1.015)
        cerca_resistencia = ultima['high'] >= (ultima['resistencia'] * 0.985)
        mecha_inf = min(ultima['close'], ultima['open']) - ultima['low']
        mecha_sup = ultima['high'] - max(ultima['close'], ultima['open'])
        cuerpo = abs(ultima['close'] - ultima['open'])

        if ultima['volume'] > (ultima['vol_ma'] * 2.0):
            if cerca_soporte and (mecha_inf > cuerpo * 1.1 or ultima['close'] > ultima['open']):
                st.warning("⚠️ Clímax de Absorción (Soporte).")
            elif cerca_resistencia and (mecha_sup > cuerpo * 1.1 or ultima['close'] < ultima['open']):
                st.warning("⚠️ Clímax de Exhausción (Resistencia).")

        if ultima['low'] < ultima['soporte'] and ultima['close'] > ultima['soporte']: 
            st.success("🎯 SPRING (Trampa Compra)")
        elif ultima['high'] > ultima['resistencia'] and ultima['close'] < ultima['resistencia']: 
            st.error("🎯 UPTHRUST (Trampa Venta)")

render_trading_panel(symbol, timeframe)

st.markdown("---")
st.markdown("### 📰 Contexto Global")
@st.cache_data(ttl=600)
def obtener_noticias():
    try:
        feed = feedparser.parse("https://www.coindesk.com/arc/outboundfeeds/rss/")
        return [{"titulo": entry.title, "fuente": "CoinDesk"} for entry in feed.entries[:4]]
    except Exception: return []

noticias = obtener_noticias()
if noticias:
    cols = st.columns(2)
    for i, n in enumerate(noticias):
        with cols[i % 2]:
            st.markdown(f"<span style='color:gray; font-size: 12px;'>● {n['fuente']}</span><br><span style='font-size: 13px;'>{n['titulo']}</span>", unsafe_allow_html=True)
            st.markdown("---")
