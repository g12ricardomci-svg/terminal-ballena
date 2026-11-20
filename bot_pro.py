import hashlib
import os
import time
from datetime import datetime, timezone

import numpy as np
import pandas as pd
import requests
import streamlit as st
import yfinance as yf

# 1. SIEMPRE PRIMERO: configuración de página para evitar pantalla en negro
st.set_page_config(
    page_title="Terminal Cuantitativa | Forex, Oro y Cripto",
    page_icon="📊",
    layout="wide",
    initial_sidebar_state="expanded",
)

# 2. ESTADO PERSISTENTE EN SESIÓN
if "alertas_enviadas" not in st.session_state:
    st.session_state["alertas_enviadas"] = set()
if "historial_senales" not in st.session_state:
    st.session_state["historial_senales"] = []
if "ultimo_estado_tg" not in st.session_state:
    st.session_state["ultimo_estado_tg"] = "Aún no se ha enviado ningún mensaje."

# 3. UNIVERSO DE ACTIVOS (EXCLUSIVO FOREX, ORO Y CRIPTO)
ACTIVOS_ESCANEO = {
    "FOREX": [
        "EURUSD=X",
        "GBPUSD=X",
        "USDCHF=X",
        "USDCAD=X",
        "AUDUSD=X",
        "NZDUSD=X",
        "EURJPY=X",
        "GBPJPY=X",
        "EURGBP=X",
    ],
    "METAL": [
        "XAUUSD=X",
    ],
    "CRYPTO": [
        "BTC-USD",
        "ETH-USD",
        "SOL-USD",
        "XRP-USD",
        "ADA-USD",
        "DOGE-USD",
        "AVAX-USD",
        "LINK-USD",
        "DOT-USD",
        "BNB-USD",
    ],
}

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
        "sl_min_dist": {"5m": 4.5, "15m": 8.0, "1h": 14.0},
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


def _leer_secreto_o_env(claves: list[str]) -> str:
    for clave in claves:
        try:
            if clave in st.secrets and st.secrets[clave]:
                return str(st.secrets[clave]).strip()
        except Exception:
            pass
        val_env = os.getenv(clave, "")
        if val_env:
            return str(val_env).strip()
    return ""


def obtener_credenciales_base_telegram() -> tuple[str, str]:
    token = _leer_secreto_o_env(["TELEGRAM_TOKEN", "BOT_TOKEN", "TOKEN_TELEGRAM", "TG_TOKEN"])
    chat_id = _leer_secreto_o_env(["TELEGRAM_CHAT_ID", "CHAT_ID", "TG_CHAT_ID"])
    return token, chat_id


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


def estimar_riesgo_usd_001(symbol: str, precio: float, dist_sl: float) -> float:
    s = canonizar_simbolo(symbol)
    if s == "XAUUSD":
        return round(dist_sl * 1.0, 2)
    if s.endswith("USD"):
        pips = dist_sl / 0.0001
        return round(pips * 0.10, 2)
    if "JPY" in s and precio > 0:
        pips = dist_sl / 0.01
        valor_pip = 10.0 / precio
        return round(pips * valor_pip, 2)
    if precio > 0:
        pips = dist_sl / 0.0001
        valor_pip = 0.10 / precio
        return round(pips * valor_pip, 2)
    return 0.0


def construir_clave_alerta(symbol: str, timeframe: str, direccion: str, precio: float, id_senal: str) -> str:
    canon = canonizar_simbolo(symbol)
    decimales = 4 if detectar_tipo_activo(canon) == "FOREX" else 2
    precio_bucket = round(float(precio), decimales)
    raw = f"{canon}|{timeframe}|{direccion}|{precio_bucket}|{id_senal}"
    return hashlib.md5(raw.encode("utf-8")).hexdigest()


@st.cache_data(ttl=120, show_spinner=False)
def obtener_datos_mercado(symbol_yf: str, timeframe: str) -> pd.DataFrame | None:
    period_map = {"5m": "5d", "15m": "10d", "1h": "30d"}
    periodo = period_map.get(timeframe, "10d")
    candidatos = ["XAUUSD=X", "GC=F"] if canonizar_simbolo(symbol_yf) == "XAUUSD" else [symbol_yf]

    for ticker in candidatos:
        try:
            df = yf.download(
                tickers=ticker,
                period=periodo,
                interval=timeframe,
                progress=False,
                auto_adjust=False,
                threads=False,
            )
            if df is None or df.empty:
                continue
            if isinstance(df.columns, pd.MultiIndex):
                df.columns = [c[0] for c in df.columns]
            df = df.dropna(subset=["Close", "High", "Low"]).copy()
            if len(df) >= 35:
                return df
        except Exception:
            continue
    return None


def evaluar_estrategia_unificada(
    df: pd.DataFrame,
    symbol: str,
    timeframe: str,
    sesgo_1h: str | None = None,
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
    vol = (
        pd.to_numeric(data["Volume"], errors="coerce").fillna(0.0)
        if "Volume" in data.columns
        else pd.Series(0.0, index=data.index)
    )

    ema9 = close.ewm(span=9, adjust=False).mean()
    ema21 = close.ewm(span=21, adjust=False).mean()

    tp_series = (high + low + close) / 3.0
    if float(vol.sum()) > 0:
        vwap = (tp_series * vol).rolling(24, min_periods=1).sum() / vol.rolling(24, min_periods=1).sum().replace(0, np.nan)
        vwap = vwap.fillna(close.rolling(20, min_periods=1).mean())
    else:
        vwap = close.rolling(20, min_periods=1).mean()

    tr = pd.concat(
        [
            high - low,
            (high - close.shift(1)).abs(),
            (low - close.shift(1)).abs(),
        ],
        axis=1,
    ).max(axis=1)
    atr = tr.rolling(14, min_periods=1).mean()

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
    adx_val = float(adx.iloc[-1]) if pd.notna(adx.iloc[-1]) else 0.0
    vwap_val = float(vwap.iloc[-1]) if pd.notna(vwap.iloc[-1]) else precio
    atr_val = float(atr.iloc[-1]) if pd.notna(atr.iloc[-1]) and atr.iloc[-1] > 0 else max(precio * 0.002, tamano_pip(canon) * 8)

    adx_min = cfg["adx_min"].get(timeframe, 28.0)
    if adx_val < adx_min:
        return None

    alcista_base = (precio >= vwap_val) and (float(ema9.iloc[-1]) >= float(ema21.iloc[-1]))
    bajista_base = (precio <= vwap_val) and (float(ema9.iloc[-1]) <= float(ema21.iloc[-1]))

    if alcista_base and not bajista_base:
        direccion = "COMPRA"
    elif bajista_base and not alcista_base:
        direccion = "VENTA"
    else:
        return None

    if cfg.get("exigir_filtro_1h") and timeframe in {"5m", "15m"} and sesgo_1h in {"COMPRA", "VENTA"}:
        if direccion != sesgo_1h:
            return None

    fvg_alcista = any(float(low.iloc[i]) > float(high.iloc[i - 2]) for i in range(max(2, len(data) - 5), len(data)))
    fvg_bajista = any(float(high.iloc[i]) < float(low.iloc[i - 2]) for i in range(max(2, len(data) - 5), len(data)))
    swing_max_prev = float(high.iloc[-10:-1].max()) if len(data) >= 11 else float(high.iloc[:-1].max())
    swing_min_prev = float(low.iloc[-10:-1].min()) if len(data) >= 11 else float(low.iloc[:-1].min())

    confluencias = 0
    detalles = []

    if tipo_activo in {"FOREX", "METAL"}:
        confluencias += 1
        detalles.append("Estructura/VWAP/EMA alineada")

        impulso_ok = (
            (precio > swing_max_prev and direccion == "COMPRA")
            or (precio < swing_min_prev and direccion == "VENTA")
            or (abs(float(close.iloc[-1]) - float(open_p.iloc[-1])) >= atr_val * 0.45)
        )
        if impulso_ok:
            confluencias += 1
            detalles.append("Impulso/ruptura estructural")

        cerca_ema21 = abs(precio - float(ema21.iloc[-1])) <= atr_val * 1.25
        zona_ok = (direccion == "COMPRA" and (fvg_alcista or cerca_ema21)) or (
            direccion == "VENTA" and (fvg_bajista or cerca_ema21)
        )
        if zona_ok:
            confluencias += 1
            detalles.append("FVG / retroceso a zona")
    else:
        vol_ma = float(vol.rolling(20, min_periods=1).mean().iloc[-1])
        vol_actual = float(vol.iloc[-1])
        rango_vela = (high - low).replace(0, np.nan)
        cvd = ((close - open_p) / rango_vela).fillna(0.0) * vol
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
        dist_min = cfg["sl_min_pips"].get(timeframe, 10.0) * pip
    elif tipo_activo == "METAL":
        dist_min = cfg["sl_min_dist"].get(timeframe, 8.0)
    else:
        dist_min = precio * cfg["sl_min_pct"].get(timeframe, 0.008)

    dist_final = max(dist_sl, dist_min)
    decimales = 5 if tipo_activo == "FOREX" else 2

    if direccion == "COMPRA":
        sl_final = round(precio - dist_final, decimales)
        tp_final = round(precio + (dist_final * rr), decimales)
    else:
        sl_final = round(precio + dist_final, decimales)
        tp_final = round(precio - (dist_final * rr), decimales)

    pips_o_pts = round(dist_final / pip, 1)
    riesgo_usd_001 = estimar_riesgo_usd_001(canon, precio, dist_final)

    id_senal = f"{canon}_{timeframe}_{direccion}_{confluencias}de3"
    clave_unica = construir_clave_alerta(canon, timeframe, direccion, precio, id_senal)

    return {
        "Activo": canon,
        "Mercado": tipo_activo,
        "TF": timeframe,
        "Dirección": direccion,
        "Confluencia": f"{confluencias}/3",
        "confluencias_num": confluencias,
        "Precio": round(precio, decimales),
        "VWAP": round(vwap_val, decimales),
        "ADX": round(adx_val, 1),
        "ADX_Min": adx_min,
        "SL": sl_final,
        "TP": tp_final,
        "Dist_SL_Pips_Pts": pips_o_pts,
        "Riesgo_Est_001_USD": riesgo_usd_001,
        "Detalles": " + ".join(detalles),
        "clave_unica": clave_unica,
        "Hora_UTC": datetime.now(timezone.utc).strftime("%H:%M:%S"),
    }


def enviar_mensaje_telegram(texto: str, token: str, chat_id: str) -> tuple[bool, str]:
    token_limpio = (token or "").strip()
    chat_limpio = (chat_id or "").strip()
    if not token_limpio or not chat_limpio:
        return False, "Falta Token o Chat ID."
    url = f"https://api.telegram.org/bot{token_limpio}/sendMessage"
    try:
        resp = requests.post(
            url,
            json={"chat_id": chat_limpio, "text": texto, "parse_mode": "Markdown"},
            timeout=10,
        )
        if resp.status_code == 200:
            return True, "Enviado correctamente."
        return False, f"Error Telegram ({resp.status_code}): {resp.text[:180]}"
    except Exception as e:
        return False, f"Error de conexión: {e}"


def enviar_alerta_telegram(senal: dict, token: str, chat_id: str) -> tuple[bool, str]:
    icono = "🟢" if senal["Dirección"] == "COMPRA" else "🔴"
    unidad = "pips" if senal["Mercado"] == "FOREX" else "pts"
    texto = (
        f"🏛️ *SEÑAL CONJUNTA UNIFICADA*\n"
        f"🌐 Mercado: *{senal['Mercado']}* | Activo: *{senal['Activo']}* | TF: *{senal['TF']}*\n"
        f"💲 Precio Ref: `{senal['Precio']}` | ADX: `{senal['ADX']}` (>{senal['ADX_Min']})\n"
        f"🎯 Confluencia: *{senal['Confluencia']}* ({senal['Detalles']})\n"
        f"🧭 Acción: *{senal['Dirección']}* {icono}\n"
        f"🛡️ SL Ref: `{senal['SL']}` ({senal['Dist_SL_Pips_Pts']} {unidad})\n"
        f"🎯 TP Ref (1:2.5): `{senal['TP']}`\n"
        f"💵 Riesgo est. 0.01 lotes: `~${senal['Riesgo_Est_001_USD']} USD` ({senal.get('Riesgo_%_Cuenta', 0)}% saldo)\n"
        f"⚠️ Valida spread, margen libre y tamaño de cuenta antes de ejecutar."
    )
    return enviar_mensaje_telegram(texto, token, chat_id)


# 4. INTERFAZ VISUAL
st.title("📊 Terminal Cuantitativa — Forex, Oro y Cripto")
st.caption("Escaneo separado por mercado, deduplicación canónica, control de SL y alertas a Telegram.")

with st.sidebar:
    st.header("⚙️ Configuración de Escaneo")
    mercados_activos = st.multiselect(
        "Mercados a escanear",
        options=["FOREX", "METAL", "CRYPTO"],
        default=["FOREX", "METAL", "CRYPTO"],
    )
    tfs_activos = st.multiselect(
        "Temporalidades",
        options=["1h", "15m", "5m"],
        default=["1h", "15m", "5m"],
    )
    min_conf = st.radio("Confluencia mínima", options=[2, 3], index=0, format_func=lambda x: f"{x}/3")
    saldo_cuenta = st.number_input("Saldo de referencia (USD)", min_value=5.0, max_value=100000.0, value=10.0, step=1.0)
    riesgo_max_pct = st.slider("Límite de riesgo 0.01 lotes (% saldo)", min_value=2, max_value=50, value=10)

    st.divider()
    st.subheader("📲 Alertas Telegram")
    base_token, base_chat = obtener_credenciales_base_telegram()

    # Campos directos siempre disponibles para ingresar o corregir Token y Chat ID al instante
    token_tg = st.text_input(
        "Token del Bot (BotFather)",
        value=base_token,
        type="password",
        placeholder="123456789:AA...",
    )
    chat_tg = st.text_input(
        "Chat ID",
        value=base_chat,
        placeholder="Ej: 123456789 o -100123456789",
    )

    tiene_creds = bool((token_tg or "").strip() and (chat_tg or "").strip())
    enviar_tg = st.checkbox(
        "Enviar alertas a Telegram",
        value=tiene_creds,
        disabled=not tiene_creds,
    )
    solo_aptas_tg = st.checkbox(
        "Solo enviar señales dentro del límite de saldo",
        value=False,
        disabled=not tiene_creds,
    )

    if st.button("✉️ Probar envío a Telegram", use_container_width=True, disabled=not tiene_creds):
        ok_test, msg_test = enviar_mensaje_telegram(
            "✅ *Prueba de conexión Telegram:* Terminal Cuantitativa conectada y lista.",
            token_tg,
            chat_tg,
        )
        st.session_state["ultimo_estado_tg"] = msg_test
        if ok_test:
            st.success("Mensaje enviado a Telegram.")
        else:
            st.error(msg_test)

    st.caption(f"Estado TG: {st.session_state['ultimo_estado_tg']}")

    st.divider()
    auto_refresh = st.checkbox("Auto-actualizar cada 60s", value=False)
    st.button("🔄 Escanear ahora", type="primary", use_container_width=True)

# 5. EJECUCIÓN DEL ESCANEO
senales_actuales = []
errores_carga = 0

if mercados_activos and tfs_activos:
    simbolos_a_revisar = []
    for m in mercados_activos:
        simbolos_a_revisar.extend(ACTIVOS_ESCANEO.get(m, []))

    barra = st.progress(0, text="Analizando mercados seleccionados...")
    total_pasos = max(1, len(simbolos_a_revisar))
    orden_tf = [tf for tf in ["1h", "15m", "5m"] if tf in tfs_activos]

    for idx, sym in enumerate(simbolos_a_revisar):
        canon = canonizar_simbolo(sym)
        sesgo_1h = None

        if detectar_tipo_activo(canon) in {"FOREX", "METAL"} and "1h" not in orden_tf:
            df_1h_ref = obtener_datos_mercado(sym, "1h")
            res_1h_ref = evaluar_estrategia_unificada(df_1h_ref, sym, "1h", sesgo_1h=None)
            if res_1h_ref:
                sesgo_1h = res_1h_ref["Dirección"]

        for tf in orden_tf:
            df_tf = obtener_datos_mercado(sym, tf)
            if df_tf is None:
                errores_carga += 1
                continue

            res = evaluar_estrategia_unificada(df_tf, sym, tf, sesgo_1h=sesgo_1h)
            if res:
                if tf == "1h":
                    sesgo_1h = res["Dirección"]

                if res["confluencias_num"] >= min_conf:
                    riesgo_pct_cuenta = (res["Riesgo_Est_001_USD"] / saldo_cuenta) * 100.0 if saldo_cuenta > 0 else 0.0
                    apto = riesgo_pct_cuenta <= riesgo_max_pct
                    res["Riesgo_%_Cuenta"] = round(riesgo_pct_cuenta, 1)
                    res["Apto_Saldo"] = "✅ Dentro de límite" if apto else "⚠️ SL alto para saldo"
                    senales_actuales.append(res)

                    cumple_filtro_tg = (not solo_aptas_tg) or apto
                    if enviar_tg and cumple_filtro_tg and res["clave_unica"] not in st.session_state["alertas_enviadas"]:
                        ok, msg_env = enviar_alerta_telegram(res, token_tg, chat_tg)
                        st.session_state["ultimo_estado_tg"] = msg_env
                        if ok:
                            st.session_state["alertas_enviadas"].add(res["clave_unica"])
                            st.session_state["historial_senales"].insert(0, res)

        barra.progress((idx + 1) / total_pasos, text=f"Escaneando {canon} ({idx + 1}/{total_pasos})...")

    barra.empty()

# 6. TABLERO DE RESULTADOS
col1, col2, col3, col4 = st.columns(4)
col1.metric("Señales activas", len(senales_actuales))
col2.metric("Señales 3/3", sum(1 for s in senales_actuales if s["confluencias_num"] == 3))
col3.metric("Alertas enviadas (sesión)", len(st.session_state["alertas_enviadas"]))
col4.metric("Saldo referencia", f"${saldo_cuenta:.2f} USD")

if senales_actuales:
    df_res = pd.DataFrame(senales_actuales)[
        [
            "Hora_UTC",
            "Mercado",
            "Activo",
            "TF",
            "Dirección",
            "Confluencia",
            "Precio",
            "SL",
            "TP",
            "Dist_SL_Pips_Pts",
            "Riesgo_Est_001_USD",
            "Riesgo_%_Cuenta",
            "Apto_Saldo",
            "ADX",
            "Detalles",
        ]
    ]
    st.subheader("📡 Señales Detectadas")
    st.dataframe(df_res, use_container_width=True, hide_index=True)
else:
    st.info("No hay señales que cumplan los filtros actuales en este ciclo.")

if errores_carga > 0:
    st.caption(f"Nota: {errores_carga} consultas de velas no devolvieron datos en este ciclo y fueron omitidas sin detener la app.")

if auto_refresh:
    time.sleep(60)
    st.rerun()
