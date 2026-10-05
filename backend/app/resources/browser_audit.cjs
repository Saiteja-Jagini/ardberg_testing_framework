const fs = require('node:fs');
const path = require('node:path');
const { chromium } = require('@playwright/test');

async function main() {
  const configuration = JSON.parse(fs.readFileSync('/workspace/.ardberg-review/targets.json', 'utf8'));
  const origin = new URL(configuration.baseUrl);
  const output = '/workspace/.ardberg-results';
  const imageDirectory = path.join(output, 'visual');
  fs.mkdirSync(imageDirectory, { recursive: true });
  const browser = await chromium.launch({ headless: true });
  const cases = [];
  const observations = [];
  try {
    for (const [routeIndex, route] of configuration.paths.entries()) {
      const target = new URL(route, origin);
      if (target.origin !== origin.origin) throw new Error('Review route leaves the local application');
      for (const width of configuration.widths) {
        const context = await browser.newContext({ viewport: { width, height: 900 } });
        const page = await context.newPage();
        const consoleErrors = [];
        page.on('pageerror', error => consoleErrors.push(String(error).slice(0, 500)));
        let status = 'passed';
        let failure = '';
        let measurement = null;
        let responseStatus = null;
        try {
          const response = await page.goto(target.href, { waitUntil: 'domcontentloaded', timeout: 30000 });
          responseStatus = response ? response.status() : null;
          await page.waitForTimeout(350);
          measurement = await page.evaluate(() => ({
            documentWidth: document.documentElement.scrollWidth,
            viewportWidth: window.innerWidth,
            bodyWidth: document.body ? document.body.scrollWidth : 0,
          }));
          const screenshot = `visual/route-${routeIndex + 1}-${width}.png`;
          await page.screenshot({ path: path.join(output, screenshot), fullPage: true });
          if (responseStatus !== null && responseStatus >= 400) {
            status = 'failed';
            failure = `HTTP ${responseStatus}`;
          } else if (measurement.documentWidth > width + 1 || measurement.bodyWidth > width + 1) {
            status = 'failed';
            failure = `Horizontal overflow: document ${measurement.documentWidth}px, body ${measurement.bodyWidth}px, viewport ${width}px`;
          }
          observations.push({ route, width, status, responseStatus, finalUrl: page.url(),
            title: await page.title(), measurement, consoleErrors, screenshot });
        } catch (error) {
          status = 'failed';
          failure = String(error).slice(0, 1000);
          observations.push({ route, width, status, responseStatus, finalUrl: page.url(),
            measurement, consoleErrors, error: failure });
        } finally {
          cases.push({ fullName: `${route} at ${width}px`, title: `${route} at ${width}px`,
            status, failureMessages: failure ? [failure] : [] });
          await context.close();
        }
      }
    }
  } finally {
    await browser.close();
  }
  fs.writeFileSync(path.join(output, 'visual-details.json'), JSON.stringify(observations, null, 2));
  fs.writeFileSync(path.join(output, 'visual-review.json'), JSON.stringify({
    numTotalTests: cases.length,
    numPassedTests: cases.filter(item => item.status === 'passed').length,
    numFailedTests: cases.filter(item => item.status === 'failed').length,
    numPendingTests: 0,
    testResults: [{ assertionResults: cases }],
  }, null, 2));
  console.log(`Browser viewport audit: ${cases.filter(item => item.status === 'passed').length}/${cases.length} checks passed`);
  process.exitCode = cases.some(item => item.status === 'failed') ? 1 : 0;
}

main().catch(error => { console.error(error); process.exitCode = 1; });
