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
    Used for TV, where candidate titles are picked in Python beforehand by
    matching duration against a user-supplied episode-length range (see
    choose_episode_length_range) - so a "Play All" compilation title (much
    longer than any single episode) never gets ripped in the first place,
    instead of being ripped and then having to be filtered out by hand."""
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


def rip_title(raw_out_dir, title_index):
    """Runs makemkvcon to rip a single, already-chosen title. Used for movies,
    where we pick the main feature from the disc's title list before ripping
    instead of ripping every long title and sorting afterward."""
    os.makedirs(raw_out_dir, exist_ok=True)
    progress = zenity_progress_pulse("Ripping disc with MakeMKV — this can take a while...")
    try:
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


def get_disc_titles(disc_num=0):
    """Queries MakeMKV's title list for the disc (name, duration, chapter
    count, and source filename per title) without ripping anything - just
    reads the disc structure, so it's fast. Title indices are returned in
    MakeMKV's own order, which is normally the disc's authoring order.
    Returns {title_index: {"name": str, "duration_seconds": float,
    "chapter_count": int|None, "source_filename": str|None}}."""
    result = subprocess.run(
        ["makemkvcon", "-r", "info", f"disc:{disc_num}"],
        capture_output=True, text=True, check=True,
    )
    titles = {}
    for line in result.stdout.splitlines():
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
    return titles


def choose_main_title(titles, device):
    """Picks the disc title index to rip as the movie. Auto-picks the longest
    title over the min-length threshold when there's a single clear winner.
    If nothing clears the threshold, or two+ titles tie for longest (runtime
    alone can't disambiguate - e.g. a theatrical/extended pair, or duplicate
    angle/audio encodes), notifies the user and lets them pick instead of
    guessing wrong. Returns None if there's nothing to rip or the user didn't
    choose - the disc has already been ejected by the time this returns None."""
    candidates = {
        idx: info for idx, info in titles.items()
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
    """Guesses each ripped file's position in episode order. Ordering
    itself always comes from the disc's title order (the order MakeMKV
    lists titles in, recovered here from MakeMKV's default '..._t<NN>.mkv'
    output naming). The source filename's sequence number and the title's
    chapter count are used only as cross-checks that downgrade confidence
    when they disagree - discs get authored in all kinds of orders, so this
    is a starting guess for the user to confirm or correct, never a final
    answer.

    Returns a list of (path, guessed_offset, confidence, reason) - one
    entry per file in mkv_files, ordered by the guess (files MakeMKV's
    naming couldn't be matched to a title go last, sorted by duration).
    guessed_offset is 0-based (add 1 for an episode number assuming this
    disc starts at episode 1); it's None when nothing could be guessed."""
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

    matched.sort(key=lambda c: c["title_index"])

    source_numbers = [c["source_number"] for c in matched]
    source_order_known = all(n is not None for n in source_numbers)
    source_order_agrees = source_order_known and source_numbers == sorted(source_numbers)

    chapter_counts = [c["chapter_count"] for c in matched if c["chapter_count"]]
    mode_chapter_count = max(set(chapter_counts), key=chapter_counts.count) if chapter_counts else None

    results = []
    for offset, c in enumerate(matched):
        reasons = ["disc title order"]
        confidence = "high"

        if not source_order_known:
            confidence = "medium"
        elif source_order_agrees:
            reasons.append("agrees with disc file order")
        else:
            confidence = "low"
            reasons.append("disagrees with disc file order")

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


def choose_episode_length_range(device):
    """Asks for the approximate episode length and turns it into a
    (min_seconds, max_seconds) window used to pick which disc titles get
    ripped as episodes - the input length is the center of the range, and a
    padding (default cfg.EPISODE_LENGTH_PAD_MINUTES, overridable here) sets
    how wide it is on each side. Using a range instead of a bare minimum
    means a "Play All" compilation title (much longer than any one episode)
    is naturally excluded, instead of being the only title that clears a
    minimum-length filter tuned for movies. Returns None if the user
    cancels any prompt (the disc has already been ejected by then)."""
    length_min = prompt_required_int(
        "Approximate episode length in minutes (e.g. 23):", "episode length", device
    )
    if length_min is None:
        return None

    default_pad = cfg.EPISODE_LENGTH_PAD_MINUTES
    choice = zenity_choice(
        f"Match episodes within {default_pad} minutes of that length "
        f"({length_min - default_pad}-{length_min + default_pad} min), "
        "or set a custom padding?",
        [f"Use default (±{default_pad} min)", "Set custom padding"],
    )
    if not choice:
        return None

    pad_min = default_pad
    if choice.startswith("Set custom"):
        pad_min = prompt_required_int("Padding in minutes (e.g. 2):", "padding", device)
        if pad_min is None:
            return None

    min_seconds = max(0, (length_min - pad_min) * 60)
    max_seconds = (length_min + pad_min) * 60
    return min_seconds, max_seconds


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
    # Looked up once per disc rather than per episode - it's the same show
    # for every file, no need to hit TMDB's search endpoint repeatedly.
    show_id = tmdb_find_show_id(show_name)

    # Ask about every ripped title up front, so we know the total transfer
    # count before encoding starts and can walk away for the whole batch.
    # The episode number field is pre-filled with a guess (from disc title
    # order, cross-checked against source filename and chapter count - see
    # guess_episode_order) so most discs just need a confirming click
    # instead of typing every number by hand.
    episodes = []
    for f, offset, confidence, reason in guesses:
        duration_min = get_duration_seconds(f) / 60
        keep = zenity_choice(
            f"File: {f.name}\nDuration: {duration_min:.0f} min\n\nIs this an episode?",
            ["Yes - it's an episode", "No - skip this file"]
        )
        if not keep or keep.startswith("No"):
            continue

        guessed_num = offset + 1 if offset is not None else None
        if guessed_num is not None:
            hint = f"\n\nGuessed episode {guessed_num} ({confidence} confidence: {reason})."
        else:
            hint = f"\n\nCouldn't guess a number ({reason})."
        episode = zenity_entry(
            f"Episode number for {f.name} (e.g. 1):{hint}",
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

    transferred = 0
    for f, episode_num in episodes:
        # Only look up the episode title once the number is confirmed -
        # there's no point querying TMDB for a number that might still change.
        episode_title = tmdb_get_episode_title(show_id, season_num, episode_num)
        title_suffix = f" - {sanitize(episode_title)}" if episode_title else ""

        season_folder = f"Season {season_num:02d}"
        episode_filename = f"{show_name} S{season_num:02d}E{episode_num:02d}{title_suffix}.mkv"
        encoded_path = Path(cfg.ENCODED_DIR) / show_name / season_folder / episode_filename

        label = f"{episode_filename} ({transferred + 1} of {total})"
        if encode_and_send(f, encoded_path, f"{cfg.REMOTE_TV_PATH}/{show_name}/{season_folder}", label):
            transferred += 1

    notify(f"Done! {transferred} of {total} episode(s) for {show_name} transferred to the media server.",
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

                episode_range = choose_episode_length_range(device)
                if episode_range is None:
                    continue

            raw_dir = os.path.join(cfg.RAW_RIP_DIR, str(int(time.time())))
            titles = get_disc_titles()
            try:
                if content_type == "Movie":
                    title_index = choose_main_title(titles, device)
                    if title_index is None:
                        continue
                    rip_title(raw_dir, title_index)
                else:
                    min_seconds, max_seconds = episode_range
                    episode_indices = sorted(
                        idx for idx, info in titles.items()
                        if min_seconds <= info.get("duration_seconds", 0) <= max_seconds
                    )
                    if not episode_indices:
                        notify("No titles on this disc matched the expected episode "
                               "length - aborting.", device=device)
                        continue
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
