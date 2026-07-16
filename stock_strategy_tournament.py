"""
BIST + ABD hisse - GENISLETILMIS strateji turnuvasi
Kripto botundaki turnuva metodolojisinin (checkpoint bazli, hem komisyonsuz/HAM
hem komisyon-dusulmus/NET getiri raporlu) hisse senedi verisine uyarlanmis hali.

Bu surum onceki turlarin devami:
- BIST gunluk: zaten karli cikmisti, birkac ek parametre varyasyonuyla dogrulama
- ABD gun ici (15m): kullanicinin gercek islem tarzina (saatlik/gun ici) gore
  checkpoint'ler (15dk/30dk/1sa/2sa/4sa) + COK DAHA FAZLA strateji/parametre
  varyasyonu (RSI donemleri, esikler, hacim carpanlari, VWAP/SMA sapma esikleri,
  ATR kirilim carpanlari, EMA/MACD momentum, acilis araligi kirilimi, gap fade...)
- ABD swing (gunluk): onceki turda Hacim Z-Skor tek karli cikan sistemdi

ONEMLI: her satirda hem HAM (komisyonsuz) hem NET (komisyon dusulmus) sonuc
raporlaniyor - artik manuel geri hesaplama gerekmiyor.

NOT: yfinance'ta 15m veri sadece ~son 60 gun icin tutuluyor, bu yuzden ABD
gun ici orneklemi BIST'e gore cok daha kucuk olacak.

Calistirmak icin: pip install yfinance pandas numpy requests --break-system-packages
                  python3 stock_strategy_tournament.py
Sonuc: stok_turnuva_bist.csv, stok_turnuva_abd.csv, stok_turnuva_abd_swing.csv
       + konsola ozet tablo + Telegram'a otomatik gonderim
"""

import os
import time
from datetime import datetime

import numpy as np
import pandas as pd
import requests

try:
    import yfinance as yf
except ImportError:
    yf = None  # test/mock modunda yfinance gerekmez


TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID")


def send_telegram_message(text: str):
    """Canli bottaki ile ayni ortam degiskenlerini kullanir, Railway'de ekstra ayar gerekmez."""
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        print("(Telegram token/chat id yok, sadece konsola yaziliyor)")
        return
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    # satir siniri bozmadan ~3500 karakterlik parcalara bol (Telegram limiti ~4096)
    lines = text.split("\n")
    chunk = ""
    for line in lines:
        if len(chunk) + len(line) + 1 > 3500:
            try:
                requests.post(url, data={"chat_id": TELEGRAM_CHAT_ID, "text": chunk}, timeout=15)
            except Exception as e:
                print(f"Telegram gonderim hatasi: {e}")
            chunk = ""
        chunk += line + "\n"
    if chunk.strip():
        try:
            requests.post(url, data={"chat_id": TELEGRAM_CHAT_ID, "text": chunk}, timeout=15)
        except Exception as e:
            print(f"Telegram gonderim hatasi: {e}")


# ---------------------------------------------------------------------------
# Hisse listeleri
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

COMMISSION_PCT = 0.15  # gidis-donus tahmini komisyon+slipaj (%) - NET hesapta dusulur, HAM'da dusulmez

BIST_CHECKPOINTS = [(1, "1g", 1.0), (3, "3g", 2.0), (5, "5g", 3.0), (10, "10g", 5.0)]

# Kullanicinin gercek islem tarzina (gun ici/saatlik opsiyon) gore kalibre edildi
US_CHECKPOINTS = [
    (1, "15dk", 0.15),
    (2, "30dk", 0.25),
    (4, "1sa", 0.40),
    (8, "2sa", 0.60),
    (16, "4sa", 0.90),
]


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

    def _rsi(period):
        delta = df["close"].diff()
        gain = delta.clip(lower=0)
        loss = -delta.clip(upper=0)
        avg_gain = gain.ewm(alpha=1 / period, adjust=False).mean()
        avg_loss = loss.ewm(alpha=1 / period, adjust=False).mean()
        rs = avg_gain / avg_loss.replace(0, np.nan)
        return (100 - (100 / (1 + rs))).fillna(50)

    df["rsi"] = _rsi(rsi_period)       # varsayilan (gunluk=14, gun ici=6 - cagrilirken belirlenir)
    df["rsi14"] = _rsi(14)
    df["rsi6"] = _rsi(6)
    df["rsi21"] = _rsi(21)

    candle_range = (df["high"] - df["low"]).replace(0, np.nan)
    df["lower_wick_ratio"] = ((df[["open", "close"]].min(axis=1) - df["low"]) / candle_range).fillna(0)
    df["upper_wick_ratio"] = ((df["high"] - df[["open", "close"]].max(axis=1)) / candle_range).fillna(0)

    boll_mid = df["close"].rolling(20).mean()
    boll_std = df["close"].rolling(20).std()
    df["boll_upper"] = boll_mid + 2 * boll_std
    df["boll_lower"] = boll_mid - 2 * boll_std

    df["ema9"] = df["close"].ewm(span=9, adjust=False).mean()
    df["ema20"] = df["close"].ewm(span=20, adjust=False).mean()
    df["ema21"] = df["close"].ewm(span=21, adjust=False).mean()
    df["ema50"] = df["close"].ewm(span=50, adjust=False).mean()

    ema12 = df["close"].ewm(span=12, adjust=False).mean()
    ema26 = df["close"].ewm(span=26, adjust=False).mean()
    df["macd"] = ema12 - ema26
    df["macd_signal"] = df["macd"].ewm(span=9, adjust=False).mean()

    high_low = df["high"] - df["low"]
    high_close = (df["high"] - df["close"].shift()).abs()
    low_close = (df["low"] - df["close"].shift()).abs()
    tr = pd.concat([high_low, high_close, low_close], axis=1).max(axis=1)
    df["atr14"] = tr.rolling(14).mean()

    low14 = df["low"].rolling(14).min()
    high14 = df["high"].rolling(14).max()
    df["stoch_k"] = ((df["close"] - low14) / (high14 - low14).replace(0, np.nan) * 100).fillna(50)

    typical = (df["high"] + df["low"] + df["close"]) / 3
    sma_tp = typical.rolling(20).mean()
    mad = typical.rolling(20).apply(lambda x: np.mean(np.abs(x - x.mean())), raw=True)
    df["cci"] = (typical - sma_tp) / (0.015 * mad.replace(0, np.nan))

    if "timestamp" in df.columns:
        ts = pd.to_datetime(df["timestamp"])
        day = ts.dt.date
        df["_day"] = day
        pv = typical * df["volume"]
        df["vwap"] = pv.groupby(day).cumsum() / df["volume"].groupby(day).cumsum()
        df["vwap_dev_pct"] = (df["close"] - df["vwap"]) / df["vwap"] * 100

        df["_bar_of_day"] = df.groupby(day).cumcount()
        df["_day_open"] = df.groupby(day)["open"].transform("first")
        # ilk 2 mumun (ör. 15m icin 30dk) yuksek/dusugunden acilis araligi
        or_high = df.groupby(day)["high"].transform(lambda x: x.iloc[:2].max())
        or_low = df.groupby(day)["low"].transform(lambda x: x.iloc[:2].min())
        df["_or_high"] = or_high
        df["_or_low"] = or_low
        prior_close_by_day = df.groupby(day)["close"].last().shift(1)
        df["_prior_day_close"] = day.map(prior_close_by_day)

    return df


# ---------------------------------------------------------------------------
# Strateji uretici fonksiyonlar - her biri (df, i) -> "LONG"/"SHORT"/None
# alan bir fonksiyon DONDURUR (closure). i: bakilan (kapanmis) mumun index'i.
# ---------------------------------------------------------------------------

def make_wick_rsi_vol(wick_th, rsi_os, rsi_ob, vol_mult=None, rsi_col="rsi"):
    def fn(df, i):
        row = df.iloc[i]
        if vol_mult is not None:
            if pd.isna(row["vol_sma20"]) or row["vol_sma20"] == 0:
                return None
            if row["volume"] / row["vol_sma20"] < vol_mult:
                return None
        if row["lower_wick_ratio"] >= wick_th and row[rsi_col] <= rsi_os:
            return "LONG"
        if row["upper_wick_ratio"] >= wick_th and row[rsi_col] >= rsi_ob:
            return "SHORT"
        return None
    return fn


def make_rsi_only(rsi_os, rsi_ob, rsi_col="rsi"):
    def fn(df, i):
        row = df.iloc[i]
        if row[rsi_col] <= rsi_os:
            return "LONG"
        if row[rsi_col] >= rsi_ob:
            return "SHORT"
        return None
    return fn


def make_volume_zscore(z_th):
    def fn(df, i):
        row = df.iloc[i]
        if pd.isna(row["vol_zscore"]) or row["vol_zscore"] < z_th:
            return None
        if row["close"] < row["open"]:
            return "LONG"
        elif row["close"] > row["open"]:
            return "SHORT"
        return None
    return fn


def make_bollinger_rsi(rsi_os, rsi_ob):
    def fn(df, i):
        row = df.iloc[i]
        if pd.isna(row["boll_lower"]) or pd.isna(row["boll_upper"]):
            return None
        if row["close"] <= row["boll_lower"] and row["rsi"] <= rsi_os:
            return "LONG"
        if row["close"] >= row["boll_upper"] and row["rsi"] >= rsi_ob:
            return "SHORT"
        return None
    return fn


def make_sma_deviation(dev_th):
    def fn(df, i):
        row = df.iloc[i]
        if pd.isna(row["dev_pct"]):
            return None
        if row["dev_pct"] <= -dev_th:
            return "LONG"
        if row["dev_pct"] >= dev_th:
            return "SHORT"
        return None
    return fn


def make_vwap_deviation(dev_th):
    def fn(df, i):
        row = df.iloc[i]
        if pd.isna(row.get("vwap_dev_pct")):
            return None
        if row["vwap_dev_pct"] <= -dev_th:
            return "LONG"
        if row["vwap_dev_pct"] >= dev_th:
            return "SHORT"
        return None
    return fn


def make_ema_cross(fast_col, slow_col):
    def fn(df, i):
        if i < 1:
            return None
        row, prev = df.iloc[i], df.iloc[i - 1]
        if pd.isna(row[fast_col]) or pd.isna(row[slow_col]) or pd.isna(prev[fast_col]) or pd.isna(prev[slow_col]):
            return None
        if prev[fast_col] <= prev[slow_col] and row[fast_col] > row[slow_col]:
            return "LONG"
        if prev[fast_col] >= prev[slow_col] and row[fast_col] < row[slow_col]:
            return "SHORT"
        return None
    return fn


def make_macd_cross():
    def fn(df, i):
        if i < 1:
            return None
        row, prev = df.iloc[i], df.iloc[i - 1]
        if pd.isna(row["macd"]) or pd.isna(row["macd_signal"]) or pd.isna(prev["macd"]) or pd.isna(prev["macd_signal"]):
            return None
        if prev["macd"] <= prev["macd_signal"] and row["macd"] > row["macd_signal"]:
            return "LONG"
        if prev["macd"] >= prev["macd_signal"] and row["macd"] < row["macd_signal"]:
            return "SHORT"
        return None
    return fn


def make_atr_breakout(atr_mult):
    def fn(df, i):
        if i < 1:
            return None
        row = df.iloc[i]
        prev_close = df.iloc[i - 1]["close"]
        if pd.isna(row["atr14"]) or row["atr14"] == 0:
            return None
        move = row["close"] - prev_close
        if move >= atr_mult * row["atr14"]:
            return "LONG"
        if move <= -atr_mult * row["atr14"]:
            return "SHORT"
        return None
    return fn


def make_stochastic(os_th, ob_th):
    def fn(df, i):
        row = df.iloc[i]
        if pd.isna(row["stoch_k"]):
            return None
        if row["stoch_k"] <= os_th:
            return "LONG"
        if row["stoch_k"] >= ob_th:
            return "SHORT"
        return None
    return fn


def make_cci(os_th, ob_th):
    def fn(df, i):
        row = df.iloc[i]
        if pd.isna(row["cci"]):
            return None
        if row["cci"] <= os_th:
            return "LONG"
        if row["cci"] >= ob_th:
            return "SHORT"
        return None
    return fn


def make_opening_range_breakout():
    """
    Sadece gun ici veri icin anlamli. Gunun ilk 2 mumunun (15m'de ~30dk) araligini
    kirip yukari/asagi devam eden fiyata, o yonde momentum girisi.
    """
    def fn(df, i):
        row = df.iloc[i]
        if pd.isna(row.get("_or_high")) or pd.isna(row.get("_or_low")) or pd.isna(row.get("_bar_of_day")):
            return None
        if row["_bar_of_day"] < 2:
            return None  # acilis araligi henuz olusmadi
        if row["close"] > row["_or_high"]:
            return "LONG"
        if row["close"] < row["_or_low"]:
            return "SHORT"
        return None
    return fn


def make_gap_fade(gap_th):
    """
    Sadece gun ici veri icin anlamli. Gun acilisinda onceki gun kapanisina gore
    buyuk bosluk (gap) varsa, boslugun kapanacagi (fade/tersine donus) beklentisi.
    """
    def fn(df, i):
        row = df.iloc[i]
        if row.get("_bar_of_day") != 0:
            return None  # sadece gunun ilk mumunda calisir
        prior_close = row.get("_prior_day_close")
        if pd.isna(prior_close) or prior_close == 0:
            return None
        gap_pct = (row["open"] - prior_close) / prior_close * 100
        if gap_pct >= gap_th:
            return "SHORT"  # yukari gap - asagi fade beklentisi
        if gap_pct <= -gap_th:
            return "LONG"   # asagi gap - yukari fade beklentisi
        return None
    return fn



# ---------------------------------------------------------------------------
# Strateji listeleri - parametre taramasiyla uretiliyor (kripto turnuvasindaki
# ~60 strateji olcegine benzer bir genislik hedeflendi)
# ---------------------------------------------------------------------------

STRATEGIES_DAILY = []

# A: Fitil+RSI+Hacim (BIST'te zaten karli cikan mevcut sistem + varyasyonlari)
for wick_th in [0.30, 0.35, 0.40]:
    for vol_mult in [1.5, 1.8, 2.0]:
        STRATEGIES_DAILY.append((
            f"A-Fitil{wick_th}+RSI+Hacim{vol_mult}",
            make_wick_rsi_vol(wick_th, 30, 70, vol_mult=vol_mult),
        ))

# B: Fitil+RSI (hacimsiz)
for wick_th in [0.30, 0.35, 0.40]:
    STRATEGIES_DAILY.append((f"B-Fitil{wick_th}+RSI (hacimsiz)", make_wick_rsi_vol(wick_th, 30, 70)))

# C: Sadece RSI - donem/esik varyasyonlari
for rsi_col, os_th, ob_th, label in [
    ("rsi14", 20, 80, "RSI14-20/80"), ("rsi14", 25, 75, "RSI14-25/75"), ("rsi14", 30, 70, "RSI14-30/70"),
    ("rsi6", 15, 85, "RSI6-15/85"), ("rsi6", 20, 80, "RSI6-20/80"),
    ("rsi21", 25, 75, "RSI21-25/75"), ("rsi21", 30, 70, "RSI21-30/70"),
]:
    STRATEGIES_DAILY.append((f"C-Sadece {label}", make_rsi_only(os_th, ob_th, rsi_col=rsi_col)))

# D: Hacim Z-Skor
for z_th in [1.5, 2.0, 2.5]:
    STRATEGIES_DAILY.append((f"D-Hacim Z-Skor>{z_th}", make_volume_zscore(z_th)))

# E: Bollinger Disi+RSI (gercek sart)
for os_th, ob_th in [(25, 75), (30, 70), (35, 65)]:
    STRATEGIES_DAILY.append((f"E-Bollinger+RSI {os_th}/{ob_th}", make_bollinger_rsi(os_th, ob_th)))

# F: SMA20 Sapmasi
for dev_th in [3.0, 5.0, 7.0]:
    STRATEGIES_DAILY.append((f"F-SMA20 Sapmasi %{dev_th}", make_sma_deviation(dev_th)))

# G: EMA kesisimi (momentum)
STRATEGIES_DAILY.append(("G-EMA9/21 Kesisimi", make_ema_cross("ema9", "ema21")))
STRATEGIES_DAILY.append(("G-EMA20/50 Kesisimi", make_ema_cross("ema20", "ema50")))

# H: MACD kesisimi (momentum)
STRATEGIES_DAILY.append(("H-MACD Kesisimi", make_macd_cross()))

# I: ATR kirilimi (momentum)
for atr_mult in [1.0, 1.5, 2.0]:
    STRATEGIES_DAILY.append((f"I-ATR Kirilimi x{atr_mult}", make_atr_breakout(atr_mult)))

# J: CCI
for os_th, ob_th in [(-100, 100), (-150, 150)]:
    STRATEGIES_DAILY.append((f"J-CCI {os_th}/{ob_th}", make_cci(os_th, ob_th)))


STRATEGIES_INTRADAY = []

# A: Fitil+RSI+Hacim
for wick_th in [0.30, 0.35, 0.40]:
    for vol_mult in [1.5, 1.8, 2.2]:
        STRATEGIES_INTRADAY.append((
            f"A-Fitil{wick_th}+RSI+Hacim{vol_mult}",
            make_wick_rsi_vol(wick_th, 25, 75, vol_mult=vol_mult, rsi_col="rsi6"),
        ))

# B: Fitil+RSI (hacimsiz)
for wick_th in [0.30, 0.35, 0.40, 0.50]:
    STRATEGIES_INTRADAY.append((f"B-Fitil{wick_th}+RSI (hacimsiz)", make_wick_rsi_vol(wick_th, 25, 75, rsi_col="rsi6")))

# C: Sadece RSI
for rsi_col, os_th, ob_th, label in [
    ("rsi14", 20, 80, "RSI14-20/80"), ("rsi14", 25, 75, "RSI14-25/75"), ("rsi14", 30, 70, "RSI14-30/70"),
    ("rsi6", 15, 85, "RSI6-15/85"), ("rsi6", 20, 80, "RSI6-20/80"), ("rsi6", 25, 75, "RSI6-25/75"),
    ("rsi21", 25, 75, "RSI21-25/75"), ("rsi21", 30, 70, "RSI21-30/70"),
]:
    STRATEGIES_INTRADAY.append((f"C-Sadece {label}", make_rsi_only(os_th, ob_th, rsi_col=rsi_col)))

# D: Hacim Z-Skor
for z_th in [1.5, 2.0, 2.5, 3.0]:
    STRATEGIES_INTRADAY.append((f"D-Hacim Z-Skor>{z_th}", make_volume_zscore(z_th)))

# E: Bollinger Disi+RSI
for os_th, ob_th in [(25, 75), (30, 70), (35, 65)]:
    STRATEGIES_INTRADAY.append((f"E-Bollinger+RSI {os_th}/{ob_th}", make_bollinger_rsi(os_th, ob_th)))

# F: Gercek gun ici VWAP sapmasi
for dev_th in [0.3, 0.5, 0.8, 1.2]:
    STRATEGIES_INTRADAY.append((f"F-VWAP Sapmasi %{dev_th}", make_vwap_deviation(dev_th)))

# G: SMA20 sapmasi (VWAP proxy)
for dev_th in [1.0, 2.0, 3.0]:
    STRATEGIES_INTRADAY.append((f"G-SMA20 Sapmasi %{dev_th}", make_sma_deviation(dev_th)))

# H: EMA kesisimi (momentum)
STRATEGIES_INTRADAY.append(("H-EMA9/21 Kesisimi", make_ema_cross("ema9", "ema21")))
STRATEGIES_INTRADAY.append(("H-EMA20/50 Kesisimi", make_ema_cross("ema20", "ema50")))

# I: MACD kesisimi (momentum)
STRATEGIES_INTRADAY.append(("I-MACD Kesisimi", make_macd_cross()))

# J: ATR kirilimi (momentum)
for atr_mult in [1.0, 1.5, 2.0, 2.5]:
    STRATEGIES_INTRADAY.append((f"J-ATR Kirilimi x{atr_mult}", make_atr_breakout(atr_mult)))

# K: Stochastic (tersine donus)
for os_th, ob_th in [(10, 90), (20, 80)]:
    STRATEGIES_INTRADAY.append((f"K-Stochastic {os_th}/{ob_th}", make_stochastic(os_th, ob_th)))

# L: CCI (tersine donus)
for os_th, ob_th in [(-100, 100), (-150, 150), (-200, 200)]:
    STRATEGIES_INTRADAY.append((f"L-CCI {os_th}/{ob_th}", make_cci(os_th, ob_th)))

# M: Acilis araligi kirilimi (momentum, sadece gun ici)
STRATEGIES_INTRADAY.append(("M-Acilis Araligi Kirilimi", make_opening_range_breakout()))

# N: Gap fade (tersine donus, sadece gun ici, gunun ilk mumu)
for gap_th in [0.3, 0.5, 1.0]:
    STRATEGIES_INTRADAY.append((f"N-Gap Fade %{gap_th}", make_gap_fade(gap_th)))


# ---------------------------------------------------------------------------
# Backtest motoru
# ---------------------------------------------------------------------------

def run_backtest(df: pd.DataFrame, strategies: list, checkpoints: list, min_gap_bars: int = 3):
    """
    Her strateji icin df uzerinde yuruyup sinyalleri toplar. Her sinyal icin HAM
    (komisyonsuz) yuzde getiriyi saklar - NET (komisyonlu) hesap summarize()'da yapilir.
    """
    results = {name: [] for name, _ in strategies}
    max_checkpoint = max(c[0] for c in checkpoints)
    n = len(df)

    for name, fn in strategies:
        last_signal_i = -min_gap_bars - 1
        i = 25
        while i < n - max_checkpoint - 1:
            if i - last_signal_i < min_gap_bars:
                i += 1
                continue
            try:
                direction = fn(df, i)
            except Exception:
                direction = None
            if direction is None:
                i += 1
                continue

            entry_price = df.iloc[i]["close"]
            outcome_hit = False
            raw_pct = None
            for bars_ahead, label, target_pct in checkpoints:
                future_price = df.iloc[i + bars_ahead]["close"]
                r = (future_price - entry_price) / entry_price * 100
                pct = r if direction == "LONG" else -r
                if pct >= target_pct:
                    outcome_hit = True
                    raw_pct = pct
                    break
            if not outcome_hit:
                future_price = df.iloc[i + max_checkpoint]["close"]
                r = (future_price - entry_price) / entry_price * 100
                raw_pct = r if direction == "LONG" else -r

            results[name].append(raw_pct)
            last_signal_i = i
            i += 1

    return results


def summarize(results: dict, commission_pct: float = COMMISSION_PCT) -> pd.DataFrame:
    """Her strateji icin HEM ham (komisyonsuz) HEM net (komisyonlu) istatistikleri raporlar."""
    rows = []
    for name, raw_outcomes in results.items():
        if not raw_outcomes:
            rows.append({
                "strateji": name, "sinyal": 0,
                "isabet_net_%": None, "ort_net_%": None, "toplam_net_%": None,
                "ort_ham_%": None, "toplam_ham_%": None,
            })
            continue
        raw = np.array(raw_outcomes)
        net = raw - commission_pct
        rows.append({
            "strateji": name,
            "sinyal": len(raw),
            "isabet_net_%": round((net > 0).mean() * 100, 1),
            "ort_net_%": round(net.mean(), 3),
            "toplam_net_%": round(net.sum(), 1),
            "ort_ham_%": round(raw.mean(), 3),
            "toplam_ham_%": round(raw.sum(), 1),
        })
    out = pd.DataFrame(rows).sort_values("ort_net_%", ascending=False, na_position="last")
    return out


# ---------------------------------------------------------------------------
# Ana akis
# ---------------------------------------------------------------------------

def tournament_bist():
    print(f"\n=== BIST GUNLUK TURNUVA ({len(STRATEGIES_DAILY)} strateji) ===")
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
            time.sleep(0.3)
        except Exception as e:
            print(f"{ticker}: hata - {e}")

    table = summarize(combined)
    print("\n--- BIST SONUCLARI (ilk 20) ---")
    print(table.head(20).to_string(index=False))
    table.to_csv("stok_turnuva_bist.csv", index=False)
    send_telegram_message(
        "📊 BIST GUNLUK TURNUVA - EN IYI 15\n\n" + table.head(15).to_string(index=False)
    )
    return table


def tournament_us():
    print(f"\n=== ABD GUN ICI (15m) TURNUVA ({len(STRATEGIES_INTRADAY)} strateji) ===")
    combined = {name: [] for name, _ in STRATEGIES_INTRADAY}
    for ticker in US_TICKERS:
        try:
            df = fetch_intraday_df(ticker)
            if len(df) < 40:
                print(f"{ticker}: yetersiz veri, atlandi")
                continue
            df = compute_indicators(df)
            res = run_backtest(df, STRATEGIES_INTRADAY, US_CHECKPOINTS)
            for name, outcomes in res.items():
                combined[name].extend(outcomes)
            print(f"{ticker}: tamamlandi ({len(df)} mum)")
            time.sleep(0.3)
        except Exception as e:
            print(f"{ticker}: hata - {e}")

    table = summarize(combined)
    print("\n--- ABD GUN ICI SONUCLARI (ilk 25) ---")
    print(table.head(25).to_string(index=False))
    table.to_csv("stok_turnuva_abd.csv", index=False)
    send_telegram_message(
        "📊 ABD GUN ICI TURNUVA - EN IYI 20 (HAM/NET)\n\n" + table.head(20).to_string(index=False)
    )
    send_telegram_message(
        "📊 ABD GUN ICI TURNUVA - EN KOTU 10\n\n" + table.tail(10).to_string(index=False)
    )
    return table


def tournament_swing_generic(tickers: list, market_label: str, out_filename: str):
    print(f"\n=== {market_label} SWING (GUNLUK) TURNUVA ({len(STRATEGIES_DAILY)} strateji) ===")
    combined = {name: [] for name, _ in STRATEGIES_DAILY}
    for ticker in tickers:
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
            time.sleep(0.3)
        except Exception as e:
            print(f"{ticker}: hata - {e}")

    table = summarize(combined)
    print(f"\n--- {market_label} SWING SONUCLARI (ilk 15) ---")
    print(table.head(15).to_string(index=False))
    table.to_csv(out_filename, index=False)
    send_telegram_message(
        f"📊 {market_label} SWING TURNUVA - EN IYI 15\n\n" + table.head(15).to_string(index=False)
    )
    return table


def tournament_us_swing():
    return tournament_swing_generic(US_TICKERS, "ABD", "stok_turnuva_abd_swing.csv")


if __name__ == "__main__":
    if yf is None:
        raise RuntimeError("yfinance kurulu degil. 'pip install yfinance --break-system-packages' calistir.")
    total_strats = len(STRATEGIES_DAILY) + len(STRATEGIES_INTRADAY)
    send_telegram_message(
        f"🏁 Genisletilmis strateji turnuvasi basliyor.\n"
        f"BIST gunluk: {len(STRATEGIES_DAILY)} strateji | ABD gun ici: {len(STRATEGIES_INTRADAY)} strateji | "
        f"ABD swing: {len(STRATEGIES_DAILY)} strateji\n"
        f"Toplam calistirilacak strateji-piyasa kombinasyonu bu yuzden yuksek, biraz surebilir..."
    )
    tournament_bist()
    tournament_us()
    tournament_us_swing()
    finish_msg = f"✅ Turnuva tamamlandi - {datetime.now().isoformat()}"
    print(f"\n{finish_msg}")
    send_telegram_message(finish_msg)
