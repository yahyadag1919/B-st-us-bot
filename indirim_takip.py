"""
indirim_takip.py — ÖZEL İNDİRİM TAKİP SİSTEMİ (futbol botunun yerine)
==============================================================================
2026-10-03 — Kullanıcının kararıyla futbol sinyal botu durduruldu, yerine
bu sistem geçiyor. Aynı Telegram bot/sohbeti (FOOTBALL_TELEGRAM_TOKEN/
FOOTBALL_TELEGRAM_CHAT_ID, mac_analiz_yahya_bot) kullanılıyor.

ŞU ANKİ AŞAMA: KEŞİF (discovery) - henüz canlı bir indirim takip sistemi
DEĞİL. Claude'un bu ortamda internete çıkışı yok, bu yüzden Trendyol'un
"kampanyalar/indirimler" sayfasının gerçek yapısını (JSON mu dönüyor,
HTML mi, kategori filtresi URL'den mi yapılıyor, iç bir API var mı)
TAHMİN ETMEK yerine canlıda ÖLÇÜYORUZ. Bu modül birkaç aday adresi
deneyip ne bulduğunu bir rapor hâlinde Telegram'a (dosya olarak) gönderiyor.
Gerçek takip sistemi, bu raporun sonucuna göre bir sonraki turda yazılacak.

⚠️ Bu, Trendyol'un HERKESE AÇIK sayfalarını okumak (tıpkı bir tarayıcının
yaptığı gibi) - kimlik doğrulama atlatmıyor, ödeme gerektiren bir hizmeti
taklit etmiyor. Yine de bu scraping'in sitenin kullanım şartlarına aykırı
olabileceğini ve site yapısı değiştiğinde kırılabileceğini unutma.
"""
import os
import re
import json
import threading
from datetime import datetime, timezone

import requests

FOOTBALL_TELEGRAM_TOKEN = os.environ.get("FOOTBALL_TELEGRAM_TOKEN", "")
FOOTBALL_TELEGRAM_CHAT_ID = os.environ.get("FOOTBALL_TELEGRAM_CHAT_ID", "")
DATA_DIR = os.environ.get("DATA_DIR", ".")

INDIRIM_SURUM = "indirim-takip-kesif-v1-2026-10-03"

_TARAYICI_BASLIKLARI = {
    "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                   "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "tr-TR,tr;q=0.9,en-US;q=0.8,en;q=0.7",
}

# (2026-10-03) Trendyol Cloudflare arkasında çıktı (403, "Attention
# Required!") - basit bir istekle geçilemiyor. Diğer 3 siteyi de deneyip
# hangisinin daha az korumalı olduğunu görmemiz lazım, pilot siteyi ona
# göre seçeceğiz.
SITE_ADAYLARI = {
    "Trendyol": [
        "https://www.trendyol.com/",
        "https://www.trendyol.com/kampanyalar",
        "https://www.trendyol.com/outlet",
        "https://www.trendyol.com/robots.txt",
    ],
    "Hepsiburada": [
        "https://www.hepsiburada.com/",
        "https://www.hepsiburada.com/kampanyalar",
        "https://www.hepsiburada.com/robots.txt",
    ],
    "Amazon.com.tr": [
        "https://www.amazon.com.tr/",
        "https://www.amazon.com.tr/deals",
        "https://www.amazon.com.tr/robots.txt",
    ],
    "N11": [
        "https://www.n11.com/",
        "https://www.n11.com/kampanyalar",
        "https://www.n11.com/robots.txt",
    ],
}

# Geriye dönük uyumluluk - eski kod bu adı kullanıyordu
TRENDYOL_ADAYLAR = SITE_ADAYLARI["Trendyol"]


def send_football_message(text: str):
    """İsim uyumu için aynı fonksiyon adı korundu - football_bot.py'nin
    yerini alan bu modül de AYNI Telegram bot/sohbete yazıyor."""
    if not FOOTBALL_TELEGRAM_TOKEN or not FOOTBALL_TELEGRAM_CHAT_ID:
        print(f"[İndirim devre dışı] {text}", flush=True)
        return
    try:
        requests.post(
            f"https://api.telegram.org/bot{FOOTBALL_TELEGRAM_TOKEN}/sendMessage",
            json={"chat_id": FOOTBALL_TELEGRAM_CHAT_ID, "text": text[:4000]}, timeout=20)
    except Exception as e:
        print(f"[İndirim Telegram hata] {e}", flush=True)


def send_football_document(dosya_yolu: str, caption: str = ""):
    if not FOOTBALL_TELEGRAM_TOKEN or not FOOTBALL_TELEGRAM_CHAT_ID:
        print(f"[İndirim devre dışı] dosya: {dosya_yolu}", flush=True)
        return
    try:
        with open(dosya_yolu, "rb") as f:
            requests.post(
                f"https://api.telegram.org/bot{FOOTBALL_TELEGRAM_TOKEN}/sendDocument",
                data={"chat_id": FOOTBALL_TELEGRAM_CHAT_ID, "caption": caption[:1024]},
                files={"document": f}, timeout=60)
    except Exception as e:
        print(f"[İndirim] Dosya gönderilemedi: {e}", flush=True)


# =============================================================================
# KEŞİF — bir adresin gerçekte ne döndürdüğünü inceler
# =============================================================================
def _icerik_tipini_tahmin_et(metin: str) -> str:
    kirpilmis = metin.strip()
    if kirpilmis.startswith("{") or kirpilmis.startswith("["):
        return "JSON olabilir"
    if kirpilmis.lower().startswith("<!doctype html") or "<html" in kirpilmis[:500].lower():
        return "HTML"
    return "Bilinmiyor"


def _gommeli_veri_blogu_ara(html: str):
    """Modern sitelerde sayfa verisi genelde bir <script> içinde JSON
    olarak gömülü gelir (Next.js: __NEXT_DATA__, bazı sitelerde
    __INITIAL_STATE__ gibi isimlerle). Varsa yerini ve boyutunu bulur -
    ileride gerçek takip sistemi muhtemelen BURADAN veri çekecek, HTML
    parse etmekten çok daha güvenilir olur."""
    bulgular = []
    kaliplar = [
        (r'<script[^>]*id=["\']__NEXT_DATA__["\'][^>]*>(.*?)</script>', "__NEXT_DATA__"),
        (r'window\.__INITIAL_STATE__\s*=\s*(\{.*?\});', "__INITIAL_STATE__"),
        (r'window\.__PRELOADED_STATE__\s*=\s*(\{.*?\});', "__PRELOADED_STATE__"),
    ]
    for desen, isim in kaliplar:
        eslesme = re.search(desen, html, re.S)
        if eslesme:
            icerik = eslesme.group(1)
            bulgular.append(f"{isim}: bulundu, boyut ~{len(icerik)} karakter")
    return bulgular


def _api_adresi_ipuclarini_ara(html: str):
    """Sayfanın <script> etiketleri içinde '/api/' geçen adres
    kalıntıları ararız - SPA siteler genelde veriyi arka planda böyle bir
    uç noktadan JSON olarak çekiyor, biz de aynı adresi doğrudan
    çağırabilirsek HTML parse etmemize hiç gerek kalmaz."""
    adaylar = set(re.findall(r'["\']((?:https?:)?//[^"\']*?/api/[^"\']*)["\']', html))
    adaylar |= set(re.findall(r'["\'](/api/[^"\']*)["\']', html))
    return sorted(adaylar)[:20]


def _adresi_incele(url: str) -> dict:
    sonuc = {"url": url, "hata": None}
    try:
        yanit = requests.get(url, headers=_TARAYICI_BASLIKLARI, timeout=20, allow_redirects=True)
    except Exception as e:
        sonuc["hata"] = str(e)
        return sonuc

    sonuc["durum_kodu"] = yanit.status_code
    sonuc["son_url"] = yanit.url
    sonuc["content_type"] = yanit.headers.get("Content-Type", "?")
    sonuc["boyut"] = len(yanit.content)
    sonuc["icerik_tipi_tahmini"] = _icerik_tipini_tahmin_et(yanit.text)
    sonuc["ilk_600_karakter"] = yanit.text[:600]

    if sonuc["icerik_tipi_tahmini"] == "HTML":
        sonuc["gommeli_veri"] = _gommeli_veri_blogu_ara(yanit.text)
        sonuc["api_ipuclari"] = _api_adresi_ipuclarini_ara(yanit.text)
    else:
        sonuc["gommeli_veri"] = []
        sonuc["api_ipuclari"] = []

    return sonuc


def kesif_calistir():
    toplam_adres = sum(len(v) for v in SITE_ADAYLARI.values())
    send_football_message(
        f"🔎 4 site için keşif taraması başladı ({INDIRIM_SURUM})\n"
        f"{len(SITE_ADAYLARI)} site, toplam {toplam_adres} aday adres deneniyor, "
        f"sonuç birkaç dakika içinde dosya olarak gelecek...")

    satirlar = [
        "# E-Ticaret Siteleri Keşif Raporu",
        f"Oluşturulma: {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}\n",
        "Trendyol Cloudflare arkasında çıktı (403) - diğer 3 site de burada "
        "test ediliyor, hangisinin pilot site olacağına bu sonuca göre "
        "karar vereceğiz. Henüz canlı bir indirim bildirimi DEĞİL.\n",
    ]

    site_ozet = {}

    for site_adi, adresler in SITE_ADAYLARI.items():
        satirlar.append(f"\n---\n# {site_adi}\n")
        basarili_sayisi = 0
        for url in adresler:
            print(f"[İndirim] İnceleniyor: {url}", flush=True)
            r = _adresi_incele(url)
            satirlar.append(f"\n## {url}\n")
            if r.get("hata"):
                satirlar.append(f"❌ Hata: {r['hata']}\n")
                continue

            satirlar.append(f"- Durum kodu: {r['durum_kodu']}")
            if r["durum_kodu"] == 200:
                basarili_sayisi += 1
            if r["son_url"] != url:
                satirlar.append(f"- Yönlendirildi: {r['son_url']}")
            satirlar.append(f"- Content-Type: {r['content_type']}")
            satirlar.append(f"- Boyut: {r['boyut']:,} byte".replace(",", "."))
            satirlar.append(f"- Tahmini içerik tipi: {r['icerik_tipi_tahmini']}")

            if r["gommeli_veri"]:
                satirlar.append("- Gömülü veri bloğu:")
                for g in r["gommeli_veri"]:
                    satirlar.append(f"    {g}")
            if r["api_ipuclari"]:
                satirlar.append(f"- Bulunan API ipuçları ({len(r['api_ipuclari'])} adet):")
                for a in r["api_ipuclari"]:
                    satirlar.append(f"    {a}")

            satirlar.append("- İlk 600 karakter:")
            satirlar.append("```")
            satirlar.append(r["ilk_600_karakter"])
            satirlar.append("```")

        site_ozet[site_adi] = f"{basarili_sayisi}/{len(adresler)} adres 200 döndü"

    satirlar_baslik = ["\n---\n## Özet (ilk bakış)\n"]
    for site_adi, ozet in site_ozet.items():
        satirlar_baslik.append(f"- {site_adi}: {ozet}")
    satirlar = satirlar[:2] + satirlar_baslik + satirlar[2:]

    satirlar.append(
        "\n---\n## Sırada ne var\nBu raporu inceleyip hangi sitenin en az "
        "korumalı olduğunu (en çok 200 dönen) bulup pilot siteyi ona göre "
        "seçeceğiz, sonra kategori filtresi ve gerçek indirim tespiti "
        "mantığını yazacağız.")

    rapor = "\n".join(satirlar)
    dosya_yolu = os.path.join(DATA_DIR, "eticaret_kesif_raporu.md")
    try:
        with open(dosya_yolu, "w", encoding="utf-8") as f:
            f.write(rapor)
        send_football_document(dosya_yolu, caption="🔎 E-Ticaret Siteleri Keşif Raporu")
    except Exception as e:
        send_football_message(f"❌ Keşif raporu oluşturulamadı: {e}")


def baslangic():
    send_football_message(
        f"🛒 İndirim Takip Sistemi — KEŞİF AŞAMASI ({INDIRIM_SURUM})\n\n"
        "Futbol botu durduruldu, yerine bu sistem geliyor. Şu an sadece "
        "Trendyol'un sayfa yapısını inceliyor (henüz canlı bildirim yok) - "
        "sonuç birkaç dakika içinde ayrı bir dosya olarak gelecek.")
