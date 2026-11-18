# 🛠️ FUNCIÓN OBTENER_DATOS BLINDADA CONTRA BLOQUEOS DE YFINANCE
def obtener_datos(symbol, timeframe, limit=100):
    df = pd.DataFrame()
    if "/" in symbol and "USDT" in symbol:
        exchange = ccxt.okx()
        try:
            ohlcv = exchange.fetch_ohlcv(symbol, timeframe, limit=limit)
            df = pd.DataFrame(ohlcv, columns=['timestamp', 'open', 'high', 'low', 'close', 'volume'])
            df['timestamp'] = pd.to_datetime(df['timestamp'], unit='ms')
        except: return pd.DataFrame()
    else:
        interval_map = {"1m": "1m", "5m": "5m", "15m": "15m", "1h": "60m"} 
        yf_interval = interval_map.get(timeframe, "5m")
        
        # Intentar primero con periodo de 5 días, si falla recurrir a 1 mes diario
        for periodo in ["5d", "1mo"]:
            try:
                ticker = yf.Ticker(symbol)
                hist = ticker.history(period=period, interval=yf_interval if periodo=="5d" else "1d")
                if not hist.empty:
                    df = hist.reset_index()
                    time_col = 'Datetime' if 'Datetime' in df.columns else ('Date' if 'Date' in df.columns else df.columns[0])
                    df.rename(columns={time_col: 'timestamp'}, inplace=True)
                    df['timestamp'] = pd.to_datetime(df['timestamp']).dt.tz_localize(None)
                    df['volume'] = df['volume'].fillna(0)
                    df = df.tail(limit)
                    break
            except:
                continue

    if not df.empty:
        df['vwap'] = calcular_vwap(df)
        df['soporte'] = df['low'].rolling(window=min(20, len(df))).min()
        df['resistencia'] = df['high'].rolling(window=min(20, len(df))).max()
        df['vol_ma'] = df['volume'].rolling(window=min(20, len(df))).mean()
        df['ema_fast'] = df['close'].ewm(span=9, adjust=False).mean()
        df['ema_slow'] = df['close'].ewm(span=21, adjust=False).mean()
        df['adx'] = calcular_adx(df)
    return df
