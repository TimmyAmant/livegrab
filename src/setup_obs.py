"""
LiveGrab OBS setup.
Creates the "LiveGrab" profile + scene collection, points OBS at Python,
loads livegrab.py, and adds the LiveGrab dock.
Run while OBS is closed. Safe to re-run (it updates in place and backs up).
"""
import os
import sys
import json
import uuid
import shutil
import argparse
import datetime
import subprocess

PROFILE_NAME = "LiveGrab"
PROFILE_DIR = "LiveGrab"
COLLECTION_NAME = "LiveGrab"
COLLECTION_FILE = "LiveGrab"
DOCK_TITLE = "LiveGrab"
DOCK_PORT = 5578
DOCK_URL = "http://127.0.0.1:%d/" % DOCK_PORT
STANDBY_URL = "http://127.0.0.1:%d/standby" % DOCK_PORT
UDP_URL = "udp://127.0.0.1:5577?overrun_nonfatal=1&fifo_size=50000000"


def say(msg):
    print(msg)
    sys.stdout.flush()


def fwd(p):
    return p.replace("\\", "/")


# ---------------------------------------------------------------------------
# Order-preserving INI editor (OBS style: keys are case-sensitive, values raw)
# ---------------------------------------------------------------------------
class Ini(object):
    def __init__(self, path):
        self.path = path
        self.sections = []  # [name, [[key, value] | [None, rawline]]]
        if os.path.exists(path):
            with open(path, "r", encoding="utf-8-sig", errors="replace") as f:
                cur = None
                for line in f.read().splitlines():
                    s = line.strip()
                    if s.startswith("[") and s.endswith("]"):
                        cur = [s[1:-1], []]
                        self.sections.append(cur)
                    elif cur is None:
                        continue
                    elif "=" in line and not s.startswith(";") and not s.startswith("#"):
                        k, v = line.split("=", 1)
                        cur[1].append([k, v])
                    elif s:
                        cur[1].append([None, line])

    def _sec(self, name, create=True):
        for sec in self.sections:
            if sec[0] == name:
                return sec
        if not create:
            return None
        sec = [name, []]
        self.sections.append(sec)
        return sec

    def get(self, section, key, default=None):
        sec = self._sec(section, False)
        if sec:
            for kv in sec[1]:
                if kv[0] == key:
                    return kv[1]
        return default

    def set(self, section, key, value):
        sec = self._sec(section)
        for kv in sec[1]:
            if kv[0] == key:
                kv[1] = str(value)
                return
        sec[1].append([key, str(value)])

    def remove(self, section, key):
        sec = self._sec(section, False)
        if sec:
            sec[1] = [kv for kv in sec[1] if kv[0] != key]

    def save(self):
        os.makedirs(os.path.dirname(self.path), exist_ok=True)
        out = []
        for name, items in self.sections:
            out.append("[%s]" % name)
            for k, v in items:
                out.append(v if k is None else "%s=%s" % (k, v))
            out.append("")
        with open(self.path, "w", encoding="utf-8", newline="\n") as f:
            f.write("\n".join(out))


def backup(path, stamp):
    if os.path.exists(path):
        dst = path + ".livegrab-backup-" + stamp
        shutil.copy2(path, dst)
        return dst
    return None


# ---------------------------------------------------------------------------
# Scene collection builders
# ---------------------------------------------------------------------------
def new_uuid():
    return str(uuid.uuid4())


def source(name, sid, settings, **extra):
    s = {
        "name": name, "uuid": new_uuid(), "id": sid, "versioned_id": sid,
        "settings": settings, "mixers": 255, "sync": 0, "flags": 0,
        "volume": 1.0, "balance": 0.5, "enabled": True, "muted": False,
        "push-to-mute": False, "push-to-mute-delay": 0,
        "push-to-talk": False, "push-to-talk-delay": 0,
        "hotkeys": {}, "deinterlace_mode": 0, "deinterlace_field_order": 0,
        "monitoring_type": 0, "private_settings": {},
    }
    s.update(extra)
    return s


def item(src, item_id, w, h):
    return {
        "name": src["name"], "source_uuid": src["uuid"], "visible": True, "locked": False,
        "rot": 0.0, "pos": {"x": 0.0, "y": 0.0}, "scale": {"x": 1.0, "y": 1.0},
        "align": 5, "bounds_type": 2, "bounds_align": 0,
        "bounds": {"x": float(w), "y": float(h)},
        "crop_left": 0, "crop_top": 0, "crop_right": 0, "crop_bottom": 0,
        "id": item_id, "group_item_backup": False, "scale_filter": "disable",
        "blend_method": "default", "blend_type": "normal",
        "show_transition": {"duration": 0}, "hide_transition": {"duration": 0},
        "private_settings": {},
    }


def scene(name, items):
    return source(name, "scene", {"id_counter": len(items), "custom_size": False, "items": items})


def script_settings(clips_dir):
    return {
        "url": "", "quality": "best", "low_latency": True,
        "scene": "Watching", "source_name": "LiveGrab Feed",
        "clip_seconds": 30, "post_roll": 0, "buffer_seconds": 120,
        "clips_dir": fwd(clips_dir), "vertical": True, "vertical_only": False, "vertical_mode": "blur",
        "use_obs_replay": True, "rename_obs_replays": True,
        "port": 5577, "dock_port": DOCK_PORT, "auto_start": False,
        # default hotkeys: F9 clip, F10 save OBS replay, F8 start/stop
        "livegrab.clip": [{"key": "OBS_KEY_F9"}],
        "livegrab.obs_replay": [{"key": "OBS_KEY_F10"}],
        "livegrab.toggle": [{"key": "OBS_KEY_F8"}],
    }


def build_collection(w, h, script_path, clips_dir):
    grab = source("LiveGrab Feed", "ffmpeg_source", {
        "is_local_file": False, "input": UDP_URL, "input_format": "mpegts",
        "buffering_mb": 2, "reconnect_delay_sec": 1, "restart_on_activate": False,
        "close_when_inactive": False, "clear_on_media_end": True, "hw_decode": True,
    }, monitoring_type=2)  # monitor and output: you hear it, recordings get it
    standby = source("Standby Screen", "browser_source", {
        "url": STANDBY_URL, "width": w, "height": h, "fps": 30,
        "reroute_audio": False, "shutdown": False, "restart_when_active": False,
    })
    desktop = source("Desktop Audio", "wasapi_output_capture", {"device_id": "default"}, muted=True)
    mic = source("Mic/Aux", "wasapi_input_capture", {"device_id": "default"}, muted=True)

    s_live = scene("Watching", [item(standby, 1, w, h), item(grab, 2, w, h)])
    s_standby = scene("Standby", [item(standby, 1, w, h)])

    return {
        "name": COLLECTION_NAME,
        "current_scene": "Watching", "current_program_scene": "Watching",
        "scene_order": [{"name": "Watching"}, {"name": "Standby"}],
        "sources": [s_live, s_standby, grab, standby],
        "groups": [], "transitions": [], "quick_transitions": [],
        "current_transition": "Fade", "transition_duration": 300,
        "saved_projectors": [], "preview_locked": False, "scaling_enabled": False,
        "DesktopAudioDevice1": desktop, "AuxAudioDevice1": mic,
        "modules": {"scripts-tool": [{"path": fwd(script_path), "settings": script_settings(clips_dir)}]},
    }


# ---------------------------------------------------------------------------
# Profile
# ---------------------------------------------------------------------------
def has_nvidia():
    if os.name != "nt":
        return False
    try:
        r = subprocess.run(["powershell", "-NoProfile", "-Command",
                            "(Get-CimInstance Win32_VideoController).Name -join ';'"],
                           capture_output=True, text=True, timeout=30, creationflags=0x08000000)
        return "nvidia" in (r.stdout or "").lower()
    except Exception:
        return False


def write_profile(profiles_root, clips_dir, encoder):
    d = os.path.join(profiles_root, PROFILE_DIR)
    os.makedirs(d, exist_ok=True)
    ini = Ini(os.path.join(d, "basic.ini"))
    vals = {
        "General": {"Name": PROFILE_NAME},
        "Video": {"BaseCX": 1920, "BaseCY": 1080, "OutputCX": 1920, "OutputCY": 1080,
                  "FPSType": 0, "FPSCommon": 60, "ScaleType": "bicubic", "ColorFormat": "NV12",
                  "ColorSpace": "709", "ColorRange": "Partial"},
        "Audio": {"SampleRate": 48000, "ChannelSetup": "Stereo",
                  "MonitoringDeviceId": "default", "MonitoringDeviceName": "Default"},
        "Output": {"Mode": "Simple"},
        "SimpleOutput": {"FilePath": fwd(clips_dir), "RecFormat2": "mp4", "RecQuality": "Small",
                         "RecEncoder": encoder, "StreamEncoder": encoder, "VBitrate": 6000,
                         "ABitrate": 160, "RecRB": "true", "RecRBTime": 60, "RecRBSize": 512,
                         "RecRBPrefix": "Replay", "FileNameWithoutSpace": "true"},
    }
    fresh = not os.path.exists(os.path.join(d, "basic.ini"))
    for sec, kv in vals.items():
        for k, v in kv.items():
            if fresh or ini.get(sec, k) is None:
                ini.set(sec, k, v)
    ini.save()
    return d


# ---------------------------------------------------------------------------
# user.ini / global.ini
# ---------------------------------------------------------------------------
def add_dock(ini):
    raw = ini.get("BasicWindow", "ExtraBrowserDocks") or "[]"
    try:
        docks = json.loads(raw)
        if not isinstance(docks, list):
            docks = []
    except Exception:
        docks = []
    keep_uuid = None
    rest = []
    for d in docks:
        if d.get("title") in (DOCK_TITLE, "Live Clipper") or str(d.get("url", "")).rstrip("/").endswith(":%d" % DOCK_PORT):
            keep_uuid = keep_uuid or d.get("uuid")  # keeps its saved spot in your layout
        else:
            rest.append(d)
    rest.append({"title": DOCK_TITLE, "url": DOCK_URL, "uuid": keep_uuid or new_uuid()})
    docks = rest
    ini.set("BasicWindow", "ExtraBrowserDocks", json.dumps(docks, separators=(",", ":")))


def remove_dock(ini):
    raw = ini.get("BasicWindow", "ExtraBrowserDocks")
    if not raw:
        return
    try:
        docks = [d for d in json.loads(raw) if d.get("title") not in (DOCK_TITLE, "Live Clipper")]
        ini.set("BasicWindow", "ExtraBrowserDocks", json.dumps(docks, separators=(",", ":")))
    except Exception:
        pass


def configure_app_ini(ini, python_dir, switch_to=True):
    ini.set("Python", "Path64bit", fwd(python_dir))
    add_dock(ini)
    if not switch_to:
        return
    ini.set("Basic", "Profile", PROFILE_NAME)
    ini.set("Basic", "ProfileDir", PROFILE_DIR)
    ini.set("Basic", "SceneCollection", COLLECTION_NAME)
    ini.set("Basic", "SceneCollectionFile", COLLECTION_FILE)


# ---------------------------------------------------------------------------
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--appdata", default=os.environ.get("APPDATA", os.path.expanduser("~")))
    ap.add_argument("--python-dir", default=os.path.dirname(sys.executable))
    ap.add_argument("--script", required=False)
    ap.add_argument("--clips-dir", default=os.path.join(os.path.expanduser("~"), "Videos", "LiveGrab"))
    ap.add_argument("--encoder", default="auto")
    ap.add_argument("--uninstall", action="store_true")
    ap.add_argument("--reset", action="store_true", help="rebuild the LiveGrab scenes from scratch")
    ap.add_argument("--update", action="store_true", help="silent auto-update: don't switch the active profile/scenes")
    a = ap.parse_args()

    root = os.path.join(a.appdata, "obs-studio")
    user_ini_p = os.path.join(root, "user.ini")
    global_ini_p = os.path.join(root, "global.ini")
    stamp = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")

    if a.uninstall:
        for p in (user_ini_p, global_ini_p):
            if os.path.exists(p):
                ini = Ini(p)
                remove_dock(ini)
                ini.save()
        say("Removed the LiveGrab dock. Your LiveGrab profile and scenes were left in place.")
        return 0

    if not a.script:
        say("ERROR: --script is required")
        return 2

    os.makedirs(a.clips_dir, exist_ok=True)
    os.makedirs(os.path.join(root, "basic", "scenes"), exist_ok=True)
    os.makedirs(os.path.join(root, "basic", "profiles"), exist_ok=True)

    # OBS 31+ keeps user settings in user.ini. If this OBS was never started on 31+,
    # copy global.ini across first (that's what OBS's own migration does).
    if not os.path.exists(user_ini_p) and os.path.exists(global_ini_p):
        shutil.copy2(global_ini_p, user_ini_p)
        say("Migrated global.ini -> user.ini")

    for p in (user_ini_p, global_ini_p):
        b = backup(p, stamp)
        if b:
            say("Backed up %s" % os.path.basename(b))

    # Profile
    encoder = a.encoder
    if encoder == "auto":
        encoder = "nvenc" if has_nvidia() else "x264"
    pdir = write_profile(os.path.join(root, "basic", "profiles"), a.clips_dir, encoder)
    say("Profile 'LiveGrab' ready (%s, 1080p60, replay buffer 60s) -> %s" % (encoder, pdir))

    # Scene collection
    coll_p = os.path.join(root, "basic", "scenes", COLLECTION_FILE + ".json")
    backup(coll_p, stamp)
    existing = None
    if os.path.exists(coll_p) and not a.reset:
        try:
            with open(coll_p, "r", encoding="utf-8-sig") as f:
                existing = json.load(f)
        except Exception:
            existing = None
    if existing:
        # Update: keep the user's scenes, sources and script settings; just point at the new script.
        mods = existing.setdefault("modules", {})
        scripts = [x for x in mods.get("scripts-tool", [])
                   if os.path.basename(str(x.get("path", ""))).lower() not in ("livegrab.py", "live_clipper.py")]
        old = [x for x in mods.get("scripts-tool", [])
               if os.path.basename(str(x.get("path", ""))).lower() == "livegrab.py"]
        settings = old[0].get("settings", {}) if old else script_settings(a.clips_dir)
        for k, v in script_settings(a.clips_dir).items():
            settings.setdefault(k, v)
        scripts.append({"path": fwd(a.script), "settings": settings})
        mods["scripts-tool"] = scripts
        with open(coll_p, "w", encoding="utf-8") as f:
            json.dump(existing, f, indent=4)
        say("Updated existing 'LiveGrab' scene collection (your scenes and settings were kept)")
    else:
        data = build_collection(1920, 1080, a.script, a.clips_dir)
        with open(coll_p, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=4)
        say("Scene collection 'LiveGrab' ready (scenes: Watching, Standby)")

    # App config: Python, active profile/collection, dock
    for p in (user_ini_p, global_ini_p):
        if p == global_ini_p and not os.path.exists(p):
            continue
        ini = Ini(p)
        configure_app_ini(ini, a.python_dir, switch_to=not a.update)
        ini.save()
    say("OBS set to use Python at %s" % a.python_dir)
    say("LiveGrab dock added (%s)" % DOCK_URL)
    say("DONE")
    return 0


if __name__ == "__main__":
    sys.exit(main())
