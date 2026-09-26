# 固定班次换乘规划 API（离线）

基于 **Python 3.11 + FastAPI** 的离线服务，提供十二个接口：

- `POST /v1/journey`：在给定站点集合与固定班次表中，计算从起点到终点的**最早到达**行程；
  到达时间相同时优先**换乘更少**，再按**班次 ID 序列字典序**取最小。
- `POST /v1/journey/meet`：规划两名旅客从不同起点出发的**会合方案**：会合站不能是任一出发站，
  两人各至少乘一班，先到者等待不超过 `max_wait`；优先**会合时刻最早**，再按**等待时间**、
  **总换乘数**、**会合站 ID**、**两人班次 ID 序列**依次裁决。
- `POST /v1/journey/reliability`：给定既定乘车序列与每班的离散延误分布，求全程**准时完成
  概率**（换乘满足缓冲且末段实际到达不晚于截止时间），以约分分数返回。
- `POST /v1/fares/quote`：给定一段已经确定的乘车序列与一组票券，求覆盖全部行程的**最低票价**；
  总价相同时优先**票数更少**，再按**票券 ID 序列字典序**取最小。
- `POST /v1/fleet/assign`：为整张班次表求**最少车辆**执行方案；每个班次恰好分配一次，
  车辆完成班次后仅可接续「末站相同、发车时间 ≥ 上一班末站到达 + `turnaround_minutes`」的班次。
- `POST /v1/fleet/charge`：为一辆电动车的既定班次链安排**最低电费补电方案**：仅在两班间
  停站且已配置充电站的站按整单位充电，每班出发前电量须覆盖本班耗电；电费并列时取
  充电量列表字典序最小者。
- `POST /v1/fleet/inspect`：为一辆车的既定班次链规划**里程检修**：每班结束后累计里程
  不得超过上限，仅可在两班之间、且车间开放时段能容纳完整工时的接续站检修，检修后里程
  归零；先使**检修次数**最少，再使**总费用**最低，并列时取**接续索引序列字典序**最小者。
- `POST /v1/seats/allocate`：在每班座位容量约束下**分配团体预订**，使接纳乘客数最大；
  并列时取接纳 ID 升序列表字典序最小者。
- `POST /v1/network/disruption`：求使出行需求**失去按时可达性**的**最少停运班次**；
  并列时取停运 ID 升序列表字典序最小者。
- `POST /v1/gtfs`：将 GTFS 子集（`stops`、`trips`、`stop_times`、`calendar`，可选
  `calendar_dates`、`frequencies`）按指定日期转换为当日 `stations`、`trips` 网络，
  结果可直接作为上述接口的输入。
- `POST /v1/timetable/design`：按客流为一条线路**编排班次**：从候选首发分钟中恰选指定数量，
  满足最小发车间隔、每批乘客等待不超过上限，使**人数加权总等待**最小；
  并列时取首发列表字典序最小者，并返回可直接用于 `/v1/journey` 的网络。
- `POST /v1/transfers/hold`：在换乘站**压车候车**：为各离站班次选择 0～上限的延后分钟，
  保证调整后发车间隔且全程时刻不越界，先使**接上换乘乘客数**最大，再使**在车延误总量**
  最小，并列时取延后列表字典序最小者。

## 快速启动（一条命令）

需要 Docker 与 Docker Compose：

```bash
docker compose up --build
```

服务监听 **8000 端口**：

- 接口：`POST http://localhost:8000/v1/journey`、`POST http://localhost:8000/v1/journey/meet`、`POST http://localhost:8000/v1/journey/reliability`、`POST http://localhost:8000/v1/fares/quote`、`POST http://localhost:8000/v1/fleet/assign`、`POST http://localhost:8000/v1/fleet/charge`、`POST http://localhost:8000/v1/fleet/inspect`、`POST http://localhost:8000/v1/seats/allocate`、`POST http://localhost:8000/v1/network/disruption`、`POST http://localhost:8000/v1/gtfs`、`POST http://localhost:8000/v1/timetable/design`、`POST http://localhost:8000/v1/transfers/hold`
- 交互式文档（Swagger UI）：`http://localhost:8000/docs`

停止：`docker compose down`。

也可以不用 Compose，纯 Docker：

```bash
docker build -t journey-planner .
docker run --rm -p 8000:8000 journey-planner
```

## 请求格式

`POST /v1/journey`，请求体 JSON：

| 字段 | 类型 | 约束 |
| --- | --- | --- |
| `stations` | `string[]` | 2～40 个，唯一、非空 |
| `trips` | `object[]` | 1～100 个班次，`id` 为唯一非空字符串 |
| `trips[].stops` | `object[]` | 2～20 个停站，按行驶顺序排列 |
| `trips[].stops[].station` | `string` | 必须出现在 `stations` 中 |
| `trips[].stops[].arrival` / `departure` | `int` | 0～2879 分钟；本站 `arrival ≤ departure`；下一站 `arrival` 必须**晚于**本站 `departure` |
| `origin` / `destination` | `string` | 必须在 `stations` 中，且二者不同 |
| `earliest_departure` | `int` | 0～2879，首次上车的最早发车分钟 |
| `transfer_buffer` | `int` | 1～60 分钟换乘缓冲 |

### 换乘规则

- 旅客可在班次任意停站上车、下车；
- 同车续乘（不下车或在同一班次上继续乘坐）**不计换乘**，也不需要缓冲；
- 改乘**别的**班次时，其发车时间不得早于「前一班下车时间 + `transfer_buffer`」；
- 首次上车只要求发车时间 `≥ earliest_departure`。

### 优选顺序

1. 到达时间最早；
2. 到达时间相同：换乘数最少；
3. 仍相同：班次 ID 序列字典序最小。

## 调用示例

```bash
curl -s -X POST http://localhost:8000/v1/journey \
  -H 'Content-Type: application/json' \
  -d '{
    "stations": ["A", "B", "C", "D"],
    "trips": [
      {"id": "T1", "stops": [
        {"station": "A", "arrival": 0,   "departure": 60},
        {"station": "B", "arrival": 120, "departure": 125},
        {"station": "C", "arrival": 200, "departure": 200}
      ]},
      {"id": "T2", "stops": [
        {"station": "C", "arrival": 210, "departure": 215},
        {"station": "D", "arrival": 300, "departure": 300}
      ]}
    ],
    "origin": "A",
    "destination": "D",
    "earliest_departure": 0,
    "transfer_buffer": 10
  }'
```

响应（`200 OK`）：

```json
{
  "reachable": true,
  "arrival_time": 300,
  "transfers": 1,
  "trip_ids": ["T1", "T2"],
  "segments": [
    {"trip_id": "T1", "from": "A", "to": "C", "departure": 60,  "arrival": 200},
    {"trip_id": "T2", "from": "C", "to": "D", "departure": 215, "arrival": 300}
  ]
}
```

### 响应字段

| 字段 | 说明 |
| --- | --- |
| `reachable` | 是否存在可行行程 |
| `arrival_time` | 到达终点的分钟数（不可达时为 `null`） |
| `transfers` | 换乘次数（改乘不同班次的次数） |
| `trip_ids` | 行程使用的班次 ID 序列 |
| `segments` | 分段行程：每段含 `trip_id`、`from`、`to`、`departure`、`arrival` |

### 不可达

无可行路线时返回 `200` 与空行程：

```json
{"reachable": false, "arrival_time": null, "transfers": 0, "trip_ids": [], "segments": []}
```

### 参数错误（422）

以下情况返回 `422 Unprocessable Entity`，响应体 `detail` 中包含具体错误位置与原因：

- 站点数量/唯一性违规，班次数量或 `id` 重复；
- `origin` / `destination` 相同，或不在 `stations` 中；
- 班次停站引用了未声明的站点；
- 时序错误：本站 `arrival > departure`，或下一站 `arrival` 未晚于上一站 `departure`；
- 时间超出 0～2879，`transfer_buffer` 超出 1～60，字段缺失或类型错误等。

## 准时概率：`POST /v1/journey/reliability`

`stations`、`trips` 的格式与约束与 `/v1/journey` 完全相同；请求另给一段**既定的乘车序列**
（`rides`，各段乘不同班次）、换乘缓冲 `transfer_buffer`、到达截止 `arrival_deadline`，
以及**恰好覆盖所乘班次**的延误分布 `delay_profiles`。

| 字段 | 类型 | 约束 |
| --- | --- | --- |
| `rides` | `object[]` | 1～12 段，按乘坐顺序排列；各段 `trip_id` **互不相同** |
| `rides[].trip_id` | `string` | 必须是 `trips` 中存在的班次 ID |
| `rides[].board_index` / `alight_index` | `int` | 停站索引（**从 0 开始**）；同一班次内必须 `board_index < alight_index`，且 `board_index` 不能是末站 |
| `transfer_buffer` | `int` | 1～60 分钟换乘缓冲 |
| `arrival_deadline` | `int` | 0～2879，末段实际到达的截止分钟 |
| `delay_profiles` | `object[]` | **恰好**覆盖所乘班次：每个所乘班次恰一条，不多不少 |
| `delay_profiles[].trip_id` | `string` | 该分布对应的班次 ID（profiles 内唯一） |
| `delay_profiles[].delays` | `object[]` | 1～6 个延误结果 |
| `delay_profiles[].delays[].delay_minutes` | `int` | 0～60 分钟，同一班次内**互不相同** |
| `delay_profiles[].delays[].weight` | `int` | 正整数权重；同一班次的权重和必须为 **100** |

### 延误与判定规则

- 每个班次**独立**抽取一次延误；延误把该班次的**全部**到发时刻（含上车站发车与下车站
  到达）整体平移同一分钟数；
- 每次换乘须满足：后段**实际发车** ≥ 前段**实际到达** + `transfer_buffer`（边界相等视为合法）；
- 末段**实际到达** ≤ `arrival_deadline` 视为准时（边界相等视为合法）；
- 乘车序列的**名义时刻**（零延误）不满足缓冲**不**视为无效行程：延误可能使换乘反而成立，
  此时仍返回 `200`，概率按实际时刻计算；任何延误组合都无法完成行程时返回概率 `0/1`。

### 调用示例

```bash
curl -s -X POST http://localhost:8000/v1/journey/reliability \
  -H 'Content-Type: application/json' \
  -d '{
    "stations": ["A", "C", "D"],
    "trips": [
      {"id": "T1", "stops": [
        {"station": "A", "arrival": 0,   "departure": 60},
        {"station": "C", "arrival": 200, "departure": 200}
      ]},
      {"id": "T2", "stops": [
        {"station": "C", "arrival": 210, "departure": 215},
        {"station": "D", "arrival": 300, "departure": 300}
      ]}
    ],
    "rides": [
      {"trip_id": "T1", "board_index": 0, "alight_index": 1},
      {"trip_id": "T2", "board_index": 0, "alight_index": 1}
    ],
    "transfer_buffer": 10,
    "arrival_deadline": 320,
    "delay_profiles": [
      {"trip_id": "T1", "delays": [
        {"delay_minutes": 0,  "weight": 70},
        {"delay_minutes": 20, "weight": 30}
      ]},
      {"trip_id": "T2", "delays": [
        {"delay_minutes": 0,  "weight": 50},
        {"delay_minutes": 10, "weight": 50}
      ]}
    ]
  }'
```

换乘要求 `215 + d2 ≥ 200 + d1 + 10`（即 `d2 ≥ d1 − 5`），截止 320 对本例不构成约束：
T1 准点（权重 70）时换乘必成，T1 延误 20 分钟（权重 30）时换乘必败，故成功概率为
70/100，约分后返回（`200 OK`）：

```json
{"success_numerator": "7", "success_denominator": "10"}
```

### 响应字段

| 字段 | 说明 |
| --- | --- |
| `success_numerator` | 约分后成功概率的**分子**（十进制字符串） |
| `success_denominator` | 约分后成功概率的**分母**（十进制字符串）；零概率时为 `"0"` / `"1"` |

### 准时概率接口的 422 情形

除 `stations` / `trips` 本身的校验外，以下情况也返回 `422`：

- 段数超出 1～12、乘车班次 ID 重复、引用不存在的班次、上下车索引越界或
  `board_index >= alight_index`；
- 前段下车站与后段上车站不一致；
- `delay_profiles` 未恰好覆盖所乘班次（缺少所乘班次、包含未乘班次或班次 ID 重复）；
- 某班次延误结果为空或超过 6 个、`delay_minutes` 重复或超出 0～60、权重非正整数、
  权重和不为 100；
- `transfer_buffer` 超出 1～60、`arrival_deadline` 超出 0～2879、字段缺失或类型错误等。

## 两人会合：`POST /v1/journey/meet`

`stations`、`trips` 的格式与约束与 `/v1/journey` 完全相同；请求另给**恰好两名**旅客
（`travelers`）、换乘缓冲 `transfer_buffer` 与最长等待 `max_wait`。

| 字段 | 类型 | 约束 |
| --- | --- | --- |
| `travelers` | `object[]` | **恰好 2 人**，按请求顺序记为第 1、2 人 |
| `travelers[].origin` | `string` | 必须在 `stations` 中；两人起点**必须不同** |
| `travelers[].earliest_departure` | `int` | 0～2879，该旅客首次上车的最早发车分钟 |
| `transfer_buffer` | `int` | 1～60 分钟换乘缓冲 |
| `max_wait` | `int` | 0～60 分钟，先到者在会合站的最长等待时间 |

### 会合规则

- 会合站**不能是任一方的出发站**；两人各自**至少乘上一班**车；
- 两人分别按既有规则行进：首次上车发车时间 ≥ 各自 `earliest_departure`，同车续乘
  不计换乘也不需要缓冲，改乘别的班次时其发车时间 ≥ 前一班下车时间 + `transfer_buffer`；
- 会合时刻 = 两人到达会合站时刻的**较大值**；先到者等待不超过 `max_wait`
  （即两人到达时刻之差 ≤ `max_wait`，边界相等视为合法）；
- 为达成会合，旅客**不一定取各自最早到达的路线**：较晚到达的路线可能反而缩短等待，
  规划时须一并考虑。

### 优选顺序

1. 会合时刻最早；
2. 会合时刻相同：等待时间（两人到达时刻之差）最短；
3. 仍相同：两人**总换乘数**最少；
4. 仍相同：会合站 ID 字典序最小；
5. 仍相同：第 1 人班次 ID 序列字典序最小，再比较第 2 人。

### 调用示例

```bash
curl -s -X POST http://localhost:8000/v1/journey/meet \
  -H 'Content-Type: application/json' \
  -d '{
    "stations": ["A", "B", "C"],
    "trips": [
      {"id": "T1", "stops": [
        {"station": "A", "arrival": 0,   "departure": 60},
        {"station": "C", "arrival": 200, "departure": 200}
      ]},
      {"id": "T2", "stops": [
        {"station": "B", "arrival": 0,   "departure": 100},
        {"station": "C", "arrival": 230, "departure": 230}
      ]},
      {"id": "T3", "stops": [
        {"station": "A", "arrival": 0,   "departure": 150},
        {"station": "C", "arrival": 225, "departure": 225}
      ]}
    ],
    "travelers": [
      {"origin": "A", "earliest_departure": 0},
      {"origin": "B", "earliest_departure": 0}
    ],
    "transfer_buffer": 10,
    "max_wait": 30
  }'
```

第 1 人可乘 T1（200 到 C）或 T3（225 到 C），第 2 人乘 T2 于 230 到 C。两种配对的会合
时刻都是 230，但选较晚到达的 T3 可把等待从 30 分钟缩短到 5 分钟，故返回（`200 OK`）：

```json
{
  "reachable": true,
  "meeting_station": "C",
  "meeting_time": 230,
  "travelers": [
    {
      "arrival_time": 225,
      "transfers": 0,
      "trip_ids": ["T3"],
      "segments": [
        {"trip_id": "T3", "from": "A", "to": "C", "departure": 150, "arrival": 225}
      ]
    },
    {
      "arrival_time": 230,
      "transfers": 0,
      "trip_ids": ["T2"],
      "segments": [
        {"trip_id": "T2", "from": "B", "to": "C", "departure": 100, "arrival": 230}
      ]
    }
  ]
}
```

### 响应字段

| 字段 | 说明 |
| --- | --- |
| `reachable` | 是否存在可行会合方案 |
| `meeting_station` | 会合站（无解时为 `null`） |
| `meeting_time` | 会合时刻，即两人到达时刻的较大值（无解时为 `null`） |
| `travelers` | 恰含两项，与请求的 `travelers` 同序 |
| `travelers[].arrival_time` | 该旅客到达会合站的分钟数（无解时为 `null`） |
| `travelers[].transfers` | 该旅客的换乘次数 |
| `travelers[].trip_ids` | 该旅客使用的班次 ID 序列 |
| `travelers[].segments` | 该旅客的分段行程（格式同 `/v1/journey` 的 `segments`） |

### 无可行会合

无满足条件的会合方案时返回 `200`，标量字段为 `null`、两人行程为空列表：

```json
{
  "reachable": false,
  "meeting_station": null,
  "meeting_time": null,
  "travelers": [
    {"arrival_time": null, "transfers": 0, "trip_ids": [], "segments": []},
    {"arrival_time": null, "transfers": 0, "trip_ids": [], "segments": []}
  ]
}
```

### 会合接口的 422 情形

除 `stations` / `trips` 本身的校验外（与 `/v1/journey` 一致），以下情况也返回 `422`：

- `travelers` 不是恰好 2 人、两人起点相同、或起点不在 `stations` 中；
- `earliest_departure` 超出 0～2879、`max_wait` 超出 0～60、`transfer_buffer` 超出 1～60；
- 字段缺失、出现多余字段或类型错误等。

## 最低票价：`POST /v1/fares/quote`

`stations`、`trips` 的格式与约束与 `/v1/journey` 完全相同；请求另含一段固定的乘车序列
（`rides`）与一组可购票券（`tickets`）。

| 字段 | 类型 | 约束 |
| --- | --- | --- |
| `rides` | `object[]` | 1～20 段，按乘坐顺序排列 |
| `rides[].trip_id` | `string` | 必须是 `trips` 中存在的班次 ID |
| `rides[].board_index` / `alight_index` | `int` | 停站索引（**从 0 开始**）；同一班次内必须 `board_index < alight_index`，且 `board_index` 不能是末站 |
| `transfer_buffer` | `int` | 1～60 分钟换乘缓冲 |
| `tickets` | `object[]` | 1～12 种票券，`id` 为唯一非空字符串 |
| `tickets[].price_cents` | `int` | 非负整数，票价（分） |
| `tickets[].valid_minutes` | `int` | 0～2879，自购票时刻起的有效分钟数 |
| `tickets[].max_rides` | `int` | 1～20，最多可覆盖的段数 |

### 乘车序列规则（违规返回 422）

- 每段在对应班次内**上车索引严格小于下车索引**；
- 相邻两段必须**换乘不同班次**；
- 前段下车站必须等于后段上车站；
- 后段发车时间 ≥ 前段到达时间 + `transfer_buffer`（边界相等视为合法）。

### 票券规则

- 票券在其所覆盖**首段的发车时刻**购买；
- 一张票只能覆盖**连续的若干段**，段数 ≤ `max_rides`；
- 其所覆盖各段的发车时间都必须 ≤ 购票时刻 + `valid_minutes`（即末段发车 − 首段发车 ≤ `valid_minutes`）；
- 每种票券可重复购买；**每段恰好由一张票覆盖**。
- 优选顺序：① 总票价最低；② 总价相同购票数最少；③ 仍相同按购票的票券 ID 序列
  （乘车顺序）字典序最小。

### 调用示例

```bash
curl -s -X POST http://localhost:8000/v1/fares/quote \
  -H 'Content-Type: application/json' \
  -d '{
    "stations": ["A", "B", "C", "D"],
    "trips": [
      {"id": "T1", "stops": [
        {"station": "A", "arrival": 0,   "departure": 100},
        {"station": "B", "arrival": 120, "departure": 125},
        {"station": "C", "arrival": 200, "departure": 200}
      ]},
      {"id": "T2", "stops": [
        {"station": "C", "arrival": 210, "departure": 215},
        {"station": "D", "arrival": 300, "departure": 300}
      ]}
    ],
    "rides": [
      {"trip_id": "T1", "board_index": 0, "alight_index": 2},
      {"trip_id": "T2", "board_index": 0, "alight_index": 1}
    ],
    "transfer_buffer": 10,
    "tickets": [
      {"id": "single", "price_cents": 200, "valid_minutes": 0,    "max_rides": 1},
      {"id": "hour",   "price_cents": 250, "valid_minutes": 60,   "max_rides": 2},
      {"id": "day",    "price_cents": 500, "valid_minutes": 2879, "max_rides": 20}
    ]
  }'
```

两段发车分别为第 100、215 分钟，相差 115 分钟：`hour` 票（60 分钟）无法覆盖两段，
`day` 票 500 分贵于两张 `single`（400 分），故返回（`200 OK`）：

```json
{
  "total_cents": 400,
  "tickets": [
    {"ticket_id": "single", "covers_from": 0, "covers_to": 0},
    {"ticket_id": "single", "covers_from": 1, "covers_to": 1}
  ]
}
```

### 响应字段

| 字段 | 说明 |
| --- | --- |
| `total_cents` | 最低总票价（分） |
| `tickets` | 依次购买的票券，按乘车顺序列出 |
| `tickets[].ticket_id` | 票券 ID（同一种票券可重复出现） |
| `tickets[].covers_from` / `covers_to` | 该票覆盖的段索引范围（0 基，**含端点**） |

### 票价接口的 422 情形

除 `stations` / `trips` 本身的校验外，以下情况也返回 `422`：

- 某段引用不存在的 `trip_id`，或上下车索引越界、`board_index >= alight_index`；
- 相邻两段使用同一班次、下车站与下段上车站不一致、或换乘缓冲不足；
- 票券 ID 重复；`price_cents` 为负、`valid_minutes` 超出 0～2879、`max_rides` 超出 1～20；
- `rides` / `tickets` 数量越界、`transfer_buffer` 超出 1～60、字段缺失或类型错误等。

## 最少车辆：`POST /v1/fleet/assign`

`stations`、`trips` 的格式与约束与 `/v1/journey` 完全相同；请求另含一个整备时间字段。

| 字段 | 类型 | 约束 |
| --- | --- | --- |
| `turnaround_minutes` | `int` | 0～60 分钟，车辆完成一班后接续下一班所需的最小间隔 |

### 接续规则

- 每个班次从**首站发车**到**末站到达**期间独占一辆车，且恰好被分配一次；
- 车辆完成班次后，只可接续**末站与下一班首站相同**、且下一班首站发车时间
  **不早于**「本班末站到达时间 + `turnaround_minutes`」的班次（边界相等视为合法）；
- 不安排空驶（车辆不得在站间空跑）；允许只执行一个班次的车辆。

### 优选与排序

- `vehicle_count` 必须为**全局最少**（最小路径覆盖：车辆数 = 班次数 − 最大接续匹配数）；
- 满足最少车辆数的接法有多种时任选其一均可，本实现对同一请求返回确定结果；
- 返回的车辆列表按**首班发车时间**、再按**首班班次 ID** 排序；每辆车的 `trip_ids`
  按执行顺序排列。

### 调用示例

```bash
curl -s -X POST http://localhost:8000/v1/fleet/assign \
  -H 'Content-Type: application/json' \
  -d '{
    "stations": ["A", "B", "C", "D"],
    "trips": [
      {"id": "T1", "stops": [
        {"station": "A", "arrival": 0,   "departure": 60},
        {"station": "B", "arrival": 120, "departure": 120}
      ]},
      {"id": "T2", "stops": [
        {"station": "B", "arrival": 130, "departure": 130},
        {"station": "C", "arrival": 200, "departure": 200}
      ]},
      {"id": "T3", "stops": [
        {"station": "A", "arrival": 70,  "departure": 80},
        {"station": "D", "arrival": 150, "departure": 150}
      ]}
    ],
    "turnaround_minutes": 10
  }'
```

T1 末站 B 到达 120，T2 首站 B 发车 130 ≥ 120 + 10，可同车接续；T3 末站 D 无班可接，
故最少 2 辆车（`200 OK`）：

```json
{
  "vehicle_count": 2,
  "vehicles": [
    {"trip_ids": ["T1", "T2"]},
    {"trip_ids": ["T3"]}
  ]
}
```

### 响应字段

| 字段 | 说明 |
| --- | --- |
| `vehicle_count` | 全局最少车辆数 |
| `vehicles` | 车辆列表，按首班发车时间、首班班次 ID 排序 |
| `vehicles[].trip_ids` | 该车执行的班次 ID，按执行顺序排列 |

### 车辆接口的 422 情形

`stations` / `trips` 的校验与 `/v1/journey` 完全一致（站点唯一、班次 ID 唯一、
停站引用已声明站点、时序合法等）；此外 `turnaround_minutes` 缺失或超出 0～60、
字段缺失或类型错误等也返回 `422`。

## 电动车补电：`POST /v1/fleet/charge`

`stations`、`trips` 的格式与约束与 `/v1/journey` 完全相同；请求另给一条**既定班次链**
（`trip_ids`，按执行顺序排列）、每班耗电量 `consumptions`、电池参数与充电站列表
`chargers`，求完成全链的**最低电费**补电方案。

| 字段 | 类型 | 约束 |
| --- | --- | --- |
| `trip_ids` | `string[]` | 1～12 个，按执行顺序排列且**互不相同**，每个都必须存在于 `trips` |
| `consumptions` | `object` | 键必须**恰好**覆盖链上班次 ID（缺班或多出链外班次均 422），值为 1～60 的每班耗电量 |
| `capacity` | `int` | 1～60，电池容量 |
| `initial_charge` | `int` | 0～`capacity`，出发时的初始电量 |
| `chargers` | `object[]` | 0～40 个充电站，`station` **互不相同**且必须出现在 `stations` 中 |
| `chargers[].rate` | `int` | 1～60，每分钟充电单位数 |
| `chargers[].price_cents` | `int` | 非负整数，每单位电费（分） |

### 接续与充电规则

- 班次链按 `trip_ids` 顺序执行：相邻两班必须在**同一站**接续（前班末站 = 后班首站），
  且后班首站发车**不得早于**前班末站到达（边界相等视为合法）；接续违规返回 422；
- 每班出发前电量须**覆盖本班耗电**（`电量 ≥ consumptions[trip]`），行驶后扣减相应电量；
- **仅在两班之间的停站**可以充电（首班之前、末班之后以及班次途中均不充电），且该站
  必须已配置充电站（未配置的站不能充电）；
- 单次充电量为**非负整数**，上限为 `rate × 停站分钟`（停站分钟 = 后班首站发车 − 前班
  末站到达），且充电后电量不得超过 `capacity`；
- 电费 = 该站充电量 × 本站 `price_cents`，总电费为各次之和；
- 优选顺序：① **总电费最低**；② 并列时取**充电量列表**（按接续顺序，即两班间隙的
  先后顺序）**字典序最小**的方案。

### 调用示例

```bash
curl -s -X POST http://localhost:8000/v1/fleet/charge \
  -H 'Content-Type: application/json' \
  -d '{
    "stations": ["A", "B", "C", "D"],
    "trips": [
      {"id": "T1", "stops": [
        {"station": "A", "arrival": 0,   "departure": 60},
        {"station": "B", "arrival": 120, "departure": 120}
      ]},
      {"id": "T2", "stops": [
        {"station": "B", "arrival": 150, "departure": 150},
        {"station": "C", "arrival": 220, "departure": 220}
      ]},
      {"id": "T3", "stops": [
        {"station": "C", "arrival": 230, "departure": 230},
        {"station": "D", "arrival": 300, "departure": 300}
      ]}
    ],
    "trip_ids": ["T1", "T2", "T3"],
    "consumptions": {"T1": 10, "T2": 20, "T3": 5},
    "capacity": 30,
    "initial_charge": 15,
    "chargers": [
      {"station": "B", "rate": 2, "price_cents": 5},
      {"station": "C", "rate": 3, "price_cents": 1}
    ]
  }'
```

T1 后电量剩 5，T2 需 20，故在 B 至少充 15（5 分/单位）；T3 需 5，在更便宜的 C
（1 分/单位）充 5 即可。若在 B 多充 5 以跳过 C，电费为 20×5 = 100，高于
15×5 + 5×1 = 80，故返回（`200 OK`）：

```json
{"feasible": true, "total_cents": 80, "charges": [15, 5]}
```

### 响应字段

| 字段 | 说明 |
| --- | --- |
| `feasible` | 是否存在完成全链的补电方案 |
| `total_cents` | 最低总电费（分）（不可行时为 `null`） |
| `charges` | 每次两班间停站的充电量，按接续顺序排列（`charges[i]` 为第 `i` 班与第 `i+1` 班之间的充电量；不可行时为 `[]`） |

### 无法完成

任何电量分配都无法完成全链时（如首班耗电超过初始电量、中间站未配置充电站、
`rate × 停站分钟` 与容量上限都不足以补足等）返回 `200` 与空方案：

```json
{"feasible": false, "total_cents": null, "charges": []}
```

### 补电接口的 422 情形

除 `stations` / `trips` 本身的校验外（与 `/v1/journey` 一致），以下情况也返回 `422`：

- `trip_ids` 数量不在 1～12、班次 ID 重复或引用不存在的班次；
- 相邻两班不在同一站接续，或后班首站发车早于前班末站到达；
- `consumptions` 未恰好覆盖链上班次（缺班、多出链外班次），或耗电量超出 1～60；
- `capacity` 超出 1～60、`initial_charge` 为负或超过 `capacity`；
- `chargers` 超过 40 个、`station` 重复或不在 `stations` 中、`rate` 超出 1～60、
  `price_cents` 为负；
- 字段缺失、出现多余字段或类型错误等。

## 里程检修：`POST /v1/fleet/inspect`

`stations`、`trips` 的格式与约束与 `/v1/journey` 完全相同；请求另给一条**既定班次链**
（`trip_ids`，按执行顺序排列）、每班里程 `trip_km`、里程上限与车间列表 `workshops`，
求完成全链的**检修方案**。

| 字段 | 类型 | 约束 |
| --- | --- | --- |
| `trip_ids` | `string[]` | 1～40 个，按执行顺序排列且**互不相同**，每个都必须存在于 `trips` |
| `trip_km` | `object` | 键必须**恰好**覆盖链上班次 ID（缺班或多出链外班次均 422），值为 1～300 的每班里程（公里） |
| `limit_km` | `int` | 1～300，检修之间的累计里程上限 |
| `initial_km` | `int` | 0～`limit_km`，出发时的已累计里程 |
| `workshops` | `object[]` | 0～40 个车间，`station` **互不相同**且必须出现在 `stations` 中 |
| `workshops[].open` / `close` | `int` | 0～2879，车间开放区间 `[open, close)`，必须 `open < close` |
| `workshops[].duration_minutes` | `int` | 1～120，一次检修的工时分钟 |
| `workshops[].cost_cents` | `int` | 非负整数，一次检修的费用（分） |

### 接续与检修规则

- 班次链按 `trip_ids` 顺序执行：相邻两班必须在**同一站**接续（前班末站 = 后班首站），
  且后班首站发车**不得早于**前班末站到达（边界相等视为合法）；接续违规返回 422；
- 车辆出发时已累计 `initial_km`；每行驶一班累加该班里程，**每班结束后**累计里程不得
  超过 `limit_km`（边界相等视为合法）；
- **仅在两班之间**可以检修（首班之前、末班之后以及班次途中均不检修），且该接续站
  必须已配置车间（未配置的站不能检修）；
- 一次检修的工时（`duration_minutes` 分钟）必须**完整落在**「前班末站到达至后班首站
  发车」与车间开放区间 `[open, close)` 的交集内，即
  `max(到达, open) + 工时 ≤ min(发车, close)`（边界相等视为合法）；
- 检修后累计里程**归零**；每次检修费用为该车间的 `cost_cents`，总费用为各次之和；
- 优选顺序：① **检修次数最少**；② 并列时**总费用最低**；③ 仍相同取**接续索引序列**
  （从 0 起、按接续顺序排列）**字典序最小**的方案。

### 调用示例

```bash
curl -s -X POST http://localhost:8000/v1/fleet/inspect \
  -H 'Content-Type: application/json' \
  -d '{
    "stations": ["A", "B", "C", "D"],
    "trips": [
      {"id": "T1", "stops": [
        {"station": "A", "arrival": 0,   "departure": 60},
        {"station": "B", "arrival": 120, "departure": 120}
      ]},
      {"id": "T2", "stops": [
        {"station": "B", "arrival": 150, "departure": 150},
        {"station": "C", "arrival": 220, "departure": 220}
      ]},
      {"id": "T3", "stops": [
        {"station": "C", "arrival": 240, "departure": 240},
        {"station": "D", "arrival": 300, "departure": 300}
      ]}
    ],
    "trip_ids": ["T1", "T2", "T3"],
    "trip_km": {"T1": 120, "T2": 150, "T3": 100},
    "limit_km": 200,
    "initial_km": 50,
    "workshops": [
      {"station": "B", "open": 100, "close": 200, "duration_minutes": 20, "cost_cents": 300},
      {"station": "C", "open": 200, "close": 250, "duration_minutes": 20, "cost_cents": 100}
    ]
  }'
```

T1 后累计 50 + 120 = 170 公里；T2 后将达 320 超过 200，必须在 B 检修（停站
[120, 150] 与开放 [100, 200) 的交集为 [120, 150)，120 + 20 = 140 ≤ 150 可行，
300 分）；归零后 T2 结束为 150，T3 后 250 再次超限，必须在 C 检修（交集
[220, 240)，220 + 20 = 240 恰为边界可行，100 分）；T3 结束为 100 ≤ 200。
共 2 次检修、400 分，返回（`200 OK`）：

```json
{
  "feasible": true,
  "inspections": [
    {"connection_index": 0, "station": "B"},
    {"connection_index": 1, "station": "C"}
  ],
  "total_cents": 400
}
```

### 响应字段

| 字段 | 说明 |
| --- | --- |
| `feasible` | 是否存在完成全链的检修方案 |
| `inspections` | 每次检修的接续与站点，按接续顺序排列（不可行时为 `[]`） |
| `inspections[].connection_index` | 检修发生的接续索引（第 `i` 班与第 `i+1` 班之间，**从 0 开始**） |
| `inspections[].station` | 检修所在站点（即该接续的接续站） |
| `total_cents` | 最低总费用（分）（不可行时为 `null`） |

### 无法完成

任何检修安排都无法完成全链时（如某班自身里程已超过上限、首班前累计里程过高、
需要检修的接续站未配置车间、停站与开放区间的交集容纳不下完整工时等）返回
`200` 与空方案：

```json
{"feasible": false, "inspections": [], "total_cents": null}
```

### 检修接口的 422 情形

除 `stations` / `trips` 本身的校验外（与 `/v1/journey` 一致），以下情况也返回 `422`：

- `trip_ids` 数量不在 1～40、班次 ID 重复或引用不存在的班次；
- 相邻两班不在同一站接续，或后班首站发车早于前班末站到达；
- `trip_km` 未恰好覆盖链上班次（缺班、多出链外班次），或里程超出 1～300；
- `limit_km` 超出 1～300、`initial_km` 为负或超过 `limit_km`；
- `workshops` 超过 40 个、`station` 重复或不在 `stations` 中、`open ≥ close` 或
  开放区间超出 0～2879、`duration_minutes` 超出 1～120、`cost_cents` 为负；
- 字段缺失、出现多余字段或类型错误等。

## 团体订座：`POST /v1/seats/allocate`

`stations`、`trips` 的格式与约束与 `/v1/journey` 完全相同；请求另含每班座位容量
`capacity`、换乘缓冲 `transfer_buffer` 与一组团体预订 `bookings`。

| 字段 | 类型 | 约束 |
| --- | --- | --- |
| `capacity` | `object` | 键必须**恰好**覆盖每个班次 ID（缺班或多出未知班次均 422），值为正整数座位容量 |
| `transfer_buffer` | `int` | 1～60 分钟换乘缓冲 |
| `bookings` | `object[]` | 1～18 笔，`id` 为唯一非空字符串 |
| `bookings[].passengers` | `int` | 正整数，该团乘客数 |
| `bookings[].rides` | `object[]` | 1～3 段，按乘坐顺序排列 |
| `bookings[].rides[].trip_id` | `string` | 必须是 `trips` 中存在的班次 ID |
| `bookings[].rides[].board_index` / `alight_index` | `int` | 停站索引（**从 0 开始**）；同一班次内必须 `board_index < alight_index`，且 `board_index` 不能是末站 |

### 预订乘车序列规则（违规返回 422）

- 每段在对应班次内**上车索引严格小于下车索引**；
- 相邻两段必须**换乘不同班次**；
- 前段下车站必须等于后段上车站；
- 后段发车时间 ≥ 前段到达时间 + `transfer_buffer`（边界相等视为合法）。

### 订座规则

- 每笔预订**整单接纳或整单拒绝**，不得拆分；
- 被接纳预订的乘客占用其所乘各段**每一个相邻停站区间**的座位（乘坐区间
  `[board_index, alight_index)` 内的每一区间都计入）；
- 每个班次的每个相邻停站区间上，所有被接纳预订的乘客总数不得超过该班次的
  `capacity`（边界相等视为合法）；
- 优选顺序：① **接纳乘客总数最大**；② 并列时取**接纳 ID 升序列表字典序最小**的方案
  （列表逐项比较，公共前缀较短者更小）。

### 调用示例

```bash
curl -s -X POST http://localhost:8000/v1/seats/allocate \
  -H 'Content-Type: application/json' \
  -d '{
    "stations": ["A", "B", "C", "D"],
    "trips": [
      {"id": "T1", "stops": [
        {"station": "A", "arrival": 0,   "departure": 60},
        {"station": "B", "arrival": 120, "departure": 125},
        {"station": "C", "arrival": 200, "departure": 200}
      ]},
      {"id": "T2", "stops": [
        {"station": "C", "arrival": 210, "departure": 215},
        {"station": "D", "arrival": 300, "departure": 300}
      ]}
    ],
    "capacity": {"T1": 5, "T2": 2},
    "transfer_buffer": 10,
    "bookings": [
      {"id": "G1", "passengers": 4, "rides": [
        {"trip_id": "T1", "board_index": 0, "alight_index": 2}
      ]},
      {"id": "G2", "passengers": 3, "rides": [
        {"trip_id": "T1", "board_index": 0, "alight_index": 1}
      ]},
      {"id": "G3", "passengers": 2, "rides": [
        {"trip_id": "T1", "board_index": 1, "alight_index": 2},
        {"trip_id": "T2", "board_index": 0, "alight_index": 1}
      ]}
    ]
  }'
```

T1 容量为 5：G1（4 人，占 A-B、B-C 两区间）与 G2（3 人，占 A-B）在 A-B 区间冲突
（4 + 3 > 5），G1 与 G3（2 人，占 B-C）在 B-C 区间冲突（4 + 2 > 5）；G2 + G3 可行，
共 5 人，优于任何含 G1 的方案，故返回（`200 OK`）：

```json
{
  "accepted_ids": ["G2", "G3"],
  "rejected_ids": ["G1"],
  "accepted_passengers": 5
}
```

### 响应字段

| 字段 | 说明 |
| --- | --- |
| `accepted_ids` | 被接纳的预订 ID，升序排列 |
| `rejected_ids` | 被拒绝的预订 ID，升序排列 |
| `accepted_passengers` | 被接纳乘客总数（最大可达值） |

### 订座接口的 422 情形

除 `stations` / `trips` 本身的校验外，以下情况也返回 `422`：

- `capacity` 未覆盖某个班次 ID、包含未知班次 ID，或容量不是正整数；
- 预订 ID 重复、`passengers` 非正整数、`bookings` 数量超出 1～18；
- 某段引用不存在的 `trip_id`，或上下车索引越界、`board_index >= alight_index`；
- 相邻两段使用同一班次、下车站与下段上车站不一致、或换乘缓冲不足；
- 单笔预订段数超出 1～3、`transfer_buffer` 超出 1～60、字段缺失或类型错误等。

## 最少停运：`POST /v1/network/disruption`

`stations`、`trips` 的格式与约束与 `/v1/journey` 基本相同，但本接口**班次限 1～16 个**；
请求另给起点、终点与出行时间窗。

| 字段 | 类型 | 约束 |
| --- | --- | --- |
| `origin` / `destination` | `string` | 必须在 `stations` 中，且二者不同 |
| `earliest_departure` | `int` | 0～2879，首次上车的最早发车分钟 |
| `arrival_deadline` | `int` | 0～2879，到达终点的截止分钟；**不得早于** `earliest_departure` |
| `transfer_buffer` | `int` | 1～60 分钟换乘缓冲 |

### 停运规则

- 每次停运移除**整趟班次**；余下班次的停站与时刻不变；
- 可达性按现有规则判断：首次上车发车时间 `≥ earliest_departure`，同车续乘不计换乘也
  不需要缓冲，改乘别的班次时其发车时间 ≥ 前一班下车时间 + `transfer_buffer`；
- 「按时可达」指存在可行行程使其到达终点时间 `≤ arrival_deadline`（边界相等视为按时）；
- 优选顺序：① **停运班次数最少**；② 并列时取**停运 ID 升序列表字典序最小**的方案
  （`cancelled_ids` 按升序返回，列表逐项比较，公共前缀较短者更小）；
- 原时刻表本就不可按时到达时，返回 `baseline_reachable=false`、`minimum_cancellations=0`
  与空列表（无需停运）。

### 调用示例

```bash
curl -s -X POST http://localhost:8000/v1/network/disruption \
  -H 'Content-Type: application/json' \
  -d '{
    "stations": ["A", "B", "C", "D"],
    "trips": [
      {"id": "T1", "stops": [
        {"station": "A", "arrival": 0,   "departure": 60},
        {"station": "B", "arrival": 120, "departure": 125},
        {"station": "C", "arrival": 200, "departure": 200}
      ]},
      {"id": "T2", "stops": [
        {"station": "C", "arrival": 210, "departure": 215},
        {"station": "D", "arrival": 300, "departure": 300}
      ]},
      {"id": "T3", "stops": [
        {"station": "A", "arrival": 0,   "departure": 70},
        {"station": "D", "arrival": 250, "departure": 250}
      ]}
    ],
    "origin": "A",
    "destination": "D",
    "earliest_departure": 0,
    "arrival_deadline": 300,
    "transfer_buffer": 10
  }'
```

可行行程有两条：T1+T2（300 到达）与 T3（250 到达）。只停一班无法同时破坏两者；
停 `{T1, T3}` 或 `{T2, T3}` 均可，字典序最小者为 `["T1", "T3"]`，故返回（`200 OK`）：

```json
{
  "baseline_reachable": true,
  "minimum_cancellations": 2,
  "cancelled_ids": ["T1", "T3"]
}
```

### 响应字段

| 字段 | 说明 |
| --- | --- |
| `baseline_reachable` | 原时刻表能否不晚于 `arrival_deadline` 到达终点 |
| `minimum_cancellations` | 使其失去按时可达性的最少停运班次数 |
| `cancelled_ids` | 停运班次 ID，升序排列（并列方案中字典序最小） |

### 停运接口的 422 情形

除 `stations` / `trips` 本身的校验外（本接口班次限 1～16 个），以下情况也返回 `422`：

- `origin` / `destination` 相同，或不在 `stations` 中；
- `arrival_deadline` 早于 `earliest_departure`；
- `earliest_departure` / `arrival_deadline` 超出 0～2879，`transfer_buffer` 超出 1～60，
  字段缺失或类型错误等。

## GTFS 转换：`POST /v1/gtfs`

把一个 GTFS 子集按指定服务日期转换为当日网络（`stations` + `trips`），结果与
`/v1/journey` 等接口的 `stations` / `trips` 字段完全同构，可直接用于规划行程。

| 字段 | 类型 | 约束 |
| --- | --- | --- |
| `date` | `string` | 服务日期，`YYYYMMDD`，必须是真实存在的日期 |
| `files` | `object` | 文件名 → CSV 文本；必须含 `stops.txt`、`trips.txt`、`stop_times.txt`、`calendar.txt`，可选 `calendar_dates.txt`、`frequencies.txt`；其余文件忽略 |

### 文件与列

每个文件首行为表头；只读取下列必需列（多余列忽略），缺少必需文件/列、表头列名重复或
行列数不齐均返回 422：

- `stops.txt`：`stop_id`（唯一、非空）；
- `trips.txt`：`trip_id`（唯一）、`service_id`（必须出现在 `calendar` 或 `calendar_dates` 中）；
- `stop_times.txt`：`trip_id`、`arrival_time`、`departure_time`、`stop_id`、`stop_sequence`；
- `calendar.txt`：`service_id`（唯一）、`monday`…`sunday`（`0`/`1`）、`start_date`、`end_date`；
- `calendar_dates.txt`：`service_id`、`date`、`exception_type`（`1`=加开、`2`=停开；
  同一 `(service_id, date)` 不得重复）；
- `frequencies.txt`：`trip_id`、`start_time`、`end_time`、`headway_secs`、`exact_times`。

### 转换规则

- **日历**：服务在 `start_date ≤ date ≤ end_date` 且当日星期标志为 `1` 时生效；
  `calendar_dates` 例外优先：`1` 加开（即使日历不生效、或服务仅在此定义）、`2` 停开；
- **停站**：每个班次的停站按**数字** `stop_sequence` 排序（`10` 排在 `9` 之后）；
- **时刻**：所有时间必须在 `00:00:00`～`47:59:00` 之间且秒必须为 `00`，
  转换为 0～2879 的分钟数（允许 `24:00:00` 之后的跨零点时刻）；
- **频率**：仅支持 `exact_times=1` 且 `headway_secs` 为**正整分钟**（60 的倍数）；
  在 `[start_time, end_time)` 内按 `headway_secs` 步进生成首发时刻，整趟班次相对模板
  首发平移并**替代**模板，班次 ID 为 `原ID@首发分钟`（如 `T1@420`）；无频率的班次保留原 ID；
- **站点**：`stations` 取 `stops.txt` 的 `stop_id`，保持文件顺序；
- 当日无班次运行时返回 `200` 与空 `trips` 列表。

### 整批 422 情形

任何一处违规都使**整批**返回 `422 Unprocessable Entity`：

- 引用错误：`stop_times` / `frequencies` 引用未知 `trip_id`、`stop_times` 引用未知
  `stop_id`、`trips` 引用未定义的 `service_id`；
- 重复键：`stop_id`、`trip_id`、`service_id`、`(service_id, date)`、班次内
  `stop_sequence`、`frequencies` 的 `trip_id`；
- 时序错误：本站 `arrival > departure`、下一站 `arrival` 未晚于上一站 `departure`、
  时间越界或秒非 `00`、`start_date > end_date`、频率 `start_time ≥ end_time`；
- 结果超出现有网络限制：站点数不在 2～40、当日班次超过 100、当日运行班次的停站数
  不在 2～20、频率平移后时刻超出 0～2879。

### 调用示例

```bash
curl -s -X POST http://localhost:8000/v1/gtfs \
  -H 'Content-Type: application/json' \
  -d '{
    "date": "20260925",
    "files": {
      "stops.txt": "stop_id,stop_name\nS1,Alpha\nS2,Beta\nS3,Gamma\nS4,Delta\n",
      "trips.txt": "route_id,service_id,trip_id\nR1,WD,T1\nR1,WD,T2\n",
      "stop_times.txt": "trip_id,arrival_time,departure_time,stop_id,stop_sequence\nT1,08:00:00,08:00:00,S1,1\nT1,08:30:00,08:35:00,S2,2\nT1,09:10:00,09:10:00,S3,3\nT2,09:20:00,09:20:00,S3,1\nT2,10:00:00,10:00:00,S4,2\n",
      "calendar.txt": "service_id,monday,tuesday,wednesday,thursday,friday,saturday,sunday,start_date,end_date\nWD,1,1,1,1,1,0,0,20260101,20261231\n"
    }
  }'
```

2026-09-25 是周五，服务 `WD` 生效，响应（`200 OK`）：

```json
{
  "stations": ["S1", "S2", "S3", "S4"],
  "trips": [
    {"id": "T1", "stops": [
      {"station": "S1", "arrival": 480, "departure": 480},
      {"station": "S2", "arrival": 510, "departure": 515},
      {"station": "S3", "arrival": 550, "departure": 550}
    ]},
    {"id": "T2", "stops": [
      {"station": "S3", "arrival": 560, "departure": 560},
      {"station": "S4", "arrival": 600, "departure": 600}
    ]}
  ]
}
```

### 转换后规划行程

转换结果补上起终点与换乘缓冲即可直接交给 `/v1/journey`（下面用 `jq` 合并字段）：

```bash
network=$(curl -s -X POST http://localhost:8000/v1/gtfs \
  -H 'Content-Type: application/json' \
  -d '{
    "date": "20260925",
    "files": {
      "stops.txt": "stop_id,stop_name\nS1,Alpha\nS2,Beta\nS3,Gamma\nS4,Delta\n",
      "trips.txt": "route_id,service_id,trip_id\nR1,WD,T1\nR1,WD,T2\n",
      "stop_times.txt": "trip_id,arrival_time,departure_time,stop_id,stop_sequence\nT1,08:00:00,08:00:00,S1,1\nT1,08:30:00,08:35:00,S2,2\nT1,09:10:00,09:10:00,S3,3\nT2,09:20:00,09:20:00,S3,1\nT2,10:00:00,10:00:00,S4,2\n",
      "calendar.txt": "service_id,monday,tuesday,wednesday,thursday,friday,saturday,sunday,start_date,end_date\nWD,1,1,1,1,1,0,0,20260101,20261231\n"
    }
  }')

echo "$network" \
  | jq '. + {origin: "S1", destination: "S4", earliest_departure: 0, transfer_buffer: 10}' \
  | curl -s -X POST http://localhost:8000/v1/journey \
      -H 'Content-Type: application/json' -d @-
```

响应（`200 OK`）：

```json
{
  "reachable": true,
  "arrival_time": 600,
  "transfers": 1,
  "trip_ids": ["T1", "T2"],
  "segments": [
    {"trip_id": "T1", "from": "S1", "to": "S3", "departure": 480, "arrival": 550},
    {"trip_id": "T2", "from": "S3", "to": "S4", "departure": 560, "arrival": 600}
  ]
}
```

## 按客流编排班次：`POST /v1/timetable/design`

为一条线路设计班次表：所有班次共用同一停站模式（`stops`，以**相对首发的到发偏移**
表示），从候选首发分钟 `candidates` 中**恰好选择 `trip_count` 个**发车时刻，使客流
`demand` 的**人数加权总等待分钟**最小。

| 字段 | 类型 | 约束 |
| --- | --- | --- |
| `stops` | `object[]` | 2～20 个停站，按行驶顺序排列；`station` 为非空字符串且**互不重复** |
| `stops[].arrival` / `departure` | `int` | 0～2879 的偏移分钟；**首站两者都为 0**；本站 `arrival ≤ departure`；下一站 `arrival` 必须**晚于**本站 `departure` |
| `candidates` | `int[]` | 1～60 个**互不相同**的候选首发分钟，每个 0～2879；候选首发 + 最大偏移（末站 `departure` 偏移）≤ 2879 |
| `demand` | `object[]` | 1～60 批乘客（允许同分钟多批） |
| `demand[].arrival` | `int` | 0～2879，该批乘客到达首站的分钟 |
| `demand[].passengers` | `int` | 正整数，该批人数 |
| `trip_count` | `int` | 1～12，必须选择的首发时刻数量 |
| `min_interval` | `int` | 0～120，相邻首发（升序）的最小间隔分钟 |
| `max_wait` | `int` | 0～120，每批乘客的最长等车分钟 |

### 编排规则

- 从 `candidates` 中**恰好**选出 `trip_count` 个首发分钟；升序后相邻两者之差
  ≥ `min_interval`（边界相等视为合法）；
- 每批乘客乘坐**不早于其到站分钟的第一班**（发车分钟 ≥ 到站分钟，同分钟到达即上车、
  等待为 0），且实际等待 `发车 − 到站` ≤ `max_wait`（边界相等视为合法）；
  没有任何班次可乘（到站晚于末班）或等待必然超限时，该选择方案不可行；
- 优选顺序：① **人数加权总等待** `Σ passengers × (发车 − 到站)` 最小；
  ② 并列时取**首发升序列表字典序最小**的方案。

### 调用示例

```bash
curl -s -X POST http://localhost:8000/v1/timetable/design \
  -H 'Content-Type: application/json' \
  -d '{
    "stops": [
      {"station": "A", "arrival": 0,  "departure": 0},
      {"station": "B", "arrival": 20, "departure": 25},
      {"station": "C", "arrival": 45, "departure": 45}
    ],
    "candidates": [100, 120, 140, 160, 180, 200],
    "demand": [
      {"arrival": 95,  "passengers": 3},
      {"arrival": 130, "passengers": 2},
      {"arrival": 170, "passengers": 4}
    ],
    "trip_count": 2,
    "min_interval": 30,
    "max_wait": 45
  }'
```

170 分到站的乘客要求末班 ∈ [170, 215]；若首班早于 130，130 分到站的乘客只能乘末班，
等待至少 50 分钟超限；故首班只能是 140（95 分到的乘客等 45 分钟，恰为上限），末班取
180 比 200 更省等待。总等待 `3×45 + 2×10 + 4×10 = 195`，返回（`200 OK`）：

```json
{
  "feasible": true,
  "departures": [140, 180],
  "total_wait": 195,
  "stations": ["A", "B", "C"],
  "trips": [
    {"id": "design@140", "stops": [
      {"station": "A", "arrival": 140, "departure": 140},
      {"station": "B", "arrival": 160, "departure": 165},
      {"station": "C", "arrival": 185, "departure": 185}
    ]},
    {"id": "design@180", "stops": [
      {"station": "A", "arrival": 180, "departure": 180},
      {"station": "B", "arrival": 200, "departure": 205},
      {"station": "C", "arrival": 225, "departure": 225}
    ]}
  ]
}
```

返回的 `stations`、`trips` 与 `/v1/journey` 等接口的网络格式完全同构（班次 ID 为
`design@首发分钟`），补上起终点与换乘缓冲即可直接规划行程：

```bash
design=$(curl -s -X POST http://localhost:8000/v1/timetable/design \
  -H 'Content-Type: application/json' \
  -d '{
    "stops": [
      {"station": "A", "arrival": 0,  "departure": 0},
      {"station": "B", "arrival": 20, "departure": 25},
      {"station": "C", "arrival": 45, "departure": 45}
    ],
    "candidates": [100, 120, 140, 160, 180, 200],
    "demand": [
      {"arrival": 95,  "passengers": 3},
      {"arrival": 130, "passengers": 2},
      {"arrival": 170, "passengers": 4}
    ],
    "trip_count": 2,
    "min_interval": 30,
    "max_wait": 45
  }')

echo "$design" \
  | jq '{stations, trips} + {origin: "A", destination: "C", earliest_departure: 0, transfer_buffer: 1}' \
  | curl -s -X POST http://localhost:8000/v1/journey \
      -H 'Content-Type: application/json' -d @-
```

### 响应字段

| 字段 | 说明 |
| --- | --- |
| `feasible` | 是否存在满足全部约束的选班方案 |
| `departures` | 选中的首发分钟，升序（无解时为 `[]`） |
| `total_wait` | 人数加权总等待分钟（无解时为 `null`） |
| `stations` | 线路站点，按行驶顺序（无解时为 `[]`） |
| `trips` | 每个首发分钟一个班次，ID 为 `design@首发分钟`，时刻为偏移平移结果（无解时为 `[]`） |

### 无可行方案

约束无法满足时（如 `trip_count` 超过候选数、某批乘客到站晚于一切候选、等待必然超过
`max_wait`、最小间隔下凑不齐班次等）返回 `200` 与空结果：

```json
{"feasible": false, "departures": [], "total_wait": null, "stations": [], "trips": []}
```

### 编排接口的 422 情形

- `stops` 数量不在 2～20、站点为空或重复、首站偏移不为 0、本站 `arrival > departure`、
  下一站 `arrival` 未晚于本站 `departure`、偏移超出 0～2879；
- `candidates` 数量不在 1～60、分钟重复、超出 0～2879，或候选首发 + 最大偏移 > 2879；
- `demand` 数量不在 1～60、`arrival` 超出 0～2879、`passengers` 非正整数；
- `trip_count` 超出 1～12、`min_interval` / `max_wait` 超出 0～120；
- 字段缺失、出现多余字段或类型错误等。

## 换乘站压车：`POST /v1/transfers/hold`

`stations`、`trips` 的格式与约束与 `/v1/journey` 完全相同；请求另给换乘站 `station`、
该站的离站班次列表 `outgoing`（可被压车候车的班次）、换乘团体列表 `groups`、换乘缓冲
`transfer_buffer`、单班最大延后 `max_hold` 与发车间隔下限 `min_interval`。

| 字段 | 类型 | 约束 |
| --- | --- | --- |
| `station` | `string` | 必须出现在 `stations` 中 |
| `outgoing` | `object[]` | 1～10 个，`trip_id` **互不相同**，且按本站发车时间**升序**排列 |
| `outgoing[].trip_id` | `string` | 必须是 `trips` 中存在的班次 ID |
| `outgoing[].stop_index` | `int` | 停站索引（**从 0 开始**），必须指向该班次**位于换乘站**的停站 |
| `outgoing[].onboard` | `int` | 非负整数，该班次到达换乘站时已在车上的乘客数 |
| `groups` | `object[]` | 1～30 个，`id` 为唯一非空字符串 |
| `groups[].actual_arrival` | `int` | 0～2879，该团实际到达换乘站的分钟 |
| `groups[].trip_id` | `string` | 目标班次，必须是 `outgoing` 中的班次之一 |
| `groups[].passengers` | `int` | 正整数，该团人数 |
| `transfer_buffer` | `int` | 1～60 分钟换乘缓冲 |
| `max_hold` | `int` | 0～30 分钟，单班发车最多可延后的分钟数 |
| `min_interval` | `int` | 0～60 分钟，相邻离站发车的最小间隔（**原时刻**必须已满足） |

### 压车规则

- 每个离站班次在换乘站的发车可延后整数分钟 `hold`，`0 ≤ hold ≤ max_hold`；
- 延后使该班次**后续各站**的到发时刻同步顺延同一分钟数，且顺延后的全部时刻
  （含本站发车）不得超过 2879，超出即该 `hold` 不可选；
- 调整后相邻离站发车（按 `outgoing` 顺序）之差仍须 ≥ `min_interval`（边界相等视为合法）；
- 团体 `g` **接上**其目标班次当且仅当 `actual_arrival + transfer_buffer ≤ 调整后发车`
  （边界相等视为接上）；
- 优选顺序：① **接上乘客总数**最大；② 并列时**在车延误总量**
  `Σ onboard × hold` 最小；③ 仍相同取**延后列表**（按 `outgoing` 顺序）**字典序最小**者；
- 全部不延后恒为可行解，故本接口总有结果返回。

### 调用示例

```bash
curl -s -X POST http://localhost:8000/v1/transfers/hold \
  -H 'Content-Type: application/json' \
  -d '{
    "stations": ["A", "B", "C", "D", "E"],
    "trips": [
      {"id": "T1", "stops": [
        {"station": "B", "arrival": 150, "departure": 155},
        {"station": "C", "arrival": 195, "departure": 200},
        {"station": "D", "arrival": 240, "departure": 240}
      ]},
      {"id": "T2", "stops": [
        {"station": "A", "arrival": 180, "departure": 182},
        {"station": "C", "arrival": 210, "departure": 215},
        {"station": "E", "arrival": 260, "departure": 260}
      ]},
      {"id": "T3", "stops": [
        {"station": "C", "arrival": 230, "departure": 235},
        {"station": "D", "arrival": 270, "departure": 270}
      ]}
    ],
    "station": "C",
    "outgoing": [
      {"trip_id": "T1", "stop_index": 1, "onboard": 5},
      {"trip_id": "T2", "stop_index": 1, "onboard": 2},
      {"trip_id": "T3", "stop_index": 0, "onboard": 9}
    ],
    "groups": [
      {"id": "G1", "actual_arrival": 190, "trip_id": "T1", "passengers": 4},
      {"id": "G2", "actual_arrival": 208, "trip_id": "T1", "passengers": 6},
      {"id": "G3", "actual_arrival": 212, "trip_id": "T2", "passengers": 3}
    ],
    "transfer_buffer": 5,
    "max_hold": 20,
    "min_interval": 10
  }'
```

G1（190 + 5 ≤ 200）不延后即能接上；G2 要求 T1 延后 ≥ 13 分钟；T1 延后 13 分钟后
发车 213，间隔约束迫使 T2 延后 ≥ 8（215 + 8 ≥ 213 + 10），这同时让 G3（212 + 5 ≤ 223）
接上；T3 无需延后（235 ≥ 223 + 10）。三团共 13 人全部接上，在车延误
`5×13 + 2×8 + 9×0 = 81`，返回（`200 OK`）：

```json
{
  "holds": [
    {"trip_id": "T1", "delay_minutes": 13},
    {"trip_id": "T2", "delay_minutes": 8},
    {"trip_id": "T3", "delay_minutes": 0}
  ],
  "connected_ids": ["G1", "G2", "G3"],
  "connected_passengers": 13,
  "onboard_delay_minutes": 81
}
```

### 响应字段

| 字段 | 说明 |
| --- | --- |
| `holds` | 每个离站班次的延后方案，与 `outgoing` 同序 |
| `holds[].trip_id` | 班次 ID |
| `holds[].delay_minutes` | 该班次在换乘站发车的延后分钟数（0～`max_hold`） |
| `connected_ids` | 接上目标班次的团体 ID，升序排列 |
| `connected_passengers` | 接上乘客总数（最大可达值） |
| `onboard_delay_minutes` | 在车延误总量 `Σ onboard × hold`（接上人数最大前提下的最小值） |

### 压车接口的 422 情形

除 `stations` / `trips` 本身的校验外（与 `/v1/journey` 一致），以下情况也返回 `422`：

- `station` 不在 `stations` 中；
- `outgoing` 数量不在 1～10、`trip_id` 重复或不存在、`stop_index` 越界或未指向换乘站、
  `onboard` 为负、未按本站发车升序排列、原发车间隔已小于 `min_interval`；
- `groups` 数量不在 1～30、`id` 重复、`actual_arrival` 超出 0～2879、目标班次不在
  `outgoing` 中、`passengers` 非正整数；
- `transfer_buffer` 超出 1～60、`max_hold` 超出 0～30、`min_interval` 超出 0～60；
- 字段缺失、出现多余字段或类型错误等。

## 本地开发（不用 Docker）

```bash
python3 -m venv .venv
. .venv/bin/activate
pip install -r requirements.txt
uvicorn app.main:app --reload --port 8000
```

运行测试（需额外安装 `pytest` 与 `httpx`）：

```bash
pip install pytest httpx
pytest
```

## 项目结构

```
app/
  main.py      # FastAPI 应用与路由（/v1/journey、/v1/journey/meet、/v1/journey/reliability、/v1/fares/quote、/v1/fleet/assign、/v1/fleet/charge、/v1/fleet/inspect、/v1/seats/allocate、/v1/network/disruption、/v1/gtfs、/v1/timetable/design、/v1/transfers/hold）
  models.py    # 请求/响应模型与全部输入校验（违规即 422）
  planner.py   # 纯标准库的最早到达规划器（带换乘缓冲与字典序决胜）
  meet.py      # 纯标准库的两人会合规划器（全到达时刻标签 + 等待窗口配对）
  reliability.py# 纯标准库的准时概率计算（链式 sum-product + 整数精确约分）
  fares.py     # 纯标准库的最低票价分段动态规划（票数与票券 ID 序列决胜）
  fleet.py     # 纯标准库的最少车辆分配（DAG 最小路径覆盖 + 二分图匹配）
  charge.py    # 纯标准库的电动车补电规划（按电量级别的动态规划 + 字典序决胜）
  inspect.py   # 纯标准库的里程检修规划（按累计里程的动态规划 + 字典序决胜）
  seats.py     # 纯标准库的团体订座分配（分支限界 + 字典序决胜）
  disruption.py# 纯标准库的最少停运分析（命中集递归 + 字典序决胜）
  gtfs.py      # 纯标准库的 GTFS 子集 → 当日网络转换器（日历/例外/频率展开）
  design.py    # 纯标准库的按客流班次编排（候选时刻动态规划 + 字典序决胜）
  hold.py      # 纯标准库的换乘站压车优化（按延后取值的动态规划 + 字典序决胜）
tests/
  test_journey.py
  test_meet.py
  test_reliability.py
  test_fares.py
  test_fleet.py
  test_charge.py
  test_inspect.py
  test_seats.py
  test_disruption.py
  test_gtfs.py
  test_design.py
  test_hold.py
Dockerfile
docker-compose.yml
requirements.txt
```

## 实现说明

`/v1/journey` 的规划器把「（所在站点， 最后乘坐的班次）」作为状态、把「（时间， 换乘数， 班次 ID
序列）」作为标签做多标签 Dijkstra：每条边表示乘坐某班次从当前停站到下一停站，
同车续乘自然形成零换乘边；换乘边要求发车时间 ≥ 下车时间 + 缓冲。堆的优先级
与题目要求的优选顺序一致，因此第一次弹出终点标签即为最优解；每个状态只保留
帕累托最优标签，规模有界（站点 ≤ 40、班次 ≤ 100、停站 ≤ 20）。

`/v1/journey/meet` 对每名旅客按规划器同一边规则做「（站点， 最后乘坐班次）」状态的多标签
搜索，但**保留每个状态的全部不同到达时刻**（同一（状态， 到达时刻）只保留换乘最少、再按
班次 ID 序列最小的标签）：会合目标里的「等待时间」使得到达更晚的标签可能反而更优
（会合时刻不变而等待更短），通常的「早到占优」剪枝在此不成立。由于继续行进的可行性
只取决于状态与时刻，同一（状态， 到达时刻）的次优标签对一切后续与配对都被占优；每个
状态的到达时刻都来自班次的停站事件，故标签总数以停站总数（≤ 2000）为界。标签按
（时刻， 换乘数， 班次 ID 序列）的堆序结算，边松弛只来自严格更早的标签，因此每个
（状态， 时刻）首次结算即最优。随后在每一候选会合站（非任一出发站）把两人的标签按到达
时刻合并，在 ±`max_wait` 窗口内枚举配对，按「（会合时刻， 等待时间， 总换乘数， 站点 ID,
第 1 人班次序列， 第 2 人班次序列）」取全局最小——与裁决规则完全一致。

`/v1/journey/reliability` 的约束只耦合相邻两段（换乘缓冲与末段截止），成功事件构成
**链式因子图**：一次前向扫描（链上 sum-product）即可精确累计。处理到第 `i` 段时，
`dp[d]` 记录前 `i` 段所有满足换乘的延误指派中、第 `i` 班延误为 `d` 分钟的权重和——
权重为百分整数，全程整数运算，处理完 `i` 段后总量恰为 `100^i`；向第 `i+1` 段转移时
只保留满足「实际发车 ≥ 前段实际到达 + 缓冲」的前驱延误，最后按截止时间汇总有效状态。
段数 ≤ 12、每班 ≤ 6 个延误结果，计算量为 O(12·6·6)；结果以
`成功权重 / 100^段数` 用最大公约数约分后输出（零概率特判为 `0/1`）。

`/v1/fares/quote` 在通过校验的乘车序列上做**分段动态规划**：令 `dp[i]` 为覆盖前 `i`
段的最优方案，枚举以第 `i` 段结尾的连续块 `[j, i)` 与每种票券（段数 ≤ `max_rides`、
末段发车 − 首段发车 ≤ `valid_minutes`）做转移，标签按
「（总价， 票数， 票券 ID 序列）」比较——与裁决规则完全一致；相邻段发车严格递增，
故有效性检查只需比较块内首末两段的发车时刻。段数 ≤ 20、票种 ≤ 12，计算量有界。

`/v1/fleet/assign` 把每个班次看作顶点：若一辆车完成班次 `i` 后可接续班次 `j`
（`i` 的末站等于 `j` 的首站，且 `j` 的首站发车 ≥ `i` 的末站到达 + `turnaround_minutes`），
则连有向边 `i → j`。边上首班发车时间严格递增，故图为 DAG，最少车辆数即其
**最小路径覆盖**：`班次数 − 最大匹配数`。在「每个班次同时作为前驱与后继」的二分图上，
按（首班发车时间，班次 ID）顺序做确定性的增广路匹配；没有匹配前驱的班次成为各车
首班，沿后继指针还原每辆车的执行序列。班次 ≤ 100，计算量有界。

`/v1/fleet/charge` 沿班次链做**按电量级别的动态规划**：`dp[b]` 记录「处理完前若干班、
当前电量为 `b`」的最优标签（总电费，充电量序列）。首班只能依靠初始电量出发；两班间
停站 `m` 分钟、充电速率 `r` 时，可任选整数充电量 `0 ≤ c ≤ min(r·m, 容量 − b)`，充后
电量满足下一班耗电即转移，代价增加 `c × 本站单价`。每个电量级别只保留最优标签是
精确的：后续可行性只取决于当前电量，而标签比较（电费升序、再按充电量序列字典序）
具有前缀单调性，被占优的前缀不可能导出更优的完整方案。班次 ≤ 12、电量级别 ≤ 61、
每状态充电选择 ≤ 61，计算量有界。

`/v1/fleet/inspect` 沿班次链做**按累计里程的动态规划**：`dp[km]` 记录「处理完前若干班、
距上次检修已累计 `km` 公里」的最优标签（检修次数，总费用，接续索引序列）。每班先累加
本班里程（超过 `limit_km` 的状态淘汰），随后二选一：不检修直接续行，或——当该接续站
车间的开放区间能容纳完整工时（`max(到达, open) + 工时 ≤ min(发车, close)`）——付费
检修并把累计里程归零。每个里程级别只保留最优标签是精确的：后续可行性只取决于当前
累计里程，而标签比较（次数升序、费用升序、再按索引序列字典序）具有前缀单调性，被
占优的前缀不可能导出更优的完整方案。班次 ≤ 40、里程级别 ≤ 301，计算量有界。

`/v1/seats/allocate` 把所有班次的相邻停站区间压平成一组容量约束（每区间容量即该班
`capacity`），每笔预订占用其各段覆盖的区间。由于预订 ≤ 18 笔，对按 ID 升序排列的预订做
**分支限界**：`suffix[k]`（第 `k` 笔起的乘客总数）给出每个节点的上界——上界低于当前
最优即剪枝；上界恰好等于最优时，唯一可能并列的完成方式是「剩余全部接纳」（乘客数为正，
少接任何一笔都会变少），直接一次性检查而不展开子树。优先走「接纳」分支使全部可容纳的
情形退化为单条路径，区间占用量在搜索中增量维护。并列时按「接纳 ID 升序列表」字典序比较，
与裁决规则一致。

`/v1/network/disruption` 把可达性检查写成与规划器同一边规则的「（站点， 最后乘坐班次）」
状态 Dijkstra（按（时间， 用班次数）弹出、剪去晚于截止时间的到达），返回一条可行行程所用
的班次集合。每个状态只保留当前已知最优标签（无论已弹出或仍在堆中），且仅在严格更优时
才更新标签并记录父指针，因此还原出的行程链必然属于该状态的最优标签：晚到达某站的前缀
不会被拼接到只有早到达才能接续的后续边上（例如班次重复停靠同一站时，回程晚到的访问
不会覆盖去程早到、仍能赶上后续班车的父链）。最少停运数即「所有可行行程班次集合」的
**最小命中集**：任取一条可行行程 `J`，任何方案至少停掉 `J` 中一班，故
`h(active) = min_{t in J} 1 + h(active \ {t})`；以在营班次位掩码记忆化（班次 ≤ 16），
每个子集至多求解一次。最少停运数确定后，按 ID 升序贪心构造字典序最小的停运列表：某班
可加入停运集当且仅当剩余网络仍能在剩余预算内被断开（由同一记忆化求解器回答）。

`/v1/gtfs` 的转换器先把各 CSV 文本解析成表并做整批校验（必需文件与列、引用完整性、
键唯一性、时刻格式与范围），再按「日历日期范围 + 星期标志」判定各服务在请求日期是否
生效，并以 `calendar_dates` 例外（1 加开、2 停开）优先覆盖；生效班次的停站按数字
`stop_sequence` 排序并检查站内与站间时序，频率班次（限 `exact_times=1`、正整分钟
`headway_secs`）在 `[start_time, end_time)` 内按头班间隔展开为 `原ID@首发分钟` 的
整体平移实例并替代模板。最后按现有网络限制（站点 2～40、当日班次 ≤ 100、每班停站
2～20、时刻 0～2879）检查转换结果；任何一步违规都抛出 `ValueError`，由 pydantic 转为
整批 422。转换在请求模型的校验阶段完成，因此路由拿到的必然是合法网络。

`/v1/timetable/design` 把候选首发分钟升序后做**动态规划**：`dp[k][i]` 记录「在前 `i`
个候选中恰选 `k` 个、且第 `k` 个首发为 `c[i]`、所有不晚于 `c[i]` 到站的乘客都已上车」
的最优标签（加权总等待，首发列表）。若上一个首发为 `c[p]`，则到站分钟落在
`(c[p], c[i]]` 的乘客都乘 `c[i]`——转移仅在间隔 ≥ `min_interval` 且 `(c[p], c[i] −
max_wait)` 内没有任何到站（否则有人等待超限）时合法；首班的情形同理（此前无车，
更早到站的乘客无人能接）。乘客按到站分钟分桶并维护人数与「人数 × 到站」两个前缀和，
每次转移的等待代价都是 O(1)；最终在所有 `c[i]` 不早于最晚到站的 `dp[K][i]` 中取
「（总等待， 首发列表）」最小者——与裁决规则完全一致。候选 ≤ 60、发车数 ≤ 12，
表中至多 720 个状态、每状态至多 60 个前驱。

`/v1/transfers/hold` 把每个离站班次的延后分钟数看作取值 0～上限的决策变量：间隔约束只
耦合相邻两个班次，而「接上人数」与「在车延误」都按班次可分解，故沿 `outgoing` 顺序做
**动态规划**——`dp[i][h]` 记录「前 `i + 1` 个班次、第 `i` 个延后 `h` 分钟」的最优标签
（接上人数， 在车延误， 延后列表）。班次 `i` 延后 `h` 时，需求阈值
`actual_arrival + transfer_buffer − 原发车` 不超过 `h` 的团体即接上，同时付出
`onboard × h` 的在车延误；转移仅保留满足「调整后相邻发车间隔 ≥ `min_interval`」的前驱。
每个班次的可延后上限还要满足「末站发车 + 延后 ≤ 2879」（时刻沿班次单调不减，故末站即
最大顺延时刻）。标签按「（接上人数， −在车延误）」比较、并列取延后列表字典序最小——
只保留每个 `(i, h)` 的最优前缀是精确的：同一末态下更优的前缀替换进去，对一切后续都只
会更优。离站班次 ≤ 10、延后取值 ≤ 31，表中至多 310 个状态、每状态至多 31 个前驱；
全零延后恒可行，故必有解。
