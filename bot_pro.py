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
from datetime import datetime

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
# 🧠 FUNCIONES DE APOYO
# ==========================================
def enviar_telegram(mensaje):
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    payload = {"chat_id": TELEGRAM_CHAT_ID, "text": mensaje, "parse_mode": "Markdown"}
    try: requests.post(url, json=payload)
    except: pass

def calcular_vwap(df):
    return (df['volume'] * (df['high'] + df['low'] + df['close']) / 3).cumsum() / df['volume'].cumsum()

def obtener_datos(symbol, timeframe, limit=100):
    """Enruta la petición a ccxt (Cripto) o yfinance (Acciones/Forex)"""
    df = pd.DataFrame()
    
    # 1. CRIPTOMONEDAS (OKX)
    if "/" in symbol and "USDT" in symbol:
        exchange = ccxt.okx()
        try:
            ohlcv = exchange.fetch_ohlcv(symbol, timeframe, limit=limit)
            df = pd.DataFrame(ohlcv, columns=['timestamp', 'open', 'high', 'low', 'close', 'volume'])
            df['timestamp'] = pd.to_datetime(df['timestamp'], unit='ms')
        except:
            return pd.DataFrame()
            
    # 2. ACCIONES Y FOREX (Yahoo Finance)
    else:
        # Mapeo de temporalidades de Streamlit a YFinance
        interval_map = {"1m": "1m", "5m": "5m", "15m": "15m", "1h": "60m", "4h": "1h"} 
        yf_interval = interval_map.get(timeframe, "5m")
        
        # YFinance requiere periodos específicos para 1m y 5m
        period = "1d" if yf_interval in ["1m", "5m", "15m"] else "5d"
        
        try:
            ticker = yf.Ticker(symbol)
            hist = ticker.history(period=period, interval=yf_interval)
            if hist.empty: return pd.DataFrame()
            
            df = hist.reset_index()
            df.rename(columns={'Datetime': 'timestamp', 'Date': 'timestamp'}, inplace=True)
            df['timestamp'] = pd.to_datetime(df['timestamp']).dt.tz_localize(None)
            
            # Si el volumen es 0 (común en Forex), le ponemos 1 para evitar errores matemáticos
            df['volume'] = df['volume'].replace(0, 1)
            
            # Asegurar que solo tomamos las últimas X velas
            df = df.tail(limit)
        except Exception:
            return pd.DataFrame()

    # Calcular indicadores
    if not df.empty:
        df['vwap'] = calcular_vwap(df)
        df['soporte'] = df['low'].rolling(window=20).min()
        df['resistencia'] = df['high'].rolling(window=20).max()
        df['vol_ma'] = df['volume'].rolling(window=20).mean()
        
    return df

# ==========================================
# 🤖 AUTOPILOTO MULTI-MERCADO
# ==========================================
def escanear_confluencia():
    for symbol in ALL_SYMBOLS:
        try:
            # 1. Macro (1H) - Solo si hay suficientes datos
            df_1h = obtener_datos(symbol, '1h', limit=50)
            if df_1h.empty: continue
            ema200 = df_1h['close'].ewm(span=50, adjust=False).mean().iloc[-1] # Usamos 50 por falta de historia en yf 1h
            tendencia_macro = "ALCISTA" if df_1h['close'].iloc[-1] > ema200 else "BAJISTA"

            # 2. VWAP (15m)
            df_15m = obtener_datos(symbol, '15m', limit=50)
            if df_15m.empty: continue
            sesgo_15m = "ALCISTA" if df_15m['close'].iloc[-1] > df_15m['vwap'].iloc[-1] else "BAJISTA"

            # 3. Order Book Imbalance (OBI) - SOLO PARA CRIPTO
            obi = 0.0
            if "/" in symbol:
                exchange = ccxt.okx()
                ob = exchange.fetch_order_book(symbol, limit=20)
                qb = sum(a for _, a in ob["bids"][:20])
                qa = sum(a for _, a in ob["asks"][:20])
                obi = (qb - qa) / (qb + qa) if (qb + qa) > 0 else 0.0

            # 4. Gatillo (5m)
            df_5m = obtener_datos(symbol, '5m', limit=50)
            if df_5m.empty: continue
            ultima = df_5m.iloc[-1]
            precio = ultima['close']

            # --- SEÑALES WYCKOFF ---
            es_spring = (ultima['low'] < ultima['soporte']) and (ultima['close'] > ultima['soporte'])
            es_upthrust = (ultima['high'] > ultima['resistencia']) and (ultima['close'] < ultima['resistencia'])
            es_climax = ultima['volume'] > (ultima['vol_ma'] * 2.5)
            
            tipo_mercado = "CRIPTO" if "/" in symbol else ("ACCION" if symbol in NASDAQ_MARKETS else "DIVISA")

            if es_spring and tendencia_macro == "ALCISTA" and obi > -0.4:
                entry, sl = precio, ultima['low'] * 0.998
                tp1, tp2 = entry + ((entry - sl) * 1.5), entry + ((entry - sl) * 3.0)
                msg = f"🚀 *SETUP A+ COMPRA ({tipo_mercado})* 🚀\n\n🪙 *Activo:* {symbol}\n⏱ *Entrada:* 5m\n🎯 *Entrada:* ${entry:,.2f}\n🛑 *SL:* ${sl:,.2f}\n💰 *TP1:* ${tp1:,.2f}\n💰 *TP2:* ${tp2:,.2f}\n📊 *OBI:* {obi:+.2f}"
                enviar_telegram(msg)

            elif es_upthrust and tendencia_macro == "BAJISTA":
                entry, sl = precio, ultima['high'] * 1.002
                tp1, tp2 = entry - ((sl - entry) * 1.5), entry - ((sl - entry) * 3.0)
                msg = f"🔻 *SETUP A+ VENTA ({tipo_mercado})* 🔻\n\n🪙 *Activo:* {symbol}\n⏱ *Entrada:* 5m\n🎯 *Entrada:* ${entry:,.2f}\n🛑 *SL:* ${sl:,.2f}\n💰 *TP1:* ${tp1:,.2f}\n💰 *TP2:* ${tp2:,.2f}\n📊 *OBI:* {obi:+.2f}"
                enviar_telegram(msg)

            elif es_climax:
                msg = f"⚠️ *VOLUMEN CLÍMAX ({tipo_mercado})* ⚠️\n\n🪙 *Activo:* {symbol}\n💰 *Precio:* ${precio:,.2f}\n📊 *Sesgo:* {sesgo_15m}\n👀 *Atención:* Movimiento institucional detectado."
                enviar_telegram(msg)
                
            time.sleep(0.5) # Pausa para no saturar las APIs
        except Exception as e:
            pass

@st.cache_resource
def iniciar_autopiloto():
    def loop():
        while True:
            escanear_confluencia()
            time.sleep(300) # Escanea cada 5 minutos
    thread = threading.Thread(target=loop, daemon=True)
    thread.start()
    return thread

iniciar_autopiloto()

# ==========================================
# 📊 INTERFAZ GRÁFICA
# ==========================================
st.markdown("<h1 style='text-align: center; color: #00FFAA;'>🏦 INSTITUTIONAL QUANT TERMINAL</h1>", unsafe_allow_html=True)
st.markdown("<p style='text-align: center; color: gray;'>Multi-Asset: Crypto + NASDAQ + Forex</p>", unsafe_allow_html=True)
st.markdown("---")

# --- BARRA LATERAL ---
st.sidebar.header("🌐 Configuración del Gráfico")
# Agrupamos por categoría para el dropdown
symbol = st.sidebar.selectbox("Ver Gráfico de:", ALL_SYMBOLS)

timeframe = st.sidebar.selectbox("Temporalidad", ["5m","15m","1h"])

st.sidebar.markdown("---")
st.sidebar.header("🔔 Alertas")
if st.sidebar.button("🚀 Probar Alerta Telegram"):
    enviar_telegram("✅ *SISTEMA EN LÍNEA:* Multi-Asset Terminal conectado.")
    st.sidebar.success("¡Mensaje enviado!")

st.sidebar.info("🤖 Autopiloto escaneando Cripto (OKX), Acciones (NASDAQ) y Forex en tiempo real.")

# --- GRÁFICO EN VIVO ---
@st.fragment(run_every="10s") # 10s para no saturar Yahoo Finance
def render_trading_panel(symbol, timeframe):
    df = obtener_datos(symbol, timeframe, limit=150)
    
    if df.empty:
        st.error(f"No se pudieron cargar los datos para {symbol}. Intenta con otra temporalidad o espera un momento.")
        return

    col_grafico, col_paneles = st.columns([2.5, 1])

    with col_grafico:
        fig = make_subplots(rows=2, cols=1, shared_xaxes=True, vertical_spacing=0.03, row_width=[0.2,0.8])
        fig.add_trace(go.Candlestick(x=df['timestamp'], open=df['open'], high=df['high'],
                                     low=df['low'], close=df['close'],
                                     increasing_line_color='#00FFAA', decreasing_line_color='#FF3333'), row=1, col=1)
        fig.add_trace(go.Scatter(x=df['timestamp'], y=df['vwap'], line=dict(color='#FF00FF', width=2), name='VWAP'), row=1, col=1)
        fig.add_trace(go.Scatter(x=df['timestamp'], y=df['soporte'], line=dict(color='#00FF00', width=1, dash='dot'), name='Soporte'), row=1, col=1)
        fig.add_trace(go.Scatter(x=df['timestamp'], y=df['resistencia'], line=dict(color='#FF0000', width=1, dash='dot'), name='Resistencia'), row=1, col=1)
        colors = ['#00FFAA' if r['close']>=r['open'] else '#FF3333' for _,r in df.iterrows()]
        fig.add_trace(go.Bar(x=df['timestamp'], y=df['volume'], marker_color=colors, name='Volumen'), row=2, col=1)
        fig.update_layout(template='plotly_dark', xaxis_rangeslider_visible=False, height=600,
                          margin=dict(l=10,r=10,t=10,b=10), showlegend=False)
        st.plotly_chart(fig, use_container_width=True)

    with col_paneles:
        st.markdown("### ⚙️ Señales de Flujo")
        ultima = df.iloc[-1]
        anterior = df.iloc[-2]
        precio_actual = ultima['close']
        st.metric(label=f"{symbol}", value=f"${precio_actual:,.2f}", delta=f"{precio_actual - anterior['close']:,.2f}")

        if precio_actual < ultima['vwap']: st.error("🔴 SESGO: BAJISTA")
        else: st.success("🟢 SESGO: ALCISTA")

        if ultima['volume'] > (ultima['vol_ma']*2.5): st.warning("🚨 Volumen Clímax.")

        if ultima['low'] < ultima['soporte'] and ultima['close'] > ultima['soporte']:
            st.success("🎯 SPRING (Trampa de Compra)")
        elif ultima['high'] > ultima['resistencia'] and ultima['close'] < ultima['resistencia']:
            st.error("🎯 UPTHRUST (Trampa de Venta)")

render_trading_panel(symbol, timeframe)

# --- CONTEXTO GLOBAL ---
st.markdown("---")
st.markdown("### 📰 Contexto Global")
@st.cache_data(ttl=600)
def obtener_noticias():
    noticias = []
    try:
        feed = feedparser.parse("https://www.coindesk.com/arc/outboundfeeds/rss/")
        for entry in feed.entries[:4]:
            noticias.append({"titulo": entry.title, "fuente": "CoinDesk"})
        return noticias
    except:
        return []

noticias = obtener_noticias()
if noticias:
    cols = st.columns(2)
    for i, n in enumerate(noticias):
        with cols[i % 2]:
            st.markdown(f"<span style='color:gray; font-size: 12px;'>● {n['fuente']}</span><br>"
                        f"<span style='font-size: 13px;'>{n['titulo']}</span>", unsafe_allow_html=True)
            st.markdown("---")
