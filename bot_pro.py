import hashlib
import numpy as np
import pandas as pd

# Normalización canónica para evitar duplicados tipo XAUUSD vs XAUUSD=X
MAPA_SIMBOLOS_CANONICOS = {
    "XAUUSD": "XAUUSD",
    "XAUUSD=X": "XAUUSD",
    "GC=F": "XAUUSD",
    "EURUSD=X": "EURUSD",
    "GBPUSD=X": "GBPUSD",
    "USDCHF=X": "USDCHF",
    "USDCAD=X": "USDCAD",
    "AUDUSD=X": "AUDUSD",
    "NZDUSD=X": "NZDUSD",
    "EURJPY=X": "EURJPY",
    "GBPJPY=X": "GBPJPY",
    "EURGBP=X": "EURGBP",
    "BTC-USD": "BTCUSD",
    "ETH-USD": "ETHUSD",
    "SOL-USD": "SOLUSD",
    "XRP-USD": "XRPUSD",
    "ADA-USD": "ADAUSD",
    "DOGE-USD": "DOGEUSD",
    "AVAX-USD": "AVAXUSD",
    "LINK-USD": "LINKUSD",
    "DOT-USD": "DOTUSD",
    "BNB-USD": "BNBUSD",
}

PARAMETROS_MERCADO = {
    "FOREX": {
        "adx_min": {"5m": 32.0, "15m": 28.0, "1h": 25.0},
        "sl_min_pips": {"5m": 8.0, "15m": 12.0, "1h": 18.0},
        "rr": 2.5,
        "exigir_filtro_1h": True,
    },
    "METAL": {
        "adx_min": {"5m": 35.0, "15m": 30.0, "1h": 28.0},
        "sl_min_dist": {"5m": 4.5, "15m": 8.0, "1h": 14.0},  # distancia mínima en precio para XAUUSD
        "rr": 2.5,
        "exigir_filtro_1h": True,
    },
    "CRYPTO": {
        "adx_min": {"5m": 32.0, "15m": 28.0, "1h": 25.0},
        "sl_min_pct": {"5m": 0.0045, "15m": 0.0080, "1h": 0.0150},
        "rr": 2.5,
        "exigir_filtro_1h": False,
    },
}


def canonizar_simbolo(symbol: str) -> str:
    s = (symbol or "").strip().upper()
    return MAPA_SIMBOLOS_CANONICOS.get(s, s.replace("=X", "").replace("-", ""))


def detectar_tipo_activo(symbol: str) -> str:
    s = canonizar_simbolo(symbol)
    if s == "XAUUSD":
        return "METAL"
    if s in {"BTCUSD", "ETHUSD", "SOLUSD", "XRPUSD", "ADAUSD", "DOGEUSD", "AVAXUSD", "LINKUSD", "DOTUSD", "BNBUSD"}:
        return "CRYPTO"
    return "FOREX"


def tamano_pip(symbol: str) -> float:
    s = canonizar_simbolo(symbol)
    if s == "XAUUSD":
        return 0.10
    if "JPY" in s:
        return 0.01
    return 0.0001


def construir_clave_alerta(symbol: str, timeframe: str, direccion: str, precio: float, id_senal: str) -> str:
    canon = canonizar_simbolo(symbol)
    # Redondeo suave para no re-alertar el mismo setup en el mismo minuto/precio
    precio_bucket = round(float(precio), 4 if detectar_tipo_activo(canon) == "FOREX" else 2)
    raw = f"{canon}|{timeframe}|{direccion}|{precio_bucket}|{id_senal}"
    return hashlib.md5(raw.encode("utf-8")).hexdigest()


def evaluar_estrategia_unificada(
    df: pd.DataFrame,
    symbol: str = "EURUSD",
    timeframe: str = "15m",
    sesgo_1h: str | None = None,  # Pasar "COMPRA", "VENTA" o None si viene de 1h
) -> dict | None:
    if df is None or len(df) < 35:
        return None

    data = df.copy()
    canon = canonizar_simbolo(symbol)
    tipo_activo = detectar_tipo_activo(canon)
    cfg = PARAMETROS_MERCADO[tipo_activo]
    rr = cfg["rr"]

    close = pd.to_numeric(data["Close"], errors="coerce")
    high = pd.to_numeric(data["High"], errors="coerce")
    low = pd.to_numeric(data["Low"], errors="coerce")
    open_p = pd.to_numeric(data["Open"], errors="coerce") if "Open" in data.columns else close.shift(1).fillna(close)
    vol = pd.to_numeric(data["Volume"], errors="coerce").fillna(0.0) if "Volume" in data.columns else pd.Series(0.0, index=data.index)

    ema9 = close.ewm(span=9, adjust=False).mean()
    ema21 = close.ewm(span=21, adjust=False).mean()

    # VWAP de sesión / ventana reciente
    tp = (high + low + close) / 3.0
    if vol.sum() > 0:
        vwap = (tp * vol).rolling(24, min_periods=1).sum() / vol.rolling(24, min_periods=1).sum().replace(0, np.nan)
        vwap = vwap.fillna(close.rolling(20, min_periods=1).mean())
    else:
        vwap = close.rolling(20, min_periods=1).mean()

    # ATR
    tr = pd.concat(
        [
            high - low,
            (high - close.shift(1)).abs(),
            (low - close.shift(1)).abs(),
        ],
        axis=1,
    ).max(axis=1)
    atr = tr.rolling(14, min_periods=1).mean()

    # ADX simplificado y estable
    up_move = high.diff()
    down_move = -low.diff()
    plus_dm = np.where((up_move > down_move) & (up_move > 0), up_move, 0.0)
    minus_dm = np.where((down_move > up_move) & (down_move > 0), down_move, 0.0)
    atr_safe = atr.replace(0, np.nan)
    plus_di = 100.0 * pd.Series(plus_dm, index=data.index).rolling(14, min_periods=1).mean() / atr_safe
    minus_di = 100.0 * pd.Series(minus_dm, index=data.index).rolling(14, min_periods=1).mean() / atr_safe
    dx = (100.0 * (plus_di - minus_di).abs() / (plus_di + minus_di).replace(0, np.nan)).fillna(0.0)
    adx = dx.rolling(14, min_periods=1).mean()

    precio = float(close.iloc[-1])
    adx_val = float(adx.iloc[-1])
    vwap_val = float(vwap.iloc[-1])
    atr_val = float(atr.iloc[-1]) if pd.notna(atr.iloc[-1]) and atr.iloc[-1] > 0 else max(precio * 0.002, tamano_pip(canon) * 8)

    adx_min = cfg["adx_min"].get(timeframe, 28.0)
    if adx_val < adx_min:
        return None

    # Determinar dirección inicial por estructura/VWAP/EMAs
    alcista_base = (precio >= vwap_val) and (float(ema9.iloc[-1]) >= float(ema21.iloc[-1]))
    bajista_base = (precio <= vwap_val) and (float(ema9.iloc[-1]) <= float(ema21.iloc[-1]))

    if alcista_base and not bajista_base:
        direccion = "COMPRA"
    elif bajista_base and not alcista_base:
        direccion = "VENTA"
    else:
        return None

    # Filtro anti-contradicción multi-temporalidad en 5m / 15m
    if cfg.get("exigir_filtro_1h") and timeframe in {"5m", "15m"} and sesgo_1h in {"COMPRA", "VENTA"}:
        if direccion != sesgo_1h:
            return None

    # FVG / Ruptura reciente (últimas 5 velas)
    fvg_alcista = any(float(low.iloc[i]) > float(high.iloc[i - 2]) for i in range(max(2, len(data) - 5), len(data)))
    fvg_bajista = any(float(high.iloc[i]) < float(low.iloc[i - 2]) for i in range(max(2, len(data) - 5), len(data)))
    swing_max_prev = float(high.iloc[-10:-1].max()) if len(data) >= 11 else float(high.iloc[:-1].max())
    swing_min_prev = float(low.iloc[-10:-1].min()) if len(data) >= 11 else float(low.iloc[:-1].min())

    # Cálculo de confluencias adaptado por mercado
    confluencias = 0
    detalles = []

    if tipo_activo in {"FOREX", "METAL"}:
        # 1) Estructura / VWAP / EMAs
        confluencias += 1
        detalles.append("Estructura/VWAP/EMA alineada")

        # 2) Ruptura o impulso direccional limpio
        impulso_ok = (precio > swing_max_prev and direccion == "COMPRA") or (precio < swing_min_prev and direccion == "VENTA") or (
            abs(float(close.iloc[-1]) - float(open_p.iloc[-1])) >= atr_val * 0.45
        )
        if impulso_ok:
            confluencias += 1
            detalles.append("Impulso/ruptura estructural")

        # 3) Zona FVG o retroceso sano cerca de EMA21 / VWAP
        cerca_ema21 = abs(precio - float(ema21.iloc[-1])) <= atr_val * 1.2
        zona_ok = (direccion == "COMPRA" and (fvg_alcista or cerca_ema21)) or (direccion == "VENTA" and (fvg_bajista or cerca_ema21))
        if zona_ok:
            confluencias += 1
            detalles.append("FVG / zona de retroceso")
    else:
        # CRYPTO: incluye flujo de volumen/CVD real
        vol_ma = float(vol.rolling(20, min_periods=1).mean().iloc[-1])
        vol_actual = float(vol.iloc[-1])
        cvd = ((close - open_p) / (high - low).replace(0, np.nan)).fillna(0.0) * vol
        cvd_series = cvd.cumsum()
        flujo_ok = (vol_actual > vol_ma * 1.25 and vol_ma > 0) or (
            (direccion == "COMPRA" and float(cvd_series.iloc[-1]) > float(cvd_series.iloc[-2]))
            or (direccion == "VENTA" and float(cvd_series.iloc[-1]) < float(cvd_series.iloc[-2]))
        )

        confluencias += 1
        detalles.append("Estructura/VWAP/EMA alineada")
        if flujo_ok:
            confluencias += 1
            detalles.append("Flujo/Volumen/CVD")
        if (direccion == "COMPRA" and fvg_alcista) or (direccion == "VENTA" and fvg_bajista):
            confluencias += 1
            detalles.append("FVG activo")

    if confluencias < 2:
        return None

    # Cálculo de SL con piso mínimo por mercado y temporalidad
    swing_min_reciente = float(low.iloc[-6:].min())
    swing_max_reciente = float(high.iloc[-6:].max())

    if direccion == "COMPRA":
        sl_tecnico = min(swing_min_reciente - atr_val * 0.35, precio - atr_val * 1.1)
        dist_sl = max(0.0, precio - sl_tecnico)
    else:
        sl_tecnico = max(swing_max_reciente + atr_val * 0.35, precio + atr_val * 1.1)
        dist_sl = max(0.0, sl_tecnico - precio)

    pip = tamano_pip(canon)
    if tipo_activo == "FOREX":
        min_pips = cfg["sl_min_pips"].get(timeframe, 10.0)
        dist_min = min_pips * pip
    elif tipo_activo == "METAL":
        dist_min = cfg["sl_min_dist"].get(timeframe, 8.0)
    else:
        min_pct = cfg["sl_min_pct"].get(timeframe, 0.008)
        dist_min = precio * min_pct

    dist_final = max(dist_sl, dist_min)

    if direccion == "COMPRA":
        sl_final = round(precio - dist_final, 5 if tipo_activo == "FOREX" else 2)
        tp_final = round(precio + (dist_final * rr), 5 if tipo_activo == "FOREX" else 2)
    else:
        sl_final = round(precio + dist_final, 5 if tipo_activo == "FOREX" else 2)
        tp_final = round(precio - (dist_final * rr), 5 if tipo_activo == "FOREX" else 2)

    id_senal = f"{canon}_{timeframe}_{direccion}_{confluencias}de3"
    clave_unica = construir_clave_alerta(canon, timeframe, direccion, precio, id_senal)

    return {
        "symbol_canon": canon,
        "tipo_activo": tipo_activo,
        "timeframe": timeframe,
        "precio": round(precio, 5 if tipo_activo == "FOREX" else 2),
        "vwap": round(vwap_val, 5 if tipo_activo == "FOREX" else 2),
        "adx": round(adx_val, 1),
        "adx_min": adx_min,
        "confluencias": confluencias,
        "detalles": detalles,
        "direccion": direccion,
        "sl": sl_final,
        "tp": tp_final,
        "distancia_sl": round(dist_final, 5 if tipo_activo == "FOREX" else 2),
        "id_senal": id_senal,
        "clave_unica": clave_unica,
    }
