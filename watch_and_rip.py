#!/usr/bin/env python3
# /// script
# dependencies = ["pyudev", "requests"]
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

import argparse
import os
import re
import shlex
import subprocess
import sys
import shutil
import struct
import time
from contextlib import contextmanager
from pathlib import Path

import pyudev
import requests

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


def zenity_entry(prompt, title="Disc Ripper", default_text=None):
    cmd = ["zenity", "--entry", "--text", prompt, "--title", title]
    if default_text is not None:
        cmd.append(f"--entry-text={default_text}")
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        return None
    return result.stdout.strip()


def zenity_choice(prompt, options, title="Disc Ripper", default=None):
    """Asks the user to pick one of options. default is pre-selected if it's
    one of them, otherwise the first option is."""
    selected = options.index(default) if default in options else 0
    result = subprocess.run(
        ["zenity", "--list", "--radiolist", "--text", prompt,
         "--column", "", "--column", "Option",
         *sum([["TRUE" if i == selected else "FALSE", opt] for i, opt in enumerate(options)], []),
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


def ensure_all_subtitles_selected():
    """Makes sure MakeMKV never skips a subtitle track based on language.

    Out of the box, MakeMKV's default track-selection rule
    (app_DefaultSelectionString) only auto-selects audio/subtitle tracks in
    your preferred language or with no language tag - foreign-language
    subtitle tracks get silently left out of the rip. This overrides that
    rule (in ~/.MakeMKV/settings.conf) to always include every subtitle
    track regardless of language, while leaving MakeMKV's normal
    language-based filtering for audio tracks untouched. Idempotent - safe
    to call on every run."""
    key = "app_DefaultSelectionString"
    selection_string = "-sel:all,+sel:(favlang|nolang|subtitle),-sel:mvcvideo"

    settings_path = Path.home() / ".MakeMKV" / "settings.conf"
    settings_path.parent.mkdir(parents=True, exist_ok=True)

    lines = settings_path.read_text().splitlines() if settings_path.exists() else []
    lines = [line for line in lines if not line.strip().startswith(key)]
    lines.append(f'{key} = "{selection_string}"')
    settings_path.write_text("\n".join(lines) + "\n")


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


def rip_titles(raw_out_dir, title_indices):
    """Rips a specific set of already-chosen disc titles, one at a time.
    Titles are always picked from the disc's title list beforehand (see
    choose_main_title and choose_episode_titles), so trailers, duplicate
    playlists and "Play All" compilations never get ripped in the first
    place, instead of being ripped and then filtered out by hand."""
    os.makedirs(raw_out_dir, exist_ok=True)
    progress = zenity_progress_pulse(
        f"Ripping {len(title_indices)} title(s) with MakeMKV — this can take a while..."
    )
    try:
        for title_index in title_indices:
            subprocess.run(
                ["makemkvcon", "mkv", "disc:0", str(title_index), raw_out_dir],
                check=True,
            )
    finally:
        progress.terminate()


def parse_makemkv_duration(value):
    """Parses a MakeMKV robot-mode duration like '1:32:14' (H:MM:SS) into seconds."""
    try:
        parts = [int(p) for p in value.split(":")]
    except ValueError:
        return 0
    seconds = 0
    for p in parts:
        seconds = seconds * 60 + p
    return seconds


def get_makemkv_error_messages(stdout):
    """Extracts MakeMKV's own error messages (robot-mode 'MSG:' lines) from
    raw makemkvcon output. Message codes don't separate errors from
    information (5011 is "Operation successfully completed", 5050 the
    evaluation-version notice, while SCSI read errors are 2003/2004), so
    errors are picked out by their text: MakeMKV starts them with "Error",
    "Failed" or "Can't" ("Error 'Scsi error - ...' occurred while reading
    ...", "Failed to open disc", "Can't locate a cell for VTS 3 ..."). This
    matters because makemkvcon exits 0 even when it fails to open a disc
    entirely, or when read errors make it drop titles it already found - so
    a caller with a short title list has no way to tell "that's all that's
    on this disc" from "MakeMKV choked" without reading these."""
    messages = []
    for line in stdout.splitlines():
        if not line.startswith("MSG:"):
            continue
        # MSG:<code>,<flags>,<count>,"<formatted message>","<format>",... -
        # split(maxsplit=3) so commas inside the message text aren't
        # mis-split, same approach as the TINFO parsing in get_disc_titles.
        parts = line[len("MSG:"):].split(",", 3)
        if len(parts) < 4:
            continue
        # The formatted message is everything up to the '","' boundary
        # before the format-string field.
        message = parts[3].split('","', 1)[0].strip().strip('"')
        if message.startswith(("Error", "Failed", "Can't")) and message not in messages:
            messages.append(message)
    return messages


def format_makemkv_errors(error_messages, limit=5):
    """Formats MakeMKV's own error messages for appending to a zenity
    notification, showing at most `limit` of them (a bad disc can produce
    hundreds). Returns '' when there's nothing to show, so callers can
    always append the result without an extra if-check."""
    if not error_messages:
        return ""
    shown = "\n".join(f"- {m}" for m in error_messages[:limit])
    more = len(error_messages) - limit
    if more > 0:
        shown += f"\n...and {more} more (see last_disc_scan.txt in {cfg.WORK_DIR})"
    return "\n\nMakeMKV reported:\n" + shown


def makemkv_gui_running():
    """True if MakeMKV's own app is open. It scans every disc as soon as
    it's inserted, and two programs reading an encrypted DVD at once
    break each other's decryption handshake - MakeMKV then reports
    "READ OF SCRAMBLED SECTOR WITHOUT AUTHENTICATION" and drops titles."""
    return subprocess.run(["pgrep", "-f", "makemkvcon guiserver"],
                          capture_output=True).returncode == 0


def wait_for_makemkv_gui_closed(device):
    """Asks the user to close MakeMKV's app until it's closed. Returns False
    (after ejecting the disc) if they cancel instead."""
    while makemkv_gui_running():
        choice = zenity_choice(
            "MakeMKV's own window is open. It reads the disc at the same time "
            "as the ripper, which makes discs come up with missing titles.\n\n"
            "Close MakeMKV, then click OK.",
            ["I've closed it - try again", "Rip anyway"],
        )
        if not choice:
            eject_disc(device)
            return False
        if choice.startswith("Rip anyway"):
            return True
    return True


def get_disc_titles(disc_num=0, log_name="last_disc_scan.txt"):
    """Queries MakeMKV's title list for the disc (name, duration, chapter
    count, source filename, segment map, size, and audio/subtitle track
    count per title) without ripping anything - just reads the disc
    structure, so it's fast. Title indices are returned in MakeMKV's own
    order, which isn't always the disc's authoring order (see
    guess_episode_order).
    Returns a (titles, error_messages) tuple: titles is
    {title_index: {"name": str, "duration_seconds": int,
    "chapter_count": int|None, "source_filename": str|None,
    "segment_map": str|None, "size_bytes": int|None, "track_count": int}};
    error_messages is MakeMKV's own error-level output for this run (see
    get_makemkv_error_messages) - empty titles plus a non-empty
    error_messages means MakeMKV failed to open the disc, not that the disc
    has nothing on it. MakeMKV's raw output is saved to log_name in
    cfg.WORK_DIR, so a disc that gets misread can be diagnosed afterwards."""
    result = subprocess.run(
        ["makemkvcon", "-r", "info", f"disc:{disc_num}"],
        capture_output=True, text=True, check=True,
    )
    Path(cfg.WORK_DIR, log_name).write_text(result.stdout)
    titles = {}
    for line in result.stdout.splitlines():
        if line.startswith("SINFO:"):
            # SINFO:<title_index>,<stream_index>,<attribute_id>,<code>,"<value>"
            parts = line[len("SINFO:"):].split(",", 4)
            if len(parts) == 5 and parts[2] == "1" and parts[4].strip('"') in ("Audio", "Subtitles"):
                title = titles.setdefault(int(parts[0]), {})
                title["track_count"] = title.get("track_count", 0) + 1
            continue
        if not line.startswith("TINFO:"):
            continue
        # TINFO:<title_index>,<attribute_id>,<code>,"<value>" - split(maxsplit=3)
        # so commas inside a quoted value (e.g. a title name) aren't mis-split.
        parts = line[len("TINFO:"):].split(",", 3)
        if len(parts) < 4:
            continue
        index_str, attr_id_str, _code, value = parts
        try:
            index, attr_id = int(index_str), int(attr_id_str)
        except ValueError:
            continue
        value = value.strip().strip('"')

        title = titles.setdefault(index, {})
        if attr_id == 9:  # Duration
            title["duration_seconds"] = parse_makemkv_duration(value)
        elif attr_id == 2:  # Name
            title["name"] = value
        elif attr_id == 8:  # Chapter count
            title["chapter_count"] = int(value) if value.isdigit() else None
        elif attr_id == 16:  # Source filename (e.g. VTS_04_1.VOB or 00003.mpls)
            title["source_filename"] = value
        elif attr_id == 11:  # Size in bytes
            title["size_bytes"] = int(value) if value.isdigit() else None
        elif attr_id == 26:  # Segment map - the video clips/cells the title plays
            title["segment_map"] = value
    return titles, get_makemkv_error_messages(result.stdout)


def choose_main_title(titles, disc_errors, device):
    """Picks the disc title index to rip as the movie. Auto-picks the longest
    title over the min-length threshold when there's a single clear winner.
    If nothing clears the threshold, or two+ titles tie for longest (runtime
    alone can't disambiguate - e.g. a theatrical/extended pair), notifies
    the user and lets them pick instead of guessing wrong. Playlists that
    only differ in audio/subtitle tracks are dropped first (see
    drop_duplicate_titles), so they don't count as a tie. Returns None if
    there's nothing to rip or the user didn't choose - the disc has already
    been ejected by the time this returns None."""
    candidates = {
        idx: info for idx, info in drop_duplicate_titles(titles).items()
        if info.get("duration_seconds", 0) >= cfg.MAKEMKV_MIN_LENGTH_SECONDS
    }
    if not candidates:
        notify("MakeMKV didn't find any titles over the minimum length on "
               f"this disc - aborting.{format_makemkv_errors(disc_errors)}", device=device)
        return None

    by_duration = sorted(candidates.items(), key=lambda kv: kv[1]["duration_seconds"], reverse=True)
    longest = by_duration[0][1]["duration_seconds"]
    tied = [idx for idx, info in by_duration if info["duration_seconds"] == longest]

    if len(tied) == 1:
        return by_duration[0][0]

    # Don't eject here - the disc is still needed to actually rip the chosen title.
    notify("MakeMKV found multiple titles of the same length - couldn't "
           "automatically pick the main feature. Pick it on the next screen.")

    options = []
    for idx, info in by_duration:
        minutes = info["duration_seconds"] / 60
        name = info.get("name", "")
        options.append(f"Title {idx} - {minutes:.0f} min" + (f" ({name})" if name else ""))

    choice = zenity_choice("Multiple titles are the same length - which one is the movie?", options)
    if not choice:
        notify("No title selected - aborting.", device=device)
        return None
    return int(choice.split()[1])


TITLE_INDEX_RE = re.compile(r"_t(\d+)\.mkv$", re.IGNORECASE)


def source_file_number(source_filename):
    """Pulls the meaningful sequence number out of a disc source filename:
    the titleset number from a DVD name like 'VTS_04_1.VOB' (-> 4), or the
    playlist/clip number from a Blu-ray name like '00003.mpls' (-> 3).
    Authoring tools generally number these sequentially in authoring order,
    which is often - not always - episode order. Returns None if the
    filename doesn't match either pattern, so callers can treat it as
    "no signal" rather than guessing wrong."""
    if not source_filename:
        return None
    match = re.match(r"VTS_(\d+)_", source_filename, re.IGNORECASE)
    if match:
        return int(match.group(1))
    match = re.match(r"(\d+)\.\w+$", source_filename)
    return int(match.group(1)) if match else None


def guess_episode_order(mkv_files, titles):
    """Guesses each ripped file's position in episode order. When every
    title has a distinct source file number (Blu-ray playlist '00071.mpls',
    or DVD titleset 'VTS_04_1.VOB'), files are ordered by that - authoring
    tools number these sequentially, and MakeMKV's own title order can be
    scrambled (e.g. on Blu-rays that list each episode under several
    playlists). Otherwise the order falls back to MakeMKV's title order,
    recovered from its default '..._t<NN>.mkv' output naming. A title whose
    chapter count differs from the other episodes is flagged as low
    confidence. Discs get authored in all kinds of orders, so this is a
    best guess, never a final answer.

    Returns a list of (path, guessed_offset, confidence, reason) - one
    entry per file in mkv_files, ordered by the guess (files MakeMKV's
    naming couldn't be matched to a title go last, sorted by duration).
    guessed_offset is 0-based (add the disc's first episode number - see
    highest_episode_on_server); it's None when nothing could be guessed."""
    matched = []
    unmatched = []
    for f in mkv_files:
        m = TITLE_INDEX_RE.search(f.name)
        if m is None:
            unmatched.append(f)
            continue
        title_index = int(m.group(1))
        info = titles.get(title_index, {})
        matched.append({
            "path": f,
            "title_index": title_index,
            "source_number": source_file_number(info.get("source_filename")),
            "chapter_count": info.get("chapter_count"),
        })

    source_numbers = [c["source_number"] for c in matched]
    if None not in source_numbers and len(set(source_numbers)) == len(source_numbers):
        matched.sort(key=lambda c: c["source_number"])
        base_confidence, base_reason = "high", "disc file order"
    else:
        matched.sort(key=lambda c: c["title_index"])
        base_confidence, base_reason = "medium", "disc title order"

    chapter_counts = [c["chapter_count"] for c in matched if c["chapter_count"]]
    mode_chapter_count = max(set(chapter_counts), key=chapter_counts.count) if chapter_counts else None

    results = []
    for offset, c in enumerate(matched):
        reasons = [base_reason]
        confidence = base_confidence

        if mode_chapter_count is not None and c["chapter_count"] not in (None, mode_chapter_count):
            confidence = "low"
            reasons.append(
                f"chapter count ({c['chapter_count']}) differs from the "
                f"other {mode_chapter_count}-chapter episodes"
            )

        results.append((c["path"], offset, confidence, ", ".join(reasons)))

    unmatched.sort(key=get_duration_seconds, reverse=True)
    for f in unmatched:
        results.append((f, None, "low", "couldn't match this file back to a disc title"))

    return results


def tmdb_get(path, params=None):
    """Minimal TMDB v3 API GET helper. Returns the parsed JSON body, or None
    on any failure - no API key configured, network error, non-2xx response
    - so callers fall back to skipping the episode name instead of failing
    the whole rip over a metadata lookup."""
    if not cfg.TMDB_API_KEY:
        return None
    try:
        response = requests.get(
            f"https://api.themoviedb.org/3{path}",
            params={**(params or {}), "api_key": cfg.TMDB_API_KEY},
            timeout=10,
        )
        response.raise_for_status()
        return response.json()
    except requests.RequestException:
        return None


def tmdb_find_show_id(show_name):
    """Looks up a TV show's TMDB id by name. Returns None if it can't be
    found (or TMDB isn't configured/reachable) - callers should treat that
    as "no episode names available" rather than an error."""
    data = tmdb_get("/search/tv", {"query": show_name})
    results = (data or {}).get("results") or []
    return results[0]["id"] if results else None


def tmdb_get_episode_title(show_id, season_num, episode_num):
    """Looks up one episode's title on TMDB. Returns None if the show id is
    unknown, the season/episode doesn't exist, or the lookup otherwise
    fails - callers should fall back to a filename with no episode title."""
    if show_id is None:
        return None
    data = tmdb_get(f"/tv/{show_id}/season/{season_num}/episode/{episode_num}")
    return (data or {}).get("name") or None


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
            stdin=subprocess.DEVNULL,
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


def highest_episode_on_server(remote_dir, season_num):
    """Returns the highest episode number already in a season folder on the
    media server (from "SxxEyy" in the filenames - for a multi-episode file
    like "S01E03-E04" the last number counts), 0 if the folder is missing or
    has no episodes, or None if the server couldn't be reached. Seasons span
    several discs and each disc's titles are numbered from scratch, so this
    is where the next disc's episode numbering picks up from."""
    result = subprocess.run(
        ["ssh", f"{cfg.SSH_USER}@{cfg.SSH_HOST}",
         f"ls -1 {shlex.quote(remote_dir)} 2>/dev/null || true"],
        capture_output=True, text=True,
    )
    if result.returncode != 0:
        print(f"Couldn't list {remote_dir} on the server: {result.stderr.strip()}",
              file=sys.stderr)
        return None

    episode_re = re.compile(rf"S0*{season_num}E(\d+)(?:-?E(\d+))?", re.IGNORECASE)
    highest = 0
    for name in result.stdout.splitlines():
        match = episode_re.search(name)
        if match:
            highest = max(highest, *(int(n) for n in match.groups() if n))
    return highest


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
    # device is None when processing a folder of existing rips (--from-folder).
    if device:
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


def prompt_required(prompt_text, field_label, device, default_text=None):
    """Prompts for a value, pre-filled with default_text if given. On empty
    input, notifies, ejects the disc, and returns None so the caller can
    abort this disc and continue the loop."""
    value = zenity_entry(prompt_text, default_text=default_text)
    if not value:
        notify(f"No {field_label} entered - aborting.")
        eject_disc(device)
        return None
    return value


def prompt_required_int(prompt_text, field_label, device, default_text=None):
    """Like prompt_required, but also validates the input parses as an int."""
    value = prompt_required(prompt_text, field_label, device, default_text=default_text)
    if value is None:
        return None
    try:
        return int(value)
    except ValueError:
        notify(f"'{value}' isn't a valid number - aborting.")
        eject_disc(device)
        return None


def prompt_tmdb_show_id(default_id=None):
    """Asks for a TMDB show id to use instead of searching TMDB by name -
    optional, since most shows resolve fine by name. Useful when a show has
    several TMDB entries (e.g. a remake with a near-identical title) and the
    name search finds the wrong one, tagging episodes with another show's
    names. Find the id in a show's themoviedb.org URL: .../tv/<id>-<slug>.
    The entry is pre-filled with default_id if given.
    Returns None (search by name, same as before this existed) if left
    blank, cancelled, or not a plain number."""
    value = zenity_entry(
        "TMDB show ID, from its themoviedb.org URL (optional - leave blank "
        "to find it by name):",
        default_text=default_id,
    )
    if not value:
        return None
    if value.strip().isdigit():
        return int(value.strip())
    notify(f"'{value}' isn't a valid TMDB id - looking it up by name instead.")
    return None


def is_bluray_title(info):
    return (info.get("source_filename") or "").lower().endswith(".mpls")


def parse_clip_ids(segment_map):
    """Parses a Blu-ray title's segment map into the set of raw .m2ts clip
    ids it plays (e.g. "1100,1062" -> {1100, 1062}). These ids are global to
    the disc, unlike a DVD's segment map, which numbers cells relative to
    each title (every episode can be "1-6"). Returns None if there's no
    segment map or it isn't a plain comma-separated list of integers."""
    if not segment_map:
        return None
    try:
        return frozenset(int(p) for p in segment_map.split(","))
    except ValueError:
        return None


def drop_duplicate_titles(titles):
    """Removes titles that play the same video as another title, keeping
    one copy of each.

    On Blu-ray, two titles are the same video whenever their clip sets
    overlap completely - not only when they're identical. Discs often list
    the same episode under several playlists that add something extra on
    top of the same core clips: a French/Japanese dub with different
    subtitle tracks (same clips, same size - an exact duplicate), a short
    bumper/logo clip spliced onto the front (same clips plus one more, a few
    seconds and a few MB different - a partial duplicate), or a "Play All"
    title that chains every episode's clips into one compilation (every
    episode's clips, plus more). In every case the smaller clip set is the
    clean copy and any title whose clips are a strict superset of it is
    dropped, since it's just that content plus filler.

    On DVD, segment map numbers are cell numbers relative to each title, so
    they aren't globally unique and overlap alone proves nothing - there,
    two titles only count as the same video when their segment map and size
    both match exactly, as before.

    Of each set of exact duplicates, the one with the most audio + subtitle
    tracks is kept (ties go to the lowest title index). Titles with no
    segment map or size are always kept as-is.
    Returns a new {title_index: info} dict."""
    kept = {}
    by_video = {}
    for idx, info in sorted(titles.items()):
        clips = parse_clip_ids(info.get("segment_map")) if is_bluray_title(info) else None
        video = clips if clips else (info.get("segment_map"), info.get("size_bytes"))
        if not clips and None in video:
            kept[idx] = info
            continue
        current = by_video.get(video)
        if current is None or info.get("track_count", 0) > titles[current].get("track_count", 0):
            by_video[video] = idx
    for idx in by_video.values():
        kept[idx] = titles[idx]

    clip_sets = {idx: clips for idx, info in kept.items()
                 if is_bluray_title(info) and (clips := parse_clip_ids(info.get("segment_map")))}
    final = {
        idx: info for idx, info in kept.items()
        if idx not in clip_sets or not any(
            idx2 != idx and other < clip_sets[idx] for idx2, other in clip_sets.items()
        )
    }

    dropped = sorted(set(titles) - set(final))
    if dropped:
        print(f"Skipping duplicate title(s) {dropped} - same video as another title")
    return dict(sorted(final.items()))


@contextmanager
def disc_mounted(device):
    """Yields the Blu-ray's mount point as a Path, mounting the disc
    read-only (via udisksctl, no root needed) if it isn't already mounted,
    and unmounting it again afterwards if it was mounted here. Yields None
    if it can't be mounted."""
    def mount_point():
        result = subprocess.run(["findmnt", "-n", "-o", "TARGET", "-S", device],
                                capture_output=True, text=True)
        return result.stdout.strip().splitlines()[0] if result.stdout.strip() else None

    root = mount_point()
    mounted_here = False
    if root is None and device:
        result = subprocess.run(["udisksctl", "mount", "-b", device, "-o", "ro"],
                                capture_output=True, text=True)
        mounted_here = result.returncode == 0
        root = mount_point()
    try:
        yield Path(root) if root else None
    finally:
        if mounted_here:
            subprocess.run(["udisksctl", "unmount", "-b", device], capture_output=True)


def read_playlist_clip_seconds(playlist_path):
    """Reads a Blu-ray playlist (BDMV/PLAYLIST/NNNNN.mpls - not encrypted, so
    readable straight off the mounted disc) and returns the length in
    seconds of each video clip it plays, in play order. Returns None if
    the file can't be read or parsed."""
    try:
        data = playlist_path.read_bytes()
        playlist_start = struct.unpack(">I", data[8:12])[0]
        item_count = struct.unpack(">H", data[playlist_start + 6:playlist_start + 8])[0]
        pos = playlist_start + 10
        seconds = []
        for _ in range(item_count):
            item_length = struct.unpack(">H", data[pos:pos + 2])[0]
            in_time, out_time = struct.unpack(">II", data[pos + 14:pos + 22])
            seconds.append((out_time - in_time) / 45000)  # 45 kHz clock
            pos += 2 + item_length
        return seconds
    except (OSError, struct.error):
        return None


def find_play_all_split(titles, device, pad_seconds):
    """Looks for a Blu-ray "Play All" title to cut into episodes, for discs
    that only offer the episodes back to back: each episode is its own
    video clip on the disc, but has no playlist of its own, so MakeMKV
    never lists it as a title. A Blu-ray title's playlist (its
    source_filename, e.g. 00004.mpls) lists the clips it plays; when
    enough of them match each other in length (see detect_episode_titles),
    those clips are the episodes. Titles are tried longest first.
    Returns (title_index, clip_seconds, episode_clips) - clip_seconds is
    every clip's length in play order, episode_clips the indices of the
    clips that are episodes (a short intro clip isn't) - or None."""
    playlists = [(info.get("duration_seconds", 0), idx, info["source_filename"])
                 for idx, info in titles.items()
                 if (info.get("source_filename") or "").lower().endswith(".mpls")]
    if not playlists:
        return None
    with disc_mounted(device) as root:
        if root is None:
            print("Couldn't mount the disc to read its playlists")
            return None
        for _duration, idx, playlist in sorted(playlists, reverse=True):
            clip_seconds = read_playlist_clip_seconds(root / "BDMV" / "PLAYLIST" / playlist)
            if not clip_seconds or len(clip_seconds) < 2:
                continue
            clips = {i: {"duration_seconds": int(s)} for i, s in enumerate(clip_seconds)}
            episode_clips, certain = detect_episode_titles(clips, pad_seconds)
            if certain:
                return idx, clip_seconds, episode_clips
    return None


def split_play_all(raw_dir, clip_seconds, episode_clips):
    """Cuts a ripped "Play All" title (see find_play_all_split) into one
    file per episode, at the points where the disc's clips join - no
    re-encoding, just copying. MakeMKV puts a chapter mark at each join,
    so a calculated cut point within a few seconds of a chapter snaps to
    it. Each episode is copied out with its own ffmpeg run that seeks to
    its start: ffmpeg's segment muxer can't be used, since MakeMKV's VC-1
    video has no presentation timestamps for it to cut on. A seek can land
    a fraction of a second off the join, which on these discs falls in the
    black between episodes.
    The episode files are written as episode_t00.mkv, episode_t01.mkv...
    in play order (matching guess_episode_order's '_t<NN>' naming) and the
    Play All file is deleted. Returns the episode file paths."""
    source = max(Path(raw_dir).glob("*.mkv"), key=lambda f: f.stat().st_size)
    chapters = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "chapter=start_time",
         "-of", "csv=p=0", str(source)],
        capture_output=True, text=True,
    ).stdout.split()
    chapter_starts = [float(c) for c in chapters if c.replace(".", "", 1).isdigit()]

    def snap(seconds):
        nearest = min(chapter_starts, key=lambda c: abs(c - seconds), default=None)
        return nearest if nearest is not None and abs(nearest - seconds) <= 3 else seconds

    # Clip k plays from bounds[k] to bounds[k + 1]; the last runs to the end.
    bounds = [0.0] + [snap(sum(clip_seconds[:k])) for k in range(1, len(clip_seconds))] + [None]
    print("Cutting the Play All title into episodes at "
          + ", ".join(f"{b / 60:.1f}" for b in bounds[1:-1]) + " min")
    progress = zenity_progress_pulse(f"Cutting the Play All title into {len(episode_clips)} episodes...")
    episode_files = []
    try:
        for n, clip in enumerate(episode_clips):
            start, end = bounds[clip], bounds[clip + 1]
            episode_file = Path(raw_dir, f"episode_t{n:02d}.mkv")
            subprocess.run(
                ["ffmpeg", "-y", "-v", "error",
                 *(["-ss", f"{start:.3f}"] if start else []), "-i", str(source),
                 *(["-t", f"{end - start:.3f}"] if end is not None else []),
                 "-map", "0", "-c", "copy", str(episode_file)],
                stdin=subprocess.DEVNULL, check=True,
            )
            episode_files.append(episode_file)
    finally:
        progress.terminate()
    source.unlink()
    return episode_files


# Rescans for a disc whose first scan didn't settle which titles are the
# episodes (see choose_episode_titles). A scratched disc can make the drive
# reset mid-scan, and it needs a while before it reads again.
RESCAN_ATTEMPTS = 3
RESCAN_DELAY_SECONDS = 30

# How close a "Play All" title's length must be to the sum of the titles it
# chains, per title - MakeMKV rounds each length to the second.
PLAY_ALL_TOLERANCE_SECONDS = 3

# Episode detection is only certain when the runner-up group of matching
# titles has less than this fraction of the winning group's total runtime.
EPISODE_DETECT_RUNNER_UP_RATIO = 0.7


def is_play_all(duration, other_durations):
    """True if duration equals the sum of two or more of other_durations
    (within PLAY_ALL_TOLERANCE_SECONDS per title summed) - i.e. the title is
    probably a "Play All" that chains those titles together."""
    # sums[k] = every total reachable by adding up k titles (k capped at 2,
    # standing for "2 or more"), mapped to how many titles went into it.
    sums = {0: {0: 0}, 1: {}, 2: {}}
    for d in other_durations:
        for k in (2, 1, 0):
            for total, count in list(sums[k].items()):
                new_total = total + d
                if new_total > duration + PLAY_ALL_TOLERANCE_SECONDS * (count + 1):
                    continue
                bucket = sums[min(k + 1, 2)]
                bucket[new_total] = min(bucket.get(new_total, count + 1), count + 1)
    return any(abs(total - duration) <= PLAY_ALL_TOLERANCE_SECONDS * count
               for total, count in sums[2].items())


def detect_episode_titles(titles, pad_seconds):
    """Picks the disc titles that are probably the episodes, on the basis
    that every episode in a season runs about the same length: titles are
    grouped by matching length (durations within 2 * pad_seconds of each
    other), and the group with the most total runtime wins - episodes are
    the disc's main content, so they add up to far more than a handful of
    same-length featurettes, even when the featurettes outnumber them.
    Titles under cfg.EPISODE_DETECT_MIN_SECONDS (menus, logos, short clips)
    are ignored, and a group needs at least cfg.EPISODE_DETECT_MIN_TITLES
    titles, so a lone "Play All" compilation can't win on runtime. A group
    whose titles are all Play Alls (each one's length is the sum of other
    titles - see is_play_all), e.g. two Play Alls of similar length, is
    skipped too.

    Returns (best_indices, certain). best_indices is the winning group, or
    [] if no group qualifies. certain is False when no group qualifies or
    another group of matching titles, with none of the same titles, comes
    within EPISODE_DETECT_RUNNER_UP_RATIO of its total runtime - i.e. when
    length alone can't say which titles are episodes. Then best_indices is
    whichever of those close groups has the longest titles, as the best
    guess to suggest."""
    candidates = sorted(
        (info["duration_seconds"], idx) for idx, info in titles.items()
        if info.get("duration_seconds", 0) >= cfg.EPISODE_DETECT_MIN_SECONDS
    )
    groups = []  # (total_seconds, indices)
    for lo in range(len(candidates)):
        hi = lo
        while hi + 1 < len(candidates) and candidates[hi + 1][0] - candidates[lo][0] <= 2 * pad_seconds:
            hi += 1
        group = candidates[lo:hi + 1]
        if len(group) < cfg.EPISODE_DETECT_MIN_TITLES:
            continue
        members = {i for _, i in group}
        others = [d for d, i in candidates if i not in members]
        if all(is_play_all(d, others) for d, _ in group):
            continue
        groups.append((sum(d for d, _ in group), sorted(members)))
    if not groups:
        return [], False

    groups.sort(reverse=True)
    best_total, best = groups[0]
    close = [indices for total, indices in groups[1:]
             if not set(indices) & set(best) and total >= EPISODE_DETECT_RUNNER_UP_RATIO * best_total]
    if not close:
        return best, True
    # Too close to call - suggest the group with the longest titles, since
    # bonus featurettes tend to be shorter than episodes.
    def average_length(indices):
        return sum(titles[i]["duration_seconds"] for i in indices) / len(indices)
    return max([best] + close, key=average_length), False


def ask_episode_titles(titles, suggested, disc_errors, device):
    """Asks the user, in a single checklist, which disc titles are episodes.
    Every title is listed with its length and chapter count, and the
    suggested titles are pre-ticked. Returns the chosen title indices, or None if the
    user cancels or ticks nothing (the disc has already been ejected by
    then)."""
    rows = []
    for idx, info in sorted(titles.items()):
        seconds = info.get("duration_seconds", 0)
        rows += [
            "TRUE" if idx in suggested else "FALSE",
            str(idx),
            f"{seconds // 3600}:{seconds // 60 % 60:02d}:{seconds % 60:02d}",
            str(info.get("chapter_count") or ""),
            info.get("name", ""),
        ]
    result = subprocess.run(
        ["zenity", "--list", "--checklist", "--title", "Disc Ripper",
         "--text", "Couldn't tell which titles are the episodes from their lengths.\n"
                   "Tick the titles to rip as episodes:" + format_makemkv_errors(disc_errors),
         "--column", "Rip", "--column", "Title", "--column", "Length",
         "--column", "Chapters", "--column", "Name",
         "--print-column=2", "--separator= ", "--width=600", "--height=450",
         *rows],
        capture_output=True, text=True,
    )
    chosen = sorted(int(i) for i in result.stdout.split()) if result.returncode == 0 else []
    if not chosen:
        notify("No titles selected - aborting.", device=device)
        return None
    return chosen


def choose_episode_titles(titles, disc_errors, device):
    """Picks the disc title indices to rip as episodes. They're picked
    automatically by matching lengths (see detect_episode_titles, with
    cfg.EPISODE_LENGTH_PAD_MINUTES as the tolerance). If that's not
    conclusive, the disc is rescanned (up to RESCAN_ATTEMPTS times) - read
    errors on a scratched disc can make MakeMKV drop titles it already
    found, or the drive reset mid-scan, but a later scan often reads it
    fine. The scan with the most titles is used, and the user is asked,
    once, only if none of them settles it. MakeMKV's own errors from the scan used (disc_errors, see
    get_disc_titles) are shown in the abort dialog and the checklist.
    Before rescanning, a Blu-ray that only offers its episodes as one
    "Play All" title is checked for (see find_play_all_split) - then that
    title is ripped and cut into episodes instead.
    Returns (title_indices, titles, split), where titles is the scan
    actually used and split is None or the (clip_seconds, episode_clips)
    to cut the single ripped title at (see split_play_all) - or None if
    there's nothing to rip (the disc has already been ejected by then)."""
    pad_seconds = cfg.EPISODE_LENGTH_PAD_MINUTES * 60
    best, certain = detect_episode_titles(drop_duplicate_titles(titles), pad_seconds)
    if not certain:
        play_all = find_play_all_split(drop_duplicate_titles(titles), device, pad_seconds)
        if play_all:
            idx, clip_seconds, episode_clips = play_all
            lengths = ", ".join(f"{clip_seconds[c] / 60:.0f}" for c in episode_clips)
            print(f"Title {idx} is a Play All of {len(episode_clips)} episode(s) with no titles "
                  f"of their own (lengths in min: {lengths}) - ripping it and cutting it up")
            return [idx], titles, (clip_seconds, episode_clips)

    attempt = 0
    while not certain and attempt < RESCAN_ATTEMPTS:
        attempt += 1
        print(f"Couldn't pick the episodes from {len(titles)} title(s) - rescanning in "
              f"{RESCAN_DELAY_SECONDS}s (attempt {attempt} of {RESCAN_ATTEMPTS})")
        time.sleep(RESCAN_DELAY_SECONDS)
        rescanned, rescan_errors = get_disc_titles(log_name=f"last_disc_rescan_{attempt}.txt")
        if len(rescanned) > len(titles):
            print(f"Rescan found {len(rescanned)} title(s) - using it")
            titles, disc_errors = rescanned, rescan_errors
            best, certain = detect_episode_titles(drop_duplicate_titles(titles), pad_seconds)

    if not titles:
        notify("MakeMKV couldn't read any titles from this disc - "
               f"aborting.{format_makemkv_errors(disc_errors)}", device=device)
        return None
    if certain:
        lengths = ", ".join(f"{titles[i]['duration_seconds'] / 60:.0f}" for i in best)
        print(f"Detected {len(best)} episode title(s): {best} (lengths in min: {lengths})")
        return best, titles, None

    # Don't eject before asking - the disc is still needed to rip the choice.
    ring_bell()
    chosen = ask_episode_titles(drop_duplicate_titles(titles), best, disc_errors, device)
    return (chosen, titles, None) if chosen else None


def handle_movie(raw_dir, title, year, device):
    """Encodes and sends the movie. Returns True if it made it to the
    server - the caller uses this to decide whether the raw rip is safe to
    delete."""
    # Find the ripped file with the longest runtime - almost always the movie itself
    mkv_files = list(Path(raw_dir).glob("*.mkv"))
    if not mkv_files:
        notify("No files were ripped - check MakeMKV output.", device=device)
        return False

    mkv_files.sort(key=get_duration_seconds, reverse=True)
    main_file = mkv_files[0]

    folder_name = f"{title} ({year})"
    output_name = f"{folder_name}.mkv"
    encoded_path = Path(cfg.ENCODED_DIR) / folder_name / output_name

    success = encode_and_send(main_file, encoded_path, f"{cfg.REMOTE_MOVIES_PATH}/{folder_name}", folder_name)
    if success:
        notify(f"Done! {folder_name} has been encoded and sent to the media server.", device=device)
    return success


def handle_tv(mkv_files, show_name, season_num, device, titles, tmdb_id=None):
    """Encodes and sends every chosen episode. Returns True only if all of
    them made it to the server - the caller uses this to decide whether the
    raw rip is safe to delete, so a partial failure (e.g. a GPU driver
    problem mid-batch) doesn't cost you the whole disc's rip.

    tmdb_id, if given, is used as the show's TMDB id as-is instead of
    searching for it by name - some shows have several TMDB entries (e.g. a
    remake sharing a near-identical title) where the name search finds the
    wrong one and tags episodes with another show's names."""
    if not mkv_files:
        notify("No files were ripped - check MakeMKV output.", device=device)
        return False

    guesses = guess_episode_order(mkv_files, titles)
    season_folder = f"Season {season_num:02d}"
    remote_season_dir = f"{cfg.REMOTE_TV_PATH}/{show_name}/{season_folder}"

    # Earlier discs of this season may already be on the server - continue
    # numbering after the highest episode there instead of restarting at 1.
    highest = highest_episode_on_server(remote_season_dir, season_num)
    first_episode = (highest or 0) + 1
    if highest is None:
        start_note = "couldn't reach the server to check for earlier discs, so starting at 1"
    elif highest:
        start_note = f"episodes up to {highest} are already on the server"
    else:
        start_note = "no episodes of this season on the server yet"
    # Looked up once per disc rather than per episode - it's the same show
    # for every file, no need to hit TMDB's search endpoint repeatedly.
    show_id = tmdb_id if tmdb_id is not None else tmdb_find_show_id(show_name)

    # Every ripped title was already picked as an episode, so it's numbered
    # automatically from the guessed order (see guess_episode_order). The
    # user is only asked about titles whose guess is low confidence or
    # missing. All of this happens before encoding starts, so the whole
    # batch can run unattended.
    episodes = []
    for f, offset, confidence, reason in guesses:
        guessed_num = offset + first_episode if offset is not None else None
        if guessed_num is not None and confidence != "low":
            print(f"{f.name} -> episode {guessed_num} ({confidence} confidence: {reason}; "
                  f"{start_note})")
            episodes.append((f, guessed_num))
            continue

        duration_min = get_duration_seconds(f) / 60
        if guessed_num is not None:
            hint = f"Guessed episode {guessed_num} ({reason}; {start_note})."
        else:
            hint = f"Couldn't guess a number ({reason})."
        episode = zenity_entry(
            f"File: {f.name}\nDuration: {duration_min:.0f} min\n\n{hint}\n\n"
            "Episode number (leave blank to skip this file):",
            default_text=guessed_num,
        )
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
        return False

    transferred = []
    for f, episode_num in episodes:
        # Only look up the episode title once the number is confirmed -
        # there's no point querying TMDB for a number that might still change.
        episode_title = tmdb_get_episode_title(show_id, season_num, episode_num)
        title_suffix = f" - {sanitize(episode_title)}" if episode_title else ""

        episode_filename = f"{show_name} S{season_num:02d}E{episode_num:02d}{title_suffix}.mkv"
        encoded_path = Path(cfg.ENCODED_DIR) / show_name / season_folder / episode_filename

        label = f"{episode_filename} ({len(transferred) + 1} of {total})"
        if encode_and_send(f, encoded_path, remote_season_dir, label):
            transferred.append(episode_filename)

    # Numbers were mostly assigned without asking, so list what was sent
    # to make a wrong guess easy to spot.
    notify(f"Done! {len(transferred)} of {total} episode(s) for {show_name} transferred "
           "to the media server:\n\n" + "\n".join(transferred),
           device=device)
    return len(transferred) == total


def main():
    os.makedirs(cfg.WORK_DIR, exist_ok=True)
    ensure_all_subtitles_selected()
    print("Disc ripper running. Press Ctrl+C to stop.")

    last_content_type = None
    last_show_name = None
    last_season_num = None
    last_tmdb_id = None

    try:
        while True:
            device = wait_for_disc()
            time.sleep(2)  # let the disc finish spinning up before MakeMKV touches it

            # Pre-selects the previous disc's answer, like the TV prompts below.
            content_type = zenity_choice(
                "What's on this disc?",
                ["Movie", "TV Show"],
                default=last_content_type,
            )
            if not content_type:
                eject_disc(device)
                continue
            last_content_type = content_type

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
                # Pre-filled with the previous disc's answers - consecutive
                # discs are almost always the same show and season.
                show_name = prompt_required("Show name:", "show name", device,
                                             default_text=last_show_name)
                if show_name is None:
                    continue
                show_name = sanitize(show_name)

                season_num = prompt_required_int("Season number (e.g. 1):", "season", device,
                                                  default_text=last_season_num)
                if season_num is None:
                    continue

                # A TMDB id only carries over while it's the same show -
                # another show's id would tag episodes with the wrong names.
                same_show = show_name == last_show_name
                last_show_name, last_season_num = show_name, season_num

                tmdb_id = prompt_tmdb_show_id(last_tmdb_id if same_show else None)
                last_tmdb_id = tmdb_id

            if not wait_for_makemkv_gui_closed(device):
                continue

            raw_dir = os.path.join(cfg.RAW_RIP_DIR, str(int(time.time())))
            try:
                titles, disc_errors = get_disc_titles()
            except subprocess.CalledProcessError as e:
                notify(f"MakeMKV failed to read the disc (exit code {e.returncode}) - "
                       "aborting. Check the terminal for details.", device=device)
                print(f"Command failed: {e}", file=sys.stderr)
                continue

            all_done = False
            try:
                if content_type == "Movie":
                    title_index = choose_main_title(titles, disc_errors, device)
                    if title_index is None:
                        continue
                    rip_titles(raw_dir, [title_index])
                else:
                    # Needs the disc's title list, so this runs after
                    # get_disc_titles rather than with the up-front prompts.
                    chosen = choose_episode_titles(titles, disc_errors, device)
                    if chosen is None:
                        continue
                    episode_indices, titles, split = chosen
                    rip_titles(raw_dir, episode_indices)
                    if split:
                        split_files = split_play_all(raw_dir, *split)
                        # The cut-up files are already in play order, and
                        # named to match these placeholder titles.
                        titles = {n: {} for n in range(len(split_files))}
            except subprocess.CalledProcessError as e:
                notify(f"{Path(e.cmd[0]).name} failed (exit code {e.returncode}) - aborting this disc. "
                       "Check the terminal for details.", device=device)
                print(f"Command failed: {e}", file=sys.stderr)
            else:
                if content_type == "Movie":
                    all_done = handle_movie(raw_dir, title, year, device)
                else:
                    all_done = handle_tv(list(Path(raw_dir).glob("*.mkv")), show_name, season_num,
                                          device, titles, tmdb_id)

            # Only reclaim the raw rip once everything in it has been
            # encoded and sent - a failed rip, encode or transfer (e.g. the
            # GPU driver mismatch that silently zeroed out a whole batch of
            # NVENC encodes) leaves the raw files in place so the disc
            # doesn't have to be re-ripped to retry.
            if all_done:
                shutil.rmtree(raw_dir, ignore_errors=True)
            else:
                print(f"Leaving raw rip in {raw_dir} - not everything was encoded and sent. "
                      "Fix the problem and retry (e.g. with --from-folder), or delete it "
                      "manually.", file=sys.stderr)
    except KeyboardInterrupt:
        print("\nCtrl+C received - shutting down.")
        sys.exit(0)


def process_folder(folder):
    """Numbers, encodes and sends a folder of .mkv files that were ripped
    some other way (e.g. with MakeMKV's own app, for a disc the watcher
    can't read reliably), as a TV season - the same steps the watcher runs
    after its own rip. The episodes are picked from the files by matching
    lengths, like disc titles are (see detect_episode_titles), so a Play
    All or extras ripped along with them are skipped; if that's not
    conclusive the user ticks them in the same one-time checklist. There's
    no disc title list, so episode order comes from MakeMKV's '_t<NN>' file
    naming (see guess_episode_order). The folder itself is left untouched."""
    folder = Path(folder).expanduser()
    files = sorted(folder.glob("*.mkv"))
    if not files:
        sys.exit(f"No .mkv files in {folder}")
    os.makedirs(cfg.WORK_DIR, exist_ok=True)

    show_name = prompt_required("Show name:", "show name", None)
    if show_name is None:
        return
    season_num = prompt_required_int("Season number (e.g. 1):", "season", None)
    if season_num is None:
        return
    tmdb_id = prompt_tmdb_show_id()

    # Keyed by position in `files`, shaped like get_disc_titles' output so
    # the disc-title helpers work on it.
    titles = {i: {"duration_seconds": int(get_duration_seconds(f)), "name": f.name}
              for i, f in enumerate(files)}
    best, certain = detect_episode_titles(titles, cfg.EPISODE_LENGTH_PAD_MINUTES * 60)
    if certain:
        chosen = best
        skipped = [files[i].name for i in titles if i not in best]
        if skipped:
            print(f"Skipping {len(skipped)} file(s) that don't match the episode length: "
                  + ", ".join(skipped))
    else:
        chosen = ask_episode_titles(titles, best, [], None)
        if not chosen:
            return

    handle_tv([files[i] for i in chosen], sanitize(show_name), season_num, None, {}, tmdb_id)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Watch for discs, then rip, encode and send them to the media server.")
    parser.add_argument(
        "--from-folder", metavar="DIR",
        help="process existing TV episode .mkv files in DIR (number, encode, send) "
             "instead of watching for discs",
    )
    args = parser.parse_args()
    if args.from_folder:
        process_folder(args.from_folder)
    else:
        main()
