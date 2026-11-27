# -*- coding: utf-8 -*-
"""Tests de Ballena Riki CRT con velas construidas a mano (sin red ni Telegram).  python -m pytest -q"""
import os
import subprocess
import sys

import numpy as np
import pandas as pd
import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import ballena_riki as br  # noqa: E402


def escenario(despues=True, n_base=300):
    """Tendencia alcista suave (H1 sobre su EMA) -> MADRE alcista -> HIJA pequena dentro -> RUPTURA con cuerpo
    sobre el maximo de la madre -> (opcional) subida que toca el TP."""
    filas, p = [], 100.0
    for _ in range(n_base):
        filas.append((p, p + 0.4, p - 0.1, p + 0.3))
        p += 0.3
    filas += [(p, p + 4.2, p - 0.2, p + 4.0),            # madre: rango 4,4 · cuerpo 4
              (p + 3.5, p + 3.6, p + 2.9, p + 3.0),      # hija: cuerpo 0,5 dentro de la madre
              (p + 3.2, p + 5.1, p + 3.1, p + 5.0)]      # ruptura: cierra con cuerpo sobre el maximo (p + 4,2)
    if despues:
        q = p + 5.0
        for _ in range(12):
            filas.append((q, q + 1.2, q - 0.1, q + 1.0))
            q += 1.0
    o, h, l, c = (np.array(x, float) for x in zip(*filas))
    idx = pd.date_range("2026-11-20 00:00", periods=len(filas), freq="5min", tz="UTC")
    return pd.DataFrame(dict(open=o, high=h, low=l, close=c, volume=1.0), index=idx), p


@pytest.fixture
def btc(monkeypatch):
    monkeypatch.setattr(br, "MERCADOS", {"BTC/USD": {"tipo": "cripto", "yf": "x", "spread": 0.0}})


def test_porcentajes_de_cuerpo_como_el_indicador():
    assert br.medir_cuerpos(100, 110, 102, 105) == (100.0, 30.0, 0)               # 100% dentro, ocupa 30%
    dentro, ocupa, lado = br.medir_cuerpos(100, 110, 108, 115)                     # sale por arriba
    assert dentro == pytest.approx(200 / 7) and ocupa == pytest.approx(20.0) and lado == 1
    assert br.medir_cuerpos(100, 110, 95, 115)[2] == 3                             # por los dos lados
    assert br.medir_cuerpos(100, 110, 120, 125)[:2] == (0.0, 0.0)                  # todo fuera
    g = br.grupo(100.0, 30.0, 0)
    assert br.nombre_grupo(g) == "Dentro 100% · Ocupa 25-49% · dentro"


def test_madre_hija_y_ruptura_con_resultado():
    df, p = escenario()
    casos = [e for e in br.eventos(df, "5m") if e.get("j") == 302]
    assert len(casos) == 1
    e = casos[0]
    assert e["dir"] == 1 and e["mayor"] == 1 and e["dentro"] == 100 and e["hija_ok"]
    assert e["sl"] == pytest.approx(p + 2.0) and e["entrada"] == pytest.approx(p + 5.0)
    assert e["tp"] == pytest.approx(p + 5.0 + 1.5 * 3.0) and e["r"] == pytest.approx(1.5)   # toco el TP


def test_el_spread_se_descuenta():
    df, _ = escenario()
    e = [x for x in br.eventos(df, "5m", spread=0.3) if x.get("j") == 302][0]
    assert e["r"] == pytest.approx(1.5 - 0.3 / 3.0)


def test_hija_grande_no_opera():
    df, p = escenario()
    df.iloc[301, :4] = [p + 0.5, p + 3.9, p + 0.4, p + 3.8]                         # cuerpo 3,3 > 50% de 4
    assert not any(e.get("j") == 302 for e in br.eventos(df, "5m"))


def _modelo(df, aprobado=True, vetos=()):
    e = [x for x in br.eventos(df, "5m") if x.get("j") == len(df) - 1][0]
    return {"aprobado": aprobado, "grupos": {str(e["g"]): [30, 10]}, "base": [50, 50], "vetos": list(vetos),
            "examen": {"ops": 40, "r_op": 0.3}}


def test_senal_en_vivo_y_mensaje_morado(monkeypatch, btc):
    df, _ = escenario(despues=False)
    monkeypatch.setattr(br, "MODELOS", {"BTC/USD|5m": _modelo(df)})
    s, motivo = br.senal_riki("BTC/USD", "5m", df)
    assert s and s["dir"] == 1 and s["prob"] == 75 and s["casos"] == 40, motivo
    lineas = br.texto_alerta(s).splitlines()
    assert lineas[0] == "🟣 🟢 COMPRA BTC/USD · RIKI CRT 5M"
    assert "Dentro 100%" in lineas[1] and "▲75% (40 casos)" in lineas[2] and lineas[-1].startswith("✅ TP:")


def test_no_aprobado_queda_en_practica(monkeypatch, btc):
    df, _ = escenario(despues=False)
    monkeypatch.setattr(br, "MODELOS", {"BTC/USD|5m": _modelo(df, aprobado=False)})
    monkeypatch.setitem(br.CONFIG, "practica", False)
    s, motivo = br.senal_riki("BTC/USD", "5m", df)
    assert s is None and "práctica" in motivo


def test_trampa_conocida_se_veta(monkeypatch, btc):
    df, _ = escenario(despues=False)
    monkeypatch.setattr(br, "MODELOS", {"BTC/USD|5m": _modelo(df, vetos=["asia", "londres", "nuevayork", "fuera"])})
    s, motivo = br.senal_riki("BTC/USD", "5m", df)
    assert s is None and "trampa conocida" in motivo


def test_grupo_en_contra_no_opera(monkeypatch, btc):
    df, _ = escenario(despues=False)
    m = _modelo(df)
    m["grupos"] = {k: [10, 30] for k in m["grupos"]}
    monkeypatch.setattr(br, "MODELOS", {"BTC/USD|5m": m})
    s, motivo = br.senal_riki("BTC/USD", "5m", df)
    assert s is None and "no da esa dirección" in motivo


def test_trampas_en_las_dos_mitades():
    ops = [{"j": i, "r": -1.0 if i % 2 == 0 else 1.5, "amb": ["asia"] if i % 2 == 0 else ["londres"]}
           for i in range(40)]
    assert br.trampas(ops) == ["asia"]
    ops = [{"j": i, "r": -1.0 if i < 20 else 1.5, "amb": ["asia"]} for i in range(40)]   # solo en una mitad
    assert br.trampas(ops) == []


def test_examen_no_aprueba_el_azar():
    rng = np.random.default_rng(3)
    n = 20000
    c = 20000 + np.cumsum(rng.normal(0, 8, n))
    o = np.r_[c[0], c[:-1]]
    df = pd.DataFrame(dict(open=o, high=np.maximum(o, c) + rng.exponential(4, n),
                           low=np.minimum(o, c) - rng.exponential(4, n), close=c, volume=1.0),
                      index=pd.date_range("2026-01-01", periods=n, freq="5min", tz="UTC"))
    m = br.entrenar_df(df, "5m", 1.35)
    assert m["aprobado"] is False and set(m["examen"]) >= {"ops", "r_op", "base_r_op"} and m["grupos"]


def test_envia_una_vez_y_sin_repetir(monkeypatch, tmp_path, btc):
    df, _ = escenario(despues=False)
    monkeypatch.setattr(br, "MODELOS", {"BTC/USD|5m": _modelo(df)})
    monkeypatch.setitem(br.CONFIG, "temporalidades", {"5m": "1h"})
    enviados = []
    monkeypatch.setattr(br, "enviar_telegram", lambda txt, png=None: enviados.append(txt) or (True, "ok"))
    e = br.Estado(str(tmp_path / "e.json"))
    ahora = df.index[-1] + pd.Timedelta(minutes=5)
    assert br.escanear(e, lambda nm, tf: df, ahora, verbose=False) == 1
    assert br.escanear(e, lambda nm, tf: df, ahora, verbose=False) == 0
    assert enviados[0].startswith("🟣 🟢 COMPRA BTC/USD")


def test_nombre_mercados_y_arranque():
    assert br.ENCABEZADO == "🐋💜 BALLENA RIKI CRT"
    assert list(br.MERCADOS) == ["XAU/USD (Oro)", "BTC/USD", "NASDAQ 100"]
    assert set(br.CONFIG["temporalidades"]) == {"5m", "15m", "1h"}
    raiz = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    r = subprocess.run([sys.executable, os.path.join(raiz, "ballena_riki.py"), "--help"], capture_output=True, text=True)
    assert r.returncode == 0 and "--avisar-estado" in r.stdout and "entrenar" in r.stdout


def test_manda_solo_a_un_chat(monkeypatch):
    monkeypatch.delenv("RIKI_CHAT_IDS", raising=False)
    monkeypatch.setenv("TELEGRAM_CHAT_IDS", "1496321437,5880373359")
    assert br._chats() == ["1496321437"]


def test_tp_inteligente_en_el_mensaje(monkeypatch, btc):
    df, _ = escenario()
    casos = br.eventos(df, "5m")
    e = [x for x in casos if x.get("j") == 302][0]
    assert e["fav"] > 0 and e["adv"] >= 0                                  # alcance medido en ATR
    al = br.alcances(casos)
    assert al[f"base|1"]["n"] >= 1
    m = {"alcance": {f"{e['g']}|1": {"n": 30, "tp": 2.0, "sl": 1.0}}}
    p = br.plan_inteligente(e, m)
    assert p["tp"] == pytest.approx(e["entrada"] + 2.0 * e["atr"]) and p["sl"] == pytest.approx(e["entrada"] - e["atr"])
    assert p["fuente"] == "su grupo" and p["rr"] == pytest.approx(2.0)
    s = {**e, "activo": "BTC/USD", "tf": "5m", "prob": 70, "casos": 40, "examen": {}, "plan": p}
    lineas = br.texto_alerta(s).splitlines()
    assert lineas[-2].startswith("📍 TP inteligente:") and "60% de su grupo llegó hasta ahí en 5 velas" in lineas[-2]
    assert lineas[-1].startswith("⚠️ SL inteligente:") and "R:R 1:2.0" in lineas[-1]


def test_tp_inteligente_usa_todas_las_rupturas_si_el_grupo_tiene_pocos_casos():
    e = {"g": 5, "dir": -1, "entrada": 100.0, "atr": 2.0}
    m = {"alcance": {"5|-1": {"n": 3, "tp": 9.0, "sl": 1.0}, "base|-1": {"n": 50, "tp": 1.5, "sl": 1.0}}}
    p = br.plan_inteligente(e, m)
    assert p["fuente"] == "todas las rupturas" and p["tp"] == pytest.approx(97.0) and p["sl"] == pytest.approx(102.0)


def test_practica_activada_por_defecto_y_marcada(monkeypatch, btc):
    assert br.CONFIG["practica"] is True
    df, _ = escenario(despues=False)
    monkeypatch.setattr(br, "MODELOS", {"BTC/USD|5m": _modelo(df, aprobado=False)})
    s, _ = br.senal_riki("BTC/USD", "5m", df)
    assert s["practica"] and "🧪 PRÁCTICA" in br.texto_alerta(s).splitlines()[0]
