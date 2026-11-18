"""告警策略领域服务：定义实时计算结果的告警触发规则。

数据流角色: anomaly_detector / cep_rule → alert_policy（消费者+生产者）→ restore_plan（消费者）
增强能力: 告警升级路径、沉默窗口计算、通知分发、批量告警合并且入 restore_plan
12号能力: 读取 CEP 命中告警数据，按同源（CEP 规则 + 任务范围）合并，同源只对应一个当前恢复方案
"""
from __future__ import annotations

import time
from typing import Any

from app.services import incident_ledger
from app.services.base import BaseModuleService
from app.store import store


class AlertPolicyService(BaseModuleService):
    MODULE = "alert_policy"
    ENTITY = "告警规则"
    LIST_FIELDS = ["规则名称", "监控指标", "触发条件", "告警级别", "沉默窗口", "通知方式", "升级路径"]
    REQUIRED_FIELDS = ["规则名称", "监控指标", "触发条件"]
    STATUS_ORDER = ["草稿", "已启用", "已沉默", "已关闭"]
    ACTION_RULES = {"启用规则": "草稿", "关闭规则": "已启用"}
    NEGATIVE_ACTIONS = []

    LEVEL_ORDER = {"DEBUG": 0, "INFO": 1, "WARNING": 2, "CRITICAL": 3}

    # ---- 跨模块数据消费与生产 ----

    def evaluate_alert_condition(
        self, rule_name: str, metric_value: float,
    ) -> dict[str, Any]:
        """评估告警条件：解析条件表达式并与当前值比较。"""
        rule = None
        for r in store.rows(self.MODULE):
            if r.get("规则名称") == rule_name:
                rule = r
                break
        if rule is None:
            return {"ok": False, "error": f"告警规则「{rule_name}」不存在"}
        if rule.get("status") in ("已关闭", "已沉默"):
            return {"ok": False, "error": f"规则「{rule_name}」处于「{rule.get('status')}」状态"}

        condition = rule.get("触发条件", "")
        triggered = self._parse_condition(condition, metric_value)
        return {
            "ok": True,
            "rule": rule_name,
            "metric_value": metric_value,
            "condition": condition,
            "triggered": triggered,
            "level": rule.get("告警级别", "WARNING"),
        }

    @staticmethod
    def _parse_condition(condition: str, value: float) -> bool:
        """解析条件表达式: '>100', '<50', '>=200', '<=10', '==0'。"""
        import re
        match = re.match(r'(>=|<=|!=|==|>|<)\s*([0-9.]+)', condition.strip())
        if not match:
            # 默认阈值
            try:
                threshold = float(condition)
                return value > threshold
            except (ValueError, TypeError):
                return value > 100
        op, threshold_str = match.groups()
        threshold = float(threshold_str)
        if op == ">":
            return value > threshold
        elif op == "<":
            return value < threshold
        elif op == ">=":
            return value >= threshold
        elif op == "<=":
            return value <= threshold
        elif op == "==":
            return value == threshold
        elif op == "!=":
            return value != threshold
        return False

    def check_silence_window(self, rule_name: str) -> dict[str, Any]:
        """检查规则是否在沉默窗口内：最近触发的告警与当前时间差。"""
        rule = None
        for r in store.rows(self.MODULE):
            if r.get("规则名称") == rule_name:
                rule = r
                break
        if rule is None:
            return {"ok": False, "error": f"告警规则「{rule_name}」不存在"}

        # 解析沉默窗口
        silence_str = (rule.get("沉默窗口", "300s") or "300s").replace("s", "")
        try:
            silence_sec = int(silence_str)
        except (ValueError, TypeError):
            silence_sec = 300

        now = time.time()
        # 查找该规则最近的告警记录
        recent_alerts = [
            a for a in store.rows(self.MODULE)
            if a.get("规则名称") == rule_name and a.get("_created_at")
        ]
        if not recent_alerts:
            return {"ok": True, "in_silence": False, "rule": rule_name}

        latest_ts = max(a.get("_created_at", 0) for a in recent_alerts)
        in_silence = (now - latest_ts) < silence_sec
        remaining = max(0, silence_sec - (now - latest_ts)) if in_silence else 0

        return {
            "ok": True,
            "rule": rule_name,
            "silence_window": silence_sec,
            "in_silence": in_silence,
            "remaining_seconds": remaining,
            "last_alert_ts": latest_ts,
        }

    def escalate_alert(self, rule_name: str) -> dict[str, Any]:
        """告警升级：按升级路径提升级别，并合并到同源当前恢复方案。

        12号修正：不再每次升级都新建方案。同一任务范围已有活动方案时，
        升级只提升级别、累计触发次数并回写合并清单，方案数量保持为一。
        """
        rule = None
        for r in store.rows(self.MODULE):
            if r.get("规则名称") == rule_name:
                rule = r
                break
        if rule is None:
            return {"ok": False, "error": f"告警规则「{rule_name}」不存在"}

        escalation_path = rule.get("升级路径", "oncall").split(",")
        current_level = rule.get("告警级别", "WARNING")
        level_value = self.LEVEL_ORDER.get(current_level, 1)
        next_level_name = "CRITICAL"
        if level_value < 3:
            next_level_name = next(
                (k for k, v in self.LEVEL_ORDER.items() if v == level_value + 1),
                "CRITICAL",
            )

        scope = rule.get("监控指标", "default_scope")
        active = incident_ledger.active_plans_for_scope(str(scope))
        if active:
            plan = active[0]
            plan["_trigger_count"] = int(plan.get("_trigger_count", 0)) + 1
            plan["恢复模式"] = "escalation_response"
            plan["跳过坏数据"] = "true"
            plan["_escalated_from"] = current_level
            plan["_escalated_to"] = next_level_name
            rule["告警级别"] = next_level_name
            return {
                "ok": True,
                "rule": rule_name,
                "escalated_from": current_level,
                "escalated_to": next_level_name,
                "path": escalation_path,
                "restore_plan_id": plan.get("id"),
                "merged_into_existing": True,
                "merge_count": len(plan.get("_hit_ids", [])),
                "message": "同源告警已合并到当前恢复方案，未新建方案",
            }

        # 在 restore_plan 中创建恢复方案
        plan = {
            "id": max(
                (int(r.get("id", 0)) for r in store.rows("restore_plan")), default=0
            ) + 1,
            "方案名称": f"告警恢复_{rule_name}_{int(time.time())}",
            "任务范围": scope,
            "恢复模式": "escalation_response",
            "源位点策略": "latest_alert",
            "状态复用": "resolved",
            "跳过坏数据": "true",
            "验证步骤": f"alert_level:{next_level_name},path:{','.join(escalation_path)}",
            "status": "草稿",
            "pending": True,
            "abnormal": True,
            "_alert_rule": rule_name,
            "_escalated_from": current_level,
            "_escalated_to": next_level_name,
            "_created_at": time.time(),
        }
        store.rows("restore_plan").append(plan)

        rule["告警级别"] = next_level_name
        return {
            "ok": True,
            "rule": rule_name,
            "escalated_from": current_level,
            "escalated_to": next_level_name,
            "path": escalation_path,
            "restore_plan_id": plan["id"],
            "merged_into_existing": False,
            "message": "该范围尚无活动恢复方案，已新建并占用唯一活动位",
        }

    def batch_merge_alerts(self) -> dict[str, Any]:
        """批量合并同一规则下的重复告警，减少告警风暴。"""
        rules: dict[str, list[dict[str, Any]]] = {}
        for r in store.rows(self.MODULE):
            rn = r.get("规则名称", "")
            if rn and r.get("_anomaly_detail"):
                rules.setdefault(rn, []).append(r)

        merges = {}
        for rn, entries in rules.items():
            if len(entries) > 1:
                # 合并：保留第一条，标记其余为已沉默
                entries.sort(key=lambda e: e.get("_created_at", 0))
                for e in entries[1:]:
                    e["status"] = "已沉默"
                    e["_merged_into"] = entries[0].get("id")
                merges[rn] = {
                    "original_count": len(entries),
                    "merged": len(entries) - 1,
                    "kept_alert_id": entries[0].get("id"),
                }

        return {
            "ok": True,
            "rules_merged": len(merges),
            "detail": merges,
        }

    # ---- 12号：CEP 命中 → 同源合并清单 ----

    def ingest_cep_hits(
        self,
        *,
        cep_rule: str,
        task_scope: str,
        alert_rule_name: str,
        matches: list[dict[str, Any]] | None = None,
        hits: list[dict[str, Any]] | None = None,
        mode: str = "auto",
        environment: str | None = None,
    ) -> dict[str, Any]:
        """读取 CEP 命中告警数据，逐条落证据后合并为同源清单。

        `hits` 每项需含 match_start_ts / match_end_ts / event_count；
        若只给 `matches`（CEP 匹配结果），则按匹配窗口换算触发证据。
        """
        raw = list(hits or [])
        if not raw and matches:
            for m in matches:
                events = m.get("events") or []
                raw.append({
                    "match_start_ts": m.get("start_ts", 0),
                    "match_end_ts": m.get("end_ts", 0),
                    "event_count": len(events),
                    "pattern": ",".join(str(e.get("event_type", "")) for e in events),
                })
        if not raw:
            return {"ok": False, "error": "没有 CEP 命中告警数据可合并", "ingested": 0}

        results = []
        for item in raw:
            results.append(
                incident_ledger.record_cep_hit(
                    cep_rule=cep_rule,
                    task_scope=task_scope,
                    alert_rule_name=alert_rule_name,
                    match_start_ts=float(item.get("match_start_ts", 0)),
                    match_end_ts=float(item.get("match_end_ts", 0)),
                    event_count=int(item.get("event_count", 0)),
                    pattern=str(item.get("pattern", "")),
                    environment=environment,
                    mode=mode,
                    hit_id=item.get("hit_id"),
                )
            )
        failed = [r for r in results if not r.get("ok")]
        merged = [r for r in results if r.get("ok")]
        first = merged[0] if merged else {}
        return {
            "ok": not failed,
            "ingested": len(merged),
            "duplicates": sum(1 for r in merged if r.get("duplicate")),
            "rotated": any(r.get("rotated") for r in merged),
            "reopened": any(r.get("reopened") for r in merged),
            "manifest_id": first.get("manifest_id"),
            "plan_id": first.get("plan_id"),
            "cycle_id": first.get("cycle_id"),
            "merge_count": first.get("merge_count"),
            "active_plan_count": first.get("active_plan_count"),
            "errors": [r.get("error") for r in failed],
        }

    def list_merge_manifests(self) -> list[dict[str, Any]]:
        """告警页面的合并清单：每个同源组一行，含合并数量与触发证据。"""
        scopes: dict[str, dict[str, Any]] = {}
        for row in incident_ledger.manifest_rows():
            key = row["source_key"]
            bucket = scopes.setdefault(key, {
                "source_key": key,
                "cep_rule": row["cep_rule"],
                "task_scope": row["task_scope"],
                "manifest_count": 0,
                "merged_hit_total": 0,
                "manifests": [],
            })
            bucket["manifest_count"] += 1
            bucket["merged_hit_total"] += row["merge_count"]
            bucket["manifests"].append(row)
        return sorted(scopes.values(), key=lambda b: b["task_scope"])

    def alert_merge_summary(self) -> dict[str, Any]:
        """告警页面顶部：合并数量、活动方案数与覆盖任务范围。"""
        rows = incident_ledger.manifest_rows()
        return {
            "manifest_count": len(rows),
            "merged_hit_total": sum(r["merge_count"] for r in rows),
            "cepHitTotal": sum(r["hit_count_evidence"] for r in rows),
            "scopes": sorted({r["task_scope"] for r in rows}),
            "active_plan_ids": [r["plan_id"] for r in rows if r["is_current_plan"]],
            "active_version": store._migration.get("active_version"),
            "environment": store._migration.get("environment"),
        }

    def list_active_alerts(self) -> list[dict[str, Any]]:
        """列出所有活跃的（未沉默/未关闭）告警及其关联恢复方案。"""
        active = [
            r for r in store.rows(self.MODULE)
            if r.get("status") == "已启用" or r.get("_anomaly_detail")
        ]
        result = []
        for a in active:
            entry = {
                "alert_id": a.get("id"),
                "rule": a.get("规则名称", ""),
                "metric": a.get("监控指标", ""),
                "level": a.get("告警级别", ""),
                "condition": a.get("触发条件", ""),
                "status": a.get("status", ""),
                "has_anomaly": bool(a.get("_anomaly_detail")),
            }
            # 查找关联恢复方案
            for rp in store.rows("restore_plan"):
                if a.get("规则名称") in str(rp.get("方案名称", "")):
                    entry["linked_restore"] = rp.get("方案名称", "")
                    entry["restore_status"] = rp.get("status", "")
                    break
            result.append(entry)
        return result

    def domain_snapshot(self) -> dict[str, Any]:
        base = super().domain_snapshot()
        rows = store.rows(self.MODULE)
        base["active_alerts"] = sum(1 for r in rows if r.get("status") == "已启用")
        base["silenced"] = sum(1 for r in rows if r.get("status") == "已沉默")
        base["escalation_count"] = sum(
            1 for r in rows if r.get("告警级别") == "CRITICAL"
        )
        return base