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
GEMINI_MODEL = os.environ.get("GEMINI_MODEL", "gemini-2.5-flash")  # 2026-08-15: gemini-2.0-flash 1 Haziran 2026'da kapatıldı, 404 veriyordu

ARGE_BOTU_ENABLED = os.environ.get("ARGE_BOTU_ENABLED", "true").lower() == "true"
RESEARCH_COOLDOWN_MINUTES = int(os.environ.get("RESEARCH_COOLDOWN_MINUTES", "10"))
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

HISTORY_FILE = _data_path("arge_hipotez_gecmisi.csv")
HISTORY_FIELDS = ["tarih", "isim", "yon", "kosullar_json", "gerekce",
                   "egitim_n", "egitim_beklenti", "dogrulama_n", "dogrulama_beklenti",
                   "sinav_n", "sinav_beklenti", "onayli_mi", "asama"]

RECONFIRM_FILE = _data_path("arge_yeniden_dogrulama.csv")
RECONFIRM_FIELDS = ["isim", "yon", "kosullar_json", "gerekce", "seri", "son_test_tarih",
                     "kesin_guvenilir_mi", "son_sinav_beklenti"]

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


def evaluate_on_slice(df_slice: pd.DataFrame, h: dict):
    """Bir veri diliminde (egitim/dogrulama/sinav) hipotezi calistirir,
    (n, ortalama_beklenti) doner. Yon SHORT ise hedef ters cevrilir
    (dusus beklendigi icin basari = negatif hareket)."""
    mask = apply_hypothesis(df_slice, h)
    eslesen = df_slice[mask].dropna(subset=["hedef_pct_change"])
    if len(eslesen) < MIN_SAMPLE_PER_STAGE:
        return len(eslesen), None
    hedef = eslesen["hedef_pct_change"]
    if h["yon"] == "SHORT":
        hedef = -hedef
    return len(eslesen), float(hedef.mean())


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


def ask_gemini_for_hypothesis() -> dict:
    """Gemini API'ye gecmis denemeleri gosterip yeni bir hipotez ister.
    Donen deger dogrulanmis (validate_hypothesis'i gecmis) bir sozluk
    ya da None (API hatasi / gecersiz cevap)."""
    if not GEMINI_API_KEY:
        print("[ARGE] GEMINI_API_KEY yok, hipotez istenemiyor.", flush=True)
        return None

    gecmis = _read_history()
    gecmis_ozet = "\n".join(
        f"- {r['isim']} ({r['yon']}): {r['kosullar_json']} -> "
        f"{'ONAYLANDI' if r['onayli_mi'] == '1' else r['asama'] + ' aşamasında elendi'}"
        for r in gecmis[-30:]  # son 30 deneme, prompt cok uzamasin
    ) or "(henüz hiç deneme yok)"

    prompt = f"""Sen bir kantitatif finans araştırmacısısın. BIST ve ABD hisseleri için,
bugünün kapanış verisinden ERTESİ GÜNÜN performansını tahmin edecek yeni bir
teknik hipotez öner.

SADECE şu özellikleri kullanabilirsin (başka hiçbir şey icat etme):
{', '.join(FEATURE_LIBRARY)}

Şimdiye kadar denenenler ve sonuçları:
{gecmis_ozet}

Daha önce denenmemiş, YENİ bir kombinasyon öner (en fazla 4 koşul).
SADECE şu JSON formatında cevap ver, başka hiçbir metin ekleme:
{{"isim": "kisa_isim", "yon": "LONG veya SHORT", "kosullar": [{{"ozellik": "...", "operator": "< veya <= veya > veya >= veya ==", "deger": sayı}}], "gerekce": "kısa açıklama"}}"""

    try:
        resp = requests.post(
            f"https://generativelanguage.googleapis.com/v1beta/models/{GEMINI_MODEL}:generateContent",
            params={"key": GEMINI_API_KEY},
            json={"contents": [{"parts": [{"text": prompt}]}]},
            timeout=30,
        )
        resp.raise_for_status()
        data = resp.json()
        metin = data["candidates"][0]["content"]["parts"][0]["text"]
        metin = metin.strip().removeprefix("```json").removeprefix("```").removesuffix("```").strip()
        h = json.loads(metin)
    except Exception as e:
        print(f"[ARGE] Gemini isteği/ayrıştırma hatası: {e}", flush=True)
        return None

    gecerli, hata = validate_hypothesis(h)
    if not gecerli:
        print(f"[ARGE] Gemini'nin hipotezi geçersiz: {hata}", flush=True)
        return None
    return h


# =============================================================================
# ANA DÖNGÜ
# =============================================================================

def run_research_cycle():
    print(f"[ARGE] Araştırma turu başlıyor...", flush=True)

    h = ask_gemini_for_hypothesis()
    if h is None:
        print("[ARGE] Bu tur hipotez alınamadı, atlanıyor.", flush=True)
        return

    print(f"[ARGE] Hipotez: {h['isim']} ({h['yon']}) - {h['kosullar']}", flush=True)

    df = fetch_all_data()
    if df.empty or len(df) < MIN_TRAIN_ROWS:
        print(f"[ARGE] Yetersiz veri ({len(df)} satır), bu tur atlanıyor.", flush=True)
        return

    egitim, dogrulama, sinav = chronological_split(df)

    egitim_n, egitim_b = evaluate_on_slice(egitim, h)
    asama = "eğitim"
    dogrulama_n, dogrulama_b = 0, None
    sinav_n, sinav_b = 0, None
    onayli = False

    if egitim_b is not None and egitim_b > 0:
        asama = "doğrulama"
        dogrulama_n, dogrulama_b = evaluate_on_slice(dogrulama, h)
        if dogrulama_b is not None and dogrulama_b > 0:
            asama = "sınav"
            sinav_n, sinav_b = evaluate_on_slice(sinav, h)
            if sinav_b is not None and sinav_b > 0:
                onayli = True
                asama = "onaylandı"

    _append_history({
        "tarih": datetime.now(timezone.utc).date().isoformat(),
        "isim": h["isim"], "yon": h["yon"], "kosullar_json": json.dumps(h["kosullar"], ensure_ascii=False),
        "gerekce": h["gerekce"],
        "egitim_n": egitim_n, "egitim_beklenti": round(egitim_b, 3) if egitim_b is not None else "",
        "dogrulama_n": dogrulama_n, "dogrulama_beklenti": round(dogrulama_b, 3) if dogrulama_b is not None else "",
        "sinav_n": sinav_n, "sinav_beklenti": round(sinav_b, 3) if sinav_b is not None else "",
        "onayli_mi": 1 if onayli else 0, "asama": asama,
    })

    print(f"[ARGE] Sonuç: {asama} | eğitim={egitim_b} doğrulama={dogrulama_b} sınav={sinav_b}", flush=True)

    if onayli:
        gecmis_simdi = _read_history()
        toplam_denenen = len(gecmis_simdi)
        toplam_onayli = sum(1 for r in gecmis_simdi if r["onayli_mi"] == "1")
        send_telegram_message(
            f"🎉 [AR-GE — ONAYLANMIŞ HİPOTEZ] '{h['isim']}' ({h['yon']})\n\n"
            f"Gerekçe: {h['gerekce']}\n"
            f"Koşullar: {json.dumps(h['kosullar'], ensure_ascii=False)}\n\n"
            f"📊 Eğitim: {egitim_n} örnek, ort. {egitim_b:+.2f}%\n"
            f"📊 Doğrulama: {dogrulama_n} örnek, ort. {dogrulama_b:+.2f}%\n"
            f"🔒 HİÇ GÖRÜLMEMİŞ SINAV: {sinav_n} örnek, ort. {sinav_b:+.2f}%\n\n"
            f"Bu hipotez üretim/seçim sürecinde HİÇ görülmemiş veride de "
            f"pozitif çıktı — şansla açıklanması daha zor. Yine de kesin "
            f"kanıt değil, canlıya almadan önce ayrıca değerlendirilmeli. "
            f"Hiçbir sisteme otomatik bağlanmadı.\n\n"
            f"📈 Bağlam: şimdiye kadar {toplam_denenen} hipotez denendi, "
            f"bu {toplam_onayli}. onaylanan — çok sayıda deneme arasından "
            f"çıkan bir onay, az denemeyle çıkandan daha temkinli "
            f"değerlendirilmeli (rastgele denemede bile ara sıra şans "
            f"eseri geçen olur).\n\n"
            f"🔁 Bu hipotez şimdi YENİDEN-DOĞRULAMA listesine eklendi — "
            f"her {RECONFIRM_INTERVAL_HOURS} saatte bir güncel veriyle "
            f"tekrar test edilecek. {RECONFIRM_STREAK_REQUIRED} kez üst "
            f"üste geçerse 'KESİN GÜVENİLİR' ilan edilecek, bir kez bile "
            f"başarısız olursa seri sıfırlanacak."
        )
        _register_for_reconfirmation(h)
    else:
        print(f"[ARGE] Hipotez '{asama}' aşamasında elendi, sessizce kaydedildi.", flush=True)


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


def _register_for_reconfirmation(h: dict):
    rows = _read_reconfirm()
    kosullar_json = json.dumps(h["kosullar"], ensure_ascii=False)
    if any(r["isim"] == h["isim"] and r["kosullar_json"] == kosullar_json for r in rows):
        return  # zaten listede
    rows.append({
        "isim": h["isim"], "yon": h["yon"], "kosullar_json": kosullar_json,
        "gerekce": h["gerekce"], "seri": 0,
        "son_test_tarih": datetime.now(timezone.utc).isoformat(),
        "kesin_guvenilir_mi": 0, "son_sinav_beklenti": "",
    })
    _write_reconfirm(rows)


def reconfirm_pending_hypotheses():
    """Her cagrida, RECONFIRM_INTERVAL_HOURS'i gecmis kayitlari GUNCEL/genislemis
    veriyle yeniden test eder. Basarili -> seri += 1 (esige ulasirsa KESIN
    GUVENILIR ilan edilir). Basarisiz -> seri = 0'a sifirlanir ve daha once
    kesinlesmisse 'aslinda o kadar saglam degilmis' diye geri cekilir."""
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

        h = {"isim": r["isim"], "yon": r["yon"], "kosullar": json.loads(r["kosullar_json"]),
             "gerekce": r["gerekce"]}
        egitim, dogrulama, sinav = chronological_split(df)
        _, eb = evaluate_on_slice(egitim, h)
        _, db = evaluate_on_slice(dogrulama, h) if eb is not None and eb > 0 else (0, None)
        sn, sb = evaluate_on_slice(sinav, h) if db is not None and db > 0 else (0, None)
        gecti = sb is not None and sb > 0

        r["son_test_tarih"] = now.isoformat()
        r["son_sinav_beklenti"] = round(sb, 3) if sb is not None else ""
        degisti = True

        onceki_kesin = r["kesin_guvenilir_mi"] in ("1", 1, "True", True)
        if gecti:
            r["seri"] = str(int(r["seri"]) + 1)
            print(f"[ARGE] Yeniden-doğrulama: '{r['isim']}' geçti, seri={r['seri']}", flush=True)
            if int(r["seri"]) >= RECONFIRM_STREAK_REQUIRED and not onceki_kesin:
                r["kesin_guvenilir_mi"] = "1"
                send_telegram_message(
                    f"🏆 [AR-GE — KESİN GÜVENİLİR] '{r['isim']}' ({r['yon']})\n\n"
                    f"{RECONFIRM_STREAK_REQUIRED} kez üst üste, her seferinde "
                    f"GÜNCEL veriyle, 3 aşamayı da geçti (son sınav: "
                    f"{r['son_sinav_beklenti']}%).\n"
                    f"Koşullar: {r['kosullar_json']}\n\n"
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
    gunun buyuk kismi bos gecıyordu. ARTIK: her turdan sonra sadece kisa bir
    bekleme (RESEARCH_COOLDOWN_MINUTES, varsayilan 10 dk) var - Yahoo/Gemini'yi
    art arda yormamak icin bir tampon, "surekli calis ama makul hizda" mantigi."""
    global _last_run_time
    if not ARGE_BOTU_ENABLED or not _ARGE_AVAILABLE:
        return
    now = datetime.now(timezone.utc)
    if _last_run_time is not None and (now - _last_run_time).total_seconds() < RESEARCH_COOLDOWN_MINUTES * 60:
        return
    _last_run_time = now
    try:
        run_research_cycle()
        reconfirm_pending_hypotheses()
    except Exception as e:
        print(f"[ARGE] Döngü hatası: {e}", flush=True)


def send_startup_message():
    send_telegram_message(
        "🔬 Ar-Ge Botu (aynı deploy içinde, izole) başlatıldı.\n\n"
        "Görevi: Gemini API ile yeni teknik hipotezler üretip, "
        "eğitim/doğrulama/hiç-görülmemiş-sınav sürecinden geçiriyor.\n"
        f"Her tur bitince {RESEARCH_COOLDOWN_MINUTES} dk bekleyip hemen "
        "yeni bir hipotez dener — boşta durmaz, ama Yahoo/Gemini'yi de "
        "yormamak için art arda değil, kısa bir tamponla.\n\n"
        "⚠️ Bu bot SADECE araştırma yapar — hiçbir sinyal/emir üretmez, "
        "canlı sisteme bağlı değildir. Sadece bir hipotez üçünü de "
        "(eğitim+doğrulama+hiç görülmemiş sınav) geçerse haber verir — "
        "onay mesajında o ana kadar kaç fikir denendiği de belirtilir, "
        "çünkü çok sayıda deneme arasında bulunan bir onay, az denemeyle "
        "bulunandan daha temkinli değerlendirilmeli.\n\n"
        "/arge_rapor — şimdiye kadarki tüm denemelerin özeti\n"
        "/arge_test — hemen bir hipotez dener (test amaçlı)"
    )


def build_report() -> str:
    gecmis = _read_history()
    if not gecmis:
        return "🔬 [AR-GE BOTU] Henüz hiç hipotez denenmedi."
    toplam = len(gecmis)
    onayli = [r for r in gecmis if r["onayli_mi"] == "1"]
    lines = [f"🔬 [AR-GE RAPORU] Toplam denenen hipotez: {toplam}", f"İlk onay: {len(onayli)}", ""]

    reconfirm = _read_reconfirm()
    if reconfirm:
        kesin = [r for r in reconfirm if r["kesin_guvenilir_mi"] in ("1", 1, "True", True)]
        lines.append(f"🏆 KESİN GÜVENİLİR (tekrar tekrar doğrulandı): {len(kesin)}")
        for r in kesin:
            lines.append(f"  {r['isim']} ({r['yon']}): seri {r['seri']}/{RECONFIRM_STREAK_REQUIRED}")
        bekleyen = [r for r in reconfirm if r not in kesin]
        if bekleyen:
            lines.append(f"\n🔁 Yeniden-doğrulama sürecinde: {len(bekleyen)}")
            for r in bekleyen:
                lines.append(f"  {r['isim']}: seri {r['seri']}/{RECONFIRM_STREAK_REQUIRED}")
        lines.append("")

    lines.append("Son 5 deneme:")
    for r in gecmis[-5:]:
        lines.append(f"  {r['tarih']} {r['isim']}: {r['asama']}")
    return "\n".join(lines)


# =============================================================================
# KENDİ TELEGRAM KOMUT DİNLEYİCİSİ — ana botun poll_stock_commands'ından
# TAMAMEN AYRI, kendi token'ıyla, kendi offset dosyasıyla çalışır. Ana bot
# bunu HİÇ ÇAĞIRMAZ kendi döngüsünde bekleterek - stock_screener_bot.py'nin
# run_forever() döngüsünden her turda kısa bir (non-blocking) çağrı ile
# tetiklenir, tıpkı diğer izole modüllerin kendi zamanlayıcıları gibi.
# =============================================================================

def poll_arge_commands():
    """Kisa, bloklamayan bir Telegram kontrolu - her cagrida en fazla birkac
    saniye surer, ana dongunun akisini durdurmaz. Kendi ARGE token'ini
    kullanir, ana botun TELEGRAM_TOKEN'iyla hicbir ilgisi yoktur."""
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
        elif text.startswith("/arge_test"):
            send_telegram_message("🧪 Manuel test turu başlatılıyor...")
            try:
                run_research_cycle()
                send_telegram_message("🧪 Test turu bitti — onaylıysa yukarıda ayrı mesaj geldi, "
                                       "değilse /arge_rapor ile son denemeyi görebilirsin.")
            except Exception as e:
                send_telegram_message(f"🧪 Test turu hata verdi: {e}")
        elif text.startswith("/arge_yardim"):
            send_telegram_message(
                "📖 Ar-Ge Botu komutları:\n"
                "/arge_rapor — şimdiye kadarki tüm denemelerin özeti\n"
                "/arge_test — hemen yeni bir hipotez dener (test amaçlı)\n"
                "/arge_yardim — bu liste"
            )

    if offset is not None:
        try:
            with open(CMD_OFFSET_FILE, "w") as f:
                f.write(str(offset))
        except Exception:
            pass


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
