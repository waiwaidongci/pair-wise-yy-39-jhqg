# 大坝巡检、缺陷与应急管理

安排巡检，记录渗流、位移、裂缝等缺陷并跟踪修复、复检和应急预案。

## 模块结构

- `app.py`：参数解析、依赖组装和HTTP服务启动。
- `src/domain.py`：数据结构、错误、状态和基础校验。
- `src/rules.py`：状态机、角色矩阵、优先级、期限和关闭不变量。
- `src/repository.py`：SQLite建表、事务、版本控制和审计链。
- `src/service.py`：权限检查、用例编排、并发控制和审计。
- `src/http_api.py`：JSON路由和统一错误响应。
- `src/audit.py`：UTC时间和SHA-256审计事件。
- `static/index.html`：最小演示页。
- `tests/`：完整流程、规则和失败测试。

## 初始化与启动

```bash
python3 app.py --db ./data.db --port 8316
```

默认端口为`8316`，首次启动自动建库。使用`X-Actor`和`X-Role`请求头传递身份。

## 主要接口

- `GET /health`
- `GET /api/items`
- `POST /api/items`
- `GET /api/items/{id}`
- `POST /api/items/{id}/records`
- `POST /api/items/{id}/transition`，必须提交`expected_version`
- `GET /api/audit`

允许角色：inspector, dam_engineer, emergency_manager, viewer。异常值比控制阈值越高，缺陷优先级越高；应急处置缺陷必须完成复检并记录证据后才能关闭。

## 汛期应急派工调度

把缺陷、抢险班组、堵漏物资和出库回执接成一条流程，角色为emergency_manager。

- `POST /api/crews`：建班组，`{"name","capacity"}`；`GET /api/crews` 查看空闲席位。
- `POST /api/materials`：建物资，`{"name","unit","stock"}`；`GET /api/materials` 查看可用库存（库存扣减已派工预占）；`POST /api/materials/{id}/inbound` 补货，补货后自动按FIFO尝试提升排队单。
- `POST /api/dispatches`：派工，载荷 `{"item_id","crew_id","idempotency_key"?, "materials":[{"material_id","request_qty"}...]}`。
  - 同一缺陷只允许一笔进行中（queued/dispatched）的派工，两个值班员同时提交只有一笔成功，另一笔409。
  - 班组席位或物资可用量不足时进入`queued`，`gaps`逐项写明缺口（班组缺席数、物资需求量/可用量/缺口量），排队单不占资源。
- `POST /api/dispatches/{id}/retry`：按**原派工号**重试排队单，只预占"申请量-已发量"的差额，已发料不会被重复占用。
- `POST /api/dispatches/{id}/receipt`：出库回执逐项对账，载荷 `{"receipt_no","lines":[{"material_id","issued_qty"}...]}`，必须覆盖全部申请项（已发齐项以0对账）。
  - 全部按待发量发足：`outcome=full`，扣减库存、核销预占、派工`receipted`，并自动尝试提升排队单。
  - 任一项实发不足：`outcome=short`，按实发扣库，释放本次全部预占，派工回`queued`并在`gaps`写明缺口，回执已持久化，按原派工号重试。
  - 回执写入失败：事务整体回滚（不扣库不留回执），`outcome=write_failed`，释放本次预占并回`queued`。
  - 同一`receipt_no`重放返回`outcome=replayed`，不会重复扣库；`idempotency_key`重放返回原派工。
- `GET /api/dispatches?status=queued|dispatched|receipted` 与 `GET /api/dispatches/{id}` 查询派工、明细和缺口。

## 测试

```bash
python3 -m unittest discover -s tests -v
```
