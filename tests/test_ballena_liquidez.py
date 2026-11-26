# -*- coding: utf-8 -*-
"""Tests de Ballena Liquidez con un escenario construido a mano (sin red ni Telegram).  python -m pytest -q"""
import os
import subprocess
import sys

import numpy as np
import pandas as pd
import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import ballena_liquidez as bl  # noqa: E402

DIAS = pd.date_range("2026-11-21", periods=4, freq="1D", tz="UTC")
INI = DIAS[-1] + pd.Timedelta(days=1)                       # comienzo del dia en curso
# Hoy: subida con minimos ascendentes que supera el PDH (1.3245), maximo 1.3258 y caida que rompe la linea
HOY = [1.3200, 1.3208, 1.3216, 1.3210, 1.3206, 1.3214, 1.3224, 1.3232, 1.3226, 1.3221, 1.3229, 1.3238, 1.3246,
       1.3240, 1.3236, 1.3244, 1.3252, 1.3258, 1.3250, 1.3243, 1.3237, 1.3230]


def diario(pdl=1.3150, bajista=True):
    d = pd.DataFrame({"open": [1.334, 1.330, 1.328, 1.326], "high": [1.336, 1.333, 1.3260, 1.3245],
                      "low": [1.328, 1.326, 1.322, pdl], "close": [1.330, 1.327, 1.3235, 1.3210], "volume": 1.0},
                     index=DIAS)
    return d if bajista else espejo(d)


def m5(hoy=HOY):
    c = np.array([1.3210] * 50 + list(hoy))
    o = np.r_[c[0], c[:-1]]
    idx = pd.date_range(end=INI + pd.Timedelta(minutes=5 * (len(hoy) - 1)), periods=len(c), freq="5min")
    return pd.DataFrame(dict(open=o, high=np.maximum(o, c) + 0.0002, low=np.minimum(o, c) - 0.0002, close=c,
                             volume=1.0), index=idx)


def espejo(df):
    return pd.DataFrame({"open": 2.6 - df["open"], "high": 2.6 - df["low"], "low": 2.6 - df["high"],
                         "close": 2.6 - df["close"], "volume": 1.0}, index=df.index)


def test_tendencia_diaria():
    assert bl.tendencia_diaria(diario())[0] == -1
    assert bl.tendencia_diaria(espejo(diario()))[0] == 1


def test_venta_tras_tomar_el_pdh_y_romper_la_linea():
    s, motivo = bl.senal_liquidez(m5(), diario())
    assert s and s["dir"] == "SHORT", motivo
    assert s["nivel"] == pytest.approx(1.3245) and s["barrido"] > s["nivel"]       # tomo la liquidez del PDH
    assert s["tp1"] < s["entrada"] < s["nivel"] < s["sl"]
    assert s["rr"] >= bl.CONFIG["rr_min"]


def test_sin_toma_de_liquidez_no_hay_operacion():
    hoy = [x - 0.0030 for x in HOY]                         # nunca supera el PDH
    s, motivo = bl.senal_liquidez(m5(hoy), diario())
    assert s is None and "sin toma de liquidez" in motivo


def test_rr_insuficiente_se_descarta():
    s, motivo = bl.senal_liquidez(m5(), diario(pdl=1.3195))   # la liquidez esta demasiado cerca
    assert s is None and "R:R" in motivo


def test_compra_es_espejo():
    s, motivo = bl.senal_liquidez(espejo(m5()), diario(bajista=False))
    assert s and s["dir"] == "LONG", motivo
    assert s["sl"] < s["nivel"] < s["entrada"] < s["tp1"]


def test_una_senal_por_dia(monkeypatch, tmp_path):
    datos, d1 = m5(), diario()
    monkeypatch.setattr(bl, "MERCADOS", {"GBP/USD": {"tipo": "forex", "yf": "x"}})
    enviados = []
    monkeypatch.setattr(bl, "enviar_telegram", lambda txt, png=None: enviados.append(txt) or (True, "ok"))
    e = bl.Estado(str(tmp_path / "e.json"))
    monkeypatch.setattr(bl, "confluencia_1h_15m", lambda h1, m15, ini, nivel, bias: (True, True))
    cargar = lambda nm, tf: {"5m": datos, "1d": d1, "1h": datos, "15m": datos}[tf]
    ahora = datos.index[-1] + pd.Timedelta(minutes=5)
    assert bl.escanear(e, cargar, ahora, verbose=False) == 1
    assert enviados[0].startswith("🚨 🔴 VENTA GBP/USD · LIQUIDEZ 5M · ⭐ CONFLUENCIA 1H + 15M")
    assert "tomó el PDH 1.32450" in enviados[0]
    assert bl.escanear(e, cargar, ahora, verbose=False) == 0


def test_nombre_y_arranque():
    assert bl.ENCABEZADO == "💧🔁 BALLENA LIQUIDEZ"
    assert list(bl.MERCADOS) == ["XAU/USD (Oro)", "BTC/USD", "NASDAQ 100"]
    raiz = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    r = subprocess.run([sys.executable, os.path.join(raiz, "ballena_liquidez.py"), "--help"],
                       capture_output=True, text=True)
    assert r.returncode == 0 and "--avisar-estado" in r.stdout



# ----------------------------------------------------------------- confluencia 1H + 15M
def _v(filas, inicio, freq):
    o, h, l, c = (np.array(x, float) for x in zip(*filas))
    return pd.DataFrame(dict(open=o, high=h, low=l, close=c, volume=1.0),
                        index=pd.date_range(inicio, periods=len(filas), freq=freq, tz="UTC"))


def test_confluencia_1h_rechazo_y_15m_estructura():
    h1 = _v([(1.320, 1.3248, 1.3195, 1.3240)], INI, "1h")                       # supero el PDH y cerro debajo
    m15 = _v([(1.3200, 1.3212, 1.3198, 1.3210), (1.3210, 1.3215, 1.3205, 1.3212), (1.3212, 1.3222, 1.3208, 1.3220),
              (1.3220, 1.3224, 1.3203, 1.3215),                                     # minimo de la subida 1.3203
              (1.3215, 1.3232, 1.3212, 1.3230), (1.3230, 1.3240, 1.3226, 1.3238), (1.3238, 1.3258, 1.3236, 1.3250),
              (1.3250, 1.3251, 1.3215, 1.3218), (1.3218, 1.3220, 1.3195, 1.3198)],   # cierra bajo 1.3203
             INI, "15min")
    assert bl.confluencia_1h_15m(h1, m15, INI, 1.3245, -1) == (True, True)
    h1_sigue = _v([(1.320, 1.3248, 1.3195, 1.3247)], INI, "1h")                 # cerro ENCIMA del PDH
    assert bl.confluencia_1h_15m(h1_sigue, m15, INI, 1.3245, -1)[0] is False
    assert bl.confluencia_1h_15m(espejo(h1), espejo(m15), INI, 2.6 - 1.3245, 1) == (True, True)


def test_liquidez_sin_confluencia_no_se_envia(monkeypatch):
    datos, d1 = m5(), diario()
    monkeypatch.setattr(bl, "MERCADOS", {"GBP/USD": {"tipo": "forex", "yf": "x"}})
    monkeypatch.setattr(bl, "confluencia_1h_15m", lambda h1, m15, ini, nivel, bias: (True, False))
    senales, motivos = bl.analizar_todas("GBP/USD", None, lambda nm, tf: {"5m": datos, "1d": d1, "1h": datos,
                                                                         "15m": datos}[tf],
                                         datos.index[-1] + pd.Timedelta(minutes=5))
    assert senales == [] and "sin confluencia de 15M" in motivos["liquidez"]
