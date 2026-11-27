# -*- coding: utf-8 -*-
"""Tests de Ballena Dorada (sin red ni Telegram).  python -m pytest -q"""
import os
import subprocess
import sys

import numpy as np
import pandas as pd
import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import ballena_dorada as bd  # noqa: E402


def mercado(n=3000, semilla=2):
    rng = np.random.default_rng(semilla)
    c = 2600 + np.cumsum(rng.normal(0, 1.2, n))
    o = np.r_[c[0], c[:-1]] + rng.normal(0, 0.3, n)
    return pd.DataFrame(dict(open=o, high=np.maximum(o, c) + rng.exponential(0.6, n),
                             low=np.minimum(o, c) - rng.exponential(0.6, n), close=c, volume=rng.uniform(50, 150, n)),
                        index=pd.date_range("2026-10-20", periods=n, freq="5min", tz="UTC"))


def test_porcentajes_del_ejemplo():
    """Madre 2000 -> 2010 y vela siguiente 2006 -> 2012: Dentro 67 % · Ocupa 40 %."""
    df = mercado(300)
    df.iloc[-2, :4] = [2000.0, 2010.5, 1999.5, 2010.0]
    df.iloc[-1, :4] = [2006.0, 2012.5, 2005.5, 2012.0]
    df.iloc[-3, 3] = 2000.0
    P = bd.preparar(df, "5m")
    assert P["dentro"][-1] == pytest.approx(200 / 3) and P["ocupa"][-1] == pytest.approx(40.0)
    assert P["lado"][-1] == 1 and P["madre"][-1]


def test_rasgos_dentro_de_su_rango():
    P = bd.preparar(mercado(), "5m")
    X = P["X"][200:]
    assert X.shape[1] == 22 == len(bd.CARD)
    assert ((X >= 0) & (X < np.array(bd.CARD))).all()


def test_no_mira_el_futuro():
    """La senal de la ultima vela es la misma aunque luego lleguen mas velas."""
    df = mercado()
    for fin in range(2900, 3000):
        s1, _ = bd.senal_dorada("XAU/USD (Oro)", "5m", df.iloc[:fin])
        if s1:
            P = bd.preparar(df.iloc[:fin + 20], "5m")
            assert P["madre"][fin - 1]
            s2, _ = bd.senal_dorada("XAU/USD (Oro)", "5m", df.iloc[:fin])
            assert s2["entrada"] == s1["entrada"] and s2["tp"] == s1["tp"]
            return
    pytest.fail("no hubo ninguna senal en 100 velas")


def test_plan_completo_y_mensaje():
    df = mercado()
    for fin in range(2800, 3000):
        s, _ = bd.senal_dorada("XAU/USD (Oro)", "5m", df.iloc[:fin])
        if s:
            break
    assert s and s["dir"] in (1, -1) and s["casos"] >= bd.CONFIG["min_casos"]
    assert (s["tp"] - s["entrada"]) * s["dir"] > 0 and (s["entrada"] - s["sl"]) * s["dir"] > 0
    lineas = bd.texto_alerta(s).splitlines()
    assert lineas[0].startswith("🟡 ") and "DORADA 5M" in lineas[0]
    assert lineas[2].startswith("🎯 Entrada:") and lineas[4].startswith("🛑 SL:")
    assert lineas[5].startswith(("✓ GANA", "⚠ NO OPERAR")) and lineas[-1].startswith("📊 ")


def test_objetivo_cuando_la_senal_es_fuerte():
    s = {"activo": "BTC/USD", "tf": "1h", "dir": -1, "pct": 63.0, "casos": 80, "fuente": "★ patrón", "entrada": 100.0,
         "tp": 95.0, "sl": 103.0, "rr": 1.7, "ev": 0.1, "acierto": 55.0, "dentro": 50.0, "ocupa": 20.0, "lado": 2,
         "patron": "RSI sobre 70 + mecha larga arriba (rechazo)", "patron_prueba": 66.0,
         "conflicto": "⚔ SEÑALES EN CONFLICTO: su grupo decía ▲ SUBE 55% (40 casos), manda el ★ patrón",
         "noticia": "⏰ horario de noticias", "tendencia": "LATERAL", "vola": "ALTA", "vol_r": 1.4, "adx": 12.0,
         "vela": pd.Timestamp("2026-11-27 13:00", tz="UTC")}
    t = bd.texto_alerta(s)
    assert "🎯 OBJETIVO: 95.000" in t and "▼63% ★" in t and "★ Patrón: RSI sobre 70" in t
    assert "⚔ SEÑALES EN CONFLICTO" in t and "⚡ NOTICIA: mejor NO operar ahora" in t
    s["pct"] = 55.0
    assert "🎯 OBJETIVO" not in bd.texto_alerta(s) and "✅ TP: 95.000" in bd.texto_alerta(s)


def test_patrones_se_confirman_con_lo_reciente():
    """Un rasgo plantado (vela medida alcista -> sube) se encuentra y se confirma."""
    df = mercado(4000, 5)
    P = bd.preparar(df, "5m")
    casos = np.nonzero(P["madre"][:-10])[0]
    alc = P["X"][casos, 11] == 1
    P["cierre"] = P["cierre"].copy()
    P["cierre"][casos] = np.where(alc, np.abs(P["cierre"][casos]) + 0.1, P["cierre"][casos])
    conf, probados = bd.buscar_patrones(P, casos)
    assert probados > 100 and conf
    assert any(p["dir"] == 1 and (int(bd.OFF[11]) + 1) in p["codigos"] for p in conf)


def test_noticia_por_vela_enorme():
    df = mercado(400)
    df.iloc[-2, 1] = df.iloc[-2, 3] + 40
    P = bd.preparar(df, "5m")
    assert "posible noticia hace 1 velas" in bd.noticia(P, len(df) - 1, "5m")


def test_manda_solo_a_un_chat_y_nombre(monkeypatch):
    monkeypatch.delenv("RIKI_CHAT_IDS", raising=False)
    monkeypatch.setenv("TELEGRAM_CHAT_IDS", "1496321437,5880373359")
    assert bd._chats() == ["1496321437"]
    assert bd.ENCABEZADO == "🐋🟡 BALLENA DORADA"
    assert list(bd.MERCADOS) == ["XAU/USD (Oro)", "BTC/USD", "NASDAQ 100"]
    assert set(bd.CONFIG["temporalidades"]) == {"5m", "15m", "1h"}


def test_envia_y_no_repite(monkeypatch, tmp_path):
    df = mercado()
    fin = next(f for f in range(2800, 3000) if bd.senal_dorada("BTC/USD", "5m", df.iloc[:f])[0])
    datos = df.iloc[:fin]
    monkeypatch.setitem(bd.CONFIG, "temporalidades", {"5m": "1h"})
    enviados = []
    monkeypatch.setattr(bd, "enviar_telegram", lambda txt, png=None: enviados.append(txt) or (True, "ok"))
    monkeypatch.setattr(bd, "MERCADOS", {"BTC/USD": bd.MERCADOS["BTC/USD"]})
    e = bd.Estado(str(tmp_path / "e.json"))
    ahora = datos.index[-1] + pd.Timedelta(minutes=5)
    assert bd.escanear(e, lambda nm, tf: datos, ahora, verbose=False) == 1
    assert bd.escanear(e, lambda nm, tf: datos, ahora, verbose=False) == 0
    assert "DORADA 5M" in enviados[0]


def test_el_programa_arranca():
    raiz = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    r = subprocess.run([sys.executable, os.path.join(raiz, "ballena_dorada.py"), "--help"], capture_output=True, text=True)
    assert r.returncode == 0 and "--avisar-estado" in r.stdout
