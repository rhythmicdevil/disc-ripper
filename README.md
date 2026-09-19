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
- **Movie**: enter title + year. MakeMKV rips every title over the minimum
  length (`MAKEMKV_MIN_LENGTH_SECONDS` in `config.py`, defaults to 65 min),
  and the script assumes the *longest* one is the movie itself (usually
  correct) and encodes just that one.
- **TV Show**: enter the show name and season number, then the approximate
  **episode length in minutes**. That length becomes the center of a range
  (± `EPISODE_LENGTH_PAD_MINUTES` in `config.py`, default 2 min — you're
  asked at rip time whether to use that default or a custom padding), and
  only disc titles whose duration falls in that range get ripped as episode
  candidates. This is what keeps a "Play All" compilation title (much
  longer than any single episode) from being mistaken for an episode, or
  even ripped at all — the old fixed minimum-length filter was tuned for
  movies and let compilations like that through as the *only* thing that
  cleared the threshold.
  Then, for each ripped title you'll be asked if it's a real episode (so you
  can still skip anything unexpected that landed in the range) and what
  episode number it is. The episode number field is
  pre-filled with a guess, derived from (in priority order) the disc's title
  order, the disc source filename's sequence number, and the title's chapter
  count — the prompt tells you the guess's confidence and why, so you know
  whether to just confirm or double-check it. None of these signals are
  fully reliable on their own (discs get authored in all kinds of orders),
  so it's always a starting point you can edit, never assumed to be correct.
  All of this happens up front, before any encoding starts, so you can walk
  away once you've answered for every ripped title.
  Once a number is confirmed, if the `TMDB_API_KEY` environment variable is
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
- **TV episode-length range**: if you enter a length that's off, or use too
  narrow a padding, real episodes can fall outside the range and get
  skipped entirely (nothing to select them from afterward, since they were
  never ripped). If a disc comes up with fewer episodes than expected,
  re-check the length/padding you entered before assuming something else
  is wrong.
- **TV numbering**: the episode-number prompt is now pre-filled with a
  guess (disc title order, cross-checked against source filename and
  chapter count) and tells you its confidence — but MakeMKV title order
  still doesn't *necessarily* match episode order, so treat "high
  confidence" as "probably right," not "definitely right." Worth
  double-checking against the actual episode list for anything with
  bonus features, double-length episodes, or a disc that starts mid-season
  (the guess always assumes disc 1 = episode 1), same as we discussed for
  Legend of Korra.
- **Naming conventions**: this matches what we set up for Jellyfin —
  `Title (Year)/Title (Year).mkv` for movies, `Show/Season NN/Show SxxExx.mkv`
  for TV.
- **First run**: try it once end-to-end on a disc you don't mind re-ripping,
  to confirm the rsync destination and Jellyfin picks it up correctly
  before running it unattended on your whole backlog.
