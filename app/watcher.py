import os
import threading
from watchdog.observers import Observer
from watchdog.observers.polling import PollingObserver
from watchdog.events import FileSystemEventHandler
from sqlmodel import Session, select
from .database import engine
from .models import VolumePath
from .scanner import scan_media_library, VIDEO_EXTENSIONS, PHOTO_EXTENSIONS, AUDIO_EXTENSIONS, is_recycle_path

class ScannerDebouncer:
    def __init__(self, delay=3.0):
        self.delay = delay
        self._timer = None

    def trigger_scan(self, delay=None):
        if self._timer is not None:
            self._timer.cancel()
        d = delay if delay is not None else self.delay
        self._timer = threading.Timer(d, self._run_scan)
        self._timer.start()

    def _run_scan(self):
        try:
            with Session(engine) as session:
                scan_media_library(session)
        except Exception as e:
            print(f"Error during background scan: {e}")

scanner_debouncer = ScannerDebouncer()

class PeriodicScanWorker:
    """Heartbeat background timer to ensure library stays updated even if filesystem events fail."""
    def __init__(self, interval_seconds=3600):
        self.interval = interval_seconds
        self._stop_event = threading.Event()
        self._thread = None

    def start(self):
        if self._thread and self._thread.is_alive():
            return
        self._stop_event.clear()
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def stop(self):
        self._stop_event.set()

    def _run(self):
        while not self._stop_event.wait(self.interval):
            try:
                scanner_debouncer.trigger_scan(delay=1.0)
            except Exception as e:
                print(f"[PeriodicScan] Error triggering periodic scan: {e}")

periodic_scanner = PeriodicScanWorker(interval_seconds=3600)

class MediaEventHandler(FileSystemEventHandler):
    def on_any_event(self, event):
        # Ignore events originating inside recycle/trash folders
        if is_recycle_path(event.src_path):
            return
        if hasattr(event, 'dest_path') and event.dest_path and is_recycle_path(event.dest_path):
            return

        # Ignore directory modification events (they fire constantly on access/reads)
        if event.is_directory:
            if getattr(event, 'event_type', None) in ('created', 'deleted', 'moved'):
                scanner_debouncer.trigger_scan(delay=5.0)
            return
        
        # Trigger scan on specific video, photo, or audio file events
        if getattr(event, 'event_type', None) in ('created', 'deleted', 'moved', 'modified'):
            src_lower = event.src_path.lower()
            if src_lower.endswith(VIDEO_EXTENSIONS) or src_lower.endswith(PHOTO_EXTENSIONS) or src_lower.endswith(AUDIO_EXTENSIONS):
                scanner_debouncer.trigger_scan(delay=5.0)
            elif hasattr(event, 'dest_path') and event.dest_path:
                dest_lower = event.dest_path.lower()
                if dest_lower.endswith(VIDEO_EXTENSIONS) or dest_lower.endswith(PHOTO_EXTENSIONS) or dest_lower.endswith(AUDIO_EXTENSIONS):
                    scanner_debouncer.trigger_scan(delay=5.0)

observer = Observer()

def _try_increase_inotify_limit():
    """Attempt to increase kernel inotify watch limit automatically if container has write access."""
    try:
        proc_path = "/proc/sys/fs/inotify/max_user_watches"
        if os.path.exists(proc_path):
            with open(proc_path, "r") as f:
                content = f.read().strip()
                current = int(content) if content.isdigit() else 0
            if current < 524288:
                with open(proc_path, "w") as f:
                    f.write("524288\n")
                print(f"[Watchdog] Automatically increased inotify max_user_watches to 524288.")
    except Exception:
        pass

def _schedule_all(target_observer, volume_paths, handler):
    """Schedules all existing volume paths onto the given observer instance."""
    scheduled_any = False
    for vp in volume_paths:
        if os.path.exists(vp.path):
            target_observer.schedule(handler, vp.path, recursive=True)
            scheduled_any = True
    return scheduled_any

def start_watcher():
    global observer
    
    _try_increase_inotify_limit()

    # Try to stop existing observer if running
    if observer.is_alive():
        try:
            observer.stop()
            observer.join(timeout=2.0)
        except Exception:
            pass
    observer = Observer()
        
    try:
        with Session(engine) as session:
            volume_paths = session.exec(select(VolumePath)).all()
    except Exception as e:
        print(f"[Watchdog] Could not query volume paths: {e}")
        return

    handler = MediaEventHandler()
    used_observer = observer

    try:
        # 1. Attempt native inotify/OS observer first
        scheduled = _schedule_all(used_observer, volume_paths, handler)
        if scheduled:
            used_observer.start()
    except (OSError, Exception) as e:
        is_inotify_limit = False
        if isinstance(e, OSError) and (getattr(e, 'errno', None) == 28 or "inotify watch limit" in str(e).lower()):
            is_inotify_limit = True
        elif "inotify watch limit" in str(e).lower():
            is_inotify_limit = True

        if is_inotify_limit:
            print("[Watchdog] Inotify watch limit reached. Automatically switching to PollingObserver (no inotify watches required).")
        else:
            print(f"[Watchdog] Native observer failed ({e}). Falling back to PollingObserver.")

        try:
            used_observer.stop()
        except Exception:
            pass

        # 2. Seamlessly fall back to PollingObserver (uses stat polling, zero inotify descriptors needed)
        used_observer = PollingObserver(timeout=15.0)
        try:
            scheduled = _schedule_all(used_observer, volume_paths, handler)
            if scheduled:
                used_observer.start()
                print("[Watchdog] PollingObserver active across all library volumes.")
        except Exception as pe:
            print(f"[Watchdog] Failed to start fallback PollingObserver: {pe}")
            used_observer = Observer()

    observer = used_observer
    periodic_scanner.start()

def stop_watcher():
    global observer
    periodic_scanner.stop()
    if observer.is_alive():
        try:
            observer.stop()
            observer.join(timeout=2.0)
        except Exception:
            pass
