export function createEvaluationApi(baseUrl, fetcher = globalThis.fetch) {
  const base = String(baseUrl || '').replace(/\/+$/, '')
  async function request(path, body) {
    const options = body === undefined ? {} : {
      method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body)
    }
    const response = await fetcher(`${base}${path}`, options)
    const text = await response.text()
    let data
    try { data = text ? JSON.parse(text) : null } catch { data = text }
    if (!response.ok) throw new Error(typeof data === 'string' ? data : data?.detail || `${response.status} ${response.statusText}`)
    return data
  }
  return {
    scenarios: () => request('/eval/scenarios'),
    runs: () => request('/eval/runs'),
    report: (id) => request(`/eval/runs/${encodeURIComponent(id)}`),
    run: (options = {}) => request('/eval/scenarios/run', { mode: 'validate', limit: 5, ...options }),
    compare: (left, right, vary = []) => request('/eval/compare', { left_run_id: left, right_run_id: right, vary })
  }
}
