# 数据字典

| 输入表 | 关键字段 | 单位及约束 |
|---|---|---|
| config | batch、root、start、end、supply、unit、quantum_l | 批号和渠名为文本，最小刻度为正整升，渠首供水为非负整升 |
| gates | channel、start、end、state | 状态 `open` 或 `closed`，闸令须完整覆盖轮灌区间 |
| channels | channel、parent、inflow、loss、unit | 渠首上游为 `null`，损耗不超过入流，子渠入流和不超过上游净流量 |
| rights | outlet、channel、version、start、end、minimum、ceiling、unit、priority、weight | 保留编码前导零，保底和上限按最小刻度填报，优先级 1 最高，权重为正整数 |

时间含偏移，计算统一为 UTC，采用左闭右开区间。水量支持 `L`、`m3`、`m³`，千位分隔符须成组。空串、水量哨兵和小数升拒收。结果 `_l` 为升，理论配额可为精确分数字符串。

同一渠道按实测子渠入流与损耗计算本段可用量。先按优先级保底，同优先级短缺按保底值比例。所有级别保底后才按级别和权重分配余量。达到权证上限后，对尚未触顶的斗口重新计算权重。最后在本渠按最大余数落整，余数相同时依次比较优先级、斗口文本编码。整数刻度之外的水留在原渠。上游不供水时下游读数留在证据中，水量暂留可核实上游。

`MATCHED` 为对平且无异常，`REVIEW` 为对平但存在需复核事项，`DIFFERENCE` 为计算差异。回执仅在 `MATCHED` 时可下发。交付回执草案与证据文件，不连接闸控设备。

## 故障改派账输入（incident）

| 字段 | 含义 | 约束 |
|---|---|---|
| incident | 故障号，幂等键 | 非空文本；同号重试只认首次改派账 |
| fault_channel | 发生闸门故障的渠段 | 必须存在于 channels，其整棵下游子树视为停水区 |
| at | 故障时刻 | 带偏移时间，统一转 UTC |
| deliveries[].outlet | 已放量斗口 | 文本编码，未知斗口只留证据不入账 |
| deliveries[].delivered / unit | 故障前已放量 | 非负整升，支持 `L`、`m3`、`m³`；超过原安排留 `DELIVERY_EXCEEDS_PLAN`，已放量绝不回改 |
| deliveries[].impact | 影响判定 | `reachable` 可恢复、`affected` 立即停掉、`unknown` 挂起 |

## 改派规则（M8）

1. 重放原七阶段规划，得到每个斗口的原安排 `planned_l`、保底、上限与原轮灌顺序。
2. 已发生放水量只读：台账保留 `delivered_l`，任何新安排只作用于尚未放出的 `pending_l`。
3. 斗口归入四态：`done`（已放完）、`stop`（故障子树内或申报 affected，立即关闸、剩余安排取消）、`held`（impact=unknown，先挂起，不参与重排，待处理水量全额释放）、`reschedule`（可恢复，参与重排）。
4. 全部未放出水量回到公共盘子：先按原轮灌顺序补可恢复斗口的保底（同序比例缩并，不足留 `GUARANTEE_SHORTAGE`），再按原序逐个灌满至原安排上限；整数刻度之外与安排不出去的水全部转 `tail_reserve_l` 渠尾余水。结果唯一确定，不含随机选择。
5. 指令先停后开、连续编号：先对全部 stop/held 斗口下 close，再按原轮灌顺序对 reschedule 斗口下 open。
6. 守恒勾稽：`planned_l = delivered_l + new_allocated_l + tail_reserve_l`，差额非零记 `FAULT_LEDGER_DIFFERENCE`。存在挂起斗口或任一异常时 `state=held`，否则 `ready`。
