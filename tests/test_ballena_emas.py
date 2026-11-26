# -*- coding: utf-8 -*-
"""Tests con datos sinteticos (sin red ni Telegram).  python -m pytest -q"""
import os
import sys

import numpy as np
import pandas as pd
import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import ballena_emas as be  # noqa: E402


def velas(precios, freq="5min", inicio="2026-11-02 14:00"):
    c = np.asarray(precios, float)
    idx = pd.date_range(inicio, periods=len(c), freq=freq, tz="UTC")
    o = np.r_[c[0], c[:-1]]
    return pd.DataFrame(dict(open=o, high=np.maximum(o, c) * 1.0003, low=np.minimum(o, c) * 0.9997, close=c,
                             volume=1.0), index=idx)


def marea_alcista():
    return velas(np.linspace(1.00, 1.10, 300), freq="1h", inicio="2026-10-20")


def marea_bajista():
    return velas(np.linspace(1.10, 1.00, 300), freq="1h", inicio="2026-10-20")


def cruce_alcista():
    """Baja suave y luego sube: la EMA 9 cruza a la 21 hacia arriba en la ULTIMA vela."""
    base = np.r_[np.linspace(1.10, 1.09, 80), np.linspace(1.09, 1.0935, 12)]
    df = velas(base)
    r, l = be.ema(df["close"], 9), be.ema(df["close"], 21)
    # recortar justo en la vela del cruce
    k = next(i for i in range(30, len(df)) if r.iloc[i] > l.iloc[i] and r.iloc[i - 1] <= l.iloc[i - 1])
    return df.iloc[:k + 1]


def test_marea():
    assert be.marea(marea_alcista()) == 1
    assert be.marea(marea_bajista()) == -1
    assert be.marea(marea_alcista().iloc[:50]) == 0          # pocas velas para la EMA 200


def cruce_confirmado():
    """El cruce alcista y 2 velas mas que lo mantienen."""
    df = cruce_alcista()
    ult = float(df["close"].iloc[-1])
    extra = velas(np.r_[df["close"].to_numpy(), ult + 0.0006, ult + 0.0012])
    return extra


def test_en_el_cruce_solo_hay_aviso():
    s, motivo = be.senal_cruce(cruce_alcista(), marea_alcista())
    assert s is None and "confirmo en 2 velas" in motivo
    av = be.aviso_cruce(cruce_alcista(), marea_alcista())
    assert av and av["dir"] == "LONG" and av["aviso"]


def test_cruce_que_no_se_mantiene():
    df = cruce_alcista()
    ult = float(df["close"].iloc[-1])
    falla = velas(np.r_[df["close"].to_numpy(), ult - 0.0015, ult - 0.0030])
    s, motivo = be.senal_cruce(falla, marea_alcista())
    assert s is None


def test_senal_a_favor_de_la_marea():
    s, motivo = be.senal_cruce(cruce_confirmado(), marea_alcista())
    assert s and s["dir"] == "LONG", motivo
    assert s["sl"] < s["entrada"] < s["tp1"] < s["tp2"]
    assert s["tp1"] - s["entrada"] == pytest.approx(2 * (s["entrada"] - s["sl"]))
    assert s["entrada"] - s["sl"] >= s["atr"] * be.CONFIG["atr_min_stop"] - 1e-12


def test_sin_senal_contra_la_marea():
    s, motivo = be.senal_cruce(cruce_confirmado(), marea_bajista())
    assert s is None and be.aviso_cruce(cruce_alcista(), marea_bajista()) is None


def test_simular_tp_y_stop():
    df = velas(np.r_[np.full(5, 1.0), np.linspace(1.0, 1.05, 30)])
    s = {"bias": 1, "sl": 0.995}
    rb, sal, motivo, e = be.simular(df, 5, s)
    assert motivo == "TP2" and rb == pytest.approx(0.5 * 2 + 0.5 * 3)
    df2 = velas(np.r_[np.full(5, 1.0), np.linspace(1.0, 0.98, 10)])
    rb2, _, motivo2, _ = be.simular(df2, 5, {"bias": 1, "sl": 0.9985})       # stop cercano: salta antes
    assert motivo2 == "SL" and rb2 == pytest.approx(-1.0, abs=0.05)
    rb3, _, motivo3, _ = be.simular(df2, 5, {"bias": 1, "sl": 0.95})         # stop lejano: sale por el cruce
    assert motivo3 == "Cruce en contra" and -1 < rb3 < 0


def test_horarios():
    t = lambda x: pd.Timestamp(x, tz="America/New_York").to_pydatetime()
    assert be.mercado_abierto("cripto", t("2026-11-21 12:00"))
    assert not be.mercado_abierto("forex", t("2026-11-21 12:00"))
    assert not be.mercado_abierto("oro", t("2026-11-23 17:30"))
    assert be.mercado_abierto("indice", t("2026-11-23 10:00"))


def test_aviso_y_luego_confirmacion(monkeypatch, tmp_path):
    """Vela del cruce -> aviso con la direccion; 2 velas despues -> entrada confirmada (sin imagen)."""
    d1 = marea_alcista()
    monkeypatch.setattr(be, "MERCADOS", {"EUR/USD": {"tipo": "forex", "yf": "x"}})
    monkeypatch.setitem(be.CONFIG, "tfs", ["5m"])
    monkeypatch.setitem(be.CONFIG, "estrategias", ["cruce"])
    enviados = []
    monkeypatch.setattr(be, "enviar_telegram", lambda txt, png=None: enviados.append((txt, png)) or (True, "ok"))
    e = be.Estado(str(tmp_path / "e.json"))
    for df in (cruce_alcista(), cruce_confirmado()):
        be.escanear(e, lambda nm, tf, df=df: d1 if tf == "1h" else df, df.index[-1] + pd.Timedelta(minutes=5),
                    verbose=False)
    (aviso, p1), (conf, p2) = enviados
    assert aviso.startswith("👀 CRUCE ALCISTA EUR/USD · 5M") and "COMPRA" in aviso and p1 is None
    assert conf.startswith("🚨 🟢 COMPRA EUR/USD · 5M") and "confirmado" in conf and p2 is None


def test_alerta_con_imagen_y_sin_repetir(monkeypatch, tmp_path):
    pytest.importorskip("matplotlib")
    df, d1 = cruce_confirmado(), marea_alcista()
    monkeypatch.setattr(be, "MERCADOS", {"EUR/USD": {"tipo": "forex", "yf": "x"}})
    monkeypatch.setitem(be.CONFIG, "tfs", ["5m"])
    monkeypatch.setitem(be.CONFIG, "imagen", True)
    monkeypatch.setitem(be.CONFIG, "estrategias", ["cruce"])
    ahora = df.index[-1] + pd.Timedelta(minutes=5)
    cargar = lambda nm, tf: d1 if tf == "1h" else df
    enviados = []
    monkeypatch.setattr(be, "enviar_telegram", lambda txt, png=None: enviados.append((txt, png)) or (True, "ok"))
    e = be.Estado(str(tmp_path / "e.json"))
    assert be.escanear(e, cargar, ahora, verbose=False) == 1
    txt, png = enviados[0]
    assert "COMPRA EUR/USD" in txt and "SL:" in txt and "TP1:" in txt and len(txt.splitlines()) == 5
    assert png[:4] == b"\x89PNG"
    assert be.escanear(e, cargar, ahora, verbose=False) == 0      # la misma senal no se repite


def test_backtest_corre(monkeypatch):
    monkeypatch.setattr(be, "MERCADOS", {**be.MERCADOS, "EUR/USD": {"tipo": "forex", "yf": "EURUSD=X"}})
    rng = np.random.default_rng(3)
    c = 1.1 * np.exp(np.cumsum(rng.normal(0.00005, 0.0012, 3000)))
    df = velas(c, freq="5min", inicio="2026-09-01")
    d1 = df.resample("1h").agg({"open": "first", "high": "max", "low": "min", "close": "last", "volume": "sum"}).dropna()
    ops = be.backtest_mercado("EUR/USD", "5m", df, d1)
    assert {"R", "R_bruto", "coste_R", "motivo"} <= set(ops.columns) and len(ops) > 0
    assert set(be.robustez(ops).columns) >= {"Ops", "R prom", "ROBUSTA"}


# ----------------------------------------------------------------- ruptura de rango
def rango_y_ruptura(arriba=True):
    """Tendencia previa, rango estrecho de 25 velas y una vela que cierra fuera con cuerpo."""
    rng = np.random.default_rng(1)
    previo = np.linspace(1.0800, 1.0900, 40)
    rango = 1.0900 + rng.uniform(-0.0006, 0.0006, 25)
    salida = [1.0915] if arriba else [1.0885]
    return velas(np.r_[previo, rango, salida])


def test_ruptura_a_favor_de_la_marea():
    s, motivo = be.senal_ruptura(rango_y_ruptura(True), marea_alcista())
    assert s and s["dir"] == "LONG", motivo
    lo, hi = s["rango"]
    assert s["entrada"] > hi and s["sl"] < s["entrada"] < s["tp1"] < s["tp2"]
    assert s["salida_cruce"] is False


def test_ruptura_contra_la_marea_no_opera():
    s, motivo = be.senal_ruptura(rango_y_ruptura(True), marea_bajista())
    assert s is None and "esperando ruptura abajo" in motivo


def test_sin_rango_estrecho():
    df = velas(np.r_[np.linspace(1.08, 1.10, 60), 1.1005])      # tendencia limpia: el "rango" es enorme
    s, motivo = be.senal_ruptura(df, marea_alcista())
    assert s is None and "sin rango estrecho" in motivo


def test_escanear_envia_ruptura_con_imagen(monkeypatch, tmp_path):
    pytest.importorskip("matplotlib")
    df, d1 = rango_y_ruptura(True), marea_alcista()
    monkeypatch.setattr(be, "MERCADOS", {"EUR/USD": {"tipo": "forex", "yf": "x"}})
    monkeypatch.setitem(be.CONFIG, "tfs", ["5m"])
    monkeypatch.setitem(be.CONFIG, "imagen", True)
    monkeypatch.setitem(be.CONFIG, "estrategias", ["cruce", "ruptura"])
    ahora = df.index[-1] + pd.Timedelta(minutes=5)
    enviados = []
    monkeypatch.setattr(be, "enviar_telegram", lambda txt, png=None: enviados.append((txt, png)) or (True, "ok"))
    n = be.escanear(be.Estado(str(tmp_path / "e.json")), lambda nm, tf: d1 if tf == "1h" else df, ahora, verbose=False)
    rup = [t for t, p in enviados if "Ruptura de rango" in t]
    assert n >= 1 and rup and all(p[:4] == b"\x89PNG" for _, p in enviados)


def test_hueco_sobre_el_stop_no_cuenta():
    df = velas(np.r_[np.full(5, 1.0), [0.9952], np.linspace(0.995, 1.01, 10)])
    df.iloc[5, df.columns.get_loc("open")] = 0.9951            # abre casi en el stop
    assert be.simular(df, 5, {"bias": 1, "sl": 0.995, "riesgo_px": 0.005}) is None


def test_backtest_con_csv(tmp_path, monkeypatch):
    monkeypatch.setattr(be, "MERCADOS", {**be.MERCADOS, "EUR/USD": {"tipo": "forex", "yf": "EURUSD=X"}})
    rng = np.random.default_rng(3)
    df = velas(1.1 * np.exp(np.cumsum(rng.normal(0.00005, 0.0012, 3000))), freq="5min", inicio="2026-09-01")
    df.to_csv(tmp_path / "EUR_USD_5m.csv")
    class A: dias, mercados, tf, salida, csv_dir = 400, ["EUR/USD"], ["5m"], str(tmp_path / "res"), str(tmp_path)
    r = be.correr_backtest(A)
    assert r is not None and set(r["Estrategia"]) <= set(be.ESTRATEGIAS)
    assert (r["R prom"].abs() < 5).all()                          # sin resultados absurdos


# ----------------------------------------------------------------- ruptura + retest
def escenario_retest(final="confirma"):
    """Tendencia, rango estrecho, ruptura, vuelta al nivel roto y vela final (confirma / falsa / espera)."""
    rng = np.random.default_rng(2)
    previo = np.linspace(1.0800, 1.0900, 45)
    rango = 1.0900 + rng.uniform(-0.0005, 0.0005, 22)
    ruptura = [1.0914, 1.0922]
    retest = [1.0915, 1.0908, 1.0907]                  # vuelve al techo del rango (~1.0905)
    fin = {"confirma": [1.0925], "falsa": [1.0880], "espera": [1.0906]}[final]
    return velas(np.r_[previo, rango, ruptura, retest, fin])


def test_retest_confirmado():
    s, motivo = be.senal_retest(escenario_retest("confirma"), marea_alcista())
    assert s and s["dir"] == "LONG", motivo
    lo, hi = s["rango"]
    assert s["nivel_ruptura"] == pytest.approx(hi)
    assert s["sl"] < hi < s["entrada"] < s["tp1"] < s["tp2"]
    assert s["entrada"] - s["sl"] >= be.CONFIG["retest_min_stop_atr"] * s["atr"] - 1e-12
    assert s["vela_ruptura"] < s["vela"]


def test_ruptura_falsa_se_descarta():
    s, motivo = be.senal_retest(escenario_retest("falsa"), marea_alcista())
    assert s is None and "ruptura falsa" in motivo


def test_retest_sin_confirmacion_espera():
    s, motivo = be.senal_retest(escenario_retest("espera"), marea_alcista())
    assert s is None and ("esperando" in motivo or "en curso" in motivo)


def test_retest_en_ventas_es_espejo():
    df = escenario_retest("confirma")
    espejo = pd.DataFrame({"open": 2.2 - df["open"], "high": 2.2 - df["low"], "low": 2.2 - df["high"],
                           "close": 2.2 - df["close"], "volume": 1.0}, index=df.index)
    s, motivo = be.senal_retest(espejo, marea_bajista())
    assert s and s["dir"] == "SHORT", motivo
    assert s["tp2"] < s["tp1"] < s["entrada"] < s["nivel_ruptura"] < s["sl"]


def test_retest_alerta_una_sola_vez(monkeypatch, tmp_path):
    pytest.importorskip("matplotlib")
    df, d1 = escenario_retest("confirma"), marea_alcista()
    monkeypatch.setattr(be, "MERCADOS", {"EUR/USD": {"tipo": "forex", "yf": "x"}})
    monkeypatch.setitem(be.CONFIG, "tfs", ["5m"])
    monkeypatch.setitem(be.CONFIG, "estrategias", ["retest"])
    monkeypatch.setitem(be.CONFIG, "imagen", True)
    enviados = []
    monkeypatch.setattr(be, "enviar_telegram", lambda txt, png=None: enviados.append((txt, png)) or (True, "ok"))
    e = be.Estado(str(tmp_path / "e.json"))
    cargar = lambda nm, tf: d1 if tf == "1h" else df
    ahora = df.index[-1] + pd.Timedelta(minutes=5)
    assert be.escanear(e, cargar, ahora, verbose=False) == 1
    assert "Ruptura + retest" in enviados[0][0] and enviados[0][1][:4] == b"\x89PNG"
    assert be.escanear(e, cargar, ahora, verbose=False) == 0



# ----------------------------------------------------------------- order block y confluencia multitemporal
def escenario_ob(final="confirma"):
    """Bajada a un minimo con vela bajista (OB), impulso que rompe el maximo previo, vuelta al OB y vela final."""
    p = np.r_[np.linspace(1.0900, 1.0880, 30), [1.0890, 1.0898, 1.0892, 1.0885, 1.0870],   # maximo 1.0898
              [1.0866, 1.0860], [1.0880, 1.0895, 1.0905, 1.0915, 1.0920],                    # OB y BOS
              [1.0910, 1.0895, 1.0880, 1.0866]]                                              # vuelta al OB
    fin = {"confirma": [1.0872], "rompe": [1.0845], "espera": [1.0864]}[final]
    return velas(np.r_[np.linspace(1.0850, 1.0900, 40), p, fin])


def test_order_block_confirmado():
    s, motivo = be.senal_orderblock(escenario_ob("confirma"), marea_alcista())
    assert s and s["dir"] == "LONG", motivo
    abajo, arriba = s["zona"]
    assert s["sl"] < abajo < arriba < s["entrada"] < s["tp1"] < s["tp2"]


def test_order_block_roto():
    s, motivo = be.senal_orderblock(escenario_ob("rompe"), marea_alcista())
    assert s is None and "roto" in motivo


def test_order_block_en_ventas_es_espejo():
    df = escenario_ob("confirma")
    esp = pd.DataFrame({"open": 2.2 - df["open"], "high": 2.2 - df["low"], "low": 2.2 - df["high"],
                        "close": 2.2 - df["close"], "volume": 1.0}, index=df.index)
    s, motivo = be.senal_orderblock(esp, marea_bajista())
    assert s and s["dir"] == "SHORT", motivo
    assert s["tp2"] < s["tp1"] < s["entrada"] < s["zona"][0] < s["zona"][1] < s["sl"]


def test_confluencia_multitemporal():
    base = np.r_[np.linspace(1.0800, 1.0900, 100), np.linspace(1.0900, 1.0880, 18)]
    # buscar la vela de 5m en la que 1H + 15m + 5m quedan alineadas por primera vez
    df_total = velas(np.r_[base, np.linspace(1.0880, 1.0920, 30)])
    hallada = None
    for i in range(125, len(df_total)):
        s, _ = be.senal_confluencia(df_total.iloc[:i], marea_alcista())
        if s:
            hallada = s
            break
    assert hallada and hallada["dir"] == "LONG" and hallada["sl"] < hallada["entrada"] < hallada["tp1"]
    s15, motivo = be.senal_confluencia(velas(np.r_[base, base], freq="15min"), marea_alcista())
    assert s15 is None and motivo == "solo en 5m"


def test_cada_estrategia_envia_su_propia_senal(monkeypatch, tmp_path):
    """Si dos estrategias dan senal a la vez en el mismo mercado, llegan DOS mensajes (no se combinan)."""
    df, d1 = escenario_ob("confirma"), marea_alcista()
    monkeypatch.setattr(be, "MERCADOS", {"EUR/USD": {"tipo": "forex", "yf": "x"}})
    monkeypatch.setitem(be.CONFIG, "tfs", ["5m"])
    monkeypatch.setitem(be.CONFIG, "imagen", False)
    falsa = lambda df_, d1_: ({**be.senal_orderblock(df_, d1_)[0], "zona": None}, "")
    monkeypatch.setattr(be, "ESTRATEGIAS", {"orderblock": be.senal_orderblock, "otra": falsa})
    monkeypatch.setitem(be.NOMBRE_EST, "otra", "Otra")
    monkeypatch.setitem(be.CONFIG, "estrategias", ["orderblock", "otra"])
    enviados = []
    monkeypatch.setattr(be, "enviar_telegram", lambda txt, png=None: enviados.append(txt) or (True, "ok"))
    n = be.escanear(be.Estado(str(tmp_path / "e.json")), lambda nm, tf: d1 if tf == "1h" else df,
                    df.index[-1] + pd.Timedelta(minutes=5), verbose=False)
    assert n == 2 and any("Order Block" in x for x in enviados) and any("Otra" in x for x in enviados)


# ----------------------------------------------------------------- soportes y resistencias
def escenario_soporte(final="rechazo"):
    """Dos toques previos del soporte ~1.0850 y una tercera vela que lo toca y cierra alcista."""
    tramo = lambda a, b, n: np.linspace(a, b, n)
    p = np.r_[tramo(1.0900, 1.0880, 30), tramo(1.0880, 1.0850, 10), tramo(1.0850, 1.0885, 12),   # toque 1
              tramo(1.0885, 1.0851, 12), tramo(1.0851, 1.0890, 12),                               # toque 2
              tramo(1.0890, 1.0858, 10)]
    df = velas(p)
    o, c = df["open"].to_numpy(), df["close"].to_numpy()
    ult = {"rechazo": (1.0849, 1.0862), "rompe": (1.0858, 1.0820), "lejos": (1.0880, 1.0885)}[final]
    extra = velas(np.r_[c, ult[1]])
    extra.iloc[-1, extra.columns.get_loc("open")] = c[-1]
    extra.iloc[-1, extra.columns.get_loc("low")] = min(ult[0], c[-1], ult[1]) - 0.0001
    return extra


def test_soporte_tercer_toque_compra():
    s, motivo = be.senal_sr(escenario_soporte("rechazo"))
    assert s and s["dir"] == "LONG", motivo
    assert s["toques"] >= 3 and abs(s["nivel"] - 1.0850) < 0.0006
    assert s["sl"] < s["nivel"] < s["entrada"] < s["tp1"] < s["tp2"]


def test_resistencia_tercer_toque_venta():
    df = escenario_soporte("rechazo")
    esp = pd.DataFrame({"open": 2.2 - df["open"], "high": 2.2 - df["low"], "low": 2.2 - df["high"],
                        "close": 2.2 - df["close"], "volume": 1.0}, index=df.index)
    s, motivo = be.senal_sr(esp)
    assert s and s["dir"] == "SHORT", motivo
    assert s["tp2"] < s["tp1"] < s["entrada"] < s["nivel"] < s["sl"]


def test_soporte_sin_toque_espera():
    s, motivo = be.senal_sr(escenario_soporte("lejos"))
    assert s is None


def test_mensajes_llevan_el_nombre_del_bot(monkeypatch):
    monkeypatch.setenv("TELEGRAM_TOKEN", "x")
    monkeypatch.setenv("TELEGRAM_CHAT_IDS", "1")
    monkeypatch.setattr(be, "_dormir", lambda s: None)
    textos = []

    class R:
        ok, status_code, text = True, 200, ""
    monkeypatch.setattr(be.requests, "post", lambda url, json=None, data=None, files=None, timeout=None:
                        textos.append(json["text"]) or R())
    be.enviar_telegram("🚨 🟢 COMPRA EUR/USD · 5M")
    assert textos[0].splitlines()[0] == "🐋 BALLENA EMAS"


# ----------------------------------------------------------------- ruptura de nivel fuerte
def escenario_rompe_soporte(toques=3):
    """Soporte ~1.0850 tocado 'toques' veces y una vela final que cierra con cuerpo por debajo."""
    tramo = lambda a, b, n: np.linspace(a, b, n)
    p = [tramo(1.0930, 1.0900, 30), tramo(1.0900, 1.0850, 15)]
    for i in range(toques - 1):
        p += [tramo(1.0850, 1.0885, 10), tramo(1.0885, 1.0851, 10)]
    p += [tramo(1.0851, 1.0870, 8), tramo(1.0870, 1.0856, 6)]
    c = np.concatenate(p)
    df = velas(np.r_[c, 1.0832])
    df.iloc[-1, df.columns.get_loc("open")] = c[-1]
    return df


def test_rompe_soporte_fuerte_venta():
    s, motivo = be.senal_ruptura_sr(escenario_rompe_soporte(3))
    assert s and s["dir"] == "SHORT", motivo
    assert s["toques"] >= 3 and s["tp2"] < s["tp1"] < s["entrada"] < s["nivel"] < s["sl"]


def test_rompe_resistencia_fuerte_compra():
    df = escenario_rompe_soporte(3)
    esp = pd.DataFrame({"open": 2.2 - df["open"], "high": 2.2 - df["low"], "low": 2.2 - df["high"],
                        "close": 2.2 - df["close"], "volume": 1.0}, index=df.index)
    s, motivo = be.senal_ruptura_sr(esp)
    assert s and s["dir"] == "LONG", motivo
    assert s["sl"] < s["nivel"] < s["entrada"] < s["tp1"] < s["tp2"]


def test_nivel_con_dos_toques_no_es_fuerte():
    s, motivo = be.senal_ruptura_sr(escenario_rompe_soporte(2))
    assert s is None


def test_solo_los_3_mercados_elegidos():
    assert list(be.MERCADOS) == ["XAU/USD (Oro)", "BTC/USD", "NASDAQ 100"]
    assert be.MERCADOS["BTC/USD"]["ccxt"] == "BTC/USDT" and be.MERCADOS["NASDAQ 100"]["yf"] == "NQ=F"
    assert be.fmt(198.4567) == "198.457" and be.fmt(1.085674) == "1.08567"


def test_cache_por_vela():
    base = 1_700_000_000 - (1_700_000_000 % 900)                 # inicio exacto de una vela de 15m
    assert be._sigue_valida(base + 20, "15m", base + 600)        # misma vela de 15m: no se descarga
    assert not be._sigue_valida(base + 20, "15m", base + 920)    # cerro una vela nueva: se descarga
    assert not be._sigue_valida(base + 20, "5m", base + 320)     # en 5m: cada escaneo


# ----------------------------------------------------------------- senales combinadas
@pytest.fixture(autouse=True)
def modo_sueltas_por_defecto(request, monkeypatch):
    """Los tests de cada estrategia usan el modo 'sueltas'; los de combinadas lo cambian ellos."""
    nombre = request.node.name
    if "combinad" in nombre:
        monkeypatch.setitem(be.CONFIG, "modo_senales", "combinadas")
    elif "consenso" not in nombre:
        monkeypatch.setitem(be.CONFIG, "modo_senales", "sueltas")


def escenario_rompe_y_retestea():
    """Soporte fuerte (3 toques) roto a la baja, vuelta al nivel desde abajo y vela bajista de confirmacion."""
    df = escenario_rompe_soporte(3)
    c = df["close"].to_numpy()
    extra = np.r_[c, 1.0828, 1.0840, 1.0846, 1.0835]          # cae, sube al nivel (~1.0847), confirma abajo
    out = velas(extra, inicio=str(df.index[0].tz_convert(None)))
    out.iloc[-1, out.columns.get_loc("open")] = 1.0845
    return out


def test_combinada_ruptura_fuerte_y_retest():
    s = be.senal_retest_nivel_fuerte(escenario_rompe_y_retestea())
    assert s and s["dir"] == "SHORT"
    assert s["tp2"] < s["tp1"] < s["entrada"] < s["nivel"] < s["sl"]


def test_combinada_ob_y_soporte(monkeypatch):
    df, d1 = escenario_ob("confirma"), marea_alcista()
    ob, _ = be.senal_orderblock(df, d1)
    nivel = sum(ob["zona"]) / 2                                   # un soporte justo dentro de la zona del bloque
    monkeypatch.setattr(be, "_soportes", lambda d, k=3, toques_min=None: [(nivel, [10, 30, 50])])
    combos, _ = be.senales_combinadas(df, d1, "5m")
    c = next(x for x in combos if x["combo"] == "ob_sr")
    assert "Order Block + Soporte" in c["combo_txt"] and "(3 toques)" in c["combo_txt"]
    assert c["sl"] <= ob["sl"] and c["tp1"] - c["entrada"] == pytest.approx(2 * (c["entrada"] - c["sl"]))


def test_combinada_ob_sin_nivel_no_se_envia(monkeypatch):
    df, d1 = escenario_ob("confirma"), marea_alcista()
    monkeypatch.setattr(be, "_soportes", lambda d, k=3, toques_min=None: [])
    monkeypatch.setattr(be, "_cruce_reciente", lambda df_, m, velas=None: False)
    monkeypatch.setitem(be.CONFIG, "combinaciones", ["ob_sr", "ob_cruce"])
    assert be.senales_combinadas(df, d1, "5m")[0] == []          # el OB solo NO basta


def test_combinada_ob_y_cruce(monkeypatch):
    df, d1 = escenario_ob("confirma"), marea_alcista()
    monkeypatch.setattr(be, "_cruce_reciente", lambda df_, m, velas=None: True)
    monkeypatch.setitem(be.CONFIG, "combinaciones", ["ob_cruce"])
    combos, _ = be.senales_combinadas(df, d1, "5m")
    assert combos and combos[0]["combo"] == "ob_cruce" and combos[0]["dir"] == "LONG"


def test_combinada_cruce_mtf(monkeypatch):
    df, d1 = cruce_confirmado(), marea_alcista()
    monkeypatch.setitem(be.CONFIG, "combinaciones", ["cruce_mtf"])
    monkeypatch.setattr(be, "_mtf_alineado", lambda df_, d1_, m, tf: True)
    assert be.senales_combinadas(df, d1, "5m")[0][0]["combo"] == "cruce_mtf"
    monkeypatch.setattr(be, "_mtf_alineado", lambda df_, d1_, m, tf: False)
    assert be.senales_combinadas(df, d1, "5m")[0] == []          # sin 15m/1H alineadas no hay senal


def test_combinadas_contrarias_no_se_envian(monkeypatch):
    largo = {"bias": 1, "dir": "LONG", "entrada": 1.0, "sl": 0.99, "tp1": 1.02, "tp2": 1.03, "riesgo_px": 0.01,
             "vela": pd.Timestamp("2026-11-02", tz="UTC"), "zona": (0.995, 0.998)}
    corto = {**largo, "bias": -1, "dir": "SHORT", "sl": 1.01, "tp1": 0.98, "tp2": 0.97, "nivel": 1.0, "toques": 3}
    monkeypatch.setattr(be, "senal_orderblock", lambda df_, d1_: (largo, ""))
    monkeypatch.setattr(be, "_cruce_reciente", lambda df_, m, velas=None: True)
    monkeypatch.setattr(be, "senal_retest_nivel_fuerte", lambda df_: corto)
    monkeypatch.setitem(be.CONFIG, "combinaciones", ["ob_cruce", "ruptura_retest"])
    combos, motivo = be.senales_combinadas(None, None, "5m")
    assert combos == [] and "contrarias" in motivo


def test_combinadas_solo_llega_la_confirmada(monkeypatch, tmp_path):
    df, d1 = escenario_ob("confirma"), marea_alcista()
    monkeypatch.setattr(be, "MERCADOS", {"EUR/USD": {"tipo": "forex", "yf": "x"}})
    monkeypatch.setitem(be.CONFIG, "tfs", ["5m"])
    monkeypatch.setitem(be.CONFIG, "combinaciones", ["ob_cruce"])
    monkeypatch.setattr(be, "_cruce_reciente", lambda df_, m, velas=None: True)
    enviados = []
    monkeypatch.setattr(be, "enviar_telegram", lambda txt, png=None: enviados.append(txt) or (True, "ok"))
    n = be.escanear(be.Estado(str(tmp_path / "e.json")), lambda nm, tf: d1 if tf == "1h" else df,
                    df.index[-1] + pd.Timedelta(minutes=5), verbose=False)
    assert n == 1 and enviados[0].startswith("🚨 ⭐ CONFIRMADA 🟢 COMPRA EUR/USD · 5M")
    assert "Order Block + Cruce EMA 9/21" in enviados[0]



# ----------------------------------------------------------------- consenso de 3+ estrategias
def _s(m, sl_off=0.01):
    return {"bias": m, "dir": "LONG" if m > 0 else "SHORT", "entrada": 1.0, "sl": 1.0 - m * sl_off,
            "tp1": 1.0 + m * 2 * sl_off, "tp2": 1.0 + m * 3 * sl_off, "riesgo_px": sl_off,
            "vela": pd.Timestamp("2026-11-02 15:00", tz="UTC"), "zona": (0.995, 0.998)}


def _ventana(**k):
    base = {x: [] for x in ("cruce", "orderblock", "sr", "retest", "ruptura_sr", "retest_fuerte")}
    base.update(k)
    return base


def _preparar(monkeypatch, ventana, mtf=False, cruce=False, nivel=False):
    monkeypatch.setattr(be, "_senales_en_ventana", lambda df, d1, velas: ventana)
    monkeypatch.setattr(be, "_mtf_alineado", lambda df, d1, m, tf: mtf if m > 0 else False)
    monkeypatch.setattr(be, "_cruce_reciente", lambda df, m, velas=None: cruce if m > 0 else False)
    monkeypatch.setattr(be, "_nivel_en_zona", lambda df, ob: (1.0, [1, 2]) if nivel else None)
    return velas(np.linspace(1.0, 1.01, 150))


def test_consenso_tres_estrategias_se_envia(monkeypatch):
    df = _preparar(monkeypatch, _ventana(orderblock=[(0, _s(1))]), mtf=True, cruce=True)
    out, motivo = be.senales_consenso(df, None, "5m")
    assert out and out[0]["votos"] == 3, motivo
    assert out[0]["combo_txt"] == "Order Block + Cruce EMA + Confluencia 1H/15m"


def test_consenso_dos_estrategias_no_se_envia(monkeypatch):
    df = _preparar(monkeypatch, _ventana(orderblock=[(0, _s(1))]), mtf=True)
    out, motivo = be.senales_consenso(df, None, "5m")
    assert out == [] and "menos de 3" in motivo


def test_consenso_sin_gatillo_actual_no_se_envia(monkeypatch):
    df = _preparar(monkeypatch, _ventana(orderblock=[(2, _s(1))], sr=[(1, _s(1))]), mtf=True, cruce=True)
    assert be.senales_consenso(df, None, "5m")[0] == []            # 4 votos pero ninguno en la vela actual


def test_consenso_ruptura_fuerte_reprobada_cuenta_doble(monkeypatch):
    df = _preparar(monkeypatch, _ventana(retest_fuerte=[(0, _s(1))]), mtf=True)
    out, _ = be.senales_consenso(df, None, "5m")
    assert out and out[0]["votos"] == 3 and "Ruptura + retest + Ruptura de nivel fuerte" in out[0]["combo_txt"]


def test_consenso_stop_mas_protegido(monkeypatch):
    df = _preparar(monkeypatch, _ventana(orderblock=[(0, _s(1, 0.01))], sr=[(0, _s(1, 0.02))]), mtf=True)
    out, _ = be.senales_consenso(df, None, "5m")
    assert out[0]["sl"] == pytest.approx(0.98) and out[0]["tp1"] == pytest.approx(1.04)


def test_consenso_en_las_dos_direcciones_no_opera(monkeypatch):
    v = _ventana(orderblock=[(0, _s(1))], sr=[(0, _s(-1))], cruce=[(0, _s(-1))], retest=[(0, _s(-1))])
    df = _preparar(monkeypatch, v, mtf=True, cruce=True)
    out, motivo = be.senales_consenso(df, None, "5m")
    assert out == [] and "dos direcciones" in motivo


def test_consenso_mensaje(monkeypatch):
    df = _preparar(monkeypatch, _ventana(orderblock=[(0, _s(1))]), mtf=True, cruce=True, nivel=True)
    s = be.senales_consenso(df, None, "5m")[0][0]
    s.update(activo="EUR/USD", tf="5m", tipo="forex")
    txt = be.texto_alerta(s)
    assert txt.startswith("🚨 ⭐⭐⭐⭐ CONFIRMADA x4 🟢 COMPRA EUR/USD · 5M")
    assert "Order Block + Soporte/Resistencia + Cruce EMA + Confluencia 1H/15m" in txt


def test_el_programa_arranca():
    """Las opciones de la linea de comandos no chocan (si chocan, el bot no arranca)."""
    import subprocess
    raiz = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    r = subprocess.run([sys.executable, os.path.join(raiz, "ballena_emas.py"), "--help"], capture_output=True, text=True)
    assert r.returncode == 0 and "--avisar-estado" in r.stdout


# ----------------------------------------------------------------- confluencia 1H + 15M obligatoria (consenso)
def test_consenso_sin_confluencia_1h_15m_no_se_envia(monkeypatch):
    df = _preparar(monkeypatch, _ventana(orderblock=[(0, _s(1))], sr=[(0, _s(1))], retest=[(0, _s(1))]),
                   mtf=False)                                              # 3 votos, pero 1H/15M no acompanan
    out, motivo = be.senales_consenso(df, None, "5m")
    assert out == [] and "sin confluencia 1H + 15M" in motivo


def test_consenso_con_confluencia_lleva_la_etiqueta(monkeypatch):
    df = _preparar(monkeypatch, _ventana(orderblock=[(0, _s(1))]), mtf=True, cruce=True)
    s = be.senales_consenso(df, None, "5m")[0][0]
    s.update(activo="BTC/USD", tf="5m", tipo="cripto")
    assert "⭐ CONFLUENCIA 1H + 15M" in be.texto_alerta(s).splitlines()[0]


def test_en_15m_tambien_cuenta_la_propia_vela(monkeypatch):
    monkeypatch.setattr(be, "_mtf_alineado", lambda df, d1, m, tf: True)    # 1H alineada
    sube = velas(np.linspace(1.00, 1.05, 80), freq="15min")
    baja = velas(np.linspace(1.05, 1.00, 80), freq="15min")
    assert be.confluencia_1h_15m(sube, None, 1, "15m") is True
    assert be.confluencia_1h_15m(baja, None, 1, "15m") is False             # 15M en contra
