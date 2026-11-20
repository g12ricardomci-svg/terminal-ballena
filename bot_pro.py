import streamlit as st
import ccxt
import yfinance as yf
import pandas as pd
import numpy as np
import plotly.graph_objects as go
from plotly.subplots import make_subplots
import requests
import time
import threading
from datetime import datetime

# ==========================================
# ⚙️ CONFIGURACIÓN GLOBAL & LISTA CERRADA ESTRICTA ($10 USD: FOREX + CRIPTO)
# ==========================================
TELEGRAM_TOKEN = "9148434925:AAH_AuswS-GGPHadFBwcrDOUUIwBFbXvtDs"
TELEGRAM_CHAT_ID = "1496321437"

st.set_page_config(page_title="Institutional Quant Terminal - Forex & Crypto", layout="wide", page_icon="🏦")

# ÚNICOS MERCADOS HABILITADOS PARA ALERTAS Y ESCANEO ($10 USD) — SIN DUPLICADOS
CRYPTO_MARKETS = ["BTC/USDT", "ETH/USDT", "BNB/USDT"]
FOREX_MARKETS = [
    "EURUSD=X", "GBPUSD=X", "USDJPY=X", "USDCHF=X", "AUDUSD=X",
    "USDCAD=X", "NZDUSD=X", "EURJPY=X", "GBPJPY=X", "EURGBP=X",
    "XAUUSD=X"
]
NASDAQ_MARKETS = []

MAPA_CANONICO = {
    "XAUUSD": "XAUUSD",
    "XAUUSD=X": "XAUUSD",
    "GC=F": "XAUUSD",
}

def canonizar_simbolo(symbol):
    s = str(symbol or "").strip().upper()
    return MAPA_CANONICO.get(s, s)

def clasificar_mercado(symbol):
    canon = canonizar_simbolo(symbol)
    if symbol in CRYPTO_MARKETS or "/" in canon:
        return "CRYPTO"
    if canon == "XAUUSD":
        return "METAL"
    return "FOREX"

def tamano_pip(symbol):
    canon = canonizar_simbolo(symbol)
    if canon == "XAUUSD":
        return 0.10
    if "JPY" in canon:
        return 0.01
    return 0.0001

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
CACHE_LOCK = threading.Lock()
AUTOPILOTO_STATUS = "Motor Forex & Cripto activo: Escaneando cada 30s..."
ULTIMO_ERROR_TG = ""

# ==========================================
# 🧠 FUNCIONES MATEMÁTICAS, FLUJO Y ESTRUCTURA
# ==========================================
def enviar_telegram(mensaje):
    global ULTIMO_ERROR_TG
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    payload = {"chat_id": TELEGRAM_CHAT_ID, "text": mensaje, "parse_mode": "Markdown"}
    try:
        resp = requests.post(url, json=payload, timeout=8)
        if resp.status_code == 200:
            ULTIMO_ERROR_TG = ""
            return True
        ULTIMO_ERROR_TG = f"HTTP {resp.status_code}: {resp.text[:140]}"
        return False
    except Exception as e:
        ULTIMO_ERROR_TG = str(e)[:140]
        return False

def _normalizar_timestamp(serie):
    ts = pd.to_datetime(serie)
    if getattr(ts.dt, "tz", None) is not None:
        ts = ts.dt.tz_convert(None)
    return ts

def obtener_umbral_adx(symbol, timeframe="15m"):
    """Adapta el umbral de fuerza por mercado y temporalidad."""
    mercado = clasificar_mercado(symbol)
    if mercado == "METAL":
        return {"5m": 38.0, "15m": 34.0, "1h": 30.0}.get(timeframe, 36.0)
    if mercado == "CRYPTO":
        return {"5m": 35.0, "15m": 32.0, "1h": 28.0}.get(timeframe, 34.0)
    # FOREX
    return {"5m": 32.0, "15m": 28.0, "1h": 25.0}.get(timeframe, 30.0)

def obtener_sl_minimo(symbol, timeframe, precio):
    """Piso mínimo de distancia de SL para evitar stops de 1 pip atrapados por el spread."""
    mercado = clasificar_mercado(symbol)
    pip = tamano_pip(symbol)
    if mercado == "FOREX":
        pips_min = {"5m": 8.0, "15m": 12.0, "1h": 18.0}.get(timeframe, 10.0)
        return pips_min * pip
    if mercado == "METAL":
        pts_min = {"5m": 4.5, "15m": 8.0, "1h": 14.0}.get(timeframe, 8.0)
        return pts_min
    # CRYPTO
    pct_min = {"5m": 0.0045, "15m": 0.0080, "1h": 0.0150}.get(timeframe, 0.0080)
    return float(precio) * pct_min

def calcular_vwap(df):
    if df.empty:
        return pd.Series(dtype=float)
    if "volume" not in df.columns or float(df["volume"].sum()) <= 0:
        return df["close"].rolling(20, min_periods=1).mean()
    tp = (df["high"] + df["low"] + df["close"]) / 3.0
    vol_roll = df["volume"].rolling(24, min_periods=1).sum().replace(0, np.nan)
    vwap = (tp * df["volume"]).rolling(24, min_periods=1).sum() / vol_roll
    return vwap.fillna(df["close"].rolling(20, min_periods=1).mean())

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
    """Detecta Fair Value Gap / Imbalance de 3 velas en las últimas barras."""
    if len(df) < 4:
        return None
    for idx in (-1, -2, -3):
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

def evaluar_estrategia_unificada(df, symbol="", timeframe="15m", sesgo_1h=None):
    """
    Motor separado por mercado (FOREX / METAL / CRYPTO) con confluencias reales
    y piso mínimo de SL por temporalidad.
    """
    if df.empty or len(df) < 25:
        return None

    mercado = clasificar_mercado(symbol)
    ultima = df.iloc[-1]
    anterior = df.iloc[-2]
    precio = float(ultima["close"])
    adx_val = float(ultima.get("adx", 0.0))
    vwap_val = float(ultima.get("vwap", precio))
    ema_fast = float(ultima.get("ema_fast", precio))
    ema_slow = float(ultima.get("ema_slow", precio))

    adx_min = obtener_umbral_adx(symbol, timeframe=timeframe)
    if adx_val <= adx_min:
        return None

    vol_actual = float(ultima.get("volume", 0.0))
    vol_ma = float(ultima.get("vol_ma", 0.0))
    desbalance_vol = (vol_ma > 0) and (vol_actual > vol_ma * 1.25)

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
    swing_max_prev = float(df["high"].iloc[-10:-1].max()) if len(df) >= 11 else swing_high_ref
    swing_min_prev = float(df["low"].iloc[-10:-1].min()) if len(df) >= 11 else swing_low_ref
    rr_mult = 2.5
    sl_min_dist = obtener_sl_minimo(symbol, timeframe, precio)

    ob_alcista = (anterior["close"] < anterior["open"]) and (ultima["close"] > ultima["open"]) and (precio > anterior["high"])
    ob_bajista = (anterior["close"] > anterior["open"]) and (ultima["close"] < ultima["open"]) and (precio < anterior["low"])
    cerca_ema_slow = abs(precio - ema_slow) <= atr_ref * 1.25
    impulso_limpio_long = (precio > swing_max_prev) or (abs(float(ultima["close"]) - float(ultima["open"])) >= atr_ref * 0.45)
    impulso_limpio_short = (precio < swing_min_prev) or (abs(float(ultima["close"]) - float(ultima["open"])) >= atr_ref * 0.45)

    if es_exhaustacion:
        sl_exh = float(swing_low_ref - sl_min_dist) if precio >= ultima["open"] else float(swing_high_ref + sl_min_dist)
        return {
            "id_senal": "EXHAUSTION_GUARD",
            "estrategia": f"Freno Exhaustación ({mercado}) • ADX > {adx_min:.0f}",
            "direccion": "NO ENTRAR EN EXTREMO / ESPERAR RETROCESO ⏸️",
            "sl": sl_exh,
            "tp": vwap_val,
            "plan": (
                f"1) ADX en {adx_val:.1f} (>{adx_min:.0f}) con vela de exhaustación detectada: evita comprar techo o vender suelo.\n"
                f"2) Espera retroceso hacia VWAP (${vwap_val:,.4f}), Order Block o zona de descuento antes de actuar.\n"
                f"3) Mantén disciplina y protege tu capital sin perseguir el movimiento."
            )
        }

    # --- BLOQUE ALCISTA (LONG) POR MERCADO ---
    confluencias_long = 0
    motivos_long = []
    if precio >= vwap_val and ema_fast >= ema_slow:
        confluencias_long += 1
        motivos_long.append("Estructura/VWAP/EMA alcista")

    if mercado == "CRYPTO":
        if cvd_alcista or desbalance_vol:
            confluencias_long += 1
            motivos_long.append("CVD/Volumen respalda compras")
        if ob_alcista or (fvg and fvg["tipo"] == "FVG_ALCISTA") or cerca_ema_slow:
            confluencias_long += 1
            motivos_long.append("OB/FVG/Zona favorable")
    else:
        # FOREX / METAL: no depende de volumen cero de yfinance
        if impulso_limpio_long or cvd_alcista:
            confluencias_long += 1
            motivos_long.append("Impulso/Estructura alcista")
        if ob_alcista or (fvg and fvg["tipo"] == "FVG_ALCISTA") or cerca_ema_slow:
            confluencias_long += 1
            motivos_long.append("OB/FVG/Retroceso EMA")

    filtro_1h_ok_long = True
    if mercado in ("FOREX", "METAL") and timeframe in ("5m", "15m") and sesgo_1h in ("COMPRA", "VENTA"):
        filtro_1h_ok_long = (sesgo_1h == "COMPRA")

    if confluencias_long >= 2 and precio >= vwap_val and filtro_1h_ok_long:
        base_low = min(swing_low_ref, float(ultima["low"]))
        sl_tecnico = float(base_low - atr_ref * 0.5)
        riesgo = max(abs(precio - sl_tecnico), atr_ref * 0.9, sl_min_dist)
        sl_sugerido = float(precio - riesgo)
        tp_sugerido = float(precio + riesgo * rr_mult)
        return {
            "id_senal": "CONFLUENCIA_LONG",
            "estrategia": f"Confluencia Conjunta ({confluencias_long}/3) • {mercado} ADX>{adx_min:.0f}",
            "direccion": "COMPRA (LONG) 🟢",
            "sl": sl_sugerido,
            "tp": tp_sugerido,
            "plan": (
                f"1) Confluencias activas: {', '.join(motivos_long)} (ADX {adx_val:.1f} > {adx_min:.0f}).\n"
                f"2) Valida retroceso hacia VWAP (${vwap_val:,.4f}), OB o FVG cercano sin perseguir picos.\n"
                f"3) Stop Loss con colchón mínimo/estructura en ${sl_sugerido:,.4f} (valida riesgo vs $10 USD).\n"
                f"4) Take Profit objetivo (1:{rr_mult:.1f}) en ${tp_sugerido:,.4f}; no promedies en contra."
            )
        }

    # --- BLOQUE BAJISTA (SHORT) POR MERCADO ---
    confluencias_short = 0
    motivos_short = []
    if precio <= vwap_val and ema_fast <= ema_slow:
        confluencias_short += 1
        motivos_short.append("Estructura/VWAP/EMA bajista")

    if mercado == "CRYPTO":
        if cvd_bajista or desbalance_vol:
            confluencias_short += 1
            motivos_short.append("CVD/Volumen respalda ventas")
        if ob_bajista or (fvg and fvg["tipo"] == "FVG_BAJISTA") or cerca_ema_slow:
            confluencias_short += 1
            motivos_short.append("OB/FVG/Zona favorable")
    else:
        if impulso_limpio_short or cvd_bajista:
            confluencias_short += 1
            motivos_short.append("Impulso/Estructura bajista")
        if ob_bajista or (fvg and fvg["tipo"] == "FVG_BAJISTA") or cerca_ema_slow:
            confluencias_short += 1
            motivos_short.append("OB/FVG/Retroceso EMA")

    filtro_1h_ok_short = True
    if mercado in ("FOREX", "METAL") and timeframe in ("5m", "15m") and sesgo_1h in ("COMPRA", "VENTA"):
        filtro_1h_ok_short = (sesgo_1h == "VENTA")

    if confluencias_short >= 2 and precio <= vwap_val and filtro_1h_ok_short:
        base_high = max(swing_high_ref, float(ultima["high"]))
        sl_tecnico = float(base_high + atr_ref * 0.5)
        riesgo = max(abs(sl_tecnico - precio), atr_ref * 0.9, sl_min_dist)
        sl_sugerido = float(precio + riesgo)
        tp_sugerido = float(precio - riesgo * rr_mult)
        return {
            "id_senal": "CONFLUENCIA_SHORT",
            "estrategia": f"Confluencia Conjunta ({confluencias_short}/3) • {mercado} ADX>{adx_min:.0f}",
            "direccion": "VENTA (SHORT) 🔴",
            "sl": sl_sugerido,
            "tp": tp_sugerido,
            "plan": (
                f"1) Confluencias activas: {', '.join(motivos_short)} (ADX {adx_val:.1f} > {adx_min:.0f}).\n"
                f"2) Valida retroceso hacia VWAP (${vwap_val:,.4f}), OB o FVG cercano sin perseguir picos.\n"
                f"3) Stop Loss con colchón mínimo/estructura en ${sl_sugerido:,.4f} (valida riesgo vs $10 USD).\n"
                f"4) Take Profit objetivo (1:{rr_mult:.1f}) en ${tp_sugerido:,.4f}; no promedies en contra."
            )
        }

    return None

# ==========================================
# 🛠️ OBTENCIÓN DE DATOS CON CACHÉ CANÓNICA Y VALIDACIÓN REAL
# ==========================================
def obtener_datos(symbol, timeframe, limit=100, ttl_segundos=60):
    canon = canonizar_simbolo(symbol)
    cache_key = f"{canon}_{timeframe}_{limit}"
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
    elif symbol in FOREX_MARKETS or canon == "XAUUSD":
        interval_map = {"5m": "5m", "15m": "15m", "1h": "60m"}
        yf_interval = interval_map.get(timeframe, "60m")
        periodos_a_probar = ["5d", "1mo"] if timeframe == "5m" else ["1mo", "3mo"]

        simbolos_yf = ["XAUUSD=X", "GC=F"] if canon == "XAUUSD" else [symbol]

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
        freq_map = {"5m": "5min", "15m": "15min", "1h": "1h"}
        fechas = pd.date_range(end=datetime.now(), periods=limit, freq=freq_map.get(timeframe, "15min"))
        base_val = 2650.0 if "XAU" in canon else (1.085 if "EUR" in canon else 150.0)
        rng = np.random.default_rng(abs(hash(canon + timeframe)) % (2**32))
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

def _extraer_sesgo_1h(symbol):
    df_1h = obtener_datos(symbol, "1h", limit=85, ttl_segundos=90)
    if df_1h.empty or not df_1h.attrs.get("es_real", False):
        return None
    res_1h = evaluar_estrategia_unificada(df_1h, symbol=symbol, timeframe="1h", sesgo_1h=None)
    if not res_1h:
        return None
    if "COMPRA" in res_1h["direccion"]:
        return "COMPRA"
    if "VENTA" in res_1h["direccion"]:
        return "VENTA"
    return None

# ==========================================
# 🤖 MOTOR EN FONDO (SOLO FOREX/METAL Y CRIPTO)
# ==========================================
def ejecutar_logica_autopiloto(es_hilo_fondo=False):
    global AUTOPILOTO_STATUS, ALERTAS_ENVIADAS_GLOBAL

    if not es_hilo_fondo:
        AUTOPILOTO_STATUS = "Ejecutando barrido Forex & Cripto..."

    temporalidades = ["1h", "15m", "5m"]
    alertas_nuevas = 0
    vistos_en_ciclo = set()

    for symbol in ALL_TRACKED_SYMBOLS:
        if symbol not in CRYPTO_MARKETS and symbol not in FOREX_MARKETS:
            continue

        canon = canonizar_simbolo(symbol)
        if canon in vistos_en_ciclo:
            continue
        vistos_en_ciclo.add(canon)

        tipo = "CRIPTO" if symbol in CRYPTO_MARKETS else ("METAL (ORO)" if canon == "XAUUSD" else "FOREX")
        sesgo_1h = _extraer_sesgo_1h(symbol) if tipo in ("FOREX", "METAL (ORO)") else None

        for tf in temporalidades:
            if not es_hilo_fondo:
                AUTOPILOTO_STATUS = f"Analizando {canon} [{tf}]..."

            try:
                df = obtener_datos(symbol, tf, limit=85, ttl_segundos=60)
                if df.empty or not df.attrs.get("es_real", False):
                    continue

                resultado = evaluar_estrategia_unificada(df, symbol=symbol, timeframe=tf, sesgo_1h=sesgo_1h)
                if resultado:
                    ultima = df.iloc[-1]
                    precio = float(ultima["close"])
                    adx_val = float(ultima.get("adx", 0.0))

                    msg = (
                        f"🏦 *SEÑAL CONJUNTA UNIFICADA*\n"
                        f"🌐 *Mercado:* `{tipo}` | *Activo:* `{canon}` | *TF:* `{tf}`\n"
                        f"💲 *Precio Real:* `${precio:,.4f}` | *ADX:* `{adx_val:.1f}`\n"
                        f"🎯 *Estrategia:* {resultado['estrategia']}\n"
                        f"🧭 *Acción:* *{resultado['direccion']}*\n"
                        f"🛡️ *SL Ref:* `${resultado['sl']:,.4f}` | *TP Ref:* `${resultado['tp']:,.4f}`\n"
                        f"📋 *Qué hacer:*\n{resultado['plan']}"
                    )

                    clave_alerta = f"{canon}_{tf}_{resultado['id_senal']}"
                    ahora = time.time()

                    if ahora - ALERTAS_ENVIADAS_GLOBAL.get(clave_alerta, 0) > 600:
                        if enviar_telegram(msg):
                            ALERTAS_ENVIADAS_GLOBAL[clave_alerta] = ahora
                            alertas_nuevas += 1

                time.sleep(0.12)
            except Exception:
                continue

    if not es_hilo_fondo:
        AUTOPILOTO_STATUS = f"✅ Barrido listo. Alertas reales enviadas: {alertas_nuevas}"
    else:
        AUTOPILOTO_STATUS = "⚡ Motor Forex & Cripto activo (ciclo cada 30s) con datos reales..."

@st.cache_resource
def iniciar_hilo_autopiloto():
    def loop():
        time.sleep(5)
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
# 📊 INTERFAZ GRÁFICA PRINCIPAL
# ==========================================
st.markdown("<h1 style='text-align: center; color: #00FFAA;'>🏦 INSTITUTIONAL QUANT TERMINAL — FOREX & CRYPTO</h1>", unsafe_allow_html=True)
st.markdown("<p style='text-align: center; color: gray;'>Alertas Filtradas para Forex/Oro y Cripto • Parámetros por Mercado + Filtro 1h + Piso de SL + Freno Exhaustación</p>", unsafe_allow_html=True)
st.markdown("---")

st.sidebar.header("🌐 Configuración del Gráfico")
symbol = st.sidebar.selectbox("Ver Gráfico de:", ALL_TRACKED_SYMBOLS)
timeframe = st.sidebar.selectbox("Temporalidad", ["5m", "15m", "1h"])

st.sidebar.markdown("---")
st.sidebar.subheader("📡 Estado del Motor")
st.sidebar.info(AUTOPILOTO_STATUS)
if ULTIMO_ERROR_TG:
    st.sidebar.caption(f"⚠️ Telegram: {ULTIMO_ERROR_TG}")

if st.sidebar.button("⚡ Forzar Escaneo Ahora"):
    with st.spinner("Ejecutando motor en Forex/Oro y Cripto..."):
        ejecutar_logica_autopiloto(es_hilo_fondo=False)
        st.sidebar.success("¡Escaneo completado!")

with st.sidebar.expander("🔍 Diagnóstico Cripto / Oro"):
    for sym_diag in ["BTC/USDT", "ETH/USDT", "BNB/USDT", "XAUUSD=X"]:
        df_d = obtener_datos(sym_diag, timeframe, limit=85, ttl_segundos=60)
        real_txt = "REAL ✅" if df_d.attrs.get("es_real", False) else "SIN DATO REAL ❌"
        adx_d = float(df_d["adx"].iloc[-1]) if not df_d.empty and "adx" in df_d.columns else 0.0
        umbral_d = obtener_umbral_adx(sym_diag, timeframe=timeframe)
        st.caption(f"{canonizar_simbolo(sym_diag)}: {real_txt} | ADX: {adx_d:.1f} (min >{umbral_d:.0f})")

@st.fragment(run_every="10s")
def render_trading_panel(symbol, timeframe):
    df = obtener_datos(symbol, timeframe, limit=120, ttl_segundos=30)
    if df.empty:
        st.error(f"No se pudieron cargar los datos para {symbol}.")
        return

    if not df.attrs.get("es_real", False):
        st.warning("⚠️ Datos de respaldo activos por límite temporal de proveedor externo. No se enviarán alertas de Telegram con estos datos.")

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
        fig.add_trace(go.Scatter(x=df["timestamp"], y=df["soporte"], line=dict(color="#00FF00", width=1, dash="dot"), name="Soporte 20"), row=1, col=1)
        fig.add_trace(go.Scatter(x=df["timestamp"], y=df["resistencia"], line=dict(color="#FF0000", width=1, dash="dot"), name="Resistencia 20"), row=1, col=1)
        colors = ["#00FFAA" if r["close"] >= r["open"] else "#FF3333" for _, r in df.iterrows()]
        fig.add_trace(go.Bar(x=df["timestamp"], y=df["volume"], marker_color=colors, name="Volumen"), row=2, col=1)
        fig.update_layout(template="plotly_dark", xaxis_rangeslider_visible=False, height=600, margin=dict(l=10, r=10, t=10, b=10), showlegend=True)
        st.plotly_chart(fig, use_container_width=True)

    with col_paneles:
        st.markdown("### ⚙️ Lectura de Flujo y Estructura")
        ultima = df.iloc[-1]
        anterior = df.iloc[-2]
        precio_actual = float(ultima["close"])
        st.metric(label=f"{canonizar_simbolo(symbol)} [{timeframe}]", value=f"${precio_actual:,.4f}", delta=f"{precio_actual - float(anterior['close']):,.4f}")

        if precio_actual < float(ultima["vwap"]):
            st.error("🔴 SESGO VS VWAP: BAJISTA")
        else:
            st.success("🟢 SESGO VS VWAP: ALCISTA")

        adx_val = float(ultima.get("adx", 25.0))
        adx_min = obtener_umbral_adx(symbol, timeframe=timeframe)
        if adx_val > adx_min:
            st.success(f"🔥 ADX: {adx_val:.1f} (>{adx_min:.0f} Fuerza Extrema)")
        elif adx_val > 25:
            st.warning(f"📈 ADX: {adx_val:.1f} (Tendencia activa / min >{adx_min:.0f})")
        else:
            st.info(f"⚖️ ADX: {adx_val:.1f} (Rango / Consolidación)")

        cvd_actual = float(ultima.get("cvd", 0.0))
        cvd_prev = float(anterior.get("cvd", 0.0))
        st.caption(f"CVD Delta: {'🟢 Presión compradora' if cvd_actual >= cvd_prev else '🔴 Presión vendedora'}")

        fvg_info = detectar_fvg_3_velas(df)
        if fvg_info:
            st.caption(f"FVG / Imbalance: `{fvg_info['tipo']}` ({fvg_info['zona_low']:,.4f} - {fvg_info['zona_high']:,.4f})")

        st.markdown("---")
        st.markdown("### 🎯 Plan Sugerido por Motor")
        sesgo_1h_panel = _extraer_sesgo_1h(symbol) if clasificar_mercado(symbol) in ("FOREX", "METAL") else None
        senal = evaluar_estrategia_unificada(df, symbol=symbol, timeframe=timeframe, sesgo_1h=sesgo_1h_panel)
        if senal:
            st.info(f"**{senal['estrategia']}**\n\n**Acción:** {senal['direccion']}\n\n{senal['plan']}")
        else:
            st.caption("Sin señal conjunta activa bajo el filtro unificado en esta vela.")

render_trading_panel(symbol, timeframe)
