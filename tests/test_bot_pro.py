# -*- coding: utf-8 -*-
"""Tests de bot_pro.py con datos sinteticos: sin red y sin Telegram real.  Correr:  python -m pytest -q"""
import os
import sys
from datetime import datetime

import numpy as np
import pandas as pd
import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import bot_pro as b  # noqa: E402


# ----------------------------------------------------------------- utilidades
class RespuestaFalsa:
    def __init__(self, ok=True, status=200, js=None):
        self.ok, self.status_code, self._js, self.text = ok, status, js or {}, ""

    def json(self):
        return self._js


@pytest.fixture
def telegram(monkeypatch):
    """Sustituye la API de Telegram: guarda cada mensaje enviado y no duerme."""
    enviados = []
    monkeypatch.setenv("TELEGRAM_TOKEN", "x")
    monkeypatch.setenv("TELEGRAM_CHAT_IDS", "1")
    monkeypatch.setattr(b, "_dormir", lambda s: None)
    monkeypatch.setattr(b.requests, "post", lambda url, json, timeout: enviados.append(json["text"]) or RespuestaFalsa())
    return enviados


@pytest.fixture
def estado(tmp_path):
    e = b.Estado(str(tmp_path / "estado.json"))
    e.agrupar = True
    return e


def senal(activo="EUR/USD", bias=1, ent=1.1000, sl=1.0980, tp=1.1040, est="Barrido", puntaje=3, ident=None):
    return {"estrategia": est, "clave": est, "activo": activo, "tipo": b.ACTIVOS[activo]["tipo"], "bias": bias,
            "dir": "LONG" if bias > 0 else "SHORT", "entrada": ent, "sl": sl, "tp1": tp, "tp2": None,
            "riesgo_usd": 0.2, "tamano_txt": "0.01 lotes", "score": 1, "score_max": 1, "confluencias": "-",
            "kz": "Londres", "vela": pd.Timestamp("2026-11-23 08:00", tz="UTC"), "puntaje": puntaje,
            "votos": ["peso propio 1"], "id": ident or f"{est}|{activo}|{bias}|{ent}"}


P = dict(b.PARAMS_DEFAULT)


# ----------------------------------------------------------------- Telegram
def test_trozos_respeta_el_limite():
    texto = "\n".join(f"linea {i} " + "x" * 80 for i in range(200))
    partes = b.trozos_telegram(texto)
    assert len(partes) > 1
    assert all(len(p) <= b.TG_MAX for p in partes)
    assert "".join(p.replace("\n", "") for p in partes) == texto.replace("\n", "")


def test_trozos_linea_gigante():
    partes = b.trozos_telegram("y" * (b.TG_MAX * 2 + 10))
    assert [len(p) for p in partes] == [b.TG_MAX, b.TG_MAX, 10]


def test_reintenta_si_telegram_pide_esperar(monkeypatch):
    monkeypatch.setenv("TELEGRAM_TOKEN", "x")
    monkeypatch.setenv("TELEGRAM_CHAT_IDS", "1")
    esperas, respuestas = [], [RespuestaFalsa(False, 429, {"parameters": {"retry_after": 3}}), RespuestaFalsa()]
    monkeypatch.setattr(b, "_dormir", esperas.append)
    monkeypatch.setattr(b.requests, "post", lambda *a, **k: respuestas.pop(0))
    ok, _ = b.enviar_telegram("hola")
    assert ok and 3.5 in esperas


# ----------------------------------------------------------------- comite y cartera
def test_letras_de_nivel():
    assert [b.letra_nivel(x) for x in (0, 1, 2, 3, 4, 5, 9)] == ["C", "C", "B", "A", "A", "A+", "A+"]


def test_ratio_rr():
    assert b.ratio_rr(senal()) == pytest.approx(2.0)
    assert b.ratio_rr({**senal(), "tp1": None}) is None


def test_exposicion_detecta_la_misma_apuesta():
    assert b.exposicion("EUR/USD", 1) == ["USD-"]
    assert b.exposicion("GBP/USD", 1) == ["USD-"]
    assert b.exposicion("USD/JPY", -1) == ["USD-"]
    assert b.exposicion("USD/JPY", 1) == ["USD+"]
    assert b.exposicion("S&P 500 (ES)", 1) == b.exposicion("DOW 30 (YM)", 1) == ["INDICES_USA+"]


def test_nivel_c_va_a_observacion(telegram, estado):
    assert b.decidir_senal(senal(puntaje=1), "15m", P, estado) == "observar"
    assert estado.cola == [] and len(estado.d["observadas"]) == 1
    assert b.decidir_senal(senal(puntaje=1), "15m", P, estado) == "repetida"     # no se duplica


def test_ratio_bajo_va_a_observacion(telegram, estado):
    s = senal(tp=1.1010)                                  # 1:0,5
    assert b.decidir_senal(s, "15m", P, estado) == "observar"
    assert "ratio bajo" in estado.d["observadas"][0]["motivo"]


def test_riesgo_segun_nivel(telegram, estado):
    for pts, letra in ((5, "A+"), (3, "A"), (2, "B")):
        s = senal(puntaje=pts, activo="BTC/USDT", ent=90000, sl=89500, tp=91000, est=f"E{pts}")
        estado.d["abiertas"] = []
        b.decidir_senal(s, "15m", P, estado)
        assert s["nivel"] == letra
        assert s["riesgo_pct"] == b.CUENTA["riesgo_nivel"][letra]
        assert s["riesgo_usd"] == pytest.approx(P["banco"] * s["riesgo_pct"] / 100)


def test_un_solo_mensaje_por_escaneo(telegram, estado):
    activos = ["EUR/USD", "BTC/USDT", "XAU/USD (Oro)"]
    for i, a in enumerate(activos):
        px = {"EUR/USD": 1.1, "BTC/USDT": 90000.0, "XAU/USD (Oro)": 4000.0}[a]
        s = senal(activo=a, ent=px, sl=px * 0.998, tp=px * 1.004, puntaje=3, est=f"E{i}")
        assert b.decidir_senal(s, "15m", P, estado) == "enviar"
    assert telegram == []                                  # nada sale hasta vaciar la cola
    assert estado.vaciar_cola() == 3
    assert len(telegram) == 1 and telegram[0].startswith("🚨 3 señales nuevas")
    assert len(estado.d["abiertas"]) == 3
    assert b.decidir_senal(senal(activo="EUR/USD", est="E0", ent=1.1, sl=1.1 * 0.998, tp=1.1 * 1.004),
                           "15m", P, estado) == "repetida"


def test_si_telegram_falla_se_deshace_el_registro(monkeypatch, estado):
    monkeypatch.setenv("TELEGRAM_TOKEN", "x")
    monkeypatch.setenv("TELEGRAM_CHAT_IDS", "1")
    monkeypatch.setattr(b, "_dormir", lambda s: None)
    monkeypatch.setattr(b.requests, "post", lambda *a, **k: RespuestaFalsa(False, 500))
    assert b.decidir_senal(senal(), "15m", P, estado) == "enviar"
    assert len(estado.d["abiertas"]) == 1
    assert estado.vaciar_cola() == 0
    assert estado.d["abiertas"] == [] and estado.d["ids"] == []     # podra reenviarse en la siguiente pasada


def test_cola_grande_se_parte_en_varios_mensajes(telegram, estado, monkeypatch):
    monkeypatch.setitem(b.CUENTA, "max_abiertas", 99)
    monkeypatch.setitem(b.CUENTA, "cupos", {"A+": 99, "A": 99, "B": 99})
    monkeypatch.setitem(b.CUENTA, "riesgo_total_max_pct", 999)
    monkeypatch.setitem(b.CUENTA, "max_por_exposicion", 0)
    monkeypatch.setitem(b.CUENTA, "una_por_mercado", False)
    for i in range(30):
        s = senal(est=f"E{i}", ident=f"id{i}")
        s["votos"] = ["voto largo " + "z" * 150] * 6
        b.decidir_senal(s, "15m", P, estado)
    assert estado.vaciar_cola() == 30
    assert len(telegram) > 1 and all(len(m) <= 4096 for m in telegram)


def test_cupo_por_nivel(telegram, estado, monkeypatch):
    monkeypatch.setitem(b.CUENTA, "cupos", {"A+": 5, "A": 1, "B": 5})
    monkeypatch.setitem(b.CUENTA, "una_por_mercado", False)
    assert b.decidir_senal(senal(est="E1", activo="BTC/USDT", ent=90000, sl=89500, tp=91000), "15m", P, estado) == "enviar"
    assert b.decidir_senal(senal(est="E2", activo="XAU/USD (Oro)", ent=4000, sl=3990, tp=4020), "15m", P,
                           estado) == "observar"
    assert "cupo del nivel A" in estado.d["observadas"][-1]["motivo"]


def test_tope_de_riesgo_total(telegram, estado, monkeypatch):
    monkeypatch.setitem(b.CUENTA, "riesgo_total_max_pct", 3.0)          # 2% (A+) + 2% (A+) > 3%
    s1 = senal(est="E1", activo="BTC/USDT", ent=90000, sl=89500, tp=91000, puntaje=5)
    s2 = senal(est="E2", activo="XAU/USD (Oro)", ent=4000, sl=3990, tp=4020, puntaje=5)
    assert b.decidir_senal(s1, "15m", P, estado) == "enviar"
    assert b.decidir_senal(s2, "15m", P, estado) == "observar"
    assert "riesgo total" in estado.d["observadas"][-1]["motivo"]


def test_exposicion_correlacionada(telegram, estado, monkeypatch):
    monkeypatch.setitem(b.CUENTA, "max_por_exposicion", 1)
    assert b.decidir_senal(senal(est="E1", activo="EUR/USD"), "15m", P, estado) == "enviar"
    s = senal(est="E2", activo="GBP/USD", ent=1.30, sl=1.298, tp=1.304)
    assert b.decidir_senal(s, "15m", P, estado) == "observar"        # tambien apuesta contra el USD
    assert "USD-" in estado.d["observadas"][-1]["motivo"]


def test_resumen_de_observacion(telegram, estado):
    b.decidir_senal(senal(puntaje=1, est="E1"), "15m", P, estado)
    b.decidir_senal(senal(tp=1.1005, est="E2"), "15m", P, estado)
    txt = b.texto_observadas(estado)
    assert "2 señales" in txt and "nivel C: 1" in txt


# ----------------------------------------------------------------- horarios
@pytest.mark.parametrize("cuando,tipo,abierto", [
    ("2026-11-21 12:00", "forex", False),     # sabado
    ("2026-11-22 16:00", "forex", False),     # domingo antes de las 17 NY
    ("2026-11-22 17:30", "forex", True),
    ("2026-11-23 17:30", "oro", False),       # pausa diaria CME 17-18 NY
    ("2026-11-23 18:30", "oro", True),
    ("2026-11-21 12:00", "cripto", True),
])
def test_mercado_abierto(cuando, tipo, abierto):
    t = pd.Timestamp(cuando, tz="America/New_York").to_pydatetime()
    assert b.mercado_abierto(tipo, t) is abierto


# ----------------------------------------------------------------- maquina del tiempo
def _velas(n=2000, deriva=0.0, semilla=1):
    rng = np.random.default_rng(semilla)
    idx = pd.date_range("2026-09-01", periods=n, freq="15min", tz="UTC")
    c = 100 * np.exp(np.cumsum(rng.normal(deriva, 0.002, n)))
    o = np.r_[100, c[:-1]]
    h = np.maximum(o, c) * (1 + abs(rng.normal(0, 0.001, n)))
    l = np.minimum(o, c) * (1 - abs(rng.normal(0, 0.001, n)))
    return pd.DataFrame(dict(open=o, high=h, low=l, close=c), index=idx)


def test_maquina_del_tiempo_proporciones():
    df = _velas()
    px = df["close"].iloc[-1]
    tm = b.maquina_del_tiempo(df, {"bias": 1, "entrada": px, "sl": px * 0.996, "tp1": px * 1.004, "max_barras": 96},
                              n=2000)
    assert tm["tp"] + tm["sl"] + tm["abierta"] == pytest.approx(1.0, abs=1e-9)
    assert 0.35 < tm["tp"] < 0.65                         # 1:1 sin tendencia ~ moneda al aire
    assert b.veredicto_tm(tm) == "MONEDA"


def test_maquina_del_tiempo_sin_datos():
    assert b.maquina_del_tiempo(_velas(100), {"bias": 1, "entrada": 1, "sl": 0.9}) is None


# ----------------------------------------------------------------- backtest del comite
def _op(activo, t, salida, R=1.0, bias=1, est="Barrido", clave="Barrido", lectura="NEUTRAL", rr=2.0):
    return {"estrategia": est, "clave": clave, "activo": activo, "bias": bias, "t_utc": t, "salida_utc": salida,
            "R": R, "R_bruto": R + 0.05, "coste_R": 0.05, "lectura": lectura, "rr": rr}


def test_puntuar_comite_aliados_y_conflicto():
    t = pd.Timestamp("2026-01-05 10:00", tz="UTC")
    ops = pd.DataFrame([_op("EUR/USD", t.isoformat(), "x")])
    votos = [{"t": t, "estrategia": "Barrido", "bias": 1}, {"t": t, "estrategia": "SMC", "bias": 1},
             {"t": t, "estrategia": "Judas", "bias": 1}]
    r = b.puntuar_comite(ops, votos, "EUR/USD", "15m").iloc[0]
    assert (r["peso"], r["aliados"], r["puntaje"], r["nivel"]) == (1, 2, 3, "A")
    votos.append({"t": t, "estrategia": "RSI2", "bias": -1})                   # voto contrario: -2
    r = b.puntuar_comite(ops, votos, "EUR/USD", "15m").iloc[0]
    assert (r["puntaje"], r["nivel"], bool(r["contraria"])) == (1, "C", True)


def test_simular_cartera_aplica_las_reglas(monkeypatch):
    monkeypatch.setitem(b.CUENTA, "riesgo_total_max_pct", 3.0)
    ops = pd.DataFrame([
        {**_op("BTC/USDT", "2026-01-01T10:00", "2026-01-01T12:00", R=2.0), "nivel": "A+"},     # tomada: +4%
        {**_op("XAU/USD (Oro)", "2026-01-01T10:30", "2026-01-01T11:00", R=-1.0), "nivel": "A+"},  # tope 3%
        {**_op("EUR/USD", "2026-01-01T10:45", "2026-01-01T13:00", R=-1.0), "nivel": "A"},     # tomada: -1%
        {**_op("GBP/USD", "2026-01-01T11:00", "2026-01-01T13:00", R=1.0), "nivel": "C"},      # nivel C
        {**_op("USD/JPY", "2026-01-01T11:00", "2026-01-01T13:00", R=1.0, rr=1.0), "nivel": "A"},  # ratio bajo
    ])
    res, tm = b.simular_cartera(ops)
    assert list(tm["activo"]) == ["BTC/USDT", "EUR/USD"]
    assert res["Retorno % (suma)"] == pytest.approx(2 * 2.0 - 1 * 1.0)
    assert res["Rechazadas"] == {"tope de riesgo total": 1, "nivel C": 1, "ratio bajo": 1}


# ----------------------------------------------------------------- analista en el chat
def _serie(precios, freq="15min"):
    c = np.asarray(precios, float)
    idx = pd.date_range("2026-11-01", periods=len(c), freq=freq, tz="UTC")
    o = np.r_[c[0], c[:-1]]
    return pd.DataFrame(dict(open=o, high=np.maximum(o, c) + 0.05, low=np.minimum(o, c) - 0.05, close=c,
                             volume=1.0), index=idx)


def test_detecta_doble_techo():
    subida = np.linspace(100, 110, 40)
    techo1 = np.r_[np.linspace(110, 104, 15), np.linspace(104, 110, 15)]
    caida = np.linspace(110, 102, 30)
    figs = b.figuras_chartistas(_serie(np.r_[subida, techo1, caida]))
    dt = [f for f in figs if f["figura"] == "Doble techo"]
    assert dt and dt[0]["estado"] == "confirmada" and dt[0]["objetivo"] < dt[0]["cuello"] < dt[0]["invalidacion"]


def test_busca_activo_y_temporalidad():
    assert b.buscar_activo("¿pa dónde va el oro?") == "XAU/USD (Oro)"
    assert b.buscar_activo("/plan sp500") == "S&P 500 (ES)"
    assert b.buscar_activo("hay un doble techo en el euro") == "EUR/USD"
    assert b.buscar_tf("/lectura btc 5m") == "5m" and b.buscar_tf("/lectura btc h1") == "1h"


def test_chat_responde_sin_red(monkeypatch):
    df = _velas(800, deriva=0.0003, semilla=7)
    monkeypatch.setattr(b, "noticia_cercana", lambda *a, **k: None)
    cargar = lambda nm, tf: df
    e = None
    assert "LECTURA" in b.responder_chat("¿pa dónde va el oro?", e, cargar)
    assert "Escenarios" in b.responder_chat("/escenarios btc", e, cargar)
    assert "PLAN" in b.responder_chat("/plan dax", e, cargar)
    assert "POSICIÓN LARGA" in b.responder_chat("/posicion oro long 100 99", e, cargar)
    assert "No reconozco" in b.responder_chat("/lectura zzz", e, cargar)


def test_chat_ignora_chats_no_autorizados(monkeypatch, estado):
    monkeypatch.setenv("TELEGRAM_TOKEN", "x")
    monkeypatch.setenv("TELEGRAM_CHAT_IDS", "1")
    upd = {"ok": True, "result": [{"update_id": 7, "message": {"chat": {"id": 999}, "text": "/ayuda"}}]}
    monkeypatch.setattr(b.requests, "get", lambda *a, **k: RespuestaFalsa(js=upd))
    enviados = []
    monkeypatch.setattr(b, "_post_telegram", lambda tok, chat, txt: enviados.append(chat))
    assert b.atender_telegram(estado) == 0 and enviados == [] and estado.d["tg_offset"] == 8


def test_modo_ganadoras_usa_niveles_y_cupos(telegram, estado, monkeypatch):
    """Una GANADORA robusta sale como nivel A (1%), con confirmacion A+ (2%); una experimental como B (0,5%)."""
    df = _velas(800, semilla=3)
    px = float(df["close"].iloc[-1])
    rob = {**senal(activo="XAU/USD (Oro)", ent=px, sl=px * 0.995, tp=px * 1.01, est="VWAP"), "clave": "VWAP"}
    exp = {**senal(activo="USD/JPY", ent=150.0, sl=149.7, tp=150.6, est="AuraPlus"), "clave": "AuraPlus"}
    rob.pop("puntaje"), exp.pop("puntaje")
    salidas = {"XAU/USD (Oro)": [rob], "USD/JPY": [exp]}
    monkeypatch.setattr(b, "analizar", lambda nm, tf, p, cargar=None: ({}, [dict(x) for x in salidas.get(nm, [])]))
    n, _ = b.escanear("15m", dict(b.PARAMS_DEFAULT), estado, verbose=False)
    assert n == 2
    niveles = {x["s"]["activo"]: (x["s"]["nivel"], x["s"]["riesgo_pct"]) for x in estado.cola}
    assert niveles == {"XAU/USD (Oro)": ("A", 1.0), "USD/JPY": ("B", 0.5)}


def test_hay_version_nueva(monkeypatch):
    monkeypatch.setenv("GITHUB_SHA", "a" * 40)
    monkeypatch.setenv("GITHUB_REPOSITORY", "x/y")
    monkeypatch.setenv("GITHUB_REF_NAME", "main")
    resp = RespuestaFalsa()
    resp.text = "b" * 40
    monkeypatch.setattr(b.requests, "get", lambda *a, **k: resp)
    b._VERSION["ultima"] = 0
    assert b.hay_version_nueva() is True
    assert b.hay_version_nueva() is False                 # no vuelve a mirar hasta pasados 10 min
    b._VERSION["ultima"] = 0
    resp.text = "a" * 40
    assert b.hay_version_nueva() is False                 # misma version: sigue
    monkeypatch.delenv("GITHUB_SHA")
    b._VERSION["ultima"] = 0
    assert b.hay_version_nueva() is False                 # fuera de GitHub Actions nunca corta
