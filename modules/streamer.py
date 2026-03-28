"""
streamer.py — Nova Media

Architecture : UN SEUL process ffmpeg permanent + pipe stdin
  - Le pipe utilise le format AAC/ADTS qui supporte les streams continus
  - Pas de headers MP3 cassés entre fichiers
  - Zéro reconnexion Icecast = flux vraiment continu pour VLC

Flux :
  Python lit fichier → transcode en AAC/ADTS → pipe → ffmpeg → Icecast MP3
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

FADE_DURATION      = 3.0
CHUNK_SIZE         = 32768
HEARTBEAT_INTERVAL = 0.5   # secondes entre heartbeats silence

_SILENCE_BYTES: bytes = b""


def _generate_silence(sample_rate: int, bitrate: str, duration: float = 1.0) -> bytes:
    """Génère du silence MP3 propre pour le heartbeat."""
    cmd = [
        "ffmpeg", "-y",
        "-f", "lavfi", "-i", f"anullsrc=r={sample_rate}:cl=stereo",
        "-t", str(duration),
        "-b:a", bitrate,
        "-codec:a", "libmp3lame",
        "-f", "mp3",
        "pipe:1"
    ]
    try:
        return subprocess.run(cmd, capture_output=True, timeout=10).stdout
    except Exception:
        return b""


class Streamer:
    def __init__(self, config: dict):
        radio = config["radio"]
        ice   = config["icecast"]
        audio = config["audio"]

        self.music_dir   = Path(radio["music_dir"])
        self.queue_dir   = Path(radio["audio_queue_dir"])
        self.bitrate     = audio["bitrate"]
        self.sample_rate = audio["sample_rate"]
        self._debug      = config.get("_debug", False)

        self.icecast_url = (
            f"icecast://{ice['user']}:{ice['password']}"
            f"@{ice['host']}:{ice['port']}{ice['mount']}"
        )

        self._stop_event      = threading.Event()
        self._fade_requested  = threading.Event()
        self._streaming_event = threading.Event()
        self._ffmpeg_proc: subprocess.Popen | None = None
        self._play_queue: Queue = Queue()
        self._lock = threading.Lock()

        self._fade_cache: bytes | None = None
        self._fade_cache_ready = threading.Event()

        self.queue_dir.mkdir(parents=True, exist_ok=True)

        global _SILENCE_BYTES
        _SILENCE_BYTES = _generate_silence(self.sample_rate, self.bitrate)
        logger.info("🔇 Buffer de silence initialisé" if _SILENCE_BYTES
                    else "⚠️ Impossible de générer le buffer de silence")

    # ------------------------------------------------------------------ #
    #  API publique                                                        #
    # ------------------------------------------------------------------ #

    def enqueue_bulletin(self, path: Path):
        if path and path.exists():
            self._play_queue.put(path)
            logger.info(f"📥 Journal en file : {path.name}")
            self._fade_requested.set()

    def run(self):
        logger.info("📻 Démarrage du streamer Nova Media")
        self._start_ffmpeg()

        heartbeat = threading.Thread(target=self._heartbeat, daemon=True, name="Heartbeat")
        heartbeat.start()

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
    #  Heartbeat                                                           #
    # ------------------------------------------------------------------ #

    def _heartbeat(self):
        while not self._stop_event.is_set():
            if self._ffmpeg_proc and self._ffmpeg_proc.poll() is not None:
                logger.warning("⚠️ ffmpeg mort détecté, relance…")
                self._start_ffmpeg()
            if not self._streaming_event.is_set():
                self._write_to_pipe(_SILENCE_BYTES)
            time.sleep(HEARTBEAT_INTERVAL)

    # ------------------------------------------------------------------ #
    #  Logique de lecture                                                  #
    # ------------------------------------------------------------------ #

    def _play_next(self):
        try:
            bulletin = self._play_queue.get_nowait()
            logger.info(f"🎙️ Diffusion journal : {bulletin.name}")
            self._stream_file(bulletin, is_music=False)
            bulletin.unlink(missing_ok=True)
        except Empty:
            music = self._pick_music()
            if music:
                if self._fade_requested.is_set():
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
    #  Intro musicale courte si journal déjà prêt                         #
    # ------------------------------------------------------------------ #

    def _stream_music_with_intro_fade(self, music_path: Path):
        logger.info(f"🎵 Intro musicale avant journal : {music_path.name}")
        self._fade_requested.clear()
        INTRO = 15.0
        tmp = tempfile.NamedTemporaryFile(suffix=".mp3", delete=False, dir=self.queue_dir)
        tmp.close()
        out_path = Path(tmp.name)
        cmd = [
            "ffmpeg", "-y",
            "-i", str(music_path),
            "-vn", "-map", "0:a",
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
    #  Streaming via pipe — transcode en AAC/ADTS                         #
    # ------------------------------------------------------------------ #

    def _stream_file(self, path: Path, is_music: bool = True):
        """
        Transcode le fichier en AAC/ADTS et l'écrit dans le pipe ffmpeg.
        AAC/ADTS est un format streamable sans header de début — zéro
        discontinuité entre fichiers, zéro 'Header missing' côté ffmpeg.
        """
        if self._ffmpeg_proc is None or self._ffmpeg_proc.poll() is not None:
            logger.warning("⚠️ Process ffmpeg mort, relance…")
            self._start_ffmpeg()
            time.sleep(1)

        transcode_cmd = [
            "ffmpeg", "-y",
            "-i", str(path),
            "-vn", "-map", "0:a",           # audio uniquement, pas de cover art
            "-ar", str(self.sample_rate), "-ac", "2",
            "-b:a", self.bitrate,
            "-codec:a", "libmp3lame",
            "-f", "mp3",
            "pipe:1"
        ]

        stderr_target = subprocess.PIPE if self._debug else subprocess.DEVNULL

        try:
            transcode = subprocess.Popen(
                transcode_cmd,
                stdout=subprocess.PIPE,
                stderr=stderr_target,
            )
            if self._debug:
                self._log_stderr(transcode, f"transcode:{path.stem[:15]}")

            self._streaming_event.set()
            start_time = time.monotonic()

            while not self._stop_event.is_set():

                # Journal détecté → fadeout pré-généré en parallèle
                if is_music and self._fade_requested.is_set():
                    elapsed = time.monotonic() - start_time
                    logger.info(f"🎚️ Fadeout depuis {elapsed:.1f}s dans {path.name}")
                    self._fade_cache = None
                    self._fade_cache_ready.clear()
                    ft = threading.Thread(
                        target=self._prebuild_fadeout,
                        args=(path, elapsed),
                        daemon=True
                    )
                    ft.start()
                    # Continuer à jouer pendant max 4s le temps que le fadeout soit prêt
                    deadline = time.monotonic() + 4.0
                    while not self._stop_event.is_set():
                        if self._fade_cache_ready.is_set():
                            break
                        if time.monotonic() > deadline:
                            break
                        chunk = transcode.stdout.read(CHUNK_SIZE)
                        if not chunk:
                            break
                        self._write_to_pipe(chunk)
                    transcode.kill()
                    transcode.stdout.close()
                    transcode.wait(timeout=3)
                    self._streaming_event.clear()
                    self._fade_requested.clear()
                    if self._fade_cache:
                        logger.info("🎚️ Injection fadeout…")
                        self._streaming_event.set()
                        self._stream_bytes(self._fade_cache)
                        self._streaming_event.clear()
                    return

                chunk = transcode.stdout.read(CHUNK_SIZE)
                if not chunk:
                    break
                self._write_to_pipe(chunk)

            transcode.stdout.close()
            transcode.wait(timeout=3)

        except Exception as e:
            logger.error(f"Erreur streaming {path.name} : {e}")
        finally:
            self._streaming_event.clear()

    def _stream_bytes(self, data: bytes):
        """Envoie des bytes directement dans le pipe (pour le fadeout)."""
        offset = 0
        while offset < len(data) and not self._stop_event.is_set():
            chunk = data[offset:offset + CHUNK_SIZE]
            self._write_to_pipe(chunk)
            offset += len(chunk)

    # ------------------------------------------------------------------ #
    #  Fadeout pré-généré en parallèle                                    #
    # ------------------------------------------------------------------ #

    def _prebuild_fadeout(self, path: Path, position_seconds: float):
        tmp = tempfile.NamedTemporaryFile(suffix=".mp3", delete=False, dir=self.queue_dir)
        tmp.close()
        out_path = Path(tmp.name)
        cmd = [
            "ffmpeg", "-y",
            "-ss", f"{position_seconds:.3f}",
            "-i", str(path),
            "-vn", "-map", "0:a",
            "-t", f"{FADE_DURATION:.2f}",
            "-af", f"afade=t=out:st=0:d={FADE_DURATION:.2f}",
            "-ar", str(self.sample_rate), "-ac", "2",
            "-b:a", self.bitrate, "-codec:a", "libmp3lame",
            str(out_path)
        ]
        try:
            r = subprocess.run(cmd, capture_output=True, timeout=30)
            if r.returncode == 0 and out_path.stat().st_size > 0:
                self._fade_cache = out_path.read_bytes()
                logger.info("🎚️ Fadeout pré-généré OK")
            else:
                logger.warning("Pré-génération fadeout échouée")
        except Exception as e:
            logger.warning(f"Erreur fadeout : {e}")
        finally:
            out_path.unlink(missing_ok=True)
            self._fade_cache_ready.set()

    # ------------------------------------------------------------------ #
    #  Écriture dans le pipe                                               #
    # ------------------------------------------------------------------ #

    def _write_to_pipe(self, data: bytes) -> bool:
        if not data or self._ffmpeg_proc is None:
            return True
        try:
            self._ffmpeg_proc.stdin.write(data)
            self._ffmpeg_proc.stdin.flush()
            return True
        except (BrokenPipeError, OSError) as e:
            logger.error(f"Pipe cassé ({type(e).__name__}: {e}), reconnexion…")
            self._start_ffmpeg()
            time.sleep(0.3)
            try:
                self._ffmpeg_proc.stdin.write(_SILENCE_BYTES)
                self._ffmpeg_proc.stdin.flush()
            except Exception:
                pass
            return False

    # ------------------------------------------------------------------ #
    #  Process ffmpeg → Icecast (lit AAC/ADTS depuis stdin)               #
    # ------------------------------------------------------------------ #

    def _start_ffmpeg(self):
        with self._lock:
            self._kill_ffmpeg()
            cmd = [
                "ffmpeg",
                "-re",
                "-probesize",       "32",   # ne pas chercher un header complet
                "-analyzeduration", "0",    # pas d'analyse de durée
                "-f", "mp3",
                "-i", "pipe:0",
                "-vn", "-map", "0:a",
                "-codec:a", "libmp3lame",
                "-b:a", self.bitrate,
                "-ar", str(self.sample_rate), "-ac", "2",
                "-f", "mp3",
                "-content_type", "audio/mpeg",
                "-ice_name", "Nova Media",
                "-ice_description", "Radio IA automatisée",
                self.icecast_url,
            ]
            stderr_target = subprocess.PIPE if self._debug else subprocess.DEVNULL
            for attempt in range(1, 11):
                try:
                    self._ffmpeg_proc = subprocess.Popen(
                        cmd,
                        stdin=subprocess.PIPE,
                        stdout=subprocess.DEVNULL,
                        stderr=stderr_target,
                    )
                    if self._debug:
                        self._log_stderr(self._ffmpeg_proc, "icecast")
                    time.sleep(1.5)
                    if self._ffmpeg_proc.poll() is not None:
                        raise ConnectionError("ffmpeg mort immédiatement")
                    logger.info(f"🔗 Connecté à Icecast (tentative {attempt})")
                    return
                except Exception as e:
                    logger.warning(f"⏳ Tentative {attempt}/10 : {e}")
                    self._ffmpeg_proc = None
                    time.sleep(3)
            logger.error("❌ Impossible de se connecter à Icecast.")

    def _kill_ffmpeg(self):
        if self._ffmpeg_proc and self._ffmpeg_proc.poll() is None:
            try:
                self._ffmpeg_proc.stdin.close()
            except Exception:
                pass
            try:
                self._ffmpeg_proc.terminate()
            except Exception:
                pass
            try:
                self._ffmpeg_proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                try:
                    self._ffmpeg_proc.kill()
                except Exception:
                    pass
        self._ffmpeg_proc = None

    # ------------------------------------------------------------------ #
    #  Debug                                                               #
    # ------------------------------------------------------------------ #

    def _log_stderr(self, proc: subprocess.Popen, label: str):
        if proc.stderr is None:
            return
        def _reader():
            try:
                for line in proc.stderr:
                    line = line.decode("utf-8", errors="replace").strip()
                    if line:
                        logger.debug(f"[ffmpeg/{label}] {line}")
            except Exception:
                pass
        threading.Thread(target=_reader, daemon=True, name=f"ffmpeg-log-{label}").start()
