"""
radar_onculu_test.py — GEMINI'NİN RADAR TASARIMINI OLDUĞU GİBİ TEST ETMEK
=========================================================================
Yahya'nin tespiti (2026-08-07) ve bu testin nedeni:

  "Bir hissenin kaderi tek bir stratejiye bagli olsaydi, o stratejiyi
   bulanlarin hepsi milyarder olurdu. Stratejiler tahmin araci degil,
   FILTRELEME aracidir."

Bu dogru ve onceki iki turnuvanin neden yanlis soruyu sordugunu aciklıyor.
M15 ve H1 turnuvalari sunu sordu: "Bu strateji sabit 1:2 R:R ile para
kazandiriyor mu?" Sabit hedef + seans sonu zorunlu kapanis dayatarak,
filtrenin kendisini degil o cikis yapisini olctuk.

BU TEST FARKLI BIR SORU SORUYOR:
  "Gemini'nin tarif ettigi filtreler bir araya geldiginde, hissenin
   BUYUK HAREKET yapma OLASILIGI artiyor mu?"

Yani strateji degil, OLASILIK KAYDIRMA olcuyoruz. Cevap "evet"se filtre
degerlidir - hangi cikis kuralini kullanacagimiz ayri ve sonraki sorudur.

=========================== KRITIK: KIYAS GRUBU ===========================
Bu testin en onemli parcasi. Sadece "filtre gecen hisselerin %30'u %3'e
gitti" demek HICBIR SEY ifade etmez - belki filtresiz de %30'u gidiyordur.
Bu yuzden her tetiklenmeyi bir KONTROL GRUBUYLA karsilastiriyoruz:
ayni gun, ayni saatte, ayni sekilde +%0.5-1.0 aralikta olan ama filtreleri
GECMEYEN hisseler. Fark varsa filtre gercekten bilgi tasiyor demektir.
Fark yoksa - ne kadar mantikli gorunurse gorunsun - filtre bos.

=========================== GEMINI'NIN TASARIMI ===========================
Dort bilesen, oldugu gibi:
  1. Anlik anormal hacim artisi (son 15dk hacmi / 20 gunluk ortalama >= 2x)
  2. Price action / kirilim (son N barin en yuksegi asildi)
  3. Endeksten pozitif ayrisma (hisse gunluk degisimi > endeks gunluk degisimi)
  4. KAP / haber kontrolu -> TEST EDILEMIYOR (ucretsiz API yok, scraping
     reddedildi). Bu bilesen olmadan olculuyor; sonucu okurken akilda tutun.
Yakalama asamasi: hisse gun ici +%0.5 ile +%1.0 arasindayken (hareketin basi)

CALISTIRMA (Render):
  1. Repoya yukle
  2. Start Command: python radar_onculu_test.py
  3. Sonuc Telegram'a dussun (~30-45 dk, 40 hisse)
  4. Start Command'i geri al: python main.py
"""

import os
import time
import threading
import requests
import numpy as np
import pandas as pd
import yfinance as yf

TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID")

# Hisse sayisi bilerek dusuk: onceki turnuvalarda surenin %90'i veri
# cekmekle geciyordu. 40 hisse x 60 gun yeterli ornek uretiyor.
TICKER_LIMIT = int(os.environ.get("RADAR_TICKER_LIMIT", "40"))

# --- Gemini'nin tanimladigi esikler (DEGISTIRILMEDI) ---
VOLUME_MULT = float(os.environ.get("RADAR_VOLUME_MULT", "2.0"))   # 2x-3x dedi, alt sinir
BREAKOUT_LOOKBACK = int(os.environ.get("RADAR_BREAKOUT_LOOKBACK", "20"))
CATCH_MIN_PCT = float(os.environ.get("RADAR_CATCH_MIN", "0.5"))   # +%0.50
CATCH_MAX_PCT = float(os.environ.get("RADAR_CATCH_MAX", "1.0"))   # +%1.00

# Olculecek hedefler: hareket nereye kadar gitti?
TARGETS = [1.0, 2.0, 3.0, 5.0]

INDEX_TICKER = "XU100.IS"

BIST_TICKERS = [
    "AEFES.IS", "AKBNK.IS", "AKSEN.IS", "ALARK.IS", "ARCLK.IS", "ASELS.IS",
    "ASTOR.IS", "BIMAS.IS", "BRSAN.IS", "CIMSA.IS", "DOHOL.IS", "EKGYO.IS",
    "ENKAI.IS", "EREGL.IS", "EUPWR.IS", "FROTO.IS", "GARAN.IS", "GUBRF.IS",
    "HALKB.IS", "HEKTS.IS", "ISCTR.IS", "KCHOL.IS", "KONTR.IS", "KOZAL.IS",
    "KRDMD.IS", "MGROS.IS", "ODAS.IS", "OYAKC.IS", "PETKM.IS", "PGSUS.IS",
    "SAHOL.IS", "SASA.IS", "SISE.IS", "SMRTG.IS", "TAVHL.IS", "TCELL.IS",
    "THYAO.IS", "TOASO.IS", "TUPRS.IS", "VESTL.IS", "YKBNK.IS",
]


def send_telegram_message(text: str):
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        print("(Telegram ayarli degil)\n" + text, flush=True)
        return
    for i in range(0, len(text), 3900):
        try:
            requests.post(
                f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage",
                json={"chat_id": TELEGRAM_CHAT_ID, "text": text[i:i + 3900]},
                timeout=20)
        except Exception as e:
            print(f"Telegram gonderilemedi: {e}", flush=True)


def fetch_15m(ticker):
    df = yf.Ticker(ticker).history(period="60d", interval="15m")
    if df is None or df.empty:
        return pd.DataFrame()
    df = df.reset_index().rename(columns={
        "Datetime": "ts", "Date": "ts", "Open": "open", "High": "high",
        "Low": "low", "Close": "close", "Volume": "volume"})
    need = ["ts", "open", "high", "low", "close", "volume"]
    if not all(c in df.columns for c in need):
        return pd.DataFrame()
    df = df[need].copy()
    df["session"] = pd.to_datetime(df["ts"]).dt.date
    return df


def collect_observations(df, index_bar):
    """Her mum icin: filtre gecti mi, ve o andan seans sonuna kadar ne oldu?

    HEM tetiklenenleri HEM kontrol grubunu ayni dongude topluyoruz ki
    kiyas adil olsun - ayni hisse, ayni gun, ayni saat dilimi."""
    if df.empty or len(df) < 100:
        return []

    # 20 gunluk ortalama BAR hacmi (gunluk degil, bar bazinda)
    df = df.copy()
    df["vol_ma"] = df["volume"].rolling(20 * 32, min_periods=200).mean()

    out = []
    for sess, day in df.groupby("session"):
        if len(day) < 6:
            continue
        idx = day.index.tolist()
        acilis = float(day.iloc[0]["open"])
        if acilis <= 0:
            continue
        seans_sonu = float(day.iloc[-1]["close"])
        for k in range(1, len(day) - 1):
            r = day.iloc[k]
            fiyat = float(r["close"])
            gun_ici_pct = (fiyat - acilis) / acilis * 100

            # YAKALAMA ASAMASI: sadece +%0.5 ile +%1.0 arasindakiler
            if not (CATCH_MIN_PCT <= gun_ici_pct <= CATCH_MAX_PCT):
                continue

            # --- Gemini'nin 3 olculebilir bileseni ---
            vol_ma = r["vol_ma"]
            hacim_ok = bool(pd.notna(vol_ma) and vol_ma > 0
                            and r["volume"] >= vol_ma * VOLUME_MULT)

            onceki = day.iloc[max(0, k - BREAKOUT_LOOKBACK):k]
            kirilim_ok = bool(not onceki.empty and fiyat > float(onceki["high"].max()))

            # Endeksin AYNI ANDAKI degisimi (bar bazinda, ileriye bakma yok)
            endeks_o_an = index_bar.get(pd.Timestamp(r["ts"]))
            if endeks_o_an is None:
                continue
            ayrisma_ok = bool(gun_ici_pct > endeks_o_an)

            gecti = hacim_ok and kirilim_ok and ayrisma_ok

            # --- SONUC: bu andan seans sonuna kadar ne oldu? ---
            kalan = day.iloc[k + 1:]
            if kalan.empty:
                continue
            max_yukari = (float(kalan["high"].max()) - fiyat) / fiyat * 100
            max_asagi = (float(kalan["low"].min()) - fiyat) / fiyat * 100
            kapanis = (seans_sonu - fiyat) / fiyat * 100

            out.append({
                "gecti": gecti, "hacim": hacim_ok, "kirilim": kirilim_ok,
                "ayrisma": ayrisma_ok,
                "max_yukari": max_yukari, "max_asagi": max_asagi,
                "kapanis": kapanis,
            })
    return out


def ozet(gozlemler, etiket):
    if not gozlemler:
        return [f"  {etiket}: gözlem yok"], None
    n = len(gozlemler)
    mu = np.array([g["max_yukari"] for g in gozlemler])
    md = np.array([g["max_asagi"] for g in gozlemler])
    kp = np.array([g["kapanis"] for g in gozlemler])
    satir = [f"  {etiket} (n={n})"]
    for t in TARGETS:
        oran = (mu >= t).mean() * 100
        satir.append(f"     +%{t:.0f}'e ulaşan: %{oran:.1f}")
    satir.append(f"     Ortalama en yüksek: +%{mu.mean():.2f} | "
                 f"en düşük: %{md.mean():.2f}")
    satir.append(f"     Seans sonu ortalama: {kp.mean():+.2f}% | "
                 f"artıda kapanan: %{(kp > 0).mean() * 100:.1f}")
    return satir, {"n": n, "hedefler": {t: (mu >= t).mean() * 100 for t in TARGETS},
                   "kapanis": kp.mean(), "artida": (kp > 0).mean() * 100}


def main():
    tickers = BIST_TICKERS[:TICKER_LIMIT]
    print(f"RADAR TESTI BASLIYOR: {len(tickers)} hisse", flush=True)
    send_telegram_message(
        "🔬 [RADAR ÖNCÜL TESTİ] Başladı.\n"
        f"{len(tickers)} hisse | 60 günlük 15dk verisi\n"
        "Soru: Gemini'nin filtreleri büyük hareket olasılığını artırıyor mu?\n"
        "Bu ~30-45 dakika sürebilir...")

    # Endeksin BAR BAZINDA degisimi (goreli guc icin).
    # DERS (2026-08-08): ilk surumde endeksin O GUNUN TAMAMINDAKI degisimi
    # kullaniliyordu ve hissenin saat 11:00'deki durumu onunla
    # karsilastiriliyordu - yani saat 11'de gun sonunu bilmek. ILERIYE BAKMA.
    # Turnuvalarda titizlikle kacindigimiz hata buraya sizmis.
    # Artik her bar, endeksin AYNI BARDAKI degisimiyle karsilastiriliyor.
    # Ayrica endeks cagrisi ilk istek oldugu icin hiz limitine takilip tum
    # testi iptal ettirmisti - tekrar deneme eklendi.
    index_bar = {}
    for deneme in range(4):
        try:
            idx = yf.Ticker(INDEX_TICKER).history(period="60d", interval="15m")
            if idx is None or idx.empty:
                raise ValueError("bos endeks verisi")
            idx = idx.reset_index().rename(columns={"Datetime": "ts", "Date": "ts",
                                                    "Open": "open", "Close": "close"})
            idx["session"] = pd.to_datetime(idx["ts"]).dt.date
            for sess, day in idx.groupby("session"):
                a = float(day.iloc[0]["open"])
                if a <= 0:
                    continue
                for _, br in day.iterrows():
                    index_bar[pd.Timestamp(br["ts"])] = (float(br["close"]) - a) / a * 100
            break
        except Exception as e:
            print(f"Endeks denemesi {deneme + 1} basarisiz: {e}", flush=True)
            if deneme < 3:
                time.sleep(15 * (deneme + 1))

    if not index_bar:
        send_telegram_message(
            "🚨 Endeks verisi 4 denemede alınamadı — göreli güç ölçülemez, test iptal.\n"
            "Muhtemelen Yahoo hız limiti. Başka bir işlem çalışmıyorken tekrar dene.")
        return

    gecen, kontrol = [], []
    ok, hata = 0, 0
    for n, tk in enumerate(tickers, 1):
        try:
            df = fetch_15m(tk)
            if df.empty:
                hata += 1
                print(f"[{n}/{len(tickers)}] {tk} veri yok", flush=True)
                continue
            for g in collect_observations(df, index_bar):
                (gecen if g["gecti"] else kontrol).append(g)
            ok += 1
            print(f"[{n}/{len(tickers)}] {tk} tamam", flush=True)
        except Exception as e:
            hata += 1
            print(f"[{n}/{len(tickers)}] {tk} HATA: {e}", flush=True)
        time.sleep(0.3)

    lines = ["🔬 [RADAR ÖNCÜL TESTİ SONUÇLARI]",
             f"Taranan: {ok}/{len(tickers)} hisse",
             "Soru: filtreler büyük hareket olasılığını artırıyor mu?", ""]

    s1, d1 = ozet(gecen, "✅ FİLTRELERİ GEÇEN")
    s2, d2 = ozet(kontrol, "⬜ KONTROL GRUBU (aynı anda +%0.5-1.0'de, filtresiz)")
    lines += s1 + [""] + s2 + [""]

    # BILESEN KIRILIMI: filtre az tetiklendiyse hangi kosulun daralttigini
    # gorebilmek icin. Tasarim degistirilmedi - sadece TESHIS ekleniyor.
    tum = gecen + kontrol
    if tum:
        n = len(tum)
        h = sum(1 for g in tum if g["hacim"])
        k = sum(1 for g in tum if g["kirilim"])
        a = sum(1 for g in tum if g["ayrisma"])
        hk = sum(1 for g in tum if g["hacim"] and g["kirilim"])
        ha = sum(1 for g in tum if g["hacim"] and g["ayrisma"])
        ka = sum(1 for g in tum if g["kirilim"] and g["ayrisma"])
        lines.append("🔍 BİLEŞEN KIRILIMI (toplam gözlem: %d)" % n)
        lines.append(f"  Hacim ≥{VOLUME_MULT:g}×: %{h / n * 100:.1f}")
        lines.append(f"  Kırılım (son {BREAKOUT_LOOKBACK} bar): %{k / n * 100:.1f}")
        lines.append(f"  Endeksten ayrışma: %{a / n * 100:.1f}")
        lines.append(f"  Hacim+Kırılım: %{hk / n * 100:.1f} | "
                     f"Hacim+Ayrışma: %{ha / n * 100:.1f} | "
                     f"Kırılım+Ayrışma: %{ka / n * 100:.1f}")
        lines.append(f"  ÜÇÜ BİRDEN: %{len(gecen) / n * 100:.1f}")
        if hk < n * 0.01:
            lines.append("  ⚠️ Hacim ve kırılım neredeyse hiç birlikte oluşmuyor — "
                         "üç koşulu birden aramak sinyal sayısını çok kısıtlıyor.")
        lines.append("")

    if d1 and d2:
        lines.append("📊 KARŞILAŞTIRMA (asıl cevap burada)")
        belirgin = False
        for t in TARGETS:
            a, b = d1["hedefler"][t], d2["hedefler"][t]
            fark = a - b
            oran = (a / b) if b > 0 else 0
            lines.append(f"  +%{t:.0f}'e ulaşma: filtreli %{a:.1f} vs kontrol %{b:.1f} "
                         f"({fark:+.1f} puan, {oran:.2f}×)")
            if t >= 2.0 and fark >= 3.0 and oran >= 1.25:
                belirgin = True
        lines.append("")
        if d1["n"] < 100:
            lines.append("⚠️ Filtreyi geçen gözlem 100'ün altında — sonuç güvenilir değil.")
        elif belirgin:
            lines.append("✅ FİLTRE BİLGİ TAŞIYOR: büyük hareket olasılığı kontrol "
                         "grubuna göre belirgin şekilde yüksek. Radar kurulmaya değer.")
        else:
            lines.append("❌ FİLTRE ANLAMLI FARK YARATMIYOR: filtreyi geçen hisseler, "
                         "aynı anda aynı seviyedeki diğer hisselerden daha iyi "
                         "davranmıyor. Mantıklı görünmesi yeterli değil.")

    lines.append("")
    lines.append("ℹ️ KAP/haber bileşeni test EDİLEMEDİ (ücretsiz API yok). "
                 "Sonuç, 4 bileşenin 3'ü için geçerlidir.")
    lines.append("ℹ️ Bu test bir strateji değil, OLASILIK KAYDIRMA ölçer — "
                 "çıkış kuralı ayrı ve sonraki sorudur.")
    if hata:
        lines.append(f"ℹ️ Veri alınamayan: {hata} hisse")

    msg = "\n".join(lines)
    send_telegram_message(msg)
    print(msg, flush=True)


if __name__ == "__main__":
    # Render Web Service port bekliyor; onceki turnuvalarda ogrenilen 3 kural
    from http.server import BaseHTTPRequestHandler, HTTPServer

    class _P(BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b"test calisiyor")

        def log_message(self, *a):
            pass

    threading.Thread(
        target=lambda: HTTPServer(("0.0.0.0", int(os.environ.get("PORT", "10000"))),
                                  _P).serve_forever(), daemon=True).start()

    def _keep_awake():
        url = os.environ.get("RENDER_EXTERNAL_URL")
        if not url:
            return
        time.sleep(60)
        while True:
            try:
                requests.get(url.rstrip("/"), timeout=20)
            except Exception:
                pass
            time.sleep(600)

    threading.Thread(target=_keep_awake, daemon=True).start()

    main()
    print("Test bitti. Start Command'i 'python main.py' yapabilirsin.", flush=True)
    while True:
        time.sleep(3600)
