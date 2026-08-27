#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
AURA90 — GÉNÉRATEUR DE COUPONS
Tourne dans le cron, après cron_quotidien.py.

1. Vérifie les coupons de la veille (résultats réels)
2. Construit les coupons du jour : Sûre · Confiance · Fun · Grosses cotes · Montante
3. Fait avancer la montante en cours (objectif ×15, départ 5 000 F)

Règles : une seule sélection par match · pas deux fois la même sélection
dans deux coupons différents · priorité V1/V2 puis buts puis double chance.

Variables d'environnement : API_FOOTBALL_KEY, SUPABASE_URL, SUPABASE_SERVICE_KEY
"""
import os
import sys
import time
import json
from datetime import datetime, timedelta, timezone

import pandas as pd
import requests

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from moteur_production import Moteur          # noqa: E402

CLE_API = os.environ.get("API_FOOTBALL_KEY", "")
URL_SB = os.environ.get("SUPABASE_URL", "").rstrip("/")
CLE_SB = os.environ.get("SUPABASE_SERVICE_KEY", "")
BASE_API = "https://v3.football.api-sports.io"

F_HISTO = "donnees/histo_api.csv"
SAISON = 2026
FENETRE_H = 36            # matchs des 36 prochaines heures
MISE_DEPART = 5000        # montante : mise de départ en F CFA
OBJECTIF_MONTANTE = 15    # ×15 puis on repart à zéro

# compétitions retenues pour les coupons (principales + complément)
LIGUES_COUPONS = {
    39: "Premier League", 140: "La Liga", 135: "Serie A", 78: "Bundesliga",
    61: "Ligue 1", 88: "Eredivisie", 94: "Liga Portugal",
    2: "Ligue des Champions", 3: "Ligue Europa",
    144: "Jupiler Pro League", 203: "Süper Lig", 179: "Scottish Premiership",
    218: "Österreich Bundesliga", 207: "Super League Suisse",
    119: "Superliga", 197: "Super League Grèce",
}
D2_VERS_D1 = {40: 39, 141: 140, 136: 135, 79: 78, 62: 61, 89: 88, 95: 94}

# bookmakers préférés, dans l'ordre (partenaires d'abord)
BOOKMAKERS = ["1xbet", "melbet", "betwinner", "1win", "bet365", "pinnacle"]

# (clé, cote visée, plancher, plafond, matchs maxi, nombre de coupons par jour)
CATEGORIES = [
    ("sure",        2.2,   1.7,   2.5,  4, 3),
    ("montante",    1.8,   1.6,   1.9,  3, 1),
    ("confiance",   6.0,   3.0,  10.0,  7, 3),
    ("fun",        15.0,  10.0,  20.0, 13, 2),
    ("grosses",    60.0,  20.0, 150.0, 13, 2),
]

# priorité des marchés voulue par Babs (0 = servi en premier)
PRIORITE = {"1": 0, "2": 0, "O2.5": 1, "U2.5": 1,
            "1X": 2, "X2": 2, "12": 2, "BTTS": 3, "NOBTTS": 3}


# ==================================================================
# Outils
# ==================================================================
def api(chemin, params):
    r = requests.get(f"{BASE_API}/{chemin}", headers={"x-apisports-key": CLE_API},
                     params=params, timeout=30)
    r.raise_for_status()
    return r.json().get("response", [])


def sb(chemin, methode="GET", corps=None, prefer=None):
    entetes = {"apikey": CLE_SB, "Authorization": f"Bearer {CLE_SB}",
               "Content-Type": "application/json"}
    if prefer:
        entetes["Prefer"] = prefer
    r = requests.request(methode, f"{URL_SB}/rest/v1/{chemin}", headers=entetes,
                         data=json.dumps(corps) if corps is not None else None, timeout=30)
    if not r.ok:
        print(f"   ⚠️ Supabase {r.status_code} sur {chemin} : {r.text[:180]}")
        return []
    return r.json() if r.text.strip() else []


# ==================================================================
# 1. Candidats : probabilités du moteur sur les matchs à venir
# ==================================================================
def candidats():
    histo = pd.read_csv(F_HISTO)
    histo["date"] = pd.to_datetime(histo["date"], errors="coerce", utc=True).dt.tz_localize(None)
    maintenant = datetime.now(timezone.utc).replace(tzinfo=None)
    fin = maintenant + timedelta(hours=FENETRE_H)

    lignes = []
    for lid, nom in LIGUES_COUPONS.items():
        viviers = [lid] + [d2 for d2, d1 in D2_VERS_D1.items() if d1 == lid]
        passe = histo[histo.ligue_id.isin(viviers) & (histo.statut == "FT")].dropna(
            subset=["buts_dom", "buts_ext"])
        avenir = histo[(histo.ligue_id == lid) & (histo.statut == "NS") &
                       (histo.date >= maintenant) & (histo.date < fin)]
        if len(passe) < 120 or avenir.empty:
            continue
        try:
            m = Moteur(passe, date_ref=pd.Timestamp(maintenant.date()))
        except ValueError:
            continue

        for _, f in avenir.iterrows():
            fiche = m.analyser(f.equipe_dom, f.equipe_ext)
            if "erreur" in fiche:
                continue
            p = fiche["_probas"]
            lignes.append({
                "fixture_id": int(f.fixture_id), "ligue": nom,
                "dom": f.equipe_dom, "ext": f.equipe_ext,
                "date_match": f.date.date().isoformat(),
                "heure": f.get("heure"),
                "p1": p["1"], "pN": p["N"], "p2": p["2"], "pO25": p["O2.5"],
                "btts": fiche["bonus"]["btts_oui"] / 100,
            })
    print(f"   {len(lignes)} match(s) analysés pour les coupons")
    return lignes


# ==================================================================
# 2. Cotes réelles (API-Football)
# ==================================================================
def id_bookmaker():
    """Choisit un bookmaker : d'abord les partenaires, sinon le premier valable.
    L'API renvoie parfois des entrées sans nom : on les ignore."""
    liste = api("odds/bookmakers", {})
    par_nom = {}
    for b in liste:
        nom, bid = b.get("name"), b.get("id")
        if isinstance(nom, str) and nom.strip() and bid:
            par_nom[nom.strip().lower()] = bid
    if not par_nom:
        print("   ❌ aucun bookmaker exploitable renvoyé par l'API")
        return None
    for voulu in BOOKMAKERS:
        for nom, bid in par_nom.items():
            if voulu in nom:
                print(f"   Cotes fournies par : {nom}")
                return bid
    nom, bid = next(iter(par_nom.items()))
    print(f"   Cotes fournies par : {nom} (aucun partenaire disponible)")
    return bid


def cotes_du_match(fixture_id, bookmaker):
    rep = api("odds", {"fixture": fixture_id, "bookmaker": bookmaker})
    cotes = {}
    for bloc in rep:
        for bk in bloc.get("bookmakers", []):
            for pari in bk.get("bets", []):
                nom = pari.get("name", "").lower()
                vals = {v["value"].lower(): float(v["odd"]) for v in pari.get("values", [])
                        if v.get("odd")}
                if nom == "match winner":
                    cotes["1"] = vals.get("home")
                    cotes["2"] = vals.get("away")
                elif nom == "double chance":
                    cotes["1X"] = vals.get("home/draw")
                    cotes["X2"] = vals.get("draw/away")
                    cotes["12"] = vals.get("home/away")
                elif nom == "goals over/under":
                    cotes["O2.5"] = vals.get("over 2.5")
                    cotes["U2.5"] = vals.get("under 2.5")
                elif nom == "both teams score":
                    cotes["BTTS"] = vals.get("yes")
                    cotes["NOBTTS"] = vals.get("no")
    return {k: v for k, v in cotes.items() if v and v > 1.01}


LIBELLES = {
    "1": ("1X2", "Victoire {dom}"), "2": ("1X2", "Victoire {ext}"),
    "1X": ("Double chance", "{dom} ou nul"), "X2": ("Double chance", "Nul ou {ext}"),
    "12": ("Double chance", "{dom} ou {ext} (pas de nul)"),
    "O2.5": ("Buts", "Plus de 2,5 buts"), "U2.5": ("Buts", "Moins de 2,5 buts"),
    "BTTS": ("Buts", "Les deux équipes marquent"),
    "NOBTTS": ("Buts", "Les deux équipes ne marquent pas"),
}


def selections_possibles(matchs, bookmaker):
    """Une liste de sélections (match + marché) avec probabilité et cote réelle."""
    out = []
    for m in matchs:
        cotes = cotes_du_match(m["fixture_id"], bookmaker)
        time.sleep(0.4)
        if not cotes:
            continue
        probas = {
            "1": m["p1"], "2": m["p2"],
            "1X": m["p1"] + m["pN"], "X2": m["pN"] + m["p2"], "12": m["p1"] + m["p2"],
            "O2.5": m["pO25"], "U2.5": 1 - m["pO25"],
            "BTTS": m["btts"], "NOBTTS": 1 - m["btts"],
        }
        for code, cote in cotes.items():
            p = probas.get(code)
            if p is None or p < 0.35:          # on ne propose rien sous 35 %
                continue
            marche, gabarit = LIBELLES[code]
            out.append({
                **{k: m[k] for k in ("fixture_id", "ligue", "dom", "ext", "date_match", "heure")},
                "code": code, "marche": marche,
                "selection": gabarit.format(dom=m["dom"], ext=m["ext"]),
                "cote": cote, "proba": p, "confiance": round(p * 100),
                "priorite": PRIORITE[code],
            })
    print(f"   {len(out)} sélection(s) disponibles avec cotes réelles")
    return out


# ==================================================================
# 3. Construction des coupons
# ==================================================================
def batir(pool, cible, plancher, plafond, max_matchs, deja_pris):
    """Empile des sélections jusqu'à approcher la cote visée, en gardant
    les probabilités les plus hautes possibles pour cette cible."""
    dispo = [s for s in pool if (s["fixture_id"], s["code"]) not in deja_pris]
    if not dispo:
        return None, 0.0

    # cote moyenne nécessaire pour atteindre la cible avec max_matchs sélections
    moy_visee = cible ** (1 / max(1, max_matchs))
    # on sert d'abord les sélections proches de cette cote moyenne, en respectant
    # l'ordre de priorité des marchés voulu (V1/V2 → buts → double chance)
    dispo.sort(key=lambda s: (s["priorite"], abs(s["cote"] - moy_visee), -s["proba"]))

    choisies, matchs_pris, total = [], set(), 1.0
    for s in dispo:
        if len(choisies) >= max_matchs or total >= cible:
            break
        if s["fixture_id"] in matchs_pris:
            continue
        if total * s["cote"] > plafond:
            continue
        choisies.append(s)
        matchs_pris.add(s["fixture_id"])
        total *= s["cote"]

    if not choisies or total < plancher:
        return None, 0.0
    return choisies, round(total, 2)


def construire(pool):
    coupons, deja_pris = [], set()
    for cle, cible, plancher, plafond, nmax, combien in CATEGORIES:
        faits = 0
        for numero in range(1, combien + 1):
            sel, total = batir(pool, cible, plancher, plafond, nmax, deja_pris)
            if not sel:
                break
            coupons.append({"categorie": cle, "numero": numero,
                            "selections": sel, "cote_totale": total})
            for s in sel:
                deja_pris.add((s["fixture_id"], s["code"]))
            print(f"   ✅ {cle} #{numero} : {len(sel)} match(s), cote {total}")
            faits += 1
        if faits == 0:
            print(f"   ⚠️ {cle} : pas assez de matchs aujourd'hui")
    return coupons


# ==================================================================
# 4. Vérification des coupons de la veille
# ==================================================================
def gagnee(code, bd, be):
    if code == "1":      return bd > be
    if code == "2":      return be > bd
    if code == "1X":     return bd >= be
    if code == "X2":     return be >= bd
    if code == "12":     return bd != be
    if code == "O2.5":   return bd + be > 2.5
    if code == "U2.5":   return bd + be < 2.5
    if code == "BTTS":   return bd > 0 and be > 0
    if code == "NOBTTS": return bd == 0 or be == 0
    return None


def verifier():
    en_cours = sb("coupons?statut=eq.en_cours&select=id,jour,categorie")
    if not en_cours:
        return
    histo = pd.read_csv(F_HISTO)
    finis = histo[(histo.statut == "FT")].dropna(subset=["buts_dom", "buts_ext"])
    scores = {int(r.fixture_id): (int(r.buts_dom), int(r.buts_ext)) for _, r in finis.iterrows()}

    for c in en_cours:
        sels = sb(f"coupon_selections?coupon_id=eq.{c['id']}&select=id,fixture_id,code,resultat")
        if not sels:
            continue
        tous_joues, perdu = True, False
        for s in sels:
            if s["fixture_id"] not in scores:
                tous_joues = False
                continue
            if s["resultat"] is None:
                ok = gagnee(s["code"], *scores[s["fixture_id"]])
                sb(f"coupon_selections?id=eq.{s['id']}", "PATCH",
                   {"resultat": "gagne" if ok else "perdu"})
                if not ok:
                    perdu = True
            elif s["resultat"] == "perdu":
                perdu = True
        if perdu or tous_joues:
            statut = "perdu" if perdu else "gagne"
            sb(f"coupons?id=eq.{c['id']}", "PATCH", {"statut": statut})
            print(f"   Coupon {c['categorie']} du {c['jour']} → {statut}")
            if c["categorie"] == "montante":
                sb(f"montante?coupon_id=eq.{c['id']}", "PATCH", {"statut": statut})


# ==================================================================
# 5. Montante
# ==================================================================
def avancer_montante(coupon_id, cote, jour):
    paliers = sb("montante?select=serie,palier,mise,gain_vise,statut&order=serie.desc,palier.desc&limit=1")
    if paliers:
        d = paliers[0]
        if d["statut"] == "gagne":
            atteint = float(d["gain_vise"]) / MISE_DEPART
            if atteint >= OBJECTIF_MONTANTE:
                serie, palier, mise = d["serie"] + 1, 1, MISE_DEPART   # objectif atteint → nouvelle série
            else:
                serie, palier, mise = d["serie"], d["palier"] + 1, float(d["gain_vise"])
        elif d["statut"] == "perdu":
            serie, palier, mise = d["serie"] + 1, 1, MISE_DEPART
        else:
            return   # palier encore en cours : on n'en ouvre pas un second
    else:
        serie, palier, mise = 1, 1, MISE_DEPART

    sb("montante", "POST", {
        "serie": serie, "palier": palier, "coupon_id": coupon_id,
        "mise": mise, "gain_vise": round(mise * cote, 2), "jour": jour,
    }, prefer="return=minimal")
    print(f"   Montante série {serie} · palier {palier} : {int(mise)} F → {int(mise*cote)} F")


# ==================================================================
# 6. Enregistrement
# ==================================================================
def enregistrer(coupons, jour):
    for c in coupons:
        cle, numero = c["categorie"], c["numero"]
        if sb(f"coupons?jour=eq.{jour}&categorie=eq.{cle}&numero=eq.{numero}&select=id"):
            print(f"   ({cle} #{numero} déjà publié aujourd'hui)")
            continue
        cree = sb("coupons", "POST", {
            "jour": jour, "categorie": cle, "numero": numero,
            "cote_totale": c["cote_totale"], "nb_matchs": len(c["selections"]),
        }, prefer="return=representation")
        if not cree:
            continue
        cid = cree[0]["id"]
        sb("coupon_selections", "POST", [{
            "coupon_id": cid, "fixture_id": s["fixture_id"], "ligue": s["ligue"],
            "dom": s["dom"], "ext": s["ext"], "date_match": s["date_match"],
            "heure": s["heure"], "marche": s["marche"], "selection": s["selection"],
            "code": s["code"], "cote": s["cote"], "confiance": s["confiance"],
        } for s in c["selections"]], prefer="return=minimal")
        if cle == "montante":
            avancer_montante(cid, c["cote_totale"], jour)


# ==================================================================
def main():
    if not (CLE_API and URL_SB and CLE_SB):
        sys.exit("❌ API_FOOTBALL_KEY / SUPABASE_URL / SUPABASE_SERVICE_KEY manquantes")

    print("→ Vérification des coupons précédents")
    verifier()

    print("→ Analyse des matchs à venir")
    matchs = candidats()
    if len(matchs) < 5:
        print("   (trop peu de matchs : aucun coupon aujourd'hui)")
        return

    bookmaker = id_bookmaker()
    if not bookmaker:
        print("   ❌ aucun bookmaker disponible")
        return

    pool = selections_possibles(matchs, bookmaker)
    if len(pool) < 5:
        print("   (pas assez de cotes disponibles)")
        return

    print("→ Construction des coupons")
    coupons = construire(pool)
    if coupons:
        enregistrer(coupons, datetime.now(timezone.utc).date().isoformat())
    print("✓ coupons du jour terminés")


if __name__ == "__main__":
    main()
