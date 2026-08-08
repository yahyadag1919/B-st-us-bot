"""
kap_ucuncu_yol.py — ÜÇ ÜCRETSİZ KANAL YOKLAMASI
================================================
Gemini onayi (2026-08-08): web tarafi kapandiktan sonra kalan uc ucretsiz
yolu denemek. Dort keşif turu sonunda kesinlesen durum: KAP'in web sayfasi
bir SPA, veri JavaScript calistiktan sonra doluyor, gomulu JSON yok, ve
tahmin edilen 27 API ucunun hicbiri calismıyor.

BU SCRIPT UC KANALI DENIYOR:

  1) KAP MOBIL API — KAP'in Android uygulamasi var. Mobil uygulamalar
     genelde web'den FARKLI ve DAHA SADE bir API kullanir, cunku telefonda
     JavaScript render etmek pahalidir. Alt alan adlari ve tipik mobil
     uc kaliplari deneniyor. Bence en umut verici kanal bu.

  2) GOOGLE NEWS RSS ARAMASI — Google News'in arama sonuclarini RSS olarak
     veren bir ucu var. Genel haber akislarindan KRITIK farki: HISSE
     BAZINDA sorgulanabiliyor ("ASELSAN KAP" gibi). Radar icin gereken tam
     bu. Scraping degil, RSS.
     UYARI (sonucu okurken akilda tutulmali): Google News haberleri
     YAYINCIDAN sonra indeksler, yani KAP'a gore GECIKMELI olur. Ayrica
     bir bildirimin habere donusmesi gerekir - kucuk sirketlerin rutin
     bildirimleri hic habere donusmeyebilir. Yani bu kanal calissa bile
     KAP'in TAM karsiligi degil, ZAYIF bir vekili olur.

  3) MKK / BORSA ISTANBUL — KAP'i isleten kurum MKK. Kendi veri servisleri
     olabilir; BIST tarafi da denenmemisti.

CALISTIRMA: Start Command -> python kap_ucuncu_yol.py   (~3 dakika)
"""

import os
import re
import time
import threading
import requests

TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID")

WEB_HEADERS = {
    "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                   "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0 Safari/537.36"),
    "Accept": "application/json, text/xml, */*;q=0.8",
    "Accept-Language": "tr-TR,tr;q=0.9",
}

# Mobil uygulama gibi gorunen basliklar - bazi sunucular mobil istemciye
# farkli (ve daha sade) yanit doner
MOBIL_HEADERS = {
    "User-Agent": "okhttp/4.12.0",
    "Accept": "application/json",
    "Accept-Language": "tr-TR",
}

# --- 1) KAP mobil aday uclari ---
MOBIL_ADAYLAR = [
    "https://mobile.kap.org.tr/tr/api/disclosure",
    "https://mobile.kap.org.tr/api/disclosure",
    "https://api.kap.org.tr/disclosure",
    "https://api.kap.org.tr/tr/api/disclosure",
    "https://m.kap.org.tr/tr/api/disclosure",
    "https://www.kap.org.tr/tr/api/mobile/disclosure",
    "https://www.kap.org.tr/api/mobile/disclosureList",
    "https://mobil.kap.org.tr/api/bildirim",
]

# --- 2) Google News RSS, hisse bazli ---
GOOGLE_SORGULARI = [
    ("ASELSAN KAP", "https://news.google.com/rss/search?q=ASELSAN+KAP&hl=tr&gl=TR&ceid=TR:tr"),
    ("THYAO bildirim", "https://news.google.com/rss/search?q=THYAO+bildirim&hl=tr&gl=TR&ceid=TR:tr"),
    ("Türk Hava Yolları", "https://news.google.com/rss/search?q=%22T%C3%BCrk+Hava+Yollar%C4%B1%22&hl=tr&gl=TR&ceid=TR:tr"),
]

# --- 3) MKK / BIST ---
KURUM_ADAYLAR = [
    "https://www.mkk.com.tr/tr/rss",
    "https://www.mkk.com.tr/api/disclosure",
    "https://www.borsaistanbul.com/tr/rss",
    "https://borsaistanbul.com/tr/api/duyurular",
    "https://www.kap.org.tr/tr/api/notification/list",
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


def dene(url, headers, timeout=20):
    try:
        return requests.get(url, headers=headers, timeout=timeout)
    except Exception as e:
        return e


def json_ozet(veri):
    if isinstance(veri, list):
        s = f"liste, {len(veri)} kayıt"
        if veri and isinstance(veri[0], dict):
            s += f" | alanlar: {', '.join(list(veri[0].keys())[:10])}"
        return s
    if isinstance(veri, dict):
        return f"sözlük | alanlar: {', '.join(list(veri.keys())[:10])}"
    return str(veri)[:100]


def rss_ozet(metin):
    """RSS'ten baslik + tarih ornekleri cikarir. Radar icin kritik olan
    ikisi de var mi, ona bakiyoruz."""
    basliklar = re.findall(r"<title[^>]*>(.*?)</title>", metin, re.S | re.I)
    tarihler = re.findall(r"<pubDate[^>]*>(.*?)</pubDate>", metin, re.S | re.I)
    temiz = []
    for b in basliklar[1:4]:
        b = re.sub(r"<!\[CDATA\[|\]\]>|<[^>]+>", "", b).strip()
        if b:
            temiz.append(b[:90])
    adet = metin.lower().count("<item")
    return adet, temiz, (tarihler[0].strip()[:31] if tarihler else None)


def kanal_calistir(baslik, adaylar, headers, lines, calisan, rss_modu=False):
    lines.append(baslik)
    for ad_url in adaylar:
        ad, url = ad_url if isinstance(ad_url, tuple) else (ad_url, ad_url)
        r = dene(url, headers)
        etiket = ad if isinstance(ad_url, tuple) else url.replace("https://", "")[:52]

        if isinstance(r, Exception):
            lines.append(f"  ❌ {etiket} — {type(r).__name__}")
        elif r.status_code != 200:
            lines.append(f"  ❌ {etiket} — HTTP {r.status_code}")
        else:
            icerik = r.text or ""
            bas = icerik[:200].lower()
            if rss_modu or "<rss" in bas or "<?xml" in bas or "<feed" in bas:
                adet, ornekler, tarih = rss_ozet(icerik)
                if adet:
                    lines.append(f"  ✅ {etiket} — RSS, {adet} haber"
                                 + (f" | tarih: {tarih}" if tarih else " | ⚠️ tarih yok"))
                    for o in ornekler:
                        lines.append(f"       • {o}")
                    calisan.append(etiket)
                else:
                    lines.append(f"  ⚠️ {etiket} — XML ama haber yok")
            else:
                try:
                    lines.append(f"  ✅ {etiket} — JSON: {json_ozet(r.json())[:170]}")
                    calisan.append(etiket)
                except Exception:
                    tur = "HTML" if "<html" in bas else "bilinmiyor"
                    lines.append(f"  ⚠️ {etiket} — HTTP 200 ama {tur}")
        time.sleep(0.6)
    lines.append("")


def main():
    send_telegram_message(
        "🛰️ [ÜÇ KANAL YOKLAMASI] Başladı.\n"
        "1) KAP Mobil API  2) Google News hisse RSS  3) MKK/BIST\n"
        "~3 dakika sürer...")

    lines = ["🛰️ [ÜÇ KANAL YOKLAMA SONUÇLARI]", ""]
    calisan = []

    kanal_calistir("① KAP MOBİL API (mobil istemci başlıklarıyla)",
                   MOBIL_ADAYLAR, MOBIL_HEADERS, lines, calisan)

    kanal_calistir("② GOOGLE NEWS — HİSSE BAZLI RSS",
                   GOOGLE_SORGULARI, WEB_HEADERS, lines, calisan, rss_modu=True)

    kanal_calistir("③ MKK / BORSA İSTANBUL",
                   KURUM_ADAYLAR, WEB_HEADERS, lines, calisan)

    lines.append("📊 SONUÇ")
    if calisan:
        lines.append(f"  ✅ {len(calisan)} kanal veri döndürdü:")
        for c in calisan:
            lines.append(f"     {c}")
        lines.append("")
        lines.append("  Bu çıktıyı Claude'a gönder. Bakılacak iki şey:")
        lines.append("   • Başlıklar HİSSEYE özgü mü (şirket adı geçiyor mu)?")
        lines.append("   • ZAMAN bilgisi var mı (radar 15 dakikalık pencere kullanıyor)?")
    else:
        lines.append("  ❌ Üç kanal da sonuç vermedi.")
        lines.append("  Ücretsiz web yolları tükendi; sıradaki konu VPS veya ücretli servis.")

    lines.append("")
    lines.append("ℹ️ Google News çalışsa bile KAP'ın TAM karşılığı değildir:")
    lines.append("   haberler KAP'tan gecikmeli indekslenir ve her bildirim")
    lines.append("   habere dönüşmez. Zayıf bir vekil olur — bunu bilerek kullanalım.")

    msg = "\n".join(lines)
    send_telegram_message(msg)
    print(msg, flush=True)


if __name__ == "__main__":
    from http.server import BaseHTTPRequestHandler, HTTPServer

    class _P(BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b"uc kanal")

        def log_message(self, *a):
            pass

    threading.Thread(
        target=lambda: HTTPServer(("0.0.0.0", int(os.environ.get("PORT", "10000"))),
                                  _P).serve_forever(), daemon=True).start()
    main()
    print("Yoklama bitti. Start Command'i geri alabilirsin.", flush=True)
    while True:
        time.sleep(3600)
