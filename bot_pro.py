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
ALL_SYMBOLS = NASDAQ_MARKETS + DOLLAR_MARKETS

# 🔥 VARIABLE GLOBAL PARA EL HILO (Evita el bug de Streamlit)
ALERTAS_ENVIADAS_GLOBAL = {}
AUTOPILOTO_STATUS = "Iniciando motor de escaneo..."

def enviar_telegram(mensaje):
    url = f"https://telegram.org{TELEGRAM_TOKEN}/sendMessage"
    payload = {"chat_id": TELEGRAM_CHAT_ID, "text": mensaje, "parse_mode": "Markdown"}
    try: requests.post(url, json=payload)
    except: pass

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
        exchange = ccxt.okx()
        try:
            ohlcv = exchange.fetch_ohlcv(symbol, timeframe, limit=limit)
            df = pd.DataFrame(ohlcv, columns=['timestamp', 'open', 'high', 'low', 'close', 'volume'])
            df['timestamp'] = pd.to_datetime(df['timestamp'], unit='ms')
        except: return pd.DataFrame()
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
        except: return pd.DataFrame()

    if not df.empty:
        df['vwap'] = calcular_vwap(df)
        df['soporte'] = df['low'].rolling(window=20).min()
        df['resistencia'] = df['high'].rolling(window=20).max()
        df['vol_ma'] = df['volume'].rolling(window=20).mean()
    return df

# ==========================================
# 🤖 LÓGICA DEL AUTOPILOTO (Estrategia Corregida)
# ==========================================
def ejecutar_logica_autopiloto(es_hilo_fondo=False):
    global AUTOPILOTO_STATUS, ALERTAS_ENVIADAS_GLOBAL
    
    if not es_hilo_fondo:
        AUTOPILOTO_STATUS = "Buscando los 50 mercados..."
    
    exchange = ccxt.okx()
    try:
        tickers = exchange.fetch_tickers()
        mercados_usdt = [(sym, data['quoteVolume']) for sym, data in tickers.items() if sym.endswith('/USDT') and data.get('quoteVolume')]
        top50_cripto = [sym for sym, vol in sorted(mercados_usdt, key=lambda x: x[1], reverse=True)[:50]]
    except:
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
            if len(df_5m) < 25 or len(df_1h) < 25: continue
            
            # CAMBIO ESTRATEGIA: Leer última vela cerrada (-2) en lugar de la vela en curso (-1)
            ultima = df_5m.iloc[-2]
            precio = df_5m.iloc[-1]['close']
            macro = "ALCISTA" if df_1h['close'].iloc[-1] > df_1h['close'].ewm(span=50).mean().iloc[-1] else "BAJISTA"
            
            señal = None
            msg = ""
            
            if (ultima['low'] <= ultima['soporte'] and ultima['close'] > ultima['soporte']) and macro == "ALCISTA":
                señal = "SPRING"
                msg = f"🚀 *AUTOPILOTO: COMPRA ({tipo})*\n🪙 {symbol} | ${precio:,.4f}"
            elif (ultima['high'] >= ultima['resistencia'] and ultima['close'] < ultima['resistencia']) and macro == "BAJISTA":
                señal = "UPTHRUST"
                msg = f"🔻 *AUTOPILOTO: VENTA ({tipo})*\n🪙 {symbol} | ${precio:,.4f}"
            elif tipo != "FOREX" and ultima['volume'] > (ultima['vol_ma'] * 2.5):
                # CAMBIO ESTRATEGIA: Validar que el clímax ocurra en soporte o resistencia e invertir giro
                en_suelo = ultima['low'] <= ultima['soporte']
                en_techo = ultima['high'] >= ultima['resistencia']
                
                if en_suelo and ultima['close'] < ultima['open']:
                    señal = "CLIMAX_COMPRA"
                    msg = (
                        f"⚠️ *AUTOPILOTO: SELLING CLIMAX ({tipo})*\n"
                        f"🪙 {symbol} | ${precio:,.4f}\n"
                        f"📊 *Posible Giro:* COMPRA (Alcista) 🟢\n"
                        f"🛑 *Mínimo Vela:* ${ultima['low']:,.4f}"
                    )
                elif en_techo and ultima['close'] > ultima['open']:
                    señal = "CLIMAX_VENTA"
                    msg = (
                        f"⚠️ *AUTOPILOTO: BUYING CLIMAX ({tipo})*\n"
                        f"🪙 {symbol} | ${precio:,.4f}\n"
                        f"📊 *Posible Giro:* VENTA (Bajista) 🔴\n"
                        f"🛑 *Máximo Vela:* ${ultima['high']:,.4f}"
                    )

            if señal and msg:
                clave_alerta = f"{symbol}_{señal}"
                ahora = time.time()
                ultimo_envio = ALERTAS_ENVIADAS_GLOBAL.get(clave_alerta, 0)
                
                # Anti-spam: 30 min
                if ahora - ultimo_envio > 1800:
                    enviar_telegram(msg)
                    ALERTAS_ENVIADAS_GLOBAL[clave_alerta] = ahora
                    alertas_nuevas += 1

            time.sleep(0.3) # Pausa para no saturar OKX
        except: pass

    if not es_hilo_fondo:
        AUTOPILOTO_STATUS = f"✅ Completado. {alertas_nuevas} alertas enviadas."
    else:
        AUTOPILOTO_STATUS = "✅ Escaneo de fondo completado. Durmiendo 5 min..."
        return alertas_nuevas

# ==========================================
# 🧵 HILO DE FONDO (Autopiloto real)
# ==========================================
@st.cache_resource
def iniciar_hilo_autopiloto():
    def loop():
        while True:
            ejecutar_logica_autopiloto(es_hilo_fondo=True)
            time.sleep(300) # 5 minutos
    thread = threading.Thread(target=loop, daemon=True)
    thread.start()
    return thread

iniciar_hilo_autopiloto()

# ==========================================
# 🦅 ESCÁNER TOP 50 (Manual)
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
            
            # Misma lógica de vela cerrada (-2) aplicada al escáner manual
            ultima = df.iloc[-2]
            precio_actual = df.iloc[-1]['close']
            
            if ultima['volume'] > (ultima['vol_ma'] * 2.5):
                if ultima['low'] <= ultima['soporte'] and ultima['close'] < ultima['open']:
                    resultados.append({"Par": sym, "Precio": precio_actual, "Señal": "SELLING CLIMAX (Giro Alcista) 🟢", "Volumen 24h": vol})
                elif ultima['high'] >= ultima['resistencia'] and ultima['close'] > ultima['open']:
                    resultados.append({"Par": sym, "Precio": precio_actual, "Señal": "BUYING CLIMAX (Giro Bajista) 🔴", "Volumen 24h": vol})
        except:
            pass
        progress_bar.progress((i + 1) / 50)
        time.sleep(0.1)
        
    status_text.empty()


