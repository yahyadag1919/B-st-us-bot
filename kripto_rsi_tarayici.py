"""
kripto_rsi_tarayici.py — KRİPTO 4 SAATLİK RSI AŞIRI SATIM TARAYICISI
==============================================================================
2026-10-23 — Kullanıcının isteğiyle kuruldu. BIST tavan tarayıcısı
kapatıldı, bu modül AYNI Telegram bot/sohbetini (ARGE_TELEGRAM_TOKEN /
ARGE_TELEGRAM_CHAT_ID) kullanıyor.

NE YAPIYOR (v2, 2026-10-23 akşam - kullanıcının gözlemine göre genişletildi):
  - İşlem hacmine göre en büyük ~400 USDT çiftini tarar (v1: 150)
  - Her birinin 4 saatlik VE günlük grafiğinde RSI(14) hesaplar (v1: sadece 4s)
  - RSI ≤ 20 → aşırı satım, RSI ≥ 75 → aşırı alım bildirimi (v1: sadece ≤ 20)
  - Kullanıcı bildirime bakıp KENDİSİ, MANUEL olarak (demo hesapta) işlem açar

NE YAPMIYOR:
  - Hiçbir borsaya emir GÖNDERMEZ, hesaba BAĞLANMAZ
  - API anahtarı GEREKTİRMEZ - sadece herkese açık piyasa verisini okur

VERİ KAYNAĞI: Render sunucusunun hangi ülkede olduğuna göre bazı borsalar
erişimi engelleyebiliyor (Binance ABD IP'lerini engelliyor). Bu yüzden
sırayla 3 kaynak deneniyor, ilk çalışan kullanılıyor:
  1) Binance herkese açık veri adresi (data-api.binance.vision)
  2) OKX
  3) KuCoin
Hangisinin çalıştığı başlangıç mesajında yazıyor.

BİLDİRİM SELİ KORUMASI: Bir coin RSI ≤ 20 olup alarm verdikten sonra, RSI
tekrar 25'in üzerine çıkana kadar o coin için YENİ alarm gönderilmez -
aynı düşüş için her 5 dakikada bir mesaj gelmesin diye.

GÜNLÜK ÖZET: Her akşam bir özet mesajı gelir (kaç coin tarandı, bugün kaç
alarm çıktı, şu an RSI'ı en düşük coinler). Böylece sessiz bir gün
"bot bozuldu" ile "hiçbir coin 20'ye düşmedi" ayırt edilebiliyor.
"""
import os
import json
import time
from datetime import datetime, timezone, timedelta

import requests

TELEGRAM_TOKEN = os.environ.get("ARGE_TELEGRAM_TOKEN", "")
TELEGRAM_CHAT_ID = os.environ.get("ARGE_TELEGRAM_CHAT_ID", "")
DATA_DIR = os.environ.get("DATA_DIR", ".")

KRIPTO_SURUM = "kripto-rsi-v4-vadeli-filtre-2026-10-23"

RSI_PERIYOT = 14
RSI_ESIK = float(os.environ.get("KRIPTO_RSI_ESIK", "20"))
RSI_YENIDEN_KURMA = float(os.environ.get("KRIPTO_RSI_YENIDEN_KURMA", "25"))
# (v2) Aşırı alım tarafı: RSI 75 ve üstü → alarm; 70'in altına inince yeniden kurulur
RSI_UST_ESIK = float(os.environ.get("KRIPTO_RSI_UST_ESIK", "75"))
RSI_UST_YENIDEN_KURMA = float(os.environ.get("KRIPTO_RSI_UST_YENIDEN_KURMA", "70"))
# (v2) 150 → 400: Binance'teki USDT çiftlerinin pratikte tamamı
COIN_SAYISI = int(os.environ.get("KRIPTO_COIN_SAYISI", "400"))
# (v2) İki zaman dilimi: 4 saatlik + günlük
ZAMAN_DILIMLERI = ["4s", "1g"]
TARAMA_ARALIGI_DK = int(os.environ.get("KRIPTO_TARAMA_DK", "5"))
EVREN_YENILEME_SAAT = 6
MUM_SAYISI = 120               # RSI'ın oturması için yeterli geçmiş
ISTEK_ARASI_SN = 0.15
OZET_SAATI_TR = 21              # Türkiye saatiyle akşam özeti
TR_SAAT_FARKI = timedelta(hours=3)

# Stabil coinler ve kaldıraçlı tokenler taranmaz (RSI'ları anlamsız)
STABIL_COINLER = {"USDT", "USDC", "FDUSD", "TUSD", "DAI", "USDP", "BUSD", "USDE",
                  "PYUSD", "USD1", "EUR", "EURI", "AEUR", "TRY", "BRL", "GBP",
                  "USTC", "UST", "USDD", "XUSD", "RLUSD", "PAXG", "XAUT"}
KALDIRACLI_EKLER = ("UP", "DOWN", "BULL", "BEAR", "3L", "3S", "5L", "5S")

_OTURUM = requests.Session()
_OTURUM.headers.update({"User-Agent": "Mozilla/5.0 (kripto-rsi-tarayici)"})


# =============================================================================
# TELEGRAM
# =============================================================================
def send_telegram_message(text: str):
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        print(f"[Kripto devre dışı] {text}", flush=True)
        return
    try:
        _OTURUM.post(f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage",
                     json={"chat_id": TELEGRAM_CHAT_ID, "text": text[:4000],
                           "disable_web_page_preview": True},
                     timeout=20)
    except Exception as e:
        print(f"[Kripto Telegram hata] {e}", flush=True)


# =============================================================================
# VERİ KAYNAKLARI — hepsi aynı biçimde veri döndürür:
#   evren()  -> [{"sembol": "SOL", "kaynak_kodu": ..., "hacim": usdt,
#                 "fiyat": float, "degisim": % (24s)}]
#   kapanislar(kaynak_kodu) -> (kapanis_listesi_eskiden_yeniye, son_mum_acilis_ms)
#                              listenin SON elemanı henüz kapanmamış (canlı) mum
# =============================================================================
def _get(url, params=None):
    r = _OTURUM.get(url, params=params, timeout=20)
    r.raise_for_status()
    return r.json()


def _uygun_sembol_mu(baz: str) -> bool:
    if not baz or baz in STABIL_COINLER:
        return False
    if baz.endswith(KALDIRACLI_EKLER) and len(baz) > 4:
        return False
    return True


AKTIF_LISTE_YENILEME_SN = 6 * 3600

# =============================================================================
# (v4) BINANCE VADELİ COIN LİSTESİ — CoinGecko üzerinden
# Render sunucusu ABD'de ve fapi.binance.com ABD'yi engelliyor (451). Vadeli
# fiyat verisini alamıyoruz, ama hangi coinlerin vadelide olduğunu CoinGecko'nun
# Binance Futures listesinden öğrenip SPOT taramasını bu coinlerle sınırlıyoruz.
# Böylece gelen her alarm, kullanıcının Binance vadelide işlem açabileceği bir
# coin oluyor. Spot ve vadeli fiyatları neredeyse aynı olduğu için RSI da
# pratikte aynı çıkıyor.
# =============================================================================
_VADELI_ON_EKLER = ("1000000", "1000", "1M")
_vadeli_onbellek = {"harita": {}, "zaman": 0, "hata": None}


def _vadeli_normallestir(baz: str) -> str:
    """Vadelide bazı coinler 1000'li listeleniyor (1000PEPE ↔ spot PEPE)."""
    for on in _VADELI_ON_EKLER:
        if baz.startswith(on) and len(baz) > len(on):
            return baz[len(on):]
    return baz


def binance_vadeli_haritasi():
    """{spot_adi: vadeli_adi} döner, ör. {"PEPE": "1000PEPE", "BTC": "BTC"}.
    6 saatte bir yenilenir. Alınamazsa boş sözlük (filtre devre dışı kalır)."""
    o = _vadeli_onbellek
    if o["harita"] and time.time() - o["zaman"] < AKTIF_LISTE_YENILEME_SN:
        return o["harita"]
    try:
        veri = _get("https://api.coingecko.com/api/v3/derivatives/exchanges/binance_futures",
                    {"include_tickers": "unexpired"})
        harita = {}
        for t in veri.get("tickers", []):
            sembol = str(t.get("symbol", "")).upper()
            if not sembol.endswith("USDT") or "_" in sembol:
                continue
            if str(t.get("contract_type", "perpetual")).lower() != "perpetual":
                continue
            vadeli_adi = sembol[:-4]
            harita[_vadeli_normallestir(vadeli_adi)] = vadeli_adi
        if len(harita) < 50:
            raise RuntimeError(f"liste çok kısa ({len(harita)})")
        o.update(harita=harita, zaman=time.time(), hata=None)
    except Exception as e:
        o["hata"] = str(e)[:150]
        print(f"[Kripto] Vadeli listesi alınamadı: {o['hata']}", flush=True)
    return o["harita"]


class BinanceVadeliKaynak:
    """(v3) Binance Futures USDT-M PERPETUAL - kullanıcı Binance vadelide
    işlem yapıyor. Sadece status=TRADING sözleşmeler. Coin adları vadelideki
    gibi gösterilir (ör. 1000PEPE), böylece Binance uygulamasında aynen aranabilir.
    Not: fapi.binance.com ABD IP'lerinden erişimi engelleyebiliyor - erişilemezse
    _calisan_kaynagi_bul() otomatik olarak spot kaynağa geçer."""
    ad = "Binance Vadeli (USDT-M)"
    taban = "https://fapi.binance.com"

    def __init__(self):
        self._aktif, self._aktif_zaman = set(), 0

    def _aktif_ciftler(self):
        if not self._aktif or time.time() - self._aktif_zaman > AKTIF_LISTE_YENILEME_SN:
            veri = _get(f"{self.taban}/fapi/v1/exchangeInfo")
            self._aktif = {x["symbol"] for x in veri.get("symbols", [])
                           if x.get("status") == "TRADING"
                           and x.get("contractType") == "PERPETUAL"
                           and x.get("quoteAsset") == "USDT"}
            self._aktif_zaman = time.time()
        return self._aktif

    def evren(self):
        aktif = self._aktif_ciftler()
        veri = _get(f"{self.taban}/fapi/v1/ticker/24hr")
        sonuc = []
        for t in veri:
            s = t.get("symbol", "")
            if s not in aktif or not s.endswith("USDT"):
                continue
            baz = s[:-4]
            if not _uygun_sembol_mu(baz):
                continue
            try:
                sonuc.append({"sembol": baz, "kaynak_kodu": s,
                              "hacim": float(t["quoteVolume"]),
                              "fiyat": float(t["lastPrice"]),
                              "degisim": float(t["priceChangePercent"])})
            except (KeyError, ValueError):
                continue
        return sonuc

    def kapanislar(self, kod, zd="4s"):
        veri = _get(f"{self.taban}/fapi/v1/klines",
                    {"symbol": kod, "interval": {"4s": "4h", "1g": "1d"}[zd], "limit": MUM_SAYISI})
        return [float(m[4]) for m in veri], int(veri[-1][0])

    def grafik_linki(self, sembol):
        return f"https://www.tradingview.com/chart/?symbol=BINANCE:{sembol}USDT.P"


class BinanceKaynak:
    """Binance SPOT. (v3) Artık sadece status=TRADING olan çiftler - v2'de
    24 saatlik fiyat listesi borsadan çıkarılmış (delist) eski çiftleri de
    döndürüyordu, coin sayısı 400'e çıkınca bunlar listeye girdi ve
    donmuş fiyatları yüzünden anlamsız RSI alarmları üretti."""
    ad = "Binance Spot"
    taban = "https://data-api.binance.vision"

    def __init__(self):
        self._aktif, self._aktif_zaman = set(), 0

    def _aktif_ciftler(self):
        if not self._aktif or time.time() - self._aktif_zaman > AKTIF_LISTE_YENILEME_SN:
            veri = _get(f"{self.taban}/api/v3/exchangeInfo")
            self._aktif = {x["symbol"] for x in veri.get("symbols", [])
                           if x.get("status") == "TRADING" and x.get("quoteAsset") == "USDT"}
            self._aktif_zaman = time.time()
        return self._aktif

    def evren(self):
        aktif = self._aktif_ciftler()
        vadeli = binance_vadeli_haritasi()  # (v4) boşsa filtre uygulanmaz
        veri = _get(f"{self.taban}/api/v3/ticker/24hr")
        sonuc = []
        for t in veri:
            s = t.get("symbol", "")
            if s not in aktif or not s.endswith("USDT"):
                continue
            baz = s[:-4]
            if not _uygun_sembol_mu(baz):
                continue
            if vadeli and baz not in vadeli:
                continue  # Binance vadelide yok → atla
            try:
                coin = {"sembol": baz, "kaynak_kodu": s,
                        "hacim": float(t["quoteVolume"]),
                        "fiyat": float(t["lastPrice"]),
                        "degisim": float(t["priceChangePercent"])}
            except (KeyError, ValueError):
                continue
            if vadeli:
                coin["vadeli_sembol"] = vadeli[baz]
            sonuc.append(coin)
        return sonuc

    def kapanislar(self, kod, zd="4s"):
        veri = _get(f"{self.taban}/api/v3/klines",
                    {"symbol": kod, "interval": {"4s": "4h", "1g": "1d"}[zd], "limit": MUM_SAYISI})
        return [float(m[4]) for m in veri], int(veri[-1][0])

    def grafik_linki(self, sembol):
        return f"https://www.tradingview.com/chart/?symbol=BINANCE:{sembol}USDT"


class OkxKaynak:
    ad = "OKX"
    taban = "https://www.okx.com"

    def evren(self):
        veri = _get(f"{self.taban}/api/v5/market/tickers", {"instType": "SPOT"})
        sonuc = []
        for t in veri.get("data", []):
            kod = t.get("instId", "")
            if not kod.endswith("-USDT"):
                continue
            baz = kod[:-5]
            if not _uygun_sembol_mu(baz):
                continue
            try:
                fiyat = float(t["last"])
                acilis = float(t["open24h"])
                sonuc.append({"sembol": baz, "kaynak_kodu": kod,
                              "hacim": float(t["volCcy24h"]),
                              "fiyat": fiyat,
                              "degisim": (fiyat - acilis) / acilis * 100 if acilis else 0.0})
            except (KeyError, ValueError):
                continue
        return sonuc

    def kapanislar(self, kod, zd="4s"):
        veri = _get(f"{self.taban}/api/v5/market/candles",
                    {"instId": kod, "bar": {"4s": "4H", "1g": "1Dutc"}[zd], "limit": min(MUM_SAYISI, 300)})
        mumlar = list(reversed(veri.get("data", [])))  # OKX yeniden eskiye veriyor
        return [float(m[4]) for m in mumlar], int(mumlar[-1][0])

    def grafik_linki(self, sembol):
        return f"https://www.tradingview.com/chart/?symbol=OKX:{sembol}USDT"


class KucoinKaynak:
    ad = "KuCoin"
    taban = "https://api.kucoin.com"

    def evren(self):
        veri = _get(f"{self.taban}/api/v1/market/allTickers")
        sonuc = []
        for t in veri.get("data", {}).get("ticker", []):
            kod = t.get("symbol", "")
            if not kod.endswith("-USDT"):
                continue
            baz = kod[:-5]
            if not _uygun_sembol_mu(baz):
                continue
            try:
                sonuc.append({"sembol": baz, "kaynak_kodu": kod,
                              "hacim": float(t["volValue"]),
                              "fiyat": float(t["last"]),
                              "degisim": float(t["changeRate"]) * 100})
            except (KeyError, ValueError, TypeError):
                continue
        return sonuc

    def kapanislar(self, kod, zd="4s"):
        simdi = int(time.time())
        saat = {"4s": 4, "1g": 24}[zd]
        veri = _get(f"{self.taban}/api/v1/market/candles",
                    {"symbol": kod, "type": {"4s": "4hour", "1g": "1day"}[zd],
                     "startAt": simdi - MUM_SAYISI * saat * 3600, "endAt": simdi})
        mumlar = list(reversed(veri.get("data", [])))  # KuCoin yeniden eskiye veriyor
        # KuCoin sırası: [zaman, açılış, KAPANIŞ, yüksek, düşük, ...]
        return [float(m[2]) for m in mumlar], int(mumlar[-1][0]) * 1000

    def grafik_linki(self, sembol):
        return f"https://www.tradingview.com/chart/?symbol=KUCOIN:{sembol}USDT"


# (v3) Önce Binance vadeli (kullanıcının piyasası), erişilemezse spot, sonra diğerleri
KAYNAKLAR = [BinanceVadeliKaynak(), BinanceKaynak(), OkxKaynak(), KucoinKaynak()]


def _calisan_kaynagi_bul():
    """Kaynakları sırayla dener; hem coin listesi hem 4s mum verisi gelen
    ilk kaynağı döndürür. Hangisinin neden başarısız olduğunu da raporlar."""
    notlar = []
    for k in KAYNAKLAR:
        try:
            evren = k.evren()
            if len(evren) < 20:
                notlar.append(f"{k.ad}: coin listesi çok kısa ({len(evren)})")
                continue
            btc = next((c for c in evren if c["sembol"] == "BTC"), evren[0])
            kapanis, _ = k.kapanislar(btc["kaynak_kodu"])
            if len(kapanis) < RSI_PERIYOT + 5:
                notlar.append(f"{k.ad}: mum verisi yetersiz ({len(kapanis)})")
                continue
            return k, notlar
        except Exception as e:
            notlar.append(f"{k.ad}: {str(e)[:120]}")
    return None, notlar


# =============================================================================
# RSI (Wilder) — TradingView'daki standart RSI ile aynı yöntem
# =============================================================================
def rsi_hesapla(kapanislar, periyot=RSI_PERIYOT):
    if len(kapanislar) < periyot + 1:
        return None
    kazanc, kayip = 0.0, 0.0
    for i in range(1, periyot + 1):
        fark = kapanislar[i] - kapanislar[i - 1]
        kazanc += max(fark, 0)
        kayip += max(-fark, 0)
    ort_kazanc, ort_kayip = kazanc / periyot, kayip / periyot
    for i in range(periyot + 1, len(kapanislar)):
        fark = kapanislar[i] - kapanislar[i - 1]
        ort_kazanc = (ort_kazanc * (periyot - 1) + max(fark, 0)) / periyot
        ort_kayip = (ort_kayip * (periyot - 1) + max(-fark, 0)) / periyot
    if ort_kayip == 0:
        return 100.0 if ort_kazanc > 0 else 50.0
    rs = ort_kazanc / ort_kayip
    return 100 - 100 / (1 + rs)


# =============================================================================
# DURUM — alarm verilen coinler + günlük sayaçlar (diske de yazılır,
# ama Render yeniden başlayınca silinebilir; bu durumda en kötü ihtimalle
# halen RSI ≤ 20 olan bir coin için bir kez daha alarm gelir)
# =============================================================================
_durum = {"alarmda": {}, "gunluk_alarm": {}, "gun": "", "son_ozet_gunu": "",
          "tur": 0, "son_tur_coin": 0, "son_rsi": {}, "kaynak": "-"}


def _durum_dosyasi():
    return os.path.join(DATA_DIR, "kripto_rsi_durum.json")


def _durum_yukle():
    try:
        with open(_durum_dosyasi(), encoding="utf-8") as f:
            kayit = json.load(f)
        for anahtar in ("alarmda", "gunluk_alarm", "gun", "son_ozet_gunu"):
            if anahtar in kayit:
                _durum[anahtar] = kayit[anahtar]
        # v1'de anahtar sadece "SOL" idi, v2'de "SOL|4s|dusuk" - eskileri at
        _durum["alarmda"] = {k: v for k, v in _durum["alarmda"].items() if k.count("|") == 2}
        if not isinstance(_durum["gunluk_alarm"], dict):
            _durum["gunluk_alarm"] = {}
    except Exception:
        pass


def _durum_kaydet():
    try:
        os.makedirs(DATA_DIR, exist_ok=True)
        with open(_durum_dosyasi(), "w", encoding="utf-8") as f:
            json.dump({k: _durum[k] for k in ("alarmda", "gunluk_alarm", "gun", "son_ozet_gunu")},
                      f, ensure_ascii=False)
    except Exception as e:
        print(f"[Kripto] Durum kaydedilemedi: {e}", flush=True)


def _tr_simdi():
    return datetime.now(timezone.utc) + TR_SAAT_FARKI


def _fiyat_bicimle(f):
    if f >= 100:
        return f"{f:,.2f}"
    if f >= 1:
        return f"{f:.4f}"
    if f >= 0.01:
        return f"{f:.5f}"
    return f"{f:.8f}".rstrip("0")


# =============================================================================
# TARAMA
# =============================================================================
ZD_AD = {"4s": "4 saatlik", "1g": "Günlük"}


def _alarm_mesaji(kaynak, coin, zd, yon, rsi_canli, rsi_kapanmis):
    kapanmis_str = f"{rsi_kapanmis:.1f}" if rsi_kapanmis is not None else "?"
    # (v4) Vadelideki adıyla göster (ör. 1000PEPE) ve vadeli grafiği aç
    ad = coin.get("vadeli_sembol", coin["sembol"])
    fiyat = coin["fiyat"]
    if "vadeli_sembol" in coin:
        grafik = f"https://www.tradingview.com/chart/?symbol=BINANCE:{ad}USDT.P"
        # 1000PEPE vadelide spot PEPE'nin 1000 katı fiyattan işlem görüyor
        carpan = {"1000000": 1_000_000, "1000": 1000, "1M": 1_000_000}
        for on in _VADELI_ON_EKLER:
            if ad.startswith(on) and ad != coin["sembol"]:
                fiyat *= carpan[on]
                break
    else:
        grafik = kaynak.grafik_linki(coin["sembol"])
    if yon == "dusuk":
        baslik = f"🔻 AŞIRI SATIM — {ad}/USDT ({ZD_AD[zd]})"
        alt = (f"RSI tekrar {RSI_YENIDEN_KURMA:.0f}'in üzerine çıkana kadar bu coin/zaman "
               f"dilimi için yeni alarm gelmeyecek. Al sinyali değil — düşüş devam edebilir.")
    else:
        baslik = f"🔺 AŞIRI ALIM — {ad}/USDT ({ZD_AD[zd]})"
        alt = (f"RSI tekrar {RSI_UST_YENIDEN_KURMA:.0f}'in altına inene kadar bu coin/zaman "
               f"dilimi için yeni alarm gelmeyecek. Sat sinyali değil — yükseliş devam edebilir.")
    return (
        f"{baslik}\n"
        f"RSI: {rsi_canli:.1f} (canlı mum, henüz kapanmadı)\n"
        f"Son kapanmış mumun RSI'ı: {kapanmis_str}\n"
        f"Fiyat: ${_fiyat_bicimle(fiyat)}  |  24s: %{coin['degisim']:+.1f}\n"
        f"Kaynak: {kaynak.ad}\n"
        f"Grafik: {grafik}\n\n"
        f"ℹ️ {alt}")


def _gun_kontrol():
    bugun = _tr_simdi().strftime("%Y-%m-%d")
    if _durum["gun"] != bugun:
        _durum["gun"] = bugun
        _durum["gunluk_alarm"] = {}


def _sayac_artir(zd, yon):
    anahtar = f"{zd}|{yon}"
    _durum["gunluk_alarm"][anahtar] = _durum["gunluk_alarm"].get(anahtar, 0) + 1


def _durumu_isle(kaynak, coin, zd, rsi_canli, rsi_kapanmis):
    """Bir coin + zaman dilimi için alarm kur/yeniden kur mantığı.
    Aşırı satım ve aşırı alım birbirinden bağımsız takip ediliyor."""
    sembol = coin["sembol"]
    k_dusuk, k_yuksek = f"{sembol}|{zd}|dusuk", f"{sembol}|{zd}|yuksek"

    if k_dusuk in _durum["alarmda"]:
        if rsi_canli > RSI_YENIDEN_KURMA:
            del _durum["alarmda"][k_dusuk]
    elif rsi_canli <= RSI_ESIK:
        send_telegram_message(_alarm_mesaji(kaynak, coin, zd, "dusuk", rsi_canli, rsi_kapanmis))
        _durum["alarmda"][k_dusuk] = round(rsi_canli, 1)
        _sayac_artir(zd, "dusuk")
        _durum_kaydet()

    if k_yuksek in _durum["alarmda"]:
        if rsi_canli < RSI_UST_YENIDEN_KURMA:
            del _durum["alarmda"][k_yuksek]
    elif rsi_canli >= RSI_UST_ESIK:
        send_telegram_message(_alarm_mesaji(kaynak, coin, zd, "yuksek", rsi_canli, rsi_kapanmis))
        _durum["alarmda"][k_yuksek] = round(rsi_canli, 1)
        _sayac_artir(zd, "yuksek")
        _durum_kaydet()


def tek_tur(kaynak, evren):
    _gun_kontrol()
    taranan = 0
    yeni_rsi = {zd: {} for zd in ZAMAN_DILIMLERI}
    for coin in evren:
        basarili = False
        for zd in ZAMAN_DILIMLERI:
            try:
                kapanis, _ = kaynak.kapanislar(coin["kaynak_kodu"], zd)
            except Exception as e:
                print(f"[Kripto] {coin['sembol']} {zd} veri hatası: {str(e)[:100]}", flush=True)
                time.sleep(ISTEK_ARASI_SN)
                continue
            time.sleep(ISTEK_ARASI_SN)

            rsi_canli = rsi_hesapla(kapanis)
            if rsi_canli is None:
                continue  # yeni listelenmiş coin, yeterli mum yok
            rsi_kapanmis = rsi_hesapla(kapanis[:-1])
            yeni_rsi[zd][coin["sembol"]] = rsi_canli
            basarili = True
            _durumu_isle(kaynak, coin, zd, rsi_canli, rsi_kapanmis)
        if basarili:
            taranan += 1

    _durum["son_rsi"] = yeni_rsi
    _durum["son_tur_coin"] = taranan
    _durum["tur"] += 1
    return taranan


def _gunluk_ozet_gerekiyorsa_gonder():
    simdi = _tr_simdi()
    bugun = simdi.strftime("%Y-%m-%d")
    if simdi.hour < OZET_SAATI_TR or _durum["son_ozet_gunu"] == bugun:
        return
    sayac = _durum["gunluk_alarm"]
    satirlar = [
        f"📊 Kripto RSI günlük özet ({bugun})",
        f"Taranan coin: {_durum['son_tur_coin']} (kaynak: {_durum['kaynak']})",
        f"Bugünkü tarama turu: ~{_durum['tur']}",
        "",
        "Bugün çıkan alarmlar:",
    ]
    for zd in ZAMAN_DILIMLERI:
        satirlar.append(f"  {ZD_AD[zd]}: 🔻 aşırı satım {sayac.get(zd + '|dusuk', 0)}  |  "
                        f"🔺 aşırı alım {sayac.get(zd + '|yuksek', 0)}")
    for zd in ZAMAN_DILIMLERI:
        rsi = _durum["son_rsi"].get(zd, {}) if isinstance(_durum["son_rsi"], dict) else {}
        if not rsi:
            continue
        sirali = sorted(rsi.items(), key=lambda x: x[1])
        satirlar.append("")
        satirlar.append(f"{ZD_AD[zd]} — RSI en düşük 3: "
                        + ", ".join(f"{s} {r:.0f}" for s, r in sirali[:3]))
        satirlar.append(f"{ZD_AD[zd]} — RSI en yüksek 3: "
                        + ", ".join(f"{s} {r:.0f}" for s, r in sirali[-3:][::-1]))
    if sum(sayac.values()) == 0:
        satirlar.append("")
        satirlar.append("Bugün hiç alarm çıkmadı — sistem çalışıyor, sadece hiçbir "
                        "coin eşiklere ulaşmadı.")
    send_telegram_message("\n".join(satirlar))
    _durum["son_ozet_gunu"] = bugun
    _durum["tur"] = 0
    _durum_kaydet()


def kripto_tarama_dongusu():
    _durum_yukle()
    kaynak, notlar = _calisan_kaynagi_bul()
    if kaynak is None:
        send_telegram_message(
            "❌ Kripto RSI tarayıcı: hiçbir veri kaynağına erişilemedi.\n"
            + "\n".join(notlar) + "\n\n10 dakika sonra tekrar denenecek.")
        time.sleep(600)
        return  # _guvenli sarmalayıcısı fonksiyonu yeniden başlatır

    _durum["kaynak"] = kaynak.ad
    evren, evren_zamani = [], 0
    ardisik_hata = 0
    while True:
        try:
            if not evren or time.time() - evren_zamani > EVREN_YENILEME_SAAT * 3600:
                tum = kaynak.evren()
                tum.sort(key=lambda c: c["hacim"], reverse=True)
                evren = tum[:COIN_SAYISI]
                evren_zamani = time.time()
                print(f"[Kripto] Evren yenilendi: {len(evren)} coin ({kaynak.ad})", flush=True)
            else:
                # Fiyat/24s değişim bilgisini tazelemek için listeyi güncelle
                guncel = {c["sembol"]: c for c in kaynak.evren()}
                evren = [guncel.get(c["sembol"], c) for c in evren]

            taranan = tek_tur(kaynak, evren)
            print(f"[Kripto] Tur tamamlandı: {taranan}/{len(evren)} coin, "
                  f"alarmda: {len(_durum['alarmda'])}", flush=True)
            ardisik_hata = 0 if taranan > 0 else ardisik_hata + 1
            _gunluk_ozet_gerekiyorsa_gonder()
        except Exception as e:
            ardisik_hata += 1
            print(f"[Kripto] Tur hatası ({ardisik_hata}): {e}", flush=True)

        if ardisik_hata >= 3:
            # Kaynak çalışmaz hale geldiyse diğer kaynaklara geçmeyi dene
            yeni, notlar = _calisan_kaynagi_bul()
            if yeni is not None and yeni.ad != kaynak.ad:
                send_telegram_message(f"⚠️ Kripto RSI: {kaynak.ad} yanıt vermiyor, "
                                      f"{yeni.ad} kaynağına geçildi.")
                kaynak, evren = yeni, []
                _durum["kaynak"] = kaynak.ad
            ardisik_hata = 0

        time.sleep(TARAMA_ARALIGI_DK * 60)


def baslangic():
    kaynak, notlar = _calisan_kaynagi_bul()
    kaynak_str = kaynak.ad if kaynak else "❌ HİÇBİRİ ÇALIŞMADI"
    satirlar = [
        f"🪙 Kripto RSI Tarayıcı AKTİF (v4) — {KRIPTO_SURUM}",
        "",
        "v3: artık Binance VADELİ (USDT-M perpetual) taranıyor, borsadan "
        "çıkarılmış eski coinler elendi.",
        f"• En yüksek hacimli {COIN_SAYISI} USDT çifti (sadece şu an işlem görenler)",
        f"• 4 saatlik VE günlük grafikte RSI({RSI_PERIYOT})",
        f"• 🔻 RSI ≤ {RSI_ESIK:.0f} → aşırı satım alarmı",
        f"• 🔺 RSI ≥ {RSI_UST_ESIK:.0f} → aşırı alım alarmı",
        f"• Her {TARAMA_ARALIGI_DK} dakikada bir tarama",
        f"• Her akşam {OZET_SAATI_TR}:00'de günlük özet",
        f"• Veri kaynağı: {kaynak_str}",
        "",
        "Borsaya bağlanmaz, emir göndermez — işlemleri sen manuel açarsın.",
    ]
    if kaynak is not None and kaynak.ad == BinanceKaynak.ad:
        vadeli = binance_vadeli_haritasi()
        satirlar.append("")
        if vadeli:
            satirlar.append(f"✅ Sunucu ABD'de olduğu için fiyatlar Binance spottan alınıyor, "
                            f"ama sadece Binance VADELİDE de olan coinler taranıyor "
                            f"({len(vadeli)} vadeli coin listesi). Spot ve vadeli fiyatları "
                            f"neredeyse aynı olduğu için RSI da aynı çıkar.")
        else:
            satirlar.append(f"⚠️ Vadeli coin listesi alınamadı ({_vadeli_onbellek['hata']}) - "
                            f"şimdilik tüm aktif spot coinler taranıyor, 6 saat sonra tekrar denenecek.")
    elif kaynak is not None and kaynak.ad != BinanceVadeliKaynak.ad:
        satirlar.append("")
        satirlar.append("⚠️ Binance verisine erişilemedi, başka borsa kullanılıyor - "
                        "bazı coinler Binance vadelide olmayabilir.")
    if notlar:
        satirlar.append("")
        satirlar.append("Denenip çalışmayan kaynaklar:")
        satirlar.extend(f"  {n}" for n in notlar)
    send_telegram_message("\n".join(satirlar))
