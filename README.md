# Heirloom

**Heirloom** is an open-source, high-performance media streaming server built fundamentally around **digital sovereignty and self-hosting**. In a world of fragmented subscription services, Heirloom is designed to give you absolute, uncompromising ownership over your personal library of movies, TV shows, and photos. It brings the premium, polished features of paid streaming platforms directly to your home server—completely for free.

By leveraging a lightweight, API-first architecture using Python and FastAPI, Heirloom empowers you to host your own Netflix-like experience without sacrificing privacy, control, or your wallet.

## 🚀 Premium Features, Zero Cost

- 100% Free & Open Source
- Watch Together
- Skip Intro & Credits
- Rewind on Resume
- Trickplay Scrubbing Thumbnail Previews
- Download Capability
- Automated Media Management
- Real-Time Stream Dashboard
- Fully Responsive Mobile & Tablet Web UI
- Profiles & Multi-User PINs
- Non-Blocking Async Scanner & Real-Time Action Indicator
- Role-Based Access Control
- Kids Mode & Content Filtering
- Stateful Playback & Watchlists
- Smart Remuxing & Smart Audio Codec Direct Streaming
- Multi-Audio Track Selection
- Multi-Track Subtitle Extraction (Embedded & Sidecar WebVTT)
- Subtitle Bitmap Burn-In
- Adaptive Bitrate (ABR) & HLS Streaming
- Hardware-Accelerated Transcoding
- Multi-Version Coexistence & Instant Restart
- Related Titles & Smart Media Discovery
- Actor & Director Filmography Hubs ("Person" Pages)
- Instant "Fix Match" Resolution
- Music Library & Persistent Audio Player
- The Loom (Private Tor v3 Friend-to-Friend Media Search & Sync)
- Cross-Platform REST API
- Frictionless Docker Deployment

## 🛠️ Tech Stack

- **Backend**: [Python 3](https://www.python.org/), [FastAPI](https://fastapi.tiangolo.com/)
- **Database**: [SQLite](https://www.sqlite.org/), [SQLModel](https://sqlmodel.tiangolo.com/) (SQLAlchemy & Pydantic)
- **Real-time & Federation**: WebSockets, Built-in Tor v3 Onion Services
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
     - /path/to/your/loom:/media/loom
   ```
   *(Your main media directories stay mounted read-only; `/media/loom` is a separate read-write directory for media downloaded from friends over The Loom, automatically scanned into your Movies, TV, Photos, and Music libraries.)*

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
   Open your browser and navigate to `http://localhost:42069`. The first user to register will automatically become the system admin.

## 📖 Usage

- **Library & Metadata**: Add media volume paths and optional TMDB / TheAudioDB keys in **Settings** to automatically scan movies, TV shows, photos, and music. Use **Fix Match** from any card's three-dots menu to correct mismatched titles.
- **Users & Profiles**: Admins can generate one-time invite codes and manage library/feature permissions in **Settings**. Each account supports up to 6 PIN-protected household profiles with Adult, Teen, and Child rating controls.
- **Playback & Streaming**: Stream directly in the browser with adaptive bitrate HLS, multi-audio track switching, embedded/sidecar subtitles (with bitmap burn-in support), HDR-to-SDR tone mapping, trickplay seek previews, and auto-detected intro/credits skip prompts.
- **Watch Together & Downloads**: Start synchronized Watch Together sessions from any movie or show page, or download media directly when enabled for your account.
- **Playback Preferences**: Configure rewind-on-resume, default audio/subtitle languages, "Are You Still Watching?" episode limits, and the in-player sleep timer in **Settings** or the player menu.
- **Music & Discovery**: Listen across pages with the persistent bottom audio player and queue drawer, explore actor/director filmography pages, and browse auto-grouped franchise collections and related titles.
- **The Loom (Friend-to-Friend Sync)**: Opt-in, disabled-by-default peer-to-peer media search and sync powered by a built-in Tor v3 daemon (no port forwarding or external Tor setup required). Pair friends with single-use 24-hour expiring `.onion` bundles in **Settings**, configure per-peer **Receive** / **Share** permissions, searchable volumes, allowed filetypes, bandwidth/quality/hop/concurrency/rate limits, and search friends' libraries directly from the Home search bar with live progress, speed, cancellation, and automatic resume for interrupted downloads.

## 🔮 Upcoming Improvements

- **Smart TV & Native Client Ecosystem**: Dedicated native client applications for Apple TV, Android TV / Google TV, Roku, and Fire TV.
- **Mobile Offline Sync**: Transcode and download media to phones and tablets for offline airplane and travel playback.
- **Seamless Continuous Music Streaming Across Pages**: Client-side PJAX/Turbo navigation keeping the persistent audio player and background music streaming uninterrupted across page transitions without audio pause or reload gaps.
- **On-Demand Subtitle Fetching**: In-player and background retrieval of missing subtitle tracks and languages via external provider integrations (OpenSubtitles / Bazarr).

## 🤝 Contributing

Contributions, issues, and feature requests are welcome! Feel free to check the [issues page](https://github.com/talonbrinkman/heirloom/issues).

## 📝 License

This project is [MIT](https://choosealicense.com/licenses/mit/) licensed.
