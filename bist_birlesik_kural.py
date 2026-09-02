"""
bist_birlesik_kural.py — AÇILIŞ DİBİNDEN GİRİŞ + ÇÖKME FİLTRELERİ BİRLİKTE
============================================================================
2026-08-31 — İki ayrı bulguyu İLK KEZ birleştiren test.

BULGU 1 (bist_cokenleri_ayirt_et.py, 1443 olay) — ÇÖKME FİLTRELERİ:
    Taban çökme oranı %36.9. Ama:
      Son 5 günde ≥%25 koşmuş     → çökme %62.7  (KAÇIN)
      Gün aralığı geniş (≥%9)     → çökme %50.7  (KAÇIN)
      Gün içi tavana değip düşmüş → çökme %48.3  (KAÇIN)
      Gün aralığı DAR (<%5)       → çökme %18.8  (ARA)
      Zirvede kapanış + dar aralık→ çökme %19.6, patlama %35.1 (EN İYİ)

BULGU 2 (bist_acilis_oyunu.py, 5dk veri) — AÇILIŞ ÖRÜNTÜSÜ:
    %6-9.5 kapatanlarda, ertesi gün YÜKSELENLERİN %71.4'ü önce
    açılışın ALTINA düşüyor (ortalama -%0.71), dip genelde 5. dakikada.
    Açılışta almak yerine ilk 15dk dibinden alabilsen %0.65 daha ucuza
    girerdin.

BU DOSYA İKİSİNİ BİRLEŞTİRİYOR — hiç test etmediğimiz kombinasyon:
    "Çökme filtrelerini geçen hisselerde, açılış dibinden girsen
     ne olurdu?"

ÜÇ GİRİŞ YÖNTEMİ KARŞILAŞTIRILIYOR:
    A) Önceki gün KAPANIŞTA al (şu ana kadar hep bunu test ettik)
    B) Ertesi gün AÇILIŞTA al
    C) Ertesi gün açılıştan sonraki İLK 15 DK'daki dipten al
       (gerçekçi versiyon: açılışın %0.5 altına limit emri koy;
        emir dolmazsa o gün İŞLEM YOK - bu dürüst bir varsayım,
        çünkü her gün dip olmuyor)

Her yöntem × her filtre × hedef/stop kombinasyonu deneniyor.

⚠️ VERİ SINIRI: 5dk verisi 60 gün geriye gidiyor. Örneklem önceki
günlük testlere göre KÜÇÜK olacak (~150-200 olay). Kesin sonuç değil,
yön gösterir. Bunu okurken aklında tut.

Start Command:  python bist_birlesik_kural.py
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
KOD_SURUMU = "birlesik-kural-v1-2026-08-31"

MALIYET_PCT = float(os.environ.get("MALIYET_PCT", "0.10"))  # Midas: komisyon yok
BANT_ALT, BANT_UST = 6.0, 9.49
TAVAN_ESIK = 9.5
LIMIT_INDIRIM = 0.5      # acilisin %0.5 altina limit emri
ILK_PENCERE_BAR = 3      # 5dk x 3 = ilk 15 dk

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


def _veri_cek(ticker, interval, period, sert_sure=40):
    import concurrent.futures
    import yfinance as yf

    def _cek():
        return yf.Ticker(ticker).history(period=period, interval=interval, timeout=25)

    ex = concurrent.futures.ThreadPoolExecutor(max_workers=1)
    try:
        return ex.submit(_cek).result(timeout=sert_sure)
    except concurrent.futures.TimeoutError:
        print(f"[SERT zaman aşımı] {ticker} ({interval})", flush=True)
        return None
    except Exception as e:
        print(f"[Veri hatası] {ticker}: {e}", flush=True)
        return None
    finally:
        ex.shutdown(wait=False)


def _cikis(alis, gun_yuksek, gun_dusuk, gun_kapanis, hedef, stop):
    """Hedef/stop ile çıkış. TEMKİNLİ: ikisi de görülmüşse stop önce."""
    hf = alis * (1 + hedef / 100)
    sf = alis * (1 - stop / 100) if stop else None
    hg = gun_yuksek >= hf
    sg = bool(sf and gun_dusuk <= sf)
    if hg and sg:
        return -stop
    if hg:
        return hedef
    if sg:
        return -stop
    return (gun_kapanis - alis) / alis * 100


def calistir():
    olaylar = []
    islenen, atlanan = 0, 0

    for n_i, ticker in enumerate(BIST_HISSELER, 1):
        print(f"[Birleşik {n_i}/{len(BIST_HISSELER)}] {ticker}...", flush=True)
        b5 = _veri_cek(ticker, "5m", "60d")
        if b5 is None or b5.empty or len(b5) < 100:
            atlanan += 1
            time.sleep(0.4)
            continue
        try:
            df = b5.rename(columns={"Open": "open", "High": "high", "Low": "low",
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

            for gi in range(2, len(gunler) - 1):
                onceki = df[df["gun"] == gunler[gi - 1]]
                bugun = df[df["gun"] == gunler[gi]]      # sinyal gunu
                ertesi = df[df["gun"] == gunler[gi + 1]]
                if onceki.empty or len(bugun) < 10 or len(ertesi) < ILK_PENCERE_BAR + 2:
                    continue
                onceki_kap = float(onceki.iloc[-1]["close"])
                kapanis = float(bugun.iloc[-1]["close"])
                if onceki_kap <= 0 or kapanis <= 0:
                    continue
                getiri = (kapanis - onceki_kap) / onceki_kap * 100
                if not (BANT_ALT <= getiri < BANT_UST):
                    continue

                # --- SINYAL GUNU OZELLIKLERI (filtre icin) ---
                gun_yuksek = float(bugun["high"].max())
                gun_dusuk = float(bugun["low"].min())
                aralik = gun_yuksek - gun_dusuk
                kapanis_konumu = (kapanis - gun_dusuk) / aralik if aralik > 0 else None
                gun_araligi_pct = aralik / kapanis * 100 if kapanis > 0 else None
                tavana_degdi = int((gun_yuksek - onceki_kap) / onceki_kap * 100 >= TAVAN_ESIK)
                bes_gun_once = None
                if gi >= 5:
                    bg = df[df["gun"] == gunler[gi - 5]]
                    if not bg.empty:
                        bes_gun_once = float(bg.iloc[-1]["close"])
                son5 = ((kapanis - bes_gun_once) / bes_gun_once * 100) if bes_gun_once else None

                # --- ERTESI GUN ---
                e_acilis = float(ertesi.iloc[0]["open"])
                if e_acilis <= 0:
                    continue
                if abs((e_acilis - kapanis) / kapanis * 100) > 12:   # veri hatasi
                    continue
                e_yuksek = float(ertesi["high"].max())
                e_dusuk = float(ertesi["low"].min())
                e_kapanis = float(ertesi.iloc[-1]["close"])
                ilk15 = ertesi.iloc[:ILK_PENCERE_BAR]
                ilk15_dip = float(ilk15["low"].min())

                # C YONTEMI: acilisin %0.5 altina limit - doldu mu?
                limit_fiyat = e_acilis * (1 - LIMIT_INDIRIM / 100)
                limit_doldu = ilk15_dip <= limit_fiyat

                olaylar.append({
                    "ticker": ticker, "tarih": str(gunler[gi]),
                    "getiri_pct": round(getiri, 2),
                    "kapanis_konumu": round(kapanis_konumu, 3) if kapanis_konumu is not None else None,
                    "gun_araligi_pct": round(gun_araligi_pct, 2) if gun_araligi_pct is not None else None,
                    "tavana_degdi": tavana_degdi,
                    "son5gun_pct": round(son5, 2) if son5 is not None else None,
                    "alis_A_kapanis": kapanis,
                    "alis_B_acilis": e_acilis,
                    "alis_C_limit": limit_fiyat if limit_doldu else None,
                    "limit_doldu": int(limit_doldu),
                    "e_yuksek": e_yuksek, "e_dusuk": e_dusuk, "e_kapanis": e_kapanis,
                })
            islenen += 1
        except Exception as e:
            print(f"[Birleşik] {ticker} hata: {e}", flush=True)
            atlanan += 1
        time.sleep(0.4)

    if not olaylar:
        return None, "Hiç olay üretilemedi."
    tum = pd.DataFrame(olaylar)
    dosya = os.path.join(DATA_DIR, "bist_birlesik_kural.csv")
    tum.to_csv(dosya, index=False, encoding="utf-8-sig")

    filtreler = {
        "TÜMÜ (filtresiz)": tum,
        "Dar aralık (<%5)": tum[tum.gun_araligi_pct < 5],
        "Zirvede + dar aralık": tum[(tum.kapanis_konumu >= 0.9) & (tum.gun_araligi_pct < 6)],
        "Riskliler ELENDİ": tum[(tum.gun_araligi_pct < 9) & (tum.tavana_degdi == 0) &
                                 ((tum.son5gun_pct < 25) | tum.son5gun_pct.isna())],
    }
    yontemler = [("A) Önceki kapanışta", "alis_A_kapanis"),
                 ("B) Ertesi açılışta", "alis_B_acilis"),
                 ("C) Açılış dibinden (limit)", "alis_C_limit")]
    satirlar = []
    for fad, alt in filtreler.items():
        if len(alt) < 30:
            continue
        for yad, kol in yontemler:
            veri = alt[alt[kol].notna()]
            if len(veri) < 25:
                continue
            for hedef in [1.5, 2.0, 2.5]:
                for stop in [None, 1.0, 2.0]:
                    g = [_cikis(r[kol], r.e_yuksek, r.e_dusuk, r.e_kapanis, hedef, stop) - MALIYET_PCT
                         for r in veri.itertuples()]
                    a = np.array(g)
                    satirlar.append({
                        "filtre": fad, "yontem": yad, "n": len(a),
                        "hedef": hedef, "stop": stop if stop else "yok",
                        "net": round(float(a.mean()), 4),
                        "kazanma": round(float((a > 0).mean() * 100), 1),
                        "en_kotu": round(float(a.min()), 2),
                    })
    if not satirlar:
        return None, "Yeterli örnek yok."
    tablo = pd.DataFrame(satirlar).sort_values("net", ascending=False)
    tablo.to_csv(os.path.join(DATA_DIR, "bist_birlesik_ozet.csv"),
                 index=False, encoding="utf-8-sig")
    return dosya, {"islenen": islenen, "toplam": len(tum),
                    "limit_dolma": round(float(tum.limit_doldu.mean() * 100), 1),
                    "satirlar": satirlar}


def _rapor(o):
    s = [f"🔗 AÇILIŞ DİBİ + ÇÖKME FİLTRELERİ BİRLİKTE — {KOD_SURUMU}",
         f"İşlenen: {o['islenen']} hisse | Olay: {o['toplam']}",
         f"Açılışın %{LIMIT_INDIRIM} altındaki limit emri dolma oranı: %{o['limit_dolma']}",
         f"Maliyet: -%{MALIYET_PCT}\n",
         "EN İYİ 15 KOMBİNASYON:",
         f"{'filtre':<22}{'yöntem':<26}{'hdf':>5}{'stop':>6}{'n':>5}{'NET':>9}{'kaz':>6}"]
    for x in sorted(o["satirlar"], key=lambda y: -y["net"])[:15]:
        s.append(f"{x['filtre'][:21]:<22}{x['yontem'][:25]:<26}{x['hedef']:>4}%"
                 f"{str(x['stop']):>6}{x['n']:>5}{x['net']:>8.3f}%{x['kazanma']:>5.1f}%")
    # yontem karsilastirmasi (filtresiz)
    s.append("\nYÖNTEM KARŞILAŞTIRMASI (TÜMÜ filtresiz, en iyi ayarla):")
    for yad, _ in [("A) Önceki kapanışta", 0), ("B) Ertesi açılışta", 0),
                   ("C) Açılış dibinden (limit)", 0)]:
        alt = [x for x in o["satirlar"] if x["filtre"] == "TÜMÜ (filtresiz)" and x["yontem"] == yad]
        if alt:
            b = max(alt, key=lambda y: y["net"])
            s.append(f"   {yad:<28} en iyi: hdf%{b['hedef']} stop{b['stop']} "
                     f"→ net %{b['net']:.3f} (n={b['n']})")
    s.append("\n⚠️ ÖRNEKLEM UYARISI: 5dk verisi 60 gün geriye gidiyor, bu "
             "yüzden örneklem küçük. Daha önce 1443 olayla çalışmıştık, "
             "burada çok daha az. Yön gösterir, kesin sonuç değil.\n"
             "Bir kombinasyonun gerçek olduğunu söylemek için n≥100 ve "
             "'TÜMÜ'den belirgin üstünlük ararız.")
    return "\n".join(s)


app = Flask(__name__)


@app.route("/health")
def health():
    return "OK (birlesik kural)", 200


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
        f"🔗 BİRLEŞİK KURAL TESTİ başlıyor — {KOD_SURUMU}\n\n"
        f"İki bulguyu İLK KEZ birleştiriyoruz:\n"
        f"  1) Çökme filtreleri (dar aralık, çok koşmamış, tavana "
        f"değmemiş) → çökmeyi %36.9'dan %18.8'e düşürüyordu\n"
        f"  2) Açılış örüntüsü → yükselenlerin %71.4'ü önce açılışın "
        f"altına düşüyor, dip 5. dakikada\n\n"
        f"Soru: filtreleri geçen hisselerde AÇILIŞ DİBİNDEN girsen "
        f"ne olurdu?\n\n"
        f"3 giriş yöntemi (kapanışta / açılışta / açılış dibinden limit) "
        f"× 4 filtre × 9 hedef-stop kombinasyonu.\n\n"
        f"⚠️ Bu deploy'da SADECE bu analiz çalışıyor.\nBitince CSV + özet göndereceğim."
    )
    try:
        dosya, sonuc = calistir()
        if dosya is None:
            send_telegram_message(f"🔗 Analiz başarısız: {sonuc}")
            return
        send_telegram_document(dosya, caption=_rapor(sonuc))
    except Exception as e:
        import traceback
        traceback.print_exc()
        send_telegram_message(f"🔗 Analiz hatası: {e}")


if __name__ == "__main__":
    print(f"[BAŞLANGIÇ] bist_birlesik_kural.py — {KOD_SURUMU}", flush=True)
    threading.Thread(target=_calis, daemon=True).start()
    threading.Thread(target=_ping, daemon=True).start()
    app.run(host="0.0.0.0", port=PORT)
