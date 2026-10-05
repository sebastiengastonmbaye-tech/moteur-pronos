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
    # SÛRE et MONTANTE : uniquement des sélections de QUALITÉ « PRONO SIGNÉ »,
    # c'est-à-dire celles que le moteur signerait dans la liste principale
    # (victoire ≥ 70 %, double chance ≥ 80 %, buts/BTTS ≥ 70 %). Ce sont les
    # mêmes analyses que celles qui font 85 % au palmarès. Deux sélections de
    # ce niveau, c'est le maximum de fiabilité possible pour un combiné.
    #
    # beta : ce que l'optimiseur maximise (voir construire_un_coupon)
    #   0   → probabilité pure du coupon (catégories prudentes)
    #   1   → probabilité rapportée à la cote (catégories offensives)
    # zones : plages de recherche successives (1er coupon, 2e, 3e…), la cible
    #   n'est qu'un départage final ; table_legs : applique la table cote → matchs
    "sure": dict(
        cote_min=1.45, cote_max=2.40, legs_min=2, legs_max=2,
        cote_sel_min=1.12, cote_sel_max=1.60, p_sel_min=0.62,
        moteur_min=0.70, moteur_min_dc=0.80,
        p_coupon_min=0.42, max_coupons=3, beta=0.0, table_legs=True,
        cascade=True, plancher=0.70, europe_ok=True),
    "confiance": dict(
        cote_min=2.60, cote_max=7.50, legs_min=2, legs_max=6,
        cote_sel_min=1.25, cote_sel_max=2.20, p_sel_min=0.45,
        p_coupon_min=0.12, max_coupons=3, beta=0.5, table_legs=True,
        cascade=True, plancher=0.65,
        zones=[(2.6, 3.6, 3.0), (3.8, 5.2, 4.5), (5.2, 7.5, 6.0)]),
    "fun": dict(
        cote_min=8.0, cote_max=18.0, legs_min=4, legs_max=7,
        cote_sel_min=1.30, cote_sel_max=2.60, p_sel_min=0.38,
        p_coupon_min=0.05, max_coupons=2, valeur=True, beta=1.0, table_legs=True,
        cascade=True, plancher=0.60,
        zones=[(8.0, 12.0, 10.0), (12.0, 18.0, 15.0)]),
    # GROSSES COTES : 1er coupon dans 30-45, 2e dans 60-90
    "grosses": dict(
        cote_min=30.0, cote_max=90.0, legs_min=7, legs_max=11,
        cote_sel_min=1.40, cote_sel_max=3.00, p_sel_min=0.33,
        p_coupon_min=0.006, max_coupons=2, valeur=True, beta=1.0, table_legs=True,
        cascade=True, plancher=0.60,
        zones=[(30.0, 45.0, 35.0), (60.0, 90.0, 70.0)]),
    "montante": dict(
        cote_min=1.40, cote_max=1.80, legs_min=1, legs_max=2,
        cote_sel_min=1.15, cote_sel_max=1.60, p_sel_min=0.65,
        moteur_min=0.70, moteur_min_dc=0.80,
        p_coupon_min=0.55, max_coupons=1, beta=0.0, table_legs=True,
        repli=dict(p_sel_min=0.68, moteur_min=0.60, moteur_min_dc=0.75,
                   p_coupon_min=0.52, europe_ok=True)),
    # MONTANTE TURBO (5 paliers, cote 1,90–2,60) : même exigence que la montante
    # de base — sélections de niveau « prono signé » (repli : favoris nets du
    # marché). Pour atteindre ~2, l'optimiseur préfère deux favoris solides à
    # un seul pari à 2 : c'est ce qui passe le plus souvent sur AURA90.
    "turbo": dict(
        cote_min=1.90, cote_max=2.60, legs_min=1, legs_max=3,
        cote_sel_min=1.15, cote_sel_max=2.30, p_sel_min=0.60,
        moteur_min=0.72, moteur_min_dc=0.82,
        p_coupon_min=0.40, max_coupons=1, beta=0.0, table_legs=True,
        repli=dict(p_sel_min=0.56, moteur_min=0.62, moteur_min_dc=0.76,
                   p_coupon_min=0.38, europe_ok=True)),
    "nuit": dict(
        cote_min=2.00, cote_max=5.00, legs_min=2, legs_max=4,
        cote_sel_min=1.20, cote_sel_max=2.20, p_sel_min=0.45,
        p_coupon_min=0.20, max_coupons=3, beta=0.0, table_legs=True,
        cascade=True, plancher=0.65),
}

# SÛRE / MONTANTE — palier de REPLI (28/09) : si aucune combinaison ne passe
# le niveau « prono signé », on accepte des favoris nets pour le marché
# (probabilité fusionnée ≥ 66-68 %) sur lesquels le moteur n'est pas en
# désaccord (≥ 60 %, double chance ≥ 74-75 %), coupes d'Europe comprises.
# Toujours plus strict que Confiance ; jamais de remplissage.

# Nombre de sélections cohérent avec la cote totale (table de Babs, 25/09).
# Plages de construction : l'optimiseur choisit dans la plage, jamais au-delà.
TABLE_LEGS = [
    (0.0,   2.0,   1, 2),
    (2.0,   3.0,   2, 3),
    (3.0,   5.0,   3, 4),
    (5.0,   10.0,  4, 6),
    (10.0,  20.0,  5, 7),
    (20.0,  50.0,  7, 9),
    (50.0,  100.0, 8, 11),
    (100.0, 150.0, 10, 13),
    (150.0, 1e9,   10, 15),
]


def plage_legs(cote):
    for a, b, lo, hi in TABLE_LEGS:
        if a <= cote < b:
            return lo, hi
    return 1, 15
# ordre de rotation : chaque catégorie reçoit son 1er coupon avant qu'une autre
# en reçoive un 2e. Sûre d'abord : c'est le produit qui fidélise.
ORDRE_JOUR = ["montante", "turbo", "sure", "confiance", "fun", "grosses"]   # les montantes se servent en premier
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
CATEGORIES_PRUDENCE = {"sure", "montante", "turbo"}
MONTANTES = {"montante", "turbo"}   # leurs matchs ne sont repris par AUCUN autre coupon le même jour

# CASCADE DE CONFIANCE (05/10/2026) — même philosophie que les montantes, pour
# Sûre, Confiance, Fun, Grosses cotes et Nuit : chaque coupon est construit avec
# les seuls pronos que le moteur juge au moins à X %, en commençant par 95 % et
# en descendant palier par palier jusqu'au plancher de la catégorie. Le coupon
# retenu est donc celui du niveau le plus strict qui permet de l'atteindre.
#   · double chance : seuil +10 points (elles sont naturellement plus probables)
#   · accord du marché : probabilité fusionnée ≥ seuil − 8 points, sinon c'est
#     le moteur qui se trompe le plus souvent → sélection écartée
PALIERS_CONFIANCE = [0.95, 0.90, 0.85, 0.80, 0.75, 0.70, 0.65, 0.60]
ACCORD_MARCHE = 0.08


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
            pm_code = mot.get(code)
            if code in CODES_DC and all(mot.get(k) for k in CODES_DC[code]):
                pm_code = mot[CODES_DC[code][0]] + mot[CODES_DC[code][1]]
            elif code == "U2.5" and mot.get("O2.5"):
                pm_code = 1 - mot["O2.5"]
            elif code == "NOBTTS" and mot.get("BTTS"):
                pm_code = 1 - mot["BTTS"]
            s.update({
                "code": code, "famille": FAMILLE[code],
                "cote": float(cotes[code]), "p": float(p),
                "p_moteur": pm_code,
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
        if cat in CATEGORIES_PRUDENCE and s["ligue"] in LIGUES_PRUDENCE and not r.get("europe_ok"):
            continue
        if not (r["cote_sel_min"] <= s["cote"] <= r["cote_sel_max"]):
            continue
        if s["p"] < r["p_sel_min"]:
            continue
        if r.get("moteur_min"):
            seuil = r["moteur_min_dc"] if s["code"] in CODES_DC else r["moteur_min"]
            if s.get("p_moteur") is None or s["p_moteur"] < seuil:
                continue
            if r.get("accord") is not None and s["p"] < seuil - r["accord"]:
                continue
        if (s["fixture_id"], s["code"]) in utilises:
            continue
        deja = codes_par_match.get((s["fixture_id"], s["famille"]))
        if deja is not None and deja != s["code"]:
            continue
        out.append(s)
    return out


PAS_LOG = 0.004        # finesse de la grille de cote (0,4 % par case)
TOLERANCE_EGALITE = 0.97   # scores à moins de 3 % : départage par le nombre de matchs
MAX_PAR_MATCH = 4          # sélections candidates gardées par match


def construire_un_coupon(candidats, r):
    """
    Optimiseur de combinaison (remplace l'ancien glouton + « réparation »).

    Parmi les sélections éligibles, cherche la combinaison qui MAXIMISE

        score = P_coupon × cote_totale ^ beta

    sous les contraintes : cote totale dans [cote_min, cote_max], un seul match
    par coupon, nombre de sélections dans [legs_min, legs_max] ET dans la plage
    de la table cote → matchs.

      beta = 0  : probabilité pure (Sûre, Montante, Nuit)
      beta = 1  : probabilité rapportée à la cote (Fun, Grosses). Sans ce
                  rapport, « maximiser la probabilité dans une plage » revient à
                  toujours prendre la plus petite cote de la plage.

    Méthode : programmation dynamique exacte sur une grille de cote (sac à dos
    par groupes : au plus une sélection par match). Tous les nombres de matchs
    sont comparés entre eux — rien n'est ajouté ni remplacé pour « atteindre »
    une cote. Départage : score (à 3 % près) → moins de matchs → proximité de
    la cible. Retourne None si aucune combinaison ne passe les seuils.
    """
    if not candidats:
        return None
    import numpy as np

    beta = float(r.get("beta", 1.0 if r.get("valeur") else 0.0))
    cmin, cmax = float(r["cote_min"]), float(r["cote_max"])
    lmin, lmax = int(r["legs_min"]), int(r["legs_max"])
    cible = float(r.get("cible") or math.sqrt(cmin * cmax))
    avec_table = bool(r.get("table_legs"))

    # candidats regroupés par match, les meilleurs de chaque match seulement
    poids_bonus = float(r.get("bonus_poids", 0.0))   # TikTok : affiche des matchs / variété

    def valeur_leg(s):
        return math.log(s["p"]) + beta * math.log(s["cote"]) + poids_bonus * s.get("bonus", 0.0)
    groupes = {}
    for s in candidats:
        if s["cote"] <= 1.0 or s["p"] <= 0:
            continue
        groupes.setdefault(s["fixture_id"], []).append(s)
    groupes = [sorted(g, key=valeur_leg, reverse=True)[:MAX_PAR_MATCH] for g in groupes.values()]
    if len(groupes) < lmin:
        return None

    B = int(math.ceil(math.log(cmax) / PAS_LOG)) + lmax + 2
    N = lmax
    NEG = -1e18
    meilleur = np.full((N + 1, B), NEG)
    meilleur[0, 0] = 0.0
    choix = []            # par match : indice de la sélection retenue (0 = aucune)
    for g in groupes:
        nouv = meilleur.copy()
        ch = np.zeros((N + 1, B), dtype=np.int8)
        for i, s in enumerate(g, 1):
            w = int(round(math.log(s["cote"]) / PAS_LOG))
            if w <= 0 or w >= B:
                continue
            cand = np.full((N + 1, B), NEG)
            cand[1:, w:] = meilleur[:-1, :B - w] + valeur_leg(s)
            masque = cand > nouv
            nouv[masque] = cand[masque]
            ch[masque] = i
        meilleur = nouv
        choix.append(ch)

    def reconstruire(n, b):
        sels = []
        for k in range(len(groupes) - 1, -1, -1):
            i = int(choix[k][n, b])
            if i:
                s = groupes[k][i - 1]
                sels.append(s)
                n -= 1
                b -= int(round(math.log(s["cote"]) / PAS_LOG))
        return sels[::-1]

    # toutes les solutions optimales par (nombre de matchs, case de cote) dans la plage
    b_lo = max(0, int(math.floor(math.log(cmin) / PAS_LOG)) - lmax)
    b_hi = min(B - 1, int(math.ceil(math.log(cmax) / PAS_LOG)) + lmax)
    valides = []
    for n in range(max(1, lmin), lmax + 1):
        lignes = meilleur[n, b_lo:b_hi + 1]
        for db in np.nonzero(lignes > NEG / 2)[0]:
            sels = reconstruire(n, b_lo + int(db))
            if len(sels) != n:
                continue
            c, p = _cote(sels), _score(sels)
            if not (cmin <= c <= cmax):
                continue
            if avec_table:
                lo, hi = plage_legs(c)
                if not (lo <= n <= hi):
                    continue
            if p < r["p_coupon_min"] or not _valide(sels, r):
                continue
            bonus = math.exp(poids_bonus * sum(x.get("bonus", 0.0) for x in sels)) if poids_bonus else 1.0
            valides.append((p * c ** beta * bonus, n, abs(math.log(c / cible)), p, c, sels))
    if not valides:
        return None

    top = max(v[0] for v in valides)
    proches = [v for v in valides if v[0] >= top * TOLERANCE_EGALITE]
    score, n, dist, p, c, sels = min(proches, key=lambda v: (v[1], v[2], -v[0]))
    return {"selections": sels, "cote_totale": round(c, 2), "p_estimee": round(p, 4)}


def construire_coupons(selections, ordre=ORDRE_JOUR, journal=print):
    """
    Construit les coupons des catégories données, en rotation. Une sélection
    ne sert qu'une fois ; jamais deux codes contradictoires sur un même match.
    Le nombre de coupons par catégorie est aussi plafonné par le nombre de
    matchs disponibles (pas de remplissage les jours creux).
    """
    nb_matchs = len({s["fixture_id"] for s in selections})
    utilises, codes_par_match, coupons = set(), {}, []
    matchs_montantes = set()
    compte = {c: 0 for c in ordre}
    epuisees = set()

    def plafond(cat):
        r = CATEGORIES[cat]
        return min(r["max_coupons"], max(1, nb_matchs // (2 * r["legs_min"])))

    while len(epuisees) < len(ordre):
        progres = False
        for cat in ordre:
            r = dict(CATEGORIES[cat])
            if cat in epuisees:
                continue
            if compte[cat] >= plafond(cat):
                epuisees.add(cat); continue
            if r.get("zones"):            # 1er coupon cherche dans la 1re zone, 2e dans la 2e…
                zmin, zmax, zcible = r["zones"][min(compte[cat], len(r["zones"]) - 1)]
                r["cote_min"], r["cote_max"], r["cible"] = zmin, zmax, zcible
            filtre = lambda l: [x for x in l if x["fixture_id"] not in matchs_montantes]
            if r.get("cascade"):
                coupon, cands = None, []
                for t in PALIERS_CONFIANCE:
                    if t < r["plancher"] - 1e-9:
                        break
                    rt = {**r, "moteur_min": t, "moteur_min_dc": min(0.97, t + 0.10), "accord": ACCORD_MARCHE}
                    cands = filtre(_eligibles(selections, cat, rt, utilises, codes_par_match))
                    coupon = construire_un_coupon(cands, rt)
                    if coupon is not None:
                        coupon["niveau"] = t
                        break
            else:
                cands = filtre(_eligibles(selections, cat, r, utilises, codes_par_match))
                coupon = construire_un_coupon(cands, r)
            if coupon is None and r.get("repli"):
                r2 = {**r, **r["repli"]}
                cands = filtre(_eligibles(selections, cat, r2, utilises, codes_par_match))
                coupon = construire_un_coupon(cands, r2)
                if coupon is not None:
                    journal(f"   ↪ {cat} : palier de repli (favoris nets du marché)")
            if coupon is None:
                epuisees.add(cat)
                if compte[cat] == 0:
                    journal(f"   ⚠️ {cat} : aucun coupon possible aujourd'hui")
                continue
            compte[cat] += 1
            coupon["categorie"], coupon["numero"] = cat, compte[cat]
            for s in coupon["selections"]:
                if cat in MONTANTES:
                    matchs_montantes.add(s["fixture_id"])
                utilises.add((s["fixture_id"], s["code"]))
                codes_par_match[(s["fixture_id"], s["famille"])] = s["code"]
            coupons.append(coupon)
            journal(f"   ✅ {cat} #{compte[cat]} : {len(coupon['selections'])} match(s), "
                    f"cote {coupon['cote_totale']}")
            progres = True
        if not progres:
            break
    return coupons
