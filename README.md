# 社区停车充电秩序事件服务

网格员晚高峰巡查发现电动车占用消防通道时，现场照片、车主申诉、整改结果往往分散在
不同群聊，导致同一辆车被重复上报、重复处罚。本服务以 **车牌/设备标识 + 位置 + 采集时间**
建立秩序事件，把分散的证据、告知、申诉、复核、处置、撤销、结案串成一条可追溯的事件链。

运行环境：Python 3.11，仅使用标准库，无第三方依赖。代码位于 `src`，测试位于 `tests`。

## 核心规则

- **关联而非新建**：同一车辆（或设备）在同一位置、短时间（默认 30 分钟，可配置）内的
  重复上报自动并入既有事件，多次上报、多群聊证据都挂到同一条事件链，事件只建一次、
  处置只做一次。
- **匿名线索**：无法确认车主/车牌时只能生成匿名线索（`clue` 阶段），不能告知、处置、
  申诉；确认主体后转正式事件，若同时段同点已有该车辆的正式事件则直接并入。
- **申诉冻结**：申诉待复核期间禁止处置与结案；申诉成立自动撤销并留原因，不成立则
  恢复到申诉前阶段。
- **防重复处罚**：已处置且未撤销的事件再次执行处置直接拒绝。
- **全程留痕**：每项处置（告知/处置/复核/撤销/结案）强制记录依据版本、经办人、时间；
  撤销必须填写原因。
- **按角色脱敏**：`reviewer`（管理端）可见完整信息；`grid`（网格员）可业务操作；
  `owner`（车主）视图隐去上报人、群聊来源并对经办人/联系方式脱敏；未认证请求只返回
  处置进度等公开信息。
- **重启一致**：数据原子落盘为单个 JSON 文件；所有计数在加载时由事件记录重算，
  重启后状态、阶段、计数保持一致。

## 运行

```bash
python3 -m src.api --db data/orderdb.json --host 127.0.0.1 --port 8080 --merge-window 30
```

## 接口

角色通过 `X-Role` 请求头指定：`reviewer` / `grid` / `owner` / `anonymous`。

| 方法 | 路径 | 允许角色 | 说明 |
| --- | --- | --- | --- |
| POST | `/v1/events` | grid, reviewer | 上报建单；命中短时同点规则则返回既有事件（`created:false`） |
| GET | `/v1/events` | 全部（按角色脱敏） | 事件列表，可用 `?stage=` 过滤 |
| GET | `/v1/events/{id}` | 全部 | 事件详情（含当前处置阶段） |
| GET | `/v1/events/{id}/chain` | 全部 | 按时间排序的完整事件链 |
| POST | `/v1/events/{id}/evidence` | grid, reviewer | 追加证据 |
| POST | `/v1/events/{id}/identify` | grid, reviewer | 匿名线索确认车主/车辆 |
| POST | `/v1/events/{id}/notify` | grid, reviewer | 告知（需 `basis_version`） |
| POST | `/v1/events/{id}/appeals` | owner | 提交申诉 |
| POST | `/v1/events/{id}/appeals/{aid}/review` | reviewer | 复核（`upheld`/`rejected`） |
| POST | `/v1/events/{id}/enforce` | grid, reviewer | 执行处置（需先告知、非申诉期、未重复） |
| POST | `/v1/events/{id}/revoke` | reviewer | 撤销（必须带 `reason`） |
| POST | `/v1/events/{id}/close` | reviewer | 结案 |
| GET | `/v1/appeals/pending` | reviewer | 待复核申诉列表 |
| GET | `/v1/stats` | reviewer, grid | 阶段分布、待复核数、有效处置数（现场重算） |

上报请求示例：

```json
{
  "subject_type": "plate",
  "subject_id": "沪A12345",
  "location": "B3消防通道",
  "location_code": "B3-FIRE",
  "reporter": "网格员甲",
  "source_chat": "晚高峰巡查群",
  "reported_at": "2026-09-21T18:00:00+08:00",
  "evidence": [{"kind": "photo", "uri": "chat1://firelane.jpg"}]
}
```

无法确认车主时传 `"anonymous": true`（可不传标识）。

## 事件阶段

`collecting`（取证中）→ `notified`（已告知）→ `enforced`（已处置）→ `closed`（已结案）

- `appealing`：申诉中，处置冻结；复核后回到原阶段或进入 `revoked`
- `clue`：匿名线索，确认主体后进入正式流程
- `revoked`：已撤销（撤销原因与依据版本留痕）

## 测试

```bash
python3 -m unittest discover -s tests
```

覆盖：短时同点合并、跨群证据归集、匿名线索流转、申诉冻结与复核、防重复处罚、
撤销留痕、事件链排序、按角色脱敏、重启后状态与计数一致、HTTP 权限。
