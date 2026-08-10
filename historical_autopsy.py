"""
historical_autopsy.py — VERİ MADENCİLİĞİ VE HİSSELERİN GENETİK HARİTASI
=========================================================================
AMAÇ: "İnşallah tutar" indikatör stratejisi kurmak yerine, geçmişte
%5+ YÜKSELEN (LONG_EVENT) veya %5+ DÜŞEN (SHORT_EVENT) günleri bulup,
o hareket BAŞLAMADAN TAM 1 GÜN ÖNCE (T-1) hissenin teknik/hacim
"parmak izinin" ne olduğunu çıkarmak. Amaç bir strateji değil, bir
OTOPSİ — sonuçlar istatistiksel bir özet, doğrudan sinyal değil.

ÖNEMLİ - LOOKAHEAD YOK: T-1'deki tüm indikatörler SADECE T-1 ve
öncesindeki barlarla hesaplanıyor, patlama gününün (T0) hiçbir verisi
(gap hariç, o zaten T0 açılışı-T-1 kapanışı farkı olarak tanımlı)
kullanılmıyor. Bu proje daha önce lookahead hatası yüzünden bir turnuva
sonucunu geçersiz kılmıştı (radar_onculu_test.py) - aynı hata burada
tekrarlanmasın diye özellikle belirtiyorum.

ÖNEMLİ - pandas_ta KULLANILMADI: pandas_ta kütüphanesi numpy>=1.24 ile
uyumsuz (np.NaN kaldırıldı, kütüphane güncellenmiyor) - kurulumda hemen
ImportError verir. Bunun yerine tüm indikatörler (RSI, MACD, Bollinger,
ATR, OBV, CMF, MFI, Stochastic, StochRSI) pandas/numpy ile elle,
bağımlılıksız yazıldı. requirements.txt'ye yeni paket eklemene gerek yok.

ÇALIŞTIRMA: `python historical_autopsy.py` — bağımsız bir analiz
scripti, canlı bota entegre değil, Start Command'i etkilemez. Sonuçlar
hem loglara hem de (TELEGRAM_TOKEN/TELEGRAM_CHAT_ID zaten Render'da
tanımlı olduğu için) Telegram'a özet olarak gönderilir - CSV dosyaları
kalıcı diskte olmadığı için (Render ücretsiz tier) asıl teslim edilecek
sonuç Telegram mesajlarıdır. BIST_TICKERS/US_TICKERS'ı aynı repo
içindeki stock_screener_bot.py'den otomatik almaya çalışır (varsa);
yoksa aşağıdaki örnek listelere düşer.
"""

import os
import time
import warnings
import ast
import numpy as np
import pandas as pd
import requests
import yfinance as yf

warnings.filterwarnings("ignore")

TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN", "")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")


def send_telegram_message(text: str):
    """Bot zaten calisirken bu iki degisken Render'da tanimli oluyor -
    yeni bir ayar gerekmiyor. Eksikse sessizce loga dusup devam eder,
    scripti durdurmaz."""
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        print("[UYARI] TELEGRAM_TOKEN/TELEGRAM_CHAT_ID yok - Telegram'a "
              "gonderilemiyor, sadece loglarda kalacak.", flush=True)
        return
    try:
        requests.post(
            f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage",
            json={"chat_id": TELEGRAM_CHAT_ID, "text": text[:4000]}, timeout=20)
    except Exception as e:
        print(f"[HATA] Telegram gonderilemedi: {e}", flush=True)

# =============================================================================
# 1. AYARLAR
# =============================================================================

EVENT_THRESHOLD_PCT = 5.0     # +-%5 esik
LOOKBACK_PERIOD = "120d"      # ne kadar geriye gidilecek
MIN_HISTORY_BARS = 55         # T-1 indikatorleri icin en az bu kadar bar lazim (SMA50 vb.)
REQUEST_DELAY_SEC = 0.4       # Yahoo rate-limit'e takilmamak icin istekler arasi bekleme

OUTPUT_CSV = "historical_autopsy_results.csv"
OUTPUT_SUMMARY_CSV = "historical_autopsy_summary.csv"


def _load_tickers_from_bot_file(path="stock_screener_bot.py"):
    """stock_screener_bot.py'yi IMPORT ETMEDEN - yani Telegram baslangic
    mesajlari, validate_tickers'in agdan dogrulamasi gibi hicbir yan etkiyi
    TETIKLEMEDEN - kaynak kodunu metin olarak okuyup BIST_TICKERS/US_TICKERS
    listelerini STATIK cikarir (AST). Onceki surumde `from stock_screener_bot
    import ...` kullaniliyordu; bu, o dosyanin TUM modul-seviyesi kodunu
    (canli botun kendi baslangic bildirimleri dahil) yan etki olarak
    calistirdigi icin degistirildi - proje genelinde zaten kullanilan
    dropped-def AST kontroluyle ayni, calistirmadan-oku yontemi."""
    try:
        with open(path, encoding="utf-8") as f:
            kaynak = f.read()
        agac = ast.parse(kaynak)
        bulunan = {}
        for node in ast.walk(agac):
            if isinstance(node, ast.Assign) and len(node.targets) == 1:
                hedef = node.targets[0]
                if isinstance(hedef, ast.Name) and hedef.id in ("BIST_TICKERS", "US_TICKERS"):
                    if isinstance(node.value, ast.List):
                        degerler = [el.value for el in node.value.elts
                                    if isinstance(el, ast.Constant) and isinstance(el.value, str)]
                        if degerler:
                            bulunan[hedef.id] = degerler
        if "BIST_TICKERS" in bulunan and "US_TICKERS" in bulunan:
            return bulunan["BIST_TICKERS"], bulunan["US_TICKERS"]
    except Exception as e:
        print(f"[BILGI] stock_screener_bot.py statik okunamadi: {e}", flush=True)
    return None, None


_bist, _us = _load_tickers_from_bot_file()
if _bist and _us:
    BIST_TICKERS, US_TICKERS = _bist, _us
    print(f"[BILGI] stock_screener_bot.py'den (import EDILMEDEN, statik okuma) "
          f"{len(BIST_TICKERS)} BIST, {len(US_TICKERS)} ABD hissesi alindi.", flush=True)
else:
    print("[BILGI] stock_screener_bot.py bulunamadi/okunamadi, ornek liste kullaniliyor. "
          "Kendi tam listeni asagida TICKERS_BIST/TICKERS_US icine yapistirabilirsin.",
          flush=True)
    BIST_TICKERS = [
        "THYAO.IS", "ASELS.IS", "SISE.IS", "KCHOL.IS", "GARAN.IS", "AKBNK.IS",
        "EREGL.IS", "BIMAS.IS", "TUPRS.IS", "SAHOL.IS", "PETKM.IS", "FROTO.IS",
        "TOASO.IS", "TCELL.IS", "YKBNK.IS", "ISCTR.IS", "KOZAL.IS", "PGSUS.IS",
        "TAVHL.IS", "VESTL.IS",
    ]
    US_TICKERS = [
        "AAPL", "MSFT", "GOOGL", "AMZN", "NVDA", "META", "TSLA", "AMD", "NFLX",
        "AVGO", "CRM", "ADBE", "PYPL", "INTC", "CSCO", "PEP", "COST", "TMUS",
        "QCOM", "TXN",
    ]

MARKETS = {
    "BIST": {"tickers": BIST_TICKERS, "index": "XU100.IS"},
    "US":   {"tickers": US_TICKERS,   "index": "SPY"},
}


# =============================================================================
# 2. ELLE YAZILMIŞ İNDİKATÖRLER (pandas_ta yerine, bağımlılıksız)
# =============================================================================

def sma(s: pd.Series, n: int) -> pd.Series:
    return s.rolling(n).mean()


def ema(s: pd.Series, n: int) -> pd.Series:
    return s.ewm(span=n, adjust=False).mean()


def rsi(close: pd.Series, n: int = 14) -> pd.Series:
    delta = close.diff()
    gain = delta.clip(lower=0)
    loss = -delta.clip(upper=0)
    avg_gain = gain.ewm(alpha=1 / n, adjust=False).mean()
    avg_loss = loss.ewm(alpha=1 / n, adjust=False).mean()
    rs = avg_gain / avg_loss.replace(0, np.nan)
    return 100 - (100 / (1 + rs))


def macd(close: pd.Series, fast=12, slow=26, signal=9):
    macd_line = ema(close, fast) - ema(close, slow)
    signal_line = ema(macd_line, signal)
    hist = macd_line - signal_line
    return macd_line, signal_line, hist


def bollinger(close: pd.Series, n=20, k=2):
    mid = sma(close, n)
    std = close.rolling(n).std()
    upper = mid + k * std
    lower = mid - k * std
    bandwidth = (upper - lower) / mid.replace(0, np.nan) * 100
    return upper, lower, bandwidth


def atr(high: pd.Series, low: pd.Series, close: pd.Series, n=14) -> pd.Series:
    prev_close = close.shift(1)
    tr = pd.concat([
        high - low,
        (high - prev_close).abs(),
        (low - prev_close).abs(),
    ], axis=1).max(axis=1)
    return tr.ewm(alpha=1 / n, adjust=False).mean()


def obv(close: pd.Series, volume: pd.Series) -> pd.Series:
    direction = np.sign(close.diff()).fillna(0)
    return (direction * volume).cumsum()


def cmf(high: pd.Series, low: pd.Series, close: pd.Series, volume: pd.Series, n=20) -> pd.Series:
    rng = (high - low).replace(0, np.nan)
    mfm = ((close - low) - (high - close)) / rng
    mfv = mfm * volume
    return mfv.rolling(n).sum() / volume.rolling(n).sum()


def mfi(high: pd.Series, low: pd.Series, close: pd.Series, volume: pd.Series, n=14) -> pd.Series:
    tp = (high + low + close) / 3
    raw_mf = tp * volume
    tp_diff = tp.diff()
    pos_mf = raw_mf.where(tp_diff > 0, 0.0).rolling(n).sum()
    neg_mf = raw_mf.where(tp_diff < 0, 0.0).rolling(n).sum()
    mfr = pos_mf / neg_mf.replace(0, np.nan)
    return 100 - (100 / (1 + mfr))


def stochastic(high: pd.Series, low: pd.Series, close: pd.Series, n=14, d=3):
    lowest = low.rolling(n).min()
    highest = high.rolling(n).max()
    k = (close - lowest) / (highest - lowest).replace(0, np.nan) * 100
    d_line = k.rolling(d).mean()
    return k, d_line


def stoch_rsi(rsi_series: pd.Series, n=14) -> pd.Series:
    lowest = rsi_series.rolling(n).min()
    highest = rsi_series.rolling(n).max()
    return (rsi_series - lowest) / (highest - lowest).replace(0, np.nan) * 100


# =============================================================================
# 3. VERİ ÇEKME + İNDİKATÖR HESAPLAMA (tüm seri, tek seferde - hızlı)
# =============================================================================

def fetch_and_prepare(ticker: str, period: str = LOOKBACK_PERIOD) -> pd.DataFrame:
    """Gunluk veriyi ceker, tum indikatorleri onceden hesaplar. Hata olursa
    bos DataFrame doner - cagiran taraf bunu atlar, script durmaz."""
    try:
        df = yf.Ticker(ticker).history(period=period, interval="1d")
        if df is None or df.empty or len(df) < MIN_HISTORY_BARS:
            return pd.DataFrame()
        df = df.rename(columns={
            "Open": "open", "High": "high", "Low": "low",
            "Close": "close", "Volume": "volume"})
        df = df[["open", "high", "low", "close", "volume"]].copy()
        df.index = pd.to_datetime(df.index).tz_localize(None)

        df["pct_change"] = df["close"].pct_change() * 100
        df["vol_ma20"] = df["volume"].rolling(20).mean()
        df["obv"] = obv(df["close"], df["volume"])
        df["cmf"] = cmf(df["high"], df["low"], df["close"], df["volume"])
        df["mfi"] = mfi(df["high"], df["low"], df["close"], df["volume"])
        _, _, df["bb_bandwidth"] = bollinger(df["close"])
        df["atr"] = atr(df["high"], df["low"], df["close"])
        df["rsi14"] = rsi(df["close"], 14)
        df["rsi9"] = rsi(df["close"], 9)
        macd_line, macd_signal, macd_hist = macd(df["close"])
        df["macd_line"] = macd_line
        df["macd_signal"] = macd_signal
        df["macd_hist"] = macd_hist
        df["stoch_k"], df["stoch_d"] = stochastic(df["high"], df["low"], df["close"])
        df["stochrsi"] = stoch_rsi(df["rsi14"])
        df["sma20"] = sma(df["close"], 20)
        df["sma50"] = sma(df["close"], 50)
        df["close_to_high_pct"] = (
            (df["close"] - df["low"]) / (df["high"] - df["low"]).replace(0, np.nan) * 100
        )
        return df
    except Exception as e:
        print(f"[HATA] {ticker}: veri/indikator hatasi - {e}", flush=True)
        return pd.DataFrame()


# =============================================================================
# 4. OLAY TESPİTİ + T-1 PARMAK İZİ ÇIKARMA
# =============================================================================

def extract_t1_fingerprint(df: pd.DataFrame, index_df: pd.DataFrame,
                            event_idx: int, market: str, ticker: str) -> dict:
    """event_idx: patlama gununun (T0) df icindeki pozisyonu. T-1 = event_idx-1.
    SADECE T-1 ve oncesi kullanilir (gap haric - o T0 acilisi ile T-1 kapanisinin
    farki, tanimi geregi lookahead degil)."""
    t1 = event_idx - 1
    t0 = event_idx
    row = df.iloc[t1]
    t0_row = df.iloc[t0]

    obv_3_ago = df["obv"].iloc[t1 - 3] if t1 - 3 >= 0 else np.nan

    # Endeksle gorece guc: T-1 gunundeki hisse getirisi - T-1 gunundeki endeks getirisi
    rel_strength = np.nan
    try:
        idx_pct_t1 = index_df.loc[row.name, "pct_change"] if row.name in index_df.index else np.nan
        rel_strength = row["pct_change"] - idx_pct_t1
    except Exception:
        pass

    gap_pct = (t0_row["open"] - row["close"]) / row["close"] * 100 if row["close"] else np.nan

    return {
        "market": market,
        "ticker": ticker,
        "event_type": "LONG_EVENT" if t0_row["pct_change"] >= EVENT_THRESHOLD_PCT else "SHORT_EVENT",
        "event_date": t0_row.name.date().isoformat(),
        "event_pct_change": round(float(t0_row["pct_change"]), 2),
        "t1_date": row.name.date().isoformat(),
        "vol_ratio_t1": _r(row["volume"] / row["vol_ma20"]) if row["vol_ma20"] else np.nan,
        "obv_trend_up_t1": bool(row["obv"] > obv_3_ago) if not np.isnan(obv_3_ago) else None,
        "cmf_t1": _r(row["cmf"]),
        "mfi_t1": _r(row["mfi"]),
        "bb_bandwidth_t1": _r(row["bb_bandwidth"]),
        "bb_bandwidth_20d_min": bool(row["bb_bandwidth"] <= df["bb_bandwidth"].iloc[max(0, t1 - 19):t1 + 1].min()),
        "atr_t1": _r(row["atr"]),
        "atr_chg_5d_pct": _r((row["atr"] / df["atr"].iloc[t1 - 5] - 1) * 100) if t1 - 5 >= 0 and df["atr"].iloc[t1 - 5] else np.nan,
        "rsi14_t1": _r(row["rsi14"]),
        "rsi9_t1": _r(row["rsi9"]),
        "rsi14_neutral_45_55": bool(45 <= row["rsi14"] <= 55) if not np.isnan(row["rsi14"]) else None,
        "macd_hist_t1": _r(row["macd_hist"]),
        "macd_bullish_cross_t1": bool(
            df["macd_line"].iloc[t1] > df["macd_signal"].iloc[t1] and
            df["macd_line"].iloc[t1 - 1] <= df["macd_signal"].iloc[t1 - 1]
        ) if t1 - 1 >= 0 else None,
        "stoch_k_t1": _r(row["stoch_k"]),
        "stochrsi_t1": _r(row["stochrsi"]),
        "dist_sma20_pct": _r((row["close"] - row["sma20"]) / row["sma20"] * 100) if row["sma20"] else np.nan,
        "dist_sma50_pct": _r((row["close"] - row["sma50"]) / row["sma50"] * 100) if row["sma50"] else np.nan,
        "close_to_high_pct_t1": _r(row["close_to_high_pct"]),
        "relative_strength_t1": _r(rel_strength),
        "gap_pct_t0_open": _r(gap_pct),
    }


def _r(x, nd=3):
    try:
        if x is None or (isinstance(x, float) and np.isnan(x)):
            return np.nan
        return round(float(x), nd)
    except Exception:
        return np.nan


def find_events_for_ticker(ticker: str, market: str, index_df: pd.DataFrame) -> list:
    df = fetch_and_prepare(ticker)
    if df.empty:
        return []
    results = []
    for i in range(MIN_HISTORY_BARS, len(df)):
        chg = df["close"].pct_change().iloc[i] * 100 if i > 0 else np.nan
        if pd.isna(chg):
            continue
        if chg >= EVENT_THRESHOLD_PCT or chg <= -EVENT_THRESHOLD_PCT:
            try:
                results.append(extract_t1_fingerprint(df, index_df, i, market, ticker))
            except Exception as e:
                print(f"[HATA] {ticker} olay {df.index[i].date()}: parmak izi cikarilamadi - {e}", flush=True)
    return results


# =============================================================================
# 5. ANA AKIŞ
# =============================================================================

def run_autopsy():
    all_events = []
    for market, cfg in MARKETS.items():
        print(f"\n=== {market} taraniyor ({len(cfg['tickers'])} hisse) ===", flush=True)
        index_df = fetch_and_prepare(cfg["index"])
        if index_df.empty:
            print(f"[UYARI] {market} endeksi ({cfg['index']}) alinamadi - "
                  "gorece guc hesaplanamayacak.", flush=True)

        for n, ticker in enumerate(cfg["tickers"], 1):
            events = find_events_for_ticker(ticker, market, index_df)
            if events:
                print(f"[{n}/{len(cfg['tickers'])}] {ticker}: {len(events)} olay bulundu", flush=True)
            all_events.extend(events)
            time.sleep(REQUEST_DELAY_SEC)

    if not all_events:
        print("\n[SONUÇ] Hiç olay bulunamadı — eşiği veya periyodu genişletmeyi düşün.", flush=True)
        send_telegram_message("🔬 [OTOPSİ] Analiz tamamlandı ama hiç ±%5 olay bulunamadı — "
                               "eşik veya periyot genişletilmeli.")
        return

    result_df = pd.DataFrame(all_events)
    result_df.to_csv(OUTPUT_CSV, index=False, encoding="utf-8-sig")
    print(f"\n[KAYDEDİLDİ] {OUTPUT_CSV} — {len(result_df)} olay", flush=True)

    long_n = (result_df["event_type"] == "LONG_EVENT").sum()
    short_n = (result_df["event_type"] == "SHORT_EVENT").sum()
    send_telegram_message(
        f"🔬 [OTOPSİ TAMAMLANDI]\n"
        f"Toplam {len(result_df)} olay bulundu (LONG {long_n}, SHORT {short_n}).\n"
        f"Aşağıda market + yön kırılımlı özetler ayrı mesajlarda gelecek."
    )

    print_and_save_summary(result_df)


def print_and_save_summary(df: pd.DataFrame):
    print("\n" + "=" * 70)
    print("İSTATİSTİKSEL ÖZET")
    print("=" * 70)

    numeric_cols = [
        "vol_ratio_t1", "cmf_t1", "mfi_t1", "bb_bandwidth_t1", "atr_t1",
        "atr_chg_5d_pct", "rsi14_t1", "rsi9_t1", "macd_hist_t1", "stoch_k_t1",
        "stochrsi_t1", "dist_sma20_pct", "dist_sma50_pct", "close_to_high_pct_t1",
        "relative_strength_t1", "gap_pct_t0_open",
    ]
    bool_cols = ["obv_trend_up_t1", "bb_bandwidth_20d_min", "rsi14_neutral_45_55", "macd_bullish_cross_t1"]

    summary_rows = []
    for market in ["BIST", "US"]:
        market_df = df[df["market"] == market]
        if market_df.empty:
            continue
        for event_type in ["LONG_EVENT", "SHORT_EVENT"]:
            grup = market_df[market_df["event_type"] == event_type]
            n = len(grup)
            baslik = f"{market} — {event_type} — {n} olay"
            print(f"\n### {baslik} ###")
            if n == 0:
                continue

            tg_lines = [f"📊 [OTOPSİ] {baslik}", "", "Ortalama / Medyan:"]
            print("-- Ortalama / Medyan tablosu --")
            for col in numeric_cols:
                vals = grup[col].dropna()
                if vals.empty:
                    continue
                print(f"  {col:26s} ort: {vals.mean():>8.2f} | medyan: {vals.median():>8.2f}")
                tg_lines.append(f"  {col}: ort {vals.mean():.2f} | med {vals.median():.2f}")
                summary_rows.append({"market": market, "event_type": event_type, "metrik": col,
                                      "ortalama": round(vals.mean(), 3), "medyan": round(vals.median(), 3), "n": len(vals)})

            tg_lines.append("")
            tg_lines.append("Koşul yüzdeleri:")
            print("-- Koşul yüzdeleri --")
            for col in bool_cols:
                vals = grup[col].dropna()
                if vals.empty:
                    continue
                oran = vals.mean() * 100
                print(f"  {col:26s} : %{oran:.1f}  (n={len(vals)})")
                tg_lines.append(f"  {col}: %{oran:.1f} (n={len(vals)})")
                summary_rows.append({"market": market, "event_type": event_type, "metrik": col,
                                      "ortalama": round(oran, 1), "medyan": np.nan, "n": len(vals)})

            send_telegram_message("\n".join(tg_lines))
            time.sleep(1)  # Telegram rate-limitine takilmamak icin mesajlar arasi kisa bekleme

    if summary_rows:
        pd.DataFrame(summary_rows).to_csv(OUTPUT_SUMMARY_CSV, index=False, encoding="utf-8-sig")
        print(f"\n[KAYDEDİLDİ] {OUTPUT_SUMMARY_CSV}")


if __name__ == "__main__":
    import threading
    from http.server import HTTPServer, BaseHTTPRequestHandler

    class _SagliqSunucusu(BaseHTTPRequestHandler):
        """Render web servis olarak calistirdigi icin bir port dinlemesi
        gerekiyor - yoksa 5 dk icinde 'port bulunamadi' diye durduruluyor
        (bist_h1_tournament.py'de ogrenilen ders)."""
        def do_GET(self):
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b"OK")

        def log_message(self, *args):
            pass  # konsolu kirletmesin

    def _saglik_sunucusunu_baslat():
        port = int(__import__("os").environ.get("PORT", 10000))
        server = HTTPServer(("0.0.0.0", port), _SagliqSunucusu)
        server.serve_forever()

    threading.Thread(target=_saglik_sunucusunu_baslat, daemon=True).start()

    run_autopsy()

    print("\n[BİTTİ] Analiz tamamlandı. Sonuçları yukarıdaki loglarda ve "
          "CSV dosyalarında görebilirsin. Şimdi Render'da Start Command'i "
          "'python main.py'ye geri çevirip yeniden deploy edebilirsin.", flush=True)
    send_telegram_message(
        "✅ [OTOPSİ BİTTİ] Tüm özet mesajları yukarıda gönderildi.\n"
        "Şimdi Render → Settings → Start Command'i 'python main.py'ye "
        "geri çevirip kaydet — bot normal taramalara döner."
    )

    # Script bitince process kapanirsa Render bunu "cokme" sanip yeniden
    # baslatir (ayni turnuva scriptlerindeki gibi) - o yuzden sonsuz
    # dongude bekletiyoruz, sen Start Command'i degistirene kadar.
    while True:
        time.sleep(3600)
