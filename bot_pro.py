# -*- coding: utf-8 -*-
"""
BOT PRO - Institutional Quant Terminal (TODO en un solo archivo)
=================================================================
Mercados: BTC/USDT · EUR/USD · GBP/USD · USD/JPY · XAU/USD (Oro)

  python bot_pro.py --ping          -> igual, y manda "✅ Bot activo" a Telegram al arrancar
  python bot_pro.py                 -> bot 24/7: escanea al cierre de cada vela y avisa por Telegram
  python bot_pro.py --una-vez       -> una sola pasada (es lo que usa GitHub Actions)
  python bot_pro.py --crear-github  -> crea .github/workflows/bot.yml para correrlo gratis en GitHub
  python bot_pro.py backtest --activos BTC/USDT "GBP/USD" -> backtest vela a vela
  streamlit run bot_pro.py          -> panel + MOTOR 24/7 en segundo plano (Streamlit Cloud): escanea al cierre de
                                       cada vela de 5m y avisa por Telegram aunque nadie tenga la pagina abierta.
                                       Streamlit Cloud duerme la app tras 12 h sin visitas: para 24/7 real
                                       usa tambien GitHub Actions (python bot_pro.py --crear-github).

Estrategias y en que mercado corre cada una (ESTRATEGIAS_ACTIVO):
  SMC      (todos)        : sesgo 1H -> BOS -> Order Block + OTE + FVG + POC + OB fresco.
  Barrido  (todos)        : estructura 1H -> barrido de liquidez (dia anterior / Asia) -> cambio de estructura.
  Tortugas (BTC, JPY, Oro): Sistema 1 de las Tortugas de Richard Dennis: ruptura de 20 dias,
                            stop 2N (ATR 20 dias), salida en el minimo/maximo de 10 dias.
  Londres  (EUR, GBP)     : Ruptura de Londres: rango 00-07 GMT, primera vela que cierra fuera
                            entre 07 y 11 GMT, stop 1,5 pips tras el ultimo swing, objetivo 1,5R.
  Setup91  (todos)        : Setup 9.1 de Larry Williams, el mas difundido en Brasil (Palex; Wolwacz 2010):
                            giro de la media exponencial de 9, orden stop en el extremo de la vela
                            senal, stop en el otro extremo, salida cuando la MME9 gira en contra.
  ORB      (todos)        : Opening Range Breakout de Zarattini y Aziz (2023/2024): direccion de la 1a vela
                            de la apertura (Londres, Tokio o Wall Street), stop en su otro extremo, 10R o fin de sesion.
  HolyGrail(todos)        : Holy Grail de Linda Raschke: ADX>30 subiendo + retroceso a la EMA 20, orden stop.
  VWAP     (todos)        : Confluencia VWAP + ADX + EMA9/21 + CVD + OB/FVG del Quant Terminal, TP 2,5R.
  Ruido    (todos)        : Noise Area de Zarattini, Aziz y Barbon ("Beat the Market"): fuera de la zona de ruido
                            de 14 dias y del lado del VWAP, control cada 30 min, stop dinamico, cierre fin de sesion.
  HoraBTC  (BTC)          : estacionalidad de Bitcoin (Quantpedia): compra 21:00 UTC, vende 23:00 UTC.
  NocheOro (Oro)          : efecto noche del oro (Blose et al. 2018): compra 19:00 NY, cierra 08:00 NY.
  ORBRetest(NY + DAX)     : rango de los primeros 30 min de Wall Street (DAX: Frankfurt), ruptura y retest, TP 2R.
  AsiaSweep(Londres)      : falsa ruptura del rango asiatico en la apertura de Londres + quiebre de estructura,
                            objetivo el lado opuesto del rango.
  EMAVWAP  (todos)        : tendencia con VWAP diario + retroceso a EMA21/VWAP + giro de EMA9, TP 2R.
  Hyenuk Chu ("Hazlo simple"): ORB15 (rango 9:30-9:45 NY, ruptura con volumen, 2R) · MaxHist (rompe el maximo
                            del historial diario en tendencia, solo compras) · Medias (rebote en EMA8 5m / SMA20 15m
                            con SMA200 + martillo/envolvente) · Gap (cierre o continuacion del gap de apertura, indices).
  Dr. Inversor (todos)    : DI_EMA (EMA200 de 15m da el sesgo, rebote en EMA20 con vela fuerte) · DI_Ruptura
                            (rompe el max/min de ayer con volumen y entra en el pullback) · DI_RSI (divergencia
                            RSI 15m + quiebre de estructura). Todas 2R y descartan si el objetivo esta bloqueado.
  Gotobi   (USD/JPY)      : dias de pago en Japon (5, 10, 15...): compra 8:00 JST, cierre en el fixing 9:55 JST.

Horario de alertas: BTC 24/7 · Oro 24h (dom 18:00 a vie 17:00 NY, pausa 17-18) ·
Forex en killzones Londres (2-5 NY) y Nueva York (7-10 NY). Se cambia en KZ_DEFAULT.
Todas las decisiones se toman SOLO con velas cerradas (sin repintado).
Herramienta educativa. No es asesoria financiera. Prueba primero en demo.
"""
import argparse
import json
import os
import time
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd
import requests

try:
    import ccxt
except Exception:  # pragma: no cover
    ccxt = None
try:
    import yfinance as yf
except Exception:  # pragma: no cover
    yf = None

NY = ZoneInfo("America/New_York")
UTC = timezone.utc

DEFAULT_SECRETS = {
    "TELEGRAM_TOKEN": "9148434925:AAH_AuswS-GGPHadFBwcrDOUUIwBFbXvtDs",
    "TELEGRAM_CHAT_IDS": "1496321437,5880373359",
}

# ----------------------------------------------------------------- ACTIVOS
# Los 5 mercados que vigila el bot. Para agregar otro, sumalo aqui y en ESTRATEGIAS_ACTIVO.
CRIPTO = ["BTC/USDT"]
FOREX = {"EUR/USD": "EURUSD=X", "GBP/USD": "GBPUSD=X", "USD/JPY": "USDJPY=X", "USD/CAD": "USDCAD=X"}
ORO = {"XAU/USD (Oro)": "GC=F"}   # GC=F = futuro COMEX (100 oz), no spot
INDICES = {"NASDAQ 100 (NQ)": "NQ=F", "S&P 500 (ES)": "ES=F", "DOW 30 (YM)": "YM=F", "DAX 40": "^GDAXI"}
# USD por punto de 1 micro contrato (MNQ, MES, MYM, micro DAX FDXS ~1 EUR)
PUNTO_MICRO = {"NASDAQ 100 (NQ)": 2.0, "S&P 500 (ES)": 5.0, "DOW 30 (YM)": 0.5, "DAX 40": 1.1}

# Que estrategias corre cada mercado (ver la cabecera del archivo)
ESTRATEGIAS_ACTIVO = {
    "BTC/USDT": ["SMC", "Barrido", "Tortugas", "Setup91", "ORB", "HolyGrail", "VWAP", "Ruido", "HoraBTC", "ORBRetest", "EMAVWAP", "ORB15", "MaxHist", "Medias", "DI_EMA", "DI_Ruptura", "DI_RSI"],
    "EUR/USD": ["SMC", "Barrido", "Londres", "Setup91", "ORB", "HolyGrail", "VWAP", "Ruido", "ORBRetest", "AsiaSweep", "EMAVWAP", "ORB15", "Medias", "DI_EMA", "DI_Ruptura", "DI_RSI", "Judas"],
    "GBP/USD": ["SMC", "Barrido", "Londres", "Setup91", "ORB", "HolyGrail", "VWAP", "Ruido", "AsiaSweep", "EMAVWAP", "Medias", "DI_EMA", "DI_Ruptura", "DI_RSI", "Judas"],
    "USD/JPY": ["SMC", "Barrido", "Tortugas", "Setup91", "ORB", "HolyGrail", "Gotobi", "VWAP", "Ruido", "ORBRetest", "AsiaSweep", "EMAVWAP", "ORB15", "Medias", "DI_EMA", "DI_Ruptura", "DI_RSI", "Judas"],
    "USD/CAD": ["SMC", "Barrido", "Londres", "Setup91", "ORB", "HolyGrail", "VWAP", "Ruido", "ORBRetest", "AsiaSweep", "EMAVWAP", "ORB15", "Medias", "DI_EMA", "DI_Ruptura", "DI_RSI", "Judas"],
    "XAU/USD (Oro)": ["SMC", "Barrido", "Tortugas", "Setup91", "ORB", "HolyGrail", "VWAP", "Ruido", "NocheOro", "ORBRetest", "AsiaSweep", "EMAVWAP", "ORB15", "MaxHist", "Medias", "DI_EMA", "DI_Ruptura", "DI_RSI", "Judas"],
    "NASDAQ 100 (NQ)": ["SMC", "Barrido", "ORB", "VWAP", "Ruido", "ORBRetest", "EMAVWAP", "ORB15", "MaxHist", "Medias", "Gap", "DI_EMA", "DI_Ruptura", "DI_RSI", "Judas"],
    "S&P 500 (ES)": ["SMC", "Barrido", "ORB", "VWAP", "Ruido", "ORBRetest", "EMAVWAP", "ORB15", "MaxHist", "Medias", "Gap", "DI_EMA", "DI_Ruptura", "DI_RSI", "Judas"],
    "DOW 30 (YM)": ["SMC", "Barrido", "ORB", "VWAP", "Ruido", "ORBRetest", "EMAVWAP", "ORB15", "MaxHist", "Medias", "Gap", "DI_EMA", "DI_Ruptura", "DI_RSI", "Judas"],
    "DAX 40": ["SMC", "Barrido", "ORB", "VWAP", "Ruido", "ORBRetest", "AsiaSweep", "EMAVWAP", "Medias", "DI_EMA", "DI_Ruptura", "DI_RSI", "Judas"],
}

# ================================================================= PERFIL GANADOR
# Combinaciones (mercado, temporalidad) -> estrategias que PASARON la prueba de 6 meses (Dukascopy/ccxt, costes
# incluidos): positivas, siguen positivas con costes x2 y ganan en la 1a y la 2a mitad del periodo.
# En vivo el bot solo avisa de estas. El backtest sigue probando todas.
#   Confluencia VWAP Oro 15m: 162 ops, +0,14 R, PF 1,26, costes x2 +0,08 R  -> ROBUSTA
#   Tortugas Oro / BTC (15m): pocas operaciones pero positivas en 58 dias y en 6 meses (sistema diario clasico)
# Descartadas a los 6 meses (habian salido bien en 58 dias): ORB Oro, Ruptura de Londres, VWAP GBP/USD,
# Tortugas USD/JPY.
#   ORB + Retest DAX 15m (guia de sesiones): 30 ops, +0,26 R, PF 1,53, 5/6 meses positivos -> ROBUSTA
#   ORB apertura DAX 15m: 112 ops, +0,24 R, PF 1,34, 5/7 meses -> ROBUSTA
#   ORB apertura S&P 500 15m: 116 ops, +0,18 R, PF 1,25, 5/7 meses -> ROBUSTA
#   EMA 20/200 (Dr. Inversor) DAX 15m: 44 ops, +0,15 R, PF 1,26, 5/6 meses -> ROBUSTA
GANADORAS = {
    ("XAU/USD (Oro)", "15m"): ["VWAP", "Tortugas"],
    ("BTC/USDT", "15m"): ["Tortugas"],
    ("DAX 40", "15m"): ["ORBRetest", "ORB", "DI_EMA"],
    ("S&P 500 (ES)", "15m"): ["ORB"],
}

ACTIVOS = {}
for _c in CRIPTO:
    ACTIVOS[_c] = {"tipo": "cripto", "yf": _c.replace("/USDT", "-USD")}
for _k, _v in FOREX.items():
    ACTIVOS[_k] = {"tipo": "forex", "yf": _v}
for _k, _v in ORO.items():
    ACTIVOS[_k] = {"tipo": "oro", "yf": _v}
for _k, _v in INDICES.items():
    ACTIVOS[_k] = {"tipo": "indice", "yf": _v}

ADX_MIN = {"cripto": 18, "forex": 20, "oro": 20, "indice": 20}
EXCHANGES = ["kucoin", "okx", "bybit", "binance"]   # binance al final: bloquea IPs de EE.UU.
TF_MIN = {"5m": 5, "15m": 15, "30m": 30, "1h": 60, "4h": 240, "1d": 1440}
# Yahoo: intervalos < 1h solo ultimos 60 dias; 1h solo ultimos 730 dias.
YF_CFG = {"5m": ("5m", "5d"), "15m": ("15m", "10d"), "1h": ("1h", "60d"), "1d": ("1d", "2y")}
YF_LIMITE_DIAS = {"5m": 59, "15m": 59, "30m": 59, "1h": 729, "1d": 3650}
COOLDOWN_H = 4
VENTANA = 300          # velas de entrada que mira el motor (igual en vivo y en backtest)
VENTANA_CTX = 200      # velas 1H de contexto

# Killzones en hora de Nueva York (ICT). Se pueden editar.
KILLZONES = {
    "Asia": (19.0, 22.0),
    "Londres": (2.0, 5.0),
    "Nueva York": (7.0, 10.0),
    "London Close": (10.0, 12.0),
}
# Lista vacia = alertas 24 horas (siempre que el mercado este abierto)
KZ_DEFAULT = {"cripto": [],                          # BTC: 24/7
              "forex": ["Londres", "Nueva York"],
              "oro": [],                             # Oro: 24h de domingo 18:00 a viernes 17:00 NY
              "indice": ["Nueva York"]}

# ================================================================= TU CUENTA (EDITA AQUI)
# Copia estos datos de tu MT5/MT4: clic derecho en el simbolo -> "Especificacion" (Specification):
#   "Tamano del contrato" (Contract size) y "Volumen minimo" (Minimum volume).
# Valores por defecto = cuenta estandar habitual: forex 100.000 unidades por lote, oro 100 oz por lote.
# Si tu cuenta es CENT, escribe aqui el tamano de contrato que muestre tu MT5 para ese simbolo.
CUENTA = {
    "banco": 10.0,              # capital en USD
    "riesgo_pct": 2.0,          # % del capital que arriesgas por operacion ($0.20 con $10)
    "riesgo_max_pct": 5.0,      # si el lote minimo obliga a arriesgar mas que esto, la senal se marca NO OPERABLE
    "lote_min": 0.01,
    "paso_lote": 0.01,
    "contrato_forex": 100_000,
    "contrato_oro": 100,
    "apalancamiento_max": 20,   # BTC: si la posicion necesita mas apalancamiento que esto, NO OPERABLE
    "solo_operables": False,    # True = no envia las senales que tu cuenta no puede operar
    "filtro_noticias": True,    # no avisar 30 min antes ni 15 min despues de una noticia de ALTO impacto
    "tfs": ["5m", "15m"],       # temporalidades de entrada que escanea el bot (alertas de 5m y de 15m)
}

LOTE = {"forex": CUENTA["contrato_forex"], "oro": CUENTA["contrato_oro"]}
NQ_USD_PUNTO, MNQ_USD_PUNTO = 20.0, 2.0

PARAMS_DEFAULT = {
    "min_score": 3,                 # de 4 confluencias reales
    "banco": CUENTA["banco"],
    "riesgo": CUENTA["riesgo_pct"], # % del banco por operacion
    "kz": dict(KZ_DEFAULT),         # tipo -> lista de killzones ([] = sin filtro)
    "fib": (0.5, 0.79),             # zona OTE (retroceso del impulso)
    "ventana_bos": 40,              # el BOS debe estar dentro de las ultimas N velas
    "estrategias": ["SMC", "Barrido", "Tortugas", "Londres", "Setup91", "ORB", "HolyGrail", "Gotobi", "VWAP", "Ruido", "HoraBTC", "NocheOro", "ORBRetest", "AsiaSweep", "EMAVWAP", "ORB15", "MaxHist", "Medias", "Gap", "DI_EMA", "DI_Ruptura", "DI_RSI", "Judas"],
    "s91_filtro_1h": True,          # Setup 9.1 solo a favor del sesgo 1H (False = reglas puras)
    "solo_ganadoras": True,         # en vivo: solo las combinaciones de GANADORAS (python bot_pro.py --todas = todas)
}


# ----------------------------------------------------------------- SECRETS
def secreto(name):
    """Prioridad: variable de entorno (o st.secrets, que app.py copia al entorno) -> valor por defecto."""
    return str(os.environ.get(name) or DEFAULT_SECRETS.get(name, "")).strip()


def enviar_telegram(texto):
    token = secreto("TELEGRAM_TOKEN")
    ids = [i.strip() for i in secreto("TELEGRAM_CHAT_IDS").replace(";", ",").split(",") if i.strip()]
    if not token or not ids:
        return False, "Faltan TELEGRAM_TOKEN / TELEGRAM_CHAT_IDS"
    errores = []
    for chat_id in ids:
        try:
            r = requests.post(f"https://api.telegram.org/bot{token}/sendMessage",
                              json={"chat_id": chat_id, "text": texto}, timeout=15)
            if not r.ok:
                errores.append(f"{chat_id}: {r.status_code} {r.text[:120]}")
        except Exception as e:
            errores.append(f"{chat_id}: {e}")
    return (not errores), ("; ".join(errores) or "Enviado")


# ----------------------------------------------------------------- HORARIOS
def _ny(ahora=None):
    if ahora is None:
        return datetime.now(NY)
    if isinstance(ahora, pd.Timestamp):
        ahora = ahora.to_pydatetime()
    if ahora.tzinfo is None:
        ahora = ahora.replace(tzinfo=UTC)
    return ahora.astimezone(NY)


def mercado_abierto(tipo, ahora=None):
    """
    Cripto: 24/7.
    Forex:  domingo 17:00 -> viernes 17:00 (hora NY).
    Oro (GC) y Nasdaq (NQ) en CME Globex: domingo 18:00 -> viernes 17:00 NY,
    con pausa diaria 17:00-18:00 NY.
    """
    if tipo == "cripto":
        return True
    n = _ny(ahora)
    h, wd = n.hour + n.minute / 60, n.weekday()   # lunes=0 ... domingo=6
    if wd == 5:
        return False
    if tipo == "forex":
        if wd == 4 and h >= 17:
            return False
        if wd == 6 and h < 17:
            return False
        return True
    # oro e indices (CME Globex)
    if wd == 6:
        return h >= 18
    if wd == 4 and h >= 17:
        return False
    return not (17 <= h < 18)


def killzone_actual(ahora=None):
    n = _ny(ahora)
    h = n.hour + n.minute / 60
    for nombre, (a, b) in KILLZONES.items():
        if a <= h < b:
            return nombre
    return None


def en_killzone(tipo, p, ahora=None):
    """True si no hay filtro para ese mercado o si estamos dentro de alguna killzone elegida."""
    zonas = (p.get("kz") or {}).get(tipo, [])
    if not zonas:
        return True
    return killzone_actual(ahora) in zonas


# ----------------------------------------------------------------- DATOS
_EX = {}


def _exchange(nombre):
    if nombre not in _EX:
        _EX[nombre] = getattr(ccxt, nombre)({"enableRateLimit": True, "timeout": 15000})
    return _EX[nombre]


def _ts_utc(x):
    t = pd.Timestamp(x)
    return t.tz_localize("UTC") if t.tzinfo is None else t.tz_convert("UTC")


def _df_ccxt(raw):
    df = pd.DataFrame(raw, columns=["t", "open", "high", "low", "close", "volume"])
    df["t"] = pd.to_datetime(df["t"], unit="ms", utc=True)
    df = df.drop_duplicates("t").set_index("t").sort_index()
    return df.astype(float)


def ohlcv_ccxt(simbolo, tf, limite=500):
    if ccxt is None:
        return None
    for nombre in EXCHANGES:
        try:
            raw = _exchange(nombre).fetch_ohlcv(simbolo, timeframe=tf, limit=limite)
            if raw and len(raw) > 50:
                return _df_ccxt(raw)
        except Exception:
            continue
    return None


def historico_ccxt(simbolo, tf, desde, hasta=None, max_lotes=400):
    """Descarga paginada (since = ultima vela + 1 ms), como en los ejemplos oficiales de ccxt."""
    if ccxt is None:
        return None
    desde_ms = int(_ts_utc(desde).timestamp() * 1000)
    hasta_ms = int(_ts_utc(hasta or datetime.now(UTC)).timestamp() * 1000)
    for nombre in EXCHANGES:
        try:
            ex = _exchange(nombre)
            since, filas = desde_ms, []
            for _ in range(max_lotes):
                lote = ex.fetch_ohlcv(simbolo, timeframe=tf, since=since, limit=1000)
                if not lote:
                    break
                filas += lote
                nuevo = lote[-1][0] + 1
                if nuevo <= since or lote[-1][0] >= hasta_ms:
                    break
                since = nuevo
            if len(filas) > 200:
                df = _df_ccxt(filas)
                return df[df.index < pd.Timestamp(hasta_ms, unit="ms", tz="UTC")]
        except Exception:
            continue
    return None


def _limpiar_yf(d):
    if d is None or d.empty:
        return None
    if isinstance(d.columns, pd.MultiIndex):
        d.columns = d.columns.get_level_values(0)
    d = d.rename(columns=str.lower)[["open", "high", "low", "close", "volume"]].dropna()
    d.index = pd.to_datetime(d.index, utc=True)
    return d.astype(float)


def ohlcv_yf(ticker, tf):
    if yf is None:
        return None
    interval, periodo = YF_CFG[tf]
    try:
        return _limpiar_yf(yf.download(ticker, interval=interval, period=periodo, progress=False,
                                       auto_adjust=False, threads=False))
    except Exception:
        return None


def historico_yf(ticker, tf, desde=None, hasta=None):
    """Respeta los limites de Yahoo: <1h = 60 dias, 1h = 730 dias."""
    if yf is None:
        return None
    lim = datetime.now(UTC) - timedelta(days=YF_LIMITE_DIAS[tf])
    ini = max(_ts_utc(desde) if desde else _ts_utc(lim), _ts_utc(lim))
    fin = _ts_utc(hasta) if hasta else None
    try:
        return _limpiar_yf(yf.download(ticker, interval=tf, start=ini.to_pydatetime(),
                                       end=fin.to_pydatetime() if fin is not None else None,
                                       progress=False, auto_adjust=False, threads=False))
    except Exception:
        return None


def obtener(nombre, tf):
    meta = ACTIVOS[nombre]
    df = ohlcv_ccxt(nombre, tf) if meta["tipo"] == "cripto" else None
    if df is None:
        df = ohlcv_yf(meta["yf"], tf)
    return df


def historico(nombre, tf, desde, hasta=None):
    meta = ACTIVOS[nombre]
    df = historico_ccxt(nombre, tf, desde, hasta) if meta["tipo"] == "cripto" else None
    if df is None:
        df = historico_yf(meta["yf"], tf, desde, hasta)
    return df


def solo_cerradas(df, tf, ahora=None):
    """Elimina la vela en formacion: se queda con las velas cuyo cierre (apertura + tf) ya paso."""
    if df is None or df.empty:
        return df
    ahora = _ts_utc(ahora if ahora is not None else datetime.now(UTC))
    cierre = df.index + pd.Timedelta(minutes=TF_MIN[tf])
    return df[cierre <= ahora]


def remuestrear(df, tf):
    """Construye velas de mayor temporalidad (p. ej. 15m -> 1h) con apertura a la izquierda."""
    regla = {"1d": "1D", "1h": "1h", "4h": "4h", "30m": "30min", "15m": "15min"}[tf]
    r = df.resample(regla, label="left", closed="left").agg(
        {"open": "first", "high": "max", "low": "min", "close": "last", "volume": "sum"})
    return r.dropna(subset=["open", "close"])


# ----------------------------------------------------------------- INDICADORES
def atr(df, n=14):
    pc = df["close"].shift()
    tr = pd.concat([df["high"] - df["low"], (df["high"] - pc).abs(),
                    (df["low"] - pc).abs()], axis=1).max(axis=1)
    return tr.ewm(alpha=1 / n, adjust=False).mean()


def adx(df, n=14):
    up, dn = df["high"].diff(), -df["low"].diff()
    plus = pd.Series(np.where((up > dn) & (up > 0), up, 0.0), index=df.index)
    minus = pd.Series(np.where((dn > up) & (dn > 0), dn, 0.0), index=df.index)
    a = atr(df, n)
    pdi = 100 * plus.ewm(alpha=1 / n, adjust=False).mean() / a
    mdi = 100 * minus.ewm(alpha=1 / n, adjust=False).mean() / a
    dx = 100 * (pdi - mdi).abs() / (pdi + mdi).replace(0, np.nan)
    return dx.ewm(alpha=1 / n, adjust=False).mean()


def williams_r(df, n=14):
    hh, ll = df["high"].rolling(n).max(), df["low"].rolling(n).min()
    return -100 * (hh - df["close"]) / (hh - ll).replace(0, np.nan)


def swings(df, k=3):
    """Swings confirmados: necesitan k velas cerradas a cada lado."""
    h, l = df["high"].to_numpy(), df["low"].to_numpy()
    sh, sl = [], []
    for i in range(k, len(df) - k):
        if h[i] == h[i - k:i + k + 1].max():
            sh.append(i)
        if l[i] == l[i - k:i + k + 1].min():
            sl.append(i)
    return sh, sl


def perfil_volumen(df, bins=48, area=0.70):
    """POC / VAH / VAL. Si no hay volumen (forex en Yahoo) pondera por tiempo (TPO)."""
    lo, hi = df["low"].min(), df["high"].max()
    if not hi > lo:
        return None
    edges = np.linspace(lo, hi, bins + 1)
    centros = (edges[:-1] + edges[1:]) / 2
    vol = np.zeros(bins)
    pesos = df["volume"].to_numpy() if df["volume"].sum() > 0 else np.ones(len(df))
    i0s = np.clip(np.searchsorted(edges, df["low"].to_numpy(), side="right") - 1, 0, bins - 1)
    i1s = np.clip(np.searchsorted(edges, df["high"].to_numpy(), side="right") - 1, 0, bins - 1)
    for i0, i1, v in zip(i0s, i1s, pesos):
        vol[i0:i1 + 1] += v / (i1 - i0 + 1)
    p = int(vol.argmax())
    objetivo, acc, a, b = vol.sum() * area, vol[p], p, p
    while acc < objetivo and (a > 0 or b < bins - 1):
        arriba = vol[b + 1] if b < bins - 1 else -1
        abajo = vol[a - 1] if a > 0 else -1
        if arriba >= abajo:
            b += 1
            acc += vol[b]
        else:
            a -= 1
            acc += vol[a]
    return {"poc": float(centros[p]), "vah": float(edges[b + 1]), "val": float(edges[a])}


def sesgo_ctx(d1):
    """+1 alcista, -1 bajista, 0 neutral (EMA21/50 + posicion vs POC) sobre velas 1H cerradas."""
    c = d1["close"]
    e21, e50 = c.ewm(span=21, adjust=False).mean(), c.ewm(span=50, adjust=False).mean()
    vp = perfil_volumen(d1.tail(120))
    s, x = 0, c.iloc[-1]
    if x > e50.iloc[-1] and e21.iloc[-1] > e50.iloc[-1]:
        s += 1
    if x < e50.iloc[-1] and e21.iloc[-1] < e50.iloc[-1]:
        s -= 1
    if vp:
        s += 1 if x > vp["poc"] else -1
    return (1 if s >= 1 else -1 if s <= -1 else 0), vp


def _flip(df):
    """Espejo de precios para reutilizar la logica LONG en SHORT."""
    f = pd.DataFrame(index=df.index)
    f["open"], f["close"] = -df["open"], -df["close"]
    f["high"], f["low"] = -df["low"], -df["high"]
    f["volume"] = df["volume"]
    return f


def setup_long(df, ventana=40, fib=(0.5, 0.79), k=3):
    """
    BOS alcista reciente + Order Block (ultima vela bajista antes del impulso).
    Devuelve las piezas por separado para que el score cuente solo confluencias reales:
      toco   -> alguna de las 3 ultimas velas cerradas (posteriores al BOS) tocó el OB sin cerrar debajo
      ote    -> retroceso Fibonacci del impulso dentro de la zona OTE (por defecto 50%-79%)
      fvg    -> hay un Fair Value Gap alcista en el tramo del impulso (entre el minimo y la vela del BOS)
      fresco -> el OB no habia sido mitigado entre el BOS y el toque actual
      roto   -> el precio perdio el origen del impulso (retroceso > 100%): setup invalido
    """
    n = len(df)
    o, h, l, c = (df[x].to_numpy() for x in ("open", "high", "low", "close"))
    sh, _ = swings(df, k)
    for j in reversed([x for x in sh if x < n - 4]):
        if j < n - 90:
            break
        idx = np.where(c[j + 1:] > h[j])[0]
        if len(idx) == 0:
            continue
        i = j + 1 + int(idx[0])                       # vela que cierra por encima del swing (BOS)
        if i < n - ventana or i > n - 2:
            continue
        m = j + int(np.argmin(l[j:i + 1]))            # minimo previo al impulso
        ob = m
        for q in range(m, max(m - 6, -1), -1):
            if c[q] < o[q]:
                ob = q
                break
        ob_lo, ob_hi = float(l[ob]), float(h[ob])
        top, base = float(h[i:].max()), float(l[m])
        if top <= base:
            continue
        px = float(c[-1])
        ratio = (top - px) / (top - base)             # 0.5 = 50% de retroceso
        ini_toque = max(i + 1, n - 3)
        toco = bool((l[ini_toque:] <= ob_hi).any() and px >= ob_lo)
        fresco = not bool((l[i + 1:ini_toque] <= ob_hi).any())
        fvg = any(l[q] > h[q - 2] and l[q] >= ob_lo for q in range(max(m + 2, 2), min(i, n - 1) + 1))
        return {"ob_lo": ob_lo, "ob_hi": ob_hi, "ratio": float(ratio), "toco": toco,
                "ote": bool(fib[0] <= ratio <= fib[1]), "fvg": bool(fvg), "fresco": fresco,
                "roto": bool(ratio > 1.0), "ob_t": df.index[ob], "bos_t": df.index[i],
                "base": base, "top": top}
    return None


# ----------------------------------------------------------------- ANALISIS
def fmt(x):
    return f"{x:,.2f}" if abs(x) >= 100 else (f"{x:.3f}" if abs(x) >= 10 else
                                             (f"{x:.5f}" if abs(x) >= 0.1 else f"{x:.6f}"))


def tamano(nombre, tipo, riesgo_usd, dist, precio):
    """Unidades para cripto; lotes estandar para forex/oro (con valor por punto en USD)."""
    if dist <= 0:
        return 0.0, ""
    if tipo == "cripto":
        u = riesgo_usd / dist
        return u, f"{u:.6f} unid."
    if tipo == "indice":
        cts = riesgo_usd / (dist * PUNTO_MICRO.get(nombre, MNQ_USD_PUNTO))
        return cts, f"{cts:.2f} micro contratos"
    contrato = LOTE[tipo]
    usd_por_unidad = 1.0
    if tipo == "forex" and nombre.startswith("USD/"):   # p. ej. USD/JPY: P&L en JPY -> USD
        usd_por_unidad = 1.0 / precio
    lotes = riesgo_usd / (dist * contrato * usd_por_unidad)
    return lotes, f"{lotes:.4f} lotes"


def usd_por_punto_lote(nombre, tipo, precio):
    """Cuantos USD gana/pierde 1 lote por cada 1.0 de movimiento del precio."""
    if tipo == "indice":
        return PUNTO_MICRO.get(nombre, MNQ_USD_PUNTO)
    usd = 1.0 / precio if (tipo == "forex" and nombre.startswith("USD/")) else 1.0
    return LOTE[tipo] * usd


def viabilidad(s, p):
    """Ajusta el tamano a TU cuenta (lote minimo, paso de lote, apalancamiento) y marca si es operable."""
    banco, dist, px = p["banco"], abs(s["entrada"] - s["sl"]), s["entrada"]
    s["viable"], s["aviso"] = True, ""
    if dist <= 0:
        s["viable"], s["aviso"] = False, "SL invalido"
        return s
    if s["tipo"] == "cripto":
        unid = s["riesgo_usd"] / dist
        nocional = unid * px
        apal = nocional / banco
        s["tamano_txt"] = f"{unid:.6f} unid. · nocional ${nocional:,.2f} (~{apal:.1f}x apalancamiento)"
        if apal > CUENTA["apalancamiento_max"]:
            s["viable"] = False
            s["aviso"] = (f"⚠️ NO OPERABLE: necesitas ~{apal:.0f}x de apalancamiento "
                          f"(tu máximo es {CUENTA['apalancamiento_max']}x).")
        return s
    lote_min, paso = CUENTA["lote_min"], CUENTA["paso_lote"]
    if s["tipo"] == "indice":
        lote_min, paso = 1, 1                       # 1 contrato MNQ
    valor = usd_por_punto_lote(s["activo"], s["tipo"], px)
    lotes = s["riesgo_usd"] / (dist * valor)
    lotes_ok = np.floor(lotes / paso + 1e-9) * paso
    unidad = "micro contratos" if s["tipo"] == "indice" else "lotes"
    if lotes_ok >= lote_min:
        r_real = lotes_ok * dist * valor
        s["tamano_txt"] = f"{lotes_ok:.2f} {unidad} (riesgo real ${r_real:.2f})"
        return s
    r_min = lote_min * dist * valor
    pct = 100 * r_min / banco
    s["tamano_txt"] = f"{lote_min:g} {unidad} (mínimo de tu cuenta)"
    if pct > CUENTA["riesgo_max_pct"]:
        s["viable"] = False
        s["aviso"] = (f"⚠️ NO OPERABLE con ${banco:g}: el lote mínimo ({lote_min:g}) arriesga ${r_min:.2f} "
                      f"({pct:.0f}% de tu cuenta). Opérala solo si tu cuenta permite lotes más pequeños (cent).")
    else:
        s["aviso"] = f"ℹ️ Con el lote mínimo arriesgas ${r_min:.2f} ({pct:.1f}% de tu cuenta)."
    return s


def evaluar_smc(nombre, tipo, df, d1, p, ahora, dd=None):
    """
    Estrategia 1 (SMC / Order Blocks). Nucleo comun (vivo y backtest). df y d1 deben contener SOLO velas cerradas a la hora 'ahora'.
    Devuelve (diagnostico, senal|None).
    """
    diag = {"Estrategia": "SMC", "Activo": nombre, "Estado": "", "Sesgo 1H": "-", "Dir": "-", "Score": 0,
            "Confluencias": ""}
    if not mercado_abierto(tipo, ahora):
        diag["Estado"] = "Mercado cerrado"
        return diag, None
    if df is None or d1 is None or len(df) < 120 or len(d1) < 60:
        diag["Estado"] = "Sin datos"
        return diag, None
    df, d1 = df.tail(VENTANA), d1.tail(VENTANA_CTX)

    bias, vp = sesgo_ctx(d1)
    diag["Sesgo 1H"] = {1: "Alcista", -1: "Bajista", 0: "Neutral"}[bias]
    diag["_vp"] = vp
    if bias == 0:
        diag["Estado"] = "Sesgo neutral: sin operar"
        return diag, None

    adx_v = float(adx(df).iloc[-1])
    if not adx_v >= ADX_MIN[tipo]:
        diag["Estado"] = f"ADX bajo ({adx_v:.0f} < {ADX_MIN[tipo]})"
        return diag, None

    fib = tuple(p.get("fib", (0.5, 0.79)))
    s = setup_long(df, p.get("ventana_bos", 40), fib) if bias == 1 else \
        setup_long(_flip(df), p.get("ventana_bos", 40), fib)
    if s is None:
        diag["Estado"] = "Sin BOS reciente"
        return diag, None
    if bias == -1:   # deshacer el espejo
        s = {**s, "ob_lo": -s["ob_hi"], "ob_hi": -s["ob_lo"], "base": -s["base"], "top": -s["top"]}
    diag["_ob"] = (s["ob_lo"], s["ob_hi"])
    diag["_ote"] = tuple(sorted((s["top"] - fib[0] * (s["top"] - s["base"]),
                                 s["top"] - fib[1] * (s["top"] - s["base"]))))
    diag["Dir"] = "LONG" if bias == 1 else "SHORT"
    if s["roto"]:
        diag["Estado"] = "Estructura rota (retroceso > 100%)"
        return diag, None

    px = float(df["close"].iloc[-1])
    a_v = float(atr(df).iloc[-1])
    wr = float(williams_r(df).iloc[-1])
    poc_ok = vp is not None and ((px <= vp["poc"]) if bias == 1 else (px >= vp["poc"]))
    conf = {"OTE": s["ote"], "FVG": s["fvg"], "Valor<POC" if bias == 1 else "Valor>POC": poc_ok,
            "OB fresco": s["fresco"]}
    score = int(sum(conf.values()))
    diag["Score"] = score
    diag["Confluencias"] = " · ".join(k for k, v in conf.items() if v) or "-"
    diag["Fib %"] = round(100 * s["ratio"], 1)

    if not s["toco"]:
        diag["Estado"] = f"Esperando retroceso al OB ({score}/4)"
        return diag, None
    if (bias == 1 and wr > -20) or (bias == -1 and wr < -80):
        diag["Estado"] = f"Filtro exhaustacion (Williams {wr:.0f})"
        return diag, None
    if score < p["min_score"]:
        diag["Estado"] = f"Score insuficiente ({score}/4, minimo {p['min_score']})"
        return diag, None
    if not en_killzone(tipo, p, ahora):
        diag["Estado"] = f"Setup valido {score}/4 fuera de killzone"
        return diag, None

    sl = s["ob_lo"] - 0.3 * a_v if bias == 1 else s["ob_hi"] + 0.3 * a_v
    riesgo_px = abs(px - sl)
    if (bias == 1 and sl >= px) or (bias == -1 and sl <= px) or riesgo_px > 3 * a_v:
        diag["Estado"] = "SL invalido o demasiado lejos"
        return diag, None
    riesgo_usd = p["banco"] * p["riesgo"] / 100
    tam, tam_txt = tamano(nombre, tipo, riesgo_usd, riesgo_px, px)
    diag["Estado"] = "🚨 SEÑAL"
    vela = df.index[-1]
    return diag, {"estrategia": "SMC Order Block", "score_max": 4, "activo": nombre, "tipo": tipo, "dir": diag["Dir"], "bias": bias,
                  "entrada": px, "sl": sl, "tp1": px + bias * 2 * riesgo_px,
                  "tp2": px + bias * 3 * riesgo_px, "riesgo_usd": riesgo_usd,
                  "tamano": tam, "tamano_txt": tam_txt, "score": score,
                  "confluencias": diag["Confluencias"], "kz": killzone_actual(ahora) or "-",
                  "vela": vela, "id": f"SMC|{nombre}|{diag['Dir']}|{s['ob_t']}"}


# ================================================================= ESTRATEGIA 2: BARRIDO DE LIQUIDEZ
def sesgo_estructura(d1, k=3):
    """Sesgo SIN indicadores: maximos y minimos crecientes (+1) o decrecientes (-1) en 1H."""
    sh, sl = swings(d1, k)
    if len(sh) < 2 or len(sl) < 2:
        return 0
    h, l = d1["high"].to_numpy(), d1["low"].to_numpy()
    if h[sh[-1]] > h[sh[-2]] and l[sl[-1]] > l[sl[-2]]:
        return 1
    if h[sh[-1]] < h[sh[-2]] and l[sl[-1]] < l[sl[-2]]:
        return -1
    return 0


def niveles_liquidez(d1, ahora):
    """Maximo/minimo del dia anterior (PDH/PDL) y rango de Asia (19:00-00:00 NY) con velas 1H cerradas."""
    ny = d1.index.tz_convert(NY)
    hoy = _ny(ahora).date()
    dias = sorted({x.date() for x in ny if x.date() < hoy})
    if not dias:
        return None
    ayer = dias[-1]
    m_ayer = np.array([x.date() == ayer for x in ny])
    ini_asia = pd.Timestamp(datetime.combine(hoy, datetime.min.time()), tz=NY) - pd.Timedelta(hours=5)
    fin_asia = pd.Timestamp(datetime.combine(hoy, datetime.min.time()), tz=NY)
    m_asia = (ny >= ini_asia) & (ny < fin_asia)
    niv = {"PDH": float(d1["high"][m_ayer].max()), "PDL": float(d1["low"][m_ayer].min())}
    if m_asia.any():
        niv["Asia H"] = float(d1["high"][m_asia].max())
        niv["Asia L"] = float(d1["low"][m_asia].min())
    return niv


def _barrido_long(df, lows, buscar=12, ref=5):
    """
    LONG: una vela perfora un nivel de liquidez inferior con la mecha y CIERRA por encima (barrido),
    y luego una vela cierra por encima del maximo de las 'ref' velas previas al barrido (cambio de estructura).
    Solo dispara si ese cambio de estructura ocurre en la ULTIMA vela cerrada (una alerta por setup).
    """
    n = len(df)
    o, h, l, c = (df[x].to_numpy() for x in ("open", "high", "low", "close"))
    if c[-1] <= o[-1]:
        return None
    for j in range(n - 2, max(n - 2 - buscar, ref), -1):
        barridos = [nm for nm, lv in lows.items() if l[j] < lv < c[j]]
        if not barridos:
            continue
        nivel_mss = h[j - ref:j].max()
        if c[j] > nivel_mss:                                      # la propia vela del barrido ya rompio
            return None
        previos = c[j + 1:n - 1]
        if (previos > nivel_mss).any() or c[-1] <= nivel_mss:   # ya habia confirmado antes, o no confirma
            return None
        minimo = float(l[j:].min())
        fvg = any(l[q] > h[q - 2] for q in range(j + 2, n))
        return {"barridos": barridos, "minimo": minimo, "mss": float(nivel_mss), "fvg": bool(fvg),
                "vela_barrido": df.index[j]}
    return None


def evaluar_barrido(nombre, tipo, df, d1, p, ahora, dd=None):
    """
    Estrategia 'Liquidez + Estructura' (sin indicadores): contexto estructural 1H -> barrido de
    liquidez (PDH/PDL o rango de Asia) -> cambio de estructura en la temporalidad de entrada.
    Basada en los conceptos PUBLICOS de SMC (estructura, bloques institucionales, liquidez).
    No es la estrategia privada de ningun trader.
    """
    diag = {"Estrategia": "Barrido", "Activo": nombre, "Estado": "", "Sesgo 1H": "-", "Dir": "-",
            "Score": 0, "Confluencias": ""}
    if not mercado_abierto(tipo, ahora):
        diag["Estado"] = "Mercado cerrado"
        return diag, None
    if df is None or d1 is None or len(df) < 60 or len(d1) < 48:
        diag["Estado"] = "Sin datos"
        return diag, None
    df, d1 = df.tail(VENTANA), d1.tail(VENTANA_CTX)
    bias = sesgo_estructura(d1)
    diag["Sesgo 1H"] = {1: "Alcista", -1: "Bajista", 0: "Rango"}[bias]
    if bias == 0:
        diag["Estado"] = "Estructura 1H en rango: sin operar"
        return diag, None
    niv = niveles_liquidez(d1, ahora)
    if not niv:
        diag["Estado"] = "Sin niveles de liquidez"
        return diag, None
    diag["_niveles"] = niv
    if bias == 1:
        s = _barrido_long(df, {k: v for k, v in niv.items() if k in ("PDL", "Asia L")})
    else:
        s = _barrido_long(_flip(df), {k: -v for k, v in niv.items() if k in ("PDH", "Asia H")})
        if s:
            s = {**s, "minimo": -s["minimo"], "mss": -s["mss"]}
    diag["Dir"] = "LONG" if bias == 1 else "SHORT"
    if s is None:
        diag["Estado"] = "Esperando barrido + cambio de estructura"
        return diag, None
    conf = {"Barrio dia anterior": any(b in ("PDL", "PDH") for b in s["barridos"]), "FVG": s["fvg"]}
    score = int(sum(conf.values()))
    diag["Score"] = score
    diag["Confluencias"] = " · ".join(k for k, v in conf.items() if v) or "-"
    if not en_killzone(tipo, p, ahora):
        diag["Estado"] = f"Barrido valido fuera de killzone ({', '.join(s['barridos'])})"
        return diag, None
    px = float(df["close"].iloc[-1])
    sl = s["minimo"] - bias * 0.1 * abs(px - s["minimo"])
    riesgo_px = abs(px - sl)
    if riesgo_px <= 0 or (bias == 1 and sl >= px) or (bias == -1 and sl <= px):
        diag["Estado"] = "SL invalido"
        return diag, None
    riesgo_usd = p["banco"] * p["riesgo"] / 100
    tam, tam_txt = tamano(nombre, tipo, riesgo_usd, riesgo_px, px)
    diag["Estado"] = "🚨 SEÑAL"
    return diag, {"estrategia": "Barrido de liquidez", "activo": nombre, "tipo": tipo, "dir": diag["Dir"],
                  "bias": bias, "entrada": px, "sl": sl, "tp1": px + bias * 2 * riesgo_px,
                  "tp2": px + bias * 3 * riesgo_px, "riesgo_usd": riesgo_usd, "tamano": tam,
                  "tamano_txt": tam_txt, "score": score, "score_max": 2,
                  "confluencias": f"barrio {', '.join(s['barridos'])}" + (" · FVG" if s["fvg"] else ""),
                  "kz": killzone_actual(ahora) or "-", "vela": df.index[-1],
                  "id": f"BARRIDO|{nombre}|{diag['Dir']}|{s['vela_barrido']}"}


# ================================================================= ESTRATEGIA 3: TORTUGAS (Richard Dennis)
def atr_n(dd, n=20):
    """N de las Tortugas: media exponencial de 20 dias del rango verdadero."""
    return atr(dd, n)


def evaluar_tortugas(nombre, tipo, df, d1, p, ahora, dd=None):
    """
    Sistema 1 de las Tortugas (reglas publicas de Richard Dennis / William Eckhardt):
      Entrada : el precio rompe el maximo (LONG) o minimo (SHORT) de los 20 dias anteriores.
      Stop    : 2N desde la entrada (N = ATR de 20 dias).
      Salida  : cuando el precio toca el minimo de 10 dias (LONG) o el maximo de 10 dias (SHORT).
    Aqui la ruptura se confirma con el CIERRE de la vela de entrada (sin repintado).
    No aplica killzone: es un sistema diario de seguimiento de tendencia.
    """
    diag = {"Estrategia": "Tortugas", "Activo": nombre, "Estado": "", "Sesgo 1H": "-", "Dir": "-",
            "Score": 0, "Confluencias": ""}
    if not mercado_abierto(tipo, ahora):
        diag["Estado"] = "Mercado cerrado"
        return diag, None
    if dd is None or df is None or len(dd) < 25 or len(df) < 3:
        diag["Estado"] = "Sin datos diarios"
        return diag, None
    dd = dd.tail(60)
    h20, l20 = float(dd["high"].tail(20).max()), float(dd["low"].tail(20).min())
    h10, l10 = float(dd["high"].tail(10).max()), float(dd["low"].tail(10).min())
    n_val = float(atr_n(dd).iloc[-1])
    c_ant, c_act = float(df["close"].iloc[-2]), float(df["close"].iloc[-1])
    diag["Confluencias"] = f"canal 20d {fmt(l20)} - {fmt(h20)}"
    if c_act > h20 >= c_ant:
        bias = 1
    elif c_act < l20 <= c_ant:
        bias = -1
    else:
        diag["Estado"] = "Dentro del canal de 20 días"
        return diag, None
    diag["Dir"] = "LONG" if bias == 1 else "SHORT"
    sl = c_act - bias * 2 * n_val
    riesgo_usd = p["banco"] * p["riesgo"] / 100
    tam, tam_txt = tamano(nombre, tipo, riesgo_usd, abs(c_act - sl), c_act)
    salida = l10 if bias == 1 else h10
    diag["Estado"] = "🚨 SEÑAL"
    return diag, {"estrategia": "Tortugas (ruptura 20 días)", "activo": nombre, "tipo": tipo,
                  "dir": diag["Dir"], "bias": bias, "entrada": c_act, "sl": sl, "tp1": None, "tp2": None,
                  "salida": "canal",
                  "salida_txt": f"Salida: cuando toque el {'mínimo' if bias == 1 else 'máximo'} de 10 días "
                                f"(hoy {fmt(salida)}). Sin TP fijo: deja correr la tendencia.",
                  "riesgo_usd": riesgo_usd, "tamano": tam, "tamano_txt": tam_txt, "score": 1,
                  "score_max": 1, "confluencias": f"rompió {'máximo' if bias == 1 else 'mínimo'} de 20 días · "
                                                  f"N={fmt(n_val)}",
                  "kz": killzone_actual(ahora) or "-", "vela": df.index[-1],
                  "id": f"TORTUGAS|{nombre}|{diag['Dir']}|{dd.index[-1]:%Y-%m-%d}"}


# ================================================================= ESTRATEGIA 4: RUPTURA DE LONDRES
def _pip(nombre):
    return 0.01 if "JPY" in nombre else 0.0001


def evaluar_londres(nombre, tipo, df, d1, p, ahora, dd=None):
    """
    Ruptura de Londres (London Breakout, version publicada por DailyForex para GBP/USD):
      Rango  : maximo y minimo de 00:00 a 07:00 GMT.
      Entrada: primera vela que CIERRA fuera del rango entre 07:00 y 11:00 GMT.
      Filtro : se descarta si la vela de ruptura es un pin bar en contra (mecha contraria >= 2x cuerpo).
      Stop   : 1,5 pips mas alla del ultimo swing (minimo para LONG, maximo para SHORT).
      Objetivo: 1,5R.
    """
    diag = {"Estrategia": "Londres", "Activo": nombre, "Estado": "", "Sesgo 1H": "-", "Dir": "-",
            "Score": 0, "Confluencias": ""}
    if tipo != "forex":
        diag["Estado"] = "Solo forex"
        return diag, None
    if not mercado_abierto(tipo, ahora) or df is None or len(df) < 40:
        diag["Estado"] = "Mercado cerrado" if df is not None else "Sin datos"
        return diag, None
    t = _ts_utc(ahora)
    hoy = t.normalize()
    if not (hoy + pd.Timedelta(hours=7) < t <= hoy + pd.Timedelta(hours=11)):
        diag["Estado"] = "Fuera de la ventana 07:00-11:00 GMT"
        return diag, None
    rango = df[(df.index >= hoy) & (df.index < hoy + pd.Timedelta(hours=7))]
    if len(rango) < 4:
        diag["Estado"] = "Sin rango 00-07 GMT"
        return diag, None
    r_hi, r_lo = float(rango["high"].max()), float(rango["low"].min())
    diag["Confluencias"] = f"rango {fmt(r_lo)} - {fmt(r_hi)}"
    diag["_niveles"] = {"Rango H": r_hi, "Rango L": r_lo}
    sesion = df[df.index >= hoy + pd.Timedelta(hours=7)]
    cierres = sesion["close"].to_numpy()
    fuera = (cierres > r_hi) | (cierres < r_lo)
    if not fuera.any() or fuera.argmax() != len(cierres) - 1:     # solo la PRIMERA ruptura del dia
        diag["Estado"] = "Esperando ruptura" if not fuera.any() else "Ruptura de hoy ya avisada"
        return diag, None
    v = df.iloc[-1]
    bias = 1 if v["close"] > r_hi else -1
    cuerpo = abs(v["close"] - v["open"])
    mecha_contra = (v["high"] - max(v["open"], v["close"])) if bias == 1 else (min(v["open"], v["close"]) - v["low"])
    diag["Dir"] = "LONG" if bias == 1 else "SHORT"
    if mecha_contra >= 2 * max(cuerpo, 1e-12):
        diag["Estado"] = "Ruptura con pin bar en contra: descartada"
        return diag, None
    _, sls = swings(df.tail(60), 2)
    shs, _ = swings(_flip(df.tail(60)), 2)
    px, pip = float(v["close"]), _pip(nombre)
    if bias == 1:
        ref = float(df.tail(60)["low"].iloc[sls[-1]]) if sls else float(df["low"].tail(5).min())
        sl = ref - 1.5 * pip
    else:
        ref = float(df.tail(60)["high"].iloc[shs[-1]]) if shs else float(df["high"].tail(5).max())
        sl = ref + 1.5 * pip
    riesgo_px = abs(px - sl)
    if (bias == 1 and sl >= px) or (bias == -1 and sl <= px):
        diag["Estado"] = "SL invalido"
        return diag, None
    riesgo_usd = p["banco"] * p["riesgo"] / 100
    tam, tam_txt = tamano(nombre, tipo, riesgo_usd, riesgo_px, px)
    diag["Estado"] = "🚨 SEÑAL"
    return diag, {"estrategia": "Ruptura de Londres", "activo": nombre, "tipo": tipo, "dir": diag["Dir"],
                  "bias": bias, "entrada": px, "sl": sl, "tp1": px + bias * 1.5 * riesgo_px, "tp2": None,
                  "r1": 1.5, "tp_r": 1.5, "riesgo_usd": riesgo_usd, "tamano": tam, "tamano_txt": tam_txt,
                  "score": 1, "score_max": 1, "confluencias": f"rompió rango 00-07 GMT ({fmt(r_lo)}-{fmt(r_hi)})",
                  "kz": killzone_actual(ahora) or "-", "vela": df.index[-1],
                  "id": f"LONDRES|{nombre}|{hoy:%Y-%m-%d}"}


# ================================================================= ESTRATEGIA 5: SETUP 9.1 (Larry Williams / Brasil)
def evaluar_setup91(nombre, tipo, df, d1, p, ahora, dd=None):
    """
    Setup 9.1 de Larry Williams, muy difundido en Brasil (Palex - Alexandre Fernandes; citado por
    Alexandre Wolwacz, 2010). Reglas publicas:
      Media    : exponencial de 9 periodos (MME9) sobre el cierre.
      Compra   : la MME9 venia cayendo y GIRA hacia arriba en la vela cerrada (vela senal).
                 Orden de compra stop por encima del maximo de la vela senal.
      Venta    : espejo (la MME9 venia subiendo y gira hacia abajo; venta stop bajo el minimo).
      Stop     : minimo (compra) / maximo (venta) de la vela senal.
      Salida   : al cierre de la vela en que la MME9 gira en contra.
      Anulacion: si la MME9 vuelve a girar en contra antes de activarse la orden.
    """
    diag = {"Estrategia": "Setup 9.1", "Activo": nombre, "Estado": "", "Sesgo 1H": "-", "Dir": "-",
            "Score": 0, "Confluencias": ""}
    if not mercado_abierto(tipo, ahora):
        diag["Estado"] = "Mercado cerrado"
        return diag, None
    if df is None or len(df) < 30:
        diag["Estado"] = "Sin datos"
        return diag, None
    ema = df["close"].ewm(span=9, adjust=False).mean().to_numpy()
    sube_ahora, subia_antes = ema[-1] > ema[-2], ema[-2] > ema[-3]
    if sube_ahora and not subia_antes:
        bias = 1
    elif (not sube_ahora) and subia_antes and ema[-1] < ema[-2]:
        bias = -1
    else:
        diag["Estado"] = "MME9 sin giro en la última vela"
        return diag, None
    diag["Dir"] = "LONG" if bias == 1 else "SHORT"
    if p.get("s91_filtro_1h", True) and d1 is not None and len(d1) >= 60:
        b1, _ = sesgo_ctx(d1.tail(VENTANA_CTX))         # filtro opcional: solo a favor del sesgo de 1H
        diag["Sesgo 1H"] = {1: "Alcista", -1: "Bajista", 0: "Neutral"}[b1]
        if b1 != bias:
            diag["Estado"] = "Giro de MME9 contra el sesgo 1H: descartado"
            return diag, None
    if not en_killzone(tipo, p, ahora):
        diag["Estado"] = "Giro de MME9 fuera de killzone"
        return diag, None
    v = df.iloc[-1]
    tick = _pip(nombre) if tipo == "forex" else 0.0
    gatillo = float(v["high"]) + tick if bias == 1 else float(v["low"]) - tick
    sl = float(v["low"]) - tick if bias == 1 else float(v["high"]) + tick
    if abs(gatillo - sl) <= 0:
        diag["Estado"] = "Vela señal sin rango"
        return diag, None
    riesgo_usd = p["banco"] * p["riesgo"] / 100
    tam, tam_txt = tamano(nombre, tipo, riesgo_usd, abs(gatillo - sl), gatillo)
    diag["Estado"] = "🚨 SEÑAL"
    lado = "COMPRA" if bias == 1 else "VENTA"
    return diag, {"estrategia": "Setup 9.1 (Larry Williams)", "activo": nombre, "tipo": tipo,
                  "dir": diag["Dir"], "bias": bias, "entrada": gatillo, "sl": sl, "tp1": None, "tp2": None,
                  "orden": "stop", "salida": "mme9",
                  "salida_txt": (f"Orden: {lado} STOP en {fmt(gatillo)} ({'máximo' if bias == 1 else 'mínimo'} "
                                 f"de la vela señal). Se anula si la MME9 vuelve a girar en contra.\n"
                                 f"Salida: al cierre de la vela en que la MME9 gire en contra."),
                  "riesgo_usd": riesgo_usd, "tamano": tam, "tamano_txt": tam_txt, "score": 1, "score_max": 1,
                  "confluencias": f"MME9 giró {'al alza' if bias == 1 else 'a la baja'}",
                  "kz": killzone_actual(ahora) or "-", "vela": df.index[-1],
                  "id": f"S91|{nombre}|{diag['Dir']}|{df.index[-1]}"}


def simular_91(o, h, l, c, ema, k0, d, gatillo, sl, max_barras):
    """Orden stop: se activa si el precio toca el gatillo antes de que la MME9 gire en contra.
    Sale en el stop o al cierre de la vela en que la MME9 gira en contra. Devuelve (R, idx salida, motivo, entrada)."""
    n = len(c)
    k = None
    for b in range(k0, min(k0 + 10, n)):
        if (d == 1 and h[b] >= gatillo) or (d == -1 and l[b] <= gatillo):
            k = b
            break
        if (ema[b] - ema[b - 1]) * d < 0:            # la media giro en contra: orden anulada
            return None
    if k is None:
        return None
    e = max(o[k], gatillo) if d == 1 else min(o[k], gatillo)   # si abre con gap, entra a la apertura
    riesgo = (e - sl) * d
    if riesgo <= 0:
        return None
    for b in range(k, min(k + max_barras, n)):
        if (d == 1 and l[b] <= sl) or (d == -1 and h[b] >= sl):
            return (sl - e) * d / riesgo, b, "SL", e
        if b > k and (ema[b] - ema[b - 1]) * d < 0:
            return (c[b] - e) * d / riesgo, b, "Giro MME9", e
    b = min(k + max_barras, n) - 1
    return (c[b] - e) * d / riesgo, b, "Tiempo", e


# ================================================================= ESTRATEGIA 6: ORB (Zarattini, Barbon y Aziz)
# Apertura de cada mercado (hora local de su sesion) y duracion de la sesion en horas.
SESION_ORB = {
    "EUR/USD": ("Europe/London", 8, 0, 8.0),        # apertura de Londres
    "GBP/USD": ("Europe/London", 8, 0, 8.0),
    "USD/JPY": ("Asia/Tokyo", 9, 0, 6.0),           # apertura de Tokio
    "USD/CAD": ("America/Toronto", 9, 30, 6.5),     # apertura de Toronto / Wall Street
    "XAU/USD (Oro)": ("America/New_York", 9, 30, 6.5),   # apertura de Wall Street
    "BTC/USDT": ("America/New_York", 9, 30, 6.5),
    "NASDAQ 100 (NQ)": ("America/New_York", 9, 30, 6.5),
    "S&P 500 (ES)": ("America/New_York", 9, 30, 6.5),
    "DOW 30 (YM)": ("America/New_York", 9, 30, 6.5),
    "DAX 40": ("Europe/Berlin", 9, 0, 8.5),
}


def evaluar_orb(nombre, tipo, df, d1, p, ahora, dd=None):
    """
    Opening Range Breakout de 5 minutos (Zarattini y Aziz 2023, "Can Day Trading Really Be Profitable?";
    Zarattini, Barbon y Aziz 2024). Reglas publicadas:
      - La PRIMERA vela de la sesion define la direccion: alcista -> LONG, bajista -> SHORT, doji -> nada.
      - Entrada a mercado al cierre de esa primera vela (apertura de la segunda).
      - Stop en el extremo contrario de la primera vela.
      - Objetivo 10R; si no llega, se cierra al final de la sesion.
    En 15m usa la primera vela de 15 minutos (el paper tambien compara rangos de 15/30/60 min).
    """
    diag = {"Estrategia": "ORB", "Activo": nombre, "Estado": "", "Sesgo 1H": "-", "Dir": "-",
            "Score": 0, "Confluencias": ""}
    ses = SESION_ORB.get(nombre)
    if ses is None or not mercado_abierto(tipo, ahora) or df is None or len(df) < 20:
        diag["Estado"] = ("Sin sesion definida" if ses is None else
                          "Mercado cerrado" if not mercado_abierto(tipo, ahora) else "Sin datos")
        return diag, None
    zona, hh, mm, dur = ses
    v_t = df.index[-1]
    loc = v_t.tz_convert(ZoneInfo(zona))
    if loc.weekday() >= 5 or (loc.hour, loc.minute) != (hh, mm):
        diag["Estado"] = f"Esperando la apertura {hh:02d}:{mm:02d} ({zona.split('/')[-1]})"
        return diag, None
    v = df.iloc[-1]
    cuerpo, rango = float(v["close"] - v["open"]), float(v["high"] - v["low"])
    if rango <= 0 or abs(cuerpo) < 0.1 * rango:
        diag["Estado"] = "Primera vela doji: sin operar"
        return diag, None
    bias = 1 if cuerpo > 0 else -1
    diag["Dir"] = "LONG" if bias == 1 else "SHORT"
    px = float(v["close"])
    sl = float(v["low"]) if bias == 1 else float(v["high"])
    riesgo_px = abs(px - sl)
    if riesgo_px <= 0:
        diag["Estado"] = "SL invalido"
        return diag, None
    riesgo_usd = p["banco"] * p["riesgo"] / 100
    tam, tam_txt = tamano(nombre, tipo, riesgo_usd, riesgo_px, px)
    tf_min = max(int(df.index.to_series().diff().tail(30).min().total_seconds() // 60), 1)
    fin = (v_t + pd.Timedelta(hours=dur)).tz_convert(NY)
    diag["Estado"] = "🚨 SEÑAL"
    return diag, {"estrategia": "ORB apertura (Zarattini/Aziz)", "activo": nombre, "tipo": tipo,
                  "dir": diag["Dir"], "bias": bias, "entrada": px, "sl": sl,
                  "tp1": px + bias * 10 * riesgo_px, "tp2": None, "tp_r": 10, "r1": 10,
                  "max_barras": int(dur * 60 / tf_min),
                  "salida_txt": f"TP (10R): {fmt(px + bias * 10 * riesgo_px)}\n"
                                f"Si no llega, cierra a las {fin:%H:%M} NY (fin de sesion).",
                  "riesgo_usd": riesgo_usd, "tamano": tam, "tamano_txt": tam_txt, "score": 1, "score_max": 1,
                  "confluencias": f"1a vela {'alcista' if bias == 1 else 'bajista'} de la apertura",
                  "kz": killzone_actual(ahora) or "-", "vela": v_t,
                  "id": f"ORB|{nombre}|{v_t:%Y-%m-%d}"}


# ================================================================= ESTRATEGIA 7: HOLY GRAIL (Linda Raschke)
def evaluar_holygrail(nombre, tipo, df, d1, p, ahora, dd=None):
    """
    Holy Grail de Linda Raschke (Street Smarts, Raschke y Connors). Reglas publicas:
      - ADX(14) > 30 y subiendo: tendencia fuerte. Direccion por +DI / -DI.
      - El precio retrocede y TOCA la EMA 20.
      - Orden stop por encima del maximo (LONG) / bajo el minimo (SHORT) de la vela que toco la EMA.
      - Stop en el otro extremo de esa vela. Objetivo: el ultimo maximo/minimo de la tendencia (min 1,5R).
    """
    diag = {"Estrategia": "HolyGrail", "Activo": nombre, "Estado": "", "Sesgo 1H": "-", "Dir": "-",
            "Score": 0, "Confluencias": ""}
    if not mercado_abierto(tipo, ahora) or df is None or len(df) < 60:
        diag["Estado"] = "Mercado cerrado" if df is not None else "Sin datos"
        return diag, None
    d = df.tail(VENTANA)
    hi, lo, cl = d["high"], d["low"], d["close"]
    up, dn = hi.diff(), -lo.diff()
    a = atr(d, 14)
    pdi = 100 * pd.Series(np.where((up > dn) & (up > 0), up, 0.0), index=d.index).ewm(alpha=1 / 14, adjust=False).mean() / a
    mdi = 100 * pd.Series(np.where((dn > up) & (dn > 0), dn, 0.0), index=d.index).ewm(alpha=1 / 14, adjust=False).mean() / a
    ax = adx(d, 14)
    ema = cl.ewm(span=20, adjust=False).mean()
    ax1, ax2 = float(ax.iloc[-1]), float(ax.iloc[-2])
    if not (ax1 > 30 and ax1 >= ax2):
        diag["Estado"] = f"ADX {ax1:.0f}: sin tendencia fuerte"
        return diag, None
    bias = 1 if pdi.iloc[-1] > mdi.iloc[-1] else -1
    diag["Dir"] = "LONG" if bias == 1 else "SHORT"
    v = d.iloc[-1]
    e = float(ema.iloc[-1])
    toca = (v["low"] <= e < v["high"]) if bias == 1 else (v["low"] < e <= v["high"])
    previas_lejos = (lo.iloc[-6:-1] > ema.iloc[-6:-1]).all() if bias == 1 else (hi.iloc[-6:-1] < ema.iloc[-6:-1]).all()
    if not (toca and previas_lejos):
        diag["Estado"] = "Tendencia fuerte, esperando retroceso a la EMA 20"
        return diag, None
    tick = _pip(nombre) if tipo == "forex" else 0.0
    gatillo = float(v["high"]) + tick if bias == 1 else float(v["low"]) - tick
    sl = float(v["low"]) - tick if bias == 1 else float(v["high"]) + tick
    riesgo_px = abs(gatillo - sl)
    extremo = float(hi.tail(20).max()) if bias == 1 else float(lo.tail(20).min())
    tp = extremo if (extremo - gatillo) * bias >= 1.5 * riesgo_px else gatillo + bias * 1.5 * riesgo_px
    riesgo_usd = p["banco"] * p["riesgo"] / 100
    tam, tam_txt = tamano(nombre, tipo, riesgo_usd, riesgo_px, gatillo)
    lado = "COMPRA" if bias == 1 else "VENTA"
    diag["Estado"] = "🚨 SEÑAL"
    return diag, {"estrategia": "Holy Grail (Raschke)", "activo": nombre, "tipo": tipo, "dir": diag["Dir"],
                  "bias": bias, "entrada": gatillo, "sl": sl, "tp1": tp, "tp2": None, "orden": "stop_tp",
                  "salida_txt": f"Orden: {lado} STOP en {fmt(gatillo)} (válida 3 velas).\n"
                                f"TP: {fmt(tp)} ({abs(tp - gatillo) / riesgo_px:.1f}R)",
                  "riesgo_usd": riesgo_usd, "tamano": tam, "tamano_txt": tam_txt, "score": 1, "score_max": 1,
                  "confluencias": f"ADX {ax1:.0f} subiendo · retroceso a EMA20",
                  "kz": killzone_actual(ahora) or "-", "vela": d.index[-1],
                  "id": f"HG|{nombre}|{diag['Dir']}|{d.index[-1]}"}


# ================================================================= ESTRATEGIA 8: GOTOBI (fixing de Tokio, USD/JPY)
JST = ZoneInfo("Asia/Tokyo")


def es_gotobi(fecha):
    """Dias 5, 10, 15, 20, 25 y ultimo del mes (hora de Japon). Si cae en fin de semana, se adelanta al viernes."""
    import calendar
    ult = calendar.monthrange(fecha.year, fecha.month)[1]
    objetivos = []
    for dia in (5, 10, 15, 20, 25, ult):
        f = fecha.replace(day=dia)
        while f.weekday() >= 5:
            f -= timedelta(days=1)
        objetivos.append(f)
    return fecha in objetivos


def evaluar_gotobi(nombre, tipo, df, d1, p, ahora, dd=None):
    """
    Efecto Gotobi: en los dias de pago de Japon (5, 10, 15...) los importadores compran dolares antes del
    fixing de Tokio de las 9:55 JST, y USD/JPY tiende a subir de 8:00 a 9:55 JST (NBER w22820 sobre el
    fixing de Tokio; flujos descritos por la prensa de mercado). Regla: COMPRA a las 8:00 JST, cierre
    a las 9:55 JST. Stop = 1 ATR de 1H.
    """
    diag = {"Estrategia": "Gotobi", "Activo": nombre, "Estado": "", "Sesgo 1H": "-", "Dir": "-",
            "Score": 0, "Confluencias": ""}
    if nombre != "USD/JPY" or df is None or d1 is None or len(d1) < 20:
        diag["Estado"] = "Solo USD/JPY"
        return diag, None
    v_t = df.index[-1]
    tf_min = max(int(df.index.to_series().diff().tail(30).min().total_seconds() // 60), 1)
    cierre = (v_t + pd.Timedelta(minutes=tf_min)).tz_convert(JST)
    if (cierre.hour, cierre.minute) != (8, 0) or not es_gotobi(cierre.date()):
        diag["Estado"] = "Esperando día Gotobi 8:00 JST"
        return diag, None
    px = float(df["close"].iloc[-1])
    sl = px - float(atr(d1).iloc[-1])
    riesgo_usd = p["banco"] * p["riesgo"] / 100
    tam, tam_txt = tamano(nombre, tipo, riesgo_usd, px - sl, px)
    diag["Dir"], diag["Estado"] = "LONG", "🚨 SEÑAL"
    fin = cierre.replace(hour=9, minute=55).tz_convert(NY)
    return diag, {"estrategia": "Gotobi (fixing Tokio)", "activo": nombre, "tipo": tipo, "dir": "LONG",
                  "bias": 1, "entrada": px, "sl": sl, "tp1": None, "tp2": None, "tp_r": 50,
                  "max_barras": max(int(115 / tf_min), 1),
                  "salida_txt": f"Salida: a las 9:55 JST ({fin:%H:%M} NY), en el fixing de Tokio.",
                  "riesgo_usd": riesgo_usd, "tamano": tam, "tamano_txt": tam_txt, "score": 1, "score_max": 1,
                  "confluencias": f"día Gotobi {cierre:%d/%m}", "kz": killzone_actual(ahora) or "-",
                  "vela": v_t, "id": f"GOTOBI|{cierre:%Y-%m-%d}"}


def simular_stop(o, h, l, c, k0, d, gatillo, sl, tp, max_barras, validez=3):
    """Orden stop pendiente 'validez' velas, luego SL / TP fijos. Devuelve (R, idx salida, motivo, entrada)."""
    n = len(c)
    k = None
    for b in range(k0, min(k0 + validez, n)):
        if (d == 1 and l[b] <= sl) or (d == -1 and h[b] >= sl):
            return None                                  # invalidada antes de activarse
        if (d == 1 and h[b] >= gatillo) or (d == -1 and l[b] <= gatillo):
            k = b
            break
    if k is None:
        return None
    e = max(o[k], gatillo) if d == 1 else min(o[k], gatillo)
    riesgo = (e - sl) * d
    if riesgo <= 0:
        return None
    for b in range(k, min(k + max_barras, n)):
        if (d == 1 and l[b] <= sl) or (d == -1 and h[b] >= sl):
            return (sl - e) * d / riesgo, b, "SL", e
        if (d == 1 and h[b] >= tp) or (d == -1 and l[b] <= tp):
            return (tp - e) * d / riesgo, b, "TP", e
    b = min(k + max_barras, n) - 1
    return (c[b] - e) * d / riesgo, b, "Tiempo", e


# ================================================================= ESTRATEGIA 9: CONFLUENCIA VWAP (tu Quant Terminal)
ADX_VWAP = {"oro": 40.0, "cripto": 38.0, "forex": 35.0, "indice": 35.0}


def evaluar_vwap(nombre, tipo, df, d1, p, ahora, dd=None):
    """
    'Senal Conjunta Unificada' del Institutional Quant Terminal, con sus reglas originales:
      ADX (suavizado span 14) > 35 forex / 38 cripto / 40 oro.
      Confluencias LONG (minimo 2 de 3, y precio >= VWAP):
        1) precio >= VWAP y EMA9 >= EMA21
        2) CVD subiendo o volumen > 1,35x su media de 20
        3) Order Block alcista, FVG alcista o descuento >= 35% del rango de 20 velas
      SHORT en espejo. Freno de exhaustacion: si la vela es enorme tras un tramo largo, no entra.
      SL: swing de 6 velas -/+ 0,6 rango medio (minimo 0,9 rango medio). TP: 2,5R.
    Correcciones: solo velas cerradas, VWAP que se reinicia cada dia (00:00 UTC) y, sin volumen
    (forex en Yahoo), VWAP / CVD ponderados por tiempo en lugar de quedar anulados.
    """
    diag = {"Estrategia": "VWAP", "Activo": nombre, "Estado": "", "Sesgo 1H": "-", "Dir": "-",
            "Score": 0, "Confluencias": ""}
    if not mercado_abierto(tipo, ahora) or df is None or len(df) < 85:
        diag["Estado"] = "Mercado cerrado" if df is not None else "Sin datos"
        return diag, None
    d = df.tail(VENTANA).copy()
    vol = d["volume"] if d["volume"].tail(85).sum() > 0 else pd.Series(1.0, index=d.index)
    tp_ = (d["high"] + d["low"] + d["close"]) / 3
    dia = d.index.floor("1D")
    vwap = (tp_ * vol).groupby(dia).cumsum() / vol.groupby(dia).cumsum()
    up, dn = d["high"].diff(), d["low"].shift() - d["low"]
    pdm = pd.Series(np.where((up > dn) & (up > 0), up, 0.0), index=d.index)
    mdm = pd.Series(np.where((dn > up) & (dn > 0), dn, 0.0), index=d.index)
    tr = pd.concat([d["high"] - d["low"], (d["high"] - d["close"].shift()).abs(),
                    (d["low"] - d["close"].shift()).abs()], axis=1).max(axis=1)
    a_ = tr.ewm(span=14, adjust=False).mean().replace(0, np.nan)
    pdi = 100 * pdm.ewm(span=14, adjust=False).mean() / a_
    mdi = 100 * mdm.ewm(span=14, adjust=False).mean() / a_
    adx_v = float((100 * (pdi - mdi).abs() / (pdi + mdi).replace(0, np.nan)).ewm(span=14, adjust=False).mean().iloc[-1])
    umbral = ADX_VWAP[tipo]
    if not adx_v > umbral:
        diag["Estado"] = f"ADX {adx_v:.0f} (mínimo {umbral:.0f})"
        return diag, None
    w = d.tail(85)
    o, h, l, c = (w[x].to_numpy() for x in ("open", "high", "low", "close"))
    v = vol.tail(85).to_numpy()
    rngo = np.where(h - l == 0, np.nan, h - l)
    cvd = np.nancumsum(v * np.nan_to_num((c - o) / rngo))
    ef = w["close"].ewm(span=9, adjust=False).mean().iloc[-1]
    es = w["close"].ewm(span=21, adjust=False).mean().iloc[-1]
    px, vw = float(c[-1]), float(vwap.iloc[-1])
    rangos = h[-21:-1] - l[-21:-1]
    atr_ref = float(rangos.mean()) if rangos.mean() > 0 else max(h[-1] - l[-1], px * 0.0025)
    # freno de exhaustacion
    if (h[-1] - l[-1]) > rangos.max() * 1.15 and abs(c[-2] - c[-10]) > rangos.mean() * 3:
        diag["Estado"] = "Freno de exhaustación: esperar retroceso"
        return diag, None
    vol_ma = float(v[-20:].mean())
    desbalance = vol_ma > 0 and v[-1] > vol_ma * 1.35
    fvg = None
    for k in (len(c) - 1, len(c) - 2):
        if l[k] > h[k - 2]:
            fvg = "up"
            break
        if h[k] < l[k - 2]:
            fvg = "dn"
            break
    rmax, rmin = h[-20:].max(), l[-20:].min()
    amp = max(rmax - rmin, 1e-12)
    ob_up = c[-2] < o[-2] and c[-1] > o[-1] and px > h[-2]
    ob_dn = c[-2] > o[-2] and c[-1] < o[-1] and px < l[-2]
    sw_lo, sw_hi = float(l[-7:-1].min()), float(h[-7:-1].max())
    bias, motivos = 0, []
    m_l = [("VWAP/EMA alcista", px >= vw and ef >= es), ("CVD/volumen comprador", cvd[-1] > cvd[-2] or desbalance),
           ("OB/FVG/descuento", ob_up or fvg == "up" or (rmax - px) / amp * 100 >= 35)]
    m_s = [("VWAP/EMA bajista", px <= vw and ef <= es), ("CVD/volumen vendedor", cvd[-1] < cvd[-2] or desbalance),
           ("OB/FVG/premium", ob_dn or fvg == "dn" or (px - rmin) / amp * 100 >= 35)]
    if sum(x for _, x in m_l) >= 2 and px >= vw:
        bias, motivos = 1, [n for n, x in m_l if x]
        riesgo = max(px - (min(sw_lo, l[-1]) - atr_ref * 0.6), atr_ref * 0.9)
    elif sum(x for _, x in m_s) >= 2 and px <= vw:
        bias, motivos = -1, [n for n, x in m_s if x]
        riesgo = max((max(sw_hi, h[-1]) + atr_ref * 0.6) - px, atr_ref * 0.9)
    if bias == 0:
        diag["Estado"] = "Sin confluencia 2/3"
        return diag, None
    diag["Dir"], diag["Score"] = ("LONG" if bias == 1 else "SHORT"), len(motivos)
    sl = px - bias * riesgo
    riesgo_usd = p["banco"] * p["riesgo"] / 100
    tam, tam_txt = tamano(nombre, tipo, riesgo_usd, riesgo, px)
    diag["Estado"] = "🚨 SEÑAL"
    return diag, {"estrategia": "Confluencia VWAP (Quant Terminal)", "activo": nombre, "tipo": tipo,
                  "dir": diag["Dir"], "bias": bias, "entrada": px, "sl": sl, "tp1": px + bias * 2.5 * riesgo,
                  "tp2": None, "tp_r": 2.5, "r1": 2.5, "riesgo_usd": riesgo_usd, "tamano": tam,
                  "tamano_txt": tam_txt, "score": len(motivos), "score_max": 3,
                  "confluencias": f"ADX {adx_v:.0f} · " + " · ".join(motivos),
                  "kz": killzone_actual(ahora) or "-", "vela": d.index[-1],
                  "id": f"VWAP|{nombre}|{diag['Dir']}|{d.index[-1]}"}


# ================================================================= ESTRATEGIA 10: NOISE AREA (Zarattini, Aziz y Barbon)
def _sigma_ruido(d1, t0, horas, dias=14):
    """Movimiento medio absoluto desde la apertura, por hora de sesion, de los 14 dias previos (velas 1H)."""
    base0 = t0.floor("1h")
    movs = {k: [] for k in range(1, horas + 1)}
    usados = 0
    for n in range(1, 40):
        if usados >= dias:
            break
        b = base0 - pd.Timedelta(days=n)
        if b not in d1.index:
            continue
        o_d = float(d1.at[b, "open"])
        usados += 1
        for k in range(1, horas + 1):
            t = b + pd.Timedelta(hours=k - 1)
            if t in d1.index:
                movs[k].append(abs(float(d1.at[t, "close"]) / o_d - 1))
    if usados < 5:
        return None
    return {k: (float(np.mean(v)) if v else np.nan) for k, v in movs.items()}


def _inicio_sesion(nombre, cierre):
    ses = SESION_ORB.get(nombre)
    if ses is None:
        return None
    zona, hh, mm, dur = ses
    loc = cierre.tz_convert(ZoneInfo(zona))
    t0 = loc.normalize() + pd.Timedelta(hours=hh, minutes=mm)
    if loc.weekday() >= 5 or not (t0 < loc <= t0 + pd.Timedelta(hours=dur)):
        return None
    return t0.tz_convert("UTC"), dur


def evaluar_ruido(nombre, tipo, df, d1, p, ahora, dd=None):
    """
    Intraday momentum 'Noise Area' (Zarattini, Aziz y Barbon 2024, "Beat the Market"):
      - Zona de ruido: apertura (o cierre previo) +/- movimiento medio de los ultimos 14 dias a esa hora.
      - Cada 30 minutos: si el precio cierra FUERA de la zona y del lado correcto del VWAP -> entra a favor.
      - Stop dinamico: el mayor entre el limite de la zona y el VWAP (LONG); sale si una vela de 30 min
        cierra al otro lado. Cierre obligatorio al final de la sesion.
    Sesiones: Londres (EUR, GBP), Tokio (JPY), Wall Street 9:30 (Oro, BTC).
    """
    diag = {"Estrategia": "Ruido", "Activo": nombre, "Estado": "", "Sesgo 1H": "-", "Dir": "-",
            "Score": 0, "Confluencias": ""}
    if not mercado_abierto(tipo, ahora) or df is None or d1 is None or len(df) < 30:
        diag["Estado"] = "Mercado cerrado" if df is not None else "Sin datos"
        return diag, None
    tf_min = max(int(df.index.to_series().diff().tail(30).min().total_seconds() // 60), 1)
    cierre = df.index[-1] + pd.Timedelta(minutes=tf_min)
    ini = _inicio_sesion(nombre, cierre)
    if ini is None:
        diag["Estado"] = "Fuera de sesión"
        return diag, None
    t0, dur = ini
    m = int((cierre - t0).total_seconds() // 60)
    if m < 30 or m % 30 or m >= dur * 60:
        diag["Estado"] = "Esperando control de cada 30 min"
        return diag, None
    ses, prev = df[df.index >= t0], df[df.index < t0]
    if ses.empty or prev.empty or ses.index[0] != t0:
        diag["Estado"] = "Sin apertura de sesión en los datos"
        return diag, None
    sig = _sigma_ruido(d1, t0, int(np.ceil(dur)))
    k = int(np.ceil(m / 60))
    if sig is None or not np.isfinite(sig.get(k, np.nan)):
        diag["Estado"] = "Sin 14 días de historial"
        return diag, None
    o_d, c_prev = float(ses["open"].iloc[0]), float(prev["close"].iloc[-1])
    hi_b, lo_b = max(o_d, c_prev), min(o_d, c_prev)
    ub, lb = hi_b * (1 + sig[k]), lo_b * (1 - sig[k])
    vol = ses["volume"] if ses["volume"].sum() > 0 else pd.Series(1.0, index=ses.index)
    vwap = float(((ses["high"] + ses["low"] + ses["close"]) / 3 * vol).sum() / vol.sum())
    px = float(ses["close"].iloc[-1])
    diag["_niveles"] = {"Ruido sup": ub, "Ruido inf": lb, "VWAP": vwap}
    if px > ub and px > vwap:
        bias, trail = 1, max(ub, vwap)
    elif px < lb and px < vwap:
        bias, trail = -1, min(lb, vwap)
    else:
        diag["Estado"] = "Precio dentro de la zona de ruido"
        return diag, None
    # stop de emergencia a un 'ancho de ruido'; la salida normal es el stop dinamico cada 30 min
    sl = px - bias * max(sig[k] * hi_b, abs(px - trail))
    if abs(px - sl) <= 0:
        diag["Estado"] = "SL invalido"
        return diag, None
    diag["Dir"] = "LONG" if bias == 1 else "SHORT"
    riesgo_usd = p["banco"] * p["riesgo"] / 100
    tam, tam_txt = tamano(nombre, tipo, riesgo_usd, abs(px - sl), px)
    fin = (t0 + pd.Timedelta(hours=dur)).tz_convert(NY)
    diag["Estado"] = "🚨 SEÑAL"
    return diag, {"estrategia": "Noise Area (Zarattini/Aziz)", "activo": nombre, "tipo": tipo, "dir": diag["Dir"],
                  "bias": bias, "entrada": px, "sl": sl, "tp1": None, "tp2": None, "orden": "ruido",
                  "t0": t0, "dur": dur, "sig": sig, "base": hi_b if bias == 1 else lo_b,
                  "salida_txt": (f"Stop dinámico: {'el mayor' if bias == 1 else 'el menor'} entre el límite de "
                                 f"ruido y el VWAP (hoy {fmt(trail)}). Revisa cada 30 min: sal si una vela cierra al "
                                 f"otro lado.\nSL de emergencia: {fmt(sl)}. Cierre obligatorio a las {fin:%H:%M} NY."),
                  "riesgo_usd": riesgo_usd, "tamano": tam, "tamano_txt": tam_txt, "score": 1, "score_max": 1,
                  "confluencias": f"fuera de la zona de ruido ({fmt(lb)} - {fmt(ub)}) · VWAP {fmt(vwap)}",
                  "kz": killzone_actual(ahora) or "-", "vela": df.index[-1],
                  "id": f"RUIDO|{nombre}|{diag['Dir']}|{t0:%Y-%m-%d}"}


def simular_ruido(df, k, s, paso):
    """Entra a la apertura de la vela k. Stop inicial fijo intravela; cada 30 min sale si el cierre cruza
    el stop dinamico max(limite de ruido, VWAP); sale al final de la sesion."""
    d, sl, t0 = s["bias"], s["sl"], s["t0"]
    o, h, l, c = (df[x].to_numpy() for x in ("open", "high", "low", "close"))
    v = df["volume"].to_numpy() if df["volume"].sum() > 0 else np.ones(len(df))
    if k >= len(c):
        return None
    e = o[k]
    riesgo = (e - sl) * d
    if riesgo <= 0:
        return None
    i0 = int(df.index.searchsorted(t0))
    tp_ = (h + l + c) / 3
    pv, vv = np.cumsum(tp_ * v), np.cumsum(v)
    fin_t = t0 + pd.Timedelta(hours=s["dur"])
    b = k
    for b in range(k, len(c)):
        cierre = df.index[b] + paso
        if (d == 1 and l[b] <= sl) or (d == -1 and h[b] >= sl):
            return (sl - e) * d / riesgo, b, "SL", e
        if cierre >= fin_t:
            return (c[b] - e) * d / riesgo, b, "Fin sesión", e
        m = int((cierre - t0).total_seconds() // 60)
        if m % 30 == 0:
            vw = (pv[b] - (pv[i0 - 1] if i0 > 0 else 0)) / (vv[b] - (vv[i0 - 1] if i0 > 0 else 0))
            sg = s["sig"].get(int(np.ceil(m / 60)), np.nan)
            lim = s["base"] * (1 + d * sg) if np.isfinite(sg) else vw
            trail = max(lim, vw) if d == 1 else min(lim, vw)
            if (c[b] - trail) * d < 0:
                return (c[b] - e) * d / riesgo, b, "Stop dinámico", e
    return (c[b] - e) * d / riesgo, b, "Tiempo", e


# ================================================================= ESTRATEGIAS 11-12: ESTACIONALIDAD HORARIA
def _por_hora(nombre, tipo, df, d1, p, ahora, clave, titulo, zona, h_ent, h_sal, dir_, fuente):
    """Compra (o vende) al cierre de la vela que termina a h_ent (hora 'zona') y cierra a h_sal.
    Stop de emergencia: 1,5 ATR de 1H."""
    diag = {"Estrategia": clave, "Activo": nombre, "Estado": "", "Sesgo 1H": "-", "Dir": "-",
            "Score": 0, "Confluencias": ""}
    if not mercado_abierto(tipo, ahora) or df is None or d1 is None or len(d1) < 20 or len(df) < 30:
        diag["Estado"] = "Mercado cerrado" if df is not None else "Sin datos"
        return diag, None
    tf_min = max(int(df.index.to_series().diff().tail(30).min().total_seconds() // 60), 1)
    cierre = (df.index[-1] + pd.Timedelta(minutes=tf_min)).tz_convert(ZoneInfo(zona))
    if (cierre.hour, cierre.minute) != (h_ent, 0) or (tipo != "cripto" and cierre.weekday() == 4 and h_sal < h_ent):
        diag["Estado"] = f"Esperando las {h_ent:02d}:00 ({zona.split('/')[-1]})"
        return diag, None
    px = float(df["close"].iloc[-1])
    sl = px - dir_ * 1.5 * float(atr(d1).iloc[-1])
    horas = (h_sal - h_ent) % 24
    salida = (cierre + pd.Timedelta(hours=horas)).tz_convert(NY)
    riesgo_usd = p["banco"] * p["riesgo"] / 100
    tam, tam_txt = tamano(nombre, tipo, riesgo_usd, abs(px - sl), px)
    diag["Dir"], diag["Estado"] = ("LONG" if dir_ == 1 else "SHORT"), "🚨 SEÑAL"
    return diag, {"estrategia": titulo, "activo": nombre, "tipo": tipo, "dir": diag["Dir"], "bias": dir_,
                  "entrada": px, "sl": sl, "tp1": None, "tp2": None, "tp_r": 50,
                  "max_barras": max(int(horas * 60 / tf_min), 1),
                  "salida_txt": f"Salida: a las {salida:%H:%M} NY ({h_sal:02d}:00 {zona.split('/')[-1]}), gane o pierda.",
                  "riesgo_usd": riesgo_usd, "tamano": tam, "tamano_txt": tam_txt, "score": 1, "score_max": 1,
                  "confluencias": fuente, "kz": killzone_actual(ahora) or "-", "vela": df.index[-1],
                  "id": f"{clave}|{nombre}|{cierre:%Y-%m-%d}"}


def evaluar_hora_btc(nombre, tipo, df, d1, p, ahora, dd=None):
    """Estacionalidad de Bitcoin (Quantpedia, 'The Seasonality of Bitcoin'): compra a las 21:00 UTC y vende
    a las 23:00 UTC, las dos horas con mayor rendimiento medio (mercados tradicionales cerrados)."""
    if tipo != "cripto":
        return {"Estrategia": "HoraBTC", "Activo": nombre, "Estado": "Solo cripto"}, None
    return _por_hora(nombre, tipo, df, d1, p, ahora, "HoraBTC", "BTC 21-23 UTC (Quantpedia)", "UTC",
                     21, 23, 1, "estacionalidad horaria 21-23 UTC")


def evaluar_noche_oro(nombre, tipo, df, d1, p, ahora, dd=None):
    """Efecto noche del oro (Blose, Gondhalekar y Kort 2018, Journal of Economics and Finance): el rendimiento
    del oro es positivo de noche y negativo de dia. Compra a las 19:00 NY (reapertura de Globex) y cierra a
    las 08:00 NY, antes de la apertura de COMEX. No abre el viernes (fin de semana)."""
    if tipo != "oro":
        return {"Estrategia": "NocheOro", "Activo": nombre, "Estado": "Solo oro"}, None
    return _por_hora(nombre, tipo, df, d1, p, ahora, "NocheOro", "Oro de noche (Blose et al.)",
                     "America/New_York", 19, 8, 1, "efecto noche del oro")


# ================================================================= ESTRATEGIA 13: ORB + RETEST (sesion de Nueva York)
APERTURA_RETEST = {"DAX 40": ("Europe/Berlin", 9, 0)}      # el resto: Wall Street 9:30 NY


def evaluar_orb_retest(nombre, tipo, df, d1, p, ahora, dd=None):
    """
    Opening Range Breakout + Retest:
      Rango  : maximo y minimo de los primeros 30 min tras la apertura (9:30-10:00 NY; DAX 9:00-9:30 Frankfurt).
      Ruptura: una vela CIERRA fuera del rango. No se entra en la ruptura.
      Retest : el precio vuelve a tocar el nivel roto y cierra a favor (vela de rechazo) -> entrada.
               Si antes una vela cierra de nuevo dentro del rango, el setup se anula.
      Stop   : detras del ultimo minimo/maximo desde la ruptura. TP: 2R. Ventana: hasta 2,5 h tras la apertura.
    """
    diag = {"Estrategia": "ORBRetest", "Activo": nombre, "Estado": "", "Sesgo 1H": "-", "Dir": "-",
            "Score": 0, "Confluencias": ""}
    if not mercado_abierto(tipo, ahora) or df is None or len(df) < 30:
        diag["Estado"] = "Mercado cerrado" if df is not None else "Sin datos"
        return diag, None
    zona, hh, mm = APERTURA_RETEST.get(nombre, ("America/New_York", 9, 30))
    tf_min = max(int(df.index.to_series().diff().tail(30).min().total_seconds() // 60), 1)
    cierre = (df.index[-1] + pd.Timedelta(minutes=tf_min)).tz_convert(ZoneInfo(zona))
    t0 = cierre.normalize() + pd.Timedelta(hours=hh, minutes=mm)
    t_r, t_fin = t0 + pd.Timedelta(minutes=30), t0 + pd.Timedelta(hours=2.5)
    if cierre.weekday() >= 5 or not (t_r < cierre <= t_fin):
        diag["Estado"] = "Fuera de la ventana de apertura"
        return diag, None
    t0u, tru = t0.tz_convert("UTC"), t_r.tz_convert("UTC")
    rango = df[(df.index >= t0u) & (df.index < tru)]
    if len(rango) < max(30 // tf_min, 1):
        diag["Estado"] = "Sin rango de apertura completo"
        return diag, None
    rh, rl = float(rango["high"].max()), float(rango["low"].min())
    diag["_niveles"] = {"Rango H": rh, "Rango L": rl}
    post = df[df.index >= tru]
    c, o, h, l = (post[x].to_numpy() for x in ("close", "open", "high", "low"))
    for d_, nivel in ((1, rh), (-1, rl)):
        fuera = np.where((c > nivel) if d_ == 1 else (c < nivel))[0]
        if len(fuera) == 0 or fuera[0] >= len(c) - 1:
            continue
        b0 = int(fuera[0])                                    # vela de ruptura
        tras = slice(b0 + 1, len(c))
        dentro = (c[tras] < nivel) if d_ == 1 else (c[tras] > nivel)
        toca = (l[tras] <= nivel) if d_ == 1 else (h[tras] >= nivel)
        rechazo = toca & ~dentro & ((c[tras] > o[tras]) if d_ == 1 else (c[tras] < o[tras]))
        if dentro.any() and (not rechazo.any() or dentro.argmax() < rechazo.argmax()):
            continue                                          # volvio al rango antes del retest: anulado
        if not rechazo.any() or rechazo.argmax() != len(rechazo) - 1:
            continue                                          # el retest valido debe ser la ultima vela
        px = float(c[-1])
        ext = float(l[b0:].min()) if d_ == 1 else float(h[b0:].max())
        buf = 0.1 * float(atr(df.tail(100)).iloc[-1])
        sl = ext - d_ * buf
        if (px - sl) * d_ <= 0:
            continue
        r = abs(px - sl)
        riesgo_usd = p["banco"] * p["riesgo"] / 100
        tam, tam_txt = tamano(nombre, tipo, riesgo_usd, r, px)
        diag["Dir"], diag["Estado"] = ("LONG" if d_ == 1 else "SHORT"), "🚨 SEÑAL"
        return diag, {"estrategia": "ORB + Retest", "activo": nombre, "tipo": tipo, "dir": diag["Dir"],
                      "bias": d_, "entrada": px, "sl": sl, "tp1": px + d_ * 2 * r, "tp2": None, "tp_r": 2, "r1": 2,
                      "max_barras": int(6 * 60 / tf_min), "riesgo_usd": riesgo_usd, "tamano": tam,
                      "tamano_txt": tam_txt, "score": 1, "score_max": 1,
                      "confluencias": f"rompió y re-testeó el {'máximo' if d_ == 1 else 'mínimo'} del rango "
                                      f"de apertura ({fmt(rl)} - {fmt(rh)})",
                      "kz": killzone_actual(ahora) or "-", "vela": df.index[-1],
                      "id": f"ORBR|{nombre}|{t0:%Y-%m-%d}"}
    diag["Estado"] = "Esperando ruptura + retest"
    return diag, None


# ================================================================= ESTRATEGIA 14: BARRIDO DEL RANGO ASIATICO
def _sweep_long(df, lo, hi, ini_idx, ref=5):
    """LONG: alguna vela desde la apertura de Londres perforo el minimo de Asia; la ultima vela cerrada vuelve
    DENTRO del rango y rompe el maximo de las 'ref' velas previas al extremo del barrido (MSS). Primera vez."""
    l, c, h = df["low"].to_numpy(), df["close"].to_numpy(), df["high"].to_numpy()
    n = len(c)
    seg = l[ini_idx:]
    if len(seg) < 2 or seg[:-1].min() >= lo:
        return None
    j = ini_idx + int(np.argmin(l[ini_idx:n - 1]))          # extremo del barrido
    if j < ref or l[j] >= lo:
        return None
    mss = float(h[j - ref:j].max())
    if not (c[-1] > mss and lo < c[-1] < hi):
        return None
    previos = c[j + 1:n - 1]
    if ((previos > mss) & (previos > lo)).any():            # ya habia confirmado antes
        return None
    return {"extremo": float(l[j]), "mss": mss}


def evaluar_asia_sweep(nombre, tipo, df, d1, p, ahora, dd=None):
    """
    Liquidity Sweep del rango asiatico en la apertura de Londres:
      Rango Asia: 00:00-07:00 UTC (19:00-02:00 hora Colombia).
      Ventana   : 07:00-10:30 UTC (2:00-5:30 hora Colombia).
      Setup     : falsa ruptura (mecha fuera del rango), el precio vuelve a entrar y hace un quiebre de
                  estructura interno (MSS) -> entrada en contra del barrido.
      Stop      : detras del extremo del barrido. TP: el lado opuesto del rango asiatico (minimo 1R).
    """
    diag = {"Estrategia": "AsiaSweep", "Activo": nombre, "Estado": "", "Sesgo 1H": "-", "Dir": "-",
            "Score": 0, "Confluencias": ""}
    if not mercado_abierto(tipo, ahora) or df is None or len(df) < 40:
        diag["Estado"] = "Mercado cerrado" if df is not None else "Sin datos"
        return diag, None
    tf_min = max(int(df.index.to_series().diff().tail(30).min().total_seconds() // 60), 1)
    cierre = df.index[-1] + pd.Timedelta(minutes=tf_min)
    hoy = cierre.normalize()
    t_ld, t_fin = hoy + pd.Timedelta(hours=7), hoy + pd.Timedelta(hours=10, minutes=30)
    if cierre.weekday() >= 5 or not (t_ld < cierre <= t_fin):
        diag["Estado"] = "Fuera de la ventana de Londres"
        return diag, None
    asia = df[(df.index >= hoy) & (df.index < t_ld)]
    if len(asia) < 7 * 60 // tf_min // 2:
        diag["Estado"] = "Sin rango asiático"
        return diag, None
    hi, lo = float(asia["high"].max()), float(asia["low"].min())
    diag["_niveles"] = {"Asia H": hi, "Asia L": lo}
    d = df[df.index >= hoy - pd.Timedelta(hours=2)]
    ini = int(d.index.searchsorted(t_ld))
    s = _sweep_long(d, lo, hi, ini)
    bias = 1
    if s is None:
        s = _sweep_long(_flip(d), -hi, -lo, ini)
        if s is not None:
            s, bias = {"extremo": -s["extremo"], "mss": -s["mss"]}, -1
    if s is None:
        diag["Estado"] = "Esperando barrido de Asia + quiebre de estructura"
        return diag, None
    px = float(d["close"].iloc[-1])
    buf = 0.1 * float(atr(df.tail(100)).iloc[-1])
    sl = s["extremo"] - bias * buf
    tp = hi if bias == 1 else lo
    r = abs(px - sl)
    if r <= 0 or (tp - px) * bias < r:
        diag["Estado"] = "Barrido válido pero objetivo < 1R"
        return diag, None
    riesgo_usd = p["banco"] * p["riesgo"] / 100
    tam, tam_txt = tamano(nombre, tipo, riesgo_usd, r, px)
    diag["Dir"], diag["Estado"] = ("LONG" if bias == 1 else "SHORT"), "🚨 SEÑAL"
    return diag, {"estrategia": "Barrido de Asia (Londres)", "activo": nombre, "tipo": tipo, "dir": diag["Dir"],
                  "bias": bias, "entrada": px, "sl": sl, "tp1": tp, "tp2": None, "tp_r": (tp - px) * bias / r,
                  "r1": round((tp - px) * bias / r, 1), "max_barras": int(8 * 60 / tf_min),
                  "riesgo_usd": riesgo_usd, "tamano": tam, "tamano_txt": tam_txt, "score": 1, "score_max": 1,
                  "confluencias": f"barrió el {'mínimo' if bias == 1 else 'máximo'} de Asia y volvió al rango",
                  "kz": killzone_actual(ahora) or "-", "vela": df.index[-1],
                  "id": f"ASIA|{nombre}|{hoy:%Y-%m-%d}"}


# ================================================================= ESTRATEGIA 15: EMA 9/21 + VWAP (retroceso)
def evaluar_ema_vwap(nombre, tipo, df, d1, p, ahora, dd=None):
    """
    Tendencia con VWAP + EMA 9/21:
      Contexto: precio sobre el VWAP del dia -> solo compras; debajo -> solo ventas.
      Setup   : retroceso que toca la EMA 21 o el VWAP en las ultimas 3 velas.
      Gatillo : la EMA 9 vuelve a apuntar a favor (giro) con EMA9 del lado correcto de la EMA21.
      Volumen : si hay volumen, la vela gatillo supera la media de 20 velas.
      Stop    : extremo del retroceso (5 velas) -/+ 0,1 ATR. TP: 2R.
    """
    diag = {"Estrategia": "EMAVWAP", "Activo": nombre, "Estado": "", "Sesgo 1H": "-", "Dir": "-",
            "Score": 0, "Confluencias": ""}
    if not mercado_abierto(tipo, ahora) or df is None or len(df) < 60:
        diag["Estado"] = "Mercado cerrado" if df is not None else "Sin datos"
        return diag, None
    d = df.tail(VENTANA)
    dia = d.index.floor("1D")
    hoy = d[dia == dia[-1]]
    if len(hoy) < 6:
        diag["Estado"] = "Inicio del día: VWAP sin datos"
        return diag, None
    vol = hoy["volume"] if hoy["volume"].sum() > 0 else pd.Series(1.0, index=hoy.index)
    vwap = ((hoy["high"] + hoy["low"] + hoy["close"]) / 3 * vol).cumsum() / vol.cumsum()
    e9, e21 = d["close"].ewm(span=9, adjust=False).mean(), d["close"].ewm(span=21, adjust=False).mean()
    px, vw = float(d["close"].iloc[-1]), float(vwap.iloc[-1])
    bias = 1 if px > vw else -1
    giro = (e9.iloc[-1] - e9.iloc[-2]) * bias > 0 and (e9.iloc[-2] - e9.iloc[-3]) * bias <= 0
    lado = (e9.iloc[-1] - e21.iloc[-1]) * bias > 0
    l3, h3 = d["low"].iloc[-3:], d["high"].iloc[-3:]
    vw3 = vwap.reindex(d.index[-3:]).ffill().fillna(vw)
    toca = ((l3 <= e21.iloc[-3:]) | (l3 <= vw3)).any() if bias == 1 else ((h3 >= e21.iloc[-3:]) | (h3 >= vw3)).any()
    v = d["volume"]
    vol_ok = True if v.tail(20).sum() == 0 else float(v.iloc[-1]) > float(v.tail(20).mean())
    if not (giro and lado and toca and vol_ok):
        diag["Estado"] = "Esperando retroceso + giro de EMA 9"
        return diag, None
    buf = 0.1 * float(atr(d).iloc[-1])
    sl = float(d["low"].tail(5).min()) - buf if bias == 1 else float(d["high"].tail(5).max()) + buf
    r = abs(px - sl)
    if (px - sl) * bias <= 0:
        diag["Estado"] = "SL invalido"
        return diag, None
    riesgo_usd = p["banco"] * p["riesgo"] / 100
    tam, tam_txt = tamano(nombre, tipo, riesgo_usd, r, px)
    diag["Dir"], diag["Estado"] = ("LONG" if bias == 1 else "SHORT"), "🚨 SEÑAL"
    return diag, {"estrategia": "EMA 9/21 + VWAP", "activo": nombre, "tipo": tipo, "dir": diag["Dir"], "bias": bias,
                  "entrada": px, "sl": sl, "tp1": px + bias * 2 * r, "tp2": None, "tp_r": 2, "r1": 2,
                  "riesgo_usd": riesgo_usd, "tamano": tam, "tamano_txt": tam_txt, "score": 1, "score_max": 1,
                  "confluencias": f"{'sobre' if bias == 1 else 'bajo'} VWAP {fmt(vw)} · retroceso a EMA21/VWAP · giro EMA9",
                  "kz": killzone_actual(ahora) or "-", "vela": d.index[-1],
                  "id": f"EMAVWAP|{nombre}|{diag['Dir']}|{d.index[-1]}"}


# ================================================================= ESTRATEGIAS 16-19: HYENUK CHU ("Hazlo simple")
def _apertura_ny(df, ahora):
    """(cierre de la ultima vela en NY, apertura 9:30 NY de ese dia, minutos de la vela)."""
    tf_min = max(int(df.index.to_series().diff().tail(30).min().total_seconds() // 60), 1)
    cierre = (df.index[-1] + pd.Timedelta(minutes=tf_min)).tz_convert(NY)
    return cierre, cierre.normalize() + pd.Timedelta(hours=9, minutes=30), tf_min


def evaluar_orb15(nombre, tipo, df, d1, p, ahora, dd=None):
    """
    ORB de 15 minutos (Hyenuk Chu): rango de los primeros 15 min tras la apertura de NY (9:30-9:45).
    Entrada en la PRIMERA vela que cierra fuera del rango (con volumen superior a su media, si hay volumen),
    stop en el lado opuesto del rango, objetivo 2R. Ventana: hasta las 12:00 NY.
    """
    diag = {"Estrategia": "ORB15", "Activo": nombre, "Estado": "", "Sesgo 1H": "-", "Dir": "-",
            "Score": 0, "Confluencias": ""}
    if not mercado_abierto(tipo, ahora) or df is None or len(df) < 30:
        diag["Estado"] = "Mercado cerrado" if df is not None else "Sin datos"
        return diag, None
    cierre, t0, tf_min = _apertura_ny(df, ahora)
    t_r, t_fin = t0 + pd.Timedelta(minutes=15), t0 + pd.Timedelta(hours=2.5)
    if cierre.weekday() >= 5 or not (t_r < cierre <= t_fin) or tf_min > 15:
        diag["Estado"] = "Fuera de la ventana 9:45-12:00 NY"
        return diag, None
    rango = df[(df.index >= t0.tz_convert("UTC")) & (df.index < t_r.tz_convert("UTC"))]
    if len(rango) < 15 // tf_min:
        diag["Estado"] = "Sin rango de 15 min"
        return diag, None
    rh, rl = float(rango["high"].max()), float(rango["low"].min())
    post = df[df.index >= t_r.tz_convert("UTC")]
    c = post["close"].to_numpy()
    fuera = (c > rh) | (c < rl)
    if not fuera.any() or fuera.argmax() != len(c) - 1:
        diag["Estado"] = "Esperando ruptura" if not fuera.any() else "Ruptura de hoy ya avisada"
        return diag, None
    v = df["volume"]
    if v.tail(20).sum() > 0 and not float(v.iloc[-1]) > float(v.tail(20).mean()):
        diag["Estado"] = "Ruptura sin volumen: descartada"
        return diag, None
    px = float(c[-1])
    bias = 1 if px > rh else -1
    sl = rl if bias == 1 else rh
    r = abs(px - sl)
    riesgo_usd = p["banco"] * p["riesgo"] / 100
    tam, tam_txt = tamano(nombre, tipo, riesgo_usd, r, px)
    diag["Dir"], diag["Estado"] = ("LONG" if bias == 1 else "SHORT"), "🚨 SEÑAL"
    return diag, {"estrategia": "ORB 15 min (Hyenuk Chu)", "activo": nombre, "tipo": tipo, "dir": diag["Dir"],
                  "bias": bias, "entrada": px, "sl": sl, "tp1": px + bias * 2 * r, "tp2": None, "tp_r": 2, "r1": 2,
                  "max_barras": int(6.5 * 60 / tf_min), "riesgo_usd": riesgo_usd, "tamano": tam,
                  "tamano_txt": tam_txt, "score": 1, "score_max": 1,
                  "confluencias": f"rompió el rango 9:30-9:45 ({fmt(rl)} - {fmt(rh)})",
                  "kz": killzone_actual(ahora) or "-", "vela": df.index[-1], "id": f"ORB15|{nombre}|{t0:%Y-%m-%d}"}


def evaluar_max_hist(nombre, tipo, df, d1, p, ahora, dd=None):
    """
    Rompimiento de maximos (Hyenuk Chu): activo en tendencia alcista diaria (cierre > SMA20 diaria y SMA20
    subiendo) que rompe el MAXIMO de todo el historial diario disponible (hasta 1 ano). Gatillo: la primera
    vela intradia que cierra por encima. Solo compras. Stop 0,5 ATR diario, objetivo 2R, hasta 3 dias.
    """
    diag = {"Estrategia": "MaxHist", "Activo": nombre, "Estado": "", "Sesgo 1H": "-", "Dir": "-",
            "Score": 0, "Confluencias": ""}
    if not mercado_abierto(tipo, ahora) or df is None or dd is None or len(dd) < 60 or len(df) < 3:
        diag["Estado"] = "Mercado cerrado" if df is not None else "Sin datos diarios"
        return diag, None
    dd = dd.tail(250)
    techo = float(dd["high"].max())
    sma = dd["close"].rolling(20).mean()
    if not (dd["close"].iloc[-1] > sma.iloc[-1] > sma.iloc[-6]):
        diag["Estado"] = "Sin tendencia alcista diaria"
        return diag, None
    c_ant, c_act = float(df["close"].iloc[-2]), float(df["close"].iloc[-1])
    if not (c_act > techo >= c_ant):
        diag["Estado"] = f"Bajo el máximo de {len(dd)} días ({fmt(techo)})"
        return diag, None
    tf_min = max(int(df.index.to_series().diff().tail(30).min().total_seconds() // 60), 1)
    r = 0.5 * float(atr(dd).iloc[-1])
    sl = c_act - r
    riesgo_usd = p["banco"] * p["riesgo"] / 100
    tam, tam_txt = tamano(nombre, tipo, riesgo_usd, r, c_act)
    diag["Dir"], diag["Estado"] = "LONG", "🚨 SEÑAL"
    return diag, {"estrategia": "Máximos históricos (Hyenuk Chu)", "activo": nombre, "tipo": tipo, "dir": "LONG",
                  "bias": 1, "entrada": c_act, "sl": sl, "tp1": c_act + 2 * r, "tp2": None, "tp_r": 2, "r1": 2,
                  "max_barras": int(3 * 24 * 60 / tf_min), "riesgo_usd": riesgo_usd, "tamano": tam,
                  "tamano_txt": tam_txt, "score": 1, "score_max": 1,
                  "confluencias": f"rompió el máximo de {len(dd)} días ({fmt(techo)}) en tendencia alcista",
                  "kz": killzone_actual(ahora) or "-", "vela": df.index[-1],
                  "id": f"MAXH|{nombre}|{dd.index[-1]:%Y-%m-%d}"}


def evaluar_medias(nombre, tipo, df, d1, p, ahora, dd=None):
    """
    Rebote en medias moviles (Hyenuk Chu): tendencia por la SMA 200; retroceso a la EMA 8 (en 5m) o a la
    SMA 20 (en 15m) y vela de confirmacion (martillo / estrella fugaz o envolvente) a favor de la tendencia.
    Stop tras la vela de confirmacion, objetivo 2R.
    """
    diag = {"Estrategia": "Medias", "Activo": nombre, "Estado": "", "Sesgo 1H": "-", "Dir": "-",
            "Score": 0, "Confluencias": ""}
    if not mercado_abierto(tipo, ahora) or df is None or len(df) < 210:
        diag["Estado"] = "Mercado cerrado" if df is not None else "Sin datos"
        return diag, None
    d = df.tail(VENTANA)
    tf_min = max(int(d.index.to_series().diff().tail(30).min().total_seconds() // 60), 1)
    c = d["close"]
    ma = c.ewm(span=8, adjust=False).mean() if tf_min <= 5 else c.rolling(20).mean()
    nombre_ma = "EMA 8" if tf_min <= 5 else "SMA 20"
    s200 = c.rolling(200).mean()
    o1, h1, l1, c1 = (float(d[x].iloc[-1]) for x in ("open", "high", "low", "close"))
    o0, c0 = float(d["open"].iloc[-2]), float(d["close"].iloc[-2])
    m, s = float(ma.iloc[-1]), float(s200.iloc[-1])
    cuerpo, rango = abs(c1 - o1), h1 - l1
    if rango <= 0:
        diag["Estado"] = "Vela sin rango"
        return diag, None
    if c1 > s and m > s:
        bias = 1
        toca = l1 <= m <= h1
        martillo = (min(o1, c1) - l1) >= 2 * cuerpo and c1 >= l1 + 0.66 * rango
        envolvente = c1 > o1 and c0 < o0 and c1 >= o0 and o1 <= c0
    elif c1 < s and m < s:
        bias = -1
        toca = l1 <= m <= h1
        martillo = (h1 - max(o1, c1)) >= 2 * cuerpo and c1 <= h1 - 0.66 * rango
        envolvente = c1 < o1 and c0 > o0 and c1 <= o0 and o1 >= c0
    else:
        diag["Estado"] = "Sin tendencia clara vs SMA 200"
        return diag, None
    diag["Dir"] = "LONG" if bias == 1 else "SHORT"
    if not (toca and (martillo or envolvente)):
        diag["Estado"] = f"Esperando retroceso a la {nombre_ma} + vela de confirmación"
        return diag, None
    buf = 0.1 * float(atr(d).iloc[-1])
    sl = l1 - buf if bias == 1 else h1 + buf
    r = abs(c1 - sl)
    riesgo_usd = p["banco"] * p["riesgo"] / 100
    tam, tam_txt = tamano(nombre, tipo, riesgo_usd, r, c1)
    diag["Estado"] = "🚨 SEÑAL"
    return diag, {"estrategia": "Rebote en medias (Hyenuk Chu)", "activo": nombre, "tipo": tipo, "dir": diag["Dir"],
                  "bias": bias, "entrada": c1, "sl": sl, "tp1": c1 + bias * 2 * r, "tp2": None, "tp_r": 2, "r1": 2,
                  "riesgo_usd": riesgo_usd, "tamano": tam, "tamano_txt": tam_txt, "score": 1, "score_max": 1,
                  "confluencias": f"rebote en {nombre_ma} · {'martillo' if martillo else 'envolvente'} · "
                                  f"{'sobre' if bias == 1 else 'bajo'} SMA 200",
                  "kz": killzone_actual(ahora) or "-", "vela": d.index[-1],
                  "id": f"MEDIAS|{nombre}|{diag['Dir']}|{d.index[-1]}"}


def evaluar_gap(nombre, tipo, df, d1, p, ahora, dd=None):
    """
    Operativa de gaps (Hyenuk Chu) en la apertura de NY: gap = apertura 9:30 vs cierre de las 16:00 del dia
    anterior (minimo 0,3%). Se observan los primeros 15 minutos:
      - si van EN CONTRA del gap -> se apuesta al cierre del gap (objetivo: el cierre anterior, minimo 1R);
      - si van A FAVOR -> continuacion, objetivo 2R.
    Stop en el extremo opuesto de esos 15 minutos.
    """
    diag = {"Estrategia": "Gap", "Activo": nombre, "Estado": "", "Sesgo 1H": "-", "Dir": "-",
            "Score": 0, "Confluencias": ""}
    if not mercado_abierto(tipo, ahora) or df is None or len(df) < 30:
        diag["Estado"] = "Mercado cerrado" if df is not None else "Sin datos"
        return diag, None
    cierre, t0, tf_min = _apertura_ny(df, ahora)
    t_r = t0 + pd.Timedelta(minutes=15)
    if cierre.weekday() >= 5 or cierre != t_r or tf_min > 15:
        diag["Estado"] = "Esperando las 9:45 NY"
        return diag, None
    t16 = t0.normalize() - pd.Timedelta(days=1) + pd.Timedelta(hours=16)
    for _ in range(3):                                   # lunes -> viernes
        if t16.weekday() < 5:
            break
        t16 -= pd.Timedelta(days=1)
    prev = df[df.index < t16.tz_convert("UTC")]
    r15 = df[(df.index >= t0.tz_convert("UTC")) & (df.index < t_r.tz_convert("UTC"))]
    if prev.empty or len(r15) < 15 // tf_min or prev.index[-1] < (t16 - pd.Timedelta(hours=1)).tz_convert("UTC"):
        diag["Estado"] = "Sin cierre del día anterior"
        return diag, None
    c_ant, ap = float(prev["close"].iloc[-1]), float(r15["open"].iloc[0])
    gap = ap / c_ant - 1
    if abs(gap) < 0.003:
        diag["Estado"] = f"Sin gap ({100 * gap:.2f}%)"
        return diag, None
    c15, h15, l15 = float(r15["close"].iloc[-1]), float(r15["high"].max()), float(r15["low"].min())
    a_favor = (c15 - ap) * np.sign(gap) > 0
    if a_favor:
        bias = int(np.sign(gap))
        sl = l15 if bias == 1 else h15
        r = abs(c15 - sl)
        tp, tipo_op = c15 + bias * 2 * r, "continuación"
    else:
        bias = -int(np.sign(gap))
        sl = h15 if bias == -1 else l15
        r = abs(c15 - sl)
        tp, tipo_op = c_ant, "cierre del gap"
    if r <= 0 or (tp - c15) * bias < r:
        diag["Estado"] = f"Gap {100 * gap:+.2f}%: objetivo < 1R"
        return diag, None
    riesgo_usd = p["banco"] * p["riesgo"] / 100
    tam, tam_txt = tamano(nombre, tipo, riesgo_usd, r, c15)
    diag["Dir"], diag["Estado"] = ("LONG" if bias == 1 else "SHORT"), "🚨 SEÑAL"
    return diag, {"estrategia": "Gap de apertura (Hyenuk Chu)", "activo": nombre, "tipo": tipo, "dir": diag["Dir"],
                  "bias": bias, "entrada": c15, "sl": sl, "tp1": tp, "tp2": None, "tp_r": (tp - c15) * bias / r,
                  "r1": round((tp - c15) * bias / r, 1), "max_barras": int(6 * 60 / tf_min),
                  "riesgo_usd": riesgo_usd, "tamano": tam, "tamano_txt": tam_txt, "score": 1, "score_max": 1,
                  "confluencias": f"gap {100 * gap:+.2f}% · {tipo_op}", "kz": killzone_actual(ahora) or "-",
                  "vela": df.index[-1], "id": f"GAP|{nombre}|{t0:%Y-%m-%d}"}


# ================================================================= ESTRATEGIAS 20-22: DR. INVERSOR
def _tf_min(df):
    return max(int(df.index.to_series().diff().tail(30).min().total_seconds() // 60), 1)


def _bloqueado(d1, px, tp, bias):
    """True si hay una resistencia (LONG) / soporte (SHORT) de las ultimas 24 velas 1H entre la entrada y el TP."""
    if d1 is None or len(d1) < 24:
        return False
    w = d1.tail(24)
    if bias == 1:
        nivel = w["high"][w["high"] > px]
        return bool(len(nivel)) and float(nivel.min()) < tp
    nivel = w["low"][w["low"] < px]
    return bool(len(nivel)) and float(nivel.max()) > tp


def _senal_di(nombre, tipo, p, ahora, d, clave, titulo, bias, px, sl, conf, ident):
    r = abs(px - sl)
    riesgo_usd = p["banco"] * p["riesgo"] / 100
    tam, tam_txt = tamano(nombre, tipo, riesgo_usd, r, px)
    return {"estrategia": titulo, "activo": nombre, "tipo": tipo, "dir": "LONG" if bias == 1 else "SHORT",
            "bias": bias, "entrada": px, "sl": sl, "tp1": px + bias * 2 * r, "tp2": None, "tp_r": 2, "r1": 2,
            "riesgo_usd": riesgo_usd, "tamano": tam, "tamano_txt": tam_txt, "score": 1, "score_max": 1,
            "confluencias": conf, "kz": killzone_actual(ahora) or "-", "vela": d.index[-1],
            "id": f"{clave}|{nombre}|{ident}"}


def evaluar_di_ema(nombre, tipo, df, d1, p, ahora, dd=None):
    """
    Tendencia con EMA 20 / EMA 200 (Dr. Inversor):
      Sesgo   : precio sobre la EMA 200 de 15m -> solo compras (debajo -> solo ventas). La EMA 200 de 15m
                se aproxima con la EMA 50 de 1H (mismo horizonte: 50 horas).
      Gatillo : retroceso que toca la EMA 20 de la temporalidad de entrada y vela de confirmacion con
                cuerpo fuerte (>= 60% del rango) a favor, cerrando sobre/bajo el maximo/minimo anterior.
      Gestion : stop tras el minimo/maximo del retroceso (3 velas); TP 2R; no entra si una resistencia /
                soporte de las ultimas 24 h bloquea el objetivo.
    """
    diag = {"Estrategia": "DI_EMA", "Activo": nombre, "Estado": "", "Sesgo 1H": "-", "Dir": "-",
            "Score": 0, "Confluencias": ""}
    if not mercado_abierto(tipo, ahora) or df is None or d1 is None or len(df) < 40 or len(d1) < 60:
        diag["Estado"] = "Mercado cerrado" if df is not None else "Sin datos"
        return diag, None
    d = df.tail(VENTANA)
    e200 = float(d1["close"].ewm(span=50, adjust=False).mean().iloc[-1])
    px = float(d["close"].iloc[-1])
    bias = 1 if px > e200 else -1
    diag["Sesgo 1H"] = "Alcista" if bias == 1 else "Bajista"
    e20 = d["close"].ewm(span=20, adjust=False).mean()
    o1, h1, l1 = (float(d[x].iloc[-1]) for x in ("open", "high", "low"))
    h0, l0 = float(d["high"].iloc[-2]), float(d["low"].iloc[-2])
    toca = ((d["low"].iloc[-3:] <= e20.iloc[-3:]) if bias == 1 else (d["high"].iloc[-3:] >= e20.iloc[-3:])).any()
    fuerte = (h1 > l1) and abs(px - o1) >= 0.6 * (h1 - l1) and (px - o1) * bias > 0
    rompe = px > h0 if bias == 1 else px < l0
    lado = (px - float(e20.iloc[-1])) * bias > 0
    if not (toca and fuerte and rompe and lado):
        diag["Estado"] = "Esperando retroceso a la EMA 20 + vela fuerte"
        return diag, None
    buf = 0.1 * float(atr(d).iloc[-1])
    sl = float(d["low"].tail(3).min()) - buf if bias == 1 else float(d["high"].tail(3).max()) + buf
    if (px - sl) * bias <= 0:
        diag["Estado"] = "SL invalido"
        return diag, None
    if _bloqueado(d1, px, px + bias * 2 * abs(px - sl), bias):
        diag["Estado"] = "Objetivo bloqueado por resistencia/soporte cercano"
        return diag, None
    diag["Dir"], diag["Estado"] = ("LONG" if bias == 1 else "SHORT"), "🚨 SEÑAL"
    return diag, _senal_di(nombre, tipo, p, ahora, d, "DIEMA", "EMA 20/200 (Dr. Inversor)", bias, px, sl,
                           f"{'sobre' if bias == 1 else 'bajo'} EMA 200 (15m) · rebote en EMA 20 · vela fuerte",
                           f"{diag['Dir']}|{d.index[-1]}")


def evaluar_di_ruptura(nombre, tipo, df, d1, p, ahora, dd=None):
    """
    Soportes/resistencias + ruptura con volumen + pullback (Dr. Inversor):
      Niveles : maximo y minimo de la sesion anterior (dia previo, velas 1H).
      Ruptura : una vela cierra fuera del nivel con volumen > 1,5x su media de 20 (si el mercado tiene volumen).
      Entrada : en el retroceso que vuelve a tocar el nivel roto y cierra a favor.
      Gestion : stop tras el minimo/maximo del pullback; TP 2R; descarta si el objetivo esta bloqueado.
    """
    diag = {"Estrategia": "DI_Ruptura", "Activo": nombre, "Estado": "", "Sesgo 1H": "-", "Dir": "-",
            "Score": 0, "Confluencias": ""}
    if not mercado_abierto(tipo, ahora) or df is None or d1 is None or len(df) < 40 or len(d1) < 30:
        diag["Estado"] = "Mercado cerrado" if df is not None else "Sin datos"
        return diag, None
    niv = niveles_liquidez(d1, ahora)
    if not niv:
        diag["Estado"] = "Sin sesión anterior"
        return diag, None
    d = df.tail(VENTANA)
    hoy = _ts_utc(ahora).tz_convert(NY).normalize().tz_convert("UTC")
    s = d[d.index >= hoy]
    if len(s) < 3:
        diag["Estado"] = "Inicio de sesión"
        return diag, None
    c, o, h, l, v = (s[x].to_numpy() for x in ("close", "open", "high", "low", "volume"))
    vm = d["volume"].rolling(20).mean().reindex(s.index).to_numpy()
    con_vol = d["volume"].tail(40).sum() > 0
    for bias, nivel, nombre_n in ((1, niv["PDH"], "máximo"), (-1, niv["PDL"], "mínimo")):
        fuera = (c > nivel) if bias == 1 else (c < nivel)
        if con_vol:
            fuera = fuera & (v > 1.5 * np.nan_to_num(vm, nan=np.inf))
        idx = np.where(fuera[:-1])[0]
        if len(idx) == 0:
            continue
        b0 = int(idx[0])
        tras = slice(b0 + 1, len(c))
        dentro = (c[tras] < nivel) if bias == 1 else (c[tras] > nivel)
        toca = (l[tras] <= nivel) if bias == 1 else (h[tras] >= nivel)
        ok = toca & ~dentro & ((c[tras] > o[tras]) if bias == 1 else (c[tras] < o[tras]))
        if dentro.any() and (not ok.any() or dentro.argmax() < ok.argmax()):
            continue
        if not ok.any() or ok.argmax() != len(ok) - 1:
            continue
        px = float(c[-1])
        buf = 0.1 * float(atr(d).iloc[-1])
        sl = float(l[b0 + 1:].min()) - buf if bias == 1 else float(h[b0 + 1:].max()) + buf
        if (px - sl) * bias <= 0 or _bloqueado(d1, px, px + bias * 2 * abs(px - sl), bias):
            continue
        diag["Dir"], diag["Estado"] = ("LONG" if bias == 1 else "SHORT"), "🚨 SEÑAL"
        return diag, _senal_di(nombre, tipo, p, ahora, d, "DIRUP", "Ruptura + pullback (Dr. Inversor)", bias, px,
                               sl, f"rompió el {nombre_n} de ayer ({fmt(nivel)})" + (" con volumen" if con_vol else "")
                               + " y re-testeó", f"{diag['Dir']}|{hoy:%Y-%m-%d}")
    diag["Estado"] = "Esperando ruptura con volumen + pullback"
    return diag, None


def _rsi(c, n=14):
    dlt = c.diff()
    up = dlt.clip(lower=0).ewm(alpha=1 / n, adjust=False).mean()
    dn = (-dlt.clip(upper=0)).ewm(alpha=1 / n, adjust=False).mean()
    return 100 - 100 / (1 + up / dn.replace(0, np.nan))


def evaluar_di_rsi(nombre, tipo, df, d1, p, ahora, dd=None):
    """
    Divergencia RSI(14) en 15m + quiebre de estructura (Dr. Inversor):
      Bajista: en 15m el precio hace un maximo mas alto pero el RSI un maximo mas bajo (ultimos 2 swings);
               gatillo: la vela de entrada cierra bajo el minimo de las 5 velas anteriores. Alcista en espejo.
      Stop tras el extremo del swing; TP 2R; descarta si el objetivo esta bloqueado.
    """
    diag = {"Estrategia": "DI_RSI", "Activo": nombre, "Estado": "", "Sesgo 1H": "-", "Dir": "-",
            "Score": 0, "Confluencias": ""}
    if not mercado_abierto(tipo, ahora) or df is None or len(df) < 120:
        diag["Estado"] = "Mercado cerrado" if df is not None else "Sin datos"
        return diag, None
    d = df.tail(VENTANA)
    m15 = d if _tf_min(d) >= 15 else remuestrear(d, "15m")
    if len(m15) < 40:
        diag["Estado"] = "Sin datos de 15m"
        return diag, None
    rsi = _rsi(m15["close"]).to_numpy()
    sh, sl_ = swings(m15, 3)
    hh, ll = m15["high"].to_numpy(), m15["low"].to_numpy()
    n15 = len(m15)
    c = d["close"].to_numpy()
    px = float(c[-1])
    previo_lo, previo_hi = float(d["low"].iloc[-6:-1].min()), float(d["high"].iloc[-6:-1].max())
    buf = 0.1 * float(atr(d).iloc[-1])
    cand = []
    if len(sh) >= 2 and sh[-1] >= n15 - 12:
        a, b = sh[-2], sh[-1]
        if hh[b] > hh[a] and rsi[b] < rsi[a] and c[-1] < previo_lo and c[-2] >= float(d["low"].iloc[-7:-2].min()):
            cand.append((-1, float(hh[b]) + buf, m15.index[b]))
    if len(sl_) >= 2 and sl_[-1] >= n15 - 12:
        a, b = sl_[-2], sl_[-1]
        if ll[b] < ll[a] and rsi[b] > rsi[a] and c[-1] > previo_hi and c[-2] <= float(d["high"].iloc[-7:-2].max()):
            cand.append((1, float(ll[b]) - buf, m15.index[b]))
    for bias, sl, t_sw in cand:
        if (px - sl) * bias <= 0 or _bloqueado(d1, px, px + bias * 2 * abs(px - sl), bias):
            continue
        diag["Dir"], diag["Estado"] = ("LONG" if bias == 1 else "SHORT"), "🚨 SEÑAL"
        return diag, _senal_di(nombre, tipo, p, ahora, d, "DIRSI", "Divergencia RSI (Dr. Inversor)", bias, px, sl,
                               f"divergencia {'alcista' if bias == 1 else 'bajista'} RSI 15m · quiebre de estructura",
                               f"{diag['Dir']}|{t_sw}")
    diag["Estado"] = "Sin divergencia RSI + quiebre"
    return diag, None


# ================================================================= ESTRATEGIA 23: JUDAS SWING + FVG (modelo ICT)
# Ventanas en hora de Colombia (UTC-5 fijo): Londres 2:00-4:30 COT = 07:00-09:30 UTC,
# solapamiento Londres-NY 7:00-10:30 COT = 12:00-15:30 UTC.
VENTANAS_JUDAS = (("Londres", 7.0, 9.5), ("Nueva York", 12.0, 15.5))


def _redondos(px, nombre, tipo):
    """Numeros redondos institucionales cercanos (cada 50 pips; JPY cada 0,50). Solo forex."""
    if tipo != "forex":
        return []
    paso = 0.50 if "JPY" in nombre else 0.0050
    base = np.floor(px / paso) * paso
    return [float(base - paso), float(base), float(base + paso), float(base + 2 * paso)]


def _judas_long(d, ini, lows, ref=5):
    """LONG: barrido de un nivel inferior (mecha bajo el nivel y cierre de vuelta arriba), luego quiebre de
    estructura con CUERPO por encima del ultimo maximo que impulso el barrido y un FVG alcista en el
    desplazamiento. Dispara solo en la vela en que se completan ambas confirmaciones."""
    o, h, l, c = (d[x].to_numpy() for x in ("open", "high", "low", "close"))
    n = len(c)
    if n - ini < 4 or not lows:
        return None
    j = ini + int(np.argmin(l[ini:n - 1]))                     # extremo del barrido en la sesion
    if j < ref:
        return None
    barridos = [lv for lv in lows if l[j] < lv and max(c[j], c[min(j + 1, n - 1)]) > lv]
    if not barridos:
        return None
    mss = float(h[j - ref:j].max())

    def cond(t):
        if t <= j + 1:
            return None
        rompe = [m for m in range(j + 1, t + 1) if min(o[m], c[m]) > mss or c[m] > mss]
        if not rompe:
            return None
        fvgs = [q for q in range(j + 2, t + 1) if l[q] > h[q - 2]]
        return (rompe[0], fvgs[-1]) if fvgs else None

    ok = cond(n - 1)
    if ok is None or cond(n - 2) is not None:
        return None
    q = ok[1]
    entrada = (l[q] + h[q - 2]) / 2                            # 50% del FVG
    if not c[-1] > entrada:
        return None
    return {"extremo": float(l[j]), "entrada": float(entrada), "fvg": (float(h[q - 2]), float(l[q])),
            "nivel": float(max(barridos))}


def evaluar_judas(nombre, tipo, df, d1, p, ahora, dd=None):
    """
    Judas Swing / Barrido de liquidez de sesion + FVG (modelo ICT):
      Mapa     : maximo/minimo de Asia (00-07 UTC), maximo/minimo del dia anterior y numeros redondos.
      Ventanas : apertura de Londres (2:00-4:30 COT) y solapamiento Londres-NY (7:00-10:30 COT).
      Trampa   : el precio rompe un extremo y lo rechaza (cierra de vuelta).
      Gatillo  : quiebre de estructura con cuerpo + Fair Value Gap en el desplazamiento.
      Entrada  : orden LIMITE en el 50% del FVG. Stop tras el extremo del barrido.
      Objetivo : extremo opuesto del rango asiatico si da 2R o mas (max 4R); si no, 2R.
      Maximo 1 operacion por sesion (2 por dia).
    """
    diag = {"Estrategia": "Judas", "Activo": nombre, "Estado": "", "Sesgo 1H": "-", "Dir": "-",
            "Score": 0, "Confluencias": ""}
    if not mercado_abierto(tipo, ahora) or df is None or d1 is None or len(df) < 40 or len(d1) < 30:
        diag["Estado"] = "Mercado cerrado" if df is not None else "Sin datos"
        return diag, None
    tf_min = max(int(df.index.to_series().diff().tail(30).min().total_seconds() // 60), 1)
    cierre = df.index[-1] + pd.Timedelta(minutes=tf_min)
    hoy = cierre.normalize()
    hora = (cierre - hoy).total_seconds() / 3600
    ses = next(((nm, a, b) for nm, a, b in VENTANAS_JUDAS if a < hora <= b), None)
    if cierre.weekday() >= 5 or ses is None:
        diag["Estado"] = "Fuera de Londres (2:00-4:30 COT) y NY (7:00-10:30 COT)"
        return diag, None
    t_s, t_fin = hoy + pd.Timedelta(hours=ses[1]), hoy + pd.Timedelta(hours=ses[2])
    asia = df[(df.index >= hoy) & (df.index < hoy + pd.Timedelta(hours=7))]
    niv = niveles_liquidez(d1, ahora) or {}
    if len(asia) >= 6:
        niv["Asia H"], niv["Asia L"] = float(asia["high"].max()), float(asia["low"].min())
    if "Asia H" not in niv:
        diag["Estado"] = "Sin rango asiático"
        return diag, None
    d = df[df.index >= t_s - pd.Timedelta(hours=2)]
    ini = int(d.index.searchsorted(t_s))
    px0 = float(d["open"].iloc[min(ini, len(d) - 1)])
    red = _redondos(px0, nombre, tipo)
    lows = [v for k, v in niv.items() if k in ("PDL", "Asia L")] + [x for x in red if x < px0]
    highs = [v for k, v in niv.items() if k in ("PDH", "Asia H")] + [x for x in red if x > px0]
    s, bias = _judas_long(d, ini, lows), 1
    if s is None:
        s = _judas_long(_flip(d), ini, [-x for x in highs])
        if s is not None:
            s = {"extremo": -s["extremo"], "entrada": -s["entrada"], "fvg": (-s["fvg"][1], -s["fvg"][0]),
                 "nivel": -s["nivel"]}
            bias = -1
    diag["_niveles"] = {k: v for k, v in niv.items() if k in ("PDH", "PDL", "Asia H", "Asia L")}
    if s is None:
        diag["Estado"] = f"{ses[0]}: esperando barrido + quiebre + FVG"
        return diag, None
    pip = _pip(nombre) if tipo == "forex" else 0.1 * float(atr(df.tail(100)).iloc[-1])
    entrada, sl = s["entrada"], s["extremo"] - bias * pip
    r = abs(entrada - sl)
    if (entrada - sl) * bias <= 0:
        diag["Estado"] = "SL invalido"
        return diag, None
    opuesto = niv["Asia H"] if bias == 1 else niv["Asia L"]
    dist = (opuesto - entrada) * bias
    r_obj = min(dist / r, 4.0) if dist >= 2 * r else 2.0
    tp = entrada + bias * r_obj * r
    riesgo_usd = p["banco"] * p["riesgo"] / 100
    tam, tam_txt = tamano(nombre, tipo, riesgo_usd, r, entrada)
    espera = max(int(((t_fin - cierre).total_seconds() / 60 + 60) // tf_min), 2)
    diag["Dir"], diag["Estado"] = ("LONG" if bias == 1 else "SHORT"), "🚨 SEÑAL"
    lado = "COMPRA" if bias == 1 else "VENTA"
    pips = f" ({r / _pip(nombre):.1f} pips)" if tipo == "forex" else ""
    return diag, {"estrategia": "Judas Swing + FVG (ICT)", "activo": nombre, "tipo": tipo, "dir": diag["Dir"],
                  "bias": bias, "entrada": entrada, "sl": sl, "tp1": tp, "tp2": None, "orden": "limite",
                  "espera": espera, "max_barras": int(8 * 60 / tf_min),
                  "salida_txt": f"Orden: {lado} LIMIT en {fmt(entrada)} (50% del FVG {fmt(s['fvg'][0])}-"
                                f"{fmt(s['fvg'][1])}). Cancélala si no se llena antes de "
                                f"{(t_fin + pd.Timedelta(hours=1)).tz_convert(NY):%H:%M} NY.\n"
                                f"Stop{pips}: tras el barrido. TP ({r_obj:.1f}R): {fmt(tp)}",
                  "riesgo_usd": riesgo_usd, "tamano": tam, "tamano_txt": tam_txt, "score": 1, "score_max": 1,
                  "confluencias": f"{ses[0]}: barrió {fmt(s['nivel'])} · quiebre de estructura · FVG",
                  "kz": ses[0], "vela": df.index[-1], "id": f"JUDAS|{nombre}|{hoy:%Y-%m-%d}|{ses[0]}"}


def simular_limite(o, h, l, c, k0, d, entrada, sl, tp, espera, max_barras):
    """Orden limite pendiente 'espera' velas: se llena si el precio vuelve a la entrada (si abre mas alla,
    a la apertura). Se cancela si antes toca el TP. Luego SL/TP (SL primero si ambos en la misma vela)."""
    n = len(c)
    k = None
    for b in range(k0, min(k0 + espera, n)):
        if (d == 1 and l[b] <= entrada) or (d == -1 and h[b] >= entrada):
            k = b
            break
        if (d == 1 and h[b] >= tp) or (d == -1 and l[b] <= tp):
            return None
    if k is None:
        return None
    e = min(o[k], entrada) if d == 1 else max(o[k], entrada)
    riesgo = (e - sl) * d
    if riesgo <= 0:
        return None
    for b in range(k, min(k + max_barras, n)):
        if (d == 1 and l[b] <= sl) or (d == -1 and h[b] >= sl):
            return (sl - e) * d / riesgo, b, "SL", e
        if (d == 1 and h[b] >= tp) or (d == -1 and l[b] <= tp):
            return (tp - e) * d / riesgo, b, "TP", e
    b = min(k + max_barras, n) - 1
    return (c[b] - e) * d / riesgo, b, "Tiempo", e


# ================================================================= NOTICIAS (calendario economico)
# Calendario semanal publico de Forex Factory (faireconomy). Se guarda en noticias.json y se renueva cada hora.
CAL_URL = "https://nfs.faireconomy.media/ff_calendar_thisweek.json"
MONEDAS = {"EUR/USD": ["EUR", "USD"], "GBP/USD": ["GBP", "USD"], "USD/JPY": ["USD", "JPY"],
           "USD/CAD": ["USD", "CAD"], "XAU/USD (Oro)": ["USD"], "BTC/USDT": ["USD"],
           "NASDAQ 100 (NQ)": ["USD"], "S&P 500 (ES)": ["USD"], "DOW 30 (YM)": ["USD"], "DAX 40": ["EUR"]}
NOTICIAS_ANTES_MIN, NOTICIAS_DESPUES_MIN = 30, 15      # no avisar 30 min antes ni 15 min despues
_CAL = {"t": 0.0, "eventos": []}


def calendario(ruta="noticias.json"):
    """Eventos de ALTO impacto de la semana: [{'t': Timestamp UTC, 'moneda', 'titulo'}]. Cache 1 h."""
    if time.time() - _CAL["t"] < 3600 and _CAL["eventos"]:
        return _CAL["eventos"]
    datos = None
    try:
        with open(ruta, encoding="utf-8") as f:
            c = json.load(f)
        if time.time() - c.get("t", 0) < 3600:
            datos = c["datos"]
    except Exception:
        pass
    if datos is None:
        try:
            r = requests.get(CAL_URL, timeout=15, headers={"User-Agent": "bot-pro-calendario"})
            r.raise_for_status()
            datos = r.json()
            with open(ruta, "w", encoding="utf-8") as f:
                json.dump({"t": time.time(), "datos": datos}, f)
        except Exception as e:
            print(f"  ! Calendario de noticias no disponible: {e}", flush=True)
            try:
                with open(ruta, encoding="utf-8") as f:
                    datos = json.load(f)["datos"]          # usa la copia vieja antes que nada
            except Exception:
                datos = []
    ev = []
    for x in datos or []:
        if str(x.get("impact", "")).lower() != "high":
            continue
        try:
            ev.append({"t": pd.Timestamp(x["date"]).tz_convert("UTC"), "moneda": x.get("country", ""),
                       "titulo": x.get("title", "")})
        except Exception:
            continue
    _CAL.update(t=time.time(), eventos=ev)
    return ev


def noticia_cercana(nombre, ahora=None):
    """Evento de alto impacto de las monedas del activo entre 30 min antes y 15 min despues de 'ahora'."""
    ahora = _ts_utc(ahora if ahora is not None else datetime.now(UTC))
    mon = MONEDAS.get(nombre, ["USD"])
    for e in calendario():
        if e["moneda"] in mon and -NOTICIAS_DESPUES_MIN <= (e["t"] - ahora).total_seconds() / 60 <= NOTICIAS_ANTES_MIN:
            return e
    return None


def proximas_noticias(horas=24, ahora=None):
    """Texto con las noticias de alto impacto de las proximas 'horas' para las monedas que opera el bot."""
    ahora = _ts_utc(ahora if ahora is not None else datetime.now(UTC))
    mon = {m for v in MONEDAS.values() for m in v}
    ev = sorted((e for e in calendario() if e["moneda"] in mon
                 and timedelta(0) <= e["t"] - ahora <= timedelta(hours=horas)), key=lambda e: e["t"])
    if not ev:
        return f"📰 Sin noticias de alto impacto en las próximas {horas} h."
    return f"📰 Noticias de ALTO impacto ({horas} h):\n" + "\n".join(
        f"• {e['t'].tz_convert(NY):%a %H:%M} NY · {e['moneda']} · {e['titulo']}" for e in ev[:12])


ESTRATEGIAS = {"SMC": evaluar_smc, "Barrido": evaluar_barrido, "Tortugas": evaluar_tortugas,
               "Londres": evaluar_londres, "Setup91": evaluar_setup91, "ORB": evaluar_orb,
               "HolyGrail": evaluar_holygrail, "Gotobi": evaluar_gotobi, "VWAP": evaluar_vwap,
               "Ruido": evaluar_ruido, "HoraBTC": evaluar_hora_btc, "NocheOro": evaluar_noche_oro,
               "ORBRetest": evaluar_orb_retest, "AsiaSweep": evaluar_asia_sweep, "EMAVWAP": evaluar_ema_vwap,
               "ORB15": evaluar_orb15, "MaxHist": evaluar_max_hist, "Medias": evaluar_medias, "Gap": evaluar_gap,
               "DI_EMA": evaluar_di_ema, "DI_Ruptura": evaluar_di_ruptura, "DI_RSI": evaluar_di_rsi,
               "Judas": evaluar_judas}


def estrategias_de(nombre, p):
    propias = ESTRATEGIAS_ACTIVO.get(nombre, list(ESTRATEGIAS))
    return [e for e in p.get("estrategias", list(ESTRATEGIAS)) if e in propias]


def evaluar(nombre, tipo, df, d1, p, ahora, dd=None):
    """Corre las estrategias de ese mercado. Devuelve (diagnosticos por estrategia, lista de senales)."""
    diags, senales = {}, []
    for nm in estrategias_de(nombre, p):
        try:
            d, s = ESTRATEGIAS[nm](nombre, tipo, df, d1, p, ahora, dd)
        except Exception as e:
            d, s = {"Estrategia": nm, "Activo": nombre, "Estado": f"Error: {e}"}, None
        if s:
            s = viabilidad(s, p)
            if not s["viable"] and CUENTA["solo_operables"]:
                d = {**d, "Estado": "Señal descartada: " + s["aviso"]}
                s = None
        diags[nm] = d
        if s:
            senales.append(s)
    return diags, senales


def analizar(nombre, tf, p, ahora=None, cargar=obtener):
    """Analisis en vivo: descarga, quita la vela en formacion y evalua todas las estrategias."""
    ahora = _ts_utc(ahora if ahora is not None else datetime.now(UTC))
    meta = ACTIVOS[nombre]
    if p.get("solo_ganadoras"):
        p = dict(p, estrategias=GANADORAS.get((nombre, tf), []))
        if not p["estrategias"]:
            return {}, []
    if not mercado_abierto(meta["tipo"], ahora):
        return {nm: {"Estrategia": nm, "Activo": nombre, "Estado": "Mercado cerrado"}
                for nm in estrategias_de(nombre, p)}, []
    df = solo_cerradas(cargar(nombre, tf), tf, ahora)
    d1 = solo_cerradas(cargar(nombre, "1h"), "1h", ahora)
    dd = solo_cerradas(cargar(nombre, "1d"), "1d", ahora) if {"Tortugas", "MaxHist"} & set(estrategias_de(nombre, p)) else None
    diags, senales = evaluar(nombre, meta["tipo"], df, d1, p, ahora, dd)
    if senales and CUENTA.get("filtro_noticias"):
        e = noticia_cercana(nombre, ahora)
        if e is not None:
            aviso = f"Bloqueada por noticia: {e['moneda']} {e['titulo']} ({e['t'].tz_convert(NY):%H:%M} NY)"
            for s in senales:
                for d in diags.values():
                    if d.get("Estado") == "🚨 SEÑAL":
                        d["Estado"] = aviso
            print(f"  ! {nombre}: {aviso}", flush=True)
            senales = []
    return diags, senales


def _nota_noticias(nombre, horas=3):
    try:
        ahora = pd.Timestamp.now("UTC")
        ev = [e for e in calendario() if e["moneda"] in MONEDAS.get(nombre, ["USD"])
              and timedelta(0) <= e["t"] - ahora <= timedelta(hours=horas)]
        if ev:
            e = min(ev, key=lambda x: x["t"])
            return f"\n📰 Ojo: {e['moneda']} {e['titulo']} a las {e['t'].tz_convert(NY):%H:%M} NY. Cierra o protege antes."
    except Exception:
        pass
    return ""


def texto_alerta(s, tf):
    ic = "🟢 LONG" if s["dir"] == "LONG" else "🔴 SHORT"
    hora = pd.Timestamp(s["vela"]).tz_convert(NY) + pd.Timedelta(minutes=TF_MIN[tf])
    return (f"{ic} {s['activo']} ({tf}) · {s['estrategia']}\n"
            f"Entrada: {fmt(s['entrada'])}\nSL: {fmt(s['sl'])}\n"
            + (f"{s['salida_txt']}\n" if s.get("salida_txt") else
               f"TP1 ({s.get('r1', 2):g}R): {fmt(s['tp1'])}\n"
               + (f"TP2 (3R): {fmt(s['tp2'])}\n" if s.get("tp2") is not None else ""))
            + f"Riesgo: ${s['riesgo_usd']:.2f} | Tamaño: {s['tamano_txt']}\n"
            f"Score: {s['score']}/{s['score_max']} ({s['confluencias']})\n"
            f"Killzone: {s['kz']} | Vela cerrada: {hora:%a %H:%M} NY"
            + (f"\n{s['aviso']}" if s.get("aviso") else "") + _nota_noticias(s["activo"]))


# ================================================================= ESTADO / ANTI-SPAM
class Estado:
    """Memoria persistente en JSON (sirve para el panel, el bucle 24/7 y GitHub Actions)."""

    def __init__(self, ruta="estado.json"):
        self.ruta = ruta
        self.d = {"enviadas": {}, "ids": [], "log": []}
        try:
            with open(ruta, encoding="utf-8") as f:
                self.d.update(json.load(f))
        except Exception:
            pass

    def guardar(self):
        self.d["ids"] = self.d["ids"][-500:]
        self.d["log"] = self.d["log"][-200:]
        tmp = self.ruta + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(self.d, f, ensure_ascii=False, indent=1)
        os.replace(tmp, self.ruta)

    def procesar(self, s, tf, enviar=True):
        """Una alerta por estrategia+activo+direccion cada COOLDOWN_H horas y nunca dos veces el mismo
        setup. El cooldown solo se activa si Telegram confirmo el envio."""
        k = f"{s['estrategia']}|{s['activo']}|{s['dir']}|{tf}"
        if s["id"] in self.d["ids"] or time.time() - self.d["enviadas"].get(k, 0) < COOLDOWN_H * 3600:
            return False
        ok, msg = enviar_telegram(texto_alerta(s, tf)) if enviar else (False, "Envio desactivado")
        if not ok and enviar:
            print(f"  ! Alerta NO enviada ({s['activo']} {s['estrategia']}): {explicar_error_telegram(msg)}",
                  flush=True)
        if ok:
            self.d["enviadas"][k] = time.time()
            self.d["ids"].append(s["id"])
        self.d["log"].append({"Hora NY": f"{datetime.now(NY):%d/%m %H:%M}", "TF": tf, "Estrategia": s["estrategia"],
                              "Activo": s["activo"], "Dir": s["dir"], "Entrada": fmt(s["entrada"]),
                              "SL": fmt(s["sl"]), "TP1": fmt(s["tp1"]) if s.get("tp1") else "canal",
                              "Score": f"{s['score']}/{s['score_max']}", "Telegram": "OK" if ok else msg})
        self.guardar()
        return ok


# ================================================================= BACKTEST
COSTE_DEFAULT = {"cripto": 0.10, "forex": 0.01, "oro": 0.02, "indice": 0.01}   # % por lado: AJUSTALO


def simular(o, h, l, c, k, d, sl, gestion, max_barras, s=None, canal_lo=None, canal_hi=None):
    """Simula una operacion desde la vela k (entrada a la apertura). Devuelve (R bruto, idx salida, motivo).
    s['salida']=='canal' -> Tortugas: stop que solo se acerca, en el minimo/maximo de 10 dias.
    s['tp_r']            -> objetivo unico a ese multiplo de R (Ruptura de Londres: 1.5R)."""
    s = s or {}
    e = o[k]
    riesgo = (e - sl) * d
    if riesgo <= 0:
        return None
    tp1, tp2 = e + d * 2 * riesgo, e + d * 3 * riesgo
    objetivo = {"tp1": tp1, "tp2": tp2, "parcial": tp1}[gestion]
    if s.get("tp_r"):
        gestion, objetivo = "unico", e + d * s["tp_r"] * riesgo
    canal = s.get("salida") == "canal" and canal_lo is not None
    if canal:
        gestion, objetivo = "canal", (np.inf if d == 1 else -np.inf)
    stop, resto, r_acum = sl, 1.0, 0.0
    fin = min(k + max_barras, len(c))
    for b in range(k, fin):
        if canal:
            nivel = canal_lo[b] if d == 1 else canal_hi[b]
            if np.isfinite(nivel):
                stop = max(stop, nivel) if d == 1 else min(stop, nivel)
        toca_sl = (l[b] <= stop) if d == 1 else (h[b] >= stop)
        toca_tp = (h[b] >= objetivo) if d == 1 else (l[b] <= objetivo)
        if toca_sl:                                     # conservador: SL primero
            motivo = "BE" if stop == e else ("Canal 10d" if canal and stop != sl else "SL")
            return r_acum + resto * (stop - e) * d / riesgo, b, motivo
        if toca_tp:
            if gestion == "parcial" and resto == 1.0:
                r_acum, resto, stop, objetivo = 0.5 * 2, 0.5, e, tp2
                continue
            return r_acum + resto * (objetivo - e) * d / riesgo, b, "TP"
    b = fin - 1
    return r_acum + resto * (c[b] - e) * d / riesgo, b, "Tiempo"


def backtest_activo(nombre, tipo, df, tf, p, ctx_tf="1h", gestion="parcial", coste_pct=None,
                    max_barras=None, progreso=None):
    """Recorre df vela a vela con la MISMA funcion evaluar() del modo en vivo.
    El contexto se construye remuestreando df (solo velas de contexto ya cerradas)."""
    coste_pct = COSTE_DEFAULT[tipo] if coste_pct is None else coste_pct
    max_barras = max_barras or int(24 * 60 / TF_MIN[tf])
    df = df.sort_index()
    df = df[~df.index.duplicated()]
    ctx = remuestrear(df, ctx_tf)
    ctx_cierre = ctx.index + pd.Timedelta(minutes=TF_MIN[ctx_tf])
    dia = remuestrear(df, "1d")
    dia_cierre = dia.index + pd.Timedelta(days=1)
    # canal de salida de las Tortugas: minimo/maximo de los 10 dias COMPLETOS previos a cada vela
    nd_vela = dia_cierre.searchsorted(df.index, side="right")
    lo10 = np.r_[np.nan, dia["low"].rolling(10).min().to_numpy()][nd_vela]
    hi10 = np.r_[np.nan, dia["high"].rolling(10).max().to_numpy()][nd_vela]
    o, h, l, c = (df[x].to_numpy() for x in ("open", "high", "low", "close"))
    ema9 = df["close"].ewm(span=9, adjust=False).mean().to_numpy()
    paso = pd.Timedelta(minutes=TF_MIN[tf])
    ops, ultimo, ids, libre = [], {}, set(), {}
    n = len(df)
    inicio = max(VENTANA, 150)
    for idx in range(inicio, n - 1):
        if progreso and idx % 200 == 0:
            progreso((idx - inicio) / max(n - 1 - inicio, 1))
        ahora = df.index[idx] + paso
        nc = int(ctx_cierre.searchsorted(ahora, side="right"))
        if nc < 60:
            continue
        nd = int(dia_cierre.searchsorted(ahora, side="right"))
        _, senales = evaluar(nombre, tipo, df.iloc[idx - VENTANA + 1: idx + 1],
                             ctx.iloc[max(0, nc - VENTANA_CTX): nc], p, ahora, dia.iloc[max(0, nd - 260): nd])
        for s in senales:
            clave = f"{s['estrategia']}|{s['dir']}"
            if idx < libre.get(s["estrategia"], 0):          # una posicion a la vez por estrategia
                continue
            if s["id"] in ids or (ahora - ultimo.get(clave, pd.Timestamp(0, tz="UTC"))) < \
                    pd.Timedelta(hours=COOLDOWN_H):
                continue
            mb = max_barras * 60 if s.get("salida") == "canal" else max_barras   # Tortugas: hasta 60 dias
            mb = s.get("max_barras") or mb                    # ORB / Gotobi: salida por tiempo propia
            if s.get("orden") == "ruido":                     # Noise Area: stop dinamico cada 30 min
                r = simular_ruido(df, idx + 1, s, paso)
                if r is None:
                    continue
                r_bruto, salida, motivo, e_px = r
            elif s.get("orden") == "limite":                    # Judas: orden limite en el 50% del FVG
                r = simular_limite(o, h, l, c, idx + 1, s["bias"], s["entrada"], s["sl"], s["tp1"],
                                   s.get("espera", 12), mb)
                if r is None:
                    continue
                r_bruto, salida, motivo, e_px = r
            elif s.get("orden") == "stop_tp":                   # Holy Grail: orden stop con TP fijo
                r = simular_stop(o, h, l, c, idx + 1, s["bias"], s["entrada"], s["sl"], s["tp1"], mb)
                if r is None:
                    continue
                r_bruto, salida, motivo, e_px = r
            elif s.get("orden") == "stop":                      # Setup 9.1: orden stop pendiente
                r = simular_91(o, h, l, c, ema9, idx + 1, s["bias"], s["entrada"], s["sl"], mb)
                if r is None:
                    continue
                r_bruto, salida, motivo, e_px = r
            else:
                r = simular(o, h, l, c, idx + 1, s["bias"], s["sl"], gestion, mb, s, lo10, hi10)
                if r is None:
                    continue
                (r_bruto, salida, motivo), e_px = r, o[idx + 1]
            riesgo_px = abs(e_px - s["sl"])
            r_coste = 2 * coste_pct / 100 * e_px / riesgo_px
            ultimo[clave] = ahora
            ids.add(s["id"])
            ops.append({"estrategia": s["estrategia"], "activo": nombre,
                        "senal_NY": ahora.tz_convert(NY).strftime("%Y-%m-%d %H:%M"), "dir": s["dir"],
                        "score": s["score"], "confluencias": s["confluencias"], "killzone": s["kz"],
                        "entrada": e_px, "sl": s["sl"],
                        "salida_NY": (df.index[salida] + paso).tz_convert(NY).strftime("%Y-%m-%d %H:%M"),
                        "motivo": motivo, "R_bruto": round(r_bruto, 3), "coste_R": round(r_coste, 3),
                        "R": round(r_bruto - r_coste, 3), "barras": salida - idx})
            libre[s["estrategia"]] = salida + 1
    if progreso:
        progreso(1.0)
    return pd.DataFrame(ops)


def metricas(ops, riesgo_pct=2.0):
    if ops is None or ops.empty:
        return {"Operaciones": 0}
    R = ops["R"].to_numpy()
    eq_R = np.r_[0, np.cumsum(R)]
    dd_R = float((np.maximum.accumulate(eq_R) - eq_R).max())
    eq = np.r_[1.0, np.cumprod(1 + riesgo_pct / 100 * R)]
    dd_pct = float(((np.maximum.accumulate(eq) - eq) / np.maximum.accumulate(eq)).max() * 100)
    gan, per = R[R > 0].sum(), -R[R < 0].sum()
    racha = max((len(x) for x in "".join("L" if r < 0 else "W" for r in R).split("W")), default=0)
    return {"Operaciones": len(R), "Winrate %": round(100 * (R > 0).mean(), 1),
            "R promedio": round(R.mean(), 3), "R total": round(R.sum(), 2),
            "Profit factor": round(gan / per, 2) if per > 0 else float("inf"),
            "Max DD (R)": round(dd_R, 2), "Max DD %": round(dd_pct, 1),
            "Retorno %": round((eq[-1] - 1) * 100, 1), "Peor racha": racha}


def desglose(ops, col):
    if ops is None or ops.empty:
        return pd.DataFrame()
    g = ops.groupby(col)["R"]
    return pd.DataFrame({"ops": g.size(), "winrate %": g.apply(lambda r: (r > 0).mean() * 100).round(1),
                         "R prom": g.mean().round(3), "R total": g.sum().round(2)})


def correr_backtest(a, p):
    os.makedirs(a.salida, exist_ok=True)
    trabajos = []
    if a.csv:
        d = pd.read_csv(a.csv, index_col=0, parse_dates=True)
        d.columns = [x.lower() for x in d.columns]
        if "volume" not in d:
            d["volume"] = 0.0
        idx = pd.to_datetime(d.index.astype(str), utc=False, format="mixed")
        if getattr(idx, "tz", None) is None:
            try:
                idx = idx.tz_localize(a.csv_tz)
            except TypeError:                      # mezcla de zonas: normalizar a UTC
                idx = pd.to_datetime(d.index.astype(str), utc=True, format="mixed")
        d.index = idx.tz_convert("UTC")
        trabajos.append((a.nombre, a.tipo, d[["open", "high", "low", "close", "volume"]].astype(float)))
    for nm in a.activos or ([] if a.csv else list(ACTIVOS)):
        desde = a.desde or (pd.Timestamp.now("UTC") - pd.Timedelta(days=58)).strftime("%Y-%m-%d")
        print(f"Descargando {nm} {a.tf} desde {desde}...", flush=True)
        d = historico(nm, a.tf, desde, a.hasta)
        if d is None or len(d) < 500:
            print(f"  ! Sin datos suficientes para {nm}")
            continue
        trabajos.append((nm, ACTIVOS[nm]["tipo"], d))
    resumen, todas = [], []
    for nm, tipo, d in trabajos:
        t0 = time.time()
        print(f"\n=== {nm} ({tipo}) {a.tf}/{a.ctx} · {len(d)} velas · "
              f"{d.index[0]:%Y-%m-%d} -> {d.index[-1]:%Y-%m-%d}", flush=True)
        ops = backtest_activo(nm, tipo, d, a.tf, p, a.ctx, a.gestion, a.coste)
        if ops.empty:
            print("Sin operaciones.")
            continue
        todas.append(ops)
        for est, g in ops.groupby("estrategia"):
            m = metricas(g, p["riesgo"])
            resumen.append({"Estrategia": est, "Activo": nm, **m})
            print(f"\n--- {est}\n" + pd.Series(m).to_string())
            print("Por score:\n" + desglose(g, "score").to_string())
            print("Por killzone:\n" + desglose(g, "killzone").to_string())
        print(f"({time.time() - t0:.0f}s)")
    if todas:
        allops = pd.concat(todas, ignore_index=True)
        allops.to_csv(os.path.join(a.salida, "operaciones.csv"), index=False)
        pd.DataFrame(resumen).to_csv(os.path.join(a.salida, "resumen.csv"), index=False)
        print("\n=== RESUMEN ===\n" + pd.DataFrame(resumen).to_string(index=False))
        print(f"\nCSV guardados en {a.salida}/")


# ================================================================= ESCANER 24/7
def escanear(tf, p, estado, verbose=True, cargar=obtener):
    nuevas, filas = 0, []
    for nm in ACTIVOS:
        try:
            diags, senales = analizar(nm, tf, p, cargar=cargar)
        except Exception as e:
            diags, senales = {"-": {"Estrategia": "-", "Activo": nm, "Estado": f"Error: {e}"}}, []
        for d in diags.values():
            d["TF"] = tf
            filas.append(d)
            if verbose:
                print(f"  {nm:<16} {d.get('Estrategia', '-'):<8} {d.get('Dir', '-'):<5} "
                      f"score {d.get('Score', 0)}  {d['Estado']}")
        for s in senales:
            s["tf"] = tf
            if estado is not None and estado.procesar(s, tf):
                nuevas += 1
    return nuevas, filas


def escanear_varios(tfs, p, estado, verbose=True, cargar=obtener):
    """Escanea cada temporalidad (p. ej. 5m y 15m). Cada alerta dice de que temporalidad viene."""
    total, filas = 0, []
    for tf in tfs:
        if verbose:
            print(f"  --- {tf} ---", flush=True)
        n, f = escanear(tf, p, estado, verbose, cargar)
        total += n
        filas += f
    return total, filas


def explicar_error_telegram(msg):
    """Traduce los errores tipicos de la API de Telegram a una causa y su solucion."""
    m = msg.lower()
    if "401" in m or "unauthorized" in m:
        return "TOKEN invalido o revocado: crea uno nuevo con @BotFather y ponlo en TELEGRAM_TOKEN."
    if "chat not found" in m:
        return "Ese chat no existe para el bot: la persona debe abrir el bot en Telegram y pulsar Iniciar (/start)."
    if "blocked" in m or "403" in m:
        return "El usuario bloqueo el bot o no lo ha iniciado: abre el bot y pulsa Iniciar (/start)."
    if "timed out" in m or "connection" in m:
        return "Sin conexion con api.telegram.org desde este servidor."
    return msg


def revisar_telegram():
    """Comprueba el token (getMe) y que cada chat reciba mensajes. Imprime la causa exacta si algo falla."""
    token = secreto("TELEGRAM_TOKEN")
    try:
        r = requests.get(f"https://api.telegram.org/bot{token}/getMe", timeout=15).json()
        if r.get("ok"):
            print(f"Telegram: token OK, bot @{r['result'].get('username')}", flush=True)
        else:
            print(f"Telegram: TOKEN INVALIDO -> {r.get('description')}", flush=True)
            return False
    except Exception as e:
        print(f"Telegram: sin conexion -> {e}", flush=True)
        return False
    return True


def resumen_estado(filas):
    """Texto corto para Telegram: que esta haciendo cada estrategia en cada mercado."""
    lineas = []
    for f in filas:
        lineas.append(f"• {f.get('TF', '')} {f.get('Activo')} · {f.get('Estrategia', '-')}: {f.get('Estado', '-')}")
    return "\n".join(lineas)


def correr_bot(a, p):
    estado = Estado(a.estado)
    ping = getattr(a, "ping", False)
    if ping:
        revisar_telegram()
        ok, msg = enviar_telegram(f"✅ Bot Pro activo · {datetime.now(NY):%a %d/%m %H:%M} NY\n"
                                  f"Mercados: {', '.join(ACTIVOS)}\nTemporalidades: {' y '.join(a.tf)}")
        print(f"Ping a Telegram: {'OK' if ok else explicar_error_telegram(msg)}", flush=True)
    print(f"🏛️ BOT PRO iniciado · estrategias: {', '.join(p['estrategias'])} · {' + '.join(a.tf)} · "
          f"{'una pasada' if a.una_vez else 'bucle 24/7'}", flush=True)
    while True:
        print(f"\n[{datetime.now(NY):%Y-%m-%d %H:%M:%S} NY] Escaneando {len(ACTIVOS)} activos", flush=True)
        try:
            n, filas = escanear_varios(a.tf, p, estado)
            print(f"  -> {n} alertas nuevas enviadas", flush=True)
            if ping:                                   # en la prueba manual: manda tambien el diagnostico
                ok, msg = enviar_telegram(f"📋 Estado del escaner ({' + '.join(a.tf)}) · {n} señales nuevas\n"
                                          + resumen_estado(filas)[:3000] + "\n\n" + proximas_noticias())
                print(f"Resumen a Telegram: {'OK' if ok else explicar_error_telegram(msg)}", flush=True)
                ping = False
        except Exception as e:                     # el bot nunca se cae por un error puntual
            print(f"  ! Error en el escaneo: {e}", flush=True)
        try:                                       # resumen diario de noticias (una vez al dia, desde las 6:00 NY)
            hoy = f"{datetime.now(NY):%Y-%m-%d}"
            if datetime.now(NY).hour >= 6 and estado.d.get("noticias_dia") != hoy:
                ok, _ = enviar_telegram(f"🗓️ Buenos días · {datetime.now(NY):%a %d/%m}\n" + proximas_noticias())
                if ok:
                    estado.d["noticias_dia"] = hoy
        except Exception as e:
            print(f"  ! Resumen de noticias: {e}", flush=True)
        estado.guardar()
        if a.una_vez:
            break
        seg = min(TF_MIN[t] for t in a.tf) * 60
        time.sleep(seg - (time.time() % seg) + 20)   # 20 s despues del cierre de la siguiente vela


# ================================================================= PANEL STREAMLIT
def panel():
    import plotly.graph_objects as go
    import streamlit as st

    st.set_page_config(page_title="Bot Pro · Quant Terminal", page_icon="🏛️", layout="wide")
    try:                                       # st.secrets tiene prioridad sobre el codigo
        for k in ("TELEGRAM_TOKEN", "TELEGRAM_CHAT_IDS"):
            if k in st.secrets:
                v = st.secrets[k]
                os.environ[k] = ",".join(map(str, v)) if isinstance(v, (list, tuple)) else str(v)
    except Exception:
        pass

    @st.cache_data(ttl=120, show_spinner=False)
    def cargar(nombre, tf):
        return obtener(nombre, tf)

    @st.cache_data(ttl=3600, show_spinner=False)
    def cargar_hist(nombre, tf, desde):
        return historico(nombre, tf, desde)

    @st.cache_resource
    def memoria():
        return Estado("estado.json")

    @st.cache_resource
    def motor_fondo():
        """Motor 24/7 dentro del servidor de Streamlit: arranca UNA vez y sigue escaneando aunque
        nadie tenga la pagina abierta (mientras el servidor este despierto)."""
        import threading
        info = {"inicio": datetime.now(NY), "ultimo": None, "senales": 0, "error": ""}

        def bucle():
            mem = memoria()
            ok, msg = enviar_telegram(f"✅ Bot Pro activo (motor 24/7 en Streamlit) · "
                                      f"{datetime.now(NY):%a %d/%m %H:%M} NY\nMercados: {', '.join(ACTIVOS)}\n"
                                      f"Temporalidades: {' y '.join(CUENTA['tfs'])}")
            info["error"] = "" if ok else explicar_error_telegram(msg)
            primera = True
            while True:
                try:
                    n, filas = escanear_varios(CUENTA["tfs"], dict(PARAMS_DEFAULT), mem, verbose=False)
                    info["ultimo"], info["senales"] = datetime.now(NY), info["senales"] + n
                    if primera:
                        enviar_telegram(f"📋 Estado del escaner ({' + '.join(CUENTA['tfs'])})\n"
                                        + resumen_estado(filas)[:3800])
                        primera = False
                except Exception as e:
                    info["error"] = str(e)
                mem.guardar()
                seg = min(TF_MIN[t] for t in CUENTA["tfs"]) * 60
                time.sleep(seg - (time.time() % seg) + 20)    # 20 s despues de cada cierre de vela

        threading.Thread(target=bucle, daemon=True, name="motor-24-7").start()
        return info

    fondo = motor_fondo()

    sb = st.sidebar
    sb.title("🌐 Configuración")
    estrategias = sb.multiselect("Estrategias", list(ESTRATEGIAS), list(ESTRATEGIAS),
                                 help="SMC = Order Block + OTE + FVG + POC · Barrido = liquidez PDH/PDL/Asia + "
                                      "cambio de estructura, sin indicadores")
    activo = sb.selectbox("Activo (gráfico):", list(ACTIVOS))
    tf = sb.selectbox("Temporalidad del gráfico y la tabla:", ["5m", "15m"])
    min_score = sb.slider("Score mínimo SMC (de 4)", 0, 4, PARAMS_DEFAULT["min_score"])
    banco = sb.number_input("Bank ($)", 1.0, 1_000_000.0, float(PARAMS_DEFAULT["banco"]))
    riesgo = sb.number_input("Riesgo por operación (%)", 0.1, 10.0, float(PARAMS_DEFAULT["riesgo"]))
    fib = sb.slider("Zona OTE Fibonacci", 0.30, 1.00, PARAMS_DEFAULT["fib"], 0.01)
    sb.markdown("**Killzones (hora NY)** · vacío = sin filtro")
    kz = {t: sb.multiselect(t.capitalize(), list(KILLZONES), KZ_DEFAULT[t], key=f"kz_{t}") for t in KZ_DEFAULT}
    sb.caption(" · ".join(f"{k} {a:02.0f}-{b:02.0f}h" for k, (a, b) in KILLZONES.items()))
    auto = sb.checkbox("Auto-escaneo cada 5 min", True)
    enviar = sb.checkbox("Enviar también desde el panel (con tus ajustes)", False,
                         help="El motor 24/7 ya envía las alertas con los ajustes por defecto. Actívalo solo si "
                              "quieres alertas con los ajustes de esta barra lateral.")
    if sb.button("🔔 Probar Telegram ahora"):
        ok, msg = enviar_telegram("✅ Prueba de Bot Pro")
        (sb.success if ok else sb.error)(msg if ok else f"No se pudo enviar: {msg}")
    if sb.button("⚡ Forzar escaneo ahora"):
        cargar.clear()
    p = dict(PARAMS_DEFAULT, min_score=min_score, banco=banco, riesgo=riesgo, fib=fib, kz=kz,
             estrategias=estrategias or list(ESTRATEGIAS))

    st.markdown("# 🏛️ BOT PRO · QUANT TERMINAL")
    st.caption("SMC · Barrido · Tortugas · Ruptura de Londres · contexto 1H → entrada 5m · solo velas cerradas")
    ult = f"{fondo['ultimo']:%H:%M}" if fondo["ultimo"] else "escaneando..."
    (st.error if fondo["error"] else st.success)(
        f"🟢 Motor 24/7 activo desde {fondo['inicio']:%d/%m %H:%M} NY · último escaneo {ult} · "
        f"{fondo['senales']} señales enviadas" + (f" · ⚠️ Telegram: {fondo['error']}" if fondo["error"] else ""))
    t1, t2 = st.tabs(["📡 Escáner", "🧪 Backtest"])

    with t1:
        @st.fragment(run_every=300 if auto else None)
        def escaner():
            mem = memoria()
            with st.spinner("Escaneando mercados..."):
                n, filas = escanear(tf, p, mem if enviar else None, verbose=False, cargar=cargar)
            ahora = datetime.now(NY)
            st.info(f"⚡ {ahora:%a %H:%M} NY · Killzone: {killzone_actual(ahora) or 'ninguna'} · "
                    f"{len(ACTIVOS)} activos · {n} alertas nuevas")
            st.dataframe(pd.DataFrame([{k: v for k, v in f.items() if not k.startswith("_")} for f in filas]),
                         width="stretch", hide_index=True)
            df = cargar(activo, tf)
            if df is not None:
                d = df.tail(150)
                fig = go.Figure(go.Candlestick(x=d.index.tz_convert(NY), open=d["open"], high=d["high"],
                                               low=d["low"], close=d["close"], name=activo))
                for f in filas:
                    if f.get("Activo") != activo:
                        continue
                    if f.get("_vp"):
                        for k, col in (("poc", "#facc15"), ("vah", "#a78bfa"), ("val", "#a78bfa")):
                            fig.add_hline(y=f["_vp"][k], line_dash="dot", line_color=col,
                                          annotation_text=k.upper())
                    if f.get("_ote"):
                        fig.add_hrect(y0=f["_ote"][0], y1=f["_ote"][1], fillcolor="rgba(34,197,94,0.10)",
                                      line_width=0, annotation_text="OTE", annotation_position="top left")
                    if f.get("_ob"):
                        fig.add_hrect(y0=f["_ob"][0], y1=f["_ob"][1], fillcolor="rgba(56,189,248,0.25)",
                                      line_width=0, annotation_text="OB")
                    for nm_, v in (f.get("_niveles") or {}).items():
                        fig.add_hline(y=v, line_dash="dash", line_color="#f97316", annotation_text=nm_)
                fig.update_layout(template="plotly_dark", height=460, xaxis_rangeslider_visible=False,
                                  margin=dict(l=10, r=10, t=30, b=10))
                st.plotly_chart(fig, width="stretch")
            with st.expander("📜 Registro de alertas"):
                log = memoria().d["log"]
                if log:
                    st.dataframe(pd.DataFrame(log[-30:][::-1]), width="stretch", hide_index=True)
                else:
                    st.write("Aún no hay alertas.")
        escaner()

    with t2:
        c1, c2, c3 = st.columns(3)
        activos = c1.multiselect("Activos", list(ACTIVOS), ["BTC/USDT"])
        dias = c2.number_input("Días hacia atrás", 7, 730, 58,
                               help="Forex, oro y Nasdaq vienen de Yahoo: 5m/15m solo últimos 60 días")
        gestion = c3.selectbox("Gestión", ["parcial", "tp1", "tp2"], help="parcial = 50% en 2R + BE, resto 3R")
        if st.button("▶️ Ejecutar backtest"):
            desde = (datetime.now(UTC) - timedelta(days=int(dias))).strftime("%Y-%m-%d")
            todas = []
            for nm in activos:
                with st.status(f"{nm}: descargando y simulando...") as stt:
                    d = cargar_hist(nm, tf, desde)
                    if d is None or len(d) < 500:
                        stt.update(label=f"{nm}: sin datos suficientes", state="error")
                        continue
                    barra = st.progress(0.0)
                    ops = backtest_activo(nm, ACTIVOS[nm]["tipo"], d, tf, p, "1h", gestion,
                                          progreso=lambda x: barra.progress(min(x, 1.0)))
                    if not ops.empty:
                        todas.append(ops)
                    stt.update(label=f"{nm}: {len(ops)} operaciones en {len(d)} velas", state="complete")
            if todas:
                ops = pd.concat(todas, ignore_index=True)
                res = [{"Estrategia": e, "Activo": a_, **metricas(g, riesgo)}
                       for (e, a_), g in ops.groupby(["estrategia", "activo"])]
                st.subheader("Resumen")
                st.dataframe(pd.DataFrame(res), width="stretch", hide_index=True)
                fig = go.Figure()
                for e, g in ops.groupby("estrategia"):
                    fig.add_trace(go.Scatter(y=g["R"].cumsum().to_numpy(), mode="lines", name=e))
                fig.update_layout(template="plotly_dark", height=300, title="R acumulado por estrategia",
                                  margin=dict(l=10, r=10, t=40, b=10))
                st.plotly_chart(fig, width="stretch")
                a1, a2 = st.columns(2)
                a1.markdown("**Por score**")
                a1.dataframe(desglose(ops, ["estrategia", "score"]), width="stretch")
                a2.markdown("**Por killzone**")
                a2.dataframe(desglose(ops, ["estrategia", "killzone"]), width="stretch")
                st.dataframe(ops, width="stretch", hide_index=True)
                st.download_button("⬇️ Descargar operaciones (CSV)", ops.to_csv(index=False), "operaciones.csv")
            else:
                st.warning("No hubo operaciones con estos parámetros.")
    st.caption("Herramienta educativa. No es asesoría financiera. Prueba primero en demo.")


# ================================================================= GITHUB ACTIONS
WORKFLOW_YML = """name: Bot Pro

on:
  schedule:
    - cron: "1-59/5 * * * *"       # UTC, 1 min despues de cada cierre de vela de 5m (minimo de GitHub: 5 min)
  workflow_dispatch: {}            # boton "Run workflow" para probarlo a mano

concurrency:
  group: bot-pro
  cancel-in-progress: false

jobs:
  escanear:
    runs-on: ubuntu-latest
    timeout-minutes: 10
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-python@v5
        with:
          python-version: "3.12"
      - run: pip install pandas numpy requests ccxt yfinance
      - name: Restaurar memoria anti-spam
        uses: actions/cache/restore@v4
        with:
          path: |
            estado.json
            noticias.json
          key: estado-${{ github.run_id }}
          restore-keys: estado-
      - name: Escanear
        env:
          TELEGRAM_TOKEN: ${{ secrets.TELEGRAM_TOKEN }}
          TELEGRAM_CHAT_IDS: ${{ secrets.TELEGRAM_CHAT_IDS }}
        # al pulsar "Run workflow" a mano tambien manda "✅ Bot activo" para comprobar Telegram
        run: python bot_pro.py --una-vez --tf 5m 15m ${{ github.event_name == 'workflow_dispatch' && '--ping' || '' }}
      - name: Guardar memoria anti-spam
        if: always()
        uses: actions/cache/save@v4
        with:
          path: |
            estado.json
            noticias.json
          key: estado-${{ github.run_id }}
"""


def crear_github():
    ruta = os.path.join(".github", "workflows", "bot.yml")
    os.makedirs(os.path.dirname(ruta), exist_ok=True)
    with open(ruta, "w", encoding="utf-8") as f:
        f.write(WORKFLOW_YML)
    print(f"Creado {ruta}. Subelo a tu repositorio junto con bot_pro.py.")


# ================================================================= ARRANQUE
def cli():
    ap = argparse.ArgumentParser(description="Bot Pro: escaner 24/7 + backtest (panel: streamlit run bot_pro.py)")
    ap.add_argument("modo", nargs="?", default="bot", choices=["bot", "backtest"])
    ap.add_argument("--una-vez", action="store_true", help="Una sola pasada (cron / GitHub Actions)")
    ap.add_argument("--crear-github", action="store_true", help="Crea .github/workflows/bot.yml")
    ap.add_argument("--ping", action="store_true", help="Envia '✅ Bot activo' a Telegram al arrancar")
    ap.add_argument("--tf", nargs="+", default=list(CUENTA["tfs"]), choices=["5m", "15m", "1h"],
                    help="Una o varias temporalidades, p. ej. --tf 5m 15m")
    ap.add_argument("--estrategias", nargs="*", default=list(ESTRATEGIAS), choices=list(ESTRATEGIAS))
    ap.add_argument("--min-score", type=int, default=PARAMS_DEFAULT["min_score"])
    ap.add_argument("--banco", type=float, default=PARAMS_DEFAULT["banco"])
    ap.add_argument("--riesgo", type=float, default=PARAMS_DEFAULT["riesgo"])
    ap.add_argument("--todas", action="store_true", help="Avisar de TODAS las estrategias, no solo las ganadoras")
    ap.add_argument("--sin-kz", action="store_true", help="Sin filtro de killzone")
    ap.add_argument("--cripto-24h", action="store_true", help="Cripto sin filtro de killzone")
    ap.add_argument("--estado", default="estado.json")
    # backtest
    ap.add_argument("--activos", nargs="*", default=[])
    ap.add_argument("--ctx", default="1h", choices=["1h", "4h"])
    ap.add_argument("--desde", default=None)
    ap.add_argument("--hasta", default=None)
    ap.add_argument("--csv", default=None)
    ap.add_argument("--csv-tz", default="UTC")
    ap.add_argument("--nombre", default="CSV")
    ap.add_argument("--tipo", default="forex", choices=["cripto", "forex", "oro", "indice"])
    ap.add_argument("--gestion", default="parcial", choices=["parcial", "tp1", "tp2"])
    ap.add_argument("--coste", type=float, default=None)
    ap.add_argument("--salida", default="resultados_backtest")
    a = ap.parse_args()
    if a.crear_github:
        return crear_github()

    kz = {k: list(v) for k, v in KZ_DEFAULT.items()}
    if a.sin_kz:
        kz = {k: [] for k in kz}
    if a.cripto_24h:
        kz["cripto"] = []
    p = dict(PARAMS_DEFAULT, min_score=a.min_score, banco=a.banco, riesgo=a.riesgo, kz=kz,
             estrategias=a.estrategias, solo_ganadoras=(a.modo == "bot" and not a.todas))
    if a.modo == "backtest":
        base_salida = a.salida
        for tf in a.tf:
            a.tf, a.salida = tf, os.path.join(base_salida, tf)
            correr_backtest(a, p)
            a.tf = None
    else:
        correr_bot(a, p)


def _en_streamlit():
    try:
        from streamlit import runtime
        return runtime.exists()
    except Exception:
        return False


if __name__ == "__main__":
    panel() if _en_streamlit() else cli()
