"""CEP规则领域服务：定义复杂事件处理的序列匹配规则。

增强能力: 规则语法校验、模式匹配验证、跳过策略评估、匹配结果输出到告警策略
"""
from __future__ import annotations

import time
from typing import Any

from app.services.base import BaseModuleService
from app.store import store


class CepRuleService(BaseModuleService):
    MODULE = "cep_rule"
    ENTITY = "事件序列"
    LIST_FIELDS = ["序列名称", "事件类型", "匹配顺序", "时间约束", "跳过策略", "动作输出", "最大跨度"]
    REQUIRED_FIELDS = ["序列名称", "事件类型", "匹配顺序"]
    STATUS_ORDER = ["草稿", "已部署", "运行中", "已停用"]
    ACTION_RULES = {"部署规则": "草稿", "停用规则": "运行中"}
    NEGATIVE_ACTIONS = ["停用规则"]

    def validate_rule_syntax(self, rule_name: str) -> dict[str, Any]:
        """校验CEP规则语法完整性。"""
        rule = None
        for r in store.rows(self.MODULE):
            if r.get("序列名称") == rule_name:
                rule = r
                break
        if rule is None:
            return {"ok": False, "error": f"CEP规则「{rule_name}」不存在"}

        checks = []
        event_types = [
            e.strip()
            for e in rule.get("事件类型", "").split(",")
            if e.strip()
        ]
        checks.append({"check": "event_types", "ok": len(event_types) >= 2, "detail": f"事件类型数: {len(event_types)}"})

        sequence = [
            s.strip()
            for s in rule.get("匹配顺序", "").split(",")
            if s.strip()
        ]
        checks.append({"check": "sequence_order", "ok": len(sequence) >= 2, "detail": f"匹配步骤: {len(sequence)}"})

        try:
            max_span = float(rule.get("最大跨度", "60"))
            checks.append({"check": "max_span", "ok": max_span > 0, "detail": f"最大跨度: {max_span}"})
        except (ValueError, TypeError):
            checks.append({"check": "max_span", "ok": False, "detail": "最大跨度格式错误"})

        return {
            "ok": all(c["ok"] for c in checks),
            "rule": rule_name,
            "checks": checks,
        }

    def match_and_alert(
        self, rule_name: str, events: list[dict[str, Any]],
    ) -> dict[str, Any]:
        """匹配CEP模式并写入告警策略。"""
        rule = None
        for r in store.rows(self.MODULE):
            if r.get("序列名称") == rule_name:
                rule = r
                break
        if rule is None:
            return {"ok": False, "error": f"CEP规则「{rule_name}」不存在"}

        pattern = [
            p.strip()
            for p in rule.get("匹配顺序", "").split(",")
            if p.strip()
        ]
        try:
            time_constraint = float(rule.get("时间约束", "60"))
        except (ValueError, TypeError):
            time_constraint = 60.0

        events_sorted = sorted(events, key=lambda e: e.get("event_time", 0))
        matches = []
        for i in range(len(events_sorted) - len(pattern) + 1):
            window = events_sorted[i:i + len(pattern)]
            if window[-1].get("event_time", 0) - window[0].get("event_time", 0) > time_constraint:
                continue
            if all(
                window[j].get("event_type", "") == pattern[j]
                for j in range(len(pattern))
            ):
                matches.append(window)

        if matches:
            output_action = rule.get("动作输出", "alert")
            alert_entry = {
                "id": max(
                    (int(r.get("id", 0)) for r in store.rows("alert_policy")), default=0
                ) + 1,
                "规则名称": f"cep_{rule_name}",
                "监控指标": rule.get("事件类型", ""),
                "触发条件": f"pattern_match_count={len(matches)}",
                "告警级别": "WARNING",
                "沉默窗口": "60s",
                "通知方式": "push",
                "升级路径": "oncall",
                "status": "已启用",
                "pending": True,
                "abnormal": True,
                "_cep_match_count": len(matches),
                "_created_at": time.time(),
            }
            store.rows("alert_policy").append(alert_entry)

            # 12号：逐条落 CEP 命中并合并到同源唯一活动方案，避免每次命中新建方案。
            from app.services import incident_ledger

            task_scope = rule.get("动作输出") or rule_name
            merge = incident_ledger.record_cep_hits_from_matches(
                cep_rule=rule_name,
                task_scope=str(task_scope),
                alert_rule_name=f"cep_{rule_name}",
                matches=matches,
            )
        else:
            merge = {"ingested": 0, "duplicates": 0}

        return {
            "ok": True,
            "rule": rule_name,
            "pattern": pattern,
            "output_action": output_action if matches else "",
            "total_events": len(events),
            "matches": len(matches),
            "match_rate": len(matches) / max(len(events) - len(pattern) + 1, 1),
            "merge": merge,
        }

    def evaluate_skip_strategy(
        self, rule_name: str, match_count: int,
    ) -> dict[str, Any]:
        """评估跳过策略：连续匹配过多时触发跳过以避免洪水。"""
        rule = None
        for r in store.rows(self.MODULE):
            if r.get("序列名称") == rule_name:
                rule = r
                break
        if rule is None:
            return {"ok": False, "error": f"CEP规则「{rule_name}」不存在"}

        skip_strategy = rule.get("跳过策略", "SKIP_PAST_LAST")
        # 模拟评估
        should_skip = match_count > 100

        return {
            "ok": True,
            "rule": rule_name,
            "strategy": skip_strategy,
            "match_count": match_count,
            "should_skip": should_skip,
            "action": "skip_and_continue" if should_skip else "process_all",
        }

    def domain_snapshot(self) -> dict[str, Any]:
        base = super().domain_snapshot()
        rows = store.rows(self.MODULE)
        base["deployed"] = sum(1 for r in rows if r.get("status") == "已部署")
        base["running"] = sum(1 for r in rows if r.get("status") == "运行中")
        return base