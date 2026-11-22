"""
INSTITUTIONAL QUANT TERMINAL - AMT & Order Blocks
Contexto 1H -> entrada 15m/5m.  Cripto 24/7, Forex/Oro lunes-viernes.
Confluencias: sesgo 1H, BOS, Order Block, descuento Fibonacci >=50%, FVG, POC/VAH/VAL.
Filtros: ADX por mercado, exhaustacion (Williams %R), mercado cerrado, killzone NY.
"""
import os
import time
from datetime import datetime
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd
import plotly.graph_objects as go
import requests
import streamlit as st

try:
    import ccxt
except Exception:
    ccxt = None
try:
    import yfinance as yf
except Exception:
    yf = None

st.set_page_config(page_title="Institutional Quant Terminal", page_icon="🏛️", layout="wide")
NY = ZoneInfo("America/New_York")

# ----------------------------------------------------------------- ACTIVOS
CRIPTO = ["BTC/USDT", "ETH/USDT", "BNB/USDT", "SOL/USDT", "XRP/USDT",
          "ADA/USDT", "DOGE/USDT", "AVAX/USDT", "LINK/USDT"]
FOREX = {"EUR/USD": "EURUSD=X", "GBP/USD": "GBPUSD=X",
         "USD/JPY": "USDJPY=X", "AUD/USD": "AUDUSD=X"}
ORO = {"XAU/USD (Oro)": "GC=F"}

ACTIVOS = {}
for c in CRIPTO:
    ACTIVOS[c] = {"tipo": "cripto", "yf": c.replace("/USDT", "-USD")}
for k, v in FOREX.items():
    ACTIVOS[k] = {"tipo": "forex", "yf": v}
for k, v in ORO.items():
    ACTIVOS[k] = {"tipo": "oro", "yf": v}

ADX_MIN = {"cripto": 18, "forex": 20, "oro": 20}   # ADX minimo por mercado
EXCHANGES = ["kucoin", "okx", "bybit", "binance"]   # binance al final: bloquea IPs de EE.UU.
YF_CFG = {"5m": ("5m", "5d"), "15m": ("15m", "10d"), "1h": ("1h", "60d")}
COOLDOWN_H = 4


# ----------------------------------------------------------------- TELEGRAM
def _secret(name):
    try:
        v = st.secrets[name]
        if isinstance(v, (list, tuple)):
            return ",".join(str(x) for x in v)
        return str(v).strip()
    except Exception:
        return os.environ.get(name, "").strip()


def enviar_telegram(texto):
    token = _secret("TELEGRAM_TOKEN")
    ids = [i.strip() for i in _secret("TELEGRAM_CHAT_IDS").replace(";", ",").split(",") if i.strip()]
    if not token or not ids:
        return False, "Faltan TELEGRAM_TOKEN / TELEGRAM_CHAT_IDS en Secrets"
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
def mercado_abierto(tipo, ahora=None):
    """Cripto 24/7. Forex/Oro: cierran viernes 17:00 NY y abren domingo 17:00 NY."""
    if tipo == "cripto":
        return True
    n = ahora or datetime.now(NY)
    h = n.hour + n.minute / 60
    wd = n.weekday()
    if wd == 5:
        return False
    if wd == 4 and h >= 17:
        return False
    if wd == 6 and h < 17:
        return False
    return True


def en_killzone(ahora=None):
    n = ahora or datetime.now(NY)
    return 7 <= n.hour < 11


# ----------------------------------------------------------------- DATOS
@st.cache_resource
def _exchange(nombre):
    return getattr(ccxt, nombre)({"enableRateLimit": True, "timeout": 15000})


def _ohlcv_ccxt(simbolo, tf, limite=500):
    if ccxt is None:
        return None
    for nombre in EXCHANGES:
        try:
            raw = _exchange(nombre).fetch_ohlcv(simbolo, timeframe=tf, limit=limite)
            if raw and len(raw) > 50:
                df = pd.DataFrame(raw, columns=["t", "open", "high", "low", "close", "volume"])
                df["t"] = pd.to_datetime(df["t"], unit="ms", utc=True)
                return df.set_index("t")
        except Exception:
            continue
    return None


def _ohlcv_yf(ticker, tf):
    if yf is None:
        return None
    interval, periodo = YF_CFG[tf]
    try:
        d = yf.download(ticker, interval=interval, period=periodo, progress=False,
                        auto_adjust=False, threads=False)
        if d is None or d.empty:
            return None
        if isinstance(d.columns, pd.MultiIndex):
            d.columns = d.columns.get_level_values(0)
        d = d.rename(columns=str.lower)[["open", "high", "low", "close", "volume"]].dropna()
        d.index = pd.to_datetime(d.index, utc=True)
        return d
    except Exception:
        return None


@st.cache_data(ttl=120, show_spinner=False)
def obtener(nombre, tf):
    meta = ACTIVOS[nombre]
    df = _ohlcv_ccxt(nombre, tf) if meta["tipo"] == "cripto" else None
    if df is None:
        df = _ohlcv_yf(meta["yf"], tf)
    return df


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
    h, l = df["high"].values, df["low"].values
    sh, sl = [], []
    for i in range(k, len(df) - k):
        if h[i] == h[i - k:i + k + 1].max():
            sh.append(i)
        if l[i] == l[i - k:i + k + 1].min():
            sl.append(i)
    return sh, sl


def perfil_volumen(df, bins=48, area=0.70):
    """POC / VAH / VAL. Si no hay volumen (forex) pondera por tiempo."""
    lo, hi = df["low"].min(), df["high"].max()
    if hi <= lo:
        return None
    edges = np.linspace(lo, hi, bins + 1)
    centros = (edges[:-1] + edges[1:]) / 2
    vol = np.zeros(bins)
    pesos = df["volume"].values if df["volume"].sum() > 0 else np.ones(len(df))
    for h, l, v in zip(df["high"].values, df["low"].values, pesos):
        i0 = max(int(np.searchsorted(edges, l, side="right")) - 1, 0)
        i1 = min(int(np.searchsorted(edges, h, side="right")) - 1, bins - 1)
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


def sesgo_1h(d1):
    """+1 alcista, -1 bajista, 0 neutral (EMA21/50 + posicion vs POC)."""
    c = d1["close"]
    e21, e50 = c.ewm(span=21, adjust=False).mean(), c.ewm(span=50, adjust=False).mean()
    vp = perfil_volumen(d1.tail(120))
    s = 0
    if c.iloc[-1] > e50.iloc[-1] and e21.iloc[-1] > e50.iloc[-1]:
        s += 1
    if c.iloc[-1] < e50.iloc[-1] and e21.iloc[-1] < e50.iloc[-1]:
        s -= 1
    if vp:
        s += 1 if c.iloc[-1] > vp["poc"] else -1
    return (1 if s >= 1 else -1 if s <= -1 else 0), vp


def _flip(df):
    """Espejo de precios para reutilizar la logica LONG en SHORT."""
    f = pd.DataFrame(index=df.index)
    f["open"], f["close"] = -df["open"], -df["close"]
    f["high"], f["low"] = -df["low"], -df["high"]
    f["volume"] = df["volume"]
    return f


def setup_long(df, ventana=40):
    """BOS alcista reciente + Order Block (ultima vela bajista antes del impulso) + FVG + retroceso."""
    n = len(df)
    o, h, l, c = (df[x].values for x in ("open", "high", "low", "close"))
    sh, _ = swings(df, 3)
    for j in reversed([x for x in sh if x < n - 4]):
        if j < n - 90:
            break
        idx = np.where(c[j + 1:] > h[j])[0]
        if len(idx) == 0:
            continue
        i = j + 1 + int(idx[0])                 # vela que rompe la estructura
        if i < n - ventana:
            continue
        m = j + int(np.argmin(l[j:i + 1]))      # minimo previo al impulso
        ob = m
        for k in range(m, max(m - 6, -1), -1):
            if c[k] < o[k]:
                ob = k
                break
        ob_lo, ob_hi = l[ob], h[ob]
        top, base = h[i:].max(), l[m]
        if top <= base:
            continue
        ratio = (top - c[-1]) / (top - base)    # retroceso Fibonacci (0.5 = 50%)
        toco = bool((l[-3:] <= ob_hi).any() and c[-1] >= ob_lo)
        fvg = any(l[q] > h[q - 2] and not (l[q] < ob_lo or h[q - 2] > ob_hi)
                  for q in range(max(2, m), n))
        return {"ob_lo": float(ob_lo), "ob_hi": float(ob_hi), "ratio": float(ratio),
                "toco": toco, "fvg": bool(fvg), "ob_t": df.index[ob],
                "base": float(base), "top": float(top)}
    return None


# ----------------------------------------------------------------- ANALISIS
def fmt(x):
    return f"{x:,.2f}" if abs(x) >= 100 else (f"{x:.4f}" if abs(x) >= 1 else f"{x:.6f}")


def analizar(nombre, tf, p):
    """Devuelve (diagnostico, senal|None)."""
    meta = ACTIVOS[nombre]
    diag = {"Activo": nombre, "Estado": "", "Sesgo 1H": "-", "Dir": "-", "Score": 0}
    if not mercado_abierto(meta["tipo"]):
        diag["Estado"] = "Mercado cerrado"
        return diag, None
    df, d1 = obtener(nombre, tf), obtener(nombre, "1h")
    if df is None or d1 is None or len(df) < 120 or len(d1) < 60:
        diag["Estado"] = "Sin datos"
        return diag, None

    bias, vp = sesgo_1h(d1)
    diag["Sesgo 1H"] = {1: "Alcista", -1: "Bajista", 0: "Neutral"}[bias]
    diag["_vp"] = vp
    if bias == 0:
        diag["Estado"] = "Sesgo neutral: sin operar"
        return diag, None

    adx_v = float(adx(df).iloc[-1])
    if adx_v < ADX_MIN[meta["tipo"]]:
        diag["Estado"] = f"ADX bajo ({adx_v:.0f} < {ADX_MIN[meta['tipo']]})"
        return diag, None

    s = setup_long(df) if bias == 1 else setup_long(_flip(df))
    if s is None:
        diag["Estado"] = "Sin BOS reciente"
        return diag, None
    if bias == -1:   # deshacer el espejo
        s = {**s, "ob_lo": -s["ob_hi"], "ob_hi": -s["ob_lo"], "base": -s["base"], "top": -s["top"]}
    diag["_ob"] = (s["ob_lo"], s["ob_hi"])
    diag["Dir"] = "LONG" if bias == 1 else "SHORT"

    px = float(df["close"].iloc[-1])
    a_v = float(atr(df).iloc[-1])
    wr = float(williams_r(df).iloc[-1])
    poc_ok = vp is not None and ((px <= vp["poc"]) if bias == 1 else (px >= vp["poc"]))
    score = 1 + 1 + int(s["toco"]) + int(s["ratio"] >= 0.5) + int(s["fvg"]) + int(poc_ok)
    diag["Score"] = score

    if not s["toco"]:
        diag["Estado"] = f"Esperando retroceso al OB ({score}/6)"
        return diag, None
    if (bias == 1 and wr > -20) or (bias == -1 and wr < -80):
        diag["Estado"] = f"Filtro exhaustacion (Williams {wr:.0f})"
        return diag, None
    if score < p["min_score"]:
        diag["Estado"] = f"Score insuficiente ({score}/{p['min_score']})"
        return diag, None
    if p["kz"] and not en_killzone():
        diag["Estado"] = f"Setup valido {score}/6 fuera de killzone"
        return diag, None

    sl = s["ob_lo"] - 0.3 * a_v if bias == 1 else s["ob_hi"] + 0.3 * a_v
    riesgo_px = abs(px - sl)
    if riesgo_px <= 0 or riesgo_px > 3 * a_v:
        diag["Estado"] = "SL invalido o demasiado lejos"
        return diag, None
    riesgo_usd = p["banco"] * p["riesgo"] / 100
    diag["Estado"] = "🚨 SENAL"
    return diag, {"activo": nombre, "tf": tf, "dir": diag["Dir"], "entrada": px, "sl": sl,
                  "tp1": px + bias * 2 * riesgo_px, "tp2": px + bias * 3 * riesgo_px,
                  "riesgo_usd": riesgo_usd, "unidades": riesgo_usd / riesgo_px, "score": score}


def texto_alerta(s):
    ic = "🟢 LONG" if s["dir"] == "LONG" else "🔴 SHORT"
    return (f"{ic} {s['activo']} ({s['tf']})\n"
            f"Entrada: {fmt(s['entrada'])}\nSL: {fmt(s['sl'])}\n"
            f"TP1 (2R): {fmt(s['tp1'])}\nTP2 (3R): {fmt(s['tp2'])}\n"
            f"Riesgo: ${s['riesgo_usd']:.2f} | Tamano: {s['unidades']:.4f} unid.\n"
            f"Score: {s['score']}/6 | Hora NY: {datetime.now(NY):%a %H:%M}")


@st.cache_resource
def memoria():
    return {"enviadas": {}, "log": []}


def procesar_senal(s):
    """Evita spam: una alerta por activo y direccion cada COOLDOWN_H horas."""
    mem = memoria()
    k = (s["activo"], s["dir"])
    if time.time() - mem["enviadas"].get(k, 0) < COOLDOWN_H * 3600:
        return False
    ok, msg = enviar_telegram(texto_alerta(s))
    mem["enviadas"][k] = time.time()
    mem["log"].append({"Hora NY": f"{datetime.now(NY):%d/%m %H:%M}", "Activo": s["activo"],
                       "Dir": s["dir"], "Entrada": fmt(s["entrada"]), "SL": fmt(s["sl"]),
                       "TP1": fmt(s["tp1"]), "Score": s["score"],
                       "Telegram": "OK" if ok else msg})
    return True


# ----------------------------------------------------------------- GRAFICO
def grafico(nombre, tf, diag):
    df = obtener(nombre, tf)
    if df is None:
        return None
    d = df.tail(150)
    fig = go.Figure(go.Candlestick(x=d.index, open=d["open"], high=d["high"],
                                   low=d["low"], close=d["close"], name=nombre))
    vp = diag.get("_vp")
    if vp:
        for k, col in (("poc", "#facc15"), ("vah", "#a78bfa"), ("val", "#a78bfa")):
            fig.add_hline(y=vp[k], line_dash="dot", line_color=col, annotation_text=k.upper())
    ob = diag.get("_ob")
    if ob:
        fig.add_hrect(y0=ob[0], y1=ob[1], fillcolor="rgba(56,189,248,0.25)",
                      line_width=0, annotation_text="OB")
    fig.update_layout(template="plotly_dark", height=460, xaxis_rangeslider_visible=False,
                      margin=dict(l=10, r=10, t=30, b=10))
    return fig


# ----------------------------------------------------------------- UI
def main():
    st.sidebar.title("🌐 Configuracion")
    activo = st.sidebar.selectbox("Activo:", list(ACTIVOS.keys()))
    tf = st.sidebar.selectbox("Temporalidad de entrada:", ["15m", "5m"], index=0)
    kz = st.sidebar.checkbox("Exigir killzone Nueva York (7-11 ET)", True)
    min_score = st.sidebar.slider("Score minimo (de 6)", 3, 6, 5)
    banco = st.sidebar.number_input("Bank ($)", 1.0, 1_000_000.0, 10.0)
    riesgo = st.sidebar.number_input("Riesgo por operacion (%)", 0.1, 10.0, 2.0)
    auto = st.sidebar.checkbox("Auto-escaneo cada 5 min", True)
    params = {"kz": kz, "min_score": min_score, "banco": banco, "riesgo": riesgo}

    if not _secret("TELEGRAM_TOKEN") or not _secret("TELEGRAM_CHAT_IDS"):
        st.sidebar.warning("Telegram sin configurar: agrega TELEGRAM_TOKEN y TELEGRAM_CHAT_IDS en Secrets.")
    if st.sidebar.button("🔔 Probar Telegram Ahora"):
        ok, msg = enviar_telegram("✅ Prueba de Institutional Quant Terminal")
        (st.sidebar.success if ok else st.sidebar.error)(msg if ok else f"No se pudo enviar: {msg}")
    if st.sidebar.button("⚡ Forzar Escaneo Ahora"):
        obtener.clear()

    st.markdown("# 🏛️ INSTITUTIONAL QUANT TERMINAL")
    st.caption("AMT + Order Blocks · contexto 1H → entrada 15m/5m · Cripto 24/7 · Forex/Oro lun-vie")

    @st.fragment(run_every=300 if auto else None)
    def panel():
        nuevas, filas, diags = 0, [], {}
        with st.spinner("Escaneando mercados..."):
            for nm in ACTIVOS:
                diag, sen = analizar(nm, tf, params)
                diags[nm] = diag
                filas.append({k: v for k, v in diag.items() if not k.startswith("_")})
                if sen and procesar_senal(sen):
                    nuevas += 1
        ahora = datetime.now(NY)
        st.info(f"⚡ Motor activo · {ahora:%a %H:%M} ET · Killzone NY: "
                f"{'SI' if en_killzone(ahora) else 'no'} · {len(ACTIVOS)} graficos revisados · "
                f"{nuevas} alertas nuevas")
        st.dataframe(pd.DataFrame(filas), use_container_width=True, hide_index=True)
        fig = grafico(activo, tf, diags.get(activo, {}))
        if fig is not None:
            st.plotly_chart(fig, use_container_width=True)
        else:
            st.warning("Sin datos para este activo ahora mismo.")
        with st.expander("📜 Registro de alertas"):
            log = memoria()["log"]
            if log:
                st.dataframe(pd.DataFrame(log[-30:][::-1]), use_container_width=True, hide_index=True)
            else:
                st.write("Aun no hay alertas.")

    panel()
    st.caption("Herramienta educativa. No es asesoria financiera. Prueba primero en demo.")


if __name__ == "__main__":
    main()
