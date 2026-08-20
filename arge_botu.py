"""
arge_botu.py — AR-GE BOTU: SADECE GECE RADARI İÇİN, SADECE AI/MODEL ARAŞTIRMASI
====================================================================================
2026-08-15 SADELEŞTİRME: Önceki sürüm hem "kural" (basit eşik) hem "AI"
(model) kolu olmak üzere iki paralel araştırma yapıyordu — kullanıcı bunun
karmaşıklaştığını belirtti ve kapsamı netleştirdi: bu bot ARTIK SADECE
gece radarının AI modelini (overnight_model.pkl tarzı) geliştirmek için
araştırma yapıyor, kural kolu tamamen kaldırıldı.

KULLANICININ GERÇEK STRATEJİSİ (hedef tanımı buna göre kuruldu):
Kapanışa yakın (~17:40-17:50 İstanbul) pozisyon açılıyor, ertesi gün
piyasa açıldığında fiyat +%2 ve üzeri yükselince satılıyor. Bu botun
"başarı" tanımı BİREBİR BU KURALA göre - "ortalama getiri" gibi soyut
bir şey değil, "kaç sinyalden kaçı gerçekten +%2 hedefine ulaştı" (kazanma
oranı) tek karar kriteri.

MİMARİ: Gemini API'ye "şimdiye kadar denediklerimiz + sonuçları" gösterilip
küçük bir modelin HANGİ ÖZELLİKLERLE eğitileceği soruluyor. Gemini SERBEST
METİN/KOD ÜRETMİYOR — sadece ÖNCEDEN TANIMLI bir gösterge kütüphanesinden
(FEATURE_LIBRARY) 2-6 özellik seçiyor. Motor (bu dosya) bunlarla sabit,
önceden test edilmiş bir XGBoost modeli eğitip test ediyor. Bu bilinçli
bir güvenlik kararı: bir dil modelinin ürettiği kodu otomatik ÇALIŞTIRMAK
ciddi bir risk olurdu - model sadece yapılandırılmış bir "seçim" yapıyor.

TOPLU İSTEK (kota tasarrufu): Gemini'nin ücretsiz kotası günde ~20 istek.
Her turda 1 soru sormak yerine, TEK istekte BATCH_SIZE (15) hipotez birden
isteniyor, kuyruğa alınıp sırayla (kota harcamadan) test ediliyor.

GÜVENLİK KATMANI - EĞİTİM/DOĞRULAMA/HİÇ-GÖRÜLMEMİŞ SINAV (3 parça, 2 değil):
Veri KRONOLOJİK olarak 3'e bölünüyor - eğitim (%50) / doğrulama (%25) /
hiç görülmemiş sınav (%25, süreçte HİÇ kullanılmıyor). Bir hipotez SADECE
ÜÇÜNÜ DE geçerse "onaylı" sayılıyor. Onaylanan bir hipotez bile hemen
"kesin güvenilir" sayılmıyor - her RECONFIRM_INTERVAL_HOURS'ta bir GÜNCEL
veriyle tekrar test ediliyor, RECONFIRM_STREAK_REQUIRED (3) kez üst üste
geçmesi gerekiyor - bir kez başarısız olursa seri sıfırlanıyor.

Hiçbir hipotez otomatik olarak canlı sisteme (overnight_radar.py) bağlanmıyor
- bu bot SADECE ARAŞTIRMA yapar, hiçbir sinyal/emir üretmez, başka hiçbir
sisteme dokunmaz.
"""

import os
import csv
import json
import time
import threading

# SÜRÜM ETİKETİ - Render'da hangi kodun gerçekten çalıştığını Telegram
# mesajlarında görünür kılmak için (2026-08-17: 3 kez üst üste "aynı
# sonuç geldi" şüphesi sonrası eklendi - deploy'un gerçekten güncel
# olup olmadığını KANITLA göstermek için).
ARGE_KOD_SURUMU = "v24-wiki-izole-rr-dogrulama-2026-08-19"
import warnings
from datetime import datetime, timezone

import numpy as np
import pandas as pd
from scipy import stats as _stats
import requests

warnings.filterwarnings("ignore")

DATA_DIR = os.environ.get("DATA_DIR", ".")


def _data_path(filename: str) -> str:
    return os.path.join(DATA_DIR, filename)


# =============================================================================
# AYARLAR — kendi Telegram kimliği, ana bottan TAMAMEN AYRI
# =============================================================================

TELEGRAM_TOKEN = os.environ.get("ARGE_TELEGRAM_TOKEN", "")
TELEGRAM_CHAT_ID = os.environ.get("ARGE_TELEGRAM_CHAT_ID", "")
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY", "")
GEMINI_MODEL = os.environ.get("GEMINI_MODEL", "gemini-3.6-flash")

ARGE_BOTU_ENABLED = os.environ.get("ARGE_BOTU_ENABLED", "true").lower() == "true"
RESEARCH_COOLDOWN_MINUTES = int(os.environ.get("RESEARCH_COOLDOWN_MINUTES", "20"))
BATCH_SIZE = int(os.environ.get("ARGE_BATCH_SIZE", "15"))
QUEUE_FILE = _data_path("arge_kuyruk.json")

MIN_TRAIN_ROWS = int(os.environ.get("MIN_TRAIN_ROWS", "200"))
MIN_SAMPLE_PER_STAGE = int(os.environ.get("MIN_SAMPLE_PER_STAGE", "20"))

# Kullanıcının GERÇEK stratejisi: kapanışa yakın giriş, ertesi gün +%2
# hedefine ulaşınca satış. TARGET_PCT = o eşik. TRANSACTION_COST_PCT
# (komisyon+kayma) eklenerek EFFECTIVE_TARGET_PCT elde ediliyor - gerçek
# net kazanç için gereken gerçek eşik.
TARGET_PCT = float(os.environ.get("TARGET_PCT", "2.0"))
TRANSACTION_COST_PCT = float(os.environ.get("TRANSACTION_COST_PCT", "0.20"))
EFFECTIVE_TARGET_PCT = TARGET_PCT + TRANSACTION_COST_PCT

# TEK karar kriteri: kazanma oranı (10 sinyalden kaçı +%2 hedefine ulaştı).
MIN_WIN_RATE_PCT = float(os.environ.get("MIN_WIN_RATE_PCT", "70.0"))

# Onaylanan bir hipotez TEK SEFERLİK testten sonra "kesin güvenilir"
# sayılmaz - overnight_model_lab.py'deki ayni felsefe: RECONFIRM_STREAK_REQUIRED
# kez UST USTE (RECONFIRM_INTERVAL_HOURS arayla, her seferinde GÜNCEL/genislemis
# veriyle) ayni 3 asamayi da gecmesi gerekiyor. Bir kez basarisiz olursa seri
# sifirlaniyor - "bir kere sansli cikti" ile "gercekten tutarli" ayirt ediliyor.
RECONFIRM_STREAK_REQUIRED = int(os.environ.get("RECONFIRM_STREAK_REQUIRED", "3"))
RECONFIRM_INTERVAL_HOURS = int(os.environ.get("RECONFIRM_INTERVAL_HOURS", "24"))

HISTORY_FILE = _data_path("arge_hipotez_gecmisi.csv")
HISTORY_FIELDS = ["tarih", "isim", "yon", "ozellikler_json", "gerekce",
                   "egitim_n", "egitim_kazanma", "dogrulama_n", "dogrulama_kazanma",
                   "sinav_n", "sinav_kazanma", "onayli_mi", "asama"]

RECONFIRM_FILE = _data_path("arge_yeniden_dogrulama.csv")
RECONFIRM_FIELDS = ["isim", "yon", "ozellikler_json", "gerekce", "seri", "son_test_tarih",
                     "kesin_guvenilir_mi", "son_sinav_kazanma"]

CMD_OFFSET_FILE = _data_path("arge_cmd_offset.txt")

# Basit ama gercek bir BIST+US evreni - stock_screener_bot.py'yi IMPORT
# ETMİYORUZ (proje standardı - import yan etkisi riski), kendi sabit,
# kucuk listesi var.
BIST_TICKERS = [
    "THYAO.IS", "ASELS.IS", "SISE.IS", "KCHOL.IS", "GARAN.IS", "AKBNK.IS",
    "EREGL.IS", "BIMAS.IS", "TUPRS.IS", "SAHOL.IS", "PETKM.IS", "FROTO.IS",
    "TOASO.IS", "TCELL.IS", "YKBNK.IS", "ISCTR.IS", "PGSUS.IS", "TAVHL.IS",
    "VESTL.IS", "SASA.IS", "KOZAL.IS", "ENKAI.IS", "MGROS.IS", "ARCLK.IS",
    "AKSEN.IS", "TTKOM.IS", "ULKER.IS", "OYAKC.IS", "HALKB.IS", "VAKBN.IS",
]
US_TICKERS = [
    "AAPL", "MSFT", "GOOGL", "AMZN", "NVDA", "META", "TSLA", "AMD", "NFLX",
    "AVGO", "CRM", "ADBE", "COST", "PEP", "TMUS", "QCOM", "INTC", "CSCO",
    "JPM", "BAC", "XOM", "V", "MA", "DIS", "UBER",
]
ALL_TICKERS = BIST_TICKERS  # 2026-08-15: Ar-Ge botu artık SADECE gece radarı için çalışıyor - o BIST-only.
# 2026-08-18: US_TICKERS gece radarında hâlâ kullanılmıyor ama finra_kisa_pozisyon_testi
# (aşağıda) için yeniden devreye girdi - kod kaybolmasın diye tutulmuştu, iyi ki tutulmuş.

_last_run_time = None
_ARGE_AVAILABLE = bool(TELEGRAM_TOKEN and TELEGRAM_CHAT_ID)

if not _ARGE_AVAILABLE:
    print("[ARGE] ARGE_TELEGRAM_TOKEN/ARGE_TELEGRAM_CHAT_ID tanımlı değil - "
          "Ar-Ge botu devre dışı (ana sistemi etkilemez).", flush=True)


def send_telegram_message(text: str):
    """SADECE Ar-Ge botunun kendi sohbetine gider - ana botun TELEGRAM_TOKEN'ıyla
    HİÇBİR ilgisi yok, ana bot mesaj akışına asla karışmaz."""
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        print(f"[ARGE] Telegram ayarlı değil:\n{text}", flush=True)
        return
    try:
        requests.post(
            f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage",
            json={"chat_id": TELEGRAM_CHAT_ID, "text": text[:4000]}, timeout=20)
    except Exception as e:
        print(f"[ARGE] Telegram gönderilemedi: {e}", flush=True)


def send_telegram_document(filepath: str, caption: str = ""):
    """Bir dosyayı (CSV vb.) doğrudan Telegram belgesi olarak gönderir -
    Gemini'ye HİÇ gerek yok, Telegram Bot API'sinin kendi sendDocument
    uç noktası dosya yüklemeyi zaten destekliyor."""
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        print(f"[ARGE] Telegram ayarlı değil, dosya gönderilemedi: {filepath}", flush=True)
        return
    try:
        with open(filepath, "rb") as f:
            requests.post(
                f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendDocument",
                data={"chat_id": TELEGRAM_CHAT_ID, "caption": caption[:1000]},
                files={"document": f}, timeout=120)
    except Exception as e:
        print(f"[ARGE] Telegram dosya gönderilemedi: {e}", flush=True)
        send_telegram_message(f"⚠️ Dosya gönderilemedi: {e}")


def _read_history():
    if not os.path.exists(HISTORY_FILE):
        return []
    with open(HISTORY_FILE, newline="", encoding="utf-8") as f:
        return list(csv.DictReader(f))


def _append_history(row: dict):
    exists = os.path.exists(HISTORY_FILE)
    with open(HISTORY_FILE, "a", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=HISTORY_FIELDS)
        if not exists:
            w.writeheader()
        w.writerow(row)


def _read_reconfirm():
    if not os.path.exists(RECONFIRM_FILE):
        return []
    with open(RECONFIRM_FILE, newline="", encoding="utf-8") as f:
        return list(csv.DictReader(f))


def _write_reconfirm(rows):
    with open(RECONFIRM_FILE, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=RECONFIRM_FIELDS)
        w.writeheader()
        w.writerows(rows)


# =============================================================================
# GÖSTERGE KÜTÜPHANESİ
# =============================================================================

def _rsi(close, n=14):
    delta = close.diff()
    gain = delta.clip(lower=0)
    loss = -delta.clip(upper=0)
    ag = gain.ewm(alpha=1 / n, adjust=False).mean()
    al = loss.ewm(alpha=1 / n, adjust=False).mean()
    rs = ag / al.replace(0, np.nan)
    return 100 - (100 / (1 + rs))


def _macd_hist(close, fast=12, slow=26, signal=9):
    ema_f = close.ewm(span=fast, adjust=False).mean()
    ema_s = close.ewm(span=slow, adjust=False).mean()
    macd = ema_f - ema_s
    sig = macd.ewm(span=signal, adjust=False).mean()
    return macd - sig


def _bb_bandwidth(close, n=20, k=2):
    mid = close.rolling(n).mean()
    std = close.rolling(n).std()
    return ((mid + k * std) - (mid - k * std)) / mid.replace(0, np.nan) * 100


def _atr_pct(high, low, close, n=14):
    prev_close = close.shift(1)
    tr = pd.concat([high - low, (high - prev_close).abs(), (low - prev_close).abs()], axis=1).max(axis=1)
    atr = tr.ewm(alpha=1 / n, adjust=False).mean()
    return atr / close.replace(0, np.nan) * 100


def _cmf(high, low, close, volume, n=20):
    rng = (high - low).replace(0, np.nan)
    mfm = ((close - low) - (high - close)) / rng
    mfv = mfm * volume
    return mfv.rolling(n).sum() / volume.rolling(n).sum()


def _mfi(high, low, close, volume, n=14):
    tp = (high + low + close) / 3
    raw = tp * volume
    diff = tp.diff()
    pos = raw.where(diff > 0, 0.0).rolling(n).sum()
    neg = raw.where(diff < 0, 0.0).rolling(n).sum()
    ratio = pos / neg.replace(0, np.nan)
    return 100 - (100 / (1 + ratio))


def _stoch_k(high, low, close, n=14):
    lo = low.rolling(n).min()
    hi = high.rolling(n).max()
    return (close - lo) / (hi - lo).replace(0, np.nan) * 100


def _vwap_dist_pct(high, low, close, volume, n=20):
    """VWAP (Hacim Ağırlıklı Ortalama Fiyat) - günlük bar verisiyle GÜN İÇİ
    VWAP birebir hesaplanamaz (tick veri gerekir), bunun yerine N günlük
    kayan VWAP'a olan uzaklık hesaplanıyor - kullanıcının istediği "VWAP"ı
    kütüphaneye ekleyen, dürüstçe yaklaşık bir versiyon."""
    tipik = (high + low + close) / 3
    vwap = (tipik * volume).rolling(n).sum() / volume.rolling(n).sum().replace(0, np.nan)
    return (close - vwap) / vwap.replace(0, np.nan) * 100


# Kapanış-penceresi (proxy emir defteri) özellikleri — gerçek Level 2 emir
# defteri verisi (Matriks vb.) hem lisanslı/ücretli hem Python/Linux'tan
# programatik erişimi belirsiz - bunun yerine BEDAVA Yahoo 15dk verisinden,
# günün SON birkaç barına (~son 1 saat) bakarak "kapanışa yakın alıcı mı
# satıcı mı baskın" sorusuna KABA bir yaklaşım üretiliyor. GERÇEK emir
# defteri DEĞİL, onun ucuz bir proxy'si.
# ÖNEMLİ SINIR: Yahoo'nun 15dk verisi sadece ~60 gün geriye gidiyor - bu 4
# özellik SADECE son ~60 gün için dolu olacak, geri kalanında NaN kalacak.
CLOSING_WINDOW_BARS = int(os.environ.get("CLOSING_WINDOW_BARS", "4"))  # ~son 1 saat
CLOSING_PROXY_FEATURES = ["kapanis_hacim_orani", "kapanis_momentum_pct",
                           "kapanis_araligi_pct", "kapanis_yon_orani"]

BASE_FEATURE_LIBRARY = [
    "rsi14", "macd_hist", "bb_bandwidth", "atr_pct", "volume_factor", "cmf", "mfi",
    "stoch_k", "dist_sma20_pct", "dist_sma50_pct", "close_to_high_pct", "gap_pct",
    "pct_change", "day_of_week", "relative_strength", "vwap_dist_pct",
    "vol_zscore",
]
FEATURE_LIBRARY = BASE_FEATURE_LIBRARY + CLOSING_PROXY_FEATURES


def compute_features(df: pd.DataFrame, index_pct_change: pd.Series = None) -> pd.DataFrame:
    """Ham OHLCV DataFrame'ine (open/high/low/close/volume kolonlari, tarih
    index) tum BASE_FEATURE_LIBRARY kolonlarini ekler.
    HEDEF TANIMI - kullanicinin GERCEK stratejisiyle birebir: bugunun
    kapanisinda (~17:45) giris yapiliyor, ERTESI GUNUN EN YUKSEK fiyati
    (gun icinde herhangi bir an +%2 gorulmus mu) hedef. Bu TEK, BILINCLI
    lookahead istisnasi - egitimde/ozellik olarak KULLANILMIYOR, sadece
    etiket (label) olarak."""
    df = df.copy()
    df["prev_close"] = df["close"].shift(1)
    df["pct_change"] = (df["close"] - df["prev_close"]) / df["prev_close"] * 100
    df["gap_pct"] = (df["open"] - df["prev_close"]) / df["prev_close"] * 100
    df["rsi14"] = _rsi(df["close"])
    df["macd_hist"] = _macd_hist(df["close"])
    df["bb_bandwidth"] = _bb_bandwidth(df["close"])
    df["atr_pct"] = _atr_pct(df["high"], df["low"], df["close"])
    df["vol_ma20"] = df["volume"].rolling(20).mean()
    df["volume_factor"] = df["volume"] / df["vol_ma20"].replace(0, np.nan)
    # 2026-08-18: ABD swing turnuvasinda TEK basina karli cikan Hacim
    # Z-Skor'un (bkz. stock_screener_bot.py check_us_volume_zscore) BIST
    # hesap makinesine tasinan versiyonu - ayni formul (hacim, 20 gunluk
    # ortalama/std'ye gore ne kadar anormal).
    df["vol_std20"] = df["volume"].rolling(20).std()
    df["vol_zscore"] = (df["volume"] - df["vol_ma20"]) / df["vol_std20"].replace(0, np.nan)
    df["cmf"] = _cmf(df["high"], df["low"], df["close"], df["volume"])
    df["mfi"] = _mfi(df["high"], df["low"], df["close"], df["volume"])
    df["stoch_k"] = _stoch_k(df["high"], df["low"], df["close"])
    df["sma20"] = df["close"].rolling(20).mean()
    df["sma50"] = df["close"].rolling(50).mean()
    df["dist_sma20_pct"] = (df["close"] - df["sma20"]) / df["sma20"].replace(0, np.nan) * 100
    df["dist_sma50_pct"] = (df["close"] - df["sma50"]) / df["sma50"].replace(0, np.nan) * 100
    df["vwap_dist_pct"] = _vwap_dist_pct(df["high"], df["low"], df["close"], df["volume"])
    df["close_to_high_pct"] = (df["close"] - df["low"]) / (df["high"] - df["low"]).replace(0, np.nan) * 100
    df["day_of_week"] = df.index.dayofweek
    if index_pct_change is not None:
        df["relative_strength"] = df["pct_change"] - index_pct_change.reindex(df.index)
    else:
        df["relative_strength"] = np.nan
    # HEDEF: kapanista giris, ERTESI GUNUN EN YUKSEK fiyatina gore getiri -
    # kullanicinin "ertesi sabah +%2 gorunce sat" kuralinin dogrudan karsiligi.
    df["hedef_pct_change"] = (df["high"].shift(-1) - df["close"]) / df["close"] * 100
    return df


NEXT_DAY_WINDOW = ((10, 0), (12, 0))  # ertesi gunun ilk 2 saati, Istanbul - overnight_radar.py ile BIREBIR AYNI


def _compute_gece_hedef(ticker: str, df_daily: pd.DataFrame) -> pd.Series:
    """GECE RADARI hedefi (2026-08-15, kullanıcının net tanımı): giriş =
    günün kendi kapanışı (~17:40-17:50 BIST yaklaşımı), başarı = ERTESİ
    GÜNÜN 10:00-12:00 İstanbul penceresindeki EN YÜKSEK fiyatın entry'ye
    göre %değişimi - overnight_radar.py'nin CANLIDA kullandığı tanımla
    BİREBİR AYNI, artık "ertesi gün kapanış-kapanış" gibi genel bir
    yaklaşık değil.
    Yahoo'nun 15dk verisi sadece ~60 gün geriye gittiği için (proje
    boyunca defalarca karşılaşılan kısıt) HİBRİT yöntem kullanılıyor -
    overnight_backtest.py'de zaten kanıtlanmış, AYNI mantık:
      - son ~60 gün: 15dk-HASSAS (gerçek pencere)
      - daha eski günler: GÜNLÜK-YAKLAŞIK (ertesi günün TÜM günlük en
        yükseği - daha az hassas ama 2 yıllık kapsamı korur)
    5 günden fazla fark varsa (overnight_backtest.py'deki AYNI güvenlik
    kontrolü) 15dk sonucu güvenilmez sayılıp yaklaşığa düşülür - eski bir
    tarihin yanlışlıkla çok ileri bir "ertesi gün" ile eşleşmesini önler."""
    import yfinance as yf

    entry = df_daily["close"]
    hedef = ((df_daily["high"].shift(-1) - entry) / entry * 100)  # gunluk-yaklasik (varsayilan)

    try:
        df15 = yf.Ticker(ticker).history(period="60d", interval="15m")
        if df15 is not None and not df15.empty:
            df15 = df15.rename(columns={"High": "high"})
            idx15 = pd.to_datetime(df15.index)
            idx15 = idx15.tz_convert("Europe/Istanbul") if idx15.tz is not None else idx15.tz_localize("Europe/Istanbul")
            df15.index = idx15
            df15["gun"] = df15.index.normalize().tz_localize(None)
            dakika = df15.index.hour * 60 + df15.index.minute
            baslangic = NEXT_DAY_WINDOW[0][0] * 60 + NEXT_DAY_WINDOW[0][1]
            bitis = NEXT_DAY_WINDOW[1][0] * 60 + NEXT_DAY_WINDOW[1][1]
            pencere15 = df15[(dakika >= baslangic) & (dakika < bitis)]
            gunluk_max = pencere15.groupby("gun")["high"].max()

            for tarih in df_daily.index:
                sonraki = gunluk_max.index[gunluk_max.index > tarih]
                if len(sonraki) == 0:
                    continue
                hedef_gun = sonraki[0]
                if (hedef_gun - tarih).days > 5:  # overnight_backtest.py'deki AYNI guvenlik kontrolu
                    continue
                entry_fiyat = entry.loc[tarih]
                if entry_fiyat:
                    hedef.loc[tarih] = (gunluk_max.loc[hedef_gun] - entry_fiyat) / entry_fiyat * 100
    except Exception as e:
        print(f"[ARGE] {ticker} 15dk-hassas hedef hatası (yaklaşığa düşüldü): {e}", flush=True)

    return hedef


def fetch_all_data():
    import yfinance as yf
    print("[ARGE] Veri çekiliyor (SADECE BIST — gece radarı hedefi: "
          "ertesi gün 10:00-12:00 arası +%2'ye ulaşıyor mu?)...", flush=True)
    index_df = yf.Ticker("XU100.IS").history(period="2y", interval="1d")
    index_pct = None
    if index_df is not None and not index_df.empty:
        index_df.index = pd.to_datetime(index_df.index).tz_localize(None)
        index_pct = (index_df["Close"] - index_df["Close"].shift(1)) / index_df["Close"].shift(1) * 100

    parcalar = []
    for ticker in ALL_TICKERS:
        try:
            df = yf.Ticker(ticker).history(period="2y", interval="1d")
            if df is None or df.empty or len(df) < 100:
                continue
            df = df.rename(columns={"Open": "open", "High": "high", "Low": "low",
                                     "Close": "close", "Volume": "volume"})
            df = df[["open", "high", "low", "close", "volume"]].copy()
            df.index = pd.to_datetime(df.index).tz_localize(None)
            df = compute_features(df, index_pct)
            df["hedef_pct_change"] = _compute_gece_hedef(ticker, df)  # GECE RADARI hedefi, genel degil
            df["ticker"] = ticker
            parcalar.append(df)
        except Exception as e:
            print(f"[ARGE] {ticker} veri hatası: {e}", flush=True)
        time.sleep(0.2)

    if not parcalar:
        return pd.DataFrame()
    tum = pd.concat(parcalar)
    # ONEMLI: sadece TEMEL (her zaman hesaplanan) ozellikler icin dropna -
    # kapanis-penceresi proxy ozellikleri henuz burada yok, sadece bir
    # hipotez onlari GERCEKTEN kullanirsa augment_with_closing_features()
    # ile SONRADAN ekleniyor.
    tum = tum.dropna(subset=BASE_FEATURE_LIBRARY + ["hedef_pct_change"])
    return tum


def _needs_closing_features(features_used) -> bool:
    return any(f in CLOSING_PROXY_FEATURES for f in features_used)


def _compute_closing_window_per_day(df15: pd.DataFrame, bars: int = CLOSING_WINDOW_BARS) -> pd.DataFrame:
    """15dk bar verisinden HER GUN icin, o gunun SON `bars` barina (~son 1
    saat, kapanisa yakin) bakip 4 proxy ozellik uretir - GERCEK emir
    defteri DEGIL, bedava veriyle onun kaba bir yansimasi."""
    df = df15.copy()
    idx = pd.to_datetime(df.index)
    if idx.tz is not None:
        idx = idx.tz_localize(None)
    df.index = idx
    df["gun"] = df.index.normalize()

    satirlar = []
    for gun, grup in df.groupby("gun"):
        grup = grup.sort_index()
        if len(grup) < bars:
            continue
        son = grup.iloc[-bars:]
        gun_ort_hacim = grup["volume"].mean()
        hacim_orani = float(son["volume"].mean() / gun_ort_hacim) if gun_ort_hacim else np.nan
        ilk_fiyat = float(son.iloc[0]["open"])
        son_fiyat = float(son.iloc[-1]["close"])
        momentum = (son_fiyat - ilk_fiyat) / ilk_fiyat * 100 if ilk_fiyat else np.nan
        yuksek, dusuk = float(son["high"].max()), float(son["low"].min())
        aralik = (yuksek - dusuk) / son_fiyat * 100 if son_fiyat else np.nan
        yon_orani = float((son["close"] > son["open"]).mean() * 100)
        satirlar.append({
            "gun": gun, "kapanis_hacim_orani": hacim_orani,
            "kapanis_momentum_pct": momentum, "kapanis_araligi_pct": aralik,
            "kapanis_yon_orani": yon_orani,
        })
    if not satirlar:
        return pd.DataFrame()
    return pd.DataFrame(satirlar).set_index("gun")


def augment_with_closing_features(df: pd.DataFrame) -> pd.DataFrame:
    """SADECE bir hipotez CLOSING_PROXY_FEATURES'tan birini kullaniyorsa
    cagrilir - her turda gereksiz yere agir 15dk fetch'i yapmasin diye
    sarta bagli."""
    import yfinance as yf
    print("[ARGE] Kapanış-penceresi (proxy emir defteri) özellikleri çekiliyor "
          "(~60 gün, 15dk, gerçek emir defteri DEĞİL)...", flush=True)
    for c in CLOSING_PROXY_FEATURES:
        if c not in df.columns:
            df[c] = np.nan

    for ticker in df["ticker"].unique():
        try:
            df15 = yf.Ticker(ticker).history(period="60d", interval="15m")
            if df15 is None or df15.empty:
                continue
            df15 = df15.rename(columns={"Open": "open", "High": "high",
                                         "Low": "low", "Close": "close", "Volume": "volume"})
            gunluk = _compute_closing_window_per_day(df15)
            if gunluk.empty:
                continue
            mask = df["ticker"] == ticker
            tarihler = df.loc[mask].index.normalize()
            for c in CLOSING_PROXY_FEATURES:
                df.loc[mask, c] = gunluk[c].reindex(tarihler).values
        except Exception as e:
            print(f"[ARGE] {ticker} kapanış-penceresi verisi hatası: {e}", flush=True)
        time.sleep(0.2)
    return df


def chronological_split(df: pd.DataFrame):
    """KRONOLOJIK 3'e bolme (egitim %50 / dogrulama %25 / sinav %25) -
    rastgele degil, tarihe gore. Sinav dilimi SURECIN HICBIR ASAMASINDA
    kullanilmiyor, sadece son onay icin."""
    df = df.sort_index()
    tarihler = df.index.unique().sort_values()
    n = len(tarihler)
    egitim_son = tarihler[int(n * 0.50)]
    dogrulama_son = tarihler[int(n * 0.75)]
    egitim = df[df.index <= egitim_son]
    dogrulama = df[(df.index > egitim_son) & (df.index <= dogrulama_son)]
    sinav = df[df.index > dogrulama_son]
    return egitim, dogrulama, sinav


# =============================================================================
# HİPOTEZ JSON — GÜVENLİ AYRIŞTIRMA (kod çalıştırma YOK, sadece özellik seçimi)
# =============================================================================

def validate_ai_hypothesis(h: dict) -> tuple:
    """Gemini SADECE FEATURE_LIBRARY'den 2-6 ozellik SECIYOR - hicbir
    kod/hiperparametre/model turu belirlemiyor, motor sabit, onceden test
    edilmis bir XGBoost yapilandirmasi kullanir."""
    if not isinstance(h, dict):
        return False, "JSON bir sözlük değil"
    for alan in ("isim", "yon", "kullanilacak_ozellikler", "gerekce"):
        if alan not in h:
            return False, f"'{alan}' eksik"
    if h["yon"] not in ("LONG", "SHORT"):
        return False, "yon LONG veya SHORT olmalı"
    ozellikler = h["kullanilacak_ozellikler"]
    if not isinstance(ozellikler, list) or not (2 <= len(ozellikler) <= 6):
        return False, "kullanilacak_ozellikler 2-6 elemanlı bir liste olmalı"
    for o in ozellikler:
        if o not in FEATURE_LIBRARY:
            return False, f"bilinmeyen özellik: {o}"
    if len(set(ozellikler)) != len(ozellikler):
        return False, "özellik listesinde tekrar var"
    return True, ""


def _compute_stats(hedef: pd.Series) -> dict:
    """Bir dizi 'ertesi gun en yuksek getiri' (%) uzerinden istatistik
    uretir. kazanma_orani ARTIK "kac tanesi EFFECTIVE_TARGET_PCT'e ulasti"
    demek - kullanicinin gercek "+%2 gorunce sat" kuralinin BIREBIR
    karsiligi, soyut bir "pozitif mi" degil."""
    n = len(hedef)
    if n < MIN_SAMPLE_PER_STAGE:
        return None
    ort = float(hedef.mean())
    kazanma_orani = float((hedef >= EFFECTIVE_TARGET_PCT).mean() * 100)
    en_kotu = float(hedef.min())
    return {"n": n, "ort": round(ort, 4), "kazanma_orani": round(kazanma_orani, 2),
            "en_kotu": round(en_kotu, 3)}


def _stage_passed(s: dict) -> bool:
    """TEK karar kriteri: kazanma orani >= MIN_WIN_RATE_PCT (varsayilan
    %70) - kullanicinin "10 sinyalden 7-8'i dogru olmali" istegiyle
    birebir. Eskiden ayrica bir 'ortalama getiri' esigi de vardi, kafa
    karistirdigi icin kaldirildi - kazanma orani zaten EFFECTIVE_TARGET_PCT'e
    ulasmayi olcuyor, ayri bir ortalama sarti gereksiz karmasiklikti."""
    if s is None:
        return False
    return s["kazanma_orani"] >= MIN_WIN_RATE_PCT


def evaluate_ai_on_slice(model, df_slice: pd.DataFrame, features: list, yon: str, threshold: float = 0.5):
    """Bir veri diliminde AI (model tabanli) hipotezi calistirir - modelin
    pozitif tahmin ettigi (proba>=threshold) satirlarin GERCEK sonuclarina
    bakar."""
    alt = df_slice.dropna(subset=features + ["hedef_pct_change"])
    if len(alt) < MIN_SAMPLE_PER_STAGE:
        return None
    proba = model.predict_proba(alt[features])[:, 1]
    secilen = alt[proba >= threshold]
    hedef = secilen["hedef_pct_change"]
    if yon == "SHORT":
        hedef = -hedef
    return _compute_stats(hedef)


def train_ai_model(df_egitim: pd.DataFrame, features: list):
    """train_model.py/overnight_model_lab.py ile AYNI, sabit, onceden test
    edilmis XGBoost yapilandirmasi - Gemini burada hicbir parametreye karar
    vermiyor, sadece hangi ozellikleri kullanacagini secmisti."""
    from xgboost import XGBClassifier
    egitim = df_egitim.dropna(subset=features + ["hedef_pct_change"])
    y = (egitim["hedef_pct_change"] >= EFFECTIVE_TARGET_PCT).astype(int)
    if y.nunique() < 2:
        return None
    model = XGBClassifier(n_estimators=150, max_depth=4, learning_rate=0.05,
                           eval_metric="logloss", random_state=42)
    model.fit(egitim[features], y)
    return model


# =============================================================================
# GEMİNİ'DEN TOPLU HİPOTEZ İSTEME
# =============================================================================

def _call_gemini(prompt: str):
    """2026-08-15: Ham REST istegi (query VEYA header) Google'in yeni "AQ."
    formatlı anahtarlarıyla hiç çalışmadı (bilinen, çözülmemiş Google
    sorunu). Resmi google-genai SDK'sı kimlik doğrulamayı kendi içinde
    farklı ele alıp çalıştı - requirements.txt'de google-genai şart."""
    if not GEMINI_API_KEY:
        print("[ARGE] GEMINI_API_KEY yok, hipotez istenemiyor.", flush=True)
        return None
    try:
        from google import genai
        client = genai.Client(api_key=GEMINI_API_KEY)
        resp = client.models.generate_content(model=GEMINI_MODEL, contents=prompt)
        metin = resp.text.strip().removeprefix("```json").removeprefix("```").removesuffix("```").strip()
        return json.loads(metin)
    except Exception as e:
        print(f"[ARGE] Gemini isteği/ayrıştırma hatası: {e}", flush=True)
        return None


# =============================================================================
# HESAP MAKİNESİ v2 — Gemini'nin KENDİSİ analist gibi karar veriyor
# =============================================================================
# 2026-08-16 YENİDEN TASARIM: Kullanıcı önceki sürümü ("Gemini sadece hangi
# özellik kullanılsın diye seçer, motor XGBoost eğitir") yanlış anlaşılma
# olarak işaretledi - istediği İSTATİSTİKSEL BİR MODEL DEĞİL, Gemini'nin
# TÜM gösterge değerlerine bakıp KENDİ akıl yürütmesiyle LONG/SHORT kararı
# vermesi. Yani "geçmiş veriden öğrenen bir model" yerine "her seferinde
# ham sayılara bakıp yorumlayan bir analist" mantığı. Bunu DÜRÜSTÇE
# belirtmek gerekir: bu, geçmişten "öğrenmiyor" - Gemini'nin genel teknik
# analiz bilgisini (RSI>70 aşırı alım gibi standart kurallar) her seferinde
# yeniden uyguluyor. Gerçekten işe yarayıp yaramadığını SADECE backtest
# gösterir - bu yüzden kullanıcının istediği tarih-bazlı test kuruldu.

def _gostergeleri_hesapla(df: pd.DataFrame, tarih) -> dict:
    """df'nin (compute_features çıkışı) belirli bir TARİHİNDEKİ (o tarihe
    kadar hesaplanmış, İLERİ SIZINTI YOK) tüm gösterge değerlerini sözlük
    olarak döner - Gemini'ye gönderilecek ham veri budur."""
    if tarih not in df.index:
        return None
    satir = df.loc[tarih]
    return {f: (round(float(satir[f]), 4) if pd.notna(satir[f]) else None) for f in BASE_FEATURE_LIBRARY}


# =============================================================================
# KOD-TABANLI HESAPLAMA (Gemini'siz) — 2026-08-16, kullanıcının düzeltmesiyle
# =============================================================================
# İLK SÜRÜM YANLIŞTI: her göstergeye "eşiği geçti mi geçmedi mi" diye BİNER
# bir oy (+1/-1/0) veriyordu ve eşiği geçemeyen hisseleri NÖTR diye ATLIYORDU
# - kullanıcı bunun "hesaplama değil filtreleme" olduğunu haklı olarak
# belirtti. ŞİMDİ: her yönlü göstergenin HAM DEĞERİ (0 etrafında
# merkezlenmiş/ölçeklenmiş) doğrudan toplama katılıyor - eşik/filtre YOK,
# HİÇBİR HİSSE ATLANMIYOR. Toplamın işareti (pozitif/negatif) yönü
# belirliyor - her hisse için MUTLAKA bir LONG ya da SHORT üretiliyor.


def hesapla_yon_kod_ile(gostergeler: dict) -> dict:
    """Gemini'ye HİÇ İHTİYAÇ DUYMADAN, ham gösterge değerlerinin AĞIRLIKLI
    TOPLAMINI hesaplar - filtre/eşik YOK, her hisse için mutlaka bir yön
    (LONG ya da SHORT) üretilir. Her göstergenin katkısı ayrı ayrı
    dönüyor ki şeffaf olsun - 'kara kutu' değil, hangi göstergenin ne
    kadar katkı yaptığı görülebiliyor.
    2026-08-17 ÜÇÜNCÜ DÜZELTME (4 gerçek tarihte test edildi - 66/116 = %56.9
    genel doğruluk ama 3 tarihte hisselerin %83-90'ı SHORT, 1 tarihte
    %96.6'sı LONG çıktı - sistem HİSSEYE ÖZEL değil, "o günkü genel piyasa
    havasını 29 hisseye kopyalıyor"du. Sebep: SMA20/SMA50/VWAP uzaklığı,
    momentum gibi göstergeler büyük ölçüde GENEL PİYASA TRENDİYLE
    KORELE - bir hisseyi değil, o günkü piyasayı ölçüyorlar. Kullanıcının
    isteğiyle: relative_strength (hissenin ENDEKSE GÖRE, yani piyasa
    trendinden ARINDIRILMIŞ ayrışması - tanım gereği hisseye özel) ağırlığı
    ARTIRILDI, piyasa-trendiyle-korele göstergelerin (SMA/VWAP uzaklığı)
    ağırlığı AZALTILDI - sistem artık 'piyasa ne yapıyor' yerine 'bu hisse
    piyasadan ne kadar farklı davranıyor' sorusuna daha çok odaklanıyor."""
    KATKI_SINIRI = 3.0

    def g(ad):
        v = gostergeler.get(ad)
        return float(v) if v is not None else 0.0

    def sinirla(deger, sinir=KATKI_SINIRI):
        return max(-sinir, min(sinir, deger))

    katkilar = {
        "RSI (50 merkezli, trend-takip)": sinirla((g("rsi14") - 50) / 10),
        "MACD histogram": sinirla(g("macd_hist") * 2),
        "CMF": sinirla(g("cmf") * 10),
        "MFI (50 merkezli, trend-takip)": sinirla((g("mfi") - 50) / 10),
        "Stochastic %K (50 merkezli, trend-takip)": sinirla((g("stoch_k") - 50) / 10),
        # Piyasa-genel-trendle KORELE gostergeler - agirlik AZALTILDI (x0.5)
        "SMA20'ye uzaklık (azaltılmış ağırlık)": sinirla(g("dist_sma20_pct") * 0.5),
        "SMA50'ye uzaklık (azaltılmış ağırlık)": sinirla(g("dist_sma50_pct") * 0.5),
        "VWAP'a uzaklık (azaltılmış ağırlık)": sinirla(g("vwap_dist_pct") * 0.5),
        "Kapanış-zirve konumu (50 merkezli)": sinirla((g("close_to_high_pct") - 50) / 10),
        # HİSSEYE ÖZEL (piyasa trendinden arındırılmış) - agirlik ARTIRILDI (x2)
        "Endekse göre relative strength (artırılmış ağırlık)": sinirla(g("relative_strength") * 2, sinir=KATKI_SINIRI * 1.5),
        "Gap": sinirla(g("gap_pct")),
        "Günlük değişim (momentum, azaltılmış ağırlık)": sinirla(g("pct_change") * 0.5),
        # 2026-08-18: ABD swing turnuvasinda TEK basina karli cikan Hacim
        # Z-Skor stratejisinin mantigi (bkz. check_us_volume_zscore) - anormal
        # yuksek hacimli GUNUN KENDI YONUNUN TERSINE bir tukenme/donus sinyali.
        # Esik/filtre YOK (KATKI_SINIRI mantigina uydu): sadece pozitif
        # z-skor (ortalamanin USTUNDE hacim) katkiya giriyor, negatif z-skor
        # (dusuk hacim) noturdur - "hacim az" bir yon iddiasi degildir.
        "Hacim anomalisi (tükenme/tersine dönüş)": sinirla(
            -np.sign(g("pct_change")) * max(0.0, g("vol_zscore")) * 0.75),
    }
    # volume_factor yon vermez (buyukluk gostergesi), sadece MEVCUT
    # toplamin buyuklugunu carpanla guclendirir - hesaplamaya KATILIYOR,
    # ayri bir esik/filtre degil.
    ham_toplam = sum(katkilar.values())
    hacim_carpani = 1.0 + max(0.0, (g("volume_factor") - 1.0)) * 0.15
    katkilar["Hacim çarpanı etkisi"] = round(ham_toplam * hacim_carpani - ham_toplam, 3)

    skor = round(sum(katkilar.values()), 3)
    yon = "LONG" if skor >= 0 else "SHORT"

    # 2026-08-18: SECICILIK ekleniyor ama FILTRE OLARAK DEGIL - hicbir hisse
    # atlanmiyor, HER hisseye yine mutlaka bir yon uretiliyor (kullanicinin
    # "hesaplama, filtreleme degil" kuralina sadik kalindi). Bunun yerine
    # "kac gosterge skorun isaretiyle AYNI yonde" sayisi AYRI bir kolon
    # olarak donuyor - test asamasinda "yuksek uyumlu kararlar daha mi
    # isabetli" diye ANALIZ EDILEBILSIN diye. Sinyali uretmeyi degistirmiyor,
    # sadece test/analiz icin ek bilgi tasiyor.
    yonlu_katkilar = {k: v for k, v in katkilar.items() if k != "Hacim çarpanı etkisi"}
    uyumlu_sayisi = sum(1 for v in yonlu_katkilar.values()
                         if (v > 0 and skor >= 0) or (v < 0 and skor < 0))
    toplam_yonlu = sum(1 for v in yonlu_katkilar.values() if v != 0)

    return {"yon": yon, "skor": skor, "detaylar": {k: round(v, 3) for k, v in katkilar.items()},
            "uyumlu_sayisi": uyumlu_sayisi, "toplam_yonlu_gosterge": toplam_yonlu}


def ask_gemini_for_verdicts_batch(ticker_gostergeleri: dict) -> dict:
    """TEK istekte BİRDEN FAZLA hisse için LONG/SHORT kararı ister - kota
    tasarrufu için (hipotez toplu-isteğiyle AYNI mantık). Dönen: {ticker:
    {"yon":..., "guven":..., "gerekce":...}} sözlüğü, ya da gecersiz/hatali
    olanlar icin o ticker hic yoktur (sessizce atlanir)."""
    prompt = f"""Sen deneyimli bir teknik analiz uzmanısın. Aşağıdaki BIST
hisseleri için, kapanışa yakın (~17:40-17:50) alınan gösterge değerlerine
bakarak her biri için ERTESİ GÜN piyasa açıldığında fiyatın YUKARI (LONG)
mı AŞAĞI (SHORT) mı gideceğine dair kararını ver:

{json.dumps(ticker_gostergeleri, ensure_ascii=False, indent=2)}

SADECE şu JSON formatında (hisse kodu -> karar) cevap ver, başka hiçbir
metin ekleme:
{{"HİSSE_KODU": {{"yon": "LONG veya SHORT", "guven": 0-100 arası sayı, "gerekce": "kısa açıklama"}}, ...}}"""

    cevap = _call_gemini(prompt)
    if not isinstance(cevap, dict):
        print("[ARGE] Gemini'den beklenen JSON sözlüğü gelmedi.", flush=True)
        return {}

    gecerliler = {}
    for ticker, karar in cevap.items():
        if (isinstance(karar, dict) and karar.get("yon") in ("LONG", "SHORT")
                and isinstance(karar.get("guven"), (int, float))):
            gecerliler[ticker] = karar
        else:
            print(f"[ARGE] {ticker} için geçersiz karar formatı, atlandı.", flush=True)
    print(f"[ARGE] Toplu karar isteği: {len(cevap)} geldi, {len(gecerliler)} geçerli.", flush=True)
    return gecerliler


HESAP_TEST_HISTORY_FILE = _data_path("arge_hesap_test_gecmisi.csv")
HESAP_TEST_FIELDS = ["test_tarihi", "ticker", "yon", "guven", "gerekce",
                      "ertesi_gun_getiri_pct", "dogru_mu"]


def hesap_makinesi_debug(ticker: str, tarih_str: str) -> str:
    """TEŞHİS ARACI - bir hissenin bir tarihteki TÜM gösterge değerlerini
    VE hesapla_yon_kod_ile'nin her faktöre verdiği KATKIYI tek tek
    gösterir. 9 Temmuz testinde piyasa %75.9 pozitifken sistemin %86.2
    SHORT demesi RSI/MFI/Stochastic düzeltmesiyle açıklanamadı - bu
    aracın amacı gerçek sebebi bulmak."""
    import yfinance as yf
    if not ticker.upper().endswith(".IS"):
        ticker = ticker.upper() + ".IS"
    hedef_tarih = pd.Timestamp(tarih_str)

    try:
        index_df = yf.Ticker("XU100.IS").history(period="2y", interval="1d")
        index_pct = None
        if index_df is not None and not index_df.empty:
            index_df.index = pd.to_datetime(index_df.index).tz_localize(None)
            index_pct = (index_df["Close"] - index_df["Close"].shift(1)) / index_df["Close"].shift(1) * 100

        df = yf.Ticker(ticker).history(period="2y", interval="1d")
        if df is None or df.empty:
            return f"🔬 {ticker}: veri yok."
        df = df.rename(columns={"Open": "open", "High": "high", "Low": "low",
                                 "Close": "close", "Volume": "volume"})
        df = df[["open", "high", "low", "close", "volume"]].copy()
        df.index = pd.to_datetime(df.index).tz_localize(None)
        df = compute_features(df, index_pct)
    except Exception as e:
        return f"🔬 {ticker}: veri hatası — {e}"

    gostergeler = _gostergeleri_hesapla(df, hedef_tarih)
    if gostergeler is None:
        return f"🔬 {ticker}: {hedef_tarih.date()} tarihi verisi yok."

    sonuc = hesapla_yon_kod_ile(gostergeler)
    lines = [f"🔬 [TEŞHİS] {ticker} — {hedef_tarih.date()}", "", "Ham gösterge değerleri:"]
    for k, v in gostergeler.items():
        lines.append(f"  {k}: {v}")
    lines.append("")
    lines.append("Her faktörün skora katkısı:")
    toplam = 0
    for k, v in sonuc["detaylar"].items():
        toplam += v
        lines.append(f"  {k}: {v:+.3f}")
    lines.append(f"\nTOPLAM SKOR: {sonuc['skor']:+.3f} → {sonuc['yon']}")
    lines.append(f"\n🔖 Kod sürümü: {ARGE_KOD_SURUMU}")
    return "\n".join(lines)


def hesap_makinesi_tam_yil_testi(baslangic_str: str = "2026-01-01", bitis_str: str = None) -> tuple:
    """Kullanıcının isteği: tek tek tarih yazmak yerine, baştan (2026-01-01)
    BUGÜNE kadar HER işlem gününü otomatik test edip TEK BİR DOSYA (CSV)
    halinde sonuç üretir. Verimlilik için: her hisse SADECE 1 KEZ çekilir,
    sonra o hissenin TÜM günleri aynı veri üzerinde döngüyle test edilir -
    /hesap_test'in her tarih için ayrı ayrı çekmesinden ÇOK daha hızlı.
    Hafta sonu/resmi tatil AYRIMI GEREKMİYOR - hangi günlerin gerçek işlem
    günü olduğu XU100 endeksinin KENDİ VERİSİNDEN (index_df.index) alınıyor,
    bu doğal olarak sadece gerçek işlem günlerini içeriyor.
    BAYAT VERİ KORUMASI: /hesap_test'teki AYNI mantık - giriş=çıkış olan
    (Yahoo'nun bayat veri döndürdüğü) satırlar sessizce ATLANIYOR, sahte
    '%0 getiri' olarak sayılmıyor.
    Döner: (dosya_yolu, özet_dict) ya da (None, hata_mesajı)."""
    import yfinance as yf
    bitis_str = bitis_str or datetime.now(timezone.utc).date().isoformat()
    baslangic, bitis = pd.Timestamp(baslangic_str), pd.Timestamp(bitis_str)

    index_df = yf.Ticker("XU100.IS").history(period="2y", interval="1d")
    if index_df is None or index_df.empty:
        return None, "XU100 endeks verisi çekilemedi."
    index_df.index = pd.to_datetime(index_df.index).tz_localize(None)
    index_pct = (index_df["Close"] - index_df["Close"].shift(1)) / index_df["Close"].shift(1) * 100
    islem_gunleri = index_df.index[(index_df.index >= baslangic) & (index_df.index <= bitis)]
    if len(islem_gunleri) == 0:
        return None, f"{baslangic_str} - {bitis_str} arasında işlem günü bulunamadı."

    print(f"[ARGE] Tam yıl testi başlıyor: {len(islem_gunleri)} işlem günü × "
          f"{len(BIST_TICKERS)} hisse", flush=True)

    tum_satirlar = []
    for ticker in BIST_TICKERS:
        try:
            df = yf.Ticker(ticker).history(period="2y", interval="1d")
            if df is None or df.empty or len(df) < 100:
                continue
            df = df.rename(columns={"Open": "open", "High": "high", "Low": "low",
                                     "Close": "close", "Volume": "volume"})
            df = df[["open", "high", "low", "close", "volume"]].copy()
            df.index = pd.to_datetime(df.index).tz_localize(None)
            df = compute_features(df, index_pct)

            for tarih in islem_gunleri:
                if tarih not in df.index:
                    continue
                gostergeler = _gostergeleri_hesapla(df, tarih)
                if gostergeler is None:
                    continue
                idx = df.index.get_loc(tarih)
                if idx + 1 >= len(df):
                    continue
                entry = df.iloc[idx]["close"]
                cikis = df.iloc[idx + 1]["close"]
                if entry == 0:
                    continue
                getiri = (cikis - entry) / entry * 100
                if abs(getiri) < 0.001:
                    continue  # BAYAT VERI KORUMASI - Yahoo'nun tekrar eden verisi
                sonuc = hesapla_yon_kod_ile(gostergeler)
                dogru = (sonuc["yon"] == "LONG" and getiri > 0) or (sonuc["yon"] == "SHORT" and getiri < 0)
                tum_satirlar.append({
                    "tarih": tarih.date().isoformat(), "ticker": ticker, "yon": sonuc["yon"],
                    "skor": sonuc["skor"], "ertesi_gun_getiri_pct": round(getiri, 3),
                    "dogru_mu": 1 if dogru else 0,
                    "uyumlu_sayisi": sonuc["uyumlu_sayisi"],
                    "toplam_yonlu_gosterge": sonuc["toplam_yonlu_gosterge"],
                })
        except Exception as e:
            print(f"[ARGE] {ticker} tam yıl testi hatası: {e}", flush=True)
        time.sleep(0.2)

    if not tum_satirlar:
        return None, "Hiçbir sonuç üretilemedi."

    dosya_yolu = _data_path("tam_yil_hesap_makinesi_testi.csv")
    with open(dosya_yolu, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.DictWriter(f, fieldnames=["tarih", "ticker", "yon", "skor",
                                           "ertesi_gun_getiri_pct", "dogru_mu",
                                           "uyumlu_sayisi", "toplam_yonlu_gosterge"])
        w.writeheader()
        w.writerows(tum_satirlar)

    n = len(tum_satirlar)
    dogru_n = sum(r["dogru_mu"] for r in tum_satirlar)
    uzun_n = sum(1 for r in tum_satirlar if r["yon"] == "LONG")
    ozet = {
        "n": n, "dogru_n": dogru_n, "dogruluk_pct": round(dogru_n / n * 100, 1),
        "uzun_n": uzun_n, "kisa_n": n - uzun_n, "gun_sayisi": len(islem_gunleri),
        "hisse_sayisi": len(BIST_TICKERS),
    }
    return dosya_yolu, ozet


# =============================================================================
# GÖSTERGE TURNUVASI — ABD swing turnuvasındaki gibi HER ÖZELLİĞİ TEK
# BAŞINA (izole) test eder — 2026-08-18
# =============================================================================
# GEREKÇE: /hesap_tam_test iki turda da (v7, v8) gösterdi ki 13 göstergenin
# ağırlıklı toplamı ne genel yönde ne uyum-sayısı bazında bir kenar
# üretiyor. ABD tarafındaki KANITLANMIŞ kenarların (ATR kırılımı %69.6,
# Hacim Z-Skor %63.0, RSI21 %69.9) hepsi TEK BAŞINA, gerçek eşikle test
# edilmiş stratejiler — 13 göstergeyi harmanlayan bir toplam değil. Bu
# turnuva, BIST'te de HERHANGİ BİR göstergenin izole bir kenarı var mı,
# varsa hangisi, önce onu bulmak için.

GOSTERGE_TURNUVASI_ESIK_YUZDE = 0.20  # üst/alt %20 - genel, sabit kural
GOSTERGE_TURNUVASI_MIN_N = 30


def _yonlu_ozellikler_listesi() -> list:
    """BASE_FEATURE_LIBRARY'den, TEK BAŞINA yön iddiası taşımayan
    (büyüklük/kategori göstergesi olan) özellikleri çıkarır."""
    haric = {"day_of_week", "atr_pct", "volume_factor", "bb_bandwidth", "vol_zscore"}
    return [f for f in BASE_FEATURE_LIBRARY if f not in haric]


def _feature_strateji_matrisi(df_all: pd.DataFrame) -> pd.DataFrame:
    """Her yönlü özellik için İKİ rakip hipotezi test eder: REVERSAL (aşırı
    uç -> TERS yön bahsi, tükenme mantığı) ve MOMENTUM (aşırı uç -> AYNI
    yön devam bahsi). Eşik: özelliğin kendi dağılımının üst/alt %20'si -
    özelliğe özel elle seçilmiş sihirli sayı YOK. Eşiği aşmayan günler o
    strateji için sinyal ÜRETMİYOR (hesap makinesindeki 'her hisseye
    mutlaka yön üret' kuralından FARKLI - burada amaç, TEK TEK
    stratejilerin izole kenarı var mı ölçmek, her hisseye zorla karar
    verdirmek değil)."""
    satirlar = []
    for ozellik in _yonlu_ozellikler_listesi():
        gecerli = df_all.dropna(subset=[ozellik, "sonraki_gun_getiri_pct"])
        if len(gecerli) < 200:
            continue
        ust_esik = gecerli[ozellik].quantile(1 - GOSTERGE_TURNUVASI_ESIK_YUZDE)
        alt_esik = gecerli[ozellik].quantile(GOSTERGE_TURNUVASI_ESIK_YUZDE)
        maske_ust = gecerli[ozellik] >= ust_esik
        maske_alt = gecerli[ozellik] <= alt_esik

        for tip, ust_yon, alt_yon in (("reversal", "SHORT", "LONG"), ("momentum", "LONG", "SHORT")):
            yon_serisi = pd.Series(np.nan, index=gecerli.index, dtype=object)
            yon_serisi[maske_ust] = ust_yon
            yon_serisi[maske_alt] = alt_yon
            secim = yon_serisi.notna()
            if secim.sum() < GOSTERGE_TURNUVASI_MIN_N:
                continue
            secilen = gecerli[secim]
            yon_sel = yon_serisi[secim]
            dogru = ((yon_sel == "LONG") & (secilen["sonraki_gun_getiri_pct"] > 0)) | \
                    ((yon_sel == "SHORT") & (secilen["sonraki_gun_getiri_pct"] < 0))
            isaretli_getiri = secilen["sonraki_gun_getiri_pct"] * np.where(yon_sel == "LONG", 1, -1)
            satirlar.append({
                "strateji": f"{ozellik} | {tip}", "n": int(secim.sum()),
                "kazanma_orani_pct": round(dogru.mean() * 100, 2),
                "ort_isaretli_getiri_pct": round(isaretli_getiri.mean(), 4),
            })

    # ABD'de KANITLANMIŞ 3 stratejinin BIST'e DOĞRUDAN transferi - aynı
    # eşik/mantık, hiçbir uyarlama yok, "burada da işliyor mu" sorusu.
    ozel_stratejiler = []
    if {"vol_zscore", "pct_change"}.issubset(df_all.columns):
        g = df_all.dropna(subset=["vol_zscore", "pct_change", "sonraki_gun_getiri_pct"])
        secim = (g["vol_zscore"] >= 2.0) & (g["pct_change"] != 0)
        if secim.sum() >= GOSTERGE_TURNUVASI_MIN_N:
            gg = g[secim]
            yon = np.where(gg["pct_change"] < 0, "LONG", "SHORT")
            dogru = ((yon == "LONG") & (gg["sonraki_gun_getiri_pct"] > 0)) | \
                    ((yon == "SHORT") & (gg["sonraki_gun_getiri_pct"] < 0))
            isaretli = gg["sonraki_gun_getiri_pct"] * np.where(yon == "LONG", 1, -1)
            ozel_stratejiler.append({
                "strateji": "[ABD transfer] Hacim Z-Skor>=2.0 tükenme",
                "n": int(secim.sum()), "kazanma_orani_pct": round(dogru.mean() * 100, 2),
                "ort_isaretli_getiri_pct": round(isaretli.mean(), 4),
            })
    if {"pct_change", "atr_pct"}.issubset(df_all.columns):
        g = df_all.dropna(subset=["pct_change", "atr_pct", "sonraki_gun_getiri_pct"])
        secim = g["pct_change"].abs() >= 2.0 * g["atr_pct"]
        if secim.sum() >= GOSTERGE_TURNUVASI_MIN_N:
            gg = g[secim]
            yon = np.where(gg["pct_change"] > 0, "LONG", "SHORT")
            dogru = ((yon == "LONG") & (gg["sonraki_gun_getiri_pct"] > 0)) | \
                    ((yon == "SHORT") & (gg["sonraki_gun_getiri_pct"] < 0))
            isaretli = gg["sonraki_gun_getiri_pct"] * np.where(yon == "LONG", 1, -1)
            ozel_stratejiler.append({
                "strateji": "[ABD transfer] ATR kırılımı x2.0 momentum",
                "n": int(secim.sum()), "kazanma_orani_pct": round(dogru.mean() * 100, 2),
                "ort_isaretli_getiri_pct": round(isaretli.mean(), 4),
            })
    if "rsi14" in df_all.columns:
        g = df_all.dropna(subset=["rsi14", "sonraki_gun_getiri_pct"])
        secim = (g["rsi14"] <= 25) | (g["rsi14"] >= 75)
        if secim.sum() >= GOSTERGE_TURNUVASI_MIN_N:
            gg = g[secim]
            yon = np.where(gg["rsi14"] <= 25, "LONG", "SHORT")
            dogru = ((yon == "LONG") & (gg["sonraki_gun_getiri_pct"] > 0)) | \
                    ((yon == "SHORT") & (gg["sonraki_gun_getiri_pct"] < 0))
            isaretli = gg["sonraki_gun_getiri_pct"] * np.where(yon == "LONG", 1, -1)
            ozel_stratejiler.append({
                "strateji": "[ABD transfer, yaklaşık - RSI21 yerine RSI14] <=25/>=75 aşırı uç reversal",
                "n": int(secim.sum()), "kazanma_orani_pct": round(dogru.mean() * 100, 2),
                "ort_isaretli_getiri_pct": round(isaretli.mean(), 4),
            })

    sonuc = pd.DataFrame(satirlar + ozel_stratejiler)
    if not sonuc.empty:
        sonuc = sonuc.sort_values("kazanma_orani_pct", ascending=False).reset_index(drop=True)
    return sonuc


def gosterge_turnuvasi_calistir(baslangic_str: str = "2026-01-01", bitis_str: str = None) -> tuple:
    """/hesap_tam_test'in AYNI veri çekme/gün döngüsü mantığını kullanır,
    ama hesap makinesinin ağırlıklı toplamı yerine HER ÖZELLİĞİ TEK
    BAŞINA test eder (ABD swing turnuvasındaki gibi izole). Döner:
    (dosya_yolu, özet_dict) ya da (None, hata_mesajı)."""
    import yfinance as yf
    bitis_str = bitis_str or datetime.now(timezone.utc).date().isoformat()
    baslangic, bitis = pd.Timestamp(baslangic_str), pd.Timestamp(bitis_str)

    index_df = yf.Ticker("XU100.IS").history(period="2y", interval="1d")
    if index_df is None or index_df.empty:
        return None, "XU100 endeks verisi çekilemedi."
    index_df.index = pd.to_datetime(index_df.index).tz_localize(None)
    index_pct = (index_df["Close"] - index_df["Close"].shift(1)) / index_df["Close"].shift(1) * 100
    islem_gunleri = index_df.index[(index_df.index >= baslangic) & (index_df.index <= bitis)]
    if len(islem_gunleri) == 0:
        return None, f"{baslangic_str} - {bitis_str} arasında işlem günü bulunamadı."

    print(f"[ARGE] Gösterge turnuvası başlıyor: {len(islem_gunleri)} işlem günü × "
          f"{len(BIST_TICKERS)} hisse", flush=True)

    parcalar = []
    for ticker in BIST_TICKERS:
        try:
            df = yf.Ticker(ticker).history(period="2y", interval="1d")
            if df is None or df.empty or len(df) < 100:
                continue
            df = df.rename(columns={"Open": "open", "High": "high", "Low": "low",
                                     "Close": "close", "Volume": "volume"})
            df = df[["open", "high", "low", "close", "volume"]].copy()
            df.index = pd.to_datetime(df.index).tz_localize(None)
            df = compute_features(df, index_pct)

            gunler = df.index[(df.index >= baslangic) & (df.index <= bitis)]
            alt = df.loc[df.index.isin(gunler)].copy()
            if alt.empty:
                continue
            alt["sonraki_gun_getiri_pct"] = np.nan
            for tarih in alt.index:
                idx = df.index.get_loc(tarih)
                if idx + 1 >= len(df):
                    continue
                entry = df.iloc[idx]["close"]
                cikis = df.iloc[idx + 1]["close"]
                if entry == 0:
                    continue
                getiri = (cikis - entry) / entry * 100
                if abs(getiri) < 0.001:
                    continue  # BAYAT VERİ KORUMASI - /hesap_tam_test ile AYNI kural
                alt.loc[tarih, "sonraki_gun_getiri_pct"] = getiri
            parcalar.append(alt)
        except Exception as e:
            print(f"[ARGE] {ticker} turnuva verisi hatası: {e}", flush=True)
        time.sleep(0.2)

    if not parcalar:
        return None, "Hiçbir hisse için veri üretilemedi."

    df_all = pd.concat(parcalar, ignore_index=False)
    df_all = df_all.dropna(subset=["sonraki_gun_getiri_pct"])
    if df_all.empty:
        return None, "Hiçbir geçerli (bayat olmayan) gün bulunamadı."

    tablo = _feature_strateji_matrisi(df_all)
    if tablo.empty:
        return None, "Yeterli örneklem büyüklüğüne ulaşan strateji bulunamadı."

    dosya_yolu = _data_path("gosterge_turnuvasi.csv")
    tablo.to_csv(dosya_yolu, index=False, encoding="utf-8-sig")

    ozet = {
        "gun_sayisi": len(islem_gunleri), "hisse_sayisi": len(BIST_TICKERS),
        "toplam_gozlem": len(df_all), "strateji_sayisi": len(tablo),
        "en_iyi_strateji": tablo.iloc[0]["strateji"],
        "en_iyi_kazanma_orani": tablo.iloc[0]["kazanma_orani_pct"],
        "en_iyi_n": int(tablo.iloc[0]["n"]),
    }
    return dosya_yolu, ozet


# =============================================================================
# KUR ARINDIRMA + SEKTÖR-GÖRECELİ TESTİ — 2026-08-18
# =============================================================================
# GEREKÇE: Kullanıcıyla birlikte teşhis ettiğimiz kök sorun (§7) - BIST
# hisselerinin çoğu, kendi hikayesinden çok TL/kur/makro havasını birlikte
# takip ediyor (v7'de bir günde hisselerin %83-96'sının aynı yöne gitmesi).
# relative_strength (XU100'e göre) zaten izole test edildi, edge çıkmadı.
# Bu test İKİ FARKLI arındırma denemesini AYNI ANDA test ediyor:
#   1) KUR ARINDIRMA: hissenin TL getirisinden USDTRY hareketini çıkarıp
#      "gerçek" (USD bazlı) getiriye bakıyor - belki sinyal kur gürültüsü
#      altında kayboluyordur.
#   2) SEKTÖR-GÖRECELİ: hisseyi piyasa geneline değil KENDİ SEKTÖRÜNE göre
#      kıyaslıyor (8 sektör grubu, kendi hariç sektör ortalaması).
# Her ikisi de HEDEFİ değiştiriyor (tahmin edilen şey), göstergeler
# (RSI, MACD vb.) aynı kalıyor - "belki doğru şeyi tahmin etmiyorduk"
# sorusuna cevap.

BIST_SEKTORLER = {
    "BANKA": ["GARAN.IS", "AKBNK.IS", "YKBNK.IS", "ISCTR.IS", "HALKB.IS", "VAKBN.IS"],
    "HOLDING": ["KCHOL.IS", "SAHOL.IS", "ENKAI.IS"],
    "SANAYI": ["SISE.IS", "EREGL.IS", "PETKM.IS", "SASA.IS", "ARCLK.IS"],
    "ULASIM_HAVACILIK": ["THYAO.IS", "PGSUS.IS", "TAVHL.IS"],
    "TUKETIM_PERAKENDE": ["BIMAS.IS", "MGROS.IS", "ULKER.IS", "VESTL.IS"],
    "ENERJI_MADEN": ["TUPRS.IS", "AKSEN.IS", "KOZAL.IS", "OYAKC.IS"],
    "TEKNOLOJI_TELEKOM": ["TCELL.IS", "TTKOM.IS", "ASELS.IS"],
    "OTOMOTIV": ["FROTO.IS", "TOASO.IS"],
}
_TICKER_SEKTOR = {t: s for s, tks in BIST_SEKTORLER.items() for t in tks}


def _hedef_matrisi_genel(df_all: pd.DataFrame, hedef_kolonu: str, etiket: str) -> pd.DataFrame:
    """_feature_strateji_matrisi ile AYNI mantık ama hedef kolonu
    parametrik - farklı hedef tanımlarını (kur-arındırılmış, sektör-
    göreceli) aynı test çatısıyla karşılaştırabilmek için."""
    satirlar = []
    for ozellik in _yonlu_ozellikler_listesi():
        gecerli = df_all.dropna(subset=[ozellik, hedef_kolonu])
        if len(gecerli) < 200:
            continue
        ust_esik = gecerli[ozellik].quantile(1 - GOSTERGE_TURNUVASI_ESIK_YUZDE)
        alt_esik = gecerli[ozellik].quantile(GOSTERGE_TURNUVASI_ESIK_YUZDE)
        maske_ust = gecerli[ozellik] >= ust_esik
        maske_alt = gecerli[ozellik] <= alt_esik

        for tip, ust_yon, alt_yon in (("reversal", "SHORT", "LONG"), ("momentum", "LONG", "SHORT")):
            yon_serisi = pd.Series(np.nan, index=gecerli.index, dtype=object)
            yon_serisi[maske_ust] = ust_yon
            yon_serisi[maske_alt] = alt_yon
            secim = yon_serisi.notna()
            if secim.sum() < GOSTERGE_TURNUVASI_MIN_N:
                continue
            secilen = gecerli[secim]
            yon_sel = yon_serisi[secim]
            dogru = ((yon_sel == "LONG") & (secilen[hedef_kolonu] > 0)) | \
                    ((yon_sel == "SHORT") & (secilen[hedef_kolonu] < 0))
            isaretli = secilen[hedef_kolonu] * np.where(yon_sel == "LONG", 1, -1)
            dogru_n = int(dogru.sum())
            p = _binom_p(dogru_n, int(secim.sum()))
            satirlar.append({
                "hedef": etiket, "strateji": f"{ozellik} | {tip}", "n": int(secim.sum()),
                "kazanma_orani_pct": round(dogru.mean() * 100, 2),
                "binom_p": p, "ort_isaretli_getiri_pct": round(isaretli.mean(), 4),
            })
    return pd.DataFrame(satirlar)


def kur_sektor_testi_calistir(baslangic_str: str = "2026-01-01", bitis_str: str = None) -> tuple:
    """gosterge_turnuvasi_calistir ile AYNI veri/döngü mantığı, ama HER
    hisse için üç ayrı hedef hesaplıyor: ham getiri (kıyas için), kur-
    arındırılmış getiri, sektör-göreceli getiri. Aynı 13 gösterge, üç
    farklı hedefe karşı test ediliyor - hangisi (varsa) gerçek bir kenar
    ortaya çıkarıyor. Döner: (dosya_yolu, özet_dict) ya da (None, hata)."""
    import yfinance as yf
    bitis_str = bitis_str or datetime.now(timezone.utc).date().isoformat()
    baslangic, bitis = pd.Timestamp(baslangic_str), pd.Timestamp(bitis_str)

    index_df = yf.Ticker("XU100.IS").history(period="2y", interval="1d")
    if index_df is None or index_df.empty:
        return None, "XU100 endeks verisi çekilemedi."
    index_df.index = pd.to_datetime(index_df.index).tz_localize(None)
    index_pct = (index_df["Close"] - index_df["Close"].shift(1)) / index_df["Close"].shift(1) * 100

    kur_df = yf.Ticker("USDTRY=X").history(period="2y", interval="1d")
    if kur_df is None or kur_df.empty:
        return None, "USDTRY kur verisi çekilemedi."
    kur_df.index = pd.to_datetime(kur_df.index).tz_localize(None)
    kur_close = kur_df["Close"]

    islem_gunleri = index_df.index[(index_df.index >= baslangic) & (index_df.index <= bitis)]
    if len(islem_gunleri) == 0:
        return None, f"{baslangic_str} - {bitis_str} arasında işlem günü bulunamadı."

    print(f"[ARGE] Kur/Sektör testi başlıyor: {len(islem_gunleri)} işlem günü × "
          f"{len(BIST_TICKERS)} hisse", flush=True)

    # 1. AŞAMA: her hisse için ozellikler + HAM sonraki-gun getirisi (kur
    # arindirmasi icin kur getirisini de topluyoruz, sektor icin ham
    # getiriyi ayri bir kolonda tutup asama 2'de capraz-hisse ortalamasini
    # alacagiz).
    parcalar = []
    for ticker in BIST_TICKERS:
        try:
            df = yf.Ticker(ticker).history(period="2y", interval="1d")
            if df is None or df.empty or len(df) < 100:
                continue
            df = df.rename(columns={"Open": "open", "High": "high", "Low": "low",
                                     "Close": "close", "Volume": "volume"})
            df = df[["open", "high", "low", "close", "volume"]].copy()
            df.index = pd.to_datetime(df.index).tz_localize(None)
            df = compute_features(df, index_pct)

            gunler = df.index[(df.index >= baslangic) & (df.index <= bitis)]
            alt = df.loc[df.index.isin(gunler)].copy()
            if alt.empty:
                continue
            alt["sonraki_gun_ham_getiri_pct"] = np.nan
            alt["sonraki_gun_kur_arindirilmis_getiri_pct"] = np.nan
            for tarih in alt.index:
                idx = df.index.get_loc(tarih)
                if idx + 1 >= len(df):
                    continue
                entry = df.iloc[idx]["close"]
                cikis = df.iloc[idx + 1]["close"]
                if entry == 0:
                    continue
                getiri = (cikis - entry) / entry * 100
                if abs(getiri) < 0.001:
                    continue  # BAYAT VERİ KORUMASI
                alt.loc[tarih, "sonraki_gun_ham_getiri_pct"] = getiri

                # kur arindirmasi: hissenin USD bazli getirisi
                tarih_sonraki = df.index[idx + 1]
                try:
                    kur_entry = kur_close.reindex([tarih], method="nearest").iloc[0]
                    kur_cikis = kur_close.reindex([tarih_sonraki], method="nearest").iloc[0]
                    if kur_entry and kur_entry != 0:
                        kur_getiri = (kur_cikis - kur_entry) / kur_entry * 100
                        kur_arindirilmis = ((1 + getiri / 100) / (1 + kur_getiri / 100) - 1) * 100
                        alt.loc[tarih, "sonraki_gun_kur_arindirilmis_getiri_pct"] = kur_arindirilmis
                except Exception:
                    pass
            alt["ticker"] = ticker
            alt["sektor"] = _TICKER_SEKTOR.get(ticker, "DIGER")
            parcalar.append(alt)
        except Exception as e:
            print(f"[ARGE] {ticker} kur/sektör verisi hatası: {e}", flush=True)
        time.sleep(0.2)

    if not parcalar:
        return None, "Hiçbir hisse için veri üretilemedi."

    df_all = pd.concat(parcalar, ignore_index=False)
    df_all = df_all.dropna(subset=["sonraki_gun_ham_getiri_pct"])
    if df_all.empty:
        return None, "Hiçbir geçerli (bayat olmayan) gün bulunamadı."

    # 2. AŞAMA: sektör-göreceli getiri - her (tarih, sektör) grubu için
    # KENDİ HARİÇ sektör ortalaması çıkarılıyor (çapraz-hisse işlem,
    # bu yüzden tüm hisseler birleştirildikten SONRA yapılıyor).
    df_all["_tarih"] = df_all.index
    grup = df_all.groupby(["_tarih", "sektor"])["sonraki_gun_ham_getiri_pct"]
    toplam = grup.transform("sum")
    sayi = grup.transform("count")
    # kendi haric ortalama = (toplam - kendisi) / (sayi - 1), sayi<2 ise NaN
    kendi_haric_ort = np.where(sayi > 1, (toplam - df_all["sonraki_gun_ham_getiri_pct"]) / (sayi - 1), np.nan)
    df_all["sonraki_gun_sektor_relatif_getiri_pct"] = df_all["sonraki_gun_ham_getiri_pct"] - kendi_haric_ort
    df_all = df_all.drop(columns=["_tarih"])

    # 3. AŞAMA: üç hedefe karşı ayrı ayrı test
    tablo_ham = _hedef_matrisi_genel(df_all, "sonraki_gun_ham_getiri_pct", "HAM (kıyas)")
    tablo_kur = _hedef_matrisi_genel(df_all, "sonraki_gun_kur_arindirilmis_getiri_pct", "KUR ARINDIRILMIŞ")
    tablo_sektor = _hedef_matrisi_genel(df_all, "sonraki_gun_sektor_relatif_getiri_pct", "SEKTÖR-GÖRECELİ")

    tablo = pd.concat([tablo_ham, tablo_kur, tablo_sektor], ignore_index=True)
    if tablo.empty:
        return None, "Yeterli örneklem büyüklüğüne ulaşan strateji bulunamadı."
    tablo = tablo.sort_values("kazanma_orani_pct", ascending=False).reset_index(drop=True)

    dosya_yolu = _data_path("kur_sektor_testi.csv")
    tablo.to_csv(dosya_yolu, index=False, encoding="utf-8-sig")

    ozet = {
        "gun_sayisi": len(islem_gunleri), "hisse_sayisi": len(BIST_TICKERS),
        "toplam_gozlem": len(df_all), "strateji_sayisi": len(tablo),
        "en_iyi_strateji": tablo.iloc[0]["strateji"], "en_iyi_hedef": tablo.iloc[0]["hedef"],
        "en_iyi_kazanma_orani": tablo.iloc[0]["kazanma_orani_pct"],
        "en_iyi_p": tablo.iloc[0]["binom_p"], "en_iyi_n": int(tablo.iloc[0]["n"]),
    }
    return dosya_yolu, ozet
# =============================================================================
# GEREKÇE: Kullanıcıyla konuşurken çıkan fikir — RSI/MACD gibi HERKESİN aynı
# formülle hesaplayabildiği göstergeler yerine, "kimsenin bakmadığı" bir veri
# türü denemek. yfinance'in insider_transactions verisi (SEC Form 4 tabanlı,
# ABD şirketlerinde yönetici/yönetim kurulu üyesi/büyük ortağın kendi hisse
# alım-satımı) TAM bunu karşılıyor: sıfır API anahtarı, sıfır kayıt (zaten
# kullanılan yfinance'in bir parçası), VE geçmişe dönük veri var - beklemeden
# şimdi backtest edilebiliyor (opsiyon zincirinin aksine).
# DÜRÜST SINIR: yfinance'in insider_transactions kolon adları sürüme göre
# değişebilir - ilk canlı çalıştırmada beklenmeyen bir yapı görülürse
# konsola yazdırılıp o hisse atlanıyor, sessizce yanlış yorumlanmıyor.

US_INSIDER_TICKERS = [
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

ICGORUSEL_ISLEM_MIN_N = 20
ICGORUSEL_ISLEM_GUN_UFKU = 20  # islem sonrasi kac islem gunu getirisine bakilacak


def _binom_p(dogru_n: int, toplam_n: int):
    if toplam_n == 0:
        return None
    return round(_stats.binomtest(dogru_n, toplam_n, 0.5).pvalue, 5)


def _yfinance_dene(fn, deneme=3, bekleme=5):
    """Yahoo'nun 'Too Many Requests' hız sınırlamasına karşı basit yeniden
    deneme - projede KOZAL.IS'te de gözlenen aynı sorun (bkz. AI Lab
    logları). Her başarısız denemede bekleme süresi katlanarak artar."""
    for deneme_no in range(deneme):
        try:
            return fn()
        except Exception as e:
            if deneme_no == deneme - 1:
                raise
            print(f"[İçeriden İşlem] hız sınırı/hata, {bekleme}sn sonra "
                  f"yeniden denenecek ({deneme_no+1}/{deneme}): {e}", flush=True)
            time.sleep(bekleme)
            bekleme *= 2


def icgorusel_islem_testi_calistir(gun_ufku: int = ICGORUSEL_ISLEM_GUN_UFKU) -> tuple:
    """yfinance Ticker.insider_transactions verisiyle: içeriden ALIM'den
    sonra hisse gerçekten daha mı çok yükseliyor, içeriden SATIM'dan sonra
    daha mı çok düşüyor - GUN_UFKU işlem günü sonraki getiriye bakarak
    test eder. Döner: (dosya_yolu, özet_dict) ya da (None, hata_mesajı)."""
    import yfinance as yf
    kayitlar = []
    atlanan_hata_sayisi = 0
    for n_i, ticker in enumerate(US_INSIDER_TICKERS, 1):
        try:
            print(f"[İçeriden İşlem {n_i}/{len(US_INSIDER_TICKERS)}] {ticker}...", flush=True)
            t = yf.Ticker(ticker)
            islemler = _yfinance_dene(lambda: t.insider_transactions)
            if islemler is None or islemler.empty:
                continue
            time.sleep(1.0)
            fiyat_df = _yfinance_dene(lambda: t.history(period="2y", interval="1d"))
            if fiyat_df is None or fiyat_df.empty:
                continue
            fiyat_df = fiyat_df.rename(columns={"Close": "close"})
            fiyat_df.index = pd.to_datetime(fiyat_df.index).tz_localize(None)

            tarih_kolonu = next((k for k in ["Start Date", "StartDate", "Date"]
                                  if k in islemler.columns), None)
            islem_kolonu = next((k for k in ["Transaction", "Text"]
                                  if k in islemler.columns), None)
            if tarih_kolonu is None or islem_kolonu is None:
                print(f"[İçeriden İşlem] {ticker}: beklenmeyen kolon yapısı: "
                      f"{list(islemler.columns)}", flush=True)
                continue

            for _, row in islemler.iterrows():
                try:
                    islem_tarihi = pd.to_datetime(row[tarih_kolonu]).tz_localize(None)
                except Exception:
                    continue
                metin = str(row[islem_kolonu])
                if "Purchase" in metin:
                    tur = "ALIM"
                elif "Sale" in metin:
                    tur = "SATIM"
                else:
                    continue

                giris_konum = fiyat_df.index.get_indexer([islem_tarihi], method="nearest")[0]
                if giris_konum < 0 or giris_konum + gun_ufku >= len(fiyat_df):
                    continue
                giris_fiyat = fiyat_df.iloc[giris_konum]["close"]
                cikis_fiyat = fiyat_df.iloc[giris_konum + gun_ufku]["close"]
                if giris_fiyat == 0 or pd.isna(giris_fiyat) or pd.isna(cikis_fiyat):
                    continue
                getiri = (cikis_fiyat - giris_fiyat) / giris_fiyat * 100
                kayitlar.append({"ticker": ticker, "tarih": islem_tarihi.date().isoformat(),
                                  "tur": tur, "getiri_pct": round(getiri, 3)})
        except Exception as e:
            atlanan_hata_sayisi += 1
            print(f"[İçeriden İşlem] {ticker} hata (atlandı): {e}", flush=True)
        time.sleep(1.5)

    if not kayitlar:
        return None, (f"Hiçbir işlem kaydı üretilemedi ({atlanan_hata_sayisi}/"
                       f"{len(US_INSIDER_TICKERS)} hisse hata verdi - muhtemelen "
                       f"Yahoo Finance hız sınırlaması, Render loglarına bak).")

    df = pd.DataFrame(kayitlar)
    dosya_yolu = _data_path("icgorusel_islem_testi.csv")
    df.to_csv(dosya_yolu, index=False, encoding="utf-8-sig")

    ozet_satirlari = []
    for tur, dogru_yon in [("ALIM", 1), ("SATIM", -1)]:
        alt = df[df["tur"] == tur]
        if len(alt) < ICGORUSEL_ISLEM_MIN_N:
            continue
        dogru_n = int((alt["getiri_pct"] * dogru_yon > 0).sum())
        p = _binom_p(dogru_n, len(alt))
        ozet_satirlari.append({
            "tur": tur, "n": len(alt), "dogru_n": dogru_n,
            "kazanma_orani_pct": round(dogru_n / len(alt) * 100, 2),
            "binom_p": p, "ort_getiri_pct": round(alt["getiri_pct"].mean(), 4),
        })

    if not ozet_satirlari:
        return None, f"Yeterli örneklem büyüklüğüne ({ICGORUSEL_ISLEM_MIN_N}) ulaşan ALIM/SATIM grubu bulunamadı."

    return dosya_yolu, {"gun_ufku": gun_ufku, "toplam_islem": len(df), "gruplar": ozet_satirlari}


# =============================================================================
# SEC EDGAR TABANLI İÇERİDEN İŞLEM TESTİ — 2026-08-18
# =============================================================================
# GEREKÇE: yfinance.insider_transactions bugün 106 hisseden 74'ünde hata
# verdi (muhtemelen Yahoo hız sınırlaması) - kullanıcının kendi önerisiyle
# (DeepSeek analizi de aynısını önerdi) doğrudan SEC EDGAR'a geçiliyor.
# EDGAR resmi, ücretsiz, kayıtsız bir devlet kaynağı - SADECE User-Agent
# header'ı istiyor (kimlik bilgisi değil, sadece "kim olduğunu söyle").

EDGAR_HEADERS = {"User-Agent": "arge-botu-arastirma contact@example.com"}
EDGAR_MIN_N = 20


def _edgar_cik_haritasi() -> dict:
    """SEC'in ticker->CIK haritasını çeker (tek seferlik, ~10.000 şirket,
    ücretsiz, kayıtsız). Döner: {"AAPL": "0000320193", ...}"""
    try:
        resp = requests.get("https://www.sec.gov/files/company_tickers.json",
                             headers=EDGAR_HEADERS, timeout=15)
        resp.raise_for_status()
        veri = resp.json()
        return {v["ticker"]: str(v["cik_str"]).zfill(10) for v in veri.values()}
    except Exception as e:
        print(f"[EDGAR] CIK haritası çekilemedi: {e}", flush=True)
        return {}


def _edgar_form4_listesi(cik: str, ticker: str) -> list:
    """Bir şirketin Form 4 (içeriden işlem bildirimi) dosyalama listesini
    çeker - tarih ve accession number döner, işlem detayı DEĞİL (o ayrı
    bir XML çağrısı gerektiriyor, sonraki fonksiyonda)."""
    try:
        resp = requests.get(f"https://data.sec.gov/submissions/CIK{cik}.json",
                             headers=EDGAR_HEADERS, timeout=15)
        resp.raise_for_status()
        veri = resp.json()
        recent = veri.get("filings", {}).get("recent", {})
        formlar = recent.get("form", [])
        tarihler = recent.get("filingDate", [])
        accessionlar = recent.get("accessionNumber", [])
        sonuc = []
        for form, tarih, acc in zip(formlar, tarihler, accessionlar):
            if form == "4":
                sonuc.append({"tarih": tarih, "accession": acc})
        return sonuc
    except Exception as e:
        print(f"[EDGAR] {ticker} Form 4 listesi hatası: {e}", flush=True)
        return []


def _edgar_form4_detay(cik: str, accession: str) -> list:
    """Bir Form 4 dosyasının XML'ini indirip ALIM/SATIM işlemlerini
    ayıklar (transactionCode P=Purchase, S=Sale)."""
    acc_no_dash = accession.replace("-", "")
    try:
        idx_resp = requests.get(
            f"https://www.sec.gov/Archives/edgar/data/{int(cik)}/{acc_no_dash}/{accession}-index.htm",
            headers=EDGAR_HEADERS, timeout=15)
        # XML dosyasını bulmak icin index.json daha guvenilir
        idx_json = requests.get(
            f"https://www.sec.gov/Archives/edgar/data/{int(cik)}/{acc_no_dash}/index.json",
            headers=EDGAR_HEADERS, timeout=15).json()
        xml_dosya = None
        for item in idx_json.get("directory", {}).get("item", []):
            ad = item.get("name", "")
            if ad.endswith(".xml") and "form4" not in ad.lower() and ad != "primary_doc.xml":
                continue
            if ad.endswith(".xml"):
                xml_dosya = ad
                break
        if xml_dosya is None:
            return []
        xml_resp = requests.get(
            f"https://www.sec.gov/Archives/edgar/data/{int(cik)}/{acc_no_dash}/{xml_dosya}",
            headers=EDGAR_HEADERS, timeout=15)
        import xml.etree.ElementTree as ET
        root = ET.fromstring(xml_resp.content)
        sonuc = []
        for islem in root.iter("nonDerivativeTransaction"):
            kod_el = islem.find(".//transactionCode")
            tarih_el = islem.find(".//transactionDate/value")
            if kod_el is None or tarih_el is None:
                continue
            kod = kod_el.text
            if kod == "P":
                sonuc.append({"tarih": tarih_el.text, "tur": "ALIM"})
            elif kod == "S":
                sonuc.append({"tarih": tarih_el.text, "tur": "SATIM"})
        return sonuc
    except Exception as e:
        print(f"[EDGAR] {accession} detay hatası: {e}", flush=True)
        return []


def icgorusel_islem_testi_edgar_calistir(gun_ufku: int = ICGORUSEL_ISLEM_GUN_UFKU,
                                          max_hisse: int = None) -> tuple:
    """yfinance yerine SEC EDGAR'ı kullanan versiyon. max_hisse=None ise
    TÜM US_INSIDER_TICKERS (106 hisse) taranır - varsayılan artık tam
    kapsamlı, önceki 40'lık sınır sadece hızlı ön-test içindi.
    2026-08-19 DÜZELTME: bir Form 4 dosyasında/aynı günde birden fazla
    işlem satırı olabiliyor (ör. bir yönetici aynı gün farklı lotlarda
    satış yapınca) - bu, örneklemi YAPAY şişiriyordu (AAPL'de aynı gün
    aynı getiri 8 kez tekrarlanmıştı). Artık istatistik HESAPLANMADAN
    ÖNCE (ticker, tarih, tür) bazında tekilleştiriliyor - CSV'ye ham hali
    yazılıyor (şeffaflık için) ama özet/anlamlılık TEKİL veriden."""
    import yfinance as yf
    cik_haritasi = _edgar_cik_haritasi()
    if not cik_haritasi:
        return None, "SEC CIK haritası çekilemedi."

    kayitlar = []
    hisseler = US_INSIDER_TICKERS if max_hisse is None else US_INSIDER_TICKERS[:max_hisse]
    for n_i, ticker in enumerate(hisseler, 1):
        cik = cik_haritasi.get(ticker)
        if not cik:
            continue
        try:
            print(f"[EDGAR İçeriden İşlem {n_i}/{len(hisseler)}] {ticker}...", flush=True)
            fiyat_df = yf.Ticker(ticker).history(period="2y", interval="1d")
            if fiyat_df is None or fiyat_df.empty:
                continue
            fiyat_df = fiyat_df.rename(columns={"Close": "close"})
            fiyat_df.index = pd.to_datetime(fiyat_df.index).tz_localize(None)

            form4ler = _edgar_form4_listesi(cik, ticker)
            time.sleep(0.15)
            for f in form4ler[:30]:  # her hisse icin en fazla 30 son dosyalama
                detaylar = _edgar_form4_detay(cik, f["accession"])
                time.sleep(0.15)
                for d in detaylar:
                    try:
                        islem_tarihi = pd.to_datetime(d["tarih"]).tz_localize(None)
                    except Exception:
                        continue
                    giris_konum = fiyat_df.index.get_indexer([islem_tarihi], method="nearest")[0]
                    if giris_konum < 0 or giris_konum + gun_ufku >= len(fiyat_df):
                        continue
                    giris_fiyat = fiyat_df.iloc[giris_konum]["close"]
                    cikis_fiyat = fiyat_df.iloc[giris_konum + gun_ufku]["close"]
                    if giris_fiyat == 0 or pd.isna(giris_fiyat) or pd.isna(cikis_fiyat):
                        continue
                    getiri = (cikis_fiyat - giris_fiyat) / giris_fiyat * 100
                    kayitlar.append({"ticker": ticker, "tarih": d["tarih"],
                                      "tur": d["tur"], "getiri_pct": round(getiri, 3)})
        except Exception as e:
            print(f"[EDGAR İçeriden İşlem] {ticker} hata: {e}", flush=True)

    if not kayitlar:
        return None, "EDGAR'dan hiçbir işlem kaydı üretilemedi."

    df = pd.DataFrame(kayitlar)
    dosya_yolu = _data_path("icgorusel_islem_edgar.csv")
    df.to_csv(dosya_yolu, index=False, encoding="utf-8-sig")
    # istatistik icin TEKILLESTIRILMIS veri kullan - ham veri sadece CSV'de saklaniyor
    df = df.drop_duplicates(subset=["ticker", "tarih", "tur"])

    ozet_satirlari = []
    for tur, dogru_yon in [("ALIM", 1), ("SATIM", -1)]:
        alt = df[df["tur"] == tur]
        if len(alt) < EDGAR_MIN_N:
            continue
        dogru_n = int((alt["getiri_pct"] * dogru_yon > 0).sum())
        p = _binom_p(dogru_n, len(alt))
        ozet_satirlari.append({
            "tur": tur, "n": len(alt), "dogru_n": dogru_n,
            "kazanma_orani_pct": round(dogru_n / len(alt) * 100, 2),
            "binom_p": p, "ort_getiri_pct": round(alt["getiri_pct"].mean(), 4),
        })

    if not ozet_satirlari:
        return None, f"Yeterli örneklem büyüklüğüne ({EDGAR_MIN_N}) ulaşan ALIM/SATIM grubu bulunamadı."

    return dosya_yolu, {"gun_ufku": gun_ufku, "toplam_islem": len(df),
                         "hisse_sayisi": len(hisseler), "gruplar": ozet_satirlari}


# =============================================================================
# TERS İŞLEM (INVERSE) TESTİ — 2026-08-18
# =============================================================================
# GEREKÇE: DeepSeek'in önerdiği "Fitil+RSI+Hacim negatif çıktı, tersini
# dene" fikri - AMA doğru şekilde. Sadece R-katsayısının işaretini
# çevirmek MATEMATİKSEL OLARAK YANLIŞ olurdu (kayıp/kazanç asimetrik:
# sabit -1R kayıp, değişken +kısmi TP/trailing kazanç - orijinal yönün
# mumuna göre hesaplanmış stop/hedef, basitçe ters çevrilemez). Bunun
# yerine YÖNÜ GERÇEKTEN TERS ALIP _kanit_bist_rr_sonuc'u SIFIRDAN,
# doğru stop/hedef ile (ters yönün kendi mum verisine göre) çalıştırıyoruz.

def kanit_ters_islem_testi_calistir() -> tuple:
    """Fitil+RSI+Hacim ve Sadece RSI'ın SİNYAL YÖNÜNÜ ters çevirip
    (LONG yerine SHORT, SHORT yerine LONG), _kanit_bist_rr_sonuc'u o
    TERS yön için SIFIRDAN (doğru stop/hedef ile) çalıştırır - basit
    işaret çevirme DEĞİL, gerçek yeniden hesaplama."""
    import yfinance as yf
    tum_sonuclar = {"[TERS] Fitil+RSI+Hacim": [], "[TERS] Sadece RSI": []}
    ters_yon = {"LONG": "SHORT", "SHORT": "LONG"}
    for n_i, ticker in enumerate(BIST_TICKERS, 1):
        try:
            print(f"[Ters İşlem Testi {n_i}/{len(BIST_TICKERS)}] {ticker}...", flush=True)
            df = yf.Ticker(ticker).history(period="2y", interval="1d")
            if df is None or df.empty or len(df) < 60:
                continue
            df = df.rename(columns={"Open": "open", "High": "high", "Low": "low",
                                     "Close": "close", "Volume": "volume"})
            df = df[["open", "high", "low", "close", "volume"]].copy().reset_index(drop=True)
            df = _kanit_compute_indicators(df)

            for idx in range(25, len(df) - 1):
                row = df.iloc[idx]
                for isim, fn in [("[TERS] Fitil+RSI+Hacim", _kanit_check_exhaustion),
                                  ("[TERS] Sadece RSI", _kanit_check_rsi_only)]:
                    orijinal_yon = fn(row)
                    if orijinal_yon is None:
                        continue
                    yon = ters_yon[orijinal_yon]
                    sonuc = _kanit_bist_rr_sonuc(df, idx, yon)
                    if sonuc is None:
                        continue
                    durum, r = sonuc
                    tum_sonuclar[isim].append((durum, r))
        except Exception as e:
            print(f"[Ters İşlem Testi] {ticker} hata: {e}", flush=True)
        time.sleep(1.0)

    satirlar = _kanit_ozet_tablosu(tum_sonuclar)
    if not satirlar:
        return None, "Hiçbir strateji için veri üretilemedi."
    tablo = pd.DataFrame(satirlar)
    dosya_yolu = _data_path("kanit_ters_islem_sonucu.csv")
    tablo.to_csv(dosya_yolu, index=False, encoding="utf-8-sig")
    return dosya_yolu, {"satirlar": satirlar}
# yeniden test eder — 2026-08-18
# =============================================================================
# GEREKÇE: Kullanıcı, tüm bu yeni-kenar aramalarını (hesap makinesi, izole
# turnuva, içeriden işlem, emir defteri) bir kenara bırakıp önce ELİMİZDEKİ
# kanıtlanmış sistemleri (Fitil+RSI+Hacim, Sadece RSI, ATR Kırılımı, Hacim
# Z-Skor) aynı titizlikle (istatistiksel anlamlılık dahil) tazeden doğrulamak
# istedi. Bu, daha önce `kanit_dogrulama.py` adıyla STANDALONE bir script
# olarak yazılmıştı (Start Command değişimi gerektiriyordu) - kullanıcı bunu
# istemedi. Aynı mantık şimdi buraya, normal bir komut olarak taşındı.
# DÜRÜST SINIR: Orijinal 87-stratejili mega turnuva scripti elimizde yok, bu
# yüzden o turnuvanın birebir kopyası değil. Giriş koşulları
# (check_exhaustion, check_rsi_only, check_us_atr_breakout,
# check_us_volume_zscore) stock_screener_bot.py'den DEĞİŞTİRİLMEDEN alındı;
# çıkış tanımı canlı sistemin gerçekten kullandığı iki yöntemi taklit ediyor:
# BIST'te ATR-stop + sabit 1:2 R:R, ABD'de US_SWING_CHECKPOINTS ile birebin
# aynı (1g/%1, 3g/%2, 5g/%3, 10g/%5). Rejim/trend filtreleri kasıtlı DAHIL
# EDİLMEDİ - "78.2%" gibi rakamlar sadece çekirdek giriş kapısını ölçüyordu.

KANIT_RSI_OVERSOLD, KANIT_RSI_OVERBOUGHT = 30, 70
KANIT_WICK_RATIO_THRESHOLD = 0.35
KANIT_VOLUME_MULTIPLIER = 1.5
KANIT_INVALIDATION_ATR_BUFFER = 1.0
KANIT_US_ZSCORE_THRESHOLD = 2.0
KANIT_US_ATR_MULT = 2.0
KANIT_RR_HEDEF_ORANI = 2.0  # eski basit simulasyon (v12) - artik kullanilmiyor, v13 staged mantik kullaniyor
KANIT_PARTIAL_TP_R_MULT = 1.5   # stock_screener_bot.py PARTIAL_TP_R_MULT ile birebin ayni
KANIT_TRAIL_ATR_MULT = 2.0      # TRAIL_ATR_MULT ile birebin ayni
KANIT_TRAIL_MIN_MOVE_PCT = 1.0  # TRAIL_MIN_MOVE_PCT ile birebin ayni
KANIT_MAX_BEKLEME_GUNU = 40
KANIT_US_CHECKPOINTS = [(1, "1g", 1.0), (3, "3g", 2.0), (5, "5g", 3.0), (10, "10g", 5.0)]
KANIT_MIN_N = 20


def _kanit_compute_indicators(df: pd.DataFrame) -> pd.DataFrame:
    """stock_screener_bot.py compute_indicators() ile BİREBİN AYNI formüller."""
    df["vol_sma20"] = df["volume"].rolling(20).mean()
    df["vol_std20"] = df["volume"].rolling(20).std()
    df["vol_zscore"] = (df["volume"] - df["vol_sma20"]) / df["vol_std20"].replace(0, np.nan)

    high_low = df["high"] - df["low"]
    high_close = (df["high"] - df["close"].shift()).abs()
    low_close = (df["low"] - df["close"].shift()).abs()
    tr = pd.concat([high_low, high_close, low_close], axis=1).max(axis=1)
    df["atr14"] = tr.rolling(14).mean()

    delta = df["close"].diff()
    gain = delta.clip(lower=0)
    loss = -delta.clip(upper=0)
    avg_gain = gain.ewm(alpha=1 / 14, adjust=False).mean()
    avg_loss = loss.ewm(alpha=1 / 14, adjust=False).mean()
    rs = avg_gain / avg_loss.replace(0, np.nan)
    df["rsi"] = (100 - (100 / (1 + rs))).fillna(50)

    candle_range = (df["high"] - df["low"]).replace(0, np.nan)
    df["lower_wick_ratio"] = ((df[["open", "close"]].min(axis=1) - df["low"]) / candle_range).fillna(0)
    df["upper_wick_ratio"] = ((df["high"] - df[["open", "close"]].max(axis=1)) / candle_range).fillna(0)
    return df


def _kanit_check_exhaustion(row) -> str:
    volume_ratio = row["volume"] / row["vol_sma20"] if row["vol_sma20"] else 0
    if pd.isna(volume_ratio) or volume_ratio < KANIT_VOLUME_MULTIPLIER:
        return None
    if row["lower_wick_ratio"] >= KANIT_WICK_RATIO_THRESHOLD and row["rsi"] <= KANIT_RSI_OVERSOLD:
        return "LONG"
    if row["upper_wick_ratio"] >= KANIT_WICK_RATIO_THRESHOLD and row["rsi"] >= KANIT_RSI_OVERBOUGHT:
        return "SHORT"
    return None


def _kanit_check_rsi_only(row) -> str:
    if row["rsi"] <= KANIT_RSI_OVERSOLD:
        return "LONG"
    if row["rsi"] >= KANIT_RSI_OVERBOUGHT:
        return "SHORT"
    return None


def _kanit_check_us_volume_zscore(row) -> str:
    if pd.isna(row.get("vol_zscore")) or row["vol_zscore"] < KANIT_US_ZSCORE_THRESHOLD:
        return None
    if row["close"] < row["open"]:
        return "LONG"
    if row["close"] > row["open"]:
        return "SHORT"
    return None


def _kanit_check_us_atr_breakout(row, prev_close) -> str:
    if pd.isna(row.get("atr14")) or row["atr14"] == 0:
        return None
    move = row["close"] - prev_close
    if move >= KANIT_US_ATR_MULT * row["atr14"]:
        return "LONG"
    if move <= -KANIT_US_ATR_MULT * row["atr14"]:
        return "SHORT"
    return None


def _kanit_bist_rr_sonuc(df: pd.DataFrame, idx: int, direction: str):
    """2026-08-18 DÜZELTME: canlı sistemin GERÇEK check_exit_alerts()
    mantığıyla birebir aynı üç aşamalı çıkış - ilk sürüm sabit 1:2 R:R
    hard target/stop kullanıyordu, bu YANLIŞTI ve BIST sonuçlarını
    yapay olarak çok kötü gösterdi. Gerçek sistem: 1.5R'de PARSİYEL
    (yarısı kilitlenir, stop breakeven'e çekilir), sonra ATR ile
    TRAILING STOP. Ayrıca canlı sistem SADECE KAPANIŞ fiyatını kontrol
    ediyor (gün içi high/low DEĞİL) - bu da birebir taklit edildi."""
    row = df.iloc[idx]
    entry = row["close"]
    atr = row["atr14"] if pd.notna(row["atr14"]) else 0
    buffer = atr * KANIT_INVALIDATION_ATR_BUFFER
    if direction == "LONG":
        stop = row["low"] - buffer
        stop_dist = entry - stop
    else:
        stop = row["high"] + buffer
        stop_dist = stop - entry
    if stop_dist <= 0:
        return None
    tp = entry + stop_dist * KANIT_PARTIAL_TP_R_MULT if direction == "LONG" \
        else entry - stop_dist * KANIT_PARTIAL_TP_R_MULT

    partial_done = False
    trail_stop = None
    for i in range(idx + 1, min(idx + 1 + KANIT_MAX_BEKLEME_GUNU, len(df))):
        gun = df.iloc[i]
        close_i = gun["close"]
        atr_i = gun["atr14"] if pd.notna(gun["atr14"]) else None

        effective_stop = trail_stop if trail_stop is not None else stop
        stopped = (close_i <= effective_stop) if direction == "LONG" else (close_i >= effective_stop)
        if stopped:
            if trail_stop is None:
                return "LOSS", -1.0
            r_kazanilan = ((close_i - entry) / stop_dist) if direction == "LONG" \
                else ((entry - close_i) / stop_dist)
            # yarisi 1.5R'de kilitlendi, yarisi trail/breakeven seviyesinde kapandi
            blend_r = 0.5 * KANIT_PARTIAL_TP_R_MULT + 0.5 * r_kazanilan
            return ("WIN" if blend_r > 0 else "LOSS"), round(blend_r, 4)

        if not partial_done:
            tp_hit = (close_i >= tp) if direction == "LONG" else (close_i <= tp)
            if tp_hit:
                partial_done = True
                trail_stop = entry  # breakeven'e cekildi
                continue

        if partial_done and atr_i:
            if direction == "LONG":
                candidate = close_i - atr_i * KANIT_TRAIL_ATR_MULT
                improved = trail_stop is None or candidate > trail_stop * (1 + KANIT_TRAIL_MIN_MOVE_PCT / 100)
            else:
                candidate = close_i + atr_i * KANIT_TRAIL_ATR_MULT
                improved = trail_stop is None or candidate < trail_stop * (1 - KANIT_TRAIL_MIN_MOVE_PCT / 100)
            if improved:
                trail_stop = candidate

    if partial_done:
        # pencere kapandi, hala acikti - parsiyel kilitli kar en azindan gercek
        return "WIN", round(0.5 * KANIT_PARTIAL_TP_R_MULT, 4)
    return "TIMEOUT", None


def _kanit_us_checkpoint_sonuc(df: pd.DataFrame, idx: int, direction: str):
    """US_SWING_CHECKPOINTS ile BİREBİN AYNI - herhangi bir checkpoint
    tutarsa isabet, 10 günde hiçbiri tutmazsa kayıp sayılır."""
    entry = df.iloc[idx]["close"]
    for gun_sayisi, etiket, hedef_pct in KANIT_US_CHECKPOINTS:
        i = idx + gun_sayisi
        if i >= len(df):
            return "TIMEOUT", None
        gun = df.iloc[i]
        if direction == "LONG":
            hedef_fiyat = entry * (1 + hedef_pct / 100)
            if gun["high"] >= hedef_fiyat:
                return "WIN", hedef_pct
        else:
            hedef_fiyat = entry * (1 - hedef_pct / 100)
            if gun["low"] <= hedef_fiyat:
                return "WIN", hedef_pct
    return "LOSS", -1.0


def kanit_dogrula_bist() -> dict:
    import yfinance as yf
    tum_sonuclar = {"Fitil+RSI+Hacim": [], "Sadece RSI": []}
    for n_i, ticker in enumerate(BIST_TICKERS, 1):
        try:
            print(f"[Kanıt Doğrulama BIST {n_i}/{len(BIST_TICKERS)}] {ticker}...", flush=True)
            df = yf.Ticker(ticker).history(period="2y", interval="1d")
            if df is None or df.empty or len(df) < 60:
                continue
            df = df.rename(columns={"Open": "open", "High": "high", "Low": "low",
                                     "Close": "close", "Volume": "volume"})
            df = df[["open", "high", "low", "close", "volume"]].copy().reset_index(drop=True)
            df = _kanit_compute_indicators(df)

            for idx in range(25, len(df) - 1):
                row = df.iloc[idx]
                for isim, fn in [("Fitil+RSI+Hacim", _kanit_check_exhaustion),
                                  ("Sadece RSI", _kanit_check_rsi_only)]:
                    yon = fn(row)
                    if yon is None:
                        continue
                    sonuc = _kanit_bist_rr_sonuc(df, idx, yon)
                    if sonuc is None:
                        continue
                    durum, r = sonuc
                    tum_sonuclar[isim].append((durum, r))
        except Exception as e:
            print(f"[Kanıt Doğrulama BIST] {ticker} hata: {e}", flush=True)
        time.sleep(1.0)
    return tum_sonuclar


def kanit_dogrula_us() -> dict:
    """2026-08-18 GÜNCELLEME: artık ATR Kırılımı ve Hacim Z-Skor'un AYNI
    GÜN AYNI YÖNDE birlikte tetiklendiği durumları da AYRI bir kategori
    olarak izliyor ("Örtüşme/Confluence") - iki bağımsız kanıtlanmış
    sinyal aynı anda aynı yönü işaret ediyorsa, tek başına birinden daha
    güçlü mü, o soruya cevap için."""
    import yfinance as yf
    tum_sonuclar = {
        "ATR Kırılımı x2.0": [], "Hacim Z-Skor": [],
        "[Örtüşme] ATR + Hacim Z-Skor (aynı yön)": [],
    }
    for n_i, ticker in enumerate(US_INSIDER_TICKERS, 1):
        try:
            print(f"[Kanıt Doğrulama US {n_i}/{len(US_INSIDER_TICKERS)}] {ticker}...", flush=True)
            df = yf.Ticker(ticker).history(period="2y", interval="1d")
            if df is None or df.empty or len(df) < 60:
                continue
            df = df.rename(columns={"Open": "open", "High": "high", "Low": "low",
                                     "Close": "close", "Volume": "volume"})
            df = df[["open", "high", "low", "close", "volume"]].copy().reset_index(drop=True)
            df = _kanit_compute_indicators(df)

            for idx in range(25, len(df) - 11):
                row = df.iloc[idx]
                prev_close = df.iloc[idx - 1]["close"]
                yon_atr = _kanit_check_us_atr_breakout(row, prev_close)
                yon_hacim = _kanit_check_us_volume_zscore(row)

                for isim, yon in [("ATR Kırılımı x2.0", yon_atr), ("Hacim Z-Skor", yon_hacim)]:
                    if yon is None:
                        continue
                    durum, r = _kanit_us_checkpoint_sonuc(df, idx, yon)
                    tum_sonuclar[isim].append((durum, r))

                if yon_atr is not None and yon_atr == yon_hacim:
                    durum, r = _kanit_us_checkpoint_sonuc(df, idx, yon_atr)
                    tum_sonuclar["[Örtüşme] ATR + Hacim Z-Skor (aynı yön)"].append((durum, r))
        except Exception as e:
            print(f"[Kanıt Doğrulama US] {ticker} hata: {e}", flush=True)
        time.sleep(1.0)
    return tum_sonuclar


def _kanit_ozet_tablosu(tumu: dict) -> list:
    satirlar = []
    for strateji, kayitlar in tumu.items():
        win = sum(1 for d, _ in kayitlar if d == "WIN")
        loss = sum(1 for d, _ in kayitlar if d == "LOSS")
        timeout = sum(1 for d, _ in kayitlar if d == "TIMEOUT")
        karar_verilen = win + loss
        kazanma_orani = round(win / karar_verilen * 100, 2) if karar_verilen else None
        p = _binom_p(win, karar_verilen) if karar_verilen >= KANIT_MIN_N else None
        r_degerleri = [r for d, r in kayitlar if r is not None]
        ort_r = round(float(np.mean(r_degerleri)), 4) if r_degerleri else None
        satirlar.append({
            "strateji": strateji, "toplam_sinyal": len(kayitlar),
            "win": win, "loss": loss, "timeout": timeout,
            "kazanma_orani_pct": kazanma_orani, "binom_p": p, "ort_R": ort_r,
        })
    return satirlar


def kanit_dogrulama_calistir() -> tuple:
    """Tüm akışı çalıştırır: BIST + ABD doğrulaması, özet tabloyu CSV'ye
    yazar. Döner: (dosya_yolu, özet_dict) ya da (None, hata_mesajı)."""
    bist_sonuc = kanit_dogrula_bist()
    us_sonuc = kanit_dogrula_us()
    tumu = {**bist_sonuc, **us_sonuc}
    satirlar = _kanit_ozet_tablosu(tumu)

    if not satirlar:
        return None, "Hiçbir strateji için veri üretilemedi."

    tablo = pd.DataFrame(satirlar)
    dosya_yolu = _data_path("kanit_dogrulama_sonucu.csv")
    tablo.to_csv(dosya_yolu, index=False, encoding="utf-8-sig")
    return dosya_yolu, {"satirlar": satirlar}


# =============================================================================
# ABD → BIST TRANSFER TESTİ — 2026-08-18
# =============================================================================
# GEREKÇE: v9'daki (gösterge turnuvası) ABD-transfer testi basit "ertesi
# gün yönü tuttu mu" yöntemiyle yapılmıştı - kaba bir ölçüm. v13'te BIST
# için canlı sistemin GERÇEK çıkış mantığını (1.5R kısmi TP + breakeven +
# ATR trailing) doğru şekilde inşa ettik. Bu, aynı testi doğru yöntemle
# tekrarlıyor: ABD'de kanıtlanmış giriş koşulları (ATR Kırılımı x2.0,
# Hacim Z-Skor), BIST hisselerinde tetiklenip BIST'in gerçek çıkışıyla
# sonuçlandırılıyor - "bu ABD sinyalini BIST'e taşısak ne olurdu" sorusuna
# en dürüst cevap.

def kanit_dogrula_abd_stratejileri_bistte() -> dict:
    import yfinance as yf
    tum_sonuclar = {"[ABD→BIST] ATR Kırılımı x2.0": [], "[ABD→BIST] Hacim Z-Skor": []}
    for n_i, ticker in enumerate(BIST_TICKERS, 1):
        try:
            print(f"[ABD→BIST Transfer {n_i}/{len(BIST_TICKERS)}] {ticker}...", flush=True)
            df = yf.Ticker(ticker).history(period="2y", interval="1d")
            if df is None or df.empty or len(df) < 60:
                continue
            df = df.rename(columns={"Open": "open", "High": "high", "Low": "low",
                                     "Close": "close", "Volume": "volume"})
            df = df[["open", "high", "low", "close", "volume"]].copy().reset_index(drop=True)
            df = _kanit_compute_indicators(df)

            for idx in range(25, len(df) - 1):
                row = df.iloc[idx]
                prev_close = df.iloc[idx - 1]["close"]
                for isim, fn_sonuc in [
                    ("[ABD→BIST] ATR Kırılımı x2.0", lambda: _kanit_check_us_atr_breakout(row, prev_close)),
                    ("[ABD→BIST] Hacim Z-Skor", lambda: _kanit_check_us_volume_zscore(row)),
                ]:
                    yon = fn_sonuc()
                    if yon is None:
                        continue
                    sonuc = _kanit_bist_rr_sonuc(df, idx, yon)
                    if sonuc is None:
                        continue
                    durum, r = sonuc
                    tum_sonuclar[isim].append((durum, r))
        except Exception as e:
            print(f"[ABD→BIST Transfer] {ticker} hata: {e}", flush=True)
        time.sleep(1.0)
    return tum_sonuclar


def kanit_dogrulama_transfer_calistir() -> tuple:
    tumu = kanit_dogrula_abd_stratejileri_bistte()
    satirlar = _kanit_ozet_tablosu(tumu)
    if not satirlar:
        return None, "Hiçbir strateji için veri üretilemedi."
    tablo = pd.DataFrame(satirlar)
    dosya_yolu = _data_path("kanit_dogrulama_transfer_sonucu.csv")
    tablo.to_csv(dosya_yolu, index=False, encoding="utf-8-sig")
    return dosya_yolu, {"satirlar": satirlar}



    """Kullanıcının istediği DOĞRULAMA TESTİ: verilen tarihin (örn.
    '2026-07-04') kapanışındaki gösterge değerleriyle Gemini'den BIST
    hisseleri için LONG/SHORT kararı alır, ERTESİ GÜNÜN GERÇEK sonucuyla
    karşılaştırır. Otomatik döngüde YOK - /hesap_test KOMUTUYLA elle
    tetikleniyor, sonuçlar kalıcı geçmişe (HESAP_TEST_HISTORY_FILE)
    kaydediliyor ki zamanla "kaç tanesi doğru çıktı" birikip izlenebilsin."""
    import yfinance as yf
    hedef_tarih = pd.Timestamp(tarih_str)
    orijinal_tarih_str = tarih_str
    duzeltme_notu = ""

    if hedef_tarih.weekday() >= 5:  # 5=Cumartesi, 6=Pazar
        gun_farki = hedef_tarih.weekday() - 4
        hedef_tarih = hedef_tarih - pd.Timedelta(days=gun_farki)
        duzeltme_notu = (f"⚠️ {orijinal_tarih_str} bir {['Pazartesi','Salı','Çarşamba','Perşembe','Cuma','Cumartesi','Pazar'][pd.Timestamp(orijinal_tarih_str).weekday()]} "
                         f"— BIST o gün kapalı. En yakın önceki işlem gününe "
                         f"({hedef_tarih.date()}) kaydırıldı.\n\n")

    index_df = yf.Ticker("XU100.IS").history(period="2y", interval="1d")
    index_pct = None
    if index_df is not None and not index_df.empty:
        index_df.index = pd.to_datetime(index_df.index).tz_localize(None)
        index_pct = (index_df["Close"] - index_df["Close"].shift(1)) / index_df["Close"].shift(1) * 100

        # 2026-08-17: HAFTASONU DISINDA da BIST kapali olabilir (resmi tatiller,
        # ornegin 19 Mayis) - hafta ici kontrolu bunlari YAKALAMAZ. Bunun yerine
        # XU100 endeksinin KENDI VERISINI (zaten cekilmis) "gercekten islem
        # gunu muydu" sorusunun otoritesi olarak kullaniyoruz - herhangi bir
        # tatil takvimi ELLE TANIMLAMAYA gerek kalmadan TUM tatilleri
        # genel olarak yakalar.
        if hedef_tarih not in index_df.index and not duzeltme_notu:
            onceki_gunler = index_df.index[index_df.index <= hedef_tarih]
            if len(onceki_gunler) > 0:
                yeni_tarih = onceki_gunler.max()
                if yeni_tarih != hedef_tarih:
                    duzeltme_notu = (f"⚠️ {orijinal_tarih_str} tarihinde BIST kapalıydı "
                                     f"(muhtemelen resmi tatil). En yakın önceki işlem "
                                     f"gününe ({yeni_tarih.date()}) kaydırıldı.\n\n")
                    hedef_tarih = yeni_tarih

    ticker_gostergeleri = {}
    ticker_df_cache = {}
    for ticker in BIST_TICKERS:
        try:
            df = yf.Ticker(ticker).history(period="2y", interval="1d")
            if df is None or df.empty or len(df) < 100:
                continue
            df = df.rename(columns={"Open": "open", "High": "high", "Low": "low",
                                     "Close": "close", "Volume": "volume"})
            df = df[["open", "high", "low", "close", "volume"]].copy()
            df.index = pd.to_datetime(df.index).tz_localize(None)
            df = compute_features(df, index_pct)
            gostergeler = _gostergeleri_hesapla(df, hedef_tarih)
            if gostergeler is None:
                continue
            # SIZINTI KONTROLU: hedef_tarih'ten SONRAKI hicbir satir kullanilmiyor -
            # sadece o tarihe kadar hesaplanmis gosterge degerleri gonderiliyor.
            ticker_gostergeleri[ticker] = gostergeler
            ticker_df_cache[ticker] = df
        except Exception as e:
            print(f"[ARGE] {ticker} backtest veri hatası: {e}", flush=True)
        time.sleep(0.2)

    if not ticker_gostergeleri:
        return f"{duzeltme_notu}🧮 {hedef_tarih.date()} için hiçbir hisseden veri alınamadı."

    # 2026-08-16: Gemini'ye SORULMUYOR - karar TAMAMEN KOD İÇİNDE, ağırlıklı
    # toplam hesabıyla üretiliyor. Eşik/filtre YOK - HER hisse için mutlaka
    # bir yön (LONG/SHORT) üretilir, "nötr, atlandı" diye eleme yapılmaz.
    kararlar = {}
    for ticker, gostergeler in ticker_gostergeleri.items():
        sonuc = hesapla_yon_kod_ile(gostergeler)
        kararlar[ticker] = {
            "yon": sonuc["yon"], "guven": min(100, abs(sonuc["skor"]) * 15),
            "gerekce": ", ".join(sonuc["detaylar"].keys()), "skor": sonuc["skor"],
        }
    print(f"[ARGE] Kod-tabanlı karar: {len(kararlar)} hissenin hepsi için "
          f"hesaplandı (filtre yok).", flush=True)

    sonuclar = []
    ilk_ornek_notu = ""
    for ticker, karar in kararlar.items():
        df = ticker_df_cache.get(ticker)
        if df is None or hedef_tarih not in df.index:
            continue
        idx = df.index.get_loc(hedef_tarih)
        if idx + 1 >= len(df):
            continue  # ertesi gun verisi henuz yok (gelecek tarih)
        entry_tarihi = df.index[idx]
        ertesi_tarihi = df.index[idx + 1]
        if ertesi_tarihi <= entry_tarihi:
            print(f"[ARGE] UYARI: {ticker} için 'ertesi gün' tarihi ({ertesi_tarihi.date()}) "
                  f"giriş tarihinden ({entry_tarihi.date()}) sonra değil - atlandı", flush=True)
            continue
        entry = df.iloc[idx]["close"]
        ertesi_kapanis = df.iloc[idx + 1]["close"]
        if not ilk_ornek_notu:
            ilk_ornek_notu = (f"🔍 Örnek (ilk hisse, {ticker}): giriş tarihi={entry_tarihi.date()} "
                              f"(kapanış={entry:.2f}), ertesi gün tarihi={ertesi_tarihi.date()} "
                              f"(kapanış={ertesi_kapanis:.2f})\n\n")
        getiri = (ertesi_kapanis - entry) / entry * 100
        dogru = (karar["yon"] == "LONG" and getiri > 0) or (karar["yon"] == "SHORT" and getiri < 0)
        sonuclar.append({
            "test_tarihi": str(hedef_tarih.date()), "ticker": ticker, "yon": karar["yon"],
            "guven": karar["guven"], "gerekce": karar.get("gerekce", ""),
            "ertesi_gun_getiri_pct": round(getiri, 3), "dogru_mu": 1 if dogru else 0,
        })

    if not sonuclar:
        return (f"{duzeltme_notu}🧮 {hedef_tarih.date()}: {len(kararlar)} karar alındı ama hiçbiri için "
                f"ertesi günün gerçek verisi bulunamadı (tarih çok yeni olabilir).")

    # BAYAT VERİ KORUMASI (2026-08-17): Yahoo Finance bazı tarihlerde bayat/
    # tekrarlanan veri döndürebiliyor (giriş ve "ertesi gün" kapanışı BİREBİR
    # AYNI) - bu gerçek bir piyasa sonucu DEĞİL, veri kaynağının sorunu.
    # Böyle günler GERCEK sonuc gibi kaydedilirse /hesap_rapor'un istatistiği
    # bozulur (sahte "hep yanlış" günler). Şüpheli oranda sıfır-getiri varsa
    # (>%50) TÜM turu geçersiz say, HİÇBİR ŞEY kaydetme.
    sifir_getiri_sayisi = sum(1 for s in sonuclar if abs(s["ertesi_gun_getiri_pct"]) < 0.001)
    if sifir_getiri_sayisi / len(sonuclar) > 0.5:
        return (
            f"{duzeltme_notu}{ilk_ornek_notu}"
            f"⚠️ [BAYAT VERİ ŞÜPHESİ] {hedef_tarih.date()}: {sifir_getiri_sayisi}/{len(sonuclar)} "
            f"hissede giriş ve ertesi gün kapanışı BİREBİR AYNI çıktı — bu gerçek bir "
            f"piyasa sonucu olamaz, Yahoo Finance'in bu tarih için bayat/tekrarlanan "
            f"veri döndürdüğüne işaret ediyor. Bu tur GEÇERSİZ SAYILDI, hiçbir şey "
            f"geçmişe kaydedilmedi.\n\n"
            f"Öneri: bir gün öncesi ya da sonrasını dene (örn. {(hedef_tarih - pd.Timedelta(days=1)).date()} "
            f"ya da {(hedef_tarih + pd.Timedelta(days=1)).date()})."
        )

    exists = os.path.exists(HESAP_TEST_HISTORY_FILE)
    with open(HESAP_TEST_HISTORY_FILE, "a", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=HESAP_TEST_FIELDS)
        if not exists:
            w.writeheader()
        w.writerows(sonuclar)

    n = len(sonuclar)
    dogru_n = sum(s["dogru_mu"] for s in sonuclar)
    detay = "\n".join(
        f"  {'✅' if s['dogru_mu'] else '❌'} {s['ticker']}: {s['yon']} → gerçek {s['ertesi_gun_getiri_pct']:+.2f}%"
        for s in sonuclar
    )
    return (
        f"{duzeltme_notu}{ilk_ornek_notu}"
        f"🧮 [HESAP MAKİNESİ TESTİ] {hedef_tarih.date()} kapanışı → ertesi gün\n\n"
        f"Sonuç: {dogru_n}/{n} doğru (%{dogru_n/n*100:.1f})\n\n{detay}\n\n"
        f"⚠️ Bu, geçmişten 'öğrenmiş' bir model DEĞİL — her göstergeye "
        f"standart bir teknik analiz kuralı uygulanıp oylanıyor, TAMAMEN "
        f"KOD İÇİNDE (Gemini'ye sorulmuyor, kotasız, deterministik). "
        f"Bu sonuç kalıcı geçmişe kaydedildi, /hesap_rapor ile birikimi "
        f"görebilirsin.\n\n"
        f"🔖 Kod sürümü: {ARGE_KOD_SURUMU}"
    )


def build_hesap_rapor() -> str:
    """/hesap_rapor - hesap_makinesi_backtest ile şimdiye kadar biriken
    TÜM test sonuçlarının toplamı - tek bir tarih değil, genel doğruluk.
    LONG/SHORT ayrı gösteriliyor - Gemini'nin bir yöne yaslanma (bias)
    eğilimi olup olmadığını görmek için (ilk testte %72 SHORT çağrısı
    bulunmuştu, bu potansiyel bir sorun işareti)."""
    if not os.path.exists(HESAP_TEST_HISTORY_FILE):
        return "🧮 [HESAP MAKİNESİ RAPORU] Henüz hiç test yapılmadı. /hesap_test TARİH ile başlat."
    with open(HESAP_TEST_HISTORY_FILE, newline="", encoding="utf-8") as f:
        satirlar = list(csv.DictReader(f))
    if not satirlar:
        return "🧮 [HESAP MAKİNESİ RAPORU] Henüz hiç test yapılmadı."
    n = len(satirlar)
    dogru_n = sum(int(s["dogru_mu"]) for s in satirlar)
    tarihler = sorted(set(s["test_tarihi"] for s in satirlar))

    uzun = [s for s in satirlar if s["yon"] == "LONG"]
    kisa = [s for s in satirlar if s["yon"] == "SHORT"]
    satir_ekle = []
    if uzun:
        dn = sum(int(s["dogru_mu"]) for s in uzun)
        satir_ekle.append(f"  LONG: {len(uzun)} çağrı, %{dn/len(uzun)*100:.1f} doğru")
    if kisa:
        dn = sum(int(s["dogru_mu"]) for s in kisa)
        satir_ekle.append(f"  SHORT: {len(kisa)} çağrı, %{dn/len(kisa)*100:.1f} doğru")
    yon_dagilimi = f"  Dağılım: %{len(uzun)/n*100:.0f} LONG / %{len(kisa)/n*100:.0f} SHORT"

    return (
        f"🧮 [HESAP MAKİNESİ RAPORU]\n"
        f"Toplam test edilen karar: {n} ({len(tarihler)} farklı tarih)\n"
        f"Genel doğruluk: %{dogru_n/n*100:.1f} ({dogru_n}/{n})\n\n"
        f"{yon_dagilimi}\n" + "\n".join(satir_ekle) + "\n\n"
        f"Test edilen tarihler: {', '.join(tarihler)}"
    )


MAX_SERI_TARIH = int(os.environ.get("MAX_SERI_TARIH", "5"))


def hesap_makinesi_backtest_seri(tarihler: list):
    """Birden fazla tarihi ARKA ARKAYA test eder - her tarih kendi Gemini
    isteğini harcıyor (kota ÖNEMLİ, bu yüzden MAX_SERI_TARIH ile
    sınırlandırıldı). Her tarihin sonucu ayrı Telegram mesajı olarak
    gönderilir (tek mesaja sığdırmaya çalışmak Telegram'ın karakter
    sınırını aşardı), en sonda birikmiş TOPLAM rapor da eklenir."""
    if len(tarihler) > MAX_SERI_TARIH:
        send_telegram_message(f"⚠️ En fazla {MAX_SERI_TARIH} tarih birden test edilebilir "
                              f"(Gemini kotasını korumak için) - {len(tarihler)} tarih verildi, "
                              f"ilk {MAX_SERI_TARIH} tanesi kullanılacak.")
        tarihler = tarihler[:MAX_SERI_TARIH]

    for t in tarihler:
        try:
            send_telegram_message(hesap_makinesi_backtest(t))
        except Exception as e:
            send_telegram_message(f"🧮 {t} test hatası: {e}")
        time.sleep(1)

    send_telegram_message("📊 Seri test bitti — toplam birikmiş sonuç:\n\n" + build_hesap_rapor())


def ask_gemini_for_hypothesis_batch() -> list:
    """TEK istekte BATCH_SIZE (15) kadar ozellik-kombinasyonu birden ister -
    kota "kac soru sordun" uzerinden isledigi icin, 15 fikri 1 istekte
    almak 15 istekte almaktan 15 KAT az kota harcar."""
    gecmis = _read_history()
    gecmis_ozet = "\n".join(
        f"- {r['isim']} ({r['yon']}): {r['ozellikler_json']} -> "
        f"{'ONAYLANDI' if r['onayli_mi'] == '1' else r['asama'] + ' aşamasında elendi'}"
        for r in gecmis[-30:]
    ) or "(henüz hiç deneme yok)"

    prompt = f"""Sen bir kantitatif finans araştırmacısısın. Şu spesifik stratejiyi
geliştirmemize yardım ediyorsun: BIST/ABD hisselerinde, PİYASA KAPANIŞINA
YAKIN (~17:40-17:50 İstanbul) pozisyon açılıyor, ERTESİ GÜN piyasa fiyatı
GİRİŞ FİYATININ +%{TARGET_PCT:.1f} ÜZERİNE çıkınca satılıyor. Görevin: küçük
bir makine öğrenmesi modeli için {BATCH_SIZE} FARKLI özellik kombinasyonu
seçmek - bu model, kapanış anındaki verilerden bu +%{TARGET_PCT:.1f} hedefine
ertesi gün ulaşılıp ulaşılmayacağını tahmin edecek.

SADECE şu özelliklerden her kombinasyonda 2-6 tanesini seçebilirsin (kod/
hiperparametre YAZMA - sadece hangi özellikler kullanılsın onu seç):
{', '.join(FEATURE_LIBRARY)}

(Not: kapanis_hacim_orani, kapanis_momentum_pct, kapanis_araligi_pct,
kapanis_yon_orani — günün SON ~1 saatine bakan, gerçek emir defteri
YERİNE geçen ucuz bir proxy: son saatin hacim oranı, momentumu, aralığı,
yukarı-kapanan bar oranı.)

Şimdiye kadar denenen özellik kombinasyonları ve sonuçları:
{gecmis_ozet}

Daha önce denenmemiş, BİRBİRİNDEN FARKLI {BATCH_SIZE} kombinasyon öner.
SADECE şu JSON DİZİ formatında cevap ver, başka hiçbir metin ekleme:
[{{"isim": "kisa_isim", "yon": "LONG veya SHORT", "kullanilacak_ozellikler": ["ozellik1", "ozellik2", ...], "gerekce": "kısa açıklama"}}, ...]"""

    liste = _call_gemini(prompt)
    if not isinstance(liste, list):
        print(f"[ARGE] Gemini'den beklenen JSON dizisi gelmedi.", flush=True)
        return []

    gecerliler = []
    for h in liste:
        gecerli, hata = validate_ai_hypothesis(h)
        if gecerli:
            gecerliler.append(h)
        else:
            print(f"[ARGE] Toplu hipotezde geçersiz bir tane elendi: {hata}", flush=True)
    print(f"[ARGE] Toplu istek: {len(liste)} hipotez geldi, {len(gecerliler)} geçerli.", flush=True)
    return gecerliler


def _read_queue() -> list:
    if not os.path.exists(QUEUE_FILE):
        return []
    try:
        with open(QUEUE_FILE, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return []


def _write_queue(queue: list):
    with open(QUEUE_FILE, "w", encoding="utf-8") as f:
        json.dump(queue, f, ensure_ascii=False)


def get_next_hypothesis():
    """Kuyruk bossa YENI bir toplu istek atar (kota tuketen tek an burasi),
    doluysa kuyruktan bir tane cikarip DONER - kota harcamadan.
    Her yeni toplu istekte, kullanicinin fikrini de ekliyoruz: Gemini'nin
    sectigi kucuk alt kumelerin YANI SIRA, TUM kutuphaneyi (RSI, MACD,
    VWAP, hacim, kapanis-penceresi - hepsi) BIRDEN kullanan iki hipotez
    (LONG+SHORT) da kuyruga ekleniyor - "tek pencere degil, hepsini
    birlikte hesapla" fikrinin dogrudan karsiligi. Bu ikisi Gemini'den
    gelmedigi (elle, guvenilir sekilde tanimlandigi) icin 2-6 ozellik
    sinirina tabi degil - validate_ai_hypothesis'i BYPASS ediyor, ÇÜNKÜ
    bu sinirlama sadece Gemini kaynakli/kontrolsuz girdiler icin var."""
    q = _read_queue()
    if not q:
        q = ask_gemini_for_hypothesis_batch()
        for yon in ("LONG", "SHORT"):
            q.append({
                "isim": f"tum_gostergeler_birlikte_{yon.lower()}", "yon": yon,
                "kullanilacak_ozellikler": list(FEATURE_LIBRARY),
                "gerekce": "Kullanıcının fikri: hiçbir tek gösterge/pencere yeterli "
                           "değil - RSI, MACD, VWAP, hacim, kapanış-penceresi ve "
                           "diğer tüm göstergeler BİRLİKTE modele veriliyor, "
                           "XGBoost hangi kombinasyonun işe yaradığını kendi öğreniyor.",
            })
        if not q:
            return None
    h = q.pop(0)
    _write_queue(q)
    print(f"[ARGE] Kuyrukta kalan: {len(q)}", flush=True)
    return h


# =============================================================================
# ANA DÖNGÜ
# =============================================================================

def _stage_line(etiket: str, s: dict) -> str:
    return (f"{etiket}: {s['n']} örnek | kazanma %{s['kazanma_orani']:.1f} "
            f"(+%{TARGET_PCT:.1f} hedefine ulaşan) | ort. potansiyel {s['ort']:+.2f}% | "
            f"en kötü {s['en_kotu']:+.2f}%")


def _process_hypothesis_result(isim: str, yon: str, features: list, gerekce: str,
                                s_egitim, s_dogrulama, s_sinav, asama: str, onayli: bool):
    ozellikler_json = json.dumps(features, ensure_ascii=False)
    _append_history({
        "tarih": datetime.now(timezone.utc).date().isoformat(),
        "isim": isim, "yon": yon, "ozellikler_json": ozellikler_json, "gerekce": gerekce,
        "egitim_n": s_egitim["n"] if s_egitim else "",
        "egitim_kazanma": s_egitim["kazanma_orani"] if s_egitim else "",
        "dogrulama_n": s_dogrulama["n"] if s_dogrulama else "",
        "dogrulama_kazanma": s_dogrulama["kazanma_orani"] if s_dogrulama else "",
        "sinav_n": s_sinav["n"] if s_sinav else "",
        "sinav_kazanma": s_sinav["kazanma_orani"] if s_sinav else "",
        "onayli_mi": 1 if onayli else 0, "asama": asama,
    })

    print(f"[ARGE] Sonuç: {asama} | "
          f"eğitim={s_egitim['kazanma_orani'] if s_egitim else None} "
          f"doğrulama={s_dogrulama['kazanma_orani'] if s_dogrulama else None} "
          f"sınav={s_sinav['kazanma_orani'] if s_sinav else None}", flush=True)

    if not onayli:
        print(f"[ARGE] Hipotez '{asama}' aşamasında elendi, sessizce kaydedildi.", flush=True)
        return

    gecmis_simdi = _read_history()
    toplam_denenen = len(gecmis_simdi)
    toplam_onayli = sum(1 for r in gecmis_simdi if r["onayli_mi"] == "1")
    send_telegram_message(
        f"🎉 [AR-GE — GECE RADARI İÇİN ONAYLANMIŞ HİPOTEZ] '{isim}' ({yon})\n\n"
        f"Gerekçe: {gerekce}\n"
        f"Kullanılan özellikler: {ozellikler_json}\n\n"
        f"📊 {_stage_line('Eğitim', s_egitim)}\n"
        f"📊 {_stage_line('Doğrulama', s_dogrulama)}\n"
        f"🔒 {_stage_line('HİÇ GÖRÜLMEMİŞ SINAV', s_sinav)}\n\n"
        f"(Hedef: kapanışta giriş, ertesi gün +%{TARGET_PCT:.1f}+%{TRANSACTION_COST_PCT:.2f} "
        f"komisyon = %{EFFECTIVE_TARGET_PCT:.2f} net hedefine ulaşma. Onay çıtası: "
        f"kazanma oranı ≥%{MIN_WIN_RATE_PCT:.0f}.)\n\n"
        f"Bu hipotez HİÇ görülmemiş veride de bu oranı tuttu — şansla "
        f"açıklanması daha zor. Yine de kesin kanıt değil, gece radarına "
        f"bağlamadan önce ayrıca değerlendirilmeli. Hiçbir sisteme "
        f"otomatik bağlanmadı.\n\n"
        f"📈 Bağlam: şimdiye kadar {toplam_denenen} hipotez denendi, "
        f"bu {toplam_onayli}. onaylanan.\n\n"
        f"🔁 Şimdi YENİDEN-DOĞRULAMA listesine eklendi — her "
        f"{RECONFIRM_INTERVAL_HOURS} saatte bir güncel veriyle tekrar "
        f"test edilecek. {RECONFIRM_STREAK_REQUIRED} kez üst üste "
        f"geçerse 'KESİN GÜVENİLİR' ilan edilecek."
    )
    _register_for_reconfirmation(isim, yon, features, gerekce)


def run_ai_research_cycle():
    """SADECE bu kol var artik - Gemini'nin sectigi ozelliklerle kucuk bir
    XGBoost modeli egitilip, kullanicinin GERCEK stratejisine (kapanista
    giris, ertesi gun +%2 hedefi) gore test edilir."""
    print(f"[ARGE] Araştırma turu başlıyor (gece radarı / AI)...", flush=True)

    h = get_next_hypothesis()
    if h is None:
        print("[ARGE] Bu tur hipotez alınamadı (kuyruk boş + toplu istek başarısız), atlanıyor.", flush=True)
        return
    features = h["kullanilacak_ozellikler"]
    print(f"[ARGE] Hipotez: {h['isim']} ({h['yon']}) - özellikler: {features}", flush=True)

    df = fetch_all_data()
    if df.empty or len(df) < MIN_TRAIN_ROWS:
        print(f"[ARGE] Yetersiz veri ({len(df)} satır), bu tur atlanıyor.", flush=True)
        return
    if _needs_closing_features(features):
        df = augment_with_closing_features(df)

    egitim, dogrulama, sinav = chronological_split(df)
    model = train_ai_model(egitim, features)
    if model is None:
        print("[ARGE] Model eğitilemedi (yetersiz/tek sınıflı veri), atlanıyor.", flush=True)
        return

    s_egitim = evaluate_ai_on_slice(model, egitim, features, h["yon"])
    asama, onayli = "eğitim", False
    s_dogrulama = s_sinav = None

    if _stage_passed(s_egitim):
        asama = "doğrulama"
        s_dogrulama = evaluate_ai_on_slice(model, dogrulama, features, h["yon"])
        if _stage_passed(s_dogrulama):
            asama = "sınav"
            model_final = train_ai_model(pd.concat([egitim, dogrulama]), features)
            s_sinav = evaluate_ai_on_slice(model_final or model, sinav, features, h["yon"])
            if _stage_passed(s_sinav):
                onayli, asama = True, "onaylandı"

    _process_hypothesis_result(h["isim"], h["yon"], features, h["gerekce"],
                                s_egitim, s_dogrulama, s_sinav, asama, onayli)


# =============================================================================
# YENİDEN-DOĞRULAMA
# =============================================================================

def _register_for_reconfirmation(isim: str, yon: str, features: list, gerekce: str):
    rows = _read_reconfirm()
    ozellikler_json = json.dumps(features, ensure_ascii=False)
    if any(r["isim"] == isim and r["ozellikler_json"] == ozellikler_json for r in rows):
        return
    rows.append({
        "isim": isim, "yon": yon, "ozellikler_json": ozellikler_json, "gerekce": gerekce,
        "seri": 0, "son_test_tarih": datetime.now(timezone.utc).isoformat(),
        "kesin_guvenilir_mi": 0, "son_sinav_kazanma": "",
    })
    _write_reconfirm(rows)


def _reconfirm_evaluate(r: dict, df: pd.DataFrame):
    egitim, dogrulama, sinav = chronological_split(df)
    yon = r["yon"]
    features = json.loads(r["ozellikler_json"])
    model = train_ai_model(egitim, features)
    s_e = evaluate_ai_on_slice(model, egitim, features, yon) if model else None
    s_s = None
    if _stage_passed(s_e):
        s_d = evaluate_ai_on_slice(model, dogrulama, features, yon)
        if _stage_passed(s_d):
            model_final = train_ai_model(pd.concat([egitim, dogrulama]), features) or model
            s_s = evaluate_ai_on_slice(model_final, sinav, features, yon)
    return s_s


def reconfirm_pending_hypotheses():
    """Her cagrida, RECONFIRM_INTERVAL_HOURS'i gecmis kayitlari GUNCEL/genislemis
    veriyle yeniden test eder. Basarili -> seri += 1 (esige ulasirsa KESIN
    GUVENILIR ilan edilir). Basarisiz -> seri = 0'a sifirlanir ve daha once
    kesinlesmisse geri cekilir."""
    rows = _read_reconfirm()
    if not rows:
        return
    now = datetime.now(timezone.utc)
    degisti = False

    def _row_needs_closing(row):
        try:
            return _needs_closing_features(json.loads(row["ozellikler_json"]))
        except Exception:
            return False

    df = None
    for r in rows:
        son_test = datetime.fromisoformat(r["son_test_tarih"])
        if (now - son_test).total_seconds() < RECONFIRM_INTERVAL_HOURS * 3600:
            continue
        if df is None:
            df = fetch_all_data()
            if df.empty or len(df) < MIN_TRAIN_ROWS:
                print("[ARGE] Yeniden-doğrulama için yetersiz veri, bu tur atlanıyor.", flush=True)
                return
            due_now = [x for x in rows if (now - datetime.fromisoformat(x["son_test_tarih"])).total_seconds() >= RECONFIRM_INTERVAL_HOURS * 3600]
            if any(_row_needs_closing(x) for x in due_now):
                df = augment_with_closing_features(df)

        s_sinav = _reconfirm_evaluate(r, df)
        gecti = _stage_passed(s_sinav)

        r["son_test_tarih"] = now.isoformat()
        r["son_sinav_kazanma"] = s_sinav["kazanma_orani"] if s_sinav else ""
        degisti = True

        onceki_kesin = r["kesin_guvenilir_mi"] in ("1", 1, "True", True)
        if gecti:
            r["seri"] = str(int(r["seri"]) + 1)
            print(f"[ARGE] Yeniden-doğrulama: '{r['isim']}' geçti, seri={r['seri']}", flush=True)
            if int(r["seri"]) >= RECONFIRM_STREAK_REQUIRED and not onceki_kesin:
                r["kesin_guvenilir_mi"] = "1"
                send_telegram_message(
                    f"🏆 [AR-GE — GECE RADARI İÇİN KESİN GÜVENİLİR] '{r['isim']}' ({r['yon']})\n\n"
                    f"{RECONFIRM_STREAK_REQUIRED} kez üst üste, her seferinde "
                    f"GÜNCEL veriyle, kazanma oranı ≥%{MIN_WIN_RATE_PCT:.0f} "
                    f"şartını geçti (son sınav: %{r['son_sinav_kazanma']}).\n"
                    f"Özellikler: {r['ozellikler_json']}\n\n"
                    f"Bu artık tek seferlik şans olma ihtimali düşük bir "
                    f"bulgu. Yine de gece radarına bağlamadan önce ayrıca "
                    f"değerlendirilmeli — hiçbir sisteme otomatik bağlanmadı."
                )
        else:
            if onceki_kesin:
                send_telegram_message(
                    f"⚠️ [AR-GE — GERİ ÇEKİLDİ] '{r['isim']}' daha önce "
                    f"'kesin güvenilir' ilan edilmişti, ama bu yeniden-"
                    f"doğrulama turunda başarısız oldu. Seri sıfırlandı — "
                    f"aslında sanıldığı kadar tutarlı değilmiş."
                )
            r["seri"] = "0"
            r["kesin_guvenilir_mi"] = "0"
            print(f"[ARGE] Yeniden-doğrulama: '{r['isim']}' başarısız, seri sıfırlandı", flush=True)

    if degisti:
        _write_reconfirm(rows)


def maybe_run_research():
    """RESEARCH_COOLDOWN_MINUTES (varsayilan 20 dk) araligiyla tekrar
    calisir - Gemini kotasi artik toplu istek sayesinde sorun degil,
    bekleme sadece Yahoo'yu yormamak icin."""
    global _last_run_time
    if not ARGE_BOTU_ENABLED or not _ARGE_AVAILABLE:
        return
    now = datetime.now(timezone.utc)
    if _last_run_time is not None and (now - _last_run_time).total_seconds() < RESEARCH_COOLDOWN_MINUTES * 60:
        return
    _last_run_time = now
    try:
        run_ai_research_cycle()
        reconfirm_pending_hypotheses()
        check_hesap_makinesi_sonuclari()
    except Exception as e:
        print(f"[ARGE] Döngü hatası: {e}", flush=True)


# =============================================================================
# PYKAP BAĞLANTI TESTİ — 2026-08-19
# =============================================================================
# GEREKÇE: KAP'ın (Kamuyu Aydınlatma Platformu) "Pay Alım Satım Bildirimi"
# (SEC Form 4'ün BIST karşılığı - içeriden alım/satım) geçmişe dönük,
# ücretsiz, konu-filtreli arşivi olduğu doğrulandı. `pykap` adlı açık
# kaynak (MIT) kütüphane tam bunu sağlıyor GİBİ görünüyor - AMA en son
# Aralık 2022'de güncellenmiş, KAP'ın arka planı o zamandan beri
# değişmiş olabilir. Büyük bir backtest kurmadan ÖNCE, küçük bir
# bağlantı testiyle gerçekten çalışıp çalışmadığını doğruluyoruz.
# NOT: pykap requirements.txt'e EKLENMELİ (kullanıcı tarafından) - burada
# sadece import edilmeye çalışılıyor, yoksa net bir hata mesajı dönüyor.

def pykap_baglanti_testi() -> str:
    """pykap kütüphanesinin hâlâ çalışıp çalışmadığını, 'Pay Alım Satım
    Bildirimi' konusunu bulup bulamadığını ve örnek bir hisse için
    geçmişe dönük bildirim çekip çekemediğini test eder. Metin raporu
    döner (CSV değil, bu sadece bir teşhis testi)."""
    satirlar = []
    try:
        import pykap
    except ImportError:
        return ("❌ pykap kurulu değil. requirements.txt'e 'pykap' satırını "
                "ekleyip yeniden deploy etmen lazım.")
    satirlar.append("✅ pykap import edildi.")

    try:
        subjects = pykap.get_disclosure_subjects("ODA")
        satirlar.append(f"✅ 'ODA' (Özel Durum Açıklaması) konu listesi çekildi: "
                         f"{len(subjects)} konu bulundu.")
        pay_konulari = [s for s in subjects
                         if "pay alım" in s.get("subject", "").lower()
                         or "pay satım" in s.get("subject", "").lower()
                         or "alım satım" in s.get("subject", "").lower()]
        if pay_konulari:
            satirlar.append(f"✅ 'Pay Alım Satım' ile eşleşen {len(pay_konulari)} "
                             f"konu bulundu:")
            for k in pay_konulari[:5]:
                satirlar.append(f"   - {k.get('subject')} (oid={k.get('subjectOid')})")
        else:
            satirlar.append("⚠️ 'Pay Alım Satım' ile eşleşen konu bulunamadı - "
                             "konu isimleri farklı formatlanmış olabilir. "
                             "İlk 15 konu adı:")
            for k in subjects[:15]:
                satirlar.append(f"   - {k.get('subject')}")
    except Exception as e:
        satirlar.append(f"❌ Konu listesi çekilirken hata: {e}")
        return "\n".join(satirlar)

    try:
        from datetime import date, timedelta
        comp = pykap.BISTCompany("THYAO")
        bugun = date.today()
        gecmis = bugun - timedelta(days=90)
        kwargs = {"fromdate": gecmis, "todate": bugun, "disclosure_type": "ODA"}
        if pay_konulari:
            kwargs["subject"] = pay_konulari[0].get("subjectOid")
        sonuc = comp.get_historical_disclosure_list(**kwargs)
        satirlar.append(f"✅ THYAO için son 90 gün bildirimi çekildi: "
                         f"{len(sonuc) if sonuc else 0} kayıt bulundu.")
        if sonuc:
            satirlar.append(f"   Örnek kayıt: {sonuc[0]}")
    except Exception as e:
        satirlar.append(f"❌ THYAO geçmiş bildirimi çekilirken hata: {e}")

    return "\n".join(satirlar)


# =============================================================================
# GOOGLE TRENDS — PERAKENDE YATIRIMCI İLGİSİ TESTİ — 2026-08-19
# =============================================================================
# GEREKÇE: Kullanıcının fikri - BIST, ABD'den farklı olarak bireysel
# yatırımcı ağırlıklı bir piyasa. Bugüne kadar hep HİSSEYİ analiz ettik,
# hiç BİREYSEL YATIRIMCININ kendisini (ilgisini/davranışını) analiz
# etmedik. Google Trends, bir hisseye olan arama ilgisinin ölçülebilir,
# ücretsiz bir vekili - akademik literatürde "perakende yatırımcı
# dikkati" (retail attention) diye bilinen, gerçek bir araştırma alanı.
# İKİ REKABETÇI HİPOTEZ test ediliyor: MOMENTUM (ilgi patlaması ->
# kalabalık peşinden gider, fiyat devam eder) vs REVERSAL (ilgi patlaması
# -> bireysel yatırımcı genelde tepede alır, fiyat geri döner).
# DÜRÜST SINIR 1: pytrends resmi olmayan bir kütüphane, Google zaman
# zaman engelliyor - bugün yfinance/pykap'ta yaşadığımız sorunlarla aynı
# aile.
# DÜRÜST SINIR 2: Google Trends 269 günden uzun aralıklar için GÜNLÜK
# değil HAFTALIK veri veriyor - bu yüzden bu test "ertesi gün" değil
# "ertesi hafta" ufkunda çalışıyor, önceki testlerden farklı bir zaman
# ölçeği.

TREND_ESIK_YUZDE = 0.20  # ust/alt %20 - digerlerinde kullandigimiz ayni kural
TREND_MIN_N = 20


def _trend_haftalik_fiyat(ticker: str) -> pd.DataFrame:
    """Haftalık kapanış fiyatlarını çeker (Google Trends'in haftalık
    çözünürlüğüyle eşleştirmek için)."""
    import yfinance as yf
    df = yf.Ticker(ticker).history(period="2y", interval="1wk")
    if df is None or df.empty:
        return pd.DataFrame()
    df = df.rename(columns={"Close": "close"})
    df.index = pd.to_datetime(df.index).tz_localize(None)
    return df[["close"]]


def google_trends_testi_calistir() -> tuple:
    """Her BIST hissesi için Google'da arama ilgisini (haftalık) çeker,
    MOMENTUM ve REVERSAL hipotezlerini ertesi haftanın getirisine karşı
    test eder. Döner: (dosya_yolu, özet_dict) ya da (None, hata_mesajı)."""
    try:
        from pytrends.request import TrendReq
    except ImportError:
        return None, "pytrends kurulu değil (requirements.txt'e eklenmesi gerekiyor)."

    pytrends = TrendReq(hl="tr-TR", tz=180)
    kayitlar = []
    for n_i, ticker in enumerate(BIST_TICKERS, 1):
        kod = ticker.replace(".IS", "")
        sorgu = f"{kod} hisse"
        try:
            print(f"[Google Trends {n_i}/{len(BIST_TICKERS)}] {kod} ({sorgu})...", flush=True)
            pytrends.build_payload([sorgu], timeframe="today 5-y", geo="TR")
            trend_df = pytrends.interest_over_time()
            if trend_df is None or trend_df.empty or sorgu not in trend_df.columns:
                print(f"[Google Trends] {kod}: veri dönmedi, atlanıyor.", flush=True)
                time.sleep(2.0)
                continue
            trend_df = trend_df[[sorgu]].rename(columns={sorgu: "ilgi"})
            trend_df.index = pd.to_datetime(trend_df.index).tz_localize(None)

            fiyat_df = _trend_haftalik_fiyat(ticker)
            if fiyat_df.empty:
                time.sleep(2.0)
                continue

            for tarih in trend_df.index:
                giris_konum = fiyat_df.index.get_indexer([tarih], method="nearest")[0]
                if giris_konum < 0 or giris_konum + 1 >= len(fiyat_df):
                    continue
                giris_fiyat = fiyat_df.iloc[giris_konum]["close"]
                cikis_fiyat = fiyat_df.iloc[giris_konum + 1]["close"]
                if giris_fiyat == 0 or pd.isna(giris_fiyat) or pd.isna(cikis_fiyat):
                    continue
                getiri = (cikis_fiyat - giris_fiyat) / giris_fiyat * 100
                kayitlar.append({"ticker": ticker, "tarih": tarih.date().isoformat(),
                                  "ilgi": trend_df.loc[tarih, "ilgi"],
                                  "sonraki_hafta_getiri_pct": round(getiri, 3)})
        except Exception as e:
            print(f"[Google Trends] {kod} hata: {e}", flush=True)
        time.sleep(2.0)  # pytrends hiz siniri icin temkinli bekleme

    if not kayitlar:
        return None, "Google Trends'ten hiçbir veri üretilemedi (muhtemelen hız sınırlaması)."

    df_all = pd.DataFrame(kayitlar)
    df_all = df_all.dropna(subset=["ilgi", "sonraki_hafta_getiri_pct"])
    if len(df_all) < 200:
        return None, f"Yeterli veri toplanamadı (sadece {len(df_all)} satır, en az 200 gerekiyor)."

    ust_esik = df_all["ilgi"].quantile(1 - TREND_ESIK_YUZDE)
    alt_esik = df_all["ilgi"].quantile(TREND_ESIK_YUZDE)
    maske_ust = df_all["ilgi"] >= ust_esik
    maske_alt = df_all["ilgi"] <= alt_esik

    # 2026-08-19 EKLENDİ: piyasa-geneli drift tuzağını ayırt etmek için
    # (v7'de ve sektör testinde defalarca gördüğümüz aynı sorun) - genel
    # kör "her zaman LONG" temel çizgisi + YÜKSEK-ilgi ve DÜŞÜK-ilgi
    # gruplarının kendi isabetleri AYRI AYRI raporlanıyor. İkisi de
    # kör temel çizgiden anlamlı şekilde ayrılıyorsa gerçek bir sinyal;
    # sadece biri ayrılıyorsa muhtemelen piyasa geneli trend.
    kor_pozitif_oran = (df_all["sonraki_hafta_getiri_pct"] > 0).mean()

    satirlar = []
    satirlar.append({
        "hipotez": "[KÖR TEMEL ÇİZGİ] Her zaman LONG (piyasa geneli drift)",
        "n": len(df_all), "dogru_n": int((df_all["sonraki_hafta_getiri_pct"] > 0).sum()),
        "kazanma_orani_pct": round(kor_pozitif_oran * 100, 2),
        "binom_p": _binom_p(int((df_all["sonraki_hafta_getiri_pct"] > 0).sum()), len(df_all)),
        "ort_isaretli_getiri_pct": round(df_all["sonraki_hafta_getiri_pct"].mean(), 4),
    })

    for grup_adi, maske, yon in (
        ("[MOMENTUM alt-grup] Yüksek ilgi -> LONG bahsi", maske_ust, "LONG"),
        ("[MOMENTUM alt-grup] Düşük ilgi -> SHORT bahsi", maske_alt, "SHORT"),
    ):
        secilen = df_all[maske]
        if len(secilen) < TREND_MIN_N:
            continue
        if yon == "LONG":
            dogru = secilen["sonraki_hafta_getiri_pct"] > 0
            isaretli = secilen["sonraki_hafta_getiri_pct"]
        else:
            dogru = secilen["sonraki_hafta_getiri_pct"] < 0
            isaretli = -secilen["sonraki_hafta_getiri_pct"]
        dogru_n = int(dogru.sum())
        satirlar.append({
            "hipotez": grup_adi, "n": len(secilen), "dogru_n": dogru_n,
            "kazanma_orani_pct": round(dogru.mean() * 100, 2),
            "binom_p": _binom_p(dogru_n, len(secilen)),
            "ort_isaretli_getiri_pct": round(isaretli.mean(), 4),
        })

    for tip, ust_yon, alt_yon in (("REVERSAL (ilgi patlaması -> tersine dön)", "SHORT", "LONG"),
                                   ("MOMENTUM (ilgi patlaması -> devam et)", "LONG", "SHORT")):
        yon_serisi = pd.Series(np.nan, index=df_all.index, dtype=object)
        yon_serisi[maske_ust] = ust_yon
        yon_serisi[maske_alt] = alt_yon
        secim = yon_serisi.notna()
        if secim.sum() < TREND_MIN_N:
            continue
        secilen = df_all[secim]
        yon_sel = yon_serisi[secim]
        dogru = ((yon_sel == "LONG") & (secilen["sonraki_hafta_getiri_pct"] > 0)) | \
                ((yon_sel == "SHORT") & (secilen["sonraki_hafta_getiri_pct"] < 0))
        dogru_n = int(dogru.sum())
        p = _binom_p(dogru_n, int(secim.sum()))
        isaretli = secilen["sonraki_hafta_getiri_pct"] * np.where(yon_sel == "LONG", 1, -1)
        satirlar.append({
            "hipotez": tip, "n": int(secim.sum()), "dogru_n": dogru_n,
            "kazanma_orani_pct": round(dogru.mean() * 100, 2),
            "binom_p": p, "ort_isaretli_getiri_pct": round(isaretli.mean(), 4),
        })

    if not satirlar:
        return None, f"Yeterli örneklem büyüklüğüne ({TREND_MIN_N}) ulaşan hipotez bulunamadı."

    tablo = pd.DataFrame(satirlar)
    dosya_yolu = _data_path("google_trends_testi.csv")
    tablo.to_csv(dosya_yolu, index=False, encoding="utf-8-sig")

    return dosya_yolu, {
        "hisse_sayisi": len(BIST_TICKERS), "toplam_gozlem": len(df_all),
        "satirlar": satirlar,
    }


# =============================================================================
# WIKIPEDIA SAYFA GÖRÜNTÜLENMELERİ — KAMU İLGİSİ TESTİ #2 — 2026-08-19
# =============================================================================
# GEREKÇE: Google Trends (pytrends) iki ayrı çalıştırmada TUTARSIZ sonuç
# verdi - güvenilmez. Wikimedia'nın (Wikipedia'nın sahibi vakıf) RESMİ,
# belgelenmiş, ücretsiz Pageviews API'si aynı "kamu ilgisi" hipotezini
# çok daha KARARLI bir altyapı üzerinde test ediyor - ham sayım, göreceli
# normalize edilmiş bir skor değil, o yüzden çalıştırmalar arası tutarsız
# olma riski çok daha düşük. GÜNLÜK çözünürlük var (Google Trends'in
# aksine haftalığa düşmüyor) - "ertesi gün" testi mümkün.

BIST_WIKI_MAKALE = {
    "THYAO.IS": "Türk Hava Yolları", "ASELS.IS": "Aselsan", "SISE.IS": "Şişecam",
    "KCHOL.IS": "Koç Holding", "GARAN.IS": "Garanti BBVA", "AKBNK.IS": "Akbank",
    "EREGL.IS": "Ereğli Demir ve Çelik Fabrikaları", "BIMAS.IS": "BİM",
    "TUPRS.IS": "Tüpraş", "SAHOL.IS": "Sabancı Holding", "PETKM.IS": "Petkim",
    "FROTO.IS": "Ford Otosan", "TOASO.IS": "Tofaş", "TCELL.IS": "Turkcell",
    "YKBNK.IS": "Yapı Kredi", "ISCTR.IS": "Türkiye İş Bankası",
    "PGSUS.IS": "Pegasus Hava Yolları", "TAVHL.IS": "TAV Havalimanları",
    "VESTL.IS": "Vestel", "SASA.IS": "Sasa Polyester",
    "KOZAL.IS": "Koza Altın İşletmeleri", "ENKAI.IS": "Enka İnşaat",
    "MGROS.IS": "Migros", "ARCLK.IS": "Arçelik", "AKSEN.IS": "Aksa Enerji",
    "TTKOM.IS": "Türk Telekom", "ULKER.IS": "Ülker", "OYAKC.IS": "Oyak Çimento",
    "HALKB.IS": "Halkbank", "VAKBN.IS": "VakıfBank",
}
WIKI_HEADERS = {"User-Agent": "arge-botu-arastirma contact@example.com"}
WIKI_ESIK_YUZDE = 0.20
WIKI_MIN_N = 20


def _wiki_gunluk_izlenme(makale: str, gun_sayisi: int = 730) -> pd.DataFrame:
    """Wikimedia Pageviews REST API - resmi, ücretsiz, kayıtsız. DÜRÜST
    NOT: Türkçe makale başlıkları tahmin edildi (BIST_WIKI_MAKALE) -
    tam eşleşmeyebilir, o hisse için 404/boş dönerse atlanır."""
    from datetime import date, timedelta
    from urllib.parse import quote
    bugun = date.today()
    baslangic = bugun - timedelta(days=gun_sayisi)
    makale_url = makale.replace(" ", "_")
    url = (f"https://wikimedia.org/api/rest_v1/metrics/pageviews/per-article/"
           f"tr.wikipedia.org/all-access/all-agents/{quote(makale_url, safe='_')}/daily/"
           f"{baslangic.strftime('%Y%m%d')}/{bugun.strftime('%Y%m%d')}")
    resp = requests.get(url, headers=WIKI_HEADERS, timeout=15)
    if resp.status_code != 200:
        return pd.DataFrame()
    items = resp.json().get("items", [])
    if not items:
        return pd.DataFrame()
    df = pd.DataFrame(items)
    df["tarih"] = pd.to_datetime(df["timestamp"], format="%Y%m%d%H")
    return df.set_index("tarih")[["views"]].rename(columns={"views": "izlenme"})


WIKI_UFUKLAR = [(1, "1 gün"), (3, "3 gün"), (5, "1 hafta"), (10, "2 hafta")]


def _wiki_hedef_matrisi(df_all: pd.DataFrame, hedef_kolonu: str, ufuk_etiketi: str) -> list:
    """Tek bir ufuk için kör temel çizgi + alt-grup + REVERSAL/MOMENTUM
    testlerini üretir - _feature_strateji_matrisi ile aynı desen, tek
    fark hedef kolonu parametrik ve etiket her satıra ekleniyor."""
    satirlar = []
    kor_pozitif_oran = (df_all[hedef_kolonu] > 0).mean()
    kor_dogru_n = int((df_all[hedef_kolonu] > 0).sum())
    satirlar.append({
        "ufuk": ufuk_etiketi, "hipotez": "[KÖR TEMEL ÇİZGİ] Her zaman LONG",
        "n": len(df_all), "dogru_n": kor_dogru_n,
        "kazanma_orani_pct": round(kor_pozitif_oran * 100, 2),
        "binom_p": _binom_p(kor_dogru_n, len(df_all)),
        "ort_isaretli_getiri_pct": round(df_all[hedef_kolonu].mean(), 4),
    })

    ust_esik = df_all["izlenme_orani"].quantile(1 - WIKI_ESIK_YUZDE)
    alt_esik = df_all["izlenme_orani"].quantile(WIKI_ESIK_YUZDE)
    maske_ust = df_all["izlenme_orani"] >= ust_esik
    maske_alt = df_all["izlenme_orani"] <= alt_esik

    for grup_adi, maske, yon in (
        ("[MOMENTUM alt-grup] Yüksek izlenme -> LONG", maske_ust, "LONG"),
        ("[MOMENTUM alt-grup] Düşük izlenme -> SHORT", maske_alt, "SHORT"),
    ):
        secilen = df_all[maske]
        if len(secilen) < WIKI_MIN_N:
            continue
        dogru = (secilen[hedef_kolonu] > 0) if yon == "LONG" else (secilen[hedef_kolonu] < 0)
        isaretli = secilen[hedef_kolonu] if yon == "LONG" else -secilen[hedef_kolonu]
        dogru_n = int(dogru.sum())
        satirlar.append({
            "ufuk": ufuk_etiketi, "hipotez": grup_adi, "n": len(secilen), "dogru_n": dogru_n,
            "kazanma_orani_pct": round(dogru.mean() * 100, 2),
            "binom_p": _binom_p(dogru_n, len(secilen)),
            "ort_isaretli_getiri_pct": round(isaretli.mean(), 4),
        })

    for tip, ust_yon, alt_yon in (("REVERSAL (tersine dön)", "SHORT", "LONG"),
                                   ("MOMENTUM (devam et)", "LONG", "SHORT")):
        yon_serisi = pd.Series(np.nan, index=df_all.index, dtype=object)
        yon_serisi[maske_ust] = ust_yon
        yon_serisi[maske_alt] = alt_yon
        secim = yon_serisi.notna()
        if secim.sum() < WIKI_MIN_N:
            continue
        secilen = df_all[secim]
        yon_sel = yon_serisi[secim]
        dogru = ((yon_sel == "LONG") & (secilen[hedef_kolonu] > 0)) | \
                ((yon_sel == "SHORT") & (secilen[hedef_kolonu] < 0))
        dogru_n = int(dogru.sum())
        isaretli = secilen[hedef_kolonu] * np.where(yon_sel == "LONG", 1, -1)
        satirlar.append({
            "ufuk": ufuk_etiketi, "hipotez": tip, "n": int(secim.sum()), "dogru_n": dogru_n,
            "kazanma_orani_pct": round(dogru.mean() * 100, 2),
            "binom_p": _binom_p(dogru_n, int(secim.sum())),
            "ort_isaretli_getiri_pct": round(isaretli.mean(), 4),
        })
    return satirlar


def wiki_testi_calistir() -> tuple:
    """Her hisse için Wikipedia sayfa görüntülenmesi (günlük) çekiyor,
    30 günlük hareketli ortalamaya göre anormal artışları (izlenme_orani)
    DÖRT AYRI UFUKTA (1 gün, 3 gün, 1 hafta, 2 hafta) MOMENTUM/REVERSAL
    hipotezleriyle test ediyor - 2026-08-19: tek ufuk yerine çoklu ufuk,
    kullanıcının "belki sinyal daha geç ortaya çıkıyor" fikrini test
    etmek için. Döner: (dosya_yolu, özet_dict) ya da (None, hata_mesajı)."""
    import yfinance as yf
    parcalar = []
    for n_i, (ticker, makale) in enumerate(BIST_WIKI_MAKALE.items(), 1):
        try:
            print(f"[Wikipedia {n_i}/{len(BIST_WIKI_MAKALE)}] {ticker} ({makale})...", flush=True)
            wiki_df = _wiki_gunluk_izlenme(makale)
            if wiki_df.empty or len(wiki_df) < 60:
                print(f"[Wikipedia] {ticker}: veri yok/az, atlanıyor.", flush=True)
                time.sleep(0.5)
                continue
            wiki_df["izlenme_ort30"] = wiki_df["izlenme"].rolling(30, min_periods=15).mean()
            wiki_df["izlenme_orani"] = wiki_df["izlenme"] / wiki_df["izlenme_ort30"].replace(0, np.nan)

            fiyat_df = yf.Ticker(ticker).history(period="2y", interval="1d")
            if fiyat_df is None or fiyat_df.empty:
                time.sleep(0.5)
                continue
            fiyat_df = fiyat_df.rename(columns={"Close": "close"})
            fiyat_df.index = pd.to_datetime(fiyat_df.index).tz_localize(None)

            for tarih in wiki_df.index:
                giris_konum = fiyat_df.index.get_indexer([tarih], method="nearest")[0]
                if giris_konum < 0:
                    continue
                giris_fiyat = fiyat_df.iloc[giris_konum]["close"]
                oran = wiki_df.loc[tarih, "izlenme_orani"]
                if giris_fiyat == 0 or pd.isna(giris_fiyat) or pd.isna(oran):
                    continue
                satir = {"ticker": ticker, "tarih": tarih.date().isoformat(), "izlenme_orani": oran}
                gecerli_satir = False
                for gun, etiket in WIKI_UFUKLAR:
                    cikis_konum = giris_konum + gun
                    if cikis_konum >= len(fiyat_df):
                        continue
                    cikis_fiyat = fiyat_df.iloc[cikis_konum]["close"]
                    if pd.isna(cikis_fiyat):
                        continue
                    getiri = (cikis_fiyat - giris_fiyat) / giris_fiyat * 100
                    satir[f"getiri_{gun}g"] = round(getiri, 3)
                    gecerli_satir = True
                if gecerli_satir:
                    parcalar.append(satir)
        except Exception as e:
            print(f"[Wikipedia] {ticker} hata: {e}", flush=True)
        time.sleep(0.5)

    if not parcalar:
        return None, "Wikipedia'dan hiçbir veri üretilemedi (makale eşleşmeleri yanlış olabilir)."

    df_all = pd.DataFrame(parcalar)
    if len(df_all) < 200:
        return None, f"Yeterli veri toplanamadı (sadece {len(df_all)} satır)."

    tum_satirlar = []
    for gun, etiket in WIKI_UFUKLAR:
        kolon = f"getiri_{gun}g"
        if kolon not in df_all.columns:
            continue
        alt_df = df_all.dropna(subset=["izlenme_orani", kolon])
        if len(alt_df) < 200:
            continue
        tum_satirlar.extend(_wiki_hedef_matrisi(alt_df, kolon, etiket))

    if not tum_satirlar:
        return None, "Hiçbir ufuk için yeterli veri/örneklem bulunamadı."

    tablo = pd.DataFrame(tum_satirlar)
    dosya_yolu = _data_path("wiki_testi.csv")
    tablo.to_csv(dosya_yolu, index=False, encoding="utf-8-sig")
    return dosya_yolu, {"hisse_sayisi": len(BIST_WIKI_MAKALE), "toplam_gozlem": len(df_all),
                         "satirlar": tum_satirlar}


# =============================================================================
# WIKIPEDIA SİNYALİ — İZOLE R:R DOĞRULAMA — 2026-08-19
# =============================================================================
# GEREKÇE: /wiki_testi'nin 2 haftalık ufukta bulduğu şey (yüksek izlenme
# -> LONG, piyasa driftinin BİLE üstünde %53.23, p=0.00014) gerçek mi
# yoksa 4 ufuk x birkaç hipotez taramasının ürünü mü, netleştirmek için
# İZOLE, TEK hipotezli, GERÇEK R:R çıkışlı (kanıt doğrulamada kullanılan
# aynı 1.5R kısmi TP + trailing mantığı, _kanit_bist_rr_sonuc'u yeniden
# kullanıyor) bir doğrulama. Sadece LONG (SHORT tarafı zaten işe
# yaramamıştı, dahil edilmedi).

def wiki_dogrulama_calistir() -> tuple:
    """Yüksek Wikipedia izlenmesi olan günlerde (üst %20) LONG açıp
    gerçek R:R çıkışıyla sonuçlandırır, kalan TÜM günlerin kör LONG
    temel çizgisiyle karşılaştırır. Döner: (dosya_yolu, özet_dict)."""
    import yfinance as yf
    ticker_data = {}
    tum_izlenme = []
    for n_i, (ticker, makale) in enumerate(BIST_WIKI_MAKALE.items(), 1):
        try:
            print(f"[Wiki Doğrulama {n_i}/{len(BIST_WIKI_MAKALE)}] {ticker}...", flush=True)
            df = yf.Ticker(ticker).history(period="2y", interval="1d")
            if df is None or df.empty or len(df) < 60:
                time.sleep(0.5)
                continue
            df = df.rename(columns={"Open": "open", "High": "high", "Low": "low",
                                     "Close": "close", "Volume": "volume"})
            df = df[["open", "high", "low", "close", "volume"]].copy()
            df.index = pd.to_datetime(df.index).tz_localize(None)
            orijinal_tarihler = df.index
            df = df.reset_index(drop=True)
            df = _kanit_compute_indicators(df)
            df["tarih"] = orijinal_tarihler.values

            wiki_df = _wiki_gunluk_izlenme(makale)
            if wiki_df.empty:
                time.sleep(0.5)
                continue
            wiki_df["izlenme_ort30"] = wiki_df["izlenme"].rolling(30, min_periods=15).mean()
            wiki_df["izlenme_orani"] = wiki_df["izlenme"] / wiki_df["izlenme_ort30"].replace(0, np.nan)

            df["izlenme_orani"] = np.nan
            df = df.set_index("tarih")
            for tarih in wiki_df.index:
                if tarih < df.index.min() or tarih > df.index.max():
                    continue
                konum = df.index.get_indexer([tarih], method="nearest")[0]
                if konum < 0:
                    continue
                eslesen_tarih = df.index[konum]
                if abs((eslesen_tarih - tarih).days) > 3:
                    continue
                df.iloc[konum, df.columns.get_loc("izlenme_orani")] = wiki_df.loc[tarih, "izlenme_orani"]
            df = df.reset_index(drop=True)

            ticker_data[ticker] = df
            tum_izlenme.extend(df["izlenme_orani"].dropna().tolist())
        except Exception as e:
            print(f"[Wiki Doğrulama] {ticker} hata: {e}", flush=True)
        time.sleep(0.5)

    if not tum_izlenme or len(tum_izlenme) < 200:
        return None, "Yeterli izlenme verisi toplanamadı."

    esik = float(np.quantile(tum_izlenme, 1 - WIKI_ESIK_YUZDE))
    print(f"[Wiki Doğrulama] Üst %20 eşiği: izlenme_orani >= {esik:.3f}", flush=True)

    tum_sonuclar = {"[Wiki] Yüksek izlenme -> LONG (gerçek R:R)": [],
                     "[KÖR] Diğer tüm günler -> LONG (gerçek R:R)": []}
    for ticker, df in ticker_data.items():
        for idx in range(25, len(df) - 1):
            row = df.iloc[idx]
            sonuc = _kanit_bist_rr_sonuc(df, idx, "LONG")
            if sonuc is None:
                continue
            durum, r = sonuc
            if pd.notna(row.get("izlenme_orani")) and row["izlenme_orani"] >= esik:
                tum_sonuclar["[Wiki] Yüksek izlenme -> LONG (gerçek R:R)"].append((durum, r))
            else:
                tum_sonuclar["[KÖR] Diğer tüm günler -> LONG (gerçek R:R)"].append((durum, r))

    satirlar = _kanit_ozet_tablosu(tum_sonuclar)
    if not satirlar:
        return None, "Yeterli sinyal üretilemedi."

    tablo = pd.DataFrame(satirlar)
    dosya_yolu = _data_path("wiki_dogrulama.csv")
    tablo.to_csv(dosya_yolu, index=False, encoding="utf-8-sig")
    return dosya_yolu, {"esik": round(esik, 3), "satirlar": satirlar}


# =============================================================================
# WIKI SİNYALİ — GERÇEK ÇIKIŞ MANTIĞIYLA İZOLE DOĞRULAMA — 2026-08-19
# =============================================================================
# GEREKÇE: wiki_testi_calistir 4 ufuk taradı, en güçlü sonuç "yüksek
# izlenme -> 2 hafta LONG" oldu (kör piyasa driftinin bile üstünde,
# p=0.00014). Bu, o TEK bulguyu izole edip GERÇEK BIST çıkış mantığıyla
# (1.5R kısmi TP + breakeven + ATR trailing - _kanit_bist_rr_sonuc,
# kanit_dogrulama'da doğrulanan aynı mekanik) yeniden test ediyor.
# KONTROL GRUBU: aynı mekanikle "her gün LONG açsak ne olurdu" - piyasa
# driftini mi yakalıyoruz yoksa gerçek ek değer mi var, ayırt etmek için.

def wiki_kanit_dogrulama_calistir() -> tuple:
    """Yüksek Wikipedia izlenmesi günlerinde LONG'u, BIST'in gerçek çıkış
    mantığıyla test eder; kontrol grubu olarak koşulsuz (her gün) LONG'u
    aynı mekanikle test eder. Döner: (dosya_yolu, özet_dict) ya da
    (None, hata_mesajı)."""
    import yfinance as yf

    # 1. AŞAMA: tüm hisseler için wiki izlenme_orani topla, global eşiği
    # wiki_testi_calistir ile TUTARLI şekilde hesapla (üst %20).
    ticker_wiki = {}
    tum_oranlar = []
    for n_i, (ticker, makale) in enumerate(BIST_WIKI_MAKALE.items(), 1):
        try:
            print(f"[Wiki Kanıt 1/2 - {n_i}/{len(BIST_WIKI_MAKALE)}] {ticker}...", flush=True)
            wiki_df = _wiki_gunluk_izlenme(makale)
            if wiki_df.empty or len(wiki_df) < 60:
                continue
            wiki_df["izlenme_ort30"] = wiki_df["izlenme"].rolling(30, min_periods=15).mean()
            wiki_df["izlenme_orani"] = wiki_df["izlenme"] / wiki_df["izlenme_ort30"].replace(0, np.nan)
            ticker_wiki[ticker] = wiki_df
            tum_oranlar.extend(wiki_df["izlenme_orani"].dropna().tolist())
        except Exception as e:
            print(f"[Wiki Kanıt] {ticker} wiki hatası: {e}", flush=True)
        time.sleep(0.5)

    if not tum_oranlar:
        return None, "Wikipedia verisi toplanamadı."
    esik = float(np.quantile(tum_oranlar, 1 - WIKI_ESIK_YUZDE))
    print(f"[Wiki Kanıt] Global eşik (üst %20): {esik:.3f}", flush=True)

    # 2. AŞAMA: her hisse için fiyat+ATR verisi çek, GERÇEK çıkış
    # mantığıyla hem WIKI-sinyalli hem KONTROL (koşulsuz) LONG'u test et.
    tum_sonuclar = {"[WIKI sinyalli] Yüksek izlenme -> LONG": [],
                     "[KONTROL] Koşulsuz her gün LONG": []}
    for n_i, (ticker, wiki_df) in enumerate(ticker_wiki.items(), 1):
        try:
            print(f"[Wiki Kanıt 2/2 - {n_i}/{len(ticker_wiki)}] {ticker}...", flush=True)
            raw = yf.Ticker(ticker).history(period="2y", interval="1d")
            if raw is None or raw.empty or len(raw) < 60:
                continue
            raw = raw.rename(columns={"Open": "open", "High": "high", "Low": "low",
                                       "Close": "close", "Volume": "volume"})
            raw = raw[["open", "high", "low", "close", "volume"]].copy()
            raw.index = pd.to_datetime(raw.index).tz_localize(None)
            tarihler = raw.index
            df = raw.reset_index(drop=True)
            df = _kanit_compute_indicators(df)

            for idx in range(25, len(df) - 1):
                tarih = tarihler[idx]
                kontrol_sonuc = _kanit_bist_rr_sonuc(df, idx, "LONG")
                if kontrol_sonuc is not None:
                    tum_sonuclar["[KONTROL] Koşulsuz her gün LONG"].append(kontrol_sonuc)

                oran = np.nan
                if tarih in wiki_df.index:
                    oran = wiki_df.loc[tarih, "izlenme_orani"]
                else:
                    konum = wiki_df.index.get_indexer([tarih], method="nearest")[0]
                    if konum >= 0:
                        aday_tarih = wiki_df.index[konum]
                        if abs((aday_tarih - tarih).days) <= 2:
                            oran = wiki_df.iloc[konum]["izlenme_orani"]
                if pd.notna(oran) and oran >= esik:
                    wiki_sonuc = _kanit_bist_rr_sonuc(df, idx, "LONG")
                    if wiki_sonuc is not None:
                        tum_sonuclar["[WIKI sinyalli] Yüksek izlenme -> LONG"].append(wiki_sonuc)
        except Exception as e:
            print(f"[Wiki Kanıt] {ticker} fiyat hatası: {e}", flush=True)
        time.sleep(1.0)

    satirlar = _kanit_ozet_tablosu(tum_sonuclar)
    if not satirlar:
        return None, "Hiçbir grup için yeterli veri üretilemedi."
    tablo = pd.DataFrame(satirlar)
    dosya_yolu = _data_path("wiki_kanit_dogrulama.csv")
    tablo.to_csv(dosya_yolu, index=False, encoding="utf-8-sig")
    return dosya_yolu, {"esik": round(esik, 3), "satirlar": satirlar}


# =============================================================================
# EKŞİ SÖZLÜK BAĞLANTI TESTİ — 2026-08-19
# =============================================================================
# GEREKÇE: Ekşi Sözlük'ün resmi bir API'si yok - sadece web sayfası
# kazıma (scraping) ile erişilebilir, yapısını hiç bilmiyorum. pykap'ta

# yaptığımız gibi ÖNCE küçük bir bağlantı testiyle gerçekten çalışıp
# çalışmadığını doğruluyoruz - büyük bir backtest kurmadan önce.

def eksisozluk_baglanti_testi(baslik: str = "thyao") -> str:
    """Bir başlık (topic) sayfasını çekmeyi dener, kaç giriş (entry)
    bulunduğunu ve ilk birkaçının tarihini raporlar. Metin raporu döner."""
    satirlar = []
    headers = {"User-Agent": "Mozilla/5.0 (research bot; arge-botu)"}
    url = f"https://eksisozluk.com/{baslik}"
    try:
        resp = requests.get(url, headers=headers, timeout=15)
        satirlar.append(f"URL: {url}")
        satirlar.append(f"HTTP durum kodu: {resp.status_code}")
        if resp.status_code != 200:
            satirlar.append("❌ Sayfa çekilemedi (403/404 engellenmiş/bulunamamış olabilir).")
            return "\n".join(satirlar)
        html = resp.text
        satirlar.append(f"✅ Sayfa çekildi, {len(html)} karakter.")

        import re
        giris_sayisi = len(re.findall(r'class="content"', html))
        satirlar.append(f"Tahmini giriş (entry) sayısı bu sayfada: {giris_sayisi}")

        tarihler = re.findall(r'<a[^>]*class="[^"]*entry-date[^"]*"[^>]*>([^<]+)</a>', html)
        if tarihler:
            satirlar.append(f"✅ {len(tarihler)} tarih etiketi bulundu. İlk 5: {tarihler[:5]}")
        else:
            satirlar.append("⚠️ Tarih etiketi bulunamadı - HTML yapısı beklenenden farklı "
                             "olabilir, kod güncellenmesi gerekebilir.")
    except Exception as e:
        satirlar.append(f"❌ Hata: {e}")
    return "\n".join(satirlar)


def send_startup_message():
    send_telegram_message(
        f"🔬 Ar-Ge Botu (aynı deploy içinde, izole) başlatıldı.\n"
        f"🔖 Kod sürümü: {ARGE_KOD_SURUMU}\n\n"
        "SADECE gece radarı için çalışıyor. İki araç var:\n"
        "  1) Hipotez araştırması (kural+AI kuyruğu, Gemini'ye soruyor)\n"
        "  2) Hesap Makinesi — TAMAMEN KOD İÇİNDE (Gemini'siz), her "
        "göstergeye standart teknik analiz kuralı uygulayıp oylayan, "
        "deterministik bir LONG/SHORT hesaplayıcı\n\n"
        "🧮 Hesap Makinesi komutları:\n"
        "/hesap_test TARİH — o günün kapanışıyla BIST hisseleri için "
        "toplu hesaplama yapıp ertesi günle karşılaştırır\n"
        "/hesap_test_seri TARİH1 TARİH2 ... — birden fazla tarihi art arda\n"
        "/hesap_tam_test [BAŞLANGIÇ] [BİTİŞ] — 2026-01-01'den bugüne (ya da "
        "verilen aralığa) kadar TÜM işlem günlerini test edip TEK BİR CSV "
        "DOSYASI olarak gönderir\n"
        "/hesap_debug TICKER TARİH — bir hissenin tam hesaplama dökümü\n"
        "/hesap_rapor — birikmiş toplam isabet oranı\n"
        "/hesap_makinesi TICKER — anlık canlı hesaplama\n"
        "/gosterge_turnuvasi [BAŞLANGIÇ] [BİTİŞ] — hesap makinesinin "
        "ağırlıklı toplamı yerine her göstergeyi TEK BAŞINA (ABD swing "
        "turnuvasındaki gibi izole) test edip lider tablosu üretir\n"
        "/kur_sektor_testi [BAŞLANGIÇ] [BİTİŞ] — göstergeleri ham getiri, "
        "USDTRY-arındırılmış getiri ve sektör-göreceli getiri olmak üzere "
        "3 farklı hedefe karşı test eder\n"
        "/icgorusel_islem [GÜN_UFKU] — ABD hisselerinde içeriden (yönetici/"
        "yönetim kurulu) alım-satımın sonraki getiriyle ilişkisini test eder "
        "(varsayılan 20 işlem günü ufku)\n"
        "/icgorusel_islem_edgar [GÜN_UFKU] — aynı test ama SEC EDGAR'dan "
        "(yfinance yerine), ilk 40 hisseyle sınırlı, daha yavaş ama hız "
        "sınırına takılmıyor\n"
        "/kanit_ters_islem — Fitil+RSI+Hacim ve Sadece RSI'ın yönünü ters "
        "çevirip (doğru stop/hedef ile sıfırdan) test eder\n"
        "/pykap_test — BIST için KAP'ın 'Pay Alım Satım Bildirimi' arşivine "
        "pykap kütüphanesiyle erişilebiliyor mu diye küçük bir bağlantı "
        "testi yapar (henüz backtest değil, sadece keşif)\n"
        "/trend_testi — BIST hisselerine olan Google arama ilgisinin "
        "(haftalık) ertesi hafta getirisiyle ilişkisini MOMENTUM ve "
        "REVERSAL hipotezleriyle test eder\n"
        "/wiki_testi — Wikipedia sayfa görüntülenmesi (resmi API, günlük) "
        "ile aynı kamu-ilgisi hipotezini daha güvenilir bir kaynakla "
        "test eder\n"
        "/wiki_kanit_dogrulama — wiki_testi'nin en güçlü bulgusunu (yüksek "
        "izlenme -> LONG) BIST'in gerçek çıkış mantığıyla, koşulsuz LONG "
        "kontrol grubuna karşı izole doğrular\n"
        "/wiki_dogrulama — /wiki_testi'nin bulgusunu (yüksek izlenme -> "
        "LONG) izole, gerçek R:R çıkışıyla yeniden doğrular\n"
        "/eksisozluk_test [BAŞLIK] — Ekşi Sözlük'ten veri çekilebiliyor "
        "mu diye bağlantı testi yapar (henüz backtest değil)\n"
        "/kanit_dogrulama — canlı sistemin kanıtlanmış 4 stratejisini "
        "(Fitil+RSI+Hacim, Sadece RSI, ATR Kırılımı, Hacim Z-Skor) taze "
        "2 yıllık veriyle, gerçek çıkış mantığıyla ve anlamlılık testiyle "
        "yeniden doğrular\n"
        "/kanit_dogrulama_transfer — ABD'nin kanıtlanmış giriş koşullarını "
        "(ATR Kırılımı, Hacim Z-Skor) BIST hisselerinde, BIST'in gerçek "
        "çıkış mantığıyla test eder\n\n"
        "🔬 Hipotez araştırması komutları:\n"
        "/arge_rapor — şimdiye kadarki tüm denemelerin özeti\n"
        "/arge_test — hemen bir hipotez dener (test amaçlı)\n\n"
        "⚠️ Bu bot SADECE araştırma yapar — hiçbir sinyal/emir üretmez, "
        "gece radarına (overnight_radar.py) bağlı değildir."
    )


def build_report() -> str:
    gecmis = _read_history()
    if not gecmis:
        return "🔬 [AR-GE BOTU] Henüz hiç hipotez denenmedi."
    toplam = len(gecmis)
    onayli = [r for r in gecmis if r["onayli_mi"] == "1"]
    lines = [f"🔬 [AR-GE RAPORU — GECE RADARI İÇİN] Toplam: {toplam}",
             f"İlk onay: {len(onayli)}", ""]

    reconfirm = _read_reconfirm()
    if reconfirm:
        kesin = [r for r in reconfirm if r["kesin_guvenilir_mi"] in ("1", 1, "True", True)]
        lines.append(f"🏆 KESİN GÜVENİLİR: {len(kesin)}")
        for r in kesin:
            lines.append(f"  {r['isim']} ({r['yon']}): seri {r['seri']}/{RECONFIRM_STREAK_REQUIRED}")
        bekleyen = [r for r in reconfirm if r not in kesin]
        if bekleyen:
            lines.append(f"\n🔁 Yeniden-doğrulama sürecinde: {len(bekleyen)}")
            for r in bekleyen:
                lines.append(f"  {r['isim']}: seri {r['seri']}/{RECONFIRM_STREAK_REQUIRED}")
        lines.append("")

    lines.append(f"Onay çıtası: kazanma oranı ≥%{MIN_WIN_RATE_PCT:.0f} (hedef: +%{TARGET_PCT:.1f})")
    lines.append("\nSon 5 deneme:")
    for r in gecmis[-5:]:
        kazanma = r.get("sinav_kazanma") or r.get("dogrulama_kazanma") or r.get("egitim_kazanma") or ""
        lines.append(f"  {r['tarih']} {r['isim']}: {r['asama']}"
                     + (f" (kazanma %{kazanma})" if kazanma != "" else ""))
    return "\n".join(lines)


# =============================================================================
# HESAP MAKİNESİ — istek üzerine, herhangi bir hisse için ANLIK tam gösterge
# dökümü + varsa onaylı/kesinleşmiş modelin verdiği LONG/SHORT tahmini.
# =============================================================================

HESAP_MAKINESI_LOG_FILE = _data_path("arge_hesap_makinesi_log.csv")
HESAP_MAKINESI_LOG_FIELDS = ["tarih", "ticker", "yon", "guven", "gerekce",
                              "entry_price", "checked_at", "sonuc", "gerceklesen_pct"]


def _read_hesap_log():
    if not os.path.exists(HESAP_MAKINESI_LOG_FILE):
        return []
    with open(HESAP_MAKINESI_LOG_FILE, newline="", encoding="utf-8") as f:
        return list(csv.DictReader(f))


def _append_hesap_log(row: dict):
    exists = os.path.exists(HESAP_MAKINESI_LOG_FILE)
    with open(HESAP_MAKINESI_LOG_FILE, "a", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=HESAP_MAKINESI_LOG_FIELDS)
        if not exists:
            w.writeheader()
        w.writerow(row)


def build_hesap_makinesi(ticker: str) -> str:
    """GERÇEK 'hesap makinesi' - istatistiksel model EĞİTMİYOR, Gemini'ye
    de SORMUYOR (2026-08-16 güncelleme - backtest'teki aynı sebep: Gemini
    hem güvenilmez çıktı hem kota sorunlarına açıktı). Bir hissenin TÜM
    güncel gösterge değerlerini (RSI, MACD, VWAP, hacim, Bollinger, ATR,
    CMF, MFI, Stochastic, vb.) hesaplayıp hesapla_yon_kod_ile() ile
    TAMAMEN KOD İÇİNDE, deterministik bir LONG/SHORT/NÖTR kararı üretiyor
    - backtest'te kullanılanla BİREBİR AYNI mantık."""
    import yfinance as yf
    if not ticker.upper().endswith(".IS"):
        ticker = ticker.upper() + ".IS"
    else:
        ticker = ticker.upper()

    try:
        df = yf.Ticker(ticker).history(period="6mo", interval="1d")
        if df is None or df.empty or len(df) < 60:
            return f"🧮 {ticker}: yeterli geçmiş veri yok (en az ~60 gün gerekli)."
        df = df.rename(columns={"Open": "open", "High": "high", "Low": "low",
                                 "Close": "close", "Volume": "volume"})
        df = df[["open", "high", "low", "close", "volume"]].copy()
        df.index = pd.to_datetime(df.index).tz_localize(None)

        index_pct = None
        try:
            idf = yf.Ticker("XU100.IS").history(period="6mo", interval="1d")
            if idf is not None and not idf.empty:
                idf.index = pd.to_datetime(idf.index).tz_localize(None)
                index_pct = (idf["Close"] - idf["Close"].shift(1)) / idf["Close"].shift(1) * 100
        except Exception:
            pass

        df = compute_features(df, index_pct)
        son = df.iloc[-1]
        entry_price = float(son["close"])
    except Exception as e:
        return f"🧮 {ticker}: veri çekme hatası — {e}"

    gosterge_satirlari = []
    for ad, deger in [
        ("RSI14", son["rsi14"]), ("MACD histogram", son["macd_hist"]),
        ("Bollinger genişliği %", son["bb_bandwidth"]), ("ATR %", son["atr_pct"]),
        ("Hacim oranı (20g ort.)", son["volume_factor"]), ("CMF", son["cmf"]),
        ("MFI", son["mfi"]), ("Stochastic %K", son["stoch_k"]),
        ("SMA20'ye uzaklık %", son["dist_sma20_pct"]), ("SMA50'ye uzaklık %", son["dist_sma50_pct"]),
        ("VWAP'a uzaklık % (yaklaşık)", son["vwap_dist_pct"]),
        ("Kapanış-zirve konumu %", son["close_to_high_pct"]), ("Gap %", son["gap_pct"]),
        ("Günlük değişim %", son["pct_change"]), ("Relative strength (XU100'e göre)", son["relative_strength"]),
    ]:
        gosterge_satirlari.append(f"{ad}: {deger:.2f}" if pd.notna(deger) else f"{ad}: bilinmiyor")

    lines = [f"🧮 [HESAP MAKİNESİ] {ticker} — {df.index[-1].date()} kapanışı", ""]
    lines.extend(f"  {s}" for s in gosterge_satirlari)
    lines.append("")

    gostergeler = _gostergeleri_hesapla(df, df.index[-1])
    sonuc = hesapla_yon_kod_ile(gostergeler)

    yon_ikon = "🟢 LONG" if sonuc["yon"] == "LONG" else "🔴 SHORT"
    lines.append(f"{yon_ikon} — kod-tabanlı skor: {sonuc['skor']:+.2f}")
    lines.append(f"Gerekçe: {', '.join(sonuc['detaylar'].keys())}")
    lines.append("")
    lines.append("⚠️ Bu İSTATİSTİKSEL OLARAK DOĞRULANMIŞ bir tahmin DEĞİL — standart "
                 "teknik analiz kurallarının oylaması. Kaydedildi, ertesi gün "
                 "/hesap_sonuclari ile gerçekte ne olduğu görülüp zamanla isabet "
                 "oranı takip edilebilecek.")

    _append_hesap_log({
        "tarih": datetime.now(timezone.utc).isoformat(), "ticker": ticker, "yon": sonuc["yon"],
        "guven": min(100, abs(sonuc["skor"]) * 15), "gerekce": ", ".join(sonuc["detaylar"].keys()),
        "entry_price": entry_price, "checked_at": "", "sonuc": "PENDING", "gerceklesen_pct": "",
    })
    return "\n".join(lines)


def check_hesap_makinesi_sonuclari():
    """Gecmis /hesap_makinesi cagrilarinin ERTESI GUN 10:00-12:00 penceresine
    bakip Gemini'nin yorumu dogru cikmis mi kontrol eder - overnight_radar.py
    ile AYNI pencere/mantik. SONUC otomatik Telegram'a gitmez, sadece
    kaydedilir - /hesap_sonuclari ile sorgulanir, spam yapilmaz."""
    import yfinance as yf
    from zoneinfo import ZoneInfo
    rows = _read_hesap_log()
    if not rows:
        return
    now_ist = datetime.now(timezone.utc).astimezone(ZoneInfo("Europe/Istanbul"))
    degisti = False
    for r in rows:
        if r["sonuc"] != "PENDING":
            continue
        try:
            olusturma = datetime.fromisoformat(r["tarih"]).astimezone(ZoneInfo("Europe/Istanbul"))
        except Exception:
            continue
        gun = olusturma.date()
        if now_ist.date() <= gun:
            continue  # ertesi gun henuz gelmedi
        try:
            df15 = yf.Ticker(r["ticker"]).history(period="60d", interval="15m")
            if df15 is None or df15.empty:
                continue
            idx = pd.to_datetime(df15.index)
            idx = idx.tz_convert("Europe/Istanbul") if idx.tz is not None else idx.tz_localize("Europe/Istanbul")
            df15.index = idx
            df15["gun_norm"] = df15.index.normalize().tz_localize(None)
            dakika = df15.index.hour * 60 + df15.index.minute
            pencere = df15[(dakika >= 600) & (dakika < 720)]
            gunler = sorted(d for d in pencere["gun_norm"].unique() if d.date() > gun)
            if not gunler:
                continue
            hedef_gun = gunler[0]
            if (hedef_gun.date() - gun).days > 5:
                continue
            tepe = float(pencere[pencere["gun_norm"] == hedef_gun]["High"].max())
            entry = float(r["entry_price"])
            gerceklesen = (tepe - entry) / entry * 100
            if r["yon"] == "SHORT":
                gerceklesen = -gerceklesen
            r["gerceklesen_pct"] = round(gerceklesen, 2)
            r["sonuc"] = "DOGRU" if gerceklesen > 0 else "YANLIS"
            r["checked_at"] = now_ist.isoformat()
            degisti = True
        except Exception as e:
            print(f"[ARGE] {r['ticker']} hesap makinesi sonuç kontrolü hatası: {e}", flush=True)
    if degisti:
        with open(HESAP_MAKINESI_LOG_FILE, "w", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=HESAP_MAKINESI_LOG_FIELDS)
            w.writeheader()
            w.writerows(rows)


def build_hesap_sonuclari() -> str:
    rows = _read_hesap_log()
    if not rows:
        return "🧮 Henüz hiç /hesap_makinesi çağrısı yapılmadı."
    kapanan = [r for r in rows if r["sonuc"] in ("DOGRU", "YANLIS")]
    lines = [f"🧮 [HESAP MAKİNESİ İSABET TAKİBİ] Toplam: {len(rows)} (kapanan {len(kapanan)})"]
    if kapanan:
        dogru = sum(1 for r in kapanan if r["sonuc"] == "DOGRU")
        lines.append(f"İsabet oranı: %{dogru/len(kapanan)*100:.1f} ({dogru}/{len(kapanan)})")
    lines.append("\nSon 5:")
    for r in rows[-5:]:
        lines.append(f"  {r['ticker']} ({r['yon']}, güven %{r['guven']}): {r['sonuc']}"
                     + (f" ({r['gerceklesen_pct']}%)" if r.get("gerceklesen_pct") else ""))
    return "\n".join(lines)


# =============================================================================
# KENDİ TELEGRAM KOMUT DİNLEYİCİSİ (ana bottan AYRI token/chat_id)
# =============================================================================

def poll_arge_commands():
    """Kisa, bloklamayan bir Telegram kontrolu - her cagrida en fazla birkac
    saniye surer, ana dongunun akisini durdurmaz."""
    if not _ARGE_AVAILABLE:
        return
    offset = None
    if os.path.exists(CMD_OFFSET_FILE):
        try:
            offset = int(open(CMD_OFFSET_FILE).read().strip())
        except Exception:
            offset = None

    try:
        params = {"timeout": 0}
        if offset is not None:
            params["offset"] = offset
        resp = requests.get(
            f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/getUpdates",
            params=params, timeout=10)
        updates = resp.json().get("result", [])
    except Exception as e:
        print(f"[ARGE] Telegram komut kontrolü hatası: {e}", flush=True)
        return

    for u in updates:
        offset = u["update_id"] + 1
        msg = u.get("message", {})
        chat_id = str(msg.get("chat", {}).get("id", ""))
        text = msg.get("text", "")
        if chat_id != str(TELEGRAM_CHAT_ID) or not text.startswith("/"):
            continue
        if text.startswith("/arge_rapor"):
            send_telegram_message(build_report())
        elif text.startswith("/hesap_makinesi"):
            parcalar = text.split()
            if len(parcalar) < 2:
                send_telegram_message("Kullanım: /hesap_makinesi ASTOR (ya da THYAO, TUPRS gibi)")
            else:
                send_telegram_message(build_hesap_makinesi(parcalar[1]))
        elif text.startswith("/hesap_debug"):
            parcalar = text.split()
            if len(parcalar) < 3:
                send_telegram_message("Kullanım: /hesap_debug ASELS 2026-07-09 "
                                       "(bir hissenin bir tarihteki tam hesaplama dökümünü gösterir)")
            else:
                def _arka_plan_debug(ticker, tarih):
                    try:
                        send_telegram_message(hesap_makinesi_debug(ticker, tarih))
                    except Exception as e:
                        send_telegram_message(f"🔬 Teşhis hatası: {e}")
                threading.Thread(target=_arka_plan_debug, args=(parcalar[1], parcalar[2]), daemon=True).start()
        elif text.startswith("/hesap_sonuclari"):
            send_telegram_message(build_hesap_sonuclari())
        elif text.startswith("/hesap_test"):
            parcalar = text.split()
            if len(parcalar) < 2:
                send_telegram_message("Kullanım: /hesap_test 2026-07-04 (o günün kapanışıyla "
                                       "BIST hisseleri için toplu karar alır, ertesi günle karşılaştırır)")
            else:
                send_telegram_message(f"🧮 {parcalar[1]} için BIST hisseleri taranıyor, "
                                       f"Gemini'den toplu karar isteniyor...")

                def _arka_plan_test(tarih):
                    try:
                        send_telegram_message(hesap_makinesi_backtest(tarih))
                    except Exception as e:
                        send_telegram_message(f"🧮 Test hata verdi: {e}")
                threading.Thread(target=_arka_plan_test, args=(parcalar[1],), daemon=True).start()
        elif text.startswith("/hesap_rapor"):
            send_telegram_message(build_hesap_rapor())
        elif text.startswith("/hesap_test_seri"):
            parcalar = text.split()[1:]
            if not parcalar:
                send_telegram_message(f"Kullanım: /hesap_test_seri 2026-06-01 2026-06-08 2026-06-15 "
                                       f"(en fazla {MAX_SERI_TARIH} tarih, boşlukla ayrılmış)")
            else:
                send_telegram_message(f"🧮 {len(parcalar)} tarih sırayla test edilecek "
                                       f"(ARKA PLANDA, ana botu bloklamadan), her biri ayrı mesaj "
                                       f"olarak gelecek — birkaç dakika sürebilir...")

                def _arka_plan_seri(tarihler):
                    try:
                        hesap_makinesi_backtest_seri(tarihler)
                    except Exception as e:
                        send_telegram_message(f"🧮 Seri test hatası: {e}")
                threading.Thread(target=_arka_plan_seri, args=(parcalar,), daemon=True).start()
        elif text.startswith("/hesap_tam_test"):
            parcalar = text.split()
            baslangic = parcalar[1] if len(parcalar) > 1 else "2026-01-01"
            bitis = parcalar[2] if len(parcalar) > 2 else None
            send_telegram_message(
                f"🧮 TAM YIL TESTİ başlıyor: {baslangic} → {bitis or 'bugün'}.\n"
                f"29 BIST hissesi × tüm işlem günleri (hafta sonu/resmi tatiller "
                f"otomatik atlanıyor) — ARKA PLANDA çalışıyor, ana botu "
                f"bloklamıyor. Muhtemelen birkaç dakika sürecek, bitince "
                f"sonuçları TEK BİR CSV DOSYASI olarak buraya göndereceğim."
            )

            def _arka_plan_tam_test(b, bt):
                try:
                    dosya_yolu, ozet = hesap_makinesi_tam_yil_testi(b, bt)
                    if dosya_yolu is None:
                        send_telegram_message(f"🧮 Tam yıl testi başarısız: {ozet}")
                        return
                    send_telegram_document(
                        dosya_yolu,
                        caption=(f"🧮 Tam Yıl Testi Sonucu\n"
                                 f"{ozet['gun_sayisi']} işlem günü × {ozet['hisse_sayisi']} hisse\n"
                                 f"Toplam karar: {ozet['n']} (bayat veri/eksik gün otomatik atlandı)\n"
                                 f"Genel doğruluk: %{ozet['dogruluk_pct']} ({ozet['dogru_n']}/{ozet['n']})\n"
                                 f"Dağılım: {ozet['uzun_n']} LONG / {ozet['kisa_n']} SHORT")
                    )
                except Exception as e:
                    send_telegram_message(f"🧮 Tam yıl testi hatası: {e}")
            threading.Thread(target=_arka_plan_tam_test, args=(baslangic, bitis), daemon=True).start()
        elif text.startswith("/gosterge_turnuvasi"):
            parcalar = text.split()
            baslangic = parcalar[1] if len(parcalar) > 1 else "2026-01-01"
            bitis = parcalar[2] if len(parcalar) > 2 else None
            send_telegram_message(
                f"🏆 GÖSTERGE TURNUVASI başlıyor: {baslangic} → {bitis or 'bugün'}.\n"
                f"Hesap makinesinin ağırlıklı toplamı yerine, her göstergeyi TEK "
                f"BAŞINA (ABD swing turnuvasındaki gibi izole) test ediyor — "
                f"29 hisse × tüm işlem günleri. ARKA PLANDA çalışıyor, birkaç "
                f"dakika sürebilir, bitince lider tablosunu CSV olarak göndereceğim."
            )

            def _arka_plan_turnuva(b, bt):
                try:
                    dosya_yolu, ozet = gosterge_turnuvasi_calistir(b, bt)
                    if dosya_yolu is None:
                        send_telegram_message(f"🏆 Gösterge turnuvası başarısız: {ozet}")
                        return
                    send_telegram_document(
                        dosya_yolu,
                        caption=(f"🏆 Gösterge Turnuvası Sonucu\n"
                                 f"{ozet['gun_sayisi']} işlem günü × {ozet['hisse_sayisi']} hisse, "
                                 f"{ozet['toplam_gozlem']} gözlem\n"
                                 f"{ozet['strateji_sayisi']} strateji test edildi\n"
                                 f"🥇 En iyi: {ozet['en_iyi_strateji']}\n"
                                 f"   %{ozet['en_iyi_kazanma_orani']} isabet (n={ozet['en_iyi_n']})")
                    )
                except Exception as e:
                    send_telegram_message(f"🏆 Gösterge turnuvası hatası: {e}")
            threading.Thread(target=_arka_plan_turnuva, args=(baslangic, bitis), daemon=True).start()
        elif text.startswith("/kur_sektor_testi"):
            parcalar = text.split()
            baslangic = parcalar[1] if len(parcalar) > 1 else "2026-01-01"
            bitis = parcalar[2] if len(parcalar) > 2 else None
            send_telegram_message(
                f"💱 KUR/SEKTÖR TESTİ başlıyor: {baslangic} → {bitis or 'bugün'}.\n"
                f"Aynı 13 göstergeyi 3 farklı hedefe karşı test ediyor: ham "
                f"getiri (kıyas), USDTRY arındırılmış getiri, sektör-göreceli "
                f"getiri (8 sektör grubu). ARKA PLANDA çalışıyor, birkaç "
                f"dakika sürebilir, bitince CSV + özet göndereceğim."
            )

            def _arka_plan_kur_sektor(b, bt):
                try:
                    dosya_yolu, ozet = kur_sektor_testi_calistir(b, bt)
                    if dosya_yolu is None:
                        send_telegram_message(f"💱 Kur/Sektör testi başarısız: {ozet}")
                        return
                    send_telegram_document(
                        dosya_yolu,
                        caption=(f"💱 Kur/Sektör Testi Sonucu\n"
                                 f"{ozet['gun_sayisi']} işlem günü × {ozet['hisse_sayisi']} hisse, "
                                 f"{ozet['toplam_gozlem']} gözlem\n"
                                 f"{ozet['strateji_sayisi']} satır (3 hedef × göstergeler)\n"
                                 f"🥇 En iyi: [{ozet['en_iyi_hedef']}] {ozet['en_iyi_strateji']}\n"
                                 f"   %{ozet['en_iyi_kazanma_orani']} isabet (n={ozet['en_iyi_n']}, "
                                 f"p={ozet['en_iyi_p']})")
                    )
                except Exception as e:
                    send_telegram_message(f"💱 Kur/Sektör testi hatası: {e}")
            threading.Thread(target=_arka_plan_kur_sektor, args=(baslangic, bitis), daemon=True).start()
        elif text.startswith("/icgorusel_islem_edgar"):
            parcalar = text.split()
            gun_ufku = int(parcalar[1]) if len(parcalar) > 1 else ICGORUSEL_ISLEM_GUN_UFKU
            send_telegram_message(
                f"👤 EDGAR İÇERİDEN İŞLEM TESTİ başlıyor: SEC EDGAR'dan (yfinance "
                f"yerine) çekiliyor, TÜM {len(US_INSIDER_TICKERS)} hisse taranacak. "
                f"Bu OLDUKÇA YAVAŞ olabilir (her hisse için onlarca Form 4 dosyası "
                f"indiriliyor, muhtemelen 20-30+ dakika). ARKA PLANDA çalışıyor, "
                f"bitince CSV + özet göndereceğim. İstatistik artık aynı gün/aynı "
                f"türdeki tekrar eden işlem satırları için TEKİLLEŞTİRİLMİŞ veriden "
                f"hesaplanıyor (önceki şişme sorunu düzeltildi)."
            )

            def _arka_plan_icgorusel_edgar(gu):
                try:
                    dosya_yolu, ozet = icgorusel_islem_testi_edgar_calistir(gu, max_hisse=None)
                    if dosya_yolu is None:
                        send_telegram_message(f"👤 EDGAR içeriden işlem testi başarısız: {ozet}")
                        return
                    satirlar = [f"👤 EDGAR İçeriden İşlem Testi Sonucu ({ozet['gun_ufku']} gün ufku, tekilleştirilmiş)",
                                f"{ozet['hisse_sayisi']} hisse, toplam benzersiz işlem: {ozet['toplam_islem']}\n"]
                    for g in ozet["gruplar"]:
                        satirlar.append(
                            f"{g['tur']}: n={g['n']}, %{g['kazanma_orani_pct']} doğru yönde "
                            f"(binom p={g['binom_p']}), ort. getiri %{g['ort_getiri_pct']}"
                        )
                    send_telegram_document(dosya_yolu, caption="\n".join(satirlar))
                except Exception as e:
                    send_telegram_message(f"👤 EDGAR içeriden işlem testi hatası: {e}")
            threading.Thread(target=_arka_plan_icgorusel_edgar, args=(gun_ufku,), daemon=True).start()
        elif text.startswith("/pykap_test"):
            send_telegram_message("🔍 pykap bağlantı testi başlıyor...")

            def _arka_plan_pykap_test():
                try:
                    sonuc = pykap_baglanti_testi()
                    send_telegram_message(f"🔍 pykap Test Sonucu:\n\n{sonuc}")
                except Exception as e:
                    send_telegram_message(f"🔍 pykap testi hatası: {e}")
            threading.Thread(target=_arka_plan_pykap_test, daemon=True).start()
        elif text.startswith("/trend_testi"):
            send_telegram_message(
                f"📈 GOOGLE TRENDS TESTİ başlıyor: {len(BIST_TICKERS)} hisse için "
                f"5 yıllık arama ilgisi (haftalık) çekilecek, MOMENTUM ve REVERSAL "
                f"hipotezleri ertesi hafta getirisine karşı test edilecek. pytrends "
                f"hız sınırına takılmamak için hisse başına 2sn bekliyor, bu YAVAŞ "
                f"olabilir (~2-3 dakika+). ARKA PLANDA çalışıyor, bitince CSV + "
                f"özet göndereceğim."
            )

            def _arka_plan_trend_testi():
                try:
                    dosya_yolu, ozet = google_trends_testi_calistir()
                    if dosya_yolu is None:
                        send_telegram_message(f"📈 Google Trends testi başarısız: {ozet}")
                        return
                    satirlar = [f"📈 Google Trends Testi Sonucu",
                                f"{ozet['hisse_sayisi']} hisse, {ozet['toplam_gozlem']} "
                                f"gözlem\n"]
                    for s in ozet["satirlar"]:
                        p_str = f", p={s['binom_p']}" if s["binom_p"] is not None else ""
                        satirlar.append(
                            f"{s['hipotez']}: n={s['n']}, %{s['kazanma_orani_pct']} "
                            f"isabet{p_str}, ort. getiri %{s['ort_isaretli_getiri_pct']}"
                        )
                    send_telegram_document(dosya_yolu, caption="\n".join(satirlar))
                except Exception as e:
                    send_telegram_message(f"📈 Google Trends testi hatası: {e}")
            threading.Thread(target=_arka_plan_trend_testi, daemon=True).start()
        elif text.startswith("/wiki_testi"):
            send_telegram_message(
                f"📖 WIKIPEDIA TESTİ başlıyor: {len(BIST_WIKI_MAKALE)} hisse için "
                f"Wikipedia sayfa görüntülenmesi (günlük, resmi API) çekilecek, "
                f"MOMENTUM/REVERSAL hipotezleri ertesi gün getirisine karşı test "
                f"edilecek. ARKA PLANDA çalışıyor, bitince CSV + özet göndereceğim."
            )

            def _arka_plan_wiki_testi():
                try:
                    dosya_yolu, ozet = wiki_testi_calistir()
                    if dosya_yolu is None:
                        send_telegram_message(f"📖 Wikipedia testi başarısız: {ozet}")
                        return
                    satirlar = [f"📖 Wikipedia Testi Sonucu (4 ufuk: 1g/3g/1hf/2hf)",
                                f"{ozet['hisse_sayisi']} hisse denendi, "
                                f"{ozet['toplam_gozlem']} gözlem\n"
                                f"Detaylı tablo CSV'de - burada sadece MOMENTUM/"
                                f"REVERSAL toplamları:\n"]
                    for s in ozet["satirlar"]:
                        if "MOMENTUM (devam" not in s["hipotez"] and "REVERSAL (tersine" not in s["hipotez"]:
                            continue
                        p_str = f", p={s['binom_p']}" if s["binom_p"] is not None else ""
                        satirlar.append(
                            f"[{s['ufuk']}] {s['hipotez']}: n={s['n']}, "
                            f"%{s['kazanma_orani_pct']} isabet{p_str}"
                        )
                    send_telegram_document(dosya_yolu, caption="\n".join(satirlar))
                except Exception as e:
                    send_telegram_message(f"📖 Wikipedia testi hatası: {e}")
            threading.Thread(target=_arka_plan_wiki_testi, daemon=True).start()
        elif text.startswith("/wiki_dogrulama"):
            send_telegram_message(
                f"✅ WIKI DOĞRULAMA başlıyor: /wiki_testi'nin 2 haftalık bulgusunu "
                f"(yüksek izlenme -> LONG) İZOLE olarak, gerçek R:R çıkışıyla "
                f"(1.5R kısmi TP + trailing) yeniden test ediyor, kör temel çizgiyle "
                f"karşılaştırıyor. ARKA PLANDA çalışıyor, bitince CSV + özet "
                f"göndereceğim."
            )

            def _arka_plan_wiki_dogrulama():
                try:
                    dosya_yolu, ozet = wiki_dogrulama_calistir()
                    if dosya_yolu is None:
                        send_telegram_message(f"✅ Wiki doğrulama başarısız: {ozet}")
                        return
                    satirlar = [f"✅ Wiki Doğrulama Sonucu (eşik: izlenme_orani>={ozet['esik']})\n"]
                    for s in ozet["satirlar"]:
                        p_str = f", p={s['binom_p']}" if s["binom_p"] is not None else ""
                        satirlar.append(
                            f"{s['strateji']}: {s['toplam_sinyal']} sinyal "
                            f"({s['win']}W/{s['loss']}L/{s['timeout']}T), "
                            f"%{s['kazanma_orani_pct']} isabet{p_str}, ort R={s['ort_R']}"
                        )
                    send_telegram_document(dosya_yolu, caption="\n".join(satirlar))
                except Exception as e:
                    send_telegram_message(f"✅ Wiki doğrulama hatası: {e}")
            threading.Thread(target=_arka_plan_wiki_dogrulama, daemon=True).start()
        elif text.startswith("/wiki_kanit_dogrulama"):
            send_telegram_message(
                f"📖✅ WIKI SİNYALİ İZOLE DOĞRULAMA başlıyor: yüksek Wikipedia "
                f"izlenmesi olan günlerde LONG'u, BIST'in gerçek çıkış "
                f"mantığıyla (1.5R kısmi TP + trailing) test ediyor, kontrol "
                f"grubuyla (koşulsuz her gün LONG) karşılaştırıyor. ARKA "
                f"PLANDA çalışıyor, bitince CSV + özet göndereceğim."
            )

            def _arka_plan_wiki_kanit():
                try:
                    dosya_yolu, ozet = wiki_kanit_dogrulama_calistir()
                    if dosya_yolu is None:
                        send_telegram_message(f"📖✅ Wiki kanıt doğrulama başarısız: {ozet}")
                        return
                    satirlar = [f"📖✅ Wiki Sinyali İzole Doğrulama Sonucu",
                                f"Global eşik (üst %20 izlenme oranı): {ozet['esik']}\n"]
                    for s in ozet["satirlar"]:
                        p_str = f", p={s['binom_p']}" if s["binom_p"] is not None else ""
                        satirlar.append(
                            f"{s['strateji']}: {s['toplam_sinyal']} sinyal "
                            f"({s['win']}W/{s['loss']}L/{s['timeout']}T), "
                            f"%{s['kazanma_orani_pct']} isabet{p_str}, ort R={s['ort_R']}"
                        )
                    send_telegram_document(dosya_yolu, caption="\n".join(satirlar))
                except Exception as e:
                    send_telegram_message(f"📖✅ Wiki kanıt doğrulama hatası: {e}")
            threading.Thread(target=_arka_plan_wiki_kanit, daemon=True).start()
        elif text.startswith("/eksisozluk_test"):
            parcalar = text.split()
            baslik = parcalar[1] if len(parcalar) > 1 else "thyao"
            send_telegram_message(f"📚 Ekşi Sözlük bağlantı testi başlıyor ('{baslik}')...")

            def _arka_plan_eksi_test(b):
                try:
                    sonuc = eksisozluk_baglanti_testi(b)
                    send_telegram_message(f"📚 Ekşi Sözlük Test Sonucu:\n\n{sonuc}")
                except Exception as e:
                    send_telegram_message(f"📚 Ekşi Sözlük testi hatası: {e}")
            threading.Thread(target=_arka_plan_eksi_test, args=(baslik,), daemon=True).start()
        elif text.startswith("/kanit_ters_islem"):
            send_telegram_message(
                f"🔄 TERS İŞLEM TESTİ başlıyor: Fitil+RSI+Hacim ve Sadece RSI'ın "
                f"sinyal yönü ters çevrilip (LONG↔SHORT), doğru stop/hedef ile "
                f"SIFIRDAN hesaplanıyor - basit işaret çevirme değil. "
                f"ARKA PLANDA çalışıyor, bitince CSV + özet göndereceğim."
            )

            def _arka_plan_ters_islem():
                try:
                    dosya_yolu, ozet = kanit_ters_islem_testi_calistir()
                    if dosya_yolu is None:
                        send_telegram_message(f"🔄 Ters işlem testi başarısız: {ozet}")
                        return
                    satirlar = ["🔄 Ters İşlem Testi Sonucu\n"]
                    for s in ozet["satirlar"]:
                        p_str = f", p={s['binom_p']}" if s["binom_p"] is not None else ""
                        satirlar.append(
                            f"{s['strateji']}: {s['toplam_sinyal']} sinyal "
                            f"({s['win']}W/{s['loss']}L/{s['timeout']}T), "
                            f"%{s['kazanma_orani_pct']} isabet{p_str}, ort R={s['ort_R']}"
                        )
                    send_telegram_document(dosya_yolu, caption="\n".join(satirlar))
                except Exception as e:
                    send_telegram_message(f"🔄 Ters işlem testi hatası: {e}")
            threading.Thread(target=_arka_plan_ters_islem, daemon=True).start()
        elif text.startswith("/icgorusel_islem"):
            parcalar = text.split()
            gun_ufku = int(parcalar[1]) if len(parcalar) > 1 else ICGORUSEL_ISLEM_GUN_UFKU
            send_telegram_message(
                f"👤 İÇERİDEN İŞLEM TESTİ başlıyor: {len(US_INSIDER_TICKERS)} ABD hissesi, "
                f"her işlemden {gun_ufku} işlem günü sonraki getiriye bakılacak. "
                f"ARKA PLANDA çalışıyor, bitince CSV + özet göndereceğim."
            )

            def _arka_plan_icgorusel(gu):
                try:
                    dosya_yolu, ozet = icgorusel_islem_testi_calistir(gu)
                    if dosya_yolu is None:
                        send_telegram_message(f"👤 İçeriden işlem testi başarısız: {ozet}")
                        return
                    satirlar = [f"👤 İçeriden İşlem Testi Sonucu ({ozet['gun_ufku']} gün ufku)",
                                f"Toplam işlem: {ozet['toplam_islem']}\n"]
                    for g in ozet["gruplar"]:
                        satirlar.append(
                            f"{g['tur']}: n={g['n']}, %{g['kazanma_orani_pct']} doğru yönde "
                            f"(binom p={g['binom_p']}), ort. getiri %{g['ort_getiri_pct']}"
                        )
                    send_telegram_document(dosya_yolu, caption="\n".join(satirlar))
                except Exception as e:
                    send_telegram_message(f"👤 İçeriden işlem testi hatası: {e}")
            threading.Thread(target=_arka_plan_icgorusel, args=(gun_ufku,), daemon=True).start()
        elif text.startswith("/kanit_dogrulama_transfer"):
            send_telegram_message(
                f"🔀 ABD→BIST TRANSFER TESTİ başlıyor: ABD'de kanıtlanmış giriş "
                f"koşullarını (ATR Kırılımı x2.0, Hacim Z-Skor) {len(BIST_TICKERS)} "
                f"BIST hissesinde tetikletip, BIST'in gerçek çıkış mantığıyla "
                f"(1.5R kısmi TP + trailing) sonuçlandırıyor. ARKA PLANDA "
                f"çalışıyor, bitince CSV + özet göndereceğim."
            )

            def _arka_plan_kanit_transfer():
                try:
                    dosya_yolu, ozet = kanit_dogrulama_transfer_calistir()
                    if dosya_yolu is None:
                        send_telegram_message(f"🔀 ABD→BIST transfer testi başarısız: {ozet}")
                        return
                    satirlar = ["🔀 ABD→BIST Transfer Testi Sonucu\n"]
                    for s in ozet["satirlar"]:
                        p_str = f", p={s['binom_p']}" if s["binom_p"] is not None else ""
                        satirlar.append(
                            f"{s['strateji']}: {s['toplam_sinyal']} sinyal "
                            f"({s['win']}W/{s['loss']}L/{s['timeout']}T), "
                            f"%{s['kazanma_orani_pct']} isabet{p_str}, "
                            f"ort R={s['ort_R']}"
                        )
                    send_telegram_document(dosya_yolu, caption="\n".join(satirlar))
                except Exception as e:
                    send_telegram_message(f"🔀 ABD→BIST transfer testi hatası: {e}")
            threading.Thread(target=_arka_plan_kanit_transfer, daemon=True).start()
        elif text.startswith("/kanit_dogrulama"):
            send_telegram_message(
                f"✅ KANIT DOĞRULAMA başlıyor: canlı sistemin kanıtlanmış 4 "
                f"stratejisini ({len(BIST_TICKERS)} BIST + {len(US_INSIDER_TICKERS)} "
                f"ABD hissesi) taze 2 yıllık veriyle, gerçek çıkış mantığıyla "
                f"yeniden test ediyor. Uzun sürebilir (~10-15 dakika), ARKA "
                f"PLANDA çalışıyor, bitince CSV + özet göndereceğim."
            )

            def _arka_plan_kanit():
                try:
                    dosya_yolu, ozet = kanit_dogrulama_calistir()
                    if dosya_yolu is None:
                        send_telegram_message(f"✅ Kanıt doğrulama başarısız: {ozet}")
                        return
                    satirlar = ["✅ Kanıt Doğrulama Sonucu\n"]
                    for s in ozet["satirlar"]:
                        p_str = f", p={s['binom_p']}" if s["binom_p"] is not None else ""
                        satirlar.append(
                            f"{s['strateji']}: {s['toplam_sinyal']} sinyal "
                            f"({s['win']}W/{s['loss']}L/{s['timeout']}T), "
                            f"%{s['kazanma_orani_pct']} isabet{p_str}, "
                            f"ort R={s['ort_R']}"
                        )
                    send_telegram_document(dosya_yolu, caption="\n".join(satirlar))
                except Exception as e:
                    send_telegram_message(f"✅ Kanıt doğrulama hatası: {e}")
            threading.Thread(target=_arka_plan_kanit, daemon=True).start()
        elif text.startswith("/arge_test"):
            send_telegram_message("🧪 Manuel test turu başlatılıyor (arka planda)...")

            def _arka_plan_arge_test():
                try:
                    run_ai_research_cycle()
                    send_telegram_message("🧪 Test turu bitti — onaylıysa yukarıda ayrı mesaj geldi, "
                                           "değilse /arge_rapor ile son denemeyi görebilirsin.")
                except Exception as e:
                    send_telegram_message(f"🧪 Test turu hata verdi: {e}")
            threading.Thread(target=_arka_plan_arge_test, daemon=True).start()
        elif text.startswith("/arge_yardim"):
            send_telegram_message(
                "📖 Ar-Ge Botu komutları (sadece gece radarı için):\n"
                "/arge_rapor — şimdiye kadarki tüm denemelerin özeti\n"
                "/hesap_makinesi TICKER — Gemini'ye ANLIK göstergeleri gösterip LONG/SHORT yorumu ister\n"
                "/hesap_sonuclari — anlık hesap makinesi yorumlarının isabet takibi\n"
                "/hesap_test TARİH — geçmiş bir tarihte (örn. 2026-07-04) BIST hisseleri için "
                "toplu karar aldırıp GERÇEK ertesi günle hemen karşılaştırır\n"
                "/hesap_rapor — /hesap_test ile şimdiye kadar biriken toplam isabet oranı (LONG/SHORT ayrımıyla)\n"
                f"/hesap_test_seri TARİH1 TARİH2 ... — birden fazla tarihi arka arkaya test eder (en fazla {MAX_SERI_TARIH})\n"
                "/arge_test — hemen bir hipotez dener (test amaçlı)\n"
                "/arge_yardim — bu liste"
            )

    if offset is not None:
        try:
            with open(CMD_OFFSET_FILE, "w") as f:
                f.write(str(offset))
        except Exception:
            pass
