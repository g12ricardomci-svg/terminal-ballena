import os, requests, streamlit as st

def _secret(name):
    try:
        v = st.secrets[name]
        if isinstance(v, (list, tuple)):
            return ",".join(str(x) for x in v)
        return str(v).strip()
    except Exception:
        return os.environ.get(name, "").strip()

def enviar_telegram(texto):
    token = _secret("TELEGRAM_TOKEN")
    ids = [i.strip() for i in _secret("TELEGRAM_CHAT_IDS").replace(";", ",").split(",") if i.strip()]
    if not token or not ids:
        return False, "Faltan TELEGRAM_TOKEN / TELEGRAM_CHAT_IDS en Secrets"
    errores = []
    for chat_id in ids:
        try:
            r = requests.post(
                f"https://api.telegram.org/bot{token}/sendMessage",
                json={"chat_id": chat_id, "text": texto},
                timeout=15,
            )
            if not r.ok:
                errores.append(f"{chat_id}: {r.status_code} {r.text[:120]}")
        except Exception as e:
            errores.append(f"{chat_id}: {e}")
    return (not errores), "; ".join(errores) or "Enviado"
