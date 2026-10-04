# ============================================================
#  BOT — commandes Telegram (inscription en temps réel + accès payant)
#
#  Flux d'inscription :
#    /start (1re fois) -> boursier -> zone(s) -> essai gratuit 7 jours
#    (boursier EN PREMIER : tous les comptages affichés ensuite
#     utilisent la bonne vue)
#    /start (déjà inscrit) -> "utilise /changer ou /status"
#    essai déjà consommé -> demande d'un code d'activation
#
#  Accès : expire_le en base ; seuls les accès actifs reçoivent les
#  notifications. Renouvellement : /activer CROUS-XXXX-XXXX.
#  Admin : /gencode, /unsub... (les non-admins sont ignorés en silence total).
# ============================================================

import logging
import re
import secrets
import time
from datetime import datetime, timedelta

from telegram import (InlineKeyboardButton, InlineKeyboardMarkup,
                      ReplyKeyboardMarkup, Update)
from telegram.constants import ParseMode
from telegram.ext import (
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    ConversationHandler,
    MessageHandler,
    TypeHandler,
    filters,
)

import asyncio

import config
import db
import notifier
import scraper
from parser import (dept_label, format_zone, normalize_city,
                    parse_city_query, parse_rayon)

log = logging.getLogger(__name__)

# États des conversations
REGION, VILLE, PORTEE, PROCHES, DEPTS, ARRS, BOURSIER, CODE = range(8)

CLAVIER_OUI_NON = ReplyKeyboardMarkup(
    [["Oui", "Non"]], one_time_keyboard=True, resize_keyboard=True
)

# choix de région au début de l'inscription / du changement
BTN_IDF = "🗼 Île-de-France"
BTN_AUTRE = "🌍 Autre"
CLAVIER_REGION = ReplyKeyboardMarkup(
    [[BTN_IDF, BTN_AUTRE]], one_time_keyboard=True, resize_keyboard=True
)

# portée de la surveillance (hors Île-de-France)
BTN_SEULE = "🏙️ Ma ville uniquement"
BTN_RAYON = f"🌐 Ville + alentours ({config.RAYON_KM} km)"
BTN_PROCHES = "🎯 Choisir des villes autour"
CLAVIER_PORTEE = ReplyKeyboardMarkup(
    [[BTN_SEULE], [BTN_RAYON], [BTN_PROCHES]],
    one_time_keyboard=True, resize_keyboard=True,
)

LISTE_DEPTS_IDF = (
    "🗼 Départements d'Île-de-France :\n\n"
    "75 — Paris\n"
    "77 — Seine-et-Marne\n"
    "78 — Yvelines\n"
    "91 — Essonne\n"
    "92 — Hauts-de-Seine\n"
    "93 — Seine-Saint-Denis\n"
    "94 — Val-de-Marne\n"
    "95 — Val-d'Oise\n\n"
    "Envoie ceux que tu veux surveiller (7 max), séparés par des "
    "virgules.\nExemple : 75, 92, 93"
)

QUESTION_ARRS = (
    "🗼 Pour Paris : veux-tu des arrondissements précis ?\n"
    "Envoie les numéros (7 max), séparés par des virgules — "
    "ex : 18, 19\n"
    "Ou réponds « non » pour surveiller tout Paris."
)

# Boutons permanents sous la zone de saisie (raccourcis des commandes)
BTN_STATUT = "📡 Mon statut"
BTN_VILLES = "🏙️ Villes"
BTN_CHANGER = "🔄 Changer de ville"
BTN_CONTACT = "💬 Contact"
BTN_CODE = "🎟 Entrer un code"
BTN_OFFRE = "🔎 Trouver mon logement"
BTN_RACHAT = "💰 Racheter un accès"
BTN_PAYER_AVANCE = "💳 Payer maintenant pour garder l'accès"
# "✅ J'ai payé" n'est PAS un bouton permanent : il n'apparaît qu'en
# inline sous le message de l'offre (cf. offre()), pour éviter qu'on le
# tape sans contexte et spamme les admins pour rien.
BTN_PAYE = "✅ J'ai payé"
TOUS_BOUTONS = (BTN_STATUT, BTN_VILLES, BTN_CHANGER, BTN_CONTACT,
                BTN_CODE, BTN_OFFRE, BTN_RACHAT, BTN_PAYER_AVANCE)

# inscrit avec accès actif (payé) : tout est accessible, rien à acheter
CLAVIER_PRINCIPAL = ReplyKeyboardMarkup(
    [[BTN_STATUT, BTN_CONTACT], [BTN_CHANGER, BTN_CODE]],
    resize_keyboard=True,
    is_persistent=True,
)
# inscrit ENCORE en essai gratuit actif : propose de payer par avance
# pour sécuriser l'accès toute la saison, sans attendre la fin de l'essai
CLAVIER_ESSAI_ACTIF = ReplyKeyboardMarkup(
    [[BTN_PAYER_AVANCE],
     [BTN_STATUT, BTN_CONTACT], [BTN_CHANGER, BTN_CODE]],
    resize_keyboard=True,
    is_persistent=True,
)
# inscrit dont l'accès est EXPIRÉ : le rachat en tête, zéro friction
CLAVIER_EXPIRE = ReplyKeyboardMarkup(
    [[BTN_RACHAT],
     [BTN_STATUT, BTN_CONTACT], [BTN_CHANGER, BTN_CODE]],
    resize_keyboard=True,
    is_persistent=True,
)
# mode découverte (pas encore inscrit) : le bouton d'offre en tête
CLAVIER_DECOUVERTE = ReplyKeyboardMarkup(
    [[BTN_OFFRE], [BTN_CONTACT], [BTN_CODE]],
    resize_keyboard=True,
    is_persistent=True,
)


def _clavier(chat_id):
    """Le clavier adapté à la situation : découverte, essai gratuit actif
    (bouton de paiement anticipé), accès payé actif, ou accès expiré
    (bouton de rachat mis en avant).
    Les admins ont un accès permanent -> jamais un autre clavier."""
    user = db.get_user(chat_id)
    if not user:
        return CLAVIER_DECOUVERTE
    if str(chat_id) in {str(a) for a in config.ADMIN_CHAT_IDS}:
        return CLAVIER_PRINCIPAL
    expire = user[1]
    actif = expire and expire > datetime.now().isoformat(timespec="seconds")
    if not actif:
        return CLAVIER_EXPIRE
    return CLAVIER_ESSAI_ACTIF if db.est_en_essai_actif(chat_id) else CLAVIER_PRINCIPAL


# le multi-zones est réservé à l'Île-de-France (anti-partage de compte)
_DEPTS_IDF = {"75", "77", "78", "91", "92", "93", "94", "95"}


def _zones_texte(zones):
    """[(ville, norm, cp), ...] -> "Paris 19e, Nanterre, Hauts-de-Seine (92)"."""
    return ", ".join(z[0] for z in zones) if zones else "aucune zone"


def _lignes_dispo_zones(zones, boursier=False):
    """Une ligne 📊 par zone surveillée, selon la vue de l'abonné,
    suivie de l'âge du dernier scan (chiffres lus en base, jamais de
    requête API à la demande)."""
    lignes = "".join(_ligne_dispo(vn, v, cp or None, boursier)
                     for v, vn, cp in zones)
    actu = _derniere_actu(boursier)
    if lignes and actu:
        lignes += f"\n⏱ Dernier scan : {actu}."
    return lignes

# Anti-spam /start : {chat_id: [nb, debut_fenetre_ts, muet_jusqua_ts]}
# 3 /start dans la minute -> on ignore ses /start pendant 30 min.
_starts_recents = {}

# Anti-spam notif admin sur l'offre : {chat_id: dernier_timestamp_notifie}
_dernier_notif_offre = {}

# ---- Mesure du temps de réponse ----
# Deux TypeHandler encadrent le traitement de CHAQUE update (commande,
# bouton, callback...) : groupe -1 (avant tous les autres handlers) et
# groupe 999 (après). La différence = temps de traitement réel, peu
# importe quel handler a répondu. Lu et remis à zéro par main.py à
# chaque cycle de scan (cf. bot.stats_latence_reponse()).
_temps_arrivee = {}
_latence_totale = 0.0
_latence_nb = 0


async def _marquer_arrivee(update: Update, context: ContextTypes.DEFAULT_TYPE):
    _temps_arrivee[update.update_id] = time.time()


async def _marquer_fin(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global _latence_totale, _latence_nb
    debut = _temps_arrivee.pop(update.update_id, None)
    if debut is not None:
        _latence_totale += time.time() - debut
        _latence_nb += 1


def stats_latence_reponse():
    """(moyenne_secondes, nb_mesures) depuis le dernier appel, puis remet
    les compteurs à zéro. (None, 0) si aucun update traité entre-temps."""
    global _latence_totale, _latence_nb
    if _latence_nb == 0:
        return None, 0
    moyenne, nb = _latence_totale / _latence_nb, _latence_nb
    _latence_totale, _latence_nb = 0.0, 0
    return moyenne, nb


# derniers chiffres connus (mis à jour par main.py à la fin de chaque
# cycle de scan) : sert UNIQUEMENT à /perf, pour un affichage à la
# demande sans attendre le rapport quotidien ni fouiller les logs.
_dernieres_stats = {
    "cycle_le": None,
    "notif_moyenne": None,
    "notif_nb": 0,
    "reponse_moyenne": None,
    "reponse_nb": 0,
    "cycles_jour": 0,
    "nouveaux_jour": 0,
    "envois_jour": 0,
}


def maj_stats_perf(**kwargs):
    """Appelé par main.py (scan_loop) à la fin de chaque cycle."""
    _dernieres_stats.update(kwargs)
    _dernieres_stats["cycle_le"] = datetime.now()


def _start_spamme(chat_id):
    """True = ce chat spamme /start, on l'ignore en silence."""
    maintenant = time.time()
    nb, debut, muet_jusqua = _starts_recents.get(chat_id, [0, maintenant, 0])

    if maintenant < muet_jusqua:
        return True
    if maintenant - debut > config.START_SPAM_WINDOW:
        nb, debut = 0, maintenant  # fenêtre expirée, on repart

    nb += 1
    if nb > config.START_SPAM_MAX:
        _starts_recents[chat_id] = [0, maintenant, maintenant + config.START_SPAM_MUTE]
        log.warning("Chat %s spamme /start -> ignoré %d min.",
                    chat_id, config.START_SPAM_MUTE // 60)
        return True
    _starts_recents[chat_id] = [nb, debut, 0]
    return False


# Anti-brute-force sur les codes :
# {chat_id: [nb_essais, bloque_jusqua_ts, dernier_essai_ts]}
# Seuls les essais RAPPROCHÉS comptent (spam) ; un essai isolé
# remet le compteur à zéro.
_tentatives = {}

# Alphabet sans caractères ambigus (pas de 0/O, 1/I/L)
_ALPHABET = "ABCDEFGHJKMNPQRSTUVWXYZ23456789"


def _nouveau_code():
    bloc = lambda: "".join(secrets.choice(_ALPHABET) for _ in range(4))
    return f"CROUS-{bloc()}-{bloc()}"


def _fmt_date(iso):
    return datetime.fromisoformat(iso).strftime("%d/%m/%Y à %H:%M")


def _derniere_actu(boursier=False):
    """Âge du dernier scan de la vue demandée, en clair ('il y a 2 min').
    Repli boursier -> non-boursier (comme les comptages). None = aucun scan."""
    iso = db.get_setting(f"last_scan_{'boursier' if boursier else 'non-boursier'}", None)
    if not iso and boursier:
        iso = db.get_setting("last_scan_non-boursier", None)
    if not iso:
        return None
    minutes = int((datetime.now() - datetime.fromisoformat(iso)).total_seconds() // 60)
    if minutes < 1:
        return "à l'instant"
    if minutes < 60:
        return f"il y a {minutes} min"
    return f"le {datetime.fromisoformat(iso).strftime('%d/%m à %H:%M')}"


def _ligne_acces(chat_id, expire):
    """La ligne '⏳ Accès : ...' de /start et /status — les admins ont
    un accès permanent, jamais affiché comme expiré."""
    if str(chat_id) in {str(a) for a in config.ADMIN_CHAT_IDS}:
        return "⏳ Accès : 👑 ADMIN (permanent)"
    if expire and expire > datetime.now().isoformat(timespec="seconds"):
        return f"⏳ Accès : ✅ actif jusqu'au {_fmt_date(expire)}"
    return "⏳ Accès : ❌ expiré — « Racheter un accès » pour renouveler"


def _ligne_dispo(ville_norm, ville, cp_filtre=None, boursier=False):
    """Ligne 'X logements actuellement à ...' pour rassurer à l'inscription.
    Le comptage suit la zone choisie ET la vue (boursier voit sa liste)."""
    rayon = parse_rayon(ville_norm)
    if rayon:
        nb = db.nb_logements_rayon(*rayon, boursier)
        zone = f"dans la zone « {ville} »"
    elif ville_norm.startswith("dept:"):
        nb = db.nb_logements_dept(ville_norm[5:], boursier)
        zone = f"dans le département {dept_label(ville_norm[5:])}"
    elif cp_filtre:
        nb = db.nb_logements_cp(cp_filtre, boursier)
        zone = f"à {format_zone(ville, cp_filtre)}"
    else:
        nb = db.nb_logements_ville(ville_norm, boursier)
        zone = f"à {ville.strip().title()}"
    if nb:
        return (f"\n📊 Il y a actuellement {nb} logement{'s' if nb > 1 else ''} "
                f"disponible{'s' if nb > 1 else ''} {zone}.")
    return (f"\n📊 Aucun logement disponible {zone} pour l'instant — "
            f"je te préviens dès qu'il y en a un.")


# chat_id (str) des admins actuellement en "mode test" : le temps de
# tester le parcours d'un abonné normal (verrouillage de zone, commandes
# bloquées...) sans avoir besoin d'un second compte Telegram. Volontairement
# en mémoire (pas en DB) : un redémarrage du bot repasse tout le monde
# admin par défaut, jamais l'inverse.
_admins_mode_test = set()


def _est_admin(update):
    chat_id = str(update.effective_chat.id)
    return (chat_id in {str(a) for a in config.ADMIN_CHAT_IDS}
            and chat_id not in _admins_mode_test)


def _identite(update):
    """(prénom, pseudo) Telegram de l'expéditeur — fournis gratuitement
    avec chaque message, aucune question à poser. Pseudo sans le @."""
    u = update.effective_user
    return (u.first_name if u else None), (u.username if u else None)


def _fmt_identite(prenom, pseudo):
    """'Wassim @wassim_x', ou '' si on ne sait rien."""
    return " ".join(x for x in (prenom, f"@{pseudo}" if pseudo else None) if x)


def _resoudre_cible(arg):
    """Argument de /who et /unsub : '123456789' ou '@pseudo' -> chat_id
    (str), ou None si le pseudo n'est pas connu en base."""
    arg = arg.strip()
    if arg.lstrip("-").isdigit():
        return arg
    return db.chat_id_par_pseudo(arg)


def _essai_gratuit_actif():
    """Réglage global : les nouveaux inscrits ont-ils droit à l'essai ?
    DÉSACTIVÉ par défaut — s'active avec /try <jours> (persistant)."""
    return db.get_setting("trial_enabled", "0") == "1"


def _essai_jours():
    """Durée de l'essai gratuit en jours (réglée par /try <jours>)."""
    return int(db.get_setting("trial_days", str(config.TRIAL_DAYS)))


async def _prevenir_admin(bot_api, texte):
    """Envoie un message à TOUS les admins (sauf si /mute). Ne plante jamais."""
    if db.get_setting("notif_inscriptions", "1") != "1":
        return
    for admin_id in config.ADMIN_CHAT_IDS:
        try:
            await bot_api.send_message(chat_id=admin_id, text=texte)
        except Exception:
            log.exception("Impossible de prévenir l'admin %s.", admin_id)


def _essayer_code(chat_id, texte):
    """
    Tente d'utiliser un code. Retourne le message à envoyer,
    ou None = chat bloqué, on l'ignore en silence total.
    """
    maintenant = time.time()
    essais, bloque_jusqua, dernier_essai = _tentatives.get(chat_id, [0, 0, 0])

    if maintenant < bloque_jusqua:
        return None  # en cooldown : silence

    # essais trop espacés = pas du spam -> on repart de zéro
    if maintenant - dernier_essai > config.CODE_ATTEMPT_WINDOW:
        essais = 0

    # normalisation tolérante : "crous k7x2 m9p4" -> "CROUS-K7X2-M9P4"
    brut = re.sub(r"[^A-Z0-9]", "", texte.strip().upper())
    if brut.startswith("CROUS") and len(brut) == 13:
        code = f"CROUS-{brut[5:9]}-{brut[9:13]}"
    else:
        code = texte.strip().upper()
    # consommation du code + accès accordé en UNE transaction (db)
    jours, fin = db.redeem_code(code, chat_id)

    if jours:
        _tentatives.pop(chat_id, None)
        log.info("Code %s utilisé par %s (+%d jours).", code, chat_id, jours)
        return (f"✅ Code accepté ! Ton accès est actif jusqu'au "
                f"{fin.strftime('%d/%m/%Y')}.\n\nBonne chasse au logement 🏠")

    essais += 1
    if essais >= config.CODE_MAX_ATTEMPTS:
        _tentatives[chat_id] = [0, maintenant + config.CODE_COOLDOWN, maintenant]
        log.warning("Chat %s bloqué %d min (codes invalides en rafale).",
                    chat_id, config.CODE_COOLDOWN // 60)
        return (f"🚫 Trop d'essais d'affilée. Réessaie dans "
                f"{config.CODE_COOLDOWN // 60} minutes.")
    _tentatives[chat_id] = [essais, 0, maintenant]
    return (f"❌ Code invalide ({essais}/{config.CODE_MAX_ATTEMPTS}). "
            f"Vérifie et renvoie-le.")


# ---------------- Inscription (/start) et modification (/changer) ----------------

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id

    if _start_spamme(chat_id):
        return ConversationHandler.END  # silence total

    user = db.get_user(chat_id)

    if user:  # déjà inscrit : pas de réinscription qui écrase tout
        boursier, expire = user
        db.update_identite(chat_id, *_identite(update))  # au cas où il a changé
        await update.effective_message.reply_text(
            f"Tu es déjà inscrit ✋\n\n"
            f"🏙️ Zones : {_zones_texte(db.get_subscriptions(chat_id))}\n"
            f"🎓 Boursier : {'oui' if boursier else 'non'}\n"
            f"{_ligne_acces(chat_id, expire)}\n\n"
            f"Pour changer de zones ou de statut : /changer",
            reply_markup=_clavier(chat_id),
        )
        return ConversationHandler.END

    context.user_data["mode"] = "inscription"
    # lien de démarrage "t.me/bot?start=early" -> tarif réservé à la
    # première vague ; sinon tarif normal (figé à l'inscription, cf. _fin_zones)
    context.user_data["tarif"] = (
        config.TARIF_EARLY if context.args and context.args[0] == "early"
        else config.TARIF_NORMAL
    )
    await update.effective_message.reply_text(
        "👋 Salut ! Je surveille les logements CROUS et je te préviens "
        "dès qu'un nouveau apparaît dans tes zones.\n\n"
        "🎓 Première question : es-tu boursier ? (ça change la liste "
        "des logements que je surveille pour toi)",
        reply_markup=CLAVIER_OUI_NON,
    )
    return BOURSIER


async def changer(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    if not db.get_user(chat_id):
        await update.effective_message.reply_text("Tu n'es pas encore inscrit : envoie /start !",
                                        reply_markup=CLAVIER_DECOUVERTE)
        return ConversationHandler.END

    # anti-prêt de compte à un(e) ami(e) dans une autre ville : /changer
    # est ouvert à tous, mais (sauf admin) la demande part en VÉRIFICATION
    # admin avant d'être réellement appliquée (cf. _fin_zones) — pas de
    # verrou en amont à gérer plus.
    context.user_data["mode"] = "changement"
    await update.effective_message.reply_text(
        "🎓 D'abord : es-tu boursier ? (ça change la liste des logements "
        "que je surveille pour toi)",
        reply_markup=CLAVIER_OUI_NON,
    )
    return BOURSIER


async def entrer_code(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Bouton '🎟 Entrer un code' : saisie directe si inscrit, sinon on
    fait d'abord la mini-inscription (le code sera demandé juste après)."""
    chat_id = update.effective_chat.id
    if db.get_user(chat_id):
        await update.effective_message.reply_text(
            "🎟 Envoie ton code d'activation (format CROUS-XXXX-XXXX)."
        )
        return CODE

    context.user_data["mode"] = "inscription"
    context.user_data["code_voulu"] = True
    await update.effective_message.reply_text(
        "Avant d'entrer ton code, deux petites questions (30 secondes) 🙂\n\n"
        "🎓 Es-tu boursier ? (ça change la liste des logements que je "
        "surveille pour toi)",
        reply_markup=CLAVIER_OUI_NON,
    )
    return BOURSIER


async def _fin_zones(update, context):
    """Zones choisies : le statut boursier est déjà connu (première
    question du parcours) -> on enregistre et on conclut directement."""
    chat_id = update.effective_chat.id
    boursier = context.user_data.get("boursier", False)
    zones = context.user_data["zones"]
    await update.effective_message.reply_text(
        "🗺 Zone(s) choisie(s) :\n" + "\n".join(f"• {z[0]}" for z in zones)
    )

    # --- Cas /changer : application immédiate pour tout le monde, plus
    # de vérification admin (trop lourd à valider à chaque fois). ---
    if context.user_data.get("mode") == "changement":
        db.update_boursier(chat_id, boursier)
        db.update_identite(chat_id, *_identite(update))
        db.set_subscriptions(chat_id, zones)
        texte = (f"🔄 C'est changé !\n\n"
                 f"🏙️ Zones : {_zones_texte(zones)}\n"
                 f"🎓 Boursier : {'oui' if boursier else 'non'}\n"
                 f"{_lignes_dispo_zones(zones, boursier)}\n\n"
                 f"Les notifications de ton ancienne configuration sont arrêtées.")
        user = db.get_user(chat_id)
        expire = user[1] if user else None
        if not _est_admin(update) and not (expire and expire > datetime.now().isoformat(timespec="seconds")):
            texte += ("\n\n⚠️ Attention : ton accès est expiré, tu ne recevras "
                      "aucune notification tant que tu ne l'as pas renouvelé "
                      "(« 🔎 Trouver mon logement »).")
        await update.effective_message.reply_text(texte, reply_markup=_clavier(chat_id))
        log.info("Changement : chat %s -> %s (boursier=%s)",
                 chat_id, _zones_texte(zones), boursier)
        return ConversationHandler.END

    # --- Cas /start : inscription + essai gratuit ou code ---
    tarif = context.user_data.pop("tarif", config.TARIF_NORMAL)
    db.add_user(chat_id, boursier, *_identite(update), tarif=tarif)
    db.set_subscriptions(chat_id, zones)
    log.info("Inscription : chat %s -> %s (boursier=%s)",
             chat_id, _zones_texte(zones), boursier)

    essai_accorde = _essai_gratuit_actif() and not db.essai_deja_utilise(chat_id)

    # notification admin : chaque nouvel inscrit, avec chat_id et date/heure
    user_tg = update.effective_user
    pseudo = f"@{user_tg.username}" if user_tg.username else "(pas de pseudo)"
    await _prevenir_admin(
        context.bot,
        f"🆕 Nouvel inscrit — {datetime.now().strftime('%d/%m/%Y %H:%M')}\n"
        f"👤 {user_tg.first_name or '?'} {pseudo}\n"
        f"💬 chat_id : {chat_id}\n"
        f"🏙️ {_zones_texte(zones)} — 🎓 boursier : {'oui' if boursier else 'non'}\n"
        f"🎁 essai : {'accordé' if essai_accorde else 'NON (code requis)'}"
    )

    if essai_accorde:
        db.marquer_essai(chat_id)
        jours = _essai_jours()
        fin = db.grant_access(chat_id, jours)
        await update.effective_message.reply_text(
            f"🎁 Bienvenue ! Tu profites de {jours} jour{'s' if jours > 1 else ''} "
            f"d'essai GRATUIT (jusqu'au {fin.strftime('%d/%m/%Y')}).\n\n"
            f"🏙️ Zones : {_zones_texte(zones)}\n"
            f"🎓 Boursier : {'oui' if boursier else 'non'}\n"
            f"{_lignes_dispo_zones(zones, boursier)}\n\n"
            f"Je te préviens dès qu'un nouveau logement apparaît. "
            f"Les boutons ci-dessous sont là pour t'aider 👇",
            reply_markup=CLAVIER_PRINCIPAL,
        )
        if context.user_data.pop("code_voulu", False):
            # il était venu pour entrer un code : on lui propose de le
            # mettre tout de suite, il PROLONGERA son essai
            await update.effective_message.reply_text(
                "🎟 Tu peux maintenant envoyer ton code : il s'ajoutera "
                "à ton essai gratuit."
            )
            return CODE
        return ConversationHandler.END

    # pas d'essai (déjà consommé, ou désactivé par /try) -> paiement direct
    await update.effective_message.reply_text(
        f"✅ C'est noté : {_zones_texte(zones)}, "
        f"boursier {'oui' if boursier else 'non'}."
        f"{_lignes_dispo_zones(zones, boursier)}\n\n"
        "💰 Pour activer tes notifications, appuie sur « 💰 Racheter un "
        "accès » ci-dessous : le paiement se fait en direct, sans code à "
        "saisir.",
        reply_markup=_clavier(chat_id),
    )
    return CODE


async def recevoir_region(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Choix Île-de-France / Autre."""
    choix = update.effective_message.text.strip()

    if choix == BTN_IDF:
        await update.effective_message.reply_text(LISTE_DEPTS_IDF)
        return DEPTS
    if choix == BTN_AUTRE:
        await update.effective_message.reply_text("🏙️ Quelle ville veux-tu surveiller ?")
        return VILLE

    # tolérance : il a tapé directement une ville ou un département
    if choix not in TOUS_BOUTONS and parse_city_query(choix)[0]:
        return await recevoir_ville(update, context)

    await update.effective_message.reply_text(
        "Choisis avec les boutons 🙂", reply_markup=CLAVIER_REGION
    )
    return REGION


async def _vers_idf(update, context, dept):
    """Saisie directe d'une ville ou d'un département d'Île-de-France :
    on aiguille vers le parcours IDF comme si ce département était déjà
    choisi (l'IDF se surveille par département, pas par ville/rayon)."""
    if dept == "75":
        context.user_data["zones"] = []  # remplies par la réponse arrondissements
        await update.effective_message.reply_text(QUESTION_ARRS)
        return ARRS
    context.user_data["zones"] = [(dept_label(dept), f"dept:{dept}", None)]
    return await _fin_zones(update, context)


async def recevoir_ville(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Hors Île-de-France : UNE zone (ville, ou "ville + numéro" toléré).
    Une saisie francilienne (ville, "75", "92"...) rebascule sur le
    parcours Île-de-France."""
    saisie = update.effective_message.text.strip()

    # boutons utiles même pendant la saisie de la ville
    if saisie == BTN_VILLES:
        await villes(update, context)  # aide à choisir, on reste sur la question
        return VILLE
    if saisie == BTN_CONTACT:
        await update.effective_message.reply_text(config.CONTACT_TEXT)
        return VILLE
    if saisie in TOUS_BOUTONS:
        await update.effective_message.reply_text(
            "❌ Je n'ai pas compris, redonne-moi un nom de ville."
        )
        return VILLE

    notes = []
    if "," in saisie:  # une seule zone ici : on garde la première
        saisie = saisie.split(",")[0].strip()
        notes.append("ℹ️ Une seule ville ici — je garde la première.")

    ville_norm, cp_filtre, arr_invalide = parse_city_query(saisie)
    if not ville_norm:
        await update.effective_message.reply_text(
            "❌ Je n'ai pas compris, redonne-moi un nom de ville."
        )
        return VILLE

    # "île de france" / "idf" en toutes lettres : la liste des départements
    if ville_norm in ("ile-de-france", "idf"):
        await update.effective_message.reply_text(LISTE_DEPTS_IDF)
        return DEPTS

    # département ou arrondissement précis : pas de question de portée
    if ville_norm.startswith("dept:"):
        dept = ville_norm[5:]
        if dept in _DEPTS_IDF:  # "75", "92"... -> parcours Île-de-France
            return await _vers_idf(update, context, dept)
        context.user_data["zones"] = [(dept_label(dept), ville_norm, None)]
        return await _fin_zones(update, context)
    if cp_filtre:
        if notes:
            await update.effective_message.reply_text("\n".join(notes))
        context.user_data["zones"] = [(format_zone(saisie, cp_filtre),
                                       ville_norm, cp_filtre)]
        return await _fin_zones(update, context)

    # ville simple : vérification d'existence via l'API officielle des
    # communes (gère aussi les fautes de frappe : "bordeau" -> Bordeaux)
    geo = await asyncio.to_thread(scraper.geocoder_ville, saisie)
    if not geo:
        await update.effective_message.reply_text(
            "🤔 Je n'ai pas bien compris ta demande — cette ville m'est "
            "inconnue. Peux-tu la réitérer ?\n"
            "En cas de souci, contacte-nous : bouton 💬 Contact."
        )
        return VILLE
    lat, lon, nom_officiel, dept = geo
    affichage = nom_officiel
    ville_norm = normalize_city(nom_officiel)

    if arr_invalide:
        notes.append(f"ℹ️ Cet arrondissement n'existe pas — je te mets "
                     f"sur tout {affichage}.")
    if notes:
        await update.effective_message.reply_text("\n".join(notes))

    # ville francilienne tapée directement : offre Île-de-France
    if dept in _DEPTS_IDF:
        if dept != "75":  # pour Paris, la question arrondissements suffit
            await update.effective_message.reply_text(
                f"🗼 {affichage} est en Île-de-France : là-bas je surveille "
                f"par département — je te mets sur tout le département "
                f"{dept_label(dept)}, {affichage} inclus."
            )
        return await _vers_idf(update, context, dept)

    context.user_data["ville_choisie"] = (affichage, ville_norm)
    context.user_data["centre"] = (lat, lon)
    await update.effective_message.reply_text(
        f"📍 {affichage}, c'est noté. Tu veux surveiller quoi ?",
        reply_markup=CLAVIER_PORTEE,
    )
    return PORTEE


async def recevoir_portee(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Ville seule / + alentours (rayon) / + villes précises des environs."""
    choix = update.effective_message.text.strip()
    affichage, ville_norm = context.user_data["ville_choisie"]
    lat, lon = context.user_data["centre"]
    boursier = context.user_data.get("boursier", False)

    if choix == BTN_SEULE:
        context.user_data["zones"] = [(affichage, ville_norm, None)]
        return await _fin_zones(update, context)

    if choix == BTN_RAYON:
        context.user_data["zones"] = [(
            f"{affichage} + {config.RAYON_KM} km",
            f"rayon:{lat:.4f},{lon:.4f},{config.RAYON_KM}",
            None,
        )]
        return await _fin_zones(update, context)

    if choix == BTN_PROCHES:
        proches = db.villes_proches(lat, lon, config.RAYON_KM,
                                    boursier=boursier, exclure=ville_norm)
        if not proches:
            # rien autour : on dit QUELLE est la ville à CROUS la plus
            # proche et à quelle distance, puis rayon (couvre le futur)
            plus_proche = db.ville_plus_proche(lat, lon, exclure=ville_norm,
                                               boursier=boursier)
            if plus_proche:
                ville_p, dist_p = plus_proche
                await update.effective_message.reply_text(
                    f"ℹ️ Aucun CROUS dans une autre ville à moins de "
                    f"{config.RAYON_KM} km. La plus proche est "
                    f"{ville_p}, à ~{dist_p:.0f} km — trop loin.\n"
                    f"Je te mets sur « {affichage} + alentours "
                    f"({config.RAYON_KM} km) » : tu es couvert si une "
                    f"résidence apparaît dans le coin."
                )
            else:
                await update.effective_message.reply_text(
                    f"ℹ️ Aucune autre ville avec des logements CROUS à "
                    f"moins de {config.RAYON_KM} km — je te mets sur "
                    f"« {affichage} + alentours » pour ne rien rater."
                )
            context.user_data["zones"] = [(
                f"{affichage} + {config.RAYON_KM} km",
                f"rayon:{lat:.4f},{lon:.4f},{config.RAYON_KM}",
                None,
            )]
            return await _fin_zones(update, context)

        proches = proches[:10]
        context.user_data["proches"] = proches
        lignes = [
            f"{i}. {ville} — "
            + (f"{nb} logement{'s' if nb > 1 else ''} dispo"
               if nb else "complet actuellement")
            + f" (à ~{d} km)"
            for i, (ville, _, nb, d) in enumerate(proches, start=1)
        ]
        actu = _derniere_actu(boursier)
        await update.effective_message.reply_text(
            f"🎯 Villes avec un CROUS autour de {affichage} :\n\n"
            + "\n".join(lignes)
            + (f"\n\n⏱ Disponibilités du dernier scan ({actu})." if actu else "")
            + f"\n\nEnvoie les numéros de celles que tu veux AJOUTER "
              f"(max {config.MAX_PROCHES}), séparés par des virgules — "
              f"ex : 1, 3"
        )
        return PROCHES

    await update.effective_message.reply_text(
        "Choisis avec les boutons 🙂", reply_markup=CLAVIER_PORTEE
    )
    return PORTEE


async def recevoir_proches(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Sélection des villes voisines par leurs numéros."""
    saisie = update.effective_message.text.strip()
    proches = context.user_data.get("proches", [])
    affichage, ville_norm = context.user_data["ville_choisie"]

    choisis, deja = [], set()
    for n in re.findall(r"\d+", saisie):
        i = int(n)
        if 1 <= i <= len(proches) and i not in deja:
            deja.add(i)
            choisis.append(proches[i - 1])

    if not choisis:
        await update.effective_message.reply_text(
            "❌ Je n'ai pas compris. Envoie les numéros de la liste, "
            "séparés par des virgules — ex : 1, 3"
        )
        return PROCHES

    if len(choisis) > config.MAX_PROCHES:
        await update.effective_message.reply_text(
            f"ℹ️ Maximum {config.MAX_PROCHES} villes en plus : je garde "
            f"les {config.MAX_PROCHES} premières."
        )
        choisis = choisis[:config.MAX_PROCHES]

    # dédoublonnage de sécurité : jamais deux fois la même zone
    zones = [(affichage, ville_norm, None)]
    deja_zones = {ville_norm}
    for ville, norm, _, _ in choisis:
        if norm not in deja_zones:
            deja_zones.add(norm)
            zones.append((ville, norm, None))
    context.user_data["zones"] = zones
    return await _fin_zones(update, context)


async def recevoir_depts(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Île-de-France : choix des départements (7 max)."""
    saisie = update.effective_message.text.strip()

    tokens = re.findall(r"\d+", saisie)
    if not tokens:
        await update.effective_message.reply_text(
            "❌ Je n'ai reconnu aucun département d'Île-de-France.\n"
            "Envoie les numéros séparés par des virgules — ex : 75, 92, 93"
        )
        return DEPTS

    # un seul numéro hors Île-de-France -> on rejette TOUTE la saisie,
    # pas de sélection partielle silencieuse : il renvoie la liste complète
    rejetes = [n for n in tokens if n not in _DEPTS_IDF]
    if rejetes:
        await update.effective_message.reply_text(
            f"❌ Pas en Île-de-France : {', '.join(rejetes)}.\n"
            f"Les départements possibles sont 75, 77, 78, 91, 92, 93, 94, 95 — "
            f"renvoie la liste complète avec uniquement ces numéros."
        )
        return DEPTS

    depts, deja = [], set()
    for n in tokens:
        if n not in deja:
            deja.add(n)
            depts.append(n)

    if len(depts) > config.MAX_DEPTS:
        await update.effective_message.reply_text(
            f"ℹ️ Maximum {config.MAX_DEPTS} départements : je garde les "
            f"{config.MAX_DEPTS} premiers."
        )
        depts = depts[:config.MAX_DEPTS]

    # les départements hors Paris deviennent directement des zones
    context.user_data["zones"] = [
        (dept_label(d), f"dept:{d}", None) for d in depts if d != "75"
    ]

    if "75" in depts:
        await update.effective_message.reply_text(QUESTION_ARRS)
        return ARRS

    return await _fin_zones(update, context)


async def recevoir_arrs(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Paris choisi : arrondissements précis (7 max) ou tout Paris."""
    saisie = update.effective_message.text.strip().lower()
    zones = context.user_data.get("zones", [])

    if saisie in ("non", "no", "tout", "tous", "tout paris"):
        zones.append((dept_label("75"), "dept:75", None))
        context.user_data["zones"] = zones
        return await _fin_zones(update, context)

    tokens = re.findall(r"\d{1,2}", saisie)
    if not tokens:
        await update.effective_message.reply_text(
            "❌ Je n'ai pas compris. Envoie des numéros d'arrondissement "
            "(1 à 20) séparés par des virgules — ex : 18, 19 — ou « non » "
            "pour tout Paris."
        )
        return ARRS

    # un seul numéro hors 1-20 -> on rejette TOUTE la saisie, pas de
    # sélection partielle silencieuse : il renvoie la liste complète
    rejetes = [n for n in tokens if not (1 <= int(n) <= 20)]
    if rejetes:
        await update.effective_message.reply_text(
            f"❌ Arrondissement(s) inexistant(s) : {', '.join(rejetes)}.\n"
            f"Paris va de 1 à 20 — renvoie la liste complète avec "
            f"uniquement ces numéros, ou « non » pour tout Paris."
        )
        return ARRS

    arrs, deja = [], set()
    for n in tokens:
        v = int(n)
        if v not in deja:
            deja.add(v)
            arrs.append(v)

    if len(arrs) > config.MAX_ARRS:
        await update.effective_message.reply_text(
            f"ℹ️ Maximum {config.MAX_ARRS} arrondissements : je garde les "
            f"{config.MAX_ARRS} premiers."
        )
        arrs = arrs[:config.MAX_ARRS]

    zones += [(f"Paris {v}{'er' if v == 1 else 'e'}", "paris", f"750{v:02d}")
              for v in arrs]
    context.user_data["zones"] = zones
    return await _fin_zones(update, context)


async def recevoir_boursier(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """PREMIÈRE question du parcours : la vue (boursier/non) est fixée
    d'emblée, tous les comptages affichés ensuite utilisent la bonne liste."""
    reponse = update.effective_message.text.strip().lower()
    if reponse not in ("oui", "non"):
        await update.effective_message.reply_text("Réponds par Oui ou Non 🙂", reply_markup=CLAVIER_OUI_NON)
        return BOURSIER

    context.user_data["boursier"] = reponse == "oui"
    maintenant = " maintenant" if context.user_data.get("mode") == "changement" else ""
    await update.effective_message.reply_text(
        f"📍 Où cherches-tu ton logement{maintenant} ?\n"
        f"(🗼 IDF est un choix possible)",
        reply_markup=CLAVIER_REGION,
    )
    return REGION


# ce qui ressemble à un code (tolère minuscules et espaces autour)
_FORMAT_CODE = re.compile(r"crous[\s-]*[a-z0-9]{4}[\s-]*[a-z0-9]{4}", re.I)


async def recevoir_code(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    texte = update.effective_message.text.strip()

    # le bouton Contact fait partie du parcours code : on reste dessus
    if texte == BTN_CONTACT:
        await update.effective_message.reply_text(config.CONTACT_TEXT)
        return CODE
    # relance du bouton code : on redemande simplement
    if texte == BTN_CODE:
        await update.effective_message.reply_text("🎟 J'écoute : envoie ton code (CROUS-XXXX-XXXX).")
        return CODE
    # les autres boutons reprennent le dessus : on quitte la saisie du code
    if texte == BTN_CHANGER:
        return await changer(update, context)
    if texte in TOUS_BOUTONS:
        await update.effective_message.reply_text("OK, on laisse le code de côté 🙂")
        if texte == BTN_STATUT:
            await status(update, context)
        elif texte == BTN_VILLES:
            await villes(update, context)
        elif texte in (BTN_OFFRE, BTN_RACHAT, BTN_PAYER_AVANCE):
            await offre(update, context)
        return ConversationHandler.END
    # sortie explicite en toutes lettres
    if texte.lower() in ("annuler", "retour", "stop", "cancel"):
        return await annuler(update, context)

    # pas la forme d'un code -> aide, SANS compter de tentative
    if not _FORMAT_CODE.fullmatch(texte):
        await update.effective_message.reply_text(
            "🤔 Ça ne ressemble pas à un code (format CROUS-XXXX-XXXX)."
        )
        return CODE

    message = _essayer_code(chat_id, texte)
    if message is None:
        return CODE  # chat en cooldown : silence total
    # après une activation réussie, le clavier se met à jour (plus de rachat)
    await update.effective_message.reply_text(message, reply_markup=_clavier(chat_id))
    return ConversationHandler.END if message.startswith("✅") else CODE


async def annuler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.effective_message.reply_text(
        "OK, on s'arrête là. (/start quand tu veux pour reprendre)",
        reply_markup=CLAVIER_PRINCIPAL,
    )
    return ConversationHandler.END


async def stop_conv(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/stop reçu en PLEINE conversation : on désinscrit ET on ferme la
    conversation (sinon l'état resterait actif et le prochain message
    reprendrait l'inscription là où elle en était)."""
    await stop(update, context)
    return ConversationHandler.END


# ---------------- Activation / renouvellement ----------------

async def activer(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    if not db.get_user(chat_id):
        await update.effective_message.reply_text("Inscris-toi d'abord avec /start !")
        return
    if not context.args:
        await update.effective_message.reply_text("Utilisation : /activer CROUS-XXXX-XXXX")
        return

    # " ".join : un code collé avec des espaces ("CROUS K7X2 M9P4") reste
    # un seul essai, au lieu de compter une tentative invalide sur "CROUS"
    message = _essayer_code(chat_id, " ".join(context.args))
    if message is not None:  # None = cooldown, silence
        await update.effective_message.reply_text(message, reply_markup=_clavier(chat_id))


# ---------------- Admin ----------------

async def admin_aide(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/admin : le mémo des commandes admin. Réservé aux admins (les
    autres sont ignorés en silence, comme pour toutes les commandes
    admin). Un admin ne s'ajoute QUE via ADMIN_CHAT_IDS dans config.py."""
    if not _est_admin(update):
        return  # silence total

    await update.effective_message.reply_text(
        "👑 Commandes admin\n\n"
        "🎟 /gencode [jours] — génère un code d'activation "
        f"({config.CODE_DEFAULT_DAYS} j par défaut, 365 max)\n"
        "🧹 /gencode clear — invalide tous les codes en circulation\n"
        "🎁 /try <jours> — active l'essai gratuit pour chaque nouvel "
        "inscrit (1-90 j) · /try off le coupe · /try seul = état actuel\n"
        "📊 /stats — vue d'ensemble : inscrits, accès actifs, codes, "
        "top 3 des zones\n"
        "🩺 /perf — latence notifs/réponses et volumes du jour, à la demande\n"
        "👥 /sub — liste détaillée de tous les inscrits\n"
        "🔎 /who <chat_id ou @pseudo> — fiche d'une personne (prénom, "
        "pseudo, zones, accès, essai)\n"
        "✉️ /msg <chat_id ou @pseudo> <message> — envoie un message libre "
        "à un client précis\n"
        "🎁 /essai <chat_id ou @pseudo> — accorde l'essai gratuit standard "
        "à ce client précis\n"
        "💰 /payer <chat_id ou @pseudo> — accorde l'accès payant complet "
        "(comme un \"Reçu\"), si payé autrement que via le bot\n"
        "🗑 /unsub <chat_id ou @pseudo> — supprime l'inscription de quelqu'un\n"
        "📍 /setzone <chat_id ou @pseudo> <ville/dept/CP> — corrige la zone "
        "de quelqu'un directement (zone simple)\n"
        "🔄 /changer est ouvert à tous, application immédiate, sans "
        "validation admin\n"
        "🎁 /finessai — demande à tous les essayeurs gratuits actifs s'ils "
        "ont eu une notif ; \"Oui\" coupe leur essai et pousse au paiement\n"
        "🔔 /rappelessai — confirme à tous les essayeurs gratuits actifs "
        "que les notifs marchent bien, avec rappel de leur date d'expiration\n"
        "🔥 /promo <prix> <heures> — offre flash pour les inscrits "
        "expirés/jamais activés, retour au tarif normal à l'expiration\n"
        "⏳ /relance — relance ceux dont l'accès est expiré, avec le "
        "nombre réel de logements ratés dans leur secteur\n"
        "📣 /annonce <texte> — broadcast à tous les accès actifs "
        "(multi-lignes OK)\n"
        "🔕 /mute · 🔔 /unmute — coupe/rétablit les notifications "
        "d'inscription\n"
        "🔇 /silence on|off — active/désactive le son sur les notifs de "
        "logements (pour tous les abonnés)\n"
        "🪪 /id — affiche ton chat_id\n"
        "🧪 /modetest — bascule TON compte en abonné normal pour tester "
        "le parcours (retape /modetest pour repasser admin)\n\n"
        "Un admin s'ajoute uniquement dans config.py (ADMIN_CHAT_IDS) "
        "+ redémarrage."
    )


async def modetest(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/modetest : bascule TON propre compte admin en "abonné normal" le
    temps de tester le parcours (inscription, verrouillage de zone,
    commandes admin bloquées...) sans avoir besoin d'un second compte
    Telegram. Retape /modetest pour repasser admin.

    Vérifie directement ADMIN_CHAT_IDS (pas _est_admin) : sinon, une fois
    en mode test, cette commande se bloquerait elle-même et il serait
    impossible de repasser admin."""
    chat_id = str(update.effective_chat.id)
    if chat_id not in {str(a) for a in config.ADMIN_CHAT_IDS}:
        return  # silence total : jamais un admin, jamais accès à ceci

    if chat_id in _admins_mode_test:
        _admins_mode_test.discard(chat_id)
        await update.effective_message.reply_text(
            "👑 Mode admin réactivé — toutes tes commandes admin refonctionnent."
        )
    else:
        _admins_mode_test.add(chat_id)
        await update.effective_message.reply_text(
            "🧪 Mode test activé — tu es maintenant traité comme un abonné "
            "normal (les commandes admin sont bloquées pour toi, le "
            "verrouillage de zone s'applique...).\n"
            "Retape /modetest pour repasser admin."
        )


async def gencode(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Génère un code d'activation. RÉSERVÉ à l'admin : tout autre
    chat est ignoré sans la moindre réponse."""
    if not _est_admin(update):
        return  # silence total

    # /gencode clear : invalide tous les codes pas encore utilisés
    if context.args and context.args[0].lower() == "clear":
        n = db.delete_unused_codes()
        log.warning("Admin %s a invalidé %d code(s) en circulation.",
                    update.effective_chat.id, n)
        await update.effective_message.reply_text(
            f"🗑 {n} code(s) en circulation invalidé(s).\n"
            f"(les codes déjà utilisés sont conservés pour la traçabilité, "
            f"et les accès actifs ne sont pas touchés)"
        )
        return

    jours = config.CODE_DEFAULT_DAYS
    if context.args:
        try:
            jours = int(context.args[0])
            assert 1 <= jours <= 365  # garde-fou (évite un /gencode 8122439272)
        except (ValueError, AssertionError):
            await update.effective_message.reply_text(
                "Utilisation : /gencode [jours entre 1 et 365] ou /gencode clear"
            )
            return

    code = _nouveau_code()
    db.create_code(code, jours)
    # <code> = monospace Telegram : un tap sur le code le copie
    await update.effective_message.reply_text(
        f"🎟 Nouveau code ({jours} jours) :\n\n<code>{code}</code>",
        parse_mode=ParseMode.HTML,
    )


async def resetdemo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/resetdemo (admin) : referme TA propre candidature en cours (si tu
    as répondu "Oui" à une /fakenotif pendant une démo) pour pouvoir
    retaper "Oui" à une prochaine prise sans attendre le lendemain 14h.
    Reverrouillé avant le vrai lancement — ne touche qu'à TA propre
    candidature, pas celle d'un autre abonné."""
    if not _est_admin(update):
        return  # silence total

    chat_id = str(update.effective_chat.id)
    if db.a_candidature_en_cours(chat_id):
        db.resoudre_candidature(chat_id)
        await update.effective_message.reply_text(
            "🔄 Candidature de démo refermée — tu peux retaper « ✅ Oui » "
            "sur une prochaine /fakenotif.")
    else:
        await update.effective_message.reply_text(
            "Aucune candidature en cours à refermer pour toi.")


async def fakenotif(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/fakenotif [ville] (admin) : envoie une notification de logement
    FACTICE, à toi-même, pour filmer une démo sans attendre une vraie
    libération (utile quand le CROUS a fermé ses annonces avant la
    prochaine campagne). Réutilise EXACTEMENT les mêmes fonctions que la
    production (notifier.send_listing_with_photo) : rendu identique à
    l'écran, boutons "Tu l'as eue ?" bien fonctionnels.

    ATTENTION : si tu réponds "Oui" dessus, ça ouvre une VRAIE candidature
    (table candidatures) qui déclenchera une VRAIE relance le lendemain —
    pense à répondre "Non" à la fin de la prise, ou attends la relance
    du lendemain pour la refermer (/resetdemo).

    N'écrit dans AUCUNE table de scan/diff (known_listings, villes_vues...)
    : zéro effet de bord sur la détection réelle des logements.

    Reverrouillé avant le vrai lancement (n'était ouvert à tous que le
    temps du tournage/démo)."""
    if not _est_admin(update):
        return  # silence total

    ville = " ".join(context.args) if context.args else "Nantes"
    logement = {
        "id": "demo-" + secrets.token_hex(3),
        "titre": "Résidence Simulateur — T1",
        "adresse": f"12 Rue de la Démo 44000 {ville.title()}",
        "cp": "44000",
        "ville": ville.title(),
        "ville_norm": normalize_city(ville),
        "prix": "268,78 €",
        "surface": "9 m²",
        "details": ["Individuel"],
        "tres_demande": True,
        "dernieres_places": False,
        "disponible": True,
        "photo": None,
        "lat": None,
        "lon": None,
        "lien": "https://trouverunlogement.lescrous.fr/tools/45/accommodations/0",
    }
    horodatage = datetime.now().strftime("%d/%m/%Y %H:%M")
    caption = notifier.format_notification([logement], f"à {logement['ville']}", horodatage)
    await notifier.send_listing_with_photo(
        context.bot, update.effective_chat.id, logement, caption)


async def essai_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/try (admin) — essai gratuit des nouveaux inscrits :
       /try           -> affiche l'état actuel
       /try <jours>   -> active l'essai de X jours (1 à 90)
       /try off       -> désactive (c'est aussi l'état par défaut)
    Pour offrir un essai à UNE personne : /gencode 7."""
    if not _est_admin(update):
        return  # silence total

    if not context.args:  # état actuel
        if _essai_gratuit_actif():
            await update.effective_message.reply_text(
                f"🎁 Essai gratuit : ACTIVÉ ({_essai_jours()} jours pour "
                f"chaque nouvel inscrit).\n/try off pour désactiver."
            )
        else:
            await update.effective_message.reply_text(
                "🚫 Essai gratuit : DÉSACTIVÉ (les nouveaux inscrits "
                "doivent entrer un code).\n/try <jours> pour activer."
            )
        return

    arg = context.args[0].lower()
    if arg in ("off", "non", "0"):
        db.set_setting("trial_enabled", "0")
        await update.effective_message.reply_text(
            "🚫 Essai gratuit DÉSACTIVÉ : les nouveaux inscrits devront entrer un code."
        )
        return

    try:
        jours = int(arg)
        assert 1 <= jours <= 90  # garde-fou (évite un /try 8122439272)
    except (ValueError, AssertionError):
        await update.effective_message.reply_text(
            "Utilisation : /try <jours entre 1 et 90>, /try off, ou /try seul (état)."
        )
        return

    db.set_setting("trial_enabled", "1")
    db.set_setting("trial_days", str(jours))
    await update.effective_message.reply_text(
        f"🎁 Essai gratuit ACTIVÉ : {jours} jour{'s' if jours > 1 else ''} "
        f"offert{'s' if jours > 1 else ''} à chaque nouvel inscrit."
    )


async def stats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/stats (admin) : tableau de bord — abonnés, codes, top villes."""
    if not _est_admin(update):
        return  # silence total

    s = db.get_stats()
    lignes = [
        "📊 Tableau de bord (vue d'ensemble)",
        "",
        f"👥 Inscrits : {s['total']}",
        f"   ✅ {s['actifs']} avec un accès actif (essai ou code)",
        f"   ❌ {s['expires']} sans accès (expiré ou jamais activé)",
        f"🎁 Essais gratuits consommés : {s['essais']}",
        f"🎟 Codes activés (achetés/utilisés) : {s['codes_utilises']}",
        f"🎫 Codes en circulation (générés, pas encore utilisés) : {s['codes_libres']}",
    ]
    if s["top_villes"]:
        lignes += ["", "🏆 Top 3 des zones (abonnés actifs) :"]
        lignes += [f"  {i}. {ville} — {nb}"
                   for i, (ville, nb) in enumerate(s["top_villes"], start=1)]
    await update.effective_message.reply_text("\n".join(lignes))


async def perf(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/perf (admin) : derniers chiffres connus de latence (notifs de
    logements + réponses aux commandes) et volumes du jour — affiché à
    la demande, sans attendre le rapport quotidien ni fouiller les logs.
    Mis à jour à chaque cycle de scan (~2 min) par main.py."""
    if not _est_admin(update):
        return  # silence total

    s = _dernieres_stats
    if s["cycle_le"] is None:
        await update.effective_message.reply_text(
            "🩺 Pas encore de cycle de scan terminé depuis le démarrage du bot."
        )
        return

    def _fmt(moyenne, nb, seuil):
        if moyenne is None:
            return f"pas assez de données ({nb}/{config.LATENCE_ECHANTILLON_MIN} min)"
        etat = "✅" if moyenne <= seuil else "🐢"
        return f"{etat} {moyenne:.2f}s (sur {nb}, seuil {seuil}s)"

    lignes = [
        "🩺 <b>Perf — dernier cycle connu</b>",
        f"🕐 Mis à jour : {s['cycle_le'].strftime('%H:%M:%S')}",
        "",
        f"📤 Notifs de logements : "
        f"{_fmt(s['notif_moyenne'], s['notif_nb'], config.SEUIL_LATENCE_NOTIF)}",
        f"💬 Réponses aux commandes : "
        f"{_fmt(s['reponse_moyenne'], s['reponse_nb'], config.SEUIL_LATENCE_REPONSE)}",
        "",
        f"🔄 {s['cycles_jour']} cycle(s) aujourd'hui",
        f"🆕 {s['nouveaux_jour']} nouveau(x) logement(s) détecté(s)",
        f"📬 {s['envois_jour']} notification(s) envoyée(s)",
    ]
    await update.effective_message.reply_text(
        "\n".join(lignes), parse_mode=ParseMode.HTML)


async def annonce(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/annonce <texte> (admin) : broadcast à TOUS les inscrits dont
    l'accès est actif — essai gratuit ou code, peu importe (rate-limité).
    Ex : /annonce La phase complémentaire ouvre demain !"""
    if not _est_admin(update):
        return  # silence total

    # texte brut après la commande (et pas context.args, qui perdrait
    # les sauts de ligne d'un message d'annonce multi-lignes)
    morceaux = update.effective_message.text.split(None, 1)
    if len(morceaux) < 2 or not morceaux[1].strip():
        await update.effective_message.reply_text("Utilisation : /annonce Ton message ici")
        return

    texte = "📣 " + morceaux[1].strip()
    destinataires = db.get_active_chat_ids()
    for chat_id in destinataires:
        await notifier.send(context.bot, chat_id, texte)
    await update.effective_message.reply_text(
        f"✅ Annonce envoyée à {len(destinataires)} abonné(s) actif(s)."
    )


async def sub(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/sub (admin) : la liste complète des inscrits avec tout ce qu'on sait."""
    if not _est_admin(update):
        return  # silence total

    users = db.get_all_users()
    if not users:
        await update.effective_message.reply_text("Aucun inscrit pour le moment.")
        return

    maintenant = datetime.now().isoformat(timespec="seconds")
    admins = {str(a) for a in config.ADMIN_CHAT_IDS}
    lignes = [f"👥 {len(users)} inscrit(s) :", ""]
    for chat_id, boursier, inscrit_le, expire_le, prenom, pseudo in users:
        zones = db.get_subscriptions(chat_id)
        if chat_id in admins:
            acces = "👑 ADMIN"
        elif expire_le and expire_le > maintenant:
            acces = f"✅ jusqu'au {datetime.fromisoformat(expire_le).strftime('%d/%m %H:%M')}"
        elif expire_le:
            acces = f"❌ expiré le {datetime.fromisoformat(expire_le).strftime('%d/%m')}"
        else:
            acces = "⭕ jamais activé"
        essai = " 🎁essai-utilisé" if db.essai_deja_utilise(chat_id) else ""
        qui = _fmt_identite(prenom, pseudo)
        lignes.append(
            f"💬 {chat_id}" + (f" — 👤 {qui}" if qui else "") + "\n"
            f"   🏙️ {_zones_texte(zones)}\n"
            f"   🎓 {'boursier' if boursier else 'non-boursier'} | {acces}{essai}\n"
            f"   📅 inscrit le {datetime.fromisoformat(inscrit_le).strftime('%d/%m/%Y %H:%M')}"
        )

    # découpage : Telegram limite un message à 4096 caractères
    bloc = ""
    for ligne in lignes:
        if len(bloc) + len(ligne) > 3500:
            await update.effective_message.reply_text(bloc)
            bloc = ""
        bloc += ligne + "\n"
    if bloc:
        await update.effective_message.reply_text(bloc)


async def unsub(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/unsub <chat_id> (admin) : supprime l'inscription de quelqu'un
    (compte + zones). L'accès et l'essai consommé ne sont pas restaurés :
    s'il refait /start, il repart comme un ancien inscrit."""
    if not _est_admin(update):
        return  # silence total

    if not context.args:
        await update.effective_message.reply_text(
            "Utilisation : /unsub <chat_id ou @pseudo>\n(la liste des inscrits : /sub)"
        )
        return

    cible = _resoudre_cible(context.args[0])
    user = db.get_user(cible) if cible else None
    if not user:
        await update.effective_message.reply_text(
            f"🤷 Aucun inscrit trouvé pour « {context.args[0]} ». (/sub pour la liste)"
        )
        return

    zones = db.get_subscriptions(cible)
    qui = _fmt_identite(*db.get_identite(cible))
    db.remove_user(cible)
    log.warning("Admin %s a supprimé l'inscription de %s.",
                update.effective_chat.id, cible)
    await update.effective_message.reply_text(
        f"🗑 Inscription supprimée :\n"
        f"💬 {cible}" + (f" — 👤 {qui}" if qui else "") + "\n"
        f"🏙️ {_zones_texte(zones)}\n"
        f"🎓 {'boursier' if user[0] else 'non-boursier'}"
    )


async def setzone(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/setzone <chat_id ou @pseudo> <ville, dept ou CP> (admin) : corrige
    la zone d'un abonné qui t'a contacté par Telegram suite à une erreur de
    saisie — les abonnés ne peuvent plus changer leurs secteurs eux-mêmes
    après leur première inscription (anti prêt de compte).
    Version simplifiée d'une seule zone (pas de rayon/villes précises) :
    pour un besoin plus complexe, /unsub puis laisse-le refaire /start."""
    if not _est_admin(update):
        return  # silence total

    if len(context.args) < 2:
        await update.effective_message.reply_text(
            "Utilisation : /setzone <chat_id ou @pseudo> <ville, dept ou CP>\n"
            "Ex : /setzone 123456789 Bordeaux\n"
            "Ex : /setzone 123456789 92\n"
            "Ex : /setzone 123456789 paris 15\n\n"
            "(une seule zone simple ; pour rayon/villes précises : "
            "/unsub puis il refait /start)"
        )
        return

    cible = _resoudre_cible(context.args[0])
    user = db.get_user(cible) if cible else None
    if not user:
        await update.effective_message.reply_text(
            f"🤷 Aucun inscrit trouvé pour « {context.args[0]} ». (/sub pour la liste)"
        )
        return

    saisie = " ".join(context.args[1:])
    ville_norm, cp_filtre, arr_invalide = parse_city_query(saisie)
    if not ville_norm:
        await update.effective_message.reply_text(f"❌ Je n'ai pas compris « {saisie} ».")
        return

    if ville_norm.startswith("dept:"):
        zone = (dept_label(ville_norm[5:]), ville_norm, None)
    elif cp_filtre:
        zone = (format_zone(saisie, cp_filtre), ville_norm, cp_filtre)
    else:
        geo = await asyncio.to_thread(scraper.geocoder_ville, saisie)
        if not geo:
            await update.effective_message.reply_text(f"🤔 Ville inconnue : « {saisie} ».")
            return
        _, _, nom_officiel, _ = geo
        zone = (nom_officiel, normalize_city(nom_officiel), None)

    db.set_subscriptions(cible, [zone])
    log.warning("Admin %s a corrigé la zone de %s -> %s.",
                update.effective_chat.id, cible, zone[0])
    await update.effective_message.reply_text(f"✅ Zone de {cible} mise à jour : {zone[0]}")
    await notifier.send(
        context.bot, cible,
        f"🔄 Ton secteur surveillé a été mis à jour par l'équipe : {zone[0]}."
    )


_CLAVIER_FINESSAI = InlineKeyboardMarkup([[
    InlineKeyboardButton("✅ Oui", callback_data="finessai_oui"),
    InlineKeyboardButton("❌ Non", callback_data="finessai_non"),
]])


async def finessai(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/finessai (admin) : demande à TOUS les essayeurs gratuits actifs
    n'ayant jamais payé s'ils ont déjà reçu une notif. "Oui" -> coupe leur
    essai et les pousse vers le paiement ; "Non" -> rien ne change.
    Ne cible QUE l'essai gratuit (jamais un accès payé, cf.
    db.get_essayeurs_non_payeurs) — un payeur ne peut jamais être coupé
    par erreur via cette commande."""
    if not _est_admin(update):
        return  # silence total

    cibles = db.get_essayeurs_non_payeurs()
    if not cibles:
        await update.effective_message.reply_text(
            "Personne à contacter (aucun essai gratuit actif sans paiement)."
        )
        return

    for chat_id in cibles:
        await notifier.send(
            context.bot, chat_id,
            "👋 Question rapide : as-tu déjà reçu au moins une notification "
            "de logement depuis ton inscription ?",
            reply_markup=_CLAVIER_FINESSAI,
        )
    log.warning("Admin %s a lancé /finessai sur %d essayeur(s).",
                update.effective_chat.id, len(cibles))
    await update.effective_message.reply_text(
        f"✅ Question envoyée à {len(cibles)} essayeur(s) actif(s) "
        f"n'ayant jamais payé."
    )


async def on_finessai_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Réponse à /finessai. "Non" : rien ne change. "Oui" : coupe l'essai
    immédiatement et pousse vers le paiement, avec le bouton "Racheter un
    accès" attaché (via _clavier, qui le montre car l'accès vient
    d'expirer)."""
    query = update.callback_query
    chat_id = str(update.effective_chat.id)
    await query.answer()
    try:
        await query.edit_message_reply_markup(reply_markup=None)
    except Exception:
        pass  # message déjà édité/trop vieux : pas grave, on continue

    if query.data == "finessai_non":
        await notifier.send(context.bot, chat_id,
                             "Pas de souci, ton essai continue normalement 🙂")
        return

    db.terminer_essai(chat_id)
    log.info("Essai terminé (via /finessai) pour %s.", chat_id)
    await notifier.send(
        context.bot, chat_id,
        "Alors tu as la preuve que ce n'est pas une arnaque et que je "
        "t'offre un vrai service qui va vraiment t'aider à trouver un "
        "logement 😉\n\n"
        "Va voir ma story Snap pour comprendre pourquoi 👀\n\n"
        "Certaines personnes ont déjà trouvé leur logement grâce au bot "
        "pendant leur essai gratuit, sans jamais payer ensuite — du coup, "
        "ton essai s'arrête ici.\n\n"
        "Des logements vont continuer à se libérer tout l'été, et le bot "
        "optimise vraiment tes chances d'en avoir un avant tout le monde. "
        "Pour continuer à recevoir les notifications jusqu'à la fin de la "
        "saison, il faut passer au paiement : appuie sur « 💰 Racheter un "
        "accès » ci-dessous.",
        reply_markup=_clavier(chat_id),
    )


async def rappelessai(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/rappelessai (admin) : confirme à TOUS les essayeurs gratuits actifs
    (jamais payé) que leurs notifications fonctionnent bien, et rappelle
    leur date d'expiration pour qu'ils pensent à renouveler à temps.
    Ne cible QUE l'essai gratuit actif (cf. db.get_essayeurs_non_payeurs,
    même ciblage que /finessai) — jamais un accès payé, et ne coupe rien
    (contrairement à /finessai), juste un rappel informatif."""
    if not _est_admin(update):
        return  # silence total

    cibles = db.get_essayeurs_non_payeurs()
    if not cibles:
        await update.effective_message.reply_text(
            "Personne à contacter (aucun essai gratuit actif sans paiement)."
        )
        return

    envoyes = 0
    for chat_id in cibles:
        user = db.get_user(chat_id)
        if not user or not user[1]:
            continue
        _, expire_le = user
        await notifier.send(
            context.bot, chat_id,
            "👋 Petit point sur ton essai gratuit : tes notifications de "
            "logements fonctionnent bien, tu les reçois normalement en "
            "temps réel ✅\n\n"
            f"⏳ Ton accès expire le <b>{_fmt_date(expire_le)}</b> — pense "
            "à renouveler avant cette date, sinon tu ne recevras plus "
            "aucune notification.\n\n"
            "👉 « 💳 Payer maintenant pour garder l'accès » ci-dessous "
            "pour sécuriser la suite.",
            reply_markup=_clavier(chat_id),
        )
        envoyes += 1

    log.warning("Admin %s a lancé /rappelessai : %d message(s) envoyé(s).",
                update.effective_chat.id, envoyes)
    await update.effective_message.reply_text(
        f"✅ Rappel envoyé à {envoyes} essayeur(s) actif(s) n'ayant jamais payé."
    )


async def relance(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/relance (admin) : relance TOUS les inscrits dont l'accès est
    expiré ou jamais activé, avec le nombre RÉEL de logements apparus
    dans LEUR secteur depuis leur expiration (ou leur inscription si
    jamais activé) — basé sur historique_nouveautes (cf. db.py,
    alimenté à chaque cycle par main.py:scan_source).
    N'envoie qu'aux gens ayant raté AU MOINS RELANCE_SEUIL_MANQUES
    logements (config.py) — en dessous, pas assez percutant, on ne
    dérange pas pour rien."""
    if not _est_admin(update):
        return  # silence total

    utilisateurs = db.get_utilisateurs_expires()
    if not utilisateurs:
        await update.effective_message.reply_text(
            "Personne à relancer (tout le monde a un accès actif)."
        )
        return

    envoyes = 0
    for chat_id, expire_le, inscrit_le, boursier in utilisateurs:
        depuis = expire_le or inscrit_le
        source = "boursier" if boursier else "non-boursier"
        zones = db.get_subscriptions(chat_id)
        total = 0
        for _, norm, cp in zones:
            if norm.startswith("rayon:"):
                r = parse_rayon(norm)
                if r:
                    lat, lon, km = r
                    total += db.compter_manques_rayon(lat, lon, km, depuis, source)
            else:
                total += db.compter_manques(norm, cp, depuis, source)

        if total < config.RELANCE_SEUIL_MANQUES:
            continue  # pas assez percutant, on ne dérange pas pour rien

        secteur = _zones_texte(zones)
        await notifier.send(
            context.bot, chat_id,
            f"⏳ Ton abonnement a expiré et tu n'as toujours pas réglé — "
            f"du coup tu ne reçois plus aucune notification.\n\n"
            f"Depuis, <b>{total} logement{'s' if total > 1 else ''}</b> "
            f"{'sont apparus' if total > 1 else 'est apparu'} dans ton "
            f"secteur ({secteur}), que tu as raté"
            f"{'s' if total > 1 else ''} faute d'accès actif.\n\n"
            f"👉 Réactive-toi maintenant pour ne plus rien manquer : "
            f"« 💰 Racheter un accès »",
            reply_markup=_clavier(chat_id),
        )
        envoyes += 1

    log.warning("Admin %s a lancé /relance : %d message(s) envoyé(s) sur %d expiré(s).",
                update.effective_chat.id, envoyes, len(utilisateurs))
    await update.effective_message.reply_text(
        f"✅ Relance envoyée à {envoyes} inscrit(s) (sur {len(utilisateurs)} "
        f"expirés) — les autres ont raté moins de "
        f"{config.RELANCE_SEUIL_MANQUES} logements, pas dérangés pour rien."
    )


async def promo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/promo <prix> <heures> (admin) : offre flash réservée à TOUS les
    inscrits expirés ou jamais activés (même ciblage que /relance) — un
    tarif temporaire (db.set_promo), remis automatiquement au tarif normal
    à l'expiration (cf. main.py:scan_loop, db.get_promos_expirees).
    N'affecte jamais le tarif public ni les abonnés déjà actifs."""
    if not _est_admin(update):
        return  # silence total

    if len(context.args) != 2:
        await update.effective_message.reply_text(
            "Usage : /promo <prix€> <durée_heures> — ex. /promo 10 24"
        )
        return
    try:
        prix, heures = int(context.args[0]), float(context.args[1])
    except ValueError:
        await update.effective_message.reply_text(
            "Prix et durée doivent être des nombres — ex. /promo 10 24"
        )
        return

    utilisateurs = db.get_utilisateurs_expires()
    if not utilisateurs:
        await update.effective_message.reply_text(
            "Personne à cibler (tout le monde a un accès actif)."
        )
        return

    fin = datetime.now() + timedelta(hours=heures)
    for chat_id, expire_le, inscrit_le, boursier in utilisateurs:
        db.set_promo(chat_id, prix, heures)

        # nombre RÉEL de logements ratés depuis l'expiration (même calcul
        # que /relance) : ajoute une urgence chiffrée à l'offre chiffrée
        depuis = expire_le or inscrit_le
        source = "boursier" if boursier else "non-boursier"
        zones = db.get_subscriptions(chat_id)
        manques = 0
        for _, norm, cp in zones:
            if norm.startswith("rayon:"):
                r = parse_rayon(norm)
                if r:
                    lat, lon, km = r
                    manques += db.compter_manques_rayon(lat, lon, km, depuis, source)
            else:
                manques += db.compter_manques(norm, cp, depuis, source)

        ligne_manques = (
            f"\n\nDepuis, <b>{manques} logement{'s' if manques > 1 else ''}</b> "
            f"{'sont apparus' if manques > 1 else 'est apparu'} dans ton "
            f"secteur ({_zones_texte(zones)}), que tu as raté"
            f"{'s' if manques > 1 else ''} faute d'accès actif."
        ) if manques > 0 else ""

        await notifier.send(
            context.bot, chat_id,
            f"🔥 <b>Offre flash</b> : ton abonnement à <b>{prix} €</b> "
            f"au lieu de {config.TARIF_NORMAL} € !\n\n"
            f"Valable jusqu'au {fin.strftime('%d/%m/%Y à %H:%M')}, ensuite "
            f"retour au tarif normal."
            f"{ligne_manques}\n\n"
            f"👉 Réactive-toi maintenant : « 💰 Racheter un accès »",
            reply_markup=_clavier(chat_id),
        )

    # aperçu envoyé aux admins : voir exactement ce que les ciblés reçoivent
    # (sans doublon de comptage, sans leur poser de tarif promo pour autant
    # — un admin a un accès permanent, aucune raison de payer)
    for admin_id in config.ADMIN_CHAT_IDS:
        await notifier.send(
            context.bot, admin_id,
            f"👀 <b>Aperçu /promo</b> (message envoyé aux {len(utilisateurs)} "
            f"ciblés) :\n\n"
            f"🔥 <b>Offre flash</b> : ton abonnement à <b>{prix} €</b> "
            f"au lieu de {config.TARIF_NORMAL} € !\n\n"
            f"Valable jusqu'au {fin.strftime('%d/%m/%Y à %H:%M')}, ensuite "
            f"retour au tarif normal.\n\n"
            f"Depuis, <b>X logement(s)</b> sont apparus dans ton secteur, "
            f"que tu as ratés faute d'accès actif. <i>(chiffre réel, "
            f"personnalisé par destinataire)</i>\n\n"
            f"👉 Réactive-toi maintenant : « 💰 Racheter un accès »",
        )

    log.warning("Admin %s a lancé /promo %d€/%gh sur %d inscrit(s) expiré(s).",
                update.effective_chat.id, prix, heures, len(utilisateurs))
    await update.effective_message.reply_text(
        f"✅ Promo {prix} € ({heures}h) envoyée à {len(utilisateurs)} "
        f"inscrit(s) expiré(s)/jamais activé(s)."
    )


async def msg(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/msg <chat_id ou @pseudo> <message> (admin) : envoie un message
    libre à UN client précis via le bot — utile pour un souci ponctuel
    (ex : "j'ai payé mais rien reçu") sans passer par le contact direct."""
    if not _est_admin(update):
        return  # silence total

    if len(context.args) < 2:
        await update.effective_message.reply_text(
            "Utilisation : /msg <chat_id ou @pseudo> <message>\n"
            "Ex : /msg 123456789 On a bien reçu ton virement, c'est activé !"
        )
        return

    cible = _resoudre_cible(context.args[0])
    user = db.get_user(cible) if cible else None
    if not user:
        await update.effective_message.reply_text(
            f"🤷 Aucun inscrit trouvé pour « {context.args[0]} ». (/sub pour la liste)"
        )
        return

    texte = " ".join(context.args[1:])
    await notifier.send(context.bot, cible, texte)
    qui = _fmt_identite(*db.get_identite(cible)) or f"chat_id {cible}"
    log.info("Admin %s a envoyé un message manuel à %s.",
              update.effective_chat.id, cible)
    await update.effective_message.reply_text(f"✅ Message envoyé à {qui}.")


async def essai_manuel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/essai <chat_id ou @pseudo> (admin) : accorde manuellement l'essai
    gratuit standard (durée actuelle de /try, ex : 2 jours) à CE client
    précis — utile si son essai automatique n'a pas été accordé (ex :
    /try était coupé au moment de son inscription)."""
    if not _est_admin(update):
        return  # silence total

    if not context.args:
        await update.effective_message.reply_text(
            "Utilisation : /essai <chat_id ou @pseudo>"
        )
        return

    cible = _resoudre_cible(context.args[0])
    user = db.get_user(cible) if cible else None
    if not user:
        await update.effective_message.reply_text(
            f"🤷 Aucun inscrit trouvé pour « {context.args[0]} ». (/sub pour la liste)"
        )
        return

    jours = _essai_jours()
    fin = db.grant_access(cible, jours)
    db.marquer_essai(cible)
    qui = _fmt_identite(*db.get_identite(cible)) or f"chat_id {cible}"
    log.warning("Admin %s a accordé un essai manuel de %d j à %s.",
                update.effective_chat.id, jours, cible)
    await update.effective_message.reply_text(
        f"✅ Essai de {jours} jour(s) accordé à {qui}, jusqu'au "
        f"{fin.strftime('%d/%m/%Y')}."
    )
    await notifier.send(
        context.bot, cible,
        f"🎁 Un admin t'a accordé {jours} jour{'s' if jours > 1 else ''} "
        f"d'essai gratuit, jusqu'au {fin.strftime('%d/%m/%Y')} — tu peux "
        f"profiter des notifications dès maintenant !"
    )


async def payer_manuel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/payer <chat_id ou @pseudo> (admin) : accorde l'accès PAYANT complet
    (PAIEMENT_JOURS, comme un "✅ Reçu" normal) à ce client précis — utile
    si le paiement a eu lieu autrement que via le bot (cash, virement direct
    sans passer par "✅ J'ai payé", etc.)."""
    if not _est_admin(update):
        return  # silence total

    if not context.args:
        await update.effective_message.reply_text(
            "Utilisation : /payer <chat_id ou @pseudo>"
        )
        return

    cible = _resoudre_cible(context.args[0])
    user = db.get_user(cible) if cible else None
    if not user:
        await update.effective_message.reply_text(
            f"🤷 Aucun inscrit trouvé pour « {context.args[0]} ». (/sub pour la liste)"
        )
        return

    jours = config.PAIEMENT_JOURS
    fin = db.grant_access(cible, jours)
    qui = _fmt_identite(*db.get_identite(cible)) or f"chat_id {cible}"
    log.warning("Admin %s a accordé un accès payant manuel (%d j) à %s.",
                update.effective_chat.id, jours, cible)
    await update.effective_message.reply_text(
        f"✅ Accès payant de {jours} jours accordé à {qui}, jusqu'au "
        f"{fin.strftime('%d/%m/%Y')}."
    )
    await notifier.send(
        context.bot, cible,
        f"✅ Ton accès est activé jusqu'au {fin.strftime('%d/%m/%Y')} !\n\n"
        f"Si tu n'as pas encore choisi tes zones : /changer\n"
        f"Bonne chasse au logement 🏠",
        reply_markup=_clavier(cible),
    )


async def who(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/who <chat_id> (admin) : la fiche d'une personne — base + Telegram
    en direct (get_chat), donc ça marche aussi pour les inscrits d'avant
    le stockage du prénom, et le pseudo est toujours à jour."""
    if not _est_admin(update):
        return  # silence total

    if not context.args:
        await update.effective_message.reply_text(
            "Utilisation : /who <chat_id ou @pseudo>\n(la liste des inscrits : /sub)"
        )
        return
    cible = _resoudre_cible(context.args[0])
    if not cible:
        await update.effective_message.reply_text(
            f"🤷 Pseudo « {context.args[0]} » inconnu en base — je ne peux "
            f"chercher un @pseudo que parmi les inscrits. Essaie avec le "
            f"chat_id (/sub pour la liste)."
        )
        return

    prenom, pseudo = db.get_identite(cible)
    try:
        chat = await context.bot.get_chat(cible)
        prenom = " ".join(x for x in (chat.first_name, chat.last_name) if x) or prenom
        pseudo = chat.username or pseudo
    except Exception:
        pass  # Telegram ne connaît pas ce chat : on garde ce qu'on a en base

    lignes = [
        f"👤 Fiche {cible}",
        f"📛 Prénom : {prenom or 'inconnu'}",
        (f"🔗 Pseudo : @{pseudo} → t.me/{pseudo}" if pseudo else "🔗 Pseudo : aucun"),
    ]
    user = db.get_user(cible)
    if user:
        boursier, expire = user
        essai = " | 🎁 essai utilisé" if db.essai_deja_utilise(cible) else ""
        lignes += [
            f"🏙️ Zones : {_zones_texte(db.get_subscriptions(cible))}",
            f"🎓 {'boursier' if boursier else 'non-boursier'}{essai}",
            _ligne_acces(cible, expire),
        ]
    else:
        lignes.append("⭕ Pas inscrit actuellement.")
        if db.essai_deja_utilise(cible):
            lignes.append("🎁 (essai gratuit déjà consommé par le passé)")
    await update.effective_message.reply_text("\n".join(lignes))


async def mute(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/mute (admin) : ne plus recevoir les notifications d'inscription."""
    if not _est_admin(update):
        return
    db.set_setting("notif_inscriptions", "0")
    await update.effective_message.reply_text("🔕 Notifications d'inscription coupées. (/unmute pour les remettre)")


async def unmute(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/unmute (admin) : recevoir à nouveau les notifications d'inscription."""
    if not _est_admin(update):
        return
    db.set_setting("notif_inscriptions", "1")
    await update.effective_message.reply_text("🔔 Notifications d'inscription réactivées.")


async def silence(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/silence [on|off] (admin) : active/désactive le mode silencieux
    (sans son/vibration) pour les notifications de nouveaux logements
    uniquement — les autres messages (inscriptions, paiements, alertes
    admin...) ne sont jamais concernés. Sans argument : affiche l'état."""
    if not _est_admin(update):
        return  # silence total

    if not context.args:
        actif = db.get_setting("notifs_silencieuses", "0") == "1"
        await update.effective_message.reply_text(
            f"🔕 Mode silencieux (notifs de logements) : "
            f"{'activé' if actif else 'désactivé'}.\n"
            f"Utilisation : /silence on ou /silence off"
        )
        return

    arg = context.args[0].lower()
    if arg not in ("on", "off"):
        await update.effective_message.reply_text(
            "Utilisation : /silence on ou /silence off"
        )
        return

    db.set_setting("notifs_silencieuses", "1" if arg == "on" else "0")
    log.warning("Admin %s a %s le mode silencieux des notifs de logements.",
                update.effective_chat.id, "activé" if arg == "on" else "désactivé")
    await update.effective_message.reply_text(
        f"✅ Mode silencieux {'activé' if arg == 'on' else 'désactivé'} "
        f"pour les notifications de logements."
    )


async def mon_id(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Affiche le chat_id (sert à remplir ADMIN_CHAT_IDS dans config.py)."""
    await update.effective_message.reply_text(f"Ton chat_id : {update.effective_chat.id}")


# ---------------- Autres commandes ----------------

async def contact(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.effective_message.reply_text(
        config.CONTACT_TEXT, reply_markup=_clavier(update.effective_chat.id))


async def offre(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Bouton '🔎 Trouver mon logement' / '💰 Racheter un accès' : l'offre,
    les tarifs, les liens de paiement direct et le contact — et l'essai
    gratuit mis en avant s'il est encore disponible. Prévient l'admin à
    chaque clic (paiement semi-manuel : à toi de confirmer via le bouton
    Reçu/Pas reçu envoyé après que le client tape "✅ J'ai payé").
    Le bouton '✅ J'ai payé' n'est attaché QU'À ce message (inline) — pas
    de bouton permanent, pour ne pas déclencher de vérif sans contexte.
    EXCEPTION : encore en essai gratuit actif -> aucun tarif, aucun lien
    de paiement, aucun bouton "J'ai payé" — juste le rappel de la date
    de fin d'essai, rien à payer avant. Élimine à la racine les faux
    "j'ai payé" des essayeurs (ils n'ont même plus accès à l'offre)."""
    chat_id = update.effective_chat.id

    if db.est_en_essai_actif(chat_id):
        _, expire_le = db.get_user(chat_id)
        await update.effective_message.reply_text(
            f"ℹ️ Tu es actuellement en <b>essai gratuit</b>, actif jusqu'au "
            f"{_fmt_date(expire_le)}.\n\n"
            f"Tu n'as rien à payer avant cette date — profite de ton "
            f"accès, tes notifications continuent normalement jusqu'à "
            f"la fin de ton essai.",
            parse_mode=ParseMode.HTML,
        )
        return

    tarif = db.get_tarif(chat_id)
    texte = (config.TARIFS_TEXT.format(tarif=tarif) + "\n\n"
             + config.PAIEMENT_TEXT
             + "\n\n" + config.CONTACT_TEXT_PAIEMENT)
    if _essai_gratuit_actif() and not db.essai_deja_utilise(chat_id):
        jours = _essai_jours()
        texte += ("\n\n🎁 <b>Bonne nouvelle :</b> ton essai GRATUIT de "
                  f"{jours} jour{'s' if jours > 1 else ''} t'attend — "
                  "envoie /start pour l'activer, sans engagement !")
    clavier_paiement = InlineKeyboardMarkup(
        [[InlineKeyboardButton(BTN_PAYE, callback_data="jai_paye")]])
    await update.effective_message.reply_text(
        texte, parse_mode=ParseMode.HTML, reply_markup=clavier_paiement,
        disable_web_page_preview=True)
    # référence envoyée à part : un message court, facile à copier/coller,
    # mais qui rappelle SEUL (sans dépendre du message précédent) pourquoi
    # elle est indispensable — évite un virement sans référence, qui
    # retarde l'activation le temps de retrouver le client manuellement.
    await update.effective_message.reply_text(
        f"🔑 <b>Ta référence à mettre dans le virement :</b>\n\n"
        f"<code>{chat_id}</code>\n\n"
        f"⚠️ Sans elle, impossible de retrouver ton paiement automatiquement "
        f"— l'activation sera retardée.",
        parse_mode=ParseMode.HTML,
    )

    # anti-spam : un même client ne redéclenche la notif admin qu'après
    # OFFRE_NOTIF_COOLDOWN, même s'il retape sur le bouton entre-temps
    maintenant = time.time()
    if maintenant - _dernier_notif_offre.get(chat_id, 0) > config.OFFRE_NOTIF_COOLDOWN:
        _dernier_notif_offre[chat_id] = maintenant
        qui = _fmt_identite(*_identite(update)) or f"chat_id {chat_id}"
        await _prevenir_admin(
            context.bot,
            f"💰 {qui} a consulté l'offre de paiement — chat_id : {chat_id}"
        )


async def jai_paye_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Bouton inline '✅ J'ai payé', attaché uniquement au message de
    l'offre : prévient TOUS les admins avec un clavier inline Reçu/Pas
    reçu — un tap suffit à accorder l'accès et à prévenir le client,
    sans passer par le contact direct ni /gencode manuel.
    EXCEPTION 1 : chat_id sans compte du tout (jamais fait /start, mode
    découverte) -> pas de ping admin. Un virement ne peut pas être associé
    à quelqu'un qui n'a même pas choisi de ville, donc c'est forcément un
    clic hasardeux : on prévient juste que ça ne marche pas comme ça.
    EXCEPTION 2 : encore en essai gratuit actif -> pas de ping admin (faux
    positif quasi systématique chez les nouveaux inscrits qui cliquent
    sans avoir payé) ; on renvoie juste au client sa date de fin d'essai,
    et le contact Telegram s'il a VRAIMENT déjà payé par avance.
    CAS RESTANT (compte expiré, plus en essai) : pas de signal fiable
    pour distinguer un vrai payeur d'un clic au hasard (les deux passent
    par le même bouton) -> on ajoute une confirmation Oui/Non explicite
    (cf. on_confirme_paye_callback) avant de déranger l'admin."""
    query = update.callback_query
    chat_id = str(update.effective_chat.id)
    await query.answer()
    try:
        await query.edit_message_reply_markup(reply_markup=None)
    except Exception:
        pass  # évite de retaper deux fois dessus, sans bloquer si ça échoue

    if db.get_user(chat_id) is None:
        await notifier.send(
            context.bot, chat_id,
            "❌ Ça ne fonctionne pas comme ça — je vérifie manuellement "
            "chaque paiement, donc ça ne sert à rien de cliquer sans "
            "avoir réellement payé.\n\n"
            "Si tu veux t'abonner : envoie /start pour créer ton compte "
            "et choisir ta ville, puis règle via les infos de paiement "
            "affichées avec la référence demandée."
        )
        return  # pas de ping admin : sans compte, ça ne peut pas être un vrai paiement

    if db.est_en_essai_actif(chat_id):
        _, expire_le = db.get_user(chat_id)
        await notifier.send(
            context.bot, chat_id,
            f"ℹ️ Tu es encore en <b>essai gratuit</b>, actif jusqu'au "
            f"{_fmt_date(expire_le)} — inutile de valider un paiement "
            f"maintenant, rien n'est dû avant cette date.\n\n"
            f"Si tu as VRAIMENT déjà payé par avance, écris-nous sur "
            f"Telegram ({config.TELEGRAM_CONTACT}) avec ta preuve de "
            f"paiement (capture du virement), et on activera ton accès "
            f"prolongé directement."
        )
        return  # pas de ping admin : évite les faux positifs des essayeurs

    clavier_confirme = InlineKeyboardMarkup([[
        InlineKeyboardButton("✅ Oui, j'ai vraiment payé", callback_data="confpaye_oui"),
        InlineKeyboardButton("❌ Non, pas encore", callback_data="confpaye_non"),
    ]])
    await notifier.send(
        context.bot, chat_id,
        "⚠️ Dernière vérification avant de prévenir l'équipe : "
        "as-tu VRAIMENT déjà envoyé le virement ?\n\n"
        "Si tu réponds « Oui » alors que tu n'as pas payé, tu seras "
        "banni du bot.",
        reply_markup=clavier_confirme,
    )


async def on_confirme_paye_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Réponse à la confirmation Oui/Non de jai_paye_callback (uniquement
    pour les comptes expirés, ni en essai ni sans compte — cf. plus haut).
    "Non" -> rien ne se passe, pas de ping admin. "Oui" -> déclenche la
    vérification admin normale (Reçu/Pas reçu), comme avant l'ajout de
    cette étape."""
    query = update.callback_query
    chat_id = str(update.effective_chat.id)
    await query.answer()
    try:
        await query.edit_message_reply_markup(reply_markup=None)
    except Exception:
        pass

    if query.data == "confpaye_non":
        await notifier.send(
            context.bot, chat_id,
            "Pas de souci, reviens quand tu auras réellement payé 🙂"
        )
        return

    db.demander_verification_paiement(chat_id)  # persisté : survit à un restart
    qui = _fmt_identite(*_identite(update)) or f"chat_id {chat_id}"
    tarif = db.get_tarif(chat_id)

    await notifier.send(
        context.bot, chat_id,
        f"🔎 Merci ! On vérifie ton virement (référence {chat_id}) — "
        f"tu reçois une confirmation ici dans quelques minutes.",
    )

    clavier_admin = InlineKeyboardMarkup([[
        InlineKeyboardButton("✅ Reçu", callback_data=f"paye_oui:{chat_id}"),
        InlineKeyboardButton("❌ Pas reçu", callback_data=f"paye_non:{chat_id}"),
    ]])
    for admin_id in config.ADMIN_CHAT_IDS:
        await notifier.send(
            context.bot, admin_id,
            f"💰 Vérification de paiement demandée\n"
            f"👤 {qui}\n💬 chat_id : {chat_id}\n"
            f"💶 Tarif attendu : {tarif} €\n\n"
            f"As-tu reçu son virement (référence {chat_id}) ?",
            reply_markup=clavier_admin,
        )


async def on_paiement_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Réponse admin (Reçu / Pas reçu) au clavier inline de jai_paye_callback().
    Toujours vérifié via _est_admin : si le message est un jour transféré
    à quelqu'un d'autre, ce chat-là n'est pas admin -> refusé."""
    query = update.callback_query
    if not _est_admin(update):
        await query.answer()
        return

    action, _, cible = query.data.partition(":")
    if not db.resoudre_paiement(cible):
        await query.answer("Déjà traité par un autre admin.", show_alert=True)
        return
    await query.answer()

    if action == "paye_oui":
        if not db.get_user(cible):
            # payé avant même d'avoir fait /start : on crée un compte
            # minimal pour que grant_access ait une ligne à mettre à jour
            db.add_user(cible, boursier=False)
        jours = config.PAIEMENT_JOURS
        fin = db.grant_access(cible, jours)
        log.warning("Admin %s a confirmé le paiement de %s (+%d jours).",
                    update.effective_chat.id, cible, jours)
        # try/except séparé : un échec d'édition (message trop vieux,
        # aléa réseau) ne doit jamais empêcher la confirmation au client
        try:
            await query.edit_message_text(
                query.message.text
                + f"\n\n✅ Traité : accès accordé jusqu'au {fin.strftime('%d/%m/%Y')}."
            )
        except Exception:
            log.exception("Impossible d'éditer le message admin (paiement %s).", cible)
        await notifier.send(
            context.bot, cible,
            f"✅ Paiement confirmé ! Ton accès est actif jusqu'au "
            f"{fin.strftime('%d/%m/%Y')}.\n\n"
            f"Si tu n'as pas encore choisi tes zones : /changer\n"
            f"Bonne chasse au logement 🏠"
        )
    else:
        log.info("Admin %s : paiement de %s marqué non reçu.",
                  update.effective_chat.id, cible)
        try:
            await query.edit_message_text(
                query.message.text + "\n\n❌ Marqué comme non reçu.")
        except Exception:
            log.exception("Impossible d'éditer le message admin (refus %s).", cible)
        await notifier.send(
            context.bot, cible,
            "❌ Non, tu n'as pas encore payé — on n'a rien reçu de notre "
            "côté.\n\n"
            f"Si tu as vraiment déjà réglé, écris-nous sur Telegram "
            f"({config.TELEGRAM_CONTACT}) pour nous montrer ta preuve de "
            f"paiement (capture du virement), et on régularisera ton "
            f"accès immédiatement."
        )


async def on_confirmation_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Réponse du client à 'Tu l'as eue ?' sous une notif de logement.
    Oui -> candidature enregistrée (relance automatique le lendemain, cf.
    main.py:relancer_candidatures) MAIS les notifications continuent
    normalement pendant l'attente de validation CROUS.
    Non -> rien de plus, il reste abonné normalement."""
    query = update.callback_query
    chat_id = str(update.effective_chat.id)
    action, _, listing_id = query.data.partition(":")
    await query.answer()
    try:
        await query.edit_message_reply_markup(reply_markup=None)
    except Exception:
        pass  # message déjà édité/trop vieux : pas grave, on continue

    if action == "eu_non":
        await notifier.send(context.bot, chat_id,
                             "Tant pis, la prochaine c'est la bonne ! 💪")
        return

    if db.a_candidature_en_cours(chat_id):
        await notifier.send(
            context.bot, chat_id,
            "Tu as déjà une candidature en cours, je reviens vers toi "
            "pour celle-là 🙂")
        return

    db.creer_candidature(chat_id, listing_id)
    log.info("Candidature ouverte : %s sur logement %s.", chat_id, listing_id)
    clavier_annuler = InlineKeyboardMarkup([[
        InlineKeyboardButton("↩️ Annuler ma candidature",
                             callback_data="annuler_candidature"),
    ]])
    await notifier.send(
        context.bot, chat_id,
        "👍 Top ! Tu continues de recevoir tes notifications normalement "
        "en attendant.\n\n"
        "Tu auras ta réponse de validation/refus de la part du CROUS "
        "demain, avant ou vers 14h. Je te redemanderai à ce moment-là "
        "si tu l'as eu ou non 🙂",
        reply_markup=clavier_annuler,
    )


async def on_annuler_candidature(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Bouton '↩️ Annuler ma candidature' sous la confirmation : referme la
    candidature (plus de relance demain). Les notifications n'étaient de
    toute façon jamais coupées. Ne touche qu'à SA PROPRE candidature
    (chat_id de celui qui clique), jamais celle d'un autre."""
    query = update.callback_query
    chat_id = str(update.effective_chat.id)
    await query.answer()
    try:
        await query.edit_message_reply_markup(reply_markup=None)
    except Exception:
        pass

    if db.a_candidature_en_cours(chat_id):
        db.resoudre_candidature(chat_id)
        log.info("Candidature annulée par erreur de clic : %s.", chat_id)
        await notifier.send(
            context.bot, chat_id,
            "🔄 C'est annulé, tu n'auras pas de relance là-dessus demain."
        )
    else:
        await notifier.send(
            context.bot, chat_id,
            "Il n'y avait plus de candidature en cours à annuler."
        )


async def on_validation_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Réponse du client à la relance du lendemain ('la validation CROUS,
    tu l'as eue ?'). Oui -> abonnement terminé + demande screen/avis Telegram.
    Non -> candidature résolue, les notifications reprennent normalement."""
    query = update.callback_query
    chat_id = str(update.effective_chat.id)
    action, _, listing_id = query.data.partition(":")
    await query.answer()
    try:
        await query.edit_message_reply_markup(reply_markup=None)
    except Exception:
        pass

    db.resoudre_candidature(chat_id)  # débloque les notifs dans tous les cas

    if action == "valid_oui":
        db.terminer_acces(chat_id)
        log.warning("Candidature validée par le CROUS pour %s — abonnement terminé.",
                    chat_id)
        await notifier.send(
            context.bot, chat_id,
            "🎉 Trop bien, félicitations !\n\n"
            "Ton abonnement s'arrête ici, tu n'en as plus besoin 🙂\n\n"
            "⚠️ <b>Avant de partir</b> (important pour nous) :\n"
            "1️⃣ Envoie-nous un screen de ta réservation\n"
            "2️⃣ Laisse-nous un avis sur le service, sur Telegram\n\n"
            + config.CONTACT_TEXT_PAIEMENT
        )
    else:
        log.info("Candidature refusée par le CROUS pour %s.", chat_id)
        await notifier.send(
            context.bot, chat_id,
            "Ah dommage, refusé... Mais la prochaine c'est la bonne 💪"
        )


async def stop(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if db.remove_user(update.effective_chat.id):
        await update.effective_message.reply_text(
            "👋 Tu es désinscrit. Reviens avec /start quand tu veux !",
            reply_markup=CLAVIER_DECOUVERTE)
    else:
        await update.effective_message.reply_text("Tu n'étais pas inscrit. /start pour t'inscrire.",
                                        reply_markup=CLAVIER_DECOUVERTE)


async def status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = db.get_user(update.effective_chat.id)
    if not user:
        await update.effective_message.reply_text(
            "Tu n'es pas encore inscrit — tu es en mode découverte 🙂\n"
            "Envoie /start pour t'inscrire !",
            reply_markup=CLAVIER_DECOUVERTE)
        return
    boursier, expire = user
    zones = db.get_subscriptions(update.effective_chat.id)
    await update.effective_message.reply_text(
        f"📡 Ta surveillance\n"
        f"🏙️ Zones : {_zones_texte(zones)}\n"
        f"🎓 Boursier : {'oui' if boursier else 'non'}\n"
        f"{_ligne_acces(update.effective_chat.id, expire)}",
        reply_markup=_clavier(update.effective_chat.id),
    )


async def rappels(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/rappels [nombre] (self-service) : combien de fois être notifié
    pour un même nouveau logement. 1 = comportement normal (défaut).
    Au-delà, l'annonce complète part une fois, suivie de rappels courts
    ("🔺 Nouveau logement...") pour augmenter les chances d'être vu."""
    chat_id = update.effective_chat.id
    if not db.get_user(chat_id):
        await update.effective_message.reply_text(
            "Tu n'es pas encore inscrit : envoie /start !",
            reply_markup=CLAVIER_DECOUVERTE)
        return

    if not context.args:
        actuel = db.get_nb_rappels(chat_id)
        await update.effective_message.reply_text(
            f"🔺 Actuellement : {actuel} notification(s) par nouveau "
            f"logement.\nUtilisation : /rappels <nombre entre 1 et "
            f"{config.MAX_RAPPELS_NOTIF}>"
        )
        return

    try:
        n = int(context.args[0])
        assert 1 <= n <= config.MAX_RAPPELS_NOTIF
    except (ValueError, AssertionError):
        await update.effective_message.reply_text(
            f"Utilisation : /rappels <nombre entre 1 et "
            f"{config.MAX_RAPPELS_NOTIF}>"
        )
        return

    db.set_nb_rappels(chat_id, n)
    await update.effective_message.reply_text(
        f"✅ C'est noté : {n} notification{'s' if n > 1 else ''} par "
        f"nouveau logement à partir de maintenant."
    )


async def villes(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # la liste suit la vue de l'abonné (un boursier voit SES chiffres)
    user = db.get_user(update.effective_chat.id)
    boursier = bool(user[0]) if user else False
    top = db.top_villes(25, boursier)
    if not top:
        await update.effective_message.reply_text("Le premier scan n'est pas encore terminé, réessaie dans quelques minutes.")
        return
    lignes = [f"• {ville} — {nb} logement{'s' if nb > 1 else ''}" for ville, nb in top]
    actu = _derniere_actu(boursier)
    await update.effective_message.reply_text(
        "🏙️ Villes avec le plus de logements en ce moment :\n\n" + "\n".join(lignes)
        + (f"\n\n⏱ Dernier scan : {actu}." if actu else ""),
        reply_markup=_clavier(update.effective_chat.id),
    )


async def aide(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.effective_message.reply_text(
        "🤖 Commandes :\n"
        "/start — s'inscrire\n"
        "/changer — changer de ville ou de statut boursier\n"
        "/activer CODE — activer/renouveler son accès\n"
        "/status — voir sa surveillance et son accès\n"
        "/villes — villes avec des logements actuellement\n"
        "/rappels [nombre] — combien de fois être notifié par nouveau "
        "logement (1 à 5, défaut 1)\n"
        "/contact — nous joindre (code, paiement, question)\n"
        "/stop — se désinscrire\n",
        reply_markup=_clavier(update.effective_chat.id),
    )


# ---------------- Gestion des erreurs ----------------

async def on_error(update, context):
    """Erreurs Telegram (réseau, conflit...) -> une ligne de log, pas un pavé."""
    log.error("Erreur Telegram : %s", context.error)


# ---------------- Enregistrement des handlers ----------------

def register_handlers(application):
    """Branche toutes les commandes sur l'application python-telegram-bot."""
    # mesure du temps de réponse : groupe -1 = avant tout le monde,
    # groupe 999 = après tout le monde (PTB traite les groupes dans l'ordre
    # pour un même update, donc ça encadre le vrai traitement)
    application.add_handler(TypeHandler(Update, _marquer_arrivee), group=-1)
    application.add_handler(TypeHandler(Update, _marquer_fin), group=999)

    inscription = ConversationHandler(
        entry_points=[
            CommandHandler("start", start),
            CommandHandler("changer", changer),
            # boutons du clavier permanent qui démarrent une conversation
            MessageHandler(filters.Regex(f"^{BTN_CHANGER}$"), changer),
            MessageHandler(filters.Regex(f"^{BTN_CODE}$"), entrer_code),
        ],
        states={
            REGION: [MessageHandler(filters.TEXT & ~filters.COMMAND, recevoir_region)],
            VILLE: [MessageHandler(filters.TEXT & ~filters.COMMAND, recevoir_ville)],
            PORTEE: [MessageHandler(filters.TEXT & ~filters.COMMAND, recevoir_portee)],
            PROCHES: [MessageHandler(filters.TEXT & ~filters.COMMAND, recevoir_proches)],
            DEPTS: [MessageHandler(filters.TEXT & ~filters.COMMAND, recevoir_depts)],
            ARRS: [MessageHandler(filters.TEXT & ~filters.COMMAND, recevoir_arrs)],
            BOURSIER: [MessageHandler(filters.TEXT & ~filters.COMMAND, recevoir_boursier)],
            CODE: [MessageHandler(filters.TEXT & ~filters.COMMAND, recevoir_code)],
        },
        # /start, /changer et /stop tapés EN PLEINE conversation ne sont
        # plus avalés en silence : ils relancent ou ferment proprement
        fallbacks=[
            CommandHandler("annuler", annuler),
            CommandHandler("start", start),
            CommandHandler("changer", changer),
            CommandHandler("stop", stop_conv),
        ],
        # état sauvegardé sur disque (cf. main.py: PicklePersistence) :
        # un redémarrage pile au milieu d'une inscription ne perd plus
        # la progression — name= est obligatoire pour le retrouver
        name="inscription",
        persistent=True,
    )
    application.add_handler(inscription)
    application.add_handler(CommandHandler("stop", stop))
    application.add_handler(CommandHandler("status", status))
    application.add_handler(CommandHandler("rappels", rappels))
    application.add_handler(CommandHandler("villes", villes))
    application.add_handler(CommandHandler("contact", contact))
    application.add_handler(CommandHandler("activer", activer))
    # boutons du clavier permanent (hors conversation)
    application.add_handler(MessageHandler(filters.Regex(f"^{BTN_STATUT}$"), status))
    application.add_handler(MessageHandler(filters.Regex(f"^{BTN_CONTACT}$"), contact))
    application.add_handler(MessageHandler(filters.Regex(f"^{BTN_OFFRE}$"), offre))
    application.add_handler(MessageHandler(filters.Regex(f"^{BTN_RACHAT}$"), offre))
    application.add_handler(MessageHandler(filters.Regex(f"^{BTN_PAYER_AVANCE}$"), offre))
    application.add_handler(CallbackQueryHandler(jai_paye_callback, pattern=r"^jai_paye$"))
    application.add_handler(CallbackQueryHandler(on_confirme_paye_callback, pattern=r"^confpaye_"))
    application.add_handler(CallbackQueryHandler(on_paiement_callback, pattern=r"^paye_"))
    application.add_handler(CallbackQueryHandler(on_confirmation_callback, pattern=r"^eu_"))
    application.add_handler(CallbackQueryHandler(on_annuler_candidature, pattern=r"^annuler_candidature$"))
    application.add_handler(CallbackQueryHandler(on_validation_callback, pattern=r"^valid_"))
    # commandes admin (jamais listées dans /aide, silence pour les non-admins)
    application.add_handler(CommandHandler("admin", admin_aide))
    application.add_handler(CommandHandler("modetest", modetest))
    application.add_handler(CommandHandler("gencode", gencode))
    application.add_handler(CommandHandler("fakenotif", fakenotif))
    application.add_handler(CommandHandler("resetdemo", resetdemo))
    application.add_handler(CommandHandler("try", essai_cmd))
    application.add_handler(CommandHandler("stats", stats))
    application.add_handler(CommandHandler("perf", perf))
    application.add_handler(CommandHandler("sub", sub))
    application.add_handler(CommandHandler("unsub", unsub))
    application.add_handler(CommandHandler("setzone", setzone))
    application.add_handler(CommandHandler("finessai", finessai))
    application.add_handler(CommandHandler("rappelessai", rappelessai))
    application.add_handler(CommandHandler("relance", relance))
    application.add_handler(CommandHandler("promo", promo))
    application.add_handler(CallbackQueryHandler(on_finessai_callback, pattern=r"^finessai_"))
    application.add_handler(CommandHandler("who", who))
    application.add_handler(CommandHandler("msg", msg))
    application.add_handler(CommandHandler("essai", essai_manuel))
    application.add_handler(CommandHandler("payer", payer_manuel))
    application.add_handler(CommandHandler("annonce", annonce))
    application.add_handler(CommandHandler("mute", mute))
    application.add_handler(CommandHandler("unmute", unmute))
    application.add_handler(CommandHandler("silence", silence))
    application.add_handler(CommandHandler("id", mon_id))
    application.add_handler(CommandHandler(("aide", "help"), aide))
    application.add_error_handler(on_error)
