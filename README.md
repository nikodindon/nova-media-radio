# 🎙️ Nova Media — AI-Powered Automated Radio

> An autonomous AI radio station driven by live news data, Text-to-Speech synthesis, and Icecast streaming.

Music plays continuously. When enough new articles are detected, an AI-voiced news bulletin is automatically generated and seamlessly inserted into the live stream — with a smooth fadeout of the current track and no interruption to listeners.

---

## ✨ Features

- 🎵 **Truly continuous streaming** — a single permanent ffmpeg pipe to Icecast, VLC never disconnects between tracks
- 📰 **Real-time news monitoring** — watches a JSON file for incoming articles every 2 seconds
- 🎙️ **Automatic bulletin generation** — assembles a script, synthesizes speech with edge-tts, mixes with background music
- 🎚️ **Smooth pre-generated fadeout** — fadeout is built in parallel while music still plays, injected seamlessly with zero silence gap
- 🔀 **Random voices** — picks a different TTS voice for each bulletin
- 🐛 **Debug mode** — `--debug` flag captures full ffmpeg logs for troubleshooting
- 🌐 **Cross-platform** — works on Linux and Windows 11
- ⚙️ **Fully configurable** via a single YAML file

---

## 📁 Project Structure

```
nova_media/
├── main.py                      ← Entry point — orchestrates all threads
├── requirements.txt
├── nova_media.m3u               ← VLC playlist with auto-reconnect
├── docker-compose.yml           ← Icecast server (Docker)
├── icecast.xml                  ← Icecast configuration
├── config/
│   └── config.yaml              ← All settings (Icecast, TTS, audio, paths)
├── modules/
│   ├── news_watcher.py          ← JSON file watcher
│   ├── journal_builder.py       ← Script builder + TTS + audio mixing
│   └── streamer.py              ← Continuous Icecast stream via ffmpeg pipe
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

> ⚠️ Check **"Add Python to PATH"** during installation.

**2. Install ffmpeg**

1. Download a Windows build from https://ffmpeg.org/download.html (essentials build from gyan.dev)
2. Extract to e.g. `C:\ffmpeg\`
3. Add `C:\ffmpeg\bin` to your **system PATH**:
   - Search "Environment Variables" in Start menu → Edit `Path` → Add `C:\ffmpeg\bin`
4. **Restart your terminal**, verify with: `ffmpeg -version`

**3. Install Docker Desktop**

Download from https://www.docker.com/products/docker-desktop/ — wait for the green icon in the system tray before continuing.

**Alternative without Docker**: download the native Windows Icecast binary from https://www.icecast.org/download/ and use the `icecast.xml` from this project.

**4. Python dependencies**
```powershell
pip install -r requirements.txt
```

**5. Start Icecast**
```powershell
docker-compose up -d
```

**6. Add your audio files**
```
music\             → drop your MP3s here (at least 1 required)
background_music\  → background music for bulletins (optional)
```

**7. Run Nova Media**
```powershell
python main.py
```

To stop: **Ctrl+C**

---

## 📻 Listening to the Stream

| Client | How |
|--------|-----|
| **VLC (recommended)** | Open `nova_media.m3u` — auto-reconnects if stream drops |
| **VLC (manual)** | Media → Open Network Stream → `http://localhost:8000/nova` |
| **Browser** | http://localhost:8000/nova |
| **ffplay** | `ffplay http://localhost:8000/nova` |

> 💡 Use `nova_media.m3u` in VLC for the best experience — it handles reconnection automatically.

---

## ▶️ Command Line Options

```bash
python main.py                        # normal mode
python main.py --debug                # debug mode: full ffmpeg logs, detailed output
python main.py --config path/to.yaml  # custom config file path
```

**Debug mode** writes to `nova_media_debug.log` and captures:
- Full ffmpeg stderr output (codec info, errors, stream details)
- Thread name on every log line
- Exact pipe error type when Icecast disconnects
- ffmpeg PID and version at startup

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

### TTS voices

List all available voices:
```bash
edge-tts --list-voices
# Filter by language:
edge-tts --list-voices | grep fr-
```

### Absolute paths on Windows

Use forward slashes `/` in `config.yaml` — backslashes are special characters in YAML:
```yaml
# ✅ Correct
data_dir: "c:/audio/articles"

# ❌ Wrong — \a will be misinterpreted
data_dir: "c:\audio\articles"
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

Only the `summary` field is used for the bulletin script.

Articles with a summary starting with `[Contenu inaccessible]` are automatically skipped and never counted toward the bulletin threshold.

---

## 🧵 Architecture

Nova Media runs several concurrent threads:

```
main.py
  ├── NewsWatcher      watches YYYYMMDD_articles.json every 2s
  │                    triggers bulletin generation when N new articles detected
  │
  ├── Streamer         main playback loop — music or bulletin, one at a time
  │
  ├── Heartbeat        sends 1s of silence every 500ms when nothing is streaming
  │                    keeps the Icecast pipe alive between tracks
  │
  ├── BulletinGen      spawned on demand: TTS → mix → audio_queue/
  │
  ├── _prebuild_fadeout spawned when bulletin arrives during music playback
  │                    generates fadeout IN PARALLEL while music still plays
  │
  ├── ffmpeg/transcode  one process per audio file, reads source MP3
  │
  └── ffmpeg/icecast   ONE permanent process → Icecast (never restarted between tracks)
```

### Why the stream stays continuous

The core design uses **a single permanent ffmpeg process** connected to Icecast via stdin pipe. Key options that make this work:

- **`-probesize 32 -analyzeduration 0`** — prevents ffmpeg from waiting for a complete MP3 header at pipe start; each file's bytes flow straight through without triggering "Header missing" errors
- **`-vn -map 0:a`** — strips embedded cover art (PNG/JPEG) from MP3 files before sending to Icecast; without this, ffmpeg tries to stream the image as a video track and Icecast returns 404

### Fadeout sequence

```
Journal ready → _fade_requested set
                 ↓
             _prebuild_fadeout thread starts (runs ffmpeg -ss {position} -af afade)
                 ↓
             Music continues playing for up to 4s while fadeout generates (~1s)
                 ↓
             Fadeout cached in memory → music transoder killed cleanly
                 ↓
             Fadeout bytes injected into pipe → bulletin plays → next music track
```

---

## 🔧 Troubleshooting

**VLC disconnects between tracks**
→ Use `nova_media.m3u` instead of the raw URL — VLC reconnects automatically

**No sound / silence**
→ Make sure there are MP3 files in `music/`

**TTS error**
→ edge-tts requires an internet connection (Microsoft API)

**Change the Icecast password**
→ Update both `icecast.xml` AND `config/config.yaml`, then restart Docker:
```powershell
docker-compose down && docker-compose up -d
```

**Diagnose pipe issues**
→ Run with `--debug` and capture `nova_media_debug.log` — look for `BrokenPipeError` or ffmpeg error codes

---

### 🪟 Windows-specific issues

**`ffmpeg` not found**
→ Verify `C:\ffmpeg\bin` is in PATH, restart terminal, test with `where ffmpeg`

**`docker-compose` not found**
→ With Docker Desktop use `docker compose` (no hyphen):
```powershell
docker compose up -d
```

**Encoding errors in logs**
→ Force UTF-8 in PowerShell:
```powershell
$OutputEncoding = [System.Text.Encoding]::UTF8
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8
python main.py
```

**Emojis not displaying**
→ Use Windows Terminal (Microsoft Store) instead of cmd

---

## 🌿 Branches

| Branch | Description |
|--------|-------------|
| `main` | Stable production version with edge-tts |
| `v2-melotts` | In progress — XTTS-v2 local TTS (higher quality, no internet needed) |

---

## 🛠️ Roadmap

- [ ] XTTS-v2 local TTS integration (branch `v2-melotts`)
- [ ] Claude API integration to rewrite summaries in radio-friendly style
- [ ] Web monitoring dashboard (current track, next bulletin, thread status)
- [ ] Multi-stream support (multiple mount points)
- [ ] Jingles and station IDs
- [ ] Automatic news feed (RSS → AI summary → JSON)

---

## 📦 Dependencies

| Package | Purpose |
|---------|---------|
| `edge-tts` | Text-to-speech (Microsoft Neural voices, internet required) |
| `pyyaml` | YAML config parsing |
| `ffmpeg` *(system)* | Audio transcoding, mixing, pipe streaming |
| `icecast` *(Docker)* | Live audio streaming server |

---

## 📄 License

MIT
