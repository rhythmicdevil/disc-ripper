"""
Configuration for the disc ripping / encoding / transfer pipeline.
Edit these values for your environment.
"""

import os

# --- Media server (rsync target) ---
SSH_USER = "swright"
SSH_HOST = "media"                       # use the off-LAN hostname/IP here if needed
REMOTE_MOVIES_PATH = "/mnt/storage/movies"
REMOTE_TV_PATH = "/mnt/storage/tv"

# --- Local working directories (scratch space on this laptop) ---
WORK_DIR = "/home/swright/disc-ripper-workdir"
RAW_RIP_DIR = WORK_DIR + "/raw"          # MakeMKV output lands here
ENCODED_DIR = WORK_DIR + "/encoded"      # FFmpeg output lands here

# --- MakeMKV settings ---
MAKEMKV_MIN_LENGTH_SECONDS = 3900        # 65 min - filters out trailers/junk titles
                                          # Lower this (e.g. 300) if ripping TV discs
                                          # with short episodes.

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
