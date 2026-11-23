/**
 * Heirloom Persistent App Shell
 * Shared UI helpers mounted once outside #pjax-root so PJAX swaps never duplicate timers or listeners:
 * Inbox, Mobile Menu, Profile Switcher, Card Dropdown Menus, Carousels, Watch Together, and Admin Activity Badge.
 */
(function () {
    if (window.HeirloomShell) return;

    function escapeHtml(str) {
        return String(str ?? '')
            .replace(/&/g, '&amp;')
            .replace(/</g, '&lt;')
            .replace(/>/g, '&gt;')
            .replace(/"/g, '&quot;')
            .replace(/'/g, '&#39;');
    }

    // ------------------------------------------------------------------
    // Inbox & Loom Transfer Progress
    // ------------------------------------------------------------------
    function formatLoomGB(bytes) {
        const gb = Number(bytes || 0) / (1024 * 1024 * 1024);
        if (!Number.isFinite(gb) || gb <= 0) return '0.00 GB';
        return `${gb.toFixed(2)} GB`;
    }

    function formatLoomDetailsLine(resolution, bytes, hops, peerName) {
        const resText = escapeHtml(resolution || 'Unknown');
        const sizeText = formatLoomGB(bytes);
        const h = Number(hops || 0);
        const hopText = `${h} hop${h === 1 ? '' : 's'}`;
        const peerText = escapeHtml(peerName || 'Anonymous');
        return `${resText} • ${sizeText} • ${hopText} • ${peerText}`;
    }

    function formatInboxDownloadStatus(status) {
        if (status === 'downloading') return 'Downloading';
        if (status === 'uploading') return 'Uploading';
        if (status === 'canceled_by_sender') return 'Canceled by Sender';
        if (status === 'queued') return 'Queued';
        if (status === 'paused_stream') return 'Paused (Streaming)';
        if (status === 'paused_schedule' || status === 'paused_cap') return 'Paused';
        return status || 'Active';
    }

    function formatInboxDownloadSpeed(bps) {
        const val = Number(bps || 0);
        if (val <= 0) return '';
        if (val >= 1024 * 1024 * 1024) return `${(val / (1024 * 1024 * 1024)).toFixed(2)} GB/s`;
        if (val >= 1024 * 1024) return `${(val / (1024 * 1024)).toFixed(1)} MB/s`;
        return `${Math.max(1, Math.round(val / 1024))} KB/s`;
    }

    function formatInboxDownloadEta(transferred, total, bps) {
        const speed = Number(bps || 0);
        const tot = Number(total || 0);
        const done = Number(transferred || 0);
        if (speed <= 0 || tot <= 0 || done >= tot) return '';
        const secs = Math.ceil((tot - done) / speed);
        if (secs < 60) return `${secs}s remaining`;
        if (secs < 3600) return `${Math.ceil(secs / 60)} min remaining`;
        const hrs = Math.floor(secs / 3600);
        const mins = Math.ceil((secs % 3600) / 60);
        return mins > 0 ? `${hrs}h ${mins}m remaining` : `${hrs}h remaining`;
    }

    function renderInboxLoomDownload(j) {
        let titleText = j.title || 'Untitled';
        let subtitleText = '';
        if (j.media_type === 'tv' && j.series_title) {
            const sStr = j.season != null ? String(j.season).padStart(2, '0') : '';
            const eStr = j.episode != null ? String(j.episode).padStart(2, '0') : '';
            titleText = sStr && eStr ? `${j.series_title} S${sStr}E${eStr}` : j.series_title;
            subtitleText = j.title || '';
        }
        const isUpload = j.direction === 'upload' || j.status === 'uploading';
        const isCanceledBySender = j.status === 'canceled_by_sender';
        const pct = Math.max(0, Math.min(100, Number(j.progress_pct || 0)));
        const details = formatLoomDetailsLine(j.quality_label, j.total_bytes || j.bytes_transferred, j.hops || 1, j.peer_name);
        const isActiveTransfer = j.status === 'downloading' || j.status === 'uploading';
        const speedText = isActiveTransfer ? formatInboxDownloadSpeed(j.speed_bps) : '';
        const etaText = isActiveTransfer ? formatInboxDownloadEta(j.bytes_transferred, j.total_bytes, j.speed_bps) : '';
        const statusText = isCanceledBySender
            ? 'Canceled by Sender'
            : [formatInboxDownloadStatus(j.status), `${pct.toFixed(0)}%`, speedText].filter(Boolean).join(' • ');
        const actionBtn = isUpload
            ? `<button type="button" class="btn btn-danger btn-sm" onclick="cancelLoomUpload('${escapeHtml(j.upload_id || j.id)}', event)">Cancel</button>`
            : (isCanceledBySender
                ? `<button type="button" class="btn btn-secondary btn-sm" onclick="cancelLoomDownload(${j.id}, event)">Dismiss</button>`
                : `<button type="button" class="btn btn-danger btn-sm" onclick="cancelLoomDownload(${j.id}, event)">Cancel</button>`);
        return `
            <div class="inbox-item">
                <strong>${escapeHtml(titleText)}</strong>
                ${subtitleText ? `<small>${escapeHtml(subtitleText)}</small>` : ''}
                <small>${details}</small>
                <div class="inbox-progress-track">
                    <div class="inbox-progress-fill" style="width: ${pct}%;"></div>
                </div>
                <div class="inbox-download-footer">
                    <small class="m-0">${escapeHtml(statusText)}</small>
                    ${etaText ? `<small class="m-0">${escapeHtml(etaText)}</small>` : ''}
                    ${actionBtn}
                </div>
            </div>
        `;
    }

    async function loadInbox() {
        const badge = document.getElementById('inboxBadge');
        const container = document.getElementById('inboxItems');
        const mobileBadge = document.getElementById('mobileInboxBadge');
        const mobileContainer = document.getElementById('mobileInboxItems');
        if (!badge && !container && !mobileBadge && !mobileContainer) return;

        try {
            const res = await fetch('/api/inbox');
            if (!res.ok) return;
            const data = await res.json();
            const items = data.invites || [];
            const resets = data.reset_requests || [];
            const loomDownloads = data.loom_downloads || [];
            const loomUploads = data.loom_uploads || [];
            const loomPairRequests = data.loom_pair_requests || [];
            const totalCount = items.length + resets.length + loomDownloads.length + loomUploads.length + loomPairRequests.length;

            if (totalCount > 0) {
                if (badge) badge.style.display = 'block';
                if (mobileBadge) {
                    mobileBadge.style.display = 'inline-block';
                    mobileBadge.textContent = totalCount;
                }
                let html = '';
                html += loomDownloads.map(renderInboxLoomDownload).join('');
                html += loomUploads.map(renderInboxLoomDownload).join('');
                html += items.map(inv => `
                    <div class="inbox-item">
                        <div><strong>${escapeHtml(inv.sender)}</strong> invited you to watch <em>${escapeHtml(inv.title)}</em></div>
                        <div class="inbox-actions">
                            <button class="btn btn-sm" style="text-align: center;" onclick="respondInvite(${inv.id}, 'accept')">Accept</button>
                            <button class="btn btn-sm btn-secondary" style="text-align: center;" onclick="respondInvite(${inv.id}, 'decline')">Decline</button>
                        </div>
                    </div>
                `).join('');
                html += resets.map(pr => `
                    <div class="inbox-item">
                        <div><strong>${escapeHtml(pr.username)}</strong> is requesting a password reset</div>
                        <small>Requested ${escapeHtml(pr.created_at)}</small>
                        <div class="inbox-actions">
                            <button class="btn btn-sm" onclick="respondReset(${pr.id}, 'approve')">Approve</button>
                            <button class="btn btn-sm btn-secondary" onclick="respondReset(${pr.id}, 'deny')">Deny</button>
                        </div>
                    </div>
                `).join('');
                html += loomPairRequests.map(pr => `
                    <div class="inbox-item">
                        <div><strong>${escapeHtml(pr.via_peer_name)}</strong> relayed a Direct Peer Pair Request${pr.item_label ? ` for <em>${escapeHtml(pr.item_label)}</em>` : ''}</div>
                        <div class="inbox-actions">
                            <button type="button" class="btn btn-sm" onclick="respondInboxLoomDirectPair(${pr.id}, 'approve')">Approve</button>
                            <button type="button" class="btn btn-sm btn-secondary" onclick="respondInboxLoomDirectPair(${pr.id}, 'decline')">Decline</button>
                        </div>
                    </div>
                `).join('');
                if (container) container.innerHTML = html;
                if (mobileContainer) mobileContainer.innerHTML = html;
            } else {
                if (badge) badge.style.display = 'none';
                if (mobileBadge) mobileBadge.style.display = 'none';
                if (container) container.innerHTML = '<div class="empty-state">No new notifications</div>';
                if (mobileContainer) mobileContainer.innerHTML = '<div class="empty-state">No new notifications</div>';
            }
        } catch (err) {
            console.error('Error loading inbox:', err);
        }
    }

    async function cancelLoomDownload(jobId, event) {
        if (event) { event.preventDefault(); event.stopPropagation(); }
        try {
            await fetch(`/api/loom/jobs/${jobId}/cancel`, { method: 'POST' });
            loadInbox();
        } catch (err) {
            console.error('Failed to cancel Loom download:', err);
        }
    }

    async function cancelLoomUpload(uploadId, event) {
        if (event) { event.preventDefault(); event.stopPropagation(); }
        try {
            await fetch(`/api/loom/uploads/${encodeURIComponent(uploadId)}/cancel`, { method: 'POST' });
            loadInbox();
        } catch (err) {
            console.error('Failed to cancel Loom upload:', err);
        }
    }

    async function respondInvite(id, action) {
        try {
            const res = await fetch(`/watch_together/${action}/${id}`, { method: 'POST' });
            const data = await res.json();
            if (data.success && action === 'accept') {
                window.location.href = `/play_sync/${data.session_id}`;
            } else {
                loadInbox();
            }
        } catch (err) {
            console.error(err);
        }
    }

    async function respondReset(id, action) {
        try {
            await fetch(`/password_reset/${action}/${id}`, { method: 'POST' });
            loadInbox();
        } catch (err) {
            console.error(err);
        }
    }

    async function respondInboxLoomDirectPair(id, decision) {
        try {
            await fetch(`/api/loom/direct_pair/${id}/respond`, {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ decision })
            });
            loadInbox();
        } catch (err) {
            console.error(err);
        }
    }

    function toggleInbox(e) {
        if (e) { e.preventDefault(); e.stopPropagation(); }
        const profileMenu = document.getElementById('profileDropdownMenu');
        if (profileMenu) profileMenu.style.display = 'none';
        const menu = document.getElementById('inboxMenu');
        if (!menu) return;
        const isHidden = menu.style.display === 'none' || !menu.style.display;
        menu.style.display = isHidden ? 'block' : 'none';
        if (isHidden) loadInbox();
    }

    function toggleMobileMenu(e) {
        if (e) { e.preventDefault(); e.stopPropagation(); }
        const menu = document.getElementById('mobileMenu');
        const backdrop = document.getElementById('mobileMenuBackdrop');
        if (!menu) return;
        const isCurrentlyOpen = menu.classList.contains('open') || menu.style.display === 'flex';
        if (isCurrentlyOpen) {
            menu.classList.remove('open');
            menu.style.display = 'none';
            if (backdrop) backdrop.classList.remove('open');
        } else {
            menu.classList.add('open');
            menu.style.display = 'flex';
            if (backdrop) backdrop.classList.add('open');
        }
        const icon = document.getElementById('mobileMenuIcon');
        if (icon) {
            icon.setAttribute('data-lucide', !isCurrentlyOpen ? 'x' : 'menu');
            if (window.lucide) lucide.createIcons();
        }
    }

    function toggleMobileInbox(e) {
        if (e) { e.preventDefault(); e.stopPropagation(); }
        const dropdown = document.getElementById('mobileInboxDropdown');
        if (!dropdown) return;
        const isHidden = dropdown.style.display === 'none' || !dropdown.style.display;
        dropdown.style.display = isHidden ? 'block' : 'none';
        if (isHidden) loadInbox();
    }

    // ------------------------------------------------------------------
    // Profile Dropdown & Quick Switch
    // ------------------------------------------------------------------
    function toggleProfileDropdown(event) {
        if (event) { event.preventDefault(); event.stopPropagation(); }
        const inboxMenu = document.getElementById('inboxMenu');
        if (inboxMenu) inboxMenu.style.display = 'none';
        const menu = document.getElementById('profileDropdownMenu');
        if (menu) {
            const isHidden = menu.style.display === 'none' || menu.style.display === '';
            menu.style.display = isHidden ? 'flex' : 'none';
        }
    }

    async function quickSwitchProfile(profileId, hasPin) {
        if (hasPin) {
            window.location.href = `/profiles?select=${profileId}`;
            return;
        }
        try {
            const resp = await fetch('/api/profile/select', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ profile_id: profileId })
            });
            const data = await resp.json();
            if (data.requires_pin) {
                window.location.href = `/profiles?select=${profileId}`;
            } else if (data.success) {
                window.location.reload();
            }
        } catch (err) {
            console.error('Profile switch error:', err);
            window.location.href = '/profiles';
        }
    }

    // ------------------------------------------------------------------
    // Card / Action Dropdown Menus & Carousels
    // ------------------------------------------------------------------
    function toggleMenu(event, menuId) {
        event.preventDefault();
        event.stopPropagation();

        document.querySelectorAll('.dropdown-menu.show').forEach(menu => {
            if (menu.id !== menuId) menu.classList.remove('show');
        });

        const menu = document.getElementById(menuId);
        if (!menu) return;
        const isShowing = !menu.classList.contains('show');
        if (isShowing) {
            menu.dataset.pjaxEphemeral = '1';
            document.body.appendChild(menu);
            menu.style.position = 'fixed';
            menu.style.visibility = 'hidden';
            menu.classList.add('show');

            const btnRect = event.currentTarget.getBoundingClientRect();
            const menuRect = menu.getBoundingClientRect();
            const menuWidth = menuRect.width || 180;
            const menuHeight = menuRect.height || 150;
            const viewportWidth = window.innerWidth;
            const viewportHeight = window.innerHeight;

            let left = event.currentTarget.classList.contains('three-dots-btn')
                ? (btnRect.right - menuWidth)
                : btnRect.left;
            if (left < 10) left = 10;
            if (left + menuWidth > viewportWidth - 10) {
                left = Math.max(10, viewportWidth - menuWidth - 10);
            }

            let top = btnRect.bottom + 5;
            if (top + menuHeight > viewportHeight - 10 && btnRect.top - menuHeight - 5 > 10) {
                top = btnRect.top - menuHeight - 5;
            }

            menu.style.left = left + 'px';
            menu.style.right = 'auto';
            menu.style.top = top + 'px';
            menu.style.visibility = 'visible';
        } else {
            menu.classList.remove('show');
        }
    }

    function scrollCarousel(btn, direction) {
        const row = btn.parentElement.querySelector('.carousel-row');
        if (!row) return;
        const scrollAmount = row.clientWidth * 0.8;
        row.scrollBy({ left: direction * scrollAmount, behavior: 'smooth' });
        setTimeout(() => { if (window.lucide) lucide.createIcons(); }, 300);
    }

    function updateCarouselArrows() {
        document.querySelectorAll('.carousel-container').forEach(container => {
            const row = container.querySelector('.carousel-row');
            const leftBtn = container.querySelector('.carousel-arrow.left');
            const rightBtn = container.querySelector('.carousel-arrow.right');
            if (!row || !leftBtn || !rightBtn) return;

            if (row.scrollWidth > row.clientWidth) {
                leftBtn.style.display = row.scrollLeft > 0 ? 'flex' : 'none';
                rightBtn.style.display = Math.ceil(row.scrollLeft + row.clientWidth) >= row.scrollWidth ? 'none' : 'flex';
            } else {
                leftBtn.style.display = 'none';
                rightBtn.style.display = 'none';
            }
        });
    }

    function bindCarousels() {
        updateCarouselArrows();
        document.querySelectorAll('.carousel-row').forEach(row => {
            if (!row.dataset.carouselBound) {
                row.dataset.carouselBound = '1';
                row.addEventListener('scroll', updateCarouselArrows, { passive: true });
            }
        });
    }

    // ------------------------------------------------------------------
    // Watch Together Modal (dynamically mounted once on document.body)
    // ------------------------------------------------------------------
    let wtCurrentMedia = { type: null, id: null };
    let isCreatingWatchSession = false;

    function ensureWatchTogetherModal() {
        let modal = document.getElementById('wtModal');
        if (modal) return modal;
        modal = document.createElement('div');
        modal.id = 'wtModal';
        modal.className = 'modal-overlay';
        modal.style.display = 'none';
        modal.innerHTML = `
            <div class="modal-content">
                <div class="card-header">
                    <h2 class="m-0">Watch Together</h2>
                    <button type="button" class="btn btn-sm btn-secondary" onclick="document.getElementById('wtModal').style.display='none'">Close</button>
                </div>
                <div id="wtActiveSessionAlert" style="display: none;"></div>
                <p>Select users to invite to this session:</p>
                <div id="wtUserList" class="modal-list">Loading users...</div>
                <button type="button" id="wtStartBtn" class="btn btn-block" onclick="startWatchTogether()">Start Session</button>
            </div>
        `;
        modal.addEventListener('click', (e) => {
            if (e.target === modal) modal.style.display = 'none';
        });
        document.body.appendChild(modal);
        return modal;
    }

    async function openWatchTogether(mediaType, itemId) {
        document.querySelectorAll('.dropdown-menu.show').forEach(m => m.classList.remove('show'));
        wtCurrentMedia = { type: mediaType, id: itemId };
        isCreatingWatchSession = false;
        const modal = ensureWatchTogetherModal();
        const userList = document.getElementById('wtUserList');
        const alertBox = document.getElementById('wtActiveSessionAlert');
        const startBtn = document.getElementById('wtStartBtn');

        if (alertBox) { alertBox.style.display = 'none'; alertBox.innerHTML = ''; }
        if (startBtn) {
            startBtn.disabled = false;
            startBtn.style.opacity = '';
            startBtn.style.pointerEvents = '';
            startBtn.textContent = 'Start Session';
        }

        modal.style.display = 'flex';
        userList.innerHTML = 'Loading users...';

        try {
            const [usersRes, activeRes] = await Promise.all([
                fetch(`/api/users/eligible?media_type=${encodeURIComponent(mediaType)}`),
                fetch('/watch_together/active')
            ]);
            const users = await usersRes.json();
            let activeData = { has_active_session: false };
            try { activeData = await activeRes.json(); } catch (_) { }

            if (activeData.has_active_session) {
                if (alertBox) {
                    alertBox.style.display = 'block';
                    alertBox.innerHTML = `
                        <div class="wt-active-warning" style="background: rgba(234, 179, 8, 0.15); border: 1px solid rgba(234, 179, 8, 0.3); border-radius: var(--radius); padding: 12px; margin-bottom: 15px; font-size: 0.9rem;">
                            <div style="font-weight: 600; color: #facc15; margin-bottom: 6px;">You already have an active Watch Together session.</div>
                            <div style="display: flex; gap: 8px; margin-top: 8px;">
                                <a href="/play_sync/${encodeURIComponent(activeData.session_id)}" class="btn btn-sm btn-primary" style="text-decoration: none; display: inline-flex; align-items: center;">Rejoin Session</a>
                                <button type="button" class="btn btn-sm btn-secondary" onclick="endAndRestartSession('${escapeHtml(activeData.session_id)}', '${escapeHtml(mediaType)}', ${Number(itemId)})">End Active Session</button>
                            </div>
                        </div>
                    `;
                }
                if (startBtn) {
                    startBtn.disabled = true;
                    startBtn.style.opacity = '0.6';
                    startBtn.style.pointerEvents = 'none';
                    startBtn.textContent = 'Active Session in Progress';
                }
            }

            const householdUsers = Array.isArray(users) ? users.filter(u => u.category === 'Household' || u.tag === 'Household') : [];
            const sharedUsers = Array.isArray(users) ? users.filter(u => u.category === 'Shared' || u.tag === 'Shared') : [];

            if (!Array.isArray(users) || users.length === 0) {
                userList.innerHTML = '<div class="empty-state">No eligible users found. Make sure they have Watch Together and library access enabled in settings.</div>';
            } else {
                let html = '';
                if (householdUsers.length > 0) {
                    html += '<div class="wt-group-title">Household</div>';
                    html += householdUsers.map(u => `
                        <label class="toggle-pill">
                            <input type="checkbox" class="hidden-checkbox wt-user-checkbox" value="${escapeHtml(u.id)}">
                            <span>${escapeHtml(u.username)}</span>
                        </label>
                    `).join('');
                }
                if (sharedUsers.length > 0) {
                    html += '<div class="wt-group-title">Shared</div>';
                    html += sharedUsers.map(u => `
                        <label class="toggle-pill">
                            <input type="checkbox" class="hidden-checkbox wt-user-checkbox" value="${escapeHtml(u.id)}">
                            <span>${escapeHtml(u.username)}</span>
                        </label>
                    `).join('');
                }
                userList.innerHTML = html;
            }
        } catch (err) {
            console.error(err);
            userList.innerHTML = 'Error loading users.';
        }
    }

    async function endAndRestartSession(sessionId, mediaType, itemId) {
        try {
            await fetch(`/watch_together/end/${encodeURIComponent(sessionId)}`, { method: 'POST' });
            openWatchTogether(mediaType, itemId);
        } catch (err) {
            console.error(err);
            alert('Failed to end active session.');
        }
    }

    async function startWatchTogether() {
        if (isCreatingWatchSession) return;
        const startBtn = document.getElementById('wtStartBtn');
        const originalText = startBtn ? startBtn.innerHTML : 'Start Session';

        isCreatingWatchSession = true;
        if (startBtn) {
            startBtn.disabled = true;
            startBtn.style.opacity = '0.6';
            startBtn.style.pointerEvents = 'none';
            startBtn.innerHTML = '<span class="spinner" style="display:inline-block; width:14px; height:14px; border:2px solid #fff; border-top-color:transparent; border-radius:50%; animation:spin 0.8s linear infinite; vertical-align:middle; margin-right:8px;"></span>Starting Session...';
        }

        const targetIds = Array.from(document.querySelectorAll('.wt-user-checkbox:checked')).map(cb => cb.value);
        try {
            const res = await fetch('/watch_together/create', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({
                    media_type: wtCurrentMedia.type,
                    item_id: wtCurrentMedia.id,
                    invited_user_ids: targetIds,
                    invited_targets: targetIds
                })
            });
            const data = await res.json();
            if (!res.ok || data.error) {
                alert(data.error || data.detail || 'Error creating session.');
                if (startBtn) {
                    startBtn.disabled = false;
                    startBtn.style.opacity = '';
                    startBtn.style.pointerEvents = '';
                    startBtn.innerHTML = originalText;
                }
                isCreatingWatchSession = false;
                if (data.session_id) openWatchTogether(wtCurrentMedia.type, wtCurrentMedia.id);
                return;
            }
            if (data.session_id) {
                window.location.href = `/play_sync/${data.session_id}`;
            } else {
                if (startBtn) {
                    startBtn.disabled = false;
                    startBtn.style.opacity = '';
                    startBtn.style.pointerEvents = '';
                    startBtn.innerHTML = originalText;
                }
                isCreatingWatchSession = false;
            }
        } catch (err) {
            console.error(err);
            alert('Error creating session.');
            if (startBtn) {
                startBtn.disabled = false;
                startBtn.style.opacity = '';
                startBtn.style.pointerEvents = '';
                startBtn.innerHTML = originalText;
            }
            isCreatingWatchSession = false;
        }
    }

    // ------------------------------------------------------------------
    // Admin Server Activity Badge Monitor (single global poller)
    // ------------------------------------------------------------------
    let activityTimer = null;

    async function checkServerActivity() {
        clearTimeout(activityTimer);
        const badge = document.getElementById('serverActivityBadge');
        const textEl = document.getElementById('serverActivityText');
        if (!badge || !textEl) return;

        if (document.hidden) {
            activityTimer = setTimeout(checkServerActivity, 5000);
            return;
        }

        let pollDelay = 8000;
        try {
            const res = await fetch('/api/scanner/status');
            if (res.ok) {
                const data = await res.json();
                const mobileBadge = document.getElementById('mobileServerActivityBadge');
                const mobileTextEl = document.getElementById('mobileServerActivityText');

                if (data.is_scanning && data.total_items > 0) {
                    badge.style.display = 'inline-flex';
                    badge.classList.remove('complete');
                    textEl.textContent = data.short_status || `Scanning (${data.items_scanned}/${data.total_items})`;
                    badge.title = data.current_item ? `${data.action}: ${data.current_item}` : (data.action || 'Library scan in progress');
                    if (mobileBadge && mobileTextEl) {
                        mobileBadge.style.display = 'inline-flex';
                        mobileTextEl.textContent = data.short_status || `Scanning (${data.items_scanned}/${data.total_items})`;
                    }
                    pollDelay = 3000;
                } else {
                    badge.style.display = 'none';
                    badge.classList.remove('complete');
                    if (mobileBadge) mobileBadge.style.display = 'none';
                    pollDelay = data.is_scanning ? 3000 : 8000;
                }
            } else {
                badge.style.display = 'none';
                pollDelay = 10000;
            }
        } catch (_) {
            badge.style.display = 'none';
            pollDelay = 10000;
        } finally {
            if (document.getElementById('serverActivityBadge')) {
                activityTimer = setTimeout(checkServerActivity, pollDelay);
            }
        }
    }

    // ------------------------------------------------------------------
    // Global Persistent Event Listeners (attached once)
    // ------------------------------------------------------------------
    document.addEventListener('click', function (e) {
        const inboxMenu = document.getElementById('inboxMenu');
        const inboxBtn = document.getElementById('inboxBtn');
        if (inboxMenu && inboxBtn && e.target !== inboxBtn && !inboxBtn.contains(e.target) && !inboxMenu.contains(e.target)) {
            inboxMenu.style.display = 'none';
        }

        const profileContainer = document.querySelector('.profile-header-container');
        if (profileContainer && !profileContainer.contains(e.target)) {
            const profileMenu = document.getElementById('profileDropdownMenu');
            if (profileMenu) profileMenu.style.display = 'none';
        }

        const mobileMenu = document.getElementById('mobileMenu');
        const mobileBtn = document.getElementById('mobileMenuBtn');
        if (mobileMenu && mobileBtn && !mobileMenu.contains(e.target) && !mobileBtn.contains(e.target)) {
            if (mobileMenu.classList.contains('open') || mobileMenu.style.display === 'flex') {
                mobileMenu.classList.remove('open');
                mobileMenu.style.display = 'none';
                const backdrop = document.getElementById('mobileMenuBackdrop');
                if (backdrop) backdrop.classList.remove('open');
                const icon = document.getElementById('mobileMenuIcon');
                if (icon) {
                    icon.setAttribute('data-lucide', 'menu');
                    if (window.lucide) lucide.createIcons();
                }
            }
        }

        if (!e.target.closest('.three-dots-btn') && !e.target.closest('.dropdown-menu') && !e.target.closest('.btn')) {
            document.querySelectorAll('.dropdown-menu.show').forEach(menu => menu.classList.remove('show'));
        }
    });

    window.addEventListener('scroll', function () {
        document.querySelectorAll('.dropdown-menu.show').forEach(menu => menu.classList.remove('show'));
    }, true);

    window.addEventListener('resize', updateCarouselArrows);

    document.addEventListener('visibilitychange', () => {
        if (!document.hidden && document.getElementById('serverActivityBadge')) {
            checkServerActivity();
        }
    });

    setInterval(() => {
        if (!document.hidden) loadInbox();
    }, 10000);

    function onPageSwap() {
        const wtModal = document.getElementById('wtModal');
        if (wtModal) wtModal.style.display = 'none';
        bindCarousels();
        loadInbox();
        checkServerActivity();
    }

    if (document.readyState === 'loading') {
        document.addEventListener('DOMContentLoaded', onPageSwap, { once: true });
    } else {
        onPageSwap();
    }

    window.loadInbox = loadInbox;
    window.cancelLoomDownload = cancelLoomDownload;
    window.cancelLoomUpload = cancelLoomUpload;
    window.respondInvite = respondInvite;
    window.respondReset = respondReset;
    window.respondInboxLoomDirectPair = respondInboxLoomDirectPair;
    window.toggleInbox = toggleInbox;
    window.toggleMobileMenu = toggleMobileMenu;
    window.toggleMobileInbox = toggleMobileInbox;
    window.toggleProfileDropdown = toggleProfileDropdown;
    window.quickSwitchProfile = quickSwitchProfile;
    window.toggleMenu = toggleMenu;
    window.scrollCarousel = scrollCarousel;
    window.updateCarouselArrows = updateCarouselArrows;
    window.openWatchTogether = openWatchTogether;
    window.endAndRestartSession = endAndRestartSession;
    window.startWatchTogether = startWatchTogether;
    window.HeirloomShell = { onPageSwap, escapeHtml };
})();
