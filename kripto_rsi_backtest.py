"""
kripto_rsi_backtest.py — KRİPTO RSI ALARMLARI GERİYE DÖNÜK TESTİ
==============================================================================
2026-10-26 — Kullanıcının "hangi zaman diliminde bakmak daha iyi" sorusuna
tahminle değil veriyle cevap vermek için kuruldu.

SORU: Canlı tarayıcının verdiği alarmlar (RSI ≤ 20 aşırı satım, RSI ≥ 75
aşırı alım) geçmişte gerçekten işe yaradı mı? Hangi zaman diliminde
(1 saatlik / 4 saatlik / günlük) daha iyi?

YÖNTEM:
  - Binance vadelide de olan, hacmi en yüksek ~100 coin, son 2 yıl
  - Alarm kuralı canlı tarayıcıyla AYNI: RSI(14) eşiğe girince alarm, sonra
    RSI 25'in üstüne (aşırı alımda 70'in altına) çıkana kadar o coin için
    yeni alarm yok. (Test KAPANMIŞ mumlarla yapılıyor.)
  - Alarmdan 4 saat / 1 gün / 3 gün / 7 gün sonra fiyat ne oldu?
  - KARŞILAŞTIRMA (kör çizgi): aynı coinlerde, aynı dönemde, RASTGELE
    herhangi bir andan aynı süre sonraki ortalama getiri. Alarm bundan
    belirgin şekilde iyi değilse, sinyal bir şey katmıyor demektir.
    (Boğa piyasasında her şey yükselir - kör çizgi bu yanılgıyı engeller.)

KOMUT: /rsi_test (kripto botunun Telegram sohbetinde). 15-30 dk sürebilir.
Sonuç hem kısa mesaj hem detaylı .md dosyası olarak gelir.

SINIRLAR (rapora da yazılıyor):
  - Bugün listede olan coinler test ediliyor; geçmişte batıp listeden
    çıkan coinler yok (hayatta kalan yanılgısı - sonuçları biraz iyimser yapar).
  - Aynı gün birçok coin birlikte alarm verebilir (piyasa çöküşü), bu
    alarmlar birbirinden bağımsız sayılmamalı - rapor "farklı gün" sayısını
    da gösteriyor.
  - Komisyon: vadeli işlemde giriş+çıkış ~%0.10 varsayıldı, "net" sütununa
    düşüldü. Fonlama (funding) ücreti dahil değil.
"""
import os
import time
import threading
from datetime import datetime, timezone

import kripto_rsi_tarayici as K

TEST_COIN_SAYISI = int(os.environ.get("KRIPTO_TEST_COIN", "100"))
TEST_GUN = int(os.environ.get("KRIPTO_TEST_GUN", "730"))
KOMISYON_PCT = 0.10
UFUKLAR_SAAT = [4, 24, 72, 168]
UFUK_AD = {4: "4 saat", 24: "1 gün", 72: "3 gün", 168: "7 gün"}
ZAMAN_DILIMLERI = [("1s", "1h", 1), ("4s", "4h", 4), ("1g", "1d", 24)]
ZD_AD = {"1s": "1 saatlik", "4s": "4 saatlik", "1g": "Günlük"}
MIN_ORNEK = 20

_kilit = threading.Lock()
_calisiyor = {"test": False}


# =============================================================================
# VERİ
# =============================================================================
def _gecmis_kapanislar(sembol, aralik, bar_saat):
    """Binance spot (data-api.binance.vision - ABD'den erişilebiliyor) geçmiş
    mumları, sayfa sayfa (her istekte en fazla 1000 mum). [(zaman_ms, kapanis)]"""
    bas_ms = int((time.time() - TEST_GUN * 86400) * 1000)
    adim_ms = bar_saat * 3600 * 1000
    sonuc = []
    while True:
        veri = K._get(f"{K.BinanceKaynak.taban}/api/v3/klines",
                      {"symbol": sembol, "interval": aralik,
                       "startTime": bas_ms, "limit": 1000})
        if not veri:
            break
        sonuc.extend((int(m[0]), float(m[4])) for m in veri)
        if len(veri) < 1000:
            break
        bas_ms = int(veri[-1][0]) + adim_ms
        time.sleep(0.12)
    if sonuc:
        sonuc = sonuc[:-1]  # son mum henüz kapanmadı - teste dahil etme
    return sonuc


def rsi_serisi(kapanislar, periyot=K.RSI_PERIYOT):
    """Her mum için RSI (Wilder, canlı tarayıcıdaki rsi_hesapla ile aynı
    yöntem). İlk `periyot` eleman None."""
    n = len(kapanislar)
    sonuc = [None] * n
    if n < periyot + 1:
        return sonuc
    kazanc = kayip = 0.0
    for i in range(1, periyot + 1):
        fark = kapanislar[i] - kapanislar[i - 1]
        kazanc += max(fark, 0)
        kayip += max(-fark, 0)
    ok, oy = kazanc / periyot, kayip / periyot

    def _rsi(a, b):
        if b == 0:
            return 100.0 if a > 0 else 50.0
        return 100 - 100 / (1 + a / b)

    sonuc[periyot] = _rsi(ok, oy)
    for i in range(periyot + 1, n):
        fark = kapanislar[i] - kapanislar[i - 1]
        ok = (ok * (periyot - 1) + max(fark, 0)) / periyot
        oy = (oy * (periyot - 1) + max(-fark, 0)) / periyot
        sonuc[i] = _rsi(ok, oy)
    return sonuc


def alarm_indeksleri(rsi, esik, yeniden_kurma, yon):
    """Canlı tarayıcının alarm kuralı: eşiğe girince alarm, yeniden kurma
    seviyesine dönene kadar susar."""
    alarmlar, kurulu = [], True
    for i, r in enumerate(rsi):
        if r is None:
            continue
        if yon == "dusuk":
            if kurulu and r <= esik:
                alarmlar.append(i)
                kurulu = False
            elif not kurulu and r > yeniden_kurma:
                kurulu = True
        else:
            if kurulu and r >= esik:
                alarmlar.append(i)
                kurulu = False
            elif not kurulu and r < yeniden_kurma:
                kurulu = True
    return alarmlar


# =============================================================================
# İSTATİSTİK BİRİKTİRİCİ
# =============================================================================
def _bos_istatistik():
    return {zd: {"kor": {h: [0.0, 0, 0] for h in UFUKLAR_SAAT},   # toplam, n, pozitif
                 "dusuk": {h: [] for h in UFUKLAR_SAAT},
                 "yuksek": {h: [] for h in UFUKLAR_SAAT},
                 "gunler": {"dusuk": set(), "yuksek": set()},
                 "coin": 0}
            for zd, _, _ in ZAMAN_DILIMLERI}


def coin_isle(ist, zd, bar_saat, mumlar):
    if len(mumlar) < K.RSI_PERIYOT + 10:
        return
    zamanlar = [z for z, _ in mumlar]
    kap = [c for _, c in mumlar]
    rsi = rsi_serisi(kap)
    n = len(kap)
    blok = ist[zd]
    blok["coin"] += 1

    # kör çizgi: RSI'ı olan her mumdan aynı süre sonra
    for h in UFUKLAR_SAAT:
        if h % bar_saat:
            continue
        k = h // bar_saat
        toplam = 0.0
        sayi = pozitif = 0
        for i in range(K.RSI_PERIYOT, n - k):
            g = kap[i + k] / kap[i] - 1
            toplam += g
            sayi += 1
            pozitif += g > 0
        kor = blok["kor"][h]
        kor[0] += toplam
        kor[1] += sayi
        kor[2] += pozitif

    for yon, esik, kurma in (("dusuk", K.RSI_ESIK, K.RSI_YENIDEN_KURMA),
                             ("yuksek", K.RSI_UST_ESIK, K.RSI_UST_YENIDEN_KURMA)):
        for i in alarm_indeksleri(rsi, esik, kurma, yon):
            gun = datetime.fromtimestamp(zamanlar[i] / 1000, tz=timezone.utc).strftime("%Y-%m-%d")
            blok["gunler"][yon].add(gun)
            for h in UFUKLAR_SAAT:
                if h % bar_saat:
                    continue
                k = h // bar_saat
                if i + k < n:
                    blok[yon][h].append(kap[i + k] / kap[i] - 1)


# =============================================================================
# RAPOR
# =============================================================================
def _medyan(liste):
    s = sorted(liste)
    m = len(s)
    return (s[m // 2] if m % 2 else (s[m // 2 - 1] + s[m // 2]) / 2) if m else 0.0


def _satir(yon, h, getiriler, kor):
    n = len(getiriler)
    kor_ort = kor[0] / kor[1] * 100 if kor[1] else 0.0
    kor_pozitif = kor[2] / kor[1] * 100 if kor[1] else 0.0
    if n < MIN_ORNEK:
        return f"| {UFUK_AD[h]} | {n} | örnek yetersiz | | | | |", None
    ort = sum(getiriler) / n * 100
    med = _medyan(getiriler) * 100
    if yon == "dusuk":  # long: yükseliş iyi
        isabet = sum(g > 0 for g in getiriler) / n * 100
        kor_isabet = kor_pozitif
        avantaj = ort - kor_ort
        net = ort - KOMISYON_PCT
    else:              # short: düşüş iyi
        isabet = sum(g < 0 for g in getiriler) / n * 100
        kor_isabet = 100 - kor_pozitif
        avantaj = kor_ort - ort
        net = -ort - KOMISYON_PCT
    isaret = "✅" if avantaj > 0.5 and net > 0 else ("➖" if avantaj > 0 else "❌")
    return (f"| {UFUK_AD[h]} | {n} | %{ort:+.2f} | %{med:+.2f} | %{isabet:.0f} "
            f"(kör %{kor_isabet:.0f}) | %{avantaj:+.2f} | %{net:+.2f} {isaret} |",
            (avantaj, net, isabet, n))


def rapor_olustur(ist, coin_sayisi, sure_dk):
    satirlar = [
        "# Kripto RSI Alarmları — Geriye Dönük Test",
        f"Oluşturulma: {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}",
        f"Kapsam: Binance vadelide de olan, hacmi en yüksek {coin_sayisi} coin, "
        f"son {TEST_GUN} gün, süre {sure_dk:.0f} dk\n",
        "## Nasıl okunur",
        "- **Aşırı satım (RSI ≤ 20)** → LONG açsaydın: getiri pozitifse iyi.",
        "- **Aşırı alım (RSI ≥ 75)** → SHORT açsaydın: getiri negatifse iyi.",
        "- **Kör**: aynı coinlerde rastgele bir anda aynı yönde girseydin ne olurdu.",
        "- **Avantaj**: alarmın kör çizgiye göre fazladan getirisi (işlem yönünde).",
        f"- **Net**: işlem yönündeki getiri − %{KOMISYON_PCT:.2f} komisyon (fonlama ücreti hariç).",
        "- ✅ avantaj > %0.5 ve net pozitif | ➖ küçük avantaj | ❌ kör çizgiden kötü",
        "",
    ]
    ozet_en_iyi = []
    for zd, _, bar_saat in ZAMAN_DILIMLERI:
        blok = ist[zd]
        satirlar.append(f"\n## {ZD_AD[zd]} grafik ({blok['coin']} coin)\n")
        for yon, baslik in (("dusuk", "🔻 Aşırı satım (RSI ≤ 20) → LONG"),
                            ("yuksek", "🔺 Aşırı alım (RSI ≥ 75) → SHORT")):
            ilk_ufuk = next((h for h in UFUKLAR_SAAT if h % bar_saat == 0), None)
            toplam_alarm = len(blok[yon][ilk_ufuk]) if ilk_ufuk else 0
            satirlar.append(f"### {baslik}")
            satirlar.append(f"Toplam alarm: {toplam_alarm}, farklı gün: {len(blok['gunler'][yon])}\n")
            satirlar.append("| Süre | n | Ort. getiri | Medyan | İsabet | Avantaj | Net |")
            satirlar.append("|---|---|---|---|---|---|---|")
            for h in UFUKLAR_SAAT:
                if h % bar_saat:
                    continue
                satir, veri = _satir(yon, h, blok[yon][h], blok["kor"][h])
                satirlar.append(satir)
                if veri:
                    ozet_en_iyi.append((veri[0], veri[1], zd, yon, h, veri[2], veri[3]))
            satirlar.append("")

    satirlar.append("\n## Sınırlar")
    satirlar.append("- Sadece bugün listede olan coinler test edildi; geçmişte batan coinler "
                    "yok, bu sonuçları bir miktar iyimser gösterir (özellikle aşırı satım/LONG).")
    satirlar.append("- Aynı gün birçok coin birlikte alarm verebilir (piyasa geneli çöküş/ralli). "
                    "'Farklı gün' sayısı küçükse sonuç birkaç olaya dayanıyor demektir.")
    satirlar.append("- Stop-loss/kâr al uygulanmadı; sabit süre sonra çıkış varsayıldı. "
                    "Gerçek işlemde likidasyon riski ayrıca düşünülmeli.")
    rapor = "\n".join(satirlar)

    ozet_en_iyi.sort(reverse=True)
    kisa = [f"📊 Kripto RSI testi bitti ({coin_sayisi} coin, {TEST_GUN} gün)", ""]
    yon_ad = {"dusuk": "aşırı satım→LONG", "yuksek": "aşırı alım→SHORT"}
    if ozet_en_iyi:
        kisa.append("En iyi 3 kombinasyon (kör çizgiye göre avantaja göre):")
        for avantaj, net, zd, yon, h, isabet, n in ozet_en_iyi[:3]:
            kisa.append(f"• {ZD_AD[zd]}, {yon_ad[yon]}, {UFUK_AD[h]} sonra: "
                        f"avantaj %{avantaj:+.2f}, net %{net:+.2f}, isabet %{isabet:.0f} (n={n})")
        kisa.append("")
        kisa.append("En kötü 2:")
        for avantaj, net, zd, yon, h, isabet, n in ozet_en_iyi[-2:]:
            kisa.append(f"• {ZD_AD[zd]}, {yon_ad[yon]}, {UFUK_AD[h]} sonra: "
                        f"avantaj %{avantaj:+.2f}, net %{net:+.2f} (n={n})")
        if ozet_en_iyi[0][0] <= 0.5:
            kisa.append("")
            kisa.append("⚠️ Hiçbir kombinasyon kör çizgiyi belirgin şekilde (%0.5+) geçemedi - "
                        "bu RSI alarmları tek başına bir avantaj sağlamıyor görünüyor.")
    kisa.append("\nDetaylı tablo dosyada.")
    return rapor, "\n".join(kisa)


# =============================================================================
# ÇALIŞTIRICI
# =============================================================================
def rsi_testi_calistir():
    with _kilit:
        if _calisiyor["test"]:
            K.send_telegram_message("⏳ RSI testi zaten çalışıyor, bitmesini bekle.")
            return
        _calisiyor["test"] = True
    baslangic = time.time()
    try:
        evren = K.BinanceKaynak().evren()
        evren.sort(key=lambda c: c["hacim"], reverse=True)
        evren = evren[:TEST_COIN_SAYISI]
        K.send_telegram_message(
            f"🔬 Kripto RSI geriye dönük testi başladı\n"
            f"{len(evren)} coin × 3 zaman dilimi (1s / 4s / günlük), son {TEST_GUN} gün.\n"
            f"Bu 15-30 dakika sürebilir, bitince sonuç gelecek.")
        ist = _bos_istatistik()
        hatali = 0
        for j, coin in enumerate(evren, 1):
            for zd, aralik, bar_saat in ZAMAN_DILIMLERI:
                try:
                    mumlar = _gecmis_kapanislar(coin["kaynak_kodu"], aralik, bar_saat)
                    coin_isle(ist, zd, bar_saat, mumlar)
                except Exception as e:
                    hatali += 1
                    print(f"[RSI test] {coin['sembol']} {zd} hata: {str(e)[:100]}", flush=True)
                time.sleep(0.12)
            if j % 10 == 0:
                print(f"[RSI test] {j}/{len(evren)} coin tamam", flush=True)

        rapor, kisa = rapor_olustur(ist, len(evren), (time.time() - baslangic) / 60)
        if hatali:
            kisa += f"\n({hatali} coin/zaman dilimi verisi alınamadı, atlandı)"
        K.send_telegram_message(kisa)
        yol = os.path.join(K.DATA_DIR, "kripto_rsi_test_raporu.md")
        os.makedirs(K.DATA_DIR, exist_ok=True)
        with open(yol, "w", encoding="utf-8") as f:
            f.write(rapor)
        _dosya_gonder(yol, "📄 Kripto RSI test raporu (detaylı)")
    except Exception as e:
        K.send_telegram_message(f"❌ RSI testi hatası: {e}")
    finally:
        with _kilit:
            _calisiyor["test"] = False


def _dosya_gonder(yol, aciklama):
    if not K.TELEGRAM_TOKEN or not K.TELEGRAM_CHAT_ID:
        print(f"[RSI test] dosya: {yol}", flush=True)
        return
    try:
        with open(yol, "rb") as f:
            K._OTURUM.post(f"https://api.telegram.org/bot{K.TELEGRAM_TOKEN}/sendDocument",
                           data={"chat_id": K.TELEGRAM_CHAT_ID, "caption": aciklama},
                           files={"document": f}, timeout=60)
    except Exception as e:
        print(f"[RSI test] dosya gönderilemedi: {e}", flush=True)


# =============================================================================
# KOMUT DİNLEYİCİ — kripto sohbetinin (ARGE_TELEGRAM_TOKEN) TEK dinleyicisi.
# ⚠️ arge_botu'nun kendi komut döngüsü (BIST_AKTIF=True iken) AYNI token'ı
# dinler - ikisi aynı anda çalışırsa 409 Conflict olur. ana.py bu döngüyü
# sadece BIST kapalıyken başlatıyor.
# =============================================================================
def komut_dongusu():
    if not K.TELEGRAM_TOKEN:
        return
    offset = None
    while True:
        try:
            params = {"timeout": 50}
            if offset is not None:
                params["offset"] = offset
            r = K._OTURUM.get(f"https://api.telegram.org/bot{K.TELEGRAM_TOKEN}/getUpdates",
                              params=params, timeout=60)
            for u in r.json().get("result", []):
                offset = u["update_id"] + 1
                mesaj = u.get("message") or {}
                if str(mesaj.get("chat", {}).get("id")) != str(K.TELEGRAM_CHAT_ID):
                    continue
                metin = (mesaj.get("text") or "").strip().lower()
                if metin.startswith("/rsi_test2"):
                    import kripto_rsi_dogrulama as D  # döngüsel import olmasın diye burada
                    threading.Thread(target=D.dogrulama_calistir, daemon=True).start()
                elif metin.startswith("/rsi_test"):
                    threading.Thread(target=rsi_testi_calistir, daemon=True).start()
                elif metin.startswith("/yardim") or metin.startswith("/start"):
                    K.send_telegram_message(
                        "Komutlar:\n/rsi_test — RSI alarmlarının son 2 yıldaki "
                        "başarısını 1s / 4s / günlük grafiklerde test eder (15-30 dk).\n"
                        "/rsi_test2 — aşırı satım → LONG bulgusunun doğrulaması: yıllara göre, "
                        "kalabalık günler hariç, yol boyunca düşüş ve stop etkisi (10-20 dk).")
        except Exception as e:
            print(f"[RSI komut] hata: {e}", flush=True)
            time.sleep(10)
