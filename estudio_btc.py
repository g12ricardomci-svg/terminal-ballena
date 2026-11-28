# -*- coding: utf-8 -*-
"""
¿COMO SUPERAR EL BTC? - el rango de la vela madre en BTC con filtros sacados de estudios (no envia nada)
=====================================================================================================
Filtros probados (solos y combinados), con 0,01 lotes y spread de Vantage ($20):
  vantage   : a favor del voto 4H estilo Vantage AI
  spread    : el SL vale al menos 10 veces el spread (el spread pesa <= 10 % del riesgo)
  usa       : horario de EE. UU. (13-20 UTC, K33: las horas mas fuertes de BTC)
  noche     : 21-23 UTC (Quantpedia: las horas mas fuertes despues del cierre de Wall Street)
  semana    : lunes a viernes (sin fin de semana)
  momentum  : a favor del momentum de 30 dias (time-series momentum, lo mas solido en cripto)
Temporalidades 5M, 15M, 1H y 4H. Se mira el resultado y si gana en las DOS mitades del periodo.
"""
import itertools

import numpy as np
import pandas as pd

import ballena_dorada as bd

NOMBRE, VALOR = "BTC/USD", 0.01


def operaciones(df):
    o, h, l, c = (df[x].to_numpy(float) for x in ("open", "high", "low", "close"))
    a = bd.atr(df).to_numpy()
    senal = bd.rango_madre(df)
    sp, nv = bd.MERCADOS[NOMBRE]["spread"], bd.CONFIG["rm_velas"]
    vdir = bd.vantage_series(df)["dir"].to_numpy()
    d1 = df["close"].resample("1D").last().dropna()
    mom = np.sign(d1 - d1.shift(30))
    pos = mom.index.searchsorted(df.index, side="right") - 2          # dia anterior ya cerrado
    mom_v = np.where(pos >= 0, mom.to_numpy()[np.clip(pos, 0, None)], 0)
    ops, libre = [], 0
    for i in np.nonzero(senal)[0]:
        if i < max(libre, 15) or i + nv >= len(c):
            continue
        d, rev = int(np.sign(senal[i])), abs(senal[i]) == 2
        e, tp, sl = bd._plan_rango(o, h, l, c, a, i, d, rev)
        riesgo = abs(e - sl)
        if riesgo <= 0 or abs(tp - e) / riesgo < bd.CONFIG["rm_min_rr"]:
            continue
        res, fin = None, i + nv
        for k in range(i + 1, i + 1 + nv):
            if (l[k] <= sl) if d > 0 else (h[k] >= sl):
                res, fin = -riesgo, k
                break
            if (h[k] >= tp) if d > 0 else (l[k] <= tp):
                res, fin = abs(tp - e), k
                break
        if res is None:
            res = (c[i + nv] - e) * d
        t = df.index[i]
        ops.append({"t": t, "usd": (res - sp) * VALOR, "sl_usd": (riesgo + sp) * VALOR,
                    "vantage": vdir[i] == d, "spread": riesgo >= 10 * sp, "usa": 13 <= t.hour < 20,
                    "noche": 21 <= t.hour < 24, "semana": t.weekday() < 5, "momentum": mom_v[i] == d})
        libre = fin + 1
    return pd.DataFrame(ops)


def caida(x):
    eq = np.cumsum(x)
    return float((np.maximum.accumulate(np.r_[0, eq])[1:] - eq).max()) if len(x) else 0.0


def main():
    filtros = ["vantage", "spread", "usa", "noche", "semana", "momentum"]
    combos = [()] + [(f,) for f in filtros] + list(itertools.combinations(filtros, 2)) + \
        [c for c in itertools.combinations(filtros, 3) if "vantage" in c]
    for tf in ("5m", "15m", "1h", "4h"):
        base_tf = "1h" if tf == "4h" else tf
        df = bd.solo_cerradas(bd.historial(NOMBRE, base_tf), base_tf)
        if tf == "4h":
            df = df.resample("4h", label="left", closed="left").agg(
                {"open": "first", "high": "max", "low": "min", "close": "last", "volume": "sum"}).dropna()
        t = operaciones(df)
        print(f"\n===== BTC {tf} · {len(df)} velas · {df.index[0]:%Y-%m-%d} -> {df.index[-1]:%Y-%m-%d} =====")
        filas = []
        for cmb in combos:
            sel = t[np.logical_and.reduce([t[f] for f in cmb])] if cmb else t
            if len(sel) < 20:
                continue
            m = len(sel) // 2
            filas.append((" + ".join(cmb) or "sin filtro", len(sel), sel["usd"].sum(), sel["usd"].mean(),
                          sel["usd"].iloc[:m].sum(), sel["usd"].iloc[m:].sum(), caida(sel["usd"].to_numpy()),
                          sel["sl_usd"].median(), 100 * (sel["usd"] > 0).mean()))
        filas.sort(key=lambda x: -x[3])
        for nom, n, tot, media, m1, m2, cd, slm, gana in filas[:12] + [f for f in filas if f[0] == "sin filtro"]:
            marca = "✅" if m1 > 0 and m2 > 0 else "  "
            print(f"  {marca} {nom:<32} {n:>4} ops · {tot:>+8.2f}$ ({media:+.3f}$/op, gana {gana:.0f}%) · mitades "
                  f"{m1:+.2f}/{m2:+.2f}$ · caída {cd:.2f}$ · SL típico {slm:.2f}$")


if __name__ == "__main__":
    main()
