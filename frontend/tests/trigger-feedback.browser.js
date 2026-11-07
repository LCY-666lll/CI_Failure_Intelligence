// 从项目根目录使用 Playwright CLI run-code --filename 执行；所有 API 请求均拦截。
async (page) => {
  const item = { event_id: 13, repository_full_name: 'example/ci-service', run_id: 1, run_attempt: 1, workflow_name: 'Tests', job_name: 'Python', head_branch: 'main', commit_sha: 'a'.repeat(40), parse_status: 'parsed', failures: [{ id: 1, testcase_index: 0, issue_index: 0, test_name: 'test_health', issue_type: 'failure', details: 'AssertionError' }] }
  const diagnosis = { summary: '测试诊断', root_causes: [], recommended_actions: [], needs_more_information: false, missing_information: [] }
  const cases = [
    ['cached', '已复用已有诊断'],
    ['generated', '新诊断已完成'],
    ['error', '触发未成功'],
    ['in_progress', '已有诊断任务正在处理中'],
  ]
  for (const [status, expected] of cases) {
    await page.evaluate(() => sessionStorage.clear())
    await page.unrouteAll()
    let posts = 0
    let submitted = false
    let release
    const pending = new Promise(resolve => { release = resolve })
    await page.route('**/api/**', async route => {
      if (route.request().method() === 'POST') {
        posts++
        await pending
        submitted = true
        return route.fulfill({ status: status === 'error' ? 401 : status === 'in_progress' ? 202 : 200, json: { status } })
      }
      const path = new URL(route.request().url()).pathname
      let body = { items: [] }
      if (path.endsWith('/failures')) body = item
      if (path.endsWith('/diagnosis')) body = submitted && status === 'in_progress'
        ? { status: 'processing', attempt_count: 1, lease_expires_at: new Date(Date.now() + 60000).toISOString() }
        : { status: 'completed', attempt_count: 1, diagnosis }
      await route.fulfill({ json: body })
    })
    await page.goto('http://127.0.0.1:5174/runs/13')
    await page.getByLabel('读取令牌').fill('mock-read-only')
    await page.getByRole('button', { name: '进入工作台', exact: true }).click()
    await page.getByRole('button', { name: '使用授权令牌触发诊断' }).click()
    await page.getByLabel('付费诊断触发令牌').fill('mock-trigger-only')
    await page.getByRole('button', { name: '确认触发', exact: true }).click()
    await page.getByRole('status').filter({ hasText: '正在提交诊断请求' }).waitFor()
    if (!await page.getByRole('button', { name: '正在提交…', exact: true }).isDisabled()) throw new Error('提交时按钮未禁用')
    release()
    await page.getByRole('status').filter({ hasText: expected }).waitFor()
    if (posts !== 1) throw new Error('发生重复POST')
    if (status === 'cached') {
      await page.setViewportSize({ width: 390, height: 844 })
      await page.screenshot({ path: 'reports/frontend-cached-feedback.png', fullPage: true })
    }
  }
  await page.unrouteAll()
  await page.goto('http://127.0.0.1:5174/runs')
}
