"""
kap_kesif.py — KAP'IN GERÇEK VERİ UCUNU BULMA
==============================================
Gemini'nin talebi: KAP bildirimlerini canli olarak cekmek.
Onerdigi yol: Playwright/Selenium ile sayfayi tarayicida acip kazimak.

NEDEN PLAYWRIGHT DEGIL:
Render ucretsiz plani 512 MB RAM veriyor ve orada zaten pandas + numpy +
yfinance + iki bot calisiyor. Playwright ayrica Chromium indirmek zorunda
(~150 MB+) ve kalici disk olmadigi icin bunu HER deploy'da yeniden yapar.
Denesek bellek yetmeyip servis coker. Bu bir tercih degil, olcu meselesi.

BUNUN YERINE — DAHA IYI VE DAHA SAGLAM YOL:
KAP'in sayfasi bir SPA; verisini kendi arka ucundan (XHR/fetch) cekiyor.
O ucu bulursak tarayiciya HIC gerek kalmaz: duz bir HTTP istegiyle temiz
JSON aliriz. Daha hizli, daha az kaynak, sayfa tasarimi degisince bozulmaz.
Ve o adres SAYFANIN KENDI KAYNAK KODUNDA gizli - HTML'de ya da yuklenen
JavaScript paketlerinin icinde gecer.

BU SCRIPT NE YAPAR:
  1. KAP ana sayfasinin HTML'ini indirir.
  2. HTML'de bildirimlerin DOGRUDAN yer alip almadigina bakar (sunucu
     tarafinda render ediliyorsa is zaten bitmis olur - BeautifulSoup yeter).
  3. HTML icindeki <script src=...> paketlerini indirir ve iclerinde
     "/api/", ".json", "disclosure", "bildirim" gecen URL kaliplarini arar.
  4. Bulunan aday uclari TEK TEK dener ve calisani raporlar.

Yani tahmin etmeyi birakip adresi kaynagindan okuyoruz.

NOT: Bu, sayfayi "kazimak" degil - sitenin kendi yayin ucunu bulmak.
KAP yasal olarak kamuya aciklanmasi ZORUNLU bildirimleri "mumkun olan en
genis kitleye eszamanli ve hizli" ulastirmak icin kurulmus bir platform;
bu veriye programatik erisim, ticari bir sitenin ozel icerigini kazimakla
ayni sey degil.

CALISTIRMA: Start Command -> python kap_kesif.py   (~2 dakika)
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

HEADERS = {
    "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                   "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0 Safari/537.36"),
    "Accept": "text/html,application/xhtml+xml,application/json;q=0.9,*/*;q=0.8",
    "Accept-Language": "tr-TR,tr;q=0.9",
}

# Bildirim tablosunun HTML'de olup olmadigini anlamak icin aranan izler
BILDIRIM_IZLERI = ["Özel Durum Açıklaması", "Ozel Durum", "bildirim",
                   "disclosure", "A.Ş.", "Finansal Rapor"]


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
        r = requests.get(url, headers=HEADERS, timeout=timeout)
        return r
    except Exception as e:
        print(f"  getir hatasi {url}: {e}", flush=True)
        return None


def url_adaylari_bul(metin):
    """Metin icinde API benzeri yol kaliplarini arar."""
    kaliplar = [
        r'["\'](/tr/api/[A-Za-z0-9_\-/]{2,60})["\']',
        r'["\'](/api/[A-Za-z0-9_\-/]{2,60})["\']',
        r'["\'](https?://[A-Za-z0-9_\-./]*kap\.org\.tr/[A-Za-z0-9_\-/]*api[A-Za-z0-9_\-/]*)["\']',
        r'["\']([A-Za-z0-9_\-/]*disclosure[A-Za-z0-9_\-/]*)["\']',
        r'["\']([A-Za-z0-9_\-/]*bildirim[A-Za-z0-9_\-/]*)["\']',
    ]
    bulunan = set()
    for k in kaliplar:
        for m in re.findall(k, metin, re.I):
            if 3 < len(m) < 120:
                bulunan.add(m)
    return bulunan


def main():
    send_telegram_message(
        "🕵️ [KAP KEŞİF] Başladı.\n"
        "Sayfanın kaynak kodundan gerçek veri ucu aranıyor.\n"
        "Playwright yerine bu yol deneniyor (Render 512 MB'a sığmaz).\n"
        "~2 dakika sürer...")

    lines = ["🕵️ [KAP KEŞİF SONUÇLARI]", ""]

    # --- 1. Ana sayfa HTML ---
    r = getir(ANA_SAYFA)
    if r is None or r.status_code != 200:
        lines.append(f"❌ Ana sayfa alınamadı (durum: {r.status_code if r else 'bağlantı yok'})")
        send_telegram_message("\n".join(lines))
        return

    html = r.text
    lines.append(f"✅ Ana sayfa indirildi ({len(html)} bayt)")

    # --- 2. Bildirimler HTML'de doğrudan var mı? ---
    izler = [iz for iz in BILDIRIM_IZLERI if iz.lower() in html.lower()]
    if izler:
        lines.append(f"🎯 HTML'de bildirim izleri VAR: {', '.join(izler[:4])}")
        lines.append("   → Sunucu tarafında render ediliyor olabilir;")
        lines.append("     bu durumda BeautifulSoup yeter, tarayıcı gerekmez.")
    else:
        lines.append("ℹ️ HTML'de bildirim metni YOK → sayfa SPA, veri ayrı uçtan geliyor.")
    lines.append("")

    # --- 3. JS paketlerini indir ve içlerinde URL ara ---
    script_urls = re.findall(r'<script[^>]+src=["\']([^"\']+)["\']', html, re.I)
    lines.append(f"📦 {len(script_urls)} JavaScript dosyası bulundu, taranıyor...")

    tum_adaylar = url_adaylari_bul(html)
    taranan = 0
    for su in script_urls[:12]:          # ilk 12 paket yeter, hepsi cok agir
        tam = su if su.startswith("http") else KOK + ("" if su.startswith("/") else "/") + su
        rr = getir(tam, timeout=30)
        if rr is not None and rr.status_code == 200:
            taranan += 1
            tum_adaylar |= url_adaylari_bul(rr.text)
        time.sleep(0.5)
    lines.append(f"   {taranan} paket indirildi.")
    lines.append("")

    # --- 4. Aday uçları dene ---
    adaylar = sorted(a for a in tum_adaylar if "api" in a.lower() or "disclosure" in a.lower())
    if not adaylar:
        lines.append("❌ Kaynak kodda API benzeri adres bulunamadı.")
    else:
        lines.append(f"🔗 {len(adaylar)} aday adres bulundu, ilk 12'si deneniyor:")
        lines.append("")
        calisan = []
        for a in adaylar[:12]:
            tam = a if a.startswith("http") else KOK + ("" if a.startswith("/") else "/") + a
            rr = getir(tam, timeout=20)
            if rr is None:
                lines.append(f"  ❌ {a[:60]} — bağlantı yok")
                continue
            if rr.status_code != 200:
                lines.append(f"  ❌ {a[:60]} — HTTP {rr.status_code}")
                continue
            try:
                veri = rr.json()
                tur = "JSON"
                if isinstance(veri, list):
                    ozet = f"liste, {len(veri)} kayıt"
                    if veri and isinstance(veri[0], dict):
                        ozet += f" | alanlar: {', '.join(list(veri[0].keys())[:8])}"
                elif isinstance(veri, dict):
                    ozet = f"sözlük | alanlar: {', '.join(list(veri.keys())[:8])}"
                else:
                    ozet = str(veri)[:80]
                lines.append(f"  ✅ {a[:60]}\n       {tur}: {ozet[:160]}")
                calisan.append(a)
            except Exception:
                bas = rr.text[:80].replace("\n", " ")
                lines.append(f"  ⚠️ {a[:60]} — HTTP 200 ama JSON değil ({bas[:50]})")
            time.sleep(0.5)

        lines.append("")
        if calisan:
            lines.append(f"🎉 {len(calisan)} çalışan JSON ucu bulundu!")
            lines.append("Bu çıktıyı Claude'a gönder — doğru uca göre KAP Monitor")
            lines.append("servisini yazacak. Tarayıcıya gerek kalmayacak.")
        else:
            lines.append("Aday adresler denendi, JSON dönen çıkmadı.")

    lines.append("")
    lines.append("ℹ️ Bu keşif, sayfayı kazımak değil — sitenin kendi veri ucunu bulmak.")

    msg = "\n".join(lines)
    send_telegram_message(msg)
    print(msg, flush=True)


if __name__ == "__main__":
    from http.server import BaseHTTPRequestHandler, HTTPServer

    class _P(BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b"kesif")

        def log_message(self, *a):
            pass

    threading.Thread(
        target=lambda: HTTPServer(("0.0.0.0", int(os.environ.get("PORT", "10000"))),
                                  _P).serve_forever(), daemon=True).start()
    main()
    print("Keşif bitti. Start Command'i geri alabilirsin.", flush=True)
    while True:
        time.sleep(3600)
