"""
abd_opsiyon_sinyal.py — OPSİYON (CALL/PUT) ALICILARI İÇİN CANLI SİNYAL
==============================================================================
2026-09-29 — Kullanıcının "ABD'de sadece opsiyon için takip ediyorum,
call/put alıp yön bahsi yapıyorum, vade değişken" açıklamasıyla kuruldu.

DÜZ hisse fiyat tahmininden farklı bir şey ölçüyor: yön + o anki
volatilitenin (IV) ucuz mu pahalı mı olduğu + kazanç takvimi riski -
üçü birlikte olmadan sadece "hisse yükselecek" bilgisi opsiyon alıcısı
için yeterli değil (IV çok pahalıyken doğru yönü bile tutturup zarar
edebilirsin, ya da kazanç sonrası IV çöküşü (crush) kârı yiyebilir).

KATMAN 1 — Yön tetikleyicisi (TEST EDİLDİ): /gap_detay_backtest sonucuna
göre gap %2-4 + hacim≥1.5x aralığında devam etme oranı %62-69 çıkmıştı,
bu aralığın dışında (küçük ya da çok büyük gap) yazı-turaya düşüyordu.
Bu yüzden tetikleyici SADECE bu doğrulanmış aralık.

KATMAN 2 — IV/HV oranı (TEST EDİLMEDİ, mantığa dayalı): Şu anki IV'nin
kendi geçmişine göre ucuz/pahalı olduğunu ölçecek ücretsiz bir geçmiş IV
kaynağı yok (yfinance sadece ANLIK opsiyon zincirini veriyor). Bunun
yerine standart bir alternatif: IV'yi hissenin kendi GERÇEKLEŞEN
(realized) volatilitesiyle karşılaştırmak. IV, gerçekleşenden çok
yüksekse piyasa "büyük hareket gelecek" diye fazla fiyatlıyor demektir.

KATMAN 3 — Kazanç takvimi (TEST EDİLMEDİ, mantığa dayalı): Yaklaşan
kazanç açıklaması varsa uyarı veriyor.

⚠️ Sadece Katman 1 geriye dönük test edildi. Katman 2 ve 3 mantığa dayalı
eklemeler, henüz kanıtlanmadı - bot raporun sonunda bunu her seferinde
hatırlatıyor.

TELEGRAM: abd_sosyal_duygu.py / abd_akilli_para.py ile AYNI
TELEGRAM_TOKEN/CHAT_ID kullanılıyor - bu token'ı hiçbir modül getUpdates
ile dinlemiyor (hepsi tek yönlü mesaj gönderiyor), bu yüzden burada da
komut dinlemeye gerek yok, sadece tarama + bildirim.
"""
import os
import math
import time
from datetime import datetime, timezone, date

import requests
import pandas as pd
import yfinance as yf

import abd_akilli_para as AK  # AKILLI_PARA_TICKERS'ı TEKRAR YAZMADAN kullanmak için

TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN", "")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")

OPSIYON_SURUM = "abd-opsiyon-sinyal-v1-2026-09-29"

# --- Katman 1: yön tetikleyicisi (doğrulanmış aralık) ---
GAP_MIN_PCT = 2.0
GAP_MAX_PCT = 4.0
HACIM_ESIK_KATSAYI = 1.5

# --- Katman 2: IV/HV eşikleri ---
IV_HV_PAHALI_ESIK = 1.3
IV_HV_UCUZ_ESIK = 0.9
REALIZED_VOL_GUN = 20

# --- Katman 3: kazanç takvimi ---
KAZANC_UYARI_GUN = 7

TARAMA_ARALIGI_SN = 15 * 60  # her 15 dakikada bir tam tur
TICKER_ARASI_BEKLEME_SN = 1.0
GUNLUK_LIMIT = 1  # bir hissede günde en fazla 1 bildirim (aynı gap olayını tekrar tekrar bildirmesin)

REQUIRED_ENV_VARS = ["TELEGRAM_TOKEN", "TELEGRAM_CHAT_ID"]


def validate_opsiyon_config():
    return [n for n in REQUIRED_ENV_VARS if not os.environ.get(n)]


def send_telegram_message(text: str):
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        print(f"[Opsiyon devre dışı] {text}", flush=True)
        return
    try:
        requests.post(
            f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage",
            json={"chat_id": TELEGRAM_CHAT_ID, "text": text[:4000]}, timeout=20)
    except Exception as e:
        print(f"[Opsiyon Telegram hata] {e}", flush=True)


# =============================================================================
# KATMAN 2 — IV/HV
# =============================================================================
def _realized_vol_hesapla(ticker: str, gun: int = REALIZED_VOL_GUN):
    """Son `gun` günün GERÇEKLEŞEN (realized) yıllıklandırılmış
    volatilitesini (%) döner - IV'yi karşılaştıracağımız referans."""
    try:
        df = yf.Ticker(ticker).history(period="3mo")
        if df.empty or len(df) < gun + 1:
            return None
        getiriler = []
        kapanislar = df["Close"].tolist()
        for i in range(1, len(kapanislar)):
            if kapanislar[i - 1] > 0 and kapanislar[i] > 0:
                getiriler.append(math.log(kapanislar[i] / kapanislar[i - 1]))
        son_getiriler = getiriler[-gun:]
        if len(son_getiriler) < gun // 2:
            return None
        ortalama = sum(son_getiriler) / len(son_getiriler)
        varyans = sum((g - ortalama) ** 2 for g in son_getiriler) / (len(son_getiriler) - 1)
        std_gunluk = varyans ** 0.5
        return std_gunluk * (252 ** 0.5) * 100  # yıllıklandırılmış %
    except Exception:
        return None


def _atm_iv_bul(ticker: str, piyasa_fiyati: float):
    """En yakın vadeli opsiyon zincirinden, piyasa fiyatına en yakın
    (ATM - at the money) call ve put'un ortalama implied volatility'sini
    (%) döner. (vade_str, iv_pct) - veri yoksa (None, None)."""
    try:
        tk = yf.Ticker(ticker)
        vadeler = tk.options
        if not vadeler:
            return None, None
        vade = vadeler[0]
        zincir = tk.option_chain(vade)
        calls, puts = zincir.calls, zincir.puts
        if calls.empty or puts.empty:
            return None, None
        calls = calls.copy()
        calls["fark"] = (calls["strike"] - piyasa_fiyati).abs()
        puts = puts.copy()
        puts["fark"] = (puts["strike"] - piyasa_fiyati).abs()
        en_yakin_call = calls.sort_values("fark").iloc[0]
        en_yakin_put = puts.sort_values("fark").iloc[0]
        iv_call = en_yakin_call.get("impliedVolatility")
        iv_put = en_yakin_put.get("impliedVolatility")
        if iv_call is None or iv_put is None or pd.isna(iv_call) or pd.isna(iv_put):
            return None, vade
        return (iv_call + iv_put) / 2 * 100, vade
    except Exception:
        return None, None


# =============================================================================
# KATMAN 3 — Kazanç takvimi
# =============================================================================
def _kazanc_tarihi_bul(ticker: str):
    try:
        tk = yf.Ticker(ticker)
        df = tk.get_earnings_dates(limit=4)
        if df is None or df.empty:
            return None
        simdi = pd.Timestamp.now(tz=df.index.tz) if df.index.tz is not None else pd.Timestamp.now()
        gelecekteki = df[df.index >= simdi]
        if gelecekteki.empty:
            return None
        return gelecekteki.index[0].date()
    except Exception:
        return None


# =============================================================================
# KATMAN 1 + BİRLEŞTİRME
# =============================================================================
_gunluk_sayac = {}  # {ticker: (tarih_str, sayac)}


def _gunluk_sayac_al(ticker: str, bugun_str: str) -> int:
    kayit = _gunluk_sayac.get(ticker)
    if kayit is None or kayit[0] != bugun_str:
        return 0
    return kayit[1]


def _gunluk_sayac_artir(ticker: str, bugun_str: str):
    _gunluk_sayac[ticker] = (bugun_str, _gunluk_sayac_al(ticker, bugun_str) + 1)


def _ticker_tara(ticker: str):
    bugun_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    if _gunluk_sayac_al(ticker, bugun_str) >= GUNLUK_LIMIT:
        return

    try:
        df = yf.Ticker(ticker).history(period="30d")
    except Exception:
        return
    if df.empty or len(df) < 22:
        return

    onceki_kapanis = df["Close"].iloc[-2]
    bugunku_acilis = df["Open"].iloc[-1]
    bugunku_hacim = df["Volume"].iloc[-1]
    ort_hacim20 = df["Volume"].iloc[-21:-1].mean()
    if onceki_kapanis == 0 or pd.isna(ort_hacim20) or ort_hacim20 == 0:
        return

    gap_pct = (bugunku_acilis - onceki_kapanis) / onceki_kapanis * 100
    abs_gap = abs(gap_pct)
    if not (GAP_MIN_PCT <= abs_gap < GAP_MAX_PCT):
        return

    hacim_orani = bugunku_hacim / ort_hacim20
    if hacim_orani < HACIM_ESIK_KATSAYI:
        return

    guncel_fiyat = df["Close"].iloc[-1]

    satirlar = [
        f"📈 OPSİYON SİNYALİ — {ticker}",
        f"Yön: {'🔼 YUKARI' if gap_pct > 0 else '🔽 AŞAĞI'} (gap %{gap_pct:+.1f}, "
        f"hacim {hacim_orani:.1f}x ortalama)",
        f"Şu anki fiyat: ${guncel_fiyat:.2f}",
    ]

    hv = _realized_vol_hesapla(ticker)
    iv, vade = _atm_iv_bul(ticker, guncel_fiyat)
    if iv is not None and hv is not None and hv > 0:
        oran = iv / hv
        if oran >= IV_HV_PAHALI_ESIK:
            etiket = "PAHALI ⚠️ (gerçekleşenden çok yüksek fiyatlanmış)"
        elif oran <= IV_HV_UCUZ_ESIK:
            etiket = "UCUZ ✅ (gerçekleşene göre makul/düşük)"
        else:
            etiket = "NORMAL"
        satirlar.append(f"IV (~{vade}): %{iv:.0f}  /  HV({REALIZED_VOL_GUN}g): %{hv:.0f}  →  {etiket}")
    else:
        satirlar.append("IV/HV karşılaştırması yapılamadı (opsiyon/fiyat verisi eksik)")

    kazanc_tarihi = _kazanc_tarihi_bul(ticker)
    if kazanc_tarihi is not None:
        gun_kaldi = (kazanc_tarihi - date.today()).days
        if 0 <= gun_kaldi <= KAZANC_UYARI_GUN:
            satirlar.append(
                f"⚠️ {gun_kaldi} gün sonra KAZANÇ AÇIKLAMASI var - "
                f"açıklama sonrası IV çöküşü (crush) riski, yön tutsa bile kârı yiyebilir")

    satirlar.append(
        "\nℹ️ Sadece yön katmanı (gap %2-4) geriye dönük test edildi. "
        "IV/HV ve kazanç katmanları mantığa dayalı, HENÜZ KANITLANMADI - "
        "bir görüş, garanti değil.")

    send_telegram_message("\n".join(satirlar))
    _gunluk_sayac_artir(ticker, bugun_str)


def opsiyon_kontrol_dongusu():
    eksik = validate_opsiyon_config()
    if eksik:
        print(f"[Opsiyon] Eksik ayar: {eksik} - döngü başlatılmadı.", flush=True)
        return
    tur = 0
    while True:
        for ticker in AK.AKILLI_PARA_TICKERS:
            try:
                _ticker_tara(ticker)
            except Exception as e:
                print(f"[Opsiyon] {ticker} tarama hatası: {e}", flush=True)
            time.sleep(TICKER_ARASI_BEKLEME_SN)
        tur += 1
        print(f"[Opsiyon] Tur #{tur} tamamlandı.", flush=True)
        time.sleep(TARAMA_ARALIGI_SN)


def baslangic():
    eksik = validate_opsiyon_config()
    if eksik:
        print(f"[Opsiyon] Eksik ayar: {eksik}", flush=True)
        return
    send_telegram_message(
        f"📈 ABD Opsiyon Sinyal Sistemi AKTİF — {OPSIYON_SURUM}\n\n"
        f"Gap %{GAP_MIN_PCT:.0f}-{GAP_MAX_PCT:.0f} + hacim≥{HACIM_ESIK_KATSAYI}x "
        f"tetiklenince IV/HV oranı ve kazanç takvimi bilgisiyle birlikte "
        f"bildirim gelecek (hisse başı günde en fazla {GUNLUK_LIMIT} bildirim).\n\n"
        "⚠️ Sadece yön katmanı (gap) geriye dönük test edildi - IV/HV ve "
        "kazanç katmanları mantığa dayalı eklemeler, henüz kanıtlanmadı.")
