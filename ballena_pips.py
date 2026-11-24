# -*- coding: utf-8 -*-
"""
BALLENA PIPS - CRT (Candle Range Theory) fractal con alertas a Telegram
========================================================================
Basado en la clase "CRT deja la confusion con ejemplo real" (canal STREETPIPS). La CRT es FRACTAL: el mismo modelo
se aplica escalado (cada escalon ~4 veces el siguiente). Dos modelos, cada uno con su propia senal:

  CRT 1H : sesgo H4 -> rango/manipulacion H1  -> confirmacion M5
  CRT 15M: sesgo H1 -> rango/manipulacion M15 -> confirmacion M1

  1) SESGO: la vela mayor barre el minimo de la anterior y cierra dentro (o cierra sobre su maximo) -> alcista;
     en espejo, bajista. Vela interna = sin direccion.
  2) TIMING: 24/7 por defecto (CONFIG["solo_sesiones"] = True lo limita a Londres 2-5 NY y Nueva York 8-11:30 NY).
  3) MANIPULACION: con sesgo bajista, la vela de rango en curso BARRE EL MAXIMO de la anterior
     ("vender por encima de maximos"); con sesgo alcista, barre el minimo.
  4) CONFIRMACION: una vela de la temporalidad menor cierra con cuerpo al otro lado de su ultimo swing previo al
     barrido y vuelve dentro del rango -> ENTRADA.
  5) Stop tras el barrido · TP1 = 2R · TP2 = extremo opuesto de la vela de rango (objetivo CRT).
  6) Construccion de la posicion: 1/3 al entrar y +1/3 por nueva confirmacion, dentro del riesgo DIARIO.

  python ballena_pips.py               -> bot: escanea cada minuto y avisa por Telegram
  python ballena_pips.py --una-vez     -> una pasada
  python ballena_pips.py --ping        -> manda "✅ activa" al arrancar

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
VERSION = "v3 · CRT 1H y 15M · 24/7 · 8 mercados"

# ----------------------------------------------------------------- CONFIGURACION (EDITA AQUI)
MERCADOS = {   # los 8 mercados elegidos: nombre -> tipo, ticker de Yahoo y, si existe, simbolo de exchange (ccxt)
    "EUR/USD": {"tipo": "forex", "yf": "EURUSD=X"},
    "AUD/USD": {"tipo": "forex", "yf": "AUDUSD=X"},
    "GBP/USD": {"tipo": "forex", "yf": "GBPUSD=X"},
    "USD/JPY": {"tipo": "forex", "yf": "USDJPY=X"},
    "GBP/JPY": {"tipo": "forex", "yf": "GBPJPY=X"},
    "XAU/USD (Oro)": {"tipo": "oro", "yf": "GC=F", "ccxt": "PAXG/USDT"},
    "BTC/USD": {"tipo": "cripto", "yf": "BTC-USD", "ccxt": "BTC/USDT"},
    "NASDAQ 100": {"tipo": "indice", "yf": "NQ=F"},
}
CONFIG = {
    # Modelos CRT FRACTALES: la misma estrategia escalada (cada escalon ~4 veces el siguiente)
    #   CRT 1H : sesgo H4 -> rango/manipulacion H1  -> confirmacion M5
    #   CRT 15M: sesgo H1 -> rango/manipulacion M15 -> confirmacion M1
    "modelos": {"1H": {"sesgo": "4h", "rango": "1h", "conf": "5m"},
                "15M": {"sesgo": "1h", "rango": "15m", "conf": "1m"}},
    "ventanas_ny": [(2.0, 5.0), (8.0, 11.5)],   # sesiones de Londres y Nueva York (hora NY)
    "solo_sesiones": False,       # False = analiza 24/7 (si el mercado esta abierto); True = solo en las sesiones
    "swing_k": 2,                 # swings de M5 para la ruptura de estructura
    "tp1_r": 2.0,                 # TP1 a 2R; TP2 = extremo opuesto de la vela H1 de referencia (objetivo CRT)
    "pausa_min": 60,              # minutos entre dos alertas del mismo mercado y direccion
    "banco": 50.0, "riesgo_pct": 1.0,           # capital y % de riesgo por entrada
    "riesgo_diario_pct": 3.0,     # limite de riesgo DIARIO para la construccion de la posicion (promediado)
    "contratos": 3,               # la posicion se construye en hasta 3 partes, cada una tras una confirmacion
    "tfs": ["1m"],                # el bucle escanea cada minuto (la confirmacion del modelo 15M es en M1)
    "imagen": False,              # mensajes resumidos sin imagen
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
    return str(os.environ.get(n) or DEFAULT_SECRETS.get(n, "")).strip()


def _chats():
    return [i.strip() for i in secreto("TELEGRAM_CHAT_IDS").replace(";", ",").split(",") if i.strip()]


def _pausa_tg():
    espera = 1.1 - (time.time() - TG["ultimo"])
    if espera > 0:
        _dormir(espera)


ENCABEZADO = "💧 BALLENA PIPS"


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



# ----------------------------------------------------------------- ESTRATEGIA CRT (Candle Range Theory)
def _ultimas_cerradas(df, n=2):
    return df.iloc[-n:] if df is not None and len(df) >= n else None


def sesgo_rango(df):
    """Direccion de un rango CRT con las 2 ultimas velas CERRADAS (c1 = rango, c2 = la que lo prueba):
      +1 si c2 barrio el minimo de c1 y cerro dentro (manipulacion bajista fallida) o cerro sobre el maximo de c1
      -1 si c2 barrio el maximo de c1 y cerro dentro, o cerro bajo el minimo de c1;  0 si quedo dentro sin barrer."""
    v = _ultimas_cerradas(df)
    if v is None:
        return 0, "sin datos"
    h1, l1 = float(v["high"].iloc[0]), float(v["low"].iloc[0])
    h2, l2, c2 = float(v["high"].iloc[1]), float(v["low"].iloc[1]), float(v["close"].iloc[1])
    if c2 > h1:
        return 1, f"cerró sobre el máximo {fmt(h1)}"
    if c2 < l1:
        return -1, f"cerró bajo el mínimo {fmt(l1)}"
    if l2 < l1 and h2 <= h1:
        return 1, f"barrió el mínimo {fmt(l1)} y cerró dentro"
    if h2 > h1 and l2 >= l1:
        return -1, f"barrió el máximo {fmt(h1)} y cerró dentro"
    return 0, "vela interna (sin dirección)"


def a_12h(h1):
    return h1.resample("12h", label="left", closed="left").agg(
        {"open": "first", "high": "max", "low": "min", "close": "last"}).dropna()


def sesgo_objetivo(d1, h1):
    """Direccion OBJETIVA: CRT de D1; si D1 no da direccion, la de H12. Devuelve (sesgo, texto)."""
    s, txt = sesgo_rango(d1)
    if s:
        return s, f"D1 {txt}"
    s12, txt12 = sesgo_rango(a_12h(h1)) if h1 is not None and len(h1) > 30 else (0, "sin datos")
    return (s12, f"H12 {txt12}") if s12 else (0, f"D1 y H12 sin dirección ({txt})")


def en_ventana(ahora):
    if not CONFIG.get("solo_sesiones", True):
        return True                                          # 24/7
    n = _ny(ahora)
    h = n.hour + n.minute / 60
    return any(a <= h < b for a, b in CONFIG["ventanas_ny"])


NOMBRE_TF = {"1m": "M1", "5m": "M5", "15m": "M15", "1h": "H1", "4h": "H4", "12h": "H12", "1d": "D1"}


def _crt_venta(conf, rango, sgn=1, tf_rango="1h", tf_conf="5m"):
    """VENTA con sesgo bajista: la vela de RANGO en curso (p. ej. H1) barrio el maximo de la anterior (vender por
    encima de maximos) y la ULTIMA vela de CONFIRMACION (p. ej. M5) cierra con cuerpo bajo el ultimo swing bajo
    previo al barrido, de nuevo dentro del rango. Stop sobre el barrido, TP1 2R, TP2 = minimo del rango de referencia.
    sgn=-1 cuando se llama con el espejo (compras): solo cambia los textos."""
    ext, bajo_sobre = ("máximo", "bajo") if sgn > 0 else ("mínimo", "sobre")
    nr, nc = NOMBRE_TF[tf_rango], NOMBRE_TF[tf_conf]
    ref = rango.iloc[-1]                                     # vela de referencia: la ultima CERRADA del rango
    ini = rango.index[-1] + pd.Timedelta(minutes=TF_MIN[tf_rango])
    c2 = conf[conf.index >= ini]
    if len(c2) < 3:
        return None, f"esperando que avance la vela {nr} en curso"
    hi1, lo1 = float(ref["high"]), float(ref["low"])
    hs = c2["high"].to_numpy(float)
    if hs.max() <= hi1:
        return None, f"sin manipulación: la {nr} aún no barrió el {ext} {fmt(hi1 * sgn)}"
    k_bar = int(np.argmax(hs))                               # vela del barrido (extremo de la manipulacion)
    pos = len(conf) - len(c2) + k_bar
    o, h, l, c = (conf[x].to_numpy(float) for x in ("open", "high", "low", "close"))
    ult = len(c) - 1
    if ult <= pos:
        return None, f"barrió el {ext} {nr} {fmt(hi1 * sgn)}: esperando confirmación en {nc}"
    k = CONFIG["swing_k"]
    bajos = [j for j in range(max(pos - 30, k), pos) if l[j] == l[j - k:j + k + 1].min()]
    if not bajos:
        return None, f"sin swing de {nc} previo al barrido"
    nivel = float(l[bajos[-1]])
    if not (c[ult] < nivel and c[ult - 1] >= nivel and c[ult] < o[ult] and c[ult] < hi1):
        return None, f"barrió el {ext} {nr} {fmt(hi1 * sgn)}: esperando cierre {nc} {bajo_sobre} {fmt(nivel * sgn)}"
    a = float(atr(conf).iloc[-1])
    px, barrido = float(c[ult]), float(h[pos:].max())
    dist = (barrido + 0.1 * a) - px
    if dist <= 0:
        return None, "stop inválido"
    tp1 = px - CONFIG["tp1_r"] * dist
    tp2 = min(lo1, px - 3 * dist) if lo1 < tp1 else px - 3 * dist
    return {"bias": -1, "dir": "SHORT", "entrada": px, "sl": px + dist, "tp1": tp1, "tp2": tp2, "riesgo_px": dist,
            "atr": a, "manipulado": hi1, "objetivo_crt": lo1, "mss": nivel, "vela": conf.index[ult],
            "vela_ruptura": rango.index[-1]}, ""


def _espejo(df):
    return pd.DataFrame({"open": -df["open"], "high": -df["low"], "low": -df["high"], "close": -df["close"]},
                        index=df.index)


def senal_crt(conf, rango, sesgo_df, ahora=None, modelo="1H"):
    """CRT de un modelo: sesgo (vela mayor) -> timing (ventana) -> manipulacion (vela de rango) -> confirmacion."""
    cfg = CONFIG["modelos"][modelo]
    if conf is None or rango is None or len(conf) < 60 or len(rango) < 5:
        return None, "sin datos suficientes"
    sesgo, txt = sesgo_rango(sesgo_df)
    txt = f"{NOMBRE_TF[cfg['sesgo']]} {txt}"
    if sesgo == 0:
        return None, txt
    momento = ahora if ahora is not None else conf.index[-1] + pd.Timedelta(minutes=TF_MIN[cfg["conf"]])
    if not en_ventana(momento):
        return None, f"sesgo {'alcista' if sesgo > 0 else 'bajista'} ({txt}) · fuera de horario (Londres/NY)"
    if sesgo < 0:
        s, motivo = _crt_venta(conf, rango, 1, cfg["rango"], cfg["conf"])
    else:
        s, motivo = _crt_venta(_espejo(conf), _espejo(rango), -1, cfg["rango"], cfg["conf"])
        if s:
            s = {**s, **{x: -s[x] for x in ("entrada", "sl", "tp1", "tp2", "manipulado", "objetivo_crt", "mss")},
                 "bias": 1, "dir": "LONG"}
    if s:
        s.update(sesgo_txt=txt, modelo=modelo, tf_rango=cfg["rango"], tf_conf=cfg["conf"])
    return s, motivo


def analizar_todas(nombre, tf=None, cargar=obtener, ahora=None):
    """Corre los modelos CRT (1H y 15M) en un mercado. Cada modelo da su propia senal."""
    meta = MERCADOS[nombre]
    if not mercado_abierto(meta["tipo"], ahora):
        return [], {m: "mercado cerrado" for m in CONFIG["modelos"]}
    senales, motivos = [], {}
    for modelo, cfg in CONFIG["modelos"].items():
        try:
            conf = solo_cerradas(cargar(nombre, cfg["conf"]), cfg["conf"], ahora)
            rango = solo_cerradas(cargar(nombre, cfg["rango"]), cfg["rango"], ahora)
            sesgo_df = solo_cerradas(cargar(nombre, cfg["sesgo"]), cfg["sesgo"], ahora)
            s, motivo = senal_crt(conf, rango, sesgo_df, ahora, modelo)
        except Exception as e:
            s, motivo = None, f"error: {e}"
        motivos[f"crt {modelo}"] = motivo
        if s:
            s.update(activo=nombre, tipo=meta["tipo"], tf=cfg["conf"], estrategia=f"crt_{modelo}",
                     id=f"crt|{modelo}|{nombre}|{s['dir']}|{s['vela_ruptura']}")
            senales.append(s)
    return senales, motivos


# ----------------------------------------------------------------- MENSAJE
def imagen_alerta(s, df):
    return None


def texto_alerta(s):
    """Mensaje RESUMIDO de la senal CRT con el plan de construccion de la posicion."""
    compra = s["bias"] > 0
    hora = pd.Timestamp(s["vela"]).tz_convert(NY) + pd.Timedelta(minutes=TF_MIN[s.get("tf_conf", "5m")])
    por_parte = CONFIG["banco"] * CONFIG["riesgo_pct"] / 100
    diario = CONFIG["banco"] * CONFIG["riesgo_diario_pct"] / 100
    n = CONFIG["contratos"]
    nr, nc = NOMBRE_TF[s.get("tf_rango", "1h")], NOMBRE_TF[s.get("tf_conf", "5m")]
    return (f"🚨 {'🟢 COMPRA' if compra else '🔴 VENTA'} {s['activo']} · CRT {s.get('modelo', '1H')}\n"
            f"📐 Sesgo {s['sesgo_txt']} · barrió el {'mínimo' if compra else 'máximo'} {nr} {fmt(s['manipulado'])} · "
            f"confirmación {nc}\n"
            f"🎯 Entrada: {fmt(s['entrada'])} ({hora:%H:%M} NY)\n"
            f"🛑 SL: {fmt(s['sl'])}\n"
            f"✅ TP1: {fmt(s['tp1'])} · TP2 (objetivo CRT): {fmt(s['tp2'])}\n"
            f"🧱 1/{n} ahora (${por_parte:.2f}) · +1/{n} por cada nueva confirmación a favor · riesgo diario máx "
            f"${diario:.2f}")


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
    s = {"bias": -1, "dir": "SHORT", "activo": "EUR/USD", "modelo": "15M", "tf_rango": "15m", "tf_conf": "1m",
         "sesgo_txt": "H1 barrió el máximo 1.16120 y cerró dentro", "manipulado": 1.16080, "entrada": 1.16010,
         "sl": 1.16095, "tp1": 1.15840, "tp2": 1.15790, "vela": pd.Timestamp.now("UTC").floor("min")}
    return ("🧪 MENSAJE DE PRUEBA (no operar) · así llegarán las señales\n" + texto_alerta(s))


def correr_bot(a):
    estado = Estado(a.estado)
    t0 = time.time()
    if a.ping:
        ok, msg = enviar_telegram(f"✅ Ballena Pips {VERSION} activa · {datetime.now(NY):%a %d/%m %H:%M} NY\n"
                                  f"CRT fractal: 1H (sesgo H4 · rango H1 · confirmación M5) y 15M (sesgo H1 · "
                                  f"rango M15 · confirmación M1) · análisis 24/7.\nMercados: {', '.join(MERCADOS)}")
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
    ap = argparse.ArgumentParser(description="Ballena Pips: CRT (Candle Range Theory) con alertas a Telegram")
    ap.add_argument("modo", nargs="?", default="bot", choices=["bot"])
    ap.add_argument("--una-vez", action="store_true")
    ap.add_argument("--ping", action="store_true")
    ap.add_argument("--minutos", type=float, default=0, help="bucle durante N minutos (GitHub Actions)")
    ap.add_argument("--estado", default="estado.json")
    ap.add_argument("--prueba", action="store_true", help="envia un mensaje de prueba a Telegram y termina")
    ap.add_argument("--avisar-estado", action="store_true", help="envia '✅ activo y analizando' a Telegram y termina")
    a = ap.parse_args()
    if a.avisar_estado:
        ok, msg = enviar_telegram(f"✅ ACTIVO Y ANALIZANDO · {VERSION}\nCRT 1H y 15M · 24/7 · escaneo cada minuto\n"
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

