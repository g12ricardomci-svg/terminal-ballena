# -*- coding: utf-8 -*-
"""
BALLENA RIKI CRT - vela MADRE + vela de DIRECCION (% de cuerpo) que APRENDE SOLA
==================================================================================
La logica de tu indicador "CRT Madre · % de cuerpo", convertida en bot de Telegram:

  1) VELA MADRE: rango >= 0,8 ATR(14) y cuerpo >= 30 % de su rango.
  2) VELA SIGUIENTE (hija / direccion): se mide SOLO con cuerpos (las mechas no cuentan):
       DENTRO = % del cuerpo de la hija que esta dentro del cuerpo de la madre
       OCUPA  = % del cuerpo de la madre que tapa el cuerpo de la hija
       LADO   = dentro / sale por arriba / sale por abajo / por los dos lados
     -> 80 grupos (5 de Dentro x 4 de Ocupa x 4 lados). La hija debe ser <= 50 % del cuerpo de la madre.
  3) APRENDE de todo el historial: en cada grupo cuenta si 5 velas despues el precio cerro MAS ARRIBA o MAS
     ABAJO y lo compara con la BASE (todas las madres). El grupo da la direccion solo si se aleja de la base.
  4) RUPTURA: solo cuando una vela CIERRA CON CUERPO fuera del Max/Min de la madre, a favor de la direccion
     aprendida y de la temporalidad mayor (5m/15m -> H1, 1h -> H4; cierre sobre/bajo su EMA 21).
  5) OPERACION: entrada al cierre de la ruptura, SL en el 50 % de la madre, TP a R:R 1:1,5. Se descuenta el
     spread de Vantage (Oro 0,30 · BTC 20 · NAS100 1,35).
  6) MEMORIA DE ERRORES: cada operacion guarda su ambiente (sesion, volatilidad alta, hija con mecha fuera de
     la madre, madre gigante, mercado lateral). Si las perdidas se juntan en un ambiente en LAS DOS MITADES del
     historial, ese ambiente queda VETADO (trampa conocida).
  7) EXAMEN: aprende con el 75 % viejo y opera el 25 % nuevo sin haberlo visto. Solo los mercados y
     temporalidades que APRUEBAN (>= 30 operaciones, >= +0,15 R por operacion en el examen, gana en las dos
     partes y le gana a la base) mandan senales a Telegram. Se vuelve a entrenar cada dia.

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
VERSION = "v1 · madre + % de cuerpo · aprende sola"

# ----------------------------------------------------------------- CONFIGURACION (EDITA AQUI)
MERCADOS = {   # oro, BTC y Nasdaq 100 (nombre -> tipo, ticker de Yahoo, simbolo de exchange y spread de Vantage)
    "XAU/USD (Oro)": {"tipo": "oro", "yf": "GC=F", "ccxt": "PAXG/USDT", "spread": 0.30},
    "BTC/USD": {"tipo": "cripto", "yf": "BTC-USD", "ccxt": "BTC/USDT", "spread": 20.0},
    "NASDAQ 100": {"tipo": "indice", "yf": "NQ=F", "spread": 1.35},
}
CONFIG = {
    "temporalidades": {"5m": "1h", "15m": "1h", "1h": "4h"},   # temporalidad de entrada -> temporalidad mayor
    "madre_atr": 0.8,             # madre: rango >= 0,8 x ATR(14)
    "cuerpo_min": 0.30,           # madre: cuerpo >= 30 % de su rango (evita dojis grandes)
    "hija_max": 0.50,             # hija: cuerpo <= 50 % del cuerpo de la madre
    "velas_adelante": 5,          # direccion: cierre 5 velas despues de la hija
    "min_casos": 20,              # minimo de casos en un grupo para fiarse
    "ventaja": 0.03,              # el grupo debe alejarse >= 3 puntos % de la base para dar direccion
    "ventana_ruptura": 6,         # velas despues de la hija para que un cuerpo cierre fuera de la madre
    "rr": 1.5,                    # TP a R:R 1:1,5 (SL en el 50 % de la madre)
    "max_velas_op": 48,           # si no toca SL ni TP en 48 velas, se cierra al precio
    # TAKE PROFIT INTELIGENTE (como el indicador): hasta donde llego el precio en las 5 velas siguientes
    "alcance_tp": 60,             # TP = altura a la que llego el 60 % de los casos de su grupo
    "alcance_sl": 80,             # SL = solo el 20 % de los casos fue mas alla en contra
    "ema_mayor": 21,
    # examen y memoria de errores
    "parte_examen": 0.25, "min_ops": 30, "min_ops_examen": 8, "min_r_op": 0.15,
    "veto_min_ops": 8, "veto_r": -0.10,
    "dias_historial": 365, "reentrenar_horas": 24,
    "practica": os.environ.get("BALLENA_PRACTICA", "") == "1",   # 1 = tambien manda las senales en practica
    "pausa_min": 30,
    "tfs": ["5m"],                # el bucle pasa al cierre de cada vela de 5m (mira 5m, 15m y 1h)
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


RIKI_CHATS = "1496321437"   # Ballena Riki manda SOLO a este chat (las demas ballenas siguen con los suyos)


def _chats():
    ids = os.environ.get("RIKI_CHAT_IDS") or RIKI_CHATS
    return [i.strip() for i in ids.replace(";", ",").split(",") if i.strip()]


def _pausa_tg():
    espera = 1.1 - (time.time() - TG["ultimo"])
    if espera > 0:
        _dormir(espera)


ENCABEZADO = "🐋💜 BALLENA RIKI CRT"


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



# ----------------------------------------------------------------- ESTRATEGIA: MADRE + % DE CUERPO
NOMBRE_TF = {"5m": "5M", "15m": "15M", "1h": "1H", "4h": "H4"}
DENTRO_TXT = ["100%", "67-99%", "34-66%", "1-33%", "0%"]
OCUPA_TXT = ["0-24%", "25-49%", "50-74%", "75-100%"]
LADO_TXT = ["dentro", "sale por arriba", "sale por abajo", "sale por los dos lados"]
AMBIENTES = {"asia": "sesión Asia", "londres": "sesión Londres", "nuevayork": "sesión Nueva York",
             "fuera": "fuera de sesión", "vol_alta": "volatilidad alta", "mecha_fuera": "hija con mecha fuera de la madre",
             "gigante": "madre gigante (posible noticia)", "lateral": "mercado lateral"}


def medir_cuerpos(mo, mc, ho, hc):
    """% Dentro, % Ocupa y lado, solo con cuerpos (igual que el indicador de TradingView)."""
    mH, mL, cH, cL = max(mo, mc), min(mo, mc), max(ho, hc), min(ho, hc)
    m_cuerpo, c_cuerpo = mH - mL, cH - cL
    solape = max(0.0, min(mH, cH) - max(mL, cL))
    dentro = 100.0 * solape / c_cuerpo if c_cuerpo > 0 else (100.0 if mL <= cH <= mH else 0.0)
    ocupa = 100.0 * solape / m_cuerpo if m_cuerpo > 0 else 0.0
    lado = 3 if cH > mH and cL < mL else 1 if cH > mH else 2 if cL < mL else 0
    return dentro, ocupa, lado


def grupo(dentro, ocupa, lado):
    b_in = 0 if dentro >= 99.95 else 1 if dentro >= 66.67 else 2 if dentro >= 33.33 else 3 if dentro > 0.05 else 4
    b_oc = 0 if ocupa < 25 else 1 if ocupa < 50 else 2 if ocupa < 75 else 3
    return (b_in * 4 + b_oc) * 4 + lado


def nombre_grupo(g):
    return f"Dentro {DENTRO_TXT[g // 16]} · Ocupa {OCUPA_TXT[(g // 4) % 4]} · {LADO_TXT[g % 4]}"


def sesgo_mayor(df, tf):
    """+1 / -1 por vela: la ultima vela MAYOR CERRADA (H1 o H4) cierra sobre / bajo su EMA 21."""
    mayor = CONFIG["temporalidades"][tf]
    agg = {"open": "first", "high": "max", "low": "min", "close": "last"}
    h = df.resample(mayor, label="left", closed="left").agg(agg).dropna()
    s = np.sign(h["close"] - ema(h["close"], CONFIG["ema_mayor"]))
    cierre_h = pd.Series(s.to_numpy(), index=h.index + pd.Timedelta(minutes=TF_MIN[mayor]))
    cierre_v = df.index + pd.Timedelta(minutes=TF_MIN[tf])
    pos = cierre_h.index.searchsorted(cierre_v, side="right") - 1
    return np.where(pos >= 0, cierre_h.to_numpy()[np.clip(pos, 0, None)], 0).astype(int)


def _sesion(t):
    h = t.tz_convert(NY).hour
    return "asia" if (h >= 19 or h < 1) else "londres" if h < 7 else "nuevayork" if h < 13 else "fuera"


def eventos(df, tf, spread=0.0, desde=0):
    """Todas las madres -> hija -> ruptura con cuerpo. Devuelve una lista de casos (dict) con su resultado."""
    if df is None or len(df) < 40:
        return []
    o, h, l, c = (df[x].to_numpy(float) for x in ("open", "high", "low", "close"))
    n, idx = len(c), df.index
    a = atr(df).to_numpy()
    a_med = pd.Series(a).rolling(100, min_periods=20).median().to_numpy()
    cambio = np.abs(np.diff(c, prepend=c[0]))
    er = (np.abs(c - np.r_[np.full(20, np.nan), c[:-20]]) / pd.Series(cambio).rolling(20).sum().to_numpy())
    mayor = sesgo_mayor(df, tf)
    nf, rr, cf = CONFIG["velas_adelante"], CONFIG["rr"], CONFIG
    casos = []
    for m in range(max(15, desde), n - 1):
        rango, cuerpo = h[m] - l[m], abs(c[m] - o[m])
        if not (a[m] > 0 and rango >= cf["madre_atr"] * a[m] and rango > 0 and cuerpo >= cf["cuerpo_min"] * rango):
            continue
        d = m + 1
        dentro, ocupa, lado = medir_cuerpos(o[m], c[m], o[d], c[d])
        caso = {"m": m, "d": d, "g": grupo(dentro, ocupa, lado), "dentro": dentro, "ocupa": ocupa, "lado": lado,
                "fut": (int(np.sign(c[d + nf] - c[d])) if d + nf < n else None),
                "hija_ok": abs(c[d] - o[d]) <= cf["hija_max"] * cuerpo}
        j_rup, dir_ = None, 0
        for j in range(d + 1, min(d + 1 + cf["ventana_ruptura"], n)):
            if c[j] > h[m]:
                j_rup, dir_ = j, 1
                break
            if c[j] < l[m]:
                j_rup, dir_ = j, -1
                break
        if j_rup is not None and caso["hija_ok"]:
            px, sl = float(c[j_rup]), float((h[m] + l[m]) / 2)
            riesgo = (px - sl) * dir_
            if riesgo > 0:
                tp = px + dir_ * rr * riesgo
                r, salida = None, None
                for k in range(j_rup + 1, min(j_rup + 1 + cf["max_velas_op"], n)):
                    if (l[k] <= sl) if dir_ > 0 else (h[k] >= sl):
                        r, salida = -1.0, k
                        break
                    if (h[k] >= tp) if dir_ > 0 else (l[k] <= tp):
                        r, salida = rr, k
                        break
                else:
                    k = min(j_rup + cf["max_velas_op"], n - 1)
                    if j_rup + cf["max_velas_op"] < n:
                        r, salida = (c[k] - px) * dir_ / riesgo, k
                amb = [_sesion(idx[j_rup])]
                if a_med[j_rup] > 0 and a[j_rup] > 1.5 * a_med[j_rup]:
                    amb.append("vol_alta")
                if h[d] > h[m] or l[d] < l[m]:
                    amb.append("mecha_fuera")
                if rango > 2.5 * a[m]:
                    amb.append("gigante")
                if er[j_rup] < 0.25:
                    amb.append("lateral")
                fin = min(j_rup + nf, n - 1)
                if j_rup + nf < n and a[j_rup] > 0:                    # alcance en ATR en las N velas siguientes
                    fav = (h[j_rup + 1:fin + 1].max() - px) if dir_ > 0 else (px - l[j_rup + 1:fin + 1].min())
                    adv = (px - l[j_rup + 1:fin + 1].min()) if dir_ > 0 else (h[j_rup + 1:fin + 1].max() - px)
                    caso.update(fav=max(0.0, fav) / a[j_rup], adv=max(0.0, adv) / a[j_rup])
                caso.update(j=j_rup, atr=float(a[j_rup]), dir=dir_, mayor=int(mayor[j_rup]), entrada=px, sl=sl, tp=tp, riesgo=riesgo,
                            r=(None if r is None else r - spread / riesgo), salida=salida, amb=amb,
                            madre_h=float(h[m]), madre_l=float(l[m]), vela=idx[j_rup])
        casos.append(caso)
    return casos


# ----------------------------------------------------------------- APRENDIZAJE
def contar_grupos(casos, hasta=None):
    """{grupo: [subio, bajo]} y la base, solo con casos ya terminados antes de 'hasta'."""
    g, base = {}, [0, 0]
    for e in casos:
        if e["fut"] in (1, -1) and (hasta is None or e["d"] + CONFIG["velas_adelante"] < hasta):
            k = 0 if e["fut"] == 1 else 1
            g.setdefault(str(e["g"]), [0, 0])[k] += 1
            base[k] += 1
    return g, base


def direccion_grupo(g, grupos, base):
    """+1 si el grupo sube mas que la base, -1 si baja mas, 0 si no aporta o tiene pocos casos."""
    u, d = grupos.get(str(g), [0, 0])
    if u + d < CONFIG["min_casos"] or sum(base) == 0:
        return 0
    p, pb = u / (u + d), base[0] / sum(base)
    return 1 if p >= pb + CONFIG["ventaja"] else -1 if p <= pb - CONFIG["ventaja"] else 0


def _media(xs):
    return float(np.mean(xs)) if xs else 0.0


def trampas(ops):
    """Ambientes donde las perdidas se juntan en LAS DOS MITADES del periodo -> vetados."""
    if len(ops) < 2:
        return []
    ops = sorted(ops, key=lambda e: e["j"])
    mitades = (ops[:len(ops) // 2], ops[len(ops) // 2:])
    vetos = []
    for amb in AMBIENTES:
        rs = [[e["r"] for e in p if amb in e["amb"]] for p in mitades]
        if all(len(x) >= CONFIG["veto_min_ops"] and _media(x) <= CONFIG["veto_r"] for x in rs):
            vetos.append(amb)
    return vetos


def operables(casos, grupos, base, vetos, desde=None, hasta=None):
    """Rupturas que siguen la direccion aprendida y la temporalidad mayor, fuera de los ambientes vetados."""
    return [e for e in casos if e.get("r") is not None and (desde is None or e["j"] >= desde)
            and (hasta is None or e["salida"] < hasta) and e["mayor"] == e["dir"]
            and direccion_grupo(e["g"], grupos, base) == e["dir"] and not set(e["amb"]) & set(vetos)]


def alcances(casos):
    """Por grupo y direccion: hasta donde llego el precio (TP) y lo que casi nunca fue en contra (SL), en ATR."""
    bolsas = {}
    for e in casos:
        if "fav" in e and e["mayor"] == e["dir"]:
            for k in (f"{e['g']}|{e['dir']}", f"base|{e['dir']}"):
                bolsas.setdefault(k, ([], []))
                bolsas[k][0].append(e["fav"])
                bolsas[k][1].append(e["adv"])
    return {k: {"n": len(f), "tp": round(float(np.percentile(f, 100 - CONFIG["alcance_tp"])), 4),
                "sl": round(float(np.percentile(a, CONFIG["alcance_sl"])), 4)} for k, (f, a) in bolsas.items()}


def plan_inteligente(e, mod):
    """TP y SL inteligentes desde la entrada: su grupo si tiene casos suficientes, si no todas las rupturas."""
    al = mod.get("alcance") or {}
    x, fuente = al.get(f"{e['g']}|{e['dir']}"), "su grupo"
    if not x or x["n"] < CONFIG["min_casos"]:
        x, fuente = al.get(f"base|{e['dir']}"), "todas las rupturas"
    if not x or x["n"] < CONFIG["min_casos"] or x["tp"] <= 0:
        return None
    tp = e["entrada"] + e["dir"] * x["tp"] * e["atr"]
    sl = e["entrada"] - e["dir"] * x["sl"] * e["atr"]
    return {"tp": tp, "sl": sl, "n": x["n"], "fuente": fuente, "rr": x["tp"] / x["sl"] if x["sl"] > 0 else None}


def entrenar_df(df, tf, spread):
    """Examen 75/25 y modelo final con todo el historial. Devuelve el modelo (dict serializable)."""
    casos = eventos(df, tf, spread)
    n = len(df)
    corte = int(n * (1 - CONFIG["parte_examen"]))
    g_tr, b_tr = contar_grupos(casos, corte)
    sel_tr = operables(casos, g_tr, b_tr, [], hasta=corte)
    v_tr = trampas(sel_tr)
    fin_tr = operables(casos, g_tr, b_tr, v_tr, hasta=corte)
    fin_ex = operables(casos, g_tr, b_tr, v_tr, desde=corte)
    base_ex = [e for e in casos if e.get("r") is not None and e["j"] >= corte and e["mayor"] == e["dir"]]
    r_tr, r_ex, r_base = _media([e["r"] for e in fin_tr]), _media([e["r"] for e in fin_ex]), \
        _media([e["r"] for e in base_ex])
    aprobado = (len(fin_tr) + len(fin_ex) >= CONFIG["min_ops"] and len(fin_ex) >= CONFIG["min_ops_examen"]
                and r_ex >= CONFIG["min_r_op"] and r_tr > 0 and r_ex > r_base)
    grupos, base = contar_grupos(casos)                                  # modelo final: todo el historial
    vetos = trampas(operables(casos, grupos, base, []))
    return {"aprobado": bool(aprobado), "grupos": grupos, "base": base, "vetos": vetos, "alcance": alcances(casos),
            "examen": {"ops": len(fin_ex), "r_op": round(r_ex, 3), "ganadas": round(100 * _media(
                [1.0 if e["r"] > 0 else 0.0 for e in fin_ex]), 1), "base_r_op": round(r_base, 3)},
            "aprendizaje": {"ops": len(fin_tr), "r_op": round(r_tr, 3)},
            "velas": n, "desde": str(df.index[0]), "hasta": str(df.index[-1]), "t": time.time()}


def historial(nombre, tf, dias=None):
    """1 ano de velas: exchange (oro = PAXG, BTC) o Yahoo (Nasdaq; 5m/15m limitado a 60 dias por Yahoo)."""
    m = MERCADOS[nombre]
    desde = datetime.now(UTC) - timedelta(days=dias or CONFIG["dias_historial"])
    df = velas_ccxt(m["ccxt"], tf, desde=desde) if m.get("ccxt") else None
    if df is None or len(df) < 500:
        df = velas_yf(m["yf"], tf, desde=desde)
    return df


def entrenar(cargar_hist=historial, verbose=True):
    """Entrena los 3 mercados x 3 temporalidades. Devuelve {"mercado|tf": modelo}."""
    modelos = {}
    for nombre, meta in MERCADOS.items():
        for tf in CONFIG["temporalidades"]:
            try:
                df = cargar_hist(nombre, tf)
                if df is None or len(df) < 300:
                    raise ValueError("sin historial suficiente")
                mod = entrenar_df(solo_cerradas(df, tf), tf, meta["spread"])
            except Exception as e:
                mod = {"aprobado": False, "error": str(e), "grupos": {}, "base": [0, 0], "vetos": [], "t": time.time()}
            modelos[f"{nombre}|{tf}"] = mod
            if verbose:
                ex = mod.get("examen", {})
                print(f"  {nombre:<14} {tf:<4} {'✅ APRUEBA' if mod['aprobado'] else '🧪 práctica'} · "
                      f"examen {ex.get('ops', 0)} ops {ex.get('r_op', 0):+.2f} R/op (base {ex.get('base_r_op', 0):+.2f})"
                      f" · trampas: {', '.join(mod.get('vetos', [])) or 'ninguna'}"
                      + (f" · error: {mod['error']}" if mod.get("error") else ""), flush=True)
    return modelos


MODELOS = {}


def analizar_todas(nombre, tf=None, cargar=obtener, ahora=None):
    """En cada temporalidad (5m, 15m, 1h): ¿la ULTIMA vela cerrada es una ruptura operable?"""
    meta = MERCADOS[nombre]
    if not mercado_abierto(meta["tipo"], ahora):
        return [], {t: "mercado cerrado" for t in CONFIG["temporalidades"]}
    senales, motivos = [], {}
    for t in CONFIG["temporalidades"]:
        try:
            s, motivo = senal_riki(nombre, t, solo_cerradas(cargar(nombre, t), t, ahora))
        except Exception as e:
            s, motivo = None, f"error: {e}"
        motivos[NOMBRE_TF[t]] = motivo
        if s:
            senales.append(s)
    return senales, motivos


def senal_riki(nombre, tf, df):
    if df is None or len(df) < 60:
        return None, "sin datos"
    mod = MODELOS.get(f"{nombre}|{tf}")
    if not mod or not mod.get("grupos"):
        return None, "sin entrenar"
    df = df.tail(800)
    ult = len(df) - 1
    casos = [e for e in eventos(df, tf, MERCADOS[nombre]["spread"], desde=ult - CONFIG["ventana_ruptura"] - 2)
             if e.get("j") == ult]
    if not casos:
        return None, "esperando madre + ruptura con cuerpo"
    e = casos[-1]
    if e["mayor"] != e["dir"]:
        return None, "ruptura contra la temporalidad mayor"
    dg = direccion_grupo(e["g"], mod["grupos"], mod["base"])
    if dg != e["dir"]:
        return None, f"el grupo ({nombre_grupo(e['g'])}) no da esa dirección"
    if set(e["amb"]) & set(mod.get("vetos", [])):
        return None, f"trampa conocida: {', '.join(AMBIENTES[x] for x in set(e['amb']) & set(mod['vetos']))}"
    if not mod["aprobado"] and not CONFIG["practica"]:
        return None, "señal en práctica (no aprobó el examen)"
    u, d = mod["grupos"].get(str(e["g"]), [0, 0])
    return {**e, "activo": nombre, "tipo": MERCADOS[nombre]["tipo"], "tf": tf, "estrategia": f"riki_{tf}",
            "bias": e["dir"], "practica": not mod["aprobado"], "casos": u + d,
            "prob": round(100 * (u if e["dir"] > 0 else d) / max(u + d, 1)), "examen": mod.get("examen", {}),
            "plan": plan_inteligente(e, mod), "id": f"riki|{tf}|{nombre}|{e['dir']}|{e['vela']}"}, ""


# ----------------------------------------------------------------- MENSAJE
def imagen_alerta(s, df):
    return None


def texto_alerta(s):
    """Mensaje RESUMIDO en morado: direccion · madre y % de cuerpo · lo aprendido · entrada · SL · TP."""
    compra = s["dir"] > 0
    hora = pd.Timestamp(s["vela"]).tz_convert(NY) + pd.Timedelta(minutes=TF_MIN[s["tf"]])
    ex = s.get("examen", {})
    mayor = NOMBRE_TF[CONFIG["temporalidades"][s["tf"]]].replace("1H", "H1")
    return (f"🟣 {'🟢 COMPRA' if compra else '🔴 VENTA'} {s['activo']} · RIKI CRT {NOMBRE_TF[s['tf']]}"
            f"{' · 🧪 PRÁCTICA' if s.get('practica') else ''}\n"
            f"🕯 Madre {fmt(s['madre_l'])} – {fmt(s['madre_h'])} · hija Dentro {s['dentro']:.0f}% · Ocupa "
            f"{s['ocupa']:.0f}% · cuerpo cerró {'arriba' if compra else 'abajo'} · {mayor} a favor\n"
            f"🧠 Grupo {'▲' if compra else '▼'}{s['prob']}% ({s['casos']} casos) · examen "
            f"{ex.get('r_op', 0):+.2f} R/op ({ex.get('ops', 0)} ops)\n"
            f"🎯 Entrada: {fmt(s['entrada'])} ({hora:%H:%M} NY)\n"
            f"🛑 SL: {fmt(s['sl'])} (50% de la madre · {fmt(s['riesgo'])})\n"
            f"✅ TP: {fmt(s['tp'])} · R:R 1:{CONFIG['rr']:g} · {fmt(abs(s['tp'] - s['entrada']))}"
            + _linea_plan(s))


def _linea_plan(s):
    p = s.get("plan")
    if not p:
        return ""
    nv = CONFIG["velas_adelante"]
    return (f"\n📍 TP inteligente: {fmt(p['tp'])} (el {CONFIG['alcance_tp']}% de {p['fuente']} llegó hasta ahí en "
            f"{nv} velas · {p['n']} casos)\n"
            f"⚠️ SL inteligente: {fmt(p['sl'])} (solo el {100 - CONFIG['alcance_sl']}% fue más allá en contra)"
            + (f" · R:R 1:{p['rr']:.1f}" if p.get("rr") else ""))


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
    """Ejemplo de senal con el formato real, marcado como PRUEBA (no es una operacion)."""
    s = {"dir": 1, "activo": "NASDAQ 100", "tf": "5m", "madre_l": 21450.0, "madre_h": 21490.0, "dentro": 100,
         "ocupa": 32, "prob": 61, "casos": 148, "examen": {"r_op": 0.27, "ops": 41}, "entrada": 21496.25,
         "sl": 21470.0, "riesgo": 26.25, "tp": 21535.63, "vela": pd.Timestamp.now("UTC").floor("5min"),
         "plan": {"tp": 21528.50, "sl": 21478.75, "n": 96, "fuente": "su grupo", "rr": 1.8}}
    return "🧪 MENSAJE DE PRUEBA (no operar) · así llegarán las señales\n" + texto_alerta(s)


def resumen_modelos():
    if not MODELOS:
        return "🧠 Aprende con el último año al arrancar y se vuelve a examinar cada día."
    ok = [k.replace("|", " ") for k, m in MODELOS.items() if m.get("aprobado")]
    return ("✅ Aprueban el examen (mandan señales): " + (", ".join(ok) if ok else "ninguno por ahora") +
            "\n🧪 El resto queda en práctica y se vuelve a examinar cada día.")


def preparar_modelos(estado, forzar=False, cargar_hist=historial):
    """Carga los modelos guardados o entrena si no hay o tienen mas de 24 h."""
    global MODELOS
    MODELOS = estado.d.get("modelos") or {}
    viejo = time.time() - estado.d.get("modelos_t", 0) > CONFIG["reentrenar_horas"] * 3600 or any(
        m.get("grupos") and "alcance" not in m for m in MODELOS.values())       # modelo sin TP inteligente
    if forzar or not MODELOS or viejo:
        print(f"[{datetime.now(NY):%H:%M} NY] Entrenando con {CONFIG['dias_historial']} días de historial...", flush=True)
        MODELOS = entrenar(cargar_hist)
        estado.d["modelos"], estado.d["modelos_t"] = MODELOS, time.time()
        estado.guardar()
        return True
    return False


def correr_bot(a):
    estado = Estado(a.estado)
    t0 = time.time()
    preparar_modelos(estado)
    if a.ping:
        ok, msg = enviar_telegram(f"🟣 Ballena Riki CRT {VERSION} activa · {datetime.now(NY):%a %d/%m %H:%M} NY\n"
                                  f"Vela madre + % de cuerpo · ruptura con cuerpo · 5M, 15M y 1H · "
                                  f"R:R 1:{CONFIG['rr']:g}\nMercados: {', '.join(MERCADOS)}\n{resumen_modelos()}")
        print(f"Ping: {msg}", flush=True)
    while True:
        if preparar_modelos(estado):
            print("Modelos re-entrenados.", flush=True)
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
    ap = argparse.ArgumentParser(description="Ballena Riki CRT: vela madre + % de cuerpo, aprende sola, alertas a Telegram")
    ap.add_argument("modo", nargs="?", default="bot", choices=["bot", "entrenar"])
    ap.add_argument("--una-vez", action="store_true")
    ap.add_argument("--ping", action="store_true")
    ap.add_argument("--minutos", type=float, default=0, help="bucle durante N minutos (GitHub Actions)")
    ap.add_argument("--estado", default="estado.json")
    ap.add_argument("--prueba", action="store_true", help="envia un mensaje de prueba a Telegram y termina")
    ap.add_argument("--avisar-estado", action="store_true", help="envia '✅ activo y analizando' a Telegram y termina")
    a = ap.parse_args()
    if a.avisar_estado:
        global MODELOS
        MODELOS = Estado(a.estado).d.get("modelos") or {}
        ok, msg = enviar_telegram(f"✅ ACTIVO Y ANALIZANDO · {VERSION}\n5M, 15M y 1H · escaneo cada 5 minutos\n"
                                  f"Mercados: {', '.join(MERCADOS)}\n{resumen_modelos()}")
        print(f"Estado a Telegram: {'OK' if ok else msg}", flush=True)
        raise SystemExit(0 if ok else 1)
    if a.prueba:
        ok, msg = enviar_telegram(mensaje_prueba())
        print(f"Prueba de Telegram: {'OK' if ok else msg}", flush=True)
        raise SystemExit(0 if ok else 1)
    if a.modo == "entrenar":
        preparar_modelos(Estado(a.estado), forzar=True)
        return
    correr_bot(a)


if __name__ == "__main__":
    cli()
