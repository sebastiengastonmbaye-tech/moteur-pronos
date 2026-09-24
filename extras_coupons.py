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
    # même optimiseur que les coupons : maximise les chances de passer dans
    # la fourchette de cote, pour un nombre de matchs donné
    "solide": dict(cote_min=90.0, cote_max=350.0, legs_min=12, legs_max=15,
                   cote_sel_min=1.15, cote_sel_max=1.80, p_sel_min=0.55, p_coupon_min=1e-6),
    "mixte":  dict(cote_min=90.0, cote_max=350.0, legs_min=10, legs_max=13,
                   cote_sel_min=1.25, cote_sel_max=2.40, p_sel_min=0.45, p_coupon_min=1e-6),
    "valeur": dict(cote_min=90.0, cote_max=500.0, legs_min=10, legs_max=12,
                   cote_sel_min=1.45, cote_sel_max=3.00, p_sel_min=0.38, p_coupon_min=1e-6, valeur=True),
}


def _coherent(legs):
    """Un seul marché par match — garanti par l'optimiseur — et cotes valides."""
    ids = [s["fixture_id"] for s in legs]
    return len(ids) == len(set(ids))


def construire_tiktok(pool, journal=print):
    """
    pool : sélections (sortie de selections_possibles) du jour visé, avec
           éventuellement s["estime"] = True quand la cote vient du moteur.
    Retourne jusqu'à 3 coupons de catégorie « tiktok » (profils solide / mixte / valeur).
    Le même match peut servir dans plusieurs combinés.
    """
    if not pool:
        return []
    coupons = []
    for k, (profil, regles) in enumerate(PROFILS_TIKTOK.items(), 1):
        cands = [s for s in pool if regles["cote_sel_min"] <= s["cote"] <= regles["cote_sel_max"]
                 and s["p"] >= regles["p_sel_min"]]
        res = SEL.construire_un_coupon(cands, regles)
        if res is None or not _coherent(res["selections"]):
            journal(f"   🎬 tiktok #{k} ({profil}) : impossible avec les matchs disponibles "
                    f"({len(cands)} sélection(s) éligibles)")
            continue
        legs = res["selections"]
        n_est = sum(1 for s in legs if s.get("estime"))
        note = profil + (f" · {n_est} cote(s) estimée(s)" if n_est else "")
        coupons.append({"categorie": "tiktok", "numero": k, "selections": legs,
                        "cote_totale": res["cote_totale"], "note": note})
        journal(f"   🎬 tiktok #{k} ({profil}) : {len(legs)} matchs, cote {res['cote_totale']}"
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
