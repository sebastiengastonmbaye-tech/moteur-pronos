#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
AURA90 — Enrichissement du moteur (données de contexte)
=======================================================

Le moteur de base note les équipes sur leurs résultats pondérés par la récence.
Ce module ajoute ce qu'il ne voit pas, match par match, via API-Football :

  1. ABSENCES      blessés et suspendus (endpoint injuries), pondérés par le
                   poste ET par le statut de titulaire (présent dans les
                   compositions des derniers matchs). Un gardien titulaire
                   absent pèse plus qu'un remplaçant.
  2. FATIGUE       jours de repos depuis le dernier match, enchaînement
                   (3 matchs en 8 jours), match européen en semaine.
  3. EXPECTED GOALS sur/sous-performance récente : une équipe qui marque
                   beaucoup plus que ses xG a eu de la réussite, elle revient
                   vers sa vraie valeur (et inversement).
  4. CLASSEMENT    contexte de saison : zone de relégation, course au titre,
                   équipe sans enjeu en fin de saison.

Chaque facteur produit un multiplicateur sur les buts attendus (attaque et
défense) de chaque équipe. Les probabilités du moteur sont recalculées avec
ces buts attendus corrigés.

Coût API maîtrisé : cache sur disque (donnees/enrichissement/), budget
d'appels par exécution (BUDGET_APPELS). Si le budget est épuisé ou l'API
indisponible, le module se retire proprement : les probabilités du moteur
restent inchangées.
"""

import json
import math
import os
import re
import time
from datetime import datetime, timedelta

import numpy as np
import pandas as pd
import requests

BASE_API = "https://v3.football.api-sports.io"
CLE_API = os.environ.get("API_FOOTBALL_KEY", "")
DOSSIER_CACHE = "donnees/enrichissement"
BUDGET_APPELS = 600           # appels API max par exécution pour ce module
SAISON = 2026

# ---------------------------------------------------------------------------
# POIDS DES FACTEURS  (multiplicateurs sur les buts attendus)
# ---------------------------------------------------------------------------
# Absences : perte d'attaque / de solidité défensive par joueur absent,
# selon le poste, si TITULAIRE. Un remplaçant compte pour un tiers.
POIDS_ABSENCE = {            # (effet sur l'attaque, effet sur la défense)
    "Goalkeeper": (0.00, 0.10),
    "Defender":   (0.01, 0.045),
    "Midfielder": (0.035, 0.03),
    "Attacker":   (0.06, 0.005),
}
FACTEUR_REMPLACANT = 0.35
PLAFOND_ABSENCES = 0.25      # perte maximale cumulée (attaque ou défense)
POIDS_INCERTAIN = 0.5        # joueur « Questionable » : moitié de l'effet

# Fatigue
REPOS_COURT = 3              # < 3 jours de repos
MALUS_REPOS_COURT = 0.035
MALUS_CONGESTION = 0.03      # ≥ 3 matchs sur les 8 derniers jours
MALUS_EUROPE_SEMAINE = 0.02  # a joué en coupe d'Europe dans les 4 derniers jours

# Expected goals : régression vers les xG sur les 6 derniers matchs
FENETRE_XG = 6
FORCE_XG = 0.45              # 0 = on ignore les xG ; 1 = on remplace les buts par les xG
PLAFOND_XG = 0.12            # correction maximale (±12 %)

# Classement (effet modeste, surtout en fin de saison)
BONUS_ENJEU = 0.03           # course au titre / lutte pour le maintien
MALUS_SANS_ENJEU = 0.03      # milieu de tableau, ≤ 6 journées restantes
JOURNEES_FIN_SAISON = 6

# Dixon-Coles : correction des petits scores utilisée pour recalculer
RHO = -0.08
MAX_BUTS = 10


# ---------------------------------------------------------------------------
# API + CACHE
# ---------------------------------------------------------------------------
class Api:
    def __init__(self):
        self.appels = 0
        self.echecs = 0
        os.makedirs(DOSSIER_CACHE, exist_ok=True)

    def _cache(self, nom):
        return os.path.join(DOSSIER_CACHE, re.sub(r"[^a-zA-Z0-9_.-]", "_", nom) + ".json")

    def get(self, chemin, params, cle_cache, ttl_heures):
        """Appel API avec cache disque. ttl_heures = None → cache permanent."""
        fichier = self._cache(cle_cache)
        if os.path.exists(fichier):
            try:
                with open(fichier, encoding="utf-8") as f:
                    d = json.load(f)
                age = (time.time() - d.get("_t", 0)) / 3600
                if ttl_heures is None or age < ttl_heures:
                    return d.get("rep", [])
            except Exception:
                pass
        if self.appels >= BUDGET_APPELS or not CLE_API or self.echecs >= 5:
            return None
        self.appels += 1
        try:
            r = requests.get(f"{BASE_API}/{chemin}", headers={"x-apisports-key": CLE_API},
                             params=params, timeout=30)
            if r.status_code == 429:
                time.sleep(20); self.echecs += 1; return None
            r.raise_for_status()
            rep = r.json().get("response", [])
        except Exception:
            self.echecs += 1
            return None
        try:
            with open(fichier, "w", encoding="utf-8") as f:
                json.dump({"_t": time.time(), "rep": rep}, f)
        except Exception:
            pass
        time.sleep(0.25)
        return rep


def id_equipe_depuis_logo(url):
    """https://media.api-sports.io/football/teams/52.png → 52"""
    m = re.search(r"/teams/(\d+)\.png", str(url or ""))
    return int(m.group(1)) if m else None


# ---------------------------------------------------------------------------
# 1. ABSENCES
# ---------------------------------------------------------------------------
def titulaires_recents(api, team_id, histo_equipe):
    """Ensemble des ids de joueurs apparus dans le XI de départ sur les 3
    derniers matchs joués (cache 5 jours)."""
    fixtures = list(histo_equipe.sort_values("date", ascending=False).head(3).fixture_id)
    titulaires, n = set(), 0
    for fid in fixtures:
        rep = api.get("fixtures/lineups", {"fixture": int(fid)}, f"lineups_{fid}", None)
        if not rep:
            continue
        for bloc in rep:
            if bloc.get("team", {}).get("id") != team_id:
                continue
            n += 1
            for j in bloc.get("startXI", []):
                pid = j.get("player", {}).get("id")
                if pid:
                    titulaires.add(pid)
    return titulaires, n


def postes_effectif(api, team_id):
    rep = api.get("players/squads", {"team": team_id}, f"squad_{team_id}_{SAISON}", 24 * 7)
    postes = {}
    for bloc in rep or []:
        for j in bloc.get("players", []):
            if j.get("id"):
                postes[j["id"]] = j.get("position", "Midfielder")
    return postes


def facteur_absences(api, fixture_id, team_id, histo_equipe):
    """Retourne (perte_attaque, perte_defense, description)."""
    rep = api.get("injuries", {"fixture": fixture_id}, f"injuries_{fixture_id}", 6)
    if rep is None:
        return 0.0, 0.0, ""
    absents = [x for x in rep if x.get("team", {}).get("id") == team_id]
    if not absents:
        return 0.0, 0.0, ""
    postes = postes_effectif(api, team_id)
    titulaires, n_compos = titulaires_recents(api, team_id, histo_equipe)
    att, dfn, noms = 0.0, 0.0, []
    for x in absents:
        pl = x.get("player", {})
        pid, nom = pl.get("id"), pl.get("name", "?")
        poste = postes.get(pid, "Midfielder")
        pa, pd_ = POIDS_ABSENCE.get(poste, POIDS_ABSENCE["Midfielder"])
        statut = 1.0 if (n_compos == 0 or pid in titulaires) else FACTEUR_REMPLACANT
        if str(pl.get("type", "")).lower().startswith("question"):
            statut *= POIDS_INCERTAIN
        att += pa * statut; dfn += pd_ * statut
        noms.append(f"{nom} ({poste[:3]}{'*' if statut >= 1 else ''})")
    att, dfn = min(att, PLAFOND_ABSENCES), min(dfn, PLAFOND_ABSENCES)
    return att, dfn, ", ".join(noms[:6])


# ---------------------------------------------------------------------------
# 2. FATIGUE
# ---------------------------------------------------------------------------
def facteur_fatigue(histo_equipe, date_match, competitions_uefa=(2, 3)):
    passes = histo_equipe[histo_equipe["date"] < date_match].sort_values("date")
    if passes.empty:
        return 0.0, ""
    dernier = passes.iloc[-1]
    repos = (date_match - dernier["date"]).days
    recents = passes[passes["date"] >= date_match - pd.Timedelta(days=8)]
    malus, notes = 0.0, []
    if repos < REPOS_COURT:
        malus += MALUS_REPOS_COURT; notes.append(f"repos {repos} j")
    if len(recents) >= 3:
        malus += MALUS_CONGESTION; notes.append(f"{len(recents)} matchs / 8 j")
    europe = passes[(passes["date"] >= date_match - pd.Timedelta(days=4)) &
                    (passes["ligue_id"].isin(competitions_uefa))]
    if not europe.empty:
        malus += MALUS_EUROPE_SEMAINE; notes.append("coupe d'Europe en semaine")
    return malus, " · ".join(notes)


# ---------------------------------------------------------------------------
# 3. EXPECTED GOALS
# ---------------------------------------------------------------------------
def xg_du_match(api, fixture_id):
    rep = api.get("fixtures/statistics", {"fixture": int(fixture_id)}, f"stats_{fixture_id}", None)
    out = {}
    for bloc in rep or []:
        tid = bloc.get("team", {}).get("id")
        for st in bloc.get("statistics", []):
            if str(st.get("type", "")).lower() == "expected_goals" and st.get("value") not in (None, ""):
                try:
                    out[tid] = float(st["value"])
                except (TypeError, ValueError):
                    pass
    return out


def facteur_xg(api, team_id, histo_equipe, date_match):
    """Compare buts réels et xG sur les derniers matchs. Retourne
    (mult_attaque, mult_defense, description)."""
    joues = histo_equipe[(histo_equipe["date"] < date_match) &
                         (histo_equipe["statut"] == "FT")].sort_values("date", ascending=False).head(FENETRE_XG)
    if len(joues) < 4:
        return 1.0, 1.0, ""
    bm = bc = xm = xc = 0.0; n = 0
    for _, m in joues.iterrows():
        xg = xg_du_match(api, m.fixture_id)
        if not xg or team_id not in xg:
            continue
        dom = id_equipe_depuis_logo(m.logo_dom) == team_id
        adv = id_equipe_depuis_logo(m.logo_ext if dom else m.logo_dom)
        if adv not in xg:
            continue
        bm += m.buts_dom if dom else m.buts_ext
        bc += m.buts_ext if dom else m.buts_dom
        xm += xg[team_id]; xc += xg[adv]; n += 1
    if n < 3 or xm <= 0 or xc <= 0:
        return 1.0, 1.0, ""
    # buts corrigés = mélange buts réels / xG ; le multiplicateur ramène
    # l'attaque « vue par le moteur » (buts) vers cette valeur corrigée
    corr_att = ((1 - FORCE_XG) * bm + FORCE_XG * xm) / max(bm, 0.5)
    corr_def = ((1 - FORCE_XG) * bc + FORCE_XG * xc) / max(bc, 0.5)
    corr_att = float(np.clip(corr_att, 1 - PLAFOND_XG, 1 + PLAFOND_XG))
    corr_def = float(np.clip(corr_def, 1 - PLAFOND_XG, 1 + PLAFOND_XG))
    desc = f"xG {n} m. : marqué {bm:.0f} pour {xm:.1f} xG, encaissé {bc:.0f} pour {xc:.1f} xGA"
    return corr_att, corr_def, desc


# ---------------------------------------------------------------------------
# 4. CLASSEMENT
# ---------------------------------------------------------------------------
def classement(api, league_id):
    rep = api.get("standings", {"league": league_id, "season": SAISON}, f"standings_{league_id}", 24)
    table = {}
    for bloc in rep or []:
        for groupe in bloc.get("league", {}).get("standings", []):
            for ligne in groupe:
                tid = ligne.get("team", {}).get("id")
                if tid:
                    table[tid] = {"rang": ligne.get("rank"), "points": ligne.get("points"),
                                  "joues": ligne.get("all", {}).get("played"), "n": len(groupe)}
    return table


def facteur_enjeu(table, team_id, nb_journees=34):
    l = table.get(team_id)
    if not l or not l.get("joues"):
        return 0.0, ""
    restantes = nb_journees - l["joues"]
    if restantes > JOURNEES_FIN_SAISON:
        return 0.0, ""
    n, rang = l["n"], l["rang"]
    if rang <= 2 or rang >= n - 2:
        return BONUS_ENJEU, "gros enjeu (titre/maintien)"
    if 7 <= rang <= n - 5:
        return -MALUS_SANS_ENJEU, "sans enjeu"
    return 0.0, ""


# ---------------------------------------------------------------------------
# RECALCUL DES PROBABILITÉS
# ---------------------------------------------------------------------------
def _matrice(lh, la, rho=RHO):
    k = np.arange(MAX_BUTS + 1)
    logf = np.cumsum(np.r_[0.0, np.log(np.arange(1, MAX_BUTS + 1))])
    ph = np.exp(-lh + k * math.log(lh) - logf)
    pa = np.exp(-la + k * math.log(la) - logf)
    M = np.outer(ph, pa)
    M[0, 0] *= 1 - lh * la * rho; M[0, 1] *= 1 + lh * rho
    M[1, 0] *= 1 + la * rho;      M[1, 1] *= 1 - rho
    M = np.clip(M, 0, None); M /= M.sum()
    return M


def probas_depuis_lambdas(lh, la):
    M = _matrice(lh, la)
    i = np.arange(MAX_BUTS + 1)
    p1 = M[np.greater.outer(i, i)].sum(); pN = np.trace(M); p2 = 1 - p1 - pN
    tot = np.add.outer(i, i)
    return {"1": float(p1), "N": float(pN), "2": float(p2),
            "O2.5": float(M[tot >= 3].sum()), "BTTS": float(M[1:, 1:].sum())}


_GRILLE = np.arange(0.15, 4.01, 0.05)


def lambdas_depuis_probas(p1, p2, pO):
    """Retrouve les buts attendus (dom, ext) qui reproduisent les probabilités
    du moteur. Grille grossière puis affinage local."""
    meilleur, best = None, 1e9
    for lh in _GRILLE[::2]:
        for la in _GRILLE[::2]:
            p = probas_depuis_lambdas(lh, la)
            e = (p["1"] - p1) ** 2 + (p["2"] - p2) ** 2 + (p["O2.5"] - pO) ** 2
            if e < best:
                best, meilleur = e, (lh, la)
    lh0, la0 = meilleur
    for lh in np.arange(lh0 - 0.1, lh0 + 0.11, 0.02):
        for la in np.arange(la0 - 0.1, la0 + 0.11, 0.02):
            if lh <= 0.05 or la <= 0.05:
                continue
            p = probas_depuis_lambdas(lh, la)
            e = (p["1"] - p1) ** 2 + (p["2"] - p2) ** 2 + (p["O2.5"] - pO) ** 2
            if e < best:
                best, meilleur = e, (lh, la)
    return meilleur


# ---------------------------------------------------------------------------
# POINT D'ENTRÉE
# ---------------------------------------------------------------------------
def enrichir(matchs, histo, journal=print):
    """
    matchs : liste de dicts avec fixture_id, ligue_id (optionnel), dom, ext,
             logo_dom, logo_ext, date_match (str), p1, pN, p2, pO25, btts
    histo  : DataFrame histo_api.csv (colonnes du cron), date en datetime
    Modifie p1/pN/p2/pO25/btts EN PLACE et ajoute une clé "contexte" lisible.
    """
    api = Api()
    histo = histo.copy()
    histo["date"] = pd.to_datetime(histo["date"], errors="coerce")
    histo["tid_dom"] = histo["logo_dom"].map(id_equipe_depuis_logo)
    histo["tid_ext"] = histo["logo_ext"].map(id_equipe_depuis_logo)
    tables = {}
    n_ok = 0

    for mt in matchs:
        try:
            date_match = pd.Timestamp(mt["date_match"])
            tid_d, tid_e = id_equipe_depuis_logo(mt.get("logo_dom")), id_equipe_depuis_logo(mt.get("logo_ext"))
            if not tid_d or not tid_e:
                continue
            h_d = histo[(histo.tid_dom == tid_d) | (histo.tid_ext == tid_d)]
            h_e = histo[(histo.tid_dom == tid_e) | (histo.tid_ext == tid_e)]
            lid = mt.get("ligue_id")
            if lid and lid not in tables:
                tables[lid] = classement(api, lid)
            table = tables.get(lid, {})

            notes = []
            mult = {"att_d": 1.0, "def_d": 1.0, "att_e": 1.0, "def_e": 1.0}

            # absences
            for cote, tid, h in (("d", tid_d, h_d), ("e", tid_e, h_e)):
                a, d, desc = facteur_absences(api, mt["fixture_id"], tid, h)
                if a or d:
                    mult[f"att_{cote}"] *= (1 - a); mult[f"def_{cote}"] *= (1 + d)
                    notes.append(f"{'dom' if cote == 'd' else 'ext'} absents : {desc}")
                # fatigue
                f, desc = facteur_fatigue(h, date_match)
                if f:
                    mult[f"att_{cote}"] *= (1 - f); mult[f"def_{cote}"] *= (1 + f * 0.5)
                    notes.append(f"{'dom' if cote == 'd' else 'ext'} fatigue : {desc}")
                # xG
                ca, cd, desc = facteur_xg(api, tid, h, date_match)
                if desc:
                    mult[f"att_{cote}"] *= ca; mult[f"def_{cote}"] *= cd
                    notes.append(f"{'dom' if cote == 'd' else 'ext'} {desc}")
                # enjeu
                e, desc = facteur_enjeu(table, tid)
                if e:
                    mult[f"att_{cote}"] *= (1 + e); mult[f"def_{cote}"] *= (1 - e * 0.5)
                    notes.append(f"{'dom' if cote == 'd' else 'ext'} {desc}")

            if all(abs(v - 1) < 1e-9 for v in mult.values()):
                mt["contexte"] = ""
                continue

            # recalcul : buts attendus corrigés → probabilités corrigées
            lh, la = lambdas_depuis_probas(mt["p1"], mt["p2"], mt["pO25"])
            lh2 = lh * mult["att_d"] * mult["def_e"]     # def_e > 1 = défense ext affaiblie
            la2 = la * mult["att_e"] * mult["def_d"]
            p = probas_depuis_lambdas(lh2, la2)
            mt.update({"p1": p["1"], "pN": p["N"], "p2": p["2"], "pO25": p["O2.5"], "btts": p["BTTS"]})
            mt["contexte"] = " | ".join(notes)
            n_ok += 1
        except Exception as exc:      # jamais bloquant
            mt["contexte"] = ""
            journal(f"   ⚠️ enrichissement {mt.get('dom')} – {mt.get('ext')} : {exc}")

    journal(f"   🔎 enrichissement : {n_ok} match(s) ajustés, {api.appels} appel(s) API")
    return matchs
