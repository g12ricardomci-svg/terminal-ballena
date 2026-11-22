# -*- coding: utf-8 -*-
"""
INSTITUTIONAL QUANT TERMINAL v2 — SMC / ICT Intradía

Una sola estrategia, clara y comprobable:
  1) SESGO (dirección):  estructura en 4H, confirmada por 1D (si se contradicen -> NO se opera).
  2) ENTRADA (5m/15m/1h): quiebre de estructura (BOS/CHoCH) a favor del sesgo
     + retesteo de un Order Block fresco + zona de descuento/premium (>= 50% del impulso).
  3) HORARIO: killzone Nueva York 7-11 EST y Londres 2-5 EST (obligatoria en Forex/Oro).
  4) RIESGO: SL tras el Order Block, TP1 = 1:2, TP2 = 1:3, break-even a 1R, regla de 45 min.
  5) Solo se evalúan velas CERRADAS (sin repintado).

Credenciales: NO van en el código. Configúralas en Streamlit -> Settings -> Secrets:
    TELEGRAM_TOKEN = "tu_token_nuevo"
    TELEGRAM_CHAT_IDS = "111111111,222222222"
    BANK_USD = "10"
"""
import html
import os
import threading
import time
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone

import ccxt
import numpy as np
import pandas as pd
import plotly.graph_objects as go
import requests
import streamlit as st
import yfinance as yf
from plotly.subplots import make_subplots

try:
    from zoneinfo import ZoneInfo
    NY_TZ = ZoneInfo("America/New_York")
except Exception:  # sin tzdata: aproximación fija (EST)
    NY_TZ = timezone(timedelta(hours=-5))

st.set_page_config(page_title="Institutional Quant Terminal — SMC Intradía", layout="wide", page_icon="🏦")


# ==========================================
# ⚙️ CONFIGURACIÓN
# ==========================================
def _secreto(nombre, defecto=""):
    try:
        if nombre in st.secrets:
            return str(st.secrets[nombre])
    except Exception:
        pass
    return os.environ.get(nombre, defecto)


TELEGRAM_TOKEN = _secreto("TELEGRAM_TOKEN")
TELEGRAM_CHAT_IDS = [c.strip() for c in _secreto("TELEGRAM_CHAT_IDS").split(",") if c.strip()]
BANK_USD = float(_secreto("BANK_USD", "10"))
RIESGO_POR_OPERACION = 0.02      # 2% del bank

# --- Parámetros de la estrategia ---
SWING_N = 3                      # velas a cada lado para confirmar un swing
LOOKBACK_BOS = 60                # el quiebre debe ser reciente (velas)
DESPLAZAMIENTO_ATR = 1.0         # el impulso debe tener al menos una vela con cuerpo >= 1 ATR
SL_BUFFER_ATR = 0.25             # colchón del SL tras el Order Block
MAX_RIESGO_ATR = 3.0             # SL máximo permitido (en ATR)
MAX_OB_ATR = 2.5                 # tamaño máximo del Order Block (en ATR)
RR_MINIMO = 2.0                  # TP1
RR_OBJETIVO_2 = 3.0              # TP2
MIN_CONFLUENCIAS = 1             # extras mínimos además de las reglas obligatorias
COOLDOWN_SEG = 90 * 60           # no repetir la misma alerta durante 90 min
INTERVALO_ESCANEO = 60           # segundos entre barridos del motor de fondo
ENTRY_TFS = ["5m", "15m"]        # temporalidades de entrada del escáner

CRYPTO_MARKETS = [
    "BTC/USDT", "ETH/USDT", "SOL/USDT", "BNB/USDT",
    "XRP/USDT", "ADA/USDT", "AVAX/USDT", "DOGE/USDT", "LINK/USDT",
]
FOREX_MARKETS = [
    "EURUSD=X", "GBPUSD=X", "USDJPY=X", "USDCHF=X", "AUDUSD=X",
    "USDCAD=X", "NZDUSD=X", "EURJPY=X", "GBPJPY=X", "EURGBP=X",
]
ORO_MARKETS = ["XAUUSD=X"]
ALL_TRACKED_SYMBOLS = CRYPTO_MARKETS + FOREX_MARKETS + ORO_MARKETS
TF_MIN = {"5m": 5, "15m": 15, "1h": 60, "4h": 240, "1d": 1440}


def tipo_mercado(symbol):
    if symbol in CRYPTO_MARKETS:
        return "CRIPTO"
    if symbol in ORO_MARKETS:
        return "ORO"
    return "FOREX"


def _crear_exchange(ex_id):
    try:
        return getattr(ccxt, ex_id)({"enableRateLimit": True})
    except Exception:
        return None


# OKX primero: Binance bloquea muchos servidores de EE.UU. (donde corre Streamlit Cloud)
EXCHANGES_CRIPTO = [e for e in (_crear_exchange("okx"), _crear_exchange("binance")) if e is not None]


# ==========================================
# 🧠 ESTADO COMPARTIDO (sobrevive a los reruns de Streamlit)
# ==========================================
@st.cache_resource
def _crear_estado():
    return {
        "lock": threading.RLock(),
        "alertas": {},
        "datos": {},
        "noticias": {"ts": 0, "items": []},
        "status": "Iniciando motor...",
        "log": [],
    }


ESTADO = _crear_estado()


def registrar(msg):
    with ESTADO["lock"]:
        ESTADO["log"].insert(0, f"{datetime.now(timezone.utc):%H:%M:%S} UTC · {msg}")
        del ESTADO["log"][40:]


# ==========================================
# 🕒 SESIONES Y KILLZONES
# ==========================================
def obtener_sesion_actual():
    ahora = datetime.now(timezone.utc)
    dia, hora = ahora.weekday(), ahora.hour
    if dia == 5 or (dia == 6 and hora < 22) or (dia == 4 and hora >= 22):
        return "FIN DE SEMANA (Forex/Oro cerrado • Cripto 24/7)", False
    sesiones = []
    if 0 <= hora < 9:
        sesiones.append("Tokio")
    if 8 <= hora < 17:
        sesiones.append("Londres")
    if 13 <= hora < 22:
        sesiones.append("Nueva York")
    if hora >= 22 or hora < 7:
        sesiones.append("Sídney")
    if 13 <= hora < 17:
        return "🔥 SOLAPAMIENTO LONDRES + NY (Máxima Liquidez)", True
    return f"Sesión Activa: {' + '.join(sesiones) if sesiones else 'Transición'}", True


def en_killzone(dt_utc=None):
    """Killzones ICT en hora de Nueva York: Londres 2-5, Nueva York 7-11."""
    dt_utc = dt_utc or datetime.now(timezone.utc)
    ny = dt_utc.astimezone(NY_TZ)
    h = ny.hour + ny.minute / 60.0
    if 7 <= h < 11:
        return True, "Killzone Nueva York (7-11 EST)"
    if 2 <= h < 5:
        return True, "Killzone Londres (2-5 EST)"
    return False, "Fuera de killzone"


# ==========================================
# 📰 NOTICIAS
# ==========================================
def obtener_noticias_en_vivo(ttl_segundos=180):
    ahora = time.time()
    with ESTADO["lock"]:
        cache = ESTADO["noticias"]
        if cache["items"] and ahora - cache["ts"] < ttl_segundos:
            return cache["items"]
    feeds = [
        ("Investing Forex", "https://www.investing.com/rss/news_1.rss"),
        ("Investing Oro", "https://www.investing.com/rss/news_11.rss"),
        ("Cointelegraph", "https://cointelegraph.com/rss"),
        ("FXStreet", "https://www.fxstreet.com/rss/news"),
    ]
    noticias = []
    for fuente, url in feeds:
        try:
            resp = requests.get(url, headers={"User-Agent": "Mozilla/5.0"}, timeout=5)
            if resp.status_code != 200:
                continue
            root = ET.fromstring(resp.content)
            for item in root.findall(".//item")[:5]:
                titulo = (item.findtext("title") or "").strip()
                if titulo:
                    noticias.append({
                        "fuente": fuente, "titulo": titulo,
                        "link": (item.findtext("link") or "").strip(),
                        "fecha": (item.findtext("pubDate") or "Reciente").strip()[:22],
                    })
        except Exception:
            continue
    if noticias:
        with ESTADO["lock"]:
            ESTADO["noticias"] = {"ts": ahora, "items": noticias}
    return noticias


# ==========================================
# 📲 TELEGRAM
# ==========================================
def enviar_telegram(mensaje):
    """Devuelve (ok, detalle_error)."""
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_IDS:
        return False, "Faltan TELEGRAM_TOKEN / TELEGRAM_CHAT_IDS en Secrets"
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    ok_alguno, errores = False, []
    for chat_id in TELEGRAM_CHAT_IDS:
        try:
            r = requests.post(
                url,
                json={"chat_id": chat_id, "text": mensaje, "parse_mode": "HTML", "disable_web_page_preview": True},
                timeout=8,
            )
            if r.status_code == 200:
                ok_alguno = True
            else:
                errores.append(f"{chat_id}: HTTP {r.status_code} {r.text[:100]}")
        except Exception as e:
            errores.append(f"{chat_id}: {e}")
    return ok_alguno, "; ".join(errores)


# ==========================================
# 📥 DATOS (solo velas CERRADAS)
# ==========================================
def normalizar_timestamp(serie):
    ts = pd.to_datetime(serie)
    if getattr(ts.dt, "tz", None) is not None:
        ts = ts.dt.tz_convert(None)  # a UTC naive
    return ts


def _descargar_cripto(symbol, tf, limit):
    cols = ["timestamp", "open", "high", "low", "close", "volume"]
    ms_vela = TF_MIN[tf] * 60 * 1000
    for ex in EXCHANGES_CRIPTO:
        try:
            por_llamada = 100 if ex.id == "okx" else 1000
            datos = []
            if limit > 300:  # paginación hacia adelante desde el pasado
                try:
                    since = ex.milliseconds() - limit * ms_vela
                    while since < ex.milliseconds() and len(datos) < limit + 50:
                        lote = ex.fetch_ohlcv(symbol, tf, since=since, limit=por_llamada)
                        if not lote:
                            break
                        datos += lote
                        nuevo = lote[-1][0] + ms_vela
                        if nuevo <= since:
                            break
                        since = nuevo
                except Exception:
                    datos = []
            if not datos:
                datos = ex.fetch_ohlcv(symbol, tf, limit=min(limit, 300))
            if datos:
                d = pd.DataFrame(datos, columns=cols)
                d["timestamp"] = pd.to_datetime(d["timestamp"], unit="ms")
                d = d.dropna(subset=["open", "high", "low", "close"])
                if not d.empty and float(d["close"].iloc[-1]) > 0:
                    return d
        except Exception:
            continue
    return pd.DataFrame()


def _descargar_yf(symbol, tf):
    base = {"5m": "5m", "15m": "15m", "1h": "60m", "4h": "60m", "1d": "1d"}[tf]
    periodos = {"5m": ["1mo", "5d"], "15m": ["1mo", "5d"], "1h": ["3mo", "1mo"], "4h": ["3mo"], "1d": ["1y"]}[tf]
    simbolos = ["XAUUSD=X", "GC=F"] if symbol in ORO_MARKETS else [symbol]
    for sym in simbolos:
        for periodo in periodos:
            try:
                hist = yf.Ticker(sym).history(period=periodo, interval=base)
                if hist.empty or "Close" not in hist.columns:
                    continue
                t = hist.reset_index()
                col_t = "Datetime" if "Datetime" in t.columns else ("Date" if "Date" in t.columns else t.columns[0])
                t = t.rename(columns={col_t: "timestamp", "Open": "open", "High": "high",
                                      "Low": "low", "Close": "close", "Volume": "volume"})
                t["timestamp"] = normalizar_timestamp(t["timestamp"])
                t["volume"] = t["volume"].fillna(0)
                t = t[["timestamp", "open", "high", "low", "close", "volume"]].dropna(
                    subset=["open", "high", "low", "close"])
                if tf == "4h":  # Yahoo no ofrece 4H: se construye desde 60m
                    t = (t.set_index("timestamp")
                         .resample("4h", origin="start_day")
                         .agg({"open": "first", "high": "max", "low": "min", "close": "last", "volume": "sum"})
                         .dropna(subset=["open", "close"]).reset_index())
                if not t.empty and float(t["close"].iloc[-1]) > 0:
                    return t
            except Exception:
                continue
    return pd.DataFrame()


def calcular_atr(df, n=14):
    pc = df["close"].shift()
    tr = pd.concat([df["high"] - df["low"], (df["high"] - pc).abs(), (df["low"] - pc).abs()], axis=1).max(axis=1)
    return tr.ewm(alpha=1 / n, adjust=False).mean()


def calcular_adx(df, n=14):
    """ADX real: una serie con un valor por vela (Wilder)."""
    up = df["high"].diff()
    down = -df["low"].diff()
    plus_dm = pd.Series(np.where((up > down) & (up > 0), up, 0.0), index=df.index)
    minus_dm = pd.Series(np.where((down > up) & (down > 0), down, 0.0), index=df.index)
    atr = calcular_atr(df, n).replace(0, np.nan)
    plus_di = 100 * plus_dm.ewm(alpha=1 / n, adjust=False).mean() / atr
    minus_di = 100 * minus_dm.ewm(alpha=1 / n, adjust=False).mean() / atr
    dx = 100 * (plus_di - minus_di).abs() / (plus_di + minus_di).replace(0, np.nan)
    return dx.ewm(alpha=1 / n, adjust=False).mean().fillna(0.0)


def calcular_vwap(df):
    """VWAP que se reinicia cada día (el original acumulaba sin reiniciar)."""
    if df["volume"].sum() <= 0:
        return df["close"].copy()
    tp = (df["high"] + df["low"] + df["close"]) / 3.0
    dia = df["timestamp"].dt.date
    pv = (tp * df["volume"]).groupby(dia).cumsum()
    v = df["volume"].groupby(dia).cumsum().replace(0, np.nan)
    return (pv / v).fillna(df["close"])


def enriquecer_indicadores(df):
    df = df.copy()
    df["atr"] = calcular_atr(df)
    df["adx"] = calcular_adx(df)
    df["vwap"] = calcular_vwap(df)
    return df


def obtener_datos_mercado(symbol, tf, limit=300, ttl=60):
    clave = f"{symbol}|{tf}|{limit}"
    ahora = time.time()
    with ESTADO["lock"]:
        c = ESTADO["datos"].get(clave)
        if c and ahora - c["ts"] < ttl:
            return c["df"].copy()
    df = _descargar_cripto(symbol, tf, limit) if symbol in CRYPTO_MARKETS else _descargar_yf(symbol, tf)
    if df.empty:
        return df
    df = df.sort_values("timestamp").drop_duplicates("timestamp").reset_index(drop=True)
    df = df.iloc[:-1]  # descarta la vela en formación: solo velas cerradas
    df = df.tail(limit).reset_index(drop=True)
    if len(df) < 30:
        return pd.DataFrame()
    df = enriquecer_indicadores(df)
    with ESTADO["lock"]:
        ESTADO["datos"][clave] = {"ts": ahora, "df": df.copy()}
    return df.copy()


def calcular_volume_profile(df, bins=24):
    """Perfil de volumen (solo referencia visual). Requiere volumen real."""
    if len(df) < 30 or df["volume"].sum() <= 0:
        return None
    p_min, p_max = float(df["low"].min()), float(df["high"].max())
    if p_max <= p_min:
        return None
    edges = np.linspace(p_min, p_max, bins + 1)
    tp = ((df["high"] + df["low"] + df["close"]) / 3.0).values
    idx = np.clip(np.digitize(tp, edges) - 1, 0, bins - 1)
    vol = np.bincount(idx, weights=df["volume"].values, minlength=bins)
    poc = int(np.argmax(vol))
    total, acc, lo, hi = vol.sum(), vol[poc], poc, poc
    while acc < 0.7 * total and (lo > 0 or hi < bins - 1):
        abajo = vol[lo - 1] if lo > 0 else -1
        arriba = vol[hi + 1] if hi < bins - 1 else -1
        if arriba >= abajo:
            hi += 1
            acc += vol[hi]
        else:
            lo -= 1
            acc += vol[lo]
    return {"poc": float((edges[poc] + edges[poc + 1]) / 2), "vah": float(edges[hi + 1]), "val": float(edges[lo])}


# ==========================================
# 🏛️ ESTRUCTURA DE MERCADO (SMC): swings, BOS, CHoCH
# ==========================================
def detectar_swings(df, n=SWING_N):
    h, l = df["high"].values, df["low"].values
    sh, sl = [], []
    for i in range(n, len(df) - n):
        if (h[i - n:i] < h[i]).all() and (h[i + 1:i + n + 1] <= h[i]).all():
            sh.append((i, float(h[i])))
        if (l[i - n:i] > l[i]).all() and (l[i + 1:i + n + 1] >= l[i]).all():
            sl.append((i, float(l[i])))
    return sh, sl


def analizar_estructura(df, n=SWING_N):
    """
    Recorre las velas en orden usando solo swings YA confirmados (sin mirar el futuro).
    Cierre sobre el último swing high -> BOS alcista (o CHoCH si venía bajista).
    Cierre bajo el último swing low  -> BOS bajista (o CHoCH si venía alcista).
    """
    sh, sl = detectar_swings(df, n)
    c = df["close"].values
    eventos, tendencia = [], 0
    ult_sh = ult_sl = None
    ish = isl = 0
    for j in range(len(df)):
        while ish < len(sh) and sh[ish][0] + n <= j:
            ult_sh = sh[ish]
            ish += 1
        while isl < len(sl) and sl[isl][0] + n <= j:
            ult_sl = sl[isl]
            isl += 1
        if ult_sh is not None and c[j] > ult_sh[1]:
            tipo = "CHoCH" if tendencia == -1 else "BOS"
            eventos.append({"tipo": tipo, "dir": 1, "idx": j, "nivel": ult_sh[1], "swing_idx": ult_sh[0]})
            tendencia, ult_sh = 1, None
        elif ult_sl is not None and c[j] < ult_sl[1]:
            tipo = "CHoCH" if tendencia == 1 else "BOS"
            eventos.append({"tipo": tipo, "dir": -1, "idx": j, "nivel": ult_sl[1], "swing_idx": ult_sl[0]})
            tendencia, ult_sl = -1, None
    return {"tendencia": tendencia, "eventos": eventos, "sh": sh, "sl": sl}


def sesgo_desde(df4, df1d):
    """Dirección: estructura 4H confirmada por 1D. Si se contradicen, no se opera."""
    e4, e1 = analizar_estructura(df4), analizar_estructura(df1d)
    t4, t1 = e4["tendencia"], e1["tendencia"]
    if t4 == 0:
        sesgo, texto = 0, "4H sin estructura clara"
    elif t1 != 0 and t1 != t4:
        sesgo, texto = 0, "Conflicto 4H vs 1D: no operar"
    else:
        sesgo, texto = t4, "ALCISTA" if t4 == 1 else "BAJISTA"
    pos = None
    if e4["sh"] and e4["sl"]:
        hi, lo = e4["sh"][-1][1], e4["sl"][-1][1]
        if hi > lo:
            p = (float(df4["close"].iloc[-1]) - lo) / (hi - lo)
            pos = p if 0.0 <= p <= 1.0 else None  # fuera del rango = dato no fiable
    return {"sesgo": sesgo, "t4h": t4, "t1d": t1, "texto": texto, "pos_4h": pos}


SESGO_NULO = {"sesgo": 0, "t4h": 0, "t1d": 0, "texto": "Sin datos", "pos_4h": None}


def obtener_sesgo(symbol):
    d4 = obtener_datos_mercado(symbol, "4h", limit=250, ttl=600)
    d1 = obtener_datos_mercado(symbol, "1d", limit=150, ttl=1800)
    if d4.empty or d1.empty or len(d4) < 40 or len(d1) < 30:
        return dict(SESGO_NULO)
    return sesgo_desde(d4, d1)


# ==========================================
# 🎯 SETUP: BOS + Order Block fresco + descuento/premium
# ==========================================
def _setup_largo(o, h, l, c, ev, atr, swings_liq, last):
    """Evalúa un setup alcista. Para vender se invierten los precios (negativos) y se reutiliza."""
    s_i, j = ev["swing_idx"], ev["idx"]
    pata = s_i + int(np.argmin(l[s_i:j + 1]))      # origen del impulso

    ob_i = pata
    for k in range(pata, max(pata - 6, -1), -1):   # última vela bajista antes del impulso
        if c[k] < o[k]:
            ob_i = k
            break
    ob_lo, ob_hi = float(l[ob_i]), float(h[ob_i])
    if ob_hi - ob_lo > MAX_OB_ATR * atr:
        return None

    cuerpos = np.abs(c[pata:j + 1] - o[pata:j + 1])  # desplazamiento institucional
    if cuerpos.max() < DESPLAZAMIENTO_ATR * atr:
        return None

    if last - 1 >= j + 1 and (l[j + 1:last] <= ob_hi).any():  # OB ya mitigado
        return None

    # Retesteo en la última vela cerrada con confirmación alcista
    if not (l[last] <= ob_hi and c[last] > ob_lo and c[last] > o[last]):
        return None

    precio = float(c[last])
    rango_hi, rango_lo = float(h[pata:last + 1].max()), float(l[pata])
    if rango_hi <= rango_lo or precio <= rango_lo:
        return None
    retro = (rango_hi - precio) / (rango_hi - rango_lo)
    if retro < 0.5:                                 # solo zona de descuento (>= 50%)
        return None

    base = min(ob_lo, float(l[last]))
    sl = base - SL_BUFFER_ATR * atr
    riesgo = precio - sl
    if riesgo > MAX_RIESGO_ATR * atr:
        return None
    if riesgo < 0.3 * atr:
        riesgo = 0.3 * atr
        sl = precio - riesgo

    fvg = any(l[k + 1] > h[k - 1] for k in range(pata + 1, j))
    sweep = False
    for idx_sw, nivel in [s for s in swings_liq if s[0] < last - SWING_N][-3:]:
        for k in range(max(idx_sw + 1, last - 10), last + 1):
            if l[k] < nivel and c[k] > nivel:
                sweep = True
    return {"ob_lo": ob_lo, "ob_hi": ob_hi, "entrada": precio, "sl": float(sl),
            "riesgo": float(riesgo), "fvg": fvg, "sweep": sweep, "retro": float(retro)}


def evaluar_setup(df, sesgo, mercado, tf):
    """Devuelve la señal (dict) o None. Usa únicamente velas cerradas."""
    if len(df) < 60 or sesgo["sesgo"] == 0:
        return None
    d = sesgo["sesgo"]
    est = analizar_estructura(df)
    if est["tendencia"] != d:
        return None
    last = len(df) - 1
    atr = float(df["atr"].iloc[-1])
    if not np.isfinite(atr) or atr <= 0:
        return None

    ts_cierre = (df["timestamp"].iloc[-1] + pd.Timedelta(minutes=TF_MIN[tf])).to_pydatetime().replace(tzinfo=timezone.utc)
    kz_ok, kz_txt = en_killzone(ts_cierre)
    if mercado != "CRIPTO" and not kz_ok:
        return None

    o, h, l, c = (df[x].values.astype(float) for x in ("open", "high", "low", "close"))
    if d == -1:
        o, h, l, c = -o, -l, -h, -c
        swings_liq = [(i, -p) for i, p in est["sh"]]
    else:
        swings_liq = est["sl"]

    eventos = [e for e in est["eventos"] if e["dir"] == d and e["idx"] < last and last - e["idx"] <= LOOKBACK_BOS][-3:]
    r, ev_ok = None, None
    for ev in reversed(eventos):
        r = _setup_largo(o, h, l, c, ev, atr, swings_liq, last)
        if r:
            ev_ok = ev
            break
    if not r:
        return None

    entrada, sl, ob_lo, ob_hi = r["entrada"], r["sl"], r["ob_lo"], r["ob_hi"]
    if d == -1:
        entrada, sl, ob_lo, ob_hi = -entrada, -sl, -ob_hi, -ob_lo
    riesgo = r["riesgo"]

    conf = []
    if r["fvg"]:
        conf.append("FVG en el impulso")
    if r["sweep"]:
        conf.append("Barrido de liquidez previo")
    if float(df["adx"].iloc[-1]) >= 20:
        conf.append("ADX >= 20 (hay movimiento)")
    if df["volume"].tail(50).sum() > 0:
        vw = float(df["vwap"].iloc[-1])
        if (d == 1 and entrada > vw) or (d == -1 and entrada < vw):
            conf.append("A favor del VWAP")
    pos = sesgo.get("pos_4h")
    if pos is not None and ((d == 1 and pos <= 0.5) or (d == -1 and pos >= 0.5)):
        conf.append("Descuento/Premium en rango 4H")
    if mercado == "CRIPTO" and kz_ok:
        conf.append(kz_txt)
    if len(conf) < MIN_CONFLUENCIAS:
        return None

    return {
        "id": f"{'L' if d == 1 else 'S'}_{ev_ok['idx']}",
        "dir": d,
        "direccion": "COMPRA (LONG) 🟢" if d == 1 else "VENTA (SHORT) 🔴",
        "modelo": f"{ev_ok['tipo']} + Order Block fresco ({'alcista' if d == 1 else 'bajista'})",
        "entrada": entrada, "sl": sl,
        "tp1": entrada + d * RR_MINIMO * riesgo,
        "tp2": entrada + d * RR_OBJETIVO_2 * riesgo,
        "be": entrada + d * riesgo,
        "riesgo": riesgo, "ob_lo": ob_lo, "ob_hi": ob_hi,
        "retro": r["retro"], "confluencias": conf, "killzone": kz_txt,
        "evento": ev_ok["tipo"], "ts": df["timestamp"].iloc[-1],
    }


# ==========================================
# 💰 GESTIÓN DE RIESGO (bank configurable)
# ==========================================
def fmt(x):
    return f"{x:,.2f}" if abs(x) >= 100 else (f"{x:,.4f}" if abs(x) >= 1 else f"{x:,.6f}")


def calcular_gestion(precio, sl, symbol):
    riesgo_usd = BANK_USD * RIESGO_POR_OPERACION
    dist = max(abs(precio - sl), 1e-9)
    mercado = tipo_mercado(symbol)
    base = f"Riesgo máx ${riesgo_usd:.2f} ({RIESGO_POR_OPERACION * 100:.0f}% de ${BANK_USD:,.0f})"
    if mercado == "CRIPTO":
        unidades = riesgo_usd / dist
        nocional = unidades * precio
        txt = f"{base} | Tamaño: {unidades:.5f} {symbol.split('/')[0]} (~${nocional:,.2f})"
        if nocional > BANK_USD:
            txt += " ⚠️ supera tu bank (requeriría apalancamiento)"
        return txt
    if mercado == "ORO":
        riesgo_min = dist * 1.0  # 0.01 lote = 1 onza
        pips_txt = f"SL ${dist:,.2f}"
    else:
        pip = 0.01 if "JPY" in symbol else 0.0001
        riesgo_min = (dist / pip) * 0.10  # ~ $0.10 por pip con 0.01 lote (aprox.)
        pips_txt = f"SL {dist / pip:.1f} pips"
    if riesgo_min > riesgo_usd:
        return (f"{base} | {pips_txt}: el lote mínimo 0.01 arriesga ~${riesgo_min:.2f} "
                f"(más de lo permitido). ⚠️ Con este bank solo en DEMO.")
    lote = max(0.01, round(riesgo_usd / riesgo_min * 0.01, 2))
    return f"{base} | {pips_txt} | Lote aprox: {lote}"


def construir_mensaje(symbol, tf, mercado, sesgo, s):
    e = html.escape
    p = (
        f"1) Sesgo 4H/1D {e(sesgo['texto'])}; {s['evento']} en {tf} y retesteo del Order Block "
        f"({fmt(s['ob_lo'])} - {fmt(s['ob_hi'])}).\n"
        f"2) Retroceso del impulso: {s['retro'] * 100:.0f}% (zona {'descuento' if s['dir'] == 1 else 'premium'}).\n"
        f"3) TP1 {fmt(s['tp1'])} (1:{RR_MINIMO:.0f}) | TP2 {fmt(s['tp2'])} (1:{RR_OBJETIVO_2:.0f}).\n"
        f"4) Al llegar a 1R ({fmt(s['be'])}) mueve el SL a break-even.\n"
        f"5) Regla de 45 min: si no avanza, cierra manualmente."
    )
    return (
        f"🏦 <b>SEÑAL SMC INTRADÍA</b>\n"
        f"🌐 <b>{mercado}</b> | <code>{e(symbol)}</code> | TF <code>{tf}</code>\n"
        f"🧭 Sesgo: 4H <code>{s_txt(sesgo['t4h'])}</code> | 1D <code>{s_txt(sesgo['t1d'])}</code>\n"
        f"🎯 <b>{e(s['modelo'])}</b>\n"
        f"➡️ <b>{e(s['direccion'])}</b>\n"
        f"💲 Entrada: <code>{fmt(s['entrada'])}</code>\n"
        f"🛡️ SL: <code>{fmt(s['sl'])}</code>\n"
        f"✅ TP1: <code>{fmt(s['tp1'])}</code> | TP2: <code>{fmt(s['tp2'])}</code>\n"
        f"⭐ Confluencias: {e(', '.join(s['confluencias']))}\n"
        f"🕒 {e(s['killzone'])}\n"
        f"💰 {e(calcular_gestion(s['entrada'], s['sl'], symbol))}\n"
        f"📋 <b>Plan:</b>\n{p}"
    )


def s_txt(t):
    return {1: "ALCISTA", -1: "BAJISTA"}.get(t, "NEUTRO")


# ==========================================
# 🤖 MOTOR EN FONDO
# ==========================================
def ejecutar_logica_autopiloto(forzado=False):
    sesion_txt, forex_abierto = obtener_sesion_actual()
    kz_ok, kz_txt = en_killzone()
    enviadas, revisados = 0, 0
    for symbol in ALL_TRACKED_SYMBOLS:
        mercado = tipo_mercado(symbol)
        if mercado != "CRIPTO" and (not forex_abierto or (not kz_ok and not forzado)):
            continue
        try:
            sesgo = obtener_sesgo(symbol)
        except Exception as e:
            registrar(f"Error sesgo {symbol}: {e}")
            continue
        if sesgo["sesgo"] == 0:
            continue
        for tf in ENTRY_TFS:
            try:
                df = obtener_datos_mercado(symbol, tf, limit=300, ttl=50)
                if df.empty:
                    continue
                revisados += 1
                s = evaluar_setup(df, sesgo, mercado, tf)
                if s:
                    clave = f"{symbol}|{tf}|{s['dir']}"
                    with ESTADO["lock"]:
                        reciente = time.time() - ESTADO["alertas"].get(clave, 0) < COOLDOWN_SEG
                    if not reciente:
                        ok, err = enviar_telegram(construir_mensaje(symbol, tf, mercado, sesgo, s))
                        if ok:
                            with ESTADO["lock"]:
                                ESTADO["alertas"][clave] = time.time()
                            enviadas += 1
                            registrar(f"✅ Alerta enviada {symbol} {tf} {s['direccion']}")
                        else:
                            registrar(f"❌ Telegram falló ({symbol} {tf}): {err}")
            except Exception as e:
                registrar(f"Error {symbol} {tf}: {e}")
            time.sleep(0.1)
    ESTADO["status"] = (f"{'✅ Barrido manual' if forzado else '⚡ Motor activo'} · {sesion_txt} · {kz_txt} · "
                        f"{revisados} gráficos revisados · {enviadas} alertas")


@st.cache_resource
def iniciar_hilo_autopiloto():
    def loop():
        time.sleep(3)
        while True:
            try:
                ejecutar_logica_autopiloto()
            except Exception as e:
                registrar(f"Error motor: {e}")
            time.sleep(INTERVALO_ESCANEO)
    t = threading.Thread(target=loop, daemon=True)
    t.start()
    return t


# ==========================================
# 🧪 BACKTEST (sin mirar el futuro)
# ==========================================
def backtest(symbol, tf, max_velas=12, limite=1000):
    mercado = tipo_mercado(symbol)
    df = obtener_datos_mercado(symbol, tf, limit=limite, ttl=300)
    d4 = obtener_datos_mercado(symbol, "4h", limit=500, ttl=600)
    d1 = obtener_datos_mercado(symbol, "1d", limit=300, ttl=1800)
    if df.empty or d4.empty or d1.empty:
        return pd.DataFrame(), 0
    min_hist, i = 120, 120
    trades, memo = [], {}
    while i < len(df) - 1:
        ts_cierre = df["timestamp"].iloc[i] + pd.Timedelta(minutes=TF_MIN[tf])
        h4 = d4[d4["timestamp"] + pd.Timedelta(minutes=240) <= ts_cierre].tail(200)
        h1 = d1[d1["timestamp"] + pd.Timedelta(days=1) <= ts_cierre].tail(120)
        if len(h4) < 40 or len(h1) < 30:
            i += 1
            continue
        k = (h4["timestamp"].iloc[-1], h1["timestamp"].iloc[-1])
        if k not in memo:
            memo[k] = sesgo_desde(h4.reset_index(drop=True), h1.reset_index(drop=True))
        sesgo = memo[k]
        sub = df.iloc[max(0, i - 299):i + 1].reset_index(drop=True)
        s = evaluar_setup(sub, sesgo, mercado, tf) if sesgo["sesgo"] != 0 else None
        if not s:
            i += 1
            continue
        d, ent, sl, tp, riesgo = s["dir"], s["entrada"], s["sl"], s["tp1"], s["riesgo"]
        resultado, salida = None, i
        for j in range(i + 1, min(i + 1 + max_velas, len(df))):
            hi, lo = float(df["high"].iloc[j]), float(df["low"].iloc[j])
            salida = j
            if (d == 1 and lo <= sl) or (d == -1 and hi >= sl):  # SL primero (conservador)
                resultado = -1.0
                break
            if (d == 1 and hi >= tp) or (d == -1 and lo <= tp):
                resultado = float(RR_MINIMO)
                break
        if resultado is None:  # cierre por tiempo
            cierre = float(df["close"].iloc[salida])
            resultado = d * (cierre - ent) / riesgo
        trades.append({"fecha": df["timestamp"].iloc[i], "dir": "LONG" if d == 1 else "SHORT",
                       "entrada": ent, "sl": sl, "tp": tp, "R": round(resultado, 2)})
        i = salida + 1
    return pd.DataFrame(trades), len(df)


# ==========================================
# 📊 INTERFAZ
# ==========================================
iniciar_hilo_autopiloto()

sesion_ui, _ = obtener_sesion_actual()
kz_ui_ok, kz_ui_txt = en_killzone()
st.markdown("<h1 style='text-align:center;color:#00FFAA;'>🏦 INSTITUTIONAL QUANT TERMINAL — SMC INTRADÍA</h1>",
            unsafe_allow_html=True)
st.markdown(f"<p style='text-align:center;color:gray;'>{sesion_ui} • {kz_ui_txt} • Sesgo 4H/1D → Entrada 5m/15m</p>",
            unsafe_allow_html=True)
st.markdown("---")

st.sidebar.header("🌐 Configuración")
symbol = st.sidebar.selectbox("Activo:", ALL_TRACKED_SYMBOLS)
timeframe = st.sidebar.selectbox("Temporalidad de entrada:", ["5m", "15m", "1h"], index=1)
st.sidebar.info(ESTADO["status"])

if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_IDS:
    st.sidebar.warning("Telegram sin configurar: agrega TELEGRAM_TOKEN y TELEGRAM_CHAT_IDS en Secrets.")

if st.sidebar.button("🔔 Probar Telegram Ahora"):
    ok, err = enviar_telegram("✅ <b>Prueba de conexión exitosa</b>\nTu bot SMC Intradía está activo.")
    if ok:
        st.sidebar.success("¡Mensaje de prueba enviado!")
    else:
        st.sidebar.error(f"No se pudo enviar: {err}")

if st.sidebar.button("⚡ Forzar Escaneo Ahora"):
    with st.spinner("Escaneando todos los mercados..."):
        ejecutar_logica_autopiloto(forzado=True)
    st.sidebar.success("¡Escaneo completado!")

with st.sidebar.expander("📜 Registro del motor"):
    for linea in ESTADO["log"][:15] or ["Sin eventos todavía."]:
        st.caption(linea)

tab_panel, tab_bt = st.tabs(["📈 Panel en vivo", "🧪 Backtest"])


@st.fragment(run_every="10s")
def render_panel(symbol, timeframe):
    mercado = tipo_mercado(symbol)
    sesgo = obtener_sesgo(symbol)
    df = obtener_datos_mercado(symbol, timeframe, limit=300, ttl=50)
    if df.empty:
        st.error(f"Sin datos reales disponibles para {symbol}.")
        return
    senal = evaluar_setup(df, sesgo, mercado, timeframe)
    vp = calcular_volume_profile(df.tail(200))
    vista = df.tail(120)
    tiene_vol = float(vista["volume"].sum()) > 0

    col_g, col_p = st.columns([2.5, 1])
    with col_g:
        filas = 2 if tiene_vol else 1
        fig = make_subplots(rows=filas, cols=1, shared_xaxes=True, vertical_spacing=0.03,
                            row_heights=[0.8, 0.2] if tiene_vol else [1.0])
        fig.add_trace(go.Candlestick(
            x=vista["timestamp"], open=vista["open"], high=vista["high"], low=vista["low"], close=vista["close"],
            increasing_line_color="#00FFAA", decreasing_line_color="#FF3333", name="Precio"), row=1, col=1)
        if tiene_vol:
            fig.add_trace(go.Scatter(x=vista["timestamp"], y=vista["vwap"], line=dict(color="#FF00FF", width=1.5),
                                     name="VWAP"), row=1, col=1)
            colores = ["#00FFAA" if r["close"] >= r["open"] else "#FF3333" for _, r in vista.iterrows()]
            fig.add_trace(go.Bar(x=vista["timestamp"], y=vista["volume"], marker_color=colores, name="Volumen"),
                          row=2, col=1)
        if vp:
            fig.add_hline(y=vp["poc"], line_dash="dash", line_color="#FFA500", annotation_text="POC", row=1, col=1)
            fig.add_hline(y=vp["vah"], line_dash="dot", line_color="#8888FF", annotation_text="VAH", row=1, col=1)
            fig.add_hline(y=vp["val"], line_dash="dot", line_color="#8888FF", annotation_text="VAL", row=1, col=1)
        if senal:
            fig.add_hrect(y0=senal["ob_lo"], y1=senal["ob_hi"], fillcolor="#FFD700", opacity=0.25, line_width=0,
                          row=1, col=1)
            fig.add_hline(y=senal["entrada"], line_color="#FFFFFF", annotation_text="Entrada", row=1, col=1)
            fig.add_hline(y=senal["sl"], line_color="#FF3333", annotation_text="SL", row=1, col=1)
            fig.add_hline(y=senal["tp1"], line_color="#00FFAA", annotation_text="TP1", row=1, col=1)
        fig.update_layout(template="plotly_dark", xaxis_rangeslider_visible=False, height=580,
                          margin=dict(l=10, r=10, t=10, b=10), showlegend=False)
        st.plotly_chart(fig)

    with col_p:
        st.markdown(f"### ⚙️ Mercado: {mercado}")
        u = df.iloc[-1]
        precio = float(u["close"])
        st.metric(f"{symbol} [{timeframe}] (última vela cerrada)", f"${fmt(precio)}",
                  f"{precio - float(df.iloc[-2]['close']):,.4f}")
        st.caption(f"🧭 **4H:** `{s_txt(sesgo['t4h'])}` | **1D:** `{s_txt(sesgo['t1d'])}` | "
                   f"**ADX:** `{float(u['adx']):.1f}`")
        st.caption(f"🎯 Sesgo operativo: **{sesgo['texto']}**")
        if mercado != "CRIPTO" and not kz_ui_ok:
            st.caption("⏳ Forex/Oro: fuera de killzone, no se generan señales.")
        if senal:
            st.success(f"**{senal['modelo']}**\n\n**{senal['direccion']}**")
            st.markdown(
                f"- Entrada: `{fmt(senal['entrada'])}`\n- SL: `{fmt(senal['sl'])}`\n"
                f"- TP1 (1:{RR_MINIMO:.0f}): `{fmt(senal['tp1'])}`\n- TP2 (1:{RR_OBJETIVO_2:.0f}): `{fmt(senal['tp2'])}`\n"
                f"- Break-even a 1R: `{fmt(senal['be'])}`\n- Confluencias: {', '.join(senal['confluencias'])}")
            st.info(calcular_gestion(senal["entrada"], senal["sl"], symbol))
        else:
            st.caption("Sin setup válido en esta vela. El bot espera: quiebre de estructura a favor del sesgo + "
                       "retesteo de un Order Block fresco en descuento/premium.")

    st.markdown("---")
    st.markdown("### 📰 Noticias de Mercados en Vivo")
    noticias = obtener_noticias_en_vivo()
    if noticias:
        cols = st.columns(2)
        for i, n in enumerate(noticias[:8]):
            with cols[i % 2]:
                st.markdown(f"- **[{n['fuente']}]** [{n['titulo']}]({n['link']}) *({n['fecha']})*")


with tab_panel:
    render_panel(symbol, timeframe)

with tab_bt:
    st.markdown("Simula la estrategia sobre datos históricos **sin mirar el futuro** (sesgo 4H/1D calculado "
                "solo con velas ya cerradas en cada momento).")
    max_velas = st.slider("Velas máximas dentro de la operación", 3, 40, 12)
    if st.button("▶️ Ejecutar backtest en este activo/temporalidad"):
        with st.spinner("Simulando..."):
            res, n_velas = backtest(symbol, timeframe, max_velas=max_velas)
        if n_velas == 0:
            st.error("No se pudieron descargar datos suficientes.")
        elif res.empty:
            st.warning(f"Sin operaciones en {n_velas} velas analizadas. No significa que falle: puede que el "
                       f"historial disponible sea corto o que no hubiera setups (Forex/Oro solo operan en killzone).")
        else:
            ganadoras = res[res["R"] > 0]
            perdedoras = res[res["R"] <= 0]
            pf = (ganadoras["R"].sum() / abs(perdedoras["R"].sum())) if len(perdedoras) and perdedoras["R"].sum() != 0 else float("inf")
            equity = res["R"].cumsum()
            dd = float((equity - equity.cummax()).min())
            c1, c2, c3, c4 = st.columns(4)
            c1.metric("Operaciones", len(res))
            c2.metric("% Acierto", f"{len(ganadoras) / len(res) * 100:.0f}%")
            c3.metric("Resultado total", f"{res['R'].sum():.1f} R")
            c4.metric("Factor de beneficio", "∞" if pf == float("inf") else f"{pf:.2f}")
            st.caption(f"Velas analizadas: {n_velas} · Máx. caída: {dd:.1f} R · Promedio por operación: "
                       f"{res['R'].mean():.2f} R")
            st.line_chart(equity.reset_index(drop=True))
            st.dataframe(res)
            st.warning("No incluye spread, comisiones ni deslizamiento, y con pocas operaciones el resultado "
                       "no es concluyente. Pruébalo en varios activos y en DEMO antes de usar dinero real.")
