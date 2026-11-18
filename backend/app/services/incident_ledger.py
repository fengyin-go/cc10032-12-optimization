"""12号同源告警合并与恢复周期台账。

数据流: cep_rule（CEP 命中）→ alert_policy（同源合并清单）→ restore_plan（唯一活动方案）
                                    ↘ incident_ledger（周期 / 回执 / 触发证据）

核心约束：
  1. 一次 CEP 命中就是一条触发记录，合并后仍逐条保留，不删除、不覆盖。
  2. 同一 source_key（CEP 规则 + 任务范围）只允许挂在一个当前恢复方案上。
  3. 同一任务范围最终只有一个活动恢复方案；周期关闭后才允许开启新周期。
  4. 回执按来源版本留存：迁移切换不会覆盖旧版本回执，只追加新版本回执。
  5. 命中写入与周期切换都按业务键幂等，重复提交不产生第二条记录。
"""
from __future__ import annotations

import hashlib
import time
from typing import Any

from app.store import store


def _next_plan_id() -> int:
    return max((int(r.get("id", 0)) for r in store.rows("restore_plan")), default=0) + 1


def _next_cycle_seq(source_key: str) -> int:
    seqs = [
        int(c.get("seq", 0))
        for c in store._cycles.values()
        if c.get("source_key") == source_key
    ]
    return max(seqs, default=0) + 1


def source_key_of(cep_rule: str, task_scope: str) -> str:
    """同源判定键：同一条 CEP 规则 + 同一个任务范围视为同一来源。"""
    return f"{cep_rule}::{task_scope}"


def active_version() -> str:
    return str(store._migration.get("active_version", "legacy-v1"))


def current_environment() -> str:
    return str(store._migration.get("environment", "dev"))


def active_plans_for_scope(scope: str) -> list[dict[str, Any]]:
    """当前活动方案：该范围内未关闭的恢复方案。"""
    return [
        p for p in store.rows("restore_plan")
        if p.get("任务范围") == scope
        and p.get("_generated")
        and not p.get("_closed_at")
        and p.get("status") != "已归档"
    ]


def current_cycle_for(source_key: str) -> dict[str, Any] | None:
    for cycle in store._cycles.values():
        if cycle.get("source_key") == source_key and cycle.get("status") == "current":
            return cycle
    return None


def current_cycle_for_scope(task_scope: str) -> dict[str, Any] | None:
    """兜底：同一任务范围只允许一个当前周期，即使 CEP 规则名不同。"""
    for cycle in store._cycles.values():
        if cycle.get("task_scope") == task_scope and cycle.get("status") == "current":
            return cycle
    return None


def current_manifest_for(source_key: str) -> dict[str, Any] | None:
    cycle = current_cycle_for(source_key)
    if cycle is None:
        return None
    return store._manifests.get(str(cycle.get("manifest_id")))


def _plan_completed(plan: dict[str, Any]) -> bool:
    return bool(plan.get("_recovery_completed_at")) or plan.get("status") == "已生效"


def _append_receipt(
    *,
    plan: dict[str, Any],
    cycle: dict[str, Any],
    manifest: dict[str, Any],
    kind: str,
    detail: str,
    source_version: str | None = None,
    switch_id: str = "",
    status: str = "committed",
    receipt_id: str | None = None,
) -> dict[str, Any]:
    version = source_version or str(plan.get("_source_version") or active_version())
    rid = receipt_id or hashlib.md5(
        f"{plan.get('id')}_{cycle.get('cycle_id')}_{kind}_{version}_{switch_id}_{time.time_ns()}".encode()
    ).hexdigest()[:16]
    receipt = {
        "receipt_id": rid,
        "plan_id": plan.get("id"),
        "plan_name": plan.get("方案名称", ""),
        "cycle_id": cycle.get("cycle_id", ""),
        "cycle_seq": cycle.get("seq", 0),
        "manifest_id": manifest.get("manifest_id", ""),
        "source_version": version,
        "environment": current_environment(),
        "kind": kind,
        "hit_count": len(manifest.get("hit_ids", [])),
        "switch_id": switch_id,
        "status": status,
        "created_at": time.time(),
        "detail": detail,
    }
    store._receipts[rid] = receipt
    return receipt


def _new_cycle_and_plan(
    *,
    source_key: str,
    cep_rule: str,
    task_scope: str,
    alert_rule_name: str,
    first_hit_ts: float,
) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
    seq = _next_cycle_seq(source_key)
    cycle_id = f"cycle_{hashlib.md5(f'{source_key}_{seq}'.encode()).hexdigest()[:8]}_{seq}"
    manifest_id = f"manifest_{hashlib.md5(f'{source_key}_{seq}'.encode()).hexdigest()[:8]}_{seq}"
    plan_id = _next_plan_id()
    trigger_count = 1
    plan = {
        "id": plan_id,
        "方案名称": f"自动恢复_{task_scope}_c{seq}",
        "任务范围": task_scope,
        "恢复模式": "restart",
        "源位点策略": "latest_snapshot",
        "状态复用": "reuse_existing",
        "跳过坏数据": "false",
        "验证步骤": "state_consistency,event_replay",
        "status": "草稿",
        "pending": True,
        "abnormal": False,
        "_generated": True,
        "_active": True,
        "_source_key": source_key,
        "_source_version": active_version(),
        "_cep_rule": cep_rule,
        "_alert_rule": alert_rule_name,
        "_manifest_id": manifest_id,
        "_cycle_id": cycle_id,
        "_cycle_seq": seq,
        "_hit_ids": [],
        "_trigger_count": 0,
        "_merged_count": 0,
        "_first_hit_ts": first_hit_ts,
        "_last_hit_ts": first_hit_ts,
        "_created_at": time.time(),
    }
    store.rows("restore_plan").append(plan)

    cycle = {
        "cycle_id": cycle_id,
        "source_key": source_key,
        "task_scope": task_scope,
        "seq": seq,
        "plan_id": plan_id,
        "plan_name": plan["方案名称"],
        "manifest_id": manifest_id,
        "hit_ids": [],
        "status": "current",
        "source_version": plan["_source_version"],
        "environment": current_environment(),
        "opened_at": first_hit_ts,
        "closed_at": None,
        "close_reason": "",
    }
    store._cycles[cycle_id] = cycle

    manifest = {
        "manifest_id": manifest_id,
        "source_key": source_key,
        "cep_rule": cep_rule,
        "task_scope": task_scope,
        "alert_rule_name": alert_rule_name,
        "plan_id": plan_id,
        "plan_name": plan["方案名称"],
        "cycle_id": cycle_id,
        "cycle_seq": seq,
        "hit_ids": [],
        "merge_count": 0,
        "status": "merged",
        "source_version": plan["_source_version"],
        "environment": current_environment(),
        "first_hit_ts": first_hit_ts,
        "last_hit_ts": first_hit_ts,
        "merged_at": first_hit_ts,
    }
    store._manifests[manifest_id] = manifest
    return plan, cycle, manifest


def _close_cycle(cycle: dict[str, Any], reason: str) -> None:
    cycle["status"] = "closed"
    cycle["closed_at"] = time.time()
    cycle["close_reason"] = reason
    plan = store.find("restore_plan", int(cycle.get("plan_id", 0)))
    if plan is not None:
        plan["_active"] = False
        plan["_closed_at"] = cycle["closed_at"]
        plan["_close_reason"] = reason


def record_cep_hit(
    *,
    cep_rule: str,
    task_scope: str,
    alert_rule_name: str,
    match_start_ts: float,
    match_end_ts: float,
    event_count: int,
    pattern: str = "",
    environment: str | None = None,
    mode: str = "auto",
    hit_id: str | None = None,
) -> dict[str, Any]:
    """记录一次 CEP 命中并按同源规则合并到当前恢复方案。

    mode:
      - auto       当前周期未完成则合并；已完成则关闭上一周期、开启新方案
      - extend     始终合并到当前方案延长处理（即使已恢复完成）
      - new_cycle  当前周期存在时强制关闭并开启新周期
    """
    if store._migration.get("ingest_paused"):
        return {
            "ok": False,
            "error": "迁移切换进行中，CEP 命中暂存等待事务提交",
            "retryable": True,
        }
    if not cep_rule or not task_scope:
        return {"ok": False, "error": "CEP 规则与任务范围不能为空"}

    source_key = source_key_of(cep_rule, task_scope)
    stable_hit_id = hit_id or "hit_" + hashlib.md5(
        f"{source_key}_{match_start_ts}_{match_end_ts}_{event_count}_{pattern}".encode()
    ).hexdigest()[:12]

    existing_hit = store._alert_hits.get(stable_hit_id)
    if existing_hit is not None:
        manifest = store._manifests.get(str(existing_hit.get("manifest_id")))
        plan = store.find("restore_plan", int((manifest or {}).get("plan_id", 0)))
        return {
            "ok": True,
            "duplicate": True,
            "hit_id": stable_hit_id,
            "manifest_id": (manifest or {}).get("manifest_id"),
            "plan_id": (plan or {}).get("id"),
            "cycle_id": (manifest or {}).get("cycle_id"),
            "merge_count": (manifest or {}).get("merge_count", 0),
            "active_plan_count": len(active_plans_for_scope(task_scope)),
        }

    env = environment or current_environment()
    version = active_version()
    rotated = False
    reopened = False

    cycle = current_cycle_for(source_key) or current_cycle_for_scope(task_scope)
    plan = store.find("restore_plan", int((cycle or {}).get("plan_id", 0)))
    manifest = current_manifest_for(source_key)
    if manifest is None and cycle is not None:
        manifest = store._manifests.get(str(cycle.get("manifest_id")))

    if cycle is not None and plan is not None and manifest is not None:
        # 同源判定以实际周期为准，避免 CEP 规则别名产生第二条 current 周期。
        source_key = str(cycle.get("source_key", source_key))
        completed = _plan_completed(plan)
        must_rotate = mode == "new_cycle" or (mode == "auto" and completed)
        if must_rotate:
            _close_cycle(cycle, "恢复完成后重新触发，开启新周期")
            plan, cycle, manifest = _new_cycle_and_plan(
                source_key=source_key,
                cep_rule=cep_rule,
                task_scope=task_scope,
                alert_rule_name=alert_rule_name,
                first_hit_ts=match_start_ts,
            )
            rotated = True
        elif completed and mode == "extend":
            plan["status"] = "草稿"
            plan["pending"] = True
            plan["_recovery_completed_at"] = None
            reopened = True

    if cycle is None or plan is None or manifest is None:
        plan, cycle, manifest = _new_cycle_and_plan(
            source_key=source_key,
            cep_rule=cep_rule,
            task_scope=task_scope,
            alert_rule_name=alert_rule_name,
            first_hit_ts=match_start_ts,
        )

    hit = {
        "hit_id": stable_hit_id,
        "source_key": source_key,
        "cep_rule": cep_rule,
        "task_scope": task_scope,
        "alert_rule_name": alert_rule_name,
        "match_start_ts": match_start_ts,
        "match_end_ts": match_end_ts,
        "event_count": event_count,
        "pattern": pattern,
        "source_version": version,
        "environment": env,
        "status": "merged",
        "manifest_id": manifest["manifest_id"],
        "recorded_at": time.time(),
    }
    store._alert_hits[stable_hit_id] = hit

    manifest["hit_ids"].append(stable_hit_id)
    manifest["merge_count"] = len(manifest["hit_ids"])
    manifest["last_hit_ts"] = max(float(manifest.get("last_hit_ts", 0)), match_end_ts)
    manifest["merged_at"] = time.time()
    manifest["status"] = "merged"
    cycle["hit_ids"].append(stable_hit_id)

    plan["_hit_ids"].append(stable_hit_id)
    plan["_trigger_count"] = int(plan.get("_trigger_count", 0)) + 1
    plan["_merged_count"] = len(plan["_hit_ids"])
    plan["_last_hit_ts"] = max(float(plan.get("_last_hit_ts", 0)), match_end_ts)
    if plan["_trigger_count"] > 5:
        plan["跳过坏数据"] = "true"
        plan["恢复模式"] = "escalation_response"
    elif plan["_trigger_count"] > 3:
        plan["恢复模式"] = "from_checkpoint"

    # 同一任务范围只能有一个活动方案：句柄外的历史方案一律关闭。
    for other in active_plans_for_scope(task_scope):
        if int(other.get("id", 0)) != int(plan.get("id", 0)):
            other["_active"] = False
            other["_closed_at"] = time.time()
            other["_close_reason"] = "同范围已合并到当前方案"

    receipt = _append_receipt(
        plan=plan,
        cycle=cycle,
        manifest=manifest,
        kind="alert_merged",
        detail=(
            f"同源命中合并：累计 {manifest['merge_count']} 条触发记录"
            + ("（关闭上一周期后开启新周期）" if rotated else "")
            + ("（恢复完成后延长当前方案）" if reopened else "")
        ),
        source_version=version,
    )

    return {
        "ok": True,
        "duplicate": False,
        "hit_id": stable_hit_id,
        "manifest_id": manifest["manifest_id"],
        "manifest": manifest,
        "plan_id": plan["id"],
        "plan_name": plan["方案名称"],
        "cycle_id": cycle["cycle_id"],
        "cycle_seq": cycle["seq"],
        "merge_count": manifest["merge_count"],
        "trigger_count": plan["_trigger_count"],
        "rotated": rotated,
        "reopened": reopened,
        "receipt_id": receipt["receipt_id"],
        "active_plan_count": len(active_plans_for_scope(task_scope)),
    }


def record_cep_hits_from_matches(
    *,
    cep_rule: str,
    task_scope: str,
    alert_rule_name: str,
    matches: list[Any],
    mode: str = "auto",
) -> dict[str, Any]:
    """把 CEP 匹配结果逐条落为触发记录并合并。

    兼容两种匹配结构：pipeline 的 {start_ts, end_ts, events} 和
    cep_rule 服务直接给出的 window 事件列表。
    """
    results = []
    for match in matches:
        if isinstance(match, dict):
            events = match.get("events") or []
            start_ts = float(match.get("start_ts", 0))
            end_ts = float(match.get("end_ts", start_ts))
        else:
            events = list(match) if match else []
            times = [float(e.get("event_time", 0)) for e in events] or [0.0]
            start_ts, end_ts = min(times), max(times)
        results.append(
            record_cep_hit(
                cep_rule=cep_rule,
                task_scope=task_scope,
                alert_rule_name=alert_rule_name,
                match_start_ts=start_ts,
                match_end_ts=end_ts,
                event_count=len(events),
                pattern=",".join(str(e.get("event_type", "")) for e in events),
                mode=mode,
            )
        )
    ok = [r for r in results if r.get("ok")]
    first = ok[0] if ok else {}
    return {
        "ingested": len(ok),
        "duplicates": sum(1 for r in ok if r.get("duplicate")),
        "manifest_id": first.get("manifest_id"),
        "plan_id": first.get("plan_id"),
        "cycle_id": first.get("cycle_id"),
        "merge_count": first.get("merge_count"),
        "rotated": any(r.get("rotated") for r in ok),
        "reopened": any(r.get("reopened") for r in ok),
        "errors": [r.get("error") for r in results if not r.get("ok")],
    }


def complete_recovery(plan_id: int) -> dict[str, Any]:
    """恢复完成：方案转已生效，周期保留并等待下一次触发判断。"""
    plan = store.find("restore_plan", int(plan_id))
    if plan is None:
        return {"ok": False, "error": f"恢复方案 {plan_id} 不存在"}
    cycle = store._cycles.get(str(plan.get("_cycle_id")))
    manifest = store._manifests.get(str(plan.get("_manifest_id")))
    plan["status"] = "已生效"
    plan["pending"] = False
    plan["_recovery_completed_at"] = time.time()
    receipt = None
    if cycle and manifest:
        cycle["recovered_at"] = plan["_recovery_completed_at"]
        receipt = _append_receipt(
            plan=plan,
            cycle=cycle,
            manifest=manifest,
            kind="recovery_completed",
            detail="恢复完成，周期进入可重新触发状态",
        )
    return {
        "ok": True,
        "plan_id": plan["id"],
        "plan_name": plan.get("方案名称", ""),
        "cycle_id": plan.get("_cycle_id", ""),
        "cycle_seq": plan.get("_cycle_seq", 0),
        "hit_count": len(plan.get("_hit_ids", [])),
        "receipt_id": (receipt or {}).get("receipt_id", ""),
    }


def manifest_rows() -> list[dict[str, Any]]:
    """告警合并清单：一行一个同源告警组，带全部触发证据。"""
    rows: list[dict[str, Any]] = []
    for manifest in store._manifests.values():
        hits = [
            store._alert_hits[hid]
            for hid in manifest.get("hit_ids", [])
            if hid in store._alert_hits
        ]
        cycle = store._cycles.get(str(manifest.get("cycle_id")))
        plan = store.find("restore_plan", int(manifest.get("plan_id", 0)))
        rows.append({
            "manifest_id": manifest.get("manifest_id", ""),
            "source_key": manifest.get("source_key", ""),
            "cep_rule": manifest.get("cep_rule", ""),
            "task_scope": manifest.get("task_scope", ""),
            "plan_id": manifest.get("plan_id"),
            "plan_name": manifest.get("plan_name", ""),
            "cycle_id": manifest.get("cycle_id", ""),
            "cycle_seq": manifest.get("cycle_seq", 0),
            "cycle_status": (cycle or {}).get("status", ""),
            "merge_count": manifest.get("merge_count", 0),
            "hit_count_evidence": len(hits),
            "status": manifest.get("status", ""),
            "source_version": manifest.get("source_version", ""),
            "environment": manifest.get("environment", ""),
            "first_hit_ts": manifest.get("first_hit_ts", 0),
            "last_hit_ts": manifest.get("last_hit_ts", 0),
            "is_current_plan": bool(plan and not plan.get("_closed_at")),
            "plan_status": (plan or {}).get("status", ""),
            "trigger_evidence": [
                {
                    "hit_id": h.get("hit_id"),
                    "match_start_ts": h.get("match_start_ts"),
                    "match_end_ts": h.get("match_end_ts"),
                    "event_count": h.get("event_count"),
                    "pattern": h.get("pattern"),
                    "source_version": h.get("source_version"),
                    "environment": h.get("environment"),
                }
                for h in sorted(hits, key=lambda x: float(x.get("match_start_ts", 0)))
            ],
        })
    rows.sort(key=lambda r: (r["task_scope"], r["cycle_seq"]))
    return rows


def plan_evidence(plan: dict[str, Any]) -> dict[str, Any]:
    """单个恢复方案对应的周期与触发证据，供恢复页面确认。"""
    cycle = store._cycles.get(str(plan.get("_cycle_id")))
    manifest = store._manifests.get(str(plan.get("_manifest_id")))
    hits = [
        store._alert_hits[hid]
        for hid in plan.get("_hit_ids", [])
        if hid in store._alert_hits
    ]
    receipts = [
        r for r in store._receipts.values()
        if int(r.get("plan_id", 0)) == int(plan.get("id", 0))
    ]
    return {
        "plan_id": plan.get("id"),
        "plan_name": plan.get("方案名称", ""),
        "task_scope": plan.get("任务范围", ""),
        "plan_status": plan.get("status", ""),
        "is_active": not plan.get("_closed_at"),
        "cycle": {
            "cycle_id": (cycle or {}).get("cycle_id", ""),
            "seq": (cycle or {}).get("seq", 0),
            "status": (cycle or {}).get("status", ""),
            "opened_at": (cycle or {}).get("opened_at"),
            "closed_at": (cycle or {}).get("closed_at"),
            "close_reason": (cycle or {}).get("close_reason", ""),
        },
        "manifest_id": (manifest or {}).get("manifest_id", ""),
        "merge_count": (manifest or {}).get("merge_count", len(hits)),
        "trigger_evidence": [
            {
                "hit_id": h.get("hit_id"),
                "match_start_ts": h.get("match_start_ts"),
                "match_end_ts": h.get("match_end_ts"),
                "event_count": h.get("event_count"),
                "pattern": h.get("pattern"),
                "source_version": h.get("source_version"),
                "environment": h.get("environment"),
            }
            for h in sorted(hits, key=lambda x: float(x.get("match_start_ts", 0)))
        ],
        "receipts": [
            {
                "receipt_id": r.get("receipt_id"),
                "kind": r.get("kind"),
                "source_version": r.get("source_version"),
                "switch_id": r.get("switch_id", ""),
                "status": r.get("status", ""),
                "created_at": r.get("created_at"),
            }
            for r in sorted(receipts, key=lambda x: float(x.get("created_at", 0)))
        ],
        "receipt_versions": sorted({str(r.get("source_version")) for r in receipts}),
    }


def assert_single_active_plan(scope: str) -> dict[str, Any]:
    active = active_plans_for_scope(scope)
    return {
        "scope": scope,
        "active_plan_count": len(active),
        "active_plan_ids": [p.get("id") for p in active],
        "single": len(active) == 1,
    }


def cross_check_manifest_plan(scope: str | None = None) -> list[dict[str, Any]]:
    """核对告警合并清单与唯一活动方案中的 CEP 命中数据是否一一对应。"""
    results: list[dict[str, Any]] = []
    for manifest in store._manifests.values():
        if scope and manifest.get("task_scope") != scope:
            continue
        plan = store.find("restore_plan", int(manifest.get("plan_id", 0)))
        if plan is None:
            results.append({
                "manifest_id": manifest.get("manifest_id"),
                "task_scope": manifest.get("task_scope"),
                "matched": False,
                "reason": "合并清单指向的恢复方案不存在",
            })
            continue
        manifest_hits = list(manifest.get("hit_ids", []))
        plan_hits = [hid for hid in plan.get("_hit_ids", []) if hid in store._alert_hits]
        missing_in_plan = sorted(set(manifest_hits) - set(plan_hits))
        missing_in_manifest = sorted(set(plan_hits) - set(manifest_hits))
        cycle = store._cycles.get(str(manifest.get("cycle_id"))) or {}
        is_current_cycle = cycle.get("status") == "current"
        active = active_plans_for_scope(str(manifest.get("task_scope", "")))
        if is_current_cycle:
            # 当前周期：清单必须挂在该范围内唯一的活动方案上。
            placement_ok = (
                len(active) == 1
                and int(active[0].get("id", 0)) == int(plan.get("id", 0))
                and not plan.get("_closed_at")
            )
            placement_rule = "当前周期清单必须对应唯一活动方案"
        else:
            # 历史周期：方案必须已关闭且不在活动位上，触发记录仍然完整。
            placement_ok = bool(plan.get("_closed_at")) and all(
                int(p.get("id", 0)) != int(plan.get("id", 0)) for p in active
            )
            placement_rule = "历史周期方案必须已关闭且让出活动位"
        results.append({
            "manifest_id": manifest.get("manifest_id"),
            "task_scope": manifest.get("task_scope"),
            "plan_id": plan.get("id"),
            "plan_name": plan.get("方案名称", ""),
            "cycle_status": cycle.get("status", ""),
            "manifest_hit_count": len(manifest_hits),
            "plan_hit_count": len(plan_hits),
            "missing_in_plan": missing_in_plan,
            "missing_in_manifest": missing_in_manifest,
            "placement_ok": placement_ok,
            "placement_rule": placement_rule,
            "plan_is_unique_active": is_current_cycle and placement_ok,
            "matched": not missing_in_plan and not missing_in_manifest and placement_ok,
        })
    return results
