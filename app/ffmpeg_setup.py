import os
import subprocess
import shutil

_detected_encoders: dict[str, bool] = {}

def ensure_ffmpeg():
    # In the docker container, ffmpeg is installed via apt-get in the Dockerfile.
    pass

def get_ffmpeg_path():
    return shutil.which("ffmpeg") or "ffmpeg"

def get_ffprobe_path():
    return shutil.which("ffprobe") or "ffprobe"

def test_encoder(encoder_name: str) -> bool:
    """Test if a given video encoder works in ffmpeg with a 0.1-second dummy video."""
    ffmpeg_bin = get_ffmpeg_path()
    try:
        test_args = [
            ffmpeg_bin, "-y",
            "-f", "lavfi", "-i", "color=c=black:s=64x64:d=0.1",
            "-c:v", encoder_name,
            "-f", "null", "-"
        ]
        if encoder_name == "h264_vaapi":
            if not os.path.exists("/dev/dri/renderD128"):
                return False
            test_args = [
                ffmpeg_bin, "-y",
                "-vaapi_device", "/dev/dri/renderD128",
                "-f", "lavfi", "-i", "color=c=black:s=64x64:d=0.1",
                "-vf", "format=nv12,hwupload",
                "-c:v", "h264_vaapi",
                "-f", "null", "-"
            ]
        
        res = subprocess.run(test_args, capture_output=True, timeout=3)
        return res.returncode == 0
    except Exception:
        return False

def get_working_encoder(profile: str = "auto") -> tuple[str, list[str]]:
    """
    Returns (encoder_type, base_ffmpeg_video_args).
    encoder_type: 'nvenc', 'qsv', 'vaapi', 'videotoolbox', or 'software'
    """
    global _detected_encoders
    
    candidate_profiles = {
        "nvenc": ("h264_nvenc", ["-c:v", "h264_nvenc", "-preset", "p1", "-tune", "ull"]),
        "qsv": ("h264_qsv", ["-c:v", "h264_qsv", "-preset", "veryfast"]),
        "vaapi": ("h264_vaapi", ["-vaapi_device", "/dev/dri/renderD128", "-c:v", "h264_vaapi"]),
        "videotoolbox": ("h264_videotoolbox", ["-c:v", "h264_videotoolbox", "-realtime", "true"]),
    }
    
    if profile in candidate_profiles:
        enc_name, args = candidate_profiles[profile]
        if profile not in _detected_encoders:
            _detected_encoders[profile] = test_encoder(enc_name)
        if _detected_encoders[profile]:
            return profile, list(args)

    if profile == "auto":
        for p, (enc_name, args) in candidate_profiles.items():
            if p not in _detected_encoders:
                _detected_encoders[p] = test_encoder(enc_name)
            if _detected_encoders[p]:
                return p, list(args)
                
    # Fallback to software CPU libx264
    return "software", ["-c:v", "libx264", "-preset", "ultrafast", "-crf", "23"]

def get_video_transcode_args(res: str = "original", profile: str = "auto") -> list[str]:
    """Generates FFmpeg video encoding parameters based on resolution and hardware profile."""
    enc_type, base_args = get_working_encoder(profile)
    cmd = list(base_args)
    if res != "original":
        if enc_type == "vaapi":
            cmd = ["-vaapi_device", "/dev/dri/renderD128", "-vf", f"format=nv12,hwupload,scale_vaapi=-2:{res}", "-c:v", "h264_vaapi"]
        else:
            cmd.extend(["-vf", f"scale=-2:{res}"])
    return cmd

_media_probe_cache: dict[str, tuple[float, dict]] = {}

def probe_media_streams(file_path: str) -> dict:
    """
    Probes video and audio streams of a media file using ffprobe.
    Results are cached based on file mtime.
    
    Returns:
    {
        "video_codec": str or None,     # e.g., "h264", "hevc", "mpeg4"
        "audio_codec": str or None,     # e.g., "aac", "ac3", "dts", "mp3"
        "audio_channels": int or None,  # e.g., 2, 6
        "width": int or None,
        "height": int or None,
        "duration": float or None,
        "can_direct_stream_video": bool,# True if video codec is web-compatible (h264/avc)
        "can_direct_stream_audio": bool,# True if audio codec is web-compatible stereo (aac/mp3 <= 2ch)
    }
    """
    import json
    global _media_probe_cache
    if not file_path or not os.path.exists(file_path):
        return {
            "video_codec": None,
            "audio_codec": None,
            "audio_channels": None,
            "width": None,
            "height": None,
            "duration": None,
            "can_direct_stream_video": False,
            "can_direct_stream_audio": False,
            "audio_tracks": [],
        }

    try:
        mtime = os.path.getmtime(file_path)
        if file_path in _media_probe_cache:
            cached_mtime, cached_data = _media_probe_cache[file_path]
            if cached_mtime == mtime:
                return cached_data
    except Exception:
        mtime = 0.0

    ffprobe_bin = get_ffprobe_path()
    try:
        cmd = [
            ffprobe_bin,
            "-v", "quiet",
            "-print_format", "json",
            "-show_streams",
            "-show_format",
            file_path
        ]
        res = subprocess.run(cmd, capture_output=True, text=True, timeout=5)
        if res.returncode == 0 and res.stdout:
            data = json.loads(res.stdout)
            streams = data.get("streams", [])
            video_stream = next((s for s in streams if s.get("codec_type") == "video"), None)
            audio_streams = [s for s in streams if s.get("codec_type") == "audio"]

            video_codec = video_stream.get("codec_name", "").lower() if video_stream else None
            width = video_stream.get("width") if video_stream else None
            height = video_stream.get("height") if video_stream else None

            lang_map = {
                "eng": "English", "en": "English",
                "jpn": "Japanese", "ja": "Japanese",
                "spa": "Spanish", "es": "Spanish",
                "fra": "French", "fr": "French",
                "deu": "German", "ger": "German", "de": "German",
                "ita": "Italian", "it": "Italian",
                "por": "Portuguese", "pt": "Portuguese",
                "rus": "Russian", "ru": "Russian",
                "zho": "Chinese", "chi": "Chinese", "zh": "Chinese",
                "kor": "Korean", "ko": "Korean",
                "und": "Unknown"
            }

            audio_tracks = []
            for idx, s in enumerate(audio_streams):
                c_name = s.get("codec_name", "").lower()
                ch = s.get("channels")
                try:
                    ch = int(ch) if ch is not None else None
                except (ValueError, TypeError):
                    ch = None

                tags = s.get("tags") or {}
                lang = tags.get("language") or tags.get("LANGUAGE") or ""
                title = tags.get("title") or tags.get("TITLE") or ""

                ch_label = ""
                if ch == 1:
                    ch_label = "Mono"
                elif ch == 2:
                    ch_label = "Stereo"
                elif ch == 6:
                    ch_label = "5.1"
                elif ch == 8:
                    ch_label = "7.1"
                elif ch:
                    ch_label = f"{ch}ch"

                lang_clean = lang.lower().strip()
                friendly_lang = lang_map.get(lang_clean, lang.upper() if lang else f"Track {idx + 1}")

                parts = []
                if title:
                    parts.append(title)
                else:
                    parts.append(friendly_lang)

                details = []
                codec_str = c_name.upper() if c_name else ""
                if codec_str and not title:
                    details.append(codec_str)
                if ch_label and ch_label not in title:
                    details.append(ch_label)

                if details:
                    label = f"{parts[0]} ({', '.join(details)})"
                else:
                    label = parts[0]

                can_copy = (
                    c_name in ("aac", "mp3") and
                    (ch is None or ch <= 2)
                )

                audio_tracks.append({
                    "index": idx,
                    "stream_index": s.get("index"),
                    "codec": c_name,
                    "channels": ch,
                    "language": lang_clean,
                    "title": title,
                    "label": label,
                    "can_direct_stream": can_copy
                })

            primary_audio = audio_tracks[0] if audio_tracks else None
            audio_codec = primary_audio["codec"] if primary_audio else None
            audio_channels = primary_audio["channels"] if primary_audio else None

            fmt = data.get("format", {})
            try:
                duration = float(fmt.get("duration", 0.0))
            except (ValueError, TypeError):
                duration = None

            # Web browser video compatibility in fragmented MP4:
            # H.264 / AVC is universally playable across all browsers and devices
            can_direct_stream_video = video_codec in ("h264", "avc", "avc1")

            # Web browser audio compatibility in MP4:
            # AAC and MP3 stereo/mono are natively supported without transcoding
            can_direct_stream_audio = (
                audio_codec in ("aac", "mp3") and
                (audio_channels is None or audio_channels <= 2)
            )

            result = {
                "video_codec": video_codec,
                "audio_codec": audio_codec,
                "audio_channels": audio_channels,
                "width": width,
                "height": height,
                "duration": duration,
                "can_direct_stream_video": can_direct_stream_video,
                "can_direct_stream_audio": can_direct_stream_audio,
                "audio_tracks": audio_tracks,
            }
            _media_probe_cache[file_path] = (mtime, result)
            return result
    except Exception:
        pass

    # Safe fallback if ffprobe is not present or probe failed
    ext = os.path.splitext(file_path)[1].lower()
    is_mp4 = ext in [".mp4", ".m4v"]
    fallback_result = {
        "video_codec": "h264" if is_mp4 else "unknown",
        "audio_codec": "aac" if is_mp4 else "unknown",
        "audio_channels": 2 if is_mp4 else None,
        "width": None,
        "height": None,
        "duration": None,
        "can_direct_stream_video": is_mp4,
        "can_direct_stream_audio": is_mp4,
        "audio_tracks": [
            {
                "index": 0,
                "stream_index": 1 if is_mp4 else 0,
                "codec": "aac" if is_mp4 else "unknown",
                "channels": 2 if is_mp4 else None,
                "language": "und",
                "title": "",
                "label": "Default Audio (AAC Stereo)" if is_mp4 else "Default Audio",
                "can_direct_stream": is_mp4,
            }
        ],
    }
    return fallback_result

if __name__ == "__main__":
    ensure_ffmpeg()

