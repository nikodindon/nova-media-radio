"""
news_watcher.py
Surveille le fichier JSON du jour et détecte les nouvelles entrées.
"""

import json
import time
import logging
import threading
from datetime import datetime
from pathlib import Path

logger = logging.getLogger("nova.watcher")


class NewsWatcher:
    def __init__(self, config: dict, on_bulletin_ready):
        """
        config          : section 'radio' du config.yaml
        on_bulletin_ready : callable(articles: list[dict]) appelé quand N news sont prêtes
        """
        self.config = config
        self.on_bulletin_ready = on_bulletin_ready
        self.data_dir = Path(config["data_dir"])
        self.processed_file = Path(config["processed_hashes_file"])
        self.interval = config["news_interval_seconds"]
        self.per_bulletin = config["news_per_bulletin"]

        self._stop_event = threading.Event()
        self._processed_hashes: set = self._load_processed_hashes()
        self._pending: list = []

        # Compte initial : on charge les hashes actuels SANS les traiter
        self._init_existing_hashes()

    # ------------------------------------------------------------------ #
    #  Hashes persistants                                                  #
    # ------------------------------------------------------------------ #

    def _load_processed_hashes(self) -> set:
        if self.processed_file.exists():
            try:
                with open(self.processed_file, "r") as f:
                    return set(json.load(f))
            except Exception:
                return set()
        return set()

    def _save_processed_hashes(self):
        self.processed_file.parent.mkdir(parents=True, exist_ok=True)
        with open(self.processed_file, "w") as f:
            json.dump(list(self._processed_hashes), f)

    # ------------------------------------------------------------------ #
    #  Initialisation : ignorer les news déjà présentes au démarrage      #
    # ------------------------------------------------------------------ #

    def _init_existing_hashes(self):
        articles = self._read_today_articles()
        already = len(articles)
        for a in articles:
            self._processed_hashes.add(a["hash"])
        self._save_processed_hashes()
        logger.info(f"📍 {already} news déjà présentes → ignorées")
        logger.info(f"👀 En attente de {self.per_bulletin} NOUVELLES news (0/{self.per_bulletin})")

    # ------------------------------------------------------------------ #
    #  Lecture du JSON du jour — avec retry sur Windows                   #
    # ------------------------------------------------------------------ #

    def _get_today_json_path(self) -> Path:
        today = datetime.now().strftime("%Y%m%d")
        return self.data_dir / f"{today}_articles.json"

    def _read_today_articles(self) -> list:
        """
        Lit le JSON des articles du jour.
        Sur Windows, atlas_fetch.py écrit via os.replace() d'un fichier .tmp —
        si on lit exactement pendant ce remplacement on peut avoir une PermissionError
        ou lire un fichier vide/corrompu. On retry jusqu'à 3 fois avec une pause courte.
        """
        path = self._get_today_json_path()
        if not path.exists():
            return []

        last_error = None
        for attempt in range(3):
            try:
                # Lecture en mode binaire puis décodage — évite les locks partiels
                raw = path.read_bytes()
                if not raw.strip():
                    # Fichier vide momentané (pendant le replace) — on attend
                    time.sleep(0.2)
                    continue
                data = json.loads(raw.decode("utf-8"))
                return data if isinstance(data, list) else []
            except (PermissionError, OSError) as e:
                last_error = e
                logger.debug(f"Retry lecture JSON ({attempt+1}/3) : {e}")
                time.sleep(0.3 * (attempt + 1))
            except json.JSONDecodeError as e:
                # JSON incomplet pendant l'écriture — on attend et on réessaie
                last_error = e
                logger.debug(f"JSON incomplet, retry ({attempt+1}/3)")
                time.sleep(0.2)

        if last_error:
            logger.warning(f"Erreur lecture JSON après 3 tentatives : {last_error}")
        return []

    # ------------------------------------------------------------------ #
    #  Boucle principale                                                   #
    # ------------------------------------------------------------------ #

    def run(self):
        logger.info("🔍 Surveillance des news démarrée")
        while not self._stop_event.is_set():
            try:
                self._check_new_articles()
            except Exception as e:
                logger.error(f"Erreur watcher : {e}")
            time.sleep(self.interval)

    def _check_new_articles(self):
        articles = self._read_today_articles()
        new_ones = [a for a in articles if a["hash"] not in self._processed_hashes]

        for article in new_ones:
            summary = article.get("summary", "").strip()

            # Ignorer les news avec contenu inaccessible
            if summary.startswith("[Contenu inaccessible]"):
                self._processed_hashes.add(article["hash"])
                logger.info(f"⏭️  News ignorée (contenu inaccessible) : {article.get('title', '')[:60]}")
                continue

            self._pending.append(article)
            self._processed_hashes.add(article["hash"])
            count = len(self._pending)
            logger.info(f"📰 {count}/{self.per_bulletin} nouvelles news détectées")

        # Déclencher un journal dès qu'on a assez de news.
        # Si plusieurs news arrivent en même temps (ex: 8 d'un coup pour per_bulletin=5),
        # on les met toutes dans le même journal au lieu de les couper.
        if len(self._pending) >= self.per_bulletin:
            batch = list(self._pending)        # on prend TOUT ce qui est en attente
            self._pending = []
            self._save_processed_hashes()
            logger.info(f"🎙️ Déclenchement journal ({len(batch)} news)")
            self.on_bulletin_ready(batch)

    def stop(self):
        self._stop_event.set()
