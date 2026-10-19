import os
import threading
from watchdog.observers import Observer
from watchdog.events import FileSystemEventHandler
from sqlmodel import Session, select
from .database import engine
from .models import VolumePath
from .scanner import scan_media_library, VIDEO_EXTENSIONS, PHOTO_EXTENSIONS, is_recycle_path

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

class MediaEventHandler(FileSystemEventHandler):
    def on_any_event(self, event):
        # Ignore events originating inside recycle/trash folders
        if is_recycle_path(event.src_path):
            return
        if hasattr(event, 'dest_path') and event.dest_path and is_recycle_path(event.dest_path):
            return

        # Trigger scan on directory events (e.g. moving a folder full of media into the library)
        if event.is_directory:
            scanner_debouncer.trigger_scan()
            return
        
        # Trigger scan on specific video or photo file events
        src_lower = event.src_path.lower()
        if src_lower.endswith(VIDEO_EXTENSIONS) or src_lower.endswith(PHOTO_EXTENSIONS):
            scanner_debouncer.trigger_scan()
        elif hasattr(event, 'dest_path') and event.dest_path:
            dest_lower = event.dest_path.lower()
            if dest_lower.endswith(VIDEO_EXTENSIONS) or dest_lower.endswith(PHOTO_EXTENSIONS):
                scanner_debouncer.trigger_scan()

observer = Observer()

def start_watcher():
    global observer
    
    # Try to stop existing observer if running
    if observer.is_alive():
        observer.stop()
        observer.join()
        observer = Observer()
        
    try:
        with Session(engine) as session:
            volume_paths = session.exec(select(VolumePath)).all()
        
        handler = MediaEventHandler()
        
        scheduled = False
        for vp in volume_paths:
            if os.path.exists(vp.path):
                observer.schedule(handler, vp.path, recursive=True)
                scheduled = True
        
        if scheduled:
            observer.start()
    except Exception as e:
        print(f"Failed to start watchdog: {e}")

def stop_watcher():
    global observer
    if observer.is_alive():
        observer.stop()
        observer.join()
