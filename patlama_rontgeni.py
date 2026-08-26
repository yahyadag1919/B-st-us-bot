"""
patlama_rontgeni.py — GÜN İÇİ PATLAMA RÖNTGENİ (BAĞIMSIZ ARAŞTIRMA ARACI)
==========================================================================
2026-08-19 — Kullanıcının fikri: "tahmin etmeye çalışmayı bırakalım,
GEÇMİŞTE gün içinde güzel yükselen hisselerin RÖNTGENİNİ çekelim - ne
oldu da yükseldi?"

Bu dosya TAMAMEN BAĞIMSIZ çalışır. Ne canlı sinyal botu, ne Ar-Ge botu,
ne Gemini, ne BIST taraması - HİÇBİRİ çalışmaz. Sadece bu araştırma.
Start Command:  python patlama_rontgeni.py

NE YAPIYOR:
1. Volatil/küçük hisselerin son 60 günlük 15dk verisini tarar
2. "Patlama" tanımı: bir bardan sonraki 8 bar (2 saat) içinde fiyatın
   >= EŞİK kadar yükseldiği İLK bar = patlamanın başladığı an.
   (Bu tanım GERÇEK ZAMANLI tespit edilebilir - dibi sonradan bilerek
   seçmiyoruz, bu yüzden dürüst bir tanım.)
3. Patlama BAŞLAMADAN ÖNCEKİ durumu ölçer (SADECE o ana kadarki veriyle,
   geleceğe bakmadan): saat, hacim oranı, RSI, VWAP uzaklığı, sıkışma
   ölçüsü, gap, o ana kadarki gün içi getiri, fiyat seviyesi...
4. KONTROL GRUBU: patlama OLMAYAN barlardan rastgele örnek alıp AYNI
   özellikleri ölçer. Böylece "patlama öncesi durum, sıradan bir andan
   GERÇEKTEN farklı mı?" sorusuna cevap verebiliriz - bugün defalarca
   öğrendiğimiz ders: kontrol grubu olmadan hiçbir sayı anlamlı değil.

HABER KAYNAĞI HAKKINDA DÜRÜST NOT:
Geçmişe dönük gerçek haber başlıkları ücretsiz olarak güvenilir şekilde
çekilemiyor (yfinance sadece GÜNCEL haberleri veriyor, geçmişi değil).
Bu yüzden haber etkisini DOLAYLI ölçüyoruz:
  - gap_pct: açılış boşluğu (gece haberi/bilanço göstergesi)
  - patlama_ilk_barda_mi: patlama açılışın ilk barında mı başladı
    (gece haberi işareti) yoksa gün ortasında mı (teknik/akış işareti)
Bu bir yaklaşım, gerçek haber verisi değil - sonuçları buna göre yorumla.
"""
import os
import time
import threading
from datetime import datetime

import numpy as np
import pandas as pd
import requests
from flask import Flask

TELEGRAM_TOKEN = os.environ.get("ARGE_TELEGRAM_TOKEN", "")
TELEGRAM_CHAT_ID = os.environ.get("ARGE_TELEGRAM_CHAT_ID", "")
DATA_DIR = os.environ.get("DATA_DIR", ".")
PORT = int(os.environ.get("PORT", "10000"))

KOD_SURUMU = "rontgen-v1-2026-08-19"

# Patlama tanimi
PATLAMA_ESIGI_PCT = 5.0      # sonraki 8 bar icinde >= %5 yukselis
PATLAMA_PENCERE_BAR = 8      # 8 x 15dk = 2 saat
KONTROL_ORNEK_ORANI = 0.02   # patlama olmayan barlardan %2 rastgele ornek

# Hisse evreni - kucuk/volatil (patlamalar burada oluyor)
HISSELER = [
    "GME", "AMC", "MARA", "RIOT", "MSTR", "PLTR", "SOFI", "LCID", "RIVN",
    "NIO", "XPEV", "LI", "OCGN", "INO", "VXRT", "BNGO", "SPCE", "NKLA",
    "CLOV", "BB", "IONQ", "RGTI", "SMCI", "UPST", "AFRM", "CVNA", "DKNG",
    "HOOD", "COIN", "ROKU", "SNAP", "PLUG", "FCEL", "CHPT", "QS", "BBAI",
    "SOUN", "CRSP", "NTLA", "BEAM", "RXRX", "ACHR", "JOBY", "DNA", "GEVO",
    "MULN", "WKHS", "TLRY", "CGC", "OPEN", "RUN", "BLNK", "EVGO", "LAZR",
    "MVIS", "GSAT", "EOSE", "AMTX", "HYLN", "PROG",
]


def send_telegram_message(text: str):
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        print(f"[Telegram kapalı] {text}", flush=True)
        return
    try:
        requests.post(f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage",
                      json={"chat_id": TELEGRAM_CHAT_ID, "text": text[:4000]}, timeout=20)
    except Exception as e:
        print(f"[Telegram hata] {e}", flush=True)


def send_telegram_document(dosya_yolu: str, caption: str = ""):
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        print(f"[Telegram kapalı] Dosya: {dosya_yolu}", flush=True)
        return
    try:
        with open(dosya_yolu, "rb") as f:
            requests.post(f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendDocument",
                          data={"chat_id": TELEGRAM_CHAT_ID, "caption": caption[:1024]},
                          files={"document": f}, timeout=60)
    except Exception as e:
        print(f"[Telegram dosya hatası] {e}", flush=True)


def _rsi(close, n=14):
    d = close.diff()
    g = d.clip(lower=0).ewm(alpha=1/n, adjust=False).mean()
    k = (-d.clip(upper=0)).ewm(alpha=1/n, adjust=False).mean()
    return 100 - (100 / (1 + g / k.replace(0, np.nan)))


def _veri_cek(ticker, period, interval, sert_sure=30):
    """yfinance cagrisini SERT zaman asimiyla sarar - bugun defalarca
    yasadigimiz donma sorununa karsi."""
    import concurrent.futures
    import yfinance as yf

    def _cek():
        return yf.Ticker(ticker).history(period=period, interval=interval, timeout=20)

    ex = concurrent.futures.ThreadPoolExecutor(max_workers=1)
    try:
        return ex.submit(_cek).result(timeout=sert_sure)
    except concurrent.futures.TimeoutError:
        print(f"[SERT zaman aşımı] {ticker} atlandı.", flush=True)
        return None
    except Exception as e:
        print(f"[Veri hatası] {ticker}: {e}", flush=True)
        return None
    finally:
        ex.shutdown(wait=False)


def _ozellikleri_olc(df, idx, gun_ilk_idx, onceki_kapanis):
    """Bir bardaki durumu ölçer - SADECE o ana kadarki veriyle (geleceğe
    bakmadan). Döner: dict ya da None (veri yetersizse)."""
    if idx < 25 or idx >= len(df):
        return None
    bar = df.iloc[idx]
    gecmis = df.iloc[max(0, idx - 20):idx]  # SADECE onceki barlar
    if len(gecmis) < 15:
        return None

    ort_hacim = gecmis["volume"].mean()
    if ort_hacim <= 0 or pd.isna(ort_hacim):
        return None

    gun_barlari = df.iloc[gun_ilk_idx:idx + 1]
    gun_acilis = gun_barlari.iloc[0]["open"] if len(gun_barlari) else bar["close"]

    # gun ici VWAP (sadece o ana kadar)
    tipik = (gun_barlari["high"] + gun_barlari["low"] + gun_barlari["close"]) / 3
    vwap = (tipik * gun_barlari["volume"]).sum() / max(gun_barlari["volume"].sum(), 1e-9)

    son_20_yuksek = gecmis["high"].max()
    son_20_dusuk = gecmis["low"].min()
    sikisma_pct = (son_20_yuksek - son_20_dusuk) / bar["close"] * 100 if bar["close"] else np.nan

    rsi_serisi = _rsi(df["close"].iloc[:idx + 1], 14)
    rsi_deger = rsi_serisi.iloc[-1] if len(rsi_serisi) else np.nan

    return {
        "saat": bar["ts"].strftime("%H:%M"),
        "hacim_orani": round(float(bar["volume"] / ort_hacim), 2),
        "onceki_bar_hacim_orani": round(float(df.iloc[idx - 1]["volume"] / ort_hacim), 2),
        "rsi14": round(float(rsi_deger), 1) if pd.notna(rsi_deger) else None,
        "vwap_uzaklik_pct": round(float((bar["close"] - vwap) / vwap * 100), 2) if vwap else None,
        "sikisma_20bar_pct": round(float(sikisma_pct), 2) if pd.notna(sikisma_pct) else None,
        "gun_ici_getiri_o_ana_pct": round(float((bar["close"] - gun_acilis) / gun_acilis * 100), 2) if gun_acilis else None,
        "gap_pct": round(float((gun_acilis - onceki_kapanis) / onceki_kapanis * 100), 2) if onceki_kapanis else None,
        "fiyat": round(float(bar["close"]), 2),
        "gunun_kacinci_bari": int(idx - gun_ilk_idx),
    }


def rontgen_calistir(esik_pct=PATLAMA_ESIGI_PCT, max_hisse=None):
    hisseler = HISSELER if max_hisse is None else HISSELER[:max_hisse]
    patlamalar, kontroller = [], []
    rng = np.random.default_rng(42)

    for n_i, ticker in enumerate(hisseler, 1):
        print(f"[Röntgen {n_i}/{len(hisseler)}] {ticker}...", flush=True)
        ham = _veri_cek(ticker, "60d", "15m")
        if ham is None or ham.empty or len(ham) < 60:
            time.sleep(0.4)
            continue
        try:
            df = ham.reset_index()
            df.columns = [str(c) for c in df.columns]
            ilk = df.columns[0]
            df = df.rename(columns={ilk: "ts", "Open": "open", "High": "high",
                                     "Low": "low", "Close": "close", "Volume": "volume"})
            df["ts"] = pd.to_datetime(df["ts"]).dt.tz_localize(None)
            df["gun"] = df["ts"].dt.date

            gun_ilk_idx_harita, onceki_kapanis_harita = {}, {}
            onceki_gun_kapanis = None
            for gun, grup in df.groupby("gun", sort=True):
                gun_ilk_idx_harita[gun] = grup.index[0]
                onceki_kapanis_harita[gun] = onceki_gun_kapanis
                onceki_gun_kapanis = grup.iloc[-1]["close"]

            patlama_idxler = set()
            for idx in range(25, len(df) - PATLAMA_PENCERE_BAR):
                gun = df.iloc[idx]["gun"]
                giris = df.iloc[idx]["close"]
                if giris <= 0:
                    continue
                ileri = df.iloc[idx + 1: idx + 1 + PATLAMA_PENCERE_BAR]
                ileri = ileri[ileri["gun"] == gun]  # SADECE ayni gun
                if ileri.empty:
                    continue
                zirve = ileri["high"].max()
                kazanc = (zirve - giris) / giris * 100
                if kazanc >= esik_pct:
                    # bu bardan onceki bar da patlama basi sayildiysa atla
                    # (ayni patlamayi tekrar tekrar saymamak icin)
                    if (idx - 1) in patlama_idxler:
                        patlama_idxler.add(idx)
                        continue
                    patlama_idxler.add(idx)
                    oz = _ozellikleri_olc(df, idx, gun_ilk_idx_harita[gun],
                                           onceki_kapanis_harita.get(gun))
                    if oz:
                        zirve_konum = ileri["high"].idxmax()
                        oz.update({
                            "ticker": ticker, "tarih": str(gun), "tip": "PATLAMA",
                            "yukselis_pct": round(float(kazanc), 2),
                            "zirveye_kac_bar": int(zirve_konum - idx),
                            "patlama_ilk_barda_mi": int(idx == gun_ilk_idx_harita[gun]),
                        })
                        patlamalar.append(oz)

            # KONTROL GRUBU - patlama OLMAYAN barlardan rastgele ornek
            for idx in range(25, len(df) - PATLAMA_PENCERE_BAR):
                if idx in patlama_idxler:
                    continue
                if rng.random() > KONTROL_ORNEK_ORANI:
                    continue
                gun = df.iloc[idx]["gun"]
                oz = _ozellikleri_olc(df, idx, gun_ilk_idx_harita[gun],
                                       onceki_kapanis_harita.get(gun))
                if oz:
                    oz.update({"ticker": ticker, "tarih": str(gun), "tip": "KONTROL",
                                "yukselis_pct": None, "zirveye_kac_bar": None,
                                "patlama_ilk_barda_mi": int(idx == gun_ilk_idx_harita[gun])})
                    kontroller.append(oz)
        except Exception as e:
            print(f"[Röntgen] {ticker} hata: {e}", flush=True)
        time.sleep(0.4)

    if not patlamalar:
        return None, f"Hiç patlama bulunamadı (eşik %{esik_pct})."

    tum = pd.DataFrame(patlamalar + kontroller)
    dosya = os.path.join(DATA_DIR, "patlama_rontgeni.csv")
    tum.to_csv(dosya, index=False, encoding="utf-8-sig")

    p = pd.DataFrame(patlamalar)
    k = pd.DataFrame(kontroller) if kontroller else pd.DataFrame()

    def _med(dfx, kol):
        if dfx.empty or kol not in dfx or dfx[kol].dropna().empty:
            return None
        return round(float(dfx[kol].dropna().median()), 2)

    ozet = {
        "patlama_sayisi": len(p), "kontrol_sayisi": len(k),
        "ort_yukselis_pct": _med(p, "yukselis_pct"),
        "medyan_zirveye_bar": _med(p, "zirveye_kac_bar"),
        "ilk_barda_baslama_orani_pct": round(float(p["patlama_ilk_barda_mi"].mean() * 100), 1),
        "kontrol_ilk_bar_orani_pct": round(float(k["patlama_ilk_barda_mi"].mean() * 100), 1) if not k.empty else None,
        "karsilastirma": {},
    }
    for kol in ["hacim_orani", "onceki_bar_hacim_orani", "rsi14", "vwap_uzaklik_pct",
                "sikisma_20bar_pct", "gun_ici_getiri_o_ana_pct", "gap_pct", "fiyat"]:
        ozet["karsilastirma"][kol] = {"patlama": _med(p, kol), "kontrol": _med(k, kol)}

    # en sik patlama saatleri
    if "saat" in p:
        ozet["en_sik_saatler"] = p["saat"].value_counts().head(5).to_dict()

    return dosya, ozet


def _rapor_metni(ozet, esik):
    s = [f"🔬 GÜN İÇİ PATLAMA RÖNTGENİ (eşik: %{esik} / 2 saat)\n",
         f"Bulunan patlama: {ozet['patlama_sayisi']}",
         f"Kontrol örneği: {ozet['kontrol_sayisi']}",
         f"Medyan yükseliş: %{ozet['ort_yukselis_pct']}",
         f"Zirveye ulaşma: medyan {ozet['medyan_zirveye_bar']} bar (x15dk)",
         f"Açılışın İLK barında başlama: %{ozet['ilk_barda_baslama_orani_pct']} "
         f"(kontrolde %{ozet['kontrol_ilk_bar_orani_pct']})\n",
         "PATLAMA ÖNCESİ vs SIRADAN AN (medyan):"]
    for kol, d in ozet["karsilastirma"].items():
        s.append(f"  {kol}: patlama={d['patlama']} | kontrol={d['kontrol']}")
    if ozet.get("en_sik_saatler"):
        s.append("\nEn sık patlama saatleri:")
        for saat, adet in ozet["en_sik_saatler"].items():
            s.append(f"  {saat} → {adet} kez")
    return "\n".join(s)


app = Flask(__name__)


@app.route("/health")
def health():
    return "OK (patlama rontgeni)", 200


def _kendi_kendine_ping():
    time.sleep(30)
    while True:
        try:
            requests.get(f"http://127.0.0.1:{PORT}/health", timeout=10)
        except Exception:
            pass
        time.sleep(600)


def _arastirmayi_calistir():
    time.sleep(5)
    send_telegram_message(
        f"🔬 PATLAMA RÖNTGENİ başlıyor — {KOD_SURUMU}\n\n"
        f"{len(HISSELER)} volatil/küçük hissenin son 60 günlük 15dk verisi "
        f"taranıyor. Aranan: 2 saat içinde >= %{PATLAMA_ESIGI_PCT} yükseliş.\n"
        f"Her patlamanın BAŞLAMADAN ÖNCEKİ durumu ölçülüyor + patlama "
        f"olmayan anlardan KONTROL GRUBU alınıyor (karşılaştırma için).\n\n"
        f"⚠️ Bu deploy'da SADECE bu araştırma çalışıyor - canlı sinyal "
        f"botu, Ar-Ge botu, Gemini, BIST taraması HİÇBİRİ çalışmıyor.\n"
        f"Bitince CSV + özet göndereceğim."
    )
    try:
        dosya, ozet = rontgen_calistir()
        if dosya is None:
            send_telegram_message(f"🔬 Röntgen başarısız: {ozet}")
            return
        send_telegram_document(dosya, caption=_rapor_metni(ozet, PATLAMA_ESIGI_PCT))
    except Exception as e:
        import traceback
        traceback.print_exc()
        send_telegram_message(f"🔬 Röntgen hatası: {e}")


if __name__ == "__main__":
    print(f"[BAŞLANGIÇ] patlama_rontgeni.py — {KOD_SURUMU}", flush=True)
    print("[BAŞLANGIÇ] SADECE bu araştırma çalışıyor, başka hiçbir sistem yok.", flush=True)
    threading.Thread(target=_arastirmayi_calistir, daemon=True).start()
    threading.Thread(target=_kendi_kendine_ping, daemon=True).start()
    app.run(host="0.0.0.0", port=PORT)
