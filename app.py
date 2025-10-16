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
import socket
from collections import deque
import whisper
import torch
from argostranslate import package as argos_package, translate as argos_translate
import srt
from datetime import timedelta
import shutil

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
JOB_QUEUE = deque()
WORKER_THREAD = None
WORKER_LOCK = threading.Lock()
RUNNING_PROCS = {}


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


def list_current_jobs():
    jobs = []
    # Collect processing/queued from persisted states
    try:
        for name in os.listdir(LOG_DIR):
            if not name.endswith('.json'):
                continue
            job_id = name[:-5]
            json_path = os.path.join(LOG_DIR, name)
            try:
                with open(json_path, 'r') as f:
                    st = json.load(f)
                status = st.get('status')
                if status in ('processing', 'queued'):
                    inp = st.get('input', '')
                    base = os.path.basename(inp) if inp else ''
                    idx = base.find('_')
                    original = base[idx+1:] if idx != -1 else base or None
                    jobs.append({
                        'job_id': job_id,
                        'original_filename': original,
                        'status': status,
                        'queue_position': st.get('queue_position')
                    })
            except Exception:
                continue
    except Exception:
        pass
    # Overlay live in-memory queue positions
    with WORKER_LOCK:
        for i, j in enumerate(JOB_QUEUE):
            job_id = j['job_id']
            pos = i + 1
            found = next((x for x in jobs if x['job_id'] == job_id), None)
            inp = j.get('input_path', '')
            base = os.path.basename(inp) if inp else ''
            idx = base.find('_')
            original = base[idx+1:] if idx != -1 else base or None
            if found:
                found['status'] = 'queued'
                found['queue_position'] = pos
                if not found.get('original_filename') and original:
                    found['original_filename'] = original
            else:
                jobs.append({
                    'job_id': job_id,
                    'original_filename': original,
                    'status': 'queued',
                    'queue_position': pos
                })
    jobs.sort(key=lambda x: (0 if x.get('status') == 'processing' else 1, x.get('queue_position') or 9999))
    return jobs


def get_local_ip() -> str:
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(('8.8.8.8', 80))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except Exception:
        return 'localhost'


def _start_worker_if_needed():
    global WORKER_THREAD
    with WORKER_LOCK:
        if WORKER_THREAD and WORKER_THREAD.is_alive():
            return
        WORKER_THREAD = threading.Thread(target=_worker_loop, daemon=True)
        WORKER_THREAD.start()


def _worker_loop():
    while True:
        try:
            job = JOB_QUEUE.popleft()
        except IndexError:
            break
        # Begin processing
        job_id = job['job_id']
        state = _load_job_state(job_id)
        state.update({"status": "processing"})
        _save_job_state(job_id, state)
        # Unified subtitle generation pipeline (handles translate and no-translate)
        tgt = job.get('target_language') or ''
        run_multilingual_subtitle(
            job['input_path'], job['output_dir'], job['model'], job_id, job.get('language'), tgt
        )


def enqueue_job(job_id: str, input_path: str, output_dir: str, model: Optional[str], translate: bool, language: Optional[str], target_language: Optional[str] = None):
    JOB_QUEUE.append({
        'job_id': job_id,
        'input_path': input_path,
        'output_dir': output_dir,
        'model': model,
        'translate': translate,
        'language': language,
        'target_language': target_language,
    })
    # save queued state with position
    position = len(JOB_QUEUE)
    _save_job_state(job_id, {
        "status": "queued",
        "input": input_path,
        "output_dir": output_dir,
        "output_file": None,
        "error": None,
        "queue_position": position,
        "model": model,
        "translate": translate,
        "language": language,
        "target_language": target_language,
    })
    _start_worker_if_needed()


def run_auto_subtitle(input_path: str, output_dir: str, model: Optional[str], translate: bool, job_id: str, language: Optional[str] = None):
    os.makedirs(output_dir, exist_ok=True)
    log_path = os.path.join(LOG_DIR, f"{job_id}.log")
    _save_job_state(job_id, {
        "status": "processing",
        "input": input_path,
        "output_dir": output_dir,
        "output_file": None,
        "error": None
    })
    # Resolve auto_subtitle executable
    auto_bin = shutil.which('auto_subtitle')
    if not auto_bin:
        venv_bin = os.path.join(BASE_DIR, '.venv', 'bin', 'auto_subtitle')
        if os.path.exists(venv_bin):
            auto_bin = venv_bin
    if not auto_bin:
        _save_job_state(job_id, {
            "status": "error",
            "input": input_path,
            "output_dir": output_dir,
            "output_file": None,
            "error": "auto_subtitle CLI not found. Ensure dependencies are installed (pip install -r requirements.txt) and that the virtual environment is active.",
        })
        return
    cmd = [
        auto_bin,
        input_path,
        '-o', output_dir
    ]
    if model:
        cmd.extend(['--model', model])
    if translate:
        cmd.extend(['--task', 'translate'])
    if language:
        cmd.extend(['--language', language])

    with open(log_path, 'w') as logf:
        logf.write(f"Running: {' '.join(shlex.quote(c) for c in cmd)}\n")
        logf.flush()
        try:
            proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
            RUNNING_PROCS[job_id] = proc
            for line in proc.stdout:
                logf.write(line)
                logf.flush()
            rc = proc.wait()
            RUNNING_PROCS.pop(job_id, None)
            # If canceled, stop without overriding status
            state = _load_job_state(job_id)
            if state.get('status') == 'canceled':
                return
            if rc != 0:
                _save_job_state(job_id, {
                    "status": "error",
                    "input": input_path,
                    "output_dir": output_dir,
                    "output_file": None,
                    "error": f"auto_subtitle exited with code {rc}"
                })
                return
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


def run_multilingual_subtitle(input_path: str, output_dir: str, model: Optional[str], job_id: str, source_lang: Optional[str], target_lang: str):
    os.makedirs(output_dir, exist_ok=True)
    log_path = os.path.join(LOG_DIR, f"{job_id}.log")
    _save_job_state(job_id, {
        "status": "processing",
        "input": input_path,
        "output_dir": output_dir,
        "output_file": None,
        "error": None,
        "model": model,
        "language": source_lang,
        "target_language": target_lang,
    })
    with open(log_path, 'a') as logf:
        try:
            # 1) Transcribe with Whisper (no translation)
            mname = model or 'small'
            logf.write(f"Loading Whisper model: {mname}\n")
            wmodel = whisper.load_model(mname)
            transcribe_kwargs = {}
            if source_lang:
                transcribe_kwargs['language'] = source_lang
            transcribe_kwargs['task'] = 'transcribe'
            logf.write(f"Transcribing: {input_path} (lang={source_lang or 'auto'})\n")
            # Track a fake proc for cancellation consistency (transcribe runs in-process)
            RUNNING_PROCS[job_id] = None
            result = wmodel.transcribe(input_path, **transcribe_kwargs)
            RUNNING_PROCS.pop(job_id, None)
            # Early exit if canceled
            state = _load_job_state(job_id)
            if state.get('status') == 'canceled':
                return
            # Detect language from Whisper if not provided
            detected_lang = None
            try:
                detected_lang = result.get('language')
            except Exception:
                detected_lang = None
            effective_src = source_lang or detected_lang or ''
            if effective_src:
                st = _load_job_state(job_id)
                st['language'] = effective_src
                _save_job_state(job_id, st)
            segments = result.get('segments', [])
            # Build original SRT
            subs = []
            for i, seg in enumerate(segments, start=1):
                start = timedelta(seconds=float(seg.get('start', 0)))
                end = timedelta(seconds=float(seg.get('end', 0)))
                text = seg.get('text', '')
                subs.append(srt.Subtitle(index=i, start=start, end=end, content=text))
            orig_srt_path = os.path.join(output_dir, 'captions_source.srt')
            with open(orig_srt_path, 'w', encoding='utf-8') as sf:
                sf.write(srt.compose(subs))
            logf.write(f"Wrote source SRT: {orig_srt_path}\n")

            # 2) Translate via Argos if target differs and is non-empty
            def ensure_argos_pair(src: str, tgt: str) -> bool:
                try:
                    installed = argos_translate.get_installed_languages()
                    src_lang = next((l for l in installed if l.code == src), None)
                    tgt_lang = next((l for l in installed if l.code == tgt), None)
                    if src_lang and tgt_lang:
                        return True
                    # Refresh the remote package index for available language pairs
                    argos_package.update_package_index()
                    avail = argos_package.get_available_packages()
                    pkg = next((p for p in avail if p.from_code == src and p.to_code == tgt), None)
                    if pkg:
                        path = pkg.download()
                        argos_package.install_from_path(path)
                        return True
                except Exception as e:
                    logf.write(f"Argos setup error: {e}\n")
                return False

            src_code = effective_src or 'en'
            translated_subs = subs
            tgt_code = target_lang or src_code
            if target_lang and target_lang != '' and target_lang != src_code:
                ok = ensure_argos_pair(src_code, target_lang)
                installed = argos_translate.get_installed_languages()
                src_lang_obj = next((l for l in installed if l.code == src_code), None)
                tgt_lang_obj = next((l for l in installed if l.code == target_lang), None)
                if not (src_lang_obj and tgt_lang_obj):
                    logf.write("Argos language pair not installed; proceeding with source captions.\n")
                else:
                    translator = src_lang_obj.get_translation(tgt_lang_obj)
                    translated_subs = []
                    for s in subs:
                        try:
                            ttext = translator.translate(s.content)
                        except Exception as e:
                            logf.write(f"Translate error: {e}; keeping source text.\n")
                            ttext = s.content
                        translated_subs.append(srt.Subtitle(index=s.index, start=s.start, end=s.end, content=ttext))
            translated_srt_path = os.path.join(output_dir, f"captions_{tgt_code}.srt")
            with open(translated_srt_path, 'w', encoding='utf-8') as tf:
                tf.write(srt.compose(translated_subs))
            logf.write(f"Wrote translated SRT: {translated_srt_path}\n")

            # 3) Burn subtitles with ffmpeg
            out_path = os.path.join(output_dir, f"subtitled_{tgt_code}.mp4")
            # Use subtitles filter with explicit char encoding; avoid shell quotes since we're not using a shell
            vf = f"subtitles={translated_srt_path}:charenc=UTF-8"
            cmd = ['ffmpeg', '-y', '-i', input_path, '-vf', vf, '-c:a', 'copy', out_path]
            logf.write(f"Running: {' '.join(shlex.quote(c) for c in cmd)}\n")
            proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
            RUNNING_PROCS[job_id] = proc
            for line in proc.stdout:
                logf.write(line)
                logf.flush()
            rc = proc.wait()
            RUNNING_PROCS.pop(job_id, None)
            # If canceled, stop without overriding status
            state = _load_job_state(job_id)
            if state.get('status') == 'canceled':
                return
            if rc != 0:
                _save_job_state(job_id, {
                    "status": "error",
                    "input": input_path,
                    "output_dir": output_dir,
                    "output_file": None,
                    "error": f"ffmpeg exited with code {rc}",
                    "model": model,
                    "language": src_code,
                    "target_language": tgt_code,
                })
                return
            rel_path = os.path.relpath(out_path, os.path.join(BASE_DIR, 'static'))
            _save_job_state(job_id, {
                "status": "done",
                "input": input_path,
                "output_dir": output_dir,
                "output_file": rel_path.replace('\\\\', '/').replace('\\', '/'),
                "error": None,
                "model": model,
                "language": src_code,
                "target_language": tgt_code,
            })
        except Exception as e:
            _save_job_state(job_id, {
                "status": "error",
                "input": input_path,
                "output_dir": output_dir,
                "output_file": None,
                "error": str(e),
                "model": model,
                "language": source_lang,
                "target_language": target_lang,
            })


@app.route('/')
def index():
    completed = list_completed_jobs()
    current_jobs = list_current_jobs()
    return render_template('index.html', completed_jobs=completed, current_jobs=current_jobs)


@app.route('/upload', methods=['POST'])
def upload():
    file = request.files.get('video')
    model = request.form.get('model') or None
    language = request.form.get('language') or None
    target_language = request.form.get('target_language') or ''
    translate = (target_language == 'en')
    if not file or file.filename == '':
        return render_template('index.html', error='Please select a file to upload.')

    filename = secure_filename(file.filename)
    if not allowed_file(filename):
        return render_template('index.html', error='Unsupported file format.')

    job_id = uuid.uuid4().hex
    input_path = os.path.join(UPLOAD_DIR, f"{job_id}_{filename}")
    file.save(input_path)

    output_dir = os.path.join(OUTPUT_BASE_DIR, job_id)

    # Queue job for sequential processing
    enqueue_job(job_id, input_path, output_dir, model, translate, language, target_language)

    # Persist selected languages and model in initial queued state
    state = _load_job_state(job_id)
    state.update({
        "language": language,
        "target_language": target_language,
        "model": model,
        "translate": translate,
    })
    _save_job_state(job_id, state)

    return redirect(url_for('job_status_page', job_id=job_id))


@app.route('/job/<job_id>')
def job_status_page(job_id):
    state = _load_job_state(job_id)
    return render_template('job.html', job_id=job_id, state=state)


@app.route('/api/job/<job_id>')
def job_status(job_id):
    state = _load_job_state(job_id)
    # If queued, compute current position dynamically
    if state.get('status') == 'queued':
        pos = None
        with WORKER_LOCK:
            for i, j in enumerate(JOB_QUEUE):
                if j['job_id'] == job_id:
                    pos = i + 1
                    break
        if pos is not None:
            state['queue_position'] = pos
    return jsonify(state)


@app.route('/play/<job_id>')
def play(job_id):
    state = _load_job_state(job_id)
    if state.get('status') != 'done' or not state.get('output_file'):
        return redirect(url_for('job_status_page', job_id=job_id))
    # Derive original filename from input path
    original_filename = None
    inp = state.get('input', '')
    if inp:
        base = os.path.basename(inp)
        idx = base.find('_')
        original_filename = base[idx+1:] if idx != -1 else base
    # Build share URL using local IP and configured port
    local_ip = get_local_ip()
    port = app.config.get('PORT', 8000)
    share_url = f"http://{local_ip}:{port}/play/{job_id}"
    return render_template('play.html', job_id=job_id, output_file=state['output_file'], original_filename=original_filename, share_url=share_url, model=state.get('model'), language=state.get('language'), target_language=state.get('target_language'))


@app.route('/static/<path:path>')
def static_files(path):
    return send_from_directory(os.path.join(BASE_DIR, 'static'), path)


def main():
    parser = argparse.ArgumentParser(description='Movie Subtitler Web UI')
    parser.add_argument('--host', default='0.0.0.0')
    parser.add_argument('--port', default=8000, type=int)
    args = parser.parse_args()
    app.config['HOST'] = args.host
    app.config['PORT'] = args.port
    app.run(host=args.host, port=args.port)


# Move the run guard below to ensure all routes are registered before app.run starts
# Moved __main__ guard to end of file to ensure routes are registered first
# if __name__ == '__main__':
#     main()


@app.route('/api/hardware')
def hardware_info():
    info = {"device": "cpu", "vram_gb": None, "recommendation": ""}
    try:
        if torch.cuda.is_available():
            info["device"] = "cuda"
            try:
                props = torch.cuda.get_device_properties(0)
                info["vram_gb"] = round(props.total_memory / (1024**3), 1)
            except Exception:
                info["vram_gb"] = None
        elif torch.backends.mps.is_available():
            info["device"] = "mps"
            info["vram_gb"] = None
    except Exception:
        pass
    vram = info["vram_gb"] or 0
    if info["device"] == 'cuda':
        if vram < 2:
            rec = 'Prefer tiny/base'
        elif vram < 6:
            rec = 'Prefer small'
        elif vram < 8:
            rec = 'Prefer medium/turbo'
        else:
            rec = 'large/turbo recommended'
    elif info["device"] == 'mps':
        rec = 'Apple Silicon: medium/turbo or small depending on workload'
    else:
        rec = 'CPU: tiny/base/small recommended for speed'
    info["recommendation"] = rec
    return jsonify(info)


@app.route('/api/job/<job_id>/cancel', methods=['POST'])
def cancel_job_endpoint(job_id):
    state = _load_job_state(job_id)
    removed = False
    with WORKER_LOCK:
        for j in list(JOB_QUEUE):
            if j.get('job_id') == job_id:
                try:
                    JOB_QUEUE.remove(j)
                except Exception:
                    pass
                else:
                    removed = True
                break
    if removed:
        state['status'] = 'canceled'
        state['queue_position'] = None
        _save_job_state(job_id, state)
        return jsonify({"ok": True, "status": "canceled"})
    # If currently processing, attempt to cancel
    if state.get('status') == 'processing':
        proc = RUNNING_PROCS.get(job_id)
        try:
            if proc:
                proc.terminate()
            # Mark as canceled; worker loop should respect this after termination or next check
            state['status'] = 'canceled'
            _save_job_state(job_id, state)
            RUNNING_PROCS.pop(job_id, None)
            return jsonify({"ok": True, "status": "canceled"})
        except Exception as e:
            return jsonify({"ok": False, "error": f"Failed to cancel active job: {str(e)}"}), 500
    # If not found in queue
    return jsonify({"ok": False, "error": "Job is not in queue or already finished"}), 404

# Ensure server starts only after all routes are defined
if __name__ == '__main__':
    main()