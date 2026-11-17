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

NASDAQ_MARKETS = ["NVDA", "AAPL", "MSFT", "TSLA", "AMZN", "GOOGL", "META"]
DOLLAR_MARKETS = ["DX-Y.NYB", "EURUSD=X", "GBPUSD=X", "USDJPY=X", "USDMXN=X", "USDBRL=X"]

# 🔥 BLOQUEO PARA HILOS SEGUROS (Evita race conditions)
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
    if tipo_mercado == "ACCION":
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
        exchange = ccxt.okx({'enableRateLimit': True})
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
            hist = ticker.history(period=period, interval=yf_interval)
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

# ==========================================
# 🤖 LÓGICA DEL AUTOPILOTO (Con Clímax Mejorado)
# ==========================================
def ejecutar_logica_autopiloto(es_hilo_fondo=False):
    global AUTOPILOTO_STATUS, ALERTAS_ENVIADAS_GLOBAL
    
    if not es_hilo_fondo:
        AUTOPILOTO_STATUS = "Buscando los 50 mercados principales..."
    
    exchange = ccxt.okx({'enableRateLimit': True})
    try:
        tickers = exchange.fetch_tickers()
        mercados_usdt = [(sym, data['quoteVolume']) for sym, data in tickers.items() if sym.endswith('/USDT') and data.get('quoteVolume')]
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
            if not mercado_abierto(tipo): continue

            df_5m = obtener_datos(symbol, '5m', limit=50)
            df_1h = obtener_datos(symbol, '1h', limit=50)
            if df_5m.empty or df_1h.empty: continue
            
            ultima = df_5m.iloc[-1]
            precio = ultima['close']
            macro = "ALCISTA" if df_1h['close'].iloc[-1] > df_1h['close'].ewm(span=50).mean().iloc[-1] else "BAJISTA"
            
            señal = None
            msg = ""
            
            # Cálculo de mechas y rango de la vela para el Clímax Institucional
            rango_vela = ultima['high'] - ultima['low']
            cuerpo_vela = abs(ultima['close'] - ultima['open'])
            mecha_inferior = min(ultima['close'], ultima['open']) - ultima['low']
            mecha_superior = ultima['high'] - max(ultima['close'], ultima['open'])
            
            # 1. SPRING (Wyckoff)
            if (ultima['low'] < ultima['soporte'] and ultima['close'] > ultima['soporte']) and macro == "ALCISTA":
                señal = "SPRING"
                msg = f"🚀 *AUTOPILOTO: SPRING COMPRA ({tipo})*\n🪙 {symbol} | ${precio:,.2f}"
            
            # 2. UPTHRUST (Wyckoff)
            elif (ultima['high'] > ultima['resistencia'] and ultima['close'] < ultima['resistencia']) and macro == "BAJISTA":
                señal = "UPTHRUST"
                msg = f"🔻 *AUTOPILOTO: UPTHRUST VENTA ({tipo})*\n🪙 {symbol} | ${precio:,.2f}"
            
            # 3. CLÍMAX DE VOLUMEN INSTITUCIONAL MEJORADO (Filtro de Ubicación + Rechazo por Mechas)
            elif tipo != "FOREX" and ultima['volume'] > (ultima['vol_ma'] * 2.5):
                # Contexto 1: Clímax Vendedor / Absorción en Soporte (Mecha inferior larga o rebote)
                cerca_soporte = ultima['low'] <= (ultima['soporte'] * 1.015)
                cerca_resistencia = ultima['high'] >= (ultima['resistencia'] * 0.985)
                
                if cerca_soporte and (mecha_inferior > cuerpo_vela * 1.2 or ultima['close'] > ultima['open']):
                    señal = "CLIMAX_ABSORCION"
                    msg = f"⚠️ *AUTOPILOTO: CLÍMAX DE ABSORCIÓN (Soporte) ({tipo})*\n🪙 {symbol} | ${precio:,.2f}\n📊 *Estrategia:* Alta acumulación institucional detectada."
                
                # Contexto 2: Clímax Comprador / Exhausción en Resistencia (Mecha superior larga o rechazo)
                elif cerca_resistencia and (mecha_superior > cuerpo_vela * 1.2 or ultima['close'] < ultima['open']):
                    señal = "CLIMAX_EXHAUSCION"
                    msg = f"⚠️ *AUTOPILOTO: CLÍMAX DE EXHAUSCIÓN (Resistencia) ({tipo})*\n🪙 {symbol} | ${precio:,.2f}\n📊 *Estrategia:* Posible techo de distribución institucional."

            if señal and msg:
                clave_alerta = f"{symbol}_{señal}"
                ahora = time.time()
                
                with thread_lock:
                    ultimo_envio = ALERTAS_ENVIADAS_GLOBAL.get(clave_alerta, 0)
                    # Anti-spam: 30 min
                    if ahora - ultimo_envio > 1800:
                        enviar_telegram(msg)
                        ALERTAS_ENVIADAS_GLOBAL[clave_alerta] = ahora
                        alertas_nuevas += 1

            time.sleep(0.2)
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
            try:
                ejecutar_logica_autopiloto(es_hilo_fondo=True)
            except Exception:
                pass
            time.sleep(300) # 5 minutos
            
    thread = threading.Thread(target=loop, daemon=True)
    thread.start()
    return thread

iniciar_hilo_autopiloto()

# ==========================================
# 🦅 ESCÁNER TOP 50 (Manual con Clímax Mejorado)
# ==========================================
def ejecutar_escaner_top50(tf):
    exchange = ccxt.okx({'enableRateLimit': True})
    try: 
        tickers = exchange.fetch_tickers()
    except Exception: 
        return pd.DataFrame()

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
            
            mecha_inferior = min(ultima['close'], ultima['open']) - ultima['low']
            mecha_superior = ultima['high'] - max(ultima['close'], ultima['open'])
            cuerpo_vela = abs(ultima['close'] - ultima['open'])
            cerca_soporte = ultima['low'] <= (ultima['soporte'] * 1.015)
            cerca_resistencia = ultima['high'] >= (ultima['resistencia'] * 0.985)
            
            if ultima['low'] < ultima['soporte'] and ultima['close'] > ultima['soporte']:
                señal = "🟢 SPRING"
                enviar_telegram(f"🦅 *SPRING (Manual)*\n🪙 {sym} | ${precio:,.2f}")
            elif ultima['high'] > ultima['resistencia'] and ultima['close'] < ultima['resistencia']:
                señal = "🔴 UPTHRUST"
                enviar_telegram(f"🦅 *UPTHRUST (Manual)*\n🪙 {sym} | ${precio:,.2f}")
            elif ultima['volume'] > (ultima['vol_ma'] * 2.5) and cerca_soporte and (mecha_inferior > cuerpo_vela * 1.2 or ultima['close'] > ultima['open']):
                señal = "⚠️ CLÍMAX ABSORCIÓN"
                enviar_telegram(f"🦅 *CLÍMAX ABSORCIÓN (Manual)*\n🪙 {sym} | ${precio:,.2f}")
            elif ultima['volume'] > (ultima['vol_ma'] * 2.5) and cerca_resistencia and (mecha_superior > cuerpo_vela * 1.2 or ultima['close'] < ultima['open']):
                señal = "⚠️ CLÍMAX EXHAUSCIÓN"
                enviar_telegram(f"🦅 *CLÍMAX EXHAUSCIÓN (Manual)*\n🪙 {sym} | ${precio:,.2f}")
                
            resultados.append({"Par": sym, "Precio": precio, "Sesgo": sesgo, "Señal": señal, "Cambio 24h %": cambio})
            time.sleep(0.05)
        except Exception: 
            pass
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
st.markdown("<p style='text-align: center; color: gray;'>Multi-Asset: Crypto + NASDAQ + Forex (Clímax Institucional Integrado)</p>", unsafe_allow_html=True)
st.markdown("---")

# --- BARRA LATERAL ---
st.sidebar.header("🌐 Configuración del Gráfico")
lista_grafico = ["BTC/USDT", "ETH/USDT", "SOL/USDT", "BNB/USDT", "XRP/USDT", "ADA/USDT", "AVAX/USDT", "DOT/USDT", "LINK/USDT", "MATIC/USDT"] + NASDAQ_MARKETS + DOLLAR_MARKETS
symbol = st.sidebar.selectbox("Ver Gráfico de:", lista_grafico)
timeframe = st.sidebar.selectbox("Temporalidad", ["5m","15m","1h"])

# --- ESTADO Y FORZAR ESCANEO ---
st.sidebar.markdown("---")
st.sidebar.subheader("📡 Estado del Autopiloto")
st.sidebar.info(AUTOPILOTO_STATUS)

if st.sidebar.button("⚡ Forzar Escaneo Autopiloto Ahora"):
    with st.spinner("Ejecutando lógica avanzada del Autopiloto..."):
        alertas = ejecutar_logica_autopiloto(es_hilo_fondo=False)
        if alertas > 0:
            st.sidebar.success(f"¡Se enviaron {alertas} alertas a Telegram!")
        else:
            st.sidebar.warning("Escaneo completado. No se encontraron nuevas señales.")

# --- BOTONES MANUALES ---
st.sidebar.markdown("---")
st.sidebar.header("🦅 Escáner Global (Top 50 Cripto)")
if st.sidebar.button("🔍 Escanear Top 50 Mercados (Manual)"):
    with st.spinner("Analizando los 50 mercados con filtros de Clímax..."):
        st.session_state['scan_results'] = ejecutar_escaner_top50(timeframe)

if 'scan_results' in st.session_state and not st.session_state['scan_results'].empty:
    st.success("✅ Resultados del Escáner Top 50:")
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
            
        # Alertas en panel lateral con el filtro integrado
        cerca_soporte = ultima['low'] <= (ultima['soporte'] * 1.015)
        cerca_resistencia = ultima['high'] >= (ultima['resistencia'] * 0.985)
        mecha_inf = min(ultima['close'], ultima['open']) - ultima['low']
        mecha_sup = ultima['high'] - max(ultima['close'], ultima['open'])
        cuerpo = abs(ultima['close'] - ultima['open'])

        if "USDT" in symbol and ultima['volume'] > (ultima['vol_ma']*2.5):
            if cerca_soporte and (mecha_inf > cuerpo * 1.2 or ultima['close'] > ultima['open']):
                st.warning("⚠️ Clímax de Absorción (Soporte).")
            elif cerca_resistencia and (mecha_sup > cuerpo * 1.2 or ultima['close'] < ultima['open']):
                st.warning("⚠️ Clímax de Exhausción (Resistencia).")
            else:
                st.info("ℹ️ Volumen alto en zona neutral (Sin estructura clara).")

        if ultima['low'] < ultima['soporte'] and ultima['close'] > ultima['soporte']: 
            st.success("🎯 SPRING (Trampa de Compra)")
        elif ultima['high'] > ultima['resistencia'] and ultima['close'] < ultima['resistencia']: 
            st.error("🎯 UPTHRUST (Trampa de Venta)")

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
    except Exception: 
        return []

noticias = obtener_noticias()
if noticias:
    cols = st.columns(2)
    for i, n in enumerate(noticias):
        with cols[i % 2]:
            st.markdown(f"<span style='color:gray; font-size: 12px;'>● {n['fuente']}</span><br><span style='font-size: 13px;'>{n['titulo']}</span>", unsafe_allow_html=True)
            st.markdown("---")

