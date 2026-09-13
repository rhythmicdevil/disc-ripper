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


def notify(message, title="Disc Ripper", device=None):
    """Shows a blocking info dialog. If `device` is given, rings the bell and
    ejects the disc right before the dialog appears, so the audible/physical
    signal fires when the user's attention is actually needed - not after
    they've already noticed and dismissed the dialog on their own."""
    if device:
        ring_bell()
        eject_disc(device)
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


def encode_file(input_path, output_path, label=None):
    os.makedirs(os.path.dirname(output_path), exist_ok=True)
    progress = zenity_progress_pulse(f"Encoding {label or os.path.basename(output_path)}...")
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


def encode_and_send(local_path, encoded_path, remote_dir, item_label):
    """Encodes one file and transfers it to the server. Returns True on
    success; on any subprocess failure (ffmpeg/ssh/rsync), notifies the user
    with details and returns False so the caller can skip this item and keep
    going instead of taking down the whole watcher."""
    try:
        encode_file(local_path, encoded_path, label=item_label)
        rsync_to_server(encoded_path, remote_dir)
        cleanup_encoded_file(encoded_path)
        return True
    except subprocess.CalledProcessError as e:
        notify(f"Failed to encode/send {item_label}: '{e.cmd[0]}' exited with "
               f"code {e.returncode}. Skipping this file - check the terminal.")
        print(f"Command failed for {item_label}: {e}", file=sys.stderr)
        return False


def eject_disc(device):
    subprocess.run(["eject", device])


def ring_bell(times=2):
    """Best-effort audible 'done' signal via the terminal bell. Combined with
    the tray physically opening, this covers you whether you're watching the
    drive or just listening from another room. Never raises - a muted
    terminal bell shouldn't be treated as a pipeline failure."""
    for _ in range(times):
        print("\a", end="", flush=True)
        time.sleep(0.3)


def sanitize(name):
    return "".join(c for c in name if c not in '/\\:*?"<>|').strip()


def prompt_required(prompt_text, field_label, device):
    """Prompts for a value. On empty input, notifies, ejects the disc, and
    returns None so the caller can abort this disc and continue the loop."""
    value = zenity_entry(prompt_text)
    if not value:
        notify(f"No {field_label} entered - aborting.")
        eject_disc(device)
        return None
    return value


def prompt_required_int(prompt_text, field_label, device):
    """Like prompt_required, but also validates the input parses as an int."""
    value = prompt_required(prompt_text, field_label, device)
    if value is None:
        return None
    try:
        return int(value)
    except ValueError:
        notify(f"'{value}' isn't a valid number - aborting.")
        eject_disc(device)
        return None


def handle_movie(raw_dir, title, year, device):
    # Find the ripped file with the longest runtime - almost always the movie itself
    mkv_files = list(Path(raw_dir).glob("*.mkv"))
    if not mkv_files:
        notify("No files were ripped - check MakeMKV output.", device=device)
        return

    mkv_files.sort(key=get_duration_seconds, reverse=True)
    main_file = mkv_files[0]

    folder_name = f"{title} ({year})"
    output_name = f"{folder_name}.mkv"
    encoded_path = Path(cfg.ENCODED_DIR) / folder_name / output_name

    if encode_and_send(main_file, encoded_path, f"{cfg.REMOTE_MOVIES_PATH}/{folder_name}", folder_name):
        notify(f"Done! {folder_name} has been encoded and sent to the media server.", device=device)


def handle_tv(raw_dir, show_name, season_num, device):
    mkv_files = sorted(Path(raw_dir).glob("*.mkv"), key=get_duration_seconds, reverse=True)
    if not mkv_files:
        notify("No files were ripped - check MakeMKV output.", device=device)
        return

    # Ask about every ripped title up front, so we know the total transfer
    # count before encoding starts and can walk away for the whole batch.
    episodes = []
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
        try:
            episode_num = int(episode)
        except ValueError:
            notify(f"'{episode}' isn't a valid episode number - skipping {f.name}.")
            continue

        episodes.append((f, episode_num))

    total = len(episodes)
    if total == 0:
        notify(f"No episodes selected for {show_name} - nothing to transfer.", device=device)
        return

    transferred = 0
    for f, episode_num in episodes:
        season_folder = f"Season {season_num:02d}"
        episode_filename = f"{show_name} S{season_num:02d}E{episode_num:02d}.mkv"
        encoded_path = Path(cfg.ENCODED_DIR) / show_name / season_folder / episode_filename

        label = f"{episode_filename} ({transferred + 1} of {total})"
        if encode_and_send(f, encoded_path, f"{cfg.REMOTE_TV_PATH}/{show_name}/{season_folder}", label):
            transferred += 1

    notify(f"Done! {transferred} of {total} episode(s) for {show_name} transferred to the media server.",
           device=device)


def main():
    os.makedirs(cfg.WORK_DIR, exist_ok=True)
    print("Disc ripper running. Press Ctrl+C to stop.")

    try:
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

            # Collect metadata up front, before the (long) rip runs.
            if content_type == "Movie":
                title = prompt_required("Movie title:", "title", device)
                if title is None:
                    continue
                year = prompt_required("Release year:", "year", device)
                if year is None:
                    continue
                title, year = sanitize(title), sanitize(year)
            else:
                show_name = prompt_required("Show name:", "show name", device)
                if show_name is None:
                    continue
                show_name = sanitize(show_name)

                season_num = prompt_required_int("Season number (e.g. 1):", "season", device)
                if season_num is None:
                    continue

            raw_dir = os.path.join(cfg.RAW_RIP_DIR, str(int(time.time())))
            try:
                rip_disc(raw_dir)
            except subprocess.CalledProcessError as e:
                notify(f"MakeMKV failed (exit code {e.returncode}) - aborting this disc. "
                       "Check the terminal for details.", device=device)
                print(f"Command failed: {e}", file=sys.stderr)
            else:
                if content_type == "Movie":
                    handle_movie(raw_dir, title, year, device)
                else:
                    handle_tv(raw_dir, show_name, season_num, device)

            # Clean up raw rip to save disk space now that encoding is done
            shutil.rmtree(raw_dir, ignore_errors=True)
    except KeyboardInterrupt:
        print("\nCtrl+C received - shutting down.")
        sys.exit(0)


if __name__ == "__main__":
    main()
