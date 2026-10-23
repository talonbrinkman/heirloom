/**
 * Heirloom Persistent Audio Player
 * Provides rich HTML5 audio playback, global persistent bottom bar,
 * queue management, playlist assignment, and MediaSession integration.
 */

(function () {
    class HeirloomAudioPlayer {
        constructor() {
            this.audio = new Audio();
            this.currentTrack = null;
            this.queue = [];
            this.queueIndex = -1;
            this.isShuffle = false;
            this.repeatMode = 'off'; // 'off' | 'all' | 'one'
            this.isSeeking = false;
            this.isQueueOpen = false;

            this.STORAGE_KEY_TRACK = 'heirloom_player_track';
            this.STORAGE_KEY_QUEUE = 'heirloom_player_queue';
            this.STORAGE_KEY_INDEX = 'heirloom_player_index';
            this.STORAGE_KEY_VOLUME = 'heirloom_player_volume';
            this.STORAGE_KEY_MUTED = 'heirloom_player_muted';
            this.STORAGE_KEY_SHUFFLE = 'heirloom_player_shuffle';
            this.STORAGE_KEY_REPEAT = 'heirloom_player_repeat';
            this.STORAGE_KEY_TIME = 'heirloom_player_time';
            this.STORAGE_KEY_DISMISSED = 'heirloom_player_dismissed';
            this.isDismissed = false;

            this.init();
        }

        init() {
            this.loadState();
            this.buildDOM();
            this.attachAudioEvents();
            this.setupMediaSession();
            this.restoreSession();
        }

        loadState() {
            try {
                this.isDismissed = localStorage.getItem(this.STORAGE_KEY_DISMISSED) === 'true';
                this.isShuffle = localStorage.getItem(this.STORAGE_KEY_SHUFFLE) === 'true';
                this.repeatMode = localStorage.getItem(this.STORAGE_KEY_REPEAT) || 'off';
                const savedVol = localStorage.getItem(this.STORAGE_KEY_VOLUME);
                this.audio.volume = savedVol !== null ? parseFloat(savedVol) : 1.0;
                this.audio.muted = localStorage.getItem(this.STORAGE_KEY_MUTED) === 'true';
                const savedQueue = localStorage.getItem(this.STORAGE_KEY_QUEUE);
                if (savedQueue) this.queue = JSON.parse(savedQueue);
                const savedIndex = localStorage.getItem(this.STORAGE_KEY_INDEX);
                if (savedIndex !== null) this.queueIndex = parseInt(savedIndex, 10);
                const savedTrack = localStorage.getItem(this.STORAGE_KEY_TRACK);
                if (savedTrack) this.currentTrack = JSON.parse(savedTrack);
            } catch (e) {
                console.warn('Error loading player state:', e);
            }
        }

        saveState() {
            try {
                if (this.currentTrack) {
                    localStorage.setItem(this.STORAGE_KEY_TRACK, JSON.stringify(this.currentTrack));
                } else {
                    localStorage.removeItem(this.STORAGE_KEY_TRACK);
                }
                localStorage.setItem(this.STORAGE_KEY_QUEUE, JSON.stringify(this.queue));
                localStorage.setItem(this.STORAGE_KEY_INDEX, this.queueIndex.toString());
                localStorage.setItem(this.STORAGE_KEY_VOLUME, this.audio.volume.toString());
                localStorage.setItem(this.STORAGE_KEY_MUTED, this.audio.muted.toString());
                localStorage.setItem(this.STORAGE_KEY_SHUFFLE, this.isShuffle.toString());
                localStorage.setItem(this.STORAGE_KEY_REPEAT, this.repeatMode);
                if (this.audio.currentTime) {
                    localStorage.setItem(this.STORAGE_KEY_TIME, this.audio.currentTime.toString());
                }
            } catch (e) {
                console.warn('Error saving player state:', e);
            }
        }

        buildDOM() {
            if (document.getElementById('heirloom-audio-player-container')) return;

            const container = document.createElement('div');
            container.id = 'heirloom-audio-player-container';
            container.className = 'heirloom-player-wrap';
            container.style.display = 'none';

            container.innerHTML = `
                <!-- Queue Drawer -->
                <div id="playerQueueDrawer" class="player-queue-drawer" style="display: none;">
                    <div class="queue-header">
                        <div class="flex align-center gap-10">
                            <h3 class="m-0">Queue</h3>
                        </div>
                        <div class="flex align-center gap-10">
                            <button type="button" class="btn btn-secondary btn-sm" id="playerClearQueueBtn" title="Clear Queue">Clear</button>
                        </div>
                    </div>
                    <div id="playerQueueList" class="queue-list"></div>
                </div>

                <!-- Bottom Floating Player Bar -->
                <div class="heirloom-player-bar">
                    <!-- Progress Container matching player.html -->
                    <div class="progress-container" id="playerProgressContainer">
                        <div class="progress-bar-fill" id="playerProgressBarFill"></div>
                        <div class="progress-hover-time" id="playerProgressHoverTime">0:00</div>
                    </div>

                    <div class="controls-row">
                        <!-- Left: Cover Thumb, Title, Artist, and Time Display -->
                        <div class="controls-left">
                            <div class="player-thumb-wrap" id="playerThumbWrap">
                                <img id="playerCoverImg" src="" alt="Album Art" class="player-cover-img" style="display: none;">
                                <div id="playerCoverPlaceholder" class="player-cover-placeholder">
                                    <i data-lucide="disc"></i>
                                </div>
                            </div>
                            <div class="player-track-info">
                                <a href="#" id="playerTrackTitle" class="player-track-title" title="">Not Playing</a>
                                <div class="player-meta-sub">
                                    <a href="#" id="playerArtistName" class="player-artist-name" title="">Unknown Artist</a>
                                </div>
                            </div>
                            <div class="time-display" id="playerTimeDisplay">0:00 / 0:00</div>
                        </div>

                        <!-- Center: Shuffle, Skip Back, Play/Pause Large, Skip Forward, Repeat -->
                        <div class="controls-center">
                            <button type="button" class="control-btn" id="playerShuffleBtn" title="Shuffle">
                                <i data-lucide="shuffle"></i>
                            </button>
                            <button type="button" class="control-btn" id="playerPrevBtn" title="Previous Track">
                                <i data-lucide="skip-back"></i>
                            </button>
                            <button type="button" class="control-btn play-btn-large" id="playerPlayPauseBtn" title="Play">
                                <i data-lucide="play" id="playerPlayPauseIcon" fill="currentColor"></i>
                            </button>
                            <button type="button" class="control-btn" id="playerNextBtn" title="Next Track">
                                <i data-lucide="skip-forward"></i>
                            </button>
                            <button type="button" class="control-btn" id="playerRepeatBtn" title="Repeat: Off">
                                <i data-lucide="repeat" id="playerRepeatIcon"></i>
                            </button>
                        </div>

                        <!-- Right: Volume, Queue, and Close Player -->
                        <div class="controls-right">
                            <div class="volume-container flex align-center" style="gap: 5px;">
                                <button type="button" class="control-btn" id="playerVolumeBtn" title="Mute/Unmute">
                                    <i data-lucide="volume-2" id="playerVolumeIcon"></i>
                                </button>
                                <input type="range" id="playerVolumeSlider" class="volume-slider" min="0" max="1" step="0.01" value="1">
                            </div>
                            <button type="button" class="control-btn" id="playerQueueBtn" title="Queue">
                                <i data-lucide="list-music"></i>
                            </button>
                            <button type="button" class="control-btn" id="playerDismissBtn" title="Close Player">
                                <i data-lucide="x"></i>
                            </button>
                        </div>
                    </div>
                </div>
            `;

            document.body.appendChild(container);
            this.container = container;
            this.bindUI();
            if (window.lucide) window.lucide.createIcons();
        }

        bindUI() {
            const playPauseBtn = document.getElementById('playerPlayPauseBtn');
            const prevBtn = document.getElementById('playerPrevBtn');
            const nextBtn = document.getElementById('playerNextBtn');
            const shuffleBtn = document.getElementById('playerShuffleBtn');
            const repeatBtn = document.getElementById('playerRepeatBtn');
            const progressContainer = document.getElementById('playerProgressContainer');
            const progressFill = document.getElementById('playerProgressBarFill');
            const progressHoverTime = document.getElementById('playerProgressHoverTime');
            const volBtn = document.getElementById('playerVolumeBtn');
            const volSlider = document.getElementById('playerVolumeSlider');
            const queueBtn = document.getElementById('playerQueueBtn');
            const closeQueueBtn = document.getElementById('playerCloseQueueBtn');
            const clearQueueBtn = document.getElementById('playerClearQueueBtn');
            const dismissBtn = document.getElementById('playerDismissBtn');

            playPauseBtn?.addEventListener('click', () => this.togglePlay());
            prevBtn?.addEventListener('click', () => this.prev());
            nextBtn?.addEventListener('click', () => this.next());
            shuffleBtn?.addEventListener('click', () => this.toggleShuffle());
            repeatBtn?.addEventListener('click', () => this.cycleRepeat());

            let isProgressDragging = false;

            if (progressContainer) {
                progressContainer.addEventListener('mousemove', (e) => {
                    const dur = this.audio.duration;
                    if (!dur || !isFinite(dur) || dur <= 0) return;
                    const rect = progressContainer.getBoundingClientRect();
                    const pct = Math.max(0, Math.min(1, (e.clientX - rect.left) / rect.width));
                    if (progressHoverTime) {
                        progressHoverTime.textContent = this.formatTime(pct * dur);
                        progressHoverTime.style.left = `${pct * 100}%`;
                    }
                });

                const seekAtEvent = (e) => {
                    const dur = this.audio.duration;
                    if (!dur || !isFinite(dur) || dur <= 0) return;
                    const rect = progressContainer.getBoundingClientRect();
                    const pct = Math.max(0, Math.min(1, (e.clientX - rect.left) / rect.width));
                    if (progressFill) progressFill.style.width = `${pct * 100}%`;
                    const seekTo = pct * dur;
                    this.audio.currentTime = seekTo;
                    const timeEl = document.getElementById('playerTimeDisplay');
                    if (timeEl) {
                        timeEl.textContent = `${this.formatTime(seekTo)} / ${this.formatTime(dur)}`;
                    }
                    this.saveState();
                };

                progressContainer.addEventListener('click', (e) => {
                    seekAtEvent(e);
                });

                progressContainer.addEventListener('mousedown', (e) => {
                    isProgressDragging = true;
                    this.isSeeking = true;
                    seekAtEvent(e);
                });

                window.addEventListener('mousemove', (e) => {
                    if (isProgressDragging) {
                        seekAtEvent(e);
                    }
                });

                window.addEventListener('mouseup', () => {
                    if (isProgressDragging) {
                        isProgressDragging = false;
                        this.isSeeking = false;
                    }
                });
            }

            volBtn?.addEventListener('click', () => {
                this.audio.muted = !this.audio.muted;
                this.updateVolumeUI();
                this.saveState();
            });

            volSlider?.addEventListener('input', (e) => {
                this.audio.volume = parseFloat(e.target.value);
                this.audio.muted = false;
                this.updateVolumeUI();
                this.saveState();
            });

            queueBtn?.addEventListener('click', () => this.toggleQueueDrawer());
            closeQueueBtn?.addEventListener('click', () => this.toggleQueueDrawer(false));
            clearQueueBtn?.addEventListener('click', () => this.clearQueue());
            dismissBtn?.addEventListener('click', () => this.dismissPlayer());

            this.updateShuffleUI();
            this.updateRepeatUI();
            this.updateVolumeUI();
        }

        attachAudioEvents() {
            this.audio.addEventListener('timeupdate', () => {
                if (this.isSeeking) return;
                const cur = this.audio.currentTime || 0;
                const dur = this.audio.duration || 0;
                const fillEl = document.getElementById('playerProgressBarFill');
                const timeEl = document.getElementById('playerTimeDisplay');

                if (dur > 0) {
                    const pct = (cur / dur) * 100;
                    if (fillEl) fillEl.style.width = `${pct}%`;
                    if (timeEl) timeEl.textContent = `${this.formatTime(cur)} / ${this.formatTime(dur)}`;
                } else {
                    if (fillEl) fillEl.style.width = '0%';
                    if (timeEl) timeEl.textContent = `${this.formatTime(cur)} / 0:00`;
                }
            });

            this.audio.addEventListener('play', () => {
                this.updatePlayPauseIcon(true);
                this.saveState();
            });

            this.audio.addEventListener('pause', () => {
                this.updatePlayPauseIcon(false);
                this.saveState();
            });

            this.audio.addEventListener('ended', () => {
                if (this.repeatMode === 'one') {
                    this.audio.currentTime = 0;
                    this.audio.play().catch(console.error);
                } else {
                    this.next(false);
                }
            });

            this.audio.addEventListener('loadedmetadata', () => {
                const cur = this.audio.currentTime || 0;
                const dur = this.audio.duration || 0;
                const timeEl = document.getElementById('playerTimeDisplay');
                if (timeEl && dur > 0) {
                    timeEl.textContent = `${this.formatTime(cur)} / ${this.formatTime(dur)}`;
                }
            });

            this.audio.addEventListener('error', (e) => {
                console.error('Audio stream error:', e, this.audio.error);
            });
        }

        setupMediaSession() {
            if ('mediaSession' in navigator) {
                navigator.mediaSession.setActionHandler('play', () => this.play());
                navigator.mediaSession.setActionHandler('pause', () => this.pause());
                navigator.mediaSession.setActionHandler('previoustrack', () => this.prev());
                navigator.mediaSession.setActionHandler('nexttrack', () => this.next());
                navigator.mediaSession.setActionHandler('seekto', (details) => {
                    if (details.seekTime !== undefined) {
                        this.audio.currentTime = details.seekTime;
                        this.saveState();
                    }
                });
            }
        }

        updateMediaSession() {
            if (!('mediaSession' in navigator) || !this.currentTrack) return;
            const artwork = [];
            if (this.currentTrack.cover_url) {
                artwork.push({
                    src: window.location.origin + this.currentTrack.cover_url,
                    sizes: '512x512',
                    type: 'image/jpeg'
                });
            }
            navigator.mediaSession.metadata = new MediaMetadata({
                title: this.currentTrack.title || 'Unknown Track',
                artist: this.currentTrack.artist_name || 'Unknown Artist',
                album: this.currentTrack.album_title || 'Unknown Album',
                artwork: artwork
            });
        }

        restoreSession() {
            if (this.isDismissed) {
                return;
            }
            if (this.currentTrack) {
                this.showPlayer();
                this.renderTrackInfo(this.currentTrack);
                this.audio.src = this.currentTrack.stream_url || `/stream/music/${this.currentTrack.id}`;
                const savedTime = localStorage.getItem(this.STORAGE_KEY_TIME);
                if (savedTime) {
                    this.audio.currentTime = parseFloat(savedTime);
                }
                this.renderQueue();
            }
        }

        showPlayer() {
            this.isDismissed = false;
            try {
                localStorage.removeItem(this.STORAGE_KEY_DISMISSED);
            } catch (e) {}
            if (this.container) {
                this.container.style.display = 'block';
                document.body.classList.add('has-active-player');
            }
        }

        dismissPlayer() {
            this.pause();
            this.isDismissed = true;
            try {
                localStorage.setItem(this.STORAGE_KEY_DISMISSED, 'true');
            } catch (e) {}
            if (this.container) {
                this.container.style.display = 'none';
                document.body.classList.remove('has-active-player');
            }
        }

        playTrack(track, queue = null, index = -1, autoPlay = true) {
            if (!track) return;
            this.showPlayer();

            if (queue && Array.isArray(queue) && queue.length > 0) {
                this.queue = queue;
                this.queueIndex = index >= 0 ? index : this.queue.findIndex(t => t.id === track.id);
            } else if (this.queue.length === 0) {
                this.queue = [track];
                this.queueIndex = 0;
            } else if (index >= 0) {
                this.queueIndex = index;
            } else {
                const existingIdx = this.queue.findIndex(t => t.id === track.id);
                if (existingIdx >= 0) {
                    this.queueIndex = existingIdx;
                } else {
                    this.queue.push(track);
                    this.queueIndex = this.queue.length - 1;
                }
            }

            this.currentTrack = track;
            this.renderTrackInfo(track);
            this.renderQueue();

            const streamUrl = track.stream_url || `/stream/music/${track.id}`;
            this.audio.src = streamUrl;
            this.audio.currentTime = 0;

            if (autoPlay) {
                this.audio.play().catch(err => {
                    console.warn('Autoplay blocked or stream error:', err);
                });
            }

            this.updateMediaSession();
            this.saveState();
        }

        renderTrackInfo(track) {
            const titleEl = document.getElementById('playerTrackTitle');
            const artistEl = document.getElementById('playerArtistName');
            const coverImg = document.getElementById('playerCoverImg');
            const placeholder = document.getElementById('playerCoverPlaceholder');
            const codecBadge = document.getElementById('playerCodecBadge');

            if (titleEl) {
                titleEl.textContent = track.title || 'Unknown Title';
                titleEl.title = track.title || '';
                titleEl.href = track.album_id ? `/album/${track.album_id}` : '#';
            }

            if (artistEl) {
                artistEl.textContent = track.artist_name || 'Unknown Artist';
                artistEl.title = track.artist_name || '';
                artistEl.href = track.artist_id ? `/artist/${track.artist_id}` : '#';
            }

            if (coverImg && placeholder) {
                if (track.cover_url) {
                    coverImg.src = track.cover_url;
                    coverImg.style.display = 'block';
                    placeholder.style.display = 'none';
                } else {
                    coverImg.style.display = 'none';
                    placeholder.style.display = 'flex';
                }
            }

            if (codecBadge) {
                if (track.codec) {
                    codecBadge.textContent = track.codec;
                    codecBadge.style.display = 'inline-block';
                } else {
                    codecBadge.style.display = 'none';
                }
            }
        }

        renderQueue() {
            const listEl = document.getElementById('playerQueueList');
            if (!listEl) return;

            if (this.queue.length === 0) {
                listEl.innerHTML = '<div class="queue-empty">Queue is empty</div>';
                return;
            }

            listEl.innerHTML = this.queue.map((t, idx) => {
                const isCurrent = idx === this.queueIndex;
                const durStr = t.duration_str || (t.duration ? this.formatTime(t.duration) : '');
                return `
                    <div class="queue-item ${isCurrent ? 'active' : ''}" data-index="${idx}">
                        <div class="queue-item-left">
                            <div class="queue-thumb">
                                ${t.cover_url ? `<img src="${t.cover_url}" alt="">` : `<i data-lucide="disc"></i>`}
                            </div>
                            <div class="queue-info">
                                <span class="queue-title">${this.escapeHtml(t.title || 'Unknown')}</span>
                                <span class="queue-artist">${this.escapeHtml(t.artist_name || 'Unknown')}</span>
                            </div>
                        </div>
                        <div class="queue-item-right">
                            <span class="queue-dur">${durStr}</span>
                            <button type="button" class="queue-remove-btn" data-index="${idx}" title="Remove">
                                <i data-lucide="x"></i>
                            </button>
                        </div>
                    </div>
                `;
            }).join('');

            if (window.lucide) window.lucide.createIcons();

            listEl.querySelectorAll('.queue-item').forEach(item => {
                item.addEventListener('click', (e) => {
                    if (e.target.closest('.queue-remove-btn')) return;
                    const idx = parseInt(item.getAttribute('data-index'), 10);
                    if (idx >= 0 && idx < this.queue.length) {
                        this.playTrack(this.queue[idx], this.queue, idx, true);
                    }
                });
            });

            listEl.querySelectorAll('.queue-remove-btn').forEach(btn => {
                btn.addEventListener('click', (e) => {
                    e.stopPropagation();
                    const idx = parseInt(btn.getAttribute('data-index'), 10);
                    this.removeFromQueue(idx);
                });
            });
        }

        removeFromQueue(index) {
            if (index < 0 || index >= this.queue.length) return;
            this.queue.splice(index, 1);
            if (index < this.queueIndex) {
                this.queueIndex--;
            } else if (index === this.queueIndex) {
                if (this.queue.length === 0) {
                    this.queueIndex = -1;
                    this.currentTrack = null;
                    this.audio.pause();
                    this.audio.src = '';
                    this.renderTrackInfo({ title: 'Not Playing', artist_name: '' });
                } else {
                    const nextIdx = this.queueIndex < this.queue.length ? this.queueIndex : 0;
                    this.playTrack(this.queue[nextIdx], this.queue, nextIdx, true);
                }
            }
            this.renderQueue();
            this.saveState();
        }

        clearQueue() {
            this.queue = [];
            this.queueIndex = -1;
            this.renderQueue();
            this.saveState();
        }

        toggleQueueDrawer(forceState = null) {
            const drawer = document.getElementById('playerQueueDrawer');
            if (!drawer) return;
            this.isQueueOpen = forceState !== null ? forceState : (drawer.style.display === 'none');
            drawer.style.display = this.isQueueOpen ? 'flex' : 'none';
            const btn = document.getElementById('playerQueueBtn');
            if (btn) btn.classList.toggle('active', this.isQueueOpen);
            if (this.isQueueOpen) this.renderQueue();
        }

        play() {
            if (this.audio.src) {
                this.audio.play().catch(console.error);
            } else if (this.queue.length > 0) {
                this.playTrack(this.queue[0], this.queue, 0, true);
            }
        }

        pause() {
            this.audio.pause();
        }

        togglePlay() {
            if (this.audio.paused) {
                this.play();
            } else {
                this.pause();
            }
        }

        next(userInitiated = true) {
            if (this.queue.length === 0) return;

            let nextIdx = this.queueIndex + 1;
            if (this.isShuffle && this.queue.length > 1) {
                let randomIdx = Math.floor(Math.random() * this.queue.length);
                if (randomIdx === this.queueIndex) {
                    randomIdx = (randomIdx + 1) % this.queue.length;
                }
                nextIdx = randomIdx;
            }

            if (nextIdx >= this.queue.length) {
                if (this.repeatMode === 'all') {
                    nextIdx = 0;
                } else {
                    if (userInitiated) {
                        nextIdx = 0;
                    } else {
                        return;
                    }
                }
            }

            this.playTrack(this.queue[nextIdx], this.queue, nextIdx, true);
        }

        prev() {
            if (this.audio.currentTime > 3.0) {
                this.audio.currentTime = 0;
                return;
            }
            if (this.queue.length === 0) return;
            let prevIdx = this.queueIndex - 1;
            if (prevIdx < 0) {
                prevIdx = this.repeatMode === 'all' ? this.queue.length - 1 : 0;
            }
            this.playTrack(this.queue[prevIdx], this.queue, prevIdx, true);
        }

        toggleShuffle() {
            this.isShuffle = !this.isShuffle;
            this.updateShuffleUI();
            this.saveState();
        }

        updateShuffleUI() {
            const btn = document.getElementById('playerShuffleBtn');
            if (btn) btn.classList.toggle('active', this.isShuffle);
        }

        cycleRepeat() {
            if (this.repeatMode === 'off') {
                this.repeatMode = 'all';
            } else if (this.repeatMode === 'all') {
                this.repeatMode = 'one';
            } else {
                this.repeatMode = 'off';
            }
            this.updateRepeatUI();
            this.saveState();
        }

        updateRepeatUI() {
            const btn = document.getElementById('playerRepeatBtn');
            const icon = document.getElementById('playerRepeatIcon');
            if (!btn) return;

            btn.classList.toggle('active', this.repeatMode !== 'off');
            if (this.repeatMode === 'one') {
                btn.title = 'Repeat: One Track';
                btn.setAttribute('data-repeat', 'one');
                if (icon) icon.setAttribute('data-lucide', 'repeat-1');
            } else if (this.repeatMode === 'all') {
                btn.title = 'Repeat: All Tracks';
                btn.removeAttribute('data-repeat');
                if (icon) icon.setAttribute('data-lucide', 'repeat');
            } else {
                btn.title = 'Repeat: Off';
                btn.removeAttribute('data-repeat');
                if (icon) icon.setAttribute('data-lucide', 'repeat');
            }
            if (window.lucide) window.lucide.createIcons();
        }

        updateVolumeUI() {
            const slider = document.getElementById('playerVolumeSlider');
            const icon = document.getElementById('playerVolumeIcon');
            if (!slider || !icon) return;

            const vol = this.audio.muted ? 0 : this.audio.volume;
            slider.value = vol;

            let iconName = 'volume-2';
            if (this.audio.muted || vol === 0) {
                iconName = 'volume-x';
            } else if (vol < 0.5) {
                iconName = 'volume-1';
            }
            icon.setAttribute('data-lucide', iconName);
            if (window.lucide) window.lucide.createIcons();
        }

        updatePlayPauseIcon(isPlaying) {
            const icon = document.getElementById('playerPlayPauseIcon');
            const btn = document.getElementById('playerPlayPauseBtn');
            if (icon) {
                icon.setAttribute('data-lucide', isPlaying ? 'pause' : 'play');
                if (isPlaying) {
                    icon.removeAttribute('fill');
                } else {
                    icon.setAttribute('fill', 'currentColor');
                }
                if (window.lucide) window.lucide.createIcons();
            }
            if (btn) {
                btn.title = isPlaying ? 'Pause' : 'Play';
            }
        }

        formatTime(seconds) {
            if (isNaN(seconds) || seconds < 0) return '0:00';
            const m = Math.floor(seconds / 60);
            const s = Math.floor(seconds % 60);
            return `${m}:${s < 10 ? '0' : ''}${s}`;
        }

        escapeHtml(text) {
            const div = document.createElement('div');
            div.textContent = text;
            return div.innerHTML;
        }

        // --- High-level Playback APIs ---

        async playAlbum(albumId, options = {}) {
            try {
                const res = await fetch(`/api/music/album/${albumId}/tracks`);
                if (!res.ok) throw new Error('Failed to load album tracks');
                const data = await res.json();
                const tracks = data.tracks || [];
                if (tracks.length === 0) {
                    alert('No tracks found for this album.');
                    return;
                }
                const startIndex = options.trackId
                    ? Math.max(0, tracks.findIndex(t => t.id === options.trackId))
                    : 0;

                if (options.shuffle) {
                    this.isShuffle = true;
                    this.updateShuffleUI();
                }

                this.playTrack(tracks[startIndex], tracks, startIndex, true);
            } catch (err) {
                console.error('Error playing album:', err);
            }
        }

        async playArtist(artistId, options = {}) {
            try {
                const res = await fetch(`/api/music/artist/${artistId}/tracks`);
                if (!res.ok) throw new Error('Failed to load artist tracks');
                const tracks = await res.json();
                if (!tracks || tracks.length === 0) {
                    alert('No tracks found for this artist.');
                    return;
                }
                if (options.shuffle) {
                    this.isShuffle = true;
                    this.updateShuffleUI();
                }
                this.playTrack(tracks[0], tracks, 0, true);
            } catch (err) {
                console.error('Error playing artist:', err);
            }
        }

        async playPlaylist(playlistId, options = {}) {
            try {
                const res = await fetch(`/api/playlists/${playlistId}`);
                if (!res.ok) throw new Error('Failed to load playlist');
                const data = await res.json();
                const tracks = data.tracks || [];
                if (tracks.length === 0) {
                    alert('This playlist is empty.');
                    return;
                }
                const startIndex = options.trackId
                    ? Math.max(0, tracks.findIndex(t => t.id === options.trackId))
                    : 0;

                if (options.shuffle) {
                    this.isShuffle = true;
                    this.updateShuffleUI();
                }

                this.playTrack(tracks[startIndex], tracks, startIndex, true);
            } catch (err) {
                console.error('Error playing playlist:', err);
            }
        }

        async addToQueue(trackOrId) {
            this.showPlayer();
            let track = trackOrId;
            if (typeof trackOrId === 'number' || typeof trackOrId === 'string') {
                try {
                    const res = await fetch(`/api/music/track/${trackOrId}`);
                    if (res.ok) {
                        track = await res.json();
                    } else {
                        return;
                    }
                } catch (e) {
                    console.error('Error fetching track for queue:', e);
                    return;
                }
            }
            if (!track) return;
            this.queue.push(track);
            if (!this.currentTrack || this.queueIndex === -1) {
                this.queueIndex = 0;
                this.currentTrack = track;
                this.renderTrackInfo(track);
                this.audio.src = track.stream_url || `/stream/music/${track.id}`;
            }
            this.renderQueue();
            this.saveState();
        }

        playNextInQueue(track) {
            this.showPlayer();
            const insertIdx = this.queueIndex + 1;
            this.queue.splice(insertIdx, 0, track);
            this.renderQueue();
            this.saveState();
        }
    }

    // Modal: Add to Playlist dialog
    window._addToPlaylistCurrentTrackId = null;

    window.openAddToPlaylistModal = async function (trackId, trackTitle = '') {
        window._addToPlaylistCurrentTrackId = trackId;

        let modal = document.getElementById('addToPlaylistModal');
        if (!modal) {
            modal = document.createElement('div');
            modal.id = 'addToPlaylistModal';
            modal.className = 'modal-overlay';
            modal.onclick = (e) => {
                if (e.target === modal) window.closeAddToPlaylistModal();
            };
            modal.innerHTML = `
                <div class="modal-content">
                    <div class="card-header mb-10">
                        <div class="flex align-center gap-10">
                            <h2 class="m-0" style="font-size: 1.25rem;">Add to Playlist</h2>
                        </div>
                        <button type="button" class="btn btn-sm btn-secondary" onclick="closeAddToPlaylistModal()">Close</button>
                    </div>
                    <p id="addToPlaylistSubtitle" class="m-0 mb-15" style="color: var(--text-muted); font-size: 0.9rem;"></p>
                    <div id="addToPlaylistList" class="playlist-modal-list">Loading playlists...</div>
                    <div class="flex gap-10 mt-15">
                        <input type="text" id="newPlaylistModalInput" placeholder="New playlist name..." autocomplete="off" class="flex-1 m-0" onkeydown="if(event.key === 'Enter') createNewPlaylistFromModal()">
                        <button type="button" class="btn btn-primary" id="createPlaylistModalBtn" onclick="createNewPlaylistFromModal()">
                            <i data-lucide="plus"></i> Create & Add
                        </button>
                    </div>
                </div>
            `;
            document.body.appendChild(modal);
        }

        modal.style.display = 'flex';
        const sub = document.getElementById('addToPlaylistSubtitle');
        if (sub) {
            const displayTitle = trackTitle ? decodeURIComponent(trackTitle) : '';
            sub.textContent = displayTitle ? `${displayTitle}` : 'Select playlist:';
        }

        const input = document.getElementById('newPlaylistModalInput');
        if (input) input.value = '';

        const list = document.getElementById('addToPlaylistList');
        try {
            const res = await fetch('/api/playlists');
            const playlists = await res.json();
            if (!playlists || playlists.length === 0) {
                list.innerHTML = '<div class="empty-state" style="padding: 20px; text-align: center; color: var(--text-muted); font-size: 0.9rem;">No playlists yet. Create one below!</div>';
            } else {
                list.innerHTML = playlists.map(pl => `
                    <button type="button" class="playlist-modal-item" onclick="addTrackToPlaylist(${pl.id})">
                        <div class="flex align-center gap-10">
                            <i data-lucide="list-music"></i>
                            <span>${pl.name}</span>
                        </div>
                        <span class="playlist-modal-count">${pl.track_count} tracks</span>
                    </button>
                `).join('');
            }
            if (window.lucide) window.lucide.createIcons();
        } catch (e) {
            list.innerHTML = '<div class="text-danger" style="padding: 10px; font-size: 0.9rem;">Failed to load playlists.</div>';
        }
    };

    window.closeAddToPlaylistModal = function () {
        const modal = document.getElementById('addToPlaylistModal');
        if (modal) modal.style.display = 'none';
    };

    window.addTrackToPlaylist = async function (playlistId, trackId = null) {
        const targetTrackId = trackId || window._addToPlaylistCurrentTrackId;
        if (!targetTrackId) return;
        try {
            const res = await fetch(`/api/playlists/${playlistId}/tracks`, {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ track_id: targetTrackId })
            });
            const data = await res.json();
            if (data.success) {
                window.closeAddToPlaylistModal();
            } else {
                alert('Could not add to playlist.');
            }
        } catch (err) {
            console.error(err);
            alert('Error adding track to playlist.');
        }
    };

    window.createNewPlaylistFromModal = async function (trackId = null) {
        const targetTrackId = trackId || window._addToPlaylistCurrentTrackId;
        const input = document.getElementById('newPlaylistModalInput');
        const name = input ? input.value.trim() : '';
        if (!name) return;
        try {
            const res = await fetch('/api/playlists', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ name: name })
            });
            const data = await res.json();
            if (data.success && data.playlist) {
                await window.addTrackToPlaylist(data.playlist.id, targetTrackId);
                if (input) input.value = '';
            }
        } catch (err) {
            console.error(err);
        }
    };

    // Instantiate and expose globally
    window.addEventListener('DOMContentLoaded', () => {
        if (!window.HeirloomPlayer) {
            window.HeirloomPlayer = new HeirloomAudioPlayer();
        }
    });

    if (document.readyState === 'complete' || document.readyState === 'interactive') {
        if (!window.HeirloomPlayer) {
            window.HeirloomPlayer = new HeirloomAudioPlayer();
        }
    }
})();
