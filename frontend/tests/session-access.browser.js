// 使用 Playwright CLI run-code --filename 执行，API全部使用模拟响应。
async (page) => {
  let unauthorized = false
  await page.route('**/api/**', route => route.fulfill({
    status: unauthorized ? 401 : 200,
    json: unauthorized ? { detail: 'Unauthorized' } : { items: [] },
  }))
  await page.goto('http://127.0.0.1:5174/runs')
  await page.getByLabel('读取令牌').fill('test-session-read-token')
  await page.getByRole('button', { name: '进入工作台', exact: true }).click()
  await page.getByRole('heading', { name: '最近的 CI 事件' }).waitFor()
  await page.reload()
  await page.getByRole('heading', { name: '最近的 CI 事件' }).waitFor()
  if (await page.getByLabel('读取令牌').count()) throw new Error('刷新未恢复访问')
  if (await page.evaluate(() => localStorage.length)) throw new Error('使用了长期存储')
  await page.getByRole('button', { name: '断开', exact: true }).click()
  await page.getByLabel('读取令牌').waitFor()
  if (await page.evaluate(() => sessionStorage.getItem('cfi.readToken'))) throw new Error('断开未清除保存的令牌')
  await page.reload()
  await page.getByLabel('读取令牌').waitFor()
  await page.getByLabel('读取令牌').fill('test-session-read-token')
  await page.getByRole('button', { name: '进入工作台', exact: true }).click()
  await page.getByRole('heading', { name: '最近的 CI 事件' }).waitFor()
  unauthorized = true
  await page.reload()
  await page.getByLabel('读取令牌').waitFor()
  if (await page.evaluate(() => sessionStorage.getItem('cfi.readToken'))) throw new Error('401未清除保存的令牌')
}
