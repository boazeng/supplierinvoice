'use strict';
/**
 * מצרף קובץ ומבצע CLOSEPIV/CLOSEPRINTPIV על חשבונית בפריורטי דרך WCF Web SDK.
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
 * מטפל במהלך ריצת פרוצדורה (ProcData) — מאשר הודעות ומסיים
 * מחזיר { ok: true } כאשר הפרוצדורה הסתיימה, או { ok: false, error } בשגיאה
 */
async function runProcedure(procData, label) {
  let step = procData;
  let iter = 0;
  while (step && iter++ < 20) {
    process.stderr.write(`[${label}] proc step type=${step.type} messagetype=${step.messagetype || ''} message=${step.message || ''}\n`);
    const proc = step.proc;

    if (step.type === 'end') {
      return { ok: true };
    }

    if (step.type === 'message') {
      if (step.messagetype === 'error') {
        return { ok: false, error: step.message || 'Procedure error' };
      }
      // warning / information — אשר אוטומטית
      if (!proc) return { ok: false, error: 'No proc object on message step' };
      step = await withTimeout(
        new Promise((res, rej) => proc.message(1, res, rej)),
        30000, `${label} message`
      ).catch(e => ({ type: 'error_caught', error: e.message }));
      if (step && step.type === 'error_caught') return { ok: false, error: step.error };
      continue;
    }

    if (step.type === 'inputFields') {
      if (!proc) return { ok: false, error: 'No proc on inputFields' };
      step = await withTimeout(
        new Promise((res, rej) => proc.inputFields(1, { EditFields: [] }, res, rej)),
        30000, `${label} inputFields`
      ).catch(e => ({ type: 'error_caught', error: e.message }));
      if (step && step.type === 'error_caught') return { ok: false, error: step.error };
      continue;
    }

    if (step.type === 'inputOptions') {
      if (!proc) return { ok: false, error: 'No proc on inputOptions' };
      const sel = (step.input && step.input.Options && step.input.Options[0]) ? step.input.Options[0].field : 1;
      step = await withTimeout(
        new Promise((res, rej) => proc.inputOptions(1, sel, res, rej)),
        30000, `${label} inputOptions`
      ).catch(e => ({ type: 'error_caught', error: e.message }));
      if (step && step.type === 'error_caught') return { ok: false, error: step.error };
      continue;
    }

    if (step.type === 'reportOptions' || step.type === 'documentOptions') {
      if (!proc) return { ok: false, error: 'No proc on options' };
      const fmt = (step.formats && step.formats[0]) ? step.formats[0].format : 1;
      const fn = step.type === 'reportOptions' ? 'reportOptions' : 'documentOptions';
      step = await withTimeout(
        new Promise((res, rej) => proc[fn](1, fmt, 1, res, rej)),
        30000, `${label} ${fn}`
      ).catch(e => ({ type: 'error_caught', error: e.message }));
      if (step && step.type === 'error_caught') return { ok: false, error: step.error };
      continue;
    }

    if (step.type === 'displayUrl') {
      // דוח/מסמך — נגמר
      return { ok: true };
    }

    // שלב לא מוכר — נסה להמשיך
    if (proc && proc.continueProc) {
      step = await withTimeout(
        new Promise((res, rej) => proc.continueProc(res, rej)),
        30000, `${label} continueProc`
      ).catch(e => ({ type: 'error_caught', error: e.message }));
      if (step && step.type === 'error_caught') return { ok: false, error: step.error };
    } else {
      return { ok: false, error: `Unknown proc step type: ${step.type}` };
    }
  }
  return { ok: false, error: 'Procedure loop exceeded limit' };
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

  // הדפס מידע על הטופס — subforms וכו'
  process.stderr.write(`Form subForms: ${JSON.stringify(Object.keys(form.subForms || {}))}\n`);

  const rows = await withTimeout(
    new Promise((res, rej) => form.getRows(1, res, rej)),
    15000, 'getRows'
  );
  process.stderr.write(`Row data: ${JSON.stringify(rows)}\n`);

  // צירוף קובץ (אם סופק)
  if (filePath && fs.existsSync(filePath)) {
    process.stderr.write('Opening EXTFILES subform...\n');
    const sub = await withTimeout(
      new Promise((res, rej) => form.startSubForm(
        'EXTFILES',
        makeMessageHandler('EXTFILES'),
        null, res, rej
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
  }

  // ניסיון לסגור — activateStart עם type='P', ואחר כך procStart
  const procNames = [
    'CLOSEPIV', 'CLOSEPRINTPIV', 'PRICLOSEPIV',
    'CPIV', 'CONFIRMIV', 'PCLOSEPIV', 'PCLOSINV',
    'FCLOSEPIV', 'ACLOSEPIV', 'SUPINVCLS', 'PIVCLS',
  ];
  let closeResult = null;
  let usedProc = '';
  let usedMethod = '';

  // שיטה 1: form.activateStart עם type='P'
  for (const proc of procNames) {
    process.stderr.write(`Trying activateStart('${proc}', 'P')...\n`);
    const firstStep = await withTimeout(
      new Promise((res, rej) => form.activateStart(proc, 'P', null, res, rej)),
      30000, `activateStart ${proc}`
    ).catch(e => ({ type: 'error_caught', error: e.message, messagetype: 'error', message: e.message }));

    process.stderr.write(`${proc} firstStep: ${JSON.stringify(firstStep)}\n`);

    const isNotFound = (firstStep.type === 'message' && firstStep.messagetype === 'error' &&
        firstStep.message && firstStep.message.includes('No such')) ||
      (firstStep.type === 'error_caught' && firstStep.error && firstStep.error.includes('No such'));
    if (isNotFound) continue;

    if (firstStep.type === 'end') {
      usedProc = proc; usedMethod = 'activateStart'; break;
    }

    closeResult = await runProcedure(firstStep, proc);
    process.stderr.write(`${proc} runProcedure result: ${JSON.stringify(closeResult)}\n`);
    if (closeResult && closeResult.ok) {
      usedProc = proc; usedMethod = 'activateStart'; break;
    }
    // אם לא "not found" — עצור (הפרוצדורה נמצאה אבל נכשלה)
    if (closeResult && !closeResult.error?.includes('No such')) {
      usedProc = proc; usedMethod = 'activateStart(failed)'; break;
    }
  }

  // שיטה 2: procStart (top-level, standalone)
  if (!usedProc) {
    for (const proc of procNames) {
      process.stderr.write(`Trying procStart('${proc}', 'P')...\n`);
      const firstStep = await withTimeout(
        new Promise((res, rej) => priority.procStart(proc, 'P', null, company, res, rej)),
        30000, `procStart ${proc}`
      ).catch(e => ({ type: 'error_caught', error: e.message, messagetype: 'error', message: e.message }));

      process.stderr.write(`procStart ${proc} firstStep: ${JSON.stringify(firstStep)}\n`);

      const isNotFound = (firstStep.type === 'message' && firstStep.messagetype === 'error' &&
          firstStep.message && firstStep.message.includes('No such')) ||
        (firstStep.type === 'error_caught' && firstStep.error && firstStep.error.includes('No such'));
      if (isNotFound) continue;

      if (firstStep.type === 'end') {
        usedProc = proc; usedMethod = 'procStart'; break;
      }

      closeResult = await runProcedure(firstStep, `procStart_${proc}`);
      process.stderr.write(`procStart ${proc} runProcedure result: ${JSON.stringify(closeResult)}\n`);
      if (closeResult && closeResult.ok) {
        usedProc = proc; usedMethod = 'procStart'; break;
      }
      if (closeResult && !closeResult.error?.includes('No such')) {
        usedProc = proc; usedMethod = 'procStart(failed)'; break;
      }
    }
  }

  process.stderr.write(`Used procedure: ${usedProc || 'NONE'} via ${usedMethod || 'N/A'}\n`);

  // קריאת IVNUM ו-FNCNUM אחרי הסגירה
  await withTimeout(new Promise((res, rej) => form.activateEnd(res, rej)), 10000, 'activateEnd').catch(() => {});
  const rowsAfter = await withTimeout(
    new Promise((res, rej) => form.getRows(1, res, rej)),
    15000, 'getRows after close'
  );
  process.stderr.write(`Rows after: ${JSON.stringify(rowsAfter)}\n`);

  const rows2 = (rowsAfter && rowsAfter.PINVOICES) ? rowsAfter.PINVOICES : {};
  const row    = rows2['1'] || rows2[Object.keys(rows2)[0]] || {};
  const fncnum = row.FNCNUM || '';
  const ivnumFinal = row.IVNUM || '';

  await withTimeout(new Promise((res, rej) => form.endCurrentForm(false, res, rej)), 15000, 'endForm').catch(() => {});

  if (!usedProc || (closeResult && !closeResult.ok)) {
    const errMsg = (closeResult && closeResult.error) || 'No close procedure found among: ' + procNames.join(', ');
    console.log(JSON.stringify({ ok: false, error: errMsg }));
    return;
  }
  console.log(JSON.stringify({ ok: true, fncnum, ivnum: ivnumFinal }));
}

main().catch(err => {
  console.log(JSON.stringify({ ok: false, error: err.message }));
  process.exit(1);
});
