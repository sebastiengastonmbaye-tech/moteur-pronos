#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
AURA90 — Sélection des coupons v2
=================================

Remplace la LOGIQUE de choix des sélections et de construction des coupons.
Catégories, tables Supabase, affichage : rien ne change.

Trois idées, toutes fondées sur des faits mesurés (diagnostic du 13/09 sur
99 coupons : le système gagne exactement au rythme que les cotes prévoient —
20 gagnés pour 21 attendus — donc pas de bug, mais une construction améliorable) :

1. PROBABILITÉ FUSIONNÉE
   Le moteur est bien calibré mais ne bat pas le marché (ROI −13 %). Quand il
   diverge du marché, c'est le marché qui a raison. On part donc de la
   probabilité du marché (cote débarrassée de la marge) et le moteur ne la
   corrige qu'à hauteur du poids POIDS_MOTEUR.

2. MOINS DE SÉLECTIONS, COTES INDIVIDUELLES BORNÉES
   Chaque sélection paie la marge du bookmaker : à cote totale égale, moins de
   matchs = plus de chances. Les cotes élevées sont sur-cotées (biais des
   outsiders) : on les plafonne par catégorie. Un coupon « Sûre » ne contient
   plus jamais une sélection à 2,07 avec 50 % de chances.

3. OBJECTIF = MAXIMISER LA PROBABILITÉ DE PASSER À COTE DONNÉE
   L'ancien constructeur empilait des cotes pour atteindre une cible. Le
   nouveau cherche, dans la fourchette de la catégorie, la combinaison qui a
   le plus de chances de passer — et ne publie rien sous un seuil.
"""

import math
from itertools import combinations

# ---------------------------------------------------------------------------
# RÉGLAGES
# ---------------------------------------------------------------------------

# Poids du moteur dans la fusion (0 = marché pur, 1 = moteur pur).
# 0,40 : le moteur (enrichi des absences, de la fatigue, des xG et du
# classement) pèse 40 % dans la probabilité finale, le marché 60 %.
POIDS_MOTEUR = 0.40

# Clés EXACTES de la colonne coupons.categorie en base.
# cote_min/max      : fourchette de cote totale
# legs_min/max      : nombre de sélections
# cote_sel_min/max  : bornes de la cote INDIVIDUELLE
# p_sel_min         : probabilité fusionnée minimale d'une sélection
# p_coupon_min      : sous ce seuil, le coupon n'est pas publié
# max_coupons       : coupons par catégorie et par jour (plafonné aussi par le
#                     nombre de matchs disponibles, voir construire_coupons)
CATEGORIES = {
    "sure": dict(
        cote_min=1.60, cote_max=2.60, legs_min=2, legs_max=3,
        cote_sel_min=1.15, cote_sel_max=1.75, p_sel_min=0.55,
        p_coupon_min=0.38, max_coupons=3),
    "confiance": dict(
        cote_min=3.00, cote_max=10.0, legs_min=2, legs_max=4,
        cote_sel_min=1.25, cote_sel_max=2.20, p_sel_min=0.45,
        p_coupon_min=0.12, max_coupons=3),
    "fun": dict(
        cote_min=10.0, cote_max=22.0, legs_min=3, legs_max=5,
        cote_sel_min=1.30, cote_sel_max=2.60, p_sel_min=0.38,
        p_coupon_min=0.05, max_coupons=2, valeur=True),
    "grosses": dict(
        cote_min=30.0, cote_max=150.0, legs_min=3, legs_max=6,
        cote_sel_min=1.40, cote_sel_max=3.00, p_sel_min=0.33,
        p_coupon_min=0.008, max_coupons=2, valeur=True),
    "montante": dict(
        cote_min=1.40, cote_max=1.80, legs_min=1, legs_max=2,
        cote_sel_min=1.15, cote_sel_max=1.60, p_sel_min=0.62,
        p_coupon_min=0.50, max_coupons=1),
    "nuit": dict(
        cote_min=2.00, cote_max=5.00, legs_min=2, legs_max=3,
        cote_sel_min=1.20, cote_sel_max=2.20, p_sel_min=0.45,
        p_coupon_min=0.20, max_coupons=3),
}
# NB : le constructeur se place toujours au plus près de cote_min (c'est là que
# les chances sont maximales). Pour des grosses cotes plus hautes, monter
# cote_min de « grosses » — en sachant que chaque doublement de la cote divise
# les chances par deux.
# ordre de rotation : chaque catégorie reçoit son 1er coupon avant qu'une autre
# en reçoive un 2e. Sûre d'abord : c'est le produit qui fidélise.
ORDRE_JOUR = ["sure", "confiance", "fun", "grosses", "montante"]
ORDRE_NUIT = ["nuit"]

# Famille de marché : un seul code par match et par famille, tous coupons
# confondus (jamais « 1 » ici et « X2 » là) ; un seul marché par match dans
# un même coupon.
FAMILLE = {
    "1": "resultat", "N": "resultat", "2": "resultat",
    "1X": "resultat", "X2": "resultat", "12": "resultat",
    "O2.5": "buts", "U2.5": "buts",
    "BTTS": "btts", "NOBTTS": "btts",
}
CODES_DC = {"1X": ("1", "N"), "X2": ("N", "2"), "12": ("1", "2")}

# Les coupes d'Europe n'entrent pas dans Sûre ni Montante tant que la
# calibration inter-ligues n'est pas validée par backtest.
LIGUES_PRUDENCE = {"Ligue des Champions", "Ligue Europa"}
CATEGORIES_PRUDENCE = {"sure", "montante"}


# ---------------------------------------------------------------------------
# 1. PROBABILITÉS
# ---------------------------------------------------------------------------

def probas_marche(cotes):
    """Retire la marge du bookmaker en normalisant les probabilités implicites."""
    inv = {k: 1.0 / c for k, c in cotes.items() if c and c > 1.0}
    total = sum(inv.values())
    if not inv or total <= 0:
        return {}
    return {k: v / total for k, v in inv.items()}


def fusionner(p_marche, p_moteur, w=POIDS_MOTEUR):
    """p_fusion = p_marche × (p_moteur / p_marche)^w, renormalisé sur le marché."""
    if not p_marche:
        return {}
    communs = [k for k in p_marche if p_moteur.get(k)]
    tot = sum(p_moteur[k] for k in communs)
    if communs and tot > 0:
        p_moteur = {k: p_moteur[k] / tot for k in communs}
    brut = {}
    for k, pm in p_marche.items():
        pe = p_moteur.get(k)
        brut[k] = pm if (pe is None or pe <= 0 or pm <= 0) else pm * (pe / pm) ** w
    total = sum(brut.values())
    return {k: v / total for k, v in brut.items()}


def preparer_selections(matchs, w=POIDS_MOTEUR):
    """
    Entrée : liste de matchs, chacun un dict avec au moins :
      fixture_id, ligue, dom, ext, date_match, heure, logo_dom, logo_ext,
      cotes  = {"1":…, "N":…, "2":…, "1X":…, "X2":…, "12":…, "O2.5":…, "U2.5":…, "BTTS":…, "NOBTTS":…}
      moteur = {"1": p, "N": p, "2": p, "O2.5": p, "BTTS": p}   (probabilités 0-1)
    Sortie : sélections candidates avec probabilité fusionnée.
    """
    selections = []
    for mt in matchs:
        cotes, mot = mt["cotes"], mt["moteur"]
        pf = {}                              # probabilité fusionnée par code

        # 1X2 : marché de référence
        c1x2 = {k: cotes[k] for k in ("1", "N", "2") if cotes.get(k)}
        if len(c1x2) == 3:
            pm = probas_marche(c1x2)
            pf.update(fusionner(pm, {k: mot.get(k) for k in c1x2}, w))
            # double chance DÉRIVÉE du 1X2 fusionné (cohérence garantie)
            for dc, (a, b) in CODES_DC.items():
                if cotes.get(dc):
                    pf[dc] = pf[a] + pf[b]

        # buts
        cou = {k: cotes[k] for k in ("O2.5", "U2.5") if cotes.get(k)}
        if len(cou) == 2:
            pm = probas_marche(cou)
            pe = {"O2.5": mot.get("O2.5")}
            if pe["O2.5"]:
                pe["U2.5"] = 1 - pe["O2.5"]
            pf.update(fusionner(pm, pe, w))

        # BTTS
        cb = {k: cotes[k] for k in ("BTTS", "NOBTTS") if cotes.get(k)}
        if len(cb) == 2:
            pm = probas_marche(cb)
            pe = {"BTTS": mot.get("BTTS")}
            if pe["BTTS"]:
                pe["NOBTTS"] = 1 - pe["BTTS"]
            pf.update(fusionner(pm, pe, w))

        for code, p in pf.items():
            if code == "N" or code not in FAMILLE or not cotes.get(code):
                continue                      # le nul sec n'est jamais proposé
            s = {k: mt.get(k) for k in ("fixture_id", "ligue", "dom", "ext", "date_match",
                                          "heure", "logo_dom", "logo_ext")}
            s.update({
                "code": code, "famille": FAMILLE[code],
                "cote": float(cotes[code]), "p": float(p),
                "p_moteur": mot.get(code),
                "valeur": float(p) * float(cotes[code]),   # > 1 : le marché sous-paie
            })
            selections.append(s)
    return selections


# ---------------------------------------------------------------------------
# 2. CONSTRUCTION D'UN COUPON
# ---------------------------------------------------------------------------

def _score(coupon):
    p = 1.0
    for s in coupon:
        p *= s["p"]
    return p


def _cote(coupon):
    c = 1.0
    for s in coupon:
        c *= s["cote"]
    return c


def _valide(coupon, r):
    n = len(coupon)
    if not (r["legs_min"] <= n <= r["legs_max"]):
        return False
    if not (r["cote_min"] <= _cote(coupon) <= r["cote_max"]):
        return False
    ids = [s["fixture_id"] for s in coupon]
    return len(ids) == len(set(ids))          # un seul marché par match


def _eligibles(selections, cat, r, utilises, codes_par_match):
    out = []
    for s in selections:
        if cat in CATEGORIES_PRUDENCE and s["ligue"] in LIGUES_PRUDENCE:
            continue
        if not (r["cote_sel_min"] <= s["cote"] <= r["cote_sel_max"]):
            continue
        if s["p"] < r["p_sel_min"]:
            continue
        if (s["fixture_id"], s["code"]) in utilises:
            continue
        deja = codes_par_match.get((s["fixture_id"], s["famille"]))
        if deja is not None and deja != s["code"]:
            continue
        out.append(s)
    return out


def construire_un_coupon(candidats, r):
    """
    Combinaison qui MAXIMISE la probabilité de passer dans la fourchette de
    cote. On commence par le plus petit nombre de sélections : pour n ≤ 3 on
    énumère les combinaisons des meilleurs candidats ; au-delà, glouton puis
    amélioration par échanges. On s'arrête au premier n qui donne un coupon
    valide (moins de sélections = moins de marges payées).
    """
    if not candidats:
        return None

    def efficacite(s):        # cote gagnée par unité de probabilité perdue
        return math.log(s["cote"]) / max(-math.log(s["p"]), 1e-9)
    if r.get("valeur"):        # catégories offensives : la valeur d'abord
        tri = sorted(candidats, key=lambda s: (s["valeur"], s["p"]), reverse=True)
    else:
        tri = sorted(candidats, key=lambda s: (efficacite(s), s["p"]), reverse=True)

    meilleur, meilleur_p = None, 0.0
    for n in range(r["legs_min"], r["legs_max"] + 1):
        if n <= 3:
            pool = tri[:20]
            for combo in combinations(pool, n):
                combo = list(combo)
                if _valide(combo, r):
                    p = _score(combo)
                    if p > meilleur_p:
                        meilleur, meilleur_p = combo, p
        else:
            coupon, vus = [], set()
            for s in tri:
                if len(coupon) >= n:
                    break
                if s["fixture_id"] in vus or _cote(coupon) * s["cote"] > r["cote_max"]:
                    continue
                coupon.append(s); vus.add(s["fixture_id"])
            if _valide(coupon, r):
                ameliore = True
                while ameliore:
                    ameliore = False
                    for i in range(len(coupon)):
                        autres = {c["fixture_id"] for j, c in enumerate(coupon) if j != i}
                        for s in tri:
                            if s in coupon or s["fixture_id"] in autres:
                                continue
                            essai = coupon[:i] + [s] + coupon[i + 1:]
                            if _valide(essai, r) and _score(essai) > _score(coupon) * 1.001:
                                coupon, ameliore = essai, True
                                break
                        if ameliore:
                            break
                p = _score(coupon)
                if p > meilleur_p:
                    meilleur, meilleur_p = coupon, p
        if meilleur is not None:
            break
    if meilleur is None or meilleur_p < r["p_coupon_min"]:
        return None
    return {"selections": meilleur, "cote_totale": round(_cote(meilleur), 2),
            "p_estimee": round(meilleur_p, 4)}


# ---------------------------------------------------------------------------
# 3. TOUS LES COUPONS
# ---------------------------------------------------------------------------

def construire_coupons(selections, ordre=ORDRE_JOUR, journal=print):
    """
    Construit les coupons des catégories données, en rotation. Une sélection
    ne sert qu'une fois ; jamais deux codes contradictoires sur un même match.
    Le nombre de coupons par catégorie est aussi plafonné par le nombre de
    matchs disponibles (pas de remplissage les jours creux).
    """
    nb_matchs = len({s["fixture_id"] for s in selections})
    utilises, codes_par_match, coupons = set(), {}, []
    compte = {c: 0 for c in ordre}
    epuisees = set()

    def plafond(cat):
        r = CATEGORIES[cat]
        return min(r["max_coupons"], max(1, nb_matchs // (2 * r["legs_min"])))

    while len(epuisees) < len(ordre):
        progres = False
        for cat in ordre:
            r = CATEGORIES[cat]
            if cat in epuisees:
                continue
            if compte[cat] >= plafond(cat):
                epuisees.add(cat); continue
            cands = _eligibles(selections, cat, r, utilises, codes_par_match)
            coupon = construire_un_coupon(cands, r)
            if coupon is None:
                epuisees.add(cat)
                if compte[cat] == 0:
                    journal(f"   ⚠️ {cat} : aucun coupon possible "
                            f"({len(cands)} sélection(s) éligibles)")
                continue
            compte[cat] += 1
            coupon["categorie"], coupon["numero"] = cat, compte[cat]
            for s in coupon["selections"]:
                utilises.add((s["fixture_id"], s["code"]))
                codes_par_match[(s["fixture_id"], s["famille"])] = s["code"]
            coupons.append(coupon)
            journal(f"   ✅ {cat} #{compte[cat]} : {len(coupon['selections'])} match(s), "
                    f"cote {coupon['cote_totale']}")
            progres = True
        if not progres:
            break
    return coupons
