#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
SYNC SUPABASE V2 — pousse pronos_publies.csv vers public.pronos
(ajout : logos d'équipes + heure de coup d'envoi)
Variables d'environnement : SUPABASE_URL, SUPABASE_SERVICE_KEY
"""
import os
import sys
import math
import requests
import pandas as pd

URL = os.environ.get("SUPABASE_URL", "").rstrip("/")
KEY = os.environ.get("SUPABASE_SERVICE_KEY", "")
if not URL or not KEY:
    sys.exit("❌ SUPABASE_URL / SUPABASE_SERVICE_KEY manquantes (GitHub Secrets)")

F_PRONOS = "donnees/pronos_publies.csv"
ENTETES = {
    "apikey": KEY,
    "Authorization": f"Bearer {KEY}",
    "Content-Type": "application/json",
    "Prefer": "resolution=merge-duplicates",
}

def nettoie(v):
    if v is None:
        return None
    if isinstance(v, float) and math.isnan(v):
        return None
    if hasattr(v, "item"):
        return v.item()
    return v

def main():
    if not os.path.exists(F_PRONOS):
        print("(pas de fichier pronos — rien à synchroniser)")
        return

    df = pd.read_csv(F_PRONOS)
    lignes = []
    for _, r in df.iterrows():
        lignes.append({
            "publie_le":       r["publie_le"],
            "fixture_id":      int(r["fixture_id"]),
            "date_match":      str(r["date_match"]),
            "heure":           nettoie(r.get("heure")),
            "ligue":           r["ligue"],
            "dom":             r["dom"],
            "ext":             r["ext"],
            "logo_dom":        nettoie(r.get("logo_dom")),
            "logo_ext":        nettoie(r.get("logo_ext")),
            "prono_principal": r["prono_principal"],
            "code_principal":  r["code_principal"],
            "badge":           r["badge"],
            "confiance":       nettoie(r["confiance"]),
            "prono_buts":      r["prono_buts"],
            "code_buts":       r["code_buts"],
            "confiance_buts":  nettoie(r["confiance_buts"]),
            "scores_top3":     r["scores_top3"],
            "btts":            nettoie(r.get("btts")),
            "p1":  nettoie(r.get("p1")),  "pn":   nettoie(r.get("pN")),
            "p2":  nettoie(r.get("p2")),  "po25": nettoie(r.get("pO25")),
            "verifie":     bool(r["verifie"]) if pd.notna(r["verifie"]) else False,
            "score_reel":  nettoie(r.get("score_reel")),
            "prono_gagne": nettoie(r.get("prono_gagne")),
            "buts_gagne":  nettoie(r.get("buts_gagne")),
        })

    total = 0
    for i in range(0, len(lignes), 200):
        paquet = lignes[i:i+200]
        rep = requests.post(
            f"{URL}/rest/v1/pronos?on_conflict=fixture_id",
            headers=ENTETES, json=paquet, timeout=60)
        if rep.status_code not in (200, 201, 204):
            print(f"⚠️ paquet {i//200+1} refusé ({rep.status_code}) : {rep.text[:300]}")
        else:
            total += len(paquet)
    print(f"✅ Supabase synchronisé : {total}/{len(lignes)} lignes upsertées")

if __name__ == "__main__":
    main()
