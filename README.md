# 🎙️ Nova Media — AI-Powered Automated Radio

> An autonomous AI radio station driven by live news data, Text-to-Speech synthesis, and Icecast streaming.

Music plays continuously. When enough new articles are detected, an AI-voiced news bulletin is automatically generated and seamlessly inserted into the live stream — with a smooth fadeout of the current track.

---

## ✨ Features

- 🎵 **Continuous music streaming** via a single stable ffmpeg pipe to Icecast
- 📰 **Real-time news monitoring** — watches a JSON file for incoming articles every 2 seconds
- 🎙️ **Automatic bulletin generation** — assembles a script, synthesizes speech with edge-tts, mixes with background music
- 🎚️ **Smooth fadeout** — music fades out cleanly when a bulletin is ready, no abrupt cuts
- 🔀 **Random voices** — picks a different TTS voice for each bulletin
- 🌐 **Cross-platform** — works on Linux and Windows 11
- ⚙️ **Fully configurable** via a single YAML file

---

## 📁 Project Structure

```
nova_media/
├── main.py                      ← Entry point — orchestrates all threads
├── requirements.txt
├── docker-compose.yml           ← Icecast server (Docker)
├── icecast.xml                  ← Icecast configuration
├── config/
│   └── config.yaml              ← All settings (Icecast, TTS, audio, paths)
├── modules/
│   ├── news_watcher.py          ← JSON file watcher
│   ├── journal_builder.py       ← Script builder + TTS + audio mixing
│   └── streamer.py              ← Icecast live stream via ffmpeg pipe
├── data/
│   ├── articles/                ← Your news JSON files (YYYYMMDD_articles.json)
│   └── processed_hashes.json   ← Tracks already-processed articles (auto)
├── music/                       ← Main playlist MP3 files
├── background_music/            ← Background music for bulletins
├── audio_queue/                 ← Generated bulletins waiting to air (auto)
└── tmp/                         ← Temporary TTS files (auto)
```

---

## 🚀 Installation

### 🐧 Linux (Ubuntu / Debian)

**1. System dependencies**
```bash
sudo apt update && sudo apt install ffmpeg docker-compose python3-pip -y
```

**2. Python dependencies**
```bash
pip install -r requirements.txt
```

**3. Start Icecast**
```bash
docker-compose up -d
```
Check it's running: http://localhost:8000

**4. Add your audio files**
```
music/             → drop your MP3s here (at least 1 required)
background_music/  → background music for bulletins (optional but recommended)
```

**5. Run Nova Media**
```bash
python main.py
```

---

### 🪟 Windows 11

**1. Install Python 3.10+**

Download from https://www.python.org/downloads/windows/

> ⚠️ Make sure to check **"Add Python to PATH"** during installation.

**2. Install ffmpeg**

1. Download a Windows build from https://ffmpeg.org/download.html (essentials or full build from gyan.dev or BtbN)
2. Extract the archive, e.g. to `C:\ffmpeg\`
3. Add `C:\ffmpeg\bin` to your **system PATH**:
   - Search "Environment Variables" in the Start menu
   - Edit the `Path` variable
   - Add `C:\ffmpeg\bin`
4. **Restart your terminal**, then verify: `ffmpeg -version`

**3. Install Docker Desktop (for Icecast)**

Download from https://www.docker.com/products/docker-desktop/

> Wait for Docker Desktop to fully start (green icon in the system tray) before running the next step.

**Alternative without Docker**: download the native Windows Icecast binary from https://www.icecast.org/download/ and use the `icecast.xml` from this project.

**4. Python dependencies**

Open a terminal (PowerShell or cmd) in the project folder:
```powershell
pip install -r requirements.txt
```

**5. Start Icecast**
```powershell
docker-compose up -d
```
Check it's running: http://localhost:8000

**6. Add your audio files**
```
music\             → drop your MP3s here (at least 1 required)
background_music\  → background music for bulletins (optional but recommended)
```

**7. Run Nova Media**
```powershell
python main.py
```

To stop: **Ctrl+C** in the terminal.

---

## 📻 Listening to the Stream

| Client | URL |
|--------|-----|
| **VLC** | Media → Open Network Stream → `http://localhost:8000/nova` |
| **Browser** | http://localhost:8000/nova |
| **ffplay** | `ffplay http://localhost:8000/nova` |

---

## ⚙️ Configuration (`config/config.yaml`)

| Key | Description | Default |
|-----|-------------|---------|
| `icecast.host` | Icecast server address | `localhost` |
| `icecast.port` | Icecast port | `8000` |
| `icecast.password` | Source password | `hackme` |
| `icecast.mount` | Mount point | `/nova` |
| `radio.news_per_bulletin` | Articles needed to trigger a bulletin | `5` |
| `radio.news_interval_seconds` | JSON polling frequency (seconds) | `2` |
| `radio.background_volume` | Background music volume (0.0–1.0) | `0.30` |
| `tts.voices` | List of edge-tts voices to use randomly | see yaml |

### Available TTS voices

List all available voices:
```bash
edge-tts --list-voices
```
Filter by language (e.g. French):
```bash
edge-tts --list-voices | grep fr-
```

---

## 📰 News JSON Format

File: `data/articles/YYYYMMDD_articles.json`

```json
[
  {
    "hash": "e6d5fef6504a0a908253ea3eb9b818c0",
    "timestamp": "2026-03-24T00:17:07.662666",
    "category": "crypto",
    "title": "Article title",
    "link": "https://...",
    "source": "source.com",
    "pub_date": "Mon, 23 Mar 2026 22:26:40 +0000",
    "summary": "Article summary that will be read on air."
  }
]
```

Only the `summary` field is used for the bulletin script. Articles with a summary starting with `[Contenu inaccessible]` are automatically skipped.

---

## 🧵 Architecture

Nova Media runs three concurrent threads:

```
main.py
  ├── Thread 1 — NewsWatcher    watches YYYYMMDD_articles.json every 2s
  │                              triggers a bulletin when N new articles detected
  │
  ├── Thread 2 — Streamer       maintains a single ffmpeg pipe to Icecast
  │                              plays music continuously, bulletins take priority
  │                              applies smooth fadeout when a bulletin is ready
  │
  └── Thread 3 — BulletinGen    spawned on demand when articles are ready
                                 builds script → TTS (edge-tts) → mix with ffmpeg
                                 drops final MP3 into audio_queue/
```

**Streaming design**: a single long-lived ffmpeg process reads from stdin and pushes to Icecast. Music and bulletins are transcoded separately and written into this pipe one after the other — no stream restarts, no client disconnections.

---

## 🔧 Troubleshooting

**VLC won't connect**
→ Check Icecast is running: `docker-compose ps`
→ Check the logs for `🔗 Connected to Icecast`

**No sound / silence**
→ Make sure there are MP3 files in `music/`

**TTS error**
→ edge-tts requires an internet connection (Microsoft API)

**Change the Icecast password**
→ Update both `icecast.xml` and `config/config.yaml`, then restart Docker

---

### 🪟 Windows-specific issues

**Absolute paths in `config.yaml`**
→ Backslashes `\` are special characters in YAML. Always use forward slashes `/`:
```yaml
# ✅ Correct
data_dir: "c:/audio/articles"
music_dir: "c:/audio/music"

# ❌ Wrong — \a and \m will be misinterpreted
data_dir: "c:\audio\articles"
```

**`ffmpeg` not found**
→ Make sure `C:\ffmpeg\bin` is in your PATH and restart the terminal after editing it
→ Test with: `where ffmpeg`

**`docker-compose` not found**
→ With Docker Desktop, use `docker compose` (no hyphen):
```powershell
docker compose up -d
```

**Encoding errors in logs**
→ Force UTF-8 in PowerShell before running:
```powershell
$OutputEncoding = [System.Text.Encoding]::UTF8
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8
python main.py
```

**Emojis not displaying in terminal**
→ Use Windows Terminal (available on the Microsoft Store) instead of the classic cmd

---

## 🛠️ Roadmap

- [ ] Claude API integration to rewrite summaries in a radio-friendly style
- [ ] Web monitoring dashboard
- [ ] Multi-stream support (multiple mount points)
- [ ] Jingles and station IDs
- [ ] Automatic news feed (RSS → AI summary → JSON)

---

## 📦 Dependencies

| Package | Purpose |
|---------|---------|
| `edge-tts` | Text-to-speech synthesis (Microsoft Neural voices) |
| `pyyaml` | YAML config parsing |
| `ffmpeg` *(system)* | Audio transcoding, mixing, streaming |
| `icecast` *(Docker)* | Live audio streaming server |

---

## 📄 License

MIT
