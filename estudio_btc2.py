# -*- coding: utf-8 -*-
"""
BTC: LAS ESTRATEGIAS DE LOS LIBROS Y ESTUDIOS, MEDIDAS CON DATOS REALES (0,01 lotes, spread $20, no envia nada)
  1) Donchian / Tortugas: ruptura del maximo/minimo de 20 velas, salida por el canal de 10 (1H y 4H), con y sin EMA 200
  2) Larry Williams: ruptura de volatilidad (apertura del dia + k x rango de ayer), salida en la apertura siguiente
  3) Larry Connors RSI(2): sobre la media de 200, debajo de la de 5 y RSI(2) < 10; salida sobre la media de 5 (4H y 1D)
  4) Quantpedia: comprar 21:00 UTC y vender 23:00 UTC
  5) Efecto lunes (Caporale y Plastun): comprar en la apertura del lunes y vender al cierre
  6) Gaps del CME: al reabrir el domingo, ir hacia el cierre del viernes del futuro CME (BTC=F)
  7) Momentum de 30 dias: estar comprado si subio en 30 dias, vendido si bajo (se revisa cada dia)
"""
import numpy as np
import pandas as pd

import ballena_dorada as bd

SP, V = 20.0, 0.01       # spread de Vantage y $ por cada $1 de precio con 0,01 lotes


def informe(nombre, pnl_precio, extra=""):
    usd = np.asarray(pnl_precio, float) * V - SP * V
    if len(usd) < 5:
        print(f"  {nombre:<55} pocos casos ({len(usd)})")
        return
    m = len(usd) // 2
    eq = np.cumsum(usd)
    caida = float((np.maximum.accumulate(np.r_[0, eq])[1:] - eq).max())
    marca = "✅" if usd[:m].sum() > 0 and usd[m:].sum() > 0 else "  "
    print(f"  {marca} {nombre:<53} {len(usd):>4} ops · {usd.sum():>+8.2f}$ ({usd.mean():+.2f}$/op, gana "
          f"{100 * (usd > 0).mean():.0f}%) · mitades {usd[:m].sum():+.2f}/{usd[m:].sum():+.2f}$ · caída {caida:.2f}${extra}")


def donchian(df, n_in=20, n_out=10, filtro=False):
    h, l, c = (df[x].to_numpy(float) for x in ("high", "low", "close"))
    hi = pd.Series(h).rolling(n_in).max().shift(1).to_numpy()
    lo = pd.Series(l).rolling(n_in).min().shift(1).to_numpy()
    hi_o = pd.Series(h).rolling(n_out).max().shift(1).to_numpy()
    lo_o = pd.Series(l).rolling(n_out).min().shift(1).to_numpy()
    e200 = bd.ema(df["close"], 200).to_numpy()
    pos, entrada, pnl = 0, 0.0, []
    for i in range(201, len(c)):
        if pos == 1 and c[i] < lo_o[i]:
            pnl.append(c[i] - entrada)
            pos = 0
        elif pos == -1 and c[i] > hi_o[i]:
            pnl.append(entrada - c[i])
            pos = 0
        if pos == 0:
            if c[i] > hi[i] and (not filtro or c[i] > e200[i]):
                pos, entrada = 1, c[i]
            elif c[i] < lo[i] and (not filtro or c[i] < e200[i]):
                pos, entrada = -1, c[i]
    return pnl


def williams(h1, k=0.5, filtro=False):
    d = h1.resample("1D").agg({"open": "first", "high": "max", "low": "min", "close": "last"}).dropna()
    e50 = bd.ema(d["close"], 50)
    pnl = []
    for j in range(51, len(d) - 1):
        dia = d.index[j]
        rango = d["high"].iloc[j - 1] - d["low"].iloc[j - 1]
        arriba, abajo = d["open"].iloc[j] + k * rango, d["open"].iloc[j] - k * rango
        velas = h1[(h1.index >= dia) & (h1.index < dia + pd.Timedelta(days=1))]
        sale = d["open"].iloc[j + 1]
        tend = 1 if d["close"].iloc[j - 1] > e50.iloc[j - 1] else -1
        for _, v in velas.iterrows():
            if v["high"] >= arriba and (not filtro or tend > 0):
                pnl.append(sale - arriba)
                break
            if v["low"] <= abajo and (not filtro or tend < 0):
                pnl.append(abajo - sale)
                break
    return pnl


def connors(df):
    c = df["close"]
    s200, s5 = c.rolling(200).mean(), c.rolling(5).mean()
    r2 = bd.rsi(c, 2)
    pnl, pos, entrada = [], False, 0.0
    for i in range(200, len(c)):
        if pos and c.iloc[i] > s5.iloc[i]:
            pnl.append(c.iloc[i] - entrada)
            pos = False
        elif not pos and c.iloc[i] > s200.iloc[i] and c.iloc[i] < s5.iloc[i] and r2.iloc[i] < 10:
            pos, entrada = True, c.iloc[i]
    return pnl


def ventana_horaria(h1, h_ini=21, h_fin=23):
    pnl = []
    for dia, g in h1.groupby(h1.index.floor("1D")):
        a = g[g.index.hour == h_ini]
        b = g[g.index.hour == h_fin]
        if len(a) and len(b):
            pnl.append(b["open"].iloc[0] - a["open"].iloc[0])
    return pnl


def lunes(h1):
    d = h1.resample("1D").agg({"open": "first", "close": "last"}).dropna()
    d = d[d.index.weekday == 0]
    return list(d["close"] - d["open"])


def momentum(h1, dias=30):
    d = h1["close"].resample("1D").last().dropna()
    s = np.sign(d - d.shift(dias)).shift(1)
    r = (d.diff() * s).dropna()
    cambios = int((s.diff().fillna(0) != 0).sum())
    return r, cambios


def gaps_cme(h1):
    if bd.yf is None:
        return None
    try:
        f = bd._limpiar_yf(bd.yf.download("BTC=F", interval="1h", period="729d", progress=False, auto_adjust=False,
                                          threads=False))
    except Exception:
        return None
    if f is None or f.empty:
        return None
    pnl, llenos, total = [], 0, 0
    huecos = f.index.to_series().diff() > pd.Timedelta(hours=24)       # reapertura del domingo
    for t in f.index[huecos.to_numpy()]:
        k = f.index.get_loc(t)
        cierre_vie = f["close"].iloc[k - 1]
        g = h1[h1.index >= t]
        if len(g) < 48:
            continue
        e = g["open"].iloc[0]
        gap = cierre_vie - e
        if abs(gap) < 50:
            continue
        total += 1
        d = 1 if gap > 0 else -1
        sl = e - d * abs(gap)                                            # SL: el tamaño del gap en contra
        res = None
        for _, v in g.iloc[:48].iterrows():
            if (v["low"] <= sl) if d > 0 else (v["high"] >= sl):
                res = -abs(gap)
                break
            if (v["high"] >= cierre_vie) if d > 0 else (v["low"] <= cierre_vie):
                res = abs(gap)
                llenos += 1
                break
        if res is None:
            res = (g["close"].iloc[47] - e) * d
        pnl.append(res)
    return pnl, llenos, total


def main():
    h1 = bd.solo_cerradas(bd.historial("BTC/USD", "1h"), "1h")
    h4 = h1.resample("4h", label="left", closed="left").agg(
        {"open": "first", "high": "max", "low": "min", "close": "last"}).dropna()
    d1 = h1.resample("1D").agg({"open": "first", "high": "max", "low": "min", "close": "last"}).dropna()
    print(f"BTC 1H: {len(h1)} velas · {h1.index[0]:%Y-%m-%d} -> {h1.index[-1]:%Y-%m-%d} · "
          f"BTC {d1['close'].iloc[0]:,.0f} -> {d1['close'].iloc[-1]:,.0f}")
    print("\n1) DONCHIAN / TORTUGAS")
    for nom, df in (("1H", h1), ("4H", h4)):
        informe(f"Donchian 20/10 {nom}", donchian(df))
        informe(f"Donchian 20/10 {nom} + EMA 200", donchian(df, filtro=True))
        informe(f"Donchian 55/20 {nom} + EMA 200 (Tortugas sistema 2)", donchian(df, 55, 20, True))
    print("\n2) LARRY WILLIAMS (ruptura de volatilidad diaria)")
    for k in (0.5, 0.7):
        informe(f"Williams k={k}", williams(h1, k))
        informe(f"Williams k={k} + EMA 50 diaria", williams(h1, k, True))
    print("\n3) LARRY CONNORS RSI(2)")
    informe("Connors RSI(2) 4H", connors(h4))
    informe("Connors RSI(2) 1D", connors(d1))
    print("\n4) QUANTPEDIA: comprar 21 UTC, vender 23 UTC")
    informe("21 -> 23 UTC (todos los dias)", ventana_horaria(h1))
    print("\n5) EFECTO LUNES")
    informe("Comprar apertura del lunes, vender al cierre", lunes(h1))
    print("\n6) GAPS DEL CME")
    g = gaps_cme(h1)
    if g:
        pnl, llenos, total = g
        informe("Ir a llenar el gap del CME (TP = cierre del viernes, 48 h)", pnl,
                f" · se llenaron {llenos}/{total} ({100 * llenos / max(total, 1):.0f}%)")
    else:
        print("  sin datos del futuro CME")
    print("\n7) MOMENTUM DE 30 DIAS (comprado si subio, vendido si bajo)")
    r, cambios = momentum(h1)
    usd = r * V
    print(f"  Momentum 30d diario: {usd.sum():+.2f}$ en {len(r)} dias (sin contar spread) · {cambios} cambios de "
          f"posicion ({cambios * SP * V:.2f}$ de spread) · neto {usd.sum() - cambios * SP * V:+.2f}$")
    print(f"\nReferencia: comprar y mantener 0,01 BTC: {(d1['close'].iloc[-1] - d1['close'].iloc[0]) * V:+.2f}$")


if __name__ == "__main__":
    main()
