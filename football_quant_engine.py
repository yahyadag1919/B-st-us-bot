"""
football_quant_engine.py
Poisson dagilimi ile mac sonucu olasiliklarini hesaplar.

ONEMLI - DURUST NOT: football-data.org ucretsiz planinda gercek xG
verisi yok. Bu yuzden "xG" yerine golden turetilmis bir hucum/savunma
gucu kullaniyoruz (klasik Poisson futbol modeli - Dixon-Coles'in
basitlestirilmis hali). Bu, gercek xG'den daha az hassas: bir takim
sansli/sanssiz sonuclarla golu gercek performansindan farkli
gosterebilir (ornegin cok sut cekip az gol atmis olabilir). Ileride
ucretsiz bir xG kaynagi bulunursa buraya eklenebilir.

Ayrica KUCUK ORNEKLEM UYARISI: bir takimin son 5-10 maci istatistiksel
olarak cok kucuk bir ornek. Fonksiyonlar bunu "confidence" alaniyla
isaretliyor - deger_engine.py bu isarete gore sinyali zayiflatabilir
veya atlayabilir.
"""

import math

MIN_MATCHES_FOR_CONFIDENCE = 6  # bunun altinda "low_sample" isaretlenir
MAX_GOALS = 8                    # olasilik matrisi 0..MAX_GOALS gol araligi

# Varsayilan lig ortalamalari (gercek veri yoksa kullanilir).
# NOT: bunlar kaba varsayimlardir - ileride her lig icin fikstur
# gecmisinden gercek ortalama hesaplanmasi cok daha dogru olur.
DEFAULT_LEAGUE_AVG_HOME_GOALS = 1.45
DEFAULT_LEAGUE_AVG_AWAY_GOALS = 1.15


def compute_team_scoring_stats(matches, team_id):
    """
    Bir takimin son maclarindan (football_data_fetcher.get_team_recent_matches
    ciktisi) ortalama attigi/yedigi golu hesaplar. Ev/deplasman ayrimi
    yapmiyor - veri az oldugu icin hepsini birlikte kullaniyoruz.

    Doner: {
        "goals_scored_avg": float,
        "goals_conceded_avg": float,
        "matches_count": int,
        "low_sample": bool,   # MIN_MATCHES_FOR_CONFIDENCE altindaysa True
    }
    None doner eger hic tamamlanmis mac yoksa (hesaplanamaz).
    """
    scored, conceded, count = 0, 0, 0

    for m in matches:
        home_score = m.get("home_score")
        away_score = m.get("away_score")
        if home_score is None or away_score is None:
            continue  # henuz oynanmamis / skor eksik

        if m.get("home_team_id") == team_id:
            scored += home_score
            conceded += away_score
        elif m.get("away_team_id") == team_id:
            scored += away_score
            conceded += home_score
        else:
            continue  # bu takima ait degilse atla

        count += 1

    if count == 0:
        return None

    return {
        "goals_scored_avg": scored / count,
        "goals_conceded_avg": conceded / count,
        "matches_count": count,
        "low_sample": count < MIN_MATCHES_FOR_CONFIDENCE,
    }


def compute_expected_goals(
    home_stats,
    away_stats,
    league_avg_home_goals=DEFAULT_LEAGUE_AVG_HOME_GOALS,
    league_avg_away_goals=DEFAULT_LEAGUE_AVG_AWAY_GOALS,
):
    """
    Klasik Poisson futbol modeli: hucum/savunma gucu oranlarindan
    beklenen gol sayisini (lambda) cikarir.

    home_stats / away_stats: compute_team_scoring_stats() ciktisi.

    Doner: (lambda_home, lambda_away, confidence)
    confidence: "normal" veya "low_sample" (herhangi bir takim
    esik altindaysa low_sample - deger_engine bunu goz onune alsin).
    """
    home_attack = home_stats["goals_scored_avg"] / league_avg_home_goals
    home_defense = home_stats["goals_conceded_avg"] / league_avg_away_goals
    away_attack = away_stats["goals_scored_avg"] / league_avg_away_goals
    away_defense = away_stats["goals_conceded_avg"] / league_avg_home_goals

    lambda_home = home_attack * away_defense * league_avg_home_goals
    lambda_away = away_attack * home_defense * league_avg_away_goals

    confidence = "normal"
    if home_stats.get("low_sample") or away_stats.get("low_sample"):
        confidence = "low_sample"

    return lambda_home, lambda_away, confidence


def _poisson_pmf(k, lam):
    """P(X = k) tek bir Poisson olasiligi. Ekstra bagimlilik gerektirmesin
    diye scipy yerine math ile hesapliyoruz."""
    return (lam ** k) * math.exp(-lam) / math.factorial(k)


def build_score_matrix(lambda_home, lambda_away, max_goals=MAX_GOALS):
    """
    (max_goals+1) x (max_goals+1) boyutunda bir olasilik matrisi olusturur.
    matrix[i][j] = P(ev sahibi i gol atar VE deplasman j gol atar)
    Ev ve deplasman gol sayilarinin bagimsiz oldugu varsayilir (standart
    Poisson futbol modeli basitlestirmesi).
    """
    home_probs = [_poisson_pmf(i, lambda_home) for i in range(max_goals + 1)]
    away_probs = [_poisson_pmf(j, lambda_away) for j in range(max_goals + 1)]

    matrix = []
    for i in range(max_goals + 1):
        row = [home_probs[i] * away_probs[j] for j in range(max_goals + 1)]
        matrix.append(row)
    return matrix


def match_outcome_probabilities(matrix):
    """
    Olasilik matrisinden 1X2 (ev / beraberlik / deplasman) olasiliklarini cikarir.
    Doner: {"home_win": p, "draw": p, "away_win": p}
    """
    home_win = draw = away_win = 0.0
    n = len(matrix)
    for i in range(n):
        for j in range(n):
            p = matrix[i][j]
            if i > j:
                home_win += p
            elif i == j:
                draw += p
            else:
                away_win += p
    return {"home_win": home_win, "draw": draw, "away_win": away_win}


def over_under_probability(matrix, line=2.5):
    """
    Toplam gol Alt/Ust olasiligini hesaplar (varsayilan cizgi: 2.5).
    Doner: {"over": p, "under": p}
    """
    over = under = 0.0
    n = len(matrix)
    for i in range(n):
        for j in range(n):
            total_goals = i + j
            if total_goals > line:
                over += matrix[i][j]
            else:
                under += matrix[i][j]
    return {"over": over, "under": under}


def analyze_match(home_matches, away_matches, home_team_id, away_team_id,
                   league_avg_home_goals=DEFAULT_LEAGUE_AVG_HOME_GOALS,
                   league_avg_away_goals=DEFAULT_LEAGUE_AVG_AWAY_GOALS,
                   ou_line=2.5):
    """
    Ust seviye fonksiyon: iki takimin son mac gecmisinden 1X2 ve
    Alt/Ust olasiliklarini tek cagriyla doner.

    home_matches / away_matches: football_data_fetcher.get_team_recent_matches()
    ciktilari (ilgili takimlarin kendi son maclari).

    Doner: dict veya None (yeterli veri yoksa None - deger_engine bu
    maci atlamali, tahmin uydurmamaliyiz).
    """
    home_stats = compute_team_scoring_stats(home_matches, home_team_id)
    away_stats = compute_team_scoring_stats(away_matches, away_team_id)

    if home_stats is None or away_stats is None:
        return None  # yeterli gecmis veri yok - dogru olan tahmin uretmemek

    lambda_home, lambda_away, confidence = compute_expected_goals(
        home_stats, away_stats, league_avg_home_goals, league_avg_away_goals
    )
    matrix = build_score_matrix(lambda_home, lambda_away)

    return {
        "lambda_home": lambda_home,
        "lambda_away": lambda_away,
        "confidence": confidence,
        "outcome_probs": match_outcome_probabilities(matrix),
        "over_under": over_under_probability(matrix, line=ou_line),
        "home_matches_used": home_stats["matches_count"],
        "away_matches_used": away_stats["matches_count"],
    }
