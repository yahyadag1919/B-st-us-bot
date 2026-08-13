"""
overnight_radar.py — CANLI İNDİKATÖR SİSTEMİ + AI GÖLGE MODU (İZOLE)
===========================================================================
2026-08-13 MİMARİ DEĞİŞİKLİĞİ (Gemini + backtest sonuçları): 150 günlük
walk-forward testte AI modeli (tek başına ya da indikatörle birlikte)
gerçek bir kenar (edge) göstermedi (bkz. rapor 29) — en güvenilir,
büyük örneklemli sonuç SADECE İNDİKATÖR PUANLAMASIYDI (3184 sinyal,
+0.018R). Bu yüzden:

- CANLI TELEGRAM SİNYALİ artık İNDİKATÖR PUANLAMASI + SABİT 1:2 R:R
  (TP +%2 / SL -%1 / timeout: ertesi gün 12:00) ile üretiliyor - AI
  modeli DEĞİL. Günde en yüksek puanlı OVERNIGHT_MAX_SIGNALS (varsayılan
  4) hisse gönderiliyor.
- overnight_model.pkl artık GÖLGE MODDA: her tarama turunda taranan
  TÜM hisseler için tahmin üretmeye devam ediyor ama HİÇBİR TELEGRAM
  MESAJI GÖNDERMİYOR, hiçbir sinyali engellemiyor/tetiklemiyor - sadece
  ai_shadow_log.csv'ye kaydediyor. Amaç: modelin gerçekten bir kenar
  kazanıp kazanmadığını, seçim yanlılığı olmadan (SEÇİLEN değil TÜM
  taranan hisseler için) izlemeye devam etmek.
- Modelin kendi kendini "sürekli/sınırsız" optimize etmesi KURULMADI -
  bu proje defalarca (M15 turnuvası, rapor 28→29) çok sayıda varyant
  denemenin şans eseri iyi görünen ama gerçek olmayan sonuçlar
  ürettiğini gördü. Bunun yerine `overnight_model_lab.py`: az sayıda
  BELİRLENMİŞ feature-alt-kümesi varyantını gerçek train/test ayrımıyla
  karşılaştıran, SESSİZCE model.pkl'yi asla üzerine yazmayan, ayrı bir
  script.

FEATURE SIRASI (2026-08-11, kullanıcı Colab eğitimini teyit etti):
volume_factor, rsi, price_change_pct, gap_pct, cmf, has_catalyst,
close_to_high_ratio (7. feature — kapanışın günün en yükseğine yakınlığı,
0-1 arası, historical_autopsy.py'deki close_to_high_pct ile aynı formül
ama 0-1 ölçeğinde: (kapanış-düşük)/(yüksek-düşük)).
has_catalyst SADECE BIST için kap_monitor.py'nin verisinden dolduruluyor.

İNDİKATÖR PUANLAMASI (overnight_backtest.py ile BİREBİR AYNI eşikler,
tutarlılık için): CMF>0.10, hacim_faktörü≥1.5, kapanış-zirve≥0.7,
RSI 35-55 arası - en az 2/4.

ÇALIŞMA ZAMANI: Her BIST işlem günü kapanışa doğru, 17:45–17:55 (İstanbul)
arası bir kez tetiklenir. SADECE BIST için.

pandas_ta KULLANILMADI - indikatörler elle yazıldı (ml_radar.py ile aynı).
"""

import os
import csv
import time
import ast
import warnings
from datetime import datetime, timezone, timedelta
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd
import requests
import yfinance as yf

warnings.filterwarnings("ignore")

DATA_DIR = os.environ.get("DATA_DIR", ".")


def _data_path(filename: str) -> str:
    return os.path.join(DATA_DIR, filename)


# =============================================================================
# AYARLAR
# =============================================================================

TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN", "")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")

OVERNIGHT_RADAR_ENABLED = os.environ.get("OVERNIGHT_RADAR_ENABLED", "true").lower() == "true"

# --- CANLI SİNYAL (indikatör tabanlı, backtest'te doğrulanan tek şey) ---
# overnight_backtest.py ile BİREBİR AYNI eşikler - tutarlılık şart.
INDICATOR_SCORE_MIN = int(os.environ.get("OVERNIGHT_INDICATOR_SCORE_MIN", "2"))
CMF_MIN = float(os.environ.get("OVERNIGHT_CMF_MIN", "0.10"))
VOLUME_FACTOR_MIN = float(os.environ.get("OVERNIGHT_VOLUME_FACTOR_MIN", "1.5"))
CLOSE_TO_HIGH_MIN = float(os.environ.get("OVERNIGHT_CLOSE_TO_HIGH_MIN", "0.7"))
RSI_DIP_MIN, RSI_DIP_MAX = 35.0, 55.0
MAX_SIGNALS_PER_DAY = int(os.environ.get("OVERNIGHT_MAX_SIGNALS", "4"))
TP_PCT = float(os.environ.get("OVERNIGHT_TP_PCT", "2.0"))
SL_PCT = float(os.environ.get("OVERNIGHT_SL_PCT", "1.0"))

# --- AI GÖLGE MODU (Telegram'a hiç gitmez, sadece gözlem/log) ---
AI_SHADOW_ENABLED = os.environ.get("AI_SHADOW_ENABLED", "true").lower() == "true"
MODEL_PATH = os.environ.get("OVERNIGHT_MODEL_PATH", "overnight_model.pkl")
FEATURE_COLUMNS = ["volume_factor", "rsi", "price_change_pct", "gap_pct", "cmf",
                    "has_catalyst", "close_to_high_ratio"]
AI_SHADOW_LOG_FILE = _data_path("ai_shadow_log.csv")
AI_SHADOW_FIELDS = ["id", "created_at", "symbol", "ai_score", "indikator_skor",
                     "secildi_mi"] + FEATURE_COLUMNS + ["entry_price", "checked_at", "result", "gerceklesen_pct"]

# Tetiklenme penceresi: BIST kapanışına doğru, İstanbul saatiyle
TRIGGER_WINDOW_START = (17, 45)
TRIGGER_WINDOW_END = (17, 55)

NEXT_DAY_CHECK_WINDOW = ((10, 0), (12, 0))  # BIST'in ilk 2 saati, Istanbul
KAP_MATCH_WINDOW_MINUTES = int(os.environ.get("OVERNIGHT_KAP_MATCH_WINDOW_MINUTES", "240"))

INDEX_TICKERS = {"BIST": "XU100.IS", "US": "SPY"}

SIGNAL_LOG_FILE = _data_path("overnight_radar_signals.csv")
SIGNAL_FIELDS = ["id", "created_at", "symbol", "market", "indikator_skor", "volume_factor",
                  "rsi", "pct_change", "gap_percent", "cmf", "has_catalyst",
                  "close_to_high_ratio", "entry_price", "tp_price", "sl_price",
                  "checked_at", "result", "exit_price", "r_multiple"]

_last_scan_time = {}
_model = None
_MODEL_AVAILABLE = False


# =============================================================================
# MODEL YÜKLEME (hata olsa bile ana sistemi düşürmez - artık sadece golge modu icin)
# =============================================================================

def _load_model():
    global _model, _MODEL_AVAILABLE
    try:
        import joblib
        _model = joblib.load(MODEL_PATH)
        _MODEL_AVAILABLE = True
        print(f"[OVERNIGHT] model.pkl yüklendi ({MODEL_PATH}) - GÖLGE MODDA (Telegram'a gitmiyor).", flush=True)
    except Exception as e:
        _MODEL_AVAILABLE = False
        print(f"[OVERNIGHT] model.pkl YÜKLENEMEDİ - gölge mod devre dışı (canlı sinyal etkilenmez): {e}", flush=True)


_load_model()


def send_telegram_message(text: str):
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        return
    try:
        requests.post(
            f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage",
            json={"chat_id": TELEGRAM_CHAT_ID, "text": text[:4000]}, timeout=20)
    except Exception as e:
        print(f"[OVERNIGHT] Telegram gönderilemedi: {e}", flush=True)


# =============================================================================
# YEREL CSV KAYIT
# =============================================================================

def _next_id(dosya, alanlar) -> int:
    if not os.path.exists(dosya):
        return 1
    with open(dosya, newline="", encoding="utf-8") as f:
        rows = list(csv.DictReader(f))
    return (max((int(r["id"]) for r in rows), default=0) + 1) if rows else 1


def _append_row(dosya, alanlar, row: dict):
    exists = os.path.exists(dosya)
    with open(dosya, "a", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=alanlar)
        if not exists:
            w.writeheader()
        w.writerow(row)


def _read_rows(dosya):
    if not os.path.exists(dosya):
        return []
    with open(dosya, newline="", encoding="utf-8") as f:
        return list(csv.DictReader(f))


def _write_rows(dosya, alanlar, rows):
    with open(dosya, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=alanlar)
        w.writeheader()
        w.writerows(rows)


def _read_signals():
    return _read_rows(SIGNAL_LOG_FILE)


def _write_signals(rows):
    _write_rows(SIGNAL_LOG_FILE, SIGNAL_FIELDS, rows)


def _log_signal(market, ticker, indikator_skor, ham, tp_price, sl_price):
    row = {
        "id": _next_id(SIGNAL_LOG_FILE, SIGNAL_FIELDS), "created_at": datetime.now(timezone.utc).isoformat(),
        "symbol": ticker, "market": market, "indikator_skor": indikator_skor,
        "volume_factor": ham["volume_factor"], "rsi": ham["rsi14"],
        "pct_change": ham["pct_change"], "gap_percent": ham["gap_percent"],
        "cmf": ham["cmf"], "has_catalyst": ham["has_catalyst"],
        "close_to_high_ratio": ham["close_to_high_ratio"],
        "entry_price": ham["fiyat"], "tp_price": round(tp_price, 4), "sl_price": round(sl_price, 4),
        "checked_at": "", "result": "PENDING", "exit_price": "", "r_multiple": "",
    }
    _append_row(SIGNAL_LOG_FILE, SIGNAL_FIELDS, row)


def _log_shadow(ticker, proba, indikator_skor, secildi_mi, feats, ham):
    row = {
        "id": _next_id(AI_SHADOW_LOG_FILE, AI_SHADOW_FIELDS), "created_at": datetime.now(timezone.utc).isoformat(),
        "symbol": ticker, "ai_score": round(float(proba), 4), "indikator_skor": indikator_skor,
        "secildi_mi": int(secildi_mi),
        **{c: feats[c] for c in FEATURE_COLUMNS},
        "entry_price": ham["fiyat"], "checked_at": "", "result": "PENDING", "gerceklesen_pct": "",
    }
    _append_row(AI_SHADOW_LOG_FILE, AI_SHADOW_FIELDS, row)


def _indicator_score(ham) -> int:
    """overnight_backtest.py'deki _indicator_score ile BİREBİR AYNI mantık -
    canlı sinyal ile backtest sonucu tutarlı olsun diye."""
    kosullar = [
        ham["cmf"] > CMF_MIN,
        ham["volume_factor"] >= VOLUME_FACTOR_MIN,
        ham["close_to_high_ratio"] >= CLOSE_TO_HIGH_MIN,
        RSI_DIP_MIN <= ham["rsi14"] <= RSI_DIP_MAX,
    ]
    return sum(kosullar)


def _has_catalyst(market: str, ticker: str) -> int:
    """1 = son KAP_MATCH_WINDOW_MINUTES icinde KAP/haber katalizoru var, yoksa 0.
    SADECE BIST icin kap_monitor.py'nin biriktirdigi veriyi kullanabiliyoruz -
    ABD icin henuz entegre bir haber kaynagi yok, bu yuzden ABD'de bu deger
    HER ZAMAN 0 doner. Colab'daki egitim verisinde has_catalyst ABD ornekleri
    icin nasil dolduruldu bilinmiyor - bu taninmis bir sinirlama."""
    if market != "BIST":
        return 0
    try:
        import kap_monitor as km
    except Exception:
        return 0
    kod = ticker.replace(".IS", "")
    try:
        rows = km._read_log()
    except Exception:
        return 0
    now = datetime.now(timezone.utc)
    for r in rows:
        if r.get("kod") != kod or not r.get("gorulme_time"):
            continue
        try:
            gorulme = datetime.fromisoformat(r["gorulme_time"])
        except Exception:
            continue
        if (now - gorulme).total_seconds() / 60 <= KAP_MATCH_WINDOW_MINUTES:
            return 1
    return 0


# =============================================================================
# TICKER LİSTELERİ — stock_screener_bot.py'yi IMPORT ETMEDEN, statik okuma
# (historical_autopsy.py'de bulunan import-yan-etkisi hatasından ders)
# =============================================================================

def _load_tickers_from_bot_file(path="stock_screener_bot.py"):
    try:
        with open(path, encoding="utf-8") as f:
            kaynak = f.read()
        agac = ast.parse(kaynak)
        bulunan = {}
        for node in ast.walk(agac):
            if isinstance(node, ast.Assign) and len(node.targets) == 1:
                hedef = node.targets[0]
                if isinstance(hedef, ast.Name) and hedef.id in ("BIST_TICKERS", "US_INTRADAY_TICKERS"):
                    if isinstance(node.value, ast.List):
                        degerler = [el.value for el in node.value.elts
                                    if isinstance(el, ast.Constant) and isinstance(el.value, str)]
                        if degerler:
                            bulunan[hedef.id] = degerler
        return bulunan.get("BIST_TICKERS"), bulunan.get("US_INTRADAY_TICKERS")
    except Exception as e:
        print(f"[OVERNIGHT] Ticker listesi okunamadı: {e}", flush=True)
        return None, None


_bist_t, _us_t = _load_tickers_from_bot_file()
BIST_TICKERS = _bist_t or ["THYAO.IS", "ASELS.IS", "SISE.IS", "GARAN.IS", "AKBNK.IS"]
US_TICKERS = _us_t or ["AAPL", "MSFT", "NVDA", "TSLA", "AMD"]
MARKET_TICKERS = {"BIST": BIST_TICKERS, "US": US_TICKERS}


# =============================================================================
# İNDİKATÖRLER (pandas_ta yok, elle yazıldı — historical_autopsy.py ile aynı)
# =============================================================================

def _rsi(close: pd.Series, n=14) -> pd.Series:
    delta = close.diff()
    gain = delta.clip(lower=0)
    loss = -delta.clip(upper=0)
    avg_gain = gain.ewm(alpha=1 / n, adjust=False).mean()
    avg_loss = loss.ewm(alpha=1 / n, adjust=False).mean()
    rs = avg_gain / avg_loss.replace(0, np.nan)
    return 100 - (100 / (1 + rs))


def _cmf(high, low, close, volume, n=20) -> pd.Series:
    rng = (high - low).replace(0, np.nan)
    mfm = ((close - low) - (high - close)) / rng
    mfv = mfm * volume
    return mfv.rolling(n).sum() / volume.rolling(n).sum()


def bist_is_open(now_ist=None) -> bool:
    now_ist = now_ist or datetime.now(ZoneInfo("Europe/Istanbul"))
    if now_ist.weekday() >= 5:
        return False
    m = now_ist.hour * 60 + now_ist.minute
    return 10 * 60 <= m < 18 * 60


def us_is_open(now_et=None) -> bool:
    now_et = now_et or datetime.now(ZoneInfo("America/New_York"))
    if now_et.weekday() >= 5:
        return False
    m = now_et.hour * 60 + now_et.minute
    return 9 * 60 + 30 <= m < 16 * 60


def _market_is_open(market: str) -> bool:
    return bist_is_open() if market == "BIST" else us_is_open()


# =============================================================================
# CANLI FEATURE ÇIKARIMI
# =============================================================================

def _fetch_15m(ticker: str, period="5d") -> pd.DataFrame:
    df = yf.Ticker(ticker).history(period=period, interval="15m")
    if df is None or df.empty:
        return pd.DataFrame()
    df = df.reset_index().rename(columns={
        "Datetime": "ts", "Date": "ts", "Open": "open", "High": "high",
        "Low": "low", "Close": "close", "Volume": "volume"})
    need = ["ts", "open", "high", "low", "close", "volume"]
    if not all(c in df.columns for c in need):
        return pd.DataFrame()
    df = df[need].copy()
    df["session"] = pd.to_datetime(df["ts"]).dt.date
    return df


def build_live_features(ticker: str, market: str, index_pct_today: float):
    """Canli veriden model.pkl'ye sorulacak feature vektorunu uretir.
    Donen: (features_dict, ham_veri_dict) - ikincisi Telegram mesaji ve
    CSV kaydi icin, birincisi FEATURE_COLUMNS sirasinda modele girer."""
    df = _fetch_15m(ticker)
    if df.empty or len(df) < 25:
        return None, None
    today = df["session"].iloc[-1]
    gun = df[df["session"] == today]
    if len(gun) < 3:
        return None, None

    acilis = float(gun.iloc[0]["open"])
    if acilis <= 0:
        return None, None
    fiyat = float(gun.iloc[-1]["close"])
    pct_change = (fiyat - acilis) / acilis * 100
    gap_percent = pct_change  # gun ici ilk barin acilisiyla ayni oldugundan gap ~ pct_change'e yakin; net gap icin onceki gun kapanisi da eklenebilir

    onceki_gun = sorted(df["session"].unique())
    if today in onceki_gun and len(onceki_gun) >= 2:
        prev_day = onceki_gun[onceki_gun.index(today) - 1]
        prev_close = float(df[df["session"] == prev_day].iloc[-1]["close"])
        gap_percent = (acilis - prev_close) / prev_close * 100 if prev_close else pct_change

    vol_ma = df["volume"].tail(20 * 8).mean()  # ~son birkac gunun bar-hacim ortalamasi
    volume_factor = float(gun.iloc[-1]["volume"] / vol_ma) if vol_ma else np.nan

    df["rsi14"] = _rsi(df["close"], 14)
    rsi14 = float(df["rsi14"].iloc[-1])

    df["cmf"] = _cmf(df["high"], df["low"], df["close"], df["volume"])
    cmf = float(df["cmf"].iloc[-1])

    has_catalyst = _has_catalyst(market, ticker)

    gun_high = float(gun["high"].max())
    gun_low = float(gun["low"].min())
    close_to_high_ratio = (fiyat - gun_low) / (gun_high - gun_low) if (gun_high - gun_low) > 0 else 0.5

    ham = {
        "fiyat": fiyat, "pct_change": round(pct_change, 3),
        "gap_percent": round(gap_percent, 3), "volume_factor": round(volume_factor, 3) if not np.isnan(volume_factor) else None,
        "rsi14": round(rsi14, 2) if not np.isnan(rsi14) else None,
        "cmf": round(cmf, 4) if not np.isnan(cmf) else None,
        "has_catalyst": has_catalyst,
        "close_to_high_ratio": round(close_to_high_ratio, 4),
    }
    # Model.predict_proba'ya BU ISIMLERLE ve BU SIRAYLA gidecek (FEATURE_COLUMNS ile birebir)
    feats = {"volume_factor": volume_factor, "rsi": rsi14, "price_change_pct": pct_change,
              "gap_pct": gap_percent, "cmf": cmf, "has_catalyst": has_catalyst,
              "close_to_high_ratio": close_to_high_ratio}

    if any(v is None or (isinstance(v, float) and np.isnan(v)) for v in feats.values()):
        return None, None
    return feats, ham


# =============================================================================
# TARAMA
# =============================================================================

def _get_index_today_pct(market: str) -> float:
    df = _fetch_15m(INDEX_TICKERS[market])
    if df.empty:
        return None
    today = df["session"].iloc[-1]
    gun = df[df["session"] == today]
    if gun.empty:
        return None
    acilis = float(gun.iloc[0]["open"])
    if acilis <= 0:
        return None
    return (float(gun.iloc[-1]["close"]) - acilis) / acilis * 100


def _in_trigger_window(now_ist=None) -> bool:
    now_ist = now_ist or datetime.now(ZoneInfo("Europe/Istanbul"))
    if now_ist.weekday() >= 5:
        return False
    m = now_ist.hour * 60 + now_ist.minute
    start = TRIGGER_WINDOW_START[0] * 60 + TRIGGER_WINDOW_START[1]
    end = TRIGGER_WINDOW_END[0] * 60 + TRIGGER_WINDOW_END[1]
    return start <= m < end


def scan(market: str = "BIST", force: bool = False):
    """YENİ MİMARİ (2026-08-13): canlı sinyal İNDİKATÖR PUANLAMASIYLA
    üretiliyor (en az INDICATOR_SCORE_MIN/4 şart), günün en yüksek puanlı
    en fazla MAX_SIGNALS_PER_DAY hissesi gönderiliyor, sabit 1:2 R:R
    (TP/SL/timeout) ile takibe alınıyor. AI modeli GÖLGE MODDA - taranan
    HER hisse için tahmin üretip ai_shadow_log.csv'ye yazıyor, hiçbir
    Telegram mesajı göndermiyor, hiçbir seçimi etkilemiyor.
    force=True: zaman penceresini atlar (/og_test için)."""
    if not OVERNIGHT_RADAR_ENABLED:
        return
    if market != "BIST":
        print(f"[OVERNIGHT] {market} desteklenmiyor - şimdilik sadece BIST.", flush=True)
        return
    if not force and not _in_trigger_window():
        return

    index_pct = _get_index_today_pct(market)
    taranan = 0
    adaylar = []  # (skor, ticker, ham) - indikator sartini gecenler

    for ticker in MARKET_TICKERS[market]:
        try:
            feats, ham = build_live_features(ticker, market, index_pct)
            if feats is None:
                continue
            taranan += 1

            indikator_skor = _indicator_score(ham)
            indikator_sinyal = indikator_skor >= INDICATOR_SCORE_MIN

            # AI GÖLGE MODU - HER taranan hisse icin (secim yanliligi
            # olmasin diye), Telegram'a gitmez, hicbir seyi etkilemez.
            if AI_SHADOW_ENABLED and _MODEL_AVAILABLE:
                try:
                    X = pd.DataFrame([[feats[c] for c in FEATURE_COLUMNS]], columns=FEATURE_COLUMNS)
                    proba = float(_model.predict_proba(X)[0][1])
                    _log_shadow(ticker, proba, indikator_skor, indikator_sinyal, feats, ham)
                except Exception as e:
                    print(f"[OVERNIGHT-GÖLGE] {ticker}: {e}", flush=True)

            if indikator_sinyal:
                adaylar.append((indikator_skor, ham["cmf"], ticker, ham))
        except Exception as e:
            print(f"[OVERNIGHT] {market} {ticker}: hata - {e}", flush=True)
            continue

    # En yuksek puanlilardan en fazla MAX_SIGNALS_PER_DAY tanesi (esitlikte CMF'e gore)
    adaylar.sort(key=lambda x: (x[0], x[1]), reverse=True)
    secilenler = adaylar[:MAX_SIGNALS_PER_DAY]

    for indikator_skor, _cmf, ticker, ham in secilenler:
        entry_price = ham["fiyat"]
        tp_price = entry_price * (1 + TP_PCT / 100)
        sl_price = entry_price * (1 - SL_PCT / 100)
        _log_signal(market, ticker, indikator_skor, ham, tp_price, sl_price)
        katalizor_satiri = "🟢 KAP katalizörü VAR" if ham["has_catalyst"] else "⚪ Katalizör yok"
        send_telegram_message(
            f"🌙 [GECE RADAR — İNDİKATÖR] {ticker}\n"
            f"Puan: {indikator_skor}/4\n"
            f"Kapanış: {entry_price:.2f} | Gün içi: {ham['pct_change']:+.2f}%\n"
            f"Hacim Faktörü: {ham['volume_factor']:.2f}x | RSI14: {ham['rsi14']:.1f}\n"
            f"CMF: {ham['cmf']:+.3f} | Kapanış-zirve oranı: {ham['close_to_high_ratio']:.2f}\n"
            f"{katalizor_satiri}\n"
            f"🎯 TP: {tp_price:.2f} (+%{TP_PCT:.1f}) | 🛑 SL: {sl_price:.2f} (-%{SL_PCT:.1f})\n"
            f"⏱️ Timeout: ertesi gün 12:00'de (ne TP ne SL vurulmazsa)\n\n"
            "⚠️ Sinyal amaçlıdır — emir talimatı değildir. /liste ile takip edilebilir."
        )
        print(f"[OVERNIGHT] {market} SİNYAL: {ticker} puan={indikator_skor}", flush=True)

    print(f"[OVERNIGHT] {market}: tur bitti, {taranan} hisse tarandı, "
          f"{len(adaylar)} aday, {len(secilenler)} sinyal gönderildi", flush=True)


_scanned_today = None  # tarih string - ayni gun tekrar tetiklenmesin


def maybe_scan(market: str = "BIST"):
    global _scanned_today
    if not OVERNIGHT_RADAR_ENABLED:
        return
    today = datetime.now(ZoneInfo("Europe/Istanbul")).date().isoformat()
    if _scanned_today == today:
        return
    if not _in_trigger_window():
        return
    scan(market)
    _scanned_today = today


# =============================================================================
# CANLI SİNYAL SONUÇ TAKİBİ — gerçek 1:2 R:R (TP/SL/timeout), her çağrıda
# açık pozisyonların bar-bar durumuna bakar (check_exit_alerts ile ayni
# felsefe: sabit hedef/stop varsa, ulaşılıp ulaşılmadığını sık kontrol et).
# =============================================================================

def check_and_update_results():
    rows = _read_signals()
    if not rows:
        return
    now_ist = datetime.now(ZoneInfo("Europe/Istanbul"))
    changed = False
    for r in rows:
        if r["result"] != "PENDING":
            continue
        try:
            created_ist = datetime.fromisoformat(r["created_at"]).astimezone(ZoneInfo("Europe/Istanbul"))
            signal_day = created_ist.date()
            if now_ist.date() <= signal_day:
                continue  # ertesi is gunu henuz gelmedi

            ticker = r["symbol"]
            entry_price = float(r["entry_price"])
            tp_price = float(r["tp_price"])
            sl_price = float(r["sl_price"])
            df = _fetch_15m(ticker, period="5d")
            if df.empty:
                continue
            gunler = sorted(df["session"].unique())
            sonraki_gunler = [g for g in gunler if g > signal_day]
            if not sonraki_gunler:
                continue
            hedef_gun = sonraki_gunler[0]

            hedef_bar = df[df["session"] == hedef_gun].copy()
            if hedef_bar.empty:
                continue
            hedef_bar["dakika"] = pd.to_datetime(hedef_bar["ts"]).dt.hour * 60 + pd.to_datetime(hedef_bar["ts"]).dt.minute
            pencere = hedef_bar[
                (hedef_bar["dakika"] >= NEXT_DAY_CHECK_WINDOW[0][0] * 60 + NEXT_DAY_CHECK_WINDOW[0][1]) &
                (hedef_bar["dakika"] < NEXT_DAY_CHECK_WINDOW[1][0] * 60 + NEXT_DAY_CHECK_WINDOW[1][1])
            ].sort_values("ts")
            if pencere.empty:
                continue

            # Bar bar yuru: TP/SL hangisi once vuruldu? Ayni barda ikisi de
            # olursa KAYIP (bu projenin turnuva konvansiyonuyla tutarli).
            sonuc = None
            for _, bar in pencere.iterrows():
                sl_hit = bar["low"] <= sl_price
                tp_hit = bar["high"] >= tp_price
                if sl_hit:
                    sonuc = "FAIL"
                    break
                if tp_hit:
                    sonuc = "SUCCESS"
                    break

            pencere_bitti = (hedef_gun < now_ist.date()) or (
                hedef_gun == now_ist.date() and
                now_ist.hour * 60 + now_ist.minute >= NEXT_DAY_CHECK_WINDOW[1][0] * 60 + NEXT_DAY_CHECK_WINDOW[1][1]
            )
            if sonuc is None:
                if not pencere_bitti:
                    continue  # hala pencere icinde, TP/SL henuz vurulmadi - bekle
                # timeout - pencere sonu fiyatiyla kapat
                sonuc = "TIMEOUT"
                son_fiyat = float(pencere.iloc[-1]["close"])
            else:
                son_fiyat = tp_price if sonuc == "SUCCESS" else sl_price

            degisim_pct = (son_fiyat - entry_price) / entry_price * 100
            r["result"] = sonuc
            r["checked_at"] = datetime.now(timezone.utc).isoformat()
            r["exit_price"] = round(son_fiyat, 4)
            if sonuc == "SUCCESS":
                r["r_multiple"] = round(TP_PCT / SL_PCT, 3)
            elif sonuc == "FAIL":
                r["r_multiple"] = -1.0
            else:
                r["r_multiple"] = round(degisim_pct / SL_PCT, 3)
            changed = True
            print(f"[OVERNIGHT] {ticker} sonuçlandı: {sonuc} ({degisim_pct:+.2f}%, R={r['r_multiple']})", flush=True)
        except Exception as e:
            print(f"[OVERNIGHT] Sonuç güncelleme hatası ({r.get('symbol')}): {e}", flush=True)
    if changed:
        _write_signals(rows)


def check_shadow_outcomes():
    """AI golge log'undaki HER satirin (secilsin ya da secilmesin) gercek
    sonucunu doldurur - SUCCESS_TARGET_PCT (+%2, ertesi gun ilk 2 saat max)
    tanimiyla. Secim yanliligi OLMADAN (sadece secilenler degil TARANAN
    HERKES) modelin gercek performansini olcmek icin - overnight_model_lab.py
    bu dosyayi okuyup yeniden egitim/karsilastirma yapiyor."""
    rows = _read_rows(AI_SHADOW_LOG_FILE)
    if not rows:
        return
    now_ist = datetime.now(ZoneInfo("Europe/Istanbul"))
    changed = False
    for r in rows:
        if r["result"] != "PENDING":
            continue
        try:
            created_ist = datetime.fromisoformat(r["created_at"]).astimezone(ZoneInfo("Europe/Istanbul"))
            signal_day = created_ist.date()
            if now_ist.date() <= signal_day:
                continue

            ticker = r["symbol"]
            entry_price = float(r["entry_price"])
            df = _fetch_15m(ticker, period="5d")
            if df.empty:
                continue
            gunler = sorted(df["session"].unique())
            sonraki_gunler = [g for g in gunler if g > signal_day]
            if not sonraki_gunler:
                continue
            hedef_gun = sonraki_gunler[0]

            hedef_bar = df[df["session"] == hedef_gun].copy()
            if hedef_bar.empty:
                continue
            hedef_bar["dakika"] = pd.to_datetime(hedef_bar["ts"]).dt.hour * 60 + pd.to_datetime(hedef_bar["ts"]).dt.minute
            pencere = hedef_bar[
                (hedef_bar["dakika"] >= NEXT_DAY_CHECK_WINDOW[0][0] * 60 + NEXT_DAY_CHECK_WINDOW[0][1]) &
                (hedef_bar["dakika"] < NEXT_DAY_CHECK_WINDOW[1][0] * 60 + NEXT_DAY_CHECK_WINDOW[1][1])
            ]
            gecti = (hedef_gun < now_ist.date()) or (
                hedef_gun == now_ist.date() and
                now_ist.hour * 60 + now_ist.minute >= NEXT_DAY_CHECK_WINDOW[1][0] * 60 + NEXT_DAY_CHECK_WINDOW[1][1]
            )
            if not gecti or pencere.empty:
                continue

            en_yuksek = float(pencere["high"].max())
            degisim_pct = (en_yuksek - entry_price) / entry_price * 100
            r["result"] = "SUCCESS" if degisim_pct >= 2.0 else "FAIL"
            r["gerceklesen_pct"] = round(degisim_pct, 2)
            r["checked_at"] = datetime.now(timezone.utc).isoformat()
            changed = True
        except Exception as e:
            print(f"[OVERNIGHT-GÖLGE] Sonuç güncelleme hatası ({r.get('symbol')}): {e}", flush=True)
    if changed:
        _write_rows(AI_SHADOW_LOG_FILE, AI_SHADOW_FIELDS, rows)


def build_overnight_report() -> str:
    rows = _read_signals()
    if not rows:
        return "🌙 [GECE RADAR] Henüz sinyal yok."
    n = len(rows)
    closed = [r for r in rows if r["result"] in ("SUCCESS", "FAIL", "TIMEOUT")]
    success = [r for r in closed if r["result"] == "SUCCESS"]
    lines = [f"🌙 [GECE RADAR RAPORU — İNDİKATÖR TABANLI]",
             f"Toplam sinyal: {n} (kapanan {len(closed)}, bekleyen {n - len(closed)})"]
    if closed:
        oran = len(success) / len(closed) * 100
        lines.append(f"TP oranı: %{oran:.1f} ({len(success)}/{len(closed)})")
        r_degerleri = [float(r["r_multiple"]) for r in closed if r.get("r_multiple") not in (None, "")]
        if r_degerleri:
            lines.append(f"Net beklenti: {sum(r_degerleri)/len(r_degerleri):+.3f}R "
                         f"({len(r_degerleri)} kapanan sinyal)")
    return "\n".join(lines)


def build_shadow_report() -> str:
    """AI golge modunun rapor edilmesi - /ai_golge komutu icin. Secilen ve
    secilmeyenleri ayri gosterir, secim yanliligi olmadan modelin gercek
    performansini gormek icin."""
    rows = _read_rows(AI_SHADOW_LOG_FILE)
    if not rows:
        return "🔬 [AI GÖLGE MODU] Henüz veri yok."
    n = len(rows)
    closed = [r for r in rows if r["result"] in ("SUCCESS", "FAIL")]
    lines = [f"🔬 [AI GÖLGE MODU RAPORU]", f"Toplam gözlem: {n} (kapanan {len(closed)})"]
    if closed:
        basarili = [r for r in closed if r["result"] == "SUCCESS"]
        oran = len(basarili) / len(closed) * 100
        lines.append(f"Genel başarı oranı (+%2 hedefi): %{oran:.1f} ({len(basarili)}/{len(closed)})")

        secilen = [r for r in closed if r["secildi_mi"] in ("1", 1, "True", True)]
        secilmeyen = [r for r in closed if r not in secilen]
        if secilen:
            b1 = sum(1 for r in secilen if r["result"] == "SUCCESS") / len(secilen) * 100
            lines.append(f"  İndikatör de seçmişti ({len(secilen)}): %{b1:.1f} başarı")
        if secilmeyen:
            b2 = sum(1 for r in secilmeyen if r["result"] == "SUCCESS") / len(secilmeyen) * 100
            lines.append(f"  İndikatör seçmemişti ({len(secilmeyen)}): %{b2:.1f} başarı")

        # yuksek AI skor esigi >= 0.6 olanlarin performansi (gercek kenar var mi diye)
        yuksek_skor = [r for r in closed if float(r["ai_score"]) >= 0.6]
        if yuksek_skor:
            b3 = sum(1 for r in yuksek_skor if r["result"] == "SUCCESS") / len(yuksek_skor) * 100
            lines.append(f"  AI skoru ≥%60 olanlar ({len(yuksek_skor)}): %{b3:.1f} başarı")
    lines.append("\nBu model canlıya hiç sinyal göndermiyor, sadece gözlemleniyor. "
                 "Yeterli örneklemde tutarlı pozitif kenar görülürse tekrar değerlendirilir.")
    return "\n".join(lines)
