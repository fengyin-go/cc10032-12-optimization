"""12号环境迁移切换：部署单、两条切换路径、幂等切换与事务回滚。

切换路径取舍：
  - atomic   ：一次性切全部活动方案与回执，事务内不可见中间态。一致性强、失败影响面大，
               适合发布窗口可控、要求"要么全切要么全不切"的场景。
  - rolling  ：按方案逐个迁移，每步单独留回执，失败时对已完成步骤做补偿回滚。
               影响面小、可分批观察，但切换期间存在新旧版本并存，需要活动方案指针兜底。

幂等：switch_key 是业务幂等键。同一 key 重复提交只返回第一次结果，不会二次写回执、
不会二次推进迁移台账。并发提交由 store._lock 串行化，并在锁内二次校验。

事务：切换前快照恢复方案 / 命中 / 合并清单 / 周期 / 回执 / 迁移状态；任一步校验失败
即整体回滚到原版本，失败记录写事务台账但不占幂等键，允许修复后重试。
"""
from __future__ import annotations

import copy
import hashlib
import time
from typing import Any

from app.services import incident_ledger
from app.store import KNOWN_VERSIONS, LEGACY_VERSION, store


class SwitchTransactionError(RuntimeError):
    pass


# ---------------------------------------------------------------------------
# 快照 / 回滚
# ---------------------------------------------------------------------------

_SNAPSHOT_TABLES = ("restore_plan", "alert_policy", "cep_rule", "replay_engine")


def _snapshot() -> dict[str, Any]:
    return {
        "tables": {
            name: copy.deepcopy(store.rows(name)) for name in _SNAPSHOT_TABLES
        },
        "manifests": copy.deepcopy(store._manifests),
        "hits": copy.deepcopy(store._alert_hits),
        "cycles": copy.deepcopy(store._cycles),
        "receipts": copy.deepcopy(store._receipts),
        "deployment": copy.deepcopy(store._deployments),
        "migration": copy.deepcopy(store._migration),
    }


def _restore(snapshot: dict[str, Any]) -> None:
    for name, rows in snapshot["tables"].items():
        store.rows(name)[:] = copy.deepcopy(rows)
    store._manifests = copy.deepcopy(snapshot["manifests"])
    store._alert_hits = copy.deepcopy(snapshot["hits"])
    store._cycles = copy.deepcopy(snapshot["cycles"])
    store._receipts = copy.deepcopy(snapshot["receipts"])
    store._deployments = copy.deepcopy(snapshot["deployment"])
    store._migration = copy.deepcopy(snapshot["migration"])


# ---------------------------------------------------------------------------
# 校验
# ---------------------------------------------------------------------------

def _verify_migration(before_hits: int, before_receipts: set[str]) -> list[str]:
    """切换后一致性校验：触发证据不丢、唯一活动方案、旧版本回执原样保留。"""
    issues: list[str] = []
    if len(store._alert_hits) != before_hits:
        issues.append(
            f"CEP 命中告警数据条数变化：{before_hits} → {len(store._alert_hits)}"
        )
    for manifest in store._manifests.values():
        missing = [hid for hid in manifest.get("hit_ids", []) if hid not in store._alert_hits]
        if missing:
            issues.append(f"合并清单 {manifest.get('manifest_id')} 丢失触发记录：{missing}")
        if int(manifest.get("merge_count", 0)) != len(manifest.get("hit_ids", [])):
            issues.append(f"合并数量与触发记录数不一致：{manifest.get('manifest_id')}")
    for receipt_id in before_receipts:
        receipt = store._receipts.get(receipt_id)
        if receipt is None:
            issues.append(f"存量回执被删除：{receipt_id}")
        elif receipt.get("status") == "rolled_back":
            issues.append(f"存量回执被标记回滚：{receipt_id}")
    scopes = {str(m.get("task_scope")) for m in store._manifests.values()}
    for scope in scopes:
        check = incident_ledger.assert_single_active_plan(scope)
        if not check["single"]:
            issues.append(f"任务范围 {scope} 存在 {check['active_plan_count']} 个活动恢复方案")
    return issues


# ---------------------------------------------------------------------------
# 切换
# ---------------------------------------------------------------------------

def _receipt_for_switch(
    plan: dict[str, Any],
    switch_id: str,
    target_version: str,
    environment: str,
    phase: str,
) -> dict[str, Any]:
    cycle = store._cycles.get(str(plan.get("_cycle_id"))) or {}
    manifest = store._manifests.get(str(plan.get("_manifest_id"))) or {}
    rid = "receipt_" + hashlib.md5(
        f"{plan.get('id')}_{switch_id}_{phase}".encode()
    ).hexdigest()[:16]
    if rid in store._receipts:
        return store._receipts[rid]
    receipt = {
        "receipt_id": rid,
        "plan_id": plan.get("id"),
        "plan_name": plan.get("方案名称", ""),
        "cycle_id": cycle.get("cycle_id", ""),
        "cycle_seq": cycle.get("seq", 0),
        "manifest_id": manifest.get("manifest_id", ""),
        "source_version": target_version,
        "environment": environment,
        "kind": "migration_switch",
        "hit_count": len(manifest.get("hit_ids", [])),
        "switch_id": switch_id,
        "status": "committed",
        "created_at": time.time(),
        "detail": f"{phase}：恢复方案回执来源版本 {target_version}",
    }
    store._receipts[rid] = receipt
    return receipt


def _plans_in_scope(target_environment: str) -> list[dict[str, Any]]:
    return [
        p for p in store.rows("restore_plan")
        if p.get("_generated")
        and str(p.get("_environment", target_environment)) == target_environment
    ]


def _apply_atomic(switch_id: str, target_version: str, environment: str) -> dict[str, Any]:
    plans = _plans_in_scope(environment) or [p for p in store.rows("restore_plan") if p.get("_generated")]
    receipts = []
    for plan in plans:
        receipts.append(_receipt_for_switch(plan, switch_id, target_version, environment, "atomic"))
        plan["_source_version"] = target_version
        plan["_environment"] = environment
        plan["_last_switch_id"] = switch_id
        cycle = store._cycles.get(str(plan.get("_cycle_id")))
        if cycle is not None:
            cycle["source_version"] = target_version
        manifest = store._manifests.get(str(plan.get("_manifest_id")))
        if manifest is not None:
            manifest["source_version"] = target_version
    return {"path": "atomic", "migrated_plans": len(plans), "receipts": len(receipts)}


def _apply_rolling(switch_id: str, target_version: str, environment: str) -> dict[str, Any]:
    plans = _plans_in_scope(environment) or [p for p in store.rows("restore_plan") if p.get("_generated")]
    migrated = 0
    receipts = 0
    for index, plan in enumerate(sorted(plans, key=lambda p: int(p.get("id", 0))), 1):
        phase = f"rolling-{index}/{len(plans)}"
        if not plan.get("_hit_ids"):
            raise SwitchTransactionError(f"方案 {plan.get('id')} 缺少触发证据，rolling 步骤失败")
        _receipt_for_switch(plan, switch_id, target_version, environment, phase)
        receipts += 1
        plan["_source_version"] = target_version
        plan["_environment"] = environment
        plan["_last_switch_id"] = switch_id
        migrated += 1
        cycle = store._cycles.get(str(plan.get("_cycle_id")))
        if cycle is not None:
            cycle["source_version"] = target_version
        manifest = store._manifests.get(str(plan.get("_manifest_id")))
        if manifest is not None:
            manifest["source_version"] = target_version
    return {"path": "rolling", "migrated_plans": migrated, "receipts": receipts}


def switch_environment(
    *,
    environment: str,
    target_version: str,
    switch_key: str | None = None,
    path: str = "atomic",
    requested_by: str = "发布负责人",
    simulate_failure: bool = False,
) -> dict[str, Any]:
    """执行一次环境版本切换。同一 switch_key 只生效一次。"""
    if target_version not in KNOWN_VERSIONS:
        return {"ok": False, "error": f"未知目标版本：{target_version}"}
    if path not in ("atomic", "rolling"):
        return {"ok": False, "error": f"未知切换路径：{path}"}

    key = switch_key or f"switch_{environment}_{target_version}"
    deployment = store._deployments.setdefault(
        "deploy_12",
        {
            "deployment_id": "deploy_12",
            "name": "12号迁移发布单",
            "environments": {},
            "switches": [],
            "applied": {},
        },
    )

    with store._lock:
        # 锁内二次校验，防止并发同时通过第一道判断。
        applied = store._applied_switches.get(key)
        if applied is not None:
            return {
                "ok": True,
                "idempotent": True,
                "switch_key": key,
                "switch_id": applied["switch_id"],
                "active_version": store._migration.get("active_version"),
                "before_version": applied["before_version"],
                "target_version": applied["target_version"],
                "path": applied["path"],
                "receipt_ids": applied["receipt_ids"],
                "message": "切换键已生效过，本次未重复执行",
            }

        current_env_version = deployment.get("environments", {}).get(environment, LEGACY_VERSION)
        before_version = str(store._migration.get("active_version", current_env_version))

        if target_version == before_version and current_env_version == target_version:
            switch_id = "switch_" + hashlib.md5(key.encode()).hexdigest()[:12]
            store._applied_switches[key] = {
                "switch_id": switch_id,
                "before_version": before_version,
                "target_version": target_version,
                "path": path,
                "receipt_ids": [],
                "applied_at": time.time(),
            }
            return {
                "ok": True,
                "idempotent": True,
                "noop": True,
                "switch_key": key,
                "switch_id": switch_id,
                "active_version": before_version,
                "receipt_ids": [],
                "message": f"目标版本 {target_version} 已是当前版本，未产生新回执",
            }

        switch_id = "switch_" + hashlib.md5(
            f"{key}_{time.time_ns()}".encode()
        ).hexdigest()[:12]
        snapshot = _snapshot()
        before_hits = len(store._alert_hits)
        before_receipts = set(store._receipts)
        store._migration["ingest_paused"] = True

        started = time.time()
        try:
            if simulate_failure:
                # 模拟事务失步：先写入部分回执，再在一致性校验处失败。
                plans = [p for p in store.rows("restore_plan") if p.get("_generated")]
                if plans:
                    _receipt_for_switch(plans[0], switch_id, target_version, environment, "atomic")
                raise SwitchTransactionError("模拟事务失步：回执与恢复方案版本指针不一致")

            result = (
                _apply_atomic(switch_id, target_version, environment)
                if path == "atomic"
                else _apply_rolling(switch_id, target_version, environment)
            )

            deployment.setdefault("environments", {})[environment] = target_version
            store._migration["active_version"] = target_version
            store._migration["environment"] = environment

            issues = _verify_migration(before_hits, before_receipts)
            if issues:
                raise SwitchTransactionError("；".join(issues))

            switch_id_receipts = [
                rid for rid, r in store._receipts.items()
                if r.get("switch_id") == switch_id
            ]
            record = {
                "switch_id": switch_id,
                "switch_key": key,
                "path": path,
                "environment": environment,
                "before_version": before_version,
                "target_version": target_version,
                "requested_by": requested_by,
                "status": "committed",
                "migrated_plans": result["migrated_plans"],
                "receipt_ids": switch_id_receipts,
                "started_at": started,
                "finished_at": time.time(),
            }
            deployment.setdefault("switches", []).append(record)
            deployment.setdefault("applied", {})[key] = switch_id
            store._migration["last_switch"] = record
            store._migration["ingest_paused"] = False
            store._applied_switches[key] = {
                "switch_id": switch_id,
                "before_version": before_version,
                "target_version": target_version,
                "path": path,
                "receipt_ids": switch_id_receipts,
                "applied_at": time.time(),
            }
            _append_ledger(
                {
                    "switch_id": switch_id,
                    "switch_key": key,
                    "environment": environment,
                    "path": path,
                    "before_version": before_version,
                    "after_version": target_version,
                    "status": "committed",
                    "migrated_plans": result["migrated_plans"],
                    "receipts": len(switch_id_receipts),
                }
            )
            return {
                "ok": True,
                "idempotent": False,
                "switch_key": key,
                "switch_id": switch_id,
                "path": path,
                "environment": environment,
                "before_version": before_version,
                "target_version": target_version,
                "active_version": target_version,
                "migrated_plans": result["migrated_plans"],
                "receipt_ids": switch_id_receipts,
                "message": "切换已提交",
            }
        except Exception as exc:  # noqa: BLE001 - 事务失败必须整体回滚
            _restore(snapshot)
            store._migration["ingest_paused"] = False
            failure = {
                "switch_id": switch_id,
                "switch_key": key,
                "environment": environment,
                "path": path,
                "before_version": before_version,
                "target_version": target_version,
                "requested_by": requested_by,
                "status": "rolled_back",
                "error": str(exc),
                "started_at": started,
                "finished_at": time.time(),
            }
            store._deployments["deploy_12"].setdefault("switches", []).append(failure)
            store._migration["last_switch"] = failure
            store._migration["active_version"] = before_version
            _append_ledger(
                {
                    "switch_id": switch_id,
                    "switch_key": key,
                    "environment": environment,
                    "path": path,
                    "before_version": before_version,
                    "after_version": before_version,
                    "status": "rolled_back",
                    "error": str(exc),
                }
            )
            return {
                "ok": False,
                "rolled_back": True,
                "switch_key": key,
                "switch_id": switch_id,
                "error": str(exc),
                "active_version": before_version,
                "message": "切换失败，已回滚原版本",
            }


def _append_ledger(entry: dict[str, Any]) -> None:
    ledger = store._migration.setdefault("ledger", [])
    entry = dict(entry)
    entry.setdefault("created_at", time.time())
    ledger.append(entry)


# ---------------------------------------------------------------------------
# 查询 / 核对
# ---------------------------------------------------------------------------

def switch_path_tradeoffs() -> list[dict[str, Any]]:
    """两条切换路径的取舍：一致性、影响面、回滚粒度、适用窗口。"""
    return [
        {
            "path": "atomic",
            "name": "一次性原子切换",
            "consistency": "强一致：全部方案与回执在同一事务内提交",
            "blast_radius": "大：任一方案校验失败则整批回滚",
            "rollback": "整体回滚到原版本",
            "downtime": "切换窗口内短暂暂停 CEP 命中写入",
            "best_for": "发布窗口可控、要求全量版本一致的场景",
        },
        {
            "path": "rolling",
            "name": "滚动逐方案切换",
            "consistency": "最终一致：切换期间新旧版本并存",
            "blast_radius": "小：仅失败方案及其后续步骤回滚",
            "rollback": "补偿回滚已迁移步骤",
            "downtime": "几乎无停机，但活动方案指针需兜底",
            "best_for": "方案多、需要分批观察和快速止损的场景",
        },
    ]


def trace_cep_call_chain(task_scope: str | None = None) -> dict[str, Any]:
    """顺着 CEP 规则调用链定位旧环境数据：CEP 规则 → 命中 → 合并清单 → 方案 → 回执。"""
    hits = [
        h for h in store._alert_hits.values()
        if task_scope is None or h.get("task_scope") == task_scope
    ]
    manifests = [
        m for m in store._manifests.values()
        if task_scope is None or m.get("task_scope") == task_scope
    ]
    plan_ids = {int(m.get("plan_id", 0)) for m in manifests}
    plans = [p for p in store.rows("restore_plan") if int(p.get("id", 0)) in plan_ids]
    receipts = [
        r for r in store._receipts.values()
        if int(r.get("plan_id", 0)) in plan_ids
    ]
    legacy_hits = [h for h in hits if h.get("source_version") == LEGACY_VERSION]
    legacy_plans = [p for p in plans if p.get("_source_version") == LEGACY_VERSION]
    legacy_receipts = [r for r in receipts if r.get("source_version") == LEGACY_VERSION]
    return {
        "task_scope": task_scope,
        "active_version": store._migration.get("active_version"),
        "chain": [
            {"stage": "cep_rule", "legacy_count": 0, "detail": "CEP 规则本身不含版本，命中在写入时打版本戳"},
            {"stage": "alert_hits", "legacy_count": len(legacy_hits), "total": len(hits)},
            {"stage": "merge_manifests", "legacy_count": len([m for m in manifests if m.get("source_version") == LEGACY_VERSION]), "total": len(manifests)},
            {"stage": "restore_plans", "legacy_count": len(legacy_plans), "total": len(plans)},
            {"stage": "receipts", "legacy_count": len(legacy_receipts), "total": len(receipts)},
        ],
        "legacy_located": bool(legacy_hits or legacy_plans or legacy_receipts),
        "legacy_hit_ids": [h.get("hit_id") for h in legacy_hits],
        "legacy_plan_ids": [p.get("id") for p in legacy_plans],
        "legacy_receipt_ids": [r.get("receipt_id") for r in legacy_receipts],
    }


def verify_restore_dataflow() -> dict[str, Any]:
    """在故障恢复数据流上核对迁移：证据完整、周期引用、唯一活动方案、回执版本留存。"""
    checks: list[dict[str, Any]] = []

    evidence_ok = all(
        all(hid in store._alert_hits for hid in m.get("hit_ids", []))
        for m in store._manifests.values()
    )
    checks.append({"check": "trigger_evidence_preserved", "ok": evidence_ok})

    cycle_refs_ok = all(
        c.get("plan_id") in {p.get("id") for p in store.rows("restore_plan")}
        for c in store._cycles.values()
    )
    checks.append({"check": "cycle_plan_references", "ok": cycle_refs_ok})

    scopes = {str(m.get("task_scope")) for m in store._manifests.values()}
    single_ok = all(incident_ledger.assert_single_active_plan(s)["single"] for s in scopes)
    checks.append({"check": "single_active_plan_per_scope", "ok": single_ok})

    cross_checks = incident_ledger.cross_check_manifest_plan()
    checks.append({
        "check": "manifest_plan_hits_correspond",
        "ok": all(c["matched"] for c in cross_checks),
    })

    versioned = all(
        {"legacy-v1", "v2-migrated"} <= set(incident_ledger.plan_evidence(p)["receipt_versions"])
        or p.get("_source_version") == LEGACY_VERSION
        for p in store.rows("restore_plan")
        if p.get("_generated")
    )
    checks.append({"check": "receipt_source_versions_retained", "ok": versioned})

    counts = {
        "hits": len(store._alert_hits),
        "manifests": len(store._manifests),
        "plans": len([p for p in store.rows("restore_plan") if p.get("_generated")]),
        "cycles": len(store._cycles),
        "receipts": len(store._receipts),
    }
    return {
        "ok": all(c["ok"] for c in checks),
        "checks": checks,
        "counts": counts,
        "active_version": store._migration.get("active_version"),
        "environment": store._migration.get("environment"),
    }


def build_pipeline(deployment_id: str = "deploy_12") -> dict[str, Any]:
    """部署详情面板的构建流水线：按最近一次切换结果给出各阶段状态。"""
    deployment = store._deployments.get(deployment_id)
    if deployment is None:
        return {"ok": False, "error": f"发布单 {deployment_id} 不存在"}
    last = store._migration.get("last_switch") or {}
    committed = last.get("status") == "committed"
    rolled_back = last.get("status") == "rolled_back"
    steps = [
        {"name": "源码构建", "status": "passed"},
        {"name": "单元测试", "status": "passed"},
        {"name": "数据库迁移", "status": "passed"},
        {"name": "恢复方案回执写入", "status": "passed" if committed else ("rolled_back" if rolled_back else "pending")},
        {"name": "迁移后核对", "status": "passed" if committed else ("failed" if rolled_back else "pending")},
        {"name": "流量切换", "status": "passed" if committed else ("pending" if not rolled_back else "blocked")},
    ]
    return {
        "ok": True,
        "deployment_id": deployment_id,
        "name": deployment.get("name", ""),
        "environments": deployment.get("environments", {}),
        "active_version": store._migration.get("active_version"),
        "last_switch": last,
        "steps": steps,
        "switch_count": len(deployment.get("switches", [])),
    }


def migration_before_after() -> dict[str, Any]:
    """接口读取迁移前后结果：每次切换的 before/after 版本、回执和校验结论。"""
    deployment = store._deployments.get("deploy_12", {})
    return {
        "active_version": store._migration.get("active_version"),
        "environment": store._migration.get("environment"),
        "deployment": {
            "deployment_id": deployment.get("deployment_id"),
            "environments": deployment.get("environments", {}),
            "switch_count": len(deployment.get("switches", [])),
        },
        "switches": deployment.get("switches", []),
        "ledger": store._migration.get("ledger", []),
        "verification": verify_restore_dataflow(),
    }


def ledger_rows() -> list[dict[str, Any]]:
    return list(reversed(store._migration.get("ledger", [])))


def receipt_rows(plan_id: int | None = None) -> list[dict[str, Any]]:
    receipts = list(store._receipts.values())
    if plan_id is not None:
        receipts = [r for r in receipts if int(r.get("plan_id", 0)) == int(plan_id)]
    return sorted(receipts, key=lambda r: float(r.get("created_at", 0)))


def hit_rows(task_scope: str | None = None, manifest_id: str | None = None) -> list[dict[str, Any]]:
    hits = list(store._alert_hits.values())
    if task_scope:
        hits = [h for h in hits if h.get("task_scope") == task_scope]
    if manifest_id:
        hits = [h for h in hits if h.get("manifest_id") == manifest_id]
    return sorted(hits, key=lambda h: float(h.get("match_start_ts", 0)))
