"""
us_sinyal_botu.py — TAMAMEN YENİDEN KURULMUŞ CANLI SİSTEM
============================================================
2026-08-19 — Kullanıcının açık kararıyla: BIST tarama mantığı, eski ABD
sistemleri (ATR Kırılımı, Hacim Z-Skor, küçük-hedefli RSI21 gün-içi),
AI/ML modelleri (ml_radar.py, model.pkl, overnight_model.pkl), KAP
gözlemcisi — HEPSİ KALDIRILDI. Bot artık SADECE aşağıdaki 8 göstergeyi
kullanıyor; hepsi arge_botu.py'de gün İçi 15dk verisiyle, kör temel
çizgiye (koşulsuz LONG/SHORT) karşı, büyük örneklemli (60-106 hisse,
binlerce sinyal) titiz testlerle doğrulandı:

  Gösterge                 | Kör'e göre R farkı (isabet)
  --------------------------|------------------------------
  Donchian-20 Kırılımı      | +0.20  (%76.5)
  EMA9/21 Kesişimi          | +0.16  (%74.4)
  ADX+DI Yön                | +0.16  (%74.9)
  Awesome Oscillator        | +0.16  (%74.6)
  Bollinger Bandı Dokunuşu  | +0.15  (%73.9)
  CCI (±100)                | +0.14  (%73.2)
  VWAP Sapması              | +0.15  (%75.8)
  MACD Kesişimi             | +0.10  (%72.0)

ÇALIŞMA MANTIĞI: ABD piyasa saatleri boyunca (16:30-23:00 TR saati =
9:30-16:00 ET) 15 dakikada bir tüm hisseleri tarar. Bir gösterge o gün
İLK KEZ tetiklenirse (aynı gün aynı göstergeden ikinci sinyal
verilmez) Telegram'a bildirim gönderir, pozisyonu kaydeder. Ayrı bir
arka plan görevi, açık pozisyonları GERÇEK checkpoint sistemiyle
(1g/%1, 3g/%2, 5g/%3, 10g/%5 - herhangi biri tutarsa isabet, canlı
ATR/Hacim Z-Skor sisteminin kullandığı AYNI mantık) takip edip
sonuçlandırır.

Bu bot SİNYAL ÜRETİR, otomatik emir vermez - karar kullanıcıya ait.
"""
import os
import time
import threading
import csv
from datetime import datetime, timezone, date, timedelta

import requests
import numpy as np
import pandas as pd
from flask import Flask

# =============================================================================
# YAPILANDIRMA
# =============================================================================
TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN", "")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")
DATA_DIR = os.environ.get("DATA_DIR", ".")
PORT = int(os.environ.get("PORT", "10000"))
TARAMA_ARALIGI_SANIYE = int(os.environ.get("TARAMA_ARALIGI_SANIYE", "900"))  # 15 dk

BOT_KOD_SURUMU = "v3-arge-botu-entegrasyonu-2026-08-19"

US_TICKERS = [
    "AAPL", "MSFT", "GOOGL", "AMZN", "NVDA", "META", "TSLA", "BRK-B", "JPM",
    "V", "UNH", "MA", "HD", "PG", "COST", "XOM", "JNJ", "ABBV", "MRK",
    "AVGO", "PEP", "KO", "BAC", "WMT", "CRM", "ADBE", "AMD", "NFLX", "DIS",
    "CSCO", "ORCL", "INTC", "QCOM", "TXN", "PFE", "NKE", "MCD", "GS", "CAT",
    "BA", "LLY", "TMO", "ABT", "DHR", "ACN", "LIN", "MDT", "NEE", "PM",
    "UNP", "RTX", "HON", "SBUX", "LOW", "INTU", "AMGN", "IBM", "GE", "CVX",
    "WFC", "MS", "SCHW", "BLK", "SPGI", "AXP", "C", "T", "VZ", "CMCSA",
    "AMAT", "MU", "LRCX", "ADI", "KLAC", "SNPS", "CDNS", "PANW", "CRWD",
    "NOW", "UBER", "ABNB", "PYPL", "XYZ", "SHOP", "COIN", "MRNA", "GILD",
    "BMY", "CVS", "CI", "ELV", "HCA", "DE", "MMM", "LMT", "NOC", "GD",
    "EOG", "SLB", "COP", "PSX", "MPC", "VLO", "NEM", "FCX", "DOW",
]

# --- gösterge parametreleri (arge_botu.py'de doğrulanan AYNI değerler) ---
DONCHIAN_PERIOD = 20
EMA_HIZLI, EMA_YAVAS = 9, 21
ADX_PERIOD = 14
AO_KISA, AO_UZUN = 5, 34
BOLL_PERIOD, BOLL_STD = 20, 2.0
CCI_PERIOD, CCI_ESIK = 20, 100
MACD_HIZLI, MACD_YAVAS, MACD_SINYAL = 12, 26, 9
VWAP_SAPMA_ESIK_PCT = 1.0

CHECKPOINTS = [(1, "1g", 1.0), (3, "3g", 2.0), (5, "5g", 3.0), (10, "10g", 5.0)]

GOSTERGE_ISIMLERI = [
    "Donchian-20 Kırılımı", "EMA9/21 Kesişimi", "ADX+DI Yön",
    "Awesome Oscillator", "Bollinger Bandı Dokunuşu", "CCI",
    "VWAP Sapması", "MACD Kesişimi",
]

PENDING_CSV = os.path.join(DATA_DIR, "us_sinyal_pending.csv")
PENDING_FIELDNAMES = ["ticker", "gosterge", "yon", "giris_fiyat", "giris_tarihi",
                       "kapandi", "sonuc", "checked_1g", "checked_3g",
                       "checked_5g", "checked_10g"]

# =============================================================================
# TELEGRAM YARDIMCI FONKSİYONLARI
# =============================================================================

def send_telegram_message(text: str):
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        print(f"[Telegram devre dışı] {text}", flush=True)
        return
    try:
        requests.post(
            f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage",
            json={"chat_id": TELEGRAM_CHAT_ID, "text": text},
            timeout=15,
        )
    except Exception as e:
        print(f"[Telegram hata] {e}", flush=True)


def send_telegram_document(dosya_yolu: str, caption: str = ""):
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        print(f"[Telegram devre dışı] Dosya: {dosya_yolu} | {caption}", flush=True)
        return
    try:
        with open(dosya_yolu, "rb") as f:
            requests.post(
                f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendDocument",
                data={"chat_id": TELEGRAM_CHAT_ID, "caption": caption[:1024]},
                files={"document": f},
                timeout=30,
            )
    except Exception as e:
        print(f"[Telegram dosya hatası] {e}", flush=True)


# =============================================================================
# GÖSTERGE FORMÜLLERİ (arge_botu.py'de doğrulanan, DEĞİŞTİRİLMEDEN taşındı)
# =============================================================================

def _ema_hesapla(close, n):
    return close.ewm(span=n, adjust=False).mean()


def _rsi_hesapla(close, n=14):
    delta = close.diff()
    gain = delta.clip(lower=0)
    loss = -delta.clip(upper=0)
    avg_gain = gain.ewm(alpha=1 / n, adjust=False).mean()
    avg_loss = loss.ewm(alpha=1 / n, adjust=False).mean()
    rs = avg_gain / avg_loss.replace(0, np.nan)
    return 100 - (100 / (1 + rs))


def _macd_hesapla(close, hizli=12, yavas=26, sinyal=9):
    macd_line = _ema_hesapla(close, hizli) - _ema_hesapla(close, yavas)
    sinyal_line = _ema_hesapla(macd_line, sinyal)
    return macd_line, sinyal_line


def _adx_di_hesapla(high, low, close, n=14):
    up_move = high.diff()
    down_move = -low.diff()
    plus_dm = pd.Series(np.where((up_move > down_move) & (up_move > 0), up_move, 0), index=high.index)
    minus_dm = pd.Series(np.where((down_move > up_move) & (down_move > 0), down_move, 0), index=high.index)
    tr = pd.concat([(high - low), (high - close.shift()).abs(), (low - close.shift()).abs()], axis=1).max(axis=1)
    atr = tr.ewm(alpha=1 / n, adjust=False).mean()
    plus_di = 100 * plus_dm.ewm(alpha=1 / n, adjust=False).mean() / atr.replace(0, np.nan)
    minus_di = 100 * minus_dm.ewm(alpha=1 / n, adjust=False).mean() / atr.replace(0, np.nan)
    dx = 100 * (plus_di - minus_di).abs() / (plus_di + minus_di).replace(0, np.nan)
    adx = dx.ewm(alpha=1 / n, adjust=False).mean()
    return adx, plus_di, minus_di


def _bollinger_hesapla(close, n=20, k=2.0):
    orta = close.rolling(n).mean()
    std = close.rolling(n).std()
    return orta - k * std, orta + k * std


def _cci_hesapla(high, low, close, n=20):
    tipik_fiyat = (high + low + close) / 3
    sma = tipik_fiyat.rolling(n).mean()
    ort_sapma = tipik_fiyat.rolling(n).apply(lambda x: np.abs(x - x.mean()).mean(), raw=True)
    return (tipik_fiyat - sma) / (0.015 * ort_sapma.replace(0, np.nan))


def _awesome_oscillator_hesapla(high, low, kisa=5, uzun=34):
    medyan = (high + low) / 2
    return medyan.rolling(kisa).mean() - medyan.rolling(uzun).mean()


def _vwap_gun_ici_hesapla(high, low, close, volume, gun_serisi):
    tipik = (high + low + close) / 3
    pv = tipik * volume
    gun_df = pd.DataFrame({"pv": pv, "volume": volume, "gun": gun_serisi})
    kumulatif_pv = gun_df.groupby("gun")["pv"].cumsum()
    kumulatif_vol = gun_df.groupby("gun")["volume"].cumsum()
    return kumulatif_pv / kumulatif_vol.replace(0, np.nan)


def _tum_gostergeleri_hesapla(df: pd.DataFrame) -> pd.DataFrame:
    """df: ts, open, high, low, close, volume, gun kolonlarını içerir.
    Tüm 8 göstergeyi ekler."""
    df["donchian_ust"] = df["high"].rolling(DONCHIAN_PERIOD).max()
    df["donchian_alt"] = df["low"].rolling(DONCHIAN_PERIOD).min()
    df["ema_hizli"] = _ema_hesapla(df["close"], EMA_HIZLI)
    df["ema_yavas"] = _ema_hesapla(df["close"], EMA_YAVAS)
    adx, plus_di, minus_di = _adx_di_hesapla(df["high"], df["low"], df["close"], ADX_PERIOD)
    df["adx"], df["plus_di"], df["minus_di"] = adx, plus_di, minus_di
    df["ao"] = _awesome_oscillator_hesapla(df["high"], df["low"], AO_KISA, AO_UZUN)
    alt_bant, ust_bant = _bollinger_hesapla(df["close"], BOLL_PERIOD, BOLL_STD)
    df["boll_alt"], df["boll_ust"] = alt_bant, ust_bant
    df["cci"] = _cci_hesapla(df["high"], df["low"], df["close"], CCI_PERIOD)
    df["vwap"] = _vwap_gun_ici_hesapla(df["high"], df["low"], df["close"], df["volume"], df["gun"])
    df["vwap_sapma_pct"] = (df["close"] - df["vwap"]) / df["vwap"] * 100
    macd_line, macd_sinyal = _macd_hesapla(df["close"], MACD_HIZLI, MACD_YAVAS, MACD_SINYAL)
    df["macd_line"], df["macd_sinyal"] = macd_line, macd_sinyal
    return df


def gostergeleri_kontrol_et(bar, onceki) -> list:
    """Son bar için 8 göstergeyi kontrol eder, tetiklenenleri
    [(gosterge_adi, yon), ...] olarak döner."""
    sonuc = []
    if pd.notna(bar["donchian_ust"]):
        if bar["close"] >= bar["donchian_ust"]:
            sonuc.append(("Donchian-20 Kırılımı", "LONG"))
        elif bar["close"] <= bar["donchian_alt"]:
            sonuc.append(("Donchian-20 Kırılımı", "SHORT"))
    if pd.notna(bar["ema_hizli"]) and pd.notna(onceki["ema_hizli"]):
        if onceki["ema_hizli"] <= onceki["ema_yavas"] and bar["ema_hizli"] > bar["ema_yavas"]:
            sonuc.append(("EMA9/21 Kesişimi", "LONG"))
        elif onceki["ema_hizli"] >= onceki["ema_yavas"] and bar["ema_hizli"] < bar["ema_yavas"]:
            sonuc.append(("EMA9/21 Kesişimi", "SHORT"))
    if pd.notna(bar["adx"]) and bar["adx"] >= 25 and pd.notna(onceki["plus_di"]):
        if onceki["plus_di"] <= onceki["minus_di"] and bar["plus_di"] > bar["minus_di"]:
            sonuc.append(("ADX+DI Yön", "LONG"))
        elif onceki["plus_di"] >= onceki["minus_di"] and bar["plus_di"] < bar["minus_di"]:
            sonuc.append(("ADX+DI Yön", "SHORT"))
    if pd.notna(bar["ao"]) and pd.notna(onceki["ao"]):
        if onceki["ao"] <= 0 and bar["ao"] > 0:
            sonuc.append(("Awesome Oscillator", "LONG"))
        elif onceki["ao"] >= 0 and bar["ao"] < 0:
            sonuc.append(("Awesome Oscillator", "SHORT"))
    if pd.notna(bar["boll_alt"]):
        if bar["close"] <= bar["boll_alt"]:
            sonuc.append(("Bollinger Bandı Dokunuşu", "LONG"))
        elif bar["close"] >= bar["boll_ust"]:
            sonuc.append(("Bollinger Bandı Dokunuşu", "SHORT"))
    if pd.notna(bar["cci"]):
        if bar["cci"] <= -CCI_ESIK:
            sonuc.append(("CCI", "LONG"))
        elif bar["cci"] >= CCI_ESIK:
            sonuc.append(("CCI", "SHORT"))
    if pd.notna(bar["vwap_sapma_pct"]):
        if bar["vwap_sapma_pct"] <= -VWAP_SAPMA_ESIK_PCT:
            sonuc.append(("VWAP Sapması", "LONG"))
        elif bar["vwap_sapma_pct"] >= VWAP_SAPMA_ESIK_PCT:
            sonuc.append(("VWAP Sapması", "SHORT"))
    if pd.notna(bar["macd_line"]) and pd.notna(onceki["macd_line"]):
        if onceki["macd_line"] <= onceki["macd_sinyal"] and bar["macd_line"] > bar["macd_sinyal"]:
            sonuc.append(("MACD Kesişimi", "LONG"))
        elif onceki["macd_line"] >= onceki["macd_sinyal"] and bar["macd_line"] < bar["macd_sinyal"]:
            sonuc.append(("MACD Kesişimi", "SHORT"))
    return sonuc


# =============================================================================
# TAKİP (bugün kim tetiklendi) + PENDING (checkpoint) YÖNETİMİ
# =============================================================================
# 2026-08-19 DÜZELTME: kullanıcının canlı geri bildirimiyle - artık HİSSE
# BAŞINA (gösterge başına değil) günde bir kez tetikleniyor, tüm
# destekleyen göstergeler TEK bir bildirimde birleştiriliyor.

_bugun_tetiklenenler = {}  # {ticker: tarih}


def _bugun_tetiklendi_mi(ticker, bugun) -> bool:
    return _bugun_tetiklenenler.get(ticker) == bugun


def _bugun_tetiklendi_isaretle(ticker, bugun):
    _bugun_tetiklenenler[ticker] = bugun


def _pending_oku() -> list:
    if not os.path.exists(PENDING_CSV):
        return []
    with open(PENDING_CSV, "r", newline="", encoding="utf-8-sig") as f:
        return list(csv.DictReader(f))


def _pending_yaz(satirlar: list):
    with open(PENDING_CSV, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.DictWriter(f, fieldnames=PENDING_FIELDNAMES)
        w.writeheader()
        for s in satirlar:
            w.writerow(s)


def sinyal_kaydet(ticker, gosterge, yon, giris_fiyat, giris_tarihi):
    satirlar = _pending_oku()
    satirlar.append({
        "ticker": ticker, "gosterge": gosterge, "yon": yon,
        "giris_fiyat": round(giris_fiyat, 4), "giris_tarihi": giris_tarihi.isoformat(),
        "kapandi": "0", "sonuc": "", "checked_1g": "0", "checked_3g": "0",
        "checked_5g": "0", "checked_10g": "0",
    })
    _pending_yaz(satirlar)


def checkpointleri_kontrol_et():
    """Açık (kapanmamış) sinyalleri gerçek fiyat verisiyle kontrol eder,
    herhangi bir checkpoint tutmuşsa ya da 10 gün dolmuşsa sonuçlandırır."""
    satirlar = _pending_oku()
    if not satirlar:
        return
    degisti = False
    bugun = date.today()

    for satir in satirlar:
        if satir["kapandi"] == "1":
            continue
        ticker = satir["ticker"]
        giris_tarihi = date.fromisoformat(satir["giris_tarihi"])
        giris_fiyat = float(satir["giris_fiyat"])
        yon = satir["yon"]
        gecen_is_gunu = np.busday_count(giris_tarihi, bugun)

        try:
            df = yf_history_guvenli(ticker, period="30d", interval="1d")
            if df is None or df.empty:
                continue
            df.index = pd.to_datetime(df.index).tz_localize(None)
            sonraki_barlar = df[df.index.date > giris_tarihi]
            if sonraki_barlar.empty:
                continue

            for gun_sayisi, etiket, hedef_pct in CHECKPOINTS:
                kolon = f"checked_{etiket}"
                if satir[kolon] == "1":
                    continue
                if gecen_is_gunu < gun_sayisi:
                    continue
                satir[kolon] = "1"
                degisti = True
                if len(sonraki_barlar) < gun_sayisi:
                    continue
                ilgili_bar = sonraki_barlar.iloc[gun_sayisi - 1]
                if yon == "LONG":
                    hedef_fiyat = giris_fiyat * (1 + hedef_pct / 100)
                    tutti = ilgili_bar["High"] >= hedef_fiyat
                else:
                    hedef_fiyat = giris_fiyat * (1 - hedef_pct / 100)
                    tutti = ilgili_bar["Low"] <= hedef_fiyat
                if tutti:
                    satir["kapandi"] = "1"
                    satir["sonuc"] = f"WIN_{etiket}_{hedef_pct}%"
                    send_telegram_message(
                        f"✅ HEDEF TUTTU: {ticker} {yon} ({satir['gosterge']})\n"
                        f"Giriş: {giris_fiyat:.2f} | Hedef: {etiket} +%{hedef_pct} "
                        f"| Giriş tarihi: {giris_tarihi}"
                    )
                    break

            if satir["kapandi"] != "1" and gecen_is_gunu >= 10:
                satir["kapandi"] = "1"
                satir["sonuc"] = "LOSS_10g_hicbiri_tutmadi"
                send_telegram_message(
                    f"❌ SÜRE DOLDU: {ticker} {yon} ({satir['gosterge']}) - "
                    f"10 işlem gününde hiçbir hedef tutmadı.\n"
                    f"Giriş: {giris_fiyat:.2f} | Giriş tarihi: {giris_tarihi}"
                )
        except Exception as e:
            print(f"[Checkpoint kontrol] {ticker} hata: {e}", flush=True)

    if degisti:
        _pending_yaz(satirlar)


# =============================================================================
# VERİ ÇEKME (dayanıklı, zaman aşımlı)
# =============================================================================

def yf_history_guvenli(ticker: str, period: str, interval: str, deneme=2):
    import yfinance as yf
    for i in range(deneme):
        try:
            df = yf.Ticker(ticker).history(period=period, interval=interval, timeout=20)
            if df is not None and not df.empty:
                return df
        except Exception as e:
            print(f"[yfinance] {ticker} ({period}/{interval}) deneme {i+1} hata: {e}", flush=True)
            time.sleep(2)
    return None


# =============================================================================
# ANA TARAMA
# =============================================================================

def us_piyasasi_acik_mi() -> bool:
    """Kaba bir kontrol: ABD işlem günü mü (hafta içi) ve saat aralığında mı
    (16:25-23:05 TR saati, yaz saati varsayımıyla ~9:25-16:05 ET)."""
    simdi_utc = datetime.now(timezone.utc)
    if simdi_utc.weekday() >= 5:  # Cumartesi=5, Pazar=6
        return False
    # TR saati = UTC+3
    tr_saat = (simdi_utc.hour + 3) % 24
    tr_dakika = simdi_utc.minute
    dakika_toplam = tr_saat * 60 + tr_dakika
    return (16 * 60 + 25) <= dakika_toplam <= (23 * 60 + 5)


def tek_hisse_tara(ticker: str):
    try:
        df = yf_history_guvenli(ticker, period="5d", interval="15m")
        if df is None or df.empty or len(df) < 40:
            return
        df = df.reset_index().rename(columns={
            "Datetime": "ts", "Open": "open", "High": "high", "Low": "low",
            "Close": "close", "Volume": "volume"})
        if "ts" not in df.columns:
            df = df.rename(columns={df.columns[0]: "ts"})
        df["ts"] = pd.to_datetime(df["ts"]).dt.tz_localize(None)
        df["gun"] = df["ts"].dt.date
        df = _tum_gostergeleri_hesapla(df)

        bar = df.iloc[-1]
        onceki = df.iloc[-2]
        bugun = bar["gun"]

        if _bugun_tetiklendi_mi(ticker, bugun):
            return  # bu hisse icin bugun zaten bir bildirim gonderildi

        tetiklenenler = gostergeleri_kontrol_et(bar, onceki)
        if not tetiklenenler:
            return

        long_gostergeler = [g for g, y in tetiklenenler if y == "LONG"]
        short_gostergeler = [g for g, y in tetiklenenler if y == "SHORT"]
        giris_fiyat = float(bar["close"])

        if long_gostergeler and short_gostergeler:
            # CELISEN SINYAL: gostergeler ayni anda ters yonde - pozisyon
            # KAYDETMİYORUZ (hangi yon takip edilecek belirsiz), sadece
            # bilgilendirme yapiyoruz.
            _bugun_tetiklendi_isaretle(ticker, bugun)
            send_telegram_message(
                f"⚠️ ÇELİŞEN SİNYAL: {ticker}\n"
                f"LONG diyor: {', '.join(long_gostergeler)}\n"
                f"SHORT diyor: {', '.join(short_gostergeler)}\n"
                f"Fiyat: {giris_fiyat:.2f}\n"
                f"Göstergeler anlaşamıyor - bu hisseyi şimdilik atlamanı öneririm."
            )
            return

        yon = "LONG" if long_gostergeler else "SHORT"
        destekleyenler = long_gostergeler or short_gostergeler
        _bugun_tetiklendi_isaretle(ticker, bugun)
        sinyal_kaydet(ticker, ", ".join(destekleyenler), yon, giris_fiyat, bugun)
        destek_sayisi = f" ({len(destekleyenler)} gösterge)" if len(destekleyenler) > 1 else ""
        send_telegram_message(
            f"🔔 YENİ SİNYAL: {ticker} → {yon}{destek_sayisi}\n"
            f"Destekleyen: {', '.join(destekleyenler)}\n"
            f"Giriş fiyatı: {giris_fiyat:.2f}\n"
            f"Hedefler: 1g(+%1) / 3g(+%2) / 5g(+%3) / 10g(+%5) - "
            f"herhangi biri tutarsa isabet"
        )
    except Exception as e:
        print(f"[Tarama] {ticker} hata: {e}", flush=True)


def tam_tarama_calistir():
    if not us_piyasasi_acik_mi():
        print(f"[{datetime.now().strftime('%H:%M:%S')}] Piyasa saatleri dışı, tarama atlandı.", flush=True)
        return
    print(f"[{datetime.now().strftime('%H:%M:%S')}] Tarama başlıyor ({len(US_TICKERS)} hisse)...", flush=True)
    for ticker in US_TICKERS:
        tek_hisse_tara(ticker)
        time.sleep(0.3)
    print(f"[{datetime.now().strftime('%H:%M:%S')}] Tarama bitti.", flush=True)


def arka_plan_dongusu():
    # 2026-08-19 DÜZELTME: kullanıcı günde-bir checkpoint kontrolünü az
    # buldu - artık SAATTE BİR kontrol ediliyor (aynı gün içinde bile
    # hedefin tuttuğu daha erken fark edilebilsin diye).
    CHECKPOINT_KONTROL_ARALIGI_SANIYE = 3600  # 1 saat
    son_checkpoint_kontrol_zamani = 0.0
    while True:
        try:
            tam_tarama_calistir()
            simdi = time.time()
            if simdi - son_checkpoint_kontrol_zamani >= CHECKPOINT_KONTROL_ARALIGI_SANIYE:
                checkpointleri_kontrol_et()
                son_checkpoint_kontrol_zamani = simdi
        except Exception as e:
            print(f"[Arka plan döngüsü] Hata: {e}", flush=True)
        time.sleep(TARAMA_ARALIGI_SANIYE)


# =============================================================================
# TELEGRAM KOMUTLARI (basit - durum sorgulama)
# =============================================================================

_son_update_id = None


def poll_telegram_commands():
    global _son_update_id
    if not TELEGRAM_TOKEN:
        return
    while True:
        try:
            params = {"timeout": 20}
            if _son_update_id:
                params["offset"] = _son_update_id + 1
            resp = requests.get(
                f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/getUpdates",
                params=params, timeout=25)
            data = resp.json()
            for update in data.get("result", []):
                _son_update_id = update["update_id"]
                msg = update.get("message", {})
                text = msg.get("text", "")
                if text.startswith("/durum"):
                    satirlar = _pending_oku()
                    acik = [s for s in satirlar if s["kapandi"] == "0"]
                    kapali = [s for s in satirlar if s["kapandi"] == "1"]
                    kazanan = [s for s in kapali if s["sonuc"].startswith("WIN")]
                    send_telegram_message(
                        f"📊 Durum\nAçık sinyal: {len(acik)}\n"
                        f"Kapanan: {len(kapali)} (Kazanan: {len(kazanan)})\n"
                        f"Sürüm: {BOT_KOD_SURUMU}"
                    )
                elif text.startswith("/liste"):
                    satirlar = _pending_oku()
                    if not satirlar:
                        send_telegram_message("Henüz kayıtlı sinyal yok.")
                        continue
                    dosya_yolu = os.path.join(DATA_DIR, "us_sinyal_pending_export.csv")
                    _pending_yaz(satirlar)
                    import shutil
                    shutil.copy(PENDING_CSV, dosya_yolu)
                    send_telegram_document(dosya_yolu, caption=f"Toplam {len(satirlar)} kayıt")
        except Exception as e:
            print(f"[Telegram poll] Hata: {e}", flush=True)
            time.sleep(5)


# =============================================================================
# FLASK (Render health check)
# =============================================================================

app = Flask(__name__)


@app.route("/health")
def health():
    return "OK", 200


def kendi_kendine_ping():
    """Render'ın ücretsiz katmanı 15 dk hareketsizlikte servisi uyutuyor -
    bu, botun kendi /health endpoint'ine düzenli istek atıp uyanık
    kalmasını sağlıyor. 2026-08-19: bu, önceki sistemde vardı ama yeni
    botta unutulmuştu - ilk canlı denemede servis uykuya dalıp hiç
    tarama yapmadı, bu yüzden eklendi."""
    time.sleep(30)  # once uygulamanin tam ayaga kalkmasini bekle
    while True:
        try:
            requests.get(f"http://127.0.0.1:{PORT}/health", timeout=10)
        except Exception as e:
            print(f"[Kendi kendine ping] Hata: {e}", flush=True)
        time.sleep(600)  # 10 dakika


def send_startup_message():
    gosterge_listesi = "\n".join(f"  • {g}" for g in GOSTERGE_ISIMLERI)
    send_telegram_message(
        f"🚀 ABD Gün İçi Sinyal Botu başlatıldı — {BOT_KOD_SURUMU}\n\n"
        f"Bu bot TAMAMEN YENİDEN KURULDU: eski BIST tarama, eski ABD "
        f"sistemleri (ATR Kırılımı, Hacim Z-Skor, küçük-hedefli RSI21), "
        f"AI/ML modelleri ve KAP gözlemcisi KALDIRILDI.\n\n"
        f"Şu an SADECE 8 doğrulanmış gösterge çalışıyor:\n{gosterge_listesi}\n\n"
        f"📅 Çalışma saatleri: ABD piyasa saatleri boyunca (16:30-23:00 "
        f"TR saati), {TARAMA_ARALIGI_SANIYE // 60} dakikada bir tarama.\n"
        f"🎯 Hedefler: 1g(+%1) / 3g(+%2) / 5g(+%3) / 10g(+%5) - herhangi "
        f"biri tutarsa isabet sayılır.\n"
        f"📊 {len(US_TICKERS)} ABD hissesi taranıyor.\n"
        f"🔔 Bir hissede birden fazla gösterge AYNI yönde tetiklenirse TEK "
        f"bir bildirimde birleştirilir. TERS yönde tetiklenirse "
        f"'ÇELİŞEN SİNYAL' olarak ayrı işaretlenir, pozisyon takibe "
        f"alınmaz.\n"
        f"⏱️ Hedef kontrolü artık saatte bir yapılıyor (günde bir yerine).\n"
        f"🔁 Kendi kendine ping: 10 dk'da bir (Render uyku moduna girmesin diye)\n\n"
        f"🔬 Ar-Ge Botu entegre edildi: {'✅ aktif' if _ARGE_MODUL_YUKLENDI else '❌ yüklenemedi'} "
        f"(ayrı Telegram token/sohbet - araştırma komutları oradan çalışır)\n\n"
        f"Komutlar:\n/durum — açık/kapalı sinyal özeti\n"
        f"/liste — tüm kayıtları CSV olarak gönderir\n\n"
        f"⚠️ Bu bot SADECE sinyal üretir, otomatik emir vermez."
    )


# =============================================================================
# AR-GE BOTU ENTEGRASYONU (arge_botu.py) — 2026-08-19
# =============================================================================
# GEREKÇE: arge_botu.py eskiden stock_screener_bot.py'nin içinden (aynı
# process, arka plan thread'i) çalışıyordu - kendi ARGE_TELEGRAM_TOKEN/
# ARGE_TELEGRAM_CHAT_ID'siyle (futbol botunun eski entegrasyon deseniyle
# birebir aynı). stock_screener_bot.py tamamen kaldırılınca arge_botu.py
# hiçbir yerden çağrılmaz oldu - dosya repo'da duruyordu ama hiç
# çalışmıyordu. Şimdi us_sinyal_botu.py'ye AYNI şekilde bağlanıyor.
try:
    import arge_botu
    _ARGE_MODUL_YUKLENDI = True
    print("[Ar-Ge Entegrasyonu] arge_botu.py başarıyla yüklendi.", flush=True)
except Exception as e:
    _ARGE_MODUL_YUKLENDI = False
    print(f"[Ar-Ge Entegrasyonu] arge_botu.py yüklenemedi: {e}", flush=True)


def arge_botu_dongusu():
    """arge_botu.py'nin komut dinleme + kendi kendine hipotez üretme
    döngüsünü çalıştırır - kendi try/except'i, ana botun sinyal
    taramasını hiç etkilemez."""
    if not _ARGE_MODUL_YUKLENDI:
        print("[Ar-Ge Entegrasyonu] Devre dışı - modül yüklenemedi, döngü başlamıyor.", flush=True)
        return
    while True:
        try:
            arge_botu.poll_arge_commands()
            arge_botu.maybe_run_research()
        except Exception as e:
            print(f"[Ar-Ge Entegrasyonu] Döngü hatası: {e}", flush=True)
        time.sleep(5)


if __name__ == "__main__":
    send_startup_message()
    threading.Thread(target=arka_plan_dongusu, daemon=True).start()
    threading.Thread(target=poll_telegram_commands, daemon=True).start()
    threading.Thread(target=kendi_kendine_ping, daemon=True).start()
    threading.Thread(target=arge_botu_dongusu, daemon=True).start()
    app.run(host="0.0.0.0", port=PORT)
