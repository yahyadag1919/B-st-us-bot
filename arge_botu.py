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
import socket

# SÜRÜM ETİKETİ - Render'da hangi kodun gerçekten çalıştığını Telegram
# mesajlarında görünür kılmak için (2026-08-17: 3 kez üst üste "aynı
# sonuç geldi" şüphesi sonrası eklendi - deploy'un gerçekten güncel
# olup olmadığını KANITLA göstermek için).
ARGE_KOD_SURUMU = "v62-buyuk-patlama-gunu-testi-2026-08-19"
import warnings
from datetime import datetime, timezone

import numpy as np
import pandas as pd
from scipy import stats as _stats
import requests

# 2026-08-19 GERİ ALINDI: socket.setdefaulttimeout(30) buraya eklenmişti
# (DNS donmalarına karşı son çare olarak) ama kullanıcı, TAM O NOKTADAN
# SONRA botun Render'da "uyumaya" başladığını fark etti - bu ayar GLOBAL
# olduğu için muhtemelen Flask'ın KENDİ sunucu soketini de etkileyip
# onu bozmuştu (tahmini bir risk olarak baştan not edilmişti, gerçekleşmiş
# görünüyor). Kaldırıldı - DNS donmalarına karşı savunma artık sadece
# ThreadPoolExecutor'ın sert zaman aşımına (120sn) ve her isteğin kendi
# requests(timeout=...) parametresine dayanıyor.

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
        df15 = yf.Ticker(ticker).history(period="60d", interval="15m", timeout=20)
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
    index_df = yf.Ticker("XU100.IS").history(period="2y", interval="1d", timeout=20)
    index_pct = None
    if index_df is not None and not index_df.empty:
        index_df.index = pd.to_datetime(index_df.index).tz_localize(None)
        index_pct = (index_df["Close"] - index_df["Close"].shift(1)) / index_df["Close"].shift(1) * 100

    parcalar = []
    for ticker in ALL_TICKERS:
        try:
            df = yf.Ticker(ticker).history(period="2y", interval="1d", timeout=20)
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
            df15 = yf.Ticker(ticker).history(period="60d", interval="15m", timeout=20)
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
        index_df = yf.Ticker("XU100.IS").history(period="2y", interval="1d", timeout=20)
        index_pct = None
        if index_df is not None and not index_df.empty:
            index_df.index = pd.to_datetime(index_df.index).tz_localize(None)
            index_pct = (index_df["Close"] - index_df["Close"].shift(1)) / index_df["Close"].shift(1) * 100

        df = yf.Ticker(ticker).history(period="2y", interval="1d", timeout=20)
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

    index_df = yf.Ticker("XU100.IS").history(period="2y", interval="1d", timeout=20)
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
            df = yf.Ticker(ticker).history(period="2y", interval="1d", timeout=20)
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

    index_df = yf.Ticker("XU100.IS").history(period="2y", interval="1d", timeout=20)
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
            df = yf.Ticker(ticker).history(period="2y", interval="1d", timeout=20)
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

    index_df = yf.Ticker("XU100.IS").history(period="2y", interval="1d", timeout=20)
    if index_df is None or index_df.empty:
        return None, "XU100 endeks verisi çekilemedi."
    index_df.index = pd.to_datetime(index_df.index).tz_localize(None)
    index_pct = (index_df["Close"] - index_df["Close"].shift(1)) / index_df["Close"].shift(1) * 100

    kur_df = yf.Ticker("USDTRY=X").history(period="2y", interval="1d", timeout=20)
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
            df = yf.Ticker(ticker).history(period="2y", interval="1d", timeout=20)
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


def _yf_history_sert_zaman_asimli(ticker: str, period: str, interval: str, sert_sure: int = 30):
    """yfinance'in history() çağrısını SERT bir zaman aşımıyla sarar -
    2026-08-19: EDGAR testinde bulduğumuz aynı sorun (bazı ağ çağrıları
    normal timeout parametresine RAĞMEN donabiliyor, muhtemelen DNS
    seviyesinde) diğer testlerde de (özellikle gün-içi/sıkışma
    turnuvaları) yaşandı - hiç ThreadPoolExecutor koruması yoktu. Bu
    fonksiyon, o korumayı TEK bir yerden tüm yeni testlere kazandırıyor."""
    import concurrent.futures
    import yfinance as yf

    def _cek():
        return yf.Ticker(ticker).history(period=period, interval=interval, timeout=20)

    executor = concurrent.futures.ThreadPoolExecutor(max_workers=1)
    try:
        gelecek = executor.submit(_cek)
        return gelecek.result(timeout=sert_sure)
    except concurrent.futures.TimeoutError:
        print(f"[yfinance SERT zaman aşımı] {ticker} ({period}/{interval}) "
              f"{sert_sure}sn'yi aştı, atlanıyor.", flush=True)
        return None
    finally:
        executor.shutdown(wait=False)


def _edgar_tek_hisse_isle(ticker: str, cik: str, gun_ufku: int) -> list:
    """Bir hissenin TÜM EDGAR işlemesini yapar (fiyat çekme + Form4 listesi
    + detaylar) - ThreadPoolExecutor ile SERT bir zaman aşımına sarılacak,
    bu yüzden fonksiyonun İÇİNDE NEREDE takılırsa takılsın (fiyat çekme,
    form4 listesi, form4 detayı - hepsi) dışarıdan zorla durdurulabilir.
    2026-08-19 EKLENDİ: ince taneli teşhis izleri - kullanıcı 120sn'lik
    sert zaman aşımına RAĞMEN 40+ dakika donma yaşadı, bu iz satırları
    hangi ADIMDA (fiyat/liste/detay) gerçekten takıldığını gösterecek."""
    import yfinance as yf
    kayitlar = []
    print(f"[EDGAR TEŞHİS] {ticker}: fiyat verisi çekiliyor...", flush=True)
    fiyat_df = yf.Ticker(ticker).history(period="2y", interval="1d", timeout=20)
    print(f"[EDGAR TEŞHİS] {ticker}: fiyat verisi geldi.", flush=True)
    if fiyat_df is None or fiyat_df.empty:
        return kayitlar
    fiyat_df = fiyat_df.rename(columns={"Close": "close"})
    fiyat_df.index = pd.to_datetime(fiyat_df.index).tz_localize(None)

    print(f"[EDGAR TEŞHİS] {ticker}: Form4 listesi çekiliyor...", flush=True)
    form4ler = _edgar_form4_listesi(cik, ticker)
    print(f"[EDGAR TEŞHİS] {ticker}: Form4 listesi geldi ({len(form4ler)} dosya).", flush=True)
    time.sleep(1.0)
    # 2026-08-19 DÜZELTME: kullanıcı defalarca, ÇOK SAYIDA Form4 dosyası
    # olan hisselerde (600+ dosya - INTC/WMT/NFLX) döngünün SONLARINA
    # doğru (25-30. dosya civarı) tam donma yaşadı - hiçbir Python/soket
    # seviyesi zaman aşımı yardımcı olmadı. Bu örüntü, SEC EDGAR'ın
    # kendisinin bizi "sert hata" yerine YAVAŞÇA CEZALANDIRDIĞINI
    # düşündürüyor (bağlantıyı damla damla akıtarak). Çözüm: SEC'e daha
    # AZ yük bindirmek - hem istek arası bekleme (0.15sn->1sn) hem de
    # hisse başına en fazla dosya sayısı (30->10) azaltıldı.
    for i, f in enumerate(form4ler[:10]):
        print(f"[EDGAR TEŞHİS] {ticker}: detay {i+1}/{min(10,len(form4ler))} çekiliyor "
              f"(accession={f.get('accession')})...", flush=True)
        detaylar = _edgar_form4_detay(cik, f["accession"])
        print(f"[EDGAR TEŞHİS] {ticker}: detay {i+1} geldi.", flush=True)
        time.sleep(1.0)
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
    print(f"[EDGAR TEŞHİS] {ticker}: TÜM işlem tamamlandı, {len(kayitlar)} kayıt.", flush=True)
    return kayitlar


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
    yazılıyor (şeffaflık için) ama özet/anlamlılık TEKİL veriden.
    2026-08-19 İKİNCİ DÜZELTME: kullanıcı iki ayrı denemede iki farklı
    hissede (AVGO 20/106, sonra AMD 27/106) saatlerce donma yaşadı - ilk
    düzeltme (yfinance timeout + iç döngü zaman bütçesi) yetersiz kaldı
    çünkü _edgar_form4_listesi() çağrısının KENDİSİ korumasızdı. Artık
    HER HİSSENİN TÜM işlemesi ThreadPoolExecutor ile SERT bir zaman
    aşımına (120sn) sarılı - fonksiyonun içinde NEREDE takılırsa takılsın
    (hangi çağrı olursa olsun) 120sn sonra zorla vazgeçilip sonraki
    hisseye geçiliyor, bir daha sonsuza kadar donma OLAMAZ."""
    import concurrent.futures
    cik_haritasi = _edgar_cik_haritasi()
    if not cik_haritasi:
        return None, "SEC CIK haritası çekilemedi."

    HISSE_SERT_ZAMAN_ASIMI_SANIYE = 120
    kayitlar = []
    hisseler = US_INSIDER_TICKERS if max_hisse is None else US_INSIDER_TICKERS[:max_hisse]
    for n_i, ticker in enumerate(hisseler, 1):
        cik = cik_haritasi.get(ticker)
        if not cik:
            continue
        print(f"[EDGAR İçeriden İşlem {n_i}/{len(hisseler)}] {ticker}...", flush=True)
        # 2026-08-19 KRİTİK DÜZELTME: `with ThreadPoolExecutor(...)` kalıbı
        # KULLANILMIYOR bilerek - `with` bloğu çıkışta executor.shutdown(wait=True)
        # çağırır, bu da SIKIŞMIŞ thread'in bitmesini BEKLER, yani
        # .result(timeout=...) zaman aşımına uğrasa bile with bloğunun
        # kendisi yine sonsuza kadar takılırdı. shutdown(wait=False) ile
        # takılan thread arka planda "sızdırılıyor" (zararsız, tek
        # seferlik) ama ana döngü ASLA beklemeden ilerliyor.
        executor = concurrent.futures.ThreadPoolExecutor(max_workers=1)
        try:
            print(f"[EDGAR TEŞHİS] {ticker}: executor'a gönderiliyor "
                  f"(sert sınır: {HISSE_SERT_ZAMAN_ASIMI_SANIYE}sn)...", flush=True)
            gelecek = executor.submit(_edgar_tek_hisse_isle, ticker, cik, gun_ufku)
            hisse_kayitlari = gelecek.result(timeout=HISSE_SERT_ZAMAN_ASIMI_SANIYE)
            print(f"[EDGAR TEŞHİS] {ticker}: sonuç geldi, {len(hisse_kayitlari)} kayıt.", flush=True)
            kayitlar.extend(hisse_kayitlari)
        except concurrent.futures.TimeoutError:
            print(f"[EDGAR İçeriden İşlem] {ticker}: SERT zaman aşımı "
                  f"({HISSE_SERT_ZAMAN_ASIMI_SANIYE}sn) - zorla vazgeçildi, "
                  f"sonraki hisseye geçiliyor.", flush=True)
        except Exception as e:
            print(f"[EDGAR İçeriden İşlem] {ticker} hata: {e}", flush=True)
        finally:
            executor.shutdown(wait=False)
            print(f"[EDGAR TEŞHİS] {ticker}: executor kapatıldı, sonraki hisseye geçiliyor.", flush=True)
        time.sleep(2.0)  # 2026-08-19: hisseler arasi da ekstra nefes payi

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
            df = yf.Ticker(ticker).history(period="2y", interval="1d", timeout=20)
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
            df = yf.Ticker(ticker).history(period="2y", interval="1d", timeout=20)
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
            df = yf.Ticker(ticker).history(period="2y", interval="1d", timeout=20)
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
        # 2026-08-19 DÜZELTME: _gun_ici_cikis_sonucu "EOD_KAPANIS" etiketi
        # döndürüyor (gün sonu gerçek kâr/zararla kapanma) - bu daha önce
        # HİÇ tanınmıyordu, "loss" hiç sayılmadığı için isabet oranı her
        # zaman %100 çıkıyordu. Artık R işaretine göre win/loss'a
        # ekleniyor - R sıfırdan büyükse "kazanç sayılan", küçükse
        # "kayıp sayılan" olarak.
        for d, r in kayitlar:
            if d == "EOD_KAPANIS" and r is not None:
                if r >= 0:
                    win += 1
                else:
                    loss += 1
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
            df = yf.Ticker(ticker).history(period="2y", interval="1d", timeout=20)
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

    index_df = yf.Ticker("XU100.IS").history(period="2y", interval="1d", timeout=20)
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
            df = yf.Ticker(ticker).history(period="2y", interval="1d", timeout=20)
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
    df = yf.Ticker(ticker).history(period="2y", interval="1wk", timeout=20)
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

            fiyat_df = yf.Ticker(ticker).history(period="2y", interval="1d", timeout=20)
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
            df = yf.Ticker(ticker).history(period="2y", interval="1d", timeout=20)
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
            raw = yf.Ticker(ticker).history(period="2y", interval="1d", timeout=20)
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
# MAKRO GÜRÜLTÜ TESTİ — ABD/DOLAR GECELİK HAREKETİ BIST'İ NE KADAR AÇIKLIYOR
# =============================================================================
# 2026-08-19 - GEREKÇE: Kullanıcının emir defteri pilotu fikri iyi ama
# "gece haber olmadığı sürece ciddi değişmez" varsayımını test etmemiz
# gerekiyordu - v7'de zaten şüphelenmiştik (bir günde hisselerin %83-96'sı
# aynı yöne gidiyordu) ama bunu hiç SAYISAL olarak ölçmedik. Bu test:
# (1) bir önceki gece S&P500 ne yaptı + USDTRY ne kadar hareket etti,
# (2) bunun o günkü BIST/hisse getirisiyle korelasyonu ne kadar güçlü,
# (3) yön olarak anlamlı şekilde tahmin edilebiliyor mu (binom testi).
# Pratik çıktı: "sakin gece" eşiği için somut, veriye dayalı bir sayı -
# kullanıcının pilotunda elle tahmin etmek yerine kullanabileceği.

MAKRO_MIN_N = 30


def makro_gurultu_testi_calistir() -> tuple:
    """XU100 + BIST_TICKERS'ın günlük getirisini, bir önceki gece S&P500
    ve USDTRY hareketine karşı test eder - korelasyon + yön-tahmin
    anlamlılığı + 'sakin gece' eşiği önerisi. Döner: (dosya_yolu,
    özet_dict) ya da (None, hata_mesajı)."""
    import yfinance as yf

    sp500 = yf.Ticker("^GSPC").history(period="2y", interval="1d", timeout=20)
    usdtry = yf.Ticker("USDTRY=X").history(period="2y", interval="1d", timeout=20)
    xu100 = yf.Ticker("XU100.IS").history(period="2y", interval="1d", timeout=20)
    if sp500 is None or sp500.empty or usdtry is None or usdtry.empty or xu100 is None or xu100.empty:
        return None, "S&P500/USDTRY/XU100 verisi çekilemedi."

    for df in (sp500, usdtry, xu100):
        df.index = pd.to_datetime(df.index).tz_localize(None)
    sp500_getiri = sp500["Close"].pct_change() * 100
    usdtry_getiri = usdtry["Close"].pct_change() * 100
    xu100_getiri = xu100["Close"].pct_change() * 100

    # 1. AŞAMA: XU100 (endeks geneli) için makro-öncesi/sonrası test
    ortak_df = pd.DataFrame({
        "sp500_onceki_gece": sp500_getiri,
        "usdtry_onceki_gece": usdtry_getiri,
        "xu100_bugun": xu100_getiri,
    }).dropna()
    # bir onceki ABD/kur hareketini BIR GUN KAYDIRIP bugunku XU100 ile hizala
    ortak_df["sp500_onceki_gece"] = ortak_df["sp500_onceki_gece"].shift(1)
    ortak_df["usdtry_onceki_gece"] = ortak_df["usdtry_onceki_gece"].shift(1)
    ortak_df = ortak_df.dropna()

    korelasyon_sp500 = round(float(ortak_df["sp500_onceki_gece"].corr(ortak_df["xu100_bugun"])), 4)
    korelasyon_usdtry = round(float(ortak_df["usdtry_onceki_gece"].corr(ortak_df["xu100_bugun"])), 4)

    # yon tahmini: sp500 dustuyse XU100 de dusecek mi (binom testi)
    yon_satirlari = []
    for isim, kolon, beklenen_yon in [
        ("S&P500 geceki yön -> XU100 bugün aynı yön mü", "sp500_onceki_gece", 1),
        ("USDTRY geceki yön -> XU100 bugün TERS yön mü (kur yükselirse BIST düşer varsayımı)",
         "usdtry_onceki_gece", -1),
    ]:
        alt = ortak_df[ortak_df[kolon] != 0]
        if len(alt) < MAKRO_MIN_N:
            continue
        dogru = np.sign(alt[kolon]) * beklenen_yon == np.sign(alt["xu100_bugun"])
        dogru_n = int(dogru.sum())
        yon_satirlari.append({
            "tur": "XU100 (endeks geneli)", "hipotez": isim, "n": len(alt),
            "dogru_n": dogru_n, "kazanma_orani_pct": round(dogru.mean() * 100, 2),
            "binom_p": _binom_p(dogru_n, len(alt)),
        })

    # 2. AŞAMA: "sakin gece" esigi onerisi - ceyreklik dilimler
    sp500_ceyrekler = ortak_df["sp500_onceki_gece"].abs().quantile([0.25, 0.5, 0.75]).round(3).to_dict()
    usdtry_ceyrekler = ortak_df["usdtry_onceki_gece"].abs().quantile([0.25, 0.5, 0.75]).round(3).to_dict()

    # sakin vs hareketli gece bolumlemesi - medyan altı/üstü ile XU100 getiri STD karsilastirmasi
    medyan_sp500 = ortak_df["sp500_onceki_gece"].abs().median()
    medyan_usdtry = ortak_df["usdtry_onceki_gece"].abs().median()
    sakin = ortak_df[(ortak_df["sp500_onceki_gece"].abs() <= medyan_sp500) &
                      (ortak_df["usdtry_onceki_gece"].abs() <= medyan_usdtry)]
    hareketli = ortak_df[(ortak_df["sp500_onceki_gece"].abs() > medyan_sp500) |
                          (ortak_df["usdtry_onceki_gece"].abs() > medyan_usdtry)]

    ozet_satirlari = [{
        "olcum": "Korelasyon: S&P500 (geceki) <-> XU100 (bugün)", "deger": korelasyon_sp500
    }, {
        "olcum": "Korelasyon: USDTRY (geceki) <-> XU100 (bugün)", "deger": korelasyon_usdtry
    }, {
        "olcum": "SAKİN gece grubu: XU100 getiri std sapması (n=" + str(len(sakin)) + ")",
        "deger": round(float(sakin["xu100_bugun"].std()), 4) if len(sakin) > 5 else None
    }, {
        "olcum": "HAREKETLİ gece grubu: XU100 getiri std sapması (n=" + str(len(hareketli)) + ")",
        "deger": round(float(hareketli["xu100_bugun"].std()), 4) if len(hareketli) > 5 else None
    }, {
        "olcum": "ÖNERİLEN 'sakin gece' eşiği: |S&P500 geceki| <= X%",
        "deger": sp500_ceyrekler.get(0.5)
    }, {
        "olcum": "ÖNERİLEN 'sakin gece' eşiği: |USDTRY geceki| <= X%",
        "deger": usdtry_ceyrekler.get(0.5)
    }]

    # 3. AŞAMA: hisse bazlı - kaç hissenin o gün ayni yonde hareket ettigi
    # (v7'deki "hisselerin %83-96'si ayni yonde" gozlemini SAYISALLASTIRIYOR)
    hisse_getirileri = []
    for ticker in BIST_TICKERS:
        try:
            df = yf.Ticker(ticker).history(period="2y", interval="1d", timeout=20)
            if df is None or df.empty:
                continue
            df.index = pd.to_datetime(df.index).tz_localize(None)
            getiri = df["Close"].pct_change() * 100
            hisse_getirileri.append(getiri.rename(ticker))
        except Exception:
            pass
        time.sleep(0.3)

    ayni_yon_orani_ortalama = None
    if hisse_getirileri:
        hisse_df = pd.concat(hisse_getirileri, axis=1)
        gunluk_pozitif_oran = (hisse_df > 0).sum(axis=1) / hisse_df.notna().sum(axis=1)
        # "ayni yonde hareket" = ya cogu pozitif ya cogu negatif
        ayni_yon = gunluk_pozitif_oran.apply(lambda x: max(x, 1 - x) if pd.notna(x) else np.nan)
        ayni_yon_orani_ortalama = round(float(ayni_yon.mean()) * 100, 2)
        ozet_satirlari.append({
            "olcum": f"Ortalama gün: hisselerin ne kadarı AYNI yönde hareket ediyor "
                     f"({len(hisse_getirileri)} hisse, v7 gözleminin sayısallaştırılmışı)",
            "deger": ayni_yon_orani_ortalama
        })

    dosya_yolu = _data_path("makro_gurultu_testi.csv")
    pd.DataFrame(ozet_satirlari + yon_satirlari).to_csv(dosya_yolu, index=False, encoding="utf-8-sig")

    return dosya_yolu, {
        "korelasyon_sp500": korelasyon_sp500, "korelasyon_usdtry": korelasyon_usdtry,
        "sakin_std": ozet_satirlari[2]["deger"], "hareketli_std": ozet_satirlari[3]["deger"],
        "sp500_esik": sp500_ceyrekler.get(0.5), "usdtry_esik": usdtry_ceyrekler.get(0.5),
        "ayni_yon_orani": ayni_yon_orani_ortalama,
        "yon_satirlari": yon_satirlari,
    }


# =============================================================================
# PİYASA SAPMASI TESTİ — AZINLIĞIN AYRIŞMASI GERÇEK Mİ, GÜRÜLTÜ MÜ
# =============================================================================
# 2026-08-19 - GEREKÇE: makro_gurultu_testi'nin bulgusu üzerine kurulu -
# ortalama bir günde hisselerin %75'i AYNI yönde hareket ediyor (v7
# gözleminin sayısallaştırılmışı). Kullanıcının fikri: piyasa geneli bir
# yöne giderken bu çoğunluğa UYMAYAN azınlık hisseler, gerçek bir
# hisseye-özel sinyal taşıyor olabilir mi? İKİ REKABETÇİ HİPOTEZ:
# DEVAM (sapma gerçek bir nedenden, ertesi gün de kendi yönünde gider)
# vs GERİ DÖNÜŞ (sapma sadece gürültüydü, ertesi gün piyasaya/çoğunluğa
# geri döner - mean reversion). Emir defteri gibi yeni bir veri kaynağı
# GEREKTİRMİYOR - tamamen mevcut BIST_TICKERS günlük verisiyle kuruluyor.

SAPMA_MIN_N = 30


def piyasa_sapmasi_testi_calistir() -> tuple:
    """Her gün piyasa çoğunluğunun yönünü bulur, o günün ÇOĞUNLUĞA
    UYMAYAN (sapan) hisselerini işaretler, DEVAM (kendi yönünde) ve
    GERİ DÖNÜŞ (çoğunluğun yönüne) hipotezlerini ertesi güne karşı test
    eder. Döner: (dosya_yolu, özet_dict) ya da (None, hata_mesajı).
    2026-08-19 DÜZELTME: kullanıcının canlı çalıştırması 2 saatten uzun
    sürüp hiç bitmedi - muhtemelen zaman aşımı olmayan bir yfinance
    çağrısı sonsuza kadar takıldı (aynı deploy'da KOZAL.IS'te tekrarlanan
    'Too Many Requests' zaten görülüyordu) VE eski kod gün×hisse üzerinde
    İKİ AYRI Python döngüsüyle geziniyordu (yavaş). İkisi de düzeltildi:
    her yfinance çağrısına timeout=20 eklendi, çift döngü TAMAMEN
    pandas vektör işlemleriyle değiştirildi (Python seviyesinde tarih×
    hisse döngüsü yok)."""
    import yfinance as yf

    hisse_getirileri = {}
    for n_i, ticker in enumerate(BIST_TICKERS, 1):
        try:
            print(f"[Piyasa Sapması {n_i}/{len(BIST_TICKERS)}] {ticker}...", flush=True)
            df = yf.Ticker(ticker).history(period="2y", interval="1d", timeout=20)
            if df is None or df.empty:
                print(f"[Piyasa Sapması] {ticker}: veri boş, atlanıyor.", flush=True)
                continue
            df.index = pd.to_datetime(df.index).tz_localize(None)
            getiri = df["Close"].pct_change() * 100
            hisse_getirileri[ticker] = getiri
        except Exception as e:
            print(f"[Piyasa Sapması] {ticker} hata: {e}", flush=True)
        time.sleep(0.3)

    if len(hisse_getirileri) < 10:
        return None, "Yeterli hisse verisi toplanamadı."

    print("[Piyasa Sapması] Veri çekme bitti, vektörleştirilmiş analiz başlıyor...", flush=True)

    getiri_df = pd.DataFrame(hisse_getirileri)  # satir: tarih, kolon: ticker
    pozitif_oran = (getiri_df > 0).sum(axis=1) / getiri_df.notna().sum(axis=1)
    piyasa_yonu = pd.Series(
        np.where(pozitif_oran > 0.5, "LONG", np.where(pozitif_oran < 0.5, "SHORT", None)),
        index=getiri_df.index)

    # UZUN FORMATA çevir - tum (tarih,ticker) ciftleri tek seferde,
    # Python dongusu YOK
    getiri_yarin_df = getiri_df.shift(-1)
    bugun_uzun = getiri_df.stack().rename("getiri_bugun").reset_index()
    bugun_uzun.columns = ["tarih", "ticker", "getiri_bugun"]
    yarin_uzun = getiri_yarin_df.stack().rename("getiri_yarin").reset_index()
    yarin_uzun.columns = ["tarih", "ticker", "getiri_yarin"]

    uzun = bugun_uzun.merge(yarin_uzun, on=["tarih", "ticker"], how="inner")
    uzun["piyasa_yonu"] = uzun["tarih"].map(piyasa_yonu)
    uzun = uzun.dropna(subset=["getiri_bugun", "getiri_yarin", "piyasa_yonu"])
    uzun = uzun[uzun["getiri_bugun"] != 0]
    uzun["hisse_yonu_bugun"] = np.where(uzun["getiri_bugun"] > 0, "LONG", "SHORT")

    sapan = uzun[uzun["hisse_yonu_bugun"] != uzun["piyasa_yonu"]]
    uyumlu = uzun[uzun["hisse_yonu_bugun"] == uzun["piyasa_yonu"]]

    if len(sapan) < SAPMA_MIN_N:
        return None, f"Yeterli örneklem büyüklüğü yok (sadece {len(sapan)} sapan gözlem)."

    satirlar = []
    dogru_devam = ((sapan["hisse_yonu_bugun"] == "LONG") & (sapan["getiri_yarin"] > 0)) | \
                  ((sapan["hisse_yonu_bugun"] == "SHORT") & (sapan["getiri_yarin"] < 0))
    dogru_n = int(dogru_devam.sum())
    satirlar.append({
        "hipotez": "DEVAM (sapan hisse kendi yönünde devam eder)",
        "n": len(sapan), "dogru_n": dogru_n,
        "kazanma_orani_pct": round(dogru_devam.mean() * 100, 2),
        "binom_p": _binom_p(dogru_n, len(sapan)),
    })

    dogru_donus = ((sapan["piyasa_yonu"] == "LONG") & (sapan["getiri_yarin"] > 0)) | \
                  ((sapan["piyasa_yonu"] == "SHORT") & (sapan["getiri_yarin"] < 0))
    dogru_n2 = int(dogru_donus.sum())
    satirlar.append({
        "hipotez": "GERİ DÖNÜŞ (sapan hisse piyasa çoğunluğuna döner)",
        "n": len(sapan), "dogru_n": dogru_n2,
        "kazanma_orani_pct": round(dogru_donus.mean() * 100, 2),
        "binom_p": _binom_p(dogru_n2, len(sapan)),
    })

    if len(uyumlu) >= SAPMA_MIN_N:
        dogru_uyumlu = ((uyumlu["hisse_yonu_bugun"] == "LONG") & (uyumlu["getiri_yarin"] > 0)) | \
                       ((uyumlu["hisse_yonu_bugun"] == "SHORT") & (uyumlu["getiri_yarin"] < 0))
        dogru_n3 = int(dogru_uyumlu.sum())
        satirlar.append({
            "hipotez": "[KONTROL] Çoğunlukla UYUMLU hisse kendi yönünde devam eder mi",
            "n": len(uyumlu), "dogru_n": dogru_n3,
            "kazanma_orani_pct": round(dogru_uyumlu.mean() * 100, 2),
            "binom_p": _binom_p(dogru_n3, len(uyumlu)),
        })

    tablo = pd.DataFrame(satirlar)
    dosya_yolu = _data_path("piyasa_sapmasi_testi.csv")
    tablo.to_csv(dosya_yolu, index=False, encoding="utf-8-sig")
    return dosya_yolu, {"toplam_sapan_gozlem": len(sapan), "satirlar": satirlar}


# =============================================================================
# PİYASA SAPMASI — GERÇEK ÇIKIŞ MANTIĞIYLA İZOLE DOĞRULAMA — 2026-08-19
# =============================================================================
# GEREKÇE: piyasa_sapmasi_testi_calistir bulgusu (sapan hisse DEVAM %52.23
# p=0.0083, uyumlu hisse kendi yönünde devam %48.03) küçük ama gerçek bir
# kenar gösterdi. Wikipedia'da öğrendiğimiz gibi basit isabet oranı testi
# yanıltıcı olabiliyor - bunu GERÇEK BIST çıkış mantığıyla (1.5R kısmi TP
# + trailing) ve İKİ GRUBU (sapan vs uyumlu) karşılaştırmalı test ediyor.

def piyasa_sapmasi_kanit_dogrulama_calistir() -> tuple:
    """Sapan ve uyumlu hisseleri, kendi günün yönünde (LONG/SHORT), BIST'in
    gerçek çıkış mantığıyla test eder. Döner: (dosya_yolu, özet_dict) ya da
    (None, hata_mesajı)."""
    import yfinance as yf

    # 1. AŞAMA: her hisse için OHLCV+ATR çek, kapanış getirisini de
    # ayrıca tutup piyasa çoğunluk yönünü hesaplamak için biriktir.
    ticker_veri = {}
    kapanis_getirileri = {}
    for n_i, ticker in enumerate(BIST_TICKERS, 1):
        try:
            print(f"[Sapma Kanıt 1/2 - {n_i}/{len(BIST_TICKERS)}] {ticker}...", flush=True)
            raw = yf.Ticker(ticker).history(period="2y", interval="1d", timeout=20)
            if raw is None or raw.empty or len(raw) < 60:
                continue
            raw = raw.rename(columns={"Open": "open", "High": "high", "Low": "low",
                                       "Close": "close", "Volume": "volume"})
            raw = raw[["open", "high", "low", "close", "volume"]].copy()
            raw.index = pd.to_datetime(raw.index).tz_localize(None)
            kapanis_getirileri[ticker] = raw["close"].pct_change() * 100
            tarihler = raw.index
            df = raw.reset_index(drop=True)
            df = _kanit_compute_indicators(df)
            ticker_veri[ticker] = (tarihler, df)
        except Exception as e:
            print(f"[Sapma Kanıt] {ticker} hata: {e}", flush=True)
        time.sleep(0.3)

    if len(kapanis_getirileri) < 10:
        return None, "Yeterli hisse verisi toplanamadı."

    # 2. AŞAMA: piyasa çoğunluk yönü (vektörleştirilmiş, ayni mantik)
    getiri_df = pd.DataFrame(kapanis_getirileri)
    pozitif_oran = (getiri_df > 0).sum(axis=1) / getiri_df.notna().sum(axis=1)
    piyasa_yonu = pd.Series(
        np.where(pozitif_oran > 0.5, "LONG", np.where(pozitif_oran < 0.5, "SHORT", None)),
        index=getiri_df.index)

    # 3. AŞAMA: her hisse için gerçek RR ile SAPAN vs UYUMLU gruplarını
    # kendi günün yönünde (LONG/SHORT) test et
    tum_sonuclar = {"[SAPAN] Kendi yönünde (azınlık)": [], "[UYUMLU] Kendi yönünde (çoğunluk)": []}
    for n_i, (ticker, (tarihler, df)) in enumerate(ticker_veri.items(), 1):
        try:
            print(f"[Sapma Kanıt 2/2 - {n_i}/{len(ticker_veri)}] {ticker}...", flush=True)
            for idx in range(25, len(df) - 1):
                tarih = tarihler[idx]
                if tarih not in piyasa_yonu.index:
                    continue
                yon_piyasa = piyasa_yonu.loc[tarih]
                if yon_piyasa is None or pd.isna(yon_piyasa):
                    continue
                onceki_kapanis = df.iloc[idx - 1]["close"]
                bugunku_kapanis = df.iloc[idx]["close"]
                if onceki_kapanis == 0:
                    continue
                getiri_bugun = (bugunku_kapanis - onceki_kapanis) / onceki_kapanis * 100
                if getiri_bugun == 0:
                    continue
                hisse_yonu = "LONG" if getiri_bugun > 0 else "SHORT"
                sapan_mi = hisse_yonu != yon_piyasa

                sonuc = _kanit_bist_rr_sonuc(df, idx, hisse_yonu)
                if sonuc is None:
                    continue
                grup = "[SAPAN] Kendi yönünde (azınlık)" if sapan_mi else "[UYUMLU] Kendi yönünde (çoğunluk)"
                tum_sonuclar[grup].append(sonuc)
        except Exception as e:
            print(f"[Sapma Kanıt] {ticker} işleme hatası: {e}", flush=True)

    satirlar = _kanit_ozet_tablosu(tum_sonuclar)
    if not satirlar:
        return None, "Hiçbir grup için yeterli veri üretilemedi."
    tablo = pd.DataFrame(satirlar)
    dosya_yolu = _data_path("piyasa_sapmasi_kanit_dogrulama.csv")
    tablo.to_csv(dosya_yolu, index=False, encoding="utf-8-sig")
    return dosya_yolu, {"satirlar": satirlar}


# =============================================================================
# SEÇENEK 3: XU100 ENDEKS-SEVİYESİ MAKRO ZAMANLAMA TESTİ — 2026-08-19
# =============================================================================
# GEREKÇE: makro_gurultu_testi tek hisse bazında zayıf korelasyon bulmuştu.
# Kullanıcının fikri: belki hisse seçiminde değil, ENDEKSİN KENDİSİNDE
# (XU100 - onlarca hissenin ortalaması, gürültü kısmen iptal olur) bir
# zamanlama sinyeli daha güçlü çıkar. ÇOKLU UFUK (1,3,5,10,20 gün) ile
# test ediliyor - value/makro etkiler genelde daha uzun ufukta belirir.

XU100_UFUKLAR = [(1, "1 gün"), (3, "3 gün"), (5, "1 hafta"), (10, "2 hafta"), (20, "1 ay")]
XU100_MIN_N = 20


def xu100_makro_zamanlama_testi_calistir() -> tuple:
    """S&P500 ve USDTRY'nin geceki hareketini, XU100'ün KENDİSİNİN çoklu
    ufuktaki getirisine karşı test eder. Döner: (dosya_yolu, özet_dict)
    ya da (None, hata_mesajı)."""
    import yfinance as yf

    sp500 = yf.Ticker("^GSPC").history(period="2y", interval="1d", timeout=20)
    usdtry = yf.Ticker("USDTRY=X").history(period="2y", interval="1d", timeout=20)
    xu100 = yf.Ticker("XU100.IS").history(period="2y", interval="1d", timeout=20)
    if sp500 is None or sp500.empty or usdtry is None or usdtry.empty or xu100 is None or xu100.empty:
        return None, "S&P500/USDTRY/XU100 verisi çekilemedi."

    for df in (sp500, usdtry, xu100):
        df.index = pd.to_datetime(df.index).tz_localize(None)
    sp500_getiri = sp500["Close"].pct_change() * 100
    usdtry_getiri = usdtry["Close"].pct_change() * 100

    xu100_kapanis = xu100["Close"]
    ortak_index = xu100_kapanis.index

    satirlar = []
    for gun, etiket in XU100_UFUKLAR:
        xu100_ufuk_getiri = (xu100_kapanis.shift(-gun) - xu100_kapanis) / xu100_kapanis * 100
        birlesik = pd.DataFrame({
            "sp500_onceki_gece": sp500_getiri.reindex(ortak_index).shift(1),
            "usdtry_onceki_gece": usdtry_getiri.reindex(ortak_index).shift(1),
            "xu100_ufuk_getiri": xu100_ufuk_getiri,
        }).dropna()
        if len(birlesik) < XU100_MIN_N:
            continue

        kor_sp = round(float(birlesik["sp500_onceki_gece"].corr(birlesik["xu100_ufuk_getiri"])), 4)
        kor_usd = round(float(birlesik["usdtry_onceki_gece"].corr(birlesik["xu100_ufuk_getiri"])), 4)

        for isim, kolon, beklenen_yon in [
            (f"[{etiket}] S&P500 geceki yön -> XU100 aynı yön mü", "sp500_onceki_gece", 1),
            (f"[{etiket}] USDTRY geceki yön -> XU100 ters yön mü", "usdtry_onceki_gece", -1),
        ]:
            alt = birlesik[birlesik[kolon] != 0]
            if len(alt) < XU100_MIN_N:
                continue
            dogru = np.sign(alt[kolon]) * beklenen_yon == np.sign(alt["xu100_ufuk_getiri"])
            dogru_n = int(dogru.sum())
            satirlar.append({
                "ufuk": etiket, "hipotez": isim, "n": len(alt), "dogru_n": dogru_n,
                "kazanma_orani_pct": round(dogru.mean() * 100, 2),
                "binom_p": _binom_p(dogru_n, len(alt)),
                "korelasyon_sp500": kor_sp, "korelasyon_usdtry": kor_usd,
            })

    if not satirlar:
        return None, "Yeterli örneklem büyüklüğüne ulaşan ufuk bulunamadı."
    tablo = pd.DataFrame(satirlar)
    dosya_yolu = _data_path("xu100_makro_zamanlama_testi.csv")
    tablo.to_csv(dosya_yolu, index=False, encoding="utf-8-sig")
    return dosya_yolu, {"satirlar": satirlar}


# =============================================================================
# SEÇENEK 1: DEĞER/TEMEL ANALİZ (P/E) TESTİ — 2026-08-19
# =============================================================================
# GEREKÇE: Kullanıcının fikri - kısa vadeli fiyat tahmini yerine, ucuz
# (düşük F/K) hisselerin ORTA/UZUN vadede pahalı hisselerden daha iyi
# performans gösterip göstermediğine bakmak - klasik "value" etkisi,
# akademik literatürde gerçek bulguları olan bir alan.
# DÜRÜST SINIR: yfinance'in BIST hisseleri için finansal veri (kazanç/
# EPS geçmişi) kapsamı BELİRSİZ - teknik/fiyat verisi kadar güvenilir
# olmayabilir. Savunmacı kodlandı: veri bulunamayan hisseler atlanıyor,
# hatalar açıkça loglanıyor, sessizce yanlış sonuç üretilmiyor.

DEGER_ESIK_YUZDE = 0.20
DEGER_UFUKLAR = [(21, "1 ay"), (63, "3 ay"), (126, "6 ay")]
DEGER_MIN_N = 20


def _hisse_eps_gecmisi(ticker: str):
    """Çeyreklik EPS (hisse başı kazanç) geçmişini çekmeyi dener - yfinance
    sürümüne göre birkaç farklı özellik adı deneniyor (API zamanla
    değişmiş olabilir)."""
    import yfinance as yf
    t = yf.Ticker(ticker)
    for ozellik_adi in ["quarterly_income_stmt", "quarterly_financials"]:
        try:
            veri = getattr(t, ozellik_adi, None)
            if veri is not None and not veri.empty:
                for satir_adi in ["Diluted EPS", "Basic EPS", "Net Income"]:
                    if satir_adi in veri.index:
                        seri = veri.loc[satir_adi].dropna()
                        if len(seri) >= 4:
                            return seri.sort_index()
        except Exception:
            continue
    return None


def deger_testi_calistir() -> tuple:
    """Her hisse için çeyreklik EPS geçmişinden 12-aylık toplam (trailing)
    EPS hesaplar, F/K oranını (fiyat/EPS) üretir, ucuz (düşük F/K) ve
    pahalı (yüksek F/K) gruplarını çoklu ufukta (1/3/6 ay) karşılaştırır.
    Döner: (dosya_yolu, özet_dict) ya da (None, hata_mesajı)."""
    import yfinance as yf

    parcalar = []
    hisse_atlandi = []
    for n_i, ticker in enumerate(BIST_TICKERS, 1):
        try:
            print(f"[Değer Testi {n_i}/{len(BIST_TICKERS)}] {ticker}...", flush=True)
            eps_seri = _hisse_eps_gecmisi(ticker)
            if eps_seri is None or len(eps_seri) < 4:
                hisse_atlandi.append(ticker)
                continue
            eps_seri.index = pd.to_datetime(eps_seri.index).tz_localize(None)
            trailing_eps = eps_seri.rolling(4).sum().dropna()
            if trailing_eps.empty:
                hisse_atlandi.append(ticker)
                continue

            fiyat_df = yf.Ticker(ticker).history(period="2y", interval="1d", timeout=20)
            if fiyat_df is None or fiyat_df.empty:
                hisse_atlandi.append(ticker)
                continue
            fiyat_df = fiyat_df.rename(columns={"Close": "close"})
            fiyat_df.index = pd.to_datetime(fiyat_df.index).tz_localize(None)

            for rapor_tarihi, eps_deger in trailing_eps.items():
                if eps_deger == 0 or pd.isna(eps_deger):
                    continue
                # BAKISH-AHEAD KORUMASI: bu EPS degeri sadece rapor
                # tarihinden SONRAKI fiyatlarla eslesir (rapordan once
                # piyasa bu degeri bilmiyordu)
                sonraki_gunler = fiyat_df.index[fiyat_df.index > rapor_tarihi]
                if len(sonraki_gunler) < DEGER_UFUKLAR[-1][0] + 1:
                    continue
                giris_tarihi = sonraki_gunler[0]
                giris_konum = fiyat_df.index.get_loc(giris_tarihi)
                giris_fiyat = fiyat_df.iloc[giris_konum]["close"]
                if giris_fiyat == 0:
                    continue
                fk_orani = giris_fiyat / eps_deger
                satir = {"ticker": ticker, "tarih": giris_tarihi.date().isoformat(), "fk_orani": fk_orani}
                gecerli = False
                for gun, etiket in DEGER_UFUKLAR:
                    cikis_konum = giris_konum + gun
                    if cikis_konum >= len(fiyat_df):
                        continue
                    cikis_fiyat = fiyat_df.iloc[cikis_konum]["close"]
                    if pd.isna(cikis_fiyat):
                        continue
                    getiri = (cikis_fiyat - giris_fiyat) / giris_fiyat * 100
                    satir[f"getiri_{gun}g"] = round(getiri, 3)
                    gecerli = True
                if gecerli:
                    parcalar.append(satir)
        except Exception as e:
            print(f"[Değer Testi] {ticker} hata: {e}", flush=True)
            hisse_atlandi.append(ticker)
        time.sleep(0.3)

    print(f"[Değer Testi] Atlanan hisse sayısı (finansal veri yok/yetersiz): "
          f"{len(hisse_atlandi)}/{len(BIST_TICKERS)} - {hisse_atlandi}", flush=True)

    if not parcalar:
        return None, (f"Hiçbir hisse için F/K verisi üretilemedi - yfinance'in BIST "
                       f"finansal veri kapsamı yetersiz olabilir ({len(hisse_atlandi)}/"
                       f"{len(BIST_TICKERS)} hisse atlandı).")

    df_all = pd.DataFrame(parcalar)
    # sadece pozitif F/K (negatif EPS/zarar eden sirket F/K yorumlamasi
    # karmasiklastirir, ayri ele alinmali - basit tutmak icin disarida)
    df_all = df_all[df_all["fk_orani"] > 0]
    if len(df_all) < 100:
        return None, (f"Yeterli veri toplanamadı (sadece {len(df_all)} geçerli F/K "
                       f"gözlemi, {len(hisse_atlandi)}/{len(BIST_TICKERS)} hisse atlandı).")

    satirlar = []
    ucuz_esik = df_all["fk_orani"].quantile(DEGER_ESIK_YUZDE)
    pahali_esik = df_all["fk_orani"].quantile(1 - DEGER_ESIK_YUZDE)
    for gun, etiket in DEGER_UFUKLAR:
        kolon = f"getiri_{gun}g"
        if kolon not in df_all.columns:
            continue
        alt_df = df_all.dropna(subset=[kolon])
        ucuz = alt_df[alt_df["fk_orani"] <= ucuz_esik]
        pahali = alt_df[alt_df["fk_orani"] >= pahali_esik]
        for grup_adi, grup in [(f"[{etiket}] UCUZ (düşük F/K) grubu", ucuz),
                                (f"[{etiket}] PAHALI (yüksek F/K) grubu", pahali)]:
            if len(grup) < DEGER_MIN_N:
                continue
            dogru_n = int((grup[kolon] > 0).sum())
            satirlar.append({
                "hipotez": grup_adi, "n": len(grup), "dogru_n": dogru_n,
                "kazanma_orani_pct": round((grup[kolon] > 0).mean() * 100, 2),
                "binom_p": _binom_p(dogru_n, len(grup)),
                "ort_getiri_pct": round(grup[kolon].mean(), 4),
            })

    if not satirlar:
        return None, "Yeterli örneklem büyüklüğüne ulaşan ufuk/grup bulunamadı."
    tablo = pd.DataFrame(satirlar)
    dosya_yolu = _data_path("deger_testi.csv")
    tablo.to_csv(dosya_yolu, index=False, encoding="utf-8-sig")
    return dosya_yolu, {
        "hisse_sayisi": len(BIST_TICKERS) - len(hisse_atlandi), "atlanan_hisse": len(hisse_atlandi),
        "toplam_gozlem": len(df_all), "satirlar": satirlar,
    }


# =============================================================================
# GERÇEK AI MODEL BACKTEST — model.pkl + overnight_model.pkl — 2026-08-19
# =============================================================================
# GEREKÇE: Kullanıcı bu iki modelin (gerçek dosyalar, XGBClassifier
# doğrulandı) canlı performansını sordu ama Render deploy'ları CSV
# takibini sürekli sıfırlamış - kalıcı bir geçmiş yok. Bu, ml_radar.py
# ve overnight_radar.py'den BİREBİR AYNI özellik hesaplama mantığını
# kullanarak, GERÇEK modellerle, GERÇEK 15dk geçmiş veride bağımsız bir
# backtest kuruyor - deploy sıfırlamalarından etkilenmez.
# DÜRÜST SINIR 1: yfinance'in 15dk verisi sadece SON 60 GÜNLE sınırlı -
# daha eski bir backtest yapılamıyor.
# DÜRÜST SINIR 2: has_catalyst (KAP katalizörü) geçmişe dönük olarak
# hesaplanamıyor (kap_monitor.py'nin canlı log dosyası yok/geçmişi kısa) -
# HER ZAMAN 0 varsayılıyor. Bu, canlıdaki gerçek davranıştan bir sapma -
# modelin canlı skorları biraz farklı çıkabilir.
# DÜRÜST SINIR 3: Basitlik için günde BİR TARAMA (günün sonuna yakın)
# simüle ediliyor - ml_radar.py canlıda günde birden fazla kez tarıyor,
# bu farkı azaltır ama tam eşleşme değildir.

AI_BACKTEST_MODEL_PATH = os.environ.get("ML_MODEL_PATH", "model.pkl")
AI_BACKTEST_OVERNIGHT_MODEL_PATH = os.environ.get("OVERNIGHT_MODEL_PATH", "overnight_model.pkl")
AI_BACKTEST_FEATURES_ML = ["volume_factor", "rsi", "price_change_pct", "gap_pct", "cmf", "has_catalyst"]
AI_BACKTEST_FEATURES_OVERNIGHT = AI_BACKTEST_FEATURES_ML + ["close_to_high_ratio"]
AI_BACKTEST_MIN_N = 20


def _ai_backtest_rsi(close, n=14):
    delta = close.diff()
    gain = delta.clip(lower=0)
    loss = -delta.clip(upper=0)
    avg_gain = gain.ewm(alpha=1 / n, adjust=False).mean()
    avg_loss = loss.ewm(alpha=1 / n, adjust=False).mean()
    rs = avg_gain / avg_loss.replace(0, np.nan)
    return 100 - (100 / (1 + rs))


def _ai_backtest_cmf(high, low, close, volume, n=20):
    rng = (high - low).replace(0, np.nan)
    mfm = ((close - low) - (high - close)) / rng
    mfv = mfm * volume
    return mfv.rolling(n).sum() / volume.rolling(n).sum()


def _ai_backtest_ozellikler_gun_sonu(df_15m: pd.DataFrame, gun) -> dict:
    """build_live_features (ml_radar.py/overnight_radar.py) ile BİREBİR
    AYNI mantık - ama canlı değil, GEÇMİŞ bir günün 15dk barlarından.
    has_catalyst HER ZAMAN 0 (bkz. yukarıdaki DÜRÜST SINIR 2)."""
    gun_barlari = df_15m[df_15m["session"] == gun]
    if len(gun_barlari) < 3:
        return None
    acilis = float(gun_barlari.iloc[0]["open"])
    if acilis <= 0:
        return None
    fiyat = float(gun_barlari.iloc[-1]["close"])
    pct_change = (fiyat - acilis) / acilis * 100
    gap_percent = pct_change

    tum_gunler = sorted(df_15m["session"].unique())
    if gun in tum_gunler and tum_gunler.index(gun) > 0:
        onceki_gun = tum_gunler[tum_gunler.index(gun) - 1]
        onceki_kapanis_barlari = df_15m[df_15m["session"] == onceki_gun]
        if not onceki_kapanis_barlari.empty:
            prev_close = float(onceki_kapanis_barlari.iloc[-1]["close"])
            if prev_close:
                gap_percent = (acilis - prev_close) / prev_close * 100

    df_su_ana_kadar = df_15m[df_15m.index <= gun_barlari.index[-1]]
    vol_ma = df_su_ana_kadar["volume"].tail(20 * 8).mean()
    volume_factor = float(gun_barlari.iloc[-1]["volume"] / vol_ma) if vol_ma else np.nan

    rsi_seri = _ai_backtest_rsi(df_su_ana_kadar["close"], 14)
    rsi14 = float(rsi_seri.iloc[-1]) if not rsi_seri.empty else np.nan

    cmf_seri = _ai_backtest_cmf(df_su_ana_kadar["high"], df_su_ana_kadar["low"],
                                 df_su_ana_kadar["close"], df_su_ana_kadar["volume"])
    cmf = float(cmf_seri.iloc[-1]) if not cmf_seri.empty else np.nan

    gun_high = float(gun_barlari["high"].max())
    gun_low = float(gun_barlari["low"].min())
    close_to_high_ratio = (fiyat - gun_low) / (gun_high - gun_low) if (gun_high - gun_low) > 0 else 0.5

    return {
        "fiyat": fiyat, "pct_change": pct_change, "gap_percent": gap_percent,
        "volume_factor": volume_factor, "rsi14": rsi14, "cmf": cmf,
        "has_catalyst": 0, "close_to_high_ratio": close_to_high_ratio,
    }


def ai_model_ozellik_onem_raporu() -> str:
    """İki modelin (model.pkl, overnight_model.pkl) her özelliğe (volume_factor,
    rsi, has_catalyst vb.) ne kadar önem verdiğini (feature_importances_)
    yazdırır - has_catalyst'in baskın olup olmadığını görmek için, çünkü
    backtest'te bu özellik her zaman 0 (geçmişe dönük KAP verisi yok)."""
    try:
        import joblib
    except ImportError:
        return "joblib kurulu değil."
    satirlar = []
    for isim, yol, feat_cols in [
        ("ml_radar (model.pkl)", AI_BACKTEST_MODEL_PATH, AI_BACKTEST_FEATURES_ML),
        ("overnight (overnight_model.pkl)", AI_BACKTEST_OVERNIGHT_MODEL_PATH, AI_BACKTEST_FEATURES_OVERNIGHT),
    ]:
        satirlar.append(f"\n📊 {isim}:")
        try:
            m = joblib.load(yol)
            onemler = getattr(m, "feature_importances_", None)
            if onemler is None:
                satirlar.append("  feature_importances_ bulunamadı.")
                continue
            for feat, onem in sorted(zip(feat_cols, onemler), key=lambda x: -x[1]):
                satirlar.append(f"  {feat}: {onem:.4f}")
        except Exception as e:
            satirlar.append(f"  Yüklenemedi: {e}")
    return "\n".join(satirlar)


def ai_model_gercek_backtest_calistir(max_hisse: int = 20) -> tuple:
    """model.pkl (ml_radar) ve overnight_model.pkl'yi GERÇEKTEN yükleyip,
    son 60 günün 15dk verisiyle GÜNLÜK bazda özellik üretip, GERÇEK
    predict_proba skorunu, GERÇEK ertesi-gün-+%2 sonucuna karşı test
    eder. Döner: (dosya_yolu, özet_dict) ya da (None, hata_mesajı)."""
    import yfinance as yf
    try:
        import joblib
    except ImportError:
        return None, "joblib kurulu değil."

    modeller = {}
    for isim, yol, feat_cols in [
        ("ml_radar (model.pkl)", AI_BACKTEST_MODEL_PATH, AI_BACKTEST_FEATURES_ML),
        ("overnight (overnight_model.pkl)", AI_BACKTEST_OVERNIGHT_MODEL_PATH, AI_BACKTEST_FEATURES_OVERNIGHT),
    ]:
        try:
            m = joblib.load(yol)
            modeller[isim] = (m, feat_cols)
            print(f"[AI Backtest] {isim} yüklendi.", flush=True)
        except Exception as e:
            print(f"[AI Backtest] {isim} yüklenemedi: {e}", flush=True)

    if not modeller:
        return None, "Hiçbir model dosyası yüklenemedi (repo'da model.pkl/overnight_model.pkl var mı kontrol et)."

    hisseler = BIST_TICKERS[:max_hisse]
    kayitlar = {isim: [] for isim in modeller}

    for n_i, ticker in enumerate(hisseler, 1):
        try:
            print(f"[AI Backtest {n_i}/{len(hisseler)}] {ticker}...", flush=True)
            df = yf.Ticker(ticker).history(period="60d", interval="15m", timeout=20)
            if df is None or df.empty:
                continue
            df = df.reset_index().rename(columns={
                "Datetime": "ts", "Date": "ts", "Open": "open", "High": "high",
                "Low": "low", "Close": "close", "Volume": "volume"})
            need = ["ts", "open", "high", "low", "close", "volume"]
            if not all(c in df.columns for c in need):
                continue
            df = df[need].copy()
            df["session"] = pd.to_datetime(df["ts"]).dt.date

            gunler = sorted(df["session"].unique())
            if len(gunler) < 10:
                continue

            for gun_idx in range(5, len(gunler) - 1):  # ilk birkac gun gostergeler icin isinma
                gun = gunler[gun_idx]
                ham = _ai_backtest_ozellikler_gun_sonu(df, gun)
                if ham is None:
                    continue
                if any(v is None or (isinstance(v, float) and np.isnan(v)) for v in ham.values()):
                    continue

                sonraki_gun = gunler[gun_idx + 1]
                sonraki_gun_barlari = df[df["session"] == sonraki_gun]
                if sonraki_gun_barlari.empty:
                    continue
                giris_fiyat = ham["fiyat"]
                en_yuksek_sonraki = float(sonraki_gun_barlari["high"].max())
                basari = (en_yuksek_sonraki - giris_fiyat) / giris_fiyat * 100 >= 2.0

                for isim, (model, feat_cols) in modeller.items():
                    feat_map = {"volume_factor": ham["volume_factor"], "rsi": ham["rsi14"],
                                "price_change_pct": ham["pct_change"], "gap_pct": ham["gap_percent"],
                                "cmf": ham["cmf"], "has_catalyst": ham["has_catalyst"],
                                "close_to_high_ratio": ham["close_to_high_ratio"]}
                    X = pd.DataFrame([[feat_map[c] for c in feat_cols]], columns=feat_cols)
                    try:
                        proba = float(model.predict_proba(X)[0][1])
                    except Exception as e:
                        print(f"[AI Backtest] {isim} predict hatası ({ticker}): {e}", flush=True)
                        continue
                    kayitlar[isim].append({"ticker": ticker, "tarih": str(gun),
                                            "ai_skor": round(proba, 4), "basari": basari})
        except Exception as e:
            print(f"[AI Backtest] {ticker} hata: {e}", flush=True)
        time.sleep(0.3)

    tum_satirlar = []
    for isim, kayit_listesi in kayitlar.items():
        if not kayit_listesi:
            continue
        df_k = pd.DataFrame(kayit_listesi)
        # kor temel cizgi (tum taranan gozlemler)
        kor_dogru = int(df_k["basari"].sum())
        tum_satirlar.append({
            "model": isim, "esik": "[KÖR] tüm gözlemler", "n": len(df_k),
            "basari_n": kor_dogru, "basari_orani_pct": round(df_k["basari"].mean() * 100, 2),
            "binom_p": _binom_p(kor_dogru, len(df_k)),
        })
        for esik in [0.5, 0.6, 0.7, 0.8]:
            secilen = df_k[df_k["ai_skor"] >= esik]
            if len(secilen) < AI_BACKTEST_MIN_N:
                continue
            dogru_n = int(secilen["basari"].sum())
            tum_satirlar.append({
                "model": isim, "esik": f"AI skor >= {esik}", "n": len(secilen),
                "basari_n": dogru_n, "basari_orani_pct": round(secilen["basari"].mean() * 100, 2),
                "binom_p": _binom_p(dogru_n, len(secilen)),
            })

    if not tum_satirlar:
        return None, "Hiçbir model için yeterli gözlem üretilemedi."

    tablo = pd.DataFrame(tum_satirlar)
    dosya_yolu = _data_path("ai_model_gercek_backtest.csv")
    tablo.to_csv(dosya_yolu, index=False, encoding="utf-8-sig")
    return dosya_yolu, {"satirlar": tum_satirlar}


# =============================================================================
# GÜN İÇİ SÜREKLİ TARAMA TESTİ (ATR Kırılımı + Hacim Z-Skor) — 2026-08-19
# =============================================================================
# GEREKÇE: Kullanıcının fikri - canlı sistem ATR Kırılımı/Hacim Z-Skor'u
# günde BİR KEZ (ABD kapanışı ~23:05) tarıyor, günün ORTASINDA da (15dk'da
# bir - yfinance'in en ince kalıcı granülaritesi, kullanıcının istediği
# "10 dk" değil ama en yakın gerçekçi seçenek) tarasak ne olur? Bu TAMAMEN
# İZOLE bir test - stock_screener_bot.py'deki kanıtlanmış canlı sisteme
# HİÇBİR ŞEKİLDE dokunmuyor, sadece arge_botu.py'de yeni bir deney.
# DÜRÜST TASARIM NOTU: günün ortasında "bugünün hacmi" yarım gündür, tam
# günle KIYASLANAMAZ - bu yüzden hacim z-skoru, o hissenin KENDİ
# GEÇMİŞİNDE, GÜNÜN AYNI SAATİNE KADAR biriken ortalama hacmine göre
# hesaplanıyor (adil kıyas). ATR Kırılımı için ATR14 zaten günlük/çok-
# günlük bir istatistik, gün içi kullanımda sorun yok.
# DÜRÜST SINIR: yfinance 15dk veri sadece SON 60 GÜNLE sınırlı.
# Aynı gün içinde tekrar tekrar tetiklenmeyi önlemek için (ayrışık
# rastgele fazladan sinyal üretmesin diye) her (hisse,gün) için SADECE
# İLK tetiklenme kaydediliyor.

GUNICI_ATR_MULT = 2.0
GUNICI_ZSCORE_ESIK = 2.0
GUNICI_MIN_N = 20


def gun_ici_surekli_tarama_testi_calistir(max_hisse: int = 30) -> tuple:
    """ATR Kırılımı ve Hacim Z-Skor'u GÜN İÇİNDE (15dk barlarda, sadece
    her (hisse,gün) için ilk tetiklenme) test eder, canlı sistemin
    checkpoint çıkış mantığıyla (1g/3g/5g/10g). Kıyas için AYNI evren/
    dönemde SADECE KAPANIŞ bazlı (canlı sistemin gerçek davranışı)
    kontrol grubu da üretiliyor. Döner: (dosya_yolu, özet_dict) ya da
    (None, hata_mesajı)."""
    import yfinance as yf

    hisseler = US_INSIDER_TICKERS[:max_hisse]
    tum_sonuclar = {
        "[GÜN İÇİ] ATR Kırılımı x2.0 (ilk tetiklenme)": [],
        "[GÜN İÇİ] Hacim Z-Skor (ilk tetiklenme)": [],
        "[KAPANIŞ - kontrol] ATR Kırılımı x2.0": [],
        "[KAPANIŞ - kontrol] Hacim Z-Skor": [],
    }

    for n_i, ticker in enumerate(hisseler, 1):
        try:
            print(f"[Gün İçi Tarama {n_i}/{len(hisseler)}] {ticker}...", flush=True)
            gunluk = yf.Ticker(ticker).history(period="2y", interval="1d", timeout=20)
            barlar_15dk = yf.Ticker(ticker).history(period="60d", interval="15m", timeout=20)
            if gunluk is None or gunluk.empty or barlar_15dk is None or barlar_15dk.empty:
                continue
            gunluk.index = pd.to_datetime(gunluk.index).tz_localize(None)
            gunluk = gunluk.rename(columns={"Close": "close", "High": "high", "Low": "low",
                                             "Open": "open", "Volume": "volume"})
            high_low = gunluk["high"] - gunluk["low"]
            high_close = (gunluk["high"] - gunluk["close"].shift()).abs()
            low_close = (gunluk["low"] - gunluk["close"].shift()).abs()
            tr = pd.concat([high_low, high_close, low_close], axis=1).max(axis=1)
            gunluk["atr14"] = tr.rolling(14).mean()
            gunluk["vol_sma20"] = gunluk["volume"].rolling(20).mean()
            gunluk["vol_std20"] = gunluk["volume"].rolling(20).std()
            gunluk["vol_zscore"] = (gunluk["volume"] - gunluk["vol_sma20"]) / gunluk["vol_std20"].replace(0, np.nan)

            barlar_15dk = barlar_15dk.reset_index().rename(columns={
                "Datetime": "ts", "Open": "open", "High": "high", "Low": "low",
                "Close": "close", "Volume": "volume"})
            if "ts" not in barlar_15dk.columns:
                barlar_15dk = barlar_15dk.rename(columns={barlar_15dk.columns[0]: "ts"})
            barlar_15dk["ts"] = pd.to_datetime(barlar_15dk["ts"]).dt.tz_localize(None)
            barlar_15dk["gun"] = barlar_15dk["ts"].dt.date
            barlar_15dk["bar_sirasi"] = barlar_15dk.groupby("gun").cumcount()
            barlar_15dk["kumulatif_hacim"] = barlar_15dk.groupby("gun")["volume"].cumsum()

            # her bar_sirasi icin, o saate kadarki ortalama kumulatif hacim
            # (bu hissenin KENDI GECMISI - adil gun-ici kiyas)
            ortalama_kumulatif = barlar_15dk.groupby("bar_sirasi")["kumulatif_hacim"].transform("mean")
            std_kumulatif = barlar_15dk.groupby("bar_sirasi")["kumulatif_hacim"].transform("std")
            barlar_15dk["gun_ici_zscore"] = (barlar_15dk["kumulatif_hacim"] - ortalama_kumulatif) / \
                                             std_kumulatif.replace(0, np.nan)

            gunler = sorted(barlar_15dk["gun"].unique())
            for gun in gunler:
                gun_barlari = barlar_15dk[barlar_15dk["gun"] == gun].sort_values("ts")
                if len(gun_barlari) < 5:
                    continue
                gunluk_konum_list = gunluk.index[gunluk.index.date == gun]
                if len(gunluk_konum_list) == 0:
                    continue
                gunluk_idx = gunluk.index.get_loc(gunluk_konum_list[0])
                if gunluk_idx == 0 or gunluk_idx >= len(gunluk):
                    continue
                prev_close = gunluk.iloc[gunluk_idx - 1]["close"]
                atr = gunluk.iloc[gunluk_idx]["atr14"]
                if pd.isna(atr) or atr == 0 or pd.isna(prev_close):
                    continue

                atr_tetiklendi = False
                hacim_tetiklendi = False
                for _, bar in gun_barlari.iterrows():
                    move = bar["close"] - prev_close
                    if not atr_tetiklendi and abs(move) >= GUNICI_ATR_MULT * atr:
                        yon = "LONG" if move > 0 else "SHORT"
                        sonuc = _kanit_us_checkpoint_sonuc(gunluk, gunluk_idx, yon)
                        tum_sonuclar["[GÜN İÇİ] ATR Kırılımı x2.0 (ilk tetiklenme)"].append(sonuc)
                        atr_tetiklendi = True
                    if not hacim_tetiklendi and pd.notna(bar["gun_ici_zscore"]) and \
                            bar["gun_ici_zscore"] >= GUNICI_ZSCORE_ESIK:
                        yon = "LONG" if bar["close"] < bar["open"] else ("SHORT" if bar["close"] > bar["open"] else None)
                        if yon:
                            sonuc = _kanit_us_checkpoint_sonuc(gunluk, gunluk_idx, yon)
                            tum_sonuclar["[GÜN İÇİ] Hacim Z-Skor (ilk tetiklenme)"].append(sonuc)
                        hacim_tetiklendi = True
                    if atr_tetiklendi and hacim_tetiklendi:
                        break

            # KONTROL: ayni ticker/donem icin SADECE kapanis bazli (canli sistem) test
            # 2026-08-19 DUZELTME: kullanicinin dogru tespiti - gun-ici veri
            # sadece son 60 gunu kapsiyor (yfinance siniri), ama kontrol tum
            # 2 yili kullaniyordu - ADIL DEGILDI. Simdi kontrol de AYNI 60
            # gunluk pencereyle sinirlandiriliyor (zaman-esleştirilmis kiyas).
            gun_ici_ilk_tarih = min(gunler) if len(gunler) > 0 else None
            gun_ici_son_tarih = max(gunler) if len(gunler) > 0 else None
            for idx in range(20, len(gunluk) - 11):
                row = gunluk.iloc[idx]
                if gun_ici_ilk_tarih is not None:
                    row_tarihi = gunluk.index[idx].date()
                    if row_tarihi < gun_ici_ilk_tarih or row_tarihi > gun_ici_son_tarih:
                        continue  # zaman penceresi disi - adil kiyas icin atla
                prev_close_k = gunluk.iloc[idx - 1]["close"]
                atr_k = row["atr14"]
                if pd.notna(atr_k) and atr_k != 0:
                    move_k = row["close"] - prev_close_k
                    if abs(move_k) >= GUNICI_ATR_MULT * atr_k:
                        yon_k = "LONG" if move_k > 0 else "SHORT"
                        tum_sonuclar["[KAPANIŞ - kontrol] ATR Kırılımı x2.0"].append(
                            _kanit_us_checkpoint_sonuc(gunluk, idx, yon_k))
                if pd.notna(row.get("vol_zscore")) and row["vol_zscore"] >= GUNICI_ZSCORE_ESIK:
                    yon_hk = "LONG" if row["close"] < row["open"] else ("SHORT" if row["close"] > row["open"] else None)
                    if yon_hk:
                        tum_sonuclar["[KAPANIŞ - kontrol] Hacim Z-Skor"].append(
                            _kanit_us_checkpoint_sonuc(gunluk, idx, yon_hk))
        except Exception as e:
            print(f"[Gün İçi Tarama] {ticker} hata: {e}", flush=True)
        time.sleep(0.5)

    satirlar = _kanit_ozet_tablosu(tum_sonuclar)
    if not satirlar:
        return None, "Hiçbir grup için yeterli veri üretilemedi."
    tablo = pd.DataFrame(satirlar)
    dosya_yolu = _data_path("gun_ici_surekli_tarama_testi.csv")
    tablo.to_csv(dosya_yolu, index=False, encoding="utf-8-sig")
    return dosya_yolu, {"satirlar": satirlar}


# =============================================================================
# RSI21 GÜN İÇİ — KÜÇÜK vs BÜYÜK HEDEF KIYASI — 2026-08-19
# =============================================================================
# GEREKÇE: Kullanıcı canlı sistemin RSI21 bildirimlerini "çalışıyor gibi
# ama oranlar çok düşük" diye tarif etti - kod incelemesi doğruladı:
# US_GUNICI_CHECKPOINTS hedefleri %0.15-%0.90 arası, yani neredeyse her
# küçük titreşim bile "isabet" sayılıyor. Bu test AYNI RSI21 giriş
# sinyalini (≤25/≥75) İKİ FARKLI hedef setiyle test ediyor: (1) canlı
# sistemin GERÇEK küçük hedefleri (aynı 5 checkpoint, aynı yüzdeler),
# (2) ATR Kırılımı/Hacim Z-Skor'un kullandığı GERÇEKTEN ANLAMLI büyük
# hedefler (%1/2/3/5, 1/3/5/10 gün) - alttaki sinyal büyük ölçekte de
# tutuyor mu, yoksa sadece küçük eşikte mi "iyi" görünüyor.

RSI21_PERIOD = 21
RSI21_OS, RSI21_OB = 25, 75
RSI21_KUCUK_CHECKPOINTS = [(1, "15dk", 0.15), (2, "30dk", 0.25), (4, "1sa", 0.40),
                           (8, "2sa", 0.60), (16, "4sa", 0.90)]  # 15dk bar sayisi olarak


def _rsi_hesapla(close: pd.Series, n: int) -> pd.Series:
    delta = close.diff()
    gain = delta.clip(lower=0)
    loss = -delta.clip(upper=0)
    avg_gain = gain.ewm(alpha=1 / n, adjust=False).mean()
    avg_loss = loss.ewm(alpha=1 / n, adjust=False).mean()
    rs = avg_gain / avg_loss.replace(0, np.nan)
    return 100 - (100 / (1 + rs))


def _rsi21_kucuk_hedef_sonuc(barlar_15dk: pd.DataFrame, giris_konum: int, yon: str):
    """Canlı sistemin GERÇEK küçük hedefleriyle (15dk-4sa, %0.15-%0.90)
    checkpoint kontrolü - bar-index ofsetleriyle yaklaşık (yfinance'in
    15dk barları düzenli aralıklı olduğu için makul bir yaklaşım)."""
    giris_fiyat = barlar_15dk.iloc[giris_konum]["close"]
    for bar_ofset, etiket, hedef_pct in RSI21_KUCUK_CHECKPOINTS:
        konum = giris_konum + bar_ofset
        if konum >= len(barlar_15dk):
            return "TIMEOUT", None
        bar = barlar_15dk.iloc[konum]
        if yon == "LONG":
            hedef_fiyat = giris_fiyat * (1 + hedef_pct / 100)
            if bar["high"] >= hedef_fiyat:
                return "WIN", hedef_pct
        else:
            hedef_fiyat = giris_fiyat * (1 - hedef_pct / 100)
            if bar["low"] <= hedef_fiyat:
                return "WIN", hedef_pct
    return "LOSS", -1.0


def rsi21_hedef_kiyasi_testi_calistir(max_hisse: int = 30) -> tuple:
    """Aynı RSI21 (≤25/≥75) giriş sinyalini küçük (canlı, %0.15-0.90) ve
    büyük (%1-5, gerçekten anlamlı) hedeflerle karşılaştırmalı test eder.
    Döner: (dosya_yolu, özet_dict) ya da (None, hata_mesajı)."""
    import yfinance as yf

    hisseler = US_INSIDER_TICKERS[:max_hisse]
    tum_sonuclar = {
        "[KÜÇÜK HEDEF - canlı sistem] RSI21": [],
        "[BÜYÜK HEDEF - anlamlı %1-5] RSI21": [],
    }

    for n_i, ticker in enumerate(hisseler, 1):
        try:
            print(f"[RSI21 Hedef Kıyası {n_i}/{len(hisseler)}] {ticker}...", flush=True)
            gunluk = yf.Ticker(ticker).history(period="2y", interval="1d", timeout=20)
            barlar_15dk = yf.Ticker(ticker).history(period="60d", interval="15m", timeout=20)
            if gunluk is None or gunluk.empty or barlar_15dk is None or barlar_15dk.empty:
                continue
            gunluk.index = pd.to_datetime(gunluk.index).tz_localize(None)
            gunluk = gunluk.rename(columns={"Close": "close", "High": "high", "Low": "low",
                                             "Open": "open", "Volume": "volume"})

            barlar_15dk = barlar_15dk.reset_index().rename(columns={
                "Datetime": "ts", "Open": "open", "High": "high", "Low": "low",
                "Close": "close", "Volume": "volume"})
            if "ts" not in barlar_15dk.columns:
                barlar_15dk = barlar_15dk.rename(columns={barlar_15dk.columns[0]: "ts"})
            barlar_15dk["ts"] = pd.to_datetime(barlar_15dk["ts"]).dt.tz_localize(None)
            barlar_15dk["gun"] = barlar_15dk["ts"].dt.date
            barlar_15dk["rsi21"] = _rsi_hesapla(barlar_15dk["close"], RSI21_PERIOD)

            tetiklenen_gunler = set()
            for konum in range(RSI21_PERIOD + 5, len(barlar_15dk)):
                bar = barlar_15dk.iloc[konum]
                gun = bar["gun"]
                if gun in tetiklenen_gunler:
                    continue  # gunde sadece ilk tetiklenme
                rsi = bar["rsi21"]
                if pd.isna(rsi):
                    continue
                yon = "LONG" if rsi <= RSI21_OS else ("SHORT" if rsi >= RSI21_OB else None)
                if yon is None:
                    continue
                tetiklenen_gunler.add(gun)

                # KUCUK hedef (15dk bar ofsetleriyle, ayni gun/sonraki barlar)
                kucuk_sonuc = _rsi21_kucuk_hedef_sonuc(barlar_15dk, konum, yon)
                tum_sonuclar["[KÜÇÜK HEDEF - canlı sistem] RSI21"].append(kucuk_sonuc)

                # BUYUK hedef (gunluk checkpoint sistemi, ayni gunun gunluk index'i)
                # 2026-08-19 DUZELTME: kesin tarih esleşmesi (==) canli veride
                # hic eslesme bulamadi (muhtemelen kucuk bir saat dilimi/format
                # farkı) - "en yakin tarih" yontemine gecirildi, digerlerinde
                # kullandigimiz ayni desen, daha dayanikli.
                gun_ts = pd.Timestamp(gun)
                gunluk_idx = gunluk.index.get_indexer([gun_ts], method="nearest")[0]
                if gunluk_idx < 0:
                    if len(tum_sonuclar["[BÜYÜK HEDEF - anlamlı %1-5] RSI21"]) == 0:
                        print(f"[RSI21 Hedef Kıyası TEŞHİS] {ticker}: gun={gun} "
                              f"(tip={type(gun)}) gunluk.index aralığı="
                              f"[{gunluk.index.min()} - {gunluk.index.max()}]", flush=True)
                    continue
                fark_gun = abs((gunluk.index[gunluk_idx].date() - gun).days)
                if fark_gun > 3:
                    continue  # en yakin tarih bile cok uzaksa (veri araligi disi) atla
                buyuk_sonuc = _kanit_us_checkpoint_sonuc(gunluk, gunluk_idx, yon)
                tum_sonuclar["[BÜYÜK HEDEF - anlamlı %1-5] RSI21"].append(buyuk_sonuc)
        except Exception as e:
            print(f"[RSI21 Hedef Kıyası] {ticker} hata: {e}", flush=True)
        time.sleep(0.5)

    satirlar = _kanit_ozet_tablosu(tum_sonuclar)
    if not satirlar:
        return None, "Hiçbir grup için yeterli veri üretilemedi."
    tablo = pd.DataFrame(satirlar)
    dosya_yolu = _data_path("rsi21_hedef_kiyasi_testi.csv")
    tablo.to_csv(dosya_yolu, index=False, encoding="utf-8-sig")
    return dosya_yolu, {"satirlar": satirlar}


# =============================================================================
# RSI21 GÜN İÇİ — BIST'TE TEST — 2026-08-19
# =============================================================================
# GEREKÇE: ABD'de RSI21 (büyük hedefle) gerçek bir kenar gösterdi
# (%70.04, ort_R=+0.73) - bunu BIST'te hiç denemedik. Aynı giriş
# mantığı (RSI21≤25/≥75, 15dk bar, günde ilk tetiklenme), ama BIST'in
# GERÇEK çıkış mantığıyla (1.5R kısmi TP + breakeven + ATR trailing -
# _kanit_bist_rr_sonuc, kanit_dogrulama'da doğrulanan aynı mekanik).
# DÜRÜST NOT: ABD'de böyle bir canlı sistem zaten vardı (küçük
# hedeflerle) - BIST'te böyle bir canlı sistem hiç yok, o yüzden burada
# "küçük vs büyük" kıyası değil, doğrudan "işe yarıyor mu" testi.

def rsi21_bist_testi_calistir(max_hisse: int = 29) -> tuple:
    """RSI21 gün içi (15dk) sinyalini BIST hisselerinde, BIST'in gerçek
    çıkış mantığıyla test eder. Döner: (dosya_yolu, özet_dict) ya da
    (None, hata_mesajı)."""
    import yfinance as yf

    hisseler = BIST_TICKERS[:max_hisse]
    tum_sonuclar = {"[BIST] RSI21 gün içi (gerçek çıkış)": []}

    for n_i, ticker in enumerate(hisseler, 1):
        try:
            print(f"[RSI21 BIST {n_i}/{len(hisseler)}] {ticker}...", flush=True)
            raw = yf.Ticker(ticker).history(period="2y", interval="1d", timeout=20)
            barlar_15dk = yf.Ticker(ticker).history(period="60d", interval="15m", timeout=20)
            if raw is None or raw.empty or barlar_15dk is None or barlar_15dk.empty:
                continue
            raw = raw.rename(columns={"Open": "open", "High": "high", "Low": "low",
                                       "Close": "close", "Volume": "volume"})
            raw = raw[["open", "high", "low", "close", "volume"]].copy()
            raw.index = pd.to_datetime(raw.index).tz_localize(None)
            tarihler = raw.index
            gunluk = raw.reset_index(drop=True)
            gunluk = _kanit_compute_indicators(gunluk)

            barlar_15dk = barlar_15dk.reset_index().rename(columns={
                "Datetime": "ts", "Open": "open", "High": "high", "Low": "low",
                "Close": "close", "Volume": "volume"})
            if "ts" not in barlar_15dk.columns:
                barlar_15dk = barlar_15dk.rename(columns={barlar_15dk.columns[0]: "ts"})
            barlar_15dk["ts"] = pd.to_datetime(barlar_15dk["ts"]).dt.tz_localize(None)
            barlar_15dk["gun"] = barlar_15dk["ts"].dt.date
            barlar_15dk["rsi21"] = _rsi_hesapla(barlar_15dk["close"], RSI21_PERIOD)

            tetiklenen_gunler = set()
            for konum in range(RSI21_PERIOD + 5, len(barlar_15dk)):
                bar = barlar_15dk.iloc[konum]
                gun = bar["gun"]
                if gun in tetiklenen_gunler:
                    continue
                rsi = bar["rsi21"]
                if pd.isna(rsi):
                    continue
                yon = "LONG" if rsi <= RSI21_OS else ("SHORT" if rsi >= RSI21_OB else None)
                if yon is None:
                    continue
                tetiklenen_gunler.add(gun)

                gun_ts = pd.Timestamp(gun)
                gunluk_idx = tarihler.get_indexer([gun_ts], method="nearest")[0]
                if gunluk_idx < 0:
                    continue
                fark_gun = abs((tarihler[gunluk_idx].date() - gun).days)
                if fark_gun > 3:
                    continue
                sonuc = _kanit_bist_rr_sonuc(gunluk, gunluk_idx, yon)
                if sonuc is not None:
                    tum_sonuclar["[BIST] RSI21 gün içi (gerçek çıkış)"].append(sonuc)
        except Exception as e:
            print(f"[RSI21 BIST] {ticker} hata: {e}", flush=True)
        time.sleep(0.5)

    satirlar = _kanit_ozet_tablosu(tum_sonuclar)
    if not satirlar:
        return None, "Hiçbir grup için yeterli veri üretilemedi."
    tablo = pd.DataFrame(satirlar)
    dosya_yolu = _data_path("rsi21_bist_testi.csv")
    tablo.to_csv(dosya_yolu, index=False, encoding="utf-8-sig")
    return dosya_yolu, {"satirlar": satirlar}


# =============================================================================
# YENİ ABD GÖSTERGE TURNUVASI (Stochastic/CCI/MFI/Bollinger) — 2026-08-19
# =============================================================================
# GEREKÇE: RSI21 + büyük hedef kombinasyonu gerçek bir kenar ortaya
# çıkardı (%70.04, ort_R=+0.73) - aynı yöntemi (gün içi 15dk aşırı uç
# giriş + gerçek checkpoint çıkışı) henüz denenmemiş 4 göstergeye
# uyguluyoruz: Stochastic %K, CCI, MFI, Bollinger Bandı dokunuşu.
# Hepsi AYNI çıkış mantığını (_kanit_us_checkpoint_sonuc, ATR Kırılımı/
# Hacim Z-Skor/RSI21 ile birebir aynı) kullanıyor - tutarlı kıyas.

STOCH_PERIOD, STOCH_OS, STOCH_OB = 14, 20, 80
CCI_PERIOD, CCI_ESIK = 20, 100
MFI_PERIOD, MFI_OS, MFI_OB = 14, 20, 80
BOLL_PERIOD, BOLL_STD = 20, 2.0


def _stochastic_hesapla(high, low, close, n=14):
    en_dusuk = low.rolling(n).min()
    en_yuksek = high.rolling(n).max()
    return 100 * (close - en_dusuk) / (en_yuksek - en_dusuk).replace(0, np.nan)


def _cci_hesapla(high, low, close, n=20):
    tipik_fiyat = (high + low + close) / 3
    sma = tipik_fiyat.rolling(n).mean()
    ort_sapma = tipik_fiyat.rolling(n).apply(lambda x: np.abs(x - x.mean()).mean(), raw=True)
    return (tipik_fiyat - sma) / (0.015 * ort_sapma.replace(0, np.nan))


def _mfi_hesapla(high, low, close, volume, n=14):
    tipik_fiyat = (high + low + close) / 3
    para_akisi = tipik_fiyat * volume
    yon = tipik_fiyat.diff()
    pozitif_akis = para_akisi.where(yon > 0, 0).rolling(n).sum()
    negatif_akis = para_akisi.where(yon < 0, 0).rolling(n).sum()
    oran = pozitif_akis / negatif_akis.replace(0, np.nan)
    return 100 - (100 / (1 + oran))


def _bollinger_hesapla(close, n=20, k=2.0):
    orta = close.rolling(n).mean()
    std = close.rolling(n).std()
    return orta - k * std, orta + k * std  # alt, ust bant


def yeni_gosterge_turnuvasi_us_calistir(max_hisse: int = 30) -> tuple:
    """Stochastic/CCI/MFI/Bollinger aşırı uç sinyallerini (15dk, günde
    ilk tetiklenme) US_SWING_CHECKPOINTS ile (%1-5, 1-10 gün) test eder.
    2026-08-19 EKLENDİ: KÖR TEMEL ÇİZGİ - her gün koşulsuz (hiçbir
    göstergeye bakmadan) LONG ve SHORT açsak bu cömert checkpoint
    sistemi tek başına ne verir - 4 göstergenin "gerçek" kenarını bu
    kör çizgiden ayırt etmek için (çok cömert bir hedef dizisi tek
    başına yüksek isabet verebilir, göstergelerden bağımsız olarak).
    Döner: (dosya_yolu, özet_dict) ya da (None, hata_mesajı)."""
    import yfinance as yf

    hisseler = US_INSIDER_TICKERS[:max_hisse]
    tum_sonuclar = {
        "Stochastic %K (≤20/≥80)": [], "CCI (±100)": [],
        "MFI (≤20/≥80)": [], "Bollinger Bandı dokunuşu": [],
        "[KÖR TEMEL ÇİZGİ] Koşulsuz LONG (her gün)": [],
        "[KÖR TEMEL ÇİZGİ] Koşulsuz SHORT (her gün)": [],
    }

    for n_i, ticker in enumerate(hisseler, 1):
        try:
            print(f"[Yeni Gösterge Turnuvası {n_i}/{len(hisseler)}] {ticker}...", flush=True)
            gunluk = yf.Ticker(ticker).history(period="2y", interval="1d", timeout=20)
            barlar_15dk = yf.Ticker(ticker).history(period="60d", interval="15m", timeout=20)
            if gunluk is None or gunluk.empty or barlar_15dk is None or barlar_15dk.empty:
                continue
            gunluk = gunluk.rename(columns={"Close": "close", "High": "high", "Low": "low",
                                             "Open": "open", "Volume": "volume"})
            gunluk.index = pd.to_datetime(gunluk.index).tz_localize(None)
            tarihler = gunluk.index

            barlar_15dk = barlar_15dk.reset_index().rename(columns={
                "Datetime": "ts", "Open": "open", "High": "high", "Low": "low",
                "Close": "close", "Volume": "volume"})
            if "ts" not in barlar_15dk.columns:
                barlar_15dk = barlar_15dk.rename(columns={barlar_15dk.columns[0]: "ts"})
            barlar_15dk["ts"] = pd.to_datetime(barlar_15dk["ts"]).dt.tz_localize(None)
            barlar_15dk["gun"] = barlar_15dk["ts"].dt.date

            barlar_15dk["stoch"] = _stochastic_hesapla(barlar_15dk["high"], barlar_15dk["low"],
                                                        barlar_15dk["close"], STOCH_PERIOD)
            barlar_15dk["cci"] = _cci_hesapla(barlar_15dk["high"], barlar_15dk["low"],
                                               barlar_15dk["close"], CCI_PERIOD)
            barlar_15dk["mfi"] = _mfi_hesapla(barlar_15dk["high"], barlar_15dk["low"],
                                               barlar_15dk["close"], barlar_15dk["volume"], MFI_PERIOD)
            alt_bant, ust_bant = _bollinger_hesapla(barlar_15dk["close"], BOLL_PERIOD, BOLL_STD)
            barlar_15dk["boll_alt"] = alt_bant
            barlar_15dk["boll_ust"] = ust_bant

            tetiklenen = {isim: set() for isim in tum_sonuclar}
            baslangic_konum = max(STOCH_PERIOD, CCI_PERIOD, MFI_PERIOD, BOLL_PERIOD) + 5
            for konum in range(baslangic_konum, len(barlar_15dk)):
                bar = barlar_15dk.iloc[konum]
                gun = bar["gun"]

                adaylar = []
                if gun not in tetiklenen["Stochastic %K (≤20/≥80)"] and pd.notna(bar["stoch"]):
                    if bar["stoch"] <= STOCH_OS:
                        adaylar.append(("Stochastic %K (≤20/≥80)", "LONG"))
                    elif bar["stoch"] >= STOCH_OB:
                        adaylar.append(("Stochastic %K (≤20/≥80)", "SHORT"))
                if gun not in tetiklenen["CCI (±100)"] and pd.notna(bar["cci"]):
                    if bar["cci"] <= -CCI_ESIK:
                        adaylar.append(("CCI (±100)", "LONG"))
                    elif bar["cci"] >= CCI_ESIK:
                        adaylar.append(("CCI (±100)", "SHORT"))
                if gun not in tetiklenen["MFI (≤20/≥80)"] and pd.notna(bar["mfi"]):
                    if bar["mfi"] <= MFI_OS:
                        adaylar.append(("MFI (≤20/≥80)", "LONG"))
                    elif bar["mfi"] >= MFI_OB:
                        adaylar.append(("MFI (≤20/≥80)", "SHORT"))
                if gun not in tetiklenen["Bollinger Bandı dokunuşu"] and pd.notna(bar["boll_alt"]):
                    if bar["close"] <= bar["boll_alt"]:
                        adaylar.append(("Bollinger Bandı dokunuşu", "LONG"))
                    elif bar["close"] >= bar["boll_ust"]:
                        adaylar.append(("Bollinger Bandı dokunuşu", "SHORT"))

                for isim, yon in adaylar:
                    tetiklenen[isim].add(gun)
                    gun_ts = pd.Timestamp(gun)
                    gunluk_idx = tarihler.get_indexer([gun_ts], method="nearest")[0]
                    if gunluk_idx < 0:
                        continue
                    fark_gun = abs((tarihler[gunluk_idx].date() - gun).days)
                    if fark_gun > 3:
                        continue
                    sonuc = _kanit_us_checkpoint_sonuc(gunluk, gunluk_idx, yon)
                    tum_sonuclar[isim].append(sonuc)

            # KOR TEMEL CIZGI: HER GUNLUK BAR icin kosulsuz LONG ve SHORT -
            # gostergelere hic bakmadan, bu cömert checkpoint sisteminin
            # KENDI BASINA ne verdigini olcuyor.
            for gunluk_idx in range(20, len(gunluk) - 11):
                tum_sonuclar["[KÖR TEMEL ÇİZGİ] Koşulsuz LONG (her gün)"].append(
                    _kanit_us_checkpoint_sonuc(gunluk, gunluk_idx, "LONG"))
                tum_sonuclar["[KÖR TEMEL ÇİZGİ] Koşulsuz SHORT (her gün)"].append(
                    _kanit_us_checkpoint_sonuc(gunluk, gunluk_idx, "SHORT"))
        except Exception as e:
            print(f"[Yeni Gösterge Turnuvası] {ticker} hata: {e}", flush=True)
        time.sleep(0.5)

    satirlar = _kanit_ozet_tablosu(tum_sonuclar)
    if not satirlar:
        return None, "Hiçbir gösterge için yeterli veri üretilemedi."
    tablo = pd.DataFrame(satirlar).sort_values("kazanma_orani_pct", ascending=False)
    dosya_yolu = _data_path("yeni_gosterge_turnuvasi_us.csv")
    tablo.to_csv(dosya_yolu, index=False, encoding="utf-8-sig")
    return dosya_yolu, {"satirlar": satirlar}


# =============================================================================
# TÜM ABD GÖSTERGELERİ — TEK KÖR ÇİZGİYE KARŞI NİHAİ KIYAS — 2026-08-19
# =============================================================================
# GEREKÇE: yeni_gosterge_turnuvasi_us_calistir'de kör temel çizginin
# şaşırtıcı derecede yüksek çıktığı (LONG %64, SHORT %59) görüldü - bu,
# checkpoint sisteminin (4 kademeli, %1-5, 10 güne kadar) TEK BAŞINA
# cömert olduğunu gösterdi. AMA bu kör çizgi bugüne kadar ATR Kırılımı,
# Hacim Z-Skor ve RSI21 için HİÇ kontrol edilmedi - o "kanıtlanmış"
# sonuçlar da aslında bu kör çizgiye göre ne kadar gerçek kenar
# taşıyor, bilmiyoruz. Bu fonksiyon YEDİ stratejiyi + kör çizgiyi TEK
# taramada (hisse başına tek fetch, verimli) hesaplayıp gerçek "kör
# çizginin üstündeki fark"ı (edge_above_kor) net gösteriyor.

def tum_abd_gostergeleri_kor_kiyasi_calistir(max_hisse: int = 30) -> tuple:
    """ATR Kırılımı, Hacim Z-Skor, RSI21, Stochastic, CCI, MFI, Bollinger
    + kör temel çizgiyi (koşulsuz LONG/SHORT) TEK taramada, aynı
    checkpoint sistemine karşı test eder. Döner: (dosya_yolu, özet_dict)
    ya da (None, hata_mesajı)."""
    import yfinance as yf

    hisseler = US_INSIDER_TICKERS[:max_hisse]
    tum_sonuclar = {
        "ATR Kırılımı x2.0": [], "Hacim Z-Skor": [], "RSI21 gün içi": [],
        "Stochastic %K": [], "CCI": [], "MFI": [], "Bollinger dokunuşu": [],
        "[KÖR] Koşulsuz LONG": [], "[KÖR] Koşulsuz SHORT": [],
    }

    for n_i, ticker in enumerate(hisseler, 1):
        try:
            print(f"[Nihai Kıyas {n_i}/{len(hisseler)}] {ticker}...", flush=True)
            gunluk = yf.Ticker(ticker).history(period="2y", interval="1d", timeout=20)
            barlar_15dk = yf.Ticker(ticker).history(period="60d", interval="15m", timeout=20)
            if gunluk is None or gunluk.empty or barlar_15dk is None or barlar_15dk.empty:
                continue
            gunluk = gunluk.rename(columns={"Close": "close", "High": "high", "Low": "low",
                                             "Open": "open", "Volume": "volume"})
            gunluk.index = pd.to_datetime(gunluk.index).tz_localize(None)
            tarihler = gunluk.index
            gunluk_hesaplanmis = gunluk.reset_index(drop=True)
            gunluk_hesaplanmis = _kanit_compute_indicators(gunluk_hesaplanmis)
            # index'i geri tarihe cevir (kolayca eslesmek icin)
            gunluk_hesaplanmis.index = tarihler

            # --- GUNLUK BAZLI: ATR Kırılımı + Hacim Z-Skor + KÖR ÇİZGİ ---
            gunluk_pos = gunluk_hesaplanmis.reset_index(drop=True)
            for idx in range(20, len(gunluk_pos) - 11):
                row = gunluk_pos.iloc[idx]
                prev_close = gunluk_pos.iloc[idx - 1]["close"]
                yon_atr = _kanit_check_us_atr_breakout(row, prev_close)
                if yon_atr:
                    tum_sonuclar["ATR Kırılımı x2.0"].append(
                        _kanit_us_checkpoint_sonuc(gunluk_pos, idx, yon_atr))
                yon_hacim = _kanit_check_us_volume_zscore(row)
                if yon_hacim:
                    tum_sonuclar["Hacim Z-Skor"].append(
                        _kanit_us_checkpoint_sonuc(gunluk_pos, idx, yon_hacim))
                tum_sonuclar["[KÖR] Koşulsuz LONG"].append(
                    _kanit_us_checkpoint_sonuc(gunluk_pos, idx, "LONG"))
                tum_sonuclar["[KÖR] Koşulsuz SHORT"].append(
                    _kanit_us_checkpoint_sonuc(gunluk_pos, idx, "SHORT"))

            # --- GUN ICI (15dk) BAZLI: RSI21, Stochastic, CCI, MFI, Bollinger ---
            barlar_15dk = barlar_15dk.reset_index().rename(columns={
                "Datetime": "ts", "Open": "open", "High": "high", "Low": "low",
                "Close": "close", "Volume": "volume"})
            if "ts" not in barlar_15dk.columns:
                barlar_15dk = barlar_15dk.rename(columns={barlar_15dk.columns[0]: "ts"})
            barlar_15dk["ts"] = pd.to_datetime(barlar_15dk["ts"]).dt.tz_localize(None)
            barlar_15dk["gun"] = barlar_15dk["ts"].dt.date
            barlar_15dk["rsi21"] = _rsi_hesapla(barlar_15dk["close"], RSI21_PERIOD)
            barlar_15dk["stoch"] = _stochastic_hesapla(barlar_15dk["high"], barlar_15dk["low"],
                                                        barlar_15dk["close"], STOCH_PERIOD)
            barlar_15dk["cci"] = _cci_hesapla(barlar_15dk["high"], barlar_15dk["low"],
                                               barlar_15dk["close"], CCI_PERIOD)
            barlar_15dk["mfi"] = _mfi_hesapla(barlar_15dk["high"], barlar_15dk["low"],
                                               barlar_15dk["close"], barlar_15dk["volume"], MFI_PERIOD)
            alt_bant, ust_bant = _bollinger_hesapla(barlar_15dk["close"], BOLL_PERIOD, BOLL_STD)
            barlar_15dk["boll_alt"], barlar_15dk["boll_ust"] = alt_bant, ust_bant

            tetiklenen = {isim: set() for isim in
                          ["RSI21 gün içi", "Stochastic %K", "CCI", "MFI", "Bollinger dokunuşu"]}
            baslangic_konum = max(RSI21_PERIOD, STOCH_PERIOD, CCI_PERIOD, MFI_PERIOD, BOLL_PERIOD) + 5
            for konum in range(baslangic_konum, len(barlar_15dk)):
                bar = barlar_15dk.iloc[konum]
                gun = bar["gun"]
                adaylar = []
                if gun not in tetiklenen["RSI21 gün içi"] and pd.notna(bar["rsi21"]):
                    if bar["rsi21"] <= RSI21_OS:
                        adaylar.append(("RSI21 gün içi", "LONG"))
                    elif bar["rsi21"] >= RSI21_OB:
                        adaylar.append(("RSI21 gün içi", "SHORT"))
                if gun not in tetiklenen["Stochastic %K"] and pd.notna(bar["stoch"]):
                    if bar["stoch"] <= STOCH_OS:
                        adaylar.append(("Stochastic %K", "LONG"))
                    elif bar["stoch"] >= STOCH_OB:
                        adaylar.append(("Stochastic %K", "SHORT"))
                if gun not in tetiklenen["CCI"] and pd.notna(bar["cci"]):
                    if bar["cci"] <= -CCI_ESIK:
                        adaylar.append(("CCI", "LONG"))
                    elif bar["cci"] >= CCI_ESIK:
                        adaylar.append(("CCI", "SHORT"))
                if gun not in tetiklenen["MFI"] and pd.notna(bar["mfi"]):
                    if bar["mfi"] <= MFI_OS:
                        adaylar.append(("MFI", "LONG"))
                    elif bar["mfi"] >= MFI_OB:
                        adaylar.append(("MFI", "SHORT"))
                if gun not in tetiklenen["Bollinger dokunuşu"] and pd.notna(bar["boll_alt"]):
                    if bar["close"] <= bar["boll_alt"]:
                        adaylar.append(("Bollinger dokunuşu", "LONG"))
                    elif bar["close"] >= bar["boll_ust"]:
                        adaylar.append(("Bollinger dokunuşu", "SHORT"))

                for isim, yon in adaylar:
                    tetiklenen[isim].add(gun)
                    gun_ts = pd.Timestamp(gun)
                    gunluk_idx2 = tarihler.get_indexer([gun_ts], method="nearest")[0]
                    if gunluk_idx2 < 0:
                        continue
                    if abs((tarihler[gunluk_idx2].date() - gun).days) > 3:
                        continue
                    tum_sonuclar[isim].append(_kanit_us_checkpoint_sonuc(gunluk_pos, gunluk_idx2, yon))
        except Exception as e:
            print(f"[Nihai Kıyas] {ticker} hata: {e}", flush=True)
        time.sleep(0.5)

    satirlar = _kanit_ozet_tablosu(tum_sonuclar)
    if not satirlar:
        return None, "Hiçbir strateji için yeterli veri üretilemedi."

    # kor cizgiden farki (edge) hesapla, kolon olarak ekle
    kor_long_orani = next((s["kazanma_orani_pct"] for s in satirlar
                            if s["strateji"] == "[KÖR] Koşulsuz LONG"), None)
    for s in satirlar:
        if kor_long_orani is not None and s["kazanma_orani_pct"] is not None:
            s["kor_cizgiden_fark_puan"] = round(s["kazanma_orani_pct"] - kor_long_orani, 2)
        else:
            s["kor_cizgiden_fark_puan"] = None

    tablo = pd.DataFrame(satirlar).sort_values("kor_cizgiden_fark_puan", ascending=False)
    dosya_yolu = _data_path("tum_abd_gostergeleri_kor_kiyasi.csv")
    tablo.to_csv(dosya_yolu, index=False, encoding="utf-8-sig")
    return dosya_yolu, {"satirlar": satirlar}


# =============================================================================
# ÖRTÜŞME TESTİ + GENİŞ TREND/MOMENTUM TURNUVASI — 2026-08-19
# =============================================================================
# GEREKÇE: Kullanıcı ABD için tam bir "cephanelik" istiyor. Elimizdeki 4
# güçlü sinyal (Bollinger, Stochastic, CCI, RSI21) hepsi AYNI AİLEDEN
# (tersine dönüş/mean-reversion - aşırı uçta ters yöne bahis). Bu dosya
# İKİ ŞEYİ BİRDEN yapıyor:
# (1) ÖRTÜŞME: bu 4 güçlü sinyalden 2+'si aynı gün aynı yönde tetiklenirse
#     ekstra avantaj var mı (daha önce ATR+Hacim'de yoktu, ama bunlar
#     daha güçlü sinyaller, tekrar bakmaya değer).
# (2) GENİŞ TREND/MOMENTUM TURNUVASI: eksik olan kategori - "hareket
#     devam eder" mantığıyla çalışan, mean-reversion AİLESİNDEN OLMAYAN
#     onlarca aday: EMA kesişimi, MACD kesişimi, Donchian kırılımı, ADX+DI
#     yön, ROC sıfır kesişimi, RSI orta-hat (50) kesişimi, Parabolic SAR
#     dönüşü, hacim-onaylı kırılım. Hepsi AYNI checkpoint çıkış sistemiyle
#     ve AYNI kör temel çizgiyle (LONG %64.01/SHORT %59.01, önceki
#     turdan) kıyaslanabilir.

EMA_HIZLI, EMA_YAVAS = 9, 21
MACD_HIZLI, MACD_YAVAS, MACD_SINYAL = 12, 26, 9
DONCHIAN_KISA, DONCHIAN_UZUN = 20, 50
ADX_PERIOD = 14
ROC_PERIOD = 10
PSAR_HIZLANMA, PSAR_MAX = 0.02, 0.2


def _ema_hesapla(close, n):
    return close.ewm(span=n, adjust=False).mean()


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


def _psar_hesapla(high, low, close, hizlanma=0.02, maks=0.2):
    """Basitleştirilmiş Parabolic SAR - trend yönü döner (True=yukarı)."""
    n = len(close)
    sar = close.copy() * 0
    trend_yukari = pd.Series([True] * n, index=close.index)
    if n < 2:
        return sar, trend_yukari
    sar.iloc[0] = low.iloc[0]
    ep = high.iloc[0]
    af = hizlanma
    yukari = True
    for i in range(1, n):
        onceki_sar = sar.iloc[i - 1]
        yeni_sar = onceki_sar + af * (ep - onceki_sar)
        if yukari:
            if low.iloc[i] < yeni_sar:
                yukari = False
                yeni_sar = ep
                ep = low.iloc[i]
                af = hizlanma
            else:
                if high.iloc[i] > ep:
                    ep = high.iloc[i]
                    af = min(af + hizlanma, maks)
        else:
            if high.iloc[i] > yeni_sar:
                yukari = True
                yeni_sar = ep
                ep = high.iloc[i]
                af = hizlanma
            else:
                if low.iloc[i] < ep:
                    ep = low.iloc[i]
                    af = min(af + hizlanma, maks)
        sar.iloc[i] = yeni_sar
        trend_yukari.iloc[i] = yukari
    return sar, trend_yukari


KELTNER_EMA_PERIOD, KELTNER_ATR_PERIOD, KELTNER_MULT = 20, 10, 2.0
WILLIAMS_R_PERIOD, WILLIAMS_OS, WILLIAMS_OB = 14, -80, -20
EMA_UCLU_KISA, EMA_UCLU_ORTA, EMA_UCLU_UZUN = 5, 13, 34
AO_KISA, AO_UZUN = 5, 34


def _keltner_hesapla(high, low, close, ema_n=20, atr_n=10, mult=2.0):
    orta = _ema_hesapla(close, ema_n)
    tr = pd.concat([(high - low), (high - close.shift()).abs(), (low - close.shift()).abs()], axis=1).max(axis=1)
    atr = tr.rolling(atr_n).mean()
    return orta - mult * atr, orta + mult * atr  # alt, ust


def _williams_r_hesapla(high, low, close, n=14):
    en_yuksek = high.rolling(n).max()
    en_dusuk = low.rolling(n).min()
    return -100 * (en_yuksek - close) / (en_yuksek - en_dusuk).replace(0, np.nan)


def _vwap_gun_ici_hesapla(high, low, close, volume, gun_serisi):
    tipik = (high + low + close) / 3
    pv = tipik * volume
    gun_df = pd.DataFrame({"pv": pv, "volume": volume, "gun": gun_serisi})
    kumulatif_pv = gun_df.groupby("gun")["pv"].cumsum()
    kumulatif_vol = gun_df.groupby("gun")["volume"].cumsum()
    return kumulatif_pv / kumulatif_vol.replace(0, np.nan)


def _awesome_oscillator_hesapla(high, low, kisa=5, uzun=34):
    medyan = (high + low) / 2
    return medyan.rolling(kisa).mean() - medyan.rolling(uzun).mean()


ICHIMOKU_TENKAN, ICHIMOKU_KIJUN = 9, 26
CHAIKIN_KISA, CHAIKIN_UZUN = 3, 10
STOCH_RSI_PERIOD, STOCH_RSI_OS, STOCH_RSI_OB = 14, 20, 80
VORTEX_PERIOD = 14


def _ichimoku_tenkan_kijun_hesapla(high, low, tenkan_n=9, kijun_n=26):
    tenkan = (high.rolling(tenkan_n).max() + low.rolling(tenkan_n).min()) / 2
    kijun = (high.rolling(kijun_n).max() + low.rolling(kijun_n).min()) / 2
    return tenkan, kijun


def _chaikin_osilator_hesapla(high, low, close, volume, kisa=3, uzun=10):
    rng = (high - low).replace(0, np.nan)
    mfm = ((close - low) - (high - close)) / rng
    mfv = mfm * volume
    adl = mfv.fillna(0).cumsum()
    return _ema_hesapla(adl, kisa) - _ema_hesapla(adl, uzun)


def _stochastic_rsi_hesapla(close, n=14):
    rsi = _rsi_hesapla(close, n)
    en_dusuk = rsi.rolling(n).min()
    en_yuksek = rsi.rolling(n).max()
    return 100 * (rsi - en_dusuk) / (en_yuksek - en_dusuk).replace(0, np.nan)


def _vortex_hesapla(high, low, close, n=14):
    vm_plus = (high - low.shift()).abs()
    vm_minus = (low - high.shift()).abs()
    tr = pd.concat([(high - low), (high - close.shift()).abs(), (low - close.shift()).abs()], axis=1).max(axis=1)
    vi_plus = vm_plus.rolling(n).sum() / tr.rolling(n).sum().replace(0, np.nan)
    vi_minus = vm_minus.rolling(n).sum() / tr.rolling(n).sum().replace(0, np.nan)
    return vi_plus, vi_minus


def gosterge_cephaneligi_calistir(max_hisse: int = 12) -> tuple:
    """(1) 4 güçlü sinyalin (Bollinger/Stochastic/CCI/RSI21) örtüşmesini,
    (2) trend/momentum + yeni adayları TEK taramada test eder, hepsini
    kör temel çizgiyle kıyaslar. 2026-08-19: Williams %R çıkarıldı
    (Stochastic %K ile birebir aynı sonucu veriyordu - gereksiz), MFI
    geri eklendi, 4 yeni aday eklendi (Ichimoku, Chaikin, Stochastic RSI,
    Vortex), varsayılan hisse sayısı büyütüldü (Donchian'ın küçük
    örneklemini daha güvenilir doğrulamak için). Döner: (dosya_yolu,
    özet_dict) ya da (None, hata_mesajı)."""
    import yfinance as yf

    hisseler = US_INSIDER_TICKERS[:max_hisse]
    guclu_isimler = ["Bollinger dokunuşu", "Stochastic %K", "CCI", "RSI21 gün içi", "MFI"]
    yeni_isimler = ["EMA9/21 kesişimi", "MACD kesişimi", "Donchian-20 kırılımı",
                     "Donchian-50 kırılımı", "ADX+DI yön", "ROC-10 sıfır kesişimi",
                     "RSI orta-hat (50) kesişimi", "Parabolic SAR dönüşü",
                     "Hacim-onaylı kırılım (10 bar)", "Keltner Kanalı kırılımı",
                     "VWAP sapması", "Üçlü EMA hizalanması",
                     "Awesome Oscillator sıfır kesişimi", "Ichimoku Tenkan/Kijun kesişimi",
                     "Chaikin Osilatörü sıfır kesişimi", "Stochastic RSI",
                     "Vortex Göstergesi kesişimi"]
    tum_sonuclar = {isim: [] for isim in guclu_isimler + yeni_isimler}
    tum_sonuclar["[ÖRTÜŞME] 2+ güçlü sinyal aynı yön"] = []
    tum_sonuclar["[KÖR] Koşulsuz LONG"] = []
    tum_sonuclar["[KÖR] Koşulsuz SHORT"] = []

    for n_i, ticker in enumerate(hisseler, 1):
        try:
            print(f"[Gösterge Cephaneliği {n_i}/{len(hisseler)}] {ticker}...", flush=True)
            gunluk = _yf_history_sert_zaman_asimli(ticker, "2y", "1d")
            barlar_15dk = _yf_history_sert_zaman_asimli(ticker, "60d", "15m")
            if gunluk is None or gunluk.empty or barlar_15dk is None or barlar_15dk.empty:
                continue
            gunluk = gunluk.rename(columns={"Close": "close", "High": "high", "Low": "low",
                                             "Open": "open", "Volume": "volume"})
            gunluk.index = pd.to_datetime(gunluk.index).tz_localize(None)
            tarihler = gunluk.index
            gunluk_pos = gunluk.reset_index(drop=True)

            # KOR CIZGI (gunluk bazli, ayni checkpoint sistemi)
            for idx in range(20, len(gunluk_pos) - 11):
                tum_sonuclar["[KÖR] Koşulsuz LONG"].append(
                    _kanit_us_checkpoint_sonuc(gunluk_pos, idx, "LONG"))
                tum_sonuclar["[KÖR] Koşulsuz SHORT"].append(
                    _kanit_us_checkpoint_sonuc(gunluk_pos, idx, "SHORT"))

            barlar_15dk = barlar_15dk.reset_index().rename(columns={
                "Datetime": "ts", "Open": "open", "High": "high", "Low": "low",
                "Close": "close", "Volume": "volume"})
            if "ts" not in barlar_15dk.columns:
                barlar_15dk = barlar_15dk.rename(columns={barlar_15dk.columns[0]: "ts"})
            barlar_15dk["ts"] = pd.to_datetime(barlar_15dk["ts"]).dt.tz_localize(None)
            barlar_15dk["gun"] = barlar_15dk["ts"].dt.date

            # --- GUCLU 4'lu (mean-reversion) ---
            barlar_15dk["rsi21"] = _rsi_hesapla(barlar_15dk["close"], RSI21_PERIOD)
            barlar_15dk["stoch"] = _stochastic_hesapla(barlar_15dk["high"], barlar_15dk["low"],
                                                        barlar_15dk["close"], STOCH_PERIOD)
            barlar_15dk["cci"] = _cci_hesapla(barlar_15dk["high"], barlar_15dk["low"],
                                               barlar_15dk["close"], CCI_PERIOD)
            alt_bant, ust_bant = _bollinger_hesapla(barlar_15dk["close"], BOLL_PERIOD, BOLL_STD)
            barlar_15dk["boll_alt"], barlar_15dk["boll_ust"] = alt_bant, ust_bant

            # --- YENI 8'li (trend/momentum) ---
            barlar_15dk["ema_hizli"] = _ema_hesapla(barlar_15dk["close"], EMA_HIZLI)
            barlar_15dk["ema_yavas"] = _ema_hesapla(barlar_15dk["close"], EMA_YAVAS)
            macd_line, macd_sinyal = _macd_hesapla(barlar_15dk["close"], MACD_HIZLI, MACD_YAVAS, MACD_SINYAL)
            barlar_15dk["macd_line"], barlar_15dk["macd_sinyal"] = macd_line, macd_sinyal
            barlar_15dk["donchian_ust20"] = barlar_15dk["high"].rolling(DONCHIAN_KISA).max()
            barlar_15dk["donchian_alt20"] = barlar_15dk["low"].rolling(DONCHIAN_KISA).min()
            barlar_15dk["donchian_ust50"] = barlar_15dk["high"].rolling(DONCHIAN_UZUN).max()
            barlar_15dk["donchian_alt50"] = barlar_15dk["low"].rolling(DONCHIAN_UZUN).min()
            adx, plus_di, minus_di = _adx_di_hesapla(barlar_15dk["high"], barlar_15dk["low"],
                                                      barlar_15dk["close"], ADX_PERIOD)
            barlar_15dk["adx"], barlar_15dk["plus_di"], barlar_15dk["minus_di"] = adx, plus_di, minus_di
            barlar_15dk["roc"] = barlar_15dk["close"].pct_change(ROC_PERIOD) * 100
            barlar_15dk["rsi14_trend"] = _rsi_hesapla(barlar_15dk["close"], 14)
            _, trend_yukari = _psar_hesapla(barlar_15dk["high"], barlar_15dk["low"],
                                             barlar_15dk["close"], PSAR_HIZLANMA, PSAR_MAX)
            barlar_15dk["psar_yukari"] = trend_yukari
            barlar_15dk["hacim_ort10"] = barlar_15dk["volume"].rolling(10).mean()
            barlar_15dk["bar_ust10"] = barlar_15dk["high"].rolling(10).max().shift(1)
            barlar_15dk["bar_alt10"] = barlar_15dk["low"].rolling(10).min().shift(1)

            keltner_alt, keltner_ust = _keltner_hesapla(barlar_15dk["high"], barlar_15dk["low"],
                                                          barlar_15dk["close"], KELTNER_EMA_PERIOD,
                                                          KELTNER_ATR_PERIOD, KELTNER_MULT)
            barlar_15dk["keltner_alt"], barlar_15dk["keltner_ust"] = keltner_alt, keltner_ust
            barlar_15dk["mfi"] = _mfi_hesapla(barlar_15dk["high"], barlar_15dk["low"],
                                               barlar_15dk["close"], barlar_15dk["volume"], MFI_PERIOD)
            barlar_15dk["vwap"] = _vwap_gun_ici_hesapla(barlar_15dk["high"], barlar_15dk["low"],
                                                         barlar_15dk["close"], barlar_15dk["volume"],
                                                         barlar_15dk["gun"])
            barlar_15dk["vwap_sapma_pct"] = (barlar_15dk["close"] - barlar_15dk["vwap"]) / barlar_15dk["vwap"] * 100
            barlar_15dk["ema_kisa3"] = _ema_hesapla(barlar_15dk["close"], EMA_UCLU_KISA)
            barlar_15dk["ema_orta3"] = _ema_hesapla(barlar_15dk["close"], EMA_UCLU_ORTA)
            barlar_15dk["ema_uzun3"] = _ema_hesapla(barlar_15dk["close"], EMA_UCLU_UZUN)
            barlar_15dk["ao"] = _awesome_oscillator_hesapla(barlar_15dk["high"], barlar_15dk["low"],
                                                             AO_KISA, AO_UZUN)
            tenkan, kijun = _ichimoku_tenkan_kijun_hesapla(barlar_15dk["high"], barlar_15dk["low"],
                                                            ICHIMOKU_TENKAN, ICHIMOKU_KIJUN)
            barlar_15dk["tenkan"], barlar_15dk["kijun"] = tenkan, kijun
            barlar_15dk["chaikin_osc"] = _chaikin_osilator_hesapla(
                barlar_15dk["high"], barlar_15dk["low"], barlar_15dk["close"],
                barlar_15dk["volume"], CHAIKIN_KISA, CHAIKIN_UZUN)
            barlar_15dk["stoch_rsi"] = _stochastic_rsi_hesapla(barlar_15dk["close"], STOCH_RSI_PERIOD)
            vi_plus, vi_minus = _vortex_hesapla(barlar_15dk["high"], barlar_15dk["low"],
                                                 barlar_15dk["close"], VORTEX_PERIOD)
            barlar_15dk["vi_plus"], barlar_15dk["vi_minus"] = vi_plus, vi_minus

            tum_isimler = guclu_isimler + yeni_isimler
            tetiklenen = {isim: set() for isim in tum_isimler}
            baslangic_konum = max(RSI21_PERIOD, STOCH_PERIOD, CCI_PERIOD, BOLL_PERIOD,
                                   EMA_YAVAS, MACD_YAVAS, DONCHIAN_UZUN, ADX_PERIOD, ROC_PERIOD,
                                   KELTNER_EMA_PERIOD, MFI_PERIOD, EMA_UCLU_UZUN, AO_UZUN,
                                   ICHIMOKU_KIJUN, CHAIKIN_UZUN, STOCH_RSI_PERIOD * 2, VORTEX_PERIOD) + 5

            for konum in range(baslangic_konum, len(barlar_15dk)):
                bar = barlar_15dk.iloc[konum]
                onceki = barlar_15dk.iloc[konum - 1]
                gun = bar["gun"]
                adaylar = []
                guclu_bugun = []  # (isim, yon) - orusme icin

                # guclu 4'lu
                if gun not in tetiklenen["Bollinger dokunuşu"] and pd.notna(bar["boll_alt"]):
                    if bar["close"] <= bar["boll_alt"]:
                        adaylar.append(("Bollinger dokunuşu", "LONG")); guclu_bugun.append(("Bollinger", "LONG"))
                    elif bar["close"] >= bar["boll_ust"]:
                        adaylar.append(("Bollinger dokunuşu", "SHORT")); guclu_bugun.append(("Bollinger", "SHORT"))
                if gun not in tetiklenen["Stochastic %K"] and pd.notna(bar["stoch"]):
                    if bar["stoch"] <= STOCH_OS:
                        adaylar.append(("Stochastic %K", "LONG")); guclu_bugun.append(("Stochastic", "LONG"))
                    elif bar["stoch"] >= STOCH_OB:
                        adaylar.append(("Stochastic %K", "SHORT")); guclu_bugun.append(("Stochastic", "SHORT"))
                if gun not in tetiklenen["CCI"] and pd.notna(bar["cci"]):
                    if bar["cci"] <= -CCI_ESIK:
                        adaylar.append(("CCI", "LONG")); guclu_bugun.append(("CCI", "LONG"))
                    elif bar["cci"] >= CCI_ESIK:
                        adaylar.append(("CCI", "SHORT")); guclu_bugun.append(("CCI", "SHORT"))
                if gun not in tetiklenen["RSI21 gün içi"] and pd.notna(bar["rsi21"]):
                    if bar["rsi21"] <= RSI21_OS:
                        adaylar.append(("RSI21 gün içi", "LONG")); guclu_bugun.append(("RSI21", "LONG"))
                    elif bar["rsi21"] >= RSI21_OB:
                        adaylar.append(("RSI21 gün içi", "SHORT")); guclu_bugun.append(("RSI21", "SHORT"))

                # yeni 8'li (trend/momentum - kesisim/kirilim TETIKLEME ANINDA)
                if gun not in tetiklenen["EMA9/21 kesişimi"] and pd.notna(bar["ema_hizli"]) and pd.notna(onceki["ema_hizli"]):
                    if onceki["ema_hizli"] <= onceki["ema_yavas"] and bar["ema_hizli"] > bar["ema_yavas"]:
                        adaylar.append(("EMA9/21 kesişimi", "LONG"))
                    elif onceki["ema_hizli"] >= onceki["ema_yavas"] and bar["ema_hizli"] < bar["ema_yavas"]:
                        adaylar.append(("EMA9/21 kesişimi", "SHORT"))
                if gun not in tetiklenen["MACD kesişimi"] and pd.notna(bar["macd_line"]) and pd.notna(onceki["macd_line"]):
                    if onceki["macd_line"] <= onceki["macd_sinyal"] and bar["macd_line"] > bar["macd_sinyal"]:
                        adaylar.append(("MACD kesişimi", "LONG"))
                    elif onceki["macd_line"] >= onceki["macd_sinyal"] and bar["macd_line"] < bar["macd_sinyal"]:
                        adaylar.append(("MACD kesişimi", "SHORT"))
                if gun not in tetiklenen["Donchian-20 kırılımı"] and pd.notna(bar["donchian_ust20"]):
                    if bar["close"] >= bar["donchian_ust20"]:
                        adaylar.append(("Donchian-20 kırılımı", "LONG"))
                    elif bar["close"] <= bar["donchian_alt20"]:
                        adaylar.append(("Donchian-20 kırılımı", "SHORT"))
                if gun not in tetiklenen["Donchian-50 kırılımı"] and pd.notna(bar["donchian_ust50"]):
                    if bar["close"] >= bar["donchian_ust50"]:
                        adaylar.append(("Donchian-50 kırılımı", "LONG"))
                    elif bar["close"] <= bar["donchian_alt50"]:
                        adaylar.append(("Donchian-50 kırılımı", "SHORT"))
                if gun not in tetiklenen["ADX+DI yön"] and pd.notna(bar["adx"]) and bar["adx"] >= 25:
                    if onceki["plus_di"] <= onceki["minus_di"] and bar["plus_di"] > bar["minus_di"]:
                        adaylar.append(("ADX+DI yön", "LONG"))
                    elif onceki["plus_di"] >= onceki["minus_di"] and bar["plus_di"] < bar["minus_di"]:
                        adaylar.append(("ADX+DI yön", "SHORT"))
                if gun not in tetiklenen["ROC-10 sıfır kesişimi"] and pd.notna(bar["roc"]) and pd.notna(onceki["roc"]):
                    if onceki["roc"] <= 0 and bar["roc"] > 0:
                        adaylar.append(("ROC-10 sıfır kesişimi", "LONG"))
                    elif onceki["roc"] >= 0 and bar["roc"] < 0:
                        adaylar.append(("ROC-10 sıfır kesişimi", "SHORT"))
                if gun not in tetiklenen["RSI orta-hat (50) kesişimi"] and pd.notna(bar["rsi14_trend"]) and pd.notna(onceki["rsi14_trend"]):
                    if onceki["rsi14_trend"] <= 50 and bar["rsi14_trend"] > 50:
                        adaylar.append(("RSI orta-hat (50) kesişimi", "LONG"))
                    elif onceki["rsi14_trend"] >= 50 and bar["rsi14_trend"] < 50:
                        adaylar.append(("RSI orta-hat (50) kesişimi", "SHORT"))
                if gun not in tetiklenen["Parabolic SAR dönüşü"] and pd.notna(bar["psar_yukari"]):
                    if bar["psar_yukari"] and not onceki["psar_yukari"]:
                        adaylar.append(("Parabolic SAR dönüşü", "LONG"))
                    elif not bar["psar_yukari"] and onceki["psar_yukari"]:
                        adaylar.append(("Parabolic SAR dönüşü", "SHORT"))
                if gun not in tetiklenen["Hacim-onaylı kırılım (10 bar)"] and pd.notna(bar["bar_ust10"]) and pd.notna(bar["hacim_ort10"]):
                    hacim_yuksek = bar["volume"] >= 1.5 * bar["hacim_ort10"]
                    if hacim_yuksek and bar["close"] > bar["bar_ust10"]:
                        adaylar.append(("Hacim-onaylı kırılım (10 bar)", "LONG"))
                    elif hacim_yuksek and bar["close"] < bar["bar_alt10"]:
                        adaylar.append(("Hacim-onaylı kırılım (10 bar)", "SHORT"))
                if gun not in tetiklenen["Keltner Kanalı kırılımı"] and pd.notna(bar["keltner_alt"]):
                    if bar["close"] >= bar["keltner_ust"]:
                        adaylar.append(("Keltner Kanalı kırılımı", "LONG"))
                    elif bar["close"] <= bar["keltner_alt"]:
                        adaylar.append(("Keltner Kanalı kırılımı", "SHORT"))
                if gun not in tetiklenen["MFI"] and pd.notna(bar["mfi"]):
                    if bar["mfi"] <= MFI_OS:
                        adaylar.append(("MFI", "LONG"))
                    elif bar["mfi"] >= MFI_OB:
                        adaylar.append(("MFI", "SHORT"))
                if gun not in tetiklenen["VWAP sapması"] and pd.notna(bar["vwap_sapma_pct"]):
                    if bar["vwap_sapma_pct"] <= -1.0:
                        adaylar.append(("VWAP sapması", "LONG"))
                    elif bar["vwap_sapma_pct"] >= 1.0:
                        adaylar.append(("VWAP sapması", "SHORT"))
                if gun not in tetiklenen["Üçlü EMA hizalanması"] and pd.notna(bar["ema_kisa3"]) and pd.notna(onceki["ema_kisa3"]):
                    yeni_yukari_hizali = (bar["ema_kisa3"] > bar["ema_orta3"] > bar["ema_uzun3"])
                    onceki_yukari_hizali = (onceki["ema_kisa3"] > onceki["ema_orta3"] > onceki["ema_uzun3"])
                    yeni_asagi_hizali = (bar["ema_kisa3"] < bar["ema_orta3"] < bar["ema_uzun3"])
                    onceki_asagi_hizali = (onceki["ema_kisa3"] < onceki["ema_orta3"] < onceki["ema_uzun3"])
                    if yeni_yukari_hizali and not onceki_yukari_hizali:
                        adaylar.append(("Üçlü EMA hizalanması", "LONG"))
                    elif yeni_asagi_hizali and not onceki_asagi_hizali:
                        adaylar.append(("Üçlü EMA hizalanması", "SHORT"))
                if gun not in tetiklenen["Awesome Oscillator sıfır kesişimi"] and pd.notna(bar["ao"]) and pd.notna(onceki["ao"]):
                    if onceki["ao"] <= 0 and bar["ao"] > 0:
                        adaylar.append(("Awesome Oscillator sıfır kesişimi", "LONG"))
                    elif onceki["ao"] >= 0 and bar["ao"] < 0:
                        adaylar.append(("Awesome Oscillator sıfır kesişimi", "SHORT"))
                if gun not in tetiklenen["Ichimoku Tenkan/Kijun kesişimi"] and pd.notna(bar["tenkan"]) and pd.notna(onceki["tenkan"]):
                    if onceki["tenkan"] <= onceki["kijun"] and bar["tenkan"] > bar["kijun"]:
                        adaylar.append(("Ichimoku Tenkan/Kijun kesişimi", "LONG"))
                    elif onceki["tenkan"] >= onceki["kijun"] and bar["tenkan"] < bar["kijun"]:
                        adaylar.append(("Ichimoku Tenkan/Kijun kesişimi", "SHORT"))
                if gun not in tetiklenen["Chaikin Osilatörü sıfır kesişimi"] and pd.notna(bar["chaikin_osc"]) and pd.notna(onceki["chaikin_osc"]):
                    if onceki["chaikin_osc"] <= 0 and bar["chaikin_osc"] > 0:
                        adaylar.append(("Chaikin Osilatörü sıfır kesişimi", "LONG"))
                    elif onceki["chaikin_osc"] >= 0 and bar["chaikin_osc"] < 0:
                        adaylar.append(("Chaikin Osilatörü sıfır kesişimi", "SHORT"))
                if gun not in tetiklenen["Stochastic RSI"] and pd.notna(bar["stoch_rsi"]):
                    if bar["stoch_rsi"] <= STOCH_RSI_OS:
                        adaylar.append(("Stochastic RSI", "LONG"))
                    elif bar["stoch_rsi"] >= STOCH_RSI_OB:
                        adaylar.append(("Stochastic RSI", "SHORT"))
                if gun not in tetiklenen["Vortex Göstergesi kesişimi"] and pd.notna(bar["vi_plus"]) and pd.notna(onceki["vi_plus"]):
                    if onceki["vi_plus"] <= onceki["vi_minus"] and bar["vi_plus"] > bar["vi_minus"]:
                        adaylar.append(("Vortex Göstergesi kesişimi", "LONG"))
                    elif onceki["vi_plus"] >= onceki["vi_minus"] and bar["vi_plus"] < bar["vi_minus"]:
                        adaylar.append(("Vortex Göstergesi kesişimi", "SHORT"))

                for isim, yon in adaylar:
                    tetiklenen[isim].add(gun)
                    gun_ts = pd.Timestamp(gun)
                    gunluk_idx = tarihler.get_indexer([gun_ts], method="nearest")[0]
                    if gunluk_idx < 0:
                        continue
                    if abs((tarihler[gunluk_idx].date() - gun).days) > 3:
                        continue
                    tum_sonuclar[isim].append(_kanit_us_checkpoint_sonuc(gunluk_pos, gunluk_idx, yon))

                # ORTUSME: bugun ates eden guclu sinyaller arasinda 2+ ayni yonde mi
                if len(guclu_bugun) >= 2:
                    yonler = [y for _, y in guclu_bugun]
                    if yonler.count("LONG") >= 2 or yonler.count("SHORT") >= 2:
                        ortusme_yon = "LONG" if yonler.count("LONG") >= 2 else "SHORT"
                        gun_ts = pd.Timestamp(gun)
                        gunluk_idx = tarihler.get_indexer([gun_ts], method="nearest")[0]
                        if gunluk_idx >= 0 and abs((tarihler[gunluk_idx].date() - gun).days) <= 3:
                            tum_sonuclar["[ÖRTÜŞME] 2+ güçlü sinyal aynı yön"].append(
                                _kanit_us_checkpoint_sonuc(gunluk_pos, gunluk_idx, ortusme_yon))
        except Exception as e:
            print(f"[Gösterge Cephaneliği] {ticker} hata: {e}", flush=True)
        time.sleep(0.5)

    satirlar = _kanit_ozet_tablosu(tum_sonuclar)
    if not satirlar:
        return None, "Hiçbir strateji için yeterli veri üretilemedi."

    kor_long = next((s["kazanma_orani_pct"] for s in satirlar if s["strateji"] == "[KÖR] Koşulsuz LONG"), None)
    kor_long_r = next((s["ort_R"] for s in satirlar if s["strateji"] == "[KÖR] Koşulsuz LONG"), None)
    for s in satirlar:
        s["kor_isabet_farki"] = round(s["kazanma_orani_pct"] - kor_long, 2) if kor_long is not None and s["kazanma_orani_pct"] is not None else None
        s["kor_R_farki"] = round(s["ort_R"] - kor_long_r, 4) if kor_long_r is not None and s["ort_R"] is not None else None

    tablo = pd.DataFrame(satirlar).sort_values("kor_R_farki", ascending=False)
    dosya_yolu = _data_path("gosterge_cephaneligi.csv")
    tablo.to_csv(dosya_yolu, index=False, encoding="utf-8-sig")
    return dosya_yolu, {"satirlar": satirlar}


# =============================================================================
# HAREKET-ÖNCESİ SIKIŞMA TURNUVASI (PRE-BREAKOUT) — 2026-08-19
# =============================================================================
# GEREKÇE: Kullanıcının fikri - bugüne kadarki 8 gösterge hep TEPKİSEL
# (hareket zaten başlamışken tetikleniyor). Kullanıcı, fiyat yatay/
# sıkışıkken, büyük hareket BAŞLAMADAN ÖNCE yakalayan bir sinyal istiyor
# (örnek: SUPX'in %21 sıçramadan önceki yatay bandı). Bu, teknik
# analizde "volatilite sıkışması / pre-breakout" diye bilinen gerçek bir
# kavram. 5 farklı aday TEK dosyada, hepsi AYNI checkpoint çıkışı ve kör
# çizgiyle test ediliyor:
#   1. NR7 - son 7 mumun en dar aralığı (Toby Crabel klasiği)
#   2. İç Mum (Inside Bar) - bugünün aralığı dünkünün içinde
#   3. Bollinger Bant Genişliği Sıkışması - bantlar kendi 60-günlük
#      tarihinde en dar noktasında
#   4. TTM Squeeze - Keltner Kanalı Bollinger'ı içine alıyor
#   5. ATR Persentil Sıkışması - volatilite kendi 100-günlük tarihinde
#      en düşük noktasında
# DÜRÜST NOT: Tüm testler AYNI 106 büyük/sakin ABD hissesi evreninde -
# kullanıcının SUPX örneği gibi küçük/volatil bir hisse değil. Burada
# bulunacak bir kenar, volatil hisselerde muhtemelen DAHA GÜÇLÜ çıkar
# (ama onu ayrıca test etmemiz gerekir, farklı bir hisse evreni ile).

NR7_PENCERE = 7
BOLL_GENISLIK_PENCERE = 60
BOLL_GENISLIK_PERSENTIL = 0.10
KELTNER_SQUEEZE_EMA, KELTNER_SQUEEZE_ATR, KELTNER_SQUEEZE_MULT = 20, 10, 1.5
ATR_PERSENTIL_PENCERE = 100
ATR_PERSENTIL_ESIK = 0.10
YON_BELIRLEME_EMA = 20  # sikisma anindaki fiyat bu EMA'nin uzerinde/altinda mi -> yon tahmini


def _nr7_tespit(high, low, n=7):
    aralik = high - low
    en_dar = aralik.rolling(n).min()
    return aralik <= en_dar * 1.001  # kendisi en dar olan gun


def _ic_mum_tespit(high, low):
    onceki_high = high.shift(1)
    onceki_low = low.shift(1)
    return (high <= onceki_high) & (low >= onceki_low)


def _boll_genislik_sikisma_tespit(close, n=20, k=2.0, pencere=60, persentil=0.10):
    orta = close.rolling(n).mean()
    std = close.rolling(n).std()
    genislik = (2 * k * std) / orta.replace(0, np.nan)
    esik = genislik.rolling(pencere).quantile(persentil)
    return genislik <= esik


def _ttm_squeeze_tespit(high, low, close, ema_n=20, atr_n=10, kc_mult=1.5, bb_n=20, bb_k=2.0):
    # 2026-08-19 DUZELTME: Keltner ve Bollinger AYNI merkezi (SMA) kullanmali -
    # farkli merkez (EMA vs SMA) kullanmak, genislik dar olsa bile bantlarin
    # kaymasina ve "icinde" testinin yanlislikla False donmesine yol aciyordu.
    bb_orta = close.rolling(bb_n).mean()
    tr = pd.concat([(high - low), (high - close.shift()).abs(), (low - close.shift()).abs()], axis=1).max(axis=1)
    atr = tr.rolling(atr_n).mean()
    kc_alt, kc_ust = bb_orta - kc_mult * atr, bb_orta + kc_mult * atr
    bb_std = close.rolling(bb_n).std()
    bb_alt, bb_ust = bb_orta - bb_k * bb_std, bb_orta + bb_k * bb_std
    # squeeze ON: Bollinger bantlari Keltner Kanali'nin ICINDE
    return (bb_alt >= kc_alt) & (bb_ust <= kc_ust)


def _atr_persentil_sikisma_tespit(high, low, close, pencere=100, esik_persentil=0.10):
    tr = pd.concat([(high - low), (high - close.shift()).abs(), (low - close.shift()).abs()], axis=1).max(axis=1)
    atr = tr.rolling(14).mean()
    esik = atr.rolling(pencere).quantile(esik_persentil)
    return atr <= esik


def hareket_oncesi_sikisma_turnuvasi_calistir(max_hisse: int = 12) -> tuple:
    """5 farklı sıkışma/pre-breakout adayını, günde ilk tetiklenme
    kuralıyla, gerçek checkpoint çıkışıyla ve kör temel çizgiyle test
    eder. Yön, sıkışma anındaki fiyatın 20-günlük EMA'ya göre konumuyla
    tahmin ediliyor (üstündeyse LONG, altındaysa SHORT) - NR7/İç Mum
    için ise ertesi barın kırılma yönü kullanılıyor (daha objektif).
    Döner: (dosya_yolu, özet_dict) ya da (None, hata_mesajı)."""
    import yfinance as yf

    hisseler = US_INSIDER_TICKERS[:max_hisse]
    isimler = ["NR7 (dar aralık)", "İç Mum (Inside Bar)", "Bollinger Genişlik Sıkışması",
               "TTM Squeeze", "ATR Persentil Sıkışması"]
    tum_sonuclar = {isim: [] for isim in isimler}
    tum_sonuclar["[KÖR] Koşulsuz LONG"] = []
    tum_sonuclar["[KÖR] Koşulsuz SHORT"] = []

    for n_i, ticker in enumerate(hisseler, 1):
        try:
            print(f"[Sıkışma Turnuvası {n_i}/{len(hisseler)}] {ticker}...", flush=True)
            gunluk = _yf_history_sert_zaman_asimli(ticker, "2y", "1d")
            if gunluk is None or gunluk.empty or len(gunluk) < 150:
                continue
            gunluk = gunluk.rename(columns={"Close": "close", "High": "high", "Low": "low",
                                             "Open": "open", "Volume": "volume"})
            gunluk.index = pd.to_datetime(gunluk.index).tz_localize(None)
            gunluk = gunluk.reset_index(drop=True)

            gunluk["nr7"] = _nr7_tespit(gunluk["high"], gunluk["low"], NR7_PENCERE)
            gunluk["ic_mum"] = _ic_mum_tespit(gunluk["high"], gunluk["low"])
            gunluk["boll_sikisma"] = _boll_genislik_sikisma_tespit(
                gunluk["close"], 20, 2.0, BOLL_GENISLIK_PENCERE, BOLL_GENISLIK_PERSENTIL)
            gunluk["ttm_squeeze"] = _ttm_squeeze_tespit(
                gunluk["high"], gunluk["low"], gunluk["close"],
                KELTNER_SQUEEZE_EMA, KELTNER_SQUEEZE_ATR, KELTNER_SQUEEZE_MULT)
            gunluk["atr_sikisma"] = _atr_persentil_sikisma_tespit(
                gunluk["high"], gunluk["low"], gunluk["close"],
                ATR_PERSENTIL_PENCERE, ATR_PERSENTIL_ESIK)
            gunluk["ema_yon"] = _ema_hesapla(gunluk["close"], YON_BELIRLEME_EMA)

            baslangic = max(NR7_PENCERE, BOLL_GENISLIK_PENCERE, ATR_PERSENTIL_PENCERE, YON_BELIRLEME_EMA) + 5
            for idx in range(baslangic, len(gunluk) - 11):
                row = gunluk.iloc[idx]

                # KOR CIZGI - her gun kosulsuz
                tum_sonuclar["[KÖR] Koşulsuz LONG"].append(_kanit_us_checkpoint_sonuc(gunluk, idx, "LONG"))
                tum_sonuclar["[KÖR] Koşulsuz SHORT"].append(_kanit_us_checkpoint_sonuc(gunluk, idx, "SHORT"))

                # yon tahmini: EMA'ya gore
                yon_tahmin = "LONG" if row["close"] >= row["ema_yon"] else "SHORT"

                if row["nr7"]:
                    # NR7 icin: ERTESI barin kirilma yonu (daha objektif)
                    sonraki = gunluk.iloc[idx + 1]
                    if sonraki["close"] > row["high"]:
                        tum_sonuclar["NR7 (dar aralık)"].append(_kanit_us_checkpoint_sonuc(gunluk, idx + 1, "LONG"))
                    elif sonraki["close"] < row["low"]:
                        tum_sonuclar["NR7 (dar aralık)"].append(_kanit_us_checkpoint_sonuc(gunluk, idx + 1, "SHORT"))
                if row["ic_mum"]:
                    sonraki = gunluk.iloc[idx + 1]
                    if sonraki["close"] > row["high"]:
                        tum_sonuclar["İç Mum (Inside Bar)"].append(_kanit_us_checkpoint_sonuc(gunluk, idx + 1, "LONG"))
                    elif sonraki["close"] < row["low"]:
                        tum_sonuclar["İç Mum (Inside Bar)"].append(_kanit_us_checkpoint_sonuc(gunluk, idx + 1, "SHORT"))
                if row["boll_sikisma"]:
                    tum_sonuclar["Bollinger Genişlik Sıkışması"].append(
                        _kanit_us_checkpoint_sonuc(gunluk, idx, yon_tahmin))
                if row["ttm_squeeze"]:
                    tum_sonuclar["TTM Squeeze"].append(_kanit_us_checkpoint_sonuc(gunluk, idx, yon_tahmin))
                if row["atr_sikisma"]:
                    tum_sonuclar["ATR Persentil Sıkışması"].append(
                        _kanit_us_checkpoint_sonuc(gunluk, idx, yon_tahmin))
        except Exception as e:
            print(f"[Sıkışma Turnuvası] {ticker} hata: {e}", flush=True)
        time.sleep(0.3)

    satirlar = _kanit_ozet_tablosu(tum_sonuclar)
    if not satirlar:
        return None, "Hiçbir strateji için yeterli veri üretilemedi."

    kor_long = next((s["kazanma_orani_pct"] for s in satirlar if s["strateji"] == "[KÖR] Koşulsuz LONG"), None)
    kor_long_r = next((s["ort_R"] for s in satirlar if s["strateji"] == "[KÖR] Koşulsuz LONG"), None)
    for s in satirlar:
        s["kor_isabet_farki"] = round(s["kazanma_orani_pct"] - kor_long, 2) if kor_long is not None and s["kazanma_orani_pct"] is not None else None
        s["kor_R_farki"] = round(s["ort_R"] - kor_long_r, 4) if kor_long_r is not None and s["ort_R"] is not None else None

    tablo = pd.DataFrame(satirlar).sort_values("kor_R_farki", ascending=False)
    dosya_yolu = _data_path("hareket_oncesi_sikisma_turnuvasi.csv")
    tablo.to_csv(dosya_yolu, index=False, encoding="utf-8-sig")
    return dosya_yolu, {"satirlar": satirlar}


# =============================================================================
# SIKIŞMA TURNUVASI v2 — VOLATİL HİSSELER + DÜZELTİLMİŞ YÖN + YENİ ADAYLAR
# =============================================================================
# 2026-08-19 - GEREKÇE: v1 (hareket_oncesi_sikisma_turnuvasi_calistir),
# 106 büyük/sakin ABD hissesinde hiçbir sıkışma adayının kör çizgiyi
# geçemediğini gösterdi. Kullanıcının isteğiyle ÜÇ şey birden düzeltiliyor:
# (1) YENİ, VOLATİL/KÜÇÜK HİSSE EVRENİ - kullanıcının SUPX örneği gibi
#     büyük günlük hareketler yapabilen hisseler (mevcut 106'nın aksine).
#     DÜRÜST NOT: bu liste elimdeki genel bilgiye dayanıyor, canlı bir
#     tarama/filtreleme değil - bazı tickerlar artık borsada olmayabilir
#     ya da eskisi kadar volatil olmayabilir, kod bunları otomatik atlar.
# (2) DÜZELTİLMİŞ YÖN MANTIĞI - v1'de sıkışma SÜRERKEN fiyat/EMA20
#     konumuna bakıyorduk (zayıf). Şimdi sıkışma BİTTİĞİ ANDA (release)
#     Awesome Oscillator'ın yönüne bakıyoruz - gerçek TTM Squeeze
#     sistemlerinin kullandığı yöntem.
# (3) 3 YENİ ADAY: 52-Hafta Zirve/Dip Kırılımı, Bollinger Bandı Yürüyüşü
#     (2+ gün üst/alt bandın dışında kapanış - trend devamı), Gap Kırılımı.

US_VOLATIL_TICKERS = [
    "GME", "AMC", "MARA", "RIOT", "MSTR", "PLTR", "SOFI", "LCID", "RIVN",
    "NIO", "XPEV", "LI", "SAVA", "OCGN", "INO", "VXRT", "BNGO", "SPCE",
    "NKLA", "GOEV", "FSR", "RIDE", "CLOV", "WISH", "BB", "IONQ", "RGTI",
    "SMCI", "UPST", "AFRM", "CVNA", "DKNG", "HOOD", "COIN", "ROKU", "SNAP",
    "PLUG", "FCEL", "CHPT", "QS",
    # 2026-08-19 EKLENDİ - kullanıcının isteğiyle daha da genişletildi,
    # küçük/volatil hisselerde "patlama günü" yakalama testi için.
    # DÜRÜST NOT: bu ek liste de canlı bir tarama değil, genel bilgime
    # dayanıyor - bazıları artık farklı davranıyor olabilir, kod
    # bunları otomatik atlar (veri gelmezse).
    "BBAI", "SOUN", "IONS", "CRSP", "NTLA", "BEAM", "EDIT", "RXRX",
    "ACHR", "JOBY", "LILM", "EVTL", "BLDE", "DNA", "GEVO", "AMTX",
    "MULN", "WKHS", "HYLN", "CENN", "GOEV", "PHUN", "ATER", "BBIG",
    "PROG", "SPRT", "ANY", "SDC", "TLRY", "CGC", "ACB", "HEXO",
    "APRN", "CCIV", "SKLZ", "OPEN", "RUN", "BLNK", "EVGO", "LAZR",
    "VLDR", "OUST", "MVIS", "CIDM", "GSAT", "SIRI", "NKE", "MRIN",
]
US_VOLATIL_TICKERS = list(dict.fromkeys(US_VOLATIL_TICKERS))  # tekrarları at, sirayi koru

_52HAFTA_PENCERE = 252


def _bollinger_bandi_yurumesi_tespit(close, boll_alt, boll_ust):
    """2+ gun ust/alt bandin DISINDA kapanis = trend devami (yuruyus)."""
    ust_disinda = close >= boll_ust
    alt_disinda = close <= boll_alt
    ust_yuruyus = ust_disinda & ust_disinda.shift(1).fillna(False)
    alt_yuruyus = alt_disinda & alt_disinda.shift(1).fillna(False)
    return ust_yuruyus, alt_yuruyus


def _52_hafta_kirilim_tespit(high, low, close, pencere=252):
    zirve = high.rolling(pencere).max().shift(1)  # bugunu DAHIL ETMEDEN onceki zirve
    dip = low.rolling(pencere).min().shift(1)
    yeni_zirve = close >= zirve
    yeni_dip = close <= dip
    return yeni_zirve, yeni_dip


def _gap_kirilim_tespit(open_, close, prev_close, esik_pct=3.0):
    gap_pct = (open_ - prev_close) / prev_close.replace(0, np.nan) * 100
    gap_yukari_devam = (gap_pct >= esik_pct) & (close > open_)
    gap_asagi_devam = (gap_pct <= -esik_pct) & (close < open_)
    return gap_yukari_devam, gap_asagi_devam


def sikisma_turnuvasi_v2_calistir(hisse_listesi: str = "volatil", max_hisse: int = 12) -> tuple:
    """v1'in düzeltilmiş hali: release-bazlı yön (AO ile), yeni volatil
    hisse evreni seçeneği, +3 yeni aday. hisse_listesi='volatil' ya da
    'buyuk' (mevcut 106'lık liste) olabilir. Döner: (dosya_yolu,
    özet_dict) ya da (None, hata_mesajı)."""
    import yfinance as yf

    hisseler = (US_VOLATIL_TICKERS if hisse_listesi == "volatil" else US_INSIDER_TICKERS)[:max_hisse]
    isimler = ["Bollinger Genişlik Sıkışması (release+AO)", "TTM Squeeze (release+AO)",
               "ATR Persentil Sıkışması (release+AO)", "NR7 (dar aralık)", "İç Mum (Inside Bar)",
               "52-Hafta Zirve/Dip Kırılımı", "Bollinger Bandı Yürüyüşü", "Gap Kırılımı"]
    tum_sonuclar = {isim: [] for isim in isimler}
    tum_sonuclar["[KÖR] Koşulsuz LONG"] = []
    tum_sonuclar["[KÖR] Koşulsuz SHORT"] = []

    for n_i, ticker in enumerate(hisseler, 1):
        try:
            print(f"[Sıkışma v2 {n_i}/{len(hisseler)}] {ticker}...", flush=True)
            gunluk = _yf_history_sert_zaman_asimli(ticker, "2y", "1d")
            if gunluk is None or gunluk.empty or len(gunluk) < 280:
                print(f"[Sıkışma v2] {ticker}: yetersiz veri (2 yıl gerekli, 52-hafta "
                      f"kırılımı için), atlanıyor.", flush=True)
                continue
            gunluk = gunluk.rename(columns={"Close": "close", "High": "high", "Low": "low",
                                             "Open": "open", "Volume": "volume"})
            gunluk.index = pd.to_datetime(gunluk.index).tz_localize(None)
            gunluk = gunluk.reset_index(drop=True)

            gunluk["nr7"] = _nr7_tespit(gunluk["high"], gunluk["low"], NR7_PENCERE)
            gunluk["ic_mum"] = _ic_mum_tespit(gunluk["high"], gunluk["low"])
            gunluk["boll_sikisma"] = _boll_genislik_sikisma_tespit(
                gunluk["close"], 20, 2.0, BOLL_GENISLIK_PENCERE, BOLL_GENISLIK_PERSENTIL)
            gunluk["ttm_squeeze"] = _ttm_squeeze_tespit(
                gunluk["high"], gunluk["low"], gunluk["close"],
                KELTNER_SQUEEZE_EMA, KELTNER_SQUEEZE_ATR, KELTNER_SQUEEZE_MULT)
            gunluk["atr_sikisma"] = _atr_persentil_sikisma_tespit(
                gunluk["high"], gunluk["low"], gunluk["close"],
                ATR_PERSENTIL_PENCERE, ATR_PERSENTIL_ESIK)
            gunluk["ao"] = _awesome_oscillator_hesapla(gunluk["high"], gunluk["low"])

            boll_orta = gunluk["close"].rolling(20).mean()
            boll_std = gunluk["close"].rolling(20).std()
            alt_bant, ust_bant = boll_orta - 2.0 * boll_std, boll_orta + 2.0 * boll_std
            ust_yuruyus, alt_yuruyus = _bollinger_bandi_yurumesi_tespit(gunluk["close"], alt_bant, ust_bant)
            yeni_zirve, yeni_dip = _52_hafta_kirilim_tespit(gunluk["high"], gunluk["low"], gunluk["close"], _52HAFTA_PENCERE)
            gap_yukari, gap_asagi = _gap_kirilim_tespit(gunluk["open"], gunluk["close"], gunluk["close"].shift(1))

            baslangic = max(NR7_PENCERE, BOLL_GENISLIK_PENCERE, ATR_PERSENTIL_PENCERE, _52HAFTA_PENCERE) + 5
            for idx in range(baslangic, len(gunluk) - 11):
                row = gunluk.iloc[idx]
                onceki = gunluk.iloc[idx - 1]

                tum_sonuclar["[KÖR] Koşulsuz LONG"].append(_kanit_us_checkpoint_sonuc(gunluk, idx, "LONG"))
                tum_sonuclar["[KÖR] Koşulsuz SHORT"].append(_kanit_us_checkpoint_sonuc(gunluk, idx, "SHORT"))

                yon_ao = "LONG" if row["ao"] > 0 else ("SHORT" if row["ao"] < 0 else None)

                # RELEASE tespiti: dun sikisma vardi, bugun yok -> tam bu an
                if yon_ao and bool(onceki["boll_sikisma"]) and not bool(row["boll_sikisma"]):
                    tum_sonuclar["Bollinger Genişlik Sıkışması (release+AO)"].append(
                        _kanit_us_checkpoint_sonuc(gunluk, idx, yon_ao))
                if yon_ao and bool(onceki["ttm_squeeze"]) and not bool(row["ttm_squeeze"]):
                    tum_sonuclar["TTM Squeeze (release+AO)"].append(
                        _kanit_us_checkpoint_sonuc(gunluk, idx, yon_ao))
                if yon_ao and bool(onceki["atr_sikisma"]) and not bool(row["atr_sikisma"]):
                    tum_sonuclar["ATR Persentil Sıkışması (release+AO)"].append(
                        _kanit_us_checkpoint_sonuc(gunluk, idx, yon_ao))

                if row["nr7"]:
                    sonraki = gunluk.iloc[idx + 1]
                    if sonraki["close"] > row["high"]:
                        tum_sonuclar["NR7 (dar aralık)"].append(_kanit_us_checkpoint_sonuc(gunluk, idx + 1, "LONG"))
                    elif sonraki["close"] < row["low"]:
                        tum_sonuclar["NR7 (dar aralık)"].append(_kanit_us_checkpoint_sonuc(gunluk, idx + 1, "SHORT"))
                if row["ic_mum"]:
                    sonraki = gunluk.iloc[idx + 1]
                    if sonraki["close"] > row["high"]:
                        tum_sonuclar["İç Mum (Inside Bar)"].append(_kanit_us_checkpoint_sonuc(gunluk, idx + 1, "LONG"))
                    elif sonraki["close"] < row["low"]:
                        tum_sonuclar["İç Mum (Inside Bar)"].append(_kanit_us_checkpoint_sonuc(gunluk, idx + 1, "SHORT"))

                if bool(yeni_zirve.iloc[idx]):
                    tum_sonuclar["52-Hafta Zirve/Dip Kırılımı"].append(_kanit_us_checkpoint_sonuc(gunluk, idx, "LONG"))
                elif bool(yeni_dip.iloc[idx]):
                    tum_sonuclar["52-Hafta Zirve/Dip Kırılımı"].append(_kanit_us_checkpoint_sonuc(gunluk, idx, "SHORT"))

                if bool(ust_yuruyus.iloc[idx]):
                    tum_sonuclar["Bollinger Bandı Yürüyüşü"].append(_kanit_us_checkpoint_sonuc(gunluk, idx, "LONG"))
                elif bool(alt_yuruyus.iloc[idx]):
                    tum_sonuclar["Bollinger Bandı Yürüyüşü"].append(_kanit_us_checkpoint_sonuc(gunluk, idx, "SHORT"))

                if bool(gap_yukari.iloc[idx]):
                    tum_sonuclar["Gap Kırılımı"].append(_kanit_us_checkpoint_sonuc(gunluk, idx, "LONG"))
                elif bool(gap_asagi.iloc[idx]):
                    tum_sonuclar["Gap Kırılımı"].append(_kanit_us_checkpoint_sonuc(gunluk, idx, "SHORT"))
        except Exception as e:
            print(f"[Sıkışma v2] {ticker} hata: {e}", flush=True)
        time.sleep(0.3)

    satirlar = _kanit_ozet_tablosu(tum_sonuclar)
    if not satirlar:
        return None, "Hiçbir strateji için yeterli veri üretilemedi."

    kor_long = next((s["kazanma_orani_pct"] for s in satirlar if s["strateji"] == "[KÖR] Koşulsuz LONG"), None)
    kor_long_r = next((s["ort_R"] for s in satirlar if s["strateji"] == "[KÖR] Koşulsuz LONG"), None)
    for s in satirlar:
        s["kor_isabet_farki"] = round(s["kazanma_orani_pct"] - kor_long, 2) if kor_long is not None and s["kazanma_orani_pct"] is not None else None
        s["kor_R_farki"] = round(s["ort_R"] - kor_long_r, 4) if kor_long_r is not None and s["ort_R"] is not None else None

    tablo = pd.DataFrame(satirlar).sort_values("kor_R_farki", ascending=False)
    dosya_yolu = _data_path("sikisma_turnuvasi_v2.csv")
    tablo.to_csv(dosya_yolu, index=False, encoding="utf-8-sig")
    return dosya_yolu, {"satirlar": satirlar, "hisse_listesi": hisse_listesi,
                         "denenen_hisse_sayisi": len(hisseler)}


# =============================================================================
# SIKIŞMA TURNUVASI v3 — ATR-ÖLÇEKLİ HEDEFLER (VOLATİL HİSSELER İÇİN)
# =============================================================================
# 2026-08-19 - GEREKÇE: v2, volatil hisse evreninde kör LONG/SHORT'un
# ikisinin de %83-85 çıktığını gösterdi - sabit %1-5 hedefler bu kadar
# oynak hisseler için ANLAMSIZLAŞIYOR (neredeyse her pozisyon "kazanıyor"
# göründüğü için ayırt edici değil). Çözüm: hedefleri sabit yüzde yerine
# HER HİSSENİN KENDİ ATR'ına göre ölçeklendirmek - volatil bir hissede
# hedef de büyük olacak, sakin bir hissede küçük - adil bir kıyas.

ATR_OLCEKLI_CHECKPOINTS = [(1, "1g", 1.0), (3, "3g", 2.0), (5, "5g", 3.0), (10, "10g", 5.0)]  # ATR KATI, yuzde DEGIL


def _kanit_us_checkpoint_sonuc_atr_olcekli(df: pd.DataFrame, idx: int, direction: str, atr_col: str = "atr14"):
    """_kanit_us_checkpoint_sonuc ile AYNI mantık, ama hedef sabit yüzde
    yerine o ANDAKİ ATR'ın katı - volatilitesi yüksek hissede otomatik
    olarak daha büyük, düşük hissede daha küçük hedef anlamına gelir."""
    entry = df.iloc[idx]["close"]
    atr = df.iloc[idx][atr_col]
    if pd.isna(atr) or atr == 0:
        return None
    for gun_sayisi, etiket, atr_kat in ATR_OLCEKLI_CHECKPOINTS:
        i = idx + gun_sayisi
        if i >= len(df):
            return "TIMEOUT", None
        gun = df.iloc[i]
        if direction == "LONG":
            hedef_fiyat = entry + atr_kat * atr
            if gun["high"] >= hedef_fiyat:
                return "WIN", atr_kat
        else:
            hedef_fiyat = entry - atr_kat * atr
            if gun["low"] <= hedef_fiyat:
                return "WIN", atr_kat
    return "LOSS", -1.0


def sikisma_turnuvasi_v3_calistir(hisse_listesi: str = "volatil", max_hisse: int = 12) -> tuple:
    """v2 ile AYNI 8 strateji + kör çizgi, ama ATR-ölçekli checkpoint
    çıkışıyla - volatil hisseler için sabit yüzde hedeflerin anlamsız
    hale gelme sorununu çözmek için. Döner: (dosya_yolu, özet_dict) ya da
    (None, hata_mesajı)."""
    import yfinance as yf

    hisseler = (US_VOLATIL_TICKERS if hisse_listesi == "volatil" else US_INSIDER_TICKERS)[:max_hisse]
    isimler = ["Bollinger Genişlik Sıkışması (release+AO)", "TTM Squeeze (release+AO)",
               "ATR Persentil Sıkışması (release+AO)", "NR7 (dar aralık)", "İç Mum (Inside Bar)",
               "52-Hafta Zirve/Dip Kırılımı", "Bollinger Bandı Yürüyüşü", "Gap Kırılımı"]
    tum_sonuclar = {isim: [] for isim in isimler}
    tum_sonuclar["[KÖR] Koşulsuz LONG"] = []
    tum_sonuclar["[KÖR] Koşulsuz SHORT"] = []

    for n_i, ticker in enumerate(hisseler, 1):
        try:
            print(f"[Sıkışma v3 {n_i}/{len(hisseler)}] {ticker}...", flush=True)
            gunluk = _yf_history_sert_zaman_asimli(ticker, "2y", "1d")
            if gunluk is None or gunluk.empty or len(gunluk) < 280:
                print(f"[Sıkışma v3] {ticker}: yetersiz veri, atlanıyor.", flush=True)
                continue
            gunluk = gunluk.rename(columns={"Close": "close", "High": "high", "Low": "low",
                                             "Open": "open", "Volume": "volume"})
            gunluk.index = pd.to_datetime(gunluk.index).tz_localize(None)
            gunluk = gunluk.reset_index(drop=True)

            # ATR14 - checkpoint hedeflerini olceklemek icin
            tr = pd.concat([(gunluk["high"] - gunluk["low"]),
                             (gunluk["high"] - gunluk["close"].shift()).abs(),
                             (gunluk["low"] - gunluk["close"].shift()).abs()], axis=1).max(axis=1)
            gunluk["atr14"] = tr.rolling(14).mean()

            gunluk["nr7"] = _nr7_tespit(gunluk["high"], gunluk["low"], NR7_PENCERE)
            gunluk["ic_mum"] = _ic_mum_tespit(gunluk["high"], gunluk["low"])
            gunluk["boll_sikisma"] = _boll_genislik_sikisma_tespit(
                gunluk["close"], 20, 2.0, BOLL_GENISLIK_PENCERE, BOLL_GENISLIK_PERSENTIL)
            gunluk["ttm_squeeze"] = _ttm_squeeze_tespit(
                gunluk["high"], gunluk["low"], gunluk["close"],
                KELTNER_SQUEEZE_EMA, KELTNER_SQUEEZE_ATR, KELTNER_SQUEEZE_MULT)
            gunluk["atr_sikisma"] = _atr_persentil_sikisma_tespit(
                gunluk["high"], gunluk["low"], gunluk["close"],
                ATR_PERSENTIL_PENCERE, ATR_PERSENTIL_ESIK)
            gunluk["ao"] = _awesome_oscillator_hesapla(gunluk["high"], gunluk["low"])

            boll_orta = gunluk["close"].rolling(20).mean()
            boll_std = gunluk["close"].rolling(20).std()
            alt_bant, ust_bant = boll_orta - 2.0 * boll_std, boll_orta + 2.0 * boll_std
            ust_yuruyus, alt_yuruyus = _bollinger_bandi_yurumesi_tespit(gunluk["close"], alt_bant, ust_bant)
            yeni_zirve, yeni_dip = _52_hafta_kirilim_tespit(gunluk["high"], gunluk["low"], gunluk["close"], _52HAFTA_PENCERE)
            gap_yukari, gap_asagi = _gap_kirilim_tespit(gunluk["open"], gunluk["close"], gunluk["close"].shift(1))

            baslangic = max(NR7_PENCERE, BOLL_GENISLIK_PENCERE, ATR_PERSENTIL_PENCERE, _52HAFTA_PENCERE) + 5
            for idx in range(baslangic, len(gunluk) - 11):
                row = gunluk.iloc[idx]
                onceki = gunluk.iloc[idx - 1]

                kor_long = _kanit_us_checkpoint_sonuc_atr_olcekli(gunluk, idx, "LONG")
                if kor_long is not None:
                    tum_sonuclar["[KÖR] Koşulsuz LONG"].append(kor_long)
                kor_short = _kanit_us_checkpoint_sonuc_atr_olcekli(gunluk, idx, "SHORT")
                if kor_short is not None:
                    tum_sonuclar["[KÖR] Koşulsuz SHORT"].append(kor_short)

                yon_ao = "LONG" if row["ao"] > 0 else ("SHORT" if row["ao"] < 0 else None)

                if yon_ao and bool(onceki["boll_sikisma"]) and not bool(row["boll_sikisma"]):
                    sonuc = _kanit_us_checkpoint_sonuc_atr_olcekli(gunluk, idx, yon_ao)
                    if sonuc is not None:
                        tum_sonuclar["Bollinger Genişlik Sıkışması (release+AO)"].append(sonuc)
                if yon_ao and bool(onceki["ttm_squeeze"]) and not bool(row["ttm_squeeze"]):
                    sonuc = _kanit_us_checkpoint_sonuc_atr_olcekli(gunluk, idx, yon_ao)
                    if sonuc is not None:
                        tum_sonuclar["TTM Squeeze (release+AO)"].append(sonuc)
                if yon_ao and bool(onceki["atr_sikisma"]) and not bool(row["atr_sikisma"]):
                    sonuc = _kanit_us_checkpoint_sonuc_atr_olcekli(gunluk, idx, yon_ao)
                    if sonuc is not None:
                        tum_sonuclar["ATR Persentil Sıkışması (release+AO)"].append(sonuc)

                if row["nr7"]:
                    sonraki = gunluk.iloc[idx + 1]
                    if sonraki["close"] > row["high"]:
                        sonuc = _kanit_us_checkpoint_sonuc_atr_olcekli(gunluk, idx + 1, "LONG")
                        if sonuc is not None:
                            tum_sonuclar["NR7 (dar aralık)"].append(sonuc)
                    elif sonraki["close"] < row["low"]:
                        sonuc = _kanit_us_checkpoint_sonuc_atr_olcekli(gunluk, idx + 1, "SHORT")
                        if sonuc is not None:
                            tum_sonuclar["NR7 (dar aralık)"].append(sonuc)
                if row["ic_mum"]:
                    sonraki = gunluk.iloc[idx + 1]
                    if sonraki["close"] > row["high"]:
                        sonuc = _kanit_us_checkpoint_sonuc_atr_olcekli(gunluk, idx + 1, "LONG")
                        if sonuc is not None:
                            tum_sonuclar["İç Mum (Inside Bar)"].append(sonuc)
                    elif sonraki["close"] < row["low"]:
                        sonuc = _kanit_us_checkpoint_sonuc_atr_olcekli(gunluk, idx + 1, "SHORT")
                        if sonuc is not None:
                            tum_sonuclar["İç Mum (Inside Bar)"].append(sonuc)

                if bool(yeni_zirve.iloc[idx]):
                    sonuc = _kanit_us_checkpoint_sonuc_atr_olcekli(gunluk, idx, "LONG")
                    if sonuc is not None:
                        tum_sonuclar["52-Hafta Zirve/Dip Kırılımı"].append(sonuc)
                elif bool(yeni_dip.iloc[idx]):
                    sonuc = _kanit_us_checkpoint_sonuc_atr_olcekli(gunluk, idx, "SHORT")
                    if sonuc is not None:
                        tum_sonuclar["52-Hafta Zirve/Dip Kırılımı"].append(sonuc)

                if bool(ust_yuruyus.iloc[idx]):
                    sonuc = _kanit_us_checkpoint_sonuc_atr_olcekli(gunluk, idx, "LONG")
                    if sonuc is not None:
                        tum_sonuclar["Bollinger Bandı Yürüyüşü"].append(sonuc)
                elif bool(alt_yuruyus.iloc[idx]):
                    sonuc = _kanit_us_checkpoint_sonuc_atr_olcekli(gunluk, idx, "SHORT")
                    if sonuc is not None:
                        tum_sonuclar["Bollinger Bandı Yürüyüşü"].append(sonuc)

                if bool(gap_yukari.iloc[idx]):
                    sonuc = _kanit_us_checkpoint_sonuc_atr_olcekli(gunluk, idx, "LONG")
                    if sonuc is not None:
                        tum_sonuclar["Gap Kırılımı"].append(sonuc)
                elif bool(gap_asagi.iloc[idx]):
                    sonuc = _kanit_us_checkpoint_sonuc_atr_olcekli(gunluk, idx, "SHORT")
                    if sonuc is not None:
                        tum_sonuclar["Gap Kırılımı"].append(sonuc)
        except Exception as e:
            print(f"[Sıkışma v3] {ticker} hata: {e}", flush=True)
        time.sleep(0.3)

    satirlar = _kanit_ozet_tablosu(tum_sonuclar)
    if not satirlar:
        return None, "Hiçbir strateji için yeterli veri üretilemedi."

    kor_long = next((s["kazanma_orani_pct"] for s in satirlar if s["strateji"] == "[KÖR] Koşulsuz LONG"), None)
    kor_long_r = next((s["ort_R"] for s in satirlar if s["strateji"] == "[KÖR] Koşulsuz LONG"), None)
    for s in satirlar:
        s["kor_isabet_farki"] = round(s["kazanma_orani_pct"] - kor_long, 2) if kor_long is not None and s["kazanma_orani_pct"] is not None else None
        s["kor_R_farki"] = round(s["ort_R"] - kor_long_r, 4) if kor_long_r is not None and s["ort_R"] is not None else None

    tablo = pd.DataFrame(satirlar).sort_values("kor_R_farki", ascending=False)
    dosya_yolu = _data_path("sikisma_turnuvasi_v3_atr_olcekli.csv")
    tablo.to_csv(dosya_yolu, index=False, encoding="utf-8-sig")
    return dosya_yolu, {"satirlar": satirlar, "hisse_listesi": hisse_listesi,
                         "denenen_hisse_sayisi": len(hisseler)}


# =============================================================================
# SIKIŞMA TURNUVASI v4 — HACİM DARALMA ÖRÜNTÜSÜ + %B STABİLİZASYONU
# =============================================================================
# 2026-08-19 - GEREKÇE: Kullanıcı Bollinger Bandı Yürüyüşü'nü (v3'ün en
# iyisi) reddetti - o "hareket zaten başladı, devam ediyor mu" sorusuna
# cevap veriyordu, "hareket henüz başlamadan" değil. İki YENİ, daha
# sofistike sıkışma kavramı:
# (1) HACİM DARALMA ÖRÜNTÜSÜ (Volume Contraction Pattern - Minervini'nin
#     tanıdığı bir yöntem): fiyat aralığı ART ARDA birkaç "dalga" boyunca
#     HER SEFERINDE daha dar hale geliyor VE hacim de AYNI ANDA azalıyor -
#     tek günlük NR7/İç Mum'dan daha güçlü, çok-periyotlu bir "sessizlik
#     birikiyor" hikayesi.
# (2) BOLLINGER %B STABİLİZASYONU: ham bant genişliği yerine, fiyatın
#     KENDİ bandı İÇİNDEKİ konumunun (%B) ne kadar İSTİKRARLI kaldığına
#     bakıyor - farklı bir sıkışma ölçüsü.
# AYNI kanıtlanmış metodoloji: volatil hisse evreni + ATR-ölçekli
# hedefler + release+AO yön mantığı (v3'te doğrulanan).

HACIM_DARALMA_PENCERE = 5
HACIM_DARALMA_DALGA_SAYISI = 3
PERCENT_B_STAB_PENCERE = 10
PERCENT_B_STAB_PERSENTIL = 0.15


def _hacim_daralma_orintusu_tespit(high, low, volume, n=5, dalga_sayisi=3):
    """Ardışık `dalga_sayisi` adet `n`-günlük pencerede hem fiyat aralığı
    hem hacim HER SEFERINDE (gecmisten bugune) daralıyor mu - Minervini
    tarzı 'Volume Contraction Pattern'."""
    aralik_ort = (high - low).rolling(n).mean()
    hacim_ort = volume.rolling(n).mean()
    daralma = pd.Series(True, index=high.index)
    hacim_azalis = pd.Series(True, index=high.index)
    for i in range(dalga_sayisi - 1):
        daralma = daralma & (aralik_ort.shift(i * n) < aralik_ort.shift((i + 1) * n))
        hacim_azalis = hacim_azalis & (hacim_ort.shift(i * n) < hacim_ort.shift((i + 1) * n))
    return daralma & hacim_azalis


def _percent_b_hesapla(close, n=20, k=2.0):
    orta = close.rolling(n).mean()
    std = close.rolling(n).std()
    alt, ust = orta - k * std, orta + k * std
    return (close - alt) / (ust - alt).replace(0, np.nan)


def _percent_b_stabilizasyon_tespit(percent_b, pencere=10, esik_persentil=0.15):
    """%B'nin KENDİ oynaklığı (rolling std) kendi 60-günlük tarihinde en
    düşük noktasında mı - farklı bir sıkışma ölçüsü (ham bant genişliği
    değil, fiyatın bant İÇİNDEKİ konum istikrarı)."""
    pb_std = percent_b.rolling(pencere).std()
    esik = pb_std.rolling(60).quantile(esik_persentil)
    return pb_std <= esik


def sikisma_turnuvasi_v4_calistir(hisse_listesi: str = "volatil", max_hisse: int = 12) -> tuple:
    """Hacim Daralma Örüntüsü + %B Stabilizasyonu'nu, v3'ün AYNI
    metodolojisiyle (ATR-ölçekli hedef, release+AO yön, volatil hisse
    evreni) test eder. Döner: (dosya_yolu, özet_dict) ya da (None,
    hata_mesajı)."""
    import yfinance as yf

    hisseler = (US_VOLATIL_TICKERS if hisse_listesi == "volatil" else US_INSIDER_TICKERS)[:max_hisse]
    isimler = ["Hacim Daralma Örüntüsü (release+AO)", "%B Stabilizasyonu (release+AO)"]
    tum_sonuclar = {isim: [] for isim in isimler}
    tum_sonuclar["[KÖR] Koşulsuz LONG"] = []
    tum_sonuclar["[KÖR] Koşulsuz SHORT"] = []

    for n_i, ticker in enumerate(hisseler, 1):
        try:
            print(f"[Sıkışma v4 {n_i}/{len(hisseler)}] {ticker}...", flush=True)
            gunluk = _yf_history_sert_zaman_asimli(ticker, "2y", "1d")
            if gunluk is None or gunluk.empty or len(gunluk) < 120:
                print(f"[Sıkışma v4] {ticker}: yetersiz veri, atlanıyor.", flush=True)
                continue
            gunluk = gunluk.rename(columns={"Close": "close", "High": "high", "Low": "low",
                                             "Open": "open", "Volume": "volume"})
            gunluk.index = pd.to_datetime(gunluk.index).tz_localize(None)
            gunluk = gunluk.reset_index(drop=True)

            tr = pd.concat([(gunluk["high"] - gunluk["low"]),
                             (gunluk["high"] - gunluk["close"].shift()).abs(),
                             (gunluk["low"] - gunluk["close"].shift()).abs()], axis=1).max(axis=1)
            gunluk["atr14"] = tr.rolling(14).mean()
            gunluk["ao"] = _awesome_oscillator_hesapla(gunluk["high"], gunluk["low"])

            gunluk["hacim_daralma"] = _hacim_daralma_orintusu_tespit(
                gunluk["high"], gunluk["low"], gunluk["volume"],
                HACIM_DARALMA_PENCERE, HACIM_DARALMA_DALGA_SAYISI)
            gunluk["percent_b"] = _percent_b_hesapla(gunluk["close"])
            gunluk["pb_stabil"] = _percent_b_stabilizasyon_tespit(
                gunluk["percent_b"], PERCENT_B_STAB_PENCERE, PERCENT_B_STAB_PERSENTIL)

            baslangic = max(HACIM_DARALMA_PENCERE * HACIM_DARALMA_DALGA_SAYISI, 60, 20) + 5
            for idx in range(baslangic, len(gunluk) - 11):
                row = gunluk.iloc[idx]
                onceki = gunluk.iloc[idx - 1]

                kor_long = _kanit_us_checkpoint_sonuc_atr_olcekli(gunluk, idx, "LONG")
                if kor_long is not None:
                    tum_sonuclar["[KÖR] Koşulsuz LONG"].append(kor_long)
                kor_short = _kanit_us_checkpoint_sonuc_atr_olcekli(gunluk, idx, "SHORT")
                if kor_short is not None:
                    tum_sonuclar["[KÖR] Koşulsuz SHORT"].append(kor_short)

                yon_ao = "LONG" if row["ao"] > 0 else ("SHORT" if row["ao"] < 0 else None)
                if not yon_ao:
                    continue

                if bool(onceki["hacim_daralma"]) and not bool(row["hacim_daralma"]):
                    sonuc = _kanit_us_checkpoint_sonuc_atr_olcekli(gunluk, idx, yon_ao)
                    if sonuc is not None:
                        tum_sonuclar["Hacim Daralma Örüntüsü (release+AO)"].append(sonuc)
                if bool(onceki["pb_stabil"]) and not bool(row["pb_stabil"]):
                    sonuc = _kanit_us_checkpoint_sonuc_atr_olcekli(gunluk, idx, yon_ao)
                    if sonuc is not None:
                        tum_sonuclar["%B Stabilizasyonu (release+AO)"].append(sonuc)
        except Exception as e:
            print(f"[Sıkışma v4] {ticker} hata: {e}", flush=True)
        time.sleep(0.3)

    satirlar = _kanit_ozet_tablosu(tum_sonuclar)
    if not satirlar:
        return None, "Hiçbir strateji için yeterli veri üretilemedi."

    kor_long = next((s["kazanma_orani_pct"] for s in satirlar if s["strateji"] == "[KÖR] Koşulsuz LONG"), None)
    kor_long_r = next((s["ort_R"] for s in satirlar if s["strateji"] == "[KÖR] Koşulsuz LONG"), None)
    for s in satirlar:
        s["kor_isabet_farki"] = round(s["kazanma_orani_pct"] - kor_long, 2) if kor_long is not None and s["kazanma_orani_pct"] is not None else None
        s["kor_R_farki"] = round(s["ort_R"] - kor_long_r, 4) if kor_long_r is not None and s["ort_R"] is not None else None

    tablo = pd.DataFrame(satirlar).sort_values("kor_R_farki", ascending=False)
    dosya_yolu = _data_path("sikisma_turnuvasi_v4.csv")
    tablo.to_csv(dosya_yolu, index=False, encoding="utf-8-sig")
    return dosya_yolu, {"satirlar": satirlar, "hisse_listesi": hisse_listesi,
                         "denenen_hisse_sayisi": len(hisseler)}


# =============================================================================
# GÜN-İÇİ GİRİŞ + GÜN-İÇİ ÇIKIŞ TURNUVASI (GERÇEK DAY-TRADE) — 2026-08-19
# =============================================================================
# GEREKÇE: Kullanıcı en baştan beri AYNI GÜN için alıp-satabileceği bir
# sistem istiyordu (SUPX örneği - yatay/hafif düşüş sonrası dağ gibi
# yükseliş, hepsi TEK GÜN içinde). Bugüne kadarki TÜM testler (8'li
# cephanelik dahil) 1-10 GÜNLÜK checkpoint kullanıyordu - kullanıcının
# asıl isteğine hiç uymuyordu. Bu, DOĞRU soruyu soran ilk test:
# - GİRİŞ: gün içi 15dk barlarla, bugüne kadarki NEREDEYSE TÜM
#   göstergeler (tersine dönüş + trend + sıkışma aileleri)
# - ÇIKIŞ: SADECE AYNI GÜN İÇİNDE - hedefe ulaşırsa WIN, gün biterse
#   GERÇEK kapanış fiyatıyla (sabit -1 değil, gerçek kâr/zarar) kapanır
# - HEDEF: ATR-ölçekli (volatil hisselerde sabit yüzde anlamsızlaşıyor,
#   bugün zaten öğrendik)
# - EVREN: volatil/küçük hisseler (kullanıcının kendi gözlemi - böyle
#   patlamalar büyük/sakin hisselerde değil, küçük hisselerde oluyor)

GUN_ICI_ATR_HEDEFI = 1.0  # ayni gun icinde ATR'nin bu kati hedef


def _gun_ici_cikis_sonucu(barlar_15dk: pd.DataFrame, giris_konum: int, yon: str,
                            gunluk_atr: float, hedef_atr_kati: float = GUN_ICI_ATR_HEDEFI):
    """SADECE AYNI GÜN içinde ilerler. Hedef (ATR'nin X katı) tutarsa
    WIN, gün biterse GERÇEK kapanış fiyatıyla (gerçek R) kapanır - sabit
    -1 DEĞİL. Döner: (sonuç_etiketi, R_değeri)."""
    giris_fiyat = barlar_15dk.iloc[giris_konum]["close"]
    giris_gun = barlar_15dk.iloc[giris_konum]["gun"]
    if pd.isna(gunluk_atr) or gunluk_atr == 0:
        return None

    son_konum = giris_konum
    for offset in range(1, 40):  # bir gunde en fazla ~26 tane 15dk bar var (6.5 saat), 40 guvenli ust sinir
        aday_konum = giris_konum + offset
        if aday_konum >= len(barlar_15dk):
            break
        bar = barlar_15dk.iloc[aday_konum]
        if bar["gun"] != giris_gun:
            break  # gun degisti, dur
        son_konum = aday_konum
        hedef_fiyat = (giris_fiyat + hedef_atr_kati * gunluk_atr if yon == "LONG"
                       else giris_fiyat - hedef_atr_kati * gunluk_atr)
        if yon == "LONG" and bar["high"] >= hedef_fiyat:
            return "WIN", hedef_atr_kati
        if yon == "SHORT" and bar["low"] <= hedef_fiyat:
            return "WIN", hedef_atr_kati

    # hedef tutmadi - GUN SONU kapanisiyla GERCEK R hesapla
    son_fiyat = barlar_15dk.iloc[son_konum]["close"]
    fiyat_farki = (son_fiyat - giris_fiyat) if yon == "LONG" else (giris_fiyat - son_fiyat)
    gercek_r = fiyat_farki / gunluk_atr
    return "EOD_KAPANIS", round(gercek_r, 3)


def gun_ici_giris_cikis_turnuvasi_calistir(hisse_listesi: str = "volatil", max_hisse: int = 12) -> tuple:
    """Bugüne kadarki NEREDEYSE TÜM göstergeleri (tersine dönüş + trend +
    sıkışma) GERÇEK aynı-gün giriş/çıkış mantığıyla test eder - hiçbir
    sinyal ertesi güne taşınmaz. Volatil hisse evreninde, ATR-ölçekli
    hedefle. Döner: (dosya_yolu, özet_dict) ya da (None, hata_mesajı)."""
    import yfinance as yf

    hisseler = (US_VOLATIL_TICKERS if hisse_listesi == "volatil" else US_INSIDER_TICKERS)[:max_hisse]
    isimler = [
        "Bollinger dokunuşu", "Stochastic %K", "CCI", "RSI21", "VWAP sapması", "MFI",
        "Donchian-20 kırılımı", "EMA9/21 kesişimi", "ADX+DI yön", "MACD kesişimi",
        "PSAR dönüşü", "Awesome Oscillator",
        "NR7 kırılımı", "İç Mum kırılımı", "Hacim Daralma Örüntüsü (release+AO)",
        "%B Stabilizasyonu (release+AO)",
    ]
    tum_sonuclar = {isim: [] for isim in isimler}
    tum_sonuclar["[KÖR] Koşulsuz LONG"] = []
    tum_sonuclar["[KÖR] Koşulsuz SHORT"] = []

    for n_i, ticker in enumerate(hisseler, 1):
        try:
            print(f"[Gün İçi Turnuva {n_i}/{len(hisseler)}] {ticker}...", flush=True)
            gunluk = _yf_history_sert_zaman_asimli(ticker, "60d", "1d")
            barlar_15dk = _yf_history_sert_zaman_asimli(ticker, "60d", "15m")
            if gunluk is None or gunluk.empty or barlar_15dk is None or barlar_15dk.empty:
                continue
            gunluk = gunluk.rename(columns={"Close": "close", "High": "high", "Low": "low"})
            gunluk.index = pd.to_datetime(gunluk.index).tz_localize(None)
            tr = pd.concat([(gunluk["high"] - gunluk["low"]),
                             (gunluk["high"] - gunluk["close"].shift()).abs(),
                             (gunluk["low"] - gunluk["close"].shift()).abs()], axis=1).max(axis=1)
            gunluk["atr14"] = tr.rolling(14).mean()
            gunluk_atr_harita = gunluk["atr14"].to_dict()
            gunluk_tarihleri = [d.date() for d in gunluk.index]
            gunluk_atr_gun_bazli = dict(zip(gunluk_tarihleri, gunluk["atr14"].values))

            barlar_15dk = barlar_15dk.reset_index().rename(columns={
                "Datetime": "ts", "Open": "open", "High": "high", "Low": "low",
                "Close": "close", "Volume": "volume"})
            if "ts" not in barlar_15dk.columns:
                barlar_15dk = barlar_15dk.rename(columns={barlar_15dk.columns[0]: "ts"})
            barlar_15dk["ts"] = pd.to_datetime(barlar_15dk["ts"]).dt.tz_localize(None)
            barlar_15dk["gun"] = barlar_15dk["ts"].dt.date

            # --- TUM GOSTERGELERI HESAPLA ---
            barlar_15dk["rsi21"] = _rsi_hesapla(barlar_15dk["close"], 21)
            barlar_15dk["stoch"] = _stochastic_hesapla(barlar_15dk["high"], barlar_15dk["low"], barlar_15dk["close"], 14)
            barlar_15dk["cci"] = _cci_hesapla(barlar_15dk["high"], barlar_15dk["low"], barlar_15dk["close"], 20)
            barlar_15dk["mfi"] = _mfi_hesapla(barlar_15dk["high"], barlar_15dk["low"], barlar_15dk["close"], barlar_15dk["volume"], 14)
            boll_alt, boll_ust = _bollinger_hesapla(barlar_15dk["close"], 20, 2.0)
            barlar_15dk["boll_alt"], barlar_15dk["boll_ust"] = boll_alt, boll_ust
            barlar_15dk["vwap"] = _vwap_gun_ici_hesapla(barlar_15dk["high"], barlar_15dk["low"], barlar_15dk["close"], barlar_15dk["volume"], barlar_15dk["gun"])
            barlar_15dk["vwap_sapma_pct"] = (barlar_15dk["close"] - barlar_15dk["vwap"]) / barlar_15dk["vwap"] * 100
            barlar_15dk["donchian_ust"] = barlar_15dk["high"].rolling(20).max()
            barlar_15dk["donchian_alt"] = barlar_15dk["low"].rolling(20).min()
            barlar_15dk["ema_hizli"] = _ema_hesapla(barlar_15dk["close"], 9)
            barlar_15dk["ema_yavas"] = _ema_hesapla(barlar_15dk["close"], 21)
            adx, plus_di, minus_di = _adx_di_hesapla(barlar_15dk["high"], barlar_15dk["low"], barlar_15dk["close"], 14)
            barlar_15dk["adx"], barlar_15dk["plus_di"], barlar_15dk["minus_di"] = adx, plus_di, minus_di
            macd_line, macd_sinyal = _macd_hesapla(barlar_15dk["close"], 12, 26, 9)
            barlar_15dk["macd_line"], barlar_15dk["macd_sinyal"] = macd_line, macd_sinyal
            _, trend_yukari = _psar_hesapla(barlar_15dk["high"], barlar_15dk["low"], barlar_15dk["close"])
            barlar_15dk["psar_yukari"] = trend_yukari
            barlar_15dk["ao"] = _awesome_oscillator_hesapla(barlar_15dk["high"], barlar_15dk["low"])
            barlar_15dk["nr7"] = _nr7_tespit(barlar_15dk["high"], barlar_15dk["low"], 7)
            barlar_15dk["ic_mum"] = _ic_mum_tespit(barlar_15dk["high"], barlar_15dk["low"])
            barlar_15dk["hacim_daralma"] = _hacim_daralma_orintusu_tespit(barlar_15dk["high"], barlar_15dk["low"], barlar_15dk["volume"], 5, 3)
            barlar_15dk["percent_b"] = _percent_b_hesapla(barlar_15dk["close"])
            barlar_15dk["pb_stabil"] = _percent_b_stabilizasyon_tespit(barlar_15dk["percent_b"], 10, 0.15)

            tetiklenen = {isim: set() for isim in isimler}
            baslangic_konum = 40
            for idx in range(baslangic_konum, len(barlar_15dk) - 1):
                bar = barlar_15dk.iloc[idx]
                onceki = barlar_15dk.iloc[idx - 1]
                gun = bar["gun"]
                gunluk_atr = gunluk_atr_gun_bazli.get(gun)
                if gunluk_atr is None or pd.isna(gunluk_atr):
                    continue

                # KOR CIZGI - ayni gun-ici cikisla
                tum_sonuclar["[KÖR] Koşulsuz LONG"].append(_gun_ici_cikis_sonucu(barlar_15dk, idx, "LONG", gunluk_atr))
                tum_sonuclar["[KÖR] Koşulsuz SHORT"].append(_gun_ici_cikis_sonucu(barlar_15dk, idx, "SHORT", gunluk_atr))

                adaylar = []
                if gun not in tetiklenen["Bollinger dokunuşu"] and pd.notna(bar["boll_alt"]):
                    if bar["close"] <= bar["boll_alt"]:
                        adaylar.append(("Bollinger dokunuşu", "LONG"))
                    elif bar["close"] >= bar["boll_ust"]:
                        adaylar.append(("Bollinger dokunuşu", "SHORT"))
                if gun not in tetiklenen["Stochastic %K"] and pd.notna(bar["stoch"]):
                    if bar["stoch"] <= 20:
                        adaylar.append(("Stochastic %K", "LONG"))
                    elif bar["stoch"] >= 80:
                        adaylar.append(("Stochastic %K", "SHORT"))
                if gun not in tetiklenen["CCI"] and pd.notna(bar["cci"]):
                    if bar["cci"] <= -100:
                        adaylar.append(("CCI", "LONG"))
                    elif bar["cci"] >= 100:
                        adaylar.append(("CCI", "SHORT"))
                if gun not in tetiklenen["RSI21"] and pd.notna(bar["rsi21"]):
                    if bar["rsi21"] <= 25:
                        adaylar.append(("RSI21", "LONG"))
                    elif bar["rsi21"] >= 75:
                        adaylar.append(("RSI21", "SHORT"))
                if gun not in tetiklenen["VWAP sapması"] and pd.notna(bar["vwap_sapma_pct"]):
                    if bar["vwap_sapma_pct"] <= -1.0:
                        adaylar.append(("VWAP sapması", "LONG"))
                    elif bar["vwap_sapma_pct"] >= 1.0:
                        adaylar.append(("VWAP sapması", "SHORT"))
                if gun not in tetiklenen["MFI"] and pd.notna(bar["mfi"]):
                    if bar["mfi"] <= 20:
                        adaylar.append(("MFI", "LONG"))
                    elif bar["mfi"] >= 80:
                        adaylar.append(("MFI", "SHORT"))
                if gun not in tetiklenen["Donchian-20 kırılımı"] and pd.notna(bar["donchian_ust"]):
                    if bar["close"] >= bar["donchian_ust"]:
                        adaylar.append(("Donchian-20 kırılımı", "LONG"))
                    elif bar["close"] <= bar["donchian_alt"]:
                        adaylar.append(("Donchian-20 kırılımı", "SHORT"))
                if gun not in tetiklenen["EMA9/21 kesişimi"] and pd.notna(bar["ema_hizli"]) and pd.notna(onceki["ema_hizli"]):
                    if onceki["ema_hizli"] <= onceki["ema_yavas"] and bar["ema_hizli"] > bar["ema_yavas"]:
                        adaylar.append(("EMA9/21 kesişimi", "LONG"))
                    elif onceki["ema_hizli"] >= onceki["ema_yavas"] and bar["ema_hizli"] < bar["ema_yavas"]:
                        adaylar.append(("EMA9/21 kesişimi", "SHORT"))
                if gun not in tetiklenen["ADX+DI yön"] and pd.notna(bar["adx"]) and bar["adx"] >= 25 and pd.notna(onceki["plus_di"]):
                    if onceki["plus_di"] <= onceki["minus_di"] and bar["plus_di"] > bar["minus_di"]:
                        adaylar.append(("ADX+DI yön", "LONG"))
                    elif onceki["plus_di"] >= onceki["minus_di"] and bar["plus_di"] < bar["minus_di"]:
                        adaylar.append(("ADX+DI yön", "SHORT"))
                if gun not in tetiklenen["MACD kesişimi"] and pd.notna(bar["macd_line"]) and pd.notna(onceki["macd_line"]):
                    if onceki["macd_line"] <= onceki["macd_sinyal"] and bar["macd_line"] > bar["macd_sinyal"]:
                        adaylar.append(("MACD kesişimi", "LONG"))
                    elif onceki["macd_line"] >= onceki["macd_sinyal"] and bar["macd_line"] < bar["macd_sinyal"]:
                        adaylar.append(("MACD kesişimi", "SHORT"))
                if gun not in tetiklenen["PSAR dönüşü"] and pd.notna(bar["psar_yukari"]):
                    if bool(bar["psar_yukari"]) and not bool(onceki["psar_yukari"]):
                        adaylar.append(("PSAR dönüşü", "LONG"))
                    elif not bool(bar["psar_yukari"]) and bool(onceki["psar_yukari"]):
                        adaylar.append(("PSAR dönüşü", "SHORT"))
                if gun not in tetiklenen["Awesome Oscillator"] and pd.notna(bar["ao"]) and pd.notna(onceki["ao"]):
                    if onceki["ao"] <= 0 and bar["ao"] > 0:
                        adaylar.append(("Awesome Oscillator", "LONG"))
                    elif onceki["ao"] >= 0 and bar["ao"] < 0:
                        adaylar.append(("Awesome Oscillator", "SHORT"))
                if gun not in tetiklenen["NR7 kırılımı"] and bool(bar["nr7"]):
                    sonraki = barlar_15dk.iloc[idx + 1] if idx + 1 < len(barlar_15dk) else None
                    if sonraki is not None and sonraki["gun"] == gun:
                        if sonraki["close"] > bar["high"]:
                            adaylar.append(("NR7 kırılımı", "LONG"))
                        elif sonraki["close"] < bar["low"]:
                            adaylar.append(("NR7 kırılımı", "SHORT"))
                if gun not in tetiklenen["İç Mum kırılımı"] and bool(bar["ic_mum"]):
                    sonraki = barlar_15dk.iloc[idx + 1] if idx + 1 < len(barlar_15dk) else None
                    if sonraki is not None and sonraki["gun"] == gun:
                        if sonraki["close"] > bar["high"]:
                            adaylar.append(("İç Mum kırılımı", "LONG"))
                        elif sonraki["close"] < bar["low"]:
                            adaylar.append(("İç Mum kırılımı", "SHORT"))
                yon_ao = "LONG" if (pd.notna(bar["ao"]) and bar["ao"] > 0) else ("SHORT" if pd.notna(bar["ao"]) else None)
                if yon_ao and gun not in tetiklenen["Hacim Daralma Örüntüsü (release+AO)"] and bool(onceki["hacim_daralma"]) and not bool(bar["hacim_daralma"]):
                    adaylar.append(("Hacim Daralma Örüntüsü (release+AO)", yon_ao))
                if yon_ao and gun not in tetiklenen["%B Stabilizasyonu (release+AO)"] and bool(onceki["pb_stabil"]) and not bool(bar["pb_stabil"]):
                    adaylar.append(("%B Stabilizasyonu (release+AO)", yon_ao))

                for isim, yon in adaylar:
                    tetiklenen[isim].add(gun)
                    sonuc = _gun_ici_cikis_sonucu(barlar_15dk, idx, yon, gunluk_atr)
                    if sonuc is not None:
                        tum_sonuclar[isim].append(sonuc)
        except Exception as e:
            print(f"[Gün İçi Turnuva] {ticker} hata: {e}", flush=True)
        time.sleep(0.3)

    satirlar = _kanit_ozet_tablosu(tum_sonuclar)
    if not satirlar:
        return None, "Hiçbir strateji için yeterli veri üretilemedi."

    kor_long = next((s["kazanma_orani_pct"] for s in satirlar if s["strateji"] == "[KÖR] Koşulsuz LONG"), None)
    kor_long_r = next((s["ort_R"] for s in satirlar if s["strateji"] == "[KÖR] Koşulsuz LONG"), None)
    for s in satirlar:
        s["kor_isabet_farki"] = round(s["kazanma_orani_pct"] - kor_long, 2) if kor_long is not None and s["kazanma_orani_pct"] is not None else None
        s["kor_R_farki"] = round(s["ort_R"] - kor_long_r, 4) if kor_long_r is not None and s["ort_R"] is not None else None

    tablo = pd.DataFrame(satirlar).sort_values("kor_R_farki", ascending=False)
    dosya_yolu = _data_path("gun_ici_giris_cikis_turnuvasi.csv")
    tablo.to_csv(dosya_yolu, index=False, encoding="utf-8-sig")
    return dosya_yolu, {"satirlar": satirlar, "hisse_listesi": hisse_listesi,
                         "denenen_hisse_sayisi": len(hisseler)}


# =============================================================================
# "BÜYÜK PATLAMA GÜNÜ" TESTİ — ERKEN ÇIKIŞ YOK, GÜN SONUNA KADAR TUT
# =============================================================================
# 2026-08-19 - GEREKÇE: Önceki gün-içi testlerde 1x ATR hedefine ulaşınca
# HEMEN çıkıyorduk - bu, SUPX gibi %21 patlayan bir günde bile sadece
# %5-8'de çıkıp geri kalan büyük hareketi KAÇIRDIĞIMIZ anlamına geliyor.
# Bu test FARKLI bir soru soruyor: "Sinyal tetiklendiğinde, pozisyonu
# HİÇ ERKEN ÇIKMADAN gün sonuna kadar tutsaydık, GERÇEKTEN büyük
# (%10+, %20+) günler yakalıyor muyuz?" Rapor artık ortalama R değil,
# GERÇEK YÜZDE GETİRİ DAĞILIMI (kaç tanesi büyük patlama oldu).
# Adaylar: bugüne kadarki TÜM sıkışma/kırılma ailesi + YENİ bir aday
# (Saf Hacim Patlaması - başka hiçbir şeye bakmadan sadece anormal
# hacim artışı). GENİŞLETİLMİŞ volatil hisse evreninde.

HACIM_PATLAMASI_KATI = 3.0  # ortalama hacmin bu katindan fazlasi


def _hacim_patlamasi_tespit(volume, pencere=20, kat=3.0):
    """SAF hacim patlaması - fiyat/başka hiçbir koşula bakmadan, sadece
    o günün hacmi son N günün ortalamasının kaç katı."""
    ort_hacim = volume.rolling(pencere).mean().shift(1)  # BUGUNU haric onceki ortalama
    return volume >= kat * ort_hacim


def _tam_gun_tutma_getirisi(barlar_15dk: pd.DataFrame, giris_konum: int, yon: str):
    """Pozisyonu HİÇ ERKEN ÇIKMADAN gün sonuna kadar tutar - GERÇEK
    yüzde getiriyi döner (ATR'a göre DEĞİL, doğrudan %). None dönerse
    veri yetersiz demektir."""
    giris_fiyat = barlar_15dk.iloc[giris_konum]["close"]
    giris_gun = barlar_15dk.iloc[giris_konum]["gun"]
    son_konum = giris_konum
    for offset in range(1, 40):
        aday = giris_konum + offset
        if aday >= len(barlar_15dk):
            break
        bar = barlar_15dk.iloc[aday]
        if bar["gun"] != giris_gun:
            break
        son_konum = aday
    if son_konum == giris_konum:
        return None  # gunun son bariydi, ilerleme yok
    son_fiyat = barlar_15dk.iloc[son_konum]["close"]
    if yon == "LONG":
        return (son_fiyat - giris_fiyat) / giris_fiyat * 100
    else:
        return (giris_fiyat - son_fiyat) / giris_fiyat * 100


def buyuk_patlama_gunu_testi_calistir(hisse_listesi: str = "volatil", max_hisse: int = 60) -> tuple:
    """İç Mum, NR7, ve tüm sıkışma ailesi + Saf Hacim Patlaması'nı ERKEN
    ÇIKIŞ OLMADAN (gün sonuna kadar tutarak) test eder - GERÇEK yüzde
    getiri dağılımını (ort., medyan, %10+ ve %20+ oranı) raporlar.
    Döner: (dosya_yolu, özet_dict) ya da (None, hata_mesajı)."""
    isimler = ["İç Mum kırılımı", "NR7 kırılımı", "Bollinger Genişlik Sıkışması",
               "TTM Squeeze", "ATR Persentil Sıkışması", "Hacim Daralma Örüntüsü",
               "%B Stabilizasyonu", "52-Hafta Zirve/Dip Kırılımı", "Gap Kırılımı",
               "Saf Hacim Patlaması", "[KÖR] Koşulsuz LONG", "[KÖR] Koşulsuz SHORT"]
    tum_getiriler = {isim: [] for isim in isimler}

    hisseler = (US_VOLATIL_TICKERS if hisse_listesi == "volatil" else US_INSIDER_TICKERS)[:max_hisse]

    for n_i, ticker in enumerate(hisseler, 1):
        try:
            print(f"[Büyük Patlama Testi {n_i}/{len(hisseler)}] {ticker}...", flush=True)
            barlar_15dk = _yf_history_sert_zaman_asimli(ticker, "60d", "15m")
            if barlar_15dk is None or barlar_15dk.empty:
                continue
            barlar_15dk = barlar_15dk.reset_index().rename(columns={
                "Datetime": "ts", "Open": "open", "High": "high", "Low": "low",
                "Close": "close", "Volume": "volume"})
            if "ts" not in barlar_15dk.columns:
                barlar_15dk = barlar_15dk.rename(columns={barlar_15dk.columns[0]: "ts"})
            barlar_15dk["ts"] = pd.to_datetime(barlar_15dk["ts"]).dt.tz_localize(None)
            barlar_15dk["gun"] = barlar_15dk["ts"].dt.date

            barlar_15dk["nr7"] = _nr7_tespit(barlar_15dk["high"], barlar_15dk["low"], 7)
            barlar_15dk["ic_mum"] = _ic_mum_tespit(barlar_15dk["high"], barlar_15dk["low"])
            barlar_15dk["boll_sikisma"] = _boll_genislik_sikisma_tespit(barlar_15dk["close"], 20, 2.0, 60, 0.10)
            barlar_15dk["ttm_squeeze"] = _ttm_squeeze_tespit(barlar_15dk["high"], barlar_15dk["low"], barlar_15dk["close"])
            barlar_15dk["atr_sikisma"] = _atr_persentil_sikisma_tespit(barlar_15dk["high"], barlar_15dk["low"], barlar_15dk["close"], 100, 0.10)
            barlar_15dk["hacim_daralma"] = _hacim_daralma_orintusu_tespit(barlar_15dk["high"], barlar_15dk["low"], barlar_15dk["volume"], 5, 3)
            barlar_15dk["percent_b"] = _percent_b_hesapla(barlar_15dk["close"])
            barlar_15dk["pb_stabil"] = _percent_b_stabilizasyon_tespit(barlar_15dk["percent_b"], 10, 0.15)
            barlar_15dk["ao"] = _awesome_oscillator_hesapla(barlar_15dk["high"], barlar_15dk["low"])
            boll_orta = barlar_15dk["close"].rolling(20).mean()
            boll_std = barlar_15dk["close"].rolling(20).std()
            alt_bant, ust_bant = boll_orta - 2.0 * boll_std, boll_orta + 2.0 * boll_std
            barlar_15dk["yeni_zirve"], barlar_15dk["yeni_dip"] = _52_hafta_kirilim_tespit(
                barlar_15dk["high"], barlar_15dk["low"], barlar_15dk["close"], 252)
            barlar_15dk["gap_yukari"], barlar_15dk["gap_asagi"] = _gap_kirilim_tespit(
                barlar_15dk["open"], barlar_15dk["close"], barlar_15dk["close"].shift(1))
            barlar_15dk["hacim_patlamasi"] = _hacim_patlamasi_tespit(barlar_15dk["volume"], 20, HACIM_PATLAMASI_KATI)

            tetiklenen = {isim: set() for isim in isimler}
            baslangic = 105
            for idx in range(baslangic, len(barlar_15dk) - 1):
                row = barlar_15dk.iloc[idx]
                onceki = barlar_15dk.iloc[idx - 1]
                gun = row["gun"]

                yon_ao = "LONG" if (pd.notna(row["ao"]) and row["ao"] > 0) else ("SHORT" if pd.notna(row["ao"]) else None)

                # KOR CIZGI
                for yon_kor in ("LONG", "SHORT"):
                    getiri = _tam_gun_tutma_getirisi(barlar_15dk, idx, yon_kor)
                    if getiri is not None:
                        tum_getiriler[f"[KÖR] Koşulsuz {yon_kor}"].append(getiri)

                if gun not in tetiklenen["İç Mum kırılımı"] and bool(row["ic_mum"]):
                    sonraki = barlar_15dk.iloc[idx + 1] if idx + 1 < len(barlar_15dk) else None
                    if sonraki is not None and sonraki["gun"] == gun:
                        y = "LONG" if sonraki["close"] > row["high"] else ("SHORT" if sonraki["close"] < row["low"] else None)
                        if y:
                            tetiklenen["İç Mum kırılımı"].add(gun)
                            g = _tam_gun_tutma_getirisi(barlar_15dk, idx + 1, y)
                            if g is not None:
                                tum_getiriler["İç Mum kırılımı"].append(g)
                if gun not in tetiklenen["NR7 kırılımı"] and bool(row["nr7"]):
                    sonraki = barlar_15dk.iloc[idx + 1] if idx + 1 < len(barlar_15dk) else None
                    if sonraki is not None and sonraki["gun"] == gun:
                        y = "LONG" if sonraki["close"] > row["high"] else ("SHORT" if sonraki["close"] < row["low"] else None)
                        if y:
                            tetiklenen["NR7 kırılımı"].add(gun)
                            g = _tam_gun_tutma_getirisi(barlar_15dk, idx + 1, y)
                            if g is not None:
                                tum_getiriler["NR7 kırılımı"].append(g)
                if yon_ao and gun not in tetiklenen["Bollinger Genişlik Sıkışması"] and bool(onceki["boll_sikisma"]) and not bool(row["boll_sikisma"]):
                    tetiklenen["Bollinger Genişlik Sıkışması"].add(gun)
                    g = _tam_gun_tutma_getirisi(barlar_15dk, idx, yon_ao)
                    if g is not None:
                        tum_getiriler["Bollinger Genişlik Sıkışması"].append(g)
                if yon_ao and gun not in tetiklenen["TTM Squeeze"] and bool(onceki["ttm_squeeze"]) and not bool(row["ttm_squeeze"]):
                    tetiklenen["TTM Squeeze"].add(gun)
                    g = _tam_gun_tutma_getirisi(barlar_15dk, idx, yon_ao)
                    if g is not None:
                        tum_getiriler["TTM Squeeze"].append(g)
                if yon_ao and gun not in tetiklenen["ATR Persentil Sıkışması"] and bool(onceki["atr_sikisma"]) and not bool(row["atr_sikisma"]):
                    tetiklenen["ATR Persentil Sıkışması"].add(gun)
                    g = _tam_gun_tutma_getirisi(barlar_15dk, idx, yon_ao)
                    if g is not None:
                        tum_getiriler["ATR Persentil Sıkışması"].append(g)
                if yon_ao and gun not in tetiklenen["Hacim Daralma Örüntüsü"] and bool(onceki["hacim_daralma"]) and not bool(row["hacim_daralma"]):
                    tetiklenen["Hacim Daralma Örüntüsü"].add(gun)
                    g = _tam_gun_tutma_getirisi(barlar_15dk, idx, yon_ao)
                    if g is not None:
                        tum_getiriler["Hacim Daralma Örüntüsü"].append(g)
                if yon_ao and gun not in tetiklenen["%B Stabilizasyonu"] and bool(onceki["pb_stabil"]) and not bool(row["pb_stabil"]):
                    tetiklenen["%B Stabilizasyonu"].add(gun)
                    g = _tam_gun_tutma_getirisi(barlar_15dk, idx, yon_ao)
                    if g is not None:
                        tum_getiriler["%B Stabilizasyonu"].append(g)
                if gun not in tetiklenen["52-Hafta Zirve/Dip Kırılımı"]:
                    if bool(row["yeni_zirve"]):
                        tetiklenen["52-Hafta Zirve/Dip Kırılımı"].add(gun)
                        g = _tam_gun_tutma_getirisi(barlar_15dk, idx, "LONG")
                        if g is not None:
                            tum_getiriler["52-Hafta Zirve/Dip Kırılımı"].append(g)
                    elif bool(row["yeni_dip"]):
                        tetiklenen["52-Hafta Zirve/Dip Kırılımı"].add(gun)
                        g = _tam_gun_tutma_getirisi(barlar_15dk, idx, "SHORT")
                        if g is not None:
                            tum_getiriler["52-Hafta Zirve/Dip Kırılımı"].append(g)
                if gun not in tetiklenen["Gap Kırılımı"]:
                    if bool(row["gap_yukari"]):
                        tetiklenen["Gap Kırılımı"].add(gun)
                        g = _tam_gun_tutma_getirisi(barlar_15dk, idx, "LONG")
                        if g is not None:
                            tum_getiriler["Gap Kırılımı"].append(g)
                    elif bool(row["gap_asagi"]):
                        tetiklenen["Gap Kırılımı"].add(gun)
                        g = _tam_gun_tutma_getirisi(barlar_15dk, idx, "SHORT")
                        if g is not None:
                            tum_getiriler["Gap Kırılımı"].append(g)
                if yon_ao and gun not in tetiklenen["Saf Hacim Patlaması"] and bool(row["hacim_patlamasi"]):
                    tetiklenen["Saf Hacim Patlaması"].add(gun)
                    g = _tam_gun_tutma_getirisi(barlar_15dk, idx, yon_ao)
                    if g is not None:
                        tum_getiriler["Saf Hacim Patlaması"].append(g)
        except Exception as e:
            print(f"[Büyük Patlama Testi] {ticker} hata: {e}", flush=True)
        time.sleep(0.3)

    satirlar = []
    for isim, getiriler in tum_getiriler.items():
        if not getiriler:
            continue
        arr = np.array(getiriler)
        satirlar.append({
            "strateji": isim, "n": len(arr),
            "ort_getiri_pct": round(float(np.mean(arr)), 3),
            "medyan_getiri_pct": round(float(np.median(arr)), 3),
            "kazanma_orani_pct": round(float((arr > 0).mean() * 100), 2),
            "yuzde10_ustu_oran_pct": round(float((arr >= 10).mean() * 100), 2),
            "yuzde20_ustu_oran_pct": round(float((arr >= 20).mean() * 100), 2),
            "en_iyi_pct": round(float(arr.max()), 2),
            "en_kotu_pct": round(float(arr.min()), 2),
        })
    if not satirlar:
        return None, "Hiçbir strateji için yeterli veri üretilemedi."

    tablo = pd.DataFrame(satirlar).sort_values("yuzde10_ustu_oran_pct", ascending=False)
    dosya_yolu = _data_path("buyuk_patlama_gunu_testi.csv")
    tablo.to_csv(dosya_yolu, index=False, encoding="utf-8-sig")
    return dosya_yolu, {"satirlar": satirlar, "hisse_listesi": hisse_listesi,
                         "denenen_hisse_sayisi": len(hisseler)}


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
        "/makro_gurultu_testi — geceki S&P500/USDTRY hareketinin XU100/"
        "hisse getirisiyle ilişkisini ölçer, 'sakin gece' eşiği önerir\n"
        "/piyasa_sapmasi_testi — piyasa çoğunluğuna uymayan (sapan) "
        "hisselerin ertesi gün DEVAM mı GERİ Mİ DÖNDÜĞÜNÜ test eder\n"
        "/piyasa_sapmasi_kanit_dogrulama — sapan vs uyumlu hisseleri "
        "BIST'in gerçek çıkış mantığıyla karşılaştırmalı izole doğrular\n"
        "/xu100_makro_zamanlama — S&P500/USDTRY'nin XU100'ün kendisiyle "
        "ilişkisini 5 ufukta (1g-1ay) test eder\n"
        "/deger_testi — hisselerin F/K oranına göre ucuz/pahalı gruplarının "
        "1/3/6 ay performansını karşılaştırır (klasik değer yatırımı testi)\n"
        "/ai_model_backtest — model.pkl ve overnight_model.pkl'yi GERÇEKTEN "
        "yükleyip son 60 günün 15dk verisiyle gerçek tahminlerini test eder\n"
        "/ai_ozellik_onemi — iki modelin her özelliğe (has_catalyst dahil) "
        "ne kadar önem verdiğini gösterir\n"
        "/gun_ici_tarama_testi — ATR Kırılımı/Hacim Z-Skor'u günde bir "
        "yerine gün içi (15dk) sürekli tarasak ne olur, kapanış-bazlı "
        "kontrol grubuyla karşılaştırır (izole deney, canlıya dokunmaz)\n"
        "/rsi21_hedef_kiyasi — RSI21 gün içi sinyalini canlının küçük "
        "hedefleriyle (%0.15-0.90) ve gerçekten anlamlı büyük hedeflerle "
        "(%1-5) karşılaştırır\n"
        "/rsi21_bist_testi — RSI21 gün içi sinyalini BIST hisselerinde, "
        "BIST'in gerçek çıkış mantığıyla (1.5R kısmi TP + trailing) test eder\n"
        "/yeni_gosterge_turnuvasi_us — Stochastic/CCI/MFI/Bollinger'ı ABD'de "
        "gün içi aşırı uç + gerçek büyük hedeflerle (RSI21 yöntemiyle) test eder\n"
        "/nihai_kor_kiyasi — 7 ABD stratejisinin (ATR, Hacim, RSI21, "
        "Stochastic, CCI, MFI, Bollinger) HEPSİNİ tek taramada kör temel "
        "çizgiye karşı kıyaslar, gerçek edge'i (kör'den fark) gösterir\n"
        "/gosterge_cephaneligi [HİSSE_SAYISI] — 4 güçlü sinyalin örtüşmesi + "
        "13 trend/momentum adayı (Donchian, EMA, MACD, ADX+DI, ROC, RSI50, "
        "PSAR, hacim-onaylı kırılım, Keltner, Williams %R, VWAP sapması, "
        "Üçlü EMA, Awesome Oscillator), kör çizgiyle birlikte (varsayılan "
        "25 hisse, daha büyük örneklem için sayı belirt)\n"
        "/sikisma_turnuvasi [HİSSE_SAYISI] — NR7/İç Mum/Bollinger Genişlik/"
        "TTM Squeeze/ATR Persentil - hareket başlamadan önce yakalayan "
        "sıkışma sinyalleri, kör çizgiyle birlikte\n"
        "/sikisma_turnuvasi_v2 [volatil|buyuk] [HİSSE_SAYISI] — düzeltilmiş "
        "yön mantığı (release+AO) + 3 yeni aday (52-Hafta Kırılımı, Bant "
        "Yürüyüşü, Gap Kırılımı), volatil küçük-hisse evreni seçeneğiyle\n"
        "/sikisma_turnuvasi_v3 [volatil|buyuk] [HİSSE_SAYISI] — v2 ile aynı "
        "ama ATR-ÖLÇEKLİ hedeflerle (sabit %1-5 yerine hissenin kendi "
        "volatilitesine göre) - volatil hisselerde sabit hedefin "
        "anlamsızlaşma sorununu çözmek için\n"
        "/gun_ici_turnuva [volatil|buyuk] [HİSSE_SAYISI] — 16 göstergeyi "
        "GERÇEK aynı-gün giriş/çıkış mantığıyla test eder (kullanıcının "
        "asıl isteği - SUPX tarzı gün-içi patlama yakalama)\n"
        "/buyuk_patlama [volatil|buyuk] [HİSSE_SAYISI] — İç Mum/NR7/sıkışma "
        "ailesi + Saf Hacim Patlaması, ERKEN ÇIKIŞ YOK, gün sonuna kadar "
        "tutuluyor - gerçek %10+/%20+ patlama günü oranını ölçer\n"
        "/sikisma_turnuvasi_v4 [volatil|buyuk] [HİSSE_SAYISI] — Hacim "
        "Daralma Örüntüsü (çok-periyotlu, Minervini tarzı) + %B "
        "Stabilizasyonu, v3'ün aynı metodolojisiyle\n"
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
        df = yf.Ticker(ticker).history(period="6mo", interval="1d", timeout=20)
        if df is None or df.empty or len(df) < 60:
            return f"🧮 {ticker}: yeterli geçmiş veri yok (en az ~60 gün gerekli)."
        df = df.rename(columns={"Open": "open", "High": "high", "Low": "low",
                                 "Close": "close", "Volume": "volume"})
        df = df[["open", "high", "low", "close", "volume"]].copy()
        df.index = pd.to_datetime(df.index).tz_localize(None)

        index_pct = None
        try:
            idf = yf.Ticker("XU100.IS").history(period="6mo", interval="1d", timeout=20)
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
            df15 = yf.Ticker(r["ticker"]).history(period="60d", interval="15m", timeout=20)
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
        print(f"[ARGE TEŞHİS] Gelen mesaj: chat_id={chat_id} (beklenen={TELEGRAM_CHAT_ID}) "
              f"metin='{text}' eşleşiyor_mu={chat_id == str(TELEGRAM_CHAT_ID)}", flush=True)
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
            edgar_hisse_sayisi = int(parcalar[2]) if len(parcalar) > 2 else 12
            send_telegram_message(
                f"👤 EDGAR İÇERİDEN İŞLEM TESTİ başlıyor: SEC EDGAR'dan (yfinance "
                f"yerine) çekiliyor, {edgar_hisse_sayisi} hisse taranacak "
                f"(varsayılan artık KÜÇÜK - tekrarlayan tam-süreç donmaları "
                f"sonrası, Render'ın ücretsiz sürümünün uzun süren arka plan "
                f"görevlerini kısıtlıyor olabileceği düşünülüyor - küçük, "
                f"hızlı, güvenilir parçalar halinde ilerliyoruz. Tam listeyi "
                f"istersen /icgorusel_islem_edgar {gun_ufku} 106 yaz). "
                f"ARKA PLANDA çalışıyor, bitince CSV + özet göndereceğim. "
                f"İstatistik aynı gün/aynı türdeki tekrar eden işlem "
                f"satırları için TEKİLLEŞTİRİLMİŞ veriden hesaplanıyor."
            )

            def _arka_plan_icgorusel_edgar(gu, hs):
                try:
                    dosya_yolu, ozet = icgorusel_islem_testi_edgar_calistir(gu, max_hisse=hs)
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
            threading.Thread(target=_arka_plan_icgorusel_edgar, args=(gun_ufku, edgar_hisse_sayisi), daemon=True).start()
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
        elif text.startswith("/makro_gurultu_testi"):
            send_telegram_message(
                f"🌍 MAKRO GÜRÜLTÜ TESTİ başlıyor: bir önceki gece S&P500 ve "
                f"USDTRY hareketinin XU100/hisse getirisiyle ilişkisini "
                f"ölçüyor, 'sakin gece' eşiği önerisi üretiyor. ARKA "
                f"PLANDA çalışıyor, bitince CSV + özet göndereceğim."
            )

            def _arka_plan_makro_gurultu():
                try:
                    dosya_yolu, ozet = makro_gurultu_testi_calistir()
                    if dosya_yolu is None:
                        send_telegram_message(f"🌍 Makro gürültü testi başarısız: {ozet}")
                        return
                    satirlar = [
                        "🌍 Makro Gürültü Testi Sonucu",
                        f"Korelasyon S&P500(gece)<->XU100(bugün): {ozet['korelasyon_sp500']}",
                        f"Korelasyon USDTRY(gece)<->XU100(bugün): {ozet['korelasyon_usdtry']}",
                        f"Sakin gece std sapma: {ozet['sakin_std']} | "
                        f"Hareketli gece std sapma: {ozet['hareketli_std']}",
                        f"Önerilen 'sakin gece' eşiği: |S&P500| <= %{ozet['sp500_esik']}, "
                        f"|USDTRY| <= %{ozet['usdtry_esik']}",
                        f"Ortalama gün hisselerin aynı-yön oranı: %{ozet['ayni_yon_orani']} "
                        f"(v7 gözleminin sayısallaştırılmışı)\n",
                    ]
                    for s in ozet["yon_satirlari"]:
                        p_str = f", p={s['binom_p']}" if s["binom_p"] is not None else ""
                        satirlar.append(f"{s['hipotez']}: n={s['n']}, "
                                         f"%{s['kazanma_orani_pct']} isabet{p_str}")
                    send_telegram_document(dosya_yolu, caption="\n".join(satirlar))
                except Exception as e:
                    send_telegram_message(f"🌍 Makro gürültü testi hatası: {e}")
            threading.Thread(target=_arka_plan_makro_gurultu, daemon=True).start()
        elif text.startswith("/piyasa_sapmasi_testi"):
            send_telegram_message(
                f"🔀 PİYASA SAPMASI TESTİ başlıyor: her gün piyasa çoğunluğunun "
                f"yönünü bulup, çoğunluğa UYMAYAN (sapan/azınlık) hisselerin "
                f"ertesi gün DEVAM mı ettiğini yoksa GERİ Mİ DÖNDÜĞÜNÜ test "
                f"ediyor. ARKA PLANDA çalışıyor, bitince CSV + özet göndereceğim."
            )

            def _arka_plan_piyasa_sapmasi():
                try:
                    dosya_yolu, ozet = piyasa_sapmasi_testi_calistir()
                    if dosya_yolu is None:
                        send_telegram_message(f"🔀 Piyasa sapması testi başarısız: {ozet}")
                        return
                    satirlar = [f"🔀 Piyasa Sapması Testi Sonucu",
                                f"Toplam sapan (azınlık) gözlem: {ozet['toplam_sapan_gozlem']}\n"]
                    for s in ozet["satirlar"]:
                        p_str = f", p={s['binom_p']}" if s["binom_p"] is not None else ""
                        satirlar.append(
                            f"{s['hipotez']}: n={s['n']}, %{s['kazanma_orani_pct']} "
                            f"isabet{p_str}"
                        )
                    send_telegram_document(dosya_yolu, caption="\n".join(satirlar))
                except Exception as e:
                    send_telegram_message(f"🔀 Piyasa sapması testi hatası: {e}")
            threading.Thread(target=_arka_plan_piyasa_sapmasi, daemon=True).start()
        elif text.startswith("/piyasa_sapmasi_kanit_dogrulama"):
            send_telegram_message(
                f"🔀✅ PİYASA SAPMASI İZOLE DOĞRULAMA başlıyor: sapan (azınlık) "
                f"ve uyumlu (çoğunluk) hisseleri, BIST'in gerçek çıkış "
                f"mantığıyla (1.5R kısmi TP + trailing) karşılaştırmalı test "
                f"ediyor. ARKA PLANDA çalışıyor, bitince CSV + özet göndereceğim."
            )

            def _arka_plan_sapma_kanit():
                try:
                    dosya_yolu, ozet = piyasa_sapmasi_kanit_dogrulama_calistir()
                    if dosya_yolu is None:
                        send_telegram_message(f"🔀✅ Piyasa sapması kanıt doğrulama başarısız: {ozet}")
                        return
                    satirlar = ["🔀✅ Piyasa Sapması İzole Doğrulama Sonucu\n"]
                    for s in ozet["satirlar"]:
                        p_str = f", p={s['binom_p']}" if s["binom_p"] is not None else ""
                        satirlar.append(
                            f"{s['strateji']}: {s['toplam_sinyal']} sinyal "
                            f"({s['win']}W/{s['loss']}L/{s['timeout']}T), "
                            f"%{s['kazanma_orani_pct']} isabet{p_str}, ort R={s['ort_R']}"
                        )
                    send_telegram_document(dosya_yolu, caption="\n".join(satirlar))
                except Exception as e:
                    send_telegram_message(f"🔀✅ Piyasa sapması kanıt doğrulama hatası: {e}")
            threading.Thread(target=_arka_plan_sapma_kanit, daemon=True).start()
        elif text.startswith("/xu100_makro_zamanlama"):
            send_telegram_message(
                f"📊 XU100 MAKRO ZAMANLAMA TESTİ başlıyor: S&P500/USDTRY'nin "
                f"geceki hareketini XU100'ün kendisine karşı 5 ayrı ufukta "
                f"(1g/3g/1hf/2hf/1ay) test ediyor. ARKA PLANDA çalışıyor, "
                f"bitince CSV + özet göndereceğim."
            )

            def _arka_plan_xu100_makro():
                try:
                    dosya_yolu, ozet = xu100_makro_zamanlama_testi_calistir()
                    if dosya_yolu is None:
                        send_telegram_message(f"📊 XU100 makro zamanlama testi başarısız: {ozet}")
                        return
                    satirlar = ["📊 XU100 Makro Zamanlama Testi Sonucu\n"]
                    for s in ozet["satirlar"]:
                        p_str = f", p={s['binom_p']}" if s["binom_p"] is not None else ""
                        satirlar.append(f"{s['hipotez']}: n={s['n']}, "
                                         f"%{s['kazanma_orani_pct']} isabet{p_str}")
                    send_telegram_document(dosya_yolu, caption="\n".join(satirlar))
                except Exception as e:
                    send_telegram_message(f"📊 XU100 makro zamanlama testi hatası: {e}")
            threading.Thread(target=_arka_plan_xu100_makro, daemon=True).start()
        elif text.startswith("/deger_testi"):
            send_telegram_message(
                f"💰 DEĞER (F/K) TESTİ başlıyor: her hisse için çeyreklik EPS "
                f"geçmişinden F/K oranı hesaplanıp, ucuz vs pahalı grupları "
                f"1/3/6 ay ufuklarında karşılaştırılacak. yfinance'in BIST "
                f"finansal veri kapsamı belirsiz - bazı hisseler atlanabilir, "
                f"bu normal. ARKA PLANDA çalışıyor, bitince CSV + özet "
                f"göndereceğim."
            )

            def _arka_plan_deger_testi():
                try:
                    dosya_yolu, ozet = deger_testi_calistir()
                    if dosya_yolu is None:
                        send_telegram_message(f"💰 Değer testi başarısız: {ozet}")
                        return
                    satirlar = [f"💰 Değer (F/K) Testi Sonucu",
                                f"{ozet['hisse_sayisi']} hisse kullanıldı "
                                f"({ozet['atlanan_hisse']} atlandı - veri yok/yetersiz), "
                                f"{ozet['toplam_gozlem']} gözlem\n"]
                    for s in ozet["satirlar"]:
                        p_str = f", p={s['binom_p']}" if s["binom_p"] is not None else ""
                        satirlar.append(
                            f"{s['hipotez']}: n={s['n']}, %{s['kazanma_orani_pct']} "
                            f"pozitif{p_str}, ort. getiri %{s['ort_getiri_pct']}"
                        )
                    send_telegram_document(dosya_yolu, caption="\n".join(satirlar))
                except Exception as e:
                    send_telegram_message(f"💰 Değer testi hatası: {e}")
            threading.Thread(target=_arka_plan_deger_testi, daemon=True).start()
        elif text.startswith("/ai_ozellik_onemi"):
            send_telegram_message("📊 Özellik önem raporu hazırlanıyor...")

            def _arka_plan_ozellik_onem():
                try:
                    sonuc = ai_model_ozellik_onem_raporu()
                    send_telegram_message(f"📊 AI Model Özellik Önem Raporu:\n{sonuc}")
                except Exception as e:
                    send_telegram_message(f"📊 Özellik önem raporu hatası: {e}")
            threading.Thread(target=_arka_plan_ozellik_onem, daemon=True).start()
        elif text.startswith("/gun_ici_tarama_testi"):
            send_telegram_message(
                f"⏱️ GÜN İÇİ SÜREKLİ TARAMA TESTİ başlıyor: ATR Kırılımı ve "
                f"Hacim Z-Skor'u 15dk barlarla gün içinde (sadece ilk "
                f"tetiklenme) test edip, aynı dönemde SADECE kapanış-bazlı "
                f"(canlı sistemin gerçek davranışı) kontrol grubuyla "
                f"karşılaştırıyor. Bu TAMAMEN İZOLE bir deney, canlı "
                f"sisteme dokunmuyor. ARKA PLANDA çalışıyor, bitince CSV + "
                f"özet göndereceğim."
            )

            def _arka_plan_gun_ici_tarama():
                try:
                    dosya_yolu, ozet = gun_ici_surekli_tarama_testi_calistir()
                    if dosya_yolu is None:
                        send_telegram_message(f"⏱️ Gün içi tarama testi başarısız: {ozet}")
                        return
                    satirlar = ["⏱️ Gün İçi Sürekli Tarama Testi Sonucu\n"]
                    for s in ozet["satirlar"]:
                        p_str = f", p={s['binom_p']}" if s["binom_p"] is not None else ""
                        satirlar.append(
                            f"{s['strateji']}: {s['toplam_sinyal']} sinyal "
                            f"({s['win']}W/{s['loss']}L/{s['timeout']}T), "
                            f"%{s['kazanma_orani_pct']} isabet{p_str}, ort R={s['ort_R']}"
                        )
                    send_telegram_document(dosya_yolu, caption="\n".join(satirlar))
                except Exception as e:
                    send_telegram_message(f"⏱️ Gün içi tarama testi hatası: {e}")
            threading.Thread(target=_arka_plan_gun_ici_tarama, daemon=True).start()
        elif text.startswith("/rsi21_hedef_kiyasi"):
            send_telegram_message(
                f"🎯 RSI21 HEDEF KIYASI başlıyor: aynı giriş sinyalini canlı "
                f"sistemin küçük hedefleriyle (%0.15-0.90) VE gerçekten "
                f"anlamlı büyük hedeflerle (%1-5) karşılaştırıyor. ARKA "
                f"PLANDA çalışıyor, bitince CSV + özet göndereceğim."
            )

            def _arka_plan_rsi21_kiyas():
                try:
                    dosya_yolu, ozet = rsi21_hedef_kiyasi_testi_calistir()
                    if dosya_yolu is None:
                        send_telegram_message(f"🎯 RSI21 hedef kıyası başarısız: {ozet}")
                        return
                    satirlar = ["🎯 RSI21 Hedef Kıyası Sonucu\n"]
                    for s in ozet["satirlar"]:
                        p_str = f", p={s['binom_p']}" if s["binom_p"] is not None else ""
                        satirlar.append(
                            f"{s['strateji']}: {s['toplam_sinyal']} sinyal "
                            f"({s['win']}W/{s['loss']}L/{s['timeout']}T), "
                            f"%{s['kazanma_orani_pct']} isabet{p_str}, ort R={s['ort_R']}"
                        )
                    send_telegram_document(dosya_yolu, caption="\n".join(satirlar))
                except Exception as e:
                    send_telegram_message(f"🎯 RSI21 hedef kıyası hatası: {e}")
            threading.Thread(target=_arka_plan_rsi21_kiyas, daemon=True).start()
        elif text.startswith("/rsi21_bist_testi"):
            send_telegram_message(
                f"🎯 RSI21 BIST TESTİ başlıyor: aynı gün-içi RSI21 sinyalini "
                f"(≤25/≥75) BIST hisselerinde, BIST'in gerçek çıkış "
                f"mantığıyla (1.5R kısmi TP + trailing) test ediyor. ARKA "
                f"PLANDA çalışıyor, bitince CSV + özet göndereceğim."
            )

            def _arka_plan_rsi21_bist():
                try:
                    dosya_yolu, ozet = rsi21_bist_testi_calistir()
                    if dosya_yolu is None:
                        send_telegram_message(f"🎯 RSI21 BIST testi başarısız: {ozet}")
                        return
                    satirlar = ["🎯 RSI21 BIST Testi Sonucu\n"]
                    for s in ozet["satirlar"]:
                        p_str = f", p={s['binom_p']}" if s["binom_p"] is not None else ""
                        satirlar.append(
                            f"{s['strateji']}: {s['toplam_sinyal']} sinyal "
                            f"({s['win']}W/{s['loss']}L/{s['timeout']}T), "
                            f"%{s['kazanma_orani_pct']} isabet{p_str}, ort R={s['ort_R']}"
                        )
                    send_telegram_document(dosya_yolu, caption="\n".join(satirlar))
                except Exception as e:
                    send_telegram_message(f"🎯 RSI21 BIST testi hatası: {e}")
            threading.Thread(target=_arka_plan_rsi21_bist, daemon=True).start()
        elif text.startswith("/yeni_gosterge_turnuvasi_us"):
            send_telegram_message(
                f"🆕 YENİ ABD GÖSTERGE TURNUVASI başlıyor: Stochastic %K, "
                f"CCI, MFI, Bollinger Bandı dokunuşunu gün içi (15dk) aşırı "
                f"uç sinyalleriyle, gerçek büyük hedeflerle (%1-5, RSI21/ATR/"
                f"Hacim Z-Skor ile aynı checkpoint sistemi) test ediyor. Artık "
                f"KÖR TEMEL ÇİZGİ (koşulsuz her gün LONG/SHORT) de eklendi - "
                f"bu daha UZUN sürecek (~15-20+ dakika). ARKA PLANDA "
                f"çalışıyor, bitince CSV + özet göndereceğim."
            )

            def _arka_plan_yeni_gosterge_us():
                try:
                    dosya_yolu, ozet = yeni_gosterge_turnuvasi_us_calistir()
                    if dosya_yolu is None:
                        send_telegram_message(f"🆕 Yeni gösterge turnuvası başarısız: {ozet}")
                        return
                    satirlar = ["🆕 Yeni ABD Gösterge Turnuvası Sonucu\n"]
                    for s in ozet["satirlar"]:
                        p_str = f", p={s['binom_p']}" if s["binom_p"] is not None else ""
                        satirlar.append(
                            f"{s['strateji']}: {s['toplam_sinyal']} sinyal "
                            f"({s['win']}W/{s['loss']}L/{s['timeout']}T), "
                            f"%{s['kazanma_orani_pct']} isabet{p_str}, ort R={s['ort_R']}"
                        )
                    send_telegram_document(dosya_yolu, caption="\n".join(satirlar))
                except Exception as e:
                    send_telegram_message(f"🆕 Yeni gösterge turnuvası hatası: {e}")
            threading.Thread(target=_arka_plan_yeni_gosterge_us, daemon=True).start()
        elif text.startswith("/nihai_kor_kiyasi"):
            send_telegram_message(
                f"⚖️ NİHAİ KÖR KIYAS başlıyor: ATR Kırılımı, Hacim Z-Skor, "
                f"RSI21, Stochastic, CCI, MFI, Bollinger + kör temel çizgiyi "
                f"(koşulsuz LONG/SHORT) TEK taramada, aynı checkpoint "
                f"sistemine karşı test ediyor - hepsinin kör çizgiden GERÇEK "
                f"farkını (edge) gösterecek. Bu EN UZUN süren test (hisse "
                f"başına hem günlük hem 15dk veri + 7 gösterge + kör çizgi), "
                f"muhtemelen 20-30+ dakika. ARKA PLANDA çalışıyor, bitince "
                f"CSV + özet göndereceğim."
            )

            def _arka_plan_nihai_kor():
                try:
                    dosya_yolu, ozet = tum_abd_gostergeleri_kor_kiyasi_calistir()
                    if dosya_yolu is None:
                        send_telegram_message(f"⚖️ Nihai kör kıyas başarısız: {ozet}")
                        return
                    satirlar = ["⚖️ Nihai Kör Kıyas Sonucu (kör çizgiden farka göre sıralı)\n"]
                    for s in ozet["satirlar"]:
                        fark_str = f", kör'den fark: {s['kor_cizgiden_fark_puan']:+.2f}puan" \
                            if s.get("kor_cizgiden_fark_puan") is not None else ""
                        satirlar.append(
                            f"{s['strateji']}: n={s['toplam_sinyal']}, "
                            f"%{s['kazanma_orani_pct']} isabet{fark_str}, ort R={s['ort_R']}"
                        )
                    send_telegram_document(dosya_yolu, caption="\n".join(satirlar))
                except Exception as e:
                    send_telegram_message(f"⚖️ Nihai kör kıyas hatası: {e}")
            threading.Thread(target=_arka_plan_nihai_kor, daemon=True).start()
        elif text.startswith("/gosterge_cephaneligi"):
            parcalar = text.split()
            hisse_sayisi = int(parcalar[1]) if len(parcalar) > 1 else 12
            send_telegram_message(
                f"🏹 GÖSTERGE CEPHANELİĞİ başlıyor ({hisse_sayisi} hisse - "
                f"Donchian'ı büyük örneklemde doğrulamak için varsayılan "
                f"60'a çıkarıldı): (1) 4 güçlü sinyalin örtüşmesini, (2) 21 "
                f"trend/momentum adayını (EMA/MACD kesişimi, Donchian x2, "
                f"ADX+DI, ROC, RSI50, PSAR, hacim-onaylı kırılım, Keltner, "
                f"MFI, VWAP sapması, Üçlü EMA, Awesome Oscillator, Ichimoku, "
                f"Chaikin, Stochastic RSI, Vortex - Williams %R çıkarıldı, "
                f"Stochastic'le birebir aynıydı) kör temel çizgiyle birlikte "
                f"TEK taramada test ediyor. Bu ÇOK UZUN sürecek (21 strateji "
                f"+ kör çizgi, {hisse_sayisi} hisseyle muhtemelen 45-60+ "
                f"dakika). ARKA PLANDA çalışıyor, bitince CSV + özet "
                f"göndereceğim."
            )

            def _arka_plan_cephanelik(hs):
                try:
                    dosya_yolu, ozet = gosterge_cephaneligi_calistir(hs)
                    if dosya_yolu is None:
                        send_telegram_message(f"🏹 Gösterge cephaneliği başarısız: {ozet}")
                        return
                    satirlar = ["🏹 Gösterge Cephaneliği Sonucu (R farkına göre sıralı)\n"]
                    for s in ozet["satirlar"]:
                        fark_str = f", kör R farkı: {s['kor_R_farki']:+.4f}" \
                            if s.get("kor_R_farki") is not None else ""
                        satirlar.append(
                            f"{s['strateji']}: n={s['toplam_sinyal']}, "
                            f"%{s['kazanma_orani_pct']} isabet, ort R={s['ort_R']}{fark_str}"
                        )
                    send_telegram_document(dosya_yolu, caption="\n".join(satirlar))
                except Exception as e:
                    send_telegram_message(f"🏹 Gösterge cephaneliği hatası: {e}")
            threading.Thread(target=_arka_plan_cephanelik, args=(hisse_sayisi,), daemon=True).start()
        elif text.startswith("/sikisma_turnuvasi_v4"):
            parcalar = text.split()
            liste4 = parcalar[1] if len(parcalar) > 1 and parcalar[1] in ("volatil", "buyuk") else "volatil"
            hisse_sayisi4 = int(parcalar[2]) if len(parcalar) > 2 else 12
            send_telegram_message(
                f"🌀 SIKIŞMA TURNUVASI v4 başlıyor (liste: {liste4}, {hisse_sayisi4} "
                f"hisse): Hacim Daralma Örüntüsü (çok-periyotlu, Minervini "
                f"tarzı) + %B Stabilizasyonu - v3'ün AYNI kanıtlanmış "
                f"metodolojisiyle (ATR-ölçekli hedef, release+AO yön). ARKA "
                f"PLANDA çalışıyor, bitince CSV + özet göndereceğim."
            )

            def _arka_plan_sikisma_v4(l, hs):
                try:
                    dosya_yolu, ozet = sikisma_turnuvasi_v4_calistir(l, hs)
                    if dosya_yolu is None:
                        send_telegram_message(f"🌀 Sıkışma turnuvası v4 başarısız: {ozet}")
                        return
                    satirlar = [f"🌀 Sıkışma Turnuvası v4 Sonucu (liste: {ozet['hisse_listesi']}, "
                                f"{ozet['denenen_hisse_sayisi']} hisse)\n"]
                    for s in ozet["satirlar"]:
                        fark_str = f", kör R farkı: {s['kor_R_farki']:+.4f}" \
                            if s.get("kor_R_farki") is not None else ""
                        satirlar.append(
                            f"{s['strateji']}: n={s['toplam_sinyal']}, "
                            f"%{s['kazanma_orani_pct']} isabet, ort R={s['ort_R']}{fark_str}"
                        )
                    send_telegram_document(dosya_yolu, caption="\n".join(satirlar))
                except Exception as e:
                    send_telegram_message(f"🌀 Sıkışma turnuvası v4 hatası: {e}")
            threading.Thread(target=_arka_plan_sikisma_v4, args=(liste4, hisse_sayisi4), daemon=True).start()
        elif text.startswith("/buyuk_patlama"):
            parcalar = text.split()
            liste_bp = parcalar[1] if len(parcalar) > 1 and parcalar[1] in ("volatil", "buyuk") else "volatil"
            hisse_sayisi_bp = int(parcalar[2]) if len(parcalar) > 2 else 60
            send_telegram_message(
                f"💥 BÜYÜK PATLAMA GÜNÜ TESTİ başlıyor (liste: {liste_bp}, "
                f"{hisse_sayisi_bp} hisse): İç Mum, NR7, tüm sıkışma ailesi "
                f"+ Saf Hacim Patlaması - ERKEN ÇIKIŞ YOK, gün sonuna kadar "
                f"tutuluyor, GERÇEK yüzde getiri dağılımı (%10+ ve %20+ "
                f"oranı dahil) ölçülüyor. ARKA PLANDA çalışıyor, bitince "
                f"CSV + özet göndereceğim."
            )

            def _arka_plan_buyuk_patlama(l, hs):
                try:
                    dosya_yolu, ozet = buyuk_patlama_gunu_testi_calistir(l, hs)
                    if dosya_yolu is None:
                        send_telegram_message(f"💥 Büyük patlama testi başarısız: {ozet}")
                        return
                    satirlar = [f"💥 Büyük Patlama Günü Testi Sonucu (liste: "
                                f"{ozet['hisse_listesi']}, {ozet['denenen_hisse_sayisi']} hisse)\n"]
                    for s in ozet["satirlar"]:
                        satirlar.append(
                            f"{s['strateji']}: n={s['n']}, ort=%{s['ort_getiri_pct']}, "
                            f"medyan=%{s['medyan_getiri_pct']}, kazanma=%{s['kazanma_orani_pct']}, "
                            f"%10+ oranı=%{s['yuzde10_ustu_oran_pct']}, %20+ oranı=%{s['yuzde20_ustu_oran_pct']}, "
                            f"en iyi=%{s['en_iyi_pct']}, en kötü=%{s['en_kotu_pct']}"
                        )
                    send_telegram_document(dosya_yolu, caption="\n".join(satirlar))
                except Exception as e:
                    send_telegram_message(f"💥 Büyük patlama testi hatası: {e}")
            threading.Thread(target=_arka_plan_buyuk_patlama, args=(liste_bp, hisse_sayisi_bp), daemon=True).start()
        elif text.startswith("/gun_ici_turnuva"):
            parcalar = text.split()
            liste_gi = parcalar[1] if len(parcalar) > 1 and parcalar[1] in ("volatil", "buyuk") else "volatil"
            hisse_sayisi_gi = int(parcalar[2]) if len(parcalar) > 2 else 12
            send_telegram_message(
                f"📅 GÜN-İÇİ GİRİŞ+ÇIKIŞ TURNUVASI başlıyor (liste: {liste_gi}, "
                f"{hisse_sayisi_gi} hisse): 16 gösterge (tersine dönüş + trend "
                f"+ sıkışma) GERÇEK aynı-gün giriş/çıkış mantığıyla test "
                f"ediliyor - hiçbir sinyal ertesi güne taşınmıyor, gün "
                f"biterse gerçek kapanış fiyatıyla kapanıyor. ARKA PLANDA "
                f"çalışıyor, biraz sürebilir, bitince CSV + özet göndereceğim."
            )

            def _arka_plan_gun_ici_turnuva(l, hs):
                try:
                    dosya_yolu, ozet = gun_ici_giris_cikis_turnuvasi_calistir(l, hs)
                    if dosya_yolu is None:
                        send_telegram_message(f"📅 Gün-içi turnuva başarısız: {ozet}")
                        return
                    satirlar = [f"📅 Gün-İçi Giriş+Çıkış Turnuvası Sonucu (liste: "
                                f"{ozet['hisse_listesi']}, {ozet['denenen_hisse_sayisi']} hisse)\n"]
                    for s in ozet["satirlar"]:
                        fark_str = f", kör R farkı: {s['kor_R_farki']:+.4f}" \
                            if s.get("kor_R_farki") is not None else ""
                        satirlar.append(
                            f"{s['strateji']}: n={s['toplam_sinyal']}, "
                            f"%{s['kazanma_orani_pct']} isabet, ort R={s['ort_R']}{fark_str}"
                        )
                    send_telegram_document(dosya_yolu, caption="\n".join(satirlar))
                except Exception as e:
                    send_telegram_message(f"📅 Gün-içi turnuva hatası: {e}")
            threading.Thread(target=_arka_plan_gun_ici_turnuva, args=(liste_gi, hisse_sayisi_gi), daemon=True).start()
        elif text.startswith("/sikisma_turnuvasi_v3"):
            parcalar = text.split()
            liste3 = parcalar[1] if len(parcalar) > 1 and parcalar[1] in ("volatil", "buyuk") else "volatil"
            hisse_sayisi3 = int(parcalar[2]) if len(parcalar) > 2 else 12
            send_telegram_message(
                f"🌀 SIKIŞMA TURNUVASI v3 başlıyor (liste: {liste3}, {hisse_sayisi3} "
                f"hisse): ATR-ÖLÇEKLİ hedefler - her hissenin kendi volatilitesine "
                f"göre büyüyen/küçülen checkpoint hedefleri, sabit %1-5'in "
                f"volatil hisselerde anlamsızlaşma sorununu çözmek için. ARKA "
                f"PLANDA çalışıyor, bitince CSV + özet göndereceğim."
            )

            def _arka_plan_sikisma_v3(l, hs):
                try:
                    dosya_yolu, ozet = sikisma_turnuvasi_v3_calistir(l, hs)
                    if dosya_yolu is None:
                        send_telegram_message(f"🌀 Sıkışma turnuvası v3 başarısız: {ozet}")
                        return
                    satirlar = [f"🌀 Sıkışma Turnuvası v3 (ATR-ölçekli) Sonucu (liste: "
                                f"{ozet['hisse_listesi']}, {ozet['denenen_hisse_sayisi']} hisse)\n"]
                    for s in ozet["satirlar"]:
                        fark_str = f", kör R farkı: {s['kor_R_farki']:+.4f}" \
                            if s.get("kor_R_farki") is not None else ""
                        satirlar.append(
                            f"{s['strateji']}: n={s['toplam_sinyal']}, "
                            f"%{s['kazanma_orani_pct']} isabet, ort R={s['ort_R']}{fark_str}"
                        )
                    send_telegram_document(dosya_yolu, caption="\n".join(satirlar))
                except Exception as e:
                    send_telegram_message(f"🌀 Sıkışma turnuvası v3 hatası: {e}")
            threading.Thread(target=_arka_plan_sikisma_v3, args=(liste3, hisse_sayisi3), daemon=True).start()
        elif text.startswith("/sikisma_turnuvasi_v2"):
            parcalar = text.split()
            liste = parcalar[1] if len(parcalar) > 1 and parcalar[1] in ("volatil", "buyuk") else "volatil"
            hisse_sayisi2 = int(parcalar[2]) if len(parcalar) > 2 else 12
            send_telegram_message(
                f"🌀 SIKIŞMA TURNUVASI v2 başlıyor (liste: {liste}, {hisse_sayisi2} "
                f"hisse): düzeltilmiş yön mantığı (sıkışma bitişinde Awesome "
                f"Oscillator yönü) + 3 yeni aday (52-Hafta Kırılımı, Bollinger "
                f"Bandı Yürüyüşü, Gap Kırılımı). ARKA PLANDA çalışıyor, "
                f"bitince CSV + özet göndereceğim."
            )

            def _arka_plan_sikisma_v2(l, hs):
                try:
                    dosya_yolu, ozet = sikisma_turnuvasi_v2_calistir(l, hs)
                    if dosya_yolu is None:
                        send_telegram_message(f"🌀 Sıkışma turnuvası v2 başarısız: {ozet}")
                        return
                    satirlar = [f"🌀 Sıkışma Turnuvası v2 Sonucu (liste: {ozet['hisse_listesi']}, "
                                f"{ozet['denenen_hisse_sayisi']} hisse denendi)\n"]
                    for s in ozet["satirlar"]:
                        fark_str = f", kör R farkı: {s['kor_R_farki']:+.4f}" \
                            if s.get("kor_R_farki") is not None else ""
                        satirlar.append(
                            f"{s['strateji']}: n={s['toplam_sinyal']}, "
                            f"%{s['kazanma_orani_pct']} isabet, ort R={s['ort_R']}{fark_str}"
                        )
                    send_telegram_document(dosya_yolu, caption="\n".join(satirlar))
                except Exception as e:
                    send_telegram_message(f"🌀 Sıkışma turnuvası v2 hatası: {e}")
            threading.Thread(target=_arka_plan_sikisma_v2, args=(liste, hisse_sayisi2), daemon=True).start()
        elif text.startswith("/sikisma_turnuvasi"):
            parcalar = text.split()
            hisse_sayisi = int(parcalar[1]) if len(parcalar) > 1 else 12
            send_telegram_message(
                f"🌀 HAREKET-ÖNCESİ SIKIŞMA TURNUVASI başlıyor ({hisse_sayisi} "
                f"hisse): NR7, İç Mum, Bollinger Genişlik Sıkışması, TTM "
                f"Squeeze, ATR Persentil Sıkışması - hepsi 'hareket "
                f"başlamadan önce yakala' mantığında, kör temel çizgiyle "
                f"birlikte test ediliyor. ARKA PLANDA çalışıyor, biraz "
                f"sürebilir, bitince CSV + özet göndereceğim."
            )

            def _arka_plan_sikisma(hs):
                try:
                    dosya_yolu, ozet = hareket_oncesi_sikisma_turnuvasi_calistir(hs)
                    if dosya_yolu is None:
                        send_telegram_message(f"🌀 Sıkışma turnuvası başarısız: {ozet}")
                        return
                    satirlar = ["🌀 Hareket-Öncesi Sıkışma Turnuvası Sonucu (R farkına göre sıralı)\n"]
                    for s in ozet["satirlar"]:
                        fark_str = f", kör R farkı: {s['kor_R_farki']:+.4f}" \
                            if s.get("kor_R_farki") is not None else ""
                        satirlar.append(
                            f"{s['strateji']}: n={s['toplam_sinyal']}, "
                            f"%{s['kazanma_orani_pct']} isabet, ort R={s['ort_R']}{fark_str}"
                        )
                    send_telegram_document(dosya_yolu, caption="\n".join(satirlar))
                except Exception as e:
                    send_telegram_message(f"🌀 Sıkışma turnuvası hatası: {e}")
            threading.Thread(target=_arka_plan_sikisma, args=(hisse_sayisi,), daemon=True).start()
        elif text.startswith("/ai_model_backtest"):
            send_telegram_message(
                f"🤖 GERÇEK AI MODEL BACKTEST başlıyor: model.pkl ve "
                f"overnight_model.pkl'yi gerçekten yükleyip, son 60 günün "
                f"15dk verisiyle GERÇEK tahminlerini GERÇEK sonuçlara karşı "
                f"test ediyor. has_catalyst=0 varsayılıyor (geçmişe dönük "
                f"KAP verisi yok). ARKA PLANDA çalışıyor, biraz sürebilir, "
                f"bitince CSV + özet göndereceğim."
            )

            def _arka_plan_ai_backtest():
                try:
                    dosya_yolu, ozet = ai_model_gercek_backtest_calistir()
                    if dosya_yolu is None:
                        send_telegram_message(f"🤖 AI model backtest başarısız: {ozet}")
                        return
                    satirlar = ["🤖 Gerçek AI Model Backtest Sonucu\n"]
                    for s in ozet["satirlar"]:
                        p_str = f", p={s['binom_p']}" if s["binom_p"] is not None else ""
                        satirlar.append(
                            f"[{s['model']}] {s['esik']}: n={s['n']}, "
                            f"%{s['basari_orani_pct']} başarı (+%2 hedefi){p_str}"
                        )
                    send_telegram_document(dosya_yolu, caption="\n".join(satirlar))
                except Exception as e:
                    send_telegram_message(f"🤖 AI model backtest hatası: {e}")
            threading.Thread(target=_arka_plan_ai_backtest, daemon=True).start()
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
        elif text.startswith("/gun_ici_giris_cikis"):
            parcalar = text.split()
            hisse_sayisi = int(parcalar[1]) if len(parcalar) > 1 else 12
            send_telegram_message(
                f"📅 GÜN-İÇİ GİRİŞ+ÇIKIŞ TESTİ başlıyor ({hisse_sayisi} hisse): "
                f"canlıda çalışan AYNI 8 gösterge, ama çıkış artık AYNI GÜN "
                f"içinde - hedef gün bitmeden tutarsa kazanç, tutmazsa "
                f"kapanışta zorla kapatılıp gerçek kâr/zarar kaydediliyor. "
                f"ARKA PLANDA çalışıyor, bitince CSV + özet göndereceğim."
            )

            def _arka_plan_gun_ici_giris_cikis(hs):
                try:
                    dosya_yolu, ozet = gun_ici_giris_cikis_testi_calistir(hs)
                    if dosya_yolu is None:
                        send_telegram_message(f"📅 Gün-içi testi başarısız: {ozet}")
                        return
                    satirlar = [f"📅 Gün-İçi Giriş+Çıkış Testi Sonucu ({ozet['hisse_sayisi']} hisse)\n"]
                    for s in ozet["satirlar"]:
                        fark_str = f", kör R farkı: {s['kor_R_farki']:+.4f}" \
                            if s.get("kor_R_farki") is not None else ""
                        satirlar.append(
                            f"{s['strateji']}: n={s['toplam_sinyal']}, "
                            f"%{s['kazanma_orani_pct']} isabet, ort R={s['ort_R']}{fark_str}"
                        )
                    send_telegram_document(dosya_yolu, caption="\n".join(satirlar))
                except Exception as e:
                    send_telegram_message(f"📅 Gün-içi testi hatası: {e}")
            threading.Thread(target=_arka_plan_gun_ici_giris_cikis, args=(hisse_sayisi,), daemon=True).start()
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


# =============================================================================
# GÜN-İÇİ GİRİŞ + GÜN-İÇİ ÇIKIŞ TESTİ — 2026-08-19
# =============================================================================
# GEREKÇE: Kullanıcı baştan beri (SUPX görseliyle) AYNI GÜN içinde
# alıp-satabileceği bir sistem istiyordu - bugüne kadarki TÜM testler
# (canlıdaki 8'li cephanelik dahil) 1-10 GÜNLÜK checkpoint kullanıyordu,
# bu YANLIŞ soruydu. Bu test, CANLIDA ZATEN ÇALIŞAN AYNI 8 göstergeyi
# kullanıyor (Donchian-20, EMA9/21, ADX+DI, Awesome Oscillator,
# Bollinger, CCI, VWAP Sapması, MACD) - ama çıkış GÜN İÇİNDE: hedef
# gün bitmeden tutarsa kazanç, tutmazsa kapanışta ZORLA kapatılıp o
# andaki GERÇEK kâr/zarar kaydediliyor (ikili KAZANDI/KAYBETTİ değil,
# gerçekçi bir P&L).

GUN_ICI_CIKIS_CHECKPOINTS = [(4, "1sa", 0.3), (8, "2sa", 0.6), (16, "4sa", 1.0)]  # bar sayisi (15dk), ATR-intraday kati


def _intraday_atr_hesapla(high, low, close, n=14):
    tr = pd.concat([(high - low), (high - close.shift()).abs(), (low - close.shift()).abs()], axis=1).max(axis=1)
    return tr.rolling(n).mean()


def _gun_ici_ayni_gun_cikis_sonuc(barlar: pd.DataFrame, giris_idx: int, yon: str, atr_col: str = "atr_gun_ici"):
    """Girişten sonra AYNI GÜN içindeki barlarla checkpoint kontrolü.
    Hiçbiri tutmazsa günün SON barında ZORLA kapatılır, o andaki GERÇEK
    R (ATR-intraday'a göre ölçekli) döner - ikili WIN/LOSS değil,
    gerçekçi bir kapanış sonucu."""
    giris_bar = barlar.iloc[giris_idx]
    giris_fiyat = giris_bar["close"]
    atr = giris_bar[atr_col]
    if pd.isna(atr) or atr == 0:
        return None
    bugun = giris_bar["gun"]

    # ayni gunun kalan barlarini bul
    gun_sonu_idx = giris_idx
    for j in range(giris_idx + 1, len(barlar)):
        if barlar.iloc[j]["gun"] != bugun:
            break
        gun_sonu_idx = j
    if gun_sonu_idx == giris_idx:
        return None  # gunun son barinda tetiklenmis, kontrol edecek bar yok

    for bar_ofset, etiket, atr_kat in GUN_ICI_CIKIS_CHECKPOINTS:
        i = giris_idx + bar_ofset
        if i > gun_sonu_idx:
            break  # bu checkpoint gun bitmeden yetismiyor
        bar = barlar.iloc[i]
        if yon == "LONG":
            hedef = giris_fiyat + atr_kat * atr
            if bar["high"] >= hedef:
                return "WIN", atr_kat
        else:
            hedef = giris_fiyat - atr_kat * atr
            if bar["low"] <= hedef:
                return "WIN", atr_kat

    # hicbir checkpoint tutmadi -> GUN SONUNDA ZORLA KAPAT, gercek R hesapla.
    # 2026-08-19 DUZELTME: _kanit_ozet_tablosu SADECE "WIN"/"LOSS"/"TIMEOUT"
    # etiketlerini taniyor - ozel "EOD_KAPANIS" etiketi kullanilsaydi hic
    # "LOSS" sayilmayacagi icin isabet orani YANLIS (yapay %100'e yakin)
    # cikardi. Artik gercek R'nin ISARETINE gore WIN/LOSS etiketleniyor,
    # ama gercek R degeri (kucuk de olsa, negatif de olsa) korunuyor -
    # hem isabet orani hem ortalama R dogru hesaplaniyor.
    kapanis_fiyat = barlar.iloc[gun_sonu_idx]["close"]
    if yon == "LONG":
        gercek_r = (kapanis_fiyat - giris_fiyat) / atr
    else:
        gercek_r = (giris_fiyat - kapanis_fiyat) / atr
    etiket = "WIN" if gercek_r > 0 else "LOSS"
    return etiket, round(gercek_r, 4)


def gun_ici_giris_cikis_testi_calistir(max_hisse: int = 12) -> tuple:
    """Canlıda çalışan AYNI 8 göstergeyi, AYNI GÜN çıkışıyla (checkpoint
    tutarsa kazanç, tutmazsa kapanışta zorla + gerçek R) test eder. Kör
    temel çizgi de (koşulsuz LONG/SHORT, günün ilk barında giriş, aynı
    gün çıkış) aynı yöntemle hesaplanıyor. Döner: (dosya_yolu, özet_dict)
    ya da (None, hata_mesajı)."""
    import yfinance as yf

    hisseler = US_INSIDER_TICKERS[:max_hisse]
    isimler = ["Donchian-20 Kırılımı", "EMA9/21 Kesişimi", "ADX+DI Yön",
               "Awesome Oscillator", "Bollinger Bandı Dokunuşu", "CCI",
               "VWAP Sapması", "MACD Kesişimi"]
    tum_sonuclar = {isim: [] for isim in isimler}
    tum_sonuclar["[KÖR] Koşulsuz LONG (gün içi)"] = []
    tum_sonuclar["[KÖR] Koşulsuz SHORT (gün içi)"] = []

    for n_i, ticker in enumerate(hisseler, 1):
        try:
            print(f"[Gün İçi Testi {n_i}/{len(hisseler)}] {ticker}...", flush=True)
            barlar = yf.Ticker(ticker).history(period="60d", interval="15m", timeout=20)
            if barlar is None or barlar.empty or len(barlar) < 60:
                continue
            barlar = barlar.reset_index().rename(columns={
                "Datetime": "ts", "Open": "open", "High": "high", "Low": "low",
                "Close": "close", "Volume": "volume"})
            if "ts" not in barlar.columns:
                barlar = barlar.rename(columns={barlar.columns[0]: "ts"})
            barlar["ts"] = pd.to_datetime(barlar["ts"]).dt.tz_localize(None)
            barlar["gun"] = barlar["ts"].dt.date

            barlar["atr_gun_ici"] = _intraday_atr_hesapla(barlar["high"], barlar["low"], barlar["close"])
            barlar["donchian_ust"] = barlar["high"].rolling(20).max()
            barlar["donchian_alt"] = barlar["low"].rolling(20).min()
            barlar["ema9"] = _ema_hesapla(barlar["close"], 9)
            barlar["ema21"] = _ema_hesapla(barlar["close"], 21)
            adx, plus_di, minus_di = _adx_di_hesapla(barlar["high"], barlar["low"], barlar["close"], 14)
            barlar["adx"], barlar["plus_di"], barlar["minus_di"] = adx, plus_di, minus_di
            barlar["ao"] = _awesome_oscillator_hesapla(barlar["high"], barlar["low"])
            boll_alt, boll_ust = _bollinger_hesapla(barlar["close"], 20, 2.0)
            barlar["boll_alt"], barlar["boll_ust"] = boll_alt, boll_ust
            barlar["cci"] = _cci_hesapla(barlar["high"], barlar["low"], barlar["close"], 20)
            barlar["vwap"] = _vwap_gun_ici_hesapla(barlar["high"], barlar["low"], barlar["close"],
                                                    barlar["volume"], barlar["gun"])
            barlar["vwap_sapma_pct"] = (barlar["close"] - barlar["vwap"]) / barlar["vwap"] * 100
            macd_line, macd_sinyal = _macd_hesapla(barlar["close"], 12, 26, 9)
            barlar["macd_line"], barlar["macd_sinyal"] = macd_line, macd_sinyal

            tetiklenen = {isim: set() for isim in isimler}
            baslangic = 40
            for idx in range(baslangic, len(barlar) - 1):
                bar = barlar.iloc[idx]
                onceki = barlar.iloc[idx - 1]
                gun = bar["gun"]

                # KOR CIZGI - gunun ilk barinda kosulsuz gir
                if gun not in tetiklenen.setdefault("_kor_gun", set()):
                    tetiklenen["_kor_gun"].add(gun)
                    sonuc_l = _gun_ici_ayni_gun_cikis_sonuc(barlar, idx, "LONG")
                    if sonuc_l is not None:
                        tum_sonuclar["[KÖR] Koşulsuz LONG (gün içi)"].append(sonuc_l)
                    sonuc_s = _gun_ici_ayni_gun_cikis_sonuc(barlar, idx, "SHORT")
                    if sonuc_s is not None:
                        tum_sonuclar["[KÖR] Koşulsuz SHORT (gün içi)"].append(sonuc_s)

                adaylar = []
                if gun not in tetiklenen["Donchian-20 Kırılımı"] and pd.notna(bar["donchian_ust"]):
                    if bar["close"] >= bar["donchian_ust"]:
                        adaylar.append(("Donchian-20 Kırılımı", "LONG"))
                    elif bar["close"] <= bar["donchian_alt"]:
                        adaylar.append(("Donchian-20 Kırılımı", "SHORT"))
                if gun not in tetiklenen["EMA9/21 Kesişimi"] and pd.notna(bar["ema9"]) and pd.notna(onceki["ema9"]):
                    if onceki["ema9"] <= onceki["ema21"] and bar["ema9"] > bar["ema21"]:
                        adaylar.append(("EMA9/21 Kesişimi", "LONG"))
                    elif onceki["ema9"] >= onceki["ema21"] and bar["ema9"] < bar["ema21"]:
                        adaylar.append(("EMA9/21 Kesişimi", "SHORT"))
                if gun not in tetiklenen["ADX+DI Yön"] and pd.notna(bar["adx"]) and bar["adx"] >= 25:
                    if onceki["plus_di"] <= onceki["minus_di"] and bar["plus_di"] > bar["minus_di"]:
                        adaylar.append(("ADX+DI Yön", "LONG"))
                    elif onceki["plus_di"] >= onceki["minus_di"] and bar["plus_di"] < bar["minus_di"]:
                        adaylar.append(("ADX+DI Yön", "SHORT"))
                if gun not in tetiklenen["Awesome Oscillator"] and pd.notna(bar["ao"]) and pd.notna(onceki["ao"]):
                    if onceki["ao"] <= 0 and bar["ao"] > 0:
                        adaylar.append(("Awesome Oscillator", "LONG"))
                    elif onceki["ao"] >= 0 and bar["ao"] < 0:
                        adaylar.append(("Awesome Oscillator", "SHORT"))
                if gun not in tetiklenen["Bollinger Bandı Dokunuşu"] and pd.notna(bar["boll_alt"]):
                    if bar["close"] <= bar["boll_alt"]:
                        adaylar.append(("Bollinger Bandı Dokunuşu", "LONG"))
                    elif bar["close"] >= bar["boll_ust"]:
                        adaylar.append(("Bollinger Bandı Dokunuşu", "SHORT"))
                if gun not in tetiklenen["CCI"] and pd.notna(bar["cci"]):
                    if bar["cci"] <= -100:
                        adaylar.append(("CCI", "LONG"))
                    elif bar["cci"] >= 100:
                        adaylar.append(("CCI", "SHORT"))
                if gun not in tetiklenen["VWAP Sapması"] and pd.notna(bar["vwap_sapma_pct"]):
                    if bar["vwap_sapma_pct"] <= -1.0:
                        adaylar.append(("VWAP Sapması", "LONG"))
                    elif bar["vwap_sapma_pct"] >= 1.0:
                        adaylar.append(("VWAP Sapması", "SHORT"))
                if gun not in tetiklenen["MACD Kesişimi"] and pd.notna(bar["macd_line"]) and pd.notna(onceki["macd_line"]):
                    if onceki["macd_line"] <= onceki["macd_sinyal"] and bar["macd_line"] > bar["macd_sinyal"]:
                        adaylar.append(("MACD Kesişimi", "LONG"))
                    elif onceki["macd_line"] >= onceki["macd_sinyal"] and bar["macd_line"] < bar["macd_sinyal"]:
                        adaylar.append(("MACD Kesişimi", "SHORT"))

                for isim, yon in adaylar:
                    tetiklenen[isim].add(gun)
                    sonuc = _gun_ici_ayni_gun_cikis_sonuc(barlar, idx, yon)
                    if sonuc is not None:
                        tum_sonuclar[isim].append(sonuc)
        except Exception as e:
            print(f"[Gün İçi Testi] {ticker} hata: {e}", flush=True)
        time.sleep(0.3)

    satirlar = _kanit_ozet_tablosu(tum_sonuclar)
    if not satirlar:
        return None, "Hiçbir strateji için yeterli veri üretilemedi."

    kor_long_r = next((s["ort_R"] for s in satirlar if s["strateji"] == "[KÖR] Koşulsuz LONG (gün içi)"), None)
    for s in satirlar:
        s["kor_R_farki"] = round(s["ort_R"] - kor_long_r, 4) if kor_long_r is not None and s["ort_R"] is not None else None

    tablo = pd.DataFrame(satirlar).sort_values("kor_R_farki", ascending=False)
    dosya_yolu = _data_path("gun_ici_giris_cikis_testi.csv")
    tablo.to_csv(dosya_yolu, index=False, encoding="utf-8-sig")
    return dosya_yolu, {"satirlar": satirlar, "hisse_sayisi": len(hisseler)}


# =============================================================================
# BAĞIMSIZ ÇALIŞMA MODU (standalone) — 2026-08-19
# =============================================================================
# GEREKÇE: Kullanıcı, us_sinyal_botu.py ile tek bir process'te birleştirme
# sonrası ağır EDGAR taramalarında tekrarlayan tam-süreç donmaları yaşadı.
# Bunun kesin sebebi tam doğrulanamasa da (muhtemelen Render'ın kısıtlı
# ücretsiz kaynaklarıyla ilgili), kullanıcının kendi önerisi mantıklı:
# ihtiyaca göre Render'ın Start Command'ını değiştirerek Ar-Ge botunu
# TAMAMEN AYRI bir süreç olarak çalıştırmak - canlı sinyal botunu hiç
# etkilemeden. Bu blok, arge_botu.py'yi TEK BAŞINA çalıştırılabilir hale
# getiriyor (kendi Flask health-check'i + kendi döngüleri).
#
# KULLANIM: Render'da Start Command'ı geçici olarak
#   python arge_botu.py
# yapıp Ar-Ge testlerini çalıştır, bitince
#   python us_sinyal_botu.py
# olarak geri al - canlı ABD sinyal sistemi bu sürede DURUR (bunu bil).

if __name__ == "__main__":
    from flask import Flask as _StandaloneFlask
    import threading as _standalone_threading

    _PORT = int(os.environ.get("PORT", "10000"))
    _standalone_app = _StandaloneFlask(__name__)

    @_standalone_app.route("/health")
    def _standalone_health():
        return "OK (arge_botu bağımsız modda)", 200

    def _standalone_komut_dongusu():
        while True:
            try:
                poll_arge_commands()
            except Exception as e:
                print(f"[Ar-Ge Bağımsız] Komut döngüsü hatası: {e}", flush=True)
            time.sleep(3)

    def _standalone_arastirma_dongusu():
        while True:
            try:
                maybe_run_research()
            except Exception as e:
                print(f"[Ar-Ge Bağımsız] Araştırma döngüsü hatası: {e}", flush=True)
            time.sleep(5)

    def _standalone_kendi_kendine_ping():
        # 2026-08-19 EKLENDİ: bağımsız modda bu HİÇ yoktu - kullanıcı
        # botun Render'ın ücretsiz sürümünün "hareketsizlikte uyu"
        # mekanizmasıyla uyuyakaldığından şüphelendi, muhtemelen haklıydı.
        import requests as _standalone_requests
        time.sleep(30)  # once uygulamanin tam ayaga kalkmasini bekle
        while True:
            try:
                _standalone_requests.get(f"http://127.0.0.1:{_PORT}/health", timeout=10)
            except Exception as e:
                print(f"[Ar-Ge Bağımsız] Kendi kendine ping hatası: {e}", flush=True)
            time.sleep(600)  # 10 dakikada bir

    print(f"[BAŞLANGIÇ] arge_botu.py BAĞIMSIZ modda çalışıyor — {ARGE_KOD_SURUMU}", flush=True)
    send_telegram_message(
        f"🔬 Ar-Ge Botu BAĞIMSIZ modda başlatıldı — {ARGE_KOD_SURUMU}\n\n"
        f"Bu bot şu an ana ABD sinyal sisteminden TAMAMEN AYRI, kendi "
        f"başına çalışıyor (Start Command geçici olarak değiştirildi).\n"
        f"⚠️ Ana ABD sinyal botu bu sürede ÇALIŞMIYOR - test bitince "
        f"Start Command'ı 'python us_sinyal_botu.py' olarak geri almayı "
        f"unutma.\n"
        f"🔁 Kendi kendine ping: 10 dk'da bir (önceden eksikti, eklendi)\n\n"
        f"/arge_yardim yazarak komutları görebilirsin."
    )
    _standalone_threading.Thread(target=_standalone_komut_dongusu, daemon=True).start()
    _standalone_threading.Thread(target=_standalone_arastirma_dongusu, daemon=True).start()
    _standalone_threading.Thread(target=_standalone_kendi_kendine_ping, daemon=True).start()
    _standalone_app.run(host="0.0.0.0", port=_PORT)
