'use strict';
/**
 * בדיקה: האם WCF SDK יכול לפתוח FNCSUP ולקרוא FNCPATNAME של ספק?
 * Usage: node test_fncsup.js <SUPNAME>
 * Example: node test_fncsup.js 60463
 */

const path = require('path');
const fs   = require('fs');

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
  return function(msg) {
    process.stderr.write(`[${label}] message: code=${msg.code} type=${msg.type} msg=${msg.message}\n`);
    if (msg.form) {
      try { msg.form.warningConfirm(1); } catch (e) {
        try { msg.form.infoMsgConfirm(); } catch (_) {}
      }
    }
  };
}

async function main() {
  const supplierCode = process.argv[2];
  if (!supplierCode) throw new Error('Usage: node test_fncsup.js <SUPNAME>');

  const odataUrl = process.env.PRIORITY_URL_REAL || '';
  const { serviceUrl, tabulaini, company } = parseOdataUrl(odataUrl);

  console.log(`Login → ${serviceUrl} (${company})`);
  await withTimeout(
    new Promise((res, rej) => priority.login(
      { username: process.env.PRIORITY_USERNAME, password: process.env.PRIORITY_PASSWORD,
        url: serviceUrl, tabulaini, language: 1, appname: 'supplierinvoice' },
      res, rej
    )),
    20000, 'login'
  );
  console.log('Login OK');

  console.log(`Opening FNCSUP for supplier ${supplierCode}...`);
  let fncsupForm;
  try {
    fncsupForm = await withTimeout(
      new Promise((res, rej) => priority.formStartEx(
        'FNCSUP',
        makeMessageHandler('FNCSUP'),
        null,
        company, 1, { zoomValue: supplierCode }, res, rej
      )),
      30000, 'formStartEx FNCSUP'
    );
    console.log('FNCSUP form opened OK');
  } catch (e) {
    console.log(`FNCSUP formStartEx FAILED: ${e.message}`);
    process.exit(1);
  }

  const rows = await withTimeout(
    new Promise((res, rej) => fncsupForm.getRows(1, res, rej)),
    15000, 'getRows FNCSUP'
  ).catch(e => { console.log(`getRows failed: ${e.message}`); return null; });

  if (!rows) { process.exit(1); }

  const rowData = (rows && rows.FNCSUP)
    ? (rows.FNCSUP['1'] || Object.values(rows.FNCSUP)[0] || {})
    : {};
  console.log(`FNCSUP row data: ${JSON.stringify(rowData)}`);
  console.log(`Current FNCPATNAME: "${rowData.FNCPATNAME || '(empty)'}"`);

  // Try setActiveRow + fieldUpdate
  console.log('Trying setActiveRow(1)...');
  await withTimeout(
    new Promise((res, rej) => fncsupForm.setActiveRow(1, res, rej)),
    10000, 'setActiveRow'
  ).catch(e => console.log(`setActiveRow failed: ${e.message}`));

  console.log('Trying fieldUpdate FNCPATNAME → TEST_VAL...');
  await withTimeout(
    new Promise((res, rej) => fncsupForm.fieldUpdate('FNCPATNAME', rowData.FNCPATNAME || '', res, rej)),
    10000, 'fieldUpdate (no-op)'
  ).catch(e => console.log(`fieldUpdate failed: ${e.message}`));

  await fncsupForm.endCurrentForm(false).catch(() => {});
  console.log('Done');
}

main().catch(err => {
  console.log(`ERROR: ${err.message}`);
  process.exit(1);
});
