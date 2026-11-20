import json
import os
import sys
from pathlib import Path

import pytest
from sqlmodel import Session, SQLModel, create_engine

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app import loom
from app.models import LoomPeer, Movie, TVShow


@pytest.fixture
def db_session(tmp_path, monkeypatch):
    db_file = tmp_path / "test_heirloom.db"
    test_engine = create_engine(f"sqlite:///{db_file}", connect_args={"check_same_thread": False})
    SQLModel.metadata.create_all(test_engine)
    monkeypatch.setattr(loom, "sqlite_file_name", str(db_file))
    monkeypatch.setattr(loom, "engine", test_engine)
    with Session(test_engine) as session:
        yield session


def test_settings_split_bandwidth_and_removed_fields(db_session):
    defaults = loom.get_loom_settings(db_session)
    assert defaults["max_search_hops"] == 1
    assert defaults["max_sending_hops"] == 1
    saved = loom.save_loom_settings(
        db_session,
        {
            "enabled": True,
            "sync_dir": "/media/loom_test",
            "upload_bandwidth_limit_kbps": 512,
            "download_bandwidth_limit_kbps": 2048,
            "pause_on_active_streams": False,
            "max_quality": "720p",
            "max_search_hops": 4,
            "max_sending_hops": 2,
        },
    )
    assert saved["enabled"] is True
    assert saved["sync_dir"] == "/media/loom_test"
    assert saved["upload_bandwidth_limit_kbps"] == 512
    assert saved["download_bandwidth_limit_kbps"] == 2048
    assert saved["pause_on_active_streams"] is False
    assert saved["max_quality"] == "720p"
    assert saved["max_search_hops"] == 4
    assert saved["max_sending_hops"] == 2
    assert saved["max_hops"] == 4
    for removed in ("node_name", "public_url", "min_free_gb", "max_quota_gb", "monthly_cap_gb", "schedule_enabled"):
        assert removed not in saved


def test_manifest_rebuild_and_load_without_drive_access(db_session, tmp_path):
    movie_file = tmp_path / "Example.Movie.2024.1080p.mkv"
    movie_file.write_bytes(b"1234567890")
    ep_file = tmp_path / "Show.S01E02.720p.mkv"
    ep_file.write_bytes(b"abcdef")

    db_session.add(Movie(title="Example Movie", year="2024", file_path=str(movie_file)))
    db_session.add(TVShow(title="Example Show", season=1, episode=2, episode_title="Pilot 2", file_path=str(ep_file)))
    db_session.commit()

    manifest = loom.rebuild_loom_manifest(db_session)
    assert len(manifest["movies"]) == 1
    assert manifest["movies"][0]["title"] == "Example Movie"
    assert manifest["movies"][0]["file_size"] == 10
    assert len(manifest["tv"]) == 1
    assert manifest["tv"][0]["series_title"] == "Example Show"

    # Delete source files on disk; load_loom_manifest should still serve from cached manifest without touching drives
    movie_file.unlink()
    ep_file.unlink()
    cached = loom.load_loom_manifest(db_session)
    assert len(cached["movies"]) == 1
    assert len(cached["tv"]) == 1


def test_per_peer_onion_bundle_and_rotation(db_session, monkeypatch):
    created = []
    deleted = []
    counter = {"n": 0}

    def fake_create(private_key=None):
        counter["n"] += 1
        sid = f"peer{counter['n']}aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
        pk = private_key or f"ED25519-V3:secret{counter['n']}"
        created.append((sid, pk))
        return sid, pk

    def fake_delete(sid):
        deleted.append(sid)
        return True

    monkeypatch.setattr(loom.tor_onion_manager, "is_control_available", lambda: True)
    monkeypatch.setattr(loom.tor_onion_manager, "create_onion_service", fake_create)
    monkeypatch.setattr(loom.tor_onion_manager, "delete_onion_service", fake_delete)

    b1 = loom.export_peer_bundle(db_session)
    b2 = loom.export_peer_bundle(db_session)
    assert b1["node_id"] != b2["node_id"]
    assert b1["base_url"] != b2["base_url"]
    assert b1["base_url"].endswith(".onion")
    assert "name" not in b1

    # Verify SOCKS5 proxy routing for .onion URLs
    kwargs = loom.get_httpx_client_kwargs(b1["base_url"])
    assert kwargs["proxy"].startswith("socks5h://")
    assert "proxy" not in loom.get_httpx_client_kwargs("https://example.com")

    # Import a friend's .onion bundle and rotate our own .onion for that peer
    peer = loom.import_peer_bundle(
        db_session,
        {"node_id": "friend-uuid-1", "base_url": "http://friend12345.onion", "shared_secret": "sec123"},
        custom_name="Alice",
    )
    assert peer.status == "approved"
    assert peer.allow_incoming is False
    assert peer.allow_outgoing is False
    assert peer.local_onion_service_id is not None
    old_sid = peer.local_onion_service_id

    new_url = loom.rotate_peer_onion(db_session, peer)
    assert new_url.endswith(".onion")
    assert peer.local_onion_service_id != old_sid
    assert old_sid in deleted


def test_export_bundle_refuses_clearnet_leak_without_tor(db_session, monkeypatch):
    monkeypatch.setattr(loom.tor_onion_manager, "is_control_available", lambda: False)
    monkeypatch.delenv("LOOM_ALLOW_CLEARNET_FALLBACK", raising=False)
    with pytest.raises(RuntimeError, match="Tor ControlPort"):
        loom.export_peer_bundle(db_session, fallback_base_url="http://192.168.1.50:42069")


def test_can_transfer_now_without_caps_or_schedule(db_session):
    loom.save_loom_settings(db_session, {"enabled": True, "pause_on_active_streams": True})
    peer = LoomPeer(
        name="Bob",
        node_id="bob-1",
        base_url="http://bob.onion",
        shared_secret="abc",
        status="approved",
        allow_incoming=True,
        allow_outgoing=True,
    )
    db_session.add(peer)
    db_session.commit()

    allowed, status, _ = loom.can_transfer_now(db_session, peer, "incoming", has_active_streams_fn=lambda: False)
    assert allowed is True and status is None

    allowed, status, _ = loom.can_transfer_now(db_session, peer, "incoming", has_active_streams_fn=lambda: True)
    assert allowed is False and status == "paused_stream"


def test_peer_name_defaults_to_anonymous_and_ignores_pasted_json(db_session, monkeypatch):
    monkeypatch.setattr(loom.tor_onion_manager, "is_control_available", lambda: False)
    peer1 = loom.import_peer_bundle(
        db_session,
        {"node_id": "anon-1", "base_url": "http://anon1.onion", "shared_secret": "sec1"},
        custom_name="",
    )
    assert peer1.name == "Anonymous"

    peer2 = loom.import_peer_bundle(
        db_session,
        {"node_id": "anon-2", "base_url": "http://anon2.onion", "shared_secret": "sec2"},
        custom_name='{"version": 2, "node_id": "abc"}',
    )
    assert peer2.name == "Anonymous"


def test_specific_item_search_and_searchable_volumes(db_session, tmp_path):
    tv_dir = tmp_path / "tv"
    movie_dir = tmp_path / "movies"
    tv_dir.mkdir()
    movie_dir.mkdir()

    dexter_ep = tv_dir / "Dexter.S01E01.1080p.mkv"
    dexter_ep.write_bytes(b"dexter-ep-1")
    other_ep = tv_dir / "Breaking.Bad.S01E01.1080p.mkv"
    other_ep.write_bytes(b"bb-ep-1")
    it_movie = movie_dir / "It.2017.1080p.mkv"
    it_movie.write_bytes(b"it-movie")
    gravity_movie = movie_dir / "Gravity.2013.1080p.mkv"
    gravity_movie.write_bytes(b"gravity-movie")

    db_session.add(TVShow(title="Dexter", season=1, episode=1, episode_title="Dexter", file_path=str(dexter_ep)))
    db_session.add(TVShow(title="Breaking Bad", season=1, episode=1, episode_title="Pilot", file_path=str(other_ep)))
    db_session.add(Movie(title="It", year="2017", file_path=str(it_movie)))
    db_session.add(Movie(title="Gravity", year="2013", file_path=str(gravity_movie)))
    db_session.commit()

    loom.rebuild_loom_manifest(db_session)

    # Disabled by default: nothing is searchable
    assert loom.get_loom_settings(db_session)["searchable_volumes"] == []
    res_disabled = loom.search_loom_manifest(db_session, "Dexter")
    assert res_disabled["found"] is False
    assert res_disabled["tv"] == []

    # Enable only the TV volume
    loom.save_loom_settings(db_session, {"searchable_volumes": [str(tv_dir)]})
    res_dexter = loom.search_loom_manifest(db_session, "Dexter")
    assert res_dexter["found"] is True
    assert len(res_dexter["tv"]) == 1
    assert res_dexter["tv"][0]["series_title"] == "Dexter"
    assert "_file_path" not in res_dexter["tv"][0]

    # Movies volume is still disabled, so 'It' is not found
    assert loom.search_loom_manifest(db_session, "It")["found"] is False

    # Enable movies volume too: 'It' should match 'It' but NOT 'Gravity'
    loom.save_loom_settings(db_session, {"searchable_volumes": [str(tv_dir), str(movie_dir)]})
    res_it = loom.search_loom_manifest(db_session, "It")
    assert res_it["found"] is True
    assert [m["title"] for m in res_it["movies"]] == ["It"]

    # Reject wildcard or overly short queries
    with pytest.raises(ValueError):
        loom.search_loom_manifest(db_session, "*")
    with pytest.raises(ValueError):
        loom.search_loom_manifest(db_session, "a")


def test_handshake_does_not_duplicate_existing_peer(db_session, monkeypatch):
    monkeypatch.setattr(loom.tor_onion_manager, "is_control_available", lambda: True)
    counter = {"n": 0}

    def fake_create(private_key=None):
        counter["n"] += 1
        return f"onion{counter['n']}aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa", f"ED25519-V3:k{counter['n']}"

    monkeypatch.setattr(loom.tor_onion_manager, "create_onion_service", fake_create)

    bundle = loom.export_peer_bundle(db_session)
    peer = loom.import_peer_bundle(db_session, bundle, custom_name="SelfTest")
    local_node_id, local_onion = loom.get_peer_local_identity(db_session, peer)

    payload = json.dumps({"node_id": local_node_id, "base_url": local_onion}).encode("utf-8")
    hdrs = loom.sign_loom_headers(local_node_id, peer.shared_secret, "POST", "/api/loom/v1/handshake", payload)

    matched = loom.consume_issued_secret_for_handshake(
        db_session,
        hdrs,
        "POST",
        "/api/loom/v1/handshake",
        payload,
        peer_name="Anonymous",
        peer_base_url=local_onion,
    )
    assert matched is not None
    assert matched.id == peer.id
    all_peers = db_session.exec(loom.select(LoomPeer)).all()
    assert len(all_peers) == 1


def test_allow_relay_setting_and_relay_helpers(db_session):
    assert loom.get_loom_settings(db_session)["allow_relay"] is False
    saved = loom.save_loom_settings(db_session, {"allow_relay": True})
    assert saved["allow_relay"] is True

    # Loop guard records unseen request IDs and flags duplicates
    req_id = "req-loop-check-1"
    assert loom.mark_or_check_search_request_id(req_id, ttl_seconds=60.0) is False
    assert loom.mark_or_check_search_request_id(req_id, ttl_seconds=60.0) is True

    # Opaque relay token maps back to target peer item without exposing peer identity
    tok = loom.register_relay_token(target_peer_id=7, media_type="tv", remote_item_id=42, upstream_relay_token="up-tok")
    resolved = loom.resolve_relay_token(tok)
    assert resolved is not None
    assert resolved["target_peer_id"] == 7
    assert resolved["media_type"] == "tv"
    assert resolved["remote_item_id"] == 42
    assert resolved["upstream_relay_token"] == "up-tok"


def test_parse_and_search_season_episode_queries(db_session, tmp_path):
    assert loom.parse_loom_search_query("The Office S1") == {"query": "The Office", "season": 1, "episode": None, "media_type": "tv"}
    assert loom.parse_loom_search_query("The Office S1E4") == {"query": "The Office", "season": 1, "episode": 4, "media_type": "tv"}
    assert loom.parse_loom_search_query("Dexter Season 2 Episode 3") == {"query": "Dexter", "season": 2, "episode": 3, "media_type": "tv"}
    assert loom.parse_loom_search_query("Dexter Season 1 E3") == {"query": "Dexter", "season": 1, "episode": 3, "media_type": "tv"}
    assert loom.parse_loom_search_query("Dexter S1 Episode 3") == {"query": "Dexter", "season": 1, "episode": 3, "media_type": "tv"}
    assert loom.parse_loom_search_query("Dexter S01 Ep. 3") == {"query": "Dexter", "season": 1, "episode": 3, "media_type": "tv"}
    assert loom.parse_loom_search_query("Inception") == {"query": "Inception", "season": None, "episode": None, "media_type": None}

    tv_dir = tmp_path / "tv"
    tv_dir.mkdir()
    f1 = tv_dir / "Office.S01E01.mkv"
    f2 = tv_dir / "Office.S01E04.mkv"
    f3 = tv_dir / "Office.S02E01.mkv"
    for f in (f1, f2, f3):
        f.write_bytes(b"ep")

    db_session.add(TVShow(title="The Office", season=1, episode=1, episode_title="Pilot", file_path=str(f1)))
    db_session.add(TVShow(title="The Office", season=1, episode=4, episode_title="The Alliance", file_path=str(f2)))
    db_session.add(TVShow(title="The Office", season=2, episode=1, episode_title="The Dundies", file_path=str(f3)))
    db_session.commit()

    loom.rebuild_loom_manifest(db_session)
    loom.save_loom_settings(db_session, {"searchable_volumes": [str(tv_dir)]})

    res_s1 = loom.search_loom_manifest(db_session, "The Office S1")
    assert res_s1["found"] is True
    assert [(e["season"], e["episode"]) for e in res_s1["tv"]] == [(1, 1), (1, 4)]

    res_s1e4 = loom.search_loom_manifest(db_session, "The Office S1E4")
    assert res_s1e4["found"] is True
    assert [(e["season"], e["episode"]) for e in res_s1e4["tv"]] == [(1, 4)]


def test_deduplicate_loom_search_results_picks_best_source():
    movies = [
        {"id": 1, "title": "Inception", "year": "2010", "quality_label": "1080p", "file_size": 2000, "hops": 1, "peer_id": 10},
        {"id": 2, "title": "Inception", "year": "2010", "quality_label": "720p", "file_size": 1000, "hops": 0, "peer_id": 20},
        {"id": 3, "title": "Inception", "year": "2010", "quality_label": "1080p", "file_size": 2500, "hops": 0, "peer_id": 30},
    ]
    tv = [
        {"id": 10, "series_title": "The Office", "season": 1, "episode": 1, "quality_label": "720p", "file_size": 300, "hops": 0, "peer_id": 1},
        {"id": 11, "series_title": "The Office", "season": 1, "episode": 1, "quality_label": "1080p", "file_size": 600, "hops": 0, "peer_id": 2},
        {"id": 12, "series_title": "The Office", "season": 1, "episode": 2, "quality_label": "720p", "file_size": 320, "hops": 0, "peer_id": 1},
    ]
    deduped = loom.deduplicate_loom_search_results(movies, tv, [], [])

    assert len(deduped["movies"]) == 1
    assert deduped["movies"][0]["peer_id"] == 30
    assert deduped["movies"][0]["sources_count"] == 3

    assert len(deduped["tv"]) == 2
    ep1, ep2 = deduped["tv"]
    assert ep1["episode"] == 1 and ep1["peer_id"] == 2 and ep1["sources_count"] == 2
    assert ep2["episode"] == 2 and ep2["peer_id"] == 1 and ep2["sources_count"] == 1

    # Exact title match ranks ahead of prefix/substring matches
    dexter_tv = [
        {"id": 1, "series_title": "A Dexter Story", "season": 1, "episode": 3, "quality_label": "1080p", "file_size": 500, "hops": 0, "peer_id": 1},
        {"id": 2, "series_title": "Dexter: New Blood", "season": 1, "episode": 3, "quality_label": "1080p", "file_size": 500, "hops": 0, "peer_id": 1},
        {"id": 3, "series_title": "Dexter", "season": 1, "episode": 3, "quality_label": "1080p", "file_size": 500, "hops": 0, "peer_id": 1},
    ]
    dexter_dedup = loom.deduplicate_loom_search_results([], dexter_tv, [], [], query="Dexter S01E03")
    assert [t["series_title"] for t in dexter_dedup["tv"]] == [
        "Dexter",
        "Dexter: New Blood",
        "A Dexter Story",
    ]


def test_peer_rate_limit_and_concurrent_transfer_slots(db_session):
    defaults = loom.get_loom_settings(db_session)
    assert defaults["max_concurrent_transfers"] == 2
    assert defaults["max_requests_per_minute"] == 3

    saved = loom.save_loom_settings(db_session, {"max_concurrent_transfers": 3, "max_requests_per_minute": 2})
    assert saved["max_concurrent_transfers"] == 3
    assert saved["max_requests_per_minute"] == 2

    # Per-peer search rate limit
    pid = 9991
    assert loom.check_peer_search_rate_limit(pid, 2)[0] is True
    assert loom.check_peer_search_rate_limit(pid, 2)[0] is True
    ok, retry_after = loom.check_peer_search_rate_limit(pid, 2)
    assert ok is False and retry_after >= 1

    # Per-peer concurrent transfer slots
    assert loom.acquire_peer_transfer_slot(pid, 2) is True
    assert loom.acquire_peer_transfer_slot(pid, 2) is True
    assert loom.acquire_peer_transfer_slot(pid, 2) is False
    loom.release_peer_transfer_slot(pid)
    assert loom.acquire_peer_transfer_slot(pid, 2) is True
    loom.release_peer_transfer_slot(pid)
    loom.release_peer_transfer_slot(pid)


def test_cleanup_orphaned_and_cancelled_part_files(db_session, tmp_path):
    sync_dir = tmp_path / "loom_sync"
    active_target = sync_dir / "tv" / "The Office" / "Season 1" / "ep1.mkv"
    orphan_target = sync_dir / "tv" / "Dexter" / "Season 1" / "ep1.mkv"
    active_target.parent.mkdir(parents=True)
    orphan_target.parent.mkdir(parents=True)

    active_part = active_target.with_suffix(".mkv.part")
    orphan_part = orphan_target.with_suffix(".mkv.part")
    active_part.write_bytes(b"partial-active")
    orphan_part.write_bytes(b"partial-orphan")

    loom.save_loom_settings(db_session, {"sync_dir": str(sync_dir)})
    peer = LoomPeer(name="Alice", node_id="a1", base_url="http://a1.onion", shared_secret="s1", status="approved")
    db_session.add(peer)
    db_session.commit()
    db_session.refresh(peer)

    job = loom.LoomTransferJob(
        peer_id=peer.id,
        media_type="tv",
        remote_item_id=1,
        title="Pilot",
        series_title="The Office",
        season=1,
        episode=1,
        target_path=str(active_target),
        status="paused_stream",
    )
    db_session.add(job)
    db_session.commit()

    # Orphan sweep deletes orphan_part (and prunes empty Dexter dirs) while keeping paused active_part
    removed = loom.cleanup_orphaned_loom_part_files(db_session)
    assert removed == 1
    assert active_part.exists()
    assert not orphan_part.exists()
    assert not (sync_dir / "tv" / "Dexter").exists()

    # Direct cancel cleanup removes active_part too
    assert loom.cleanup_job_part_file(str(active_target), sync_dir=str(sync_dir)) is True
    assert not active_part.exists()


def test_allowed_filetypes_default_and_filtering(db_session, tmp_path):
    settings = loom.get_loom_settings(db_session)
    assert ".mp4" in settings["allowed_filetypes_list"]
    assert ".mkv" in settings["allowed_filetypes_list"]
    assert ".mp3" in settings["allowed_filetypes_list"]
    assert ".png" in settings["allowed_filetypes_list"]
    assert ".jpg" in settings["allowed_filetypes_list"]
    assert ".exe" not in settings["allowed_filetypes_list"]
    assert ".svg" not in settings["allowed_filetypes_list"]

    # Safe normalization strips invalid entries and normalizes case/dots
    saved = loom.save_loom_settings(db_session, {"allowed_filetypes": "MP4, .mkv, ../bad, .sh; .png"})
    assert saved["allowed_filetypes_list"] == [".mp4", ".mkv", ".sh", ".png"]

    # Search only returns files whose extension is in allowed_filetypes
    movie_dir = tmp_path / "movies"
    movie_dir.mkdir()
    ok_file = movie_dir / "Good.Movie.2024.mp4"
    bad_file = movie_dir / "Bad.Movie.2024.exe"
    ok_file.write_bytes(b"mp4")
    bad_file.write_bytes(b"exe")
    db_session.add(Movie(title="Good Movie", year="2024", file_path=str(ok_file)))
    db_session.add(Movie(title="Bad Movie", year="2024", file_path=str(bad_file)))
    db_session.commit()

    loom.rebuild_loom_manifest(db_session)
    loom.save_loom_settings(db_session, {"searchable_volumes": [str(movie_dir)], "allowed_filetypes": ".mp4, .mkv"})

    assert loom.search_loom_manifest(db_session, "Good Movie")["found"] is True
    assert loom.search_loom_manifest(db_session, "Bad Movie")["found"] is False


def test_peer_bundle_expiration_prunes_and_rejects(db_session, monkeypatch):
    deleted = []
    monkeypatch.setattr(loom.tor_onion_manager, "is_control_available", lambda: True)
    monkeypatch.setattr(
        loom.tor_onion_manager,
        "create_onion_service",
        lambda private_key=None: ("exp111aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa", "ED25519-V3:expkey"),
    )
    monkeypatch.setattr(loom.tor_onion_manager, "delete_onion_service", lambda sid: deleted.append(sid) or True)

    bundle = loom.export_peer_bundle(db_session)
    assert "expires_at" in bundle

    # Pruning at a future timestamp removes the bundle and tears down its .onion service
    future_ts = int(loom.time.time()) + loom.LOOM_BUNDLE_TTL_SECONDS + 10
    pruned = loom.prune_expired_issued_secrets(db_session, now_ts=future_ts)
    assert pruned == 1
    assert "exp111aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa" in deleted
    assert loom._get_issued_secrets(db_session) == []

    # Importing an already-expired bundle raises ValueError
    expired_bundle = {
        **bundle,
        "expires_at": "2020-01-01T00:00:00+00:00",
    }
    with pytest.raises(ValueError, match="expired"):
        loom.import_peer_bundle(db_session, expired_bundle)


def test_recover_interrupted_jobs_resumes_from_part_file(db_session, tmp_path):
    target = tmp_path / "movies" / "Test.Movie.mp4"
    target.parent.mkdir(parents=True)
    part = target.with_suffix(".mp4.part")
    part.write_bytes(b"0123456789")

    peer = LoomPeer(name="Bob", node_id="b1", base_url="http://b1.onion", shared_secret="s1", status="approved")
    db_session.add(peer)
    db_session.commit()
    db_session.refresh(peer)

    job = loom.LoomTransferJob(
        peer_id=peer.id,
        media_type="movie",
        remote_item_id=5,
        title="Test Movie",
        target_path=str(target),
        bytes_transferred=0,
        total_bytes=100,
        status="downloading",
    )
    db_session.add(job)
    db_session.commit()

    mgr = loom.LoomSyncManager()
    recovered = mgr.recover_interrupted_jobs(db_session)
    assert recovered == 1
    db_session.refresh(job)
    assert job.status == "queued"
    assert job.bytes_transferred == 10
    assert part.exists()


def test_upload_tracker_progress_and_sender_cancel():
    tracker = loom.LoomUploadTracker()
    uid, cancel_ev = tracker.register_upload(
        peer_id=7,
        peer_name="NAS Friend",
        media_type="movie",
        item_id=42,
        title="Madden",
        quality_label="1080p",
        total_bytes=1000,
        start_offset=100,
        hops=1,
        remote_job_id="nodeA:99",
    )
    assert not cancel_ev.is_set()
    tracker.update_progress(uid, 500, 200_000.0)
    active = tracker.list_active_uploads()
    assert len(active) == 1
    assert active[0]["status"] == "uploading"
    assert active[0]["peer_name"] == "NAS Friend"
    assert active[0]["progress_pct"] == 50.0
    assert active[0]["speed_bps"] == 200_000.0

    # Sender cancels upload
    assert tracker.cancel_upload(uid) is True
    assert cancel_ev.is_set()
    assert tracker.list_active_uploads() == []

    # Same remote_job_id is recognized as canceled by sender; a fresh job_id is allowed
    assert tracker.is_transfer_cancelled_by_sender(7, "nodeA:99", "movie", 42) is True
    assert tracker.is_transfer_cancelled_by_sender(7, "nodeA:100", "movie", 42) is False
    tracker.unregister_upload(uid)


def test_max_file_size_gb_filtering(db_session, tmp_path):
    assert loom.get_loom_settings(db_session)["max_file_size_gb"] == 0.0
    assert loom.is_file_size_allowed(5 * (1024 ** 3), 0) is True
    assert loom.is_file_size_allowed(5 * (1024 ** 3), 4.5) is False
    assert loom.is_file_size_allowed(4 * (1024 ** 3), 4.5) is True

    vol = tmp_path / "movies"
    vol.mkdir()
    small_file = vol / "Small.Movie.1080p.mp4"
    large_file = vol / "Large.Movie.1080p.mp4"
    small_file.write_bytes(b"a")
    large_file.write_bytes(b"b")

    manifest_path = tmp_path / "loom_manifest.json"
    manifest_data = {
        "version": 3,
        "movies": [
            {
                "id": 1,
                "media_type": "movie",
                "title": "Small Movie",
                "filename": small_file.name,
                "file_size": 4 * (1024 ** 3),
                "quality_label": "1080p",
                "_file_path": str(small_file),
            },
            {
                "id": 2,
                "media_type": "movie",
                "title": "Large Movie",
                "filename": large_file.name,
                "file_size": 5 * (1024 ** 3),
                "quality_label": "1080p",
                "_file_path": str(large_file),
            },
        ],
        "tv": [],
        "photos": [],
        "music": [],
    }
    manifest_path.write_text(__import__("json").dumps(manifest_data), encoding="utf-8")

    loom.save_loom_settings(db_session, {"searchable_volumes": [str(vol)], "max_file_size_gb": 4.5})
    with __import__("unittest.mock").mock.patch.object(loom, "get_loom_manifest_path", return_value=str(manifest_path)):
        res_small = loom.search_loom_manifest(db_session, "Small Movie")
        res_large = loom.search_loom_manifest(db_session, "Large Movie")
        assert len(res_small["movies"]) == 1
        assert res_large["movies"] == []
