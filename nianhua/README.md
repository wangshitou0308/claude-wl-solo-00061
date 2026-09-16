# 年画木版成套交接核对 API

祖辈把成套年画木版交给晚辈续藏时，木版、包纸、试印样常已混放，双方对"哪块配哪套"的记忆也可能不一。
本服务是一个**纯后端**交接核对 API：双方分别提交清点、套组、状态确认，系统按物性证据排除硬冲突、
证据不足时保留全部候选，绝不凭题名定案；只有每套木版/包纸/样张闭合且双方共同确认后，才转移保管责任并冻结留痕。

技术栈：Python · FastAPI · Pydantic v2 · SQLite（无外部服务，SQLite 文件即全部状态）。

## 核心规则

1. **三类实物**：`block` 木版（尺寸/色版角色/套准缺口/刻面方向/损伤/题记）、
   `wrapper` 包纸（材质/题记/损伤）、`sample` 试印样（呈色/线索/题记）。
2. **硬冲突 → 排除，不配对**。两造声明在以下物性上不可两立即判硬冲突：
   - 尺寸超容差（默认 ±2mm，缺测不冲突）
   - 色版角色具体色互异（线版/红/黄/蓝/绿/紫/黑/金；"色版"等泛指词不与具体色冲突）
   - 刻面方向互异（正刻/反刻/横刻）
   - 包纸材质互异（宣纸/毛边/布/函套…；泛指"纸"不冲突）
   - 样张呈色完全不相交
   命中硬冲突的条目进 `unmatched`，附带**最小冲突线索**（字段、类型、对侧候选码）。
3. **证据不足 → 全部候选保留**。多候选在容差内且无互单最高证据时，输出 `candidate_groups`，
   系统绝不替人拍板；通过"互为唯一最高候选（互单）"迭代消解的边才锁定为 `locked_pairs`。
   套准缺口、题记等是额外证据：见证补录独有缺口并被双方采纳后，可打破平票。
4. **题名不参与认定**。`title` 原样登记、原样进冻结快照，但不影响任何配对/并合结论；同名不同物照样硬冲突。
5. **软差异**（缺口不齐、损伤只一方报、题记有出入）不阻断配对，但在状态确认时必须显式
   `accept_discrepancy=true` 知晓。
6. **套组闭合**：两方套组标签靠锁定对并合（并查集）。一个套组必须
   三类齐全、单侧/分裂/空套不存在、无触碰的候选组、无未决见证、无见证矛盾，才算 `closed`。

## 状态机与"停在上一共同状态"

```
created ──双方清点确认──▶ inventory ──双方套组确认──▶ grouping ──每套双方状态确认──▶ status ──freeze──▶ frozen
```

- 双方各自持有确认行；共同阶段由双方确认实时推导。任一方撤回/更正，共同阶段自动回退。
- **撤回**：`POST .../sides/{side}/withdraw` 撤回该方最近一次确认（状态阶段按时间最新的一套），不删资料。
- **更正**：`PUT .../sides/{side}/items/{code}` 写新版本，只失效受影响结论
  （本方清点/套组头与该套状态；并经套组组分映射失效对方相关套的状态），旧版本保留留痕。
- **见证人**可随时补录实物观察；观察需**双方分别采纳**才并入证据（pending/disputed 均为阻断项）。
  一方退回复核即 `disputed`；并入的观察若与原声明硬矛盾，置 `witness_contradiction` 阻断闭合。
- **冻结**：全部套组闭合、无未配/候选/未决见证、共同阶段到 `status` 才成功。
  冻结后一切写入返回 `409 batch_frozen`。

每次确认带 `basis_hash`（所依据声明+已并入观察的哈希）；冻结批次保存
**原始声明（含全部历史版本）、分歧处置（失效/撤回/更正/见证处置事件）、责任变化、输入哈希与快照哈希**。

## 运行

```bash
cd nianhua
python3 -m venv .venv && . .venv/bin/activate
python -m pip install -r requirements.txt
export NIANHUA_DB=$PWD/nianhua.db          # 可选，默认 ./nianhua.db
python -m uvicorn app.main:app --port 8000
# 或：bash run.sh
```

交互式文档：<http://127.0.0.1:8000/docs>（Swagger），健康检查 `GET /health`。

无 HTTP 的端到端示例：`.venv/bin/python demo.py /tmp/demo.db`

## API 一览（路径参数 `side` ∈ handover/receive）

| 方法 | 路径 | 说明 |
|---|---|---|
| POST | `/batches` | 建批次 |
| GET | `/batches/{id}` | 批次全貌（状态 + 报告） |
| POST | `/batches/{id}/parties` | 登记交出方/接收方 |
| POST | `/batches/{id}/witnesses` | 登记见证家属 |
| GET | `/batches/{id}/report` | 核对报告（锁定对/候选/未配/套组闭合/阻断） |
| GET | `/batches/{id}/events` | 留痕事件流 |
| PUT | `/batches/{id}/sides/{side}/inventory` | 整批提交/替换清点（确认后须先撤回） |
| POST | `…/inventory/confirm` | 清点确认（本方有硬冲突未配时 409 + 最小线索） |
| PUT | `…/items/{code}` | 单条资料更正（局部失效） |
| PUT | `…/groups` | 条目分配到本方套组标签 |
| POST | `…/groups/confirm` | 套组确认（候选未消/未入组/分裂时 409） |
| POST | `…/status/confirm` | 逐套状态确认（软差异需 accept_discrepancy） |
| POST | `…/withdraw` | 撤回最近确认 |
| POST | `/batches/{id}/witnesses/{wid}/observations` | 见证补录（条目或套组） |
| POST | `/batches/{id}/observations/{oid}/sides/{side}/decision` | 采纳 / 退回复核 |
| POST | `/batches/{id}/freeze` | 满足全部闭合条件后冻结、转移责任 |
| GET | `/batches/{id}/freeze` | 取冻结快照（含输入哈希） |

所有阻断返回形如：

```json
{"error": {"code": "grouping_conflict", "message": "…",
           "details": {"minimal_conflict_clues": { … }}}}
```

## 最小调用示例

```bash
B=$(curl -s -X POST localhost:8000/batches -H 'Content-Type: application/json' \
  -d '{"title":"门神一套"}' | python -c 'import sys,json;print(json.load(sys.stdin)["batch_id"])')

curl -s -X POST localhost:8000/batches/$B/parties -H 'Content-Type: application/json' \
  -d '{"side":"handover","person":{"name":"祖父"}}'
curl -s -X POST localhost:8000/batches/$B/parties -H 'Content-Type: application/json' \
  -d '{"side":"receive","person":{"name":"长孙"}}'

curl -s -X PUT localhost:8000/batches/$B/sides/handover/inventory \
  -H 'Content-Type: application/json' -d '{"items":[
    {"kind":"block","code":"H1","dims":{"width_mm":300,"height_mm":450},
     "color_role":"线版","face_dir":"反刻","gaps":[[12,13]]},
    {"kind":"wrapper","code":"HW","material":"宣纸"},
    {"kind":"sample","code":"HS","colors":["黑"]}]}'
# …… 接收方同样提交后：
curl -s localhost:8000/batches/$B/report
```

## 测试

```bash
python -m pytest                       # 21 个用例（recon 纯函数 + API 全流程）
```

覆盖：同义归一化、尺寸/角色/方向/材质/呈色硬冲突、容差软差异、2:1 平票保留候选、
互单锁定、题名不决定、套组缺类/分裂/单侧、见证补证破平票、见证矛盾阻断、双方采纳语义、
撤回回退共同状态、更正的局部失效与跨方映射、冻结条件与哈希复现、冻结后禁写。

## 目录

```
nianhua/
├── app/
│   ├── schemas.py    # Pydantic 入参模型
│   ├── recon.py      # 纯函数核对引擎（归一化/硬冲突/候选消解/套组闭合）
│   ├── db.py         # SQLite schema 与连接
│   ├── hashing.py    # 规范化 JSON + SHA-256
│   ├── service.py    # 状态机、确认依据、撤回、更正、见证、冻结
│   └── main.py       # FastAPI 路由
├── tests/            # pytest
├── demo.py
├── run.sh
└── requirements.txt
```
