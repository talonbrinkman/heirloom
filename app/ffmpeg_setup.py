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

if __name__ == "__main__":
    ensure_ffmpeg()
