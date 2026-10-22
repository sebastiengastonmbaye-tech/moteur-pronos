#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
AURA90 — GÉNÉRATEUR DE COUPONS
Tourne dans le cron, après cron_quotidien.py.

1. Vérifie les coupons de la veille (résultats réels)
2. Construit les coupons du jour : Confiance · Fun · Montante · Turbo (Sûre retirée le 17/10)
3. Fait avancer les deux montantes : Montante (10 paliers ; 12 pour la série n°6) et Montante Turbo
   (5 paliers, cote 1,90–2,60), départ 10 000 F

Règles : une seule sélection par match · pas deux fois la même sélection
dans deux coupons différents · jamais deux codes contradictoires sur un match.

V2.5 (01/10/2026) — UN COUPON PUBLIÉ NE BOUGE PLUS : une relance du cron ne
fait que compléter les catégories vides (--forcer pour régénérer, jamais les
montantes). Équipes de jeunes et féminines exclues des coupons.
V2.4 (29/09/2026) — Montante passée à 12 paliers ; MONTANTE TURBO (catégorie
« turbo », 5 paliers, cote 1,90–2,60, même exigence d'analyse) ; les deux
montantes ne partagent jamais un match le même jour.
V2.3 (28/09/2026) — le passage de 5h publie AUSSI les coupons de demain
(onglet « Demain » rempli toute la journée ; 18h les met à jour). Sûre et
Montante ont un palier de repli (selection_v2.py) pour ne plus rester vides.
V2.2 (24/09/2026) — SCORE EXACT (catégorie « score », 1-2 scores par match, les
plus nets du jour, gagné dès qu'un score tombe) + COMBINÉS TIKTOK (catégorie
« tiktok », 3 par jour, 10-15 matchs, cote ≥ 90, 7 jours glissants, figés à J+2).
V2.1 (19/09/2026) — montante en 15 PALIERS (plus d'objectif ×15) ; Sûre et
Montante n'acceptent que des sélections de qualité « prono signé » ; grosses
cotes échelonnées (35 puis 70) ; équipes nationales (Ligue des Nations, qualifs
CAN/CdM, amicaux) pour les trêves internationales.
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
import extras_coupons as EXT                   # noqa: E402  (score exact, combinés TikTok)
SCORE_EXACT_ACTIF = False                       # section Score exact retirée le 21/10/2026

CLE_API = os.environ.get("API_FOOTBALL_KEY", "")
URL_SB = os.environ.get("SUPABASE_URL", "").rstrip("/")
CLE_SB = os.environ.get("SUPABASE_SERVICE_KEY", "")
BASE_API = "https://v3.football.api-sports.io"

F_HISTO = "donnees/histo_api.csv"
SAISON = 2026
FIN_DE_JOURNEE = True     # on ne retient que les matchs du jour même
MISE_DEPART = 10000       # montantes : mise de départ en F CFA (10 000 F depuis le 29/09/2026)
PALIERS = {"montante": 10, "turbo": 5}   # paliers par série ; série bouclée → nouvelle série
PALIERS_EXCEPTIONS = {("montante", 6): 12}   # la série n°6, lancée avant le changement, garde ses 12 paliers


def nb_paliers(typ, serie):
    return PALIERS_EXCEPTIONS.get((typ, serie), PALIERS[typ])
NOMS_MONTANTE = {"montante": "Montante", "turbo": "Montante Turbo"}

# compétitions retenues pour les coupons (principales + complément)
LIGUES_COUPONS = {
    39: "Premier League", 140: "La Liga", 135: "Serie A", 78: "Bundesliga",
    61: "Ligue 1", 88: "Eredivisie", 94: "Liga Portugal",
    2: "Ligue des Champions", 3: "Ligue Europa", 848: "Ligue Conférence",
    144: "Jupiler Pro League", 203: "Süper Lig", 179: "Scottish Premiership",
    218: "Österreich Bundesliga", 207: "Super League Suisse",
    119: "Superliga", 197: "Super League Grèce",
}
D2_VERS_D1 = {40: 39, 141: 140, 136: 135, 79: 78, 62: 61, 89: 88, 95: 94}

# Équipes nationales (trêves internationales) : un seul moteur entraîné sur
# toutes ces compétitions, avec des réglages adaptés au peu de matchs par an.
LIGUES_NATIONS = {
    5: "UEFA Nations League", 36: "CAN — qualifications",
    29: "CdM — qualifications Afrique", 32: "CdM — qualifications Europe",
    10: "Amicaux internationaux",
}
PARAMS_NATIONS = {"min_matchs": 2.5, "xi": 0.003}

# Coupes d'Europe : moteur entraîné sur TOUT l'historique européen (championnats
# collectés par le cron + matchs de C1/C2 passés), jamais sur la seule coupe.
COMPETITIONS_UEFA = {2, 3, 848}
LIGUES_SANS_LIEN_EUROPE = {71, 128, 253, 98, 262, 239} | set(LIGUES_NATIONS)

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
import re as _re
_JEUNES = _re.compile(r"\bU-?(1[5-9]|2[0-3])\b|\bwomen\b|\(w\)|\bfemin|\bféminin|\sW$", _re.I)


def equipe_jeunes(nom):
    """Équipes de jeunes (U17, U19, U21…) ou féminines : jamais dans les coupons —
    le moteur n'a pas de données fiables sur elles."""
    return bool(_JEUNES.search(str(nom or "")))


STATUTS_LIVE = ("NS", "1H", "HT", "2H", "ET", "BT", "P", "LIVE", "INT")


def candidats(demain=False, nuit=False, jour_cible=None, enrichir=True,
              statuts=("NS",), depuis_debut=False):
    histo = pd.read_csv(F_HISTO)
    # la colonne « date » ne contient que le jour : on recompose l'horodatage réel
    jours = pd.to_datetime(histo["date"], errors="coerce").dt.normalize()
    heures = pd.to_timedelta(histo["heure"].fillna("00:00").astype(str) + ":00", errors="coerce")
    histo["date"] = jours + heures.fillna(pd.Timedelta(0))
    maintenant = datetime.now(timezone.utc).replace(tzinfo=None)
    if jour_cible is not None:
        # une journée précise (combinés TikTok à J+1 … J+6), jamais un match commencé
        debut = datetime.combine(jour_cible, datetime.min.time())
        if not depuis_debut:
            debut = max(debut, maintenant)
        fin = datetime.combine(jour_cible + timedelta(days=1), datetime.min.time())
    elif nuit:
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
    api_enr = ENR.Api()               # un seul budget d'appels pour toute l'analyse
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

    moteur_nations = {"m": None, "essaye": False}

    def obtenir_moteur_nations():
        if not moteur_nations["essaye"]:
            moteur_nations["essaye"] = True
            passe = histo[histo.ligue_id.isin(LIGUES_NATIONS) &
                          (histo.statut == "FT")].dropna(subset=["buts_dom", "buts_ext"])
            try:
                moteur_nations["m"] = Moteur(passe, date_ref=pd.Timestamp(maintenant.date()), **PARAMS_NATIONS)
                print(f"   🌐 moteur équipes nationales : {len(passe):,} matchs")
            except ValueError as e:
                print(f"   ⚠️ moteur équipes nationales indisponible : {e}")
        return moteur_nations["m"]

    pool_ligues = LIGUES_NUIT if nuit else {**LIGUES_COUPONS, **LIGUES_NATIONS}
    for lid, nom in pool_ligues.items():
        avenir = histo[(histo.ligue_id == lid) & (histo.statut.isin(statuts)) &
                       (histo.date >= debut) & (histo.date < fin)]
        if avenir.empty:
            continue
        if lid in COMPETITIONS_UEFA:
            m = obtenir_moteur_europe()
            if m is None:
                continue
        elif lid in LIGUES_NATIONS:
            m = obtenir_moteur_nations()
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

        # PROMUS (17/10/2026) : équipe absente de cette ligue la saison passée et
        # moins de 8 matchs joués cette saison → niveau réel encore inconnu
        # (son attaque est gonflée par sa saison en 2e division). Écartée des
        # catégories prudentes (Sûre, Montante, Turbo).
        promus = {}
        if lid not in COMPETITIONS_UEFA and lid not in LIGUES_NATIONS and "saison" in histo.columns:
            lg = histo[histo.ligue_id == lid]
            avant = set(lg[lg.saison == SAISON - 1].equipe_dom) | set(lg[lg.saison == SAISON - 1].equipe_ext)
            cette = lg[(lg.saison == SAISON) & (lg.statut == "FT")]
            if avant:
                for eq in set(avenir.equipe_dom) | set(avenir.equipe_ext):
                    if eq not in avant:
                        joues = int(((cette.equipe_dom == eq) | (cette.equipe_ext == eq)).sum())
                        if joues < 8:
                            promus[eq] = joues

        # facteurs de contexte (absences, fatigue, xG, classement) pour ces matchs
        facteurs = {} if not enrichir else ENR.calculer_facteurs([{
            "fixture_id": int(f.fixture_id), "ligue_id": int(lid),
            "dom": f.equipe_dom, "ext": f.equipe_ext,
            "logo_dom": f.get("logo_dom"), "logo_ext": f.get("logo_ext"),
            "date_match": f.date.date().isoformat(),
        } for _, f in avenir.iterrows()], histo, journal=lambda *a: None, api=api_enr)

        avenir = avenir[~avenir.equipe_dom.map(equipe_jeunes) & ~avenir.equipe_ext.map(equipe_jeunes)]
        for _, f in avenir.iterrows():
            fiche = m.analyser(f.equipe_dom, f.equipe_ext,
                               ajustements=facteurs.get(int(f.fixture_id)))
            if "erreur" in fiche:
                continue
            if fiche.get("contexte"):
                print(f"   · {f.equipe_dom} – {f.equipe_ext} : {fiche['contexte'][:260]}")
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
                "pO15": ech(p.get("O1.5", 0)) or None, "pU35": ech(p.get("U3.5", 0)) or None,
                "promu": bool(promus.get(f.equipe_dom) or promus.get(f.equipe_ext)),
                "btts": ech(fiche["bonus"]["btts_oui"]),
                "scores": fiche.get("scores_probables", []),   # score exact
            })
    print(f"   {len(lignes)} match(s) analysés{' (nuit)' if nuit else ''}, "
          f"{api_enr.appels} appel(s) API d'enrichissement")
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
                print(f"   Cotes : référence du marché (tous bookmakers, {int(COTE_QUANTILE*100)}e centile) · secours : {nom}")
                return bid
    nom, bid = next(iter(par_nom.items()))
    print(f"   Cotes : référence du marché (tous bookmakers) · secours : {nom}")
    return bid


# ----- cote de référence (21/10/2026) -----
# Avant : cotes d'un seul bookmaker (1xBet), parmi les plus hautes du marché →
# les abonnés trouvaient des cotes nettement plus basses sur leurs sites (Winwin…).
# Maintenant : pour chaque pari, on lit TOUS les bookmakers du match (même nombre
# d'appels API) et on retient une cote un peu en dessous de la médiane du marché.
COTE_QUANTILE = 0.35        # 0.5 = médiane ; 0.35 = un peu plus prudent, sans abuser
COTE_MIN_BOOKS = 3          # en dessous, on prend la plus basse des cotes disponibles
STATS_COTES = {"matchs": 0, "books": 0}


def _cote_reference(valeurs):
    v = sorted(x for x in valeurs if x and x > 1.01)
    if not v:
        return None
    if len(v) < COTE_MIN_BOOKS:
        return v[0]
    pos = COTE_QUANTILE * (len(v) - 1)
    bas = int(pos)
    haut = min(bas + 1, len(v) - 1)
    return round(v[bas] + (v[haut] - v[bas]) * (pos - bas), 2)


def _lire_bookmaker(bk):
    """Cotes d'UN bookmaker, au format interne (1, N, 2, 1X, O2.5, SE:2-1…)."""
    cotes = {}
    for pari in bk.get("bets", []):
        nom = pari.get("name", "").lower()
        vals = {}
        for v in pari.get("values", []):
            try:
                if v.get("odd"):
                    vals[str(v["value"]).lower()] = float(v["odd"])
            except (TypeError, ValueError):
                pass
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
            cotes["U2.5"] = vals.get("under 2.5")     # gardée pour retirer la marge, jamais proposée
            cotes["O1.5"] = vals.get("over 1.5")
            cotes["U1.5"] = vals.get("under 1.5")
            cotes["O3.5"] = vals.get("over 3.5")
            cotes["U3.5"] = vals.get("under 3.5")
        elif nom == "both teams score":
            cotes["BTTS"] = vals.get("yes")
            cotes["NOBTTS"] = vals.get("no")
        elif nom in ("exact score", "correct score"):
            for val, odd in vals.items():          # "2:1" → "SE:2-1"
                if ":" in val:
                    cotes["SE:" + val.replace(":", "-").strip()] = odd
    return {k: v for k, v in cotes.items() if v and v > 1.01}


def cotes_du_match(fixture_id, bookmaker):
    """Cote de référence du marché pour chaque pari du match.
    `bookmaker` ne sert plus que de secours si l'API ne renvoie pas le marché complet."""
    rep = api("odds", {"fixture": fixture_id})
    par_cle = {}
    nb_books = 0
    for bloc in rep:
        for bk in bloc.get("bookmakers", []):
            c = _lire_bookmaker(bk)
            if c:
                nb_books += 1
            for k, v in c.items():
                par_cle.setdefault(k, []).append(v)
    if not par_cle and bookmaker:                      # secours : un seul bookmaker
        for bloc in api("odds", {"fixture": fixture_id, "bookmaker": bookmaker}):
            for bk in bloc.get("bookmakers", []):
                for k, v in _lire_bookmaker(bk).items():
                    par_cle.setdefault(k, []).append(v)
    propres = {}
    for k, vs in par_cle.items():
        c = _cote_reference(vs)
        if c and c > 1.01:
            propres[k] = c
    if propres:
        STATS_COTES["matchs"] += 1
        STATS_COTES["books"] += nb_books
    COTES_BRUTES[fixture_id] = propres
    return propres


COTES_BRUTES = {}   # fixture_id → toutes les cotes lues (dont Score exact)


LIBELLES = {
    "1": ("1X2", "Victoire {dom}"), "2": ("1X2", "Victoire {ext}"),
    "1X": ("Double chance", "{dom} ou nul"), "X2": ("Double chance", "Nul ou {ext}"),
    "12": ("Double chance", "{dom} ou {ext} (pas de nul)"),
    "O2.5": ("Buts", "Plus de 2,5 buts"), "U2.5": ("Buts", "Moins de 2,5 buts"),
    "O1.5": ("Buts", "Plus de 1,5 but"), "U3.5": ("Buts", "Moins de 3,5 buts"),
    "BTTS": ("Buts", "Les deux équipes marquent"),
    "NOBTTS": ("Buts", "Les deux équipes ne marquent pas"),
}


def selections_possibles(matchs, bookmaker, estimer_si_absent=False):
    """Sélections candidates avec probabilité FUSIONNÉE (marché + moteur) et
    cote réelle. Les libellés et champs attendus par la base sont ajoutés ici.
    estimer_si_absent : pour les jours lointains (TikTok), un match sans cotes
    publiées reçoit des cotes déduites du moteur, marquées « estimées »."""
    entrees = []
    for m in matchs:
        cotes = cotes_du_match(m["fixture_id"], bookmaker)
        time.sleep(0.4)
        estime = False
        if not cotes and estimer_si_absent:
            cotes, estime = EXT.cotes_estimees(m), True
        if not cotes:
            continue
        cotes = {k: v for k, v in cotes.items() if k == "N" or k.startswith("SE:") or v <= COTE_MAX_SELECTION}
        entrees.append({
            "estime": estime,
            **{k: m[k] for k in ("fixture_id", "ligue", "dom", "ext", "date_match",
                                 "heure", "logo_dom", "logo_ext")},
            "cotes": cotes,
            "moteur": {"1": m["p1"], "N": m["pN"], "2": m["p2"],
                       "O2.5": m["pO25"], "BTTS": m["btts"],
                       "O1.5": m.get("pO15"), "U3.5": m.get("pU35")},
            "promu": m.get("promu", False),
        })
    out = SEL.preparer_selections(entrees)
    estimes = {e["fixture_id"] for e in entrees if e.get("estime")}
    for s in out:
        s["estime"] = s["fixture_id"] in estimes
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
def construire(pool, nuit=False, sans=()):
    """Sert les catégories en rotation via selection_v2. Retourne des coupons
    au format attendu par enregistrer(). sans : catégories à ne pas construire
    (montante dont le palier précédent attend son résultat)."""
    if not pool:
        return []
    ordre = [c for c in (SEL.ORDRE_NUIT if nuit else SEL.ORDRE_JOUR) if c not in sans]
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
    if code.startswith("SE:"): return f"{bd}-{be}" == code[3:]
    if code[:1] in ("O", "U") and code[1:].replace(".", "", 1).isdigit():
        ligne = float(code[1:])
        return (bd + be) > ligne if code[0] == "O" else (bd + be) < ligne
    return None


def verifier():
    en_cours = sb("coupons?statut=eq.en_cours&select=id,jour,categorie")
    if not en_cours:
        return
    histo = pd.read_csv(F_HISTO)
    finis = histo[(histo.statut == "FT")].dropna(subset=["buts_dom", "buts_ext"])
    scores = {int(r.fixture_id): (int(r.buts_dom), int(r.buts_ext)) for _, r in finis.iterrows()}

    hier = (datetime.now(timezone.utc).date() - timedelta(days=1)).isoformat()
    for c in en_cours:
        if c["categorie"] in ("montante", "turbo") and not est_palier(c["id"]):
            continue          # coupon réservé (pas encore palier) : il sera adopté ou retiré, jamais vérifié seul
        sels = sb(f"coupon_selections?coupon_id=eq.{c['id']}&select=id,fixture_id,code,resultat,date_match,heure,score")
        if not sels:
            continue
        # résultat absent de l'historique local (passage hors cron, import en
        # retard) : on le demande directement à l'API pour les matchs déjà joués
        # 23/10/2026 : aussi les matchs du jour terminés (début + 1h55) → vérification possible dans la nuit
        manquants = [s["fixture_id"] for s in sels if s["fixture_id"] not in scores
                     and (str(s.get("date_match") or "")[:10] <= hier or termine_probablement(s))]
        for i in range(0, len(manquants), 20):
            try:
                rep = api("fixtures", {"ids": "-".join(str(x) for x in manquants[i:i + 20])})
            except Exception as e:
                print(f"   ⚠️ résultats API indisponibles : {e}")
                break
            for f in rep:
                g = f.get("goals") or {}
                if f["fixture"]["status"]["short"] in ("FT", "AET", "PEN") and g.get("home") is not None:
                    scores[int(f["fixture"]["id"])] = (int(g["home"]), int(g["away"]))
        tous_joues, perdu, un_gagne = True, False, False
        for s in sels:
            if s["fixture_id"] not in scores:
                tous_joues = False
                continue
            if s["resultat"] is None:
                ok = gagnee(s["code"], *scores[s["fixture_id"]])
                sb(f"coupon_selections?id=eq.{s['id']}", "PATCH",
                   {"resultat": "gagne" if ok else "perdu"})
                # score final, pour la page Preuves du panneau admin (facultatif :
                # un refus éventuel n'empêche jamais la vérification)
                try:
                    bd_, be_ = scores[s["fixture_id"]]
                    sb(f"coupon_selections?id=eq.{s['id']}", "PATCH", {"score": f"{bd_}-{be_}"})
                except Exception:
                    pass
            else:
                ok = s["resultat"] == "gagne"
                if not s.get("score"):
                    try:
                        bd_, be_ = scores[s["fixture_id"]]
                        sb(f"coupon_selections?id=eq.{s['id']}", "PATCH", {"score": f"{bd_}-{be_}"})
                    except Exception:
                        pass
            if ok:
                un_gagne = True
            else:
                perdu = True
        if c["categorie"] == "score":
            # score exact : gagné dès qu'un des scores proposés tombe
            if not tous_joues:
                continue
            statut = "gagne" if un_gagne else "perdu"
            sb(f"coupons?id=eq.{c['id']}", "PATCH", {"statut": statut})
            print(f"   Coupon score exact du {c['jour']} → {statut}")
            continue
        if perdu or tous_joues:
            statut = "perdu" if perdu else "gagne"
            sb(f"coupons?id=eq.{c['id']}", "PATCH", {"statut": statut})
            print(f"   Coupon {c['categorie']} du {c['jour']} → {statut}")
            if c["categorie"] in ("montante", "turbo"):
                sb(f"montante?coupon_id=eq.{c['id']}", "PATCH", {"statut": statut})


def completer_scores(jours=21):
    """Score final des matchs déjà tranchés qui n'en ont pas encore (coupons des
    3 dernières semaines) : affiché dans « Validés hier » et dans les Preuves."""
    depuis = (datetime.now(timezone.utc).date() - timedelta(days=jours)).isoformat()
    sels = sb(f"coupon_selections?score=is.null&resultat=not.is.null&date_match=gte.{depuis}"
              f"&select=id,fixture_id&limit=1000")
    if not isinstance(sels, list) or not sels:
        return
    scores = {}
    try:
        histo = pd.read_csv(F_HISTO)
        finis = histo[(histo.statut == "FT")].dropna(subset=["buts_dom", "buts_ext"])
        scores = {int(r.fixture_id): (int(r.buts_dom), int(r.buts_ext)) for _, r in finis.iterrows()}
    except Exception:
        pass
    manquants = sorted({int(x["fixture_id"]) for x in sels if x.get("fixture_id") and int(x["fixture_id"]) not in scores})
    for i in range(0, len(manquants), 20):
        try:
            rep = api("fixtures", {"ids": "-".join(str(x) for x in manquants[i:i + 20])})
        except Exception:
            break
        for f in rep:
            g = f.get("goals") or {}
            if g.get("home") is not None:
                scores[int(f["fixture"]["id"])] = (int(g["home"]), int(g["away"]))
    n = 0
    for x in sels:
        sc = scores.get(int(x["fixture_id"] or 0))
        if sc:
            sb(f"coupon_selections?id=eq.{x['id']}", "PATCH", {"score": f"{sc[0]}-{sc[1]}"})
            n += 1
    if n:
        print(f"   ⚽ scores finaux ajoutés : {n}")


# ==================================================================
# 5. Montante
# ==================================================================
def debut_match(s):
    """Coup d'envoi (UTC) d'une sélection, ou None."""
    try:
        h = str(s.get("heure") or "00:00")[:5]
        return datetime.fromisoformat(f"{str(s['date_match'])[:10]}T{h}:00+00:00")
    except Exception:
        return None


def termine_probablement(s):
    d = debut_match(s)
    return d is not None and datetime.now(timezone.utc) >= d + timedelta(minutes=115)


def est_palier(coupon_id):
    lien = sb(f"montante?coupon_id=eq.{coupon_id}&select=palier")
    return isinstance(lien, list) and bool(lien)


# ----- RÉSERVATION DES MONTANTES (23/10/2026, demande de Babs) -----
# Les montantes passent TOUJOURS en premier. Quand le coupon du lendemain est
# préparé avant de connaître le résultat du palier en cours, le moteur choisit
# quand même d'abord le coupon Montante / Turbo (meilleurs matchs, meilleures
# options) et le RÉSERVE : il est enregistré sans palier, donc invisible dans
# l'app. Les autres coupons sont construits avec les matchs restants.
# Dès que le palier en cours est vérifié (passages de nuit), le coupon réservé
# est ADOPTÉ : palier suivant si validé, palier 1 d'une nouvelle série sinon.
def reserves(typ, jour):
    """Coupons Montante/Turbo du jour encore en cours et non rattachés à un palier."""
    l = sb(f"coupons?jour=eq.{jour}&categorie=eq.{typ}&statut=eq.en_cours&select=id,cote_totale")
    return [c for c in (l if isinstance(l, list) else []) if not est_palier(c["id"])]


def deja_commence(coupon_id):
    sels = sb(f"coupon_selections?coupon_id=eq.{coupon_id}&select=date_match,heure")
    now = datetime.now(timezone.utc)
    return any((debut_match(s) or now) <= now for s in (sels if isinstance(sels, list) else []))


def adopter_reserves():
    """Transforme les coupons réservés en vrais paliers dès que c'est possible."""
    auj = datetime.now(timezone.utc).date()
    for jour in (auj.isoformat(), (auj + timedelta(days=1)).isoformat()):
        for typ in PALIERS:
            res = reserves(typ, jour)
            if not res:
                continue
            for c in res[1:]:                                  # un seul coupon par montante et par jour
                supprimer_coupon(c["id"])
            c = res[0]
            if deja_commence(c["id"]):
                supprimer_coupon(c["id"])
                print(f"   ✗ {NOMS_MONTANTE[typ]} : coupon réservé du {jour} retiré (un match a déjà commencé)")
                continue
            if not montante_prete(typ, jour):
                print(f"   ⏸ {NOMS_MONTANTE[typ]} : coupon du {jour} réservé, en attente du résultat du palier en cours")
                continue
            if avancer_montante(c["id"], float(c["cote_totale"]), jour, typ):
                print(f"   ✅ {NOMS_MONTANTE[typ]} : coupon réservé du {jour} publié comme palier")


def montantes_presentes(jour):
    """Types de montante ayant déjà un coupon pour ce jour (palier ou réservé) et leurs matchs."""
    types, fx = set(), set()
    for typ in PALIERS:
        l = sb(f"coupons?jour=eq.{jour}&categorie=eq.{typ}&select=id,statut")
        for c in (l if isinstance(l, list) else []):
            if c.get("statut") == "en_cours" and not est_palier(c["id"]) and deja_commence(c["id"]):
                continue                                       # réservé périmé : sera retiré
            types.add(typ)
            s = sb(f"coupon_selections?coupon_id=eq.{c['id']}&select=fixture_id")
            fx |= {x["fixture_id"] for x in (s if isinstance(s, list) else [])}
    return types, fx


def debloquer_montante(typ="montante"):
    """Un palier ne doit jamais rester bloqué « en cours » : sinon la montante
    ne repart plus. Si le coupon du palier a déjà été tranché, on recopie son
    statut ; s'il est introuvable ou toujours pas vérifiable 2 jours après
    (match reporté, annulé…), le palier est annulé et sera rejoué à la même mise."""
    d = sb(f"montante?type=eq.{typ}&select=serie,palier,coupon_id,statut,jour&order=serie.desc,palier.desc&limit=1")
    if not isinstance(d, list) or not d or d[0]["statut"] != "en_cours":
        return
    d = d[0]
    cle = f"montante?type=eq.{typ}&serie=eq.{d['serie']}&palier=eq.{d['palier']}"
    c = sb(f"coupons?id=eq.{d['coupon_id']}&select=statut,jour") if d.get("coupon_id") else []
    c = c[0] if isinstance(c, list) and c else None
    if c and c["statut"] in ("gagne", "perdu"):
        sb(cle, "PATCH", {"statut": c["statut"]})
        print(f"   {NOMS_MONTANTE[typ]} série {d['serie']} · palier {d['palier']} → {c['statut']} (rattrapage)")
        return
    try:
        age = (datetime.now(timezone.utc).date() - datetime.fromisoformat(str(d["jour"])[:10]).date()).days
    except Exception:
        age = 0
    if (c is None and age >= 1) or age >= 2:
        sb(cle, "DELETE")
        print(f"   {NOMS_MONTANTE[typ]} série {d['serie']} · palier {d['palier']} du {d['jour']} annulé "
              f"({'coupon introuvable' if c is None else 'non vérifiable'}) — il sera rejoué")


def montante_prete(typ, jour):
    """RÈGLE (13/10/2026) : le palier suivant d'une montante n'est JAMAIS publié
    avant que le palier précédent soit vérifié. Sinon un coupon part « dans le
    vide » (sans palier) et bloque la journée.
    Retourne True si on peut construire le coupon de ce type pour ce jour."""
    try:
        debloquer_montante(typ)
    except Exception:
        pass
    d = sb(f"montante?type=eq.{typ}&select=serie,palier,statut,jour&order=serie.desc,palier.desc&limit=1")
    if not isinstance(d, list) or not d:
        return True                                   # toute première série
    d = d[0]
    if d["statut"] != "en_cours":
        return True                                   # palier précédent tranché → on enchaîne
    if str(d["jour"])[:10] == jour:
        return True                                   # le palier de ce jour existe déjà (conservé)
    print(f"   ⏳ {NOMS_MONTANTE[typ]} : palier {d['palier']} du {str(d['jour'])[:10]} pas encore vérifié "
          f"→ palier suivant publié dès sa validation")
    return False


def avancer_montante(coupon_id, cote, jour, typ="montante"):
    debloquer_montante(typ)
    paliers = sb(f"montante?type=eq.{typ}&select=serie,palier,mise,gain_vise,statut&order=serie.desc,palier.desc&limit=1")
    if paliers:
        d = paliers[0]
        if d["statut"] == "gagne":
            if d["palier"] >= nb_paliers(typ, d["serie"]):
                serie, palier, mise = d["serie"] + 1, 1, MISE_DEPART   # série bouclée → nouvelle série
            else:
                serie, palier, mise = d["serie"], d["palier"] + 1, float(d["gain_vise"])
        elif d["statut"] == "perdu":
            serie, palier, mise = d["serie"] + 1, 1, MISE_DEPART
        else:
            return False   # palier encore en cours : on n'en ouvre pas un second
    else:
        serie, palier, mise = 1, 1, MISE_DEPART

    sb("montante", "POST", {
        "type": typ, "serie": serie, "palier": palier, "coupon_id": coupon_id,
        "mise": mise, "gain_vise": round(mise * cote, 2), "jour": jour,
    }, prefer="return=minimal")
    print(f"   {NOMS_MONTANTE[typ]} série {serie} · palier {palier}/{nb_paliers(typ, serie)} : {int(mise)} F → {int(mise*cote)} F")
    return True


# ==================================================================
# 6. Enregistrement
# ==================================================================
CATEGORIES_REMPLACABLES = {"tiktok"}   # seules catégories reconstruites à chaque passage


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


def enregistrer(coupons, jour, nuit=False, reserve=()):
    from datetime import date as _date
    lendemain = (datetime.fromisoformat(jour).date() + timedelta(days=1)).isoformat()
    autorises = {jour, lendemain} if nuit else {jour}

    # RÈGLE (01/10/2026) : un coupon publié ne bouge plus. Des gens misent dessus
    # dès sa publication — montantes en tête. Une relance du cron ne fait donc
    # que COMPLÉTER les catégories encore vides pour ce jour-là.
    #   · exception : les combinés TikTok provisoires (J+3 … J+6), qui suivent
    #     leurs propres règles de gel (generer_tiktok)
    #   · --forcer : régénération volontaire, jamais pour les montantes
    forcer = "--forcer" in sys.argv
    cats = {c["categorie"] for c in coupons}
    gardees = set()
    for cat in cats:
        anciens = sb(f"coupons?jour=eq.{jour}&categorie=eq.{cat}&select=id,statut")
        anciens = anciens if isinstance(anciens, list) else []
        if cat in ("montante", "turbo") and anciens:
            # coupon de montante publié trop tôt (palier précédent pas encore
            # vérifié) : il n'est rattaché à aucun palier, donc invisible dans
            # l'app → on le retire pour publier le vrai palier
            # coupon RÉSERVÉ (sans palier) : gardé tant qu'aucun de ses matchs n'a commencé
            orphelins = []
            for a in anciens:
                if a.get("statut") == "en_cours" and not est_palier(a["id"]) and deja_commence(a["id"]):
                    orphelins.append(a)
            for a in orphelins:
                if supprimer_coupon(a["id"]):
                    print(f"   ↻ {cat} : coupon réservé du {jour} périmé (match commencé), retiré")
            anciens = [a for a in anciens if a not in orphelins]
        remplacable = cat in CATEGORIES_REMPLACABLES or (forcer and cat not in ("montante", "turbo"))
        if anciens and not remplacable:
            gardees.add(cat)
            print(f"   🔒 {cat} : {len(anciens)} coupon(s) déjà publié(s) le {jour}, conservé(s)")
            continue
        n = 0
        for a in anciens:
            if a.get("statut") == "en_cours":
                n += supprimer_coupon(a["id"])
        if n:
            print(f"   ↻ {cat} : {n} ancien(s) coupon(s) remplacé(s)")
    coupons = [c for c in coupons if c["categorie"] not in gardees]
    cats = cats - gardees
    if not coupons:
        return

    # numéros déjà pris dans la journée (coupons gagnés/perdus conservés) :
    # un coupon terminé garde son numéro, les nouveaux prennent les suivants
    pris = {}
    for cat in cats:
        existants = sb(f"coupons?jour=eq.{jour}&categorie=eq.{cat}&select=numero")
        pris[cat] = {e["numero"] for e in (existants if isinstance(existants, list) else [])}
    for c in coupons:
        n = 1
        while n in pris[c["categorie"]]:
            n += 1
        c["numero"] = n
        pris[c["categorie"]].add(n)

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
        if c.get("note"):
            corps["note"] = c["note"]
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

        if cle in ("montante", "turbo"):
            if cle in reserve:
                print(f"   🔒 {NOMS_MONTANTE[cle]} : coupon du {jour} RÉSERVÉ (publié dès la vérification du palier en cours)")
            else:
                avancer_montante(cid, c["cote_totale"], jour, cle)   # un seul palier ouvert à la fois par montante


# ==================================================================
def main():
    if not (CLE_API and URL_SB and CLE_SB):
        sys.exit("❌ API_FOOTBALL_KEY / SUPABASE_URL / SUPABASE_SERVICE_KEY manquantes")

    print("→ Vérification des coupons précédents")
    verifier()
    try:
        completer_scores()
    except Exception as e:
        print(f"   ⚠️ scores finaux : {e}")
    for typ in PALIERS:
        try:
            debloquer_montante(typ)
        except Exception as e:
            print(f"   ⚠️ {typ} : {e}")
    try:
        verifier_live(pd.read_csv(F_HISTO))
    except Exception as e:
        print(f"   ⚠️ vérification live : {e}")
    try:
        adopter_reserves()
    except Exception as e:
        print(f"   ⚠️ adoption des coupons réservés : {e}")

    if "--montantes" in sys.argv:
        # passage de rattrapage (9h, 13h) : uniquement les montantes du jour
        bookmaker = id_bookmaker()
        if bookmaker:
            generer_montantes(bookmaker)
        print("✓ passage montantes terminé")
        return

    if "--verifier-seulement" in sys.argv:
        print("✓ vérification terminée (pas de nouveaux coupons)")
        return

    bookmaker = id_bookmaker()
    if not bookmaker:
        print("   ❌ aucun bookmaker disponible")
        return

    if "--demain" in sys.argv:
        # passage du soir (18h) : on met à jour les coupons de demain
        generer_journee(True, bookmaker)
    else:
        # passage du matin (5h) : coupons du jour ET de demain, pour que
        # l'onglet « Demain » soit rempli toute la journée (le passage de 18h
        # les remet à jour avec les dernières cotes et compositions)
        generer_journee(False, bookmaker)
        if "--sans-demain" not in sys.argv:
            generer_journee(True, bookmaker)

    # ----- combinés TikTok sur 7 jours glissants -----
    if "--sans-tiktok" not in sys.argv:
        generer_tiktok(bookmaker)
    print("✓ génération terminée")


def generer_montantes(bookmaker):
    """Rattrapage : si le palier d'hier a été validé après le passage de 5h,
    publie tout de suite le palier du jour (Montante et/ou Turbo). Ne touche à
    aucun autre coupon."""
    jour = datetime.now(timezone.utc).date().isoformat()
    print("→ Coupons réservés : adoption")
    adopter_reserves()
    print(f"→ Montantes du {jour} (rattrapage)")
    besoin = []
    for typ in PALIERS:
        deja = sb(f"montante?type=eq.{typ}&jour=eq.{jour}&select=palier")
        if isinstance(deja, list) and deja:
            print(f"   ✓ {NOMS_MONTANTE[typ]} : palier du jour déjà publié")
            continue
        if reserves(typ, jour):
            continue                                   # un coupon réservé attend déjà ce palier
        if montante_prete(typ, jour):
            besoin.append(typ)
    if not besoin:
        print("   rien à publier")
        return

    matchs = candidats(False)                      # matchs du jour pas encore commencés
    if len(matchs) < 3:
        print("   (trop peu de matchs restants aujourd'hui)")
        return
    pool = selections_possibles(matchs, bookmaker)

    # on évite les matchs déjà utilisés par les coupons publiés ce matin
    # (règle : les matchs d'une montante ne sont repris par aucun autre coupon)
    ids = sb(f"coupons?jour=eq.{jour}&select=id")
    pris = set()
    if isinstance(ids, list) and ids:
        liste = ",".join(str(x["id"]) for x in ids)
        sels = sb(f"coupon_selections?coupon_id=in.({liste})&select=fixture_id")
        pris = {x["fixture_id"] for x in (sels if isinstance(sels, list) else [])}
    libre = [x for x in pool if x["fixture_id"] not in pris]

    # 23/10/2026 : plus JAMAIS de match déjà joué par un autre coupon (cohérence) ;
    # pas assez de matchs libres → pas de palier aujourd'hui, la série attend
    coupons = SEL.construire_coupons(libre, ordre=besoin, journal=print)
    coupons = [{"categorie": c["categorie"], "numero": c["numero"],
                "selections": c["selections"], "cote_totale": c["cote_totale"]} for c in coupons]
    if coupons:
        enregistrer(coupons, jour)
    else:
        print("   ⚠️ aucun palier constructible avec les matchs restants")


def generer_journee(demain, bookmaker):
    """Tous les coupons d'une journée (jour même ou lendemain)."""
    jour = (datetime.now(timezone.utc).date() + timedelta(days=1 if demain else 0)).isoformat()
    print(f"→ Analyse des matchs du {jour} (moteur enrichi : absences, fatigue, xG, classement)"
          + (" — préparation de demain" if demain else ""))
    # prévisions pour LIVE COUPON : tous les matchs de la journée visée, y compris
    # ceux déjà commencés (une relance le soir doit les couvrir)
    cible = datetime.now(timezone.utc).date() + timedelta(days=1 if demain else 0)
    publier_previsions(candidats(jour_cible=cible, enrichir=False, statuts=STATUTS_LIVE, depuis_debut=True))

    matchs = candidats(demain)
    if len(matchs) < 5:
        print(f"   (trop peu de matchs le {jour} : aucun coupon)")
        return

    pool = selections_possibles(matchs, bookmaker)
    if len(pool) < 5:
        print("   (pas assez de cotes disponibles)")
        return

    print("→ Construction des coupons (Montante et Turbo servies en premier)")
    en_attente = {typ for typ in PALIERS if not montante_prete(typ, jour)}
    presentes, pris_mt = montantes_presentes(jour)            # déjà publiées ou réservées : on garde, matchs exclus
    if presentes:
        pool = [x for x in pool if x["fixture_id"] not in pris_mt]
    coupons = construire(pool, sans=presentes)

    # ----- coupons de la nuit (Amériques) -----
    print(f"→ Coupons de la nuit du {jour} (22h → 7h)")
    coupons_nuit = []
    matchs_nuit = candidats(demain, nuit=True)
    if matchs_nuit:
        pool_nuit = selections_possibles(matchs_nuit, bookmaker)
        if pool_nuit:
            coupons_nuit = construire(pool_nuit, nuit=True)
    else:
        print("   (aucun match cette nuit)")

    # ----- prévisions live : matchs de la nuit -----
    publier_previsions(matchs_nuit or [])

    # ----- score exact : SECTION RETIRÉE le 21/10/2026 (décision Babs, résultats jugés mauvais) -----
    # Plus aucun coupon « score » n'est publié. Les anciens restent en base et sont toujours vérifiés.
    # Pour la réactiver : remettre SCORE_EXACT_ACTIF = True.
    if SCORE_EXACT_ACTIF:
        print(f"→ Score exact du {jour}")
        coupons += EXT.construire_scores_exacts(matchs, COTES_BRUTES)

    ordre = {"sure": 0, "confiance": 1, "fun": 2, "grosses": 3, "nuit": 4, "montante": 5, "turbo": 6, "score": 7}
    if coupons:
        coupons.sort(key=lambda c: (ordre.get(c["categorie"], 9), c["numero"]))
        enregistrer(coupons, jour, reserve=en_attente)
    if coupons_nuit:
        enregistrer(coupons_nuit, jour, nuit=True)
    print(f"✓ coupons du {jour} enregistrés")


def publier_previsions(matchs):
    """Enregistre les probabilités du moteur pour chaque match analysé : c'est
    le point de départ de LIVE COUPON (fonction serveur « live »)."""
    if not matchs:
        return
    lignes = [{
        "fixture_id": int(m["fixture_id"]), "jour": str(m["date_match"]),
        "ligue": m.get("ligue"), "ligue_id": m.get("ligue_id"),
        "dom": m["dom"], "ext": m["ext"], "heure": m.get("heure"),
        "logo_dom": m.get("logo_dom"), "logo_ext": m.get("logo_ext"),
        "p1": round(m["p1"], 4), "pn": round(m["pN"], 4), "p2": round(m["p2"], 4),
        "po25": round(m["pO25"], 4), "btts": round(m["btts"], 4),
        "maj": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    } for m in matchs]
    r = sb("previsions_jour", "POST", lignes, prefer="resolution=merge-duplicates")
    print(f"   📡 prévisions live : {len(lignes)} match(s) publiés" if r is not None else "   ⚠️ prévisions live non publiées")


def _scores_api(fids):
    """Score à la pause et score final de matchs, lus directement chez API-Football
    (20 matchs par appel). → {fixture_id: (statut, mt_dom, mt_ext, fin_dom, fin_ext)}"""
    out = {}
    fids = sorted(set(fids))
    for i in range(0, len(fids), 20):
        try:
            for f in api("fixtures", {"ids": "-".join(str(x) for x in fids[i:i + 20])}):
                sc = f.get("score") or {}
                mt, ft = sc.get("halftime") or {}, sc.get("fulltime") or {}
                out[int(f["fixture"]["id"])] = (f["fixture"]["status"]["short"], mt.get("home"), mt.get("away"),
                                                ft.get("home") if ft.get("home") is not None else (f.get("goals") or {}).get("home"),
                                                ft.get("away") if ft.get("away") is not None else (f.get("goals") or {}).get("away"))
        except Exception as e:
            print(f"   ⚠️ scores mi-temps : {e}")
    return out


def gagnee_mi_temps(code, info):
    """Paris live sur une mi-temps : H1_O0.5, H1_U1.5, H2_O0.5… → True/False, ou None si pas encore joué."""
    statut, md, me, fd, fe = info
    mt, ligne_txt = code[1], code[3:]
    sens, ligne = ligne_txt[0], float(ligne_txt[1:])
    if md is None or me is None:
        return None
    if mt == "1":
        if statut in ("NS", "1H", "TBD"):
            return None
        buts = md + me
    else:
        if statut not in ("FT", "AET", "PEN") or fd is None or fe is None:
            return None
        buts = (fd + fe) - (md + me)
    return buts > ligne if sens == "O" else buts < ligne


def verifier_live(histo):
    """Vérifie les combinés LIVE générés par les utilisateurs, une fois leurs
    matchs terminés (au plus tôt 2 h 30 après le clic). Les paris « mi-temps »
    (codes H1_… / H2_…) sont vérifiés avec le score à la pause."""
    limite = (datetime.now(timezone.utc) - timedelta(hours=2, minutes=30)).strftime("%Y-%m-%dT%H:%M:%SZ")
    en_cours = sb(f"coupons_live?statut=eq.en_cours&cree_le=lte.{limite}&select=id,selections")
    if not isinstance(en_cours, list) or not en_cours:
        return
    fini = histo[histo.statut == "FT"].dropna(subset=["buts_dom", "buts_ext"]).set_index("fixture_id")
    fids_mt = [int(s["fixture_id"]) for c in en_cours for s in (c.get("selections") or [])
               if str(s.get("code", "")).startswith(("H1_", "H2_"))]
    scores_mt = _scores_api(fids_mt) if fids_mt else {}
    n = 0
    for c in en_cours:
        sels = c.get("selections") or []
        tous_joues, perdu, maj = True, False, []
        for s in sels:
            fid, code = int(s["fixture_id"]), str(s["code"])
            if code.startswith(("H1_", "H2_")):
                ok = gagnee_mi_temps(code, scores_mt[fid]) if fid in scores_mt else None
            elif fid in fini.index:
                m = fini.loc[fid]
                if isinstance(m, pd.DataFrame):
                    m = m.iloc[0]
                ok = gagnee(code, int(m.buts_dom), int(m.buts_ext))
            else:
                ok = None
            if ok is None:
                tous_joues = False; maj.append(s); continue
            s = {**s, "resultat": "gagne" if ok else "perdu"}
            if not ok:
                perdu = True
            maj.append(s)
        if perdu or tous_joues:
            statut = "perdu" if perdu else "gagne"
            sb(f"coupons_live?id=eq.{c['id']}", "PATCH",
               {"statut": statut, "selections": maj,
                "verifie_le": datetime.now(timezone.utc).isoformat(timespec="seconds")})
            n += 1
    if n:
        print(f"   🔴 combinés live vérifiés : {n}")


def generer_tiktok(bookmaker):
    """3 combinés par jour de J+1 à J+6. J+1 : jamais touché. J+2 : dernière
    reconstruction puis figé. J+3 … J+6 : provisoires, refaits chaque jour."""
    print("→ Combinés TikTok (7 jours glissants)")
    auj = datetime.now(timezone.utc).date()
    for d in range(1, EXT.TIKTOK_JOURS + 1):
        jour = auj + timedelta(days=d)
        js = jour.isoformat()
        existants = sb(f"coupons?jour=eq.{js}&categorie=eq.tiktok&statut=eq.en_cours&select=id,note")
        existants = existants if isinstance(existants, list) else []
        if d < EXT.TIKTOK_FIGE_A and existants:
            continue                                    # J+1 : on ne touche plus
        if d == EXT.TIKTOK_FIGE_A and existants and all("figé" in (e.get("note") or "") for e in existants):
            continue                                    # déjà figé
        matchs = candidats(jour_cible=jour, enrichir=(d <= 2))
        if len(matchs) < EXT.TIKTOK_MIN_LEGS:
            print(f"   J+{d} ({js}) : {len(matchs)} match(s), pas assez pour un combiné")
            continue
        pool = selections_possibles(matchs, bookmaker, estimer_si_absent=True)
        coupons = EXT.construire_tiktok(pool)
        if not coupons:
            continue
        etat = "figé" if d <= EXT.TIKTOK_FIGE_A else "provisoire"
        for c in coupons:
            c["note"] = f"{etat} · {c['note']}"
        print(f"   J+{d} ({js}) : {len(coupons)} combiné(s) {etat}")
        enregistrer(coupons, js)


if __name__ == "__main__":
    main()
