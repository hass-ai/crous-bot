# ============================================================
#  MAIN — point d'entrée
#  Deux choses tournent en parallèle dans la même boucle asyncio :
#    1. le bot Telegram (commandes, inscriptions en temps réel)
#    2. la boucle de scan (scrape national -> diff -> notifications)
# ============================================================

import asyncio
import logging
import time
from collections import Counter, defaultdict
from datetime import datetime
from logging.handlers import RotatingFileHandler

from telegram import InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, PicklePersistence

import bot
import config
import db
import diff
import notifier
import parser as crous_parser
import scraper
import os 




# logs en console ET dans bot.log (rotation à 2 Mo, 2 archives) :
# si la console gèle ou disparaît, l'historique reste consultable
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    handlers=[
        logging.StreamHandler(),
        RotatingFileHandler(os.path.join(config.DATA_DIR,"bot.log"),maxBytes=2_000_000,backupCount=2,encoding="utf-8"),
    ],
)

# les logs de la lib HTTP de telegram sont trop bavards
logging.getLogger("httpx").setLevel(logging.WARNING)
log = logging.getLogger("main")


def _dump_logements(logements, total, source):
    """Debug : écrit tout ce que le scan a vu dans un fichier consultable.
    Écrasé à chaque cycle -> reflète toujours le dernier scan."""
    try:
        nb_dispo = sum(1 for l in logements if l["disponible"])
        with open(os.path.join(config.DATA_DIR,f"debug_logements_{source}.txt"), "w", encoding="utf-8") as f:
            f.write(f"Scan {source} du "
                    f"{datetime.now().strftime('%d/%m/%Y %H:%M:%S')} — "
                    f"{nb_dispo} disponibles / {len(logements)} logements "
                    f"(annoncé par le site : {total})\n")
            f.write("=" * 70 + "\n")
            for l in sorted(logements, key=lambda x: (x["ville_norm"], x["id"])):
                etat = "" if l["disponible"] else " | COMPLET"
                f.write(f"id={l['id']:>6} | {l['cp']} {l['ville']:<25} | "
                        f"{l['titre'][:50]} | {l['prix']}{etat}\n")
    except Exception:
        log.exception("Échec du dump debug %s.", source)


async def scan_source(application, session, source, vues, stats_jour, notif_perf):
    """
    Un cycle complet pour une source ('non-boursier' ou 'boursier') :
    scrape -> diff -> routage des notifications par ville.
    `vues` = quels abonnés notifier : [False] = non-boursiers,
    [True] = boursiers, [False, True] = tout le monde (cas sans cookies).
    `stats_jour` = compteurs du rapport quotidien admin.
    `notif_perf` = {"total": secondes, "nb": nombre} accumulé pour mesurer
    le temps moyen d'envoi des notifs sur ce cycle (cf. scan_loop).
    """
    # requests est bloquant -> on le fait tourner dans un thread
    logements, total = await asyncio.to_thread(scraper.fetch_all_listings, session)
    disponibles = [l for l in logements if l["disponible"]]
    log.info("[%s] %d disponibles / %d scannés (annoncé : %s)",
             source, len(disponibles), len(logements), total)

    if config.DEBUG_DUMP and logements:
        _dump_logements(logements, total, source)

    # garde-fou : page vide ou site cassé -> on ne touche à rien,
    # sinon tous les logements seraient "nouveaux" au cycle suivant
    if not logements:
        log.warning("[%s] 0 logement récupéré, cycle ignoré.", source)
        return []

    # comptages par ville et par CP, PAR VUE. Les villes incluent les
    # résidences COMPLÈTES (nb=0 possible) : en pleine phase, une ville
    # pleine doit rester proposable dans les "alentours" — ses logements
    # se libéreront. Les nb ne comptent que les disponibles.
    compteur = Counter()
    affichage, dept_ville, coords = {}, {}, defaultdict(list)
    for l in logements:
        if l["ville_norm"]:
            compteur[l["ville_norm"]] += 1 if l["disponible"] else 0
            affichage[l["ville_norm"]] = l["ville"]
            if l["cp"]:
                dept_ville[l["ville_norm"]] = (
                    l["cp"][:3] if l["cp"].startswith("97") else l["cp"][:2])
            if l["lat"] is not None:
                coords[l["ville_norm"]].append((l["lat"], l["lon"]))
    def _centre(norm):
        pts = coords.get(norm)
        if not pts:
            return None, None
        return (sum(p[0] for p in pts) / len(pts),
                sum(p[1] for p in pts) / len(pts))
    # écritures DB potentiellement lourdes (1000+ lignes réécrites) ->
    # déportées dans un thread pour ne jamais geler la boucle asyncio
    # principale pendant qu'un client tape une commande en même temps
    await asyncio.to_thread(
        db.update_villes_vues,
        {norm: (affichage[norm], nb, dept_ville.get(norm), *_centre(norm))
         for norm, nb in compteur.items()},
        source=source,
    )
    await asyncio.to_thread(
        db.update_cps_vus,
        Counter(l["cp"] for l in disponibles if l["cp"]), source=source)
    # horodatage du scan : le bot affiche des chiffres "au dernier scan"
    # sans jamais refaire de requête API à la demande
    db.set_setting(f"last_scan_{source}",
                   datetime.now().isoformat(timespec="seconds"))

    # diff T1/T2 sur TOUT le scan (dispo ET complets) : un logement qui
    # passe complet/absent -> disponible est une LIBÉRATION -> notification.
    # Scan partiel (moins d'items que le total annoncé) : on ne marque pas
    # les absents indisponibles, pour ne pas fabriquer de fausses libérations.
    scan_complet = not total or len(logements) >= total
    if not scan_complet:
        log.warning("[%s] Scan partiel (%d/%s) : absents non marqués complets.",
                    source, len(logements), total)
    nouveaux, premier_run = await asyncio.to_thread(
        diff.find_new, logements, source, scan_complet)
    if premier_run:
        log.info("[%s] Premier scan : %d logements enregistrés en silence.",
                 source, len(logements))
        return logements

    if nouveaux:
        log.info("[%s] %d NOUVEAU(X) logement(s) !", source, len(nouveaux))
        stats_jour["nouveaux"] += len(nouveaux)
        # historique horodaté par zone : sert à /relance pour dire à un
        # abonné expiré "tu as raté X logements dans ton secteur depuis"
        await asyncio.to_thread(db.logger_nouveautes, nouveaux, source)
        horodatage = datetime.now().strftime("%d/%m/%Y %H:%M")

        # résidences confirmées fausses (config.RESIDENCES_SUSPECTES) :
        # jamais notifiées aux abonnés, seulement aux admins, pour garder
        # un œil dessus sans polluer tout le monde de fausses pistes
        suspects = [l for l in nouveaux if any(
            r.upper() in l["titre"].upper() for r in config.RESIDENCES_SUSPECTES)]
        if suspects:
            nouveaux = [l for l in nouveaux if l not in suspects]
            taches_suspects = []
            for admin_id in config.ADMIN_CHAT_IDS:
                for l in suspects:
                    caption = notifier.format_notification(
                        [l], f"à {l['ville']}", horodatage)
                    taches_suspects.append(notifier.send_listing_with_photo(
                        application.bot, admin_id, l, caption))
            await asyncio.gather(*taches_suspects, return_exceptions=True)
            log.info("[%s] %d logement(s) de résidence suspecte filtré(s), "
                     "notifié(s) aux admins uniquement.", source, len(suspects))
            if not nouveaux:
                return logements

        # regroupement par zone : chaque logement alimente sa ville ET son
        # département ("dept:92") — un abonné n'a qu'une zone, pas de doublon
        par_zone = defaultdict(list)
        for l in nouveaux:
            par_zone[l["ville_norm"]].append(l)
            if l["cp"]:
                prefixe = l["cp"][:3] if l["cp"].startswith("97") else l["cp"][:2]
                par_zone[f"dept:{prefixe}"].append(l)

        # sélection PAR ABONNÉ, dédupliquée : un abonné "nanterre" + "92"
        # ne reçoit qu'une fois un logement de Nanterre
        # UNE requête groupée par vue (pas une par zone) : décisif à
        # l'échelle de centaines d'abonnés répartis sur des dizaines de villes
        envois = {}  # chat_id -> {id_logement: logement}
        for boursier in vues:
            abonnes_par_zone = db.get_subscribers_batch(list(par_zone.keys()), boursier)
            for zone, groupe in par_zone.items():
                for chat_id, cp_filtre in abonnes_par_zone.get(zone, []):
                    for l in groupe:
                        if not cp_filtre or l["cp"] == cp_filtre:
                            envois.setdefault(chat_id, {})[l["id"]] = l

        # abonnés "ville + alentours" : match par distance GPS
        for boursier in vues:
            for chat_id, lat, lon, km in db.get_radius_subscribers(boursier):
                for l in nouveaux:
                    if (l["lat"] is not None
                            and crous_parser.dist_km(lat, lon, l["lat"], l["lon"]) <= km):
                        envois.setdefault(chat_id, {})[l["id"]] = l

        # on CONSTRUIT la liste des envois à faire, sans les attendre un par
        # un : un send_photo attend que Telegram aille chercher l'image chez
        # le CROUS (300ms-2s) — en séquentiel, des centaines d'abonnés sur
        # une même ville peuvent prendre plusieurs MINUTES. En parallèle, le
        # rate-limiter (notifier.py) régule proprement le débit réel envoyé
        # à Telegram, mais l'attente réseau de chacun se recouvre.
        taches = []
        for chat_id, selection_map in envois.items():
            selection = list(selection_map.values())

            if config.PHOTOS_ENABLED and len(selection) <= config.PHOTOS_MAX_GROUP:
                # peu de logements -> un message AVEC PHOTO par logement
                for logement in selection:
                    caption = notifier.format_notification(
                        [logement], f"à {logement['ville']}", horodatage)
                    taches.append(notifier.send_listing_with_photo(
                        application.bot, chat_id, logement, caption))
            else:
                # grosse vague -> message(s) texte groupé(s), découpés si
                # ça dépasse la limite Telegram (anti-spam ET anti-perte)
                villes_distinctes = {l["ville_norm"] for l in selection}
                lieu = (f"à {selection[0]['ville']}" if len(villes_distinctes) == 1
                        else "dans tes zones surveillées")
                taches.append(notifier.send_grouped(application.bot, chat_id,
                                                    selection, lieu, horodatage))

        if taches:
            debut_envoi = asyncio.get_running_loop().time()
            await asyncio.gather(*taches, return_exceptions=True)
            notif_perf["total"] += asyncio.get_running_loop().time() - debut_envoi
            notif_perf["nb"] += len(taches)
        stats_jour["envois"] += len(taches)

        # rappels courts ("🔺 Nouveau logement...") pour ceux qui en ont
        # réglé plus d'un (/rappels) — envoyés en SECOND, une fois que
        # TOUT LE MONDE a déjà reçu l'annonce complète : le réglage de
        # quelqu'un ne retarde jamais le premier message d'un autre abonné
        rappels_par_chat = await asyncio.to_thread(
            db.get_rappels_batch, list(envois.keys()))
        taches_rappels = [
            notifier.send(application.bot, chat_id,
                          "🔺 Nouveau logement dans ta zone !",
                          prioritaire=True)
            for chat_id in envois
            for _ in range(rappels_par_chat.get(chat_id, 1) - 1)
        ]
        if taches_rappels:
            debut_rappels = asyncio.get_running_loop().time()
            await asyncio.gather(*taches_rappels, return_exceptions=True)
            notif_perf["total"] += asyncio.get_running_loop().time() - debut_rappels
            notif_perf["nb"] += len(taches_rappels)
        stats_jour["envois"] += len(taches_rappels)

        log.info("[%s] %d nouveau(x) logement(s) -> %d abonné(s) notifié(s)",
                 source, len(nouveaux), len(envois))

    return logements


async def relancer_candidatures(application):
    """Suivi 'tu l'as eu ?' : relance ceux qui ont dit oui à une notif
    (cf. bot.on_confirmation_callback), dès le lendemain vers 14h."""
    for chat_id, listing_id in db.candidatures_a_relancer():
        db.marquer_relance_envoyee(chat_id)
        clavier = InlineKeyboardMarkup([[
            InlineKeyboardButton("✅ Oui", callback_data=f"valid_oui:{listing_id}"),
            InlineKeyboardButton("❌ Non", callback_data=f"valid_non:{listing_id}"),
        ]])
        await notifier.send(application.bot, chat_id,
                            "🔔 Alors, tu as eu la validation du CROUS ?",
                            reply_markup=clavier)


async def scan_loop(application):
    """Boucle infinie : un cycle toutes les SCAN_INTERVAL secondes."""
    session_non_boursier = scraper.build_session(with_cookies=False)
    session_boursier = scraper.build_session(with_cookies=True)

    if session_boursier:
        log.info("Cookies trouvés : vue boursier activée.")
    else:
        log.warning("Pas de %s : vue boursier désactivée, les abonnés "
                    "boursiers recevront la vue non-boursier.", config.COOKIES_FILE)

    stats_jour = {"cycles": 0, "nouveaux": 0, "envois": 0}
    jour_courant = datetime.now().date()
    derniere_alerte = 0.0         # anti-spam alertes "cycle long" (1/heure max)
    derniere_alerte_cookie = 0.0  # anti-spam alerte cookies (1/jour max)
    derniere_verif_cookie = 0.0   # check direct de session (1/heure, dès le 1er cycle)
    derniere_alerte_notif = 0.0    # anti-spam alerte "notifs lentes" (1/heure max)
    derniere_alerte_reponse = 0.0  # anti-spam alerte "réponses lentes" (1/heure max)

    while True:
        debut_cycle = asyncio.get_running_loop().time()
        notif_perf = {"total": 0.0, "nb": 0}  # temps de notif de CE cycle seulement
        echec_cycle = False
        try:
            if session_boursier:
                await scan_source(application, session_non_boursier, "non-boursier",
                                  vues=[False], stats_jour=stats_jour, notif_perf=notif_perf)
                await scan_source(application, session_boursier, "boursier",
                                  vues=[True], stats_jour=stats_jour, notif_perf=notif_perf)

                # cookies morts ? vérification DIRECTE (même signal que le
                # bouton « Déconnexion » du site : bloc user du contexte
                # global) — 1 requête légère par heure, dès le démarrage
                if time.time() - derniere_verif_cookie > 3600:
                    derniere_verif_cookie = time.time()
                    valides = await asyncio.to_thread(
                        scraper.cookies_valides, session_boursier)
                    if valides is False:
                        log.warning("Session boursier déconnectée : cookies expirés !")
                        if time.time() - derniere_alerte_cookie > 24 * 3600:
                            derniere_alerte_cookie = time.time()
                            for admin_id in config.ADMIN_CHAT_IDS:
                                await notifier.send(
                                    application.bot, admin_id,
                                    "🍪 <b>Alerte cookies !</b>\n\n"
                                    "Le site ne reconnaît plus la session "
                                    "« boursier » (cookies expirés) : en attendant, "
                                    "les abonnés boursiers reçoivent la même vue "
                                    "que les non-boursiers.\n\n"
                                    "👉 Reconnecte-toi sur trouverunlogement.lescrous.fr, "
                                    "réexporte les cookies dans Cookies_Hard.JSON et "
                                    "relance le bot."
                                )
            else:
                # pas de cookies : tout le monde suit la vue non-boursier
                await scan_source(application, session_non_boursier, "non-boursier",
                                  vues=[False, True], stats_jour=stats_jour, notif_perf=notif_perf)
            stats_jour["cycles"] += 1

            # temps de notif moyen de ce cycle : log systématique + alerte
            # admin si trop lent (échantillon minimum pour éviter le bruit)
            moyenne_notif = (notif_perf["total"] / notif_perf["nb"]
                              if notif_perf["nb"] > 0 else None)
            if moyenne_notif is not None and notif_perf["nb"] >= config.LATENCE_ECHANTILLON_MIN:
                log.info("Temps de notif moyen : %.2fs/notif (%d notifs ce cycle).",
                         moyenne_notif, notif_perf["nb"])
                if (moyenne_notif > config.SEUIL_LATENCE_NOTIF
                        and time.time() - derniere_alerte_notif > config.ALERTE_LATENCE_COOLDOWN):
                    derniere_alerte_notif = time.time()
                    for admin_id in config.ADMIN_CHAT_IDS:
                        await notifier.send(
                            application.bot, admin_id,
                            f"🐢 <b>Notifs lentes</b>\n\n"
                            f"Temps moyen : {moyenne_notif:.1f}s/notif "
                            f"(seuil : {config.SEUIL_LATENCE_NOTIF}s), "
                            f"sur {notif_perf['nb']} notifs ce cycle."
                        )

            # temps de réponse moyen aux commandes/boutons : idem
            moyenne_reponse, nb_reponses = bot.stats_latence_reponse()
            if moyenne_reponse is not None and nb_reponses >= config.LATENCE_ECHANTILLON_MIN:
                log.info("Temps de réponse moyen : %.2fs (%d messages traités).",
                         moyenne_reponse, nb_reponses)
                if (moyenne_reponse > config.SEUIL_LATENCE_REPONSE
                        and time.time() - derniere_alerte_reponse > config.ALERTE_LATENCE_COOLDOWN):
                    derniere_alerte_reponse = time.time()
                    for admin_id in config.ADMIN_CHAT_IDS:
                        await notifier.send(
                            application.bot, admin_id,
                            f"🐢 <b>Réponses lentes</b>\n\n"
                            f"Temps de réponse moyen : {moyenne_reponse:.1f}s "
                            f"(seuil : {config.SEUIL_LATENCE_REPONSE}s), "
                            f"sur {nb_reponses} messages traités."
                        )

            # mémorise les derniers chiffres connus pour /perf (affichage
            # à la demande, sans attendre le rapport quotidien)
            bot.maj_stats_perf(
                notif_moyenne=moyenne_notif,
                notif_nb=notif_perf["nb"],
                reponse_moyenne=moyenne_reponse,
                reponse_nb=nb_reponses,
                cycles_jour=stats_jour["cycles"],
                nouveaux_jour=stats_jour["nouveaux"],
                envois_jour=stats_jour["envois"],
            )

            # rappel d'expiration : prévenir une fois, à J-3
            for chat_id, expire in db.users_a_rappeler(config.RAPPEL_JOURS):
                await notifier.send(
                    application.bot, chat_id,
                    f"⏳ Ton accès expire le "
                    f"{datetime.fromisoformat(expire).strftime('%d/%m/%Y')} !\n"
                    f"Pour continuer à recevoir les logements, appuie sur "
                    f"« 🔎 Trouver mon logement » pour renouveler."
                )
                db.marquer_rappel(chat_id)

            # fin d'offre flash (/promo) : retour silencieux au tarif
            # normal, sans notif — n'affecte que ceux qui n'ont pas payé
            # à temps, jamais les abonnés qui ont déjà réglé entre-temps
            for chat_id in db.get_promos_expirees():
                db.revert_promo(chat_id)

            # suivi des candidatures en attente ("tu l'as eu ?" du lendemain)
            await relancer_candidatures(application)

            # rapport quotidien aux admins (au premier cycle du nouveau jour)
            if datetime.now().date() != jour_courant and config.ADMIN_CHAT_IDS:
                s = db.get_stats()
                rapport = (
                    f"🩺 <b>Rapport quotidien — "
                    f"{jour_courant.strftime('%d/%m/%Y')}</b>\n\n"
                    f"✅ Bot en vie\n"
                    f"🔄 {stats_jour['cycles']} cycles de scan\n"
                    f"🆕 {stats_jour['nouveaux']} nouveaux logements détectés\n"
                    f"📤 {stats_jour['envois']} notifications envoyées\n"
                    f"👥 {s['actifs']} abonnés actifs ({s['total']} inscrits)"
                )
                for admin_id in config.ADMIN_CHAT_IDS:
                    await notifier.send(application.bot, admin_id, rapport)
                jour_courant = datetime.now().date()
                stats_jour = {"cycles": 0, "nouveaux": 0, "envois": 0}

        except Exception:
            log.exception("Erreur pendant le cycle de scan (on réessaie au prochain).")
            echec_cycle = True

        # la pause démarre APRÈS la fin des envois : sous forte charge le
        # cycle s'étire tout seul, la file ne peut jamais s'empiler
        duree = asyncio.get_running_loop().time() - debut_cycle
        if duree > 60:
            log.warning("Cycle long : %.0fs (grosse vague de notifications ?)", duree)
            # alerte Telegram aux admins, au plus 1 fois par heure
            if config.ADMIN_CHAT_IDS and time.time() - derniere_alerte > 3600:
                derniere_alerte = time.time()
                for admin_id in config.ADMIN_CHAT_IDS:
                    await notifier.send(
                        application.bot, admin_id,
                        f"⚠️ <b>Cycle long : {duree:.0f}s</b> (au lieu de ~5s)\n"
                        f"Grosse vague de notifications ou site lent. Rien de "
                        f"cassé — la file s'écoule — mais garde un œil dessus."
                    )
        else:
            log.info("Cycle terminé en %.1fs.", duree)

        # cycle en échec (site surchargé/en erreur) -> on retente plus vite
        # pour attraper la réouverture ; dès qu'un cycle réussit, retour
        # à l'intervalle normal automatiquement, sans intervention
        intervalle = (config.SCAN_INTERVAL_RECUPERATION if echec_cycle
                      else config.SCAN_INTERVAL)
        if echec_cycle:
            log.info("Site en échec : prochain essai dans %ds (au lieu de %ds).",
                      intervalle, config.SCAN_INTERVAL)
        await asyncio.sleep(intervalle)


# tâche de scan en variable de module (PAS dans application.bot_data) :
# un Task asyncio n'est pas sérialisable, et bot_data est justement ce que
# la persistence essaie de sauvegarder périodiquement sur disque
_scan_task = None


async def post_init(application):
    """Appelé par python-telegram-bot au démarrage :
    on lance la boucle de scan en tâche de fond sur la même boucle asyncio.
    Prévient aussi les admins par Telegram — appelé à CHAQUE démarrage, y
    compris après un crash-restart automatique de systemd, donc utile pour
    repérer un plantage silencieux entre deux rapports quotidiens."""
    global _scan_task
    _scan_task = asyncio.get_running_loop().create_task(scan_loop(application))
    log.info("Boucle de scan lancée (cycle : %ds).", config.SCAN_INTERVAL)
    for admin_id in config.ADMIN_CHAT_IDS:
        await notifier.send(
            application.bot, admin_id,
            f"🔄 Bot démarré — {datetime.now().strftime('%d/%m/%Y %H:%M:%S')}"
        )


async def post_shutdown(application):
    """Au Ctrl+C : on arrête la boucle de scan proprement."""
    if _scan_task:
        _scan_task.cancel()
    log.info("Boucle de scan arrêtée.")


def main():
    db.init_db()
    log.info("Base initialisée (%d abonné(s) existant(s)).", db.count_users())

    # état des conversations /start-/changer en cours : survit à un
    # redémarrage (cf. ConversationHandler(name=..., persistent=True) dans
    # bot.py). Sauvegardé sur disque toutes les 60s + à l'arrêt propre.
    persistance = PicklePersistence(filepath=config.PERSISTENCE_FILE)

    application = (
        Application.builder()
        .token(config.TELEGRAM_TOKEN)
        .persistence(persistance)
        .post_init(post_init)
        .post_shutdown(post_shutdown)
        # par défaut PTB traite les updates UN PAR UN (max_concurrent_updates=1) :
        # avec plusieurs clients actifs en même temps, le 2e attendrait que le
        # 1er finisse entièrement. On autorise jusqu'à 64 updates en parallèle.
        .concurrent_updates(64)
        .build()
    )
    bot.register_handlers(application)

    log.info("🚀 Bot démarré — /start sur Telegram pour s'inscrire.")
    application.run_polling()  # bloquant, Ctrl+C pour arrêter


if __name__ == "__main__":
    main()
