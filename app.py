import os
import shutil
import threading
import urllib.request
import uuid
from pathlib import Path

from flask import Flask, jsonify, render_template, request, Response
import yt_dlp

app = Flask(__name__)

DOWNLOAD_DIR = Path("downloads")
DOWNLOAD_DIR.mkdir(exist_ok=True)

# Tracks active downloads: id -> { status, progress, filename, error }
jobs = {}

# Find ffmpeg — check PATH first, then winget location
def _find_ffmpeg() -> str:
    found = shutil.which("ffmpeg")
    if found:
        return str(Path(found).parent)
    winget_bin = Path(os.path.expandvars(
        r"%LOCALAPPDATA%\Microsoft\WinGet\Packages"
    ))
    for exe in winget_bin.rglob("ffmpeg.exe"):
        return str(exe.parent)
    return ""

FFMPEG_DIR = _find_ffmpeg()
FFMPEG_AVAILABLE = bool(FFMPEG_DIR)



def get_ydl_opts(job_id: str, output_format: str) -> dict:
    def progress_hook(d):
        if d["status"] == "downloading":
            total = d.get("total_bytes") or d.get("total_bytes_estimate") or 0
            downloaded = d.get("downloaded_bytes", 0)
            percent = int(downloaded / total * 100) if total else 0
            jobs[job_id]["progress"] = percent
            jobs[job_id]["speed"] = d.get("_speed_str", "")
            jobs[job_id]["eta"] = d.get("_eta_str", "")
            jobs[job_id]["status"] = "downloading"
        elif d["status"] == "finished":
            jobs[job_id]["progress"] = 100
            jobs[job_id]["status"] = "processing"
            jobs[job_id]["filename"] = Path(d["filename"]).name

    # Use only pre-merged single-file formats (no ffmpeg merge needed).
    # Instagram/Facebook already serve combined mp4. YouTube falls back to
    # best combined stream (up to ~720p) which is always available.
    fmt_map = {
        "best":  "best[ext=mp4]/best",
        "1080":  "best[height<=1080][ext=mp4]/best[height<=1080]/best",
        "720":   "best[height<=720][ext=mp4]/best[height<=720]/best",
        "480":   "best[height<=480][ext=mp4]/best[height<=480]/best",
        "audio": "bestaudio[ext=m4a]/bestaudio",
    }

    format_selector = fmt_map.get(output_format, fmt_map["best"])

    opts = {
        "format": format_selector,
        "outtmpl": str(DOWNLOAD_DIR / "%(title)s.%(ext)s"),
        "progress_hooks": [progress_hook],
        "quiet": True,
        "no_warnings": True,
        "postprocessors": [],
        "overwrites": True,
    }

    return opts


def run_download(job_id: str, url: str, fmt: str):
    try:
        opts = get_ydl_opts(job_id, fmt)
        with yt_dlp.YoutubeDL(opts) as ydl:
            ydl.download([url])
        jobs[job_id]["status"] = "done"
    except Exception as e:
        jobs[job_id]["status"] = "error"
        jobs[job_id]["error"] = str(e)


@app.route("/")
def index():
    return render_template("index.html", ffmpeg=FFMPEG_AVAILABLE)


@app.route("/api/thumb")
def proxy_thumb():
    """Proxy thumbnail images to avoid browser CORS/same-origin blocks."""
    url = request.args.get("url", "")
    if not url or not url.startswith("http"):
        return "", 400
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=8) as resp:
            data = resp.read()
            ct = resp.headers.get("Content-Type", "image/jpeg")
        return Response(data, content_type=ct)
    except Exception:
        return "", 502


@app.route("/api/info", methods=["POST"])
def get_info():
    url = request.json.get("url", "").strip()
    if not url:
        return jsonify({"error": "URL vazia"}), 400
    try:
        opts = {
            "quiet": True,
            "no_warnings": True,
            "skip_download": True,
        }
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(url, download=False)
        thumb = info.get("thumbnail", "")
        return jsonify({
            "title": info.get("title", "Sem título"),
            "thumbnail": f"/api/thumb?url={urllib.request.quote(thumb, safe='')}" if thumb else "",
            "duration": info.get("duration"),
            "uploader": info.get("uploader", ""),
            "platform": info.get("extractor_key", ""),
        })
    except Exception as e:
        return jsonify({"error": str(e)}), 400


@app.route("/api/download", methods=["POST"])
def start_download():
    url = request.json.get("url", "").strip()
    fmt = request.json.get("format", "best")
    if not url:
        return jsonify({"error": "URL vazia"}), 400

    job_id = str(uuid.uuid4())[:8]
    jobs[job_id] = {"status": "starting", "progress": 0, "speed": "", "eta": "", "filename": "", "error": ""}
    thread = threading.Thread(target=run_download, args=(job_id, url, fmt), daemon=True)
    thread.start()
    return jsonify({"job_id": job_id})


@app.route("/api/status/<job_id>")
def job_status(job_id):
    job = jobs.get(job_id)
    if not job:
        return jsonify({"error": "Job não encontrado"}), 404
    return jsonify(job)


@app.route("/api/downloads")
def list_downloads():
    files = []
    for f in sorted(DOWNLOAD_DIR.iterdir(), key=lambda x: x.stat().st_mtime, reverse=True):
        if f.is_file():
            files.append({
                "name": f.name,
                "size": f.stat().st_size,
                "mtime": f.stat().st_mtime,
            })
    return jsonify(files)



@app.route("/api/download-file/<path:filename>")
def download_file(filename):
    from flask import send_from_directory
    return send_from_directory(DOWNLOAD_DIR.resolve(), filename, as_attachment=True)


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    print("=" * 50)
    print("  Baixador de Videos - YouTube / Instagram / Facebook")
    print(f"  FFmpeg: {'OK' if FFMPEG_AVAILABLE else 'NAO ENCONTRADO (qualidade limitada)'}")
    print(f"  Acesse: http://localhost:{port}")
    print("=" * 50)
    app.run(debug=False, host="0.0.0.0", port=port)
