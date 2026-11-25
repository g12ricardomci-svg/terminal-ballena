# -*- coding: utf-8 -*-
"""
BALLENA RANGO - CRT: la temporalidad mayor manda (Bias + DOL) y se entra en un rango pendiente activado
=====================================================================================================
Tres modelos, la misma estrategia adaptada a cada temporalidad (cada uno da su propia senal):

  RANGO 5M : mayor H1                      -> entrada M5
  RANGO 15M: mayor H4                      -> entrada M15
  RANGO 1H : mayor D1 (H4 sin contradecir) -> entrada H1

  1) BIAS: la ultima vela MAYOR CERRADA cierra con cuerpo alcista (bajista). Solo rangos ACTIVOS (velas cerradas).
  2) DOL (Draw on Liquidity): el maximo (minimo) de esa vela mayor. Si el precio ya lo alcanzo, no se opera.
  3) RANGO PENDIENTE: un minimo (maximo) de una vela menor anterior, aun sin mitigar.
  4) ACTIVACION CON CONFIRMACION: dentro de la vela mayor en curso, la ultima vela menor CERRADA barre ese minimo
     (retroceso / manipulacion) y CIERRA CON CUERPO alcista por encima -> ENTRADA a favor del DOL.
  5) Stop bajo el barrido · objetivo = DOL · FILTRO R:R: solo si el ratio es >= 1:2,5.

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
VERSION = "v1 · Bias + DOL · 5M, 15M y 1H"

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
    # La misma estrategia adaptada a cada temporalidad: la MAYOR da Bias y DOL; la de ENTRADA el rango pendiente
    "modelos": {"5M": {"mayor": "1h", "entrada": "5m"},
                "15M": {"mayor": "4h", "entrada": "15m"},
                "1H": {"mayor": "1d", "entrada": "1h", "filtro": "4h"}},   # en 1H, H4 no puede contradecir a D1
    "rr_min": 2.5,                # filtro riesgo-beneficio: solo entradas con ratio >= 1:2,5 hasta el DOL
    "pendientes": 12,             # velas menores hacia atras donde buscar rangos pendientes sin mitigar
    "stop_min_atr": 0.3,          # stop minimo: 0,3 ATR de la temporalidad de entrada
    "pausa_min": 60,              # minutos entre dos alertas del mismo mercado, modelo y direccion
    "banco": 50.0, "riesgo_pct": 1.0,
    "tfs": ["5m"],                # el bucle escanea al cierre de cada vela de 5m (los 3 modelos en cada pasada)
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


ENCABEZADO = "📦 BALLENA RANGO"


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



# ----------------------------------------------------------------- ESTRATEGIA: BIAS + DOL + RANGO PENDIENTE
NOMBRE_TF = {"5m": "M5", "15m": "M15", "1h": "H1", "4h": "H4", "1d": "D1"}


def bias_dol(mayor):
    """Bias por el CUERPO de la ultima vela mayor cerrada y su DOL (maximo si alcista, minimo si bajista)."""
    if mayor is None or len(mayor) < 2:
        return 0, None
    v = mayor.iloc[-1]
    o, c = float(v["open"]), float(v["close"])
    if c > o:
        return 1, float(v["high"])
    if c < o:
        return -1, float(v["low"])
    return 0, None


def _espejo(df):
    return pd.DataFrame({"open": -df["open"], "high": -df["low"], "low": -df["high"], "close": -df["close"]},
                        index=df.index)


def _rango_compra(ent, ini_mayor, dol, sgn=1, tf_ent="15m"):
    """COMPRA (las ventas usan el espejo; sgn=-1 solo cambia los textos). Dentro de la vela mayor en curso (desde
    ini_mayor), la ultima vela de entrada CERRADA barre un minimo pendiente sin mitigar y cierra con cuerpo
    alcista por encima. Stop bajo el barrido, objetivo = DOL, R:R minimo CONFIG['rr_min']."""
    ext, nt = ("mínimo" if sgn > 0 else "máximo"), NOMBRE_TF[tf_ent]
    o, h, l, c = (ent[x].to_numpy(float) for x in ("open", "high", "low", "close"))
    ult = len(c) - 1
    if ent.index[ult] < ini_mayor:
        return None, "esperando la primera vela de la vela mayor en curso"
    desde = int(ent.index.searchsorted(ini_mayor))
    if h[desde:].max() >= dol:
        return None, f"DOL {fmt(dol * sgn)} ya alcanzado en la vela mayor en curso"
    pendientes = [j for j in range(max(ult - CONFIG["pendientes"], 1), ult)
                  if l[j] < l[j + 1:ult].min(initial=np.inf)]           # minimos aun sin mitigar antes de la ultima
    barridos = [j for j in pendientes if l[ult] < l[j] < c[ult]]
    if not barridos:
        return None, f"esperando que una vela {nt} barra un {ext} pendiente y cierre de vuelta"
    if not c[ult] > o[ult]:
        return None, f"barrió el {ext} pendiente {nt} pero sin cierre con cuerpo a favor"
    nivel = float(min(l[j] for j in barridos))
    a = float(atr(ent).iloc[-1])
    px = float(c[ult])
    dist = max(px - (l[ult] - 0.1 * a), CONFIG["stop_min_atr"] * a)
    rr = (dol - px) / dist
    if rr < CONFIG["rr_min"]:
        return None, f"rango pendiente activado pero R:R 1:{rr:.1f} < 1:{CONFIG['rr_min']:g} (se descarta)"
    return {"bias": 1, "dir": "LONG", "entrada": px, "sl": px - dist, "tp1": dol, "tp2": dol, "riesgo_px": dist,
            "atr": a, "dol": dol, "nivel": nivel, "rr": rr, "vela": ent.index[ult]}, ""


def senal_rango(ent, mayor, modelo, filtro=None, ahora=None):
    """Un modelo: bias + DOL de la vela mayor cerrada -> rango pendiente activado en la temporalidad de entrada."""
    cfg = CONFIG["modelos"][modelo]
    if ent is None or mayor is None or len(ent) < 40 or len(mayor) < 3:
        return None, "sin datos suficientes"
    bias, dol = bias_dol(mayor)
    nm = NOMBRE_TF[cfg["mayor"]]
    if bias == 0:
        return None, f"vela {nm} sin cuerpo (sin bias)"
    if filtro is not None:
        b_f, _ = bias_dol(filtro)
        if b_f == -bias:
            return None, f"bias {nm} {'alcista' if bias > 0 else 'bajista'} contradicho por H4"
    ini = mayor.index[-1] + pd.Timedelta(minutes=TF_MIN[cfg["mayor"]])   # comienzo de la vela mayor en curso
    if bias > 0:
        s, motivo = _rango_compra(ent, ini, dol, 1, cfg["entrada"])
    else:
        s, motivo = _rango_compra(_espejo(ent), ini, -dol, -1, cfg["entrada"])
        if s:
            s = {**s, **{x: -s[x] for x in ("entrada", "sl", "tp1", "tp2", "dol", "nivel")}, "bias": -1, "dir": "SHORT"}
    if s:
        s.update(modelo=modelo, tf_mayor=cfg["mayor"], tf_entrada=cfg["entrada"])
        return s, ""
    return None, f"bias {nm} {'alcista' if bias > 0 else 'bajista'} · DOL {fmt(dol)} · {motivo}"


def analizar_todas(nombre, tf=None, cargar=obtener, ahora=None):
    """Corre los 3 modelos (5M, 15M, 1H) en un mercado. Cada modelo da su propia senal."""
    meta = MERCADOS[nombre]
    if not mercado_abierto(meta["tipo"], ahora):
        return [], {m: "mercado cerrado" for m in CONFIG["modelos"]}
    senales, motivos = [], {}
    for modelo, cfg in CONFIG["modelos"].items():
        try:
            ent = solo_cerradas(cargar(nombre, cfg["entrada"]), cfg["entrada"], ahora)
            mayor = solo_cerradas(cargar(nombre, cfg["mayor"]), cfg["mayor"], ahora)
            filtro = (solo_cerradas(cargar(nombre, cfg["filtro"]), cfg["filtro"], ahora)
                      if cfg.get("filtro") else None)
            s, motivo = senal_rango(ent, mayor, modelo, filtro, ahora)
        except Exception as e:
            s, motivo = None, f"error: {e}"
        motivos[f"rango {modelo}"] = motivo
        if s:
            s.update(activo=nombre, tipo=meta["tipo"], tf=cfg["entrada"], estrategia=f"rango_{modelo}",
                     id=f"rango|{modelo}|{nombre}|{s['dir']}|{s['vela']}")
            senales.append(s)
    return senales, motivos


# ----------------------------------------------------------------- MENSAJE
def imagen_alerta(s, df):
    return None


def texto_alerta(s):
    """Mensaje RESUMIDO: direccion, mercado y modelo · bias, DOL y rango activado · entrada · stop · DOL y R:R."""
    compra = s["bias"] > 0
    nm, ne = NOMBRE_TF[s["tf_mayor"]], NOMBRE_TF[s["tf_entrada"]]
    hora = pd.Timestamp(s["vela"]).tz_convert(NY) + pd.Timedelta(minutes=TF_MIN[s["tf_entrada"]])
    return (f"🚨 {'🟢 COMPRA' if compra else '🔴 VENTA'} {s['activo']} · RANGO {s['modelo']}\n"
            f"📐 Bias {nm} {'alcista' if compra else 'bajista'} · DOL {fmt(s['dol'])} · barrió el rango pendiente "
            f"{ne} {fmt(s['nivel'])} y cerró {'encima' if compra else 'debajo'}\n"
            f"🎯 Entrada: {fmt(s['entrada'])} ({hora:%H:%M} NY)\n"
            f"🛑 SL: {fmt(s['sl'])}\n"
            f"✅ TP (DOL): {fmt(s['dol'])} · R:R 1:{s['rr']:.1f}")


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
    s = {"bias": 1, "dir": "LONG", "activo": "EUR/USD", "modelo": "15M", "tf_mayor": "4h", "tf_entrada": "15m",
         "dol": 1.16420, "nivel": 1.15880, "entrada": 1.15920, "sl": 1.15860, "rr": 8.3,
         "vela": pd.Timestamp.now("UTC").floor("15min")}
    return "🧪 MENSAJE DE PRUEBA (no operar) · así llegarán las señales\n" + texto_alerta(s)


def correr_bot(a):
    estado = Estado(a.estado)
    t0 = time.time()
    if a.ping:
        ok, msg = enviar_telegram(f"✅ Ballena Rango {VERSION} activa · {datetime.now(NY):%a %d/%m %H:%M} NY\n"
                                  f"Bias + DOL de la temporalidad mayor · entrada en rango pendiente activado · "
                                  f"modelos 5M (H1), 15M (H4) y 1H (D1) · R:R mínimo 1:{CONFIG['rr_min']:g}.\n"
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
    ap = argparse.ArgumentParser(description="Ballena Rango: Bias + DOL y rango pendiente con alertas a Telegram")
    ap.add_argument("modo", nargs="?", default="bot", choices=["bot"])
    ap.add_argument("--una-vez", action="store_true")
    ap.add_argument("--ping", action="store_true")
    ap.add_argument("--minutos", type=float, default=0, help="bucle durante N minutos (GitHub Actions)")
    ap.add_argument("--estado", default="estado.json")
    ap.add_argument("--prueba", action="store_true", help="envia un mensaje de prueba a Telegram y termina")
    ap.add_argument("--avisar-estado", action="store_true", help="envia '✅ activo y analizando' a Telegram y termina")
    a = ap.parse_args()
    if a.avisar_estado:
        ok, msg = enviar_telegram(f"✅ ACTIVO Y ANALIZANDO · {VERSION}\nModelos 5M, 15M y 1H · escaneo cada 5 minutos\n"
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

