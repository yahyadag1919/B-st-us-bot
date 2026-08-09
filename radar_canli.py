"""
radar_canli.py — CANLI ÖNCÜL RADAR (SİNYAL-AMAÇLI, EMİR AÇMAZ)
================================================================
radar_onculu_test.py'nin (2026-08-07/08) test edilmiş 3 bileşenini
(hacim artışı, kırılım, endeksten ayrışma) AYNEN kullanır — o testte
ilk kez pozitif sonuç bulunmuştu: filtreyi geçenlerde +%2'ye ulaşma
ihtimali kontrol grubuna göre ~2 katıydı (%24.1 vs %12.5, 39 hisse).

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
KAP_MATCH_WINDOW_MINUTES = int(os.environ.get("RADAR_KAP_MATCH_WINDOW_MINUTES", "240"))

INDEX_TICKER = "XU100.IS"

VOL_CACHE_HOURS = 20  # gunluk hacim ortalamasi - gun icinde degismez, gunde 1 yeniler
_vol_ma_cache = {}    # ticker -> (deger, hesaplanma_zamani)

SIGNAL_LOG_FILE = _data_path("radar_canli_signals.csv")
SIGNAL_FIELDS = ["ticker", "gun", "entry_time", "entry_price", "day_open_pct",
                  "hacim_ok", "kirilim_ok", "ayrisma_ok", "kap_var", "kap_kaynak",
                  "status", "max_up_pct", "max_down_pct", "session_end_pct"]

_triggered_today = {}  # (ticker, gun) -> True, ayni gun tekrar tetiklenmesin


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


def _get_index_today_pct(now_ts) -> float:
    """XU100'un bugunku gun ici yuzde degisimi, mevcut bara en yakin."""
    df = _fetch_15m(INDEX_TICKER, period="2d")
    if df.empty:
        return None
    today = date.today()
    gun = df[df["session"] == today]
    if gun.empty:
        return None
    acilis = float(gun.iloc[0]["open"])
    if acilis <= 0:
        return None
    son = float(gun.iloc[-1]["close"])
    return (son - acilis) / acilis * 100


# ---------------------------------------------------------------------------
# KAP eslesmesi (kap_monitor.py'nin biriktirdigi gercek veriden)
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
    """Acik (status=OPEN) sinyallerin fiyatini gunceller. Seans bittiyse
    kapatir. Sabit TP/stop YOK - sadece max yukari/asagi/seans sonu izlenir."""
    rows = _read_signals()
    if not rows:
        return
    today = date.today().isoformat()
    ist_open = bist_is_open()
    changed = False
    for r in rows:
        if r["status"] != "OPEN":
            continue
        ticker = r["ticker"]
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
            if r["gun"] != today or not ist_open:
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

def scan(tickers: list):
    """Her tetiklenmede radar_onculu_test.py ile AYNI 3 teknik bileseni
    kontrol eder + KAP eslesmesini etiket olarak ekler. Sabit TP/stop yok,
    sadece bildirim + sonuc takibi. Ayni hisse ayni gun sadece 1 kez."""
    if not RADAR_ENABLED or not bist_is_open():
        return

    index_pct = _get_index_today_pct(datetime.now())
    if index_pct is None:
        return

    today = date.today().isoformat()

    for ticker in tickers:
        key = (ticker, today)
        if _triggered_today.get(key):
            continue
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

            kap_var, kap_kaynak = _kap_match(ticker)

            _triggered_today[key] = True
            _append_signal({
                "ticker": ticker, "gun": today,
                "entry_time": datetime.now().isoformat(),
                "entry_price": f"{fiyat:.4f}", "day_open_pct": f"{gun_ici_pct:.2f}",
                "hacim_ok": hacim_ok, "kirilim_ok": kirilim_ok, "ayrisma_ok": ayrisma_ok,
                "kap_var": kap_var, "kap_kaynak": kap_kaynak or "",
                "status": "OPEN", "max_up_pct": "", "max_down_pct": "", "session_end_pct": "",
            })

            etiket = f"🔴 KAP DOĞRULANMIŞ ({kap_kaynak})" if kap_var else "⚪ Teknik (KAP eşleşmedi)"
            send_telegram_message(
                f"🔬 [ÖNCÜL RADAR] {ticker}\n"
                f"Gün içi: {gun_ici_pct:+.2f}% | Fiyat: {fiyat:.2f}\n"
                f"Hacim ≥{VOLUME_MULT:g}× ✅ | Kırılım ✅ | Endeksten ayrışma ✅\n"
                f"{etiket}\n\n"
                "ℹ️ SİNYAL AMAÇLIDIR — emir talimatı DEĞİLDİR, sabit hedef/stop "
                "YOKTUR. Öncül testte bu filtre +%2'ye ulaşma ihtimalini "
                "kontrol grubuna göre ~2 katına çıkarmıştı (%24.1 vs %12.5). "
                "Bu sinyal sonuç takibine alındı, /radar ile sorgulanabilir."
            )
        except Exception:
            continue  # bu radar izole - tek hisse hatasi taramayi durdurmaz


def maybe_scan(tickers: list):
    global _last_scan_time
    if not RADAR_ENABLED:
        return
    now = datetime.now()
    if (_last_scan_time is not None and
            (now - _last_scan_time).total_seconds() < RADAR_SCAN_INTERVAL_MINUTES * 60):
        return
    _last_scan_time = now
    scan(tickers)


_last_scan_time = None


def build_radar_report() -> str:
    rows = _read_signals()
    if not rows:
        return ("🔬 [ÖNCÜL RADAR] Henüz sinyal yok.\n"
                f"Her {RADAR_SCAN_INTERVAL_MINUTES} dk BIST açıkken taranıyor, "
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

    kap_var = [r for r in rows if r.get("kap_var") in ("True", "true", True)]
    kap_yok = [r for r in rows if r.get("kap_var") not in ("True", "true", True)]

    lines = ["🔬 [ÖNCÜL RADAR RAPORU]", f"Toplam sinyal: {len(rows)}", ""]
    lines += _grup_ozet(kap_var, "🔴 KAP DOĞRULANMIŞ")
    lines.append("")
    lines += _grup_ozet(kap_yok, "⚪ Sadece teknik (KAP yok)")
    lines.append("")
    lines.append("📌 Referans (öncül test, 39 hisse, KAP'sız): filtreli +%2'ye "
                 "ulaşma %24.1 vs kontrol %12.5. Bu canlı veri o referansla "
                 "karşılaştırılmalı, ayrıca KAP'lı/KAP'sız grup farkı asıl "
                 "yeni soruya (KAP fark yaratıyor mu?) cevap verir.")
    lines.append("ℹ️ Sabit TP/stop yok, hiçbir emir açılmadı — bu rapor ham "
                 "olasılık verisidir, çıkış kuralı kararı ayrı ve sonraki adımdır.")
    return "\n".join(lines)
