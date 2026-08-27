"""
bist_tavana_yakin.py — TAVANA YAKIN KAPANANLAR DA ERTESİ GÜN YUKARI AÇIYOR MU?
================================================================================
2026-08-19 — bist_tavan.py şunu bulmuştu:
    Tavan (>= %9.5) yapan hisse ertesi gün ort. +%2.47 YUKARI açıyor
    (%81 ihtimalle), ama açıldıktan sonra ort. -%1.05 düşüyor.
    → "Açılışta sat" mantıklı görünüyor.

AMA PRATİK SORUN: Tavan olmuş hissede ALIŞ tarafına girmek çok zor -
tavan kilitlenince kimse satmıyor, alış sırasında bekliyorsun. Yani
"tavan kapanışında al" stratejisi kâğıt üstünde kalıyor.

BU DOSYANIN SORDUĞU SORU:
Tavan OLMAYAN ama TAVANA YAKIN kapananlar (%6, %7, %8, %9 gibi) da
ertesi gün yukarı açıyor mu? Çünkü onlarda alım YAPILABİLİR - tavan
kilidi yok, satıcı var.

Eğer %8'de kapayanlar da +%2 gibi açıyorsa → uygulanabilir bir sistem.
Eğer sadece TAM tavan yapanlar açıyorsa → giriş sorunu çözülmüyor,
dürüstçe kabul edip bırakırız.

YÖNTEM: günlük getiriyi dilimlere ayırıp her dilim için ertesi gün
ölçümlerini karşılaştırıyoruz:
    %0-2, %2-4, %4-6, %6-8, %8-9.5 (tavana yakın), >=%9.5 (TAM TAVAN)
Böylece "yukarı açma" etkisinin getiriyle nasıl arttığını GÖRÜYORUZ -
tek bir eşiğe bakmak yerine tüm eğriyi çıkarıyoruz.

AYRICA ölçülüyor:
  - Her dilimde açılış boşluğu, açılış sonrası hareket, toplam
  - Hacim etkisi: yüksek hacimle mi kapandı (ilgi göstergesi)
  - "Kapanış, günün zirvesine ne kadar yakın" (güçlü kapanış mı)

Start Command:  python bist_tavana_yakin.py
Bu deploy'da SADECE bu analiz çalışır.
"""
import os
import time
import threading

import numpy as np
import pandas as pd
import requests
from flask import Flask

TELEGRAM_TOKEN = os.environ.get("ARGE_TELEGRAM_TOKEN", "")
TELEGRAM_CHAT_ID = os.environ.get("ARGE_TELEGRAM_CHAT_ID", "")
DATA_DIR = os.environ.get("DATA_DIR", ".")
PORT = int(os.environ.get("PORT", "10000"))
KOD_SURUMU = "bist-tavana-yakin-v1-2026-08-19"

# getiri dilimleri: (alt, ust, etiket)
DILIMLER = [
    (-100.0, 0.0, "negatif"),
    (0.0, 2.0, "%0-2"),
    (2.0, 4.0, "%2-4"),
    (4.0, 6.0, "%4-6"),
    (6.0, 8.0, "%6-8"),
    (8.0, 9.5, "%8-9.5 (tavana yakın)"),
    (9.5, 100.0, "TAM TAVAN (>=%9.5)"),
]

BIST_HISSELER = [
    "THYAO.IS", "GARAN.IS", "AKBNK.IS", "ISCTR.IS", "YKBNK.IS", "VAKBN.IS",
    "HALKB.IS", "SISE.IS", "EREGL.IS", "KRDMD.IS", "TUPRS.IS", "PETKM.IS",
    "ASELS.IS", "TCELL.IS", "TTKOM.IS", "BIMAS.IS", "MGROS.IS", "SOKM.IS",
    "FROTO.IS", "TOASO.IS", "ARCLK.IS", "VESTL.IS", "TAVHL.IS", "PGSUS.IS",
    "KCHOL.IS", "SAHOL.IS", "DOHOL.IS", "ALARK.IS", "ENKAI.IS", "TKFEN.IS",
    "KOZAL.IS", "KOZAA.IS", "IPEKE.IS", "ODAS.IS", "ZOREN.IS", "AKSEN.IS",
    "EKGYO.IS", "ISGYO.IS", "TRGYO.IS", "HEKTS.IS", "SASA.IS", "GUBRF.IS",
    "AEFES.IS", "ULKER.IS", "CCOLA.IS", "TATGD.IS", "BANVT.IS", "PENTA.IS",
    "SMRTG.IS", "ALFAS.IS", "ASTOR.IS", "EUPWR.IS", "CWENE.IS", "GESAN.IS",
    "KONTR.IS", "ISDMR.IS", "CIMSA.IS", "AKCNS.IS", "OYAKC.IS", "BRSAN.IS",
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


def _veri_cek(ticker, sert_sure=30):
    import concurrent.futures
    import yfinance as yf

    def _cek():
        return yf.Ticker(ticker).history(period="2y", interval="1d", timeout=20)

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


def _dilim(getiri):
    for alt, ust, etiket in DILIMLER:
        if alt <= getiri < ust:
            return etiket
    return None


def calistir():
    kayitlar = []
    islenen = atlanan = 0

    for n_i, ticker in enumerate(BIST_HISSELER, 1):
        print(f"[Tavana Yakın {n_i}/{len(BIST_HISSELER)}] {ticker}...", flush=True)
        ham = _veri_cek(ticker)
        if ham is None or ham.empty or len(ham) < 60:
            atlanan += 1
            time.sleep(0.4)
            continue
        try:
            df = ham.rename(columns={"Open": "open", "High": "high", "Low": "low",
                                      "Close": "close", "Volume": "volume"})
            df = df[["open", "high", "low", "close", "volume"]].dropna()
            if len(df) < 60:
                atlanan += 1
                time.sleep(0.4)
                continue
            df.index = pd.to_datetime(df.index).tz_localize(None)
            df = df.reset_index()
            df = df.rename(columns={df.columns[0]: "tarih"})

            df["gunluk_getiri"] = df["close"].pct_change() * 100
            df["hacim_ort20"] = df["volume"].rolling(20).mean()

            for i in range(20, len(df) - 1):
                bugun = df.iloc[i]
                ertesi = df.iloc[i + 1]
                if (bugun["close"] <= 0 or ertesi["open"] <= 0
                        or pd.isna(bugun["gunluk_getiri"])):
                    continue
                dilim = _dilim(float(bugun["gunluk_getiri"]))
                if dilim is None:
                    continue
                gun_aralik = bugun["high"] - bugun["low"]
                kapanis_zirve_orani = ((bugun["close"] - bugun["low"]) / gun_aralik) if gun_aralik > 0 else None
                hacim_orani = (bugun["volume"] / bugun["hacim_ort20"]) if bugun["hacim_ort20"] and bugun["hacim_ort20"] > 0 else None

                kayitlar.append({
                    "ticker": ticker, "tarih": str(bugun["tarih"].date()),
                    "dilim": dilim,
                    "o_gun_getiri_pct": round(float(bugun["gunluk_getiri"]), 2),
                    "hacim_orani": round(float(hacim_orani), 2) if hacim_orani else None,
                    "kapanis_zirve_orani": round(float(kapanis_zirve_orani), 2) if kapanis_zirve_orani is not None else None,
                    "acilis_boslugu_pct": round(float((ertesi["open"] - bugun["close"]) / bugun["close"] * 100), 2),
                    "acilis_sonrasi_pct": round(float((ertesi["close"] - ertesi["open"]) / ertesi["open"] * 100), 2),
                    "acilis_zirve_pct": round(float((ertesi["high"] - ertesi["open"]) / ertesi["open"] * 100), 2),
                    "toplam_ertesi_gun_pct": round(float((ertesi["close"] - bugun["close"]) / bugun["close"] * 100), 2),
                })
            islenen += 1
        except Exception as e:
            print(f"[Tavana Yakın] {ticker} hata: {e}", flush=True)
            atlanan += 1
        time.sleep(0.4)

    if not kayitlar:
        return None, "Hiç kayıt üretilemedi."
    tum = pd.DataFrame(kayitlar)
    dosya = os.path.join(DATA_DIR, "bist_tavana_yakin.csv")
    tum.to_csv(dosya, index=False, encoding="utf-8-sig")

    satirlar = []
    for _, _, etiket in DILIMLER:
        alt = tum[tum.dilim == etiket]
        if len(alt) < 10:
            continue
        satirlar.append({
            "dilim": etiket, "n": len(alt),
            "bosluk_ort": round(float(alt.acilis_boslugu_pct.mean()), 3),
            "bosluk_medyan": round(float(alt.acilis_boslugu_pct.median()), 3),
            "yukari_acma_orani_pct": round(float((alt.acilis_boslugu_pct > 0).mean() * 100), 1),
            "acilis_sonrasi_ort": round(float(alt.acilis_sonrasi_pct.mean()), 3),
            "toplam_ort": round(float(alt.toplam_ertesi_gun_pct.mean()), 3),
        })

    # tavana yakin dilimde hacim/kapanis gucu etkisi
    yakin = tum[tum.dilim.isin(["%6-8", "%8-9.5 (tavana yakın)"])].dropna(subset=["hacim_orani"])
    ekstra = {}
    if len(yakin) >= 40:
        yuksek = yakin[yakin.hacim_orani >= yakin.hacim_orani.median()]
        dusuk = yakin[yakin.hacim_orani < yakin.hacim_orani.median()]
        ekstra["hacim"] = {
            "yuksek_hacim_bosluk": round(float(yuksek.acilis_boslugu_pct.mean()), 3),
            "yuksek_n": len(yuksek),
            "dusuk_hacim_bosluk": round(float(dusuk.acilis_boslugu_pct.mean()), 3),
            "dusuk_n": len(dusuk),
        }
    yakin2 = tum[tum.dilim.isin(["%6-8", "%8-9.5 (tavana yakın)"])].dropna(subset=["kapanis_zirve_orani"])
    if len(yakin2) >= 40:
        guclu = yakin2[yakin2.kapanis_zirve_orani >= 0.9]
        zayif = yakin2[yakin2.kapanis_zirve_orani < 0.9]
        if len(guclu) >= 15 and len(zayif) >= 15:
            ekstra["kapanis_gucu"] = {
                "zirvede_kapanis_bosluk": round(float(guclu.acilis_boslugu_pct.mean()), 3),
                "zirvede_n": len(guclu),
                "zayif_kapanis_bosluk": round(float(zayif.acilis_boslugu_pct.mean()), 3),
                "zayif_n": len(zayif),
            }
    return dosya, {"islenen": islenen, "atlanan": atlanan, "toplam_kayit": len(tum),
                    "dilimler": satirlar, "ekstra": ekstra}


def _rapor(o):
    s = [f"📊 TAVANA YAKIN KAPANANLAR — {KOD_SURUMU}",
         f"İşlenen hisse: {o['islenen']} | Atlanan: {o['atlanan']} | Kayıt: {o['toplam_kayit']}\n",
         "GÜNLÜK GETİRİ DİLİMİNE GÖRE ERTESİ GÜN:",
         f"{'dilim':<24}{'n':>6}{'boşluk':>9}{'yukarı%':>9}{'açılış sonrası':>16}{'toplam':>9}"]
    for d in o["dilimler"]:
        s.append(f"{d['dilim']:<24}{d['n']:>6}{d['bosluk_ort']:>9.2f}"
                 f"{d['yukari_acma_orani_pct']:>9.1f}{d['acilis_sonrasi_ort']:>16.2f}{d['toplam_ort']:>9.2f}")
    e = o.get("ekstra", {})
    if e.get("hacim"):
        h = e["hacim"]
        s.append(f"\n%6-9.5 diliminde HACİM etkisi:")
        s.append(f"  Yüksek hacim (n={h['yuksek_n']}): boşluk %{h['yuksek_hacim_bosluk']}")
        s.append(f"  Düşük hacim  (n={h['dusuk_n']}): boşluk %{h['dusuk_hacim_bosluk']}")
    if e.get("kapanis_gucu"):
        k = e["kapanis_gucu"]
        s.append(f"\n%6-9.5 diliminde KAPANIŞ GÜCÜ etkisi:")
        s.append(f"  Zirveye yakın kapanış (n={k['zirvede_n']}): boşluk %{k['zirvede_kapanis_bosluk']}")
        s.append(f"  Zayıf kapanış (n={k['zayif_n']}): boşluk %{k['zayif_kapanis_bosluk']}")
    s.append("\n⚠️ ASIL SORU: '%6-8' ve '%8-9.5' dilimlerinin boşluğu, TAM "
             "TAVAN'a yakın mı? Yakınsa → alım YAPILABİLİR bir seviyede aynı "
             "etkiyi yakalıyoruz, uygulanabilir sistem var. Çok düşükse → "
             "etki sadece tavan kilidine özgü, giriş sorunu çözülmüyor.")
    return "\n".join(s)


app = Flask(__name__)


@app.route("/health")
def health():
    return "OK (bist tavana yakin)", 200


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
        f"📊 TAVANA YAKIN ANALİZİ başlıyor — {KOD_SURUMU}\n\n"
        f"Soru: tavan OLMAYAN ama tavana yakın kapananlar (%6-8, %8-9.5) "
        f"da ertesi gün yukarı açıyor mu? Çünkü onlarda ALIM YAPILABİLİR "
        f"(tavan kilidi yok).\n\n"
        f"{len(BIST_HISSELER)} BIST hissesi, son 2 yıl, 7 getiri dilimi "
        f"karşılaştırmalı. Ayrıca hacim ve kapanış gücü etkisi.\n\n"
        f"⚠️ Bu deploy'da SADECE bu analiz çalışıyor.\n"
        f"Bitince CSV + özet göndereceğim."
    )
    try:
        dosya, sonuc = calistir()
        if dosya is None:
            send_telegram_message(f"📊 Analiz başarısız: {sonuc}")
            return
        send_telegram_document(dosya, caption=_rapor(sonuc))
    except Exception as e:
        import traceback
        traceback.print_exc()
        send_telegram_message(f"📊 Analiz hatası: {e}")


if __name__ == "__main__":
    print(f"[BAŞLANGIÇ] bist_tavana_yakin.py — {KOD_SURUMU}", flush=True)
    threading.Thread(target=_calis, daemon=True).start()
    threading.Thread(target=_ping, daemon=True).start()
    app.run(host="0.0.0.0", port=PORT)
