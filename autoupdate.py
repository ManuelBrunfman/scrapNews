import os, json, hashlib, requests, chardet
from urllib.parse import urljoin, urlparse
from bs4 import BeautifulSoup

import firebase_admin
from firebase_admin import credentials, firestore

###############################################
# Configuración
###############################################
PAGE_URL = "https://labancaria.org/sintesis-de-prensa/"  # página que contiene los links
HASH_FILE = "last_page_hash.txt"                         # para saber si ya la procesamos
COLLECTION = "news"                                     # nombre esperado en tu app RN

# dominios que NO queremos guardar (propios, redes, etc.)
SKIP_DOMAINS = [
    "labancaria.org",
    "facebook.com", "instagram.com", "twitter.com", "x.com",
    "youtube.com", "youtu.be"
]

HEADERS = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; rv:128.0) Gecko/20100101 Firefox/128.0"}

###############################################
# Utilidades
###############################################

def load_credentials():
    """Devuelve la ruta del JSON de credenciales, leyendo desde una variable de entorno
    (GitHub Actions) o desde un archivo local si se corre a mano."""
    if "FIREBASE_CREDENTIALS" in os.environ:
        import tempfile
        f = tempfile.NamedTemporaryFile(delete=False, mode="w", suffix=".json")
        f.write(os.environ["FIREBASE_CREDENTIALS"])
        f.close()
        return f.name
    return "serviceAccountKey.json"


def already_processed(new_hash: str) -> bool:
    return os.path.isfile(HASH_FILE) and open(HASH_FILE).read().strip() == new_hash


def save_new_hash(new_hash: str):
    with open(HASH_FILE, "w") as f:
        f.write(new_hash)

###############################################
# Scraping de la página principal
###############################################

def extract_urls_from_page(url: str = PAGE_URL):
    resp = requests.get(url, timeout=15, headers=HEADERS)
    resp.raise_for_status()
    soup = BeautifulSoup(resp.text, "lxml")

    post = soup.find("div", class_="entry-content") or soup
    urls = []
    for a in post.find_all("a", href=True):
        href = a["href"].strip()
        if not href.startswith(("http://", "https://")):
            continue
        if any(domain in href for domain in SKIP_DOMAINS):
            continue
        # Omitir homepages o URLs sin ruta (ej. https://ejemplo.com/ )
        if urlparse(href).path.strip("/") == "":
            continue
        urls.append(href)
    # eliminar duplicados
    return list(set(urls)), resp.text

###############################################
# Obtener metadatos de cada noticia
###############################################

def get_metadata(link: str):
    try:
        r = requests.get(link, timeout=15, headers=HEADERS)
        r.raise_for_status()
        enc = chardet.detect(r.content)["encoding"] or "utf-8"
        soup = BeautifulSoup(r.content.decode(enc, "replace"), "lxml")

        def meta_content(*names):
            for name in names:
                tag = soup.find("meta", attrs=name) if isinstance(name, dict) else soup.find("meta", property=name)
                if tag and tag.get("content"):
                    return tag["content"].strip()
            return ""

        title = meta_content({"property": "og:title"}, {"name": "twitter:title"}) or (soup.title.string.strip() if soup.title else "")
        description = meta_content({"name": "description"}, {"property": "og:description"}, {"name": "twitter:description"})
        img = meta_content({"property": "og:image"}, {"name": "twitter:image:src"})
        if img and not img.startswith(("http://", "https://")):
            img = urljoin(link, img)

        return {"title": title, "description": description, "img": img, "link": link}
    except Exception as e:
        print(f"⚠️  Error con {link}: {e}")
        return {"title": "", "description": "", "img": "", "link": link}

###############################################
# Subir a Firestore (limpia colección primero)
###############################################

def clear_collection(col_ref):
    """Borra documentos en lotes de 500 hasta que la colección quede vacía."""
    deleted_total = 0
    while True:
        docs = list(col_ref.limit(500).stream())
        if not docs:
            break
        for d in docs:
            d.reference.delete()
            deleted_total += 1
        print(f"  Borrados {deleted_total} documentos…")
    return deleted_total


def upload_to_firestore(news_items):
    cred_path = load_credentials()
    if not firebase_admin._apps:
        firebase_admin.initialize_app(credentials.Certificate(cred_path))
    db = firestore.client()

    col_ref = db.collection(COLLECTION)
    # 1) Vaciar la colección antes de cargar las nuevas noticias
    deleted = clear_collection(col_ref)
    print(f"Colección limpia: {deleted} docs eliminados.")

    # 2) Subir las noticias nuevas
    for n in news_items:
        n["createdAt"] = firestore.SERVER_TIMESTAMP  # timestamp para ordenar en la app
        doc_id = hashlib.md5(n["link"].encode()).hexdigest()
        col_ref.document(doc_id).set(n)
        print("Noticia subida:", n["title"][:80])
    print("Subida completa ✔️")

###############################################
# Main
###############################################

def main():
    urls, html = extract_urls_from_page()
    page_hash = hashlib.md5(html.encode()).hexdigest()

    if already_processed(page_hash):
        print("La página no cambió; salgo.")
        return
    save_new_hash(page_hash)

    print(f"▶️  {len(urls)} URLs encontradas. Procesando…")
    news_list = []
    for i, url in enumerate(urls, 1):
        print(f"[{i}/{len(urls)}] {url}")
        meta = get_metadata(url)
        if meta["title"] or meta["description"]:
            news_list.append(meta)

    if news_list:
        with open("noticias.json", "w", encoding="utf-8") as f:
            json.dump(news_list, f, ensure_ascii=False, indent=2)
        print(f"JSON generado con {len(news_list)} noticias.")
        upload_to_firestore(news_list)
    else:
        print("No se obtuvieron noticias válidas.")


if __name__ == "__main__":
    main()
