"""
main.py — Nova Media Radio
Point d'entrée principal. Lance les 3 threads :
  - Thread 1 : NewsWatcher  (surveillance JSON)
  - Thread 2 : Streamer     (flux Icecast)
  - Thread 3 : JournalBuilder (génération TTS + mixage, ponctuel)

Usage :
  python main.py           # mode normal
  python main.py --debug   # mode debug (logs ffmpeg, niveau DEBUG partout)
"""

import argparse
import logging
import signal
import sys
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

import yaml

from modules.journal_builder import JournalBuilder, verify_messages_file
from modules.news_watcher import NewsWatcher
from modules.streamer import Streamer

# ------------------------------------------------------------------ #
#  Arguments CLI                                                       #
# ------------------------------------------------------------------ #

def parse_args():
    parser = argparse.ArgumentParser(description="Nova Media — Radio IA automatisée")
    parser.add_argument(
        "--debug",
        action="store_true",
        help="Active le mode debug : logs ffmpeg, niveau DEBUG sur tous les modules"
    )
    parser.add_argument(
        "--config",
        default="config/config.yaml",
        help="Chemin vers le fichier de configuration (défaut: config/config.yaml)"
    )
    return parser.parse_args()


# ------------------------------------------------------------------ #
#  Logging                                                             #
# ------------------------------------------------------------------ #

def setup_logging(debug: bool):
    level = logging.DEBUG if debug else logging.INFO
    log_file = "nova_media_debug.log" if debug else "nova_media.log"

    # Format plus détaillé en debug
    fmt = (
        "%(asctime)s  %(levelname)-8s  %(name)s [%(threadName)s] — %(message)s"
        if debug else
        "%(asctime)s  %(levelname)-8s  %(name)s — %(message)s"
    )

    logging.basicConfig(
        level=level,
        format=fmt,
        datefmt="%H:%M:%S",
        handlers=[
            logging.StreamHandler(sys.stdout),
            logging.FileHandler(log_file, encoding="utf-8"),
        ],
    )

    if debug:
        # Activer DEBUG sur tous nos modules
        for name in ["nova.main", "nova.streamer", "nova.journal", "nova.watcher"]:
            logging.getLogger(name).setLevel(logging.DEBUG)
        logging.getLogger("nova.main").info(
            f"🐛 Mode DEBUG activé — logs écrits dans {log_file}"
        )


# ------------------------------------------------------------------ #
#  Chargement de la configuration                                      #
# ------------------------------------------------------------------ #

def load_config(path: str = "config/config.yaml") -> dict:
    with open(path, "r", encoding="utf-8") as f:
        return yaml.safe_load(f)


# ------------------------------------------------------------------ #
#  Bootstrap                                                           #
# ------------------------------------------------------------------ #

def create_directories(config: dict):
    radio = config["radio"]
    dirs = [
        radio["music_dir"],
        radio["background_music_dir"],
        radio["audio_queue_dir"],
        radio["tmp_dir"],
        radio["data_dir"],
        "data",
    ]
    for d in dirs:
        Path(d).mkdir(parents=True, exist_ok=True)


def check_prerequisites(logger):
    import subprocess
    try:
        result = subprocess.run(
            ["ffmpeg", "-version"], capture_output=True, check=True, text=True
        )
        # Extraire la version ffmpeg pour les logs debug
        version_line = result.stdout.splitlines()[0] if result.stdout else "version inconnue"
        logger.debug(f"ffmpeg détecté : {version_line}")
    except (subprocess.CalledProcessError, FileNotFoundError):
        if sys.platform == "win32":
            logger.error(
                "❌ ffmpeg introuvable.\n"
                "   → Téléchargez-le sur https://ffmpeg.org/download.html\n"
                "   → Ajoutez C:\\ffmpeg\\bin à votre PATH système.\n"
                "   → Redémarrez votre terminal après modification du PATH."
            )
        else:
            logger.error("❌ ffmpeg introuvable. Installez-le avec : sudo apt install ffmpeg")
        sys.exit(1)

    try:
        import edge_tts  # noqa
    except ImportError:
        logger.error("❌ edge-tts introuvable. Lancez : pip install edge-tts")
        sys.exit(1)

    logger.info(f"✅ Prérequis OK (ffmpeg + edge-tts) — plateforme : {sys.platform}")


# ------------------------------------------------------------------ #
#  Main                                                                #
# ------------------------------------------------------------------ #

def main():
    args = parse_args()
    setup_logging(args.debug)
    logger = logging.getLogger("nova.main")

    logger.info("=" * 55)
    logger.info("  🎙️  NOVA MEDIA — Démarrage")
    if args.debug:
        logger.info("  🐛  MODE DEBUG ACTIVÉ")
    logger.info("=" * 55)

    config = load_config(args.config)

    if args.debug:
        logger.debug(f"Config chargée depuis : {args.config}")
        logger.debug(f"Icecast : {config['icecast']['host']}:{config['icecast']['port']}{config['icecast']['mount']}")
        logger.debug(f"Music dir : {config['radio']['music_dir']}")
        logger.debug(f"Bitrate : {config['audio']['bitrate']} — Sample rate : {config['audio']['sample_rate']}")

    create_directories(config)
    check_prerequisites(logger)
    verify_messages_file()

    # Passer le flag debug aux modules
    config["_debug"] = args.debug

    streamer = Streamer(config)
    builder  = JournalBuilder(config)

    def on_bulletin_ready(articles: list):
        def _generate():
            path = builder.build(articles)
            if path:
                streamer.enqueue_bulletin(path)
            else:
                logger.error("❌ Échec génération du journal")
        t = threading.Thread(target=_generate, daemon=True, name="BulletinGen")
        t.start()

    watcher = NewsWatcher(config["radio"], on_bulletin_ready)

    t_watcher = threading.Thread(target=watcher.run, daemon=True, name="NewsWatcher")
    t_streamer = threading.Thread(target=streamer.run, daemon=True, name="Streamer")

    t_watcher.start()
    logger.info("🔍 Thread NewsWatcher démarré")

    logger.info("⏳ Attente 2s avant démarrage streamer…")
    time.sleep(2)
    logger.info("⏳ Sleep terminé, lancement streamer…")

    t_streamer.start()
    logger.info("📻 Thread Streamer démarré")

    def shutdown(sig, frame):
        logger.info("\n⛔ Arrêt demandé…")
        watcher.stop()
        streamer.stop()
        logger.info("👋 Nova Media arrêtée proprement")
        sys.exit(0)

    signal.signal(signal.SIGINT, shutdown)
    if sys.platform != "win32":
        signal.signal(signal.SIGTERM, shutdown)

    logger.info("🟢 Nova Media est en ligne — écouter sur http://localhost:8000/nova")
    logger.info("   (Ctrl+C pour arrêter)")

    try:
        while True:
            time.sleep(1)
            if not t_watcher.is_alive():
                logger.error("❌ Thread NewsWatcher mort de façon inattendue !")
            if not t_streamer.is_alive():
                logger.error("❌ Thread Streamer mort de façon inattendue !")
    except Exception as e:
        logger.error(f"❌ Erreur fatale : {e}", exc_info=True)
        shutdown(None, None)


if __name__ == "__main__":
    main()
