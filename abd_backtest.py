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
              "sektor": False, "sektor_patlama": False, "form4": False}


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
# 2026-09-22: Kullanıcı isteğiyle 7 geniş sektör yerine 37 alt kategoriye
# (çip, yazılım, e-ticaret, siber güvenlik vb. ayrı ayrı) bölündü - geniş
# "Teknoloji" gibi kategoriler çok heterojen olduğu için tek başına
# anlamlı sonuç vermiyordu. Bazı alt kategoriler kasıtlı olarak küçük
# (1-3 hisse) - bunlar Bölüm A/B'de "veri yetersiz" çıkacak, bu normal.
SEKTOR_ESLESTIRME_ABD = {
    # --- Teknoloji alt kategorileri ---
    "Yarı İletken/Çip": ["NVDA","AVGO","AMD","INTC","QCOM","TXN","AMAT","MU","ADI",
                          "LRCX","KLAC","NXPI","MCHP","ON","SWKS","TER","KEYS"],
    "Kurumsal Yazılım": ["MSFT","ORCL","CRM","ADBE","NOW","INTU","SNPS","CDNS",
                          "WDAY","ANSS","TEAM","PLTR"],
    "Siber Güvenlik": ["PANW","FTNT","CRWD","ZS","NET"],
    "Bulut/Veri Altyapısı": ["SNOW","DDOG","MDB"],
    "İnternet/Pazaryeri/Seyahat": ["GOOGL","GOOG","AMZN","META","EBAY","ETSY","SHOP",
                                     "SPOT","PYPL","SQ","BKNG","ABNB","MAR"],
    "Ulaşım Uygulamaları": ["UBER","LYFT"],
    "Donanım/Bilgisayar": ["AAPL","CSCO","IBM","HPQ","DELL","APH","GLW","ROP"],
    "Telekom": ["TMUS","VZ","T"],
    "Medya/Eğlence": ["NFLX","DIS","CMCSA","CHTR"],
    "Elektrikli Araç": ["TSLA"],
    # --- Finans alt kategorileri ---
    "Büyük Bankalar": ["JPM","BAC","WFC","C","USB","PNC","TFC","COF"],
    "Yatırım Bankacılığı/Broker": ["GS","MS","SCHW","BK","STT"],
    "Sigorta": ["CB","MMC","AON","AJG","PGR","TRV","ALL","MET","PRU","AIG"],
    "Ödeme Sistemleri": ["AXP","V","MA","FIS","FISV","PAYX","ADP"],
    "Varlık Yönetimi/Borsa": ["BLK","SPGI","MCO","ICE","CME"],
    # --- Sağlık alt kategorileri ---
    "Büyük İlaç": ["JNJ","LLY","PFE","MRK","ABBV","BMY"],
    "Biyoteknoloji": ["AMGN","GILD","REGN","VRTX","MRNA","BIIB"],
    "Sağlık Sigortası": ["UNH","CVS","CI","ELV","HUM"],
    "Tıbbi Cihaz": ["TMO","ABT","DHR","MDT","ISRG","SYK","BSX","ZTS","BDX","EW","IDXX"],
    # --- Tüketici alt kategorileri ---
    "Perakende": ["WMT","COST","TGT","LOW","HD","TJX","DG","DLTR","ROST","ULTA","KR"],
    "Restoran/Fast-food": ["MCD","SBUX","CMG","YUM"],
    "Gıda/İçecek": ["KO","PEP","GIS","KHC","MDLZ","MNST","STZ","HSY"],
    "Kişisel/Ev Bakım": ["PG","CL","KMB","EL"],
    "Giyim/Spor": ["NKE"],
    # --- Sanayi alt kategorileri ---
    "Havacılık/Savunma": ["BA","LMT","RTX","NOC","GD"],
    "Makine/Ekipman": ["CAT","DE","EMR","ETN","ITW","PH","DOV","XYL","IR","ROK","CMI","PCAR"],
    "Ulaştırma/Lojistik": ["UPS","UNP","CSX","NSC","FDX","WM"],
    "Genel Sanayi/Konglomera": ["GE","HON","MMM","JCI","CARR","OTIS"],
    # --- Enerji alt kategorileri ---
    "Büyük Entegre Enerji": ["XOM","CVX"],
    "Bağımsız Üretim (E&P)": ["COP","EOG","PXD","OXY","DVN","FANG","HES"],
    "Petrol Servis/Ekipman": ["SLB","HAL","BKR"],
    "Boru Hattı/Midstream": ["WMB","KMI"],
    "Rafineri": ["PSX","VLO","MPC"],
    # --- Malzeme/Emlak/Kamu alt kategorileri ---
    "Kimya": ["LIN","APD","SHW","ECL","DOW","DD"],
    "Madencilik/Metal": ["FCX","NEM","NUE","VMC"],
    "Elektrik/Kamu Hizmetleri": ["NEE","DUK","SO","D","AEP","EXC","SRE","XEL","ED","PEG"],
    "Gayrimenkul (REIT)": ["PLD","AMT","EQIX","PSA","O","SPG","WELL","DLR","AVB","EQR"],
}

TICKER_SEKTOR_HARITASI = {t: s for s, tl in SEKTOR_ESLESTIRME_ABD.items() for t in tl}

SEKTOR_MIN_HISSE_ORNEK = 3      # sektör içi korelasyon için min hisse sayısı (alt kategoriler küçük olduğu için düşürüldü)
SEKTOR_HABER_GERI_TEST_GUN = 90
SEKTOR_KUME_MIN_HISSE = 2        # "sektörel olay günü" için aynı gün en az kaç hissede haber olmalı
SEKTOR_TEPKI_UFUKLARI = [0, 1, 2, 3, 5]
SEKTOR_TEPKI_MIN_ORNEK = 3


def _sektor_ic_korelasyon_testi(tickerlar: list, periyod: str = "2y"):
    getiri_serileri = {}
    for t in tickerlar:
        s = _endeks_getirisi(t, periyod)
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


# =============================================================================
# 9) SEKTÖREL PATLAMA NEDENİ ARAŞTIRMASI — bütünleşik sektörlerde büyük
# hareket olduğunda: genel piyasa mı, hisse haberi mi, başka bir şey mi?
# Ve hangi hisseler en çok öne çıkıyor?
# =============================================================================
SEKTOR_PATLAMA_KORELASYON_ESIGI = 0.6   # sadece bütünleşik sektörlere bakılır
SEKTOR_PATLAMA_Z_ESIGI = 2.0             # istatistiksel olarak "uç" gün sayılması için
SEKTOR_PATLAMA_LIDER_SAYISI = 3          # her patlama gününde incelenecek en çok hareket eden hisse sayısı
SEKTOR_PATLAMA_SPY_ESIK_PCT = 1.0        # SPY bu kadar hareket ettiyse "genel piyasa da hareketliydi" say

# ⚠️ Bu tarihler/açıklamalar Claude'un EĞİTİM VERİSİNDEN HATIRLANMIŞTIR,
# resmi bir kaynaktan teyit edilmemiştir - özellikle 2025 sonrası için
# kesinlik garanti edilmez. Bir önceki /sektor_patlama_arastirmasi
# sonucunda "belirsiz" çıkan günlerin çoğunun aynı birkaç tarihte
# kümelendiği fark edildi (6 Kasım 2024, 18 Aralık 2024, 27 Ocak 2025,
# 3-4-9-10 Nisan 2025) - bunlar tek tek şirket haberi değil, GENEL
# MAKRO/POLİTİK olaylar. Finnhub'ın genel haber ucu geçmişe dönük tarih
# aralığı sorgulamayı desteklemediği için (sadece güncel haberler),
# bunun yerine bilinen büyük olayları elle bir takvime koyup eşleştiriyoruz.
BILINEN_MAKRO_OLAYLAR = [
    ("2024-09-18", "Fed faiz indirimi (0.50 baz puan)"),
    ("2024-11-06", "ABD başkanlık seçimi sonucu"),
    ("2024-11-07", "Fed faiz kararı"),
    ("2024-12-18", "Fed faiz kararı (2025 için daha az indirim sinyali)"),
    ("2025-01-27", "DeepSeek şoku - Çin yapay zeka modeli, ABD teknoloji/çip hisselerinde sert satış"),
    ("2025-01-29", "Fed faiz kararı (sabit)"),
    ("2025-03-19", "Fed faiz kararı"),
    ("2025-04-02", "Trump 'Liberation Day' gümrük tarifesi ilanı"),
    ("2025-04-03", "Tarife sonrası küresel satış dalgası"),
    ("2025-04-04", "Tarife sonrası satış dalgası devam"),
    ("2025-04-09", "Trump bazı tarifeleri 90 gün erteledi - büyük ralli"),
    ("2025-04-10", "Tarife belirsizliği sonrası dalgalanma"),
]
MAKRO_TARIH_TOLERANSI_GUN = 1  # olay tarihinden ±1 gün sapma da eşleşme sayılır


def _makro_gun_esles(tarih):
    tarih_ts = pd.Timestamp(tarih)
    for olay_str, aciklama in BILINEN_MAKRO_OLAYLAR:
        olay_ts = pd.Timestamp(olay_str)
        if abs((tarih_ts - olay_ts).days) <= MAKRO_TARIH_TOLERANSI_GUN:
            return aciklama
    return None


def _sektor_getiri_matrisi(tickerlar: list, periyod: str = "2y"):
    """Sektördeki her hissenin GÜNLÜK getiri serisini tek bir tabloda
    döner - hem sektör ortalamasını hem o günün 'en çok hareket eden'
    hisselerini bulmak için kullanılıyor."""
    getiri_serileri = {}
    for t in tickerlar:
        s = _endeks_getirisi(t, periyod)
        if s is not None and len(s) > 100:
            getiri_serileri[t] = s
    if len(getiri_serileri) < SEKTOR_MIN_HISSE_ORNEK:
        return None
    return pd.concat(getiri_serileri, axis=1).dropna()


# (2026-09-23 eklendi) Enerji ve Kamu Hizmetleri gibi sektörlerin "belirsiz"
# çıkan günlerinin çoğu, tek bir şirket haberinden veya Fed/seçim gibi
# genel bir olaydan DEĞİL, kendi özel sürücülerinden (petrol fiyatı, tahvil
# faizi) kaynaklanıyor olabilir. Tarih tahmin etmek yerine (belirsiz OPEC
# toplantı tarihleri gibi), o günün GERÇEK petrol/faiz hareketine bakmak
# çok daha güvenilir - tahmine dayalı bir takvim yerine ölçülebilir veri.
SEKTOR_SURUCU_TICKER = {
    "Büyük Entegre Enerji": ("CL=F", "petrol fiyatı"),
    "Bağımsız Üretim (E&P)": ("CL=F", "petrol fiyatı"),
    "Petrol Servis/Ekipman": ("CL=F", "petrol fiyatı"),
    "Boru Hattı/Midstream": ("CL=F", "petrol fiyatı"),
    "Rafineri": ("CL=F", "petrol fiyatı"),
    "Elektrik/Kamu Hizmetleri": ("^TNX", "ABD 10Y tahvil faizi"),
    "Gayrimenkul (REIT)": ("^TNX", "ABD 10Y tahvil faizi"),
}
SEKTOR_SURUCU_ESIK_PCT = 2.5  # sürücünün bu kadar hareket etmesi "kaynak bu" saymak için yeterli


def _surucu_gun_esles(sektor_adi: str, tarih, surucu_serileri: dict):
    bilgi = SEKTOR_SURUCU_TICKER.get(sektor_adi)
    if not bilgi:
        return None
    ticker, isim = bilgi
    seri = surucu_serileri.get(ticker)
    if seri is None:
        return None
    deger = seri.get(tarih)
    if deger is None or abs(deger) < SEKTOR_SURUCU_ESIK_PCT:
        return None
    return f"{isim} o gün %{deger:+.1f} hareket etti"


def sektor_patlama_nedeni_arastirmasi_calistir():
    with _kilit:
        if _calisiyor.get("sektor_patlama"):
            send_telegram_message("⏳ Sektörel patlama araştırması zaten çalışıyor, bekle.")
            return
        _calisiyor["sektor_patlama"] = True

    try:
        send_telegram_message(
            f"🔬 Sektörel patlama nedeni araştırması başladı ({BACKTEST_SURUM})\n"
            f"Önce korelasyonu ≥{SEKTOR_PATLAMA_KORELASYON_ESIGI:.1f} olan bütünleşik "
            f"sektörler seçilecek, sonra her birinin patlama günleri bulunup "
            f"neden/lider hisse araştırılacak. Bu 20-30 dakika sürebilir...")

        spy_getiri = _endeks_getirisi("SPY", "2y")
        if spy_getiri is None:
            send_telegram_message("❌ SPY verisi alınamadı, araştırma durduruldu.")
            return

        # Enerji/Kamu Hizmetleri gibi sektörler için petrol/tahvil faizi
        # serilerini önceden çekiyoruz (tekrar tekrar indirmemek için).
        surucu_serileri = {}
        for ticker, _ in SEKTOR_SURUCU_TICKER.values():
            if ticker not in surucu_serileri:
                surucu_serileri[ticker] = _endeks_getirisi(ticker, "2y")

        satirlar = ["# Sektörel Patlama Nedeni Araştırması",
                    f"Oluşturulma: {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}",
                    f"Sadece korelasyonu ≥{SEKTOR_PATLAMA_KORELASYON_ESIGI:.1f} olan "
                    f"bütünleşik sektörler incelendi.\n",
                    "⚠️ 'Bilinen makro olay' eşleştirmesi Claude'un eğitim verisinden "
                    "hatırlanan bir takvime dayanıyor (Fed toplantıları, seçim, tarife "
                    "olayları vb.) - resmi kaynaktan teyit edilmedi, özellikle 2025 "
                    "sonrası tarihler için kesinlik garanti edilmez. 'Sektör sürücüsü "
                    "hareketli' (Enerji/Kamu Hizmetleri) ise GERÇEK petrol/tahvil "
                    "faizi verisine dayanıyor, tahmin değil.\n"]

        for sektor_adi, tickerlar in SEKTOR_ESLESTIRME_ABD.items():
            matris = _sektor_getiri_matrisi(tickerlar)
            if matris is None:
                continue

            # sektörün ortalama iç korelasyonunu (Bölüm A'daki yöntemle) hesapla
            kor_sonuc = _sektor_ic_korelasyon_testi(tickerlar)
            if kor_sonuc is None or kor_sonuc["ortalama_korelasyon"] < SEKTOR_PATLAMA_KORELASYON_ESIGI:
                continue

            sektor_ortalamasi = matris.mean(axis=1)
            ort, std = sektor_ortalamasi.mean(), sektor_ortalamasi.std()
            if std == 0 or pd.isna(std):
                continue
            z_skor = (sektor_ortalamasi - ort) / std
            patlama_gunleri = sektor_ortalamasi[z_skor.abs() >= SEKTOR_PATLAMA_Z_ESIGI]
            if len(patlama_gunleri) < 3:
                satirlar.append(f"### {sektor_adi}\nYeterli patlama günü bulunamadı (n={len(patlama_gunleri)}).\n")
                continue

            print(f"[Backtest] {sektor_adi}: {len(patlama_gunleri)} patlama günü inceleniyor", flush=True)

            genel_piyasa_sayisi = 0
            haberli_sayisi = 0
            makro_sayisi = 0
            surucu_sayisi = 0
            belirsiz_sayisi = 0
            lider_sayaci = defaultdict(int)
            ornek_gunler = []

            for tarih, sektor_getiri in patlama_gunleri.items():
                spy_o_gun = spy_getiri.get(tarih)
                spy_hareketli = spy_o_gun is not None and abs(spy_o_gun) >= SEKTOR_PATLAMA_SPY_ESIK_PCT

                o_gun_getirileri = matris.loc[tarih].sort_values(
                    key=lambda x: x.abs(), ascending=False)
                liderler = o_gun_getirileri.head(SEKTOR_PATLAMA_LIDER_SAYISI)
                for t in liderler.index:
                    lider_sayaci[t] += 1

                haber_bulundu = None
                for t in liderler.index:
                    h = _o_gun_haber_var_mi(t, tarih)
                    time.sleep(1.1)
                    if h and "filtre dışı" not in h:
                        haber_bulundu = (t, h)
                        break

                makro_aciklama = None if haber_bulundu else _makro_gun_esles(tarih)
                surucu_aciklama = (None if (haber_bulundu or makro_aciklama)
                                    else _surucu_gun_esles(sektor_adi, tarih, surucu_serileri))

                if haber_bulundu:
                    haberli_sayisi += 1
                elif makro_aciklama:
                    makro_sayisi += 1
                elif surucu_aciklama:
                    surucu_sayisi += 1
                elif spy_hareketli:
                    genel_piyasa_sayisi += 1
                else:
                    belirsiz_sayisi += 1

                if len(ornek_gunler) < 5:
                    lider_str = ", ".join(f"{t}(%{v:+.1f})" for t, v in liderler.items())
                    if haber_bulundu:
                        aciklama_str = f"{haber_bulundu[0]}: {haber_bulundu[1][:70]}"
                    elif makro_aciklama:
                        aciklama_str = f"[BİLİNEN MAKRO OLAY] {makro_aciklama}"
                    elif surucu_aciklama:
                        aciklama_str = f"[SEKTÖR SÜRÜCÜSÜ] {surucu_aciklama}"
                    else:
                        aciklama_str = "yok"
                    spy_str = f"%{spy_o_gun:+.1f}" if spy_o_gun is not None else "?"
                    ornek_gunler.append(
                        f"  {tarih}: sektör %{sektor_getiri:+.1f}, SPY {spy_str}, "
                        f"en hareketli: {lider_str}, sebep: {aciklama_str}")

            n = len(patlama_gunleri)
            satirlar.append(
                f"### {sektor_adi} (korelasyon {kor_sonuc['ortalama_korelasyon']:+.2f}, "
                f"n={n} patlama günü)")
            satirlar.append(
                f"- Hisse haberi bulundu (sektöre yayılan şirket-özel tetikleyici): "
                f"{haberli_sayisi}/{n}")
            satirlar.append(
                f"- Bilinen makro/politik olayla eşleşti (Fed, seçim, tarife vb.): "
                f"{makro_sayisi}/{n}")
            if sektor_adi in SEKTOR_SURUCU_TICKER:
                surucu_isim = SEKTOR_SURUCU_TICKER[sektor_adi][1]
                satirlar.append(
                    f"- Sektör sürücüsü ({surucu_isim}) o gün hareketliydi: {surucu_sayisi}/{n}")
            satirlar.append(
                f"- Genel piyasa hareketliydi ama tanımlı bir olayla eşleşmedi: "
                f"{genel_piyasa_sayisi}/{n}")
            satirlar.append(f"- Gerçekten belirsiz (hiçbiri): {belirsiz_sayisi}/{n}")

            en_sik_liderler = sorted(lider_sayaci.items(), key=lambda x: x[1], reverse=True)[:5]
            satirlar.append("En sık öne çıkan hisseler (patlama günlerinde ilk 3'e girme sayısı):")
            for t, sayi in en_sik_liderler:
                satirlar.append(f"  {t}: {sayi}/{n} günde")

            satirlar.append("Örnek günler:")
            satirlar.extend(ornek_gunler)
            satirlar.append("")

        satirlar.append(
            "\n## Not\nHer patlama gününde sadece en çok hareket eden "
            f"{SEKTOR_PATLAMA_LIDER_SAYISI} hissede haber arandı (tüm sektörde "
            "değil) - API çağrısını sınırlamak için. Bu yüzden 'belirsiz' "
            "sayılanların bir kısmında aslında daha az öne çıkan bir hissede "
            "gerçek bir haber olabilir, kaçırılmış olabilir.")

        rapor = "\n".join(satirlar)
        dosya_yolu = os.path.join(os.environ.get("DATA_DIR", "."),
                                   "sektor_patlama_nedeni_arastirmasi.md")
        with open(dosya_yolu, "w", encoding="utf-8") as f:
            f.write(rapor)

        send_telegram_document(dosya_yolu, caption="📄 Sektörel Patlama Nedeni Araştırması")

    except Exception as e:
        send_telegram_message(f"❌ Sektörel patlama araştırması hatası: {e}")
    finally:
        with _kilit:
            _calisiyor["sektor_patlama"] = False


# =============================================================================
# 10) SEC FORM 4 (İÇERİDEN İŞLEM) GERİYE DÖNÜK TESTİ
# Canlı sistemde (abd_akilli_para.py) zaten sinyal olarak kullanılan bu
# veri kaynağı HİÇ geriye dönük test edilmemişti - bu, ilk doğrulaması.
# =============================================================================
FORM4_BACKTEST_GUN_SAYISI = 180  # filing tarihine göre ne kadar geriye gidilecek
FORM4_BACKTEST_MIN_ORNEK = 8


def _fiyat_serisi_al_uzun(ticker: str):
    """_fiyat_serisi_al'ın 6 aylık versiyonu bu test için yetersiz kalabilir
    (180 gün öncesine kadar giden bir filing + 10 gün sonrası gerekebilir) -
    bu yüzden ayrı, 1 yıllık bir versiyon."""
    try:
        df = yf.Ticker(ticker).history(period="1y")
        return df if not df.empty else None
    except Exception:
        return None


def sec_form4_backtest_calistir():
    with _kilit:
        if _calisiyor.get("form4"):
            send_telegram_message("⏳ SEC Form4 geriye dönük testi zaten çalışıyor, bekle.")
            return
        _calisiyor["form4"] = True

    try:
        tickers = AK.AKILLI_PARA_TICKERS
        send_telegram_message(
            f"🔬 SEC Form4 (içeriden işlem) geriye dönük testi başladı ({BACKTEST_SURUM})\n"
            f"Son {FORM4_BACKTEST_GUN_SAYISI} gün, {len(tickers)} hisse taranıyor - "
            f"bu 15-25 dakika sürebilir...")

        cik_map = AK._cik_map_yukle()
        if not cik_map:
            send_telegram_message("❌ CIK haritası alınamadı, test durduruldu.")
            return

        cutoff_str = (datetime.now(timezone.utc) -
                      timedelta(days=FORM4_BACKTEST_GUN_SAYISI)).strftime("%Y-%m-%d")

        getiriler = {"ALIM": {g: [] for g in UFUK_GUNLERI},
                     "SATIM": {g: [] for g in UFUK_GUNLERI}}
        toplam_islem = 0

        # --- Teşhis sayaçları (0 sonuç çıkarsa NEREDE tıkandığını görmek için) ---
        tanı = {"cik_bulunamayan": 0, "submissions_hata": 0, "form4_yok_pencerede": 0,
                "fiyat_verisi_yok": 0, "xml_index_hata": 0, "xml_indirme_hata": 0,
                "xml_parse_hata": 0, "getiri_hesaplanamayan": 0, "tutar_esigi_altinda": 0,
                "cik_bulunan_ticker": 0, "form4_bulunan_ticker": 0}

        for i, ticker in enumerate(tickers):
            cik = cik_map.get(ticker.replace("-", ".")) or cik_map.get(ticker)
            if not cik:
                tanı["cik_bulunamayan"] += 1
                continue
            tanı["cik_bulunan_ticker"] += 1
            try:
                r = requests.get(f"https://data.sec.gov/submissions/CIK{cik}.json",
                                  headers=AK._SEC_HEADERS, timeout=15)
            except Exception:
                tanı["submissions_hata"] += 1
                time.sleep(0.3)
                continue
            if r.status_code != 200:
                tanı["submissions_hata"] += 1
                time.sleep(0.3)
                continue

            veri = r.json()
            son = veri.get("filings", {}).get("recent", {})
            formlar = son.get("form", [])
            accessionlar = son.get("accessionNumber", [])
            tarihler_str = son.get("filingDate", [])

            dortler = [(a, t) for f, a, t in zip(formlar, accessionlar, tarihler_str)
                       if f == "4" and t >= cutoff_str]
            if not dortler:
                tanı["form4_yok_pencerede"] += 1
                time.sleep(0.3)
                continue
            tanı["form4_bulunan_ticker"] += 1

            fiyat_df = _fiyat_serisi_al_uzun(ticker)
            if fiyat_df is None:
                tanı["fiyat_verisi_yok"] += 1
                time.sleep(0.3)
                continue

            cik_no_lead = str(int(cik))
            for accession, tarih_str in dortler:
                try:
                    xml_url = AK._form4_xml_url_bul(cik_no_lead, accession)
                    if not xml_url:
                        tanı["xml_index_hata"] += 1
                        continue
                    xr = requests.get(xml_url, headers=AK._SEC_HEADERS, timeout=15)
                    if xr.status_code != 200:
                        tanı["xml_indirme_hata"] += 1
                        continue
                    detay = AK._form4_xml_ayristir(xr.content)
                except Exception:
                    tanı["xml_parse_hata"] += 1
                    continue

                try:
                    olay_tarihi = datetime.strptime(tarih_str, "%Y-%m-%d").replace(tzinfo=timezone.utc)
                except ValueError:
                    continue
                getiri_sozlugu = _getirileri_hesapla(fiyat_df, olay_tarihi)
                if not getiri_sozlugu:
                    tanı["getiri_hesaplanamayan"] += 1
                    continue

                for tx in detay["islemler"]:
                    if tx["lot"] is None:
                        continue
                    tutar = tx["lot"] * tx["fiyat"] if tx["fiyat"] else None
                    if tutar is not None and tutar < AK.FORM4_MIN_TUTAR_USD:
                        tanı["tutar_esigi_altinda"] += 1
                        continue
                    yon = "ALIM" if tx["kod"] == "P" else "SATIM"
                    toplam_islem += 1
                    for g, deger in getiri_sozlugu.items():
                        getiriler[yon][g].append(deger)

            if i % 20 == 0:
                print(f"[Backtest] Form4 testi {i}/{len(tickers)}, "
                      f"şimdiye kadar {toplam_islem} işlem", flush=True)
            time.sleep(0.3)  # SEC EDGAR'a nazik davranmak için

        # --- Rapor ---
        satirlar = [
            f"📊 SEC FORM4 (İÇERİDEN İŞLEM) GERİYE DÖNÜK TEST SONUCU "
            f"(son {FORM4_BACKTEST_GUN_SAYISI} gün)",
            f"Toplam bulunan işlem (≥${AK.FORM4_MIN_TUTAR_USD:,.0f}): {toplam_islem}\n".replace(",", "."),
            "🔍 Teşhis (0 çıkarsa nerede tıkandığını gösterir):",
            f"  CIK bulunamayan hisse: {tanı['cik_bulunamayan']}/{len(tickers)}",
            f"  CIK bulunan hisse: {tanı['cik_bulunan_ticker']}/{len(tickers)}",
            f"  submissions.json alınamayan: {tanı['submissions_hata']}",
            f"  {FORM4_BACKTEST_GUN_SAYISI} gün penceresinde hiç Form4 olmayan: {tanı['form4_yok_pencerede']}",
            f"  Penceresinde Form4 bulunan hisse: {tanı['form4_bulunan_ticker']}",
            f"  Fiyat verisi alınamayan: {tanı['fiyat_verisi_yok']}",
            f"  XML index bulunamayan filing: {tanı['xml_index_hata']}",
            f"  XML indirilemeyen filing: {tanı['xml_indirme_hata']}",
            f"  XML ayrıştırılamayan filing: {tanı['xml_parse_hata']}",
            f"  Fiyat getirisi hesaplanamayan: {tanı['getiri_hesaplanamayan']}",
            f"  ${AK.FORM4_MIN_TUTAR_USD:,.0f} eşiğinin altında kalan işlem: {tanı['tutar_esigi_altinda']}\n".replace(",", ".")]

        for yon, baslik_tr in [("ALIM", "🟢 İçeriden ALIM işlemleri"),
                                 ("SATIM", "🔴 İçeriden SATIM işlemleri")]:
            satirlar.append(baslik_tr + ":")
            for g in UFUK_GUNLERI:
                degerler = getiriler[yon][g]
                if len(degerler) < FORM4_BACKTEST_MIN_ORNEK:
                    satirlar.append(f"  {g}. gün: örnek yetersiz (n={len(degerler)})")
                    continue
                ort = sum(degerler) / len(degerler)
                beklenen_dogru = (sum(1 for d in degerler if (d > 0) == (yon == "ALIM"))
                                   / len(degerler) * 100)
                isaret = "✅" if (ort > 0) == (yon == "ALIM") else "❌"
                satirlar.append(
                    f"  {g}. gün: ort %{ort:+.2f}, beklenen yönde %{beklenen_dogru:.0f} "
                    f"(n={len(degerler)}) {isaret}")
            satirlar.append("")

        satirlar.append(
            "ℹ️ 1. gün = SEC'e bildirim tarihi, referans = bir önceki kapanış. "
            "✅ ortalama getiri beklenen yönde (alımdan sonra yükseliş, satımdan "
            "sonra düşüş), ❌ ters yönde. Bu, canlı sistemde zaten sinyal olarak "
            "kullanılan bu veri kaynağının GEÇMİŞE dönük ilk doğrulaması.")

        send_telegram_message("\n".join(satirlar))

    except Exception as e:
        send_telegram_message(f"❌ SEC Form4 backtest hatası: {e}")
    finally:
        with _kilit:
            _calisiyor["form4"] = False


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
                    elif text.startswith("/sektor_patlama_arastirmasi"):
                        threading.Thread(target=sektor_patlama_nedeni_arastirmasi_calistir, daemon=True).start()
                    elif text.startswith("/form4_backtest"):
                        threading.Thread(target=sec_form4_backtest_calistir, daemon=True).start()
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
        "haberlere tepki hemen mi geliyor yoksa gecikmeli mi (dosya olarak gelir).\n"
        "/sektor_patlama_arastirmasi — bütünleşik sektörlerde büyük hareket "
        "olduğunda neden oluyor (hisse haberi mi, genel piyasa mı, başka "
        "bir şey mi) ve hangi hisseler en çok öne çıkıyor (dosya olarak gelir).\n"
        "/form4_backtest — canlı sistemde kullanılan SEC Form4 (içeriden "
        "alım-satım) sinyalinin geçmişe dönük ilk doğrulaması.\n\n"
        "Hepsi birkaç dakika sürebilir, sonuç hazır olunca ayrı mesaj gelecek.")
