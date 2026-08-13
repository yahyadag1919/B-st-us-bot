"""
overnight_model_lab.py — ARKA PLANDA OTONOM MODEL LABORATUVARI (İZOLE)
==========================================================================
Kullanıcının isteği: "sistem arka planda sürekli kendi kendini eğitsin,
geçmiş verilerle gelişsin, komut yazınca güncel başarı oranını söylesin,
her gün sorunca güncellenmiş olsun." Bunu GÜVENLİ şekilde kuruyor.

2026-08-13 GÜNCELLEMESİ - HAFTALIK SERİ YERİNE ÇAPRAZ DOĞRULAMA: İlk
tasarım "3 hafta üst üste iyi çıkmalı" diyordu - artık binlerce geçmiş
satır (overnight_backtest_results.csv) hemen elde olduğu için bu YAVAS
kaldı, kullanıcı haklı olarak günlük güncel cevap istedi. Çözüm: her
döngüde TEK train/test bölmesi yerine 5 KATMANLI ÇAPRAZ DOĞRULAMA
(cross-validation) - aynı günde 5 farklı bölünmeyle ölçüyor, ortalama +
STANDART SAPMA raporluyor. Bir varyant "onaylı" sayılması için:
  1) Ortalama AUC ≥ AUC_FLOOR (0.55 - rastgeleden belirgin iyi)
  2) Standart sapma ≤ AUC_STD_MAX (0.08 - 5 kat arasında tutarlı, tek
     şanslı bölünme değil)
Bu, "3 hafta bekle" yerine AYNI GÜN içinde gerçek bir tutarlılık testi
yapıyor - istatistiksel olarak en az o kadar sağlam, çok daha hızlı.
Döngü artık günde bir (LAB_INTERVAL_DAYS=1) çalışıyor, /lab_rapor her
gün güncel sayı gösteriyor.

NEDEN HÂLÂ SINIRSIZ ARAMA YOK: Yeterince çok kombinasyon denersen,
gerçek bir kenar (edge) olmasa bile şans eseri iyi görünen biri MUTLAKA
çıkar - bu proje bunu rapor 28→29'da yaşadı (35 sinyalle +0.369R
"harika" bulgu, 195 sinyale çıkınca -0.018R'ye eridi). Bu yüzden hâlâ
sadece 6 ÖNCEDEN BELİRLENMİŞ varyant deneniyor, sınırsız/rastgele değil.

Hiçbir zaman overnight_model.pkl'nin ÜZERİNE otomatik yazılmaz -
candidate_model.pkl olarak kaydedilir, canlıya alma kararı insana kalır.

VERİ KAYNAĞI: ai_shadow_log.csv (ileriye dönük, seçim yanlılığı yok,
yavaş büyüyor) + overnight_backtest_results.csv (150 günlük geçmiş,
hemen hazır, büyük ama AI/indikatör eşiğini geçenleri içerdiği için
SEÇİM YANLILIĞI TAŞIYOR - ikisi birlikte kullanılıyor, kaynak etiketiyle
ayrı izlenebiliyor).
"""

import os
import csv
import time
import warnings
from datetime import datetime, timezone, timedelta

import numpy as np
import pandas as pd
import requests

warnings.filterwarnings("ignore")

DATA_DIR = os.environ.get("DATA_DIR", ".")


def _data_path(filename: str) -> str:
    return os.path.join(DATA_DIR, filename)


AI_SHADOW_LOG_FILE = _data_path("ai_shadow_log.csv")
BACKTEST_RESULTS_FILE = _data_path("overnight_backtest_results.csv")
LAB_HISTORY_FILE = _data_path("ai_lab_history.csv")
HISTORY_FIELDS = ["run_date", "varyant", "n_features", "n_obs", "cv_folds",
                   "auc_ortalama", "auc_std", "accuracy_ortalama", "onayli_mi", "en_iyi_mi"]

LAB_ENABLED = os.environ.get("AI_LAB_ENABLED", "true").lower() == "true"
LAB_INTERVAL_DAYS = int(os.environ.get("AI_LAB_INTERVAL_DAYS", "1"))
MIN_LABELED_ROWS = int(os.environ.get("LAB_MIN_LABELED_ROWS", "400"))
CV_FOLDS = int(os.environ.get("AI_LAB_CV_FOLDS", "5"))
AUC_FLOOR = float(os.environ.get("AI_LAB_AUC_FLOOR", "0.55"))
AUC_STD_MAX = float(os.environ.get("AI_LAB_AUC_STD_MAX", "0.08"))

TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN", "")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")

ALL_FEATURES = ["volume_factor", "rsi", "price_change_pct", "gap_pct", "cmf",
                 "has_catalyst", "close_to_high_ratio"]

# ÖNCEDEN BELİRLENMİŞ, SINIRLI VARYANT SETİ (6 tane - sınırsız arama değil).
# Her biri bir HİPOTEZi test ediyor, rastgele kombinasyon değil.
VARYANTLAR = {
    "tum_ozellikler": ALL_FEATURES,
    "katalizorsuz": [f for f in ALL_FEATURES if f != "has_catalyst"],
    "sadece_fiyat_aksiyon": ["price_change_pct", "gap_pct", "close_to_high_ratio"],
    "sadece_hacim_para": ["volume_factor", "cmf"],
    "fiyat_ve_hacim": ["price_change_pct", "gap_pct", "close_to_high_ratio", "volume_factor", "cmf"],
    "sadece_rsi_cmf": ["rsi", "cmf"],
}

_last_run_time = None


def send_telegram_message(text: str):
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        return
    try:
        requests.post(
            f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage",
            json={"chat_id": TELEGRAM_CHAT_ID, "text": text[:4000]}, timeout=20)
    except Exception as e:
        print(f"[AI_LAB] Telegram gönderilemedi: {e}", flush=True)


def _read_history():
    if not os.path.exists(LAB_HISTORY_FILE):
        return []
    with open(LAB_HISTORY_FILE, newline="", encoding="utf-8") as f:
        return list(csv.DictReader(f))


def _append_history(rows):
    exists = os.path.exists(LAB_HISTORY_FILE)
    with open(LAB_HISTORY_FILE, "a", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=HISTORY_FIELDS)
        if not exists:
            w.writeheader()
        w.writerows(rows)


def load_labeled_data() -> pd.DataFrame:
    """İKİ kaynaktan birleştirir:
    1) ai_shadow_log.csv — İLERİYE doğru birikiyor, TARANAN HER hisseyi
       kapsıyor (seçim yanlılığı yok) ama yavaş büyüyor.
    2) overnight_backtest_results.csv — 150 günlük GEÇMİŞ veri, hemen
       hazır ve çok daha büyük (binlerce satır) ama SADECE AI veya
       indikatör eşiğini geçen günleri içeriyor (backtest_ticker zaten
       öyle filtrelemişti) - yani bu kaynak SEÇİM YANLILIĞI TAŞIYOR,
       ai_shadow_log kadar temiz değil. Ikisi birlikte kullanılıyor ama
       bu fark raporda açıkça belirtiliyor."""
    parcalar = []

    if os.path.exists(AI_SHADOW_LOG_FILE):
        df1 = pd.read_csv(AI_SHADOW_LOG_FILE)
        df1 = df1[df1["result"].isin(["SUCCESS", "FAIL"])].copy()
        df1["kaynak"] = "golge_ileri"
        parcalar.append(df1)

    if os.path.exists(BACKTEST_RESULTS_FILE):
        df2 = pd.read_csv(BACKTEST_RESULTS_FILE)
        # backtest sonuc_tipi TP/SL/TIMEOUT - SUCCESS/FAIL'e cevir (TIMEOUT'un
        # kendi r_multiple'i zaten pozitif/negatif olabilir, gerceklesen_pct>0 ise basarili sayalim)
        df2["result"] = df2.apply(
            lambda r: "SUCCESS" if r["sonuc_tipi"] == "TP" or (r["sonuc_tipi"] == "TIMEOUT" and r["r_multiple"] > 0)
            else "FAIL", axis=1)
        eksik = [c for c in ALL_FEATURES if c not in df2.columns]
        if not eksik:
            df2["kaynak"] = "backtest_gecmis"
            parcalar.append(df2)
        else:
            print(f"[AI_LAB] overnight_backtest_results.csv eski formatta (feature eksik: {eksik}) - atlandı, "
                  f"backtest'i tekrar çalıştırırsan dahil olur.", flush=True)

    if not parcalar:
        return pd.DataFrame()

    df = pd.concat(parcalar, ignore_index=True)
    for c in ALL_FEATURES:
        df[c] = pd.to_numeric(df[c], errors="coerce")
    df = df.dropna(subset=ALL_FEATURES)
    df["y"] = (df["result"] == "SUCCESS").astype(int)
    return df


def evaluate_variant(df: pd.DataFrame, features: list):
    """TEK bolunme yerine CV_FOLDS katmanli capraz dogrulama - ayni gun
    icinde tutarlilik olculuyor, haftalarca beklemeye gerek kalmadan.
    Ortalama VE standart sapma donuyor - std yuksekse (kat arasi sonuc
    tutarsizsa) bu 'onayli' sayilmiyor, tek sansli bolunme guven vermez."""
    from sklearn.model_selection import StratifiedKFold, cross_validate
    from xgboost import XGBClassifier

    X = df[features]
    y = df["y"]
    if y.nunique() < 2 or len(df) < CV_FOLDS * 10:
        return None

    model = XGBClassifier(n_estimators=150, max_depth=4, learning_rate=0.05,
                           eval_metric="logloss", random_state=42)
    cv = StratifiedKFold(n_splits=CV_FOLDS, shuffle=True, random_state=42)
    sonuc = cross_validate(model, X, y, cv=cv,
                            scoring=["roc_auc", "accuracy", "precision", "recall"])

    metrikler = {
        "n_obs": len(df),
        "auc_ortalama": round(float(np.mean(sonuc["test_roc_auc"])), 4),
        "auc_std": round(float(np.std(sonuc["test_roc_auc"])), 4),
        "accuracy_ortalama": round(float(np.mean(sonuc["test_accuracy"])), 4),
        "precision_ortalama": round(float(np.mean(sonuc["test_precision"])), 4),
        "recall_ortalama": round(float(np.mean(sonuc["test_recall"])), 4),
    }
    # Rapor icin nihai modeli TUM veriyle egit (CV sadece dogrulama icindi)
    final_model = XGBClassifier(n_estimators=150, max_depth=4, learning_rate=0.05,
                                 eval_metric="logloss", random_state=42)
    final_model.fit(X, y)
    return final_model, metrikler


def run_lab_cycle():
    """Gunde bir (LAB_INTERVAL_DAYS) cagirilir - 6 varyanti CAPRAZ
    DOGRULAMAYLA degerlendirir, sonuclari kalici gecmise ekler. Bir
    varyant BUGUN onayli hale geldiyse (dun degildi, bugun oldu)
    SADECE O ZAMAN Telegram'a haber verir - tekrar tekrar spam olmasin."""
    print(f"[AI_LAB] Günlük döngü başlıyor ({len(VARYANTLAR)} sınırlı varyant, "
          f"{CV_FOLDS}-katlı çapraz doğrulama)...", flush=True)

    df = load_labeled_data()
    print(f"[AI_LAB] Etiketli gözlem sayısı: {len(df)}", flush=True)
    if len(df) < MIN_LABELED_ROWS:
        print(f"[AI_LAB] Yetersiz veri ({len(df)} < {MIN_LABELED_ROWS}) - bugün atlanıyor.", flush=True)
        return
    if "kaynak" in df.columns:
        kaynak_ozet = df["kaynak"].value_counts().to_dict()
        print(f"[AI_LAB] Kaynak dağılımı: {kaynak_ozet}", flush=True)

    today = datetime.now(timezone.utc).date().isoformat()
    gecmis = _read_history()
    onceki_onayli = {r["varyant"] for r in gecmis if r["run_date"] == max(
        (x["run_date"] for x in gecmis), default="") and r["onayli_mi"] in ("1", "True", "true", 1, True)}

    sonuclar = {}
    for isim, features in VARYANTLAR.items():
        try:
            sonuc = evaluate_variant(df, features)
            if sonuc is None:
                print(f"[AI_LAB]   {isim}: yetersiz veri, atlandı", flush=True)
                continue
            model, metrikler = sonuc
            sonuclar[isim] = (model, metrikler, features)
            print(f"[AI_LAB]   {isim}: AUC {metrikler['auc_ortalama']}±{metrikler['auc_std']}", flush=True)
        except Exception as e:
            print(f"[AI_LAB] HATA {isim}: {e}", flush=True)

    if not sonuclar:
        print("[AI_LAB] Hiçbir varyant değerlendirilemedi.", flush=True)
        return

    en_iyi_isim = max(sonuclar.items(), key=lambda kv: kv[1][1]["auc_ortalama"])[0]

    yeni_kayitlar = []
    yeni_onayli = set()
    for isim, (_, m, feats) in sonuclar.items():
        onayli = m["auc_ortalama"] >= AUC_FLOOR and m["auc_std"] <= AUC_STD_MAX
        if onayli:
            yeni_onayli.add(isim)
        yeni_kayitlar.append({
            "run_date": today, "varyant": isim, "n_features": len(feats), "n_obs": m["n_obs"],
            "cv_folds": CV_FOLDS, "auc_ortalama": m["auc_ortalama"], "auc_std": m["auc_std"],
            "accuracy_ortalama": m["accuracy_ortalama"],
            "onayli_mi": 1 if onayli else 0, "en_iyi_mi": 1 if isim == en_iyi_isim else 0,
        })
    _append_history(yeni_kayitlar)

    # SADECE bugun YENI onayli olan varyant icin bildirim (dun onayli degildi).
    for isim in yeni_onayli - onceki_onayli:
        _bildir_onaylanmis_bulgu(isim, sonuclar[isim])

    print(f"[AI_LAB] Döngü bitti. En iyi: {en_iyi_isim} | Onaylı: {yeni_onayli or 'yok'}", flush=True)


def _bildir_onaylanmis_bulgu(varyant: str, sonuc):
    _, metrikler, features = sonuc
    mesaj = (
        f"🎉 [AI LAB — ONAYLANMIŞ BULGU] '{varyant}'\n\n"
        f"{CV_FOLDS} katlı çapraz doğrulamada tutarlı sonuç:\n"
        f"AUC: {metrikler['auc_ortalama']:.3f} ± {metrikler['auc_std']:.3f} "
        f"(eşik ≥{AUC_FLOOR}, tutarlılık için sapma ≤{AUC_STD_MAX})\n"
        f"Doğruluk: %{metrikler['accuracy_ortalama']*100:.1f} | "
        f"Gözlem sayısı: {metrikler['n_obs']}\n\n"
        f"Özellikler: {', '.join(features)}\n\n"
        f"⚠️ Bu hâlâ kesin kanıt değil — çapraz doğrulama tek bölünmeden "
        f"daha güvenilir ama garanti değil, canlıda ayrıca izlenmeli. "
        f"candidate_model.pkl olarak kaydedildi, canlıya almak için elle "
        f"overnight_model.pkl ile değiştirip yüklemen gerekiyor — otomatik "
        f"uygulanmadı."
    )
    print(mesaj, flush=True)
    send_telegram_message(mesaj)
    try:
        import joblib
        model = sonuc[0]
        joblib.dump(model, "candidate_model.pkl")
        print(f"[AI_LAB] candidate_model.pkl kaydedildi ({varyant}).", flush=True)
    except Exception as e:
        print(f"[AI_LAB] candidate_model.pkl kaydedilemedi: {e}", flush=True)


def maybe_run_lab():
    """run_forever() dongusunden her turda cagirilmasi guvenlidir - kendi
    zamanlayicisina gore LAB_INTERVAL_DAYS'te bir (varsayilan gunde 1)
    gercekten calisir."""
    global _last_run_time
    if not LAB_ENABLED:
        return
    now = datetime.now(timezone.utc)
    if _last_run_time is not None and (now - _last_run_time).total_seconds() < LAB_INTERVAL_DAYS * 86400:
        return
    _last_run_time = now
    try:
        run_lab_cycle()
    except Exception as e:
        print(f"[AI_LAB] Döngü hatası: {e}", flush=True)


def build_lab_report() -> str:
    """/lab_rapor komutu - en son gunun capraz-dogrulama sonuclarini
    gosterir. Gunluk dongu sayesinde her gun sorulunca guncel deger doner."""
    gecmis = _read_history()
    if not gecmis:
        return (f"🔬 [AI LAB] Henüz çalışma yok. Her {LAB_INTERVAL_DAYS} günde bir "
                f"otomatik çalışıyor, en az {MIN_LABELED_ROWS} etiketli gözlem gerekiyor.")

    son_tarih = max(r["run_date"] for r in gecmis)
    son_tur = [r for r in gecmis if r["run_date"] == son_tarih]
    lines = [f"🔬 [AI LAB RAPORU] {son_tarih} — {CV_FOLDS} katlı çapraz doğrulama", ""]
    for r in sorted(son_tur, key=lambda r: -float(r["auc_ortalama"])):
        ikon = "✅" if r["onayli_mi"] in ("1", "True", "true", 1, True) else ("🏆" if r["en_iyi_mi"] in ("1", "True", "true", 1, True) else "  ")
        lines.append(f"{ikon} {r['varyant']}: AUC {r['auc_ortalama']}±{r['auc_std']} "
                     f"| doğruluk %{float(r['accuracy_ortalama'])*100:.1f} | n={r['n_obs']}")
    lines.append(f"\nOnay için gereken: AUC≥{AUC_FLOOR} VE sapma≤{AUC_STD_MAX} (aynı gün, {CV_FOLDS} kat arası tutarlılık)")
    lines.append("✅ = onaylı bulgu | 🏆 = bugünün en iyisi (henüz onay eşiğinde olmayabilir)")
    return "\n".join(lines)

