import test from 'node:test'
import assert from 'node:assert/strict'
import { createEvaluationApi } from '../src/lib/evaluation.js'

test('default scenario action validates data without paid model execution', async () => {
  const requests = []
  const api = createEvaluationApi('/api/python/', async (url, options = {}) => {
    requests.push({ url, options })
    return { ok: true, text: async () => JSON.stringify({ status: 'validated', total: 0 }) }
  })
  const report = await api.run()
  assert.equal(requests[0].url, '/api/python/eval/scenarios/run')
  assert.deepEqual(JSON.parse(requests[0].options.body), { mode: 'validate', limit: 5 })
  assert.equal(report.status, 'validated')
  assert.equal(report.total, 0)
})

test('live replay and declared experiment factors are explicit payloads', async () => {
  const requests = []
  const api = createEvaluationApi('/api', async (url, options) => {
    requests.push({ url, options })
    return { ok: true, text: async () => '{}' }
  })
  await api.run({ mode: 'live', scenario: 'billing', limit: 1 })
  await api.compare('left', 'right', ['retrieval'])
  assert.deepEqual(JSON.parse(requests[0].options.body), { mode: 'live', scenario: 'billing', limit: 1 })
  assert.deepEqual(JSON.parse(requests[1].options.body), { left_run_id: 'left', right_run_id: 'right', vary: ['retrieval'] })
})

test('history identifiers are encoded and backend errors remain visible', async () => {
  const calls = []
  const api = createEvaluationApi('/api', async (url) => {
    calls.push(url)
    return { ok: false, status: 404, statusText: 'Not Found', text: async () => '{"detail":"unknown run"}' }
  })
  await assert.rejects(api.report('a/b'), /unknown run/)
  assert.equal(calls[0], '/api/eval/runs/a%2Fb')
})
