"""
radar_canli.py — CANLI ÖNCÜL RADAR (SİNYAL-AMAÇLI, EMİR AÇMAZ)
================================================================
radar_onculu_test.py'nin (2026-08-07/08) test edilmiş 3 bileşenini
(hacim artışı, kırılım, endeksten ayrışma) AYNEN kullanır — o testte
ilk kez pozitif sonuç bulunmuştu: filtreyi geçenlerde +%2'ye ulaşma
ihtimali kontrol grubuna göre ~2 katıydı (%24.1 vs %12.5, 39 BIST hissesi).

BU MODÜLÜN FARKI: o test KAP/haber bileşenini test EDEMEMİŞTİ (ücretsiz
kaynak yoktu). Artık kap_monitor.py gerçek KAP verisi biriktiriyor —
bu modül onu 4. bileşen olarak KULLANIR ama ZORUNLU KILMAZ: her sinyal
KAP doğrulamalı/doğrulamasız etiketiyle ayrı ayrı loglanır, böylece
KAP'ın gerçekten fark yaratıp yaratmadığı canlı veriyle ölçülebilir.

KRİTİK, ÖNCEKİ HATADAN DERS (rapor 17): M15/H1 turnuvaları ve exit
testi, filtrenin "olasılık kaydırma" gücü var olsa bile SABİT 1:2 R:R
altında para kazandırmadığını gösterdi (başabaş oranı %33.3, filtre
en iyi ihtimalle ~%24 üretiyor). O yüzden bu modül SABİT TP/STOP
KOYMAZ — tıpkı öncül testin ölçtüğü gibi, tetiklenmeden seans sonuna
kadar max yukarı / max aşağı / seans sonu yüzdesini kaydeder. Çıkış
kuralı kararı, bu ham veri biriktikten SONRA verilir.

ABD DESTEĞİ (2026-08-09): aynı 3 teknik bileşen SPY kıyaslı olarak ABD
hisselerine de uygulanıyor — DEĞİŞTİRİLMEDEN, kullanıcının açık isteğiyle
ayrıca test edilmeden. Açık uyarı: filtre yalnızca BIST verisiyle
doğrulanmıştı (radar_onculu_test.py, 39 BIST hissesi); ABD tarafında
BAŞTAN TEST EDİLMEDEN canlıya alınıyor. KAP bileşeni ABD'de yok (KAP
Türkiye'ye özgü) — ABD sinyalleri sadece 3 teknik bileşenle üretiliyor,
KAP etiketi hep boş/yok.

İZOLASYON: sinyal üretir ama HİÇBİR EMİR AÇMAZ (bu bot zaten hiç emir
açmıyor), hiçbir mevcut taramayı etkilemez/durdurmaz, kendi dosyalarına
yazar, kendi try/except'i ve zamanlayıcısıyla çalışır. stock_screener_bot.py
bu modülü de try/except içinde import eder.
"""

import os
import csv
import time
from datetime import datetime, date, timezone
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd
import requests
import yfinance as yf

DATA_DIR = os.environ.get("DATA_DIR", ".")


def _data_path(filename: str) -> str:
    return os.path.join(DATA_DIR, filename)


TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN", "")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")

RADAR_ENABLED = os.environ.get("RADAR_ENABLED", "true").lower() == "true"
RADAR_SCAN_INTERVAL_MINUTES = int(os.environ.get("RADAR_SCAN_INTERVAL_MINUTES", "15"))

# --- radar_onculu_test.py ile AYNI eşikler, değiştirilmedi ---
VOLUME_MULT = float(os.environ.get("RADAR_VOLUME_MULT", "2.0"))
BREAKOUT_LOOKBACK = int(os.environ.get("RADAR_BREAKOUT_LOOKBACK", "20"))
CATCH_MIN_PCT = float(os.environ.get("RADAR_CATCH_MIN", "0.5"))
CATCH_MAX_PCT = float(os.environ.get("RADAR_CATCH_MAX", "1.0"))

# KAP eşleşmesi: kod, bugün ve son X dakika içinde kap_monitor logunda
# görülmüş mü? Öncül testte ölçülemeyen bileşen artık BURADA, gerçek
# veriyle kontrol ediliyor - ama sinyali ENGELLEMİYOR, sadece etiketliyor.
# Sadece BIST için - ABD'de KAP karşılığı yok.
KAP_MATCH_WINDOW_MINUTES = int(os.environ.get("RADAR_KAP_MATCH_WINDOW_MINUTES", "240"))

INDEX_TICKERS = {"BIST": "XU100.IS", "US": "SPY"}

VOL_CACHE_HOURS = 20  # gunluk hacim ortalamasi - gun icinde degismez, gunde 1 yeniler
_vol_ma_cache = {}    # ticker -> (deger, hesaplanma_zamani)

SIGNAL_LOG_FILE = _data_path("radar_canli_signals.csv")
SIGNAL_FIELDS = ["market", "ticker", "gun", "entry_time", "entry_price", "day_open_pct",
                  "hacim_ok", "kirilim_ok", "ayrisma_ok", "kap_var", "kap_kaynak",
                  "status", "max_up_pct", "max_down_pct", "session_end_pct"]

_triggered_today = {}  # (market, ticker, gun) -> True, ayni gun tekrar tetiklenmesin
_last_scan_time = {}   # market -> zaman, her market kendi zamanlayicisinda


def send_telegram_message(text: str):
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        return
    try:
        requests.post(
            f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage",
            json={"chat_id": TELEGRAM_CHAT_ID, "text": text[:3900]}, timeout=20)
    except Exception as e:
        print(f"radar_canli telegram gonderilemedi: {e}")


def bist_is_open(now_ist=None) -> bool:
    now_ist = now_ist or datetime.now(ZoneInfo("Europe/Istanbul"))
    if now_ist.weekday() >= 5:
        return False
    minutes = now_ist.hour * 60 + now_ist.minute
    return 10 * 60 <= minutes < 18 * 60


def us_is_open(now_et=None) -> bool:
    now_et = now_et or datetime.now(ZoneInfo("America/New_York"))
    if now_et.weekday() >= 5:
        return False
    minutes = now_et.hour * 60 + now_et.minute
    return 9 * 60 + 30 <= minutes < 16 * 60


def _market_is_open(market: str) -> bool:
    return bist_is_open() if market == "BIST" else us_is_open()


def _today_str(market: str) -> str:
    tz = ZoneInfo("Europe/Istanbul") if market == "BIST" else ZoneInfo("America/New_York")
    return datetime.now(tz).date().isoformat()


# ---------------------------------------------------------------------------
# Veri cekme
# ---------------------------------------------------------------------------

def _fetch_15m(ticker: str, period: str = "2d") -> pd.DataFrame:
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


def _get_vol_ma(ticker: str):
    """Gunluk 15dk-bar hacim ortalamasi - agir fetch (60d), gunde 1 kez
    yenilenir. Onculu testteki 'vol_ma' ile ayni mantik, ama canlida her
    dongude tekrar cekmek yerine onbelleklenir."""
    cached = _vol_ma_cache.get(ticker)
    if cached is not None:
        deger, zaman = cached
        if (datetime.now() - zaman).total_seconds() < VOL_CACHE_HOURS * 3600:
            return deger
    try:
        df = yf.Ticker(ticker).history(period="60d", interval="15m")
        if df is None or df.empty or "Volume" not in df.columns:
            return None
        deger = float(df["Volume"].tail(20 * 32).mean())
    except Exception:
        return None
    _vol_ma_cache[ticker] = (deger, datetime.now())
    return deger


def _get_index_today_pct(market: str, today: str) -> float:
    """Endeksin (BIST->XU100, US->SPY) bugunku gun ici yuzde degisimi,
    mevcut bara en yakin."""
    df = _fetch_15m(INDEX_TICKERS[market], period="2d")
    if df.empty:
        return None
    gun = df[df["session"].astype(str) == today]
    if gun.empty:
        return None
    acilis = float(gun.iloc[0]["open"])
    if acilis <= 0:
        return None
    son = float(gun.iloc[-1]["close"])
    return (son - acilis) / acilis * 100


# ---------------------------------------------------------------------------
# KAP eslesmesi (kap_monitor.py'nin biriktirdigi gercek veriden) - sadece BIST
# ---------------------------------------------------------------------------

def _kap_match(ticker: str):
    """kap_monitor.py'nin logunda bu hissenin kodu, son KAP_MATCH_WINDOW_MINUTES
    icinde gorulmus mu? (kod, kaynak) donuyor, yoksa (False, None)."""
    try:
        import kap_monitor as km
    except Exception:
        return False, None
    kod = ticker.replace(".IS", "")
    try:
        rows = km._read_log()
    except Exception:
        return False, None
    now = datetime.now(timezone.utc)
    for r in rows:
        if r.get("kod") != kod or not r.get("gorulme_time"):
            continue
        try:
            gorulme = datetime.fromisoformat(r["gorulme_time"])
        except Exception:
            continue
        if (now - gorulme).total_seconds() / 60 <= KAP_MATCH_WINDOW_MINUTES:
            return True, r.get("kaynak", "?")
    return False, None


# ---------------------------------------------------------------------------
# Sinyal loglama + sonuc takibi
# ---------------------------------------------------------------------------

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


def check_outcomes():
    """Acik (status=OPEN) sinyallerin fiyatini gunceller (BIST+US birlikte).
    Seans bittiyse kapatir. Sabit TP/stop YOK - sadece max yukari/asagi/
    seans sonu izlenir."""
    rows = _read_signals()
    if not rows:
        return
    changed = False
    for r in rows:
        if r["status"] != "OPEN":
            continue
        market = r.get("market", "BIST")
        ticker = r["ticker"]
        today = _today_str(market)
        market_open = _market_is_open(market)
        try:
            df = _fetch_15m(ticker, period="2d")
            gun = df[df["session"].astype(str) == r["gun"]]
            if gun.empty:
                continue
            entry_price = float(r["entry_price"])
            max_up = (float(gun["high"].max()) - entry_price) / entry_price * 100
            max_down = (float(gun["low"].min()) - entry_price) / entry_price * 100
            r["max_up_pct"] = f"{max_up:.2f}"
            r["max_down_pct"] = f"{max_down:.2f}"
            if r["gun"] != today or not market_open:
                # seans kapandi (bugun degilse ya da bugun ama seans bitti)
                r["session_end_pct"] = f"{(float(gun.iloc[-1]['close']) - entry_price) / entry_price * 100:.2f}"
                r["status"] = "CLOSED"
            changed = True
        except Exception:
            continue
    if changed:
        _write_signals(rows)


# ---------------------------------------------------------------------------
# Tarama
# ---------------------------------------------------------------------------

def scan(tickers: list, market: str = "BIST"):
    """Her tetiklenmede radar_onculu_test.py ile AYNI 3 teknik bileseni
    kontrol eder (BIST icin ayrica KAP eslesmesini etiket olarak ekler).
    Sabit TP/stop yok, sadece bildirim + sonuc takibi. Ayni hisse ayni
    gun sadece 1 kez, market bazinda ayri."""
    if not RADAR_ENABLED:
        print(f"[RADAR] {market}: RADAR_ENABLED=false, atlandi", flush=True)
        return
    if not _market_is_open(market):
        print(f"[RADAR] {market}: piyasa kapali, atlandi", flush=True)
        return

    today = _today_str(market)
    index_pct = _get_index_today_pct(market, today)
    if index_pct is None:
        print(f"[RADAR] {market}: endeks verisi alinamadi, bu tur atlandi", flush=True)
        return

    taranan = 0
    sinyal = 0
    for ticker in tickers:
        key = (market, ticker, today)
        if _triggered_today.get(key):
            continue
        taranan += 1
        try:
            df = _fetch_15m(ticker, period="2d")
            gun = df[df["session"].astype(str) == today]
            if len(gun) < 3:
                continue
            acilis = float(gun.iloc[0]["open"])
            if acilis <= 0:
                continue
            r = gun.iloc[-1]
            fiyat = float(r["close"])
            gun_ici_pct = (fiyat - acilis) / acilis * 100

            if not (CATCH_MIN_PCT <= gun_ici_pct <= CATCH_MAX_PCT):
                continue

            vol_ma = _get_vol_ma(ticker)
            hacim_ok = bool(vol_ma and vol_ma > 0 and float(r["volume"]) >= vol_ma * VOLUME_MULT)

            onceki = gun.iloc[max(0, len(gun) - 1 - BREAKOUT_LOOKBACK):-1]
            kirilim_ok = bool(not onceki.empty and fiyat > float(onceki["high"].max()))

            ayrisma_ok = bool(gun_ici_pct > index_pct)

            if not (hacim_ok and kirilim_ok and ayrisma_ok):
                continue  # 3 teknik bilesen sarti - onculu testteki "gecti" tanimi

            if market == "BIST":
                kap_var, kap_kaynak = _kap_match(ticker)
            else:
                kap_var, kap_kaynak = False, None  # ABD'de KAP karsiligi yok

            _triggered_today[key] = True
            sinyal += 1
            _append_signal({
                "market": market, "ticker": ticker, "gun": today,
                "entry_time": datetime.now().isoformat(),
                "entry_price": f"{fiyat:.4f}", "day_open_pct": f"{gun_ici_pct:.2f}",
                "hacim_ok": hacim_ok, "kirilim_ok": kirilim_ok, "ayrisma_ok": ayrisma_ok,
                "kap_var": kap_var, "kap_kaynak": kap_kaynak or "",
                "status": "OPEN", "max_up_pct": "", "max_down_pct": "", "session_end_pct": "",
            })
            print(f"[RADAR] {market} SİNYAL: {ticker} @ {fiyat:.2f} ({gun_ici_pct:+.2f}%)", flush=True)

            index_adi = "XU100" if market == "BIST" else "SPY"
            if market == "BIST":
                etiket = f"🔴 KAP DOĞRULANMIŞ ({kap_kaynak})" if kap_var else "⚪ Teknik (KAP eşleşmedi)"
            else:
                etiket = "🇺🇸 ABD — KAP karşılığı yok, sadece teknik"
            uyari_ekstra = (
                "" if market == "BIST" else
                "\n⚠️ Bu filtre YALNIZCA BIST verisiyle test edilmişti — ABD "
                "tarafında ayrıca test edilmeden canlıya alındı."
            )
            send_telegram_message(
                f"🔬 [ÖNCÜL RADAR — {market}] {ticker}\n"
                f"Gün içi: {gun_ici_pct:+.2f}% | Fiyat: {fiyat:.2f}\n"
                f"Hacim ≥{VOLUME_MULT:g}× ✅ | Kırılım ✅ | {index_adi}'ten ayrışma ✅\n"
                f"{etiket}\n"
                "ℹ️ SİNYAL AMAÇLIDIR — emir talimatı DEĞİLDİR, sabit hedef/stop "
                "YOKTUR. Öncül testte (BIST, 39 hisse) bu filtre +%2'ye ulaşma "
                "ihtimalini kontrol grubuna göre ~2 katına çıkarmıştı (%24.1 vs "
                "%12.5). Bu sinyal sonuç takibine alındı, /radar ile sorgulanabilir."
                f"{uyari_ekstra}"
            )
        except Exception as e:
            print(f"[RADAR] {market} {ticker}: hata - {e}", flush=True)
            continue  # bu radar izole - tek hisse hatasi taramayi durdurmaz

    print(f"[RADAR] {market}: tur bitti, {taranan} hisse tarandı, {sinyal} sinyal", flush=True)


def maybe_scan(tickers: list, market: str = "BIST"):
    if not RADAR_ENABLED:
        return
    now = datetime.now()
    son = _last_scan_time.get(market)
    if son is not None and (now - son).total_seconds() < RADAR_SCAN_INTERVAL_MINUTES * 60:
        return
    _last_scan_time[market] = now
    scan(tickers, market)


def build_radar_report() -> str:
    rows = _read_signals()
    if not rows:
        return ("🔬 [ÖNCÜL RADAR] Henüz sinyal yok.\n"
                f"Her {RADAR_SCAN_INTERVAL_MINUTES} dk BIST+ABD açıkken taranıyor, "
                "koşullar oluşunca bildirim gelir.")

    def _grup_ozet(grup, baslik):
        if not grup:
            return [f"{baslik}: henüz gözlem yok"]
        closed = [g for g in grup if g["status"] == "CLOSED" and g.get("session_end_pct")]
        n = len(grup)
        satir = [f"{baslik} (toplam {n}, kapanan {len(closed)})"]
        if closed:
            up = [float(g["max_up_pct"]) for g in closed if g.get("max_up_pct")]
            se = [float(g["session_end_pct"]) for g in closed]
            if up:
                for t in (1.0, 2.0, 3.0):
                    oran = sum(1 for x in up if x >= t) / len(up) * 100
                    satir.append(f"  +%{t:.0f}'e ulaşan: %{oran:.1f}")
            if se:
                satir.append(f"  Seans sonu ortalama: {sum(se)/len(se):+.2f}% | "
                             f"artıda: %{sum(1 for x in se if x > 0)/len(se)*100:.1f}")
        return satir

    bist_rows = [r for r in rows if r.get("market", "BIST") == "BIST"]
    us_rows = [r for r in rows if r.get("market") == "US"]

    kap_var = [r for r in bist_rows if r.get("kap_var") in ("True", "true", True)]
    kap_yok = [r for r in bist_rows if r.get("kap_var") not in ("True", "true", True)]

    lines = ["🔬 [ÖNCÜL RADAR RAPORU]", f"Toplam sinyal: {len(rows)} (BIST {len(bist_rows)}, ABD {len(us_rows)})", ""]
    lines.append("── BIST ──")
    lines += _grup_ozet(kap_var, "🔴 KAP DOĞRULANMIŞ")
    lines.append("")
    lines += _grup_ozet(kap_yok, "⚪ Sadece teknik (KAP yok)")
    lines.append("")
    lines.append("── ABD (yalnızca teknik, KAP yok) ──")
    lines += _grup_ozet(us_rows, "🇺🇸 ABD sinyalleri")
    lines.append("")
    lines.append("📌 Referans (öncül test, 39 BIST hissesi): filtreli +%2'ye "
                 "ulaşma %24.1 vs kontrol %12.5. ABD grubu bu referansla "
                 "karşılaştırılmalı ama ABD'de filtre AYRICA TEST EDİLMEDİ — "
                 "bu canlı veri ABD için ilk gerçek ölçüm.")
    lines.append("ℹ️ Sabit TP/stop yok, hiçbir emir açılmadı — bu rapor ham "
                 "olasılık verisidir, çıkış kuralı kararı ayrı ve sonraki adımdır.")
    return "\n".join(lines)

