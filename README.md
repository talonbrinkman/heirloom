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
- Subtitle Bitmap Burn-In
- Adaptive Bitrate (ABR) & HLS Streaming
- Hardware-Accelerated Transcoding
- Multi-Version Coexistence & Instant Restart
- Instant "Fix Match" Resolution
- Music Library & Persistent Audio Player
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
     - /path/to/your/music:/media/music:ro
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

- **Low-Impact Background Scanning & Real-Time Action Indicator**: Media indexing and file event processing run asynchronously with reduced subprocess priority (`nice` on Linux/macOS and `BELOW_NORMAL_PRIORITY_CLASS` on Windows), cooperative async batch yielding, and intelligently debounced filesystem watchers to keep CPU usage low and streaming silky-smooth during large initial scans and ongoing library synchronization. The primary admin sees a live glowing activity badge right next to the Server Dashboard icon on the dashboard and library headers (e.g., `Scanning (3/14)...` with pulsing indicator and detailed item tooltips, transitioning to a graceful `Scan Complete` badge), keeping admins informed without stalling the web server or blocking concurrent playback.
- **Inviting Users**: As an admin, navigate to the dashboard to generate one-time use invite codes for your family and friends.
- **Watch Together**: Start a session from any media page and share the session invite with another registered user.
- **Rewind on Resume**: Configure your preferred rewind buffer (e.g., 5s, 10s) in Settings so playback automatically skips back slightly when resuming interrupted movies or TV episodes, easing you back into context.
- **Smart Remuxing & Direct Stream**: Media files with web-compatible video streams (H.264/AVC) are repacked on-the-fly into fragmented MP4 streams via zero-overhead stream copy (`-c:v copy`), even across MKV/AVI containers and seeking/resuming. Only audio or incompatible video codecs/resolutions are transcoded, eliminating unnecessary CPU/GPU load while delivering instant playback and bit-for-bit picture quality.
- **Multi-Audio Track Selection**: Switch between multiple audio tracks (e.g., original language tracks, international dubs, director's commentary) on-the-fly directly from the player settings menu, with persistent per-title track memory and zero buffering loss.
- **Multi-Track Subtitles & Bitmap Burn-In**: Stream embedded text subtitles (SRT, WebVTT, ASS/SSA, MOV text) and external sidecar files dynamically extracted to WebVTT with keyframe-accurate seek synchronization. For Blu-ray (PGS) and DVD (VobSub) bitmap subtitle formats incompatible with browser text rendering, Heirloom dynamically burns subtitles directly into the video transcode stream with full resolution scaling and ABR support. Toggle captions instantly with the dedicated player button or the `C` keyboard shortcut, select specific subtitle tracks directly from the player settings menu with persistent per-title memory, and close the player anytime with `Esc`.
- **Adaptive Bitrate (ABR) & HLS Streaming**: Enjoy buffer-free playback with native HTTP Live Streaming (HLS). Heirloom automatically generates master and variant VOD playlists, dynamically scaling stream bitrates (Original, 1080p, 720p, 480p, 360p) based on real-time client bandwidth and device capabilities. Select **Auto** for hands-free dynamic switching, or manually lock your preferred quality tier with instant, zero-reload level transitions. Completely self-hosted and offline-ready with bundled Hls.js and native iOS/Safari fallback.
- **Fast Profile Switching & Multi-User PINs**: Organize family and shared accounts into distinct household profiles (up to 6 per account). Each profile maintains its own watch history, in-progress resume states, watchlists, and personal rewind buffers. Secure individual profiles with 4–15 digit numeric PIN locks powered by bcrypt hashing and HMAC session authorization, tag profiles with Kids Mode to restrict media to family-friendly ratings (G and PG), and personalize profiles with vibrant color palettes and custom icon avatars. Switch between profiles seamlessly via the top navigation dropdown or the dedicated "Who's Watching?" hub with active `OWNER` and `KIDS` badges.
- **Mobile-First Responsive Interface**: Enjoy a streamlined, touch-optimized experience on smartphones and tablets featuring collapsible on-demand search, touch carousels, thumb-friendly controls, perfectly square responsive cards for photos and music, horizontal overflow-scrollable subnavigation for music views and playlists, and an adaptive two-row bottom audio player bar.
- **Real-Time Telemetry & Volume Monitoring**: Access live stream health, real-time playback timestamps, and mounted media storage metrics directly from the Server Dashboard with automated background telemetry.
- **Instant "Fix Match" Resolution**: Quickly resolve mismatched titles or incorrect metadata directly from the three-dots dropdown menu on any media card or detail view. Search TMDB with live, debounced inputs, preview matching candidates complete with poster thumbnails and plot overviews, and update movie or TV series metadata, backdrops, and season artwork with a single click.
- **Multi-Version Coexistence & Instant Restart**: Seamlessly organize multiple file editions (e.g., 4K Remux, 1080p SDR, 720p) of the same title under a unified media card. Switch editions with one click, retain watch progress across versions, and restart media anytime from the beginning with the dedicated "Restart" button.
- **Music Library, Persistent Audio Player & Clean Metadata**: High-fidelity audio playback (FLAC, MP3, ALAC, AAC, OGG, WAV) featuring file-first ID3 tag and Vorbis comment extraction (title, artist, album, year, genre, track & disc numbers, bitrate), automatic multi-disc album merging, featured-artist normalization without library fragmentation, automated silence and filler track filtering, embedded cover art extraction with local folder fallback, artist discographies, playlists, queue management (play next, add to queue, queue drawer), seamless persistent bottom player bar that follows you across page navigations, MediaSession API integration (OS media keys, lock screen controls), and out-of-the-box TheAudioDB artist bio and album art enrichment (using the built-in free tier key `123` with zero configuration required).

## 🔮 Upcoming Improvements

- **Real-Time HDR to SDR Tone Mapping**: Hardware-accelerated color space conversion (VA-API, NVENC, OpenCL) to prevent washed-out colors when streaming 4K HDR10 and Dolby Vision media to SDR displays.
- **Curated Collections & Franchises**: Automatic and custom multi-title groupings (e.g., Marvel Cinematic Universe, Harry Potter) with unified collection views and playlists.
- **Granular Parental Controls & Rating Sliders**: Advanced content restriction thresholds (e.g., custom PG-13, R, TV-MA levels) and tag-based blocklists beyond standard Kids Mode.
- **Smart TV & Native Client Ecosystem**: Dedicated native client applications for Apple TV, Android TV / Google TV, Roku, and Fire TV.
- **Actor & Director Filmography Hubs**: Dedicated talent pages linking all matching movies and series across your local library.
- **Mobile Offline Sync**: Transcode and download media to phones and tablets for offline airplane and travel playback.


## 🤝 Contributing

Contributions, issues, and feature requests are welcome! Feel free to check the [issues page](https://github.com/talonbrinkman/heirloom/issues).

## 📝 License

This project is [MIT](https://choosealicense.com/licenses/mit/) licensed.
