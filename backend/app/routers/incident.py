"""12号告警合并 / 恢复方案 / 迁移切换接口。

路径统一挂在 /api/incidents 与 /api/migration 下，避免与通用 /api/{module} 资源路由抢占。
"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from app.services import incident_ledger, migration_switch
from app.services import service_for
from app.store import store

router = APIRouter(tags=["12号告警合并与迁移"])


class CepHitPayload(BaseModel):
    cep_rule: str = "登录失败序列"
    task_scope: str = "login_security"
    alert_rule_name: str = "登录失败告警规则"
    hits: list[dict[str, Any]] = Field(default_factory=list)
    mode: str = "auto"
    environment: str | None = None


class SwitchPayload(BaseModel):
    environment: str = "dev"
    target_version: str = "v2-migrated"
    switch_key: str | None = None
    path: str = "atomic"
    requested_by: str = "发布负责人"
    simulate_failure: bool = False
    rerun: bool = False


def _alert_service():
    service = service_for("alert_policy")
    assert service is not None
    return service


# ---------------------------------------------------------------------------
# 告警合并
# ---------------------------------------------------------------------------

@router.get("/api/incidents/merge-manifests")
def merge_manifests() -> dict[str, Any]:
    """告警页面：合并清单 + 合并数量汇总。"""
    return {
        "summary": _alert_service().alert_merge_summary(),
        "groups": _alert_service().list_merge_manifests(),
        "manifests": incident_ledger.manifest_rows(),
    }


@router.get("/api/incidents/merge-manifests/{manifest_id}")
def merge_manifest_detail(manifest_id: str) -> dict[str, Any]:
    rows = [r for r in incident_ledger.manifest_rows() if r["manifest_id"] == manifest_id]
    if not rows:
        raise HTTPException(status_code=404, detail=f"告警合并清单 {manifest_id} 不存在")
    manifest = store._manifests[manifest_id]
    plan = store.find("restore_plan", int(manifest.get("plan_id", 0))) or {}
    return {
        **rows[0],
        "plan_evidence": incident_ledger.plan_evidence(plan) if plan else {},
        "receipts": migration_switch.receipt_rows(int(manifest.get("plan_id", 0))),
    }

@router.get("/api/incidents/cep-hits")
def cep_hits(task_scope: str | None = None, manifest_id: str | None = None) -> dict[str, Any]:
    """CEP 命中告警原始数据，逐条可查，合并后也不丢。"""
    rows = migration_switch.hit_rows(task_scope, manifest_id)
    return {"total": len(rows), "items": rows}


@router.post("/api/incidents/cep-hits")
def ingest_cep_hits(payload: CepHitPayload) -> dict[str, Any]:
    """写入一批 CEP 命中并按同源合并；重复提交同一批命中不会重复计数。"""
    return _alert_service().ingest_cep_hits(
        cep_rule=payload.cep_rule,
        task_scope=payload.task_scope,
        alert_rule_name=payload.alert_rule_name,
        hits=payload.hits,
        mode=payload.mode,
        environment=payload.environment,
    )


@router.get("/api/incidents/active-plan-audit")
def active_plan_audit() -> dict[str, Any]:
    """页面核对：每个任务范围是否只有一个活动恢复方案。"""
    service = service_for("restore_plan")
    assert service is not None
    return service.active_plan_audit()


# ---------------------------------------------------------------------------
# 恢复方案 / 周期 / 回执
# ---------------------------------------------------------------------------

@router.get("/api/incidents/restore-plans")
def restore_plans_with_cycles() -> dict[str, Any]:
    service = service_for("restore_plan")
    assert service is not None
    plans = service.list_plans_with_cycles()
    return {
        "total": len(plans),
        "items": plans,
        "audit": service.active_plan_audit(),
        "build_pipeline": migration_switch.build_pipeline(),
        "active_version": store._migration.get("active_version"),
        "environment": store._migration.get("environment"),
    }


@router.get("/api/incidents/restore-plans/{plan_id}")
def restore_plan_detail(plan_id: int) -> dict[str, Any]:
    service = service_for("restore_plan")
    assert service is not None
    return service.plan_cycle_evidence(plan_id)


@router.post("/api/incidents/restore-plans/{plan_id}/consume-manifest")
def consume_manifest(plan_id: int, manifest_id: str) -> dict[str, Any]:
    service = service_for("restore_plan")
    assert service is not None
    plan = service.get_entry(plan_id)
    if plan is None:
        raise HTTPException(status_code=404, detail=f"恢复方案 {plan_id} 不存在")
    if str(plan.get("_manifest_id")) != manifest_id:
        raise HTTPException(status_code=409, detail="合并清单与恢复方案不匹配")
    return service.consume_merge_manifest(manifest_id)


@router.post("/api/incidents/restore-plans/{plan_id}/complete")
def complete_recovery(plan_id: int) -> dict[str, Any]:
    return incident_ledger.complete_recovery(plan_id)


@router.get("/api/incidents/receipts")
def receipts(plan_id: int | None = None) -> dict[str, Any]:
    rows = migration_switch.receipt_rows(plan_id)
    return {
        "total": len(rows),
        "items": rows,
        "versions": sorted({str(r.get("source_version")) for r in rows}),
    }


# ---------------------------------------------------------------------------
# 部署 / 迁移切换
# ---------------------------------------------------------------------------

@router.get("/api/migration/status")
def migration_status() -> dict[str, Any]:
    return migration_switch.migration_before_after()


@router.get("/api/migration/paths")
def migration_paths() -> dict[str, Any]:
    """两种切换路径的取舍说明。"""
    return {"paths": migration_switch.switch_path_tradeoffs()}


@router.get("/api/migration/build-pipeline")
def build_pipeline(deployment_id: str = "deploy_12") -> dict[str, Any]:
    """故障恢复详情面板：查看构建流水线与最近一次切换的阶段状态。"""
    return migration_switch.build_pipeline(deployment_id)


@router.get("/api/migration/trace")
def trace_call_chain(task_scope: str | None = None) -> dict[str, Any]:
    """顺着 CEP 规则调用链定位旧环境数据。"""
    return migration_switch.trace_cep_call_chain(task_scope)


@router.get("/api/migration/verify")
def verify_migration() -> dict[str, Any]:
    """在故障恢复数据流上核对迁移结果。"""
    return migration_switch.verify_restore_dataflow()


@router.get("/api/migration/ledger")
def migration_ledger() -> dict[str, Any]:
    rows = migration_switch.ledger_rows()
    return {"total": len(rows), "items": rows}


@router.post("/api/migration/switch")
def switch(payload: SwitchPayload) -> dict[str, Any]:
    switch_key = payload.switch_key
    if payload.rerun and switch_key:
        # 允许重新演练同一键：生成新的一次性键，但不影响原键的幂等结果。
        switch_key = f"{switch_key}#rerun-{len(store._migration.get('ledger', []))}"
    return migration_switch.switch_environment(
        environment=payload.environment,
        target_version=payload.target_version,
        switch_key=switch_key,
        path=payload.path,
        requested_by=payload.requested_by,
        simulate_failure=payload.simulate_failure,
    )
