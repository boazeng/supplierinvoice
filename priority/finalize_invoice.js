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

/**
 * בונה onShowMessage handler שמאשר אוטומטית כל הודעה מפריורטי.
 * CLOSEPRINTPIV עשוי להציג דיאלוג אישור — בלי handler זה הפעולה תיתקע.
 */
function makeMessageHandler(label) {
  return function onShowMessage(msg) {
    process.stderr.write(`[${label}] Priority message: code=${msg.code} type=${msg.type} msg=${msg.message}\n`);
    if (msg.form) {
      try {
        // warningConfirm(1) = OK/Yes לכל סוגי ההודעות
        msg.form.warningConfirm(1);
      } catch (e) {
        try { msg.form.infoMsgConfirm(); } catch (_) {}
      }
    }
  };
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
      makeMessageHandler('PINVOICES'),  // onShowMessage — מאשר הודעות אוטומטית
      null,
      company, 1, { zoomValue: ivnum }, res, rej
    )),
    30000, 'formStartEx'
  );
  process.stderr.write('Form opened\n');

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
        makeMessageHandler('EXTFILES'),  // onShowMessage לתת-טופס
        null,
        res, rej
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

  // ניסיון לסגור — בודק רשימת שמות פרוצדורות אפשריים
  const procNames = [
    'CLOSEPIV', 'CLOSEPRINTPIV', 'PRICLOSEPIV', 'CONFIRM',
    'APPROVE', 'FINALIZE', 'CLOSE', 'INVCLOSE', 'DOCCLOSE'
  ];
  let closeResult = null;
  let usedProc = '';
  for (const proc of procNames) {
    process.stderr.write(`Trying ${proc}...\n`);
    closeResult = await withTimeout(
      new Promise((res, rej) => form.activateStart(proc, null, null, res, rej)),
      30000, `activateStart ${proc}`
    ).catch(e => ({ messagetype: 'error', message: e.message }));
    process.stderr.write(`${proc} result: ${JSON.stringify(closeResult)}\n`);
    const isNotFound = closeResult && closeResult.messagetype === 'error' &&
        closeResult.message && (
          closeResult.message.includes('No such') ||
          closeResult.message.includes('not found') ||
          closeResult.message.includes('timed out')
        );
    if (isNotFound) continue;
    usedProc = proc;
    break;
  }
  process.stderr.write(`Used procedure: ${usedProc || 'NONE — all failed'}\n`);

  // קריאת IVNUM ו-FNCNUM אחרי הסגירה
  const rowsAfter = await withTimeout(
    new Promise((res, rej) => form.getRows(1, res, rej)),
    15000, 'getRows after close'
  );
  process.stderr.write(`Rows after: ${JSON.stringify(rowsAfter)}\n`);

  const rows2 = rowsAfter && rowsAfter.PINVOICES ? rowsAfter.PINVOICES : {};
  const row    = rows2['1'] || rows2[Object.keys(rows2)[0]] || {};
  const fncnum = row.FNCNUM || '';
  const ivnumFinal = row.IVNUM || '';

  await withTimeout(new Promise((res, rej) => form.endCurrentForm(false, res, rej)), 15000, 'endForm');

  if (!usedProc) {
    console.log(JSON.stringify({ ok: false, error: 'No close procedure found among: ' + procNames.join(', ') }));
    return;
  }
  console.log(JSON.stringify({ ok: true, fncnum, ivnum: ivnumFinal }));
}

main().catch(err => {
  console.log(JSON.stringify({ ok: false, error: err.message }));
  process.exit(1);
});
