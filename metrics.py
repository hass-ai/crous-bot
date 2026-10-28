from prometheus_client import Counter, Gauge

LOGEMENTS = Gauge("crous_logements_disponibles", "Logements disponibles au dernier scan", ["source"])
NOTIFICATIONS = Counter("crous_notifications_envoyees_total", "Notifications envoyées aux abonnés")
SCANS_ECHEC = Counter("crous_scans_echec_total", "Cycles de scan en échec")
DERNIER_SCAN_OK = Gauge("crous_dernier_scan_reussi_timestamp_seconds", "Heure du dernier scan réussi")