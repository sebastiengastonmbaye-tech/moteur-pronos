#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
GÉNÉRATEUR DES PAGES GOOGLE — aura90.app (27/10/2026)
=====================================================
Chaque jour, après le cron, fabrique des pages HTML statiques que Google peut
lire et classer (l'app elle-même est une seule page en JavaScript) :

  /pronostics/                      les matchs analysés des prochains jours
  /pronostics/<championnat>/        une page par compétition (prochains matchs seulement — 29/10 : aucun historique)
  /pronostic/<dom>-<ext>-<date>/    une page par match (forme, buts, face-à-face…)
  /palmares/                        simple renvoi vers le match gratuit du jour dans l'app (29/10 : plus de page en double)
  /sitemap.xml  /robots.txt

Règles :
  · AVANT le match, le verdict reste réservé au PRO (sauf le match du jour,
    gratuit comme dans l'app) → la page donne envie d'ouvrir l'app.
  · APRÈS le match, le verdict publié avant le coup d'envoi est montré avec le
    score réel, gagné ou perdu : c'est le palmarès public, rien n'est caché.
  · Vocabulaire : analyse, pronostic, probabilité — jamais pari, cote, mise.

Usage : python generer_seo.py <dossier du dépôt aura90-app>
        (SITE_URL optionnel, défaut https://aura90.app)
"""
import os
import re
import sys
import json
import html
import unicodedata
from datetime import datetime, timezone, timedelta
from zoneinfo import ZoneInfo

import pandas as pd

from moteur_production import canon

SITE = os.environ.get("SITE_URL", "https://aura90.app").rstrip("/")
F_PRONOS = "donnees/pronos_publies.csv"
F_HISTO = "donnees/histo_api.csv"
PARIS, DAKAR = ZoneInfo("Europe/Paris"), ZoneInfo("Africa/Dakar")
MOIS = ["janvier", "février", "mars", "avril", "mai", "juin", "juillet", "août",
        "septembre", "octobre", "novembre", "décembre"]
JOURS = ["lundi", "mardi", "mercredi", "jeudi", "vendredi", "samedi", "dimanche"]
E = lambda s: html.escape(str(s if s is not None else ""), quote=True)


# ------------------------------------------------------------------ outils
def slug(s):
    s = unicodedata.normalize("NFKD", str(s)).encode("ascii", "ignore").decode()
    return re.sub(r"[^a-z0-9]+", "-", s.lower()).strip("-") or "x"


def date_longue(d):
    d = pd.Timestamp(d)
    return f"{JOURS[d.weekday()]} {d.day}{'er' if d.day == 1 else ''} {MOIS[d.month - 1]} {d.year}"


def heures(date_match, heure):
    """heure de l'API (UTC) → (Paris, Dakar)"""
    try:
        h, m = map(int, str(heure).split(":")[:2])
        t = datetime.fromisoformat(str(date_match)[:10]).replace(hour=h, minute=m, tzinfo=timezone.utc)
        return t.astimezone(PARIS).strftime("%H:%M"), t.astimezone(DAKAR).strftime("%H:%M"), t
    except Exception:
        return None, None, None


def vrai(x):
    return str(x).strip().lower() in ("true", "1", "1.0")


def pct(x):
    return f"{x * 100:.0f} %".replace(".", ",")


def url_match(r):
    return f"/pronostic/{slug(r.dom)}-{slug(r.ext)}-{str(r.date_match)[:10]}/"


def url_ligue(ligue):
    return f"/pronostics/{slug(ligue)}/"


# ------------------------------------------------------------------ gabarit
CSS = """
:root{--nuit:#070B12;--surface:#0E1520;--surface2:#141D2B;--ligne:#1E2A3B;--texte:#E8EEF6;--sourdine:#8A97AB;--aura1:#3DE8C8;--aura2:#8B7CFF;--ok:#3DE8C8;--ko:#FF7A9E}
*{box-sizing:border-box}html{-webkit-text-size-adjust:100%}
body{margin:0;background:var(--nuit);color:var(--texte);font:15px/1.6 -apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,Helvetica,Arial,sans-serif}
a{color:var(--aura1);text-decoration:none}a:hover{text-decoration:underline}
.haut{position:sticky;top:0;z-index:5;background:rgba(7,11,18,.95);border-bottom:1px solid var(--ligne);backdrop-filter:blur(8px)}
.haut .in{max-width:760px;margin:0 auto;display:flex;align-items:center;gap:12px;padding:12px 16px}
.logo{font-weight:900;font-size:20px;letter-spacing:-.02em;color:var(--texte)}.logo b{background:linear-gradient(135deg,var(--aura1),var(--aura2));-webkit-background-clip:text;background-clip:text;color:transparent}
.haut nav{margin-left:auto;display:flex;gap:14px;font-size:13px;font-weight:700}.haut nav a{color:var(--sourdine)}
main{max-width:760px;margin:0 auto;padding:18px 16px 40px}
.fil{font-size:12px;color:var(--sourdine);margin-bottom:10px}.fil a{color:var(--sourdine)}
h1{font-size:25px;line-height:1.25;margin:4px 0 8px;letter-spacing:-.01em}
h2{font-size:18px;margin:28px 0 10px}
.sous{color:var(--sourdine);margin:0 0 16px}
.carte{background:var(--surface);border:1px solid var(--ligne);border-radius:16px;padding:16px;margin:12px 0}
.duel{display:flex;align-items:center;justify-content:center;gap:14px;text-align:center}
.duel img{width:56px;height:56px;object-fit:contain}.duel .eq{flex:1;font-weight:800}.duel .vs{color:var(--sourdine);font-weight:800}
.score{font:800 28px ui-monospace,Menlo,monospace}
.verrou{border:1px solid rgba(139,124,255,.45);background:linear-gradient(160deg,#121a2b,#16152c)}
.verdict{font-size:19px;font-weight:800;margin:4px 0}
.badge{display:inline-block;font-size:12px;font-weight:800;padding:3px 9px;border-radius:99px;background:var(--surface2);border:1px solid var(--ligne);color:var(--sourdine)}
.ok{color:var(--ok)}.ko{color:var(--ko)}
.btn{display:inline-block;margin-top:10px;padding:12px 18px;border-radius:12px;font-weight:800;color:#06110F;background:linear-gradient(135deg,var(--aura1),var(--aura2))}
.btn:hover{text-decoration:none;opacity:.92}
table{width:100%;border-collapse:collapse;font-size:14px}td,th{padding:8px 6px;border-bottom:1px solid var(--ligne);text-align:left}th{color:var(--sourdine);font-size:12px;text-transform:uppercase;letter-spacing:.04em}
.liste a.m{display:flex;align-items:center;gap:10px;padding:11px 12px;border:1px solid var(--ligne);border-radius:12px;margin:8px 0;background:var(--surface);color:var(--texte)}
.liste a.m:hover{border-color:rgba(61,232,200,.5);text-decoration:none}
.liste img{width:22px;height:22px;object-fit:contain}.liste .t{flex:1;min-width:0;font-weight:700}.liste .h{color:var(--sourdine);font-size:12.5px;white-space:nowrap}
.forme span{display:inline-block;width:22px;height:22px;line-height:22px;text-align:center;border-radius:6px;font-size:12px;font-weight:800;margin-right:3px;color:#06110F}
.forme .V{background:var(--ok)}.forme .N{background:#C9D2DE}.forme .D{background:var(--ko)}
.kpi{display:grid;grid-template-columns:repeat(3,1fr);gap:8px}.kpi div{background:var(--surface2);border-radius:12px;padding:12px;text-align:center}.kpi b{display:block;font:800 21px ui-monospace,Menlo,monospace;color:var(--aura1)}.kpi span{font-size:11.5px;color:var(--sourdine)}
.chips{display:flex;flex-wrap:wrap;gap:8px}.chips a{padding:7px 12px;border:1px solid var(--ligne);border-radius:99px;font-size:13px;font-weight:700;color:var(--texte);background:var(--surface)}
details{border-bottom:1px solid var(--ligne);padding:10px 0}summary{cursor:pointer;font-weight:700}
footer{max-width:760px;margin:0 auto;padding:22px 16px 40px;color:var(--sourdine);font-size:12.5px;border-top:1px solid var(--ligne)}
"""


def page(chemin, titre, description, corps, jsonld=None, fil=None, image=None):
    """chemin = URL relative (/pronostics/…/) ; écrit <sortie><chemin>index.html"""
    canon_url = SITE + chemin
    ld = ""
    blocs = list(jsonld or [])
    if fil:
        blocs.append({"@context": "https://schema.org", "@type": "BreadcrumbList",
                      "itemListElement": [{"@type": "ListItem", "position": i + 1, "name": n,
                                           "item": SITE + u} for i, (n, u) in enumerate(fil)]})
    for b in blocs:
        ld += '<script type="application/ld+json">' + json.dumps(b, ensure_ascii=False).replace("</", "<\\/") + "</script>\n"
    fil_html = ""
    if fil:
        fil_html = '<div class="fil">' + " › ".join(
            f'<a href="{E(u)}">{E(n)}</a>' if i < len(fil) - 1 else E(n) for i, (n, u) in enumerate(fil)) + "</div>"
    img = image or f"{SITE}/og-image.png"
    return f"""<!DOCTYPE html>
<html lang="fr">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{E(titre)}</title>
<meta name="description" content="{E(description)}">
<link rel="canonical" href="{E(canon_url)}">
<meta name="robots" content="index, follow, max-image-preview:large">
<meta name="theme-color" content="#070B12">
<link rel="icon" type="image/png" href="/icon-192.png">
<link rel="apple-touch-icon" href="/icon-180.png">
<meta property="og:type" content="website">
<meta property="og:site_name" content="AURA90">
<meta property="og:locale" content="fr_FR">
<meta property="og:url" content="{E(canon_url)}">
<meta property="og:title" content="{E(titre)}">
<meta property="og:description" content="{E(description)}">
<meta property="og:image" content="{E(img)}">
<meta name="twitter:card" content="summary_large_image">
{ld}<style>{CSS}</style>
</head>
<body>
<header class="haut"><div class="in"><a class="logo" href="/">AURA<b>90'</b></a>
<nav><a href="/pronostics/">Pronostics</a><a href="/?aller=palmares">Palmarès</a><a href="/">L'app</a></nav></div></header>
<main>{fil_html}{corps}</main>
<footer>AURA90 analyse chaque match de football avec un moteur statistique : forme des équipes, buts attendus, 121 scénarios de score simulés.
Chaque verdict est publié et daté AVANT le coup d'envoi.<br><br>
Réservé aux 18 ans et plus. AURA90 fournit des analyses statistiques, jamais une garantie de résultat.<br><br>
<a href="/">Ouvrir l'app AURA90</a> · <a href="/pronostics/">Pronostics du jour</a> · <a href="/?aller=palmares">Palmarès</a></footer>
</body>
</html>
"""


def ecrire(sortie, chemin, contenu):
    d = os.path.join(sortie, chemin.strip("/"))
    os.makedirs(d, exist_ok=True)
    f = os.path.join(d, "index.html")
    ancien = open(f, encoding="utf-8").read() if os.path.exists(f) else None
    if ancien != contenu:
        with open(f, "w", encoding="utf-8") as x:
            x.write(contenu)
        return True
    return False


# ------------------------------------------------------------------ statistiques des équipes
class Stats:
    def __init__(self, histo):
        h = histo[histo.statut.isin(["FT", "AET", "PEN"])].dropna(subset=["buts_dom", "buts_ext"]).copy()
        h["date"] = pd.to_datetime(h["date"])
        h["dom_c"] = h.equipe_dom.map(canon)
        h["ext_c"] = h.equipe_ext.map(canon)
        self.h = h.sort_values("date")

    def derniers(self, equipe, avant, n):
        e = canon(equipe)
        h = self.h[((self.h.dom_c == e) | (self.h.ext_c == e)) & (self.h.date < pd.Timestamp(avant))]
        res = []
        for _, r in h.tail(n).iloc[::-1].iterrows():
            dom = r.dom_c == e
            bp, bc = (r.buts_dom, r.buts_ext) if dom else (r.buts_ext, r.buts_dom)
            res.append({"date": r.date, "adv": r.equipe_ext if dom else r.equipe_dom, "dom": dom,
                        "bp": int(bp), "bc": int(bc), "ligue": r.ligue_nom,
                        "r": "V" if bp > bc else ("N" if bp == bc else "D")})
        return res

    def face_a_face(self, a, b, avant, n=5):
        ca, cb = canon(a), canon(b)
        h = self.h[(((self.h.dom_c == ca) & (self.h.ext_c == cb)) | ((self.h.dom_c == cb) & (self.h.ext_c == ca)))
                   & (self.h.date < pd.Timestamp(avant))]
        return [{"date": r.date, "dom": r.equipe_dom, "ext": r.equipe_ext,
                 "s": f"{int(r.buts_dom)}-{int(r.buts_ext)}", "dom_c": r.dom_c}
                for _, r in h.tail(n).iloc[::-1].iterrows()]


def phrase_forme(nom, m):
    if not m:
        return f"Pas assez de matchs récents de {nom} dans nos données."
    v = sum(x["r"] == "V" for x in m); n = sum(x["r"] == "N" for x in m); d = sum(x["r"] == "D" for x in m)
    bp = sum(x["bp"] for x in m); bc = sum(x["bc"] for x in m)
    morceaux = [f"{v} victoire{'s' if v > 1 else ''}" if v else None,
                f"{n} nul{'s' if n > 1 else ''}" if n else None,
                f"{d} défaite{'s' if d > 1 else ''}" if d else None]
    txt = ", ".join(x for x in morceaux if x)
    return (f"Sur ses {len(m)} derniers matchs, {nom} compte {txt}, avec {bp} but{'s' if bp > 1 else ''} "
            f"marqué{'s' if bp > 1 else ''} et {bc} encaissé{'s' if bc > 1 else ''}.")


def bloc_forme(nom, m):
    if not m:
        return ""
    pastilles = "".join(f'<span class="{x["r"]}" title="{E(x["adv"])} {x["bp"]}-{x["bc"]}">{x["r"]}</span>' for x in m)
    lignes = "".join(
        f'<tr><td>{x["date"].strftime("%d/%m")}</td><td>{"vs" if x["dom"] else "@"} {E(x["adv"])}</td>'
        f'<td class="{"ok" if x["r"] == "V" else ("ko" if x["r"] == "D" else "")}">{x["bp"]}-{x["bc"]}</td><td>{E(x["ligue"])}</td></tr>'
        for x in m)
    return (f'<h3 style="margin:16px 0 6px;font-size:15px">{E(nom)}</h3><div class="forme">{pastilles}</div>'
            f'<p style="margin:8px 0">{E(phrase_forme(nom, m))}</p>'
            f'<table><tr><th>Date</th><th>Adversaire</th><th>Score</th><th>Compétition</th></tr>{lignes}</table>')


# ------------------------------------------------------------------ pages
def verdict_txt(r):
    return str(r.prono_principal)


def resultat_html(r):
    gagne, buts = vrai(r.prono_gagne), vrai(r.buts_gagne)
    return (f'<div class="carte"><div class="badge">Publié avant le match</div>'
            f'<div class="verdict">{E(verdict_txt(r))} '
            f'<span class="{"ok" if gagne else "ko"}">{"✓ validé" if gagne else "✗ raté"}</span></div>'
            f'<div>Confiance annoncée : {int(r.confiance)} % · {E(r.badge)}</div>'
            f'<div style="margin-top:6px">{E(r.prono_buts)} '
            f'<span class="{"ok" if buts else "ko"}">{"✓" if buts else "✗"}</span></div></div>')


def page_match(r, st, mdj_id, ligues_ok):
    p_h, d_h, t = heures(r.date_match, r.heure)
    joue = vrai(r.verifie) and isinstance(r.score_reel, str)
    quand = date_longue(r.date_match)
    m_dom = st.derniers(r.dom, r.date_match, 5)
    m_ext = st.derniers(r.ext, r.date_match, 5)
    h2h = st.face_a_face(r.dom, r.ext, r.date_match)
    m10d, m10e = st.derniers(r.dom, r.date_match, 10), st.derniers(r.ext, r.date_match, 10)
    moy = lambda m: (sum(x["bp"] + x["bc"] for x in m) / len(m)) if m else None

    titre = (f"{r.dom} – {r.ext} ({r.score_reel}) : pronostic et analyse · {r.ligue}" if joue
             else f"Pronostic {r.dom} – {r.ext} · {r.ligue} · {pd.Timestamp(r.date_match).day} {MOIS[pd.Timestamp(r.date_match).month - 1]}")
    desc = (f"{r.dom} – {r.ext} ({r.ligue}, {quand}) : score final {r.score_reel}, "
            f"la forme des deux équipes et les confrontations directes." if joue else
            f"Pronostic {r.dom} – {r.ext} ({r.ligue}) le {quand}"
            + (f" à {p_h} (Paris), {d_h} à Dakar" if p_h else "")
            + " : forme des équipes, buts, face-à-face et verdict du moteur AURA90.")

    intro = (f"{r.dom} reçoit {r.ext} le {quand}"
             + (f" à {p_h} (heure de Paris), {d_h} à Dakar" if p_h else "")
             + f", en {r.ligue}.")
    if m10d and m10e:
        f1 = lambda x: f"{x:.1f}".replace(".", ",")
        intro += (f" En moyenne, les 10 derniers matchs de {r.dom} ont produit {f1(moy(m10d))} buts, "
                  f"contre {f1(moy(m10e))} pour ceux de {r.ext}.")
    duel = (f'<div class="carte"><div class="duel">'
            f'<div class="eq"><img src="{E(r.logo_dom)}" alt="{E(r.dom)}" loading="lazy" width="56" height="56"><br>{E(r.dom)}</div>'
            f'<div class="vs">{("<div class=score>" + E(r.score_reel) + "</div>") if joue else "VS"}</div>'
            f'<div class="eq"><img src="{E(r.logo_ext)}" alt="{E(r.ext)}" loading="lazy" width="56" height="56"><br>{E(r.ext)}</div>'
            f'</div><div style="text-align:center;color:var(--sourdine);font-size:13px;margin-top:8px">'
            f'{E(r.ligue)} · {E(quand)}{(" · " + p_h + " Paris / " + d_h + " Dakar") if p_h else ""}</div></div>')

    lien_app = f"{SITE}/?aller=match/{int(r.fixture_id)}"
    if joue:   # 29/10/2026 : match terminé → score final seulement, aucun historique de pronostic
        verdict = (f'<h2>Match terminé</h2><div class="carte"><div>Score final : <b>{E(r.score_reel)}</b>.</div>'
                   f'<div style="margin-top:6px">Le moteur AURA90 analyse chaque jour les prochains matchs.</div>'
                   f'<a class="btn" href="{SITE}/?aller=mdj">Voir le match gratuit du jour</a></div>')
        rep_faq = f"Le match s'est terminé sur le score de {r.score_reel}."
    elif int(r.fixture_id) == mdj_id:
        verdict = (f'<h2>Le verdict du moteur · match du jour gratuit</h2><div class="carte">'
                   f'<div class="verdict">{E(verdict_txt(r))}</div><div>Confiance : {int(r.confiance)} % · {E(r.badge)}</div>'
                   f'<div style="margin-top:6px">{E(r.prono_buts)} · confiance {int(r.confiance_buts)} %</div>'
                   f'<div style="margin-top:6px;color:var(--sourdine)">Scores les plus probables : {E(r.scores_top3)}</div>'
                   f'<a class="btn" href="{E(lien_app)}">Voir l\'analyse complète dans l\'app</a></div>')
        rep_faq = f"Le moteur AURA90 donne « {verdict_txt(r)} » avec une confiance de {int(r.confiance)} %."
    else:
        verdict = (f'<h2>Le verdict du moteur</h2><div class="carte verrou">'
                   f'<div class="verdict">🔒 Verdict réservé aux membres PRO</div>'
                   f'<div>Le moteur AURA90 a simulé 121 scénarios de score pour {E(r.dom)} – {E(r.ext)}. '
                   f'Son verdict, son niveau de confiance et les scores les plus probables sont dans l\'app.</div>'
                   f'<a class="btn" href="{E(lien_app)}">Voir le verdict dans l\'app</a></div>')
        rep_faq = "Le verdict du moteur AURA90 et son niveau de confiance sont disponibles dans l'app, publiés avant le coup d'envoi."
    verdict += f'<p class="sous"><a href="{url_ligue(r.ligue)}">Tous les prochains pronostics {E(r.ligue)}</a></p>'

    h2h_html = ""
    if h2h:
        a = n_ = b = 0
        for x in h2h:
            g1, g2 = map(int, x["s"].split("-"))
            if g1 == g2:
                n_ += 1
            elif (g1 > g2) == (x["dom_c"] == canon(r.dom)):
                a += 1
            else:
                b += 1
        h2h_html = (f"<h2>Face-à-face</h2><p>Lors des {len(h2h)} dernières confrontations : {E(r.dom)} {a} victoire{'s' if a > 1 else ''}, "
                    f"{n_} nul{'s' if n_ > 1 else ''}, {E(r.ext)} {b} victoire{'s' if b > 1 else ''}.</p><table>"
                    + "".join(f'<tr><td>{x["date"].strftime("%d/%m/%Y")}</td><td>{E(x["dom"])} – {E(x["ext"])}</td><td>{x["s"]}</td></tr>' for x in h2h)
                    + "</table>")

    faq = [(f"À quelle heure se joue {r.dom} – {r.ext} ?",
            f"Le match se joue le {quand}" + (f" à {p_h} heure de Paris ({d_h} à Dakar)." if p_h else ".")),
           (f"Quel est le pronostic de {r.dom} – {r.ext} ?", rep_faq)]
    if m_dom and m_ext:
        faq.append((f"Quelle est la forme de {r.dom} et de {r.ext} ?", phrase_forme(r.dom, m_dom) + " " + phrase_forme(r.ext, m_ext)))
    faq_html = "<h2>Questions fréquentes</h2>" + "".join(f"<details><summary>{E(q)}</summary><p>{E(a)}</p></details>" for q, a in faq)

    corps = (f'<h1>{E("Pronostic " if not joue else "")}{E(r.dom)} – {E(r.ext)}</h1><p class="sous">{E(intro)}</p>'
             + duel + verdict
             + (f"<h2>Forme récente</h2>{bloc_forme(r.dom, m_dom)}{bloc_forme(r.ext, m_ext)}" if (m_dom or m_ext) else "")
             + h2h_html + faq_html)

    # 28/10/2026 : plus de données « Événement » (SportsEvent) — Google les réserve aux
    # organisateurs d'événements avec billetterie et signalait des champs manquants
    # (lieu, adresse, prix…). On garde la FAQ et le fil d'Ariane, sans erreur.
    ld = [{"@context": "https://schema.org", "@type": "FAQPage",
           "mainEntity": [{"@type": "Question", "name": q, "acceptedAnswer": {"@type": "Answer", "text": a}} for q, a in faq]}]
    fil = [("AURA90", "/"), ("Pronostics", "/pronostics/"), (r.ligue, url_ligue(r.ligue)), (f"{r.dom} – {r.ext}", url_match(r))]
    return page(url_match(r), titre, desc, corps, ld, fil)


def ligne_match(r, joue=False):
    p_h, d_h, _ = heures(r.date_match, r.heure)
    droite = (f'<span class="{"ok" if vrai(r.prono_gagne) else "ko"}">{E(r.score_reel)} {"✓" if vrai(r.prono_gagne) else "✗"}</span>'
              if joue else f'{pd.Timestamp(r.date_match).strftime("%d/%m")}{(" · " + p_h) if p_h else ""}')
    return (f'<a class="m" href="{url_match(r)}"><img src="{E(r.logo_dom)}" alt="" loading="lazy" width="22" height="22">'
            f'<span class="t">{E(r.dom)} – {E(r.ext)}</span><img src="{E(r.logo_ext)}" alt="" loading="lazy" width="22" height="22">'
            f'<span class="h">{droite}</span></a>')


def stats_globales(v):
    return len(v), (v.prono_gagne.map(vrai).mean() if len(v) else 0)


def page_ligue(ligue, df, taux):
    a_venir = df[~df.verifie.map(vrai)].sort_values(["date_match", "heure"])
    joues = df[df.verifie.map(vrai)].sort_values(["date_match", "heure"], ascending=False)
    n, r = stats_globales(joues)
    titre = f"Pronostic {ligue} : analyses des prochains matchs"
    desc = (f"Pronostics {ligue} par le moteur AURA90 : les prochains matchs analysés, "
            f"forme des équipes, buts attendus et verdict publié avant le coup d'envoi.")
    corps = (f"<h1>Pronostics {E(ligue)}</h1>"
             f'<p class="sous">Chaque match de {E(ligue)} passe par le moteur AURA90 : forme des équipes, buts attendus, '
             f"121 scénarios de score. Le verdict est publié avant le coup d'envoi (heures de Paris).</p>")
    if len(a_venir):
        corps += "<h2>Prochains matchs analysés</h2><div class='liste'>" + "".join(ligne_match(x) for x in a_venir.itertuples()) + "</div>"
    else:
        corps += "<p>Les prochains matchs sont publiés chaque nuit. Reviens demain matin.</p>"
    corps += f'<a class="btn" href="/">Ouvrir l\'app AURA90</a>'
    fil = [("AURA90", "/"), ("Pronostics", "/pronostics/"), (ligue, url_ligue(ligue))]
    return page(url_ligue(ligue), titre, desc, corps, None, fil)


def page_hub(p, ligues):
    auj = pd.Timestamp(datetime.now(PARIS).date())
    a_venir = p[(~p.verifie.map(vrai)) & (pd.to_datetime(p.date_match) >= auj)].sort_values(["date_match", "heure"])
    v = p[p.verifie.map(vrai)]
    n, r = stats_globales(v)
    hier = v[pd.to_datetime(v.date_match) >= auj - pd.Timedelta(days=3)].sort_values("date_match", ascending=False)
    titre = "Pronostics foot du jour : analyses du moteur AURA90"
    desc = ("Les pronostics foot du jour et des prochains jours, calculés par le moteur AURA90 : Ligue des Champions, "
            "Premier League, Liga, Ligue 1, Serie A…")
    corps = (f"<h1>Pronostics foot du jour</h1>"
             f'<p class="sous">Le moteur AURA90 analyse chaque nuit les matchs des grands championnats et des coupes. '
             "Chaque verdict est publié avant le coup d'envoi · heures de Paris.</p>"
             '<div class="chips">' + "".join(f'<a href="{url_ligue(l)}">{E(l)}</a>' for l in ligues) + "</div>")
    for d, g in a_venir.groupby("date_match", sort=True):
        corps += f"<h2>{E(date_longue(d).capitalize())}</h2><div class='liste'>" + "".join(ligne_match(x) for x in g.itertuples()) + "</div>"
    if not len(a_venir):
        corps += "<p>Les prochains matchs sont publiés chaque nuit. Reviens demain matin.</p>"
    corps += '<a class="btn" href="/">Ouvrir l\'app AURA90</a>'
    ld = [{"@context": "https://schema.org", "@type": "ItemList",
           "itemListElement": [{"@type": "ListItem", "position": i + 1, "url": SITE + url_match(x)}
                               for i, x in enumerate(a_venir.itertuples())]}]
    return page("/pronostics/", titre, desc, corps, ld, [("AURA90", "/"), ("Pronostics", "/pronostics/")])


RENVOI_PALMARES = """<!DOCTYPE html><html lang="fr"><head><meta charset="UTF-8">
<meta name="robots" content="noindex, follow"><link rel="canonical" href="https://aura90.app/">
<meta http-equiv="refresh" content="0; url=/?aller=mdj"><title>AURA90</title>
<script>location.replace('/?aller=mdj')</script></head>
<body style="background:#070B12;color:#E8EEF6;font-family:sans-serif;text-align:center;padding:40px">
<a href="/?aller=mdj" style="color:#3DE8C8">Voir le match gratuit du jour</a></body></html>"""


# ------------------------------------------------------------------ main
def main(sortie):
    p = pd.read_csv(F_PRONOS)
    p = p.dropna(subset=["dom", "ext", "date_match", "ligue"]).drop_duplicates("fixture_id", keep="first")
    p["date_match"] = p.date_match.astype(str).str[:10]
    p["heure"] = p["heure"].fillna("").astype(str)
    st = Stats(pd.read_csv(F_HISTO, low_memory=False))

    # match du jour gratuit = le plus confiant de la date la plus proche (même règle que l'app)
    auj = datetime.now(PARIS).date().isoformat()
    av = p[(~p.verifie.map(vrai)) & (p.date_match >= auj)]
    mdj_id = int(av[av.date_match == av.date_match.min()].sort_values("confiance", ascending=False).iloc[0].fixture_id) if len(av) else None

    v = p[p.verifie.map(vrai)]
    ligues_ok = {l: (s.prono_gagne.map(vrai).mean(), len(s)) for l, s in v.groupby("ligue") if len(s) >= 10}
    ligues = [l for l, _ in sorted(p.groupby("ligue"), key=lambda x: -len(x[1]))]

    n_ecrit, urls = 0, []
    for r in p.itertuples():
        try:
            n_ecrit += ecrire(sortie, url_match(r), page_match(r, st, mdj_id, ligues_ok))
            urls.append((url_match(r), str(r.date_match)[:10], "weekly" if not vrai(r.verifie) else "monthly", "0.6"))
        except Exception as e:
            print(f"   ⚠️ page {r.dom} – {r.ext} : {e}")
    for l in ligues:
        n_ecrit += ecrire(sortie, url_ligue(l), page_ligue(l, p[p.ligue == l], ligues_ok.get(l)))
        urls.append((url_ligue(l), auj, "daily", "0.8"))
    n_ecrit += ecrire(sortie, "/pronostics/", page_hub(p, ligues))
    # 29/10/2026 : plus de page « palmarès » à part (doublon du Palmarès de l'app) →
    # l'ancienne adresse ouvre directement l'analyse du match gratuit du jour dans l'app
    n_ecrit += ecrire(sortie, "/palmares/", RENVOI_PALMARES)
    urls = [("/", auj, "daily", "1.0"), ("/pronostics/", auj, "daily", "0.9")] + urls

    sm = ['<?xml version="1.0" encoding="UTF-8"?>', '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">']
    sm += [f"  <url><loc>{E(SITE + u)}</loc><lastmod>{d}</lastmod><changefreq>{c}</changefreq><priority>{pr}</priority></url>"
           for u, d, c, pr in urls]
    sm.append("</urlset>")
    with open(os.path.join(sortie, "sitemap.xml"), "w", encoding="utf-8") as f:
        f.write("\n".join(sm) + "\n")
    with open(os.path.join(sortie, "robots.txt"), "w", encoding="utf-8") as f:
        f.write(f"User-agent: *\nAllow: /\nDisallow: /admin.html\n\nSitemap: {SITE}/sitemap.xml\n")
    print(f"   🌐 pages Google : {len(urls)} URL dans le sitemap, {n_ecrit} page(s) créée(s) ou mise(s) à jour")


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else "site")
