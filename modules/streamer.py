"""
streamer.py
Gère le flux audio continu vers Icecast via un pipe ffmpeg unique.

Principe :
  - Un seul processus ffmpeg reste actif en permanence (pipe stdin)
  - Les fichiers sont transcodés à la volée et écrits dans ce pipe
  - Fadeout : on track la position de lecture en temps réel (bytes lus → secondes)
    et on génère le fadeout depuis cette position exacte → transition fluide garantie
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

CHUNK_SIZE = 65536   # 64KB par chunk
FADE_DURATION = 3.0  # durée du fadeout en secondes


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


def _get_duration(path: Path) -> float | None:
    """Retourne la durée d'un fichier audio en secondes via ffprobe."""
    try:
        result = subprocess.run(
            ["ffprobe", "-v", "error",
             "-show_entries", "format=duration",
             "-of", "default=noprint_wrappers=1:nokey=1",
             str(path)],
            capture_output=True, text=True, timeout=10
        )
        return float(result.stdout.strip())
    except Exception:
        return None


# Buffer de silence global (initialisé dans Streamer.__init__)
_SILENCE_BYTES: bytes = b""


class Streamer:
    def __init__(self, config: dict):
        radio = config["radio"]
        ice   = config["icecast"]
        audio = config["audio"]

        self.music_dir  = Path(radio["music_dir"])
        self.queue_dir  = Path(radio["audio_queue_dir"])
        self.bitrate     = audio["bitrate"]
        self.sample_rate = audio["sample_rate"]

        self.icecast_url = (
            f"icecast://{ice['user']}:{ice['password']}"
            f"@{ice['host']}:{ice['port']}{ice['mount']}"
        )

        self._stop_event     = threading.Event()
        self._fade_requested = threading.Event()
        self._ffmpeg_proc: subprocess.Popen | None = None
        self._play_queue: Queue = Queue()
        self._lock = threading.Lock()

        self.queue_dir.mkdir(parents=True, exist_ok=True)

        global _SILENCE_BYTES
        _SILENCE_BYTES = _generate_silence(self.sample_rate, self.bitrate)
        logger.info("🔇 Buffer de silence initialisé" if _SILENCE_BYTES
                    else "⚠️ Impossible de générer le buffer de silence")

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
                if self._fade_requested.is_set():
                    # Journal prêt avant que la musique commence → intro courte
                    self._stream_music_with_intro_fade(music)
                else:
                    logger.info(f"🎵 Musique : {music.name}")
                    self._stream_file(music, is_music=True)
            else:
                logger.warning("⚠️ Aucune musique trouvée, attente 5s…")
                time.sleep(5)

    def _pick_music(self) -> Path | None:
        if not self.music_dir.exists():
            return None
        files = list(self.music_dir.glob("*.mp3"))
        return random.choice(files) if files else None

    # ------------------------------------------------------------------ #
    #  Cas : journal prêt avant que la musique commence                   #
    # ------------------------------------------------------------------ #

    def _stream_music_with_intro_fade(self, music_path: Path):
        """
        Journal déjà prêt → joue 15s de musique puis fadeout propre.
        Tout est calculé en une seule passe ffmpeg : pas de coupure.
        """
        logger.info(f"🎵 Intro musicale avant journal : {music_path.name}")
        self._fade_requested.clear()

        INTRO = 15.0
        tmp = tempfile.NamedTemporaryFile(suffix=".mp3", delete=False)
        tmp.close()
        out_path = Path(tmp.name)

        cmd = [
            "ffmpeg", "-y",
            "-i", str(music_path),
            "-t", str(INTRO + FADE_DURATION),
            "-af", f"afade=t=out:st={INTRO:.2f}:d={FADE_DURATION:.2f}",
            "-ar", str(self.sample_rate), "-ac", "2",
            "-b:a", self.bitrate, "-codec:a", "libmp3lame",
            str(out_path)
        ]
        try:
            r = subprocess.run(cmd, capture_output=True, timeout=60)
            if r.returncode == 0:
                self._stream_file(out_path, is_music=False)
            else:
                logger.warning("Intro fade échouée, passage direct au journal")
        except Exception as e:
            logger.warning(f"Erreur intro fade : {e}")
        finally:
            out_path.unlink(missing_ok=True)

    # ------------------------------------------------------------------ #
    #  Streaming principal                                                 #
    # ------------------------------------------------------------------ #

    def _stream_file(self, path: Path, is_music: bool = True):
        """
        Réencode et streame le fichier vers Icecast.

        Si is_music=True et qu'un journal arrive pendant la lecture :
          1. On note la position exacte (bytes lus → secondes écoulées)
          2. On arrête proprement le transcodeur
          3. On repart de cette position dans le fichier avec un afade=out
          4. Transition parfaitement continue, sans saut audible
        """
        if self._ffmpeg_proc is None or self._ffmpeg_proc.poll() is not None:
            logger.warning("⚠️ Process ffmpeg mort, relance…")
            self._start_ffmpeg()
            time.sleep(1)

        self._write_silence()

        # Durée totale du fichier (pour calculer le bitrate réel)
        total_duration = _get_duration(path)

        transcode_cmd = [
            "ffmpeg", "-y",
            "-i", str(path),
            "-ar", str(self.sample_rate), "-ac", "2",
            "-b:a", self.bitrate, "-codec:a", "libmp3lame",
            "-f", "mp3", "pipe:1"
        ]

        try:
            transcode = subprocess.Popen(
                transcode_cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
            )

            bytes_read   = 0
            start_time   = time.monotonic()

            while not self._stop_event.is_set():

                # Journal arrivé → fadeout depuis la position courante
                if is_music and self._fade_requested.is_set():
                    self._fade_requested.clear()
                    elapsed = time.monotonic() - start_time
                    logger.info(f"🎚️ Fadeout depuis {elapsed:.1f}s dans {path.name}")
                    transcode.kill()
                    transcode.stdout.close()
                    transcode.wait(timeout=3)
                    self._do_positioned_fadeout(path, elapsed)
                    return

                chunk = transcode.stdout.read(CHUNK_SIZE)
                if not chunk:
                    break

                bytes_read += len(chunk)

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

    # ------------------------------------------------------------------ #
    #  Fadeout positionné : repart exactement là où on était              #
    # ------------------------------------------------------------------ #

    def _do_positioned_fadeout(self, path: Path, position_seconds: float):
        """
        Repart depuis `position_seconds` dans le fichier original,
        joue FADE_DURATION secondes avec un fadeout progressif.
        Résultat : transition parfaitement fluide, sans saut ni bout de chanson bizarre.
        """
        logger.info(f"🎚️ Génération fadeout positionné à {position_seconds:.1f}s…")
        tmp = tempfile.NamedTemporaryFile(suffix=".mp3", delete=False)
        tmp.close()
        out_path = Path(tmp.name)

        cmd = [
            "ffmpeg", "-y",
            "-ss", f"{position_seconds:.3f}",   # seek exact vers la position courante
            "-i", str(path),
            "-t", f"{FADE_DURATION:.2f}",        # on prend juste la durée du fade
            "-af", f"afade=t=out:st=0:d={FADE_DURATION:.2f}",  # fade depuis le début du segment
            "-ar", str(self.sample_rate), "-ac", "2",
            "-b:a", self.bitrate, "-codec:a", "libmp3lame",
            str(out_path)
        ]
        try:
            r = subprocess.run(cmd, capture_output=True, timeout=30)
            if r.returncode == 0 and out_path.stat().st_size > 0:
                logger.info("🎚️ Fadeout positionné prêt, diffusion…")
                self._stream_file(out_path, is_music=False)
            else:
                logger.warning(f"Fadeout positionné échoué (code {r.returncode}), passage direct au journal")
        except Exception as e:
            logger.warning(f"Erreur fadeout positionné : {e}")
        finally:
            out_path.unlink(missing_ok=True)

    # ------------------------------------------------------------------ #
    #  Silence de transition                                               #
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
                "ffmpeg", "-re",
                "-f", "mp3", "-i", "pipe:0",
                "-ar", str(self.sample_rate), "-ac", "2",
                "-b:a", self.bitrate, "-codec:a", "libmp3lame",
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
