"""
overnight_radar.py — GECE AI RADAR (ERTESİ GÜN AÇILIŞ/GAP MODELİ, İZOLE)
===========================================================================
İkinci AI modülü — ml_radar.py'nin (gün içi model) mimarisini temel alır
ama farklı bir soruya cevap arar: "bugünkü kapanış barının parmak izine
bakarak, ERTESİ GÜNÜN İLK 2 SAATİNDE +%2 potansiyeli var mı?"

ÇALIŞMA ZAMANI: Her BIST işlem günü kapanışa doğru, 17:45–17:55 (İstanbul)
arası bir kez tetiklenir — spesifikasyonda belirtilen pencere. SADECE
BIST için (spesifikasyon tek bir kapanış-saati penceresi verdi, ABD'nin
kendi kapanışı farklı saatte olduğundan ABD şimdilik kapsam dışı — istenirse
~22:00 İstanbul (16:00 New York sonrası) için ayrı bir pencere eklenebilir).

FEATURE SIRASI (2026-08-11, kullanıcı Colab eğitimini teyit etti):
volume_factor, rsi, price_change_pct, gap_pct, cmf, has_catalyst,
close_to_high_ratio (7. feature — kapanışın günün en yükseğine yakınlığı,
0-1 arası, historical_autopsy.py'deki close_to_high_pct ile aynı formül
ama 0-1 ölçeğinde: (kapanış-düşük)/(yüksek-düşük)).
has_catalyst SADECE BIST için kap_monitor.py'nin verisinden dolduruluyor.

SONUÇ TAKİBİ farklı çalışıyor (ml_radar.py'den): "48 saat sonra fiyata
bak" değil, "ERTESİ İŞ GÜNÜNÜN İLK 2 SAATİ (10:00-12:00 İstanbul) içindeki
en yüksek fiyat +%2'ye ulaştı mı" — hedefin tanımına birebir uysun diye.

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
OVERNIGHT_UNUSED_INTERVAL = int(os.environ.get("OVERNIGHT_UNUSED_INTERVAL", "15"))
# 2026-08-12: 0.80 -> 0.60 dusuruldu (Gemini talebi - haber katalizoru
# olmayan ama fiyat/hacim/CMF'i guclu tahtalari da yakalasin diye). Not:
# has_catalyst ve close_to_high_ratio bu kodda HICBIR ZAMAN sert filtre
# degildi - hep sadece modele giden feature'lardi (asagida FEATURE_COLUMNS).
# Onlari "esnetmek" aslinda bu tek esigi dusurmekle ayni sey - modelin
# kendisi bu feature'lara ne kadar agirlik verdigine karar veriyor.
AI_SCORE_THRESHOLD = float(os.environ.get("OVERNIGHT_AI_SCORE_THRESHOLD", "0.60"))

MODEL_PATH = os.environ.get("OVERNIGHT_MODEL_PATH", "overnight_model.pkl")

# Kullanıcının Colab eğitiminde kullandığı BİREBİR isim + sıra (2026-08-11 teyit edildi)
# overnight_model.pkl'nin 7. feature'ı var: close_to_high_ratio.
FEATURE_COLUMNS = ["volume_factor", "rsi", "price_change_pct", "gap_pct", "cmf",
                    "has_catalyst", "close_to_high_ratio"]

# Tetiklenme penceresi: BIST kapanışına doğru, İstanbul saatiyle
TRIGGER_WINDOW_START = (17, 45)
TRIGGER_WINDOW_END = (17, 55)

SUCCESS_TARGET_PCT = float(os.environ.get("OVERNIGHT_SUCCESS_TARGET_PCT", "2.0"))  # spesifikasyonla ayni: ertesi gun ilk 2 saatte +%2
# CHECK_WINDOW_HOURS KALDIRILDI - sonuc kontrolu artik "ertesi is gunu ilk 2 saat"
# penceresine gore yapiliyor, sabit saat sayisina gore degil (asagida check_and_update_results).
NEXT_DAY_CHECK_WINDOW = ((10, 0), (12, 0))  # BIST'in ilk 2 saati, Istanbul
KAP_MATCH_WINDOW_MINUTES = int(os.environ.get("OVERNIGHT_KAP_MATCH_WINDOW_MINUTES", "240"))  # has_catalyst icin, radar_canli.py ile ayni varsayilan

INDEX_TICKERS = {"BIST": "XU100.IS", "US": "SPY"}

SIGNAL_LOG_FILE = _data_path("overnight_radar_signals.csv")
SIGNAL_FIELDS = ["id", "created_at", "symbol", "market", "ai_score", "volume_factor",
                  "rsi", "pct_change", "gap_percent", "cmf", "has_catalyst",
                  "close_to_high_ratio", "entry_price", "checked_at", "result"]

_last_scan_time = {}
_model = None
_MODEL_AVAILABLE = False


# =============================================================================
# MODEL YÜKLEME (hata olsa bile ana sistemi düşürmez)
# =============================================================================

def _load_model():
    global _model, _MODEL_AVAILABLE
    try:
        import joblib
        _model = joblib.load(MODEL_PATH)
        _MODEL_AVAILABLE = True
        print(f"[OVERNIGHT] model.pkl yüklendi ({MODEL_PATH}).", flush=True)
    except Exception as e:
        _MODEL_AVAILABLE = False
        print(f"[OVERNIGHT] model.pkl YÜKLENEMEDİ - ML radar devre dışı: {e}", flush=True)


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
# YEREL CSV KAYIT (Supabase yerine — daha basit, yeni hesap gerektirmez)
# =============================================================================

def _next_id() -> int:
    rows = _read_signals()
    return (max((int(r["id"]) for r in rows), default=0) + 1) if rows else 1


def _append_signal(row: dict):
    exists = os.path.exists(SIGNAL_LOG_FILE)
    with open(SIGNAL_LOG_FILE, "a", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=SIGNAL_FIELDS)
        if not exists:
            w.writeheader()
        w.writerow(row)


def _read_signals():
    if not os.path.exists(SIGNAL_LOG_FILE):
        return []
    with open(SIGNAL_LOG_FILE, newline="", encoding="utf-8") as f:
        return list(csv.DictReader(f))


def _write_signals(rows):
    with open(SIGNAL_LOG_FILE, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=SIGNAL_FIELDS)
        w.writeheader()
        w.writerows(rows)


def _log_signal(market, ticker, ai_score, ham):
    row = {
        "id": _next_id(), "created_at": datetime.now(timezone.utc).isoformat(),
        "symbol": ticker, "market": market, "ai_score": round(float(ai_score), 4),
        "volume_factor": ham["volume_factor"], "rsi": ham["rsi14"],
        "pct_change": ham["pct_change"], "gap_percent": ham["gap_percent"],
        "cmf": ham["cmf"], "has_catalyst": ham["has_catalyst"],
        "close_to_high_ratio": ham["close_to_high_ratio"],
        "entry_price": ham["fiyat"], "checked_at": "", "result": "PENDING",
    }
    _append_signal(row)


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


def scan(market: str = "BIST"):
    """Sadece BIST icin, sadece TRIGGER_WINDOW icinde (17:45-17:55 Istanbul)
    calisir - gunluk kapanisa dogru bir kerelik tarama. AI_SCORE_THRESHOLD
    (varsayilan %60, 2026-08-12'de %80'den dusuruldu) disinda ek filtre yok -
    has_catalyst/close_to_high_ratio hicbir zaman sert filtre olmadi, sadece
    modele giden feature'lar."""
    if not OVERNIGHT_RADAR_ENABLED or not _MODEL_AVAILABLE:
        return
    if market != "BIST":
        print(f"[OVERNIGHT] {market} desteklenmiyor - şimdilik sadece BIST.", flush=True)
        return
    if not _in_trigger_window():
        return

    index_pct = _get_index_today_pct(market)
    taranan, sinyal = 0, 0

    for ticker in MARKET_TICKERS[market]:
        try:
            feats, ham = build_live_features(ticker, market, index_pct)
            if feats is None:
                continue
            taranan += 1

            X = pd.DataFrame([[feats[c] for c in FEATURE_COLUMNS]], columns=FEATURE_COLUMNS)
            proba = float(_model.predict_proba(X)[0][1])  # 1 = basari sinifi varsayimi

            if proba < AI_SCORE_THRESHOLD:
                continue

            sinyal += 1
            _log_signal(market, ticker, proba, ham)
            katalizor_satiri = "🟢 KAP katalizörü VAR" if ham["has_catalyst"] else "⚪ Katalizör yok"
            send_telegram_message(
                f"🌙 [GECE AI RADAR] {ticker}\n"
                f"Güven Skoru: %{proba*100:.1f}\n"
                f"Kapanış: {ham['fiyat']:.2f} | Gün içi: {ham['pct_change']:+.2f}%\n"
                f"Hacim Faktörü: {ham['volume_factor']:.2f}x | RSI14: {ham['rsi14']:.1f}\n"
                f"Gap: {ham['gap_percent']:+.2f}% | CMF: {ham['cmf']:+.3f}\n"
                f"Kapanış-zirve oranı: {ham['close_to_high_ratio']:.2f}\n"
                f"{katalizor_satiri}\n\n"
                "⚠️ Model tahmini — ertesi günün ilk 2 saatinde +%2 potansiyeli "
                "öngörüyor. Emir talimatı değildir. /liste ile takip edilebilir."
            )
            print(f"[OVERNIGHT] {market} SİNYAL: {ticker} skor={proba:.3f}", flush=True)
        except Exception as e:
            print(f"[OVERNIGHT] {market} {ticker}: hata - {e}", flush=True)
            continue

    print(f"[OVERNIGHT] {market}: tur bitti, {taranan} hisse tarandı, {sinyal} sinyal", flush=True)


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
# SONUÇ TAKİBİ — PENDING sinyalleri SUCCESS/FAIL'e çevirir (yerel CSV'de)
# =============================================================================

def check_and_update_results():
    """ml_radar.py'den farkli: sabit saat sayisi yerine 'ertesi is gununun
    ilk 2 saati (10:00-12:00 Istanbul) icindeki en yuksek fiyat +%2'ye
    ulasti mi' sorusuna gore SUCCESS/FAIL veriyor - hedefin tanimina birebir
    uysun diye. Bu pencere henuz tamamlanmadiysa (bugun sinyal gunuyle ayni
    gunse ya da hala o sabahin icindeysek) dokunmuyor."""
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
            df = _fetch_15m(ticker, period="5d")
            if df.empty:
                continue
            # ertesi is gunu = sinyalden sonraki ilk oturum (df["session"] zaten tarih tipinde)
            gunler = sorted(df["session"].unique())
            sonraki_gunler = [g for g in gunler if g > signal_day]
            if not sonraki_gunler:
                continue
            hedef_gun = sonraki_gunler[0]

            hedef_bar = df[df["session"] == hedef_gun]
            if hedef_bar.empty:
                continue
            dakika = pd.to_datetime(hedef_bar["ts"]).dt.hour * 60 + pd.to_datetime(hedef_bar["ts"]).dt.minute
            pencere = hedef_bar[
                (dakika >= NEXT_DAY_CHECK_WINDOW[0][0] * 60 + NEXT_DAY_CHECK_WINDOW[0][1]) &
                (dakika < NEXT_DAY_CHECK_WINDOW[1][0] * 60 + NEXT_DAY_CHECK_WINDOW[1][1])
            ]
            # pencere henuz gecmediyse (bugun hedef gunse ve saat 12:00'i gecmediyse) bekle
            if hedef_gun == now_ist.date() and now_ist.hour * 60 + now_ist.minute < NEXT_DAY_CHECK_WINDOW[1][0] * 60 + NEXT_DAY_CHECK_WINDOW[1][1]:
                continue
            if pencere.empty:
                continue

            en_yuksek = float(pencere["high"].max())
            degisim_pct = (en_yuksek - entry_price) / entry_price * 100
            r["result"] = "SUCCESS" if degisim_pct >= SUCCESS_TARGET_PCT else "FAIL"
            r["checked_at"] = datetime.now(timezone.utc).isoformat()
            changed = True
            print(f"[OVERNIGHT] {ticker} sonuçlandı: {r['result']} (ilk 2 saat max {degisim_pct:+.2f}%)", flush=True)
        except Exception as e:
            print(f"[OVERNIGHT] Sonuç güncelleme hatası ({r.get('symbol')}): {e}", flush=True)
    if changed:
        _write_signals(rows)


def build_overnight_report() -> str:
    rows = _read_signals()
    if not rows:
        return "🌙 [GECE AI RADAR] Henüz sinyal yok."
    n = len(rows)
    closed = [r for r in rows if r["result"] in ("SUCCESS", "FAIL")]
    success = [r for r in closed if r["result"] == "SUCCESS"]
    lines = [f"🌙 [GECE AI RADAR RAPORU]", f"Toplam sinyal: {n} (kapanan {len(closed)}, bekleyen {n - len(closed)})"]
    if closed:
        oran = len(success) / len(closed) * 100
        lines.append(f"Başarı oranı: %{oran:.1f} ({len(success)}/{len(closed)})")
        skorlar = [float(r["ai_score"]) for r in closed]
        lines.append(f"Ortalama AI skoru (kapananlar): %{sum(skorlar)/len(skorlar)*100:.1f}")
    return "\n".join(lines)
