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

Compatible Operating Systems
- macOS (primary target; tested)
- Linux (Ubuntu/Debian/Fedora and similar; expected to work)
- Windows: Core Python may run, but manage.sh and certain dependencies (ffmpeg install paths, shell utilities) are primarily geared for Unix-like systems. If you use Windows, consider WSL2 or adapt the start/stop scripts.

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
- Jobs are queued and processed sequentially by a background worker
- The auto_subtitle CLI runs on your file
- It writes results into static/subtitled/<jobId>/subtitled video
- The status is polled from /api/job/<jobId>
- When finished, /play/<jobId> serves a player for the subtitled video

Notes
- The first run will download Whisper models as needed, which may take time.
- For best accuracy with non‑English audio, select a larger model or use “Translate to English”.
- Ensure ffmpeg is installed (on macOS you can use Homebrew: brew install ffmpeg).

Release Notes (Latest)
- Current Jobs panel: Added a live section on the home page that shows processing and queued jobs, including queue position and quick “View” links.
- Removed outdated links: Eliminated the non‑functional “Open job details” link on the player page and removed “Open Player” from the processing page (auto-redirect still occurs upon completion).
- Removed Translate checkbox: The dedicated “Translate to English” checkbox is gone; you can now choose English or any other target language directly from the Target subtitles selector.
- Searchable language selectors: Added type‑to‑filter search inputs on the audio language and target subtitle language dropdowns for faster selection.
- Multilingual subtitle translation: Implemented an on‑device pipeline using Whisper for transcription and Argos Translate for offline translation to many target languages (English and non‑English). If a language pair isn’t installed, the app will fetch and install it automatically before translating.
- Hardware‑aware recommendations: Added hardware detection and dynamic model recommendations (CUDA/MPS/CPU, VRAM). The UI shows tooltips and guidance to help you choose the right Whisper model for your device and target language.
- Job and Player metadata: The job status and player pages now display the chosen Whisper model, audio language, and target subtitle language so you can verify settings per job.
- Turbo model: Kept “turbo” in the model selector. Turbo is an optimized large‑v3 variant that offers faster transcription with minimal accuracy tradeoffs in many scenarios.
- Sequential job processing: Reliable in‑app queue so multiple uploads are handled one after another.
- UI improvements: Refreshed button styles, modern dark theme tweaks, corrected static download paths, and better mobile layout.
- Job details UX: The job status page shows queue position and handles missing/expired jobs gracefully without reload loops.
- Player enhancements: Share Link button copies a local‑network URL (only works on the same LAN).
- Spacebar behavior: Space toggles play/pause without page scrolling.
- Host/port persistence: Server restarts reuse your last configured host/port.
- Performance note: Whisper performance varies by language, speaking rate, and hardware. For non‑English audio or translation tasks, consider larger models for lower error rates.

Acknowledgments
- Subtitling is powered by auto-subtitle, which uses OpenAI Whisper and ffmpeg.