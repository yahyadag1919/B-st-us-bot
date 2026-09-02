"""
bist_kapanis_baskisi.py — KAPANIŞ DAVRANIŞI, EMİR DEFTERİNİN VEKİLİ OLABİLİR Mİ?
=================================================================================
2026-08-31 — Kullanıcının fikri: "kapanışta emir defterine baksak,
alış/satış dengesizliğinden ertesi günün yönünü daha net görebilirdik."

FİKİR SAĞLAM ama iki engel var:
  1. Erişim: Midas'ın API'si yok, Algolab hesabı gerekiyor (ertelendi)
  2. TEST EDİLEMEZ: geçmişe dönük emir defteri verisi ücretsiz YOK.
     Sistemi kursak bile işe yarayıp yaramadığını haftalarca canlı veri
     toplamadan bilemeyiz - bugüne kadarki çalışma şeklimizin tersi.

BU DOSYA ARA YOL: emir defterinin ZAYIF VEKİLİNİ test ediyor.
Kapanışa yakın dakikalardaki fiyat/hacim davranışı, alıcı mı satıcı mı
baskın olduğunu KISMEN gösterir. Gerçek emir defteri kadar net değil -
ama elimizde var ve ŞİMDİ test edilebilir.

ÖLÇÜLEN VEKİL GÖSTERGELER (5dk veri, kapanış öncesi son 30-60 dk):
  1. Son 30dk getirisi — kapanışa doğru yükseliyor mu düşüyor mu
  2. Son 30dk hacim payı — günün hacminin ne kadarı son yarım saatte
     (yoğunlaşma = kararlılık işareti olabilir)
  3. Son barın gövde yönü ve gücü — son mumda alıcı mı kazandı
  4. Kapanışın son 30dk aralığındaki konumu — tepede mi kapandı
  5. Son 30dk'da yukarı barların oranı
  6. Son 3 barın ardışık yönü (üst üste yükseliş = ivme)
  7. Kapanış fiyatının gün VWAP'ına uzaklığı

HEDEF: Ertesi gün açılış boşluğu ve gün içi yön.
Yani: "kapanışta alıcı baskınsa, ertesi gün gerçekten daha mı iyi?"

EVREN: Hem tavan yapanlar hem %6-9.5 kapatanlar ayrı ayrı.

⚠️ 5dk verisi 60 gün geriye gidiyor - örneklem sınırlı olacak.

Start Command:  python bist_kapanis_baskisi.py
Bu deploy'da SADECE bu analiz çalışır.
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
KOD_SURUMU = "kapanis-baskisi-v1-2026-08-31"

TAVAN_ESIK = 9.5
BANT_ALT, BANT_UST = 6.0, 9.49
SON_BAR = 6          # 5dk x 6 = son 30 dakika

BIST_HISSELER = [
    "THYAO.IS", "GARAN.IS", "AKBNK.IS", "ISCTR.IS", "YKBNK.IS", "VAKBN.IS",
    "HALKB.IS", "SISE.IS", "EREGL.IS", "KRDMD.IS", "KRDMA.IS", "TUPRS.IS",
    "PETKM.IS", "ASELS.IS", "TCELL.IS", "TTKOM.IS", "BIMAS.IS", "MGROS.IS",
    "SOKM.IS", "FROTO.IS", "TOASO.IS", "ARCLK.IS", "VESTL.IS", "TAVHL.IS",
    "PGSUS.IS", "KCHOL.IS", "SAHOL.IS", "DOHOL.IS", "ALARK.IS", "ENKAI.IS",
    "TKFEN.IS", "KOZAA.IS", "ODAS.IS", "ZOREN.IS", "AKSEN.IS", "EKGYO.IS",
    "ISGYO.IS", "TRGYO.IS", "HEKTS.IS", "SASA.IS", "GUBRF.IS", "AEFES.IS",
    "ULKER.IS", "CCOLA.IS", "TATGD.IS", "BANVT.IS", "PENTA.IS", "SMRTG.IS",
    "ALFAS.IS", "ASTOR.IS", "EUPWR.IS", "CWENE.IS", "GESAN.IS", "KONTR.IS",
    "ISDMR.IS", "CIMSA.IS", "AKCNS.IS", "OYAKC.IS", "BRSAN.IS", "AGHOL.IS",
    "AKFGY.IS", "ALBRK.IS", "ANSGR.IS", "ARDYZ.IS", "AYDEM.IS", "BERA.IS",
    "BIOEN.IS", "BRYAT.IS", "BUCIM.IS", "CANTE.IS", "CEMTS.IS", "DOAS.IS",
    "ECILC.IS", "EGEEN.IS", "ENJSA.IS", "ESEN.IS", "EUREN.IS", "GENIL.IS",
    "GLYHO.IS", "GWIND.IS", "HATSN.IS", "IEYHO.IS", "IZMDC.IS", "KARSN.IS",
    "KAYSE.IS", "KLSER.IS", "KORDS.IS", "MAVI.IS", "MPARK.IS", "NTHOL.IS",
    "OTKAR.IS", "PAPIL.IS", "QUAGR.IS", "SELEC.IS", "SKBNK.IS", "SNGYO.IS",
    "TMSN.IS", "TSKB.IS", "TTRAK.IS", "TURSG.IS", "ULUUN.IS", "VESBE.IS",
    "YATAS.IS", "YEOTK.IS", "YYLGD.IS", "ZRGYO.IS", "MAALT.IS", "PSDTC.IS",
    "PRZMA.IS", "DEVA.IS", "ADEL.IS", "ALCTL.IS", "BFREN.IS", "KRDMB.IS",
    "AKSA.IS", "ASUZU.IS", "BAGFS.IS", "KARTN.IS", "KATMR.IS", "KLMSN.IS",
    "KONYA.IS", "LOGO.IS", "NUHCM.IS", "PARSN.IS", "SARKY.IS", "TUKAS.IS",
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


def _veri_cek(ticker, sert_sure=40):
    import concurrent.futures
    import yfinance as yf

    def _cek():
        return yf.Ticker(ticker).history(period="60d", interval="5m", timeout=25)

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


def calistir():
    kayitlar = []
    islenen, atlanan = 0, 0

    for n_i, ticker in enumerate(BIST_HISSELER, 1):
        print(f"[Kapanış Baskısı {n_i}/{len(BIST_HISSELER)}] {ticker}...", flush=True)
        ham = _veri_cek(ticker)
        if ham is None or ham.empty or len(ham) < 80:
            atlanan += 1
            time.sleep(0.4)
            continue
        try:
            df = ham.rename(columns={"Open": "open", "High": "high", "Low": "low",
                                      "Close": "close", "Volume": "volume"})
            df = df[["open", "high", "low", "close", "volume"]].dropna()
            idx = pd.to_datetime(df.index)
            try:
                idx = idx.tz_localize(None)
            except (TypeError, AttributeError):
                pass
            df.index = idx
            df["gun"] = df.index.date
            gunler = sorted(set(df["gun"]))

            for gi in range(1, len(gunler) - 1):
                onceki = df[df["gun"] == gunler[gi - 1]]
                bugun = df[df["gun"] == gunler[gi]]
                ertesi = df[df["gun"] == gunler[gi + 1]]
                if onceki.empty or len(bugun) < SON_BAR + 4 or ertesi.empty:
                    continue
                onceki_kap = float(onceki.iloc[-1]["close"])
                kapanis = float(bugun.iloc[-1]["close"])
                if onceki_kap <= 0 or kapanis <= 0:
                    continue
                getiri = (kapanis - onceki_kap) / onceki_kap * 100
                if getiri >= TAVAN_ESIK:
                    tur = "TAVAN"
                elif BANT_ALT <= getiri < BANT_UST:
                    tur = "GUCLU"
                else:
                    continue

                son = bugun.iloc[-SON_BAR:]
                son_bar = bugun.iloc[-1]

                # --- EMIR DEFTERI VEKILLERI ---
                son30_baslangic = float(son.iloc[0]["open"])
                son30_getiri = ((kapanis - son30_baslangic) / son30_baslangic * 100) \
                    if son30_baslangic > 0 else None
                gun_hacim = float(bugun["volume"].sum())
                son30_hacim_pay = (float(son["volume"].sum()) / gun_hacim * 100) if gun_hacim > 0 else None
                sb_aralik = float(son_bar["high"] - son_bar["low"])
                son_bar_govde = ((float(son_bar["close"] - son_bar["open"])) / sb_aralik) if sb_aralik > 0 else None
                s_yuksek, s_dusuk = float(son["high"].max()), float(son["low"].min())
                s_aralik = s_yuksek - s_dusuk
                son30_konum = ((kapanis - s_dusuk) / s_aralik) if s_aralik > 0 else None
                yukari_bar_orani = float((son["close"].values > son["open"].values).mean() * 100)
                son3 = bugun.iloc[-3:]
                ardisik_yukari = int(all(son3["close"].values > son3["open"].values))
                tipik = (bugun["high"] + bugun["low"] + bugun["close"]) / 3
                vwap = float((tipik * bugun["volume"]).sum() / max(gun_hacim, 1e-9))
                vwap_uzaklik = ((kapanis - vwap) / vwap * 100) if vwap > 0 else None

                # --- ERTESI GUN SONUCU ---
                e_acilis = float(ertesi.iloc[0]["open"])
                if e_acilis <= 0 or abs((e_acilis - kapanis) / kapanis * 100) > 12:
                    continue
                e_kapanis = float(ertesi.iloc[-1]["close"])
                e_yuksek = float(ertesi["high"].max())

                kayitlar.append({
                    "ticker": ticker, "tarih": str(gunler[gi]), "tur": tur,
                    "getiri_pct": round(getiri, 2),
                    "son30_getiri": round(son30_getiri, 3) if son30_getiri is not None else None,
                    "son30_hacim_pay": round(son30_hacim_pay, 2) if son30_hacim_pay is not None else None,
                    "son_bar_govde": round(son_bar_govde, 3) if son_bar_govde is not None else None,
                    "son30_konum": round(son30_konum, 3) if son30_konum is not None else None,
                    "yukari_bar_orani": round(yukari_bar_orani, 1),
                    "ardisik_yukari": ardisik_yukari,
                    "vwap_uzaklik": round(vwap_uzaklik, 3) if vwap_uzaklik is not None else None,
                    "ertesi_bosluk": round((e_acilis - kapanis) / kapanis * 100, 3),
                    "ertesi_kapanis_pct": round((e_kapanis - kapanis) / kapanis * 100, 3),
                    "ertesi_zirve_pct": round((e_yuksek - kapanis) / kapanis * 100, 3),
                })
            islenen += 1
        except Exception as e:
            print(f"[Kapanış Baskısı] {ticker} hata: {e}", flush=True)
            atlanan += 1
        time.sleep(0.4)

    if not kayitlar:
        return None, "Hiç kayıt üretilemedi."
    tum = pd.DataFrame(kayitlar)
    dosya = os.path.join(DATA_DIR, "bist_kapanis_baskisi.csv")
    tum.to_csv(dosya, index=False, encoding="utf-8-sig")

    vekiller = ["son30_getiri", "son30_hacim_pay", "son_bar_govde",
                "son30_konum", "yukari_bar_orani", "ardisik_yukari", "vwap_uzaklik"]
    sonuc = {"islenen": islenen, "toplam": len(tum), "gruplar": []}
    for tur in ["TAVAN", "GUCLU"]:
        alt = tum[tum.tur == tur]
        if len(alt) < 40:
            continue
        g = {"tur": tur, "n": len(alt),
             "ort_bosluk": round(float(alt.ertesi_bosluk.mean()), 3),
             "korelasyonlar": [], "ustalt": []}
        for v in vekiller:
            a = alt[[v, "ertesi_bosluk"]].dropna()
            if len(a) < 30:
                continue
            try:
                r, p = _stats.spearmanr(a[v], a["ertesi_bosluk"])
            except Exception:
                continue
            g["korelasyonlar"].append({"vekil": v, "r": round(float(r), 4), "p": float(p)})
            # ust ceyrek vs alt ceyrek karsilastirmasi
            q1, q3 = a[v].quantile(0.25), a[v].quantile(0.75)
            ust, altc = a[a[v] >= q3], a[a[v] <= q1]
            if len(ust) >= 12 and len(altc) >= 12:
                g["ustalt"].append({
                    "vekil": v,
                    "ust_bosluk": round(float(ust.ertesi_bosluk.mean()), 3),
                    "alt_bosluk": round(float(altc.ertesi_bosluk.mean()), 3),
                    "fark": round(float(ust.ertesi_bosluk.mean() - altc.ertesi_bosluk.mean()), 3),
                    "n_ust": len(ust), "n_alt": len(altc)})
        g["korelasyonlar"].sort(key=lambda x: x["p"])
        g["ustalt"].sort(key=lambda x: -abs(x["fark"]))
        sonuc["gruplar"].append(g)
    return dosya, sonuc


def _rapor(o):
    s = [f"📖 KAPANIŞ BASKISI = EMİR DEFTERİ VEKİLİ Mİ? — {KOD_SURUMU}",
         f"İşlenen: {o['islenen']} hisse | Toplam olay: {o['toplam']}\n"]
    for g in o["gruplar"]:
        ad = "🔒 TAVAN YAPANLAR" if g["tur"] == "TAVAN" else "📈 %6-9.5 KAPATANLAR"
        s.append(f"═══ {ad} (n={g['n']}) ═══")
        s.append(f"Ortalama ertesi gün boşluğu: %{g['ort_bosluk']}\n")
        s.append("Vekil göstergelerin ertesi gün boşluğuyla İLİŞKİSİ:")
        s.append(f"{'vekil':<20}{'korelasyon':>12}{'p':>11}")
        for k in g["korelasyonlar"]:
            s.append(f"{k['vekil']:<20}{k['r']:>+12.4f}{k['p']:>11.3f}")
        if g["ustalt"]:
            s.append("\nÜST ÇEYREK vs ALT ÇEYREK (ertesi gün boşluğu):")
            for u in g["ustalt"][:5]:
                s.append(f"   {u['vekil']:<20} üst %{u['ust_bosluk']:+.3f} | "
                         f"alt %{u['alt_bosluk']:+.3f} | fark %{u['fark']:+.3f}")
        s.append("")
    s.append("⚠️ NASIL OKUNMALI:\n"
             "  p < 0.05 VE korelasyon belirginse → o vekil gerçekten bilgi "
             "taşıyor, emir defteri fikri umut verici demektir.\n"
             "  Hepsi p > 0.05 ise → kapanış davranışı ertesi günü "
             "öngörmüyor. O zaman GERÇEK emir defteri de işe yaramayabilir "
             "(ama kesin değil - vekil zayıf bir ölçüm).\n"
             "  'fark' sütunu pratik büyüklüğü gösterir: %0.5+ ise anlamlı, "
             "%0.1 civarıysa işlem maliyetine bile yetmez.")
    return "\n".join(s)


app = Flask(__name__)


@app.route("/health")
def health():
    return "OK (kapanis baskisi)", 200


def _ping():
    harici = (os.environ.get("RENDER_EXTERNAL_URL", "").rstrip("/")
              or os.environ.get("HARICI_URL", "").rstrip("/"))
    time.sleep(30)
    while True:
        try:
            if harici:
                requests.get(f"{harici}/health", timeout=20)
            else:
                requests.get(f"http://127.0.0.1:{PORT}/health", timeout=10)
        except Exception:
            pass
        time.sleep(600)


def _calis():
    time.sleep(5)
    send_telegram_message(
        f"📖 KAPANIŞ BASKISI ANALİZİ başlıyor — {KOD_SURUMU}\n\n"
        f"Emir defteri fikrinin ZAYIF VEKİLİ test ediliyor.\n\n"
        f"Gerçek emir defteri verisi geçmişe dönük ücretsiz YOK - o yüzden "
        f"kapanışa yakın dakikalardaki fiyat/hacim davranışına bakıyoruz. "
        f"Alıcı mı satıcı mı baskın, kısmen gösterir.\n\n"
        f"7 vekil gösterge: son 30dk getirisi, son 30dk hacim payı, son "
        f"barın gövdesi, kapanışın son 30dk aralığındaki konumu, yukarı "
        f"bar oranı, ardışık yükseliş, VWAP uzaklığı.\n\n"
        f"Tavan yapanlar ve %6-9.5 kapatanlar AYRI ayrı inceleniyor.\n\n"
        f"⚠️ Bu deploy'da SADECE bu analiz çalışıyor.\nBitince CSV + özet göndereceğim."
    )
    try:
        dosya, sonuc = calistir()
        if dosya is None:
            send_telegram_message(f"📖 Analiz başarısız: {sonuc}")
            return
        send_telegram_document(dosya, caption=_rapor(sonuc))
    except Exception as e:
        import traceback
        traceback.print_exc()
        send_telegram_message(f"📖 Analiz hatası: {e}")


if __name__ == "__main__":
    print(f"[BAŞLANGIÇ] bist_kapanis_baskisi.py — {KOD_SURUMU}", flush=True)
    threading.Thread(target=_calis, daemon=True).start()
    threading.Thread(target=_ping, daemon=True).start()
    app.run(host="0.0.0.0", port=PORT)
