import { test, expect } from '@playwright/test'
import { login, openTab } from './helpers'

for (const width of [390, 1280, 1440]) for (const theme of ['light', 'dark']) {
  test(`admission, focus, security and radiation layout ${width} ${theme}`, async ({ page }, info) => {
    await page.setViewportSize({ width, height: width === 390 ? 844 : 1000 })
    await page.addInitScript(value => localStorage.setItem('orb-theme', value), theme)
    const errors: string[] = []
    page.on('pageerror', error => errors.push(error.message))
    await login(page, 'doctor')
    const trigger = page.locator('#btn-admit-patient')
    await trigger.click()
    const modal = page.getByRole('dialog')
    await expect(modal).toBeVisible()
    const name = modal.locator('[data-autofocus]')
    await expect(name).toBeFocused()
    await name.fill('Synthetic Responsive Patient')
    for (const label of ['Identity', 'Admission', 'Clinical', 'History']) {
      const tab = modal.getByRole('button', { name: new RegExp(`^${label}`) })
      await tab.click()
      const body = modal.locator('.admit-modal__body')
      const overflow = await body.evaluate(e => e.scrollWidth - e.clientWidth)
      expect(overflow).toBeLessThanOrEqual(2)
      for (const control of await modal.locator('.admit-modal__scroll input:not([type=hidden]), .admit-modal__scroll select').all()) {
        if (!(await control.isVisible())) continue
        await control.scrollIntoViewIfNeeded()
        const box = await control.boundingBox()
        expect(box).not.toBeNull()
        expect(box!.width).toBeGreaterThan(100)
        expect(box!.x).toBeGreaterThanOrEqual(0)
        expect(box!.x + box!.width).toBeLessThanOrEqual(width)
        await control.click({ trial: true })
      }
    }
    await page.screenshot({ path: info.outputPath('admission.png'), fullPage: true })
    await page.keyboard.press('Escape')
    await expect(modal).toHaveCount(0)
    await expect(trigger).toBeFocused()
    const security = page.locator('#btn-mfa-enroll')
    await expect(security).toBeVisible()
    await security.click()
    await expect(page.getByRole('dialog')).toBeVisible()
    await page.screenshot({ path: info.outputPath('account-security.png'), fullPage: true })
    await page.keyboard.press('Escape')
    await expect(security).toBeFocused()
    await page.locator('#btn-logout').click()
    await login(page, 'admin')
    await openTab(page, 'radiation-safety')
    const panel = page.locator('#panel-radiation-safety')
    await expect(panel.getByRole('heading', { name: 'Radiation safety documents' })).toBeVisible()
    await expect(panel.getByRole('status').filter({ hasText: /Loading/ })).toHaveCount(0)
    const geometry = await panel.locator('.record-governance').evaluate(e => ({ width: e.clientWidth, scroll: e.scrollWidth }))
    expect(geometry.scroll - geometry.width).toBeLessThanOrEqual(2)
    for (const control of await panel.locator('input, select, textarea, button').all()) {
      if (!(await control.isVisible())) continue
      await control.scrollIntoViewIfNeeded()
      const box = await control.boundingBox()
      expect(box!.x).toBeGreaterThanOrEqual(0)
      expect(box!.x + box!.width).toBeLessThanOrEqual(width)
      const unobscured = await control.evaluate(e => {
        const r = e.getBoundingClientRect()
        const top = document.elementFromPoint(r.x + r.width / 2, r.y + r.height / 2)
        return top === e || (top !== null && e.contains(top))
      })
      expect(unobscured).toBe(true)
    }
    await page.screenshot({ path: info.outputPath('radiation-safety.png'), fullPage: true })
    expect(errors).toEqual([])
  })
}
