(function () {
    // Media Navigation Loading Overlay
    function ensureMediaNavOverlay() {
        let overlay = document.getElementById('mediaNavOverlay');
        if (!overlay) {
            overlay = document.createElement('div');
            overlay.id = 'mediaNavOverlay';
            overlay.className = 'media-nav-overlay';
            overlay.innerHTML = `
                <div class="media-nav-spinner-box">
                    <div class="loading-spinner">
                        <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round" style="width: 48px; height: 48px; animation: spin 1.2s linear infinite; color: var(--accent, #e50914);">
                            <path d="M21 12a9 9 0 1 1-6.219-8.56"></path>
                        </svg>
                    </div>
                    <div class="media-nav-title" id="mediaNavTitle">Loading...</div>
                    <div class="media-nav-subtext" id="mediaNavSubtext">Fetching media details</div>
                </div>
            `;
            document.body.appendChild(overlay);
        }
        return overlay;
    }

    let hideTimer = null;

    function showMediaLoader(title, subtext) {
        const overlay = ensureMediaNavOverlay();
        const titleEl = document.getElementById('mediaNavTitle');
        const subtextEl = document.getElementById('mediaNavSubtext');
        if (titleEl) titleEl.textContent = title || 'Loading...';
        if (subtextEl) subtextEl.textContent = subtext || 'Fetching media information';
        
        overlay.classList.add('active');

        // Safety timeout to prevent getting stuck if page navigation is cancelled
        if (hideTimer) clearTimeout(hideTimer);
        hideTimer = setTimeout(hideMediaLoader, 8000);
    }

    function hideMediaLoader() {
        if (hideTimer) {
            clearTimeout(hideTimer);
            hideTimer = null;
        }
        const overlay = document.getElementById('mediaNavOverlay');
        if (overlay) {
            overlay.classList.remove('active');
        }
    }

    window.showMediaLoader = showMediaLoader;
    window.hideMediaLoader = hideMediaLoader;

    // Attach click listeners to all links navigating to /movie/ or /series/
    function handleMediaLinkClick(e) {
        if (e.button !== 0 || e.metaKey || e.ctrlKey || e.shiftKey || e.altKey) return;

        // Skip if event was cancelled or prevented by another handler
        if (e.defaultPrevented) return;

        const link = e.target.closest('a');
        if (!link) return;

        // Skip non-navigating links: anchors, void, javascript, empty
        const rawHref = (link.getAttribute('href') || '').trim();
        if (!rawHref || rawHref === '#' || rawHref.startsWith('#') || rawHref.startsWith('javascript:')) {
            return;
        }

        // Skip anchor jump links, downloads, new tabs, and explicit ignore flags
        if (link.target === '_blank' || link.hasAttribute('download') || link.dataset.noLoader !== undefined) {
            return;
        }

        // Skip clicks originating from dropdown toggles, buttons, or modals
        if (link.closest('#inboxBtn, .inbox, .inbox-menu, #inboxMenu, .dropdown-menu, .modal-overlay, .profile-header-btn')) {
            return;
        }

        let url;
        try {
            url = new URL(link.href, window.location.origin);
        } catch (_) {
            return;
        }

        if (url.origin !== window.location.origin) return;

        // Skip if staying on the same page (e.g. hash change or self link)
        if (url.pathname === window.location.pathname && (url.search === window.location.search || !rawHref.includes('?'))) {
            return;
        }

        const path = url.pathname;

        if (path.startsWith('/series/')) {
            let title = decodeURIComponent(path.replace('/series/', '').split('?')[0]);
            showMediaLoader(title ? `Loading ${title}` : 'Loading Series...', 'Fetching episodes and series details');
        } else if (path.startsWith('/movie/')) {
            showMediaLoader(title ? `Loading ${title}` : 'Loading Movie...', 'Fetching movie details');
        }
    }

    // Initialize listeners (bubbling phase so handlers calling preventDefault are respected)
    document.addEventListener('click', handleMediaLinkClick);

    // Handle bfcache (back/forward cache) when user returns to page
    window.addEventListener('pageshow', function () {
        hideMediaLoader();
    });

    // Also hide on page load
    if (document.readyState === 'loading') {
        document.addEventListener('DOMContentLoaded', hideMediaLoader);
    } else {
        hideMediaLoader();
    }
})();

