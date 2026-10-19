#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
AURA90 — Extras du générateur de coupons
========================================

1. SCORE EXACT (catégorie « score », visible dans l'onglet Coupons)
   Les matchs du jour où le moteur est le plus net sur le score : pour chacun,
   1 ou 2 scores exacts (2 si le deuxième est proche du premier). Chaque match
   est enregistré comme un coupon de catégorie « score » dont les sélections
   sont les scores proposés ; il est GAGNÉ dès qu'un des scores tombe.

2. COMBINÉS TIKTOK (catégorie « tiktok », réservée à l'admin — page tiktok.html)
   3 combinés par jour, 10 à 15 matchs, cote totale visée ≥ 90, construits sur
   7 jours glissants :
     · J+3 à J+6 : PROVISOIRES, reconstruits chaque jour (cotes réelles quand le
       bookmaker les a publiées, sinon estimées par le moteur)
     · J+2      : dernière reconstruction puis FIGÉ (Babs monte ses vidéos 2 j avant)
     · J+1 et J : jamais touchés
   Trois profils : SOLIDE (base de pronos de qualité signée, complétée), MIXTE,
   VALEUR (sélections où le moteur voit plus que la cote ne paie).
"""

import math

import selection_v2 as SEL

# ----------------------------------------------------------------- SCORE EXACT
# v2 (19/10/2026) — les scores ne sortent plus « au hasard » :
#   1. on lit ce que le marché pense du match (cotes 1-N-2 et +/- 2,5 buts) et on
#      en déduit les buts attendus de chaque équipe ; idem pour le moteur ;
#   2. on mélange les deux (marché majoritaire : il connaît les équipes de coupe
#      d'Europe que le moteur voit peu) et on calcule toute la grille des scores
#      avec la correction Dixon-Coles (0-0, 1-0, 0-1, 1-1 mieux estimés) ;
#   3. si le bookmaker publie les cotes Score exact, elles corrigent encore la grille ;
#   4. cohérence : quand une équipe est nettement favorite, on ne propose que des
#      victoires de cette équipe (fini le 1-0 pour Sabah face à Dortmund) ;
#   5. un match sans cotes 1-N-2 n'est jamais proposé en score exact.
SCORE_MAX_MATCHS = 5        # matchs par jour dans la section Score exact
SCORE_P_MIN = 9.0           # % minimum du meilleur score pour qu'un match soit retenu
SCORE_RATIO_UN_SEUL = 1.6   # si le 1er score domine le 2e de ce facteur → un seul score
SCORE_FAVORI_NET = 0.50     # au-delà, seules les victoires du favori sont proposées
SCORE_VALEUR_MIN = 0.80     # proba × cote minimum (on ne propose pas un score bradé)
POIDS_MARCHE = 0.65         # part du marché dans les buts attendus (le reste : moteur)
POIDS_COTES_SE = 0.40       # part des cotes Score exact du bookmaker dans la proba finale
RHO_DC = -0.08              # correction Dixon-Coles des petits scores
MAX_B = 8
MARGE_ESTIMEE = 0.85        # cote estimée = 0,85 / p quand le bookmaker n'a pas de cote


def _code_score(sc):
    return "SE:" + sc.replace(":", "-").strip()


def _pmf(mu):
    k = list(range(MAX_B + 1))
    return [math.exp(-mu) * mu ** x / math.factorial(x) for x in k]


def grille_scores(mu_h, mu_a, rho=RHO_DC):
    """Grille des probabilités de score (Poisson + correction Dixon-Coles)."""
    ph, pa = _pmf(mu_h), _pmf(mu_a)
    G = [[ph[i] * pa[j] for j in range(MAX_B + 1)] for i in range(MAX_B + 1)]
    G[0][0] *= max(1 - mu_h * mu_a * rho, 0.01)
    G[0][1] *= max(1 + mu_h * rho, 0.01)
    G[1][0] *= max(1 + mu_a * rho, 0.01)
    G[1][1] *= max(1 - rho, 0.01)
    t = sum(sum(r) for r in G)
    return [[v / t for v in r] for r in G]


def _issues(G):
    p1 = sum(G[i][j] for i in range(MAX_B + 1) for j in range(MAX_B + 1) if i > j)
    p2 = sum(G[i][j] for i in range(MAX_B + 1) for j in range(MAX_B + 1) if i < j)
    o25 = sum(G[i][j] for i in range(MAX_B + 1) for j in range(MAX_B + 1) if i + j >= 3)
    return p1, p2, o25


def buts_attendus(p1, p2, o25=None):
    """Retrouve (buts attendus dom, ext) qui reproduisent p1, p2 (et +2,5 si connu)."""
    def ecart(mh, ma):
        a1, a2, ao = _issues(grille_scores(mh, ma))
        e = (a1 - p1) ** 2 + (a2 - p2) ** 2
        if o25 is not None:
            e += 0.6 * (ao - o25) ** 2
        return e
    best = (9, 1.3, 1.1)
    for mh in [x / 10 for x in range(2, 41, 2)]:
        for ma in [x / 10 for x in range(2, 41, 2)]:
            e = ecart(mh, ma)
            if e < best[0]:
                best = (e, mh, ma)
    for pas in (0.05, 0.02):
        _, mh0, ma0 = best
        for dh in range(-4, 5):
            for da in range(-4, 5):
                mh, ma = max(mh0 + dh * pas, 0.1), max(ma0 + da * pas, 0.1)
                e = ecart(mh, ma)
                if e < best[0]:
                    best = (e, mh, ma)
    return best[1], best[2]


def _sans_marge(cotes, codes):
    inv = {c: 1 / cotes[c] for c in codes if cotes.get(c)}
    if len(inv) != len(codes):
        return None
    t = sum(inv.values())
    return {c: v / t for c, v in inv.items()}


def analyse_scores(m, cotes):
    """Grille de scores finale d'un match, ou None si le marché n'a pas de cotes 1-N-2."""
    x12 = _sans_marge(cotes, ["1", "N", "2"])
    if not x12:
        return None
    ou = _sans_marge(cotes, ["O2.5", "U2.5"])
    mh_m, ma_m = buts_attendus(x12["1"], x12["2"], ou["O2.5"] if ou else None)
    mh, ma = mh_m, ma_m
    if m.get("p1") is not None and m.get("p2") is not None:
        mh_e, ma_e = buts_attendus(float(m["p1"]), float(m["p2"]), m.get("pO25"))
        # moteur très loin du marché (souvent : équipe de coupe d'Europe peu connue
        # du moteur) → on suit presque entièrement le marché
        ecart = max(abs(float(m["p1"]) - x12["1"]), abs(float(m["p2"]) - x12["2"]))
        w = 0.9 if ecart > 0.15 else POIDS_MARCHE
        # moyenne géométrique pondérée : le marché pèse plus
        mh = math.exp(w * math.log(mh_m) + (1 - w) * math.log(mh_e))
        ma = math.exp(w * math.log(ma_m) + (1 - w) * math.log(ma_e))
    G = grille_scores(mh, ma)
    probas = {f"{i}-{j}": G[i][j] for i in range(6) for j in range(6)}
    se = {k[3:]: v for k, v in cotes.items() if k.startswith("SE:")}
    if len(se) >= 8:   # assez de cotes Score exact pour en tirer un avis
        inv = {k: 1 / v for k, v in se.items() if v > 1.01}
        t = sum(inv.values())
        for k in probas:
            if k in inv:
                probas[k] = (1 - POIDS_COTES_SE) * probas[k] + POIDS_COTES_SE * inv[k] / t
    p1, p2, _ = _issues(G)
    return {"probas": probas, "p1": p1, "p2": p2, "mu": (round(mh, 2), round(ma, 2))}


def construire_scores_exacts(matchs, cotes_brutes, journal=print):
    """
    matchs        : sortie de candidats() (p1, pN, p2, pO25 du moteur)
    cotes_brutes  : {fixture_id: {"1": 1.25, "N": 6.5, "2": 11, "O2.5": 1.45, "SE:2-0": 6.0, …}}
    Retourne des coupons prêts pour enregistrer().
    """
    retenus = []
    for m in matchs:
        cotes = cotes_brutes.get(m["fixture_id"]) or {}
        a = analyse_scores(m, cotes)
        if not a:
            continue
        favori = "1" if a["p1"] >= a["p2"] else "2"
        p_fav = max(a["p1"], a["p2"])
        candidats = []
        for sc, p in sorted(a["probas"].items(), key=lambda t: -t[1]):
            h, e = map(int, sc.split("-"))
            issue = "1" if h > e else "2" if e > h else "N"
            if p_fav >= SCORE_FAVORI_NET and issue != favori:
                continue                                   # favori net : seulement ses victoires
            if p_fav >= 0.42 and issue not in (favori, "N"):
                continue                                   # jamais une victoire de l'outsider
            cote = cotes.get(_code_score(sc))
            estimee = not cote
            if estimee:
                cote = round(MARGE_ESTIMEE / max(p, 0.02), 2)
            if p * cote < SCORE_VALEUR_MIN:
                continue
            candidats.append({"score": sc, "p": p, "cote": float(cote), "estimee": estimee})
            if len(candidats) == 2:
                break
        if not candidats or candidats[0]["p"] * 100 < SCORE_P_MIN:
            continue
        retenus.append((candidats[0]["p"], m, candidats, a))
    retenus.sort(key=lambda t: -t[0])

    coupons = []
    for k, (p1, m, cands, a) in enumerate(retenus[:SCORE_MAX_MATCHS], 1):
        liste = [cands[0]]
        if len(cands) > 1 and cands[0]["p"] < SCORE_RATIO_UN_SEUL * cands[1]["p"]:
            liste.append(cands[1])
        sels = [{
            **{c: m.get(c) for c in ("fixture_id", "ligue", "dom", "ext", "date_match",
                                      "heure", "logo_dom", "logo_ext")},
            "marche": "Score exact",
            "selection": "Score exact " + sc["score"],
            "code": _code_score(sc["score"]), "cote": sc["cote"],
            "confiance": round(sc["p"] * 100),
            "famille": "score", "p": sc["p"],
        } for sc in liste]
        coupons.append({"categorie": "score", "numero": k, "selections": sels,
                        "cote_totale": sels[0]["cote"]})
        journal(f"   🎯 score exact #{k} : {m['dom']} – {m['ext']} (buts attendus {a['mu'][0]}–{a['mu'][1]}) → "
                + " / ".join(f"{s['selection'].replace('Score exact ', '')} ({s['confiance']} %)" for s in sels))
    if not coupons:
        journal("   🎯 score exact : aucun match assez net aujourd'hui")
    return coupons


# ----------------------------------------------------------------- TIKTOK
TIKTOK_MIN_LEGS = 5         # (utilisé par le générateur pour savoir si la journée est jouable)
TIKTOK_JOURS = 6            # J+1 … J+6
TIKTOK_FIGE_A = 2           # figé à partir de J+2


# 09/10/2026 (Babs) : 8 matchs maximum par combiné, cote 30 et plus,
# priorité absolue aux soirées de Ligue des Champions et de Ligue Europa.
PROFILS_TIKTOK = {
    # même optimiseur que les coupons, avec en plus un bonus « grosse affiche »
    # et une pénalité de répétition (bonus_poids) pour des combinés qui donnent envie
    "solide": dict(cote_min=30.0, cote_max=80.0, legs_min=6, legs_max=8,
                   cote_sel_min=1.30, cote_sel_max=2.10, p_sel_min=0.50, p_coupon_min=1e-6, bonus_poids=0.30),
    "mixte":  dict(cote_min=40.0, cote_max=150.0, legs_min=6, legs_max=8,
                   cote_sel_min=1.40, cote_sel_max=2.80, p_sel_min=0.40, p_coupon_min=1e-6, bonus_poids=0.30),
    "valeur": dict(cote_min=60.0, cote_max=300.0, legs_min=5, legs_max=8,
                   cote_sel_min=1.60, cote_sel_max=3.40, p_sel_min=0.32, p_coupon_min=1e-6, valeur=True, bonus_poids=0.30),
}

# Coupes d'Europe en priorité : on construit d'abord avec la C1 + C2 seules,
# puis en ajoutant la C3, et seulement ensuite avec tous les matchs du jour
# (où les coupes gardent un gros bonus).
PALIERS_COUPES = [
    ("C1/C2", {"ligue des champions", "ligue europa"}),
    ("C1/C2/C3", {"ligue des champions", "ligue europa", "ligue conference"}),
]
BONUS_COUPES = {"ligue des champions": 0.8, "ligue europa": 0.7, "ligue conference": 0.35}

# ---------------------------------------------------------------- AFFICHES
# Notoriété d'un match pour TikTok : ce qui fait cliquer, c'est un Real–Atlético,
# pas un Îles Féroé–Kazakhstan (constat sur les vidéos de Babs : 27 000 vues avec
# des grands clubs, 1 200 avec des petites sélections).
NOTORIETE_LIGUE = {
    "ligue des champions": 1.0, "premier league": 1.0, "la liga": 0.95, "serie a": 0.9,
    "bundesliga": 0.9, "ligue 1": 0.95, "ligue europa": 0.95, "ligue conference": 0.6, "uefa nations league": 0.65,
    "ligue des nations": 0.65, "can — qualifications": 0.75, "cdm — qualifications afrique": 0.75,
    "coupe du monde — qualifications afrique": 0.75, "cdm — qualifications europe": 0.6,
    "coupe du monde — qualifications europe": 0.6, "eredivisie": 0.55, "liga portugal": 0.55,
    "süper lig": 0.45, "jupiler pro league": 0.35, "amicaux internationaux": 0.4,
}
GRANDS = [  # clubs et sélections qui parlent à tout le monde (public francophone / Sénégal)
    "real madrid", "barcelona", "atletico madrid", "manchester city", "manchester united", "liverpool",
    "arsenal", "chelsea", "tottenham", "paris saint germain", "psg", "marseille", "bayern",
    "dortmund", "juventus", "inter", "ac milan", "napoli",
    "france", "spain", "england", "germany", "portugal", "brazil", "argentina", "italy", "netherlands",
    "senegal", "morocco", "ivory coast", "cote d'ivoire", "cameroon", "nigeria", "egypt", "algeria",
]
CONNUS = [
    "lyon", "monaco", "lille", "lens", "nice", "rennes", "leverkusen", "leipzig", "roma", "lazio",
    "atalanta", "milan", "sevilla", "villarreal", "real sociedad", "betis", "valencia", "newcastle",
    "aston villa", "west ham", "brighton", "ajax", "psv", "feyenoord", "benfica", "porto", "sporting",
    "galatasaray", "fenerbahce", "besiktas", "celtic", "rangers",
    "belgium", "croatia", "switzerland", "denmark", "norway", "sweden", "turkey", "scotland", "wales",
    "mali", "ghana", "tunisia", "guinea", "burkina faso", "dr congo", "south africa",
]


def _norm(x):
    import unicodedata
    x = unicodedata.normalize("NFD", str(x or "")).encode("ascii", "ignore").decode().lower()
    return x


def _equipe(nom):
    n = _norm(nom)
    if any(g in n for g in GRANDS):
        return 1.0
    if any(c in n for c in CONNUS):
        return 0.6
    return 0.0


def notoriete(s):
    """0 (inconnu) à 1 (affiche) : les deux équipes et la compétition."""
    e1, e2 = _equipe(s.get("dom")), _equipe(s.get("ext"))
    lig = _NOTO_LIGUE.get(_norm(s.get("ligue")), 0.3)
    return round(0.45 * max(e1, e2) + 0.25 * min(e1, e2) + 0.30 * lig, 3)


# clés normalisées (sans accents) : « Süper Lig », « Ligue Conférence »… sont reconnues
_NOTO_LIGUE = {_norm(k): v for k, v in NOTORIETE_LIGUE.items()}


def _coupe(s):
    return _norm(s.get("ligue")).strip()


def _coherent(legs):
    """Un seul marché par match dans un combiné."""
    ids = [s["fixture_id"] for s in legs]
    return len(ids) == len(set(ids))


# ---------------------------------------------------------------- VARIÉTÉ
def groupe_marche(code):
    c = str(code)
    if c in ("1", "2"): return "victoire"
    if c in ("1X", "X2", "12"): return "double chance"
    if c in ("BTTS", "NOBTTS"): return "btts"
    if c[:1] in ("O", "U"): return "buts"
    return "autre"


def diversifier(legs, cands, regles, part_max=0.40, part_victoires=0.30):
    """Aucun type de pari ne dépasse ~40 % du combiné (au moins 2), et au moins
    ~30 % de victoires sèches (V1/V2) quand le jour en propose : on remplace la
    sélection la plus faible par la meilleure sélection du type manquant, en
    restant dans la fourchette de cote."""
    legs = list(legs)
    quota = max(2, math.ceil(part_max * len(legs)))
    poids = regles.get("bonus_poids", 0.0)
    beta = 1.0 if regles.get("valeur") else 0.0
    val = lambda s: math.log(s["p"]) + beta * math.log(s["cote"]) + poids * s.get("bonus", 0.0)

    # 1) plancher de victoires sèches
    min_v = math.ceil(part_victoires * len(legs))
    for _ in range(len(legs)):
        nv = sum(1 for s in legs if groupe_marche(s["code"]) == "victoire")
        if nv >= min_v:
            break
        remplacables = sorted((s for s in legs if groupe_marche(s["code"]) != "victoire"), key=val)
        fait = False
        for faible in remplacables:
            autres = [s for s in legs if s is not faible]
            pris = {s["fixture_id"] for s in autres}
            cote_sans = math.prod(s["cote"] for s in autres)
            options = [c for c in cands if groupe_marche(c["code"]) == "victoire" and c["fixture_id"] not in pris
                       and regles["cote_min"] <= cote_sans * c["cote"] <= regles["cote_max"]]
            if options:
                # d'abord changer de pari sur le MÊME match (on garde l'affiche), sinon un autre match
                meme = [c for c in options if c["fixture_id"] == faible["fixture_id"]]
                legs = autres + [max(meme or options, key=val)]
                fait = True
                break
        if not fait:
            break

    # 2) plafond par type de pari (sans redescendre sous le plancher de victoires)
    for _ in range(3 * len(legs)):
        compte = {}
        for s in legs:
            compte[groupe_marche(s["code"])] = compte.get(groupe_marche(s["code"]), 0) + 1
        trop = [g for g, n in compte.items() if n > quota]
        if not trop:
            break
        g = trop[0]
        faible = min((s for s in legs if groupe_marche(s["code"]) == g), key=val)
        autres = [s for s in legs if s is not faible]
        pris = {s["fixture_id"] for s in autres}
        cote_sans = math.prod(s["cote"] for s in autres)
        options = [c for c in cands
                   if c["fixture_id"] not in pris and groupe_marche(c["code"]) != g
                   and compte.get(groupe_marche(c["code"]), 0) < quota
                   and not (g == "victoire" and compte.get("victoire", 0) <= min_v)
                   and regles["cote_min"] <= cote_sans * c["cote"] <= regles["cote_max"]]
        if not options:
            break
        legs = autres + [max(options, key=val)]
    return legs


def construire_tiktok(pool, journal=print):
    """
    pool : sélections (sortie de selections_possibles) du jour visé, avec
           éventuellement s["estime"] = True quand la cote vient du moteur.
    Retourne jusqu'à 3 coupons de catégorie « tiktok » (profils solide / mixte / valeur).
    Priorité aux grosses affiches ; types de paris variés dans chaque combiné ;
    un match déjà pris dans un combiné précédent du jour est découragé (pas interdit).
    """
    if not pool:
        return []
    coupons, deja_match, deja_sel = [], {}, set()
    paliers = [(nom, [s for s in pool if _coupe(s) in ligues]) for nom, ligues in PALIERS_COUPES]
    paliers.append(("tous", list(pool)))
    for k, (profil, regles) in enumerate(PROFILS_TIKTOK.items(), 1):
        res, palier, cands = None, None, []
        for palier, sous_pool in paliers:
            if len({s["fixture_id"] for s in sous_pool}) < regles["legs_min"]:
                continue
            cands = []
            for s in sous_pool:
                if not (regles["cote_sel_min"] <= s["cote"] <= regles["cote_sel_max"] and s["p"] >= regles["p_sel_min"]):
                    continue
                b = notoriete(s) + BONUS_COUPES.get(_coupe(s), 0.0)
                if groupe_marche(s["code"]) == "victoire":
                    b += 0.15
                b -= 0.35 * deja_match.get(s["fixture_id"], 0)            # match déjà utilisé : découragé
                if (s["fixture_id"], s["code"]) in deja_sel:
                    b -= 0.6                                              # même pari déjà utilisé : fortement découragé
                cands.append({**s, "bonus": b})
            res = SEL.construire_un_coupon(cands, regles)
            if res is not None and _coherent(res["selections"]):
                break
            res = None
        if res is None:
            journal(f"   🎬 tiktok #{k} ({profil}) : impossible avec les matchs disponibles "
                    f"({len(cands)} sélection(s) éligibles)")
            continue
        legs = diversifier(res["selections"], cands, regles)
        if not _coherent(legs):
            legs = res["selections"]
        cote = round(math.prod(s["cote"] for s in legs), 2)
        for s in legs:
            deja_match[s["fixture_id"]] = deja_match.get(s["fixture_id"], 0) + 1
            deja_sel.add((s["fixture_id"], s["code"]))
        legs = [{kk: vv for kk, vv in s.items() if kk != "bonus"} for s in legs]
        n_est = sum(1 for s in legs if s.get("estime"))
        n_aff = sum(1 for s in legs if notoriete(s) >= 0.6)
        n_cup = sum(1 for s in legs if _coupe(s) in BONUS_COUPES)
        note = profil + (f" · {n_est} cote(s) estimée(s)" if n_est else "")
        coupons.append({"categorie": "tiktok", "numero": k, "selections": legs,
                        "cote_totale": cote, "note": note})
        types = {}
        for s in legs:
            types[groupe_marche(s["code"])] = types.get(groupe_marche(s["code"]), 0) + 1
        journal(f"   🎬 tiktok #{k} ({profil}) : {len(legs)} matchs, cote {cote}, "
                f"{n_aff} grosse(s) affiche(s), {n_cup} coupe(s) d'Europe, " + ", ".join(f"{v} {g}" for g, v in types.items())
                + (f", {n_est} estimée(s)" if n_est else ""))
    return coupons


def cotes_estimees(m, marge=1.06):
    """Cotes déduites des probabilités du moteur pour un match sans cotes
    publiées (jours lointains). Marquées comme estimées."""
    p1, pN, p2, pO, pB = m["p1"], m["pN"], m["p2"], m["pO25"], m["btts"]
    def c(p):
        return round(1 / max(p * marge, 0.02), 2)
    return {"1": c(p1), "N": c(pN), "2": c(p2), "1X": c(p1 + pN), "X2": c(pN + p2),
            "12": c(p1 + p2), "O2.5": c(pO), "BTTS": c(pB), "NOBTTS": c(1 - pB),
            **({"O1.5": c(m["pO15"])} if m.get("pO15") else {}),
            **({"U3.5": c(m["pU35"])} if m.get("pU35") else {})}
