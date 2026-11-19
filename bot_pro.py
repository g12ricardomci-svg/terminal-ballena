EXCHANGE_OKX = ccxt.okx({"enableRateLimit": True})
EXCHANGE_BINANCE = ccxt.binance({"enableRateLimit": True})

def _normalizar_timestamp(serie):
    ts = pd.to_datetime(serie)
    if getattr(ts.dt, "tz", None) is not None:
        ts = ts.dt.tz_convert(None)
    return ts

def obtener_datos(symbol, timeframe, limit=120, ttl_segundos=25):
    cache_key = f"{symbol}_{timeframe}_{limit}"
    ahora = time.time()

    with CACHE_LOCK:
        cached = CACHE_DATOS_GLOBAL.get(cache_key)
        if cached and (ahora - cached["ts"] < ttl_segundos) and cached.get("es_real", False):
            df_cached = cached["df"].copy()
            df_cached.attrs["es_real"] = True
            return df_cached

    df = pd.DataFrame()
    es_real = False

    if symbol in CRYPTO_MARKETS:
        for ex in (EXCHANGE_OKX, EXCHANGE_BINANCE):
            try:
                ohlcv = ex.fetch_ohlcv(symbol, timeframe, limit=limit)
                if ohlcv:
                    temp_df = pd.DataFrame(ohlcv, columns=["timestamp", "open", "high", "low", "close", "volume"])
                    temp_df["timestamp"] = _normalizar_timestamp(pd.to_datetime(temp_df["timestamp"], unit="ms"))
                    temp_df = temp_df.dropna(subset=["open", "high", "low", "close"]).tail(limit).copy()
                    if not temp_df.empty and float(temp_df["close"].iloc[-1]) > 0:
                        df = temp_df
                        es_real = True
                        break
            except Exception:
                continue
    else:
        interval_map = {"5m": "5m", "15m": "15m", "1h": "60m"}
        yf_interval = interval_map.get(timeframe, "60m")
        periodos_a_probar = ["5d", "1mo"] if timeframe == "5m" else ["1mo", "3mo"]

        simbolos_yf = [symbol]
        if symbol == "XAUUSD":
            simbolos_yf = ["XAUUSD=X", "GC=F"]
        elif symbol == "XAUUSD=X":
            simbolos_yf = ["XAUUSD=X", "GC=F"]

        for sym_try in simbolos_yf:
            for periodo in periodos_a_probar:
                try:
                    hist = yf.Ticker(sym_try).history(period=periodo, interval=yf_interval)
                    if not hist.empty and "Close" in hist.columns:
                        temp_df = hist.reset_index()
                        time_col = "Datetime" if "Datetime" in temp_df.columns else ("Date" if "Date" in temp_df.columns else temp_df.columns[0])
                        temp_df.rename(columns={
                            time_col: "timestamp",
                            "Open": "open",
                            "High": "high",
                            "Low": "low",
                            "Close": "close",
                            "Volume": "volume"
                        }, inplace=True)
                        temp_df["timestamp"] = _normalizar_timestamp(temp_df["timestamp"])
                        temp_df["volume"] = temp_df["volume"].fillna(0)
                        temp_df = temp_df.dropna(subset=["open", "high", "low", "close"]).tail(limit).copy()
                        if not temp_df.empty and float(temp_df["close"].iloc[-1]) > 0:
                            df = temp_df
                            es_real = True
                            break
                except Exception:
                    continue
            if es_real:
                break

    if df.empty or not all(col in df.columns for col in ["open", "high", "low", "close", "volume", "timestamp"]):
        freq_map = {"5m": "5min", "15m": "15min", "1h": "1h"}
        fechas = pd.date_range(end=datetime.now(), periods=limit, freq=freq_map.get(timeframe, "15min"))
        base_val = 2650.0 if "XAU" in symbol else (1.085 if "EUR" in symbol else 150.0)
        rng = np.random.default_rng(abs(hash(symbol + timeframe)) % (2**32))
        returns = rng.normal(0.0001, 0.002, limit)
        prices = base_val * np.cumprod(1 + returns)

        df = pd.DataFrame({
            "timestamp": fechas,
            "open": prices * (1 + rng.normal(0, 0.0005, limit)),
            "high": prices * (1 + abs(rng.normal(0, 0.001, limit))),
            "low": prices * (1 - abs(rng.normal(0, 0.001, limit))),
            "close": prices,
            "volume": rng.integers(1000, 10000, limit)
        })
        es_real = False

    if not df.empty and len(df) > 5:
        df["vwap"] = calcular_vwap(df)
        window_val = min(20, len(df))
        df["soporte"] = df["low"].rolling(window=window_val).min()
        df["resistencia"] = df["high"].rolling(window=window_val).max()
        df["vol_ma"] = df["volume"].rolling(window=window_val).mean()
        df["ema_fast"] = df["close"].ewm(span=9, adjust=False).mean()
        df["ema_slow"] = df["close"].ewm(span=21, adjust=False).mean()
        df["adx"] = calcular_adx(df)
        df["cvd"] = calcular_cvd_proxy(df)

    df.attrs["es_real"] = es_real

    if es_real:
        with CACHE_LOCK:
            CACHE_DATOS_GLOBAL[cache_key] = {"ts": ahora, "df": df.copy(), "es_real": True}

    return df
