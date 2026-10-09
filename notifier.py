# ============================================================
#  NOTIFIER — formatage des messages + envoi avec rate limit
#  (Telegram : 30 msg/s max en global, on plafonne à 25)
# ============================================================

import asyncio
import html
import logging

from aiolimiter import AsyncLimiter
from telegram import InlineKeyboardButton, InlineKeyboardMarkup
from telegram.constants import ParseMode
from telegram.error import BadRequest, Forbidden, RetryAfter

import config
import db

log = logging.getLogger(__name__)

# deux files séparées (fenêtre glissante de 1s) : une notif de logement
# ne peut jamais être retardée par une vague d'inscriptions/paiements/admin,
# et inversement, même si les deux arrivent en même temps
_limiter_notifs = AsyncLimiter(config.NOTIF_MSG_PER_SECOND, 1)
_limiter_autres = AsyncLimiter(config.AUTRES_MSG_PER_SECOND, 1)


def _notifs_silencieuses():
    """Réglage admin (/silence) : les notifs de logements partent-elles
    sans son/vibration chez les abonnés ? Ne concerne QUE la file
    prioritaire (logements) — jamais les autres messages (inscriptions,
    paiements, alertes admin...)."""
    return db.get_setting("notifs_silencieuses", "0") == "1"


def format_listing(l):
    """Un logement -> bloc de texte lisible. Les champs venus de l'API
    (titre, adresse...) sont échappés : un '<' dans un libellé ferait
    rejeter tout le message par Telegram (parse_mode=HTML)."""
    lignes = [f"🔑 <b>{html.escape(l['titre'])}</b>",
              f"📍 {html.escape(l['adresse'])}"]
    if l["prix"]:
        lignes.append(f"💶 {html.escape(l['prix'])}")
    if l["surface"]:
        lignes.append(f"📐 {html.escape(l['surface'])}")
    if l["details"]:
        lignes.append(f"▫️ {' · '.join(html.escape(d) for d in l['details'])}")
    if l.get("dernieres_places"):
        lignes.append("⚠️ <b>Dernières places !</b>")
    elif l.get("tres_demande"):
        lignes.append("🔥 Très demandé")
    lignes.append(f'👉 <a href="{l["lien"]}">Voir le logement</a>')
    return "\n".join(lignes)


def _clavier_confirmation(listing_id):
    """Boutons 'Tu l'as eue ?' attachés à une notif de logement.
    callback_data lu par bot.on_confirmation_callback."""
    return InlineKeyboardMarkup([[
        InlineKeyboardButton("✅ Oui", callback_data=f"eu_oui:{listing_id}"),
        InlineKeyboardButton("❌ Non", callback_data=f"eu_non:{listing_id}"),
    ]])


_SEPARATEUR = "\n\n──────────────\n\n"


def _titre(n, lieu, suite=None):
    """En-tête du message ; `lieu` contient sa préposition ("à Paris")."""
    if suite:
        return f"📢 <b>{html.escape(lieu).capitalize()} (suite {suite})</b>"
    return (f"📢 <b>{n} nouveau{'x' if n > 1 else ''} "
            f"logement{'s' if n > 1 else ''} {html.escape(lieu)} !</b>")


def format_notification(logements, lieu, horodatage):
    """Groupe de nouveaux logements -> UN message (à réserver aux petits
    groupes / légendes photo ; pour les grosses vagues : send_grouped)."""
    corps = _SEPARATEUR.join(format_listing(l) for l in logements)
    return (f"{_titre(len(logements), lieu)}\n\n{corps}"
            f"\n\n⏰ <i>Détecté le {horodatage}</i>")


# marge sous la limite Telegram de 4096 caractères par message
_TAILLE_MAX = 3500


async def send_grouped(bot, chat_id, logements, lieu, horodatage):
    """Grosse vague -> un message, ou PLUSIEURS si ça dépasse la limite
    Telegram (sinon le message entier est rejeté et la notif perdue)."""
    blocs = [format_listing(l) for l in logements]
    pied = f"\n\n⏰ <i>Détecté le {horodatage}</i>"

    # répartition des blocs en paquets qui tiennent sous la limite
    paquets, paquet, taille = [], [], 0
    for bloc in blocs:
        if paquet and taille + len(bloc) + len(_SEPARATEUR) > _TAILLE_MAX:
            paquets.append(paquet)
            paquet, taille = [], 0
        paquet.append(bloc)
        taille += len(bloc) + len(_SEPARATEUR)
    paquets.append(paquet)

    # un seul jeu de boutons possible par message : posé sur le DERNIER
    # paquet, référencé au logement seul s'il n'y en a qu'un, sinon au lot
    ref = logements[0]["id"] if len(logements) == 1 else "groupe"
    clavier = _clavier_confirmation(ref)

    for i, p in enumerate(paquets, start=1):
        suite = f"{i}/{len(paquets)}" if len(paquets) > 1 and i > 1 else None
        dernier = i == len(paquets)
        texte = _titre(len(logements), lieu, suite) + "\n\n" + _SEPARATEUR.join(p) + pied
        if dernier:
            texte += "\n\n👇 Tu as eu l'un de ces logements ?"
        await send(bot, chat_id, texte, reply_markup=clavier if dernier else None,
                  prioritaire=True)


async def _envoyer(bot, action, chat_id, limiter):
    """Exécute un envoi avec rate limit + gestion des erreurs communes :
    - 429 RetryAfter : Telegram ne met RIEN en queue, il rejette ; on
      attend le délai qu'il indique et on renvoie (2 tentatives max)
    - Forbidden : l'utilisateur a bloqué le bot -> désinscription
    Retourne True si le message est parti."""
    for tentative in (1, 2):
        async with limiter:
            try:
                await action()
                return True
            except RetryAfter as e:
                log.warning("429 Telegram pour %s, on attend %ss (essai %d).",
                            chat_id, e.retry_after, tentative)
                await asyncio.sleep(e.retry_after + 0.5)
            except Forbidden:
                log.info("Chat %s a bloqué le bot, désinscription.", chat_id)
                db.remove_user(chat_id)
                return False
    log.error("Échec d'envoi vers %s après retries 429.", chat_id)
    return False


async def send(bot, chat_id, text, reply_markup=None, prioritaire=False):
    """Envoi d'un message texte, en respectant le rate limit.
    prioritaire=True -> file des notifs de logements (jamais coincée
    derrière une vague d'inscriptions/paiements/admin, cf. notifier.py).

    Si le texte contient du HTML mal formé (ex : /annonce avec un '<' ou
    '&' tapé par erreur par un admin), Telegram rejette le message entier
    (BadRequest) — au lieu de le perdre en silence, on retombe une fois
    sur du texte brut (sans parse_mode) pour garantir la livraison."""
    limiter = _limiter_notifs if prioritaire else _limiter_autres
    silencieux = prioritaire and _notifs_silencieuses()
    async def essai(mode):
        await bot.send_message(
            chat_id=chat_id,
            text=text,
            parse_mode=mode,
            disable_web_page_preview=True,
            reply_markup=reply_markup,
            disable_notification=silencieux,
        )
    try:
        await _envoyer(bot, lambda: essai(ParseMode.HTML), chat_id, limiter)
    except BadRequest as e:
        log.warning("HTML invalide pour %s (%s), repli en texte brut.", chat_id, e)
        try:
            await _envoyer(bot, lambda: essai(None), chat_id, limiter)
        except Exception:
            log.exception("Échec d'envoi (texte brut) vers %s", chat_id)
    except Exception:
        log.exception("Échec d'envoi vers %s", chat_id)


async def send_listing_with_photo(bot, chat_id, logement, caption):
    """Notification avec la photo du logement en grand + les infos en
    légende, plus les boutons 'Tu l'as eue ?'. Si la photo échoue (URL
    morte, image invalide...), on retombe automatiquement sur un message
    texte : jamais de notif perdue. Toujours en file PRIORITAIRE."""
    caption = caption + "\n\n👇 Tu l'as eue ?"
    clavier = _clavier_confirmation(logement["id"])
    if not logement.get("photo"):
        await send(bot, chat_id, caption, reply_markup=clavier, prioritaire=True)
        return

    async def action():
        await bot.send_photo(
            chat_id=chat_id,
            photo=logement["photo"],
            caption=caption,
            parse_mode=ParseMode.HTML,
            reply_markup=clavier,
            disable_notification=_notifs_silencieuses(),
        )
    try:
        await _envoyer(bot, action, chat_id, _limiter_notifs)
    except Exception:
        log.warning("Photo KO pour %s (%s), repli en texte.",
                    logement["id"], logement["photo"])
        await send(bot, chat_id, caption, reply_markup=clavier, prioritaire=True)
