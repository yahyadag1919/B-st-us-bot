"""
midas_takip.py — MIDAS PRO MANUEL DERİNLİK/TAKAS OKUYUCU
============================================================
2026-09-10 — Kullanıcının kararıyla kuruldu.

NE İŞE YARAR:
Kullanıcı günün HERHANGİ bir saatinde, Midas Pro uygulamasında ilgisini
çeken bir hissenin Derinlik (10 kademe) ve/veya Takas Analizi (ilk 5
aracı kurum dağılımı) ekran görüntüsünü Telegram'a atar. Bot, Gemini
Vision API ile görüntüyü okuyup sayısal veriye çevirir, emir defterindeki
"duvarları" (anormal büyük kademeleri) tespit ederek olası destek/direnç
seviyelerini çıkarır, takas verisindeki kurumsal yön (birikim/dağıtım)
ile birleştirip 0-100 arası bir "Alıcı Gücü Skoru" ile birlikte anlık
rapor döner.

ÖNEMLİ - NE YAPMAZ:
Bu bot "hisse şu fiyata kadar düşer, sonra şuraya çıkar" şeklinde KESİN
bir tahmin motoru DEĞİL. Tek bir anlık ekran görüntüsünden çalışıyor;
emir defteri saniyeler içinde değişebilir, büyük emirler gerçek niyet
olmadan da girilip çekilebilir (spoofing). Bot sadece "şu an, şu ekran
görüntüsüne göre" hangi seviyelerin önemli olduğunu okuyor. Nihai al/sat
kararı ve zamanlaması kullanıcıya ait.

CAPTION FORMATI:
  "THYAO"     -> Derinlik ekran görüntüsü (varsayılan tip)
  "THYAO d"   -> Derinlik ekran görüntüsü (açık)
  "THYAO t"   -> Takas Analizi ekran görüntüsü
İkisi de aynı hisse için 30 dk içinde gelirse otomatik birleştirilir.

TELEGRAM AKIŞI (2026-09-10 DÜZELTMESİ): Bu modül KENDİ getUpdates
döngüsünü AÇMIYOR. Aynı bot token'ından (ARGE_TELEGRAM_TOKEN) zaten
arge_botu.py'nin poll_arge_commands() döngüsü update çekiyor - ikinci
bir eşzamanlı getUpdates isteği Telegram'da "409 Conflict" hatasına
yol açar ve tavan tarayıcının komutlarını kaçırmasına sebep olabilirdi.
Bunun yerine arge_botu.ek_update_isleyici_ekle() ile bu modülün
midas_update_isle() fonksiyonu arge'nin mevcut döngüsüne kaydediliyor
(bkz. ana.py). Fotoğraf/analiz mantığı tamamen aynı, sadece update'in
NEREDEN geldiği değişti.
"""
import os
import re
import csv
import json
import base64
import threading
from datetime import datetime, timedelta

import requests

# =============================================================================
# YAPILANDIRMA
# =============================================================================
# NOT: BIST tavan tarayıcısıyla (arge_botu.py) AYNI Telegram bot/sohbeti
# - ARGE_TELEGRAM_TOKEN/ARGE_TELEGRAM_CHAT_ID (TELEGRAM_TOKEN DEĞİL -
# o, us_sinyal_botu.py'nin ayrı ve ana.py'de hiç başlatılmayan kendi
# komut döngüsüne ait, kullanılmıyor).
TELEGRAM_TOKEN = os.environ.get("ARGE_TELEGRAM_TOKEN", "")
TELEGRAM_CHAT_ID = os.environ.get("ARGE_TELEGRAM_CHAT_ID", "")
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY", "")
DATA_DIR = os.environ.get("DATA_DIR", ".")

# NOT: Google, "gemini-2.5-flash" modelini yeni kullanıcılar için
# kapattı (2026-09-10, bot hatasından öğrenildi - Google'ın kendi mesajı:
# "This model models/gemini-2.5-flash is no longer available to new
# users. Please update your code to use models/gemini-3.6-flash").
# Sorun tekrar ederse: https://generativelanguage.googleapis.com/v1beta/models?key=ANAHTAR
GEMINI_MODEL = os.environ.get("MIDAS_GEMINI_MODEL", "gemini-3.6-flash")
GEMINI_URL = (f"https://generativelanguage.googleapis.com/v1beta/models/"
              f"{GEMINI_MODEL}:generateContent?key={GEMINI_API_KEY}")

MIDAS_SURUM = "midas-takip-v2-tek-getupdates-2026-09-10"
KAYIT_CSV = os.path.join(DATA_DIR, "midas_takip_kayitlari.csv")

# Aynı hissenin derinlik + takas görüntüsü ne kadar süre içinde gelirse
# birleştirilecek (dakika).
BIRLESTIRME_PENCERESI_DK = 30

# Bir kademeyi "duvar" saymak için: önceki kademelerin ortalamasının
# kaç katı olmalı. Kullanıcı zamanla ayarlayabilir.
DUVAR_ESIGI_KAT = float(os.environ.get("MIDAS_DUVAR_ESIGI_KAT", "1.5"))

# Takas yorumunda "yabancı kurum" sayılan proxy liste (taslak - piyasa
# tecrübesine göre düzeltilebilir).
YABANCI_KURUMLAR = {
    "citibank", "hsbc", "ubs", "merrill lynch", "jpmorgan", "jp morgan",
    "deutsche", "goldman sachs", "morgan stanley", "credit suisse",
    "bnp paribas", "societe generale",
}

REQUIRED_ENV_VARS = ["TELEGRAM_TOKEN", "TELEGRAM_CHAT_ID", "GEMINI_API_KEY"]


def validate_midas_config():
    return [name for name in REQUIRED_ENV_VARS if not os.environ.get(name)]


# =============================================================================
# TELEGRAM YARDIMCI FONKSİYONLAR
# =============================================================================
def send_midas_message(text: str):
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        print(f"[Midas devre dışı] {text}", flush=True)
        return
    try:
        requests.post(
            f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage",
            json={"chat_id": TELEGRAM_CHAT_ID, "text": text, "parse_mode": "HTML"},
            timeout=15,
        )
    except Exception as e:
        print(f"[Midas Telegram hata] {e}", flush=True)


def _telegram_dosya_indir(file_id: str) -> bytes:
    """Telegram'daki bir fotoğrafı ham byte olarak indirir."""
    r = requests.get(
        f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/getFile",
        params={"file_id": file_id}, timeout=15)
    r.raise_for_status()
    file_path = r.json()["result"]["file_path"]
    dosya_url = f"https://api.telegram.org/file/bot{TELEGRAM_TOKEN}/{file_path}"
    r2 = requests.get(dosya_url, timeout=20)
    r2.raise_for_status()
    return r2.content


# =============================================================================
# GEMINI VISION — GÖRÜNTÜDEN VERİ ÇIKARMA
# =============================================================================
PROMPT_DERINLIK = """Bu bir Midas Pro uygulamasından BIST hissesi emir
defteri (derinlik) ekran görüntüsü. Aşağıdaki alanları SADECE JSON olarak
çıkar, başka hiçbir metin ekleme:

{
  "piyasa_fiyati": <sayı>,
  "alis_toplam_lot": <sayı>,
  "satis_toplam_lot": <sayı>,
  "kademeler": [
    {"emir_alis": <sayı>, "lot_alis": <sayı>, "fiyat_alis": <sayı>,
     "fiyat_satis": <sayı>, "lot_satis": <sayı>, "emir_satis": <sayı>},
    ... (görüntüdeki TÜM kademeler, üstten alta sırayla)
  ]
}

Sayılardaki nokta/virgül Türkçe format olabilir (286,50 = 286.50,
36.347 = 36347) - bunları normal ondalık sayıya çevir. Bir alanı
okuyamıyorsan null koy, alanı atlama."""

PROMPT_TAKAS = """Bu bir Midas Pro uygulamasından BIST hissesi Takas
Analizi (aracı kurum dağılımı) ekran görüntüsü. Aşağıdaki alanları
SADECE JSON olarak çıkar, başka hiçbir metin ekleme:

{
  "ilk5_toplam_pct": <sayı>,
  "diger_toplam_pct": <sayı>,
  "kurumlar": [
    {"kurum": "<isim>", "toplam_lot": <sayı>, "dagilim_pct": <sayı>,
     "degisim_pct": <sayı, negatifse eksi işaretiyle>},
    ... (görüntüdeki TÜM kurumlar)
  ]
}

Sayılardaki nokta/virgül Türkçe format olabilir - normal ondalık sayıya
çevir. Bir alanı okuyamıyorsan null koy, alanı atlama."""


def _gemini_gorsel_oku(image_bytes: bytes, prompt: str) -> dict:
    """Görüntüyü Gemini Vision'a gönderip yapılandırılmış JSON döner.
    Hata durumunda None döner, çağıran taraf kullanıcıya bildirmeli."""
    if not GEMINI_API_KEY:
        raise RuntimeError("GEMINI_API_KEY tanımlı değil.")
    b64 = base64.b64encode(image_bytes).decode("utf-8")
    body = {
        "contents": [{
            "parts": [
                {"text": prompt},
                {"inline_data": {"mime_type": "image/jpeg", "data": b64}},
            ]
        }],
        "generationConfig": {"response_mime_type": "application/json"},
    }
    resp = requests.post(GEMINI_URL, json=body, timeout=45)
    if resp.status_code != 200:
        # Google'ın gönderdiği GERÇEK hata açıklamasını (sadece durum kodu
        # değil) göster - "404 Not Found" gibi genel başlıklar yerine asıl
        # sebebi (API_KEY_INVALID, model devre dışı, kota aşımı vb.) net
        # olarak Telegram'a düşürüyor.
        raise RuntimeError(f"Gemini API {resp.status_code}: {resp.text[:600]}")
    data = resp.json()
    metin = data["candidates"][0]["content"]["parts"][0]["text"]
    # Bazen model kod bloğu (```json ... ```) ile sarıyor - temizle.
    metin = re.sub(r"^```json\s*|\s*```$", "", metin.strip())
    return json.loads(metin)


# =============================================================================
# EMİR DEFTERİ ANALİZİ — DUVAR TESPİTİ
# =============================================================================
def _duvar_bul(kademeler: list, taraf: str):
    """taraf='alis' -> destek (aşağı yön), taraf='satis' -> direnç (yukarı
    yön). Kademeler listesi, fiyata en yakından en uzağa sıralı olmalı.
    İlk 'duvar'ı (önceki kademelerin ortalamasının DUVAR_ESIGI_KAT katı
    veya üzeri) bulunca durur. Duvar yoksa en derin kademeyi döner."""
    lot_alani = f"lot_{taraf}"
    fiyat_alani = f"fiyat_{taraf}"
    gecmis_lotlar = []
    for k in kademeler:
        lot = k.get(lot_alani)
        fiyat = k.get(fiyat_alani)
        if lot is None or fiyat is None:
            continue
        if gecmis_lotlar:
            ortalama = sum(gecmis_lotlar) / len(gecmis_lotlar)
            if lot >= ortalama * DUVAR_ESIGI_KAT:
                return {"fiyat": fiyat, "lot": lot, "duvar_mi": True}
        gecmis_lotlar.append(lot)
    if kademeler:
        son = kademeler[-1]
        return {"fiyat": son.get(fiyat_alani), "lot": son.get(lot_alani),
                "duvar_mi": False}
    return None


def _derinlik_analiz_et(veri: dict) -> dict:
    kademeler = veri.get("kademeler") or []
    destek = _duvar_bul(kademeler, "alis")
    direnc = _duvar_bul(kademeler, "satis")
    alis_lot = veri.get("alis_toplam_lot") or 0
    satis_lot = veri.get("satis_toplam_lot") or 0
    toplam = alis_lot + satis_lot
    alis_pct = (alis_lot / toplam * 100) if toplam else 50.0
    return {
        "piyasa_fiyati": veri.get("piyasa_fiyati"),
        "destek": destek,
        "direnc": direnc,
        "alis_pct": alis_pct,
        "satis_pct": 100 - alis_pct,
    }


# =============================================================================
# TAKAS ANALİZİ — KURUMSAL YÖN
# =============================================================================
def _takas_analiz_et(veri: dict) -> dict:
    kurumlar = veri.get("kurumlar") or []
    if not kurumlar:
        return {"yon": "veri yok", "yon_skoru": 0.0, "one_cikan": None}

    agirlikli_toplam = 0.0
    agirlik_toplami = 0.0
    for k in kurumlar:
        pay = k.get("dagilim_pct") or 0
        degisim = k.get("degisim_pct")
        if degisim is None:
            continue
        agirlikli_toplam += pay * degisim
        agirlik_toplami += pay
    yon_skoru = (agirlikli_toplam / agirlik_toplami) if agirlik_toplami else 0.0

    if yon_skoru > 0.3:
        yon = "birikim (kurumsal payı artıyor)"
    elif yon_skoru < -0.3:
        yon = "dağıtım (kurumsal payı azalıyor)"
    else:
        yon = "nötr, belirgin yön yok"

    # En büyük paya sahip kurumu ve yabancı/yerli proxy notunu bul.
    one_cikan = None
    if kurumlar:
        en_buyuk = max(kurumlar, key=lambda k: k.get("dagilim_pct") or 0)
        isim = (en_buyuk.get("kurum") or "").strip()
        proxy = "yabancı" if isim.lower() in YABANCI_KURUMLAR else "yerli"
        one_cikan = f"{isim} (%{en_buyuk.get('dagilim_pct', 0):.1f}, {proxy} proxy)"

    return {"yon": yon, "yon_skoru": yon_skoru, "one_cikan": one_cikan}


# =============================================================================
# SKOR VE RAPOR
# =============================================================================
def _skor_hesapla(derinlik: dict, takas: dict = None) -> float:
    """0-100 arası Alıcı Gücü Skoru. 50 nötr. Şeffaf/basit formül -
    her bileşenin katkısı ayrı ayrı görülebilir olsun diye kasıtlı
    karmaşık değil."""
    skor = 50.0
    skor += (derinlik["alis_pct"] - 50) * 0.6
    if takas:
        skor += takas["yon_skoru"] * 15
    return max(0.0, min(100.0, skor))


def _rapor_olustur(hisse: str, derinlik: dict = None, takas: dict = None) -> str:
    satirlar = [f"📊 <b>{hisse}</b> — Midas Analiz"]

    if derinlik:
        satirlar.append(f"Mevcut fiyat: {derinlik['piyasa_fiyati']}")
        if derinlik["destek"]:
            etiket = "duvar" if derinlik["destek"]["duvar_mi"] else "en derin kademe"
            satirlar.append(
                f"🔻 Destek: {derinlik['destek']['fiyat']} "
                f"({derinlik['destek']['lot']:,.0f} lot {etiket})".replace(",", "."))
        if derinlik["direnc"]:
            etiket = "duvar" if derinlik["direnc"]["duvar_mi"] else "en derin kademe"
            satirlar.append(
                f"🔺 Direnç: {derinlik['direnc']['fiyat']} "
                f"({derinlik['direnc']['lot']:,.0f} lot {etiket})".replace(",", "."))
        satirlar.append(
            f"Alış/Satış oranı: %{derinlik['alis_pct']:.1f} / %{derinlik['satis_pct']:.1f}")
    else:
        satirlar.append("⚠️ Derinlik verisi yok — sadece takas ile sınırlı yorum.")

    if takas:
        oc = f" ({takas['one_cikan']})" if takas["one_cikan"] else ""
        satirlar.append(f"🏦 Takas: {takas['yon']}{oc}")
    else:
        satirlar.append("🏦 Takas: gönderilmedi (istersen ekle, skor güçlenir)")

    if derinlik:
        skor = _skor_hesapla(derinlik, takas)
        satirlar.append(f"⚡ Alıcı Gücü Skoru: {skor:.0f}/100")

    satirlar.append(
        "\nℹ️ Tek bir anlık görüntüden çıkarıldı - emir defteri saniyeler "
        "içinde değişebilir. Kesin hedef değil, referans seviyeleri.")
    return "\n".join(satirlar)


# =============================================================================
# BEKLEYEN KAYITLAR — DERİNLİK + TAKAS EŞLEŞTİRME
# =============================================================================
_bekleyenler_kilit = threading.Lock()
_bekleyenler = {}  # {HISSE: {"derinlik": {...,"ts":dt}, "takas": {...,"ts":dt}}}


def _kayit_csv_yaz(hisse, derinlik, takas):
    yeni = not os.path.exists(KAYIT_CSV)
    try:
        with open(KAYIT_CSV, "a", newline="", encoding="utf-8") as f:
            w = csv.writer(f)
            if yeni:
                w.writerow(["zaman", "hisse", "piyasa_fiyati", "destek",
                            "direnc", "alis_pct", "takas_yon"])
            w.writerow([
                datetime.now().isoformat(timespec="seconds"), hisse,
                derinlik["piyasa_fiyati"] if derinlik else "",
                derinlik["destek"]["fiyat"] if derinlik and derinlik["destek"] else "",
                derinlik["direnc"]["fiyat"] if derinlik and derinlik["direnc"] else "",
                f"{derinlik['alis_pct']:.1f}" if derinlik else "",
                takas["yon"] if takas else "",
            ])
    except OSError as e:
        print(f"[Midas] CSV yazılamadı: {e}", flush=True)


def _fotograf_isle(hisse: str, tip: str, image_bytes: bytes):
    hisse = hisse.upper()
    try:
        if tip == "derinlik":
            ham = _gemini_gorsel_oku(image_bytes, PROMPT_DERINLIK)
            islenmis = _derinlik_analiz_et(ham)
        else:
            ham = _gemini_gorsel_oku(image_bytes, PROMPT_TAKAS)
            islenmis = _takas_analiz_et(ham)
    except Exception as e:
        send_midas_message(f"❌ {hisse} — görüntü okunamadı: {e}")
        return

    with _bekleyenler_kilit:
        kayit = _bekleyenler.setdefault(hisse, {})
        kayit[tip] = {"veri": islenmis, "ts": datetime.now()}

        derinlik_kayit = kayit.get("derinlik")
        takas_kayit = kayit.get("takas")
        pencere = timedelta(minutes=BIRLESTIRME_PENCERESI_DK)

        derinlik_veri = None
        takas_veri = None
        if derinlik_kayit and datetime.now() - derinlik_kayit["ts"] <= pencere:
            derinlik_veri = derinlik_kayit["veri"]
        if takas_kayit and datetime.now() - takas_kayit["ts"] <= pencere:
            takas_veri = takas_kayit["veri"]

    if derinlik_veri is None and takas_veri is None:
        return  # olmamalı ama savunma amaçlı

    rapor = _rapor_olustur(hisse, derinlik_veri, takas_veri)
    send_midas_message(rapor)
    if derinlik_veri:
        _kayit_csv_yaz(hisse, derinlik_veri, takas_veri)


def _caption_ayristir(caption: str):
    """'THYAO', 'THYAO d', 'THYAO derinlik', 'THYAO t', 'THYAO takas'
    formatlarını ayrıştırır. (hisse, tip) döner, anlaşılamazsa None."""
    if not caption:
        return None
    parcalar = caption.strip().split()
    if not parcalar:
        return None
    hisse = parcalar[0].upper()
    if not re.match(r"^[A-Z0-9]{2,10}$", hisse):
        return None
    tip = "derinlik"
    if len(parcalar) > 1:
        ikinci = parcalar[1].lower()
        if ikinci.startswith("t"):
            tip = "takas"
        elif ikinci.startswith("d"):
            tip = "derinlik"
    return hisse, tip


# =============================================================================
# TEK UPDATE İŞLEYİCİ — arge_botu'nun getUpdates döngüsüne kaydedilir
# =============================================================================
def midas_update_isle(update: dict):
    """arge_botu.ek_update_isleyici_ekle() ile kaydedilir; arge_botu'nun
    HER Telegram update'inde (arge kendi filtrelerini uygulamadan önce)
    çağrılır. Kendi getUpdates/offset yönetimi YOK - 409 Conflict riski
    ortadan kalkıyor. Hata verirse arge_botu bunu yakalayıp loglar,
    tavan tarayıcıyı etkilemez."""
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        return
    mesaj = update.get("message", {})
    chat_id = str(mesaj.get("chat", {}).get("id", ""))
    if chat_id != str(TELEGRAM_CHAT_ID):
        return

    fotograflar = mesaj.get("photo")
    if fotograflar:
        caption = mesaj.get("caption", "")
        ayrisim = _caption_ayristir(caption)
        if not ayrisim:
            send_midas_message(
                "⚠️ Fotoğrafı hisse koduyla gönder, örn: "
                "\"THYAO\" (derinlik) veya \"THYAO t\" (takas)")
            return
        hisse, tip = ayrisim
        # en büyük boyutlu fotoğrafı al (liste küçükten büyüğe sıralı)
        file_id = fotograflar[-1]["file_id"]
        try:
            img_bytes = _telegram_dosya_indir(file_id)
        except Exception as e:
            send_midas_message(f"❌ {hisse} — fotoğraf indirilemedi: {e}")
            return
        _fotograf_isle(hisse, tip, img_bytes)
        return

    text = (mesaj.get("text") or "").strip().lower()
    if text.startswith("/midas_durum"):
        with _bekleyenler_kilit:
            n = len(_bekleyenler)
        send_midas_message(
            f"💗 Midas takip çalışıyor ({MIDAS_SURUM})\n"
            f"Bellekte {n} hisse için bekleyen kayıt var.")


def midas_baslangic():
    eksik = validate_midas_config()
    if eksik:
        print(f"[Midas] Eksik ayar: {eksik}", flush=True)
        return
    send_midas_message(
        f"📊 Midas Pro Takip Sistemi AKTİF — {MIDAS_SURUM}\n\n"
        f"Herhangi bir hissenin Midas Derinlik ve/veya Takas Analizi "
        f"ekran görüntüsünü caption'a hisse kodunu yazarak gönder:\n"
        f'  "THYAO" -> derinlik (varsayılan)\n'
        f'  "THYAO t" -> takas\n'
        f"İkisini {BIRLESTIRME_PENCERESI_DK} dk içinde gönderirsen otomatik "
        f"birleştirip tam rapor veririm.\n\n"
        f"Komut: /midas_durum")
