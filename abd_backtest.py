"""
abd_backtest.py — GERİYE DÖNÜK TEST MODÜLÜ (haber sınıflandırması + gap)
==============================================================================
2026-09-17 — Kullanıcının isteğiyle kuruldu.

Bu modül CANLI bir sinyal ÜRETMİYOR - sadece iki hipotezi geçmiş veriyle
test edip Telegram'a bir rapor dönüyor:

1) /haber_backtest — abd_akilli_para.py'nin haber sınıflandırması
   (pozitif/negatif/belirsiz) gerçekten fiyatla örtüşüyor mu? Kaç gün
   sonra etkisi en güçlü görülüyor?

2) /gap_backtest — Kullanıcının gözlemi: "hacimli bir hisse küçük bir
   gap'le (%0.5-1) açarsa genelde tersine döner, büyük bir gap'le
   (%2-4+) açarsa günün geri kalanında aynı yönde devam eder" - bu
   gerçekten doğru mu?

TELEGRAM: Aynı TELEGRAM_TOKEN/TELEGRAM_CHAT_ID (abd_sosyal_duygu.py ve
abd_akilli_para.py ile ortak) kullanılıyor. Bu token'ı ŞU ANA KADAR
HİÇBİR modül getUpdates ile dinlemiyordu (ikisi de sadece mesaj
gönderiyor) - bu yüzden BU modülün kendi komut dinleme döngüsünü
açması 409 Conflict riski TAŞIMIYOR.

Hesaplamalar birkaç dakika sürebilir (yüzlerce API isteği) - bu yüzden
komut geldiğinde AYRI bir thread'de çalıştırılıyor, botun kendisini
kilitlemiyor.
"""
import os
import time
import threading
from datetime import datetime, timedelta, timezone

import requests
import pandas as pd
import yfinance as yf

import abd_akilli_para as AK  # sınıflandırma fonksiyonlarını TEKRAR YAZMADAN kullanmak için

# =============================================================================
# YAPILANDIRMA
# =============================================================================
TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN", "")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")
FINNHUB_API_KEY = os.environ.get("FINNHUB_API_KEY", "")

BACKTEST_SURUM = "abd-backtest-v1-2026-09-17"

HABER_GERI_TEST_GUN_SAYISI = 90     # Finnhub'dan ne kadar geriye gidilecek
UFUK_GUNLERI = [1, 2, 3, 5, 10]      # kaç gün sonrasına bakılacak
HABER_MIN_ORNEK = 8                  # bir ufuk için bu sayının altı raporlanmaz

GAP_PERIYOD = "1y"
GAP_HACIM_ESIK_KATSAYI = 1.5         # "hacimli gün" sayılması için ort. hacmin katı
GAP_BUCKETLARI = [(0, 0.5), (0.5, 1), (1, 2), (2, 3), (3, 4), (4, 999)]
GAP_MIN_ORNEK = 20


def _running_test_kilit():
    return threading.Lock()


_kilit = _running_test_kilit()
_calisiyor = {"haber": False, "gap": False}


# =============================================================================
# TELEGRAM
# =============================================================================
def send_telegram_message(text: str):
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        print(f"[Backtest devre dışı] {text}", flush=True)
        return
    try:
        requests.post(
            f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage",
            json={"chat_id": TELEGRAM_CHAT_ID, "text": text[:4000]}, timeout=20)
    except Exception as e:
        print(f"[Backtest Telegram hata] {e}", flush=True)


# =============================================================================
# 1) HABER SINIFLANDIRMASI — GERİYE DÖNÜK TEST
# =============================================================================
def _gecmis_haberleri_cek(ticker: str, baslangic: str, bitis: str):
    if not FINNHUB_API_KEY:
        return []
    try:
        r = requests.get(
            "https://finnhub.io/api/v1/company-news",
            params={"symbol": ticker, "from": baslangic, "to": bitis,
                    "token": FINNHUB_API_KEY},
            timeout=20)
        if r.status_code != 200:
            return []
        veri = r.json()
        return veri if isinstance(veri, list) else []
    except Exception:
        return []


def _fiyat_serisi_al(ticker: str):
    try:
        df = yf.Ticker(ticker).history(period="6mo")
        return df if not df.empty else None
    except Exception:
        return None


def _getirileri_hesapla(df, olay_tarihi_utc):
    """olay_tarihi'ni 1. gün sayarak (haberin açıklandığı gün dahil),
    UFUK_GUNLERI'ndeki her ufuk için % getiri döner. Referans fiyat,
    haberden BİR ÖNCEKİ günün kapanışı (haberin henüz fiyatlanmadığı
    varsayılan son an)."""
    olay_tarih_naive = olay_tarihi_utc
    if df.index.tz is not None:
        olay_tarih_naive = olay_tarihi_utc.astimezone(df.index.tz)
    else:
        olay_tarih_naive = olay_tarihi_utc.replace(tzinfo=None)

    sonraki = df[df.index >= olay_tarih_naive]
    if sonraki.empty:
        return {}
    gun0_konum = df.index.get_loc(sonraki.index[0])
    if gun0_konum == 0:
        return {}  # önceki gün kapanışı yok, referans alınamaz

    baz_fiyat = df["Close"].iloc[gun0_konum - 1]
    if baz_fiyat == 0:
        return {}

    sonuc = {}
    for g in UFUK_GUNLERI:
        hedef_konum = gun0_konum + (g - 1)
        if hedef_konum >= len(df):
            continue
        hedef_fiyat = df["Close"].iloc[hedef_konum]
        sonuc[g] = (hedef_fiyat - baz_fiyat) / baz_fiyat * 100
    return sonuc


def haber_backtest_calistir():
    with _kilit:
        if _calisiyor["haber"]:
            send_telegram_message("⏳ Haber geriye dönük testi zaten çalışıyor, bekle.")
            return
        _calisiyor["haber"] = True

    try:
        send_telegram_message(
            f"🔬 Haber geriye dönük testi başladı ({BACKTEST_SURUM})\n"
            f"Son {HABER_GERI_TEST_GUN_SAYISI} gün, {len(AK.AKILLI_PARA_TICKERS)} hisse "
            f"taranıyor - bu birkaç dakika sürebilir...")

        bitis_dt = datetime.now(timezone.utc)
        baslangic_dt = bitis_dt - timedelta(days=HABER_GERI_TEST_GUN_SAYISI)
        baslangic_str = baslangic_dt.strftime("%Y-%m-%d")
        bitis_str = bitis_dt.strftime("%Y-%m-%d")

        getiriler = {"pozitif": {g: [] for g in UFUK_GUNLERI},
                     "negatif": {g: [] for g in UFUK_GUNLERI}}
        sayim = {"pozitif": 0, "negatif": 0, "belirsiz": 0, "elenen": 0}

        for i, ticker in enumerate(AK.AKILLI_PARA_TICKERS):
            haberler = _gecmis_haberleri_cek(ticker, baslangic_str, bitis_str)
            olaylar = []
            for h in haberler:
                baslik = h.get("headline", "")
                if not AK._kaynak_guvenilir_mi(h.get("source", "")):
                    sayim["elenen"] += 1
                    continue
                if not AK._onemli_haber_mi(baslik):
                    continue
                if not AK._haber_konusu_dogru_mu(ticker, baslik):
                    sayim["elenen"] += 1
                    continue
                ts = h.get("datetime")
                if not ts:
                    continue
                tarih = datetime.fromtimestamp(ts, tz=timezone.utc)
                yon = AK._haber_yonu(baslik)
                sayim[yon] = sayim.get(yon, 0) + 1
                if yon in ("pozitif", "negatif"):
                    olaylar.append((tarih, yon))

            if olaylar:
                fiyat_df = _fiyat_serisi_al(ticker)
                if fiyat_df is not None:
                    for tarih, yon in olaylar:
                        for g, deger in _getirileri_hesapla(fiyat_df, tarih).items():
                            getiriler[yon][g].append(deger)

            if i % 20 == 0:
                print(f"[Backtest] Haber testi {i}/{len(AK.AKILLI_PARA_TICKERS)} hisse", flush=True)
            time.sleep(1.1)  # Finnhub 60/dk limiti

        # --- Rapor ---
        satirlar = [f"📊 HABER GERİYE DÖNÜK TEST SONUCU ({HABER_GERI_TEST_GUN_SAYISI} gün)",
                    f"Toplam sınıflandırılan: {sayim['pozitif']} pozitif, "
                    f"{sayim['negatif']} negatif, {sayim['belirsiz']} belirsiz "
                    f"({sayim['elenen']} kaynak/özne filtresine takıldı)\n"]

        for yon, baslik_tr in [("pozitif", "🟢 POZİTİF haberler"), ("negatif", "🔴 NEGATİF haberler")]:
            satirlar.append(baslik_tr + ":")
            for g in UFUK_GUNLERI:
                degerler = getiriler[yon][g]
                if len(degerler) < HABER_MIN_ORNEK:
                    satirlar.append(f"  {g}. gün: örnek yetersiz (n={len(degerler)})")
                    continue
                ort = sum(degerler) / len(degerler)
                beklenen_yon = ort > 0 if yon == "pozitif" else ort < 0
                dogru_oran = sum(1 for d in degerler if (d > 0) == (yon == "pozitif")) / len(degerler) * 100
                isaret = "✅" if beklenen_yon else "❌"
                satirlar.append(
                    f"  {g}. gün: ort %{ort:+.2f}, beklenen yönde %{dogru_oran:.0f} "
                    f"(n={len(degerler)}) {isaret}")
            satirlar.append("")

        satirlar.append(
            "ℹ️ 1. gün = haberin açıklandığı gün, referans = bir önceki kapanış. "
            "✅ ortalama getiri beklenen yönde, ❌ ters yönde çıkmış demek - "
            "sınıflandırmanın o ufukta işe yaramadığını gösterir.")
        send_telegram_message("\n".join(satirlar))

    except Exception as e:
        send_telegram_message(f"❌ Haber backtest hatası: {e}")
    finally:
        with _kilit:
            _calisiyor["haber"] = False


# =============================================================================
# 2) AÇILIŞ BOŞLUĞU (GAP) — DEVAM Mİ, TERSİNE DÖNÜŞ MÜ?
# =============================================================================
def gap_backtest_calistir():
    with _kilit:
        if _calisiyor["gap"]:
            send_telegram_message("⏳ Gap geriye dönük testi zaten çalışıyor, bekle.")
            return
        _calisiyor["gap"] = True

    try:
        tickers = AK.AKILLI_PARA_TICKERS
        send_telegram_message(
            f"🔬 Gap geriye dönük testi başladı ({BACKTEST_SURUM})\n"
            f"Son {GAP_PERIYOD}, {len(tickers)} hisse, hacmi ortalamanın "
            f"{GAP_HACIM_ESIK_KATSAYI}x üzerinde olan günler taranıyor...")

        veri = yf.download(tickers=" ".join(tickers), period=GAP_PERIYOD,
                            group_by="ticker", threads=True, progress=False,
                            auto_adjust=True)

        sonuc = {b: {"n": 0, "ayni_yon": 0, "intraday_toplam": 0.0} for b in GAP_BUCKETLARI}

        for t in tickers:
            try:
                df = veri[t].dropna() if len(tickers) > 1 else veri.dropna()
            except (KeyError, Exception):
                continue
            if df is None or len(df) < 30:
                continue

            df = df.copy()
            df["onceki_kapanis"] = df["Close"].shift(1)
            df["ort_hacim20"] = df["Volume"].shift(1).rolling(20).mean()

            for idx in range(21, len(df)):
                onceki_kapanis = df["onceki_kapanis"].iloc[idx]
                ort_hacim = df["ort_hacim20"].iloc[idx]
                if pd.isna(onceki_kapanis) or pd.isna(ort_hacim) or ort_hacim == 0 or onceki_kapanis == 0:
                    continue
                hacim = df["Volume"].iloc[idx]
                if hacim / ort_hacim < GAP_HACIM_ESIK_KATSAYI:
                    continue

                acilis = df["Open"].iloc[idx]
                kapanis = df["Close"].iloc[idx]
                gap_pct = (acilis - onceki_kapanis) / onceki_kapanis * 100
                intraday_pct = (kapanis - acilis) / acilis * 100
                abs_gap = abs(gap_pct)

                for (lo, hi) in GAP_BUCKETLARI:
                    if lo <= abs_gap < hi:
                        ayni_yon = (gap_pct > 0 and intraday_pct > 0) or (gap_pct < 0 and intraday_pct < 0)
                        # yönlü karşılaştırma yapılabilsin diye gap yönüne göre işaretliyoruz
                        yonlu_intraday = intraday_pct if gap_pct >= 0 else -intraday_pct
                        sonuc[(lo, hi)]["n"] += 1
                        if ayni_yon:
                            sonuc[(lo, hi)]["ayni_yon"] += 1
                        sonuc[(lo, hi)]["intraday_toplam"] += yonlu_intraday
                        break

        # --- Rapor ---
        satirlar = [f"📊 GAP GERİYE DÖNÜK TEST SONUCU ({GAP_PERIYOD}, "
                    f"hacim ≥ {GAP_HACIM_ESIK_KATSAYI}x ortalama)\n"]
        for (lo, hi) in GAP_BUCKETLARI:
            veri_b = sonuc[(lo, hi)]
            n = veri_b["n"]
            aralik = f"%{lo:.1f}-{hi:.1f}" if hi < 999 else f"%{lo:.1f}+"
            if n < GAP_MIN_ORNEK:
                satirlar.append(f"Gap {aralik}: örnek yetersiz (n={n})")
                continue
            devam_orani = veri_b["ayni_yon"] / n * 100
            ort_yonlu_intraday = veri_b["intraday_toplam"] / n
            satirlar.append(
                f"Gap {aralik}: n={n}, gün içi AYNI yönde devam etme oranı "
                f"%{devam_orani:.0f}, ort. gün-içi hareket (gap yönünde) %{ort_yonlu_intraday:+.2f}")

        satirlar.append(
            "\nℹ️ 'Aynı yönde devam' = açılış boşluğuyla aynı yönde kapanması "
            "(örn. yukarı gap verip günü daha da yukarıda kapatması). %50 "
            "civarı = yazı-tura, anlamlı bir şey yok. Belirgin şekilde "
            "%50'den uzaklaşan aralıklar hipotezi destekler ya da çürütür.")
        send_telegram_message("\n".join(satirlar))

    except Exception as e:
        send_telegram_message(f"❌ Gap backtest hatası: {e}")
    finally:
        with _kilit:
            _calisiyor["gap"] = False


# =============================================================================
# KOMUT DİNLEME — bu token'ı başka HİÇBİR modül dinlemiyor, kendi
# getUpdates döngüsünü açması güvenli (409 Conflict riski yok).
# =============================================================================
def _offset_dosyasi():
    return os.path.join(os.environ.get("DATA_DIR", "."), "abd_backtest_offset.txt")


def _offset_yukle():
    try:
        with open(_offset_dosyasi(), "r") as f:
            return int(f.read().strip())
    except (OSError, ValueError):
        return 0


def _offset_kaydet(offset):
    try:
        with open(_offset_dosyasi(), "w") as f:
            f.write(str(offset))
    except OSError:
        pass


def backtest_komut_dongusu():
    if not TELEGRAM_TOKEN:
        print("[Backtest] TELEGRAM_TOKEN yok - komut döngüsü başlatılmadı.", flush=True)
        return
    offset = _offset_yukle()
    while True:
        try:
            r = requests.get(
                f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/getUpdates",
                params={"offset": offset, "timeout": 20}, timeout=30)
            if r.status_code == 200:
                for u in r.json().get("result", []):
                    offset = max(offset, u.get("update_id", 0) + 1)
                    mesaj = u.get("message", {})
                    if str(mesaj.get("chat", {}).get("id", "")) != str(TELEGRAM_CHAT_ID):
                        continue
                    text = (mesaj.get("text") or "").strip().lower()
                    if text.startswith("/haber_backtest"):
                        threading.Thread(target=haber_backtest_calistir, daemon=True).start()
                    elif text.startswith("/gap_backtest"):
                        threading.Thread(target=gap_backtest_calistir, daemon=True).start()
                _offset_kaydet(offset)
        except Exception as e:
            print(f"[Backtest] Komut döngüsü hatası: {e}", flush=True)
            time.sleep(5)


def baslangic():
    send_telegram_message(
        f"🔬 Geriye Dönük Test Modülü AKTİF — {BACKTEST_SURUM}\n\n"
        "Komutlar:\n"
        "/haber_backtest — haber pozitif/negatif sınıflandırması gerçekten "
        "fiyatla örtüşüyor mu, kaç gün sonra tutuyor?\n"
        "/gap_backtest — hacimli hisselerde küçük açılış boşluğu tersine mi "
        "dönüyor, büyük boşluk aynı yönde mi devam ediyor?\n\n"
        "İkisi de birkaç dakika sürebilir, sonuç hazır olunca ayrı mesaj gelecek.")
