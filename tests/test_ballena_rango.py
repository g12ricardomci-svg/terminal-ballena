# -*- coding: utf-8 -*-
"""Tests de Ballena Rango con escenarios construidos a mano (sin red ni Telegram).  python -m pytest -q"""
import os
import subprocess
import sys

import numpy as np
import pandas as pd
import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import ballena_rango as br  # noqa: E402

T0 = pd.Timestamp("2026-11-24 12:00", tz="UTC")      # comienzo de la vela H4 en curso


def velas(filas, inicio, freq):
    o, h, l, c = (np.array(x, float) for x in zip(*filas))
    idx = pd.date_range(inicio, periods=len(filas), freq=freq, tz="UTC")
    return pd.DataFrame(dict(open=o, high=h, low=l, close=c, volume=1.0), index=idx)


def mayor_h4(o_ult=1.1000, c_ult=1.1040, h_ult=1.1100, l_ult=1.0990):
    """H4 cerradas hasta las 12:00; la ultima (08:00-12:00) define el bias y el DOL."""
    filas = [(1.10, 1.102, 1.098, 1.10)] * 9 + [(o_ult, h_ult, l_ult, c_ult)]
    return velas(filas, T0 - pd.Timedelta(hours=40), "4h")


def entrada_m15(ultima=(1.1022, 1.1032, 1.1015, 1.1030)):
    """48 velas M15 antes de las 12:00 y, en la H4 en curso: bajada que deja un minimo pendiente en 1.1020 y una
    ultima vela que lo barre (1.1015) y cierra con cuerpo alcista (1.1030)."""
    antes = [(1.1040, 1.1045, 1.1035, 1.1040)] * 48
    curso = [(1.1040, 1.1042, 1.1030, 1.1032), (1.1032, 1.1034, 1.1020, 1.1025),   # 12:15 deja el minimo 1.1020
             (1.1025, 1.1030, 1.1022, 1.1028), (1.1028, 1.1030, 1.1023, 1.1024), ultima]
    return velas(antes + curso, T0 - pd.Timedelta(minutes=15 * 48), "15min")


def espejo(df):
    return pd.DataFrame({"open": 2.2 - df["open"], "high": 2.2 - df["low"], "low": 2.2 - df["high"],
                         "close": 2.2 - df["close"], "volume": 1.0}, index=df.index)


def test_bias_por_cuerpo_y_dol():
    assert br.bias_dol(mayor_h4()) == (1, 1.1100)
    assert br.bias_dol(mayor_h4(o_ult=1.1040, c_ult=1.1000))[0] == -1
    assert br.bias_dol(mayor_h4(o_ult=1.1, c_ult=1.1))[0] == 0


def test_compra_en_rango_pendiente_activado():
    s, motivo = br.senal_rango(entrada_m15(), mayor_h4(), "15M")
    assert s and s["dir"] == "LONG", motivo
    assert s["sl"] < s["nivel"] < s["entrada"] < s["dol"] == pytest.approx(1.1100)
    assert s["rr"] >= br.CONFIG["rr_min"] and s["nivel"] == pytest.approx(1.1020)


def test_venta_es_espejo():
    s, motivo = br.senal_rango(espejo(entrada_m15()), espejo(mayor_h4()), "15M")
    assert s and s["dir"] == "SHORT", motivo
    assert s["dol"] < s["entrada"] < s["nivel"] < s["sl"]


def test_rr_insuficiente_se_descarta():
    s, motivo = br.senal_rango(entrada_m15(), mayor_h4(h_ult=1.1045), "15M")     # DOL demasiado cerca
    assert s is None and "R:R" in motivo and "se descarta" in motivo


def test_dol_ya_alcanzado_no_opera():
    ent = entrada_m15()
    ent.iloc[49, ent.columns.get_loc("high")] = 1.1105                              # la H4 en curso ya tomo el DOL
    s, motivo = br.senal_rango(ent, mayor_h4(), "15M")
    assert s is None and "ya alcanzado" in motivo


def test_sin_cierre_con_cuerpo_no_hay_entrada():
    s, motivo = br.senal_rango(entrada_m15(ultima=(1.1030, 1.1032, 1.1015, 1.1026)), mayor_h4(), "15M")
    assert s is None and "sin cierre con cuerpo" in motivo


def test_modelo_1h_h4_no_puede_contradecir():
    h4_bajista = mayor_h4(o_ult=1.1040, c_ult=1.1000)
    s, motivo = br.senal_rango(entrada_m15(), mayor_h4(), "1H", filtro=h4_bajista)
    assert s is None and "contradicho por H4" in motivo


def test_mensaje_y_sin_repetir(monkeypatch, tmp_path):
    ent, h4 = entrada_m15(), mayor_h4()
    monkeypatch.setattr(br, "MERCADOS", {"EUR/USD": {"tipo": "forex", "yf": "x"}})
    monkeypatch.setitem(br.CONFIG, "modelos", {"15M": {"mayor": "4h", "entrada": "15m"}})
    enviados = []
    monkeypatch.setattr(br, "enviar_telegram", lambda txt, png=None: enviados.append(txt) or (True, "ok"))
    ahora = ent.index[-1] + pd.Timedelta(minutes=15)
    e = br.Estado(str(tmp_path / "e.json"))
    cargar = lambda nm, tf: {"15m": ent, "4h": h4}[tf]
    assert br.escanear(e, cargar, ahora, verbose=False) == 1
    lineas = enviados[0].splitlines()
    assert lineas[0] == "🚨 🟢 COMPRA EUR/USD · RANGO 15M"
    assert "Bias H4 alcista · DOL 1.11000" in lineas[1] and lineas[-1].startswith("✅ TP (DOL): 1.11000 · R:R 1:")
    assert br.escanear(e, cargar, ahora, verbose=False) == 0


def test_nombre_del_bot_y_8_mercados():
    assert br.ENCABEZADO == "📦 BALLENA RANGO"
    assert list(br.MERCADOS) == ["EUR/USD", "AUD/USD", "GBP/USD", "USD/JPY", "GBP/JPY", "XAU/USD (Oro)",
                                 "BTC/USD", "NASDAQ 100"]
    assert set(br.CONFIG["modelos"]) == {"5M", "15M", "1H"}


def test_el_programa_arranca():
    raiz = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    r = subprocess.run([sys.executable, os.path.join(raiz, "ballena_rango.py"), "--help"], capture_output=True, text=True)
    assert r.returncode == 0 and "--avisar-estado" in r.stdout
