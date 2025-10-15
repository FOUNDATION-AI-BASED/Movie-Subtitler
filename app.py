from flask import Flask, render_template, request, redirect, url_for, send_from_directory, jsonify
import os
import uuid
import threading
import subprocess
import shlex
from werkzeug.utils import secure_filename
import argparse
import json
from typing import Optional

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
UPLOAD_DIR = os.path.join(BASE_DIR, 'uploads')
OUTPUT_BASE_DIR = os.path.join(BASE_DIR, 'static', 'subtitled')
LOG_DIR = os.path.join(BASE_DIR, 'logs')

os.makedirs(UPLOAD_DIR, exist_ok=True)
os.makedirs(OUTPUT_BASE_DIR, exist_ok=True)
os.makedirs(LOG_DIR, exist_ok=True)

ALLOWED_EXTENSIONS = {'.mp4', '.mov', '.mkv', '.avi', '.wav', '.mp3'}

app = Flask(__name__)
app.config['MAX_CONTENT_LENGTH'] = 2 * 1024 * 1024 * 1024  # 2GB

# In-memory registry (best-effort), persisted per job under logs/<id>.json
JOB_REGISTRY = {}


def _job_state_path(job_id: str) -> str:
    return os.path.join(LOG_DIR, f"{job_id}.json")


def _save_job_state(job_id: str, state: dict):
    JOB_REGISTRY[job_id] = state
    try:
        with open(_job_state_path(job_id), 'w') as f:
            json.dump(state, f)
    except Exception:
        pass


def _load_job_state(job_id: str) -> dict:
    if job_id in JOB_REGISTRY:
        return JOB_REGISTRY[job_id]
    try:
        with open(_job_state_path(job_id), 'r') as f:
            data = json.load(f)
            JOB_REGISTRY[job_id] = data
            return data
    except Exception:
        return {"status": "unknown"}


def allowed_file(filename: str) -> bool:
    _, ext = os.path.splitext(filename.lower())
    return ext in ALLOWED_EXTENSIONS


def find_output_video(out_dir: str) -> Optional[str]:
    if not os.path.isdir(out_dir):
        return None
    for name in os.listdir(out_dir):
        if name.lower().endswith('.mp4'):
            return os.path.join(out_dir, name)
    return None


# Add helper to list completed jobs
def list_completed_jobs():
    jobs = []
    try:
        for name in os.listdir(LOG_DIR):
            if not name.endswith('.json'):
                continue
            job_id = name[:-5]
            json_path = os.path.join(LOG_DIR, name)
            try:
                with open(json_path, 'r') as f:
                    st = json.load(f)
                if st.get('status') == 'done' and st.get('output_file'):
                    # Derive original filename from input path
                    inp = st.get('input', '')
                    base = os.path.basename(inp)
                    idx = base.find('_')
                    original = base[idx+1:] if idx != -1 else base
                    jobs.append({
                        'job_id': job_id,
                        'original_filename': original,
                        'output_file': st.get('output_file'),
                        'mtime': os.path.getmtime(json_path)
                    })
            except Exception:
                continue
        jobs.sort(key=lambda x: x['mtime'], reverse=True)
    except Exception:
        pass
    return jobs


def run_auto_subtitle(input_path: str, output_dir: str, model: Optional[str], translate: bool, job_id: str):
    os.makedirs(output_dir, exist_ok=True)
    log_path = os.path.join(LOG_DIR, f"{job_id}.log")
    _save_job_state(job_id, {
        "status": "processing",
        "input": input_path,
        "output_dir": output_dir,
        "output_file": None,
        "error": None
    })
    cmd = [
        'auto_subtitle',
        input_path,
        '-o', output_dir
    ]
    if model:
        cmd.extend(['--model', model])
    if translate:
        cmd.extend(['--task', 'translate'])

    with open(log_path, 'w') as logf:
        logf.write(f"Running: {' '.join(shlex.quote(c) for c in cmd)}\n")
        logf.flush()
        try:
            proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
            for line in proc.stdout:
                logf.write(line)
                logf.flush()
            rc = proc.wait()
            if rc != 0:
                _save_job_state(job_id, {
                    "status": "error",
                    "input": input_path,
                    "output_dir": output_dir,
                    "output_file": None,
                    "error": f"auto_subtitle exited with code {rc}"
                })
                return
            # Find resulting video file
            out_video = find_output_video(output_dir)
            if out_video and os.path.exists(out_video):
                rel_path = os.path.relpath(out_video, os.path.join(BASE_DIR, 'static'))
                _save_job_state(job_id, {
                    "status": "done",
                    "input": input_path,
                    "output_dir": output_dir,
                    "output_file": rel_path.replace('\\\\', '/').replace('\\', '/'),
                    "error": None
                })
            else:
                _save_job_state(job_id, {
                    "status": "error",
                    "input": input_path,
                    "output_dir": output_dir,
                    "output_file": None,
                    "error": "Output file not found"
                })
        except Exception as e:
            _save_job_state(job_id, {
                "status": "error",
                "input": input_path,
                "output_dir": output_dir,
                "output_file": None,
                "error": str(e)
            })


@app.route('/')
def index():
    completed = list_completed_jobs()
    return render_template('index.html', completed_jobs=completed)


@app.route('/upload', methods=['POST'])
def upload():
    file = request.files.get('video')
    model = request.form.get('model') or None
    translate = request.form.get('translate') == 'on'

    if not file or file.filename == '':
        return render_template('index.html', error='Please select a file to upload.')

    filename = secure_filename(file.filename)
    if not allowed_file(filename):
        return render_template('index.html', error='Unsupported file format.')

    job_id = uuid.uuid4().hex
    input_path = os.path.join(UPLOAD_DIR, f"{job_id}_{filename}")
    file.save(input_path)

    output_dir = os.path.join(OUTPUT_BASE_DIR, job_id)

    # Persist initial job state
    _save_job_state(job_id, {
        "status": "queued",
        "input": input_path,
        "output_dir": output_dir,
        "output_file": None,
        "error": None
    })

    # Start background thread
    t = threading.Thread(target=run_auto_subtitle, args=(input_path, output_dir, model, translate, job_id), daemon=True)
    t.start()

    return redirect(url_for('job_status_page', job_id=job_id))


@app.route('/job/<job_id>')
def job_status_page(job_id):
    state = _load_job_state(job_id)
    return render_template('job.html', job_id=job_id, state=state)


@app.route('/api/job/<job_id>')
def job_status(job_id):
    state = _load_job_state(job_id)
    return jsonify(state)


@app.route('/play/<job_id>')
def play(job_id):
    state = _load_job_state(job_id)
    if state.get('status') != 'done' or not state.get('output_file'):
        return redirect(url_for('job_status_page', job_id=job_id))
    return render_template('play.html', job_id=job_id, output_file=state['output_file'])


@app.route('/static/<path:path>')
def static_files(path):
    return send_from_directory(os.path.join(BASE_DIR, 'static'), path)


def main():
    parser = argparse.ArgumentParser(description='Movie Subtitler Web UI')
    parser.add_argument('--host', default='0.0.0.0')
    parser.add_argument('--port', default=8000, type=int)
    args = parser.parse_args()
    app.run(host=args.host, port=args.port)


if __name__ == '__main__':
    main()
