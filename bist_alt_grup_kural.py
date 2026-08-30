"""
bist_alt_grup_kural.py — HEDEF/STOP KURALI HANGİ ALT GRUPTA ÇALIŞIYOR?
========================================================================
2026-08-28 — Test edilmemiş son boşluk.

ŞİMDİYE KADAR NE BULDUK:
  • Tavan kapanışı yapanlar → ertesi sabah +%2.51 net, %90 kazanma ✅
  • %6-9.5 kapananlar (TÜMÜ birlikte) → en iyi ayar +%0.162, başa baş ⚠️
  • En iyi ayar: +%2 hedef / -%1 stop (hedefi büyüt, stop'u daralt)

EKSİK KALAN: Hedef/stop kombinasyonlarını hep TÜM %6-9.5 grubunda
test ettik. Ama daha önce (bist_guclu_kapanis.py) bu grubun ALT
GRUPLARA ayrıldığında farklılaştığını görmüştük - sadece o zaman
sadece "açılış boşluğuna" bakmıştık, TAM KURALIN kârına değil.

Belki kural TÜM grupta başa baş ama BELİRLİ bir alt grupta gerçekten
kazandırıyor. Bu dosya onu arıyor.

ALT GRUPLAR:
  • Kapanış konumu: günün zirvesinde mi kapandı, geriledi mi
  • Hacim: ortalamanın kaç katı
  • Gün içi tavana değip geri düştü mü (talep var, kilitlenememiş)
  • Üst üste kaçıncı yükselen gün
  • Kapanış getirisi seviyesi (%6-7 / %7-8 / %8-9 / %9-9.5)

ÖNEMLİ İYİLEŞTİRME - ÖRNEKLEM: Önceki saatlik test sadece 152 olaydı
(15dk verisi 60 günle sınırlı). Bu test GÜNLÜK veriyle 2 YIL geriye
gidiyor → ~1300 olay. Aynı hassasiyette stop/hedef simülasyonu
yapılabiliyor çünkü günlük veri de high/low içeriyor.

TEMKİNLİ VARSAYIM: Gün içinde hem hedef hem stop görülmüşse "önce stop
tetiklendi" sayılıyor (kötümser). Gerçek sonuç bundan biraz iyi olabilir.

MALİYET: Midas'ta BIST komisyonu YOK (kullanıcı doğruladı). Sadece
alış-satış farkı için %0.10 düşülüyor. MALIYET_PCT ile değiştirilebilir.

Start Command:  python bist_alt_grup_kural.py
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
KOD_SURUMU = "alt-grup-kural-v1-2026-08-28"

MALIYET_PCT = float(os.environ.get("MALIYET_PCT", "0.10"))   # Midas: komisyon yok
TAVAN_ESIK = 9.5
BANT_ALT, BANT_UST = 6.0, 9.49
HEDEFLER = [1.5, 2.0, 2.5]
STOPLAR = [1.0, 1.5, 2.0, None]
MIN_ORNEK = 40   # bir alt grubun raporlanmasi icin gereken en az olay

BIST_HISSELER = [
    "THYAO.IS", "GARAN.IS", "AKBNK.IS", "ISCTR.IS", "YKBNK.IS", "VAKBN.IS",
    "HALKB.IS", "SISE.IS", "EREGL.IS", "KRDMD.IS", "TUPRS.IS", "PETKM.IS",
    "ASELS.IS", "TCELL.IS", "TTKOM.IS", "BIMAS.IS", "MGROS.IS", "SOKM.IS",
    "FROTO.IS", "TOASO.IS", "ARCLK.IS", "VESTL.IS", "TAVHL.IS", "PGSUS.IS",
    "KCHOL.IS", "SAHOL.IS", "DOHOL.IS", "ALARK.IS", "ENKAI.IS", "TKFEN.IS",
    "KOZAA.IS", "ODAS.IS", "ZOREN.IS", "AKSEN.IS", "EKGYO.IS", "ISGYO.IS",
    "TRGYO.IS", "HEKTS.IS", "SASA.IS", "GUBRF.IS", "AEFES.IS", "ULKER.IS",
    "CCOLA.IS", "TATGD.IS", "BANVT.IS", "PENTA.IS", "SMRTG.IS", "ALFAS.IS",
    "ASTOR.IS", "EUPWR.IS", "CWENE.IS", "GESAN.IS", "KONTR.IS", "ISDMR.IS",
    "CIMSA.IS", "AKCNS.IS", "OYAKC.IS", "BRSAN.IS", "AGHOL.IS", "AKFGY.IS",
    "ALBRK.IS", "ANSGR.IS", "ARDYZ.IS", "AYDEM.IS", "BERA.IS", "BIOEN.IS",
    "BRYAT.IS", "BUCIM.IS", "CANTE.IS", "CEMTS.IS", "DOAS.IS", "ECILC.IS",
    "EGEEN.IS", "ENJSA.IS", "ESEN.IS", "EUREN.IS", "GENIL.IS", "GLYHO.IS",
    "GWIND.IS", "HATSN.IS", "IEYHO.IS", "IZMDC.IS", "KARSN.IS", "KAYSE.IS",
    "KLSER.IS", "KORDS.IS", "KRDMA.IS", "MAVI.IS", "MPARK.IS", "NTHOL.IS",
    "OTKAR.IS", "PAPIL.IS", "QUAGR.IS", "SELEC.IS", "SKBNK.IS", "SNGYO.IS",
    "TMSN.IS", "TSKB.IS", "TTRAK.IS", "TURSG.IS", "ULUUN.IS", "VESBE.IS",
    "YATAS.IS", "YEOTK.IS", "YYLGD.IS", "ZRGYO.IS", "MAALT.IS", "PSDTC.IS",
    "PRZMA.IS", "AVOD.IS", "BEYAZ.IS", "BURVA.IS", "DAGI.IS", "DERIM.IS",
    "DGATE.IS", "DITAS.IS", "EGEPO.IS", "EMKEL.IS", "ERSU.IS", "FONET.IS",
    "GEDIK.IS", "INTEM.IS", "KAPLM.IS", "KRSTL.IS", "KUTPO.IS", "LINK.IS",
    "MERKO.IS", "MTRKS.IS", "NIBAS.IS", "ORCAY.IS", "PKENT.IS", "SANFM.IS",
    "SEKUR.IS", "TEKTU.IS", "ULAS.IS", "VANGD.IS", "YAYLA.IS", "MEGAP.IS",
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


def _sonuc(alis, ac, yk, dk, kp, hedef, stop):
    """Tek işlemin BRÜT getirisi (%)."""
    hf = alis * (1 + hedef / 100)
    sf = alis * (1 - stop / 100) if stop else None
    if ac >= hf:
        return (ac - alis) / alis * 100          # acilista zaten hedefin ustunde
    if sf and ac <= sf:
        return (ac - alis) / alis * 100          # gap down, acilistan stop
    hedef_gor = yk >= hf
    stop_gor = bool(sf and dk <= sf)
    if hedef_gor and stop_gor:
        return -stop                              # TEMKINLI: once stop
    if hedef_gor:
        return hedef
    if stop_gor:
        return -stop
    return (kp - alis) / alis * 100


def calistir():
    olaylar = []
    islenen, atlanan = 0, 0

    for n_i, ticker in enumerate(BIST_HISSELER, 1):
        print(f"[Alt Grup {n_i}/{len(BIST_HISSELER)}] {ticker}...", flush=True)
        ham = _veri_cek(ticker)
        if ham is None or ham.empty or len(ham) < 40:
            atlanan += 1
            time.sleep(0.35)
            continue
        try:
            df = ham.rename(columns={"Open": "open", "High": "high", "Low": "low",
                                      "Close": "close", "Volume": "volume"})
            df = df[["open", "high", "low", "close", "volume"]].dropna().reset_index(drop=True)
            if len(df) < 40:
                atlanan += 1
                time.sleep(0.35)
                continue
            df["getiri"] = df["close"].pct_change() * 100
            df["hacim_ort20"] = df["volume"].rolling(20).mean()
            ardisik, liste = 0, []
            for g in df["getiri"]:
                ardisik = ardisik + 1 if (pd.notna(g) and g > 0) else 0
                liste.append(ardisik)
            df["ardisik"] = liste

            for i in range(21, len(df) - 1):
                bugun, ertesi = df.iloc[i], df.iloc[i + 1]
                onceki_kap = df.iloc[i - 1]["close"]
                g = bugun["getiri"]
                if pd.isna(g) or not (BANT_ALT <= g < BANT_UST):
                    continue
                alis = float(bugun["close"])
                if alis <= 0 or ertesi["open"] <= 0 or onceki_kap <= 0:
                    continue
                # veri hatasi filtresi (BIST limiti +-%10)
                if abs((ertesi["open"] - alis) / alis * 100) > 12:
                    continue
                aralik = bugun["high"] - bugun["low"]
                olaylar.append({
                    "ticker": ticker, "getiri_pct": round(float(g), 2),
                    "kapanis_konumu": round(float((bugun["close"] - bugun["low"]) / aralik), 3) if aralik > 0 else None,
                    "hacim_orani": round(float(bugun["volume"] / bugun["hacim_ort20"]), 2) if bugun["hacim_ort20"] > 0 else None,
                    "tavana_degdi": int((bugun["high"] - onceki_kap) / onceki_kap * 100 >= TAVAN_ESIK),
                    "ardisik_yukselen": int(bugun["ardisik"]),
                    "alis": alis, "e_ac": float(ertesi["open"]), "e_yk": float(ertesi["high"]),
                    "e_dk": float(ertesi["low"]), "e_kp": float(ertesi["close"]),
                })
            islenen += 1
        except Exception as e:
            print(f"[Alt Grup] {ticker} hata: {e}", flush=True)
            atlanan += 1
        time.sleep(0.35)

    if not olaylar:
        return None, "Hiç olay üretilemedi."
    tum = pd.DataFrame(olaylar)

    # alt grup tanimlari
    gruplar = {
        "TÜMÜ": tum,
        "Zirvede kapandı (≥0.9)": tum[tum.kapanis_konumu >= 0.9],
        "Ortada kapandı (0.5-0.9)": tum[(tum.kapanis_konumu >= 0.5) & (tum.kapanis_konumu < 0.9)],
        "Gün içi tavana DEĞDİ": tum[tum.tavana_degdi == 1],
        "Tavana değmedi": tum[tum.tavana_degdi == 0],
        "Hacim ≥3x": tum[tum.hacim_orani >= 3],
        "Hacim 1.5-3x": tum[(tum.hacim_orani >= 1.5) & (tum.hacim_orani < 3)],
        "Hacim <1.5x": tum[tum.hacim_orani < 1.5],
        "İlk yükseliş günü": tum[tum.ardisik_yukselen == 1],
        "2+ gün üst üste": tum[tum.ardisik_yukselen >= 2],
        "Getiri %6-7": tum[(tum.getiri_pct >= 6) & (tum.getiri_pct < 7)],
        "Getiri %7-8": tum[(tum.getiri_pct >= 7) & (tum.getiri_pct < 8)],
        "Getiri %8-9.5": tum[tum.getiri_pct >= 8],
        "★ Zirvede + hacim≥2x": tum[(tum.kapanis_konumu >= 0.9) & (tum.hacim_orani >= 2)],
        "★ Zirvede + %8+": tum[(tum.kapanis_konumu >= 0.9) & (tum.getiri_pct >= 8)],
        "★ Tavana değdi + zirvede": tum[(tum.tavana_degdi == 1) & (tum.kapanis_konumu >= 0.9)],
    }

    satirlar = []
    for ad, alt in gruplar.items():
        if len(alt) < MIN_ORNEK:
            continue
        for h in HEDEFLER:
            for s in STOPLAR:
                getiriler = [_sonuc(r.alis, r.e_ac, r.e_yk, r.e_dk, r.e_kp, h, s) - MALIYET_PCT
                             for r in alt.itertuples()]
                a = np.array(getiriler)
                satirlar.append({
                    "grup": ad, "n": len(a), "hedef": h, "stop": s if s else "yok",
                    "net_ort": round(float(a.mean()), 4),
                    "kazanma": round(float((a > 0).mean() * 100), 1),
                    "medyan": round(float(np.median(a)), 3),
                    "en_kotu": round(float(a.min()), 2),
                })
    if not satirlar:
        return None, "Yeterli örnek yok."
    tablo = pd.DataFrame(satirlar).sort_values("net_ort", ascending=False)
    dosya = os.path.join(DATA_DIR, "bist_alt_grup_kural.csv")
    tablo.to_csv(dosya, index=False, encoding="utf-8-sig")
    return dosya, {"islenen": islenen, "toplam_olay": len(tum), "satirlar": satirlar}


def _rapor(o):
    s = [f"🔍 ALT GRUP × HEDEF/STOP — {KOD_SURUMU}",
         f"İşlenen: {o['islenen']} hisse | Toplam olay: {o['toplam_olay']}",
         f"Maliyet: -%{MALIYET_PCT} (Midas'ta komisyon yok, sadece spread)\n"]
    sirali = sorted(o["satirlar"], key=lambda x: -x["net_ort"])
    s.append("EN İYİ 15 KOMBİNASYON (net getiriye göre):")
    s.append(f"{'grup':<26}{'hdf':>5}{'stop':>6}{'n':>6}{'NET':>9}{'kazan':>7}")
    for x in sirali[:15]:
        s.append(f"{x['grup'][:25]:<26}{x['hedef']:>4}%{str(x['stop']):>6}{x['n']:>6}"
                 f"{x['net_ort']:>8.3f}%{x['kazanma']:>6.1f}%")
    # TUMU grubunun en iyisi - kiyas noktasi
    tumu = [x for x in sirali if x["grup"] == "TÜMÜ"]
    if tumu:
        b = tumu[0]
        s.append(f"\nKIYAS - 'TÜMÜ' grubunun en iyisi: hedef %{b['hedef']}, "
                 f"stop {b['stop']} → net %{b['net_ort']:.3f} (n={b['n']})")
    pozitif = [x for x in sirali if x["net_ort"] > 0]
    s.append(f"\nPozitif net veren kombinasyon: {len(pozitif)}/{len(sirali)}")
    s.append("\n⚠️ NASIL OKUNMALI:\n"
             "  Bir alt grup 'TÜMÜ'den BELİRGİN iyiyse ve n yeterince "
             "büyükse (100+), o filtre gerçek olabilir.\n"
             "  Ama dikkat: çok sayıda kombinasyon denendi. En üstteki "
             "birkaçı ŞANS eseri iyi çıkmış olabilir - özellikle n<100 ise.\n"
             "  Güvenmek için: hem yüksek n, hem 'TÜMÜ'den net üstünlük, "
             "hem de mantıklı bir gerekçe olmalı.")
    return "\n".join(s)


app = Flask(__name__)


@app.route("/health")
def health():
    return "OK (alt grup kural)", 200


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
        f"🔍 ALT GRUP ANALİZİ başlıyor — {KOD_SURUMU}\n\n"
        f"Test edilmemiş son boşluk: hedef/stop kuralını hep TÜM %6-9.5 "
        f"grubunda denedik. Belki BELİRLİ bir alt grupta gerçekten "
        f"kazandırıyor.\n\n"
        f"16 alt grup × 12 hedef/stop kombinasyonu deneniyor.\n"
        f"Örneklem büyütüldü: 2 yıllık GÜNLÜK veri (~1300 olay), önceki "
        f"saatlik testteki 152'ye göre çok daha güvenilir.\n"
        f"Maliyet %{MALIYET_PCT} (Midas'ta komisyon yok - sadece spread).\n\n"
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
    print(f"[BAŞLANGIÇ] bist_alt_grup_kural.py — {KOD_SURUMU}", flush=True)
    threading.Thread(target=_calis, daemon=True).start()
    threading.Thread(target=_ping, daemon=True).start()
    app.run(host="0.0.0.0", port=PORT)
