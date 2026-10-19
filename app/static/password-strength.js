/**
 * Password Strength Meter
 * Evaluates password strength and updates the strength bar & state text.
 */
(function() {
    function evaluatePasswordStrength(password) {
        if (!password) return null;
        const len = password.length;
        if (len < 6) return 'weak';

        let variety = 0;
        if (/[a-z]/.test(password)) variety++;
        if (/[A-Z]/.test(password)) variety++;
        if (/[0-9]/.test(password)) variety++;
        if (/[^A-Za-z0-9]/.test(password)) variety++;

        if (len >= 10 && variety >= 3) return 'great';
        if (len >= 14 && variety >= 2) return 'great';
        if (len >= 8 && variety >= 2) return 'good';
        if (len >= 12) return 'good';

        return 'weak';
    }

    const LABELS = {
        weak: 'Weak',
        good: 'Good',
        great: 'Great'
    };

    function setupMeter(container) {
        if (container._pwStrengthInit) return;
        container._pwStrengthInit = true;

        const targetId = container.getAttribute('data-for');
        let input = targetId ? document.getElementById(targetId) : null;
        if (!input && container.parentElement) {
            input = container.parentElement.querySelector('input[type="password"], input[name*="password"]');
        }
        if (!input) return;

        let fillEl = container.querySelector('.password-strength-fill');
        let textEl = container.querySelector('.password-strength-text');

        // Automatically build bar and text if not present in container
        if (!fillEl) {
            const bar = document.createElement('div');
            bar.className = 'password-strength-bar';
            fillEl = document.createElement('div');
            fillEl.className = 'password-strength-fill';
            bar.appendChild(fillEl);
            container.appendChild(bar);
        }
        if (!textEl) {
            textEl = document.createElement('div');
            textEl.className = 'password-strength-text';
            container.appendChild(textEl);
        }

        function update() {
            const val = input.value;
            const strength = evaluatePasswordStrength(val);

            if (!strength) {
                container.removeAttribute('data-strength');
                textEl.textContent = '';
            } else {
                container.setAttribute('data-strength', strength);
                textEl.textContent = LABELS[strength] || '';
            }
        }

        input.addEventListener('input', update);
        input.addEventListener('change', update);
        update();
    }

    function initAll() {
        document.querySelectorAll('.password-strength-container').forEach(setupMeter);
    }

    if (document.readyState === 'loading') {
        document.addEventListener('DOMContentLoaded', initAll);
    } else {
        initAll();
    }

    window.initPasswordStrength = initAll;
})();
