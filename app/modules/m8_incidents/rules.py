"""M8 故障改派账规则。

放水中途闸门故障时，在不改动已发生放水量的前提下，把剩余水量重排成
唯一可执行的开闸顺序，并与原安排、已放量逐项勾稽。规则全部为纯函数，
同一份输入必然得到同一份改派账，便于事件库按故障号幂等去重。
"""
from collections import defaultdict

from app.evidence import issue, key, timestamp
from app.errors import InputError
from app.units import liters
from app.pipeline import calculate

# 斗口行的处理状态
DONE = "done"            # 故障前已放完原安排，不再开闸
STOP = "stop"            # 受故障影响：立即停掉，剩余安排取消
HOLD = "held"            # 影响不明：挂起，不参与本轮重排
RESCHEDULE = "reschedule"  # 可恢复供水：参与剩余水量重排


def _validate_incident(payload):
    """校验故障申报外壳，返回规整后的 incident；不合法直接拒收。"""
    if not isinstance(payload, dict):
        raise InputError("BAD_INCIDENT", "故障申报格式有误")
    try:
        incident = {
            "incident": key(payload["incident"]),
            "fault_channel": key(payload["fault_channel"]),
            "at": timestamp(payload["at"]).isoformat(),
        }
    except (KeyError, TypeError, ValueError) as error:
        raise InputError("BAD_INCIDENT", "故障号、故障渠段或故障时刻有误") from error
    deliveries = payload.get("deliveries", [])
    if not isinstance(deliveries, list) or not deliveries:
        raise InputError("BAD_INCIDENT", "至少需要一条已放量记录")
    rows, seen_outlets = [], set()
    for line, raw in enumerate(deliveries, 1):
        if not isinstance(raw, dict):
            raise InputError("BAD_DELIVERY", f"第 {line} 行已放量记录格式有误")
        try:
            outlet = key(raw["outlet"])
            delivered = liters(raw["delivered"], raw["unit"])
        except (KeyError, TypeError, ValueError) as error:
            raise InputError("BAD_DELIVERY", f"第 {line} 行斗口或已放量有误") from error
        if outlet in seen_outlets:
            raise InputError("BAD_DELIVERY", f"斗口 {outlet} 的已放量重复申报")
        seen_outlets.add(outlet)
        status = raw.get("impact", "reachable")
        if status not in ("reachable", "affected", "unknown"):
            raise InputError("BAD_INCIDENT", f"斗口 {outlet} 的影响判定只能是 reachable/affected/unknown")
        rows.append({"outlet": outlet, "delivered_l": delivered, "impact": status})
    incident["deliveries"] = rows
    return incident


def _baseline(data):
    """重放原轮灌规划，取每个斗口的原安排与所属渠段（原安排即故障前承诺）。"""
    result = calculate(data)
    plan, order = {}, []
    for row in result["tail"]["allocations"]:
        plan[row["outlet"]] = {
            "outlet": row["outlet"], "channel": row["channel"],
            "planned_l": row["allocated_l"],
            "minimum_l": row["minimum_l"], "ceiling_l": row["ceiling_l"],
            "priority": row["priority"], "weight": row["weight"],
        }
        order.append(row["outlet"])
    channels = {row["channel"]: row for row in result["channels"]["channels"]}
    return result, plan, order, channels


def _downstream(root, channels, start):
    """返回含 start 在内、以 start 为根的整棵下游子树（树状渠网）。"""
    children = defaultdict(list)
    for name, row in channels.items():
        if row["parent"] is not None:
            children[row["parent"]].append(name)
    subtree, stack = set(), [start]
    while stack:
        name = stack.pop()
        if name in subtree:
            continue
        subtree.add(name)
        stack.extend(children.get(name, ()))
    return subtree


def _classify(incident, plan, order, subtree, issues):
    """把每个斗口归入 done/stop/held/reschedule，并核对已放量。"""
    declared = {row["outlet"]: row for row in incident["deliveries"]}
    unknown = set(declared) - set(plan)
    for outlet in sorted(unknown):
        issues.append(issue("UNKNOWN_OUTLET"))
    rows = []
    for outlet in order:
        info = plan[outlet]
        record = declared.get(outlet)
        delivered = record["delivered_l"] if record else 0
        if delivered > info["planned_l"]:
            # 已放量是既成事实，绝不回改；超放只挂证据，新安排一律为 0。
            issues.append(issue("DELIVERY_EXCEEDS_PLAN"))
        in_fault_branch = info["channel"] in subtree
        if record is None:
            state = STOP if in_fault_branch else RESCHEDULE
        elif record["impact"] == "unknown":
            state = HOLD
        elif record["impact"] == "affected" or in_fault_branch:
            # 申报受影响，或斗口本就挂在故障子树上：剩余安排取消并立即停闸。
            state = STOP
        elif delivered >= info["planned_l"]:
            state = DONE
        else:
            state = RESCHEDULE
        rows.append({**info, "delivered_l": delivered, "state": state,
                     "pending_l": max(info["planned_l"] - delivered, 0)})
    return rows


def _fill_guarantees(candidates, budget, quantum, issues):
    """第一阶段：按原轮灌顺序补足保底，水量不足时按比例缩并留短缺证据。"""
    need_total = sum(max(row["minimum_l"] - row["delivered_l"], 0) for row in candidates)
    ratio = 1 if need_total <= budget else (budget / need_total if need_total else 1)
    assigned = {}
    for row in candidates:
        need = max(row["minimum_l"] - row["delivered_l"], 0)
        units = int(need * ratio // quantum)
        assigned[row["outlet"]] = units * quantum
        if ratio < 1 and need:
            issues.append(issue("GUARANTEE_SHORTAGE"))
    return assigned, budget - sum(assigned.values())


def _distribute_tail(candidates, assigned, budget, quantum):
    """第二阶段：保底之后按原轮灌顺序逐个灌满原安排，保证结果唯一。

    不引入新的权重随机选择：故障改派只在原安排盘子内重排，顺序即原轮灌
    顺序，先到先得、灌满为止，剩余整数刻度之外的水退回渠尾余水。
    """
    for row in candidates:
        # 累计水量（已放 + 已派保底 + 本次）不得超过原安排，已放量绝不重配。
        cap_units = (row["planned_l"] - row["delivered_l"] - assigned[row["outlet"]]) // quantum
        take_units = min(cap_units, budget // quantum)
        assigned[row["outlet"]] += take_units * quantum
        budget -= take_units * quantum
    return assigned, budget


def run(data, payload):
    """生成一份故障改派账。纯函数：相同 (data, payload) 输出逐字节稳定。"""
    incident = _validate_incident(payload)
    result, plan, order, channels = _baseline(data)
    issues = []
    if incident["fault_channel"] not in channels:
        raise InputError("UNKNOWN_FAULT_CHANNEL", "故障渠段不在渠网中")
    subtree = _downstream(result["batches"]["root"], channels, incident["fault_channel"])

    ledger_rows = _classify(incident, plan, order, subtree, issues)
    candidates = [row for row in ledger_rows if row["state"] == RESCHEDULE]

    # 可重排水量 = 原盘子里所有尚未放出的水。停闸/挂起斗口的待处理水量
    # 先释放回公共账，再按保底优先、原序灌满的规则重排，剩余转渠尾余水。
    total_pending = sum(row["pending_l"] for row in ledger_rows)
    quantum = result["batches"]["quantum_l"]

    assigned, leftover = _fill_guarantees(candidates, total_pending, quantum, issues)
    assigned, leftover = _distribute_tail(candidates, assigned, leftover, quantum)

    for row in ledger_rows:
        new_l = assigned.get(row["outlet"], 0)
        row["new_allocated_l"] = new_l
        row["released_l"] = row["pending_l"] - new_l if row["state"] in (STOP, HOLD) else 0
        row["total_l"] = row["delivered_l"] + new_l
    # 被停掉/挂起的斗口：待处理水量全部释放回公共账，不再给它们开新闸。
    # leftover 即保底与灌满之后仍未安排出去的水，全部转渠尾余水。
    tail_reserve_l = leftover

    commands, seq = [], 0
    # 先停后开：所有受影响和挂起的斗口先下关闸令，再按原轮灌顺序排开闸。
    for row in ledger_rows:
        if row["state"] in (STOP, HOLD):
            commands.append({"seq": seq, "command": "close", "outlet": row["outlet"],
                             "channel": row["channel"], "amount_l": 0})
            seq += 1
    for row in ledger_rows:
        if row["state"] == RESCHEDULE and row["new_allocated_l"]:
            commands.append({"seq": seq, "command": "open", "outlet": row["outlet"],
                             "channel": row["channel"], "amount_l": row["new_allocated_l"]})
            seq += 1

    delivered_total = sum(row["delivered_l"] for row in ledger_rows)
    new_total = sum(row["new_allocated_l"] for row in ledger_rows)
    planned_total = sum(row["planned_l"] for row in ledger_rows)
    difference = planned_total - delivered_total - new_total - tail_reserve_l
    if difference:
        issues.append(issue("FAULT_LEDGER_DIFFERENCE"))

    held = [row["outlet"] for row in ledger_rows if row["state"] == HOLD]
    state = "held" if (held or issues or difference) else "ready"
    return {
        "incident": incident["incident"],
        "fault_channel": incident["fault_channel"],
        "at": incident["at"],
        "state": state,
        "quantum_l": quantum,
        "ledger": [{
            "outlet": row["outlet"], "channel": row["channel"], "state": row["state"],
            "planned_l": row["planned_l"], "minimum_l": row["minimum_l"],
            "delivered_l": row["delivered_l"],
            "new_allocated_l": row["new_allocated_l"], "pending_l": row["pending_l"],
            "released_l": row["released_l"], "total_l": row["total_l"],
        } for row in ledger_rows],
        "commands": commands,
        "held_outlets": held,
        "summary": {
            "planned_l": planned_total, "delivered_l": delivered_total,
            "new_allocated_l": new_total, "tail_reserve_l": tail_reserve_l,
            "released_l": sum(row["released_l"] for row in ledger_rows),
            "difference_l": difference,
        },
        "issues": issues,
    }
