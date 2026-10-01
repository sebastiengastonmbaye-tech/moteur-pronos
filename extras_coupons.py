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
SCORE_MAX_MATCHS = 5        # matchs par jour dans la section Score exact
SCORE_P_MIN = 9.0           # % minimum du meilleur score pour qu'un match soit retenu
SCORE_RATIO_UN_SEUL = 1.6   # si le 1er score domine le 2e de ce facteur → un seul score
MARGE_ESTIMEE = 0.85        # cote estimée = 0,85 / p quand le bookmaker n'a pas de cote


def _code_score(sc):
    return "SE:" + sc.replace(":", "-").strip()


def construire_scores_exacts(matchs, cotes_brutes, journal=print):
    """
    matchs        : sortie de candidats() — chaque match porte "scores"
                    ([{"score": "2-1", "proba": 14.2}, …], du moteur enrichi)
    cotes_brutes  : {fixture_id: {"SE:2-1": 7.5, …}} (cotes Score exact du bookmaker)
    Retourne des coupons prêts pour enregistrer().
    """
    retenus = []
    for m in matchs:
        scores = [s for s in (m.get("scores") or []) if s.get("score")]
        if not scores:
            continue
        s1 = scores[0]
        if float(s1.get("proba", 0)) < SCORE_P_MIN:
            continue
        retenus.append((float(s1["proba"]), m, scores))
    retenus.sort(key=lambda t: -t[0])

    coupons = []
    for k, (p1, m, scores) in enumerate(retenus[:SCORE_MAX_MATCHS], 1):
        liste = [scores[0]]
        if len(scores) > 1 and float(scores[0]["proba"]) < SCORE_RATIO_UN_SEUL * float(scores[1]["proba"]):
            liste.append(scores[1])
        sels = []
        for sc in liste:
            code = _code_score(sc["score"])
            cote = (cotes_brutes.get(m["fixture_id"]) or {}).get(code)
            if not cote:
                cote = round(MARGE_ESTIMEE / max(float(sc["proba"]) / 100, 0.02), 2)
            sels.append({
                **{c: m.get(c) for c in ("fixture_id", "ligue", "dom", "ext", "date_match",
                                          "heure", "logo_dom", "logo_ext")},
                "marche": "Score exact",
                "selection": "Score exact " + sc["score"].replace(":", "-"),
                "code": code, "cote": float(cote),
                "confiance": round(float(sc["proba"])),
                "famille": "score", "p": float(sc["proba"]) / 100,
            })
        coupons.append({"categorie": "score", "numero": k, "selections": sels,
                        "cote_totale": sels[0]["cote"]})
        journal(f"   🎯 score exact #{k} : {m['dom']} – {m['ext']} → "
                + " / ".join(s["selection"].replace("Score exact ", "") for s in sels))
    if not coupons:
        journal("   🎯 score exact : aucun match assez net aujourd'hui")
    return coupons


# ----------------------------------------------------------------- TIKTOK
TIKTOK_MIN_LEGS = 10        # (utilisé par le générateur pour savoir si la journée est jouable)
TIKTOK_JOURS = 6            # J+1 … J+6
TIKTOK_FIGE_A = 2           # figé à partir de J+2


PROFILS_TIKTOK = {
    # même optimiseur que les coupons, avec en plus un bonus « grosse affiche »
    # et une pénalité de répétition (bonus_poids) pour des combinés qui donnent envie
    "solide": dict(cote_min=90.0, cote_max=350.0, legs_min=12, legs_max=15,
                   cote_sel_min=1.15, cote_sel_max=1.80, p_sel_min=0.55, p_coupon_min=1e-6, bonus_poids=0.30),
    "mixte":  dict(cote_min=90.0, cote_max=350.0, legs_min=10, legs_max=13,
                   cote_sel_min=1.25, cote_sel_max=2.40, p_sel_min=0.45, p_coupon_min=1e-6, bonus_poids=0.30),
    "valeur": dict(cote_min=90.0, cote_max=200.0, legs_min=10, legs_max=12,
                   cote_sel_min=1.45, cote_sel_max=3.00, p_sel_min=0.38, p_coupon_min=1e-6, valeur=True, bonus_poids=0.30),
}

# ---------------------------------------------------------------- AFFICHES
# Notoriété d'un match pour TikTok : ce qui fait cliquer, c'est un Real–Atlético,
# pas un Îles Féroé–Kazakhstan (constat sur les vidéos de Babs : 27 000 vues avec
# des grands clubs, 1 200 avec des petites sélections).
NOTORIETE_LIGUE = {
    "ligue des champions": 1.0, "premier league": 1.0, "la liga": 0.95, "serie a": 0.9,
    "bundesliga": 0.9, "ligue 1": 0.95, "ligue europa": 0.8, "uefa nations league": 0.65,
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
    lig = NOTORIETE_LIGUE.get(_norm(s.get("ligue")), 0.3)
    return round(0.45 * max(e1, e2) + 0.25 * min(e1, e2) + 0.30 * lig, 3)


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
    for k, (profil, regles) in enumerate(PROFILS_TIKTOK.items(), 1):
        cands = []
        for s in pool:
            if not (regles["cote_sel_min"] <= s["cote"] <= regles["cote_sel_max"] and s["p"] >= regles["p_sel_min"]):
                continue
            b = notoriete(s)
            if groupe_marche(s["code"]) == "victoire":
                b += 0.15
            b -= 0.35 * deja_match.get(s["fixture_id"], 0)            # match déjà utilisé : découragé
            if (s["fixture_id"], s["code"]) in deja_sel:
                b -= 0.6                                              # même pari déjà utilisé : fortement découragé
            cands.append({**s, "bonus": b})
        res = SEL.construire_un_coupon(cands, regles)
        if res is None or not _coherent(res["selections"]):
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
        note = profil + (f" · {n_est} cote(s) estimée(s)" if n_est else "")
        coupons.append({"categorie": "tiktok", "numero": k, "selections": legs,
                        "cote_totale": cote, "note": note})
        types = {}
        for s in legs:
            types[groupe_marche(s["code"])] = types.get(groupe_marche(s["code"]), 0) + 1
        journal(f"   🎬 tiktok #{k} ({profil}) : {len(legs)} matchs, cote {cote}, "
                f"{n_aff} grosse(s) affiche(s), " + ", ".join(f"{v} {g}" for g, v in types.items())
                + (f", {n_est} estimée(s)" if n_est else ""))
    return coupons


def cotes_estimees(m, marge=1.06):
    """Cotes déduites des probabilités du moteur pour un match sans cotes
    publiées (jours lointains). Marquées comme estimées."""
    p1, pN, p2, pO, pB = m["p1"], m["pN"], m["p2"], m["pO25"], m["btts"]
    def c(p):
        return round(1 / max(p * marge, 0.02), 2)
    return {"1": c(p1), "N": c(pN), "2": c(p2), "1X": c(p1 + pN), "X2": c(pN + p2),
            "12": c(p1 + p2), "O2.5": c(pO), "U2.5": c(1 - pO), "BTTS": c(pB), "NOBTTS": c(1 - pB)}
