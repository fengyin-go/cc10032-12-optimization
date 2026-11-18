"""故障恢复领域服务：定义流式任务的故障恢复方案。

数据流角色: checkpoint_mgr / alert_policy → restore_plan（消费者）→ replay_engine（消费者）
增强能力: 恢复方案演练/启用、恢复模式选择、位点策略管理、状态复用验证、恢复后校验
12号能力: 消费告警合并清单，维护当前方案与历史周期的引用，保留恢复方案回执的来源版本
"""
from __future__ import annotations

import time
from typing import Any

from app.services import incident_ledger
from app.services.base import BaseModuleService
from app.store import store


class RestorePlanService(BaseModuleService):
    MODULE = "restore_plan"
    ENTITY = "恢复方案"
    LIST_FIELDS = ["方案名称", "任务范围", "恢复模式", "源位点策略", "状态复用", "跳过坏数据", "验证步骤"]
    REQUIRED_FIELDS = ["方案名称", "任务范围", "恢复模式"]
    STATUS_ORDER = ["草稿", "已演练", "已生效"]
    ACTION_RULES = {"演练方案": "草稿", "启用方案": "已演练"}
    NEGATIVE_ACTIONS = []

    RESTORE_MODES = {"from_checkpoint", "restart", "escalation_response", "manual"}

    # ---- 12号：告警合并清单 → 唯一活动恢复方案 ----

    def consume_merge_manifest(self, manifest_id: str) -> dict[str, Any]:
        """消费告警合并清单，挂到当前方案并核对周期引用。"""
        manifest = store._manifests.get(manifest_id)
        if manifest is None:
            return {"ok": False, "error": f"告警合并清单「{manifest_id}」不存在"}
        plan = store.find("restore_plan", int(manifest.get("plan_id", 0)))
        if plan is None:
            return {"ok": False, "error": f"合并清单 {manifest_id} 指向的恢复方案不存在"}
        cycle = store._cycles.get(str(plan.get("_cycle_id")))
        if cycle is None:
            return {"ok": False, "error": f"恢复方案 {plan.get('id')} 缺少恢复周期引用"}
        plan["_manifest_id"] = manifest_id
        plan["_cycle_id"] = cycle["cycle_id"]
        plan["_cycle_seq"] = cycle.get("seq", 0)
        active = incident_ledger.active_plans_for_scope(str(plan.get("任务范围", "")))
        return {
            "ok": True,
            "manifest_id": manifest_id,
            "merge_count": manifest.get("merge_count", 0),
            "hit_ids": list(manifest.get("hit_ids", [])),
            "plan_id": plan.get("id"),
            "plan_name": plan.get("方案名称", ""),
            "cycle": {
                "cycle_id": cycle["cycle_id"],
                "seq": cycle.get("seq", 0),
                "status": cycle.get("status", ""),
            },
            "active_plan_count": len(active),
            "single_active_plan": len(active) == 1,
        }

    def list_plans_with_cycles(self) -> list[dict[str, Any]]:
        """恢复页面：每个活动方案对应的周期、合并数量与触发证据。

        只列出 12号同源合并生成的方案；手工登记的示例方案没有周期，不参与核对。
        """
        plans = [p for p in store.rows(self.MODULE) if p.get("_generated")]
        return [incident_ledger.plan_evidence(p) for p in plans]

    def plan_cycle_evidence(self, plan_id: int) -> dict[str, Any]:
        plan = store.find(self.MODULE, int(plan_id))
        if plan is None:
            return {"ok": False, "error": f"恢复方案 {plan_id} 不存在"}
        evidence = incident_ledger.plan_evidence(plan)
        evidence["ok"] = True
        return evidence

    def active_plan_audit(self) -> dict[str, Any]:
        """核对每个任务范围是否只有一个活动方案，以及清单与方案命中是否对应。"""
        scopes = {
            str(p.get("任务范围", ""))
            for p in store.rows(self.MODULE)
            if p.get("_generated")
        }
        audits = [incident_ledger.assert_single_active_plan(s) for s in sorted(scopes)]
        cross_checks = incident_ledger.cross_check_manifest_plan()
        return {
            "ok": all(a["single"] for a in audits)
            and all(c["matched"] for c in cross_checks),
            "scopes": audits,
            "cross_checks": cross_checks,
        }

    def recalculate_escalation(self, plan_id: int) -> dict[str, Any]:
        """按累计触发次数重算恢复模式：连续告警升级但不新增方案。"""
        plan = store.find(self.MODULE, int(plan_id))
        if plan is None:
            return {"ok": False, "error": f"恢复方案 {plan_id} 不存在"}
        scope = str(plan.get("任务范围", ""))
        count = len(plan.get("_hit_ids", []))
        decision = self.select_optimal_recovery_mode(scope, count)
        plan["恢复模式"] = decision["mode"]
        plan["_trigger_count"] = count
        plan["_merged_count"] = count
        return {"ok": True, "plan_id": plan_id, "trigger_count": count, **decision}

    def complete_plan_recovery(self, plan_id: int) -> dict[str, Any]:
        """恢复完成：写入周期完成标记，保留方案与全部触发记录。"""
        return incident_ledger.complete_recovery(plan_id)

    def run_action(self, entry_id: int, action: str) -> tuple[dict[str, Any] | None, str]:
        """12号：同源合并生成的方案，状态流转要同步周期，不能只改行状态。"""
        entry = store.find(self.MODULE, entry_id)
        if entry is None:
            return None, f"{self.ENTITY} {entry_id} 不存在或已归档"

        if entry.get("_generated"):
            if entry.get("_closed_at") and action in ("演练方案", "启用方案"):
                return None, (
                    f"方案 {entry_id} 属于已关闭周期 #{entry.get('_cycle_seq')}，"
                    "请在当前活动方案上操作"
                )
            if action == "启用方案":
                result = incident_ledger.complete_recovery(entry_id)
                if not result.get("ok"):
                    return None, str(result.get("error", "启用失败"))
                message = (
                    f"恢复方案已启用，周期 #{result.get('cycle_seq')} 保留 "
                    f"{result.get('hit_count')} 条触发记录"
                )
                return entry, message
            if action == "演练方案":
                entry["status"] = "已演练"
                entry["pending"] = True
                return entry, "恢复方案已演练，周期与触发证据保持不变"
            return None, f"动作「{action}」不属于同源合并生成的恢复方案"

        return super().run_action(entry_id, action)

    # ---- 跨模块数据管理 ----

    def dry_run_restore(self, plan_name: str) -> dict[str, Any]:
        """演练恢复方案：模拟执行恢复流程，验证各步骤可行性。"""
        plan = None
        for r in store.rows(self.MODULE):
            if r.get("方案名称") == plan_name:
                plan = r
                break
        if plan is None:
            return {"ok": False, "error": f"恢复方案「{plan_name}」不存在"}

        mode = plan.get("恢复模式", "restart")
        scope = plan.get("任务范围", "")
        offset_strategy = plan.get("源位点策略", "latest_snapshot")
        skip_bad = plan.get("跳过坏数据", "false")
        state_reuse = plan.get("状态复用", "")

        steps = []
        # Step 1: 检查位点策略
        if offset_strategy == "latest_snapshot":
            cp_available = sum(
                1 for r in store.rows("checkpoint_mgr")
                if r.get("checkpoint_id") and r.get("status") == "已启用"
            )
            steps.append({
                "step": "checkpoint_check",
                "ok": cp_available > 0,
                "detail": f"可用检查点: {cp_available}",
            })
        else:
            steps.append({
                "step": "offset_strategy",
                "ok": True,
                "detail": f"位点策略: {offset_strategy}",
            })

        # Step 2: 状态复用校验
        if state_reuse:
            has_state = any(
                scope in str(r.get("配置名称", ""))
                for r in store.rows("state_store")
            )
            steps.append({
                "step": "state_reuse_check",
                "ok": has_state,
                "detail": f"状态复用: {state_reuse}, 可用: {has_state}",
            })
        else:
            steps.append({
                "step": "state_reuse",
                "ok": True,
                "detail": "无状态复用需求",
            })

        # Step 3: 恢复模式
        mode_valid = mode in self.RESTORE_MODES
        steps.append({
            "step": "restore_mode",
            "ok": mode_valid,
            "detail": f"恢复模式: {mode}",
        })

        # Step 4: 跳过坏数据处理
        steps.append({
            "step": "skip_bad_data",
            "ok": True,
            "detail": f"跳过坏数据: {skip_bad}",
        })

        # Step 5: 验证步骤解析
        verify_steps = [
            s.strip()
            for s in plan.get("验证步骤", "").split(",")
            if s.strip()
        ]
        steps.append({
            "step": "verification_steps",
            "ok": len(verify_steps) > 0,
            "detail": f"验证步骤: {verify_steps}",
        })

        all_ok = all(s["ok"] for s in steps)
        if all_ok:
            plan["status"] = "已演练"
            plan["pending"] = True

        return {
            "ok": all_ok,
            "plan": plan_name,
            "mode": mode,
            "steps": steps,
            "all_steps_passed": all_ok,
        }

    def select_optimal_recovery_mode(
        self, scope: str, trigger_count: int,
    ) -> dict[str, Any]:
        """根据触发次数和范围选择最优恢复模式。"""
        if trigger_count == 0:
            return {"mode": "none", "reason": "无需恢复", "scope": scope}

        cp_count = sum(
            1 for r in store.rows("checkpoint_mgr")
            if r.get("checkpoint_id") and r.get("status") == "已启用"
        )
        state_count = sum(
            1 for r in store.rows("state_store")
            if scope in str(r.get("配置名称", ""))
        )

        if trigger_count <= 2:
            mode = "restart"
            reason = "触发次数少，直接重启即可"
        elif trigger_count <= 5 and cp_count > 0:
            mode = "from_checkpoint"
            reason = f"检测到 {trigger_count} 次触发，从 {cp_count} 个检查点恢复"
        elif trigger_count > 5:
            mode = "escalation_response"
            reason = f"持续 {trigger_count} 次触发，需要告警升级处理"
        else:
            mode = "manual"
            reason = "条件不满足自动恢复，需人工介入"

        return {
            "mode": mode,
            "reason": reason,
            "scope": scope,
            "trigger_count": trigger_count,
            "available_checkpoints": cp_count,
            "available_states": state_count,
        }

    def verify_restore_completion(self, plan_name: str) -> dict[str, Any]:
        """验证恢复方案执行完成后的状态一致性。"""
        plan = None
        for r in store.rows(self.MODULE):
            if r.get("方案名称") == plan_name:
                plan = r
                break
        if plan is None:
            return {"ok": False, "error": f"恢复方案「{plan_name}」不存在"}

        scope = plan.get("任务范围", "")
        checks = []

        # 检查 replay_engine 中的回放任务
        replay_done = any(
            scope in str(t.get("目标拓扑", ""))
            and t.get("status") == "已完成"
            for t in store.rows("replay_engine")
        )
        checks.append({"source": "replay_engine", "ok": replay_done})

        # 检查 state_store 中的恢复状态
        state_restored = any(
            scope in str(s.get("配置名称", "")) or "replay" in str(s.get("配置名称", ""))
            for s in store.rows("state_store")
        )
        checks.append({"source": "state_store", "ok": state_restored})

        all_ok = all(c["ok"] for c in checks)
        if all_ok:
            plan["status"] = "已生效"
            plan["pending"] = False

        return {
            "ok": all_ok,
            "plan": plan_name,
            "scope": scope,
            "checks": checks,
        }

    def list_recovery_plans_with_status(self) -> list[dict[str, Any]]:
        """列出所有恢复方案及其关联的回放和检查点状态。"""
        plans = [
            r for r in store.rows(self.MODULE)
            if r.get("id", 0) < 1000 and r.get("方案名称")
        ]
        result = []
        for p in plans:
            name = p.get("方案名称", "")
            scope = p.get("任务范围", "")
            # 关联回放引擎
            linked_replays = [
                t for t in store.rows("replay_engine")
                if scope in str(t.get("目标拓扑", ""))
            ]
            # 关联检查点
            linked_cps = [
                c for c in store.rows("checkpoint_mgr")
                if c.get("checkpoint_id") and c.get("status") == "已启用"
            ]
            result.append({
                "plan": name,
                "mode": p.get("恢复模式", ""),
                "scope": scope,
                "status": p.get("status", ""),
                "linked_replays": len(linked_replays),
                "available_checkpoints": len(linked_cps),
            })
        return result

    def domain_snapshot(self) -> dict[str, Any]:
        base = super().domain_snapshot()
        rows = store.rows(self.MODULE)
        base["drilled"] = sum(1 for r in rows if r.get("status") == "已演练")
        base["activated"] = sum(1 for r in rows if r.get("status") == "已生效")
        base["active_plans"] = sum(
            1 for r in rows if r.get("_generated") and not r.get("_closed_at")
        )
        base["merged_hits"] = sum(len(r.get("_hit_ids", [])) for r in rows)
        base["cycles"] = len(store._cycles)
        base["receipts"] = len(store._receipts)
        base["receipt_versions"] = sorted(
            {str(r.get("source_version")) for r in store._receipts.values()}
        )
        return base