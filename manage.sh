#!/usr/bin/env bash
set -e

PROJECT_DIR="$(cd "$(dirname "$0")" && pwd)"
VENV_DIR="$PROJECT_DIR/.venv"
STATE_FILE="$PROJECT_DIR/install_state.json"
PID_FILE="$PROJECT_DIR/server.pid"
LOG_FILE="$PROJECT_DIR/server.log"

usage() {
  cat <<EOF
Usage: $(basename "$0") <command> [options]

If you run this script WITHOUT arguments, an interactive menu will open.

Commands:
  install                Create virtualenv, install deps (Flask, auto-subtitle), ensure ffmpeg, setup folders
  uninstall              Stop server, remove virtualenv and created folders/files
  start [--host HOST] [--port PORT]
                        Start the web UI server (default host 0.0.0.0, port 8000)
  stop                   Stop the running web UI server
  status                 Show server status (PID and HTTP reachability)
  menu                   Open the interactive control menu

Examples:
  $(basename "$0")
  $(basename "$0") start --host 0.0.0.0 --port 8080
EOF
}

ensure_python() {
  if ! command -v python3 >/dev/null 2>&1; then
    echo "Error: python3 not found. Please install Python 3.7+ first." >&2
    exit 1
  fi
}

ensure_venv() {
  ensure_python
  if [ ! -d "$VENV_DIR" ]; then
    echo "Creating virtual environment at $VENV_DIR"
    python3 -m venv "$VENV_DIR"
  fi
  # shellcheck disable=SC1091
  source "$VENV_DIR/bin/activate"
  python -m pip install --upgrade pip
}

write_state() {
  # Preserve existing host/port unless new values provided
  local HOST_VAL="$1"
  local PORT_VAL="$2"
  local EXIST_HOST="0.0.0.0"
  local EXIST_PORT="8000"
  if [ -f "$STATE_FILE" ]; then
    EXIST_HOST=$(python -c 'import json,sys;print(json.load(open(sys.argv[1])).get("host","0.0.0.0"))' "$STATE_FILE" 2>/dev/null || echo "0.0.0.0")
    EXIST_PORT=$(python -c 'import json,sys;print(json.load(open(sys.argv[1])).get("port",8000))' "$STATE_FILE" 2>/dev/null || echo "8000")
  fi
  local FINAL_HOST
  local FINAL_PORT
  FINAL_HOST=${HOST_VAL:-$EXIST_HOST}
  FINAL_PORT=${PORT_VAL:-$EXIST_PORT}
  cat > "$STATE_FILE" <<JSON
{
  "project_dir": "$PROJECT_DIR",
  "venv_dir": "$VENV_DIR",
  "installed_packages": ["Flask", "auto-subtitle", "ffmpeg-python"],
  "created_paths": [
    "$PROJECT_DIR/uploads",
    "$PROJECT_DIR/static/subtitled"
  ],
  "host": "$FINAL_HOST",
  "port": "$FINAL_PORT"
}
JSON
}

install_cmd() {
  ensure_venv
  echo "Installing Python dependencies"
  if [ -f "$PROJECT_DIR/requirements.txt" ]; then
    python -m pip install -r "$PROJECT_DIR/requirements.txt"
  else
    python -m pip install Flask
    python -m pip install git+https://github.com/m1guelpf/auto-subtitle.git
    python -m pip install ffmpeg-python
  fi
  echo "Installing auto-subtitle package"
  python -m pip install git+https://github.com/m1guelpf/auto-subtitle.git

  echo "Checking ffmpeg availability"
  if ! command -v ffmpeg >/dev/null 2>&1; then
    echo "ffmpeg not found. Attempting to install with Homebrew (macOS)."
    if command -v brew >/dev/null 2>&1; then
      brew install ffmpeg || {
        echo "Warning: failed to install ffmpeg via Homebrew. Please install ffmpeg manually." >&2
      }
    else
      echo "Warning: brew not found. Please install ffmpeg manually." >&2
    fi
  fi

  echo "Creating directories"
  mkdir -p "$PROJECT_DIR/uploads"
  mkdir -p "$PROJECT_DIR/static/subtitled"

  # Preserve last configured host/port
  write_state
  echo "Install completed."
}

is_running() {
  if [ -f "$PID_FILE" ]; then
    PID=$(cat "$PID_FILE")
    if ps -p "$PID" >/dev/null 2>&1; then
      return 0
    fi
  fi
  return 1
}

start_cmd() {
  HOST="0.0.0.0"
  PORT="8000"
  while [ $# -gt 0 ]; do
    case "$1" in
      --host)
        HOST="$2"; shift 2;;
      --port)
        PORT="$2"; shift 2;;
      *)
        echo "Unknown option: $1" >&2; usage; exit 1;;
    esac
  done

  if is_running; then
    echo "Server already running (PID $(cat "$PID_FILE"))." >&2
    exit 0
  fi

  ensure_venv
  write_state "$HOST" "$PORT"

  echo "Starting server on $HOST:$PORT"
  # shellcheck disable=SC1091
  source "$VENV_DIR/bin/activate"
  nohup python "$PROJECT_DIR/app.py" --host "$HOST" --port "$PORT" > "$LOG_FILE" 2>&1 &
  echo $! > "$PID_FILE"
  echo "Server started (PID $(cat "$PID_FILE"))"
}

stop_cmd() {
  if ! is_running; then
    echo "Server is not running."
    [ -f "$PID_FILE" ] && rm -f "$PID_FILE"
    exit 0
  fi
  PID=$(cat "$PID_FILE")
  echo "Stopping server (PID $PID)"
  kill "$PID" || true
  sleep 1
  if ps -p "$PID" >/dev/null 2>&1; then
    echo "Process still running, sending SIGKILL"
    kill -9 "$PID" || true
  fi
  rm -f "$PID_FILE"
  echo "Server stopped."
}

status_cmd() {
  if is_running; then
    if [ -f "$PID_FILE" ]; then
      echo "Server is running (PID $(cat \"$PID_FILE\"))."
    else
      echo "Server is running (PID unknown)."
    fi
    if [ -f "$STATE_FILE" ]; then
      HOST=$(python -c 'import json,sys;print(json.load(open(sys.argv[1]))["host"])' "$STATE_FILE" 2>/dev/null || echo "0.0.0.0")
      PORT=$(python -c 'import json,sys;print(json.load(open(sys.argv[1]))["port"])' "$STATE_FILE" 2>/dev/null || echo "8000")
      echo "Listening at http://$HOST:$PORT/"
    fi
    return 0
  fi
  # Not running by PID — try HTTP reachability using stored host/port
  if [ -f "$STATE_FILE" ]; then
    HOST=$(python -c 'import json,sys;print(json.load(open(sys.argv[1])).get("host","0.0.0.0"))' "$STATE_FILE" 2>/dev/null || echo "0.0.0.0")
    PORT=$(python -c 'import json,sys;print(json.load(open(sys.argv[1])).get("port",8000))' "$STATE_FILE" 2>/dev/null || echo "8000")
    if command -v curl >/dev/null 2>&1; then
      if curl -s --max-time 2 -o /dev/null "http://$HOST:$PORT/"; then
        echo "Server appears reachable at http://$HOST:$PORT/, but PID file is missing."
        return 0
      fi
    else
      # Fallback to Python check if curl is unavailable
      python - <<PY 2>/dev/null || true
import sys,urllib.request
try:
    urllib.request.urlopen(f"http://$HOST:$PORT/", timeout=2)
    print("Server appears reachable at http://$HOST:$PORT/, but PID file is missing.")
except Exception:
    pass
PY
      if [ "$?" -eq 0 ]; then return 0; fi
    fi
  fi
  echo "Server is not running."
}

uninstall_cmd() {
  echo "Uninstalling web UI and cleaning up"
  stop_cmd || true

  if [ -f "$STATE_FILE" ]; then
    # Remove created paths
    echo "Removing created paths"
    python - "$STATE_FILE" <<'PY'
import json, os, shutil, sys
state = json.load(open(sys.argv[1]))
for p in state.get("created_paths", []):
    try:
        if os.path.isdir(p):
            shutil.rmtree(p, ignore_errors=True)
        elif os.path.exists(p):
            os.remove(p)
    except Exception as e:
        print(f"Warning: failed to remove {p}: {e}")
PY
  fi

  echo "Removing virtual environment"
  rm -rf "$VENV_DIR"

  echo "Cleaning state/log files"
  rm -f "$STATE_FILE" "$PID_FILE" "$LOG_FILE"

  echo "Uninstall complete."
}

interactive_menu() {
  while true; do
    clear 2>/dev/null || true
    echo "========================================"
    echo " Movie Subtitler - Interactive Control"
    echo "========================================"
    echo "Project: $PROJECT_DIR"
    echo "Virtualenv: $VENV_DIR"
    if [ -f "$STATE_FILE" ]; then
      MH=$(python -c 'import json,sys;print(json.load(open(sys.argv[1])).get("host","0.0.0.0"))' "$STATE_FILE" 2>/dev/null || echo "0.0.0.0")
      MP=$(python -c 'import json,sys;print(json.load(open(sys.argv[1])).get("port",8000))' "$STATE_FILE" 2>/dev/null || echo "8000")
      echo "Last configured: $MH:$MP"
    fi
    echo "----------------------------------------"
    echo "Select an option:"
    echo "  1) Install dependencies"
    echo "  2) Start server"
    echo "  3) Stop server"
    echo "  4) Status"
    echo "  5) View recent logs"
    echo "  6) Uninstall (cleanup)"
    echo "  7) Exit"
    echo "  8) Restart server (use last configured host/port)"
    echo -n "Enter choice [1-8]: "
    read -r choice
    case "$choice" in
      1)
        install_cmd
        echo "\nInstall finished. Press Enter to continue..."; read -r ;;
      2)
        DEFAULT_HOST="0.0.0.0"
        DEFAULT_PORT="8000"
        if [ -f "$STATE_FILE" ]; then
          DEFAULT_HOST=$(python -c 'import json,sys;print(json.load(open(sys.argv[1])).get("host","0.0.0.0"))' "$STATE_FILE" 2>/dev/null || echo "0.0.0.0")
          DEFAULT_PORT=$(python -c 'import json,sys;print(json.load(open(sys.argv[1])).get("port",8000))' "$STATE_FILE" 2>/dev/null || echo "8000")
        fi
        echo -n "Host [$DEFAULT_HOST]: "
        read -r HOST
        HOST=${HOST:-$DEFAULT_HOST}
        echo -n "Port [$DEFAULT_PORT]: "
        read -r PORT
        PORT=${PORT:-$DEFAULT_PORT}
        start_cmd --host "$HOST" --port "$PORT"
        echo "\nServer started on $HOST:$PORT. Press Enter to continue..."; read -r ;;
      3)
        stop_cmd
        echo "\nServer stopped. Press Enter to continue..."; read -r ;;
      4)
        status_cmd
        echo "\nPress Enter to continue..."; read -r ;;
      5)
        echo "\nRecent logs (last 50 lines):"
        if [ -f "$LOG_FILE" ]; then
          tail -n 50 "$LOG_FILE"
        else
          echo "No logs yet."
        fi
        echo "\nPress Enter to continue..."; read -r ;;
      6)
        echo -n "Are you sure you want to uninstall and cleanup everything? [y/N]: "
        read -r confirm
        if [ "$confirm" = "y" ] || [ "$confirm" = "Y" ]; then
          uninstall_cmd
        else
          echo "Skipped uninstall."
        fi
        echo "\nPress Enter to continue..."; read -r ;;
      7)
        echo "Goodbye!"; break ;;
      8)
        RH=$(python -c 'import json,sys;print(json.load(open(sys.argv[1])).get("host","0.0.0.0"))' "$STATE_FILE" 2>/dev/null || echo "0.0.0.0")
        RP=$(python -c 'import json,sys;print(json.load(open(sys.argv[1])).get("port",8000))' "$STATE_FILE" 2>/dev/null || echo "8000")
        stop_cmd
        start_cmd --host "$RH" --port "$RP"
        echo "\nServer restarted on $RH:$RP. Press Enter to continue..."; read -r ;;
      *)
        echo "Invalid choice."; sleep 1 ;;
    esac
  done
}

case "$1" in
  "")
    interactive_menu; exit 0 ;;
  menu)
    shift; interactive_menu ;;
  install)
    shift; install_cmd "$@" ;;
  start)
    shift; start_cmd "$@" ;;
  stop)
    shift; stop_cmd "$@" ;;
  status)
    shift; status_cmd "$@" ;;
  uninstall)
    shift; uninstall_cmd "$@" ;;
  *)
    usage; exit 1;;
esac