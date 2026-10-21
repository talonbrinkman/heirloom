import os
import subprocess
import shutil
import json
import re
import time
from typing import Optional, Union

LANG_MAP = {
    "eng": "English", "en": "English",
    "spa": "Spanish", "es": "Spanish",
    "fra": "French", "fre": "French", "fr": "French",
    "deu": "German", "ger": "German", "de": "German",
    "ita": "Italian", "it": "Italian",
    "por": "Portuguese", "pt": "Portuguese",
    "rus": "Russian", "ru": "Russian",
    "jpn": "Japanese", "ja": "Japanese",
    "zho": "Chinese", "chi": "Chinese", "zh": "Chinese",
    "kor": "Korean", "ko": "Korean",
    "ara": "Arabic", "ar": "Arabic",
    "hin": "Hindi", "hi": "Hindi",
    "nld": "Dutch", "dut": "Dutch", "nl": "Dutch",
    "pol": "Polish", "pl": "Polish",
    "swe": "Swedish", "sv": "Swedish",
    "nor": "Norwegian", "no": "Norwegian",
    "dan": "Danish", "da": "Danish",
    "fin": "Finnish", "fi": "Finnish",
    "tur": "Turkish", "tr": "Turkish",
    "vie": "Vietnamese", "vi": "Vietnamese",
    "tha": "Thai", "th": "Thai",
    "ukr": "Ukrainian", "uk": "Ukrainian",
    "ces": "Czech", "cze": "Czech", "cs": "Czech",
    "hun": "Hungarian", "hu": "Hungarian",
    "ell": "Greek", "gre": "Greek", "el": "Greek",
    "heb": "Hebrew", "he": "Hebrew",
    "ind": "Indonesian", "id": "Indonesian",
    "fil": "Filipino", "tl": "Tagalog",
    "msa": "Malay", "may": "Malay", "ms": "Malay",
    "und": "Unknown"
}

def get_subtitle_cache_dir() -> str:
    """Returns the local cache directory for extracted WebVTT subtitles."""
    base = "/config" if os.path.exists("/config") else os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "config"))
    sub_dir = os.path.join(base, "subtitles")
    os.makedirs(sub_dir, exist_ok=True)
    return sub_dir

def get_sidecar_subtitles(file_path: str) -> list[dict]:
    """
    Finds external sidecar subtitle files (.srt, .vtt) in the same directory as the media file.
    Matches files with the same base name (e.g., Movie.en.srt, Movie.forced.vtt).
    """
    if not file_path or not os.path.exists(file_path):
        return []
    
    dir_name = os.path.dirname(file_path)
    base_name = os.path.splitext(os.path.basename(file_path))[0]
    sidecars = []
    
    try:
        if not os.path.isdir(dir_name):
            return []
            
        for fname in sorted(os.listdir(dir_name)):
            lower_name = fname.lower()
            if not (lower_name.endswith(".srt") or lower_name.endswith(".vtt")):
                continue
            
            f_base, ext = os.path.splitext(fname)
            ext_clean = ext.lower().lstrip(".")
            
            # Subtitle file must start with the media file's base name
            if f_base == base_name or f_base.startswith(base_name + ".") or f_base.startswith(base_name + "_"):
                full_sub_path = os.path.join(dir_name, fname)
                suffix = f_base[len(base_name):].lower().replace("_", ".").strip(".")
                parts = [p for p in suffix.split(".") if p]
                
                lang_code = "und"
                is_forced = "forced" in parts
                is_sdh = "sdh" in parts or "cc" in parts
                
                for p in parts:
                    if p in LANG_MAP or len(p) in (2, 3):
                        lang_code = p
                        break
                        
                lang_name = LANG_MAP.get(lang_code, lang_code.upper() if lang_code != "und" else "")
                display_title = lang_name or "Sidecar"
                
                tags = []
                if is_forced:
                    tags.append("Forced")
                if is_sdh:
                    tags.append("SDH")
                tags.append(ext_clean.upper())
                
                label = f"{display_title} ({', '.join(tags)})" if tags else display_title
                
                sidecars.append({
                    "id": f"sidecar:{fname}",
                    "type": "sidecar",
                    "file_path": full_sub_path,
                    "filename": fname,
                    "codec": ext_clean,
                    "is_text": True,
                    "is_bitmap": False,
                    "language": lang_code,
                    "title": fname,
                    "label": label,
                    "is_forced": is_forced,
                    "is_default": False,
                    "is_hearing_impaired": is_sdh,
                })
    except Exception:
        pass
        
    return sidecars

def sanitize_subtitle_line(line: str) -> str:
    """
    Cleans up common OCR and ripped subtitle formatting artifacts, such as
    missing spaces after punctuation, speaker tags touching dialogue, etc.
    """
    if not line or '-->' in line or line.startswith(('WEBVTT', 'NOTE', 'STYLE', 'REGION')):
        return line
    if line.isdigit():
        return line
    
    # 1. Closing brackets/parentheses directly touching a letter or number: [Speaker]Hello -> [Speaker] Hello
    line = re.sub(r'([\]\)\}])([A-Za-z0-9])', r'\1 \2', line)
    
    # 2. Opening brackets directly touching letter/number if preceded by a letter: Hello[Speaker] -> Hello [Speaker]
    line = re.sub(r'([A-Za-z0-9])([\[\(\{])', r'\1 \2', line)

    # 3. HTML/VTT closing tag touching letter: </i>Hello -> </i> Hello, or Period before opening tag: Yes.<i> -> Yes. <i>
    line = re.sub(r'(</[a-zA-Z0-9]+>)([A-Za-z0-9])', r'\1 \2', line)
    line = re.sub(r'([.?!,:;])(<[a-zA-Z0-9]+[ >])', r'\1 \2', line)

    # 4. Closing quotation mark touching letter: "Hello."He said -> "Hello." He said
    line = re.sub(r'([.?!,]["\'”’])([A-Za-z0-9])', r'\1 \2', line)

    # 5. Punctuation (comma, colon, semicolon, exclamation, question mark) touching letter:
    line = re.sub(r'([,;:!?])([A-Za-z])', r'\1 \2', line)

    # 6. Period directly touching a letter:
    # Avoid abbreviations like 'U.S.A.', 'e.g.', 'i.e.' by ensuring not preceded by a single letter at word boundary
    line = re.sub(r'(?<!\b[A-Za-z])([.])([A-Za-z])', r'\1 \2', line)
    # Also handle single-letter 'I.' followed by word: e.g. "said I.Next" -> "said I. Next"
    line = re.sub(r'(?<=\bI\.)([A-Za-z])', r' \1', line)

    # 7. Collapse duplicate inline spaces
    line = re.sub(r'[ \t]{2,}', ' ', line)
    return line.strip()

def sanitize_vtt_content(vtt_content: str) -> str:
    """
    Sanitizes each dialogue line in a WebVTT or SubRip content string.
    """
    lines = vtt_content.splitlines()
    cleaned = [sanitize_subtitle_line(line) for line in lines]
    return "\n".join(cleaned)

def extract_subtitle_to_vtt(media_path: str, stream_index: int, output_vtt_path: str) -> bool:
    """
    Extracts an embedded text subtitle stream from a video container into a WebVTT file using ffmpeg.
    Sanitizes dialogue lines and uses atomic writes to avoid race conditions.
    """
    try:
        ffmpeg_bin = get_ffmpeg_path()
        os.makedirs(os.path.dirname(output_vtt_path), exist_ok=True)
        tmp_vtt = output_vtt_path + f".tmp_{os.getpid()}_{int(time.time())}.vtt"
        
        cmd = [
            ffmpeg_bin,
            "-v", "error",
            "-y",
            "-i", media_path,
            "-map", f"0:{stream_index}",
            "-f", "webvtt",
            tmp_vtt
        ]
        res = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
        if res.returncode == 0 and os.path.exists(tmp_vtt) and os.path.getsize(tmp_vtt) > 0:
            try:
                with open(tmp_vtt, "r", encoding="utf-8", errors="ignore") as f:
                    raw_content = f.read()
                cleaned_content = sanitize_vtt_content(raw_content)
                with open(tmp_vtt, "w", encoding="utf-8") as f:
                    f.write(cleaned_content)
            except Exception:
                pass
            os.replace(tmp_vtt, output_vtt_path)
            return True
        else:
            if os.path.exists(tmp_vtt):
                try:
                    os.remove(tmp_vtt)
                except OSError:
                    pass
            return False
    except Exception:
        return False

def shift_vtt_timestamps(vtt_content: str, offset_seconds: float) -> str:
    """
    Shifts all WebVTT cue timestamps backwards by offset_seconds to align with video playback.
    Cues that end before 0.0 are excluded. Sanitizes dialogue lines for clean spacing.
    """
    if offset_seconds <= 0.0:
        return sanitize_vtt_content(vtt_content)

    def parse_vtt_time(ts_str: str) -> float:
        parts = ts_str.strip().split(":")
        if len(parts) == 3:
            h, m, s = parts
            return float(h) * 3600.0 + float(m) * 60.0 + float(s)
        elif len(parts) == 2:
            m, s = parts
            return float(m) * 60.0 + float(s)
        return float(parts[0])

    def format_vtt_time(sec: float) -> str:
        sec = max(0.0, sec)
        h = int(sec // 3600)
        m = int((sec % 3600) // 60)
        s = sec % 60
        return f"{h:02d}:{m:02d}:{s:06.3f}"

    lines = vtt_content.splitlines()
    output_lines = ["WEBVTT", ""]
    i = 0
    while i < len(lines):
        if "-->" in lines[i]:
            break
        i += 1

    while i < len(lines):
        line = lines[i].strip()
        if not line:
            i += 1
            continue

        cue_id = ""
        if "-->" not in line and (i + 1) < len(lines) and "-->" in lines[i + 1]:
            cue_id = line
            i += 1
            line = lines[i].strip()

        if "-->" in line:
            parts = line.split("-->")
            start_str = parts[0].strip()
            end_parts = parts[1].strip().split()
            end_str = end_parts[0]
            settings_str = " ".join(end_parts[1:]) if len(end_parts) > 1 else ""

            try:
                start_sec = parse_vtt_time(start_str) - offset_seconds
                end_sec = parse_vtt_time(end_str) - offset_seconds
            except Exception:
                start_sec = None
                end_sec = None

            cue_text_lines = []
            i += 1
            while i < len(lines) and lines[i].strip():
                cue_text_lines.append(sanitize_subtitle_line(lines[i]))
                i += 1

            if end_sec is not None and end_sec > 0.0:
                shifted_timing = f"{format_vtt_time(start_sec)} --> {format_vtt_time(end_sec)}"
                if settings_str:
                    shifted_timing += f" {settings_str}"
                if cue_id:
                    output_lines.append(cue_id)
                output_lines.append(shifted_timing)
                output_lines.extend(cue_text_lines)
                output_lines.append("")
        else:
            i += 1

    return "\n".join(output_lines)

_keyframe_cache: dict[str, dict[float, float]] = {}

def get_keyframe_before_time(file_path: str, target_time: float) -> float:
    """
    Finds the exact keyframe presentation timestamp (PTS) at or immediately preceding target_time.
    When video is stream-copied with FFmpeg using -noaccurate_seek, FFmpeg snaps to this keyframe.
    Synchronizing subtitle shifts to this keyframe ensures subtitles align frame-accurately
    with the video and audio.
    """
    if target_time <= 0.5:
        return 0.0

    target_rounded = round(target_time, 3)

    if file_path in _keyframe_cache and target_rounded in _keyframe_cache[file_path]:
        return _keyframe_cache[file_path][target_rounded]

    # Search window [target - 35, target + 0.1]
    window_start = max(0.0, target_rounded - 35.0)
    window_end = target_rounded + 0.1
    cmd = [
        get_ffprobe_path(),
        "-v", "error",
        "-read_intervals", f"{window_start}%{window_end}",
        "-select_streams", "v:0",
        "-show_packets",
        "-show_entries", "packet=pts_time,flags",
        "-of", "csv=p=0",
        file_path
    ]
    try:
        out = subprocess.check_output(cmd, stderr=subprocess.DEVNULL, timeout=5).decode()
        kfs = []
        for line in out.splitlines():
            if "K" in line:
                parts = line.split(",")
                try:
                    pts = float(parts[0])
                    if pts <= target_time + 0.001:
                        kfs.append(pts)
                except ValueError:
                    pass
        if not kfs and window_start > 0.0:
            # Fallback wider search window if GOP is unusually long
            cmd[3] = f"{max(0.0, target_rounded - 90.0)}%{window_end}"
            out = subprocess.check_output(cmd, stderr=subprocess.DEVNULL, timeout=5).decode()
            for line in out.splitlines():
                if "K" in line:
                    parts = line.split(",")
                    try:
                        pts = float(parts[0])
                        if pts <= target_time + 0.001:
                            kfs.append(pts)
                    except ValueError:
                        pass
        if kfs:
            best_kf = max(kfs)
            if file_path not in _keyframe_cache:
                _keyframe_cache[file_path] = {}
            if len(_keyframe_cache[file_path]) > 200:
                _keyframe_cache[file_path].clear()
            _keyframe_cache[file_path][target_rounded] = best_kf
            return best_kf
    except Exception:
        pass

    return target_time

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

def parse_burn_sub_index(burn_sub: Optional[Union[str, int]], subtitle_tracks: list[dict]) -> Optional[int]:
    """
    Resolves a burn_sub track identifier (e.g. 'stream:2', '2', or a track ID)
    into an absolute media container stream index.
    Returns None if subtitle should not be burned in or track is not found.
    """
    if not burn_sub:
        return None
    sub_str = str(burn_sub).strip().lower()
    if sub_str in ("", "off", "none", "false", "0"):
        return None
    if sub_str.startswith("stream:"):
        raw_idx = sub_str.replace("stream:", "")
        try:
            target_idx = int(raw_idx)
            for s in subtitle_tracks:
                if s.get("stream_index") == target_idx:
                    return target_idx
            return target_idx
        except ValueError:
            return None
    try:
        target_idx = int(sub_str)
        for s in subtitle_tracks:
            if s.get("stream_index") == target_idx:
                return target_idx
    except ValueError:
        pass
    for s in subtitle_tracks:
        if s.get("id") == burn_sub:
            return s.get("stream_index")
    return None

def get_video_transcode_pipeline(
    res: str = "original",
    profile: str = "auto",
    burn_sub_stream: Optional[int] = None
) -> tuple[list[str], str, str]:
    """
    Constructs FFmpeg video encoding parameters, filter graph, and stream mapping.
    Handles hardware acceleration, resolution downscaling, and dynamic bitmap subtitle burn-in.
    Returns: (pipeline_ffmpeg_args, video_map_specifier, encoder_type)
    """
    enc_type, base_args = get_working_encoder(profile)
    
    if burn_sub_stream is not None:
        # Dynamic filter_complex pipeline: burns bitmap subtitles directly into video frames
        if enc_type == "vaapi":
            if res != "original":
                fc = f"[0:v:0][0:{burn_sub_stream}]overlay,scale=-2:{res},format=nv12,hwupload[v]"
            else:
                fc = f"[0:v:0][0:{burn_sub_stream}]overlay,format=nv12,hwupload[v]"
            args = ["-vaapi_device", "/dev/dri/renderD128", "-filter_complex", fc, "-c:v", "h264_vaapi"]
        else:
            if res != "original":
                fc = f"[0:v:0][0:{burn_sub_stream}]overlay[vsub];[vsub]scale=-2:{res}[v]"
            else:
                fc = f"[0:v:0][0:{burn_sub_stream}]overlay[v]"
            args = ["-filter_complex", fc] + list(base_args)
        return args, "[v]", enc_type
    else:
        # Standard video transcode without subtitle burn-in
        if res != "original":
            if enc_type == "vaapi":
                args = ["-vaapi_device", "/dev/dri/renderD128", "-vf", f"format=nv12,hwupload,scale_vaapi=-2:{res}", "-c:v", "h264_vaapi"]
            else:
                args = list(base_args) + ["-vf", f"scale=-2:{res}"]
        else:
            args = list(base_args)
        return args, "0:v:0", enc_type

def get_video_transcode_args(res: str = "original", profile: str = "auto") -> list[str]:
    """Generates FFmpeg video encoding parameters based on resolution and hardware profile."""
    args, _, _ = get_video_transcode_pipeline(res=res, profile=profile, burn_sub_stream=None)
    return args

_media_probe_cache: dict[str, tuple[float, dict]] = {}

def probe_media_streams(file_path: str) -> dict:
    """
    Probes video, audio, and subtitle streams of a media file using ffprobe.
    Discovers embedded streams and local sidecar subtitles.
    Results are cached based on file mtime.
    """
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
            "subtitle_tracks": [],
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
            sub_streams = [s for s in streams if s.get("codec_type") == "subtitle"]

            video_codec = video_stream.get("codec_name", "").lower() if video_stream else None
            width = video_stream.get("width") if video_stream else None
            height = video_stream.get("height") if video_stream else None

            # Audio tracks
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
                friendly_lang = LANG_MAP.get(lang_clean, lang.upper() if lang else f"Track {idx + 1}")

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

            # Subtitle tracks: sidecars + embedded streams
            sidecar_tracks = get_sidecar_subtitles(file_path)
            embedded_subtitles = []
            for idx, s in enumerate(sub_streams):
                c_name = s.get("codec_name", "").lower()
                stream_idx = s.get("index")
                tags = s.get("tags") or {}
                lang = tags.get("language") or tags.get("LANGUAGE") or ""
                title = tags.get("title") or tags.get("TITLE") or ""
                disp = s.get("disposition") or {}
                is_forced = bool(disp.get("forced"))
                is_default = bool(disp.get("default"))
                is_sdh = bool(disp.get("hearing_impaired"))
                
                # Check if text-based or bitmap (PGS / DVDsub)
                is_text = c_name in ("subrip", "srt", "ass", "ssa", "mov_text", "webvtt", "text")
                is_bitmap = not is_text
                
                lang_clean = lang.lower().strip()
                friendly_lang = LANG_MAP.get(lang_clean, lang.upper() if lang else f"Track {idx + 1}")

                parts = []
                if title:
                    parts.append(title)
                else:
                    parts.append(friendly_lang)

                details = []
                if is_forced and "forced" not in (title or "").lower():
                    details.append("Forced")
                if is_sdh and "sdh" not in (title or "").lower():
                    details.append("SDH")
                if not is_text:
                    codec_display = "PGS" if "pgs" in c_name else ("VobSub" if "dvd" in c_name else (c_name.upper() if c_name else "Bitmap"))
                    details.append(f"{codec_display} (Burn-in)")

                if details:
                    label = f"{parts[0]} [{', '.join(details)}]"
                else:
                    label = parts[0]

                embedded_subtitles.append({
                    "id": f"stream:{stream_idx}",
                    "type": "embedded",
                    "stream_index": stream_idx,
                    "codec": c_name,
                    "is_text": is_text,
                    "is_bitmap": is_bitmap,
                    "language": lang_clean or "und",
                    "title": title,
                    "label": label,
                    "is_forced": is_forced,
                    "is_default": is_default,
                    "is_hearing_impaired": is_sdh,
                })

            all_subtitles = sidecar_tracks + embedded_subtitles

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

            bitrate = None
            try:
                if fmt.get("bit_rate"):
                    bitrate = int(fmt.get("bit_rate"))
                elif fmt.get("size") and duration and duration > 0:
                    bitrate = int(float(fmt.get("size")) * 8 / duration)
            except (ValueError, TypeError):
                bitrate = None

            result = {
                "video_codec": video_codec,
                "audio_codec": audio_codec,
                "audio_channels": audio_channels,
                "width": width,
                "height": height,
                "duration": duration,
                "bitrate": bitrate,
                "can_direct_stream_video": can_direct_stream_video,
                "can_direct_stream_audio": can_direct_stream_audio,
                "audio_tracks": audio_tracks,
                "subtitle_tracks": all_subtitles,
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
        "subtitle_tracks": get_sidecar_subtitles(file_path),
    }
    return fallback_result

if __name__ == "__main__":
    ensure_ffmpeg()

