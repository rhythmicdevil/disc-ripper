"""
Configuration for the disc ripping / encoding / transfer pipeline.
Edit these values for your environment.
"""

import os

# --- Media server (rsync target) ---
SSH_USER = "swright"
SSH_HOST = "media"                       # use the off-LAN hostname/IP here if needed
REMOTE_MOVIES_PATH = "/mnt/nas/movies"
REMOTE_TV_PATH = "/mnt/nas/tv"

# --- Local working directories (scratch space on this laptop) ---
WORK_DIR = "/home/swright/disc-ripper-workdir"
RAW_RIP_DIR = WORK_DIR + "/raw"          # MakeMKV output lands here
ENCODED_DIR = WORK_DIR + "/encoded"      # FFmpeg output lands here

# --- MakeMKV settings ---
# Movie-only: filters out trailers/junk titles when picking the main feature.
# TV rips instead detect (or, failing that, ask for) the episode length per
# disc and use that as a range (see EPISODE_LENGTH_PAD_MINUTES below), since
# a single minimum can't tell a short episode apart from a "Play All" compilation title.
MAKEMKV_MIN_LENGTH_SECONDS = 3900        # 65 min

# Default +/- padding (in minutes) around the episode length for a TV rip,
# used to decide which disc titles are ripped as episodes. The episode
# length is auto-detected from the disc (the largest group of titles whose
# durations all fit within this padding of a common center); you're only
# asked for a length, or to adjust the padding, if detection fails.
EPISODE_LENGTH_PAD_MINUTES = 2

# Episode-length auto-detection: titles shorter than this are ignored
# (menus, logos, short clips), and at least this many titles must share a
# length before it's trusted as the episode length.
EPISODE_DETECT_MIN_SECONDS = 300         # 5 min
EPISODE_DETECT_MIN_TITLES = 2

# --- FFmpeg / NVENC encode settings ---
# RTX 3080 Max-Q supports NVENC hardware encoding.
VIDEO_CODEC = "h264_nvenc"
VIDEO_QUALITY = "20"                     # roughly matches HandBrake RF 20
AUDIO_CODEC = "aac"
AUDIO_BITRATE = "192k"

# --- Optical drive device (adjust if you have more than one / different path) ---
OPTICAL_DEVICE = "/dev/sr0"

# --- TMDB (episode name lookup for TV rips) ---
# Free account + API key at https://www.themoviedb.org/settings/api
# Set via the TMDB_API_KEY environment variable (never committed to git).
# Leave unset to skip episode-name lookup (files are named "Show SxxExx.mkv"
# with no title suffix).
TMDB_API_KEY = os.environ.get("TMDB_API_KEY", "")
