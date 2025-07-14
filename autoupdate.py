import requests, os, hashlib, json, chardet
from bs4 import BeautifulSoup
from urllib.parse import urljoin
import firebase_admin
from firebase_admin import credentials, firestore

# --- CONFIG ---------------------------------------------
PAGE_URL   = "https://labancaria.org/sintesis-de-prensa/"
HASH_FILE  = "last_page_hash.txt"
# ---------------------------------------------------------

# Cargar credenciales de Firebase (desde secreto en Actions o archivo local para pruebas)
def load_credentials():
    if "FIREBASE_CREDENTIALS" in os.environ:
        import tempfile
        tmp = tempfile.NamedTemporaryFile(delete=False, mode="w", suffix=".json")
        tmp.write(os.environ["FIREBASE_CREDENTIALS"])
        tmp.close()
        return tmp.name
    return "serviceAccountKey.json"   # para correr local

# Extraer todas las URLs de la síntesis de prensa (HTML)
def extract_urls_from_page(url=PAGE_URL):
    resp = requests.get(url, timeout=15, headers={"User-Agent": "Mozilla/5.0"})
    resp.raise_for_status()
    soup = BeautifulSoup(resp.text, "lxml")

    post = soup.find("div", class_="entry-content") or soup  # por si cambia el theme
    anchors = post.find_all("a", href=True)

    urls = [a["href"].strip() for a in anchors
            if a["href"].startswith(("http://", "https://"))]
    return list(set(urls)), resp.text            # devuelvo el HTML para hashear

# Sacar metadatos de cada noticia
def get_metadata(url):
    try:
        r = requests.get(url, timeout=15, headers={"User-Agent": "Mozilla/5.0"})
        r.raise_for_status()
        enc = chardet.detect(r.content)['encoding']
        soup = BeautifulSoup(r.content.decode(enc, "replace"), "lxml")

        title = (
            soup.find("meta", property="og:title") or
            soup.find("meta", attrs={"name": "twitter:title"}) or
            soup.find("title")
        )
        title = title.get("content", "").strip() if title else (soup.title.string.strip() if soup.title else "")

        desc = (
            soup.find("meta", attrs={"name": "description"}) or
            soup.find("meta", property="og:description") or
            soup.find("meta", attrs={"name": "twitter:description"})
        )
        desc = desc.get("content", "").strip() if desc else ""

        img  = (
            soup.find("meta", property="og:image") or
            soup.find("meta", attrs={"name": "twitter:image:src"})
        )
        img = img.get("content", "").strip() if img else ""
        if img and not img.startswith(("http://", "https://")):
            img = urljoin(url, img)

        return {"title": title, "description": desc, "img": img, "link": url}
    except Exception as e:
        print(f"⚠️  Error con {url}: {e}")
        return {"title": "", "description": "", "img": "", "link": url}

# ------ helpers de hash para saber si la página cambió --------------
def already_processed(new_hash):
    return os.path.isfile(HASH_FILE) and open(HASH_FILE).read().strip() == new_hash

def save_new_hash(new_hash):
    with open(HASH_FILE, "w") as f:
        f.write(new_hash)

# ------ subida a Firestore -----------------------------------------
def upload_json_to_firestore(path):
    cred_path = load_credentials()
    if not firebase_admin._apps:
        firebase_admin.initialize_app(credentials.Certificate(cred_path))
    db = firestore.client()

    noticias = json.load(open(path, encoding="utf-8"))
    for n in noticias:
        db.collection("noticias").add(n)
        print("Noticia subida:", n.get("title", "")[:80])
    print("Subida completa ✔️")

# ---------------------- MAIN ---------------------------------------
def main():
    urls, html = extract_urls_from_page()
    page_hash = hashlib.md5(html.encode()).hexdigest()

    if already_processed(page_hash):
        print("La página no cambió; salgo.")
        return
    save_new_hash(page_hash)

    print(f"▶️  {len(urls)} URLs encontradas. Procesando…")
    news = []
    for i, u in enumerate(urls, 1):
        print(f"[{i}/{len(urls)}] {u}")
        meta = get_metadata(u)
        if meta["title"] or meta["description"]:
            news.append(meta)

    if news:
        json.dump(news, open("noticias.json", "w", encoding="utf-8"),
                  ensure_ascii=False, indent=2)
        print(f"JSON generado con {len(news)} noticias.")
        upload_json_to_firestore("noticias.json")
    else:
        print("No se obtuvieron noticias válidas.")

if __name__ == "__main__":
    main()
