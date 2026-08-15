"""
arge_botu.py — AR-GE BOTU: LLM Fikir Üretici + Disiplinli Test Motoru (İZOLE)
================================================================================
2026-08-14 GÜNCELLEMESİ - AYRI SERVİS DEĞİL, İZOLE MODÜL: Bu bot ayrı bir
Render servisi olarak DEĞİL, mevcut B-st-us-bot servisinin İÇİNDE,
kap_monitor.py/radar_canli.py ile AYNI izolasyon deseniyle çalışır:
try/except ile import edilir, kendi zamanlayıcısı vardır, hata verirse
sadece kendi try/except'inde kalır - CANLI SİNYAL SİSTEMİNE (BIST/ABD
taramaları) HİÇBİR ŞEKİLDE DOKUNMAZ, onu asla yavaşlatmaz/durdurmaz/etkilemez.

Ayrı Render servisi kurmaktan vazgeçildi çünkü: (1) Render'ın 750 saatlik
ücretsiz havuzu HESAP BAZLI - ikinci bir 7/24 servis ana botu da düşürme
riski taşırdı; (2) ücretsiz serviste kalıcı disk yok, uyku/uyanma
döngüsünde geçmiş veri sıfırlanırdı. Aynı, zaten 7/24 açık olan servisin
İÇİNDE çalışmak bu iki sorunu da ortadan kaldırıyor - ek saat maliyeti
yok, CSV'ler diğer modüllerle aynı güvenilirlikte kalıcı.

KENDİ TELEGRAM KİMLİĞİ: ARGE_TELEGRAM_TOKEN/ARGE_TELEGRAM_CHAT_ID (kripto
botunun eski token'ı) - ana botun TELEGRAM_TOKEN'ından TAMAMEN AYRI, mesajlar
farklı bir Telegram sohbetine gider, ana bot sohbeti hiç karışmaz.

MİMARİ: Gemini API'ye "şimdiye kadar denediklerimiz + sonuçları" gösterilip
yeni bir hipotez isteniyor. Gemini SERBEST METİN/KOD ÜRETMİYOR — sadece
ÖNCEDEN TANIMLI bir gösterge kütüphanesinden (FEATURE_LIBRARY) seçim yapıp
JSON formatında bir kural öneriyor. Bu bilinçli bir güvenlik kararı: bir
dil modelinin ürettiği kodu otomatik ÇALIŞTIRMAK ciddi bir güvenlik riski
olurdu (prompt injection, hatalı kod, öngörülemeyen davranış) - bunun
yerine model sadece yapılandırılmış bir "seçim" yapıyor, motor (bu dosya)
onu mekanik olarak test ediyor.

GÜVENLİK KATMANI - EĞİTİM/DOĞRULAMA/HİÇ-GÖRÜLMEMİŞ SINAV (3 parça, 2 değil):
Bu proje "%54 başarı" gibi görünüp gerçekte şans olan bulgularla defalarca
karşılaştı (bkz. overnight_backtest.py'nin 35→195 sinyalde eriyen bulgusu).
Bunu önlemek için veri KRONOLOJİK olarak 3'e bölünüyor:
  - EĞİTİM (ilk %50) — Gemini'nin hipotez üretirken "şuna benzer bir şey
    dene" diye görebileceği geçmiş performans burada hesaplanıyor
  - DOĞRULAMA (sonraki %25) — hipotez burada da tutarlı mı diye ikinci kontrol
  - HİÇ GÖRÜLMEMİŞ SINAV (son %25) — SADECE ikisini de geçen hipotezler
    buraya bakıyor. Bu veri, hipotez üretim/seçim sürecinin HİÇBİR
    aşamasında kullanılmıyor - burada iyi çıkmak şansla açıklanamaz.
Bir hipotez SADECE ÜÇÜNÜ DE geçerse "onaylı" sayılıp Telegram'a bildiriliyor.

Hiçbir hipotez otomatik olarak canlı bir sisteme bağlanmıyor - bu bot
SADECE ARAŞTIRMA yapıyor, emir/sinyal üretmiyor, başka hiçbir bot'a/sisteme
dokunmuyor.
"""

import os
import csv
import json
import time
import warnings
from datetime import datetime, timezone

import numpy as np
import pandas as pd
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
GEMINI_MODEL = os.environ.get("GEMINI_MODEL", "gemini-3.6-flash")  # 2026-08-15: 2.5-flash "yeni hesaplara kapalı" hatası verdi, 3.6-flash şu an güncel/GA model

ARGE_BOTU_ENABLED = os.environ.get("ARGE_BOTU_ENABLED", "true").lower() == "true"
RESEARCH_COOLDOWN_MINUTES = int(os.environ.get("RESEARCH_COOLDOWN_MINUTES", "20"))  # 2026-08-15: artik TOPLU istek var, kota degil sadece Yahoo nezaketi icin bekleniyor
BATCH_SIZE = int(os.environ.get("ARGE_BATCH_SIZE", "15"))  # Gemini'ye TEK istekte kac hipotez birden istenecek - kota "kac soru sordun" uzerinden isliyor, tek celp cok fikir = kota tasarrufu
QUEUE_FILE_KURAL = _data_path("arge_kuyruk_kural.json")
QUEUE_FILE_AI = _data_path("arge_kuyruk_ai.json")
MIN_TRAIN_ROWS = int(os.environ.get("MIN_TRAIN_ROWS", "200"))
SUCCESS_THRESHOLD_PCT = float(os.environ.get("SUCCESS_THRESHOLD_PCT", "1.5"))
MIN_SAMPLE_PER_STAGE = int(os.environ.get("MIN_SAMPLE_PER_STAGE", "20"))

# Onaylanan bir hipotez TEK SEFERLİK testten sonra "kesin güvenilir"
# sayılmaz - overnight_model_lab.py'deki ayni felsefe: RECONFIRM_STREAK_REQUIRED
# kez UST USTE (RECONFIRM_INTERVAL_HOURS arayla, her seferinde GÜNCEL/genislemis
# veriyle) ayni 3 asamayi da gecmesi gerekiyor. Bir kez basarisiz olursa seri
# sifirlaniyor - "bir kere sansli cikti" ile "gercekten tutarli" ayirt ediliyor.
RECONFIRM_STREAK_REQUIRED = int(os.environ.get("RECONFIRM_STREAK_REQUIRED", "3"))
RECONFIRM_INTERVAL_HOURS = int(os.environ.get("RECONFIRM_INTERVAL_HOURS", "24"))

# İşlem maliyeti (komisyon + kayma) tahmini - GİDİŞ-DÖNÜŞ yüzde olarak.
# Ham ortalama pozitif olsa bile maliyet düşülünce negatife dönebilir -
# bu yüzden onay kriteri artık HAM değil MALİYET-DÜŞÜLMÜŞ ortalamaya göre.
TRANSACTION_COST_PCT = float(os.environ.get("TRANSACTION_COST_PCT", "0.20"))

HISTORY_FILE = _data_path("arge_hipotez_gecmisi.csv")
HISTORY_FIELDS = ["tarih", "tur_tipi", "isim", "yon", "kosullar_json", "gerekce",
                   "egitim_n", "egitim_ham", "egitim_maliyetli", "egitim_kazanma", "egitim_en_kotu",
                   "dogrulama_n", "dogrulama_ham", "dogrulama_maliyetli", "dogrulama_kazanma", "dogrulama_en_kotu",
                   "sinav_n", "sinav_ham", "sinav_maliyetli", "sinav_kazanma", "sinav_en_kotu",
                   "onayli_mi", "asama"]

RECONFIRM_FILE = _data_path("arge_yeniden_dogrulama.csv")
RECONFIRM_FIELDS = ["isim", "tur_tipi", "yon", "kosullar_json", "gerekce", "seri", "son_test_tarih",
                     "kesin_guvenilir_mi", "son_sinav_maliyetli", "son_sinav_kazanma", "son_sinav_en_kotu"]

CMD_OFFSET_FILE = _data_path("arge_cmd_offset.txt")

# Basit ama gercek bir BIST+US evreni - stock_screener_bot.py'yi IMPORT
# ETMİYORUZ (proje standardı - import yan etkisi riski, historical_autopsy.py'de
# bulunan hatadan ders), kendi sabit, kucuk listesi var.
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
ALL_TICKERS = BIST_TICKERS + US_TICKERS

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


# =============================================================================
# GÖSTERGE KÜTÜPHANESİ (Gemini SADECE bunlardan seçim yapabilir, kod üretmez)
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


FEATURE_LIBRARY = [
    "rsi14", "macd_hist", "bb_bandwidth", "atr_pct", "volume_factor", "cmf", "mfi",
    "stoch_k", "dist_sma20_pct", "dist_sma50_pct", "close_to_high_pct", "gap_pct",
    "pct_change", "day_of_week", "relative_strength",
]


def compute_features(df: pd.DataFrame, index_pct_change: pd.Series = None) -> pd.DataFrame:
    """Ham OHLCV DataFrame'ine (open/high/low/close/volume kolonlari, tarih index)
    tum FEATURE_LIBRARY kolonlarini ekler."""
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
    df["cmf"] = _cmf(df["high"], df["low"], df["close"], df["volume"])
    df["mfi"] = _mfi(df["high"], df["low"], df["close"], df["volume"])
    df["stoch_k"] = _stoch_k(df["high"], df["low"], df["close"])
    df["sma20"] = df["close"].rolling(20).mean()
    df["sma50"] = df["close"].rolling(50).mean()
    df["dist_sma20_pct"] = (df["close"] - df["sma20"]) / df["sma20"].replace(0, np.nan) * 100
    df["dist_sma50_pct"] = (df["close"] - df["sma50"]) / df["sma50"].replace(0, np.nan) * 100
    df["close_to_high_pct"] = (df["close"] - df["low"]) / (df["high"] - df["low"]).replace(0, np.nan) * 100
    df["day_of_week"] = df.index.dayofweek
    if index_pct_change is not None:
        df["relative_strength"] = df["pct_change"] - index_pct_change.reindex(df.index)
    else:
        df["relative_strength"] = np.nan
    # HEDEF: bir sonraki gunun kapanis-kapanis degisimi (lookahead icin
    # bilincli tek istisna - egitimde KULLANILMIYOR, sadece etiket)
    df["hedef_pct_change"] = df["pct_change"].shift(-1)
    return df


# =============================================================================
# HİPOTEZ JSON — GÜVENLİ AYRIŞTIRMA (kod çalıştırma YOK, sadece filtre)
# =============================================================================

VALID_OPERATORS = {"<", "<=", ">", ">=", "=="}


def validate_hypothesis(h: dict) -> tuple:
    """Gemini'den gelen JSON'u dogrular. Gecersizse (False, hata_mesaji)
    doner - hicbir sekilde rastgele kod calistirilmiyor, sadece bu
    yapiya uyan bir sozluk kabul ediliyor."""
    if not isinstance(h, dict):
        return False, "JSON bir sözlük değil"
    for alan in ("isim", "yon", "kosullar", "gerekce"):
        if alan not in h:
            return False, f"'{alan}' eksik"
    if h["yon"] not in ("LONG", "SHORT"):
        return False, "yon LONG veya SHORT olmalı"
    if not isinstance(h["kosullar"], list) or not h["kosullar"]:
        return False, "kosullar boş olmayan bir liste olmalı"
    if len(h["kosullar"]) > 4:
        return False, "en fazla 4 koşul (aşırı karmaşık hipotez reddedildi)"
    for k in h["kosullar"]:
        if not isinstance(k, dict) or not all(x in k for x in ("ozellik", "operator", "deger")):
            return False, "her koşulda ozellik/operator/deger olmalı"
        if k["ozellik"] not in FEATURE_LIBRARY:
            return False, f"bilinmeyen özellik: {k['ozellik']}"
        if k["operator"] not in VALID_OPERATORS:
            return False, f"geçersiz operator: {k['operator']}"
        if not isinstance(k["deger"], (int, float)):
            return False, "deger sayısal olmalı"
    return True, ""


def validate_ai_hypothesis(h: dict) -> tuple:
    """AI-hipotez JSON'unu dogrular. Gemini burada da SADECE FEATURE_LIBRARY'den
    2-6 ozellik SECIYOR - hicbir kod/hiperparametre/model turu belirlemiyor,
    motor sabit, onceden test edilmis bir XGBoost yapilandirmasi kullanir
    (train_model.py/overnight_model_lab.py ile ayni). Kural hipoteziyle AYNI
    guvenlik ilkesi: sadece onceden tanimli bir listeden secim, kod calistirma yok."""
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


def apply_hypothesis(df: pd.DataFrame, h: dict) -> pd.Series:
    """Hipotezin kosullarini DataFrame'e uygular, boolean mask doner.
    SADECE onceden dogrulanmis ozellik/operator/deger uclulerini
    karsilastiriyor - hicbir kod calistirilmiyor (eval/exec YOK)."""
    mask = pd.Series(True, index=df.index)
    for k in h["kosullar"]:
        col = df[k["ozellik"]]
        val = k["deger"]
        op = k["operator"]
        if op == "<":
            mask &= col < val
        elif op == "<=":
            mask &= col <= val
        elif op == ">":
            mask &= col > val
        elif op == ">=":
            mask &= col >= val
        elif op == "==":
            mask &= col == val
    return mask


def _compute_stats(hedef: pd.Series) -> dict:
    """Bir dizi getiri (%) uzerinden ORTAK istatistik seti uretir - hem kural
    hem AI hipotezleri, hem ilk test hem yeniden-dogrulama AYNI fonksiyonu
    kullanir. Maliyet-dusulmus ortalama artik ONAY KRITERI - ham ortalama
    pozitif olsa bile komisyon+kayma dusulunce negatife donebiliyor."""
    n = len(hedef)
    if n < MIN_SAMPLE_PER_STAGE:
        return None
    ort_ham = float(hedef.mean())
    ort_maliyetli = ort_ham - TRANSACTION_COST_PCT
    kazanma_orani = float((hedef > 0).mean() * 100)
    en_kotu = float(hedef.min())
    return {
        "n": n, "ort_ham": round(ort_ham, 4), "ort_maliyetli": round(ort_maliyetli, 4),
        "kazanma_orani": round(kazanma_orani, 2), "en_kotu": round(en_kotu, 3),
    }


def evaluate_on_slice(df_slice: pd.DataFrame, h: dict):
    """Bir veri diliminde (egitim/dogrulama/sinav) KURAL hipotezini calistirir.
    Yon SHORT ise hedef ters cevrilir (dusus beklendigi icin basari = negatif
    hareket). Donen: _compute_stats sozlugu ya da None (yetersiz ornek)."""
    mask = apply_hypothesis(df_slice, h)
    eslesen = df_slice[mask].dropna(subset=["hedef_pct_change"])
    hedef = eslesen["hedef_pct_change"]
    if h["yon"] == "SHORT":
        hedef = -hedef
    return _compute_stats(hedef)


def evaluate_ai_on_slice(model, df_slice: pd.DataFrame, features: list, yon: str, threshold: float = 0.5):
    """Bir veri diliminde AI (model tabanli) hipotezi calistirir - modelin
    pozitif tahmin ettigi (proba>=threshold) satirlarin GERCEK sonuclarina
    bakar. Ayni _compute_stats formatini dondurur, rapor/onay mantigi
    kural hipotezleriyle BIREBIR ortak calisir."""
    alt = df_slice.dropna(subset=features + ["hedef_pct_change"])
    if len(alt) < MIN_SAMPLE_PER_STAGE:
        return None
    proba = model.predict_proba(alt[features])[:, 1]
    secilen = alt[proba >= threshold]
    hedef = secilen["hedef_pct_change"]
    if yon == "SHORT":
        hedef = -hedef
    return _compute_stats(hedef)


# =============================================================================
# VERİ HAZIRLAMA (tüm evren, tüm feature'lar, tek seferde)
# =============================================================================

def fetch_all_data():
    import yfinance as yf
    print("[ARGE] Veri çekiliyor (BIST+US, ~2 yıl günlük)...", flush=True)
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
            df["ticker"] = ticker
            parcalar.append(df)
        except Exception as e:
            print(f"[ARGE] {ticker} veri hatası: {e}", flush=True)
        time.sleep(0.2)

    if not parcalar:
        return pd.DataFrame()
    tum = pd.concat(parcalar)
    tum = tum.dropna(subset=FEATURE_LIBRARY + ["hedef_pct_change"])
    return tum


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
# GEMİNİ'DEN HİPOTEZ İSTEME (yapılandırılmış JSON, serbest kod DEĞİL)
# =============================================================================

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


def _call_gemini(prompt: str):
    """Ortak Gemini cagrisi - hem kural hem AI hipotez istekleri bunu kullanir.
    Ham JSON metnini doner (ayristirma cagiran tarafta), hata olursa None."""
    if not GEMINI_API_KEY:
        print("[ARGE] GEMINI_API_KEY yok, hipotez istenemiyor.", flush=True)
        return None
    try:
        # 2026-08-15: Ham REST istegi (query VEYA header) Google'in yeni "AQ."
        # formatlı anahtarlarıyla hiç çalışmadı (bilinen, çözülmemiş Google
        # sorunu). Resmi google-genai SDK'sı kimlik doğrulamayı kendi içinde
        # farklı ele alıp çalıştı - requirements.txt'de google-genai şart.
        from google import genai
        client = genai.Client(api_key=GEMINI_API_KEY)
        resp = client.models.generate_content(model=GEMINI_MODEL, contents=prompt)
        metin = resp.text.strip().removeprefix("```json").removeprefix("```").removesuffix("```").strip()
        return json.loads(metin)
    except Exception as e:
        print(f"[ARGE] Gemini isteği/ayrıştırma hatası: {e}", flush=True)
        return None


def ask_gemini_for_hypothesis_batch() -> list:
    """Gemini'ye TEK istekte BATCH_SIZE kadar (varsayilan 15) KURAL hipotezi
    birden ister - kota "kac soru sordun" uzerinden isledigi icin, 1 istekte
    15 fikir almak 15 istekte 15 fikir almaktan 15 KAT daha az kota harcar.
    Donen: gecerli (validate_hypothesis'i gecmis) hipotezlerin listesi."""
    gecmis = [r for r in _read_history() if r.get("tur_tipi", "kural") == "kural"]
    gecmis_ozet = "\n".join(
        f"- {r['isim']} ({r['yon']}): {r['kosullar_json']} -> "
        f"{'ONAYLANDI' if r['onayli_mi'] == '1' else r['asama'] + ' aşamasında elendi'}"
        for r in gecmis[-30:]
    ) or "(henüz hiç deneme yok)"

    prompt = f"""Sen bir kantitatif finans araştırmacısısın. BIST ve ABD hisseleri için,
bugünün kapanış verisinden ERTESİ GÜNÜN performansını tahmin edecek {BATCH_SIZE}
FARKLI teknik hipotez öner.

SADECE şu özellikleri kullanabilirsin (başka hiçbir şey icat etme):
{', '.join(FEATURE_LIBRARY)}

Şimdiye kadar denenenler ve sonuçları:
{gecmis_ozet}

Daha önce denenmemiş, BİRBİRİNDEN FARKLI {BATCH_SIZE} kombinasyon öner (her biri en fazla 4 koşul).
SADECE şu JSON DİZİ formatında cevap ver, başka hiçbir metin ekleme:
[{{"isim": "kisa_isim", "yon": "LONG veya SHORT", "kosullar": [{{"ozellik": "...", "operator": "< veya <= veya > veya >= veya ==", "deger": sayı}}], "gerekce": "kısa açıklama"}}, ...]"""

    liste = _call_gemini(prompt)
    if not isinstance(liste, list):
        print(f"[ARGE] Gemini'den beklenen JSON dizisi gelmedi.", flush=True)
        return []

    gecerliler = []
    for h in liste:
        gecerli, hata = validate_hypothesis(h)
        if gecerli:
            gecerliler.append(h)
        else:
            print(f"[ARGE] Toplu hipotezde geçersiz bir tane elendi: {hata}", flush=True)
    print(f"[ARGE] Toplu istek: {len(liste)} hipotez geldi, {len(gecerliler)} geçerli.", flush=True)
    return gecerliler


def ask_gemini_for_ai_hypothesis_batch() -> list:
    """AI-hipotez kolu icin AYNI toplu-istek mantigi - TEK cagrida BATCH_SIZE
    kadar ozellik-kombinasyonu birden istenir."""
    gecmis = [r for r in _read_history() if r.get("tur_tipi") == "ai"]
    gecmis_ozet = "\n".join(
        f"- {r['isim']} ({r['yon']}): {r['kosullar_json']} -> "
        f"{'ONAYLANDI' if r['onayli_mi'] == '1' else r['asama'] + ' aşamasında elendi'}"
        for r in gecmis[-30:]
    ) or "(henüz hiç deneme yok)"

    prompt = f"""Sen bir kantitatif finans araştırmacısısın. BIST ve ABD hisseleri için,
bugünün kapanış verisinden ERTESİ GÜNÜN performansını tahmin edecek küçük
makine öğrenmesi modelleri için {BATCH_SIZE} FARKLI özellik kombinasyonu seç.

SADECE şu özelliklerden her kombinasyonda 2-6 tanesini seçebilirsin (kod/
hiperparametre YAZMA - sadece hangi özellikler kullanılsın onu seç):
{', '.join(FEATURE_LIBRARY)}

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
            print(f"[ARGE] Toplu AI hipotezinde geçersiz bir tane elendi: {hata}", flush=True)
    print(f"[ARGE] Toplu istek (AI): {len(liste)} hipotez geldi, {len(gecerliler)} geçerli.", flush=True)
    return gecerliler


def _read_queue(path: str) -> list:
    if not os.path.exists(path):
        return []
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return []


def _write_queue(path: str, queue: list):
    with open(path, "w", encoding="utf-8") as f:
        json.dump(queue, f, ensure_ascii=False)


def get_next_kural_hypothesis():
    """Kuyruk bossa YENI bir toplu istek atar (kota tuketen tek an burasi),
    doluysa kuyruktan bir tane cikarip DONER - kota harcamadan. Bir kuyruk
    dolumu (15 fikir) ortalama 15 test turu boyunca hic Gemini'ye
    dokunmadan calismayi saglar."""
    q = _read_queue(QUEUE_FILE_KURAL)
    if not q:
        q = ask_gemini_for_hypothesis_batch()
        if not q:
            return None
    h = q.pop(0)
    _write_queue(QUEUE_FILE_KURAL, q)
    print(f"[ARGE] Kural kuyruğunda kalan: {len(q)}", flush=True)
    return h


def get_next_ai_hypothesis():
    q = _read_queue(QUEUE_FILE_AI)
    if not q:
        q = ask_gemini_for_ai_hypothesis_batch()
        if not q:
            return None
    h = q.pop(0)
    _write_queue(QUEUE_FILE_AI, q)
    print(f"[ARGE] AI kuyruğunda kalan: {len(q)}", flush=True)
    return h


def train_ai_model(df_egitim: pd.DataFrame, features: list):
    """train_model.py/overnight_model_lab.py ile AYNI, sabit, onceden test
    edilmis XGBoost yapilandirmasi - Gemini burada hicbir parametreye karar
    vermiyor, sadece hangi ozellikleri kullanacagini secmisti."""
    from xgboost import XGBClassifier
    egitim = df_egitim.dropna(subset=features + ["hedef_pct_change"])
    y = (egitim["hedef_pct_change"] > 0).astype(int)
    if y.nunique() < 2:
        return None
    model = XGBClassifier(n_estimators=150, max_depth=4, learning_rate=0.05,
                           eval_metric="logloss", random_state=42)
    model.fit(egitim[features], y)
    return model


# =============================================================================
# ANA DÖNGÜ
# =============================================================================

# =============================================================================
# ANA DÖNGÜ (iki kol: KURAL hipotezi + AI hipotezi, nöbetleşe çalışır)
# =============================================================================

def _stage_line(etiket: str, s: dict) -> str:
    return (f"{etiket}: {s['n']} örnek | ham {s['ort_ham']:+.2f}% | "
            f"maliyet-düşülmüş {s['ort_maliyetli']:+.2f}% | "
            f"kazanma %{s['kazanma_orani']:.1f} | en kötü {s['en_kotu']:+.2f}%")


def _process_hypothesis_result(tur_tipi: str, isim: str, yon: str, kosullar_repr, gerekce: str,
                                s_egitim, s_dogrulama, s_sinav, asama: str, onayli: bool,
                                reconfirm_payload: dict):
    """KURAL ve AI kollarının ORTAK gunlukleme/bildirim/yeniden-dogrulama-kayit
    mantigi - kod tekrarini onlemek ve iki kolun ayni kurallara uymasini
    garanti etmek icin tek yerden yonetiliyor."""
    kosullar_json = json.dumps(kosullar_repr, ensure_ascii=False)
    _append_history({
        "tarih": datetime.now(timezone.utc).date().isoformat(), "tur_tipi": tur_tipi,
        "isim": isim, "yon": yon, "kosullar_json": kosullar_json, "gerekce": gerekce,
        "egitim_n": s_egitim["n"] if s_egitim else "",
        "egitim_ham": s_egitim["ort_ham"] if s_egitim else "",
        "egitim_maliyetli": s_egitim["ort_maliyetli"] if s_egitim else "",
        "egitim_kazanma": s_egitim["kazanma_orani"] if s_egitim else "",
        "egitim_en_kotu": s_egitim["en_kotu"] if s_egitim else "",
        "dogrulama_n": s_dogrulama["n"] if s_dogrulama else "",
        "dogrulama_ham": s_dogrulama["ort_ham"] if s_dogrulama else "",
        "dogrulama_maliyetli": s_dogrulama["ort_maliyetli"] if s_dogrulama else "",
        "dogrulama_kazanma": s_dogrulama["kazanma_orani"] if s_dogrulama else "",
        "dogrulama_en_kotu": s_dogrulama["en_kotu"] if s_dogrulama else "",
        "sinav_n": s_sinav["n"] if s_sinav else "",
        "sinav_ham": s_sinav["ort_ham"] if s_sinav else "",
        "sinav_maliyetli": s_sinav["ort_maliyetli"] if s_sinav else "",
        "sinav_kazanma": s_sinav["kazanma_orani"] if s_sinav else "",
        "sinav_en_kotu": s_sinav["en_kotu"] if s_sinav else "",
        "onayli_mi": 1 if onayli else 0, "asama": asama,
    })

    print(f"[ARGE] Sonuç ({tur_tipi}): {asama} | "
          f"eğitim={s_egitim['ort_maliyetli'] if s_egitim else None} "
          f"doğrulama={s_dogrulama['ort_maliyetli'] if s_dogrulama else None} "
          f"sınav={s_sinav['ort_maliyetli'] if s_sinav else None}", flush=True)

    if not onayli:
        print(f"[ARGE] Hipotez '{asama}' aşamasında elendi, sessizce kaydedildi.", flush=True)
        return

    gecmis_simdi = _read_history()
    toplam_denenen = len(gecmis_simdi)
    toplam_onayli = sum(1 for r in gecmis_simdi if r["onayli_mi"] == "1")
    tur_etiketi = "KURAL" if tur_tipi == "kural" else "AI (model)"
    send_telegram_message(
        f"🎉 [AR-GE — ONAYLANMIŞ HİPOTEZ — {tur_etiketi}] '{isim}' ({yon})\n\n"
        f"Gerekçe: {gerekce}\n"
        f"{'Koşullar' if tur_tipi == 'kural' else 'Kullanılan özellikler'}: {kosullar_json}\n\n"
        f"📊 {_stage_line('Eğitim', s_egitim)}\n"
        f"📊 {_stage_line('Doğrulama', s_dogrulama)}\n"
        f"🔒 {_stage_line('HİÇ GÖRÜLMEMİŞ SINAV', s_sinav)}\n\n"
        f"(Maliyet-düşülmüş = ~%{TRANSACTION_COST_PCT:.2f} tahmini komisyon+kayma "
        f"düşülmüş hali — asıl karar kriteri bu, ham değil.)\n\n"
        f"Bu hipotez üretim/seçim sürecinde HİÇ görülmemiş veride de "
        f"maliyet sonrası pozitif çıktı — şansla açıklanması daha zor. "
        f"Yine de kesin kanıt değil, canlıya almadan önce ayrıca "
        f"değerlendirilmeli. Hiçbir sisteme otomatik bağlanmadı.\n\n"
        f"📈 Bağlam: şimdiye kadar {toplam_denenen} hipotez denendi, "
        f"bu {toplam_onayli}. onaylanan.\n\n"
        f"🔁 Şimdi YENİDEN-DOĞRULAMA listesine eklendi — her "
        f"{RECONFIRM_INTERVAL_HOURS} saatte bir güncel veriyle tekrar "
        f"test edilecek. {RECONFIRM_STREAK_REQUIRED} kez üst üste "
        f"geçerse 'KESİN GÜVENİLİR' ilan edilecek."
    )
    _register_for_reconfirmation(tur_tipi, isim, yon, kosullar_repr, gerekce)


def run_research_cycle():
    """KURAL hipotezi kolu - esik tabanli basit kurallar."""
    print(f"[ARGE] Araştırma turu başlıyor (kural)...", flush=True)

    h = get_next_kural_hypothesis()
    if h is None:
        print("[ARGE] Bu tur hipotez alınamadı (kuyruk boş + toplu istek başarısız), atlanıyor.", flush=True)
        return
    print(f"[ARGE] Hipotez: {h['isim']} ({h['yon']}) - {h['kosullar']}", flush=True)

    df = fetch_all_data()
    if df.empty or len(df) < MIN_TRAIN_ROWS:
        print(f"[ARGE] Yetersiz veri ({len(df)} satır), bu tur atlanıyor.", flush=True)
        return

    egitim, dogrulama, sinav = chronological_split(df)
    s_egitim = evaluate_on_slice(egitim, h)
    asama, onayli = "eğitim", False
    s_dogrulama = s_sinav = None

    if s_egitim is not None and s_egitim["ort_maliyetli"] > 0:
        asama = "doğrulama"
        s_dogrulama = evaluate_on_slice(dogrulama, h)
        if s_dogrulama is not None and s_dogrulama["ort_maliyetli"] > 0:
            asama = "sınav"
            s_sinav = evaluate_on_slice(sinav, h)
            if s_sinav is not None and s_sinav["ort_maliyetli"] > 0:
                onayli, asama = True, "onaylandı"

    _process_hypothesis_result("kural", h["isim"], h["yon"], h["kosullar"], h["gerekce"],
                                s_egitim, s_dogrulama, s_sinav, asama, onayli, h)


def run_ai_research_cycle():
    """AI hipotezi kolu - Gemini'nin sectigi ozelliklerle kucuk bir XGBoost
    modeli egitilir. AYNI 3 asamali disiplin, AYNI maliyet-dusulmus kriter."""
    print(f"[ARGE] Araştırma turu başlıyor (AI)...", flush=True)

    h = get_next_ai_hypothesis()
    if h is None:
        print("[ARGE] Bu tur AI hipotezi alınamadı (kuyruk boş + toplu istek başarısız), atlanıyor.", flush=True)
        return
    features = h["kullanilacak_ozellikler"]
    print(f"[ARGE] AI Hipotez: {h['isim']} ({h['yon']}) - özellikler: {features}", flush=True)

    df = fetch_all_data()
    if df.empty or len(df) < MIN_TRAIN_ROWS:
        print(f"[ARGE] Yetersiz veri ({len(df)} satır), bu tur atlanıyor.", flush=True)
        return

    egitim, dogrulama, sinav = chronological_split(df)
    model = train_ai_model(egitim, features)
    if model is None:
        print("[ARGE] Model eğitilemedi (yetersiz/tek sınıflı veri), atlanıyor.", flush=True)
        return

    s_egitim = evaluate_ai_on_slice(model, egitim, features, h["yon"])
    asama, onayli = "eğitim", False
    s_dogrulama = s_sinav = None

    if s_egitim is not None and s_egitim["ort_maliyetli"] > 0:
        asama = "doğrulama"
        s_dogrulama = evaluate_ai_on_slice(model, dogrulama, features, h["yon"])
        if s_dogrulama is not None and s_dogrulama["ort_maliyetli"] > 0:
            asama = "sınav"
            # Sinav asamasindan once egitim+dogrulama BIRLIKTE ile modeli
            # yeniden egit - daha fazla veri, ama sinav HALA hic gorulmemis.
            model_final = train_ai_model(pd.concat([egitim, dogrulama]), features)
            s_sinav = evaluate_ai_on_slice(model_final or model, sinav, features, h["yon"])
            if s_sinav is not None and s_sinav["ort_maliyetli"] > 0:
                onayli, asama = True, "onaylandı"

    _process_hypothesis_result("ai", h["isim"], h["yon"], features, h["gerekce"],
                                s_egitim, s_dogrulama, s_sinav, asama, onayli, h)


# =============================================================================
# YENİDEN-DOĞRULAMA — onaylanan bir hipotez tek seferlik testle "kesin
# güvenilir" sayılmaz. RECONFIRM_STREAK_REQUIRED kez üst üste, her seferinde
# GÜNCEL/genişlemiş veriyle aynı 3 aşamayı da geçmesi gerekir.
# =============================================================================

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


def _register_for_reconfirmation(tur_tipi: str, isim: str, yon: str, kosullar_repr, gerekce: str):
    rows = _read_reconfirm()
    kosullar_json = json.dumps(kosullar_repr, ensure_ascii=False)
    if any(r["isim"] == isim and r["kosullar_json"] == kosullar_json for r in rows):
        return  # zaten listede
    rows.append({
        "isim": isim, "tur_tipi": tur_tipi, "yon": yon, "kosullar_json": kosullar_json,
        "gerekce": gerekce, "seri": 0,
        "son_test_tarih": datetime.now(timezone.utc).isoformat(),
        "kesin_guvenilir_mi": 0, "son_sinav_maliyetli": "", "son_sinav_kazanma": "", "son_sinav_en_kotu": "",
    })
    _write_reconfirm(rows)


def _reconfirm_evaluate(r: dict, df: pd.DataFrame):
    """Bir yeniden-dogrulama kaydini GUNCEL veriyle 3 asamadan gecirir -
    tur_tipi'ne gore KURAL ya da AI degerlendirmesi kullanir. Sinav
    istatistigini (s_sinav ya da None) doner."""
    egitim, dogrulama, sinav = chronological_split(df)
    yon = r["yon"]
    if r["tur_tipi"] == "kural":
        h = {"yon": yon, "kosullar": json.loads(r["kosullar_json"])}
        s_e = evaluate_on_slice(egitim, h)
        s_d = evaluate_on_slice(dogrulama, h) if s_e and s_e["ort_maliyetli"] > 0 else None
        s_s = evaluate_on_slice(sinav, h) if s_d and s_d["ort_maliyetli"] > 0 else None
    else:  # "ai"
        features = json.loads(r["kosullar_json"])
        model = train_ai_model(egitim, features)
        s_e = evaluate_ai_on_slice(model, egitim, features, yon) if model else None
        s_d = None
        s_s = None
        if s_e and s_e["ort_maliyetli"] > 0:
            s_d = evaluate_ai_on_slice(model, dogrulama, features, yon)
            if s_d and s_d["ort_maliyetli"] > 0:
                model_final = train_ai_model(pd.concat([egitim, dogrulama]), features) or model
                s_s = evaluate_ai_on_slice(model_final, sinav, features, yon)
    return s_s


def reconfirm_pending_hypotheses():
    """Her cagrida, RECONFIRM_INTERVAL_HOURS'i gecmis kayitlari GUNCEL/genislemis
    veriyle yeniden test eder (KURAL ya da AI turune gore). Basarili -> seri += 1
    (esige ulasirsa KESIN GUVENILIR ilan edilir). Basarisiz -> seri = 0'a
    sifirlanir ve daha once kesinlesmisse 'aslinda o kadar saglam degilmis'
    diye geri cekilir."""
    rows = _read_reconfirm()
    if not rows:
        return
    now = datetime.now(timezone.utc)
    degisti = False

    df = None  # tembel yukleme - hic zamani gelen kayit yoksa hic veri cekme
    for r in rows:
        son_test = datetime.fromisoformat(r["son_test_tarih"])
        if (now - son_test).total_seconds() < RECONFIRM_INTERVAL_HOURS * 3600:
            continue
        if df is None:
            df = fetch_all_data()
            if df.empty or len(df) < MIN_TRAIN_ROWS:
                print("[ARGE] Yeniden-doğrulama için yetersiz veri, bu tur atlanıyor.", flush=True)
                return

        s_sinav = _reconfirm_evaluate(r, df)
        gecti = s_sinav is not None and s_sinav["ort_maliyetli"] > 0

        r["son_test_tarih"] = now.isoformat()
        r["son_sinav_maliyetli"] = s_sinav["ort_maliyetli"] if s_sinav else ""
        r["son_sinav_kazanma"] = s_sinav["kazanma_orani"] if s_sinav else ""
        r["son_sinav_en_kotu"] = s_sinav["en_kotu"] if s_sinav else ""
        degisti = True

        onceki_kesin = r["kesin_guvenilir_mi"] in ("1", 1, "True", True)
        if gecti:
            r["seri"] = str(int(r["seri"]) + 1)
            print(f"[ARGE] Yeniden-doğrulama: '{r['isim']}' geçti, seri={r['seri']}", flush=True)
            if int(r["seri"]) >= RECONFIRM_STREAK_REQUIRED and not onceki_kesin:
                r["kesin_guvenilir_mi"] = "1"
                send_telegram_message(
                    f"🏆 [AR-GE — KESİN GÜVENİLİR] '{r['isim']}' ({r['yon']}, {r['tur_tipi']})\n\n"
                    f"{RECONFIRM_STREAK_REQUIRED} kez üst üste, her seferinde "
                    f"GÜNCEL veriyle, maliyet-düşülmüş 3 aşamayı da geçti "
                    f"(son sınav: {r['son_sinav_maliyetli']}%, kazanma "
                    f"%{r['son_sinav_kazanma']}, en kötü {r['son_sinav_en_kotu']}%).\n"
                    f"{'Koşullar' if r['tur_tipi'] == 'kural' else 'Özellikler'}: {r['kosullar_json']}\n\n"
                    f"Bu artık tek seferlik şans olma ihtimali düşük bir "
                    f"bulgu. Yine de canlıya almadan önce ayrıca "
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
    """ONCEDEN: sabit 24 saat bekliyordu, bir tur birkac dakika surdugu icin
    gunun buyuk kismi bos gecıyordu. 10 dk'lik bekleme de denendi ama Gemini
    ucretsiz kotasi (gunde 20 istek/model) hemen asildi - simdi 80 dk
    (RESEARCH_COOLDOWN_MINUTES) ile gunde ~18 istek yapiliyor, kotanin
    guvenle altinda.
    KURAL ve AI kollari NOBETLESE calisir - toplam gecmis uzunlugunun tek/cift
    olmasina gore hangi turun sirasi geldigi belirlenir, ekstra durum dosyasi
    gerekmez."""
    global _last_run_time
    if not ARGE_BOTU_ENABLED or not _ARGE_AVAILABLE:
        return
    now = datetime.now(timezone.utc)
    if _last_run_time is not None and (now - _last_run_time).total_seconds() < RESEARCH_COOLDOWN_MINUTES * 60:
        return
    _last_run_time = now
    try:
        toplam_gecmis = len(_read_history())
        if toplam_gecmis % 2 == 0:
            run_research_cycle()
        else:
            run_ai_research_cycle()
        reconfirm_pending_hypotheses()
    except Exception as e:
        print(f"[ARGE] Döngü hatası: {e}", flush=True)


def send_startup_message():
    send_telegram_message(
        "🔬 Ar-Ge Botu (aynı deploy içinde, izole) başlatıldı.\n\n"
        "Görevi: Gemini API ile yeni teknik hipotezler üretip, "
        "eğitim/doğrulama/hiç-görülmemiş-sınav sürecinden geçiriyor. "
        "İki kol NÖBETLEŞE çalışıyor:\n"
        "  🔹 KURAL kolu — basit eşik kuralları (\"RSI<30 ise\")\n"
        "  🔹 AI kolu — Gemini'nin seçtiği özelliklerle küçük bir model eğitiliyor\n\n"
        f"Her kolun kendi TOPLU hipotez kuyruğu var — bir seferde Gemini'ye "
        f"{BATCH_SIZE} fikir birden sorulup kuyruğa alınıyor, sonra kuyruk "
        f"bitene kadar HİÇ Gemini'ye dokunmadan sırayla test ediliyor. "
        f"Böylece kota (günde 20 istek/model) sadece kuyruk boşaldığında "
        f"harcanıyor, çok daha verimli.\n\n"
        f"Her tur bitince {RESEARCH_COOLDOWN_MINUTES} dk bekleyip yeni bir "
        "hipotez test eder (Yahoo'yu yormamak için, Gemini kotası artık "
        "sorun değil).\n\n"
        f"Onay kriteri artık MALİYET-DÜŞÜLMÜŞ ortalama (~%{TRANSACTION_COST_PCT:.2f} "
        "tahmini komisyon+kayma düşülmüş) — ham pozitif yetmiyor.\n\n"
        "⚠️ Bu bot SADECE araştırma yapar — hiçbir sinyal/emir üretmez, "
        "canlı sisteme bağlı değildir. Sadece bir hipotez üçünü de "
        "(eğitim+doğrulama+hiç görülmemiş sınav, maliyet sonrası) geçerse "
        "haber verir.\n\n"
        "/arge_rapor — şimdiye kadarki tüm denemelerin özeti\n"
        "/arge_test — hemen bir hipotez dener (test amaçlı)"
    )


def build_report() -> str:
    gecmis = _read_history()
    if not gecmis:
        return "🔬 [AR-GE BOTU] Henüz hiç hipotez denenmedi."
    toplam = len(gecmis)
    kural_n = sum(1 for r in gecmis if r.get("tur_tipi", "kural") == "kural")
    ai_n = sum(1 for r in gecmis if r.get("tur_tipi") == "ai")
    onayli = [r for r in gecmis if r["onayli_mi"] == "1"]
    lines = [f"🔬 [AR-GE RAPORU] Toplam: {toplam} (🔹kural {kural_n}, 🔹AI {ai_n})",
             f"İlk onay: {len(onayli)}", ""]

    reconfirm = _read_reconfirm()
    if reconfirm:
        kesin = [r for r in reconfirm if r["kesin_guvenilir_mi"] in ("1", 1, "True", True)]
        lines.append(f"🏆 KESİN GÜVENİLİR (tekrar tekrar doğrulandı): {len(kesin)}")
        for r in kesin:
            lines.append(f"  {r['isim']} ({r['yon']}, {r['tur_tipi']}): seri {r['seri']}/{RECONFIRM_STREAK_REQUIRED}")
        bekleyen = [r for r in reconfirm if r not in kesin]
        if bekleyen:
            lines.append(f"\n🔁 Yeniden-doğrulama sürecinde: {len(bekleyen)}")
            for r in bekleyen:
                lines.append(f"  {r['isim']} ({r['tur_tipi']}): seri {r['seri']}/{RECONFIRM_STREAK_REQUIRED}")
        lines.append("")

    lines.append("Son 5 deneme:")
    for r in gecmis[-5:]:
        maliyetli = r.get("sinav_maliyetli") or r.get("dogrulama_maliyetli") or r.get("egitim_maliyetli") or ""
        lines.append(f"  {r['tarih']} [{r.get('tur_tipi','kural')}] {r['isim']}: {r['asama']}"
                     + (f" (maliyet-düşülmüş {maliyetli}%)" if maliyetli != "" else ""))
    return "\n".join(lines)


# =============================================================================
# KENDİ TELEGRAM KOMUT DİNLEYİCİSİ (ana bottan AYRI token/chat_id) —
# main.py'ye hiç dokunmadan, stock_screener_bot.py'nin run_forever() dönüsü
# içinden çağrılabilir, kendi offset dosyasıyla self-contained çalışır.
# =============================================================================

_ARGE_CMD_OFFSET_FILE = _data_path("arge_cmd_offset.txt")


def _arge_load_offset():
    if os.path.exists(_ARGE_CMD_OFFSET_FILE):
        try:
            return int(open(_ARGE_CMD_OFFSET_FILE).read().strip())
        except Exception:
            return 0
    return 0


def _arge_save_offset(offset):
    try:
        with open(_ARGE_CMD_OFFSET_FILE, "w") as f:
            f.write(str(offset))
    except Exception:
        pass


def poll_arge_commands():
    """Ar-Ge botunun kendi Telegram kimliğini (ARGE_TELEGRAM_TOKEN) dinler -
    ana botun TELEGRAM_TOKEN'ından TAMAMEN AYRI. Hata sessizce yutulur,
    ana döngüyü asla durdurmaz (kap_monitor.py/radar_canli.py ile ayni ilke)."""
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        return
    offset = _arge_load_offset()
    try:
        resp = requests.get(
            f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/getUpdates",
            params={"offset": offset, "timeout": 0}, timeout=15)
        updates = resp.json().get("result", [])
    except Exception as e:
        print(f"[ARGE] komut dinleyici hatası: {e}", flush=True)
        return
    if not updates:
        return

    max_id = offset - 1
    for u in updates:
        max_id = max(max_id, u.get("update_id", 0))
        msg = u.get("message") or {}
        text = (msg.get("text") or "").strip()
        chat_id = str((msg.get("chat") or {}).get("id", ""))
        if chat_id != str(TELEGRAM_CHAT_ID) or not text.startswith("/"):
            continue
        try:
            if text.startswith("/arge_rapor"):
                send_telegram_message(build_report())
            elif text.startswith("/arge_test"):
                send_telegram_message("🧪 Manuel test turu başlatılıyor...")
                run_research_cycle()
                send_telegram_message("🧪 Test turu bitti — sonuç yukarıda (onaylıysa) ya da /arge_rapor ile sorgulanabilir.")
            elif text.startswith("/yardim") or text.startswith("/help"):
                send_telegram_message(
                    "📖 Ar-Ge Botu komutları:\n"
                    "/arge_rapor — şimdiye kadarki tüm denemelerin özeti\n"
                    "/arge_test — hemen yeni bir hipotez dener (test amaçlı)\n"
                    "/yardim — bu liste"
                )
        except Exception as e:
            print(f"[ARGE] komut işleme hatası: {e}", flush=True)
    _arge_save_offset(max_id + 1)
