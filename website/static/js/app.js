/**
 * SupplierInvoice — לוגיקת ממשק: upload, polling, approve
 */
const app = {
    currentFilter: { status: '', source: '' },
    currentInvoice: null,
    pollingInterval: null,
    isFullscreen: false,
    highlightDrag: null,  // state for drag/resize
    activeHighlight: 'supplier',  // 'supplier' | 'customer'

    // === אתחול ===
    init() {
        this.setupDropZone();
        this.setupFileInput();
        this.loadInvoices();
        this.loadSyncStatus();
        this._setupKeyboardShortcuts();
        // polling כל 5 שניות
        this.pollingInterval = setInterval(() => this.loadInvoices(), 5000);
    },

    // === העלאת קבצים ===
    setupDropZone() {
        const zone = document.getElementById('drop-zone');
        zone.addEventListener('dragover', (e) => {
            e.preventDefault();
            zone.classList.add('drag-over');
        });
        zone.addEventListener('dragleave', () => {
            zone.classList.remove('drag-over');
        });
        zone.addEventListener('drop', (e) => {
            e.preventDefault();
            zone.classList.remove('drag-over');
            const files = e.dataTransfer.files;
            if (files.length > 0) this.uploadFile(files[0]);
        });
    },

    setupFileInput() {
        const input = document.getElementById('file-input');
        input.addEventListener('change', () => {
            if (input.files.length > 0) {
                this.uploadFile(input.files[0]);
                input.value = '';
            }
        });
    },

    async uploadFile(file) {
        const formData = new FormData();
        formData.append('file', file);

        this.showToast('מעלה חשבונית...', 'info');

        try {
            const res = await fetch('/api/invoices/upload', {
                method: 'POST',
                body: formData,
            });
            const data = await res.json();
            if (res.ok) {
                this.showToast('החשבונית הועלתה ונכנסה לעיבוד', 'success');
                this.loadInvoices();
            } else {
                this.showToast(data.detail || 'שגיאה בהעלאה', 'error');
            }
        } catch (err) {
            this.showToast('שגיאת תקשורת', 'error');
        }
    },

    // === טעינת רשימה ===
    async loadInvoices() {
        try {
            // טוענים את הכל פעם אחת — סופרים לפי לשונית ומסננים בצד-לקוח
            const res = await fetch('/api/invoices');
            const data = await res.json();
            const all = data.invoices || [];

            // עדכון מספר בכל לשונית — מסונן לפי data-status + data-source
            const matches = (inv, status, source) =>
                (!status || inv.status === status) &&
                (!source || inv.source === source);

            document.querySelectorAll('.tab').forEach(tab => {
                const status = tab.dataset.status || '';
                const source = tab.dataset.source || '';
                const base = tab.dataset.label || tab.textContent.replace(/\s*\(\d+\)\s*$/, '');
                tab.dataset.label = base;
                const n = all.filter(inv => matches(inv, status, source)).length;
                tab.textContent = `${base} (${n})`;
            });

            // הצגה — לפי הסינון הנוכחי
            const cf = this.currentFilter || { status: '', source: '' };
            const filtered = all.filter(inv => matches(inv, cf.status, cf.source));
            this.renderInvoiceList(filtered);
        } catch (err) {
            // שגיאה שקטה — ננסה שוב ב-polling הבא
        }
    },

    renderInvoiceList(invoices) {
        const container = document.getElementById('invoice-list');

        if (invoices.length === 0) {
            container.innerHTML = '<tr><td colspan="8" class="empty-state">אין חשבוניות להצגה</td></tr>';
            return;
        }

        const statusLabels = {
            pending_approval: 'ממתין לאישור',
            pending_extraction: 'ממתין לפענוח',
            pending_submission: 'ממתין לקליטה',
            pending_filing: 'ממתין לתיוק',
            on_hold: 'בהמתנה',
            cancelled: 'בוטל',
        };

        const fmt = (n) => n != null && n !== 0 ? `₪${Number(n).toLocaleString('he-IL', {minimumFractionDigits:2, maximumFractionDigits:2})}` : '—';

        // ריבוע פענוח — ✓ הצליח · ✗ נכשל · ריק טרם בוצע
        const extractBox = (ok) => {
            const sym = ok === true ? '✓' : ok === false ? '✗' : '';
            const col = ok === true ? 'var(--success)' : ok === false ? 'var(--danger)' : 'var(--border)';
            return `<span style="display:inline-flex;width:20px;height:20px;border:1.5px solid ${col};`
                + `border-radius:4px;align-items:center;justify-content:center;color:${col};font-weight:700">${sym}</span>`;
        };

        container.innerHTML = invoices.map(inv => {
            const d = inv.extracted_data;
            const supplierName = d?.supplier?.name || 'טרם נותח';
            const invoiceNum = d?.invoice_number || '—';
            const beforeVat = d?.subtotal ?? null;
            const afterVat = d?.total_amount ?? null;
            const date = inv.created_at
                ? new Date(inv.created_at).toLocaleDateString('he-IL')
                : '';

            return `
                <tr onclick="app.openInvoice('${inv.id}')">
                    <td>${supplierName}</td>
                    <td>${invoiceNum}</td>
                    <td class="col-amount">${fmt(beforeVat)}</td>
                    <td class="col-amount">${fmt(afterVat)}</td>
                    <td>${date}</td>
                    <td style="text-align:center">${extractBox(inv.extraction_ok)}</td>
                    <td><span class="status-badge status-${inv.status}">${statusLabels[inv.status] || inv.status}</span></td>
                    <td><button class="btn-delete-row" title="מחק חשבונית" onclick="event.stopPropagation(); app.deleteInvoiceById('${inv.id}')">🗑</button></td>
                </tr>
            `;
        }).join('');
    },

    // === סינון ===
    filterByStatus(btn, status, source) {
        document.querySelectorAll('.tab').forEach(t => t.classList.remove('active'));
        btn.classList.add('active');
        this.currentFilter = { status: status || '', source: source || '' };
        this.loadInvoices();
    },

    // === פתיחת חשבונית (מודאל) ===
    async openInvoice(id) {
        try {
            const res = await fetch(`/api/invoices/${id}`);
            const invoice = await res.json();
            this.currentInvoice = invoice;

            const modal = document.getElementById('invoice-modal');
            modal.style.display = 'flex';
            this.toggleFullscreen(true);

            // render אחרי שה-modal visible כדי שה-iframe יטען
            this.renderModal(invoice);
            this.renderTransactionPreview(invoice);

            // אתחול ריבועים אחרי שהתמונה/PDF נטענת
            setTimeout(() => {
                this.initHighlightBox(invoice, 'supplier');
                this.initHighlightBox(invoice, 'customer');
                this.initHighlightBox(invoice, 'allocation');
                this.setActiveHighlight('supplier');
            }, 500);
        } catch (err) {
            this.showToast('שגיאה בטעינת חשבונית', 'error');
        }
    },

    renderModal(inv) {
        // Preview — PDF ב-iframe, תמונות ב-img
        const iframe = document.getElementById('file-preview-iframe');
        const img = document.getElementById('file-preview-img');
        const fileUrl = `/api/invoices/${inv.id}/file`;
        const ext = (inv.file_path || '').split('.').pop().toLowerCase();

        if (ext === 'pdf') {
            iframe.src = fileUrl;
            iframe.style.display = 'block';
            img.style.display = 'none';
            img.src = '';
        } else {
            img.src = fileUrl;
            img.style.display = 'block';
            iframe.style.display = 'none';
            iframe.src = '';
        }

        // Extracted Data
        const dataDiv = document.getElementById('extracted-data');
        const d = inv.extracted_data;

        if (!d) {
            let msg = '<p style="color:var(--text-muted)">הנתונים טרם נותחו</p>';
            if (inv.error_message) {
                msg += `<p style="color:var(--danger);font-size:0.85rem">שגיאה: ${inv.error_message}</p>`;
            }
            dataDiv.innerHTML = msg;
            const actionBtns = document.getElementById('action-buttons');
            actionBtns.style.display = 'flex';
            actionBtns.innerHTML = `
                <button class="btn" style="background:var(--text-muted);color:white" onclick="app.deleteInvoice()">🗑 מחק</button>
            `;
            document.getElementById('validation-warnings').style.display = 'none';
            return;
        }

        const supMatch = d.supplier?.priority_match_found ? '<span class="match-ok">✓</span>' : '<span class="match-fail">✗</span>';
        const custMatch = d.customer?.priority_match_found ? '<span class="match-ok">✓</span>' : '';
        const supTax = d.supplier?.tax_id_type || 'ח.פ/ע.מ';
        const custTax = d.customer?.tax_id_type || 'ח.פ/ע.מ';

        const ef = (path, val) => `<input class="edit-field" data-path="${path}" value="${(val || '').replace(/"/g, '&quot;')}" />`;
        const efAc = (path, val, endpoint) => {
            const v = (val || '').replace(/"/g, '&quot;');
            return `<span class="ac-field"><input class="edit-field ac-input" data-path="${path}" data-ep="${endpoint}" value="${v}" autocomplete="off" spellcheck="false" /><ul class="ac-dd"></ul></span>`;
        };

        // === Validation helpers ===
        const checkIcon = (ok) => ok ? '<span class="match-ok" title="תקין">✓</span>' : '<span class="match-fail" title="לא תואם">✗</span>';
        const approxEq = (a, b) => Math.abs((a || 0) - (b || 0)) < 0.5;

        // Validate line totals
        const lineChecks = (d.lines || []).map(line => {
            const qty = parseFloat(line.quantity) || 0;
            const price = parseFloat(line.unit_price) || 0;
            const total = parseFloat(line.total_price) || 0;
            return (qty === 0 && price === 0) || approxEq(qty * price, total);
        });

        // Validate invoice totals
        const linesSum = (d.lines || []).reduce((s, l) => s + (parseFloat(l.total_price) || 0), 0);
        const subtotal = parseFloat(d.subtotal) || 0;
        const vatAmt = parseFloat(d.vat_amount) || 0;
        const totalAmt = parseFloat(d.total_amount) || 0;
        const subtotalOk = approxEq(linesSum, subtotal) || linesSum === 0;
        const totalOk = approxEq(subtotal + vatAmt, totalAmt);

        let html = `
            <h4 class="section-header" style="color:var(--accent)">📄 פרטי חשבונית</h4>
            <div class="data-row">
                <div class="data-field"><span class="label">חשבונית</span>${ef('invoice_number', d.invoice_number)}</div>
                <div class="data-field"><span class="label">תאריך</span>${ef('invoice_date', d.invoice_date)}</div>
                <div class="data-field"><span class="label">הקצאה</span>${ef('allocation_number', d.allocation_number)}</div>
            </div>

            <h4 class="section-header" style="color:var(--accent)">📦 ספק ${supMatch}</h4>
            <div class="data-row">
                <div class="data-field" style="flex:2"><span class="label">שם</span>${ef('supplier.name', d.supplier?.name)}</div>
                <div class="data-field"><span class="label">${supTax}</span>${ef('supplier.tax_id', d.supplier?.tax_id)}</div>
            </div>
            <div class="data-row">
                <div class="data-field"><span class="label">פריורטי</span>${ef('supplier.priority_supplier_code', d.supplier?.priority_supplier_code)}</div>
                <div class="data-field" style="flex:2"><span class="label">כתובת</span>${ef('supplier.address', d.supplier?.address)}</div>
            </div>
            <div class="data-row">
                <div class="data-field" style="flex:1"><span class="label">חשבון הוצאות</span>${efAc('expense_account', d.expense_account, '/api/db/accounts/search')}</div>
                <div class="data-field" style="flex:2"></div>
            </div>

            <h4 class="section-header" style="color:#1e40af">🏢 לקוח ${custMatch}</h4>
            <div class="data-row">
                <div class="data-field" style="flex:2"><span class="label">שם</span>${ef('customer.name', d.customer?.name)}</div>
                <div class="data-field"><span class="label">${custTax}</span>${ef('customer.tax_id', d.customer?.tax_id)}</div>
            </div>
            <div class="data-row">
                <div class="data-field"><span class="label">פריורטי</span>${ef('customer.priority_customer_code', d.customer?.priority_customer_code)}</div>
                <div class="data-field"><span class="label">סניף</span>${efAc('customer.branch', d.customer?.branch, '/api/db/branches/search')}</div>
                <div class="data-field" style="flex:2"><span class="label">כתובת</span>${ef('customer.address', d.customer?.address)}</div>
            </div>
        `;

        // שורות חשבונית
        if (d.lines && d.lines.length > 0) {
            html += `
                <h4 style="margin-top:12px; color:var(--text-secondary)">שורות חשבונית</h4>
                <table class="lines-table">
                    <thead>
                        <tr>
                            <th>#</th>
                            <th style="min-width:200px">תיאור</th>
                            <th>מק"ט</th>
                            <th>כמות</th>
                            <th>מחיר</th>
                            <th>סה"כ</th>
                            <th style="width:30px"></th>
                        </tr>
                    </thead>
                    <tbody>
            `;
            for (let i = 0; i < d.lines.length; i++) {
                const line = d.lines[i];
                const lf = (field, val) => `<input class="edit-field edit-line" data-path="lines.${i}.${field}" value="${(String(val ?? '')).replace(/"/g, '&quot;')}" />`;
                html += `
                    <tr>
                        <td>${line.line_number}</td>
                        <td style="min-width:200px">${lf('description', line.description)}</td>
                        <td>${lf('catalog_number', line.catalog_number)}</td>
                        <td>${lf('quantity', line.quantity)}</td>
                        <td>${lf('unit_price', line.unit_price)}</td>
                        <td>${lf('total_price', line.total_price)}</td>
                        <td>${checkIcon(lineChecks[i])}</td>
                    </tr>
                `;
            }
            html += '</tbody></table>';
        }

        // סכומים — מיושרים לימין כמו בחשבונית
        html += `
            <div class="totals-block">
                <div class="totals-row"><span class="totals-label">סה"כ ${checkIcon(subtotalOk)}</span><span class="totals-value">₪${subtotal.toLocaleString()}</span></div>
                <div class="totals-row"><span class="totals-label">מע"מ</span><span class="totals-value">₪${vatAmt.toLocaleString()}</span></div>
                <div class="totals-row totals-grand"><span class="totals-label">סה"כ כולל מע"מ ${checkIcon(totalOk)}</span><span class="totals-value">₪${totalAmt.toLocaleString()}</span></div>
            </div>
            <div class="data-field"><span class="label">רמת ביטחון</span><span class="value">${Math.round((d.confidence_score || 0) * 100)}%</span></div>
        `;

        dataDiv.innerHTML = html;

        // שמירה אוטומטית כשיוצאים משדה
        dataDiv.querySelectorAll('.edit-field').forEach(input => {
            input.addEventListener('change', () => this.saveFieldEdit(input));
        });

        // אתחול שדות autocomplete
        this._setupAcFields(dataDiv);

        // הסתרת הזהרות
        document.getElementById('validation-warnings').style.display = 'none';

        // Notes
        document.getElementById('user-notes').value = inv.user_notes || '';

        // כפתורי פעולה לפי סטטוס + תווית הסטטוס
        const actionBtns = document.getElementById('action-buttons');
        const statusDisplay = document.getElementById('status-display');
        const s = inv.status;
        const showBtn = (id, on) => {
            const el = document.getElementById(id);
            if (el) el.style.display = on ? '' : 'none';
        };

        actionBtns.style.display = 'flex';
        showBtn('btn-approve-intake', s === 'pending_approval');
        showBtn('btn-extract', s === 'pending_extraction');
        showBtn('btn-submit', s === 'pending_submission');
        showBtn('btn-file', s === 'pending_filing');
        showBtn('btn-restore', s === 'on_hold' || s === 'cancelled');
        showBtn('btn-hold', s !== 'on_hold' && s !== 'cancelled');
        showBtn('btn-cancel', s !== 'cancelled');
        // btn-delete-modal — תמיד גלוי

        const statusLabels = {
            pending_approval: 'ממתין לאישור',
            pending_extraction: 'ממתין לפענוח',
            pending_submission: 'ממתין לקליטה',
            pending_filing: 'ממתין לתיוק',
            on_hold: 'בהמתנה',
            cancelled: 'בוטל',
        };
        let statusText = statusLabels[s] || s;
        if (inv.priority_invoice_id) statusText += ` · IVNUM: ${inv.priority_invoice_id}`;
        if (inv.priority_journal_id) statusText += ` · תנועת יומן: ${inv.priority_journal_id}`;
        statusDisplay.textContent = statusText;
        statusDisplay.className = `status-display status-badge status-${s}`;
        statusDisplay.style.display = 'block';
    },

    closeModal() {
        const modal = document.getElementById('modal-content');
        if (modal) modal.classList.remove('fullscreen');
        this.isFullscreen = false;
        this._cleanupHighlight();

        document.getElementById('invoice-modal').style.display = 'none';
        this.currentInvoice = null;
    },

    // תצוגה מקדימה של תנועת היומן שתיווצר בקליטה לפריורטי
    renderTransactionPreview(invoice) {
        const box = document.getElementById('transaction-preview');
        if (!box) return;

        const d = invoice && invoice.extracted_data;
        if (!d) { box.innerHTML = '<div style="color:var(--text-secondary);font-size:0.85rem">אין נתונים מחולצים</div>'; return; }

        const branch = ((d.customer && d.customer.branch) || '').trim();
        const sfx = branch ? '-' + branch : '';
        const subtotal = parseFloat(d.subtotal) || 0;
        const vat = parseFloat(d.vat_amount) || 0;
        const total = parseFloat(d.total_amount) || 0;
        const expenseAcc = (d.expense_account || '').trim();
        const supplierAcc = ((d.supplier && d.supplier.priority_supplier_code) || '').trim();
        const lines = Array.isArray(d.lines) ? d.lines.filter(l => l && (l.description || l.total_price)) : [];

        const money = n => parseFloat(n || 0).toLocaleString('he-IL', { minimumFractionDigits: 2, maximumFractionDigits: 2 });

        // שדה חשבון עם autocomplete
        const acInput = (path, val) => `<span class="ac-field" style="position:relative">
            <input class="edit-field ac-input tx-acc-input" data-path="${path}" data-ep="/api/db/accounts/search"
                value="${val}" placeholder="— חסר —" autocomplete="off" spellcheck="false"
                style="width:90px;font-size:0.82rem;padding:2px 5px;border:1px solid var(--border);border-radius:4px;background:var(--bg-primary);color:var(--text-primary)">
            <ul class="ac-dd"></ul></span>${sfx ? `<span style="font-size:0.8rem;color:var(--text-secondary)"> -${branch}</span>` : ''}`;

        // שדה סכום עריכה
        const amtInput = (path, val) => `<input class="edit-field tx-amt-input" data-path="${path}"
            value="${parseFloat(val) || 0}"
            style="width:80px;font-size:0.82rem;padding:2px 5px;border:1px solid var(--border);border-radius:4px;background:var(--bg-primary);color:var(--text-primary);text-align:left" type="number" step="0.01" min="0">`;

        // בניית שורות החובה — אם יש שורות חשבונית, נציג אותן; אחרת שורה אחת מסך
        let debitRows = '';
        let totalDebit = 0;

        if (lines.length > 0) {
            // שורה לכל פריט בחשבונית
            lines.forEach((ln, i) => {
                const amt = parseFloat(ln.total_price || ln.unit_price || 0);
                totalDebit += amt;
                const desc = ln.description || `שורה ${i + 1}`;
                debitRows += `<tr>
                    <td>${acInput('expense_account', expenseAcc)}</td>
                    <td style="font-size:0.82rem;max-width:160px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap" title="${desc}">${desc}</td>
                    <td>₪${money(amt)}</td><td></td></tr>`;
            });
            // אם סכום שורות שונה מ-subtotal — נציג גם subtotal לעריכה
            if (Math.abs(totalDebit - subtotal) > 0.01) {
                debitRows += `<tr style="font-size:0.78rem;color:var(--text-secondary)">
                    <td colspan="2">סכום שורות vs. סיכום: ${money(totalDebit)} / ${money(subtotal)}</td><td></td><td></td></tr>`;
            }
        } else {
            // שורה אחת עם סכום ניתן לעריכה
            totalDebit = subtotal;
            debitRows = `<tr>
                <td>${acInput('expense_account', expenseAcc)}</td>
                <td>הוצאות</td>
                <td>${amtInput('subtotal', subtotal)}</td><td></td></tr>`;
        }

        const vatRow = vat > 0 ? `<tr>
            <td style="font-size:0.82rem">205-2${sfx}</td><td>מע"מ תשומות</td>
            <td>${amtInput('vat_amount', vat)}</td><td></td></tr>` : '';

        const dr = (lines.length > 0 ? totalDebit : subtotal) + vat;
        const cr = total;
        const balanced = Math.abs(dr - cr) < 0.01;

        let warn = '';
        if (!branch) warn = '⚠ לא זוהה סניף ללקוח — קודי החשבון חסרים את סיומת הסניף.';
        else if (!expenseAcc) warn = '⚠ לא הוזן חשבון הוצאות.';
        else if (!supplierAcc) warn = '⚠ הספק לא זוהה בפריורטי.';

        box.innerHTML = `
            <h4 style="margin:0 0 6px;color:var(--accent)">תנועת יומן — תצוגה מקדימה</h4>
            <div style="font-size:0.8rem;color:var(--text-secondary);margin-bottom:8px">
                סוג תנועה: חשבונית ספק · סניף: ${branch || '—'}
                ${lines.length > 0 ? ` · <span style="color:var(--success)">${lines.length} שורות פריטים</span>` : ''}</div>
            ${warn ? `<div style="color:var(--danger);font-size:0.82rem;margin-bottom:8px">${warn}</div>` : ''}
            <table class="invoice-table" style="width:100%">
                <thead><tr><th>חשבון</th><th>תיאור</th><th>חובה</th><th>זכות</th></tr></thead>
                <tbody>
                    ${debitRows}
                    ${vatRow}
                    <tr>
                        <td>${acInput('supplier.priority_supplier_code', supplierAcc)}</td>
                        <td>${(d.supplier && d.supplier.name) || 'ספק'}</td>
                        <td></td><td>${amtInput('total_amount', total)}</td></tr>
                    <tr style="font-weight:700;border-top:2px solid var(--border)">
                        <td colspan="2">סה"כ</td>
                        <td>₪${money(dr)}</td><td>₪${money(cr)}</td></tr>
                </tbody>
            </table>
            <div style="font-size:0.8rem;margin-top:6px;color:${balanced ? 'var(--success)' : 'var(--danger)'}">
                ${balanced ? '✓ התנועה מאוזנת' : '⚠ התנועה אינה מאוזנת — בדוק את הסכומים'}</div>`;

        box.querySelectorAll('.edit-field').forEach(input => {
            input.addEventListener('change', () => this.saveFieldEdit(input));
        });
        this._setupAcFields(box);
    },

    toggleFullscreen(forceOn = null) {
        const modal = document.getElementById('modal-content');
        const icon = document.getElementById('expand-icon');
        this.isFullscreen = forceOn !== null ? forceOn : !this.isFullscreen;
        if (this.isFullscreen) {
            modal.classList.add('fullscreen');
            icon.textContent = '✕';
        } else {
            modal.classList.remove('fullscreen');
            icon.textContent = '⛶';
        }
    },

    // === קיצור מקלדת Ctrl+A לפענוח ===
    _setupKeyboardShortcuts() {
        document.addEventListener('keydown', (e) => {
            // Ctrl+A (אנגלית או עברית — keyCode 65 = 'a'/'ש')
            if (e.ctrlKey && (e.key === 'a' || e.key === 'A' || e.key === 'ש' || e.key === 'Ш' || e.keyCode === 65)) {
                const modal = document.getElementById('invoice-modal');
                if (modal && modal.style.display !== 'none') {
                    const activeBox = document.getElementById(`highlight-box-${this.activeHighlight}`);
                    if (activeBox && activeBox.style.display !== 'none') {
                        e.preventDefault();
                        this.testOcrCrop(this.activeHighlight);
                    }
                }
            }
        });
    },

    setActiveHighlight(target) {
        this.activeHighlight = target;
        for (const t of ['supplier', 'customer', 'allocation']) {
            const b = document.getElementById(`highlight-box-${t}`);
            if (b) b.classList.toggle('active', t === target);
        }
        const btn = document.getElementById('btn-ocr-test');
        if (btn) {
            const labels = { supplier: '🔍 קרא ספק', customer: '🔍 קרא לקוח', allocation: '🔍 קרא הקצאה' };
            btn.textContent = labels[target] || '🔍 קרא';
        }
    },

    // === ריבועי זיהוי — drag & resize ===
    initHighlightBox(inv, target = 'supplier') {
        const box = document.getElementById(`highlight-box-${target}`);
        const container = document.getElementById('preview-container');
        if (!box || !container) return;

        const d = inv.extracted_data;

        box.style.display = 'block';
        box.style.right = 'auto';

        // labels
        if (target === 'supplier') {
            const info = d?.supplier;
            if (info && info.tax_id) {
                const taxType = info.tax_id_type || 'ח.פ/ע.מ';
                box.querySelector('.highlight-label').textContent = `${taxType} ספק: ${info.tax_id}`;
            } else {
                box.querySelector('.highlight-label').textContent = '📌 ספק';
            }
        } else if (target === 'customer') {
            const info = d?.customer;
            if (info && info.tax_id) {
                const taxType = info.tax_id_type || 'ח.פ/ע.מ';
                box.querySelector('.highlight-label').textContent = `${taxType} לקוח: ${info.tax_id}`;
            } else {
                box.querySelector('.highlight-label').textContent = '📌 לקוח';
            }
        } else if (target === 'allocation') {
            const allocNum = d?.allocation_number;
            box.querySelector('.highlight-label').textContent = allocNum
                ? `הקצאה: ${allocNum}` : '📌 הקצאה';
        }

        // מיקום — כולם בתחתית אחד ליד השני
        const positions = { supplier: '5%', customer: '35%', allocation: '65%' };
        box.style.bottom = '8px';
        box.style.top = 'auto';
        box.style.left = positions[target] || '5%';
        box.style.width = '28%';
        box.style.height = '3%';

        // לחיצה על ריבוע = בחירה
        box.onclick = (e) => {
            if (!e.target.classList.contains('resize-handle-br')) {
                this.setActiveHighlight(target);
            }
        };

        // Drag
        this._setupDrag(box, container);
        // Resize
        this._setupResize(box, container);
    },

    _setupDrag(box, container) {
        let startX, startY, startLeft, startTop;

        const onMouseDown = (e) => {
            if (e.target.classList.contains('resize-handle-br')) return;
            e.preventDefault();
            const rect = container.getBoundingClientRect();
            startX = e.clientX;
            startY = e.clientY;
            startLeft = box.offsetLeft;
            startTop = box.offsetTop;

            const onMouseMove = (e) => {
                const dx = e.clientX - startX;
                const dy = e.clientY - startY;
                let newLeft = startLeft + dx;
                let newTop = startTop + dy;
                // bounds
                newLeft = Math.max(0, Math.min(newLeft, container.clientWidth - box.offsetWidth));
                newTop = Math.max(0, Math.min(newTop, container.clientHeight - box.offsetHeight));
                box.style.left = newLeft + 'px';
                box.style.right = 'auto';
                box.style.top = newTop + 'px';
            };

            const onMouseUp = () => {
                document.removeEventListener('mousemove', onMouseMove);
                document.removeEventListener('mouseup', onMouseUp);
            };
            document.addEventListener('mousemove', onMouseMove);
            document.addEventListener('mouseup', onMouseUp);
        };

        box._dragHandler = onMouseDown;
        box.addEventListener('mousedown', onMouseDown);
    },

    _setupResize(box, container) {
        const handle = box.querySelector('.resize-handle-br');
        if (!handle) return;

        const onMouseDown = (e) => {
            e.preventDefault();
            e.stopPropagation();
            const startX = e.clientX;
            const startY = e.clientY;
            const startW = box.offsetWidth;
            const startH = box.offsetHeight;
            const startLeft = box.offsetLeft;

            const onMouseMove = (e) => {
                const dx = e.clientX - startX;
                const dy = e.clientY - startY;

                // כיוון RTL: גרירה ימינה מקטינה רוחב, שמאלה מגדילה
                // הנקודה בצד ימין-למטה, אז שינוי רוחב = הזזת הצד הימני
                // צד שמאל (left) נשאר קבוע, רוחב משתנה עם dx
                let newW = startW + dx;
                let newH = startH + dy;

                newW = Math.max(40, Math.min(newW, container.clientWidth - startLeft));
                newH = Math.max(20, Math.min(newH, container.clientHeight - box.offsetTop));

                box.style.width = newW + 'px';
                box.style.height = newH + 'px';
            };

            const onMouseUp = () => {
                document.removeEventListener('mousemove', onMouseMove);
                document.removeEventListener('mouseup', onMouseUp);
            };
            document.addEventListener('mousemove', onMouseMove);
            document.addEventListener('mouseup', onMouseUp);
        };

        handle._resizeHandler = onMouseDown;
        handle.addEventListener('mousedown', onMouseDown);
    },

    _cleanupHighlight() {
        for (const target of ['supplier', 'customer', 'allocation']) {
            const box = document.getElementById(`highlight-box-${target}`);
            if (box && box._dragHandler) {
                box.removeEventListener('mousedown', box._dragHandler);
            }
            const handle = box?.querySelector('.resize-handle-br');
            if (handle && handle._resizeHandler) {
                handle.removeEventListener('mousedown', handle._resizeHandler);
            }
        }
    },

    // === פענוח חוזר ===
    _getHighlightCoordsRelativeToImage(target = null) {
        const t = target || this.activeHighlight;
        const box = document.getElementById(`highlight-box-${t}`);
        const img = document.getElementById('file-preview-img');
        const iframe = document.getElementById('file-preview-iframe');
        const isImage = (img && img.style.display !== 'none');
        const visibleEl = isImage ? img : iframe;

        if (!box || !visibleEl) return null;

        const boxRect = box.getBoundingClientRect();
        const elRect = visibleEl.getBoundingClientRect();

        if (isImage && img.naturalWidth && img.naturalHeight) {
            // object-fit:contain — חישוב האזור בו התמונה באמת מרונדרת
            const natW = img.naturalWidth;
            const natH = img.naturalHeight;
            const elW = elRect.width;
            const elH = elRect.height;

            const scale = Math.min(elW / natW, elH / natH);
            const renderedW = natW * scale;
            const renderedH = natH * scale;

            // offset של התמונה בתוך האלמנט (ריפוד מ-object-fit:contain)
            const offsetX = elRect.left + (elW - renderedW) / 2;
            const offsetY = elRect.top + (elH - renderedH) / 2;

            return {
                left: ((boxRect.left - offsetX) / renderedW) * 100,
                top: ((boxRect.top - offsetY) / renderedH) * 100,
                width: (boxRect.width / renderedW) * 100,
                height: (boxRect.height / renderedH) * 100,
            };
        }

        // fallback ל-iframe / PDF
        return {
            left: ((boxRect.left - elRect.left) / elRect.width) * 100,
            top: ((boxRect.top - elRect.top) / elRect.height) * 100,
            width: (boxRect.width / elRect.width) * 100,
            height: (boxRect.height / elRect.height) * 100,
        };
    },

    async testOcrCrop(target = 'supplier') {
        if (!this.currentInvoice) return;

        const cropCoords = this._getHighlightCoordsRelativeToImage(target);
        if (!cropCoords) {
            this.showToast('לא ניתן לחשב מיקום', 'error');
            return;
        }

        const btn = document.getElementById('btn-ocr-test');
        btn.disabled = true;
        btn.textContent = '⏳ קורא...';

        try {
            const res = await fetch(`/api/invoices/${this.currentInvoice.id}/ocr-crop`, {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ crop_coords: cropCoords, target }),
            });
            const data = await res.json();

            if (data.ocr_data) {
                const d = data.ocr_data;
                const targetLabels = { supplier: 'ספק', customer: 'לקוח', allocation: 'הקצאה' };
                const targetLabel = targetLabels[target] || target;

                const identified = (target === 'allocation' && d.allocation_number) || d.tax_id;

                if (target === 'allocation' && d.allocation_number) {
                    this.showToast(`זוהה מספר הקצאה: ${d.allocation_number}`, 'success');
                } else if (d.tax_id) {
                    let msg = `זוהה ${d.tax_id_type || 'מזהה'} ${targetLabel}: ${d.tax_id}`;
                    if (data.db_match) {
                        msg += ` — ${data.db_match.name} (קוד ${data.db_match.priority_code})`;
                    }
                    this.showToast(msg, 'success');
                } else {
                    this.showToast(`לא זוהה בחיתוך. טקסט: ${d.ocr_text || '(ריק)'}`, 'error');
                }

                // רענון תצוגה אם זוהה משהו
                if (identified) {
                    const savedPositions = {};
                    for (const t of ['supplier', 'customer', 'allocation']) {
                        const b = document.getElementById(`highlight-box-${t}`);
                        if (b) {
                            savedPositions[t] = {
                                left: b.style.left, top: b.style.top,
                                width: b.style.width, height: b.style.height,
                            };
                        }
                    }

                    const invRes = await fetch(`/api/invoices/${this.currentInvoice.id}`);
                    const updatedInv = await invRes.json();
                    this.currentInvoice = updatedInv;
                    this.renderModal(updatedInv);
                    this.renderTransactionPreview(updatedInv);

                    setTimeout(() => {
                        for (const t of ['supplier', 'customer', 'allocation']) {
                            this.initHighlightBox(updatedInv, t);
                            if (savedPositions[t]) {
                                const rb = document.getElementById(`highlight-box-${t}`);
                                if (rb) {
                                    rb.style.left = savedPositions[t].left;
                                    rb.style.top = savedPositions[t].top;
                                    rb.style.right = 'auto';
                                    rb.style.width = savedPositions[t].width;
                                    rb.style.height = savedPositions[t].height;
                                }
                            }
                        }
                        this.setActiveHighlight(target);
                    }, 300);
                }
            } else {
                this.showToast(`שגיאה: ${data.error || 'לא ידוע'}`, 'error');
            }
        } catch (err) {
            this.showToast('שגיאה בקריאת חיתוך', 'error');
        } finally {
            btn.disabled = false;
            const labels = { supplier: 'ספק', customer: 'לקוח', allocation: 'הקצאה' };
            btn.textContent = `🔍 קרא ${labels[this.activeHighlight] || ''}`;
        }
    },

    async reextractInvoice() {
        if (!this.currentInvoice) return;

        const cropCoords = this._getHighlightCoordsRelativeToImage('supplier');
        if (!cropCoords) {
            this.showToast('לא ניתן לחשב מיקום הריבוע', 'error');
            return;
        }

        // שמירת מיקום שני הריבועים
        const savedPositions = {};
        for (const t of ['supplier', 'customer', 'allocation']) {
            const b = document.getElementById(`highlight-box-${t}`);
            if (b) {
                savedPositions[t] = {
                    left: b.style.left, top: b.style.top,
                    width: b.style.width, height: b.style.height,
                };
            }
        }

        const btn = document.getElementById('btn-reextract');
        btn.disabled = true;
        btn.textContent = '⏳ מפענח...';

        try {
            const res = await fetch(`/api/invoices/${this.currentInvoice.id}/reextract`, {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ crop_coords: cropCoords }),
            });
            const data = await res.json();

            if (data.status === 'review' || data.extracted_data) {
                const invRes = await fetch(`/api/invoices/${this.currentInvoice.id}`);
                const updatedInv = await invRes.json();
                this.currentInvoice = updatedInv;
                this.renderModal(updatedInv);

                // שחזור שני הריבועים
                setTimeout(() => {
                    for (const t of ['supplier', 'customer', 'allocation']) {
                        this.initHighlightBox(updatedInv, t);
                        if (savedPositions[t]) {
                            const rb = document.getElementById(`highlight-box-${t}`);
                            if (rb) {
                                rb.style.left = savedPositions[t].left;
                                rb.style.top = savedPositions[t].top;
                                rb.style.right = 'auto';
                                rb.style.width = savedPositions[t].width;
                                rb.style.height = savedPositions[t].height;
                            }
                        }
                    }
                    this.setActiveHighlight(this.activeHighlight);
                }, 300);

                this.showToast('פענוח חוזר הושלם בהצלחה', 'success');
            } else {
                this.showToast(data.message || 'שגיאה בפענוח חוזר', 'error');
            }
        } catch (err) {
            this.showToast('שגיאה בפענוח חוזר', 'error');
        } finally {
            btn.disabled = false;
            btn.textContent = '🔄 פענוח חוזר';
        }
    },

    // === אישור / דחייה ===
    async fileToLedger() {
        if (!this.currentInvoice) return;
        try {
            const res = await fetch(`/api/invoices/${this.currentInvoice.id}/file-to-ledger`, {
                method: 'POST',
            });
            const data = await res.json();
            if (!res.ok) {
                this.showToast(data.detail || 'שגיאה בתיוק', 'error');
                return;
            }
            this.showToast(`תויק בספרי הנהלת חשבונות — ${data.branch} / ${data.year}`, 'success');
            this.closeModal();
            this.loadInvoices();
        } catch (err) {
            this.showToast('שגיאת תקשורת', 'error');
        }
    },

    async approveInvoice() {
        if (!this.currentInvoice) return;
        const notes = document.getElementById('user-notes').value;

        try {
            const res = await fetch(`/api/invoices/${this.currentInvoice.id}/approve`, {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ notes }),
            });
            const data = await res.json();
            if (!res.ok) {
                this.showToast(data.detail || data.message || 'שגיאה באישור', 'error');
                return;
            }
            if (data.status === 'pending_filing') {
                this.showToast('החשבונית נקלטה בפריורטי בהצלחה!', 'success');
            } else {
                this.showToast(data.message || 'שגיאה בקליטה בפריורטי', 'error');
            }
            this.closeModal();
            this.loadInvoices();
        } catch (err) {
            this.showToast('שגיאת תקשורת', 'error');
        }
    },

    async rejectInvoice() {
        if (!this.currentInvoice) return;
        const reason = document.getElementById('user-notes').value || 'נדחה על ידי המשתמש';

        try {
            const res = await fetch(`/api/invoices/${this.currentInvoice.id}/reject`, {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ reason }),
            });
            if (res.ok) {
                this.showToast('החשבונית נדחתה', 'info');
            }
            this.closeModal();
            this.loadInvoices();
        } catch (err) {
            this.showToast('שגיאת תקשורת', 'error');
        }
    },

    async deleteInvoice() {
        if (!this.currentInvoice) return;
        await this.deleteInvoiceById(this.currentInvoice.id, true);
    },

    async deleteInvoiceById(id, fromModal = false) {
        if (!confirm('האם למחוק את החשבונית? פעולה זו בלתי הפיכה.')) return;

        try {
            const res = await fetch(`/api/invoices/${id}`, { method: 'DELETE' });
            if (res.ok) {
                this.showToast('החשבונית נמחקה', 'info');
            }
            if (fromModal) this.closeModal();
            this.loadInvoices();
        } catch (err) {
            this.showToast('שגיאה במחיקה', 'error');
        }
    },

    // === סנכרון Priority ===
    async syncPriority() {
        const btn = document.getElementById('btn-sync');
        btn.disabled = true;
        btn.textContent = 'מסנכרן...';

        try {
            // /api/db/sync — מסנכרן ספקים, לקוחות ותתי-חברות (סניפים) ל-DB
            const res = await fetch('/api/db/sync', { method: 'POST' });
            const data = await res.json();
            this.showToast(
                `סנכרון הושלם — ${data.suppliers_synced} ספקים, ${data.customers_synced} לקוחות, ${data.branches_synced || 0} תתי-חברות`,
                'success'
            );
            this.loadSyncStatus();
        } catch (err) {
            this.showToast('שגיאה בסנכרון', 'error');
        } finally {
            btn.disabled = false;
            btn.textContent = 'סנכרון Priority';
        }
    },

    // משיכה ידנית של חשבוניות מתיבת המייל הייעודית
    async createFilingTest() {
        try {
            const res = await fetch('/api/test/create-filing-test', { method: 'POST' });
            const data = await res.json();
            if (!res.ok) { this.showToast(data.detail || 'שגיאה', 'error'); return; }
            this.showToast(data.message, 'success');
            this.loadInvoices();
        } catch (err) { this.showToast('שגיאת תקשורת', 'error'); }
    },

    async fetchEmail() {
        const btn = document.getElementById('btn-fetch-email');
        const orig = btn.textContent;
        btn.disabled = true;
        btn.textContent = '⏳ מושך…';
        try {
            const res = await fetch('/api/invoices/fetch-email', { method: 'POST' });
            const data = await res.json();
            if (res.ok) {
                this.showToast(data.fetched > 0
                    ? `נמשכו ${data.fetched} חשבוניות מהמייל — ממתינות לפענוח`
                    : 'אין חשבוניות חדשות במייל', data.fetched > 0 ? 'success' : 'info');
                this.loadInvoices();
            } else {
                this.showToast(data.detail || 'שגיאה במשיכה מהמייל', 'error');
            }
        } catch (err) {
            this.showToast('שגיאת תקשורת', 'error');
        } finally {
            btn.disabled = false;
            btn.textContent = orig;
        }
    },

    // הפעלת פענוח על חשבונית שממתינה לפענוח (נקלטה ממייל)
    async extractInvoice() {
        if (!this.currentInvoice) return;
        const btn = document.getElementById('btn-extract');
        btn.disabled = true;
        btn.textContent = '⏳ מפענח…';
        try {
            const res = await fetch(`/api/invoices/${this.currentInvoice.id}/extract`, { method: 'POST' });
            if (res.ok) {
                this.showToast('הפענוח החל', 'success');
                this.closeModal();
                this.loadInvoices();
            } else {
                const d = await res.json().catch(() => ({}));
                this.showToast(d.detail || 'שגיאה בהפעלת פענוח', 'error');
            }
        } catch (err) {
            this.showToast('שגיאת תקשורת', 'error');
        } finally {
            btn.disabled = false;
            btn.textContent = 'פענח';
        }
    },

    // שינוי סטטוס ידני — אישור / ביטול / העברה להמתנה / החזרה לתהליך
    async changeStatus(status, note) {
        if (!this.currentInvoice) return;
        try {
            const res = await fetch(`/api/invoices/${this.currentInvoice.id}/status`, {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ status, notes: note || '' }),
            });
            if (res.ok) {
                this.showToast(note || 'הסטטוס עודכן', 'success');
                this.closeModal();
                this.loadInvoices();
            } else {
                const d = await res.json().catch(() => ({}));
                this.showToast(d.detail || 'שגיאה בעדכון סטטוס', 'error');
            }
        } catch (err) {
            this.showToast('שגיאת תקשורת', 'error');
        }
    },

    async loadSyncStatus() {
        try {
            const res = await fetch('/api/db/stats');
            const data = await res.json();
            // עדכון label בתוך המודל
            const label = document.getElementById('sync-status-label');
            if (label && data.last_sync_at) {
                const date = new Date(data.last_sync_at).toLocaleString('he-IL');
                label.textContent = `סנכרון: ${date} | ${data.suppliers} ספקים, ${data.customers} לקוחות`;
            }
            // עדכון badge בדף הראשי
            const badge = document.getElementById('sync-status');
            if (badge && data.last_sync_at) {
                const date = new Date(data.last_sync_at).toLocaleString('he-IL');
                badge.textContent = `סנכרון: ${date}`;
            }
        } catch (err) {
            // שקט
        }
    },

    async saveFieldEdit(input) {
        if (!this.currentInvoice) return;
        const path = input.dataset.path;
        const value = input.value;

        try {
            const res = await fetch(`/api/invoices/${this.currentInvoice.id}/update-field`, {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ path, value }),
            });
            if (res.ok) {
                input.style.borderColor = '#4CAF50';
                setTimeout(() => input.style.borderColor = '', 1000);
                // עדכון מקומי
                const parts = path.split('.');
                let obj = this.currentInvoice.extracted_data;
                for (let i = 0; i < parts.length - 1; i++) obj = obj[parts[i]];
                obj[parts[parts.length - 1]] = value;
                // רענון תנועת יומן אם שונה שדה סכום
                const amountFields = ['subtotal', 'vat_amount', 'total_amount'];
                if (amountFields.includes(parts[parts.length - 1])) {
                    this.renderTransactionPreview(this.currentInvoice);
                }
            }
        } catch (err) {
            input.style.borderColor = '#f44336';
        }
    },

    async syncWithPriority() {
        const btn = document.getElementById('btn-sync');
        btn.disabled = true;
        btn.textContent = '⏳ מסנכרן...';

        try {
            const res = await fetch('/api/db/sync', { method: 'POST' });
            const data = await res.json();
            this.showToast(
                `סנכרון הושלם — ${data.suppliers_synced} ספקים, ${data.customers_synced} לקוחות, ${data.branches_synced || 0} חברות`,
                'success'
            );
            this.loadSyncStatus();
        } catch (err) {
            this.showToast('שגיאה בסנכרון', 'error');
        } finally {
            btn.disabled = false;
            btn.textContent = '🔄 סנכרן פריורטי';
        }
    },

    // === בדיקה לפני קליטה ===
    // === Autocomplete ===
    _setupAcFields(container) {
        container.querySelectorAll('.ac-input').forEach(input => {
            const dd = input.nextElementSibling;
            let timer = null;

            const positionDd = () => {
                const rect = input.getBoundingClientRect();
                dd.style.position = 'fixed';
                dd.style.top = (rect.bottom + 2) + 'px';
                dd.style.left = rect.left + 'px';
                dd.style.width = Math.max(240, rect.width) + 'px';
                dd.style.right = 'auto';
                dd.style.zIndex = '99999';
            };

            const search = async (q, showAll = false) => {
                if (!showAll && !q.trim()) { dd.style.display = 'none'; return; }
                try {
                    const res = await fetch(`${input.dataset.ep}?q=${encodeURIComponent(q)}`);
                    const data = await res.json();
                    const items = data.results || [];
                    if (!items.length) { dd.style.display = 'none'; return; }
                    dd.innerHTML = items.map(item => {
                        const code = item.account_code || item.branch_code || '';
                        const name = item.account_name || item.name || '';
                        const display = name ? `${code} — ${name}` : code;
                        return `<li data-val="${code.replace(/"/g, '&quot;')}">${display}</li>`;
                    }).join('');
                    positionDd();
                    dd.style.display = 'block';
                } catch { dd.style.display = 'none'; }
            };

            input.addEventListener('input', () => {
                clearTimeout(timer);
                timer = setTimeout(() => search(input.value), 250);
            });

            input.addEventListener('focus', () => {
                search(input.value, true);
            });

            input.addEventListener('blur', () => {
                setTimeout(() => { dd.style.display = 'none'; }, 200);
            });

            dd.addEventListener('mousedown', (e) => {
                e.preventDefault();
                const li = e.target.closest('li');
                if (!li) return;
                input.value = li.dataset.val;
                dd.style.display = 'none';
                this.saveFieldEdit(input);
            });
        });
    },

    // === Toast Notifications ===
    showToast(message, type = 'info') {
        const container = document.getElementById('toast-container');
        const toast = document.createElement('div');
        toast.className = `toast toast-${type}`;
        toast.textContent = message;
        container.appendChild(toast);
        setTimeout(() => toast.remove(), 4000);
    },
};

// הפעלה
document.addEventListener('DOMContentLoaded', () => app.init());
