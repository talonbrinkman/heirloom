/**
 * Heirloom PJAX Router
 * Keeps the document shell and persistent audio player alive across page navigations.
 */
(function () {
    if (window.HeirloomPjax) return;

    const EXCLUDED_PREFIXES = [
        '/play/',
        '/play_sync/',
        '/photo/',
        '/photo_file/',
        '/download/',
        '/stream/',
        '/hls/',
        '/subtitle/',
        '/trickplay/',
        '/api/',
        '/login',
        '/logout',
        '/register',
        '/setup',
        '/profiles',
        '/auth',
        '/reset_password',
    ];

    let pageAbort = new AbortController();
    let cleanupFns = [];
    const activeIntervals = new Set();
    const activeTimeouts = new Set();
    let inflightFetch = null;
    let isSwapping = false;
    let lastPath = window.location.pathname;
    const loadedLibs = new Set();

    function trackExistingLibs() {
        document.querySelectorAll('script[src]').forEach(s => {
            try {
                loadedLibs.add(new URL(s.src, window.location.origin).pathname);
            } catch (_) { }
        });
    }

    function isEligibleUrl(urlObj) {
        if (!urlObj || urlObj.origin !== window.location.origin) return false;
        const p = urlObj.pathname;
        for (const prefix of EXCLUDED_PREFIXES) {
            if (p === prefix || p.startsWith(prefix)) return false;
        }
        return true;
    }

    function runCleanup() {
        try { pageAbort.abort(); } catch (_) { }
        pageAbort = new AbortController();

        activeIntervals.forEach(id => clearInterval(id));
        activeIntervals.clear();

        activeTimeouts.forEach(id => clearTimeout(id));
        activeTimeouts.clear();

        const fns = cleanupFns;
        cleanupFns = [];
        for (const fn of fns) {
            try { fn(); } catch (e) { console.error('[PJAX cleanup]', e); }
        }

        // Remove portaled dropdowns/menus belonging to the outgoing page
        document.querySelectorAll('[data-pjax-ephemeral="1"]').forEach(el => el.remove());
        document.querySelectorAll('.dropdown-menu.show').forEach(el => el.classList.remove('show'));
    }

    function loadScriptOnce(src) {
        let path;
        try {
            path = new URL(src, window.location.origin).pathname;
        } catch (_) {
            path = src;
        }
        if (loadedLibs.has(path)) return Promise.resolve();
        return new Promise((resolve) => {
            const s = document.createElement('script');
            s.src = src;
            s.onload = () => { loadedLibs.add(path); resolve(); };
            s.onerror = () => resolve();
            document.head.appendChild(s);
        });
    }

    function syncBodyAttributes(fetchedBody) {
        const keepActivePlayer = document.body.classList.contains('has-active-player');
        document.body.className = fetchedBody.className || '';
        if (keepActivePlayer) {
            document.body.classList.add('has-active-player');
        }
        document.body.style.cssText = fetchedBody.style.cssText || '';
    }

    function syncHeadStyles(fetchedDoc) {
        document.head.querySelectorAll('[data-pjax-head]').forEach(el => el.remove());
        fetchedDoc.head.querySelectorAll('[data-pjax-head]').forEach(el => {
            document.head.appendChild(document.importNode(el, true));
        });
    }

    async function executeRootScripts(fetchedRoot, targetRoot) {
        const scripts = Array.from(fetchedRoot.querySelectorAll('script'));
        scripts.forEach(s => s.remove());

        targetRoot.innerHTML = fetchedRoot.innerHTML;

        for (const oldScript of scripts) {
            if (oldScript.src) {
                await loadScriptOnce(oldScript.src);
            } else if (oldScript.textContent && oldScript.textContent.trim()) {
                const s = document.createElement('script');
                if (oldScript.type) s.type = oldScript.type;
                s.textContent = oldScript.textContent;
                targetRoot.appendChild(s);
            }
        }
    }

    async function navigate(rawUrl, options = {}) {
        const { push = true, replace = false, preserveScroll = false } = options;
        let url;
        try {
            url = new URL(rawUrl, window.location.origin);
        } catch (_) {
            window.location.href = rawUrl;
            return;
        }

        if (!isEligibleUrl(url)) {
            window.location.href = url.href;
            return;
        }

        if (inflightFetch) {
            try { inflightFetch.abort(); } catch (_) { }
        }
        const controller = new AbortController();
        inflightFetch = controller;
        const signal = controller.signal;
        let timedOut = false;
        const fetchTimeoutId = setTimeout(() => {
            timedOut = true;
            try { controller.abort(); } catch (_) { }
        }, 20000);

        try {
            const res = await fetch(url.href, {
                headers: { 'X-PJAX': '1', 'Accept': 'text/html' },
                credentials: 'same-origin',
                signal,
            });
            clearTimeout(fetchTimeoutId);

            const finalUrl = new URL(res.url || url.href, window.location.origin);
            const contentType = (res.headers.get('content-type') || '').toLowerCase();
            if (!res.ok || !contentType.includes('text/html') || !isEligibleUrl(finalUrl)) {
                window.location.href = finalUrl.href;
                return;
            }

            const html = await res.text();
            const doc = new DOMParser().parseFromString(html, 'text/html');
            const fetchedRoot = doc.getElementById('pjax-root');
            const targetRoot = document.getElementById('pjax-root');
            if (!fetchedRoot || !targetRoot) {
                window.location.href = finalUrl.href;
                return;
            }

            // Load any page-specific external libraries declared in <head data-pjax-lib>
            const headLibs = Array.from(doc.head.querySelectorAll('script[src][data-pjax-lib]'));
            for (const lib of headLibs) {
                await loadScriptOnce(lib.getAttribute('src'));
            }

            isSwapping = true;
            runCleanup();

            document.title = doc.title || 'Heirloom';
            syncHeadStyles(doc);
            syncBodyAttributes(doc.body);

            if (replace) {
                history.replaceState({ pjax: true }, document.title, finalUrl.pathname + finalUrl.search + finalUrl.hash);
            } else if (push) {
                history.pushState({ pjax: true }, document.title, finalUrl.pathname + finalUrl.search + finalUrl.hash);
            }
            lastPath = finalUrl.pathname;

            await executeRootScripts(fetchedRoot, targetRoot);

            if (window.lucide && typeof window.lucide.createIcons === 'function') {
                window.lucide.createIcons();
            }
            if (window.HeirloomShell && typeof window.HeirloomShell.onPageSwap === 'function') {
                window.HeirloomShell.onPageSwap();
            }

            if (!preserveScroll) {
                if (finalUrl.hash) {
                    const hashTarget = document.getElementById(finalUrl.hash.slice(1));
                    if (hashTarget) hashTarget.scrollIntoView();
                    else window.scrollTo(0, 0);
                } else {
                    window.scrollTo(0, 0);
                }
            }
        } catch (err) {
            clearTimeout(fetchTimeoutId);
            if (err && err.name === 'AbortError' && !timedOut) return;
            console.error('[PJAX] Navigation failed, falling back:', err);
            window.location.href = url.href;
        } finally {
            isSwapping = false;
            if (inflightFetch && inflightFetch.signal === signal) {
                inflightFetch = null;
            }
            if (typeof window.hideMediaLoader === 'function') {
                window.hideMediaLoader();
            }
        }
    }

    function handleClick(e) {
        if (e.button !== 0 || e.metaKey || e.ctrlKey || e.shiftKey || e.altKey || e.defaultPrevented) return;
        const link = e.target.closest('a');
        if (!link) return;

        const rawHref = (link.getAttribute('href') || '').trim();
        if (!rawHref || rawHref.startsWith('#') || rawHref.startsWith('javascript:')) return;
        if (link.target && link.target !== '_self') return;
        if (link.hasAttribute('download') || link.dataset.noPjax !== undefined) return;

        let url;
        try {
            url = new URL(link.href, window.location.origin);
        } catch (_) {
            return;
        }

        if (!isEligibleUrl(url)) return;

        // Same page & same query: let native hash jump or scroll top
        if (url.pathname === window.location.pathname && url.search === window.location.search) {
            if (url.hash) return;
            e.preventDefault();
            window.scrollTo({ top: 0, behavior: 'smooth' });
            return;
        }

        e.preventDefault();
        if (typeof window.maybeShowMediaLoaderForLink === 'function') {
            window.maybeShowMediaLoaderForLink(link, url);
        }
        navigate(url.href, { push: true });
    }

    window.addEventListener('popstate', () => {
        // Series page uses history.pushState for ?ep= switching without reloading
        if (window.location.pathname === lastPath && window.location.pathname.startsWith('/series/')) {
            return;
        }
        navigate(window.location.href, { push: false, preserveScroll: true });
    });

    document.addEventListener('click', handleClick);
    document.addEventListener('DOMContentLoaded', () => {
        trackExistingLibs();
        if (window.lucide && typeof window.lucide.createIcons === 'function') {
            window.lucide.createIcons();
        }
    });

    window.HeirloomPjax = {
        onReady(fn) {
            if (typeof fn !== 'function') return;
            if (isSwapping || document.readyState !== 'loading') {
                Promise.resolve().then(() => {
                    try { fn(); } catch (e) { console.error('[PJAX onReady]', e); }
                });
            } else {
                document.addEventListener('DOMContentLoaded', fn, { once: true });
            }
        },
        onCleanup(fn) {
            if (typeof fn === 'function') cleanupFns.push(fn);
        },
        listen(target, event, handler, options) {
            if (!target || typeof target.addEventListener !== 'function') return;
            const opts = typeof options === 'boolean'
                ? { capture: options, signal: pageAbort.signal }
                : Object.assign({}, options || {}, { signal: pageAbort.signal });
            target.addEventListener(event, handler, opts);
        },
        interval(fn, ms) {
            const id = setInterval(fn, ms);
            activeIntervals.add(id);
            return id;
        },
        timeout(fn, ms) {
            const id = setTimeout(() => {
                activeTimeouts.delete(id);
                fn();
            }, ms);
            activeTimeouts.add(id);
            return id;
        },
        navigate,
        isEligibleUrl,
    };
})();
