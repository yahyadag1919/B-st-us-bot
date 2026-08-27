"""
neden_patlamadi.py — "PATLAYAN vs AYNI GÖRÜNÜMDE OLUP PATLAMAYAN"
==================================================================
2026-08-19 — Kullanıcının haklı itirazı: "röntgende sadece 'neden
yükseldi' sorduk, bir de 'neden YÜKSELMEDİ' sorusuna bakalım."

ÖNCEKİ TESTLERİN EKSİĞİ:
- patlama_rontgeni.py: patlayanları SIRADAN anlarla karşılaştırdı →
  güçlü farklar buldu (p=1e-106) ama...
- patlama_stratejisi.py: o farklardan kural çıkardı → DIŞARIDA
  (görülmemiş hisselerde) ortalama -%0.08, kör çizgiyi geçemedi.
- SEBEP: bulduğumuz özellikler patlamanın GEREKLİ koşuluydu ama
  YETERLİ koşulu değildi. Aynı görünümdeki anların sadece ~%12'si
  patladı. Asıl ayırt edici bilgi, o %12 ile diğer %88 arasındaki
  farkta saklı - ve ona HİÇ bakmadık.

BU DOSYA TAM OLARAK ONA BAKIYOR:
Aynı kurulum penceresindeki (günün ilk saati) TÜM anları alır, ikiye
ayırır ve karşılaştırır:
  PATLADI   : sonraki 8 bar (2 saat) içinde >= %5 yükseldi
  PATLAMADI : aynı pencerede < %2 yükseldi (net ayrım için arası
              "ORTA" olarak ayrılıp ana karşılaştırmadan çıkarılır)

RÖNTGENDE HİÇ BAKMADIĞIMIZ YENİ ÖZELLİKLER:
  - PİYASA BAĞLAMI (SPY): o anda piyasa geneli ne yapıyordu? Patlamalar
    güçlü piyasa günlerinde mi kümeleniyor? (röntgende hiç yoktu)
  - Hacim İVMESİ: hacim sadece yüksek mi, yoksa ARTIYOR mu?
  - Mum gövde oranı: |kapanış-açılış| / (yüksek-düşük) → kararlılık
  - Ardışık yön: son 3/5 barın kaçı yükselişti
  - Dünkü ve son 5 günün getirisi (çok günlü bağlam)
  - Gün içi aralıktaki konum: dibe mi yakın, tepeye mi
  - Dolar hacmi (likidite), ATR yüzdesi, 20-bar zirveye uzaklık

TÜM ÖLÇÜMLER GELECEĞE BAKMADAN - sadece o ana kadarki veriyle.

Start Command:  python neden_patlamadi.py
Bu deploy'da SADECE bu araştırma çalışır.
"""
import os
import time
import threading

import numpy as np
import pandas as pd
import requests
from flask import Flask
from scipy import stats as _stats

TELEGRAM_TOKEN = os.environ.get("ARGE_TELEGRAM_TOKEN", "")
TELEGRAM_CHAT_ID = os.environ.get("ARGE_TELEGRAM_CHAT_ID", "")
DATA_DIR = os.environ.get("DATA_DIR", ".")
PORT = int(os.environ.get("PORT", "10000"))
KOD_SURUMU = "neden-patlamadi-v1-2026-08-19"

PATLADI_ESIK = 5.0      # >= %5 -> PATLADI
PATLAMADI_ESIK = 2.0    # < %2  -> PATLAMADI (arasi ORTA, disarida birakilir)
PENCERE_BAR = 8         # 2 saat
MAX_BAR_NO = 4          # gunun ilk ~1 saati (rontgende patlamalar burada yogundu)

HISSELER = [
    "GME", "AMC", "MARA", "RIOT", "MSTR", "PLTR", "SOFI", "LCID", "RIVN",
    "NIO", "XPEV", "LI", "OCGN", "INO", "VXRT", "BNGO", "SPCE", "NKLA",
    "CLOV", "BB", "IONQ", "RGTI", "SMCI", "UPST", "AFRM", "CVNA", "DKNG",
    "HOOD", "COIN", "ROKU", "SNAP", "PLUG", "FCEL", "CHPT", "QS", "BBAI",
    "SOUN", "CRSP", "NTLA", "BEAM", "RXRX", "ACHR", "JOBY", "DNA", "GEVO",
    "MULN", "TLRY", "CGC", "OPEN", "RUN", "BLNK", "EVGO", "LAZR", "MVIS",
    "GSAT", "EOSE", "FUBO", "SNDL", "KOSS", "EXPR",
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
        print(f"[Telegram kapalı] {dosya_yolu}", flush=True)
        return
    try:
        with open(dosya_yolu, "rb") as f:
            requests.post(f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendDocument",
                          data={"chat_id": TELEGRAM_CHAT_ID, "caption": caption[:1024]},
                          files={"document": f}, timeout=60)
    except Exception as e:
        print(f"[Telegram dosya hatası] {e}", flush=True)


def _veri_cek(ticker, period, interval, sert_sure=30):
    import concurrent.futures
    import yfinance as yf

    def _cek():
        return yf.Ticker(ticker).history(period=period, interval=interval, timeout=20)

    ex = concurrent.futures.ThreadPoolExecutor(max_workers=1)
    try:
        return ex.submit(_cek).result(timeout=sert_sure)
    except concurrent.futures.TimeoutError:
        print(f"[SERT zaman aşımı] {ticker}", flush=True)
        return None
    except Exception as e:
        print(f"[Veri hatası] {ticker}: {e}", flush=True)
        return None
    finally:
        ex.shutdown(wait=False)


def _normalize(ham):
    df = ham.reset_index()
    df.columns = [str(c) for c in df.columns]
    df = df.rename(columns={df.columns[0]: "ts", "Open": "open", "High": "high",
                             "Low": "low", "Close": "close", "Volume": "volume"})
    df["ts"] = pd.to_datetime(df["ts"]).dt.tz_localize(None)
    df["gun"] = df["ts"].dt.date
    return df


def _spy_haritasi():
    """Piyasa baglami - SPY'in her 15dk barinda o ana kadarki gun ici
    getirisi. Rontgende HIC bakmadigimiz bir boyut."""
    ham = _veri_cek("SPY", "60d", "15m")
    if ham is None or ham.empty:
        print("[UYARI] SPY verisi alınamadı - piyasa bağlamı ölçülemeyecek.", flush=True)
        return {}
    df = _normalize(ham)
    harita = {}
    for gun, grup in df.groupby("gun", sort=True):
        acilis = grup.iloc[0]["open"]
        if acilis <= 0:
            continue
        for ts, kapanis in zip(grup["ts"], grup["close"]):
            harita[ts] = (kapanis - acilis) / acilis * 100
    return harita


def _ozellikler(df, idx, gun_ilk, onceki_gun_kapanis, gun5_once_kapanis, spy_harita):
    """Geleceğe BAKMADAN, sadece idx'e kadarki veriyle ölçer."""
    if idx < 25:
        return None
    bar = df.iloc[idx]
    gecmis = df.iloc[max(0, idx - 20):idx]
    if len(gecmis) < 15:
        return None
    ort_hacim = gecmis["volume"].mean()
    if ort_hacim <= 0 or pd.isna(ort_hacim):
        return None

    gun_barlari = df.iloc[gun_ilk:idx + 1]
    gun_acilis = gun_barlari.iloc[0]["open"]
    gun_yuksek = gun_barlari["high"].max()
    gun_dusuk = gun_barlari["low"].min()
    tipik = (gun_barlari["high"] + gun_barlari["low"] + gun_barlari["close"]) / 3
    vwap = (tipik * gun_barlari["volume"]).sum() / max(gun_barlari["volume"].sum(), 1e-9)

    son3 = df.iloc[max(0, idx - 2):idx + 1]
    son5 = df.iloc[max(0, idx - 4):idx + 1]
    govde = abs(bar["close"] - bar["open"])
    aralik = bar["high"] - bar["low"]
    tr = pd.concat([gecmis["high"] - gecmis["low"],
                    (gecmis["high"] - gecmis["close"].shift()).abs(),
                    (gecmis["low"] - gecmis["close"].shift()).abs()], axis=1).max(axis=1)
    atr = tr.mean()

    return {
        # --- rontgende de vardi ---
        "gunun_kacinci_bari": int(idx - gun_ilk),
        "hacim_orani": round(float(bar["volume"] / ort_hacim), 2),
        "onceki_bar_hacim_orani": round(float(df.iloc[idx - 1]["volume"] / ort_hacim), 2),
        "vwap_uzaklik_pct": round(float((bar["close"] - vwap) / vwap * 100), 2) if vwap else None,
        "sikisma_20bar_pct": round(float((gecmis["high"].max() - gecmis["low"].min()) / bar["close"] * 100), 2) if bar["close"] else None,
        "gun_ici_getiri_o_ana_pct": round(float((bar["close"] - gun_acilis) / gun_acilis * 100), 2) if gun_acilis else None,
        "fiyat": round(float(bar["close"]), 2),
        # --- YENI: rontgende HIC bakmadiklarimiz ---
        "spy_gun_ici_pct": round(float(spy_harita.get(bar["ts"], np.nan)), 3) if bar["ts"] in spy_harita else None,
        "hacim_ivmesi": round(float(son3["volume"].mean() / ort_hacim), 2),
        "hacim_artiyor_mu": int(df.iloc[idx]["volume"] > df.iloc[idx - 1]["volume"] > df.iloc[idx - 2]["volume"]),
        "govde_orani": round(float(govde / aralik), 2) if aralik > 0 else None,
        "son3_yukselen_bar": int((son3["close"].values > son3["open"].values).sum()),
        "son5_yukselen_bar": int((son5["close"].values > son5["open"].values).sum()),
        "onceki_bar_getiri_pct": round(float((df.iloc[idx - 1]["close"] - df.iloc[idx - 2]["close"]) / df.iloc[idx - 2]["close"] * 100), 2) if df.iloc[idx - 2]["close"] else None,
        "gap_pct": round(float((gun_acilis - onceki_gun_kapanis) / onceki_gun_kapanis * 100), 2) if onceki_gun_kapanis else None,
        "dun_getiri_pct": None,  # asagida doldurulur
        "son5gun_getiri_pct": round(float((bar["close"] - gun5_once_kapanis) / gun5_once_kapanis * 100), 2) if gun5_once_kapanis else None,
        "gun_ici_aralik_konumu": round(float((bar["close"] - gun_dusuk) / (gun_yuksek - gun_dusuk)), 2) if gun_yuksek > gun_dusuk else None,
        "dolar_hacim_bin": round(float(bar["close"] * bar["volume"] / 1000), 1),
        "atr_pct": round(float(atr / bar["close"] * 100), 2) if bar["close"] and atr else None,
        "zirveye_uzaklik_pct": round(float((gecmis["high"].max() - bar["close"]) / bar["close"] * 100), 2) if bar["close"] else None,
    }


def calistir():
    spy_harita = _spy_haritasi()
    kayitlar = []
    for n_i, ticker in enumerate(HISSELER, 1):
        print(f"[Analiz {n_i}/{len(HISSELER)}] {ticker}...", flush=True)
        ham = _veri_cek(ticker, "60d", "15m")
        if ham is None or ham.empty or len(ham) < 60:
            time.sleep(0.4)
            continue
        try:
            df = _normalize(ham)
            gun_ilk, gun_son_kapanis = {}, {}
            gunler = []
            for gun, grup in df.groupby("gun", sort=True):
                gun_ilk[gun] = grup.index[0]
                gun_son_kapanis[gun] = grup.iloc[-1]["close"]
                gunler.append(gun)

            for idx in range(25, len(df) - PENCERE_BAR):
                gun = df.iloc[idx]["gun"]
                if (idx - gun_ilk[gun]) > MAX_BAR_NO:
                    continue
                giris = df.iloc[idx]["close"]
                if giris <= 0:
                    continue
                ileri = df.iloc[idx + 1: idx + 1 + PENCERE_BAR]
                ileri = ileri[ileri["gun"] == gun]
                if ileri.empty:
                    continue
                kazanc = (ileri["high"].max() - giris) / giris * 100
                if kazanc >= PATLADI_ESIK:
                    etiket = "PATLADI"
                elif kazanc < PATLAMADI_ESIK:
                    etiket = "PATLAMADI"
                else:
                    etiket = "ORTA"

                gi = gunler.index(gun) if gun in gunler else 0
                onceki_kapanis = gun_son_kapanis[gunler[gi - 1]] if gi >= 1 else None
                gun5_once = gun_son_kapanis[gunler[gi - 5]] if gi >= 5 else None
                oz = _ozellikler(df, idx, gun_ilk[gun], onceki_kapanis, gun5_once, spy_harita)
                if not oz:
                    continue
                if gi >= 2 and onceki_kapanis:
                    onceki2 = gun_son_kapanis[gunler[gi - 2]]
                    if onceki2:
                        oz["dun_getiri_pct"] = round(float((onceki_kapanis - onceki2) / onceki2 * 100), 2)
                oz.update({"ticker": ticker, "tarih": str(gun),
                            "saat": df.iloc[idx]["ts"].strftime("%H:%M"),
                            "etiket": etiket, "gerceklesen_kazanc_pct": round(float(kazanc), 2)})
                kayitlar.append(oz)
        except Exception as e:
            print(f"[Analiz] {ticker} hata: {e}", flush=True)
        time.sleep(0.4)

    if not kayitlar:
        return None, "Hiç kayıt üretilemedi."
    tum = pd.DataFrame(kayitlar)
    dosya = os.path.join(DATA_DIR, "neden_patlamadi.csv")
    tum.to_csv(dosya, index=False, encoding="utf-8-sig")

    p = tum[tum.etiket == "PATLADI"]
    n = tum[tum.etiket == "PATLAMADI"]
    kolonlar = [c for c in tum.columns if c not in
                ("ticker", "tarih", "saat", "etiket", "gerceklesen_kazanc_pct")]
    karsilastirma = []
    for kol in kolonlar:
        a, b = p[kol].dropna(), n[kol].dropna()
        if len(a) < 20 or len(b) < 20:
            continue
        try:
            _, pv = _stats.mannwhitneyu(a, b, alternative="two-sided")
        except Exception:
            continue
        karsilastirma.append({
            "ozellik": kol,
            "PATLADI_medyan": round(float(a.median()), 3),
            "PATLAMADI_medyan": round(float(b.median()), 3),
            "fark": round(float(a.median() - b.median()), 3),
            "p_deger": float(pv),
        })
    karsilastirma.sort(key=lambda x: x["p_deger"])
    return dosya, {"toplam": len(tum), "patladi": len(p), "patlamadi": len(n),
                    "orta": int((tum.etiket == "ORTA").sum()),
                    "karsilastirma": karsilastirma}


def _rapor(o):
    s = [f"🔍 PATLAYAN vs PATLAMAYAN — {KOD_SURUMU}",
         f"Aynı kurulum penceresi (günün ilk {MAX_BAR_NO} barı), {PENCERE_BAR} bar (2sa) sonrası:",
         f"  PATLADI (>=%{PATLADI_ESIK}): {o['patladi']}",
         f"  PATLAMADI (<%{PATLAMADI_ESIK}): {o['patlamadi']}",
         f"  ORTA (arada, hariç): {o['orta']}\n",
         "AYIRT EDİCİLİK SIRALAMASI (p-değerine göre, en anlamlı üstte):"]
    for k in o["karsilastirma"][:20]:
        s.append(f"  {k['ozellik']}: patladı={k['PATLADI_medyan']} | "
                 f"patlamadı={k['PATLAMADI_medyan']} | p={k['p_deger']:.2e}")
    s.append("\n⚠️ p-değeri küçük = iki grup GERÇEKTEN farklı. Ama unutma: "
             "istatistiksel fark, kâra dönüşeceği anlamına gelmiyor (bugün "
             "bunu bir kez yaşadık). Önce farkı görelim, sonra karar veririz.")
    return "\n".join(s)


app = Flask(__name__)


@app.route("/health")
def health():
    return "OK (neden patlamadi)", 200


def _ping():
    time.sleep(30)
    while True:
        try:
            requests.get(f"http://127.0.0.1:{PORT}/health", timeout=10)
        except Exception:
            pass
        time.sleep(600)


def _calis():
    time.sleep(5)
    send_telegram_message(
        f"🔍 'NEDEN PATLAMADI' ANALİZİ başlıyor — {KOD_SURUMU}\n\n"
        f"Bu sefer patlayanları SIRADAN anlarla değil, AYNI KURULUMDA OLUP "
        f"PATLAMAYANLARLA karşılaştırıyoruz.\n"
        f"{len(HISSELER)} hisse + piyasa bağlamı (SPY) taranıyor.\n"
        f"Röntgende hiç bakmadığımız yeni özellikler: piyasa bağlamı, hacim "
        f"ivmesi, mum gövde oranı, ardışık yön, dünkü/5 günlük getiri, "
        f"gün içi aralık konumu, dolar hacmi.\n\n"
        f"⚠️ Bu deploy'da SADECE bu analiz çalışıyor.\nBitince CSV + özet göndereceğim."
    )
    try:
        dosya, sonuc = calistir()
        if dosya is None:
            send_telegram_message(f"🔍 Analiz başarısız: {sonuc}")
            return
        send_telegram_document(dosya, caption=_rapor(sonuc))
    except Exception as e:
        import traceback
        traceback.print_exc()
        send_telegram_message(f"🔍 Analiz hatası: {e}")


if __name__ == "__main__":
    print(f"[BAŞLANGIÇ] neden_patlamadi.py — {KOD_SURUMU}", flush=True)
    threading.Thread(target=_calis, daemon=True).start()
    threading.Thread(target=_ping, daemon=True).start()
    app.run(host="0.0.0.0", port=PORT)
