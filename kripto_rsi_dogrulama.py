"""
kripto_rsi_dogrulama.py — RSI AŞIRI SATIM BULGUSUNUN DOĞRULAMA TESTİ (2. tur)
==============================================================================
2026-10-26 — İlk test (/rsi_test) 4 saatlik grafikte RSI ≤ 20 → LONG için
güçlü bir sonuç verdi (3 gün sonra ort. +%5.9, isabet %73, kör çizgi %46).
Aşırı alım → SHORT her yerde başarısızdı, bu testte YOK.

Bu test, bulgunun gerçekten sağlam olup olmadığını üç açıdan sınar:
  1) YILLARA GÖRE: Son 2 yıl ikiye bölünüyor. Avantaj iki yılda da var mı,
     yoksa tek bir dönemden mi geliyor?
  2) KALABALIK GÜNLER: Alarmların çoğu piyasa geneli çöküş günlerinde
     toplanıyor. (a) Her gün eşit ağırlıklı ortalama, (b) en kalabalık 10
     gün çıkarıldığında sonuç - avantaj birkaç büyük olaydan mı geliyor?
  3) YOL BOYUNCA DÜŞÜŞ: Alarmdan sonra, belirlenen süre içinde fiyat en fazla
     ne kadar düştü (mumun en düşük fiyatına göre)? Ve -%5 / -%10 / -%15 /
     -%20 stop konsaydı sonuç ne olurdu? Bu, güvenli kaldıracı ve stop
     mesafesini söyler.

KOMUT: /rsi_test2 (kripto sohbetinde). ~10-20 dk.
"""
import os
import time
import threading
from collections import defaultdict
from datetime import datetime, timezone

import kripto_rsi_tarayici as K
import kripto_rsi_backtest as B

UFUKLAR = [24, 72]                 # 1 gün, 3 gün (ilk testte en iyi çıkanlar)
STOPLAR = [-0.05, -0.10, -0.15, -0.20]
KALABALIK_GUN_SAYISI = 10
KOMISYON = B.KOMISYON_PCT / 100

_kilit = threading.Lock()
_calisiyor = {"test": False}


def _gecmis_mumlar(sembol, aralik, bar_saat):
    """[(zaman_ms, kapanis, en_dusuk)] - son (kapanmamış) mum hariç."""
    bas_ms = int((time.time() - B.TEST_GUN * 86400) * 1000)
    adim_ms = bar_saat * 3600 * 1000
    sonuc = []
    while True:
        veri = K._get(f"{K.BinanceKaynak.taban}/api/v3/klines",
                      {"symbol": sembol, "interval": aralik,
                       "startTime": bas_ms, "limit": 1000})
        if not veri:
            break
        sonuc.extend((int(m[0]), float(m[4]), float(m[3])) for m in veri)
        if len(veri) < 1000:
            break
        bas_ms = int(veri[-1][0]) + adim_ms
        time.sleep(0.12)
    return sonuc[:-1] if sonuc else sonuc


def _yil(zaman_ms, bas_ms):
    return 1 if zaman_ms < bas_ms + 365 * 86400 * 1000 else 2


def coin_dogrula(ist, zd, bar_saat, mumlar, bas_ms):
    if len(mumlar) < K.RSI_PERIYOT + 10:
        return
    zaman = [m[0] for m in mumlar]
    kap = [m[1] for m in mumlar]
    dusuk = [m[2] for m in mumlar]
    n = len(kap)
    rsi = B.rsi_serisi(kap)
    blok = ist[zd]

    for h in UFUKLAR:
        if h % bar_saat:
            continue
        k = h // bar_saat
        for i in range(K.RSI_PERIYOT, n - k):
            g = kap[i + k] / kap[i] - 1
            kor = blok["kor"][h][_yil(zaman[i], bas_ms)]
            kor[0] += g
            kor[1] += 1
            kor[2] += g > 0

    for i in alarm_listesi(rsi):
        kayit = {"gun": datetime.fromtimestamp(zaman[i] / 1000, tz=timezone.utc).strftime("%Y-%m-%d"),
                 "yil": _yil(zaman[i], bas_ms), "getiri": {}, "dusus": {}, "stoplu": {}}
        giris = kap[i]
        for h in UFUKLAR:
            if h % bar_saat:
                continue
            k = h // bar_saat
            if i + k >= n:
                continue
            kayit["getiri"][h] = kap[i + k] / giris - 1
            kayit["dusus"][h] = min(dusuk[i + 1:i + k + 1]) / giris - 1
            stoplu = {}
            for s in STOPLAR:
                sonuc = kayit["getiri"][h]
                for j in range(i + 1, i + k + 1):
                    if dusuk[j] / giris - 1 <= s:
                        sonuc = s
                        break
                stoplu[s] = sonuc
            kayit["stoplu"][h] = stoplu
        if kayit["getiri"]:
            blok["alarmlar"].append(kayit)


def alarm_listesi(rsi):
    return B.alarm_indeksleri(rsi, K.RSI_ESIK, K.RSI_YENIDEN_KURMA, "dusuk")


def _bos():
    return {zd: {"kor": {h: {1: [0.0, 0, 0], 2: [0.0, 0, 0]} for h in UFUKLAR},
                 "alarmlar": []}
            for zd, _, _ in B.ZAMAN_DILIMLERI}


def _yuzdelik(liste, p):
    s = sorted(liste)
    if not s:
        return 0.0
    return s[min(len(s) - 1, int(p * (len(s) - 1)))]


def _ozet(getiriler):
    n = len(getiriler)
    if n == 0:
        return 0, 0.0, 0.0
    return n, sum(getiriler) / n * 100, sum(g > 0 for g in getiriler) / n * 100


def rapor(ist, coin_sayisi, sure_dk):
    s = ["# RSI Aşırı Satım → LONG — Doğrulama Testi (2. tur)",
         f"Oluşturulma: {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}",
         f"{coin_sayisi} coin, son {B.TEST_GUN} gün (1. yıl = eski yarı, 2. yıl = son 365 gün), "
         f"süre {sure_dk:.0f} dk. Tüm getiriler komisyon ÖNCESİ, 'net' olanlar -%{B.KOMISYON_PCT:.2f}.\n"]
    kisa = [f"🔬 RSI doğrulama testi bitti ({coin_sayisi} coin)", ""]

    for zd, _, bar_saat in B.ZAMAN_DILIMLERI:
        blok = ist[zd]
        alarmlar = blok["alarmlar"]
        s.append(f"\n## {B.ZD_AD[zd]} grafik — {len(alarmlar)} alarm\n")
        for h in UFUKLAR:
            if h % bar_saat:
                continue
            ad = B.UFUK_AD[h]
            liste = [a for a in alarmlar if h in a["getiri"]]
            if len(liste) < B.MIN_ORNEK:
                s.append(f"### {ad} sonra: örnek yetersiz (n={len(liste)})\n")
                continue
            s.append(f"### {ad} sonra\n")

            # 1) Yıllara göre
            s.append("**1) Yıllara göre**\n")
            s.append("| Dönem | n | Ort. | İsabet | Kör ort. | Kör isabet | Avantaj |")
            s.append("|---|---|---|---|---|---|---|")
            yil_avantaj = {}
            for yil in (1, 2):
                n, ort, isabet = _ozet([a["getiri"][h] for a in liste if a["yil"] == yil])
                kor = blok["kor"][h][yil]
                kor_ort = kor[0] / kor[1] * 100 if kor[1] else 0.0
                kor_isabet = kor[2] / kor[1] * 100 if kor[1] else 0.0
                yil_avantaj[yil] = (ort - kor_ort, n)
                s.append(f"| {yil}. yıl | {n} | %{ort:+.2f} | %{isabet:.0f} | %{kor_ort:+.2f} | "
                         f"%{kor_isabet:.0f} | %{ort - kor_ort:+.2f} |")

            # 2) Kalabalık günler
            gunluk = defaultdict(list)
            for a in liste:
                gunluk[a["gun"]].append(a["getiri"][h])
            gun_esit = sum(sum(v) / len(v) for v in gunluk.values()) / len(gunluk) * 100
            gun_isabet = sum(sum(v) / len(v) > 0 for v in gunluk.values()) / len(gunluk) * 100
            kalabalik = sorted(gunluk, key=lambda g: len(gunluk[g]), reverse=True)[:KALABALIK_GUN_SAYISI]
            kalabalik_pay = sum(len(gunluk[g]) for g in kalabalik) / len(liste) * 100
            kalan = [a["getiri"][h] for a in liste if a["gun"] not in kalabalik]
            n_k, ort_k, isabet_k = _ozet(kalan)
            n_t, ort_t, isabet_t = _ozet([a["getiri"][h] for a in liste])
            s.append("\n**2) Kalabalık günlerin etkisi**\n")
            s.append(f"- Tüm alarmlar: n={n_t}, ort %{ort_t:+.2f}, isabet %{isabet_t:.0f}")
            s.append(f"- Her gün eşit ağırlıklı ({len(gunluk)} farklı gün): ort %{gun_esit:+.2f}, "
                     f"kârlı gün %{gun_isabet:.0f}")
            s.append(f"- En kalabalık {KALABALIK_GUN_SAYISI} gün alarmların %{kalabalik_pay:.0f}'i "
                     f"({', '.join(kalabalik[:5])}...)")
            s.append(f"- Bu {KALABALIK_GUN_SAYISI} gün ÇIKARILINCA: n={n_k}, ort %{ort_k:+.2f}, "
                     f"isabet %{isabet_k:.0f}")

            # 3) Yol boyunca düşüş + stop
            dususler = [a["dusus"][h] * 100 for a in liste]
            medyan_d = _yuzdelik(dususler, 0.5)
            kotu25 = _yuzdelik(dususler, 0.25)
            kotu10 = _yuzdelik(dususler, 0.10)
            pay10 = sum(d <= -10 for d in dususler) / len(dususler) * 100
            pay20 = sum(d <= -20 for d in dususler) / len(dususler) * 100
            s.append("\n**3) Alarmdan sonra en derin düşüş (toparlanmadan önce)**\n")
            s.append(f"- Medyan: %{medyan_d:.1f} | En kötü %25'lik dilim: %{kotu25:.1f} | "
                     f"En kötü %10'luk dilim: %{kotu10:.1f}")
            s.append(f"- Alarmların %{pay10:.0f}'inde fiyat önce %10+, %{pay20:.0f}'inde %20+ düştü")
            guvenli_kaldirac = max(1, int(80 / abs(kotu10))) if kotu10 < 0 else 10
            s.append(f"- Kabaca: stopsuz tutulsaydı, alarmların %90'ında likide olmamak için "
                     f"kaldıraç en fazla ~{guvenli_kaldirac}x (bakım marjı için pay bırakılarak)")
            s.append("\n| Stop | Ort. net | İsabet | Stopa takılan |")
            s.append("|---|---|---|---|")
            net_stopsuz = ort_t - B.KOMISYON_PCT
            s.append(f"| Stopsuz | %{net_stopsuz:+.2f} | %{isabet_t:.0f} | - |")
            en_iyi_stop = (net_stopsuz, "stopsuz")
            for st in STOPLAR:
                sonuclar = [a["stoplu"][h][st] for a in liste]
                n_s, ort_s, isabet_s = _ozet(sonuclar)
                takilan = sum(a["stoplu"][h][st] == st for a in liste) / len(liste) * 100
                net = ort_s - B.KOMISYON_PCT
                if net > en_iyi_stop[0]:
                    en_iyi_stop = (net, f"%{st * 100:.0f}")
                s.append(f"| %{st * 100:.0f} | %{net:+.2f} | %{isabet_s:.0f} | %{takilan:.0f} |")
            s.append("")

            if h == 72:
                a1, n1 = yil_avantaj[1]
                a2, n2 = yil_avantaj[2]
                kisa.append(f"{B.ZD_AD[zd]} ({ad} sonra):")
                kisa.append(f"  1. yıl avantaj %{a1:+.1f} (n={n1}) | 2. yıl %{a2:+.1f} (n={n2})")
                kisa.append(f"  En kalabalık 10 gün hariç: ort %{ort_k:+.1f}, isabet %{isabet_k:.0f}")
                kisa.append(f"  Ara düşüş: medyan %{medyan_d:.0f}, en kötü %10'luk dilim %{kotu10:.0f}")
                kisa.append(f"  En iyi stop: {en_iyi_stop[1]} (net %{en_iyi_stop[0]:+.1f})")
                kisa.append("")

    s.append("\n## Nasıl yorumlanır")
    s.append("- **Yıllar**: avantaj iki yılda da pozitifse bulgu tek bir döneme bağlı değil.")
    s.append("- **Kalabalık günler**: en kalabalık 10 gün çıkınca da pozitif kalıyorsa, sonuç "
             "birkaç büyük çöküşün toparlanmasından ibaret değil.")
    s.append("- **Düşüş**: 'en kötü %10'luk dilim' değeri, stopun ve kaldıracın ne kadar geniş "
             "olması gerektiğini gösterir. Stoplu sonuç stopsuzdan kötüyse, sinyal 'önce daha "
             "da düşüp sonra toparlanan' türden demektir - dar stop onu öldürür.")
    s.append("- Hayatta kalan yanılgısı (sadece bugünkü coinler) bu testte de geçerli.")
    kisa.append("Detaylar dosyada.")
    return "\n".join(s), "\n".join(kisa)


def dogrulama_calistir():
    with _kilit:
        if _calisiyor["test"]:
            K.send_telegram_message("⏳ Doğrulama testi zaten çalışıyor.")
            return
        _calisiyor["test"] = True
    baslangic = time.time()
    try:
        evren = K.BinanceKaynak().evren()
        evren.sort(key=lambda c: c["hacim"], reverse=True)
        evren = evren[:B.TEST_COIN_SAYISI]
        bas_ms = int((time.time() - B.TEST_GUN * 86400) * 1000)
        K.send_telegram_message(
            f"🔬 RSI doğrulama testi (2. tur) başladı\n{len(evren)} coin, sadece aşırı satım → LONG.\n"
            f"Yıllara göre, kalabalık günler hariç ve yol boyunca düşüş ölçülüyor. ~10-20 dk.")
        ist = _bos()
        hatali = 0
        for j, coin in enumerate(evren, 1):
            for zd, aralik, bar_saat in B.ZAMAN_DILIMLERI:
                try:
                    coin_dogrula(ist, zd, bar_saat,
                                 _gecmis_mumlar(coin["kaynak_kodu"], aralik, bar_saat), bas_ms)
                except Exception as e:
                    hatali += 1
                    print(f"[RSI doğrulama] {coin['sembol']} {zd}: {str(e)[:100]}", flush=True)
                time.sleep(0.12)
            if j % 10 == 0:
                print(f"[RSI doğrulama] {j}/{len(evren)}", flush=True)
        uzun, kisa = rapor(ist, len(evren), (time.time() - baslangic) / 60)
        if hatali:
            kisa += f"\n({hatali} coin/zaman dilimi atlandı)"
        K.send_telegram_message(kisa)
        yol = os.path.join(K.DATA_DIR, "kripto_rsi_dogrulama_raporu.md")
        os.makedirs(K.DATA_DIR, exist_ok=True)
        with open(yol, "w", encoding="utf-8") as f:
            f.write(uzun)
        B._dosya_gonder(yol, "📄 RSI doğrulama raporu (detaylı)")
    except Exception as e:
        K.send_telegram_message(f"❌ Doğrulama testi hatası: {e}")
    finally:
        with _kilit:
            _calisiyor["test"] = False
