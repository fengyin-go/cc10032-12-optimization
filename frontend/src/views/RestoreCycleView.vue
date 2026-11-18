<template>
  <section class="page" data-page="restore-cycles">
    <header class="page-head">
      <div>
        <h2>恢复方案与周期</h2>
        <p class="page-desc">
          每个活动恢复方案对应一个恢复周期和一份告警合并清单；恢复完成后重新触发会形成可区分的新周期。
        </p>
      </div>
      <div class="page-actions">
        <button class="btn" data-testid="refresh-cycles" type="button" @click="reload">刷新</button>
        <RouterLink class="btn" data-testid="goto-alerts" to="/alert/merge">查看告警合并</RouterLink>
        <RouterLink class="btn primary" data-testid="goto-deploy" to="/deploy/migration">迁移与部署</RouterLink>
      </div>
    </header>

    <div class="stat-row">
      <article class="stat-card">
        <span class="stat-label">恢复方案</span>
        <strong class="stat-value" data-testid="plan-total">{{ payload.total }}</strong>
      </article>
      <article class="stat-card">
        <span class="stat-label">活动方案</span>
        <strong class="stat-value" data-testid="active-total">{{ activeCount }}</strong>
      </article>
      <article class="stat-card">
        <span class="stat-label">唯一活动范围核对</span>
        <strong class="stat-value" :class="payload.audit.ok ? 'ok-text' : 'error-text'" data-testid="audit-ok">
          {{ payload.audit.ok ? '通过' : '不通过' }}
        </strong>
      </article>
      <article class="stat-card">
        <span class="stat-label">当前版本</span>
        <strong class="stat-value" data-testid="restore-active-version">{{ payload.active_version }}</strong>
      </article>
    </div>

    <table class="data-table">
      <thead>
        <tr>
          <th>恢复方案</th>
          <th>任务范围</th>
          <th>周期</th>
          <th>方案状态</th>
          <th>合并数量</th>
          <th>回执版本</th>
          <th>操作</th>
        </tr>
      </thead>
      <tbody>
        <tr
          v-for="plan in payload.items"
          :key="plan.plan_id"
          :data-testid="`cycle-row-${plan.plan_id}`"
          :data-cycle-seq="plan.cycle.seq"
          :data-active="plan.is_active"
        >
          <td>
            {{ plan.plan_name }} (#{{ plan.plan_id }})
            <span v-if="plan.is_active" class="badge badge-on">活动</span>
            <span v-else class="badge badge-off">已关闭</span>
          </td>
          <td>{{ plan.task_scope }}</td>
          <td>
            <span data-testid="cycle-seq">#{{ plan.cycle.seq }}</span>
            <span class="muted">{{ plan.cycle.status === 'current' ? ' 当前周期' : ' 已关闭' }}</span>
          </td>
          <td>{{ plan.plan_status }}</td>
          <td data-testid="plan-merge-count">{{ plan.merge_count }}</td>
          <td>
            <span
              v-for="version in plan.receipt_versions"
              :key="version"
              class="badge badge-version"
              :data-testid="`receipt-version-${plan.plan_id}-${version}`"
            >
              {{ version }}
            </span>
          </td>
          <td class="row-actions">
            <button
              class="link"
              type="button"
              :data-testid="`toggle-plan-${plan.plan_id}`"
              @click="toggle(plan.plan_id)"
            >
              {{ expanded[plan.plan_id] ? '收起证据' : '查看周期与证据' }}
            </button>
            <button
              v-if="plan.is_active && plan.plan_status !== '已生效'"
              class="link"
              type="button"
              :data-testid="`complete-${plan.plan_id}`"
              @click="complete(plan)"
            >
              标记恢复完成
            </button>
          </td>
        </tr>
        <tr v-if="!payload.items.length">
          <td colspan="7" class="empty-state">暂无恢复方案</td>
        </tr>
      </tbody>
    </table>

    <section v-if="selected" class="panel" data-testid="plan-detail">
      <header class="panel-head">
        <h3>方案 {{ selected.plan_id }} · 周期 #{{ selected.cycle.seq }}</h3>
        <span class="muted">
          周期 {{ selected.cycle.cycle_id }} · {{ selected.cycle.status === 'current' ? '当前' : '已关闭' }}
          <template v-if="selected.cycle.close_reason">（{{ selected.cycle.close_reason }}）</template>
        </span>
      </header>

      <dl class="dag-detail">
        <div>
          <dt>合并清单</dt>
          <dd data-testid="detail-manifest">{{ selected.manifest_id }}</dd>
        </div>
        <div>
          <dt>合并数量</dt>
          <dd data-testid="detail-merge-count">{{ selected.merge_count }}</dd>
        </div>
        <div>
          <dt>来源版本（保留全部）</dt>
          <dd data-testid="detail-receipt-versions">{{ selected.receipt_versions.join(' / ') }}</dd>
        </div>
        <div>
          <dt>周期开启 / 关闭</dt>
          <dd>{{ formatTs(selected.cycle.opened_at) }} / {{ formatTs(selected.cycle.closed_at) }}</dd>
        </div>
      </dl>

      <h4>触发证据（CEP 命中告警数据）</h4>
      <table class="data-table nested" data-testid="plan-evidence-table">
        <thead>
          <tr>
            <th>命中ID</th>
            <th>匹配开始</th>
            <th>匹配结束</th>
            <th>事件数</th>
            <th>事件模式</th>
            <th>写入版本</th>
          </tr>
        </thead>
        <tbody>
          <tr
            v-for="hit in selected.trigger_evidence"
            :key="hit.hit_id"
            :data-testid="`plan-evidence-${hit.hit_id}`"
          >
            <td>{{ hit.hit_id }}</td>
            <td>{{ formatTs(hit.match_start_ts) }}</td>
            <td>{{ formatTs(hit.match_end_ts) }}</td>
            <td>{{ hit.event_count }}</td>
            <td class="mono">{{ hit.pattern }}</td>
            <td>{{ hit.source_version }}</td>
          </tr>
          <tr v-if="!selected.trigger_evidence.length">
            <td colspan="6" class="empty-state">该方案暂无触发证据</td>
          </tr>
        </tbody>
      </table>

      <h4>告警合并清单核对</h4>
      <table class="data-table nested" data-testid="cross-check-table">
        <thead>
          <tr>
            <th>合并清单</th>
            <th>周期状态</th>
            <th>清单命中数</th>
            <th>方案命中数</th>
            <th>核对规则</th>
            <th>结论</th>
          </tr>
        </thead>
        <tbody>
          <tr
            v-for="row in payload.audit.cross_checks || []"
            :key="row.manifest_id"
            :data-testid="`cross-check-${row.manifest_id}`"
          >
            <td class="mono">{{ row.manifest_id }}</td>
            <td>{{ row.cycle_status === 'current' ? '当前' : '已关闭' }}</td>
            <td>{{ row.manifest_hit_count }}</td>
            <td>{{ row.plan_hit_count }}</td>
            <td class="muted">{{ row.placement_rule }}</td>
            <td :class="row.matched ? 'ok-text' : 'error-text'">{{ row.matched ? '对应' : '不一致' }}</td>
          </tr>
        </tbody>
      </table>

      <h4>恢复方案回执</h4>
      <table class="data-table nested" data-testid="plan-receipt-table">
        <thead>
          <tr>
            <th>回执ID</th>
            <th>类型</th>
            <th>来源版本</th>
            <th>切换批次</th>
            <th>状态</th>
            <th>时间</th>
          </tr>
        </thead>
        <tbody>
          <tr
            v-for="receipt in selected.receipts"
            :key="receipt.receipt_id"
            :data-testid="`plan-receipt-${receipt.receipt_id}`"
          >
            <td>{{ receipt.receipt_id }}</td>
            <td>{{ receipt.kind }}</td>
            <td>{{ receipt.source_version }}</td>
            <td>{{ receipt.switch_id || '—' }}</td>
            <td>{{ receipt.status }}</td>
            <td>{{ formatTs(receipt.created_at) }}</td>
          </tr>
        </tbody>
      </table>

      <h4>构建流水线状态</h4>
      <ol class="pipeline" data-testid="detail-pipeline">
        <li
          v-for="step in payload.build_pipeline?.steps || []"
          :key="step.name"
          :class="['pipeline-step', `step-${step.status}`]"
          :data-testid="`detail-step-${step.name}`"
        >
          <span class="step-name">{{ step.name }}</span>
          <span class="step-status">{{ step.status }}</span>
        </li>
      </ol>
    </section>

    <footer class="page-foot">
      <span>活动方案 {{ activeCount }} 个，每个任务范围只保留一个活动恢复范围</span>
      <span v-if="message" class="result-text" data-testid="restore-message">{{ message }}</span>
      <span v-if="errorMessage" class="error-text">{{ errorMessage }}</span>
    </footer>
  </section>
</template>

<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'
import { useRoute } from 'vue-router'

import { completeRestorePlan, loadRestorePlans } from '@/api/backend-service'
import type { RestorePlansResult } from '@/api/backend-service'

const route = useRoute()
const payload = ref<RestorePlansResult>({
  total: 0,
  items: [],
  audit: { ok: true, scopes: [], cross_checks: [] },
  active_version: '',
  environment: '',
})
const expanded = ref<Record<number, boolean>>({})
const message = ref('')
const errorMessage = ref('')

const activeCount = computed(() => payload.value.items.filter((item) => item.is_active).length)
const selected = computed(() => payload.value.items.find((item) => expanded.value[item.plan_id]) ?? null)

function formatTs(value: number | null) {
  if (!value) return '—'
  return new Date(value * 1000).toLocaleString('zh-CN', { hour12: false })
}

function toggle(planId: number) {
  expanded.value = { [planId]: !expanded.value[planId] }
}

async function complete(plan: RestorePlansResult['items'][number]) {
  errorMessage.value = ''
  message.value = ''
  try {
    const result = (await completeRestorePlan(plan.plan_id)) as Record<string, unknown>
    message.value = `方案 ${plan.plan_id} 已标记恢复完成，周期 #${result.cycle_seq} 保留，累计 ${result.hit_count} 条触发记录`
    await reload()
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '恢复完成标记失败'
  }
}

async function reload() {
  errorMessage.value = ''
  try {
    payload.value = await loadRestorePlans()
    const requested = Number(route.query.plan)
    if (requested) {
      expanded.value = { [requested]: true }
    } else if (selected.value === null) {
      const active = payload.value.items.find((item) => item.is_active)
      if (active) expanded.value = { [active.plan_id]: true }
    }
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '恢复方案与周期读取失败'
  }
}

onMounted(reload)
</script>
