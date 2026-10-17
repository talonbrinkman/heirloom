# Heirloom

**Heirloom** is an open-source, high-performance media streaming server built fundamentally around **digital sovereignty and self-hosting**. In a world of fragmented subscription services, Heirloom is designed to give you absolute, uncompromising ownership over your personal library of movies, TV shows, and photos. It brings the premium, polished features of paid streaming platforms directly to your home server—completely for free.

By leveraging a lightweight, API-first architecture using Python and FastAPI, Heirloom empowers you to host your own Netflix-like experience without sacrificing privacy, control, or your wallet.

## 🚀 Premium Features, Zero Cost

- 100% Free & Open Source
- Watch Together
- Skip Intro & Credits
- Rewind on Resume
- Download Capability
- Automated Media Management
- Real-Time Stream Dashboard
- Fully Responsive Mobile & Tablet Web UI
- Profiles & Multi-User PINs
- Non-Blocking Async Scanner & Real-Time Action Indicator
- Role-Based Access Control
- Kids Mode & Content Filtering
- Stateful Playback & Watchlists
- Smart Remuxing & Direct Stream
- Multi-Audio Track Selection
- Multi-Track Subtitle Extraction (Embedded & Sidecar WebVTT)
- Adaptive Bitrate (ABR) & HLS Streaming
- Hardware-Accelerated Transcoding
- Cross-Platform REST API
- Frictionless Docker Deployment

## 🛠️ Tech Stack

- **Backend**: [Python 3](https://www.python.org/), [FastAPI](https://fastapi.tiangolo.com/)
- **Database**: [SQLite](https://www.sqlite.org/), [SQLModel](https://sqlmodel.tiangolo.com/) (SQLAlchemy & Pydantic)
- **Real-time Communication**: WebSockets
- **Containerization**: [Docker](https://www.docker.com/), Docker Compose
- **Media Processing**: FFmpeg
- **Frontend**: Jinja2 Templates, HTML/CSS/JavaScript

## 🏗️ Architecture Overview

Heirloom utilizes an asynchronous FastAPI backend to handle high-concurrency streaming and REST API requests efficiently. The database schema is strictly typed and managed using SQLModel, ensuring data integrity. The application includes a multi-threaded watcher and scanner that monitors mounted volumes for new media, extracting metadata automatically. Video playback and real-time syncing are handled via native HTML5 video coupled with a custom WebSocket connection manager.

## ⚙️ Installation & Setup

### Prerequisites
- [Docker](https://docs.docker.com/get-docker/) and [Docker Compose](https://docs.docker.com/compose/install/)

### Quick Start with Docker

1. **Clone the repository:**
   ```bash
   git clone https://github.com/talonbrinkman/heirloom.git
   cd heirloom
   ```

2. **Configure Environment:**
   Create a `.env` file in the root directory to define your user IDs and timezone:
   ```env
   PUID=1000
   PGID=1000
   TZ=America/Chicago
   ```

3. **Mount your media:**
   Edit the `docker-compose.yml` file to point the volume mounts to your local media directories:
   ```yaml
   volumes:
     - /path/to/your/tv:/media/tv:ro
     - /path/to/your/movies:/media/movies:ro
     - /path/to/your/photos:/media/photos:ro
   ```

4. **(Optional) Enable Hardware-Accelerated Transcoding (GPU):**
   Heirloom includes automated GPU detection with universal software fallback. By default, device mounts are commented out so the container starts safely on any system (Windows, Mac, cloud VPS).

   To enable GPU hardware acceleration:
   - **Synology NAS / Linux with Intel QuickSync or AMD GPU (VA-API)**:
     Uncomment the `devices:` section in `docker-compose.yml`:
     ```yaml
     devices:
       - /dev/dri:/dev/dri
     ```
     *(This mounts `/dev/dri/renderD128` directly into the container, granting access to Intel QuickSync and AMD VA-API encoders).*

   - **Linux with NVIDIA GPU**:
     Ensure the [NVIDIA Container Toolkit](https://docs.nvidia.com/datacenter/cloud-native/container-toolkit/latest/install-guide.html) is installed on the host, then uncomment the `deploy:` section in `docker-compose.yml`:
     ```yaml
     deploy:
       resources:
         reservations:
           devices:
             - driver: nvidia
               count: all
               capabilities: [gpu]
     ```

   - **Web UI Profile Selection**:
     Navigate to **Settings** -> **Hardware Accelerated Transcoding** to keep **Auto** (recommended; probes available hardware and falls back to CPU if not present) or force a specific encoder profile (e.g., `Intel QuickSync`, `VA-API`, `NVIDIA NVENC`, `Apple VideoToolbox`).

5. **Start the server:**
   ```bash
   docker-compose up -d --build
   ```

6. **Access the application:**
   Open your browser and navigate to `http://localhost:8000`. The first user to register will automatically become the system admin.

## 📖 Usage

- **Non-Blocking Async Scanner & Real-Time Action Indicator**: Media indexing and file event processing run asynchronously in a dedicated worker thread with connection pooling (`httpx`), concurrent TMDB downloads via async semaphores, and async subprocess `ffprobe` chapter marker extraction. The primary admin sees a live glowing activity badge right next to the Server Dashboard icon on the dashboard and library headers (e.g., `Scanning (3/14)...` with pulsing indicator and detailed item tooltips, transitioning to a graceful `Scan Complete` badge), keeping admins informed without stalling the web server or blocking concurrent playback.
- **Inviting Users**: As an admin, navigate to the dashboard to generate one-time use invite codes for your family and friends.
- **Watch Together**: Start a session from any media page and share the session invite with another registered user.
- **Rewind on Resume**: Configure your preferred rewind buffer (e.g., 5s, 10s) in Settings so playback automatically skips back slightly when resuming interrupted movies or TV episodes, easing you back into context.
- **Smart Remuxing & Direct Stream**: Media files with web-compatible video streams (H.264/AVC) are repacked on-the-fly into fragmented MP4 streams via zero-overhead stream copy (`-c:v copy`), even across MKV/AVI containers and seeking/resuming. Only audio or incompatible video codecs/resolutions are transcoded, eliminating unnecessary CPU/GPU load while delivering instant playback and bit-for-bit picture quality.
- **Multi-Audio Track Selection**: Switch between multiple audio tracks (e.g., original language tracks, international dubs, director's commentary) on-the-fly directly from the player settings menu, with persistent per-title track memory and zero buffering loss.
- **Multi-Track Subtitles & Closed Captions**: Stream embedded text subtitles (SRT, WebVTT, ASS/SSA, MOV text) and external sidecar files dynamically extracted to WebVTT with keyframe-accurate seek synchronization. Toggle captions instantly with the dedicated player button or the `C` keyboard shortcut, select specific subtitle tracks directly from the player settings menu with persistent per-title memory, and close the player anytime with `Esc`.
- **Adaptive Bitrate (ABR) & HLS Streaming**: Enjoy buffer-free playback with native HTTP Live Streaming (HLS). Heirloom automatically generates master and variant VOD playlists, dynamically scaling stream bitrates (Original, 1080p, 720p, 480p, 360p) based on real-time client bandwidth and device capabilities. Select **Auto** for hands-free dynamic switching, or manually lock your preferred quality tier with instant, zero-reload level transitions. Completely self-hosted and offline-ready with bundled Hls.js and native iOS/Safari fallback.
- **Fast Profile Switching & Multi-User PINs**: Organize family and shared accounts into distinct household profiles (up to 6 per account). Each profile maintains its own watch history, in-progress resume states, watchlists, and personal rewind buffers. Secure individual profiles with 4–15 digit numeric PIN locks powered by bcrypt hashing and HMAC session authorization, tag profiles with Kids Mode to restrict media to family-friendly ratings (G and PG), and personalize profiles with vibrant color palettes and custom icon avatars. Switch between profiles seamlessly via the top navigation dropdown or the dedicated "Who's Watching?" hub with active `OWNER` and `KIDS` badges.
- **Mobile-First Responsive Interface**: Enjoy a streamlined, touch-optimized experience on smartphones and tablets featuring collapsible on-demand search, touch carousels, and thumb-friendly controls.
- **Real-Time Telemetry & Volume Monitoring**: Access live stream health, real-time playback timestamps, and mounted media storage metrics directly from the Server Dashboard with automated background telemetry.

## 🔮 Upcoming Improvements

- **Manual "Fix Match" & Metadata Editor**: Search alternative TMDB/IMDb candidates, resolve mismatched titles, and customize artwork directly from the web interface.
- **Real-Time HDR to SDR Tone Mapping**: Hardware-accelerated color space conversion (VA-API, NVENC, OpenCL) to prevent washed-out colors when streaming 4K HDR10 and Dolby Vision media to SDR displays.
- **Bitmap & Image Subtitle Burn-In**: Dynamic video filter pipeline to burn Blu-ray (PGS) and DVD (VobSub) bitmap subtitles directly into transcoding streams.
- **Curated Collections & Franchises**: Automatic and custom multi-title groupings (e.g., Marvel Cinematic Universe, Harry Potter) with unified collection views and playlists.
- **Multi-Version Coexistence**: Unified media cards that merge multiple file editions (e.g., 4K Remux + 1080p SDR) with automatic quality selection.
- **Granular Parental Controls & Rating Sliders**: Advanced content restriction thresholds (e.g., custom PG-13, R, TV-MA levels) and tag-based blocklists beyond standard Kids Mode.
- **Smart TV & Native Client Ecosystem**: Dedicated native client applications for Apple TV, Android TV / Google TV, Roku, and Fire TV.
- **Actor & Director Filmography Hubs**: Dedicated talent pages linking all matching movies and series across your local library.
- **Music Library & Audio Player**: FLAC/MP3/ALAC playback with ID3 tag parsing, album artwork, artist discographies, and playlists.
- **Mobile Offline Sync**: Transcode and download media to phones and tablets for offline airplane and travel playback.
- **Live TV & DVR Tuner Support**: Network tuner integration (HDHomeRun, M3U) with Electronic Program Guide (EPG) and scheduled recording.


## 🤝 Contributing

Contributions, issues, and feature requests are welcome! Feel free to check the [issues page](https://github.com/talonbrinkman/heirloom/issues).

## 📝 License

This project is [MIT](https://choosealicense.com/licenses/mit/) licensed.
