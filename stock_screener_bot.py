import os
import csv
import time
from datetime import datetime, date
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd
import requests
import yfinance as yf

TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN", "")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")

if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
    raise RuntimeError(
        "TELEGRAM_TOKEN ve TELEGRAM_CHAT_ID ortam degiskenleri tanimli degil. "
        "Railway'de Variables kismindan ekle."
    )

# ---------------------------------------------------------------------------
# Hisse listeleri
# ---------------------------------------------------------------------------

# BIST30 (yfinance ".IS" uzantisiyla). Endeks icerigi zaman zaman degisir,
# bu listeyi periyodik olarak gozden gecirmek gerekebilir.
BIST_TICKERS = [
    "AKBNK.IS", "ARCLK.IS", "ASELS.IS", "BIMAS.IS", "EKGYO.IS",
    "ENKAI.IS", "EREGL.IS", "FROTO.IS", "GARAN.IS", "GUBRF.IS",
    "HALKB.IS", "ISCTR.IS", "KCHOL.IS", "KOZAL.IS", "KRDMD.IS",
    "MGROS.IS", "ODAS.IS", "PETKM.IS", "PGSUS.IS", "SAHOL.IS",
    "SASA.IS", "SISE.IS", "TAVHL.IS", "TCELL.IS", "THYAO.IS",
    "TOASO.IS", "TUPRS.IS", "VAKBN.IS", "YKBNK.IS", "ALARK.IS",
]

# ABD'de likit, tanınan buyuk sirketler
US_TICKERS = [
    "AAPL", "MSFT", "GOOGL", "AMZN", "NVDA", "META", "TSLA", "BRK-B",
    "JPM", "V", "UNH", "MA", "HD", "PG", "COST", "XOM", "JNJ", "ABBV",
    "MRK", "AVGO", "PEP", "KO", "BAC", "WMT", "CRM", "ADBE", "AMD",
    "NFLX", "DIS", "CSCO", "ORCL", "INTC", "QCOM", "TXN", "PFE",
    "NKE", "MCD", "GS", "CAT", "BA",
]

# ---------------------------------------------------------------------------
# Ayarlar
# ---------------------------------------------------------------------------

RSI_PERIOD = 14
RSI_OVERSOLD = 30
RSI_OVERBOUGHT = 70
BOLLINGER_PERIOD = 20
BOLLINGER_STD = 2
WICK_RATIO_THRESHOLD = 0.35
VOLUME_MULTIPLIER = 1.5          # gunluk hacim, 20 gunluk ortalamanin bu kati olmali
INVALIDATION_ATR_BUFFER = 1.0    # gecersizlik seviyesi icin ATR'nin bu kati kadar tampon

# ABD gun ici tarama icin ayri esikler (15m mumlar, kriptoya benzer mantik)
INTRADAY_RSI_PERIOD = 6
INTRADAY_RSI_OVERSOLD = 25
INTRADAY_RSI_OVERBOUGHT = 75
INTRADAY_WICK_RATIO = 0.4
INTRADAY_VOLUME_MULTIPLIER = 1.8
INTRADAY_TREND_GAP_THRESHOLD = 3.0   # ust zaman diliminde (1h) EMA20/50 farki bu esigi gecerse "guclu trend"

# Kontrol saatleri (yerel piyasa saatine gore, DST otomatik yonetilir)
BIST_CHECK_HOUR, BIST_CHECK_MINUTE = 17, 35       # Europe/Istanbul
CHECK_WINDOW_MINUTES = 5
LOOP_INTERVAL_SECONDS = 120                        # her 2 dakikada bir kontrol

_last_bist_run_date = None
_us_candidates = {}  # ABD gun ici tukenme adaylari - onay mumu bekleniyor


# ---------------------------------------------------------------------------
# Telegram
# ---------------------------------------------------------------------------

def send_telegram_message(text: str):
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    payload = {"chat_id": TELEGRAM_CHAT_ID, "text": text, "parse_mode": "HTML"}
    try:
        requests.post(url, data=payload, timeout=15)
    except Exception as e:
        print(f"Telegram gonderim hatasi: {e}")


# ---------------------------------------------------------------------------
# Veri ve indikatorler
# ---------------------------------------------------------------------------

def fetch_daily_df(ticker: str, period: str = "6mo") -> pd.DataFrame:
    df = yf.Ticker(ticker).history(period=period, interval="1d")
    df = df.reset_index()
    df = df.rename(columns={
        "Date": "timestamp", "Open": "open", "High": "high",
        "Low": "low", "Close": "close", "Volume": "volume",
    })
    return df[["timestamp", "open", "high", "low", "close", "volume"]]


def fetch_intraday_df(ticker: str, interval: str = "15m", period: str = "5d") -> pd.DataFrame:
    df = yf.Ticker(ticker).history(period=period, interval=interval)
    df = df.reset_index()
    # yfinance intraday index kolonu "Datetime" olarak gelir
    df = df.rename(columns={
        "Datetime": "timestamp", "Date": "timestamp", "Open": "open", "High": "high",
        "Low": "low", "Close": "close", "Volume": "volume",
    })
    return df[["timestamp", "open", "high", "low", "close", "volume"]]


def compute_indicators(df: pd.DataFrame, rsi_period: int = RSI_PERIOD) -> pd.DataFrame:
    df["ema20"] = df["close"].ewm(span=20, adjust=False).mean()
    df["ema50"] = df["close"].ewm(span=50, adjust=False).mean()
    df["vol_sma20"] = df["volume"].rolling(20).mean()

    high_low = df["high"] - df["low"]
    high_close = (df["high"] - df["close"].shift()).abs()
    low_close = (df["low"] - df["close"].shift()).abs()
    tr = pd.concat([high_low, high_close, low_close], axis=1).max(axis=1)
    df["atr14"] = tr.rolling(14).mean()

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
    df["lower_wick_ratio"] = (df[["open", "close"]].min(axis=1) - df["low"]) / candle_range
    df["upper_wick_ratio"] = (df["high"] - df[["open", "close"]].max(axis=1)) / candle_range
    df["lower_wick_ratio"] = df["lower_wick_ratio"].fillna(0)
    df["upper_wick_ratio"] = df["upper_wick_ratio"].fillna(0)

    boll_mid = df["close"].rolling(BOLLINGER_PERIOD).mean()
    boll_std = df["close"].rolling(BOLLINGER_PERIOD).std()
    df["boll_upper"] = boll_mid + BOLLINGER_STD * boll_std
    df["boll_lower"] = boll_mid - BOLLINGER_STD * boll_std

    return df


def check_exhaustion(df: pd.DataFrame):
    """
    Son (henuz kapanmamis/kapanmaya yakin) gunun mumuna bakar.
    RSI asiri uc + fitil + hacim patlamasi birlikte tutmali.
    Donus: (direction, row) ya da None
    """
    if len(df) < max(BOLLINGER_PERIOD, 20) + 2:
        return None

    row = df.iloc[-1]  # gun ici, henuz kapanmamis ama kapanisa yakin veri
    volume_ratio = row["volume"] / row["vol_sma20"] if row["vol_sma20"] else 0
    if volume_ratio < VOLUME_MULTIPLIER:
        return None

    if row["lower_wick_ratio"] >= WICK_RATIO_THRESHOLD and row["rsi"] <= RSI_OVERSOLD:
        return "LONG", row
    if row["upper_wick_ratio"] >= WICK_RATIO_THRESHOLD and row["rsi"] >= RSI_OVERBOUGHT:
        return "SHORT", row

    return None


def compute_invalidation(direction: str, row) -> float:
    atr = row["atr14"] if pd.notna(row["atr14"]) else 0
    buffer = atr * INVALIDATION_ATR_BUFFER
    if direction == "LONG":
        return row["low"] - buffer
    return row["high"] + buffer


def score_bollinger(row) -> tuple:
    if pd.isna(row["boll_upper"]) or pd.isna(row["boll_lower"]):
        return 0, "veri yetersiz"
    if row["close"] <= row["boll_lower"]:
        return 1, "alt bant disinda"
    if row["close"] >= row["boll_upper"]:
        return 1, "ust bant disinda"
    return 0, "bant icinde"


def score_trend(df: pd.DataFrame, direction: str) -> tuple:
    """Hissenin kendi orta vadeli trendi tersine mi (20/50 EMA farki)."""
    row = df.iloc[-1]
    if pd.isna(row["ema50"]) or row["ema50"] == 0:
        return 0, "veri yetersiz"
    gap_pct = (row["ema20"] - row["ema50"]) / row["ema50"] * 100
    if direction == "LONG" and gap_pct <= -5:
        return -1, f"EMA farki {gap_pct:+.1f}% (guclu dususte, riskli)"
    if direction == "SHORT" and gap_pct >= 5:
        return -1, f"EMA farki {gap_pct:+.1f}% (guclu yukseliste, riskli)"
    return 0, f"EMA farki {gap_pct:+.1f}% (notr)"


# ---------------------------------------------------------------------------
# ABD gun ici mantik (15m mumlar, kriptoya benzer: kapi + onay mumu + trend filtresi)
# ---------------------------------------------------------------------------

def check_intraday_gate(df: pd.DataFrame):
    """Son KAPANMIS 15m muma bakar (df.iloc[-2]) - hala olusan mum degerlendirilmez."""
    if len(df) < max(BOLLINGER_PERIOD, 20) + 2:
        return None

    row = df.iloc[-2]
    volume_ratio = row["volume"] / row["vol_sma20"] if row["vol_sma20"] else 0
    if volume_ratio < INTRADAY_VOLUME_MULTIPLIER:
        return None

    if row["lower_wick_ratio"] >= INTRADAY_WICK_RATIO and row["rsi"] <= INTRADAY_RSI_OVERSOLD:
        return "LONG", row
    if row["upper_wick_ratio"] >= INTRADAY_WICK_RATIO and row["rsi"] >= INTRADAY_RSI_OVERBOUGHT:
        return "SHORT", row

    return None


def get_symbol_trend_intraday(ticker: str):
    """1 saatlik grafige bakarak hissenin kendi trendinin guclu olup olmadigini kontrol eder."""
    try:
        df1h = fetch_intraday_df(ticker, interval="1h", period="1mo")
        df1h = compute_indicators(df1h, rsi_period=INTRADAY_RSI_PERIOD)
        row = df1h.iloc[-2]
        if pd.isna(row["ema50"]) or row["ema50"] == 0:
            return "BILINMIYOR", 0.0
        gap_pct = (row["ema20"] - row["ema50"]) / row["ema50"] * 100
        if gap_pct <= -INTRADAY_TREND_GAP_THRESHOLD:
            return "GUCLU_DUSUS", gap_pct
        if gap_pct >= INTRADAY_TREND_GAP_THRESHOLD:
            return "GUCLU_YUKSELIS", gap_pct
        return "YATAY", gap_pct
    except Exception as e:
        print(f"{ticker} icin 1h trend alinamadi: {e}")
        return "BILINMIYOR", 0.0


def check_us_candidate_confirmation(ticker: str, df: pd.DataFrame):
    """Bekleyen bir ABD tukenme adayi varsa, en son kapanan mumun onaylayip onaylamadigina bakar."""
    candidate = _us_candidates.get(ticker)
    if not candidate:
        return None

    latest_row = df.iloc[-2]
    if latest_row["timestamp"] <= candidate["candle_time"]:
        return None

    direction = candidate["direction"]
    exhaustion_row = candidate["exhaustion_row"]
    del _us_candidates[ticker]

    confirmed = (
        (direction == "LONG" and bool(latest_row["is_bull"])) or
        (direction == "SHORT" and not bool(latest_row["is_bull"]))
    )
    status = "confirmed" if confirmed else "rejected"
    return (status, direction, latest_row, exhaustion_row)


# ---------------------------------------------------------------------------
# Loglama
# ---------------------------------------------------------------------------

SIGNAL_LOG_FILE = "stock_signal_history.csv"


def log_signal(ticker: str, market: str, direction: str, row, breakdown: list):
    file_exists = os.path.isfile(SIGNAL_LOG_FILE)
    with open(SIGNAL_LOG_FILE, "a", newline="") as f:
        writer = csv.writer(f)
        if not file_exists:
            writer.writerow([
                "date", "ticker", "market", "direction", "price", "rsi", "breakdown"
            ])
        writer.writerow([
            datetime.now().isoformat(), ticker, market, direction,
            row["close"], row["rsi"], " | ".join(breakdown)
        ])


# ---------------------------------------------------------------------------
# Tarama
# ---------------------------------------------------------------------------

def scan_bist(tickers: list, market_label: str):
    print(f"\n[{datetime.now().strftime('%H:%M:%S')}] {market_label} taramasi basliyor...")
    results = []

    for ticker in tickers:
        try:
            df = fetch_daily_df(ticker)
            if df.empty or len(df) < 25:
                print(f"{ticker}: yetersiz veri")
                continue
            df = compute_indicators(df)

            gate_result = check_exhaustion(df)
            if not gate_result:
                print(f"{ticker}: kriter yok")
                continue

            direction, row = gate_result

            breakdown = []
            pts, note = score_bollinger(row)
            breakdown.append(f"Bollinger: {note}")
            pts_trend, note_trend = score_trend(df, direction)
            breakdown.append(f"Kendi trendi: {note_trend}")

            if pts_trend < 0:
                print(f"{ticker}: {direction} tespit edildi ama kendi trendi tersine guclu, atlandi")
                continue

            invalidation = compute_invalidation(direction, row)
            log_signal(ticker, market_label, direction, row, breakdown)

            results.append({
                "ticker": ticker,
                "direction": direction,
                "price": row["close"],
                "rsi": row["rsi"],
                "invalidation": invalidation,
                "breakdown": breakdown,
            })

        except Exception as e:
            print(f"{ticker} hata: {e}")

    if results:
        lines = [f"📊 {market_label} - Kapanışa Yakın Tarama Sonuçları\n"]
        for r in results:
            yon_emoji = "🟢 LONG" if r["direction"] == "LONG" else "🔴 SHORT"
            lines.append(
                f"{yon_emoji} {r['ticker']}\n"
                f"Fiyat: {r['price']:.2f} | RSI: {r['rsi']:.1f}\n"
                f"Geçersizlik seviyesi: {r['invalidation']:.2f}\n"
                f"{' | '.join(r['breakdown'])}\n"
            )
        msg = "\n".join(lines)
        print(msg)
        send_telegram_message(msg)
    else:
        print(f"{market_label}: bugun kriterlere uyan hisse bulunamadi")
        send_telegram_message(f"📊 {market_label}: bugün kriterlere uyan hisse bulunamadı.")


def scan_us_intraday():
    """ABD piyasasi acikken her dongude cagrilir - kriptoya benzer kapi + onay mumu mantigi."""
    print(f"\n[{datetime.now().strftime('%H:%M:%S')}] ABD gun ici taramasi basliyor...")

    for ticker in US_TICKERS:
        try:
            df = fetch_intraday_df(ticker, interval="15m", period="5d")
            if df.empty or len(df) < 25:
                print(f"{ticker}: yetersiz veri")
                continue
            df = compute_indicators(df, rsi_period=INTRADAY_RSI_PERIOD)

            confirmation = check_us_candidate_confirmation(ticker, df)

            if confirmation is not None:
                status, direction, confirm_row, exhaustion_row = confirmation
                if status == "rejected":
                    print(f"{ticker}: aday onaylanmadi (beklenen yon {direction} degildi), iptal edildi")
                    continue
                row = confirm_row
            else:
                gate_result = check_intraday_gate(df)
                if not gate_result:
                    print(f"{ticker}: kriter yok")
                    continue

                direction, exhaustion_row = gate_result

                symbol_regime, symbol_gap = get_symbol_trend_intraday(ticker)
                if direction == "LONG" and symbol_regime == "GUCLU_DUSUS":
                    print(f"{ticker}: LONG tespit edildi ama kendi 1h trendi guclu dususte ({symbol_gap:+.1f}%), engellendi")
                    continue
                if direction == "SHORT" and symbol_regime == "GUCLU_YUKSELIS":
                    print(f"{ticker}: SHORT tespit edildi ama kendi 1h trendi guclu yukseliste ({symbol_gap:+.1f}%), engellendi")
                    continue

                _us_candidates[ticker] = {
                    "direction": direction,
                    "candle_time": exhaustion_row["timestamp"],
                    "exhaustion_row": exhaustion_row,
                }
                print(f"{ticker}: tukenme adayi olustu ({direction}), onay mumu bekleniyor")
                continue

            breakdown = []
            pts, note = score_bollinger(row)
            breakdown.append(f"Bollinger: {note}")

            invalidation = compute_invalidation(direction, exhaustion_row)
            log_signal(ticker, "ABD-gunici", direction, row, breakdown)

            yon_emoji = "🟢 LONG" if direction == "LONG" else "🔴 SHORT"
            msg = (
                f"{yon_emoji} {ticker} - ABD gün içi tükenme sinyali\n"
                f"✅ Onay mumu ile teyit edildi\n\n"
                f"Tükenme fiyatı: {exhaustion_row['close']:.2f} (RSI {exhaustion_row['rsi']:.1f})\n"
                f"Onay/Giriş fiyatı: {row['close']:.2f}\n"
                f"Geçersizlik seviyesi: {invalidation:.2f}\n"
                f"Zaman dilimi: 15m\n\n"
                f"{' | '.join(breakdown)}"
            )
            print(msg)
            send_telegram_message(msg)

        except Exception as e:
            print(f"{ticker} hata: {e}")


# ---------------------------------------------------------------------------
# Zamanlama
# ---------------------------------------------------------------------------

def _within_window(now, target_hour, target_minute, window_minutes):
    target_total = target_hour * 60 + target_minute
    now_total = now.hour * 60 + now.minute
    return 0 <= (now_total - target_total) < window_minutes


def run_forever():
    global _last_bist_run_date

    send_telegram_message(
        "BIST + ABD hisse tarama botu baslatildi.\n"
        f"BIST: {len(BIST_TICKERS)} hisse, her gun ~{BIST_CHECK_HOUR:02d}:{BIST_CHECK_MINUTE:02d} (Istanbul) taranacak.\n"
        f"ABD: {len(US_TICKERS)} hisse, piyasa acikken (9:30-16:00 New York saati) surekli taranacak."
    )

    while True:
        istanbul_now = datetime.now(ZoneInfo("Europe/Istanbul"))
        ny_now = datetime.now(ZoneInfo("America/New_York"))

        if istanbul_now.weekday() < 5:  # Pazartesi-Cuma
            if _within_window(istanbul_now, BIST_CHECK_HOUR, BIST_CHECK_MINUTE, CHECK_WINDOW_MINUTES):
                if _last_bist_run_date != istanbul_now.date():
                    scan_bist(BIST_TICKERS, "BIST")
                    _last_bist_run_date = istanbul_now.date()

        ny_minutes = ny_now.hour * 60 + ny_now.minute
        market_open = 9 * 60 + 30
        market_close = 16 * 60
        if ny_now.weekday() < 5 and market_open <= ny_minutes < market_close:
            scan_us_intraday()

        time.sleep(LOOP_INTERVAL_SECONDS)


if __name__ == "__main__":
    run_forever()
