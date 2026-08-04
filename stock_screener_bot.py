import os
import csv
import json
import time
from datetime import datetime, date, timezone
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd
import requests
import yfinance as yf

import football_bot as fb

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

# BIST100 (yfinance ".IS" uzantisiyla). Endeks icerigi UC AYDA BIR degisir
# (Ocak-Mart / Nisan-Haziran / Temmuz-Eylul / Ekim-Aralik donemleri), bu yuzden
# bu liste zamanla eskiyebilir. Bot acilista her kodu dogrular ve veri
# gelmeyenleri otomatik eler (validate_tickers), o yuzden yanlis/endeksten
# cikmis bir kod sessiz hataya degil, tek seferlik bir uyariya yol acar.
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

# ABD: S&P 500'un genis, likit bir kesiti (gunluk swing taramasi icin)
US_TICKERS = [
    "AAPL", "MSFT", "GOOGL", "AMZN", "NVDA", "META",
    "TSLA", "BRK-B", "JPM", "V", "UNH", "MA",
    "HD", "PG", "COST", "XOM", "JNJ", "ABBV",
    "MRK", "AVGO", "PEP", "KO", "BAC", "WMT",
    "CRM", "ADBE", "AMD", "NFLX", "DIS", "CSCO",
    "ORCL", "INTC", "QCOM", "TXN", "PFE", "NKE",
    "MCD", "GS", "CAT", "BA", "LLY", "TMO",
    "ABT", "DHR", "ACN", "LIN", "MDT", "NEE",
    "PM", "UNP", "RTX", "HON", "SBUX", "LOW",
    "INTU", "AMGN", "IBM", "GE", "CVX", "WFC",
    "MS", "SCHW", "BLK", "SPGI", "AXP", "C",
    "T", "VZ", "CMCSA", "AMAT", "MU", "LRCX",
    "ADI", "KLAC", "SNPS", "CDNS", "PANW", "CRWD",
    "NOW", "UBER", "ABNB", "PYPL", "SQ", "SHOP",
    "COIN", "MRNA", "GILD", "BMY", "CVS", "CI",
    "ELV", "HCA", "DE", "MMM", "LMT", "NOC",
    "GD", "EOG", "SLB", "COP", "PSX", "MPC",
    "VLO", "NEM", "FCX", "DOW",
]

# Gun ici tarama 15 dakikada bir calistigi icin AYRI ve DAR bir liste kullanir -
# 100+ hisseyi 15 dakikada bir cekmek yfinance hiz limitine takilir ve tarama
# bir dongude bitmeyebilir. Burada sadece en likit / en cok opsiyon hacmi olan
# isimler var.
US_INTRADAY_TICKERS = [
    "AAPL", "MSFT", "GOOGL", "AMZN", "NVDA", "META",
    "TSLA", "AMD", "NFLX", "AVGO", "MU", "INTC",
    "JPM", "BAC", "XOM", "COIN", "PLTR", "UBER",
    "DIS", "BA", "WMT", "COST", "CRM", "ORCL",
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
US_SWING_CHECK_HOUR, US_SWING_CHECK_MINUTE = 16, 5  # America/New_York, ABD kapanisindan hemen sonra
CHECK_WINDOW_MINUTES = 5
LOOP_INTERVAL_SECONDS = 120                        # her 2 dakikada bir kontrol

# Planli taramalarin son calisma tarihi artik RAM'de degil, DATA_DIR icindeki
# run_state.json'da tutuluyor (bkz. should_run_daily_scan) - yeniden
# baslatmalarda kaybolmasin diye.
_last_us_gunici_scan_time = None
# Gun ici 3 motorlu taramanin son calisma zamani (mevcut RSI21 kolundan ayri)
_last_m15_scan_time = None
# Piyasa Beyni gun ici tarama durdurma bildirimi icin - ayni rejimde tekrar
# tekrar "durduruldu" mesaji atmamak icin son bildirilen rejimi hatirlar.
_last_gunici_halt_regime = None
US_GUNICI_SCAN_INTERVAL_MINUTES = 15  # yfinance'i asiri yormamak icin 15dk'da bir tara
_us_candidates = {}  # ABD gun ici tukenme adaylari - onay mumu bekleniyor

# Mac analiz botu (SPO-QUANT) - ayri, izole bir tarama kolu. Kendi verisi,
# kendi Telegram botu, kendi try/except'i var; hisse tarafini etkilemez.
# Ayristirilmis frekans: model taramasi sik, oran taramasi seyrek (Gemini'nin
# onayladigi tasarim - Odds API / API-Football gunluk-aylik kotalarini korur).
_last_football_model_scan_time = None
_last_football_odds_scan_time = None
_last_football_results_update_time = None
FOOTBALL_RESULTS_UPDATE_INTERVAL_MINUTES = int(os.environ.get("FOOTBALL_RESULTS_UPDATE_INTERVAL_MINUTES", "60"))


# ---------------------------------------------------------------------------
# Kalici depolama (Railway Volume)
# ---------------------------------------------------------------------------
# Railway'de her re-deploy/restart konteyner dosya sistemini sifirlar. Takip
# CSV'leri burada tutulmazsa acik sinyallerin cikis takibi sessizce kaybolur -
# kullanici parsiyel TP / stop uyarisi beklerken hicbir sey gelmez. Bu yuzden
# DATA_DIR bir Railway Volume'a (ornek: /data) bagli olmalidir.
# NOT: Railway'de Volume tanimlanip DATA_DIR=/data ayarlanmazsa dosyalar yine
# calisma dizinine yazilir ve re-deploy'da kaybolur - bu durumda asagidaki
# uyari acilista Telegram'a duser.
DATA_DIR = os.environ.get("DATA_DIR", ".")
try:
    os.makedirs(DATA_DIR, exist_ok=True)
except Exception as e:
    print(f"DATA_DIR olusturulamadi ({e}) - calisma dizini kullanilacak")
    DATA_DIR = "."


def _data_path(filename: str) -> str:
    return os.path.join(DATA_DIR, filename)


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
# 4. KATMAN: SİSTEM DEDEKTİFİ (hata izleme)
# ---------------------------------------------------------------------------
# Kripto botundaki Dedektif'in bu bota uyarlanmis hali. Onemli fark: burada
# bot kendisi islem acmadigi icin hata = "sinyal uretemedim / veri gelmedi"
# demektir. Sessizce gecmek tehlikeli, cunku kullanici sinyal beklerken
# botun aslinda calismadigini fark etmeyebilir.

_ERROR_HINTS = {
    "no data": "yfinance veri dondurmedi - sembol degismis ya da borsa tatil olabilir",
    "not found": "sembol yfinance'te bulunamadi - ticker listesi guncellenmeli",
    "delisted": "hisse borsadan cikarilmis olabilir - ticker listesini guncelle",
    "timeout": "yfinance/ag zaman asimi - gecici olabilir",
    "connection": "ag baglanti hatasi - gecici olabilir",
    "rate limit": "yfinance hiz limiti - tarama araligi artirilmali",
    "429": "yfinance hiz limiti - tarama araligi artirilmali",
    "index out of": "yetersiz mum verisi - hisse yeni islem gormeye baslamis olabilir",
    "permission denied": "dosya yazma izni yok - Railway ayarini kontrol et",
}

# Ayni hatayi her dongude tekrar tekrar bildirmemek icin basit bir bogucu -
# kripto botunda yasadigimiz "yuzlerce ayni bildirim" sorununu bastan onler.
_reported_errors = {}
ERROR_REPORT_COOLDOWN_MINUTES = 60


def _guess_root_cause(e: Exception) -> str:
    text = str(e).lower()
    for key, hint in _ERROR_HINTS.items():
        if key in text:
            return hint
    return "Bilinen bir kaliba uymuyor - detaylari incele."


# ---------------------------------------------------------------------------
# TAMİRCİ KATMANI (Auto-Healer) — 2026-08-04
# ---------------------------------------------------------------------------
# Kripto botundaki Tamirci'nin bu bota uyarlanmis hali. ONEMLI FARK: bu bot
# islem acmadigi icin "duzeltilecek bir borsa durumu" yok. Burada gercekten
# onarilabilecek iki sey var:
#   1) Veri kaynagi hiz limiti (429 / bos yanit) -> botun KENDI hizini
#      otomatik dusurmek. Sadece tekrar denemek ayni duvara toslamaktir;
#      asil onarim tempoyu degistirmektir.
#   2) Takilmis HTTP oturumu / DNS-baglanti sorunu -> yfinance'in onbellegini
#      temizleyip yeni istekleri temiz bir durumdan baslatmak.
# Ayrica dosya yazma hatalarinda DATA_DIR'i yeniden olusturmayi deniyoruz.

# Hiz limitine takilinca istekler arasina eklenen ekstra gecikme (saniye).
# Basarili turlarda kademeli olarak sifira geri iner - kalici yavaslamayalim.
_yf_extra_delay = 0.0
TAMIRCI_DELAY_STEP = float(os.environ.get("TAMIRCI_DELAY_STEP", "1.0"))
TAMIRCI_DELAY_MAX = float(os.environ.get("TAMIRCI_DELAY_MAX", "8.0"))
_tamirci_last_action = {}
TAMIRCI_COOLDOWN_MINUTES = int(os.environ.get("TAMIRCI_COOLDOWN_MINUTES", "15"))


def _is_rate_limit_error(e: Exception) -> bool:
    text = str(e).lower()
    return ("429" in text or "rate limit" in text or "too many requests" in text
            or "no data" in text or "0 mum" in text)


def _is_network_error(e: Exception) -> bool:
    text = str(e).lower()
    return ("timeout" in text or "timed out" in text or "connection" in text
            or "network" in text or "ssl" in text or "resolve" in text)


def _tamirci_should_act(kind: str) -> bool:
    """Ayni onarimi dakikada bir tekrarlamayalim - onarim da bir maliyettir."""
    last = _tamirci_last_action.get(kind)
    now = datetime.now()
    if last and (now - last).total_seconds() < TAMIRCI_COOLDOWN_MINUTES * 60:
        return False
    _tamirci_last_action[kind] = now
    return True


def _refresh_yf_session() -> bool:
    """yfinance'in dahili onbelleklerini temizler. Takilmis bir cerez/crumb
    ya da bozuk zaman dilimi onbellegi tum istekleri sessizce bosa
    dusurebiliyor; temizleyince kutuphane bunlari yeniden kurar."""
    cleared = False
    try:
        import yfinance.utils as yfu
        for attr in ("cache_dir", "_tz_cache"):
            if hasattr(yfu, attr):
                cleared = True
    except Exception:
        pass
    try:
        # Ticker nesnelerinin tuttugu oturum/cerez durumunu birakmak icin
        # modul seviyesindeki paylasilan veri nesnesini sifirliyoruz.
        from yfinance import data as yfdata
        if hasattr(yfdata, "YfData") and hasattr(yfdata.YfData, "_instances"):
            yfdata.YfData._instances.clear()
            cleared = True
    except Exception:
        pass
    return cleared


def tamirci_repair(context: str, e: Exception) -> str:
    """Hatanin turune gore otomatik onarim dener.
    Yapilan islemin aciklamasini doner; hicbir sey yapilmadiysa bos string."""
    global _yf_extra_delay

    if _is_rate_limit_error(e):
        if not _tamirci_should_act("rate_limit"):
            return ""
        if _yf_extra_delay >= TAMIRCI_DELAY_MAX:
            return ""
        _yf_extra_delay = min(_yf_extra_delay + TAMIRCI_DELAY_STEP, TAMIRCI_DELAY_MAX)
        return (f"veri kaynağı hız limiti algılandı → istekler arası gecikme "
                f"{_yf_extra_delay:.1f} sn'ye çıkarıldı")

    if _is_network_error(e):
        if not _tamirci_should_act("network"):
            return ""
        if _refresh_yf_session():
            return "ağ/oturum hatası → yfinance oturum önbelleği temizlendi"
        return ""

    if "permission" in str(e).lower() or "no such file" in str(e).lower():
        if not _tamirci_should_act("storage"):
            return ""
        try:
            os.makedirs(DATA_DIR, exist_ok=True)
            return f"dosya erişim hatası → {DATA_DIR} yeniden oluşturuldu"
        except Exception:
            return ""
    return ""


def tamirci_note_success():
    """Basarili bir veri cekiminden sonra ekstra gecikmeyi kademeli azaltir,
    boylece gecici bir hiz limiti yuzunden kalici yavas kalmayiz."""
    global _yf_extra_delay
    if _yf_extra_delay > 0:
        _yf_extra_delay = max(0.0, _yf_extra_delay - TAMIRCI_DELAY_STEP / 4)


def dedektif_report(context: str, e: Exception, ticker: str = "-"):
    """Hata bildirir, ama ayni hata/ticker kombinasyonu icin saatte en fazla bir kez.
    Bildirmeden ONCE Tamirci'ye onarim sansi verir - boylece kullanici hem
    hatayi hem de sistemin buna karsi ne yaptigini ayni mesajda gorur."""
    repair = ""
    try:
        repair = tamirci_repair(context, e)
    except Exception as re_:
        print(f"Tamirci hatasi: {re_}")

    key = f"{context}|{ticker}|{type(e).__name__}"
    now = datetime.now()
    last = _reported_errors.get(key)
    if last and (now - last).total_seconds() < ERROR_REPORT_COOLDOWN_MINUTES * 60:
        return
    _reported_errors[key] = now
    send_telegram_message(
        f"🚨 [DEDEKTİF UYARISI]\n"
        f"Yer: {context} | Hisse: {ticker}\n"
        f"Hata: {e}\n"
        f"Tahmini kök neden: {_guess_root_cause(e)}"
        + (f"\n🛠️ [TAMİRCİ] {repair}" if repair else "")
    )


# ---------------------------------------------------------------------------
# 1. KATMAN: PİYASA BEYNİ (Market Allocator)
# ---------------------------------------------------------------------------
# BIST icin XU100, ABD icin SPY endeks trendini olcer. Endeks yatay/dususte
# ise o piyasanin taramasi durdurulur - "gurultulu piyasada sinyal uretme".
# NOT: kripto botundaki gibi ADX + EMA200 kullaniyoruz, ama burada karar
# ikili degil uclu: YUKSELIS (tara) / YATAY (durdur) / DUSUS (durdur).

INDEX_TICKERS = {"BIST": "XU100.IS", "ABD": "SPY"}
REGIME_ADX_PERIOD = 14
REGIME_ADX_TREND_MIN = 20.0      # bunun altinda trend "yok" sayilir (yatay)
REGIME_EMA_PERIOD = 200

# Endeks verisi saatte bir yenilenir - her taramada tekrar cekmek gereksiz.
_regime_cache = {}
REGIME_CACHE_MINUTES = 60


def _compute_adx(df: pd.DataFrame, period: int = REGIME_ADX_PERIOD) -> pd.Series:
    """Standart Wilder ADX - kripto botundaki _compute_adx ile ayni mantik."""
    high, low, close = df["high"], df["low"], df["close"]
    plus_dm = high.diff()
    minus_dm = -low.diff()
    plus_dm = plus_dm.where((plus_dm > minus_dm) & (plus_dm > 0), 0.0)
    minus_dm = minus_dm.where((minus_dm > plus_dm) & (minus_dm > 0), 0.0)

    tr = pd.concat([
        high - low,
        (high - close.shift()).abs(),
        (low - close.shift()).abs(),
    ], axis=1).max(axis=1)

    atr = tr.ewm(alpha=1 / period, adjust=False).mean()
    plus_di = 100 * plus_dm.ewm(alpha=1 / period, adjust=False).mean() / atr.replace(0, np.nan)
    minus_di = 100 * minus_dm.ewm(alpha=1 / period, adjust=False).mean() / atr.replace(0, np.nan)
    dx = 100 * (plus_di - minus_di).abs() / (plus_di + minus_di).replace(0, np.nan)
    return dx.ewm(alpha=1 / period, adjust=False).mean()


def get_market_regime(market: str):
    """(rejim, aciklama) doner. rejim: 'YUKSELIS' | 'YATAY' | 'DUSUS' | 'BILINMIYOR'.
    BILINMIYOR = endeks verisi cekilemedi; bu durumda taramayi DURDURMUYORUZ
    (veri sorunu yuzunden butun sinyalleri kaybetmek, gurultulu piyasada
    islem yapmaktan daha kotu bir hata olurdu) - sadece uyari notu dusuyoruz."""
    cached = _regime_cache.get(market)
    if cached and (datetime.now() - cached["time"]).total_seconds() < REGIME_CACHE_MINUTES * 60:
        return cached["regime"], cached["note"]

    ticker = INDEX_TICKERS[market]
    try:
        df = fetch_daily_df(ticker, period="2y")
        if df.empty or len(df) < REGIME_EMA_PERIOD + 5:
            raise ValueError(f"endeks verisi yetersiz ({len(df)} mum)")

        df["adx"] = _compute_adx(df)
        df["ema200"] = df["close"].ewm(span=REGIME_EMA_PERIOD, adjust=False).mean()
        row = df.iloc[-1]
        adx, close, ema200 = row["adx"], row["close"], row["ema200"]

        if pd.isna(adx) or pd.isna(ema200):
            raise ValueError("ADX/EMA200 hesaplanamadi")

        if adx < REGIME_ADX_TREND_MIN:
            regime = "YATAY"
            note = f"ADX {adx:.1f} (<{REGIME_ADX_TREND_MIN}) - trend yok"
        elif close > ema200:
            regime = "YUKSELIS"
            note = f"ADX {adx:.1f}, endeks EMA200 üstünde"
        else:
            regime = "DUSUS"
            note = f"ADX {adx:.1f}, endeks EMA200 altında"

    except Exception as e:
        dedektif_report(f"{market} piyasa rejimi", e, ticker)
        regime, note = "BILINMIYOR", f"endeks verisi alınamadı ({e})"

    _regime_cache[market] = {"regime": regime, "note": note, "time": datetime.now()}
    return regime, note


def market_scan_allowed(market: str):
    """(izin_var_mi, izinli_yon, rejim, aciklama)

    Gemini'nin nihai karari (2026-07-28): rejim filtresi taramayi tamamen
    durdurmak yerine YONLENDIRIR -
      YUKSELIS -> sadece LONG sinyalleri
      DUSUS    -> sadece SHORT sinyalleri (dusus trendi SHORT icin en verimli
                  ortam; eski hali bu firsatlari tamamen bloke ediyordu)
      YATAY    -> tarama durdurulur (gurultu / hatali sinyal onlemi)
    izinli_yon None ise her iki yon de serbest (sadece BILINMIYOR durumunda)."""
    regime, note = get_market_regime(market)
    if regime == "YUKSELIS":
        return True, "LONG", regime, note
    if regime == "DUSUS":
        return True, "SHORT", regime, note
    if regime == "BILINMIYOR":
        # Endeks verisi alinamadi - taramaya devam, yon kisiti yok.
        return True, None, regime, note
    return False, None, regime, note  # YATAY


# ---------------------------------------------------------------------------
# 2. KATMAN: PORTFÖY VE RİSK BEYNİ (Portfolio Manager)
# ---------------------------------------------------------------------------
# Sinyalde "kac lot / kac adet" alinmasi gerektigini hesaplar. Bot islem
# ACMIYOR - bu sadece emir talimati. Hesap: risk_tutari / stop_mesafesi.

def _optional_float_env(name: str):
    """Ayarlanmamis ya da bos birakilmis degiskenler icin None doner.
    DERS (2026-07-28): eskiden burada 100000/10000 gibi varsayilanlar vardi ve
    kullanici bakiyesini girmediginde bot bunlarin uzerinden 'EMIR: 200 adet al'
    gibi KESIN ama gercekle ilgisiz bir sayi yaziyordu. Yanlis sayi, hic sayi
    olmamasindan daha kotu - o yuzden artik varsayilan yok."""
    raw = os.environ.get(name, "").strip()
    if not raw:
        return None
    try:
        return float(raw)
    except ValueError:
        print(f"{name} sayiya cevrilemedi ('{raw}') - yok sayiliyor")
        return None


PORTFOLIO_BALANCE_TRY = _optional_float_env("PORTFOLIO_BALANCE_TRY")
PORTFOLIO_BALANCE_USD = _optional_float_env("PORTFOLIO_BALANCE_USD")
RISK_PER_TRADE_PCT = float(os.environ.get("RISK_PER_TRADE_PCT", "2.0"))
# Tek pozisyonun portfoyun bu yuzdesinden fazlasini kaplamasini engelle
# (dar stop mesafelerinde miktar patlamasin - kripto botunda ogrenilen ders).
MAX_POSITION_PCT_OF_BALANCE = float(os.environ.get("MAX_POSITION_PCT_OF_BALANCE", "20"))

# Gemini'nin karari (2026-07-28): opsiyon/varant icin ayri bir matematik modulu
# kurmak yerine, hesabin neyi kapsadigini acikca yazan bir not yeterli.
OPTIONS_SIZING_NOTE = (
    "⚠️ Bu hesaplama spot hisse alımı içindir. Opsiyon/Varant işlemlerinde "
    "riske edilecek toplam tutarı aşmayacak şekilde prim bazlı pozisyon açınız."
)


def _balance_for(market: str):
    return PORTFOLIO_BALANCE_TRY if market == "BIST" else PORTFOLIO_BALANCE_USD


def sizing_line(market: str, entry_price: float, stop_price: float) -> str:
    """Sinyal mesajindaki tek satirlik boyutlandirma bilgisi.
    Bakiye ayarliysa 'kac adet al', degilse stop mesafesini yuzde olarak
    gosterir - kullanici kendi riskini ona gore ayarlar."""
    if _balance_for(market) is not None:
        qty, risk_amount, currency, note = compute_position_size(market, entry_price, stop_price)
        if qty > 0:
            return (f"➡️ EMİR: {qty} adet al (risk: {risk_amount:.0f} {currency})"
                    + (f" — {note}" if note else ""))
        return f"⚠️ Miktar hesaplanamadı: {note}"

    if entry_price > 0 and stop_price > 0:
        pct = abs(entry_price - stop_price) / entry_price * 100
        return (f"📏 Stop mesafesi: %{pct:.2f} ({entry_price:.2f} → {stop_price:.2f}) — "
                f"pozisyon büyüklüğünü kendi riskine göre ayarla")
    return "📏 Stop mesafesi hesaplanamadı"


def compute_position_size(market: str, entry_price: float, stop_price: float):
    """(adet, risk_tutari, para_birimi, uyari_notu) doner.
    adet=0 ise sinyal yine gonderilir ama miktar hesaplanamamis demektir."""
    balance = _balance_for(market)
    currency = "TL" if market == "BIST" else "$"
    if balance is None:
        return 0, 0.0, currency, "portföy bakiyesi ayarlı değil"

    stop_distance = abs(entry_price - stop_price)
    if stop_distance <= 0 or entry_price <= 0:
        return 0, 0.0, currency, "stop mesafesi hesaplanamadı"

    risk_amount = balance * (RISK_PER_TRADE_PCT / 100)
    qty_by_risk = risk_amount / stop_distance

    # Pozisyon buyuklugu tavani (kaldirac yok, nakit alim varsayimi)
    max_position_value = balance * (MAX_POSITION_PCT_OF_BALANCE / 100)
    qty_by_cap = max_position_value / entry_price

    qty = int(min(qty_by_risk, qty_by_cap))
    note = ""
    if qty <= 0:
        return 0, risk_amount, currency, "hesaplanan miktar 1 adetin altında (stop çok geniş ya da bakiye küçük)"
    if qty_by_cap < qty_by_risk:
        note = f"pozisyon tavanı (%{MAX_POSITION_PCT_OF_BALANCE:.0f}) devrede"
    return qty, risk_amount, currency, note


# ---------------------------------------------------------------------------
# Veri ve indikatorler
# ---------------------------------------------------------------------------

YF_TIMEOUT_SECONDS = int(os.environ.get("YF_TIMEOUT_SECONDS", "20"))
# 429 / gecici bos yanit durumunda kac kez denenecegi ve bekleme taban suresi
YF_MAX_RETRIES = int(os.environ.get("YF_MAX_RETRIES", "3"))
YF_RETRY_BACKOFF_SECONDS = float(os.environ.get("YF_RETRY_BACKOFF_SECONDS", "2.0"))
# Acilistaki toplu dogrulama ayarlari (hizli olmali - bot bunu bitirmeden
# acilis mesajini gonderemiyor)
VALIDATION_SLEEP_SECONDS = float(os.environ.get("VALIDATION_SLEEP_SECONDS", "0.2"))
VALIDATION_PROBE_SIZE = int(os.environ.get("VALIDATION_PROBE_SIZE", "10"))
_EXPECTED_COLS = ["timestamp", "open", "high", "low", "close", "volume"]


def _normalize_df(df: pd.DataFrame) -> pd.DataFrame:
    """yfinance bazen beklenen kolonlari eksik/farkli isimle dondurebiliyor -
    burada tek yerde normalize edip, eksikse bos DataFrame donuyoruz ki
    cagiran taraf 'yetersiz veri' olarak sessizce atlasin (KeyError ile
    tarama komple cokmesin)."""
    if df is None or df.empty:
        return pd.DataFrame(columns=_EXPECTED_COLS)
    df = df.reset_index()
    df = df.rename(columns={
        "Datetime": "timestamp", "Date": "timestamp", "index": "timestamp",
        "Open": "open", "High": "high", "Low": "low",
        "Close": "close", "Volume": "volume",
    })
    if not all(c in df.columns for c in _EXPECTED_COLS):
        return pd.DataFrame(columns=_EXPECTED_COLS)
    return df[_EXPECTED_COLS]


def fetch_daily_df(ticker: str, period: str = "6mo", retries: int = None) -> pd.DataFrame:
    # DERS (2026-08-04): Render gibi veri merkezi IP'lerinden yfinance sik sik
    # 429 (hiz limiti) donuyor ve tek denemede vazgecmek, saglam kodlari bile
    # "veri yok" gibi gostererek butun taramayi bosa cikariyordu. Artik
    # kademeli bekleyerek birkac kez deniyoruz.
    # DIKKAT: acilistaki toplu dogrulama bu yeniden denemeleri KULLANMAZ
    # (retries=1 gecer). Aksi halde 226 hisse x 3 deneme x kademeli bekleme
    # ~50 dakika suruyor ve bot acilis mesajini bile gonderemiyordu.
    attempts = YF_MAX_RETRIES if retries is None else max(1, retries)
    last_df = pd.DataFrame()
    for attempt in range(attempts):
        # TAMIRCI: hiz limitine takildiysak istekler arasina ekstra gecikme
        # koyuyoruz. Basarili cekimlerde bu gecikme kendiliginden azaliyor.
        if _yf_extra_delay > 0:
            time.sleep(_yf_extra_delay)
        try:
            try:
                df = yf.Ticker(ticker).history(period=period, interval="1d",
                                               timeout=YF_TIMEOUT_SECONDS)
            except TypeError:
                # Eski yfinance surumlerinde timeout parametresi yok
                df = yf.Ticker(ticker).history(period=period, interval="1d")
            last_df = df
            if df is not None and not df.empty:
                tamirci_note_success()
                break
        except Exception:
            if attempt == attempts - 1:
                raise
        if attempt < attempts - 1:
            time.sleep(YF_RETRY_BACKOFF_SECONDS * (attempt + 1))
    return _normalize_df(last_df)


def fetch_intraday_df(ticker: str, interval: str = "15m", period: str = "5d") -> pd.DataFrame:
    try:
        df = yf.Ticker(ticker).history(period=period, interval=interval,
                                       timeout=YF_TIMEOUT_SECONDS)
    except TypeError:
        df = yf.Ticker(ticker).history(period=period, interval=interval)
    return _normalize_df(df)


def compute_indicators(df: pd.DataFrame, rsi_period: int = RSI_PERIOD) -> pd.DataFrame:
    df["ema20"] = df["close"].ewm(span=20, adjust=False).mean()
    df["ema50"] = df["close"].ewm(span=50, adjust=False).mean()
    df["vol_sma20"] = df["volume"].rolling(20).mean()
    df["vol_std20"] = df["volume"].rolling(20).std()
    df["vol_zscore"] = (df["volume"] - df["vol_sma20"]) / df["vol_std20"].replace(0, np.nan)

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

    avg_gain21 = gain.ewm(alpha=1 / 21, adjust=False).mean()
    avg_loss21 = loss.ewm(alpha=1 / 21, adjust=False).mean()
    rs21 = avg_gain21 / avg_loss21.replace(0, np.nan)
    df["rsi21"] = (100 - (100 / (1 + rs21))).fillna(50)

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


def check_rsi_only(df: pd.DataFrame):
    """
    Turnuvada BIST icin 2. en iyi ort. net getiriyi veren sistem
    (571 sinyal, %69.2 isabet, +%0.759 ort. net) - tek sart: RSI asiri uc.
    """
    if len(df) < max(BOLLINGER_PERIOD, 20) + 2:
        return None

    row = df.iloc[-1]
    if row["rsi"] <= RSI_OVERSOLD:
        return "LONG", row
    if row["rsi"] >= RSI_OVERBOUGHT:
        return "SHORT", row

    return None


US_SWING_ZSCORE_THRESHOLD = 2.0
US_SWING_ATR_MULT = 2.0


def check_us_volume_zscore(df: pd.DataFrame):
    """
    ABD swing turnuvasinda TEK karli cikan strateji: Hacim Z-Skor
    (889 sinyal, %63.0 isabet, +%0.321 ort. net). RSI/fitil bazli mantik
    ABD hisselerinde gunluk/swing tutuşta ise yaramadigi icin kullanilmiyor.
    """
    if len(df) < 25:
        return None

    row = df.iloc[-1]
    if pd.isna(row.get("vol_zscore")) or row["vol_zscore"] < US_SWING_ZSCORE_THRESHOLD:
        return None

    if row["close"] < row["open"]:
        return "LONG", row
    elif row["close"] > row["open"]:
        return "SHORT", row

    return None


def check_us_atr_breakout(df: pd.DataFrame):
    """
    Genisletilmis turnuvada ABD swing icin YENI en iyi strateji: ATR kirilimi
    x2.0 (270 sinyal, %69.6 net isabet, ort. net +%0.749, toplam +%202.3) -
    onceki turdaki Hacim Z-Skor'u (+%0.321) bile gecti. Momentum mantigi:
    fiyat, onceki kapanistan ATR'nin 2 kati kadar sicramissa o yonde devam beklentisi.
    """
    if len(df) < 20:
        return None

    row = df.iloc[-1]
    prev_close = df.iloc[-2]["close"]
    if pd.isna(row.get("atr14")) or row["atr14"] == 0:
        return None

    move = row["close"] - prev_close
    if move >= US_SWING_ATR_MULT * row["atr14"]:
        return "LONG", row
    if move <= -US_SWING_ATR_MULT * row["atr14"]:
        return "SHORT", row

    return None


US_GUNICI_RSI_OS = 25
US_GUNICI_RSI_OB = 75


def check_us_rsi21_gunici(df: pd.DataFrame):
    """
    Genisletilmis turnuvada ABD gun ici (15dk-4sa checkpoint) icin en yuksek HAM
    (komisyonsuz) edge'i veren strateji: sadece RSI21 asiri uc (778 sinyal, %69.9
    net isabet, ort. ham +%0.115). Komisyon sonrasi hisse bazinda hafif zararli
    cikiyor ama kullanicinin gercek opsiyon maliyeti (sabit ~$1 Midas komisyonu +
    opsiyon spread'i) farkli oldugu icin canliya sinyal-amacli alindi; otomatik
    islem yapmiyor, sadece Telegram bildirimi + checkpoint takibi yapiyor.
    """
    if len(df) < 25:
        return None

    row = df.iloc[-1]
    if pd.isna(row.get("rsi21")):
        return None

    if row["rsi21"] <= US_GUNICI_RSI_OS:
        return "LONG", row
    if row["rsi21"] >= US_GUNICI_RSI_OB:
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

SIGNAL_LOG_FILE = _data_path("stock_signal_history.csv")

# ---------------------------------------------------------------------------
# 3. KATMAN: HİBRİT ÇIKIŞ UYARI SİSTEMİ
# ---------------------------------------------------------------------------
# Bot islem acmadigi icin burada "pozisyon yonetimi" degil, EMİR TALİMATI
# uretiliyor. Sinyal verilen her hisse takibe alinir ve fiyat seviyelere
# geldikce Telegram'a ne yapmasi gerektigi yazilir:
#   1.5R  -> "%50 sat + stop'u girise cek"
#   sonra -> ATR trailing stop seviyesi guncellendikce uyari
#   stop  -> "stop seviyesi gecildi, cik"
# Onemli: bu takip yalnizca UYARI verir; gercekte islemi kullanici yapar,
# bu yuzden bot "kapandi" varsaymaz - kullanici /iptal ile dusurene ya da
# stop/son hedef tetiklenene kadar takipte tutar.

TRACKING_FILE = _data_path("signal_tracking.csv")
TRACKING_FIELDNAMES = [
    "ticker", "market", "strategy", "direction", "entry_price", "stop_price",
    "tp_price", "entry_time", "qty", "partial_done", "trail_stop", "closed",
]
PARTIAL_TP_R_MULT = float(os.environ.get("PARTIAL_TP_R_MULT", "1.5"))
TRAIL_ATR_MULT = float(os.environ.get("TRAIL_ATR_MULT", "2.0"))
# Trailing stop her kucuk oynamada mesaj atmasin - en az bu kadar iyilesme sart.
TRAIL_MIN_MOVE_PCT = float(os.environ.get("TRAIL_MIN_MOVE_PCT", "1.0"))


def _read_tracking():
    if not os.path.isfile(TRACKING_FILE):
        return []
    with open(TRACKING_FILE, newline="") as f:
        return [r for r in csv.DictReader(f) if r.get("closed") != "1"]


def _write_tracking(rows):
    with open(TRACKING_FILE, "w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=TRACKING_FIELDNAMES)
        writer.writeheader()
        for r in rows:
            writer.writerow({k: r.get(k, "") for k in TRACKING_FIELDNAMES})


def track_new_signal(ticker, market, strategy, direction, entry_price, stop_price, tp_price, qty):
    rows = _read_tracking()
    # Ayni hissede ayni yonde zaten acik takip varsa tekrar ekleme.
    for r in rows:
        if r["ticker"] == ticker and r["direction"] == direction:
            return
    rows.append({
        "ticker": ticker, "market": market, "strategy": strategy, "direction": direction,
        "entry_price": entry_price, "stop_price": stop_price, "tp_price": tp_price,
        "entry_time": datetime.now().isoformat(), "qty": qty,
        "partial_done": "0", "trail_stop": "", "closed": "0",
    })
    _write_tracking(rows)
    # Gemini'nin istegi (2026-07-28): CSV kaybolursa takip zinciri kopmasin diye
    # her takibe alma Telegram'a da log dusuyor - boylece kullanicinin elinde
    # her zaman seviyelerin yazili bir kaydi kalir.
    send_telegram_message(
        f"📋 [TAKİBE ALINDI] {ticker} ({direction}) — {market} / {strategy}\n"
        f"Giriş: {entry_price:.2f} | 🛑 Stop: {stop_price:.2f} | 🎯 TP: {tp_price:.2f} | Adet: {qty}\n"
        f"(Bu kayıt sistem sıfırlansa bile elinde kalsın diye gönderildi.)"
    )


def check_exit_alerts():
    """Takipteki her sinyal icin guncel fiyati kontrol edip gerekirse
    Telegram'a EMİR TALİMATI gonderir."""
    rows = _read_tracking()
    if not rows:
        return

    still_open = []
    for r in rows:
        ticker = r["ticker"]
        try:
            direction = r["direction"]
            entry = float(r["entry_price"])
            stop = float(r["stop_price"])
            tp = float(r["tp_price"])
            partial_done = r.get("partial_done") == "1"
            trail_stop = float(r["trail_stop"]) if r.get("trail_stop") else None

            df = fetch_daily_df(ticker, period="3mo")
            if df.empty:
                still_open.append(r)
                continue
            df = compute_indicators(df)
            last = df.iloc[-1]
            price = float(last["close"])
            atr = float(last["atr14"]) if not pd.isna(last["atr14"]) else None

            effective_stop = trail_stop if trail_stop is not None else stop
            stopped = (price <= effective_stop) if direction == "LONG" else (price >= effective_stop)
            if stopped:
                pct = ((price - entry) / entry * 100) * (1 if direction == "LONG" else -1)
                if trail_stop is None:
                    label = "STOP"
                elif abs(trail_stop - entry) < 1e-9:
                    label = "BREAKEVEN"
                else:
                    label = "TRAILING STOP"
                send_telegram_message(
                    f"🛑 [{label}] {ticker} ({direction})\n"
                    f"Fiyat {price:.2f}, stop seviyesi {effective_stop:.2f} geçildi.\n"
                    f"➡️ POZİSYONU KAPAT.\n"
                    f"Giriş: {entry:.2f} | Sonuç: {pct:+.2f}%"
                )
                r["closed"] = "1"
                continue

            if not partial_done:
                tp_hit = (price >= tp) if direction == "LONG" else (price <= tp)
                if tp_hit:
                    pct = ((price - entry) / entry * 100) * (1 if direction == "LONG" else -1)
                    qty = r.get("qty", "")
                    half = ""
                    try:
                        half = f" (~{int(int(qty) / 2)} adet)"
                    except Exception:
                        pass
                    send_telegram_message(
                        f"🎯 [PARSİYEL TP] {ticker} ({direction}) {PARTIAL_TP_R_MULT}R seviyesine ulaştı!\n"
                        f"Fiyat: {price:.2f} | Giriş: {entry:.2f} | Kâr: {pct:+.2f}%\n"
                        f"➡️ %50 SATIŞ YAP{half} ve STOP'U GİRİŞE ({entry:.2f}) ÇEK!"
                    )
                    r["partial_done"] = "1"
                    r["trail_stop"] = str(entry)  # breakeven
                    still_open.append(r)
                    continue

            # Parsiyel alindiysa ATR trailing stop uyarisi
            if partial_done and atr:
                if direction == "LONG":
                    candidate = price - atr * TRAIL_ATR_MULT
                    improved = trail_stop is None or candidate > trail_stop * (1 + TRAIL_MIN_MOVE_PCT / 100)
                else:
                    candidate = price + atr * TRAIL_ATR_MULT
                    improved = trail_stop is None or candidate < trail_stop * (1 - TRAIL_MIN_MOVE_PCT / 100)
                if improved:
                    send_telegram_message(
                        f"📈 [TRAILING STOP GÜNCELLE] {ticker} ({direction})\n"
                        f"Fiyat: {price:.2f} | Trend devam ediyor.\n"
                        f"➡️ STOP'U {candidate:.2f} SEVİYESİNE ÇEK ({TRAIL_ATR_MULT}×ATR)."
                    )
                    r["trail_stop"] = str(candidate)

            still_open.append(r)

        except Exception as e:
            dedektif_report("çıkış takibi", e, ticker)
            still_open.append(r)

    _write_tracking(still_open)


US_SWING_PENDING_FILE = _data_path("us_swing_pending.csv")
US_SWING_OUTCOME_FILE = _data_path("us_swing_outcomes.csv")

# (gun_sayisi, etiket, hedef_yuzde) - turnuvadaki BIST_CHECKPOINTS ile ayni yapida
US_SWING_CHECKPOINTS = [(1, "1g", 1.0), (3, "3g", 2.0), (5, "5g", 3.0), (10, "10g", 5.0)]

US_SWING_PENDING_FIELDNAMES = ["ticker", "strategy", "direction", "entry_price", "entry_date"] + [
    f"checked_{label}" for _, label, _ in US_SWING_CHECKPOINTS
] + ["closed"]


def log_us_swing_pending(ticker: str, strategy: str, direction: str, entry_price: float, entry_date: date):
    file_exists = os.path.isfile(US_SWING_PENDING_FILE)
    with open(US_SWING_PENDING_FILE, "a", newline="") as f:
        writer = csv.writer(f)
        if not file_exists:
            writer.writerow(US_SWING_PENDING_FIELDNAMES)
        row = [ticker, strategy, direction, entry_price, entry_date.isoformat()]
        row += ["0" for _ in US_SWING_CHECKPOINTS]
        row += ["0"]
        writer.writerow(row)


def _read_us_swing_pending():
    if not os.path.isfile(US_SWING_PENDING_FILE):
        return []
    with open(US_SWING_PENDING_FILE, newline="") as f:
        return list(csv.DictReader(f))


def _write_us_swing_pending(rows):
    with open(US_SWING_PENDING_FILE, "w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=US_SWING_PENDING_FIELDNAMES)
        writer.writeheader()
        writer.writerows(rows)


def log_us_swing_outcome(ticker, strategy, direction, entry_price, entry_date, days, label, target_pct,
                          current_price, pct_change, success):
    file_exists = os.path.isfile(US_SWING_OUTCOME_FILE)
    with open(US_SWING_OUTCOME_FILE, "a", newline="") as f:
        writer = csv.writer(f)
        if not file_exists:
            writer.writerow([
                "ticker", "strategy", "direction", "entry_price", "entry_date", "gun", "checkpoint",
                "target_pct", "price_now", "pct_change", "success"
            ])
        writer.writerow([
            ticker, strategy, direction, entry_price, entry_date, days, label,
            target_pct, current_price, f"{pct_change:.3f}", success
        ])


def check_us_swing_outcomes():
    rows = _read_us_swing_pending()
    if not rows:
        return

    today = datetime.now(ZoneInfo("America/New_York")).date()
    still_pending = []

    for r in rows:
        if r.get("closed", "0") == "1":
            continue

        entry_date = date.fromisoformat(r["entry_date"])
        entry_price = float(r["entry_price"])
        ticker = r["ticker"]
        strategy = r.get("strategy", "?")
        direction = r["direction"]
        closed = False

        for days, label, target_pct in US_SWING_CHECKPOINTS:
            flag_key = f"checked_{label}"
            if r.get(flag_key, "0") == "1":
                continue

            trading_days_passed = np.busday_count(entry_date, today)
            if trading_days_passed < days:
                break  # bu checkpoint'e daha ulasilmadi

            try:
                current_price = yf.Ticker(ticker).history(period="1d")["Close"].iloc[-1]
                raw_pct = (current_price - entry_price) / entry_price * 100
                pct_change = raw_pct if direction == "LONG" else -raw_pct
                success = pct_change >= target_pct

                log_us_swing_outcome(ticker, strategy, direction, entry_price, r["entry_date"], days, label,
                                      target_pct, current_price, pct_change, success)
                r[flag_key] = "1"

                if success:
                    msg = (
                        f"🎯 [ABD Swing / {strategy}] {ticker} {direction} - {label} checkpoint'te hedef tutturuldu\n"
                        f"Giriş: {entry_price:.2f} | Şimdi: {current_price:.2f}\n"
                        f"Değişim: {pct_change:+.2f}% (hedef: %{target_pct})\n\n"
                        f"Öneri: kârı realize etmeyi değerlendir."
                    )
                    send_telegram_message(msg)
                    r["closed"] = "1"
                    closed = True
                    break
                elif label == US_SWING_CHECKPOINTS[-1][1]:
                    msg = (
                        f"⏱ [ABD Swing / {strategy}] {ticker} {direction} - 10 gün sonunda hiçbir checkpoint'te hedef tutmadı\n"
                        f"Giriş: {entry_price:.2f} | Şimdi: {current_price:.2f}\n"
                        f"Son değişim: {pct_change:+.2f}%\n\nSinyal geçersiz sayılıyor, kapatılıyor."
                    )
                    send_telegram_message(msg)
                    r["closed"] = "1"
                    closed = True
            except Exception as e:
                print(f"{ticker} ABD swing sonuc kontrolu hatasi: {e}")
                break

        if not closed:
            still_pending.append(r)

    _write_us_swing_pending(still_pending)


def scan_us_swing(tickers: list):
    print(f"\n[{datetime.now().strftime('%H:%M:%S')}] ABD swing taramasi basliyor (Hacim Z-Skor + ATR Kirilimi)...")

    check_us_swing_outcomes()

    # 1. KATMAN: Piyasa Beyni (SPY endeksi) - yonlendirici filtre
    allowed, allowed_direction, regime, regime_note = market_scan_allowed("ABD")
    if not allowed:
        send_telegram_message(
            f"⏸️ [PİYASA BEYNİ] ABD swing: piyasa gürültülü (YATAY), hisse taraması durduruldu.\n"
            f"SPY rejimi: {regime} ({regime_note})"
        )
        print(f"ABD swing: rejim {regime} - tarama atlandi")
        return

    today = datetime.now(ZoneInfo("America/New_York")).date()
    results = []

    for ticker in tickers:
        try:
            df = fetch_daily_df(ticker)
            if df.empty or len(df) < 25:
                print(f"{ticker}: yetersiz veri")
                continue
            df = compute_indicators(df)

            fired_directions = set()

            for strategy_name, gate_fn in [
                ("Hacim Z-Skor", check_us_volume_zscore),
                ("ATR Kirilimi", check_us_atr_breakout),
            ]:
                gate_result = gate_fn(df)
                if not gate_result:
                    continue

                direction, row = gate_result
                if allowed_direction and direction != allowed_direction:
                    print(f"{ticker}: {direction} sinyali rejim ({regime}) ile uyusmuyor, atlandi")
                    continue
                if direction in fired_directions:
                    continue
                fired_directions.add(direction)

                entry_price = float(row["close"])
                detail = (f"Hacim Z-Skor: {row['vol_zscore']:.2f}" if strategy_name == "Hacim Z-Skor"
                          else f"ATR: {row['atr14']:.2f}, hareket: {row['close'] - df.iloc[-2]['close']:+.2f}")

                log_signal(ticker, "ABD-swing", strategy_name, direction, row, [detail])
                log_us_swing_pending(ticker, strategy_name, direction, entry_price, today)

                # 2. KATMAN: Portfoy Beyni (bakiye ayarliysa adet, degilse stop mesafesi)
                stop_price = compute_invalidation(direction, row)
                stop_distance = abs(entry_price - stop_price)
                tp_price = (entry_price + stop_distance * PARTIAL_TP_R_MULT if direction == "LONG"
                            else entry_price - stop_distance * PARTIAL_TP_R_MULT)
                emir = sizing_line("ABD", entry_price, stop_price)

                # 3. KATMAN: cikis uyarilari icin takibe al.
                # ONEMLI: takip, pozisyon boyutundan BAGIMSIZ olmali - bakiye
                # ayarli olmasa bile parsiyel TP / stop uyarilari gelmeli.
                qty_for_tracking, _, _, _ = compute_position_size("ABD", entry_price, stop_price)
                if stop_distance > 0:
                    track_new_signal(ticker, "ABD", strategy_name, direction,
                                     entry_price, stop_price, tp_price, qty_for_tracking)

                checkpoint_text = " / ".join(f"{label}(%{target})" for _, label, target in US_SWING_CHECKPOINTS)
                results.append(
                    f"{'🟢 LONG' if direction == 'LONG' else '🔴 SHORT'} {ticker} [{strategy_name}]\n"
                    f"Giriş: {entry_price:.2f} | {detail}\n"
                    f"{emir}\n"
                    f"🛑 Stop: {stop_price:.2f} | 🎯 Parsiyel TP ({PARTIAL_TP_R_MULT}R): {tp_price:.2f}\n"
                    f"Checkpoint hedefleri: {checkpoint_text}\n"
                )

            if not fired_directions:
                print(f"{ticker}: kriter yok")

        except Exception as e:
            print(f"{ticker} hata: {e}")
            dedektif_report("ABD swing taramasi", e, ticker)

    if results:
        msg = (f"📊 ABD Swing Sinyalleri\n"
               f"🧠 Piyasa rejimi: {regime} ({regime_note})\n\n" + "\n".join(results))
        if PORTFOLIO_BALANCE_USD is not None:
            msg += "\n" + OPTIONS_SIZING_NOTE
        print(msg)
        send_telegram_message(msg)
    else:
        print("ABD swing: bugun kriterlere uyan hisse bulunamadi")


US_GUNICI_PENDING_FILE = _data_path("us_gunici_pending.csv")
US_GUNICI_OUTCOME_FILE = _data_path("us_gunici_outcomes.csv")

# (dakika, etiket, hedef_yuzde) - genisletilmis turnuvadaki US_CHECKPOINTS ile ayni
US_GUNICI_CHECKPOINTS = [
    (15, "15dk", 0.15), (30, "30dk", 0.25), (60, "1sa", 0.40), (120, "2sa", 0.60), (240, "4sa", 0.90),
]

US_GUNICI_PENDING_FIELDNAMES = ["ticker", "direction", "entry_price", "entry_time"] + [
    f"checked_{label}" for _, label, _ in US_GUNICI_CHECKPOINTS
] + ["closed"]


def log_us_gunici_pending(ticker: str, direction: str, entry_price: float, entry_time: datetime):
    file_exists = os.path.isfile(US_GUNICI_PENDING_FILE)
    with open(US_GUNICI_PENDING_FILE, "a", newline="") as f:
        writer = csv.writer(f)
        if not file_exists:
            writer.writerow(US_GUNICI_PENDING_FIELDNAMES)
        row = [ticker, direction, entry_price, entry_time.isoformat()]
        row += ["0" for _ in US_GUNICI_CHECKPOINTS]
        row += ["0"]
        writer.writerow(row)


def _read_us_gunici_pending():
    if not os.path.isfile(US_GUNICI_PENDING_FILE):
        return []
    with open(US_GUNICI_PENDING_FILE, newline="") as f:
        return list(csv.DictReader(f))


def _write_us_gunici_pending(rows):
    with open(US_GUNICI_PENDING_FILE, "w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=US_GUNICI_PENDING_FIELDNAMES)
        writer.writeheader()
        writer.writerows(rows)


def log_us_gunici_outcome(ticker, direction, entry_price, entry_time, minutes, label, target_pct,
                           current_price, pct_change, success):
    file_exists = os.path.isfile(US_GUNICI_OUTCOME_FILE)
    with open(US_GUNICI_OUTCOME_FILE, "a", newline="") as f:
        writer = csv.writer(f)
        if not file_exists:
            writer.writerow([
                "ticker", "direction", "entry_price", "entry_time", "dakika", "checkpoint",
                "target_pct", "price_now", "pct_change", "success"
            ])
        writer.writerow([
            ticker, direction, entry_price, entry_time, minutes, label,
            target_pct, current_price, f"{pct_change:.3f}", success
        ])


def check_us_gunici_outcomes():
    rows = _read_us_gunici_pending()
    if not rows:
        return

    now = datetime.now(ZoneInfo("America/New_York"))
    still_pending = []

    for r in rows:
        if r.get("closed", "0") == "1":
            continue

        entry_time = datetime.fromisoformat(r["entry_time"])
        entry_price = float(r["entry_price"])
        ticker = r["ticker"]
        direction = r["direction"]
        closed = False

        for minutes, label, target_pct in US_GUNICI_CHECKPOINTS:
            flag_key = f"checked_{label}"
            if r.get(flag_key, "0") == "1":
                continue
            if now < entry_time + pd.Timedelta(minutes=minutes):
                break  # bu checkpoint'e daha ulasilmadi

            try:
                current_price = yf.Ticker(ticker).history(period="1d", interval="1m")["Close"].iloc[-1]
                raw_pct = (current_price - entry_price) / entry_price * 100
                pct_change = raw_pct if direction == "LONG" else -raw_pct
                success = pct_change >= target_pct

                log_us_gunici_outcome(ticker, direction, entry_price, r["entry_time"], minutes, label,
                                       target_pct, current_price, pct_change, success)
                r[flag_key] = "1"

                if success:
                    msg = (
                        f"🎯 [ABD Gün İçi / RSI21] {ticker} {direction} - {label} checkpoint'te hedef tutturuldu\n"
                        f"Giriş: {entry_price:.2f} | Şimdi: {current_price:.2f}\n"
                        f"Değişim: {pct_change:+.2f}% (hedef: %{target_pct})\n\n"
                        f"Öneri: kârı realize etmeyi değerlendir. (Bu ham fiyat hareketi - senin gerçek opsiyon "
                        f"maliyetine göre net sonucun farklı olabilir.)"
                    )
                    send_telegram_message(msg)
                    r["closed"] = "1"
                    closed = True
                    break
                elif label == US_GUNICI_CHECKPOINTS[-1][1]:
                    msg = (
                        f"⏱ [ABD Gün İçi / RSI21] {ticker} {direction} - 4sa sonunda hiçbir checkpoint'te hedef tutmadı\n"
                        f"Giriş: {entry_price:.2f} | Şimdi: {current_price:.2f}\n"
                        f"Son değişim: {pct_change:+.2f}%\n\nSinyal geçersiz sayılıyor, kapatılıyor."
                    )
                    send_telegram_message(msg)
                    r["closed"] = "1"
                    closed = True
            except Exception as e:
                print(f"{ticker} ABD gun ici sonuc kontrolu hatasi: {e}")
                break

        if not closed:
            still_pending.append(r)

    _write_us_gunici_pending(still_pending)


# ============================================================
# 3 MOTORLU GÜN İÇİ MİMARİ (2026-08-04) — kripto botundakinin aynısı
# ============================================================
# Kripto botunda kullandigimiz mimarinin hisse senedine uyarlanmis hali.
# ONEMLI: mevcut BIST gunluk sistemi (turnuvada %78.2 isabet) SILINMEDI -
# bu AYRI bir gun ici koludur, ikisi paralel calisir ve karsilastirilabilir.
#
# Kriptodan farklar (zorunlu uyarlamalar):
#   * Kripto 7/24 acik, hisse degil -> gun ici pozisyon geceye tasinmaz,
#     sinyal mesajinda "seans sonunda kapat" notu var.
#   * Kriptodaki H4 Swing motorunun hisse karsiligi GUNLUK trend + M15 giris
#     (BIST seansi 8 saat oldugu icin H4 gunde sadece 2 mum eder, anlamsiz).
#   * Bot islem ACMIYOR -> cikislar emir talimati olarak bildirilir.
#
# Her motor ayni sozlesmeyi doner: (yon, giris, stop, motor_adi) ya da None.
# TP motorlar tarafindan belirlenmez, her zaman sabit 1:2 R:R'dir.

M15_RR_RATIO = float(os.environ.get("M15_RR_RATIO", "2.0"))
M15_SCAN_INTERVAL_MINUTES = int(os.environ.get("M15_SCAN_INTERVAL_MINUTES", "15"))
M15_BB_PERIOD = int(os.environ.get("M15_BB_PERIOD", "20"))
M15_SQUEEZE_LOOKBACK = int(os.environ.get("M15_SQUEEZE_LOOKBACK", "50"))
M15_SQUEEZE_PCT = float(os.environ.get("M15_SQUEEZE_PCT", "25"))
M15_VOLUME_MULT = float(os.environ.get("M15_VOLUME_MULT", "1.5"))
M15_RANGE_LOOKBACK = int(os.environ.get("M15_RANGE_LOOKBACK", "20"))
M15_WICK_MIN_ATR = float(os.environ.get("M15_WICK_MIN_ATR", "0.3"))
M15_WICK_MIN_RATIO = float(os.environ.get("M15_WICK_MIN_RATIO", "0.5"))
# Ayni hisse+yon icin gunde bir kez sinyal (spam onlemi)
_m15_alerted = {}


def _m15_bollinger(df, period=M15_BB_PERIOD, mult=2.0):
    mid = df["close"].rolling(period).mean()
    std = df["close"].rolling(period).std()
    upper, lower = mid + std * mult, mid - std * mult
    width = (upper - lower) / mid.replace(0, np.nan) * 100
    return mid, upper, lower, width


def _m15_result(direction, entry, stop, name):
    """Bozuk stop'u baştan eler — yanlis tarafta ya da asiri dar bir stop
    1:2 hesabini ve stop mesafesi yuzdesini sacmalastirirdi."""
    if entry is None or stop is None or entry <= 0 or stop <= 0:
        return None
    if direction == "LONG" and stop >= entry:
        return None
    if direction == "SHORT" and stop <= entry:
        return None
    if abs(entry - stop) / entry < 0.0015:   # %0.15'ten dar = gurultu
        return None
    return direction, float(entry), float(stop), name


def m15_engine_breakout(df):
    """A) BREAKOUT — Bollinger sikismasi + hacimli kirilim."""
    if len(df) < M15_SQUEEZE_LOOKBACK + M15_BB_PERIOD + 5:
        return None
    mid, up, low, width = _m15_bollinger(df)
    d = df.assign(bb_up=up, bb_low=low, bb_width=width,
                  vol_ma=df["volume"].rolling(20).mean())
    row = d.iloc[-2]   # son KAPANMIS mum (canli mum -1'de, ona guvenmiyoruz)
    if any(pd.isna(row[c]) for c in ("bb_width", "vol_ma", "atr14")):
        return None
    prior = d["bb_width"].iloc[-(M15_SQUEEZE_LOOKBACK + 2):-2].dropna()
    if prior.empty:
        return None
    # Sikisma kirilim mumundan ONCEKI mumda olculur (kirilim bantlari acar)
    if d["bb_width"].iloc[-3] > np.nanpercentile(prior, M15_SQUEEZE_PCT):
        return None
    if row["volume"] < row["vol_ma"] * M15_VOLUME_MULT:
        return None
    # Kirilim, kendi bandiyla degil ONCEKI mumun bandiyla karsilastirilir
    pu, pl = d["bb_up"].iloc[-3], d["bb_low"].iloc[-3]
    if pd.isna(pu) or pd.isna(pl):
        return None
    atr = float(row["atr14"])
    if row["close"] > pu:
        return _m15_result("LONG", row["close"], row["low"] - atr * 0.5, "BREAKOUT")
    if row["close"] < pl:
        return _m15_result("SHORT", row["close"], row["high"] + atr * 0.5, "BREAKOUT")
    return None


def m15_engine_liquidity(df):
    """B) LİKİDİTE AVCISI — kanal disina atilan igne, iceri kapanis, tuzak yonu."""
    if len(df) < M15_RANGE_LOOKBACK + 20:
        return None
    row = df.iloc[-2]
    if pd.isna(row.get("atr14")):
        return None
    atr = float(row["atr14"])
    win = df.iloc[-(M15_RANGE_LOOKBACK + 2):-2]   # igne mumu HARIC
    if win.empty:
        return None
    rh, rl = float(win["high"].max()), float(win["low"].min())
    if rh <= rl:
        return None
    crange = float(row["high"] - row["low"])
    if crange <= 0:
        return None
    uw = float(row["high"] - max(row["close"], row["open"]))
    if (row["high"] > rh + atr * M15_WICK_MIN_ATR and row["close"] < rh
            and uw / crange >= M15_WICK_MIN_RATIO):
        return _m15_result("SHORT", row["close"], row["high"] + atr * 0.2, "LIKIDITE")
    lw = float(min(row["close"], row["open"]) - row["low"])
    if (row["low"] < rl - atr * M15_WICK_MIN_ATR and row["close"] > rl
            and lw / crange >= M15_WICK_MIN_RATIO):
        return _m15_result("LONG", row["close"], row["low"] - atr * 0.2, "LIKIDITE")
    return None


def m15_engine_trend(df, daily_df):
    """C) TREND MOTORU — kriptodaki H4 Swing'in hisse karsiligi.
    Yon GUNLUK grafikten (EMA50 + fiyat), zamanlama M15'ten gelir.
    Amac: M15 gurultusunde yon secmemek."""
    if daily_df is None or daily_df.empty or len(daily_df) < 55:
        return None
    d = daily_df.copy()
    d["ema50"] = d["close"].ewm(span=50, adjust=False).mean()
    drow = d.iloc[-1]
    if pd.isna(drow["ema50"]):
        return None
    if drow["close"] > drow["ema50"]:
        bias = "LONG"
    elif drow["close"] < drow["ema50"]:
        bias = "SHORT"
    else:
        return None

    if len(df) < 60:
        return None
    row = df.iloc[-2]
    if pd.isna(row.get("ema20")) or pd.isna(row.get("atr14")):
        return None
    atr = float(row["atr14"])
    tol = float(row["close"]) * 0.004   # EMA20'ye "yakin" toleransi
    near = (abs(float(row["low"]) - float(row["ema20"])) <= tol or
            abs(float(row["high"]) - float(row["ema20"])) <= tol)
    if not near:
        return None
    win = df.iloc[-22:-2]
    if win.empty:
        return None
    if bias == "LONG" and row["close"] > row["open"]:
        swing = min(float(win["low"].min()), float(row["low"]))
        return _m15_result("LONG", row["close"], swing - atr * 0.3, "TREND")
    if bias == "SHORT" and row["close"] < row["open"]:
        swing = max(float(win["high"].max()), float(row["high"]))
        return _m15_result("SHORT", row["close"], swing + atr * 0.3, "TREND")
    return None


def active_m15_engines(regime: str):
    """Kripto botundaki active_engines() ile ayni mantik: rejim hangi
    motorlarin calisacagini secer. BREAKOUT her rejimde aday cunku sikisma
    HISSE BAZINDA tespit edilir, endeks rejiminden bagimsizdir."""
    if regime == "YUKSELIS" or regime == "DUSUS":
        return ["TREND", "BREAKOUT"]
    if regime == "YATAY":
        return ["LIKIDITE", "BREAKOUT"]
    return ["BREAKOUT"]


def scan_m15_engines(market: str, tickers: list):
    """Gun ici 3 motorlu tarama. Sinyal uretirse Telegram'a emir talimati
    gonderir. Bot islem ACMAZ."""
    allowed, allowed_direction, regime, regime_note = market_scan_allowed(market)
    if not allowed:
        print(f"{market} M15: rejim {regime} - tarama atlandi")
        return

    engines = active_m15_engines(regime)
    today = datetime.now().date().isoformat()
    results = []

    for ticker in tickers:
        try:
            df = fetch_intraday_df(ticker, interval="15m", period="5d")
            if df.empty or len(df) < 80:
                continue
            df = compute_indicators(df)

            daily = None
            if "TREND" in engines:
                daily = fetch_daily_df(ticker, period="6mo")

            result = None
            for eng in engines:   # ilk sinyal veren kazanir
                if eng == "BREAKOUT":
                    result = m15_engine_breakout(df)
                elif eng == "LIKIDITE":
                    result = m15_engine_liquidity(df)
                elif eng == "TREND":
                    result = m15_engine_trend(df, daily)
                if result:
                    break
            if not result:
                continue

            direction, entry, stop, engine_name = result
            if allowed_direction and direction != allowed_direction:
                continue
            key = f"{market}|{ticker}|{direction}|{today}"
            if _m15_alerted.get(key):
                continue
            _m15_alerted[key] = True

            dist = abs(entry - stop)
            tp = entry + dist * M15_RR_RATIO if direction == "LONG" else entry - dist * M15_RR_RATIO
            results.append({
                "ticker": ticker, "engine": engine_name, "direction": direction,
                "entry": entry, "stop": stop, "tp": tp,
                "sizing": sizing_line(market, entry, stop),
            })
        except Exception as e:
            print(f"{ticker} M15 hata: {e}")
            dedektif_report(f"{market} M15 motor taraması", e, ticker)

    if not results:
        print(f"{market} M15: sinyal yok (rejim {regime}, motorlar {engines})")
        return

    lines = [f"⚙️ [GÜN İÇİ MOTOR SİNYALİ] {market}",
             f"🧠 Rejim: {regime} | Aktif motorlar: {', '.join(engines)}\n"]
    for r in results:
        yon = "🟢 LONG" if r["direction"] == "LONG" else "🔴 SHORT"
        if market == "BIST" and r["direction"] == "SHORT":
            yon = "🔴 [BİLGİ AMAÇLI SHORT]"
        lines.append(
            f"{yon} {r['ticker']} — motor: {r['engine']}\n"
            f"Giriş: {r['entry']:.2f} | 🛑 Stop: {r['stop']:.2f} | "
            f"🎯 TP (1:{M15_RR_RATIO:g}): {r['tp']:.2f}\n"
            f"{r['sizing']}\n"
        )
    lines.append("⏰ Gün içi sinyaldir — pozisyonu seans sonunda kapat, geceye taşıma.")
    lines.append("⚠️ Bu motorlar backtest edilmedi; canlı gözlem aşamasındadır.")
    msg = "\n".join(lines)
    print(msg)
    send_telegram_message(msg)


def scan_us_gunici(tickers: list):
    global _last_gunici_halt_regime
    print(f"\n[{datetime.now().strftime('%H:%M:%S')}] ABD gun ici (RSI21) taramasi basliyor...")

    check_us_gunici_outcomes()

    # 1. KATMAN: Piyasa Beyni. Bu tarama gun icinde sik calistigi icin
    # her seferinde "durduruldu" mesaji atmak spam olur - rejim degisimi
    # oldugunda bir kez haber verip sonra sessizce atliyoruz.
    allowed, allowed_direction, regime, regime_note = market_scan_allowed("ABD")
    if not allowed:
        if _last_gunici_halt_regime != regime:
            send_telegram_message(
                f"⏸️ [PİYASA BEYNİ] ABD gün içi: piyasa gürültülü (YATAY), tarama durduruldu.\n"
                f"SPY rejimi: {regime} ({regime_note})\n"
                f"Rejim düzelene kadar gün içi sinyal üretilmeyecek."
            )
            _last_gunici_halt_regime = regime
        print(f"ABD gun ici: rejim {regime} - tarama atlandi")
        return
    _last_gunici_halt_regime = None

    now_ny = datetime.now(ZoneInfo("America/New_York"))
    results = []

    for ticker in tickers:
        try:
            df = fetch_intraday_df(ticker)
            if df.empty or len(df) < 25:
                print(f"{ticker}: yetersiz veri")
                continue
            df = compute_indicators(df)

            gate_result = check_us_rsi21_gunici(df)
            if not gate_result:
                print(f"{ticker}: kriter yok")
                continue

            direction, row = gate_result
            if allowed_direction and direction != allowed_direction:
                print(f"{ticker}: {direction} sinyali rejim ({regime}) ile uyusmuyor, atlandi")
                continue
            entry_price = row["close"]

            log_signal(ticker, "ABD-gunici", "RSI21", direction, row, [f"RSI21: {row['rsi21']:.1f}"])
            log_us_gunici_pending(ticker, direction, entry_price, now_ny)

            checkpoint_text = " / ".join(f"{label}(%{target})" for _, label, target in US_GUNICI_CHECKPOINTS)
            results.append(
                f"{'🟢 LONG' if direction == 'LONG' else '🔴 SHORT'} {ticker} [RSI21, sinyal-amaçlı]\n"
                f"Giriş: {entry_price:.2f} | RSI21: {row['rsi21']:.1f}\n"
                f"Checkpoint hedefleri: {checkpoint_text}\n"
                f"⚡ Gün içi hızlı sinyal - Manuel takip önerilir.\n"
                f"Not: bu HAM fiyat hareketi test ediyor, otomatik işlem yapmıyor — kendi opsiyon maliyetine göre değerlendir.\n"
            )

        except Exception as e:
            print(f"{ticker} hata: {e}")
            dedektif_report("ABD gün içi taramasi", e, ticker)

    if results:
        msg = (f"📊 ABD Gün İçi - RSI21 Sinyalleri (test amaçlı)\n"
               f"🧠 Piyasa rejimi: {regime} ({regime_note})\n\n" + "\n".join(results))
        print(msg)
        send_telegram_message(msg)
    else:
        print("ABD gun ici: bugun kriterlere uyan hisse bulunamadi")


def log_signal(ticker: str, market: str, strategy: str, direction: str, row, breakdown: list):
    file_exists = os.path.isfile(SIGNAL_LOG_FILE)
    with open(SIGNAL_LOG_FILE, "a", newline="") as f:
        writer = csv.writer(f)
        if not file_exists:
            writer.writerow([
                "date", "ticker", "market", "strategy", "direction", "price", "rsi", "breakdown"
            ])
        writer.writerow([
            datetime.now().isoformat(), ticker, market, strategy, direction,
            row["close"], row["rsi"], " | ".join(breakdown)
        ])


# ---------------------------------------------------------------------------
# Tarama
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# GÜN İÇİ ÖN UYARI (RADAR) TARAMASI — B/C hibrit modeli
# ---------------------------------------------------------------------------
# Gemini'nin karari (2026-07-30): gunluk stratejilerin istatistiksel gucunu
# korumak icin KESIN sinyal yine SADECE kapanmis bar ile (17:35 taramasi)
# uretilir. Bu radar taramasi seans ortasinda calisir, sartlar OLUSMAKTA olan
# gunluk mumda saglaniyorsa "ON UYARI" gonderir.
#
# KRITIK AYRIM - bu radar bilerek sunlari YAPMAZ:
#   * track_new_signal cagirmaz  -> cikis takibine girmez (takip sadece teyit
#     edilmis sinyaller icin; yoksa kapanista gecersizlesen bir kurulum
#     sonsuza kadar takipte kalirdi)
#   * pozisyon boyutu / EMIR satiri basmaz -> emir talimati degil, radar
#   * turnuva istatistikleri (%78.2) bu uyarilar icin GECERLI DEGILDIR, cunku
#     kapanmamis mum uzerinde olculuyor. Mesajda bu acikca yazili.
BIST_RADAR_TIMES = [(13, 0), (15, 30)]  # Europe/Istanbul


def _radar_alert_key(now) -> str:
    return f"radar_alerts_{now.date().isoformat()}"


def _already_radar_alerted(now, ticker: str, direction: str) -> bool:
    """Ayni hisse+yon icin gunde bir kez uyarir. 13:00 ve 15:30 taramalarinin
    ayni kurulumu iki kez bildirmesini onler; ayrica diske yazildigi icin
    yeniden baslatma da tekrar uyariya yol acmaz."""
    state = _load_run_state()
    return f"{ticker}|{direction}" in state.get(_radar_alert_key(now), [])


def _mark_radar_alerted(now, ticker: str, direction: str):
    state = _load_run_state()
    key = _radar_alert_key(now)
    alerted = state.get(key, [])
    alerted.append(f"{ticker}|{direction}")
    # Sadece bugunun listesini tut - eski gunlerin kayitlari birikmesin.
    state = {k: v for k, v in state.items() if not k.startswith("radar_alerts_") or k == key}
    state[key] = alerted
    _save_run_state(state)


def scan_bist_radar(tickers: list, label: str):
    now = datetime.now(ZoneInfo("Europe/Istanbul"))
    print(f"\n[{now.strftime('%H:%M:%S')}] {label} ÖN UYARI (radar) taramasi basliyor...")

    # Rejim filtresi kesin sinyalle ayni mantikta uygulanir - radar da
    # rejime aykiri yonde uyari vermesin.
    allowed, allowed_direction, regime, regime_note = market_scan_allowed("BIST")
    if not allowed:
        print(f"{label} radar: rejim {regime} - atlandi")
        return

    found = []
    for ticker in tickers:
        try:
            df = fetch_daily_df(ticker)
            if df.empty or len(df) < 25:
                continue
            df = compute_indicators(df)

            fired = set()
            for strategy_name, gate_fn in [
                ("Fitil+RSI+Hacim", check_exhaustion),
                ("Sadece RSI", check_rsi_only),
            ]:
                gate_result = gate_fn(df)
                if not gate_result:
                    continue
                direction, row = gate_result
                if allowed_direction and direction != allowed_direction:
                    continue
                if direction in fired:
                    continue

                pts_trend, _ = score_trend(df, direction)
                if pts_trend < 0:
                    continue
                if _already_radar_alerted(now, ticker, direction):
                    continue

                fired.add(direction)
                _mark_radar_alerted(now, ticker, direction)
                found.append({
                    "ticker": ticker, "strategy": strategy_name, "direction": direction,
                    "price": float(row["close"]), "rsi": float(row["rsi"]),
                    "stop": compute_invalidation(direction, row),
                })
        except Exception as e:
            print(f"{ticker} radar hatasi: {e}")
            dedektif_report(f"{label} radar taramasi", e, ticker)

    if not found:
        print(f"{label} radar: su an sartlari olusan hisse yok")
        return

    lines = [
        f"⏳ [ÖN UYARI / RADAR] {now.strftime('%H:%M')} — {label}",
        f"🧠 Rejim: {regime} ({regime_note})\n",
    ]
    for f in found:
        yon = "🟢 LONG" if f["direction"] == "LONG" else "🔴 SHORT"
        lines.append(
            f"{yon} {f['ticker']} [{f['strategy']}]\n"
            f"Şartlar şu an oluşuyor. Fiyat: {f['price']:.2f} | RSI: {f['rsi']:.1f} | "
            f"olası stop: {f['stop']:.2f}\n"
            f"Kapanışta ({BIST_CHECK_HOUR:02d}:{BIST_CHECK_MINUTE:02d}) teyit edilirse KESİN SİNYAL gelecektir.\n"
        )
    lines.append(
        "ℹ️ Bu bir ön uyarıdır, emir talimatı DEĞİLDİR. Mum henüz kapanmadığı için "
        "turnuvada doğrulanmış isabet oranı bu uyarılar için geçerli değildir — "
        "kapanışa kadar şartlar bozulabilir."
    )
    msg = "\n".join(lines)
    print(msg)
    send_telegram_message(msg)


def scan_bist(tickers: list, market_label: str):
    print(f"\n[{datetime.now().strftime('%H:%M:%S')}] {market_label} taramasi basliyor...")

    # 1. KATMAN: Piyasa Beyni - rejim artik taramayi durdurmak yerine yonlendirir.
    allowed, allowed_direction, regime, regime_note = market_scan_allowed("BIST")
    if not allowed:
        send_telegram_message(
            f"⏸️ [PİYASA BEYNİ] {market_label}: piyasa gürültülü (YATAY), hisse taraması durduruldu.\n"
            f"Endeks rejimi: {regime} ({regime_note})"
        )
        print(f"{market_label}: rejim {regime} - tarama atlandi")
        return

    results = []

    for ticker in tickers:
        try:
            df = fetch_daily_df(ticker)
            if df.empty or len(df) < 25:
                print(f"{ticker}: yetersiz veri")
                continue
            df = compute_indicators(df)

            fired_directions = set()

            for strategy_name, gate_fn in [
                ("Fitil+RSI+Hacim", check_exhaustion),
                ("Sadece RSI", check_rsi_only),
            ]:
                gate_result = gate_fn(df)
                if not gate_result:
                    continue

                direction, row = gate_result
                # Rejim yon filtresi: yukselen piyasada sadece LONG, dusen
                # piyasada sadece SHORT sinyali gecerli sayilir.
                if allowed_direction and direction != allowed_direction:
                    print(f"{ticker}: {direction} sinyali rejim ({regime}) ile uyusmuyor, atlandi")
                    continue
                # ayni ticker'da ayni yonde iki strateji birden tetiklenirse tekrar mesaj atma
                if direction in fired_directions:
                    continue
                fired_directions.add(direction)

                breakdown = []
                pts, note = score_bollinger(row)
                breakdown.append(f"Bollinger: {note}")
                pts_trend, note_trend = score_trend(df, direction)
                breakdown.append(f"Kendi trendi: {note_trend}")

                if pts_trend < 0:
                    print(f"{ticker}: {direction} ({strategy_name}) tespit edildi ama kendi trendi tersine guclu, atlandi")
                    continue

                invalidation = compute_invalidation(direction, row)
                log_signal(ticker, market_label, strategy_name, direction, row, breakdown)

                # 2. KATMAN: Portfoy Beyni (bakiye ayarliysa adet, degilse stop mesafesi)
                entry_price = float(row["close"])

                # 1.5R parsiyel TP seviyesi
                stop_distance = abs(entry_price - invalidation)
                tp_price = (entry_price + stop_distance * PARTIAL_TP_R_MULT if direction == "LONG"
                            else entry_price - stop_distance * PARTIAL_TP_R_MULT)
                emir = sizing_line("BIST", entry_price, invalidation)

                # 3. KATMAN: cikis uyarilari icin takibe al.
                # ONEMLI: takip, pozisyon boyutundan BAGIMSIZ olmali - bakiye
                # ayarli olmasa bile parsiyel TP / stop uyarilari gelmeli.
                qty_for_tracking, _, _, _ = compute_position_size("BIST", entry_price, invalidation)
                if stop_distance > 0:
                    track_new_signal(ticker, "BIST", strategy_name, direction,
                                     entry_price, invalidation, tp_price, qty_for_tracking)

                results.append({
                    "ticker": ticker,
                    "strategy": strategy_name,
                    "direction": direction,
                    "price": entry_price,
                    "rsi": row["rsi"],
                    "invalidation": invalidation,
                    "tp_price": tp_price,
                    "emir": emir,
                    "breakdown": breakdown,
                })

            if not fired_directions:
                print(f"{ticker}: kriter yok")

        except Exception as e:
            print(f"{ticker} hata: {e}")
            dedektif_report(f"{market_label} taramasi", e, ticker)

    if results:
        lines = [
            f"✅ [KESİN SİNYAL / KAPANIŞ TEYİDİ] {market_label} - Kapanışa Yakın Tarama",
            f"🧠 Piyasa rejimi: {regime} ({regime_note})\n",
        ]
        for r in results:
            if r["direction"] == "SHORT":
                # Gemini'nin karari: BIST'te aciga satis bireysel yatirimci icin
                # pratikte yok - sinyali gizlemek yerine ne ise yarayacagini
                # acikca yaziyoruz.
                baslik = f"🔴 [BIST - BİLGİ AMAÇLI SHORT] {r['ticker']} [{r['strategy']}]"
                short_note = ("ℹ️ Varant/VİOP tarafında Put pozisyonu veya eldeki hisse için "
                              "kâr alma/stop uyarısıdır.\n")
            else:
                baslik = f"🟢 LONG {r['ticker']} [{r['strategy']}]"
                short_note = ""

            lines.append(
                f"{baslik}\n"
                f"{short_note}"
                f"Fiyat: {r['price']:.2f} | RSI: {r['rsi']:.1f}\n"
                f"{r['emir']}\n"
                f"🛑 Stop: {r['invalidation']:.2f} | 🎯 Parsiyel TP ({PARTIAL_TP_R_MULT}R): {r['tp_price']:.2f}\n"
                f"{' | '.join(r['breakdown'])}\n"
            )
        if PORTFOLIO_BALANCE_TRY is not None:
            lines.append(OPTIONS_SIZING_NOTE)
        msg = "\n".join(lines)
        print(msg)
        send_telegram_message(msg)
    else:
        print(f"{market_label}: bugun kriterlere uyan hisse bulunamadi")
        send_telegram_message(
            f"📊 {market_label}: bugün kriterlere uyan hisse bulunamadı.\n"
            f"🧠 Piyasa rejimi: {regime} ({regime_note})"
        )


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
            log_signal(ticker, "ABD-gunici", "Fitil+RSI+Hacim", direction, row, breakdown)

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


def validate_tickers(tickers: list, label: str) -> list:
    """Acilista her kodu bir kez test eder, veri gelmeyenleri listeden eler.

    DERS (2026-08-04, Render'a tasindiktan sonra): ilk deploy'da BU FONKSIYON
    TUM LISTEYI SILDI (BIST 0, ABD 0). Sebep yfinance'in bu ortamdan veri
    dondurmemesiydi (veri merkezi IP'si hiz limitine takiliyor) - yani kodlar
    olu degildi, ALTYAPI sorunu vardi. Eski hali bunu ayirt edemedigi icin
    bot sessizce hicbir sey taramayan bir kabuga donusuyordu.
    Yeni davranis:
      1) Basarisiz olan her kod, bir kez daha (daha uzun bekleyerek) denenir.
      2) Kodlarin yarisindan fazlasi hala basarisizsa, bu 'liste eskimis'
         degil 'kaynak calismiyor' demektir - liste OLDUGU GIBI korunur ve
         uyari gonderilir. Bos listeyle calismaktansa, birkac olu kodla
         calisip her taramada onlari atlamak cok daha iyidir.
    """
    valid, dead = [], []
    for i, t in enumerate(tickers):
        ok = False
        try:
            # retries=1: dogrulama HIZLI olmali. Yeniden denemeler burada
            # devreye girerse acilis 226 hisse icin ~50 dakikaya cikiyor.
            df = fetch_daily_df(t, period="1mo", retries=1)
            ok = (not df.empty) and len(df) >= 5
        except Exception:
            ok = False
        (valid if ok else dead).append(t)

        # ERKEN CIKIS: ilk VALIDATION_PROBE_SIZE kodun HEPSI basarisizsa,
        # tek tek 226 kodu denemenin anlami yok - kaynak calismiyor demektir.
        # Bu kontrol olmadan bot acilis mesajini bile gonderemeden dakikalarca
        # bekliyordu (2026-08-04'te tam olarak bu yasandi).
        if len(dead) >= VALIDATION_PROBE_SIZE and not valid:
            send_telegram_message(
                f"🚨 [TICKER DOĞRULAMA ATLANDI] {label}: ilk {len(dead)} kodun "
                f"hiçbirinden veri gelmedi.\n"
                f"Veri kaynağı çalışmıyor (yfinance hız limiti / sunucu IP engeli) — "
                f"doğrulama iptal edildi, liste olduğu gibi korunuyor.\n"
                f"Tarama başlayacak; veri gelmeyen kodlar tek tek atlanacak."
            )
            print(f"{label}: kaynak calismiyor - dogrulama atlandi, liste korunuyor")
            return tickers

        time.sleep(VALIDATION_SLEEP_SECONDS)

    # ALTYAPI KORUMASI: cogunluk basarisizsa eleme yapma.
    if tickers and len(dead) > len(tickers) / 2:
        send_telegram_message(
            f"🚨 [TICKER DOĞRULAMA BAŞARISIZ] {label}: {len(dead)}/{len(tickers)} kod "
            f"için veri alınamadı.\n"
            f"Bu kadar yüksek oran, kodların ölü olduğunu değil VERİ KAYNAĞININ "
            f"çalışmadığını gösterir (yfinance hız limiti / sunucu IP engeli).\n"
            f"Liste silinmedi, olduğu gibi korunuyor — tarama devam edecek, "
            f"veri gelmeyen kodlar tek tek atlanacak."
        )
        print(f"{label}: dogrulama basarisiz ({len(dead)}/{len(tickers)}) - liste korunuyor")
        return tickers

    if dead:
        send_telegram_message(
            f"⚠️ [TICKER DOĞRULAMA] {label}: {len(dead)} kod için veri alınamadı, "
            f"taramadan çıkarıldı.\n"
            f"{', '.join(d.replace('.IS', '') for d in dead)}\n"
            f"(Endeksten çıkmış ya da kodu değişmiş olabilir — listeyi güncellemek isteyebilirsin.)"
        )
    print(f"{label}: {len(valid)} gecerli, {len(dead)} elendi")
    return valid


def bist_is_open(now_ist=None) -> bool:
    """BIST seans saatleri: hafta ici 10:00-18:10 (Istanbul). Tatil gunleri
    takvimi burada tutulmuyor - tatilde yfinance zaten bos veri doner ve
    Dedektif'in bogucu sayesinde spam olusmaz."""
    now = now_ist or datetime.now(ZoneInfo("Europe/Istanbul"))
    if now.weekday() >= 5:
        return False
    minutes = now.hour * 60 + now.minute
    return (10 * 60) <= minutes < (18 * 60 + 10)


def us_is_open(now_ny=None) -> bool:
    """ABD seans saatleri: hafta ici 09:30-16:00 (New York)."""
    now = now_ny or datetime.now(ZoneInfo("America/New_York"))
    if now.weekday() >= 5:
        return False
    minutes = now.hour * 60 + now.minute
    return (9 * 60 + 30) <= minutes < (16 * 60)


def any_market_open() -> bool:
    return bist_is_open() or us_is_open()


# ---------------------------------------------------------------------------
# Zamanlama
# ---------------------------------------------------------------------------

def _within_window(now, target_hour, target_minute, window_minutes):
    target_total = target_hour * 60 + target_minute
    now_total = now.hour * 60 + now.minute
    return 0 <= (now_total - target_total) < window_minutes


# ---------------------------------------------------------------------------
# Tarama durumu (yeniden baslatmaya dayanikli zamanlama)
# ---------------------------------------------------------------------------
# SORUN (2026-07-29 tespit): planli taramalar SADECE 5 dakikalik bir pencerede
# (ornek 17:35-17:40) tetikleniyordu ve son-calisma tarihi yalnizca RAM'de
# tutuluyordu. Bot o pencerede yeniden baslarsa (Railway redeploy, cokme,
# acilistaki 226 ticker dogrulamasi birkac dakika surdugu icin ozellikle
# riskli) o gunun taramasi TAMAMEN ve SESSIZCE atlaniyordu - kullanici
# sinyal beklerken hicbir mesaj gelmiyordu ve nedenini anlamanin yolu yoktu.
# COZUM: son calisma tarihini diske yaz + pencereyi kacirdiysak ayni gun
# icinde telafi et (gunluk barlar kapanis sonrasi kesinlestigi icin gec
# calisan bir tarama hala degerlidir).
RUN_STATE_FILE = _data_path("run_state.json")


def _load_run_state() -> dict:
    try:
        with open(RUN_STATE_FILE) as f:
            return json.load(f)
    except Exception:
        return {}


def _save_run_state(state: dict):
    try:
        with open(RUN_STATE_FILE, "w") as f:
            json.dump(state, f)
    except Exception as e:
        print(f"run_state yazilamadi ({e})")


def should_run_daily_scan(key: str, now, target_hour: int, target_minute: int) -> bool:
    """Bugun bu tarama henuz calismadiysa VE planlanan saati gectiyse True.
    Pencere yerine 'gectiyse telafi et' mantigi - yeniden baslatmalara
    dayaniklidir."""
    if now.weekday() >= 5:
        return False
    now_total = now.hour * 60 + now.minute
    if now_total < target_hour * 60 + target_minute:
        return False
    state = _load_run_state()
    return state.get(key) != now.date().isoformat()


def mark_daily_scan_done(key: str, now):
    state = _load_run_state()
    state[key] = now.date().isoformat()
    _save_run_state(state)


EXIT_CHECK_INTERVAL_MINUTES = int(os.environ.get("EXIT_CHECK_INTERVAL_MINUTES", "30"))
# Gunluk yasam sinyali saati (Istanbul). BIST taramasindan sonraya koyuyoruz
# ki o gunun durumunu da yansitsin.
HEARTBEAT_HOUR = int(os.environ.get("HEARTBEAT_HOUR", "19"))
_last_exit_check_time = None


def _self_check():
    """Acilista zorunlu fonksiyonlarin var oldugunu dogrular.

    NEDEN VAR: bu projede tekrarlayan bir hata deseni var - buyuk bir
    duzenleme sirasinda bir fonksiyonun 'def' satiri kazara silinip govdesi
    bir onceki fonksiyona yapisiyor. Sozdizimi gecerli kaldigi icin
    py_compile bunu YAKALAMAZ; hata ancak o fonksiyon cagrildigi anda
    (ornegin gunun 17:35 taramasinda) NameError olarak ortaya cikiyor ve o
    gunun sinyali kayboluyor. Bu kontrol boyle bir durumu aciliste, ilk
    saniyede yakalar."""
    required = [
        "scan_bist", "scan_bist_radar", "scan_us_swing", "scan_us_gunici",
        "check_exit_alerts", "track_new_signal", "market_scan_allowed",
        "compute_indicators", "compute_invalidation", "sizing_line",
        "validate_tickers", "should_run_daily_scan", "mark_daily_scan_done",
        "tamirci_repair", "tamirci_note_success", "dedektif_report",
        "scan_m15_engines", "m15_engine_breakout", "m15_engine_liquidity",
        "m15_engine_trend", "active_m15_engines",
    ]
    missing = [name for name in required
               if not callable(globals().get(name))]
    if missing:
        msg = ("🚨 [BAŞLATMA HATASI] Şu fonksiyonlar bulunamadı: "
               + ", ".join(missing)
               + "\nKod bozulmuş olabilir (silinmiş def satırı?) — bot güvenli şekilde durduruluyor.")
        print(msg)
        send_telegram_message(msg)
        raise SystemExit(1)
    print(f"Oz-kontrol tamam: {len(required)} zorunlu fonksiyon yerinde.")


def run_forever():
    global _last_us_gunici_scan_time
    global _last_exit_check_time
    global _last_football_model_scan_time
    global _last_football_odds_scan_time
    global _last_football_results_update_time
    global BIST_TICKERS, US_TICKERS, US_INTRADAY_TICKERS

    print("Ticker dogrulamasi basliyor (bir kez, acilista)...")
    _self_check()
    # DERS (2026-08-04): baslangic mesaji dogrulamadan SONRA gonderiliyordu.
    # Render'da yfinance calismayinca dogrulama cok uzadi ve kullanici
    # dakikalarca hicbir mesaj alamadi - bot olmus mu calisiyor mu
    # anlasilmiyordu. Artik once "basladim" diyoruz, sonra dogruluyoruz.
    send_telegram_message(
        "⏳ Bot başlatılıyor — hisse listesi doğrulanıyor.\n"
        "Bu birkaç dakika sürebilir; bitince tam başlangıç raporu gelecek."
    )
    BIST_TICKERS = validate_tickers(BIST_TICKERS, "BIST")
    US_TICKERS = validate_tickers(US_TICKERS, "ABD swing")
    US_INTRADAY_TICKERS = validate_tickers(US_INTRADAY_TICKERS, "ABD gün içi")

    football_ok = fb.self_check_football()
    if football_ok:
        fb.send_football_message(
            f"⚽ Maç analiz botu (SPO-QUANT) başladı. Model taraması her "
            f"{fb.FOOTBALL_MODEL_SCAN_INTERVAL_MINUTES} dakikada, oran taraması her "
            f"{fb.FOOTBALL_ODDS_SCAN_INTERVAL_MINUTES} dakikada bir çalışır. "
            f"EV≥%{fb.EV_THRESHOLD*100:.0f} sinyalleri bildirir."
        )

    storage_warning = ""
    if DATA_DIR == ".":
        storage_warning = (
            "\n\n⚠️ DİKKAT: Kalıcı depolama (Railway Volume) ayarlı değil — "
            "her re-deploy'da takip kayıtları silinir. Railway'de Volume ekleyip "
            "DATA_DIR=/data ayarlaman önerilir. (Takibe alınan her sinyal ayrıca "
            "Telegram'a loglanıyor, yani kayıt tamamen kaybolmaz.)"
        )

    if PORTFOLIO_BALANCE_TRY is not None or PORTFOLIO_BALANCE_USD is not None:
        bist_bal = f"{PORTFOLIO_BALANCE_TRY:,.0f} TL" if PORTFOLIO_BALANCE_TRY is not None else "ayarsız"
        usd_bal = f"{PORTFOLIO_BALANCE_USD:,.0f} $" if PORTFOLIO_BALANCE_USD is not None else "ayarsız"
        portfoy_satiri = (
            f"💰 Portföy: BIST {bist_bal} / ABD {usd_bal} | "
            f"işlem başı risk %{RISK_PER_TRADE_PCT} | pozisyon tavanı %{MAX_POSITION_PCT_OF_BALANCE}\n"
        )
    else:
        portfoy_satiri = (
            "📏 Portföy bakiyesi ayarlanmadı — sinyallerde adet yerine stop mesafesi (%) gösterilecek, "
            "pozisyon büyüklüğü kararı sende.\n"
        )

    send_telegram_message(
        "🚀 BIST + ABD hisse tarama botu (4 KATMANLI MİMARİ) başlatıldı.\n"
        "1️⃣ Piyasa Beyni | 2️⃣ Portföy/Risk Beyni | 3️⃣ Hibrit Çıkış Uyarıları | 4️⃣ Sistem Dedektifi\n\n"
        f"🧠 Piyasa Beyni: BIST için XU100, ABD için SPY (ADX+EMA200). "
        f"YÜKSELİŞ → sadece LONG, DÜŞÜŞ → sadece SHORT, YATAY → tarama durur.\n"
        + portfoy_satiri +
        f"🎯 Çıkış: {PARTIAL_TP_R_MULT}R'de %50 satış + breakeven uyarısı, sonra {TRAIL_ATR_MULT}×ATR trailing stop "
        f"(her {EXIT_CHECK_INTERVAL_MINUTES} dk, sadece piyasa açıkken).\n\n"
        f"BIST: {len(BIST_TICKERS)} hisse, her gün ~{BIST_CHECK_HOUR:02d}:{BIST_CHECK_MINUTE:02d} (İstanbul) KESİN SİNYAL (kapanmış bar). "
        f"İki strateji: Fitil+RSI+Hacim + Sadece RSI.\n"
        f"⏳ BIST ÖN UYARI (radar): "
        + ", ".join(f"{h:02d}:{m:02d}" for h, m in BIST_RADAR_TIMES)
        + " — şartlar oluşuyorsa haber verir, kapanış teyidi beklenir (emir talimatı değil).\n"
        f"ABD gün içi: {len(US_INTRADAY_TICKERS)} hisse (dar liste, 15 dk'da bir), piyasa açıkken. Strateji: RSI21 aşırı uç "
        f"(SİNYAL AMAÇLI — kendi opsiyon maliyetine göre değerlendir).\n"
        f"ABD swing: {len(US_TICKERS)} hisse, ABD kapanışından sonra (~{US_SWING_CHECK_HOUR:02d}:{US_SWING_CHECK_MINUTE:02d} New York). "
        f"İki strateji: Hacim Z-Skor + ATR Kırılımı.\n"
        f"\n⚙️ GÜN İÇİ 3 MOTOR (kripto mimarisi, YENİ): Breakout / Likidite Avcısı / Trend — "
        f"her {M15_SCAN_INTERVAL_MINUTES} dk, iki piyasa da seans içinde. Sabit 1:{M15_RR_RATIO:g} R:R. "
        f"Rejim motoru seçer. Backtest edilmedi, canlı gözlem aşamasında.\n\n"
        f"⚠️ Bot işlem AÇMIYOR — sadece emir talimatı ve takip uyarısı gönderir."
        + storage_warning
    )

    # Bir onceki dongude cokme olsa bile bot yasamaya devam etsin: her tarama
    # kendi try/except'inde. ONCEDEN: taramalar ciplak cagriliyordu, yani
    # tarama fonksiyonunun kendi ic hata yakalayicilarinin DISINDA olusan bir
    # istisna butun run_forever dongusunu oldururdu - hicbir Telegram bildirimi
    # gonderilmeden. Bot olmus mu yoksa sinyal mi yok, ayirt edilemiyordu.
    heartbeat_key = "heartbeat"

    while True:
        istanbul_now = datetime.now(ZoneInfo("Europe/Istanbul"))
        ny_now = datetime.now(ZoneInfo("America/New_York"))

        # GÜN İÇİ ÖN UYARI (RADAR): seans ortasinda, kesin sinyalden ONCE.
        # Her saat icin ayri bir durum anahtari kullaniyoruz ki 13:00 ve 15:30
        # bagimsiz calissin; telafi mantigi burada da gecerli, ama radar
        # kapanistan SONRA anlamsizlastigi icin sadece seans icinde tetiklenir.
        if bist_is_open(istanbul_now):
            for r_hour, r_minute in BIST_RADAR_TIMES:
                radar_key = f"bist_radar_{r_hour:02d}{r_minute:02d}"
                if should_run_daily_scan(radar_key, istanbul_now, r_hour, r_minute):
                    try:
                        scan_bist_radar(BIST_TICKERS, "BIST")
                        mark_daily_scan_done(radar_key, istanbul_now)
                    except Exception as e:
                        dedektif_report("BIST radar taraması (döngü)", e)

        if should_run_daily_scan("bist", istanbul_now, BIST_CHECK_HOUR, BIST_CHECK_MINUTE):
            try:
                scan_bist(BIST_TICKERS, "BIST")
                mark_daily_scan_done("bist", istanbul_now)
            except Exception as e:
                dedektif_report("BIST taraması (döngü)", e)
                # Tarihi ISARETLEMIYORUZ - bir sonraki dongude tekrar denesin.

        if should_run_daily_scan("us_swing", ny_now, US_SWING_CHECK_HOUR, US_SWING_CHECK_MINUTE):
            try:
                scan_us_swing(US_TICKERS)
                mark_daily_scan_done("us_swing", ny_now)
            except Exception as e:
                dedektif_report("ABD swing taraması (döngü)", e)

        ny_minutes = ny_now.hour * 60 + ny_now.minute
        market_open = 9 * 60 + 30
        market_close = 16 * 60
        if ny_now.weekday() < 5 and market_open <= ny_minutes < market_close:
            if (_last_us_gunici_scan_time is None or
                    (ny_now - _last_us_gunici_scan_time).total_seconds() >= US_GUNICI_SCAN_INTERVAL_MINUTES * 60):
                try:
                    scan_us_gunici(US_INTRADAY_TICKERS)
                except Exception as e:
                    dedektif_report("ABD gün içi taraması (döngü)", e)
                _last_us_gunici_scan_time = ny_now

        # GÜN İÇİ 3 MOTOR (kripto mimarisi): her iki piyasa da kendi seansi
        # icinde taranir. Ayri bir zamanlayici kullaniyoruz ki mevcut RSI21
        # kolundan bagimsiz calissin.
        global _last_m15_scan_time
        if (_last_m15_scan_time is None or
                (datetime.now() - _last_m15_scan_time).total_seconds() >= M15_SCAN_INTERVAL_MINUTES * 60):
            if bist_is_open(istanbul_now):
                try:
                    scan_m15_engines("BIST", BIST_TICKERS)
                except Exception as e:
                    dedektif_report("BIST M15 motor taraması (döngü)", e)
            if us_is_open(ny_now):
                try:
                    scan_m15_engines("ABD", US_INTRADAY_TICKERS)
                except Exception as e:
                    dedektif_report("ABD M15 motor taraması (döngü)", e)
            if bist_is_open(istanbul_now) or us_is_open(ny_now):
                _last_m15_scan_time = datetime.now()

        # Gunluk yasam sinyali: bot ayakta ama sinyal uretmiyorsa bunu
        # sessizlikten ayirt edebilmek icin gunde bir kez ozet gonderiyoruz.
        if should_run_daily_scan(heartbeat_key, istanbul_now, HEARTBEAT_HOUR, 0):
            try:
                bist_reg, bist_note = get_market_regime("BIST")
                us_reg, us_note = get_market_regime("ABD")
                open_count = len(_read_tracking())
                send_telegram_message(
                    f"💓 [GÜNLÜK DURUM] Bot çalışıyor.\n"
                    f"BIST rejimi: {bist_reg} ({bist_note})\n"
                    f"ABD rejimi: {us_reg} ({us_note})\n"
                    f"Takipteki açık sinyal: {open_count}\n"
                    f"Bugün sinyal gelmediyse sebebi ya rejim filtresi ya da kriterlere uyan hisse olmamasıdır."
                )
                mark_daily_scan_done(heartbeat_key, istanbul_now)
            except Exception as e:
                dedektif_report("günlük durum mesajı", e)

        # 3. KATMAN: acik sinyaller icin cikis uyarilarini kontrol et.
        # Piyasa saatleri kontrolu (Gemini'nin istegi): iki piyasa da kapaliyken
        # fiyat degismeyecegi icin yfinance'e istek atmak gereksiz - hem hiz
        # limitini yer hem de bos veri yuzunden gereksiz hata uretir.
        # NOT: bu kontrol SADECE cikis takibi icin - planli taramalarin kendi
        # zaman pencereleri var ve ABD swing taramasi BILEREK piyasa
        # kapandiktan sonra calisir, o yuzden onlari kapilamiyoruz.
        if any_market_open():
            if (_last_exit_check_time is None or
                    (datetime.now() - _last_exit_check_time).total_seconds() >= EXIT_CHECK_INTERVAL_MINUTES * 60):
                try:
                    check_exit_alerts()
                except Exception as e:
                    dedektif_report("çıkış uyarı döngüsü", e)
                _last_exit_check_time = datetime.now()

        # Mac analiz botu (SPO-QUANT) - hisse taramalarindan tamamen
        # bagimsiz, kendi zaman araliklari ve kendi try/except'i. Burada bir
        # hata olursa hisse tarama dongusu ETKILENMEZ. Ayristirilmis frekans:
        # model taramasi sik, oran taramasi seyrek (kota koruma).
        if (_last_football_model_scan_time is None or
                (datetime.now(timezone.utc) - _last_football_model_scan_time).total_seconds()
                >= fb.FOOTBALL_MODEL_SCAN_INTERVAL_MINUTES * 60):
            try:
                model_result = fb.run_model_scan()
                if model_result["errors"]:
                    dedektif_report(
                        "Futbol model taraması (döngü)",
                        Exception("; ".join(model_result["errors"])),
                    )
            except Exception as e:
                dedektif_report("Futbol model taraması (döngü)", e)
            _last_football_model_scan_time = datetime.now(timezone.utc)

        if (_last_football_odds_scan_time is None or
                (datetime.now(timezone.utc) - _last_football_odds_scan_time).total_seconds()
                >= fb.FOOTBALL_ODDS_SCAN_INTERVAL_MINUTES * 60):
            try:
                odds_result = fb.run_odds_scan()
                if odds_result["errors"]:
                    dedektif_report(
                        "Futbol oran taraması (döngü)",
                        Exception("; ".join(odds_result["errors"])),
                    )
            except Exception as e:
                dedektif_report("Futbol oran taraması (döngü)", e)
            _last_football_odds_scan_time = datetime.now(timezone.utc)

        if (_last_football_results_update_time is None or
                (datetime.now(timezone.utc) - _last_football_results_update_time).total_seconds()
                >= FOOTBALL_RESULTS_UPDATE_INTERVAL_MINUTES * 60):
            try:
                fb.run_results_update()
            except Exception as e:
                dedektif_report("Futbol sonuç güncelleme (döngü)", e)
            _last_football_results_update_time = datetime.now(timezone.utc)

        # Telegram komutlarını (/stats, /rapor, /status) her turda kontrol
        # eder — ucuz bir istek, LOOP_INTERVAL_SECONDS hızında yeterli.
        try:
            fb.poll_and_respond()
        except Exception as e:
            dedektif_report("Futbol komut dinleyici (döngü)", e)

        time.sleep(LOOP_INTERVAL_SECONDS)


if __name__ == "__main__":
    # En ust seviye guvenlik agi: run_forever beklenmedik bir sekilde cokerse
    # sessizce olmek yerine haber verip yeniden basliyor. Onceden ciplak
    # cagriliyordu, yani tek bir istisna botu tamamen susturabiliyordu.
    while True:
        try:
            run_forever()
        except KeyboardInterrupt:
            print("Manuel olarak durduruldu.")
            break
        except Exception as e:
            print(f"KRITIK: run_forever coktu ({e}) - 60 saniye sonra yeniden baslatiliyor")
            try:
                send_telegram_message(
                    f"🚨 [KRİTİK] Bot beklenmedik şekilde çöktü:\n{e}\n"
                    f"60 saniye içinde yeniden başlatılıyor."
                )
            except Exception:
                pass
            time.sleep(60)
