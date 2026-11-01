"""Local dashboard for the competitor-video pipeline.

Serves the dashboard page and a small JSON API on 127.0.0.1 only, so nothing is reachable
from other devices. Run it with the pipeline's own Python:

    .venv\\Scripts\\python.exe dashboard\\server.py [--port 8765] [--open]
"""
import argparse
import json
import os
import re
import shutil
import threading
import time
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import unquote, urlparse

import psutil

from pipeline import JOBS_DIR, LANGUAGES, MODELS, JobManager, tool_status

STATIC = Path(__file__).resolve().parent / "static"
JOB_ID = re.compile(r"^\d{8}-\d{6}-[0-9a-f]{4}$")
# Windows' registry-based mimetypes map .js to text/plain on some machines, so be explicit.
CONTENT_TYPES = {
    ".html": "text/html; charset=utf-8", ".css": "text/css; charset=utf-8", ".js": "text/javascript; charset=utf-8",
    ".json": "application/json; charset=utf-8", ".md": "text/markdown; charset=utf-8", ".txt": "text/plain; charset=utf-8",
    ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".png": "image/png", ".webp": "image/webp", ".svg": "image/svg+xml",
    ".mp4": "video/mp4", ".webm": "video/webm", ".mkv": "video/x-matroska", ".wav": "audio/wav",
}

manager = None
allowed_hosts = set()
tools_cache = {"at": 0.0, "value": {}}
cpu_now = {"percent": 0.0}


def sample_cpu():
    # psutil keeps cpu_percent() state per thread, and every request runs on a new thread,
    # so one long-lived thread does the sampling.
    while True:
        cpu_now["percent"] = psutil.cpu_percent(interval=1)


def inside(base, rel):
    base = base.resolve()
    path = (base / rel).resolve()
    return path if base in path.parents else None


def system_info():
    if time.time() - tools_cache["at"] > 30:
        tools_cache.update(at=time.time(), value=tool_status())
    memory = psutil.virtual_memory()
    disk = shutil.disk_usage(JOBS_DIR)
    return {
        "cpu": cpu_now["percent"],
        "cores": psutil.cpu_count(),
        "ram_used": memory.total - memory.available,
        "ram_total": memory.total,
        "ram_percent": memory.percent,
        "disk_free": disk.free,
        "disk_total": disk.total,
        "tools": tools_cache["value"],
        "models": MODELS,
        "languages": LANGUAGES,
        "jobs_dir": str(JOBS_DIR),
    }


class Handler(BaseHTTPRequestHandler):
    server_version = "VideoIntel/1.0"

    def log_message(self, fmt, *args):
        pass

    # ---- responses ----

    def send_json(self, obj, status=200):
        body = json.dumps(obj).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def error(self, status, message):
        self.send_json({"error": message}, status)

    def send_file(self, path, allow_range=False):
        if path is None or not path.is_file():
            return self.error(404, "Not found")
        size = path.stat().st_size
        start, end, status = 0, size - 1, 200
        header = self.headers.get("Range") if allow_range else None
        if header:
            m = re.fullmatch(r"bytes=(\d*)-(\d*)", header.strip())
            if m and (m.group(1) or m.group(2)):
                if m.group(1):
                    start = int(m.group(1))
                    end = min(int(m.group(2)), size - 1) if m.group(2) else size - 1
                else:
                    start = max(0, size - int(m.group(2)))
                if start > end:
                    self.send_response(416)
                    self.send_header("Content-Range", f"bytes */{size}")
                    self.end_headers()
                    return
                status = 206
        self.send_response(status)
        self.send_header("Content-Type", CONTENT_TYPES.get(path.suffix.lower(), "application/octet-stream"))
        self.send_header("Content-Length", str(end - start + 1))
        self.send_header("Accept-Ranges", "bytes")
        self.send_header("Cache-Control", "no-cache")
        if status == 206:
            self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
        self.end_headers()
        remaining = end - start + 1
        try:
            with open(path, "rb") as fh:
                fh.seek(start)
                while remaining > 0:
                    chunk = fh.read(min(256 * 1024, remaining))
                    if not chunk:
                        break
                    self.wfile.write(chunk)
                    remaining -= len(chunk)
        except (ConnectionAbortedError, ConnectionResetError, BrokenPipeError):
            pass  # the video player cancels range requests while seeking

    # ---- routing ----

    def host_ok(self):
        # Rejects DNS-rebinding requests that reach 127.0.0.1 under a foreign host name.
        return self.headers.get("Host", "") in allowed_hosts

    def do_GET(self):
        if not self.host_ok():
            return self.error(403, "Forbidden host")
        path = unquote(urlparse(self.path).path)
        if path == "/":
            return self.send_file(STATIC / "index.html")
        if path.startswith("/static/"):
            return self.send_file(inside(STATIC, path[len("/static/"):]))
        if path == "/api/jobs":
            return self.send_json({"jobs": manager.summaries()})
        if path == "/api/system":
            return self.send_json(system_info())
        m = re.fullmatch(r"/api/jobs/([^/]+)", path)
        if m:
            try:
                return self.send_json(manager.detail(m.group(1)))
            except KeyError:
                return self.error(404, "No such job")
        m = re.fullmatch(r"/files/([^/]+)/(.+)", path)
        if m and JOB_ID.match(m.group(1)):
            return self.send_file(inside(JOBS_DIR / m.group(1), m.group(2)), allow_range=True)
        self.error(404, "Not found")

    def do_POST(self):
        if not self.host_ok():
            return self.error(403, "Forbidden host")
        origin = self.headers.get("Origin")
        if origin and urlparse(origin).netloc not in allowed_hosts:
            return self.error(403, "Cross-site request refused")
        # Requiring JSON forces a CORS preflight for other sites, which this server never answers.
        if self.headers.get("Content-Type", "").split(";")[0].strip() != "application/json":
            return self.error(415, "Send JSON")
        length = int(self.headers.get("Content-Length") or 0)
        if length > 10_000:
            return self.error(413, "Request too large")
        try:
            body = json.loads(self.rfile.read(length) or b"{}")
        except ValueError:
            return self.error(400, "Invalid JSON")
        path = urlparse(self.path).path
        try:
            if path == "/api/jobs":
                job_id = manager.create(body.get("url"), body.get("model", "base"), body.get("language", "auto"))
                return self.send_json({"id": job_id}, 201)
            m = re.fullmatch(r"/api/jobs/([^/]+)/(cancel|retry|open)", path)
            if m and JOB_ID.match(m.group(1)):
                job_id, action = m.groups()
                if action == "cancel":
                    manager.cancel(job_id)
                elif action == "retry":
                    manager.retry(job_id)
                else:
                    manager.detail(job_id)  # raises KeyError for unknown jobs
                    os.startfile(JOBS_DIR / job_id)
                return self.send_json({"ok": True})
        except KeyError:
            return self.error(404, "No such job")
        except ValueError as exc:
            return self.error(400, str(exc))
        self.error(404, "Not found")


def main():
    global manager
    parser = argparse.ArgumentParser(description="Video Intelligence dashboard")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--open", action="store_true", help="open the dashboard in the default browser")
    args = parser.parse_args()
    allowed_hosts.update({f"127.0.0.1:{args.port}", f"localhost:{args.port}"})
    manager = JobManager()
    threading.Thread(target=sample_cpu, daemon=True).start()
    server = ThreadingHTTPServer(("127.0.0.1", args.port), Handler)
    server.daemon_threads = True
    url = f"http://127.0.0.1:{args.port}/"
    print(f"Video Intelligence dashboard running at {url}  (Ctrl+C to stop)", flush=True)
    if args.open:
        webbrowser.open(url)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
