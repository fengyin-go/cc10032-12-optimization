<template>
  <section class="page" data-page="alert-merge">
    <header class="page-head">
      <div>
        <h2>告警合并清单</h2>
        <p class="page-desc">
          读取 CEP 命中告警数据，按同源（CEP 规则 + 任务范围）合并，同源只对应一个当前恢复方案。
        </p>
      </div>
      <div class="page-actions">
        <button class="btn" data-testid="refresh-merge" type="button" @click="reload">刷新清单</button>
        <RouterLink class="btn primary" data-testid="goto-restore" to="/restore/cycles">查看恢复周期</RouterLink>
      </div>
    </header>

    <div class="stat-row">
      <article class="stat-card">
        <span class="stat-label">合并清单数</span>
        <strong class="stat-value" data-testid="manifest-count">{{ summary.manifest_count }}</strong>
      </article>
      <article class="stat-card">
        <span class="stat-label">合并触发记录</span>
        <strong class="stat-value" data-testid="merged-hit-total">{{ summary.merged_hit_total }}</strong>
      </article>
      <article class="stat-card">
        <span class="stat-label">CEP 命中总数</span>
        <strong class="stat-value" data-testid="cep-hit-total">{{ summary.cepHitTotal }}</strong>
      </article>
      <article class="stat-card">
        <span class="stat-label">活动恢复方案</span>
        <strong class="stat-value" data-testid="active-plan-count">{{ summary.active_plan_ids.length }}</strong>
      </article>
      <article class="stat-card">
        <span class="stat-label">当前版本</span>
        <strong class="stat-value" data-testid="alert-active-version">{{ summary.active_version }}</strong>
      </article>
    </div>

    <p v-if="auditHint" class="status-legend" data-testid="audit-hint">{{ auditHint }}</p>

    <table class="data-table">
      <thead>
        <tr>
          <th>CEP 规则</th>
          <th>任务范围</th>
          <th>合并数量</th>
          <th>周期</th>
          <th>当前恢复方案</th>
          <th>方案状态</th>
          <th>来源版本</th>
          <th>触发证据</th>
        </tr>
      </thead>
      <tbody>
        <template v-for="manifest in manifests" :key="manifest.manifest_id">
          <tr
            :data-testid="`manifest-row-${manifest.manifest_id}`"
            :data-merge-count="manifest.merge_count"
          >
            <td>{{ manifest.cep_rule }}</td>
            <td>{{ manifest.task_scope }}</td>
            <td>
              <strong data-testid="merge-count">{{ manifest.merge_count }}</strong>
              <span class="muted"> / {{ manifest.hit_count_evidence }} 条证据</span>
            </td>
            <td>
              <span :class="['badge', manifest.cycle_status === 'current' ? 'badge-on' : 'badge-off']">
                #{{ manifest.cycle_seq }} {{ manifest.cycle_status === 'current' ? '当前' : '已关闭' }}
              </span>
            </td>
            <td>
              <RouterLink
                class="link"
                :to="`/restore/cycles?plan=${manifest.plan_id}`"
                :data-testid="`plan-link-${manifest.plan_id}`"
              >
                {{ manifest.plan_name }} (#{{ manifest.plan_id }})
              </RouterLink>
              <span v-if="manifest.is_current_plan" class="badge badge-on">活动</span>
            </td>
            <td>{{ manifest.plan_status }}</td>
            <td>{{ manifest.source_version }}</td>
            <td>
              <button
                class="link"
                type="button"
                :data-testid="`toggle-evidence-${manifest.manifest_id}`"
                @click="toggle(manifest.manifest_id)"
              >
                {{ expanded[manifest.manifest_id] ? '收起' : `展开 ${manifest.trigger_evidence.length} 条` }}
              </button>
            </td>
          </tr>
          <tr v-if="expanded[manifest.manifest_id]">
            <td colspan="8">
              <table class="data-table nested" data-testid="evidence-table">
                <thead>
                  <tr>
                    <th>命中ID</th>
                    <th>匹配开始</th>
                    <th>匹配结束</th>
                    <th>事件数</th>
                    <th>事件模式</th>
                    <th>来源版本</th>
                  </tr>
                </thead>
                <tbody>
                  <tr
                    v-for="hit in manifest.trigger_evidence"
                    :key="hit.hit_id"
                    :data-testid="`evidence-${hit.hit_id}`"
                  >
                    <td>{{ hit.hit_id }}</td>
                    <td>{{ formatTs(hit.match_start_ts) }}</td>
                    <td>{{ formatTs(hit.match_end_ts) }}</td>
                    <td>{{ hit.event_count }}</td>
                    <td class="mono">{{ hit.pattern }}</td>
                    <td>{{ hit.source_version }}</td>
                  </tr>
                </tbody>
              </table>
            </td>
          </tr>
        </template>
        <tr v-if="!manifests.length">
          <td colspan="8" class="empty-state">暂无告警合并清单</td>
        </tr>
      </tbody>
    </table>

    <section class="panel">
      <h3>模拟一次连续 CEP 命中</h3>
      <p class="page-desc">
        同一来源连续触发时，默认合并到当前方案延长处理；若当前周期已恢复完成，则关闭上一周期并开启新方案。
      </p>
      <form class="filter-bar" @submit.prevent="submitHit">
        <label class="filter-item">
          <span>CEP 规则</span>
          <input v-model="form.cep_rule" data-testid="hit-cep-rule" />
        </label>
        <label class="filter-item">
          <span>任务范围</span>
          <input v-model="form.task_scope" data-testid="hit-task-scope" />
        </label>
        <label class="filter-item">
          <span>事件数</span>
          <input v-model.number="form.event_count" data-testid="hit-event-count" type="number" min="1" />
        </label>
        <label class="filter-item">
          <span>处理方式</span>
          <select v-model="form.mode" data-testid="hit-mode">
            <option value="auto">自动（完成则开新周期）</option>
            <option value="extend">合并延长当前方案</option>
            <option value="new_cycle">关闭上一周期并开新周期</option>
          </select>
        </label>
        <button class="btn primary" data-testid="submit-hit" type="submit" :disabled="submitting">
          {{ submitting ? '写入中…' : '写入命中并合并' }}
        </button>
      </form>
      <p v-if="resultText" class="result-text" data-testid="hit-result">{{ resultText }}</p>
    </section>

    <footer class="page-foot">
      <span>共 {{ manifests.length }} 份合并清单，覆盖 {{ summary.scopes.length }} 个任务范围</span>
      <span v-if="errorMessage" class="error-text">{{ errorMessage }}</span>
    </footer>
  </section>
</template>

<script setup lang="ts">
import { onMounted, ref } from 'vue'

import { ingestCepHits, loadMergeManifests, loadRestorePlans } from '@/api/backend-service'
import type { ManifestRow, MergeManifestResult } from '@/api/backend-service'

const summary = ref<MergeManifestResult['summary']>({
  manifest_count: 0,
  merged_hit_total: 0,
  cepHitTotal: 0,
  scopes: [],
  active_plan_ids: [],
  active_version: '',
  environment: '',
})
const manifests = ref<ManifestRow[]>([])
const expanded = ref<Record<string, boolean>>({})
const auditHint = ref('')
const errorMessage = ref('')
const resultText = ref('')
const submitting = ref(false)
const form = ref({ cep_rule: '登录失败序列', task_scope: 'login_security', event_count: 3, mode: 'auto' })

function formatTs(value: number) {
  if (!value) return '—'
  return new Date(value * 1000).toLocaleString('zh-CN', { hour12: false })
}

function toggle(manifestId: string) {
  expanded.value = { ...expanded.value, [manifestId]: !expanded.value[manifestId] }
}

async function reload() {
  errorMessage.value = ''
  try {
    const payload = await loadMergeManifests()
    summary.value = payload.summary
    manifests.value = payload.manifests
    const audit = await loadRestorePlans()
    const bad = audit.audit.scopes.filter((item) => !item.single)
    auditHint.value = bad.length
      ? `警告：${bad.map((item) => item.scope).join('、')} 存在多个活动恢复方案`
      : `任务范围核对通过：${audit.audit.scopes.map((item) => `${item.scope}=${item.active_plan_count}`).join('，')}`
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '告警合并清单读取失败'
  }
}

async function submitHit() {
  submitting.value = true
  resultText.value = ''
  errorMessage.value = ''
  try {
    const now = Math.floor(Date.now() / 1000)
    const offset = manifests.value.reduce((sum, item) => sum + item.merge_count, 0)
    const result = (await ingestCepHits({
      cep_rule: form.value.cep_rule,
      task_scope: form.value.task_scope,
      alert_rule_name: '登录失败告警规则',
      mode: form.value.mode,
      hits: [
        {
          match_start_ts: now + offset,
          match_end_ts: now + offset + 30,
          event_count: form.value.event_count,
          pattern: 'login_fail,login_fail,login_fail',
        },
      ],
    })) as Record<string, unknown>
    const bits = [
      `写入 ${result.ingested ?? 0} 条`,
      `合并数量 ${result.merge_count ?? 0}`,
      `活动方案 ${result.active_plan_count ?? 0}`,
    ]
    if (result.rotated) bits.push('已关闭上一周期并开启新周期')
    if (result.reopened) bits.push('已合并延长当前方案')
    if (result.duplicates) bits.push(`重复命中 ${result.duplicates} 条未重复计数`)
    resultText.value = bits.join('，')
    await reload()
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : 'CEP 命中写入失败'
  } finally {
    submitting.value = false
  }
}

onMounted(reload)
</script>
