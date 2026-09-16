"""
bist_katalizor.py — İYİ HABER, HENÜZ FİYATA YANSIMAMIŞ (KATALİZÖR) TARAYICISI
==============================================================================
2026-09-15 — Kullanıcının kararıyla kuruldu.

FİKİR: Bir hisse için gerçekten olumlu bir gelişme (bilanço/kâr artışı,
büyük sözleşme/ihale kazanma, kapasite artışı, yeni yatırım vb.) haber
olarak çıktığında, fiyat bunu HENÜZ yansıtmamışsa (son birkaç günde
belirgin bir yükseliş olmamışsa), bu bir "henüz keşfedilmemiş" fırsat
olabilir - literatürde "post-earnings announcement drift" gibi tanınan
bir olguya benziyor.

NEDEN GEMINI KULLANILMIYOR: Haberler zaten Türkçe geliyor, kullanıcı da
Türkçe okuyor - "İngilizce'yi anlaşılır kıl" ihtiyacı yok (ABD tarafında
olsaydı gerekirdi). Türkçe anahtar kelime listesi bedava ve anında
çalışıyor, Gemini kotasını boşa harcamaya gerek yok.

NEDEN kap_monitor.py KULLANILMIYOR: O modül FARKLI bir amaç için
yazılmış - KAP bildirimlerinin haberlere ne kadar gecikmeli düştüğünü
ÖLÇMEK, sadece 6 RSS + 2 dar Google News sorgusuyla, yalnızca
"KAP *** KOD ***" kalıbını yakalıyor. Bu modülün ihtiyacı olan geniş,
hisse-bazlı haber taraması için kapsamı yetersiz - kap_monitor.py'ye
HİÇ dokunulmadı, ayrı ve izole yeni bir modül kuruldu.

VERİ KAYNAĞI: Google News RSS, her BIST hissesi için ayrı sorgu -
kap_monitor.py'de zaten kanıtlanmış aynı teknik (Google News RSS XML
parse), sadece hisse-bazlı ve pozitif/negatif ayrımı yapacak şekilde
uyarlandı.

FİYAT TEPKİSİ KONTROLÜ: yfinance ile son birkaç günlük kapanış
karşılaştırılıyor - haber çıktıktan sonra fiyat hâlâ belirgin
yükselmemişse "henüz fiyatlanmamış" sayılıp bildiriliyor.

DÜRÜSTLÜK NOTU: Bu TEST EDİLMEMİŞ bir hipotez, kanıtlanmış bir sistem
DEĞİL - tıpkı Midas yön etiketinde olduğu gibi, kullanıcı kendi
gözlemiyle zamanla değerlendirecek.
"""
import os
import re
import csv
import time
import threading
from datetime import datetime, timezone, timedelta
from email.utils import parsedate_to_datetime

import requests
import yfinance as yf

# =============================================================================
# YAPILANDIRMA
# =============================================================================
TELEGRAM_TOKEN = os.environ.get("ARGE_TELEGRAM_TOKEN", "")
TELEGRAM_CHAT_ID = os.environ.get("ARGE_TELEGRAM_CHAT_ID", "")
DATA_DIR = os.environ.get("DATA_DIR", ".")

KATALIZOR_SURUM = "bist-katalizor-v1-2026-09-15"
GORULEN_DOSYASI = os.path.join(DATA_DIR, "bist_katalizor_gorulen.json")

TARAMA_ARALIGI_SN = 30 * 60          # tüm liste her 30 dakikada bir taranır
TICKER_ARASI_BEKLEME_SN = 1.5        # Google News'e nazik davranmak için

# Fiyat "henüz tepki vermemiş" sayılması için: haberden önceki kapanışa
# göre şu anki fiyatın artış yüzdesi bu eşiğin altında kalmalı.
FIYAT_TEPKI_ESIGI_PCT = 4.0

HEADERS = {
    "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                   "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0 Safari/537.36"),
    "Accept": "text/xml, application/rss+xml, */*;q=0.8",
    "Accept-Language": "tr-TR,tr;q=0.9",
}

REQUIRED_ENV_VARS = ["ARGE_TELEGRAM_TOKEN", "ARGE_TELEGRAM_CHAT_ID"]


def validate_katalizor_config():
    return [n for n in REQUIRED_ENV_VARS if not os.environ.get(n)]


# =============================================================================
# HİSSE LİSTESİ — arge_botu.py'den METİN olarak okunuyor (canlı import YOK,
# projenin standart kuralı).
# =============================================================================
def _load_bist_hisseler():
    varsayilan = ["THYAO.IS", "GARAN.IS", "ASELS.IS", "SASA.IS", "BIMAS.IS"]
    try:
        import ast
        with open("arge_botu.py", "r", encoding="utf-8") as f:
            kaynak = f.read()
        agac = ast.parse(kaynak)
        for node in ast.walk(agac):
            if isinstance(node, ast.Assign):
                for hedef in node.targets:
                    if isinstance(hedef, ast.Name) and hedef.id == "BIST_HISSELER":
                        return ast.literal_eval(node.value)
    except Exception as e:
        print(f"[Katalizör] BIST_HISSELER okunamadı, varsayılan liste: {e}", flush=True)
    return varsayilan


BIST_HISSELER = _load_bist_hisseler()


# =============================================================================
# TELEGRAM
# =============================================================================
def send_telegram_message(text: str):
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        print(f"[Katalizör devre dışı] {text}", flush=True)
        return
    try:
        requests.post(
            f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage",
            json={"chat_id": TELEGRAM_CHAT_ID, "text": text[:4000]}, timeout=20)
    except Exception as e:
        print(f"[Katalizör Telegram hata] {e}", flush=True)


# =============================================================================
# BASİT JSON DURUM DOSYASI
# =============================================================================
def _json_yukle(yol, varsayilan):
    if not os.path.exists(yol):
        return varsayilan
    try:
        import json
        with open(yol, "r", encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return varsayilan


def _json_kaydet(yol, veri):
    try:
        import json
        with open(yol, "w", encoding="utf-8") as f:
            json.dump(veri, f)
    except OSError as e:
        print(f"[Katalizör] {yol} kaydedilemedi: {e}", flush=True)


# =============================================================================
# POZİTİF/NEGATİF ANAHTAR KELİME SINIFLANDIRMASI (Türkçe)
# =============================================================================
POZITIF_KELIMELER = {
    "rekor kâr", "rekor kar", "kârını artırdı", "karını artırdı",
    "net kârı yükseldi", "net karı yükseldi", "kâr açıkladı",
    "sözleşme imzaladı", "anlaşma imzaladı", "ihale kazandı", "ihaleyi kazandı",
    "yeni yatırım", "kapasite artışı", "kapasitesini artırıyor",
    "ihracat rekoru", "rekor ihracat", "temettü dağıtacak", "temettü açıkladı",
    "bedelsiz sermaye artırımı", "hisse geri alım", "geri alım programı",
    "yeni pazara girdi", "dev sipariş", "milyar dolarlık anlaşma",
    "stratejik ortaklık", "birleşme", "satın alma anlaşması",
    "kredi derecelendirme notu yükseltildi", "not artırımı",
    "üretim kapasitesi", "yeni fabrika", "genişleme yatırımı",
}

NEGATIF_KELIMELER = {
    "zarar açıkladı", "kâr düşüşü", "kar düşüşü", "net zarar",
    "dava açıldı", "soruşturma", "para cezası", "idari para cezası",
    "üretim durduruldu", "işten çıkarma", "kapatma kararı",
    "not indirimi", "kredi notu düşürüldü", "iflas", "konkordato",
}


def _haber_siniflandir(baslik: str) -> str:
    b = baslik.lower()
    if any(k in b for k in NEGATIF_KELIMELER):
        return "negatif"
    if any(k in b for k in POZITIF_KELIMELER):
        return "pozitif"
    return "notr"


# =============================================================================
# GOOGLE NEWS RSS — HİSSE BAZLI TARAMA
# =============================================================================
def _temizle(s: str) -> str:
    s = re.sub(r"<!\[CDATA\[|\]\]>", "", s)
    s = re.sub(r"<[^>]+>", "", s)
    return re.sub(r"\s+", " ", s).strip()


def _item_ayikla(xml: str):
    cikti = []
    for o in re.findall(r"<item>(.*?)</item>", xml, re.S | re.I):
        b = re.search(r"<title[^>]*>(.*?)</title>", o, re.S | re.I)
        t = re.search(r"<pubDate[^>]*>(.*?)</pubDate>", o, re.S | re.I)
        link = re.search(r"<link[^>]*>(.*?)</link>", o, re.S | re.I)
        if not b:
            continue
        tarih = None
        if t:
            try:
                tarih = parsedate_to_datetime(_temizle(t.group(1)))
                if tarih.tzinfo is None:
                    tarih = tarih.replace(tzinfo=timezone.utc)
            except Exception:
                pass
        cikti.append({
            "baslik": _temizle(b.group(1)),
            "tarih": tarih,
            "link": _temizle(link.group(1)) if link else "",
        })
    return cikti


def _haber_ara(hisse_kodu: str):
    """hisse_kodu 'THYAO.IS' formatında - Google News sorgusu için '.IS' atılır.
    'when:2d' operatörü Google News'e SADECE son 2 gündeki haberleri
    getirmesini söylüyor - bu olmadan Google, aramayla "alakalı" 1-2 yıl
    önceki haberleri de döndürebiliyor (2026-09-17'de yaşanan 2000+
    mesajlık akının sebebi buydu)."""
    kod = hisse_kodu.replace(".IS", "")
    q = requests.utils.quote(f"{kod} hisse when:1d")
    url = f"https://news.google.com/rss/search?q={q}&hl=tr&gl=TR&ceid=TR:tr"
    try:
        r = requests.get(url, headers=HEADERS, timeout=20)
        r.raise_for_status()
    except Exception:
        return []
    return _item_ayikla(r.text)


# 'when:2d' bir garanti değil (Google'ın kendi belgelemediği bir operatör) -
# ikinci bir emniyet katmanı olarak, tarihi elimizde olan haberlerde
# gerçekten yakın zamanlı olduğunu kod tarafında da doğruluyoruz.
HABER_MAKSIMUM_YAS_SAAT = 24


def _taze_mi(tarih) -> bool:
    if not tarih:
        return True  # tarih bilgisi yoksa reddetmiyoruz, aksi halde hiç haber geçemez
    yas = datetime.now(timezone.utc) - tarih
    return yas <= timedelta(hours=HABER_MAKSIMUM_YAS_SAAT)


# =============================================================================
# FİYAT TEPKİSİ KONTROLÜ
# =============================================================================
def _fiyat_tepki_kontrolu(hisse_kodu: str, haber_tarihi):
    """Haberden önceki kapanışa göre şu anki fiyatın % değişimini döner.
    None dönerse veri alınamamış demektir - o durumda temkinli davranıp
    yine de bildiriyoruz (emin olamadığımız için susturmuyoruz).
    (2026-09-17 düzeltmesi: BIST verisi saat dilimi BİLGİLİ (Europe/
    Istanbul) geliyor, haber_tarihi'nden saat dilimini SİLMEK yerine
    veri.index'in saat dilimine ÇEVİRİYORUZ - aksi halde pandas
    karşılaştıramayıp hata veriyor, bu yüzden kontrol hep başarısız
    oluyordu.)"""
    try:
        veri = yf.Ticker(hisse_kodu).history(period="10d")
        if veri.empty or len(veri) < 2:
            return None
        simdiki_fiyat = veri["Close"].iloc[-1]
        if haber_tarihi:
            karsilastirma = haber_tarihi
            if karsilastirma.tzinfo is None:
                karsilastirma = karsilastirma.replace(tzinfo=timezone.utc)
            if veri.index.tz is not None:
                karsilastirma = karsilastirma.astimezone(veri.index.tz)
            else:
                karsilastirma = karsilastirma.replace(tzinfo=None)
            oncesi = veri[veri.index < karsilastirma]
            referans_fiyat = oncesi["Close"].iloc[-1] if not oncesi.empty else veri["Close"].iloc[0]
        else:
            referans_fiyat = veri["Close"].iloc[0]
        if referans_fiyat == 0:
            return None
        return (simdiki_fiyat - referans_fiyat) / referans_fiyat * 100, simdiki_fiyat
    except Exception as e:
        print(f"[Katalizör] {hisse_kodu} fiyat kontrolü hatası: {e}", flush=True)
        return None


# =============================================================================
# BİLDİRİM
# =============================================================================
def _katalizor_bildir(hisse_kodu: str, haber: dict, degisim_pct, simdiki_fiyat):
    kod = hisse_kodu.replace(".IS", "")
    satirlar = [
        f"💡 KATALİZÖR — {kod}",
        f"📰 {haber['baslik']}",
    ]
    if degisim_pct is not None:
        satirlar.append(
            f"Haberden bu yana fiyat değişimi: %{degisim_pct:+.1f} "
            f"(şu an {simdiki_fiyat:.2f}) — henüz belirgin tepki yok")
    else:
        satirlar.append("⚠️ Fiyat verisi alınamadı, teyit edilemedi")
    if haber.get("link"):
        satirlar.append(haber["link"])
    satirlar.append(
        "\nℹ️ TEST EDİLMEMİŞ bir hipotez - 'iyi haber henüz fiyatlanmamış' "
        "fikri kanıtlanmış bir sistem değil, bir gözlem. Kendi araştırmanla teyit et.")
    send_telegram_message("\n".join(satirlar))


# =============================================================================
# ANA TARAMA DÖNGÜSÜ
# =============================================================================
def _hisse_tara(hisse_kodu: str, gorulen: dict):
    haberler = _haber_ara(hisse_kodu)
    haberler = [h for h in haberler if _taze_mi(h["tarih"])]
    if not haberler:
        return

    gorulmus = set(gorulen.get(hisse_kodu, []))
    ilk_calisma = hisse_kodu not in gorulen
    yeni_basliklar = []

    for h in haberler:
        anahtar = h["baslik"][:120]  # ayni haberi tekrar tekrar islemeyelim
        if anahtar in gorulmus:
            continue
        yeni_basliklar.append(anahtar)

        if ilk_calisma:
            continue  # ilk turda sessizce temel al, eski haberleri basma

        sinif = _haber_siniflandir(h["baslik"])
        if sinif != "pozitif":
            continue

        sonuc = _fiyat_tepki_kontrolu(hisse_kodu, h["tarih"])
        if sonuc is None:
            degisim_pct, simdiki_fiyat = None, None
        else:
            degisim_pct, simdiki_fiyat = sonuc
            if degisim_pct >= FIYAT_TEPKI_ESIGI_PCT:
                continue  # fiyat zaten tepki vermiş, fırsat kaçmış

        _katalizor_bildir(hisse_kodu, h, degisim_pct, simdiki_fiyat)

    if yeni_basliklar or ilk_calisma:
        birlesik = list(gorulmus) + yeni_basliklar
        gorulen[hisse_kodu] = birlesik[-50:]  # dosya şişmesin diye sınırlı tut


def katalizor_kontrol_dongusu():
    eksik = validate_katalizor_config()
    if eksik:
        print(f"[Katalizör] Eksik ayar: {eksik} - döngü başlatılmadı.", flush=True)
        return
    gorulen = _json_yukle(GORULEN_DOSYASI, {})
    tur = 0
    while True:
        for i, hisse in enumerate(BIST_HISSELER):
            try:
                _hisse_tara(hisse, gorulen)
            except Exception as e:
                print(f"[Katalizör] {hisse} tarama hatası: {e}", flush=True)
            if i % 10 == 0:
                _json_kaydet(GORULEN_DOSYASI, gorulen)
            time.sleep(TICKER_ARASI_BEKLEME_SN)
        _json_kaydet(GORULEN_DOSYASI, gorulen)
        tur += 1
        print(f"[Katalizör] Tur #{tur} tamamlandı ({len(BIST_HISSELER)} hisse).", flush=True)
        time.sleep(TARAMA_ARALIGI_SN)


def baslangic():
    eksik = validate_katalizor_config()
    if eksik:
        print(f"[Katalizör] Eksik ayar: {eksik}", flush=True)
        return
    send_telegram_message(
        f"💡 BIST Katalizör Tarayıcı AKTİF — {KATALIZOR_SURUM}\n"
        f"Takip edilen hisse sayısı: {len(BIST_HISSELER)}\n\n"
        "Bir hissede olumlu bir gelişme (kâr artışı, büyük sözleşme, "
        "kapasite artışı vb.) haber olur ve fiyat henüz belirgin tepki "
        f"vermemişse (< %{FIYAT_TEPKI_ESIGI_PCT:.0f}) bildirim gelecek.\n\n"
        "⚠️ Bu TEST EDİLMEMİŞ bir hipotez, kanıtlanmış bir sistem değil - "
        "kendi gözlemlerinle zamanla değerlendirmen gerekiyor.")
