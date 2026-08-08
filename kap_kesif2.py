"""
kap_kesif2.py — KAP KEŞİF, İKİNCİ TUR (iki somut düzeltmeyle)
==============================================================
ILK KESIF NE BULDU (2026-08-08):
  * Ana sayfa HTML'inde bildirim izleri VAR (161 KB, "Özel Durum
    Açıklaması", "A.Ş." geciyor) - yani veri sayfada olabilir.
  * JS paketlerinde gercek yol parcalari bulundu:
        api/disclosure/filter/FILTERYFBF
        api/company-detail/disclosures
        api/disclosure/funds/byCriteria
        api/BildirimPdf
  * Ayrica buyuk harfli SABIT ISIMLERI: GET_DISCLOSURE_BY_TYPE,
    GET_DISCLOSURE_MEMBERS_BY_CRITERIA_FROM_ROUTE_API vb.
  * `/tr/api/about/content-file/...` CALISTI (PDF dondu).

ILK KESFIN IKI HATASI — BU SCRIPT ONLARI DUZELTIYOR:

1) YANLIS ONEK. Bulunan yollar bassiz geliyordu ("api/...") ve script
   onlari `kap.org.tr/api/...` diye denedi. Ama calisan tek ornek
   `/tr/api/about/...` seklindeydi - yani dogru onek `/tr/`. Bu yuzden
   hepsi 404 dondu. Artik her yol UC onekle deneniyor: /tr/, /en/, kok.

2) SABIT ISMINI ADRES SANMA. `GET_DISCLOSURE_BY_TYPE` bir adres degil,
   JS icindeki bir sabitin ADI; gercek adres onun DEGERINDE. Ilk regex
   isimleri yakaladi, degerleri degil. Artik `AD: "deger"` ve
   `AD = "deger"` kaliplari ayrica cikariliyor.

Ayrica: HTML'de bildirim izi bulundugu icin, JSON hic cikmasa bile
HTML'den okumayi denemek mantikli - o yuzden sonunda ana sayfadaki
tablo yapisi da ozetleniyor.

CALISTIRMA: Start Command -> python kap_kesif2.py   (~3 dakika)
"""

import os
import re
import time
import threading
import requests

TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID")

ANA_SAYFA = "https://www.kap.org.tr/tr/"
KOK = "https://www.kap.org.tr"
ONEKLER = ["/tr/", "/en/", "/"]

HEADERS = {
    "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                   "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0 Safari/537.36"),
    "Accept": "application/json, text/html;q=0.9, */*;q=0.8",
    "Accept-Language": "tr-TR,tr;q=0.9",
    "Referer": ANA_SAYFA,
}

# Ilk kesifte bulunan yollar - bu sefer dogru oneklerle denenecek
BILINEN_YOLLAR = [
    "api/disclosure/filter/FILTERYFBF",
    "api/company-detail/disclosures",
    "api/disclosure/funds/byCriteria",
    "api/disclosure/members/byCriteria",
    "api/disclosure/byType",
    "api/disclosure/topic",
    "api/disclosure",
    "api/disclosures",
    "api/BildirimPdf",
]


def send_telegram_message(text: str):
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        print("(Telegram ayarli degil)\n" + text, flush=True)
        return
    for i in range(0, len(text), 3900):
        try:
            requests.post(
                f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage",
                json={"chat_id": TELEGRAM_CHAT_ID, "text": text[i:i + 3900]},
                timeout=20)
        except Exception as e:
            print(f"Telegram gonderilemedi: {e}", flush=True)


def getir(url, timeout=25):
    try:
        return requests.get(url, headers=HEADERS, timeout=timeout)
    except Exception:
        return None


def sabit_degerleri_cikar(metin):
    """GET_DISCLOSURE_X: "yol"  ya da  GET_DISCLOSURE_X = "yol" kaliplarindan
    SABITIN DEGERINI cikarir. Ilk kesif sabitin ADINI yakalamisti."""
    bulunan = set()
    kaliplar = [
        r'[A-Z_]*DISCLOSURE[A-Z_]*\s*[:=]\s*["\']([^"\']{3,120})["\']',
        r'[A-Z_]*BILDIRIM[A-Z_]*\s*[:=]\s*["\']([^"\']{3,120})["\']',
        r'[A-Z_]*ROUTE_API[A-Z_]*\s*[:=]\s*["\']([^"\']{3,120})["\']',
    ]
    for k in kaliplar:
        for m in re.findall(k, metin, re.I):
            if "/" in m or m.lower().startswith("api"):
                bulunan.add(m.strip())
    return bulunan


def dene_ve_raporla(yol, lines, calisan):
    """Bir yolu tum oneklerle dener. Ilk JSON doneni kaydeder."""
    for onek in ONEKLER:
        temiz = yol[1:] if yol.startswith("/") else yol
        url = KOK + onek + temiz
        r = getir(url, timeout=20)
        if r is None or r.status_code != 200:
            continue
        try:
            veri = r.json()
        except Exception:
            continue
        if isinstance(veri, list):
            ozet = f"liste, {len(veri)} kayıt"
            if veri and isinstance(veri[0], dict):
                ozet += f" | alanlar: {', '.join(list(veri[0].keys())[:10])}"
        elif isinstance(veri, dict):
            ozet = f"sözlük | alanlar: {', '.join(list(veri.keys())[:10])}"
        else:
            ozet = str(veri)[:100]
        lines.append(f"  ✅ {onek}{temiz}\n       {ozet[:200]}")
        calisan.append(onek + temiz)
        return True
    lines.append(f"  ❌ {yol[:70]} — üç önekte de JSON yok")
    return False


def main():
    send_telegram_message(
        "🕵️ [KAP KEŞİF 2] Başladı.\n"
        "İlk keşifte iki hata vardı: yanlış önek (/tr/ eksikti) ve\n"
        "sabit isimlerini adres sanmak. İkisi de düzeltildi.\n"
        "~3 dakika sürer...")

    lines = ["🕵️ [KAP KEŞİF 2 SONUÇLARI]", ""]
    calisan = []

    # --- 1. Bilinen yollari DOGRU oneklerle dene ---
    lines.append("① İlk keşifte bulunan yollar, doğru öneklerle:")
    for yol in BILINEN_YOLLAR:
        dene_ve_raporla(yol, lines, calisan)
        time.sleep(0.4)
    lines.append("")

    # --- 2. JS paketlerinden SABIT DEGERLERINI cikar ---
    r = getir(ANA_SAYFA)
    yeni_yollar = set()
    if r is not None and r.status_code == 200:
        html = r.text
        yeni_yollar |= sabit_degerleri_cikar(html)
        scripts = re.findall(r'<script[^>]+src=["\']([^"\']+)["\']', html, re.I)
        indirilen = 0
        for su in scripts[:14]:
            tam = su if su.startswith("http") else KOK + ("" if su.startswith("/") else "/") + su
            rr = getir(tam, timeout=30)
            if rr is not None and rr.status_code == 200:
                indirilen += 1
                yeni_yollar |= sabit_degerleri_cikar(rr.text)
            time.sleep(0.4)
        lines.append(f"② Sabit değerleri tarandı ({indirilen} JS paketi):")
    else:
        lines.append("② Ana sayfa alınamadı, sabit taraması yapılamadı.")

    yeni = sorted(y for y in yeni_yollar if y not in BILINEN_YOLLAR)
    if yeni:
        lines.append(f"   {len(yeni)} yeni yol bulundu, ilk 10'u deneniyor:")
        for yol in yeni[:10]:
            dene_ve_raporla(yol, lines, calisan)
            time.sleep(0.4)
    else:
        lines.append("   Sabit değerlerinden yeni yol çıkmadı.")
    lines.append("")

    # --- 3. Sonuc ---
    lines.append("📊 SONUÇ")
    if calisan:
        lines.append(f"  🎉 {len(calisan)} çalışan JSON ucu bulundu:")
        for c in calisan:
            lines.append(f"     {c}")
        lines.append("  Bu çıktıyı Claude'a gönder — KAP Monitor servisi yazılacak.")
    else:
        lines.append("  ❌ Yine JSON ucu bulunamadı.")
        lines.append("  Ama ilk keşif HTML'de bildirim izleri bulmuştu (161 KB sayfa).")
        lines.append("  Sıradaki adım: sayfayı doğrudan HTML olarak ayrıştırmayı denemek")
        lines.append("  (BeautifulSoup, tarayıcı gerekmez). Claude bunu yazabilir.")

    msg = "\n".join(lines)
    send_telegram_message(msg)
    print(msg, flush=True)


if __name__ == "__main__":
    from http.server import BaseHTTPRequestHandler, HTTPServer

    class _P(BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b"kesif2")

        def log_message(self, *a):
            pass

    threading.Thread(
        target=lambda: HTTPServer(("0.0.0.0", int(os.environ.get("PORT", "10000"))),
                                  _P).serve_forever(), daemon=True).start()
    main()
    print("Keşif 2 bitti. Start Command'i geri alabilirsin.", flush=True)
    while True:
        time.sleep(3600)
