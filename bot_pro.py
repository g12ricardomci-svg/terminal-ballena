import streamlit as st
import ccxt
import pandas as pd
import plotly.graph_objects as go
from plotly.subplots import make_subplots
import requests
import feedparser

# ==========================================
# ⚙️ CONFIGURACIÓN GLOBAL (TUS CREDENCIALES)
# ==========================================
TELEGRAM_TOKEN = "9148434925:AAH_AuswS-GGPHadFBwcrDOUUIwBFbXvtDs"
TELEGRAM_CHAT_ID = "1496321437"

# ==========================================
# 🏦 CONFIGURACIÓN DE LA INTERFAZ
# ==========================================
st.set_page_config(page_title="Institutional Quant Terminal", layout="wide", page_icon="🏦")
st.markdown("<h1 style='text-align: center; color: #00FFAA;'>🏦 INSTITUTIONAL QUANT TERMINAL</h1>", unsafe_allow_html=True)
st.markdown("<p style='text-align: center; color: gray;'>Live Market Data & Wyckoff Order Flow</p>", unsafe_allow_html=True)
st.markdown("---")

# --- BARRA LATERAL: MERCADOS Y CONFIGURACIÓN ---
st.sidebar.header("🌐 10 Mercados Globales (Crypto 24/7)")
activos = [
    "BTC/USDT", "ETH/USDT", "SOL/USDT", "BNB/USDT", "XRP/USDT",
    "ADA/USDT", "DOGE/USDT", "AVAX/USDT", "DOT/USDT", "MATIC/USDT"
]
symbol = st.sidebar.selectbox("Selecciona el Activo", activos)
timeframe = st.sidebar.selectbox("Temporalidad", ["1m", "5m", "15m", "1h", "4h"])

st.sidebar.markdown("---")
st.sidebar.header("🔔 Alertas")
activar_alertas = st.sidebar.checkbox("Activar Alertas en Telegram", value=True)

# Botón de prueba de Telegram
if st.sidebar.button("🚀 Probar Alerta Telegram"):
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    payload = {"chat_id": TELEGRAM_CHAT_ID, "text": "✅ *SISTEMA INICIADO:* Terminal Ballena conectada y escaneando el mercado 24/7.", "parse_mode": "Markdown"}
    try:
        requests.post(url, json=payload)
        st.sidebar.success("¡Mensaje enviado a tu celular!")
    except Exception as e:
        st.sidebar.error(f"Error: Revisa tu Token o Chat ID. {e}")

# ==========================================
# 🧠 FUNCIONES DEL MOTOR (QUANT LOGIC)
# ==========================================

def enviar_telegram(mensaje):
    if not activar_alertas:
        return
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    payload = {"chat_id": TELEGRAM_CHAT_ID, "text": mensaje, "parse_mode": "Markdown"}
    try:
        requests.post(url, json=payload)
    except:
        pass

@st.cache_data(ttl=600) # Se actualiza cada 10 minutos
def obtener_noticias():
    noticias = []
    try:
        # RSS oficial de CoinDesk (Gratis e ilimitado)
        feed = feedparser.parse("https://www.coindesk.com/arc/outboundfeeds/rss/")
        
        for entry in feed.entries[:5]:
            noticias.append({
                "titulo": entry.title,
                "fuente": "CoinDesk",
                "link": entry.link
            })
            
        # Análisis de sentimiento básico (Búsqueda de palabras clave)
        sentimiento_global = "NEUTRAL"
        score = 0.0
        texto_total = " ".join([n['titulo'].lower() for n in noticias])
        
        palabras_alcistas = ["surge", "rally", "bull", "high", "gain", "soar", "jump", "record"]
        palabras_bajistas = ["drop", "bear", "low", "crash", "fall", "plunge", "dump", "fear"]
        
        score_alcista = sum(texto_total.count(p) for p in palabras_alcistas)
        score_bajista = sum(texto_total.count(p) for p in palabras_bajistas)
        
        if score_alcista > score_bajista:
            sentimiento_global = "POSITIVO"
            score = 0.5
        elif score_bajista > score_alcista:
            sentimiento_global = "NEGATIVO"
            score = -0.5
            
        return noticias, sentimiento_global, score, "OK"
    except Exception as e:
        return [], "ERROR", 0.0, str(e)

@st.cache_data(ttl=10) # Actualiza datos de precio cada 10 segundos
def get_data(symbol, timeframe):
    exchange = ccxt.binance()
    try:
        ohlcv = exchange.fetch_ohlcv(symbol, timeframe, limit=150)
        df = pd.DataFrame(ohlcv, columns=['timestamp', 'open', 'high', 'low', 'close', 'volume'])
        df['timestamp'] = pd.to_datetime(df['timestamp'], unit='ms')
        return df
    except Exception as e:
        st.error(f"Error de conexión con Binance: {e}")
        return None

def calcular_indicadores(df):
    df['vwap'] = (df['volume'] * (df['high'] + df['low'] + df['close']) / 3).cumsum() / df['volume'].cumsum()
    df['soporte'] = df['low'].rolling(window=20).min()
    df['resistencia'] = df['high'].rolling(window=20).max()
    df['vol_ma'] = df['volume'].rolling(window=20).mean()
    return df

# ==========================================
# 📊 EJECUCIÓN PRINCIPAL
# ==========================================
df = get_data(symbol, timeframe)
noticias, sentimiento_global, score, api_status = obtener_noticias()

if df is not None:
    df = calcular_indicadores(df)
    
    col_grafico, col_paneles = st.columns([2.5, 1])
    
    with col_grafico:
        fig = make_subplots(rows=2, cols=1, shared_xaxes=True, 
                            vertical_spacing=0.03, row_width=[0.2, 0.8])
        
        fig.add_trace(go.Candlestick(x=df['timestamp'], open=df['open'], high=df['high'],
                        low=df['low'], close=df['close'], name='Precio', 
                        increasing_line_color='#00FFAA', decreasing_line_color='#FF3333'), row=1, col=1)
        
        fig.add_trace(go.Scatter(x=df['timestamp'], y=df['vwap'], line=dict(color='#FF00FF', width=2), name='VWAP'), row=1, col=1)
        fig.add_trace(go.Scatter(x=df['timestamp'], y=df['soporte'], line=dict(color='#00FF00', width=1, dash='dot'), name='Soporte'), row=1, col=1)
        fig.add_trace(go.Scatter(x=df['timestamp'], y=df['resistencia'], line=dict(color='#FF0000', width=1, dash='dot'), name='Resistencia'), row=1, col=1)
        
        colors = ['#00FFAA' if row['close'] >= row['open'] else '#FF3333' for index, row in df.iterrows()]
        fig.add_trace(go.Bar(x=df['timestamp'], y=df['volume'], marker_color=colors, name='Volumen'), row=2, col=1)
        
        fig.update_layout(template='plotly_dark', xaxis_rangeslider_visible=False, height=700, 
                          margin=dict(l=10, r=10, t=10, b=10), showlegend=False)
        st.plotly_chart(fig, use_container_width=True)

    with col_paneles:
        # --- PANEL 1: SEÑALES TÉCNICAS ---
        st.markdown("### ⚙️ Señales de Flujo")
        ultima = df.iloc[-1]
        vela_anterior = df.iloc[-2]
        
        precio_actual = ultima['close']
        cambio = precio_actual - vela_anterior['close']
        st.metric(label=f"{symbol}", value=f"${precio_actual:,.2f}", delta=f"{cambio:,.2f}")
        
        if precio_actual < ultima['vwap']:
            st.error("🔴 SESGO: BAJISTA (Bajo VWAP)")
        else:
            st.success("🟢 SESGO: ALCISTA (Sobre VWAP)")
            
        if ultima['volume'] > (ultima['vol_ma'] * 2.5):
            st.warning("🚨 FLUJO INSTITUCIONAL: Volumen Clímax.")
            
        if ultima['low'] < ultima['soporte'] and ultima['close'] > ultima['soporte']:
            st.success("🎯 SPRING: Falsa ruptura a la baja. (COMPRA)")
            enviar_telegram(f"🟢 *SPRING DETECTADO* en {symbol}\nPrecio: ${precio_actual:,.2f}\nTemporalidad: {timeframe}\n*Acción:* Posible compra institucional.")
            
        elif ultima['high'] > ultima['resistencia'] and ultima['close'] < ultima['resistencia']:
            st.error("🎯 UPTHRUST: Falsa ruptura al alza. (VENTA)")
            enviar_telegram(f"🔴 *UPTHRUST DETECTADO* en {symbol}\nPrecio: ${precio_actual:,.2f}\nTemporalidad: {timeframe}\n*Acción:* Posible venta institucional.")

        st.markdown("---")
        
        # --- PANEL 2: CONTEXTO GLOBAL ---
        st.markdown("### 📰 Contexto Global (CoinDesk)")
        if sentimiento_global == "POSITIVO":
            st.success(f"Sentimiento: {sentimiento_global} (Score: {score:.2f})")
        elif sentimiento_global == "NEGATIVO":
            st.error(f"Sentimiento: {sentimiento_global} (Score: {score:.2f})")
        else:
            st.info(f"Sentimiento: {sentimiento_global} (Score: {score:.2f})")
            
        if noticias:
            for n in noticias:
                color = "#00FFAA" if "pos" in sentimiento_global.lower() else "#FF3333" if "neg" in sentimiento_global.lower() else "gray"
                st.markdown(f"<span style='color:{color}; font-size: 12px;'>● {n['fuente']}</span><br><span style='font-size: 13px;'>{n['titulo']}</span>", unsafe_allow_html=True)
                st.markdown("---")
        else:
            st.caption(f"Noticias: {api_status}")