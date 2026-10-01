"""
Bot PS5 Cyber Day Chile
- Revisa Falabella, Paris, Ripley, PC Factory cada N segundos
- Detecta baja de precio / quiebre de umbral $400.000
- Avisa por Telegram (instantáneo y gratis)

Uso:
  pip install -r requirements.txt
  playwright install chromium  (solo 1 vez)
  configura .env y luego:  python tracker.py
"""
import asyncio
import json
import os
import random
import re
import sys
from datetime import datetime
from pathlib import Path

import httpx
from bs4 import BeautifulSoup
from dotenv import load_dotenv

import config

load_dotenv()

BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")
STATE_FILE = Path(__file__).parent / "state.json"

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36",
    "Accept-Language": "es-CL,es;q=0.9",
    "Accept": "text/html,application/xhtml+xml",
}

def parse_precio_cl(texto: str) -> int | None:
    """$609.990 / $ 1.299.990 -> 609990. Exige $ para no tragarse SKUs."""
    if not texto:
        return None
    from collections import Counter
    # solo precios con $ delante y con formato chileno con puntos
    matches = re.findall(r"\$\s*(\d{1,3}(?:\.\d{3})+)", texto)
    precios = []
    for m in matches:
        try:
            v = int(m.replace(".", ""))
            if 100000 <= v <= 3000000:
                precios.append(v)
        except ValueError:
            continue
    if not precios:
        return None
    # el más frecuente (el precio principal se repite muchas veces), desempate: el menor
    conteo = Counter(precios)
    top = conteo.most_common(3)
    return top[0][0]


def normalizar(txt: str) -> str:
    import unicodedata
    txt = unicodedata.normalize("NFD", txt.lower())
    return "".join(c for c in txt if unicodedata.category(c) != "Mn")


def extraer_cupones(html: str) -> list:
    """Busca cupon de DESCUENTO: exige palabra clave + precio $ cerca.
    Ignora 'cupón de juego' del bundle, footers y legales. Max 2."""
    soup = BeautifulSoup(html, "lxml")
    for tag in soup(["script", "style", "noscript"]):
        tag.decompose()
    texto = soup.get_text(" ", strip=True)
    norm = normalizar(texto)
    kws = sorted((normalizar(k) for k in config.CUPON_KEYWORDS), key=len, reverse=True)
    excl = [normalizar(e) for e in getattr(config, "CUPON_EXCLUIR", [])]
    hallazgos = []
    for kw in kws:
        for m in re.finditer(r"\b" + re.escape(kw) + r"\b", norm):
            i = m.start()
            a = max(0, i - 120)
            ctx = texto[a:i + len(kw) + 120]
            ctx = re.sub(r"\s+", " ", ctx).strip()
            nctx = normalizar(ctx)
            if any(e in nctx for e in excl):
                continue
            if "$" not in ctx:
                continue
            precio = parse_precio_cl(ctx)
            hallazgos.append({"kw": kw, "contexto": ctx[:220], "precio": precio})
            if len(hallazgos) >= 6:
                break
    vistos, final = set(), []
    for h in hallazgos:
        key = h["contexto"][:80]
        if key not in vistos:
            vistos.add(key)
            final.append(h)
        if len(final) >= 2:
            break
    return final


def extraer_precios(html: str, tienda: str = "") -> tuple[int | None, int | None, str]:
    """Retorna (internet, tarjeta, metodo).
    internet = precio sin tarjeta / normal web
    tarjeta = precio con tarjeta tienda (CMR, Cencosud, Ripley) o None si no hay
    """
    from collections import Counter
    soup = BeautifulSoup(html, "lxml")
    t = tienda.lower()

    def to_int(v) -> int | None:
        try:
            n = int(float(str(v).replace("$", "").replace(".", "").replace(",", "").strip()))
            return n if 100000 <= n <= 3000000 else None
        except Exception:
            return None

    def parse_monto(txt: str) -> int | None:
        txt = txt.replace("$", "").replace(" ", "").replace(".", "")
        try:
            return to_int(txt)
        except Exception:
            return None

    # --- Paris: offers en ld+json = [normal, internet, tarjeta] ---
    if "paris" in t:
        ofertas = []
        for tag in soup.find_all("script", type="application/ld+json"):
            try:
                data = json.loads(tag.string or "")
                items = data if isinstance(data, list) else [data]
                for d in items:
                    if isinstance(d, dict) and d.get("@type") == "Product":
                        for o in (d.get("offers") or []):
                            v = to_int(o.get("price"))
                            if v:
                                ofertas.append(v)
            except Exception:
                continue
        if ofertas:
            s = sorted(set(ofertas))
            if len(s) >= 3:
                return s[1], s[0], "paris-ld"  # internet, tarjeta
            elif len(s) == 2:
                return s[1], s[0], "paris-ld"
            return s[0], None, "paris-ld"

    # --- Ripley: bloque principal .normal-price-group ---
    if "ripley" in t:
        vals = [e.get_text(strip=True) for e in soup.select(".normal-price-group .price-value")[:3]]
        nums = [parse_monto(x) for x in vals]
        nums = [n for n in nums if n]
        # esperado: [normal, internet, tarjeta]
        if len(nums) >= 3:
            return nums[1], nums[2], "ripley-web"
        if len(nums) == 2:
            return nums[0], nums[1], "ripley-web"

    # --- Falabella: JSON "type":"cmrPrice|internetPrice|normalPrice" ---
    if "falabella" in t:
        tipos: dict[str, int] = {}
        for m in re.finditer(
            r'"type"\s*:\s*"(cmrPrice|internetPrice|eventPrice|normalPrice)"[^\}]{0,150}?"price"\s*:\s*\[?"?([\d\.]+)"?\]?',
            html,
        ):
            tipo, val = m.group(1), m.group(2)
            v = to_int(val.replace(".", ""))
            # Falabella repite para garantías, quedarnos con el primero de cada tipo (el del producto)
            if tipo not in tipos and v:
                tipos[tipo] = v
            if len(tipos) >= 3:
                break
        if tipos:
            internet = tipos.get("internetPrice") or tipos.get("eventPrice")
            tarjeta = tipos.get("cmrPrice")
            # si no hay cmr, tarjeta = None, internet manda
            metodo = "falabella-" + "+".join(sorted(tipos.keys()))
            return internet, tarjeta, metodo

    # --- Fallback genérico (una sola cifra) ---
    precio, metodo = extraer_precio_html(html)
    return precio, None, metodo


def extraer_precio_html(html: str) -> tuple[int | None, str]:
    from collections import Counter
    soup = BeautifulSoup(html, "lxml")
    # 1) JSON-LD (el más fiable en Paris/Falabella)
    for tag in soup.find_all("script", type="application/ld+json"):
        try:
            data = json.loads(tag.string or "")
            items = data if isinstance(data, list) else [data]
            for d in items:
                offers = (d.get("offers") if isinstance(d, dict) else None)
                if offers:
                    offs = offers if isinstance(offers, list) else [offers]
                    for o in offs:
                        p = o.get("price") or o.get("lowPrice")
                        if p:
                            v = int(float(str(p)))
                            if 100000 <= v <= 3000000:
                                return v, "json-ld"
        except Exception:
            continue
    # 2) meta tags
    for sel in ['meta[property="product:price:amount"]', 'meta[itemprop="price"]', 'meta[property="og:price:amount"]']:
        tag = soup.select_one(sel)
        if tag and tag.get("content"):
            try:
                v = int(float(tag["content"]))
                if 100000 <= v <= 3000000:
                    return v, "meta"
            except ValueError:
                pass
    # 2b) JSON genérico tipo "price":"709990" (Ripley usa __NEXT_DATA__, no ld+json)
    genericos = re.findall(r'"(?:price|salePrice|offerPrice|bestPrice)"\s*:\s*"?(\d{5,7}(?:\.\d+)?)"?', html)
    vals = []
    for g in genericos:
        try:
            v = int(float(g))
            if 100000 <= v <= 3000000:
                vals.append(v)
        except ValueError:
            continue
    if vals:
        return Counter(vals).most_common(1)[0][0], "json-data"
    # 3) texto visible
    texto = soup.get_text(" ", strip=True)
    precio = parse_precio_cl(texto)
    return precio, "texto" if precio else (None, "nada")


async def fetch_requests(url: str, tienda: str = "") -> tuple[int | None, int | None, str, str]:
    """Retorna (internet, tarjeta, metodo, detalle). Prueba varias identidades (Ripley bloquea datacenters)."""
    def _get(imp: str):
        from curl_cffi import requests as creq
        r = creq.get(
            url,
            impersonate=imp,  # type: ignore
            timeout=25,
            headers={
                "Accept-Language": "es-CL,es;q=0.9",
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                "Referer": "https://www.google.cl/",
            },
        )
        return r.status_code, r.text

    last_status, last_len = None, 0
    try:
        for imp in ("chrome", "safari15_5", "chrome120"):
            try:
                status, text = await asyncio.to_thread(_get, imp)
            except Exception as e:
                last_status = f"err-{imp}"
                continue
            last_status, last_len = status, len(text)
            if status != 200:
                await asyncio.sleep(1)
                continue
            internet, tarjeta, metodo = extraer_precios(text, tienda)
            cupones = extraer_cupones(text)
            if internet is not None:
                return internet, tarjeta, f"{metodo}+{imp}", f"HTTP 200 ({len(text)//1000}kb)", cupones
        return None, None, "http", f"HTTP {last_status} len={last_len}", []
    except Exception as e:
        # fallback httpx por si acaso
        try:
            async with httpx.AsyncClient(headers=HEADERS, timeout=25, follow_redirects=True) as c:
                r = await c.get(url)
                if r.status_code != 200:
                    return None, None, "http", f"HTTP {r.status_code} + curl fail {str(e)[:80]}", []
                internet, tarjeta, metodo = extraer_precios(r.text, tienda)
                return internet, tarjeta, metodo, f"HTTP 200 httpx ({len(r.text)//1000}kb)", extraer_cupones(r.text)
        except Exception as e2:
            return None, None, "http", f"error: {e} / {e2}", []


async def fetch_playwright(url: str, tienda: str = "") -> tuple[int | None, int | None, str, str]:
    """Intento con navegador real (para Falabella/Ripley/Paris con anti-bot)."""
    try:
        from playwright.async_api import async_playwright
    except ImportError:
        return None, None, "playwright", "sin playwright en nube", []
    try:
        async with async_playwright() as p:
            browser = await p.chromium.launch(headless=True)
            page = await browser.new_page(user_agent=HEADERS["User-Agent"], locale="es-CL")
            await page.goto(url, wait_until="domcontentloaded", timeout=45000)
            await page.wait_for_timeout(4000)
            # scroll para gatillar lazy-load de precios
            await page.evaluate("window.scrollTo(0, 800)")
            await page.wait_for_timeout(1500)
            html = await page.content()
            await browser.close()
            internet, tarjeta, metodo = extraer_precios(html, tienda)
            return internet, tarjeta, f"playwright+{metodo}", "navegador real", extraer_cupones(html)
    except Exception as e:
        return None, None, "playwright", f"error: {str(e)[:200]}", []


async def chequear_producto(prod: dict) -> dict:
    url = prod["url"]
    tienda = prod.get("tienda", "")
    # 1) intento rápido
    internet, tarjeta, metodo, detalle, cupones = await fetch_requests(url, tienda)
    # 2) si falla o no hay precio, usar navegador
    if internet is None:
        i2, t2, metodo2, detalle2, c2 = await fetch_playwright(url, tienda)
        if i2 is not None:
            internet, tarjeta, metodo, detalle, cupones = i2, t2, metodo2, detalle2, c2
        else:
            detalle = detalle + " | " + detalle2
    mejor = None
    for v in (internet, tarjeta):
        if v is not None and (mejor is None or v < mejor):
            mejor = v
    return {
        "tienda": prod["tienda"],
        "nombre": prod["nombre"],
        "url": url,
        "precio": mejor,
        "internet": internet,
        "tarjeta": tarjeta,
        "cupones": cupones,
        "metodo": metodo,
        "detalle": detalle,
        "hora": datetime.now().isoformat(timespec="seconds"),
    }


def cargar_estado() -> dict:
    if STATE_FILE.exists():
        try:
            return json.loads(STATE_FILE.read_text(encoding="utf-8"))
        except Exception:
            return {}
    return {}


def guardar_estado(estado: dict):
    STATE_FILE.write_text(json.dumps(estado, indent=2, ensure_ascii=False), encoding="utf-8")


SUBS_FILE = Path(__file__).parent / "subscribers.json"
DEFAULT_PREFS = {"ps5": True, "switch": True}


def categoria(prod: dict) -> str:
    """ps5 o switch. Los Switch empiezan con 'SW' en el nombre."""
    if prod.get("cat") in ("ps5", "switch"):
        return prod["cat"]
    return "switch" if prod.get("nombre", "").startswith("SW") else "ps5"


def cargar_subs() -> dict:
    subs: dict = {}
    if CHAT_ID:
        subs[str(CHAT_ID)] = dict(DEFAULT_PREFS)
    if SUBS_FILE.exists():
        try:
            data = json.loads(SUBS_FILE.read_text(encoding="utf-8"))
            if isinstance(data, list):  # formato viejo: lista de chats
                for x in data:
                    subs[str(x)] = dict(DEFAULT_PREFS)
            elif isinstance(data, dict):
                for chat, prefs in data.items():
                    p = dict(DEFAULT_PREFS)
                    if isinstance(prefs, dict):
                        p.update({k: bool(v) for k, v in prefs.items() if k in p})
                    subs[str(chat)] = p
        except Exception:
            pass
    return subs


def guardar_subs(subs: dict):
    SUBS_FILE.write_text(json.dumps(subs, indent=2, ensure_ascii=False), encoding="utf-8")


def agregar_sub(chat: str):
    try:
        subs = cargar_subs()
        if str(chat) not in subs:
            subs[str(chat)] = dict(DEFAULT_PREFS)
            guardar_subs(subs)
            print(f"  [Subs] nuevo {chat} (total {len(subs)})", flush=True)
    except Exception as e:
        print(f"  [Subs] error: {e}", flush=True)


def set_pref(chat: str, cat: str, valor: bool):
    subs = cargar_subs()
    p = subs.get(str(chat), dict(DEFAULT_PREFS))
    p[cat] = valor
    subs[str(chat)] = p
    guardar_subs(subs)


def quitar_sub(chat: str):
    try:
        subs = cargar_subs()
        subs.pop(str(chat), None)
        if CHAT_ID:
            subs.setdefault(str(CHAT_ID), dict(DEFAULT_PREFS))
        guardar_subs(subs)
    except Exception:
        pass


async def enviar_a_todos(mensaje: str, cat: str | None = None):
    for chat, prefs in sorted(cargar_subs().items()):
        if cat is None or prefs.get(cat, True):
            await enviar_telegram(mensaje, chat_id=chat)


async def enviar_telegram(mensaje: str, chat_id: str | None = None):
    if not BOT_TOKEN or not CHAT_ID:
        print("  [Telegram] sin configurar (.env), solo consola.")
        print(f"  {mensaje[:300]}")
        return
    try:
        destino = chat_id or CHAT_ID
        async with httpx.AsyncClient(timeout=15) as c:
            r = await c.post(
                f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage",
                json={"chat_id": destino, "text": mensaje, "disable_web_page_preview": False},
            )
            if r.status_code != 200:
                print(f"  [Telegram] error {r.status_code}: {r.text[:200]}")
            else:
                print("  [Telegram] enviado OK")
    except Exception as e:
        print(f"  [Telegram] error: {e}")


def formato_clp(n: int) -> str:
    return "$" + f"{n:,}".replace(",", ".")


async def ronda():
    estado = cargar_estado()
    print(f"\n=== Ronda {datetime.now().strftime('%H:%M:%S')} ({len(config.PRODUCTS)} productos) ===", flush=True)
    sem = asyncio.Semaphore(3)  # max 3 tiendas en paralelo para no parecer bot

    async def procesar(prod):
        meta = prod.get("meta", config.PRECIO_OBJETIVO)
        async with sem:
            await asyncio.sleep(random.uniform(0.5, 1.5))
            try:
                res = await chequear_producto(prod)
            except Exception as e:
                print(f"? {prod.get('tienda')}: error {str(e)[:120]}", flush=True)
                return
        key = res["url"]
        previo = estado.get(key, {}).get("precio")
        actual = res["precio"]
        internet = res.get("internet")
        tarjeta = res.get("tarjeta")

        if actual is None:
            print(f"? {res['tienda']}: no pude leer precio ({res['detalle'][:100]})", flush=True)
            print(f"   {res['url']}", flush=True)
        else:
            flag = ""
            if actual <= meta:
                flag = f" !!BAJO META {formato_clp(meta)}!!"
            detalle_precios = ""
            if internet:
                detalle_precios += f" internet {formato_clp(internet)}"
            if tarjeta:
                detalle_precios += f" | tarjeta {formato_clp(tarjeta)}"
            print(f"{'[ALERTA]' if flag else '[OK]'} {res['tienda']} {res['nombre'][:30]}: mejor {formato_clp(actual)}{flag} ({detalle_precios.strip()}) (antes: {formato_clp(previo) if previo else '-'}) [{res['metodo']}]", flush=True)

            # Avisar si: primera vez bajo umbral, bajó de precio, o bajó vs anterior
            debe_avisar = False
            motivo = ""
            if previo is None and actual <= meta:
                debe_avisar, motivo = True, "esta bajo tu meta"
            elif previo is not None and actual < previo:
                debe_avisar, motivo = True, f"bajo de {formato_clp(previo)} a {formato_clp(actual)}"
            elif previo is None:
                pass

            if debe_avisar:
                lineas = [
                    "Cyber Day",
                    f"{res['tienda']} - {res['nombre']}",
                    motivo,
                ]
                if internet:
                    lineas.append(f"Internet: {formato_clp(internet)}")
                if tarjeta:
                    lineas.append(f"Tarjeta: {formato_clp(tarjeta)}")
                lineas.append(f"Mejor: {formato_clp(actual)}")
                lineas.append(f"Meta: {formato_clp(meta)}")
                lineas.append(res["url"])
                await enviar_a_todos("\n".join(lineas), cat=categoria(res))

            # Cupones: avisar solo si aparece algo nuevo
            cupones = res.get("cupones") or []
            firma = "|".join(sorted(h["contexto"][:60] for h in cupones))
            firma_previa = estado.get(key, {}).get("cupones_sig", "")
            if cupones and firma != firma_previa:
                cl = ["CUPON detectado", f"{res['tienda']} - {res['nombre']}"]
                for h in cupones[:2]:
                    extra = f" (precio cercano: {formato_clp(h['precio'])})" if h.get("precio") else ""
                    cl.append(f"- '{h['kw']}'{extra}: {h['contexto'][:160]}")
                cl.append(res["url"])
                await enviar_a_todos("\n".join(cl), cat=categoria(res))

        key_sig = ""
        if actual is not None:
            cup_list = res.get("cupones") or []
            key_sig = "|".join(sorted(h["contexto"][:60] for h in cup_list))
        else:
            key_sig = estado.get(key, {}).get("cupones_sig", "")
        estado[key] = {"precio": actual, "internet": internet if actual is not None else estado.get(key, {}).get("internet"),
                       "tarjeta": tarjeta if actual is not None else estado.get(key, {}).get("tarjeta"),
                       "cupones_sig": key_sig,
                       "hora": res["hora"]}

    await asyncio.gather(*(procesar(p) for p in config.PRODUCTS))
    guardar_estado(estado)


async def telegram_poll_loop():
    """Escucha mensajes al bot. Si escribes 'test', responde que esta OK."""
    import os
    instancia = os.getenv("INSTANCIA", "nube" if os.getenv("PORT") else "pc")
    if not BOT_TOKEN or not CHAT_ID:
        return
    offset = 0
    print(f"  [Telegram] escucha activa ({instancia}): escribeme 'test' o 'precios'", flush=True)
    while True:
        try:
            async with httpx.AsyncClient(timeout=40) as c:
                r = await c.get(
                    f"https://api.telegram.org/bot{BOT_TOKEN}/getUpdates",
                    params={"offset": offset, "timeout": 25},
                )
                data = r.json() if r.status_code == 200 else {}
            for up in data.get("result", []):
                offset = up["update_id"] + 1
                msg = up.get("message") or {}
                chat = str(msg.get("chat", {}).get("id", ""))
                if not chat:
                    continue
                texto = (msg.get("text") or "").strip().lower()
                if texto in ("salir", "stop", "/stop"):
                    quitar_sub(chat)
                    await enviar_telegram("Listo, ya no te avisare de bajas.", chat_id=chat)
                    continue
                # auto-suscripcion: quien escriba queda registrado para las alertas
                agregar_sub(chat)
                if texto in ("mejor", "/mejor", "mejorprecio", "mejor precio", "barato", "masbarato", "mas barato", "más barato", "sintarjeta", "sin tarjeta"):
                    estado = cargar_estado()
                    filas = []
                    for prod in config.PRODUCTS:
                        v = estado.get(prod["url"], {})
                        if v.get("internet"):
                            filas.append((v["internet"], prod))
                    if not filas:
                        await enviar_telegram("Aun sin lecturas de precio internet.", chat_id=chat)
                    else:
                        filas.sort(key=lambda x: x[0])
                        lineas = ["Mas barato SIN tarjeta (solo internet):"]
                        for precio, prod in filas[:5]:
                            lineas.append(f"- {prod['tienda']} {prod['nombre'][:28]}: {formato_clp(precio)}")
                        await enviar_telegram("\n".join(lineas), chat_id=chat)
                    continue
                    set_pref(chat, "ps5", True)
                    set_pref(chat, "switch", False)
                    await enviar_telegram("Listo: solo te avisare de PS5. Con /todo vuelves a ver todo.", chat_id=chat)
                    continue
                if texto in ("soloswitch", "/soloswitch", "solo switch"):
                    set_pref(chat, "switch", True)
                    set_pref(chat, "ps5", False)
                    await enviar_telegram("Listo: solo te avisare de Switch. Con /todo vuelves a ver todo.", chat_id=chat)
                    continue
                if texto in ("todo", "/todo", "ambos", "todas"):
                    set_pref(chat, "ps5", True)
                    set_pref(chat, "switch", True)
                    await enviar_telegram("Listo: te avisare de PS5 y Switch.", chat_id=chat)
                    continue
                # comandos abiertos a cualquiera (las alertas de precio siguen yendo solo al dueno)
                if texto in ("test", "/test", "hola", "ok", "/start", "start"):
                    estado = cargar_estado()
                    mejores = [(v.get("precio"), k) for k, v in estado.items() if v.get("precio")]
                    mejor_txt = f"Mejor visto: {formato_clp(min(m[0] for m in mejores))}" if mejores else "Aun sin primera ronda"
                    await enviar_telegram(
                        f"Bot OK ({instancia}). Vigilando {len(config.PRODUCTS)} productos cada {config.INTERVALO_SEGUNDOS}s.\n"
                        f"Meta: {formato_clp(config.PRECIO_OBJETIVO)}.\n{mejor_txt}.",
                        chat_id=chat,
                    )
                elif texto in ("precios", "/precios", "precio"):
                    estado = cargar_estado()
                    lineas = ["Precios actuales:"]
                    for prod in config.PRODUCTS:
                        v = estado.get(prod["url"], {})
                        if v.get("precio"):
                            extra = ""
                            if v.get("internet"):
                                extra += f" int {formato_clp(v['internet'])}"
                            if v.get("tarjeta"):
                                extra += f" tarj {formato_clp(v['tarjeta'])}"
                            lineas.append(f"- {prod['tienda']} {prod['nombre'][:25]}: {formato_clp(v['precio'])} ({extra.strip()})")
                        else:
                            lineas.append(f"- {prod['tienda']} {prod['nombre'][:25]}: sin lectura")
                    await enviar_telegram("\n".join(lineas[:40]), chat_id=chat)
        except Exception as e:
            print(f"  [Telegram-poll] error: {e}", flush=True)
            await asyncio.sleep(10)
        await asyncio.sleep(1)


async def main():
    print("Bot PS5 Cyber Day iniciado", flush=True)
    print(f"   Meta: <= ${config.PRECIO_OBJETIVO:,} | Intervalo: {config.INTERVALO_SEGUNDOS}s", flush=True)
    if not BOT_TOKEN:
        print("   Telegram no configurado. Crea .env (ver .env.example). Igual veras precios en consola.", flush=True)
    asyncio.create_task(telegram_poll_loop())
    while True:
        try:
            await ronda()
        except KeyboardInterrupt:
            print("\nDetenido.")
            sys.exit(0)
        except Exception as e:
            print(f"Error en ronda: {e}")
        # jitter +-20% para no ser predecible
        espera = config.INTERVALO_SEGUNDOS * random.uniform(0.8, 1.2)
        print(f"⏳ próxima revisión en {int(espera)}s...")
        await asyncio.sleep(espera)


if __name__ == "__main__":
    asyncio.run(main())
