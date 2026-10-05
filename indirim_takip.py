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


# =============================================================================
# (2026-10-03) YÖN DEĞİŞİKLİĞİ — yemek siparişi indirim/kupon takibi
# Kullanıcı sabit bir adrese (İstanbul Tuzla) her gün yemek siparişi
# veriyor - o adrese hizmet veren restoranlarda büyük indirim/kupon
# çıkınca haber vermesi isteniyor. Bu 4 platformun web sitesi yapısını
# (varsa) ve adrese göre arama şeklini BİLMİYORUZ - yine keşifle
# başlıyoruz. Bazıları sadece mobil uygulamada çalışıyor olabilir, web
# sitesi hiç bulunmayabilir - bu da bu keşfin bulgularından biri olacak.
# =============================================================================
YEMEK_ADRES = "İstanbul, Tuzla, Deri OSB, Tanem Sokak No:6"

YEMEK_SITE_ADAYLARI = {
    "Yemeksepeti": [
        "https://www.yemeksepeti.com/",
        "https://www.yemeksepeti.com/robots.txt",
    ],
    "Trendyol Yemek (tgoyemek.com)": [
        "https://www.tgoyemek.com/",
        "https://www.tgoyemek.com/robots.txt",
    ],
    "Getir Yemek": [
        "https://getir.com/",
        "https://getir.com/robots.txt",
    ],
    "Migros Yemek": [
        "https://www.migros.com.tr/",
        "https://www.migros.com.tr/robots.txt",
    ],
}


def yemek_kesif_calistir():
    toplam_adres = sum(len(v) for v in YEMEK_SITE_ADAYLARI.values())
    send_football_message(
        f"🔎 Yemek platformları keşif taraması başladı ({INDIRIM_SURUM})\n"
        f"Hedef adres: {YEMEK_ADRES}\n"
        f"{len(YEMEK_SITE_ADAYLARI)} platform, {toplam_adres} aday adres "
        f"deneniyor...")

    satirlar = [
        "# Yemek Platformları Keşif Raporu",
        f"Oluşturulma: {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}",
        f"Hedef adres: {YEMEK_ADRES}\n",
        "⚠️ Bu 4 platformun 'adrese göre restoran listesi' gösteren gerçek "
        "URL yapısı bilinmiyor - burada sadece ana sayfa/robots.txt "
        "deneniyor. Eğer bunlar web'de çalışmıyorsa (sadece mobil "
        "uygulama), bunu da burada göreceğiz.\n",
    ]

    site_ozet = {}
    for site_adi, adresler in YEMEK_SITE_ADAYLARI.items():
        satirlar.append(f"\n---\n# {site_adi}\n")
        basarili = 0
        for url in adresler:
            print(f"[İndirim] İnceleniyor: {url}", flush=True)
            r = _adresi_incele(url)
            satirlar.append(f"\n## {url}\n")
            if r.get("hata"):
                satirlar.append(f"❌ Hata: {r['hata']}\n")
                continue
            satirlar.append(f"- Durum kodu: {r['durum_kodu']}")
            if r["durum_kodu"] == 200:
                basarili += 1
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
        site_ozet[site_adi] = f"{basarili}/{len(adresler)} adres 200 döndü"

    ozet_blogu = ["\n---\n## Özet\n"]
    for site_adi, ozet in site_ozet.items():
        ozet_blogu.append(f"- {site_adi}: {ozet}")
    satirlar = satirlar[:3] + ozet_blogu + satirlar[3:]

    satirlar.append(
        "\n---\n## Not\nBu sadece ana sayfaların erişilebilirliğini test "
        "etti - adrese göre restoran/kampanya listesini görmek için muhtemelen "
        "gerçek bir tarayıcıdan (bilgisayar/telefon) bu adreslerden birine "
        "gidip adresini girip URL'in nasıl değiştiğine bakman gerekebilir.")

    rapor = "\n".join(satirlar)
    dosya_yolu = os.path.join(DATA_DIR, "yemek_kesif_raporu.md")
    try:
        with open(dosya_yolu, "w", encoding="utf-8") as f:
            f.write(rapor)
        send_football_document(dosya_yolu, caption="🔎 Yemek Platformları Keşif Raporu")
    except Exception as e:
        send_football_message(f"❌ Keşif raporu oluşturulamadı: {e}")


# =============================================================================
# (2026-10-03) YÖN DEĞİŞİKLİĞİ: Kullanıcı e-ticaret yerine YEMEK siparişi
# indirim/kupon takibine karar verdi - her gün aynı saatte, aynı adrese
# (İstanbul Tuzla Deri OSB) sipariş verdiği için buradaki restoranların
# anlık indirim/kupon durumunu izlemek istiyor. 4 platform: Yemeksepeti,
# Trendyol Yemek, Getir Yemek, Migros Yemek.
#
# ⚠️ Aşağıdaki adresler TAHMİN - hiçbirinin gerçek yapısını doğrulamadık.
# Adrese özel restoran listesi genelde bir "önce adresi seç/çözümle"
# adımı gerektiriyor (çoğu yemek uygulaması konum bazlı oturum açıyor),
# bu yüzden bu ilk tur sadece "siteye genel olarak erişebiliyor muyuz,
# nasıl bir yapı var" sorusuna cevap arıyor - adrese özel filtreleme
# BİR SONRAKİ adımda, bu sonuca göre kurulacak.
# =============================================================================
YEMEK_SITE_ADAYLARI = {
    "Yemeksepeti": [
        "https://www.yemeksepeti.com/",
        "https://www.yemeksepeti.com/tuzla-istanbul",
        "https://www.yemeksepeti.com/robots.txt",
    ],
    "Trendyol Yemek": [
        "https://tgoyemek.com/",
        "https://www.trendyol.com/yemek",
        "https://tgoyemek.com/robots.txt",
    ],
    "Getir Yemek": [
        "https://getir.com/yemek/",
        "https://getir.com/robots.txt",
    ],
    "Migros Yemek": [
        "https://www.migros.com.tr/yemek",
        "https://www.migros.com.tr/robots.txt",
    ],
}


def kesif_calistir_yemek():
    toplam = sum(len(v) for v in YEMEK_SITE_ADAYLARI.values())
    send_football_message(
        f"🔎 Yemek platformları keşif taraması başladı ({INDIRIM_SURUM})\n"
        f"{len(YEMEK_SITE_ADAYLARI)} platform, toplam {toplam} aday adres "
        f"deneniyor (adresler TAHMİN, gerçek yapıyı öğrenmeye çalışıyoruz)...")

    satirlar = [
        "# Yemek Platformları Keşif Raporu",
        f"Oluşturulma: {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}\n",
        "Hedef adres: İstanbul Tuzla Deri OSB (bu turda adrese özel değil, "
        "sadece genel erişim/yapı test ediliyor).\n",
    ]
    site_ozet = {}

    for site_adi, adresler in YEMEK_SITE_ADAYLARI.items():
        satirlar.append(f"\n---\n# {site_adi}\n")
        basarili = 0
        for url in adresler:
            print(f"[İndirim-Yemek] İnceleniyor: {url}", flush=True)
            r = _adresi_incele(url)
            satirlar.append(f"\n## {url}\n")
            if r.get("hata"):
                satirlar.append(f"❌ Hata: {r['hata']}\n")
                continue
            satirlar.append(f"- Durum kodu: {r['durum_kodu']}")
            if r["durum_kodu"] == 200:
                basarili += 1
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
        site_ozet[site_adi] = f"{basarili}/{len(adresler)} adres 200 döndü"

    ozet_blok = ["\n---\n## Özet (ilk bakış)\n"]
    for site_adi, ozet in site_ozet.items():
        ozet_blok.append(f"- {site_adi}: {ozet}")
    satirlar = satirlar[:2] + ozet_blok + satirlar[2:]

    satirlar.append(
        "\n---\n## Sırada ne var\nEn açık/erişilebilir çıkan platformu pilot "
        "seçip, adrese özel restoran listesinin gerçekte NASIL çekildiğini "
        "(muhtemelen ayrı bir konum/adres API'si var) bir sonraki keşif "
        "turunda inceleyeceğiz.")

    rapor = "\n".join(satirlar)
    dosya_yolu = os.path.join(DATA_DIR, "yemek_kesif_raporu.md")
    try:
        with open(dosya_yolu, "w", encoding="utf-8") as f:
            f.write(rapor)
        send_football_document(dosya_yolu, caption="🔎 Yemek Platformları Keşif Raporu")
    except Exception as e:
        send_football_message(f"❌ Keşif raporu oluşturulamadı: {e}")


def tgoyemek_derin_inceleme():
    """(2026-10-03) tgoyemek.com (Trendyol Yemek) erişilebilir çıktı VE
    robots.txt açıkça '/restoranlar' taranmasına izin veriyor - bu, veriyi
    arama motorları için sayfa içine GÖMDÜKLERİNİN işareti (Amazon'daki
    gibi gizli bir client-only widget olması ihtimali düşük). Next.js
    sitelerde bu genelde __NEXT_DATA__ adlı bir <script> içinde JSON
    olarak gelir - burada arayıp BULURSA ayrı bir dosyaya kaydediyoruz,
    gerçek restoran/adres yapısını görüp doğru ayrıştırıcıyı yazabilelim."""
    hedefler = ["https://tgoyemek.com/", "https://tgoyemek.com/restoranlar"]

    for url in hedefler:
        send_football_message(f"📥 İnceleniyor: {url}")
        try:
            yanit = requests.get(url, headers=_TARAYICI_BASLIKLARI, timeout=30)
        except Exception as e:
            send_football_message(f"❌ {url} alınamadı: {e}")
            continue

        yol_kismi = url.split("://", 1)[-1].split("/", 1)
        dosya_adi_govde = yol_kismi[1].rstrip("/") if len(yol_kismi) > 1 and yol_kismi[1] else "anasayfa"

        eslesme = re.search(
            r'<script[^>]*id=["\']__NEXT_DATA__["\'][^>]*>(.*?)</script>',
            yanit.text, re.S)
        if eslesme:
            json_metni = eslesme.group(1)
            try:
                ayristirilmis = json.loads(json_metni)
                json_metni = json.dumps(ayristirilmis, ensure_ascii=False, indent=2)
            except Exception:
                pass  # ham hâliyle kaydet, en azından okunabilir olsun
            dosya_yolu = os.path.join(DATA_DIR, f"tgoyemek_{dosya_adi_govde}_nextdata.json")
            with open(dosya_yolu, "w", encoding="utf-8") as f:
                f.write(json_metni)
            send_football_document(
                dosya_yolu,
                caption=f"📥 {url} — __NEXT_DATA__ bulundu (~{len(json_metni):,} karakter)".replace(",", "."))
        else:
            # Gömülü veri yoksa en azından ham HTML'i gönderelim, en baştan
            # bakalım gerçekte ne geliyor.
            dosya_yolu = os.path.join(DATA_DIR, f"tgoyemek_{dosya_adi_govde}_ham.html")
            with open(dosya_yolu, "w", encoding="utf-8") as f:
                f.write(yanit.text)
            send_football_document(
                dosya_yolu,
                caption=f"📥 {url} — __NEXT_DATA__ bulunamadı, ham HTML gönderildi "
                        f"(durum {yanit.status_code}, {len(yanit.content):,} byte)".replace(",", "."))


def amazon_deals_ham_kaydet():
    """(2026-10-03) Amazon.com.tr tek açık site çıktı, /deals sayfası da
    muhtemelen Amazon'un KENDİ resmi 'Günün Fırsatları' sayfası. Gerçek
    ayrıştırıcıyı doğru yazabilmek için sayfanın TAM HTML'ini (sadece ilk
    600 karakter değil) indirip dosya olarak gönderiyoruz - ürün/fiyat/
    indirim etiketlerinin gerçek HTML yapısını görmemiz lazım."""
    url = "https://www.amazon.com.tr/deals"
    send_football_message(f"📥 Amazon.com.tr /deals sayfasının tam HTML'i indiriliyor...")
    try:
        yanit = requests.get(url, headers=_TARAYICI_BASLIKLARI, timeout=30)
    except Exception as e:
        send_football_message(f"❌ İndirilemedi: {e}")
        return

    dosya_yolu = os.path.join(DATA_DIR, "amazon_deals_ham.html")
    try:
        with open(dosya_yolu, "w", encoding="utf-8") as f:
            f.write(yanit.text)
        send_football_document(
            dosya_yolu,
            caption=f"📥 Amazon.com.tr /deals ham HTML (durum {yanit.status_code}, "
                    f"{len(yanit.content):,} byte)".replace(",", "."))
    except Exception as e:
        send_football_message(f"❌ Dosya kaydedilemedi/gönderilemedi: {e}")


def baslangic():
    send_football_message(
        f"🍔 Yemek İndirim Takip Sistemi — KEŞİF AŞAMASI ({INDIRIM_SURUM})\n\n"
        "Futbol botu durduruldu, yerine bu sistem geliyor. Şu an 4 yemek "
        "platformunun (Yemeksepeti, Trendyol Yemek, Getir Yemek, Migros "
        "Yemek) genel sayfa yapısını inceliyor (henüz canlı bildirim yok, "
        "adrese özel filtre de yok) - sonuç birkaç dakika içinde ayrı bir "
        "dosya olarak gelecek.")
