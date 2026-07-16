"""
BIST + ABD hisse - strateji turnuvasi
Kripto botundaki turnuva metodolojisinin (checkpoint bazli, komisyon dusulmus
ort. net getiri) hisse senedi verisine uyarlanmis hali.

BIST: gunluk mumlar, checkpoint'ler gun bazinda (1g/3g/5g/10g)
ABD:  15 dakikalik gun ici mumlar, checkpoint'ler saat bazinda (1sa/4sa/gun sonu)

NOT: yfinance'ta 15m veri sadece ~son 60 gun icin tutuluyor, bu yuzden ABD
gun ici orneklemi BIST'e gore cok daha kucuk olacak. Bu normal, sonuclari
yorumlarken orneklem buyuklugune dikkat et.

Calistirmak icin: pip install yfinance pandas numpy --break-system-packages
                  python3 stock_strategy_tournament.py
Sonuc: stok_turnuva_bist.csv ve stok_turnuva_abd.csv + konsola ozet tablo
"""

import time
from datetime import datetime

import numpy as np
import pandas as pd

try:
    import yfinance as yf
except ImportError:
    yf = None  # test/mock modunda yfinance gerekmez


# ---------------------------------------------------------------------------
# Hisse listeleri (mevcut bottaki ile ayni)
# ---------------------------------------------------------------------------

BIST_TICKERS = [
    "AKBNK.IS", "ARCLK.IS", "ASELS.IS", "BIMAS.IS", "EKGYO.IS",
    "ENKAI.IS", "EREGL.IS", "FROTO.IS", "GARAN.IS", "GUBRF.IS",
    "HALKB.IS", "ISCTR.IS", "KCHOL.IS", "KOZAL.IS", "KRDMD.IS",
    "MGROS.IS", "ODAS.IS", "PETKM.IS", "PGSUS.IS", "SAHOL.IS",
    "SASA.IS", "SISE.IS", "TAVHL.IS", "TCELL.IS", "THYAO.IS",
    "TOASO.IS", "TUPRS.IS", "VAKBN.IS", "YKBNK.IS", "ALARK.IS",
]

US_TICKERS = [
    "AAPL", "MSFT", "GOOGL", "AMZN", "NVDA", "META", "TSLA", "BRK-B",
    "JPM", "V", "UNH", "MA", "HD", "PG", "COST", "XOM", "JNJ", "ABBV",
    "MRK", "AVGO", "PEP", "KO", "BAC", "WMT", "CRM", "ADBE", "AMD",
    "NFLX", "DIS", "CSCO", "ORCL", "INTC", "QCOM", "TXN", "PFE",
    "NKE", "MCD", "GS", "CAT", "BA",
]

COMMISSION_PCT = 0.15  # gidis-donus tahmini komisyon+slipaj (%), ihtiyaca gore ayarla

# BIST checkpoint'leri: (gun_sayisi, etiket, hedef_yuzde)
BIST_CHECKPOINTS = [(1, "1g", 1.0), (3, "3g", 2.0), (5, "5g", 3.0), (10, "10g", 5.0)]

# ABD gun ici checkpoint'leri (15m mum sayisi): (mum_sayisi, etiket, hedef_yuzde)
# 4 mum = 1sa, 16 mum = 4sa, 26 mum = ~gun sonu (6.5sa'lik ABD seansi)
US_CHECKPOINTS = [(4, "1sa", 0.3), (16, "4sa", 0.6), (26, "gunsonu", 1.0)]


# ---------------------------------------------------------------------------
# Veri cekme
# ---------------------------------------------------------------------------

def fetch_daily_df(ticker: str, period: str = "2y") -> pd.DataFrame:
    df = yf.Ticker(ticker).history(period=period, interval="1d")
    df = df.reset_index()
    df = df.rename(columns={
        "Date": "timestamp", "Open": "open", "High": "high",
        "Low": "low", "Close": "close", "Volume": "volume",
    })
    return df[["timestamp", "open", "high", "low", "close", "volume"]]


def fetch_intraday_df(ticker: str, interval: str = "15m", period: str = "60d") -> pd.DataFrame:
    df = yf.Ticker(ticker).history(period=period, interval=interval)
    df = df.reset_index()
    df = df.rename(columns={
        "Datetime": "timestamp", "Date": "timestamp", "Open": "open", "High": "high",
        "Low": "low", "Close": "close", "Volume": "volume",
    })
    return df[["timestamp", "open", "high", "low", "close", "volume"]]


# ---------------------------------------------------------------------------
# Indikatorler
# ---------------------------------------------------------------------------

def compute_indicators(df: pd.DataFrame, rsi_period: int = 14) -> pd.DataFrame:
    df = df.copy()
    df["vol_sma20"] = df["volume"].rolling(20).mean()
    df["vol_std20"] = df["volume"].rolling(20).std()
    df["vol_zscore"] = (df["volume"] - df["vol_sma20"]) / df["vol_std20"].replace(0, np.nan)

    df["sma20"] = df["close"].rolling(20).mean()
    df["dev_pct"] = (df["close"] - df["sma20"]) / df["sma20"] * 100

    df["is_bull"] = df["close"] > df["open"]

    delta = df["close"].diff()
    gain = delta.clip(lower=0)
    loss = -delta.clip(upper=0)
    avg_gain = gain.ewm(alpha=1 / rsi_period, adjust=False).mean()
    avg_loss = loss.ewm(alpha=1 / rsi_period, adjust=False).mean()
    rs = avg_gain / avg_loss.replace(0, np.nan)
    df["rsi"] = 100 - (100 / (1 + rs))
    df["rsi"] = df["rsi"].fillna(50)

    candle_range = (df["high"] - df["low"]).replace(0, np.nan)
    df["lower_wick_ratio"] = ((df[["open", "close"]].min(axis=1) - df["low"]) / candle_range).fillna(0)
    df["upper_wick_ratio"] = ((df["high"] - df[["open", "close"]].max(axis=1)) / candle_range).fillna(0)

    boll_mid = df["close"].rolling(20).mean()
    boll_std = df["close"].rolling(20).std()
    df["boll_upper"] = boll_mid + 2 * boll_std
    df["boll_lower"] = boll_mid - 2 * boll_std

    return df


# ---------------------------------------------------------------------------
# Strateji tanimlari - her biri (df, i) -> "LONG"/"SHORT"/None dondurur
# i: bakilan mumun index'i (bu mum KAPANMIS kabul edilir)
# ---------------------------------------------------------------------------

def s1_wick_rsi_volume(df, i, wick_th=0.35, rsi_os=30, rsi_ob=70, vol_mult=1.5):
    """Mevcut canli bot mantigi: fitil + RSI + hacim, ucu de ayni anda."""
    row = df.iloc[i]
    if pd.isna(row["vol_sma20"]) or row["vol_sma20"] == 0:
        return None
    vol_ratio = row["volume"] / row["vol_sma20"]
    if vol_ratio < vol_mult:
        return None
    if row["lower_wick_ratio"] >= wick_th and row["rsi"] <= rsi_os:
        return "LONG"
    if row["upper_wick_ratio"] >= wick_th and row["rsi"] >= rsi_ob:
        return "SHORT"
    return None


def s2_wick_rsi(df, i, wick_th=0.35, rsi_os=30, rsi_ob=70):
    """Hacim sartini kaldirilmis hali - daha sik tetiklenir mi?"""
    row = df.iloc[i]
    if row["lower_wick_ratio"] >= wick_th and row["rsi"] <= rsi_os:
        return "LONG"
    if row["upper_wick_ratio"] >= wick_th and row["rsi"] >= rsi_ob:
        return "SHORT"
    return None


def s3_rsi_only(df, i, rsi_os=30, rsi_ob=70):
    """Sadece RSI asiri uc - en basit filtre, referans/kiyas amacli."""
    row = df.iloc[i]
    if row["rsi"] <= rsi_os:
        return "LONG"
    if row["rsi"] >= rsi_ob:
        return "SHORT"
    return None


def s4_volume_zscore(df, i, z_th=2.0):
    """Kripto botundaki Hacim Z-Skor stratejisinin hisse senedine uyarlanmasi."""
    row = df.iloc[i]
    if pd.isna(row["vol_zscore"]) or row["vol_zscore"] < z_th:
        return None
    if row["close"] < row["open"]:
        return "LONG"
    elif row["close"] > row["open"]:
        return "SHORT"
    return None


def s5_bollinger_touch_rsi(df, i, rsi_os=35, rsi_ob=65):
    """Gercek Bollinger disina tasma sarti (mevcut botta bu sart yoktu, bilgi amacliydi) + RSI."""
    row = df.iloc[i]
    if pd.isna(row["boll_lower"]) or pd.isna(row["boll_upper"]):
        return None
    if row["close"] <= row["boll_lower"] and row["rsi"] <= rsi_os:
        return "LONG"
    if row["close"] >= row["boll_upper"] and row["rsi"] >= rsi_ob:
        return "SHORT"
    return None


def s6_sma_deviation(df, i, dev_th=5.0):
    """Kripto botundaki VWAP Sapmasi stratejisinin proxy'si (VWAP yerine SMA20 sapmasi)."""
    row = df.iloc[i]
    if pd.isna(row["dev_pct"]):
        return None
    if row["dev_pct"] <= -dev_th:
        return "LONG"
    if row["dev_pct"] >= dev_th:
        return "SHORT"
    return None


STRATEGIES_DAILY = [
    ("01-Fitil+RSI+Hacim (mevcut sistem)", lambda df, i: s1_wick_rsi_volume(df, i)),
    ("02-Fitil+RSI (hacimsiz)", lambda df, i: s2_wick_rsi(df, i)),
    ("03-Sadece RSI", lambda df, i: s3_rsi_only(df, i)),
    ("04-Hacim Z-Skor", lambda df, i: s4_volume_zscore(df, i)),
    ("05-Bollinger Disi+RSI (gercek sart)", lambda df, i: s5_bollinger_touch_rsi(df, i)),
    ("06-SMA20 Sapmasi %5", lambda df, i: s6_sma_deviation(df, i, dev_th=5.0)),
]

STRATEGIES_INTRADAY = [
    ("01-Fitil+RSI+Hacim (mevcut sistem)", lambda df, i: s1_wick_rsi_volume(df, i, wick_th=0.4, rsi_os=25, rsi_ob=75, vol_mult=1.8)),
    ("02-Fitil+RSI (hacimsiz)", lambda df, i: s2_wick_rsi(df, i, wick_th=0.4, rsi_os=25, rsi_ob=75)),
    ("03-Sadece RSI", lambda df, i: s3_rsi_only(df, i, rsi_os=25, rsi_ob=75)),
    ("04-Hacim Z-Skor", lambda df, i: s4_volume_zscore(df, i)),
    ("05-Bollinger Disi+RSI (gercek sart)", lambda df, i: s5_bollinger_touch_rsi(df, i, rsi_os=30, rsi_ob=70)),
    ("06-SMA20 Sapmasi %2", lambda df, i: s6_sma_deviation(df, i, dev_th=2.0)),
]


# ---------------------------------------------------------------------------
# Backtest motoru
# ---------------------------------------------------------------------------

def run_backtest(df: pd.DataFrame, strategies: list, checkpoints: list, min_gap_bars: int = 3):
    """
    Her strateji icin df uzerinde yuruyup sinyalleri toplar, her checkpoint'te
    basari/basarisizlik ve komisyon sonrasi net getiriyi hesaplar.
    min_gap_bars: ayni yonde ust uste sinyal spam'ini onlemek icin, bir sinyalden
    sonra en az bu kadar mum gecmedenayni tickerda yeni sinyal alinmaz.
    """
    results = {name: [] for name, _ in strategies}
    max_checkpoint = max(c[0] for c in checkpoints)
    n = len(df)

    for name, fn in strategies:
        last_signal_i = -min_gap_bars - 1
        i = 20  # indikatorlerin oturmasi icin bastan biraz atla
        while i < n - max_checkpoint - 1:
            if i - last_signal_i < min_gap_bars:
                i += 1
                continue
            direction = fn(df, i)
            if direction is None:
                i += 1
                continue

            entry_price = df.iloc[i]["close"]
            outcome_hit = False
            outcome_pct = None
            for bars_ahead, label, target_pct in checkpoints:
                future_price = df.iloc[i + bars_ahead]["close"]
                raw_pct = (future_price - entry_price) / entry_price * 100
                pct = raw_pct if direction == "LONG" else -raw_pct
                net_pct = pct - COMMISSION_PCT
                if pct >= target_pct:
                    outcome_hit = True
                    outcome_pct = net_pct
                    break
            if not outcome_hit:
                # son checkpoint'teki net getiriyi basarisiz sonuc olarak kaydet
                future_price = df.iloc[i + max_checkpoint]["close"]
                raw_pct = (future_price - entry_price) / entry_price * 100
                pct = raw_pct if direction == "LONG" else -raw_pct
                outcome_pct = pct - COMMISSION_PCT

            results[name].append(outcome_pct)
            last_signal_i = i
            i += 1

    return results


def summarize(results: dict) -> pd.DataFrame:
    rows = []
    for name, outcomes in results.items():
        if not outcomes:
            rows.append({"strateji": name, "sinyal": 0, "isabet_%": None, "ort_net_%": None, "toplam_%": None})
            continue
        arr = np.array(outcomes)
        hit_rate = (arr > 0).mean() * 100
        rows.append({
            "strateji": name,
            "sinyal": len(arr),
            "isabet_%": round(hit_rate, 1),
            "ort_net_%": round(arr.mean(), 3),
            "toplam_%": round(arr.sum(), 1),
        })
    out = pd.DataFrame(rows).sort_values("ort_net_%", ascending=False, na_position="last")
    return out


# ---------------------------------------------------------------------------
# Ana akis
# ---------------------------------------------------------------------------

def tournament_bist():
    print("\n=== BIST GUNLUK TURNUVA ===")
    combined = {name: [] for name, _ in STRATEGIES_DAILY}
    for ticker in BIST_TICKERS:
        try:
            df = fetch_daily_df(ticker)
            if len(df) < 40:
                print(f"{ticker}: yetersiz veri, atlandi")
                continue
            df = compute_indicators(df)
            res = run_backtest(df, STRATEGIES_DAILY, BIST_CHECKPOINTS)
            for name, outcomes in res.items():
                combined[name].extend(outcomes)
            print(f"{ticker}: tamamlandi ({len(df)} mum)")
            time.sleep(0.3)  # yfinance rate limit'e takilmamak icin
        except Exception as e:
            print(f"{ticker}: hata - {e}")

    table = summarize(combined)
    print("\n--- BIST SONUCLARI ---")
    print(table.to_string(index=False))
    table.to_csv("stok_turnuva_bist.csv", index=False)
    return table


def tournament_us():
    print("\n=== ABD GUN ICI (15m) TURNUVA ===")
    combined = {name: [] for name, _ in STRATEGIES_INTRADAY}
    for ticker in US_TICKERS:
        try:
            df = fetch_intraday_df(ticker)
            if len(df) < 40:
                print(f"{ticker}: yetersiz veri, atlandi")
                continue
            df = compute_indicators(df, rsi_period=6)
            res = run_backtest(df, STRATEGIES_INTRADAY, US_CHECKPOINTS)
            for name, outcomes in res.items():
                combined[name].extend(outcomes)
            print(f"{ticker}: tamamlandi ({len(df)} mum)")
            time.sleep(0.3)
        except Exception as e:
            print(f"{ticker}: hata - {e}")

    table = summarize(combined)
    print("\n--- ABD GUN ICI SONUCLARI ---")
    print(table.to_string(index=False))
    table.to_csv("stok_turnuva_abd.csv", index=False)
    return table


if __name__ == "__main__":
    if yf is None:
        raise RuntimeError("yfinance kurulu degil. 'pip install yfinance --break-system-packages' calistir.")
    tournament_bist()
    tournament_us()
    print(f"\nTamamlandi - {datetime.now().isoformat()}")
