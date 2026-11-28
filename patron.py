# -*- coding: utf-8 -*-
"""
PATRON: BARRIDO DE LIQUIDEZ + VELA ENVOLVENTE (Turtle Soup / CRT de reversion) - ¿siempre da impulso?
=====================================================================================================
COMPRA (la venta es el espejo):
  1) venia bajando (cierre de la vela anterior < cierre de 5 velas antes)
  2) la vela anterior es bajista
  3) la vela del patron BARRE la liquidez: su minimo queda por debajo del minimo de las 2 velas anteriores
  4) cierra ALCISTA y ENVUELVE el cuerpo de la anterior (cierra por encima de su apertura)
  5) cuerpo grande (>= N x ATR) y (variante) mecha inferior larga
Mide, con datos reales: % de veces que 5 y 10 velas despues el precio esta mas arriba (contra la base de TODAS
las velas), hasta donde llega a favor y en contra, y la operacion con SL bajo la mecha del barrido y TP 1R/2R/3R
(spread de Vantage descontado). Separa el periodo en dos mitades para ver si se repite.
"""
import sys

import numpy as np
import pandas as pd

import ballena_dorada as bd

VARIANTES = {
    "suave (cuerpo >= 0,5 ATR)": {"cuerpo": 0.5, "mecha": 0.0},
    "como la foto (cuerpo >= 1 ATR)": {"cuerpo": 1.0, "mecha": 0.0},
    "fuerte (cuerpo >= 1 ATR + mecha >= 50 % del cuerpo)": {"cuerpo": 1.0, "mecha": 0.5},
    "muy fuerte (cuerpo >= 1,5 ATR)": {"cuerpo": 1.5, "mecha": 0.0},
}


def senales(df, cuerpo_min, mecha_min):
    o, h, l, c = (df[x].to_numpy(float) for x in ("open", "high", "low", "close"))
    a = bd.atr(df).to_numpy()
    n = len(c)
    i = np.arange(6, n)
    cuerpo = np.abs(c[i] - o[i])
    compra = ((c[i - 1] < c[i - 5]) & (c[i - 1] < o[i - 1]) & (l[i] < np.minimum(l[i - 1], l[i - 2]))
              & (c[i] > o[i]) & (c[i] > o[i - 1]) & (cuerpo >= cuerpo_min * a[i])
              & ((np.minimum(o[i], c[i]) - l[i]) >= mecha_min * cuerpo))
    venta = ((c[i - 1] > c[i - 5]) & (c[i - 1] > o[i - 1]) & (h[i] > np.maximum(h[i - 1], h[i - 2]))
             & (c[i] < o[i]) & (c[i] < o[i - 1]) & (cuerpo >= cuerpo_min * a[i])
             & ((h[i] - np.maximum(o[i], c[i])) >= mecha_min * cuerpo))
    return i[compra], i[venta]


def medir(df, idx, d, spread, max_velas=20):
    h, l, c = (df[x].to_numpy(float) for x in ("high", "low", "close"))
    a = bd.atr(df).to_numpy()
    idx = idx[idx + max_velas < len(c)]
    if len(idx) == 0:
        return None
    off = np.arange(1, max_velas + 1)
    H, L, C = h[idx[:, None] + off], l[idx[:, None] + off], c[idx[:, None] + off]
    e = c[idx]
    sube5, sube10 = (C[:, 4] - e) * d > 0, (C[:, 9] - e) * d > 0
    fav = np.maximum.accumulate((H - e[:, None]) if d > 0 else (e[:, None] - L), axis=1)
    adv = np.maximum.accumulate((e[:, None] - L) if d > 0 else (H - e[:, None]), axis=1)
    sl_px = (e - l[idx]) if d > 0 else (h[idx] - e)                      # SL detras de la mecha del barrido
    sl_px = sl_px + 0.1 * a[idx]
    out = {"n": len(idx), "sube5": 100 * sube5.mean(), "sube10": 100 * sube10.mean(),
           "fav10_atr": float(np.median(fav[:, 9] / a[idx])), "adv10_atr": float(np.median(adv[:, 9] / a[idx]))}
    for rr in (1, 2, 3):
        t_sl = np.where((adv >= sl_px[:, None]).any(1), (adv >= sl_px[:, None]).argmax(1), 10 ** 6)
        t_tp = np.where((fav >= rr * sl_px[:, None]).any(1), (fav >= rr * sl_px[:, None]).argmax(1), 10 ** 6)
        fin = (C[:, -1] - e) * d / sl_px
        r = np.where((t_sl < max_velas) & (t_sl <= t_tp), -1.0, np.where(t_tp < max_velas, rr, fin)) - spread / sl_px
        mitad = len(r) // 2
        out[f"R{rr}"] = (float(r.mean()), float(100 * (r > 0).mean()), float(r[:mitad].mean()), float(r[mitad:].mean()))
    return out


def base(df, d):
    c = df["close"].to_numpy(float)
    return 100 * (((c[5:] - c[:-5]) * d) > 0).mean()


def main():
    for nombre, meta in bd.MERCADOS.items():
        for tf in bd.CONFIG["temporalidades"]:
            try:
                df = bd.solo_cerradas(bd.historial(nombre, tf), tf)
            except Exception as e:
                print(f"\n===== {nombre} {tf}: ERROR {e}")
                continue
            print(f"\n===== {nombre} {tf} · {len(df)} velas · {df.index[0]:%Y-%m-%d} -> {df.index[-1]:%Y-%m-%d} =====")
            print(f"  base (cualquier vela): sube en 5 velas {base(df, 1):.0f}% · baja {base(df, -1):.0f}%")
            for nv, v in VARIANTES.items():
                compras, ventas = senales(df, v["cuerpo"], v["mecha"])
                for lado, idx, d in (("COMPRA", compras, 1), ("VENTA", ventas, -1)):
                    m = medir(df, idx, d, meta["spread"])
                    if not m:
                        print(f"  {nv:<52} {lado:<6} sin casos")
                        continue
                    rr = " · ".join(f"TP {k[1]}R: {m[k][0]:+.2f}R (gana {m[k][1]:.0f}%, mitades {m[k][2]:+.2f}/{m[k][3]:+.2f})"
                                    for k in ("R1", "R2", "R3"))
                    print(f"  {nv:<52} {lado:<6} {m['n']:>4} casos · a favor 5v {m['sube5']:.0f}% · 10v {m['sube10']:.0f}% · "
                          f"llega {m['fav10_atr']:.1f} ATR / contra {m['adv10_atr']:.1f} ATR\n      {rr}")
            sys.stdout.flush()


if __name__ == "__main__":
    main()
