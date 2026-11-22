"""M8 故障改派账：改派规则、对账守恒与斗口状态。"""
import copy

import pytest

from app.errors import InputError
from app.manifest import canonical
from app.modules.m8_incidents.rules import run


@pytest.fixture
def report(clean_data, incident):
    return run(clean_data, incident)


def test_ledger_reconciles_to_zero(clean_data, report):
    s = report["summary"]
    # 原安排 = 已放量 + 新安排 + 渠尾余水，逐项勾稽。
    assert s["planned_l"] == s["delivered_l"] + s["new_allocated_l"] + s["tail_reserve_l"]
    assert s["difference_l"] == 0


def test_delivered_amounts_are_immutable(report):
    # 已发生的放水量原样保留，新安排只作用于尚未放出的部分。
    for row in report["ledger"]:
        assert row["total_l"] == row["delivered_l"] + row["new_allocated_l"]
        assert row["delivered_l"] >= 0


def test_fault_branch_outlets_are_stopped(report):
    stopped = {row["outlet"] for row in report["ledger"] if row["state"] == "stop"}
    # 东支渠及其下游北斗渠全部停掉，不再开新闸。
    assert {"001", "003", "004", "031", "032"} <= stopped
    assert all(row["new_allocated_l"] == 0 for row in report["ledger"] if row["state"] == "stop")


def test_unknown_impact_outlet_is_held(report):
    # 影响不明的 002 先挂起，不参与重排，待处理水量全额释放。
    assert report["held_outlets"] == ["002"]
    row = next(r for r in report["ledger"] if r["outlet"] == "002")
    assert row["state"] == "held" and row["new_allocated_l"] == 0
    assert row["released_l"] == row["pending_l"] == 1300
    assert report["state"] == "held"


def test_commands_close_before_open_and_are_sequenced(report):
    kinds = [c["command"] for c in report["commands"]]
    assert kinds == sorted(kinds, key=lambda k: 0 if k == "close" else 1)
    assert [c["seq"] for c in report["commands"]] == list(range(len(report["commands"])))
    # 挂起斗口同样先下关闸令。
    assert any(c["command"] == "close" and c["outlet"] == "002" for c in report["commands"])


def test_reschedule_respects_original_plan_cap(report):
    # 重排只在原安排盘子内：新安排不超过待处理水量，未超放斗口的累计不超过原安排。
    for row in report["ledger"]:
        assert row["new_allocated_l"] <= row["pending_l"]
        if row["delivered_l"] <= row["planned_l"]:
            assert row["total_l"] <= row["planned_l"]


def test_reachable_outlets_get_guarantee_then_fill(clean_data, report):
    # 西支 011/012、总干 020、苗圃 041/042 可恢复，按原序灌满原安排。
    refilled = {row["outlet"]: row for row in report["ledger"] if row["state"] == "reschedule"}
    assert set(refilled) == {"020", "041", "042", "011", "012"}
    for row in refilled.values():
        assert row["total_l"] == row["planned_l"]


def test_output_is_deterministic(clean_data, incident, report):
    again = run(copy.deepcopy(clean_data), copy.deepcopy(incident))
    assert canonical(again) == canonical(report)


def test_over_delivery_is_flagged_but_never_rewritten(clean_data, incident):
    bad = copy.deepcopy(incident)
    bad["incident"] = "INC-OVER"
    bad["deliveries"][0]["delivered"] = "2.0"  # 原安排仅 1.7m3
    report = run(clean_data, bad)
    row = next(r for r in report["ledger"] if r["outlet"] == "001")
    assert row["delivered_l"] == 2000 and row["new_allocated_l"] == 0
    assert "DELIVERY_EXCEEDS_PLAN" in {i["code"] for i in report["issues"]}


def test_unknown_outlet_is_evidence_not_ledger(clean_data, incident):
    bad = copy.deepcopy(incident)
    bad["incident"] = "INC-UNKNOWN-OUTLET"
    bad["deliveries"].append({"outlet": "999", "delivered": "0.1", "unit": "m3", "impact": "reachable"})
    report = run(clean_data, bad)
    assert "UNKNOWN_OUTLET" in {i["code"] for i in report["issues"]}
    assert all(row["outlet"] != "999" for row in report["ledger"])


def test_guarantee_shortage_when_water_runs_out(clean_data, incident):
    # 故障渠段吞掉绝大部分水，可重排盘子不足以同时补足所有保底。
    tight = copy.deepcopy(incident)
    tight["incident"] = "INC-TIGHT"
    tight["deliveries"] = [
        {"outlet": "001", "delivered": "1.7", "unit": "m3", "impact": "affected"},
        {"outlet": "002", "delivered": "2.3", "unit": "m3", "impact": "affected"},
        {"outlet": "003", "delivered": "3.0", "unit": "m3", "impact": "affected"},
        {"outlet": "004", "delivered": "1.0", "unit": "m3", "impact": "affected"},
        {"outlet": "031", "delivered": "0.5", "unit": "m3", "impact": "affected"},
        {"outlet": "032", "delivered": "0.4", "unit": "m3", "impact": "affected"},
    ]
    report = run(clean_data, tight)
    pending = report["summary"]["planned_l"] - report["summary"]["delivered_l"]
    floors = sum(r["minimum_l"] - r["delivered_l"] for r in report["ledger"]
                 if r["state"] == "reschedule" and r["minimum_l"] > r["delivered_l"])
    if floors > pending:
        assert "GUARANTEE_SHORTAGE" in {i["code"] for i in report["issues"]}
    assert report["summary"]["difference_l"] == 0


def test_reject_unknown_fault_channel(clean_data, incident):
    bad = copy.deepcopy(incident)
    bad["fault_channel"] = "不存在的渠"
    with pytest.raises(InputError) as exc:
        run(clean_data, bad)
    assert exc.value.code == "UNKNOWN_FAULT_CHANNEL"


def test_reject_bad_delivery_payload(clean_data, incident):
    bad = copy.deepcopy(incident)
    bad["deliveries"] = []
    with pytest.raises(InputError) as exc:
        run(clean_data, bad)
    assert exc.value.code == "BAD_INCIDENT"
