"""
journal_builder.py
Génère un journal audio complet :
  1. Construit le script texte (intro + news + transitions + outro)
  2. Synthèse vocale via edge-tts (voix aléatoire)
  3. Mixage avec musique de fond via ffmpeg
  4. Dépose le fichier final dans audio_queue/
"""

import asyncio
import logging
import random
import subprocess
import threading
from datetime import datetime
from pathlib import Path

import edge_tts

logger = logging.getLogger("nova.journal")

# ------------------------------------------------------------------ #
#  Phrases d'intro / transitions / outro                               #
# ------------------------------------------------------------------ #

INTROS = [
    "Bonjour, il est {heure}, vous êtes sur la radio d'infos de nikodindon. Voici les titres du jour de ce {date}.",
    "nikodindon Media : le seul support d'infos géré par des dindons qui travaillent dur, il est {heure}. Bienvenue dans votre journal de {date}.",
    "Bonsoir à tous, {heure} sur nikodindon Media , la meilleure radio du maine et loire. Faisons le point sur l'actualité de ce {date}.",
    "Il est exactement {heure}, vous écoutez nikodindon Media , et vous avez bien raison. Voici votre rendez-vous info du {date}.",
    "Bonjour et bienvenue sur nikodindon Media, la meilleure radio d'infos continue de France , rien que ça. En ce {date}, il est {heure}. Voici l'essentiel de l'actualité.",
    "nikodindon media vous accueille, bienvenue sur la meilleur de l'info continue de Daumeray City. Il est {heure} ce {date}. Place aux informations.",
    "Vous êtes sur Nikodindon media votre radio de l'actualité en continu situé au coeur de Daumeray City. Il est {heure}, ce {date}.",
]

TRANSITIONS = [
    "Par ailleurs,",
    "Dans un autre registre,",
    "Autre information,",
    "On enchaîne avec",
    "Passons maintenant à",
    "À signaler également,",
    "Sur un autre sujet,",
    "Côté économie,",
    "Du côté international,",
    "On parle aussi de",
    "Retenons également que",
    "Rappelons par ailleurs que",
]

OUTROS = [
    "C'est tout pour ce journal. Nous vous retrouvons très vite avec de nouvelles informations. En attendant, place à la musique, et n'oubliez pas : vous êtes capable du meilleur comme du pire. Mais dans le pire, c’est vous le meilleur..",
    "Voilà pour ce bulletin d'information. Intéressant à se rappeler : il suffirait que les gens n’achètent pas des trucs nuls pour que ça ne se vende plus. nikodindon Media reste à vos côtés, place à la musique.",
    "C'est la fin de ce journal. Restez à l'écoute de nikodindon Media pour toute l'actualité en continu. A garder en tête : On peut rire de tout, mais pas avec tout le monde",
    "Nous vous donnons rendez-vous très prochainement pour un nouveau journal. et N'oubliez pas : Rien n’est moins sûr que l’incertain.En attendant, profitez de la musique sur Nikdindon Media.",
    "Ce journal est terminé. Merci de votre écoute. Important à se rappeler tous les jours : Le problème, ce n’est pas les cons, c’est qu’ils osent tout.nikodindon Media continue avec de la musique.",
    "C'est la fin de ce bulletin. À très bientôt sur nikodindon Media pour les prochaines informations. Ha oui au fait , Le capitalisme, c’est l’exploitation de l’homme par l’homme. Le communisme, c’est le contraire. Place à notre super musique !",
    "Voilà, vous êtes informés. nikodindon Media reprend sa programmation musicale. A ne pas oublier : Un con qui marche ira toujours plus loin qu’un intellectuel assis.",
]

# Jours et mois en français
JOURS_FR = ["lundi", "mardi", "mercredi", "jeudi", "vendredi", "samedi", "dimanche"]
MOIS_FR = [
    "janvier", "février", "mars", "avril", "mai", "juin",
    "juillet", "août", "septembre", "octobre", "novembre", "décembre"
]


def _format_date_fr(dt: datetime) -> str:
    jour = JOURS_FR[dt.weekday()]
    return f"{jour} {dt.day} {MOIS_FR[dt.month - 1]} {dt.year}"


def _format_heure(dt: datetime) -> str:
    return f"{dt.hour}h{dt.minute:02d}"


def _build_script(articles: list) -> str:
    """Assemble le texte complet du journal."""
    now = datetime.now()
    date_str = _format_date_fr(now)
    heure_str = _format_heure(now)

    intro = random.choice(INTROS).format(heure=heure_str, date=date_str)
    outro = random.choice(OUTROS)

    parts = [intro]
    for i, article in enumerate(articles):
        summary = article.get("summary", "").strip()
        if not summary:
            continue
        if i > 0:
            transition = random.choice(TRANSITIONS)
            parts.append(f"{transition} {summary}")
        else:
            parts.append(summary)
    parts.append(outro)

    return "\n\n".join(parts)


# ------------------------------------------------------------------ #
#  Classe principale                                                   #
# ------------------------------------------------------------------ #

class JournalBuilder:
    def __init__(self, config: dict):
        self.config = config
        self.voices: list = config["tts"]["voices"]
        self.bg_dir = Path(config["radio"]["background_music_dir"])
        self.queue_dir = Path(config["radio"]["audio_queue_dir"])
        self.tmp_dir = Path(config["radio"]["tmp_dir"])
        self.bg_volume = config["radio"]["background_volume"]
        self.bitrate = config["audio"]["bitrate"]
        self.sample_rate = config["audio"]["sample_rate"]

        self.queue_dir.mkdir(parents=True, exist_ok=True)
        self.tmp_dir.mkdir(parents=True, exist_ok=True)

    # ------------------------------------------------------------------ #
    #  Point d'entrée public (appelé depuis un thread)                    #
    # ------------------------------------------------------------------ #

    def build(self, articles: list) -> Path | None:
        """Génère le journal et retourne le chemin du MP3 final."""
        try:
            script = _build_script(articles)
            logger.info("📝 Script construit")

            voice = random.choice(self.voices)
            logger.info(f"🎤 Voix sélectionnée : {voice}")

            voice_path = self._synthesize(script, voice)
            if voice_path is None:
                return None

            final_path = self._mix(voice_path)
            if final_path:
                logger.info(f"✅ Journal prêt : {final_path}")
            return final_path

        except Exception as e:
            logger.error(f"Erreur génération journal : {e}")
            return None

    def build_async(self, articles: list, callback=None):
        """Lance la génération dans un thread dédié."""
        def _run():
            path = self.build(articles)
            if callback:
                callback(path)
        t = threading.Thread(target=_run, daemon=True)
        t.start()

    # ------------------------------------------------------------------ #
    #  Synthèse vocale (edge-tts)                                         #
    # ------------------------------------------------------------------ #

    def _synthesize(self, text: str, voice: str) -> Path | None:
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        out_path = self.tmp_dir / f"voice_{timestamp}.mp3"
        try:
            asyncio.run(self._tts(text, voice, str(out_path)))
            logger.info(f"🔊 TTS généré : {out_path}")
            return out_path
        except Exception as e:
            logger.error(f"Erreur TTS : {e}")
            return None

    @staticmethod
    async def _tts(text: str, voice: str, output: str):
        communicate = edge_tts.Communicate(text, voice)
        await communicate.save(output)

    # ------------------------------------------------------------------ #
    #  Mixage voix + musique de fond (ffmpeg)                             #
    # ------------------------------------------------------------------ #

    def _pick_background(self) -> Path | None:
        if not self.bg_dir.exists():
            return None
        files = list(self.bg_dir.glob("*.mp3"))
        return random.choice(files) if files else None

    def _mix(self, voice_path: Path) -> Path | None:
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        final_path = self.queue_dir / f"journal_{timestamp}.mp3"

        bg = self._pick_background()

        if bg:
            # Mixage voix (volume normal) + musique de fond (volume réduit, en boucle)
            cmd = [
                "ffmpeg", "-y",
                "-i", str(voice_path),
                "-stream_loop", "-1", "-i", str(bg),
                "-filter_complex",
                (
                    f"[0:a]volume=1.0[voice];"
                    f"[1:a]volume={self.bg_volume}[bg];"
                    f"[voice][bg]amix=inputs=2:duration=first:dropout_transition=2[out]"
                ),
                "-map", "[out]",
                "-ar", str(self.sample_rate),
                "-ac", "2",
                "-b:a", self.bitrate,
                "-codec:a", "libmp3lame",
                str(final_path)
            ]
        else:
            # Pas de musique de fond : simple réencodage
            logger.warning("⚠️ Aucune musique de fond trouvée, journal sans fond musical")
            cmd = [
                "ffmpeg", "-y",
                "-i", str(voice_path),
                "-ar", str(self.sample_rate),
                "-ac", "2",
                "-b:a", self.bitrate,
                "-codec:a", "libmp3lame",
                str(final_path)
            ]

        try:
            result = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
            if result.returncode != 0:
                logger.error(f"ffmpeg error : {result.stderr[-500:]}")
                return None
            # Nettoyage fichier TTS temporaire
            voice_path.unlink(missing_ok=True)
            return final_path
        except subprocess.TimeoutExpired:
            logger.error("ffmpeg timeout lors du mixage")
            return None
        except Exception as e:
            logger.error(f"Erreur ffmpeg : {e}")
            return None
