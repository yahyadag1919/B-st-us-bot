"""
haber_yoklama.py — ALTERNATİF HABER KAYNAĞI YOKLAMASI (1 dakikalık test)
===============================================================
AMAC: Radar sistemine KAP/haber bileseni eklemeden ONCE, KAP'tan
programatik olarak veri cekilip cekilemedigini kesin olarak ogrenmek.

NEDEN BU SCRIPT VAR:
Claude'un calisma ortaminda internet yok, dolayisiyla hangi adresin
calistigini buradan deneyemiyor. Korlemesine entegrasyon yazmak yerine
once bu yoklama kosuluyor: hangi uc calisiyor, ne donduruyor, tarih
araligi destekliyor mu.

ONCEKI ADIM (2026-08-08): kap_yoklama.py kosuldu, 7 aday ucun HEPSI 404
verdi. KAP'in gercek API ucu var (site calisiyor) ama tahminle bulunamadi;
bulmak icin sitenin ag trafigini incelemek gerekiyor - Claude'un ortaminda
internet yok, kullanicinin telefonunda da pratik degil.
Bu yuzden alternatif haber kaynaklarina geciliyor.

DURUST UYARI: Genel finans haber akislari KAP'in yerini TAM tutmaz.
KAP resmi ve zorunlu bildirim kaynagidir; haber siteleri gecikmeli,
eksik ve gurultulu olabilir. Ayrica haberi HISSEYE eslestirmek gerekir
(baslikta sirket adi/kodu aramak) - bu da hata payi getirir.
Yine de Gemini'nin hipotezini test etmek icin bir baslangic saglar.

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
    ("Bloomberg HT ekonomi", "https://www.bloomberght.com/rss", "GET", None),
    ("Bloomberg HT borsa", "https://www.bloomberght.com/borsa/rss", "GET", None),
    ("Mynet Finans", "https://finans.mynet.com/rss/haberler/", "GET", None),
    ("BigPara borsa", "https://bigpara.hurriyet.com.tr/rss/borsa/", "GET", None),
    ("Dünya gazetesi", "https://www.dunya.com/rss?dt=finans", "GET", None),
    ("Ekonomim (eski Dünya)", "https://www.ekonomim.com/rss", "GET", None),
    ("Investing.com TR haber", "https://tr.investing.com/rss/news.rss", "GET", None),
    ("Investing.com TR hisse", "https://tr.investing.com/rss/stock.rss", "GET", None),
    ("Patronlar Dünyası", "https://www.patronlardunyasi.com/rss", "GET", None),
    ("Borsa Gündem", "https://www.borsagundem.com/rss", "GET", None),
    ("AA Ekonomi", "https://www.aa.com.tr/tr/rss/default?cat=ekonomi", "GET", None),
    ("Foreks haber", "https://www.foreks.com/rss", "GET", None),
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
             "boyut": 0, "ornek": "", "hata": None, "adet": 0}
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
            # Asil soru: baslikta hisse kodu/sirket adi var mi? Radar icin
            # haberi HISSEYE eslestirebilmemiz sart, yoksa akis ise yaramaz.
            import re as _re
            basliklar = _re.findall(r"<title[^>]*>(.*?)</title>", icerik,
                                    _re.S | _re.I)[1:4]
            basliklar = [_re.sub(r"<[^>]+>|<!\[CDATA\[|\]\]>", "", b).strip()
                         for b in basliklar]
            sonuc["ornek"] = " || ".join(b[:70] for b in basliklar if b) or "başlık okunamadı"
            sonuc["adet"] = icerik.lower().count("<item")
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
        "📰 [HABER KAYNAĞI YOKLAMASI] Başladı.\n"
        f"{len(ADAYLAR)} aday adres deneniyor. ~1 dakika sürer.")

    sonuclar = []
    for ad, url, yontem, govde in ADAYLAR:
        s = dene(ad, url, yontem, govde)
        sonuclar.append(s)
        print(f"{ad}: durum={s['durum']} tur={s['tur']} hata={s['hata']}", flush=True)
        time.sleep(1)

    lines = ["📰 [HABER KAYNAĞI YOKLAMA SONUÇLARI]", ""]
    calisan = []
    for s in sonuclar:
        if s["hata"]:
            lines.append(f"❌ {s['ad']}\n   hata: {s['hata'][:100]}")
        elif s["durum"] != 200:
            lines.append(f"❌ {s['ad']}\n   HTTP {s['durum']}")
        elif s["tur"] in ("JSON", "XML/RSS"):
            lines.append(f"✅ {s['ad']}\n   {s['tur']} | {s.get('adet', 0)} haber\n"
                         f"   Örnek başlıklar: {s['ornek'][:220]}")
            calisan.append(s)
        else:
            lines.append(f"⚠️ {s['ad']}\n   HTTP 200 ama {s['tur']} — kullanılabilir değil")
        lines.append("")

    lines.append("📊 SONUÇ")
    if calisan:
        lines.append(f"  ✅ {len(calisan)} kaynak çalışıyor.")
        lines.append("  Bu çıktıyı Claude'a gönder. Örnek başlıklara bakıp haberin")
        lines.append("  hisseye eşleştirilip eşleştirilemeyeceğine karar verecek —")
        lines.append("  başlıkta şirket adı/kodu geçmiyorsa akış radar için işe yaramaz.")
    else:
        lines.append("  ❌ Hiçbir haber kaynağı da çalışmadı.")
        lines.append("  Bu durumda dürüst seçenek: radarı KAP/haber bileşeni OLMADAN,")
        lines.append("  sadece hacim+kırılım+ayrışma ile kurmak — ama o kombinasyonun")
        lines.append("  ölçülen sonucu zaten negatifti. Yani kurmaya değmeyebilir.")
        lines.append("  Alternatif: ücretli bir finansal haber API'si.")

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
