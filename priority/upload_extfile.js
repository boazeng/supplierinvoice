'use strict';
/**
 * Attaches a PDF/image file to a PINVOICES record in Priority via the WCF Web SDK.
 *
 * Usage (from Python via subprocess):
 *   node upload_extfile.js <IVNUM> <IVTYPE> <DEBIT> <filePath> <fileDescription>
 *
 * Returns JSON to stdout:
 *   { ok: true, extfilenum: <n> }
 *   { ok: false, error: "<message>" }
 */

const path = require('path');
const fs   = require('fs');

// Load .env from the project root (two levels up from priority/)
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
  const [ivnum, ivtype, debit, filePath, fileDesc] = process.argv.slice(2);
  if (!ivnum || !filePath) {
    throw new Error('Usage: node upload_extfile.js <IVNUM> <IVTYPE> <DEBIT> <filePath> [description]');
  }

  if (!fs.existsSync(filePath)) throw new Error('File not found: ' + filePath);

  const odataUrl = process.env.PRIORITY_URL_REAL || '';
  const { serviceUrl, tabulaini, company } = parseOdataUrl(odataUrl);

  // Login
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

  // Open PINVOICES form directly on the specific invoice (zoomValue navigates to the record)
  process.stderr.write(`Opening PINVOICES form for invoice ${ivnum}...\n`);
  const form = await withTimeout(
    new Promise((res, rej) => priority.formStartEx(
      'PINVOICES',
      (msg) => process.stderr.write(`MSG: ${JSON.stringify(msg)}\n`),
      (fields) => process.stderr.write(`FORM FIELDS: ${JSON.stringify(fields)}\n`),
      company,
      1,
      { zoomValue: ivnum },
      res, rej
    )),
    30000, 'formStartEx'
  );
  process.stderr.write('Form opened\n');

  // Verify we are on the correct record
  const rows = await withTimeout(
    new Promise((res, rej) => form.getRows(1, res, rej)),
    15000, 'getRows'
  );
  process.stderr.write(`Rows: ${JSON.stringify(rows)}\n`);

  // Open EXTFILES subform (subform name is EXTFILES in Priority)
  process.stderr.write('Opening EXTFILES subform...\n');
  const subForm = await withTimeout(
    new Promise((res, rej) => form.startSubForm(
      'EXTFILES',
      (msg) => process.stderr.write(`SUB MSG: ${JSON.stringify(msg)}\n`),
      (fields) => process.stderr.write(`SUB FIELDS: ${JSON.stringify(fields)}\n`),
      res, rej
    )),
    20000, 'startSubForm'
  );
  process.stderr.write('EXTFILES subform opened\n');

  // Create new row in EXTFILES, then upload
  await withTimeout(
    new Promise((res, rej) => subForm.newRow(res, rej)),
    15000, 'newRow'
  );
  process.stderr.write('New EXTFILES row created\n');

  // Upload the file as data URL
  process.stderr.write(`Uploading ${filePath}...\n`);
  const fileBuffer = fs.readFileSync(filePath);
  const ext = path.extname(filePath).toLowerCase().replace('.', '');
  const mimeType = ext === 'pdf' ? 'application/pdf' : `image/${ext}`;
  const dataUrl = `data:${mimeType};base64,` + fileBuffer.toString('base64');

  const uploadResult = await withTimeout(
    new Promise((res, rej) =>
      subForm.uploadDataUrl(dataUrl, ext, (pct) => process.stderr.write(`Upload: ${pct}%\n`), res, rej)
    ),
    60000, 'uploadDataUrl'
  );
  process.stderr.write(`Upload result: ${JSON.stringify(uploadResult)}\n`);

  // Check current row data after upload
  const rowsAfterUpload = await withTimeout(
    new Promise((res, rej) => subForm.getRows(1, res, rej)),
    15000, 'getRows after upload'
  );
  process.stderr.write(`EXTFILES rows after upload: ${JSON.stringify(rowsAfterUpload)}\n`);

  // Save the row
  const saveResult = await withTimeout(
    new Promise((res, rej) => subForm.saveRow(false, res, rej)),
    15000, 'saveRow'
  );
  process.stderr.write(`saveRow result: ${JSON.stringify(saveResult)}\n`);

  // Exit the subform back to parent (commits to DB)
  const exitResult = await withTimeout(
    new Promise((res, rej) => subForm.endCurrentForm(false, res, rej)),
    15000, 'endCurrentForm'
  );
  process.stderr.write(`endCurrentForm result: ${JSON.stringify(exitResult)}\n`);

  process.stderr.write('File attached successfully\n');
  console.log(JSON.stringify({ ok: true, extfilenum: uploadResult }));
}

main().catch(err => {
  console.log(JSON.stringify({ ok: false, error: err.message }));
  process.exit(1);
});
