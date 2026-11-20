import hashlib
import hmac
import json
import os
import re
import shutil
import socket
import subprocess
import tempfile
import threading
import time
import uuid
from datetime import datetime, timezone
from typing import Any, Callable, Optional
from urllib.parse import urlparse

from sqlmodel import Session, select

from .database import engine, sqlite_file_name
from .ffmpeg_setup import get_cached_media_probe, probe_media_streams
from .models import LoomPeer, LoomTransferJob, Movie, Photo, SystemConfig, TVShow, Track

try:
    import httpx
except ImportError:
    httpx = None


DEFAULT_LOOM_ALLOWED_FILETYPES = (
    ".mp4, .mkv, .webm, .mov, .m4v, .avi, "
    ".mp3, .flac, .m4a, .aac, .ogg, .wav, .opus, "
    ".jpg, .jpeg, .png, .webp"
)

LOOM_DEFAULTS: dict[str, str] = {
    "loom_enabled": "false",
    "loom_allow_relay": "false",
    "loom_sync_dir": "/media/loom",
    "loom_upload_bandwidth_limit_kbps": "0",
    "loom_download_bandwidth_limit_kbps": "0",
    "loom_pause_on_active_streams": "true",
    "loom_max_quality": "1080p",
    "loom_max_search_hops": "1",
    "loom_max_sending_hops": "1",
    "loom_max_concurrent_transfers": "2",
    "loom_max_requests_per_minute": "3",
    "loom_max_file_size_gb": "0",
    "loom_allowed_filetypes": DEFAULT_LOOM_ALLOWED_FILETYPES,
    "loom_searchable_volumes": "[]",
}

QUALITY_HEIGHT_MAP: dict[str, int] = {
    "480p": 480,
    "720p": 720,
    "1080p": 1080,
    "hd": 1080,
    "1440p": 1440,
    "4k": 2160,
    "2160p": 2160,
    "original": 99999,
}


def _get_config_value(session: Session, key: str, default: str = "") -> str:
    row = session.exec(select(SystemConfig).where(SystemConfig.key == key)).first()
    return row.value if row and row.value is not None else default


def _set_config_value(session: Session, key: str, value: str):
    row = session.exec(select(SystemConfig).where(SystemConfig.key == key)).first()
    if row:
        row.value = value
    else:
        row = SystemConfig(key=key, value=value)
    session.add(row)


def get_or_create_node_id(session: Session) -> str:
    node_id = _get_config_value(session, "loom_node_id", "").strip()
    if not node_id:
        node_id = str(uuid.uuid4())
        _set_config_value(session, "loom_node_id", node_id)
        session.commit()
    return node_id


def get_tor_socks_url() -> str:
    return (os.environ.get("LOOM_TOR_SOCKS_URL") or "socks5h://127.0.0.1:9050").strip()


def is_onion_url(url: str) -> bool:
    try:
        host = (urlparse(url).hostname or "").lower()
        return host.endswith(".onion")
    except Exception:
        return False


def get_httpx_client_kwargs(target_url: str, timeout: Any = 15.0) -> dict[str, Any]:
    kwargs: dict[str, Any] = {"timeout": timeout}
    if is_onion_url(target_url):
        kwargs["proxy"] = get_tor_socks_url()
    return kwargs


class TorOnionManager:
    """Manages a built-in Tor daemon and per-peer Tor v3 onion services via ControlPort (ADD_ONION / DEL_ONION)."""

    def __init__(self):
        self.control_host = (os.environ.get("LOOM_TOR_CONTROL_HOST") or "127.0.0.1").strip()
        self.control_port = int(os.environ.get("LOOM_TOR_CONTROL_PORT") or "9051")
        self.control_password = os.environ.get("LOOM_TOR_CONTROL_PASSWORD") or ""
        self.cookie_path = os.environ.get("LOOM_TOR_CONTROL_COOKIE_PATH") or ""
        self.app_host = (os.environ.get("LOOM_APP_HOST") or "127.0.0.1").strip()
        self.app_port = int(os.environ.get("LOOM_APP_PORT") or os.environ.get("PORT") or "42069")
        socks_parsed = urlparse(get_tor_socks_url())
        self.socks_host = socks_parsed.hostname or "127.0.0.1"
        self.socks_port = socks_parsed.port or 9050
        self._tor_proc: Optional[subprocess.Popen] = None
        self._tor_log_fp: Optional[Any] = None
        self._tor_lock = threading.Lock()

    def _get_tor_data_dir(self) -> str:
        env_dir = (os.environ.get("LOOM_TOR_DATA_DIR") or "").strip()
        if env_dir:
            data_dir = env_dir
        elif os.name == "posix":
            # Use native container filesystem so strict 0700 permissions always succeed
            # even when /config is a Windows/SMB bind mount. Per-peer keys persist in SQLite.
            data_dir = "/var/lib/heirloom-tor"
            try:
                os.makedirs(data_dir, mode=0o700, exist_ok=True)
                os.chmod(data_dir, 0o700)
                return data_dir
            except OSError:
                data_dir = os.path.join(tempfile.gettempdir(), "heirloom-tor")
        else:
            data_dir = os.path.join(tempfile.gettempdir(), "heirloom-tor")

        os.makedirs(data_dir, mode=0o700, exist_ok=True)
        if os.name == "posix":
            try:
                os.chmod(data_dir, 0o700)
            except OSError:
                pass
        return data_dir

    def ensure_tor_running(self, timeout: float = 20.0) -> bool:
        """Start the bundled local Tor daemon if ControlPort is not already reachable."""
        if self.is_control_available():
            return True
        if self.control_host not in ("127.0.0.1", "localhost", "::1"):
            return False

        log_path = ""
        with self._tor_lock:
            if self.is_control_available():
                return True
            if self._tor_proc is None or self._tor_proc.poll() is not None:
                tor_bin = (os.environ.get("LOOM_TOR_BIN") or "").strip() or shutil.which("tor")
                if not tor_bin:
                    print("[Loom] Tor binary not found on PATH; cannot auto-start Tor.")
                    return False

                data_dir = self._get_tor_data_dir()
                log_path = os.path.join(data_dir, "tor.log")
                cmd = [
                    tor_bin,
                    "--SocksPort", f"{self.socks_host}:{self.socks_port}",
                    "--ControlPort", f"{self.control_host}:{self.control_port}",
                    "--CookieAuthentication", "0",
                    "--DataDirectory", data_dir,
                    "--Log", "notice stdout",
                ]
                try:
                    if self._tor_log_fp is not None:
                        try:
                            self._tor_log_fp.close()
                        except Exception:
                            pass
                    self._tor_log_fp = open(log_path, "ab")
                    self._tor_proc = subprocess.Popen(
                        cmd,
                        stdout=self._tor_log_fp,
                        stderr=subprocess.STDOUT,
                    )
                    print(f"[Loom] Started built-in Tor daemon (pid={self._tor_proc.pid})")
                except Exception as e:
                    print(f"[Loom] Failed to start built-in Tor daemon: {e}")
                    self._tor_proc = None
                    return False

        deadline = time.time() + max(0.5, timeout)
        while time.time() < deadline:
            if self.is_control_available():
                return True
            proc = self._tor_proc
            if proc is not None and proc.poll() is not None:
                tail = ""
                if log_path and os.path.exists(log_path):
                    try:
                        with open(log_path, "r", encoding="utf-8", errors="replace") as fp:
                            tail = " | ".join(line.strip() for line in fp.readlines()[-5:] if line.strip())
                    except Exception:
                        pass
                print(f"[Loom] Built-in Tor exited early (code={proc.returncode}): {tail}")
                return False
            time.sleep(0.25)

        return self.is_control_available()

    def stop_tor(self) -> None:
        """Cleanly stop the managed Tor daemon on shutdown."""
        with self._tor_lock:
            proc = self._tor_proc
            self._tor_proc = None
            if proc is not None and proc.poll() is None:
                try:
                    proc.terminate()
                    proc.wait(timeout=5.0)
                except Exception:
                    try:
                        proc.kill()
                    except Exception:
                        pass
            if self._tor_log_fp is not None:
                try:
                    self._tor_log_fp.close()
                except Exception:
                    pass
                self._tor_log_fp = None

    def _authenticate(self, sock: socket.socket) -> None:
        if self.control_password:
            escaped = self.control_password.replace("\\", "\\\\").replace('"', '\\"')
            cmd = f'AUTHENTICATE "{escaped}"\r\n'
        elif self.cookie_path and os.path.exists(self.cookie_path):
            with open(self.cookie_path, "rb") as fp:
                cookie_hex = fp.read().hex()
            cmd = f"AUTHENTICATE {cookie_hex}\r\n"
        else:
            cmd = "AUTHENTICATE\r\n"
        sock.sendall(cmd.encode("utf-8"))
        resp = self._read_reply(sock)
        if not any(line.startswith("250") for line in resp):
            raise RuntimeError(f"Tor ControlPort authentication failed: {' | '.join(resp)}")

    @staticmethod
    def _read_reply(sock: socket.socket) -> list[str]:
        lines: list[str] = []
        buf = b""
        while True:
            chunk = sock.recv(4096)
            if not chunk:
                break
            buf += chunk
            while b"\r\n" in buf:
                raw_line, buf = buf.split(b"\r\n", 1)
                line = raw_line.decode("utf-8", errors="replace")
                lines.append(line)
                if len(line) >= 4 and line[:3].isdigit() and line[3] == " ":
                    return lines
        return lines

    def _send_command(self, command: str) -> list[str]:
        with socket.create_connection((self.control_host, self.control_port), timeout=5.0) as sock:
            sock.settimeout(5.0)
            self._authenticate(sock)
            sock.sendall((command.rstrip("\r\n") + "\r\n").encode("utf-8"))
            resp = self._read_reply(sock)
            if not any(line.startswith("250") for line in resp):
                raise RuntimeError(f"Tor ControlPort command failed ({command.split()[0]}): {' | '.join(resp)}")
            return resp

    def is_control_available(self) -> bool:
        try:
            with socket.create_connection((self.control_host, self.control_port), timeout=1.5):
                return True
        except OSError:
            return False

    def create_onion_service(self, private_key: Optional[str] = None) -> tuple[str, str]:
        """Create (or re-register) a detached Tor v3 onion service pointing to this Heirloom server."""
        key_spec = private_key.strip() if private_key else "NEW:ED25519-V3"
        cmd = f"ADD_ONION {key_spec} Flags=Detach Port=80,{self.app_host}:{self.app_port}"
        resp = self._send_command(cmd)
        service_id = ""
        returned_key = private_key or ""
        for line in resp:
            if line.startswith("250-ServiceID="):
                service_id = line.split("=", 1)[1].strip()
            elif line.startswith("250-PrivateKey="):
                returned_key = line.split("=", 1)[1].strip()
        if not service_id:
            raise RuntimeError("Tor ControlPort did not return a ServiceID.")
        return service_id, returned_key

    def delete_onion_service(self, service_id: str) -> bool:
        sid = (service_id or "").strip().removesuffix(".onion")
        if not sid:
            return False
        try:
            self._send_command(f"DEL_ONION {sid}")
            return True
        except Exception:
            return False

    def restore_peer_onions(self, session: Session) -> int:
        """Re-register active per-peer .onion services on startup."""
        if not self.is_control_available():
            return 0
        restored = 0
        restored_keys: set[str] = set()
        peers = session.exec(select(LoomPeer).where(LoomPeer.status.in_(["pending", "approved"]))).all()
        for p in peers:
            pk = (p.local_onion_private_key or "").strip()
            if pk and pk not in restored_keys:
                restored_keys.add(pk)
                try:
                    sid, _ = self.create_onion_service(private_key=pk)
                    if sid and sid != p.local_onion_service_id:
                        p.local_onion_service_id = sid
                        session.add(p)
                    restored += 1
                except Exception as e:
                    if "550" not in str(e):
                        print(f"[Loom] Failed to restore .onion for peer {p.id}: {e}")
        if restored:
            session.commit()

        # Prune expired bundles before restoring any pending exported-bundle .onion services
        prune_expired_issued_secrets(session)
        for entry in _get_issued_secrets(session):
            pk = (entry.get("local_onion_private_key") or "").strip()
            if pk and pk not in restored_keys:
                restored_keys.add(pk)
                try:
                    self.create_onion_service(private_key=pk)
                    restored += 1
                except Exception as e:
                    if "550" not in str(e):
                        print(f"[Loom] Failed to restore issued bundle .onion: {e}")
        return restored


tor_onion_manager = TorOnionManager()


def _parse_searchable_volumes(raw_val: str) -> list[str]:
    try:
        parsed = json.loads(raw_val or "[]")
        if isinstance(parsed, list):
            seen: set[str] = set()
            result: list[str] = []
            for item in parsed:
                p = str(item or "").strip()
                if p and p not in seen:
                    seen.add(p)
                    result.append(p)
            return result
    except Exception:
        pass
    return []


def is_path_in_volumes(file_path: str, volume_paths: list[str]) -> bool:
    """Return True if file_path resides inside any of the enabled searchable volume paths."""
    if not file_path or not volume_paths:
        return False
    norm_file = os.path.normcase(os.path.normpath(os.path.abspath(file_path)))
    for vp in volume_paths:
        if not vp:
            continue
        norm_vol = os.path.normcase(os.path.normpath(os.path.abspath(vp)))
        try:
            if os.path.commonpath([norm_file, norm_vol]) == norm_vol:
                return True
        except ValueError:
            continue
    return False


def _sanitize_peer_name(raw_name: Optional[str]) -> str:
    cleaned = str(raw_name or "").strip()
    if not cleaned:
        return "Anonymous"
    # Guard against accidentally pasting a Peer Bundle JSON into the nickname field
    if cleaned.startswith("{") or cleaned.startswith("["):
        return "Anonymous"
    return cleaned[:80]


def parse_allowed_filetypes(raw: Any) -> list[str]:
    """Parse a comma/space-separated string (or list) into safe normalized extensions like ['.mp4', '.mkv']."""
    if isinstance(raw, list):
        tokens = [str(x) for x in raw]
    else:
        tokens = re.split(r"[\s,;]+", str(raw or ""))
    seen: set[str] = set()
    cleaned: list[str] = []
    for tok in tokens:
        t = tok.strip().lower()
        if not t:
            continue
        if not t.startswith("."):
            t = f".{t}"
        if not re.match(r"^\.[a-z0-9]{1,10}$", t):
            continue
        if t not in seen:
            seen.add(t)
            cleaned.append(t)
    return cleaned


def is_filetype_allowed(filename_or_path: Optional[str], allowed_filetypes: list[str]) -> bool:
    """Return True only if filename_or_path has an extension present in allowed_filetypes."""
    if not filename_or_path or not allowed_filetypes:
        return False
    ext = os.path.splitext(str(filename_or_path).strip())[1].lower()
    if not ext:
        return False
    return ext in set(allowed_filetypes)


def is_file_size_allowed(file_size: Any, max_file_size_gb: float) -> bool:
    """Return True if max_file_size_gb <= 0 or file_size is within the configured GB limit."""
    limit_gb = float(max_file_size_gb or 0)
    if limit_gb <= 0:
        return True
    try:
        size_bytes = int(file_size or 0)
    except (TypeError, ValueError):
        return False
    return size_bytes <= int(limit_gb * (1024 ** 3))


def get_loom_settings(session: Session) -> dict[str, Any]:
    node_id = get_or_create_node_id(session)
    raw = {k: _get_config_value(session, k, v) for k, v in LOOM_DEFAULTS.items()}
    legacy_hops = _get_config_value(session, "loom_max_hops", "").strip()
    search_hops_raw = _get_config_value(session, "loom_max_search_hops", "").strip() or legacy_hops or "1"
    max_search_hops = max(1, min(10, int(float(search_hops_raw))))
    max_sending_hops = max(1, min(10, int(float(raw["loom_max_sending_hops"] or 1))))
    allowed_list = parse_allowed_filetypes(raw["loom_allowed_filetypes"])
    return {
        "node_id": node_id,
        "enabled": raw["loom_enabled"].strip().lower() == "true",
        "allow_relay": raw["loom_allow_relay"].strip().lower() == "true",
        "sync_dir": raw["loom_sync_dir"].strip() or "/media/loom",
        "upload_bandwidth_limit_kbps": max(0, int(float(raw["loom_upload_bandwidth_limit_kbps"] or 0))),
        "download_bandwidth_limit_kbps": max(0, int(float(raw["loom_download_bandwidth_limit_kbps"] or 0))),
        "pause_on_active_streams": raw["loom_pause_on_active_streams"].strip().lower() == "true",
        "max_quality": (raw["loom_max_quality"].strip().lower() or "1080p"),
        "max_hops": max_search_hops,
        "max_search_hops": max_search_hops,
        "max_sending_hops": max_sending_hops,
        "max_concurrent_transfers": max(1, min(20, int(float(raw["loom_max_concurrent_transfers"] or 2)))),
        "max_requests_per_minute": max(1, min(120, int(float(raw["loom_max_requests_per_minute"] or 3)))),
        "max_file_size_gb": max(0.0, round(float(raw["loom_max_file_size_gb"] or 0), 3)),
        "allowed_filetypes": ", ".join(allowed_list),
        "allowed_filetypes_list": allowed_list,
        "searchable_volumes": _parse_searchable_volumes(raw["loom_searchable_volumes"]),
        "tor_available": tor_onion_manager.is_control_available(),
    }


def save_loom_settings(session: Session, data: dict[str, Any]) -> dict[str, Any]:
    def _encode_searchable_volumes(v: Any) -> str:
        if not isinstance(v, list):
            return "[]"
        seen: set[str] = set()
        cleaned: list[str] = []
        for item in v:
            p = str(item or "").strip()
            if p and p not in seen:
                seen.add(p)
                cleaned.append(p)
        return json.dumps(cleaned)

    def _encode_allowed_filetypes(v: Any) -> str:
        return ", ".join(parse_allowed_filetypes(v))

    mapping = {
        "enabled": ("loom_enabled", lambda v: "true" if bool(v) else "false"),
        "allow_relay": ("loom_allow_relay", lambda v: "true" if bool(v) else "false"),
        "sync_dir": ("loom_sync_dir", lambda v: str(v or "/media/loom").strip() or "/media/loom"),
        "upload_bandwidth_limit_kbps": ("loom_upload_bandwidth_limit_kbps", lambda v: str(max(0, int(float(v or 0))))),
        "download_bandwidth_limit_kbps": ("loom_download_bandwidth_limit_kbps", lambda v: str(max(0, int(float(v or 0))))),
        "pause_on_active_streams": ("loom_pause_on_active_streams", lambda v: "true" if bool(v) else "false"),
        "max_quality": (
            "loom_max_quality",
            lambda v: str(v or "1080p").strip().lower()
            if str(v or "").strip().lower() in ("720p", "1080p", "original")
            else "1080p",
        ),
        "max_hops": ("loom_max_search_hops", lambda v: str(max(1, min(10, int(float(v or 1)))))),
        "max_search_hops": ("loom_max_search_hops", lambda v: str(max(1, min(10, int(float(v or 1)))))),
        "max_sending_hops": ("loom_max_sending_hops", lambda v: str(max(1, min(10, int(float(v or 1)))))),
        "max_concurrent_transfers": ("loom_max_concurrent_transfers", lambda v: str(max(1, min(20, int(float(v or 2)))))),
        "max_requests_per_minute": ("loom_max_requests_per_minute", lambda v: str(max(1, min(120, int(float(v or 3)))))),
        "max_file_size_gb": ("loom_max_file_size_gb", lambda v: str(max(0.0, round(float(v or 0), 3)))),
        "allowed_filetypes": ("loom_allowed_filetypes", _encode_allowed_filetypes),
        "searchable_volumes": ("loom_searchable_volumes", _encode_searchable_volumes),
    }
    for field_name, (cfg_key, conv) in mapping.items():
        if field_name in data:
            _set_config_value(session, cfg_key, conv(data[field_name]))
    session.commit()
    return get_loom_settings(session)


def sign_loom_headers(node_id: str, secret: str, method: str, path: str, body: bytes = b"") -> dict[str, str]:
    ts = str(int(time.time()))
    body_hash = hashlib.sha256(body or b"").hexdigest()
    payload = f"{method.upper()}\n{path}\n{ts}\n{body_hash}".encode("utf-8")
    sig = hmac.new(secret.encode("utf-8"), payload, hashlib.sha256).hexdigest()
    return {
        "X-Loom-Node-Id": node_id,
        "X-Loom-Timestamp": ts,
        "X-Loom-Signature": sig,
    }


def _compute_signature(secret: str, method: str, path: str, ts: str, body: bytes = b"") -> str:
    body_hash = hashlib.sha256(body or b"").hexdigest()
    payload = f"{method.upper()}\n{path}\n{ts}\n{body_hash}".encode("utf-8")
    return hmac.new(secret.encode("utf-8"), payload, hashlib.sha256).hexdigest()


def verify_loom_request(
    session: Session,
    headers: dict[str, str],
    method: str,
    path: str,
    body: bytes = b"",
    max_skew_seconds: int = 300,
) -> Optional[LoomPeer]:
    """Verify HMAC-SHA256 peer headers and return the matching LoomPeer if valid, else None."""
    norm_headers = {k.lower(): v for k, v in headers.items()}
    peer_node_id = (norm_headers.get("x-loom-node-id") or "").strip()
    ts_str = (norm_headers.get("x-loom-timestamp") or "").strip()
    sig = (norm_headers.get("x-loom-signature") or "").strip()

    if not peer_node_id or not ts_str or not sig:
        return None
    try:
        ts_val = int(ts_str)
    except ValueError:
        return None
    if abs(int(time.time()) - ts_val) > max_skew_seconds:
        return None

    peer = session.exec(
        select(LoomPeer).where(
            (LoomPeer.node_id == peer_node_id) | (LoomPeer.local_node_id == peer_node_id)
        )
    ).first()
    if not peer or not peer.shared_secret:
        return None

    expected = _compute_signature(peer.shared_secret, method, path, ts_str, body)
    if not hmac.compare_digest(expected, sig):
        return None
    return peer


LOOM_BUNDLE_TTL_SECONDS = 24 * 60 * 60  # Exported peer bundles expire after 24 hours


def _get_issued_secrets(session: Session) -> list[dict[str, Any]]:
    raw = _get_config_value(session, "loom_issued_secrets", "[]")
    try:
        data = json.loads(raw)
        return data if isinstance(data, list) else []
    except Exception:
        return []


def _save_issued_secrets(session: Session, items: list[dict[str, Any]]):
    _set_config_value(session, "loom_issued_secrets", json.dumps(items[-50:]))


def _is_issued_entry_expired(entry: dict[str, Any], now_ts: Optional[int] = None) -> bool:
    now = int(time.time()) if now_ts is None else int(now_ts)
    exp = entry.get("expires_at")
    if exp is not None:
        try:
            return now >= int(exp)
        except (TypeError, ValueError):
            pass
    created = entry.get("created_at")
    if created is not None:
        try:
            return (now - int(created)) >= LOOM_BUNDLE_TTL_SECONDS
        except (TypeError, ValueError):
            pass
    return False


def prune_expired_issued_secrets(session: Session, now_ts: Optional[int] = None) -> int:
    """Remove expired unclaimed peer bundles and tear down their temporary .onion services."""
    issued = _get_issued_secrets(session)
    if not issued:
        return 0
    now = int(time.time()) if now_ts is None else int(now_ts)
    active_peer_sids = {
        p.local_onion_service_id
        for p in session.exec(select(LoomPeer)).all()
        if p.local_onion_service_id
    }
    kept: list[dict[str, Any]] = []
    removed = 0
    for entry in issued:
        if _is_issued_entry_expired(entry, now_ts=now):
            sid = (entry.get("local_onion_service_id") or "").strip()
            if sid and sid not in active_peer_sids:
                tor_onion_manager.delete_onion_service(sid)
            removed += 1
        else:
            kept.append(entry)
    if removed:
        _save_issued_secrets(session, kept)
        session.commit()
    return removed


def export_peer_bundle(session: Session, fallback_base_url: str = "") -> dict[str, Any]:
    """Generate a single-use, 24-hour expiring peer bundle with a dedicated per-peer .onion address."""
    prune_expired_issued_secrets(session)
    local_node_id = str(uuid.uuid4())
    secret = uuid.uuid4().hex + uuid.uuid4().hex

    service_id = ""
    private_key = ""
    base_url = ""
    if tor_onion_manager.is_control_available():
        service_id, private_key = tor_onion_manager.create_onion_service()
        base_url = f"http://{service_id}.onion"
    elif os.environ.get("LOOM_ALLOW_CLEARNET_FALLBACK") == "1" and fallback_base_url.strip():
        base_url = fallback_base_url.strip().rstrip("/")
    else:
        raise RuntimeError(
            "Tor ControlPort is not reachable. Start Tor with ControlPort enabled to mint a private per-peer .onion address."
        )

    now_ts = int(time.time())
    exp_ts = now_ts + LOOM_BUNDLE_TTL_SECONDS
    issued = _get_issued_secrets(session)
    issued.append({
        "secret": secret,
        "local_node_id": local_node_id,
        "local_onion_service_id": service_id,
        "local_onion_private_key": private_key,
        "created_at": now_ts,
        "expires_at": exp_ts,
    })
    _save_issued_secrets(session, issued)
    session.commit()

    return {
        "version": 2,
        "node_id": local_node_id,
        "base_url": base_url,
        "shared_secret": secret,
        "max_hops": get_loom_settings(session)["max_hops"],
        "issued_at": datetime.fromtimestamp(now_ts, tz=timezone.utc).isoformat(),
        "expires_at": datetime.fromtimestamp(exp_ts, tz=timezone.utc).isoformat(),
    }


def consume_issued_secret_for_handshake(
    session: Session,
    headers: dict[str, str],
    method: str,
    path: str,
    body: bytes,
    peer_name: str,
    peer_base_url: str,
) -> Optional[LoomPeer]:
    """Allow an unseen peer to register if it signs a handshake using an unexpired exported bundle secret."""
    prune_expired_issued_secrets(session)
    norm_headers = {k.lower(): v for k, v in headers.items()}
    peer_node_id = (norm_headers.get("x-loom-node-id") or "").strip()
    ts_str = (norm_headers.get("x-loom-timestamp") or "").strip()
    sig = (norm_headers.get("x-loom-signature") or "").strip()
    if not peer_node_id or not ts_str or not sig:
        return None
    try:
        if abs(int(time.time()) - int(ts_str)) > 300:
            return None
    except ValueError:
        return None

    issued = _get_issued_secrets(session)
    matched_idx = None
    matched_entry = None
    for idx, entry in enumerate(issued):
        sec = entry.get("secret", "")
        if sec and hmac.compare_digest(_compute_signature(sec, method, path, ts_str, body), sig):
            matched_idx = idx
            matched_entry = entry
            break

    if matched_entry is None:
        return None

    issued.pop(matched_idx)
    _save_issued_secrets(session, issued)

    # Prevent duplicate peer creation if this secret or node_id already belongs to an existing peer
    # (e.g. re-enabling a peer or self-pairing during local testing).
    existing = session.exec(
        select(LoomPeer).where(
            (LoomPeer.shared_secret == matched_entry["secret"])
            | (LoomPeer.node_id == peer_node_id)
            | (LoomPeer.local_node_id == peer_node_id)
        )
    ).first()
    if existing:
        existing.last_seen_at = datetime.now(timezone.utc)
        session.add(existing)
        session.commit()
        session.refresh(existing)
        return existing

    peer = LoomPeer(
        name=_sanitize_peer_name(peer_name),
        node_id=peer_node_id,
        base_url=(peer_base_url or "").strip().rstrip("/"),
        shared_secret=matched_entry["secret"],
        local_node_id=matched_entry.get("local_node_id") or str(uuid.uuid4()),
        local_onion_service_id=matched_entry.get("local_onion_service_id") or None,
        local_onion_private_key=matched_entry.get("local_onion_private_key") or None,
        status="approved",
        allow_incoming=False,
        allow_outgoing=False,
        max_hops=get_loom_settings(session)["max_hops"],
        usage_month=datetime.now(timezone.utc).strftime("%Y-%m"),
        last_seen_at=datetime.now(timezone.utc),
    )
    session.add(peer)
    session.commit()
    session.refresh(peer)
    return peer


def import_peer_bundle(session: Session, bundle: dict[str, Any], custom_name: Optional[str] = None) -> LoomPeer:
    """Import a peer bundle into 'pending' state with incoming/outgoing disabled until approved."""
    node_id = str(bundle.get("node_id") or "").strip()
    base_url = str(bundle.get("base_url") or "").strip().rstrip("/")
    shared_secret = str(bundle.get("shared_secret") or "").strip()
    name = _sanitize_peer_name(custom_name or bundle.get("name"))

    local_fallback_id = get_or_create_node_id(session)
    if not node_id or not base_url or not shared_secret:
        raise ValueError("Peer bundle must include node_id, base_url, and shared_secret.")
    raw_expires = bundle.get("expires_at")
    if raw_expires:
        try:
            exp_dt = datetime.fromisoformat(str(raw_expires).replace("Z", "+00:00"))
            if exp_dt.tzinfo is None:
                exp_dt = exp_dt.replace(tzinfo=timezone.utc)
            if datetime.now(timezone.utc) >= exp_dt:
                raise ValueError("This peer bundle has expired. Ask your friend to export a new one.")
        except ValueError as ve:
            if "expired" in str(ve).lower():
                raise
    if not (base_url.startswith("http://") or base_url.startswith("https://")):
        raise ValueError("Peer base_url must start with http:// or https://.")
    if base_url.startswith("http://") and not is_onion_url(base_url) and os.environ.get("LOOM_ALLOW_CLEARNET_FALLBACK") != "1":
        raise ValueError("Plaintext http:// is only allowed for .onion addresses.")
    if node_id == local_fallback_id:
        raise ValueError("Cannot import your own server bundle as a peer.")

    local_node_id = str(uuid.uuid4())
    local_sid = None
    local_priv = None
    # If importing our own exported bundle (e.g. local self-testing), reuse its persisted .onion key
    # so the peer's base_url stays alive across restarts.
    for issued_entry in _get_issued_secrets(session):
        if issued_entry.get("secret") == shared_secret and issued_entry.get("local_onion_private_key"):
            local_sid = issued_entry.get("local_onion_service_id") or None
            local_priv = issued_entry.get("local_onion_private_key") or None
            break
    if not local_sid and tor_onion_manager.is_control_available():
        local_sid, local_priv = tor_onion_manager.create_onion_service()

    existing = session.exec(select(LoomPeer).where(LoomPeer.node_id == node_id)).first()
    if existing:
        existing.name = name
        existing.base_url = base_url
        existing.shared_secret = shared_secret
        if not existing.local_node_id:
            existing.local_node_id = local_node_id
        if local_sid and not existing.local_onion_service_id:
            existing.local_onion_service_id = local_sid
            existing.local_onion_private_key = local_priv
        existing.status = "approved"
        existing.allow_incoming = False
        existing.allow_outgoing = False
        session.add(existing)
        session.commit()
        session.refresh(existing)
        return existing

    peer = LoomPeer(
        name=name,
        node_id=node_id,
        base_url=base_url,
        shared_secret=shared_secret,
        local_node_id=local_node_id,
        local_onion_service_id=local_sid,
        local_onion_private_key=local_priv,
        status="approved",
        allow_incoming=False,
        allow_outgoing=False,
        max_hops=get_loom_settings(session)["max_hops"],
        usage_month=datetime.now(timezone.utc).strftime("%Y-%m"),
    )
    session.add(peer)
    session.commit()
    session.refresh(peer)
    return peer


def get_peer_local_identity(session: Session, peer: LoomPeer) -> tuple[str, str]:
    """Return (local_node_id, local_base_url) for communicating with a specific peer."""
    local_node_id = peer.local_node_id or get_or_create_node_id(session)
    local_base_url = f"http://{peer.local_onion_service_id}.onion" if peer.local_onion_service_id else ""
    return local_node_id, local_base_url


def rotate_peer_onion(session: Session, peer: LoomPeer) -> str:
    """Mint a new per-peer .onion address, update DB, and delete the previous .onion service."""
    if not tor_onion_manager.is_control_available():
        raise RuntimeError("Tor ControlPort is not reachable to rotate .onion address.")
    old_sid = peer.local_onion_service_id
    new_sid, new_priv = tor_onion_manager.create_onion_service()
    peer.local_onion_service_id = new_sid
    peer.local_onion_private_key = new_priv
    session.add(peer)
    session.commit()
    session.refresh(peer)

    new_url = f"http://{new_sid}.onion"
    if httpx is not None and peer.base_url and peer.status == "approved":
        local_node_id, _ = get_peer_local_identity(session, peer)
        payload = json.dumps({"node_id": local_node_id, "base_url": new_url}).encode("utf-8")
        hdrs = sign_loom_headers(local_node_id, peer.shared_secret, "POST", "/api/loom/v1/handshake", payload)
        hdrs["Content-Type"] = "application/json"
        try:
            with httpx.Client(**get_httpx_client_kwargs(peer.base_url, timeout=15.0)) as client:
                client.post(f"{peer.base_url.rstrip('/')}/api/loom/v1/handshake", headers=hdrs, content=payload)
        except Exception as e:
            print(f"[Loom] Best-effort onion rotation notify failed for peer {peer.id}: {e}")

    if old_sid and old_sid != new_sid:
        tor_onion_manager.delete_onion_service(old_sid)
    return new_url


def get_loom_manifest_path() -> str:
    db_dir = os.path.dirname(os.path.abspath(sqlite_file_name))
    if os.path.isdir(db_dir) and os.access(db_dir, os.W_OK):
        return os.path.join(db_dir, "loom_manifest.json")
    fallback_dir = os.path.join(os.getcwd(), "config")
    os.makedirs(fallback_dir, exist_ok=True)
    return os.path.join(fallback_dir, "loom_manifest.json")


def _infer_resolution_from_filename(path: str) -> str:
    low = (path or "").lower()
    if "2160p" in low or "4k" in low:
        return "4K"
    if "1440p" in low:
        return "1440p"
    if "1080p" in low:
        return "1080p"
    if "720p" in low:
        return "720p"
    if "480p" in low:
        return "480p"
    return "HD"


def _get_resolution_label(probe: Optional[dict[str, Any]], file_path: str) -> str:
    if probe and isinstance(probe, dict):
        width = probe.get("width") or 0
        height = probe.get("height") or 0
        if isinstance(height, int) and height > 0:
            w = width if isinstance(width, int) else 0
            if w >= 3600 or height >= 2000:
                return "4K"
            if w >= 2400 or height >= 1350:
                return "1440p"
            if w >= 1800 or height >= 950:
                return "1080p"
            if w >= 1150 or height >= 650:
                return "720p"
            if height >= 450:
                return "480p"
            return f"{height}p"
    return _infer_resolution_from_filename(file_path)


def _probe_or_cached_resolution(file_path: str, file_size: int, prev_cache: dict[str, tuple[int, str]]) -> str:
    cached = prev_cache.get(file_path)
    if cached and cached[0] == file_size and cached[1] and cached[1] != "HD":
        return cached[1]
    probe = get_cached_media_probe(file_path)
    if probe is None and file_size >= 4096:
        try:
            probe = probe_media_streams(file_path)
        except Exception:
            probe = None
    return _get_resolution_label(probe, file_path)


def rebuild_loom_manifest(session: Session) -> dict[str, Any]:
    """Scan DB + media probes once and write a lightweight loom_manifest.json."""
    prev_cache: dict[str, tuple[int, str]] = {}
    manifest_path = get_loom_manifest_path()
    if os.path.exists(manifest_path):
        try:
            with open(manifest_path, "r", encoding="utf-8") as fp:
                old_data = json.load(fp)
            if isinstance(old_data, dict):
                for bucket in ("movies", "tv"):
                    for item in (old_data.get(bucket) or []):
                        fp_key = item.get("_file_path")
                        if fp_key and item.get("quality_label"):
                            prev_cache[fp_key] = (int(item.get("file_size") or 0), str(item.get("quality_label")))
        except Exception:
            pass

    movies_out: list[dict[str, Any]] = []
    seen_movie_keys: set[tuple[str, str]] = set()
    raw_movies = session.exec(select(Movie).order_by(Movie.title.asc())).all()
    for m in raw_movies:
        if not m.file_path or not os.path.exists(m.file_path):
            continue
        year_str = str(m.year or "").split(" • ")[0].strip()
        dedupe_key = ((m.title or "").strip().lower(), year_str)
        if dedupe_key in seen_movie_keys:
            continue
        seen_movie_keys.add(dedupe_key)

        try:
            f_size = os.path.getsize(m.file_path)
        except OSError:
            f_size = 0
        q_label = _probe_or_cached_resolution(m.file_path, f_size, prev_cache)
        movies_out.append({
            "id": m.id,
            "media_type": "movie",
            "title": m.title,
            "year": year_str,
            "quality_label": q_label,
            "file_size": f_size,
            "filename": os.path.basename(m.file_path),
            "genres": m.genres or "",
            "rating": m.rating,
            "_file_path": m.file_path,
        })

    tv_out: list[dict[str, Any]] = []
    all_eps = session.exec(select(TVShow).order_by(TVShow.title.asc(), TVShow.season.asc(), TVShow.episode.asc())).all()
    for ep in all_eps:
        if not ep.file_path or not os.path.exists(ep.file_path):
            continue
        try:
            f_size = os.path.getsize(ep.file_path)
        except OSError:
            f_size = 0
        q_label = _probe_or_cached_resolution(ep.file_path, f_size, prev_cache)
        s_num = ep.season if ep.season is not None else 1
        e_num = ep.episode if ep.episode is not None else 1
        tv_out.append({
            "id": ep.id,
            "media_type": "tv",
            "title": ep.episode_title or f"S{s_num:02d}E{e_num:02d}",
            "series_title": ep.title,
            "year": ep.year or "",
            "season": s_num,
            "episode": e_num,
            "quality_label": q_label,
            "file_size": f_size,
            "filename": os.path.basename(ep.file_path),
            "genres": ep.genres or "",
            "rating": ep.rating,
            "_file_path": ep.file_path,
        })

    photos_out: list[dict[str, Any]] = []
    all_photos = session.exec(select(Photo).order_by(Photo.title.asc())).all()
    for ph in all_photos:
        if not ph.file_path or not os.path.exists(ph.file_path):
            continue
        try:
            f_size = os.path.getsize(ph.file_path)
        except OSError:
            f_size = 0
        photos_out.append({
            "id": ph.id,
            "media_type": "photo",
            "title": ph.title,
            "album": ph.album or "",
            "quality_label": "original",
            "file_size": f_size,
            "filename": os.path.basename(ph.file_path),
            "_file_path": ph.file_path,
        })

    music_out: list[dict[str, Any]] = []
    all_tracks = session.exec(select(Track).order_by(Track.artist_name.asc(), Track.album_title.asc(), Track.track_number.asc())).all()
    for tr in all_tracks:
        if not tr.file_path or not os.path.exists(tr.file_path):
            continue
        try:
            f_size = os.path.getsize(tr.file_path)
        except OSError:
            f_size = 0
        music_out.append({
            "id": tr.id,
            "media_type": "music",
            "title": tr.title,
            "artist_name": tr.artist_name or "",
            "album_title": tr.album_title or "",
            "track_number": tr.track_number,
            "disc_number": tr.disc_number,
            "year": tr.year or "",
            "format": tr.format or "",
            "quality_label": "original",
            "file_size": f_size,
            "filename": os.path.basename(tr.file_path),
            "_file_path": tr.file_path,
        })

    manifest = {
        "version": 3,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "movies": movies_out,
        "tv": tv_out,
        "photos": photos_out,
        "music": music_out,
    }
    manifest_path = get_loom_manifest_path()
    tmp_path = f"{manifest_path}.{uuid.uuid4().hex}.tmp"
    with open(tmp_path, "w", encoding="utf-8") as fp:
        json.dump(manifest, fp)
    os.replace(tmp_path, manifest_path)
    return manifest


def load_loom_manifest(session: Optional[Session] = None) -> dict[str, Any]:
    """Load the lightweight manifest file without touching media drives."""
    manifest_path = get_loom_manifest_path()
    if os.path.exists(manifest_path):
        try:
            with open(manifest_path, "r", encoding="utf-8") as fp:
                data = json.load(fp)
            if isinstance(data, dict) and data.get("version", 1) >= 3 and "movies" in data and "tv" in data:
                data.setdefault("photos", [])
                data.setdefault("music", [])
                return data
        except Exception:
            pass
    if session is not None:
        return rebuild_loom_manifest(session)
    return {"version": 3, "generated_at": None, "movies": [], "tv": [], "photos": [], "music": []}


def get_manifest_quality_label(session: Session, media_type: str, item_id: int, file_path: str) -> str:
    """Return the probed resolution label from the Loom manifest for a movie/tv item, falling back to filename."""
    bucket = "movies" if media_type == "movie" else ("tv" if media_type == "tv" else "")
    if bucket:
        manifest = load_loom_manifest(session)
        for entry in manifest.get(bucket, []):
            if entry.get("id") == item_id and entry.get("quality_label"):
                return str(entry["quality_label"])
    return _infer_resolution_from_filename(file_path)


def _normalize_search_text(text: Optional[str]) -> str:
    cleaned = re.sub(r"[^\w\s]", " ", (text or "").lower())
    return re.sub(r"\s+", " ", cleaned).strip()


def _matches_specific_query(norm_query: str, *candidates: Optional[str]) -> bool:
    if not norm_query:
        return False
    pattern = re.compile(rf"(?<!\w){re.escape(norm_query)}(?!\w)")
    for cand in candidates:
        norm_cand = _normalize_search_text(cand)
        if not norm_cand:
            continue
        if norm_cand == norm_query or pattern.search(norm_cand):
            return True
    return False


def _strip_private_fields(item: dict[str, Any]) -> dict[str, Any]:
    return {k: v for k, v in item.items() if not k.startswith("_")}


def parse_loom_search_query(raw_query: str) -> dict[str, Any]:
    """
    Dissect a search string like 'The Office S1E4', 'The Office S1', 'Dexter Season 2 Episode 3',
    or mixed forms like 'Dexter Season 1 E3' into a clean title query plus optional season,
    episode, and inferred media_type.
    """
    text = str(raw_query or "").strip()
    season: Optional[int] = None
    episode: Optional[int] = None

    cleaned = text
    combined_patterns = (
        # S01E04, S1 E4, S01.E04, S1-E4
        re.compile(r"\b[sS](\d{1,2})[\s.\-_]*[eE](\d{1,3})\b"),
        # 1x04
        re.compile(r"\b(\d{1,2})[xX](\d{1,3})\b"),
    )

    matched_combined = False
    for pat in combined_patterns:
        m = pat.search(cleaned)
        if m:
            season = int(m.group(1))
            episode = int(m.group(2))
            cleaned = (cleaned[:m.start()] + " " + cleaned[m.end():]).strip()
            matched_combined = True
            break

    if not matched_combined:
        season_pat = re.compile(r"\b(?:season|s)\s*(\d{1,2})\b", re.IGNORECASE)
        episode_pat = re.compile(r"\b(?:episode|ep\.?|e)\s*(\d{1,3})\b", re.IGNORECASE)

        sm = season_pat.search(cleaned)
        if sm:
            season = int(sm.group(1))
            cleaned = (cleaned[:sm.start()] + " " + cleaned[sm.end():]).strip()

        em = episode_pat.search(cleaned)
        if em:
            episode = int(em.group(1))
            cleaned = (cleaned[:em.start()] + " " + cleaned[em.end():]).strip()

    cleaned = re.sub(r"[\s\-–—:]+$", "", cleaned)
    cleaned = re.sub(r"^[\s\-–—:]+", "", cleaned)
    cleaned = re.sub(r"\s+", " ", cleaned).strip()

    return {
        "query": cleaned or text,
        "season": season,
        "episode": episode,
        "media_type": "tv" if (season is not None or episode is not None) else None,
    }


def search_loom_manifest(
    session: Session,
    query: str,
    media_type: Optional[str] = None,
    year: Optional[str] = None,
    season: Optional[int] = None,
    episode: Optional[int] = None,
) -> dict[str, Any]:
    """
    Answer a specific item availability query (e.g. 'Dexter', 'The Office S1', 'The Office S1E4')
    strictly within volumes explicitly enabled as searchable. Never dumps an entire library.
    """
    raw_query = str(query or "").strip()
    if any(ch in raw_query for ch in ("*", "?", "%")):
        raise ValueError("Wildcard queries are not allowed. Request a specific item name.")

    parsed = parse_loom_search_query(raw_query)
    effective_query = parsed["query"]
    effective_season = int(season) if season is not None else parsed["season"]
    effective_episode = int(episode) if episode is not None else parsed["episode"]

    norm_q = _normalize_search_text(effective_query)
    if len(norm_q) < 2:
        raise ValueError("Query must specify an item name (at least 2 characters).")

    settings = get_loom_settings(session)
    searchable_volumes = settings["searchable_volumes"]
    allowed_filetypes = settings["allowed_filetypes_list"]
    max_file_size_gb = settings["max_file_size_gb"]
    norm_type = (media_type or parsed["media_type"] or "").strip().lower() or None
    norm_year = str(year or "").strip() or None

    if not searchable_volumes or not allowed_filetypes:
        return {
            "found": False,
            "query": raw_query,
            "movies": [],
            "tv": [],
            "photos": [],
            "music": [],
        }

    manifest = load_loom_manifest(session)
    movies: list[dict[str, Any]] = []
    tv: list[dict[str, Any]] = []
    photos: list[dict[str, Any]] = []
    music: list[dict[str, Any]] = []

    if norm_type in (None, "movie") and effective_season is None and effective_episode is None:
        for item in manifest.get("movies", []):
            if not is_path_in_volumes(item.get("_file_path", ""), searchable_volumes):
                continue
            if not is_filetype_allowed(item.get("filename") or item.get("_file_path"), allowed_filetypes):
                continue
            if not is_file_size_allowed(item.get("file_size"), max_file_size_gb):
                continue
            if norm_year and str(item.get("year") or "").strip() != norm_year:
                continue
            if _matches_specific_query(norm_q, item.get("title")):
                movies.append(_strip_private_fields(item))

    if norm_type in (None, "tv"):
        for item in manifest.get("tv", []):
            if not is_path_in_volumes(item.get("_file_path", ""), searchable_volumes):
                continue
            if not is_filetype_allowed(item.get("filename") or item.get("_file_path"), allowed_filetypes):
                continue
            if not is_file_size_allowed(item.get("file_size"), max_file_size_gb):
                continue
            if norm_year and str(item.get("year") or "").strip() != norm_year:
                continue
            if effective_season is not None:
                try:
                    if int(item.get("season")) != effective_season:
                        continue
                except (TypeError, ValueError):
                    continue
            if effective_episode is not None:
                try:
                    if int(item.get("episode")) != effective_episode:
                        continue
                except (TypeError, ValueError):
                    continue
            if _matches_specific_query(norm_q, item.get("series_title")):
                tv.append(_strip_private_fields(item))

    if norm_type in (None, "photo") and effective_season is None and effective_episode is None:
        for item in manifest.get("photos", []):
            if not is_path_in_volumes(item.get("_file_path", ""), searchable_volumes):
                continue
            if not is_filetype_allowed(item.get("filename") or item.get("_file_path"), allowed_filetypes):
                continue
            if not is_file_size_allowed(item.get("file_size"), max_file_size_gb):
                continue
            if _matches_specific_query(norm_q, item.get("title"), item.get("album")):
                photos.append(_strip_private_fields(item))

    if norm_type in (None, "music") and effective_season is None and effective_episode is None:
        for item in manifest.get("music", []):
            if not is_path_in_volumes(item.get("_file_path", ""), searchable_volumes):
                continue
            if not is_filetype_allowed(item.get("filename") or item.get("_file_path"), allowed_filetypes):
                continue
            if not is_file_size_allowed(item.get("file_size"), max_file_size_gb):
                continue
            if norm_year and str(item.get("year") or "").strip() != norm_year:
                continue
            if _matches_specific_query(norm_q, item.get("title"), item.get("album_title"), item.get("artist_name")):
                music.append(_strip_private_fields(item))

    found = bool(movies or tv or photos or music)
    return {
        "found": found,
        "query": raw_query,
        "movies": movies,
        "tv": tv,
        "photos": photos,
        "music": music,
    }


def _rank_loom_source(item: dict[str, Any]) -> tuple[int, int, int]:
    """Lower tuple is better: prefer fewest hops (Direct=0), then highest quality, then largest size."""
    hops = int(item.get("hops", 0) or 0)
    q_h = quality_height(item.get("quality_label"))
    size = int(item.get("file_size", 0) or 0)
    return (hops, -q_h, -size)


def _title_match_rank(title: Optional[str], norm_q: str) -> int:
    """Rank exact title matches (0) ahead of prefix matches (1) and other matches (2)."""
    if not norm_q:
        return 0
    norm_t = _normalize_search_text(title)
    if norm_t == norm_q:
        return 0
    if norm_t.startswith(norm_q + " "):
        return 1
    return 2


def deduplicate_loom_search_results(
    movies: list[dict[str, Any]],
    tv: list[dict[str, Any]],
    photos: list[dict[str, Any]],
    music: list[dict[str, Any]],
    query: str = "",
) -> dict[str, list[dict[str, Any]]]:
    """
    Merge duplicate matches across multiple peers, picking the best source per item
    (Direct > relayed, higher quality > lower quality, larger file > smaller) and
    annotating each item with `sources_count`.
    """
    norm_q = _normalize_search_text(parse_loom_search_query(query)["query"]) if query else ""

    def _merge(items: list[dict[str, Any]], key_fn: Callable[[dict[str, Any]], tuple]) -> list[dict[str, Any]]:
        grouped: dict[tuple, list[dict[str, Any]]] = {}
        for item in items:
            k = key_fn(item)
            grouped.setdefault(k, []).append(item)
        merged: list[dict[str, Any]] = []
        for group in grouped.values():
            group_sorted = sorted(group, key=_rank_loom_source)
            best = dict(group_sorted[0])
            unique_peers = {
                (g.get("peer_id"), g.get("relay_token") or g.get("id"))
                for g in group_sorted
            }
            best["sources_count"] = max(1, len(unique_peers))
            merged.append(best)
        return merged

    dedup_movies = _merge(
        movies,
        lambda m: (_normalize_search_text(m.get("title")), str(m.get("year") or "").strip()),
    )
    if norm_q:
        dedup_movies.sort(key=lambda m: (_title_match_rank(m.get("title"), norm_q), _normalize_search_text(m.get("title"))))

    dedup_tv = _merge(
        tv,
        lambda t: (_normalize_search_text(t.get("series_title") or t.get("title")), t.get("season"), t.get("episode")),
    )
    dedup_tv.sort(key=lambda t: (
        _title_match_rank(t.get("series_title") or t.get("title"), norm_q),
        _normalize_search_text(t.get("series_title") or ""),
        int(t.get("season") or 0),
        int(t.get("episode") or 0),
    ))

    dedup_photos = _merge(
        photos,
        lambda p: (_normalize_search_text(p.get("album")), _normalize_search_text(p.get("title"))),
    )
    dedup_music = _merge(
        music,
        lambda tr: (
            _normalize_search_text(tr.get("artist_name")),
            _normalize_search_text(tr.get("album_title")),
            _normalize_search_text(tr.get("title")),
        ),
    )
    return {
        "movies": dedup_movies,
        "tv": dedup_tv,
        "photos": dedup_photos,
        "music": dedup_music,
    }


_relay_state_lock = threading.Lock()
_seen_search_requests: dict[str, float] = {}
_relay_tokens: dict[str, dict[str, Any]] = {}
_peer_search_timestamps: dict[int, list[float]] = {}
_peer_active_transfers: dict[int, int] = {}


def check_peer_search_rate_limit(peer_id: int, max_per_minute: int) -> tuple[bool, int]:
    """
    Enforce a per-peer sliding 60s request rate limit.
    Returns (allowed, retry_after_seconds).
    """
    limit = max(1, int(max_per_minute or 1))
    now = time.monotonic()
    cutoff = now - 60.0
    with _relay_state_lock:
        recent = [ts for ts in _peer_search_timestamps.get(peer_id, []) if ts > cutoff]
        if len(recent) >= limit:
            _peer_search_timestamps[peer_id] = recent
            retry_after = max(1, int(60.0 - (now - recent[0])) + 1)
            return False, retry_after
        recent.append(now)
        _peer_search_timestamps[peer_id] = recent
        return True, 0


def acquire_peer_transfer_slot(peer_id: int, max_concurrent: int) -> bool:
    """Reserve an active transfer slot for a peer if below max_concurrent."""
    limit = max(1, int(max_concurrent or 1))
    with _relay_state_lock:
        cur = _peer_active_transfers.get(peer_id, 0)
        if cur >= limit:
            return False
        _peer_active_transfers[peer_id] = cur + 1
        return True


def release_peer_transfer_slot(peer_id: int) -> None:
    """Release an active transfer slot for a peer."""
    with _relay_state_lock:
        cur = _peer_active_transfers.get(peer_id, 0)
        if cur <= 1:
            _peer_active_transfers.pop(peer_id, None)
        else:
            _peer_active_transfers[peer_id] = cur - 1


def mark_or_check_search_request_id(request_id: str, ttl_seconds: float = 60.0) -> bool:
    """Return True if request_id was already seen within TTL (loop guard); otherwise record it and return False."""
    rid = (request_id or "").strip()
    if not rid:
        return False
    now = time.monotonic()
    with _relay_state_lock:
        expired = [k for k, exp in _seen_search_requests.items() if exp <= now]
        for k in expired:
            _seen_search_requests.pop(k, None)
        if rid in _seen_search_requests:
            return True
        _seen_search_requests[rid] = now + ttl_seconds
        return False


def register_relay_token(
    target_peer_id: int,
    media_type: str,
    remote_item_id: int,
    upstream_relay_token: Optional[str] = None,
    ttl_seconds: float = 1800.0,
) -> str:
    """Issue a short-lived opaque relay token mapping to a downstream peer item without exposing peer identity."""
    token = uuid.uuid4().hex
    now = time.monotonic()
    with _relay_state_lock:
        expired = [k for k, v in _relay_tokens.items() if v.get("expires_at", 0) <= now]
        for k in expired:
            _relay_tokens.pop(k, None)
        _relay_tokens[token] = {
            "target_peer_id": int(target_peer_id),
            "media_type": str(media_type),
            "remote_item_id": int(remote_item_id),
            "upstream_relay_token": upstream_relay_token,
            "expires_at": now + ttl_seconds,
        }
    return token


def resolve_relay_token(token: str) -> Optional[dict[str, Any]]:
    tok = (token or "").strip()
    if not tok:
        return None
    now = time.monotonic()
    with _relay_state_lock:
        entry = _relay_tokens.get(tok)
        if not entry:
            return None
        if entry.get("expires_at", 0) <= now:
            _relay_tokens.pop(tok, None)
            return None
        return dict(entry)


def rollover_peer_monthly_usage(session: Session, peer: LoomPeer) -> bool:
    cur_month = datetime.now(timezone.utc).strftime("%Y-%m")
    if peer.usage_month != cur_month:
        peer.usage_month = cur_month
        peer.bytes_sent_month = 0
        peer.bytes_received_month = 0
        session.add(peer)
        session.commit()
        return True
    return False


def quality_height(label: Optional[str]) -> int:
    key = (label or "hd").strip().lower()
    return QUALITY_HEIGHT_MAP.get(key, 1080)


def is_quality_allowed(item_quality: Optional[str], max_quality: str) -> bool:
    cap = quality_height(max_quality)
    if cap >= 99999:
        return True
    return quality_height(item_quality) <= cap


def can_transfer_now(
    session: Session,
    peer: Optional[LoomPeer],
    direction: str,
    has_active_streams_fn: Optional[Callable[[], bool]] = None,
    now_dt: Optional[datetime] = None,
) -> tuple[bool, Optional[str], Optional[str]]:
    """
    Evaluate Loom guardrails before starting or continuing a transfer.
    Returns (allowed, pause_status, reason).
    """
    settings = get_loom_settings(session)
    if not settings["enabled"]:
        return False, "paused_cap", "The Loom master switch is disabled."
    if not peer or peer.status != "approved":
        return False, "paused_cap", "Peer is not approved."
    if direction == "incoming" and not peer.allow_incoming:
        return False, "paused_cap", "Incoming sync is disabled for this peer."
    if direction == "outgoing" and not peer.allow_outgoing:
        return False, "paused_cap", "Outgoing sharing is disabled for this peer."

    if settings["pause_on_active_streams"] and has_active_streams_fn and has_active_streams_fn():
        return False, "paused_stream", "Paused while active media streams are playing."

    return True, None, None


def sanitize_filename_part(name: str) -> str:
    cleaned = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", (name or "").strip())
    return cleaned.strip(" .") or "Unknown"


def build_sync_target_path(
    sync_dir: str,
    media_type: str,
    title: str,
    remote_filename: str,
    series_title: Optional[str] = None,
    season: Optional[int] = None,
) -> str:
    safe_file = sanitize_filename_part(os.path.basename(remote_filename or f"{title}.mkv"))
    if media_type == "tv":
        show_dir = sanitize_filename_part(series_title or title)
        s_folder = f"Season {int(season)}" if season is not None else "Season 1"
        return os.path.join(sync_dir, "tv", show_dir, s_folder, safe_file)
    if media_type == "photo":
        return os.path.join(sync_dir, "photos", safe_file)
    if media_type == "music":
        return os.path.join(sync_dir, "music", safe_file)
    return os.path.join(sync_dir, "movies", safe_file)


ACTIVE_LOOM_JOB_STATUSES = ("queued", "downloading", "paused_stream", "paused_schedule", "paused_cap")


def _prune_empty_parents(start_dir: str, stop_root: Optional[str] = None) -> None:
    """Remove empty parent directories up to (but never including) stop_root."""
    if not start_dir:
        return
    norm_root = os.path.normcase(os.path.normpath(os.path.abspath(stop_root))) if stop_root else None
    cur = os.path.normpath(os.path.abspath(start_dir))
    for _ in range(5):
        if norm_root and os.path.normcase(cur) == norm_root:
            break
        if norm_root:
            try:
                if os.path.commonpath([os.path.normcase(cur), norm_root]) != norm_root:
                    break
            except ValueError:
                break
        try:
            if os.path.isdir(cur) and not os.listdir(cur):
                os.rmdir(cur)
                parent = os.path.dirname(cur)
                if parent == cur:
                    break
                cur = parent
            else:
                break
        except OSError:
            break


def cleanup_job_part_file(target_path: Optional[str], sync_dir: Optional[str] = None) -> bool:
    """Delete a job's .part file if present and prune any empty parent folders inside sync_dir."""
    if not target_path:
        return False
    part_path = target_path if target_path.endswith(".part") else f"{target_path}.part"
    removed = False
    if os.path.exists(part_path):
        try:
            os.remove(part_path)
            removed = True
        except OSError:
            return False
    if removed and sync_dir:
        _prune_empty_parents(os.path.dirname(part_path), stop_root=sync_dir)
    return removed


def cleanup_orphaned_loom_part_files(session: Session) -> int:
    """Delete any .part files under sync_dir that do not belong to an active/paused LoomTransferJob."""
    settings = get_loom_settings(session)
    sync_dir = settings.get("sync_dir") or "/media/loom"
    if not sync_dir or not os.path.isdir(sync_dir):
        return 0

    active_jobs = session.exec(
        select(LoomTransferJob).where(LoomTransferJob.status.in_(ACTIVE_LOOM_JOB_STATUSES))
    ).all()
    active_parts = {
        os.path.normcase(os.path.normpath(os.path.abspath(f"{j.target_path}.part")))
        for j in active_jobs
        if j.target_path
    }

    removed_count = 0
    for root, _, files in os.walk(sync_dir):
        for fname in files:
            if not fname.endswith(".part"):
                continue
            full_path = os.path.join(root, fname)
            norm_path = os.path.normcase(os.path.normpath(os.path.abspath(full_path)))
            if norm_path not in active_parts:
                if cleanup_job_part_file(full_path, sync_dir=sync_dir):
                    removed_count += 1
    return removed_count


class LoomSyncManager:
    """Background manager that executes queued LoomTransferJobs with download throttling and guardrail checks."""

    def __init__(self):
        self._stop_event = threading.Event()
        self._wake_event = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._has_active_streams_fn: Optional[Callable[[], bool]] = None
        self._on_file_synced_fn: Optional[Callable[[], None]] = None
        self._speed_lock = threading.Lock()
        self._job_speed_bps: dict[int, float] = {}
        self._job_retry_after: dict[int, float] = {}
        self._job_retry_counts: dict[int, int] = {}

    def _schedule_job_retry(self, job_id: int, reason: str, written: Optional[int] = None) -> None:
        """Re-queue an interrupted job with quick first probe (1.5s) then backoff (10s -> 20s -> 40s -> 60s) while keeping .part."""
        with self._speed_lock:
            attempts = self._job_retry_counts.get(job_id, 0) + 1
            self._job_retry_counts[job_id] = attempts
            delay = 1.5 if attempts == 1 else min(60.0, 10.0 * (2 ** (attempts - 2)))
            self._job_retry_after[job_id] = time.monotonic() + delay
        with Session(engine) as session:
            j = session.get(LoomTransferJob, job_id)
            if j and j.status not in ("cancelled", "completed"):
                j.status = "queued"
                if written is not None:
                    j.bytes_transferred = written
                j.error_message = f"{reason} (retrying in {int(delay)}s)"
                j.updated_at = datetime.now(timezone.utc)
                session.add(j)
                session.commit()

    def _clear_job_retry(self, job_id: int) -> None:
        with self._speed_lock:
            self._job_retry_after.pop(job_id, None)
            self._job_retry_counts.pop(job_id, None)

    def _is_job_in_backoff(self, job_id: int) -> bool:
        with self._speed_lock:
            due = self._job_retry_after.get(job_id)
            if due is None:
                return False
            if time.monotonic() >= due:
                self._job_retry_after.pop(job_id, None)
                return False
            return True

    def recover_interrupted_jobs(self, session: Session) -> int:
        """Reset any jobs left in 'downloading' state after a crash/restart back to 'queued' so they resume from .part."""
        stale = session.exec(select(LoomTransferJob).where(LoomTransferJob.status == "downloading")).all()
        for j in stale:
            j.status = "queued"
            part_path = f"{j.target_path}.part" if j.target_path else ""
            if part_path and os.path.exists(part_path):
                try:
                    j.bytes_transferred = os.path.getsize(part_path)
                except OSError:
                    pass
            j.error_message = "Resuming interrupted download..."
            j.updated_at = datetime.now(timezone.utc)
            session.add(j)
        if stale:
            session.commit()
        return len(stale)

    def get_job_speed_bps(self, job_id: int) -> float:
        with self._speed_lock:
            return float(self._job_speed_bps.get(job_id, 0.0))

    def _set_job_speed_bps(self, job_id: int, bps: float) -> None:
        with self._speed_lock:
            if bps <= 0:
                self._job_speed_bps.pop(job_id, None)
            else:
                prev = self._job_speed_bps.get(job_id, 0.0)
                self._job_speed_bps[job_id] = (0.6 * bps + 0.4 * prev) if prev > 0 else bps

    def configure(
        self,
        has_active_streams_fn: Optional[Callable[[], bool]] = None,
        on_file_synced_fn: Optional[Callable[[], None]] = None,
    ):
        self._has_active_streams_fn = has_active_streams_fn
        self._on_file_synced_fn = on_file_synced_fn

    def start(self):
        if self._thread and self._thread.is_alive():
            return
        self._stop_event.clear()
        self._thread = threading.Thread(target=self._run_loop, daemon=True, name="loom-sync-worker")
        self._thread.start()

    def stop(self):
        self._stop_event.set()
        self._wake_event.set()
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=3.0)

    def wake(self):
        self._wake_event.set()

    def _run_loop(self):
        # Recover any jobs that were mid-download before a restart/crash so they resume from their .part file
        try:
            with Session(engine) as session:
                recovered = self.recover_interrupted_jobs(session)
                if recovered:
                    print(f"[Loom] Re-queued {recovered} interrupted download(s) for automatic resume.")
        except Exception as e:
            print(f"[Loom] Interrupted job recovery error: {e}")

        last_cleanup = 0.0
        cleanup_interval = 600.0  # Sweep orphaned .part files every 10 minutes
        while not self._stop_event.is_set():
            self._wake_event.clear()
            now_mono = time.monotonic()
            if now_mono - last_cleanup >= cleanup_interval:
                try:
                    with Session(engine) as session:
                        removed = cleanup_orphaned_loom_part_files(session)
                        if removed:
                            print(f"[Loom] Cleaned up {removed} orphaned .part file(s).")
                        expired_bundles = prune_expired_issued_secrets(session)
                        if expired_bundles:
                            print(f"[Loom] Pruned {expired_bundles} expired peer bundle(s).")
                except Exception as e:
                    print(f"[Loom] Maintenance cleanup error: {e}")
                last_cleanup = now_mono

            try:
                did_work = self._process_next_job()
            except Exception as e:
                print(f"[Loom] Sync worker error: {e}")
                did_work = False
            if not did_work:
                self._wake_event.wait(timeout=5.0)

    def _process_next_job(self) -> bool:
        if httpx is None:
            return False

        with Session(engine) as session:
            settings = get_loom_settings(session)
            if not settings["enabled"]:
                return False

            candidates = session.exec(
                select(LoomTransferJob)
                .where(LoomTransferJob.status.in_(["queued", "paused_stream", "paused_cap"]))
                .order_by(LoomTransferJob.id.asc())
            ).all()
            if not candidates:
                return False

            chosen_job = None
            chosen_peer = None
            for job in candidates:
                if job.id is not None and self._is_job_in_backoff(job.id):
                    continue
                peer = session.get(LoomPeer, job.peer_id)
                allowed, pause_status, reason = can_transfer_now(
                    session,
                    peer,
                    "incoming",
                    has_active_streams_fn=self._has_active_streams_fn,
                )
                if allowed:
                    chosen_job = job
                    chosen_peer = peer
                    break
                if job.status != pause_status or job.error_message != reason:
                    job.status = pause_status or "paused_cap"
                    job.error_message = reason
                    job.updated_at = datetime.now(timezone.utc)
                    session.add(job)
            session.commit()

            if not chosen_job or not chosen_peer:
                return False

            job_id = chosen_job.id
            peer_id = chosen_peer.id
            peer_url = chosen_peer.base_url.rstrip("/")
            peer_secret = chosen_peer.shared_secret
            local_node_id, _ = get_peer_local_identity(session, chosen_peer)
            remote_path = f"/api/loom/v1/download/{chosen_job.media_type}/{chosen_job.remote_item_id}"
            target_path = chosen_job.target_path
            chosen_job.status = "downloading"
            chosen_job.error_message = None
            chosen_job.updated_at = datetime.now(timezone.utc)
            session.add(chosen_job)
            session.commit()

        return self._download_job(job_id, peer_id, peer_url, peer_secret, local_node_id, remote_path, target_path)

    def _download_job(
        self,
        job_id: int,
        peer_id: int,
        peer_url: str,
        peer_secret: str,
        local_node_id: str,
        remote_path: str,
        target_path: str,
    ) -> bool:
        os.makedirs(os.path.dirname(target_path), exist_ok=True)
        part_path = f"{target_path}.part"
        existing_offset = os.path.getsize(part_path) if os.path.exists(part_path) else 0

        headers = sign_loom_headers(local_node_id, peer_secret, "GET", remote_path)
        headers["X-Loom-Job-Id"] = f"{local_node_id}:{job_id}"
        if existing_offset > 0:
            headers["Range"] = f"bytes={existing_offset}-"

        last_check_time = time.monotonic()
        bytes_since_db_flush = 0
        window_start = time.monotonic()
        window_bytes = 0

        cancelled_mid_stream = False
        stopped_mid_stream = False
        written = existing_offset
        total_bytes = 0
        sync_dir_for_cleanup = os.path.dirname(target_path)
        try:
            with Session(engine) as session:
                dl_settings = get_loom_settings(session)
                sync_dir_for_cleanup = dl_settings.get("sync_dir") or "/media/loom"
                if not is_filetype_allowed(target_path, dl_settings["allowed_filetypes_list"]):
                    self._mark_job(job_id, "failed", "Filetype is not in Allowed Filetypes.")
                    cleanup_job_part_file(target_path, sync_dir=sync_dir_for_cleanup)
                    return True
            client_kwargs = get_httpx_client_kwargs(peer_url, timeout=httpx.Timeout(30.0, read=60.0))
            with httpx.Client(**client_kwargs) as client:
                with client.stream("GET", f"{peer_url}{remote_path}", headers=headers) as resp:
                    if resp.status_code == 410:
                        self._clear_job_retry(job_id)
                        self._mark_job(job_id, "canceled_by_sender", "Canceled by Sender")
                        cleanup_job_part_file(target_path, sync_dir=sync_dir_for_cleanup)
                        return True
                    elif resp.status_code == 416 and existing_offset > 0:
                        cleanup_job_part_file(target_path, sync_dir=sync_dir_for_cleanup)
                        self._schedule_job_retry(job_id, "Range offset reset", written=0)
                        return False
                    elif resp.status_code in (429, 500, 502, 503, 504):
                        self._schedule_job_retry(job_id, f"Peer temporarily unavailable (HTTP {resp.status_code})", written=existing_offset)
                        return False
                    elif resp.status_code not in (200, 206):
                        self._mark_job(job_id, "failed", f"Peer returned HTTP {resp.status_code}")
                        cleanup_job_part_file(target_path, sync_dir=sync_dir_for_cleanup)
                        return True

                    is_resume = resp.status_code == 206 and existing_offset > 0
                    mode = "ab" if is_resume else "wb"
                    written = existing_offset if is_resume else 0

                    content_len = resp.headers.get("content-length")
                    total_bytes = written + int(content_len) if (content_len and content_len.isdigit()) else 0

                    with Session(engine) as session:
                        j = session.get(LoomTransferJob, job_id)
                        if j:
                            j.bytes_transferred = written
                            if total_bytes > 0:
                                j.total_bytes = total_bytes
                            session.add(j)
                            session.commit()

                    with open(part_path, mode) as fp:
                        for chunk in resp.iter_bytes(chunk_size=65536):
                            if not chunk:
                                continue
                            if self._stop_event.is_set():
                                stopped_mid_stream = True
                                break

                            fp.write(chunk)
                            chunk_len = len(chunk)
                            written += chunk_len
                            bytes_since_db_flush += chunk_len
                            window_bytes += chunk_len

                            now_mono = time.monotonic()
                            if now_mono - last_check_time >= 1.0 or bytes_since_db_flush >= 512 * 1024:
                                dt = max(0.05, now_mono - last_check_time)
                                self._set_job_speed_bps(job_id, bytes_since_db_flush / dt)
                                self._clear_job_retry(job_id)
                                with Session(engine) as session:
                                    j = session.get(LoomTransferJob, job_id)
                                    p = session.get(LoomPeer, peer_id)
                                    if not j or j.status == "cancelled":
                                        cancelled_mid_stream = True
                                        break
                                    settings = get_loom_settings(session)
                                    j.bytes_transferred = written
                                    j.updated_at = datetime.now(timezone.utc)
                                    if p:
                                        rollover_peer_monthly_usage(session, p)
                                        p.bytes_received_month += bytes_since_db_flush
                                        p.last_seen_at = datetime.now(timezone.utc)
                                        session.add(p)

                                    allowed, pause_status, reason = can_transfer_now(
                                        session, p, "incoming", has_active_streams_fn=self._has_active_streams_fn
                                    )
                                    if not allowed:
                                        j.status = pause_status or "paused_cap"
                                        j.error_message = reason
                                        session.add(j)
                                        session.commit()
                                        return True
                                    session.add(j)
                                    session.commit()
                                    bw_kbps = settings["download_bandwidth_limit_kbps"]
                                bytes_since_db_flush = 0
                                last_check_time = now_mono

                                if bw_kbps > 0:
                                    elapsed = now_mono - window_start
                                    target_elapsed = window_bytes / (bw_kbps * 1024.0)
                                    if target_elapsed > elapsed:
                                        time.sleep(min(2.0, target_elapsed - elapsed))
                                    if now_mono - window_start >= 1.0:
                                        window_start = time.monotonic()
                                        window_bytes = 0

            if cancelled_mid_stream:
                self._clear_job_retry(job_id)
                cleanup_job_part_file(target_path, sync_dir=sync_dir_for_cleanup)
                return True

            if stopped_mid_stream:
                with Session(engine) as session:
                    j = session.get(LoomTransferJob, job_id)
                    if j and j.status == "downloading":
                        j.status = "queued"
                        j.bytes_transferred = written
                        j.updated_at = datetime.now(timezone.utc)
                        session.add(j)
                        session.commit()
                return False

            # Guard against silent mid-stream TCP/Tor truncation: keep .part and resume instead of finalizing a partial file
            if total_bytes > 0 and written < total_bytes:
                self._schedule_job_retry(job_id, "Connection severed mid-download", written=written)
                return False

            os.replace(part_path, target_path)
            self._clear_job_retry(job_id)
            with Session(engine) as session:
                j = session.get(LoomTransferJob, job_id)
                p = session.get(LoomPeer, peer_id)
                if j:
                    j.status = "completed"
                    j.bytes_transferred = os.path.getsize(target_path)
                    j.total_bytes = j.bytes_transferred
                    j.error_message = None
                    j.updated_at = datetime.now(timezone.utc)
                    session.add(j)
                if p:
                    if bytes_since_db_flush > 0:
                        rollover_peer_monthly_usage(session, p)
                        p.bytes_received_month += bytes_since_db_flush
                    p.last_sync_at = datetime.now(timezone.utc)
                    session.add(p)
                session.commit()

            if self._on_file_synced_fn:
                try:
                    self._on_file_synced_fn()
                except Exception:
                    pass
            return True
        except Exception as e:
            # Keep .part file on transient/severed network errors so the download resumes automatically
            self._schedule_job_retry(job_id, f"Connection interrupted: {str(e)[:120]}", written=written)
            return False
        finally:
            self._set_job_speed_bps(job_id, 0.0)

    def _mark_job(self, job_id: int, status: str, error_message: Optional[str]):
        with Session(engine) as session:
            j = session.get(LoomTransferJob, job_id)
            if j and j.status != "cancelled":
                j.status = status
                j.error_message = error_message
                j.updated_at = datetime.now(timezone.utc)
                session.add(j)
                session.commit()


class LoomUploadTracker:
    """Tracks active outgoing Loom uploads so admins can monitor and cancel them in real time."""

    CANCEL_TTL_JOB_SECONDS = 1800.0   # 30 minutes for specific remote job IDs
    CANCEL_TTL_ITEM_SECONDS = 60.0    # 60 seconds fallback when no X-Loom-Job-Id header is sent

    def __init__(self):
        self._lock = threading.Lock()
        self._uploads: dict[str, dict[str, Any]] = {}
        self._cancelled_keys: dict[tuple, float] = {}

    def _prune_cancelled_locked(self, now: float) -> None:
        expired = [k for k, exp in self._cancelled_keys.items() if exp <= now]
        for k in expired:
            self._cancelled_keys.pop(k, None)

    def register_upload(
        self,
        *,
        peer_id: Optional[int],
        peer_name: str,
        media_type: str,
        item_id: int,
        title: str,
        series_title: Optional[str] = None,
        season: Optional[int] = None,
        episode: Optional[int] = None,
        quality_label: str = "",
        total_bytes: int = 0,
        start_offset: int = 0,
        hops: int = 1,
        remote_job_id: str = "",
    ) -> tuple[str, threading.Event]:
        upload_id = uuid.uuid4().hex
        cancel_event = threading.Event()
        with self._lock:
            self._uploads[upload_id] = {
                "upload_id": upload_id,
                "peer_id": peer_id,
                "peer_name": peer_name or "Anonymous",
                "media_type": media_type,
                "item_id": item_id,
                "remote_job_id": (remote_job_id or "").strip(),
                "title": title or "Untitled",
                "series_title": series_title,
                "season": season,
                "episode": episode,
                "quality_label": quality_label or "",
                "hops": max(1, int(hops or 1)),
                "bytes_transferred": max(0, int(start_offset or 0)),
                "total_bytes": max(0, int(total_bytes or 0)),
                "speed_bps": 0.0,
                "status": "uploading",
                "cancel_event": cancel_event,
                "started_at": time.monotonic(),
            }
        return upload_id, cancel_event

    def update_progress(self, upload_id: str, bytes_transferred: int, speed_bps: float) -> None:
        with self._lock:
            entry = self._uploads.get(upload_id)
            if not entry:
                return
            entry["bytes_transferred"] = max(0, int(bytes_transferred))
            if speed_bps > 0:
                prev = float(entry.get("speed_bps") or 0.0)
                entry["speed_bps"] = (0.6 * speed_bps + 0.4 * prev) if prev > 0 else speed_bps

    def cancel_upload(self, upload_id: str) -> bool:
        now = time.monotonic()
        with self._lock:
            self._prune_cancelled_locked(now)
            entry = self._uploads.get(upload_id)
            if not entry:
                return False
            entry["status"] = "cancelled"
            entry["cancel_event"].set()
            peer_id = entry.get("peer_id")
            remote_job_id = entry.get("remote_job_id") or ""
            if peer_id is not None:
                if remote_job_id:
                    self._cancelled_keys[("job", peer_id, remote_job_id)] = now + self.CANCEL_TTL_JOB_SECONDS
                else:
                    self._cancelled_keys[("item", peer_id, entry.get("media_type"), entry.get("item_id"))] = (
                        now + self.CANCEL_TTL_ITEM_SECONDS
                    )
            return True

    def is_transfer_cancelled_by_sender(
        self,
        peer_id: Optional[int],
        remote_job_id: str,
        media_type: str,
        item_id: int,
    ) -> bool:
        if peer_id is None:
            return False
        now = time.monotonic()
        rjid = (remote_job_id or "").strip()
        with self._lock:
            self._prune_cancelled_locked(now)
            if rjid and ("job", peer_id, rjid) in self._cancelled_keys:
                return True
            if not rjid and ("item", peer_id, media_type, item_id) in self._cancelled_keys:
                return True
            return False

    def unregister_upload(self, upload_id: str) -> None:
        with self._lock:
            self._uploads.pop(upload_id, None)

    def list_active_uploads(self) -> list[dict[str, Any]]:
        with self._lock:
            results: list[dict[str, Any]] = []
            for entry in self._uploads.values():
                if entry.get("status") != "uploading":
                    continue
                total = int(entry.get("total_bytes") or 0)
                transferred = int(entry.get("bytes_transferred") or 0)
                pct = round((transferred / total) * 100.0, 1) if total > 0 else 0.0
                results.append({
                    "id": entry["upload_id"],
                    "upload_id": entry["upload_id"],
                    "direction": "upload",
                    "peer_id": entry["peer_id"],
                    "peer_name": entry["peer_name"],
                    "media_type": entry["media_type"],
                    "title": entry["title"],
                    "series_title": entry["series_title"],
                    "season": entry["season"],
                    "episode": entry["episode"],
                    "quality_label": entry["quality_label"],
                    "hops": entry["hops"],
                    "status": "uploading",
                    "bytes_transferred": transferred,
                    "total_bytes": total,
                    "progress_pct": min(100.0, pct),
                    "speed_bps": round(float(entry.get("speed_bps") or 0.0), 1),
                })
            return results


loom_upload_tracker = LoomUploadTracker()
loom_sync_manager = LoomSyncManager()
