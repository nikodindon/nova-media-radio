"""
journal_builder.py
Génère un journal audio complet :
  1. Charge les messages depuis config/messages.yaml
  2. Construit le script texte (intro + news + transitions + outro)
  3. Synthèse vocale via edge-tts (voix aléatoire)
  4. Mixage avec musique de fond via ffmpeg
  5. Dépose le fichier final dans audio_queue/

Pour personnaliser les textes (intros, outros, transitions),
éditez uniquement config/messages.yaml — pas besoin de toucher au code.
"""

import asyncio
import logging
import random
import subprocess
import threading
from datetime import datetime
from pathlib import Path

import edge_tts
import yaml

logger = logging.getLogger("nova.journal")

# Jours et mois en français
JOURS_FR = ["lundi", "mardi", "mercredi", "jeudi", "vendredi", "samedi", "dimanche"]
MOIS_FR  = [
    "janvier", "février", "mars", "avril", "mai", "juin",
    "juillet", "août", "septembre", "octobre", "novembre", "décembre"
]

# Chemin par défaut du fichier de messages
MESSAGES_FILE = Path("config/messages.yaml")

# Fallbacks si le fichier est absent ou corrompu
_DEFAULT_INTROS = [
    "Bonjour, il est {heure}, vous écoutez Nova Media. Voici les titres du jour de ce {date}.",
]
_DEFAULT_TRANSITIONS = ["Par ailleurs,", "Dans un autre registre,", "Autre information,"]
_DEFAULT_OUTROS = [
    "C'est tout pour ce journal. Place à la musique.",
]


def _load_messages() -> tuple[list, list, list]:
    """
    Charge intros, transitions et outros depuis config/messages.yaml.
    Retourne les listes ou les fallbacks en cas d'erreur.
    """
    try:
        with open(MESSAGES_FILE, "r", encoding="utf-8") as f:
            data = yaml.safe_load(f)
        intros      = data.get("intros",      _DEFAULT_INTROS)
        transitions = data.get("transitions", _DEFAULT_TRANSITIONS)
        outros      = data.get("outros",      _DEFAULT_OUTROS)
        logger.debug(f"Messages chargés : {len(intros)} intros, {len(transitions)} transitions, {len(outros)} outros")
        return intros, transitions, outros
    except FileNotFoundError:
        logger.warning(f"⚠️ {MESSAGES_FILE} introuvable, utilisation des textes par défaut")
        return _DEFAULT_INTROS, _DEFAULT_TRANSITIONS, _DEFAULT_OUTROS
    except Exception as e:
        logger.error(f"Erreur chargement messages.yaml : {e} — utilisation des textes par défaut")
        return _DEFAULT_INTROS, _DEFAULT_TRANSITIONS, _DEFAULT_OUTROS


def verify_messages_file():
    """
    Vérifie et valide le fichier messages.yaml au démarrage.
    Affiche un rapport détaillé dans les logs.
    Appelé une seule fois depuis main.py.
    """
    logger.info(f"📋 Vérification de {MESSAGES_FILE}…")

    # Existence du fichier
    if not MESSAGES_FILE.exists():
        logger.error(
            f"❌ {MESSAGES_FILE} introuvable !\n"
            f"   → Créez le fichier ou copiez le modèle depuis la documentation.\n"
            f"   → La radio utilisera des phrases par défaut en attendant."
        )
        return

    # Lecture brute
    try:
        with open(MESSAGES_FILE, "r", encoding="utf-8") as f:
            raw = f.read()
    except PermissionError:
        logger.error(f"❌ Impossible de lire {MESSAGES_FILE} : permission refusée.")
        return
    except UnicodeDecodeError as e:
        logger.error(
            f"❌ Erreur d'encodage dans {MESSAGES_FILE} : {e}\n"
            f"   → Assurez-vous que le fichier est encodé en UTF-8."
        )
        return

    # Parsing YAML
    try:
        data = yaml.safe_load(raw)
    except yaml.YAMLError as e:
        # Extraire la position de l'erreur si disponible
        if hasattr(e, "problem_mark"):
            mark = e.problem_mark
            line   = mark.line + 1
            col    = mark.column + 1
            logger.error(
                f"❌ Erreur YAML dans {MESSAGES_FILE} ligne {line}, colonne {col} :\n"
                f"   {e.problem}\n"
                f"   → Vérifiez l'indentation et les guillemets autour de cette ligne."
            )
        else:
            logger.error(f"❌ Erreur YAML dans {MESSAGES_FILE} : {e}")
        logger.warning("⚠️ La radio utilisera les phrases par défaut.")
        return

    if not isinstance(data, dict):
        logger.error(
            f"❌ {MESSAGES_FILE} ne contient pas un dictionnaire YAML valide.\n"
            f"   → Vérifiez la structure du fichier (sections intros:, transitions:, outros:)."
        )
        return

    # Vérification des sections
    ok = True
    for section in ["intros", "transitions", "outros"]:
        if section not in data:
            logger.warning(f"⚠️ Section '{section}' absente de {MESSAGES_FILE} — valeur par défaut utilisée.")
            ok = False
        elif not isinstance(data[section], list):
            logger.error(f"❌ Section '{section}' n'est pas une liste dans {MESSAGES_FILE}.")
            ok = False
        elif len(data[section]) == 0:
            logger.warning(f"⚠️ Section '{section}' est vide dans {MESSAGES_FILE} — valeur par défaut utilisée.")
            ok = False
        else:
            count = len(data[section])
            # Vérifier que toutes les entrées sont des strings
            non_strings = [i+1 for i, v in enumerate(data[section]) if not isinstance(v, str)]
            if non_strings:
                logger.error(
                    f"❌ Section '{section}' : entrées non-textuelles aux lignes {non_strings}.\n"
                    f"   → Chaque entrée doit être une chaîne de caractères."
                )
                ok = False
            else:
                logger.info(f"   ✅ {section:<12} : {count} phrase{'s' if count > 1 else ''} chargée{'s' if count > 1 else ''}")

    # Vérification des variables {heure} et {date} dans les intros
    if "intros" in data and isinstance(data["intros"], list):
        for i, intro in enumerate(data["intros"]):
            if isinstance(intro, str):
                manquantes = []
                if "{heure}" not in intro:
                    manquantes.append("{heure}")
                if "{date}" not in intro:
                    manquantes.append("{date}")
                if manquantes:
                    logger.warning(
                        f"⚠️ Intro #{i+1} : variable(s) manquante(s) {manquantes}\n"
                        f"   → \"{intro[:60]}{'...' if len(intro) > 60 else ''}\""
                    )

    if ok:
        logger.info(f"✅ {MESSAGES_FILE} chargé avec succès.")
    else:
        logger.warning(f"⚠️ {MESSAGES_FILE} chargé avec des avertissements — voir ci-dessus.")


def _format_date_fr(dt: datetime) -> str:
    jour = JOURS_FR[dt.weekday()]
    return f"{jour} {dt.day} {MOIS_FR[dt.month - 1]} {dt.year}"


def _format_heure(dt: datetime) -> str:
    return f"{dt.hour}h{dt.minute:02d}"


def _is_valid_summary(summary: str) -> bool:
    """
    Retourne True si le summary est utilisable à l'antenne.
    Rejette : vide, trop court, ou commençant par [Contenu inaccessible].
    """
    if not summary:
        return False
    if summary.startswith("[Contenu inaccessible]"):
        return False
    if len(summary) < 20:   # trop court pour être un vrai résumé
        return False
    return True


def _build_script(articles: list) -> str:
    """
    Assemble le texte complet du journal.
    Les messages sont rechargés depuis messages.yaml à chaque appel,
    ce qui permet de modifier le fichier sans redémarrer la radio.
    """
    intros, transitions, outros = _load_messages()

    now       = datetime.now()
    date_str  = _format_date_fr(now)
    heure_str = _format_heure(now)

    intro = random.choice(intros).format(heure=heure_str, date=date_str)
    outro = random.choice(outros)

    parts = [intro]
    skipped = 0
    news_index = 0

    for article in articles:
        summary = article.get("summary", "").strip()
        if not _is_valid_summary(summary):
            skipped += 1
            logger.debug(f"⏭️ Summary ignoré dans le script : '{summary[:50]}…'" if summary else "⏭️ Summary vide ignoré dans le script")
            continue
        if news_index > 0:
            transition = random.choice(transitions)
            parts.append(f"{transition} {summary}")
        else:
            parts.append(summary)
        news_index += 1

    if skipped > 0:
        logger.info(f"ℹ️ {skipped} article(s) ignoré(s) dans le script (summary vide ou inaccessible) — {news_index} news diffusées")

    if news_index == 0:
        logger.warning("⚠️ Aucun summary valide dans ce batch — journal vide (intro + outro seulement)")

    parts.append(outro)
    return "\n\n".join(parts)


# ------------------------------------------------------------------ #
#  Classe principale                                                   #
# ------------------------------------------------------------------ #

class JournalBuilder:
    def __init__(self, config: dict):
        self.config    = config
        self.voices    = config["tts"]["voices"]
        self.bg_dir    = Path(config["radio"]["background_music_dir"])
        self.queue_dir = Path(config["radio"]["audio_queue_dir"])
        self.tmp_dir   = Path(config["radio"]["tmp_dir"])
        self.bg_volume = config["radio"]["background_volume"]
        self.bitrate   = config["audio"]["bitrate"]
        self.sample_rate = config["audio"]["sample_rate"]

        self.queue_dir.mkdir(parents=True, exist_ok=True)
        self.tmp_dir.mkdir(parents=True, exist_ok=True)

    # ------------------------------------------------------------------ #
    #  Point d'entrée public                                              #
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
        threading.Thread(target=_run, daemon=True).start()

    # ------------------------------------------------------------------ #
    #  Synthèse vocale (edge-tts)                                         #
    # ------------------------------------------------------------------ #

    def _synthesize(self, text: str, voice: str) -> Path | None:
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        out_path  = self.tmp_dir / f"voice_{timestamp}.mp3"
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
        timestamp  = datetime.now().strftime("%Y%m%d_%H%M%S")
        final_path = self.queue_dir / f"journal_{timestamp}.mp3"
        bg         = self._pick_background()

        if bg:
            cmd = [
                "ffmpeg", "-y",
                "-i", str(voice_path),
                "-stream_loop", "-1", "-i", str(bg),
                "-filter_complex",
                (
                    f"[0:a]volume=2.0[voice];"
                    f"[1:a]volume={self.bg_volume}[bg];"
                    f"[voice][bg]amix=inputs=2:duration=first:dropout_transition=2:normalize=0[out]"
                ),
                "-map", "[out]",
                "-ar", str(self.sample_rate),
                "-ac", "2",
                "-b:a", self.bitrate,
                "-codec:a", "libmp3lame",
                str(final_path)
            ]
        else:
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
            voice_path.unlink(missing_ok=True)
            return final_path
        except subprocess.TimeoutExpired:
            logger.error("ffmpeg timeout lors du mixage")
            return None
        except Exception as e:
            logger.error(f"Erreur ffmpeg : {e}")
            return None
