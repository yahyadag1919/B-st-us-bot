"""
bist_plan_testi.py — KULLANICININ PLANININ GERÇEK TESTİ
=========================================================
2026-08-28 — Kullanıcının planı, kendi anlattığı gibi:

  1. Akşam (17:00 civarı) bottan bildirim gelen hisselere emir ver
  2. Kapanışta portföye bak - elinde ne kaldıysa o
  3. ERTESİ SABAH, ALIŞ FİYATINA göre sabit emirler koy:
       - Tavan olarak kapananlar     → +%2.00 / +%2.30 satış
       - Tavan olmayanlar (%6-9.5)   → +%1.00 / +%1.50 satış
       - Hepsinde zarar-durdur       → -%1 veya -%2
  4. Her şey SABİT, karar verme yok.

ÖNEMLİ: Hedefler ALIŞ FİYATINA göre (kullanıcı "B" dedi), açılış
fiyatına göre değil. Bu fark kritik: tavan yapanlar ertesi gün zaten
ortalama +%2.42 boşlukla açıyor - yani +%2 hedefi AÇILIŞTA zaten
tutmuş oluyor. Tavan olmayanlar ise boşluksuz açıyor (+%0.10), onlarda
hedefin gün içinde tutması gerekiyor.

GİRİŞ VARSAYIMI: alış fiyatı = o günün KAPANIŞI (kullanıcı kapanışa
yakın emir veriyor). Tavan kilitliyken gerçekte almak zor olabilir -
bu, sonuçların İYİMSER tarafı, dürüstçe belirtiliyor.

ÇIKIŞ MANTIĞI (gerçekçi ve TEMKİNLİ):
  - Ertesi gün AÇILIŞ zaten hedefin üstündeyse → açılış fiyatından satış
    (limit emri açılış seansında gerçekleşir, hedeften İYİ fiyat)
  - Açılış zaten stop'un altındaysa → açılış fiyatından zarar (gap down)
  - Gün içinde hem hedef hem stop görülmüşse → TEMKİNLİ varsayım: önce
    STOP tetiklendi say (kötümser; gerçekte bazen hedef önce gelir,
    yani gerçek sonuç bundan biraz İYİ olabilir)
  - Hiçbiri tutmazsa → gün sonu kapanış fiyatından çık

MALİYET: her işlemde alım+satım komisyonu ve fiyat farkı için
varsayılan %0.30 düşülüyor (MALIYET_PCT ile değiştirilebilir).
Bu olmadan sonuçlar yanıltıcı olur - önceki testte marjın maliyetten
düşük çıktığını görmüştük.

Start Command:  python bist_plan_testi.py
Bu deploy'da SADECE bu test çalışır.
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
KOD_SURUMU = "plan-testi-v1-2026-08-28"

MALIYET_PCT = float(os.environ.get("MALIYET_PCT", "0.30"))
TAVAN_ESIK = 9.5
GUCLU_ALT, GUCLU_UST = 6.0, 9.49

# Denenecek kombinasyonlar
TAVAN_HEDEFLERI = [1.5, 2.0, 2.3, 2.5, 3.0]
GUCLU_HEDEFLERI = [0.75, 1.0, 1.5, 2.0]
STOPLAR = [1.0, 1.5, 2.0, 3.0, None]   # None = stop yok

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


def _islem_sonucu(alis, e_acilis, e_yuksek, e_dusuk, e_kapanis, hedef_pct, stop_pct):
    """Tek işlemin BRÜT getirisi (%). Çıkış mantığı dosya başında anlatıldı."""
    hedef_fiyat = alis * (1 + hedef_pct / 100)
    stop_fiyat = alis * (1 - stop_pct / 100) if stop_pct else None

    # 1) Acilis zaten hedefin ustunde -> acilistan sat (hedeften IYI fiyat)
    if e_acilis >= hedef_fiyat:
        return (e_acilis - alis) / alis * 100, "ACILIS_HEDEF_USTU"
    # 2) Acilis zaten stop'un altinda -> gap down, acilistan cik
    if stop_fiyat and e_acilis <= stop_fiyat:
        return (e_acilis - alis) / alis * 100, "ACILIS_GAP_STOP"
    # 3) Gun ici
    hedef_gorundu = e_yuksek >= hedef_fiyat
    stop_gorundu = bool(stop_fiyat and e_dusuk <= stop_fiyat)
    if hedef_gorundu and stop_gorundu:
        # TEMKINLI: once stop tetiklendi say
        return -stop_pct, "IKISI_DE_TEMKINLI_STOP"
    if hedef_gorundu:
        return hedef_pct, "HEDEF"
    if stop_gorundu:
        return -stop_pct, "STOP"
    return (e_kapanis - alis) / alis * 100, "KAPANIS"


def calistir():
    olaylar = []
    islenen, atlanan = 0, 0

    for n_i, ticker in enumerate(BIST_HISSELER, 1):
        print(f"[Plan Testi {n_i}/{len(BIST_HISSELER)}] {ticker}...", flush=True)
        ham = _veri_cek(ticker)
        if ham is None or ham.empty or len(ham) < 40:
            atlanan += 1
            time.sleep(0.35)
            continue
        try:
            df = ham.rename(columns={"Open": "open", "High": "high",
                                      "Low": "low", "Close": "close"})
            df = df[["open", "high", "low", "close"]].dropna().reset_index(drop=True)
            if len(df) < 40:
                atlanan += 1
                time.sleep(0.35)
                continue
            df["getiri"] = df["close"].pct_change() * 100

            for i in range(1, len(df) - 1):
                g = df.iloc[i]["getiri"]
                if pd.isna(g):
                    continue
                if g >= TAVAN_ESIK:
                    tur = "TAVAN"
                elif GUCLU_ALT <= g < GUCLU_UST:
                    tur = "GUCLU"
                else:
                    continue
                alis = float(df.iloc[i]["close"])
                e = df.iloc[i + 1]
                if alis <= 0 or e["open"] <= 0:
                    continue
                olaylar.append({"ticker": ticker, "tur": tur,
                                 "alis": alis, "e_acilis": float(e["open"]),
                                 "e_yuksek": float(e["high"]), "e_dusuk": float(e["low"]),
                                 "e_kapanis": float(e["close"])})
            islenen += 1
        except Exception as e:
            print(f"[Plan Testi] {ticker} hata: {e}", flush=True)
            atlanan += 1
        time.sleep(0.35)

    if not olaylar:
        return None, "Hiç olay üretilemedi."
    ham_df = pd.DataFrame(olaylar)
    ham_df.to_csv(os.path.join(DATA_DIR, "bist_plan_olaylar.csv"),
                  index=False, encoding="utf-8-sig")

    satirlar = []
    for tur, hedefler in [("TAVAN", TAVAN_HEDEFLERI), ("GUCLU", GUCLU_HEDEFLERI)]:
        alt = ham_df[ham_df.tur == tur]
        if len(alt) < 20:
            continue
        for hedef in hedefler:
            for stop in STOPLAR:
                getiriler, sonuclar = [], []
                for _, r in alt.iterrows():
                    brut, nasil = _islem_sonucu(r.alis, r.e_acilis, r.e_yuksek,
                                                 r.e_dusuk, r.e_kapanis, hedef, stop)
                    getiriler.append(brut - MALIYET_PCT)   # NET
                    sonuclar.append(nasil)
                arr = np.array(getiriler)
                satirlar.append({
                    "tur": tur, "hedef_pct": hedef,
                    "stop_pct": stop if stop else "yok",
                    "n": len(arr),
                    "net_ort_pct": round(float(arr.mean()), 4),
                    "net_medyan_pct": round(float(np.median(arr)), 3),
                    "kazanma_pct": round(float((arr > 0).mean() * 100), 1),
                    "hedef_tutma_pct": round(float(sum(1 for s in sonuclar
                        if s in ("HEDEF", "ACILIS_HEDEF_USTU")) / len(sonuclar) * 100), 1),
                    "stop_yeme_pct": round(float(sum(1 for s in sonuclar
                        if "STOP" in s) / len(sonuclar) * 100), 1),
                    "en_kotu_pct": round(float(arr.min()), 2),
                })
    if not satirlar:
        return None, "Yeterli olay yok."
    tablo = pd.DataFrame(satirlar).sort_values(["tur", "net_ort_pct"], ascending=[True, False])
    dosya = os.path.join(DATA_DIR, "bist_plan_testi.csv")
    tablo.to_csv(dosya, index=False, encoding="utf-8-sig")
    return dosya, {"islenen": islenen, "tavan_olay": int((ham_df.tur == "TAVAN").sum()),
                    "guclu_olay": int((ham_df.tur == "GUCLU").sum()),
                    "satirlar": satirlar}


def _rapor(o):
    s = [f"🎯 PLANININ TESTİ — {KOD_SURUMU}",
         f"İşlenen: {o['islenen']} hisse | TAVAN olayı: {o['tavan_olay']} | "
         f"GÜÇLÜ (%6-9.5) olayı: {o['guclu_olay']}",
         f"Maliyet düşülmüş: her işlemde -%{MALIYET_PCT} (komisyon+fark)\n"]
    for tur, baslik in [("TAVAN", "🔒 TAVAN OLARAK KAPANANLAR"),
                        ("GUCLU", "📈 %6-9.5 ARASI KAPANANLAR")]:
        alt = [x for x in o["satirlar"] if x["tur"] == tur]
        if not alt:
            continue
        alt = sorted(alt, key=lambda x: -x["net_ort_pct"])[:8]
        s.append(f"{baslik} — en iyi 8 kombinasyon:")
        s.append(f"{'hedef':>7}{'stop':>7}{'NET ort':>10}{'kazanma':>9}{'hedef tut':>11}{'en kötü':>9}")
        for x in alt:
            s.append(f"{x['hedef_pct']:>6}%{str(x['stop_pct']):>7}{x['net_ort_pct']:>9.3f}%"
                     f"{x['kazanma_pct']:>8.1f}%{x['hedef_tutma_pct']:>10.1f}%{x['en_kotu_pct']:>8.1f}%")
        s.append("")
    s.append("⚠️ NASIL OKUNMALI:\n"
             "  'NET ort' POZİTİFSE → maliyet sonrası kâr bırakıyor demektir.\n"
             "  NEGATİFSE → o kombinasyon zarar ettirir, ne kadar yüksek\n"
             "  kazanma oranı olursa olsun.\n"
             "  Gün içinde hem hedef hem stop görülen vakalarda TEMKİNLİ\n"
             "  varsayım (stop önce) kullanıldı - gerçek sonuç biraz daha iyi olabilir.\n"
             "  Tavan kilitliyken almak gerçekte zor - o taraf iyimser.")
    return "\n".join(s)


app = Flask(__name__)


@app.route("/health")
def health():
    return "OK (plan testi)", 200


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
        f"🎯 PLANININ TESTİ başlıyor — {KOD_SURUMU}\n\n"
        f"Senin anlattığın plan birebir test ediliyor:\n"
        f"  • Akşam kapanışta al\n"
        f"  • Ertesi sabah ALIŞ FİYATINA göre sabit satış emri\n"
        f"  • Tavan olanlar: +%1.5 ile +%3 arası denenecek\n"
        f"  • %6-9.5 olanlar: +%0.75 ile +%2 arası denenecek\n"
        f"  • Zarar-durdur: -%1 / -%1.5 / -%2 / -%3 / yok\n\n"
        f"{len(BIST_HISSELER)} hisse × 2 yıl. Her işlemden %{MALIYET_PCT} "
        f"maliyet düşülüyor - önceki testte marjın maliyetten düşük "
        f"çıktığını görmüştük, o yüzden bu şart.\n\n"
        f"⚠️ Bu deploy'da SADECE bu test çalışıyor.\nBitince CSV + özet göndereceğim."
    )
    try:
        dosya, sonuc = calistir()
        if dosya is None:
            send_telegram_message(f"🎯 Test başarısız: {sonuc}")
            return
        send_telegram_document(dosya, caption=_rapor(sonuc))
    except Exception as e:
        import traceback
        traceback.print_exc()
        send_telegram_message(f"🎯 Test hatası: {e}")


if __name__ == "__main__":
    print(f"[BAŞLANGIÇ] bist_plan_testi.py — {KOD_SURUMU}", flush=True)
    threading.Thread(target=_calis, daemon=True).start()
    threading.Thread(target=_ping, daemon=True).start()
    app.run(host="0.0.0.0", port=PORT)
