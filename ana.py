"""
ana.py — TEK BAŞLATICI: TÜM SİSTEMLER BİRLİKTE
================================================
2026-09-04 — Kullanıcının haklı uyarısı üzerine yazıldı:
  "ABD sistemi durmamalı ve uyuma sorunu olmasın."

SORUN: Render'ın ücretsiz katmanında TEK SERVİS = TEK START COMMAND var.
`python football_bot.py` yazınca ABD sinyal botu ve sosyal duygu
takipçisi DURUYOR. Ayrı ayrı çalıştırmak mümkün değil.

ÇÖZÜM: Bu dosya hepsini TEK SÜREÇTE, AYRI THREAD'lerde çalıştırıyor.
Daha önce Ar-Ge botunu us_sinyal_botu'na bağlarken kullandığımız
desenin aynısı - orada işe yaramıştı.

ÇALIŞAN SİSTEMLER:
  1. 📡 BIST TAVAN TARAYICISI (us_sinyal_botu.py → arge_botu.py)
       • 10:00-18:15 TR, tavana koşan hisseler
       • ABD sinyal botu (swing + gün-içi) KAPATILDI - testte
         göstergeler kör temel çizgiyi geçemedi (bkz. aşağıdaki not)
  2. ABD SOSYAL DUYGU (abd_sosyal_duygu.py)
       • StockTwits long/short etiketleri, seans dilimlerine göre
  3. FUTBOL BOTU (football_bot.py)
       • Poisson modeli + değer bahsi motoru

UYUMA SORUNU: Tek bir DIŞ ping thread'i var (her 10 dk). Daha önce
öğrendiğimiz kritik ders: loopback (127.0.0.1) ping Render'da İŞE
YARAMIYOR - Render uyutma kararını DIŞARIDAN gelen trafiğe göre
veriyor. Bu yüzden RENDER_EXTERNAL_URL / HARICI_URL kullanılıyor.

İZOLASYON: Her sistem kendi try/except'inde. Biri çökse bile diğerleri
çalışmaya devam eder - başlangıçta hangisinin yüklendiği raporlanır.

Start Command:  python ana.py
"""
import os
import sys
import time
import threading
import traceback

import requests
from flask import Flask

PORT = int(os.environ.get("PORT", "10000"))
ANA_SURUM = "ana-v2-abd-sinyal-kapatildi-2026-09-04"

app = Flask(__name__)
_durum = {"us": "yüklenmedi", "sosyal": "yüklenmedi", "futbol": "yüklenmedi"}


# =====================================================================
# MODÜLLERİ YÜKLE - her biri ayrı korumada
# =====================================================================
try:
    import us_sinyal_botu as US
    _durum["us"] = "✅ yüklendi"
except Exception as e:
    US = None
    _durum["us"] = f"❌ {e}"
    print(f"[ANA] us_sinyal_botu yüklenemedi: {e}", flush=True)
    traceback.print_exc()

try:
    import abd_sosyal_duygu as SOSYAL
    _durum["sosyal"] = "✅ yüklendi"
except Exception as e:
    SOSYAL = None
    _durum["sosyal"] = f"❌ {e}"
    print(f"[ANA] abd_sosyal_duygu yüklenemedi: {e}", flush=True)
    traceback.print_exc()

try:
    import football_bot as FUTBOL
    _durum["futbol"] = "✅ yüklendi"
except Exception as e:
    FUTBOL = None
    _durum["futbol"] = f"❌ {e}"
    print(f"[ANA] football_bot yüklenemedi: {e}", flush=True)
    traceback.print_exc()


def _guvenli(ad, fonk):
    """Bir thread'i sonsuza kadar güvenle çalıştırır. Fonksiyon
    beklenmedik şekilde biterse ya da hata verirse, tüm sistemi
    çökertmek yerine kendini yeniden başlatır."""
    def _sarmal():
        while True:
            try:
                fonk()
                print(f"[ANA] {ad} beklenmedik şekilde bitti, "
                      f"30sn sonra yeniden başlatılıyor.", flush=True)
            except Exception as e:
                print(f"[ANA] {ad} HATA: {e}", flush=True)
                traceback.print_exc()
            time.sleep(30)
    return _sarmal


def _tek_seferlik(ad, fonk):
    """Başlangıç mesajı gibi bir kez çalışacak işler."""
    def _sarmal():
        try:
            fonk()
        except Exception as e:
            print(f"[ANA] {ad} hatası: {e}", flush=True)
    return _sarmal


# =====================================================================
# TEK DIŞ PING - hepsi için ortak
# =====================================================================
def dis_ping():
    """2026-08-19'da öğrendiğimiz KRİTİK ders: loopback (127.0.0.1)
    ping Render'da uyumayı ENGELLEMİYOR. Render, uyutma kararını
    DIŞARIDAN gelen trafiğe bakarak veriyor; container'ın kendi
    içindeki istek yük dengeleyiciye hiç ulaşmıyor.
    Belirti şuydu: deploy'dan ~15 dk sonra servis tamamen susuyor,
    manuel deploy ile canlanıyordu. Dış adrese ping ile çözüldü."""
    harici = (os.environ.get("RENDER_EXTERNAL_URL", "").rstrip("/")
              or os.environ.get("HARICI_URL", "").rstrip("/"))
    if not harici:
        print("[ANA] ⚠️ RENDER_EXTERNAL_URL / HARICI_URL TANIMLI DEĞİL! "
              "Servis 15 dk sonra uyuyabilir. Render'a HARICI_URL ekle: "
              "https://b-st-us-bot.onrender.com", flush=True)
    time.sleep(30)
    while True:
        try:
            if harici:
                r = requests.get(f"{harici}/health", timeout=20)
                print(f"[ANA] Dış ping OK ({harici}) durum={r.status_code}", flush=True)
        except Exception as e:
            print(f"[ANA] Dış ping hatası: {e}", flush=True)
        time.sleep(600)


@app.route("/health")
def health():
    return "OK (ana - hepsi birlikte)", 200


@app.route("/")
def ana_sayfa():
    h = [f"<h2>Ana Başlatıcı — {ANA_SURUM}</h2>",
         "<h3>Sistem durumu</h3><ul>",
         f"<li>ABD Sinyal Botu: ❌ KAPALI (test: göstergeler kör "
         f"çizgiyi geçemedi)</li>",
         f"<li>BIST Tavan Tarayıcı: {_durum['us']}</li>",
         f"<li>ABD Sosyal Duygu: {_durum['sosyal']}</li>",
         f"<li>Futbol Botu: {_durum['futbol']}</li>",
         "</ul>"]
    if SOSYAL is not None:
        try:
            h.append(f"<p>Sosyal: {SOSYAL._durum['tur']} tur, "
                     f"{SOSYAL._durum['toplam_mesaj']} mesaj</p>")
        except Exception:
            pass
    if FUTBOL is not None:
        try:
            s = FUTBOL.compute_stats()
            h.append(f"<p>Futbol: {s['total']} sinyal, "
                     f"{s['pending']} bekleyen</p>")
        except Exception:
            pass
    return "\n".join(h)


if __name__ == "__main__":
    print(f"[ANA] {ANA_SURUM} başlıyor...", flush=True)
    print(f"[ANA] Modül durumu: {_durum}", flush=True)

    # --- 1) ABD SİNYAL BOTU — KAPATILDI (2026-09-04) ---
    # Test sonucu (abd_hedef_testi.py, 401 hisse × 2 yıl, 168.871 sinyal):
    # 8 göstergenin HİÇBİRİ kör temel çizgiyi geçemedi. Kör "koşulsuz al"
    # %10 hedef/20 gün ile net +%0.963 verirken, en iyi gösterge
    # (Bollinger) -%0.087 ile GERİDE kaldı; Donchian -%1.43'e kadar
    # düştü. Ayrıca mevcut kademeli hedef (%1/2/3/5) net -%0.112 ile
    # test edilen 13 ayarın EN KÖTÜSÜYDÜ - kullanıcının "10 gün bekleyip
    # %1 almak anlamsız" şikayeti haklıydı.
    # Günde 100+ bildirim gönderip değer katmıyordu → kapatıldı.
    #
    # ⚠️ AMA us_sinyal_botu.py TAMAMEN kapatılmıyor: BIST TAVAN
    # TARAYICISI onun içinden çağrılıyor ve ORADA GERÇEK BİR BULGU VAR
    # (tavan kapanışı → ertesi gün +%2.51 net, %90 isabet). Sadece ABD
    # tarama ve komut thread'leri başlatılmıyor.
    if US is not None:
        print("[ANA] ABD sinyal botu KAPALI (test: göstergeler kör çizgiyi "
              "geçemedi) - sadece BIST tavan tarayıcısı çalışacak.", flush=True)
        try:
            threading.Thread(target=_tek_seferlik("BIST tarayıcı başlangıç",
                                                   US.arge_botu_baslangic), daemon=True).start()
            threading.Thread(target=_guvenli("BIST tarayıcı komut",
                                              US.arge_botu_komut_dongusu), daemon=True).start()
            threading.Thread(target=_guvenli("BIST tavan tarayıcı",
                                              US.arge_botu_tarama_dongusu), daemon=True).start()
            print("[ANA] BIST tavan tarayıcı thread'leri başlatıldı.", flush=True)
        except AttributeError as e:
            print(f"[ANA] BIST tarayıcı thread'leri başlatılamadı: {e}", flush=True)

    # --- 2) ABD SOSYAL DUYGU ---
    if SOSYAL is not None:
        threading.Thread(target=_guvenli("Sosyal kontrol",
                                          SOSYAL._kontrol_dongusu), daemon=True).start()
        threading.Thread(target=_guvenli("Sosyal rapor",
                                          SOSYAL._rapor_dongusu), daemon=True).start()
        print("[ANA] Sosyal duygu thread'leri başlatıldı.", flush=True)

    # --- 3) FUTBOL BOTU ---
    if FUTBOL is not None:
        threading.Thread(target=_guvenli("Futbol ana", FUTBOL._fb_ana_dongu), daemon=True).start()
        threading.Thread(target=_guvenli("Futbol komut",
                                          FUTBOL._fb_komut_dongusu), daemon=True).start()
        try:
            FUTBOL.send_football_message(
                f"⚽ Futbol botu AKTİF — {FUTBOL.FB_SURUM}\n"
                f"Ana başlatıcı üzerinden çalışıyor (ABD sistemleriyle birlikte).\n"
                f"Ligler: {', '.join(FUTBOL.TRACKED_COMPETITIONS)} + Süper Lig\n"
                f"Komutlar: /stats /rapor /status")
        except Exception as e:
            print(f"[ANA] Futbol başlangıç mesajı gönderilemedi: {e}", flush=True)
        print("[ANA] Futbol botu thread'leri başlatıldı.", flush=True)

    # --- TEK DIŞ PING (hepsi için) ---
    threading.Thread(target=dis_ping, daemon=True).start()
    print("[ANA] Dış ping thread'i başlatıldı.", flush=True)

    print("[ANA] Flask sunucusu başlatılıyor - tüm sistemler çalışıyor.", flush=True)
    app.run(host="0.0.0.0", port=PORT)
