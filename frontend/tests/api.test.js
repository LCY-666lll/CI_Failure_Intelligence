import { afterEach, test } from 'node:test'
import assert from 'node:assert/strict'
import { getDiagnosis, triggerDiagnosis } from '../src/api.js'
import { readToken, setReadToken, clearReadToken } from '../src/auth.js'

const originalFetch = globalThis.fetch
afterEach(() => { globalThis.fetch = originalFetch; clearReadToken() })

test('只读GET携带读取凭据且401清除当前访问状态', async () => {
  setReadToken('read-only')
  globalThis.fetch = async (path, options) => {
    assert.equal(path, '/api/v1/ci-runs/13/diagnosis')
    assert.equal(options.headers.Authorization, 'Bearer read-only')
    assert.equal(options.method, undefined)
    return new Response('{}', { status: 401 })
  }
  await assert.rejects(getDiagnosis(13), { status: 401 })
  assert.equal(readToken.value, '')
})

test('触发POST使用独立凭据，错误触发令牌不清除读取状态', async () => {
  setReadToken('read-only')
  globalThis.fetch = async (path, options) => {
    assert.equal(options.method, 'POST')
    assert.equal(options.headers.Authorization, 'Bearer trigger-only')
    return new Response('{}', { status: 401 })
  }
  await assert.rejects(triggerDiagnosis(13, 'trigger-only'), { status: 401 })
  assert.equal(readToken.value, 'read-only')
})

test('202表示已有任务正在处理，客户端不会自动重复触发', async () => {
  setReadToken('read-only')
  let calls = 0
  globalThis.fetch = async (path, options) => {
    calls++
    assert.equal(options.method, 'POST')
    return Response.json({ status: 'in_progress', attempt_count: 1 }, { status: 202 })
  }
  assert.equal((await triggerDiagnosis(13, 'trigger-only')).status, 'in_progress')
  assert.equal(calls, 1)
})
