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
import time
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
defteri (derinlik) ekran görüntüsü. Görüntüyü DİKKATLİCE incele; her
sayıyı okuduktan sonra kendi kendine kontrol et, emin olamadığın bir
rakam olursa görüntünün o bölgesine tekrar bak ve en doğru değeri yaz.
Aşağıdaki alanları SADECE JSON olarak çıkar, başka hiçbir metin ekleme:

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
Analizi (aracı kurum dağılımı) ekran görüntüsü. Görüntüyü DİKKATLİCE
incele; her sayıyı okuduktan sonra kendi kendine kontrol et, emin
olamadığın bir rakam olursa görüntünün o bölgesine tekrar bak ve en
doğru değeri yaz. Aşağıdaki alanları SADECE JSON olarak çıkar, başka
hiçbir metin ekleme:

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
    Sadece GERÇEKTEN geçici olan 503 (sunucu yoğunluğu) hatasında, az
    sayıda (2) ve kısa aralıkla tekrar dener. 429 (kota/limit dolu)
    hatasında TEKRAR DENEMEZ - kota tükenmişse denemek işe yaramaz,
    sadece kullanıcıya net mesaj verir. Diğer kalıcı hatalarda (404,
    400 vb.) da hemen durur."""
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

    GECICI_HATA_KODLARI = {503}  # sadece sunucu-taraflı geçici yoğunluk
    MAX_DENEME = 2               # kota tüketimini sınırlı tutmak için az
    BEKLEME_SN = 6

    son_hata = None
    for deneme in range(1, MAX_DENEME + 1):
        resp = requests.post(GEMINI_URL, json=body, timeout=45)
        if resp.status_code == 200:
            data = resp.json()
            metin = data["candidates"][0]["content"]["parts"][0]["text"]
            # Bazen model kod bloğu (```json ... ```) ile sarıyor - temizle.
            metin = re.sub(r"^```json\s*|\s*```$", "", metin.strip())
            return json.loads(metin)

        if resp.status_code == 429:
            # Kota/dakikalık limit dolmuş - tekrar denemek işe yaramaz,
            # sadece kalan kotayı boşuna tüketir. Hemen ve net bildir.
            raise RuntimeError(
                "Gemini istek kotası doldu (429) - bir süre bekleyip "
                "tekrar dene. Kota genelde dakikalık/günlük sıfırlanır.")

        son_hata = RuntimeError(f"Gemini API {resp.status_code}: {resp.text[:600]}")
        if resp.status_code in GECICI_HATA_KODLARI and deneme < MAX_DENEME:
            print(f"[Midas] Gemini geçici hata ({resp.status_code}), "
                  f"{deneme}/{MAX_DENEME} - {BEKLEME_SN}sn sonra tekrar denenecek",
                  flush=True)
            time.sleep(BEKLEME_SN)
            continue
        break

    raise son_hata


# =============================================================================
# EMİR DEFTERİ ANALİZİ — DUVAR TESPİTİ
# =============================================================================
def _duvar_bul(kademeler: list, taraf: str):
    """taraf='alis' -> destek (aşağı yön), taraf='satis' -> direnç (yukarı
    yön). GÖRÜNEN TÜM kademeler içinde en büyük lota sahip olanı döner -
    yani "ilk gördüğün ortadan büyükçe kademe" değil, gerçekten en kalın
    duvar hangisiyse o.
    (2026-09-15 düzeltmesi: eski sürüm önceki kademelerin ortalamasının
    1.5 katını geçen İLK kademede duruyordu - bu, tabloda daha derinde
    duran çok daha büyük bir duvarın hiç görülmemesine yol açıyordu,
    örn. ASELS'te 370.50'deki 39.359 lot "duvar" sayılıp hemen arkasındaki
    370.00'daki 107.484 lot hiç kontrol edilmemişti.)"""
    lot_alani = f"lot_{taraf}"
    fiyat_alani = f"fiyat_{taraf}"
    gecerli = [(k.get(fiyat_alani), k.get(lot_alani)) for k in kademeler
               if k.get(fiyat_alani) is not None and k.get(lot_alani) is not None]
    if not gecerli:
        return None
    en_buyuk_fiyat, en_buyuk_lot = max(gecerli, key=lambda x: x[1])
    return {"fiyat": en_buyuk_fiyat, "lot": en_buyuk_lot, "duvar_mi": True}


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
    """0-100 arası yön skoru. 50 nötr, 100'e yakın YUKARI, 0'a yakın
    AŞAĞI. Mevcut bileşenlerin ORTALAMASI alınır (kaçı varsa) - böylece
    takas eklenmesi diğer bileşenleri haksız yere ezmez, sadece ek bir
    oy olarak katılır. Bileşenler:
      1) Üstteki alış/satış oranı
      2) Destek/direnç duvarlarının büyüklük karşılaştırması
      3) Takas yönü (gönderildiyse)
    """
    katkilar = [(derinlik["alis_pct"] - 50)]  # -50..+50

    destek = derinlik.get("destek")
    direnc = derinlik.get("direnc")
    if destek and direnc and (destek["lot"] + direnc["lot"]) > 0:
        denge = (destek["lot"] - direnc["lot"]) / (destek["lot"] + direnc["lot"])  # -1..+1
        katkilar.append(denge * 50)  # -50..+50

    if takas:
        yon = max(-10.0, min(10.0, takas["yon_skoru"]))  # aşırı uçları kırp
        katkilar.append(yon * 5)  # -50..+50

    ortalama_katki = sum(katkilar) / len(katkilar)
    skor = 50 + ortalama_katki * 0.7  # 0.7: aşırı uçlara fazla hızlı gitmesin
    return max(0.0, min(100.0, skor))


def _yon_etiketi(skor: float) -> str:
    if skor >= 60:
        return "🔼 YUKARI eğilimli"
    if skor <= 40:
        return "🔽 AŞAĞI eğilimli"
    return "➖ NÖTR / belirsiz"


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
        satirlar.append("🏦 Takas: gönderilmedi (istersen ekle, yön daha güvenilir olur)")

    if derinlik:
        skor = _skor_hesapla(derinlik, takas)
        satirlar.append(f"\n<b>Kural bazlı okuma: {_yon_etiketi(skor)}</b> ({skor:.0f}/100)")
        gerekce = [f"Alış/satış oranı %{derinlik['alis_pct']:.0f}/%{derinlik['satis_pct']:.0f}"]
        if derinlik.get("destek") and derinlik.get("direnc"):
            d_lot, r_lot = derinlik["destek"]["lot"], derinlik["direnc"]["lot"]
            if d_lot > r_lot:
                gerekce.append("destek duvarı direnç duvarından daha kalın")
            elif r_lot > d_lot:
                gerekce.append("direnç duvarı destek duvarından daha kalın")
            else:
                gerekce.append("destek/direnç duvarları dengeli")
        if takas:
            gerekce.append(f"takas: {takas['yon']}")
        satirlar.append("Gerekçe: " + ", ".join(gerekce) + ".")

    satirlar.append(
        "\nℹ️ Bu bir TEST EDİLMEMİŞ kural, kanıtlanmış bir sistem değil - "
        "tek bir anlık görüntüden çıkarıldı, emir defteri saniyeler içinde "
        "değişebilir. Bir görüş, garanti değil.")
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


def _fotograf_isle_arkaplan(hisse: str, tip: str, file_id: str):
    """_fotograf_isle'yi ayrı bir thread'de çalıştırır - indirme ve Gemini
    çağrısı (tekrar denemelerle birlikte) burada, arge_botu'nun Telegram
    döngüsünden bağımsız olarak yürütülür."""
    try:
        img_bytes = _telegram_dosya_indir(file_id)
    except Exception as e:
        send_midas_message(f"❌ {hisse} — fotoğraf indirilemedi: {e}")
        return
    _fotograf_isle(hisse, tip, img_bytes)


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
        # Gemini'nin yanıtı (özellikle yoğunluk anında tekrar denemelerle)
        # birkaç saniye sürebilir - bunu AYRI bir thread'de yapıyoruz ki
        # arge_botu'nun Telegram döngüsü bu süre boyunca hiç bloklanmasın.
        threading.Thread(target=_fotograf_isle_arkaplan,
                          args=(hisse, tip, file_id), daemon=True).start()
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
