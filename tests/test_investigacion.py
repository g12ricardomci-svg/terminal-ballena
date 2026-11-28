# -*- coding: utf-8 -*-
import os
import sys

import numpy as np
import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import investigacion as inv  # noqa: E402


def test_laboratorio_corre_y_compara_con_el_azar():
    rng = np.random.default_rng(2)
    n = 3000
    c = 2600 + np.cumsum(rng.normal(0, 1.2, n))
    o = np.r_[c[0], c[:-1]] + rng.normal(0, 0.3, n)
    df = pd.DataFrame(dict(open=o, high=np.maximum(o, c) + rng.exponential(0.6, n),
                           low=np.minimum(o, c) - rng.exponential(0.6, n), close=c, volume=1.0),
                      index=pd.date_range("2026-10-20", periods=n, freq="5min", tz="UTC"))
    r = inv.estudiar("XAU/USD (Oro)", "5m", df, np.random.default_rng(1))
    assert r["casos"] > 500 and r["combinaciones"] > 100000 and len(r["formulas_azar"]) == 3


def test_triple_barrera():
    idx = pd.date_range("2026-11-02", periods=30, freq="5min", tz="UTC")
    c = np.r_[np.full(5, 100.0), 100.0, np.linspace(100.5, 110, 24)]
    df = pd.DataFrame(dict(open=c, high=c + 0.1, low=c - 0.1, close=c, volume=1.0), index=idx)
    casos, R = inv.resultados(df, np.array([5]), np.full(30, 1.0), 0.0)
    k = inv.SALIDAS.index((1, 1.0, 2.0, 10))
    assert R[0, k] == 2.0                                   # COMPRA: toca TP 2 ATR antes que SL 1 ATR
    k = inv.SALIDAS.index((-1, 1.0, 2.0, 10))
    assert R[0, k] == -1.0                                  # VENTA: toca el SL
