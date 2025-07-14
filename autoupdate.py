import requests
import os
import hashlib
import pdfplumber
import re
import json
import chardet
from bs4 import BeautifulSoup
from urllib.parse import unquote, urljoin

import firebase_admin
from firebase_admin import credentials, firestore

PDF_URL = "https://labancaria.org/wp-content/uploads/Sintesis.pdf"
PDF_PATH = "Sintesis.pdf"
HASH_FILE = "last_pdf_hash.txt"

# Lee credenciales desde variable de entorno (GitHub Actions) o archivo local (para debug local)
def load_credentials():
    if "FIREBASE_CREDENTIALS" in os.environ:
        import tempfile
        temp = tempfile.NamedTemporaryFile(delete=False, mode='w', suffix='.json')
        temp.write(os.environ["FIREBASE_CREDENTIALS"])
        temp.close()
        return temp.name
    else:
        # Si querés probar local, dejá el archivo junto al script
        return "serviceAccountKey.json"

def extract_clean_urls(pdf_path):
    print(f"Extrayendo URLs de {os.path.basename(pdf_path)}...")
    with pdfplumber.open(pdf_path) as pdf:
        all_text = ""
        for page in pdf.pages:
            text = page.extract_text()
            if text:
                all_text += text + "\n"

        url_pattern = re.compile(
            r'http[s]?://(?:[a-zA-Z]|[0-9]|[$-_@.&+#~!*\'(),/?])+(?:%[0-9a-fA-F][0-9a-fA-F])*'
        )
        urls = url_pattern.findall(all_text)

        cleaned_urls = []
        for url in urls:
            clean_url = re.sub(r'[\s\u00ad\u200b\u200c\u200d]+', '', url)
            clean_url = unquote(clean_url)
            if clean_url.endswith(('.', ',', ';', ':', '>', '<', '[', ']', '{', '}', '"', "'")):
                clean_url = clean_url[:-1]
            clean_url = clean_url.strip()
            cleaned_urls.append(clean_url)
        final_urls = list(set([u for u in cleaned_urls if u.startswith(("http://", "https://"))]))
        return final_urls

def get_metadata(url):
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36"
    }
    try:
        response = requests.get(url, headers=headers, timeout=15)
        response.raise_for_status()
        encoding = chardet.detect(response.content)['encoding']
        html_content = response.content.decode(encoding, errors='replace')
        soup = BeautifulSoup(html_content, 'lxml')

        title_text = ""
        description = ""
        img_url = ""

        title_tag = soup.find('title')
        title_text = title_tag.get_text().strip() if title_tag else ""
        if not title_text:
            og_title = soup.find('meta', attrs={'property': 'og:title'})
            if og_title and og_title.get('content'):
                title_text = og_title.get('content').strip()
            elif not title_text:
                twitter_title = soup.find('meta', attrs={'name': 'twitter:title'})
                if twitter_title and twitter_title.get('content'):
                    title_text = twitter_title.get('content').strip()

        meta_desc = (
            soup.find('meta', attrs={'name': 'description'}) or
            soup.find('meta', attrs={'property': 'og:description'}) or
            soup.find('meta', attrs={'name': 'twitter:description'})
        )
        description = meta_desc.get('content').strip() if meta_desc and meta_desc.get('content') else ""

        meta_img = (
            soup.find('meta', attrs={'property': 'og:image'}) or
            soup.find('meta', attrs={'name': 'twitter:image:src'}) or
            soup.find('link', attrs={'rel': 'image_src'})
        )
        if meta_img and meta_img.get('content'):
            img_url = meta_img.get('content').strip()
            if not img_url.startswith(('http://', 'https://')):
                img_url = urljoin(url, img_url)
        if not img_url:
            possible_images = soup.find_all('img', src=True)
            for img_tag in possible_images:
                src = img_tag.get('src')
                if src and ('logo' not in src.lower() and 'icon' not in src.lower() and 'svg' not in src.lower()):
                    abs_src = urljoin(url, src)
                    if abs_src.startswith(('http://', 'https://')):
                        img_url = abs_src
                        break
        if img_url and ("logo" in img_url.lower() or "default" in img_url.lower() or "blank" in img_url.lower()):
            img_url = ""

        return {
            "title": title_text,
            "description": description,
            "img": img_url,
            "link": url
        }
    except Exception as e:
        print(f"Error con {url}: {type(e).__name__} - {str(e)}")
        return {
            "title": "",
            "description": "",
            "img": "",
            "link": url
        }

def get_remote_pdf_hash(url):
    r = requests.get(url, stream=True)
    hasher = hashlib.md5()
    for chunk in r.iter_content(1024):
        hasher.update(chunk)
    return hasher.hexdigest()

def already_processed(new_hash):
    if os.path.exists(HASH_FILE):
        with open(HASH_FILE, "r") as f:
            last_hash = f.read().strip()
        return last_hash == new_hash
    return False

def save_new_hash(new_hash):
    with open(HASH_FILE, "w") as f:
        f.write(new_hash)

def upload_json_to_firestore(news_json):
    cred_path = load_credentials()
    if not firebase_admin._apps:
        cred = credentials.Certificate(cred_path)
        firebase_admin.initialize_app(cred)
    db = firestore.client()
    with open(news_json, "r", encoding="utf-8") as f:
        noticias = json.load(f)
    for noticia in noticias:
        db.collection('noticias').add(noticia)
        print("Noticia subida:", noticia.get("title"))
    print("Subida a Firestore completada.")

def main():
    new_hash = get_remote_pdf_hash(PDF_URL)
    if already_processed(new_hash):
        print("PDF no cambió, saliendo.")
        return
    print("PDF nuevo detectado, descargando...")
    pdf = requests.get(PDF_URL)
    with open(PDF_PATH, "wb") as f:
        f.write(pdf.content)
    save_new_hash(new_hash)

    urls = extract_clean_urls(PDF_PATH)
    print(f"Total de URLs únicas encontradas: {len(urls)}")
    news_list = []
    for idx, url in enumerate(urls, 1):
        print(f"Procesando {idx}/{len(urls)}: {url}")
        noticia = get_metadata(url)
        if noticia["title"] or noticia["description"] or noticia["img"]:
            news_list.append(noticia)
        else:
            print(f"  Saltando URL '{url}' por falta de metadatos significativos.")

    if news_list:
        with open("noticias.json", "w", encoding="utf-8") as f:
            json.dump(news_list, f, ensure_ascii=False, indent=2)
        print(f"\n✅ JSON guardado en noticias.json con {len(news_list)} noticias válidas.")
        upload_json_to_firestore("noticias.json")
    else:
        print("\n❌ No se encontraron noticias válidas para guardar en noticias.json.")

if __name__ == "__main__":
    main()
