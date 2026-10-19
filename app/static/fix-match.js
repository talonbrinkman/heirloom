// ==========================================================================
// FIX MATCH MODAL & HANDLERS
// ==========================================================================

function ensureFixMatchModal() {
    if (document.getElementById('fixMatchModal')) return;

    const modal = document.createElement('div');
    modal.id = 'fixMatchModal';
    modal.className = 'modal-overlay';
    modal.style.display = 'none';

    modal.innerHTML = `
        <div class="modal-content" style="width: 520px; max-width: 95%;">
            <div class="flex justify-between align-center mb-15">
                <h2 class="m-0" style="font-size: 1.2rem; font-weight: 700;">Fix Match</h2>
                <button type="button" class="btn btn-sm btn-secondary" onclick="closeFixMatchModal()" style="padding: 4px 10px; cursor: pointer;">
                    Close
                </button>
            </div>
            
            <div class="flex gap-10 mb-15">
                <input type="text" id="fixMatchQuery" placeholder="Search title on TMDB..." class="flex-1" autocomplete="off">
                <input type="text" id="fixMatchYear" placeholder="Year" style="width: 85px;" autocomplete="off">
            </div>

            <div id="fixMatchNotice" style="display: none; padding: var(--padding); border-radius: var(--radius); margin-bottom: 12px; font-size: 0.85em; text-align: center;"></div>

            <div id="fixMatchResults" class="fix-match-list">
                <div style="text-align: center; color: var(--text-muted); padding: 25px 10px; font-size: 0.9em;">
                    Search TMDB to find the correct match for this title.
                </div>
            </div>
        </div>
    `;

    document.body.appendChild(modal);

    modal.addEventListener('click', function(e) {
        if (e.target === modal) {
            closeFixMatchModal();
        }
    });

    const queryInput = document.getElementById('fixMatchQuery');
    const yearInput = document.getElementById('fixMatchYear');

    function onSearchInputChange() {
        if (window.fixMatchDebounceTimer) clearTimeout(window.fixMatchDebounceTimer);
        window.fixMatchDebounceTimer = setTimeout(() => {
            searchFixMatches();
        }, 300);
    }

    queryInput.addEventListener('input', onSearchInputChange);
    yearInput.addEventListener('input', onSearchInputChange);

    queryInput.addEventListener('keydown', function(e) {
        if (e.key === 'Enter') {
            e.preventDefault();
            if (window.fixMatchDebounceTimer) clearTimeout(window.fixMatchDebounceTimer);
            searchFixMatches();
        }
    });

    yearInput.addEventListener('keydown', function(e) {
        if (e.key === 'Enter') {
            e.preventDefault();
            if (window.fixMatchDebounceTimer) clearTimeout(window.fixMatchDebounceTimer);
            searchFixMatches();
        }
    });
}

function escapeHtml(str) {
    if (!str) return '';
    return String(str)
        .replace(/&/g, '&amp;')
        .replace(/</g, '&lt;')
        .replace(/>/g, '&gt;')
        .replace(/"/g, '&quot;')
        .replace(/'/g, '&#039;');
}

function openFixMatch(mediaType, itemId, title, year) {
    // Hide any open card dropdown menus
    document.querySelectorAll('.dropdown-menu.show').forEach(m => m.classList.remove('show'));

    ensureFixMatchModal();
    window.currentFixMatch = { mediaType, itemId, title };

    const queryInput = document.getElementById('fixMatchQuery');
    const yearInput = document.getElementById('fixMatchYear');
    const notice = document.getElementById('fixMatchNotice');

    if (notice) notice.style.display = 'none';

    // Parse title & year if combined, e.g. "Movie Title (2020)"
    let cleanTitle = (title || '').trim();
    let cleanYear = '';
    const match = cleanTitle.match(/^(.*?)\s*\((\d{4})\)$/);
    if (match) {
        cleanTitle = match[1].trim();
        cleanYear = match[2];
    } else {
        const yearMatch = String(year || '').match(/\b(19\d\d|20\d\d)\b/);
        if (yearMatch) cleanYear = yearMatch[1];
    }

    queryInput.value = cleanTitle;
    yearInput.value = cleanYear;

    const modal = document.getElementById('fixMatchModal');
    modal.style.display = 'flex';

    // Automatically trigger initial search
    searchFixMatches();
}

function closeFixMatchModal() {
    const modal = document.getElementById('fixMatchModal');
    if (modal) modal.style.display = 'none';
}

async function searchFixMatches() {
    if (!window.currentFixMatch) return;

    const query = document.getElementById('fixMatchQuery').value.trim();
    const year = document.getElementById('fixMatchYear').value.trim();
    const resultsContainer = document.getElementById('fixMatchResults');
    const notice = document.getElementById('fixMatchNotice');

    if (notice) notice.style.display = 'none';

    if (!query) {
        resultsContainer.innerHTML = `
            <div style="text-align: center; color: var(--text-muted); padding: 25px 10px; font-size: 0.9em;">
                Type a title above to search TMDB for matches.
            </div>
        `;
        return;
    }

    resultsContainer.innerHTML = `
        <div style="text-align: center; color: var(--text-muted); padding: 30px 10px; font-size: 0.9em;">
            Searching TMDB...
        </div>
    `;

    try {
        const params = new URLSearchParams({
            media_type: window.currentFixMatch.mediaType,
            query: query
        });
        if (year) params.append('year', year);

        const res = await fetch(`/api/media/matches?${params.toString()}`);
        const data = await res.json();

        if (!res.ok) {
            const err = data.error || 'Failed to search matches.';
            resultsContainer.innerHTML = `
                <div style="text-align: center; color: var(--failure); padding: 20px 10px; font-size: 0.9em;">
                    ${escapeHtml(err)}
                </div>
            `;
            return;
        }

        const candidates = data.candidates || [];
        if (candidates.length === 0) {
            resultsContainer.innerHTML = `
                <div style="text-align: center; color: var(--text-muted); padding: 30px 10px; font-size: 0.9em;">
                    No matches found on TMDB.<br>
                    <small style="display: block; margin-top: 6px;">Try adjusting the search query or clearing the year.</small>
                </div>
            `;
            return;
        }

        resultsContainer.innerHTML = candidates.map(c => `
            <div class="fix-match-item">
                <img src="${c.poster_url || ''}" class="fix-match-poster" onerror="this.style.opacity='0.2'">
                <div class="fix-match-info">
                    <div class="fix-match-title" title="${escapeHtml(c.title)}">${escapeHtml(c.title)}</div>
                    <div class="fix-match-year">${c.year ? c.year : 'Unknown Year'}</div>
                    <div class="fix-match-overview">${escapeHtml(c.overview || 'No overview available.')}</div>
                </div>
                <button type="button" class="btn btn-sm" onclick="applyFixMatch(${c.tmdb_id}, this)">Match</button>
            </div>
        `).join('');

    } catch (err) {
        resultsContainer.innerHTML = `
            <div style="text-align: center; color: var(--failure); padding: 20px 10px; font-size: 0.9em;">
                Network error while connecting to server.
            </div>
        `;
    }
}

async function applyFixMatch(tmdbId, btnEl) {
    if (!window.currentFixMatch) return;
    const { mediaType, itemId } = window.currentFixMatch;
    const notice = document.getElementById('fixMatchNotice');

    const originalText = btnEl.textContent;
    btnEl.disabled = true;
    btnEl.textContent = 'Matching...';

    // Disable all match buttons during processing
    document.querySelectorAll('.fix-match-item button').forEach(b => b.disabled = true);

    try {
        const res = await fetch('/api/media/fix_match', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({
                media_type: mediaType,
                item_id: itemId,
                tmdb_id: tmdbId
            })
        });

        const data = await res.json();
        if (!res.ok) {
            const err = data.error || 'Failed to apply match.';
            if (notice) {
                notice.style.display = 'block';
                notice.style.background = 'rgba(229, 9, 20, 0.2)';
                notice.style.border = '1px solid var(--accent)';
                notice.style.color = 'var(--accent)';
                notice.textContent = err;
            }
            btnEl.disabled = false;
            btnEl.textContent = originalText;
            document.querySelectorAll('.fix-match-item button').forEach(b => b.disabled = false);
            return;
        }

        if (notice) {
            notice.style.display = 'block';
            notice.style.background = 'rgba(76, 175, 80, 0.2)';
            notice.style.border = '1px solid var(--success)';
            notice.style.color = 'var(--success)';
            notice.textContent = 'Matched successfully! Updating...';
        }

        setTimeout(() => {
            if (mediaType === 'tv' && window.location.pathname.startsWith('/series/')) {
                window.location.href = `/series/${encodeURIComponent(data.title)}`;
            } else {
                window.location.reload();
            }
        }, 800);

    } catch (err) {
        if (notice) {
            notice.style.display = 'block';
            notice.style.background = 'rgba(229, 9, 20, 0.2)';
            notice.style.border = '1px solid var(--accent)';
            notice.style.color = 'var(--accent)';
            notice.textContent = 'Connection error while saving match.';
        }
        btnEl.disabled = false;
        btnEl.textContent = originalText;
        document.querySelectorAll('.fix-match-item button').forEach(b => b.disabled = false);
    }
}

// Helpers for triggering from data-attributes
function triggerFixMatch(btn) {
    const mediaType = btn.dataset.mediaType;
    const itemId = parseInt(btn.dataset.itemId, 10);
    const title = btn.dataset.title;
    const year = btn.dataset.year;
    openFixMatch(mediaType, itemId, title, year);
}

function triggerFixMatchEncoded(btn) {
    const mediaType = btn.dataset.mediaType;
    const itemId = parseInt(btn.dataset.itemId, 10);
    const title = decodeURIComponent(btn.dataset.title || '');
    const year = btn.dataset.year;
    openFixMatch(mediaType, itemId, title, year);
}

window.addEventListener('keydown', function(e) {
    if (e.key === 'Escape') {
        closeFixMatchModal();
    }
});
