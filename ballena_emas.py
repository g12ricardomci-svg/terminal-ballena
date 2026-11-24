# -*- coding: utf-8 -*-
"""
BALLENA EMAS - cruce de EMAs en 5m y 15m a favor de la tendencia de 1 hora
==========================================================================
Base: el "Triple Screen" de Alexander Elder (Trading for a Living): una temporalidad MAYOR marca la "marea"
(la direccion) y solo se opera a su favor en la temporalidad MENOR.

  Pantalla 1 (1H, la marea):  ALCISTA si el cierre esta sobre la EMA 200 y la EMA 13 sube (pendiente de Elder).
                              BAJISTA en espejo. Si no, NEUTRAL: no se opera.
  Pantalla 2 (5m o 15m):      la EMA 9 cruza a la EMA 21 a favor de la marea en la ULTIMA vela cerrada,
                              la vela cierra al otro lado de las dos EMAs y la EMA 21 apunta en la misma direccion.
  Entrada:                    a mercado en la apertura de la vela siguiente (el precio de cierre de la alerta).
  Stop:                       tras el minimo (maximo) de las ultimas 5 velas, nunca menos de 1 ATR(14).
  Objetivos:                  TP1 = 2R (se cierra la mitad y el stop pasa a la entrada) · TP2 = 3R.
                              Salida anticipada si la EMA 9 vuelve a cruzar en contra.

  RUPTURA DE RANGO (Toby Crabel, "Day Trading with Short Term Price Patterns": contraccion -> expansion):
    rango = las ultimas 20 velas, estrecho (alto entre 0,8 y 3 ATR). La vela cierra FUERA del rango, con cuerpo y
    a favor de la marea 1H (es la primera que cierra fuera). Stop en la mitad del rango (min 1 ATR). TP1 2R, TP2 3R.

  python ballena_emas.py                 -> bot: escanea cada 5 min y avisa por Telegram (con la imagen)
  python ballena_emas.py --una-vez       -> una pasada
  python ballena_emas.py --ping          -> manda "✅ activo" al arrancar
  python ballena_emas.py backtest        -> backtest (Yahoo ~59 dias en 5m/15m; BTC y oro via exchanges)

Telegram: variables de entorno TELEGRAM_TOKEN y TELEGRAM_CHAT_IDS (las mismas del bot terminal-ballena).
Herramienta educativa. No es asesoria financiera. Prueba primero en demo.
"""
import argparse
import io
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
VERSION = "v11 · consenso 3+ · 8 mercados"

# ----------------------------------------------------------------- CONFIGURACION (EDITA AQUI)
# Mercados elegidos: 5 pares de forex + oro, BTC y Nasdaq 100 (en 5m y 15m con todas las estrategias).
PARES_FOREX = ["EUR/USD", "AUD/USD", "GBP/USD", "USD/JPY", "GBP/JPY"]
MERCADOS = {par: {"tipo": "forex", "yf": par.replace("/", "") + "=X"} for par in PARES_FOREX}
MERCADOS.update({   # nombre -> tipo, ticker de Yahoo y, si existe, simbolo de exchange (ccxt)
    "XAU/USD (Oro)": {"tipo": "oro", "yf": "GC=F", "ccxt": "PAXG/USDT"},
    "BTC/USD": {"tipo": "cripto", "yf": "BTC-USD", "ccxt": "BTC/USDT"},
    "NASDAQ 100": {"tipo": "indice", "yf": "NQ=F"},
})
CONFIG = {
    "tfs": ["5m", "15m"],         # temporalidades de entrada
    "tf_tendencia": "1h",         # la "marea" de Elder
    "ema_rapida": 9, "ema_lenta": 21,           # el cruce
    "ema_marea": 13, "ema_filtro": 200,         # pendiente de la EMA 13 y precio vs EMA 200 en 1H
    "velas_stop": 5, "atr_min_stop": 1.0,       # stop tras las ultimas 5 velas, minimo 1 ATR
    "tp1_r": 2.0, "tp2_r": 3.0,
    "max_velas": 96,              # cierre por tiempo (96 velas = 8 h en 5m, 24 h en 15m)
    "pausa_min": 60,              # minutos entre dos alertas del mismo mercado, TF y direccion
    "banco": 50.0, "riesgo_pct": 1.0,           # capital y % de riesgo para el tamano sugerido
    "imagen": False,              # True = cada alerta con su grafico (ahora: solo el mensaje resumido)
    "velas_confirmacion": 2,      # el cruce se confirma cuando pasan 2 velas que lo mantienen
    "aviso_cruce": True,          # avisar en el momento del cruce (solo en modo "sueltas")
    "modo_senales": "consenso",   # "consenso" = solo llegan senales con >= min_estrategias estrategias de acuerdo
                                  # "combinadas" = parejas fijas (OB+S/R, ruptura+retest, cruce+MTF, OB+cruce)
                                  # "sueltas" = cada estrategia envia su propia senal
    "min_estrategias": 3,         # MAS de dos estrategias unidas en la misma direccion
    "combinaciones": ["ob_sr", "ruptura_retest", "cruce_mtf", "ob_cruce"],
    "combo_ventana": 3,           # el cruce del combo "OB + Cruce" debe estar en las ultimas 3 velas
    "estrategias": ["cruce", "retest", "orderblock", "confluencia", "sr", "ruptura_sr"],
                                  # cada una envia SU senal por separado (nunca se combinan). Disponibles:
                                  # "cruce", "retest" (ruptura + retest), "orderblock", "confluencia" y "ruptura"
    "ob_buscar": 40,              # el BOS que origina el order block, en las ultimas 40 velas
    "sr_mirar": 200,              # soportes/resistencias: velas hacia atras donde buscar los toques
    "sr_toques_min": 2,           # toques previos minimos del nivel (la entrada es el siguiente toque)
    "sr_tol_atr": 0.25,           # un toque cuenta si llega a +-0,25 ATR del nivel
    "sr_separacion": 5,           # velas minimas entre dos toques
    "sr_fuerte_toques": 3,        # un nivel es FUERTE con 3+ toques: su ruptura al cierre da senal
    "retest_buscar": 15,          # la ruptura debe haber ocurrido en las ultimas 15 velas
    "retest_tol_atr": 0.25,       # el retest toca el nivel roto con 0,25 ATR de tolerancia
    "retest_min_stop_atr": 0.6,   # stop minimo del retest: 0,6 ATR
    "rango_velas": 20,            # velas que forman el rango
    "rango_max_atr": 3.0,         # el rango debe ser ESTRECHO: alto <= 3 ATR (contraccion de volatilidad, Crabel)
    "rango_min_atr": 0.8,         # y no ridiculo: alto >= 0,8 ATR
}
COSTE = {"cripto": 0.10, "forex": 0.01, "oro": 0.02, "indice": 0.01}   # % por lado (spread + comision)
TF_MIN = {"5m": 5, "15m": 15, "1h": 60}
YF_CFG = {"5m": ("5m", "15d"), "15m": ("15m", "30d"), "1h": ("1h", "60d")}
YF_LIMITE_DIAS = {"5m": 59, "15m": 59, "1h": 729}
EXCHANGES = ["kucoin", "okx", "bybit", "binance"]


# ----------------------------------------------------------------- TELEGRAM
TG = {"ultimo": 0.0}
_dormir = time.sleep


# Si creas los secretos TELEGRAM_TOKEN / TELEGRAM_CHAT_IDS en GitHub, tienen prioridad sobre estos valores.
DEFAULT_SECRETS = {      # repositorio PUBLICO: sin credenciales en el codigo. Se leen de los secretos de GitHub
    "TELEGRAM_TOKEN": "",  # (Settings -> Secrets and variables -> Actions: TELEGRAM_TOKEN y TELEGRAM_CHAT_IDS)
    "TELEGRAM_CHAT_IDS": "",
}


def secreto(n):
    """Secreto de GitHub (variable de entorno) o, si no hay, la misma configuracion de Telegram que usa el bot
    Terminal Ballena (bot_pro.py, en este mismo repositorio)."""
    valor = os.environ.get(n) or DEFAULT_SECRETS.get(n, "")
    if not valor:
        try:
            import bot_pro
            valor = bot_pro.DEFAULT_SECRETS.get(n, "")
        except Exception:
            valor = ""
    return str(valor).strip()


def _chats():
    return [i.strip() for i in secreto("TELEGRAM_CHAT_IDS").replace(";", ",").split(",") if i.strip()]


def _pausa_tg():
    espera = 1.1 - (time.time() - TG["ultimo"])
    if espera > 0:
        _dormir(espera)


ENCABEZADO = "🐋 BALLENA EMAS"


def enviar_telegram(texto, png=None):
    """Texto (o foto con el texto como pie si cabe en 1024) a todos los chats, con el nombre del bot arriba."""
    texto = f"{ENCABEZADO}\n{texto}"
    token, ids = secreto("TELEGRAM_TOKEN"), _chats()
    if not token or not ids:
        return False, "Faltan TELEGRAM_TOKEN / TELEGRAM_CHAT_IDS"
    errores = []
    for chat in ids:
        try:
            if png:
                _pausa_tg()
                r = requests.post(f"https://api.telegram.org/bot{token}/sendPhoto",
                                  data={"chat_id": chat, "caption": texto[:1024] if len(texto) <= 1024 else
                                        texto.split("\n")[0]},
                                  files={"photo": ("ballena.png", png, "image/png")}, timeout=30)
                TG["ultimo"] = time.time()
                if not r.ok:
                    errores.append(f"{chat}: {r.status_code} {r.text[:100]}")
                    continue
                if len(texto) <= 1024:
                    continue
            for i in range(0, len(texto), 3900):
                _pausa_tg()
                r = requests.post(f"https://api.telegram.org/bot{token}/sendMessage",
                                  json={"chat_id": chat, "text": texto[i:i + 3900]}, timeout=15)
                TG["ultimo"] = time.time()
                if not r.ok:
                    errores.append(f"{chat}: {r.status_code} {r.text[:100]}")
                    break
        except Exception as e:
            errores.append(f"{chat}: {e}")
    return not errores, "; ".join(errores) or "Enviado"


# ----------------------------------------------------------------- HORARIOS
def _ny(ahora=None):
    a = ahora or datetime.now(UTC)
    if isinstance(a, pd.Timestamp):
        a = a.to_pydatetime()
    if a.tzinfo is None:
        a = a.replace(tzinfo=UTC)
    return a.astimezone(NY)


def mercado_abierto(tipo, ahora=None):
    """Cripto 24/7 · forex dom 17:00 a vie 17:00 NY · oro e indices CME dom 18:00 a vie 17:00 con pausa 17-18."""
    if tipo == "cripto":
        return True
    n = _ny(ahora)
    h, wd = n.hour + n.minute / 60, n.weekday()
    if wd == 5:
        return False
    if tipo == "forex":
        return not ((wd == 4 and h >= 17) or (wd == 6 and h < 17))
    if wd == 6:
        return h >= 18
    if wd == 4 and h >= 17:
        return False
    return not (17 <= h < 18)


# ----------------------------------------------------------------- DATOS
_EX, _CACHE = {}, {}


def _ex(nombre):
    if nombre not in _EX:
        _EX[nombre] = getattr(ccxt, nombre)({"enableRateLimit": True, "timeout": 15000})
    return _EX[nombre]


def _df_ccxt(raw):
    d = pd.DataFrame(raw, columns=["t", "open", "high", "low", "close", "volume"])
    d.index = pd.to_datetime(d["t"], unit="ms", utc=True)
    return d[["open", "high", "low", "close", "volume"]].astype(float)


def velas_ccxt(simbolo, tf, desde=None, limite=1000):
    if ccxt is None:
        return None
    for ex in EXCHANGES:
        try:
            if desde is None:
                return _df_ccxt(_ex(ex).fetch_ohlcv(simbolo, tf, limit=limite))
            partes, t = [], int(pd.Timestamp(desde).timestamp() * 1000)
            for _ in range(400):
                raw = _ex(ex).fetch_ohlcv(simbolo, tf, since=t, limit=1000)
                if not raw:
                    break
                partes.append(_df_ccxt(raw))
                t = raw[-1][0] + 1
                if len(raw) < 2:
                    break
            if partes:
                d = pd.concat(partes)
                return d[~d.index.duplicated()].sort_index()
        except Exception:
            continue
    return None


def _limpiar_yf(d):
    if d is None or d.empty:
        return None
    if isinstance(d.columns, pd.MultiIndex):
        d.columns = d.columns.get_level_values(0)
    d = d.rename(columns=str.lower)[["open", "high", "low", "close", "volume"]].astype(float).dropna()
    d.index = pd.to_datetime(d.index, utc=True)
    return d


def velas_yf(ticker, tf, desde=None):
    if yf is None:
        return None
    try:
        if desde is None:
            iv, per = YF_CFG[tf]
            return _limpiar_yf(yf.download(ticker, interval=iv, period=per, progress=False, auto_adjust=False,
                                           threads=False))
        lim = datetime.now(UTC) - timedelta(days=YF_LIMITE_DIAS[tf])
        ini = max(pd.Timestamp(desde, tz="UTC") if pd.Timestamp(desde).tzinfo is None else pd.Timestamp(desde),
                  pd.Timestamp(lim))
        return _limpiar_yf(yf.download(ticker, interval=tf, start=ini.to_pydatetime(), progress=False,
                                       auto_adjust=False, threads=False))
    except Exception:
        return None


def _sigue_valida(t_descarga, tf, ahora=None):
    """La cache vale mientras no haya cerrado una vela nueva de ese TF desde la descarga (y como maximo 1 TF)."""
    ahora = ahora or time.time()
    seg = TF_MIN.get(tf, 5) * 60
    return ahora - t_descarga < seg and int(ahora // seg) == int(t_descarga // seg)


def obtener(nombre, tf):
    """Velas recientes. Solo se vuelven a descargar cuando cierra una vela nueva de ese TF: 15m cada 15 min y
    1H cada hora (el escaner de 28 pares hace la mitad de consultas a Yahoo)."""
    v = _CACHE.get((nombre, tf))
    if v is not None and _sigue_valida(v[0], tf):
        return v[1]
    m = MERCADOS[nombre]
    df = velas_ccxt(m["ccxt"], tf) if m.get("ccxt") else None
    if df is None:
        df = velas_yf(m["yf"], tf)
    if df is not None:
        _CACHE[(nombre, tf)] = (time.time(), df)
    return df


def solo_cerradas(df, tf, ahora=None):
    if df is None or df.empty:
        return df
    ahora = pd.Timestamp(ahora or datetime.now(UTC))
    ahora = ahora.tz_localize("UTC") if ahora.tzinfo is None else ahora.tz_convert("UTC")
    return df[df.index + pd.Timedelta(minutes=TF_MIN[tf]) <= ahora]


# ----------------------------------------------------------------- INDICADORES
def ema(s, n):
    return s.ewm(span=n, adjust=False).mean()


def atr(df, n=14):
    pc = df["close"].shift()
    tr = pd.concat([df["high"] - df["low"], (df["high"] - pc).abs(), (df["low"] - pc).abs()], axis=1).max(axis=1)
    return tr.ewm(alpha=1 / n, adjust=False).mean()


def fmt(x):
    x = float(x)
    if 50 <= abs(x) < 400:                         # pares con JPY (p. ej. 158.950): 3 decimales
        return f"{x:.3f}"
    return f"{x:,.2f}" if abs(x) >= 100 else (f"{x:.3f}" if abs(x) >= 10 else f"{x:.5f}")


# ----------------------------------------------------------------- ESTRATEGIA (Triple Screen + cruce de EMAs)
def marea(d1):
    """Pantalla 1 (1H): +1 alcista, -1 bajista, 0 neutral. Usa solo velas de 1H ya cerradas."""
    if d1 is None or len(d1) < CONFIG["ema_filtro"] + 5:
        return 0
    c = d1["close"]
    e13, e200 = ema(c, CONFIG["ema_marea"]), ema(c, CONFIG["ema_filtro"])
    sube, baja = e13.iloc[-1] > e13.iloc[-2], e13.iloc[-1] < e13.iloc[-2]
    if c.iloc[-1] > e200.iloc[-1] and sube:
        return 1
    if c.iloc[-1] < e200.iloc[-1] and baja:
        return -1
    return 0


def senal_cruce(df, d1):
    """Pantalla 2: la EMA 9 cruzo a la 21 a favor de la marea hace CONFIG['velas_confirmacion'] velas y las velas
    siguientes lo MANTIENEN (la 9 sigue del mismo lado y cierran al otro lado de la 21). Entrada al cierre de la
    ultima vela de confirmacion. Devuelve dict o (None, motivo)."""
    if df is None or len(df) < 60:
        return None, "sin datos suficientes"
    m = marea(d1)
    if m == 0:
        return None, "marea 1H neutral (no se opera)"
    c = df["close"]
    r, l_ = ema(c, CONFIG["ema_rapida"]), ema(c, CONFIG["ema_lenta"])
    nconf = CONFIG["velas_confirmacion"]
    k = len(c) - 1 - nconf                                     # vela del cruce
    lado = ((r - l_) * m > 0).to_numpy()
    if not (lado[k] and not lado[k - 1]):
        if lado[-1] and not lado[-2]:
            return None, f"cruce {'alcista' if m > 0 else 'bajista'} recién formado: confirmo en {nconf} velas"
        return None, f"marea {'alcista' if m > 0 else 'bajista'} · sin cruce para confirmar"
    if not lado[k:].all():
        return None, "el cruce no se mantuvo (vuelta en contra)"
    if not (((c.iloc[k + 1:] - l_.iloc[k + 1:]) * m > 0).all()):
        return None, "las velas de confirmación no cerraron al otro lado de la EMA 21"
    if (l_.iloc[-1] - l_.iloc[k]) * m <= 0:
        return None, "cruce confirmado pero con la EMA 21 en contra"
    px = float(c.iloc[-1])
    a = float(atr(df).iloc[-1])
    n = CONFIG["velas_stop"]
    extremo = float(df["low"].iloc[-n:].min()) if m > 0 else float(df["high"].iloc[-n:].max())
    dist = max(abs(px - extremo) + 0.1 * a, CONFIG["atr_min_stop"] * a)
    return {"bias": m, "dir": "LONG" if m > 0 else "SHORT", "entrada": px, "sl": px - m * dist,
            "tp1": px + m * CONFIG["tp1_r"] * dist, "tp2": px + m * CONFIG["tp2_r"] * dist, "riesgo_px": dist,
            "atr": a, "ema_r": float(r.iloc[-1]), "ema_l": float(l_.iloc[-1]), "vela": df.index[-1],
            "vela_ruptura": df.index[k]}, ""


def aviso_cruce(df, d1):
    """Aviso en el MOMENTO del cruce (ultima vela) a favor de la marea: direccion esperada, sin niveles."""
    if df is None or len(df) < 60:
        return None
    m = marea(d1)
    if m == 0:
        return None
    c = df["close"]
    r, l_ = ema(c, CONFIG["ema_rapida"]), ema(c, CONFIG["ema_lenta"])
    if (r.iloc[-1] - l_.iloc[-1]) * m > 0 and (r.iloc[-2] - l_.iloc[-2]) * m <= 0:
        return {"bias": m, "dir": "LONG" if m > 0 else "SHORT", "entrada": float(c.iloc[-1]), "vela": df.index[-1],
                "aviso": True}
    return None


def senal_ruptura(df, d1):
    """Ruptura de rango estrecho a favor de la marea 1H en la ULTIMA vela cerrada. Devuelve dict o (None, motivo)."""
    n = CONFIG["rango_velas"]
    if df is None or len(df) < n + 30:
        return None, "sin datos suficientes"
    m = marea(d1)
    if m == 0:
        return None, "marea 1H neutral (no se opera)"
    o, h, l, c = (df[x].to_numpy(float) for x in ("open", "high", "low", "close"))
    hi, lo = float(h[-n - 1:-1].max()), float(l[-n - 1:-1].min())   # rango de las n velas ANTERIORES
    a = float(atr(df).iloc[-2])
    alto = hi - lo
    if not (CONFIG["rango_min_atr"] * a <= alto <= CONFIG["rango_max_atr"] * a):
        return None, f"sin rango estrecho (alto {alto / a:.1f} ATR)"
    nivel = hi if m > 0 else lo
    rompe = (c[-1] > hi and c[-2] <= hi and c[-1] > o[-1]) if m > 0 else (c[-1] < lo and c[-2] >= lo and c[-1] < o[-1])
    if not rompe:
        return None, f"rango {fmt(lo)}-{fmt(hi)}: esperando ruptura {'arriba' if m > 0 else 'abajo'}"
    px = float(c[-1])
    mitad = (hi + lo) / 2
    dist = max(abs(px - mitad), CONFIG["atr_min_stop"] * a)
    return {"bias": m, "dir": "LONG" if m > 0 else "SHORT", "entrada": px, "sl": px - m * dist,
            "tp1": px + m * CONFIG["tp1_r"] * dist, "tp2": px + m * CONFIG["tp2_r"] * dist, "riesgo_px": dist,
            "atr": a, "rango": (lo, hi), "nivel_ruptura": float(nivel), "salida_cruce": False,
            "vela": df.index[-1]}, ""


def senal_retest(df, d1):
    """Ruptura + RETEST a favor de la marea 1H (evita rupturas falsas). Para compras:
      1) en las ultimas 15 velas, una vela cerro sobre un rango estrecho de 20 velas (primera fuera, con cuerpo);
      2) despues el precio volvio a tocar el nivel roto (+-0,25 ATR) SIN cerrar dentro del rango (si cierra dentro,
         la ruptura era falsa y el setup se anula);
      3) la ULTIMA vela confirma: alcista, cierra sobre el nivel y sobre el maximo de la vela anterior.
    Entrada al cierre de la confirmacion, stop bajo el minimo del retest (min 0,6 ATR), TP1 2R, TP2 3R."""
    n, buscar = CONFIG["rango_velas"], CONFIG["retest_buscar"]
    if df is None or len(df) < n + buscar + 30:
        return None, "sin datos suficientes"
    m = marea(d1)
    if m == 0:
        return None, "marea 1H neutral (no se opera)"
    if m < 0:                                            # ventas: espejo de precios y misma logica
        f = pd.DataFrame({"open": -df["open"], "high": -df["low"], "low": -df["high"], "close": -df["close"]},
                         index=df.index)
        s, motivo = _retest_largo(f)
        if s is None:
            return None, motivo
        s = {**s, "entrada": -s["entrada"], "sl": -s["sl"], "tp1": -s["tp1"], "tp2": -s["tp2"],
             "rango": (-s["rango"][1], -s["rango"][0]), "nivel_ruptura": -s["nivel_ruptura"]}
        s.update(bias=-1, dir="SHORT")
        return s, ""
    return _retest_largo(df)


def _retest_largo(df):
    n, buscar = CONFIG["rango_velas"], CONFIG["retest_buscar"]
    o, h, l, c = (df[x].to_numpy(float) for x in ("open", "high", "low", "close"))
    a_ser = atr(df).to_numpy()
    ult = len(c) - 1
    for b in range(ult - buscar, ult - 1):             # la PRIMERA vela que cierra fuera (deja >= 1 vela de retest)
        hi, lo = h[b - n:b].max(), l[b - n:b].min()
        a = a_ser[b - 1]
        if not (CONFIG["rango_min_atr"] * a <= hi - lo <= CONFIG["rango_max_atr"] * a):
            continue
        if not (c[b] > hi and c[b - 1] <= hi and c[b] > o[b]):
            continue
        tras = slice(b + 1, ult + 1)
        tol = CONFIG["retest_tol_atr"] * a
        if (c[tras] < hi - tol).any():
            return None, f"ruptura falsa: volvió a cerrar dentro del rango {fmt(lo)}-{fmt(hi)}"
        toques = [j for j in range(b + 1, ult + 1) if l[j] <= hi + tol]
        if not toques:
            return None, f"ruptura de {fmt(hi)}: esperando el retest del nivel"
        if not (c[ult] > o[ult] and c[ult] > hi and c[ult] > h[ult - 1] and toques[-1] >= ult - 3):
            return None, f"retest de {fmt(hi)} en curso: esperando vela de confirmación"
        px = float(c[ult])
        a_u = a_ser[ult]
        dist = max(px - (min(l[j] for j in toques) - 0.1 * a_u), CONFIG["retest_min_stop_atr"] * a_u)
        return {"bias": 1, "dir": "LONG", "entrada": px, "sl": px - dist, "tp1": px + CONFIG["tp1_r"] * dist,
                "tp2": px + CONFIG["tp2_r"] * dist, "riesgo_px": dist, "atr": float(a_u), "rango": (float(lo), float(hi)),
                "nivel_ruptura": float(hi), "salida_cruce": False, "vela": df.index[ult],
                "vela_ruptura": df.index[b]}, ""
    return None, "sin ruptura reciente de un rango estrecho"


def _espejo(df):
    return pd.DataFrame({"open": -df["open"], "high": -df["low"], "low": -df["high"], "close": -df["close"]},
                        index=df.index)


def _desespejar(s):
    s = {**s, "entrada": -s["entrada"], "sl": -s["sl"], "tp1": -s["tp1"], "tp2": -s["tp2"]}
    if s.get("zona"):
        s["zona"] = (-s["zona"][1], -s["zona"][0])
    s.update(bias=-1, dir="SHORT")
    return s


def senal_orderblock(df, d1):
    """ORDER BLOCK a favor de la marea 1H. Para compras:
      1) BOS: en las ultimas 40 velas una vela CIERRA sobre un maximo previo (swing);
      2) order block = la ultima vela bajista en el origen de ese impulso (zona: su cuerpo alto -> su minimo);
      3) el precio vuelve a la zona sin CERRAR por debajo de ella (si cierra debajo, el bloque se rompio);
      4) la ULTIMA vela confirma: alcista y cierra sobre la zona.
    Entrada al cierre, stop bajo el bloque (min 0,6 ATR), TP1 2R, TP2 3R. Una alerta por bloque."""
    if df is None or len(df) < CONFIG["ob_buscar"] + 40:
        return None, "sin datos suficientes"
    m = marea(d1)
    if m == 0:
        return None, "marea 1H neutral (no se opera)"
    if m > 0:
        return _ob_largo(df)
    s, motivo = _ob_largo(_espejo(df))
    return (_desespejar(s), "") if s else (None, motivo)


def _ob_largo(df, k=2):
    o, h, l, c = (df[x].to_numpy(float) for x in ("open", "high", "low", "close"))
    a_ser = atr(df).to_numpy()
    ult = len(c) - 1
    altos = [j for j in range(k, ult - k) if h[j] == h[j - k:j + k + 1].max()]
    bos = None
    for b in range(ult - CONFIG["ob_buscar"], ult):            # el BOS mas reciente (deja >= 1 vela de vuelta)
        previos = [j for j in altos if j + k < b]
        if previos and c[b] > h[previos[-1]] and c[b - 1] <= h[previos[-1]]:
            bos = (b, previos[-1])
    if bos is None:
        return None, "sin BOS reciente: no hay order block"
    b, j = bos
    org = j + int(np.argmin(l[j:b + 1]))                       # minimo que origino el impulso
    q = next((x for x in range(org, max(org - 6, 0), -1) if c[x] < o[x]), None)
    if q is None:
        return None, "impulso sin vela contraria (sin order block)"
    arriba, abajo = float(max(o[q], c[q])), float(min(l[q], l[org]))
    if (c[b + 1:] < abajo).any():
        return None, f"order block {fmt(abajo)}-{fmt(arriba)} roto"
    toques = [x for x in range(b + 1, ult + 1) if l[x] <= arriba]
    if not toques:
        return None, f"order block {fmt(abajo)}-{fmt(arriba)}: esperando que el precio vuelva"
    if not (c[ult] > o[ult] and c[ult] > arriba and toques[-1] >= ult - 3):
        return None, f"precio en el order block {fmt(abajo)}-{fmt(arriba)}: esperando confirmación"
    a = float(a_ser[ult])
    px = float(c[ult])
    dist = max(px - (abajo - 0.1 * a), CONFIG["retest_min_stop_atr"] * a)
    return {"bias": 1, "dir": "LONG", "entrada": px, "sl": px - dist, "tp1": px + CONFIG["tp1_r"] * dist,
            "tp2": px + CONFIG["tp2_r"] * dist, "riesgo_px": dist, "atr": a, "zona": (abajo, arriba),
            "salida_cruce": False, "vela": df.index[ult], "vela_ruptura": df.index[q]}, ""


def _alineada(df, m):
    """Tendencia de una temporalidad: EMA 9 sobre la 21 y cierre sobre la 21 (en ventas, al reves)."""
    c = df["close"]
    r, l_ = ema(c, CONFIG["ema_rapida"]), ema(c, CONFIG["ema_lenta"])
    return ((r - l_) * m > 0) & ((c - l_) * m > 0)


def senal_confluencia(df, d1):
    """CONFLUENCIA MULTITEMPORAL (solo en 5m): 1H (marea) + 15m + 5m apuntan igual. Se avisa en la vela de 5m en
    la que se completa la alineacion (antes no lo estaba). Stop tras las ultimas 5 velas (min 1 ATR), TP1 2R, TP2 3R."""
    if df is None or len(df) < 120:
        return None, "sin datos suficientes"
    if df.index[-1] - df.index[-2] != pd.Timedelta(minutes=5):
        return None, "solo en 5m"
    m = marea(d1)
    if m == 0:
        return None, "marea 1H neutral (no se opera)"
    m15 = df.resample("15min", label="left", closed="left").agg(
        {"open": "first", "high": "max", "low": "min", "close": "last"}).dropna()
    if df.index[-1] < m15.index[-1] + pd.Timedelta(minutes=10):
        m15 = m15.iloc[:-1]                                    # la vela de 15m en curso aun no cerro
    if not bool(_alineada(m15, m).iloc[-1]):
        return None, f"15m no acompaña a la marea {'alcista' if m > 0 else 'bajista'}"
    al5 = _alineada(df, m)
    if not al5.iloc[-1]:
        return None, "1H y 15m alineadas: esperando que 5m se alinee"
    if al5.iloc[-2]:
        return None, "1H, 15m y 5m ya alineadas (señal dada antes)"
    a = float(atr(df).iloc[-1])
    px = float(df["close"].iloc[-1])
    ext = float(df["low"].iloc[-5:].min()) if m > 0 else float(df["high"].iloc[-5:].max())
    dist = max(abs(px - ext) + 0.1 * a, CONFIG["atr_min_stop"] * a)
    return {"bias": m, "dir": "LONG" if m > 0 else "SHORT", "entrada": px, "sl": px - m * dist,
            "tp1": px + m * CONFIG["tp1_r"] * dist, "tp2": px + m * CONFIG["tp2_r"] * dist, "riesgo_px": dist,
            "atr": a, "salida_cruce": False, "vela": df.index[-1]}, ""


def senal_sr(df, d1=None):
    """SOPORTES Y RESISTENCIAS: mirando hacia atras (200 velas) busca niveles horizontales con al menos 2 toques
    (minimos o maximos de swing a +-0,25 ATR, separados >= 5 velas) que no se hayan roto al cierre. Avisa en el
    SIGUIENTE toque (el tercero) si la ultima vela toca el nivel y lo RECHAZA:
      soporte    -> vela alcista que cierra sobre el nivel  -> COMPRA
      resistencia-> vela bajista que cierra bajo el nivel   -> VENTA
    Stop tras el extremo de los toques (min 0,6 ATR), TP1 2R, TP2 3R. No depende de la marea (se informa)."""
    if df is None or len(df) < 80:
        return None, "sin datos suficientes"
    s = _sr_compra(df)
    if s[0]:
        return s
    v, motivo_v = _sr_compra(_espejo(df))
    if v:
        v = _desespejar(v)
        v["nivel"] = -v["nivel"]
        return v, ""
    return None, s[1] if "toque" in s[1] else motivo_v


def _soportes(d, k=3, toques_min=None):
    """Soportes vigentes ANTES de la ultima vela: [(nivel, [indices de los toques])], sin romper al cierre."""
    toques_min = toques_min or CONFIG["sr_toques_min"]
    l, c = d["low"].to_numpy(float), d["close"].to_numpy(float)
    ult = len(c) - 1
    tol = CONFIG["sr_tol_atr"] * float(atr(d).iloc[-2])
    minimos = [j for j in range(k, ult - k) if l[j] == l[j - k:j + k + 1].min()]
    niveles, vistos = [], set()
    for j in minimos:
        grupo = [x for x in minimos if abs(l[x] - l[j]) <= tol]
        toques = []
        for x in grupo:                                  # toques separados al menos sr_separacion velas
            if not toques or x - toques[-1] >= CONFIG["sr_separacion"]:
                toques.append(x)
        if len(toques) < toques_min or ult - toques[-1] < CONFIG["sr_separacion"] or tuple(toques) in vistos:
            continue
        vistos.add(tuple(toques))
        nivel = float(np.mean([l[x] for x in toques]))
        if (c[toques[0]:ult] < nivel - tol).any():
            continue                                     # el soporte ya se rompio al cierre antes: no vale
        niveles.append((nivel, toques))
    return niveles


def _sr_compra(df, k=3):
    """Soporte con >= N toques previos y rechazo alcista en la ultima vela (las ventas usan el espejo)."""
    d = df.tail(CONFIG["sr_mirar"] + 1)
    o, h, l, c = (d[x].to_numpy(float) for x in ("open", "high", "low", "close"))
    ult = len(c) - 1
    a = float(atr(d).iloc[-1])
    tol = CONFIG["sr_tol_atr"] * a
    niveles = _soportes(d, k)
    mejor = min(niveles, key=lambda x: abs(c[ult] - x[0])) if niveles else None
    if mejor is None:
        return None, f"sin nivel con {CONFIG['sr_toques_min']}+ toques"
    nivel, toques = mejor
    if not (l[ult] <= nivel + tol and c[ult] > nivel and c[ult] > o[ult]):
        return None, f"nivel {fmt(nivel)} con {len(toques)} toques: esperando el toque {len(toques) + 1}"
    px = float(c[ult])
    extremo = min(min(l[x] for x in toques), l[ult])
    dist = max(px - (extremo - 0.1 * a), CONFIG["retest_min_stop_atr"] * a)
    return {"bias": 1, "dir": "LONG", "entrada": px, "sl": px - dist, "tp1": px + CONFIG["tp1_r"] * dist,
            "tp2": px + CONFIG["tp2_r"] * dist, "riesgo_px": dist, "atr": a, "nivel": nivel,
            "toques": len(toques) + 1, "salida_cruce": False, "vela": d.index[ult],
            "vela_ruptura": d.index[toques[0]]}, ""


def _rompe_soporte(df):
    """VENTA si la ultima vela CIERRA bajo un soporte fuerte (3+ toques) y es la primera en cerrar debajo."""
    d = df.tail(CONFIG["sr_mirar"] + 1)
    o, h, l, c = (d[x].to_numpy(float) for x in ("open", "high", "low", "close"))
    ult = len(c) - 1
    a = float(atr(d).iloc[-1])
    tol = CONFIG["sr_tol_atr"] * a
    fuertes = _soportes(d, toques_min=CONFIG["sr_fuerte_toques"])
    if not fuertes:
        return None, f"sin soporte/resistencia fuerte ({CONFIG['sr_fuerte_toques']}+ toques)"
    rotos = [(n_, t_) for n_, t_ in fuertes if c[ult] < n_ - tol and c[ult - 1] >= n_ - tol and c[ult] < o[ult]]
    if not rotos:
        cerca = min(fuertes, key=lambda x: abs(c[ult] - x[0]))
        return None, f"nivel fuerte {fmt(cerca[0])} ({len(cerca[1])} toques): esperando ruptura al cierre"
    nivel, toques = max(rotos, key=lambda x: len(x[1]))           # el mas fuerte de los rotos
    px = float(c[ult])
    dist = max((nivel + 0.3 * a) - px, CONFIG["atr_min_stop"] * 0.8 * a)
    return {"bias": -1, "dir": "SHORT", "entrada": px, "sl": px + dist, "tp1": px - CONFIG["tp1_r"] * dist,
            "tp2": px - CONFIG["tp2_r"] * dist, "riesgo_px": dist, "atr": a, "nivel": nivel, "toques": len(toques),
            "salida_cruce": False, "vela": d.index[ult], "vela_ruptura": d.index[toques[0]]}, ""


def senal_ruptura_sr(df, d1=None):
    """RUPTURA DE SOPORTE O RESISTENCIA FUERTE (3+ toques en las ultimas 200 velas): la ultima vela cierra con
    cuerpo al otro lado del nivel por primera vez. Resistencia rota hacia arriba -> COMPRA; soporte roto hacia abajo
    -> VENTA. Stop al otro lado del nivel (0,3 ATR, min 0,8 ATR), TP1 2R, TP2 3R. No depende de la marea."""
    if df is None or len(df) < 80:
        return None, "sin datos suficientes"
    v, motivo_v = _rompe_soporte(df)
    if v:
        return v, ""
    s, motivo_c = _rompe_soporte(_espejo(df))                     # resistencia = soporte del espejo
    if s:
        s = {**s, "entrada": -s["entrada"], "sl": -s["sl"], "tp1": -s["tp1"], "tp2": -s["tp2"], "nivel": -s["nivel"],
             "bias": 1, "dir": "LONG"}
        return s, ""
    return None, motivo_v if "esperando" in motivo_v else motivo_c


ESTRATEGIAS = {"cruce": senal_cruce, "ruptura": senal_ruptura, "retest": senal_retest,
               "orderblock": senal_orderblock, "confluencia": senal_confluencia, "sr": senal_sr,
               "ruptura_sr": senal_ruptura_sr}
NOMBRE_EST = {"cruce": "Cruce de EMAs", "ruptura": "Ruptura de rango", "retest": "Ruptura + retest",
              "orderblock": "Order Block", "confluencia": "Confluencia 1H+15m+5m", "sr": "Soporte/Resistencia",
              "ruptura_sr": "Ruptura de nivel fuerte"}


# ----------------------------------------------------------------- SENALES COMBINADAS (confirmacion por 2 estrategias)
COMBOS_NOMBRE = {"ob_sr": "Order Block + Soporte/Resistencia", "ruptura_retest": "Ruptura fuerte + Retest",
                 "cruce_mtf": "Cruce + Confluencia 1H/15m", "ob_cruce": "Order Block + Cruce EMA"}


def _nivel_en_zona(df, ob):
    """Nivel de soporte (compra) o resistencia (venta) con 2+ toques que coincide con la zona del order block."""
    d = df.tail(CONFIG["sr_mirar"] + 1)
    tol = CONFIG["sr_tol_atr"] * float(atr(d).iloc[-1])
    if ob["bias"] > 0:
        niveles = _soportes(d)
    else:
        niveles = [(-n_, t_) for n_, t_ in _soportes(_espejo(d))]
    lo, hi = sorted(ob["zona"])
    dentro = [(n_, t_) for n_, t_ in niveles if lo - tol <= n_ <= hi + tol]
    return max(dentro, key=lambda x: len(x[1])) if dentro else None


def _cruce_reciente(df, m, velas=None):
    """True si la EMA 9 cruzo a la 21 en la direccion m dentro de las ultimas 'velas' velas."""
    velas = velas or CONFIG["combo_ventana"]
    c = df["close"]
    lado = ((ema(c, CONFIG["ema_rapida"]) - ema(c, CONFIG["ema_lenta"])) * m > 0).to_numpy()
    return any(lado[i] and not lado[i - 1] for i in range(len(lado) - velas, len(lado)))


def _mtf_alineado(df, d1, m, tf):
    """1H alineada (EMA 9 sobre 21 y cierre sobre 21, o al reves) y, si se opera en 5m, tambien 15m."""
    if d1 is None or len(d1) < 30 or not bool(_alineada(d1, m).iloc[-1]):
        return False
    if tf != "5m":
        return True
    m15 = df.resample("15min", label="left", closed="left").agg(
        {"open": "first", "high": "max", "low": "min", "close": "last"}).dropna()
    if df.index[-1] < m15.index[-1] + pd.Timedelta(minutes=10):
        m15 = m15.iloc[:-1]
    return len(m15) > 30 and bool(_alineada(m15, m).iloc[-1])


def senal_retest_nivel_fuerte(df):
    """Ruptura de un nivel FUERTE (3+ toques) en las ultimas 15 velas + retest del nivel + vela de confirmacion.
    Se anula si el precio vuelve a cerrar al otro lado del nivel."""
    if df is None or len(df) < 100:
        return None
    ult = len(df) - 1
    for b in range(ult - CONFIG["retest_buscar"], ult - 1):
        rup, _ = senal_ruptura_sr(df.iloc[:b + 1])
        if not rup:
            continue
        m, nivel = rup["bias"], rup["nivel"]
        o, c = df["open"].to_numpy(float) * m, df["close"].to_numpy(float) * m
        h = (df["high"] if m > 0 else -df["low"]).to_numpy(float)
        l = (df["low"] if m > 0 else -df["high"]).to_numpy(float)
        lv = nivel * m
        a = float(atr(df).iloc[-1])
        tol = CONFIG["retest_tol_atr"] * a
        if (c[b + 1:] < lv - tol).any():
            return None                                        # volvio al otro lado: ruptura falsa
        toques = [j for j in range(b + 1, ult + 1) if l[j] <= lv + tol]
        if not toques or not (c[ult] > o[ult] and c[ult] > lv and c[ult] > h[ult - 1] and toques[-1] >= ult - 3):
            return None
        px = c[ult]
        dist = max(px - (min(l[j] for j in toques) - 0.1 * a), CONFIG["retest_min_stop_atr"] * a)
        return {"bias": m, "dir": "LONG" if m > 0 else "SHORT", "entrada": px * m, "sl": (px - dist) * m,
                "tp1": (px + CONFIG["tp1_r"] * dist) * m, "tp2": (px + CONFIG["tp2_r"] * dist) * m, "riesgo_px": dist,
                "atr": a, "nivel": nivel, "toques": rup["toques"], "salida_cruce": False, "vela": df.index[ult],
                "vela_ruptura": df.index[b]}
    return None


def _combo(base, tipo, detalle, sl=None):
    """Senal combinada a partir de la senal gatillo; si se da un stop mas lejano, recalcula los objetivos."""
    s = dict(base)
    m = s["bias"]
    if sl is not None and (s["entrada"] - sl) * m > (s["entrada"] - s["sl"]) * m:
        dist = (s["entrada"] - sl) * m
        s.update(sl=sl, riesgo_px=dist, tp1=s["entrada"] + m * CONFIG["tp1_r"] * dist,
                 tp2=s["entrada"] + m * CONFIG["tp2_r"] * dist)
    s.update(combo=tipo, combo_txt=detalle, estrategia=f"combo_{tipo}")
    return s


def senales_combinadas(df, d1, tf):
    """Las 4 combinaciones activas. Devuelve (lista de senales combinadas, motivo si no hay)."""
    activas = CONFIG["combinaciones"]
    combos = []
    ob = senal_orderblock(df, d1)[0] if {"ob_sr", "ob_cruce"} & set(activas) else None
    if ob and "ob_sr" in activas:
        nv = _nivel_en_zona(df, ob)
        if nv:
            nivel, toques = nv
            a = float(atr(df).iloc[-1])
            combos.append(_combo(ob, "ob_sr", f"Order Block + {'Soporte' if ob['bias'] > 0 else 'Resistencia'} "
                                              f"{fmt(nivel)} ({len(toques)} toques)",
                                 sl=nivel - ob["bias"] * 0.2 * a))
    if ob and "ob_cruce" in activas and _cruce_reciente(df, ob["bias"]):
        combos.append(_combo(ob, "ob_cruce", f"Order Block + Cruce EMA {CONFIG['ema_rapida']}/{CONFIG['ema_lenta']}"))
    if "cruce_mtf" in activas:
        cr = senal_cruce(df, d1)[0]
        if cr and _mtf_alineado(df, d1, cr["bias"], tf):
            combos.append(_combo(cr, "cruce_mtf", "Cruce confirmado + Confluencia " +
                                 ("1H + 15m + 5m" if tf == "5m" else "1H + 15m")))
    if "ruptura_retest" in activas:
        rr = senal_retest_nivel_fuerte(df)
        if rr:
            combos.append(_combo(rr, "ruptura_retest", f"Ruptura fuerte + Retest · "
                                                       f"{'resistencia' if rr['bias'] > 0 else 'soporte'} "
                                                       f"{fmt(rr['nivel'])} ({rr['toques']} toques)"))
    if len({c_["bias"] for c_ in combos}) > 1:
        return [], "señales combinadas contrarias: no operar"
    return combos, ("" if combos else "sin confirmación de 2 estrategias")


# ----------------------------------------------------------------- CONSENSO (3+ estrategias de acuerdo)
VOTOS_NOMBRE = {"cruce": "Cruce EMA", "orderblock": "Order Block", "sr": "Soporte/Resistencia",
                "retest": "Ruptura + retest", "ruptura_sr": "Ruptura de nivel fuerte", "mtf": "Confluencia 1H/15m"}
_ENTRADAS = ("retest_fuerte", "orderblock", "sr", "retest", "ruptura_sr", "cruce")   # prioridad del gatillo


def _senales_en_ventana(df, d1, velas):
    """Senales de cada estrategia de entrada en las ultimas 'velas' velas: {estrategia: [(edad, senal), ...]}."""
    fns = {"cruce": lambda x: senal_cruce(x, d1)[0], "orderblock": lambda x: senal_orderblock(x, d1)[0],
           "sr": lambda x: senal_sr(x)[0], "retest": lambda x: senal_retest(x, d1)[0],
           "ruptura_sr": lambda x: senal_ruptura_sr(x)[0], "retest_fuerte": senal_retest_nivel_fuerte}
    out = {k: [] for k in fns}
    for edad in range(velas):
        sub = df if edad == 0 else df.iloc[:-edad]
        for k, fn in fns.items():
            try:
                s = fn(sub)
            except Exception:
                s = None
            if s:
                out[k].append((edad, s))
    return out


def senales_consenso(df, d1, tf):
    """Senal solo si >= CONFIG['min_estrategias'] estrategias DISTINTAS apuntan en la misma direccion.
    Devuelve ([senal], motivo)."""
    if df is None or len(df) < 120:
        return [], "sin datos suficientes"
    ventana = CONFIG["combo_ventana"]
    sen = _senales_en_ventana(df, d1, ventana)
    mejores = []
    for m in (1, -1):
        votos = set()
        for k in ("cruce", "orderblock", "sr", "retest", "ruptura_sr"):
            if any(s["bias"] == m for _, s in sen[k]):
                votos.add(k)
        if any(s["bias"] == m for _, s in sen["retest_fuerte"]):
            votos |= {"ruptura_sr", "retest"}                    # ruptura fuerte reprobada = 2 estrategias
        if "cruce" not in votos and _cruce_reciente(df, m, ventana):
            votos.add("cruce")
        obs = [s for _, s in sen["orderblock"] if s["bias"] == m]
        if obs and "sr" not in votos and _nivel_en_zona(df, obs[0]):
            votos.add("sr")                                      # el bloque coincide con un nivel de 2+ toques
        if _mtf_alineado(df, d1, m, tf):
            votos.add("mtf")
        actuales = [s for k in _ENTRADAS for e, s in sen[k] if e == 0 and s["bias"] == m]
        if len(votos) >= CONFIG["min_estrategias"] and actuales:
            mejores.append((m, votos, actuales))
    if not mejores:
        return [], f"menos de {CONFIG['min_estrategias']} estrategias de acuerdo"
    if len(mejores) > 1:
        return [], "consenso en las dos direcciones: no operar"
    m, votos, actuales = mejores[0]
    gatillo = actuales[0]
    sl_lejano = min((s["sl"] for s in actuales), key=lambda x: x * m)   # el stop mas protegido
    nombres = [VOTOS_NOMBRE[v] for v in ("orderblock", "sr", "retest", "ruptura_sr", "cruce", "mtf") if v in votos]
    s = _combo(gatillo, "consenso", " + ".join(nombres), sl=sl_lejano)
    s.update(votos=len(votos))
    return [s], ""


def analizar_todas(nombre, tf, cargar=obtener, ahora=None):
    """Corre las estrategias activas en un mercado y TF. Devuelve (lista de senales, {estrategia: motivo})."""
    meta = MERCADOS[nombre]
    if not mercado_abierto(meta["tipo"], ahora):
        return [], {e: "mercado cerrado" for e in CONFIG["estrategias"]}
    df = solo_cerradas(cargar(nombre, tf), tf, ahora)
    d1 = solo_cerradas(cargar(nombre, CONFIG["tf_tendencia"]), CONFIG["tf_tendencia"], ahora)
    if CONFIG.get("modo_senales") in ("combinadas", "consenso"):
        combos, motivo = (senales_consenso if CONFIG["modo_senales"] == "consenso" else senales_combinadas)(df, d1, tf)
        for s in combos:
            s.update(activo=nombre, tipo=meta["tipo"], tf=tf,
                     id=f"{s['estrategia']}|{nombre}|{tf}|{s['dir']}|{s.get('vela_ruptura', s['vela'])}")
        return combos, {"combinadas": motivo}
    senales, motivos = [], {}
    for est in CONFIG["estrategias"]:
        s, motivo = ESTRATEGIAS[est](df, d1)
        motivos[est] = motivo
        if s:
            s.update(activo=nombre, tipo=meta["tipo"], tf=tf, estrategia=est,
                     id=f"{est}|{nombre}|{tf}|{s['dir']}|{s.get('vela_ruptura', s['vela'])}")
            senales.append(s)
    if "cruce" in CONFIG["estrategias"] and CONFIG.get("aviso_cruce"):
        av = aviso_cruce(df, d1)
        if av:
            av.update(activo=nombre, tipo=meta["tipo"], tf=tf, estrategia="aviso",
                      id=f"aviso|{nombre}|{tf}|{av['dir']}|{av['vela']}")
            senales.append(av)
    return senales, motivos


def analizar(nombre, tf, cargar=obtener, ahora=None):
    """Compatibilidad: primera senal (o None) y el motivo de la primera estrategia."""
    senales, motivos = analizar_todas(nombre, tf, cargar, ahora)
    return (senales[0], "") if senales else (None, next(iter(motivos.values()), ""))


# ----------------------------------------------------------------- TAMANO, TEXTO E IMAGEN
def tamano_txt(s):
    riesgo_usd = CONFIG["banco"] * CONFIG["riesgo_pct"] / 100
    d = s["riesgo_px"]
    if s["tipo"] == "cripto":
        return f"{riesgo_usd / d:.6f} unid. (riesgo ${riesgo_usd:.2f})"
    if s["tipo"] == "indice":
        return f"riesgo ${riesgo_usd:.2f} = {d:.1f} puntos de stop (ajusta el lote al valor del punto de tu CFD)"
    contrato = 100_000 if s["tipo"] == "forex" else 100
    usd = 1 / s["entrada"] if s["activo"].startswith("USD/") else 1.0
    lotes = riesgo_usd / (d * contrato * usd)
    return f"{lotes:.3f} lotes (riesgo ${riesgo_usd:.2f}; mínimo habitual 0.01)"


def texto_alerta(s):
    """Mensaje RESUMIDO: direccion, mercado y TF · estrategia · entrada · stop · objetivos."""
    compra = s["bias"] > 0
    minutos = TF_MIN[s["tf"]]
    hora = pd.Timestamp(s["vela"]).tz_convert(NY) + pd.Timedelta(minutes=minutos)
    if s.get("combo"):
        estrellas = f"{'⭐' * min(s['votos'], 5)} CONFIRMADA x{s['votos']}" if s.get("votos") else "⭐ CONFIRMADA"
        return (f"🚨 {estrellas} {'🟢 COMPRA' if compra else '🔴 VENTA'} {s['activo']} · {minutos}M\n"
                f"📐 {s['combo_txt']}\n"
                f"🎯 Entrada: {fmt(s['entrada'])} ({hora:%H:%M} NY)\n"
                f"🛑 SL: {fmt(s['sl'])}\n"
                f"✅ TP1: {fmt(s['tp1'])} · TP2: {fmt(s['tp2'])}")
    if s.get("aviso"):
        return (f"👀 CRUCE {'ALCISTA' if compra else 'BAJISTA'} {s['activo']} · {minutos}M\n"
                f"Dirección esperada: {'🟢 COMPRA' if compra else '🔴 VENTA'} · precio {fmt(s['entrada'])}\n"
                f"⏳ Confirmo la entrada en {CONFIG['velas_confirmacion']} velas")
    return (f"🚨 {'🟢 COMPRA' if compra else '🔴 VENTA'} {s['activo']} · {minutos}M\n"
            f"📐 {NOMBRE_EST.get(s.get('estrategia', 'cruce'), 'Señal')}"
            + (f" · ✅ confirmado ({CONFIG['velas_confirmacion']} velas)" if s.get("estrategia", "cruce") == "cruce" else "")
            + (f" · {'soporte' if compra else 'resistencia'} {fmt(s['nivel'])} · toque {s['toques']}"
               if s.get("estrategia") == "sr" else "")
            + (f" · rompe {'resistencia' if compra else 'soporte'} {fmt(s['nivel'])} ({s['toques']} toques)"
               if s.get("estrategia") == "ruptura_sr" else "") + "\n"
            f"🎯 Entrada: {fmt(s['entrada'])} ({hora:%H:%M} NY)\n"
            f"🛑 SL: {fmt(s['sl'])}\n"
            f"✅ TP1: {fmt(s['tp1'])} · TP2: {fmt(s['tp2'])}")


def pie_estrategia(s):
    est = s.get("estrategia", "cruce")
    if est == "retest":
        return (f"🐋 Ballena EMAs · ruptura + retest de {fmt(s['nivel_ruptura'])} confirmado "
                f"(rango {fmt(s['rango'][0])}-{fmt(s['rango'][1])}) a favor de la marea 1H")
    if est == "ruptura":
        return f"🐋 Ballena EMAs · ruptura del rango {fmt(s['rango'][0])}-{fmt(s['rango'][1])} a favor de la marea 1H"
    return f"🐋 Ballena EMAs · cruce EMA {CONFIG['ema_rapida']}/{CONFIG['ema_lenta']} a favor de la marea 1H"


def imagen_alerta(s, df):
    """PNG: velas, EMA 9/21/200 y lineas de entrada, stop, TP1 y TP2."""
    try:
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
        from matplotlib.patches import Rectangle
    except Exception:
        return None
    col = {"fondo": "#0f1419", "panel": "#151b23", "texto": "#c9d1d9", "rejilla": "#232b36", "sube": "#26a69a",
           "baja": "#ef5350", "r": "#f2cc60", "l": "#58a6ff", "f": "#bc8cff"}
    d = df.tail(110)
    n = len(d)
    o, h, l, c = (d[x].to_numpy(float) for x in ("open", "high", "low", "close"))
    cc = df["close"]
    emas = [(ema(cc, CONFIG["ema_rapida"]).tail(n), col["r"], f"EMA {CONFIG['ema_rapida']}"),
            (ema(cc, CONFIG["ema_lenta"]).tail(n), col["l"], f"EMA {CONFIG['ema_lenta']}"),
            (ema(cc, CONFIG["ema_filtro"]).tail(n), col["f"], f"EMA {CONFIG['ema_filtro']}")]
    fig, ax = plt.subplots(figsize=(11, 6), dpi=110)
    fig.patch.set_facecolor(col["fondo"])
    ax.set_facecolor(col["panel"])
    for i in range(n):
        k = col["sube"] if c[i] >= o[i] else col["baja"]
        ax.vlines(i, l[i], h[i], color=k, linewidth=0.8)
        ax.add_patch(Rectangle((i - 0.3, min(o[i], c[i])), 0.6, max(abs(c[i] - o[i]), 1e-12), color=k, linewidth=0))
    for serie, k, nom in emas:
        ax.plot(range(n), serie.to_numpy(), color=k, linewidth=1.2, label=nom)
    xmax = n + 12
    for nom, y, k in (("Entrada", s["entrada"], "#58a6ff"), ("Stop", s["sl"], "#ef5350"),
                      ("TP1", s["tp1"], "#26a69a"), ("TP2", s["tp2"], "#26a69a")):
        ax.hlines(y, 0, xmax, colors=k, linewidth=1.3)
        ax.text(xmax - 0.5, y, f"{nom} {fmt(y)}", color=k, fontsize=8.5, ha="right", va="bottom", fontweight="bold")
    if s.get("rango"):
        lo_r, hi_r = s["rango"]
        kb = n - 1                                   # vela de ruptura (en el retest no es la ultima)
        if s.get("vela_ruptura") is not None and s["vela_ruptura"] in d.index:
            kb = int(d.index.get_loc(s["vela_ruptura"]))
        k0 = max(kb - CONFIG["rango_velas"], 0)
        ax.add_patch(Rectangle((k0 - 0.5, lo_r), kb - k0, hi_r - lo_r, facecolor="#f2cc60", alpha=0.10,
                               edgecolor="#f2cc60", linewidth=1.2, linestyle="--"))
        ax.text(k0, hi_r, " RANGO", color="#f2cc60", fontsize=8.5, va="bottom", fontweight="bold")
    ax.annotate("", xy=(n - 1, s["entrada"]), xytext=(n - 1, s["entrada"] - s["bias"] * s["riesgo_px"] * 1.2),
                arrowprops=dict(arrowstyle="->", color=col["sube"] if s["bias"] > 0 else col["baja"], lw=2))
    ys = [l.min(), h.max(), s["sl"], s["tp2"]]
    rg = max(ys) - min(ys)
    ax.set_ylim(min(ys) - 0.05 * rg, max(ys) + 0.05 * rg)
    ax.set_xlim(-1, xmax)
    paso = max(n // 6, 1)
    ax.set_xticks(range(0, n, paso))
    ax.set_xticklabels([t.tz_convert(NY).strftime("%d/%m %H:%M") for t in d.index[::paso]], fontsize=7.5)
    ax.tick_params(colors=col["texto"], labelsize=7.5)
    for sp in ax.spines.values():
        sp.set_color(col["rejilla"])
    ax.grid(color=col["rejilla"], linewidth=0.5)
    ax.yaxis.tick_right()
    que = {"ruptura": "ruptura de rango", "retest": "ruptura + retest", "orderblock": "order block",
           "confluencia": "confluencia 1H + 15m + 5m"}.get(
        s.get("estrategia"), f"cruce EMA {CONFIG['ema_rapida']}/{CONFIG['ema_lenta']}")
    if s.get("zona"):
        za, zb = s["zona"]
        kq = int(d.index.get_loc(s["vela_ruptura"])) if s.get("vela_ruptura") in d.index else max(n - 30, 0)
        ax.add_patch(Rectangle((kq - 0.5, za), n - kq, zb - za, facecolor="#26a69a" if s["bias"] > 0 else "#ef5350",
                               alpha=0.18, edgecolor="none"))
        ax.text(kq, zb if s["bias"] > 0 else za, " ORDER BLOCK", color="#e6edf3", fontsize=8.5,
                va="bottom" if s["bias"] > 0 else "top", fontweight="bold")
    if s.get("estrategia") == "retest":
        ax.hlines(s["nivel_ruptura"], 0, n - 1, colors="#f2cc60", linestyles=":", linewidth=1.4)
        ax.text(1, s["nivel_ruptura"], " nivel roto → retest", color="#f2cc60", fontsize=8.5,
                va="bottom" if s["bias"] > 0 else "top", fontweight="bold")
    ax.set_title(f"{'COMPRA' if s['bias'] > 0 else 'VENTA'} {s['activo']} · {s['tf']} · {que} a favor de la marea 1H",
                 color=col["texto"],
                 fontsize=11, loc="left")
    ax.legend(loc="upper left", fontsize=8, facecolor=col["panel"], edgecolor=col["rejilla"], labelcolor=col["texto"])
    buf = io.BytesIO()
    fig.tight_layout()
    fig.savefig(buf, format="png", facecolor=fig.get_facecolor())
    plt.close(fig)
    return buf.getvalue()


# ----------------------------------------------------------------- MEMORIA (anti-repeticion)
class Estado:
    def __init__(self, ruta="estado.json"):
        self.ruta, self.d = ruta, {"ids": [], "enviadas": {}}
        try:
            with open(ruta, encoding="utf-8") as f:
                self.d.update(json.load(f))
        except Exception:
            pass

    def guardar(self):
        self.d["ids"] = self.d["ids"][-500:]
        tmp = self.ruta + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(self.d, f, ensure_ascii=False)
        os.replace(tmp, self.ruta)

    def es_nueva(self, s):
        k = f"{s.get('estrategia', 'cruce')}|{s['activo']}|{s['tf']}|{s['dir']}"
        return s["id"] not in self.d["ids"] and time.time() - self.d["enviadas"].get(k, 0) >= CONFIG["pausa_min"] * 60

    def marcar(self, s):
        self.d["ids"].append(s["id"])
        self.d["enviadas"][f"{s.get('estrategia', 'cruce')}|{s['activo']}|{s['tf']}|{s['dir']}"] = time.time()


def escanear(estado, cargar=obtener, ahora=None, enviar=True, verbose=True):
    """Una pasada por todos los mercados y temporalidades. Devuelve el numero de alertas enviadas."""
    nuevas = 0
    for nombre in MERCADOS:
        for tf in CONFIG["tfs"]:
            try:
                senales, motivos = analizar_todas(nombre, tf, cargar, ahora)
            except Exception as e:
                senales, motivos = [], {"error": str(e)}
            if verbose:
                lista = [f"🚨 {s['estrategia']} {s['dir']}" for s in senales] or [f"{k}: {v}" for k, v in motivos.items()]
                print(f"  {nombre:<14} {tf:<4} " + " · ".join(lista), flush=True)
            for s in senales:
                nuevas += _enviar_senal(s, estado, cargar, ahora, enviar)
    estado.guardar()
    return nuevas


def _enviar_senal(s, estado, cargar, ahora, enviar):
    """Envia una senal nueva (texto + imagen). Devuelve 1 si salio, 0 si no."""
    nombre, tf = s["activo"], s["tf"]
    if not estado.es_nueva(s):
        return 0
    txt, png = texto_alerta(s), None
    if CONFIG["imagen"]:
        try:
            png = imagen_alerta(s, solo_cerradas(cargar(nombre, tf), tf, ahora))
        except Exception as e:
            print(f"  ! imagen {nombre}: {e}", flush=True)
    ok, msg = enviar_telegram(txt, png) if enviar else (True, "sin envio")
    if ok:
        estado.marcar(s)
        return 1
    print(f"  ! Alerta NO enviada {nombre} {tf}: {msg}", flush=True)
    return 0


# ----------------------------------------------------------------- BACKTEST
def simular(df, k, s):
    """Desde la vela k (entrada en su apertura): SL, TP1 2R (mitad + stop a la entrada), TP2 3R, cruce en contra
    o tiempo. Si SL y TP tocan en la misma vela, cuenta el SL. Devuelve (R_bruto, indice_salida, motivo, entrada)."""
    o, h, l, c = (df[x].to_numpy(float) for x in ("open", "high", "low", "close"))
    r_, l_ = ema(df["close"], CONFIG["ema_rapida"]).to_numpy(), ema(df["close"], CONFIG["ema_lenta"]).to_numpy()
    d, e = s["bias"], o[k]
    riesgo = s.get("riesgo_px") or abs(e - s["sl"])
    if riesgo <= 0 or (e - s["sl"]) * d < 0.25 * riesgo:
        return None                                    # la vela siguiente abre (casi) sobre el stop: no se entra
    stop, tp1, tp2 = s["sl"], e + d * CONFIG["tp1_r"] * riesgo, e + d * CONFIG["tp2_r"] * riesgo
    acum, resto = 0.0, 1.0
    for i in range(k, min(k + CONFIG["max_velas"], len(c))):
        if (l[i] <= stop) if d > 0 else (h[i] >= stop):
            return acum + resto * (stop - e) * d / riesgo, i, "SL" if resto == 1 else "BE", e
        if resto == 1 and ((h[i] >= tp1) if d > 0 else (l[i] <= tp1)):
            acum, resto, stop = 0.5 * CONFIG["tp1_r"], 0.5, e
        if (h[i] >= tp2) if d > 0 else (l[i] <= tp2):
            return acum + resto * CONFIG["tp2_r"], i, "TP2", e
        if s.get("salida_cruce", True) and (r_[i] - l_[i]) * d < 0:
            return acum + resto * (c[i] - e) * d / riesgo, i, "Cruce en contra", e
    i = min(k + CONFIG["max_velas"], len(c)) - 1
    return acum + resto * (c[i] - e) * d / riesgo, i, "Tiempo", e


def backtest_mercado(nombre, tf, df, d1, estrategia="cruce"):
    """Recorre las velas con la MISMA funcion senal_cruce del bot en vivo. Una operacion a la vez."""
    tipo = MERCADOS[nombre]["tipo"]
    ops, libre = [], 0
    cierre_1h = d1.index + pd.Timedelta(hours=1)
    for idx in range(250, len(df) - 1):
        if idx < libre:
            continue
        ahora = df.index[idx] + pd.Timedelta(minutes=TF_MIN[tf])
        if not mercado_abierto(tipo, ahora):
            continue
        n1 = int(cierre_1h.searchsorted(ahora, side="right"))
        s, _ = ESTRATEGIAS[estrategia](df.iloc[max(0, idx - 300):idx + 1], d1.iloc[max(0, n1 - 400):n1])
        if not s:
            continue
        r = simular(df, idx + 1, s)
        if r is None:
            continue
        rb, sal, motivo, e = r
        coste = 2 * COSTE[tipo] / 100 * e / (s.get("riesgo_px") or abs(e - s["sl"]))
        ops.append({"estrategia": estrategia, "activo": nombre, "tf": tf, "senal_NY": ahora.tz_convert(NY).strftime("%Y-%m-%d %H:%M"),
                    "dir": s["dir"], "entrada": e, "sl": s["sl"], "motivo": motivo, "R_bruto": round(rb, 3),
                    "coste_R": round(coste, 3), "R": round(rb - coste, 3)})
        libre = sal + 1
    return pd.DataFrame(ops)


def robustez(ops):
    filas = []
    if "estrategia" not in ops:
        ops = ops.assign(estrategia="cruce")
    for (est, a, tf), g in ops.groupby(["estrategia", "activo", "tf"]):
        R = g["R"].to_numpy()
        gan, per = R[R > 0].sum(), -R[R < 0].sum()
        mitad = len(g) // 2
        x2 = (g["R_bruto"] - 2 * g["coste_R"]).mean()
        filas.append({"Estrategia": est, "Mercado": a, "TF": tf, "Ops": len(g), "Aciertos%": round(100 * (R > 0).mean(), 1),
                      "R prom": round(R.mean(), 3), "R total": round(R.sum(), 1),
                      "PF": round(gan / per, 2) if per > 0 else np.inf, "Costes x2": round(x2, 3),
                      "1a mitad": round(R[:mitad].mean(), 3) if mitad else np.nan,
                      "2a mitad": round(R[mitad:].mean(), 3)})
    r = pd.DataFrame(filas)
    if not r.empty:
        r["ROBUSTA"] = ((r["Ops"] >= 30) & (r["R prom"] > 0) & (r["Costes x2"] > 0) & (r["1a mitad"] > 0)
                        & (r["2a mitad"] > 0)).map({True: "SI", False: ""})
    return r


def _csv(dir_, nombre, tf):
    """Velas de un CSV (p. ej. Dukascopy) en dir_/<nombre>_<tf>.csv, o None."""
    if not dir_:
        return None
    f = os.path.join(dir_, "".join(ch if ch.isalnum() else "_" for ch in nombre) + f"_{tf}.csv")
    if not os.path.exists(f):
        return None
    d = pd.read_csv(f, index_col=0)
    d.index = pd.to_datetime(d.index, utc=True)
    return d[["open", "high", "low", "close", "volume"]].astype(float)


def _a_1h(df):
    return df.resample("1h", label="left", closed="left").agg(
        {"open": "first", "high": "max", "low": "min", "close": "last", "volume": "sum"}).dropna()


def correr_backtest(a):
    todas, fuentes = [], {}
    desde = (pd.Timestamp.now("UTC") - pd.Timedelta(days=a.dias)).strftime("%Y-%m-%d")
    for nombre in (a.mercados or list(MERCADOS)):
        m = MERCADOS[nombre]
        base = next((x for x in (_csv(a.csv_dir, nombre, t) for t in ("5m", "15m")) if x is not None), None)
        d1 = velas_ccxt(m["ccxt"], "1h", desde) if m.get("ccxt") else None
        if d1 is None and base is not None:
            d1 = _a_1h(base)
        d1 = d1 if d1 is not None else velas_yf(m["yf"], "1h", desde)
        for tf in a.tf:
            df = velas_ccxt(m["ccxt"], tf, desde) if m.get("ccxt") else None
            fuente = "exchange" if df is not None else None
            if df is None:
                df = _csv(a.csv_dir, nombre, tf)
                fuente = "Dukascopy" if df is not None else None
            if df is None:
                df = velas_yf(m["yf"], tf, desde)
                fuente = "Yahoo (max ~59 dias)" if df is not None else None
            if df is not None:
                df = df[df.index >= pd.Timestamp(desde, tz="UTC")]
                fuentes[(nombre, tf)] = f"{fuente}: {df.index[0]:%Y-%m-%d} -> {df.index[-1]:%Y-%m-%d}" if len(df) else "-"
            if df is None or d1 is None or len(df) < 400:
                print(f"! Sin datos suficientes: {nombre} {tf}", flush=True)
                continue
            for est in CONFIG["estrategias"]:
                ops = backtest_mercado(nombre, tf, df, d1, est)
                print(f"{nombre} {tf} {est}: {len(df)} velas {df.index[0]:%Y-%m-%d} -> {df.index[-1]:%Y-%m-%d} · "
                      f"{len(ops)} operaciones", flush=True)
                if len(ops):
                    todas.append(ops)
    if not todas:
        print("Sin operaciones.")
        return None
    ops = pd.concat(todas, ignore_index=True)
    os.makedirs(a.salida, exist_ok=True)
    ops.to_csv(os.path.join(a.salida, "operaciones.csv"), index=False)
    r = robustez(ops)
    r.to_csv(os.path.join(a.salida, "robustez.csv"), index=False)
    pd.set_option("display.width", 220)
    print("\n=== Datos usados ===\n" + "\n".join(f"{k[0]} {k[1]}: {v}" for k, v in fuentes.items()))
    print("\n=== BALLENA EMAS · robustez ===\n" + r.to_string(index=False))
    return r


# ----------------------------------------------------------------- BUCLE
def hay_version_nueva(cada_min=10, _ult={"t": 0.0}):
    sha, repo, rama = os.environ.get("GITHUB_SHA"), os.environ.get("GITHUB_REPOSITORY"), os.environ.get("GITHUB_REF_NAME")
    if not (sha and repo and rama) or time.time() - _ult["t"] < cada_min * 60:
        return False
    _ult["t"] = time.time()
    try:
        r = requests.get(f"https://api.github.com/repos/{repo}/commits/{rama}",
                         headers={"Accept": "application/vnd.github.sha",
                                  **({"Authorization": f"Bearer {os.environ['GH_TOKEN']}"}
                                     if os.environ.get("GH_TOKEN") else {})}, timeout=10)
        return r.ok and len(r.text.strip()) == 40 and r.text.strip() != sha
    except Exception:
        return False


def correr_bot(a):
    estado = Estado(a.estado)
    t0 = time.time()
    if a.ping:
        ok, msg = enviar_telegram(f"✅ Ballena EMAs {VERSION} activa · {datetime.now(NY):%a %d/%m %H:%M} NY\n"
                                  f"{' + '.join(NOMBRE_EST[e] for e in CONFIG['estrategias'])} en "
                                  f"{' y '.join(CONFIG['tfs'])}, a favor de la marea de 1H (Triple Screen de Elder).\n"
                                  f"Mercados: {', '.join(MERCADOS)}")
        print(f"Ping: {msg}", flush=True)
    while True:
        print(f"\n[{datetime.now(NY):%Y-%m-%d %H:%M:%S} NY] Escaneando {len(MERCADOS)} mercados", flush=True)
        try:
            print(f"  -> {escanear(estado)} alertas enviadas", flush=True)
        except Exception as e:
            print(f"  ! Error en el escaneo: {e}", flush=True)
        if a.una_vez:
            break
        seg = min(TF_MIN[t] for t in CONFIG["tfs"]) * 60
        if a.minutos and time.time() - t0 + seg > a.minutos * 60:
            print("Fin del bucle: me relanzo.", flush=True)
            break
        if a.minutos and hay_version_nueva():
            print("Versión nueva en GitHub: me relanzo con ella.", flush=True)
            break
        time.sleep(seg - (time.time() % seg) + 20)


def cli():
    ap = argparse.ArgumentParser(description="Ballena EMAs: cruce de EMAs a favor de la tendencia de 1H")
    ap.add_argument("modo", nargs="?", default="bot", choices=["bot", "backtest"])
    ap.add_argument("--una-vez", action="store_true")
    ap.add_argument("--ping", action="store_true")
    ap.add_argument("--minutos", type=float, default=0, help="bucle durante N minutos (GitHub Actions)")
    ap.add_argument("--estado", default="estado.json")
    ap.add_argument("--tf", nargs="+", default=list(CONFIG["tfs"]), choices=["5m", "15m"])
    ap.add_argument("--mercados", nargs="*", default=[])
    ap.add_argument("--dias", type=int, default=58)
    ap.add_argument("--salida", default="resultados")
    ap.add_argument("--csv-dir", default=None, help="carpeta con CSV <mercado>_<tf>.csv (Dukascopy)")
    ap.add_argument("--avisar-estado", action="store_true", help="envia '✅ activo y analizando' a Telegram y termina")
    a = ap.parse_args()
    if a.avisar_estado:
        ok, msg = enviar_telegram(f"✅ ACTIVO Y ANALIZANDO · {VERSION}\nConsenso de {CONFIG['min_estrategias']}+ "
                                  f"estrategias en {' y '.join(CONFIG['tfs'])} · escaneo cada 5 minutos\n"
                                  f"Mercados: {', '.join(MERCADOS)}\nTe aviso en cuanto aparezca una señal.")
        print(f"Estado a Telegram: {'OK' if ok else msg}", flush=True)
        raise SystemExit(0 if ok else 1)
    if a.modo == "backtest":
        correr_backtest(a)
    else:
        correr_bot(a)


if __name__ == "__main__":
    cli()
