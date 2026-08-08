"""
kap_google_olcum.py — GOOGLE NEWS KANALI: TAZELİK VE KAPSAM ÖLÇÜMÜ
===================================================================
BULGU (2026-08-08): Google News RSS calisiyor ve bazi yayinlar (Yeni Safak,
GZT) KAP bildirimlerini OTOMATIK ve BIREBIR yayinliyor:

    "KAP *** TÜRK HAVA YOLLARI A.O. *** THYAO *** Özel Durum Açıklaması (Genel)"

Yani hisse kodu, sirket adi ve bildirim tipi bir arada, ustelik <pubDate>
ile. Radarin ihtiyaci olan uc alanin ucu de var.

AMA RADARA BAGLAMADAN ONCE IKI SEY OLCULMELI - ikisi de tasarimi tek
basina oldurebilir:

  1) TAZELIK. Radar 15 DAKIKALIK pencere kullaniyor ("son 15 dakikada bu
     hisse icin bildirim dustu mu?"). Google News bildirimleri 3 saat
     gecikmeyle indeksliyorsa bu tasarim calismaz. Bu script en yeni
     kaydin YASINI dakika cinsinden olcuyor - asil karar verici sayi bu.

  2) SORGU HACMI. Hisse basina sorgu yapilirsa 100 hisse x 15 dakikada bir
     = saatte 400 istek. Google bunu engeller. Cozum: hisse basina degil,
     TARAMA BASINA TEK GENIS SORGU yapip sonuclari yerelde eslestirmek.
     Bu script genis sorgularin kac FARKLI hisseyi kapsadigini olcuyor.
     Genis sorgu 30-40 hisse getiriyorsa tasarim ayakta kalir.

Ayrica hangi yayinlarin KAP'i otomatik yayinladigi da raporlaniyor -
ileride dogrudan o yayinin kendi RSS'ini kullanmak daha temiz olabilir.

CALISTIRMA: Start Command -> python kap_google_olcum.py   (~2 dakika)
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

GN = "https://news.google.com/rss/search?q={}&hl=tr&gl=TR&ceid=TR:tr"

# Genis sorgular: tek istekle COK hisseyi kapsamayi hedefliyor
GENIS_SORGULAR = [
    ('KAP Özel Durum Açıklaması', 'KAP+%22%C3%96zel+Durum+A%C3%A7%C4%B1klamas%C4%B1%22'),
    ('KAP bildirim (genel)', 'KAP+bildirim+borsa'),
    ('KAP *** kalıbı', '%22KAP+***%22'),
    ('KAP Finansal Rapor', 'KAP+%22Finansal+Rapor%22'),
]

# Karsilastirma icin tek hisse sorgusu (hacim sorununu gostermek adina)
TEKIL_SORGU = ('Tek hisse: ASELSAN', 'ASELSAN+KAP')

# "KAP *** SIRKET *** KOD *** TIP" kalibindan hisse kodunu cikarir
KOD_KALIBI = re.compile(r"KAP\s*\*\*\*.*?\*\*\*\s*([A-Z]{4,6})\s*\*\*\*", re.S)
# Yedek: baslikta gecen 4-6 harfli buyuk harf blogu
YEDEK_KOD = re.compile(r"\b([A-Z]{4,6})\b")


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


def item_ayikla(xml):
    """RSS'ten (baslik, tarih, kaynak) uclulerini cikarir."""
    out = []
    for blok in re.findall(r"<item>(.*?)</item>", xml, re.S | re.I):
        b = re.search(r"<title[^>]*>(.*?)</title>", blok, re.S | re.I)
        t = re.search(r"<pubDate[^>]*>(.*?)</pubDate>", blok, re.S | re.I)
        k = re.search(r"<source[^>]*>(.*?)</source>", blok, re.S | re.I)
        if not b:
            continue
        baslik = re.sub(r"<!\[CDATA\[|\]\]>|<[^>]+>", "", b.group(1)).strip()
        tarih = None
        if t:
            try:
                tarih = parsedate_to_datetime(t.group(1).strip())
            except Exception:
                tarih = None
        kaynak = re.sub(r"<[^>]+>", "", k.group(1)).strip() if k else ""
        out.append((baslik, tarih, kaynak))
    return out


def yas_dakika(tarih):
    if tarih is None:
        return None
    if tarih.tzinfo is None:
        tarih = tarih.replace(tzinfo=timezone.utc)
    return (datetime.now(timezone.utc) - tarih).total_seconds() / 60


def sorgu_calistir(ad, q, lines):
    url = GN.format(q)
    try:
        r = requests.get(url, headers=HEADERS, timeout=25)
    except Exception as e:
        lines.append(f"  ❌ {ad} — {type(e).__name__}")
        return None
    if r.status_code != 200:
        lines.append(f"  ❌ {ad} — HTTP {r.status_code}")
        return None

    items = item_ayikla(r.text)
    if not items:
        lines.append(f"  ⚠️ {ad} — haber yok")
        return None

    # KAP kalibina uyanlari ve hisse kodlarini cikar
    kap_items, kodlar, kaynaklar = [], set(), {}
    for baslik, tarih, kaynak in items:
        m = KOD_KALIBI.search(baslik)
        if m:
            kap_items.append((baslik, tarih, kaynak))
            kodlar.add(m.group(1))
            if kaynak:
                kaynaklar[kaynak] = kaynaklar.get(kaynak, 0) + 1

    yaslar = [yas_dakika(t) for _, t, _ in kap_items if t is not None]
    en_taze = min(yaslar) if yaslar else None

    lines.append(f"  ✅ {ad}")
    lines.append(f"       toplam {len(items)} haber | KAP kalıbına uyan: {len(kap_items)}")
    lines.append(f"       farklı hisse kodu: {len(kodlar)}"
                 + (f" → {', '.join(sorted(kodlar)[:12])}" if kodlar else ""))
    if en_taze is not None:
        if en_taze < 60:
            lines.append(f"       ⏱️ EN TAZE: {en_taze:.0f} dakika önce ✅")
        elif en_taze < 240:
            lines.append(f"       ⏱️ EN TAZE: {en_taze / 60:.1f} saat önce ⚠️")
        else:
            lines.append(f"       ⏱️ EN TAZE: {en_taze / 60:.1f} saat önce ❌")
    else:
        lines.append("       ⏱️ tarih bilgisi çıkarılamadı")
    if kaynaklar:
        en_cok = sorted(kaynaklar.items(), key=lambda x: -x[1])[:3]
        lines.append("       yayınlar: " + ", ".join(f"{k} ({v})" for k, v in en_cok))
    if kap_items:
        lines.append(f"       örnek: {kap_items[0][0][:110]}")
    return {"kap": len(kap_items), "kodlar": kodlar, "en_taze": en_taze,
            "kaynaklar": kaynaklar}


def main():
    send_telegram_message(
        "📐 [GOOGLE NEWS ÖLÇÜMÜ] Başladı.\n"
        "İki soru ölçülüyor:\n"
        "1) Bildirimler ne kadar TAZE geliyor? (radar 15 dk pencere kullanıyor)\n"
        "2) Tek geniş sorgu kaç hisseyi kapsıyor? (hisse başına sorgu Google'ı zorlar)\n"
        "~2 dakika...")

    lines = ["📐 [GOOGLE NEWS ÖLÇÜM SONUÇLARI]", "",
             "① GENİŞ SORGULAR (tarama başına tek istek hedefi)"]

    sonuclar = []
    tum_kodlar = set()
    tum_kaynaklar = {}
    for ad, q in GENIS_SORGULAR:
        s = sorgu_calistir(ad, q, lines)
        if s:
            sonuclar.append(s)
            tum_kodlar |= s["kodlar"]
            for k, v in s["kaynaklar"].items():
                tum_kaynaklar[k] = tum_kaynaklar.get(k, 0) + v
        time.sleep(1.5)

    lines.append("")
    lines.append("② KARŞILAŞTIRMA — tek hisse sorgusu")
    sorgu_calistir(*TEKIL_SORGU, lines)

    lines.append("")
    lines.append("📊 DEĞERLENDİRME")
    lines.append(f"  Geniş sorgular toplam {len(tum_kodlar)} farklı hisse kapsadı.")

    tazeler = [s["en_taze"] for s in sonuclar if s["en_taze"] is not None]
    en_iyi = min(tazeler) if tazeler else None

    if en_iyi is None:
        lines.append("  ❌ Tazelik ölçülemedi — tarih bilgisi yok.")
    elif en_iyi < 60:
        lines.append(f"  ✅ TAZELİK UYGUN: en yeni bildirim {en_iyi:.0f} dakika önce.")
        lines.append("     15 dakikalık pencere biraz dar kalabilir ama 30-60 dk'lık")
        lines.append("     bir pencereyle radar çalışabilir.")
    elif en_iyi < 240:
        lines.append(f"  ⚠️ TAZELİK SINIRDA: en yeni bildirim {en_iyi / 60:.1f} saat önce.")
        lines.append("     Radarın 15 dakikalık penceresi bu gecikmeyle çalışmaz;")
        lines.append("     pencereyi saatlere çıkarmak gerekir — bu da 'hareketin")
        lines.append("     başında yakalama' fikrini zayıflatır.")
    else:
        lines.append(f"  ❌ TAZELİK YETERSİZ: en yeni bildirim {en_iyi / 60:.1f} saat önce.")
        lines.append("     Bu gecikmeyle KAP doğrulaması gün içi radara katkı sağlamaz.")

    if len(tum_kodlar) >= 20:
        lines.append(f"  ✅ KAPSAM UYGUN: tek sorgu setiyle {len(tum_kodlar)} hisse görülüyor,")
        lines.append("     hisse başına sorgu yapmaya gerek yok.")
    elif tum_kodlar:
        lines.append(f"  ⚠️ KAPSAM DAR: sadece {len(tum_kodlar)} hisse. Hisse başına sorgu")
        lines.append("     gerekebilir; bu da Google'ın hız limitine takılma riski demek.")
    else:
        lines.append("  ❌ Geniş sorgulardan hisse kodu çıkarılamadı.")

    if tum_kaynaklar:
        lines.append("")
        lines.append("  📰 KAP'ı otomatik yayınlayan kaynaklar:")
        for k, v in sorted(tum_kaynaklar.items(), key=lambda x: -x[1])[:5]:
            lines.append(f"     {k} ({v} bildirim)")
        lines.append("     → Bunların kendi RSS'i varsa Google'a hiç gerek kalmaz,")
        lines.append("       daha taze ve daha güvenilir olur.")

    msg = "\n".join(lines)
    send_telegram_message(msg)
    print(msg, flush=True)


if __name__ == "__main__":
    from http.server import BaseHTTPRequestHandler, HTTPServer

    class _P(BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b"olcum")

        def log_message(self, *a):
            pass

    threading.Thread(
        target=lambda: HTTPServer(("0.0.0.0", int(os.environ.get("PORT", "10000"))),
                                  _P).serve_forever(), daemon=True).start()
    main()
    print("Ölçüm bitti. Start Command'i geri alabilirsin.", flush=True)
    while True:
        time.sleep(3600)
