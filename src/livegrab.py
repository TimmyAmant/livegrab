"""
LiveGrab for OBS Studio  (v1.6.1)
-----------------------------------
Pull someone's LIVE stream (Twitch / Kick / YouTube / anything Streamlink
supports) into OBS as a Media Source, keep a rolling buffer of it, and save
clips with one hotkey while they're still live.

Two ways to clip (use either or both):
  1. Built-in buffer (default): the script keeps the last N seconds of the
     stream on disk and cuts a clip on hotkey. No re-encoding, original
     quality, and it works even while you're streaming your own gameplay and
     the grabbed stream isn't on screen.
  2. OBS Replay Buffer: saves your whole OBS canvas (overlays, facecam, etc.).
     The script can start the Replay Buffer for you and rename the saved files
     with the channel name.

Optional: auto-export a 9:16 vertical copy of every clip for TikTok / Shorts /
Reels (NVENC if available, CPU fallback).

Requires: Streamlink (Windows installer bundles FFmpeg) and OBS Python set up.
Only uses the Python standard library.
"""

import os
import re
import sys
import glob
import math
import time
import shutil
import tempfile
import datetime
import threading
import subprocess

try:
    import obspython as obs
except ImportError:  # lets the engine be imported/tested outside OBS
    obs = None

VERSION = "1.6.1"
UPDATE_REPO = "TimmyAmant/livegrab"   # GitHub repo that hosts LiveGrab releases
IS_WIN = os.name == "nt"
NO_WINDOW = 0x08000000 if IS_WIN else 0  # CREATE_NO_WINDOW: no console popups


LOG_LINES = []


def log(msg):
    LOG_LINES.append(time.strftime("%H:%M:%S") + "  " + msg)
    del LOG_LINES[:-30]
    print("[LiveGrab] " + msg)
    sys.stdout.flush()


# =============================================================================
# Helpers (no OBS dependency)
# =============================================================================

def _expand(p):
    return os.path.expandvars(os.path.expanduser(p))


STREAMLINK_CANDIDATES = [
    r"C:\Program Files\Streamlink\bin\streamlink.exe",
    r"C:\Program Files (x86)\Streamlink\bin\streamlink.exe",
    r"%LOCALAPPDATA%\Programs\Streamlink\bin\streamlink.exe",
    "/opt/homebrew/bin/streamlink",
    "/usr/local/bin/streamlink",
    "/usr/bin/streamlink",
]
FFMPEG_CANDIDATES = [
    r"C:\Program Files\Streamlink\ffmpeg\ffmpeg.exe",
    r"C:\Program Files (x86)\Streamlink\ffmpeg\ffmpeg.exe",
    r"%LOCALAPPDATA%\Programs\Streamlink\ffmpeg\ffmpeg.exe",
    r"%LOCALAPPDATA%\Microsoft\WinGet\Links\ffmpeg.exe",
    r"C:\ffmpeg\bin\ffmpeg.exe",
    "/opt/homebrew/bin/ffmpeg",
    "/usr/local/bin/ffmpeg",
    "/usr/bin/ffmpeg",
]


def find_exe(user_path, name, candidates):
    if user_path and os.path.isfile(_expand(user_path)):
        return _expand(user_path)
    found = shutil.which(name)
    if found:
        return found
    for c in candidates:
        c = _expand(c)
        if os.path.isfile(c):
            return c
    return None


def normalize_url(text):
    """'xqc' -> https://twitch.tv/xqc ; 'kick.com/x' -> https://kick.com/x"""
    t = (text or "").strip()
    if not t:
        return ""
    if "://" in t:
        return t
    if "." not in t.split("/")[0]:
        return "https://www.twitch.tv/" + t.lstrip("@")
    return "https://" + t


def channel_name(url):
    u = normalize_url(url)
    path = re.sub(r"^[a-z]+://[^/]+", "", u).split("?")[0].strip("/")
    parts = [p for p in path.split("/") if p and p.lower() not in ("live", "c", "channel", "user", "watch", "videos")]
    name = parts[0] if parts else re.sub(r"^[a-z]+://(www\.)?", "", u).split("/")[0]
    name = name.lstrip("@")
    return re.sub(r"[^A-Za-z0-9_.-]+", "_", name)[:40] or "stream"


def timestamp():
    return datetime.datetime.now().strftime("%Y-%m-%d_%H-%M-%S")


def unique_path(path):
    if not os.path.exists(path):
        return path
    base, ext = os.path.splitext(path)
    i = 2
    while os.path.exists("%s_%d%s" % (base, i, ext)):
        i += 1
    return "%s_%d%s" % (base, i, ext)


def run(cmd, timeout=None):
    return subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                          stdin=subprocess.DEVNULL, timeout=timeout,
                          creationflags=NO_WINDOW)


def open_folder(path):
    try:
        os.makedirs(path, exist_ok=True)
        if IS_WIN:
            os.startfile(path)  # noqa
        elif sys.platform == "darwin":
            subprocess.Popen(["open", path])
        else:
            subprocess.Popen(["xdg-open", path])
    except Exception as e:
        log("Could not open folder: %s" % e)


# =============================================================================
# Vertical (9:16) export
# =============================================================================

def media_duration(ffmpeg, path):
    """Seconds of media in `path` (reads ffmpeg's header info), or None."""
    try:
        r = run([ffmpeg, "-hide_banner", "-i", path], timeout=30)
        m = re.search(r"Duration:\s*(\d+):(\d+):(\d+(?:\.\d+)?)", r.stderr.decode("utf-8", "replace"))
        if m:
            return int(m.group(1)) * 3600 + int(m.group(2)) * 60 + float(m.group(3))
    except Exception:
        pass
    return None


def make_vertical(ffmpeg, src, mode="blur", on_progress=None):
    """Writes <src>_vertical.mp4. mode: 'blur' (fit + blurred background) or 'crop' (center crop).
    on_progress(done_seconds) is called while encoding."""
    base, _ = os.path.splitext(src)
    dst = unique_path(base + "_vertical.mp4")
    if mode == "crop":
        vf = ["-vf", "scale=-2:1920,crop=1080:1920,setsar=1"]
    else:
        vf = ["-filter_complex",
              "[0:v]split=2[bg][fg];"
              "[bg]scale=1080:1920:force_original_aspect_ratio=increase,crop=1080:1920,boxblur=20:2[b];"
              "[fg]scale=1080:-2[f];[b][f]overlay=(W-w)/2:(H-h)/2,setsar=1[v]",
              "-map", "[v]", "-map", "0:a?"]
    encoders = [
        ["-c:v", "h264_nvenc", "-preset", "p5", "-rc", "vbr", "-cq", "21", "-b:v", "0"],
        ["-c:v", "libx264", "-preset", "veryfast", "-crf", "20"],
    ]
    for enc in encoders:
        cmd = [ffmpeg, "-hide_banner", "-loglevel", "error", "-nostats", "-progress", "pipe:1", "-y", "-i", src] + vf + enc + \
              ["-pix_fmt", "yuv420p", "-c:a", "aac", "-b:a", "160k", "-movflags", "+faststart", dst]
        try:
            p = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                                 stdin=subprocess.DEVNULL, creationflags=NO_WINDOW)
            for line in iter(p.stdout.readline, b""):
                line = line.decode("ascii", "ignore").strip()
                if line.startswith("out_time_us=") and on_progress:
                    try:
                        on_progress(max(0, int(line.split("=", 1)[1])) / 1e6)
                    except ValueError:
                        pass
            p.wait(timeout=600)
            if p.returncode == 0 and os.path.getsize(dst) > 0:
                return dst
        except Exception:
            pass
        if on_progress:
            on_progress(0)
    log("Vertical export failed for %s" % os.path.basename(src))
    return None


# =============================================================================
# Grabber: streamlink -> ffmpeg -> (UDP for OBS preview) + (rolling segment buffer)
# =============================================================================

class Grabber(object):
    def __init__(self):
        self.url = ""
        self.quality = "best"
        self.low_latency = True
        self.port = 5577
        self.buffer_seconds = 120
        self.seg_time = 2
        self.streamlink = None
        self.ffmpeg = None
        self.extra_args = ""
        self.buffer_dir = os.path.join(tempfile.gettempdir(), "obs-livegrab")

        self.want_running = False
        self.status = "stopped"
        self.live_since = None
        self.on_pipeline_started = None   # callback, called from worker thread
        self._sl = None
        self._ff = None
        self._thread = None
        self._lock = threading.Lock()
        self._sl_err = []

    # ---- paths ----
    @property
    def seg_dir(self):
        return os.path.join(self.buffer_dir, "port%d" % self.port)

    def seg_pattern(self):
        return os.path.join(self.seg_dir, "seg%04d.ts").replace("\\", "/")

    def obs_input_url(self):
        return "udp://127.0.0.1:%d?overrun_nonfatal=1&fifo_size=50000000" % self.port

    # ---- control ----
    def start(self):
        if self.want_running:
            return
        if not self.url:
            log("No channel set. Enter a channel/URL first.")
            return
        if not self.streamlink or not self.ffmpeg:
            log("Streamlink or FFmpeg not found. Install Streamlink (winget install streamlink) or set the paths.")
            return
        self.want_running = True
        self._thread = threading.Thread(target=self._supervise, name="livegrab", daemon=True)
        self._thread.start()

    def stop(self):
        self.want_running = False
        self._kill()
        self.status = "stopped"
        self.live_since = None

    @property
    def running(self):
        return self.want_running

    # ---- internals ----
    def _kill(self):
        with self._lock:
            for p in (self._ff, self._sl):
                if p and p.poll() is None:
                    try:
                        p.terminate()
                    except Exception:
                        pass
            for p in (self._ff, self._sl):
                if p:
                    try:
                        p.wait(timeout=3)
                    except Exception:
                        try:
                            p.kill()
                        except Exception:
                            pass
            self._ff = self._sl = None

    def _clear_buffer(self):
        os.makedirs(self.seg_dir, exist_ok=True)
        for f in glob.glob(os.path.join(self.seg_dir, "seg*.ts")):
            try:
                os.remove(f)
            except Exception:
                pass

    def streamlink_cmd(self):
        cmd = [self.streamlink, "--stdout", "--loglevel", "warning",
               "--hls-live-edge", "2", "--stream-segment-threads", "2"]
        host = normalize_url(self.url).lower()
        if self.low_latency and "twitch.tv" in host:
            cmd.append("--twitch-low-latency")
        if self.low_latency and "kick.com" in host:
            cmd.append("--kick-low-latency")
        if self.extra_args.strip():
            cmd += self.extra_args.split()
        cmd += [normalize_url(self.url), self.quality or "best"]
        return cmd

    def ffmpeg_cmd(self, input_arg="pipe:0"):
        wrap = int(math.ceil(float(self.buffer_seconds) / self.seg_time)) + 8
        return [self.ffmpeg, "-hide_banner", "-loglevel", "error", "-nostdin",
                "-fflags", "+genpts+discardcorrupt", "-i", input_arg,
                # output 1: live feed for the OBS Media Source
                "-map", "0:v?", "-map", "0:a?", "-c", "copy",
                "-f", "mpegts", "udp://127.0.0.1:%d?pkt_size=1316" % self.port,
                # output 2: rolling buffer on disk (overwrites itself)
                "-map", "0:v?", "-map", "0:a?", "-c", "copy",
                "-f", "segment", "-segment_time", str(self.seg_time),
                "-segment_wrap", str(wrap), "-segment_format", "mpegts",
                self.seg_pattern()]

    def _drain(self, pipe, sink):
        try:
            for line in iter(pipe.readline, b""):
                s = line.decode("utf-8", "replace").strip()
                if s:
                    sink.append(s)
                    del sink[:-20]
        except Exception:
            pass

    def _supervise(self):
        backoff = 3
        while self.want_running:
            self._clear_buffer()
            self._sl_err = []
            ff_err = []
            try:
                with self._lock:
                    self._sl = subprocess.Popen(self.streamlink_cmd(), stdout=subprocess.PIPE,
                                                stderr=subprocess.PIPE, stdin=subprocess.DEVNULL,
                                                creationflags=NO_WINDOW)
                    self._ff = subprocess.Popen(self.ffmpeg_cmd(), stdin=self._sl.stdout,
                                                stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
                                                creationflags=NO_WINDOW)
                    self._sl.stdout.close()  # ffmpeg owns it now
                    sl, ff = self._sl, self._ff
            except Exception as e:
                log("Failed to launch: %s" % e)
                self.status = "error"
                self.want_running = False
                return

            threading.Thread(target=self._drain, args=(sl.stderr, self._sl_err), daemon=True).start()
            threading.Thread(target=self._drain, args=(ff.stderr, ff_err), daemon=True).start()

            self.status = "connecting"
            log("Connecting to %s (%s)..." % (normalize_url(self.url), self.quality))
            started_at = time.time()
            announced = False
            while self.want_running and ff.poll() is None:
                if not announced and glob.glob(os.path.join(self.seg_dir, "seg*.ts")):
                    announced = True
                    self.status = "live"
                    self.live_since = time.time()
                    backoff = 3
                    log("LIVE: receiving %s. Clip hotkey is ready." % channel_name(self.url))
                    if self.on_pipeline_started:
                        try:
                            self.on_pipeline_started()
                        except Exception as e:
                            log("callback error: %s" % e)
                time.sleep(0.5)

            if not self.want_running:
                break

            self.live_since = None
            err = " | ".join(self._sl_err[-3:] + ff_err[-2:])
            self._kill()
            offline = ("No playable streams" in err or "offline" in err.lower()
                       or "No plugin" in err)
            if "No plugin can handle" in err:
                log("Streamlink doesn't support that URL: %s" % self.url)
                self.status = "error"
                self.want_running = False
                break
            if offline:
                self.status = "offline"
                wait = 30
                log("Channel looks offline. Checking again in %ds." % wait)
            else:
                self.status = "reconnecting"
                wait = backoff
                backoff = min(backoff * 2, 30)
                ran = int(time.time() - started_at)
                log("Stream dropped after %ds%s. Reconnecting in %ds."
                    % (ran, (" (" + err[:300] + ")") if err else "", wait))
            for _ in range(wait * 2):
                if not self.want_running:
                    break
                time.sleep(0.5)
        self.status = "stopped"

    # ---- clipping from the rolling buffer ----
    def segments(self):
        files = glob.glob(os.path.join(self.seg_dir, "seg*.ts"))
        files = [f for f in files if os.path.getsize(f) > 0]
        return sorted(files, key=os.path.getmtime)

    def save_clip(self, seconds, out_dir, name_hint=None):
        """Cut the last `seconds` of the buffer into an MP4. Returns the path or None."""
        segs = self.segments()
        if not segs:
            log("Nothing buffered yet. Is the grab running and the channel live?")
            return None
        now = time.time()
        cutoff = now - float(seconds)
        pick = [f for f in segs if os.path.getmtime(f) >= cutoff]
        earlier = [f for f in segs if os.path.getmtime(f) < cutoff]
        if earlier:
            pick.insert(0, earlier[-1])  # segment that contains the start point
        if not pick:
            pick = segs[-1:]

        os.makedirs(out_dir, exist_ok=True)
        name = name_hint or channel_name(self.url)
        out = unique_path(os.path.join(out_dir, "%s_%s.mp4" % (name, timestamp())))
        tmp = out + ".part.ts"
        try:
            with open(tmp, "wb") as w:
                for f in pick:
                    with open(f, "rb") as r:
                        shutil.copyfileobj(r, w)
            cmd = [self.ffmpeg, "-hide_banner", "-loglevel", "error", "-y",
                   "-fflags", "+genpts+discardcorrupt", "-i", tmp,
                   "-map", "0:v?", "-map", "0:a?", "-c", "copy",
                   "-movflags", "+faststart", out]
            r = run(cmd, timeout=180)
            if r.returncode != 0 or not os.path.exists(out) or os.path.getsize(out) == 0:
                log("Clip failed: %s" % r.stderr.decode("utf-8", "replace")[-400:])
                return None
            return out
        finally:
            try:
                os.remove(tmp)
            except Exception:
                pass


# =============================================================================
# Updater: checks GitHub releases, downloads the new installer, installs it
# silently right after OBS closes, then reopens OBS.
# =============================================================================
import json as _json
import hashlib
import urllib.request


def _ver_tuple(v):
    nums = re.findall(r"\d+", str(v))
    return tuple(int(n) for n in nums[:3]) + (0,) * (3 - len(nums[:3]))


def _obs_exe():
    cands = []
    if IS_WIN:
        try:
            import winreg
            for hive in (winreg.HKEY_LOCAL_MACHINE, winreg.HKEY_CURRENT_USER):
                try:
                    with winreg.OpenKey(hive, r"SOFTWARE\OBS Studio") as k:
                        cands.append(os.path.join(winreg.QueryValue(k, None), "bin", "64bit", "obs64.exe"))
                except OSError:
                    pass
        except Exception:
            pass
        cands.append(os.path.join(os.environ.get("ProgramFiles", r"C:\Program Files"), "obs-studio", "bin", "64bit", "obs64.exe"))
    if os.path.basename(sys.executable).lower().startswith("obs"):
        cands.insert(0, sys.executable)
    for c in cands:
        if c and os.path.isfile(c):
            return c
    return ""


APPLY_PS = r"""
param([string]$Installer, [string]$Obs, [string]$Marker)
while (Get-Process -Name obs64 -ErrorAction SilentlyContinue) { Start-Sleep -Seconds 2 }
if (-not (Test-Path $Marker)) { exit 0 }   # update was cancelled
Start-Sleep -Seconds 2
$p = Start-Process -FilePath $Installer -ArgumentList "/S" -Wait -PassThru
Remove-Item $Marker -ErrorAction SilentlyContinue
if ($Obs -and (Test-Path $Obs)) { Start-Process -FilePath $Obs -WorkingDirectory (Split-Path $Obs) }
"""


class Updater(object):
    def __init__(self):
        self.state = "idle"
        self.latest = ""
        self.notes = ""
        self.pct = 0.0
        self.msg = ""
        self.asset_url = ""
        self.sha_url = ""
        self.file = ""
        self._busy = False

    @property
    def dir(self):
        base = os.environ.get("LOCALAPPDATA") or tempfile.gettempdir()
        return os.path.join(base, "LiveGrab", "updates")

    @property
    def marker(self):
        return os.path.join(self.dir, "pending.txt")

    def payload(self):
        return {"state": self.state, "latest": self.latest, "notes": self.notes[:1500],
                "pct": round(self.pct, 1), "msg": self.msg}

    def _get(self, url, timeout=20):
        req = urllib.request.Request(url, headers={"User-Agent": "LiveGrab/" + VERSION,
                                                   "Accept": "application/vnd.github+json"})
        return urllib.request.urlopen(req, timeout=timeout)

    def check_async(self, manual=False):
        if self._busy or self.state in ("downloading", "ready"):
            return
        self._busy = True
        self.state = "checking"

        def work():
            try:
                self._check()
            except Exception as e:
                code = getattr(e, "code", None)
                if code == 404:
                    self.state = "nosetup"
                else:
                    self.state = "error"
                    self.msg = "Couldn't reach GitHub (%s)" % (code or e.__class__.__name__)
                if manual:
                    log("Update check: %s" % (self.msg or self.state))
            finally:
                self._busy = False

        threading.Thread(target=work, daemon=True).start()

    def _check(self):
        with self._get("https://api.github.com/repos/%s/releases/latest" % UPDATE_REPO) as r:
            rel = _json.loads(r.read().decode("utf-8"))
        tag = rel.get("tag_name") or ""
        exe = sha = None
        for a in rel.get("assets", []):
            n = a.get("name", "")
            if n.lower().endswith(".exe") and n.lower().startswith("livegrabsetup"):
                exe = a
            elif n.lower().endswith(".sha256"):
                sha = a
        if _ver_tuple(tag) <= _ver_tuple(VERSION) or not exe:
            self.state = "none"
            self.latest = tag.lstrip("vV")
            return
        url = exe.get("browser_download_url", "")
        if not url.startswith("https://github.com/%s/releases/download/" % UPDATE_REPO):
            self.state = "error"
            self.msg = "Update file isn't from the LiveGrab repo, skipped"
            return
        self.latest = tag.lstrip("vV")
        self.notes = (rel.get("body") or "").strip()
        self.asset_url = url
        self.sha_url = sha.get("browser_download_url", "") if sha else ""
        self.state = "available"
        log("Update available: v%s (you have v%s)" % (self.latest, VERSION))

    def download_and_schedule(self):
        if self.state != "available" or not self.asset_url:
            return "Check for updates first"
        if not IS_WIN:
            return "Auto-install only works on Windows"
        self.state = "downloading"
        self.pct = 0.0

        def work():
            try:
                os.makedirs(self.dir, exist_ok=True)
                dst = os.path.join(self.dir, "LiveGrabSetup-%s.exe" % self.latest)
                part = dst + ".part"
                h = hashlib.sha256()
                with self._get(self.asset_url, timeout=60) as r, open(part, "wb") as w:
                    total = int(r.headers.get("Content-Length") or 0)
                    got = 0
                    while True:
                        chunk = r.read(65536)
                        if not chunk:
                            break
                        w.write(chunk)
                        h.update(chunk)
                        got += len(chunk)
                        if total:
                            self.pct = 100.0 * got / total
                if self.sha_url:
                    with self._get(self.sha_url) as r:
                        want = r.read().decode("utf-8", "ignore").split()[0].strip().lower()
                    if want != h.hexdigest():
                        os.remove(part)
                        raise RuntimeError("download was corrupted (checksum mismatch)")
                if os.path.exists(dst):
                    os.remove(dst)
                os.rename(part, dst)
                self.file = dst
                self._schedule()
                self.state = "ready"
                self.pct = 100.0
                log("Update v%s downloaded. It installs when you close OBS." % self.latest)
            except Exception as e:
                self.state = "error"
                self.msg = "Update download failed: %s" % e
                log(self.msg)

        threading.Thread(target=work, daemon=True).start()
        return "Downloading v%s..." % self.latest

    def _schedule(self):
        with open(self.marker, "w") as f:
            f.write(self.latest)
        ps = os.path.join(self.dir, "apply_update.ps1")
        with open(ps, "w", encoding="utf-8") as f:
            f.write(APPLY_PS)
        flags = 0x00000008 | 0x00000200 | 0x08000000  # DETACHED | NEW_PROCESS_GROUP | NO_WINDOW
        subprocess.Popen(["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-WindowStyle", "Hidden",
                          "-File", ps, "-Installer", self.file, "-Obs", _obs_exe(), "-Marker", self.marker],
                         creationflags=flags, close_fds=True,
                         stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    def cancel(self):
        try:
            os.remove(self.marker)
        except Exception:
            pass
        if self.state == "ready":
            self.state = "available"
        return "Update cancelled"


UPD = Updater()


def update_payload():
    return UPD.payload()


def _auto_update_check():
    obs.timer_remove(_auto_update_check)
    if cfg("update_auto", True):
        UPD.check_async()


# =============================================================================
# Dock panel (served locally, added to OBS as a Custom Browser Dock)
# =============================================================================

DOCK_HTML = r"""<!doctype html>
<html><head><meta charset="utf-8"><title>LiveGrab</title>
<meta name="viewport" content="width=device-width,initial-scale=1">
<style>
:root{--bg:#1b1c21;--panel:#25262d;--panel2:#2d2e36;--line:#393a44;--text:#e8e8ec;--muted:#9c9da8;
--live:#ef4444;--ok:#22c55e;--warn:#f59e0b;--accent:#6d5dfc;--accent2:#8b7dff}
*{box-sizing:border-box}html,body{margin:0;height:100%;background:var(--bg);color:var(--text);
font:13px/1.35 "Segoe UI",system-ui,sans-serif;-webkit-user-select:none;user-select:none;overflow:hidden}
.wrap{height:100%;padding:10px;display:flex;flex-direction:column;gap:10px;overflow:auto}
.card{background:var(--panel);border:1px solid var(--line);border-radius:8px;padding:10px;display:flex;flex-direction:column;gap:8px}
.fill{flex:1 1 auto;min-height:140px}
.row{display:flex;gap:6px;align-items:center}
.status{display:flex;align-items:center;gap:8px;font-weight:600}
.dot{width:10px;height:10px;border-radius:50%;background:#666;flex:none}
.dot.live{background:var(--live);animation:p 1.6s infinite}
.dot.connecting,.dot.reconnecting{background:var(--warn)}.dot.offline{background:#777}.dot.error{background:var(--live)}
@keyframes p{0%{box-shadow:0 0 0 0 rgba(239,68,68,.55)}70%{box-shadow:0 0 0 8px rgba(239,68,68,0)}100%{box-shadow:0 0 0 0 rgba(239,68,68,0)}}
.sub{color:var(--muted);font-size:12px;font-weight:400}.grow{flex:1;min-width:0}
.ellip{overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
input[type=text],select{background:var(--panel2);color:var(--text);border:1px solid var(--line);border-radius:6px;
padding:7px 8px;font:inherit;outline:none;min-width:0}
input[type=text]{flex:1;-webkit-user-select:text;user-select:text}input[type=text]:focus,select:focus{border-color:var(--accent)}
button{font:inherit;color:var(--text);background:var(--panel2);border:1px solid var(--line);border-radius:6px;
padding:7px 10px;cursor:pointer}button:hover{border-color:#50515d}button:active{transform:translateY(1px)}
button:disabled{opacity:.45;cursor:default}
.primary{background:var(--accent);border-color:var(--accent)}.primary:hover{background:var(--accent2);border-color:var(--accent2)}
.stop{background:#3a2326;border-color:#6b2a30}
.icon{padding:5px 8px;line-height:0;flex:none;position:relative}
.icon .pip{position:absolute;top:2px;right:2px;width:8px;height:8px;border-radius:50%;background:var(--ok);box-shadow:0 0 0 2px var(--panel);display:none}
.icon.has-update .pip{display:block}
.upd-notes{font-size:12px;color:#c9cad3;background:var(--panel2);border:1px solid var(--line);border-radius:6px;padding:8px;max-height:140px;overflow:auto;white-space:pre-wrap;-webkit-user-select:text;user-select:text;display:none}
.upd-bar{height:6px;border-radius:4px;background:var(--panel2);overflow:hidden;display:none}.upd-bar i{display:block;height:100%;width:0;background:var(--ok);transition:width .3s}
.okbtn{background:#1f5f3a;border-color:#2d8a55}.okbtn:hover{background:#26734a;border-color:#35a063}.icon svg{width:16px;height:16px;fill:none;stroke:currentColor;stroke-width:2;stroke-linecap:round;stroke-linejoin:round}
.clip{width:100%;padding:18px 10px;font-size:22px;font-weight:800;letter-spacing:.06em;border:none;border-radius:8px;
background:linear-gradient(180deg,#f15a5a,#d33636);color:#fff}
.clip:hover{filter:brightness(1.08)}.clip.busy{background:#7a3b3b}
.clip small{display:block;font-size:11px;font-weight:500;letter-spacing:0;opacity:.85;margin-top:2px}
.prog{display:none;flex-direction:column;gap:5px}.prog.show{display:flex}
.prog .top{display:flex;justify-content:space-between;gap:8px;font-size:12px}
.prog .top b{font-weight:600}.prog .eta{color:var(--muted);font-variant-numeric:tabular-nums;flex:none}
.track{height:8px;border-radius:5px;background:var(--panel2);overflow:hidden;border:1px solid var(--line)}
.bar{height:100%;width:0;border-radius:5px;background:linear-gradient(90deg,var(--accent),var(--accent2));transition:width .35s linear}
.prog.done .bar{background:var(--ok)}.prog.failed .bar{background:var(--live)}
.prog .more{font-size:11px;color:var(--muted)}
.chips{display:flex;gap:4px}.chips button{flex:1;padding:5px 0;font-size:12px}
.chips button.on{background:var(--accent);border-color:var(--accent)}
label.tg{display:flex;align-items:center;gap:8px;cursor:pointer}
.lbl{color:var(--muted);font-size:11px;text-transform:uppercase;letter-spacing:.06em}
.clips{display:flex;flex-direction:column;gap:2px;flex:1 1 auto;min-height:0;overflow:auto}
.ci{display:flex;align-items:center;gap:6px;padding:6px;border-radius:6px;cursor:pointer}
.ci:hover{background:var(--panel2)}.ci .t{color:var(--muted);font-size:11px;flex:none}
.badge{font-size:10px;padding:1px 5px;border-radius:4px;background:#34305e;color:#c9c3ff;flex:none}
.empty{color:var(--muted);font-size:12px;padding:4px}
.banner{background:#3a2326;border:1px solid #6b2a30;border-radius:8px;padding:10px;display:none}
details summary{cursor:pointer;color:var(--muted);font-size:12px}
pre{margin:6px 0 0;white-space:pre-wrap;font:11px/1.4 Consolas,monospace;color:#b9bac4;max-height:160px;overflow:auto;-webkit-user-select:text;user-select:text}
.path{font:12px Consolas,monospace;background:var(--panel2);border:1px solid var(--line);border-radius:6px;padding:7px 8px;
word-break:break-all;-webkit-user-select:text;user-select:text}
.qp{display:grid;grid-template-columns:1fr 1fr;gap:4px}.qp button{font-size:12px;padding:6px 4px}
.qp button.on{border-color:var(--accent);color:#c9c3ff}
.hk{display:grid;grid-template-columns:auto 1fr;gap:4px 10px;font-size:12px}
kbd{font:11px Consolas,monospace;background:var(--panel2);border:1px solid var(--line);border-bottom-width:2px;border-radius:4px;padding:1px 6px;justify-self:start}
#settings{position:fixed;inset:0;background:var(--bg);display:none;flex-direction:column}
#settings.open{display:flex}
.shead{display:flex;align-items:center;gap:8px;padding:10px 10px 0}
.shead b{flex:1;font-size:14px}
.sbody{flex:1;overflow:auto;padding:10px;display:flex;flex-direction:column;gap:10px}
.toast{position:fixed;left:10px;right:10px;bottom:10px;background:#2b2c34;border:1px solid var(--line);border-radius:8px;
padding:9px 10px;opacity:0;transform:translateY(8px);transition:.2s;pointer-events:none;z-index:9}.toast.show{opacity:1;transform:none}
</style></head><body><div class="wrap" id="main">
<div class="banner" id="banner"><b>Can't reach LiveGrab.</b><div class="sub">Make sure livegrab.py is loaded in Tools &gt; Scripts.</div></div>

<div class="card">
  <div class="row"><div class="status grow"><span class="dot" id="dot"></span>
    <span class="ellip" id="stxt">Stopped</span></div><span class="sub" id="upt"></span>
    <button class="icon" id="gear" title="Settings"><span class="pip"></span><svg viewBox="0 0 24 24"><circle cx="12" cy="12" r="3"/><path d="M19.4 15a1.65 1.65 0 0 0 .33 1.82l.06.06a2 2 0 1 1-2.83 2.83l-.06-.06a1.65 1.65 0 0 0-1.82-.33 1.65 1.65 0 0 0-1 1.51V21a2 2 0 1 1-4 0v-.09A1.65 1.65 0 0 0 9 19.4a1.65 1.65 0 0 0-1.82.33l-.06.06a2 2 0 1 1-2.83-2.83l.06-.06A1.65 1.65 0 0 0 4.68 15a1.65 1.65 0 0 0-1.51-1H3a2 2 0 1 1 0-4h.09A1.65 1.65 0 0 0 4.6 9a1.65 1.65 0 0 0-.33-1.82l-.06-.06a2 2 0 1 1 2.83-2.83l.06.06A1.65 1.65 0 0 0 9 4.68a1.65 1.65 0 0 0 1-1.51V3a2 2 0 1 1 4 0v.09a1.65 1.65 0 0 0 1 1.51 1.65 1.65 0 0 0 1.82-.33l.06-.06a2 2 0 1 1 2.83 2.83l-.06.06A1.65 1.65 0 0 0 19.4 9a1.65 1.65 0 0 0 1.51 1H21a2 2 0 1 1 0 4h-.09a1.65 1.65 0 0 0-1.51 1z"/></svg></button></div>
  <div class="row"><input type="text" id="url" placeholder="Channel: xqc, kick.com/name, youtube.com/@name/live" spellcheck="false"></div>
  <div class="row"><select id="quality" class="grow">
    <option>best</option><option>1080p60</option><option>1080p</option><option>720p60</option><option>720p</option><option>480p</option><option>audio_only</option>
  </select><button id="go" class="primary" style="min-width:84px">Start</button></div>
</div>

<div class="card">
  <button class="clip" id="clip">CLIP IT<small id="cliplbl">last 30s</small></button>
  <div class="prog" id="prog"><div class="top"><b class="ellip" id="plbl">Saving clip...</b><span class="eta" id="peta"></span></div>
    <div class="track"><div class="bar" id="pbar"></div></div><div class="more" id="pmore"></div></div>
  <div><div class="lbl" style="margin-bottom:4px">Length</div>
  <div class="chips" id="len"><button data-v="15">15s</button><button data-v="30">30s</button><button data-v="60">60s</button><button data-v="120">2m</button></div></div>
  <div><div class="lbl" style="margin-bottom:4px">Keep rolling after press</div>
  <div class="chips" id="post"><button data-v="0">0s</button><button data-v="5">+5s</button><button data-v="10">+10s</button><button data-v="15">+15s</button></div></div>
  <div><div class="lbl" style="margin-bottom:4px">Save as</div>
  <div class="chips" id="fmt"><button data-v="wide" title="Normal widescreen clip only">16:9</button><button data-v="vertical" title="Only the vertical TikTok / Shorts version">9:16 only</button><button data-v="both" title="Keep both versions">Both</button></div></div>
  <div class="row" id="vrow"><span class="grow sub">9:16 style</span>
  <select id="vmode"><option value="blur">Blur background</option><option value="crop">Crop to fill</option></select></div>
  <div class="row"><button id="rb" class="grow">Save OBS Replay</button><button id="folder" class="grow">Open folder</button></div>
</div>

<div class="card fill"><div class="row"><div class="lbl grow">Recent clips</div><span class="sub ellip" id="savedto" style="max-width:60%;cursor:pointer" title="Change in Settings"></span></div>
<div class="clips" id="clips"><div class="empty">No clips yet.</div></div></div>
<details class="card"><summary>Log</summary><pre id="log"></pre></details>
</div>

<div id="settings">
  <div class="shead"><button class="icon" id="back" title="Back"><svg viewBox="0 0 24 24"><path d="M15 18l-6-6 6-6"/></svg></button><b>Settings</b><span class="sub" id="ver"></span></div>
  <div class="sbody">
    <div class="card">
      <div class="lbl">Save clips to</div>
      <div class="path" id="dir">...</div>
      <div class="row"><button id="browse" class="primary grow">Browse...</button><button id="opendir" class="grow">Open</button></div>
      <div class="qp" id="presets"></div>
    </div>
    <div class="card">
      <div class="lbl">Stream</div>
      <label class="tg"><input type="checkbox" id="s_ll"> Low latency (Twitch / Kick)</label>
      <div class="row"><span class="grow">Buffer kept on disk</span><select id="s_buf">
        <option value="60">1 min</option><option value="120">2 min</option><option value="300">5 min</option><option value="600">10 min</option></select></div>
      <label class="tg"><input type="checkbox" id="s_auto"> Start the last channel when OBS opens</label>
    </div>
    <div class="card">
      <div class="lbl">OBS Replay Buffer &amp; Recording</div>
      <label class="tg"><input type="checkbox" id="s_rb"> Turn Replay Buffer on/off with Start/Stop</label>
      <label class="tg"><input type="checkbox" id="s_rec"> Turn Recording on/off with Start/Stop</label>
      <label class="tg"><input type="checkbox" id="s_ren"> Rename replays and move them to the clips folder</label>
    </div>
    <div class="card">
      <div class="lbl">Updates</div>
      <div class="row"><div class="grow"><b id="u_cur">LiveGrab</b><div class="sub" id="u_msg">Up to date</div></div></div>
      <div class="upd-bar" id="u_bar"><i id="u_fill"></i></div>
      <div class="upd-notes" id="u_notes"></div>
      <button id="u_btn" class="grow">Check for updates</button>
      <label class="tg"><input type="checkbox" id="u_auto"> Check for updates when OBS opens</label>
    </div>
    <div class="card">
      <div class="lbl">Hotkeys</div>
      <div class="hk"><kbd>F9</kbd><span>Clip it</span><kbd>F10</kbd><span>Save OBS replay</span><kbd>F8</kbd><span>Start / stop</span></div>
      <div class="sub">Change these in OBS Settings &gt; Hotkeys (search "LiveGrab").</div>
    </div>
  </div>
</div>
<div class="toast" id="toast"></div>
<script>
const $=id=>document.getElementById(id);
const LABEL={stopped:"Stopped",connecting:"Connecting...",live:"LIVE",reconnecting:"Reconnecting...",offline:"Offline, waiting",error:"Error, see log"};
let S=null,urlDirty=false,busyUntil=0,lastClipCount=-1;
function toast(t){const e=$("toast");e.textContent=t;e.classList.add("show");clearTimeout(e._t);e._t=setTimeout(()=>e.classList.remove("show"),2600)}
async function api(p,b){try{const r=await fetch("/api/"+p,{method:"POST",headers:{"Content-Type":"application/json","X-LiveGrab":"1"},body:JSON.stringify(b||{})});
const j=await r.json();if(j.msg)toast(j.msg);setTimeout(poll,250);return j}catch(e){toast("Couldn't reach the script")}}
function fmt(s){s=Math.floor(s);const h=Math.floor(s/3600),m=Math.floor(s%3600/60),x=s%60;return(h?h+":"+String(m).padStart(2,"0"):m)+":"+String(x).padStart(2,"0")}
function setChips(id,v){for(const b of $(id).children)b.classList.toggle("on",+b.dataset.v===+v)}
function esc(t){return String(t).replace(/[&<>"]/g,c=>({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;"}[c]))}
function shortDir(d){const p=d.replace(/\\/g,"/").split("/").filter(Boolean);return p.length>2?".../"+p.slice(-2).join("/"):d}
function setVal(id,v,prop){const e=$(id);if(document.activeElement!==e)e[prop||"value"]=v}
function render(){
  const st=S.status;$("dot").className="dot "+st;
  $("stxt").textContent=(LABEL[st]||st)+(S.channel&&st!=="stopped"?"  ·  "+S.channel:"");
  $("upt").textContent=S.live_for!=null?fmt(S.live_for):"";
  if(!urlDirty&&document.activeElement!==$("url"))$("url").value=S.url||"";
  setVal("quality",S.quality||"best");
  const run=S.running;$("go").textContent=run?"Stop":"Start";$("go").className=run?"stop":"primary";
  $("url").disabled=run;$("quality").disabled=run;
  setChips("len",S.clip_seconds);setChips("post",S.post_roll);
  $("cliplbl").textContent="last "+S.clip_seconds+"s"+(S.post_roll?" + "+S.post_roll+"s after":"");
  const outMode=!S.vertical?"wide":(S.vertical_only?"vertical":"both");
  for(const b of $("fmt").children)b.classList.toggle("on",b.dataset.v===outMode);
  setVal("vmode",S.vertical_mode);$("vmode").disabled=outMode==="wide";$("vrow").style.opacity=outMode==="wide"?.45:1;
  const busy=S.clipping>0||Date.now()<busyUntil;
  $("clip").disabled=st!=="live";
  renderJobs(S.jobs||[]);
  $("rb").textContent=S.obs_rb?"Save OBS Replay":"OBS Replay (off)";
  $("savedto").textContent="→ "+shortDir(S.clips_dir);
  const c=$("clips");
  if(!S.clips.length)c.innerHTML='<div class="empty">No clips yet. Press F9 or CLIP IT while a channel is live.</div>';
  else c.innerHTML=S.clips.map(k=>'<div class="ci" data-n="'+encodeURIComponent(k.name)+'"><span class="grow ellip">'+esc(k.name)+'</span>'+
    (k.only?'<span class="badge">9:16 only</span>':k.vertical?'<span class="badge">+ 9:16</span>':'')+'<span class="t">'+k.ago+'</span></div>').join("");
  if(lastClipCount>=0&&S.clip_total>lastClipCount&&S.clips.length)toast("Saved "+S.clips[0].name);lastClipCount=S.clip_total;
  const L=$("log");const atBottom=L.scrollTop+L.clientHeight>=L.scrollHeight-4;L.textContent=S.log.join("\n");if(atBottom)L.scrollTop=L.scrollHeight;
  // settings view
  $("ver").textContent="v"+S.version;$("dir").textContent=S.clips_dir;
  $("browse").disabled=S.picking;$("browse").textContent=S.picking?"Picker open...":"Browse...";
  $("presets").innerHTML=S.presets.map(p=>'<button data-k="'+p.key+'" class="'+(p.active?"on":"")+'" title="'+esc(p.path)+'">'+esc(p.label)+'</button>').join("");
  $("s_ll").checked=S.low_latency;setVal("s_buf",String(S.buffer_seconds));
  renderUpdate(S.update||{});
  $("s_auto").checked=S.auto_start;$("s_rec").checked=S.record_with_grab;$("s_rb").checked=S.use_obs_replay;$("s_ren").checked=S.rename_obs_replays;
}
function renderJobs(J){
  const P=$("prog");
  if(!J.length){P.className="prog";return}
  const active=J.filter(j=>j.stage!=="done"&&j.stage!=="failed");
  const j=active.length?active[active.length-1]:J[J.length-1];
  P.className="prog show "+(j.stage==="done"?"done":j.stage==="failed"?"failed":"");
  $("plbl").textContent=j.stage==="done"?"Saved \u2713 "+j.name:j.label;
  $("peta").textContent=j.stage==="done"||j.stage==="failed"?"":(j.eta<1?"almost done":"~"+Math.ceil(j.eta)+"s left");
  $("pbar").style.width=j.pct+"%";
  $("pmore").textContent=active.length>1?"+"+(active.length-1)+" more clip"+(active.length>2?"s":"")+" in progress":"";
}
function renderUpdate(U){
  const st=U.state||"idle",btn=$("u_btn");
  $("u_cur").textContent="LiveGrab v"+S.version;
  $("gear").classList.toggle("has-update",st==="available"||st==="ready");
  const msgs={idle:"",checking:"Checking GitHub...",none:"You're on the latest version",available:"Version "+U.latest+" is available",
    downloading:"Downloading v"+U.latest+"... "+Math.round(U.pct||0)+"%",ready:"v"+U.latest+" will install when you close OBS",
    error:U.msg||"Couldn't check for updates",nosetup:"Update server isn't set up yet"};
  $("u_msg").textContent=msgs[st]||"";
  $("u_bar").style.display=st==="downloading"?"block":"none";$("u_fill").style.width=(U.pct||0)+"%";
  const n=$("u_notes");n.style.display=(st==="available"||st==="ready")&&U.notes?"block":"none";n.textContent=U.notes||"";
  btn.disabled=st==="checking"||st==="downloading";btn.className="grow";
  if(st==="available"){btn.textContent="Update to v"+U.latest;btn.className="grow okbtn"}
  else if(st==="ready"){btn.textContent="Cancel update";}
  else if(st==="checking")btn.textContent="Checking...";
  else if(st==="downloading")btn.textContent="Downloading...";
  else btn.textContent="Check for updates";
  btn.dataset.act=st==="available"?"update_install":st==="ready"?"update_cancel":"update_check";
  $("u_auto").checked=S.update_auto!==false;
}
async function poll(){let r;try{r=await fetch("/api/status",{cache:"no-store"});S=await r.json()}
catch(e){$("banner").style.display="block";return}
$("banner").style.display="none";if(S.version&&S.version!=="__LGVER__"){location.reload();return}try{render()}catch(e){console.error(e);toast("Dock error: "+e.message)}}
$("url").addEventListener("input",()=>urlDirty=true);
$("url").addEventListener("keydown",e=>{if(e.key==="Enter")$("go").click()});
$("go").onclick=()=>{if(S&&S.running)api("stop");else{const u=$("url").value.trim();if(!u){toast("Type a channel first");return}
  urlDirty=false;api("start",{url:u,quality:$("quality").value})}};
$("clip").onclick=()=>{api("clip")};
$("len").onclick=e=>{if(e.target.dataset.v)api("settings",{clip_seconds:+e.target.dataset.v})};
$("post").onclick=e=>{if(e.target.dataset.v)api("settings",{post_roll:+e.target.dataset.v})};
$("fmt").onclick=e=>{const b=e.target.closest("button");if(b)api("settings",{output:b.dataset.v})};
$("vmode").onchange=()=>api("settings",{vertical_mode:$("vmode").value});
$("rb").onclick=()=>api("obs_replay");$("folder").onclick=()=>api("open_folder");
$("clips").onclick=e=>{const d=e.target.closest(".ci");if(d)api("open_clip",{name:decodeURIComponent(d.dataset.n)})};
const openS=()=>$("settings").classList.add("open"),closeS=()=>$("settings").classList.remove("open");
$("gear").onclick=openS;$("savedto").onclick=openS;$("back").onclick=closeS;
document.addEventListener("keydown",e=>{if(e.key==="Escape")closeS()});
$("browse").onclick=()=>api("pick_folder");$("opendir").onclick=()=>api("open_folder");
$("presets").onclick=e=>{const b=e.target.closest("button");if(b)api("set_folder",{preset:b.dataset.k})};
$("s_ll").onchange=()=>api("settings",{low_latency:$("s_ll").checked});
$("s_buf").onchange=()=>api("settings",{buffer_seconds:+$("s_buf").value});
$("s_auto").onchange=()=>api("settings",{auto_start:$("s_auto").checked});
$("s_rb").onchange=()=>api("settings",{use_obs_replay:$("s_rb").checked});
$("s_ren").onchange=()=>api("settings",{rename_obs_replays:$("s_ren").checked});
$("s_rec").onchange=()=>api("settings",{record_with_grab:$("s_rec").checked});
$("u_btn").onclick=()=>api($("u_btn").dataset.act||"update_check");
$("u_auto").onchange=()=>api("settings",{update_auto:$("u_auto").checked});
poll();(function loop(){setTimeout(()=>{poll();loop()},S&&S.jobs&&S.jobs.length?400:1000)})();
</script></body></html>"""


STANDBY_HTML = r"""<!doctype html><html><head><meta charset="utf-8"><title>Standby</title><style>
html,body{margin:0;width:100%;height:100%;overflow:hidden;background:#0f1016;color:#eceaf6;font-family:"Segoe UI",system-ui,sans-serif}
.bg{position:absolute;inset:0;background:radial-gradient(1200px 700px at 20% 10%,#2a2170 0,transparent 60%),radial-gradient(900px 600px at 90% 100%,#4a1630 0,transparent 55%),#0f1016}
.c{position:absolute;inset:0;display:flex;flex-direction:column;align-items:center;justify-content:center;gap:3vh;text-align:center}
.t{font-size:8vh;font-weight:800;letter-spacing:.35em;margin-right:-.35em}
.bar{width:22vh;height:.6vh;border-radius:1vh;background:#6d5dfc}
.s{font-size:3.2vh;letter-spacing:.2em;text-transform:uppercase;color:#b9b4d8;display:flex;align-items:center;gap:1.4vh}
.d{width:1.6vh;height:1.6vh;border-radius:50%;background:#777}.d.on{background:#ef4444;animation:p 1.6s infinite}
.d.wait{background:#f59e0b;animation:p 1.6s infinite}
@keyframes p{50%{opacity:.35}}
.h{position:absolute;bottom:5vh;font-size:2.2vh;color:#77738f;letter-spacing:.08em}
</style></head><body><div class="bg"></div><div class="c"><div class="t">LIVEGRAB</div><div class="bar"></div>
<div class="s"><span class="d" id="d"></span><span id="s">Pick a channel in the LiveGrab dock</span></div></div>
<div class="h" id="h" style="left:0;right:0;text-align:center"></div>
<script>
async function tick(){try{const j=await (await fetch("/api/status",{cache:"no-store"})).json();const d=document.getElementById("d"),s=document.getElementById("s");
const c=j.channel||"";let t="Pick a channel in the LiveGrab dock",k="";
if(j.status==="connecting"||j.status==="reconnecting"){t="Connecting to "+c;k="wait"}
else if(j.status==="offline"){t=c+" is offline. Waiting for them to go live";k="wait"}
else if(j.status==="live"){t="Live: "+c;k="on"}else if(j.status==="error"){t="Couldn't load that channel";k=""}
s.textContent=t;d.className="d "+k}catch(e){}}
tick();setInterval(tick,1500);
</script></body></html>"""


try:
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
except ImportError:  # very old Python
    from http.server import BaseHTTPRequestHandler, HTTPServer as ThreadingHTTPServer
import json
import collections


def _ago(t):
    d = max(0, int(time.time() - t))
    if d < 60:
        return "%ds ago" % d
    if d < 3600:
        return "%dm ago" % (d // 60)
    if d < 86400:
        return "%dh ago" % (d // 3600)
    return time.strftime("%b %d", time.localtime(t))


_clip_cache = {"t": 0, "items": [], "total": 0}


def recent_clips(limit=8):
    if time.time() - _clip_cache["t"] < 1.5:
        return _clip_cache["items"], _clip_cache["total"]
    items, total = [], 0
    d = clips_dir()
    try:
        names = [f for f in os.listdir(d) if f.lower().endswith((".mp4", ".mkv", ".mov"))]
        present = set(n.lower() for n in names)
        files = []
        for n in names:
            stem, ext = os.path.splitext(n)
            if stem.endswith("_vertical"):
                wide = stem[:-len("_vertical")]
                if any((wide + e).lower() in present for e in (".mp4", ".mkv", ".mov")):
                    continue  # shown with its 16:9 twin
            files.append(os.path.join(d, n))
        total = len(files)
        files.sort(key=os.path.getmtime, reverse=True)
        for f in files[:limit]:
            base = os.path.splitext(f)[0]
            only = base.endswith("_vertical")
            items.append({"name": os.path.basename(f), "ago": _ago(os.path.getmtime(f)),
                          "vertical": only or os.path.exists(base + "_vertical.mp4"), "only": only})
    except Exception:
        pass
    _clip_cache.update(t=time.time(), items=items, total=total)
    return items, total


def status_payload():
    clips, total = recent_clips()
    return {
        "status": G.status, "running": G.running,
        "url": cfg("url", ""), "channel": channel_name(G.url) if G.url else "",
        "quality": cfg("quality", "best"),
        "live_for": (time.time() - G.live_since) if G.live_since else None,
        "clip_seconds": int(cfg("clip_seconds", 30)), "post_roll": int(cfg("post_roll", 0)),
        "vertical": bool(cfg("vertical", False)), "vertical_only": bool(cfg("vertical_only", False)), "vertical_mode": cfg("vertical_mode", "blur") or "blur",
        "obs_rb": STATE["obs_rb"], "clipping": STATE["clipping"],
        "clips": clips, "clip_total": total, "clips_dir": clips_dir(),
        "log": LOG_LINES[-30:], "version": VERSION,
        "presets": folder_presets(), "picking": STATE.get("picking", False),
        "jobs": jobs_payload(),
        "low_latency": bool(cfg("low_latency", True)), "buffer_seconds": int(cfg("buffer_seconds", 120) or 120),
        "auto_start": bool(cfg("auto_start", False)), "use_obs_replay": bool(cfg("use_obs_replay", False)),
        "rename_obs_replays": bool(cfg("rename_obs_replays", True)),
        "update": update_payload(), "update_auto": bool(cfg("update_auto", True)),
        "record_with_grab": bool(cfg("record_with_grab", False)),
    }


def run_on_main(fn):
    MAIN_Q.append(fn)


def persist(**kw):
    """Update config now, write to the script's saved settings on the OBS thread."""
    CFG.update(kw)

    def w():
        s = SETTINGS[0]
        if s is None:
            return
        for k, v in kw.items():
            if isinstance(v, bool):
                obs.obs_data_set_bool(s, k, v)
            elif isinstance(v, int):
                obs.obs_data_set_int(s, k, v)
            else:
                obs.obs_data_set_string(s, k, str(v))
    run_on_main(w)


def _desktop_dir():
    home = os.path.expanduser("~")
    for d in (os.path.join(home, "OneDrive", "Desktop"), os.path.join(home, "Desktop")):
        if os.path.isdir(d):
            return d
    return os.path.join(home, "Desktop")


def folder_presets():
    home = os.path.expanduser("~")
    opts = [("videos", "Videos", os.path.join(home, "Videos", "LiveGrab")),
            ("desktop", "Desktop", os.path.join(_desktop_dir(), "LiveGrab"))]
    if IS_WIN:
        for letter in "DEF":
            if os.path.isdir(letter + ":\\"):
                opts.append((letter.lower(), letter + ": drive", letter + ":\\LiveGrab"))
    cur = os.path.normcase(os.path.normpath(clips_dir()))
    return [{"key": k, "label": l, "path": p, "active": os.path.normcase(os.path.normpath(p)) == cur}
            for k, l, p in opts[:4]]


def set_clips_dir(path):
    path = os.path.normpath(_expand(path.strip()))
    try:
        os.makedirs(path, exist_ok=True)
        probe = os.path.join(path, ".livegrab_write_test")
        with open(probe, "w") as f:
            f.write("ok")
        os.remove(probe)
    except Exception as e:
        return "Can't save there: %s" % e
    persist(clips_dir=path)
    _clip_cache["t"] = 0
    log("Clips will save to %s" % path)
    return "Saving clips to %s" % path


PICK_PS = r"""
Add-Type -AssemblyName System.Windows.Forms
[Console]::OutputEncoding = [Text.Encoding]::UTF8
$owner = New-Object System.Windows.Forms.Form -Property @{TopMost=$true; ShowInTaskbar=$false}
$d = New-Object System.Windows.Forms.FolderBrowserDialog
$d.Description = "Where should LiveGrab save your clips?"
$d.ShowNewFolderButton = $true
if ($env:LG_START -and (Test-Path $env:LG_START)) { $d.SelectedPath = $env:LG_START }
if ($d.ShowDialog($owner) -eq [System.Windows.Forms.DialogResult]::OK) { Write-Output $d.SelectedPath }
"""


def pick_folder_async():
    if STATE.get("picking"):
        return "The folder picker is already open (check your taskbar)"
    if not IS_WIN:
        return "The folder picker only works on Windows. Use a quick pick instead."
    STATE["picking"] = True

    def work():
        try:
            env = dict(os.environ, LG_START=clips_dir())
            r = subprocess.run(["powershell", "-NoProfile", "-STA", "-ExecutionPolicy", "Bypass",
                                "-Command", PICK_PS], capture_output=True, env=env,
                               creationflags=NO_WINDOW, timeout=900)
            chosen = r.stdout.decode("utf-8", "replace").strip().splitlines()
            if chosen and chosen[-1].strip():
                set_clips_dir(chosen[-1].strip())
        except Exception as e:
            log("Folder picker error: %s" % e)
        finally:
            STATE["picking"] = False

    threading.Thread(target=work, daemon=True).start()
    return "Pick a folder in the window that just opened"


def handle_action(action, body):
    if action == "start":
        url = (body.get("url") or "").strip()
        if not url:
            return "Type a channel first"
        persist(url=url, quality=body.get("quality") or cfg("quality", "best") or "best")
        run_on_main(start_grab)
        return "Starting %s..." % channel_name(url)
    if action == "stop":
        threading.Thread(target=stop_grab, daemon=True).start()
        return "Stopped"
    if action == "clip":
        if not G.running:
            return "Start a grab first"
        clip_now()
        return None
    if action == "obs_replay":
        if not STATE["obs_rb"]:
            return "OBS Replay Buffer is off (Settings > Output > Replay Buffer)"
        run_on_main(obs_replay_save)
        return "Saving OBS replay..."
    if action == "settings":
        upd = {}
        if "clip_seconds" in body:
            upd["clip_seconds"] = max(5, min(300, int(body["clip_seconds"])))
        if "post_roll" in body:
            upd["post_roll"] = max(0, min(30, int(body["post_roll"])))
        if body.get("output") in ("wide", "vertical", "both"):
            upd["vertical"] = body["output"] != "wide"
            upd["vertical_only"] = body["output"] == "vertical"
        if "vertical" in body:
            upd["vertical"] = bool(body["vertical"])
        if body.get("vertical_mode") in ("blur", "crop"):
            upd["vertical_mode"] = body["vertical_mode"]
        for k in ("low_latency", "auto_start", "use_obs_replay", "rename_obs_replays", "update_auto", "record_with_grab"):
            if k in body:
                upd[k] = bool(body[k])
        if "buffer_seconds" in body:
            upd["buffer_seconds"] = max(30, min(1800, int(body["buffer_seconds"])))
        if upd:
            persist(**upd)
            if "clip_seconds" in upd:
                G.buffer_seconds = max(G.buffer_seconds, upd["clip_seconds"] + 10)
            if "buffer_seconds" in upd or "low_latency" in upd:
                return "Saved. Takes effect next time you press Start"
            return "Saved" if len(upd) == 1 and list(upd)[0] in ("auto_start", "use_obs_replay", "rename_obs_replays") else None
        return None
    if action == "update_check":
        UPD.check_async(manual=True)
        return None
    if action == "update_install":
        return UPD.download_and_schedule()
    if action == "update_cancel":
        return UPD.cancel()
    if action == "pick_folder":
        return pick_folder_async()
    if action == "set_folder":
        for p in folder_presets():
            if p["key"] == body.get("preset"):
                return set_clips_dir(p["path"])
        return "Unknown folder"
    if action == "open_folder":
        open_folder(clips_dir())
        return None
    if action == "open_clip":
        name = os.path.basename(body.get("name") or "")
        path = os.path.join(clips_dir(), name)
        if name and os.path.isfile(path):
            if IS_WIN:
                os.startfile(path)  # noqa
            else:
                subprocess.Popen(["open" if sys.platform == "darwin" else "xdg-open", path])
        return None
    return "Unknown action"


class DockHandler(BaseHTTPRequestHandler):
    server_version = "LiveGrab"

    def log_message(self, *a):
        pass

    def _send(self, code, body, ctype):
        data = body.encode("utf-8") if isinstance(body, str) else body
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        path = self.path.split("?")[0]
        if path in ("/", "/index.html"):
            self._send(200, DOCK_HTML.replace("__LGVER__", VERSION), "text/html; charset=utf-8")
        elif path == "/standby":
            self._send(200, STANDBY_HTML, "text/html; charset=utf-8")
        elif path == "/api/status":
            self._send(200, json.dumps(status_payload()), "application/json")
        else:
            self._send(404, "not found", "text/plain")

    def do_POST(self):
        # Custom header forces a CORS preflight, so random websites can't drive this.
        if self.headers.get("X-LiveGrab") != "1" or not self.path.startswith("/api/"):
            self._send(403, "forbidden", "text/plain")
            return
        try:
            n = int(self.headers.get("Content-Length") or 0)
            body = json.loads(self.rfile.read(n) or b"{}") if n else {}
            msg = handle_action(self.path[5:].split("?")[0], body)
            self._send(200, json.dumps({"ok": True, "msg": msg}), "application/json")
        except Exception as e:
            self._send(500, json.dumps({"ok": False, "msg": "Error: %s" % e}), "application/json")


class DockServer(object):
    def __init__(self):
        self.httpd = None
        self.port = None

    def start(self, port):
        if self.httpd and self.port == port:
            return
        self.stop()
        try:
            ThreadingHTTPServer.allow_reuse_address = True
            self.httpd = ThreadingHTTPServer(("127.0.0.1", port), DockHandler)
            self.httpd.daemon_threads = True
            self.port = port
            threading.Thread(target=self.httpd.serve_forever, kwargs={"poll_interval": 0.5},
                             daemon=True).start()
            log("Dock ready at http://127.0.0.1:%d/" % port)
        except Exception as e:
            self.httpd = None
            log("Couldn't start the dock on port %d (%s). Pick another Dock port." % (port, e))

    def stop(self):
        if self.httpd:
            try:
                self.httpd.shutdown()
                self.httpd.server_close()
            except Exception:
                pass
        self.httpd = None


# =============================================================================
# OBS integration
# =============================================================================

SOURCE_KIND = "ffmpeg_source"
G = Grabber()
CFG = {}
HOTKEYS = {}
SETTINGS = [None]
STATE = {"obs_rb": False, "clipping": 0, "picking": False}
MAIN_Q = collections.deque()
DOCK = DockServer()
_restart_media = [False]


def cfg(k, default=None):
    v = CFG.get(k, default)
    return default if v is None else v


def clips_dir():
    d = cfg("clips_dir") or os.path.join(os.path.expanduser("~"), "Videos", "LiveGrab")
    return _expand(d)


def dock_url():
    return "http://127.0.0.1:%d/" % int(cfg("dock_port", 5578) or 5578)


def _media_settings():
    d = obs.obs_data_create()
    obs.obs_data_set_bool(d, "is_local_file", False)
    obs.obs_data_set_string(d, "input", G.obs_input_url())
    obs.obs_data_set_string(d, "input_format", "mpegts")
    obs.obs_data_set_int(d, "buffering_mb", 2)
    obs.obs_data_set_int(d, "reconnect_delay_sec", 1)
    obs.obs_data_set_bool(d, "restart_on_activate", False)
    obs.obs_data_set_bool(d, "close_when_inactive", False)
    obs.obs_data_set_bool(d, "clear_on_media_end", True)
    obs.obs_data_set_bool(d, "hw_decode", True)
    return d


def ensure_media_source():
    """Create/update the Media Source and make sure it's in the chosen scene."""
    name = cfg("source_name") or "LiveGrab Feed"
    data = _media_settings()
    src = obs.obs_get_source_by_name(name)
    if src is None:
        src = obs.obs_source_create(SOURCE_KIND, name, data, None)
        try:  # so you can hear the stream while it also goes to recordings/replays
            obs.obs_source_set_monitoring_type(src, obs.OBS_MONITORING_TYPE_MONITOR_AND_OUTPUT)
        except Exception:
            pass
        created = True
    else:
        obs.obs_source_update(src, data)
        created = False
    obs.obs_data_release(data)

    scene_name = cfg("scene") or ""
    if scene_name and scene_name != "(current scene)":
        scene_src = obs.obs_get_source_by_name(scene_name)
    else:
        scene_src = obs.obs_frontend_get_current_scene()
    if scene_src is not None:
        scene = obs.obs_scene_from_source(scene_src)
        item = obs.obs_scene_find_source(scene, name)
        if item is None:
            item = obs.obs_scene_add(scene, src)
            ovi = obs.obs_video_info()
            obs.obs_get_video_info(ovi)
            b = obs.vec2()
            b.x, b.y = float(ovi.base_width), float(ovi.base_height)
            obs.obs_sceneitem_set_bounds_type(item, obs.OBS_BOUNDS_SCALE_INNER)
            obs.obs_sceneitem_set_bounds(item, b)
            log("Added '%s' to scene '%s'." % (name, obs.obs_source_get_name(scene_src)))
        obs.obs_source_release(scene_src)
    obs.obs_source_release(src)
    if created:
        log("Created Media Source '%s'." % name)


def restart_media_source():
    src = obs.obs_get_source_by_name(cfg("source_name") or "LiveGrab Feed")
    if src is not None:
        obs.obs_source_media_restart(src)
        obs.obs_source_release(src)


def apply_config():
    G.url = cfg("url", "")
    G.quality = cfg("quality", "best") or "best"
    G.low_latency = cfg("low_latency", True)
    G.port = int(cfg("port", 5577) or 5577)
    G.buffer_seconds = max(int(cfg("buffer_seconds", 120) or 120), int(cfg("clip_seconds", 30)) + 10)
    G.extra_args = cfg("extra_args", "")
    G.streamlink = find_exe(cfg("streamlink_path"), "streamlink", STREAMLINK_CANDIDATES)
    G.ffmpeg = find_exe(cfg("ffmpeg_path"), "ffmpeg", FFMPEG_CANDIDATES)


def start_grab():
    if G.running:
        return
    apply_config()
    if not G.url:
        log("Enter a channel or URL first.")
        return
    ensure_media_source()
    G.on_pipeline_started = lambda: _restart_media.__setitem__(0, True)
    G.start()
    if cfg("use_obs_replay") and not obs.obs_frontend_replay_buffer_active():
        obs.obs_frontend_replay_buffer_start()
        log("Started OBS Replay Buffer.")
    if cfg("record_with_grab") and not obs.obs_frontend_recording_active():
        obs.obs_frontend_recording_start()
        log("Started OBS Recording.")


def _stop_replay_buffer():
    try:
        if obs.obs_frontend_replay_buffer_active():
            obs.obs_frontend_replay_buffer_stop()
            log("Stopped OBS Replay Buffer.")
    except Exception as e:
        log("Couldn't stop the Replay Buffer: %s" % e)


def _stop_recording():
    try:
        if obs.obs_frontend_recording_active():
            obs.obs_frontend_recording_stop()
            log("Stopped OBS Recording.")
    except Exception as e:
        log("Couldn't stop the recording: %s" % e)


def stop_grab():
    G.stop()
    log("Stopped.")
    # "Turn on Replay Buffer when a grab starts" also means "turn it off when the grab stops"
    if cfg("use_obs_replay"):
        run_on_main(_stop_replay_buffer)
    if cfg("record_with_grab"):
        run_on_main(_stop_recording)


class Job(object):
    """Tracks one clip from hotkey press to finished files, for the dock's progress bar."""
    _ids = [0]

    def __init__(self, kind, post=0, vertical=False, clip_len=30):
        Job._ids[0] += 1
        self.id = Job._ids[0]
        self.kind = kind
        self.started = time.time()
        self.post = post
        self.vertical = vertical
        self.clip_len = float(clip_len)
        self.stage = "rolling" if post > 0 else "saving"
        self.stage_start = time.time()
        self.stage_frac = 0.0
        self.vert_speed = 2.5      # guess: x realtime; replaced by the measured speed
        self.name = ""
        self.done_at = None
        self.ok = True
        self.base = 0.0   # bar % when the current stage began
        self.disp = 0.0   # bar % shown last time (never goes backwards)
        pre = post + 1.5
        vert = (self.clip_len / self.vert_speed) if vertical else 0.0
        self.pre_share = 100.0 * pre / (pre + vert) if (pre + vert) else 100.0
        JOBS[self.id] = self

    def set_stage(self, stage):
        self.base = self.disp
        self.stage = stage
        self.stage_start = time.time()
        self.stage_frac = 0.0

    def vert_progress(self, done):
        el = time.time() - self.stage_start
        if self.clip_len > 0:
            self.stage_frac = max(self.stage_frac, min(1.0, done / self.clip_len))
        if done > 1 and el > 0.5:
            self.vert_speed = max(0.2, done / el)

    def finish(self, ok, name=""):
        self.ok = ok
        self.name = name
        self.stage = "done" if ok else "failed"
        self.done_at = time.time()

    def remaining(self):
        now = time.time()
        el = now - self.stage_start
        vert_total = self.clip_len / self.vert_speed if self.vertical else 0.0
        if self.stage == "rolling":
            return max(0.0, self.post - el) + 1.5 + vert_total
        if self.stage == "saving":
            return max(0.3, 1.5 - el) + vert_total
        if self.stage == "vertical":
            left = self.clip_len * (1 - self.stage_frac) / self.vert_speed
            return max(0.3, left)
        return 0.0

    def to_dict(self):
        rem = self.remaining()
        el = time.time() - self.stage_start
        if self.stage == "rolling":
            pct = self.pre_share * min(1.0, el / (self.post + 1.5))
        elif self.stage == "saving":
            top = self.pre_share if self.vertical else 100.0
            pct = self.base + (top - self.base) * 0.9 * min(1.0, el / 1.5)
        elif self.stage == "vertical":
            pct = self.base + (100.0 - self.base) * self.stage_frac
        else:
            pct = 100.0 if self.stage == "done" else self.disp
        if self.stage != "done":
            pct = min(99.0, pct)
        self.disp = max(self.disp, pct)
        pct = self.disp
        labels = {"rolling": "Recording %ds more..." % max(0, int(round(self.post - (time.time() - self.stage_start)))),
                  "saving": "Saving clip...", "vertical": "Making 9:16 copy...",
                  "done": "Saved", "failed": "Clip failed, see Log"}
        return {"id": self.id, "stage": self.stage, "label": labels.get(self.stage, self.stage),
                "pct": round(pct, 1), "eta": round(rem, 1), "name": self.name, "kind": self.kind}


JOBS = {}


def jobs_payload():
    now = time.time()
    for jid in [j for j, job in JOBS.items() if job.done_at and now - job.done_at > 4]:
        JOBS.pop(jid, None)
    return [j.to_dict() for j in sorted(JOBS.values(), key=lambda j: j.id)]


def _postprocess(path, job=None):
    if cfg("vertical") and path:
        if job:
            job.vertical = True
            dur = media_duration(G.ffmpeg, path)
            if dur:
                job.clip_len = dur
            job.set_stage("vertical")
        v = make_vertical(G.ffmpeg, path, cfg("vertical_mode", "blur"),
                          on_progress=job.vert_progress if job else None)
        if v:
            log("Vertical copy saved: %s" % os.path.basename(v))
            if cfg("vertical_only"):
                for _ in range(10):
                    try:
                        os.remove(path)
                        break
                    except Exception:
                        time.sleep(0.5)
                _clip_cache["t"] = 0
                return v
        elif cfg("vertical_only"):
            log("Kept the 16:9 clip because the 9:16 version failed.")
    _clip_cache["t"] = 0
    return path


def clip_now():
    if not G.running:
        log("Grab isn't running. Press Start first.")
        return
    secs = int(cfg("clip_seconds", 30))
    post = int(cfg("post_roll", 0))
    out_dir = clips_dir()
    STATE["clipping"] += 1
    job = Job("clip", post=post, vertical=bool(cfg("vertical")), clip_len=secs + post)

    def work():
        ok, name = False, ""
        try:
            if post > 0:
                log("Clipping... capturing %ds more first." % post)
                time.sleep(post)
            job.set_stage("saving")
            path = G.save_clip(secs + post, out_dir)
            if path:
                name = os.path.basename(path)
                log("Clip saved (%ds): %s" % (secs + post, name))
                _clip_cache["t"] = 0
                final = _postprocess(path, job)
                name = os.path.basename(final or path)
                ok = True
        finally:
            job.finish(ok, name)
            STATE["clipping"] -= 1

    threading.Thread(target=work, daemon=True).start()


def obs_replay_save():
    if not obs.obs_frontend_replay_buffer_active():
        log("OBS Replay Buffer isn't running. Enable it in Settings > Output > Replay Buffer.")
        return
    obs.obs_frontend_replay_buffer_save()


def _handle_obs_replay_saved():
    try:
        path = obs.obs_frontend_get_last_replay()
    except Exception:
        path = None
    if not path:
        return
    rename_on = cfg("rename_obs_replays") and G.running and G.url

    job = Job("replay", vertical=bool(cfg("vertical")), clip_len=60)
    job.set_stage("saving")

    def work():
        src = dst = path
        if rename_on:
            ext = os.path.splitext(src)[1]
            os.makedirs(clips_dir(), exist_ok=True)
            dst = unique_path(os.path.join(clips_dir(), "%s_%s_replay%s" % (channel_name(G.url), timestamp(), ext)))
            for _ in range(20):  # file can be briefly locked right after saving
                try:
                    shutil.move(src, dst)
                    break
                except Exception:
                    time.sleep(0.5)
            else:
                dst = src
        log("OBS replay saved: %s" % dst)
        try:
            dst = _postprocess(dst, job) or dst
        finally:
            job.finish(True, os.path.basename(dst))

    threading.Thread(target=work, daemon=True).start()


def on_frontend_event(event):
    if event == obs.OBS_FRONTEND_EVENT_REPLAY_BUFFER_SAVED:
        _handle_obs_replay_saved()
    elif event == obs.OBS_FRONTEND_EVENT_EXIT:
        G.stop()
        DOCK.stop()


def tick():
    while MAIN_Q:
        fn = MAIN_Q.popleft()
        try:
            fn()
        except Exception as e:
            log("Error: %s" % e)
    if _restart_media[0]:
        _restart_media[0] = False
        restart_media_source()
    try:
        STATE["obs_rb"] = bool(obs.obs_frontend_replay_buffer_active())
    except Exception:
        pass


def _autostart_once():
    obs.timer_remove(_autostart_once)
    if cfg("auto_start") and cfg("url"):
        start_grab()


# ---- hotkeys ----
def hk_clip(pressed):
    if pressed:
        clip_now()


def hk_toggle(pressed):
    if pressed:
        if G.running:
            threading.Thread(target=stop_grab, daemon=True).start()
        else:
            run_on_main(start_grab)


def hk_obs_replay(pressed):
    if pressed:
        run_on_main(obs_replay_save)


HOTKEY_DEFS = [
    ("livegrab.clip", "LiveGrab: Save clip (built-in buffer)", hk_clip),
    ("livegrab.toggle", "LiveGrab: Start/Stop grab", hk_toggle),
    ("livegrab.obs_replay", "LiveGrab: Save OBS Replay Buffer", hk_obs_replay),
]


# ---- buttons ----
def btn_start(props, prop):
    start_grab()
    return False


def btn_stop(props, prop):
    threading.Thread(target=stop_grab, daemon=True).start()
    return False


def btn_clip(props, prop):
    clip_now()
    return False


def btn_obs_replay(props, prop):
    obs_replay_save()
    return False


def btn_open(props, prop):
    open_folder(clips_dir())
    return False


def btn_status(props, prop):
    apply_config()
    log("Status: %s | channel: %s | streamlink: %s | ffmpeg: %s | dock: %s"
        % (G.status, normalize_url(G.url) or "-", G.streamlink or "NOT FOUND",
           G.ffmpeg or "NOT FOUND", dock_url() if DOCK.httpd else "NOT RUNNING"))
    return False


# =============================================================================
# OBS script entry points
# =============================================================================

def script_description():
    return ("<b>LiveGrab %s</b><br>"
            "Pull a live Twitch / Kick / YouTube stream into OBS and clip it while it's live.<br><br>"
            "<b>Add the dock:</b> Docks &gt; Custom Browser Docks, name it <i>LiveGrab</i>, URL "
            "<code>%s</code>, click Apply. It then shows under the Docks menu.<br>"
            "Hotkeys: Settings &gt; Hotkeys, search \"LiveGrab\"." % (VERSION, dock_url()))


def script_defaults(s):
    obs.obs_data_set_default_string(s, "quality", "best")
    obs.obs_data_set_default_bool(s, "low_latency", True)
    obs.obs_data_set_default_string(s, "scene", "(current scene)")
    obs.obs_data_set_default_string(s, "source_name", "LiveGrab Feed")
    obs.obs_data_set_default_int(s, "clip_seconds", 30)
    obs.obs_data_set_default_int(s, "post_roll", 0)
    obs.obs_data_set_default_int(s, "buffer_seconds", 120)
    obs.obs_data_set_default_string(s, "clips_dir", os.path.join(os.path.expanduser("~"), "Videos", "LiveGrab"))
    obs.obs_data_set_default_bool(s, "vertical", False)
    obs.obs_data_set_default_bool(s, "vertical_only", False)
    obs.obs_data_set_default_string(s, "vertical_mode", "blur")
    obs.obs_data_set_default_bool(s, "use_obs_replay", False)
    obs.obs_data_set_default_bool(s, "rename_obs_replays", True)
    obs.obs_data_set_default_int(s, "port", 5577)
    obs.obs_data_set_default_int(s, "dock_port", 5578)
    obs.obs_data_set_default_bool(s, "auto_start", False)
    obs.obs_data_set_default_bool(s, "update_auto", True)
    obs.obs_data_set_default_bool(s, "record_with_grab", False)


def script_properties():
    p = obs.obs_properties_create()

    obs.obs_properties_add_text(p, "url", "Channel or URL", obs.OBS_TEXT_DEFAULT)
    q = obs.obs_properties_add_list(p, "quality", "Quality", obs.OBS_COMBO_TYPE_EDITABLE, obs.OBS_COMBO_FORMAT_STRING)
    for v in ("best", "1080p60", "1080p", "720p60", "720p", "480p", "worst", "audio_only"):
        obs.obs_property_list_add_string(q, v, v)
    obs.obs_properties_add_bool(p, "low_latency", "Low latency mode (Twitch / Kick)")

    sc = obs.obs_properties_add_list(p, "scene", "Add source to scene", obs.OBS_COMBO_TYPE_LIST, obs.OBS_COMBO_FORMAT_STRING)
    obs.obs_property_list_add_string(sc, "(current scene)", "(current scene)")
    for n in (obs.obs_frontend_get_scene_names() or []):
        obs.obs_property_list_add_string(sc, n, n)
    obs.obs_properties_add_text(p, "source_name", "Media Source name", obs.OBS_TEXT_DEFAULT)

    obs.obs_properties_add_button(p, "btn_start", "Start grab", btn_start)
    obs.obs_properties_add_button(p, "btn_stop", "Stop grab", btn_stop)
    obs.obs_properties_add_button(p, "btn_clip", "Save clip now", btn_clip)
    obs.obs_properties_add_button(p, "btn_status", "Show status in Script Log", btn_status)

    obs.obs_properties_add_int_slider(p, "clip_seconds", "Clip length (seconds before hotkey)", 5, 300, 5)
    obs.obs_properties_add_int_slider(p, "post_roll", "Keep recording after hotkey (seconds)", 0, 30, 1)
    obs.obs_properties_add_int(p, "buffer_seconds", "Buffer size (seconds kept on disk)", 30, 1800, 10)
    obs.obs_properties_add_path(p, "clips_dir", "Save clips to", obs.OBS_PATH_DIRECTORY, "", None)
    obs.obs_properties_add_button(p, "btn_open", "Open clips folder", btn_open)

    obs.obs_properties_add_bool(p, "vertical", "Also export a 9:16 vertical copy (TikTok / Shorts / Reels)")
    obs.obs_properties_add_bool(p, "vertical_only", "Keep only the 9:16 version (delete the 16:9 one)")
    vm = obs.obs_properties_add_list(p, "vertical_mode", "Vertical style", obs.OBS_COMBO_TYPE_LIST, obs.OBS_COMBO_FORMAT_STRING)
    obs.obs_property_list_add_string(vm, "Fit + blurred background", "blur")
    obs.obs_property_list_add_string(vm, "Center crop (fills screen)", "crop")

    obs.obs_properties_add_bool(p, "use_obs_replay", "Also start OBS Replay Buffer when grab starts")
    obs.obs_properties_add_bool(p, "rename_obs_replays", "Rename OBS replays with channel name + move to clips folder")
    obs.obs_properties_add_button(p, "btn_obs_replay", "Save OBS Replay Buffer now", btn_obs_replay)

    obs.obs_properties_add_bool(p, "auto_start", "Auto-start grab when OBS opens")
    obs.obs_properties_add_int(p, "dock_port", "Dock port", 1025, 65000, 1)
    obs.obs_properties_add_int(p, "port", "Local UDP port (stream feed)", 1025, 65000, 1)
    obs.obs_properties_add_text(p, "extra_args", "Extra Streamlink args (advanced)", obs.OBS_TEXT_DEFAULT)
    obs.obs_properties_add_path(p, "streamlink_path", "streamlink.exe (blank = auto-detect)", obs.OBS_PATH_FILE, "Programs (*.exe);;All files (*.*)", None)
    obs.obs_properties_add_path(p, "ffmpeg_path", "ffmpeg.exe (blank = auto-detect)", obs.OBS_PATH_FILE, "Programs (*.exe);;All files (*.*)", None)
    return p


def script_update(s):
    SETTINGS[0] = s
    for k in ("url", "quality", "scene", "source_name", "clips_dir", "vertical_mode",
              "extra_args", "streamlink_path", "ffmpeg_path"):
        CFG[k] = obs.obs_data_get_string(s, k)
    for k in ("low_latency", "vertical", "vertical_only", "use_obs_replay", "rename_obs_replays", "auto_start", "update_auto", "record_with_grab"):
        CFG[k] = obs.obs_data_get_bool(s, k)
    for k in ("clip_seconds", "post_roll", "buffer_seconds", "port", "dock_port"):
        CFG[k] = obs.obs_data_get_int(s, k)
    if not G.running:
        apply_config()
    if DOCK.httpd and DOCK.port != int(CFG["dock_port"]):
        DOCK.start(int(CFG["dock_port"]))


def script_load(s):
    SETTINGS[0] = s
    for key, desc, cb in HOTKEY_DEFS:
        hid = obs.obs_hotkey_register_frontend(key, desc, cb)
        HOTKEYS[key] = hid
        arr = obs.obs_data_get_array(s, key)
        obs.obs_hotkey_load(hid, arr)
        obs.obs_data_array_release(arr)
    obs.obs_frontend_add_event_callback(on_frontend_event)
    obs.timer_add(tick, 250)
    obs.timer_add(_autostart_once, 3000)
    obs.timer_add(_auto_update_check, 8000)
    DOCK.start(int(cfg("dock_port", 5578) or 5578))
    log("Loaded v%s" % VERSION)


def script_save(s):
    for key, hid in HOTKEYS.items():
        arr = obs.obs_hotkey_save(hid)
        obs.obs_data_set_array(s, key, arr)
        obs.obs_data_array_release(arr)


def script_unload():
    G.stop()
    DOCK.stop()
    try:
        obs.timer_remove(tick)
        obs.obs_frontend_remove_event_callback(on_frontend_event)
    except Exception:
        pass
