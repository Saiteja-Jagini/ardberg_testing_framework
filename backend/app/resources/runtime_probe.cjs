const fs = require('fs');
const { chromium } = require('/tmp/ardberg-probe/node_modules/@playwright/test');

(async () => {
  const input = JSON.parse(fs.readFileSync(process.argv[2], 'utf8'));
  const browser = await chromium.launch({ headless: true });
  const page = await browser.newPage({ viewport: { width: 1280, height: 800 } });
  const errors = [];
  page.on('pageerror', error => errors.push(String(error)));
  const response = await page.goto(input.url, { waitUntil: 'domcontentloaded', timeout: 30000 });
  for (const step of input.actions) {
    const target = page.locator(step.selector).first();
    if (step.action === 'click') await target.click({ timeout: 10000 });
    else if (step.action === 'fill') await target.fill(step.value, { timeout: 10000 });
    else if (step.action === 'select') await target.selectOption(step.value, { timeout: 10000 });
    else if (step.action === 'check') await target.check({ timeout: 10000 });
  }
  await page.screenshot({ path: input.screenshot, fullPage: true });
  console.log(JSON.stringify({ status: response ? response.status() : null,
    url: page.url(), title: await page.title(),
    body_text: (await page.locator('body').innerText()).slice(0, 12000),
    page_errors: errors }, null, 2));
  await browser.close();
})().catch(error => { console.error(String(error.stack || error)); process.exit(1); });
