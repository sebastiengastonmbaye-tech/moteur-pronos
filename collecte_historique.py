#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
COLLECTE HISTORIQUE — Backtest du moteur de pronostics
=======================================================
Télécharge 10 saisons des grands championnats depuis football-data.co.uk
(matchs + scores + cotes de clôture) et produit UN fichier propre :
    donnees/matchs_historique.csv

Usage :
    pip install pandas requests
    python collecte_historique.py

Aucune clé API nécessaire. ~100 fichiers CSV téléchargés (rapide).
"""

import os
import time
import requests
import pandas as pd

# ---------------------------------------------------------------
# CONFIGURATION
# ---------------------------------------------------------------

# Codes football-data.co.uk des grands championnats
LIGUES = {
    "E0":  "Premier League",
    "SP1": "La Liga",
    "I1":  "Serie A",
    "D1":  "Bundesliga",
    "F1":  "Ligue 1",
    "N1":  "Eredivisie",
    "P1":  "Liga Portugal",
    "B1":  "Jupiler Pro League",
    "T1":  "Süper Lig",
    "SC0": "Scottish Premiership",
}

# Saisons : "1516" = 2015/2016 ... "2526" = 2025/2026
SAISONS = ["1516", "1617", "1718", "1819", "1920",
           "2021", "2122", "2223", "2324", "2425", "2526"]

URL = "https://www.football-data.co.uk/mmz4281/{saison}/{ligue}.csv"

# Colonnes qu'on garde (si présentes dans le fichier)
COLONNES = {
    "Date": "date",
    "Time": "heure",
    "HomeTeam": "equipe_dom",
    "AwayTeam": "equipe_ext",
    "FTHG": "buts_dom",          # buts domicile temps réglementaire
    "FTAG": "buts_ext",
    "HTHG": "mt_buts_dom",       # score mi-temps
    "HTAG": "mt_buts_ext",
    "HS": "tirs_dom",  "AS": "tirs_ext",
    "HST": "tirs_cadres_dom", "AST": "tirs_cadres_ext",
    "HC": "corners_dom", "AC": "corners_ext",
    # Cotes de clôture (moyenne du marché + Bet365 + Pinnacle)
    "AvgH": "cote_moy_1", "AvgD": "cote_moy_N", "AvgA": "cote_moy_2",
    "B365H": "cote_b365_1", "B365D": "cote_b365_N", "B365A": "cote_b365_2",
    "PSH": "cote_pin_1",  "PSD": "cote_pin_N",  "PSA": "cote_pin_2",
    # Over/Under 2.5
    "Avg>2.5": "cote_over25", "Avg<2.5": "cote_under25",
    "B365>2.5": "cote_b365_over25", "B365<2.5": "cote_b365_under25",
}


def telecharger(ligue: str, saison: str) -> pd.DataFrame | None:
    """Télécharge un CSV saison/ligue, retourne un DataFrame nettoyé ou None."""
    url = URL.format(saison=saison, ligue=ligue)
    try:
        r = requests.get(url, timeout=30)
        if r.status_code != 200 or len(r.content) < 500:
            return None
        from io import StringIO
        # encodage parfois latin-1 sur les vieux fichiers
        try:
            df = pd.read_csv(StringIO(r.content.decode("utf-8")),
                             on_bad_lines="skip")
        except UnicodeDecodeError:
            df = pd.read_csv(StringIO(r.content.decode("latin-1")),
                             on_bad_lines="skip")
    except Exception as e:
        print(f"   ⚠️  {ligue} {saison} : {e}")
        return None

    presentes = {c: n for c, n in COLONNES.items() if c in df.columns}
    df = df[list(presentes.keys())].rename(columns=presentes)

    # métadonnées
    df["ligue"] = ligue
    df["ligue_nom"] = LIGUES[ligue]
    df["saison"] = f"20{saison[:2]}/20{saison[2:]}"

    # nettoyage minimal
    df = df.dropna(subset=["equipe_dom", "equipe_ext", "buts_dom", "buts_ext"])
    df["date"] = pd.to_datetime(df["date"], dayfirst=True, errors="coerce")
    df = df.dropna(subset=["date"])
    df["buts_dom"] = df["buts_dom"].astype(int)
    df["buts_ext"] = df["buts_ext"].astype(int)
    return df


def main():
    os.makedirs("donnees", exist_ok=True)
    morceaux = []
    total = len(LIGUES) * len(SAISONS)
    fait = 0

    for saison in SAISONS:
        for ligue in LIGUES:
            fait += 1
            df = telecharger(ligue, saison)
            if df is not None and len(df) > 0:
                morceaux.append(df)
                print(f"[{fait:>3}/{total}] ✅ {LIGUES[ligue]:<22} {df['saison'].iloc[0]} : {len(df)} matchs")
            else:
                print(f"[{fait:>3}/{total}] ❌ {LIGUES[ligue]:<22} {saison} : indisponible")
            time.sleep(0.3)  # politesse envers le serveur

    if not morceaux:
        print("\n❌ Aucune donnée récupérée. Vérifie ta connexion.")
        return

    tout = pd.concat(morceaux, ignore_index=True).sort_values("date")
    chemin = "donnees/matchs_historique.csv"
    tout.to_csv(chemin, index=False)

    # ------- RAPPORT -------
    print("\n" + "=" * 60)
    print(f"✅ TERMINÉ : {len(tout):,} matchs → {chemin}")
    print(f"   Période : {tout['date'].min().date()} → {tout['date'].max().date()}")
    print(f"   Ligues  : {tout['ligue_nom'].nunique()}")
    avec_cotes = tout["cote_moy_1"].notna().sum() if "cote_moy_1" in tout else 0
    print(f"   Matchs avec cotes 1N2 : {avec_cotes:,} "
          f"({100*avec_cotes/len(tout):.0f} %)")
    if "cote_over25" in tout:
        ou = tout["cote_over25"].notna().sum()
        print(f"   Matchs avec cotes O/U 2.5 : {ou:,} ({100*ou/len(tout):.0f} %)")
    print("=" * 60)
    print("\n➡️  Envoie-moi ce fichier (ou juste le rapport ci-dessus)")
    print("   et je construis le moteur Dixon-Coles + le backtest.")


if __name__ == "__main__":
    main()
