/**
 * SupplierInvoice — לוגיקת ממשק: upload, polling, approve
 */
const app = {
    currentFilter: '',
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
            const url = this.currentFilter
                ? `/api/invoices?status=${this.currentFilter}`
                : '/api/invoices';
            const res = await fetch(url);
            const data = await res.json();
            this.renderInvoiceList(data.invoices || []);
        } catch (err) {
            // שגיאה שקטה — ננסה שוב ב-polling הבא
        }
    },

    renderInvoiceList(invoices) {
        const container = document.getElementById('invoice-list');

        if (invoices.length === 0) {
            container.innerHTML = '<tr><td colspan="7" class="empty-state">אין חשבוניות להצגה</td></tr>';
            return;
        }

        const statusLabels = {
            pending: 'ממתין',
            processing: 'בעיבוד',
            review: 'לבקרה',
            submitted: 'נקלט',
            rejected: 'נדחה',
            error: 'שגיאה',
        };

        const fmt = (n) => n != null && n !== 0 ? `₪${Number(n).toLocaleString('he-IL', {minimumFractionDigits:2, maximumFractionDigits:2})}` : '—';

        container.innerHTML = invoices.map(inv => {
            const d = inv.extracted_data;
            const supplierName = d?.supplier?.name || 'טרם נותח';
            const invoiceNum = d?.invoice_number || '—';
            const beforeVat = d?.total_before_vat ?? d?.total_amount ?? null;
            const afterVat = d?.total_with_vat ?? d?.total_amount ?? null;
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
                    <td><span class="status-badge status-${inv.status}">${statusLabels[inv.status] || inv.status}</span></td>
                    <td><button class="btn-delete-row" title="מחק חשבונית" onclick="event.stopPropagation(); app.deleteInvoiceById('${inv.id}')">🗑</button></td>
                </tr>
            `;
        }).join('');
    },

    // === סינון ===
    filterByStatus(btn, status) {
        document.querySelectorAll('.tab').forEach(t => t.classList.remove('active'));
        btn.classList.add('active');
        this.currentFilter = status;
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

            <h4 class="section-header" style="color:#1e40af">🏢 לקוח ${custMatch}</h4>
            <div class="data-row">
                <div class="data-field" style="flex:2"><span class="label">שם</span>${ef('customer.name', d.customer?.name)}</div>
                <div class="data-field"><span class="label">${custTax}</span>${ef('customer.tax_id', d.customer?.tax_id)}</div>
            </div>
            <div class="data-row">
                <div class="data-field"><span class="label">פריורטי</span>${ef('customer.priority_customer_code', d.customer?.priority_customer_code)}</div>
                <div class="data-field"><span class="label">סניף</span>${ef('customer.branch', d.customer?.branch)}</div>
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

        // הסתרת הזהרות
        document.getElementById('validation-warnings').style.display = 'none';

        // Notes
        document.getElementById('user-notes').value = inv.user_notes || '';

        // Action buttons vs status display
        const actionBtns = document.getElementById('action-buttons');
        const statusDisplay = document.getElementById('status-display');

        if (inv.status === 'review') {
            actionBtns.style.display = 'flex';
            statusDisplay.style.display = 'none';
        } else {
            actionBtns.style.display = 'none';
            const statusMessages = {
                pending: 'ממתין לעיבוד',
                processing: 'בעיבוד...',
                submitted: `נקלט בפריורטי — ${inv.priority_invoice_id || ''}`,
                rejected: `נדחה — ${inv.user_notes || ''}`,
                error: `שגיאה: ${inv.error_message || ''}`,
            };
            statusDisplay.innerHTML = statusMessages[inv.status] || inv.status;
            statusDisplay.className = `status-display status-badge status-${inv.status}`;
            statusDisplay.style.display = 'block';
        }
    },

    closeModal() {
        const modal = document.getElementById('modal-content');
        if (modal) modal.classList.remove('fullscreen');
        this.isFullscreen = false;
        this._cleanupHighlight();

        document.getElementById('invoice-modal').style.display = 'none';
        this.currentInvoice = null;
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
            if (res.ok && data.status === 'submitted') {
                this.showToast('החשבונית נקלטה בפריורטי בהצלחה!', 'success');
            } else {
                this.showToast(data.message || 'שגיאה באישור', 'error');
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
            const res = await fetch('/api/sync/priority', { method: 'POST' });
            const data = await res.json();
            this.showToast(
                `סנכרון הושלם — ${data.suppliers_count} ספקים, ${data.parts_count} פריטים`,
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
