#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
AURA90 - Backtest walk-forward de la calibration inter-ligues
=============================================================

Regle du jeu : pour predire un match europeen du 4 novembre 2024, on ne
s'autorise QUE les donnees anterieures au 4 novembre 2024. Aucune fuite.
Le modele est re-ajuste a chaque journee europeenne (les matchs sont groupes
par fenetres de quelques jours), puis applique aux matchs de cette journee.

Deux versions sont evaluees en parallele sur exactement les memes matchs :
  - AVEC calibration   : niveaux de championnat estimes
  - SANS calibration   : tous les championnats traites comme equivalents
                         (= le comportement actuel de la production)

Ce qu'on regarde, dans l'ordre d'importance :
  1. CALIBRATION   quand le moteur annonce 75 %, est-ce que ca tombe a 75 % ?
                   C'est le critere bloquant. Un moteur sur-confiant qui
                   annonce 78 % et realise 62 % detruirait le palmares.
  2. VOLUME        combien de matchs franchissent les seuils de signature
                   (1X2 >= 70 %, double chance >= 80 %) ?
  3. REUSSITE      taux de reussite reel des pronos signes.

DECISION : on ne deploie que si la calibration est bonne (ecart moyen faible,
pas de sur-confiance systematique). Si le volume augmente mais que la
calibration se degrade, on ne deploie pas. Les seuils ne bougent pas.

Usage :
    python backtest_uefa.py
    python backtest_uefa.py --depuis 2024      # ignorer les saisons anciennes
"""

import argparse
import math
import sys
from collections import defaultdict

import numpy as np
import pandas as pd

import calibration_ligues as C

SEUIL_1X2 = 0.70
SEUIL_DC = 0.80
FENETRE_JOURS = 6          # regroupement des matchs europeens en journees


def resultat(bd, be):
    return "1" if bd > be else ("2" if be > bd else "N")


def journees(eur):
    """Groupe les matchs europeens en journees (fenetres de FENETRE_JOURS)."""
    blocs, courant, debut = [], [], None
    for _, m in eur.iterrows():
        if debut is None or (m["date"] - debut).days > FENETRE_JOURS:
            if courant:
                blocs.append(pd.DataFrame(courant))
            courant, debut = [], m["date"]
        courant.append(m)
    if courant:
        blocs.append(pd.DataFrame(courant))
    return blocs


def evaluer(df, depuis=None):
    eur = df[df["ligue"].isin(C.COMPETITIONS_UEFA)].sort_values("date")
    if depuis:
        eur = eur[eur["date"].dt.year >= depuis]
    blocs = journees(eur)
    print("Journees europeennes a rejouer : %d (%d matchs)"
          % (len(blocs), sum(len(b) for b in blocs)))

    lignes = []
    for n, bloc in enumerate(blocs, 1):
        coupe = bloc["date"].min()
        passe = df[df["date"] < coupe]
        if len(passe[passe["ligue"].isin(C.COMPETITIONS_UEFA)]) < 120:
            continue                                  # pas assez pour estimer
        try:
            avec = C.construire(passe, date_ref=coupe, sans_calibration=False, verbeux=False)
            sans = C.construire(passe, date_ref=coupe, sans_calibration=True, verbeux=False)
        except Exception as exc:                       # journee ininterpretable
            print("  journee %d ignoree (%s)" % (n, exc))
            continue

        for _, m in bloc.iterrows():
            vrai = resultat(m["bd"], m["be"])
            for nom, calib in (("avec", avec), ("sans", sans)):
                p = C.predire(calib, m["dom"], m["ext"])
                if p is None:
                    continue
                issues = {"1": p["1"], "N": p["N"], "2": p["2"]}
                choix = max(issues, key=issues.get)
                dc = {"1X": p["1X"], "X2": p["X2"], "12": p["12"]}
                choix_dc = max(dc, key=dc.get)
                lignes.append({
                    "variante": nom, "journee": n, "date": m["date"],
                    "ligue": m["ligue"], "dom": m["dom"], "ext": m["ext"],
                    "vrai": vrai,
                    "choix": choix, "p_choix": issues[choix],
                    "ok": choix == vrai,
                    "choix_dc": choix_dc, "p_dc": dc[choix_dc],
                    "ok_dc": vrai in choix_dc,
                    "eligible": p["eligible_signature"],
                    "ecart_dom": p["ecart_dom"], "ecart_ext": p["ecart_ext"],
                    "mu": p["mu"], "gamma": p["gamma"], "rho": p["rho"],
                })
        if n % 10 == 0:
            print("  ... journee %d/%d" % (n, len(blocs)))
    return pd.DataFrame(lignes)


def tableau_calibration(r, titre):
    print("\n" + titre)
    print("  proba annoncee | n    | annonce | realise | ecart")
    print("  " + "-" * 52)
    bornes = [(0.30, 0.40), (0.40, 0.50), (0.50, 0.60),
              (0.60, 0.70), (0.70, 0.80), (0.80, 1.01)]
    ecarts, poids = [], []
    for lo, hi in bornes:
        sel = r[(r["p_choix"] >= lo) & (r["p_choix"] < hi)]
        if len(sel) < 5:
            continue
        annonce, realise = sel["p_choix"].mean(), sel["ok"].mean()
        print("  %4.0f-%3.0f %%      | %4d | %6.1f%% | %6.1f%% | %+5.1f pts"
              % (lo * 100, hi * 100, len(sel), annonce * 100,
                 realise * 100, (realise - annonce) * 100))
        ecarts.append(abs(realise - annonce)); poids.append(len(sel))
    if ecarts:
        ece = np.average(ecarts, weights=poids) * 100
        print("  " + "-" * 52)
        print("  ecart de calibration moyen (ECE) : %.1f points" % ece)
        return ece
    return None


def chercher_retrait(r):
    """
    Cherche le facteur de retrait qui rend le moteur le mieux calibre.
    On rejoue les memes matchs en resserrant les ecarts de force, sans rien
    reajuster : seule la traduction force -> probabilite change.
    Critere : ecart de calibration (ECE) minimal.
    """
    print("\n  Recherche du retrait de confiance optimal :")
    print("  retrait | ECE   | signes 1X2 | reussite signes")
    print("  " + "-" * 48)
    meilleur, best_r = 1e9, 1.0
    for retrait in np.arange(0.60, 1.06, 0.05):
        p1, ok = [], []
        for _, m in r.iterrows():
            lh = math.exp(m["mu"] + m["gamma"] + retrait * m["ecart_dom"])
            la = math.exp(m["mu"] + retrait * m["ecart_ext"])
            pr = C.probabilites(lh, la, m["rho"])
            issues = {"1": pr["1"], "N": pr["N"], "2": pr["2"]}
            ch = max(issues, key=issues.get)
            p1.append(issues[ch]); ok.append(ch == m["vrai"])
        d = pd.DataFrame({"p_choix": p1, "ok": ok})
        ecarts, poids = [], []
        for lo, hi in [(0.3,0.4),(0.4,0.5),(0.5,0.6),(0.6,0.7),(0.7,0.8),(0.8,1.01)]:
            sel = d[(d["p_choix"] >= lo) & (d["p_choix"] < hi)]
            if len(sel) < 5:
                continue
            ecarts.append(abs(sel["ok"].mean() - sel["p_choix"].mean()))
            poids.append(len(sel))
        ece = np.average(ecarts, weights=poids) * 100 if ecarts else 99
        sig = d[d["p_choix"] >= SEUIL_1X2]
        print("  %6.2f  | %5.2f | %10d | %6.1f %%"
              % (retrait, ece, len(sig), 100 * sig["ok"].mean() if len(sig) else 0))
        if ece < meilleur:
            meilleur, best_r = ece, float(retrait)
    print("  " + "-" * 48)
    print("  => RETRAIT_CONFIANCE = %.2f  (ECE %.2f points)" % (best_r, meilleur))
    print("     A recopier dans calibration_ligues.py avant deploiement.")
    return best_r


def rapport(res):
    if res.empty:
        print("Aucun match evalue.")
        return
    resume = {}
    for nom in ("avec", "sans"):
        r = res[(res["variante"] == nom) & res["eligible"]]
        if r.empty:
            continue
        print("\n" + "=" * 68)
        print("VARIANTE : %s calibration  (%d matchs eligibles)"
              % (nom.upper(), len(r)))
        print("=" * 68)
        print("  reussite brute 1X2 (tous matchs) : %.1f %%" % (100 * r["ok"].mean()))

        ece = tableau_calibration(r, "  Calibration des probabilites :")

        sig = r[r["p_choix"] >= SEUIL_1X2]
        sigdc = r[r["p_dc"] >= SEUIL_DC]
        print("\n  SIGNES 1X2 (>= %d %%)       : %3d matchs | reussite %.1f %%"
              % (SEUIL_1X2 * 100, len(sig), 100 * sig["ok"].mean() if len(sig) else 0))
        print("  SIGNES double chance (>= %d %%) : %3d matchs | reussite %.1f %%"
              % (SEUIL_DC * 100, len(sigdc), 100 * sigdc["ok_dc"].mean() if len(sigdc) else 0))
        nj = r["journee"].nunique()
        print("  volume : %.1f signes par journee europeenne"
              % ((len(sig) + len(sigdc)) / max(nj, 1)))
        if nom == "avec":
            chercher_retrait(r)
        resume[nom] = {
            "ece": ece, "n_sig": len(sig),
            "reussite_sig": 100 * sig["ok"].mean() if len(sig) else 0,
            "n_dc": len(sigdc),
            "reussite_dc": 100 * sigdc["ok_dc"].mean() if len(sigdc) else 0,
        }

    if "avec" in resume and "sans" in resume:
        a, s = resume["avec"], resume["sans"]
        print("\n" + "=" * 68)
        print("VERDICT")
        print("=" * 68)
        print("  signes 1X2      : %d -> %d" % (s["n_sig"], a["n_sig"]))
        print("  reussite signes : %.1f %% -> %.1f %%" % (s["reussite_sig"], a["reussite_sig"]))
        print("  ECE             : %.1f -> %.1f points"
              % (s["ece"] or 0, a["ece"] or 0))
        ok_calib = (a["ece"] is not None and a["ece"] <= 4.0)
        ok_reussite = a["reussite_sig"] >= 70.0
        ok_volume = a["n_sig"] > s["n_sig"]
        print()
        print("  [%s] calibration sous 4 points d'ecart" % ("OK " if ok_calib else "NON"))
        print("  [%s] reussite des signes >= 70 %% (le seuil promis)"
              % ("OK " if ok_reussite else "NON"))
        print("  [%s] volume de signes en hausse" % ("OK " if ok_volume else "NON"))
        print()
        if ok_calib and ok_reussite and ok_volume:
            print("  => DEPLOIEMENT VALIDE.")
        elif ok_calib and ok_reussite:
            print("  => Calibration saine mais peu de gain de volume.")
            print("     Deployable, sans attendre de miracle sur le nombre de signes.")
        else:
            print("  => NE PAS DEPLOYER en l'etat.")
            print("     Le moteur serait sur-confiant sur les coupes : c'est exactement")
            print("     ce qui casserait le palmares. Les coupes restent peu signees.")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--depuis", type=int, default=None,
                    help="ne tester que les matchs a partir de cette annee")
    ap.add_argument("--csv", default=None, help="chemin du fichier historique")
    args = ap.parse_args()
    if args.csv:
        C.FICHIER_HISTO = args.csv
    df = C.charger(args.csv or C.FICHIER_HISTO)
    res = evaluer(df, depuis=args.depuis)
    res.to_csv("backtest_uefa_resultats.csv", index=False)
    rapport(res)
    print("\nDetail par match : backtest_uefa_resultats.csv")
