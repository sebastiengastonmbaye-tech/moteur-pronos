#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
AURA90 - Calibration inter-ligues
=================================

Objectif : permettre au moteur de prédire correctement les matchs de Ligue des
Champions et de Ligue Europa, où deux equipes issues de championnats differents
s'affrontent.

Le probleme : les forces d'attaque et de defense sont estimees A L'INTERIEUR de
chaque championnat (moyenne normalisee a 1). Une attaque de 1,4 en Eredivisie et
une attaque de 1,4 en Premier League ne valent pas la meme chose. Sans conversion,
le moteur compare deux echelles differentes et sort des probabilites tiedes qui ne
franchissent jamais les seuils de signature (70 % en 1X2, 80 % en double chance).

La solution : un parametre de niveau s_L par championnat, estime sur les vraies
confrontations europeennes de l'historique. Pour un match entre l'equipe i (ligue L)
et l'equipe j (ligue M) :

    lambda_i = exp(mu + gamma + (att_i + s_L) - (def_j + s_M))
    lambda_j = exp(mu      + (att_j + s_M) - (def_i + s_L))

Les equipes issues de championnats non collectes (Ukraine, Tchequie, Slovaquie,
Azerbaidjan, Autriche, Grece...) n'ont aucune note domestique : leurs notes sont
estimees directement sur leurs matchs europeens, avec retrecissement vers un niveau
de pays deduit par regression sur l'indice UEFA.

Sorties : calibration/coefficients.json

Aucune dependance en dehors de numpy et pandas (deja presentes dans le cron).
"""

import json
import math
import os
import sys
import unicodedata
from datetime import datetime

import numpy as np
import pandas as pd

# ---------------------------------------------------------------------------
# CONFIGURATION
# ---------------------------------------------------------------------------

FICHIER_HISTO = os.environ.get("HISTO_CSV", "donnees/histo_api.csv")
DOSSIER_SORTIE = os.environ.get("CALIB_DIR", "calibration")
FICHIER_SORTIE = "coefficients.json"

# Meme parametre de recence que le moteur de production.
XI = 0.0065

# Competitions UEFA sur lesquelles on publie.
COMPETITIONS_UEFA = {2: "Ligue des Champions", 3: "Ligue Europa"}

# Championnats collectes -> pays. Sert au prior de niveau et au diagnostic.
# Les 7 premiers sont les ligues de publication, les suivants sont en LIGUES_SUIVI
# (resultats collectes, aucun prono publie) mais leurs donnees restent exploitables
# pour noter les equipes.
PAYS_PAR_LIGUE = {
    39: "Angleterre", 140: "Espagne", 135: "Italie", 78: "Allemagne",
    61: "France", 88: "Pays-Bas", 94: "Portugal",
    144: "Belgique", 203: "Turquie", 103: "Norvege", 113: "Suede",
    179: "Ecosse", 218: "Autriche", 207: "Suisse", 119: "Danemark", 197: "Grece",
    40: "Angleterre", 141: "Espagne", 136: "Italie", 79: "Allemagne",
    62: "France", 89: "Pays-Bas", 95: "Portugal",
}

# Deuxiemes divisions : elles servent a entrainer le modele domestique du pays
# (via D2_VERS_D1 dans le cron) mais ne sont jamais un niveau europeen a part.
DEUXIEMES_DIVISIONS = {40: 39, 141: 140, 136: 135, 79: 78, 62: 61, 89: 88, 95: 94}

# Indice UEFA des pays (classement 2026). Sert UNIQUEMENT de prior pour les pays
# dont on ne collecte pas le championnat. La valeur absolue n'a pas d'importance :
# le module fait une regression entre log(indice) et le niveau estime des pays
# connus, puis extrapole. A mettre a jour une fois par an.
COEF_PAYS_UEFA = {
    "Angleterre": 94.0, "Italie": 85.0, "Espagne": 80.0, "Allemagne": 78.0,
    "France": 68.0, "Pays-Bas": 62.0, "Portugal": 55.0, "Belgique": 50.0,
    "Turquie": 42.0, "Grece": 38.0, "Tchequie": 36.0, "Autriche": 33.0,
    "Norvege": 32.0, "Suisse": 30.0, "Danemark": 29.0, "Pologne": 28.0,
    "Ecosse": 27.0, "Israel": 25.0, "Ukraine": 24.0, "Croatie": 23.0,
    "Serbie": 22.0, "Chypre": 21.0, "Roumanie": 20.0, "Suede": 19.0,
    "Hongrie": 18.0, "Slovaquie": 16.0, "Bulgarie": 15.0, "Slovenie": 14.0,
    "Azerbaidjan": 13.0, "Kazakhstan": 12.0, "Bosnie": 11.0, "Chypre-Nord": 10.0,
}

# Seuils d'eligibilite a la signature.
MIN_MATCHS_DOMESTIQUES = 6      # aligne sur MIN_MATCHS du cron
MIN_MATCHS_EUROPEENS = 8        # pour une equipe sans championnat collecte

# Penalisations (retrecissement).
# Exprimees en EQUIVALENT-MATCHS : PEN_NIVEAU_LIGUE = 20 signifie que le prior
# (toutes les ligues au meme niveau) pese autant que 20 matchs europeens reels.
PEN_NIVEAU_LIGUE = 20.0
PEN_EQUIPE_EXTERNE = 10.0

# Optimisation.
ITERATIONS = 4000
PAS = 0.05

MAX_BUTS = 10                   # taille de la matrice de scores

# Retrait de confiance : resserre les ecarts de force avant de calculer les
# probabilites. 1.0 = aucun retrait. 0.85 = les ecarts sont reduits de 15 %,
# ce qui rapproche les probabilites du tirage au sort.
# A quoi ca sert : les forces sont estimees, pas connues. Cette incertitude rend
# le moteur SUR-CONFIANT (il annonce 80 %, il realise 70 %). Le retrait corrige
# ce biais SANS toucher aux seuils de signature.
# La valeur juste est celle que sort backtest_uefa.py sur ton historique reel.
RETRAIT_CONFIANCE = 1.0


# ---------------------------------------------------------------------------
# LECTURE DU CSV (detection automatique des colonnes)
# ---------------------------------------------------------------------------

CANDIDATS = {
    "date":   ["date", "date_match", "jour", "match_date"],
    "heure":  ["heure", "time", "kickoff", "horaire"],
    "ligue":  ["ligue_id", "league_id", "id_ligue", "ligue", "league", "competition_id"],
    "saison": ["saison", "season", "annee"],
    "dom":    ["domicile", "equipe_domicile", "home", "home_team", "equipe_dom", "team_home"],
    "ext":    ["exterieur", "equipe_exterieur", "away", "away_team", "equipe_ext", "team_away"],
    "bd":     ["buts_dom", "buts_domicile", "score_dom", "goals_home", "home_goals", "but_dom"],
    "be":     ["buts_ext", "buts_exterieur", "score_ext", "goals_away", "away_goals", "but_ext"],
}


def _normaliser(txt):
    txt = unicodedata.normalize("NFKD", str(txt))
    txt = "".join(c for c in txt if not unicodedata.combining(c))
    return txt.strip().lower().replace(" ", "_")


def detecter_colonnes(df):
    """Associe les colonnes reelles du CSV aux champs logiques attendus."""
    dispo = {_normaliser(c): c for c in df.columns}
    trouve, manquants = {}, []
    for champ, options in CANDIDATS.items():
        for opt in options:
            if opt in dispo:
                trouve[champ] = dispo[opt]
                break
        else:
            if champ != "heure":
                manquants.append(champ)
    if manquants:
        print("\n*** Colonnes introuvables dans le CSV : " + ", ".join(manquants))
        print("*** Colonnes presentes : " + ", ".join(map(str, df.columns)))
        print("*** Ajoute le nom reel dans le dictionnaire CANDIDATS en haut du fichier.")
        sys.exit(1)
    return trouve


def charger(chemin=FICHIER_HISTO):
    if not os.path.exists(chemin):
        print("Fichier introuvable : " + chemin)
        sys.exit(1)
    brut = pd.read_csv(chemin)
    cols = detecter_colonnes(brut)
    print("Colonnes detectees : " + ", ".join(k + "=" + v for k, v in cols.items()))

    df = pd.DataFrame({
        "ligue": pd.to_numeric(brut[cols["ligue"]], errors="coerce"),
        "saison": pd.to_numeric(brut[cols["saison"]], errors="coerce"),
        "dom": brut[cols["dom"]].astype(str).str.strip(),
        "ext": brut[cols["ext"]].astype(str).str.strip(),
        "bd": pd.to_numeric(brut[cols["bd"]], errors="coerce"),
        "be": pd.to_numeric(brut[cols["be"]], errors="coerce"),
    })

    # Horodatage : date seule ou date + heure (voir le bug du 27/08 sur les coupons).
    dates = brut[cols["date"]].astype(str)
    if "heure" in cols:
        dates = dates + " " + brut[cols["heure"]].astype(str).fillna("00:00")
    df["date"] = pd.to_datetime(dates, errors="coerce", format="mixed")

    avant = len(df)
    df = df.dropna(subset=["date", "ligue", "bd", "be"])
    df = df[(df["dom"] != "") & (df["ext"] != "") & (df["dom"] != "nan")]
    df["ligue"] = df["ligue"].astype(int)
    df["bd"] = df["bd"].astype(int)
    df["be"] = df["be"].astype(int)
    print("Matchs joues retenus : %d (sur %d lignes)" % (len(df), avant))
    return df.sort_values("date").reset_index(drop=True)


# ---------------------------------------------------------------------------
# OPTIMISATION (Adam, numpy seul)
# ---------------------------------------------------------------------------

def adam(gradient, x0, iterations=ITERATIONS, pas=PAS):
    x = x0.astype(float).copy()
    m = np.zeros_like(x)
    v = np.zeros_like(x)
    b1, b2, eps = 0.9, 0.999, 1e-8
    for t in range(1, iterations + 1):
        g = gradient(x)
        m = b1 * m + (1 - b1) * g
        v = b2 * v + (1 - b2) * g * g
        mc = m / (1 - b1 ** t)
        vc = v / (1 - b2 ** t)
        x -= pas * mc / (np.sqrt(vc) + eps)
    return x


# ---------------------------------------------------------------------------
# MODELE DOMESTIQUE (un par championnat / pays)
# ---------------------------------------------------------------------------

def ajuster_domestique(matchs, date_ref):
    """
    Ajuste attaque/defense ponderees par la recence sur un ensemble de matchs
    d'un meme pays (D1 + D2 confondues, comme le cron depuis le 25/08).
    Retourne (notes, nb_matchs_par_equipe, mu, gamma).
    notes[equipe] = (attaque, defense) centrees a 0.
    """
    equipes = sorted(set(matchs["dom"]) | set(matchs["ext"]))
    idx = {e: i for i, e in enumerate(equipes)}
    n = len(equipes)
    if n < 4 or len(matchs) < 20:
        return {}, {}, 0.0, 0.0

    ih = matchs["dom"].map(idx).to_numpy()
    ia = matchs["ext"].map(idx).to_numpy()
    bd = matchs["bd"].to_numpy(float)
    be = matchs["be"].to_numpy(float)
    age = (date_ref - matchs["date"]).dt.days.to_numpy(float)
    w = np.exp(-XI * np.clip(age, 0, None))

    # parametres : [mu, gamma, att(n), def(n)]
    def gradient(p):
        mu, gam = p[0], p[1]
        att, dfn = p[2:2 + n], p[2 + n:2 + 2 * n]
        lh = np.exp(mu + gam + att[ih] - dfn[ia])
        la = np.exp(mu + att[ia] - dfn[ih])
        rh = w * (lh - bd)          # d(-loglik)/d(log lambda_h)
        ra = w * (la - be)
        g = np.zeros_like(p)
        g[0] = rh.sum() + ra.sum()
        g[1] = rh.sum()
        np.add.at(g, 2 + ih, rh)
        np.add.at(g, 2 + ia, ra)
        np.add.at(g, 2 + n + ia, -rh)
        np.add.at(g, 2 + n + ih, -ra)
        # centrage doux pour l'identifiabilite (equivalent-matchs)
        g[2:2 + n] += 1.0 * att
        g[2 + n:] += 1.0 * dfn
        return g

    p0 = np.zeros(2 + 2 * n)
    p0[0] = math.log(max(np.average(np.r_[bd, be], weights=np.r_[w, w]), 0.2))
    p0[1] = 0.2
    p = adam(gradient, p0)

    att = p[2:2 + n] - p[2:2 + n].mean()
    dfn = p[2 + n:] - p[2 + n:].mean()
    notes = {e: (float(att[idx[e]]), float(dfn[idx[e]])) for e in equipes}
    compte = matchs["dom"].value_counts().add(matchs["ext"].value_counts(), fill_value=0)
    return notes, compte.to_dict(), float(p[0]), float(p[1])


# ---------------------------------------------------------------------------
# COUCHE EUROPEENNE
# ---------------------------------------------------------------------------

def ajuster_europe(eur, notes_dom, ligue_de_equipe, pays_de_ligue,
                   sans_calibration=False):
    """
    Estime :
      - s_L : le niveau de chaque championnat, sur echelle logarithmique de buts
      - att/def des equipes sans championnat collecte
      - mu, gamma europeens
      - rho (correction Dixon-Coles des petits scores)
    """
    ligues = sorted({ligue_de_equipe[e] for e in
                     set(eur["dom"]) | set(eur["ext"]) if e in ligue_de_equipe})
    idxL = {l: i for i, l in enumerate(ligues)}
    externes = sorted({e for e in set(eur["dom"]) | set(eur["ext"])
                       if e not in notes_dom})
    idxE = {e: i for i, e in enumerate(externes)}
    nL, nE = len(ligues), len(externes)

    def vecteurs(colonne):
        att = np.array([notes_dom[e][0] if e in notes_dom else 0.0 for e in eur[colonne]])
        dfn = np.array([notes_dom[e][1] if e in notes_dom else 0.0 for e in eur[colonne]])
        lig = np.array([idxL.get(ligue_de_equipe.get(e, -1), -1) for e in eur[colonne]])
        ext = np.array([idxE.get(e, -1) for e in eur[colonne]])
        return att, dfn, lig, ext

    aH, dH, lH, eH = vecteurs("dom")
    aA, dA, lA, eA = vecteurs("ext")
    bd = eur["bd"].to_numpy(float)
    be = eur["be"].to_numpy(float)
    date_ref = eur["date"].max()
    age = (date_ref - eur["date"]).dt.days.to_numpy(float)
    w = np.exp(-XI * np.clip(age, 0, None))
    m = len(eur)

    mL_H = lH >= 0
    mL_A = lA >= 0
    mE_H = eH >= 0
    mE_A = eA >= 0

    # prior de niveau pour les equipes externes : rempli apres estimation des s_L
    prior_ext = np.zeros(nE)

    def composer(p):
        mu, gam = p[0], p[1]
        s = p[2:2 + nL]
        ae = p[2 + nL:2 + nL + nE]
        de = p[2 + nL + nE:2 + nL + 2 * nE]
        AH = aH.copy(); DH = dH.copy(); AA = aA.copy(); DA = dA.copy()
        if not sans_calibration:
            AH[mL_H] += s[lH[mL_H]]; DH[mL_H] += s[lH[mL_H]]
            AA[mL_A] += s[lA[mL_A]]; DA[mL_A] += s[lA[mL_A]]
        AH[mE_H] = ae[eH[mE_H]]; DH[mE_H] = de[eH[mE_H]]
        AA[mE_A] = ae[eA[mE_A]]; DA[mE_A] = de[eA[mE_A]]
        lh = np.exp(mu + gam + AH - DA)
        la = np.exp(mu + AA - DH)
        return lh, la, AH, DH, AA, DA

    def gradient(p):
        lh, la, *_ = composer(p)
        rh = w * (lh - bd)
        ra = w * (la - be)
        g = np.zeros_like(p)
        g[0] = rh.sum() + ra.sum()
        g[1] = rh.sum()
        if not sans_calibration:
            # s_L de l'equipe A DOMICILE : + dans lambda_dom (attaque),
            # - dans lambda_ext (sa defense soustrait). D'ou rh - ra.
            np.add.at(g, 2 + lH[mL_H], rh[mL_H] - ra[mL_H])
            # s_L de l'equipe A L'EXTERIEUR : effet symetrique.
            np.add.at(g, 2 + lA[mL_A], ra[mL_A] - rh[mL_A])
            g[2:2 + nL] += PEN_NIVEAU_LIGUE * p[2:2 + nL]
        else:
            g[2:2 + nL] = p[2:2 + nL]
        np.add.at(g, 2 + nL + eH[mE_H], rh[mE_H])
        np.add.at(g, 2 + nL + eA[mE_A], ra[mE_A])
        np.add.at(g, 2 + nL + nE + eA[mE_A], -rh[mE_A])
        np.add.at(g, 2 + nL + nE + eH[mE_H], -ra[mE_H])
        g[2 + nL:2 + nL + nE] += PEN_EQUIPE_EXTERNE * (p[2 + nL:2 + nL + nE] - prior_ext)
        g[2 + nL + nE:] += PEN_EQUIPE_EXTERNE * (p[2 + nL + nE:] - prior_ext)
        return g

    p = np.zeros(2 + nL + 2 * nE)
    p[0] = math.log(max(np.average(np.r_[bd, be], weights=np.r_[w, w]), 0.2))
    p[1] = 0.18
    p = adam(gradient, p, iterations=ITERATIONS // 2)

    # --- prior des externes par regression sur l'indice UEFA ---
    s = p[2:2 + nL]
    xs, ys = [], []
    for l, i in idxL.items():
        pays = pays_de_ligue.get(l)
        c = COEF_PAYS_UEFA.get(pays)
        if c:
            xs.append(math.log(c)); ys.append(s[i])
    if len(xs) >= 4:
        a1, a0 = np.polyfit(np.array(xs), np.array(ys), 1)
    else:
        a1, a0 = 0.0, 0.0
    pays_ext = {}
    for e, i in idxE.items():
        pays = PAYS_EQUIPE_EXTERNE.get(e)
        c = COEF_PAYS_UEFA.get(pays)
        prior_ext[i] = (a0 + a1 * math.log(c)) if c else float(np.median(s)) if nL else 0.0
        pays_ext[e] = pays

    p = adam(gradient, p, iterations=ITERATIONS)
    lh, la, *_ = composer(p)

    # --- rho Dixon-Coles par balayage ---
    meilleur, best_rho = -1e18, 0.0
    for rho in np.arange(-0.20, 0.05, 0.01):
        ll = np.sum(w * np.log(np.clip(
            tau(bd, be, lh, la, rho) *
            np.exp(-lh) * lh ** bd / fact(bd) *
            np.exp(-la) * la ** be / fact(be), 1e-12, None)))
        if ll > meilleur:
            meilleur, best_rho = ll, float(rho)

    return {
        "mu": float(p[0]), "gamma": float(p[1]),
        "niveaux": {str(l): float(p[2 + i]) for l, i in idxL.items()},
        "externes": {e: {"att": float(p[2 + nL + i]),
                         "def": float(p[2 + nL + nE + i]),
                         "pays": pays_ext.get(e)} for e, i in idxE.items()},
        "rho": best_rho,
    }


PAYS_EQUIPE_EXTERNE = {}   # rempli au chargement, voir associer_pays_externes()


_FACT = np.array([math.factorial(i) for i in range(21)], dtype=float)


def fact(k):
    """Factorielle vectorisee sur des petits entiers (buts marques)."""
    return _FACT[np.clip(np.asarray(k, dtype=int), 0, 20)]


def tau(x, y, lh, la, rho):
    t = np.ones_like(lh, dtype=float)
    m00 = (x == 0) & (y == 0); m01 = (x == 0) & (y == 1)
    m10 = (x == 1) & (y == 0); m11 = (x == 1) & (y == 1)
    t[m00] = 1 - lh[m00] * la[m00] * rho
    t[m01] = 1 + lh[m01] * rho
    t[m10] = 1 + la[m10] * rho
    t[m11] = 1 - rho
    return np.clip(t, 1e-6, None)


# ---------------------------------------------------------------------------
# PREDICTION
# ---------------------------------------------------------------------------

def probabilites(lh, la, rho):
    """Matrice de scores 0..MAX_BUTS avec correction Dixon-Coles."""
    k = np.arange(MAX_BUTS + 1)
    logf = np.cumsum(np.r_[0.0, np.log(np.arange(1, MAX_BUTS + 1))])
    ph = np.exp(-lh + k * math.log(lh) - logf)
    pa = np.exp(-la + k * math.log(la) - logf)
    M = np.outer(ph, pa)
    M[0, 0] *= 1 - lh * la * rho
    M[0, 1] *= 1 + lh * rho
    M[1, 0] *= 1 + la * rho
    M[1, 1] *= 1 - rho
    M = np.clip(M, 0, None)
    M /= M.sum()
    idx = np.arange(MAX_BUTS + 1)
    dom = M[np.greater.outer(idx, idx)].sum()
    nul = np.trace(M)
    ext = 1 - dom - nul
    total = np.add.outer(idx, idx)
    return {
        "1": float(dom), "N": float(nul), "2": float(ext),
        "1X": float(dom + nul), "X2": float(nul + ext), "12": float(dom + ext),
        "O25": float(M[total >= 3].sum()), "U25": float(M[total <= 2].sum()),
        "BTTS": float(M[1:, 1:].sum()),
        "scores_top3": [
            {"score": "%d-%d" % (i, j), "p": float(M[i, j])}
            for i, j in sorted(np.ndindex(M.shape), key=lambda c: -M[c])[:3]
        ],
    }


def predire(calib, equipe_dom, equipe_ext):
    """Retourne les probabilites d'un match europeen, ou None si equipe inconnue."""
    def note(e):
        if e in calib["notes_domestiques"]:
            a, d = calib["notes_domestiques"][e]
            s = calib["europe"]["niveaux"].get(str(calib["ligue_equipe"].get(e)), 0.0)
            n = calib["matchs_domestiques"].get(e, 0)
            return a + s, d + s, n >= MIN_MATCHS_DOMESTIQUES
        if e in calib["europe"]["externes"]:
            x = calib["europe"]["externes"][e]
            n = calib["matchs_europeens"].get(e, 0)
            return x["att"], x["def"], n >= MIN_MATCHS_EUROPEENS
        return None
    nd, ne = note(equipe_dom), note(equipe_ext)
    if nd is None or ne is None:
        return None
    mu, gam, rho = calib["europe"]["mu"], calib["europe"]["gamma"], calib["europe"]["rho"]
    r = calib.get("retrait", RETRAIT_CONFIANCE)
    ecart_dom, ecart_ext = nd[0] - ne[1], ne[0] - nd[1]
    lh = math.exp(mu + gam + r * ecart_dom)
    la = math.exp(mu + r * ecart_ext)
    p = probabilites(lh, la, rho)
    p["lambda_dom"], p["lambda_ext"] = lh, la
    # composantes brutes : permettent de retester un autre retrait sans reajuster
    p["ecart_dom"], p["ecart_ext"] = ecart_dom, ecart_ext
    p["mu"], p["gamma"], p["rho"] = mu, gam, rho
    p["eligible_signature"] = bool(nd[2] and ne[2])
    return p


# ---------------------------------------------------------------------------
# CONSTRUCTION COMPLETE
# ---------------------------------------------------------------------------

def associer_pays_externes(eur, notes_dom, indice_pays_equipe):
    """
    Devine le pays des equipes sans championnat collecte a partir de leurs
    adversaires ? Non : sans information fiable on laisse None, le prior tombe
    alors sur le niveau median. Un fichier optionnel calibration/pays_clubs.json
    permet de renseigner les clubs a la main (recommande pour Shakhtar, Slavia,
    Slovan, Sabah, LASK, AEK...).
    """
    chemin = os.path.join(DOSSIER_SORTIE, "pays_clubs.json")
    if os.path.exists(chemin):
        with open(chemin, encoding="utf-8") as f:
            PAYS_EQUIPE_EXTERNE.update(json.load(f))
        print("Pays renseignes a la main : %d clubs" % len(PAYS_EQUIPE_EXTERNE))
    else:
        print("(pas de calibration/pays_clubs.json : les externes utiliseront "
              "le niveau median comme prior)")


def construire(df, date_ref=None, sans_calibration=False, verbeux=True):
    date_ref = date_ref or df["date"].max()
    dom_df = df[~df["ligue"].isin(COMPETITIONS_UEFA)]
    eur = df[df["ligue"].isin(COMPETITIONS_UEFA)]

    # regroupement par pays : D1 + D2 du meme pays s'entrainent ensemble
    dom_df = dom_df.copy()
    dom_df["ligue_ref"] = dom_df["ligue"].map(lambda l: DEUXIEMES_DIVISIONS.get(l, l))

    notes_dom, matchs_dom, ligue_equipe = {}, {}, {}
    for lref, bloc in dom_df.groupby("ligue_ref"):
        notes, compte, mu, gam = ajuster_domestique(bloc, date_ref)
        if not notes:
            continue
        # seules les equipes de D1 recoivent une identite de ligue europeenne
        d1 = bloc[bloc["ligue"] == lref]
        equipes_d1 = set(d1["dom"]) | set(d1["ext"])
        for e, v in notes.items():
            notes_dom[e] = v
            matchs_dom[e] = int(compte.get(e, 0))
            if e in equipes_d1:
                ligue_equipe[e] = int(lref)
        if verbeux:
            print("  ligue %-4s : %4d matchs, %3d equipes" % (lref, len(bloc), len(notes)))

    # on ne garde que les equipes rattachees a une D1 pour la couche europeenne
    notes_utiles = {e: v for e, v in notes_dom.items() if e in ligue_equipe}

    associer_pays_externes(eur, notes_utiles, ligue_equipe)
    europe = ajuster_europe(eur, notes_utiles, ligue_equipe, PAYS_PAR_LIGUE,
                            sans_calibration=sans_calibration)

    compte_eur = eur["dom"].value_counts().add(eur["ext"].value_counts(), fill_value=0)
    return {
        "date_reference": str(date_ref),
        "notes_domestiques": notes_utiles,
        "matchs_domestiques": {e: matchs_dom[e] for e in notes_utiles},
        "matchs_europeens": {k: int(v) for k, v in compte_eur.items()},
        "ligue_equipe": ligue_equipe,
        "retrait": RETRAIT_CONFIANCE,
        "europe": europe,
    }


def main():
    print("=" * 68)
    print("AURA90 - calibration inter-ligues")
    print("=" * 68)
    df = charger()
    eur = df[df["ligue"].isin(COMPETITIONS_UEFA)]
    print("Matchs europeens : %d (C1 %d / C2 %d)" % (
        len(eur), (eur["ligue"] == 2).sum(), (eur["ligue"] == 3).sum()))
    print("\nAjustement des modeles domestiques :")
    calib = construire(df)

    os.makedirs(DOSSIER_SORTIE, exist_ok=True)
    chemin = os.path.join(DOSSIER_SORTIE, FICHIER_SORTIE)
    with open(chemin, "w", encoding="utf-8") as f:
        json.dump(calib, f, ensure_ascii=False, indent=1)

    print("\nNIVEAUX DE CHAMPIONNAT (echelle log, 0 = moyenne europeenne)")
    print("-" * 68)
    niveaux = sorted(calib["europe"]["niveaux"].items(),
                     key=lambda kv: -kv[1])
    for l, s in niveaux:
        pays = PAYS_PAR_LIGUE.get(int(l), "?")
        # lecture concrete : ecart de buts attendus contre une equipe moyenne
        print("  %-12s (ligue %-3s) : %+6.3f   soit x%.2f en buts attendus"
              % (pays, l, s, math.exp(s)))
    print("-" * 68)
    print("rho Dixon-Coles europeen : %+.3f" % calib["europe"]["rho"])
    print("Equipes sans championnat collecte : %d" % len(calib["europe"]["externes"]))
    peu = [e for e in calib["europe"]["externes"]
           if calib["matchs_europeens"].get(e, 0) < MIN_MATCHS_EUROPEENS]
    print("  dont %d avec moins de %d matchs europeens -> NON eligibles a la signature"
          % (len(peu), MIN_MATCHS_EUROPEENS))
    print("\nEcrit : " + chemin)


if __name__ == "__main__":
    main()
