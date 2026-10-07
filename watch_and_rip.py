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

import os
import re
import shlex
import subprocess
import sys
import shutil
import time
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


def get_disc_titles(disc_num=0, log_name="last_disc_scan.txt"):
    """Queries MakeMKV's title list for the disc (name, duration, chapter
    count, source filename, segment map, size, and audio/subtitle track
    count per title) without ripping anything - just reads the disc structure, so it's
    fast. Title indices are returned in MakeMKV's own order, which isn't
    always the disc's authoring order (see guess_episode_order).
    Returns {title_index: {"name": str, "duration_seconds": int,
    "chapter_count": int|None, "source_filename": str|None,
    "segment_map": str|None, "size_bytes": int|None, "track_count": int}}.
    MakeMKV's raw output is saved to log_name in cfg.WORK_DIR, so a disc
    that gets misread can be diagnosed afterwards."""
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
    return titles


def choose_main_title(titles, device):
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
               "this disc - aborting.", device=device)
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


def drop_duplicate_titles(titles):
    """Removes titles that play the same video as another title. Blu-rays
    often list each episode under several playlists that differ only in
    which audio/subtitle tracks they carry (e.g. a French and a Japanese
    version of the same episode) - ripping them all would give every
    episode twice. Titles count as the same video when both their segment
    map (the clips/cells they play) and their size match: on a DVD the
    segment map is cell numbers relative to each title (every episode can
    be "1-6"), so it isn't unique on its own. Of each set of duplicates, the
    one with the most audio + subtitle tracks is kept (ties go to the lowest
    title index). Titles missing either value are kept as-is.
    Returns a new {title_index: info} dict."""
    kept = {}
    by_video = {}
    for idx, info in sorted(titles.items()):
        video = (info.get("segment_map"), info.get("size_bytes"))
        if None in video:
            kept[idx] = info
            continue
        current = by_video.get(video)
        if current is None or info.get("track_count", 0) > titles[current].get("track_count", 0):
            by_video[video] = idx
    for idx in by_video.values():
        kept[idx] = titles[idx]
    dropped = sorted(set(titles) - set(kept))
    if dropped:
        print(f"Skipping duplicate title(s) {dropped} - same video as another title")
    return dict(sorted(kept.items()))


# How long to wait before rescanning a disc whose first scan didn't settle
# which titles are the episodes (see choose_episode_titles).
RESCAN_DELAY_SECONDS = 10


def detect_episode_titles(titles, pad_seconds):
    """Picks the disc titles that are probably the episodes, on the basis
    that every episode in a season runs about the same length: titles are
    grouped by matching length (durations within 2 * pad_seconds of each
    other), and the largest group wins. A "Play All" compilation is several
    episodes long, so it never matches the episodes themselves. Titles
    under cfg.EPISODE_DETECT_MIN_SECONDS (menus, logos, short clips) are
    ignored.

    Returns (best_indices, certain). best_indices is the largest group (on a
    tie, the one with the longer titles, since bonus featurettes tend to be
    shorter than episodes) or [] if there are no candidate titles at all.
    certain is False when the group has fewer than
    cfg.EPISODE_DETECT_MIN_TITLES titles or another, different group is just
    as large - i.e. when length alone can't say which titles are episodes."""
    candidates = sorted(
        (info["duration_seconds"], idx) for idx, info in titles.items()
        if info.get("duration_seconds", 0) >= cfg.EPISODE_DETECT_MIN_SECONDS
    )
    groups = []  # (count, total_seconds, indices)
    for lo in range(len(candidates)):
        hi = lo
        while hi + 1 < len(candidates) and candidates[hi + 1][0] - candidates[lo][0] <= 2 * pad_seconds:
            hi += 1
        group = candidates[lo:hi + 1]
        groups.append((len(group), sum(d for d, _ in group), sorted(i for _, i in group)))
    if not groups:
        return [], False

    groups.sort(key=lambda g: g[:2], reverse=True)
    best_count, _total, best = groups[0]
    tied = any(count == best_count and indices != best for count, _t, indices in groups[1:])
    certain = best_count >= cfg.EPISODE_DETECT_MIN_TITLES and not tied
    return best, certain


def ask_episode_titles(titles, suggested, device):
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
                   "Tick the titles to rip as episodes:",
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


def choose_episode_titles(titles, device):
    """Picks the disc title indices to rip as episodes. They're picked
    automatically by matching lengths (see detect_episode_titles, with
    cfg.EPISODE_LENGTH_PAD_MINUTES as the tolerance). If that's not
    conclusive, the disc is scanned once more first - a scan that runs while
    the disc is still spinning up can come back with only some of its
    titles - and the user is asked, once, only if the rescan doesn't settle
    it either. Returns (title_indices, titles), where titles is the scan
    actually used, or None if there's nothing to rip (the disc has already
    been ejected by then)."""
    best, certain = detect_episode_titles(drop_duplicate_titles(titles),
                                          cfg.EPISODE_LENGTH_PAD_MINUTES * 60)
    if not certain:
        print(f"Couldn't pick the episodes from {len(titles)} title(s) - "
              f"rescanning the disc in {RESCAN_DELAY_SECONDS}s in case it wasn't fully read")
        time.sleep(RESCAN_DELAY_SECONDS)
        rescanned = get_disc_titles(log_name="last_disc_rescan.txt")
        if len(rescanned) > len(titles):
            print(f"Rescan found {len(rescanned)} title(s) - using it")
            titles = rescanned
            best, certain = detect_episode_titles(drop_duplicate_titles(titles),
                                                  cfg.EPISODE_LENGTH_PAD_MINUTES * 60)

    if not titles:
        notify("MakeMKV couldn't read any titles from this disc - aborting.", device=device)
        return None
    if certain:
        lengths = ", ".join(f"{titles[i]['duration_seconds'] / 60:.0f}" for i in best)
        print(f"Detected {len(best)} episode title(s): {best} (lengths in min: {lengths})")
        return best, titles

    # Don't eject before asking - the disc is still needed to rip the choice.
    # A "group" too small to count as matching isn't worth pre-ticking - on
    # a disc where nothing matched it's usually just the Play All title.
    ring_bell()
    suggested = best if len(best) >= cfg.EPISODE_DETECT_MIN_TITLES else []
    chosen = ask_episode_titles(drop_duplicate_titles(titles), suggested, device)
    return (chosen, titles) if chosen else None


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


def handle_tv(raw_dir, show_name, season_num, device, titles):
    mkv_files = list(Path(raw_dir).glob("*.mkv"))
    if not mkv_files:
        notify("No files were ripped - check MakeMKV output.", device=device)
        return

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
    show_id = tmdb_find_show_id(show_name)

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
        return

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


def main():
    os.makedirs(cfg.WORK_DIR, exist_ok=True)
    ensure_all_subtitles_selected()
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
                titles = get_disc_titles()
                if content_type == "Movie":
                    title_index = choose_main_title(titles, device)
                    if title_index is None:
                        continue
                    rip_titles(raw_dir, [title_index])
                else:
                    # Needs the disc's title list, so this runs after
                    # get_disc_titles rather than with the up-front prompts.
                    chosen = choose_episode_titles(titles, device)
                    if chosen is None:
                        continue
                    episode_indices, titles = chosen
                    rip_titles(raw_dir, episode_indices)
            except subprocess.CalledProcessError as e:
                notify(f"MakeMKV failed (exit code {e.returncode}) - aborting this disc. "
                       "Check the terminal for details.", device=device)
                print(f"Command failed: {e}", file=sys.stderr)
            else:
                if content_type == "Movie":
                    handle_movie(raw_dir, title, year, device)
                else:
                    handle_tv(raw_dir, show_name, season_num, device, titles)

            # Clean up raw rip to save disk space now that encoding is done
            shutil.rmtree(raw_dir, ignore_errors=True)
    except KeyboardInterrupt:
        print("\nCtrl+C received - shutting down.")
        sys.exit(0)


if __name__ == "__main__":
    main()
