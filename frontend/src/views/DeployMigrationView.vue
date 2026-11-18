<template>
  <section class="page" data-page="deploy-migration">
    <header class="page-head">
      <div>
        <h2>迁移与部署</h2>
        <p class="page-desc">
          12号迁移同时贯通部署页面：切换前查看路径取舍，切换后核对迁移台账与恢复方案回执。
        </p>
      </div>
      <div class="page-actions">
        <button class="btn" data-testid="refresh-migration" type="button" @click="reload">刷新状态</button>
        <RouterLink class="btn" data-testid="goto-cycles" to="/restore/cycles">恢复周期</RouterLink>
      </div>
    </header>

    <div class="stat-row">
      <article class="stat-card">
        <span class="stat-label">当前版本</span>
        <strong class="stat-value" data-testid="active-version">{{ status.active_version }}</strong>
      </article>
      <article class="stat-card">
        <span class="stat-label">开发环境版本</span>
        <strong class="stat-value" data-testid="env-version">{{ status.deployment.environments.dev || '—' }}</strong>
      </article>
      <article class="stat-card">
        <span class="stat-label">切换次数</span>
        <strong class="stat-value" data-testid="switch-count">{{ status.deployment.switch_count }}</strong>
      </article>
      <article class="stat-card">
        <span class="stat-label">迁移核对</span>
        <strong class="stat-value" :class="status.verification.ok ? 'ok-text' : 'error-text'" data-testid="verify-ok">
          {{ status.verification.ok ? '通过' : '不通过' }}
        </strong>
      </article>
    </div>

    <section class="panel">
      <header class="panel-head">
        <h3>切换操作（序号12）</h3>
        <span class="muted">同一幂等键重复提交只生效一次；事务失步时整体回滚原版本</span>
      </header>
      <form class="filter-bar" @submit.prevent="submitSwitch">
        <label class="filter-item">
          <span>环境</span>
          <input v-model="form.environment" data-testid="switch-environment" />
        </label>
        <label class="filter-item">
          <span>目标版本</span>
          <select v-model="form.target_version" data-testid="switch-target">
            <option value="v2-migrated">v2-migrated（迁移后）</option>
            <option value="legacy-v1">legacy-v1（旧环境）</option>
          </select>
        </label>
        <label class="filter-item">
          <span>切换路径</span>
          <select v-model="form.path" data-testid="switch-path">
            <option value="atomic">atomic 一次性原子切换</option>
            <option value="rolling">rolling 滚动逐方案切换</option>
          </select>
        </label>
        <label class="filter-item">
          <span>幂等键</span>
          <input v-model="form.switch_key" data-testid="switch-key" />
        </label>
        <label class="filter-item checkbox">
          <span>模拟事务失步</span>
          <input v-model="form.simulate_failure" data-testid="switch-failure" type="checkbox" />
        </label>
        <button class="btn primary" data-testid="submit-switch" type="submit" :disabled="switching">
          {{ switching ? '切换中…' : '执行切换' }}
        </button>
      </form>
      <p v-if="switchMessage" class="result-text" data-testid="switch-result">{{ switchMessage }}</p>
    </section>

    <section class="panel">
      <h3>两条切换路径的取舍</h3>
      <table class="data-table" data-testid="path-table">
        <thead>
          <tr>
            <th>路径</th>
            <th>一致性</th>
            <th>影响面</th>
            <th>回滚粒度</th>
            <th>停机</th>
            <th>适用场景</th>
          </tr>
        </thead>
        <tbody>
          <tr v-for="path in paths" :key="path.path" :data-testid="`path-${path.path}`">
            <td><strong>{{ path.path }}</strong> {{ path.name }}</td>
            <td>{{ path.consistency }}</td>
            <td>{{ path.blast_radius }}</td>
            <td>{{ path.rollback }}</td>
            <td>{{ path.downtime }}</td>
            <td>{{ path.best_for }}</td>
          </tr>
        </tbody>
      </table>
    </section>

    <section class="panel">
      <h3>构建流水线（故障恢复详情面板）</h3>
      <ol class="pipeline" data-testid="build-pipeline">
        <li
          v-for="step in pipeline.steps"
          :key="step.name"
          :class="['pipeline-step', `step-${step.status}`]"
          :data-testid="`step-${step.name}`"
          :data-status="step.status"
        >
          <span class="step-name">{{ step.name }}</span>
          <span class="step-status">{{ step.status }}</span>
        </li>
      </ol>
      <p v-if="pipeline.last_switch?.error" class="error-text">最近一次切换失败：{{ pipeline.last_switch.error }}</p>
    </section>

    <section class="panel">
      <h3>迁移台账（迁移前后结果）</h3>
      <form class="filter-bar" @submit.prevent="reloadTrace">
        <label class="filter-item">
          <span>任务范围</span>
          <input v-model="traceScope" data-testid="trace-scope" placeholder="login_security" />
        </label>
        <button class="btn" data-testid="trace-query" type="submit">顺 CEP 调用链定位旧环境</button>
      </form>
      <table v-if="traceChain.length" class="data-table nested" data-testid="trace-table">
        <thead>
          <tr><th>调用链阶段</th><th>旧版本数据</th><th>总数</th></tr>
        </thead>
        <tbody>
          <tr v-for="stage in traceChain" :key="String(stage.stage)" :data-testid="`trace-${stage.stage}`">
            <td>{{ stage.stage }}</td>
            <td>{{ stage.legacy_count ?? 0 }}</td>
            <td>{{ stage.total ?? '—' }}</td>
          </tr>
        </tbody>
      </table>

      <table class="data-table" data-testid="ledger-table">
        <thead>
          <tr>
            <th>切换批次</th>
            <th>环境</th>
            <th>路径</th>
            <th>切换前</th>
            <th>切换后</th>
            <th>状态</th>
            <th>说明</th>
          </tr>
        </thead>
        <tbody>
          <tr v-for="entry in status.ledger" :key="entry.switch_id" :data-testid="`ledger-${entry.switch_id}`">
            <td class="mono">{{ entry.switch_id }}</td>
            <td>{{ entry.environment }}</td>
            <td>{{ entry.path }}</td>
            <td>{{ entry.before_version }}</td>
            <td>{{ entry.after_version }}</td>
            <td>{{ entry.status }}</td>
            <td :class="entry.error ? 'error-text' : 'muted'">{{ entry.error || '—' }}</td>
          </tr>
          <tr v-if="!status.ledger.length">
            <td colspan="7" class="empty-state">暂无迁移台账，执行一次切换后生成</td>
          </tr>
        </tbody>
      </table>
    </section>

    <section class="panel">
      <h3>迁移后核对</h3>
      <table class="data-table nested" data-testid="verify-table">
        <thead>
          <tr><th>核对项</th><th>结果</th></tr>
        </thead>
        <tbody>
          <tr v-for="check in status.verification.checks" :key="check.check" :data-testid="`verify-${check.check}`">
            <td>{{ check.check }}</td>
            <td :class="check.ok ? 'ok-text' : 'error-text'">{{ check.ok ? '通过' : '失败' }}</td>
          </tr>
        </tbody>
      </table>
      <p class="muted">
        数据量：命中 {{ status.verification.counts.hits ?? 0 }} 条 · 合并清单
        {{ status.verification.counts.manifests ?? 0 }} 份 · 恢复方案
        {{ status.verification.counts.plans ?? 0 }} 个 · 周期 {{ status.verification.counts.cycles ?? 0 }} 个 · 回执
        {{ status.verification.counts.receipts ?? 0 }} 条
      </p>
    </section>

    <footer class="page-foot">
      <span>重复切换只生效一次；事务失步后回滚原版本</span>
      <span v-if="errorMessage" class="error-text">{{ errorMessage }}</span>
    </footer>
  </section>
</template>

<script setup lang="ts">
import { onMounted, ref } from 'vue'

import {
  loadBuildPipeline,
  loadMigrationStatus,
  loadSwitchPaths,
  loadTrace,
  switchEnvironment,
} from '@/api/backend-service'
import type { BuildPipeline, MigrationStatus, SwitchPath } from '@/api/backend-service'

const status = ref<MigrationStatus>({
  active_version: '',
  environment: '',
  deployment: { deployment_id: 'deploy_12', environments: {}, switch_count: 0 },
  switches: [],
  ledger: [],
  verification: { ok: false, checks: [], counts: {}, active_version: '', environment: '' },
})
const pipeline = ref<BuildPipeline>({
  ok: false,
  deployment_id: 'deploy_12',
  name: '',
  environments: {},
  active_version: '',
  last_switch: {},
  steps: [],
  switch_count: 0,
})
const paths = ref<SwitchPath[]>([])
const traceChain = ref<Record<string, unknown>[]>([])
const traceScope = ref('login_security')
const switchMessage = ref('')
const errorMessage = ref('')
const switching = ref(false)
const form = ref({
  environment: 'dev',
  target_version: 'v2-migrated',
  path: 'atomic',
  switch_key: 'release-12-dev',
  simulate_failure: false,
})

async function reload() {
  errorMessage.value = ''
  try {
    const [statusPayload, pathPayload, pipelinePayload] = await Promise.all([
      loadMigrationStatus(),
      loadSwitchPaths(),
      loadBuildPipeline(),
    ])
    status.value = statusPayload
    paths.value = pathPayload.paths
    pipeline.value = pipelinePayload
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '迁移状态读取失败'
  }
}

async function reloadTrace() {
  errorMessage.value = ''
  try {
    const result = await loadTrace(traceScope.value)
    const chain = result.chain
    traceChain.value = Array.isArray(chain) ? (chain as Record<string, unknown>[]) : []
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '调用链定位失败'
  }
}

async function submitSwitch() {
  switching.value = true
  switchMessage.value = ''
  errorMessage.value = ''
  try {
    const result = (await switchEnvironment({ ...form.value })) as Record<string, unknown>
    if (result.ok) {
      const bits = []
      if (result.noop) {
        bits.push('目标版本已是当前版本，未执行切换、未产生新回执')
      } else if (result.idempotent) {
        bits.push('幂等键已生效过，本次未重复执行')
        if (result.before_version) bits.push(`沿用首次切换 ${result.before_version} → ${result.target_version}`)
        if (Array.isArray(result.receipt_ids)) bits.push(`沿用首次回执 ${result.receipt_ids.length} 条`)
      } else {
        bits.push('切换已提交')
        if (result.before_version) bits.push(`${result.before_version} → ${result.target_version}`)
        if (result.migrated_plans !== undefined) bits.push(`迁移方案 ${result.migrated_plans} 个`)
        if (Array.isArray(result.receipt_ids)) bits.push(`新增回执 ${result.receipt_ids.length} 条`)
      }
      switchMessage.value = bits.join('，')
    } else {
      switchMessage.value = `切换失败：${String(result.error ?? '未知错误')}（已回滚到 ${String(result.active_version ?? '')}）`
    }
    await reload()
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '环境切换请求失败'
  } finally {
    switching.value = false
  }
}

onMounted(async () => {
  await reload()
  await reloadTrace()
})
</script>
