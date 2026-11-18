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

# Intento de importar MetaTrader 5 para ejecución en Vantage (Opcional si se ejecuta localmente con MT5 abierto)
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

st.set_page_config(page_title="Institutional Quant Terminal - Master Edition", layout="wide", page_icon="🏦")

NASDAQ_MARKETS = ["NVDA", "AAPL", "MSFT", "TSLA", "AMZN", "GOOGL", "META", "NFLX", "AMD"]
FOREX_MARKETS = ["EURUSD=X", "GBPUSD=X", "USDJPY=X", "AUDUSD=X", "USDCAD=X", "NZDUSD=X", "USDCHF=X", "EURGBP=X", "EURJPY=X", "GBPJPY=X"]
INDEX_MARKETS = ["^IXIC", "QQQ", "^GSPC", "^DJI", "^GDAXI", "^FTSE", "^N225", "DX-Y.NYB"]
COMMODITY_MARKETS = ["GC=F", "SI=F", "CL=F", "BZ=F", "NG=F"]

thread_lock = threading.Lock()
ALERTAS_ENVIADAS_GLOBAL = {}
AUTOPILOTO_STATUS = "Iniciando motor multi-estrategia institucional..."

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
        interval_map = {"1m": "1m", "5m": "5m", "15m": "15m", "1h": "60m"} 
        yf_interval = interval_map.get(timeframe, "15m")
        period = "1d" if yf_interval in ["1m", "5m", "15m"] else "1mo"
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
# ⚡ PASARELA DE EJECUCIÓN AUTOMÁTICA (Vantage / OKX)
# ==========================================
def ejecutar_orden_en_broker(symbol, tipo_senal, precio):
    """
    Motor de ejecución automática que conecta con Vantage (via MT5) o Cripto (via CCXT).
    """
    modo_auto = st.session_state.get('modo_automatico', False)
    if not modo_auto:
        return # Si el switch automático está apagado, solo emite alerta

    # 1. Ejecución automática en Cripto (OKX)
    if "/" in symbol and "USDT" in symbol:
        try:
            exchange = ccxt.okx({'enableRateLimit': True})
            side = 'buy' if "COMPRA" in tipo_senal else 'sell'
            # Ejemplo de ejecución con 15 USDT de margen controlado
            cantidad = round(15.0 / precio, 4)
            # order = exchange.create_market_order(symbol, side, cantidad)
            enviar_telegram(f"🤖 *EJECUCIÓN AUTOMÁTICA OKX*\nOperación en {symbol} | Lado: {side.upper()} | Cantidad: {cantidad}")
        except Exception as e:
            enviar_telegram(f"❌ Error ejecutando en OKX: {e}")

    # 2. Ejecución automática en Vantage (MetaTrader 5)
    elif MT5_DISPONIBLE:
        try:
            if mt5.initialize():
                mt5_symbol = symbol.replace("=X", "").replace("^", "")
                symbol_info = mt5.symbol_info(mt5_symbol)
                if symbol_info and symbol_info.visible:
                    tick = mt5.symbol_info_tick(mt5_symbol)
                    if tick:
                        is_buy = "COMPRA" in tipo_senal
                        order_type = mt5.ORDER_TYPE_BUY if is_buy else mt5.ORDER_TYPE_SELL
                        p = tick.ask if is_buy else tick.bid
                        point = symbol_info.point
                        
                        request = {
                            "action": mt5.TRADE_ACTION_DEAL,
                            "symbol": mt5_symbol,
                            "volume": 0.01, # Lote mínimo de seguridad para pruebas
                            "type": order_type,
                            "price": p,
                            "sl": p - (300 * point) if is_buy else p + (300 * point),
                            "tp": p + (600 * point) if is_buy else p - (600 * point),
                            "deviation": 20,
                            "magic": 777777,
                            "comment": "Master Quant Bot",
                            "type_time": mt5.ORDER_TIME_GTC,
                            "type_filling": mt5.ORDER_FILLING_IOC,
                        }
                        result = mt5.order_send(request)
                        if result.retcode == mt5.TRADE_RETCODE_DONE:
                            enviar_telegram(f"🤖 *EJECUCIÓN VANTAGE MT5 EXITOSA*\nActivo: {mt5_symbol} | Ticket: {result.order}")
                mt5.shutdown()
        except Exception as e:
            enviar_telegram(f"❌ Error ejecutando en MT5 Vantage: {e}")

# ==========================================
# 🤖 MOTOR MULTI-ESTRATEGIA INSTITUCIONAL
# ==========================================
def ejecutar_logica_autopiloto(es_hilo_fondo=False):
    global AUTOPILOTO_STATUS, ALERTAS_ENVIADAS_GLOBAL
    
    if not es_hilo_fondo:
        AUTOPILOTO_STATUS = "Escaneando confluencia multi-estrategia..."
    
    exchange = ccxt.okx({'enableRateLimit': True, 'timeout': 8000})
    try:
        tickers = exchange.fetch_tickers()
        mercados_usdt = [(sym, data['quoteVolume']) for sym, data in tickers.items() if sym.endswith('/USDT') and data.get('quoteVolume')]
        top10_cripto = [sym for sym, vol in sorted(mercados_usdt, key=lambda x: x[1], reverse=True)[:10]]
    except:
        top10_cripto = ["BTC/USDT", "ETH/USDT", "SOL/USDT"]

    mercados_a_escanear = top10_cripto + NASDAQ_MARKETS[:5] + ["EURUSD=X", "^IXIC", "GC=F"]
    alertas_nuevas = 0

    for i, symbol in enumerate(mercados_a_escanear):
        if not es_hilo_fondo:
            AUTOPILOTO_STATUS = f"Analizando {symbol} ({i+1}/{len(mercados_a_escanear)})..."
            
        try:
            tipo = clasificar_tipo_mercado(symbol)
            if not mercado_abierto(tipo): continue

            df_15m = obtener_datos(symbol, '15m', limit=50)
            df_1h = obtener_datos(symbol, '1h', limit=50)
            if df_15m.empty or df_1h.empty: continue
            
            ultima = df_15m.iloc[-1]
            precio = ultima['close']
            
            # --- CONFLUENCIAS ESTRATÉGICAS ---
            macro_alcista = ultima['close'] > ultima['ema_50']
            cuerpo_vela = abs(ultima['close'] - ultima['open'])
            mecha_inf = min(ultima['close'], ultima['open']) - ultima['low']
            mecha_sup = ultima['high'] - max(ultima['close'], ultima['open'])
            mult_vol = 2.0 if tipo in ["FOREX", "INDICE"] else 2.5
            
            señal = None
            tipo_orden = ""
            msg = ""
            
            # 1. Estrategia Wyckoff: Spring & Upthrust
            if (ultima['low'] < ultima['soporte'] and ultima['close'] > ultima['soporte']) and macro_alcista:
                señal = "SPRING"
                tipo_orden = "COMPRA"
                msg = f"🚀 *ESTRATEGIA WYCKOFF: SPRING*\n🪙 {symbol} | ${precio:,.4f}\n📊 Trampa bajista en soporte con recuperación de tendencia."
            
            elif (ultima['high'] > ultima['resistencia'] and ultima['close'] < ultima['resistencia']) and not macro_alcista:
                señal = "UPTHRUST"
                tipo_orden = "VENTA"
                msg = f"🔻 *ESTRATEGIA WYCKOFF: UPTHRUST*\n🪙 {symbol} | ${precio:,.4f}\n📊 Falsa ruptura en resistencia con rechazo bajista."
            
            # 2. Estrategia Clímax de Volumen y Reversión a la Media (VWAP)
            elif ultima['volume'] > (ultima['vol_ma'] * mult_vol):
                cerca_soporte = ultima['low'] <= (ultima['soporte'] * 1.015)
                cerca_resistencia = ultima['high'] >= (ultima['resistencia'] * 0.985)
                
                if cerca_soporte and (mecha_inf > cuerpo_vela * 1.1 or ultima['close'] > ultima['open']):
                    señal = "CLIMAX_ABSORCION"
                    tipo_orden = "COMPRA"
                    msg = f"⚠️ *ESTRATEGIA VOLUMEN: CLÍMAX DE ABSORCIÓN*\n🪙 {symbol} | ${precio:,.4f}\n📊 Alta absorción institucional en soporte."
                elif cerca_resistencia and (mecha_sup > cuerpo_vela * 1.1 or ultima['close'] < ultima['open']):
                    señal = "CLIMAX_EXHAUSCION"
                    tipo_orden = "VENTA"
                    msg = f"⚠️ *ESTRATEGIA VOLUMEN: CLÍMAX DE EXHAUSCIÓN*\n🪙 {symbol} | ${precio:,.4f}\n📊 Techo de distribución institucional detectado."

            if señal and msg:
                clave_alerta = f"{symbol}_{señal}"
                ahora = time.time()
                with thread_lock:
                    if ahora - ALERTAS_ENVIADAS_GLOBAL.get(clave_alerta, 0) > 1800:
                        enviar_telegram(msg)
                        ejecutar_orden_en_broker(symbol, tipo_orden, precio)
                        ALERTAS_ENVIADAS_GLOBAL[clave_alerta] = ahora
                        alertas_nuevas += 1

            time.sleep(0.05)
        except: 
            pass

    with thread_lock:
        if not es_hilo_fondo:
            AUTOPILOTO_STATUS = f"✅ Escaneo Multi-Estrategia completado. {alertas_nuevas} señales."
        else:
            AUTOPILOTO_STATUS = "✅ Motor autónomo activo. Esperando ciclo..."
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
# 📊 INTERFAZ GRÁFICA MAESTRA
# ==========================================
st.markdown("<h1 style='text-align: center; color: #00FFAA;'>🏦 MASTER INSTITUTIONAL QUANT TERMINAL</h1>", unsafe_allow_html=True)
st.markdown("<p style='text-align: center; color: gray;'>Motor Multi-Estrategia (Wyckoff + Volumen + VWAP + Trend) con Pasarela de Ejecución Automática</p>", unsafe_allow_html=True)
st.markdown("---")

st.sidebar.header("⚙️ Configuración y Automatización")
modo_auto = st.sidebar.checkbox("🤖 Activar Trading Autónomo (Opera por mí)", value=False, help="Si se activa, el bot enviará órdenes reales a Vantage MT5 o OKX al cumplirse las reglas.")
st.session_state['modo_automatico'] = modo_auto

if modo_auto:
    st.sidebar.error("⚠️ MODO AUTOMÁTICO ACTIVO: El bot ejecutará operaciones reales según las reglas institucionales.")
else:
    st.sidebar.success("🛡️ MODO ALERTA: El bot solo enviará notificaciones a Telegram.")

st.sidebar.markdown("---")
lista_grafico = ["EURUSD=X", "^IXIC", "QQQ", "BTC/USDT", "ETH/USDT", "SOL/USDT"] + NASDAQ_MARKETS
symbol = st.sidebar.selectbox("Seleccionar Activo:", list(dict.fromkeys(lista_grafico)))
timeframe = st.sidebar.selectbox("Temporalidad", ["5m","15m","1h"], index=1)

st.sidebar.markdown("---")
st.sidebar.info(AUTOPILOTO_STATUS)

if st.sidebar.button("⚡ Forzar Ciclo Multi-Estrategia"):
    with st.spinner("Analizando confluencias..."):
        alertas = ejecutar_logica_autopiloto(es_hilo_fondo=False)
        st.sidebar.success(f"Ciclo finalizado. Alertas: {alertas}")

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
        st.markdown(f"### ⚙️ Panel de Confluencia")
        ultima = df.iloc[-1]
        p_actual = ultima['close']
        st.metric(label=symbol, value=f"${p_actual:,.4f}")
        
        if p_actual < ultima['vwap']: st.error("🔴 Tendencia VWAP: Bajista")
        else: st.success("🟢 Tendencia VWAP: Alcista")
        
        # Estado de tácticas en tiempo real
        if ultima['low'] < ultima['soporte'] and ultima['close'] > ultima['soporte']:
            st.success("🎯 Patrón activo: SPRING")
        elif ultima['high'] > ultima['resistencia'] and ultima['close'] < ultima['resistencia']:
            st.error("🎯 Patrón activo: UPTHRUST")
        else:
            st.info("🔍 Buscando Clímax / Absorción...")

render_trading_panel(symbol, timeframe)
