# 接口与证据

- `GET /api/health` 返回项目和规则版本。
- `GET /api/{模块}/{样例}` 单独计算到该阶段。模块与样例见深度登记。
- `POST /api/runs` 接收 `{"sample":"clean"}`、`{"sample":"dirty"}` 或 `{"data":{…}}`，返回完整计算和归档 ID。
- `GET /api/runs/{id}` 查询不可变归档。`POST /api/runs/{id}/rerun` 用归档输入与当前规则运行。相同规则与来源复用原记录。
- `GET /api/runs/{id}/export` 下载 JSON 证据。内含批次设置、各输入表、原行号、各阶段计算及规则版本。

## 故障改派账（放水中途闸门故障）

- `POST /api/incidents` 接收 `{"data":{…}或"sample":"clean", "incident":{…}}`，在不改已放量的前提下生成唯一可执行的改派账，并按故障号幂等落库。
- `GET /api/incidents/{incident_id}` 回读不可变改派账。
- 同一 `incident` 故障号重试：直接返回首次记录且 `reused=true`，不重新计算、不二次扣减。同一故障号换底账再次提交返回 `INCIDENT_CONFLICT`，禁止覆盖。
- 工作台页面 `/work/incidents?sample=clean` 展示原安排、已放量、新安排、待处理水量逐项对账与先停后开的指令序列。

incident 字段：`incident`（故障号，幂等键）、`fault_channel`（故障渠段，须在渠网内）、`at`（故障时刻，带偏移）、`deliveries`（已放量数组，每行 `outlet`、`delivered`、`unit`、`impact`）。`impact` 取 `reachable`（可恢复）、`affected`（受影响停掉）、`unknown`（影响不明，挂起）。

行号从 1 开始，按各 JSON 数组的位置计。归档键由项目、规则版本、来源名和规范化输入 JSON 计算。输入摘要为规范化 JSON 的 SHA-256。耗时与生成时刻只在首次提交时落库，便于重启后比较同一份记录。
