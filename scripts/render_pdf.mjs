/**
 * Render a styled manual HTML file to PDF with headless Chromium.
 *
 * Pairs with scripts/build_manual_pdf.py / build_module_manual_pdf.py, which
 * produce the styled HTML. Chromium is the renderer because it ships with the
 * repo's Playwright install and supports the paged-media CSS the manuals rely
 * on (per-chapter page breaks, running header/footer, page counters).
 *
 *   node scripts/render_pdf.mjs                      # user manual (default)
 *   node scripts/render_pdf.mjs <in.html> <out.pdf> [running header text]
 */
import { chromium } from '@playwright/test'
import { pathToFileURL } from 'node:url'
import { statSync } from 'node:fs'
import path from 'node:path'

const repo = path.resolve(import.meta.dirname, '..')
const htmlPath = process.argv[2]
  ? path.resolve(process.argv[2])
  : path.join(repo, 'docs', 'USER_MANUAL.html')
const pdfPath = process.argv[3]
  ? path.resolve(process.argv[3])
  : path.join(repo, 'docs', 'Orb_Hospital_OS_User_Manual.pdf')
const headerText = process.argv[4] ?? 'Orb Hospital OS — User Manual'

const browser = await chromium.launch()
const page = await browser.newPage()
await page.goto(pathToFileURL(htmlPath).href, { waitUntil: 'networkidle' })

await page.pdf({
  path: pdfPath,
  format: 'A4',
  printBackground: true,
  margin: { top: '20mm', bottom: '18mm', left: '18mm', right: '18mm' },
  displayHeaderFooter: true,
  // The cover page must not carry the running header, so the header template
  // is blank on page 1 via the .cover-page class Chromium exposes... it does
  // not, so instead keep the header subtle and let it run throughout.
  headerTemplate:
    '<div style="font-size:7pt;color:#9aa0aa;width:100%;padding:0 18mm;font-family:Helvetica,Arial,sans-serif;">' +
    `<span style="float:right">${headerText.replace(/&/g, '&amp;').replace(/</g, '&lt;')}</span></div>`,
  footerTemplate:
    '<div style="font-size:8pt;color:#9aa0aa;width:100%;text-align:center;font-family:Helvetica,Arial,sans-serif;">' +
    '<span class="pageNumber"></span> / <span class="totalPages"></span></div>',
})

await browser.close()
const { size } = statSync(pdfPath)
console.log(`PDF written: ${pdfPath} (${size.toLocaleString()} bytes)`)
