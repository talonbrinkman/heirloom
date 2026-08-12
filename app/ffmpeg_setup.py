import os

def ensure_ffmpeg():
    # In the docker container, ffmpeg is installed via apt-get in the Dockerfile.
    pass

def get_ffmpeg_path():
    return "ffmpeg"

def get_ffprobe_path():
    return "ffprobe"

if __name__ == "__main__":
    ensure_ffmpeg()
