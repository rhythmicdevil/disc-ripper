# Disc Ripper Automation

Watches for a disc, prompts you for title/type via popup, rips with MakeMKV,
encodes with FFmpeg (NVENC), and sends the finished file to the media server
via rsync over SSH.

## One-time setup

### 1. Install dependencies

```bash
sudo apt update
sudo apt install ffmpeg zenity rsync python3-pip
pip3 install pyudev
```

### 2. Install MakeMKV

MakeMKV isn't in the standard Ubuntu repos. Follow the official Linux build
instructions:
https://www.makemkv.com/forum2/viewtopic.php?f=3&t=224

Confirm it works from the command line:

```bash
makemkvcon info disc:0
```

### 3. Set up passwordless SSH to the media server

From this laptop:

```bash
ssh-keygen -t ed25519 -C "disc-ripper-laptop"
ssh-copy-id swright@media
```

Test it — this should NOT prompt for a password:

```bash
ssh swright@media echo ok
```

### 4. Edit config.py

Open `config.py` and confirm:
- `SSH_USER` / `SSH_HOST` match your media server
- `REMOTE_MOVIES_PATH` / `REMOTE_TV_PATH` match your actual mount points
- `WORK_DIR` has enough free disk space for raw rips (Blu-ray rips can be
  20-30GB before encoding)

Also, optionally, set the `TMDB_API_KEY` environment variable to a free API
key from https://www.themoviedb.org/settings/api. This enables episode-name
lookup for TV rips (e.g. "S01E01 - Welcome to the Hellmouth"). It's read
from the environment (not `config.py`) so the key never ends up in git —
add it to your shell profile:

```bash
echo 'export TMDB_API_KEY="your-key-here"' >> ~/.bashrc
source ~/.bashrc
```

Leave it unset to skip episode-name lookup — episodes are still named
`Show SxxExx.mkv` with no title.

## Running it

```bash
cd disc-ripper
python3 watch_and_rip.py
```

Leave the terminal open. Insert a disc:
- A popup asks if it's a **Movie** or **TV Show**
- **Movie**: enter title + year. The script reads the disc's title list,
  drops playlists that only differ in audio/subtitle tracks (same video
  and same size), and rips just
  the *longest* title over the minimum length (`MAKEMKV_MIN_LENGTH_SECONDS`
  in `config.py`, defaults to 65 min) - usually the movie itself. If two
  different titles tie for longest, you're asked to pick.
- **TV Show**: enter the show name and season number. The script then
  reads the disc's title list and **picks the episodes itself**:
  - Titles that play the same video as another title are dropped. Blu-rays
    often list each episode under several playlists that differ only in
    audio/subtitle tracks; the one with the most tracks is kept. Titles only
    count as the same video when both the segments they play and their size
    match (on DVDs, different episodes often share segment numbers).
  - Since every episode in a season runs about the same length, the biggest
    group of titles whose lengths match (within ± `EPISODE_LENGTH_PAD_MINUTES`
    in `config.py`, default 2 min) is ripped as the episodes. Titles under
    `EPISODE_DETECT_MIN_SECONDS` (5 min) are ignored. A "Play All"
    compilation is several episodes long, so it never matches.
  - If that's not conclusive (fewer than `EPISODE_DETECT_MIN_TITLES` titles
    match, or two different groups are equally big), you're asked **once**:
    a checklist of every title with its length and chapter count, with the
    best guess pre-ticked.
  The ripped episodes are then **numbered automatically**, ordered by the
  disc's playlist/file numbers (`00071.mpls`, `VTS_04_1.VOB`) when each
  title has a distinct one, otherwise by MakeMKV's title order. Since a
  season usually spans several discs, numbering continues from what's
  already on the media server: the script lists the season folder over SSH
  and starts this disc after the highest `SxxEyy` episode it finds (at 1 if
  there are none yet, or if the server can't be reached). Each assignment is
  printed in the terminal. You're only asked for a number (pre-filled with
  the guess; leave it blank to skip the file) for a title whose chapter
  count doesn't match the other episodes, or that couldn't be matched back
  to a disc title. All of this happens up front, before any encoding
  starts, and the final "Done" dialog lists every file sent.
  Once a number is settled, if the `TMDB_API_KEY` environment variable is
  set, the script looks up that episode's title on TMDB and appends it to the
  filename (e.g. `Buffy the Vampire Slayer S01E01 - Welcome to the
  Hellmouth.mkv`). If the lookup fails or isn't configured, the file is just
  named `Show SxxExx.mkv` as before.
- The finished file(s) get encoded with your GPU (NVENC) and rsync'd
  straight to the correct folder on the media server. For TV discs, the
  progress popup and final summary show a running count (e.g. "2 of 4")
  against the total number of episodes you selected.
- Once everything for that disc is finished (or has failed and been
  reported), the script rings the terminal bell twice and ejects the disc —
  so you get both an audible and a physical signal that it's done, even if
  you've stepped away.

Insert the next disc and repeat — the script loops forever until you
Ctrl+C it.

## Notes / things to double check

- **No sound on completion?**: the "done" chime is the plain terminal bell
  (`\a`). Some terminal emulators mute it by default or show a visual flash
  instead — check your terminal's "bell"/"audible bell" preference if you
  don't hear it, or just rely on the disc ejecting.
- **Movie title matching**: the script picks the single longest ripped
  title as "the movie." For most single-feature discs this is right, but
  double-check the result against Jellyfin after a few rips to be sure.
- **Disc structure surprises**: some special editions bundle a "Play All"
  title that's longer than the actual movie (concatenates it with something
  else). If a movie comes out with a suspiciously long runtime, check the
  raw MakeMKV output folder before trusting the automation blindly.
- **TV episode detection**: if the padding is too narrow, real episodes
  can miss the matching group and never get ripped. Detection can also
  pick the wrong group on a disc with more same-length bonus features than
  episodes. The chosen titles (and any duplicates skipped) are printed in
  the terminal - if a disc comes up with fewer episodes than expected,
  check those lines and the padding first.
- **TV numbering**: episodes are numbered automatically when the guess is
  high or medium confidence, but disc playlist or title order still doesn't
  *necessarily* match episode order, so treat that as "probably right," not
  "definitely right." Check the file list in the "Done" dialog against the
  actual episode list, especially for discs with bonus features or
  double-length episodes, same as we discussed for Legend of Korra.
- **Naming conventions**: this matches what we set up for Jellyfin —
  `Title (Year)/Title (Year).mkv` for movies, `Show/Season NN/Show SxxExx.mkv`
  for TV.
- **First run**: try it once end-to-end on a disc you don't mind re-ripping,
  to confirm the rsync destination and Jellyfin picks it up correctly
  before running it unattended on your whole backlog.
