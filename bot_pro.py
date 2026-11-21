import streamlit as st
import ccxt
import yfinance as yf
import pandas as pd
import numpy as np
import plotly.graph_objects as go
from plotly.subplots import make_subplots
import requests
import xml.etree.ElementTree as ET
import time
import threading
from datetime import datetime

# ==========================================
# âš™ï¸ CONFIGURACIÃ“N GLOBAL & LISTA CERRADA ESTRICTA ($10 USD: FOREX + CRIPTO)
# ==========================================
TELEGRAM_TOKEN = "9148434925:AAH_AuswS-GGPHadFBwcrDOUUIwBFbXvtDs"
TELEGRAM_CHAT_ID = "1496321437"

st.set_page_config(page_title="Institutional Quant Terminal - Forex & Crypto", layout="wide", page_icon="ðŸ¦")

# ÃšNICOS MERCADOS HABILITADOS PARA ALERTAS Y ESCANEO
CRYPTO_MARKETS = ["BTC/USDT", "ETH/USDT", "BNB/USDT"]
FOREX_MARKETS = [
    "EURUSD=X", "GBPUSD=X", "USDJPY=X", "USDCHF=X", "AUDUSD=X",
    "USDCAD=X", "NZDUSD=X", "EURJPY=X", "GBPJPY=X", "EURGBP=X",
    "XAUUSD=X", "XAUUSD"
]
ALL_TRACKED_SYMBOLS = list(dict.fromkeys(CRYPTO_MARKETS + FOREX_MARKETS))


def _crear_exchange(ex_id):
    try:
        cls = getattr(ccxt, ex_id)
        return cls({"enableRateLimit": True})
    except Exception:
        return None


EXCHANGE_OKX = _crear_exchange("okx")
EXCHANGE_BINANCE = _crear_exchange("binance")

ALERTAS_ENVIADAS_GLOBAL = {}
CACHE_DATOS_GLOBAL = {}
CACHE_NOTICIAS_GLOBAL = {"ts": 0, "items": []}
CACHE_LOCK = threading.Lock()
AUTOPILOTO_STATUS = "Motor Forex & Cripto activo: Escaneando cada 30s..."


# ==========================================
# ðŸ“° MÃ“DULO DE NOTICIAS DE MERCADO EN VIVO (RSS PÃšBLICOS)
# ==========================================
def obtener_noticias_en_vivo(ttl_segundos=180):
    """Obtiene titulares recientes de mercados (Forex, Oro, Cripto, Macro) vÃ­a RSS sin API key."""
    ahora = time.time()
    with CACHE_LOCK:
        if CACHE_NOTICIAS_GLOBAL["items"] and (ahora - CACHE_NOTICIAS_GLOBAL["ts"] < ttl_segundos):
            return CACHE_NOTICIAS_GLOBAL["items"]

    feeds = [
        ("Investing Forex", "https://www.investing.com/rss/news_1.rss"),
        ("Investing Commodities/Oro", "https://www.investing.com/rss/news_11.rss"),
        ("Cointelegraph", "https://cointelegraph.com/rss"),
        ("FXStreet", "https://www.fxstreet.com/rss/news"),
    ]
    noticias = []
    headers = {"User-Agent": "Mozilla/5.0"}

    for fuente, url in feeds:
        try:
            resp = requests.get(url, headers=headers, timeout=5)
            if resp.status_code != 200:
                continue
            root = ET.fromstring(resp.content)
            for item in root.findall(".//item")[:5]:
                titulo = (item.findtext("title") or "").strip()
                link = (item.findtext("link") or "").strip()
                pub_date = (item.findtext("pubDate") or "").strip()
                if titulo:
                    noticias.append({
                        "fuente": fuente,
                        "titulo": titulo,
                        "link": link,
                        "fecha": pub_date[:22] if pub_date else "Reciente"
                    })
        except Exception:
            continue

    if noticias:
        with CACHE_LOCK:
            CACHE_NOTICIAS_GLOBAL["ts"] = ahora
            CACHE_NOTICIAS_GLOBAL["items"] = noticias

    return noticias


# ==========================================
# ðŸ§  FUNCIONES MATEMÃTICAS, AMT (VOLUME PROFILE) & SMART MONEY
# ==========================================
def enviar_telegram(mensaje):
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    payload = {"chat_id": TELEGRAM_CHAT_ID, "text": mensaje, "parse_mode": "Markdown"}
    try:
        requests.post(url, json=payload, timeout=5)
    except Exception:
        pass


def _normalizar_timestamp(serie):
    ts = pd.to_datetime(serie)
    if getattr(ts.dt, "tz", None) is not None:
        ts = ts.dt.tz_convert(None)
    return ts


def obtener_umbral_adx(symbol):
    if symbol in ("XAUUSD", "XAUUSD=X"):
        return 40.0
    if symbol in CRYPTO_MARKETS:
        return 38.0
    if symbol in FOREX_MARKETS:
        return 35.0
    return 35.0


def calcular_vwap(df):
    if df.empty or "volume" not in df.columns or df["volume"].sum() == 0:
        return df["close"] if not df.empty else pd.Series(dtype=float)
    tp = (df["high"] + df["low"] + df["close"]) / 3.0
    return (df["volume"] * tp).cumsum() / df["volume"].cumsum()


def calcular_adx(df, period=14):
    if len(df) < period + 2:
        return 25.0
    try:
        d = df.copy()
        d["up_move"] = d["high"] - d["high"].shift()
        d["down_move"] = d["low"].shift() - d["low"]
        d["plus_dm"] = np.where((d["up_move"] > d["down_move"]) & (d["up_move"] > 0), d["up_move"], 0.0)
        d["minus_dm"] = np.where((d["down_move"] > d["up_move"]) & (d["down_move"] > 0), d["down_move"], 0.0)
        d["tr"] = np.maximum(
            d["high"] - d["low"],
            np.maximum(abs(d["high"] - d["close"].shift()), abs(d["low"] - d["close"].shift()))
        )
        d["atr"] = d["tr"].ewm(span=period, adjust=False).mean()
        plus_di = 100.0 * (d["plus_dm"].ewm(span=period, adjust=False).mean() / d["atr"].replace(0, np.nan))
        minus_di = 100.0 * (d["minus_dm"].ewm(span=period, adjust=False).mean() / d["atr"].replace(0, np.nan))
        dx = 100.0 * (abs(plus_di - minus_di) / (plus_di + minus_di).replace(0, np.nan))
        adx_series = dx.ewm(span=period, adjust=False).mean()
        val = adx_series.iloc[-1]
        return 25.0 if pd.isna(val) else float(val)
    except Exception:
        return 25.0


def calcular_cvd_proxy(df):
    rango = (df["high"] - df["low"]).replace(0, np.nan)
    delta_ratio = ((df["close"] - df["open"]) / rango).fillna(0.0)
    delta_vol = df["volume"] * delta_ratio
    return delta_vol.cumsum()


def calcular_volume_profile_amt(df, bins=24):
    """
    Calcula POC, VAH, VAL y LVN (Low Volume Nodes) segÃºn Auction Market Theory (Fabio Valentino).
    """
    if df.empty or len(df) < 10:
        return None
    p_min = float(df["low"].min())
    p_max = float(df["high"].max())
    if p_max <= p_min:
        return None

    edges = np.linspace(p_min, p_max, bins + 1)
    vol_por_bin = np.zeros(bins)
    tp = ((df["high"] + df["low"] + df["close"]) / 3.0).values
    vols = df["volume"].values

    indices = np.clip(np.digitize(tp, edges) - 1, 0, bins - 1)
    for idx, v in zip(indices, vols):
        vol_por_bin[idx] += float(v)

    total_vol = vol_por_bin.sum()
    if total_vol <= 0:
        return None

    poc_idx = int(np.argmax(vol_por_bin))
    poc_price = float((edges[poc_idx] + edges[poc_idx + 1]) / 2.0)

    # Value Area (70% del volumen alrededor del POC)
    target_vol = total_vol * 0.70
    acc_vol = vol_por_bin[poc_idx]
    low_idx, high_idx = poc_idx, poc_idx

    while acc_vol < target_vol and (low_idx > 0 or high_idx < bins - 1):
        vol_down = vol_por_bin[low_idx - 1] if low_idx > 0 else -1
        vol_up = vol_por_bin[high_idx + 1] if high_idx < bins - 1 else -1
        if vol_up >= vol_down and high_idx < bins - 1:
            high_idx += 1
            acc_vol += vol_por_bin[high_idx]
        elif low_idx > 0:
            low_idx -= 1
            acc_vol += vol_por_bin[low_idx]
        else:
            break

    val_price = float(edges[low_idx])
    vah_price = float(edges[high_idx + 1])

    # Nodos de Bajo Volumen (LVN): bins por debajo del 35% de la media
    media_vol = vol_por_bin.mean()
    lvn_levels = [
        float((edges[i] + edges[i + 1]) / 2.0)
        for i in range(bins)
        if vol_por_bin[i] < media_vol * 0.35
    ]

    precio_actual = float(df["close"].iloc[-1])
    estado = "OUT OF BALANCE (Tendencia)" if (precio_actual > vah_price or precio_actual < val_price) else "BALANCE (ConsolidaciÃ³n)"

    return {
        "poc": poc_price,
        "vah": vah_price,
        "val": val_price,
        "lvn": lvn_levels,
        "estado": estado
    }


def detectar_exhaustacion(df):
    if len(df) < 22:
        return False
    rango_actual = df["high"].iloc[-1] - df["low"].iloc[-1]
    rangos_previos = (df["high"].iloc[-21:-1] - df["low"].iloc[-21:-1])
    max_previo = rangos_previos.max()
    mov_previo = abs(df["close"].iloc[-2] - df["close"].iloc[-10])
    atr_aprox = rangos_previos.mean()
    return (rango_actual > max_previo * 1.15) and (mov_previo > atr_aprox * 3.0)


def detectar_fvg_3_velas(df):
    if len(df) < 4:
        return None
    for idx in (-1, -2):
        pos = len(df) + idx
        if pos < 2:
            continue
        v1_high = float(df["high"].iloc[pos - 2])
        v1_low = float(df["low"].iloc[pos - 2])
        v3_low = float(df["low"].iloc[pos])
        v3_high = float(df["high"].iloc[pos])

        if v3_low > v1_high:
            return {"tipo": "FVG_ALCISTA", "zona_low": v1_high, "zona_high": v3_low}
        if v3_high < v1_low:
            return {"tipo": "FVG_BAJISTA", "zona_low": v3_high, "zona_high": v1_low}
    return None


def evaluar_estrategia_unificada(df, symbol="", sesgo_htf="NEUTRO", vp_info=None):
    """
    Motor integrado:
    - AMT / Fabio Valentino: Estado Balance vs Out of Balance, POC/VAH/VAL, CVD.
    - El Sensei Trading: Order Block, FVG/Imbalance, Descuento Fibonacci >= 50%, ConfirmaciÃ³n Multitemporal (4H).
    - Filtro de ExhaustaciÃ³n (Larry Williams) + ADX adaptativo.
    """
    if df.empty or len(df) < 25:
        return None

    ultima = df.iloc[-1]
    anterior = df.iloc[-2]
    precio = float(ultima["close"])
    adx_val = float(ultima.get("adx", 0.0))
    vwap_val = float(ultima.get("vwap", precio))
    ema_fast = float(ultima.get("ema_fast", precio))
    ema_slow = float(ultima.get("ema_slow", precio))

    adx_min = obtener_umbral_adx(symbol)
    if adx_val <= adx_min:
        return None

    vol_actual = float(ultima.get("volume", 0.0))
    vol_ma = float(ultima.get("vol_ma", 0.0))
    desbalance_vol = (vol_ma > 0) and (vol_actual > vol_ma * 1.35)

    cvd_actual = float(ultima.get("cvd", 0.0))
    cvd_prev = float(anterior.get("cvd", 0.0))
    cvd_alcista = cvd_actual > cvd_prev
    cvd_bajista = cvd_actual < cvd_prev

    es_exhaustacion = detectar_exhaustacion(df)
    fvg = detectar_fvg_3_velas(df)

    rangos_previos = (df["high"].iloc[-21:-1] - df["low"].iloc[-21:-1])
    atr_prom = float(rangos_previos.mean()) if len(rangos_previos) > 0 else 0.0
    atr_ref = atr_prom if (pd.notna(atr_prom) and atr_prom > 0) else max(abs(float(ultima["high"]) - float(ultima["low"])), precio * 0.0025)

    swing_low_ref = float(df["low"].iloc[-7:-1].min()) if len(df) >= 8 else float(ultima["low"])
    swing_high_ref = float(df["high"].iloc[-7:-1].max()) if len(df) >= 8 else float(ultima["high"])
    rr_mult = 2.5

    # Fibonacci Descuento (El Sensei: >= 50% de descuento sobre el rango)
    rango_max = float(df["high"].iloc[-20:].max())
    rango_min = float(df["low"].iloc[-20:].min())
    amplitud = max(rango_max - rango_min, 1e-9)
    descuento_compra = ((rango_max - precio) / amplitud) * 100.0
    descuento_venta = ((precio - rango_min) / amplitud) * 100.0

    ob_alcista = (anterior["close"] < anterior["open"]) and (ultima["close"] > ultima["open"]) and (precio > anterior["high"])
    ob_bajista = (anterior["close"] > anterior["open"]) and (ultima["close"] < ultima["open"]) and (precio < anterior["low"])

    out_of_balance = vp_info and ("OUT OF BALANCE" in vp_info["estado"])

    if es_exhaustacion:
        sl_exh = float(swing_low_ref - atr_ref * 0.5) if precio >= ultima["open"] else float(swing_high_ref + atr_ref * 0.5)
        return {
            "id_senal": "EXHAUSTION_GUARD",
            "estrategia": f"Freno ExhaustaciÃ³n (L. Williams) â€¢ ADX > {adx_min:.0f}",
            "direccion": "NO ENTRAR EN EXTREMO / ESPERAR RETROCESO â¸ï¸",
            "sl": sl_exh,
            "tp": vwap_val,
            "plan": (
                f"1) ADX en {adx_val:.1f} (>{adx_min:.0f}) con vela de exhaustaciÃ³n detectada: evita comprar techo o vender suelo.\n"
                f"2) Espera retroceso hacia VWAP (${vwap_val:,.4f}), POC o Order Block con descuento >=50%.\n"
                f"3) MantÃ©n disciplina y protege tu capital sin perseguir el movimiento."
            )
        }

    confluencias_long = 0
    motivos_long = []
    if precio >= vwap_val and ema_fast >= ema_slow:
        confluencias_long += 1
        motivos_long.append("Estructura/VWAP/EMA alcista")
    if cvd_alcista or desbalance_vol:
        confluencias_long += 1
        motivos_long.append("CVD/Volumen respalda compras")
    if ob_alcista or (fvg and fvg["tipo"] == "FVG_ALCISTA") or descuento_compra >= 50.0:
        confluencias_long += 1
        motivos_long.append(f"OB/FVG/Descuento Fib ({descuento_compra:.0f}%)")
    if sesgo_htf == "ALCISTA" or out_of_balance:
        confluencias_long += 1
        motivos_long.append("AlineaciÃ³n HTF/AMT Out-of-Balance")

    if confluencias_long >= 2 and precio >= vwap_val:
        base_low = min(swing_low_ref, float(ultima["low"]))
        sl_sugerido = float(base_low - atr_ref * 0.6)
        riesgo = max(abs(precio - sl_sugerido), atr_ref * 0.9)
        sl_sugerido = float(precio - riesgo)
        tp_sugerido = float(precio + riesgo * rr_mult)
        return {
            "id_senal": "CONFLUENCIA_LONG",
            "estrategia": f"Confluencia Conjunta ({confluencias_long}/4) â€¢ ADX>{adx_min:.0f} + AMT + OB/FVG",
            "direccion": "COMPRA (LONG) ðŸŸ¢",
            "sl": sl_sugerido,
            "tp": tp_sugerido,
            "plan": (
                f"1) Confluencias: {', '.join(motivos_long)} (ADX {adx_val:.1f}, HTF: {sesgo_htf}).\n"
                f"2) Valida retroceso hacia VWAP (${vwap_val:,.4f}), LVN o OB/FVG sin perseguir picos.\n"
                f"3) Stop Loss en ${sl_sugerido:,.4f} | Take Profit (1:{rr_mult:.1f}) en ${tp_sugerido:,.4f}."
            )
        }

    confluencias_short = 0
    motivos_short = []
    if precio <= vwap_val and ema_fast <= ema_slow:
        confluencias_short += 1
        motivos_short.append("Estructura/VWAP/EMA bajista")
    if cvd_bajista or desbalance_vol:
        confluencias_short += 1
        motivos_short.append("CVD/Volumen respalda ventas")
    if ob_bajista or (fvg and fvg["tipo"] == "FVG_BAJISTA") or descuento_venta >= 50.0:
        confluencias_short += 1
        motivos_short.append(f"OB/FVG/Descuento Fib ({descuento_venta:.0f}%)")
    if sesgo_htf == "BAJISTA" or out_of_balance:
        confluencias_short += 1
        motivos_short.append("AlineaciÃ³n HTF/AMT Out-of-Balance")

    if confluencias_short >= 2 and precio <= vwap_val:
        base_high = max(swing_high_ref, float(ultima["high"]))
        sl_sugerido = float(base_high + atr_ref * 0.6)
        riesgo = max(abs(sl_sugerido - precio), atr_ref * 0.9)
        sl_sugerido = float(precio + riesgo)
        tp_sugerido = float(precio - riesgo * rr_mult)
        return {
            "id_senal": "CONFLUENCIA_SHORT",
            "estrategia": f"Confluencia Conjunta ({confluencias_short}/4) â€¢ ADX>{adx_min:.0f} + AMT + OB/FVG",
            "direccion": "VENTA (SHORT) ðŸ”´",
            "sl": sl_sugerido,
            "tp": tp_sugerido,
            "plan": (
                f"1) Confluencias: {', '.join(motivos_short)} (ADX {adx_val:.1f}, HTF: {sesgo_htf}).\n"
                f"2) Valida retroceso hacia VWAP (${vwap_val:,.4f}), LVN o OB/FVG sin perseguir picos.\n"
                f"3) Stop Loss en ${sl_sugerido:,.4f} | Take Profit (1:{rr_mult:.1f}) en ${tp_sugerido:,.4f}."
            )
        }

    return None


# ==========================================
# ðŸ› ï¸ OBTENCIÃ“N DE DATOS CON CACHÃ‰ Y VALIDACIÃ“N REAL
# ==========================================
def obtener_datos(symbol, timeframe, limit=100, ttl_segundos=60):
    cache_key = f"{symbol}_{timeframe}_{limit}"
    ahora = time.time()

    with CACHE_LOCK:
        cached = CACHE_DATOS_GLOBAL.get(cache_key)
        if cached and (ahora - cached["ts"] < ttl_segundos) and cached.get("es_real", False):
            df_cached = cached["df"].copy()
            df_cached.attrs["es_real"] = True
            return df_cached

    df = pd.DataFrame()
    es_real = False

    if symbol in CRYPTO_MARKETS:
        for ex in [e for e in (EXCHANGE_OKX, EXCHANGE_BINANCE) if e is not None]:
            try:
                ohlcv = ex.fetch_ohlcv(symbol, timeframe, limit=limit)
                if ohlcv:
                    temp_df = pd.DataFrame(ohlcv, columns=["timestamp", "open", "high", "low", "close", "volume"])
                    temp_df["timestamp"] = _normalizar_timestamp(pd.to_datetime(temp_df["timestamp"], unit="ms"))
                    temp_df = temp_df.dropna(subset=["open", "high", "low", "close"]).tail(limit).copy()
                    if not temp_df.empty and float(temp_df["close"].iloc[-1]) > 0:
                        df = temp_df
                        es_real = True
                        break
            except Exception:
                continue
    elif symbol in FOREX_MARKETS:
        interval_map = {"5m": "5m", "15m": "15m", "1h": "60m", "4h": "60m"}
        yf_interval = interval_map.get(timeframe, "60m")
        periodos_a_probar = ["5d", "1mo"] if timeframe == "5m" else ["1mo", "3mo"]

        simbolos_yf = [symbol]
        if symbol in ("XAUUSD", "XAUUSD=X"):
            simbolos_yf = ["XAUUSD=X", "GC=F"]

        for sym_try in simbolos_yf:
            for periodo in periodos_a_probar:
                try:
                    hist = yf.Ticker(sym_try).history(period=periodo, interval=yf_interval)
                    if not hist.empty and "Close" in hist.columns:
                        temp_df = hist.reset_index()
                        time_col = "Datetime" if "Datetime" in temp_df.columns else ("Date" if "Date" in temp_df.columns else temp_df.columns[0])
                        temp_df.rename(columns={
                            time_col: "timestamp",
                            "Open": "open",
                            "High": "high",
                            "Low": "low",
                            "Close": "close",
                            "Volume": "volume"
                        }, inplace=True)
                        temp_df["timestamp"] = _normalizar_timestamp(temp_df["timestamp"])
                        temp_df["volume"] = temp_df["volume"].fillna(0)
                        temp_df = temp_df.dropna(subset=["open", "high", "low", "close"]).tail(limit).copy()
                        if not temp_df.empty and float(temp_df["close"].iloc[-1]) > 0:
                            df = temp_df
                            es_real = True
                            break
                except Exception:
                    continue
            if es_real:
                break

    if df.empty or not all(col in df.columns for col in ["open", "high", "low", "close", "volume", "timestamp"]):
        freq_map = {"5m": "5min", "15m": "15min", "1h": "1h", "4h": "4h"}
        fechas = pd.date_range(end=datetime.now(), periods=limit, freq=freq_map.get(timeframe, "15min"))
        base_val = 2650.0 if "XAU" in symbol else (1.085 if "EUR" in symbol else 150.0)
        rng = np.random.default_rng(abs(hash(symbol + timeframe)) % (2**32))
        returns = rng.normal(0.0001, 0.002, limit)
        prices = base_val * np.cumprod(1 + returns)

        df = pd.DataFrame({
            "timestamp": fechas,
            "open": prices * (1 + rng.normal(0, 0.0005, limit)),
            "high": prices * (1 + abs(rng.normal(0, 0.001, limit))),
            "low": prices * (1 - abs(rng.normal(0, 0.001, limit))),
            "close": prices,
            "volume": rng.integers(1000, 10000, limit)
        })
        es_real = False

    if not df.empty and len(df) > 5:
        df["vwap"] = calcular_vwap(df)
        window_val = min(20, len(df))
        df["soporte"] = df["low"].rolling(window=window_val).min()
        df["resistencia"] = df["high"].rolling(window=window_val).max()
        df["vol_ma"] = df["volume"].rolling(window=window_val).mean()
        df["ema_fast"] = df["close"].ewm(span=9, adjust=False).mean()
        df["ema_slow"] = df["close"].ewm(span=21, adjust=False).mean()
        df["adx"] = calcular_adx(df)
        df["cvd"] = calcular_cvd_proxy(df)

    df.attrs["es_real"] = es_real

    if es_real:
        with CACHE_LOCK:
            CACHE_DATOS_GLOBAL[cache_key] = {"ts": ahora, "df": df.copy(), "es_real": True}

    return df


def obtener_sesgo_htf(symbol):
    """DirecciÃ³n de temporalidad alta (4H/1H) segÃºn El Sensei Trading."""
    df_htf = obtener_datos(symbol, "4h", limit=60, ttl_segundos=180)
    if df_htf.empty or len(df_htf) < 20:
        return "NEUTRO"
    ultima = df_htf.iloc[-1]
    if float(ultima["close"]) > float(ultima["ema_slow"]) and float(ultima["ema_fast"]) > float(ultima["ema_slow"]):
        return "ALCISTA"
    if float(ultima["close"]) < float(ultima["ema_slow"]) and float(ultima["ema_fast"]) < float(ultima["ema_slow"]):
        return "BAJISTA"
    return "NEUTRO"


# ==========================================
# ðŸ¤– MOTOR EN FONDO (SOLO FOREX/METAL Y CRIPTO)
# ==========================================
def ejecutar_logica_autopiloto(es_hilo_fondo=False):
    global AUTOPILOTO_STATUS, ALERTAS_ENVIADAS_GLOBAL

    if not es_hilo_fondo:
        AUTOPILOTO_STATUS = "Ejecutando barrido Forex & Cripto..."

    temporalidades = ["5m", "15m", "1h"]
    alertas_nuevas = 0

    for symbol in ALL_TRACKED_SYMBOLS:
        tipo = "CRIPTO" if symbol in CRYPTO_MARKETS else "FOREX/METAL"
        sesgo_htf = obtener_sesgo_htf(symbol)

        for tf in temporalidades:
            if not es_hilo_fondo:
                AUTOPILOTO_STATUS = f"Analizando {symbol} [{tf}]..."

            try:
                df = obtener_datos(symbol, tf, limit=85, ttl_segundos=60)
                if df.empty or not df.attrs.get("es_real", False):
                    continue

                vp_info = calcular_volume_profile_amt(df)
                resultado = evaluar_estrategia_unificada(df, symbol=symbol, sesgo_htf=sesgo_htf, vp_info=vp_info)
                if resultado:
                    ultima = df.iloc[-1]
                    precio = float(ultima["close"])
                    adx_val = float(ultima.get("adx", 0.0))

                    msg = (
                        f"ðŸ¦ *SEÃ‘AL CONJUNTA UNIFICADA*\n"
                        f"ðŸŒ *Mercado:* `{tipo}` | *Activo:* `{symbol}` | *TF:* `{tf}` (HTF: `{sesgo_htf}`)\n"
                        f"ðŸ’² *Precio Real:* `${precio:,.4f}` | *ADX:* `{adx_val:.1f}`\n"
                        f"ðŸŽ¯ *Estrategia:* {resultado['estrategia']}\n"
                        f"ðŸ§­ *AcciÃ³n:* *{resultado['direccion']}*\n"
                        f"ðŸ›¡ï¸ *SL Ref:* `${resultado['sl']:,.4f}` | *TP Ref:* `${resultado['tp']:,.4f}`\n"
                        f"ðŸ“‹ *QuÃ© hacer:*\n{resultado['plan']}"
                    )

                    clave_alerta = f"{symbol}_{tf}_{resultado['id_senal']}"
                    ahora = time.time()

                    if ahora - ALERTAS_ENVIADAS_GLOBAL.get(clave_alerta, 0) > 600:
                        enviar_telegram(msg)
                        ALERTAS_ENVIADAS_GLOBAL[clave_alerta] = ahora
                        alertas_nuevas += 1

                time.sleep(0.12)
            except Exception:
                continue

    if not es_hilo_fondo:
        AUTOPILOTO_STATUS = f"âœ… Barrido listo. Alertas reales enviadas: {alertas_nuevas}"
    else:
        AUTOPILOTO_STATUS = "âš¡ Motor Forex & Cripto activo (ciclo cada 30s) con datos reales..."


@st.cache_resource
def iniciar_hilo_autopiloto():
    def loop():
        time.sleep(3)
        while True:
            try:
                ejecutar_logica_autopiloto(es_hilo_fondo=True)
            except Exception:
                pass
            time.sleep(30)
    thread = threading.Thread(target=loop, daemon=True)
    thread.start()
    return thread


iniciar_hilo_autopiloto()


# ==========================================
# ðŸ“Š INTERFAZ GRÃFICA PRINCIPAL
# ==========================================
st.markdown("<h1 style='text-align: center; color: #00FFAA;'>ðŸ¦ INSTITUTIONAL QUANT TERMINAL â€” FOREX & CRYPTO</h1>", unsafe_allow_html=True)
st.markdown("<p style='text-align: center; color: gray;'>AMT (POC/VAH/VAL/LVN) + Order Blocks/FVG/Fib + Multitemporalidad 4H + Noticias en Vivo</p>", unsafe_allow_html=True)
st.markdown("---")

st.sidebar.header("ðŸŒ ConfiguraciÃ³n del GrÃ¡fico")
symbol = st.sidebar.selectbox("Ver GrÃ¡fico de:", ALL_TRACKED_SYMBOLS)
timeframe = st.sidebar.selectbox("Temporalidad", ["5m", "15m", "1h"])

st.sidebar.markdown("---")
st.sidebar.subheader("ðŸ“¡ Estado del Motor")
st.sidebar.info(AUTOPILOTO_STATUS)

if st.sidebar.button("âš¡ Forzar Escaneo Ahora"):
    with st.spinner("Ejecutando motor en Forex/Oro y Cripto..."):
        ejecutar_logica_autopiloto(es_hilo_fondo=False)
        st.sidebar.success("Â¡Escaneo completado!")

with st.sidebar.expander("ðŸ” DiagnÃ³stico Cripto / Oro"):
    for sym_diag in ["BTC/USDT", "ETH/USDT", "BNB/USDT", "XAUUSD=X"]:
        df_d = obtener_datos(sym_diag, timeframe, limit=85, ttl_segundos=60)
        real_txt = "REAL âœ…" if df_d.attrs.get("es_real", False) else "SIN DATO REAL âŒ"
        adx_d = float(df_d["adx"].iloc[-1]) if not df_d.empty and "adx" in df_d.columns else 0.0
        umbral_d = obtener_umbral_adx(sym_diag)
        st.caption(f"{sym_diag}: {real_txt} | ADX: {adx_d:.1f} (min >{umbral_d:.0f})")


@st.fragment(run_every="10s")
def render_trading_panel(symbol, timeframe):
    df = obtener_datos(symbol, timeframe, limit=120, ttl_segundos=30)
    if df.empty:
        st.error(f"No se pudieron cargar los datos para {symbol}.")
        return

    if not df.attrs.get("es_real", False):
        st.warning("âš ï¸ Datos de respaldo activos por lÃ­mite temporal de proveedor externo. No se enviarÃ¡n alertas de Telegram con estos datos.")

    vp_info = calcular_volume_profile_amt(df)
    sesgo_htf = obtener_sesgo_htf(symbol)

    col_grafico, col_paneles = st.columns([2.5, 1])
    with col_grafico:
        fig = make_subplots(rows=2, cols=1, shared_xaxes=True, vertical_spacing=0.03, row_width=[0.2, 0.8])
        fig.add_trace(go.Candlestick(
            x=df["timestamp"], open=df["open"], high=df["high"], low=df["low"], close=df["close"],
            increasing_line_color="#00FFAA", decreasing_line_color="#FF3333", name="Precio"
        ), row=1, col=1)
        fig.add_trace(go.Scatter(x=df["timestamp"], y=df["vwap"], line=dict(color="#FF00FF", width=2), name="VWAP"), row=1, col=1)
        fig.add_trace(go.Scatter(x=df["timestamp"], y=df["ema_fast"], line=dict(color="#FFFF00", width=1.5), name="EMA 9"), row=1, col=1)
        fig.add_trace(go.Scatter(x=df["timestamp"], y=df["ema_slow"], line=dict(color="#00BFFF", width=1.5), name="EMA 21"), row=1, col=1)

        if vp_info:
            fig.add_hline(y=vp_info["poc"], line_dash="dash", line_color="#FFA500", annotation_text="POC", row=1, col=1)
            fig.add_hline(y=vp_info["vah"], line_dash="dot", line_color="#8888FF", annotation_text="VAH", row=1, col=1)
            fig.add_hline(y=vp_info["val"], line_dash="dot", line_color="#8888FF", annotation_text="VAL", row=1, col=1)

        colors = ["#00FFAA" if r["close"] >= r["open"] else "#FF3333" for _, r in df.iterrows()]
        fig.add_trace(go.Bar(x=df["timestamp"], y=df["volume"], marker_color=colors, name="Volumen"), row=2, col=1)
        fig.update_layout(template="plotly_dark", xaxis_rangeslider_visible=False, height=600, margin=dict(l=10, r=10, t=10, b=10), showlegend=True)
        st.plotly_chart(fig, use_container_width=True)

    with col_paneles:
        st.markdown("### âš™ï¸ Lectura AMT & Smart Money")
        ultima = df.iloc[-1]
        anterior = df.iloc[-2]
        precio_actual = float(ultima["close"])
        st.metric(label=f"{symbol} [{timeframe}]", value=f"${precio_actual:,.4f}", delta=f"{precio_actual - float(anterior['close']):,.4f}")

        st.caption(f"ðŸ§­ **Sesgo HTF (4H):** `{sesgo_htf}`")
        if vp_info:
            st.caption(f"ðŸ“Š **Estado AMT:** `{vp_info['estado']}`")
            st.caption(f"ðŸŽ¯ **POC:** `${vp_info['poc']:,.4f}` | **VA:** `${vp_info['val']:,.4f}` - `${vp_info['vah']:,.4f}`")

        if precio_actual < float(ultima["vwap"]):
            st.error("ðŸ”´ SESGO VS VWAP: BAJISTA")
        else:
            st.success("ðŸŸ¢ SESGO VS VWAP: ALCISTA")

        adx_val = float(ultima.get("adx", 25.0))
        adx_min = obtener_umbral_adx(symbol)
        if adx_val > adx_min:
            st.success(f"ðŸ”¥ ADX: {adx_val:.1f} (>{adx_min:.0f} Fuerza Extrema)")
        elif adx_val > 25:
            st.warning(f"ðŸ“ˆ ADX: {adx_val:.1f} (Tendencia activa / min >{adx_min:.0f})")
        else:
            st.info(f"âš–ï¸ ADX: {adx_val:.1f} (Rango / ConsolidaciÃ³n)")

        cvd_actual = float(ultima.get("cvd", 0.0))
        cvd_prev = float(anterior.get("cvd", 0.0))
        st.caption(f"CVD Delta: {'ðŸŸ¢ PresiÃ³n compradora' if cvd_actual >= cvd_prev else 'ðŸ”´ PresiÃ³n vendedora'}")

        fvg_info = detectar_fvg_3_velas(df)
        if fvg_info:
            st.caption(f"FVG / Imbalance: `{fvg_info['tipo']}` ({fvg_info['zona_low']:,.4f} - {fvg_info['zona_high']:,.4f})")

        st.markdown("---")
        st.markdown("### ðŸŽ¯ Plan Sugerido por Motor")
        senal = evaluar_estrategia_unificada(df, symbol=symbol, sesgo_htf=sesgo_htf, vp_info=vp_info)
        if senal:
            st.info(f"**{senal['estrategia']}**\n\n**AcciÃ³n:** {senal['direccion']}\n\n{senal['plan']}")
        else:
            st.caption("Sin seÃ±al conjunta activa bajo el filtro unificado en esta vela.")

    st.markdown("---")
    st.markdown("### ðŸ“° Noticias de Mercados en Vivo (Forex, Oro & Cripto)")
    noticias = obtener_noticias_en_vivo()
    if noticias:
        cols_news = st.columns(2)
        for idx, n in enumerate(noticias[:8]):
            with cols_news[idx % 2]:
                st.markdown(f"- **[{n['fuente']}]** [{n['titulo']}]({n['link']}) *({n['fecha']})*")
    else:
        st.caption("No se pudieron cargar titulares en este momento.")


render_trading_panel(symbol, timeframe)