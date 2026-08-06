"""
bist_m15_tournament.py — BIST GÜN İÇİ (M15) STRATEJİ TURNUVASI
===============================================================
Gemini onayi (2026-08-04): BIST icin gun ici bir strateji kolu kurmadan ONCE,
kripto tarafinda kullandigimiz turnuva metodolojisiyle 6 aday stratejiyi
gecmis veri uzerinde olcmek.

CALISTIRMA (Render, mobil dostu):
  1. Bu dosyayi repoya yukle.
  2. Render > Settings > Start Command'i gecici olarak degistir:
         python bist_m15_tournament.py
  3. Deploy et, sonuclar Telegram'a dussun (~15-40 dk).
  4. Start Command'i geri al: python main.py

=========================== METODOLOJI ===========================
Kripto turnuvasindaki "sonuclari IYIMSER GOSTERME" kurallari aynen gecerli:
  * ILERIYE BAKMA YOK: sinyal i. mumun KAPANISINDA uretilir, simulasyon
    i+1'den baslar. Sinyal mumunun kendi hareketi asla sayilmaz.
  * AYNI MUMDA HEM STOP HEM TP -> ZARAR sayilir. Mum ici hangisinin once
    oldugunu bilemeyiz; iyimser varsayim istatistikleri sistematik olarak
    sisirir ve kendimize yalan soylemis oluruz.
  * MALIYET her islemden dusulur (komisyon x2 + tahmini kayma), fiyat
    yuzdesinden R cinsine cevrilerek.
  * Sonuc R cinsinden BEKLENTI olarak raporlanir, sadece isabet orani degil.

=========================== BIST'E OZEL ===========================
  * SEANS SONU ZORUNLU KAPANIS: gun ici pozisyon geceye tasinmaz. Seans
    sonunda acik kalan pozisyon o gunun son mumunun kapanisindan kapatilir.
    Tasinsaydi bu artik "gun ici strateji" olmazdi (gap riski girer).
  * LONG ve SHORT AYRI RAPORLANIR: BIST'te bireysel yatirimci pratikte
    aciga satis yapamiyor. Toplamda pozitif gorunen ama karini SHORT'tan
    alan bir strateji Yahya icin KULLANILAMAZ - bunu gorebilmeliyiz.
  * VERI KISITI: yfinance 15 dakikalik veriyi yalnizca son ~60 gun icin
    veriyor. Ornek buyuklugu yeterli olabilir ama TEK BIR PIYASA REJIMINI
    kapsar. "Son 2 ayda calisti" ile "calisiyor" ayni sey degildir.
  * LIKIDITE: dusuk hacimli hisselerde M15 mumlari seyrek ve spread genis.
    Esigin altindakiler ayri isaretlenir.

CANLIYA GECIS ESIGI (Gemini onayli, 3 altin kural):
  1) Maliyet sonrasi POZITIF beklenti
  2) EN AZ 100 islem
  3) LONG tarafi TEK BASINA pozitif
Ucu birden saglanmiyorsa strateji canliya ALINMAZ.
"""

import os
import time
import requests
import numpy as np
import pandas as pd
import yfinance as yf

# ------------------------------------------------------------
# Ayarlar
# ------------------------------------------------------------
TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID")

TOURNAMENT_TICKER_LIMIT = int(os.environ.get("TOURNAMENT_TICKER_LIMIT", "100"))
RR_RATIO = float(os.environ.get("TOURNAMENT_RR", "2.0"))

# BIST maliyetleri kriptodan yuksek: komisyon + spread. Gun ici cok islem
# urettigi icin bu duyarlilik kritik.
FEE_PCT_PER_SIDE = float(os.environ.get("BIST_FEE_PCT", "0.05"))
SLIPPAGE_PCT = float(os.environ.get("BIST_SLIPPAGE_PCT", "0.05"))

# Likidite esigi: gunluk ortalama TL hacmi bunun altindaysa isaretlenir
MIN_DAILY_TURNOVER_TRY = float(os.environ.get("MIN_TURNOVER", "5000000"))

# Canliya gecis esikleri
MIN_TRADES_FOR_LIVE = int(os.environ.get("MIN_TRADES_FOR_LIVE", "100"))

ATR_PERIOD = 14

BIST_TICKERS = [
    "AEFES.IS", "AGHOL.IS", "AKBNK.IS", "AKCNS.IS", "AKFGY.IS", "AKSA.IS",
    "AKSEN.IS", "ALARK.IS", "ALBRK.IS", "ALFAS.IS", "ANSGR.IS", "ARCLK.IS",
    "ASELS.IS", "ASTOR.IS", "BAGFS.IS", "BERA.IS", "BIENY.IS", "BIMAS.IS",
    "BOBET.IS", "BRSAN.IS", "BRYAT.IS", "BUCIM.IS", "CANTE.IS", "CCOLA.IS",
    "CIMSA.IS", "CWENE.IS", "DOAS.IS", "DOHOL.IS", "ECILC.IS", "ECZYT.IS",
    "EGEEN.IS", "EKGYO.IS", "ENERY.IS", "ENJSA.IS", "ENKAI.IS", "EREGL.IS",
    "EUPWR.IS", "FROTO.IS", "GARAN.IS", "GESAN.IS", "GLYHO.IS", "GUBRF.IS",
    "GWIND.IS", "HALKB.IS", "HEKTS.IS", "IPEKE.IS", "ISCTR.IS", "ISGYO.IS",
    "ISMEN.IS", "IZMDC.IS", "KARSN.IS", "KCAER.IS", "KCHOL.IS", "KLSER.IS",
    "KONTR.IS", "KONYA.IS", "KORDS.IS", "KOZAA.IS", "KOZAL.IS", "KRDMD.IS",
    "MAVI.IS", "MGROS.IS", "MIATK.IS", "ODAS.IS", "OTKAR.IS", "OYAKC.IS",
    "PENTA.IS", "PETKM.IS", "PGSUS.IS", "QUAGR.IS", "SAHOL.IS", "SASA.IS",
    "SDTTR.IS", "SISE.IS", "SKBNK.IS", "SMRTG.IS", "SOKM.IS", "TAVHL.IS",
    "TCELL.IS", "THYAO.IS", "TKFEN.IS", "TOASO.IS", "TSKB.IS", "TTKOM.IS",
    "TTRAK.IS", "TUKAS.IS", "TUPRS.IS", "TURSG.IS", "ULKER.IS", "VAKBN.IS",
    "VESBE.IS", "VESTL.IS", "YEOTK.IS", "YKBNK.IS", "YYLGD.IS", "ZOREN.IS",
    "ARDYZ.IS", "KMPUR.IS", "AGROT.IS", "TABGD.IS",
]


def send_telegram_message(text: str):
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        print("(Telegram ayarli degil)\n" + text)
        return
    for i in range(0, len(text), 3900):
        try:
            requests.post(
                f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage",
                json={"chat_id": TELEGRAM_CHAT_ID, "text": text[i:i + 3900]},
                timeout=20,
            )
        except Exception as e:
            print(f"Telegram gonderilemedi: {e}")


# ------------------------------------------------------------
# Gostergeler
# ------------------------------------------------------------
def compute_atr(df, period=ATR_PERIOD):
    h, l, c = df["high"], df["low"], df["close"]
    tr = pd.concat([h - l, (h - c.shift()).abs(), (l - c.shift()).abs()], axis=1).max(axis=1)
    return tr.ewm(alpha=1 / period, adjust=False).mean()


def compute_rsi(series, period=14):
    delta = series.diff()
    gain = delta.clip(lower=0).ewm(alpha=1 / period, adjust=False).mean()
    loss = (-delta.clip(upper=0)).ewm(alpha=1 / period, adjust=False).mean()
    rs = gain / loss.replace(0, np.nan)
    return 100 - (100 / (1 + rs))


def compute_bollinger(df, period=20, mult=2.0):
    mid = df["close"].rolling(period).mean()
    std = df["close"].rolling(period).std()
    upper, lower = mid + std * mult, mid - std * mult
    width = (upper - lower) / mid.replace(0, np.nan) * 100
    return mid, upper, lower, width


def add_session_vwap(df):
    """Seans bazli VWAP - her gun sifirdan baslar. Kumulatif hesap yalnizca
    GECMIS mumlari kullandigi icin ileriye bakma olusmaz."""
    tp = (df["high"] + df["low"] + df["close"]) / 3
    pv = tp * df["volume"]
    g = df.groupby("session")
    df["vwap"] = g.apply(lambda x: (pv.loc[x.index].cumsum() /
                                    x["volume"].cumsum().replace(0, np.nan))
                         ).reset_index(level=0, drop=True)
    dev = tp - df["vwap"]
    df["vwap_dev_std"] = g["close"].transform(lambda s: s.expanding().std())
    df["vwap_dev"] = dev
    return df


def fetch_m15(ticker):
    df = yf.Ticker(ticker).history(period="60d", interval="15m")
    if df is None or df.empty:
        return pd.DataFrame()
    df = df.reset_index()
    df = df.rename(columns={"Datetime": "ts", "Date": "ts", "Open": "open",
                            "High": "high", "Low": "low", "Close": "close",
                            "Volume": "volume"})
    need = ["ts", "open", "high", "low", "close", "volume"]
    if not all(c in df.columns for c in need):
        return pd.DataFrame()
    df = df[need].copy()
    df["session"] = pd.to_datetime(df["ts"]).dt.date
    return df


# ------------------------------------------------------------
# STRATEJILER — her biri (index, yon, giris, stop) listesi doner
# Hepsi son KAPANMIS mumu kullanir; sinyal o mumun kapanisinda uretilir.
# Stop mantigi stratejiye ozgu, TP her zaman sabit R:R ile hesaplanir —
# boylece stratejiler ADIL sekilde karsilastirilir.
# ------------------------------------------------------------
def _valid(direction, entry, stop):
    if entry <= 0 or stop <= 0:
        return False
    if direction == "LONG" and stop >= entry:
        return False
    if direction == "SHORT" and stop <= entry:
        return False
    return abs(entry - stop) / entry >= 0.0015  # %0.15'ten dar stop = gurultu


def strat_rsi_extreme(df, period=14, low=30, high=70):
    df = df.assign(rsi=compute_rsi(df["close"], period), atr=compute_atr(df))
    out = []
    for i in range(period + ATR_PERIOD + 2, len(df) - 1):
        r = df.iloc[i]
        if pd.isna(r["rsi"]) or pd.isna(r["atr"]):
            continue
        if r["rsi"] < low:
            e, s = r["close"], r["low"] - r["atr"] * 0.5
            if _valid("LONG", e, s):
                out.append((i, "LONG", e, s))
        elif r["rsi"] > high:
            e, s = r["close"], r["high"] + r["atr"] * 0.5
            if _valid("SHORT", e, s):
                out.append((i, "SHORT", e, s))
    return out


def strat_opening_range(df, or_bars=2):
    """Seansin ilk or_bars mumunun yuksek/dusugu kirilirsa o yone girer."""
    out = []
    for _, day in df.groupby("session"):
        if len(day) < or_bars + 3:
            continue
        idx = day.index.tolist()
        opening = day.iloc[:or_bars]
        hi, lo = opening["high"].max(), opening["low"].min()
        if hi <= lo:
            continue
        fired = False
        for k in range(or_bars, len(day) - 1):
            if fired:
                break
            r = day.iloc[k]
            if r["close"] > hi and _valid("LONG", r["close"], lo):
                out.append((idx[k], "LONG", r["close"], lo))
                fired = True
            elif r["close"] < lo and _valid("SHORT", r["close"], hi):
                out.append((idx[k], "SHORT", r["close"], hi))
                fired = True
    return out


def strat_vwap_reversion(df, k=2.0):
    """Fiyat seans VWAP'indan k standart sapma uzaklasip geri donerse,
    VWAP'a dogru ortalamaya donus islemi."""
    d = add_session_vwap(df.copy())
    d["atr"] = compute_atr(d)
    out = []
    for i in range(ATR_PERIOD + 4, len(d) - 1):
        r = d.iloc[i]
        if pd.isna(r["vwap"]) or pd.isna(r["vwap_dev_std"]) or pd.isna(r["atr"]):
            continue
        if r["vwap_dev_std"] <= 0:
            continue
        z = r["vwap_dev"] / r["vwap_dev_std"]
        if z < -k and r["close"] > r["open"]:      # asiri asagi + donus mumu
            e, s = r["close"], r["low"] - r["atr"] * 0.5
            if _valid("LONG", e, s):
                out.append((i, "LONG", e, s))
        elif z > k and r["close"] < r["open"]:     # asiri yukari + donus mumu
            e, s = r["close"], r["high"] + r["atr"] * 0.5
            if _valid("SHORT", e, s):
                out.append((i, "SHORT", e, s))
    return out


def strat_bb_squeeze_breakout(df, lookback=50, pct=25, vol_mult=1.5, vol_ma=20):
    mid, up, low, width = compute_bollinger(df)
    d = df.assign(bb_up=up, bb_low=low, bb_width=width,
                  vol_ma=df["volume"].rolling(vol_ma).mean(), atr=compute_atr(df))
    out = []
    start = max(lookback + 20, vol_ma) + 2
    for i in range(start, len(d) - 1):
        r = d.iloc[i]
        if pd.isna(r["bb_width"]) or pd.isna(r["vol_ma"]) or pd.isna(r["atr"]):
            continue
        prior = d["bb_width"].iloc[i - lookback:i].dropna()
        if prior.empty:
            continue
        # Sikisma, kirilim mumundan ONCEKI mumda olculur (kirilim bantlari acar)
        if d["bb_width"].iloc[i - 1] > np.nanpercentile(prior, pct):
            continue
        if r["volume"] < r["vol_ma"] * vol_mult:
            continue
        pu, pl = d["bb_up"].iloc[i - 1], d["bb_low"].iloc[i - 1]
        if pd.isna(pu) or pd.isna(pl):
            continue
        if r["close"] > pu:
            e, s = r["close"], r["low"] - r["atr"] * 0.5
            if _valid("LONG", e, s):
                out.append((i, "LONG", e, s))
        elif r["close"] < pl:
            e, s = r["close"], r["high"] + r["atr"] * 0.5
            if _valid("SHORT", e, s):
                out.append((i, "SHORT", e, s))
    return out


def strat_liquidity_hunt(df, lookback=20, wick_atr=0.3, wick_ratio=0.5):
    d = df.assign(atr=compute_atr(df))
    out = []
    for i in range(lookback + ATR_PERIOD + 2, len(d) - 1):
        r = d.iloc[i]
        if pd.isna(r["atr"]):
            continue
        atr = r["atr"]
        win = d.iloc[i - lookback:i]        # igne mumu HARIC
        rh, rl = win["high"].max(), win["low"].min()
        if rh <= rl:
            continue
        crange = r["high"] - r["low"]
        if crange <= 0:
            continue
        uw = r["high"] - max(r["close"], r["open"])
        if r["high"] > rh + atr * wick_atr and r["close"] < rh and uw / crange >= wick_ratio:
            e, s = r["close"], r["high"] + atr * 0.2
            if _valid("SHORT", e, s):
                out.append((i, "SHORT", e, s))
            continue
        lw = min(r["close"], r["open"]) - r["low"]
        if r["low"] < rl - atr * wick_atr and r["close"] > rl and lw / crange >= wick_ratio:
            e, s = r["close"], r["low"] - atr * 0.2
            if _valid("LONG", e, s):
                out.append((i, "LONG", e, s))
    return out


def strat_volume_momentum(df, vol_mult=2.0, vol_ma=20):
    d = df.assign(vol_ma=df["volume"].rolling(vol_ma).mean(), atr=compute_atr(df))
    out = []
    for i in range(max(vol_ma, ATR_PERIOD) + 2, len(d) - 1):
        r = d.iloc[i]
        if pd.isna(r["vol_ma"]) or pd.isna(r["atr"]) or r["vol_ma"] <= 0:
            continue
        if r["volume"] < r["vol_ma"] * vol_mult:
            continue
        if r["close"] > r["open"]:
            e, s = r["close"], r["low"] - r["atr"] * 0.5
            if _valid("LONG", e, s):
                out.append((i, "LONG", e, s))
        elif r["close"] < r["open"]:
            e, s = r["close"], r["high"] + r["atr"] * 0.5
            if _valid("SHORT", e, s):
                out.append((i, "SHORT", e, s))
    return out


def strat_daily_trend_pullback(df, daily=None, ema_daily=50, ema_fast=20,
                               tol_pct=0.4, swing_lb=20):
    """Canli bottaki TREND motorunun turnuva karsiligi.
    Yon SON KAPANMIS GUNLUK mumdan (EMA50'ye gore), zamanlama M15'ten.
    Canli botta d.iloc[-1] (kapanmamis mum) kullanilmasi yon kararsizligina
    yol acmisti; burada bastan iloc[-2] mantigiyla kuruluyor - yani gun
    boyunca yon SABIT."""
    if daily is None or daily.empty or len(daily) < ema_daily + 5:
        return []
    dd = daily.copy()
    dd["ema"] = dd["close"].ewm(span=ema_daily, adjust=False).mean()
    drow = dd.iloc[-2]                      # SON KAPANMIS gunluk mum
    if pd.isna(drow["ema"]):
        return []
    if drow["close"] > drow["ema"]:
        bias = "LONG"
    elif drow["close"] < drow["ema"]:
        bias = "SHORT"
    else:
        return []

    d = df.assign(ema_fast=df["close"].ewm(span=ema_fast, adjust=False).mean(),
                  atr=compute_atr(df))
    out = []
    for i in range(max(ema_fast, swing_lb) + ATR_PERIOD + 2, len(d) - 1):
        r = d.iloc[i]
        if pd.isna(r["ema_fast"]) or pd.isna(r["atr"]):
            continue
        tol = r["close"] * (tol_pct / 100)
        near = (abs(r["low"] - r["ema_fast"]) <= tol or
                abs(r["high"] - r["ema_fast"]) <= tol)
        if not near:
            continue
        win = d.iloc[i - swing_lb:i]
        if bias == "LONG" and r["close"] > r["open"]:
            e, st = r["close"], min(win["low"].min(), r["low"]) - r["atr"] * 0.3
            if _valid("LONG", e, st):
                out.append((i, "LONG", e, st))
        elif bias == "SHORT" and r["close"] < r["open"]:
            e, st = r["close"], max(win["high"].max(), r["high"]) + r["atr"] * 0.3
            if _valid("SHORT", e, st):
                out.append((i, "SHORT", e, st))
    return out


# Parametre kombinasyonlari BILEREK az tutuldu: yuzlerce kombinasyon
# denenirse icinden biri sans eseri iyi cikar ve biz onu "kesif" saniriz.
STRATEGIES = [
    ("RSI Aşırı Uç",        lambda d, dl=None: strat_rsi_extreme(d, 14, 30, 70)),
    ("RSI Aşırı Uç (sıkı)", lambda d, dl=None: strat_rsi_extreme(d, 14, 25, 75)),
    ("Açılış Kırılımı",     lambda d, dl=None: strat_opening_range(d, 2)),
    ("VWAP Sapması",        lambda d, dl=None: strat_vwap_reversion(d, 2.0)),
    ("Hacim Momentum",      lambda d, dl=None: strat_volume_momentum(d, 2.0)),

    # ---- BREAKOUT KALIBRASYONU (2026-08-06, Gemini'nin talebi) ----
    # Canlida BREAKOUT motoru HIC tetiklenmedi; sentetik testte de 300
    # denemede sifir cikti. Sikisma esigi kriptonun oynaklik yapisina gore
    # ayarlanmisti, hisse senedinde neredeyse hic olusmuyor.
    # Parametreyi "kafamiza gore" gevsetmek yerine birkac esigi olcuyoruz.
    # KOMBINASYON SAYISI BILEREK AZ: yuzlerce varyant denenirse icinden biri
    # sans eseri iyi cikar ve onu kesif saniriz - bu projede daha once
    # dusulen tuzak. Burada 5 varyant var, hepsi ayni yonde tek bir soruyu
    # soruyor: "esik ne kadar gevserse bu motor calismaya baslar?"
    ("BB Sıkışma %25 / hac 1.5",  lambda d, dl=None: strat_bb_squeeze_breakout(d, pct=25, vol_mult=1.5)),
    ("BB Sıkışma %40 / hac 1.5",  lambda d, dl=None: strat_bb_squeeze_breakout(d, pct=40, vol_mult=1.5)),
    ("BB Sıkışma %40 / hac 1.2",  lambda d, dl=None: strat_bb_squeeze_breakout(d, pct=40, vol_mult=1.2)),
    ("BB Sıkışma %60 / hac 1.2",  lambda d, dl=None: strat_bb_squeeze_breakout(d, pct=60, vol_mult=1.2)),
    ("BB Sıkışma %60 / hac 1.0",  lambda d, dl=None: strat_bb_squeeze_breakout(d, pct=60, vol_mult=1.0)),

    # ---- LIKIDITE AVCISI KALIBRASYONU ----
    # Canlida yalnizca YATAY rejimde calistigi icin pratikte cok az tetikleniyor.
    ("Likidite iğne 0.3×ATR",     lambda d, dl=None: strat_liquidity_hunt(d, wick_atr=0.3, wick_ratio=0.5)),
    ("Likidite iğne 0.2×ATR",     lambda d, dl=None: strat_liquidity_hunt(d, wick_atr=0.2, wick_ratio=0.4)),
    ("Likidite iğne 0.1×ATR",     lambda d, dl=None: strat_liquidity_hunt(d, wick_atr=0.1, wick_ratio=0.35)),

    # ---- TREND MOTORU (canlida tek calisan motor - edge'i var mi?) ----
    ("Günlük Trend + M15 pullback",       lambda d, dl=None: strat_daily_trend_pullback(d, dl, tol_pct=0.4)),
    ("Günlük Trend + M15 (geniş tol)",    lambda d, dl=None: strat_daily_trend_pullback(d, dl, tol_pct=0.8)),
]


# ------------------------------------------------------------
# Simulasyon
# ------------------------------------------------------------
def simulate(df, signals):
    """(yon, R_sonuc) listesi doner. 1R = stop mesafesi."""
    results = []
    sessions = df["session"].values
    for idx, direction, entry, stop in signals:
        risk = abs(entry - stop)
        if risk <= 0:
            continue
        tp = entry + risk * RR_RATIO if direction == "LONG" else entry - risk * RR_RATIO
        day = sessions[idx]

        outcome = None
        j = idx + 1
        while j < len(df) and sessions[j] == day:   # SADECE ayni seans icinde
            bar = df.iloc[j]
            if direction == "LONG":
                hit_stop, hit_tp = bar["low"] <= stop, bar["high"] >= tp
            else:
                hit_stop, hit_tp = bar["high"] >= stop, bar["low"] <= tp
            if hit_stop:            # ayni mumda ikisi de olduysa ZARAR
                outcome = -1.0
                break
            if hit_tp:
                outcome = RR_RATIO
                break
            j += 1

        if outcome is None:
            # SEANS SONU ZORUNLU KAPANIS - gun ici pozisyon geceye tasinmaz
            last = df.iloc[min(j, len(df) - 1) - 1]["close"] if j > idx + 1 else entry
            pnl = (last - entry) if direction == "LONG" else (entry - last)
            outcome = pnl / risk

        cost_pct = (FEE_PCT_PER_SIDE * 2 + SLIPPAGE_PCT) / 100
        outcome -= (cost_pct * entry) / risk
        results.append((direction, outcome))
    return results


def summarize(results):
    if not results:
        return None
    arr = np.array([r for _, r in results])
    wins = int((arr > 0).sum())
    return {"n": len(arr), "win_rate": wins / len(arr) * 100,
            "exp": float(arr.mean()), "total": float(arr.sum())}


def fmt(label, s):
    if not s:
        return f"    {label}: işlem yok"
    return (f"    {label}: {s['n']} işlem | isabet %{s['win_rate']:.1f} | "
            f"beklenti {s['exp']:+.3f}R | toplam {s['total']:+.1f}R")


def main():
    tickers = BIST_TICKERS[:TOURNAMENT_TICKER_LIMIT]
    print(f"TURNUVA BASLIYOR: {len(tickers)} hisse", flush=True)
    send_telegram_message(
        "🏁 [BIST M15 TURNUVA] Başladı.\n"
        f"{len(tickers)} hisse | 60 günlük 15dk verisi | {len(STRATEGIES)} strateji\n"
        f"Hedef: 1:{RR_RATIO:g} R:R | Maliyet: %{FEE_PCT_PER_SIDE}×2 + %{SLIPPAGE_PCT} kayma\n"
        "Bu 15-40 dakika sürebilir..."
    )

    agg = {name: [] for name, _ in STRATEGIES}
    ok, failed, illiquid = 0, [], []

    for n, tk in enumerate(tickers, 1):
        try:
            df = fetch_m15(tk)
            if df.empty or len(df) < 150:
                failed.append(tk)
                continue
            # TREND stratejisi gunluk veriye ihtiyac duyuyor - ticker basina
            # BIR KEZ cekilir, tum stratejilere ayni veri gecilir.
            try:
                daily = yf.Ticker(tk).history(period="1y", interval="1d")
                daily = daily.reset_index().rename(columns={
                    "Date": "ts", "Open": "open", "High": "high",
                    "Low": "low", "Close": "close", "Volume": "volume"})
            except Exception:
                daily = pd.DataFrame()

            turnover = float((df["close"] * df["volume"]).mean()) * 32
            if turnover < MIN_DAILY_TURNOVER_TRY:
                illiquid.append(tk)
            for name, fn in STRATEGIES:
                try:
                    agg[name] += simulate(df, fn(df, daily))
                except Exception as e:
                    print(f"{tk} / {name}: {e}")
            ok += 1
            print(f"[{n}/{len(tickers)}] {tk} tamam", flush=True)
        except Exception as e:
            failed.append(tk)
            print(f"[{n}/{len(tickers)}] {tk} HATA: {e}", flush=True)
        time.sleep(0.4)

    lines = ["🏆 [BIST M15 TURNUVA SONUÇLARI]",
             f"Taranan: {ok}/{len(tickers)} hisse | Hedef 1:{RR_RATIO:g} R:R | maliyet dahil", ""]

    ranked = []
    for name, _ in STRATEGIES:
        res = agg[name]
        overall = summarize(res)
        longs = summarize([r for r in res if r[0] == "LONG"])
        shorts = summarize([r for r in res if r[0] == "SHORT"])
        lines.append(f"▸ {name}")
        lines.append(fmt("TOPLAM", overall))
        lines.append(fmt("LONG  ", longs))
        lines.append(fmt("SHORT ", shorts))

        # 3 ALTIN KURAL (Gemini onayli canliya gecis esigi)
        rules = []
        rules.append(("maliyet sonrası pozitif", bool(overall and overall["exp"] > 0)))
        rules.append((f"≥{MIN_TRADES_FOR_LIVE} işlem", bool(overall and overall["n"] >= MIN_TRADES_FOR_LIVE)))
        rules.append(("LONG tek başına pozitif", bool(longs and longs["exp"] > 0)))
        passed = all(v for _, v in rules)
        detail = ", ".join(f"{'✅' if v else '❌'} {k}" for k, v in rules)
        lines.append(f"    {'✅ CANLIYA UYGUN' if passed else '❌ CANLIYA UYGUN DEĞİL'} → {detail}")
        lines.append("")
        if overall:
            ranked.append((overall["exp"], name, passed))

    if ranked:
        ranked.sort(reverse=True)
        winners = [n for _, n, p in ranked if p]
        if winners:
            lines.append(f"✅ Üç kuralı da geçen: {', '.join(winners)}")
        else:
            lines.append("⚠️ HİÇBİR STRATEJİ ÜÇ KURALI BİRDEN GEÇEMEDİ.")
            lines.append("Bu bir başarısızlık değil, doğru cevap: test edilmemiş bir "
                         "gün içi kolunu canlıya almaktan çok daha iyidir.")

    lines.append("")
    lines.append("ℹ️ UYARI: Bu veri seti yalnızca son ~60 günü, yani TEK BİR "
                 "piyasa rejimini kapsar. 'Son 2 ayda çalıştı' ile 'çalışıyor' "
                 "aynı şey değildir.")
    if illiquid:
        lines.append(f"ℹ️ Düşük hacimli ({len(illiquid)} hisse) — spread geniş olabilir, "
                     f"gerçek maliyet burada tahminden yüksektir.")
    if failed:
        lines.append(f"ℹ️ Veri alınamayan: {len(failed)} hisse")

    msg = "\n".join(lines)
    send_telegram_message(msg)
    print(msg)


if __name__ == "__main__":
    # Render Web Service, sureci canli saymak icin bir PORT dinlemesini
    # bekliyor. Turnuva script'i normalde sadece hesap yapip bitiyor ve hic
    # port acmiyordu; bu yuzden Render "no open ports detected" deyip
    # SURECI SONLANDIRIYORDU - turnuva bitmeden kesiliyordu.
    # Cozum: arka planda kucuk bir HTTP sunucusu acip Render'i memnun etmek.
    # Turnuva ana thread'de calismaya devam eder.
    import threading
    from http.server import BaseHTTPRequestHandler, HTTPServer

    class _Ping(BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b"turnuva calisiyor")

        def log_message(self, *a):
            pass  # Render loglarini doldurmasin

    def _serve():
        try:
            HTTPServer(("0.0.0.0", int(os.environ.get("PORT", "10000"))), _Ping).serve_forever()
        except Exception as e:
            print(f"Port dinleyici acilamadi: {e}")

    threading.Thread(target=_serve, daemon=True).start()

    # KENDI KENDINE PING (2026-08-06): Render ucretsiz plani 15 dakika
    # DISARIDAN istek almazsa servisi uyutuyor. Ana bot bunu keep_awake ile
    # cozuyordu; turnuva script'inde yoktu ve turnuva ortasinda servis
    # uyutuldugu icin islem sessizce durdu (son log 23:11, sonrasi bos).
    # Turnuva 40+ dakika surdugu icin bu sart.
    def _keep_awake():
        url = os.environ.get("RENDER_EXTERNAL_URL")
        if not url:
            return
        target = url.rstrip("/")
        time.sleep(60)
        while True:
            try:
                requests.get(target, timeout=20)
            except Exception:
                pass
            time.sleep(600)

    threading.Thread(target=_keep_awake, daemon=True).start()

    main()
    # Turnuva bitti ama sureci hemen kapatmiyoruz: Render bunu cokme sayip
    # yeniden baslatir ve turnuva bastan koser. Kullanici Start Command'i
    # geri alana kadar bekliyoruz.
    print("Turnuva tamamlandi. Start Command'i 'python main.py' olarak geri alabilirsin.")
    while True:
        time.sleep(3600)
