# -*- coding: utf-8 -*-
"""
RIESGO DE BALLENA DORADA CON 0,01 LOTES - ¿cuanto saldo hace falta para aguantar? (no envia nada)
================================================================================================
Simula TODAS las senales del rango de la vela madre del historial (una operacion a la vez por mercado y
temporalidad, igual que el bot) y mide en DOLARES con 0,01 lotes en Vantage:
  BTC: 0,01 BTC -> $0,01 por cada $1 · Oro: 1 onza -> $1 por cada $1 · NAS100: $0,01 por punto (revisalo en MT5)
SL mas grande, SL tipico, peor racha de perdidas seguidas, caida maxima de la cuenta y resultado final.
"""
import numpy as np
import pandas as pd

import ballena_dorada as bd

VALOR = {"XAU/USD (Oro)": 1.0, "BTC/USD": 0.01, "NASDAQ 100": 0.01}     # $ por cada 1 de precio con 0,01 lotes


def simular(df, nombre):
    o, h, l, c = (df[x].to_numpy(float) for x in ("open", "high", "low", "close"))
    a = bd.atr(df).to_numpy()
    senal = bd.rango_madre(df)
    v, sp, nv = VALOR[nombre], bd.MERCADOS[nombre]["spread"], bd.CONFIG["rm_velas"]
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
        ops.append({"t": df.index[fin], "usd": (res - sp) * v, "sl_usd": (riesgo + sp) * v})
        libre = fin + 1
    return pd.DataFrame(ops)


def racha(x):
    peor = act = 0
    for u in x:
        act = act + 1 if u < 0 else 0
        peor = max(peor, act)
    return peor


def caida(x):
    eq = np.cumsum(x)
    return float((np.maximum.accumulate(np.r_[0, eq])[1:] - eq).max()) if len(x) else 0.0


def main():
    todas = []
    print(f"{'mercado':<15}{'tf':<5}{'ops':>5}{'SL típico':>11}{'SL 95%':>9}{'SL máx':>9}{'racha':>7}{'caída máx':>11}"
          f"{'resultado':>11}")
    for nombre in bd.MERCADOS:
        for tf in bd.CONFIG["temporalidades"]:
            try:
                df = bd.solo_cerradas(bd.historial(nombre, tf), tf)
                t = simular(df, nombre)
            except Exception as e:
                print(f"{nombre} {tf}: ERROR {e}")
                continue
            if t.empty:
                continue
            t["mtf"] = f"{nombre} {tf}"
            todas.append(t)
            print(f"{nombre:<15}{tf:<5}{len(t):>5}{t['sl_usd'].median():>10.2f}${t['sl_usd'].quantile(.95):>8.2f}$"
                  f"{t['sl_usd'].max():>8.2f}${racha(t['usd']):>7}{caida(t['usd']):>10.2f}${t['usd'].sum():>+10.2f}$"
                  f"   ({df.index[0]:%Y-%m-%d} -> {df.index[-1]:%Y-%m-%d})")
    if todas:
        t = pd.concat(todas).sort_values("t")
        u = t[t["t"] >= t["t"].max() - pd.Timedelta(days=30)]
        print(f"\nTODO JUNTO (los 9 a la vez, ultimos 30 dias): {len(u)} operaciones · peor racha {racha(u['usd'])} · "
              f"caida maxima {caida(u['usd']):.2f}$ · resultado {u['usd'].sum():+.2f}$")
        por_dia = u.groupby(u["t"].dt.date)["usd"].sum()
        print(f"peor dia: {por_dia.min():+.2f}$ · mejor dia: {por_dia.max():+.2f}$ · operaciones por dia ~{len(u) / 30:.0f}")


if __name__ == "__main__":
    main()
