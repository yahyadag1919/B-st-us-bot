"""
abd_endeks_brifing.py — ABD AÇILIŞ ÖNCESİ BRİFİNG + ENDEKS YÖN TESTİ
==============================================================================
2026-10-26 — Kullanıcının "endeksin yönünü nasıl öğrenebilirim" sorusu üzerine.

DÜRÜST ÇERÇEVE: Endeksin yönünü kesin bilen bir sistem yok. Bu modül yönü
TAHMİN ETMİYOR; açılıştan önce yönü etkileyen her şeyi tek mesajda topluyor
ve her birinin geçmişte ne anlama geldiğini GERÇEK VERİYLE gösteriyor.

1) OTOMATİK BRİFİNG — her iş günü New York saatiyle 09:00'da (açılıştan 30 dk
   önce; Türkiye saatiyle yaz döneminde 16:00, kış döneminde 17:00 - saat
   değişimi New York saat dilimi kullanıldığı için kendiliğinden uyar):
     - Vadeli endeksler (S&P 500, Nasdaq, Dow, Russell) dünkü kapanışa göre
     - Geçmişte benzer açılışlar: SPY'nin son 10 yılında aynı büyüklükte
       açılış boşluğu (gap) olan günlerde seans ve gün nasıl kapandı
     - Risk göstergeleri (VIX, 10Y faiz, dolar), emtia, Bitcoin
     - Avrupa ve Asya borsaları
     - Endeksi taşıyan dev şirketlerin seans öncesi hareketi
     - Bugünkü önemli ABD verileri (ForexFactory takvimi - resmi değil)
     - Bilançolar (Finnhub): bugün açılış öncesi + dün kapanış sonrası
2) /brifing — aynı brifingi istediğin an gönderir
3) /endeks_test — SPY ve QQQ son 10 yıl: açılış boşluğu, önceki gün ve VIX
   değişimine göre gün nasıl kapanıyor (detaylı rapor dosyası)

Komutlar abd_backtest.py'nin komut döngüsünden yönlendiriliyor (aynı Telegram
token'ını iki ayrı döngü dinlerse 409 Conflict olur). Bu modül sadece
mesaj gönderiyor.
"""
import os
import json
import time
import threading
from datetime import datetime, timedelta

import requests
import pandas as pd
import yfinance as yf

TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN", "")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")
FINNHUB_API_KEY = os.environ.get("FINNHUB_API_KEY", "")
DATA_DIR = os.environ.get("DATA_DIR", ".")

BRIFING_SURUM = "abd-endeks-brifing-v1-2026-10-26"
NY = "America/New_York"
TR = "Europe/Istanbul"
BRIFING_SAAT_NY = (9, 0)
BRIFING_SON_DK_NY = (9, 40)   # deploy bu saatten sonra olursa o gün eski brifing atılmaz
GECMIS_YIL = 10

# Elle güncellenmeli. Kaynak: NYSE resmi tatil takvimi (2026 sonu - 2027).
NYSE_TATILLER = {
    "2026-11-26", "2026-12-25",
    "2027-01-01", "2027-01-18", "2027-02-15", "2027-03-26", "2027-05-31",
    "2027-06-18", "2027-07-05", "2027-09-06", "2027-11-25", "2027-12-24",
}

VADELILER = [("ES=F", "S&P 500"), ("NQ=F", "Nasdaq 100"), ("YM=F", "Dow"), ("RTY=F", "Russell 2000")]
RISK = [("^VIX", "VIX (korku)"), ("^TNX", "10Y faiz"), ("DX-Y.NYB", "Dolar endeksi")]
EMTIA = [("CL=F", "Petrol"), ("GC=F", "Altın"), ("BTC-USD", "Bitcoin")]
DUNYA = [("^GDAXI", "DAX"), ("^STOXX50E", "Euro Stoxx 50"), ("^FTSE", "FTSE 100"),
         ("^N225", "Nikkei"), ("^HSI", "Hang Seng")]
DEVLER = ["NVDA", "AAPL", "MSFT", "AMZN", "GOOGL", "META", "AVGO", "TSLA"]

GAP_SINIRLAR = [-99, -1.0, -0.5, -0.2, 0.2, 0.5, 1.0, 99]
GAP_ETIKET = ["-%1'den büyük düşüş", "-%1 / -%0.5", "-%0.5 / -%0.2", "yatay (±%0.2)",
              "+%0.2 / +%0.5", "+%0.5 / +%1", "+%1'den büyük yükseliş"]

_oturum = requests.Session()
_kilit = threading.Lock()
_test_calisiyor = {"durum": False}
_gecmis_onbellek = {}


# =============================================================================
# TELEGRAM
# =============================================================================
def mesaj_gonder(metin):
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        print(f"[Brifing devre dışı] {metin}", flush=True)
        return
    try:
        _oturum.post(f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage",
                     json={"chat_id": TELEGRAM_CHAT_ID, "text": metin[:4000],
                           "disable_web_page_preview": True}, timeout=20)
    except Exception as e:
        print(f"[Brifing Telegram] {e}", flush=True)


def dosya_gonder(yol, aciklama):
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        print(f"[Brifing] dosya: {yol}", flush=True)
        return
    try:
        with open(yol, "rb") as f:
            _oturum.post(f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendDocument",
                         data={"chat_id": TELEGRAM_CHAT_ID, "caption": aciklama},
                         files={"document": f}, timeout=60)
    except Exception as e:
        print(f"[Brifing] dosya gönderilemedi: {e}", flush=True)


# =============================================================================
# FİYAT YARDIMCILARI
# =============================================================================
def _ny_simdi():
    return pd.Timestamp.now(tz=NY)


def son_ve_onceki(sembol, seans_disi=True):
    """(son_fiyat, önceki_kapanış, güncel_mi). Önceki kapanış = bugünden
    önceki son günlük kapanış. Son fiyat = varsa bugünkü dakikalık veri
    (seans öncesi dahil), yoksa son günlük kapanış (güncel_mi=False)."""
    t = yf.Ticker(sembol)
    gunluk = t.history(period="10d", interval="1d")
    if gunluk is None or gunluk.empty:
        return None
    bugun = _ny_simdi().date()
    tarihler = [ts.date() for ts in gunluk.index]
    onceki_satirlar = [i for i, d in enumerate(tarihler) if d < bugun]
    if not onceki_satirlar:
        return None
    onceki = float(gunluk["Close"].iloc[onceki_satirlar[-1]])
    son, guncel = None, False
    try:
        dakika = t.history(period="1d", interval="1m", prepost=seans_disi)
        if dakika is not None and not dakika.empty:
            ts = dakika.index[-1].tz_convert(NY) if dakika.index[-1].tzinfo else dakika.index[-1]
            if ts.date() == bugun:
                son, guncel = float(dakika["Close"].iloc[-1]), True
    except Exception:
        pass
    if son is None:
        if tarihler[-1] == bugun:
            son, guncel = float(gunluk["Close"].iloc[-1]), True
        else:
            son = onceki
            if len(onceki_satirlar) >= 2:
                onceki = float(gunluk["Close"].iloc[onceki_satirlar[-2]])
    return son, onceki, guncel


def _yuzde(sembol, seans_disi=True):
    try:
        r = son_ve_onceki(sembol, seans_disi)
    except Exception as e:
        print(f"[Brifing] {sembol}: {e}", flush=True)
        return None
    if not r or not r[1]:
        return None
    son, onceki, guncel = r
    return son, (son / onceki - 1) * 100, guncel


def _bicim(etiket, sonuc, puan=False, deger=False):
    if sonuc is None:
        return f"{etiket}: veri yok"
    son, degisim, guncel = sonuc
    eski = "" if guncel else " (dünkü)"
    if puan:   # faiz: yüzde değil puan farkı
        onceki = son / (1 + degisim / 100)
        return f"{etiket} %{son:.2f} ({(son - onceki) * 100:+.0f} baz puan){eski}"
    if deger:
        return f"{etiket} {son:,.1f} (%{degisim:+.2f}){eski}"
    return f"{etiket} %{degisim:+.2f}{eski}"


# =============================================================================
# GEÇMİŞ İSTATİSTİK — "geçmişte benzer açılışlar"
# =============================================================================
def gecmis_veri(sembol):
    """Günlük: gap (açılış/önceki kapanış), seans (kapanış/açılış), gün
    (kapanış/önceki kapanış), gap kapandı mı. Günde bir kez önbellekli."""
    anahtar = (sembol, _ny_simdi().date())
    if anahtar in _gecmis_onbellek:
        return _gecmis_onbellek[anahtar]
    df = yf.Ticker(sembol).history(period=f"{GECMIS_YIL}y", interval="1d")
    df = df.dropna(subset=["Open", "Close", "High", "Low"])
    onceki = df["Close"].shift(1)
    sonuc = pd.DataFrame({
        "gap": (df["Open"] / onceki - 1) * 100,
        "seans": (df["Close"] / df["Open"] - 1) * 100,
        "gun": (df["Close"] / onceki - 1) * 100,
        "onceki_gun": (onceki / df["Close"].shift(2) - 1) * 100,
        "gap_kapandi": ((df["Open"] > onceki) & (df["Low"] <= onceki))
                       | ((df["Open"] < onceki) & (df["High"] >= onceki)),
    }).dropna()
    bugun = _ny_simdi().date()
    sonuc = sonuc[[ts.date() < bugun for ts in sonuc.index]]  # bugünün yarım günü hariç
    _gecmis_onbellek.clear()
    _gecmis_onbellek[anahtar] = sonuc
    return sonuc


def gap_dilimi(gap):
    for i in range(len(GAP_SINIRLAR) - 1):
        if GAP_SINIRLAR[i] <= gap < GAP_SINIRLAR[i + 1]:
            return i
    return len(GAP_ETIKET) - 1


def dilim_istatistigi(df, sutun, alt, ust):
    secim = df[(df[sutun] >= alt) & (df[sutun] < ust)]
    n = len(secim)
    if n == 0:
        return None
    return {"n": n,
            "seans_yukari": (secim["seans"] > 0).mean() * 100,
            "seans_ort": secim["seans"].mean(),
            "gun_yukari": (secim["gun"] > 0).mean() * 100,
            "gun_ort": secim["gun"].mean(),
            "gap_kapandi": secim["gap_kapandi"].mean() * 100}


# =============================================================================
# TAKVİM VE BİLANÇOLAR
# =============================================================================
def bugunku_veriler():
    """ForexFactory haftalık takvim (resmi olmayan ama yaygın kullanılan JSON).
    Sadece ABD (USD), orta ve yüksek önemdeki veriler."""
    r = _oturum.get("https://nfs.faireconomy.media/ff_calendar_thisweek.json", timeout=20)
    r.raise_for_status()
    bugun = _ny_simdi().date()
    simdi = _ny_simdi()
    satirlar = []
    for o in r.json():
        if o.get("country") != "USD" or o.get("impact") not in ("High", "Medium"):
            continue
        try:
            ts = pd.Timestamp(o["date"]).tz_convert(NY)
        except Exception:
            continue
        if ts.date() != bugun:
            continue
        isaret = "🔴" if o.get("impact") == "High" else "🟠"
        durum = "açıklandı" if ts <= simdi else "bekleniyor"
        beklenti = f", beklenti {o['forecast']}" if o.get("forecast") else ""
        satirlar.append((ts, f"{isaret} {ts.tz_convert(TR).strftime('%H:%M')} {o.get('title', '')}"
                             f" ({durum}{beklenti})"))
    return [s for _, s in sorted(satirlar)]


def bilancolar():
    """Finnhub: dün kapanış sonrası (amc) + bugün açılış öncesi (bmo).
    Sadece Akıllı Para listesindeki (büyük/orta ölçekli) şirketler."""
    if not FINNHUB_API_KEY:
        return None
    try:
        import abd_akilli_para as AK
        izlenen = set(AK.AKILLI_PARA_TICKERS)
    except Exception:
        izlenen = None
    bugun = _ny_simdi().date()
    dun = bugun - timedelta(days=3 if bugun.weekday() == 0 else 1)
    r = _oturum.get("https://finnhub.io/api/v1/calendar/earnings",
                    params={"from": dun.isoformat(), "to": bugun.isoformat(),
                            "token": FINNHUB_API_KEY}, timeout=20)
    r.raise_for_status()
    sonuc = {"dun_aksam": [], "bugun_sabah": [], "bugun_aksam": []}
    for b in r.json().get("earningsCalendar", []):
        sembol = b.get("symbol", "")
        if izlenen is not None and sembol not in izlenen:
            continue
        tahmin, gercek = b.get("epsEstimate"), b.get("epsActual")
        not_ = ""
        if tahmin is not None and gercek is not None:
            not_ = " ✅beklentiyi aştı" if gercek > tahmin else (" ❌beklentinin altında" if gercek < tahmin else " =beklenti")
        satir = f"{sembol}{not_}"
        tarih, saat = b.get("date", ""), b.get("hour", "")
        if tarih == dun.isoformat() and saat == "amc":
            sonuc["dun_aksam"].append(satir)
        elif tarih == bugun.isoformat() and saat == "bmo":
            sonuc["bugun_sabah"].append(satir)
        elif tarih == bugun.isoformat() and saat == "amc":
            sonuc["bugun_aksam"].append(sembol)
    return sonuc


# =============================================================================
# BRİFİNG
# =============================================================================
def brifing_olustur():
    simdi = _ny_simdi()
    acilis_tr = simdi.replace(hour=9, minute=30, second=0).tz_convert(TR).strftime("%H:%M")
    gunler = ["Pazartesi", "Salı", "Çarşamba", "Perşembe", "Cuma", "Cumartesi", "Pazar"]
    s = [f"📋 ABD Açılış Öncesi Brifing — {simdi.strftime('%d.%m.%Y')} {gunler[simdi.weekday()]}",
         f"Ana seans Türkiye saatiyle {acilis_tr}'da açılıyor.", ""]

    # 1) Vadeliler
    vadeli = {}
    satir = []
    for sembol, ad in VADELILER:
        r = _yuzde(sembol)
        vadeli[sembol] = r
        satir.append(_bicim(ad, r))
    s.append("🔮 Vadeli endeksler (dünkü kapanışa göre)")
    s.append("  " + " | ".join(satir))

    # 2) Geçmişte benzer açılışlar
    for vadeli_sembol, etf, ad in (("ES=F", "SPY", "S&P 500"), ("NQ=F", "QQQ", "Nasdaq")):
        r = vadeli.get(vadeli_sembol)
        if not r:
            continue
        try:
            df = gecmis_veri(etf)
            i = gap_dilimi(r[1])
            ist = dilim_istatistigi(df, "gap", GAP_SINIRLAR[i], GAP_SINIRLAR[i + 1])
            tum_yukari = (df["gun"] > 0).mean() * 100
        except Exception as e:
            print(f"[Brifing] geçmiş {etf}: {e}", flush=True)
            continue
        if not ist:
            continue
        s.append("")
        s.append(f"📊 Geçmişte benzer açılış ({ad}, {etf} son {GECMIS_YIL} yıl, "
                 f"açılış {GAP_ETIKET[i]}: {ist['n']} gün)")
        s.append(f"  Gün yukarı kapandı: %{ist['gun_yukari']:.0f} (tüm günlerde %{tum_yukari:.0f})")
        s.append(f"  Açılıştan sonra seans yukarı gitti: %{ist['seans_yukari']:.0f} "
                 f"(ort. %{ist['seans_ort']:+.2f})")
        if i != 3:
            s.append(f"  Açılış boşluğu gün içinde kapandı: %{ist['gap_kapandi']:.0f}")

    # 3) Risk / emtia / dünya
    s.append("")
    s.append("😨 Risk: " + " | ".join([
        _bicim("VIX", _yuzde("^VIX"), deger=True),
        _bicim("10Y faiz", _yuzde("^TNX"), puan=True),
        _bicim("Dolar", _yuzde("DX-Y.NYB"), deger=True)]))
    s.append("🛢 " + " | ".join(_bicim(ad, _yuzde(sm)) for sm, ad in EMTIA))
    s.append("🌍 " + " | ".join(_bicim(ad, _yuzde(sm, seans_disi=False)) for sm, ad in DUNYA))

    # 4) Devler
    devler = []
    for sm in DEVLER:
        r = _yuzde(sm, seans_disi=True)
        if r:
            devler.append((r[1], f"{sm} %{r[1]:+.1f}" + ("" if r[2] else "*")))
    if devler:
        devler.sort(reverse=True)
        s.append("🏢 Devler (seans öncesi): " + ", ".join(d for _, d in devler))
        if any(d.endswith("*") for _, d in devler):
            s.append("  (* = seans öncesi işlem verisi yok, dünkü hareket)")

    # 5) Veriler
    s.append("")
    try:
        veriler = bugunku_veriler()
        s.append("📅 Bugünkü ABD verileri (TR saati):")
        s.extend(f"  {v}" for v in veriler) if veriler else s.append("  Önemli veri yok.")
    except Exception as e:
        s.append(f"📅 Veri takvimi alınamadı ({str(e)[:60]})")

    # 6) Bilançolar
    try:
        b = bilancolar()
        if b is not None:
            s.append("💼 Bilançolar:")
            s.append("  Dün kapanış sonrası: " + (", ".join(b["dun_aksam"]) or "yok"))
            s.append("  Bugün açılış öncesi: " + (", ".join(b["bugun_sabah"]) or "yok"))
            if b["bugun_aksam"]:
                s.append("  Bugün kapanış sonrası gelecek: " + ", ".join(b["bugun_aksam"][:12]))
    except Exception as e:
        s.append(f"💼 Bilanço takvimi alınamadı ({str(e)[:60]})")

    s.append("")
    s.append("ℹ️ Bu bir yön tahmini değil. 'Geçmişte benzer açılış' satırları gerçek "
             "verinin istatistiği; tek bir günü garanti etmez.")
    return "\n".join(s)


def brifing_gonder():
    try:
        mesaj_gonder(brifing_olustur())
    except Exception as e:
        mesaj_gonder(f"❌ Brifing oluşturulamadı: {e}")


def _durum_yolu():
    return os.path.join(DATA_DIR, "endeks_brifing_durum.json")


def _son_gonderim():
    try:
        with open(_durum_yolu(), encoding="utf-8") as f:
            return json.load(f).get("son", "")
    except Exception:
        return ""


def _gonderim_kaydet(gun):
    try:
        os.makedirs(DATA_DIR, exist_ok=True)
        with open(_durum_yolu(), "w", encoding="utf-8") as f:
            json.dump({"son": gun}, f)
    except Exception:
        pass


def brifing_zamanlayici():
    """Her 30 sn kontrol: NY saatiyle 09:00-09:40 arası, iş günü, bugün
    gönderilmediyse brifingi gönder."""
    while True:
        try:
            simdi = _ny_simdi()
            gun = simdi.strftime("%Y-%m-%d")
            dk = (simdi.hour, simdi.minute)
            if (simdi.weekday() < 5 and gun not in NYSE_TATILLER
                    and BRIFING_SAAT_NY <= dk < BRIFING_SON_DK_NY and _son_gonderim() != gun):
                _gonderim_kaydet(gun)
                brifing_gonder()
        except Exception as e:
            print(f"[Brifing zamanlayıcı] {e}", flush=True)
        time.sleep(30)


# =============================================================================
# /endeks_test — 10 yıllık geriye dönük rapor
# =============================================================================
def _tablo(df, sutun, sinirlar, etiketler, s):
    tum_gun = (df["gun"] > 0).mean() * 100
    s.append("| Koşul | Gün sayısı | Gün yukarı | Ort. gün | Seans yukarı | Ort. seans |")
    s.append("|---|---|---|---|---|---|")
    for i, etiket in enumerate(etiketler):
        ist = dilim_istatistigi(df, sutun, sinirlar[i], sinirlar[i + 1])
        if not ist:
            continue
        fark = ist["gun_yukari"] - tum_gun
        isaret = " ⬆" if fark >= 5 else (" ⬇" if fark <= -5 else "")
        s.append(f"| {etiket} | {ist['n']} | %{ist['gun_yukari']:.0f}{isaret} | %{ist['gun_ort']:+.2f} | "
                 f"%{ist['seans_yukari']:.0f} | %{ist['seans_ort']:+.2f} |")
    s.append(f"\nKarşılaştırma: tüm günlerin %{tum_gun:.0f}'i yukarı kapandı. "
             "⬆/⬇ = bundan en az 5 puan farklı.\n")


def endeks_test_calistir():
    with _kilit:
        if _test_calisiyor["durum"]:
            mesaj_gonder("⏳ Endeks testi zaten çalışıyor.")
            return
        _test_calisiyor["durum"] = True
    try:
        mesaj_gonder(f"🔬 Endeks yön testi başladı (SPY + QQQ, son {GECMIS_YIL} yıl). 1-2 dk.")
        s = ["# ABD Endeks Yön Testi",
             f"Oluşturulma: {_ny_simdi().strftime('%Y-%m-%d %H:%M')} (New York)",
             f"Veri: Yahoo Finance günlük, son {GECMIS_YIL} yıl. Gün = önceki kapanıştan kapanışa, "
             "seans = açılıştan kapanışa.\n"]
        kisa = [f"📊 Endeks yön testi bitti (son {GECMIS_YIL} yıl)", ""]
        vix = yf.Ticker("^VIX").history(period=f"{GECMIS_YIL}y", interval="1d")["Close"]
        vix_deg = (vix / vix.shift(1) - 1) * 100
        vix_deg.index = [ts.date() for ts in vix_deg.index]
        for etf, ad in (("SPY", "S&P 500"), ("QQQ", "Nasdaq 100")):
            df = gecmis_veri(etf).copy()
            s.append(f"\n## {ad} ({etf}) — {len(df)} gün\n")
            s.append("### 1) Açılış boşluğuna göre (vadelilerin gece hareketi ≈ bu boşluk)\n")
            _tablo(df, "gap", GAP_SINIRLAR, GAP_ETIKET, s)
            # gap kapanma
            s.append("Açılış boşluğu gün içinde kapanma oranı: " + ", ".join(
                f"{GAP_ETIKET[i]} %{dilim_istatistigi(df, 'gap', GAP_SINIRLAR[i], GAP_SINIRLAR[i + 1])['gap_kapandi']:.0f}"
                for i in range(len(GAP_ETIKET)) if i != 3
                and dilim_istatistigi(df, 'gap', GAP_SINIRLAR[i], GAP_SINIRLAR[i + 1])) + "\n")

            s.append("### 2) Önceki günün hareketine göre\n")
            onceki_sinir = [-99, -2, -1, 0, 1, 2, 99]
            onceki_etiket = ["dün -%2'den sert düştü", "dün -%2 / -%1", "dün -%1 / 0",
                             "dün 0 / +%1", "dün +%1 / +%2", "dün +%2'den sert yükseldi"]
            _tablo(df, "onceki_gun", onceki_sinir, onceki_etiket, s)

            s.append("### 3) Dünkü VIX değişimine göre\n")
            vix_onceki = vix_deg.copy()
            tarih_listesi = list(vix_onceki.index)
            onceki_harita = {tarih_listesi[k]: vix_onceki.iloc[k - 1] for k in range(1, len(tarih_listesi))}
            df["vix_dun"] = [onceki_harita.get(ts.date()) for ts in df.index]
            df_v = df.dropna(subset=["vix_dun"])
            vix_sinir = [-99, -10, -5, 0, 5, 10, 99]
            vix_etiket = ["VIX dün -%10'dan fazla düştü", "VIX dün -%10 / -%5", "VIX dün -%5 / 0",
                          "VIX dün 0 / +%5", "VIX dün +%5 / +%10", "VIX dün +%10'dan fazla arttı"]
            _tablo(df_v, "vix_dun", vix_sinir, vix_etiket, s)

            # kısa özet: gap'e göre
            tum = (df["gun"] > 0).mean() * 100
            kisa.append(f"{ad} (tüm günler %{tum:.0f} yukarı):")
            for i in (1, 2, 4, 5):
                ist = dilim_istatistigi(df, "gap", GAP_SINIRLAR[i], GAP_SINIRLAR[i + 1])
                if ist:
                    kisa.append(f"  Açılış {GAP_ETIKET[i]}: gün yukarı %{ist['gun_yukari']:.0f}, "
                                f"seans yukarı %{ist['seans_yukari']:.0f}, boşluk kapandı %{ist['gap_kapandi']:.0f} "
                                f"(n={ist['n']})")
            kisa.append("")
        s.append("\n## Sınırlar")
        s.append("- 'Gün yukarı' oranı büyük ölçüde açılış boşluğunun kendisinden gelir (yukarı açılan "
                 "gün zaten önde başlar). Açılıştan SONRA ne olduğunu 'seans' sütunları gösterir.")
        s.append("- Geçmiş 10 yıl çoğunlukla yükselen bir piyasaydı; 'tüm günler' oranı bu yüzden %50'nin üstünde.")
        yol = os.path.join(DATA_DIR, "endeks_yon_testi.md")
        os.makedirs(DATA_DIR, exist_ok=True)
        with open(yol, "w", encoding="utf-8") as f:
            f.write("\n".join(s))
        kisa.append("Detaylı tablolar dosyada.")
        mesaj_gonder("\n".join(kisa))
        dosya_gonder(yol, "📄 Endeks yön testi (detaylı)")
    except Exception as e:
        mesaj_gonder(f"❌ Endeks testi hatası: {e}")
    finally:
        with _kilit:
            _test_calisiyor["durum"] = False


def baslangic():
    mesaj_gonder(
        f"📋 ABD Açılış Öncesi Brifing AKTİF — {BRIFING_SURUM}\n\n"
        "Her iş günü açılıştan 30 dk önce (TR saatiyle şu an 16:00, 1 Kasım'dan "
        "itibaren 17:00) otomatik brifing gelecek: vadeliler, geçmişte benzer "
        "açılışların istatistiği, VIX/faiz/dolar, dünya borsaları, dev şirketler, "
        "bugünkü veriler ve bilançolar.\n\n"
        "Komutlar:\n/brifing — brifingi şimdi gönder\n"
        "/endeks_test — 10 yıllık endeks yön testi (açılış boşluğu, önceki gün, VIX)")
