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
from collections import defaultdict
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
_calisiyor = {"haber": False, "gap": False, "acilis": False, "korelasyon": False,
              "premarket": False, "premarket_gap": False, "patlama": False,
              "sektor": False}


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

        # S&P 500'e göre RELATİF getiri hesaplıyoruz - aksi halde "haber
        # etkisi" dediğimiz şey aslında sadece genel piyasa akıntısı olabilir
        # (90 günlük dönemde piyasa yükseliyorsa, hem pozitif hem negatif
        # haberlerden sonra "ortalama getiri pozitif" çıkar, bu haberle
        # ilgisiz bir yanılgı olur).
        spy_df = _fiyat_serisi_al("SPY")
        if spy_df is None:
            send_telegram_message("❌ SPY (piyasa referansı) verisi alınamadı, test durduruldu.")
            return

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
                        hisse_getiri = _getirileri_hesapla(fiyat_df, tarih)
                        spy_getiri = _getirileri_hesapla(spy_df, tarih)
                        for g, deger in hisse_getiri.items():
                            if g not in spy_getiri:
                                continue
                            relatif = deger - spy_getiri[g]
                            getiriler[yon][g].append(relatif)

            if i % 20 == 0:
                print(f"[Backtest] Haber testi {i}/{len(AK.AKILLI_PARA_TICKERS)} hisse", flush=True)
            time.sleep(1.1)  # Finnhub 60/dk limiti

        # --- Rapor ---
        satirlar = [f"📊 HABER GERİYE DÖNÜK TEST SONUCU ({HABER_GERI_TEST_GUN_SAYISI} gün, "
                    f"S&P 500'e göre RELATİF getiri)",
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
            "Getiriler S&P 500'ün AYNI dönemdeki hareketine göre RELATİF - yani "
            "genel piyasa akıntısı çıkarılmış hâli. ✅ ortalama getiri beklenen "
            "yönde, ❌ ters yönde çıkmış demek.")
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
# 3) AÇILIŞTAN SONRAKİ İLK 15 DAKİKA — TERSİNE Mİ DÖNÜYOR, DEVAM MI EDİYOR?
# (2026-09-17 eklendi - önceki gap testi GÜNLÜK veriyle çalışıyordu, bu
# soruyu hiç ölçmüyordu. Bu, dakikalık veriyle DOĞRU zaman ölçeğinde
# test ediyor. yfinance 5 dakikalık mumları en fazla ~60 gün geriye
# veriyor - bu yüzden örnek boyutu günlük gap testinden daha küçük.)
# =============================================================================
ACILIS_PERIYOD = "59d"
ACILIS_ERKEN_BAR_SAYISI = 3          # 3 x 5dk = ilk 15 dakika
ACILIS_HEDEF_BAR_SAYILARI = [12, 18, 24]  # 60dk, 90dk, 120dk sonrası
ACILIS_ERKEN_BUCKETLARI = [(0, 0.3), (0.3, 0.5), (0.5, 1.0), (1.0, 999)]
ACILIS_MIN_ORNEK = 15


def acilis_ilk15dk_backtest_calistir():
    with _kilit:
        if _calisiyor.get("acilis"):
            send_telegram_message("⏳ Açılış geriye dönük testi zaten çalışıyor, bekle.")
            return
        _calisiyor["acilis"] = True

    try:
        tickers = AK.AKILLI_PARA_TICKERS
        send_telegram_message(
            f"🔬 Açılış (ilk 15dk) geriye dönük testi başladı ({BACKTEST_SURUM})\n"
            f"Son {ACILIS_PERIYOD}, {len(tickers)} hisse, 5 dakikalık mumlarla "
            f"taranıyor - bu birkaç dakika sürebilir...")

        veri = yf.download(tickers=" ".join(tickers), period=ACILIS_PERIYOD,
                            interval="5m", group_by="ticker", threads=True,
                            progress=False, auto_adjust=True)

        sonuc = {b: {h: {"n": 0, "ters_yon": 0, "hareket_toplam": 0.0}
                     for h in ACILIS_HEDEF_BAR_SAYILARI}
                 for b in ACILIS_ERKEN_BUCKETLARI}

        for t in tickers:
            try:
                df = veri[t].dropna(how="all") if len(tickers) > 1 else veri.dropna(how="all")
            except (KeyError, Exception):
                continue
            if df is None or df.empty:
                continue

            for _, grup in df.groupby(df.index.date):
                grup = grup.sort_index()
                if len(grup) < max(ACILIS_HEDEF_BAR_SAYILARI) + 1:
                    continue
                acilis = grup["Open"].iloc[0]
                if pd.isna(acilis) or acilis == 0:
                    continue
                erken_fiyat = grup["Close"].iloc[ACILIS_ERKEN_BAR_SAYISI - 1]
                if pd.isna(erken_fiyat):
                    continue
                erken_hareket = (erken_fiyat - acilis) / acilis * 100
                abs_erken = abs(erken_hareket)

                for (lo, hi) in ACILIS_ERKEN_BUCKETLARI:
                    if lo <= abs_erken < hi:
                        for h in ACILIS_HEDEF_BAR_SAYILARI:
                            if h >= len(grup):
                                continue
                            hedef_fiyat = grup["Close"].iloc[h]
                            if pd.isna(hedef_fiyat):
                                continue
                            sonraki_hareket = (hedef_fiyat - erken_fiyat) / erken_fiyat * 100
                            ters_mi = (erken_hareket > 0 and sonraki_hareket < 0) or \
                                      (erken_hareket < 0 and sonraki_hareket > 0)
                            yonlu = sonraki_hareket if erken_hareket >= 0 else -sonraki_hareket
                            b = sonuc[(lo, hi)][h]
                            b["n"] += 1
                            if ters_mi:
                                b["ters_yon"] += 1
                            b["hareket_toplam"] += yonlu
                        break

        # --- Rapor ---
        satirlar = [f"📊 AÇILIŞ İLK 15DK GERİYE DÖNÜK TEST SONUCU ({ACILIS_PERIYOD})\n"]
        for (lo, hi) in ACILIS_ERKEN_BUCKETLARI:
            aralik = f"%{lo:.1f}-{hi:.1f}" if hi < 999 else f"%{lo:.1f}+"
            satirlar.append(f"İlk 15dk hareket {aralik}:")
            for h in ACILIS_HEDEF_BAR_SAYILARI:
                dk = h * 5
                veri_b = sonuc[(lo, hi)][h]
                n = veri_b["n"]
                if n < ACILIS_MIN_ORNEK:
                    satirlar.append(f"  +{dk}dk: örnek yetersiz (n={n})")
                    continue
                ters_oran = veri_b["ters_yon"] / n * 100
                ort_yonlu = veri_b["hareket_toplam"] / n
                satirlar.append(
                    f"  +{dk}dk: n={n}, TERSİNE DÖNME oranı %{ters_oran:.0f}, "
                    f"ort. hareket (erken yöne göre) %{ort_yonlu:+.2f}")
            satirlar.append("")

        satirlar.append(
            "ℹ️ 'Tersine dönme' = ilk 15dk'daki yönün tam tersi yönde kapanmış "
            "olması. %50 civarı = yazı-tura, anlamlı bir şey yok. 'Ort. hareket' "
            "pozitifse ilk yönde devam ediyor demek, negatifse tersine dönüyor "
            "demek (erken harekete göre işaretli).")
        send_telegram_message("\n".join(satirlar))

    except Exception as e:
        send_telegram_message(f"❌ Açılış backtest hatası: {e}")
    finally:
        with _kilit:
            _calisiyor["acilis"] = False


# =============================================================================
# 4) BIST-ABD KORELASYONU (GENEL + SEKTÖREL)
# =============================================================================
KORELASYON_PERIYOD = "2y"

SEKTOR_ESLESTIRME = {
    "Gıda/Tüketim": {
        "bist": ["AEFES.IS", "ULKER.IS", "CCOLA.IS", "TATGD.IS", "BANVT.IS"],
        "abd_etf": "XLP",
    },
    "Bankacılık": {
        "bist": ["GARAN.IS", "AKBNK.IS", "ISCTR.IS", "YKBNK.IS", "VAKBN.IS", "HALKB.IS"],
        "abd_etf": "XLF",
    },
    "Sanayi": {
        "bist": ["EREGL.IS", "KRDMD.IS", "SISE.IS", "TKFEN.IS", "ISDMR.IS"],
        "abd_etf": "XLI",
    },
}


def _endeks_getirisi(ticker: str, periyod: str):
    try:
        df = yf.Ticker(ticker).history(period=periyod)
        if df.empty:
            return None
        seri = df["Close"].pct_change() * 100
        if seri.index.tz is not None:
            seri.index = seri.index.tz_localize(None)
        return seri.dropna()
    except Exception:
        return None


def _sepet_getirisi(tickers: list, periyod: str):
    try:
        veri = yf.download(tickers=" ".join(tickers), period=periyod,
                            group_by="ticker", threads=True, progress=False,
                            auto_adjust=True)
    except Exception:
        return None
    kapanislar = {}
    for t in tickers:
        try:
            df = veri[t] if len(tickers) > 1 else veri
            k = df["Close"].dropna()
            if not k.empty:
                kapanislar[t] = k
        except Exception:
            continue
    if not kapanislar:
        return None
    ortak = pd.concat(kapanislar, axis=1)
    if ortak.index.tz is not None:
        ortak.index = ortak.index.tz_localize(None)
    getiri = ortak.pct_change().mean(axis=1) * 100  # eşit ağırlıklı sepet
    return getiri.dropna()


def _abd_bir_onceki_getiri(bist_getiri, us_getiri):
    """Her BIST günü için, o gün açılmadan önce TAMAMLANMIŞ en son ABD
    seansının getirisini hizalar (basit ama makul bir yaklaşım - hafta
    sonu/tatil farklarını ffill+shift ile idare ediyor)."""
    tum_tarihler = sorted(set(bist_getiri.index) | set(us_getiri.index))
    us_gunluk = us_getiri.reindex(tum_tarihler).ffill()
    us_bir_onceki = us_gunluk.shift(1)
    return us_bir_onceki.reindex(bist_getiri.index)


def _iliski_raporu(bist_getiri, abd_onceki, isim: str) -> str:
    birlesik = pd.DataFrame({"bist": bist_getiri, "abd_onceki": abd_onceki}).dropna()
    if len(birlesik) < 30:
        return f"{isim}: örnek yetersiz (n={len(birlesik)})"
    korelasyon = birlesik["bist"].corr(birlesik["abd_onceki"])
    yukselince = birlesik[birlesik["abd_onceki"] > 0]
    dusunce = birlesik[birlesik["abd_onceki"] < 0]
    ayni_yon_orani = ((birlesik["bist"] > 0) == (birlesik["abd_onceki"] > 0)).mean() * 100
    return (
        f"{isim}\n"
        f"  n={len(birlesik)}, korelasyon={korelasyon:+.2f}, aynı yönde hareket %{ayni_yon_orani:.0f}\n"
        f"  ABD (önceki seans) yükselince → BIST ort. %{yukselince['bist'].mean():+.2f} (n={len(yukselince)})\n"
        f"  ABD (önceki seans) düşünce → BIST ort. %{dusunce['bist'].mean():+.2f} (n={len(dusunce)})")


def korelasyon_backtest_calistir():
    with _kilit:
        if _calisiyor.get("korelasyon"):
            send_telegram_message("⏳ Korelasyon testi zaten çalışıyor, bekle.")
            return
        _calisiyor["korelasyon"] = True

    try:
        send_telegram_message(
            f"🔬 BIST-ABD korelasyon testi başladı ({BACKTEST_SURUM})\n"
            f"Son {KORELASYON_PERIYOD} - genel piyasa + 3 sektör taranıyor...")

        satirlar = [f"📊 BIST-ABD KORELASYON TESTİ ({KORELASYON_PERIYOD})\n", "GENEL PİYASA:"]

        bist_genel = _endeks_getirisi("XU100.IS", KORELASYON_PERIYOD)
        us_genel = _endeks_getirisi("^GSPC", KORELASYON_PERIYOD)
        if bist_genel is None or us_genel is None:
            satirlar.append("❌ Genel endeks verisi alınamadı (XU100.IS ve/veya ^GSPC).")
        else:
            abd_onceki = _abd_bir_onceki_getiri(bist_genel, us_genel)
            satirlar.append(_iliski_raporu(bist_genel, abd_onceki, "BIST100 vs S&P500 (bir önceki seans)"))
        satirlar.append("")

        satirlar.append("SEKTÖREL:")
        for sektor_adi, eslestirme in SEKTOR_ESLESTIRME.items():
            bist_sepet = _sepet_getirisi(eslestirme["bist"], KORELASYON_PERIYOD)
            us_etf = _endeks_getirisi(eslestirme["abd_etf"], KORELASYON_PERIYOD)
            if bist_sepet is None or us_etf is None:
                satirlar.append(f"{sektor_adi}: veri alınamadı\n")
                continue
            abd_onceki_sektor = _abd_bir_onceki_getiri(bist_sepet, us_etf)
            satirlar.append(_iliski_raporu(
                bist_sepet, abd_onceki_sektor,
                f"{sektor_adi} (BIST sepeti vs ABD {eslestirme['abd_etf']} ETF)"))
            satirlar.append("")

        satirlar.append(
            "ℹ️ Korelasyon -1..+1: +1 tam aynı yönde, 0 ilişkisiz, -1 tam ters "
            "yönde. 'Bir önceki seans' = BIST günü açılmadan önce tamamlanmış "
            "en son ABD seansı.")
        send_telegram_message("\n".join(satirlar))

    except Exception as e:
        send_telegram_message(f"❌ Korelasyon backtest hatası: {e}")
    finally:
        with _kilit:
            _calisiyor["korelasyon"] = False


# =============================================================================
# 5) PRE-MARKET vs ANA SEANS — pre-market'teki (düşük hacimli) hareket, gerçek
# hacim geldiğinde (ana seans açılışı) devam mı ediyor, siliniyor mu?
# =============================================================================
PREMARKET_PERIYOD = "59d"
PREMARKET_BUCKETLARI = [(0, 0.5), (0.5, 1), (1, 2), (2, 999)]
PREMARKET_HEDEF_DAKIKALAR = [30, 60, 120]
PREMARKET_MIN_ORNEK = 15


def premarket_backtest_calistir():
    with _kilit:
        if _calisiyor.get("premarket"):
            send_telegram_message("⏳ Pre-market testi zaten çalışıyor, bekle.")
            return
        _calisiyor["premarket"] = True

    try:
        tickers = AK.AKILLI_PARA_TICKERS
        send_telegram_message(
            f"🔬 Pre-market vs ana seans testi başladı ({BACKTEST_SURUM})\n"
            f"Son {PREMARKET_PERIYOD}, {len(tickers)} hisse, 5dk mumlarla "
            f"(pre-market dahil) taranıyor - bu birkaç dakika sürebilir...")

        sonuc = {b: {dk: {"n": 0, "ters_yon": 0, "hareket_toplam": 0.0}
                     for dk in PREMARKET_HEDEF_DAKIKALAR}
                 for b in PREMARKET_BUCKETLARI}
        veri_alinamayan = 0

        for i, ticker in enumerate(tickers):
            try:
                df = yf.Ticker(ticker).history(period=PREMARKET_PERIYOD,
                                                interval="5m", prepost=True)
            except Exception:
                veri_alinamayan += 1
                continue
            if df is None or df.empty:
                veri_alinamayan += 1
                continue

            try:
                if df.index.tz is not None:
                    df.index = df.index.tz_convert("America/New_York")
            except Exception:
                pass  # saat dilimi dönüştürülemezse ham veriyle devam et

            for _, grup in df.groupby(df.index.date):
                grup = grup.sort_index()
                ana_seans_maske = (grup.index.hour > 9) | \
                                   ((grup.index.hour == 9) & (grup.index.minute >= 30))
                if not ana_seans_maske.any() or ana_seans_maske.all():
                    continue  # pre-market ya da ana seans verisi hiç yok
                ana_baslangic_konum = ana_seans_maske.argmax()

                premarket_grup = grup.iloc[:ana_baslangic_konum]
                ana_seans_grup = grup.iloc[ana_baslangic_konum:]
                if premarket_grup.empty or len(ana_seans_grup) < max(PREMARKET_HEDEF_DAKIKALAR) // 5 + 1:
                    continue

                pre_ilk = premarket_grup["Open"].iloc[0]
                pre_son = premarket_grup["Close"].iloc[-1]
                if pd.isna(pre_ilk) or pre_ilk == 0 or pd.isna(pre_son):
                    continue
                premarket_hareket = (pre_son - pre_ilk) / pre_ilk * 100
                abs_pre = abs(premarket_hareket)

                ana_acilis = ana_seans_grup["Open"].iloc[0]
                if pd.isna(ana_acilis) or ana_acilis == 0:
                    continue

                for (lo, hi) in PREMARKET_BUCKETLARI:
                    if lo <= abs_pre < hi:
                        for dk in PREMARKET_HEDEF_DAKIKALAR:
                            bar_sayisi = dk // 5
                            if bar_sayisi >= len(ana_seans_grup):
                                continue
                            hedef_fiyat = ana_seans_grup["Close"].iloc[bar_sayisi]
                            if pd.isna(hedef_fiyat):
                                continue
                            sonraki_hareket = (hedef_fiyat - ana_acilis) / ana_acilis * 100
                            ters_mi = (premarket_hareket > 0 and sonraki_hareket < 0) or \
                                      (premarket_hareket < 0 and sonraki_hareket > 0)
                            yonlu = sonraki_hareket if premarket_hareket >= 0 else -sonraki_hareket
                            b = sonuc[(lo, hi)][dk]
                            b["n"] += 1
                            if ters_mi:
                                b["ters_yon"] += 1
                            b["hareket_toplam"] += yonlu
                        break

            if i % 20 == 0:
                print(f"[Backtest] Pre-market testi {i}/{len(tickers)}", flush=True)

        # --- Rapor ---
        satirlar = [f"📊 PRE-MARKET vs ANA SEANS TEST SONUCU ({PREMARKET_PERIYOD})"]
        if veri_alinamayan:
            satirlar.append(f"({veri_alinamayan} hissede veri alınamadı, atlandı)\n")
        for (lo, hi) in PREMARKET_BUCKETLARI:
            aralik = f"%{lo:.1f}-{hi:.1f}" if hi < 999 else f"%{lo:.1f}+"
            satirlar.append(f"Pre-market hareketi {aralik}:")
            for dk in PREMARKET_HEDEF_DAKIKALAR:
                veri_b = sonuc[(lo, hi)][dk]
                n = veri_b["n"]
                if n < PREMARKET_MIN_ORNEK:
                    satirlar.append(f"  ana seans +{dk}dk: örnek yetersiz (n={n})")
                    continue
                ters_oran = veri_b["ters_yon"] / n * 100
                ort_yonlu = veri_b["hareket_toplam"] / n
                satirlar.append(
                    f"  ana seans +{dk}dk: n={n}, TERSİNE DÖNME oranı %{ters_oran:.0f}, "
                    f"ort. hareket (pre-market yönüne göre) %{ort_yonlu:+.2f}")
            satirlar.append("")

        satirlar.append(
            "ℹ️ 'Tersine dönme' = pre-market'teki yönün tam tersi yönde ana "
            "seansta hareket etmesi. %50 = yazı-tura. Negatif 'ort. hareket' "
            "= pre-market hareketi ana seansta SİLİNİYOR/tersine dönüyor "
            "demek; pozitif = gerçek hacimle DEVAM ediyor demek.")
        send_telegram_message("\n".join(satirlar))

    except Exception as e:
        send_telegram_message(f"❌ Pre-market backtest hatası: {e}")
    finally:
        with _kilit:
            _calisiyor["premarket"] = False


# =============================================================================
# 6) PRE-MARKET'TE ≥%2 YÜKSELEN HİSSELER — ana seans açılınca ne oluyor?
# + o gün endeks (SPY) pre-market'te yukarıda mı aşağıda mı bağlamı
# =============================================================================
PREMARKET_GAP_ESIK_PCT = 2.0
PREMARKET_GAP_HEDEF_DAKIKALAR = [30, 60, 120]
PREMARKET_GAP_MIN_ORNEK = 15
PREMARKET_GAP_ENDEKS_YATAY_BANT = 0.2  # bu aralıktaki SPY hareketi "yatay" sayılır


def _spy_premarket_haritasi():
    """{tarih: spy_premarket_gap_%} sözlüğü döner - o gün genel piyasa
    bağlamını (endeks yukarıda mıydı) her hisse-olayına eklemek için."""
    harita = {}
    try:
        df = yf.Ticker("SPY").history(period=PREMARKET_PERIYOD, interval="5m", prepost=True)
    except Exception:
        return harita
    if df is None or df.empty:
        return harita
    try:
        if df.index.tz is not None:
            df.index = df.index.tz_convert("America/New_York")
    except Exception:
        pass

    onceki_kapanis = None
    for gun, grup in df.groupby(df.index.date):
        grup = grup.sort_index()
        ana_seans_maske = (grup.index.hour > 9) | ((grup.index.hour == 9) & (grup.index.minute >= 30))
        if not ana_seans_maske.any() or ana_seans_maske.all():
            continue
        ana_baslangic_konum = ana_seans_maske.argmax()
        premarket_grup = grup.iloc[:ana_baslangic_konum]
        ana_seans_grup = grup.iloc[ana_baslangic_konum:]

        if onceki_kapanis is not None and not premarket_grup.empty:
            pre_son = premarket_grup["Close"].iloc[-1]
            if not pd.isna(pre_son) and onceki_kapanis != 0:
                harita[gun] = (pre_son - onceki_kapanis) / onceki_kapanis * 100
        if not ana_seans_grup.empty:
            son_fiyat = ana_seans_grup["Close"].iloc[-1]
            if not pd.isna(son_fiyat):
                onceki_kapanis = son_fiyat
    return harita


def premarket_gap_backtest_calistir():
    with _kilit:
        if _calisiyor.get("premarket_gap"):
            send_telegram_message("⏳ Pre-market gap testi zaten çalışıyor, bekle.")
            return
        _calisiyor["premarket_gap"] = True

    try:
        tickers = AK.AKILLI_PARA_TICKERS
        send_telegram_message(
            f"🔬 Pre-market ≥%{PREMARKET_GAP_ESIK_PCT:.0f} gap testi başladı ({BACKTEST_SURUM})\n"
            f"Son {PREMARKET_PERIYOD}, {len(tickers)} hisse taranıyor - "
            f"bu birkaç dakika sürebilir...")

        spy_haritasi = _spy_premarket_haritasi()

        sonuc_ufuk = {dk: [] for dk in PREMARKET_GAP_HEDEF_DAKIKALAR}
        gun_sonu_getiriler = []
        baglam_getiri = {"endeks_yukari": [], "endeks_asagi": [], "endeks_yatay": []}
        toplam_olay = 0

        for i, ticker in enumerate(tickers):
            try:
                df = yf.Ticker(ticker).history(period=PREMARKET_PERIYOD,
                                                interval="5m", prepost=True)
            except Exception:
                continue
            if df is None or df.empty:
                continue
            try:
                if df.index.tz is not None:
                    df.index = df.index.tz_convert("America/New_York")
            except Exception:
                pass

            onceki_kapanis = None
            for gun, grup in df.groupby(df.index.date):
                grup = grup.sort_index()
                ana_seans_maske = (grup.index.hour > 9) | \
                                   ((grup.index.hour == 9) & (grup.index.minute >= 30))
                if not ana_seans_maske.any() or ana_seans_maske.all():
                    continue
                ana_baslangic_konum = ana_seans_maske.argmax()
                premarket_grup = grup.iloc[:ana_baslangic_konum]
                ana_seans_grup = grup.iloc[ana_baslangic_konum:]

                if onceki_kapanis is None or premarket_grup.empty:
                    if not ana_seans_grup.empty:
                        son_fiyat = ana_seans_grup["Close"].iloc[-1]
                        if not pd.isna(son_fiyat):
                            onceki_kapanis = son_fiyat
                    continue

                pre_son = premarket_grup["Close"].iloc[-1]
                if pd.isna(pre_son) or onceki_kapanis == 0:
                    if not ana_seans_grup.empty:
                        son_fiyat = ana_seans_grup["Close"].iloc[-1]
                        if not pd.isna(son_fiyat):
                            onceki_kapanis = son_fiyat
                    continue

                premarket_gap = (pre_son - onceki_kapanis) / onceki_kapanis * 100

                if premarket_gap >= PREMARKET_GAP_ESIK_PCT and not ana_seans_grup.empty:
                    ana_acilis = ana_seans_grup["Open"].iloc[0]
                    if not pd.isna(ana_acilis) and ana_acilis != 0:
                        toplam_olay += 1
                        for dk in PREMARKET_GAP_HEDEF_DAKIKALAR:
                            bar_sayisi = dk // 5
                            if bar_sayisi < len(ana_seans_grup):
                                hedef_fiyat = ana_seans_grup["Close"].iloc[bar_sayisi]
                                if not pd.isna(hedef_fiyat):
                                    getiri = (hedef_fiyat - ana_acilis) / ana_acilis * 100
                                    sonuc_ufuk[dk].append(getiri)

                        gun_sonu_fiyat = ana_seans_grup["Close"].iloc[-1]
                        gun_sonu_getiri = None
                        if not pd.isna(gun_sonu_fiyat):
                            gun_sonu_getiri = (gun_sonu_fiyat - ana_acilis) / ana_acilis * 100
                            gun_sonu_getiriler.append(gun_sonu_getiri)

                        spy_gap = spy_haritasi.get(gun)
                        if spy_gap is not None and gun_sonu_getiri is not None:
                            if spy_gap > PREMARKET_GAP_ENDEKS_YATAY_BANT:
                                kategori = "endeks_yukari"
                            elif spy_gap < -PREMARKET_GAP_ENDEKS_YATAY_BANT:
                                kategori = "endeks_asagi"
                            else:
                                kategori = "endeks_yatay"
                            baglam_getiri[kategori].append(gun_sonu_getiri)

                if not ana_seans_grup.empty:
                    son_fiyat = ana_seans_grup["Close"].iloc[-1]
                    if not pd.isna(son_fiyat):
                        onceki_kapanis = son_fiyat

            if i % 20 == 0:
                print(f"[Backtest] Pre-market gap testi {i}/{len(tickers)}", flush=True)

        # --- Rapor ---
        satirlar = [
            f"📊 PRE-MARKET ≥%{PREMARKET_GAP_ESIK_PCT:.0f} GAP TEST SONUCU ({PREMARKET_PERIYOD})",
            f"Toplam bulunan olay (hisse-gün): {toplam_olay}\n",
            "Ana seans açılışından sonra (açılış fiyatına göre):"]

        for dk in PREMARKET_GAP_HEDEF_DAKIKALAR:
            degerler = sonuc_ufuk[dk]
            if len(degerler) < PREMARKET_GAP_MIN_ORNEK:
                satirlar.append(f"  +{dk}dk: örnek yetersiz (n={len(degerler)})")
                continue
            ort = sum(degerler) / len(degerler)
            yukselme_orani = sum(1 for d in degerler if d > 0) / len(degerler) * 100
            satirlar.append(
                f"  +{dk}dk: n={len(degerler)}, ort %{ort:+.2f}, "
                f"yükselişte kalma oranı %{yukselme_orani:.0f}")

        if len(gun_sonu_getiriler) >= PREMARKET_GAP_MIN_ORNEK:
            ort_gun = sum(gun_sonu_getiriler) / len(gun_sonu_getiriler)
            yukselme_gun = sum(1 for d in gun_sonu_getiriler if d > 0) / len(gun_sonu_getiriler) * 100
            satirlar.append(
                f"  Gün sonu (kapanış): n={len(gun_sonu_getiriler)}, ort %{ort_gun:+.2f}, "
                f"yükselişte kalma oranı %{yukselme_gun:.0f}")

        satirlar.append("\nO gün S&P 500 (SPY) pre-market'te ne durumdaydı (gün sonu sonucuna göre):")
        for kategori, etiket in [("endeks_yukari", f"Endeks de YUKARIDA (>%{PREMARKET_GAP_ENDEKS_YATAY_BANT})"),
                                   ("endeks_asagi", f"Endeks AŞAĞIDA (<-%{PREMARKET_GAP_ENDEKS_YATAY_BANT})"),
                                   ("endeks_yatay", "Endeks YATAY")]:
            degerler = baglam_getiri[kategori]
            if len(degerler) < 5:
                satirlar.append(f"  {etiket}: örnek yetersiz (n={len(degerler)})")
                continue
            ort = sum(degerler) / len(degerler)
            satirlar.append(f"  {etiket}: n={len(degerler)}, gün sonu ort %{ort:+.2f}")

        satirlar.append(
            "\nℹ️ Tüm getiriler ANA SEANS AÇILIŞ fiyatına göre (pre-market "
            "seviyesine göre değil) - yani '+30dk ort %+0.5' demek, açılıştan "
            "sonra fiyat ortalama daha da yükseldi demek. Negatifse, pre-market "
            "yükselişi ana seansta erimeye başlıyor demek.")
        send_telegram_message("\n".join(satirlar))

    except Exception as e:
        send_telegram_message(f"❌ Pre-market gap backtest hatası: {e}")
    finally:
        with _kilit:
            _calisiyor["premarket_gap"] = False


# =============================================================================
# KOMUT DİNLEME — bu token'ı başka HİÇBİR modül dinlemiyor, kendi
# getUpdates döngüsünü açması güvenli (409 Conflict riski yok).
# =============================================================================
def send_telegram_document(dosya_yolu: str, caption: str = ""):
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        print(f"[Backtest devre dışı] dosya: {dosya_yolu}", flush=True)
        return
    try:
        with open(dosya_yolu, "rb") as f:
            requests.post(
                f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendDocument",
                data={"chat_id": TELEGRAM_CHAT_ID, "caption": caption[:1024]},
                files={"document": f}, timeout=60)
    except Exception as e:
        print(f"[Backtest] Dosya gönderilemedi: {e}", flush=True)


# =============================================================================
# 7) PRE-MARKET "PATLAMA" ARAŞTIRMASI — sadece ≥%2 pre-market VE açılıştan
# kapanışa EK olarak ≥%5 yükselen olayları bulup, HER BİRİNİ tek tek
# inceliyor (hacim, haber, teknik gösterge, o günkü endeks durumu).
# Amaç: önceki testte ortalamaya gömülen "gerçek patlayanları" ayıklamak.
# =============================================================================
PATLAMA_ESIK_PCT = 5.0  # açılıştan kapanışa EK yükseliş eşiği


def _hacim_orani_gunluk(ticker: str, tarih):
    """O günün hacminin, önceki 20 günün ortalamasına oranı."""
    try:
        df = yf.Ticker(ticker).history(period="4mo")
        if df.empty:
            return None
        if df.index.tz is not None:
            df.index = df.index.tz_localize(None)
        tarih_ts = pd.Timestamp(tarih)
        if tarih_ts not in df.index:
            sonrakiler = df[df.index >= tarih_ts]
            if sonrakiler.empty:
                return None
            tarih_ts = sonrakiler.index[0]
        konum = df.index.get_loc(tarih_ts)
        if isinstance(konum, slice):
            konum = konum.start
        if konum < 20:
            return None
        o_gun_hacim = df["Volume"].iloc[konum]
        ort_hacim = df["Volume"].iloc[konum - 20:konum].mean()
        if ort_hacim == 0 or pd.isna(ort_hacim):
            return None
        return o_gun_hacim / ort_hacim
    except Exception:
        return None


def _rsi_hesapla(seri, periyot: int = 14):
    fark = seri.diff()
    kazanc = fark.clip(lower=0)
    kayip = -fark.clip(upper=0)
    ort_kazanc = kazanc.rolling(periyot).mean()
    ort_kayip = kayip.rolling(periyot).mean()
    rs = ort_kazanc / ort_kayip.replace(0, float("nan"))
    rsi = 100 - (100 / (1 + rs))
    # Kenar durumlar: hiç düşüş yoksa (ort_kayip==0) RSI=100 olmalı, normal
    # bölme bunu NaN yapıyor - düzeltiyoruz. İkisi de sıfırsa (hiç hareket
    # yok) RSI=50 (nötr) sayıyoruz.
    rsi = rsi.where(ort_kayip != 0, 100.0)
    ikisi_de_sifir = (ort_kayip == 0) & (ort_kazanc == 0)
    rsi = rsi.where(~ikisi_de_sifir, 50.0)
    return rsi


def _teknik_gostergeler(ticker: str, tarih):
    """Olay gününden ÖNCEKİ verilerle hesaplanan RSI ve SMA konumu -
    "olaydan önce hisse zaten güçlü müydü" sorusuna cevap."""
    try:
        df = yf.Ticker(ticker).history(period="6mo")
        if df.empty:
            return None
        if df.index.tz is not None:
            df.index = df.index.tz_localize(None)
        tarih_ts = pd.Timestamp(tarih)
        oncekiler = df[df.index < tarih_ts]
        if len(oncekiler) < 50:
            return None
        kapanislar = oncekiler["Close"]
        son_rsi = _rsi_hesapla(kapanislar).iloc[-1]
        sma20 = kapanislar.rolling(20).mean().iloc[-1]
        sma50 = kapanislar.rolling(50).mean().iloc[-1]
        son_fiyat = kapanislar.iloc[-1]
        if pd.isna(son_rsi) or pd.isna(sma20) or pd.isna(sma50):
            return None
        return {"rsi": son_rsi, "sma20_uzerinde": son_fiyat > sma20,
                "sma50_uzerinde": son_fiyat > sma50}
    except Exception:
        return None


def _o_gun_haber_var_mi(ticker: str, tarih):
    if not AK.FINNHUB_API_KEY:
        return None
    try:
        tarih_str = tarih.strftime("%Y-%m-%d") if hasattr(tarih, "strftime") else str(tarih)
        r = requests.get(
            "https://finnhub.io/api/v1/company-news",
            params={"symbol": ticker, "from": tarih_str, "to": tarih_str,
                    "token": AK.FINNHUB_API_KEY},
            timeout=15)
        if r.status_code != 200:
            return None
        veri = r.json()
        if not isinstance(veri, list):
            return None
        onemli = [h for h in veri if AK._kaynak_guvenilir_mi(h.get("source", ""))
                  and AK._onemli_haber_mi(h.get("headline", ""))]
        if onemli:
            return onemli[0].get("headline", "")
        elif veri:
            return f"(filtre dışı {len(veri)} haber var, önemli/güvenilir değil)"
        return None
    except Exception:
        return None


def premarket_patlama_arastirmasi_calistir():
    with _kilit:
        if _calisiyor.get("patlama"):
            send_telegram_message("⏳ Patlama araştırması zaten çalışıyor, bekle.")
            return
        _calisiyor["patlama"] = True

    try:
        tickers = AK.AKILLI_PARA_TICKERS
        send_telegram_message(
            f"🔬 Pre-market patlama araştırması başladı ({BACKTEST_SURUM})\n"
            f"Önce ≥%{PREMARKET_GAP_ESIK_PCT:.0f} pre-market gap + açılıştan "
            f"kapanışa ≥%{PATLAMA_ESIK_PCT:.0f} ek yükseliş yapan hisse-günler "
            f"bulunacak, sonra her biri hacim/haber/teknik gösterge açısından "
            f"incelenecek. Bu 10-20 dakika sürebilir...")

        spy_haritasi = _spy_premarket_haritasi()
        patlamalar = []

        for i, ticker in enumerate(tickers):
            try:
                df = yf.Ticker(ticker).history(period=PREMARKET_PERIYOD,
                                                interval="5m", prepost=True)
            except Exception:
                continue
            if df is None or df.empty:
                continue
            try:
                if df.index.tz is not None:
                    df.index = df.index.tz_convert("America/New_York")
            except Exception:
                pass

            onceki_kapanis = None
            for gun, grup in df.groupby(df.index.date):
                grup = grup.sort_index()
                ana_seans_maske = (grup.index.hour > 9) | \
                                   ((grup.index.hour == 9) & (grup.index.minute >= 30))
                if not ana_seans_maske.any() or ana_seans_maske.all():
                    continue
                ana_baslangic_konum = ana_seans_maske.argmax()
                premarket_grup = grup.iloc[:ana_baslangic_konum]
                ana_seans_grup = grup.iloc[ana_baslangic_konum:]

                if onceki_kapanis is None or premarket_grup.empty:
                    if not ana_seans_grup.empty:
                        sf = ana_seans_grup["Close"].iloc[-1]
                        if not pd.isna(sf):
                            onceki_kapanis = sf
                    continue

                pre_son = premarket_grup["Close"].iloc[-1]
                if pd.isna(pre_son) or onceki_kapanis == 0:
                    if not ana_seans_grup.empty:
                        sf = ana_seans_grup["Close"].iloc[-1]
                        if not pd.isna(sf):
                            onceki_kapanis = sf
                    continue

                premarket_gap = (pre_son - onceki_kapanis) / onceki_kapanis * 100

                if premarket_gap >= PREMARKET_GAP_ESIK_PCT and not ana_seans_grup.empty:
                    ana_acilis = ana_seans_grup["Open"].iloc[0]
                    gun_sonu_fiyat = ana_seans_grup["Close"].iloc[-1]
                    if not pd.isna(ana_acilis) and ana_acilis != 0 and not pd.isna(gun_sonu_fiyat):
                        acilis_kapanis = (gun_sonu_fiyat - ana_acilis) / ana_acilis * 100
                        if acilis_kapanis >= PATLAMA_ESIK_PCT:
                            toplam_gun = (gun_sonu_fiyat - onceki_kapanis) / onceki_kapanis * 100
                            patlamalar.append({
                                "ticker": ticker, "tarih": gun,
                                "premarket_gap": premarket_gap,
                                "acilis_kapanis": acilis_kapanis,
                                "toplam_gun": toplam_gun,
                                "spy_gap": spy_haritasi.get(gun),
                            })

                if not ana_seans_grup.empty:
                    sf = ana_seans_grup["Close"].iloc[-1]
                    if not pd.isna(sf):
                        onceki_kapanis = sf

            if i % 20 == 0:
                print(f"[Backtest] Patlama taraması {i}/{len(tickers)}, "
                      f"şimdiye kadar {len(patlamalar)} olay", flush=True)

        send_telegram_message(
            f"🔎 {len(patlamalar)} 'patlayan' hisse-gün bulundu, şimdi her biri "
            f"detaylı inceleniyor (hacim/haber/teknik)...")

        detaylar = []
        for p in patlamalar:
            hacim_orani = _hacim_orani_gunluk(p["ticker"], p["tarih"])
            teknik = _teknik_gostergeler(p["ticker"], p["tarih"])
            haber = _o_gun_haber_var_mi(p["ticker"], p["tarih"])
            detaylar.append({**p, "hacim_orani": hacim_orani, "teknik": teknik, "haber": haber})
            time.sleep(1.1)  # Finnhub 60/dk limiti

        # --- Rapor ---
        haberli = sum(1 for d in detaylar if d["haber"] and "filtre dışı" not in (d["haber"] or ""))
        yuksek_hacimli = sum(1 for d in detaylar if d["hacim_orani"] and d["hacim_orani"] >= 2)
        trend_ustunde = sum(1 for d in detaylar if d["teknik"] and d["teknik"]["sma20_uzerinde"])

        satirlar = [
            "# Pre-market Patlama Araştırması",
            f"Oluşturulma: {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}",
            f"Kriter: pre-market ≥%{PREMARKET_GAP_ESIK_PCT:.0f} VE açılıştan "
            f"kapanışa ≥%{PATLAMA_ESIK_PCT:.0f} ek yükseliş",
            f"Bulunan olay sayısı: {len(detaylar)}\n",
            "## Genel Örüntü",
            f"- Gerçek/önemli haber katalizörü olan: {haberli}/{len(detaylar)}",
            f"- Hacmi ortalamanın 2x+ üzerinde olan: {yuksek_hacimli}/{len(detaylar)}",
            f"- Olay öncesi 20 günlük ortalamanın ÜZERİNDE olan (zaten trend "
            f"yükselişteydi): {trend_ustunde}/{len(detaylar)}\n",
            "## Tüm Vakalar (en büyük toplam güne göre sıralı)\n",
            "| Hisse | Tarih | Pre-market | Açılış→Kapanış | Toplam Gün | "
            "SPY (pre-market) | Hacim (20g ort.) | RSI | SMA20 Üstü | Haber |",
            "|---|---|---|---|---|---|---|---|---|",
        ]
        for d in sorted(detaylar, key=lambda x: x["toplam_gun"], reverse=True):
            hacim_str = f"{d['hacim_orani']:.1f}x" if d["hacim_orani"] else "?"
            rsi_str = f"{d['teknik']['rsi']:.0f}" if d["teknik"] else "?"
            sma_str = ("Evet" if (d["teknik"] and d["teknik"]["sma20_uzerinde"])
                       else ("Hayır" if d["teknik"] else "?"))
            spy_str = f"%{d['spy_gap']:+.1f}" if d["spy_gap"] is not None else "?"
            haber_ham = d["haber"] or "-"
            haber_str = haber_ham[:60] + ("..." if len(haber_ham) > 60 else "")
            satirlar.append(
                f"| {d['ticker']} | {d['tarih']} | %{d['premarket_gap']:+.1f} | "
                f"%{d['acilis_kapanis']:+.1f} | %{d['toplam_gun']:+.1f} | {spy_str} | "
                f"{hacim_str} | {rsi_str} | {sma_str} | {haber_str} |")

        satirlar.append(
            "\n## Not\nBu liste otomatik bir 'kural' çıkarmıyor, senin tek "
            "tek inceleyip ortak noktaları gözünle değerlendirmen için "
            "hazırlandı. SEC Form 4 (içeriden alım) kontrolü bu araştırmaya "
            "dahil edilmedi - istersen ekleyip tekrar çalıştırabiliriz.")

        rapor = "\n".join(satirlar)
        dosya_yolu = os.path.join(os.environ.get("DATA_DIR", "."),
                                   "premarket_patlama_arastirmasi.md")
        with open(dosya_yolu, "w", encoding="utf-8") as f:
            f.write(rapor)

        send_telegram_document(dosya_yolu, caption="📄 Pre-market Patlama Araştırması")

    except Exception as e:
        send_telegram_message(f"❌ Patlama araştırması hatası: {e}")
    finally:
        with _kilit:
            _calisiyor["patlama"] = False


# =============================================================================
# 8) SEKTÖR İÇİ BİRLİKTE HAREKET + SEKTÖREL HABER TEPKİ GECİKMESİ
# =============================================================================
# AKILLI_PARA_TICKERS'ın (abd_akilli_para.py) kendi içindeki gruplaşmasına
# göre - ayrı bir "gerçek" sektör listesi teyit edilmedi, bu GICS'e yakın
# genel bilgiye dayalı bir sınıflandırma. COIN/MSTR/SOFI gibi net bir
# geleneksel sektöre oturmayan hisseler bu analize dahil edilmedi.
SEKTOR_ESLESTIRME_ABD = {
    "Teknoloji": ["AAPL","MSFT","GOOGL","GOOG","AMZN","NVDA","META","TSLA","AVGO","ORCL",
                  "CRM","ADBE","AMD","CSCO","INTC","QCOM","TXN","IBM","NOW","INTU",
                  "AMAT","MU","ADI","LRCX","KLAC","SNPS","CDNS","PANW","FTNT","CRWD",
                  "PLTR","SNOW","NET","DDOG","ZS","MDB","TEAM","WDAY","ANSS","ROP",
                  "APH","GLW","HPQ","DELL","NXPI","MCHP","ON","SWKS","TER","KEYS",
                  "UBER","LYFT","ABNB","BKNG","EBAY","ETSY","SHOP","SPOT","PYPL","SQ",
                  "NFLX","DIS","CMCSA","CHTR","TMUS","VZ","T"],
    "Finans": ["JPM","BAC","WFC","C","GS","MS","SCHW","BLK","AXP","USB",
               "PNC","TFC","COF","BK","STT","SPGI","MCO","ICE","CME","CB",
               "MMC","AON","AJG","PGR","TRV","ALL","MET","PRU","AIG","V",
               "MA","FIS","FISV","PAYX","ADP"],
    "Sağlık": ["UNH","JNJ","LLY","PFE","MRK","ABBV","TMO","ABT","DHR","BMY",
               "AMGN","GILD","CVS","CI","ELV","HUM","MDT","ISRG","SYK","BSX",
               "REGN","VRTX","ZTS","BDX","EW","IDXX","MRNA","BIIB"],
    "Tüketici": ["WMT","PG","KO","PEP","COST","MCD","NKE","SBUX","TGT","LOW",
                 "HD","TJX","BKNG","MAR","CMG","YUM","DG","DLTR","ROST","ULTA",
                 "EL","CL","KMB","GIS","KHC","MDLZ","MNST","STZ","HSY","KR"],
    "Sanayi": ["BA","CAT","GE","HON","UPS","UNP","LMT","RTX","DE","MMM",
               "NOC","GD","EMR","ETN","ITW","PH","CSX","NSC","FDX","WM",
               "PCAR","CMI","ROK","DOV","XYL","IR","JCI","CARR","OTIS"],
    "Enerji": ["XOM","CVX","COP","SLB","EOG","PXD","OXY","WMB","KMI","PSX",
               "VLO","MPC","HAL","BKR","DVN","FANG","HES"],
    "Malzeme/Emlak/Kamu Hizmetleri": ["LIN","APD","SHW","ECL","FCX","NEM","DOW","DD","NUE","VMC",
                                        "NEE","DUK","SO","D","AEP","EXC","SRE","XEL","ED","PEG",
                                        "PLD","AMT","EQIX","PSA","O","SPG","WELL","DLR","AVB","EQR"],
}

TICKER_SEKTOR_HARITASI = {t: s for s, tl in SEKTOR_ESLESTIRME_ABD.items() for t in tl}

SEKTOR_MIN_HISSE_ORNEK = 5      # sektör içi korelasyon için min hisse sayısı
SEKTOR_HABER_GERI_TEST_GUN = 90
SEKTOR_KUME_MIN_HISSE = 2        # "sektörel olay günü" için aynı gün en az kaç hissede haber olmalı
SEKTOR_TEPKI_UFUKLARI = [0, 1, 2, 3, 5]
SEKTOR_TEPKI_MIN_ORNEK = 3


def _sektor_ic_korelasyon_testi(tickerlar: list, periyod: str = "2y"):
    getiri_serileri = {}
    for t in tickerlar:
        s = _getiri_serisi(t, periyod)
        if s is not None and len(s) > 100:
            getiri_serileri[t] = s
    if len(getiri_serileri) < SEKTOR_MIN_HISSE_ORNEK:
        return None
    ortak = pd.concat(getiri_serileri, axis=1).dropna()
    if len(ortak) < 50:
        return None
    sektor_ortalamasi = ortak.mean(axis=1)  # eşit ağırlıklı, basit bir sektör "endeksi"
    sonuclar = []
    for t in getiri_serileri:
        # bir hissenin kendi sektör ortalamasına karşı korelasyonu doğal
        # olarak şişer (kendisi de ortalamaya dahil) - bunu önlemek için
        # hissenin kendisini ÇIKARARAK hesaplanan ortalamayla karşılaştır.
        digerleri_ortalamasi = (ortak.drop(columns=[t]).mean(axis=1))
        kor = ortak[t].corr(digerleri_ortalamasi)
        sonuclar.append((t, kor))
    sonuclar.sort(key=lambda x: x[1])
    ort_kor = sum(k for _, k in sonuclar) / len(sonuclar)
    return {"ortalama_korelasyon": ort_kor, "hisseler": sonuclar, "n_gun": len(ortak)}


def _sektor_tepki_olcumu(sektor_gunleri: dict, tickerlar: list):
    """sektor_gunleri: {tarih: [haberli_tickerlar]} - ama tepkiyi TÜM
    sektör için ölçüyoruz (sadece haberi çıkanlar değil), çünkü sektörel
    yayılma etkisini görmek istiyoruz."""
    fiyat_serileri = {}
    for t in tickerlar:
        try:
            df = yf.Ticker(t).history(period="1y")
        except Exception:
            continue
        if df.empty:
            continue
        s = df["Close"]
        if s.index.tz is not None:
            s.index = s.index.tz_localize(None)
        fiyat_serileri[t] = s

    ufuk_getirileri = {u: [] for u in SEKTOR_TEPKI_UFUKLARI}
    for tarih in sektor_gunleri:
        tarih_ts = pd.Timestamp(tarih)
        gunluk = {u: [] for u in SEKTOR_TEPKI_UFUKLARI}
        for t in tickerlar:
            s = fiyat_serileri.get(t)
            if s is None:
                continue
            sonraki = s[s.index >= tarih_ts]
            if sonraki.empty:
                continue
            baz_konum = s.index.get_loc(sonraki.index[0])
            if isinstance(baz_konum, slice):
                baz_konum = baz_konum.start
            if baz_konum == 0:
                continue
            baz_fiyat = s.iloc[baz_konum - 1]
            if baz_fiyat == 0:
                continue
            for u in SEKTOR_TEPKI_UFUKLARI:
                hedef_konum = baz_konum + u
                if hedef_konum < len(s):
                    hedef_fiyat = s.iloc[hedef_konum]
                    if not pd.isna(hedef_fiyat):
                        gunluk[u].append((hedef_fiyat - baz_fiyat) / baz_fiyat * 100)
        for u in SEKTOR_TEPKI_UFUKLARI:
            if gunluk[u]:
                ufuk_getirileri[u].append(sum(gunluk[u]) / len(gunluk[u]))  # o günün sektör ortalaması
    return ufuk_getirileri


def sektor_analiz_calistir():
    with _kilit:
        if _calisiyor.get("sektor"):
            send_telegram_message("⏳ Sektör analizi zaten çalışıyor, bekle.")
            return
        _calisiyor["sektor"] = True

    try:
        send_telegram_message(
            f"🔬 Sektör analizi başladı ({BACKTEST_SURUM})\n"
            f"Önce sektör içi birlikte hareket, sonra sektörel haber tepki "
            f"gecikmesi test edilecek - bu 10-20 dakika sürebilir...")

        # --- BÖLÜM A: Sektör içi birlikte hareket ---
        satirlar = ["# Sektör Analizi Raporu",
                    f"Oluşturulma: {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}\n",
                    "## Bölüm A: Sektör İçi Birlikte Hareket Etme (son 2 yıl)\n",
                    "Korelasyon, her hissenin KENDİSİ HARİÇ sektör ortalamasına göre "
                    "hesaplandı (kendisiyle karşılaştırma şişirmesin diye).\n"]

        for sektor_adi, tickerlar in SEKTOR_ESLESTIRME_ABD.items():
            sonuc = _sektor_ic_korelasyon_testi(tickerlar)
            if sonuc is None:
                satirlar.append(f"### {sektor_adi}\nVeri yetersiz.\n")
                continue
            satirlar.append(
                f"### {sektor_adi} (sektör içi ort. korelasyon: "
                f"{sonuc['ortalama_korelasyon']:+.2f}, n_gün={sonuc['n_gun']})")
            en_bagimsiz = sonuc["hisseler"][:3]
            en_uyumlu = sonuc["hisseler"][-3:][::-1]
            satirlar.append("En BAĞIMSIZ hareket edenler (sektörden kopuk):")
            for t, k in en_bagimsiz:
                satirlar.append(f"  {t}: {k:+.2f}")
            satirlar.append("En UYUMLU hareket edenler (sektörle aynı):")
            for t, k in en_uyumlu:
                satirlar.append(f"  {t}: {k:+.2f}")
            satirlar.append("")

        # --- BÖLÜM B: Sektörel haber kümelenmesi + tepki gecikmesi ---
        send_telegram_message(
            "🔎 Bölüm A tamamlandı. Şimdi son 90 günün haberleri taranıp "
            "sektörel kümelenme aranıyor (bu kısım daha uzun sürer)...")

        bitis_dt = datetime.now(timezone.utc)
        baslangic_dt = bitis_dt - timedelta(days=SEKTOR_HABER_GERI_TEST_GUN)
        baslangic_str = baslangic_dt.strftime("%Y-%m-%d")
        bitis_str = bitis_dt.strftime("%Y-%m-%d")

        # {(sektor, tarih): set(ticker)}
        sektor_gun_haritasi = defaultdict(set)
        for sektor_adi, tickerlar in SEKTOR_ESLESTIRME_ABD.items():
            for ticker in tickerlar:
                haberler = _gecmis_haberleri_cek(ticker, baslangic_str, bitis_str)
                for h in haberler:
                    baslik = h.get("headline", "")
                    if not AK._kaynak_guvenilir_mi(h.get("source", "")):
                        continue
                    if not AK._onemli_haber_mi(baslik):
                        continue
                    if not AK._haber_konusu_dogru_mu(ticker, baslik):
                        continue
                    ts = h.get("datetime")
                    if not ts:
                        continue
                    tarih = datetime.fromtimestamp(ts, tz=timezone.utc).date()
                    sektor_gun_haritasi[(sektor_adi, tarih)].add(ticker)
                time.sleep(1.1)  # Finnhub 60/dk limiti

        satirlar.append("\n---\n## Bölüm B: Sektörel Haber Tepki Hızı ve Büyüklüğü\n")
        satirlar.append(
            f"'Sektörel olay günü' = aynı sektörde aynı gün en az "
            f"{SEKTOR_KUME_MIN_HISSE} farklı hissede önemli haber çıkması. "
            f"T+0 = olay günü, T+1..T+5 = sonraki günler (sektör ortalaması).\n")

        for sektor_adi, tickerlar in SEKTOR_ESLESTIRME_ABD.items():
            ilgili_gunler = {tarih: hisseler for (s, tarih), hisseler in sektor_gun_haritasi.items()
                              if s == sektor_adi and len(hisseler) >= SEKTOR_KUME_MIN_HISSE}
            if len(ilgili_gunler) < SEKTOR_TEPKI_MIN_ORNEK:
                satirlar.append(f"### {sektor_adi}\nYeterli kümelenmiş olay bulunamadı (n={len(ilgili_gunler)}).\n")
                continue

            tepki = _sektor_tepki_olcumu(ilgili_gunler, tickerlar)
            satirlar.append(f"### {sektor_adi} (n={len(ilgili_gunler)} kümelenmiş olay günü)")
            for u in SEKTOR_TEPKI_UFUKLARI:
                degerler = tepki[u]
                if len(degerler) < SEKTOR_TEPKI_MIN_ORNEK:
                    satirlar.append(f"  T+{u}: örnek yetersiz")
                    continue
                ort = sum(degerler) / len(degerler)
                satirlar.append(f"  T+{u} gün: sektör ort. tepkisi %{ort:+.2f} (n={len(degerler)})")
            satirlar.append("")

        satirlar.append(
            "\n## Nasıl Yorumlanır\n"
            "Bölüm A: 0'a yakın/negatif korelasyon = o hisse sektöründen "
            "bağımsız hareket ediyor demek; +0.5 üzeri = sektörle güçlü "
            "birlikte hareket.\n"
            "Bölüm B: T+0 değeri T+3/T+5'ten büyükse tepki ANINDA oluyor "
            "demek; T+1-T+3 T+0'dan büyükse tepki GECİKMELİ/YAYILARAK "
            "oluyor demek (haberi geç fark edenler birkaç gün sonra alıyor). "
            "Küçük örnek sayıları (n) sonuçların güvenilirliğini sınırlıyor.")

        rapor = "\n".join(satirlar)
        dosya_yolu = os.path.join(os.environ.get("DATA_DIR", "."), "sektor_analiz_raporu.md")
        with open(dosya_yolu, "w", encoding="utf-8") as f:
            f.write(rapor)

        send_telegram_document(dosya_yolu, caption="📄 Sektör Analizi Raporu")

    except Exception as e:
        send_telegram_message(f"❌ Sektör analizi hatası: {e}")
    finally:
        with _kilit:
            _calisiyor["sektor"] = False


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
                    elif text.startswith("/acilis_backtest"):
                        threading.Thread(target=acilis_ilk15dk_backtest_calistir, daemon=True).start()
                    elif text.startswith("/korelasyon_backtest"):
                        threading.Thread(target=korelasyon_backtest_calistir, daemon=True).start()
                    elif text.startswith("/premarket_backtest"):
                        threading.Thread(target=premarket_backtest_calistir, daemon=True).start()
                    elif text.startswith("/premarket_gap_backtest"):
                        threading.Thread(target=premarket_gap_backtest_calistir, daemon=True).start()
                    elif text.startswith("/patlama_arastirmasi"):
                        threading.Thread(target=premarket_patlama_arastirmasi_calistir, daemon=True).start()
                    elif text.startswith("/sektor_analizi"):
                        threading.Thread(target=sektor_analiz_calistir, daemon=True).start()
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
        "dönüyor, büyük boşluk aynı yönde mi devam ediyor?\n"
        "/acilis_backtest — piyasa açıldıktan sonraki ilk 15 dakikadaki hareket, "
        "sonraki 1-2 saatte tersine mi dönüyor, devam mı ediyor?\n"
        "/korelasyon_backtest — BIST, ABD piyasasından (genel + sektörel: "
        "gıda/bankacılık/sanayi) ne kadar etkileniyor?\n"
        "/premarket_backtest — pre-market'teki (düşük hacimli) hareket, "
        "ana seans açıldığında (gerçek hacim) devam mı ediyor, siliniyor mu?\n"
        "/premarket_gap_backtest — pre-market'te ≥%2 yükselen hisseler ana "
        "seans açılınca ne oluyor, o gün endeks de yukarıda mıydı?\n"
        "/patlama_arastirmasi — sadece GERÇEKTEN patlayan (pre-market+açılış "
        "sonrası ek büyük yükseliş) vakaları bulup hacim/haber/teknik "
        "gösterge açısından tek tek inceler (dosya olarak gelir).\n"
        "/sektor_analizi — sektörler kendi içinde ne kadar birlikte "
        "hareket ediyor (bağımsız hareket edenler kim), ve sektörel "
        "haberlere tepki hemen mi geliyor yoksa gecikmeli mi (dosya olarak gelir).\n\n"
        "Hepsi birkaç dakika sürebilir, sonuç hazır olunca ayrı mesaj gelecek.")
