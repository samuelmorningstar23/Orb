// Generate PWA PNG icons (192/512 + maskable) from the brand mark using the
// pre-installed Chromium. Run: node scripts/gen-pwa-icons.cjs
const { chromium } = require('playwright');
const fs = require('fs');
const path = require('path');

const PUBLIC = path.join(__dirname, '..', 'public');
const MARK = fs.readFileSync(path.join(PUBLIC, 'favicon.svg'), 'utf8');
const BG = '#0a0a0a'; // matches the PWA theme_color / dark shell

// scale = fraction of the square the mark occupies. Maskable icons keep the
// logo inside a ~80% safe zone so platform masks (circle/squircle) never clip it.
function page(size, scale) {
  return `<!doctype html><html><body style="margin:0">
    <div style="width:${size}px;height:${size}px;background:${BG};display:flex;align-items:center;justify-content:center">
      <div style="width:${Math.round(size * scale)}px;height:${Math.round(size * scale)}px;display:flex;align-items:center;justify-content:center">
        ${MARK.replace('<svg', '<svg style="width:100%;height:100%"')}
      </div>
    </div></body></html>`;
}

(async () => {
  const browser = await chromium.launch(
    process.env.ORB_CHROMIUM_PATH ? { executablePath: process.env.ORB_CHROMIUM_PATH } : {}
  );
  const targets = [
    { file: 'pwa-192.png', size: 192, scale: 0.62 },
    { file: 'pwa-512.png', size: 512, scale: 0.62 },
    { file: 'pwa-maskable-512.png', size: 512, scale: 0.5 }, // extra padding for mask safe zone
    { file: 'apple-touch-icon.png', size: 180, scale: 0.6 },
  ];
  for (const t of targets) {
    const ctx = await browser.newContext({ viewport: { width: t.size, height: t.size }, deviceScaleFactor: 1 });
    const p = await ctx.newPage();
    await p.setContent(page(t.size, t.scale), { waitUntil: 'networkidle' });
    await p.screenshot({ path: path.join(PUBLIC, t.file), omitBackground: false });
    await ctx.close();
    console.log('wrote', t.file, `${t.size}x${t.size}`);
  }
  await browser.close();
})();
