<template>
  <section class="page page-evaluation">
    <div class="page-heading">
      <div class="heading-copy">
        <span class="kicker">Scenario evaluation</span><h1>客服场景评测</h1>
        <p>按业务任务回放，定位路由、工具、检索与记忆问题。</p>
      </div>
      <button :disabled="busy" @click="run">{{ busy ? '运行中…' : mode === 'validate' ? '校验场景数据' : '运行模型回放' }}</button>
    </div>

    <p v-if="error" role="alert" class="eval-error">{{ error }}</p>
    <div class="workspace-card eval-controls">
      <label>运行方式<select v-model="mode"><option value="validate">数据校验 · 不调用模型</option><option value="live">真实模型回放 · 消耗模型额度</option></select></label>
      <label>业务场景<select v-model="scenario"><option value="">全部场景</option><option v-for="(count, name) in catalog?.summary?.by_scenario || {}" :key="name" :value="name">{{ scenarioLabels[name] || name }} · {{ count }}</option></select></label>
      <label>回放条数<select v-model.number="limit"><option :value="1">1 条</option><option :value="5">5 条</option><option :value="20">20 条</option><option :value="120">全部（最多 120 条）</option></select></label>
      <p v-if="catalog" class="eval-caption">{{ catalog.id }} · v{{ catalog.version }} · {{ catalog.summary?.cases ?? catalog.summary?.total ?? catalog.cases?.length }} 条场景。当前为合成回归集，待业务审核；校验通过只表示数据可执行。</p>
    </div>

    <div v-if="report" class="evaluation-content">
      <div v-if="report.status === 'validated'" class="workspace-card validation-result">
        <strong>场景数据校验通过</strong><p>已检查格式、业务断言、知识引用与场景覆盖。本次没有调用模型，也没有生成质量成绩。</p>
        <small>运行 {{ report.run_id }} · {{ report.validation?.case_count ?? '—' }} 条数据</small>
      </div>
      <div v-else class="evaluation-summary">
        <div class="score-hero"><span>任务通过率</span><strong>{{ percent(report.pass_rate) }}</strong><small>{{ report.passed }} / {{ report.total }} 个任务，包含全部尝试</small></div>
        <div><span>不可评估</span><strong>{{ report.invalid ?? 0 }}</strong></div>
        <div><span>执行错误 / 超时</span><strong>{{ report.error ?? 0 }} / {{ report.timeout ?? 0 }}</strong></div>
        <div><span>硬性失败</span><strong class="danger">{{ report.hard_fail ?? 0 }}</strong></div>
      </div>
      <div v-if="report.status !== 'validated'" class="workspace-card">
        <div class="card-heading"><h2>逐项结果</h2><label class="inline-label"><input v-model="failedOnly" type="checkbox">只看失败与不可评估</label></div>
        <p class="eval-caption">任务通过要求所有必选断言通过。延迟与 Token 仅展示本次实际记录。</p>
        <details v-for="item in visibleCases" :key="item.case_id || item.id" class="case-row">
          <summary><span class="case-status" :class="item.status">{{ statusLabel(item.status) }}</span><strong>{{ item.case_id || item.id }}</strong><span>{{ scenarioLabels[item.scenario] || item.scenario }}</span></summary>
          <p v-if="item.error || item.reason || item.error_detail" class="eval-error">{{ item.error || item.reason || item.error_detail?.message || item.error_detail }}</p>
          <div v-for="(turn, index) in item.turns || []" :key="index" class="turn-result">
            <strong>第 {{ index + 1 }} 轮 · {{ turn.actual?.intent || turn.intent || turn.reply?.intent || turn.response?.intent || turn.status }}</strong>
            <p v-if="turn.message">{{ turn.message }}</p>
            <pre>{{ turn.actual?.response || (typeof turn.response === 'string' ? turn.response : turn.reply?.response || turn.response?.response || turn.error_detail?.message || turn.error_detail || turn.error || '') }}</pre>
          </div>
          <ul class="assertion-list"><li v-for="(check, index) in item.assertions || item.checks || []" :key="check.id || index"><b>{{ statusLabel(check.status) }}</b> {{ check.id || check.type }} — {{ check.reason || check.detail || check.message || '' }}</li></ul>
          <small>{{ item.latency_ms != null ? `${Number(item.latency_ms).toFixed(0)} ms` : '' }}</small>
        </details>
        <p v-if="!visibleCases.length" class="eval-caption">当前筛选没有结果。</p>
      </div>
    </div>

    <div class="evaluation-layout eval-bottom">
      <section class="workspace-card">
        <div class="card-heading"><h2>运行历史</h2><button class="secondary" :disabled="busy" @click="load">刷新</button></div>
        <div v-for="item in history" :key="item.run_id" class="run-row"><button class="secondary" @click="openReport(item.run_id)">{{ item.run_id.slice(0, 12) }}</button><span>{{ item.mode === 'validate' ? '数据校验' : percent(item.pass_rate) }}</span><small>{{ item.timestamp }}</small></div>
        <p v-if="!history.length" class="eval-caption">运行报告将在这里保留，基线不会自动覆盖。</p>
      </section>
      <section class="workspace-card">
        <div class="card-heading"><h2>配对实验比较</h2></div>
        <label>基准运行<select v-model="left"><option value="">选择运行</option><option v-for="item in history" :key="item.run_id" :value="item.run_id">{{ item.run_id.slice(0, 12) }} · {{ item.mode }}</option></select></label>
        <label>候选运行<select v-model="right"><option value="">选择运行</option><option v-for="item in history" :key="item.run_id" :value="item.run_id">{{ item.run_id.slice(0, 12) }} · {{ item.mode }}</option></select></label>
        <p class="eval-caption">声明本次改变的因素；数据集、知识和裁判保持一致。</p>
        <div class="factor-list"><label v-for="(label, key) in factors" :key="key" class="inline-label"><input v-model="vary" type="checkbox" :value="key">{{ label }}</label></div>
        <button :disabled="busy || !left || !right" @click="compare">比较运行</button>
        <div v-if="comparison" class="comparison-result"><strong>{{ comparison.comparable ? '控制条件一致，可以比较' : '本次运行不可直接比较' }}</strong><p>{{ comparison.reason }}</p><p v-for="(value, key) in comparison.deltas || {}" :key="key">{{ key }}：{{ Number(value).toFixed(4) }}</p></div>
      </section>
    </div>
  </section>
</template>

<script setup>
import { computed, onMounted, ref, watch } from 'vue'
import { createEvaluationApi } from '../lib/evaluation.js'

const props = defineProps({ baseUrl: { type: String, required: true } })
const api = computed(() => createEvaluationApi(props.baseUrl))
const catalog = ref(null), history = ref([]), report = ref(null), comparison = ref(null)
const busy = ref(false), error = ref(''), mode = ref('validate'), scenario = ref(''), limit = ref(5)
const failedOnly = ref(false), left = ref(''), right = ref(''), vary = ref([])
const factors = { code: '代码', model: '模型', prompt: '提示词', skills: '业务规则', retrieval: '检索策略' }
const scenarioLabels = { basic_routing: '基础意图分流', order_logistics: '订单与物流', billing_refund: '账单与退款', technical_security: '技术与账号安全', multi_turn: '多轮补充与更正', cross_session_memory: '跨会话记忆', handoff_safety: '人工升级与安全边界', knowledge_boundaries: '知识缺失与政策边界' }
const visibleCases = computed(() => (report.value?.cases || []).filter(item => !failedOnly.value || item.status !== 'passed'))
const percent = value => typeof value === 'number' ? `${(value * 100).toFixed(1)}%` : '—'
const statusLabel = status => ({ passed: '通过', failed: '失败', hard_fail: '硬性失败', invalid: '不可评估', error: '错误', timeout: '超时', skipped: '未执行' }[status] || status || '—')

async function load() {
  const results = await Promise.allSettled([api.value.scenarios(), api.value.runs()])
  if (results[0].status === 'fulfilled') catalog.value = results[0].value
  if (results[1].status === 'fulfilled') history.value = results[1].value.items || results[1].value
  error.value = results.filter(item => item.status === 'rejected').map(item => item.reason.message).join('；')
}
async function run() {
  busy.value = true; error.value = ''; comparison.value = null
  try {
    report.value = await api.value.run({ mode: mode.value, limit: limit.value, ...(scenario.value ? { scenario: scenario.value } : {}) })
    await load()
  } catch (problem) { error.value = problem.message } finally { busy.value = false }
}
async function openReport(id) {
  error.value = ''
  try { report.value = await api.value.report(id) } catch (problem) { error.value = problem.message }
}
async function compare() {
  busy.value = true; error.value = ''
  try { comparison.value = await api.value.compare(left.value, right.value, vary.value) } catch (problem) { error.value = problem.message } finally { busy.value = false }
}
onMounted(load)
watch(() => props.baseUrl, () => { report.value = null; comparison.value = null; left.value = ''; right.value = ''; load() })
</script>

<style scoped>
.eval-controls { display: flex; flex-wrap: wrap; gap: 18px; align-items: end; margin-bottom: 22px; }
label { display: grid; gap: 8px; font-size: 13px; color: #555c71; margin-bottom: 12px; }
select { min-width: 150px; width: 100%; padding: 10px 12px; border: 1px solid #dce1ec; border-radius: 10px; background: white; color: #202b43; }
.eval-caption { color: #6d7589; font-size: 13px; line-height: 1.7; }
.eval-controls .eval-caption { flex-basis: 100%; margin: 0; }
.eval-error { padding: 12px 16px; border-radius: 10px; color: #a23535; background: #fff1ef; overflow-wrap: anywhere; }
.validation-result { margin-bottom: 20px; border-left: 4px solid #4b8b75; }
.validation-result p, .comparison-result p { line-height: 1.7; font-size: 14px; }
.eval-bottom { margin-top: 24px; }
.inline-label { display: flex; align-items: center; gap: 7px; margin: 0; }
.inline-label input { width: auto; accent-color: #365dba; }
.case-row { padding: 16px 0; border-top: 1px solid #edf0f6; }
.case-row summary { cursor: pointer; display: flex; gap: 12px; flex-wrap: wrap; align-items: center; font-size: 13px; }
.case-status { padding: 4px 8px; border-radius: 6px; background: #edf1f8; color: #526078; }
.case-status.passed { background: #e8f5ee; color: #2c7754; }
.case-status.failed, .case-status.hard_fail, .case-status.error, .case-status.timeout { background: #fff0ed; color: #a84c3c; }
.case-status.invalid { background: #fff4d8; color: #8b6b24; }
.turn-result { padding: 14px; margin-top: 12px; background: #f8f9fc; border-radius: 10px; font-size: 13px; }
pre { white-space: pre-wrap; overflow-wrap: anywhere; font-family: inherit; line-height: 1.7; }
.assertion-list { padding-left: 20px; font-size: 13px; line-height: 1.9; }
.run-row { display: flex; gap: 12px; flex-wrap: wrap; align-items: center; padding: 12px 0; border-top: 1px solid #edf0f6; font-size: 13px; }
.run-row small { color: #7e879a; }
.factor-list { display: flex; flex-wrap: wrap; gap: 14px; margin-bottom: 18px; }
.comparison-result { margin-top: 20px; padding-top: 18px; border-top: 1px solid #edf0f6; }
@media (max-width: 760px) { .eval-controls { display: grid; } .eval-controls label { width: 100%; } }
</style>
