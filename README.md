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

## Running it

```bash
cd disc-ripper
python3 watch_and_rip.py
```

Leave the terminal open. Insert a disc:
- A popup asks if it's a **Movie** or **TV Show**
- MakeMKV rips all titles over the minimum length (configurable in
  `config.py` — defaults to 20 minutes, lower it for TV discs with short
  episodes)
- **Movie**: enter title + year. The script assumes the *longest* ripped
  title is the movie itself (usually correct) and encodes just that one.
- **TV Show**: enter the show name and season number, then for each ripped
  title you'll be asked if it's a real episode (so you can skip trailers/
  recaps) and what episode number it is.
- The finished file(s) get encoded with your GPU (NVENC) and rsync'd
  straight to the correct folder on the media server.
- The disc ejects automatically when done.

Insert the next disc and repeat — the script loops forever until you
Ctrl+C it.

## Notes / things to double check

- **Movie title matching**: the script picks the single longest ripped
  title as "the movie." For most single-feature discs this is right, but
  double-check the result against Jellyfin after a few rips to be sure.
- **Disc structure surprises**: some special editions bundle a "Play All"
  title that's longer than the actual movie (concatenates it with something
  else). If a movie comes out with a suspiciously long runtime, check the
  raw MakeMKV output folder before trusting the automation blindly.
- **TV numbering**: MakeMKV title order doesn't necessarily match episode
  order. Look up the actual episode list/order before naming, same as we
  discussed for Legend of Korra.
- **Naming conventions**: this matches what we set up for Jellyfin —
  `Title (Year)/Title (Year).mkv` for movies, `Show/Season NN/Show SxxExx.mkv`
  for TV.
- **First run**: try it once end-to-end on a disc you don't mind re-ripping,
  to confirm the rsync destination and Jellyfin picks it up correctly
  before running it unattended on your whole backlog.
