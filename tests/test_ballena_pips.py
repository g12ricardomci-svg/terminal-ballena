# -*- coding: utf-8 -*-
"""Tests de la CRT con datos sinteticos (sin red ni Telegram).  python -m pytest -q"""
import os
import sys

import numpy as np
import pandas as pd
import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import ballena_pips as bp  # noqa: E402


def df_de(o, h, l, c, inicio, freq):
    idx = pd.date_range(inicio, periods=len(c), freq=freq, tz="UTC")
    return pd.DataFrame(dict(open=o, high=h, low=l, close=c, volume=1.0), index=idx)


def diario_bajista():
    """Ultima D1 cerrada barre el maximo de la anterior y cierra dentro -> sesgo bajista."""
    return df_de([1.10, 1.105], [1.110, 1.114], [1.095, 1.100], [1.105, 1.102], "2026-11-01", "1D")


def escenario_venta(confirma=True, f_rango="1h", f_conf="5min"):
    """9:00-10:00 NY: H1 de referencia; 10:00 NY en adelante la H1 en curso barre su maximo y M5 confirma."""
    # H1 cerradas (UTC): la ultima cerrada va de 15:00 a 16:00 UTC (9:00-10:00 NY en noviembre)
    n1 = 30
    c1 = np.linspace(1.1000, 1.1040, n1)
    paso_r, paso_c = pd.Timedelta(f_rango), pd.Timedelta(f_conf)
    h1 = df_de(c1 - 0.0004, c1 + 0.0006, c1 - 0.0008, c1, pd.Timestamp("2026-11-23 15:00", tz="UTC")
               - paso_r * n1, f_rango)
    ref_hi = float(h1["high"].iloc[-1])
    # M5 previas (historia) y la H1 en curso desde las 16:00 UTC
    prev = np.linspace(1.1020, 1.1040, 60)
    curso = [1.1040, 1.1036, 1.1032, 1.1038, ref_hi + 0.0012, 1.1043, 1.1034]       # baja, sube, barre, vuelve
    curso += [1.1020] if confirma else [1.1039]
    c = np.r_[prev, curso]
    o = np.r_[c[0], c[:-1]]
    h = np.maximum(o, c) + 0.0002
    l = np.minimum(o, c) - 0.0002
    m5 = df_de(o, h, l, c, pd.Timestamp("2026-11-23 15:00", tz="UTC") - paso_c * len(prev), f_conf)
    return m5, h1


def test_sesgo_crt():
    assert bp.sesgo_rango(diario_bajista())[0] == -1
    alcista = df_de([1.10, 1.098], [1.110, 1.106], [1.095, 1.090], [1.105, 1.100], "2026-11-01", "1D")
    assert bp.sesgo_rango(alcista)[0] == 1
    interna = df_de([1.10, 1.101], [1.110, 1.108], [1.095, 1.097], [1.105, 1.103], "2026-11-01", "1D")
    assert bp.sesgo_rango(interna)[0] == 0


def test_venta_crt_confirmada():
    m5, h1 = escenario_venta(True)
    ahora = m5.index[-1] + pd.Timedelta(minutes=5)
    s, motivo = bp.senal_crt(m5, h1, diario_bajista(), ahora)
    assert s and s["dir"] == "SHORT", motivo
    assert s["entrada"] < s["manipulado"] < s["sl"]                 # vende tras barrer el maximo H1
    assert s["tp2"] <= s["tp1"] < s["entrada"]
    assert s["entrada"] - s["tp1"] == pytest.approx(2 * (s["sl"] - s["entrada"]))


def test_sin_confirmacion_no_hay_senal():
    m5, h1 = escenario_venta(False)
    s, motivo = bp.senal_crt(m5, h1, diario_bajista(), m5.index[-1] + pd.Timedelta(minutes=5))
    assert s is None and "esperando" in motivo


def test_fuera_de_horario(monkeypatch):
    monkeypatch.setitem(bp.CONFIG, "solo_sesiones", True)
    m5, h1 = escenario_venta(True)
    tarde = pd.Timestamp("2026-11-23 20:30", tz="UTC")              # 15:30 NY
    s, motivo = bp.senal_crt(m5, h1, diario_bajista(), tarde)
    assert s is None and "fuera de horario" in motivo


def test_compra_es_espejo():
    m5, h1 = escenario_venta(True)
    esp = lambda d: pd.DataFrame({"open": 2.2 - d["open"], "high": 2.2 - d["low"], "low": 2.2 - d["high"],
                                  "close": 2.2 - d["close"], "volume": 1.0}, index=d.index)
    d1 = esp(diario_bajista())
    s, motivo = bp.senal_crt(esp(m5), esp(h1), d1, m5.index[-1] + pd.Timedelta(minutes=5))
    assert s and s["dir"] == "LONG", motivo
    assert s["sl"] < s["manipulado"] < s["entrada"] < s["tp1"] <= s["tp2"]


def test_mensaje_resumido_con_nombre_y_una_vez(monkeypatch, tmp_path):
    m5, h1 = escenario_venta(True)
    d1 = diario_bajista()
    monkeypatch.setattr(bp, "MERCADOS", {"EUR/USD": {"tipo": "forex", "yf": "x"}})
    monkeypatch.setenv("TELEGRAM_TOKEN", "x")
    monkeypatch.setenv("TELEGRAM_CHAT_IDS", "1")
    monkeypatch.setattr(bp, "_dormir", lambda s: None)
    textos = []

    class R:
        ok, status_code, text = True, 200, ""
    monkeypatch.setattr(bp.requests, "post", lambda url, json=None, data=None, files=None, timeout=None:
                        textos.append(json["text"]) or R())
    monkeypatch.setitem(bp.CONFIG, "confluencia", None)                  # aqui se prueba el formato del modelo 1H
    cargar = lambda nm, tf: {"5m": m5, "1h": h1, "4h": d1}[tf]          # el modelo 15M no tiene datos aqui
    ahora = m5.index[-1] + pd.Timedelta(minutes=5)
    e = bp.Estado(str(tmp_path / "e.json"))
    assert bp.escanear(e, cargar, ahora, verbose=False) == 1
    lineas = textos[0].splitlines()
    assert lineas[0] == "💧 BALLENA PIPS" and lineas[1].startswith("🚨 🔴 VENTA EUR/USD · CRT 1H")
    assert "barrió el máximo H1" in lineas[2] and "confirmación M5" in lineas[2]
    assert any(x.startswith("🧱 1/3 ahora") for x in lineas)
    assert bp.escanear(e, cargar, ahora, verbose=False) == 0               # la misma CRT no se repite


def test_motivos_de_compra_sin_numeros_negativos():
    m5, h1 = escenario_venta(False)
    esp = lambda d: pd.DataFrame({"open": 2.2 - d["open"], "high": 2.2 - d["low"], "low": 2.2 - d["high"],
                                  "close": 2.2 - d["close"], "volume": 1.0}, index=d.index)
    s, motivo = bp.senal_crt(esp(m5), esp(h1), esp(diario_bajista()), m5.index[-1] + pd.Timedelta(minutes=5))
    assert s is None and "mínimo" in motivo and "-" not in motivo.split("mínimo")[1]



def test_modelo_15m_rango_m15_confirmacion_m1():
    m1, m15 = escenario_venta(True, f_rango="15min", f_conf="1min")
    ahora = m1.index[-1] + pd.Timedelta(minutes=1)
    s, motivo = bp.senal_crt(m1, m15, diario_bajista(), ahora, modelo="15M")
    assert s and s["dir"] == "SHORT" and s["modelo"] == "15M", motivo
    assert (s["tf_rango"], s["tf_conf"]) == ("15m", "1m")
    s.update(activo="EUR/USD")
    txt = bp.texto_alerta(s)
    assert "CRT 15M" in txt and "barrió el máximo M15" in txt and "confirmación M1" in txt


def test_cada_modelo_da_su_propia_senal(tmp_path, monkeypatch):
    monkeypatch.setattr(bp, "MERCADOS", {"EUR/USD": {"tipo": "forex", "yf": "x"}})
    m5, h1 = escenario_venta(True)
    m1, m15 = escenario_venta(True, f_rango="15min", f_conf="1min")
    datos = {"5m": m5, "1h": h1, "4h": diario_bajista(), "1m": m1, "15m": m15}
    senales, motivos = bp.analizar_todas("EUR/USD", "1m", lambda nm, tf: datos[tf], m5.index[-1] + pd.Timedelta(minutes=5))
    assert {s["modelo"] for s in senales} == {"1H", "15M"}
    assert len({s["id"] for s in senales}) == 2


def test_h4_se_construye_desde_h1(monkeypatch):
    idx = pd.date_range("2026-11-23 00:00", periods=8, freq="1h", tz="UTC")
    h1 = pd.DataFrame({"open": range(8), "high": range(1, 9), "low": range(8), "close": range(8), "volume": 1.0},
                      index=idx, dtype=float)
    monkeypatch.setattr(bp, "_obtener_red", lambda nm, tf: h1)
    h4 = bp.obtener("EUR/USD", "4h")
    assert len(h4) == 2 and h4["high"].iloc[0] == 4 and h4["open"].iloc[1] == 4


def test_mensaje_de_prueba():
    txt = bp.mensaje_prueba()
    assert txt.startswith("🧪 MENSAJE DE PRUEBA (no operar)") and "CRT 15M" in txt and "SL:" in txt



def test_analiza_24_7_y_los_3_mercados():
    assert bp.CONFIG["solo_sesiones"] is False and bp.en_ventana(pd.Timestamp("2026-11-24 20:30", tz="UTC"))
    assert list(bp.MERCADOS) == ["XAU/USD (Oro)", "BTC/USD", "NASDAQ 100"]
    assert bp.CONFIG["confluencia"] is None                               # cada modelo envia lo suyo


def test_el_programa_arranca():
    """Las opciones de la linea de comandos no chocan (si chocan, el bot no arranca)."""
    import subprocess
    raiz = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    r = subprocess.run([sys.executable, os.path.join(raiz, "ballena_pips.py"), "--help"], capture_output=True, text=True)
    assert r.returncode == 0 and "--avisar-estado" in r.stdout



def test_crt_sin_confluencia_no_se_envia(monkeypatch):
    """Con la confluencia ACTIVADA: el 15M da VENTA pero el sesgo H4 del modelo 1H es alcista -> no se envia."""
    monkeypatch.setitem(bp.CONFIG, "confluencia", ("15M", "1H"))
    monkeypatch.setattr(bp, "MERCADOS", {"EUR/USD": {"tipo": "forex", "yf": "x"}})
    m5, h1 = escenario_venta(True)
    m1, m15 = escenario_venta(True, f_rango="15min", f_conf="1min")
    alcista = df_de([1.10, 1.098], [1.110, 1.106], [1.095, 1.090], [1.105, 1.100], "2026-11-01", "1D")
    datos = {"5m": m5, "1h": h1, "4h": alcista, "1m": m1, "15m": m15}
    senales, motivos = bp.analizar_todas("EUR/USD", "1m", lambda nm, tf: datos[tf],
                                         m5.index[-1] + pd.Timedelta(minutes=5))
    assert senales == [] and "sin confluencia del modelo 1H" in motivos["crt 15M"]
