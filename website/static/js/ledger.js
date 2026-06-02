/* ספרי הנהלת חשבונות — לוגיקת הדף */
'use strict';

const HEB_MONTHS = ['ינואר', 'פברואר', 'מרץ', 'אפריל', 'מאי', 'יוני',
    'יולי', 'אוגוסט', 'ספטמבר', 'אוקטובר', 'נובמבר', 'דצמבר'];

async function api(url, opts) {
    const r = await fetch(url, opts);
    if (!r.ok) {
        let d = {};
        try { d = await r.json(); } catch (e) { /* ignore */ }
        throw new Error(d.detail || ('שגיאה ' + r.status));
    }
    return r.json();
}

function toast(msg) {
    const t = document.getElementById('toast');
    t.textContent = msg;
    t.style.display = 'block';
    setTimeout(() => { t.style.display = 'none'; }, 3500);
}

function el(id) { return document.getElementById(id); }

const L = {
    company: null, bookId: null, book: null, editingDocId: null,

    async init() {
        try {
            const u = await (await fetch('/auth/me')).json();
            if (u && u.email) el('current-user').textContent = '👤 ' + (u.name || u.email);
        } catch (e) { /* ignore */ }
        el('company-select').onchange = (e) => L.selectCompany(e.target.value);
        await L.loadCompanies();
    },

    // ---------- חברות ----------
    async loadCompanies(selectId) {
        const { companies } = await api('/api/ledger/companies');
        const sel = el('company-select');
        sel.innerHTML = '<option value="">— בחר חברה —</option>' +
            companies.map(c => `<option value="${c.id}">${esc(c.name)}</option>`).join('');
        if (selectId) { sel.value = selectId; L.selectCompany(selectId); }
    },

    openNewCompany() { el('nc-name').value = ''; el('nc-tax').value = ''; el('dlg-company').showModal(); },

    async importFromPriority() {
        const btn = el('btn-import-priority');
        btn.disabled = true;
        btn.textContent = 'מייבא...';
        try {
            const r = await api('/api/ledger/import-companies', { method: 'POST' });
            toast(`יובאו ${r.imported} חברות מ-Priority${r.skipped ? ` (${r.skipped} קיימות)` : ''}`);
            await L.loadCompanies();
        } catch (e) {
            toast('שגיאה בייבוא: ' + e.message);
        } finally {
            btn.disabled = false;
            btn.textContent = '↓ ייבא מ-Priority';
        }
    },

    async createCompany() {
        const name = el('nc-name').value.trim();
        if (!name) { toast('הזן שם חברה'); return; }
        try {
            const c = await api('/api/ledger/companies', {
                method: 'POST', headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ name, tax_id: el('nc-tax').value }),
            });
            el('dlg-company').close();
            toast('החברה נוצרה');
            await L.loadCompanies(c.id);
        } catch (e) { toast(e.message); }
    },

    async deleteCompany() {
        if (!L.company) return;
        const sel = el('company-select');
        const name = sel.options[sel.selectedIndex]?.text || '';
        if (!confirm(`למחוק את "${name}"?\nכל הספרים והמסמכים שלה יימחקו לצמיתות.`)) return;
        try {
            await api(`/api/ledger/companies/${L.company}`, { method: 'DELETE' });
            toast('החברה נמחקה');
            el('btn-delete-company').style.display = 'none';
            el('books-card').classList.add('hidden');
            el('book-card').classList.add('hidden');
            L.company = null;
            await L.loadCompanies();
        } catch (e) { toast(e.message); }
    },

    async selectCompany(id) {
        L.company = id || null;
        el('btn-delete-company').style.display = id ? 'inline-flex' : 'none';
        el('book-card').classList.add('hidden');
        L.bookId = null;
        if (!id) { el('books-card').classList.add('hidden'); return; }
        await L.loadBooks();
        el('books-card').classList.remove('hidden');
    },

    // ---------- ספרים ----------
    async loadBooks() {
        const { books } = await api(`/api/ledger/companies/${L.company}/books`);
        el('books-row').innerHTML =
            books.map(b => `<button class="year-btn" data-b="${b.id}"
                onclick="L.selectBook(${b.id})">${b.year}</button>`).join('') +
            `<button class="btn-ok" onclick="L.openNewBook()">+ ספר חדש</button>`;
    },

    openNewBook() { el('nb-year').value = new Date().getFullYear(); el('dlg-book').showModal(); },

    async createBook() {
        const year = parseInt(el('nb-year').value, 10);
        if (!year) { toast('הזן שנה'); return; }
        try {
            const b = await api(`/api/ledger/companies/${L.company}/books`, {
                method: 'POST', headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ year }),
            });
            el('dlg-book').close();
            toast('הספר נוצר');
            await L.loadBooks();
            L.selectBook(b.id);
        } catch (e) { toast(e.message); }
    },

    async selectBook(bid) {
        L.bookId = bid;
        L.book = await api(`/api/ledger/books/${bid}`);
        document.querySelectorAll('.year-btn').forEach(b =>
            b.classList.toggle('active', b.dataset.b == bid));
        el('book-title').textContent =
            `${L.book.company_name} — ספר ${L.book.year}`;
        // חוצצים: סינון + datalist קטגוריות
        const dopts = '<option value="">כל החוצצים</option>' +
            L.book.dividers.map(d => `<option value="${d.id}">${esc(d.name)}</option>`).join('');
        el('divider-filter').innerHTML = dopts;
        el('cat-list').innerHTML = L.book.categories.map(c => `<option value="${esc(c)}">`).join('');
        el('book-card').classList.remove('hidden');
        await L.loadDocuments();
    },

    // ---------- מסמכים ----------
    async loadDocuments() {
        if (!L.bookId) return;
        const p = new URLSearchParams();
        if (el('q').value.trim()) p.set('q', el('q').value.trim());
        if (el('from').value) p.set('date_from', el('from').value);
        if (el('to').value) p.set('date_to', el('to').value);
        if (el('divider-filter').value) p.set('divider_id', el('divider-filter').value);
        const { documents } = await api(`/api/ledger/books/${L.bookId}/documents?${p}`);
        L.renderDocuments(documents);
    },

    clearSearch() {
        el('q').value = ''; el('from').value = ''; el('to').value = '';
        el('divider-filter').value = '';
        L.loadDocuments();
    },

    renderDocuments(docs) {
        const box = el('documents');
        if (!docs.length) {
            box.innerHTML = '<p class="muted">אין מסמכים בספר זה.</p>';
            return;
        }
        let html = '', curMonth = '';
        for (const d of docs) {
            const eff = d.document_date || d.scan_date || '';
            const ym = eff.slice(0, 7);
            if (ym !== curMonth) {
                if (curMonth) html += '</div>';
                curMonth = ym;
                const [y, m] = ym.split('-');
                html += `<div class="month-group"><div class="month-head">${HEB_MONTHS[+m - 1] || ''} ${y}</div>`;
            }
            const tags = [];
            if (d.category1) tags.push(`<span class="tag">${esc(d.category1)}</span>`);
            if (d.category2) tags.push(`<span class="tag">${esc(d.category2)}</span>`);
            if (d.divider_name) tags.push(`<span class="tag divider">${esc(d.divider_name)}</span>`);
            const scanBadge = d.date_source === 'scan'
                ? '<span class="badge-scan"> (תאריך סריקה)</span>' : '';
            html += `<div class="doc">
                <span class="date">${eff}${scanBadge}</span>
                <span class="title">${esc(d.title || d.original_filename || 'מסמך')}</span>
                ${tags.join(' ')}
                <span class="actions">
                    <button class="btn-light btn-sm" onclick="window.open('/api/ledger/documents/${d.id}/file')">צפה</button>
                    <button class="btn-light btn-sm" onclick="L.editDoc(${d.id})">ערוך</button>
                    ${d.invoice_id ? `<button class="btn-light btn-sm" style="color:#2563eb" onclick="L.restoreInvoice(${d.id})">החזר לרשימה</button>` : ''}
                </span></div>`;
        }
        html += '</div>';
        box.innerHTML = html;
    },

    async upload(input) {
        const file = input.files[0];
        if (!file) return;
        el('upload-status').textContent = '⏳ מעלה ומזהה תאריך…';
        const fd = new FormData();
        fd.append('file', file);
        try {
            await api(`/api/ledger/books/${L.bookId}/documents`, { method: 'POST', body: fd });
            el('upload-status').textContent = '';
            toast('המסמך הועלה');
            L.book = await api(`/api/ledger/books/${L.bookId}`);  // רענון קטגוריות
            el('cat-list').innerHTML = L.book.categories.map(c => `<option value="${esc(c)}">`).join('');
            await L.loadDocuments();
        } catch (e) {
            el('upload-status').textContent = '';
            toast(e.message);
        }
        input.value = '';
    },

    editDoc(id) {
        L.editingDocId = id;
        // טעינת פרטי המסמך מרשימת המסמכים של הספר
        fetch(`/api/ledger/books/${L.bookId}/documents`).then(r => r.json()).then(({ documents }) => {
            const doc = documents.find(x => x.id === id);
            if (!doc) { toast('המסמך לא נמצא'); return; }
            el('ed-title').value = doc.title || '';
            el('ed-date').value = doc.document_date || doc.scan_date || '';
            el('ed-datesrc').textContent = doc.date_source === 'scan' ? '(זוהה כתאריך סריקה)' : '(זוהה מהמסמך)';
            el('ed-cat1').value = doc.category1 || '';
            el('ed-cat2').value = doc.category2 || '';
            el('ed-divider').innerHTML = '<option value="">— ללא —</option>' +
                L.book.dividers.map(v => `<option value="${v.id}"${v.id === doc.divider_id ? ' selected' : ''}>${esc(v.name)}</option>`).join('');
            el('dlg-doc').showModal();
        });
    },

    async saveDoc() {
        try {
            await api(`/api/ledger/documents/${L.editingDocId}`, {
                method: 'POST', headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({
                    title: el('ed-title').value,
                    document_date: el('ed-date').value,
                    category1: el('ed-cat1').value,
                    category2: el('ed-cat2').value,
                    divider_id: el('ed-divider').value ? parseInt(el('ed-divider').value, 10) : null,
                }),
            });
            el('dlg-doc').close();
            toast('נשמר');
            L.book = await api(`/api/ledger/books/${L.bookId}`);
            el('cat-list').innerHTML = L.book.categories.map(c => `<option value="${esc(c)}">`).join('');
            await L.loadDocuments();
        } catch (e) { toast(e.message); }
    },

    async restoreInvoice(docId) {
        if (!confirm('להחזיר חשבונית זו לרשימת חשבוניות ספק?')) return;
        try {
            const res = await fetch(`/api/ledger/documents/${docId}/restore-invoice`, { method: 'POST' });
            const data = await res.json();
            if (!res.ok) { toast(data.detail || 'שגיאה'); return; }
            toast('החשבונית הוחזרה לרשימה');
            await L.loadDocuments();
        } catch (e) { toast(e.message); }
    },

    async deleteDoc() {
        if (!confirm('למחוק את המסמך?')) return;
        try {
            await api(`/api/ledger/documents/${L.editingDocId}`, { method: 'DELETE' });
            el('dlg-doc').close();
            toast('המסמך נמחק');
            await L.loadDocuments();
        } catch (e) { toast(e.message); }
    },

    // ---------- חוצצים ----------
    openDividers() {
        L.renderDividers();
        el('nd-name').value = '';
        el('dlg-dividers').showModal();
    },

    renderDividers() {
        el('dividers-list').innerHTML = L.book.dividers.length
            ? L.book.dividers.map(d => `<div class="row" style="justify-content:space-between;padding:4px 0">
                <span>${esc(d.name)}</span>
                <button class="btn-danger btn-sm" onclick="L.deleteDivider(${d.id})">מחק</button></div>`).join('')
            : '<p class="muted">אין חוצצים. (לא חובה)</p>';
    },

    async addDivider() {
        const name = el('nd-name').value.trim();
        if (!name) return;
        try {
            await api(`/api/ledger/books/${L.bookId}/dividers`, {
                method: 'POST', headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ name }),
            });
            L.book = await api(`/api/ledger/books/${L.bookId}`);
            el('nd-name').value = '';
            L.renderDividers();
            el('divider-filter').innerHTML = '<option value="">כל החוצצים</option>' +
                L.book.dividers.map(d => `<option value="${d.id}">${esc(d.name)}</option>`).join('');
        } catch (e) { toast(e.message); }
    },

    async deleteDivider(id) {
        try {
            await api(`/api/ledger/dividers/${id}`, { method: 'DELETE' });
            L.book = await api(`/api/ledger/books/${L.bookId}`);
            L.renderDividers();
            el('divider-filter').innerHTML = '<option value="">כל החוצצים</option>' +
                L.book.dividers.map(d => `<option value="${d.id}">${esc(d.name)}</option>`).join('');
            await L.loadDocuments();
        } catch (e) { toast(e.message); }
    },
};

function esc(s) {
    return String(s == null ? '' : s)
        .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
        .replace(/"/g, '&quot;');
}

// חיפוש בהקלדה (Enter)
document.addEventListener('DOMContentLoaded', () => {
    L.init();
    el('q').addEventListener('keydown', e => { if (e.key === 'Enter') L.loadDocuments(); });
});
