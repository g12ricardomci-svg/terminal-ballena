import streamlit as st
import ccxt
import pandas as pd
import plotly.graph_objects as go
from plotly.subplots import make_subplots
import requests
import feedparser
import time

# ==========================================
# ⚙️ CONFIGURACIÓN GLOBAL
# ==========================================
TELEGRAM_TOKEN = "9148434925:AAH_AuswS-GGPHadFBwcrDOUUIwBFbXvtDs"
TELEGRAM_CHAT_ID = "1496321437"

st.set_page_config(page_title="Institutional Quant Terminal", layout="wide", page_icon="🏦")

st.markdown("<h1 style='text-align: center; color: #00FFAA;'>🏦 INSTITUTIONAL QUANT TERMINAL</h1>", unsafe_allow_html=True)
st.markdown("<p style='text-align: center; color: gray;'>Global Market Scanner & Wyckoff Order Flow</p>", unsafe_allow_html=True)
st.markdown("---")

# --- BARRA LATERAL ---
st.sidebar.header("🌐 Configuración del Gráfico")
activos = ["BTC/USDT", "ETH/USDT", "SOL/USDT", "BNB/USDT", "XRP/USDT", "ADA/USDT", "DOGE/USDT", "AVAX/USDT", "DOT/USDT", "MATIC/USDT"]
symbol = st.sidebar.selectbox("Ver Gráfico de:", activos)
timeframe = st.sidebar.selectbox("Temporalidad", ["1m", "5m", "15m", "1h", "4h"])

st.sidebar.markdown("---")
st.sidebar.header("🔔 Alertas")
activar_alertas = st.sidebar.checkbox("Activar Alertas en Telegram", value=True)

if st.sidebar.button("🚀 Probar Alerta Telegram"):
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    payload = {"chat_id": TELEGRAM_CHAT_ID, "text": "✅ *SISTEMA EN LÍNEA:* Terminal Ballena conectada y lista para operar.", "parse_mode": "Markdown"}
    try:
        requests.post(url, json=payload)
        st.sidebar.success("¡Mensaje enviado!")
    except Exception as e:
        st.sidebar.error(f"Error: {e}")

# 🔍 BOTÓN DEL ESCÁNER MASIVO
st.sidebar.markdown("---")
st.sidebar.header("🦅 Escáner Global (Top 50)")
escanear_todo = st.sidebar.button("🔍 Escanear Top 50 Mercados")

# ==========================================
# 🧠 FUNCIONES
# ==========================================
def enviar_telegram(mensaje):
    if not activar_alertas: return
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    payload = {"chat_id": TELEGRAM_CHAT_ID, "text": mensaje, "parse_mode": "Markdown"}
    try: requests.post(url, json=payload)
    except: pass

@st.cache_data(ttl=600)
def obtener_noticias():
    noticias = []
    try:
        feed = feedparser.parse("https://www.coindesk.com/arc/outboundfeeds/rss/")
        for entry in feed.entries[:5]:
            noticias.append({"titulo": entry.title, "fuente": "CoinDesk"})
        
        texto_total = " ".join([n['titulo'].lower() for n in noticias])
        alcistas = sum(texto_total.count(p) for p in ["surge", "rally", "bull", "high", "gain"])
        bajistas = sum(texto_total.count(p) for p in ["drop", "bear", "low", "crash", "fall"])
        
        sentimiento, score = "NEUTRAL", 0.0
        if alcistas > bajistas: sentimiento, score = "POSITIVO", 0.5
        elif bajistas > alcistas: sentimiento, score = "NEGATIVO", -0.5
        return noticias, sentimiento, score, "OK"
    except Exception as e:
        return [], "ERROR", 0.0, str(e)

# 📊 LÓGICA DEL ESCÁNER DINÁMICO (Ahora con OKX)
def ejecutar_escaner_global(tf):
    exchange = ccxt.okx() # Usamos OKX para evitar bloqueos en la nube
    
    try:
        tickers = exchange.fetch_tickers()
    except Exception as e:
        st.error(f"Error obteniendo tickers: {e}")
        return pd.DataFrame()

    mercados_usdt = []
    for sym, data in tickers.items():
        if sym.endswith('/USDT') and data['quoteVolume'] is not None:
            mercados_usdt.append((sym, data['quoteVolume'], data['percentage']))
            
    mercados_top50 = sorted(mercados_usdt, key=lambda x: x[1], reverse=True)[:50]
    
    resultados = []
    progress_bar = st.progress(0)
    status_text = st.empty()
    
    for i, (sym, vol, cambio_24h) in enumerate(mercados_top50):
        status_text.text(f"Escaneando {sym} ({i+1}/50)...")
        try:
            ohlcv = exchange.fetch_ohlcv(sym, tf, limit=30)
            df = pd.DataFrame(ohlcv, columns=['timestamp', 'open', 'high', 'low', 'close', 'volume'])
            
            df['vwap'] = (df['volume'] * (df['high'] + df['low'] + df['close']) / 3).cumsum() / df['volume'].cumsum()
            df['soporte'] = df['low'].rolling(window=20).min()
            df['resistencia'] = df['high'].rolling(window=20).max()
            df['vol_ma'] = df['volume'].rolling(window=20).mean()
            
            ultima = df.iloc[-1]
            precio = ultima['close']
            sesgo = "ALCISTA" if precio > ultima['vwap'] else "BAJISTA"
            señal = "NEUTRAL"
            
            if ultima['low'] < ultima['soporte'] and ultima['close'] > ultima['soporte']:
                señal = "🟢 SPRING"
            elif ultima['high'] > ultima['resistencia'] and ultima['close'] < ultima['resistencia']:
                señal = "🔴 UPTHRUST"
            elif ultima['volume'] > (ultima['vol_ma'] * 2.5):
                señal = "⚠️ VOL CLÍMAX"
                
            # GUARDAMOS TODOS LOS RESULTADOS (No solo los que tienen señal)
            resultados.append({"Par": sym, "Precio": precio, "Sesgo": sesgo, "Señal": señal, "Cambio 24h %": cambio_24h})
                
            if señal in ["🟢 SPRING", "🔴 UPTHRUST"]:
                accion = "COMPRA" if "SPRING" in señal else "VENTA"
                enviar_telegram(f"🚨 *ESCÁNER GLOBAL:* {señal} en {sym}\nPrecio: ${precio:,.2f}\nSesgo: {sesgo}\n*Posible {accion}*")
            
            time.sleep(0.05) # Pausa mínima
            
        except Exception as e:
            # Si falla una, la agregamos a la tabla como error para que lo sepas
            resultados.append({"Par": sym, "Precio": 0, "Sesgo": "ERROR", "Señal": "Fallo de API", "Cambio 24h %": cambio_24h})
            
        progress_bar.progress((i + 1) / 50)
    
    status_text.empty()
    progress_bar.empty()
    
    # Convertimos a DataFrame y ordenamos para que las señales salgan primero
    df_final = pd.DataFrame(resultados)
    if not df_final.empty:
        df_final['Orden'] = df_final['Señal'].apply(lambda x: 1 if x != "NEUTRAL" else 2)
        df_final = df_final.sort_values(by=['Orden', 'Cambio 24h %'], ascending=[True, False]).drop(columns=['Orden'])
        
    return df_final

# ==========================================
# 📊 EJECUCIÓN PRINCIPAL
# ==========================================

if escanear_todo:
    st.info("🦅 Iniciando Escáner Global en OKX. Buscando los 50 mercados con más volumen...")
    resultados = ejecutar_escaner_global(timeframe)
    
    if not resultados.empty:
        st.success(f"✅ Escaneo completado. Analizados {len(resultados)} mercados (Ordenados por oportunidad).")
        st.dataframe(resultados, use_container_width=True)
    else:
        st.warning("El escáner no pudo obtener datos. Intenta de nuevo en 1 minuto.")
    st.markdown("---")

@st.fragment(run_every="5s") 
def render_trading_panel(symbol, timeframe):
    exchange = ccxt.okx() # Cambiado a OKX también para el gráfico principal
    try:
        ohlcv = exchange.fetch_ohlcv(symbol, timeframe, limit=150)
        df = pd.DataFrame(ohlcv, columns=['timestamp', 'open', 'high', 'low', 'close', 'volume'])
        df['timestamp'] = pd.to_datetime(df['timestamp'], unit='ms')
        
        df['vwap'] = (df['volume'] * (df['high'] + df['low'] + df['close']) / 3).cumsum() / df['volume'].cumsum()
        df['soporte'] = df['low'].rolling(window=20).min()
        df['resistencia'] = df['high'].rolling(window=20).max()
        df['vol_ma'] = df['volume'].rolling(window=20).mean()
    except Exception as e:
        st.error(f"Error de conexión: {e}")
        return

    col_grafico, col_paneles = st.columns([2.5, 1])
    
    with col_grafico:
        fig = make_subplots(rows=2, cols=1, shared_xaxes=True, vertical_spacing=0.03, row_width=[0.2, 0.8])
        fig.add_trace(go.Candlestick(x=df['timestamp'], open=df['open'], high=df['high'], low=df['low'], close=df['close'], 
                                     increasing_line_color='#00FFAA', decreasing_line_color='#FF3333'), row=1, col=1)
        fig.add_trace(go.Scatter(x=df['timestamp'], y=df['vwap'], line=dict(color='#FF00FF', width=2), name='VWAP'), row=1, col=1)
        fig.add_trace(go.Scatter(x=df['timestamp'], y=df['soporte'], line=dict(color='#00FF00', width=1, dash='dot'), name='Soporte'), row=1, col=1)
        fig.add_trace(go.Scatter(x=df['timestamp'], y=df['resistencia'], line=dict(color='#FF0000', width=1, dash='dot'), name='Resistencia'), row=1, col=1)
        
        colors = ['#00FFAA' if row['close'] >= row['open'] else '#FF3333' for index, row in df.iterrows()]
        fig.add_trace(go.Bar(x=df['timestamp'], y=df['volume'], marker_color=colors, name='Volumen'), row=2, col=1)
        
        fig.update_layout(template='plotly_dark', xaxis_rangeslider_visible=False, height=700, margin=dict(l=10, r=10, t=10, b=10), showlegend=False)
        st.plotly_chart(fig, use_container_width=True)

    with col_paneles:
        st.markdown("### ⚙️ Señales de Flujo")
        ultima = df.iloc[-1]
        vela_anterior = df.iloc[-2]
        
        precio_actual = ultima['close']
        st.metric(label=f"{symbol}", value=f"${precio_actual:,.2f}", delta=f"{precio_actual - vela_anterior['close']:,.2f}")
        
        if precio_actual < ultima['vwap']: st.error("🔴 SESGO: BAJISTA")
        else: st.success("🟢 SESGO: ALCISTA")
            
        if ultima['volume'] > (ultima['vol_ma'] * 2.5): st.warning("🚨 Volumen Clímax.")
            
        if ultima['low'] < ultima['soporte'] and ultima['close'] > ultima['soporte']:
            st.success("🎯 SPRING: Falsa ruptura (COMPRA)")
            entry = precio_actual
            sl = ultima['low'] * 0.999
            tp = entry + ((entry - sl) * 2)
            if vela_anterior['low'] >= vela_anterior['soporte']:
                enviar_telegram(f"🟢 *SPRING* en {symbol}\nEntrada: ${entry:,.2f}\nStop: ${sl:,.2f}\nTarget: ${tp:,.2f}")
            
        elif ultima['high'] > ultima['resistencia'] and ultima['close'] < ultima['resistencia']:
            st.error("🎯 UPTHRUST: Falsa ruptura (VENTA)")
            entry = precio_actual
            sl = ultima['high'] * 1.001
            tp = entry - ((sl - entry) * 2)
            if vela_anterior['high'] <= vela_anterior['resistencia']:
                enviar_telegram(f"🔴 *UPTHRUST* en {symbol}\nEntrada: ${entry:,.2f}\nStop: ${sl:,.2f}\nTarget: ${tp:,.2f}")

render_trading_panel(symbol, timeframe)

st.markdown("---")
st.markdown("### 📰 Contexto Global")
noticias, sentimiento_global, score, api_status = obtener_noticias()

if sentimiento_global == "POSITIVO": st.success(f"Sentimiento: {sentimiento_global} ({score:.2f})")
elif sentimiento_global == "NEGATIVO": st.error(f"Sentimiento: {sentimiento_global} ({score:.2f})")
else: st.info(f"Sentimiento: {sentimiento_global} ({score:.2f})")
    
if noticias:
    cols = st.columns(2)
    for i, n in enumerate(noticias):
        with cols[i % 2]:
            st.markdown(f"<span style='color:gray; font-size: 12px;'>● {n['fuente']}</span><br><span style='font-size: 13px;'>{n['titulo']}</span>", unsafe_allow_html=True)
            st.markdown("---")
else:
    st.caption(f"Noticias: {api_status}")
