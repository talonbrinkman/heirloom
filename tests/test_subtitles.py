import hashlib
import os
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from app import subtitles


def test_opensubtitles_hash_and_lang_mapping(tmp_path):
    assert subtitles.to_opensubtitles_lang("eng") == "en"
    assert subtitles.to_opensubtitles_lang("spa") == "es"
    assert subtitles.to_opensubtitles_lang("fr") == "fr"

    small = tmp_path / "tiny.mkv"
    small.write_bytes(b"abc")
    assert subtitles.compute_opensubtitles_hash(str(small)) is None

    media = tmp_path / "Movie.2024.1080p.WEB-DL.mkv"
    media.write_bytes(b"\x01\x02\x03\x04" * 40000)  # 160 KB >= 128 KB
    h = subtitles.compute_opensubtitles_hash(str(media))
    assert h is not None and len(h) == 16


def test_score_subtitle_candidate_ranks_hash_and_release_overlap_first():
    local_name = "Dexter.S01E03.1080p.BluRay.x264-GROUP.mkv"
    candidates = [
        {
            "attributes": {
                "moviehash_match": False,
                "release": "Dexter.S01E03.HDTV.XviD",
                "machine_translated": False,
                "download_count": 50000,
            }
        },
        {
            "attributes": {
                "moviehash_match": False,
                "release": "Dexter.S01E03.1080p.BluRay.x264-GROUP",
                "machine_translated": False,
                "download_count": 200,
            }
        },
        {
            "attributes": {
                "moviehash_match": True,
                "release": "Dexter.S01E03.720p.WEB",
                "machine_translated": False,
                "download_count": 10,
            }
        },
    ]
    ranked = sorted(candidates, key=lambda c: subtitles.score_subtitle_candidate(c, local_name, "Dexter"), reverse=True)
    assert ranked[0]["attributes"]["moviehash_match"] is True
    assert "BluRay" in ranked[1]["attributes"]["release"]
    assert "HDTV" in ranked[2]["attributes"]["release"]


def test_srt_bytes_to_vtt_conversion():
    srt = b"\xef\xbb\xbf1\r\n00:00:01,500 --> 00:00:03,000\r\nHello world!\r\n"
    vtt = subtitles.srt_bytes_to_vtt(srt)
    assert vtt.startswith("WEBVTT")
    assert "00:00:01.500 --> 00:00:03.000" in vtt
    assert "Hello world!" in vtt


def test_fetched_subtitle_labels_match_sidecar_format(tmp_path, monkeypatch):
    monkeypatch.setattr(subtitles, "get_subtitle_cache_dir", lambda: str(tmp_path))
    sub_dir = subtitles.get_fetched_subtitle_dir("movie", 42)
    with open(os.path.join(sub_dir, "Movie.123.sdh.en.vtt"), "w", encoding="utf-8") as fp:
        fp.write("WEBVTT\n\n00:00:01.000 --> 00:00:02.000\nHi\n")

    tracks = subtitles.list_fetched_subtitles("movie", 42)
    assert len(tracks) == 1
    assert tracks[0]["label"] == "English (SDH, VTT)"
    assert tracks[0]["id"] == "fetched:Movie.123.sdh.en.vtt"


def test_sidecar_discovery_and_probe_cache_invalidation(tmp_path):
    from app import ffmpeg_setup

    media = tmp_path / "Show.S01E01.mkv"
    media.write_bytes(b"video-data")
    media_str = str(media)
    mtime = os.path.getmtime(media_str)

    ffmpeg_setup._media_probe_cache[media_str] = (mtime, {"subtitle_tracks": []})
    assert ffmpeg_setup.get_cached_media_probe(media_str) is not None

    sidecar = tmp_path / "Show.S01E01.en.vtt"
    sidecar.write_text("WEBVTT\n\n00:00:01.000 --> 00:00:02.000\nLine 1\n", encoding="utf-8")

    ffmpeg_setup.invalidate_media_probe_cache(media_str)
    assert ffmpeg_setup.get_cached_media_probe(media_str) is None

    sidecars = ffmpeg_setup.get_sidecar_subtitles(media_str)
    assert len(sidecars) == 1
    assert sidecars[0]["id"] == "sidecar:Show.S01E01.en.vtt"
    assert sidecars[0]["label"] == "English (VTT)"
