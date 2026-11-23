import os
import subprocess
import sys

import jinja2
import pytest

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
TEMPLATES_DIR = os.path.join(ROOT, "app", "templates")
STATIC_DIR = os.path.join(ROOT, "app", "static")

PJAX_TEMPLATES = [
    "album.html",
    "artist.html",
    "dashboard.html",
    "library.html",
    "movie.html",
    "person.html",
    "playlist.html",
    "server_dashboard.html",
    "series.html",
    "settings.html",
]


def test_all_jinja_templates_parse():
    env = jinja2.Environment(loader=jinja2.FileSystemLoader(TEMPLATES_DIR))
    files = sorted(f for f in os.listdir(TEMPLATES_DIR) if f.endswith(".html"))
    assert len(files) >= 19
    for fname in files:
        env.get_template(fname)


def test_base_shell_mounts_persistent_audio_outside_pjax_root():
    with open(os.path.join(TEMPLATES_DIR, "base.html"), "r", encoding="utf-8") as fp:
        base = fp.read()

    root_idx = base.index('id="pjax-root"')
    audio_idx = base.index('src="/static/audio-player.js"')
    shell_idx = base.index('src="/static/app-shell.js"')
    pjax_idx = base.index('src="/static/pjax.js"')

    assert pjax_idx < root_idx
    assert shell_idx > root_idx
    assert audio_idx > root_idx


@pytest.mark.parametrize("template_name", PJAX_TEMPLATES)
def test_pjax_templates_extend_base_without_duplicate_shell_code(template_name):
    with open(os.path.join(TEMPLATES_DIR, template_name), "r", encoding="utf-8") as fp:
        src = fp.read()

    assert src.lstrip().startswith('{% extends "base.html" %}')
    for forbidden in (
        "audio-player.js",
        "navigation-loader.js",
        "watch-toggle.js",
        "fix-match.js",
        "lucide.min.js",
        'id="wtModal"',
        "DOMContentLoaded",
        "function loadInbox(",
        "function toggleMenu(",
        "function toggleProfileDropdown(",
    ):
        assert forbidden not in src, f"{template_name} should not contain {forbidden}"


def test_photo_viewer_has_no_dead_inbox_code():
    with open(os.path.join(TEMPLATES_DIR, "photo.html"), "r", encoding="utf-8") as fp:
        src = fp.read()

    assert "loadInbox" not in src
    assert "toggleInbox" not in src
    assert "navPhoto" in src


def test_static_js_files_pass_node_syntax_check():
    js_files = sorted(f for f in os.listdir(STATIC_DIR) if f.endswith(".js"))
    for fname in js_files:
        path = os.path.join(STATIC_DIR, fname)
        res = subprocess.run(["node", "--check", path], capture_output=True, text=True)
        assert res.returncode == 0, f"{fname} syntax error: {res.stderr}"
