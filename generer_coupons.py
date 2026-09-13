#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
AURA90 — GÉNÉRATEUR DE COUPONS
Tourne dans le cron, après cron_quotidien.py.

1. Vérifie les coupons de la veille (résultats réels)
2. Construit les coupons du jour : Sûre · Confiance · Fun · Grosses cotes · Montante
3. Fait avancer la montante en cours (objectif ×15, départ 5 000 F)

Règles : une seule sélection par match · pas deux fois la même sélection
dans deux coupons différents · jamais deux codes contradictoires sur un match.

V2 (13/09/2026) — le moteur est ENRICHI (enrichissement.py : blessés et
suspendus pondérés par le statut de titulaire, repos et enchaînement, expected
goals des derniers matchs, contexte de classement) et la LOGIQUE DE SÉLECTION
est déléguée à selection_v2.py :
  · probabilité fusionnée marché + moteur (le marché est l'ancre, le moteur
    corrige à hauteur de POIDS_MOTEUR) — fin de l'anti-sélection
  · minimum de sélections par coupon, cotes individuelles bornées par catégorie
  · le constructeur maximise la probabilité de passer à cote donnée, et ne
    publie rien sous le seuil de la catégorie
  · correctif : les coupes d'Europe sont analysées avec un moteur entraîné sur
    tous les championnats (comme cron_quotidien.py V3.1) — avant, aucun match
    de C1/C2 n'entrait dans les coupons
  · correctif : la cote du nul est récupérée (nécessaire pour retirer la marge)

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
import selection_v2 as SEL                     # noqa: E402  (logique v2)
import enrichissement as ENR                   # noqa: E402  (absences, fatigue, xG, classement)

CLE_API = os.environ.get("API_FOOTBALL_KEY", "")
URL_SB = os.environ.get("SUPABASE_URL", "").rstrip("/")
CLE_SB = os.environ.get("SUPABASE_SERVICE_KEY", "")
BASE_API = "https://v3.football.api-sports.io"

F_HISTO = "donnees/histo_api.csv"
SAISON = 2026
FIN_DE_JOURNEE = True     # on ne retient que les matchs du jour même
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

# Coupes d'Europe : moteur entraîné sur TOUT l'historique européen (championnats
# collectés par le cron + matchs de C1/C2 passés), jamais sur la seule coupe.
COMPETITIONS_UEFA = {2, 3}
LIGUES_SANS_LIEN_EUROPE = {71, 128, 253, 98, 262, 239}

# Championnats joués pendant la nuit africaine (23h → 7h)
LIGUES_NUIT = {
    71:  "Brésil Série A",
    128: "Argentine Liga Profesional",
    253: "MLS",
    262: "Liga MX",
    239: "Colombie Primera A",
}
# (Liga MX et Colombie ne produiront des coupons qu'une fois leur historique
#  collecté par le cron ; sans données, le moteur les ignore silencieusement)
NUIT_DEBUT, NUIT_FIN = 22, 7      # heures (UTC = heure de Dakar)

# bookmakers préférés, dans l'ordre (partenaires d'abord)
COTE_MAX_SELECTION = 3.60    # au-delà, c'est un outsider : jamais dans un combiné
# (les anciens ECART_MAX_MARCHE / TAILLE_MINI / CATEGORIES sont remplacés par
#  selection_v2.CATEGORIES : fourchettes, tailles et seuils par catégorie)

BOOKMAKERS = ["1xbet", "melbet", "betwinner", "1win", "bet365", "pinnacle"]

# ==================================================================
# Outils
# ==================================================================
def api(chemin, params):
    r = requests.get(f"{BASE_API}/{chemin}", headers={"x-apisports-key": CLE_API},
                     params=params, timeout=30)
    r.raise_for_status()
    return r.json().get("response", [])


def sb(chemin, methode="GET", corps=None, prefer=None, essais=3):
    """Appel Supabase REST. Les erreurs 5xx (504 Gateway Timeout…) sont
    retentées avec une pause croissante ; les 4xx sont remontées telles quelles."""
    entetes = {"apikey": CLE_SB, "Authorization": f"Bearer {CLE_SB}",
               "Content-Type": "application/json"}
    if prefer:
        entetes["Prefer"] = prefer
    for essai in range(essais):
        try:
            r = requests.request(methode, f"{URL_SB}/rest/v1/{chemin}", headers=entetes,
                                 data=json.dumps(corps) if corps is not None else None, timeout=60)
        except requests.RequestException as e:
            print(f"   ⚠️ Supabase réseau sur {chemin} : {e}")
            time.sleep(3 * (essai + 1)); continue
        if r.status_code >= 500:
            print(f"   ⏳ Supabase {r.status_code} sur {chemin}, nouvelle tentative…")
            time.sleep(3 * (essai + 1)); continue
        if not r.ok:
            print(f"   ⚠️ Supabase {r.status_code} sur {chemin} : {r.text[:180]}")
            return None
        return r.json() if r.text.strip() else []
    print(f"   ❌ Supabase injoignable sur {chemin} après {essais} tentatives")
    return None


# ==================================================================
# 1. Candidats : probabilités du moteur sur les matchs à venir
# ==================================================================
def candidats(demain=False, nuit=False):
    histo = pd.read_csv(F_HISTO)
    # la colonne « date » ne contient que le jour : on recompose l'horodatage réel
    jours = pd.to_datetime(histo["date"], errors="coerce").dt.normalize()
    heures = pd.to_timedelta(histo["heure"].fillna("00:00").astype(str) + ":00", errors="coerce")
    histo["date"] = jours + heures.fillna(pd.Timedelta(0))
    maintenant = datetime.now(timezone.utc).replace(tzinfo=None)
    if nuit:
        # nuit du jour visé : de 22h ce soir-là à 7h le lendemain matin
        base = maintenant.date() + timedelta(days=1 if demain else 0)
        debut = datetime.combine(base, datetime.min.time()) + timedelta(hours=NUIT_DEBUT)
        fin = datetime.combine(base + timedelta(days=1), datetime.min.time()) + timedelta(hours=NUIT_FIN)
        debut = max(debut, maintenant)      # jamais un match déjà commencé
    elif demain:
        # toute la journée de demain
        debut = datetime.combine(maintenant.date() + timedelta(days=1), datetime.min.time())
        fin = debut + timedelta(days=1)
    else:
        # ce qu'il reste de la journée en cours (jamais les matchs de demain)
        debut = maintenant
        fin = datetime.combine(maintenant.date() + timedelta(days=1), datetime.min.time())

    lignes = []
    moteur_europe = {"m": None, "essaye": False}

    def obtenir_moteur_europe():
        if not moteur_europe["essaye"]:
            moteur_europe["essaye"] = True
            passe = histo[~histo.ligue_id.isin(LIGUES_SANS_LIEN_EUROPE) &
                          (histo.statut == "FT")].dropna(subset=["buts_dom", "buts_ext"])
            try:
                moteur_europe["m"] = Moteur(passe, date_ref=pd.Timestamp(maintenant.date()))
                print(f"   🌍 moteur européen : {len(passe):,} matchs")
            except ValueError as e:
                print(f"   ⚠️ moteur européen indisponible : {e}")
        return moteur_europe["m"]

    for lid, nom in (LIGUES_NUIT if nuit else LIGUES_COUPONS).items():
        avenir = histo[(histo.ligue_id == lid) & (histo.statut == "NS") &
                       (histo.date >= debut) & (histo.date < fin)]
        if avenir.empty:
            continue
        if lid in COMPETITIONS_UEFA:
            m = obtenir_moteur_europe()
            if m is None:
                continue
        else:
            viviers = [lid] + [d2 for d2, d1 in D2_VERS_D1.items() if d1 == lid]
            passe = histo[histo.ligue_id.isin(viviers) & (histo.statut == "FT")].dropna(
                subset=["buts_dom", "buts_ext"])
            if len(passe) < 120:
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
            # le moteur renvoie des pourcentages (46) : on ramène tout sur 0-1
            ech = lambda v: (float(v) / 100) if float(v) > 1 else float(v)
            lignes.append({
                "fixture_id": int(f.fixture_id), "ligue": nom, "ligue_id": int(lid),
                "dom": f.equipe_dom, "ext": f.equipe_ext,
                "date_match": f.date.date().isoformat(),
                "heure": f.get("heure"),
                "logo_dom": f.get("logo_dom"), "logo_ext": f.get("logo_ext"),
                "p1": ech(p["1"]), "pN": ech(p["N"]), "p2": ech(p["2"]), "pO25": ech(p["O2.5"]),
                "btts": ech(fiche["bonus"]["btts_oui"]),
            })
    print(f"   {len(lignes)} match(s) analysés{' (nuit)' if nuit else ''}")
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
                    cotes["N"] = vals.get("draw")     # nécessaire pour retirer la marge
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
    """Sélections candidates avec probabilité FUSIONNÉE (marché + moteur) et
    cote réelle. Les libellés et champs attendus par la base sont ajoutés ici."""
    entrees = []
    for m in matchs:
        cotes = cotes_du_match(m["fixture_id"], bookmaker)
        time.sleep(0.4)
        if not cotes:
            continue
        cotes = {k: v for k, v in cotes.items() if k == "N" or v <= COTE_MAX_SELECTION}
        entrees.append({
            **{k: m[k] for k in ("fixture_id", "ligue", "dom", "ext", "date_match",
                                 "heure", "logo_dom", "logo_ext")},
            "cotes": cotes,
            "moteur": {"1": m["p1"], "N": m["pN"], "2": m["p2"],
                       "O2.5": m["pO25"], "BTTS": m["btts"]},
        })
    out = SEL.preparer_selections(entrees)
    for s in out:
        marche, gabarit = LIBELLES[s["code"]]
        s["marche"] = marche
        s["selection"] = gabarit.format(dom=s["dom"], ext=s["ext"])
        # « confiance » stockée = probabilité fusionnée (marché + moteur), en %
        s["confiance"] = round(s["p"] * 100)
    print(f"   {len(out)} sélection(s) disponibles avec cotes réelles")
    return out


# ==================================================================
# 3. Construction des coupons (logique dans selection_v2.py)
# ==================================================================
def construire(pool, nuit=False):
    """Sert les catégories en rotation via selection_v2. Retourne des coupons
    au format attendu par enregistrer()."""
    if not pool:
        return []
    ordre = SEL.ORDRE_NUIT if nuit else SEL.ORDRE_JOUR
    coupons = SEL.construire_coupons(pool, ordre=ordre, journal=print)
    return [{"categorie": c["categorie"], "numero": c["numero"],
             "selections": c["selections"], "cote_totale": c["cote_totale"]}
            for c in coupons]


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
def enregistrer(coupons, jour, nuit=False):
    from datetime import date as _date
    lendemain = (datetime.fromisoformat(jour).date() + timedelta(days=1)).isoformat()
    autorises = {jour, lendemain} if nuit else {jour}

    # on efface d'abord les coupons du jour encore en cours : une nouvelle
    # exécution doit toujours refléter la logique la plus récente
    def supprimer_coupon(cid):
        """Supprime un coupon et ses dépendances, puis VÉRIFIE qu'il a disparu."""
        for _ in range(2):
            sb(f"coupon_selections?coupon_id=eq.{cid}", "DELETE")
            sb(f"montante?coupon_id=eq.{cid}", "DELETE")
            sb(f"coupons?id=eq.{cid}", "DELETE")
            reste = sb(f"coupons?id=eq.{cid}&select=id")
            if isinstance(reste, list) and not reste:
                return True
            time.sleep(2)
        print(f"   ❌ coupon {cid} toujours présent après suppression")
        return False

    cats = {c["categorie"] for c in coupons}
    for cat in cats:
        anciens = sb(f"coupons?jour=eq.{jour}&categorie=eq.{cat}&statut=eq.en_cours&select=id")
        n = 0
        for a in (anciens if isinstance(anciens, list) else []):
            n += supprimer_coupon(a["id"])
        if n:
            print(f"   ↻ {cat} : {n} ancien(s) coupon(s) remplacé(s)")

    for c in coupons:
        # garde-fou : aucune sélection en dehors de la journée visée
        hors = [s for s in c["selections"] if s["date_match"] not in autorises]
        if hors:
            print(f"   ❌ {c['categorie']} #{c['numero']} : {len(hors)} match(s) hors du "
                  f"{jour} → coupon annulé")
            continue
        cle, numero = c["categorie"], c["numero"]
        corps = {"jour": jour, "categorie": cle, "numero": numero,
                 "cote_totale": c["cote_totale"], "nb_matchs": len(c["selections"])}
        cree = sb("coupons", "POST", corps, prefer="return=representation")
        if not cree:
            # clé en double : un ancien coupon en cours a survécu → on l'enlève et on réessaie
            doublon = sb(f"coupons?jour=eq.{jour}&categorie=eq.{cle}&numero=eq.{numero}"
                         f"&statut=eq.en_cours&select=id")
            if isinstance(doublon, list) and doublon and supprimer_coupon(doublon[0]["id"]):
                cree = sb("coupons", "POST", corps, prefer="return=representation")
        if not cree:
            print(f"   ❌ {cle} #{numero} : impossible d'enregistrer le coupon")
            continue
        cid = cree[0]["id"]
        rep_sel = sb("coupon_selections", "POST", [{
            "coupon_id": cid, "fixture_id": s["fixture_id"], "ligue": s["ligue"],
            "dom": s["dom"], "ext": s["ext"], "date_match": s["date_match"],
            "heure": s["heure"], "logo_dom": s["logo_dom"], "logo_ext": s["logo_ext"],
            "marche": s["marche"], "selection": s["selection"],
            "code": s["code"], "cote": s["cote"], "confiance": s["confiance"],
        } for s in c["selections"]], prefer="return=representation")

        # garde-fou : un coupon sans sélection ne doit jamais rester en base
        verif = sb(f"coupon_selections?coupon_id=eq.{cid}&select=id")
        if not isinstance(verif, list) or len(verif) != len(c["selections"]):
            print(f"   ❌ {cle} #{numero} : sélections non enregistrées ({len(verif) if isinstance(verif, list) else 0}"
                  f"/{len(c['selections'])}) → coupon annulé")
            sb(f"coupon_selections?coupon_id=eq.{cid}", "DELETE")
            sb(f"coupons?id=eq.{cid}", "DELETE")
            continue

        if cle == "montante":
            avancer_montante(cid, c["cote_totale"], jour)   # un seul palier ouvert à la fois


# ==================================================================
def main():
    if not (CLE_API and URL_SB and CLE_SB):
        sys.exit("❌ API_FOOTBALL_KEY / SUPABASE_URL / SUPABASE_SERVICE_KEY manquantes")

    print("→ Vérification des coupons précédents")
    verifier()

    if "--verifier-seulement" in sys.argv:
        print("✓ vérification terminée (pas de nouveaux coupons)")
        return

    demain = "--demain" in sys.argv
    jour = (datetime.now(timezone.utc).date() + timedelta(days=1 if demain else 0)).isoformat()
    print(f"→ Analyse des matchs du {jour}" + (" (préparation de demain)" if demain else ""))
    matchs = candidats(demain)
    if len(matchs) < 5:
        print("   (trop peu de matchs : aucun coupon aujourd'hui)")
        return
    histo_brut = pd.read_csv(F_HISTO)
    print("→ Enrichissement (absences, fatigue, xG, classement)")
    matchs = ENR.enrichir(matchs, histo_brut)
    for m in matchs:
        if m.get("contexte"):
            print(f"   · {m['dom']} – {m['ext']} : {m['contexte'][:260]}")

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

    # ----- coupons de la nuit (Amériques) -----
    print(f"→ Coupons de la nuit du {jour} (22h → 7h)")
    coupons_nuit = []
    matchs_nuit = candidats(demain, nuit=True)
    if matchs_nuit:
        matchs_nuit = ENR.enrichir(matchs_nuit, histo_brut)
        pool_nuit = selections_possibles(matchs_nuit, bookmaker)
        if pool_nuit:
            coupons_nuit = construire(pool_nuit, nuit=True)
    else:
        print("   (aucun match cette nuit)")

    ordre = {"sure": 0, "confiance": 1, "fun": 2, "grosses": 3, "nuit": 4, "montante": 5}
    if coupons:
        coupons.sort(key=lambda c: (ordre.get(c["categorie"], 9), c["numero"]))
        enregistrer(coupons, jour)
    if coupons_nuit:
        enregistrer(coupons_nuit, jour, nuit=True)
    print(f"✓ coupons du {jour} terminés")


if __name__ == "__main__":
    main()
