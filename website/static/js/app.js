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
    currentJournalLines: [],      // שורות פקודת יומן הנוכחיות
    _journalInvId: null,          // id חשבונית שעליה מבוסס currentJournalLines

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
            // חשבוניות שתויקו לא מוצגות יותר במסך
            const all = (data.invoices || []).filter(inv => inv.status !== 'filed');

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
            filed: 'תויקה',
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
            const priorityId = inv.priority_invoice_id && !inv.priority_invoice_id.toUpperCase().startsWith('T')
                ? `<div style="font-size:0.75rem;color:var(--text-muted);margin-top:2px">${inv.priority_invoice_id}</div>`
                : '';

            const statusCell = inv.status === 'pending_filing'
                ? `<button class="btn btn-success btn-sm" style="font-size:0.8rem;padding:4px 10px"
                      onclick="event.stopPropagation(); app.fileById('${inv.id}')">תייק בספרי הנהלת חשבונות</button>`
                : `<span class="status-badge status-${inv.status}">${statusLabels[inv.status] || inv.status}</span>`;

            return `
                <tr onclick="app.openInvoice('${inv.id}')">
                    <td>${supplierName}</td>
                    <td>${invoiceNum}${priorityId}</td>
                    <td class="col-amount">${fmt(beforeVat)}</td>
                    <td class="col-amount">${fmt(afterVat)}</td>
                    <td>${date}</td>
                    <td style="text-align:center">${extractBox(inv.extraction_ok)}</td>
                    <td>${statusCell}</td>
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
            this.toggleFullscreen(false);

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
            // מסתירים את כל כפתורי הפעולה ומשאירים רק "מחק חשבונית" —
            // בלי לדרוס את ה-DOM, כדי שהכפתורים יחזרו אחרי פענוח חוזר
            const hideBtn = (id) => {
                const el = document.getElementById(id);
                if (el) el.style.display = 'none';
            };
            ['btn-approve-intake', 'btn-extract', 'btn-submit', 'btn-file',
             'btn-restore', 'btn-hold', 'btn-cancel', 'btn-clear-extraction'].forEach(hideBtn);
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

        const priorityIvnum = inv.priority_invoice_id && !inv.priority_invoice_id.toUpperCase().startsWith('T')
            ? `<div style="display:inline-flex;align-items:center;gap:6px;background:var(--bg-secondary);border:1px solid var(--border);border-radius:6px;padding:4px 10px;margin-bottom:8px;font-size:0.85rem">
                <span style="color:var(--text-muted)">מס׳ פריורטי:</span>
                <strong style="color:var(--accent)">${inv.priority_invoice_id}</strong>
               </div>`
            : '';

        let html = `
            ${priorityIvnum}

            <div class="data-section-card card-invoice">
                <h4 class="section-header" style="color:var(--text-secondary)">📄 פרטי חשבונית</h4>
                <div class="data-row">
                    <div class="data-field"><span class="label">חשבונית</span>${ef('invoice_number', d.invoice_number)}</div>
                    <div class="data-field"><span class="label">תאריך</span>${ef('invoice_date', d.invoice_date)}</div>
                    <div class="data-field"><span class="label">הקצאה</span>${ef('allocation_number', d.allocation_number)}</div>
                </div>
            </div>

            <div class="data-section-card card-supplier">
                <h4 class="section-header" style="color:var(--accent)">📦 ספק ${supMatch}</h4>
                <div class="data-row">
                    <div class="data-field"><span class="label">שם</span>${ef('supplier.name', d.supplier?.name)}</div>
                    <div class="data-field"><span class="label">${supTax}</span>${ef('supplier.tax_id', d.supplier?.tax_id)}</div>
                    <div class="data-field"><span class="label">פריורטי</span>${ef('supplier.priority_supplier_code', d.supplier?.priority_supplier_code)}</div>
                </div>
            </div>

            <div class="data-section-card card-customer">
                <h4 class="section-header" style="color:#1e40af">🏢 לקוח ${custMatch}</h4>
                <div class="data-row">
                    <div class="data-field"><span class="label">שם</span>${ef('customer.name', d.customer?.name)}</div>
                    <div class="data-field"><span class="label">${custTax}</span>${ef('customer.tax_id', d.customer?.tax_id)}</div>
                    <div class="data-field"><span class="label">סניף</span>${efAc('customer.branch', d.customer?.branch, '/api/db/branches/search')}</div>
                </div>
            </div>
        `;

        // שורות חשבונית
        if (d.lines && d.lines.length > 0) {
            html += `<div class="data-section-card card-lines">
                <h4 class="section-header" style="color:var(--success)">📋 שורות חשבונית</h4>
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
            html += `</tbody></table>
                <div class="totals-block">
                    <div class="totals-row"><span class="totals-label">סה"כ ${checkIcon(subtotalOk)}</span><span class="totals-value">₪${subtotal.toLocaleString()}</span></div>
                    <div class="totals-row"><span class="totals-label">מע"מ</span><span class="totals-value">₪${vatAmt.toLocaleString()}</span></div>
                    <div class="totals-row totals-grand"><span class="totals-label">סה"כ כולל מע"מ ${checkIcon(totalOk)}</span><span class="totals-value">₪${totalAmt.toLocaleString()}</span></div>
                </div>
            </div>`;
        } else {
            // סכומים בלי שורות
            html += `<div class="data-section-card card-lines">
                <h4 class="section-header" style="color:var(--success)">💰 סכומים</h4>
                <div class="totals-block">
                    <div class="totals-row"><span class="totals-label">סה"כ ${checkIcon(subtotalOk)}</span><span class="totals-value">₪${subtotal.toLocaleString()}</span></div>
                    <div class="totals-row"><span class="totals-label">מע"מ</span><span class="totals-value">₪${vatAmt.toLocaleString()}</span></div>
                    <div class="totals-row totals-grand"><span class="totals-label">סה"כ כולל מע"מ ${checkIcon(totalOk)}</span><span class="totals-value">₪${totalAmt.toLocaleString()}</span></div>
                </div>
            </div>`;
        }


        dataDiv.innerHTML = html;

        // שמירה אוטומטית כשיוצאים משדה
        dataDiv.querySelectorAll('.edit-field').forEach(input => {
            input.addEventListener('change', () => this.saveFieldEdit(input));
        });

        // אתחול שדות autocomplete
        this._setupAcFields(dataDiv);

        // הסתרת הזהרות
        document.getElementById('validation-warnings').style.display = 'none';

        const notesEl = document.getElementById('user-notes');
        if (notesEl) notesEl.value = inv.user_notes || '';

        // כפתורי פעולה לפי סטטוס
        const actionBtns = document.getElementById('action-buttons');
        const s = inv.status;
        const showBtn = (id, on) => {
            const el = document.getElementById(id);
            if (el) el.style.display = on ? '' : 'none';
        };

        actionBtns.style.display = 'flex';
        showBtn('btn-approve-intake', false);
        showBtn('btn-extract', false);
        showBtn('btn-submit', s === 'pending_submission' || s === 'pending_extraction');
        showBtn('btn-file', s === 'pending_filing');
        showBtn('btn-restore', s === 'on_hold' || s === 'cancelled');
        showBtn('btn-hold', s !== 'on_hold' && s !== 'cancelled');
        showBtn('btn-cancel', s !== 'cancelled');
        showBtn('btn-clear-extraction', true);   // יש נתוני פענוח — אפשר למחוק אותם
        // btn-delete-modal — תמיד גלוי

    },

    closeModal() {
        const modal = document.getElementById('modal-content');
        if (modal) modal.classList.remove('fullscreen');
        this.isFullscreen = false;
        this._cleanupHighlight();

        document.getElementById('invoice-modal').style.display = 'none';
        this.currentInvoice = null;
    },

    // תצוגה מקדימה של תנועת היומן — ניתנת לעריכה מלאה
    renderTransactionPreview(invoice) {
        const box = document.getElementById('transaction-preview');
        if (!box) return;
        const d = invoice && invoice.extracted_data;
        if (!d) { box.innerHTML = '<div style="color:var(--text-secondary);font-size:0.85rem">אין נתונים מחולצים</div>'; return; }
        const branch = ((d.customer && d.customer.branch) || '').trim();
        this._initJournalLines(d, branch);
        this._renderJournalTable(box, d, branch);
    },

    _initJournalLines(d, branch) {
        // שורות שמורות מהשרת — עדיפות ראשונה
        if (d.journal_lines && d.journal_lines.length > 0) {
            this._journalInvId = this.currentInvoice && this.currentInvoice.id;
            this.currentJournalLines = d.journal_lines.map(l => Object.assign({}, l));
            return;
        }
        // שורות מקומיות מאותה חשבונית — שמור עריכות בתהליך
        if (this._journalInvId === (this.currentInvoice && this.currentInvoice.id) &&
                this.currentJournalLines && this.currentJournalLines.length > 0) {
            return;
        }
        // בניה ראשונית מנתוני הפענוח
        this._journalInvId = this.currentInvoice && this.currentInvoice.id;
        const expAcc  = (d.expense_account || '').trim();
        const vatAcc  = branch ? `205-2-${branch}` : '205-2';
        const supCode = ((d.supplier && d.supplier.priority_supplier_code) || '').trim();
        const supAcc  = supCode && branch ? `${supCode}-${branch}` : supCode;
        const subtotal = parseFloat(d.subtotal) || 0;
        const vat      = parseFloat(d.vat_amount) || 0;
        const total    = parseFloat(d.total_amount) || 0;
        const invLines = Array.isArray(d.lines) ? d.lines.filter(l => l && (l.description || l.total_price)) : [];
        const lines = [];
        if (invLines.length > 1) {
            invLines.forEach((ln, i) => lines.push({
                id: `exp_${i}`, type: 'debit', account: expAcc,
                description: ln.description || `שורה ${i + 1}`,
                debit: parseFloat(ln.total_price || ln.unit_price || 0), credit: 0,
            }));
        } else {
            lines.push({ id: 'exp_0', type: 'debit', account: expAcc, description: 'הוצאות', debit: subtotal, credit: 0 });
        }
        if (vat > 0) {
            lines.push({ id: 'vat', type: 'vat', account: vatAcc, description: 'מע"מ תשומות', debit: vat, credit: 0 });
        }
        lines.push({ id: 'sup', type: 'credit', account: supAcc, description: (d.supplier && d.supplier.name) || 'ספק', debit: 0, credit: total });
        this.currentJournalLines = lines;
    },

    _renderJournalTable(box, d, branch) {
        const lines = this.currentJournalLines || [];
        const money  = n => parseFloat(n || 0).toLocaleString('he-IL', { minimumFractionDigits: 2, maximumFractionDigits: 2 });
        const totDr  = lines.reduce((s, l) => s + (parseFloat(l.debit)  || 0), 0);
        const totCr  = lines.reduce((s, l) => s + (parseFloat(l.credit) || 0), 0);
        const balanced   = Math.abs(totDr - totCr) < 0.01;
        const invTotal   = parseFloat(d.total_amount) || 0;
        const totalOk    = Math.abs(totCr - invTotal)  < 0.01;
        const drCount    = lines.filter(l => l.type === 'debit').length;
        const ep_exp = `/api/db/accounts/search?branch=${encodeURIComponent(branch)}`;
        const ep_sup = `/api/db/suppliers/journal-accounts?branch=${encodeURIComponent(branch)}`;

        const rowHtml = (l, i) => {
            const isDr = l.type === 'debit', isVat = l.type === 'vat', isCr = l.type === 'credit';
            const safeAcc  = (l.account  || '').replace(/"/g, '&quot;');
            const safeDesc = (l.description || '').replace(/"/g, '&quot;');

            const accCell = isDr
                ? `<span class="ac-field" style="position:relative;display:inline-flex;width:100%">
                     <input class="edit-field ac-input jl-acc" data-jli="${i}" data-ep="${ep_exp}"
                       value="${safeAcc}" placeholder="— חסר —" autocomplete="off" spellcheck="false"
                       style="width:100%;font-size:0.88rem;padding:3px 6px">
                     <ul class="ac-dd"></ul></span>`
                : isCr
                ? `<span class="ac-field" style="position:relative;display:inline-flex;width:100%">
                     <input class="edit-field ac-input jl-acc" data-jli="${i}" data-ep="${ep_sup}"
                       value="${safeAcc}" placeholder="קוד ספק-סניף" autocomplete="off" spellcheck="false"
                       style="width:100%;font-size:0.88rem;padding:3px 6px">
                     <ul class="ac-dd"></ul></span>`
                : `<span style="font-size:0.87rem;color:var(--text-secondary);padding:3px 0">${l.account || ''}</span>`;

            const descCell = isDr
                ? `<input class="edit-field jl-fld" data-jli="${i}" data-jlf="description"
                     value="${safeDesc}" style="width:100%;font-size:0.88rem;padding:3px 6px">`
                : `<span style="font-size:0.87rem;padding:3px 0">${l.description || ''}</span>`;

            const drCell = (isDr || isVat)
                ? `<input class="edit-field jl-fld" data-jli="${i}" data-jlf="debit" type="number" step="0.01" min="0"
                     value="${parseFloat(l.debit || 0).toFixed(2)}"
                     style="width:88px;font-size:0.88rem;padding:3px 6px;direction:ltr;text-align:left">`
                : '';

            const crCell = isCr
                ? `<input class="edit-field jl-fld" data-jli="${i}" data-jlf="credit" type="number" step="0.01" min="0"
                     value="${parseFloat(l.credit || 0).toFixed(2)}"
                     style="width:88px;font-size:0.88rem;padding:3px 6px;direction:ltr;text-align:left">`
                : '';

            const delBtn = (isDr && drCount > 1)
                ? `<button class="jl-del" data-jldel="${i}"
                     style="background:none;border:none;cursor:pointer;color:var(--danger);padding:2px 5px;font-size:0.85rem" title="מחק שורה">✕</button>`
                : '';

            return `<tr>
                <td style="padding:4px 6px">${accCell}</td>
                <td style="padding:4px 6px">${descCell}</td>
                <td style="padding:4px 6px;direction:ltr;text-align:left">${drCell}</td>
                <td style="padding:4px 6px;direction:ltr;text-align:left">${crCell}</td>
                <td style="padding:4px 2px;text-align:center;width:24px">${delBtn}</td>
            </tr>`;
        };

        let warnHtml = '';
        if (!branch) warnHtml = `<span class="jl-warn" style="color:var(--danger);font-size:0.82rem">⚠ לא זוהה סניף</span>`;
        else if (!balanced) warnHtml = `<span class="jl-warn" style="color:var(--danger);font-size:0.82rem">⚠ לא מאוזן — חובה ₪${money(totDr)} זכות ₪${money(totCr)}</span>`;
        else if (!totalOk) warnHtml = `<span class="jl-warn" style="color:var(--warning,#b45309);font-size:0.82rem">⚠ סה"כ שונה מחשבונית ₪${money(invTotal)}</span>`;
        else warnHtml = `<span class="jl-warn" style="color:var(--success);font-size:0.82rem">✓ מאוזן</span>`;

        box.innerHTML = `
        <div class="tx-card">
            <div class="tx-card-header" style="gap:8px;flex-wrap:wrap">
                <span>📒 פקודת יומן</span>
                <span class="tx-meta">סניף: ${branch || '—'}</span>
                <span style="flex:1"></span>
                ${warnHtml}
                <button id="btn-add-jl" class="btn btn-secondary btn-sm"
                  style="font-size:0.8rem;padding:3px 10px">＋ הוסף שורה</button>
            </div>
            <table class="tx-table" style="table-layout:fixed">
                <colgroup>
                    <col style="width:27%"><col style="width:31%"><col style="width:17%"><col style="width:17%"><col style="width:8%">
                </colgroup>
                <thead><tr>
                    <th>חשבון</th><th>תיאור</th>
                    <th style="direction:ltr;text-align:left">חובה</th>
                    <th style="direction:ltr;text-align:left">זכות</th>
                    <th></th>
                </tr></thead>
                <tbody>
                    ${lines.map(rowHtml).join('')}
                    <tr class="tx-total-row">
                        <td colspan="2" style="padding:5px 6px">סה"כ</td>
                        <td class="jl-tot-dr" style="padding:5px 6px;direction:ltr;text-align:left">₪${money(totDr)}</td>
                        <td class="jl-tot-cr" style="padding:5px 6px;direction:ltr;text-align:left">₪${money(totCr)}</td>
                        <td></td>
                    </tr>
                </tbody>
            </table>
            <div class="tx-card-footer" style="color:${balanced && totalOk ? 'var(--success)' : 'var(--danger)'}">
                ${balanced && totalOk ? '✓ מאוזן' : (balanced ? '⚠ שונה מסה"כ חשבונית' : '⚠ לא מאוזן')}
            </div>
        </div>`;

        // הוסף שורה
        box.querySelector('#btn-add-jl').addEventListener('click', () => {
            const insertAt = this.currentJournalLines.findIndex(l => l.type !== 'debit');
            const newLine  = { id: `exp_${Date.now()}`, type: 'debit', account: '', description: 'הוצאות', debit: 0, credit: 0 };
            if (insertAt === -1) this.currentJournalLines.push(newLine);
            else this.currentJournalLines.splice(insertAt, 0, newLine);
            this._renderJournalTable(box, d, branch);
            this.saveJournalLines();
        });

        // מחק שורה
        box.querySelectorAll('.jl-del').forEach(btn => {
            btn.addEventListener('click', () => {
                this.currentJournalLines.splice(parseInt(btn.dataset.jldel), 1);
                this._renderJournalTable(box, d, branch);
                this.saveJournalLines();
            });
        });

        // עריכת שדות (תיאור, סכום) — עדכון ללא re-render
        box.querySelectorAll('.jl-fld').forEach(input => {
            input.addEventListener('change', () => {
                const idx = parseInt(input.dataset.jli);
                const fld = input.dataset.jlf;
                const isNum = fld === 'debit' || fld === 'credit';
                this.currentJournalLines[idx][fld] = isNum ? (parseFloat(input.value) || 0) : input.value;
                this._updateJournalTotals(box, d);
                this.saveJournalLines();
            });
        });

        // autocomplete לשדות חשבון
        box.querySelectorAll('.jl-acc').forEach(input => {
            const dd = input.nextElementSibling;
            let timer = null;
            const pos = () => {
                const r = input.getBoundingClientRect();
                Object.assign(dd.style, { position: 'fixed', top: (r.bottom + 2) + 'px', left: r.left + 'px',
                    width: Math.max(240, r.width) + 'px', right: 'auto', zIndex: '99999' });
            };
            const search = async (q, all = false) => {
                if (!all && !q.trim()) { dd.style.display = 'none'; return; }
                try {
                    const sep = input.dataset.ep.includes('?') ? '&' : '?';
                    const res  = await fetch(`${input.dataset.ep}${sep}q=${encodeURIComponent(q)}`);
                    const data = await res.json();
                    const items = data.results || [];
                    if (!items.length) { dd.style.display = 'none'; return; }
                    dd.innerHTML = items.map(it => {
                        const code = it.account_code || '', name = it.account_name || '';
                        return `<li data-val="${code.replace(/"/g,'&quot;')}">${name ? `${code} — ${name}` : code}</li>`;
                    }).join('');
                    pos(); dd.style.display = 'block';
                } catch { dd.style.display = 'none'; }
            };
            input.addEventListener('input',  () => { clearTimeout(timer); timer = setTimeout(() => search(input.value), 250); });
            input.addEventListener('focus',  () => search(input.value, true));
            input.addEventListener('blur',   () => setTimeout(() => { dd.style.display = 'none'; }, 200));
            dd.addEventListener('mousedown', e => {
                e.preventDefault();
                const li = e.target.closest('li');
                if (!li) return;
                const val = li.dataset.val;
                input.value = val;
                dd.style.display = 'none';
                const idx = parseInt(input.dataset.jli);
                this.currentJournalLines[idx].account = val;
                // שמור גם ב-expense_account של הספק לזיכרון לטווח ארוך
                if (this.currentJournalLines[idx].type === 'debit' && val) {
                    fetch(`/api/invoices/${this.currentInvoice.id}/update-field`, {
                        method: 'POST', headers: { 'Content-Type': 'application/json' },
                        body: JSON.stringify({ path: 'expense_account', value: val }),
                    });
                    if (this.currentInvoice.extracted_data) this.currentInvoice.extracted_data.expense_account = val;
                }
                this.saveJournalLines();
            });
        });
    },

    _updateJournalTotals(box, d) {
        const lines  = this.currentJournalLines || [];
        const money  = n => parseFloat(n || 0).toLocaleString('he-IL', { minimumFractionDigits: 2, maximumFractionDigits: 2 });
        const totDr  = lines.reduce((s, l) => s + (parseFloat(l.debit)  || 0), 0);
        const totCr  = lines.reduce((s, l) => s + (parseFloat(l.credit) || 0), 0);
        const bal    = Math.abs(totDr - totCr) < 0.01;
        const invTot = parseFloat(d && d.total_amount) || 0;
        const totOk  = Math.abs(totCr - invTot) < 0.01;
        const drEl   = box.querySelector('.jl-tot-dr');
        const crEl   = box.querySelector('.jl-tot-cr');
        const warnEl = box.querySelector('.jl-warn');
        const footEl = box.querySelector('.tx-card-footer');
        if (drEl) drEl.textContent = `₪${money(totDr)}`;
        if (crEl) crEl.textContent = `₪${money(totCr)}`;
        if (warnEl) {
            if (!bal) { warnEl.style.color = 'var(--danger)'; warnEl.textContent = `⚠ לא מאוזן — חובה ₪${money(totDr)} זכות ₪${money(totCr)}`; }
            else if (!totOk) { warnEl.style.color = 'var(--warning,#b45309)'; warnEl.textContent = `⚠ סה"כ שונה מחשבונית ₪${money(invTot)}`; }
            else { warnEl.style.color = 'var(--success)'; warnEl.textContent = '✓ מאוזן'; }
        }
        if (footEl) {
            footEl.textContent = bal && totOk ? '✓ מאוזן' : (bal ? '⚠ שונה מסה"כ חשבונית' : '⚠ לא מאוזן');
            footEl.style.color = bal && totOk ? 'var(--success)' : 'var(--danger)';
        }
    },

    async saveJournalLines() {
        if (!this.currentInvoice) return;
        if (this.currentInvoice.extracted_data)
            this.currentInvoice.extracted_data.journal_lines = this.currentJournalLines.slice();
        try {
            await fetch(`/api/invoices/${this.currentInvoice.id}/journal-lines`, {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ lines: this.currentJournalLines }),
            });
        } catch {}
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
        // איפוס שורות יומן — פענוח חוזר יבנה אותן מחדש
        this._journalInvId = null;
        this.currentJournalLines = [];

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

    // === תיוק ישיר מהרשימה ===
    async fileById(invoiceId) {
        try {
            const res = await fetch(`/api/invoices/${invoiceId}/file-to-ledger`, { method: 'POST' });
            const data = await res.json();
            if (!res.ok) {
                this.showToast(data.detail || 'שגיאה בתיוק', 'error');
                return;
            }
            const companyLabel = data.company_name || data.branch;
            const dividerLabel = data.divider_name ? ` / ${data.divider_name}` : '';
            this.showToast(`תויק — ${companyLabel} / ${data.year}${dividerLabel}`, 'success');
            this.loadInvoices();
        } catch (err) {
            this.showToast('שגיאת תקשורת: ' + (err.message || err), 'error');
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
            const companyLabel = data.company_name || data.branch;
            const dividerLabel = data.divider_name ? ` / ${data.divider_name}` : '';
            this.showToast(`תויק בספרי הנהלת חשבונות — ${companyLabel} / ${data.year}${dividerLabel}`, 'success');
            this.closeModal();
            this.loadInvoices();
        } catch (err) {
            this.showToast('שגיאת תקשורת: ' + (err.message || err), 'error');
            console.error('fileToLedger error:', err);
        }
    },

    async approveInvoice() {
        if (!this.currentInvoice) return;

        // ולידציה: פקודת יומן חייבת להיות מאוזנת לפני קליטה בפריורטי
        const jLines = this.currentJournalLines;
        if (jLines && jLines.length > 0) {
            const money = n => parseFloat(n || 0).toLocaleString('he-IL', { minimumFractionDigits: 2, maximumFractionDigits: 2 });
            const totDr = jLines.reduce((s, l) => s + (parseFloat(l.debit)  || 0), 0);
            const totCr = jLines.reduce((s, l) => s + (parseFloat(l.credit) || 0), 0);
            if (Math.abs(totDr - totCr) > 0.01) {
                this.showToast(`פקודת יומן לא מאוזנת — חובה ₪${money(totDr)} זכות ₪${money(totCr)}`, 'error');
                return;
            }
            const invTot = parseFloat(this.currentInvoice.extracted_data && this.currentInvoice.extracted_data.total_amount) || 0;
            if (invTot > 0 && Math.abs(totCr - invTot) > 0.51) {
                const ok = confirm(`סה"כ פקודת יומן ₪${money(totCr)} שונה מסה"כ חשבונית ₪${money(invTot)}.\nלהמשיך בכל זאת?`);
                if (!ok) return;
            }
        }

        const notes = document.getElementById('user-notes')?.value || '';
        const btn = document.getElementById('btn-submit');

        if (btn) {
            btn.disabled = true;
            btn.textContent = '⏳ שולח לפריורטי...';
        }

        try {
            const controller = new AbortController();
            const timeoutId = setTimeout(() => controller.abort(), 300000); // 5 min
            const res = await fetch(`/api/invoices/${this.currentInvoice.id}/approve`, {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ notes }),
                signal: controller.signal,
            });
            clearTimeout(timeoutId);
            let data;
            try { data = await res.json(); } catch { data = {}; }
            if (!res.ok) {
                this.showToast(data.detail || data.message || `שגיאת שרת ${res.status}`, 'error');
                if (btn) { btn.disabled = false; btn.textContent = 'אשר וקלוט בפריורטי'; }
                return;
            }
            if (data.status === 'pending_filing') {
                this.showToast('החשבונית נקלטה בפריורטי בהצלחה!', 'success');
            } else {
                this.showToast(data.message || 'שגיאה בקליטה בפריורטי', 'error');
                if (btn) { btn.disabled = false; btn.textContent = 'אשר וקלוט בפריורטי'; }
            }
            this.closeModal();
            this.loadInvoices();
        } catch (err) {
            this.showToast(`שגיאת תקשורת: ${err.message || err}`, 'error');
            if (btn) { btn.disabled = false; btn.textContent = 'אשר וקלוט בפריורטי'; }
        }
    },

    async rejectInvoice() {
        if (!this.currentInvoice) return;
        const reason = document.getElementById('user-notes')?.value || 'נדחה על ידי המשתמש';

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
        const ok = await this.showConfirm(
            'החשבונית והקובץ יימחקו לצמיתות. פעולה זו בלתי הפיכה.',
            { title: 'מחיקת חשבונית', confirmText: 'מחק חשבונית', icon: '🗑', type: 'danger' }
        );
        if (!ok) return;

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

    async clearExtraction() {
        if (!this.currentInvoice) return;
        const ok = await this.showConfirm(
            'נתוני הפענוח יימחקו והחשבונית תחזור לסטטוס "ממתין לפענוח". הקובץ עצמו יישמר.',
            { title: 'מחיקת נתוני פענוח', confirmText: 'מחק פענוח', icon: '🧹', type: 'danger' }
        );
        if (!ok) return;

        try {
            const res = await fetch(`/api/invoices/${this.currentInvoice.id}/clear-extraction`, { method: 'POST' });
            if (!res.ok) {
                this.showToast('שגיאה במחיקת הפענוח', 'error');
                return;
            }
            this.showToast('נתוני הפענוח נמחקו', 'info');
            // איפוס פקודת היומן שנשמרה בזיכרון
            this.currentJournalLines = [];
            this._journalInvId = null;
            // רענון המודל מהשרת
            const invRes = await fetch(`/api/invoices/${this.currentInvoice.id}`);
            if (invRes.ok) {
                this.currentInvoice = await invRes.json();
                this.renderModal(this.currentInvoice);
                this.renderTransactionPreview(this.currentInvoice);
            }
            this.loadInvoices();
        } catch (err) {
            this.showToast('שגיאה במחיקת הפענוח', 'error');
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
                    const sep = input.dataset.ep.includes('?') ? '&' : '?';
                    const res = await fetch(`${input.dataset.ep}${sep}q=${encodeURIComponent(q)}`);
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
    // דיאלוג אישור מעוצב — מחזיר Promise<boolean>
    showConfirm(message, opts = {}) {
        const {
            title = 'אישור פעולה',
            confirmText = 'אישור',
            cancelText = 'ביטול',
            type = 'danger',     // danger | info
            icon = type === 'danger' ? '🗑' : 'ℹ️',
        } = opts;

        return new Promise(resolve => {
            const overlay = document.createElement('div');
            overlay.className = 'confirm-overlay';
            overlay.innerHTML = `
                <div class="confirm-box" role="dialog" aria-modal="true">
                    <div class="confirm-icon ${type}">${icon}</div>
                    <h3 class="confirm-title">${title}</h3>
                    <p class="confirm-message">${message}</p>
                    <div class="confirm-actions">
                        <button class="btn confirm-btn-cancel" data-act="cancel">${cancelText}</button>
                        <button class="btn ${type === 'danger' ? 'btn-danger' : 'btn-primary'}" data-act="ok">${confirmText}</button>
                    </div>
                </div>
            `;

            const close = (val) => {
                document.removeEventListener('keydown', onKey);
                overlay.remove();
                resolve(val);
            };
            const onKey = (e) => {
                if (e.key === 'Escape') close(false);
                if (e.key === 'Enter') close(true);
            };

            overlay.addEventListener('click', (e) => {
                if (e.target === overlay) close(false);
                const act = e.target.dataset.act;
                if (act === 'ok') close(true);
                if (act === 'cancel') close(false);
            });
            document.addEventListener('keydown', onKey);

            document.body.appendChild(overlay);
            overlay.querySelector('[data-act="ok"]').focus();
        });
    },

    showToast(message, type = 'info') {
        const container = document.getElementById('toast-container');
        const toast = document.createElement('div');
        toast.className = `toast toast-${type}`;
        toast.textContent = message;
        container.appendChild(toast);
        const duration = type === 'success' ? 7000 : 4000;
        setTimeout(() => toast.remove(), duration);
    },
};

// הפעלה
document.addEventListener('DOMContentLoaded', () => app.init());
