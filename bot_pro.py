                        f"💲 *Precio:* `${precio:,.4f}` | *POC:* `${resultado['poc']:,.4f}` (VAL `${resultado['val']:,.4f}` / VAH `${resultado['vah']:,.4f}`)\n"
                        f"🛡️ *SL:* `${resultado['sl']:,.4f}` | *TP:* `${resultado['tp']:,.4f}`\n"
                        f"💵 *Riesgo 0.01 lot:* `~${r_usd:.2f} USD` (`{r_pct:.1f}%` de ${SALDO_CUENTA_USD:.0f}) ✅\n"
                        f"📋 *Ejecución:*\n{resultado['plan']}"
                    )

                    # Cooldown de 20 minutos por activo y dirección (evita spam entre 15m y 5m)
                    clave_alerta = f"{canon}_{resultado['id_senal']}"
                    ahora = time.time()

                    if ahora - ALERTAS_ENVIADAS_GLOBAL.get(clave_alerta, 0) > 1200:
                        if enviar_telegram(msg):
                            ALERTAS_ENVIADAS_GLOBAL[clave_alerta] = ahora
                            alertas_nuevas += 1
                    break  # Ya procesó la mejor oportunidad de este activo

            time.sleep(0.12)
        except Exception:
            continue

    if not es_hilo_fondo:
        AUTOPILOTO_STATUS = f"✅ Barrido institucional listo. Alertas enviadas: {alertas_nuevas}"
    else:
        AUTOPILOTO_STATUS = "⚡ Motor AMT + Order Block activo (1h contexto -> 15m/5m cada 30s)..."

@st.cache_resource
def iniciar_hilo_autopiloto():
    def loop():
        time.sleep(5)
        while True:
            try:
                ejecutar_logica_autopiloto(es_hilo_fondo=True)
            except Exception:
                pass
            time.sleep(30)
    thread = threading.Thread(target=loop, daemon=True)
    thread.start()
    return thread

iniciar_hilo_autopiloto()

# ==========================================
# 📊 INTERFAZ GRÁFICA PRINCIPAL
# ==========================================
st.markdown("<h1 style='text-align: center; color: #00FFAA;'>🏦 INSTITUTIONAL QUANT TERMINAL — AMT + SMART MONEY + CONFLUENCIAS</h1>", unsafe_allow_html=True)
st.markdown("<p style='text-align: center; color: gray;'>AMT (POC/VAH/VAL) + Smart Money (OB/FVG) + VWAP/EMA + CVD proxy + ADX adaptativo + filtro de agotamiento</p>", unsafe_allow_html=True)
st.markdown("---")

st.sidebar.header("🌐 Configuración del Gráfico")
symbol = st.sidebar.selectbox("Ver Gráfico de:", ALL_TRACKED_SYMBOLS)
timeframe = st.sidebar.selectbox("Temporalidad", ["15m", "5m", "1h"])

st.sidebar.markdown("---")
st.sidebar.subheader("📡 Estado del Motor")
st.sidebar.info(AUTOPILOTO_STATUS)
if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
    st.sidebar.warning("Telegram sin configurar. Define TELEGRAM_BOT_TOKEN y TELEGRAM_CHAT_ID como variables de entorno.")
if ULTIMO_ERROR_TG:
    st.sidebar.caption(f"⚠️ Telegram: {ULTIMO_ERROR_TG}")

if st.sidebar.button("⚡ Forzar Escaneo Ahora"):
    with st.spinner("Analizando contexto 1h, perfil de volumen y bloques..."):
        ejecutar_logica_autopiloto(es_hilo_fondo=False)
        st.sidebar.success("¡Escaneo completado!")

@st.fragment(run_every="10s")
def render_trading_panel(symbol, timeframe):
    df = obtener_datos(symbol, timeframe, limit=120, ttl_segundos=30)
    if df.empty:
        st.error(f"No se pudieron cargar los datos para {symbol}.")
        return

    if not df.attrs.get("es_real", False):
        st.warning("⚠️ Datos de respaldo activos por límite temporal de proveedor externo. No se enviarán alertas de Telegram con estos datos.")

    perfil_panel = calcular_perfil_volumen_amt(df, lookback=36)

    col_grafico, col_paneles = st.columns([2.5, 1])
    with col_grafico:
        fig = make_subplots(rows=2, cols=1, shared_xaxes=True, vertical_spacing=0.03, row_width=[0.2, 0.8])
        fig.add_trace(go.Candlestick(
            x=df["timestamp"], open=df["open"], high=df["high"], low=df["low"], close=df["close"],
            increasing_line_color="#00FFAA", decreasing_line_color="#FF3333", name="Precio"
        ), row=1, col=1)
        fig.add_trace(go.Scatter(x=df["timestamp"], y=df["vwap"], line=dict(color="#FF00FF", width=2), name="VWAP"), row=1, col=1)
        fig.add_trace(go.Scatter(x=df["timestamp"], y=df["ema_fast"], line=dict(color="#FFFF00", width=1.2), name="EMA 9"), row=1, col=1)
        fig.add_trace(go.Scatter(x=df["timestamp"], y=df["ema_slow"], line=dict(color="#00BFFF", width=1.2), name="EMA 21"), row=1, col=1)

        # Niveles AMT (POC, VAH, VAL) dibujados directamente en el gráfico
        fig.add_hline(y=perfil_panel["poc"], line_dash="solid", line_color="#FFA500", annotation_text="POC", row=1, col=1)
        fig.add_hline(y=perfil_panel["vah"], line_dash="dash", line_color="#00FFAA", annotation_text="VAH", row=1, col=1)
        fig.add_hline(y=perfil_panel["val"], line_dash="dash", line_color="#FF5555", annotation_text="VAL", row=1, col=1)

        colors = ["#00FFAA" if r["close"] >= r["open"] else "#FF3333" for _, r in df.iterrows()]
        fig.add_trace(go.Bar(x=df["timestamp"], y=df["volume"], marker_color=colors, name="Volumen"), row=2, col=1)
        fig.update_layout(template="plotly_dark", xaxis_rangeslider_visible=False, height=600, margin=dict(l=10, r=10, t=10, b=10), showlegend=True)
        st.plotly_chart(fig, use_container_width=True)

    with col_paneles:
        st.markdown("### 📊 Lectura AMT & Fractalidad")
        ultima = df.iloc[-1]
        anterior = df.iloc[-2]
        precio_actual = float(ultima["close"])
        st.metric(label=f"{canonizar_simbolo(symbol)} [{timeframe}]", value=f"${precio_actual:,.4f}", delta=f"{precio_actual - float(anterior['close']):,.4f}")

        df_1h_panel = obtener_datos(symbol, "1h", limit=90, ttl_segundos=90)
        ctx_1h_panel = analizar_contexto_macro_1h(df_1h_panel, symbol)
        st.info(f"🧭 Contexto 1h: **{ctx_1h_panel['sesgo']}** | Estado AMT: **{perfil_panel['estado']}**")
        st.caption(f"POC: `${perfil_panel['poc']:,.4f}` | VAL: `${perfil_panel['val']:,.4f}` | VAH: `${perfil_panel['vah']:,.4f}`")
        adx_panel = float(ultima.get("adx", 0.0))
        adx_min_panel = obtener_umbral_adx(symbol)
        st.caption(f"ADX: {adx_panel:.1f} | Umbral adaptativo: {adx_min_panel:.0f} | Exhaustación: {'SÍ — BLOQUEAR' if detectar_exhaustacion(df) else 'No detectada'}")
        cvd_panel = float(ultima.get("cvd", 0.0))
        cvd_prev_panel = float(anterior.get("cvd", 0.0))
        st.caption(f"CVD proxy: {'🟢 comprador' if cvd_panel > cvd_prev_panel else '🔴 vendedor' if cvd_panel < cvd_prev_panel else '⚪ neutro'} (no es delta real de agresores)")

        ob_panel = detectar_order_block_institucional(df)
        if ob_panel:
            st.caption(f"Bloque detectado: `{ob_panel['tipo']}` (`${ob_panel['low']:,.4f}` - `${ob_panel['high']:,.4f}`)")

        fvg_panel = detectar_fvg_3_velas(df)
        if fvg_panel:
            st.caption(f"Imbalance FVG: `{fvg_panel['tipo']}` (`${fvg_panel['zona_low']:,.4f}` - `${fvg_panel['zona_high']:,.4f}`)")

        st.markdown("---")
        st.markdown("### 🎯 Motor Integrado: AMT + Smart Money + Confirmaciones")
        tf_eval = "15m" if timeframe == "1h" else timeframe
        df_eval = obtener_datos(symbol, tf_eval, limit=90, ttl_segundos=60) if tf_eval != timeframe else df
