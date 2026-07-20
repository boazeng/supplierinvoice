'use strict';
/**
 * מצרף קובץ ומבצע CLOSEPRINTPIV על חשבונית בפריורטי דרך WCF Web SDK.
 *
 * Usage:
 *   node finalize_invoice.js <IVNUM> <filePath>
 *
 * Returns JSON to stdout:
 *   { ok: true, fncnum: <n>, ivnum: <final> }
 *   { ok: false, error: "<message>" }
 */

const path = require('path');
const fs   = require('fs');

// קריאת .env ידנית — dotenv v16+ מדפיס לוג ל-stdout ושובר את הפארסינג ב-Python
(function loadEnv() {
  const envFile = path.join(__dirname, '..', '.env');
  if (!fs.existsSync(envFile)) return;
  fs.readFileSync(envFile, 'utf8').split('\n').forEach(line => {
    const m = line.match(/^\s*([^#=\s][^=]*?)\s*=\s*(.*?)\s*$/);
    if (m && !process.env[m[1]]) process.env[m[1]] = m[2].replace(/^['"]|['"]$/g, '');
  });
})();

const priority = require('priority-web-sdk');

function parseOdataUrl(odataUrl) {
  const url = (odataUrl || '').replace(/\/$/, '');
  const match = url.match(/^(https?:\/\/[^\/]+)\/odata\/Priority\/([^\/]+)\/(.+)$/);
  if (!match) throw new Error('Cannot parse Priority URL: ' + url);
  const [, base, tabulaini, company] = match;
  return { serviceUrl: base + '/wcf/service.svc', tabulaini, company };
}

function withTimeout(promise, ms, label) {
  const t = new Promise((_, reject) =>
    setTimeout(() => reject(new Error(`${label} timed out after ${ms}ms`)), ms)
  );
  return Promise.race([promise, t]);
}

function makeMessageHandler(label) {
  return function onShowMessage(msg) {
    process.stderr.write(`[${label}] Priority message: code=${msg.code} type=${msg.type} msg=${msg.message}\n`);
    if (msg.form) {
      try { msg.form.warningConfirm(1); } catch (e) {
        try { msg.form.infoMsgConfirm(); } catch (_) {}
      }
    }
  };
}

function isTempIvnum(ivnum) {
  return ivnum && ivnum.toString().toUpperCase().startsWith('T');
}

/**
 * מריץ פרוצדורה צעד אחרי צעד.
 * כאשר מגיעים לשלב "client" (הדפסה בדפדפן) — מחשיבים כהצלחה (החשבונית נסגרה בשרת).
 */
async function runProcedure(firstStep, label, ivnumVal) {
  let step = firstStep;
  let iter = 0;
  while (step && iter++ < 20) {
    const t = step.type || 'unknown';
    const msg = step.message || '';
    const mtype = step.messagetype || '';
    process.stderr.write(`[${label}] step=${t} messagetype=${mtype} message=${msg.substring(0,80)}\n`);

    if (t === 'end') {
      return { ok: true };
    }

    if (t === 'error_caught') return { ok: false, error: step.error || msg };

    const proc = step.proc;

    if (t === 'client' || t === 'displayUrl') {
      // קריאה ל-clientContinue מאשרת לשרת שהלקוח טיפל בשלב ההדפסה — Priority ממשיך לסגור
      if (!proc || !proc.clientContinue) {
        process.stderr.write(`[${label}] client step but no clientContinue — treating as done\n`);
        return { ok: true };
      }
      process.stderr.write(`[${label}] calling clientContinue with data=${JSON.stringify(step.data)}\n`);
      step = await withTimeout(
        new Promise((res, rej) => proc.clientContinue(step.data || {}, res, rej)),
        150000, `${label}.clientContinue`
      ).catch(e => {
        process.stderr.write(`[${label}] clientContinue timed out or failed: ${e.message}\n`);
        return { type: 'end' }; // Python יבדוק OData לפי BOOKNUM בכל מקרה
      });
      continue;
    }

    if (t === 'message') {
      if (mtype === 'error') return { ok: false, error: msg || 'Procedure error' };
      if (!proc) return { ok: false, error: 'No proc on message' };
      step = await withTimeout(
        new Promise((res, rej) => proc.message(1, res, rej)),
        30000, `${label}.message`
      ).catch(e => ({ type: 'error_caught', error: e.message }));
      continue;
    }

    if (t === 'inputHelp') {
      if (!proc) return { ok: false, error: 'No proc on inputHelp' };
      step = await withTimeout(
        new Promise((res, rej) => proc.inputHelp(1, res, rej)),
        30000, `${label}.inputHelp`
      ).catch(e => ({ type: 'error_caught', error: e.message }));
      continue;
    }

    if (t === 'inputFields') {
      if (!proc) return { ok: false, error: 'No proc on inputFields' };
      const editFields = (step.input && step.input.EditFields) || [];
      process.stderr.write(`[${label}] inputFields titles: ${editFields.map(f=>f.title).join(', ')}\n`);
      // מלא שדות IVNUM/ID עם הערך שלנו
      const ivnumNum = ivnumVal ? ivnumVal.replace(/^T/i, '') : '';
      const filled = editFields.map(f => {
        const titleLower = (f.title || '').toLowerCase();
        const isIdField = titleLower.includes('id') || titleLower.includes('ivnum') || titleLower.includes('חשבונית') || titleLower.includes('invoice');
        return {
          field: f.field,
          op: f.operator || 0,
          value: isIdField ? ivnumNum : (f.value || ''),
          op2: 0,
          value2: '',
        };
      });
      step = await withTimeout(
        new Promise((res, rej) => proc.inputFields(1, { EditFields: filled }, res, rej)),
        30000, `${label}.inputFields`
      ).catch(e => ({ type: 'error_caught', error: e.message }));
      continue;
    }

    if (t === 'inputOptions') {
      if (!proc) return { ok: false, error: 'No proc on inputOptions' };
      const sel = (step.input && step.input.Options && step.input.Options[0]) ? step.input.Options[0].field : 1;
      step = await withTimeout(
        new Promise((res, rej) => proc.inputOptions(1, sel, res, rej)),
        30000, `${label}.inputOptions`
      ).catch(e => ({ type: 'error_caught', error: e.message }));
      continue;
    }

    if (t === 'reportOptions' || t === 'documentOptions') {
      if (!proc) return { ok: false, error: 'No proc on options' };
      const fmt = (step.formats && step.formats[0]) ? step.formats[0].format : 1;
      const fn = t === 'reportOptions' ? 'reportOptions' : 'documentOptions';
      step = await withTimeout(
        new Promise((res, rej) => proc[fn](1, fmt, 1, res, rej)),
        30000, `${label}.${fn}`
      ).catch(e => ({ type: 'error_caught', error: e.message }));
      continue;
    }

    if (proc && proc.continueProc) {
      step = await withTimeout(
        new Promise((res, rej) => proc.continueProc(res, rej)),
        30000, `${label}.continueProc`
      ).catch(e => ({ type: 'error_caught', error: e.message }));
    } else {
      return { ok: false, error: `Unknown step: ${t}` };
    }
  }
  return { ok: false, error: 'Loop limit exceeded' };
}

function isNotFoundError(step) {
  const err = (step.type === 'error_caught' ? step.error : step.message) || '';
  return step.messagetype === 'error' && err.toLowerCase().includes('no such');
}

// 'client' as first step = procedure delegates to browser without server closure
function isClientOnly(step) {
  return step.type === 'client';
}

/**
 * פותח את מסך FNCSUP של הספק דרך WCF, קורא את FNCPATNAME הנוכחי,
 * מעדכן ל-newPatName אם שונה, וסוגר. מחזיר את הערך המקורי.
 * On error: mutes the error and returns ''.
 */
async function getAndSetSupplierFncpatname(company, supplierCode, newPatName) {
  process.stderr.write(`[FNCSUP] START: supplier=${supplierCode} target="${newPatName}"\n`);

  let fncsupForm;
  try {
    fncsupForm = await withTimeout(
      new Promise((res, rej) => priority.formStartEx(
        'FNCSUP', makeMessageHandler('FNCSUP'), null,
        company, 1, { zoomValue: supplierCode }, res, rej
      )),
      30000, 'formStartEx FNCSUP'
    );
    process.stderr.write(`[FNCSUP] form opened OK\n`);
  } catch (e) {
    process.stderr.write(`[FNCSUP] FAILED to open form: ${e.message}\n`);
    return { original: '', saved: false };
  }

  let original = '';
  try {
    const rows = await withTimeout(
      new Promise((res, rej) => fncsupForm.getRows(1, res, rej)),
      15000, 'getRows FNCSUP'
    );
    const rowData = (rows && rows.FNCSUP)
      ? (rows.FNCSUP['1'] || Object.values(rows.FNCSUP)[0] || {})
      : {};
    original = (rowData.FNCPATNAME || '').trim();
    process.stderr.write(`[FNCSUP] read FNCPATNAME="${original}" rowKeys=${Object.keys(rowData).join(',')}\n`);
  } catch (e) {
    process.stderr.write(`[FNCSUP] FAILED getRows: ${e.message}\n`);
    await fncsupForm.endCurrentForm(false).catch(() => {});
    return { original: '', saved: false };
  }

  if (original === newPatName) {
    process.stderr.write(`[FNCSUP] already "${newPatName}", no change needed\n`);
    await fncsupForm.endCurrentForm(false).catch(() => {});
    return { original, saved: true };
  }

  try {
    await withTimeout(new Promise((res, rej) => fncsupForm.setActiveRow(1, res, rej)), 10000, 'setActiveRow');
    process.stderr.write(`[FNCSUP] setActiveRow OK\n`);
    await withTimeout(new Promise((res, rej) => fncsupForm.fieldUpdate('FNCPATNAME', newPatName, res, rej)), 10000, 'fieldUpdate');
    process.stderr.write(`[FNCSUP] fieldUpdate OK\n`);
    await withTimeout(new Promise((res, rej) => fncsupForm.saveRow(false, res, rej)), 15000, 'saveRow');
    process.stderr.write(`[FNCSUP] saveRow OK\n`);
  } catch (e) {
    process.stderr.write(`[FNCSUP] FAILED during edit/save: ${e.message}\n`);
    await fncsupForm.endCurrentForm(false).catch(() => {});
    return { original, saved: false };
  }

  // אימות: קרא שוב כדי לוודא שהשינוי נשמר
  try {
    const rowsAfter = await withTimeout(
      new Promise((res, rej) => fncsupForm.getRows(1, res, rej)),
      10000, 'getRows FNCSUP verify'
    );
    const rowAfter = (rowsAfter && rowsAfter.FNCSUP)
      ? (rowsAfter.FNCSUP['1'] || Object.values(rowsAfter.FNCSUP)[0] || {})
      : {};
    const actualVal = (rowAfter.FNCPATNAME || '').trim();
    process.stderr.write(`[FNCSUP] verify after save: FNCPATNAME="${actualVal}"\n`);
    const saved = (actualVal === newPatName);
    if (!saved) process.stderr.write(`[FNCSUP] WARNING: save did not persist! still "${actualVal}"\n`);
    await fncsupForm.endCurrentForm(false).catch(() => {});
    return { original, saved };
  } catch (e) {
    process.stderr.write(`[FNCSUP] verify read failed: ${e.message}\n`);
    await fncsupForm.endCurrentForm(false).catch(() => {});
    return { original, saved: false };
  }
}

async function main() {
  const [ivnum, filePath, supplierCode, vatType] = process.argv.slice(2);
  if (!ivnum) throw new Error('Usage: node finalize_invoice.js <IVNUM> [filePath] [supplierCode] [vatType]');

  const odataUrl = process.env.PRIORITY_URL_REAL || '';
  const { serviceUrl, tabulaini, company } = parseOdataUrl(odataUrl);

  process.stderr.write(`ARGS: ivnum="${ivnum}" supplierCode="${supplierCode}" vatType="${vatType}"\n`);
  process.stderr.write(`Login → ${serviceUrl} (${company})\n`);
  await withTimeout(
    new Promise((res, rej) => priority.login(
      { username: process.env.PRIORITY_USERNAME, password: process.env.PRIORITY_PASSWORD,
        url: serviceUrl, tabulaini, language: 1, appname: 'supplierinvoice' },
      res, rej
    )),
    20000, 'login'
  );
  process.stderr.write('Login OK\n');

  // ===== עדכון FNCSUP לפני פתיחת PINVOICES (רק ב-2/3) =====
  // חשוב: פעולה זו חייבת לקרות לפני פתיחת PINVOICES כדי למנוע קונפליקט ב-WCF
  let _origFncpatname = '';
  let _fncsupSaved = false;
  if (vatType === 'two_thirds' && supplierCode) {
    const fncsupResult = await getAndSetSupplierFncpatname(company, supplierCode, '2/3');
    _origFncpatname = fncsupResult.original;
    _fncsupSaved = fncsupResult.saved;
    process.stderr.write(`[FNCSUP] result: original="${_origFncpatname}" saved=${_fncsupSaved}\n`);
  }

  process.stderr.write(`Opening PINVOICES for ${ivnum}...\n`);
  const form = await withTimeout(
    new Promise((res, rej) => priority.formStartEx(
      'PINVOICES',
      makeMessageHandler('PINVOICES'),
      null,
      company, 1, { zoomValue: ivnum }, res, rej
    )),
    30000, 'formStartEx'
  );
  process.stderr.write('Form opened\n');

  // בדיקה: האם החשבונית כבר סגורה (IVNUM לא זמני)?
  const rowsInit = await withTimeout(
    new Promise((res, rej) => form.getRows(1, res, rej)),
    15000, 'getRows init'
  );
  const initRow = (rowsInit && rowsInit.PINVOICES) ? (rowsInit.PINVOICES['1'] || Object.values(rowsInit.PINVOICES)[0] || {}) : {};
  process.stderr.write(`Initial IVNUM: ${initRow.IVNUM} FNCNUM: ${initRow.FNCNUM}\n`);

  if (initRow.IVNUM && !isTempIvnum(initRow.IVNUM)) {
    // חשבונית כבר סגורה עם IVNUM סופי
    process.stderr.write(`Invoice already finalized: ${initRow.IVNUM}\n`);
    await form.endCurrentForm(false).catch(() => {});
    console.log(JSON.stringify({ ok: true, fncnum: initRow.FNCNUM || '', ivnum: initRow.IVNUM }));
    return;
  }

  if (!initRow.IVNUM) {
    // T-number לא נמצא בפריורטי — כנראה כבר הוסב; מחזיר שגיאה ל-Python לטיפול
    process.stderr.write(`T-number ${ivnum} not found in PINVOICES (may already be converted)\n`);
    await form.endCurrentForm(false).catch(() => {});
    console.log(JSON.stringify({ ok: false, error: `T_NOT_FOUND:${ivnum}` }));
    return;
  }

  // צירוף קובץ (אם סופק)
  if (filePath && fs.existsSync(filePath)) {
    process.stderr.write('Opening EXTFILES subform...\n');
    try {
      const sub = await withTimeout(
        new Promise((res, rej) => form.startSubForm(
          'EXTFILES', makeMessageHandler('EXTFILES'), null, res, rej
        )),
        20000, 'startSubForm EXTFILES'
      );
      await withTimeout(new Promise((res, rej) => sub.newRow(res, rej)), 10000, 'newRow');
      const ext  = path.extname(filePath).toLowerCase().replace('.', '');
      const mime = ext === 'pdf' ? 'application/pdf' : `image/${ext}`;
      const data = `data:${mime};base64,` + fs.readFileSync(filePath).toString('base64');
      await withTimeout(
        new Promise((res, rej) => sub.uploadDataUrl(data, ext, () => {}, res, rej)),
        60000, 'uploadDataUrl'
      );
      await withTimeout(new Promise((res, rej) => sub.saveRow(false, res, rej)), 15000, 'saveRow');
      await withTimeout(new Promise((res, rej) => sub.endCurrentForm(false, res, rej)), 15000, 'endSubForm');
      process.stderr.write('File attached\n');
    } catch (e) {
      process.stderr.write(`EXTFILES attach failed (continuing): ${e.message}\n`);
    }
  }

  // ===== סגירת החשבונית =====
  // CLOSEPRINTPIV עם activateStart('CLOSEPRINTPIV', 'P') — מחזיר 'client' כשהסגירה הצליחה בשרת
  const procAttempts = [
    { method: 'activateStart', proc: 'CLOSEPRINTPIV', type: 'P' },
    { method: 'activateStart', proc: 'CLOSEPIV',      type: 'P' },
    { method: 'procStart',     proc: 'CLOSEPRINTPIV', type: 'P' },
    { method: 'procStart',     proc: 'CLOSEPIV',      type: 'P' },
  ];

  let procSucceeded = false;
  for (const attempt of procAttempts) {
    const { method, proc, type } = attempt;
    process.stderr.write(`Trying ${method}('${proc}', '${type}')...\n`);
    let firstStep;
    if (method === 'activateStart') {
      firstStep = await withTimeout(
        new Promise((res, rej) => form.activateStart(proc, type, null, res, rej)),
        30000, `activateStart ${proc}`
      ).catch(e => ({ type: 'error_caught', error: e.message }));
    } else {
      firstStep = await withTimeout(
        new Promise((res, rej) => priority.procStart(proc, type, null, company, res, rej)),
        30000, `procStart ${proc}`
      ).catch(e => ({ type: 'error_caught', error: e.message }));
    }
    process.stderr.write(`${method} ${proc}: firstStep.type=${firstStep.type}\n`);
    if (firstStep.type === 'client' || firstStep.type === 'displayUrl') {
      process.stderr.write(`${method} ${proc}: client step data=${JSON.stringify({url: firstStep.url, message: firstStep.message, displayUrl: firstStep.displayUrl, proc: !!firstStep.proc})}\n`);
    }

    if (isNotFoundError(firstStep)) continue; // לא קיים — נסה הבא

    const result = await runProcedure(firstStep, `${method}_${proc}`, ivnum);
    process.stderr.write(`${method} ${proc}: result=${JSON.stringify(result)}\n`);
    if (result.ok) { procSucceeded = true; break; }
  }

  process.stderr.write(`Proc loop done. procSucceeded=${procSucceeded}\n`);

  await withTimeout(new Promise((res, rej) => form.activateEnd(res, rej)), 10000, 'activateEnd').catch(() => {});

  // תמיד קרא את השורה אחרי — ה-IVNUM האמיתי הוא הבדיקה האמיתית
  const rowsAfter = await withTimeout(
    new Promise((res, rej) => form.getRows(1, res, rej)),
    15000, 'getRows after'
  ).catch(() => null);

  const rows2 = (rowsAfter && rowsAfter.PINVOICES) ? rowsAfter.PINVOICES : {};
  const row    = rows2['1'] || rows2[Object.keys(rows2)[0]] || {};
  const fncnum = row.FNCNUM || '';
  const ivnumFinal = row.IVNUM || '';
  process.stderr.write(`After: IVNUM=${ivnumFinal} FNCNUM=${fncnum}\n`);

  await withTimeout(new Promise((res, rej) => form.endCurrentForm(false, res, rej)), 15000, 'endForm').catch(() => {});

  // ===== שחזור FNCPATNAME המקורי אחרי סגירת PINVOICES =====
  if (vatType === 'two_thirds' && supplierCode && _origFncpatname && _origFncpatname !== '2/3') {
    await getAndSetSupplierFncpatname(company, supplierCode, _origFncpatname);
  }

  // צירוף קובץ + עדכון FNCPATNAME לתנועת יומן (FNCTRANS) — אחרי CLOSEPRINTPIV יש FNCNUM
  const needFnctrans = (filePath && fs.existsSync(filePath) && fncnum && !isTempIvnum(ivnumFinal))
                     || (vatType === 'two_thirds' && fncnum && !isTempIvnum(ivnumFinal));
  if (needFnctrans) {
    process.stderr.write(`Opening FNCTRANS ${fncnum} (file=${!!filePath} vatType=${vatType})...\n`);
    try {
      const journalForm = await withTimeout(
        new Promise((res, rej) => priority.formStartEx(
          'FNCTRANS',
          makeMessageHandler('FNCTRANS'),
          null,
          company, 1, { zoomValue: fncnum }, res, rej
        )),
        30000, 'formStartEx FNCTRANS'
      );

      // ===== עדכון FNCPATNAME ל-'2/3' ישירות על FNCTRANS =====
      if (vatType === 'two_thirds') {
        try {
          const fncTransRows = await withTimeout(new Promise((res, rej) => journalForm.getRows(1, res, rej)), 10000, 'getRows FNCTRANS');
          const fncTransRow = (fncTransRows && fncTransRows.FNCTRANS) ? (fncTransRows.FNCTRANS['1'] || Object.values(fncTransRows.FNCTRANS)[0] || {}) : {};
          process.stderr.write(`[FNCTRANS] current FNCPATNAME="${fncTransRow.FNCPATNAME || '(empty)'}" all_keys=${Object.keys(fncTransRow).join(',')}\n`);
          await withTimeout(new Promise((res, rej) => journalForm.setActiveRow(1, res, rej)), 10000, 'setActiveRow FNCTRANS');
          await withTimeout(new Promise((res, rej) => journalForm.fieldUpdate('FNCPATNAME', '2/3', res, rej)), 10000, 'fieldUpdate FNCTRANS.FNCPATNAME');
          await withTimeout(new Promise((res, rej) => journalForm.saveRow(false, res, rej)), 15000, 'saveRow FNCTRANS.FNCPATNAME');
          process.stderr.write(`[FNCTRANS] FNCPATNAME update saved\n`);
        } catch(e) {
          process.stderr.write(`[FNCTRANS] FNCPATNAME update failed (continuing): ${e.message}\n`);
        }
      }

      if (filePath && fs.existsSync(filePath)) {
        const jSub = await withTimeout(
          new Promise((res, rej) => journalForm.startSubForm(
            'EXTFILES', makeMessageHandler('FNCTRANS.EXTFILES'), null, res, rej
          )),
          20000, 'startSubForm FNCTRANS.EXTFILES'
        );
        await withTimeout(new Promise((res, rej) => jSub.newRow(res, rej)), 10000, 'newRow FNCTRANS');
        const jExt  = path.extname(filePath).toLowerCase().replace('.', '');
        const jMime = jExt === 'pdf' ? 'application/pdf' : `image/${jExt}`;
        const jData = `data:${jMime};base64,` + fs.readFileSync(filePath).toString('base64');
        await withTimeout(
          new Promise((res, rej) => jSub.uploadDataUrl(jData, jExt, () => {}, res, rej)),
          60000, 'uploadDataUrl FNCTRANS'
        );
        await withTimeout(new Promise((res, rej) => jSub.saveRow(false, res, rej)), 15000, 'saveRow FNCTRANS');
        await withTimeout(new Promise((res, rej) => jSub.endCurrentForm(false, res, rej)), 15000, 'endSubForm FNCTRANS');
        process.stderr.write(`File attached to FNCTRANS ${fncnum}\n`);
      }

      await withTimeout(new Promise((res, rej) => journalForm.endCurrentForm(false, res, rej)), 15000, 'endForm FNCTRANS');
    } catch (e) {
      process.stderr.write(`FNCTRANS section failed (continuing): ${e.message}\n`);
    }
  }

  // צירוף קובץ ל-PINVOICES עם ה-IVNUM הסופי (החשבונית שנסגרה)
  if (filePath && fs.existsSync(filePath) && ivnumFinal && !isTempIvnum(ivnumFinal)) {
    process.stderr.write(`Attaching file to final PINVOICES ${ivnumFinal}...\n`);
    try {
      const finalInvForm = await withTimeout(
        new Promise((res, rej) => priority.formStartEx(
          'PINVOICES',
          makeMessageHandler('PINVOICES_FINAL'),
          null,
          company, 1, { zoomValue: ivnumFinal }, res, rej
        )),
        30000, 'formStartEx PINVOICES final'
      );
      const pinvSub = await withTimeout(
        new Promise((res, rej) => finalInvForm.startSubForm(
          'EXTFILES', makeMessageHandler('PINVOICES_FINAL.EXTFILES'), null, res, rej
        )),
        20000, 'startSubForm PINVOICES EXTFILES'
      );
      await withTimeout(new Promise((res, rej) => pinvSub.newRow(res, rej)), 10000, 'newRow PINVOICES');
      const pExt  = path.extname(filePath).toLowerCase().replace('.', '');
      const pMime = pExt === 'pdf' ? 'application/pdf' : `image/${pExt}`;
      const pData = `data:${pMime};base64,` + fs.readFileSync(filePath).toString('base64');
      await withTimeout(
        new Promise((res, rej) => pinvSub.uploadDataUrl(pData, pExt, () => {}, res, rej)),
        60000, 'uploadDataUrl PINVOICES'
      );
      await withTimeout(new Promise((res, rej) => pinvSub.saveRow(false, res, rej)), 15000, 'saveRow PINVOICES');
      await withTimeout(new Promise((res, rej) => pinvSub.endCurrentForm(false, res, rej)), 15000, 'endSubForm PINVOICES');
      await withTimeout(new Promise((res, rej) => finalInvForm.endCurrentForm(false, res, rej)), 15000, 'endForm PINVOICES');
      process.stderr.write(`File attached to PINVOICES ${ivnumFinal}\n`);
    } catch (e) {
      process.stderr.write(`PINVOICES EXTFILES final attach failed: ${e.message}\n`);
    }
  }

  // החשבונית נסגרה אם ה-IVNUM הוא כבר מספר סופי (לא T)
  if (!isTempIvnum(ivnumFinal) && ivnumFinal) {
    console.log(JSON.stringify({ ok: true, fncnum, ivnum: ivnumFinal, fncsupSaved: _fncsupSaved }));
  } else {
    console.log(JSON.stringify({ ok: false, error: `Invoice still has temp IVNUM: ${ivnumFinal || 'none'}. Check server logs.`, fncsupSaved: _fncsupSaved }));
  }
}

main().catch(err => {
  console.log(JSON.stringify({ ok: false, error: err.message }));
  process.exit(1);
});
