#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
CRON QUOTIDIEN — Pont API-Football + publication des pronos
===========================================================
V3 : capture des logos d'équipes (URL API-Football) et de l'heure
de coup d'envoi, pour affichage dans l'app.

Tourne chaque nuit via GitHub Actions. Trois étapes :
  1. IMPORT     : rafraîchit l'historique des matchs (cache local)
  2. VÉRIFIE    : va chercher les résultats des pronos déjà publiés
  3. PUBLIE     : entraîne le moteur et publie les pronos des 7 prochains jours

Les pronos sont APPEND-ONLY dans donnees/pronos_publies.csv.
Clé API : variable d'environnement API_FOOTBALL_KEY (GitHub Secret).
"""
import os
import sys
import time
import requests
import numpy as np
import pandas as pd
from datetime import datetime, timedelta, timezone

from moteur_production import Moteur

# ------------------------------------------------------------------
CLE = os.environ.get("API_FOOTBALL_KEY")
if not CLE:
    sys.exit("❌ API_FOOTBALL_KEY absente (à définir dans les GitHub Secrets)")

BASE = "https://v3.football.api-sports.io"
HEADERS = {"x-apisports-key": CLE}

# Compétitions sur lesquelles le moteur PUBLIE des pronos
LIGUES = {
    # --- Top 7 championnats européens (saison août → mai) ---
    39:  "Premier League",
    140: "La Liga",
    135: "Serie A",
    78:  "Bundesliga",
    61:  "Ligue 1",
    88:  "Eredivisie",
    94:  "Liga Portugal",
    # --- Coupes d'Europe UEFA ---
    2:   "Ligue des Champions",
    3:   "Ligue Europa",
}

# Deuxièmes divisions : leurs matchs nourrissent le modèle du championnat
# correspondant (les équipes promues/reléguées font le pont entre les deux
# niveaux, ce qui laisse le moteur calibrer seul l'écart de niveau).
# Aucun prono n'est publié sur ces divisions.
D2_VERS_D1 = {
    40:  39,   # Championship        → Premier League
    141: 140,  # Segunda División    → La Liga
    136: 135,  # Serie B             → Serie A
    79:  78,   # 2. Bundesliga       → Bundesliga
    62:  61,   # Ligue 2             → Ligue 1
    89:  88,   # Eerste Divisie      → Eredivisie
    95:  94,   # Liga Portugal 2     → Liga Portugal
}
NOMS_D2 = {
    40: "Championship", 141: "Segunda División", 136: "Serie B",
    79: "2. Bundesliga", 62: "Ligue 2", 89: "Eerste Divisie",
    95: "Liga Portugal 2",
}

# Championnats collectés UNIQUEMENT pour alimenter les coupons
# (aucun prono publié dessus dans la liste principale de l'app)
LIGUES_COUPONS = {
    144: "Jupiler Pro League",     # Belgique
    203: "Süper Lig",              # Turquie
    179: "Scottish Premiership",   # Écosse
    218: "Österreich Bundesliga",  # Autriche
    207: "Super League Suisse",    # Suisse
    119: "Superliga",              # Danemark
    197: "Super League Grèce",     # Grèce
}

# Compétitions dont on continue de RÉCUPÉRER les résultats, uniquement pour
# vérifier les pronos déjà publiés (engagement : tout prono publié est vérifié,
# gagné ou perdu). Aucune nouvelle prédiction n'y est faite.
LIGUES_SUIVI = {
    71:  "Brésil Série A",
    128: "Argentine Liga Profesional",
    253: "MLS",
    103: "Norvège Eliteserien",
    113: "Suède Allsvenskan",
    98:  "Japon J1 League",
}

SAISONS_HISTO = [2023, 2024, 2025]
SAISON_COURANTE = 2026
JOURS_A_PREDIRE = 7

F_HISTO  = "donnees/histo_api.csv"
F_PRONOS = "donnees/pronos_publies.csv"


def appel(endpoint, params):
    for essai in range(3):
        try:
            r = requests.get(f"{BASE}/{endpoint}", headers=HEADERS,
                             params=params, timeout=30)
            if r.status_code == 429:
                print("   ⏳ quota atteint, pause 60 s")
                time.sleep(60); continue
            r.raise_for_status()
            j = r.json()
            if j.get("errors"):
                print(f"   ⚠️ API: {j['errors']}")
                return []
            return j.get("response", [])
        except Exception as e:
            print(f"   ⚠️ {endpoint} {params} : {e}")
            time.sleep(3)
    return []


def plat(f):
    return {
        "fixture_id": f["fixture"]["id"],
        "date": f["fixture"]["date"][:10],
        "heure": f["fixture"]["date"][11:16],
        "statut": f["fixture"]["status"]["short"],
        "ligue_id": f["league"]["id"],
        "ligue_nom": f["league"]["name"],
        "saison": f["league"]["season"],
        "equipe_dom": f["teams"]["home"]["name"],
        "equipe_ext": f["teams"]["away"]["name"],
        "logo_dom": f["teams"]["home"].get("logo"),
        "logo_ext": f["teams"]["away"].get("logo"),
        "buts_dom": f["goals"]["home"],
        "buts_ext": f["goals"]["away"],
    }


# ==================================================================
# 1. IMPORT DE L'HISTORIQUE
# ==================================================================
def importer_historique():
    os.makedirs("donnees", exist_ok=True)
    if os.path.exists(F_HISTO):
        histo = pd.read_csv(F_HISTO)
        deja = set(zip(histo.ligue_id, histo.saison))
    else:
        histo, deja = pd.DataFrame(), set()

    nouveaux = []
    a_historiser = {**LIGUES, **NOMS_D2, **LIGUES_COUPONS}
    for lid in a_historiser:
        for saison in SAISONS_HISTO:
            if (lid, saison) in deja:
                continue
            print(f"   ↓ {a_historiser[lid]} {saison}")
            rep = appel("fixtures", {"league": lid, "season": saison})
            nouveaux += [plat(f) for f in rep]
            time.sleep(1)

    toutes = {**LIGUES, **NOMS_D2, **LIGUES_COUPONS, **LIGUES_SUIVI}
    for lid in toutes:
        print(f"   ↻ {toutes[lid]} {SAISON_COURANTE}")
        rep = appel("fixtures", {"league": lid, "season": SAISON_COURANTE})
        nouveaux += [plat(f) for f in rep]
        time.sleep(1)

    if nouveaux:
        neuf = pd.DataFrame(nouveaux)
        histo = pd.concat([histo, neuf], ignore_index=True)
        histo = histo.drop_duplicates(subset="fixture_id", keep="last")
        histo.to_csv(F_HISTO, index=False)
    print(f"   ✅ historique : {len(histo):,} matchs")
    return histo


# ==================================================================
# 2. VÉRIFICATION DES PRONOS PASSÉS
# ==================================================================
def verifier(histo):
    """Ajoute le résultat réel aux pronos dont le match est terminé.
    N'écrit QUE dans les colonnes de résultat : le prono reste intact."""
    if not os.path.exists(F_PRONOS):
        print("   (aucun prono à vérifier)")
        return
    p = pd.read_csv(F_PRONOS)
    for c in ("score_reel", "prono_gagne", "buts_gagne"):
        p[c] = p[c].astype("object")
    fini = histo[histo.statut == "FT"].set_index("fixture_id")
    n = 0
    for i, row in p[p.verifie != True].iterrows():
        if row.fixture_id not in fini.index:
            continue
        m = fini.loc[row.fixture_id]
        if isinstance(m, pd.DataFrame):
            m = m.iloc[0]
        bd, be = int(m.buts_dom), int(m.buts_ext)
        res = "1" if bd > be else ("N" if bd == be else "2")
        over = (bd + be) >= 3

        gagne = {
            "1":  res == "1", "2": res == "2", "N": res == "N",
            "1X": res != "2", "X2": res != "1",
            "O2.5": over, "U2.5": not over,
        }.get(row.code_principal)

        p.at[i, "score_reel"] = f"{bd}-{be}"
        p.at[i, "prono_gagne"] = bool(gagne)
        p.at[i, "buts_gagne"] = bool({"O2.5": over, "U2.5": not over}.get(row.code_buts))
        p.at[i, "verifie"] = True
        n += 1
    p.to_csv(F_PRONOS, index=False)
    print(f"   ✅ {n} pronos vérifiés")



# ==================================================================
# 2bis. RATTRAPAGE DES MÉTADONNÉES (logos, heure) sur les pronos déjà publiés
# ==================================================================
def completer_metadonnees(histo):
    """Complète logos et heure manquants des pronos existants, depuis
    l'historique. Ne touche JAMAIS aux pronos eux-mêmes."""
    if not os.path.exists(F_PRONOS):
        return
    p = pd.read_csv(F_PRONOS)
    for col in ("logo_dom", "logo_ext", "heure"):
        if col not in p.columns:
            p[col] = None
        p[col] = p[col].astype("object")
    ref = histo.drop_duplicates("fixture_id").set_index("fixture_id")
    n = 0
    for i, row in p.iterrows():
        if row.fixture_id not in ref.index:
            continue
        m = ref.loc[row.fixture_id]
        for col in ("logo_dom", "logo_ext", "heure"):
            if (pd.isna(row.get(col)) or row.get(col) in (None, "")) and pd.notna(m.get(col)):
                p.at[i, col] = m[col]
                n += 1
    if n:
        p.to_csv(F_PRONOS, index=False)
    print(f"   ✅ métadonnées complétées : {n} champs (logos/heure)")

# ==================================================================
# 3. PUBLICATION DES NOUVEAUX PRONOS
# ==================================================================
def publier(histo):
    histo["date"] = pd.to_datetime(histo["date"])
    aujourdhui = pd.Timestamp(datetime.now(timezone.utc).date())
    fin = aujourdhui + pd.Timedelta(days=JOURS_A_PREDIRE)

    deja = set()
    if os.path.exists(F_PRONOS):
        deja = set(pd.read_csv(F_PRONOS).fixture_id)

    lignes = []
    ecartes = []          # journal des matchs non publiés
    for lid, nom in LIGUES.items():
        # le vivier d'entraînement inclut la 2e division du même pays :
        # les équipes promues y ont leur historique, et les équipes qui
        # font l'aller-retour calibrent l'écart de niveau entre divisions
        viviers = [lid] + [d2 for d2, d1 in D2_VERS_D1.items() if d1 == lid]
        passe = histo[histo.ligue_id.isin(viviers) & (histo.statut == "FT")].dropna(
            subset=["buts_dom", "buts_ext"])
        avenir = histo[(histo.ligue_id == lid) & (histo.statut == "NS") &
                       (histo.date >= aujourdhui) & (histo.date < fin)]
        if len(passe) < 120 or avenir.empty:
            if not avenir.empty:
                ecartes.append((nom, len(avenir), "historique insuffisant pour la ligue"))
            continue
        try:
            m = Moteur(passe, date_ref=aujourdhui)
        except ValueError as e:
            print(f"   ⚠️ {nom} : {e}")
            continue

        for _, f in avenir.iterrows():
            if f.fixture_id in deja:
                continue
            fiche = m.analyser(f.equipe_dom, f.equipe_ext)
            if "erreur" in fiche:
                ecartes.append((nom, f"{f.equipe_dom} – {f.equipe_ext}", fiche["erreur"]))
                continue
            lignes.append({
                "publie_le": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                "fixture_id": f.fixture_id,
                "date_match": f.date.date(),
                "heure": f.get("heure"),
                "ligue": nom,
                "dom": f.equipe_dom, "ext": f.equipe_ext,
                "logo_dom": f.get("logo_dom"), "logo_ext": f.get("logo_ext"),
                "prono_principal": fiche["prono_principal"]["selection"],
                "code_principal": fiche["prono_principal"]["code"],
                "badge": fiche["prono_principal"]["niveau"],
                "confiance": fiche["prono_principal"]["confiance"],
                "prono_buts": fiche["prono_buts"]["selection"],
                "code_buts": fiche["prono_buts"]["code"],
                "confiance_buts": fiche["prono_buts"]["confiance"],
                "scores_top3": " · ".join(s["score"] for s in fiche["scores_probables"]),
                "btts": fiche["bonus"]["btts_oui"],
                "p1": fiche["_probas"]["1"], "pN": fiche["_probas"]["N"],
                "p2": fiche["_probas"]["2"], "pO25": fiche["_probas"]["O2.5"],
                "score_reel": None, "prono_gagne": None,
                "buts_gagne": None, "verifie": False,
            })

    if ecartes:
        print(f"   ⚠️ {len(ecartes)} match(s) non publiés :")
        for ligue, quoi, motif in ecartes[:40]:
            print(f"      · {ligue} — {quoi} : {motif}")

    if not lignes:
        print("   (aucun nouveau prono)")
        return
    neuf = pd.DataFrame(lignes)
    if os.path.exists(F_PRONOS):
        neuf = pd.concat([pd.read_csv(F_PRONOS), neuf], ignore_index=True)
    neuf.to_csv(F_PRONOS, index=False)
    print(f"   ✅ {len(lignes)} nouveaux pronos publiés")
    recap = pd.DataFrame(lignes).groupby("ligue").size().sort_values(ascending=False)
    for ligue, n in recap.items():
        print(f"      · {ligue} : {n}")


# ==================================================================
def palmares():
    if not os.path.exists(F_PRONOS):
        return
    p = pd.read_csv(F_PRONOS)
    v = p[p.verifie == True]
    if v.empty:
        print("\n📊 Palmarès : aucun prono encore vérifié")
        return
    print(f"\n📊 PALMARÈS — {len(v):,} pronos vérifiés")
    print(f"   Réussite globale : {v.prono_gagne.mean()*100:.1f} % "
          f"(confiance annoncée : {v.confiance.mean():.1f} %)")
    for b in ["Très sûr", "Sûr", "Équilibré", "Audacieux"]:
        s = v[v.badge == b]
        if len(s):
            print(f"   {b:<10} {s.prono_gagne.mean()*100:5.1f} %  ({len(s)} pronos)")


if __name__ == "__main__":
    print("1️⃣  IMPORT");   histo = importer_historique()
    print("2️⃣  VÉRIFICATION"); verifier(histo); completer_metadonnees(histo)
    print("3️⃣  PUBLICATION");  publier(histo)
    palmares()
