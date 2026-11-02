import { test, expect, type Page, type TestInfo } from '@playwright/test'
import { readFile } from 'node:fs/promises'
import { login, openTab } from './helpers'

/** Real routes, cookies, migrations and persistence; only owned demo data. */
async function evidence(page: Page, info: TestInfo, name: string) {
  const path = info.outputPath(`${name}.png`)
  await page.screenshot({ path, fullPage: true })
  await info.attach(name, { path, contentType: 'image/png' })
}
async function choosePatient(page: Page, panelId: string) {
  const response = await page.request.get('/api/vigil/patients')
  expect(response.ok()).toBeTruthy()
  const [patient] = await response.json()
  expect(patient?.id).toBeTruthy()
  const panel = page.locator(`#panel-${panelId}`)
  await panel.getByLabel('Search name, hospital number or record ID', { exact: true }).fill(patient.name)
  await panel.getByRole('button', { name: new RegExp(`^${String(patient.name).replace(/[.*+?^${}()|[\]\\]/g, '\\$&')} ·`) }).click()
  await expect(panel.getByText(/Selected record:/)).toContainText(patient.name)
  return patient
}

/** RFC-style quoted CSV, including escaped quotes and embedded newlines. */
function csvRows(text: string): string[][] {
  const source = text.replace(/^\uFEFF/, '')
  const rows: string[][] = []
  let row: string[] = [], field = '', quoted = false, afterQuote = false
  for (let index = 0; index < source.length; index++) {
    const char = source[index]
    if (quoted) {
      if (char === '"' && source[index + 1] === '"') { field += '"'; index++ }
      else if (char === '"') { quoted = false; afterQuote = true }
      else field += char
      continue
    }
    if (afterQuote && ![',', '\r', '\n'].includes(char)) throw new Error('Malformed quoted CSV field')
    if (char === '"') {
      if (field) throw new Error('Unexpected quote in CSV field')
      quoted = true
    } else if (char === ',') {
      row.push(field); field = ''; afterQuote = false
    } else if (char === '\r' || char === '\n') {
      rows.push([...row, field]); row = []; field = ''; afterQuote = false
      if (char === '\r' && source[index + 1] === '\n') index++
    } else field += char
  }
  if (quoted) throw new Error('Unclosed quoted CSV field')
  if (row.length || field || afterQuote) rows.push([...row, field])
  return rows
}

test.beforeEach(async ({ context }) => {
  // A regression attempting an external request is a test failure, never an
  // accidental connection while exercising an offline hospital workflow.
  await context.route('**/*', async route => {
    const url = new URL(route.request().url())
    if (!['127.0.0.1', 'localhost', '[::1]'].includes(url.hostname)) {
      throw new Error(`Offline browser attempted an external request: ${url.origin}`)
    }
    await route.continue()
  })
})

test('all thirteen local module screens render against real authenticated APIs', async ({ page }, info) => {
  const errors: string[] = []
  page.on('pageerror', error => errors.push(error.message))
  await login(page, 'admin')
  const modules = ['feedback', 'mortuary', 'cssd', 'reporting', 'finance-tools', 'radiology', 'care-plans',
    'emergency', 'perioperative', 'dialysis', 'record-governance', 'radiation-safety', 'infection-surveillance']
  for (const id of modules) {
    await openTab(page, id)
    const panel = page.locator(`#panel-${id}`)
    await expect(panel).toBeVisible()
    await expect(panel.locator('h1').first()).toBeVisible()
    await expect(panel.getByRole('status').filter({ hasText: /^Loading/ })).toHaveCount(0)
    await expect(panel.getByRole('alert')).toHaveCount(0)
  }
  await evidence(page, info, 'real-infection-surveillance')
  expect(errors).toEqual([])
})

test('complaint statement persists and resolution survives reload with immutable history', async ({ page }, info) => {
  await login(page, 'admin')
  await openTab(page, 'feedback')
  const panel = page.locator('#panel-feedback')
  const title = `Synthetic browser complaint: meal delivery ${Date.now()}`
  const statement = 'Demo record only: the breakfast tray arrived late.'
  await panel.getByText('Record feedback or a complaint', { exact: true }).click()
  await panel.getByLabel('Subject', { exact: true }).fill(title)
  await panel.getByLabel('Original statement', { exact: true }).fill(statement)
  const created = page.waitForResponse(response => response.url().endsWith('/api/feedback') && response.request().method() === 'POST')
  await panel.getByRole('button', { name: 'Save local record', exact: true }).click()
  const response = await created
  expect(response.ok(), await response.text()).toBeTruthy()
  const record = await response.json()
  await panel.getByRole('button', { name: `${title} · recorded` }).click()
  await expect(panel.getByRole('heading', { name: title })).toBeVisible()
  await panel.getByLabel('Case action').selectOption('resolve')
  await panel.getByLabel('Response or reason').fill('Demo review: delivery procedure discussed with the kitchen team.')
  await panel.getByRole('button', { name: 'Record action', exact: true }).click()
  await expect(panel.getByRole('button', { name: `${title} · resolved` })).toBeVisible()
  await page.reload()
  await openTab(page, 'feedback')
  await panel.getByRole('button', { name: `${title} · resolved` }).click()
  await expect(panel.getByText(statement, { exact: true })).toBeVisible()
  await expect(panel.getByText('Demo review: delivery procedure discussed with the kitchen team.', { exact: true }).first()).toBeVisible()
  const persisted = await page.request.get(`/api/feedback/${record.id}`)
  expect(persisted.ok()).toBeTruthy()
  const saved = await persisted.json()
  expect(saved.statement).toBe(statement)
  expect(saved.status).toBe('resolved')
  expect(saved.events.length).toBeGreaterThanOrEqual(2)
  await evidence(page, info, 'real-feedback-persisted')
})

test('care plan intervention records a completed outcome and reopens from storage', async ({ page }, info) => {
  await login(page, 'doctor')
  await openTab(page, 'care-plans')
  const panel = page.locator('#panel-care-plans')
  const title = `Synthetic browser care plan ${Date.now()}`
  const patient = await choosePatient(page, 'care-plans')
  await panel.getByRole('button', { name: 'New record', exact: true }).click()
  await panel.getByLabel('Record title', { exact: true }).fill(title)
  await panel.getByLabel('Problem / focus', { exact: true }).fill('Demo discharge education')
  await panel.getByLabel('Care goal', { exact: true }).fill('Patient can describe the documented follow-up plan.')
  await panel.getByRole('button', { name: 'Add intervention' }).click()
  await panel.getByLabel('Intervention', { exact: true }).fill('Review the authored follow-up instructions.')
  await panel.getByLabel('Responsible staff', { exact: true }).fill('Demo nurse')
  const created = page.waitForResponse(response => response.url().endsWith('/api/care-plans/records') && response.request().method() === 'POST')
  await panel.getByRole('button', { name: 'Create record', exact: true }).click()
  const response = await created
  expect(response.status(), await response.text()).toBe(201)
  const original = await response.json()
  await expect(panel.getByText('Record saved.', { exact: true })).toBeVisible()
  await panel.getByRole('combobox', { name: 'Intervention outcome', exact: true }).selectOption('done')
  await panel.getByLabel('Outcome time', { exact: true }).fill('2026-10-11T10:30')
  await panel.getByLabel('Intervention evaluation', { exact: true }).fill('Demo teach-back was documented.')
  await panel.getByLabel('Change / cancellation reason').fill('Recorded the completed teaching session.')
  const updated = page.waitForResponse(r => r.url().endsWith(`/api/care-plans/records/${original.id}`) && r.request().method() === 'PUT')
  await panel.getByRole('button', { name: 'Save revision', exact: true }).click()
  expect((await updated).ok()).toBeTruthy()
  await page.reload()
  await openTab(page, 'care-plans')
  await panel.getByRole('button', { name: title, exact: true }).click()
  await expect(panel.getByRole('combobox', { name: 'Intervention outcome', exact: true })).toHaveValue('done')
  await expect(panel.getByRole('textbox', { name: 'Intervention evaluation', exact: true })).toHaveValue('Demo teach-back was documented.')
  const saved = await (await page.request.get(`/api/care-plans/records/${original.id}`)).json()
  expect(saved.patient_id).toBe(patient.id)
  expect(saved.history.length).toBeGreaterThanOrEqual(2)
  // A stale revision must not overwrite the successful edit.
  const csrf = (await page.context().cookies()).find(cookie => cookie.name === 'orb_csrf')!.value
  const stale = await page.request.put(`/api/care-plans/records/${original.id}`, {
    headers: { 'X-CSRF-Token': csrf }, data: { version: original.version, title: original.title, state: original.state, data: original.data },
  })
  expect(stale.status()).toBe(409)
  await panel.getByRole('combobox', { name: 'Intervention outcome', exact: true }).scrollIntoViewIfNeeded()
  await evidence(page, info, 'real-care-plan-persisted')
})

test('radiology report signs, locks editing and appends an audited addendum', async ({ page }, info) => {
  await login(page, 'doctor')
  await openTab(page, 'radiology')
  const panel = page.locator('#panel-radiology')
  await choosePatient(page, 'radiology')
  await panel.getByRole('button', { name: 'New record', exact: true }).click()
  await panel.getByLabel('Record title', { exact: true }).fill('Synthetic browser radiology report')
  await panel.getByRole('combobox', { name: 'Modality', exact: true }).selectOption('X-ray')
  await panel.getByLabel('Body site', { exact: true }).fill('Chest (synthetic example)')
  await panel.getByRole('button', { name: 'Create record', exact: true }).click()
  await expect(panel.getByText('Record saved.', { exact: true })).toBeVisible()
  for (const state of ['performed', 'reported']) {
    await panel.getByRole('combobox', { name: 'Workflow stage', exact: true }).selectOption(state)
    if (state === 'performed') {
      await panel.getByLabel('Scheduled time', { exact: true }).fill('2026-10-11T09:00')
      await panel.getByLabel('Arrival time', { exact: true }).fill('2026-10-11T09:30')
      await panel.getByLabel('Performed time', { exact: true }).fill('2026-10-11T10:00')
    }
    else {
      await panel.getByLabel('Report findings', { exact: true }).fill('Synthetic report for software verification only.')
      await panel.getByLabel('Report impression', { exact: true }).fill('Demo impression; no clinical interpretation.')
    }
    const saved = page.waitForResponse(r => r.url().includes('/api/radiology/records/') && r.request().method() === 'PUT')
    await panel.getByRole('button', { name: 'Save revision', exact: true }).click()
    const response = await saved
    expect(response.ok(), await response.text()).toBeTruthy()
  }
  await expect(panel.getByRole('button', { name: 'Sign final record', exact: true })).toBeEnabled()
  await panel.getByRole('button', { name: 'Sign final record', exact: true }).click()
  await expect(panel.getByRole('heading', { name: 'Addendum to final record' })).toBeVisible()
  await expect(panel.getByRole('textbox', { name: 'Report findings', exact: true })).toBeDisabled()
  await panel.getByLabel('Addendum', { exact: true }).fill('Synthetic addendum retained separately from the signed report.')
  await panel.getByLabel('Reason', { exact: true }).fill('Browser acceptance evidence.')
  await panel.getByRole('button', { name: 'Append addendum', exact: true }).click()
  await expect(panel.getByText('Synthetic addendum retained separately from the signed report.', { exact: true })).toBeVisible()
  await evidence(page, info, 'real-radiology-signed')
})

test('doctor cannot open administrative feedback, governance or finance tools', async ({ page }) => {
  await login(page, 'doctor')
  for (const id of ['feedback', 'record-governance', 'finance-tools', 'infection-surveillance']) {
    await expect(page.locator(`#nav-${id}`)).toHaveCount(0)
  }
  for (const path of ['/api/feedback', '/api/record-governance/hold', '/api/finance-tools/meta']) {
    const response = await page.request.get(path)
    expect(response.status(), `${path} should refuse this role`).toBe(403)
  }
})

test('saved report definitions reopen and export the previewed synthetic cohort', async ({ page }, info) => {
  await login(page, 'admin')
  await openTab(page, 'reporting')
  const panel = page.locator('#panel-reporting')
  await expect(panel.getByRole('button', { name: 'Preview report', exact: true })).toBeVisible()
  const name = `Browser acceptance ward cohort ${Date.now()}`
  await panel.getByLabel('Definition name', { exact: true }).fill(name)
  await panel.getByRole('button', { name: 'Save definition', exact: true }).click()
  await expect(panel.getByText('Report definition saved. It will rerun with your current access.', { exact: true })).toBeVisible()
  await page.reload()
  await openTab(page, 'reporting')
  const saved = panel.getByRole('combobox', { name: 'Your saved definitions', exact: true })
  await saved.selectOption({ label: name })
  const previewing = page.waitForResponse(response => new URL(response.url()).pathname === '/api/reporting/query'
    && response.request().method() === 'POST')
  await panel.getByRole('button', { name: 'Preview report', exact: true }).click()
  const previewResponse = await previewing
  expect(previewResponse.ok(), await previewResponse.text()).toBeTruthy()
  const preview = await previewResponse.json() as { columns: string[]; rows: Record<string, unknown>[]; truncated: boolean }
  expect(preview.truncated).toBe(false)
  const patientColumn = preview.columns.indexOf('patient_id')
  expect(patientColumn, 'Preview must expose stable patient IDs').toBeGreaterThanOrEqual(0)
  const expectedIds = preview.rows.map(row => String(row.patient_id)).sort()
  expect(expectedIds.length).toBeGreaterThan(0)
  const results = panel.getByRole('region', { name: 'Report results', exact: true })
  await expect(results.locator('tbody tr')).toHaveCount(preview.rows.length)
  expect(await results.locator('thead th').allTextContents()).toEqual(preview.columns.map(column => column.replaceAll('_', ' ')))
  const renderedIds = await results.locator('tbody tr').evaluateAll((rows, index) =>
    rows.map(row => row.querySelectorAll('td')[index]?.textContent ?? ''), patientColumn)
  expect(renderedIds.sort()).toEqual(expectedIds)
  const downloading = page.waitForEvent('download')
  await panel.getByRole('button', { name: 'Export CSV', exact: true }).click()
  const download = await downloading
  expect(download.suggestedFilename()).toMatch(/\.csv$/)
  const path = info.outputPath('synthetic-cohort.csv')
  await download.saveAs(path)
  const csv = await readFile(path, 'utf8')
  const [header, ...exported] = csvRows(csv)
  expect(header).toEqual(preview.columns)
  expect(exported).toHaveLength(preview.rows.length)
  expect(exported.every(row => row.length === header.length)).toBe(true)
  expect(exported.map(row => row[patientColumn]).sort()).toEqual(expectedIds)
  await results.locator('thead').scrollIntoViewIfNeeded()
  await evidence(page, info, 'real-reporting-preview')
})
