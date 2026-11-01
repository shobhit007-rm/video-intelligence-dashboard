"""Job runner for the competitor-video pipeline.

Every job lives in jobs/<id>/ and walks through STAGES in order, one job at a time
(transcription is CPU-bound without a CUDA GPU). Job state is kept in memory for the
dashboard and mirrored to status.json, so finished jobs reappear after a restart and an
interrupted job can be run again without re-downloading or re-transcribing.
"""
import json
import math
import os
import queue
import re
import shutil
import statistics
import subprocess
import threading
import time
import uuid
from collections import deque
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
JOBS_DIR = ROOT / "jobs"
VENV_SCRIPTS = ROOT / ".venv" / "Scripts"
WINGET_LINKS = Path(os.environ.get("LOCALAPPDATA", "")) / "Microsoft" / "WinGet" / "Links"

STAGES = [
    ("download", "Download"),
    ("probe", "Probe"),
    ("scenes", "Scene detection"),
    ("audio", "Audio extract"),
    ("transcribe", "Transcription"),
    ("frames", "Frames"),
    ("report", "Report"),
]
# Rough share of wall-clock time per stage, used for the overall progress bar.
STAGE_WEIGHTS = {"download": 12, "probe": 1, "scenes": 18, "audio": 3, "transcribe": 52, "frames": 12, "report": 2}
MODELS = ("tiny", "base", "small", "medium")
LANGUAGES = ("auto", "en", "hi")
HOOK_SECONDS = 30
MAX_FRAMES = 240
FRAME_WIDTH = 768
YOUTUBE_URL = re.compile(r"^https?://(www\.|m\.)?(youtube\.com/(watch\?|shorts/|live/)|youtu\.be/)\S+$", re.I)
TQDM_BAR = re.compile(r"\d+%\|")
NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)


class Cancelled(Exception):
    pass


def tool(name):
    for folder in (VENV_SCRIPTS, WINGET_LINKS):
        candidate = folder / f"{name}.exe"
        if candidate.exists():
            return str(candidate)
    found = shutil.which(name)
    if not found:
        raise RuntimeError(f"{name} is not installed or not on PATH")
    return found


def tool_status():
    status = {}
    for name in ("yt-dlp", "deno", "ffmpeg", "ffprobe", "whisperx"):
        try:
            tool(name)
            status[name] = True
        except RuntimeError:
            status[name] = False
    return status


def fmt_clock(seconds):
    seconds = max(0, int(seconds or 0))
    h, rest = divmod(seconds, 3600)
    m, s = divmod(rest, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"


def plural(n, word):
    return f"{n} {word}" if n == 1 else f"{n} {word}s"


def new_stages():
    return {
        key: {"label": label, "status": "pending", "progress": 0, "started": None, "ended": None, "detail": ""}
        for key, label in STAGES
    }


def overall_progress(job):
    total = 0.0
    for key, weight in STAGE_WEIGHTS.items():
        stage = job["stages"][key]
        share = 100 if stage["status"] in ("done", "skipped") else stage["progress"] or 0
        total += weight * share / 100
    return round(total / sum(STAGE_WEIGHTS.values()) * 100, 1)


class JobManager:
    def __init__(self):
        JOBS_DIR.mkdir(exist_ok=True)
        self.lock = threading.RLock()
        self.jobs = {}
        self.logs = {}
        self.cancel_events = {}
        self.procs = {}
        self.scene_managers = {}
        self.queue = queue.Queue()
        for status_file in sorted(JOBS_DIR.glob("*/status.json")):
            try:
                job = json.loads(status_file.read_text(encoding="utf-8-sig"))
            except (OSError, ValueError):
                continue
            if job.get("state") in ("queued", "running"):
                job["state"] = "interrupted"
                job["error"] = "The dashboard was closed while this job was running. Use Run again to resume."
            self.logs[job["id"]] = deque(job.pop("log_tail", []), maxlen=300)
            self.jobs[job["id"]] = job
        threading.Thread(target=self._worker, daemon=True).start()

    # ---- public API used by the server ----

    def create(self, url, model="base", language="auto"):
        url = (url or "").strip()
        if not YOUTUBE_URL.match(url):
            raise ValueError("Paste a YouTube video link (youtube.com/watch, youtu.be or a Shorts link).")
        if model not in MODELS:
            raise ValueError(f"Model must be one of {', '.join(MODELS)}.")
        if language not in LANGUAGES:
            raise ValueError(f"Language must be one of {', '.join(LANGUAGES)}.")
        job_id = datetime.now().strftime("%Y%m%d-%H%M%S") + "-" + uuid.uuid4().hex[:4]
        job = {
            "id": job_id,
            "url": url,
            "options": {"model": model, "language": language},
            "created": time.time(),
            "started": None,
            "finished": None,
            "state": "queued",
            "stage": None,
            "stages": new_stages(),
            "meta": {},
            "error": None,
            "claude_prompt": None,
        }
        with self.lock:
            (JOBS_DIR / job_id).mkdir()
            self.jobs[job_id] = job
            self.logs[job_id] = deque(maxlen=300)
            self._save(job_id)
        self.queue.put(job_id)
        return job_id

    def retry(self, job_id):
        with self.lock:
            job = self.jobs[job_id]
            if job["state"] in ("queued", "running"):
                raise ValueError("This job is already queued or running.")
            job.update(state="queued", stage=None, stages=new_stages(), error=None, started=None, finished=None)
            self._save(job_id)
        self.queue.put(job_id)

    def cancel(self, job_id):
        with self.lock:
            job = self.jobs[job_id]
            if job["state"] == "queued":
                job.update(state="cancelled", finished=time.time())
                self._save(job_id)
                return
            if job["state"] != "running":
                return
            self.cancel_events[job_id].set()
            proc = self.procs.get(job_id)
            scene_manager = self.scene_managers.get(job_id)
        if proc and proc.poll() is None:
            # whisperx.exe is a launcher with a python child, so kill the whole tree.
            subprocess.run(["taskkill", "/PID", str(proc.pid), "/T", "/F"], capture_output=True, creationflags=NO_WINDOW)
        if scene_manager:
            scene_manager.stop()

    def summaries(self):
        with self.lock:
            jobs = sorted(self.jobs.values(), key=lambda j: j["created"], reverse=True)
            return [
                {
                    "id": j["id"],
                    "url": j["url"],
                    "state": j["state"],
                    "created": j["created"],
                    "title": j["meta"].get("title"),
                    "channel": j["meta"].get("channel"),
                    "thumbnail": j["meta"].get("thumbnail"),
                    "stage_label": j["stages"][j["stage"]]["label"] if j["stage"] else None,
                    "progress": overall_progress(j),
                }
                for j in jobs
            ]

    def detail(self, job_id):
        with self.lock:
            job = self.jobs[job_id]
            data = json.loads(json.dumps(job))
            data["stage_order"] = [key for key, _ in STAGES]
            data["progress"] = overall_progress(job)
            data["log"] = list(self.logs[job_id])[-150:]
            data["dir"] = str(JOBS_DIR / job_id)
            return data

    # ---- used by Runner ----

    def log(self, job_id, line):
        line = line.rstrip()
        if not line:
            return
        with self.lock:
            self.logs[job_id].append(line)
        try:
            with open(JOBS_DIR / job_id / "log.txt", "a", encoding="utf-8") as fh:
                fh.write(line + "\n")
        except OSError:
            pass

    def update_stage(self, job_id, key, save=True, **fields):
        with self.lock:
            job = self.jobs[job_id]
            job["stages"][key].update(fields)
            if fields.get("status") == "running":
                job["stage"] = key
            if save:
                self._save(job_id)

    def set_meta(self, job_id, **fields):
        with self.lock:
            self.jobs[job_id]["meta"].update(fields)
            self._save(job_id)

    def set_field(self, job_id, **fields):
        with self.lock:
            self.jobs[job_id].update(fields)
            self._save(job_id)

    def _save(self, job_id):
        job = self.jobs[job_id]
        data = dict(job, log_tail=list(self.logs.get(job_id, []))[-80:])
        path = JOBS_DIR / job_id / "status.json"
        tmp = path.with_suffix(".tmp")
        try:
            tmp.write_text(json.dumps(data, indent=1), encoding="utf-8")
            os.replace(tmp, path)
        except OSError:
            pass

    def _worker(self):
        while True:
            job_id = self.queue.get()
            with self.lock:
                job = self.jobs.get(job_id)
                if not job or job["state"] != "queued":
                    continue
                job.update(state="running", started=time.time())
                self.cancel_events[job_id] = threading.Event()
                self._save(job_id)
            try:
                Runner(self, job_id).run()
                state, error = "done", None
            except Cancelled:
                state, error = "cancelled", None
                self.log(job_id, "Cancelled.")
            except Exception as exc:  # surface every failure on the dashboard
                state, error = "failed", str(exc)
                self.log(job_id, f"ERROR: {exc}")
            with self.lock:
                for stage in job["stages"].values():
                    if stage["status"] == "running":
                        stage.update(status="failed" if state == "failed" else "cancelled", ended=time.time())
                job.update(state=state, error=error, finished=time.time(), stage=None)
                self.procs.pop(job_id, None)
                self.scene_managers.pop(job_id, None)
                self._save(job_id)


class Runner:
    def __init__(self, manager, job_id):
        self.m = manager
        self.id = job_id
        self.dir = JOBS_DIR / job_id
        self.job = manager.jobs[job_id]
        self.cancelled = manager.cancel_events[job_id]
        self.env = dict(os.environ, PYTHONUNBUFFERED="1", PYTHONIOENCODING="utf-8")
        self.env["PATH"] = os.pathsep.join([str(VENV_SCRIPTS), str(WINGET_LINKS), self.env.get("PATH", "")])
        self.source = None
        self.duration = 0.0
        self.has_audio = False
        self.scenes = []

    def run(self):
        for key, _ in STAGES:
            self.check()
            getattr(self, f"stage_{key}")()

    # ---- helpers ----

    def check(self):
        if self.cancelled.is_set():
            raise Cancelled()

    def begin(self, key, detail=""):
        self.check()
        self.m.update_stage(self.id, key, status="running", progress=0, started=time.time(), ended=None, detail=detail)

    def progress(self, key, pct, detail=None):
        fields = {"progress": round(max(0.0, min(100.0, pct)), 1)}
        if detail is not None:
            fields["detail"] = detail
        self.m.update_stage(self.id, key, save=False, **fields)

    def done(self, key, detail=None, status="done"):
        fields = {"status": status, "progress": 100, "ended": time.time()}
        if detail is not None:
            fields["detail"] = detail
        self.m.update_stage(self.id, key, **fields)

    def read_json(self, name, default):
        try:
            return json.loads((self.dir / name).read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return default

    def write_json(self, name, data):
        (self.dir / name).write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")

    def run_cmd(self, cmd, on_line=None, log_cmd=True):
        if log_cmd:
            self.m.log(self.id, "$ " + " ".join([Path(cmd[0]).stem] + [str(c) for c in cmd[1:]]))
        proc = subprocess.Popen(
            cmd, cwd=self.dir, env=self.env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True, encoding="utf-8", errors="replace", creationflags=NO_WINDOW,
        )
        with self.m.lock:
            self.m.procs[self.id] = proc
        tail = deque(maxlen=12)
        try:
            # Text mode turns the \r updates of progress bars into separate lines.
            for line in proc.stdout:
                line = line.rstrip()
                if not line:
                    continue
                consumed = on_line(line) if on_line else False
                if not consumed and not TQDM_BAR.search(line):
                    tail.append(line)
                    self.m.log(self.id, line)
            code = proc.wait()
        finally:
            with self.m.lock:
                self.m.procs.pop(self.id, None)
        self.check()
        if code != 0:
            detail = " | ".join(tail) or "no output"
            raise RuntimeError(f"{Path(cmd[0]).stem} exited with code {code}: {detail[-600:]}")

    def ffmpeg(self, key, args):
        total = max(self.duration, 0.001)

        def on_line(line):
            name, _, value = line.partition("=")
            if name in ("out_time_us", "out_time_ms"):
                try:
                    secs = int(value) / 1e6
                except ValueError:
                    return True
                self.progress(key, secs / total * 100, f"{fmt_clock(secs)} of {fmt_clock(total)}")
                return True
            return bool(value) and " " not in name  # other -progress key=value lines

        self.run_cmd([tool("ffmpeg"), "-y", "-hide_banner", "-loglevel", "error", "-nostats", "-progress", "pipe:1", *args], on_line)

    def find_source(self):
        for ext in ("mp4", "mkv", "webm"):
            candidate = self.dir / f"source.{ext}"
            if candidate.exists():
                return candidate
        return None

    def load_info(self):
        info = self.read_json("source.info.json", None)
        if not info:
            return
        upload = info.get("upload_date") or ""
        thumb = next((p.name for p in sorted(self.dir.glob("source.*")) if p.suffix.lower() in (".jpg", ".jpeg", ".png", ".webp")), None)
        self.m.set_meta(
            self.id,
            title=info.get("title"),
            channel=info.get("channel") or info.get("uploader"),
            channel_url=info.get("channel_url") or info.get("uploader_url"),
            channel_followers=info.get("channel_follower_count"),
            view_count=info.get("view_count"),
            like_count=info.get("like_count"),
            comment_count=info.get("comment_count"),
            upload_date=f"{upload[:4]}-{upload[4:6]}-{upload[6:]}" if len(upload) == 8 else None,
            duration=info.get("duration"),
            webpage_url=info.get("webpage_url") or self.job["url"],
            description=(info.get("description") or "")[:600],
            tags=(info.get("tags") or [])[:15],
            thumbnail=thumb,
        )

    # ---- stages ----

    def stage_download(self):
        self.begin("download", "Contacting YouTube")
        self.source = self.find_source()
        if self.source:
            self.load_info()
            self.done("download", "Already downloaded")
            return
        part = {"n": 0}

        def on_line(line):
            if line.startswith("[info] Writing video metadata as JSON"):
                threading.Timer(0.5, self.load_info).start()
            if line.startswith("[download] Destination:"):
                part["n"] += 1
                return True
            m = re.match(r"\[download\]\s+(\d+(?:\.\d+)?)% of\s+~?\s*(\S+)(?:.*? at\s+(\S+))?(?:.*? ETA\s+(\S+))?", line)
            if m:
                pct = float(m.group(1))
                n = max(part["n"], 1)
                overall = pct * 0.85 if n == 1 else 85 + pct * 0.15
                bits = [f"{'video' if n == 1 else 'audio'} {pct:.0f}% of {m.group(2)}"]
                if m.group(3) and "Unknown" not in m.group(3):
                    bits.append(m.group(3))
                if m.group(4) and "Unknown" not in m.group(4):
                    bits.append(f"ETA {m.group(4)}")
                self.progress("download", overall, " · ".join(bits))
                return True
            if line.startswith("[Merger]"):
                self.progress("download", 97, "Merging video and audio")
            return False

        ffmpeg_dir = str(Path(tool("ffmpeg")).resolve().parent)
        self.run_cmd([
            tool("yt-dlp"), "--no-playlist", "--newline", "--no-mtime", "--concurrent-fragments", "4",
            "-f", "bv*[height<=720][ext=mp4]+ba[ext=m4a]/b[height<=720][ext=mp4]/bv*[height<=720]+ba/b[height<=720]/b",
            "--merge-output-format", "mp4", "--ffmpeg-location", ffmpeg_dir,
            "--write-info-json", "--write-thumbnail", "--convert-thumbnails", "jpg",
            "-o", "source.%(ext)s", self.job["url"],
        ], on_line)
        self.source = self.find_source()
        if not self.source:
            raise RuntimeError("yt-dlp finished but no video file was saved.")
        self.load_info()
        self.done("download", f"{self.source.stat().st_size / 1e6:.0f} MB saved")

    def stage_probe(self):
        self.begin("probe", "Reading video details")
        proc = subprocess.run(
            [tool("ffprobe"), "-v", "error", "-print_format", "json", "-show_format", "-show_streams", str(self.source)],
            capture_output=True, text=True, encoding="utf-8", errors="replace", env=self.env, creationflags=NO_WINDOW,
        )
        if proc.returncode:
            raise RuntimeError(f"ffprobe failed: {proc.stderr.strip()[-300:]}")
        info = json.loads(proc.stdout)
        streams = info.get("streams", [])
        video = next((s for s in streams if s.get("codec_type") == "video"), None)
        if not video:
            raise RuntimeError("The download has no video stream.")
        self.has_audio = any(s.get("codec_type") == "audio" for s in streams)
        self.duration = float(info.get("format", {}).get("duration") or video.get("duration") or 0)
        num, _, den = (video.get("avg_frame_rate") or "0/1").partition("/")
        fps = float(num or 0) / float(den or 1) if float(den or 1) else 0.0
        width, height = video.get("width"), video.get("height")
        self.m.set_meta(self.id, video={
            "file": self.source.name, "duration": round(self.duration, 2), "width": width, "height": height,
            "fps": round(fps, 2), "has_audio": self.has_audio,
        })
        self.done("probe", f"{fmt_clock(self.duration)} · {width}×{height} · {fps:.0f} fps" + ("" if self.has_audio else " · no audio"))

    def stage_scenes(self):
        self.begin("scenes", "Looking for cuts")
        cached = self.read_json("scenes.json", None)
        if cached:
            self.scenes = cached
            self.done("scenes", f"{plural(len(cached), 'scene')} · {plural(len(cached) - 1, 'cut')} (reused)")
            return
        from scenedetect import ContentDetector, SceneManager, open_video

        video = open_video(str(self.source))
        scene_manager = SceneManager()
        scene_manager.add_detector(ContentDetector())
        with self.m.lock:
            self.m.scene_managers[self.id] = scene_manager
        errors = []

        def detect():
            try:
                scene_manager.detect_scenes(video=video)
            except Exception as exc:
                errors.append(exc)

        worker = threading.Thread(target=detect, daemon=True)
        worker.start()
        total = max(self.duration, 0.001)
        while worker.is_alive():
            worker.join(0.5)
            try:
                pos = video.position.get_seconds()
            except Exception:
                continue
            self.progress("scenes", pos / total * 100, f"{fmt_clock(pos)} of {fmt_clock(total)} scanned")
        with self.m.lock:
            self.m.scene_managers.pop(self.id, None)
        self.check()
        if errors:
            raise RuntimeError(f"Scene detection failed: {errors[0]}")
        scene_list = scene_manager.get_scene_list(start_in_scene=True)
        scenes = [
            {"index": i + 1, "start": round(start.get_seconds(), 3), "end": round(end.get_seconds(), 3)}
            for i, (start, end) in enumerate(scene_list)
        ] or [{"index": 1, "start": 0.0, "end": round(self.duration, 3)}]
        self.write_json("scenes.json", scenes)
        self.scenes = scenes
        self.done("scenes", f"{plural(len(scenes), 'scene')} · {plural(len(scenes) - 1, 'cut')}")

    def stage_audio(self):
        self.begin("audio", "Extracting speech track")
        if not self.has_audio:
            self.done("audio", "No audio track", status="skipped")
            return
        wav = self.dir / "audio.wav"
        if not wav.exists():
            partial = self.dir / "audio.partial.wav"
            self.ffmpeg("audio", ["-i", str(self.source), "-vn", "-ac", "1", "-ar", "16000", str(partial)])
            os.replace(partial, wav)
        self.done("audio", f"16 kHz mono · {wav.stat().st_size / 1e6:.0f} MB")

    def stage_transcribe(self):
        self.begin("transcribe", "Loading Whisper model")
        if not self.has_audio:
            self.done("transcribe", "No audio track", status="skipped")
            return
        out = self.dir / "transcript.json"
        if out.exists():
            data = self.read_json("transcript.json", {})
            self.done("transcribe", f"{len(data.get('segments', []))} lines · {data.get('language', '?')} (reused)")
            return
        opts = self.job["options"]
        cmd = [
            tool("whisperx"), "audio.wav", "--model", opts["model"], "--device", "cpu", "--compute_type", "int8",
            "--output_dir", str(self.dir), "--output_format", "json", "--print_progress", "True",
            "--threads", str(max(1, (os.cpu_count() or 2) - 1)),
        ]
        if opts["language"] != "auto":
            cmd += ["--language", opts["language"]]
        phase = {"name": "transcription", "noise": False}

        def on_line(line):
            low = line.lower()
            # pyannote prints a long, harmless torchcodec warning on Windows; keep it out of the log.
            if "pyannote" in low and "userwarning" in low or low.strip() == "warnings.warn(":
                return True
            if "torchcodec" in low and not phase["noise"]:
                phase["noise"] = True
                self.m.log(self.id, "(hid a torchcodec warning: harmless for transcription)")
                return True
            if phase["noise"]:
                if "end of libtorchcodec" in low:
                    phase["noise"] = False
                    return True
                if not ("progress" in low or "performing" in low or "detected language" in low):
                    return True
                phase["noise"] = False
            if "performing transcription" in low:
                phase["name"] = "transcription"
                self.progress("transcribe", 1, "Transcribing speech")
            elif "performing alignment" in low:
                phase["name"] = "alignment"
                self.progress("transcribe", 80, "Aligning word timings")
            elif "detected language" in low:
                self.progress("transcribe", 1, line.strip())
            elif TQDM_BAR.search(line):
                self.progress("transcribe", 0, "Downloading Whisper model (first run only)")
                return True
            m = re.search(r"progress:\s*(\d+(?:\.\d+)?)%", low)
            if m:
                pct = float(m.group(1))
                overall = pct * 0.8 if phase["name"] == "transcription" else 80 + pct * 0.2
                self.progress("transcribe", overall, f"{phase['name'].capitalize()} {pct:.0f}%")
                return True
            return False

        self.run_cmd(cmd, on_line)
        produced = self.dir / "audio.json"
        if not produced.exists():
            raise RuntimeError("WhisperX finished without writing a transcript.")
        os.replace(produced, out)
        data = self.read_json("transcript.json", {})
        self.done("transcribe", f"{len(data.get('segments', []))} lines · language {data.get('language', '?')}")

    def stage_frames(self):
        self.begin("frames", "Choosing frames")
        frames_dir = self.dir / "frames"
        frames_dir.mkdir(exist_ok=True)
        hook_end = min(HOOK_SECONDS, self.duration)
        times = {round(i + 0.5, 2): "hook" for i in range(int(hook_end))}
        budget = max(1, MAX_FRAMES - len(times))
        chosen = self.scenes
        if len(chosen) > budget:
            chosen = [self.scenes[round(k * (len(self.scenes) - 1) / max(1, budget - 1))] for k in range(budget)]
        for scene in chosen:
            if scene["end"] <= hook_end and any(scene["start"] <= t < scene["end"] for t in times):
                continue  # the per-second hook frames already show this scene
            times.setdefault(round((scene["start"] + scene["end"]) / 2, 2), "scene")
        ordered = sorted(t for t in times if t < self.duration)
        ffmpeg = tool("ffmpeg")
        frames = []
        for n, t in enumerate(ordered, 1):
            self.check()
            name = f"t{t:08.2f}.jpg"
            path = frames_dir / name
            if not path.exists():
                self.run_cmd([
                    ffmpeg, "-y", "-hide_banner", "-loglevel", "error", "-ss", f"{t:.2f}", "-i", str(self.source),
                    "-frames:v", "1", "-vf", f"scale=w='min({FRAME_WIDTH},iw)':h=-2", "-q:v", "4", str(path),
                ], log_cmd=False)
            if path.exists():
                frames.append({"t": t, "file": f"frames/{name}", "kind": times[t]})
            self.progress("frames", n / len(ordered) * 100, f"{n} of {len(ordered)} frames")
        self.write_json("frames.json", frames)
        hook_count = sum(1 for f in frames if f["kind"] == "hook")
        self.done("frames", f"{len(frames)} frames · {hook_count} in the hook")

    def stage_report(self):
        self.begin("report", "Combining results")
        transcript = self.read_json("transcript.json", {"segments": []})
        frames = self.read_json("frames.json", [])
        dur = self.duration
        segments, words = [], []
        for seg in transcript.get("segments", []):
            if seg.get("start") is None or seg.get("end") is None:
                continue
            segments.append({"start": round(seg["start"], 2), "end": round(seg["end"], 2), "text": (seg.get("text") or "").strip()})
            last = seg["start"]
            if seg.get("words"):
                for w in seg["words"]:
                    last = w.get("start", last)
                    words.append((last, (w.get("word") or "").strip()))
            else:
                words.extend((seg["start"], token) for token in (seg.get("text") or "").split())

        scenes = []
        for s in self.scenes:
            inside = [w for t, w in words if s["start"] <= t < s["end"]]
            mid = (s["start"] + s["end"]) / 2
            shown = [f for f in frames if s["start"] <= f["t"] < s["end"]]
            frame = min(shown, key=lambda f: abs(f["t"] - mid))["file"] if shown else None
            scenes.append(dict(s, length=round(s["end"] - s["start"], 2), text=" ".join(inside), frame=frame))

        lengths = [s["length"] for s in scenes] or [dur]
        cuts = max(0, len(scenes) - 1)
        speech = sum(max(0.0, s["end"] - s["start"]) for s in segments)
        gaps = []
        if segments:
            gaps = [segments[0]["start"]] + [b["start"] - a["end"] for a, b in zip(segments, segments[1:])] + [dur - segments[-1]["end"]]
        minutes = dur / 60 if dur else 0
        metrics = {
            "scene_count": len(scenes),
            "cuts": cuts,
            "cuts_per_min": round(cuts / minutes, 1) if minutes else 0,
            "avg_shot": round(statistics.mean(lengths), 2),
            "median_shot": round(statistics.median(lengths), 2),
            "min_shot": round(min(lengths), 2),
            "max_shot": round(max(lengths), 2),
            "words": len(words),
            "wpm": round(len(words) / minutes) if words and minutes else None,
            "speaking_wpm": round(len(words) / (speech / 60)) if words and speech else None,
            "speech_ratio": round(min(1.0, speech / dur), 3) if segments and dur else None,
            "longest_silence": round(max(gaps), 1) if gaps else None,
        }

        hook_end = min(HOOK_SECONDS, dur)
        hook_cuts = sum(1 for s in scenes[1:] if s["start"] < hook_end)
        hook_words = [(t, w) for t, w in words if t < hook_end]
        hook = {
            "seconds": round(hook_end),
            "cuts": hook_cuts,
            "avg_shot": round(hook_end / (hook_cuts + 1), 2) if hook_end else 0,
            "words": len(hook_words),
            "first_speech": round(min(t for t, _ in words), 2) if words else None,
            "text": " ".join(w for _, w in hook_words),
        }

        per_minute = []
        for m in range(max(1, math.ceil(dur / 60))):
            lo, hi = m * 60, min((m + 1) * 60, dur)
            span = max(hi - lo, 1e-6) / 60
            minute_cuts = sum(1 for s in scenes[1:] if lo <= s["start"] < hi)
            minute_words = sum(1 for t, _ in words if lo <= t < hi)
            per_minute.append({"minute": m, "cuts": minute_cuts, "words": minute_words,
                               "cuts_rate": round(minute_cuts / span, 1), "wpm": round(minute_words / span)})

        meta = self.job["meta"]
        analysis = {
            "job": self.id,
            "meta": {k: v for k, v in meta.items() if k != "video"},
            "video": meta.get("video", {}),
            "language": transcript.get("language"),
            "metrics": metrics,
            "hook": hook,
            "per_minute": per_minute,
            "scenes": scenes,
            "segments": segments,
            "frames": frames,
        }
        self.write_json("analysis.json", analysis)
        (self.dir / "claude_brief.md").write_text(self.brief(analysis), encoding="utf-8")
        prompt = (
            f'Analyse the competitor video in "{self.dir}". Read claude_brief.md there first, then look at the hook '
            "frames and the scene frames it lists. Give me a timestamp-by-timestamp breakdown covering the hook, "
            "pacing and editing, narration and story structure, visual style, retention devices and calls to action, "
            "and what our kids channel should copy or avoid. You can't hear music or tone of voice, so say where that "
            "limits the analysis."
        )
        self.m.set_field(self.id, claude_prompt=prompt)
        self.done("report", "analysis.json and claude_brief.md written")

    def brief(self, a):
        meta, video, k, h = a["meta"], a["video"], a["metrics"], a["hook"]

        def cell(text):
            return (text or "").replace("|", "\\|").replace("\n", " ")

        def num(n):
            return f"{n:,}" if isinstance(n, int) else "n/a"

        lines = [
            f"# Video brief: {meta.get('title') or self.job['url']}",
            "",
            f"- Channel: {meta.get('channel') or 'n/a'} ({num(meta.get('channel_followers'))} subscribers)",
            f"- Views: {num(meta.get('view_count'))} · Likes: {num(meta.get('like_count'))} · Comments: {num(meta.get('comment_count'))}",
            f"- Uploaded: {meta.get('upload_date') or 'n/a'} · URL: {meta.get('webpage_url') or self.job['url']}",
            f"- Duration: {fmt_clock(video.get('duration'))} · {video.get('width')}×{video.get('height')} at {video.get('fps')} fps · "
            f"speech language: {a.get('language') or 'none detected'}",
            f"- Job folder: {self.dir}",
            "",
            "## Measured metrics",
            "",
            "These come from the pipeline (PySceneDetect and WhisperX), not from estimates.",
            "",
            "| Metric | Value |",
            "|---|---|",
            f"| Scenes / cuts | {k['scene_count']} / {k['cuts']} |",
            f"| Cuts per minute | {k['cuts_per_min']} |",
            f"| Shot length avg / median / min / max (s) | {k['avg_shot']} / {k['median_shot']} / {k['min_shot']} / {k['max_shot']} |",
            f"| Words / words per minute | {k['words']} / {k['wpm'] if k['wpm'] is not None else 'n/a'} |",
            f"| Share of runtime with speech | {round(k['speech_ratio'] * 100) if k['speech_ratio'] is not None else 'n/a'}% |",
            f"| Longest stretch without speech (s) | {k['longest_silence'] if k['longest_silence'] is not None else 'n/a'} |",
            "",
            f"## Hook: first {h['seconds']} seconds",
            "",
            f"- Cuts: {h['cuts']} (average shot {h['avg_shot']} s) · first words at {h['first_speech'] if h['first_speech'] is not None else 'n/a'} s · {h['words']} words",
            f"- Narration: \"{h['text'] or '(no speech)'}\"",
            "- One frame per second, in order:",
        ]
        lines += [f"  - {f['t']:.1f}s: {f['file']}" for f in a["frames"] if f["kind"] == "hook"]
        lines += [
            "",
            "## Scene-by-scene timeline",
            "",
            "| # | Start | End | Length (s) | Frame | Narration during the scene |",
            "|---|---|---|---|---|---|",
        ]
        lines += [
            f"| {s['index']} | {fmt_clock(s['start'])} | {fmt_clock(s['end'])} | {s['length']} | {s['frame'] or '(not sampled)'} | {cell(s['text'])} |"
            for s in a["scenes"]
        ]
        lines += ["", "## Full transcript", ""]
        lines += [f"[{fmt_clock(s['start'])}] {s['text']}" for s in a["segments"]] or ["(no speech detected)"]
        lines += [
            "",
            "## How to analyse this video",
            "",
            "1. View the hook frames in order, then each scene's frame, reading the narration next to it.",
            "2. Break the video down by timestamp: hook, setup, main content, payoff and ending/CTA.",
            "3. Cover pacing and editing, narration style, story structure, visual style (colour, characters, text on screen),",
            "   retention devices (questions, reveals, repetition, songs) and calls to action.",
            "4. End with what a small kids channel should copy, adapt or avoid.",
            "",
            "Limits: frames are stills sampled from the video, so motion between frames is not visible. Music, sound effects and",
            "tone of voice are not available. Say so wherever a conclusion depends on them.",
        ]
        return "\n".join(lines) + "\n"
