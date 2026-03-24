import os
import threading
import uuid
import zipfile
from datetime import datetime
from flask import Flask, request, jsonify, send_file, after_this_request
from flask_cors import CORS
import yt_dlp

app = Flask(__name__)
CORS(app)

DOWNLOAD_DIR = "/tmp/downloads"
os.makedirs(DOWNLOAD_DIR, exist_ok=True)

jobs = {}

def run_download(job_id, url, fmt, quality):
    jobs[job_id] = {
        "status": "downloading",
        "progress": 0,
        "filename": None,
        "zip": False,
        "error": None,
        "title": "",
        "type": "video",
        "done_videos": 0,
        "total_videos": 0,
    }

    is_audio = fmt == "mp3"
    job_dir = os.path.join(DOWNLOAD_DIR, job_id)
    os.makedirs(job_dir, exist_ok=True)

    try:
        with yt_dlp.YoutubeDL({"quiet": True, "skip_download": True, "noplaylist": False}) as ydl:
            meta = ydl.extract_info(url, download=False)
    except Exception as e:
        jobs[job_id]["status"] = "error"
        jobs[job_id]["error"] = str(e)
        return

    is_playlist = meta.get("_type") == "playlist"
    entries = [e for e in (meta.get("entries") or []) if e] if is_playlist else [meta]
    total = len(entries)
    jobs[job_id]["total_videos"] = total
    jobs[job_id]["title"] = meta.get("title", "download")
    jobs[job_id]["type"] = "playlist" if is_playlist else "video"

    completed = [0]

    def progress_hook(d):
        if d["status"] == "downloading":
            total_b = d.get("total_bytes") or d.get("total_bytes_estimate", 1)
            done_b = d.get("downloaded_bytes", 0)
            file_pct = (done_b / total_b) if total_b else 0
            overall = int(((completed[0] + file_pct) / total) * 100) if total else 0
            jobs[job_id]["progress"] = overall
        elif d["status"] == "finished":
            completed[0] += 1
            jobs[job_id]["done_videos"] = completed[0]

    outtmpl = os.path.join(job_dir, "%(title)s.%(ext)s")

    if is_audio:
        ydl_opts = {
            "format": "bestaudio/best",
            "postprocessors": [{"key": "FFmpegExtractAudio", "preferredcodec": "mp3", "preferredquality": "192"}],
            "outtmpl": outtmpl,
            "progress_hooks": [progress_hook],
            "quiet": True,
            "noplaylist": False,
        }
    else:
        height = quality.replace("p", "") if quality else "720"
        ydl_opts = {
            "format": f"bestvideo[height<={height}]+bestaudio/best[height<={height}]/best",
            "outtmpl": outtmpl,
            "merge_output_format": "mp4",
            "progress_hooks": [progress_hook],
            "quiet": True,
            "noplaylist": False,
        }

    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            ydl.download([url])

        downloaded_files = os.listdir(job_dir)

        if is_playlist and len(downloaded_files) > 1:
            jobs[job_id]["status"] = "zipping"
            safe_title = "".join(c for c in meta.get("title", "playlist") if c.isalnum() or c in " _-")[:50]
            zip_name = f"{job_id}_{safe_title}.zip"
            zip_path = os.path.join(DOWNLOAD_DIR, zip_name)

            with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
                for fname in sorted(downloaded_files):
                    fpath = os.path.join(job_dir, fname)
                    zf.write(fpath, arcname=fname)

            jobs[job_id]["filename"] = zip_name
            jobs[job_id]["zip"] = True

            for fname in downloaded_files:
                try:
                    os.remove(os.path.join(job_dir, fname))
                except:
                    pass
            try:
                os.rmdir(job_dir)
            except:
                pass
        else:
            if downloaded_files:
                fname = downloaded_files[0]
                src = os.path.join(job_dir, fname)
                safe_name = f"{job_id}_{fname}"
                dst = os.path.join(DOWNLOAD_DIR, safe_name)
                os.rename(src, dst)
                jobs[job_id]["filename"] = safe_name
                jobs[job_id]["zip"] = False
            try:
                os.rmdir(job_dir)
            except:
                pass

        jobs[job_id]["status"] = "done"
        jobs[job_id]["progress"] = 100

    except Exception as e:
        jobs[job_id]["status"] = "error"
        jobs[job_id]["error"] = str(e)


@app.route("/api/info", methods=["POST"])
def get_info():
    data = request.get_json()
    url = (data or {}).get("url", "").strip()
    if not url:
        return jsonify({"error": "URL required"}), 400
    try:
        with yt_dlp.YoutubeDL({"quiet": True, "skip_download": True, "noplaylist": False}) as ydl:
            info = ydl.extract_info(url, download=False)
        if info.get("_type") == "playlist":
            entries = [e for e in (info.get("entries") or []) if e]
            return jsonify({
                "type": "playlist",
                "title": info.get("title", "Playlist"),
                "count": len(entries),
                "thumbnail": entries[0].get("thumbnail") if entries else None,
                "uploader": info.get("uploader", ""),
                "videos": [{"title": e.get("title", ""), "duration": e.get("duration", 0)} for e in entries[:20]],
            })
        else:
            formats = info.get("formats", [])
            qualities = sorted(set(f.get("height") for f in formats if f.get("height")), reverse=True)
            return jsonify({
                "type": "video",
                "title": info.get("title", ""),
                "thumbnail": info.get("thumbnail", ""),
                "duration": info.get("duration", 0),
                "uploader": info.get("uploader", ""),
                "view_count": info.get("view_count", 0),
                "qualities": [f"{q}p" for q in qualities[:6]],
            })
    except Exception as e:
        return jsonify({"error": str(e)}), 400


@app.route("/api/download", methods=["POST"])
def start_download():
    data = request.get_json()
    url = (data or {}).get("url", "").strip()
    fmt = (data or {}).get("format", "mp4")
    quality = (data or {}).get("quality", "720p")
    if not url:
        return jsonify({"error": "URL required"}), 400
    job_id = str(uuid.uuid4())[:8]
    t = threading.Thread(target=run_download, args=(job_id, url, fmt, quality))
    t.daemon = True
    t.start()
    return jsonify({"job_id": job_id})


@app.route("/api/progress/<job_id>")
def get_progress(job_id):
    job = jobs.get(job_id)
    if not job:
        return jsonify({"error": "Job not found"}), 404
    return jsonify(job)


@app.route("/api/file/<job_id>")
def download_file(job_id):
    job = jobs.get(job_id)
    if not job or job["status"] != "done":
        return jsonify({"error": "File not ready"}), 404
    filepath = os.path.join(DOWNLOAD_DIR, job["filename"])
    if not os.path.exists(filepath):
        return jsonify({"error": "File missing"}), 404

    @after_this_request
    def cleanup(response):
        try:
            os.remove(filepath)
            del jobs[job_id]
        except:
            pass
        return response

    clean_name = job["filename"].split("_", 1)[-1] if "_" in job["filename"] else job["filename"]
    return send_file(filepath, as_attachment=True, download_name=clean_name)


@app.route("/health")
def health():
    return jsonify({"status": "ok"})


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
