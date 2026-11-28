# -*- coding: utf-8 -*-
import os
import sys

import numpy as np
import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import biblioteca_patrones as bp  # noqa: E402


def test_biblioteca_detecta_y_proyecta():
    rng = np.random.default_rng(2)
    n = 3000
    c = 2600 + np.cumsum(rng.normal(0, 1.2, n))
    o = np.r_[c[0], c[:-1]] + rng.normal(0, 0.3, n)
    df = pd.DataFrame(dict(open=o, high=np.maximum(o, c) + rng.exponential(0.6, n),
                           low=np.minimum(o, c) - rng.exponential(0.6, n), close=c, volume=1.0),
                      index=pd.date_range("2026-10-01", periods=n, freq="5min", tz="UTC"))
    P = bp.patrones(df)
    assert len(P) == 12 and all(m.dtype == bool for m, _ in P.values())
    idx, R = bp.operar(df, np.nonzero(P["outside bar alcista"][0])[0], 2, 0.3)
    assert R.shape[1] == 6 and (R[:, 3] >= 0).all() and set(np.unique(R[:, 5])) <= {0.0, 1.0}
    assert len(bp.patrones(bp.espejo(df))) == 12
