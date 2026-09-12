#!/usr/bin/env python3
# /// script
# dependencies = ["pyudev"]
# ///
"""
watch_and_rip.py

Watches for an optical disc insertion, prompts you (via zenity popups) for
the title/type, rips it with MakeMKV, encodes with FFmpeg (NVENC), and
rsyncs the finished file(s) to the media server.

Run this in a terminal and leave it running. Insert a disc, answer the
prompts, walk away.

Requires: makemkv-bin (or makemkv), ffmpeg, ffprobe, zenity, rsync, pyudev
  sudo apt install ffmpeg zenity rsync python3-pyudev
  (MakeMKV: see https://www.makemkv.com/forum2/viewtopic.php?f=3&t=224)
"""

import os
import shlex
import subprocess
import sys
import shutil
import time
from pathlib import Path

import pyudev

import config as cfg


def notify(message, title="Disc Ripper"):
    subprocess.run(["zenity", "--info", "--text", message, "--title", title])


def zenity_entry(prompt, title="Disc Ripper"):
    result = subprocess.run(
        ["zenity", "--entry", "--text", prompt, "--title", title],
        capture_output=True, text=True
    )
    if result.returncode != 0:
        return None
    return result.stdout.strip()


def zenity_choice(prompt, options, title="Disc Ripper"):
    result = subprocess.run(
        ["zenity", "--list", "--radiolist", "--text", prompt,
         "--column", "", "--column", "Option",
         *sum([["TRUE" if i == 0 else "FALSE", opt] for i, opt in enumerate(options)], []),
         "--title", title, "--hide-header"],
        capture_output=True, text=True
    )
    if result.returncode != 0:
        return None
    return result.stdout.strip()


def zenity_progress_pulse(message, title="Disc Ripper"):
    """Returns a Popen handle to a pulsing progress dialog. Kill it when done."""
    return subprocess.Popen(
        ["zenity", "--progress", "--pulsate", "--no-cancel",
         "--text", message, "--title", title, "--auto-close"]
    )


def wait_for_disc():
    """Blocks until an optical disc is inserted. Returns the device node."""
    context = pyudev.Context()
    monitor = pyudev.Monitor.from_netlink(context)
    monitor.filter_by(subsystem="block")

    print("Waiting for a disc to be inserted...")
    for device in iter(monitor.poll, None):
        if device.action != "change":
            continue
        if device.get("ID_CDROM_MEDIA") == "1":
            print(f"Disc detected: {device.device_node}")
            return device.device_node


def rip_disc(raw_out_dir):
    """Runs makemkvcon to rip all titles over the min-length threshold."""
    os.makedirs(raw_out_dir, exist_ok=True)
    progress = zenity_progress_pulse("Ripping disc with MakeMKV — this can take a while...")
    try:
        subprocess.run(
            [
                "makemkvcon", "mkv", "disc:0", "all", raw_out_dir,
                f"--minlength={cfg.MAKEMKV_MIN_LENGTH_SECONDS}",
            ],
            check=True,
        )
    finally:
        progress.terminate()


def get_duration_seconds(filepath):
    result = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration",
         "-of", "default=noprint_wrappers=1:nokey=1", str(filepath)],
        capture_output=True, text=True
    )
    try:
        return float(result.stdout.strip())
    except ValueError:
        return 0.0


def encode_file(input_path, output_path):
    os.makedirs(os.path.dirname(output_path), exist_ok=True)
    progress = zenity_progress_pulse(f"Encoding {os.path.basename(output_path)}...")
    try:
        subprocess.run(
            [
                "ffmpeg", "-y", "-i", str(input_path),
                "-c:v", cfg.VIDEO_CODEC, "-cq", cfg.VIDEO_QUALITY,
                "-c:a", cfg.AUDIO_CODEC, "-b:a", cfg.AUDIO_BITRATE,
                str(output_path),
            ],
            check=True,
        )
    finally:
        progress.terminate()


def rsync_to_server(local_path, remote_dir):
    remote_target = f"{cfg.SSH_USER}@{cfg.SSH_HOST}:{remote_dir}/"
    subprocess.run(
        ["ssh", f"{cfg.SSH_USER}@{cfg.SSH_HOST}", "mkdir", "-p", shlex.quote(remote_dir)],
        check=True,
    )
    subprocess.run(["rsync", "-avh", "--progress", str(local_path), remote_target], check=True)


def cleanup_encoded_file(encoded_path):
    """Removes the local encoded file (and any now-empty parent folders under
    ENCODED_DIR) after it's been confirmed sent to the server."""
    encoded_path = Path(encoded_path)
    encoded_path.unlink(missing_ok=True)

    encoded_root = Path(cfg.ENCODED_DIR)
    parent = encoded_path.parent
    while parent != encoded_root and encoded_root in parent.parents and not any(parent.iterdir()):
        parent.rmdir()
        parent = parent.parent


def eject_disc(device):
    subprocess.run(["eject", device])


def sanitize(name):
    return "".join(c for c in name if c not in '/\\:*?"<>|').strip()


def handle_movie(raw_dir):
    title = zenity_entry("Movie title:")
    if not title:
        notify("No title entered - aborting.")
        return
    year = zenity_entry("Release year:")
    if not year:
        notify("No year entered - aborting.")
        return

    title = sanitize(title)
    year = sanitize(year)

    # Find the ripped file with the longest runtime - almost always the movie itself
    mkv_files = list(Path(raw_dir).glob("*.mkv"))
    if not mkv_files:
        notify("No files were ripped - check MakeMKV output.")
        return

    mkv_files.sort(key=get_duration_seconds, reverse=True)
    main_file = mkv_files[0]

    folder_name = f"{title} ({year})"
    output_name = f"{folder_name}.mkv"
    encoded_path = Path(cfg.ENCODED_DIR) / folder_name / output_name

    encode_file(main_file, encoded_path)
    rsync_to_server(encoded_path, f"{cfg.REMOTE_MOVIES_PATH}/{folder_name}")
    cleanup_encoded_file(encoded_path)

    notify(f"Done! {folder_name} has been encoded and sent to the media server.")


def handle_tv(raw_dir):
    show_name = zenity_entry("Show name:")
    if not show_name:
        notify("No show name entered - aborting.")
        return
    show_name = sanitize(show_name)

    season = zenity_entry("Season number (e.g. 1):")
    if not season:
        notify("No season entered - aborting.")
        return
    season_num = int(season)

    mkv_files = sorted(Path(raw_dir).glob("*.mkv"), key=get_duration_seconds, reverse=True)
    if not mkv_files:
        notify("No files were ripped - check MakeMKV output.")
        return

    for f in mkv_files:
        duration_min = get_duration_seconds(f) / 60
        keep = zenity_choice(
            f"File: {f.name}\nDuration: {duration_min:.0f} min\n\nIs this an episode?",
            ["Yes - it's an episode", "No - skip this file"]
        )
        if not keep or keep.startswith("No"):
            continue

        episode = zenity_entry(f"Episode number for {f.name} (e.g. 1):")
        if not episode:
            continue
        episode_num = int(episode)

        season_folder = f"Season {season_num:02d}"
        episode_filename = f"{show_name} S{season_num:02d}E{episode_num:02d}.mkv"
        encoded_path = Path(cfg.ENCODED_DIR) / show_name / season_folder / episode_filename

        encode_file(f, encoded_path)
        rsync_to_server(
            encoded_path,
            f"{cfg.REMOTE_TV_PATH}/{show_name}/{season_folder}"
        )
        cleanup_encoded_file(encoded_path)

    notify(f"Done! Episodes for {show_name} have been encoded and sent to the media server.")


def main():
    os.makedirs(cfg.WORK_DIR, exist_ok=True)

    while True:
        device = wait_for_disc()
        time.sleep(2)  # let the disc finish spinning up before MakeMKV touches it

        content_type = zenity_choice(
            "What's on this disc?",
            ["Movie", "TV Show"]
        )
        if not content_type:
            eject_disc(device)
            continue

        raw_dir = os.path.join(cfg.RAW_RIP_DIR, str(int(time.time())))
        rip_disc(raw_dir)

        if content_type == "Movie":
            handle_movie(raw_dir)
        else:
            handle_tv(raw_dir)

        # Clean up raw rip to save disk space now that encoding is done
        shutil.rmtree(raw_dir, ignore_errors=True)

        eject_disc(device)


if __name__ == "__main__":
    main()
