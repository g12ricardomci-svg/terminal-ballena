# -*- coding: utf-8 -*-
"""
BIBLIOTECA DE PATRONES - los patrones que leen los analistas, medidos con datos reales (no envia nada)
====================================================================================================
Cada patron se programa en COMPRA; la VENTA es el mismo patron en el grafico espejo (precio invertido).
Operacion: entrada al cierre de la vela del patron, SL detras del extremo del patron (+0,1 ATR, minimo 0,3 ATR),
TP 1R / 2R / 3R, maximo 20 velas, spread de Vantage descontado. Se separa: TODOS, A FAVOR y EN CONTRA de la
tendencia mayor (H1 para 5m/15m, H4 para 1h: cierre sobre/bajo su EMA 50) y las dos mitades del periodo.
Al final, un RANKING juntando los 3 mercados y las 3 temporalidades (lo que funciona en todos, no en uno).
"""
import sys

import numpy as np
import pandas as pd

import ballena_dorada as bd


def espejo(df):
    return pd.DataFrame({"open": -df["open"], "high": -df["low"], "low": -df["high"], "close": -df["close"],
                         "volume": df.get("volume", 1.0)}, index=df.index)


def patrones(df):
    """{nombre: (mascara de compra, velas del patron para el SL)}"""
    o, h, l, c = (df[x].to_numpy(float) for x in ("open", "high", "low", "close"))
    a = bd.atr(df).to_numpy()
    n = len(c)
    S = lambda x, k: np.r_[np.full(k, np.nan), x[:-k]]          # x[i-k]
    o1, h1, l1, c1 = S(o, 1), S(h, 1), S(l, 1), S(c, 1)
    o2, h2, l2, c2 = S(o, 2), S(h, 2), S(l, 2), S(c, 2)
    c3, o3, c5 = S(c, 3), S(o, 3), S(c, 5)
    cuerpo, rango = np.abs(c - o), h - l
    alc, baj = c > o, c < o
    venia_bajando = c1 < c5
    min10 = pd.Series(l).rolling(10).min().shift(1).to_numpy()
    rng7 = pd.Series(rango).rolling(7).min().to_numpy()
    with np.errstate(invalid="ignore"):
        P = {
            "barrido + envolvente (la foto)": (venia_bajando & (c1 < o1) & (l < np.minimum(l1, l2)) & alc & (c > o1)
                                               & (cuerpo >= a), 2),
            "envolvente clásica": (venia_bajando & (c1 < o1) & alc & (o <= c1) & (c >= o1)
                                   & (cuerpo > np.abs(c1 - o1)), 2),
            "pin bar / martillo (rechazo)": (((np.minimum(o, c) - l) >= 2 / 3 * rango) & (rango >= a)
                                             & (l < np.fmin(l1, l2)), 1),
            "fakey (falsa ruptura de inside bar)": ((h1 < h2) & (l1 > l2) & (l < l1) & (c > l1) & alc, 3),
            "ruptura de inside bar": ((h1 < h2) & (l1 > l2) & (c > h2) & alc, 3),
            "outside bar alcista": ((h > h1) & (l < l1) & (c > h1) & alc, 2),
            "2B de Sperandeo (falso nuevo mínimo)": ((l < min10) & (c > min10), 1),
            "estrella de la mañana": ((c2 < o2) & (np.abs(c2 - o2) >= a) & (np.abs(c1 - o1) <= 0.3 * a) & alc
                                      & (c > (o2 + c2) / 2), 3),
            "tres soldados blancos": (alc & (c1 > o1) & (c2 > o2) & (c > c1) & (c1 > c2) & (cuerpo >= 0.5 * a)
                                      & (np.abs(c1 - o1) >= 0.5 * a) & (np.abs(c2 - o2) >= 0.5 * a), 3),
            "ruptura NR7 (compresión de Crabel)": ((S(rango, 1) <= S(rng7, 1)) & (c > h1) & alc, 2),
            "three line strike (envuelve 3 bajistas)": ((c1 < o1) & (c2 < o2) & (c3 < o3) & (c1 < c2) & (c2 < c3)
                                                        & alc & (c > o3), 4),
            "mínimos iguales (matching low)": (venia_bajando & (c1 < o1) & baj & (np.abs(c - c1) <= 0.05 * a), 2),
        }
    return {k: (np.nan_to_num(m.astype(float), nan=0).astype(bool), v) for k, (m, v) in P.items()}


def tendencia(df, tf):
    """+1 / -1 por vela con la ultima vela mayor CERRADA sobre / bajo su EMA 50."""
    mayor = bd.CONFIG["temporalidades"][tf]
    h = df["close"].resample(mayor, label="left", closed="left").last().dropna()
    s = np.sign(h - bd.ema(h, 50)).to_numpy()
    cierre_h = h.index + pd.Timedelta(minutes=bd.TF_MIN[mayor])
    pos = cierre_h.searchsorted(df.index + pd.Timedelta(minutes=bd.TF_MIN[tf]), side="right") - 1
    return np.where(pos >= 0, s[np.clip(pos, 0, None)], 0)


def operar(df, idx, velas_sl, spread, max_v=20):
    """R para TP 1R, 2R y 3R de cada caso (compra) + proyeccion: hasta donde llego a favor y en contra en 10 velas
    (en ATR) y si 10 velas despues estaba a favor."""
    h, l, c = (df[x].to_numpy(float) for x in ("high", "low", "close"))
    a = bd.atr(df).to_numpy()
    idx = idx[(idx + max_v < len(c)) & (idx > 10)]
    if len(idx) == 0:
        return idx, np.zeros((0, 6))
    e = c[idx]
    ext = np.min(np.stack([l[idx - k] for k in range(velas_sl)]), axis=0)
    sl = np.maximum(e - ext + 0.1 * a[idx], 0.3 * a[idx])
    off = np.arange(1, max_v + 1)
    H, L, C = h[idx[:, None] + off], l[idx[:, None] + off], c[idx[:, None] + off]
    fav = np.maximum.accumulate(H - e[:, None], axis=1)
    adv = np.maximum.accumulate(e[:, None] - L, axis=1)
    t_sl = np.where((adv >= sl[:, None]).any(1), (adv >= sl[:, None]).argmax(1), 10 ** 6)
    out = []
    for rr in (1, 2, 3):
        t_tp = np.where((fav >= rr * sl[:, None]).any(1), (fav >= rr * sl[:, None]).argmax(1), 10 ** 6)
        r = np.where((t_sl < max_v) & (t_sl <= t_tp), -1.0, np.where(t_tp < max_v, rr, (C[:, -1] - e) / sl))
        out.append(r - spread / sl)
    out += [fav[:, 9] / a[idx], adv[:, 9] / a[idx], (C[:, 9] > e).astype(float)]
    return idx, np.column_stack(out)


def main():
    filas = []
    for nombre, meta in bd.MERCADOS.items():
        for tf in bd.CONFIG["temporalidades"]:
            try:
                df0 = bd.solo_cerradas(bd.historial(nombre, tf), tf)
            except Exception as e:
                print(f"{nombre} {tf}: ERROR {e}")
                continue
            print(f"\n===== {nombre} {tf} · {len(df0)} velas =====")
            for lado, df in (("COMPRA", df0), ("VENTA", espejo(df0))):
                tend = tendencia(df, tf)
                for pat, (m, k) in patrones(df).items():
                    idx, R = operar(df, np.nonzero(m)[0], k, meta["spread"])
                    if len(idx) == 0:
                        continue
                    for grupo, sel in (("todos", np.ones(len(idx), bool)), ("a favor", tend[idx] > 0),
                                       ("en contra", tend[idx] < 0)):
                        r = R[sel]
                        if len(r) < 5:
                            continue
                        mit = len(r) // 2
                        proy = {"a_favor10": 100 * r[:, 5].mean(), "llega60": np.percentile(r[:, 3], 40),
                                "llega50": np.percentile(r[:, 3], 50), "contra80": np.percentile(r[:, 4], 80)}
                        filas.append({**proy, "mercado": nombre, "tf": tf, "lado": lado, "patron": pat, "grupo": grupo,
                                      "n": len(r), "r1": r[:, 0].mean(), "r2": r[:, 1].mean(), "r3": r[:, 2].mean(),
                                      "gana2": 100 * (r[:, 1] > 0).mean(), "m1": r[:mit, 1].mean(),
                                      "m2": r[mit:, 1].mean()})
                        if grupo != "todos" and len(r) >= 15 and r[:, 1].mean() > 0.15 and r[:mit, 1].mean() > 0 \
                                and r[mit:, 1].mean() > 0:
                            print(f"  ✅ {lado:<6} {pat:<42} {grupo:<9} {len(r):>4} casos · TP2R {r[:, 1].mean():+.2f} R "
                                  f"(gana {100 * (r[:, 1] > 0).mean():.0f}%, mitades {r[:mit, 1].mean():+.2f}/"
                                  f"{r[mit:, 1].mean():+.2f}) · TP1R {r[:, 0].mean():+.2f} · TP3R {r[:, 2].mean():+.2f}\n"
                                  f"       📍 10 velas después a favor {proy['a_favor10']:.0f}% · el 60% llegó a "
                                  f"{proy['llega60']:.1f} ATR · el 50% a {proy['llega50']:.1f} ATR · en contra (80%) "
                                  f"{proy['contra80']:.1f} ATR")
            sys.stdout.flush()
    t = pd.DataFrame(filas)
    if t.empty:
        return
    print("\n\n===== RANKING: juntando Oro, BTC y NAS100 en 5M, 15M y 1H (TP 2R) =====")
    t["peso"] = t["n"] * t["r2"]
    for col in ("a_favor10", "llega60", "llega50", "contra80"):
        t[col + "_p"] = t[col] * t["n"]
    g = t.groupby(["patron", "lado", "grupo"]).agg(casos=("n", "sum"), suma=("peso", "sum"),
                                                     mercados_tf=("n", "size"),
                                                     positivos=("r2", lambda x: int((x > 0).sum())),
                                                     ambas_mitades=("m1", "size"), af=("a_favor10_p", "sum"),
                                                     l60=("llega60_p", "sum"), l50=("llega50_p", "sum"),
                                                     c80=("contra80_p", "sum"))
    g["r2_medio"] = g["suma"] / g["casos"]
    pos_mit = t.assign(ok=(t["m1"] > 0) & (t["m2"] > 0)).groupby(["patron", "lado", "grupo"])["ok"].sum()
    g["ambas_mitades"] = pos_mit
    g = g[g["casos"] >= 60].sort_values("r2_medio", ascending=False)
    for (pat, lado, grupo), r in g.iterrows():
        print(f"  {r['r2_medio']:+.2f} R · {lado:<6} {pat:<42} {grupo:<9} · {int(r['casos']):>5} casos · positivo en "
              f"{int(r['positivos'])}/{int(r['mercados_tf'])} mercado-temporalidad · gana en las 2 mitades en "
              f"{int(r['ambas_mitades'])}\n          📍 a favor 10 velas después {r['af'] / r['casos']:.0f}% · el 60% "
              f"llegó a {r['l60'] / r['casos']:.1f} ATR · el 50% a {r['l50'] / r['casos']:.1f} ATR · en contra (80%) "
              f"{r['c80'] / r['casos']:.1f} ATR")


if __name__ == "__main__":
    main()
