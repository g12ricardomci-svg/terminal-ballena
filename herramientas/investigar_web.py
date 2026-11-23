"""Lee paginas publicas de SpikeCharting (sin iniciar sesion) y resume su contenido y su JavaScript publico."""
import re
from urllib.parse import urljoin, urlparse

import requests
from bs4 import BeautifulSoup

UA = {"User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124 Safari/537.36",
      "Accept-Language": "es-CO,es;q=0.9,en;q=0.8"}
INICIO = ["https://spikecharting.com/", "https://spikecharting.com/en/", "https://spikecharting.com/es/",
          "https://app.spikecharting.com/", "https://academia.spikecharting.com/",
          "https://spikecharting.com/robots.txt", "https://spikecharting.com/sitemap.xml",
          "https://app.spikecharting.com/robots.txt", "https://academia.spikecharting.com/sitemap.xml"]
CLAVES = ["aura", "tick", "plan", "precio", "price", "edge", "openai", "gpt", "claude", "anthropic",
          "gemini", "llm", "deepseek", "mistral", "groq", "api/", "deriv", "affiliate", "afiliad", "terms",
          "términos", "privacidad", "privacy", "empresa", "company", "contact", "soporte", "support", "replay",
          "choch", "fvg", "stripe", "paypal", "mercadopago", "suscrip", "subscription", "supabase", "firebase",
          "wss://", "lightweight-charts", "derivws", "twelvedata", "polygon", "binance"]
vistos, scripts, enlaces = set(), set(), set()


def get(url):
    try:
        r = requests.get(url, headers=UA, timeout=20)
        return r.status_code, r.headers.get("content-type", ""), r.text
    except Exception as e:
        return None, "", f"ERROR {e}"


def texto(html):
    s = BeautifulSoup(html, "html.parser")
    for t in s(["script", "style", "noscript"]):
        t.decompose()
    return re.sub(r"\s+", " ", s.get_text(" ")).strip()


def pagina(url):
    if url in vistos or len(vistos) > 60:
        return
    vistos.add(url)
    code, ctype, body = get(url)
    print(f"\n{'=' * 100}\n### {url}  -> {code} {ctype[:40]}  ({len(body)} chars)")
    if code is None or code >= 400:
        print(body[:300])
        return
    if "xml" in ctype or url.endswith((".xml", ".txt")):
        print(body[:4000])
        for loc in re.findall(r"<loc>(.*?)</loc>", body)[:40]:
            enlaces.add(loc.strip())
        return
    soup = BeautifulSoup(body, "html.parser")
    t = soup.title.string.strip() if soup.title and soup.title.string else ""
    meta = {m.get("name") or m.get("property"): m.get("content") for m in soup.find_all("meta")
            if (m.get("name") or m.get("property")) and m.get("content")}
    print("TITULO:", t)
    for k in ("description", "og:description", "og:title", "keywords", "author", "generator"):
        if k in meta:
            print(f"META {k}: {meta[k][:300]}")
    print("TEXTO:", texto(body)[:6000])
    for sc in soup.find_all("script", src=True):
        scripts.add(urljoin(url, sc["src"]))
    for a in soup.find_all("a", href=True):
        u = urljoin(url, a["href"]).split("#")[0]
        if urlparse(u).netloc.endswith("spikecharting.com"):
            enlaces.add(u)
        elif u.startswith("http"):
            print("ENLACE EXTERNO:", u)


for u in INICIO:
    pagina(u)
for u in sorted(enlaces):
    if not u.endswith((".png", ".jpg", ".svg", ".webp", ".pdf")):
        pagina(u)

print(f"\n{'#' * 100}\n### SCRIPTS PUBLICOS ({len(scripts)})")
for s in sorted(scripts)[:25]:
    code, _, js = get(s)
    print(f"\n--- {s} -> {code} ({len(js)} chars)")
    if not code or code >= 400:
        continue
    urls = sorted(set(re.findall(r"https?://[a-zA-Z0-9._/-]{6,120}", js)))
    print("URLS EN EL CODIGO:", [u for u in urls if "w3.org" not in u and "reactjs" not in u][:80])
    low = js.lower()
    for k in CLAVES:
        n = low.count(k)
        if n:
            ej = [re.sub(r"\s+", " ", js[max(0, m.start() - 90): m.start() + 110])
                  for m in re.finditer(re.escape(k), low)][:3]
            print(f"  [{k}] x{n}: " + " || ".join(ej))
