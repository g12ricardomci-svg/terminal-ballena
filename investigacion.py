# -*- coding: utf-8 -*-
"""
LABORATORIO BALLENA - busca la formula con datos reales y la pone a prueba (sin enviar nada a Telegram)
======================================================================================================
Casos: cada vela madre + vela siguiente de Ballena Dorada (22 rasgos) + bloque H4 de Nueva York y dia de la semana.
Para cada caso simula la TRIPLE BARRERA (Lopez de Prado): COMPRA y VENTA al cierre de la vela medida con
  SL  = 0,5 / 0,75 / 1 / 1,5 / 2 ATR
  TP  = 0,5 / 0,75 / 1 / 1,5 / 2 / 3 ATR
  tiempo maximo = 5 / 10 / 20 velas            -> 180 planes de salida por caso (spread de Vantage descontado)
Reglas: 1 o 2 condiciones (~4.600) x 180 salidas = ~830.000 combinaciones por mercado y temporalidad.

Para NO engañarse:
  - BUSCA en el 60 % mas viejo, VALIDA en el 20 % siguiente y solo al final mira el 20 % mas nuevo (PRUEBA).
  - Repite la misma busqueda con los resultados BARAJADOS (azar puro) para saber cuantas "formulas" salen por
    suerte. Una formula vale si gana en la PRUEBA y si hay muchas mas que en el azar.
"""
import json
import sys
import time

import numpy as np
import pandas as pd

import ballena_dorada as bd

SL = [0.5, 0.75, 1.0, 1.5, 2.0]
TP = [0.5, 0.75, 1.0, 1.5, 2.0, 3.0]
TIEMPO = [5, 10, 20]
SALIDAS = [(d, s, t, v) for d in (1, -1) for s in SL for t in TP for v in TIEMPO]
MIN_BUSCA, MIN_VAL, MIN_PRUEBA = 60, 20, 20


def resultados(df, casos, a, spread):
    """R de cada caso para las 180 salidas (triple barrera; si toca SL y TP en la misma vela cuenta como perdida)."""
    h, l, c = (df[x].to_numpy(float) for x in ("high", "low", "close"))
    tmax = max(TIEMPO)
    casos = casos[casos + tmax < len(c)]
    off = np.arange(1, tmax + 1)
    H, L, C = h[casos[:, None] + off], l[casos[:, None] + off], c[casos[:, None] + off]
    e, at = c[casos][:, None], a[casos][:, None]
    R = np.full((len(casos), len(SALIDAS)), np.nan)
    k = 0
    for d in (1, -1):
        fav = np.maximum.accumulate((H - e) if d > 0 else (e - L), axis=1) / at
        adv = np.maximum.accumulate((e - L) if d > 0 else (H - e), axis=1) / at
        fin = (C - e) * d / at
        for s in SL:
            t_sl = np.where((adv >= s).any(1), (adv >= s).argmax(1), 10 ** 6)
            coste = spread / (s * a[casos])
            for t in TP:
                t_tp = np.where((fav >= t).any(1), (fav >= t).argmax(1), 10 ** 6)
                for v in TIEMPO:
                    r = np.where((t_sl < v) & (t_sl <= t_tp), -1.0,
                                 np.where(t_tp < v, t / s, fin[:, v - 1] / s))
                    R[:, k] = r - coste
                    k += 1
    return casos, R


def condiciones(P, casos, idx):
    """Matriz caso x condicion (1 y 2 condiciones) y su nombre."""
    t = idx[casos].tz_convert(bd.NY)
    extra = np.column_stack([t.hour // 4, np.clip(t.weekday, 0, 4)])     # bloque H4 NY (6) · dia (5)
    X = np.column_stack([P["X"][casos], extra])
    card = list(bd.CARD) + [6, 5]
    off = np.cumsum([0] + card[:-1])
    codes = X + off
    n_cod = int(sum(card))
    M1 = np.zeros((len(casos), n_cod), dtype=np.float32)
    M1[np.arange(len(casos))[:, None], codes] = 1
    rasgo = np.repeat(np.arange(len(card)), card)
    i1, i2 = np.triu_indices(n_cod, 1)
    distinto = rasgo[i1] != rasgo[i2]
    i1, i2 = i1[distinto], i2[distinto]
    M2 = M1[:, i1] * M1[:, i2]
    nombres_c = []
    for cd in range(n_cod):
        f = int(rasgo[cd])
        v = int(cd - off[f])
        if f == len(bd.CARD):
            nombres_c.append(f"bloque H4 NY {v * 4:02d}-{v * 4 + 4:02d}h")
        elif f == len(bd.CARD) + 1:
            nombres_c.append(["lunes", "martes", "miércoles", "jueves", "viernes"][v])
        else:
            nombres_c.append(bd.nombre_rasgo(f, v))
    nombres = nombres_c + [f"{nombres_c[a]} + {nombres_c[b]}" for a, b in zip(i1, i2)]
    return np.hstack([M1, M2]), nombres


def ev(M, R):
    n = M.sum(0)
    s = M.T @ R
    return s / np.maximum(n, 1)[:, None], n


def buscar(M, R, partes):
    """Formulas que ganan en la busqueda (>= +0,15 R) y en la validacion (> +0,05 R). Devuelve su resultado."""
    tr, va, te = partes
    e_tr, n_tr = ev(M[tr], R[tr])
    e_va, n_va = ev(M[va], R[va])
    e_te, n_te = ev(M[te], R[te])
    ok = ((n_tr >= MIN_BUSCA) & (n_va >= MIN_VAL) & (n_te >= MIN_PRUEBA))[:, None] & (e_tr >= 0.15) & (e_va > 0.05)
    filas, cols = np.nonzero(ok)
    orden = np.argsort(-np.minimum(e_tr[filas, cols], e_va[filas, cols]))
    return [(int(filas[j]), int(cols[j]), float(e_tr[filas[j], cols[j]]), float(e_va[filas[j], cols[j]]),
             float(e_te[filas[j], cols[j]]), int(n_tr[filas[j]]), int(n_va[filas[j]]), int(n_te[filas[j]]))
            for j in orden]


def estudiar(nombre, tf, df, rng):
    P = bd.preparar(df, tf)
    casos = np.nonzero(P["madre"])[0]
    casos = casos[casos > 120]
    casos, R = resultados(df, casos, P["atr"], bd.MERCADOS[nombre]["spread"])
    ok = np.isfinite(R).all(1)
    casos, R = casos[ok], R[ok]
    M, nombres = condiciones(P, casos, P["idx"])
    n = len(casos)
    partes = (np.arange(n) < int(n * 0.6), (np.arange(n) >= int(n * 0.6)) & (np.arange(n) < int(n * 0.8)),
              np.arange(n) >= int(n * 0.8))
    reales = buscar(M, R, partes)
    azar = [len(buscar(M, R[rng.permutation(n)], partes)) for _ in range(3)]
    base = {f"{'COMPRA' if d > 0 else 'VENTA'} SL{s} TP{t} {v}v": float(R[partes[2], k].mean())
            for k, (d, s, t, v) in enumerate(SALIDAS) if (s, t, v) == (1.0, 1.0, 10)}
    top = []
    vistos = set()
    for f, k, a_tr, a_va, a_te, n1, n2, n3 in reales:
        if (f, SALIDAS[k][0]) in vistos:
            continue
        vistos.add((f, SALIDAS[k][0]))
        d, s, t, v = SALIDAS[k]
        top.append({"regla": nombres[f], "lado": "COMPRA" if d > 0 else "VENTA", "sl_atr": s, "tp_atr": t,
                    "velas": v, "busqueda_r": round(a_tr, 3), "validacion_r": round(a_va, 3),
                    "prueba_r": round(a_te, 3), "casos": [n1, n2, n3]})
        if len(top) == 15:
            break
    prueba_media = float(np.mean([x[4] for x in reales])) if reales else 0.0
    ganan_prueba = float(np.mean([x[4] > 0 for x in reales])) if reales else 0.0
    return {"mercado": nombre, "tf": tf, "casos": n, "desde": str(df.index[0]), "hasta": str(df.index[-1]),
            "combinaciones": int(M.shape[1] * R.shape[1]), "formulas_reales": len(reales), "formulas_azar": azar,
            "prueba_media_r": round(prueba_media, 3), "ganan_en_prueba": round(100 * ganan_prueba, 1),
            "base_sin_filtro_prueba": base, "mejores": top}


def main():
    rng = np.random.default_rng(7)
    informe = []
    for nombre in bd.MERCADOS:
        for tf in bd.CONFIG["temporalidades"]:
            t0 = time.time()
            try:
                df = bd.solo_cerradas(bd.historial(nombre, tf), tf)
                r = estudiar(nombre, tf, df, rng)
            except Exception as e:
                r = {"mercado": nombre, "tf": tf, "error": f"{type(e).__name__}: {e}"}
            informe.append(r)
            print(f"\n===== {nombre} {tf} ({time.time() - t0:.0f}s) =====")
            if "error" in r:
                print("ERROR", r["error"])
                continue
            print(f"casos {r['casos']} · {r['desde'][:10]} -> {r['hasta'][:10]} · {r['combinaciones']:,} combinaciones")
            print(f"formulas que pasan busqueda+validacion: REALES {r['formulas_reales']} · AZAR {r['formulas_azar']}")
            print(f"en la PRUEBA (datos nunca vistos): media {r['prueba_media_r']:+.3f} R · ganan {r['ganan_en_prueba']}%")
            print("base sin filtro (SL 1 TP 1 10 velas) en la prueba:", r["base_sin_filtro_prueba"])
            for m in r["mejores"]:
                print(f"  {m['lado']:<6} SL {m['sl_atr']} TP {m['tp_atr']} {m['velas']}v · busq {m['busqueda_r']:+.2f} "
                      f"val {m['validacion_r']:+.2f} PRUEBA {m['prueba_r']:+.2f} R · casos {m['casos']} · {m['regla']}")
            sys.stdout.flush()
    with open("informe_laboratorio.json", "w", encoding="utf-8") as f:
        json.dump(informe, f, ensure_ascii=False, indent=1)


if __name__ == "__main__":
    main()
