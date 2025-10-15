# Movie Subtitler Web UI

A simple, modern web interface that lets you upload a video, automatically generate and overlay subtitles using Whisper (via the auto-subtitle tool), and then play or download the resulting subtitled video.

Features
- Upload a video/audio file
- Choose Whisper model (tiny, base, small, medium, large, etc.)
- Optional translation to English
- Background processing with live status
- Modern player to view and download the subtitled output
- Interactive management script to install, start, stop, and uninstall
- Configurable host/port, binds to all interfaces by default

Requirements
- macOS (tested), Linux should also work
- Python 3.7+
- ffmpeg installed and available on PATH

Quick Start
1) Make the manager executable and open the interactive menu:

   ./manage.sh

2) From the menu:
   - Choose “Install dependencies” once
   - Choose “Start server” and pick host/port (default 0.0.0.0:8000)

3) Open the UI:

   http://localhost:8000/

4) Upload your video, select model/translate, wait for processing, then play/download.

Non-interactive usage
- Install: ./manage.sh install
- Start:   ./manage.sh start --host 0.0.0.0 --port 8000
- Stop:    ./manage.sh stop
- Status:  ./manage.sh status
- Uninstall: ./manage.sh uninstall
- Interactive menu directly: ./manage.sh menu

Project structure
- manage.sh: interactive installer/runner/uninstaller
- app.py: Flask web app
- templates/: HTML templates
- static/subtitled/: output directory for processed videos
- uploads/: temporary upload storage
- logs/: per-job logs

How it works
- The server accepts uploads at /upload
- A background thread calls the auto_subtitle CLI on your file
- It writes results into static/subtitled/<jobId>/subtitled video
- The status is polled from /api/job/<jobId>
- When finished, /play/<jobId> serves a player for the subtitled video

Notes
- The first run will download Whisper models as needed, which may take time.
- For best accuracy with non‑English audio, select a larger model or use “Translate to English”.
- Ensure ffmpeg is installed (on macOS you can use Homebrew: brew install ffmpeg).

Acknowledgments
- Subtitling is powered by auto-subtitle, which uses OpenAI Whisper and ffmpeg.