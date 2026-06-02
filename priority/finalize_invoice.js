'use strict';
/**
 * מצרף קובץ ומבצע CLOSEPIV על חשבונית בפריורטי דרך WCF Web SDK.
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

require('dotenv').config({ path: path.join(__dirname, '..', '.env') });

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

/**
 * מטפל במהלך ריצת פרוצדורה — מאשר הודעות ומסיים
 * ivnumVal: IVNUM שייכנס לשדה קלט אם הפרוצדורה מבקשת
 */
async function runProcedure(procData, label, ivnumVal) {
  let step = procData;
  let iter = 0;
  while (step && iter++ < 20) {
    const t = step.type || 'unknown';
    const msg = step.message || '';
    const mtype = step.messagetype || '';
    process.stderr.write(`[${label}] step=${t} messagetype=${mtype} message=${msg}\n`);

    if (t === 'end') return { ok: true };

    if (t === 'error_caught') return { ok: false, error: step.error || msg };

    const proc = step.proc;

    if (t === 'message') {
      if (mtype === 'error') return { ok: false, error: msg || 'Procedure error' };
      if (!proc) return { ok: false, error: 'No proc on message' };
      step = await withTimeout(
        new Promise((res, rej) => proc.message(1, res, rej)),
        30000, `${label}.message`
      ).catch(e => ({ type: 'error_caught', error: e.message }));
      continue;
    }

    if (t === 'inputFields') {
      if (!proc) return { ok: false, error: 'No proc on inputFields' };
      const editFields = (step.input && step.input.EditFields) || [];
      process.stderr.write(`[${label}] inputFields: ${JSON.stringify(editFields.map(f=>({field:f.field,title:f.title,code:f.code})))}\n`);
      // מלא את כל שדות ה-IVNUM/invoice עם ה-IVNUM שלנו, שאר השדות ריקים
      const filled = editFields.map(f => ({
        field: f.field,
        op: f.operator || 0,
        value: (f.title && (f.title.includes('IVNUM') || f.title.includes('חשבונית') || f.title.includes('invoice'))) ? (ivnumVal || '') : (f.value || ''),
        op2: 0,
        value2: '',
      }));
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

    if (t === 'inputHelp') {
      if (!proc) return { ok: false, error: 'No proc on inputHelp' };
      step = await withTimeout(
        new Promise((res, rej) => proc.inputHelp(1, res, rej)),
        30000, `${label}.inputHelp`
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

    if (t === 'displayUrl') return { ok: true };

    if (proc && proc.continueProc) {
      step = await withTimeout(
        new Promise((res, rej) => proc.continueProc(res, rej)),
        30000, `${label}.continueProc`
      ).catch(e => ({ type: 'error_caught', error: e.message }));
    } else {
      return { ok: false, error: `Unknown step type: ${t}` };
    }
  }
  return { ok: false, error: 'Loop limit exceeded' };
}

function isNotFoundError(err) {
  if (!err) return false;
  const s = err.toLowerCase();
  return s.includes('no such') || s.includes('not found') || s.includes('timed out');
}

async function main() {
  const [ivnum, filePath] = process.argv.slice(2);
  if (!ivnum) throw new Error('Usage: node finalize_invoice.js <IVNUM> [filePath]');

  const odataUrl = process.env.PRIORITY_URL_REAL || '';
  const { serviceUrl, tabulaini, company } = parseOdataUrl(odataUrl);

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
  process.stderr.write(`Form subForms: ${JSON.stringify(Object.keys(form.subForms || {}))}\n`);

  const rows = await withTimeout(
    new Promise((res, rej) => form.getRows(1, res, rej)),
    15000, 'getRows'
  );
  process.stderr.write(`Row data IVNUM: ${rows && rows.PINVOICES && rows.PINVOICES['1'] ? rows.PINVOICES['1'].IVNUM : 'N/A'}\n`);

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
      process.stderr.write(`EXTFILES attach failed: ${e.message}\n`);
    }
  }

  // ===== ניסיון לסגור =====
  // שיטה 1: form.activateStart (all types)
  const procNames = ['CLOSEPIV', 'CLOSEPRINTPIV', 'PRICLOSEPIV', 'CPIV', 'PCLOSEPIV'];
  const procTypes = ['P', 'R'];
  let usedProc = '';
  let usedMethod = '';
  let closeResult = null;

  outer1:
  for (const proc of procNames) {
    for (const ptype of procTypes) {
      process.stderr.write(`Trying activateStart('${proc}', '${ptype}')...\n`);
      const firstStep = await withTimeout(
        new Promise((res, rej) => form.activateStart(proc, ptype, null, res, rej)),
        30000, `activateStart ${proc}`
      ).catch(e => ({ type: 'error_caught', error: e.message }));
      process.stderr.write(`activateStart ${proc}/${ptype}: ${JSON.stringify(firstStep)}\n`);

      const err0 = (firstStep.type === 'error_caught' ? firstStep.error : firstStep.message) || '';
      if (isNotFoundError(err0)) continue; // not found — try next

      if (firstStep.type === 'end') { usedProc = proc; usedMethod = `activateStart/${ptype}`; break outer1; }

      closeResult = await runProcedure(firstStep, `${proc}/${ptype}`, ivnum);
      if (closeResult.ok) { usedProc = proc; usedMethod = `activateStart/${ptype}`; break outer1; }
    }
  }

  // שיטה 2: procStart (standalone, לא צמוד לטופס)
  if (!usedProc) {
    for (const proc of procNames) {
      process.stderr.write(`Trying procStart('${proc}', 'P')...\n`);
      const firstStep = await withTimeout(
        new Promise((res, rej) => priority.procStart(proc, 'P', null, company, res, rej)),
        30000, `procStart ${proc}`
      ).catch(e => ({ type: 'error_caught', error: e.message }));
      process.stderr.write(`procStart ${proc}: ${JSON.stringify(firstStep)}\n`);

      const err0 = (firstStep.type === 'error_caught' ? firstStep.error : firstStep.message) || '';
      if (isNotFoundError(err0)) continue;

      if (firstStep.type === 'end') { usedProc = proc; usedMethod = 'procStart'; break; }

      closeResult = await runProcedure(firstStep, `procStart_${proc}`, ivnum);
      if (closeResult.ok) { usedProc = proc; usedMethod = 'procStart'; break; }
    }
  }

  process.stderr.write(`Close result: usedProc=${usedProc} method=${usedMethod} result=${JSON.stringify(closeResult)}\n`);

  await withTimeout(new Promise((res, rej) => form.activateEnd(res, rej)), 10000, 'activateEnd').catch(() => {});

  // קריאת IVNUM ו-FNCNUM אחרי הסגירה
  const rowsAfter = await withTimeout(
    new Promise((res, rej) => form.getRows(1, res, rej)),
    15000, 'getRows after close'
  ).catch(() => null);
  process.stderr.write(`rowsAfter: ${JSON.stringify(rowsAfter && rowsAfter.PINVOICES ? rowsAfter.PINVOICES['1'] : null)}\n`);

  const rows2 = (rowsAfter && rowsAfter.PINVOICES) ? rowsAfter.PINVOICES : {};
  const row    = rows2['1'] || rows2[Object.keys(rows2)[0]] || {};
  const fncnum = row.FNCNUM || '';
  const ivnumFinal = row.IVNUM || '';

  await withTimeout(new Promise((res, rej) => form.endCurrentForm(false, res, rej)), 15000, 'endForm').catch(() => {});

  if (!usedProc || (closeResult && !closeResult.ok)) {
    const errMsg = (closeResult && closeResult.error) || 'No close procedure worked. Check stderr.';
    console.log(JSON.stringify({ ok: false, error: errMsg }));
    return;
  }
  console.log(JSON.stringify({ ok: true, fncnum, ivnum: ivnumFinal }));
}

main().catch(err => {
  console.log(JSON.stringify({ ok: false, error: err.message }));
  process.exit(1);
});
