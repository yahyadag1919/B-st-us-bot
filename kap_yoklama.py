"""
kap_yoklama.py — KAP VERİ KAYNAĞI YOKLAMASI (1 dakikalık test)
===============================================================
AMAC: Radar sistemine KAP/haber bileseni eklemeden ONCE, KAP'tan
programatik olarak veri cekilip cekilemedigini kesin olarak ogrenmek.

NEDEN BU SCRIPT VAR:
Claude'un calisma ortaminda internet yok, dolayisiyla hangi adresin
calistigini buradan deneyemiyor. Korlemesine entegrasyon yazmak yerine
once bu yoklama kosuluyor: hangi uc calisiyor, ne donduruyor, tarih
araligi destekliyor mu.

ONEMLI TESPIT (2026-08-08): KAP sitesinde "Son 15 Gun" ve "Diger" tarih
filtreleri var. Yani KAP GECMIS bildirimleri de veriyor. Bu, daha once
"KAP bileseni geriye donuk test edilemez" dedigim degerlendirmeyi
GECERSIZ kilar - eger bu uclardan biri tarih araligi kabul ediyorsa,
hacim patlamalarinin zamanlariyla eslestirip Gemini'nin hipotezini
(haber destekli hareket farkli davranir) kontrol gruplu olarak test
edebiliriz.

CALISTIRMA: Start Command -> python kap_yoklama.py
Sure: ~1 dakika. Sonuc Telegram'a duser.
"""

import os
import json
import time
import threading
import requests

TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID")

# Tarayici gibi gorunmek icin standart basliklar - bu bir kacamak degil,
# sunucularin bot trafigini ayirt etmesi icin normal bir uygulama.
HEADERS = {
    "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                   "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0 Safari/537.36"),
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "tr-TR,tr;q=0.9",
}

# Aday uclar. KAP'in kendi arayuzu /tr/api/... deseni kullaniyor
# (kullanicinin gonderdigi ekranda gorulen URL yapisi).
ADAYLAR = [
    ("Bildirim listesi (api/disclosure)",
     "https://www.kap.org.tr/tr/api/disclosure/list", "GET", None),
    ("Bildirim sorgu (memberDisclosureQuery)",
     "https://www.kap.org.tr/tr/api/memberDisclosureQuery", "GET", None),
    ("Ana sayfa bildirimleri (todayDisclosure)",
     "https://www.kap.org.tr/tr/api/todayDisclosure", "GET", None),
    ("Filtreli sorgu (POST)",
     "https://www.kap.org.tr/tr/api/disclosureQuery", "POST",
     {"fromDate": "2026-06-01", "toDate": "2026-08-08", "disclosureClass": "ODA"}),
    ("Eski arayuz JSON",
     "https://www.kap.org.tr/tr/BildirimSorgu", "GET", None),
    ("RSS denemesi 1", "https://www.kap.org.tr/tr/rss", "GET", None),
    ("RSS denemesi 2", "https://www.kap.org.tr/rss/bildirimler.xml", "GET", None),
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


def dene(ad, url, yontem, govde):
    """Tek bir ucu dener. Ne olursa olsun exception firlatmaz - amac
    hepsini deneyip rapor etmek, ilk hatada durmak degil."""
    sonuc = {"ad": ad, "url": url, "durum": None, "tur": None,
             "boyut": 0, "ornek": "", "hata": None}
    try:
        if yontem == "POST":
            r = requests.post(url, headers=HEADERS, json=govde, timeout=20)
        else:
            r = requests.get(url, headers=HEADERS, timeout=20)
        sonuc["durum"] = r.status_code
        icerik = r.text or ""
        sonuc["boyut"] = len(icerik)

        if r.status_code != 200:
            return sonuc

        # JSON mu?
        try:
            veri = r.json()
            sonuc["tur"] = "JSON"
            if isinstance(veri, list):
                sonuc["ornek"] = f"liste, {len(veri)} kayıt"
                if veri:
                    ilk = veri[0]
                    if isinstance(ilk, dict):
                        sonuc["ornek"] += f" | alanlar: {', '.join(list(ilk.keys())[:8])}"
            elif isinstance(veri, dict):
                sonuc["ornek"] = f"sözlük | alanlar: {', '.join(list(veri.keys())[:8])}"
            return sonuc
        except Exception:
            pass

        # XML/RSS mi?
        bas = icerik[:200].lower()
        if "<rss" in bas or "<feed" in bas or "<?xml" in bas:
            sonuc["tur"] = "XML/RSS"
            sonuc["ornek"] = icerik[:150].replace("\n", " ")
        elif "<html" in bas:
            sonuc["tur"] = "HTML"
            sonuc["ornek"] = "HTML sayfa (JSON değil)"
        else:
            sonuc["tur"] = "bilinmiyor"
            sonuc["ornek"] = icerik[:120].replace("\n", " ")
    except Exception as e:
        sonuc["hata"] = f"{type(e).__name__}: {e}"
    return sonuc


def main():
    send_telegram_message(
        "🔎 [KAP VERİ KAYNAĞI YOKLAMASI] Başladı.\n"
        f"{len(ADAYLAR)} aday adres deneniyor. ~1 dakika sürer.")

    sonuclar = []
    for ad, url, yontem, govde in ADAYLAR:
        s = dene(ad, url, yontem, govde)
        sonuclar.append(s)
        print(f"{ad}: durum={s['durum']} tur={s['tur']} hata={s['hata']}", flush=True)
        time.sleep(1)

    lines = ["🔎 [KAP YOKLAMA SONUÇLARI]", ""]
    calisan = []
    for s in sonuclar:
        if s["hata"]:
            lines.append(f"❌ {s['ad']}\n   hata: {s['hata'][:100]}")
        elif s["durum"] != 200:
            lines.append(f"❌ {s['ad']}\n   HTTP {s['durum']}")
        elif s["tur"] in ("JSON", "XML/RSS"):
            lines.append(f"✅ {s['ad']}\n   {s['tur']} | {s['boyut']} bayt\n   {s['ornek'][:180]}")
            calisan.append(s)
        else:
            lines.append(f"⚠️ {s['ad']}\n   HTTP 200 ama {s['tur']} — kullanılabilir değil")
        lines.append("")

    lines.append("📊 SONUÇ")
    if calisan:
        lines.append(f"  ✅ {len(calisan)} adet kullanılabilir kaynak bulundu.")
        lines.append("  Bu çıktıyı Claude'a gönder — doğru uca göre KAP bileşenini")
        lines.append("  kurup GERİYE DÖNÜK test edebiliriz (Gemini'nin hipotezi ölçülebilir).")
    else:
        lines.append("  ❌ Hiçbir aday uç kullanılabilir veri döndürmedi.")
        lines.append("  Alternatifler:")
        lines.append("   • Borsa haber siteleri RSS (Bloomberg HT, Foreks, Matriks)")
        lines.append("   • Investing.com / Mynet Finans haber akışı")
        lines.append("   • Ücretli finansal haber API'si")
        lines.append("  Bu çıktıyı Claude'a gönder, alternatif kaynak için yoklama yazsın.")

    msg = "\n".join(lines)
    send_telegram_message(msg)
    print(msg, flush=True)


if __name__ == "__main__":
    from http.server import BaseHTTPRequestHandler, HTTPServer

    class _P(BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b"yoklama")

        def log_message(self, *a):
            pass

    threading.Thread(
        target=lambda: HTTPServer(("0.0.0.0", int(os.environ.get("PORT", "10000"))),
                                  _P).serve_forever(), daemon=True).start()
    main()
    print("Yoklama bitti. Start Command'i geri alabilirsin.", flush=True)
    while True:
        time.sleep(3600)
