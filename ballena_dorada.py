# -*- coding: utf-8 -*-
"""
BALLENA DORADA - el indicador "CRT Madre · % de cuerpo" (version completa) convertido en bot de Telegram
======================================================================================================
En cada vela de 5M, 15M y 1H de Oro, BTC y NASDAQ 100 (24/7):

  1) VELA MADRE: rango >= 1 x ATR(14) y cuerpo >= 30 % de su rango.
  2) VELA SIGUIENTE medida SOLO con cuerpos: DENTRO (% de su cuerpo dentro del de la madre), OCUPA (% del cuerpo
     de la madre que tapa) y LADO (dentro / sale por arriba / por abajo / por los dos).
  3) APRENDE del historial que carga (como TradingView): en cada grupo mira si 5 velas despues el precio cerro
     mas arriba o mas abajo y hasta donde subio y bajo (en ATR). Usa los casos con el MISMO CONTEXTO
     (tendencia por ADX + EMA 50 y volatilidad contra lo normal de ESE mercado).
  4) PLAN: direccion, TP (altura a la que llego el 60 % de los casos), SL (solo el 20 % fue mas alla), R:R y la
     prueba del plan en esos casos (gana / no operar), con el spread de Vantage descontado.
  5) 🎯 OBJETIVO cuando la senal sale con 60 % o mas.
  6) NOTICIAS: velas de 2,5 x ATR (⚡) y horarios de noticias de Nueva York (8:30, 10:00, 14:00).
  7) BUSCADOR DE PATRONES ★: 22 rasgos, combinaciones de 1 a 4 condiciones, confirmadas con el 30 % mas
     reciente. Si la ultima madre cumple uno, MANDA el patron (y avisa si su grupo decia lo contrario).

Sin examen que la frene: cada madre con direccion manda su mensaje. Herramienta educativa, no es asesoria.
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
VERSION = "v1 · CRT madre · % de cuerpo · patrones ★"

# ----------------------------------------------------------------- CONFIGURACION (EDITA AQUI)
MERCADOS = {   # oro, BTC y Nasdaq 100 (nombre -> tipo, ticker de Yahoo, simbolo de exchange y spread de Vantage)
    "XAU/USD (Oro)": {"tipo": "oro", "yf": "GC=F", "ccxt": "PAXG/USDT", "spread": 0.30},
    "BTC/USD": {"tipo": "cripto", "yf": "BTC-USD", "ccxt": "BTC/USDT", "spread": 20.0},
    "NASDAQ 100": {"tipo": "indice", "yf": "NQ=F", "spread": 1.35},
}
CONFIG = {
    "temporalidades": {"5m": "1h", "15m": "1h", "1h": "4h"},   # temporalidad -> vela mayor (CRT de la mayor)
    "dias_historial": {"5m": 30, "15m": 60, "1h": 365},       # historial que "carga" cada temporalidad
    "atr_madre": 1.0, "cuerpo_min": 30.0,       # 1) vela madre
    "velas_adelante": 5, "min_casos": 20,       # 2-3) direccion 5 velas despues · minimo de casos para fiarse
    "alcance_tp": 60, "alcance_sl": 80,         # 4) TP: 60 % llego · SL: solo el 20 % fue mas alla
    "objetivo_min": 60,                         # 5) 🎯 objetivo si el % es >= 60
    "adx_min": 20,                              # tendencia si ADX >= 20 (si no, LATERAL)
    "noticia_atr": 2.5, "horarios_noticias": [("08:25", "08:45"), ("09:55", "10:10"), ("13:55", "14:15")],
    # 7) buscador de patrones
    "patrones": True, "pat_min_casos": 30, "pat_min_pct": 60.0, "pat_min_prueba": 20, "pat_haz": 20,
    "pat_max_casos": 4500,
    # las madres 50/50 o con pocos casos no tienen plan y no mandan mensaje
    "pausa_min": 0,                             # cada madre manda su mensaje
    "tfs": ["5m"],                              # el bucle pasa al cierre de cada vela de 5m (mira 5m, 15m y 1h)
    "imagen": False,
}
COSTE = {"cripto": 0.10, "forex": 0.01, "oro": 0.02, "indice": 0.01}   # % por lado (spread + comision)
TF_MIN = {"1m": 1, "5m": 5, "15m": 15, "1h": 60, "4h": 240, "12h": 720, "1d": 1440}
YF_CFG = {"1m": ("1m", "5d"), "5m": ("5m", "10d"), "15m": ("15m", "30d"), "1h": ("1h", "60d"), "1d": ("1d", "1y")}
YF_LIMITE_DIAS = {"1m": 7, "5m": 59, "15m": 59, "1h": 729, "1d": 3650}
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


RIKI_CHATS = "1496321437"   # Ballena Dorada manda SOLO a este chat (el mismo que Ballena Riki)


def _chats():
    ids = os.environ.get("RIKI_CHAT_IDS") or RIKI_CHATS
    return [i.strip() for i in ids.replace(";", ",").split(",") if i.strip()]


def _pausa_tg():
    espera = 1.1 - (time.time() - TG["ultimo"])
    if espera > 0:
        _dormir(espera)


ENCABEZADO = "🐋🟡 BALLENA DORADA"


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
    """La cache vale mientras no haya cerrado una vela nueva de ese TF desde la descarga."""
    ahora = ahora or time.time()
    seg = TF_MIN.get(tf, 5) * 60
    return ahora - t_descarga < seg and int(ahora // seg) == int(t_descarga // seg)


def obtener(nombre, tf):
    """Velas recientes; H4 se construye desde H1. Cada TF se vuelve a descargar solo al cerrar una vela nueva."""
    if tf == "4h":
        h1 = obtener(nombre, "1h")
        return None if h1 is None else h1.resample("4h", label="left", closed="left").agg(
            {"open": "first", "high": "max", "low": "min", "close": "last", "volume": "sum"}).dropna()
    return _obtener_red(nombre, tf)


def _obtener_red(nombre, tf):
    """Velas recientes con cache de 2 minutos."""
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
    return f"{x:,.2f}" if abs(x) >= 100 else (f"{x:.3f}" if abs(x) >= 10 else f"{x:.5f}")



# ----------------------------------------------------------------- ESTRATEGIA: CRT MADRE · % DE CUERPO
NOMBRE_TF = {"5m": "5M", "15m": "15M", "1h": "1H", "4h": "H4"}
DENTRO_TXT = ["100%", "67-99%", "34-66%", "1-33%", "0%"]
OCUPA_TXT = ["0-24%", "25-49%", "50-74%", "75-100%"]
LADO_TXT = ["cuerpo dentro", "sale por arriba", "sale por abajo", "sale por los dos lados"]
TENDENCIA_TXT = ["LATERAL", "TENDENCIA ▲", "TENDENCIA ▼"]
VOL_TXT = ["BAJA", "NORMAL", "ALTA"]
RASGOS = ["grupo", "lado", "tendencia", "volatilidad", "mayor", "rango50", "barrido", "sesion", "venia", "madre_color",
          "previas", "color", "cierre_madre", "mecha", "tam_madre", "ema50", "rsi", "volumen", "crt_mayor", "fvg",
          "eficiencia", "nr7"]
CARD = [20, 4, 3, 3, 2, 3, 3, 4, 2, 2, 8, 2, 3, 3, 3, 3, 3, 3, 3, 3, 3, 2]
OFF = np.cumsum([0] + CARD[:-1])
NOMBRES_VALOR = {
    1: LADO_TXT, 2: ["lateral", "tendencia ▲", "tendencia ▼"], 3: ["volatilidad baja", "volatilidad normal", "volatilidad alta"],
    4: ["vela mayor ▼", "vela mayor ▲"], 5: ["abajo del rango", "en medio del rango", "arriba del rango"],
    6: ["sin barrido", "barrió el máximo", "barrió el mínimo"],
    7: ["sesión Asia", "sesión Londres", "sesión Nueva York", "cierre de NY"], 8: ["venía bajando", "venía subiendo"],
    9: ["madre bajista", "madre alcista"], 11: ["vela medida bajista", "vela medida alcista"],
    12: ["cierra abajo de la madre", "cierra en medio de la madre", "cierra arriba de la madre"],
    13: ["sin mecha larga", "mecha larga arriba (rechazo)", "mecha larga abajo (rechazo)"],
    14: ["madre normal", "madre grande (1,5-2,5 ATR)", "madre enorme (+2,5 ATR)"],
    15: ["muy debajo de la EMA 50", "cerca de la EMA 50", "muy encima de la EMA 50"],
    16: ["RSI bajo 30", "RSI normal", "RSI sobre 70"], 17: ["volumen bajo", "volumen normal", "volumen alto"],
    18: ["sin barrido de la vela mayor", "barrió el máximo de la vela mayor", "barrió el mínimo de la vela mayor"],
    19: ["sin hueco", "dejó hueco alcista (FVG)", "dejó hueco bajista (FVG)"],
    20: ["mercado caótico (eficiencia baja)", "eficiencia normal", "movimiento limpio (eficiencia alta)"],
    21: ["sin compresión", "compresión antes de la madre (NR7)"]}


def nombre_rasgo(f, v):
    if f == 0:
        return f"Dentro {DENTRO_TXT[v // 4]} · Ocupa {OCUPA_TXT[v % 4]}"
    if f == 10:
        return "3 velas antes: " + "".join("▲" if v & b else "▼" for b in (4, 2, 1))
    return NOMBRES_VALOR[f][v]


def _wilder(s, n):
    return s.ewm(alpha=1 / n, adjust=False).mean()


def rsi(c, n=14):
    d = c.diff()
    up, dn = _wilder(d.clip(lower=0), n), _wilder((-d).clip(lower=0), n)
    return 100 - 100 / (1 + up / dn.replace(0, np.nan))


def adx(df, n=14):
    h, l, c = df["high"], df["low"], df["close"]
    up, dn = h.diff(), -l.diff()
    mas = pd.Series(np.where((up > dn) & (up > 0), up, 0.0), index=df.index)
    menos = pd.Series(np.where((dn > up) & (dn > 0), dn, 0.0), index=df.index)
    tr = pd.concat([h - l, (h - c.shift()).abs(), (l - c.shift()).abs()], axis=1).max(axis=1)
    a = _wilder(tr, n)
    pdi, mdi = 100 * _wilder(mas, n) / a, 100 * _wilder(menos, n) / a
    return _wilder(100 * (pdi - mdi).abs() / (pdi + mdi).replace(0, np.nan), n)


def _vela_mayor_anterior(df, tf):
    """Para cada vela: la vela MAYOR anterior ya cerrada (no repinta): alcista?, maximo y minimo."""
    mayor = CONFIG["temporalidades"][tf]
    h = df.resample(mayor, label="left", closed="left").agg(
        {"open": "first", "high": "max", "low": "min", "close": "last"}).dropna().shift(1)
    pos = h.index.searchsorted(df.index, side="right") - 1
    ok = pos >= 0
    pos = np.clip(pos, 0, None)
    alc = np.where(ok, (h["close"] > h["open"]).to_numpy()[pos], False)
    return alc, np.where(ok, h["high"].to_numpy()[pos], np.nan), np.where(ok, h["low"].to_numpy()[pos], np.nan)


def preparar(df, tf):
    """Todas las columnas del indicador para cada vela i (la vela MEDIDA; la madre es i-1)."""
    o, h, l, c = (df[x] for x in ("open", "high", "low", "close"))
    v = df["volume"] if "volume" in df else pd.Series(1.0, index=df.index)
    a = atr(df)
    a1 = a.shift()
    mH, mL = np.maximum(o.shift(), c.shift()), np.minimum(o.shift(), c.shift())
    cH, cL = np.maximum(o, c), np.minimum(o, c)
    m_cuerpo, m_rango, c_cuerpo = mH - mL, h.shift() - l.shift(), cH - cL
    solape = (np.minimum(mH, cH) - np.maximum(mL, cL)).clip(lower=0)
    dentro = np.where(c_cuerpo > 0, 100 * solape / c_cuerpo.replace(0, np.nan), np.where((cH >= mL) & (cH <= mH), 100.0, 0.0))
    ocupa = np.where(m_cuerpo > 0, 100 * solape / m_cuerpo.replace(0, np.nan), 0.0)
    madre = ((m_cuerpo > 0) & (m_rango >= CONFIG["atr_madre"] * a1) & (m_cuerpo >= m_rango * CONFIG["cuerpo_min"] / 100)
             ).fillna(False).to_numpy()
    lado = np.where((cH > mH) & (cL < mL), 3, np.where(cH > mH, 1, np.where(cL < mL, 2, 0)))
    b_in = np.select([dentro >= 99.95, dentro >= 66.67, dentro >= 33.33, dentro > 0.05], [0, 1, 2, 3], 4)
    b_oc = np.select([ocupa < 25, ocupa < 50, ocupa < 75], [0, 1, 2], 3)
    g = b_in * 4 + b_oc
    e50 = ema(c, 50)
    ad = adx(df).fillna(0)
    tend = np.where(ad < CONFIG["adx_min"], 0, np.where(c > e50, 1, 2))
    vol_r = (a / a.rolling(100, min_periods=20).mean()).fillna(1.0)
    vola = np.where(vol_r < 0.8, 0, np.where(vol_r > 1.25, 2, 1))
    alc_m, hH, hL = _vela_mayor_anterior(df, tf)
    hi50, lo50 = h.rolling(50).max().shift(), l.rolling(50).min().shift()
    r_pos = ((c - lo50) / (hi50 - lo50).replace(0, np.nan)).fillna(0.5)
    hny = df.index.tz_convert(NY).hour
    m_r = (h.shift() - l.shift())
    c_pos = ((c - l.shift()) / m_r.replace(0, np.nan)).fillna(0.5)
    rng0, up_w, dn_w = h - l, h - cH, cL - l
    m_atr = (m_rango / a1.replace(0, np.nan)).fillna(1.0)
    e_dst = ((c - e50) / a.replace(0, np.nan)).fillna(0)
    rs = rsi(c).fillna(50)
    v_rat = (v / v.rolling(20).mean().replace(0, np.nan)).fillna(1.0)
    er_net = (c.shift() - c.shift(11)).abs()
    er_sum = c.diff().abs().rolling(10).sum().shift()
    er_v = (er_net / er_sum.replace(0, np.nan)).fillna(0)
    rngs = h - l
    X = np.column_stack([
        g, lado, tend, vola, alc_m.astype(int),
        np.where(r_pos < 0.33, 0, np.where(r_pos > 0.67, 2, 1)),
        np.where((h > h.shift()) & (c < h.shift()), 1, np.where((l < l.shift()) & (c > l.shift()), 2, 0)),
        np.where((hny >= 19) | (hny < 3), 0, np.where(hny < 8, 1, np.where(hny < 17, 2, 3))),
        (c.shift() > c.shift(4)).astype(int), (c.shift() > o.shift()).astype(int),
        (c.shift(2) > o.shift(2)).astype(int) + 2 * (c.shift(3) > o.shift(3)).astype(int)
        + 4 * (c.shift(4) > o.shift(4)).astype(int),
        (c > o).astype(int),
        np.where(c_pos < 0.33, 0, np.where(c_pos > 0.67, 2, 1)),
        np.where((rng0 > 0) & (up_w >= 0.5 * rng0) & (up_w >= 2 * c_cuerpo), 1,
                 np.where((rng0 > 0) & (dn_w >= 0.5 * rng0) & (dn_w >= 2 * c_cuerpo), 2, 0)),
        np.where(m_atr < 1.5, 0, np.where(m_atr < 2.5, 1, 2)),
        np.where(e_dst < -2, 0, np.where(e_dst > 2, 2, 1)),
        np.where(rs < 30, 0, np.where(rs > 70, 2, 1)),
        np.where(v_rat < 0.7, 0, np.where(v_rat > 1.5, 2, 1)),
        np.where((h > hH) & (c < hH), 1, np.where((l < hL) & (c > hL), 2, 0)),
        np.where(l > h.shift(2), 1, np.where(h < l.shift(2), 2, 0)),
        np.where(er_v < 0.3, 0, np.where(er_v > 0.6, 2, 1)),
        (rngs.shift(2) <= rngs.rolling(7).min().shift(2)).astype(int),
    ]).astype(int)
    # resultado de cada caso: hasta donde subio / bajo y donde cerro N velas despues (en ATR)
    n = CONFIG["velas_adelante"]
    hi_n = h[::-1].rolling(n).max()[::-1].shift(-1)
    lo_n = l[::-1].rolling(n).min()[::-1].shift(-1)
    a_v = a.replace(0, np.nan)
    sube = ((hi_n - c).clip(lower=0) / a_v).to_numpy()
    baja = ((c - lo_n).clip(lower=0) / a_v).to_numpy()
    cierre = ((c.shift(-n) - c) / a_v).to_numpy()
    sh_x = (rngs / a1.replace(0, np.nan)).fillna(0).to_numpy()
    return {"madre": madre, "X": X, "dentro": np.nan_to_num(dentro), "ocupa": np.nan_to_num(ocupa), "lado": lado,
            "sube": sube, "baja": baja, "cierre": cierre, "atr": a.to_numpy(), "c": c.to_numpy(),
            "tend": tend, "vola": vola, "vol_r": vol_r.to_numpy(), "adx": ad.to_numpy(), "hny": hny, "choque": sh_x,
            "idx": df.index, "fkey": g * 4 + lado}


def _rango_cercano(x, p):
    """percentile_nearest_rank de Pine."""
    s = np.sort(np.asarray(x, float))
    return float(s[max(int(np.ceil(p / 100 * len(s))) - 1, 0)])


def tp_sl(ups, dns, cls, d, atr_ahora, coste):
    """TP / SL (en ATR) y la prueba del plan en esos casos: gana si llega al TP sin tocar el SL."""
    if d == 0 or len(cls) == 0:
        return None
    fav, adv = (ups, dns) if d == 1 else (dns, ups)
    tp, sl = _rango_cercano(fav, 100 - CONFIG["alcance_tp"]), _rango_cercano(adv, CONFIG["alcance_sl"])
    if sl <= 0:
        return None
    ca = coste / atr_ahora if atr_ahora > 0 else 0.0
    r_a = np.where(adv >= sl, -sl, np.where(fav >= tp, tp, d * cls))
    gana = (fav >= tp) & (adv < sl)
    return {"tp": tp, "sl": sl, "ev": float(np.mean((r_a - ca) / sl)), "acierto": float(100 * gana.mean())}


def plan_grupo(P, casos, i, coste):
    """Direccion, % y TP/SL con los casos de su grupo y el MISMO contexto (como f_ctx del indicador)."""
    mc = CONFIG["min_casos"]
    fk, gk = P["fkey"][i], P["X"][i, 0]
    en_f = casos[(P["fkey"][casos] == fk)]
    usa_f = np.count_nonzero(np.sign(P["cierre"][en_f])) >= mc
    sel = en_f if usa_f else casos[P["X"][casos, 0] == gk]
    misma_t = sel[P["tend"][sel] == P["tend"][i]]
    misma_tv = misma_t[P["vola"][misma_t] == P["vola"][i]]
    nivel = 2 if len(misma_tv) >= mc else 1 if len(misma_t) >= mc else 0
    sel = [sel, misma_t, misma_tv][nivel] if len(sel) >= mc else sel
    cl = P["cierre"][sel]
    nu, nd = int((cl > 0).sum()), int((cl < 0).sum())
    d = (1 if nu > nd else -1) if len(sel) >= mc and nu != nd else 0
    pct = 100 * max(nu, nd) / (nu + nd) if nu + nd else 0.0
    plan = tp_sl(P["sube"][sel], P["baja"][sel], cl, d, P["atr"][i], coste) if d else None
    return {"dir": d, "pct": pct, "casos": len(sel), "nivel": nivel, "plan": plan}


NIVEL_TXT = ["todo su grupo", "misma tendencia", "misma tendencia y volatilidad"]


def _sc(u, d):
    n = u + d
    p = np.where(n > 0, np.maximum(u, d) / np.maximum(n, 1), 0.0)
    return np.where(n > 0, p - np.sqrt(p * (1 - p) / np.maximum(n, 1)), 0.0)


def buscar_patrones(P, casos):
    """22 rasgos · combinaciones de 1 y 2 condiciones y busqueda en haz a 3 y 4. Busca en el 70 % viejo y
    confirma con el 30 % reciente. Devuelve la lista de patrones confirmados (codigos, direccion, %, puntuacion)."""
    casos = casos[-CONFIG["pat_max_casos"]:]
    dd = np.sign(P["cierre"][casos])
    casos, dd = casos[dd != 0], dd[dd != 0]
    n = len(casos)
    if n < 2 * CONFIG["pat_min_casos"]:
        return [], 0
    codes = P["X"][casos] + OFF                                   # codigo global de cada rasgo
    M = np.zeros((n, int(sum(CARD))), dtype=np.int32)
    M[np.arange(n)[:, None], codes] = 1
    te = np.arange(n) >= int(n * 0.7)
    grupos = {"tu": M[~te & (dd > 0)], "td": M[~te & (dd < 0)], "eu": M[te & (dd > 0)], "ed": M[te & (dd < 0)]}
    cand = {}                                                     # tupla de codigos -> (tu, td, eu, ed)
    s = {k: v.sum(0) for k, v in grupos.items()}
    for cdg in np.nonzero(s["tu"] + s["td"] >= CONFIG["pat_min_casos"])[0]:
        cand[(int(cdg),)] = tuple(int(s[k][cdg]) for k in ("tu", "td", "eu", "ed"))
    pares = {k: v.T @ v for k, v in grupos.items()}
    tot = pares["tu"] + pares["td"]
    for c1, c2 in zip(*np.nonzero(np.triu(tot >= CONFIG["pat_min_casos"], 1))):
        cand[(int(c1), int(c2))] = tuple(int(pares[k][c1, c2]) for k in ("tu", "td", "eu", "ed"))
    rasgo_de = np.repeat(np.arange(len(CARD)), CARD)
    for nivel in (3, 4):
        prev = [k for k in cand if len(k) == nivel - 1]
        prev.sort(key=lambda k: -float(_sc(np.array(cand[k][0]), np.array(cand[k][1]))))
        for k in prev[:CONFIG["pat_haz"]]:
            mask = M[:, list(k)].all(1)
            usados = set(rasgo_de[list(k)])
            cu = {g: M[mask & sel].sum(0) for g, sel in
                  (("tu", ~te & (dd > 0)), ("td", ~te & (dd < 0)), ("eu", te & (dd > 0)), ("ed", te & (dd < 0)))}
            for c3 in np.nonzero(cu["tu"] + cu["td"] >= CONFIG["pat_min_casos"])[0]:
                if rasgo_de[c3] not in usados:
                    cand.setdefault(tuple(sorted(k + (int(c3),))), tuple(int(cu[g][c3]) for g in ("tu", "td", "eu", "ed")))
    confirmados = []
    for k, (u1, d1, u2, d2) in cand.items():
        dr = 1 if u1 >= d1 else -1
        n1, n2 = u1 + d1, u2 + d2
        p2 = (u2 if dr > 0 else d2) / n2 if n2 else 0.0
        pa = (u1 + u2 if dr > 0 else d1 + d2) / (n1 + n2)
        if n2 >= CONFIG["pat_min_prueba"] and p2 >= CONFIG["pat_min_pct"] / 100 and pa >= CONFIG["pat_min_pct"] / 100:
            lcb = pa - 1.96 * np.sqrt(pa * (1 - pa) / (n1 + n2))
            lcb2 = p2 - 1.96 * np.sqrt(p2 * (1 - p2) / n2)
            confirmados.append({"codigos": k, "dir": dr, "pct": 100 * pa, "prueba": 100 * p2, "n": n1 + n2,
                                "n_prueba": n2, "score": 1 + min(lcb, lcb2)})
    confirmados.sort(key=lambda p: -p["score"])
    return confirmados, len(cand)


def nombre_patron(codigos):
    rasgo_de = np.repeat(np.arange(len(CARD)), CARD)
    return " + ".join(nombre_rasgo(int(rasgo_de[c]), int(c - OFF[rasgo_de[c]])) for c in codigos)


def noticia(P, i, tf):
    """Huella de noticia: vela >= 2,5 ATR (hasta 4h) en las ultimas 3 velas o horario de noticias NY (hasta 1h)."""
    if TF_MIN[tf] > 240:
        return ""
    for k in range(max(i - 3, 0), i + 1):
        if P["choque"][k] >= CONFIG["noticia_atr"]:
            return (f"⚡ posible noticia {'ahora' if k == i else f'hace {i - k} velas'} "
                    f"(vela {P['choque'][k]:.1f}× ATR)")
    if TF_MIN[tf] <= 60:
        t = P["idx"][i].tz_convert(NY)
        hm = f"{t:%H:%M}"
        if any(a <= hm <= b for a, b in CONFIG["horarios_noticias"]):
            return "⏰ horario de noticias"
    return ""


def senal_dorada(nombre, tf, df):
    """La ULTIMA vela cerrada: si es la vela siguiente a una madre, arma el plan completo del indicador."""
    if df is None or len(df) < 150:
        return None, "sin datos suficientes"
    P = preparar(df, tf)
    i = len(df) - 1
    if not P["madre"][i]:
        return None, "la vela anterior no es madre"
    nv = CONFIG["velas_adelante"]
    casos = np.nonzero(P["madre"][:i - nv + 1])[0]               # solo casos ya terminados (no mira el futuro)
    casos = casos[np.isfinite(P["cierre"][casos]) & np.isfinite(P["sube"][casos])]
    coste = MERCADOS[nombre]["spread"]
    gp = plan_grupo(P, casos, i, coste)
    pat, probados, conflicto = None, 0, ""
    if CONFIG["patrones"]:
        confirmados, probados = buscar_patrones(P, casos)
        cod = P["X"][i] + OFF
        pat = next((p for p in confirmados if all(c in cod for c in p["codigos"])), None)
    if pat:
        match = casos[np.isin(P["X"][casos] + OFF, pat["codigos"]).sum(1) >= len(pat["codigos"])]
        plan = tp_sl(P["sube"][match], P["baja"][match], P["cierre"][match], pat["dir"], P["atr"][i], coste)
        if plan:
            if gp["dir"] and gp["dir"] != pat["dir"]:
                conflicto = (f"⚔ SEÑALES EN CONFLICTO: su grupo decía {'▲ SUBE' if gp['dir'] > 0 else '▼ BAJA'} "
                             f"{gp['pct']:.0f}% ({gp['casos']} casos), manda el ★ patrón")
            d, pct, n_c, fuente = pat["dir"], pat["pct"], pat["n"], "★ patrón"
        else:
            pat = None
    if not pat:
        d, pct, n_c, plan, fuente = gp["dir"], gp["pct"], gp["casos"], gp["plan"], NIVEL_TXT[gp["nivel"]]
    if d == 0 or not plan:
        return None, f"madre sin dirección clara ({gp['casos']} casos, 50/50 o pocos)"
    px, a = float(P["c"][i]), float(P["atr"][i])
    tp, sl = px + d * plan["tp"] * a, px - d * plan["sl"] * a
    s = {"activo": nombre, "tipo": MERCADOS[nombre]["tipo"], "tf": tf, "estrategia": f"dorada_{tf}", "dir": d,
         "bias": d, "pct": pct, "casos": n_c, "fuente": fuente, "entrada": px, "tp": tp, "sl": sl,
         "rr": plan["tp"] / plan["sl"], "ev": plan["ev"], "acierto": plan["acierto"], "dentro": float(P["dentro"][i]),
         "ocupa": float(P["ocupa"][i]), "lado": int(P["lado"][i]), "patron": pat and nombre_patron(pat["codigos"]),
         "patron_prueba": pat and pat["prueba"], "probados": probados, "conflicto": conflicto,
         "noticia": noticia(P, i, tf), "tendencia": TENDENCIA_TXT[P["tend"][i]], "vola": VOL_TXT[P["vola"][i]],
         "vol_r": float(P["vol_r"][i]), "adx": float(P["adx"][i]), "vela": P["idx"][i],
         "id": f"dorada|{tf}|{nombre}|{P['idx'][i]}"}
    return s, ""


# ----------------------------------------------------------------- DATOS (historial + velas recientes)
_HIST = {}


def historial(nombre, tf):
    """El historial que 'carga' cada temporalidad (como TradingView), refrescado cada hora y unido a lo reciente."""
    v = _HIST.get((nombre, tf))
    if v is None or time.time() - v[0] > 3600:
        m = MERCADOS[nombre]
        desde = datetime.now(UTC) - timedelta(days=CONFIG["dias_historial"][tf])
        df = velas_ccxt(m["ccxt"], tf, desde=desde) if m.get("ccxt") else None
        if df is None or len(df) < 300:
            df = velas_yf(m["yf"], tf, desde=desde)
        if df is not None:
            _HIST[(nombre, tf)] = v = (time.time(), df)
    base = v[1] if v else None
    rec = obtener(nombre, tf)
    if base is None:
        return rec
    if rec is None:
        return base
    d = pd.concat([base, rec])
    return d[~d.index.duplicated(keep="last")].sort_index()


def analizar_todas(nombre, tf=None, cargar=historial, ahora=None):
    meta = MERCADOS[nombre]
    if not mercado_abierto(meta["tipo"], ahora):
        return [], {t: "mercado cerrado" for t in CONFIG["temporalidades"]}
    senales, motivos = [], {}
    for t in CONFIG["temporalidades"]:
        try:
            s, motivo = senal_dorada(nombre, t, solo_cerradas(cargar(nombre, t), t, ahora))
        except Exception as e:
            s, motivo = None, f"error: {e}"
        motivos[NOMBRE_TF[t]] = motivo
        if s:
            senales.append(s)
    return senales, motivos


# ----------------------------------------------------------------- MENSAJE
def imagen_alerta(s, df):
    return None


def texto_alerta(s):
    compra = s["dir"] > 0
    hora = pd.Timestamp(s["vela"]).tz_convert(NY) + pd.Timedelta(minutes=TF_MIN[s["tf"]])
    nv, flecha = CONFIG["velas_adelante"], "▲" if compra else "▼"
    fuerte = s["pct"] >= CONFIG["objetivo_min"]
    lineas = [
        f"🟡 {'🟢 COMPRA' if compra else '🔴 VENTA'} {s['activo']} · DORADA {NOMBRE_TF[s['tf']]} · "
        f"{flecha}{s['pct']:.0f}%{' ★' if s.get('patron') else ''}",
        f"🕯 Dentro {s['dentro']:.0f}% · Ocupa {s['ocupa']:.0f}% · {LADO_TXT[s['lado']]} · {s['casos']} casos "
        f"({s['fuente']})",
        f"🎯 Entrada: {fmt(s['entrada'])} ({hora:%H:%M} NY)",
        (f"🎯 OBJETIVO: {fmt(s['tp'])} · lo alcanzó el {CONFIG['alcance_tp']}% de los casos en {nv} velas" if fuerte else
         f"✅ TP: {fmt(s['tp'])} · el {CONFIG['alcance_tp']}% de los casos llegó en {nv} velas"),
        f"🛑 SL: {fmt(s['sl'])} · solo el {100 - CONFIG['alcance_sl']}% fue más allá · R:R 1:{s['rr']:.1f}",
        (f"✓ GANA en el historial: {s['ev']:+.2f} R por operación · acierta {s['acierto']:.0f}%" if s["ev"] > 0 else
         f"⚠ NO OPERAR: en el historial pierde {s['ev']:.2f} R por operación · acierta {s['acierto']:.0f}%"),
    ]
    if s.get("patron"):
        lineas.append(f"★ Patrón: {s['patron']} (prueba reciente {s['patron_prueba']:.0f}%)")
    if s.get("conflicto"):
        lineas.append(s["conflicto"])
    if s.get("noticia"):
        lineas.append(f"⚡ NOTICIA: mejor NO operar ahora ({s['noticia']})")
    lineas.append(f"📊 {s['tendencia']} (ADX {s['adx']:.0f}) · volatilidad {s['vola']} ({s['vol_r']:.1f}× lo normal)")
    return "\n".join(lineas)


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


def mensaje_prueba():
    s = {"activo": "XAU/USD (Oro)", "tf": "15m", "dir": 1, "pct": 63.0, "casos": 142, "fuente": "misma tendencia",
         "entrada": 2650.40, "tp": 2658.90, "sl": 2645.10, "rr": 1.6, "ev": 0.12, "acierto": 52.0, "dentro": 100.0,
         "ocupa": 40.0, "lado": 0, "patron": None, "conflicto": "", "noticia": "", "tendencia": "TENDENCIA ▲",
         "vola": "NORMAL", "vol_r": 1.0, "adx": 26.0, "vela": pd.Timestamp.now("UTC").floor("15min")}
    return "🧪 MENSAJE DE PRUEBA (no operar) · así llegarán las señales\n" + texto_alerta(s)


def diagnostico(n_velas=12):
    """Sin enviar nada: datos que llegan y que dice el bot en las ultimas velas de cada mercado y temporalidad."""
    for nombre in MERCADOS:
        for tf in CONFIG["temporalidades"]:
            try:
                df = solo_cerradas(historial(nombre, tf), tf)
                if df is None or df.empty:
                    print(f"{nombre:<14} {tf:<4} SIN DATOS", flush=True)
                    continue
                print(f"{nombre:<14} {tf:<4} {len(df)} velas · {df.index[0]:%m-%d %H:%M} -> {df.index[-1]:%m-%d %H:%M} UTC",
                      flush=True)
                for k in range(n_velas, 0, -1):
                    s, m = senal_dorada(nombre, tf, df.iloc[:len(df) - k + 1])
                    print(f"    {df.index[-k]:%m-%d %H:%M} " + (f"SEÑAL {'COMPRA' if s['dir'] > 0 else 'VENTA'} "
                          f"{s['pct']:.0f}%" if s else m), flush=True)
            except Exception as e:
                print(f"{nombre:<14} {tf:<4} ERROR {type(e).__name__}: {e}", flush=True)


def correr_bot(a):
    estado = Estado(a.estado)
    t0 = time.time()
    if a.ping:
        ok, msg = enviar_telegram(f"🟡 Ballena Dorada {VERSION} activa · {datetime.now(NY):%a %d/%m %H:%M} NY\n"
                                  f"CRT madre · % de cuerpo · TP inteligente · patrones ★ · 5M, 15M y 1H · 24/7\n"
                                  f"Mercados: {', '.join(MERCADOS)}")
        print(f"Ping: {msg}", flush=True)
    while True:
        print(f"\n[{datetime.now(NY):%Y-%m-%d %H:%M:%S} NY] Escaneando {len(MERCADOS)} mercados", flush=True)
        try:
            print(f"  -> {escanear(estado, historial)} alertas enviadas", flush=True)
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
    ap = argparse.ArgumentParser(description="Ballena Dorada: CRT madre · % de cuerpo, alertas a Telegram 24/7")
    ap.add_argument("modo", nargs="?", default="bot", choices=["bot"])
    ap.add_argument("--una-vez", action="store_true")
    ap.add_argument("--ping", action="store_true")
    ap.add_argument("--minutos", type=float, default=0, help="bucle durante N minutos (GitHub Actions)")
    ap.add_argument("--estado", default="estado.json")
    ap.add_argument("--prueba", action="store_true", help="envia un mensaje de prueba a Telegram y termina")
    ap.add_argument("--avisar-estado", action="store_true", help="envia '✅ activo y analizando' a Telegram y termina")
    ap.add_argument("--diagnostico", action="store_true", help="analiza las ultimas velas sin enviar nada y lo explica")
    a = ap.parse_args()
    if a.avisar_estado:
        ok, msg = enviar_telegram(f"✅ ACTIVO Y ANALIZANDO · {VERSION}\n5M, 15M y 1H · 24/7 · escaneo cada 5 minutos\n"
                                  f"Mercados: {', '.join(MERCADOS)}\nTe aviso en cada vela madre con dirección.")
        print(f"Estado a Telegram: {'OK' if ok else msg}", flush=True)
        raise SystemExit(0 if ok else 1)
    if a.diagnostico:
        diagnostico()
        return
    if a.prueba:
        ok, msg = enviar_telegram(mensaje_prueba())
        print(f"Prueba de Telegram: {'OK' if ok else msg}", flush=True)
        raise SystemExit(0 if ok else 1)
    correr_bot(a)


if __name__ == "__main__":
    cli()
