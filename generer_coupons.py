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
ECART_MAX_MARCHE = 1.85      # écart maximal toléré entre le moteur et le marché
TAILLE_MINI = {"grosses": 4, "fun": 3, "confiance": 3, "sure": 2, "nuit": 2, "montante": 1}

BOOKMAKERS = ["1xbet", "melbet", "betwinner", "1win", "bet365", "pinnacle"]

# (clé, cote visée, plancher, plafond, taille visée, matchs maxi, coupons/jour)
# ORDRE = ordre de construction : les catégories risquées servent EN PREMIER,
# sinon les coupons sûrs raflent toutes les sélections à cote moyenne et il ne
# reste que des cotes à 1,20 pour les grosses cotes.
# (clé, cote visée, plancher, plafond, taille visée, matchs maxi, coupons/jour)
CATEGORIES_NUIT = [
    ("nuit",        3.2,   2.0,   5.0,  2,  4, 3),
]

CATEGORIES = [
    ("grosses",    60.0,  20.0, 150.0,  6,  8, 2),
    ("fun",        15.0,   9.0,  22.0,  5,  6, 2),
    ("confiance",   6.0,   3.0,  10.0,  4,  5, 3),
    ("sure",        2.2,   1.7,   2.6,  3,  3, 3),
    ("montante",    1.7,   1.4,   2.0,  2,  3, 1),
]

# Issues 1X2 couvertes par chaque code (H = dom, D = nul, A = ext)
ISSUES = {
    "1": {"H"}, "2": {"A"}, "1X": {"H", "D"}, "X2": {"D", "A"}, "12": {"H", "A"},
}
# Familles exclusives : deux codes d'une même famille se contredisent
FAMILLES = {"O2.5": "buts", "U2.5": "buts", "BTTS": "btts", "NOBTTS": "btts"}


def compatibles(code_a, code_b):
    """Deux sélections sur LE MÊME match peuvent-elles coexister sans se contredire ?"""
    if code_a == code_b:
        return True
    if code_a in ISSUES and code_b in ISSUES:
        # il faut au moins une issue commune (ex. « 1 » et « 1X » : oui ; « 1 » et « X2 » : non)
        return bool(ISSUES[code_a] & ISSUES[code_b])
    fa, fb = FAMILLES.get(code_a), FAMILLES.get(code_b)
    if fa and fb and fa == fb:
        return False          # Plus de 2,5 vs Moins de 2,5 → contradiction
    return True               # marchés indépendants (résultat vs buts) : compatibles


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
    for lid, nom in (LIGUES_NUIT if nuit else LIGUES_COUPONS).items():
        viviers = [lid] + [d2 for d2, d1 in D2_VERS_D1.items() if d1 == lid]
        passe = histo[histo.ligue_id.isin(viviers) & (histo.statut == "FT")].dropna(
            subset=["buts_dom", "buts_ext"])
        avenir = histo[(histo.ligue_id == lid) & (histo.statut == "NS") &
                       (histo.date >= debut) & (histo.date < fin)]
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
            # le moteur renvoie des pourcentages (46) : on ramène tout sur 0-1
            ech = lambda v: (float(v) / 100) if float(v) > 1 else float(v)
            lignes.append({
                "fixture_id": int(f.fixture_id), "ligue": nom,
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
            if cote > COTE_MAX_SELECTION:      # pas d'outsider isolé dans un combiné
                continue
            # si le moteur s'écarte trop du marché, c'est LUI qui se trompe :
            # un bookmaker à 9,30 (11 %) contre un modèle à 40 %, c'est une anomalie
            if p > ECART_MAX_MARCHE * (1 / cote):
                continue
            marche, gabarit = LIBELLES[code]
            out.append({
                **{k: m[k] for k in ("fixture_id", "ligue", "dom", "ext", "date_match",
                                     "heure", "logo_dom", "logo_ext")},
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
def batir(pool, cible, plancher, plafond, taille, max_matchs, deja_pris, verdicts, usages, mini=1):
    """Empile des sélections jusqu'à approcher la cote visée.
    Deux garde-fous : jamais de sélection qui contredit un autre coupon du jour,
    et on privilégie les sélections où le moteur voit un écart avec la cote."""
    # on écarte tout ce qui contredirait un choix déjà fait sur le même match
    dispo = []
    for s in pool:
        deja = verdicts.get(s["fixture_id"], [])
        if any(not compatibles(s["code"], c) for c in deja):
            continue
        if usages.get((s["fixture_id"], s["code"]), 0) >= 1:   # jamais deux fois la même sélection
            continue
        dispo.append(s)
    if not dispo:
        return None, 0.0

    # cote moyenne nécessaire pour atteindre la cible avec la taille visée
    moy_visee = cible ** (1 / max(1, taille))
    # « valeur » = écart entre ce que dit le moteur et ce que paie le bookmaker
    for s in dispo:
        s["valeur"] = s["proba"] * s["cote"] - 1
    # plancher de cote : plus bas pour les coupons sûrs, qui vivent de petites cotes
    plancher_cote = 1.15 if cible <= 2.5 else 1.20
    dispo = [s for s in dispo if s["cote"] >= plancher_cote]
    if not dispo:
        return None, 0.0

    # on ne retient d'abord que les cotes dans la zone utile pour cette cible
    zone = [s for s in dispo if 0.7 * moy_visee <= s["cote"] <= 1.7 * moy_visee]
    candidats_tries = zone or dispo
    # priorité du marché, puis valeur réelle, puis probabilité, puis fraîcheur
    candidats_tries.sort(key=lambda s: (s["priorite"], -s["valeur"], -s["proba"]))

    def equilibre(cotes, nouvelle):
        """Un coupon reste lisible si ses cotes restent du même ordre :
        la plus forte ne dépasse pas 2,2 fois la plus faible."""
        toutes = cotes + [nouvelle]
        return max(toutes) <= 2.2 * min(toutes)

    choisies, matchs_pris, total = [], set(), 1.0
    for s in candidats_tries:
        if len(choisies) >= max_matchs or total >= cible:
            break
        if s["fixture_id"] in matchs_pris:
            continue
        if total * s["cote"] > plafond:
            continue
        if not equilibre([x["cote"] for x in choisies], s["cote"]):
            continue
        choisies.append(s)
        matchs_pris.add(s["fixture_id"])
        total *= s["cote"]

    # si la cote reste sous le plancher, on complète avec les meilleures cotes
    # restantes (les plus rémunératrices d'abord) plutôt que de renoncer
    if total < plancher and len(choisies) < max_matchs:
        reste = [s for s in dispo if s["fixture_id"] not in matchs_pris]
        reste.sort(key=lambda s: (-s["cote"], -s["valeur"]))
        for s in reste:
            if len(choisies) >= max_matchs or total >= cible:
                break
            if total * s["cote"] > plafond:
                continue
            if not equilibre([x["cote"] for x in choisies], s["cote"]):
                continue
            choisies.append(s)
            matchs_pris.add(s["fixture_id"])
            total *= s["cote"]

    if not choisies or total < plancher or len(choisies) < mini:
        return None, 0.0
    return choisies, round(total, 2)


def construire(pool, categories=None):
    """Sert les catégories EN ROTATION : chacune obtient son premier coupon
    avant qu'une autre en reçoive un deuxième. Sans ça, les catégories
    servies en premier épuisent le vivier et les suivantes repartent vides."""
    cats = categories or CATEGORIES
    coupons, deja_pris = [], set()
    verdicts, usages = {}, {}

    # avec peu de matchs, on publie moins de coupons plutôt que du remplissage
    nb_matchs = len({s["fixture_id"] for s in pool})
    plafond_par_cat = max(1, nb_matchs // 4)

    tours = max(c[6] for c in cats)
    epuisees = set()
    for numero in range(1, tours + 1):
        for cle, cible, plancher, plafond, taille, nmax, combien in cats:
            if cle in epuisees or numero > min(combien, plafond_par_cat):
                continue
            sel, total = batir(pool, cible, plancher, plafond, taille, nmax,
                               deja_pris, verdicts, usages, TAILLE_MINI.get(cle, 1))
            if not sel:
                epuisees.add(cle)
                if numero == 1:
                    libres = [s for s in pool
                              if usages.get((s["fixture_id"], s["code"]), 0) == 0
                              and all(compatibles(s["code"], c) for c in verdicts.get(s["fixture_id"], []))]
                    print(f"   ⚠️ {cle} : aucun coupon possible "
                          f"({len(libres)} sélection(s) libres, cible {cible})")
                continue

            coupons.append({"categorie": cle, "numero": numero,
                            "selections": sel, "cote_totale": total})
            for s in sel:
                deja_pris.add((s["fixture_id"], s["code"]))
                verdicts.setdefault(s["fixture_id"], [])
                if s["code"] not in verdicts[s["fixture_id"]]:
                    verdicts[s["fixture_id"]].append(s["code"])
                usages[(s["fixture_id"], s["code"])] = usages.get((s["fixture_id"], s["code"]), 0) + 1
            print(f"   ✅ {cle} #{numero} : {len(sel)} match(s), cote {total}")

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
def enregistrer(coupons, jour, nuit=False):
    from datetime import date as _date
    lendemain = (datetime.fromisoformat(jour).date() + timedelta(days=1)).isoformat()
    autorises = {jour, lendemain} if nuit else {jour}

    # on efface d'abord les coupons du jour encore en cours : une nouvelle
    # exécution doit toujours refléter la logique la plus récente
    cats = {c["categorie"] for c in coupons}
    for cat in cats:
        anciens = sb(f"coupons?jour=eq.{jour}&categorie=eq.{cat}&statut=eq.en_cours&select=id")
        for a in (anciens if isinstance(anciens, list) else []):
            sb(f"coupon_selections?coupon_id=eq.{a['id']}", "DELETE")
            sb(f"montante?coupon_id=eq.{a['id']}", "DELETE")
            sb(f"coupons?id=eq.{a['id']}", "DELETE")
        if anciens:
            print(f"   ↻ {cat} : {len(anciens)} ancien(s) coupon(s) remplacé(s)")

    for c in coupons:
        # garde-fou : aucune sélection en dehors de la journée visée
        hors = [s for s in c["selections"] if s["date_match"] not in autorises]
        if hors:
            print(f"   ❌ {c['categorie']} #{c['numero']} : {len(hors)} match(s) hors du "
                  f"{jour} → coupon annulé")
            continue
        cle, numero = c["categorie"], c["numero"]
        cree = sb("coupons", "POST", {
            "jour": jour, "categorie": cle, "numero": numero,
            "cote_totale": c["cote_totale"], "nb_matchs": len(c["selections"]),
        }, prefer="return=representation")
        if not cree:
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

    demain = "--demain" in sys.argv
    jour = (datetime.now(timezone.utc).date() + timedelta(days=1 if demain else 0)).isoformat()
    print(f"→ Analyse des matchs du {jour}" + (" (préparation de demain)" if demain else ""))
    matchs = candidats(demain)
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

    # ----- coupons de la nuit (Amériques) -----
    print(f"→ Coupons de la nuit du {jour} (22h → 7h)")
    coupons_nuit = []
    matchs_nuit = candidats(demain, nuit=True)
    if matchs_nuit:
        pool_nuit = selections_possibles(matchs_nuit, bookmaker)
        if pool_nuit:
            coupons_nuit = construire(pool_nuit, CATEGORIES_NUIT)
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
