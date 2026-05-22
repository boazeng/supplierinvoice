/* TACT shared header — fills the account chip with the signed-in user
   and reveals the admin tab for admins. Safe no-op if elements are absent. */
fetch('/auth/me')
    .then(r => r.json())
    .then(u => {
        if (!u || !u.email) return;
        const chip = document.getElementById('current-user');
        if (chip) chip.textContent = '👤 ' + (u.name || u.email);
        if (u.role === 'admin') {
            const adminTab = document.getElementById('admin-link');
            if (adminTab) adminTab.style.display = '';
        }
    })
    .catch(() => { /* not logged in / offline — leave header as-is */ });
