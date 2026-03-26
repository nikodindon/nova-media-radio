# 🎙️ Nova Media — Radio IA automatisée

Radio autonome pilotée par des données JSON + TTS + Icecast.

---

## 📁 Structure du projet

```
nova_media/
├── main.py                      ← Point d'entrée
├── requirements.txt
├── docker-compose.yml           ← Icecast
├── icecast.xml                  ← Config Icecast
├── config/
│   └── config.yaml              ← Toute la configuration
├── modules/
│   ├── news_watcher.py          ← Surveillance JSON
│   ├── journal_builder.py       ← TTS + mixage audio
│   └── streamer.py              ← Flux Icecast via ffmpeg
├── data/
│   ├── articles/                ← Vos JSON de news (YYYYMMDD_articles.json)
│   └── processed_hashes.json   ← Hashes des news déjà traitées
├── music/                       ← Vos MP3 de musique principale
├── background_music/            ← Vos MP3 de fond pour les journaux
├── audio_queue/                 ← Journaux générés (auto)
└── tmp/                         ← Fichiers TTS temporaires (auto)
```

---

## 🚀 Installation

### 🐧 Linux (Ubuntu / Debian)

#### 1. Prérequis système

```bash
sudo apt update && sudo apt install ffmpeg docker-compose python3-pip -y
```

#### 2. Dépendances Python

```bash
pip install -r requirements.txt
```

#### 3. Lancer Icecast

```bash
docker-compose up -d
```

Vérifier qu'Icecast tourne : http://localhost:8000

#### 4. Ajouter vos fichiers audio

```
music/            → posez vos MP3 de musique ici (≥ 1 fichier requis)
background_music/ → posez vos MP3 de fond ici (optionnel mais recommandé)
```

#### 5. Lancer Nova Media

```bash
python main.py
```

---

### 🪟 Windows 11

#### 1. Installer Python 3.10+

Téléchargez Python sur https://www.python.org/downloads/windows/

> ⚠️ Cochez bien **"Add Python to PATH"** lors de l'installation.

#### 2. Installer ffmpeg

1. Téléchargez la version Windows sur https://ffmpeg.org/download.html  
   (choisir un build *essentials* ou *full* depuis gyan.dev ou BtbN)
2. Extrayez l'archive, par exemple dans `C:\ffmpeg\`
3. Ajoutez `C:\ffmpeg\bin` à votre **PATH système** :
   - Rechercher "Variables d'environnement" dans le menu Démarrer
   - Éditer la variable `Path` de l'utilisateur ou du système
   - Ajouter `C:\ffmpeg\bin`
4. **Redémarrez votre terminal** puis vérifiez : `ffmpeg -version`

#### 3. Installer Docker Desktop (pour Icecast)

Téléchargez Docker Desktop sur https://www.docker.com/products/docker-desktop/

> Après installation, lancez Docker Desktop et attendez qu'il soit bien démarré (icône verte dans la barre des tâches).

**Alternative sans Docker** : téléchargez le binaire Icecast natif Windows sur https://www.icecast.org/download/ et remplacez le fichier `icecast.xml` par celui du projet.

#### 4. Dépendances Python

Ouvrez un terminal (PowerShell ou cmd) dans le dossier du projet :

```powershell
pip install -r requirements.txt
```

#### 5. Lancer Icecast

```powershell
docker-compose up -d
```

Vérifier qu'Icecast tourne : http://localhost:8000

#### 6. Ajouter vos fichiers audio

```
music\            → posez vos MP3 de musique ici (≥ 1 fichier requis)
background_music\ → posez vos MP3 de fond ici (optionnel mais recommandé)
```

#### 7. Lancer Nova Media

```powershell
python main.py
```

Pour arrêter : **Ctrl+C** dans le terminal (SIGTERM n'est pas utilisé sous Windows, Ctrl+C suffit).

---

## 📻 Écouter la radio

- **VLC** : Media → Ouvrir un flux réseau → `http://localhost:8000/nova`
- **Navigateur** : http://localhost:8000/nova
- **ffplay** : `ffplay http://localhost:8000/nova`

---

## ⚙️ Configuration (`config/config.yaml`)

| Clé | Description | Défaut |
|-----|-------------|--------|
| `icecast.host` | Adresse du serveur Icecast | `localhost` |
| `icecast.port` | Port Icecast | `8000` |
| `icecast.password` | Mot de passe source | `hackme` |
| `icecast.mount` | Point de montage | `/nova` |
| `radio.news_per_bulletin` | News requises pour déclencher un journal | `5` |
| `radio.news_interval_seconds` | Fréquence de vérification du JSON | `2` |
| `radio.background_volume` | Volume musique de fond (0.0–1.0) | `0.30` |
| `tts.voices` | Liste des voix edge-tts | Voir yaml |

### Voix disponibles

Pour lister toutes les voix disponibles :

```bash
edge-tts --list-voices | grep fr-
```

---

## 📰 Format du JSON de news

Fichier : `data/articles/YYYYMMDD_articles.json`

```json
[
  {
    "hash": "e6d5fef6504a0a908253ea3eb9b818c0",
    "timestamp": "2026-03-24T00:17:07.662666",
    "category": "crypto",
    "title": "Titre de l'article",
    "link": "https://...",
    "source": "source.com",
    "pub_date": "Mon, 23 Mar 2026 22:26:40 +0000",
    "summary": "Résumé de l'article en français qui sera lu à l'antenne."
  }
]
```

Seul le champ `summary` est utilisé pour la lecture à l'antenne.

---

## 🧵 Architecture des threads

```
main.py
  ├── Thread 1 : NewsWatcher    → surveille YYYYMMDD_articles.json toutes les 2s
  ├── Thread 2 : Streamer       → flux continu vers Icecast (1 seul ffmpeg pipe)
  └── Thread 3 : BulletinGen    → généré ponctuellement quand 5 news sont prêtes
                                   (TTS edge-tts + mixage ffmpeg → audio_queue/)
```

---

## 🔧 Dépannage

**VLC ne se connecte pas**
→ Vérifier qu'Icecast tourne : `docker-compose ps`
→ Vérifier que ffmpeg est connecté dans les logs

**Pas de son / silence**
→ Vérifier qu'il y a des MP3 dans `music/`

**Erreur TTS**
→ edge-tts nécessite une connexion internet (API Microsoft)

**Changer le mot de passe Icecast**
→ Modifier dans `icecast.xml` ET `config/config.yaml`, puis redémarrer Docker

### 🪟 Dépannage spécifique Windows

**Utiliser des chemins absolus Windows dans `config.yaml`**
→ Le backslash `\` est un caractère spécial en YAML. Utilisez toujours des slashes `/` à la place :
```yaml
# ✅ Correct
data_dir: "c:/audio/articles"
music_dir: "c:/audio/music"

# ❌ À éviter — le \a et \m seront mal interprétés
data_dir: "c:\audio\articles"
```
Les slashes `/` sont parfaitement acceptés par Windows et Python, et évitent tout problème d'échappement.

**`ffmpeg` introuvable dans le terminal**
→ Vérifier que `C:\ffmpeg\bin` est bien dans le PATH et que le terminal a été redémarré après modification
→ Tester avec : `where ffmpeg`

**`docker-compose` introuvable**
→ Avec Docker Desktop, la commande est `docker compose` (sans tiret) :
```powershell
docker compose up -d
```

**Erreur d'encodage dans les logs (caractères spéciaux)**
→ Forcer l'UTF-8 dans PowerShell avant de lancer :
```powershell
$OutputEncoding = [System.Text.Encoding]::UTF8
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8
python main.py
```

**Emoji non affichés dans le terminal Windows**
→ Utiliser Windows Terminal (disponible sur le Microsoft Store) plutôt que l'invite de commandes classique

---

## 🛠️ Développement futur

- [ ] Intégration Claude API pour reformulation des summaries
- [ ] Interface web de monitoring
- [ ] Gestion multi-flux (plusieurs mountpoints)
- [ ] Jingles et sons d'habillage
- [ ] Générateur de JSON de news automatique (RSS → résumé IA)
