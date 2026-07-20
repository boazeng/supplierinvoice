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

  // נסה לפתוח subforms של FNCSUP
  const subformsToTry = ['FNCPATSUP_SUBFORM','FNCSUPPAT_SUBFORM','FNCSUP_SUBFORM','FNCPAT_SUBFORM','SUPPLIERSFNC_SUBFORM'];
  for (const sfName of subformsToTry) {
    console.log(`Trying subform: ${sfName}...`);
    try {
      const sf = await withTimeout(
        new Promise((res, rej) => fncsupForm.startSubForm(sfName, makeMessageHandler(sfName), null, res, rej)),
        10000, `startSubForm ${sfName}`
      );
      const sfRows = await withTimeout(new Promise((res, rej) => sf.getRows(1, res, rej)), 10000, 'getRows sf');
      console.log(`${sfName} rows: ${JSON.stringify(sfRows)}`);
      await sf.endCurrentForm(false).catch(() => {});
    } catch(e) {
      console.log(`${sfName} failed: ${e.message}`);
    }
  }

  await fncsupForm.endCurrentForm(false).catch(() => {});

  // נסה גם SUPPLIERS form + subforms שלה
  console.log('\n--- Trying SUPPLIERS form + subforms ---');
  try {
    const supForm = await withTimeout(
      new Promise((res, rej) => priority.formStartEx(
        'SUPPLIERS', makeMessageHandler('SUPPLIERS'), null,
        company, 1, { zoomValue: supplierCode }, res, rej
      )),
      30000, 'formStartEx SUPPLIERS'
    );
    console.log('SUPPLIERS form opened');
    const supRows = await withTimeout(new Promise((res, rej) => supForm.getRows(1, res, rej)), 15000, 'getRows SUPPLIERS');
    const supRow = (supRows && supRows.SUPPLIERS) ? (supRows.SUPPLIERS['1'] || Object.values(supRows.SUPPLIERS)[0] || {}) : {};
    console.log(`SUPPLIERS keys with FNC: ${Object.keys(supRow).filter(k => k.toUpperCase().includes('FNC')).join(', ') || 'none'}`);

    // נסה subforms של SUPPLIERS שעשויות להכיל FNCPATNAME
    const suppSubforms = ['FNCSUP_SUBFORM','SUPPLIERSA_SUBFORM','SUPPLIERSFNC_SUBFORM','FNCSUPPAT','SUPFNC_SUBFORM'];
    for (const sfName of suppSubforms) {
      console.log(`  Trying SUPPLIERS subform: ${sfName}...`);
      try {
        const sf = await withTimeout(
          new Promise((res, rej) => supForm.startSubForm(sfName, makeMessageHandler(sfName), null, res, rej)),
          10000, `startSubForm ${sfName}`
        );
        const sfRows = await withTimeout(new Promise((res, rej) => sf.getRows(1, res, rej)), 10000, 'getRows sf');
        const sfData = sfRows ? JSON.stringify(sfRows).substring(0, 500) : 'empty';
        console.log(`  ${sfName} data: ${sfData}`);
        await sf.endCurrentForm(false).catch(() => {});
      } catch(e) {
        console.log(`  ${sfName} failed: ${e.message}`);
      }
    }
    await supForm.endCurrentForm(false).catch(() => {});
  } catch(e) {
    console.log(`SUPPLIERS form failed: ${e.message}`);
  }

  console.log('Done');
}

main().catch(err => {
  console.log(`ERROR: ${err.message}`);
  process.exit(1);
});
