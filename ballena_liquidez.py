# -*- coding: utf-8 -*-
"""
BALLENA LIQUIDEZ - sistema mecanico de liquidez (PDH / PDL, modelo AMD) en 5 minutos
====================================================================================
  1) TENDENCIA DIARIA: estructura de los 2 ultimos dias cerrados (HH+HL alcista, LH+LL bajista) o, si es mixta,
     la direccion de los cierres diarios.
  2) LIQUIDEZ DEL DIA ANTERIOR: tendencia bajista -> se espera que hoy el precio SUPERE el PDH (buy side liquidity);
     tendencia alcista -> que BARRA el PDL (sell side liquidity). Sin toma de liquidez no hay operacion ese dia.
  3) CONFIRMACION EN 5M: linea de tendencia que une los minimos ascendentes de la subida (en ventas); la ULTIMA vela
     cierra bajo la linea y bajo el ultimo minimo estructural -> ENTRADA.
  4) Stop sobre el maximo del barrido · TP = siguiente liquidez (minimo reciente o PDL) con R:R >= 1:1,5.
  5) Una senal por mercado y dia.

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
VERSION = "v2 · PDH/PDL · confluencia 1H + 15M · 3 mercados"

# ----------------------------------------------------------------- CONFIGURACION (EDITA AQUI)
MERCADOS = {   # solo 3 mercados: oro, BTC y Nasdaq 100 (nombre -> tipo, ticker de Yahoo y simbolo de exchange)
    "XAU/USD (Oro)": {"tipo": "oro", "yf": "GC=F", "ccxt": "PAXG/USDT"},
    "BTC/USD": {"tipo": "cripto", "yf": "BTC-USD", "ccxt": "BTC/USDT"},
    "NASDAQ 100": {"tipo": "indice", "yf": "NQ=F"},
}
CONFIG = {
    "rr_min": 1.5,                # R:R minimo hasta la siguiente liquidez (lo ideal: 1:2 o mas)
    "confluencia_1h_15m": False,  # True = ademas de la entrada en 5M: 1H rechaza el PDH/PDL y 15M rompe estructura
    "exigir_estructura": True,    # ademas de la linea, cerrar bajo el ultimo minimo estructural (mas seguro)
    "velas_subida": 36,           # velas de 5m antes del barrido donde buscar los minimos de la subida
    "swing_k": 2,                 # swings de 5m
    "pausa_min": 60,
    "banco": 50.0, "riesgo_pct": 1.0,
    "tfs": ["5m"],                # escanea al cierre de cada vela de 5m
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


def _chats():
    return [i.strip() for i in secreto("TELEGRAM_CHAT_IDS").replace(";", ",").split(",") if i.strip()]


def _pausa_tg():
    espera = 1.1 - (time.time() - TG["ultimo"])
    if espera > 0:
        _dormir(espera)


ENCABEZADO = "💧🔁 BALLENA LIQUIDEZ"


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



# ----------------------------------------------------------------- ESTRATEGIA: LIQUIDEZ PDH / PDL (AMD)
def tendencia_diaria(d1):
    """+1 alcista / -1 bajista / 0 sin tendencia, con los 3 ultimos dias CERRADOS:
    estructura de los 2 ultimos (HH+HL alcista, LH+LL bajista); si es mixta, la direccion de los cierres diarios
    (solo si los dos ultimos cierres van en el mismo sentido)."""
    if d1 is None or len(d1) < 3:
        return 0, "sin datos diarios"
    h, l, c = (d1[x].to_numpy(float)[-3:] for x in ("high", "low", "close"))
    if h[2] > h[1] and l[2] > l[1]:
        return 1, "estructura diaria alcista (HH + HL)"
    if h[2] < h[1] and l[2] < l[1]:
        return -1, "estructura diaria bajista (LH + LL)"
    if c[2] > c[1] > c[0]:
        return 1, "cierres diarios alcistas"
    if c[2] < c[1] < c[0]:
        return -1, "cierres diarios bajistas"
    return 0, "diario sin tendencia clara"


def _espejo(df):
    return pd.DataFrame({"open": -df["open"], "high": -df["low"], "low": -df["high"], "close": -df["close"]},
                        index=df.index)


def _swings_bajos(l, desde, hasta, k):
    return [j for j in range(max(desde, k), hasta - k + 1) if j + k < len(l) and l[j] == l[j - k:j + k + 1].min()]


def _venta_liquidez(m5, ini_dia, pdh, pdl, sgn=1):
    """VENTA (las compras usan el espejo; sgn=-1 solo cambia los textos): hoy el precio barrio el PDH; en M5 la
    linea de tendencia de los minimos de la subida se rompe y la ULTIMA vela cierra bajo la linea y bajo el
    ultimo minimo estructural (primera vez). Stop sobre el barrido; TP = siguiente liquidez con R:R >= minimo."""
    nivel_txt, ext = ("PDH", "máximo") if sgn > 0 else ("PDL", "mínimo")
    o, h, l, c = (m5[x].to_numpy(float) for x in ("open", "high", "low", "close"))
    ult = len(c) - 1
    desde = int(m5.index.searchsorted(ini_dia))
    if desde > ult - 3:
        return None, "esperando velas de M5 del día en curso"
    hoy_h = h[desde:]
    if hoy_h.max() <= pdh:
        return None, f"sin toma de liquidez: hoy aún no superó el {nivel_txt} {fmt(pdh * sgn)}"
    barrido = desde + int(np.argmax(hoy_h))                         # vela del maximo del barrido
    if ult <= barrido + 1:
        return None, f"barrió el {nivel_txt} {fmt(pdh * sgn)}: esperando la ruptura de la línea de tendencia"
    k = CONFIG["swing_k"]
    bajos = _swings_bajos(l, barrido - CONFIG["velas_subida"], barrido, k)
    subida = []                                                     # minimos ascendentes de la subida
    for j in reversed(bajos):
        if not subida or l[j] < l[subida[0]]:
            subida.insert(0, j)
    if len(subida) < 2:
        return None, "sin línea de tendencia (faltan mínimos ascendentes en la subida)"
    j1, j2 = subida[-2], subida[-1]
    pend = (l[j2] - l[j1]) / (j2 - j1)
    linea = lambda x: l[j2] + pend * (x - j2)
    minimo_estructural = float(l[j2])
    def roto(x):
        abajo = c[x] < linea(x)
        return abajo and (c[x] < minimo_estructural if CONFIG["exigir_estructura"] else True)
    if not roto(ult):
        return None, (f"barrió el {nivel_txt} {fmt(pdh * sgn)}: esperando cierre M5 "
                      f"{'bajo' if sgn > 0 else 'sobre'} la línea y el {'mínimo' if sgn > 0 else 'máximo'} "
                      f"{fmt(minimo_estructural * sgn)}")
    if any(roto(x) for x in range(barrido + 1, ult)):
        return None, "la ruptura ya se dio antes (señal pasada)"
    if not c[ult] < o[ult]:
        return None, "ruptura sin vela con cuerpo a favor"
    a = float(atr(m5).iloc[-1])
    px, tope = float(c[ult]), float(h[barrido:].max())
    dist = (tope + 0.1 * a) - px
    if dist <= 0:
        return None, "stop inválido"
    # siguiente liquidez: minimos recientes por debajo (de mas cerca a mas lejos) y el PDL
    objetivos = sorted({float(l[j]) for j in _swings_bajos(l, desde - 60, ult, k) if l[j] < px} | {float(pdl)},
                       reverse=True)
    tp = next((t for t in objetivos if (px - t) / dist >= CONFIG["rr_min"]), None)
    if tp is None:
        return None, f"ruptura confirmada pero sin liquidez a R:R >= 1:{CONFIG['rr_min']:g} (se descarta)"
    tp2 = min(pdl, tp) if pdl < px else tp
    return {"bias": -1, "dir": "SHORT", "entrada": px, "sl": px + dist, "tp1": tp, "tp2": tp2, "riesgo_px": dist,
            "atr": a, "nivel": pdh, "barrido": tope, "rr": (px - tp) / dist, "vela": m5.index[ult]}, ""


def senal_liquidez(m5, d1, ahora=None):
    """Tendencia diaria -> toma del PDH (venta) o del PDL (compra) hoy -> ruptura de la linea de tendencia en M5."""
    if m5 is None or d1 is None or len(m5) < 60 or len(d1) < 4:
        return None, "sin datos suficientes"
    t, txt = tendencia_diaria(d1)
    if t == 0:
        return None, txt
    ayer = d1.iloc[-1]
    pdh, pdl = float(ayer["high"]), float(ayer["low"])
    ini_dia = d1.index[-1] + pd.Timedelta(days=1)                   # comienzo del dia en curso
    if t < 0:
        s, motivo = _venta_liquidez(m5, ini_dia, pdh, pdl, 1)
    else:
        s, motivo = _venta_liquidez(_espejo(m5), ini_dia, -pdl, -pdh, -1)
        if s:
            s = {**s, **{x: -s[x] for x in ("entrada", "sl", "tp1", "tp2", "nivel", "barrido")},
                 "bias": 1, "dir": "LONG"}
    if s:
        s.update(tendencia_txt=txt, dia=str(d1.index[-1].date()))
        return s, ""
    return None, f"{txt} · {motivo}"


def confluencia_1h_15m(h1, m15, ini_dia, nivel, bias):
    """Confluencia de temporalidades para una senal de liquidez (en ventas; las compras se miran en espejo):
      1H : hoy una vela de 1H supero el PDH y la ULTIMA vela de 1H cerrada cerro de nuevo por debajo
           (la manipulacion fracaso);
      15M: la ULTIMA vela de 15M cerrada cerro por debajo del ultimo minimo de la subida previo al maximo de hoy
           (cambio de estructura). Devuelve (ok_1h, ok_15m)."""
    if bias > 0:
        h1, m15, nivel = _espejo(h1), _espejo(m15), -nivel
    hoy1 = h1[h1.index >= ini_dia]
    ok1 = len(hoy1) >= 1 and float(hoy1["high"].max()) > nivel and float(hoy1["close"].iloc[-1]) < nivel
    hoy15 = m15[m15.index >= ini_dia]
    ok15 = False
    if len(hoy15) >= 3:
        h, l, c = (m15[x].to_numpy(float) for x in ("high", "low", "close"))
        desde = len(m15) - len(hoy15)
        pico = desde + int(np.argmax(h[desde:]))
        k = 2
        bajos = [j for j in range(max(pico - 24, k), pico) if j + k < len(l) and l[j] == l[j - k:j + k + 1].min()]
        ok15 = bool(bajos) and pico < len(c) - 1 and c[-1] < l[bajos[-1]]
    return ok1, ok15


def analizar_todas(nombre, tf=None, cargar=obtener, ahora=None):
    meta = MERCADOS[nombre]
    if not mercado_abierto(meta["tipo"], ahora):
        return [], {"liquidez": "mercado cerrado"}
    try:
        m5 = solo_cerradas(cargar(nombre, "5m"), "5m", ahora)
        d1 = solo_cerradas(cargar(nombre, "1d"), "1d", ahora)
        s, motivo = senal_liquidez(m5, d1, ahora)
    except Exception as e:
        s, motivo = None, f"error: {e}"
    if not s:
        return [], {"liquidez": motivo}
    if CONFIG.get("confluencia_1h_15m"):
        try:
            h1 = solo_cerradas(cargar(nombre, "1h"), "1h", ahora)
            m15 = solo_cerradas(cargar(nombre, "15m"), "15m", ahora)
            ok1, ok15 = confluencia_1h_15m(h1, m15, d1.index[-1] + pd.Timedelta(days=1), s["nivel"], s["bias"])
        except Exception as e:
            return [], {"liquidez": f"entrada {s['dir']} en 5M pero sin datos de 1H/15M ({e})"}
        if not (ok1 and ok15):
            falta = " y ".join(x for x, ok in (("1H", ok1), ("15M", ok15)) if not ok)
            return [], {"liquidez": f"entrada {s['dir']} en 5M sin confluencia de {falta}"}
        s["confluencia"] = "1H + 15M"
    s.update(activo=nombre, tipo=meta["tipo"], tf="5m", estrategia="liquidez",
             id=f"liquidez|{nombre}|{s['dir']}|{s['dia']}")         # una senal por mercado, direccion y dia
    return [s], {}


# ----------------------------------------------------------------- MENSAJE
def imagen_alerta(s, df):
    return None


def texto_alerta(s):
    """Mensaje RESUMIDO de la senal de liquidez."""
    compra = s["bias"] > 0
    hora = pd.Timestamp(s["vela"]).tz_convert(NY) + pd.Timedelta(minutes=5)
    cfl = f" · ⭐ CONFLUENCIA {s['confluencia']}" if s.get("confluencia") else ""
    return (f"🚨 {'🟢 COMPRA' if compra else '🔴 VENTA'} {s['activo']} · LIQUIDEZ 5M{cfl}\n"
            f"📐 {s['tendencia_txt']} · tomó el {'PDL' if compra else 'PDH'} {fmt(s['nivel'])} · rompió la "
            f"línea de tendencia en M5\n"
            f"🎯 Entrada: {fmt(s['entrada'])} ({hora:%H:%M} NY)\n"
            f"🛑 SL: {fmt(s['sl'])} ({'bajo' if compra else 'sobre'} el barrido)\n"
            f"✅ TP (siguiente liquidez): {fmt(s['tp1'])} · R:R 1:{s['rr']:.1f}"
            + (f" · TP2 ({'PDH' if compra else 'PDL'}): {fmt(s['tp2'])}" if abs(s["tp2"] - s["tp1"]) > 1e-12 else ""))


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
    s = {"bias": -1, "dir": "SHORT", "activo": "GBP/USD", "tendencia_txt": "estructura diaria bajista (LH + LL)",
         "nivel": 1.32450, "entrada": 1.32380, "sl": 1.32520, "tp1": 1.32100, "tp2": 1.31950, "rr": 2.0,
         "vela": pd.Timestamp.now("UTC").floor("5min")}
    return "🧪 MENSAJE DE PRUEBA (no operar) · así llegarán las señales\n" + texto_alerta(s)


def correr_bot(a):
    estado = Estado(a.estado)
    t0 = time.time()
    if a.ping:
        ok, msg = enviar_telegram(f"✅ Ballena Liquidez {VERSION} activa · {datetime.now(NY):%a %d/%m %H:%M} NY\n"
                                  f"Tendencia diaria · toma del PDH/PDL · ruptura de la línea de tendencia en 5M · "
                                  f"R:R mínimo 1:{CONFIG['rr_min']:g}.\nMercados: {', '.join(MERCADOS)}")
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
    ap = argparse.ArgumentParser(description="Ballena Liquidez: PDH/PDL y linea de tendencia en 5M con alertas a Telegram")
    ap.add_argument("modo", nargs="?", default="bot", choices=["bot"])
    ap.add_argument("--una-vez", action="store_true")
    ap.add_argument("--ping", action="store_true")
    ap.add_argument("--minutos", type=float, default=0, help="bucle durante N minutos (GitHub Actions)")
    ap.add_argument("--estado", default="estado.json")
    ap.add_argument("--prueba", action="store_true", help="envia un mensaje de prueba a Telegram y termina")
    ap.add_argument("--avisar-estado", action="store_true", help="envia '✅ activo y analizando' a Telegram y termina")
    a = ap.parse_args()
    if a.avisar_estado:
        ok, msg = enviar_telegram(f"✅ ACTIVO Y ANALIZANDO · {VERSION}\nPDH/PDL + línea de tendencia en 5M · escaneo cada 5 minutos\n"
                                  f"Mercados: {', '.join(MERCADOS)}\nTe aviso en cuanto aparezca una señal.")
        print(f"Estado a Telegram: {'OK' if ok else msg}", flush=True)
        raise SystemExit(0 if ok else 1)
    if a.prueba:
        ok, msg = enviar_telegram(mensaje_prueba())
        print(f"Prueba de Telegram: {'OK' if ok else msg}", flush=True)
        raise SystemExit(0 if ok else 1)
    correr_bot(a)


if __name__ == "__main__":
    cli()

