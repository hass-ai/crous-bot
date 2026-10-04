# ============================================================
#  DIFF — comparaison T1 (état en base) vs T2 (scan courant).
#  Nouveau = disponible MAINTENANT et pas disponible AVANT
#  (jamais vu, complet, ou absent du scan précédent).
#  -> une place LIBÉRÉE re-déclenche bien une notification,
#     même si son ID a déjà été vu il y a des semaines.
# ============================================================

import db


def find_new(logements, source, scan_complet=True):
    """
    logements = TOUT le scan (disponibles ET complets).
    Retourne (nouveaux, premier_run) :
      - premier_run=True -> base vide : on enregistre tout SANS notifier
        (sinon 1400 notifications au premier démarrage)
      - scan_complet=False (le site a rendu une liste partielle) : on met
        à jour les états scannés mais on ne marque PAS les absents comme
        indisponibles, sinon leur retour créerait de fausses libérations
    """
    etats_avant = db.get_known_states(source)  # T1
    premier_run = len(etats_avant) == 0

    nouveaux = [l for l in logements
                if l["disponible"] and not etats_avant.get(l["id"], 0)]

    # T2 devient le nouveau T1 pour le prochain cycle
    db.save_known_states({l["id"]: l["disponible"] for l in logements},
                         source, marquer_absents=scan_complet)

    return ([] if premier_run else nouveaux), premier_run
