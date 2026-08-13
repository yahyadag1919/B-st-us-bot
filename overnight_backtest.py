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
NEXT_DAY_WINDOW = ((10, 0), (12, 0))  # ertesi gunun ilk 2 saati, Istanbul

# Rapor 27 (2026-08-12) - Gemini'nin istedigi genisletilmis giris havuzu:
# AI modeli VEYA asagidaki 4 teknik kosuldan en az INDICATOR_SCORE_MIN
# tanesini saglayan gunler de aday sayiliyor. "RSI dip tepkisi" acik
# tanimli degildi - 35-55 araligi (asiri satimdan toparlanma bolgesi)
# olarak yorumlandi, net degilse Gemini'ye sorulmasi gereken bir nokta.
INDICATOR_SCORE_MIN = int(os.environ.get("BACKTEST_INDICATOR_SCORE_MIN", "2"))
CMF_MIN = float(os.environ.get("BACKTEST_CMF_MIN", "0.10"))
VOLUME_FACTOR_MIN = float(os.environ.get("BACKTEST_VOLUME_FACTOR_MIN", "1.5"))
CLOSE_TO_HIGH_MIN = float(os.environ.get("BACKTEST_CLOSE_TO_HIGH_MIN", "0.7"))
RSI_DIP_MIN, RSI_DIP_MAX = 35.0, 55.0

# Rapor 27 - disiplinli 1:2 R:R cikis kurali (komisyon/slipaj DAHIL DEGIL)
TP_PCT = float(os.environ.get("BACKTEST_TP_PCT", "2.0"))
SL_PCT = float(os.environ.get("BACKTEST_SL_PCT", "1.0"))

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


def _indicator_score(row) -> tuple:
    """Modelden BAGIMSIZ, en az INDICATOR_SCORE_MIN kosulu saglayan gunleri
    de aday havuzuna almak icin (rapor 27). Esikler cevre degiskeniyle
    ayarlanabilir, varsayilanlar makul kabul edilen degerler - kesin
    dogrulanmis degil."""
    kosullar = {
        "cmf": bool(row["cmf"] > CMF_MIN),
        "hacim": bool(row["volume_factor"] >= VOLUME_FACTOR_MIN),
        "zirveye_yakin": bool(row["close_to_high_ratio"] >= CLOSE_TO_HIGH_MIN),
        "rsi_dip": bool(RSI_DIP_MIN <= row["rsi"] <= RSI_DIP_MAX),
    }
    return sum(kosullar.values()), kosullar


def _simulate_rr_exit(ticker: str, gun_tarihi, entry_price: float):
    """Ertesi gunun 10:00-12:00 penceresinde 15dk bar bar yururur: TP
    (+TP_PCT) mi once vurulur SL (-SL_PCT) mi mi bakar. AYNI BARDA IKISI
    DE tetiklenirse KAYIP sayilir - bu projenin turnuvalarindaki 'ayni
    bar stop+TP = kayip' konvansiyonuyla tutarlilik icin. Pencere
    bitene kadar hicbiri vurulmazsa TIMEOUT, son barin kapanisiyla R
    hesaplanir. Donen: (sonuc_tipi, r_multiple, gerceklesen_pct) -
    sonuc_tipi 'TP'/'SL'/'TIMEOUT', bulunamazsa (None, None, None)."""
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
        ].sort_values("ts")
        if pencere.empty:
            return None, None, None

        tp_price = entry_price * (1 + TP_PCT / 100)
        sl_price = entry_price * (1 - SL_PCT / 100)

        for _, bar in pencere.iterrows():
            tp_hit = bar["high"] >= tp_price
            sl_hit = bar["low"] <= sl_price
            if sl_hit:  # ayni barda ikisi de olsa dahi (tp_hit da True olsa) KAYIP - konservatif
                return "SL", -1.0, -SL_PCT
            if tp_hit:
                return "TP", TP_PCT / SL_PCT, TP_PCT

        son_close = float(pencere.iloc[-1]["close"])
        gerceklesen_pct = (son_close - entry_price) / entry_price * 100
        return "TIMEOUT", gerceklesen_pct / SL_PCT, gerceklesen_pct
    except Exception as e:
        print(f"[HATA] {ticker} R:R simülasyonu: {e}")
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

        indikator_skor, _ = _indicator_score(row)
        ai_sinyal = proba >= AI_SCORE_THRESHOLD
        indikator_sinyal = indikator_skor >= INDICATOR_SCORE_MIN

        if not (ai_sinyal or indikator_sinyal):
            continue  # ne AI ne indikator havuzuna girdi

        secim = "+".join(k for k, v in [("AI", ai_sinyal), ("INDIKATOR", indikator_sinyal)] if v)

        entry_price = float(row["close"])
        sonuc_tipi, r_multiple, gerceklesen_pct = _simulate_rr_exit(ticker, gun_tarihi, entry_price)
        if sonuc_tipi is None:
            continue  # 15dk veri yoksa (60 gunden eski) sonuc bilinemez, atla

        sonuclar.append({
            "ticker": ticker, "tarih": gun_tarihi.isoformat(), "ai_skor": round(proba, 4),
            "indikator_skor": indikator_skor, "secim_kaynagi": secim,
            "entry_price": round(entry_price, 4), "sonuc_tipi": sonuc_tipi,
            "r_multiple": round(r_multiple, 3), "gerceklesen_pct": round(gerceklesen_pct, 2),
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

    def _grup_ozet(grup: pd.DataFrame, baslik: str) -> str:
        if grup.empty:
            return f"{baslik}: sinyal yok\n"
        n = len(grup)
        tp_n = int((grup["sonuc_tipi"] == "TP").sum())
        sl_n = int((grup["sonuc_tipi"] == "SL").sum())
        to_n = int((grup["sonuc_tipi"] == "TIMEOUT").sum())
        win_rate = (grup["r_multiple"] > 0).mean() * 100
        expectancy = grup["r_multiple"].mean()
        return (
            f"{baslik} — {n} sinyal\n"
            f"  TP: {tp_n} | SL: {sl_n} | Timeout: {to_n}\n"
            f"  Kazanma oranı (R>0): %{win_rate:.1f}\n"
            f"  NET BEKLENTİ: {expectancy:+.3f}R\n"
        )

    tum_havuz = df_sonuc
    sadece_ai = df_sonuc[df_sonuc["secim_kaynagi"] == "AI"]
    sadece_indikator = df_sonuc[df_sonuc["secim_kaynagi"] == "INDIKATOR"]
    ikisi_de = df_sonuc[df_sonuc["secim_kaynagi"] == "AI+INDIKATOR"]

    mesaj1 = (
        f"📊 [{BACKTEST_DAYS} GÜNLÜK TEST — GENİŞLETİLMİŞ HAVUZ + 1:{TP_PCT/SL_PCT:.0f} R:R]\n"
        f"TP +%{TP_PCT:.1f} | SL -%{SL_PCT:.1f} | Timeout: pencere sonu (10:00-12:00)\n"
        f"İndikatör havuzu: CMF/Hacim/Zirveye yakınlık/RSI'dan en az {INDICATOR_SCORE_MIN}/4\n\n"
        f"🌐 TÜM HAVUZ (AI VEYA İndikatör)\n{_grup_ozet(tum_havuz, 'Toplam')}"
    )
    mesaj2 = (
        f"🤖 Sadece AI eşiği (%{AI_SCORE_THRESHOLD*100:.0f})\n{_grup_ozet(sadece_ai, 'AI-only')}\n"
        f"📐 Sadece İndikatör puanı\n{_grup_ozet(sadece_indikator, 'İndikatör-only')}\n"
        f"🎯 İkisi de aynı fikirde\n{_grup_ozet(ikisi_de, 'AI+İndikatör')}"
    )
    mesaj3 = (
        f"⚠️ Komisyon/slipaj DAHİL DEĞİL — gerçek net sonuç bu rakamlardan "
        f"biraz daha düşük olur.\n"
        f"⚠️ has_catalyst bu testte HER ZAMAN 0 — KAP verisi henüz bu kadar "
        f"geriye gitmiyor.\n"
        f"⚠️ Feature'lar günlük barla hesaplandı (canlı radar 15dk kullanıyor) "
        f"— yaklaşık sonuç.\n"
        f"⚠️ 'RSI dip tepkisi' net tanımlı değildi, RSI 35-55 aralığı olarak "
        f"yorumlandı — kesinleştirilmesi gerekebilir."
    )
    for m in (mesaj1, mesaj2, mesaj3):
        print(m, flush=True)
        send_telegram_message(m)
        time.sleep(1)
    print(f"[KAYDEDİLDİ] {OUTPUT_CSV} — {len(df_sonuc)} satır", flush=True)


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
