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
- `GET /api/teams`
- `POST /api/teams`
- `GET /api/materials`
- `POST /api/materials`
- `GET /api/dispatch`（可加`?status=`过滤）
- `POST /api/dispatch`
- `GET /api/dispatch/{dispatch_no}`
- `POST /api/dispatch/{dispatch_no}/retry`
- `GET /api/dispatch/{dispatch_no}/receipt`
- `POST /api/dispatch/{dispatch_no}/receipt`
- `GET /api/audit`

允许角色：inspector, dam_engineer, emergency_manager, viewer。异常值比控制阈值越高，缺陷优先级越高；应急处置缺陷必须完成复检并记录证据后才能关闭。

## 应急调度流程（派工 → 预占 → 出库回执 → 重试）

汛期多条应急缺陷同时抢班组与堵漏物资，系统把缺陷、应急班组和出库回执接成一条调度流程：

1. **派工（`POST /api/dispatch`）**：值班员提交派工单（`dispatch_no`、`item_id`、`team_id`、`materials`）。容量检查与预占在同一把锁、同一个事务内完成，并发提交不会超卖；同一 `dispatch_no` 重复提交只有一笔成功（409）。
2. **排队与缺口**：班组或物资容量不足时单据进入 `queued`，并在 `gap` 中写明缺口（班组缺几人、物资缺多少），不做任何预占。
3. **预占**：容量足够时单据置 `dispatched`，物资按明细行预占（`dispatch_lines.status='held'`），班组置 `assigned`。可用量 = 库存 − 已预占。
4. **出库回执逐项对账（`POST /api/dispatch/{dispatch_no}/receipt`）**：按物资明细行比较 `requested` 与 `shipped_qty`，逐行给出 `ok`/`short`。
   - 全额实发 → 回执 `reconciled`，扣减库存、释放预占与班组，单据置 `completed`。
   - 实发不足（`short`）或写入失败（`write_fail`）→ 回执记录失败，**释放本次预占**（明细行置 `released`、班组释放、单据置 `released`），随后**按原派工号重试**。
5. **重试不重复占料**：重试复用已有明细行（把 `released`/`pending` 行重新置为 `held`，而非新增），因此预占量始终等于申请量，不会叠加。已完成或已派工的单据不可再重试（幂等保护）。

重试接口：`POST /api/dispatch/{dispatch_no}/retry`（班组/物资补货后可手动重试）。

## 测试

```bash
python3 -m unittest discover -s tests -v
```
