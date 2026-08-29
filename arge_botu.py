"""
arge_botu.py — BIST TAVAN YAKLAŞANLARI TARAYICISI
==================================================
2026-08-19 — Kullanıcının kararıyla eski Ar-Ge botu (8000+ satır, onlarca
araştırma komutu, otomatik AI hipotez üretimi) TAMAMEN KALDIRILDI ve
yerine bu tarayıcı kondu. Eski hali arge_botu_ESKI_YEDEK.py'de ve
GitHub geçmişinde duruyor.

NE YAPIYOR — TEK İŞ:
BIST kapanışına yakın saatlerde (16:00-18:10 TR), tavana doğru hızla
ilerleyen ama HENÜZ TAVAN OLMAMIŞ hisseleri bulup bildirim gönderir.
Strateji iddiası YOK, tahmin YOK - sadece bir tarayıcı.

NEDEN BU EŞİKLER:
Bugünkü BIST araştırması şunu gösterdi: TAM TAVAN yapanlar ertesi gün
ort. +%2.42 yukarı açıyor (%81 ihtimalle), ama %8-9.5'te kapananlar
sadece +%0.39 (%48 ihtimalle - yazı tura). Yani değerli olan şey TAVAN
KİLİDİNİN KENDİSİ. Bu tarayıcı, tavan olmadan ÖNCE fark etmeni sağlıyor
ki karar verecek vaktin olsun.

DÜRÜST SINIR — VERİ GECİKMESİ:
yfinance BIST verisi ~15 dakika gecikmeli. Yani gördüğün fiyat 15 dakika
öncesinin. Kapanışa 70+ dakika varken bu genelde sorun değil ama son
15-20 dakikada körleşiyorsun. Gerçek zamanlı veri istersen Algolab
(Deniz Yatırım) hesabı gerekiyor - o zaman SADECE veri çekme kısmı
değişir, geri kalan aynı kalır.

Telegram komutları: /durum, /tara (elle tarama), /liste
"""
import os
import gc
import time
import threading
from datetime import datetime, timezone

import numpy as np
import pandas as pd
import requests

ARGE_KOD_SURUMU = "tavan-tarayici-v5-bellek-duzeltmesi-2026-08-28"

TELEGRAM_TOKEN = os.environ.get("ARGE_TELEGRAM_TOKEN", "")
TELEGRAM_CHAT_ID = os.environ.get("ARGE_TELEGRAM_CHAT_ID", "")
DATA_DIR = os.environ.get("DATA_DIR", ".")

# --- TARAMA AYARLARI ---
TARAMA_ARALIGI_SANIYE = 300 # 5 dakika

# 2026-08-19 EKLENDI: minimum GUNLUK TL ISLEM HACMI filtresi.
# Kullanicinin istegi: "cok olu, giremiyecegim hisseler gelmesin ama
# ayni zamanda digerlerini de kacirmayalim."
# 2 milyon TL secildi: gercekten olu hisseleri (birkac yuz bin TL)
# eler, ama orta-kucuk hisseleri kacirmaz. Cok yuksek yapmak kucuk
# hisseleri (tavanlarin cogu orada oluyor) disarida birakirdi.
# Render'da MIN_TL_HACIM degiskeniyle kod degistirmeden ayarlanabilir.
MIN_TL_HACIM = float(os.environ.get("MIN_TL_HACIM", "2000000"))

# 2026-08-28 BELLEK: hisseler kac'arli gruplar halinde cekilip islenecek.
# 414 hisseyi tek seferde cekmek ~2500 sutunluk dev bir DataFrame
# yaratiyordu ve 5 dakikada bir tekrarlaniyordu; Render'in 512MB'lik
# ucretsiz plani birkac saat icinde tukenip surec sessizce donuyordu.
# 100'luk gruplar tepe bellegi ~4 kat dusuruyor. Render'da PARCA_BOYUTU
# degiskeniyle kod degistirmeden ayarlanabilir.
PARCA_BOYUTU = int(os.environ.get("PARCA_BOYUTU", "100"))

# 2026-08-19 DEĞİŞTİRİLDİ: tarama artık GÜN BOYU (10:00-18:15 TR).
# Kullanıcının isteği: "tam gaz tavana giden trene erken binmek."
PENCERE_BASLANGIC = 10 * 60      # 10:00 TR (BIST açılışı)
PENCERE_BITIS = 18 * 60 + 15     # 18:15 TR

# SAAT DUYARLI EŞİKLER — neden gerekli:
# Araştırmamız (bist_tavan.py + bist_tavana_yakin.py) şunu gösterdi:
#   - GERÇEKTEN tavan kilitlenirse ertesi gün +%2.42 açıyor (%81 ihtimal)
#   - Tavana yakın (%8-9.5) ama kilitlenmeden kapanırsa sadece +%0.39
# Yani asıl değer, tavanın KİLİTLENMESİNDE. Bu yüzden:
#   - Sabah erken: hissenin önünde 7-8 saat var, hem kilitlenebilir hem
#     sönebilir → DAHA GÜÇLÜ kanıt istiyoruz (yüksek getiri + yüksek hız)
#   - Kapanışa yakın: az zaman kaldı, mevcut seviye çok daha güvenilir
#     bir gösterge → eşik gevşiyor
# Format: (bitiş_dakikası, min_getiri_pct, min_son1saat_hiz_pct, etiket)
SAAT_ESIKLERI = [
    (12 * 60,        8.5, 2.5, "🌅 ERKEN (10:00-12:00)"),
    (15 * 60,        8.0, 2.0, "☀️ ÖĞLE (12:00-15:00)"),
    (17 * 60,        7.5, 1.0, "🌇 İKİNDİ (15:00-17:00)"),
    (18 * 60 + 15,   7.0, 0.0, "🌆 KAPANIŞA YAKIN (17:00-18:15)"),
]
UST_ESIK_PCT = 9.49         # bunun ustu zaten TAVAN, gec kalmis olurduk
TEKRAR_BILDIRIM_ARTIS = 0.5 # tekrar bildirim icin en az bu kadar yukselmeli
TARAMA_ARALIGI_SANIYE = 300 # 5 dakika


def _gecerli_esikler():
    """Şu anki TR saatine göre (min_getiri, min_hiz, etiket) döner."""
    simdi = _tr_dakika()
    for bitis, min_getiri, min_hiz, etiket in SAAT_ESIKLERI:
        if simdi <= bitis:
            return min_getiri, min_hiz, etiket
    son = SAAT_ESIKLERI[-1]
    return son[1], son[2], son[3]


BIST_HISSELER = [
    # --- buyuk/orta olcekli (mevcut liste) ---
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
    "MAVI.IS", "BRYAT.IS", "AGHOL.IS", "KARSN.IS", "OTKAR.IS", "KLSER.IS",
    "EGEEN.IS", "ALTNY.IS", "REEDR.IS", "IZINV.IS", "MIATK.IS", "FORTE.IS",
    # --- 2026-08-19 EKLENDİ: KÜÇÜK/ORTA ölçekli hisseler ---
    # Kullanıcının haklı tespiti: tavan hareketleri ağırlıklı olarak
    # KÜÇÜK hisselerde oluyor, büyüklerde nadir. Liste bu yüzden ciddi
    # şekilde genişletildi.
    # DÜRÜST NOT: bu liste genel bilgime dayanıyor, canlı bir BIST
    # taraması değil - bazı kodlar değişmiş/işlemden kalkmış olabilir.
    # Veri gelmeyen hisseler otomatik atlanır (kod bunu zaten yapıyor).
    "ADESE.IS", "AFYON.IS", "AGYO.IS", "AHGAZ.IS", "AKFGY.IS", "AKSA.IS",
    "AKSUE.IS", "ALCAR.IS", "ALKIM.IS", "ANELE.IS", "ARENA.IS", "ARSAN.IS",
    "ASUZU.IS", "ATAKP.IS", "ATEKS.IS", "AVOD.IS", "AYCES.IS", "AYDEM.IS",
    "AYEN.IS", "AZTEK.IS", "BAGFS.IS", "BAKAB.IS", "BALAT.IS", "BARMA.IS",
    "BASGZ.IS", "BAYRK.IS", "BERA.IS", "BEYAZ.IS", "BIENY.IS", "BIGCH.IS",
    "BIOEN.IS", "BLCYT.IS", "BMSCH.IS", "BMSTL.IS", "BNTAS.IS", "BOBET.IS",
    "BORSK.IS", "BOSSA.IS", "BRISA.IS", "BRKSN.IS", "BRLSM.IS", "BSOKE.IS",
    "BUCIM.IS", "BURCE.IS", "BURVA.IS", "CANTE.IS", "CASA.IS", "CATES.IS",
    "CELHA.IS", "CEMAS.IS", "CEMTS.IS", "CEOEM.IS", "CMBTN.IS", "CONSE.IS",
    "COSMO.IS", "CRDFA.IS", "CUSAN.IS", "DAGI.IS", "DAPGM.IS", "DARDL.IS",
    "DENGE.IS", "DERHL.IS", "DERIM.IS", "DESA.IS", "DESPC.IS", "DGATE.IS",
    "DGNMO.IS", "DIRIT.IS", "DITAS.IS", "DMSAS.IS", "DOBUR.IS", "DOCO.IS",
    "DOFER.IS", "DURDO.IS", "DYOBY.IS", "EBEBK.IS", "ECILC.IS", "EDATA.IS",
    "EGGUB.IS", "EGPRO.IS", "EKIZ.IS", "EKSUN.IS", "ELITE.IS", "EMKEL.IS",
    "ENERY.IS", "ENSRI.IS", "EPLAS.IS", "ERBOS.IS", "ERCB.IS", "ERSU.IS",
    "ESCAR.IS", "ESCOM.IS", "ESEN.IS", "ETILR.IS", "EUKYO.IS", "EUYO.IS",
    "FADE.IS", "FENER.IS", "FLAP.IS", "FMIZP.IS", "FONET.IS", "FRIGO.IS",
    "GARFA.IS", "GEDIK.IS", "GENIL.IS", "GENTS.IS", "GEREL.IS", "GLBMD.IS",
    "GLCVY.IS", "GLRYH.IS", "GMTAS.IS", "GOKNR.IS", "GOLTS.IS", "GOODY.IS",
    "GRNYO.IS", "GRSEL.IS", "GRTRK.IS", "GSDDE.IS", "GSDHO.IS", "GSRAY.IS",
    "GWIND.IS", "GZNMI.IS", "HATEK.IS", "HATSN.IS", "HDFGS.IS", "HKTM.IS",
    "HLGYO.IS", "HUBVC.IS", "HUNER.IS", "HURGZ.IS", "ICBCT.IS", "ICUGS.IS",
    "IDGYO.IS", "IHAAS.IS", "IHEVA.IS", "IHGZT.IS", "IHLAS.IS", "IHLGM.IS",
    "IHYAY.IS", "IMASM.IS", "INDES.IS", "INFO.IS", "INGRM.IS", "INTEM.IS",
    "INVEO.IS", "INVES.IS", "ISBIR.IS", "ISDEM.IS", "ISFIN.IS", "ISGSY.IS",
    "ISKPL.IS", "ISMEN.IS", "ISSEN.IS", "ISYAT.IS", "IZENR.IS", "IZFAS.IS",
    "IZMDC.IS", "JANTS.IS", "KAPLM.IS", "KAREL.IS", "KARTN.IS", "KATMR.IS",
    "KAYSE.IS", "KBORU.IS", "KCAER.IS", "KFEIN.IS", "KGYO.IS", "KIMMR.IS",
    "KLGYO.IS", "KLKIM.IS", "KLMSN.IS", "KLRHO.IS", "KMPUR.IS", "KNFRT.IS",
    "KOCMT.IS", "KONKA.IS", "KONYA.IS", "KOPOL.IS", "KORDS.IS", "KRGYO.IS",
    "KRONT.IS", "KRPLS.IS", "KRSTL.IS", "KRVGD.IS", "KTLEV.IS", "KTSKR.IS",
    "KUTPO.IS", "KUYAS.IS", "KZBGY.IS", "LIDER.IS", "LIDFA.IS", "LILAK.IS",
    "LINK.IS", "LKMNH.IS", "LOGO.IS", "LUKSK.IS", "MAALT.IS", "MACKO.IS",
    "MAKIM.IS", "MAKTK.IS", "MANAS.IS", "MARBL.IS", "MARKA.IS", "MARTI.IS",
    "MEDTR.IS", "MEGAP.IS", "MEKAG.IS", "MEPET.IS", "MERCN.IS", "MERIT.IS",
    "METRO.IS", "METUR.IS", "MHRGY.IS", "MMCAS.IS", "MNDRS.IS", "MOBTL.IS",
    "MPARK.IS", "MRGYO.IS", "MRSHL.IS", "MSGYO.IS", "MTRKS.IS", "MZHLD.IS",
    "NATEN.IS", "NIBAS.IS", "NTGAZ.IS", "NTHOL.IS", "NUHCM.IS", "OBASE.IS",
    "OFSYM.IS", "ONCSM.IS", "ORCAY.IS", "ORGE.IS", "ORMA.IS", "OSMEN.IS",
    "OSTIM.IS", "OYAYO.IS", "OZGYO.IS", "OZKGY.IS", "OZRDN.IS", "OZSUB.IS",
    "PAGYO.IS", "PAMEL.IS", "PAPIL.IS", "PARSN.IS", "PASEU.IS", "PATEK.IS",
    "PCILT.IS", "PEKGY.IS", "PENGD.IS", "PETUN.IS", "PINSU.IS", "PKART.IS",
    "PKENT.IS", "PLTUR.IS", "PNLSN.IS", "POLTK.IS", "PRDGS.IS", "PRKAB.IS",
    "PRKME.IS", "PRZMA.IS", "PSDTC.IS", "QUAGR.IS", "RALYH.IS", "RAYSG.IS",
    "RNPOL.IS", "RODRG.IS", "ROYAL.IS", "RTALB.IS", "RUBNS.IS", "SAFKR.IS",
    "SAMAT.IS", "SANEL.IS", "SANFM.IS", "SANKO.IS", "SARKY.IS", "SAYAS.IS",
    "SDTTR.IS", "SEGYO.IS", "SEKUR.IS", "SELEC.IS", "SELGD.IS", "SELVA.IS",
    "SEYKM.IS", "SILVR.IS", "SKTAS.IS", "SMART.IS", "SNGYO.IS", "SNICA.IS",
    "SNPAM.IS", "SODSN.IS", "SOKE.IS", "SONME.IS", "SUMAS.IS", "SUNTK.IS",
    "SURGY.IS", "TBORG.IS", "TDGYO.IS", "TEKTU.IS", "TERA.IS", "TEZOL.IS",
    "TGSAS.IS", "TKNSA.IS", "TLMAN.IS", "TMPOL.IS", "TMSN.IS", "TNZTP.IS",
    "TRILC.IS", "TSGYO.IS", "TSPOR.IS", "TUCLK.IS", "TUKAS.IS", "TUREX.IS",
    "TURGG.IS", "UFUK.IS", "ULAS.IS", "ULUFA.IS", "ULUSE.IS", "ULUUN.IS",
    "UNLU.IS", "USAK.IS", "UZERB.IS", "VAKKO.IS", "VANGD.IS", "VBTYZ.IS",
    "VERTU.IS", "VERUS.IS", "VKGYO.IS", "VKING.IS", "YAPRK.IS", "YATAS.IS",
    "YAYLA.IS", "YEOTK.IS", "YESIL.IS", "YGGYO.IS", "YIGIT.IS", "YKSLN.IS",
    "YONGA.IS", "YUNSA.IS", "YYAPI.IS", "YYLGD.IS", "ZEDUR.IS", "ZRGYO.IS",
]
BIST_HISSELER = list(dict.fromkeys(BIST_HISSELER))  # tekrarlari at, sirayi koru

_ARGE_AVAILABLE = bool(TELEGRAM_TOKEN and TELEGRAM_CHAT_ID)
if not _ARGE_AVAILABLE:
    print("[TARAYICI] ARGE_TELEGRAM_TOKEN/CHAT_ID tanımlı değil - "
          "tarayıcı devre dışı (ana sistemi etkilemez).", flush=True)

_bugun_bildirilen = {}   # {ticker: en_son_bildirilen_getiri}
_bugun_tarih = None
_son_tarama_ozeti = {"zaman": None, "bulunan": 0, "taranan": 0, "hata": None}
_son_update_id = None
_poll_sayac = 0
_gunluk_kayitlar = []


def send_telegram_message(text: str):
    if not _ARGE_AVAILABLE:
        print(f"[TARAYICI-Telegram kapalı] {text}", flush=True)
        return
    try:
        requests.post(f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage",
                      json={"chat_id": TELEGRAM_CHAT_ID, "text": text[:4000]}, timeout=20)
    except Exception as e:
        print(f"[TARAYICI-Telegram hata] {e}", flush=True)


def send_telegram_document(dosya_yolu: str, caption: str = ""):
    if not _ARGE_AVAILABLE:
        print(f"[TARAYICI-Telegram kapalı] {dosya_yolu}", flush=True)
        return
    try:
        with open(dosya_yolu, "rb") as f:
            requests.post(f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendDocument",
                          data={"chat_id": TELEGRAM_CHAT_ID, "caption": caption[:1024]},
                          files={"document": f}, timeout=60)
    except Exception as e:
        print(f"[TARAYICI-Telegram dosya hatası] {e}", flush=True)


def _tr_dakika():
    """Turkiye saatiyle gunun kacinci dakikasi (UTC+3)."""
    u = datetime.now(timezone.utc)
    return ((u.hour + 3) % 24) * 60 + u.minute


def pencere_icinde_mi():
    u = datetime.now(timezone.utc)
    if u.weekday() >= 5:
        return False
    return PENCERE_BASLANGIC <= _tr_dakika() <= PENCERE_BITIS


def _toplu_veri_cek(tickers=None, sert_sure=90):
    """Verilen hisseleri TEK istekte ceker.
    2026-08-28 BELLEK DUZELTMESI: Once TUM 414 hisse tek seferde
    cekiliyordu - bu, ~2500 sutunluk dev bir DataFrame demek ve 5
    dakikada bir tekrarlaniyordu. Zaman asimi olunca thread olmuyor,
    veriyi tutmaya devam ediyordu; Render'in 512MB'lik ucretsiz plani
    birkac saat icinde tukeniyor ve surec sessizce donuyordu
    (kullanicinin defalarca yasadigi 'yeniden baslatinca duzeliyor,
    sonra yine donuyor' sorununun muhtemel kok sebebi).
    Artik PARCA PARCA cekiliyor ve her parca islenip hemen atiliyor -
    tepe bellek kullanimi ~4 kat dusuyor. Ayrica period 5g -> 3g
    (hacim ortalamasi icin 2 onceki gun yeterli)."""
    import concurrent.futures
    import yfinance as yf

    hedef = tickers if tickers is not None else BIST_HISSELER

    def _cek():
        return yf.download(tickers=" ".join(hedef), period="3d",
                           interval="15m", group_by="ticker", progress=False,
                           threads=False, auto_adjust=False)

    ex = concurrent.futures.ThreadPoolExecutor(max_workers=1)
    try:
        return ex.submit(_cek).result(timeout=sert_sure)
    except concurrent.futures.TimeoutError:
        print("[TARAYICI] Toplu veri çekimi SERT zaman aşımına uğradı.", flush=True)
        return None
    except Exception as e:
        print(f"[TARAYICI] Veri hatası: {e}", flush=True)
        return None
    finally:
        ex.shutdown(wait=False)


def _hisse_durumu(veri, ticker):
    """Bir hissenin bugunku getirisini ve hacim oranini hesaplar.
    Doner: dict ya da None."""
    try:
        df = veri[ticker] if ticker in veri.columns.get_level_values(0) else None
        if df is None or df.empty:
            return None
        df = df.dropna(subset=["Close"])
        if len(df) < 10:
            return None
        idx = pd.to_datetime(df.index)
        try:
            idx = idx.tz_localize(None)
        except Exception:
            idx = idx.tz_convert(None)
        df = df.copy()
        df["gun"] = idx.date

        gunler = sorted(set(df["gun"]))
        if len(gunler) < 2:
            return None
        bugun, dun = gunler[-1], gunler[-2]
        bugun_barlar = df[df["gun"] == bugun]
        dun_barlar = df[df["gun"] == dun]
        if bugun_barlar.empty or dun_barlar.empty:
            return None

        onceki_kapanis = float(dun_barlar["Close"].iloc[-1])
        son_fiyat = float(bugun_barlar["Close"].iloc[-1])
        if onceki_kapanis <= 0:
            return None
        getiri = (son_fiyat - onceki_kapanis) / onceki_kapanis * 100

        bugun_hacim = float(bugun_barlar["Volume"].sum())
        gecmis_hacimler = [float(df[df["gun"] == g]["Volume"].sum()) for g in gunler[:-1]]
        gecmis_hacimler = [h for h in gecmis_hacimler if h > 0]
        hacim_orani = (bugun_hacim / np.mean(gecmis_hacimler)) if gecmis_hacimler else None

        # 2026-08-19 EKLENDI: TL cinsinden islem hacmi (lot degil).
        # Amac: girilemeyecek kadar OLU hisseleri elemek. Bugunku her
        # barin (kapanis x hacim) toplami = yaklasik TL islem hacmi.
        tl_hacim = float((bugun_barlar["Close"] * bugun_barlar["Volume"]).sum())

        # son 1 saatte (4 bar) ne kadar hizlandi
        son4 = bugun_barlar["Close"].tail(5)
        hiz = ((son4.iloc[-1] - son4.iloc[0]) / son4.iloc[0] * 100) if len(son4) >= 2 and son4.iloc[0] > 0 else None

        return {"ticker": ticker.replace(".IS", ""), "fiyat": round(son_fiyat, 2),
                "getiri_pct": round(getiri, 2),
                "tavana_kalan_pct": round(10.0 - getiri, 2),
                "hacim_orani": round(hacim_orani, 2) if hacim_orani else None,
                "tl_hacim": tl_hacim,
                "son1saat_pct": round(hiz, 2) if hiz is not None else None,
                "son_bar_saati": bugun_barlar.index[-1].strftime("%H:%M")}
    except Exception:
        return None


def taramayi_calistir(elle=False):
    """Bir tarama turu. elle=True ise pencere kontrolu atlanir."""
    global _bugun_bildirilen, _bugun_tarih, _son_tarama_ozeti, _gunluk_kayitlar

    bugun = datetime.now(timezone.utc).date()
    if _bugun_tarih != bugun:
        _bugun_tarih = bugun
        _bugun_bildirilen = {}
        _gunluk_kayitlar = []

    min_getiri, min_hiz, dilim_etiketi = _gecerli_esikler()
    bulunanlar, taranan, hacim_elenen, hiz_elenen = [], 0, 0, 0
    zaten_tavan_atlanan = 0
    basarili_parca = 0

    # 2026-08-28: PARCA PARCA isle - her parca islenip HEMEN bellekten
    # atiliyor. Tepe bellek kullanimi tum listeyi birden tutmaya gore
    # ~4 kat dusuk. Render'in 512MB sinirinda donmalarin onune gecmek icin.
    for i in range(0, len(BIST_HISSELER), PARCA_BOYUTU):
        parca = BIST_HISSELER[i:i + PARCA_BOYUTU]
        veri = _toplu_veri_cek(parca)
        if veri is None or veri.empty:
            print(f"[TARAYICI] Parça {i//PARCA_BOYUTU + 1} verisi alınamadı, "
                  f"atlanıyor.", flush=True)
            continue
        basarili_parca += 1
        for ticker in parca:
            d = _hisse_durumu(veri, ticker)
            if d is None:
                continue
            taranan += 1
            # 2026-08-19: getiri esigini gecse bile TL hacmi cok dusukse
            # bildirme - girilemeyecek olu hisseler bildirim kirliligi
            # yaratiyordu. Sadece ELEME icin, tarama yine tum listede.
            if d.get("tl_hacim", 0) < MIN_TL_HACIM:
                if d["getiri_pct"] >= min_getiri:
                    hacim_elenen += 1
                continue
            if d["getiri_pct"] > UST_ESIK_PCT:
                # 2026-08-19 DÜZELTME: zaten TAVAN OLMUŞ hisseyi bildirmenin
                # kullanıcıya faydası yok - amaç tavan olmadan ÖNCE girmek,
                # bu bildirim "geç kaldın" demekten ibaret.
                # Artık SADECE daha önce radara girmiş bir hisse sonradan
                # kilitlenirse haber veriyoruz - o bilgi anlamlı.
                if d["ticker"] in _bugun_bildirilen:
                    d["tavan_oldu"] = True
                    bulunanlar.append(d)
                else:
                    zaten_tavan_atlanan += 1
            elif d["getiri_pct"] >= min_getiri:
                # HIZ FILTRESI - kullanicinin "tam gaz giden tren" tarifi.
                # Ayni %8, son 1 saatte hic kipirdamadan gelinmisse "tam gaz"
                # degil; son 1 saatte hizla gelinmisse tam gaz.
                hiz = d.get("son1saat_pct")
                if min_hiz > 0 and (hiz is None or hiz < min_hiz):
                    hiz_elenen += 1
                    continue
                d["dilim"] = dilim_etiketi
                bulunanlar.append(d)

        # PARCA BITTI - veriyi HEMEN bellekten at
        del veri
        gc.collect()

    if basarili_parca == 0:
        _son_tarama_ozeti = {"zaman": datetime.now().strftime("%H:%M:%S"),
                              "bulunan": 0, "taranan": 0, "hata": "veri alınamadı"}
        return []

    _son_tarama_ozeti = {"zaman": datetime.now().strftime("%H:%M:%S"),
                          "bulunan": len(bulunanlar), "taranan": taranan,
                          "hacim_elenen": hacim_elenen, "hiz_elenen": hiz_elenen,
                          "zaten_tavan_atlanan": zaten_tavan_atlanan,
                          "dilim": dilim_etiketi, "esik": f"%{min_getiri}/hız%{min_hiz}",
                          "hata": None}

    yeni_bildirimler = []
    for d in sorted(bulunanlar, key=lambda x: -x["getiri_pct"]):
        onceki = _bugun_bildirilen.get(d["ticker"])
        if onceki is not None and d["getiri_pct"] < onceki + TEKRAR_BILDIRIM_ARTIS:
            continue  # zaten bildirdik, kayda deger yukselis yok
        _bugun_bildirilen[d["ticker"]] = d["getiri_pct"]
        yeni_bildirimler.append(d)
        _gunluk_kayitlar.append({**d, "bildirim_saati": datetime.now().strftime("%H:%M")})

    if yeni_bildirimler:
        satirlar = [f"🔺 TAVANA YAKLAŞANLAR ({len(yeni_bildirimler)} hisse) "
                    f"— veri saati ~{yeni_bildirimler[0].get('son_bar_saati','?')}",
                    f"{dilim_etiketi} | eşik: %{min_getiri}+ ve son 1sa %{min_hiz}+"]
        for d in yeni_bildirimler:
            if d.get("tavan_oldu"):
                satirlar.append(f"\n🔒 {d['ticker']}: %{d['getiri_pct']} — TAVAN OLDU "
                                f"(fiyat {d['fiyat']})")
            else:
                satirlar.append(f"\n📈 {d['ticker']}: %{d['getiri_pct']} "
                                f"(tavana %{d['tavana_kalan_pct']} kaldı)")
            satirlar.append(f"   Fiyat: {d['fiyat']}" +
                            (f" | Hacim: {d['hacim_orani']}x ort." if d.get("hacim_orani") else "") +
                            (f" | İşlem: {d['tl_hacim']/1_000_000:.1f}M TL" if d.get("tl_hacim") else "") +
                            (f" | Son 1sa: %{d['son1saat_pct']}" if d.get("son1saat_pct") is not None else ""))
        satirlar.append("\n⏰ Veri ~15 dk gecikmeli olabilir - karar verirken hesaba kat.")
        if _tr_dakika() < 15 * 60:
            satirlar.append("⚠️ Erken saat: tavana kilitlenmeden sönme ihtimali "
                            "kapanışa yakın saatlere göre daha yüksek.")
        send_telegram_message("\n".join(satirlar))
    elif elle:
        send_telegram_message(f"🔍 Tarama bitti ({dilim_etiketi}): {taranan} hisse tarandı, "
                               f"%{min_getiri}+ ve son 1sa %{min_hiz}+ koşulunu sağlayan "
                               f"yeni hisse yok."
                               + (f"\n({hacim_elenen} hisse getiri eşiğini geçti ama "
                                  f"{MIN_TL_HACIM/1_000_000:.0f}M TL hacim filtresine takıldı.)"
                                  if hacim_elenen else "")
                               + (f"\n({hiz_elenen} hisse getiri eşiğini geçti ama "
                                  f"yeterince hızlı yükselmiyordu.)" if hiz_elenen else "")
                               + (f"\n({zaten_tavan_atlanan} hisse ZATEN TAVAN OLMUŞ - "
                                  f"bunlar bildirilmiyor, çünkü amaç tavan olmadan "
                                  f"önce yakalamak.)" if zaten_tavan_atlanan else ""))
    return yeni_bildirimler


def maybe_run_scan():
    """Ana bot bunu dongude cagirir - kendi kendini zamanlar."""
    if not _ARGE_AVAILABLE:
        return
    if not pencere_icinde_mi():
        return
    global _son_calisma
    simdi = time.time()
    if simdi - globals().get("_son_calisma", 0) < TARAMA_ARALIGI_SANIYE:
        return
    globals()["_son_calisma"] = simdi
    try:
        taramayi_calistir()
    except Exception as e:
        print(f"[TARAYICI] Tarama hatası: {e}", flush=True)


def send_startup_message():
    if not _ARGE_AVAILABLE:
        return
    send_telegram_message(
        f"🔺 BIST TAVAN TARAYICISI başlatıldı — {ARGE_KOD_SURUMU}\n\n"
        f"Eski Ar-Ge botu (araştırma komutları, otomatik AI hipotez üretimi) "
        f"TAMAMEN KALDIRILDI. Bu bot artık TEK İŞ yapıyor:\n\n"
        f"📊 {len(BIST_HISSELER)} BIST hissesi taranıyor\n"
        f"💧 Likidite filtresi: günlük işlem hacmi "
        f"{MIN_TL_HACIM/1_000_000:.0f}M TL altındaki hisseler bildirilmiyor "
        f"(girilemeyecek ölü hisseleri elemek için - liste yine tam "
        f"taranıyor, sadece bildirim süzülüyor)\n"
        f"🎯 SAAT DUYARLI EŞİKLER (tren ne kadar erken yakalanırsa o kadar\n"
        f"   güçlü kanıt isteniyor - sabah sönme riski yüksek, kapanışa\n"
        f"   yakın mevcut seviye daha güvenilir):\n"
        + "".join(f"   {et}: %{mg}+ ve son 1sa %{mh}+\n"
                  for _, mg, mh, et in SAAT_ESIKLERI)
        + f"🔒 Tavan olanlar da ayrıca işaretlenip bildiriliyor\n"
        f"⏰ Çalışma penceresi: 10:00-18:15 TR (GÜN BOYU), "
        f"{TARAMA_ARALIGI_SANIYE//60} dakikada bir\n"
        f"🔁 Aynı hisse için tekrar bildirim: sadece %{TEKRAR_BILDIRIM_ARTIS} "
        f"daha yükselirse (bildirim kirliliği olmasın)\n\n"
        f"⚠️ ÖNEMLİ: Veri ~15 dakika gecikmeli. Kapanışa 70+ dk varken sorun "
        f"değil ama son 15-20 dakikada körleşiyorsun. Gerçek zamanlı veri "
        f"için Algolab hesabı gerekir - o zaman sadece veri çekme kısmı "
        f"değişir.\n\n"
        f"Komutlar: /tara (elle tarama) | /durum | /liste\n\n"
        f"ℹ️ Bu bot strateji ÖNERMİYOR, tahmin YAPMIYOR - sadece tarayıp "
        f"haber veriyor. Karar tamamen sana ait."
    )


def poll_arge_commands():
    """Ana botun komut dongusu bunu cagirir."""
    global _son_update_id, _poll_sayac
    if not _ARGE_AVAILABLE:
        print("[TARAYICI TEŞHİS] _ARGE_AVAILABLE=False - token/chat_id "
              "tanımlı değil, komutlar HİÇ dinlenmiyor!", flush=True)
        return
    try:
        params = {"timeout": 5}
        if _son_update_id:
            params["offset"] = _son_update_id + 1
        r = requests.get(f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/getUpdates",
                         params=params, timeout=15)
        data = r.json()
        # 2026-08-19 TEŞHİS: Telegram API'nin kendisi hata döndürüyor mu?
        # (ornegin baska bir yerde webhook kuruluysa getUpdates calismaz)
        if not data.get("ok", True):
            print(f"[TARAYICI TEŞHİS] Telegram API HATA döndürdü: {data}", flush=True)
    except Exception as e:
        print(f"[TARAYICI] Komut alma hatası: {e}", flush=True)
        return

    _poll_sayac += 1
    if _poll_sayac % 20 == 0:
        print(f"[TARAYICI TEŞHİS] Komut dinleme nabız: çalışıyor "
              f"(sorgu #{_poll_sayac}, son gelen mesaj sayısı: "
              f"{len(data.get('result', []))})", flush=True)

    for u in data.get("result", []):
        _son_update_id = u["update_id"]
        msg = u.get("message", {})
        chat_id = str(msg.get("chat", {}).get("id", ""))
        text = (msg.get("text") or "").strip()
        # 2026-08-19 TEŞHİS: kullanıcı /tara yazdığında hiç yanıt
        # alamadığını bildirdi. Bu satır GELEN HER MESAJI loglar - sorunun
        # chat_id uyuşmazlığı mı, mesajın hiç ulaşmaması mı, yoksa başka
        # bir şey mi olduğunu KESİN gösterir (tahmin etmeyi bırakıyoruz).
        print(f"[TARAYICI TEŞHİS] Gelen mesaj: chat_id={chat_id} "
              f"(beklenen={TELEGRAM_CHAT_ID}) metin='{text}' "
              f"eşleşiyor_mu={chat_id == str(TELEGRAM_CHAT_ID)}", flush=True)
        if chat_id != str(TELEGRAM_CHAT_ID) or not text.startswith("/"):
            continue

        if text.startswith("/tara"):
            send_telegram_message("🔍 Elle tarama başlatıldı...")
            threading.Thread(target=lambda: taramayi_calistir(elle=True), daemon=True).start()
        elif text.startswith("/durum"):
            o = _son_tarama_ozeti
            send_telegram_message(
                f"📊 Tarayıcı Durumu — {ARGE_KOD_SURUMU}\n"
                f"Pencere içinde mi: {'✅ evet' if pencere_icinde_mi() else '❌ hayır (16:00-18:15 TR dışı)'}\n"
                f"Son tarama: {o['zaman'] or 'henüz yok'}\n"
                f"Taranan hisse: {o['taranan']} | Bulunan: {o['bulunan']}\n"
                f"Hata: {o['hata'] or 'yok'}\n"
                f"Bugün bildirilen: {len(_bugun_bildirilen)} hisse"
            )
        elif text.startswith("/liste"):
            if not _gunluk_kayitlar:
                send_telegram_message("Bugün henüz bildirim yok.")
                continue
            yol = os.path.join(DATA_DIR, "tavan_tarayici_bugun.csv")
            pd.DataFrame(_gunluk_kayitlar).to_csv(yol, index=False, encoding="utf-8-sig")
            send_telegram_document(yol, caption=f"Bugünkü {len(_gunluk_kayitlar)} bildirim")


if __name__ == "__main__":
    from flask import Flask
    _PORT = int(os.environ.get("PORT", "10000"))
    _app = Flask(__name__)

    @_app.route("/health")
    def _h():
        return "OK (tavan tarayici)", 200

    def _dongu():
        send_startup_message()
        while True:
            try:
                maybe_run_scan()
            except Exception as e:
                print(f"[TARAYICI] Döngü hatası: {e}", flush=True)
            try:
                poll_arge_commands()
            except Exception as e:
                print(f"[TARAYICI] Komut hatası: {e}", flush=True)
            time.sleep(5)

    def _ping():
        time.sleep(30)
        while True:
            try:
                requests.get(f"http://127.0.0.1:{_PORT}/health", timeout=10)
            except Exception:
                pass
            time.sleep(600)

    print(f"[BAŞLANGIÇ] arge_botu.py BAĞIMSIZ modda — {ARGE_KOD_SURUMU}", flush=True)
    threading.Thread(target=_dongu, daemon=True).start()
    threading.Thread(target=_ping, daemon=True).start()
    _app.run(host="0.0.0.0", port=_PORT)
