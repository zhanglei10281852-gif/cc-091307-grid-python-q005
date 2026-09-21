# 社区停车充电秩序事件服务

服务于网格员与社区管理：以**车牌或设备标识 + 位置 + 采集时间**建立秩序事件，
贯通「上报 → 告知 → 处置 → 申诉 → 复核 → 结案」全流程并全程留痕，
从机制上避免同一辆车在多群、多网格员之间被**重复上报、重复处罚**。

仅依赖 **Python 3.11 标准库**（SQLite + http.server），无需安装第三方包。

## 解决的核心问题

网格员晚高峰发现电动车占用消防通道，但**现场照片、车主申诉、整改结果散落在不同群聊**，
导致同一辆车被多次处罚。服务通过以下规则解决：

| 风险 | 机制 |
| --- | --- |
| 同车同点短时重复上报、重复处罚 | 同 `车辆/设备 + 位置` 且采集时间在去重窗口（默认 2 小时）内，自动**关联为既有事件的新上报与证据**，不新建事件；同类生效处置禁止重复出具 |
| 车主无法确认 | 只能生成**匿名线索（LEAD）**，线索阶段不得告知、不得处置；核实车主后才可流转 |
| 申诉期间继续被处罚 | 申诉即进入 `APPEALED`，**冻结一切新增处置/告知/结案**，须由管理员复核 |
| 处置无据可查 | 每项处置记录**依据版本、经办人、时间**三要素；撤销必须填写原因且原记录保留 |
| 越权看到个人信息 | 详情按**角色裁剪**：网格员看不到车主电话/证件号；车主看不到其他上报人身份 |
| 重启后状态/计数错乱 | 阶段以持久化字段为准；启动时按明细重算 `report_count`、生效处置数并校正发号器 |

## 事件状态机

```
上报 ─► OPEN（已受理）──► NOTIFIED（已告知）──► ENFORCED（处置生效）──► CLOSED
  │                                       ▲             │
  └─ 无法确认车主 ─► LEAD（匿名线索）       │        APPEALED（申诉中，处置冻结）
                    核实车主后转 OPEN       │          ├─ rejected 驳回 → 恢复 ENFORCED
                                           └──────────┴─ upheld 成立 → REVOKED（撤销）
```

## 目录结构

```
src/
  models.py    枚举、异常、数据载体与时间工具
  storage.py   SQLite 持久化（事务、计数自愈 reconcile、只追加事件链）
  service.py   领域核心 OrderEventService（状态机/去重/冻结/留痕/隐私）
  views.py     按角色的隐私裁剪
  api.py       标准库 http.server JSON 接口
tests/         unittest 测试（62 个）
```

## 运行

```bash
# 内存库仅用于测试；生产使用文件库
python3 -m src.api --host 0.0.0.0 --port 8080 \
    --db data/order_events.db --dedup-minutes 120
```

健康检查：

```bash
curl http://127.0.0.1:8080/v1/health
```

## 鉴权约定

通过请求头标识操作人（部署时替换为网关注名/令牌）：

| 头 | 说明 |
| --- | --- |
| `X-Actor-Id` | 经办人/车主标识（必填） |
| `X-Actor-Role` | `grid_worker` 网格员 / `admin` 管理员 / `owner` 车主 |
| `X-Actor-Name` | 姓名（可选，ASCII；用于留痕展示） |

## 接口一览

| 方法 | 路径 | 角色 | 说明 |
| --- | --- | --- | --- |
| POST | `/v1/incidents/report` | 网格员/管理员 | 上报（同车同点短时自动 `merged` 关联） |
| POST | `/v1/incidents/{id}/evidence` | 网格员/管理员 | 追加证据（照片/视频/备注/聊天记录） |
| POST | `/v1/incidents/{id}/identify` | 网格员/管理员 | 匿名线索确认车主 |
| POST | `/v1/incidents/{id}/notify` | 网格员/管理员 | 处罚前告知（需依据版本） |
| POST | `/v1/incidents/{id}/disposals` | 管理员 | 新增处置（需依据版本） |
| POST | `/v1/incidents/{id}/disposals/{did}/revoke` | 管理员 | 撤销处置（**必须有 reason**） |
| POST | `/v1/incidents/{id}/appeals` | 车主/网格员 | 申诉（可由网格员代登记） |
| POST | `/v1/incidents/{id}/appeals/{aid}/review` | 管理员 | 复核 `upheld` / `rejected` |
| POST | `/v1/incidents/{id}/close` | 管理员 | 结案 |
| GET | `/v1/incidents/{id}` | 按身份 | 事件详情（按角色裁剪 PII） |
| GET | `/v1/incidents/{id}/chain` | 按身份 | 只追加事件链 |
| GET | `/v1/incidents?stage=` | 网格员/管理员 | 列表与当前处置阶段 |
| GET | `/v1/admin/pending-appeals` | 管理员 | 待复核申诉工作台 |
| GET | `/v1/admin/stage-board` | 管理员 | 各阶段计数 |

### 典型流程

```bash
G=(-H 'X-Actor-Id: gw1' -H 'X-Actor-Role: grid_worker')
A=(-H 'X-Actor-Id: admin1' -H 'X-Actor-Role: admin')
O=(-H 'X-Actor-Id: own1' -H 'X-Actor-Role: owner')
CT=(-H 'Content-Type: application/json')

# 1. 上报（含车主与证据）
curl -s -X POST .../v1/incidents/report "${G[@]}" "${CT[@]}" -d '{
  "subject_type": "plate", "subject_id": "沪A12345",
  "location": {"location_key": "BLDG3-FIRELANE-A", "name": "3号楼消防通道"},
  "owner": {"id": "own1", "name": "张三", "phone": "13911112222"},
  "channel": "晚高峰巡查群",
  "evidences": [{"kind": "photo", "attachment_uri": "oss://a/1.jpg"}]
}'

# 同车同点短时再次上报 -> {"merged": true, ...}，不新建事件

# 2. 告知 → 3. 处置（依据版本必填）
curl .../{id}/notify   "${G[@]}" -d '{"basis_version":"XFMD-2026.1"}'
curl .../{id}/disposals "${A[@]}" -d '{"kind":"fine","basis_version":"XFMD-2026.1","amount":50}'

# 4. 车主申诉（处置冻结）→ 5. 管理员复核
curl .../{id}/appeals  "${O[@]}" -d '{"reason":"车已搬离"}'
curl .../{id}/appeals/1/review "${A[@]}" \
     -d '{"decision":"upheld","basis_version":"XFMD-2026.2","note":"监控属实"}'
```

## 关键设计说明

- **去重关联**：按 `subject_type + subject_id(大写车牌) + location_key` 取最近事件，
  采集时间差绝对值 ≤ 窗口且事件未结案即关联；晚到/补报同样纳入。窗口可配。
- **匿名线索**：上报时省略 `owner` 即为匿名，落 `LEAD`；`identify` 后转 `OPEN`。
- **申诉冻结**：`APPEALED` 阶段拒绝处置/告知/结案（409）；
  `upheld` 撤销全部生效处置并逐条写入撤销原因，`rejected` 恢复申诉前阶段。
- **留痕**：`chain` 表只追加，每个动作带动作、经办人、时间、依据版本、原因；
  处置撤销后原处置记录保留为 `revoked`，不物理删除。
- **隐私视角**：`views.py` 统一裁剪——管理员可见全部；网格员见姓名不见电话/证件；
  车主仅见本人事件且隐藏其他上报人；非本人访问统一返回 404。
- **重启一致**：启动执行 `reconcile()`，依据 `reports`/`disposals` 明细表
  重算派生计数，并以事件 ID 数字尾号校正 `INC-` 发号器（只增不减）。

## 测试

```bash
python3 -m unittest discover -s tests -v
```

覆盖：去重关联（异点/异车/窗口内外/晚到/结案）、匿名线索约束、告知前置、
处置三要素与同类去重、撤销原因与记录保留、申诉冻结与复核结论、角色隐私裁剪、
多群场景端到端、HTTP 状态码、以及重启后的状态/计数/发号一致性。
