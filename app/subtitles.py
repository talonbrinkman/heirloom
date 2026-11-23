import os
import re
import struct
from typing import Any, Optional

from sqlmodel import Session, select

from .ffmpeg_setup import LANG_MAP, get_subtitle_cache_dir, sanitize_vtt_content
from .models import SystemConfig

try:
    import httpx
except ImportError:
    httpx = None

OPENSUBTITLES_API_BASE = "https://api.opensubtitles.com/api/v1"
OPENSUBTITLES_USER_AGENT = "Heirloom v2.1.8"

ISO_639_2_TO_1: dict[str, str] = {
    "eng": "en",
    "spa": "es",
    "fra": "fr",
    "fre": "fr",
    "deu": "de",
    "ger": "de",
    "ita": "it",
    "por": "pt",
    "jpn": "ja",
    "zho": "zh",
    "chi": "zh",
    "kor": "ko",
    "ara": "ar",
    "hin": "hi",
    "rus": "ru",
    "nld": "nl",
    "dut": "nl",
    "pol": "pl",
    "swe": "sv",
    "nor": "no",
    "dan": "da",
    "fin": "fi",
    "tur": "tr",
    "vie": "vi",
    "tha": "th",
    "ukr": "uk",
}


def to_opensubtitles_lang(lang_code: Optional[str]) -> str:
    code = (lang_code or "eng").strip().lower()
    if len(code) == 2:
        return code
    return ISO_639_2_TO_1.get(code, code[:2] if len(code) >= 2 else "en")


def get_opensubtitles_api_key(session: Session) -> str:
    cfg = session.exec(select(SystemConfig).where(SystemConfig.key == "opensubtitles_api_key")).first()
    if cfg and cfg.value and cfg.value.strip():
        return cfg.value.strip()
    return (os.getenv("OPENSUBTITLES_API_KEY") or "").strip()


def get_fetched_subtitle_dir(media_type: str, item_id: int) -> str:
    sub_dir = os.path.join(get_subtitle_cache_dir(), f"fetched_{media_type}_{item_id}")
    os.makedirs(sub_dir, exist_ok=True)
    return sub_dir


def list_fetched_subtitles(media_type: str, item_id: int) -> list[dict[str, Any]]:
    sub_dir = os.path.join(get_subtitle_cache_dir(), f"fetched_{media_type}_{item_id}")
    if not os.path.isdir(sub_dir):
        return []
    tracks: list[dict[str, Any]] = []
    try:
        for fname in sorted(os.listdir(sub_dir)):
            lower = fname.lower()
            if not (lower.endswith(".vtt") or lower.endswith(".srt")):
                continue
            parts = os.path.splitext(fname)[0].split(".")
            lang_code = "und"
            is_hi = "hi" in [p.lower() for p in parts] or "sdh" in [p.lower() for p in parts]
            for p in reversed(parts):
                pl = p.lower()
                if pl in LANG_MAP or (len(pl) == 2 and pl.isalpha()):
                    lang_code = pl
                    break
            lang_name = LANG_MAP.get(lang_code, lang_code.upper() if lang_code != "und" else "Sidecar")
            ext_clean = os.path.splitext(fname)[1].lstrip(".").lower() or "vtt"
            tags = []
            if is_hi:
                tags.append("SDH")
            tags.append(ext_clean.upper())
            tracks.append({
                "id": f"fetched:{fname}",
                "type": "sidecar",
                "file_path": os.path.join(sub_dir, fname),
                "filename": fname,
                "codec": ext_clean,
                "is_text": True,
                "is_bitmap": False,
                "language": lang_code,
                "title": fname,
                "label": f"{lang_name} ({', '.join(tags)})",
                "is_forced": False,
                "is_default": False,
                "is_hearing_impaired": is_hi,
            })
    except Exception:
        pass
    return tracks


def compute_opensubtitles_hash(file_path: str) -> Optional[str]:
    """Compute the standard 64-bit OpenSubtitles moviehash (16-char hex) for exact file matching."""
    if not file_path or not os.path.exists(file_path):
        return None
    chunk_size = 65536
    try:
        file_size = os.path.getsize(file_path)
        if file_size < chunk_size * 2:
            return None
        total = file_size
        fmt = "<" + ("Q" * (chunk_size // 8))
        with open(file_path, "rb") as fp:
            head = fp.read(chunk_size)
            fp.seek(max(0, file_size - chunk_size))
            tail = fp.read(chunk_size)
        for buf in (head, tail):
            if len(buf) != chunk_size:
                return None
            total = (total + sum(struct.unpack(fmt, buf))) & 0xFFFFFFFFFFFFFFFF
        return f"{total:016x}"
    except OSError:
        return None


def _tokenize_release_name(name: Optional[str]) -> set[str]:
    cleaned = re.sub(r"[^a-z0-9]+", " ", (name or "").lower()).strip()
    return {tok for tok in cleaned.split() if len(tok) >= 2}


def score_subtitle_candidate(
    candidate: dict[str, Any],
    local_filename: str = "",
    expected_title: str = "",
) -> tuple[int, int, int, int, int]:
    """
    Higher tuple = better match:
    1) Exact moviehash match
    2) Title match against feature_details / release
    3) Token overlap with local media filename (resolution, source, release group)
    4) Non-machine-translated tie-breaker
    5) Download count
    """
    attrs = candidate.get("attributes") or {}
    hash_score = 1 if attrs.get("moviehash_match") else 0
    feature = attrs.get("feature_details") or {}
    feature_text = " ".join(
        str(x) for x in (
            feature.get("parent_title"),
            feature.get("movie_name"),
            feature.get("title"),
            attrs.get("release"),
        ) if x
    )
    title_tokens = _tokenize_release_name(expected_title)
    cand_tokens = _tokenize_release_name(feature_text)
    title_match = 1 if (title_tokens and title_tokens.issubset(cand_tokens)) else 0

    local_tokens = _tokenize_release_name(os.path.splitext(local_filename)[0]) if local_filename else set()
    release_tokens = _tokenize_release_name(attrs.get("release"))
    overlap = len(local_tokens & release_tokens) if (local_tokens and release_tokens) else 0
    not_mt = 0 if attrs.get("machine_translated") else 1
    dl_count = int(attrs.get("download_count") or 0)
    return (hash_score, title_match, overlap, not_mt, dl_count)


async def search_opensubtitles(
    api_key: str,
    *,
    title: str,
    media_type: str,
    file_path: str = "",
    year: Optional[str] = None,
    tmdb_id: Optional[int] = None,
    season: Optional[int] = None,
    episode: Optional[int] = None,
    language: str = "eng",
    custom_query: str = "",
) -> list[dict[str, Any]]:
    if httpx is None:
        raise RuntimeError("httpx is required for OpenSubtitles requests.")
    if not api_key:
        raise ValueError("OpenSubtitles API key is not configured.")

    os_lang = to_opensubtitles_lang(language)
    params: dict[str, Any] = {
        "languages": os_lang,
        "type": "episode" if media_type == "tv" else "movie",
    }
    q_override = (custom_query or "").strip()

    if q_override:
        params["query"] = q_override
        if media_type == "tv":
            if season is not None:
                params["season_number"] = int(season)
            if episode is not None:
                params["episode_number"] = int(episode)
    else:
        file_hash = compute_opensubtitles_hash(file_path) if file_path else None
        if file_hash:
            params["moviehash"] = file_hash
        if tmdb_id:
            if media_type == "tv":
                params["parent_tmdb_id"] = int(tmdb_id)
            else:
                params["tmdb_id"] = int(tmdb_id)
        else:
            if title:
                params["query"] = title.strip()
            if year and str(year).strip().isdigit():
                params["year"] = int(str(year).strip())
        if media_type == "tv":
            if season is not None:
                params["season_number"] = int(season)
            if episode is not None:
                params["episode_number"] = int(episode)

    headers = {
        "Api-Key": api_key,
        "User-Agent": OPENSUBTITLES_USER_AGENT,
        "Accept": "application/json",
    }

    async with httpx.AsyncClient(timeout=15.0, follow_redirects=True) as client:
        res = await client.get(f"{OPENSUBTITLES_API_BASE}/subtitles", params=params, headers=headers)

    if res.status_code == 401 or res.status_code == 403:
        raise ValueError("Invalid OpenSubtitles API key.")
    if res.status_code == 429:
        raise RuntimeError("OpenSubtitles rate limit reached. Try again shortly.")
    if res.status_code != 200:
        raise RuntimeError(f"OpenSubtitles search failed (HTTP {res.status_code}).")

    raw_items = res.json().get("data") or []
    local_base = os.path.basename(file_path) if file_path else ""
    expected_title = q_override or title
    ranked = sorted(
        raw_items,
        key=lambda c: score_subtitle_candidate(c, local_base, expected_title),
        reverse=True,
    )

    # Filter out unrelated non-hash-matched shows/movies when auto-searching by title
    if not q_override and expected_title:
        title_filtered = [
            c for c in ranked
            if (c.get("attributes") or {}).get("moviehash_match")
            or score_subtitle_candidate(c, local_base, expected_title)[1] == 1
        ]
        if title_filtered:
            ranked = title_filtered

    results: list[dict[str, Any]] = []
    seen_file_ids: set[int] = set()
    for entry in ranked:
        attrs = entry.get("attributes") or {}
        files = attrs.get("files") or []
        if not files:
            continue
        first_file = files[0] or {}
        fid = first_file.get("file_id")
        if not fid or int(fid) in seen_file_ids:
            continue
        seen_file_ids.add(int(fid))
        results.append({
            "file_id": int(fid),
            "release": str(attrs.get("release") or first_file.get("file_name") or title or "Subtitle"),
            "language": str(attrs.get("language") or os_lang),
            "download_count": int(attrs.get("download_count") or 0),
            "moviehash_match": bool(attrs.get("moviehash_match")),
            "hearing_impaired": bool(attrs.get("hearing_impaired")),
            "fps": attrs.get("fps"),
        })
        if len(results) >= 12:
            break
    return results


def srt_bytes_to_vtt(raw_bytes: bytes) -> str:
    text = raw_bytes.decode("utf-8", errors="ignore").lstrip("\ufeff")
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    if text.lstrip().startswith("WEBVTT"):
        vtt = text
    else:
        vtt = "WEBVTT\n\n" + re.sub(r"(\d{2}:\d{2}:\d{2}),(\d{3})", r"\1.\2", text)
    return sanitize_vtt_content(vtt)


async def fetch_opensubtitles_file(
    api_key: str,
    *,
    file_id: int,
    media_type: str,
    item_id: int,
    language: str = "eng",
    release_name: str = "",
    hearing_impaired: bool = False,
    video_file_path: str = "",
) -> dict[str, Any]:
    if httpx is None:
        raise RuntimeError("httpx is required for OpenSubtitles requests.")
    if not api_key:
        raise ValueError("OpenSubtitles API key is not configured.")

    headers = {
        "Api-Key": api_key,
        "User-Agent": OPENSUBTITLES_USER_AGENT,
        "Content-Type": "application/json",
        "Accept": "application/json",
    }

    async with httpx.AsyncClient(timeout=20.0, follow_redirects=True) as client:
        dl_res = await client.post(
            f"{OPENSUBTITLES_API_BASE}/download",
            json={"file_id": int(file_id)},
            headers=headers,
        )
        if dl_res.status_code in (401, 403):
            raise ValueError("Invalid OpenSubtitles API key or download quota exceeded.")
        if dl_res.status_code == 406:
            raise RuntimeError("OpenSubtitles daily download quota exceeded.")
        if dl_res.status_code != 200:
            raise RuntimeError(f"OpenSubtitles download failed (HTTP {dl_res.status_code}).")
        dl_link = (dl_res.json() or {}).get("link")
        if not dl_link:
            raise RuntimeError("OpenSubtitles did not return a download link.")

        sub_res = await client.get(dl_link)
        if sub_res.status_code != 200:
            raise RuntimeError(f"Failed to download subtitle file (HTTP {sub_res.status_code}).")

    vtt_text = srt_bytes_to_vtt(sub_res.content)
    lang_clean = to_opensubtitles_lang(language)
    hi_tag = ".sdh" if hearing_impaired else ""

    track_id_prefix = "fetched"
    out_path = ""
    fname = ""

    # Save alongside media file if its directory exists and is writable; otherwise fall back to config folder
    if video_file_path:
        video_dir = os.path.dirname(video_file_path)
        video_stem = os.path.splitext(os.path.basename(video_file_path))[0]
        if video_dir and os.path.isdir(video_dir):
            candidate_name = f"{video_stem}.{lang_clean}{hi_tag}.vtt"
            candidate_path = os.path.join(video_dir, candidate_name)
            if os.path.exists(candidate_path):
                candidate_name = f"{video_stem}.{int(file_id)}.{lang_clean}{hi_tag}.vtt"
                candidate_path = os.path.join(video_dir, candidate_name)
            try:
                with open(candidate_path, "w", encoding="utf-8") as fp:
                    fp.write(vtt_text)
                track_id_prefix = "sidecar"
                out_path = candidate_path
                fname = candidate_name
            except OSError:
                out_path = ""

    if not out_path:
        safe_rel = re.sub(r"[^A-Za-z0-9._-]+", "_", (release_name or f"os_{file_id}").strip())[:60].strip("._-")
        if not safe_rel:
            safe_rel = f"os_{file_id}"
        fname = f"{safe_rel}.{int(file_id)}{hi_tag}.{lang_clean}.vtt"
        sub_dir = get_fetched_subtitle_dir(media_type, item_id)
        out_path = os.path.join(sub_dir, fname)
        with open(out_path, "w", encoding="utf-8") as fp:
            fp.write(vtt_text)

    lang_name = LANG_MAP.get(lang_clean, lang_clean.upper())
    tags = []
    if hearing_impaired:
        tags.append("SDH")
    tags.append("VTT")
    return {
        "id": f"{track_id_prefix}:{fname}",
        "type": "sidecar",
        "file_path": out_path,
        "filename": fname,
        "codec": "vtt",
        "is_text": True,
        "is_bitmap": False,
        "language": lang_clean,
        "title": fname,
        "label": f"{lang_name} ({', '.join(tags)})",
        "is_forced": False,
        "is_default": False,
        "is_hearing_impaired": hearing_impaired,
    }
