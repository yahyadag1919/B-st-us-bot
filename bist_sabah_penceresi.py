"""
bist_sabah_penceresi.py — SABAH İLK 4 SAAT: TAVANA KOŞANLAR
=============================================================
2026-08-28 — Kullanıcının gözlemi: "sabah bazı hisseler ciddi şekilde
tavana ilerliyor ve bazıları tavan oluyor."

ÖNCEKİ TEST BUNU ZATEN DESTEKLEDİ (bist_tavan_kilitlenme.py):
    Kapanışa 4+ saat kala yakalananlar → %32.2 tavana kilitleniyor
    Son 30 dk kala yakalananlar        → sadece %5.1
    4+ saat kala + %9-9.5 seviyesinde  → %58.6 (!)
Yani erken sinyal, geç sinyalden ÇOK daha değerli. Bu, radarı kurarken
yaptığım varsayımın TERSİ çıktı (ben "kapanışa yakın daha güvenilir"
diye ayarlamıştım - yanlışmış).

BU DOSYA SABAHA ODAKLANIP ŞUNLARI ÖLÇÜYOR:
  1. Açılıştan sonraki ilk 4 saatte (bar 0-16) hisse hangi seviyeye,
     saat kaçta ulaştı - eşik geçişleri (%5/%6/%7/%8/%9)
  2. O gün kapanışta tavana kilitlendi mi
  3. ★ EN ÖNEMLİSİ: O ANDA ALSAN NE OLURDU?
       a) Aynı gün kapanışa kadar tutsan
       b) Ertesi sabah açılışa kadar tutsan
       c) Kullanıcının planı: ertesi sabah +%2 hedefle sat

ZAMAN ÖLÇÜMÜ: saat dilimi karışıklığından kaçınmak için AÇILIŞTAN
İTİBAREN bar sayısı kullanılıyor (1 bar = 15dk). Bar 0-3 = ilk saat,
4-7 = ikinci saat, 8-11 = üçüncü, 12-15 = dördüncü saat.

VERİ SINIRI: 15dk verisi 60 gün geriye gidiyor (~40 işlem günü).
Örneklem sınırlı - kesin sayı değil, mertebe fikri verir.

Start Command:  python bist_sabah_penceresi.py
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
KOD_SURUMU = "sabah-penceresi-v1-2026-08-28"

TAVAN_ESIK = 9.5
SABAH_BAR_SINIRI = 16          # ilk 16 bar = ilk 4 saat
ESIKLER = [5.0, 6.0, 7.0, 8.0, 9.0]
MALIYET_PCT = 0.30
ERTESI_HEDEF = 2.0             # kullanicinin plani

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
        return yf.Ticker(ticker).history(period="60d", interval="15m", timeout=20)

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


def _saat_grubu(bar_no):
    if bar_no < 4:
        return "1. saat (ilk 1sa)"
    if bar_no < 8:
        return "2. saat"
    if bar_no < 12:
        return "3. saat"
    return "4. saat"


def calistir():
    kayitlar = []
    islenen, atlanan = 0, 0

    for n_i, ticker in enumerate(BIST_HISSELER, 1):
        print(f"[Sabah {n_i}/{len(BIST_HISSELER)}] {ticker}...", flush=True)
        ham = _veri_cek(ticker)
        if ham is None or ham.empty or len(ham) < 30:
            atlanan += 1
            time.sleep(0.35)
            continue
        try:
            df = ham.rename(columns={"Open": "open", "High": "high", "Low": "low",
                                      "Close": "close", "Volume": "volume"})
            df = df[["open", "high", "low", "close", "volume"]].dropna()
            if len(df) < 30:
                atlanan += 1
                time.sleep(0.35)
                continue
            idx = pd.to_datetime(df.index)
            try:
                idx = idx.tz_localize(None)
            except (TypeError, AttributeError):
                pass
            df.index = idx
            df["gun"] = df.index.date

            gunler = sorted(set(df["gun"]))
            for gi in range(1, len(gunler) - 1):   # ertesi gun de lazim
                gun = gunler[gi]
                dun = df[df["gun"] == gunler[gi - 1]]
                bugun = df[df["gun"] == gun]
                ertesi = df[df["gun"] == gunler[gi + 1]]
                if dun.empty or len(bugun) < 8 or ertesi.empty:
                    continue
                dun_kapanis = float(dun.iloc[-1]["close"])
                if dun_kapanis <= 0:
                    continue

                gun_kapanis = float(bugun.iloc[-1]["close"])
                gun_sonu_getiri = (gun_kapanis - dun_kapanis) / dun_kapanis * 100
                kilitlendi = int(gun_sonu_getiri >= TAVAN_ESIK)
                ertesi_acilis = float(ertesi.iloc[0]["open"])
                ertesi_yuksek = float(ertesi["high"].max())
                ertesi_kapanis = float(ertesi.iloc[-1]["close"])
                ort_hacim = float(dun["volume"].mean()) or 1.0

                # SABAH penceresinde her esigin ILK gecildigi ani bul
                for esik in ESIKLER:
                    for bi in range(min(SABAH_BAR_SINIRI, len(bugun))):
                        bar = bugun.iloc[bi]
                        getiri = (float(bar["close"]) - dun_kapanis) / dun_kapanis * 100
                        if getiri < esik:
                            continue
                        if getiri >= TAVAN_ESIK:
                            break  # zaten tavan, sinyal degil
                        alis = float(bar["close"])
                        # o andan gun sonuna
                        gun_sonuna = (gun_kapanis - alis) / alis * 100
                        # o andan ertesi sabah acilisa
                        ertesi_acilisa = (ertesi_acilis - alis) / alis * 100
                        # kullanicinin plani: ertesi sabah +%2 hedef
                        hedef_f = alis * (1 + ERTESI_HEDEF / 100)
                        if ertesi_acilis >= hedef_f:
                            plan = (ertesi_acilis - alis) / alis * 100
                        elif ertesi_yuksek >= hedef_f:
                            plan = ERTESI_HEDEF
                        else:
                            plan = (ertesi_kapanis - alis) / alis * 100
                        kayitlar.append({
                            "ticker": ticker, "tarih": str(gun), "esik": esik,
                            "bar_no": bi, "saat_grubu": _saat_grubu(bi),
                            "bar_saati": bugun.index[bi].strftime("%H:%M"),
                            "yakalanan_getiri": round(getiri, 2),
                            "hacim_orani": round(float(bar["volume"]) / ort_hacim, 2) if ort_hacim > 0 else None,
                            "gun_sonu_getiri": round(gun_sonu_getiri, 2),
                            "tavana_kilitlendi": kilitlendi,
                            "o_andan_gun_sonuna_pct": round(gun_sonuna, 2),
                            "o_andan_ertesi_acilisa_pct": round(ertesi_acilisa, 2),
                            "plan_net_pct": round(plan - MALIYET_PCT, 3),
                        })
                        break
            islenen += 1
        except Exception as e:
            print(f"[Sabah] {ticker} hata: {e}", flush=True)
            atlanan += 1
        time.sleep(0.35)

    if not kayitlar:
        return None, "Hiç kayıt üretilemedi."
    tum = pd.DataFrame(kayitlar)
    # imkansiz hareketleri temizle (BIST limiti +-%10; veri hatasi olabilir)
    tum = tum[tum.o_andan_ertesi_acilisa_pct.abs() <= 25]
    dosya = os.path.join(DATA_DIR, "bist_sabah_penceresi.csv")
    tum.to_csv(dosya, index=False, encoding="utf-8-sig")

    sonuc = {"islenen": islenen, "toplam": len(tum), "esikler": [], "saatler": [], "hacim": []}
    for e in ESIKLER:
        g = tum[tum.esik == e]
        if len(g) >= 15:
            sonuc["esikler"].append({
                "esik": e, "n": len(g),
                "kilit": round(float(g.tavana_kilitlendi.mean() * 100), 1),
                "gun_sonu": round(float(g.o_andan_gun_sonuna_pct.mean()), 2),
                "ertesi_acilis": round(float(g.o_andan_ertesi_acilisa_pct.mean()), 2),
                "plan": round(float(g.plan_net_pct.mean()), 3),
                "plan_kazanma": round(float((g.plan_net_pct > 0).mean() * 100), 1),
            })
    for sg in ["1. saat (ilk 1sa)", "2. saat", "3. saat", "4. saat"]:
        g = tum[tum.saat_grubu == sg]
        if len(g) >= 15:
            sonuc["saatler"].append({
                "ad": sg, "n": len(g),
                "kilit": round(float(g.tavana_kilitlendi.mean() * 100), 1),
                "plan": round(float(g.plan_net_pct.mean()), 3),
            })
    for ad, g in [("hacim ≥3x", tum[tum.hacim_orani >= 3]),
                  ("hacim 1-3x", tum[(tum.hacim_orani >= 1) & (tum.hacim_orani < 3)]),
                  ("hacim <1x", tum[tum.hacim_orani < 1])]:
        if len(g) >= 15:
            sonuc["hacim"].append({"ad": ad, "n": len(g),
                                    "kilit": round(float(g.tavana_kilitlendi.mean() * 100), 1),
                                    "plan": round(float(g.plan_net_pct.mean()), 3)})
    # en iyi: esik x saat
    capraz = []
    for e in ESIKLER:
        for sg in ["1. saat (ilk 1sa)", "2. saat", "3. saat", "4. saat"]:
            g = tum[(tum.esik == e) & (tum.saat_grubu == sg)]
            if len(g) >= 15:
                capraz.append({"esik": e, "saat": sg, "n": len(g),
                                "kilit": round(float(g.tavana_kilitlendi.mean() * 100), 1),
                                "plan": round(float(g.plan_net_pct.mean()), 3)})
    sonuc["capraz"] = sorted(capraz, key=lambda x: -x["plan"])[:10]
    return dosya, sonuc


def _rapor(o):
    s = [f"🌅 SABAH PENCERESİ (ilk 4 saat) — {KOD_SURUMU}",
         f"İşlenen: {o['islenen']} hisse | Örnek: {o['toplam']} sinyal\n",
         "EŞİĞE GÖRE (sabah o seviyeye ilk ulaştığı an):",
         f"{'eşik':>6}{'n':>6}{'kilitlenme':>12}{'gün sonuna':>12}{'ertesi açılışa':>16}{'PLAN net':>10}{'kazanma':>9}"]
    for e in o["esikler"]:
        s.append(f"{e['esik']:>5}%{e['n']:>6}{e['kilit']:>11.1f}%{e['gun_sonu']:>11.2f}%"
                 f"{e['ertesi_acilis']:>15.2f}%{e['plan']:>9.3f}%{e['plan_kazanma']:>8.1f}%")
    if o["saatler"]:
        s.append("\nGÜNÜN HANGİ SAATİNDE YAKALANDIĞINA GÖRE:")
        for x in o["saatler"]:
            s.append(f"   {x['ad']:<20} n={x['n']:<5} kilitlenme %{x['kilit']:<6} plan net %{x['plan']}")
    if o["hacim"]:
        s.append("\nHACME GÖRE:")
        for x in o["hacim"]:
            s.append(f"   {x['ad']:<12} n={x['n']:<5} kilitlenme %{x['kilit']:<6} plan net %{x['plan']}")
    if o["capraz"]:
        s.append("\nEN İYİ KOMBİNASYONLAR (eşik × saat, plan getirisine göre):")
        for x in o["capraz"][:8]:
            s.append(f"   %{x['esik']} / {x['saat']:<20} n={x['n']:<5} "
                     f"kilit %{x['kilit']:<6} plan net %{x['plan']}")
    s.append("\n⚠️ SÜTUNLARIN ANLAMI:\n"
             "  'kilitlenme' = o gün kapanışta tavan yapma oranı\n"
             "  'gün sonuna' = o an alıp AYNI GÜN kapanışta satsan\n"
             "  'ertesi açılışa' = o an alıp ERTESİ SABAH açılışta satsan\n"
             "  'PLAN net' = senin planın (ertesi sabah +%2 hedef), "
             f"maliyet (-%{MALIYET_PCT}) düşülmüş\n"
             "  PLAN net POZİTİFSE o kural gerçekten para kazandırır.")
    return "\n".join(s)


app = Flask(__name__)


@app.route("/health")
def health():
    return "OK (sabah penceresi)", 200


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
        f"🌅 SABAH PENCERESİ ANALİZİ başlıyor — {KOD_SURUMU}\n\n"
        f"Senin gözlemin test ediliyor: 'sabah bazı hisseler ciddi şekilde "
        f"tavana ilerliyor.'\n\n"
        f"Açılıştan sonraki ilk 4 saatte %5/%6/%7/%8/%9 eşiklerini ilk "
        f"geçtiği an yakalanıyor, sonra ölçülüyor:\n"
        f"  • O gün tavana kilitlendi mi\n"
        f"  • O an alsan aynı gün kapanışta ne olurdu\n"
        f"  • O an alsan ertesi sabah açılışta ne olurdu\n"
        f"  • ★ Senin planın: ertesi sabah +%{ERTESI_HEDEF} hedefle sat "
        f"(maliyet düşülmüş)\n\n"
        f"{len(BIST_HISSELER)} hisse × 60 günlük 15dk verisi.\n"
        f"⚠️ Bu deploy'da SADECE bu analiz çalışıyor.\nBitince CSV + özet göndereceğim."
    )
    try:
        dosya, sonuc = calistir()
        if dosya is None:
            send_telegram_message(f"🌅 Analiz başarısız: {sonuc}")
            return
        send_telegram_document(dosya, caption=_rapor(sonuc))
    except Exception as e:
        import traceback
        traceback.print_exc()
        send_telegram_message(f"🌅 Analiz hatası: {e}")


if __name__ == "__main__":
    print(f"[BAŞLANGIÇ] bist_sabah_penceresi.py — {KOD_SURUMU}", flush=True)
    threading.Thread(target=_calis, daemon=True).start()
    threading.Thread(target=_ping, daemon=True).start()
    app.run(host="0.0.0.0", port=PORT)
