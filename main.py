"""
main.py — Nova Media Radio
Point d'entrée principal. Lance les 3 threads :
  - Thread 1 : NewsWatcher  (surveillance JSON)
  - Thread 2 : Streamer     (flux Icecast)
  - Thread 3 : JournalBuilder (génération TTS + mixage, ponctuel)
"""

import logging
import signal
import sys
import threading
import time
from pathlib import Path

# Garantit que le dossier du projet est dans le path Python
# (nécessaire notamment sur Windows)
sys.path.insert(0, str(Path(__file__).parent))

import yaml

from modules.journal_builder import JournalBuilder
from modules.news_watcher import NewsWatcher
from modules.streamer import Streamer

# ------------------------------------------------------------------ #
#  Logging                                                             #
# ------------------------------------------------------------------ #

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(name)s — %(message)s",
    datefmt="%H:%M:%S",
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler("nova_media.log", encoding="utf-8"),
    ],
)
logger = logging.getLogger("nova.main")


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
    """Crée les dossiers nécessaires s'ils n'existent pas."""
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


def check_prerequisites():
    """Vérifie que ffmpeg est disponible."""
    import subprocess
    try:
        subprocess.run(["ffmpeg", "-version"], capture_output=True, check=True)
    except (subprocess.CalledProcessError, FileNotFoundError):
        if sys.platform == "win32":
            logger.error(
                "❌ ffmpeg introuvable.\n"
                "   → Téléchargez-le sur https://ffmpeg.org/download.html\n"
                "   → Extrayez l'archive et ajoutez le dossier bin\\ à votre PATH système.\n"
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
    logger.info("=" * 55)
    logger.info("  🎙️  NOVA MEDIA — Démarrage")
    logger.info("=" * 55)

    config = load_config()
    create_directories(config)
    check_prerequisites()

    # Instanciation des modules
    streamer = Streamer(config)
    builder = JournalBuilder(config)

    def on_bulletin_ready(articles: list):
        """Callback : le watcher a collecté N news → on génère le journal."""
        def _generate():
            path = builder.build(articles)
            if path:
                streamer.enqueue_bulletin(path)
            else:
                logger.error("❌ Échec génération du journal")
        t = threading.Thread(target=_generate, daemon=True, name="BulletinGen")
        t.start()

    watcher = NewsWatcher(config["radio"], on_bulletin_ready)

    # ------------------------------------------------------------------ #
    #  Lancement des threads                                              #
    # ------------------------------------------------------------------ #

    t_watcher = threading.Thread(target=watcher.run, daemon=True, name="NewsWatcher")
    t_streamer = threading.Thread(target=streamer.run, daemon=True, name="Streamer")

    t_watcher.start()
    logger.info("🔍 Thread NewsWatcher démarré")

    logger.info("⏳ Attente 2s avant démarrage streamer…")
    time.sleep(2)
    logger.info("⏳ Sleep terminé, lancement streamer…")

    t_streamer.start()
    logger.info("📻 Thread Streamer démarré")

    # ------------------------------------------------------------------ #
    #  Gestion du signal d'arrêt (Ctrl+C)                                #
    # ------------------------------------------------------------------ #

    def shutdown(sig, frame):
        logger.info("\n⛔ Arrêt demandé…")
        watcher.stop()
        streamer.stop()
        logger.info("👋 Nova Media arrêtée proprement")
        sys.exit(0)

    signal.signal(signal.SIGINT, shutdown)
    # SIGTERM n'existe pas sous Windows
    if sys.platform != "win32":
        signal.signal(signal.SIGTERM, shutdown)

    logger.info("🟢 Nova Media est en ligne — écouter sur http://localhost:8000/nova")
    logger.info("   (Ctrl+C pour arrêter)")

    # Maintenir le thread principal vivant
    try:
        while True:
            time.sleep(1)
            # Vérifier que les threads sont toujours vivants
            if not t_watcher.is_alive():
                logger.error("❌ Thread NewsWatcher mort de façon inattendue !")
            if not t_streamer.is_alive():
                logger.error("❌ Thread Streamer mort de façon inattendue !")
    except Exception as e:
        logger.error(f"❌ Erreur fatale dans la boucle principale : {e}", exc_info=True)
        shutdown(None, None)


if __name__ == "__main__":
    main()
