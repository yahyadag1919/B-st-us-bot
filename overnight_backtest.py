"""
overnight_backtest.py — GERÇEK GERİYE DÖNÜK TEST (walk-forward backtest)
==========================================================================
Soru: "Bu sistemi 60 gün önce kursaydık, bugüne kadar ne kadar isabetli
olurdu?" Bunun cevabı — istatistik/otopsi değil, MODELİN KENDİSİNİ
(overnight_model.pkl) her gün için o günde bilinebilecek veriyle
çalıştırıp gerçek predict_proba skorunu üretmek ve gerçek sonucu kontrol
etmek.

LOOKAHEAD DİSİPLİNİ (bu projenin standart kuralı, radar_onculu_test.py'den
beri): gün i için feature'lar SADECE gün i'nin kendi kapanışına kadarki
veriyle hesaplanır. Sonuç kontrolü SADECE gün i+1'in verisiyle yapılır.
Hiçbir feature ileri bakmaz.

NEDEN GÜNLÜK BAR + 15DK KARIŞIK KULLANILDI:
- yfinance 15 dakikalık veriyi sadece SON ~60 GÜN için veriyor (proje
  boyunca defalarca karşılaşılan kısıt). 60 günlük test + ~30 günlük
  feature geriye-bakış tamponu birleşince 90 günlük 15dk veri gerekirdi,
  bu yfinance'de YOK.
- Çözüm: feature'lar GÜNLÜK barlarla hesaplanıyor (sınırsız geçmiş var,
  hacim oranı/RSI/CMF/kapanış-zirve oranı bu granülaritede de anlamlı).
  Sonuç kontrolü (ertesi gün ilk 2 saat) İSE 15 dakikalık veriyle
  yapılıyor - bu sadece son 60 güne bakıyor, sınıra takılmıyor.
- Bu, canlı radarın (17:45-17:55, 15dk bar) BİREBİR AYNISI DEĞİL, günlük
  bazda bir YAKLAŞIKLAMASI. Sonuçlar canlıdakiyle küçük farklar
  gösterebilir - ama yön/büyüklük tahmini için yeterince sağlam.

has_catalyst SINIRLAMASI: kap_monitor.py birkaç gün önce kurulmaya
başladı, 60 gün öncesine ait KAP kaydı YOK. Backtest'te bu feature HER
ZAMAN 0 - gerçek performans, KAP katalizörlü günlerde muhtemelen daha
iyi olurdu. Bu dürüstçe raporun içinde de belirtiliyor.

pandas_ta KULLANILMADI (numpy>=1.24 uyumsuzluğu, proje boyunca tespit
edildi) - indikatörler elle yazıldı.
"""

import os
import ast
import csv
import time
import warnings
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd
import requests
import yfinance as yf

warnings.filterwarnings("ignore")

# =============================================================================
# AYARLAR
# =============================================================================

BACKTEST_DAYS = int(os.environ.get("BACKTEST_DAYS", "60"))
FEATURE_LOOKBACK_BUFFER = 30  # vol_ma20/rsi/cmf icin gereken minimum gecmis
DAILY_FETCH_PERIOD = f"{BACKTEST_DAYS + FEATURE_LOOKBACK_BUFFER + 10}d"

MODEL_PATH = os.environ.get("OVERNIGHT_MODEL_PATH", "overnight_model.pkl")
FEATURE_COLUMNS = ["volume_factor", "rsi", "price_change_pct", "gap_pct", "cmf",
                    "has_catalyst", "close_to_high_ratio"]
AI_SCORE_THRESHOLD = float(os.environ.get("OVERNIGHT_AI_SCORE_THRESHOLD", "0.60"))
SUCCESS_TARGET_PCT = float(os.environ.get("ML_SUCCESS_TARGET_PCT", "2.0"))
NEXT_DAY_WINDOW = ((10, 0), (12, 0))  # ertesi gunun ilk 2 saati, Istanbul

TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN", "")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")

OUTPUT_CSV = "overnight_backtest_results.csv"


def send_telegram_message(text: str):
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        print(f"[BİLGİ] Telegram ayarlı değil:\n{text}")
        return
    try:
        requests.post(
            f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage",
            json={"chat_id": TELEGRAM_CHAT_ID, "text": text[:4000]}, timeout=20)
    except Exception as e:
        print(f"[HATA] Telegram gönderilemedi: {e}")


def _load_tickers_from_bot_file(path="stock_screener_bot.py"):
    """Import ETMEDEN, statik AST okuma - historical_autopsy.py'de bulunan
    import-yan-etkisi hatasindan ders (proje standardi)."""
    try:
        with open(path, encoding="utf-8") as f:
            kaynak = f.read()
        agac = ast.parse(kaynak)
        for node in ast.walk(agac):
            if isinstance(node, ast.Assign) and len(node.targets) == 1:
                hedef = node.targets[0]
                if isinstance(hedef, ast.Name) and hedef.id == "BIST_TICKERS":
                    if isinstance(node.value, ast.List):
                        degerler = [el.value for el in node.value.elts
                                    if isinstance(el, ast.Constant) and isinstance(el.value, str)]
                        if degerler:
                            return degerler
    except Exception as e:
        print(f"[BİLGİ] Ticker listesi okunamadı: {e}")
    return None


BIST_TICKERS = _load_tickers_from_bot_file() or [
    "THYAO.IS", "ASELS.IS", "SISE.IS", "GARAN.IS", "AKBNK.IS", "EREGL.IS",
    "BIMAS.IS", "TUPRS.IS", "SAHOL.IS", "KCHOL.IS",
]


# =============================================================================
# İNDİKATÖRLER (pandas_ta yok, elle yazıldı)
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


# =============================================================================
# GÜNLÜK VERİ + FEATURE SERİSİ (T-1 disiplinli: her satır SADECE o güne
# kadarki veriyle hesaplanmış)
# =============================================================================

def fetch_daily_with_features(ticker: str) -> pd.DataFrame:
    try:
        df = yf.Ticker(ticker).history(period=DAILY_FETCH_PERIOD, interval="1d")
    except Exception as e:
        print(f"[HATA] {ticker} günlük veri alınamadı: {e}")
        return pd.DataFrame()
    if df is None or df.empty or len(df) < FEATURE_LOOKBACK_BUFFER + 5:
        return pd.DataFrame()
    df = df.rename(columns={"Open": "open", "High": "high", "Low": "low",
                             "Close": "close", "Volume": "volume"})
    df = df[["open", "high", "low", "close", "volume"]].copy()
    df.index = pd.to_datetime(df.index).tz_localize(None)

    df["prev_close"] = df["close"].shift(1)
    df["price_change_pct"] = (df["close"] - df["prev_close"]) / df["prev_close"] * 100
    df["gap_pct"] = (df["open"] - df["prev_close"]) / df["prev_close"] * 100
    df["vol_ma20"] = df["volume"].rolling(20).mean()
    df["volume_factor"] = df["volume"] / df["vol_ma20"]
    df["rsi"] = _rsi(df["close"], 14)
    df["cmf"] = _cmf(df["high"], df["low"], df["close"], df["volume"])
    df["close_to_high_ratio"] = (df["close"] - df["low"]) / (df["high"] - df["low"]).replace(0, np.nan)
    df["has_catalyst"] = 0  # gecmis KAP verisi yok - bilinen sinirlama
    return df


def _get_next_day_first2h_range(ticker: str, gun_tarihi):
    """Ertesi is gununun 10:00-12:00 Istanbul penceresindeki en yuksek,
    en dusuk ve pencere-sonu (12:00'a en yakin bar) fiyatlarini dondurur:
    (max_high, min_low, pencere_sonu_close). 15dk veriden - bu tarih araligi
    (son ~60 gun) yfinance'in 15dk sinirinin icinde oldugu icin calisir.
    Bulamazsa (None, None, None) doner.
    radar_canli.py'deki max_up/max_down/session_end raporlama tarzinin ayni
    mantigi: sadece 'hedefe ulasti mi' degil, basarisiz sinyallerin ne kadar
    dustugu de gorunsun diye eklendi."""
    try:
        df15 = yf.Ticker(ticker).history(period="60d", interval="15m")
        if df15 is None or df15.empty:
            return None, None, None
        df15 = df15.reset_index().rename(columns={"Datetime": "ts", "High": "high", "Low": "low", "Close": "close"})
        df15["ts"] = pd.to_datetime(df15["ts"])
        if df15["ts"].dt.tz is not None:
            df15["ts"] = df15["ts"].dt.tz_convert("Europe/Istanbul")
        else:
            df15["ts"] = df15["ts"].dt.tz_localize("Europe/Istanbul")
        df15["tarih"] = df15["ts"].dt.date

        sonraki_gunler = sorted(d for d in df15["tarih"].unique() if d > gun_tarihi)
        if not sonraki_gunler:
            return None, None, None
        ertesi_gun = sonraki_gunler[0]

        pencere = df15[
            (df15["tarih"] == ertesi_gun) &
            (df15["ts"].dt.hour * 60 + df15["ts"].dt.minute >= NEXT_DAY_WINDOW[0][0] * 60 + NEXT_DAY_WINDOW[0][1]) &
            (df15["ts"].dt.hour * 60 + df15["ts"].dt.minute <= NEXT_DAY_WINDOW[1][0] * 60 + NEXT_DAY_WINDOW[1][1])
        ]
        if pencere.empty:
            return None, None, None
        return float(pencere["high"].max()), float(pencere["low"].min()), float(pencere.iloc[-1]["close"])
    except Exception as e:
        print(f"[HATA] {ticker} ertesi gün kontrolü: {e}")
        return None, None, None


# =============================================================================
# WALK-FORWARD SİMÜLASYON
# =============================================================================

def backtest_ticker(ticker: str, model) -> list:
    df = fetch_daily_with_features(ticker)
    if df.empty:
        return []

    sonuclar = []
    test_baslangic = max(FEATURE_LOOKBACK_BUFFER, len(df) - BACKTEST_DAYS - 1)
    for i in range(test_baslangic, len(df) - 1):  # son satir icin ertesi gun yok, atla
        row = df.iloc[i]
        gun_tarihi = df.index[i].date()

        feats = {c: row[c] for c in FEATURE_COLUMNS}
        if any(pd.isna(v) for v in feats.values()):
            continue

        X = pd.DataFrame([[feats[c] for c in FEATURE_COLUMNS]], columns=FEATURE_COLUMNS)
        try:
            proba = float(model.predict_proba(X)[0][1])
        except Exception as e:
            print(f"[HATA] {ticker} {gun_tarihi} predict_proba: {e}")
            continue

        sinyal_var = proba >= AI_SCORE_THRESHOLD
        if not sinyal_var:
            continue  # sadece gercekten sinyal ureteceginiz gunler kaydediliyor

        entry_price = float(row["close"])
        max_high, min_low, pencere_sonu = _get_next_day_first2h_range(ticker, gun_tarihi)
        if max_high is None:
            continue  # 15dk veri yoksa (60 gunden eski) sonuc bilinemez, atla

        max_pct = (max_high - entry_price) / entry_price * 100
        min_pct = (min_low - entry_price) / entry_price * 100
        sonu_pct = (pencere_sonu - entry_price) / entry_price * 100
        basarili = max_pct >= SUCCESS_TARGET_PCT

        sonuclar.append({
            "ticker": ticker, "tarih": gun_tarihi.isoformat(), "ai_skor": round(proba, 4),
            "entry_price": round(entry_price, 4), "ertesi_gun_max_pct": round(max_pct, 2),
            "ertesi_gun_min_pct": round(min_pct, 2), "ertesi_gun_sonu_pct": round(sonu_pct, 2),
            "basarili": basarili,
        })
    return sonuclar


def run_backtest():
    print(f"[{datetime.now().isoformat()}] Geriye dönük test başlıyor — "
          f"{BACKTEST_DAYS} gün, {len(BIST_TICKERS)} hisse, eşik %{AI_SCORE_THRESHOLD*100:.0f}", flush=True)

    try:
        import joblib
        model = joblib.load(MODEL_PATH)
    except Exception as e:
        mesaj = f"❌ [BACKTEST BAŞARISIZ] {MODEL_PATH} yüklenemedi: {e}"
        print(mesaj, flush=True)
        send_telegram_message(mesaj)
        return

    tum_sonuclar = []
    for n, ticker in enumerate(BIST_TICKERS, 1):
        r = backtest_ticker(ticker, model)
        if r:
            print(f"[{n}/{len(BIST_TICKERS)}] {ticker}: {len(r)} sinyal bulundu", flush=True)
        tum_sonuclar.extend(r)
        time.sleep(0.3)

    if not tum_sonuclar:
        mesaj = ("📊 [BACKTEST SONUÇ] 60 günde hiç sinyal üretilmedi.\n"
                  "Eşik çok yüksek olabilir ya da modelin gördüğü koşullar bu dönemde oluşmadı.")
        print(mesaj, flush=True)
        send_telegram_message(mesaj)
        return

    df_sonuc = pd.DataFrame(tum_sonuclar)
    df_sonuc.to_csv(OUTPUT_CSV, index=False, encoding="utf-8-sig")

    n = len(df_sonuc)
    basarili_n = int(df_sonuc["basarili"].sum())
    oran = basarili_n / n * 100

    basarili_grup = df_sonuc[df_sonuc["basarili"]]
    basarisiz_grup = df_sonuc[~df_sonuc["basarili"]]

    mesaj = (
        f"📊 [{BACKTEST_DAYS} GÜNLÜK GERİYE DÖNÜK TEST SONUCU]\n"
        f"'Bu sistemi {BACKTEST_DAYS} gün önce kursaydık ne olurdu?'\n\n"
        f"Toplam sinyal: {n}\n"
        f"Başarılı (+%{SUCCESS_TARGET_PCT:.1f} hedefine ulaşan): {basarili_n} (%{oran:.1f})\n\n"
    )

    if not basarili_grup.empty:
        mesaj += (
            f"✅ Başarılı grup ({len(basarili_grup)}):\n"
            f"  Ort. max: {basarili_grup['ertesi_gun_max_pct'].mean():+.2f}% | "
            f"Ort. pencere sonu: {basarili_grup['ertesi_gun_sonu_pct'].mean():+.2f}%\n"
        )
    if not basarisiz_grup.empty:
        mesaj += (
            f"❌ Başarısız grup ({len(basarisiz_grup)}) — HEDEFE ULAŞMAYANLARDA NE OLDU:\n"
            f"  Ort. min (en kötü an): {basarisiz_grup['ertesi_gun_min_pct'].mean():+.2f}% | "
            f"En kötü tekil: {basarisiz_grup['ertesi_gun_min_pct'].min():+.2f}%\n"
            f"  Ort. pencere sonu: {basarisiz_grup['ertesi_gun_sonu_pct'].mean():+.2f}% | "
            f"Sonu eksi bitenler: %{(basarisiz_grup['ertesi_gun_sonu_pct'] < 0).mean()*100:.1f}\n"
        )

    mesaj += (
        f"\n⚠️ has_catalyst bu testte HER ZAMAN 0 — KAP verisi henüz bu kadar "
        f"geriye gitmiyor, gerçek performans muhtemelen biraz daha iyi olurdu.\n"
        f"⚠️ Feature'lar günlük barla hesaplandı (canlı radar 15dk kullanıyor) "
        f"— yaklaşık sonuç, birebir aynısı değil.\n"
        f"⚠️ 'Başarılı' ölçütü penceredeki EN YÜKSEK fiyata göre — iyimser üst "
        f"sınır. Başarısız grubun pencere-sonu ortalaması, gerçekte elde "
        f"tutulsaydı ne olacağına dair daha gerçekçi bir fikir verir."
    )
    print(mesaj, flush=True)
    send_telegram_message(mesaj)
    print(f"[KAYDEDİLDİ] {OUTPUT_CSV} — {n} satır", flush=True)


if __name__ == "__main__":
    import threading
    from http.server import HTTPServer, BaseHTTPRequestHandler

    class _Health(BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b"OK")

        def log_message(self, *a):
            pass

    def _start_health():
        port = int(os.environ.get("PORT", 10000))
        HTTPServer(("0.0.0.0", port), _Health).serve_forever()

    threading.Thread(target=_start_health, daemon=True).start()
    run_backtest()
    print("\n[BİTTİ] Backtest tamamlandı. Start Command'i 'python main.py'ye "
          "geri çevirebilirsin.", flush=True)
    while True:
        time.sleep(3600)
