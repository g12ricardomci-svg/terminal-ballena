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
WHALE_ALERT_KEY = "TU_WHALE_ALERT_API_KEY"  # Opcional

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
        return "NEUTRO (sin API key)"
    try:
        r = requests.get(
            "https://api.whale-alert.io/v1/transactions",
            params={"api_key": WHALE_ALERT_KEY, "min_value": 1000000, "limit": 5}
        )
        txs = r.json().get("transactions", [])
        if not txs: return "SIN MOVIMIENTOS RECIENTES"
        entradas = sum(1 for t in txs if t.get("to", {}).get("owner_type") == "exchange")
        salidas = sum(1 for t in txs if t.get("from", {}).get("owner_type") == "exchange")
        if entradas > salidas: return "🔴 PRESIÓN BAJISTA (ballenas depositan en exchange)"
        elif salidas > entradas: return "🟢 ACUMULACIÓN (ballenas retiran de exchange)"
        return "⚪ NEUTRO"
    except:
        return "ERROR CONSULTANDO WHALE ALERT"

def calcular_vwap(df):
    return (df['volume'] * (df['high'] + df['low'] + df['close']) / 3).cumsum() / df['volume'].cumsum()

# ==========================================
# 🤖 AUTOPILOTO (Con Alertas de Wyckoff y Volumen Clímax)
# ==========================================
def escanear_confluencia():
    exchange = ccxt.okx()
    activos = ["BTC/USDT", "ETH/USDT", "SOL/USDT", "BNB/USDT", "XRP/USDT",
               "ADA/USDT", "AVAX/USDT", "DOT/USDT", "MATIC/USDT", "LINK/USDT"]

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
            filtro_obi = obi > -0.3

            # 4. Gatillo Wyckoff (5m)
            ohlcv_5m = exchange.fetch_ohlcv(symbol, '5m', limit=50)
            df_5m = pd.DataFrame(ohlcv_5m, columns=['timestamp','open','high','low','close','volume'])
            df_5m['soporte'] = df_5m['low'].rolling(window=20).min()
            df_5m['resistencia'] = df_5m['high'].rolling(window=20).max()
            df_5m['vol_ma'] = df_5m['volume'].rolling(window=20).mean() # NUEVO: Media de volumen
            
            ultima = df_5m.iloc[-1]
            precio = ultima['close']

            flujo = obtener_flujo_ballenas()
            onchain_ok = "PRESIÓN BAJISTA" not in flujo

            # --- LÓGICA DE COMPRA (SPRING) ---
            if (ultima['low'] < ultima['soporte'] and ultima['close'] > ultima['soporte']
                and tendencia_macro == "ALCISTA" and sesgo_15m == "ALCISTA"
                and filtro_obi and onchain_ok):

                entry = precio
                sl = ultima['low'] * 0.998
                riesgo = entry - sl
                tp1 = entry + (riesgo * 1.5)
                tp2 = entry + (riesgo * 3.0)

                mensaje = (f"🚀 *SETUP A+ (COMPRA)* 🚀\n\n"
                           f"🪙 *Activo:* {symbol}\n"
                           f"⏱ *Entrada:* 5m\n"
                           f"📈 *Macro (1H):* {tendencia_macro} | *VWAP (15m):* {sesgo_15m}\n"
                           f"📊 *OBI (20 niveles):* {obi:+.2f}\n"
                           f"🐋 *On-chain:* {flujo}\n\n"
                           f"🎯 *ENTRADA:* ${entry:,.2f}\n"
                           f"🛑 *STOP LOSS:* ${sl:,.2f}\n"
                           f"💰 *TP1:* ${tp1:,.2f} (Cierra 50%)\n"
                           f"💰 *TP2:* ${tp2:,.2f} (Deja correr)\n\n"
                           f"⚖️ *Ratio R:B:* 1:1.5 y 1:3\n"
                           f"⚠️ *Gestión:* Arriesga solo el 1% de tu capital.")
                enviar_telegram(mensaje)

            # --- LÓGICA DE VENTA (UPTHRUST) ---
            elif (ultima['high'] > ultima['resistencia'] and ultima['close'] < ultima['resistencia']
                  and tendencia_macro == "BAJISTA" and sesgo_15m == "BAJISTA"
                  and obi < 0.3 and not onchain_ok):

                entry = precio
                sl = ultima['high'] * 1.002
                riesgo = sl - entry
                tp1 = entry - (riesgo * 1.5)
                tp2 = entry - (riesgo * 3.0)

                mensaje = (f"🔻 *SETUP A+ (VENTA)* 🔻\n\n"
                           f"🪙 *Activo:* {symbol}\n"
                           f"⏱ *Entrada:* 5m\n"
                           f"📉 *Macro (1H):* {tendencia_macro} | *VWAP (15m):* {sesgo_15m}\n"
                           f"📊 *OBI (20 niveles):* {obi:+.2f}\n"
                           f"🐋 *On-chain:* {flujo}\n\n"
                           f"🎯 *ENTRADA:* ${entry:,.2f}\n"
                           f"🛑 *STOP LOSS:* ${sl:,.2f}\n"
                           f"💰 *TP1:* ${tp1:,.2f} (Cierra 50%)\n"
                           f"💰 *TP2:* ${tp2:,.2f} (Deja correr)\n\n"
                           f"⚖️ *Ratio R:B:* 1:1.5 y 1:3\n"
                           f"⚠️ *Gestión:* Arriesga solo el 1% de tu capital.")
                enviar_telegram(mensaje)
                
            # --- NUEVA ALERTA: VOLUMEN CLÍMAX ---
            elif ultima['volume'] > (ultima['vol_ma'] * 2.5):
                mensaje = (f"⚠️ *VOLUMEN CLÍMAX DETECTADO* ⚠️\n\n"
                           f"🪙 *Activo:* {symbol}\n"
                           f"⏱ *Temporalidad:* 5m\n"
                           f"💰 *Precio:* ${precio:,.2f}\n"
                           f"📊 *Sesgo actual:* {sesgo_15m}\n\n"
                           f"👀 *Atención:* Los grandes están moviendo el mercado. Prepárate para una posible ruptura o trampa.")
                enviar_telegram(mensaje)

        except:
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
# 🦅 FUNCIÓN DEL ESCÁNER TOP 50
# ==========================================
def ejecutar_escaner_top50(tf):
    exchange = ccxt.okx()
    try:
        tickers = exchange.fetch_tickers()
    except:
        return pd.DataFrame()

    mercados_usdt = []
    for sym, data in tickers.items():
        if sym.endswith('/USDT') and data.get('quoteVolume') is not None:
            mercados_usdt.append((sym, data['quoteVolume'], data.get('percentage', 0)))
            
    top50 = sorted(mercados_usdt, key=lambda x: x[1], reverse=True)[:50]
    
    resultados = []
    progress_bar = st.progress(0)
    status_text = st.empty()
    
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
            
            if ultima['low'] < ultima['soporte'] and ultima['close'] > ultima['soporte']:
                señal = "🟢 SPRING"
            elif ultima['high'] > ultima['resistencia'] and ultima['close'] < ultima['resistencia']:
                señal = "🔴 UPTHRUST"
            elif ultima['volume'] > (ultima['vol_ma'] * 2.5):
                señal = "⚠️ VOL CLÍMAX"
                
            resultados.append({"Par": sym, "Precio": precio, "Sesgo": sesgo, "Señal": señal, "Cambio 24h %": cambio})
            time.sleep(0.05)
        except:
            pass
        progress_bar.progress((i + 1) / 50)
    
    status_text.empty()
    progress_bar.empty()
    
    df_final = pd.DataFrame(resultados)
    if not df_final.empty:
        df_final['Orden'] = df_final['Señal'].apply(lambda x: 1 if x != "NEUTRAL" else 2)
        df_final = df_final.sort_values(by=['Orden', 'Cambio 24h %'], ascending=[True, False]).drop(columns=['Orden'])
    return df_final

# ==========================================
# 📊 INTERFAZ GRÁFICA
# ==========================================
st.markdown("<h1 style='text-align: center; color: #00FFAA;'>🏦 INSTITUTIONAL QUANT TERMINAL</h1>", unsafe_allow_html=True)
st.markdown("<p style='text-align: center; color: gray;'>Autopilot ON | Wyckoff + OBI + On-chain</p>", unsafe_allow_html=True)
st.markdown("---")

# --- BARRA LATERAL ---
st.sidebar.header("🌐 Configuración del Gráfico")
activos = ["BTC/USDT","ETH/USDT","SOL/USDT","BNB/USDT","XRP/USDT",
           "ADA/USDT","DOGE/USDT","AVAX/USDT","DOT/USDT","MATIC/USDT"]
symbol = st.sidebar.selectbox("Ver Gráfico de:", activos)
timeframe = st.sidebar.selectbox("Temporalidad", ["1m","5m","15m","1h","4h"])

st.sidebar.markdown("---")
st.sidebar.header("🔔 Alertas")
activar_alertas = st.sidebar.checkbox("Activar Alertas Manuales en Telegram", value=True)
if st.sidebar.button("🚀 Probar Alerta Telegram"):
    enviar_telegram("✅ *SISTEMA EN LÍNEA:* Terminal Ballena + OBI + On-chain.")
    st.sidebar.success("¡Mensaje enviado!")

st.sidebar.info("🤖 Autopiloto escaneando: Macro 1H + VWAP 15m + OBI + On-chain + Wyckoff 5m.")

# 🔍 BOTÓN DEL ESCÁNER TOP 50
st.sidebar.markdown("---")
st.sidebar.header("🦅 Escáner Global (Top 50)")
if st.sidebar.button("🔍 Escanear Top 50 Mercados"):
    with st.spinner("Analizando los 50 mercados con más volumen..."):
        st.session_state['scan_results'] = ejecutar_escaner_top50(timeframe)

# --- MOSTRAR RESULTADOS DEL ESCÁNER (Si existen) ---
if 'scan_results' in st.session_state and not st.session_state['scan_results'].empty:
    st.success("✅ Resultados del Escáner Top 50 (Ordenados por oportunidad):")
    st.dataframe(st.session_state['scan_results'], use_container_width=True)
    st.markdown("---")
elif 'scan_results' in st.session_state and st.session_state['scan_results'].empty:
    st.warning("El escáner no encontró oportunidades o hubo un error de conexión.")
    st.markdown("---")

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
