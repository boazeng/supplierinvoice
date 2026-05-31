'use strict';
/**
 * מצרף קובץ ומבצע CLOSEPRINTPIV על חשבונית בפריורטי דרך WCF Web SDK.
 *
 * Usage:
 *   node finalize_invoice.js <IVNUM> <filePath>
 *
 * Returns JSON to stdout:
 *   { ok: true, fncnum: <n> }
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
      'PINVOICES', null, null, company, 1, { zoomValue: ivnum }, res, rej
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
      new Promise((res, rej) => form.startSubForm('EXTFILES', null, null, res, rej)),
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

  // CLOSEPRINTPIV — אישור וסגירה
  process.stderr.write('Calling CLOSEPRINTPIV...\n');
  const closeResult = await withTimeout(
    new Promise((res, rej) => form.activateStart('CLOSEPRINTPIV', null, null, res, rej)),
    30000, 'activateStart CLOSEPRINTPIV'
  );
  process.stderr.write(`CLOSEPRINTPIV result: ${JSON.stringify(closeResult)}\n`);

  // קריאת FNCNUM אחרי הסגירה
  const rowsAfter = await withTimeout(
    new Promise((res, rej) => form.getRows(1, res, rej)),
    15000, 'getRows after close'
  );
  process.stderr.write(`Rows after: ${JSON.stringify(rowsAfter)}\n`);

  const fncnum = (rowsAfter && rowsAfter.PINVOICES && rowsAfter.PINVOICES[0]
    ? rowsAfter.PINVOICES[0].FNCNUM : null) || '';

  await withTimeout(new Promise((res, rej) => form.endCurrentForm(false, res, rej)), 15000, 'endForm');

  console.log(JSON.stringify({ ok: true, fncnum }));
}

main().catch(err => {
  console.log(JSON.stringify({ ok: false, error: err.message }));
  process.exit(1);
});
