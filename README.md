# Heirloom

**Heirloom** is an open-source, high-performance media streaming server built fundamentally around **digital sovereignty and self-hosting**. In a world of fragmented subscription services, Heirloom is designed to give you absolute, uncompromising ownership over your personal library of movies, TV shows, and photos. It brings the premium, polished features of paid streaming platforms directly to your home server—completely for free.

By leveraging a lightweight, API-first architecture using Python and FastAPI, Heirloom empowers you to host your own Netflix-like experience without sacrificing privacy, control, or your wallet.

## 🚀 Premium Features, Zero Cost

- **100% Free & Open Source**: Full transparency and community-driven. No paywalls, no premium tiers, no data harvesting. You own your media, you own the server.
- **Premium "Watch Together"**: Real-time synchronized playback sessions across different clients using native WebSockets—a feature often gated behind premium subscriptions on other platforms.
- **Automated Media Management**: Background file watchers automatically scan, organize, and parse metadata for Movies, TV Shows, and Photos.
- **Role-Based Access Control**: Secure login system with bcrypt password hashing. Admins can generate secure, expiring invite codes and manage permissions for friends and family.
- **Kids Mode & Filtering**: Granular content filtering based on official content ratings (G, PG, TV-Y, etc.) to ensure a safe viewing environment.
- **Cross-Platform REST API**: A robust JSON API powering the web frontend, and ready for mobile or third-party client integrations.
- **Frictionless Deployment**: Fully containerized using Docker and Docker Compose for simple, reproducible deployments.
- **Stateful Playback**: Tracks watch history, progress, and user-specific watchlists seamlessly.
- **On-the-Fly Media Handling**: Integrates with FFmpeg for robust media processing and format compatibility.

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
   git clone https://github.com/yourusername/heirloom.git
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

4. **Start the server:**
   ```bash
   docker-compose up -d --build
   ```

5. **Access the application:**
   Open your browser and navigate to `http://localhost:8000`. The first user to register will automatically become the system admin.

## 📖 Usage

- **Library Scanning**: The server automatically scans mounted directories on startup and uses file system watchers to detect new content.
- **Inviting Users**: As an admin, navigate to the dashboard to generate one-time use invite codes for your family and friends.
- **Watch Together**: Start a session from any media page and share the session invite with another registered user to synchronize your viewing experience.

## 🤝 Contributing

Contributions, issues, and feature requests are welcome! Feel free to check the [issues page](https://github.com/yourusername/heirloom/issues).

## 📝 License

This project is [MIT](https://choosealicense.com/licenses/mit/) licensed.
