# Heirloom

**Heirloom** is an open-source, high-performance media streaming server built fundamentally around **digital sovereignty and self-hosting**. In a world of fragmented subscription services, Heirloom is designed to give you absolute, uncompromising ownership over your personal library of movies, TV shows, and photos. It brings the premium, polished features of paid streaming platforms directly to your home server—completely for free.

By leveraging a lightweight, API-first architecture using Python and FastAPI, Heirloom empowers you to host your own Netflix-like experience without sacrificing privacy, control, or your wallet.

## 🚀 Premium Features, Zero Cost

- 100% Free & Open Source
- Watch Together
- Skip Intro & Credits
- Download Capability
- Automated Media Management
- Role-Based Access Control
- Kids Mode & Content Filtering
- Stateful Playback & Watchlists
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
   TZ=America/New_York
   ```

3. **Mount your media:**
   Edit the `docker-compose.yml` file to point the volume mounts to your local media directories:
   ```yaml
   volumes:
     - ./config:/config
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

- **Library Scanning**: The server automatically scans mounted directories on startup and uses file system watchers to detect new content.
- **Inviting Users**: As an admin, navigate to the dashboard to generate one-time use invite codes for your family and friends.
- **Watch Together**: Start a session from any media page and share the session invite with another registered user. Heirloom uses real-time WebSockets with **rolling latency compensation** (adapted from the Cinematic Break pilot system) to keep playback in near-perfect lip sync across varying network conditions.

## 🔮 Upcoming Improvements

- Rewind on Resume
- Smart Remuxing & Direct Stream Optimization
- Multi-Audio Track Selection
- Embedded Subtitle Extraction & Subtitle Search
- Adaptive Bitrate & HLS Streaming
- Trailers & Extras
- Fast Profile Switching & PIN Protection
- Real-Time Stream Dashboard
- Non-Blocking Async Scanner
- Actor & Director Filmography Hubs
- Smart Collections & Recommendations
- Music Library & Audio Player
- Mobile Offline Sync

## 🤝 Contributing

Contributions, issues, and feature requests are welcome! Feel free to check the [issues page](https://github.com/talonbrinkman/heirloom/issues).

## 📝 License

This project is [MIT](https://choosealicense.com/licenses/mit/) licensed.
