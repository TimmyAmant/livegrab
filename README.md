<p align="center"><img src="docs/img/icon.svg" width="80" alt="LiveGrab icon"></p>
<h1 align="center">LiveGrab for OBS</h1>
<p align="center">Clip anyone's Twitch, Kick or YouTube stream <b>while they're live</b>, right inside OBS.<br>One key saves the clip, with a 9:16 version ready for TikTok, Shorts and Reels.</p>
<p align="center">
  <a href="https://github.com/TimmyAmant/livegrab/releases/latest/download/LiveGrabSetup.exe"><b>Download for Windows</b></a> ·
  <a href="https://timmyamant.github.io/livegrab/">Website</a> ·
  <a href="https://github.com/TimmyAmant/livegrab/releases">Release notes</a>
</p>

<p align="center"><a href="https://timmyamant.github.io/livegrab/"><b>Try the live demo of the dock on the website</b></a></p>

## How it works

1. **Type a channel** (like `xqc`, `kick.com/name` or `youtube.com/@name/live`) into the LiveGrab dock and click **Start**.
2. **Their stream appears in OBS**, and LiveGrab keeps the last couple of minutes saved in the background.
3. **Press F9** to save the last 30 seconds. A progress bar shows how long until it's done.

## Features

- **Save as 16:9, 9:16 only, or both.** Blurred background or crop to fill.
- **Keep rolling** for 5 to 15 seconds after you press, so you catch the payoff.
- **Original quality.** The widescreen clip is cut straight from the stream with no re-encoding.
- **Save anywhere.** Folder picker plus one-tap Videos, Desktop or another drive.
- **Works with OBS Replay Buffer and recording.** Start and Stop can turn them on and off.
- **Updates itself.** New versions download in the background and install when you close OBS.

## Install

1. Close OBS.
2. Download and run **[LiveGrabSetup.exe](https://github.com/TimmyAmant/livegrab/releases/latest/download/LiveGrabSetup.exe)**.
3. Open OBS. It starts on the **LiveGrab** profile with the dock on the right.

The installer adds OBS, Python 3.12 and Streamlink if they're missing, creates a LiveGrab profile and scene collection, and backs up your OBS settings first. Your other profiles and scenes aren't touched.

> Windows may say "Windows protected your PC" because the installer isn't code-signed yet. Click **More info**, then **Run anyway**.

**Hotkeys:** F9 clip · F10 save OBS replay · F8 start/stop (change them in OBS Settings > Hotkeys, search "LiveGrab").

**Requirements:** Windows 10 or 11, OBS Studio 30 or newer. An NVIDIA GPU makes the 9:16 export fastest.

## Uninstall

Windows Settings > Apps > **LiveGrab for OBS** > Uninstall.

## Please clip responsibly

Clips of someone else's stream are their content. Get the streamer's permission or follow each platform's clip rules before posting.

---
Made by LiveGrab. Not affiliated with OBS, Twitch, Kick or YouTube.
