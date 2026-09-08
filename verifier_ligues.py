#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Confirme à quoi correspondent des identifiants de ligue API-Football.
Usage : python verifier_ligues.py            -> vérifie LIGUES_EUROPE
        python verifier_ligues.py 332 333    -> vérifie ces identifiants
Nécessite API_FOOTBALL_KEY dans l'environnement (comme le cron)."""
import os, sys, requests

CLE = os.environ.get("API_FOOTBALL_KEY") or sys.exit("API_FOOTBALL_KEY absente")
ids = [int(x) for x in sys.argv[1:]]
if not ids:
    import re
    src = open("cron_quotidien.py", encoding="utf-8").read()
    bloc = src.split("LIGUES_EUROPE = {")[1].split("}")[0]
    ids = [int(x) for x in re.findall(r"^\s*(\d+):", bloc, re.M)]

for i in ids:
    r = requests.get("https://v3.football.api-sports.io/leagues",
                     headers={"x-apisports-key": CLE}, params={"id": i}, timeout=30).json()
    rep = r.get("response") or []
    if rep:
        l = rep[0]
        print(f"  {i:>4} → {l['country']['name']:<14} {l['league']['name']}")
    else:
        print(f"  {i:>4} → ❌ identifiant inconnu")
