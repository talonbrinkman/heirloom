import os
import sys

import pytest
from fastapi.testclient import TestClient
from sqlmodel import SQLModel, Session, create_engine, select
from starlette.requests import Request

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from app import database, main
from app.models import Movie, Profile, SystemConfig, TVShow, User, UserSession, Watchlist


def _make_request(headers=None, cookies=None, query_string=""):
    raw_headers = [(k.lower().encode("latin-1"), v.encode("latin-1")) for k, v in (headers or {}).items()]
    if cookies:
        cookie_str = "; ".join(f"{k}={v}" for k, v in cookies.items())
        raw_headers.append((b"cookie", cookie_str.encode("latin-1")))
    scope = {
        "type": "http",
        "method": "GET",
        "path": "/",
        "query_string": query_string.encode("latin-1"),
        "headers": raw_headers,
    }
    return Request(scope)


@pytest.fixture()
def isolated_client(tmp_path, monkeypatch):
    db_path = tmp_path / "test_heirloom.db"
    test_engine = create_engine(
        f"sqlite:///{db_path}",
        echo=False,
        connect_args={"check_same_thread": False},
    )
    SQLModel.metadata.create_all(test_engine)
    monkeypatch.setattr(main, "engine", test_engine)
    monkeypatch.setattr(database, "engine", test_engine)

    def override_get_session():
        with Session(test_engine) as session:
            yield session

    main.app.dependency_overrides[main.get_session] = override_get_session

    with Session(test_engine) as session:
        admin = User(
            username="admin",
            password_hash="not-used",
            is_admin=True,
            session_token="admin-tok-123",
            movies_access=True,
            tv_access=True,
            downloads_access=True,
            rewind_seconds=10,
            default_audio_lang="eng",
            default_subtitle_lang="eng",
            auto_pause_episodes=3,
        )
        member = User(
            username="member",
            password_hash="not-used",
            is_admin=False,
            session_token="member-tok-456",
            movies_access=True,
            tv_access=True,
            downloads_access=False,
        )
        session.add(admin)
        session.add(member)
        session.commit()
        session.refresh(admin)
        session.refresh(member)

        admin_profile = Profile(user_id=admin.id, name="Admin", is_primary=True)
        member_profile = Profile(user_id=member.id, name="Member", is_primary=True)
        session.add(admin_profile)
        session.add(member_profile)
        session.add(UserSession(user_id=admin.id, session_token="admin-tok-123"))
        session.add(UserSession(user_id=member.id, session_token="member-tok-456"))

        movie_file = tmp_path / "Inception (2010).mkv"
        movie_file.write_bytes(b"fake-video")
        movie = Movie(
            title="Inception",
            file_path=str(movie_file),
            year="2010",
            tmdb_id=27205,
            runtime=148,
            content_rating="PG-13",
            poster_filename="inception.jpg",
        )
        tv_ep = TVShow(
            title="Severance",
            file_path=str(tmp_path / "Severance S01E01.mkv"),
            year="2022",
            season=1,
            episode=1,
            episode_title="Good News About Hell",
        )
        session.add(movie)
        session.add(tv_ep)
        session.commit()
        session.refresh(movie)
        session.refresh(tv_ep)
        ids = {"movie_id": movie.id, "tv_id": tv_ep.id, "movie_path": str(movie_file)}

    try:
        client = TestClient(main.app)
        yield client, test_engine, ids
    finally:
        main.app.dependency_overrides.pop(main.get_session, None)
        test_engine.dispose()


def test_extract_session_token_and_wants_json_response():
    assert main.extract_session_token(_make_request(cookies={"session_token": "cookie-tok"})) == "cookie-tok"
    assert main.extract_session_token(_make_request(headers={"Authorization": "Bearer bearer-tok"})) == "bearer-tok"
    assert main.extract_session_token(_make_request(headers={"X-Auth-Token": "header-tok"})) == "header-tok"
    assert main.extract_session_token(_make_request(query_string="token=query-tok")) == "query-tok"
    # Cookie takes precedence if explicitly sent alongside header
    assert main.extract_session_token(_make_request(
        headers={"Authorization": "Bearer bearer-tok"},
        cookies={"session_token": "cookie-tok"},
    )) == "cookie-tok"

    assert main.wants_json_response(_make_request(headers={"Accept": "application/json"})) is True
    assert main.wants_json_response(_make_request(headers={"X-Requested-With": "XMLHttpRequest"})) is True
    assert main.wants_json_response(_make_request(headers={"Authorization": "Bearer tok"})) is True
    assert main.wants_json_response(_make_request(headers={"X-Auth-Token": "tok"})) is True
    assert main.wants_json_response(_make_request(headers={"Accept": "text/html"})) is False
    assert main.wants_json_response(_make_request(headers={"Authorization": "Bearer tok", "Accept": "text/html"})) is False


def test_token_auth_json_parity_and_logout(isolated_client, monkeypatch):
    client, test_engine, ids = isolated_client
    monkeypatch.setattr(main.scanner_debouncer, "trigger_scan", lambda delay=0.5: None)
    monkeypatch.setattr(main.scanner_manager, "start_scan", lambda: None)

    bearer = {"Authorization": "Bearer admin-tok-123"}

    # Form POST settings endpoints return JSON for Bearer clients (no 303 redirect)
    r_rewind = client.post("/settings/rewind", data={"rewind_seconds": "15"}, headers=bearer, follow_redirects=False)
    assert r_rewind.status_code == 200
    assert r_rewind.json() == {"status": "ok", "rewind_seconds": 15}

    r_tmdb = client.post("/settings/tmdb", data={"tmdb_key": "abc123"}, headers=bearer, follow_redirects=False)
    assert r_tmdb.status_code == 200
    assert r_tmdb.json() == {"status": "ok", "configured": True}

    r_aud = client.post("/settings/theaudiodb", data={"theaudiodb_key": ""}, headers=bearer, follow_redirects=False)
    assert r_aud.status_code == 200
    assert r_aud.json() == {"status": "ok", "configured": False}

    r_os = client.post("/settings/opensubtitles", data={"opensubtitles_key": "os-key"}, headers=bearer, follow_redirects=False)
    assert r_os.status_code == 200
    assert r_os.json() == {"status": "ok", "configured": True}

    r_rem = client.post("/settings/remote_access", data={"remote_url": "https://tv.example.com"}, headers=bearer, follow_redirects=False)
    assert r_rem.status_code == 200
    assert r_rem.json() == {"status": "ok", "remote_access_url": "https://tv.example.com"}

    r_rescan = client.post("/settings/rescan", headers=bearer, follow_redirects=False)
    assert r_rescan.status_code == 200
    assert r_rescan.json() == {"status": "ok", "scanning": True}

    # Browser HTML cookie submission still redirects cleanly
    client.cookies.set("session_token", "admin-tok-123")
    r_html = client.post(
        "/settings/rewind",
        data={"rewind_seconds": "20"},
        headers={"Accept": "text/html"},
        follow_redirects=False,
    )
    client.cookies.clear()
    assert r_html.status_code == 303

    # X-Auth-Token works for watchlist + watch history parity
    x_auth = {"X-Auth-Token": "admin-tok-123"}
    r_wl = client.post(f"/watchlist/add/movie/{ids['movie_id']}", headers=x_auth, follow_redirects=False)
    assert r_wl.status_code == 200
    assert r_wl.json()["in_watchlist"] is True

    r_toggle = client.post(
        "/watchlist/toggle",
        json={"media_type": "movie", "item_id": ids["movie_id"]},
        headers=x_auth,
        follow_redirects=False,
    )
    assert r_toggle.status_code == 200
    assert r_toggle.json() == {"status": "success", "in_watchlist": False}

    r_w = client.post(f"/history/mark_watched/movie/{ids['movie_id']}", headers=x_auth, follow_redirects=False)
    assert r_w.status_code == 200
    assert r_w.json() == {"status": "success", "watched": True}

    r_u = client.post(f"/history/mark_unwatched/movie/{ids['movie_id']}", headers=x_auth, follow_redirects=False)
    assert r_u.status_code == 200
    assert r_u.json() == {"status": "success", "watched": False}

    # Unauthenticated JSON request gets 401 JSON instead of /login redirect
    r_unauth = client.post(
        f"/history/mark_unwatched/movie/{ids['movie_id']}",
        headers={"Accept": "application/json"},
        follow_redirects=False,
    )
    assert r_unauth.status_code == 401
    assert r_unauth.json()["status"] == "unauthorized"

    # Bearer logout revokes token in UserSession + User and returns JSON
    r_out = client.post("/logout", headers=bearer, follow_redirects=False)
    assert r_out.status_code == 200
    assert r_out.json() == {"status": "ok", "logged_out": True}

    with Session(test_engine) as session:
        assert session.exec(select(UserSession).where(UserSession.session_token == "admin-tok-123")).first() is None
    r_after = client.post("/settings/rewind", data={"rewind_seconds": "5"}, headers=bearer, follow_redirects=False)
    assert r_after.status_code == 401


def test_playback_info_standardized_capabilities_and_subtitles(isolated_client, monkeypatch):
    client, _engine, ids = isolated_client

    fake_probe = {
        "duration": 8880.0,
        "video_codec": "h264",
        "audio_codec": "aac",
        "audio_channels": 2,
        "width": 1920,
        "height": 1080,
        "bitrate": 5200000,
        "is_hdr": False,
        "hdr_format": None,
        "can_direct_stream_video": True,
        "can_direct_stream_audio": True,
        "audio_tracks": [{"id": 0, "language": "eng", "codec": "aac", "channels": 2}],
        "subtitle_tracks": [
            {
                "id": 2,
                "language": "eng",
                "title": "English (SDH)",
                "label": "English (SDH)",
                "codec_name": "subrip",
                "is_text": True,
                "is_bitmap": False,
                "is_default": True,
                "is_forced": False,
                "is_hearing_impaired": True,
            },
            {
                "id": 3,
                "language": "spa",
                "title": "Spanish PGS",
                "label": "Spanish PGS",
                "codec_name": "hdmv_pgs_subtitle",
                "is_text": False,
                "is_bitmap": True,
                "is_default": False,
                "is_forced": False,
                "is_hearing_impaired": False,
            },
        ],
    }
    monkeypatch.setattr(main, "probe_media_streams", lambda _path: fake_probe)
    monkeypatch.setattr(
        main,
        "get_sidecar_subtitles",
        lambda _path: [{
            "id": "sidecar:Inception (2010).en.vtt",
            "language": "eng",
            "title": "Sidecar English",
            "label": "English (External VTT)",
            "codec_name": "vtt",
            "is_text": True,
            "is_bitmap": False,
            "is_default": False,
            "is_forced": False,
            "is_hearing_impaired": False,
        }],
    )
    monkeypatch.setattr(main, "list_fetched_subtitles", lambda _mt, _id: [])
    monkeypatch.setattr(main, "is_trickplay_ready", lambda _mt, _id: True)

    res = client.get(
        f"/api/playback/info/movie/{ids['movie_id']}",
        headers={"Authorization": "Bearer admin-tok-123"},
    )
    assert res.status_code == 200
    payload = res.json()

    assert payload["hls_master_url"] == f"/hls/movie/{ids['movie_id']}/master.m3u8"
    assert payload["stream_url"] == f"/stream/movie/{ids['movie_id']}"
    assert payload["trickplay"] == {
        "ready": True,
        "manifest_url": f"/trickplay/movie/{ids['movie_id']}/manifest.json",
    }
    assert payload["preferences"] == {
        "rewind_seconds": 10,
        "default_audio_lang": "eng",
        "default_subtitle_lang": "eng",
        "auto_pause_episodes": 3,
    }
    caps = payload["capabilities"]
    assert caps["video_codec"] == "h264"
    assert caps["audio_codec"] == "aac"
    assert caps["direct_play_supported"] is True
    assert caps["server_hdr_tonemapping"] is True
    assert caps["available_resolutions"] == ["original", "1080", "720", "480"]
    assert caps["downloads_allowed"] is True

    subs = payload["subtitle_tracks"]
    assert len(subs) == 3
    assert subs[0]["is_hearing_impaired"] is True
    assert subs[0]["is_text"] is True
    assert subs[0]["is_bitmap"] is False
    assert subs[1]["is_bitmap"] is True
    assert subs[1]["format"] == "hdmv_pgs_subtitle"
    assert "track_id=sidecar%3AInception%20%282010%29.en.vtt" in subs[2]["url"]


def test_refresh_metadata_endpoint_and_template_buttons(isolated_client, monkeypatch):
    client, test_engine, ids = isolated_client

    # Non-admin user is rejected
    r_forbidden = client.post(
        "/api/media/refresh_metadata",
        json={"media_type": "movie", "item_id": ids["movie_id"]},
        headers={"Authorization": "Bearer member-tok-456"},
    )
    assert r_forbidden.status_code == 403

    # Without TMDB API key configured
    monkeypatch.delenv("TMDB_API_KEY", raising=False)
    r_nokey = client.post(
        "/api/media/refresh_metadata",
        json={"media_type": "movie", "item_id": ids["movie_id"]},
        headers={"Authorization": "Bearer admin-tok-123"},
    )
    assert r_nokey.status_code == 400

    with Session(test_engine) as session:
        session.add(SystemConfig(key="tmdb_api_key", value="fake-tmdb-key"))
        session.commit()

    class FakeResp:
        def __init__(self, status_code, payload):
            self.status_code = status_code
            self._payload = payload

        def json(self):
            return self._payload

    class FakeSafeClient:
        def __init__(self, _raw):
            pass

        async def get(self, url, params=None):
            if url.endswith("/movie/27205"):
                return FakeResp(200, {
                    "title": "Inception",
                    "release_date": "2010-07-16",
                    "overview": "Refreshed dream heist plot.",
                    "vote_average": 8.8,
                    "poster_path": "/p.jpg",
                    "backdrop_path": "/b.jpg",
                })
            if url.endswith("/search/tv"):
                return FakeResp(200, {"results": [{"id": 95396, "name": "Severance"}]})
            if url.endswith("/tv/95396"):
                return FakeResp(200, {
                    "name": "Severance",
                    "first_air_date": "2022-02-18",
                    "overview": "Refreshed Lumon plot.",
                    "vote_average": 8.7,
                    "poster_path": "/tv_p.jpg",
                    "backdrop_path": "/tv_b.jpg",
                })
            if "/tv/95396/season/1" in url:
                return FakeResp(200, {
                    "poster_path": "/s1.jpg",
                    "episodes": [{"episode_number": 1, "name": "Good News About Hell", "overview": "Refreshed ep 1 overview."}],
                })
            return FakeResp(404, {})

    async def fake_download_img(*_args, **_kwargs):
        return "downloaded.jpg"

    async def fake_additional(*_args, **_kwargs):
        return ("Actor A", "Director B", "PG-13", "Sci-Fi", "trailer123", 0, None, None, None, "logo.png")

    monkeypatch.setattr(main, "SafeHTTPClient", FakeSafeClient)
    monkeypatch.setattr(main, "async_download_image", fake_download_img)
    monkeypatch.setattr(main, "async_fetch_additional_details", fake_additional)
    monkeypatch.setattr(main, "invalidate_movie_cache", lambda _mid: None)
    monkeypatch.setattr(main, "invalidate_series_cache", lambda: None)

    r_movie = client.post(
        "/api/media/refresh_metadata",
        json={"media_type": "movie", "item_id": ids["movie_id"]},
        headers={"Authorization": "Bearer admin-tok-123"},
    )
    assert r_movie.status_code == 200
    assert r_movie.json() == {"success": True, "title": "Inception", "tmdb_id": 27205}

    r_tv = client.post(
        "/api/media/refresh_metadata",
        json={"media_type": "tv", "item_id": ids["tv_id"]},
        headers={"Authorization": "Bearer admin-tok-123"},
    )
    assert r_tv.status_code == 200
    assert r_tv.json() == {"success": True, "title": "Severance", "tmdb_id": 95396}

    with Session(test_engine) as session:
        m = session.get(Movie, ids["movie_id"])
        t = session.get(TVShow, ids["tv_id"])
        assert m.plot == "Refreshed dream heist plot."
        assert m.rating == 8.8
        assert t.plot == "Refreshed Lumon plot."
        assert t.episode_plot == "Refreshed ep 1 overview."

    root = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
    for tmpl in ("dashboard.html", "library.html", "movie.html", "series.html", "person.html"):
        with open(os.path.join(root, "app", "templates", tmpl), "r", encoding="utf-8") as fp:
            src = fp.read()
        assert "triggerRefreshMetadata" in src, f"{tmpl} missing triggerRefreshMetadata"
        if tmpl in ("movie.html", "series.html"):
            assert "refresh-meta-btn" in src, f"{tmpl} missing hero .refresh-meta-btn"
