# -*- coding: utf-8 -*-
"""
¿QUE HACE QUE FUNCIONEN LAS VELAS JAPONESAS? - los 61 patrones de TA-Lib medidos con y sin contexto
==================================================================================================
Segun Nison y Bulkowski una vela sola no basta. Se mide cada patron (en su direccion oficial) en 5 versiones:
  A) crudo: entrar al cierre de la vela del patron
  B) + CONFIRMACION (Nison): la vela siguiente cierra por encima del maximo (alcista) / debajo del minimo (bajista)
  C) + UBICACION: alcista en soporte (minimo de 20 velas, PDL o minimo de la vela mayor, a 0,5 ATR) o en el tercio
     bajo del rango de 100 velas; bajista en resistencia o en el tercio alto
  D) confirmacion + ubicacion
  E) D + a favor de la tendencia mayor
Operacion: SL 1 ATR, TP 1,5 ATR, maximo 10 velas, spread de Vantage. Se juntan Oro, BTC y NAS100 en 5M, 15M y 1H.
"""
import sys

import numpy as np
import pandas as pd

import ballena_dorada as bd
import biblioteca_patrones as bib


def contexto(df, tf):
    o, h, l, c = (df[x].to_numpy(float) for x in ("open", "high", "low", "close"))
    v = df["volume"].to_numpy(float) if "volume" in df else np.ones(len(c))
    a = bd.atr(df).to_numpy()
    s = pd.Series
    min20, max20 = s(l).rolling(20).min().shift(1).to_numpy(), s(h).rolling(20).max().shift(1).to_numpy()
    d = df.resample("1D").agg({"high": "max", "low": "min"}).shift(1)
    pdh = d["high"].reindex(df.index.floor("1D")).to_numpy()
    pdl = d["low"].reindex(df.index.floor("1D")).to_numpy()
    _, hH, hL = bd._vela_mayor_anterior(df, tf)
    cerca = lambda x, nivel: np.abs(x - nivel) <= 0.5 * a
    soporte = cerca(l, min20) | cerca(l, pdl) | cerca(l, hL) | (l < min20)
    resistencia = cerca(h, max20) | cerca(h, pdh) | cerca(h, hH) | (h > max20)
    hi100, lo100 = s(h).rolling(100).max().to_numpy(), s(l).rolling(100).min().to_numpy()
    pos = (c - lo100) / np.where(hi100 > lo100, hi100 - lo100, np.nan)
    vol_alto = v > 1.2 * s(v).rolling(20).mean().to_numpy()
    return {"soporte": soporte | (pos < 1 / 3), "resistencia": resistencia | (pos > 2 / 3), "vol_alto": vol_alto,
            "h": h, "l": l, "c": c, "a": a}


def r_operacion(df, idx, d, spread, sl=1.0, tp=1.5, nv=10):
    h, l, c = (df[x].to_numpy(float) for x in ("high", "low", "close"))
    a = bd.atr(df).to_numpy()
    idx = idx[(idx + nv < len(c)) & (idx > 120)]
    if len(idx) == 0:
        return np.array([])
    off = np.arange(1, nv + 1)
    H, L, C = h[idx[:, None] + off], l[idx[:, None] + off], c[idx[:, None] + off]
    e, at = c[idx][:, None], a[idx][:, None]
    fav = np.maximum.accumulate((H - e) if d > 0 else (e - L), axis=1) / at
    adv = np.maximum.accumulate((e - L) if d > 0 else (H - e), axis=1) / at
    t_sl = np.where((adv >= sl).any(1), (adv >= sl).argmax(1), 10 ** 6)
    t_tp = np.where((fav >= tp).any(1), (fav >= tp).argmax(1), 10 ** 6)
    fin = (C[:, -1] - e[:, 0]) * d / a[idx]
    r = np.where((t_sl < nv) & (t_sl <= t_tp), -1.0, np.where(t_tp < nv, tp / sl, fin / sl))
    return r - spread / (sl * a[idx])


def main():
    filas = []
    for nombre, meta in bd.MERCADOS.items():
        for tf in bd.CONFIG["temporalidades"]:
            try:
                df = bd.solo_cerradas(bd.historial(nombre, tf), tf)
            except Exception as e:
                print(f"{nombre} {tf}: ERROR {e}")
                continue
            J = bd.velas_japonesas(df)
            X = contexto(df, tf)
            tend = bib.tendencia(df, tf)
            n = len(df)
            for f, cod in enumerate(bd.JAPONESAS):
                for d in (1, -1):
                    pat = np.nonzero(J[:, f] == d)[0]
                    pat = pat[pat + 1 < n]
                    if len(pat) < 5:
                        continue
                    conf = (X["c"][pat + 1] > X["h"][pat]) if d > 0 else (X["c"][pat + 1] < X["l"][pat])
                    ubic = X["soporte"][pat] if d > 0 else X["resistencia"][pat]
                    versiones = {"A crudo": pat, "B +confirmación": pat[conf] + 1, "C +ubicación": pat[ubic],
                                 "D conf+ubic": pat[conf & ubic] + 1,
                                 "E conf+ubic+tendencia": (pat[conf & ubic] + 1)[tend[pat[conf & ubic] + 1] == d],
                                 "F conf+ubic+volumen": pat[conf & ubic & X["vol_alto"][pat]] + 1}
                    for ver, idx in versiones.items():
                        r = r_operacion(df, idx, d, meta["spread"])
                        if len(r):
                            m = len(r) // 2
                            filas.append({"patron": bd.JAPONESAS[cod], "lado": "alcista" if d > 0 else "bajista",
                                          "version": ver, "mtf": f"{nombre} {tf}", "n": len(r), "suma": r.sum(),
                                          "m1": r[:m].sum(), "n1": m, "m2": r[m:].sum(), "n2": len(r) - m})
            print(f"listo {nombre} {tf}", flush=True)
    t = pd.DataFrame(filas)
    print("\n===== EFECTO DEL CONTEXTO (todos los patrones juntos, R medio por operacion) =====")
    g = t.groupby("version")[["n", "suma"]].sum()
    for ver, r in g.iterrows():
        print(f"  {ver:<24} {int(r['n']):>7} operaciones · {r['suma'] / r['n']:+.3f} R")
    print("\n===== MEJORES PATRON + CONTEXTO (>= 40 casos, positivo en las dos mitades, juntando mercados) =====")
    g = t.groupby(["patron", "lado", "version"]).agg(n=("n", "sum"), suma=("suma", "sum"), m1=("m1", "sum"),
                                                      n1=("n1", "sum"), m2=("m2", "sum"), n2=("n2", "sum"),
                                                      mtf=("mtf", "nunique"))
    g["r"], g["r1"], g["r2"] = g["suma"] / g["n"], g["m1"] / g["n1"].clip(lower=1), g["m2"] / g["n2"].clip(lower=1)
    g = g[(g["n"] >= 40) & (g["r1"] > 0) & (g["r2"] > 0)].sort_values("r", ascending=False)
    for (p, lado, ver), r in g.head(30).iterrows():
        print(f"  {r['r']:+.2f} R · {p} ({lado}) · {ver} · {int(r['n'])} casos en {int(r['mtf'])} mercado-tf · "
              f"mitades {r['r1']:+.2f}/{r['r2']:+.2f}")
    sys.stdout.flush()


if __name__ == "__main__":
    main()
