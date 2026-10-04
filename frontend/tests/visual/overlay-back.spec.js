import { expect, test } from '@playwright/test'

// Overlays on a phone: Back closes the open overlay (top one first) and stays on the page,
// the backdrop above the card sheet closes it, and the close button stays in reach.

async function openGallery(page) {
  const cardBack = await (await page.request.get('/cardback.jpg')).body()
  await page.route(/\/api\/images\/card\/(?:me04|me03|base1)-[^/]+\/(?:small|large)$/, route => route.fulfill({
    status: 200, contentType: 'image/jpeg', body: cardBack,
  }))
  await page.goto('/__card-system')
  await expect(page.getByTestId('card-system-gallery')).toBeVisible()
}

const cardDialog = page => page.locator('[role="dialog"][aria-label="Cinccino ex"]')

test('Back closes the card dialog and stays on the page', async ({ page }) => {
  await openGallery(page)
  const url = page.url()
  await page.getByTestId('open-card-dialog').click()
  await expect(cardDialog(page)).toBeVisible()
  await page.goBack()
  await expect(cardDialog(page)).toHaveCount(0)
  expect(page.url()).toBe(url)
  await expect(page.getByTestId('card-system-gallery')).toBeVisible()
  // opened and closed with ✕: no dead history entry, so Back after it leaves the page as usual
  await page.getByTestId('open-card-dialog').click()
  await page.getByTestId('card-dialog-close').click()
  await expect(cardDialog(page)).toHaveCount(0)
  await expect.poll(() => page.evaluate(() => window.history.state?.__ov ?? 0)).toBe(0)
})

test('Back closes the zoom first, then the card', async ({ page }) => {
  await openGallery(page)
  await page.getByTestId('open-card-dialog').click()
  await cardDialog(page).getByRole('button', { name: /Zoom image/ }).click()
  const zoom = page.getByRole('dialog', { name: 'Zoom image — Cinccino ex' })
  await expect(zoom).toBeVisible()
  await page.goBack()
  await expect(zoom).toHaveCount(0)
  await expect(cardDialog(page)).toBeVisible()
  await page.goBack()
  await expect(cardDialog(page)).toHaveCount(0)
})

test('phone: a tap above the card sheet closes it; ✕ stays visible while scrolling', async ({ page, isMobile }) => {
  test.skip(!isMobile, 'phone layout')
  await openGallery(page)
  await page.getByTestId('open-card-dialog').click()
  const dialog = cardDialog(page)
  await expect(dialog).toBeVisible()
  const box = await dialog.boundingBox()
  expect(box.y).toBeGreaterThanOrEqual(48)          // a strip of backdrop to tap
  await dialog.evaluate(node => { node.scrollTop = node.scrollHeight })
  const close = page.getByTestId('card-dialog-close')
  await expect(close).toBeInViewport()
  const closeBox = await close.boundingBox()
  expect(closeBox.width).toBeGreaterThanOrEqual(44)
  expect(closeBox.y).toBeLessThan(box.y + 60)       // still at the top of the sheet
  await page.mouse.click(box.x + box.width / 2, box.y / 2)
  await expect(dialog).toHaveCount(0)
})

test('Escape closes the card dialog', async ({ page }) => {
  await openGallery(page)
  await page.getByTestId('open-card-dialog').click()
  await expect(cardDialog(page)).toBeVisible()
  await page.keyboard.press('Escape')
  await expect(cardDialog(page)).toHaveCount(0)
  await expect.poll(() => page.evaluate(() => window.history.state?.__ov ?? 0)).toBe(0)
})
