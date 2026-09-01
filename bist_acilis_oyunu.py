"""
bist_acilis_oyunu.py — AÇILIŞTA "ÖNCE TERS YÖNE" ÖRÜNTÜSÜ GERÇEK Mİ?
======================================================================
2026-08-31 — Kullanıcının gözlemi:
  "Sabah piyasa açıldıktan hemen sonra, yükselecek hisseler önce hafif
   -%0.50/-%1.00 düşüyor sonra yükselişe geçiyor. Düşecek hisselerde de
   aynı: önce hafif yükseliş, sonra düşüş. Ama bazen gerçekten birden
   yükseliyor/düşüyor. Bu gerçekten oluyor mu yoksa bana mı öyle geliyor?"

Bu, borsada "shakeout" / "açılış tuzağı" diye bilinen bir örüntünün
tarifi. GERÇEKTEN var mı, yoksa seçici hafıza mı - test edilebilir.

YÖNTEM (5 DAKİKALIK veri, ilk 1 saat):
  1. Her hisse-günü için açılıştan sonraki İLK SAAT takip ediliyor
     (5dk barlarla → 12 nokta, 15dk'lık veriden çok daha ince)
  2. Saatin SONUNDAKİ yöne göre etiketleniyor:
        YÜKSELDİ  : 1. saat sonunda açılışa göre >= +%1
        DÜŞTÜ     : <= -%1
        YATAY     : arada
  3. Sonra GERİYE bakılıyor: o yöne gitmeden ÖNCE ters yöne gitti mi?
        YÜKSELENLER için: önce açılışın ALTINA düştü mü, ne kadar,
                          en dip kaçıncı dakikada?
        DÜŞENLER için   : önce açılışın ÜSTÜNE çıktı mı, ne kadar,
                          en tepe kaçıncı dakikada?

İKİ AYRI EVREN:
  A) TÜM hisse-günleri (genel örüntü var mı)
  B) Önceki gün %6-9.5 kapatanlar (bizim sinyal grubumuz - asıl
     ilgilendiğimiz, çünkü onlara işlem açıyorsun)

★ PRATİK ÇIKTI: Eğer örüntü gerçekse, "açılışta hemen alma, 15-30 dk
bekle" demek daha iyi giriş fiyatı demektir. Test bunu da ölçüyor:
açılışta alsan vs 15/30 dk bekleyip alsan, giriş fiyatın ne kadar
farklı olurdu.

⚠️ DÜRÜST SINIR: Örüntü çıksa bile, o an düşüşün "tuzak mı gerçek mi"
olduğunu BİLEMEZSİN. Bu test "örüntü var mı" sorusunu cevaplar,
"kâr eder mi" sorusunu değil. İkisini karıştırmayalım.

Start Command:  python bist_acilis_oyunu.py
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
KOD_SURUMU = "acilis-oyunu-v1-2026-08-31"

YON_ESIK = 1.0          # 1. saat sonunda +-%1 -> yon belirlendi
ILK_SAAT_BAR = 12       # 5dk x 12 = 1 saat
TERS_ESIK = 0.15        # bu kadar ters harekete "ters yone gitti" denir

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
        # 5 DAKIKALIK veri - ilk saatte 12 nokta (15dk'da sadece 4 olurdu)
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
        print(f"[Açılış {n_i}/{len(BIST_HISSELER)}] {ticker}...", flush=True)
        ham = _veri_cek(ticker)
        if ham is None or ham.empty or len(ham) < 100:
            atlanan += 1
            time.sleep(0.4)
            continue
        try:
            df = ham.rename(columns={"Open": "open", "High": "high", "Low": "low",
                                      "Close": "close", "Volume": "volume"})
            df = df[["open", "high", "low", "close", "volume"]].dropna()
            if len(df) < 100:
                atlanan += 1
                time.sleep(0.4)
                continue
            idx = pd.to_datetime(df.index)
            try:
                idx = idx.tz_localize(None)
            except (TypeError, AttributeError):
                pass
            df.index = idx
            df["gun"] = df.index.date

            gunler = sorted(set(df["gun"]))
            for gi in range(1, len(gunler)):
                dun = df[df["gun"] == gunler[gi - 1]]
                bugun = df[df["gun"] == gunler[gi]]
                if dun.empty or len(bugun) < ILK_SAAT_BAR:
                    continue
                dun_kap = float(dun.iloc[-1]["close"])
                if dun_kap <= 0:
                    continue
                # onceki gunun getirisi (sinyal grubunu ayirmak icin)
                dun_ilk = float(dun.iloc[0]["open"])
                dun_onceki = df[df["gun"] == gunler[gi - 2]] if gi >= 2 else None
                dun_getiri = None
                if dun_onceki is not None and not dun_onceki.empty:
                    ref = float(dun_onceki.iloc[-1]["close"])
                    if ref > 0:
                        dun_getiri = (dun_kap - ref) / ref * 100

                ilk_saat = bugun.iloc[:ILK_SAAT_BAR]
                acilis = float(ilk_saat.iloc[0]["open"])
                if acilis <= 0:
                    continue
                saat_sonu = float(ilk_saat.iloc[-1]["close"])
                yon_getiri = (saat_sonu - acilis) / acilis * 100
                if yon_getiri >= YON_ESIK:
                    yon = "YÜKSELDİ"
                elif yon_getiri <= -YON_ESIK:
                    yon = "DÜŞTÜ"
                else:
                    yon = "YATAY"

                # ters yone gitti mi? (acilis fiyatina gore)
                dip = float(ilk_saat["low"].min())
                tepe = float(ilk_saat["high"].max())
                dip_pct = (dip - acilis) / acilis * 100
                tepe_pct = (tepe - acilis) / acilis * 100
                dip_bar = int(np.argmin(ilk_saat["low"].values))
                tepe_bar = int(np.argmax(ilk_saat["high"].values))

                # ilk 15dk (3 bar) ve 30dk (6 bar) icindeki uc noktalar
                ilk15 = ilk_saat.iloc[:3]
                ilk30 = ilk_saat.iloc[:6]
                kayitlar.append({
                    "ticker": ticker, "tarih": str(gunler[gi]),
                    "dun_getiri_pct": round(dun_getiri, 2) if dun_getiri is not None else None,
                    "acilis_boslugu_pct": round((acilis - dun_kap) / dun_kap * 100, 2),
                    "yon": yon, "saat_sonu_pct": round(yon_getiri, 2),
                    "dip_pct": round(dip_pct, 3), "dip_dakika": dip_bar * 5,
                    "tepe_pct": round(tepe_pct, 3), "tepe_dakika": tepe_bar * 5,
                    "ilk15_dip_pct": round((float(ilk15["low"].min()) - acilis) / acilis * 100, 3),
                    "ilk15_tepe_pct": round((float(ilk15["high"].max()) - acilis) / acilis * 100, 3),
                    "ilk30_dip_pct": round((float(ilk30["low"].min()) - acilis) / acilis * 100, 3),
                    "ilk30_tepe_pct": round((float(ilk30["high"].max()) - acilis) / acilis * 100, 3),
                    "fiyat_15dk": round((float(ilk_saat.iloc[2]["close"]) - acilis) / acilis * 100, 3),
                    "fiyat_30dk": round((float(ilk_saat.iloc[5]["close"]) - acilis) / acilis * 100, 3),
                })
            islenen += 1
        except Exception as e:
            print(f"[Açılış] {ticker} hata: {e}", flush=True)
            atlanan += 1
        time.sleep(0.4)

    if not kayitlar:
        return None, "Hiç kayıt üretilemedi."
    tum = pd.DataFrame(kayitlar)
    dosya = os.path.join(DATA_DIR, "bist_acilis_oyunu.csv")
    tum.to_csv(dosya, index=False, encoding="utf-8-sig")

    def _analiz(veri, ad):
        yuk = veri[veri.yon == "YÜKSELDİ"]
        dus = veri[veri.yon == "DÜŞTÜ"]
        if len(yuk) < 30 or len(dus) < 30:
            return None
        # YUKSELENLER: once acilis ALTINA dustu mu
        y_ters = yuk[yuk.dip_pct <= -TERS_ESIK]
        # DUSENLER: once acilis USTUNE cikti mi
        d_ters = dus[dus.tepe_pct >= TERS_ESIK]
        return {
            "ad": ad, "n": len(veri),
            "yukselen_n": len(yuk), "dusen_n": len(dus),
            "yatay_n": int((veri.yon == "YATAY").sum()),
            # yukselenlerde once dusus
            "y_ters_oran": round(float(len(y_ters) / len(yuk) * 100), 1),
            "y_ort_dip": round(float(yuk.dip_pct.mean()), 3),
            "y_medyan_dip": round(float(yuk.dip_pct.median()), 3),
            "y_dip_dakika": round(float(y_ters.dip_dakika.median()), 1) if len(y_ters) else None,
            "y_dip_1den_derin": round(float((yuk.dip_pct <= -1.0).mean() * 100), 1),
            # dusenlerde once yukselis
            "d_ters_oran": round(float(len(d_ters) / len(dus) * 100), 1),
            "d_ort_tepe": round(float(dus.tepe_pct.mean()), 3),
            "d_medyan_tepe": round(float(dus.tepe_pct.median()), 3),
            "d_tepe_dakika": round(float(d_ters.tepe_dakika.median()), 1) if len(d_ters) else None,
            # PRATIK: acilista mi alsan, 15/30 dk beklesen mi
            "y_giris_acilis": 0.0,
            "y_giris_15dk": round(float(yuk.fiyat_15dk.mean()), 3),
            "y_giris_30dk": round(float(yuk.fiyat_30dk.mean()), 3),
            "y_giris_ilk15_dip": round(float(yuk.ilk15_dip_pct.mean()), 3),
        }

    sonuc = {"islenen": islenen, "toplam": len(tum), "gruplar": []}
    a = _analiz(tum, "A) TÜM hisse-günleri")
    if a:
        sonuc["gruplar"].append(a)
    sinyal = tum[(tum.dun_getiri_pct >= 6.0) & (tum.dun_getiri_pct < 9.49)]
    b = _analiz(sinyal, "B) Önceki gün %6-9.5 kapatanlar (sinyal grubu)")
    if b:
        sonuc["gruplar"].append(b)
    tavan = tum[tum.dun_getiri_pct >= 9.5]
    c = _analiz(tavan, "C) Önceki gün TAVAN yapanlar")
    if c:
        sonuc["gruplar"].append(c)
    return dosya, sonuc


def _rapor(o):
    s = [f"🎭 AÇILIŞTA 'ÖNCE TERS YÖNE' ÖRÜNTÜSÜ — {KOD_SURUMU}",
         f"İşlenen: {o['islenen']} hisse | Toplam hisse-günü: {o['toplam']}",
         f"5dk verisiyle ilk 1 saat incelendi\n"]
    for g in o["gruplar"]:
        s.append(f"═══ {g['ad']} (n={g['n']}) ═══")
        s.append(f"Yükselen: {g['yukselen_n']} | Düşen: {g['dusen_n']} | Yatay: {g['yatay_n']}\n")
        s.append(f"📈 YÜKSELENLERDE (1. saat sonu ≥+%{YON_ESIK}):")
        s.append(f"   Önce açılışın ALTINA düşenler: %{g['y_ters_oran']}")
        s.append(f"   Ortalama dip: %{g['y_ort_dip']} | medyan: %{g['y_medyan_dip']}")
        if g["y_dip_dakika"] is not None:
            s.append(f"   Dip genelde {g['y_dip_dakika']:.0f}. dakikada")
        s.append(f"   %1'den derin düşenler: %{g['y_dip_1den_derin']}")
        s.append(f"\n📉 DÜŞENLERDE (1. saat sonu ≤-%{YON_ESIK}):")
        s.append(f"   Önce açılışın ÜSTÜNE çıkanlar: %{g['d_ters_oran']}")
        s.append(f"   Ortalama tepe: %{g['d_ort_tepe']} | medyan: %{g['d_medyan_tepe']}")
        if g["d_tepe_dakika"] is not None:
            s.append(f"   Tepe genelde {g['d_tepe_dakika']:.0f}. dakikada")
        s.append(f"\n💰 GİRİŞ FİYATI (yükselenlerde, açılışa göre):")
        s.append(f"   Açılışta alsan:        %0.000 (referans)")
        s.append(f"   15 dk bekleyip alsan:  %{g['y_giris_15dk']:+.3f}")
        s.append(f"   30 dk bekleyip alsan:  %{g['y_giris_30dk']:+.3f}")
        s.append(f"   İlk 15dk dibinden:     %{g['y_giris_ilk15_dip']:+.3f}")
        s.append("")
    s.append("⚠️ NASIL OKUNMALI:\n"
             "  'Önce ters yöne gidenler' oranı YÜKSEKSE (%70+) → gözlemin "
             "doğru, örüntü gerçek.\n"
             "  %50 civarıysa → yazı-tura, örüntü yok, seçici hafıza.\n"
             "  'Giriş fiyatı' negatifse → beklemek daha ucuza almanı sağlar.\n"
             "  AMA: o an düşüşün tuzak mı gerçek mi olduğunu BİLEMEZSİN. "
             "Bu test örüntüyü ölçer, kâr garantisi vermez.")
    return "\n".join(s)


app = Flask(__name__)


@app.route("/health")
def health():
    return "OK (acilis oyunu)", 200


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
        f"🎭 AÇILIŞ ÖRÜNTÜSÜ TESTİ başlıyor — {KOD_SURUMU}\n\n"
        f"Senin gözlemin: 'yükselecek hisseler önce hafif düşüyor, "
        f"düşecekler önce hafif yükseliyor.'\n\n"
        f"Gerçekten oluyor mu, yoksa seçici hafıza mı - ölçülüyor.\n"
        f"5 DAKİKALIK veri (15dk'lık yerine - ilk saatte 12 nokta), "
        f"{len(BIST_HISSELER)} hisse × 60 gün.\n\n"
        f"Üç evrende ayrı ayrı: tüm günler, önceki gün %6-9.5 "
        f"kapatanlar (sinyal grubu), önceki gün tavan yapanlar.\n\n"
        f"Ayrıca ölçülecek: açılışta mı almalı, 15-30 dk beklemeli mi.\n\n"
        f"⚠️ Bu deploy'da SADECE bu analiz çalışıyor.\nBitince CSV + özet göndereceğim."
    )
    try:
        dosya, sonuc = calistir()
        if dosya is None:
            send_telegram_message(f"🎭 Analiz başarısız: {sonuc}")
            return
        send_telegram_document(dosya, caption=_rapor(sonuc))
    except Exception as e:
        import traceback
        traceback.print_exc()
        send_telegram_message(f"🎭 Analiz hatası: {e}")


if __name__ == "__main__":
    print(f"[BAŞLANGIÇ] bist_acilis_oyunu.py — {KOD_SURUMU}", flush=True)
    threading.Thread(target=_calis, daemon=True).start()
    threading.Thread(target=_ping, daemon=True).start()
    app.run(host="0.0.0.0", port=PORT)
