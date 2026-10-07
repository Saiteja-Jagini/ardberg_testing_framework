const fs = require('fs');
const { chromium } = require('/tmp/ardberg-probe/node_modules/@playwright/test');

(async () => {
  const input = JSON.parse(fs.readFileSync(process.argv[2], 'utf8'));
  const browser = await chromium.launch({ headless: true });
  const page = await browser.newPage({ viewport: { width: 1280, height: 800 } });
  const errors = [];
  const consoleMessages = [];
  const responses = [];
  const requestsFailed = [];
  page.on('pageerror', error => errors.push(String(error)));
  page.on('console', message => { if (consoleMessages.length < 50) consoleMessages.push({type: message.type(), text: message.text().slice(0, 1000)}); });
  page.on('response', response => { if (responses.length < 100) responses.push({url: response.url(), status: response.status()}); });
  page.on('requestfailed', request => { if (requestsFailed.length < 50) requestsFailed.push({url: request.url(), error: request.failure()?.errorText}); });
  let response;
  let failure;
  const actions = [];
  try {
  response = await page.goto(input.url, { waitUntil: 'domcontentloaded', timeout: 30000 });
  for (const step of input.actions) {
    const target = page.locator(step.selector).first();
    if (step.action === 'click') await target.click({ timeout: 10000 });
    else if (step.action === 'fill') await target.fill(step.value, { timeout: 10000 });
    else if (step.action === 'select') await target.selectOption(step.value, { timeout: 10000 });
    else if (step.action === 'check') await target.check({ timeout: 10000 });
    else if (step.action === 'wait_for') await target.waitFor({ state: 'visible', timeout: 10000 });
    actions.push({action: step.action, selector: step.selector, completed: true});
  }
  } catch (error) { failure = String(error.stack || error); }
  try { await page.screenshot({ path: input.screenshot, fullPage: true, timeout: 10000 }); }
  catch (error) { errors.push('Screenshot: ' + String(error)); }
  console.log(JSON.stringify({ status: response ? response.status() : null,
    url: page.url(), title: await page.title(),
    body_text: (await page.locator('body').innerText()).slice(0, 12000),
    page_errors: errors, console_messages: consoleMessages, network_responses: responses,
    failed_requests: requestsFailed, completed_actions: actions, action_error: failure || null }, null, 2));
  await browser.close();
  if (failure) process.exitCode = 1;
})().catch(error => { console.error(String(error.stack || error)); process.exit(1); });
