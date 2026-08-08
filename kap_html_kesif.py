"""
kap_html_kesif.py — HTML İÇİNDEKİ VERİYİ BULMA (üçüncü ve son keşif)
=====================================================================
DURUM: Iki API turu da bos cikti (27 yol, 3 onek, sabit degerleri dahil).
Ama ilk kesif su bilgiyi verdi: ana sayfa 161 KB ve icinde "Özel Durum
Açıklaması", "A.Ş." geciyor. Yani VERI SAYFANIN ICINDE.

BU SCRIPT NEDEN KORLEMESINE PARSER YAZMIYOR:
Nerede oldugunu bilmeden ayristirici yazmak tahmindir. Once BAKIYORUZ.
Iki ihtimali ayri ayri kontrol ediyor:

  A) GOMULU JSON — SPA'lar baslangic verisini genelde sayfaya JSON olarak
     gomer: `window.__NUXT__`, `__NEXT_DATA__`, `<script type="application/json">`
     gibi. Buradan cikarsa is biter: temiz veri, ayristirma derdi yok,
     tasarim degisikliginden etkilenmez. EN IYI SENARYO BU.

  B) HTML TABLOSU — veri dogrudan HTML etiketlerinde render edilmisse,
     "Özel Durum" gecen yerlerin ETRAFINDAKI yapiyi (etiket adi, class
     ismi, komsu hucreler) ornekleriyle raporluyor. Bu bilgiyle dogru
     parser yazilabilir.

Cikti ornekleri icerecek; ona bakip gercek KAP Monitor'u yazacagim.
BeautifulSoup KULLANILMIYOR - requirements.txt'de yok ve sirf kesif icin
bagimlilik eklemeye gerek yok. Duz regex yeterli.

CALISTIRMA: Start Command -> python kap_html_kesif.py   (~1 dakika)
"""

import os
import re
import json
import time
import threading
import requests

TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID")

ANA_SAYFA = "https://www.kap.org.tr/tr/"
HEADERS = {
    "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                   "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0 Safari/537.36"),
    "Accept": "text/html,application/xhtml+xml,*/*;q=0.8",
    "Accept-Language": "tr-TR,tr;q=0.9",
}

ANAHTARLAR = ["Özel Durum", "Ozel Durum", "Finansal Rapor", "A.Ş."]


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
    """HTML etiketlerini ve fazla boslugu at, kisa ozet birak."""
    s = re.sub(r"<[^>]+>", " ", s)
    s = re.sub(r"\s+", " ", s)
    return s.strip()


def gomulu_json_ara(html, lines):
    """SPA'larin sayfaya gomdugu baslangic verisini arar."""
    bulundu = False
    kaliplar = [
        (r'window\.__NUXT__\s*=\s*(\{.*?\});?\s*</script>', "window.__NUXT__"),
        (r'id=["\']__NEXT_DATA__["\'][^>]*>(\{.*?\})</script>', "__NEXT_DATA__"),
        (r'<script[^>]+type=["\']application/json["\'][^>]*>(.*?)</script>', "application/json"),
        (r'window\.__INITIAL_STATE__\s*=\s*(\{.*?\});?\s*</script>', "__INITIAL_STATE__"),
        (r'window\.__DATA__\s*=\s*(\{.*?\});?\s*</script>', "window.__DATA__"),
    ]
    for kalip, ad in kaliplar:
        for m in re.findall(kalip, html, re.S | re.I):
            parca = m.strip()
            if len(parca) < 50:
                continue
            bulundu = True
            lines.append(f"  🎯 GÖMÜLÜ JSON bulundu: {ad} ({len(parca)} bayt)")
            try:
                veri = json.loads(parca)
                if isinstance(veri, dict):
                    lines.append(f"     üst alanlar: {', '.join(list(veri.keys())[:12])}")
                elif isinstance(veri, list):
                    lines.append(f"     liste, {len(veri)} kayıt")
                # Icinde bildirim gecen anahtarlari ara
                duz = json.dumps(veri, ensure_ascii=False)[:400000]
                for iz in ANAHTARLAR:
                    if iz.lower() in duz.lower():
                        lines.append(f"     ✅ içinde '{iz}' geçiyor — bildirim verisi burada olabilir")
                        break
            except Exception as e:
                lines.append(f"     (JSON ayrıştırılamadı: {type(e).__name__}) ilk 120 karakter:")
                lines.append(f"     {parca[:120]}")
    if not bulundu:
        lines.append("  ℹ️ Sayfada gömülü JSON bloğu bulunamadı.")
    return bulundu


def yapi_ornekle(html, lines):
    """Anahtar kelimelerin etrafindaki HTML yapisini ornekler."""
    ornek_sayisi = 0
    for anahtar in ANAHTARLAR:
        for m in re.finditer(re.escape(anahtar), html):
            if ornek_sayisi >= 4:
                return ornek_sayisi
            bas = max(0, m.start() - 700)
            son = min(len(html), m.end() + 400)
            parca = html[bas:son]

            # Bu parcadaki etiketleri ve class isimlerini cikar
            etiketler = re.findall(r'<(\w+)[^>]*class=["\']([^"\']{3,60})["\']', parca)
            benzersiz = []
            for tag, cls in etiketler:
                anahtar_cls = (tag, cls.split()[0] if cls.split() else cls)
                if anahtar_cls not in benzersiz:
                    benzersiz.append(anahtar_cls)

            ornek_sayisi += 1
            lines.append(f"  ── Örnek {ornek_sayisi} ('{anahtar}' çevresi) ──")
            if benzersiz:
                lines.append("     etiket/class: " +
                             ", ".join(f"{t}.{c}" for t, c in benzersiz[:6]))
            metin = temizle(parca)
            lines.append(f"     metin: {metin[:230]}")
            break   # her anahtardan bir ornek yeter
    return ornek_sayisi


def main():
    send_telegram_message(
        "🔬 [KAP HTML KEŞFİ] Başladı.\n"
        "İki API turu boş çıktı; veri sayfanın içinde olabilir.\n"
        "Önce yapısına bakıyoruz, sonra parser yazılacak.\n"
        "~1 dakika...")

    lines = ["🔬 [KAP HTML KEŞİF SONUÇLARI]", ""]

    try:
        r = requests.get(ANA_SAYFA, headers=HEADERS, timeout=30)
    except Exception as e:
        lines.append(f"❌ Sayfa alınamadı: {type(e).__name__}: {e}")
        send_telegram_message("\n".join(lines))
        return

    if r.status_code != 200:
        lines.append(f"❌ HTTP {r.status_code}")
        send_telegram_message("\n".join(lines))
        return

    html = r.text
    lines.append(f"✅ Sayfa indirildi: {len(html)} bayt")
    sayimlar = {a: html.count(a) for a in ANAHTARLAR}
    lines.append("   anahtar sayıları: " +
                 ", ".join(f"{a}={n}" for a, n in sayimlar.items() if n))
    lines.append("")

    lines.append("① GÖMÜLÜ JSON ARAMASI (en iyi senaryo)")
    gomulu_json_ara(html, lines)
    lines.append("")

    lines.append("② HTML YAPISI ÖRNEKLERİ")
    n = yapi_ornekle(html, lines)
    if n == 0:
        lines.append("  ℹ️ Anahtar kelime çevresinde yapı örneklenemedi.")
    lines.append("")

    lines.append("📊 Bu çıktıyı Claude'a gönder — yapıya göre KAP Monitor")
    lines.append("   parser'ı yazılacak. Tarayıcı/Playwright gerekmeyecek.")

    msg = "\n".join(lines)
    send_telegram_message(msg)
    print(msg, flush=True)


if __name__ == "__main__":
    from http.server import BaseHTTPRequestHandler, HTTPServer

    class _P(BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b"html kesif")

        def log_message(self, *a):
            pass

    threading.Thread(
        target=lambda: HTTPServer(("0.0.0.0", int(os.environ.get("PORT", "10000"))),
                                  _P).serve_forever(), daemon=True).start()
    main()
    print("Keşif bitti. Start Command'i geri alabilirsin.", flush=True)
    while True:
        time.sleep(3600)
