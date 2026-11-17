import streamlit as st
import ccxt
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
WHALE_ALERT_KEY = "TU_WHALE_ALERT_API_KEY" 

st.set_page_config(page_title="Institutional Quant Terminal", layout="wide", page_icon="🏦")

# ==========================================
# 🧠 FUNCIONES DE APOYO
# ==========================================
def enviar_telegram(mensaje):
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    payload = {"chat_id": TELEGRAM_CHAT_ID, "text": mensaje, "parse_mode": "Markdown"}
    try: requests.post(url, json=payload)
    except: pass

def calcular_obi(bids, asks, levels=20):
    qb = sum(a for _, a in bids[:levels])
    qa = sum(a for _, a in asks[:levels])
    if qb + qa == 0: return 0.0
    return (qb - qa) / (qb + qa)

def obtener_flujo_ballenas():
    if WHALE_ALERT_KEY == "TU_WHALE_ALERT_API_KEY":
        return "NEUTRO"
    try:
        r = requests.get(
            "https://api.whale-alert.io/v1/transactions",
            params={"api_key": WHALE_ALERT_KEY, "min_value": 1000000, "limit": 5}
        )
        txs = r.json().get("transactions", [])
        if not txs: return "SIN MOVIMIENTOS"
        entradas = sum(1 for t in txs if t.get("to", {}).get("owner_type") == "exchange")
        salidas = sum(1 for t in txs if t.get("from", {}).get("owner_type") == "exchange")
        if entradas > salidas: return "🔴 PRESIÓN BAJISTA"
        elif salidas > entradas: return "🟢 ACUMULACIÓN"
        return "⚪ NEUTRO"
    except:
        return "ERROR API"

def calcular_vwap(df):
    return (df['volume'] * (df['high'] + df['low'] + df['close']) / 3).cumsum() / df['volume'].cumsum()

# Variable global para guardar el último escaneo y mostrarlo en pantalla
if 'ultimo_escaneo' not in st.session_state:
    st.session_state['ultimo_escaneo'] = []

# ==========================================
# 🤖 AUTOPILOTO CORREGIDO
# ==========================================
def escanear_confluencia():
    exchange = ccxt.okx()
    activos = ["BTC/USDT", "ETH/USDT", "SOL/USDT", "BNB/USDT", "XRP/USDT", "ADA/USDT", "AVAX/USDT", "LINK/USDT"]
    log_escaner = []

    for symbol in activos:
        try:
            # 1. Tendencia Macro (1H)
            ohlcv_1h = exchange.fetch_ohlcv(symbol, '1h', limit=200)
            df_1h = pd.DataFrame(ohlcv_1h, columns=['timestamp','open','high','low','close','volume'])
            ema200 = df_1h['close'].ewm(span=200, adjust=False).mean().iloc[-1]
            tendencia_macro = "ALCISTA" if df_1h['close'].iloc[-1] > ema200 else "BAJISTA"

            # 2. Sesgo VWAP (15m)
            ohlcv_15m = exchange.fetch_ohlcv(symbol, '15m', limit=50)
            df_15m = pd.DataFrame(ohlcv_15m, columns=['timestamp','open','high','low','close','volume'])
            df_15m['vwap'] = calcular_vwap(df_15m)
            sesgo_15m = "ALCISTA" if df_15m['close'].iloc[-1] > df_15m['vwap'].iloc[-1] else "BAJISTA"

            # 3. Order Book Imbalance (OBI)
            ob = exchange.fetch_order_book(symbol, limit=20)
            obi = calcular_obi(ob["bids"], ob["asks"], levels=20)

            # 4. Gatillo Wyckoff (5m) - ¡USAMOS VELA CERRADA (-2)!
            ohlcv_5m = exchange.fetch_ohlcv(symbol, '5m', limit=50)
            df_5m = pd.DataFrame(ohlcv_5m, columns=['timestamp','open','high','low','close','volume'])
            df_5m['soporte'] = df_5m['low'].rolling(window=20).min()
            df_5m['resistencia'] = df_5m['high'].rolling(window=20).max()
            
            # La vela cerrada es la -2. La -1 es la que se está moviendo ahora.
            vela_cerrada = df_5m.iloc[-2] 
            precio = vela_cerrada['close']

            flujo = obtener_flujo_ballenas()

            # --- LÓGICA DE COMPRA (Relajada pero profesional) ---
            # Solo exigimos: Macro Alcista + Spring en vela cerrada + OBI no muy negativo
            es_spring = (vela_cerrada['low'] < vela_cerrada['soporte']) and (vela_cerrada['close'] > vela_cerrada['soporte'])
            
            if es_spring and tendencia_macro == "ALCISTA" and obi > -0.4:
                entry = precio
                sl = vela_cerrada['low'] * 0.998
                riesgo = entry - sl
                tp1 = entry + (riesgo * 1.5)
                tp2 = entry + (riesgo * 3.0)

                mensaje = (f"🚀 *SETUP A+ (COMPRA)* 🚀\n\n"
                           f"🪙 *Activo:* {symbol}\n"
                           f"⏱ *Entrada:* 5m (Vela Cerrada)\n"
                           f"📈 *Macro (1H):* {tendencia_macro}\n"
                           f"📊 *VWAP (15m):* {sesgo_15m}\n"
                           f"⚖️ *OBI:* {obi:+.2f} | 🐋 *Ballenas:* {flujo}\n\n"
                           f"🎯 *ENTRADA:* ${entry:,.2f}\n"
                           f"🛑 *STOP LOSS:* ${sl:,.2f}\n"
                           f"💰 *TP1:* ${tp1:,.2f}\n"
                           f"💰 *TP2:* ${tp2:,.2f}")
                enviar_telegram(mensaje)
                log_escaner.append(f"✅ {symbol}: ¡SETUP ENVIADO! (Spring + Macro ALCISTA)")

            # --- LÓGICA DE VENTA (Corregida) ---
            es_upthrust = (vela_cerrada['high'] > vela_cerrada['resistencia']) and (vela_cerrada['close'] < vela_cerrada['resistencia'])
            
            if es_upthrust and tendencia_macro == "BAJISTA" and obi < 0.4:
                entry = precio
                sl = vela_cerrada['high'] * 1.002
                riesgo = sl - entry
                tp1 = entry - (riesgo * 1.5)
                tp2 = entry - (riesgo * 3.0)

                mensaje = (f"🔻 *SETUP A+ (VENTA)* 🔻\n\n"
                           f"🪙 *Activo:* {symbol}\n"
                           f"⏱ *Entrada:* 5m (Vela Cerrada)\n"
                           f"📉 *Macro (1H):* {tendencia_macro}\n"
                           f"📊 *VWAP (15m):* {sesgo_15m}\n"
                           f"⚖️ *OBI:* {obi:+.2f} | 🐋 *Ballenas:* {flujo}\n\n"
                           f"🎯 *ENTRADA:* ${entry:,.2f}\n"
                           f"🛑 *STOP LOSS:* ${sl:,.2f}\n"
                           f"💰 *TP1:* ${tp1:,.2f}\n"
                           f"💰 *TP2:* ${tp2:,.2f}")
                enviar_telegram(mensaje)
                log_escaner.append(f"✅ {symbol}: ¡SETUP ENVIADO! (Upthrust + Macro BAJISTA)")
                
            else:
                # Guardamos por qué NO se envió (para que lo veas en pantalla)
                log_escaner.append(f"⏳ {symbol}: Macro {tendencia_macro} | OBI {obi:+.2f} | Sin Trampa")

        except Exception as e:
            log_escaner.append(f"❌ {symbol}: Error {str(e)[:20]}")
            
    st.session_state['ultimo_escaneo'] = log_escaner

@st.cache_resource
def iniciar_autopiloto():
    def loop():
        while True:
            escanear_confluencia()
            time.sleep(300) # 5 minutos
    thread = threading.Thread(target=loop, daemon=True)
    thread.start()
    return thread

iniciar_autopiloto()

# ==========================================
# 📊 INTERFAZ GRÁFICA (Con Radar de Estado)
# ==========================================
st.markdown("<h1 style='text-align: center; color: #00FFAA;'>🏦 INSTITUTIONAL QUANT TERMINAL</h1>", unsafe_allow_html=True)
st.markdown("<p style='text-align: center; color: gray;'>Autopilot ON | Wyckoff + OBI + On-chain</p>", unsafe_allow_html=True)
st.markdown("---")

st.sidebar.header("🌐 Configuración del Gráfico")
activos = ["BTC/USDT","ETH/USDT","SOL/USDT","BNB/USDT","XRP/USDT", "ADA/USDT","DOGE/USDT","AVAX/USDT","DOT/USDT","MATIC/USDT"]
symbol = st.sidebar.selectbox("Ver Gráfico de:", activos)
timeframe = st.sidebar.selectbox("Temporalidad", ["1m","5m","15m","1h","4h"])

if st.sidebar.button("🚀 Probar Alerta Telegram"):
    enviar_telegram("✅ *SISTEMA EN LÍNEA:* Probando conexión.")
    st.sidebar.success("¡Mensaje enviado!")

# --- RADAR DE ESTADO DEL AUTOPILOTO ---
st.sidebar.markdown("---")
st.sidebar.subheader("📡 Radar del Autopiloto")
if st.session_state['ultimo_escaneo']:
    for log in st.session_state['ultimo_escaneo'][:5]: # Mostrar los primeros 5
        if "✅" in log:
            st.sidebar.success(log)
        elif "❌" in log:
            st.sidebar.error(log)
        else:
            st.sidebar.text(log)
else:
    st.sidebar.info("El autopiloto está iniciando su primer escaneo...")

# --- GRÁFICO EN VIVO ---
@st.fragment(run_every="5s")
def render_trading_panel(symbol, timeframe):
    exchange = ccxt.okx()
    try:
        ohlcv = exchange.fetch_ohlcv(symbol, timeframe, limit=150)
        df = pd.DataFrame(ohlcv, columns=['timestamp','open','high','low','close','volume'])
        df['timestamp'] = pd.to_datetime(df['timestamp'], unit='ms')
        df['vwap'] = calcular_vwap(df)
        df['soporte'] = df['low'].rolling(window=20).min()
        df['resistencia'] = df['high'].rolling(window=20).max()
        df['vol_ma'] = df['volume'].rolling(window=20).mean()
    except Exception as e:
        st.error(f"Error de conexión: {e}")
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
        st.metric(label=f"{symbol}", value=f"${precio_actual:,.2f}",
                  delta=f"{precio_actual - anterior['close']:,.2f}")

        if precio_actual < ultima['vwap']: st.error("🔴 SESGO: BAJISTA")
        else: st.success("🟢 SESGO: ALCISTA")

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
