"""
bist_kapsamli_analiz.py — BIST KAPSAMLI ANOMALİ + MAKRO OLAY RAPORU
==============================================================================
2026-09-17 — Kullanıcının isteğiyle kuruldu.

Bu modül CANLI sinyal ÜRETMİYOR - tek seferlik, komutla tetiklenen bir
analiz raporu üretiyor ve Telegram'a DOSYA olarak (mesaj değil) gönderiyor.

3 BÖLÜM:
1) Fiyat-Hacim Anomali Taraması — hacim düşükken sert fiyat hareketi olan
   günleri buluyor ("az emirle büyük hareket" = şüpheli kırıntı, kesin
   kanıt değil).
2) TCMB faiz kararları sonrası BIST100 tepkisi.
3) Fed (ABD) faiz kararları sonrası BIST100 tepkisi.

⚠️ ÖNEMLİ: TCMB_KARARLARI ve FED_KARARLARI listelerindeki tarih/yönler
Claude'un EĞİTİM VERİSİNDEN HATIRLANMIŞTIR - TCMB/Fed'in resmi duyuru
sayfalarıyla TEYİT EDİLMEMİŞTİR. Özellikle 2025 sonrası kararlar için
kesinlik garanti edilmez. Rapor bunu açıkça belirtiyor.

TELEGRAM: arge_botu.py'nin (BIST tavan tarayıcı) MEVCUT Telegram
döngüsüne "misafir" olarak bağlanıyor (ek_update_isleyici_ekle) - kendi
getUpdates döngüsünü AÇMIYOR, 409 Conflict riski yok. Aynı sohbete
(ARGE_TELEGRAM_TOKEN/CHAT_ID) yazıyor.
"""
import os
import time
import threading
from datetime import datetime, timezone

import requests
import pandas as pd
import yfinance as yf

TELEGRAM_TOKEN = os.environ.get("ARGE_TELEGRAM_TOKEN", "")
TELEGRAM_CHAT_ID = os.environ.get("ARGE_TELEGRAM_CHAT_ID", "")
DATA_DIR = os.environ.get("DATA_DIR", ".")

KAPSAMLI_SURUM = "bist-kapsamli-analiz-v1-2026-09-17"

_kilit = threading.Lock()
_calisiyor = {"rapor": False, "finans_iliski": False}


# =============================================================================
# TELEGRAM
# =============================================================================
def send_telegram_message(text: str):
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        print(f"[KapsamlıAnaliz devre dışı] {text}", flush=True)
        return
    try:
        requests.post(
            f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage",
            json={"chat_id": TELEGRAM_CHAT_ID, "text": text[:4000]}, timeout=20)
    except Exception as e:
        print(f"[KapsamlıAnaliz Telegram hata] {e}", flush=True)


def send_telegram_document(dosya_yolu: str, caption: str = ""):
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        print(f"[KapsamlıAnaliz devre dışı] dosya: {dosya_yolu}", flush=True)
        return
    try:
        with open(dosya_yolu, "rb") as f:
            requests.post(
                f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendDocument",
                data={"chat_id": TELEGRAM_CHAT_ID, "caption": caption[:1024]},
                files={"document": f}, timeout=60)
    except Exception as e:
        print(f"[KapsamlıAnaliz] Dosya gönderilemedi: {e}", flush=True)


# =============================================================================
# HİSSE LİSTESİ — arge_botu.py'den METİN olarak okunuyor (canlı import YOK)
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
        print(f"[KapsamlıAnaliz] BIST_HISSELER okunamadı, varsayılan liste: {e}", flush=True)
    return varsayilan


BIST_HISSELER = _load_bist_hisseler()


# =============================================================================
# 1) FİYAT-HACİM ANOMALİ TARAMASI
# =============================================================================
ANOMALI_PERIYOD = "1y"
ANOMALI_MIN_HAREKET_PCT = 5.0    # bu ve üstü günlük hareket "sert" sayılır
ANOMALI_MAX_HACIM_ORANI = 1.2    # ortalamanın bu katından AZ hacim "düşük" sayılır
ANOMALI_MAX_SONUC = 40           # rapora en çarpıcı kaç örnek girecek


def _fiyat_hacim_bolumu() -> str:
    sonuclar = []
    for i, ticker in enumerate(BIST_HISSELER):
        try:
            df = yf.Ticker(ticker).history(period=ANOMALI_PERIYOD)
        except Exception:
            continue
        if df.empty or len(df) < 30:
            continue
        df = df.copy()
        df["getiri_pct"] = df["Close"].pct_change() * 100
        df["ort_hacim20"] = df["Volume"].shift(1).rolling(20).mean()
        for idx in range(21, len(df)):
            getiri = df["getiri_pct"].iloc[idx]
            ort_hacim = df["ort_hacim20"].iloc[idx]
            hacim = df["Volume"].iloc[idx]
            if pd.isna(getiri) or pd.isna(ort_hacim) or ort_hacim == 0:
                continue
            hacim_orani = hacim / ort_hacim
            if abs(getiri) >= ANOMALI_MIN_HAREKET_PCT and hacim_orani <= ANOMALI_MAX_HACIM_ORANI:
                sonuclar.append({
                    "hisse": ticker.replace(".IS", ""),
                    "tarih": df.index[idx].strftime("%Y-%m-%d"),
                    "getiri": getiri,
                    "hacim_orani": hacim_orani,
                })
        if i % 20 == 0:
            print(f"[KapsamlıAnaliz] Anomali taraması {i}/{len(BIST_HISSELER)}", flush=True)
        time.sleep(0.3)

    sonuclar.sort(key=lambda x: abs(x["getiri"]), reverse=True)
    sonuclar = sonuclar[:ANOMALI_MAX_SONUC]

    if not sonuclar:
        return "Kriterlere uyan anomali bulunamadı."

    satirlar = ["| Hisse | Tarih | Günlük Hareket | Hacim (ort. 20 günün katı) |",
                "|---|---|---|---|"]
    for s in sonuclar:
        satirlar.append(
            f"| {s['hisse']} | {s['tarih']} | %{s['getiri']:+.1f} | {s['hacim_orani']:.2f}x |")
    return "\n".join(satirlar)


# =============================================================================
# 2) ve 3) FAİZ KARARLARI — ⚠️ TARİH/YÖNLER EĞİTİM VERİSİNDEN HATIRLANMIŞTIR
# =============================================================================
TCMB_KARARLARI = [
    ("2023-06-22", "artis"), ("2023-07-20", "artis"), ("2023-08-24", "artis"),
    ("2023-09-21", "artis"), ("2023-10-26", "artis"), ("2023-11-23", "artis"),
    ("2023-12-21", "artis"), ("2024-01-25", "artis"), ("2024-02-22", "sabit"),
    ("2024-03-21", "artis"), ("2024-04-25", "sabit"), ("2024-05-23", "sabit"),
    ("2024-06-27", "sabit"), ("2024-07-25", "sabit"), ("2024-08-22", "sabit"),
    ("2024-09-19", "sabit"), ("2024-10-17", "sabit"), ("2024-11-21", "sabit"),
    ("2024-12-26", "indirim"), ("2025-01-23", "indirim"),
]

FED_KARARLARI = [
    ("2023-06-14", "sabit"), ("2023-07-26", "artis"), ("2023-09-20", "sabit"),
    ("2023-11-01", "sabit"), ("2023-12-13", "sabit"), ("2024-01-31", "sabit"),
    ("2024-03-20", "sabit"), ("2024-05-01", "sabit"), ("2024-06-12", "sabit"),
    ("2024-07-31", "sabit"), ("2024-09-18", "indirim"), ("2024-11-07", "indirim"),
    ("2024-12-18", "indirim"),
]

FAIZ_UFUK_GUNLERI = [0, 1, 3]


def _fiyat_serisi(ticker: str, periyod: str):
    try:
        df = yf.Ticker(ticker).history(period=periyod)
        if df.empty:
            return None
        s = df["Close"]
        if s.index.tz is not None:
            s.index = s.index.tz_localize(None)
        return s
    except Exception:
        return None


def _karar_sonrasi_getiri(fiyat_serisi, karar_tarihi, gun_sonra):
    sonraki = fiyat_serisi[fiyat_serisi.index >= karar_tarihi]
    if sonraki.empty:
        return None
    baz_konum = fiyat_serisi.index.get_loc(sonraki.index[0])
    if isinstance(baz_konum, slice):  # aynı tarihte birden fazla kayıt olursa
        baz_konum = baz_konum.start
    if baz_konum == 0:
        return None
    baz_fiyat = fiyat_serisi.iloc[baz_konum - 1]
    hedef_konum = baz_konum + gun_sonra
    if hedef_konum >= len(fiyat_serisi) or baz_fiyat == 0:
        return None
    hedef_fiyat = fiyat_serisi.iloc[hedef_konum]
    return (hedef_fiyat - baz_fiyat) / baz_fiyat * 100


# =============================================================================
# 4) FİNANSAL OKURYAZARLIK TESTİ — "herkesin bildiği" piyasa ilişkileri
# gerçekten geçerli mi?
# =============================================================================
FINANSAL_ILISKI_PERIYOD = "2y"

FINANSAL_ILISKILER = [
    # (x_ticker, x_isim, y_ticker, y_isim, beklenti: "ayni"/"ters")
    ("DX-Y.NYB", "Dolar Endeksi", "GC=F", "Altın", "ters"),
    ("^TNX", "ABD 10Y Tahvil Faizi", "GC=F", "Altın", "ters"),
    ("^TNX", "ABD 10Y Tahvil Faizi", "QQQ", "Nasdaq/Büyüme Hisseleri", "ters"),
    ("^TNX", "ABD 10Y Tahvil Faizi", "XLF", "ABD Bankacılık Sektörü", "ayni"),
    ("CL=F", "Ham Petrol", "XLE", "ABD Enerji Sektörü", "ayni"),
    ("CL=F", "Ham Petrol", "DAL", "Havayolu (Delta)", "ters"),
    ("DX-Y.NYB", "Dolar Endeksi", "EEM", "Gelişen Piyasalar", "ters"),
    ("DX-Y.NYB", "Dolar Endeksi", "XU100.IS", "BIST100", "ters"),
    ("^VIX", "VIX (Korku Endeksi)", "^GSPC", "S&P 500", "ters"),
    ("BTC-USD", "Bitcoin", "GC=F", "Altın", "ayni"),
    ("GC=F", "Altın", "XU100.IS", "BIST100", "ayni"),
]

FINANSAL_ILISKI_KORELASYON_ESIGI = 0.15  # bunun altı "zayıf/yok" sayılır


def _getiri_serisi(ticker: str, periyod: str):
    try:
        df = yf.Ticker(ticker).history(period=periyod)
        if df.empty:
            return None
        s = df["Close"].pct_change() * 100
        if s.index.tz is not None:
            s.index = s.index.tz_localize(None)
        return s.dropna()
    except Exception:
        return None


def _iliski_testi(x_ticker, x_isim, y_ticker, y_isim, beklenti) -> str:
    x = _getiri_serisi(x_ticker, FINANSAL_ILISKI_PERIYOD)
    y = _getiri_serisi(y_ticker, FINANSAL_ILISKI_PERIYOD)
    if x is None or y is None:
        return f"**{x_isim} → {y_isim}**: veri alınamadı ({x_ticker} ve/veya {y_ticker})\n"

    birlesik = pd.DataFrame({"x": x, "y": y}).dropna()
    if len(birlesik) < 30:
        return f"**{x_isim} → {y_isim}**: örnek yetersiz (n={len(birlesik)})\n"

    kor = birlesik["x"].corr(birlesik["y"])
    x_yukselince = birlesik[birlesik["x"] > 0]
    x_dusunce = birlesik[birlesik["x"] < 0]

    if beklenti == "ayni":
        dogrulandi = kor > FINANSAL_ILISKI_KORELASYON_ESIGI
        beklenti_tr = "aynı yönde hareket"
    else:
        dogrulandi = kor < -FINANSAL_ILISKI_KORELASYON_ESIGI
        beklenti_tr = "ters yönde hareket"
    isaret = "✅ Beklenti doğrulandı" if dogrulandi else "❌ Beklenti doğrulanmadı / ilişki zayıf"

    return (
        f"**{x_isim} → {y_isim}** (klasik beklenti: {beklenti_tr})\n"
        f"  n={len(birlesik)}, korelasyon={kor:+.2f} — {isaret}\n"
        f"  {x_isim} yükselince → {y_isim} ort. %{x_yukselince['y'].mean():+.2f} (n={len(x_yukselince)})\n"
        f"  {x_isim} düşünce → {y_isim} ort. %{x_dusunce['y'].mean():+.2f} (n={len(x_dusunce)})\n")


def _finansal_okuryazarlik_bolumu() -> str:
    satirlar = []
    for x_t, x_i, y_t, y_i, beklenti in FINANSAL_ILISKILER:
        satirlar.append(_iliski_testi(x_t, x_i, y_t, y_i, beklenti))
        time.sleep(0.3)
    return "\n".join(satirlar)


def finansal_okuryazarlik_raporu_olustur():
    with _kilit:
        if _calisiyor.get("finans_iliski"):
            send_telegram_message("⏳ Finansal ilişki testi zaten çalışıyor, bekle.")
            return
        _calisiyor["finans_iliski"] = True

    try:
        send_telegram_message(
            f"🔬 Finansal okuryazarlık testi başladı ({KAPSAMLI_SURUM})\n"
            f"{len(FINANSAL_ILISKILER)} klasik piyasa ilişkisi test ediliyor...")

        bolum = _finansal_okuryazarlik_bolumu()

        rapor = f"""# Finansal Okuryazarlık Testi — Klasik Piyasa İlişkileri
Oluşturulma: {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}
Dönem: son {FINANSAL_ILISKI_PERIYOD}

## Ne test edildi?
"Herkesin bildiği" klasik piyasa ilişkileri (altın-dolar, faiz-büyüme
hisseleri, petrol-enerji vb.) gerçek fiyat verisiyle test edildi.
Korelasyon ±{FINANSAL_ILISKI_KORELASYON_ESIGI}'in altındaysa "zayıf/yok"
sayıldı - bu eşiğin keyfi olduğunu unutma, sınırda çıkan sonuçlara
temkinli yaklaş.

---

{bolum}

---

## Genel Değerlendirme
Korelasyon -1..+1 arası: +1 tam aynı yönde, 0 ilişkisiz, -1 tam ters
yönde. "❌ doğrulanmadı" çıkan bir ilişki, klasik kuralın YANLIŞ olduğu
anlamına gelmez - kısa dönemde (2 yıl) başka faktörlerin baskın
olabileceği, ya da ilişkinin uzun vadede geçerli olup kısa vadede
gürültüye karıştığı anlamına da gelebilir.
"""
        dosya_yolu = os.path.join(DATA_DIR, "finansal_okuryazarlik_raporu.md")
        with open(dosya_yolu, "w", encoding="utf-8") as f:
            f.write(rapor)

        send_telegram_document(dosya_yolu, caption="📄 Finansal Okuryazarlık Testi Raporu")

    except Exception as e:
        send_telegram_message(f"❌ Finansal ilişki testi hatası: {e}")
    finally:
        with _kilit:
            _calisiyor["finans_iliski"] = False


def _faiz_karari_bolumu(karar_listesi, bist_fiyat) -> str:
    satirlar = []
    for yon_kod, yon_tr in [("artis", "Faiz ARTIŞI"), ("indirim", "Faiz İNDİRİMİ"),
                              ("sabit", "Faiz SABİT tutuldu")]:
        tarihler = [t for t, y in karar_listesi if y == yon_kod]
        if len(tarihler) < 2:
            continue
        satirlar.append(f"**{yon_tr}** ({len(tarihler)} karar):")
        for g in FAIZ_UFUK_GUNLERI:
            degerler = []
            for t in tarihler:
                d = _karar_sonrasi_getiri(bist_fiyat, pd.Timestamp(t), g)
                if d is not None:
                    degerler.append(d)
            if len(degerler) < 2:
                satirlar.append(f"  T+{g} gün: örnek yetersiz")
                continue
            ort = sum(degerler) / len(degerler)
            satirlar.append(f"  T+{g} gün: BIST100 ort. %{ort:+.2f} (n={len(degerler)})")
        satirlar.append("")
    return "\n".join(satirlar) if satirlar else "Yeterli örnek bulunamadı."


# =============================================================================
# RAPOR OLUŞTURMA
# =============================================================================
def kapsamli_rapor_olustur():
    with _kilit:
        if _calisiyor["rapor"]:
            send_telegram_message("⏳ Kapsamlı rapor zaten hazırlanıyor, bekle.")
            return
        _calisiyor["rapor"] = True

    try:
        send_telegram_message(
            f"🔬 Kapsamlı BIST analiz raporu hazırlanıyor ({KAPSAMLI_SURUM})\n"
            f"{len(BIST_HISSELER)} hisse taranıyor - 5-10 dakika sürebilir...")

        bolum1 = _fiyat_hacim_bolumu()

        bist_fiyat = _fiyat_serisi("XU100.IS", "3y")
        if bist_fiyat is not None:
            bolum2 = _faiz_karari_bolumu(TCMB_KARARLARI, bist_fiyat)
            bolum3 = _faiz_karari_bolumu(FED_KARARLARI, bist_fiyat)
        else:
            bolum2 = bolum3 = "❌ BIST100 (XU100.IS) verisi alınamadı, bu bölüm atlandı."

        rapor = f"""# BIST Kapsamlı Analiz Raporu
Oluşturulma: {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}
Sürüm: {KAPSAMLI_SURUM}

## ⚠️ ÖNEMLİ UYARI
Bölüm 2 ve 3'teki faiz kararı tarihleri/yönleri **Claude'un eğitim
verisinden hatırlanmıştır** — TCMB ve Fed'in resmi duyuru sayfalarıyla
**teyit edilmemiştir**. Özellikle 2025 sonrası kararlar için kesinlik
garanti edilmez. Önemli bir karara dayanmadan önce tarihleri kendin
doğrula (tcmb.gov.tr, federalreserve.gov).

Ayrıca: gerçek/güncel bir "BIST30" bileşen listesi teyit edilemediği
için, botun genel takip listesindeki ({len(BIST_HISSELER)} hisse) tüm
hisseler tarandı - sadece BIST30 değil.

---

## Bölüm 1: Fiyat-Hacim Anomalileri (Hacimsiz Sert Hareketler)
Kriter: günlük hareket ≥ %{ANOMALI_MIN_HAREKET_PCT}, hacim ortalama 20
günün {ANOMALI_MAX_HACIM_ORANI}x katından AZ (yani "az emirle büyük
hareket"). Bu tür günler ince/manipüle edilebilir bir emir defterine
işaret EDEBİLİR ama tek başına kanıt değildir - meşru sebepler
(KAP açıklaması, düşük likidite, genel piyasa hareketi) de aynı
görünümü verebilir.

{bolum1}

---

## Bölüm 2: TCMB Faiz Kararları ve BIST100 Tepkisi

{bolum2}

---

## Bölüm 3: Fed (ABD) Faiz Kararları ve BIST100 Tepkisi

{bolum3}

---

## Genel Değerlendirme
Bu rapor bir "manipülasyon kanıtı" değil, örüntü arama çalışmasıdır.
Bölüm 1'deki her satır otomatik olarak manipülasyon anlamına gelmez.
Bölüm 2-3'teki küçük örnek boyutları (n) istatistiksel güveni
sınırlıyor - kesin sonuç değil, ilk bakış niteliğinde.
"""
        dosya_yolu = os.path.join(DATA_DIR, "bist_kapsamli_rapor.md")
        with open(dosya_yolu, "w", encoding="utf-8") as f:
            f.write(rapor)

        send_telegram_document(dosya_yolu, caption="📄 BIST Kapsamlı Analiz Raporu")

    except Exception as e:
        send_telegram_message(f"❌ Kapsamlı rapor hatası: {e}")
    finally:
        with _kilit:
            _calisiyor["rapor"] = False


# =============================================================================
# ARGE_BOTU'NUN TELEGRAM DÖNGÜSÜNE KANCA — kendi getUpdates döngüsü YOK
# =============================================================================
def kapsamli_analiz_update_isle(update: dict):
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        return
    mesaj = update.get("message", {})
    if str(mesaj.get("chat", {}).get("id", "")) != str(TELEGRAM_CHAT_ID):
        return
    text = (mesaj.get("text") or "").strip().lower()
    if text.startswith("/kapsamli_rapor"):
        threading.Thread(target=kapsamli_rapor_olustur, daemon=True).start()
    elif text.startswith("/finans_iliski_raporu"):
        threading.Thread(target=finansal_okuryazarlik_raporu_olustur, daemon=True).start()


def baslangic():
    send_telegram_message(
        f"📄 BIST Kapsamlı Analiz Modülü AKTİF — {KAPSAMLI_SURUM}\n\n"
        "Komut: /kapsamli_rapor\n"
        "Fiyat-hacim anomalileri + TCMB/Fed faiz kararı tepkisini tek bir "
        "dosyada toplar (5-10 dk sürebilir).\n\n"
        "Komut: /finans_iliski_raporu\n"
        "Altın-dolar, faiz-hisse, petrol-enerji gibi klasik piyasa "
        "ilişkilerini gerçek veriyle test eder.")
