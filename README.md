# 🎬 Video Studio Bot

A focused Telegram bot with two operations:

| Feature | Description |
|---|---|
| 🔤 **Burn Subtitles** | Hardcode .srt / .ass / .vtt / .sub into any video — permanently part of the picture |
| 📐 **Change Resolution** | Re-encode to 360p / 480p / 720p / 1080p / 1440p / 4K with aspect-ratio-preserving letterboxing |

Powered by **FFmpeg**. No cloud APIs needed.

---

## 🚀 Setup

### 1. Install FFmpeg (required)

```bash
# Ubuntu / Debian
sudo apt install ffmpeg

# macOS
brew install ffmpeg

# Windows — download from https://ffmpeg.org/download.html
```

### 2. Create your bot

Open [@BotFather](https://t.me/BotFather) → `/newbot` → copy the token.

### 3. Configure

```bash
cp bot/.env.example bot/.env
# Edit bot/.env — fill in BOT_TOKEN and MINI_APP_URL
```

### 4. Install Python dependencies

```bash
pip install -r bot/requirements.txt
```

### 5. Run

```bash
python bot/main.py
```

### 6. (Optional) Mini App

Host `mini_app/index.html` on Vercel / Netlify / GitHub Pages, then paste the URL into `.env` as `MINI_APP_URL`.

---

## 🔄 How it works

```
User sends video
      │
      ▼
Bot: "What do you want to do?"
   [🔤 Burn Subtitles]  [📐 Change Resolution]
      │                         │
      ▼                         ▼
User sends .srt/.ass/…    User picks resolution
      │                         │
      ▼                         ▼
FFmpeg: subtitles filter    FFmpeg: scale + pad filter
      │                         │
      ▼                         ▼
Bot sends back converted MP4 ✅
```

### Subtitle burning (FFmpeg filter)
```
-vf "ass=subtitles.ass"          # for .ass/.ssa — preserves custom styles
-vf "subtitles=subs.srt:..."     # for .srt/.sub — uses clean white default style
```
SRT/VTT files are first converted to ASS internally for consistent rendering.

### Resolution change (FFmpeg filter)
```
-vf "scale=1280:720:force_original_aspect_ratio=decrease,
     pad=1280:720:(ow-iw)/2:(oh-ih)/2:black"
```
This shrinks/grows the video to fit inside the target frame, then adds black bars (letterbox/pillarbox) to fill any remaining space. The output is always exactly the requested size.

---

## 📁 Project structure

```
bot/
  main.py              # Entry point
  config.py            # Settings & constants
  handlers/
    start.py           # /start, /help commands
    workflow.py        # Main FSM: video → operation → result
  processors/
    ffmpeg.py          # burn_subtitles() + change_resolution()
  utils/
    file_utils.py      # format_size, cleanup, file_icon, …
mini_app/
  index.html           # Telegram Mini App (single file)
```
