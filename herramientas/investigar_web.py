"""Segunda pasada: textos legales completos de SpikeCharting y busqueda dirigida en TODO su JavaScript publico."""
import re
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup

UA = {"User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124 Safari/537.36",
      "Accept-Language": "es-CO,es;q=0.9"}


def get(url):
    try:
        r = requests.get(url, headers=UA, timeout=25)
        return r.status_code, r.text
    except Exception as e:
        return None, f"ERROR {e}"


def texto(html):
    s = BeautifulSoup(html, "html.parser")
    for t in s(["script", "style", "noscript"]):
        t.decompose()
    return re.sub(r"\s+", " ", s.get_text(" ")).strip()


# 1) Textos legales completos, solo las secciones relevantes
for url, secciones in [("https://spikecharting.com/terminos",
                        ["1.7 ", "1.8 ", "1.9 ", "1.10 ", "1.11 ", "1.12 ", "1.13 ", "1.22 ", "1.23 ", "1.24 ",
                         "1.25 ", "1.26 ", "1.27 ", "1.36 "]),
                       ("https://spikecharting.com/privacidad", ["2.8 ", "2.9 ", "2.18 "])]:
    code, html = get(url)
    t = texto(html)
    print(f"\n{'=' * 100}\n### {url} -> {code} ({len(t)} chars de texto)")
    for sec in secciones:
        idxs = [m.start() for m in re.finditer(re.escape(sec), t)]
        if len(idxs) >= 2:                       # la 1a aparicion es el indice; la 2a es el cuerpo
            i = idxs[1]
            sig = re.search(r"\s\d\.\d{1,2} [A-ZÁÉÍÓÚ]", t[i + 6:])
            fin = i + 6 + sig.start() if sig else i + 2500
            print(f"\n[{sec.strip()}] {t[i:min(fin, i + 2500)]}")

# 2) Todo el JavaScript publico de la app: busqueda dirigida
code, html = get("https://app.spikecharting.com/")
soup = BeautifulSoup(html, "html.parser")
scripts = sorted({urljoin("https://app.spikecharting.com/", s["src"]) for s in soup.find_all("script", src=True)})
print(f"\n{'#' * 100}\n### {len(scripts)} scripts")
PATRONES = {
    "proveedor_ia": r"openai|chatgpt|gpt-[0-9o]|anthropic|claude|gemini|generativelanguage|deepseek|mistral|groq|llama|grok|x\.ai|openrouter|together\.ai|perplexity|cohere",
    "backend": r"[a-z0-9]{10,}\.supabase\.co|functions/v1/[a-z0-9_-]+|/rest/v1/[a-z_]+|workers\.dev|wss://[a-z0-9./_-]+",
    "pagos": r"whop\.com[a-z0-9/_?=-]*|stripe|paypal|mercadopago|lemonsqueezy|checkout",
    "precios": r"(?:US)?\$\s?\d{1,3}(?:[.,]\d{2})?|\d{1,3}(?:[.,]\d{2})?\s?(?:USD|usd)|/\s?mes|por mes|mensual|anual",
    "ticks": r"\d{1,3}[.,]?\d{3}\s?ticks|ticks?\s?(?:al|por)\s?mes|costo[s]? (?:en|de) ticks|cuesta[n]? \d+",
    "datos": r"derivws|binary\.com|ws\.derivws|weltrade|fapi\.binance|api\.binance|stream\.binance|twelvedata|polygon\.io|finnhub|oanda",
    "aura_prompt": r"eres aura|you are aura|system ?prompt|no inventes|role:\s?['\"]system",
}
hallado = {k: {} for k in PATRONES}
for s in scripts:
    c, js = get(s)
    if not c or c >= 400:
        continue
    for k, pat in PATRONES.items():
        for m in re.finditer(pat, js, flags=re.I):
            frag = re.sub(r"\s+", " ", js[max(0, m.start() - 120): m.end() + 160])
            clave = m.group(0).lower()
            if clave not in hallado[k] or len(hallado[k]) < 60:
                hallado[k].setdefault(clave, (s.rsplit("/", 1)[-1], frag))
for k, d in hallado.items():
    print(f"\n{'-' * 100}\n## {k}: {len(d)} coincidencias distintas")
    for clave, (arch, frag) in list(d.items())[:25]:
        print(f"  <{clave}> en {arch}: ...{frag}...")
