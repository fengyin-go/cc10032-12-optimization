import { request } from './client'
import { moduleMeta } from '@/data/modules'
import type { ActionResult, EntryRow, OverviewResult, PageResult } from '@/data/types'

export { moduleMeta }

export async function listEntries(
  key: string,
  filters: Record<string, string> = {},
): Promise<PageResult> {
  const query = new URLSearchParams(filters).toString()
  const response = await request(`/api/${key}?${query}`)
  if (!response.ok) throw new Error('业务数据读取失败')
  return await response.json()
}

export async function createEntry(
  key: string,
  values: Record<string, string>,
): Promise<ActionResult> {
  const response = await request(`/api/${key}`, {
    method: 'POST',
    body: JSON.stringify({ values }),
  })
  return await response.json()
}

export async function runAction(
  key: string,
  id: number,
  action: string,
): Promise<ActionResult> {
  const response = await request(`/api/${key}/${id}/actions`, {
    method: 'POST',
    body: JSON.stringify({ action }),
  })
  return await response.json()
}

export async function downloadEntries(
  key: string,
  filters: Record<string, string> = {},
): Promise<void> {
  const query = new URLSearchParams(filters).toString()
  const response = await request(`/api/${key}/export?${query}`)
  if (!response.ok) throw new Error('业务数据导出失败')
  const url = URL.createObjectURL(await response.blob())
  const anchor = document.createElement('a')
  anchor.href = url
  anchor.download = `${key}.csv`
  document.body.appendChild(anchor)
  anchor.click()
  document.body.removeChild(anchor)
  URL.revokeObjectURL(url)
}

export async function loadOverview(): Promise<OverviewResult> {
  const response = await request('/api/overview')
  if (!response.ok) throw new Error('运营概览读取失败')
  return await response.json()
}

// ---------------------------------------------------------------------------
// 12号：告警合并 / 恢复周期 / 迁移切换
// ---------------------------------------------------------------------------

export type CepHit = {
  hit_id: string
  match_start_ts: number
  match_end_ts: number
  event_count: number
  pattern: string
  source_version: string
  environment: string
  manifest_id?: string
  cep_rule?: string
  task_scope?: string
}

export type ManifestRow = {
  manifest_id: string
  source_key: string
  cep_rule: string
  task_scope: string
  plan_id: number | null
  plan_name: string
  cycle_id: string
  cycle_seq: number
  cycle_status: string
  merge_count: number
  hit_count_evidence: number
  status: string
  source_version: string
  environment: string
  first_hit_ts: number
  last_hit_ts: number
  is_current_plan: boolean
  plan_status: string
  trigger_evidence: CepHit[]
}

export type MergeManifestResult = {
  summary: {
    manifest_count: number
    merged_hit_total: number
    cepHitTotal: number
    scopes: string[]
    active_plan_ids: number[]
    active_version: string
    environment: string
  }
  groups: {
    source_key: string
    cep_rule: string
    task_scope: string
    manifest_count: number
    merged_hit_total: number
    manifests: ManifestRow[]
  }[]
  manifests: ManifestRow[]
}

export type PlanEvidence = {
  plan_id: number
  plan_name: string
  task_scope: string
  plan_status: string
  is_active: boolean
  cycle: {
    cycle_id: string
    seq: number
    status: string
    opened_at: number | null
    closed_at: number | null
    close_reason: string
  }
  manifest_id: string
  merge_count: number
  trigger_evidence: CepHit[]
  receipts: {
    receipt_id: string
    kind: string
    source_version: string
    switch_id: string
    status: string
    created_at: number
  }[]
  receipt_versions: string[]
}

export type RestorePlansResult = {
  total: number
  items: PlanEvidence[]
  audit: {
    ok: boolean
    scopes: { scope: string; active_plan_count: number; single: boolean }[]
    cross_checks?: {
      manifest_id: string
      task_scope: string
      plan_id: number
      cycle_status: string
      manifest_hit_count: number
      plan_hit_count: number
      placement_rule: string
      matched: boolean
    }[]
  }
  active_version: string
  environment: string
  build_pipeline?: BuildPipeline
}

export type SwitchRecord = {
  switch_id: string
  switch_key?: string
  path: string
  environment: string
  before_version: string
  target_version: string
  status: string
  migrated_plans?: number
  receipt_ids?: string[]
  error?: string
  requested_by?: string
  started_at?: number
  finished_at?: number
}

export type MigrationStatus = {
  active_version: string
  environment: string
  deployment: { deployment_id: string; environments: Record<string, string>; switch_count: number }
  switches: SwitchRecord[]
  ledger: { switch_id: string; status: string; before_version: string; after_version: string; environment: string; path: string; error?: string }[]
  verification: {
    ok: boolean
    checks: { check: string; ok: boolean }[]
    counts: Record<string, number>
    active_version: string
    environment: string
  }
}

export type BuildPipeline = {
  ok: boolean
  deployment_id: string
  name: string
  environments: Record<string, string>
  active_version: string
  last_switch: SwitchRecord | Record<string, never>
  steps: { name: string; status: string }[]
  switch_count: number
}

export type SwitchPath = {
  path: string
  name: string
  consistency: string
  blast_radius: string
  rollback: string
  downtime: string
  best_for: string
}

async function jsonOrThrow<T>(response: Response, message: string): Promise<T> {
  if (!response.ok) throw new Error(message)
  return (await response.json()) as T
}

export async function loadMergeManifests(): Promise<MergeManifestResult> {
  return jsonOrThrow(await request('/api/incidents/merge-manifests'), '告警合并清单读取失败')
}

export async function loadCepHits(manifestId: string): Promise<{ total: number; items: CepHit[] }> {
  return jsonOrThrow(
    await request(`/api/incidents/cep-hits?manifest_id=${encodeURIComponent(manifestId)}`),
    'CEP 命中告警数据读取失败',
  )
}

export async function ingestCepHits(payload: {
  cep_rule: string
  task_scope: string
  alert_rule_name: string
  hits: { match_start_ts: number; match_end_ts: number; event_count: number; pattern?: string }[]
  mode?: string
}): Promise<Record<string, unknown>> {
  return jsonOrThrow(
    await request('/api/incidents/cep-hits', { method: 'POST', body: JSON.stringify(payload) }),
    'CEP 命中写入失败',
  )
}

export async function loadRestorePlans(): Promise<RestorePlansResult> {
  return jsonOrThrow(await request('/api/incidents/restore-plans'), '恢复方案与周期读取失败')
}

export async function completeRestorePlan(planId: number): Promise<Record<string, unknown>> {
  return jsonOrThrow(
    await request(`/api/incidents/restore-plans/${planId}/complete`, { method: 'POST' }),
    '恢复完成标记失败',
  )
}

export async function loadMigrationStatus(): Promise<MigrationStatus> {
  return jsonOrThrow(await request('/api/migration/status'), '迁移状态读取失败')
}

export async function loadSwitchPaths(): Promise<{ paths: SwitchPath[] }> {
  return jsonOrThrow(await request('/api/migration/paths'), '切换路径说明读取失败')
}

export async function loadBuildPipeline(): Promise<BuildPipeline> {
  return jsonOrThrow(await request('/api/migration/build-pipeline'), '构建流水线读取失败')
}

export async function loadTrace(taskScope = ''): Promise<Record<string, unknown>> {
  const query = taskScope ? `?task_scope=${encodeURIComponent(taskScope)}` : ''
  return jsonOrThrow(await request(`/api/migration/trace${query}`), '调用链定位失败')
}

export async function switchEnvironment(payload: {
  environment: string
  target_version: string
  switch_key: string
  path: string
  simulate_failure?: boolean
}): Promise<Record<string, unknown>> {
  return jsonOrThrow(
    await request('/api/migration/switch', { method: 'POST', body: JSON.stringify(payload) }),
    '环境切换请求失败',
  )
}
