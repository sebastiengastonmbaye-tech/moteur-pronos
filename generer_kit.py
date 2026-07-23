# ============================================================
# AURA90 — Générateur du kit marketing quotidien
# Tourne dans le cron GitHub après la sync Supabase (5h Dakar).
# Écrit kit_du_jour.txt : message de chaîne WhatsApp prêt à coller
# + script vidéo TikTok choisi selon les résultats réels.
# Ne fait JAMAIS échouer le workflow (kit minimal en cas de pépin).
#
# Test local : python generer_kit.py --demo
# ============================================================
import os
import sys
import json
from datetime import datetime, timedelta, timezone
from urllib.request import Request, urlopen
from urllib.parse import quote

LIEN_APP = "https://aura90.netlify.app"

URL = os.environ.get("SUPABASE_URL", "").rstrip("/")
CLE = os.environ.get("SUPABASE_SERVICE_KEY", "")


# ------------------------------------------------------------
# Accès données
# ------------------------------------------------------------
def sb(chemin):
    req = Request(f"{URL}/rest/v1/{chemin}", headers={
        "apikey": CLE, "Authorization": f"Bearer {CLE}"})
    with urlopen(req, timeout=30) as r:
        return json.loads(r.read().decode())


def charger_donnees():
    auj = datetime.now(timezone.utc).date()
    hier = auj - timedelta(days=1)
    sel = "dom,ext,ligue,heure,confiance,prono_principal,prono_gagne,score_reel,scores_top3,signe,verifie,date_match"

    hier_signes = sb(f"pronos?date_match=eq.{hier}&signe=eq.true&verifie=eq.true&select={sel}&order=confiance.desc")
    jour_signes = sb(f"pronos?date_match=eq.{auj}&signe=eq.true&select={sel}&order=confiance.desc")
    jour_total = sb(f"pronos?date_match=eq.{auj}&select=dom")
    recents = sb(f"pronos?signe=eq.true&verifie=eq.true&select=prono_gagne,date_match&order=date_match.desc,confiance.desc&limit=12")
    sem_deb = auj - timedelta(days=7)
    semaine = sb(f"pronos?signe=eq.true&verifie=eq.true&date_match=gte.{sem_deb}&select=prono_gagne")
    return auj, hier_signes, jour_signes, len(jour_total), recents, semaine


def donnees_demo():
    auj = datetime.now(timezone.utc).date()
    return auj, [
        {"dom": "Viking", "ext": "Sandefjord", "ligue": "Norvège Eliteserien", "prono_principal": "Victoire Viking",
         "prono_gagne": True, "score_reel": "2-1", "scores_top3": "2-1 · 1-1 · 2-0"},
        {"dom": "Molde", "ext": "Brann", "ligue": "Norvège Eliteserien", "prono_principal": "Molde ou nul (1X)",
         "prono_gagne": False, "score_reel": "1-2", "scores_top3": "1-1 · 2-1 · 1-0"},
    ], [
        {"dom": "Flamengo", "ext": "Palmeiras", "ligue": "Brésil Série A", "heure": "21:00", "confiance": 74},
        {"dom": "Djurgardens", "ext": "Hammarby", "ligue": "Suède Allsvenskan", "heure": "17:00", "confiance": 71},
    ], 11, [{"prono_gagne": True}, {"prono_gagne": True}, {"prono_gagne": False}], \
        [{"prono_gagne": True}] * 8 + [{"prono_gagne": False}] * 2


# ------------------------------------------------------------
# Analyse des résultats
# ------------------------------------------------------------
def score_exact(p):
    sr, tops = str(p.get("score_reel") or "").strip(), str(p.get("scores_top3") or "")
    return bool(sr) and sr in [s.strip() for s in tops.split("·")]


def analyser(hier_signes, recents):
    exacts = [p for p in hier_signes if score_exact(p)]
    perdus = [p for p in hier_signes if not p.get("prono_gagne")]
    streak = 0
    for p in recents:
        if p.get("prono_gagne"):
            streak += 1
        else:
            break
    if exacts:
        return "preuve", exacts[0]
    if streak >= 3:
        return "serie", streak
    if perdus and hier_signes:
        return "assume", perdus[0]
    return "verdict", None


# ------------------------------------------------------------
# Blocs du kit
# ------------------------------------------------------------
def bloc_chaine(auj, hier_signes, jour_signes, n_jour, scenario, detail):
    lignes = []
    if hier_signes:
        lignes.append("Les signés d'hier ⚡")
        for p in hier_signes:
            ico = "✓" if p.get("prono_gagne") else "✗"
            extra = "  🎯 SCORE EXACT" if score_exact(p) else ""
            lignes.append(f"{ico} {p['dom']} – {p['ext']} → {p.get('score_reel') or '?'}{extra}")
        g = sum(1 for p in hier_signes if p.get("prono_gagne"))
        lignes.append(f"{g}/{len(hier_signes)} — tout est publié AVANT les matchs, daté, vérifiable 📲")
        lignes.append("")
    if scenario == "preuve" and detail:
        lignes.append(f"Le score exact de {detail['dom']} – {detail['ext']} était dans les scores publiés la veille. Va vérifier toi-même.")
        lignes.append("")
    if jour_signes:
        lignes.append(f"Aujourd'hui : {n_jour} matchs analysés, {len(jour_signes)} signés par le moteur.")
    else:
        lignes.append(f"Aujourd'hui : {n_jour} matchs analysés par le moteur.")
    lignes.append("Le match du jour est gratuit dans l'app 👇")
    lignes.append(LIEN_APP)
    return "\n".join(lignes)


def bloc_video(scenario, detail, jour_signes):
    C = "⚙️ Sous-titres auto ON · son tendance discret · poster 19h-21h\n" \
        "#football #IA #data #prediction #senegal — ❌ jamais : paris, cotes, bookmaker"
    if scenario == "preuve":
        p = detail
        return f"""SCRIPT « LA PREUVE » (25 s, sans visage — le plus fort, priorité absolue)
0-2 s   Main + téléphone, carte dorée à l'écran. Gros texte : « L'IA a trouvé le SCORE EXACT 🤯 »
2-10 s  Zoom sur la carte : « {p['dom']} – {p['ext']}. Score final {p.get('score_reel')}. L'algorithme l'avait mis dans ses scores probables. »
10-18 s Scroll du palmarès : « Publié AVANT le match. Daté. Public. Les réussis ET les ratés. Allez vérifier vous-mêmes. »
18-25 s Accueil : « Un match analysé gratuit chaque jour. Lien en bio. »
{C}"""
    if scenario == "serie":
        return f"""SCRIPT « LA SÉRIE » (20 s)
0-2 s   Gros texte : « {detail} signés validés d'affilée ✅ »
2-10 s  Scroll du palmarès, les ✓ qui défilent : « Chaque prono, publié avant le match, daté. »
10-16 s « Ce n'est pas moi qui le dis — c'est écrit, et vous pouvez tout vérifier. »
16-20 s « Match gratuit chaque jour. Lien en bio. »
{C}"""
    if scenario == "assume":
        p = detail
        return f"""SCRIPT « ON ASSUME » (20 s — celui qui construit la crédibilité long terme)
0-2 s   Gros texte : « L'IA s'est trompée hier. Et je le montre. »
2-10 s  La carte du perdu : « {p['dom']} – {p['ext']} : le moteur avait dit "{p['prono_principal']}". Raté. {p.get('score_reel')}. »
10-16 s « Il reste affiché en rouge au palmarès, pour toujours. C'est ça, la différence : ici, rien ne s'efface. »
16-20 s « Jugez sur l'ensemble — tout est public. Lien en bio. »
{C}"""
    haut = jour_signes[0] if jour_signes else None
    duel = f"{haut['dom']} – {haut['ext']}" if haut else "le match du jour"
    return f"""SCRIPT « VERDICT DU JOUR » (25 s)
0-2 s   « L'IA vient d'analyser {duel} 👀 »
2-14 s  Filme l'analyse EN DIRECT : l'anneau, les satellites, le % qui scanne, le verdict qui tombe. (C'est ton spectacle — laisse-le vivre.)
14-20 s La fiche : les barres des marchés, les scores probables. « 121 scénarios simulés. »
20-25 s « Verdict complet dans l'app — match gratuit chaque jour. Lien en bio. »
{C}"""


def bloc_bilan(semaine):
    tot = len(semaine)
    if not tot:
        return ""
    g = sum(1 for p in semaine if p.get("prono_gagne"))
    return f"""
──────────────────────────────────────────
④ BILAN DU DIMANCHE — à poster sur la chaîne
──────────────────────────────────────────
Bilan de la semaine ⚡
{g} validés sur {tot} pronos signés ({round(100*g/tot)} %)
Chaque prono publié AVANT le match, daté, public.
Les réussis comme les ratés — tout est au palmarès, va vérifier.
La semaine prochaine, le moteur remet ça. Match gratuit chaque jour 👇
{LIEN_APP}
"""


REPONSES = """✋ « C'est fiable ? » → « Va voir le palmarès dans l'app : tout est publié avant les matchs, daté. Tu juges toi-même 📊 »
✋ « Ça marche vraiment ? » → cite le bilan du jour (les chiffres du bloc ① ci-dessus) + « les ratés restent affichés aussi — c'est ça la différence. »
✋ « C'est payant ? » → « Un match analysé offert chaque jour. Le reste, c'est l'accès complet — tarif de lancement en ce moment. »
✋ « Comment gagner de l'argent avec vous ? » → « Programme partenaire : commission sur chaque abonné. Tout est là 👉 """ + LIEN_APP + """/partenaire.html »"""


# ------------------------------------------------------------
# Assemblage
# ------------------------------------------------------------
def generer(auj, hier_signes, jour_signes, n_jour, recents, semaine):
    scenario, detail = analyser(hier_signes, recents)
    noms = {"preuve": "🎯 LA PREUVE (score exact hier)", "serie": "🔥 LA SÉRIE",
            "assume": "🛡️ ON ASSUME", "verdict": "⚡ VERDICT DU JOUR"}
    kit = f"""══════════════════════════════════════════
 AURA90 — KIT DU {auj.strftime('%d/%m/%Y')} (généré 5h)
 Scénario du jour : {noms[scenario]}
══════════════════════════════════════════

──────────────────────────────────────────
① MESSAGE CHAÎNE WHATSAPP — copier-coller
──────────────────────────────────────────
{bloc_chaine(auj, hier_signes, jour_signes, n_jour, scenario, detail)}

──────────────────────────────────────────
② VIDÉO TIKTOK DU JOUR
──────────────────────────────────────────
{bloc_video(scenario, detail, jour_signes)}

──────────────────────────────────────────
③ RÉPONSES PRÊTES AUX COMMENTAIRES
──────────────────────────────────────────
{REPONSES}
"""
    if auj.weekday() == 6:
        kit += bloc_bilan(semaine)
    kit += f"""
──────────────────────────────────────────
📊 CE SOIR, NOTE TES 3 CHIFFRES (pour le pilotage)
──────────────────────────────────────────
· Utilisateurs totaux (panneau admin) :
· VIP actifs :
· Vues de ta meilleure vidéo :
"""
    return kit


def main():
    try:
        if "--demo" in sys.argv:
            donnees = donnees_demo()
        else:
            donnees = charger_donnees()
        kit = generer(*donnees)
    except Exception as e:
        print(f"Kit minimal (données indisponibles : {e})")
        kit = (f"AURA90 — KIT DU {datetime.now(timezone.utc).date().strftime('%d/%m/%Y')}\n"
               f"Données indisponibles ce matin — script de secours :\n\n"
               + bloc_video("verdict", None, []) + "\n\n" + REPONSES + "\n")
    with open("kit_du_jour.txt", "w", encoding="utf-8") as f:
        f.write(kit)
    print("kit_du_jour.txt écrit ✓")


if __name__ == "__main__":
    main()
