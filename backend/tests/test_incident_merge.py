"""12号：同源告警合并、恢复周期、幂等切换与事务回滚。

覆盖：
  - 同源连续命中合并到唯一当前恢复方案，触发记录逐条保留
  - 重复写入同一命中不重复计数（幂等）
  - 恢复完成后再次触发形成可区分的新周期（两种路径：合并延长 / 关闭后新开）
  - 同一任务范围只有一个活动恢复方案，合并清单与方案命中一一对应
  - 迁移切换保留旧版本回执，回执出现在不同存量版本下
  - 同一幂等键重复切换只生效一次
  - 事务失步后整体回滚原版本，失败不占幂等键
"""
from __future__ import annotations

import copy

import pytest

from app.pipeline import generate_restore_plan, match_cep_pattern, trigger_alert_for_cep_matches
from app.services import incident_ledger as ledger
from app.services import migration_switch
from app.store import store


@pytest.fixture(autouse=True)
def _isolate_store():
    """每个用例独立快照 + 清空12号台账，避免种子数据和用例互相污染。"""
    snapshot = {
        "tables": {name: copy.deepcopy(rows) for name, rows in store._tables.items()},
        "manifests": copy.deepcopy(store._manifests),
        "hits": copy.deepcopy(store._alert_hits),
        "cycles": copy.deepcopy(store._cycles),
        "receipts": copy.deepcopy(store._receipts),
        "deployments": copy.deepcopy(store._deployments),
        "migration": copy.deepcopy(store._migration),
        "applied": copy.deepcopy(store._applied_switches),
    }
    store._manifests = {}
    store._alert_hits = {}
    store._cycles = {}
    store._receipts = {}
    store._migration = {
        "active_version": "legacy-v1",
        "environment": "dev",
        "last_switch": None,
        "ledger": [],
    }
    store._applied_switches = {}
    store.rows("restore_plan")[:] = [
        row for row in store.rows("restore_plan") if not row.get("_generated")
    ]
    yield
    store._tables = snapshot["tables"]
    store._manifests = snapshot["manifests"]
    store._alert_hits = snapshot["hits"]
    store._cycles = snapshot["cycles"]
    store._receipts = snapshot["receipts"]
    store._deployments = snapshot["deployments"]
    store._migration = snapshot["migration"]
    store._applied_switches = snapshot["applied"]


def _hit(start: float, end: float, events: int = 3, mode: str = "auto", scope: str = "login_security"):
    return ledger.record_cep_hit(
        cep_rule="登录失败序列",
        task_scope=scope,
        alert_rule_name="登录失败告警规则",
        match_start_ts=start,
        match_end_ts=end,
        event_count=events,
        pattern="login_fail,login_fail,login_fail",
        mode=mode,
    )


class TestSameSourceMerge:
    def test_consecutive_hits_merge_into_one_plan(self):
        first = _hit(1794000000, 1794000050)
        second = _hit(1794000100, 1794000150)
        third = _hit(1794000200, 1794000250)

        assert first["plan_id"] == second["plan_id"] == third["plan_id"]
        assert third["merge_count"] == 3
        assert third["active_plan_count"] == 1
        assert third["rotated"] is False

        plan = store.find("restore_plan", third["plan_id"])
        assert plan is not None
        assert len(plan["_hit_ids"]) == 3, "合并后必须保留全部触发记录"
        assert plan["_merged_count"] == 3

    def test_all_trigger_records_kept(self):
        hits = [_hit(1794100000 + i * 100, 1794100050 + i * 100) for i in range(4)]
        manifest_id = hits[-1]["manifest_id"]
        manifest = store._manifests[manifest_id]
        assert manifest["merge_count"] == 4
        assert len(manifest["hit_ids"]) == 4
        assert all(hid in store._alert_hits for hid in manifest["hit_ids"])
        assert len({hid for hid in manifest["hit_ids"]}) == 4

    def test_duplicate_hit_is_idempotent(self):
        first = _hit(1794200000, 1794200050)
        again = _hit(1794200000, 1794200050)
        assert again["duplicate"] is True
        assert again["hit_id"] == first["hit_id"]
        assert again["merge_count"] == first["merge_count"]
        assert len(store._alert_hits) >= 1
        manifest = store._manifests[first["manifest_id"]]
        assert manifest["merge_count"] == 1, "重复提交不得重复计数"

    def test_different_scope_gets_own_plan(self):
        a = _hit(1794300000, 1794300050, scope="login_security")
        b = _hit(1794300000, 1794300050, scope="payment_scope")
        assert a["plan_id"] != b["plan_id"]
        assert a["manifest_id"] != b["manifest_id"]

    def test_alias_rule_name_reuses_cycle(self):
        first = _hit(1794400000, 1794400050)
        alias = ledger.record_cep_hit(
            cep_rule="登录失败序列别名",
            task_scope="login_security",
            alert_rule_name="另一个规则名",
            match_start_ts=1794400100,
            match_end_ts=1794400150,
            event_count=2,
        )
        assert alias["plan_id"] == first["plan_id"], "同范围不得出现第二个当前周期"


class TestCycleRotation:
    def test_recovery_completed_then_retrigger_opens_new_cycle(self):
        first = _hit(1794500000, 1794500050)
        ledger.complete_recovery(first["plan_id"])
        second = _hit(1794501000, 1794501050)

        assert second["rotated"] is True
        assert second["plan_id"] != first["plan_id"]
        assert second["cycle_seq"] == first["cycle_seq"] + 1
        assert second["active_plan_count"] == 1

        old_plan = store.find("restore_plan", first["plan_id"])
        new_plan = store.find("restore_plan", second["plan_id"])
        assert old_plan is not None and new_plan is not None
        assert old_plan["_closed_at"] is not None
        assert old_plan["_hit_ids"], "关闭上一周期也必须保留其触发记录"
        assert new_plan["_hit_ids"] == [second["hit_id"]]
        assert old_plan["_cycle_id"] != new_plan["_cycle_id"]

    def test_extend_after_recovery_keeps_same_plan(self):
        first = _hit(1794600000, 1794600050)
        ledger.complete_recovery(first["plan_id"])
        extended = _hit(1794601000, 1794601050, mode="extend")

        assert extended["reopened"] is True
        assert extended["rotated"] is False
        assert extended["plan_id"] == first["plan_id"]
        assert extended["merge_count"] == 2
        plan = store.find("restore_plan", first["plan_id"])
        assert plan is not None
        assert len(plan["_hit_ids"]) == 2
        assert plan["_recovery_completed_at"] is None

    def test_new_cycle_mode_closes_previous(self):
        first = _hit(1794700000, 1794700050)
        second = _hit(1794700100, 1794700150, mode="new_cycle")
        assert second["rotated"] is True
        assert second["plan_id"] != first["plan_id"]
        cycle = store._cycles[store._manifests[first["manifest_id"]]["cycle_id"]]
        assert cycle["status"] == "closed"
        assert store._cycles[second["cycle_id"]]["status"] == "current"

    def test_cycle_evidence_lists_all_hits(self):
        first = _hit(1794800000, 1794800050)
        _hit(1794800100, 1794800150)
        plan = store.find("restore_plan", first["plan_id"])
        assert plan is not None
        evidence = ledger.plan_evidence(plan)
        assert evidence["merge_count"] == 2
        assert len(evidence["trigger_evidence"]) == 2
        assert evidence["cycle"]["status"] == "current"

    def test_cross_check_manifest_and_plan(self):
        first = _hit(1794900000, 1794900050)
        _hit(1794900100, 1794900150)
        checks = ledger.cross_check_manifest_plan("login_security")
        row = next(c for c in checks if c["manifest_id"] == first["manifest_id"])
        assert row["matched"] is True
        assert row["plan_is_unique_active"] is True
        assert row["manifest_hit_count"] == row["plan_hit_count"] == 2

    def test_cross_check_after_rotation(self):
        first = _hit(1794950000, 1794950050)
        ledger.complete_recovery(first["plan_id"])
        second = _hit(1794951000, 1794951050)

        checks = {
            c["manifest_id"]: c for c in ledger.cross_check_manifest_plan("login_security")
        }
        old = checks[first["manifest_id"]]
        new = checks[second["manifest_id"]]
        assert old["cycle_status"] == "closed"
        assert old["placement_ok"] is True, "历史周期方案应已关闭且让出活动位"
        assert old["matched"] is True, "历史周期的触发记录仍须完整"
        assert new["cycle_status"] == "current"
        assert new["plan_is_unique_active"] is True
        assert new["manifest_hit_count"] == new["plan_hit_count"] == 1


class TestMigrationReceipts:
    def test_switch_creates_receipts_per_version(self):
        first = _hit(1795000000, 1795000050)
        result = migration_switch.switch_environment(
            environment="dev",
            target_version="v2-migrated",
            switch_key="key-1",
            path="atomic",
        )
        assert result["ok"] is True
        assert result["migrated_plans"] >= 1
        plan = store.find("restore_plan", first["plan_id"])
        assert plan is not None
        versions = ledger.plan_evidence(plan)["receipt_versions"]
        assert "legacy-v1" in versions
        assert "v2-migrated" in versions, "迁移后保留旧版本回执并追加新版本回执"

    def test_switch_is_idempotent_per_key(self):
        first = migration_switch.switch_environment(
            environment="dev", target_version="v2-migrated", switch_key="key-2", path="atomic",
        )
        receipts_after_first = len(store._receipts)
        second = migration_switch.switch_environment(
            environment="dev", target_version="v2-migrated", switch_key="key-2", path="atomic",
        )
        assert second["idempotent"] is True
        assert second["switch_id"] == first["switch_id"]
        assert len(store._receipts) == receipts_after_first, "重复切换不得二次写回执"
        assert len(store._deployments["deploy_12"]["switches"]) == 1

    def test_switch_to_current_version_is_noop(self):
        first = migration_switch.switch_environment(
            environment="dev", target_version="v2-migrated", switch_key="key-3", path="atomic",
        )
        again = migration_switch.switch_environment(
            environment="dev", target_version="v2-migrated", switch_key="key-3", path="atomic",
        )
        assert again["switch_id"] == first["switch_id"]
        other_key = migration_switch.switch_environment(
            environment="dev", target_version="v2-migrated", switch_key="key-4", path="atomic",
        )
        assert other_key.get("noop") is True
        assert other_key["active_version"] == "v2-migrated"

    def test_failed_switch_rolls_back_to_original_version(self):
        _hit(1795100000, 1795100050)
        before_hits = len(store._alert_hits)
        before_receipts = dict(store._receipts)
        before_manifests = copy.deepcopy(store._manifests)

        result = migration_switch.switch_environment(
            environment="dev",
            target_version="v2-migrated",
            switch_key="key-fail",
            path="atomic",
            simulate_failure=True,
        )
        assert result["ok"] is False
        assert result["rolled_back"] is True
        assert result["active_version"] == "legacy-v1"
        assert len(store._alert_hits) == before_hits
        assert set(store._receipts) == set(before_receipts)
        assert store._manifests == before_manifests
        assert store._migration["active_version"] == "legacy-v1"

    def test_failed_switch_does_not_consume_idempotency_key(self):
        failed = migration_switch.switch_environment(
            environment="dev", target_version="v2-migrated", switch_key="key-retry",
            path="atomic", simulate_failure=True,
        )
        assert failed["ok"] is False
        retried = migration_switch.switch_environment(
            environment="dev", target_version="v2-migrated", switch_key="key-retry", path="atomic",
        )
        assert retried["ok"] is True
        assert retried["idempotent"] is False

    def test_rolling_path(self):
        _hit(1795200000, 1795200050)
        result = migration_switch.switch_environment(
            environment="dev", target_version="v2-migrated", switch_key="key-roll", path="rolling",
        )
        assert result["ok"] is True
        assert result["path"] == "rolling"
        assert migration_switch.verify_restore_dataflow()["ok"] is True

    def test_trace_locates_legacy_data(self):
        _hit(1795300000, 1795300050)
        trace = migration_switch.trace_cep_call_chain("login_security")
        assert trace["legacy_located"] is True
        stages = {stage["stage"]: stage for stage in trace["chain"]}
        assert stages["alert_hits"]["legacy_count"] >= 1
        assert "receipts" in stages

    def test_migration_keeps_single_active_plan(self):
        _hit(1795400000, 1795400050)
        _hit(1795400100, 1795400150)
        migration_switch.switch_environment(
            environment="dev", target_version="v2-migrated", switch_key="key-audit", path="atomic",
        )
        audit = ledger.assert_single_active_plan("login_security")
        assert audit["single"] is True
        assert audit["active_plan_count"] == 1


class TestRestorePlanActions:
    def test_execute_action_marks_recovery_complete(self):
        from app.services import service_for

        service = service_for("restore_plan")
        assert service is not None
        first = _hit(1795500000, 1795500050)
        entry, message = service.run_action(first["plan_id"], "启用方案")
        assert entry is not None
        assert "周期" in message
        plan = store.find("restore_plan", first["plan_id"])
        assert plan is not None
        assert plan["status"] == "已生效"
        assert plan.get("_recovery_completed_at")
        assert len(plan["_hit_ids"]) == 1

    def test_closed_cycle_plan_cannot_be_reactivated(self):
        from app.services import service_for

        service = service_for("restore_plan")
        assert service is not None
        first = _hit(1795600000, 1795600050)
        ledger.complete_recovery(first["plan_id"])
        second = _hit(1795601000, 1795601050)
        assert second["rotated"] is True

        entry, message = service.run_action(first["plan_id"], "启用方案")
        assert entry is None
        assert "已关闭周期" in message
        closed = store.find("restore_plan", first["plan_id"])
        assert closed is not None
        assert closed["_hit_ids"], "关闭周期的触发记录仍需保留"


class TestPipelineLink4:
    def test_cep_rule_service_merges_into_single_plan(self):
        from app.services import service_for

        service = service_for("cep_rule")
        assert service is not None
        rule = store.find("cep_rule", 1)
        assert rule is not None
        rule["匹配顺序"] = "login_fail,login_fail"
        rule["时间约束"] = "60"
        rule["动作输出"] = "cep_merge_scope"
        events = [
            {"event_id": str(i), "event_type": "login_fail", "event_time": 1797000000 + i}
            for i in range(5)
        ]
        first = service.match_and_alert("CEP规则样例1", events)
        second = service.match_and_alert("CEP规则样例1", events)
        assert first["matches"] >= 1
        assert first["merge"]["ingested"] >= 1
        assert second["merge"]["duplicates"] == second["merge"]["ingested"]
        plans = [
            p for p in store.rows("restore_plan")
            if p.get("任务范围") == "cep_merge_scope" and p.get("_generated")
        ]
        assert len(plans) == 1, "同源连续命中不得堆积恢复方案"
        manifest = store._manifests[plans[0]["_manifest_id"]]
        assert manifest["merge_count"] >= first["matches"]

    def test_pipeline_cep_alert_merges_instead_of_duplicating(self):
        events = [
            {"event_id": str(i), "event_type": "login_fail", "event_time": 1796000000 + i}
            for i in range(6)
        ]
        matches = match_cep_pattern(events, ["login_fail"] * 3, 60.0)
        first = trigger_alert_for_cep_matches(
            matches, "告警策略样例2", task_scope="pipeline_scope", cep_rule="登录失败序列",
        )
        assert first["triggered"] is True
        assert first["merge"]["ingested"] >= 1
        plan_count = sum(
            1 for p in store.rows("restore_plan")
            if p.get("任务范围") == "pipeline_scope" and p.get("_generated")
        )
        assert plan_count == 1

    def test_generate_restore_plan_merges_when_active(self):
        first = generate_restore_plan(3, "pipeline_scope_2", cep_rule="规则A")
        second = generate_restore_plan(3, "pipeline_scope_2", cep_rule="规则A")
        assert first["plan_created"] is True
        assert second["plan_created"] is False
        assert second["merged"] is True
        assert second["id"] == first["id"]

    def test_generate_restore_plan_zero_trigger(self):
        assert generate_restore_plan(0, "no_scope")["plan_created"] is False
