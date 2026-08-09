"""
kap_pazartesi_testi.py — SEANS SAATİNDE ÇALIŞTIRILACAK ÖLÇÜM
=============================================================
NEDEN BU SCRIPT VAR — ÖNCEKI ÖLÇÜMÜN HATASI:
kap_google_olcum.py hafta sonu kosuldu ve "en taze bildirim 13 saat once"
sonucunu verip "tazelik yetersiz" hukmu verdi. BU HUKUM GECERSIZ.
Kullanicinin tespiti dogru: KAP bildirimleri borsa CALISIRKEN gelir.
Hafta sonu piyasa kapali oldugu icin en son bildirimler cuma gunune ait.
Yani o olcum Google'in gecikmesini degil, PIYASANIN KAPALI OLMASINI olctu.

Dogru olcum HAFTA ICI, SEANS SAATLERINDE yapilmali (BIST 10:00-18:00).

BU SCRIPT IKI SEYI BIRDEN YAPAR (tek kosuda cevap):

  ① GOOGLE NEWS'IN GERCEK GECIKMESI
     Ayni genis sorgular, ama bu sefer seans icinde. Bildirimlerin yasi
     dakika cinsinden olculur. Radar 15 dakikalik pencere kullaniyor:
       < 30 dk  -> kanal radar icin kullanilabilir
       30-180dk -> pencere genisletilmeli ya da "gunluk baglam" olur
       > 180 dk -> anlik dogrulama icin kullanilamaz

  ② YAYINCILARIN KENDI RSS'I — muhtemelen daha iyisi
     Onceki olcum sunu gosterdi: KAP bildirimlerini Yeni Safak (216 adet)
     ve GZT (66 adet) otomatik ve SABIT formatta yayinliyor. Google'in
     gecikmesi GOOGLE'IN indeksleme gecikmesidir - yayincinin degil.
     Dogrudan onlarin RSS'ine gidersek arada Google olmaz, bildirim
     dakikalar icinde elimize gecebilir.
     Bu kanal calisirsa Google'a hic gerek kalmaz.

NE ZAMAN CALISTIRILMALI: Hafta ici, tercihen 11:00-17:00 arasi (BIST
seansi surerken ve bildirim akisi yogunken).

CALISTIRMA: Start Command -> python kap_pazartesi_testi.py   (~2 dakika)
"""

import os
import re
import time
import threading
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime

import requests

TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID")

HEADERS = {
    "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                   "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0 Safari/537.36"),
    "Accept": "text/xml, application/rss+xml, */*;q=0.8",
    "Accept-Language": "tr-TR,tr;q=0.9",
}

GOOGLE_SORGULARI = [
    ("KAP Özel Durum", "KAP+%22%C3%96zel+Durum+A%C3%A7%C4%B1klamas%C4%B1%22"),
    ("KAP *** kalıbı", "%22KAP+***%22"),
]

# Yayincilarin kendi RSS adaylari - once bunlar denenir, calisirsa
# Google'a gerek kalmaz
YAYINCI_ADAYLARI = [
    ("Yeni Şafak ekonomi", "https://www.yenisafak.com/rss?xml=ekonomi"),
    ("Yeni Şafak genel", "https://www.yenisafak.com/rss"),
    ("Yeni Şafak borsa", "https://www.yenisafak.com/rss?xml=borsa"),
    ("GZT ekonomi", "https://www.gzt.com/rss/ekonomi"),
    ("GZT genel", "https://www.gzt.com/rss"),
    ("GZT finans", "https://www.gzt.com/rss/finans"),
]

KOD_KALIBI = re.compile(r"KAP\s*\*\*\*.*?\*\*\*\s*([A-Z]{4,6})\s*\*\*\*", re.S)


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


def temizle(s):
    s = re.sub(r"<!\[CDATA\[|\]\]>", "", s)
    s = re.sub(r"<[^>]+>", "", s)
    return re.sub(r"\s+", " ", s).strip()


def item_ayikla(xml):
    """RSS'ten (baslik, tarih) ciftleri."""
    cikti = []
    for o in re.findall(r"<item>(.*?)</item>", xml, re.S | re.I):
        b = re.search(r"<title[^>]*>(.*?)</title>", o, re.S | re.I)
        t = re.search(r"<pubDate[^>]*>(.*?)</pubDate>", o, re.S | re.I)
        if not b:
            continue
        tarih = None
        if t:
            try:
                tarih = parsedate_to_datetime(temizle(t.group(1)))
                if tarih.tzinfo is None:
                    tarih = tarih.replace(tzinfo=timezone.utc)
            except Exception:
                pass
        cikti.append((temizle(b.group(1)), tarih))
    return cikti


def yas_dk(t):
    return (datetime.now(timezone.utc) - t).total_seconds() / 60


def analiz_et(haberler, lines, girinti="     "):
    """KAP kalibina uyanlari sayar, kod cikarir, tazeligi olcer."""
    kap = [(b, t) for b, t in haberler if KOD_KALIBI.search(b)]
    kodlar = {KOD_KALIBI.search(b).group(1) for b, _ in kap}
    lines.append(f"{girinti}toplam {len(haberler)} haber | KAP kalıbına uyan: {len(kap)}")
    if kodlar:
        lines.append(f"{girinti}farklı hisse: {len(kodlar)} → {', '.join(sorted(kodlar)[:10])}")
    yaslar = [yas_dk(t) for _, t in kap if t is not None]
    if not yaslar:
        lines.append(f"{girinti}⚠️ tarih bilgisi yok")
        return None
    en_taze = min(yaslar)
    if en_taze < 30:
        lines.append(f"{girinti}⏱️ EN TAZE: {en_taze:.0f} dakika önce ✅")
    elif en_taze < 180:
        lines.append(f"{girinti}⏱️ EN TAZE: {en_taze:.0f} dakika önce ⚠️")
    else:
        lines.append(f"{girinti}⏱️ EN TAZE: {en_taze / 60:.1f} saat önce ❌")
    son15 = sum(1 for y in yaslar if y <= 15)
    son60 = sum(1 for y in yaslar if y <= 60)
    lines.append(f"{girinti}son 15 dk: {son15} | son 60 dk: {son60} bildirim")
    if kap:
        lines.append(f"{girinti}örnek: {kap[0][0][:95]}")
    return en_taze


def main():
    ist_saat = datetime.now().strftime("%A %H:%M")
    send_telegram_message(
        "📅 [SEANS İÇİ KAP ÖLÇÜMÜ] Başladı.\n"
        f"Çalışma zamanı: {ist_saat}\n"
        "Hafta sonu ölçümü geçersizdi (piyasa kapalıyken bildirim gelmez).\n"
        "Bu sefer yayıncı RSS'leri de deneniyor.\n"
        "~2 dakika...")

    lines = ["📅 [SEANS İÇİ KAP ÖLÇÜM SONUÇLARI]",
             f"Çalışma zamanı: {ist_saat}", ""]

    # --- ① Yayinci RSS'leri (once bunlar - calisirsa Google'a gerek yok) ---
    lines.append("① YAYINCI RSS'LERİ (Google'sız, en taze olması beklenen)")
    en_iyi_yayinci = None
    for ad, url in YAYINCI_ADAYLARI:
        try:
            r = requests.get(url, headers=HEADERS, timeout=25)
        except Exception as e:
            lines.append(f"  ❌ {ad} — {type(e).__name__}")
            time.sleep(0.5)
            continue
        if r.status_code != 200:
            lines.append(f"  ❌ {ad} — HTTP {r.status_code}")
            time.sleep(0.5)
            continue
        haberler = item_ayikla(r.text)
        if not haberler:
            lines.append(f"  ⚠️ {ad} — HTTP 200 ama haber yok")
            time.sleep(0.5)
            continue
        lines.append(f"  ✅ {ad}")
        taze = analiz_et(haberler, lines)
        if taze is not None and (en_iyi_yayinci is None or taze < en_iyi_yayinci):
            en_iyi_yayinci = taze
        time.sleep(0.8)
    lines.append("")

    # --- ② Google News (kiyas) ---
    lines.append("② GOOGLE NEWS (kıyas için)")
    en_iyi_google = None
    for ad, q in GOOGLE_SORGULARI:
        url = f"https://news.google.com/rss/search?q={q}&hl=tr&gl=TR&ceid=TR:tr"
        try:
            r = requests.get(url, headers=HEADERS, timeout=25)
        except Exception as e:
            lines.append(f"  ❌ {ad} — {type(e).__name__}")
            continue
        if r.status_code != 200:
            lines.append(f"  ❌ {ad} — HTTP {r.status_code}")
            continue
        lines.append(f"  ✅ {ad}")
        taze = analiz_et(item_ayikla(r.text), lines)
        if taze is not None and (en_iyi_google is None or taze < en_iyi_google):
            en_iyi_google = taze
        time.sleep(1.0)
    lines.append("")

    # --- Karar ---
    lines.append("📊 DEĞERLENDİRME")
    adaylar = [("Yayıncı RSS", en_iyi_yayinci), ("Google News", en_iyi_google)]
    gecerli = [(a, t) for a, t in adaylar if t is not None]
    if not gecerli:
        lines.append("  ❌ Hiçbir kanaldan tarihli KAP bildirimi alınamadı.")
    else:
        kazanan, sure = min(gecerli, key=lambda x: x[1])
        lines.append(f"  En taze kanal: {kazanan} ({sure:.0f} dakika)")
        if sure < 30:
            lines.append("  ✅ RADAR İÇİN UYGUN — 15 dakikalık doğrulama yapılabilir.")
            lines.append("     KAP Monitor bu kanaldan kurulabilir.")
        elif sure < 180:
            lines.append("  ⚠️ SINIRDA — 15 dk penceresi dar kalır. Ya pencere")
            lines.append("     genişletilir (örn. 60 dk) ya da bu kanal 'anlık teyit'")
            lines.append("     yerine 'gün içi bağlam' olarak kullanılır.")
        else:
            lines.append("  ❌ ANLIK DOĞRULAMA İÇİN YETERSİZ.")
            lines.append("     Günlük özet için değerli olabilir ama Gemini'nin")
            lines.append("     tasarladığı 15 dakikalık teyidi veremez.")

    lines.append("")
    lines.append("ℹ️ Bu ölçüm seans içinde yapıldıysa geçerlidir. Piyasa kapalıyken")
    lines.append("   çalıştırılırsa sonuç yine yanıltıcı olur.")

    msg = "\n".join(lines)
    send_telegram_message(msg)
    print(msg, flush=True)


if __name__ == "__main__":
    from http.server import BaseHTTPRequestHandler, HTTPServer

    class _P(BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b"seans olcumu")

        def log_message(self, *a):
            pass

    threading.Thread(
        target=lambda: HTTPServer(("0.0.0.0", int(os.environ.get("PORT", "10000"))),
                                  _P).serve_forever(), daemon=True).start()
    main()
    print("Ölçüm bitti. Start Command'i geri alabilirsin.", flush=True)
    while True:
        time.sleep(3600)
