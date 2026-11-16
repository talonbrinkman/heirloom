(function () {
    const WATCH_ACTION_RE = /\/history\/(mark_watched|mark_unwatched)\/(movie|tv)\/(\d+)/;

    function parseWatchAction(action) {
        if (!action) return null;
        try {
            const url = new URL(action, window.location.origin);
            const match = url.pathname.match(WATCH_ACTION_RE);
            if (!match) return null;
            return {
                url,
                actionType: match[1],
                mediaType: match[2],
                itemId: match[3],
                scope: url.searchParams.get('scope') || ''
            };
        } catch (_) {
            return null;
        }
    }

    function buildSwappedAction(url, watched) {
        const nextAction = watched ? 'mark_unwatched' : 'mark_watched';
        const nextPath = url.pathname.replace(
            /\/history\/(mark_watched|mark_unwatched)\//,
            `/history/${nextAction}/`
        );
        return nextPath + url.search;
    }

    function updatePosterWatchedUI(posterContainer, watched) {
        if (!posterContainer) return;

        if (posterContainer.classList.contains('episode-poster-container')) {
            posterContainer.classList.toggle('watched', watched);
        }

        const existingBadge = posterContainer.querySelector(':scope > .watched-badge');
        if (watched && !existingBadge) {
            const badge = document.createElement('div');
            badge.className = 'watched-badge';
            badge.title = 'Watched';
            badge.innerHTML = '<i data-lucide="check"></i>';
            const dropdown = posterContainer.querySelector(':scope > .dropdown-menu');
            if (dropdown) {
                posterContainer.insertBefore(badge, dropdown);
            } else {
                posterContainer.appendChild(badge);
            }
            if (window.lucide) {
                lucide.createIcons({ nodes: [badge] });
            }
        } else if (!watched && existingBadge) {
            existingBadge.remove();
        }

        const progressBg = posterContainer.querySelector('.progress-bg');
        if (progressBg) {
            progressBg.remove();
        }
    }

    function findPosterContainer(form) {
        const direct = form.closest('.poster-container');
        if (direct) return direct;
        const menu = form.closest('.dropdown-menu');
        if (menu && menu.id) {
            const triggers = document.querySelectorAll('.three-dots-btn[onclick]');
            for (const btn of triggers) {
                const onclickVal = btn.getAttribute('onclick') || '';
                if (onclickVal.includes(menu.id)) {
                    return btn.closest('.poster-container');
                }
            }
        }
        return null;
    }

    function updateWatchForm(form, watched) {
        const parsed = parseWatchAction(form.getAttribute('action') || form.action);
        if (!parsed) return;

        form.setAttribute('action', buildSwappedAction(parsed.url, watched));

        const btn = form.querySelector('button[type="submit"]');
        if (!btn) return;

        if (form.closest('.info-actions')) {
            const isSeries = (form.getAttribute('title') || '').includes('Series');
            form.title = watched
                ? (isSeries ? 'Mark Series as Unwatched' : 'Mark as Unwatched')
                : (isSeries ? 'Mark Series as Watched' : 'Mark as Watched');
            btn.style.background = watched ? 'var(--accent)' : '';

            const restartBtn = document.querySelector('.hero-actions .restart-btn, #heroRestartBtn');
            if (restartBtn) {
                restartBtn.style.display = 'none';
            }
        } else {
            const isSeries = (btn.textContent || '').includes('Series');
            btn.textContent = watched
                ? (isSeries ? 'Mark Series as Unwatched' : 'Mark as Unwatched')
                : (isSeries ? 'Mark Series as Watched' : 'Mark as Watched');

            const posterContainer = findPosterContainer(form);
            updatePosterWatchedUI(posterContainer, watched);
        }
    }

    function syncSeriesEpisodesAndHero(triggerForm, parsed, watched) {
        const episodesGrid = document.getElementById('episodesGrid');
        if (!episodesGrid) return;

        // Series hero button toggled -> update all episode cards in the series grid
        if (triggerForm.closest('.info-actions') && parsed.mediaType === 'tv' && parsed.scope !== 'episode') {
            episodesGrid.querySelectorAll('.episode-card').forEach(card => {
                const epId = card.getAttribute('data-episode-id');
                updatePosterWatchedUI(card.querySelector('.episode-poster-container'), watched);
                if (epId) {
                    document.querySelectorAll(`form[action*="/history/mark_"][action*="/tv/${epId}"]`).forEach(epForm => {
                        const epParsed = parseWatchAction(epForm.getAttribute('action') || epForm.action);
                        if (epParsed && epParsed.scope === 'episode') {
                            updateWatchForm(epForm, watched);
                        }
                    });
                }
            });
            return;
        }

        // Episode card toggled -> update series hero button if all episodes watched / any unwatched
        if (parsed.mediaType === 'tv' && parsed.scope === 'episode') {
            const epCard = episodesGrid.querySelector(`.episode-card[data-episode-id="${parsed.itemId}"]`);
            if (epCard) {
                updatePosterWatchedUI(epCard.querySelector('.episode-poster-container'), watched);
            }
            const heroForm = document.querySelector('.info-actions form[action*="/history/mark_"]');
            if (!heroForm) return;
            const epContainers = episodesGrid.querySelectorAll('.episode-poster-container');
            if (epContainers.length === 0) return;
            const allWatched = Array.from(epContainers).every(el => el.classList.contains('watched'));
            updateWatchForm(heroForm, allWatched);
        }
    }

    function applyWatchUI(triggerForm, parsed, watched) {
        document.querySelectorAll('form[action*="/history/mark_"]').forEach(candidate => {
            const candidateParsed = parseWatchAction(candidate.getAttribute('action') || candidate.action);
            if (
                candidateParsed &&
                candidateParsed.mediaType === parsed.mediaType &&
                candidateParsed.itemId === parsed.itemId &&
                candidateParsed.scope === parsed.scope
            ) {
                updateWatchForm(candidate, watched);
            }
        });

        syncSeriesEpisodesAndHero(triggerForm, parsed, watched);
    }

    document.addEventListener('submit', async (e) => {
        const form = e.target;
        if (!(form instanceof HTMLFormElement)) return;

        const parsed = parseWatchAction(form.getAttribute('action') || form.action);
        if (!parsed) return;

        e.preventDefault();

        const actionUrl = form.getAttribute('action') || form.action;
        const optimisticWatched = parsed.actionType === 'mark_watched';
        const previousWatched = !optimisticWatched;

        const dropdown = form.closest('.dropdown-menu');
        if (dropdown) {
            dropdown.classList.remove('show');
        }

        // Apply optimistic UI update immediately
        applyWatchUI(form, parsed, optimisticWatched);

        try {
            const response = await fetch(actionUrl, {
                method: 'POST',
                credentials: 'same-origin',
                headers: {
                    'X-Requested-With': 'XMLHttpRequest',
                    'Accept': 'application/json'
                }
            });

            if (response.status === 401) {
                window.location.href = '/login';
                return;
            }
            if (!response.ok) {
                throw new Error(`Request failed with status ${response.status}`);
            }

            const data = await response.json();
            const serverWatched = Boolean(data.watched);
            if (serverWatched !== optimisticWatched) {
                applyWatchUI(form, parsed, serverWatched);
            }
        } catch (err) {
            console.error('[WatchToggle] Failed to update watched state:', err);
            applyWatchUI(form, parsed, previousWatched);
        }
    });
})();
