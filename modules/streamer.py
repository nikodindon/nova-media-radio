"""
streamer.py
Gère le flux audio continu vers Icecast via un pipe ffmpeg unique.

Principe :
  - Un seul processus ffmpeg reste actif en permanence (pipe stdin)
  - On écrit les fichiers audio dedans un par un (musique ou journal)
  - Fadeout : appliqué EN UNE SEULE PASSE ffmpeg avant le stream,
    pas chunk par chunk (évite les micro-coupures)
  - Aucune relance brutale du process principal → pas de coupure VLC
"""

import logging
import random
import subprocess
import tempfile
import threading
import time
from pathlib import Path
from queue import Empty, Queue

logger = logging.getLogger("nova.streamer")

CHUNK_SIZE = 65536  # 64KB
FADE_DURATION = 3.0  # secondes de fadeout musical

# Buffer de silence global (initialisé dans Streamer.__init__)
_SILENCE_BYTES: bytes = b""


def _generate_silence(sample_rate: int, bitrate: str, duration: float = 0.5) -> bytes:
    """Génère un court buffer de silence MP3."""
    cmd = [
        "ffmpeg", "-y",
        "-f", "lavfi",
        "-i", f"anullsrc=r={sample_rate}:cl=stereo",
        "-t", str(duration),
        "-b:a", bitrate,
        "-codec:a", "libmp3lame",
        "-f", "mp3", "pipe:1"
    ]
    try:
        result = subprocess.run(cmd, capture_output=True, timeout=10)
        return result.stdout
    except Exception:
        return b""


class Streamer:
    def __init__(self, config: dict):
        radio = config["radio"]
        ice = config["icecast"]
        audio = config["audio"]

        self.music_dir = Path(radio["music_dir"])
        self.queue_dir = Path(radio["audio_queue_dir"])
        self.bitrate = audio["bitrate"]
        self.sample_rate = audio["sample_rate"]

        self.icecast_url = (
            f"icecast://{ice['user']}:{ice['password']}"
            f"@{ice['host']}:{ice['port']}{ice['mount']}"
        )

        self._stop_event = threading.Event()
        self._fade_requested = threading.Event()
        self._ffmpeg_proc: subprocess.Popen | None = None
        self._play_queue: Queue = Queue()
        self._lock = threading.Lock()

        self.queue_dir.mkdir(parents=True, exist_ok=True)

        global _SILENCE_BYTES
        _SILENCE_BYTES = _generate_silence(self.sample_rate, self.bitrate)
        if _SILENCE_BYTES:
            logger.info("🔇 Buffer de silence initialisé")
        else:
            logger.warning("⚠️ Impossible de générer le buffer de silence")

    # ------------------------------------------------------------------ #
    #  API publique                                                        #
    # ------------------------------------------------------------------ #

    def enqueue_bulletin(self, path: Path):
        """Ajoute un journal à la file et demande un fadeout de la musique."""
        if path and path.exists():
            self._play_queue.put(path)
            logger.info(f"📥 Journal en file : {path.name}")
            self._fade_requested.set()

    def run(self):
        logger.info("📻 Démarrage du streamer Nova Media")
        self._start_ffmpeg()
        try:
            while not self._stop_event.is_set():
                self._play_next()
        except Exception as e:
            logger.error(f"Erreur streamer : {e}", exc_info=True)
        finally:
            self._kill_ffmpeg()

    def stop(self):
        self._stop_event.set()

    # ------------------------------------------------------------------ #
    #  Logique de lecture                                                  #
    # ------------------------------------------------------------------ #

    def _play_next(self):
        """Joue le prochain fichier : journal prioritaire, sinon musique."""
        try:
            bulletin = self._play_queue.get_nowait()
            logger.info(f"🎙️ Diffusion journal : {bulletin.name}")
            self._stream_file(bulletin, is_music=False)
            bulletin.unlink(missing_ok=True)
        except Empty:
            music = self._pick_music()
            if music:
                # Journal déjà en file avant même que la musique commence ?
                # → musique courte avec fadeout d'emblée
                if self._fade_requested.is_set():
                    self._play_music_short_with_fadeout(music)
                else:
                    logger.info(f"🎵 Musique : {music.name}")
                    self._stream_file(music, is_music=True)
            else:
                logger.warning("⚠️ Aucune musique trouvée, attente 5s…")
                time.sleep(5)

    def _play_music_short_with_fadeout(self, music_path: Path):
        """
        Joue ~15s de musique puis fadeout de 3s avant de passer au journal.
        Utilisé quand un journal est déjà prêt avant que la musique commence.
        """
        logger.info(f"🎵 Musique courte avant journal : {music_path.name}")
        self._fade_requested.clear()

        SHORT = 15.0
        tmp = tempfile.NamedTemporaryFile(suffix=".mp3", delete=False)
        tmp.close()
        out_path = Path(tmp.name)

        cmd = [
            "ffmpeg", "-y",
            "-i", str(music_path),
            "-t", str(SHORT + FADE_DURATION),
            "-af", f"afade=t=out:st={SHORT:.1f}:d={FADE_DURATION:.1f}",
            "-ar", str(self.sample_rate),
            "-ac", "2",
            "-b:a", self.bitrate,
            "-codec:a", "libmp3lame",
            str(out_path)
        ]
        try:
            result = subprocess.run(cmd, capture_output=True, timeout=60)
            if result.returncode == 0:
                self._stream_file(out_path, is_music=False)
            else:
                logger.warning("Musique courte échouée, passage direct au journal")
        except Exception as e:
            logger.warning(f"Erreur musique courte : {e}")
        finally:
            out_path.unlink(missing_ok=True)

    def _pick_music(self) -> Path | None:
        if not self.music_dir.exists():
            return None
        files = list(self.music_dir.glob("*.mp3"))
        return random.choice(files) if files else None

    # ------------------------------------------------------------------ #
    #  Streaming d'un fichier                                              #
    # ------------------------------------------------------------------ #

    def _stream_file(self, path: Path, is_music: bool = True):
        """
        Réencode et streame le fichier vers Icecast via le pipe ffmpeg.
        Si is_music=True et qu'un journal arrive, interrompt proprement
        avec fadeout EN UNE PASSE (pas chunk par chunk).
        """
        if self._ffmpeg_proc is None or self._ffmpeg_proc.poll() is not None:
            logger.warning("⚠️ Process ffmpeg mort, relance…")
            self._start_ffmpeg()
            time.sleep(1)

        self._write_silence()

        transcode_cmd = [
            "ffmpeg", "-y",
            "-i", str(path),
            "-ar", str(self.sample_rate),
            "-ac", "2",
            "-b:a", self.bitrate,
            "-codec:a", "libmp3lame",
            "-f", "mp3", "pipe:1"
        ]

        try:
            transcode = subprocess.Popen(
                transcode_cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
            )

            while not self._stop_event.is_set():

                # Journal arrivé pendant la musique → fadeout en une passe
                if is_music and self._fade_requested.is_set():
                    logger.info("🎚️ Journal détecté — fadeout en cours…")
                    self._fade_requested.clear()
                    transcode.kill()
                    transcode.stdout.close()
                    transcode.wait(timeout=3)
                    self._do_fadeout_tail(path)
                    return

                chunk = transcode.stdout.read(CHUNK_SIZE)
                if not chunk:
                    break

                try:
                    self._ffmpeg_proc.stdin.write(chunk)
                    self._ffmpeg_proc.stdin.flush()
                except (BrokenPipeError, OSError):
                    logger.error("Pipe Icecast cassé, relance…")
                    transcode.kill()
                    self._start_ffmpeg()
                    return

            transcode.stdout.close()
            transcode.wait(timeout=3)

        except Exception as e:
            logger.error(f"Erreur streaming {path.name} : {e}")

    def _do_fadeout_tail(self, original_path: Path):
        """
        Prend les dernières (FADE_DURATION * 2) secondes du fichier original,
        applique un fadeout en une passe ffmpeg, streame le résultat.
        Cela donne un fondu propre sans micro-coupures.
        """
        logger.info("🎚️ Génération fadeout final…")
        tmp = tempfile.NamedTemporaryFile(suffix=".mp3", delete=False)
        tmp.close()
        out_path = Path(tmp.name)

        segment = FADE_DURATION * 2  # quelques secondes avant le fadeout
        cmd = [
            "ffmpeg", "-y",
            "-sseof", f"-{segment:.1f}",  # N secondes avant la fin du fichier
            "-i", str(original_path),
            "-af", f"afade=t=out:st=0:d={FADE_DURATION:.1f}",
            "-ar", str(self.sample_rate),
            "-ac", "2",
            "-b:a", self.bitrate,
            "-codec:a", "libmp3lame",
            str(out_path)
        ]
        try:
            result = subprocess.run(cmd, capture_output=True, timeout=30)
            if result.returncode == 0 and out_path.stat().st_size > 0:
                logger.info("🎚️ Fadeout prêt, diffusion…")
                self._stream_file(out_path, is_music=False)
            else:
                logger.warning("Fadeout final échoué, passage direct au journal")
        except Exception as e:
            logger.warning(f"Erreur fadeout final : {e}")
        finally:
            out_path.unlink(missing_ok=True)

    # ------------------------------------------------------------------ #
    #  Silence entre fichiers                                              #
    # ------------------------------------------------------------------ #

    def _write_silence(self):
        if not _SILENCE_BYTES:
            return
        try:
            self._ffmpeg_proc.stdin.write(_SILENCE_BYTES)
            self._ffmpeg_proc.stdin.flush()
        except (BrokenPipeError, OSError):
            pass

    # ------------------------------------------------------------------ #
    #  Process ffmpeg → Icecast                                           #
    # ------------------------------------------------------------------ #

    def _start_ffmpeg(self):
        with self._lock:
            self._kill_ffmpeg()
            cmd = [
                "ffmpeg",
                "-re",
                "-f", "mp3",
                "-i", "pipe:0",
                "-ar", str(self.sample_rate),
                "-ac", "2",
                "-b:a", self.bitrate,
                "-codec:a", "libmp3lame",
                "-f", "mp3",
                "-content_type", "audio/mpeg",
                "-ice_name", "Nova Media",
                "-ice_description", "Radio IA automatisée",
                self.icecast_url,
            ]
            for attempt in range(1, 11):
                try:
                    self._ffmpeg_proc = subprocess.Popen(
                        cmd,
                        stdin=subprocess.PIPE,
                        stdout=subprocess.DEVNULL,
                        stderr=subprocess.DEVNULL,
                    )
                    time.sleep(1.5)
                    if self._ffmpeg_proc.poll() is not None:
                        raise ConnectionError("ffmpeg s'est arrêté immédiatement")
                    logger.info(f"🔗 Connecté à Icecast (tentative {attempt})")
                    return
                except Exception as e:
                    logger.warning(f"⏳ Icecast non joignable (tentative {attempt}/10) : {e}")
                    self._ffmpeg_proc = None
                    time.sleep(3)
            logger.error("❌ Impossible de se connecter à Icecast après 10 tentatives.")

    def _kill_ffmpeg(self):
        if self._ffmpeg_proc and self._ffmpeg_proc.poll() is None:
            try:
                self._ffmpeg_proc.stdin.close()
            except Exception:
                pass
            self._ffmpeg_proc.terminate()
            try:
                self._ffmpeg_proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self._ffmpeg_proc.kill()
        self._ffmpeg_proc = None
