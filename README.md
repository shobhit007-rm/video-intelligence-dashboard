# Video Intelligence dashboard

A local, real-time dashboard that breaks down YouTube videos (hooks, pacing, cuts and narration) with free, open-source tools:
yt-dlp (download) → PySceneDetect (cuts) → WhisperX (transcript with word timings) → FFmpeg (frames) → a brief for Claude or any other AI model.

Everything runs on your own computer. The server only listens on `127.0.0.1` and nothing is uploaded anywhere.

## Requirements

- Windows 10 or 11 with [winget](https://learn.microsoft.com/windows/package-manager/winget/)
- About 5 GB of free disk space for the tools and Whisper models
- No GPU needed. Transcription runs on the CPU.

## Install

1. Download or clone this repository.
2. Double-click **Setup.bat**. It installs FFmpeg, yt-dlp, Deno and Python 3.12 with winget, then creates a `.venv` with the packages in `requirements.txt`.

To set it up by hand instead:

```bat
winget install --id Gyan.FFmpeg --exact
winget install --id yt-dlp.yt-dlp --exact
winget install --id DenoLand.Deno --exact
winget install --id Python.Python.3.12 --exact
py -3.12 -m venv .venv
.venv\Scripts\python.exe -m pip install -r requirements.txt
```

## Start it

Double-click **Start Dashboard.bat**. It opens http://127.0.0.1:8765 in your browser.
Keep the black window open while you use the dashboard, and close it to stop.

## Use it

1. Paste a YouTube link, pick a Whisper model and language, and click **Analyse video**.
   - `base` is fast and fine for English. Use `small` for Hindi or other languages.
   - Jobs run one at a time, and extra links wait in the queue.
2. Watch each stage run live. The CPU and RAM meters show your computer's load.
3. When it finishes you get:
   - the metrics
   - a video player synced to the scene timeline and transcript
   - pacing charts
   - the hook frames
   - every scene
4. Click **Copy Claude prompt** and paste it into Claude Code (or any assistant that can read local files) for a full timestamped breakdown.

## Where things are saved

Each video gets its own folder in `jobs\`. The folder is git-ignored, so downloaded videos never end up in the repository.

| File | What it is |
|---|---|
| `source.mp4`, `source.info.json`, `source.jpg` | The video (720p max), its YouTube details and thumbnail |
| `scenes.json` | Every cut found by PySceneDetect |
| `transcript.json` | WhisperX transcript with word timings |
| `frames\` | One frame per second for the first 30 s, plus one per scene (up to 240) |
| `analysis.json` | All metrics the dashboard shows |
| `claude_brief.md` | The brief for the AI model |
| `log.txt` | Full log of the run |

If a job is cancelled or the window is closed mid-run, **Run again** resumes it and skips the finished steps.
To remove a job, close the dashboard and delete its folder in `jobs\`.

## Limits

- Transcription runs on the CPU (WhisperX needs an NVIDIA GPU for acceleration), so a 20-minute video takes roughly 10–30 minutes.
- Frames are still images. Neither the dashboard nor an AI reading the brief can hear music, sound effects or tone of voice.
- Downloading YouTube videos may break YouTube's Terms of Service. Use the tool for private research only and don't redistribute the downloaded files.

## License

[MIT](LICENSE). The license covers this repository's code only. FFmpeg, yt-dlp, WhisperX and PySceneDetect keep their own licenses.
