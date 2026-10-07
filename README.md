# 录音删段交付 API

对节目录音做「删段」交付的 HTTP 服务：上传原始 WAV 后，按**原始采样帧坐标**反复提交保留清单，随时查询换算为**成品帧坐标**的章节，并下载真正裁接好的成品（WAV + 章节 JSON 的 ZIP）。

技术栈：Python · Flask · SQLite · 标准库 `wave` / `zipfile`。

## 快速开始

单条命令启动（Docker 与 Docker Compose）：

```bash
docker compose up --build
```

服务监听 **8000** 端口。数据保存在名为 `app-data` 的卷中（SQLite，WAL 模式），容器重启后节目仍可继续编辑。

本地运行（可选）：

```bash
pip install -r requirements.txt
python app.py                 # 监听 0.0.0.0:8000，可用 PORT 环境变量修改
```

运行测试：

```bash
pip install pytest
python -m pytest tests/ -v
```

## 概念与规则

- **音频规格**：≤ 30 秒、16 kHz、单声道、16 位 PCM WAV（上限 480000 帧）。原始音频入库后永不修改。
- **帧坐标**：所有接口中的坐标均为**原始音频的采样帧坐标**；区间一律**左闭右开** `[start_frame, end_frame)`。
- **修订号**：创建节目时为 `0`，初始保留全段 `[0, total_frames)`。每次提交保留清单或章节清单必须带上 `expected_revision`，等于当前修订号才生效，成功后修订号 +1。过期或非法提交会返回原因，旧修订与成品保持原样。
- **保留清单**：完整替换式语义。各段须非空、升序、不重叠、在音频范围内；每个**必留口播区间**必须完整落入某一个保留段。
- **章节修改**：录入之后可通过 `PUT /programs/{id}/chapters` 提交**完整章节清单**（JSON，原始帧坐标），**无须重传音频**；校验规则与创建时完全一致（起点在音频范围内、按起点升序、起点不得重复，章节可为整数或 `{"title", "start_frame"}` 对象），**允许传空数组清空全部章节**。成功时保留段原样沿用当前修订，只推进一次修订；响应返回本次修订的保留段、成品帧数与章节投影。过期或非法提交不改动当前版本。
- **章节随修订快照化**：每次成功的删段提交、旧版合入或章节修改都会把**当时生效的完整章节清单**复制到新修订名下。因此后续删段提交与旧版建议合入都使用**提交时**的章节清单，原有的删段冲突规则保持不变；章节被后来的请求改写也不会影响任何历史修订。每笔成功响应即使晚于另一笔章节或剪辑修改送达，也只能展示**自己写入修订**的章节与保留段。查询与 ZIP 下载始终从同一修订同时计算成品音频与章节：被删区间内的章节消失，保留段起点的章节保留。
- **旧版建议合入**：客户端拿着旧修订（基版）做出的剪辑建议，可用 `POST /programs/{id}/merges` 合入。服务端在同一次受保护的当前状态上比较**建议、当前修订、基版**三者的保留状态：两侧相对基版改动的帧区间互不重叠才合成新修订；区间相交即返回 `409 merge_conflict` 与冲突区间（**双方都在删同一段也算冲突**，不擅自覆盖）。基版修订不存在、建议清单非法、合并结果不能保证必留口播完整落入单段时，返回原因且当前版本不变。
- **成功响应的修订含义**：每次 `PUT .../keeps`、`PUT .../chapters` 或 `POST .../merges` 成功，响应体都是**该笔请求自己写入的那个修订**的快照——`revision` 即本笔写库的修订号，其中的 `keep_segments`、`product_frames` 与 `chapters` 必然与该修订号对应，并不一定等于响应到达时的最新修订。并发场景下两笔非冲突修改可能连续产生修订 N 与 N+1：先完成的请求即使在提交修订 N 后、响应返回前，另一请求已写入修订 N+1，它拿到的仍是修订 N 的内容；需要最新版本时再 `GET /programs/{id}` 查询。
- **章节投影**：查询与下载时，章节被换算为成品帧坐标——被删除区间内的章节不再出现；位于保留段**起点**的章节保留（左闭）。换算始终从原始帧出发，反复编辑不会积累偏移。章节与保留段一样是**按修订不可变**的：旧修订的章节投影永远不变。
- **一致性**：每次成功提交/合入产生一个不可变修订；合入与提交共用同一把写事务（`BEGIN IMMEDIATE`），「比较 -> 校验 -> 写入」原子完成，并发合入不会丢失任何一方已生效的修改：两笔基于同一旧修订、删除互不相交区间的合入都会成功，最终当前版本同时包含两处删除（各占一个修订号，各自成功响应只描述自己写入的修订）。下载在单个读事务内取定修订快照，同一 ZIP 中的 WAV 与 JSON 必然来自同一修订，并发编辑不会混用版本。并发提交同一 `expected_revision` 时只有一个成功，其余收到 `409 stale_revision`。

## API 一览

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/health` | 健康检查 |
| POST | `/programs` | 创建节目（multipart：`audio` 文件 + `chapters`/`must_keep` JSON 字段） |
| GET | `/programs` | 列出全部节目 |
| GET | `/programs/{id}` | 查询当前版本（保留段 + 成品帧坐标章节） |
| PUT | `/programs/{id}/keeps` | 按预期修订号提交完整保留清单 |
| PUT | `/programs/{id}/chapters` | 按预期修订号提交完整章节清单（无须重传音频，可清空） |
| POST | `/programs/{id}/merges` | 旧版建议三方合入（`base_revision` + 完整保留清单，帧坐标仍指原始音频） |
| GET | `/programs/{id}/download` | 下载 ZIP（`audio.wav` + `chapters.json`） |

错误统一返回 JSON：`{"error": <代码>, "message": <中文原因>, ...}`，状态码为 400（参数非法 / 合入结果违反必留口播）/ 404（节目或基版修订不存在）/ 409（修订号过期或合入冲突）。

## 可执行的调用示例

先准备一段合规测试音频（10 秒 16 kHz 单声道 16 位）：

```bash
python3 - <<'PY'
import wave, math, struct
with wave.open("tone.wav", "wb") as w:
    w.setnchannels(1); w.setsampwidth(2); w.setframerate(16000)
    w.writeframes(b"".join(
        struct.pack("<h", int(10000*math.sin(2*math.pi*440*i/16000)))
        for i in range(16000*10)))
PY
```

### 1. 创建节目（登记章节与必留口播）

```bash
curl -s -X POST http://localhost:8000/programs \
  -F "audio=@tone.wav" \
  -F 'chapters=[{"title":"开场","start_frame":0},{"title":"口播","start_frame":16000},{"title":"正文","start_frame":80000},{"title":"结尾","start_frame":150000}]' \
  -F 'must_keep=[{"start_frame":16000,"end_frame":32000}]'
```

响应 `201`，含 `program_id` 与 `revision: 0`。后续用 `PID=<program_id>` 指代。

### 2. 查询当前版本

```bash
curl -s http://localhost:8000/programs/$PID
```

`chapters[*]` 中 `start_frame` 为原始帧坐标，`product_start_frame` 为换算后的成品帧坐标（修订 0 时两者相同）。

### 3. 提交保留清单（删段）

删去 `[32000,48000)` 与 `[96000,112000)` 两段：

```bash
curl -s -X PUT http://localhost:8000/programs/$PID/keeps \
  -H 'Content-Type: application/json' \
  -d '{"expected_revision":0,
       "keep_segments":[{"start_frame":0,"end_frame":32000},
                        {"start_frame":48000,"end_frame":96000},
                        {"start_frame":112000,"end_frame":160000}]}'
```

响应中 `revision` 变为 `1`，`product_frames` 为 `128000`，章节被重新投影（如「正文」从原始 80000 变为成品 64000）。

过期修订号示例（返回 409 与原因，状态不变）：

```bash
curl -s -X PUT http://localhost:8000/programs/$PID/keeps \
  -H 'Content-Type: application/json' \
  -d '{"expected_revision":0,"keep_segments":[{"start_frame":0,"end_frame":160000}]}'
# => {"current_revision":1,"error":"stale_revision","message":"预期修订号 0 与当前修订号 1 不一致；..."}
```

### 4. 下载成品 ZIP

```bash
curl -s -OJ http://localhost:8000/programs/$PID/download
unzip -o program-$PID-rev1.zip   # 得到 audio.wav 与 chapters.json
```

- `audio.wav`：按保留段真正裁接后的成品（16 kHz / 单声道 / 16 位，帧数等于保留段总长）。
- `chapters.json`：同一修订的章节表，`start_frame` 为成品帧坐标。

### 5. 旧版建议合入（三方合入）

接着第 3 步后的状态：当前为**修订 1**，已删 `[32000,48000)` 与 `[96000,112000)`。假设一个旧客户端仍基于**修订 0** 做了剪辑，想删 `[128000,144000)`（帧坐标仍指原始音频）。该区间与当前侧改动互不重叠，可以合入：

```bash
curl -s -X POST http://localhost:8000/programs/$PID/merges \
  -H 'Content-Type: application/json' \
  -d '{"base_revision":0,
       "keep_segments":[{"start_frame":0,"end_frame":128000},
                        {"start_frame":144000,"end_frame":160000}]}'
```

响应 `200`，`revision` 变为 `2`，两侧删除同时保留：

```json
{"revision":2,
 "keep_segments":[
   {"start_frame":0,"end_frame":32000},
   {"start_frame":48000,"end_frame":96000},
   {"start_frame":112000,"end_frame":128000},
   {"start_frame":144000,"end_frame":160000}],
 ...}
```

此后 `GET /programs/$PID` 的章节投影与 `GET /programs/$PID/download` 的 ZIP（文件名也变为 `program-$PID-rev2.zip`）都反映修订 2 的同一音频与章节。

建议删除区与当前删除区**相交**时（示例相交于 `[40000,48000)`）返回 `409 merge_conflict` 与冲突区间，当前版本保持不变：

```bash
curl -s -X POST http://localhost:8000/programs/$PID/merges \
  -H 'Content-Type: application/json' \
  -d '{"base_revision":0,
       "keep_segments":[{"start_frame":0,"end_frame":40000},
                        {"start_frame":56000,"end_frame":160000}]}'
# => {"base_revision":0,"current_revision":2,"error":"merge_conflict",
#     "conflicts":[{"start_frame":40000,"end_frame":48000}], ...}
```

注意：即使建议与当前修订**删的是完全相同的一段**，响应同样是 `409 merge_conflict`（冲突区间即该段），服务端不会擅自当成一致而覆盖。冲突后需重新 `GET` 当前版本、基于新修订重新生成建议再合入。

其它拒绝情形（均不改变当前版本）：

```bash
# 基版修订不存在 -> 404 base_revision_not_found
curl -s -X POST http://localhost:8000/programs/$PID/merges \
  -H 'Content-Type: application/json' \
  -d '{"base_revision":99,
       "keep_segments":[{"start_frame":0,"end_frame":160000}]}'

# 建议清单非法（段重叠/未升序/越界等）-> 400 invalid_segments
# 合并结果使必留口播 [16000,32000) 无法完整落入单段 -> 400 must_keep_uncovered
```

两个并发合入各自删除互不相交的区间时，服务端在同一把写事务内完成「比较 → 校验 → 写入」，两处删除都会生效（各产生一个修订号），不会有一方的修改丢失。每个成功响应只描述**自己写入的修订**（修订号与其中的保留段、成品帧数、章节投影相互对应）：先提交者即使在响应返回前另一笔已写入下一个修订号，也仍然拿到自己的修订号与内容；最终当前版本则包含两笔删除，需要时再 `GET /programs/$PID` 查询最新版本。

### 6. 修改章节清单（不重传音频）

接着前面的状态（当前为**修订 2**）。章节标题或原始帧起点可以整体修订：用 `PUT /programs/$PID/chapters` 提交**完整章节清单**即可，音频无需重传，保留段沿用当前修订，成功后修订号 +1。请求体与创建时的 `chapters` 字段同构（也接受裸整数数组）：

```bash
curl -s -X PUT http://localhost:8000/programs/$PID/chapters \
  -H 'Content-Type: application/json' \
  -d '{"expected_revision":2,
       "chapters":[{"title":"新开场","start_frame":0},
                   {"title":"口播（修订）","start_frame":16000},
                   {"title":"正文（修订）","start_frame":80000}]}'
```

响应 `200`，`revision` 变为 `3`；`keep_segments` 与 `product_frames` 与修订 2 完全相同，`chapters` 则是新清单按修订 2 的保留段重新投影的结果（被删区间 `[32000,48000)`、`[96000,112000)` 内的新章节会自动消失，位于保留段起点的章节保留）。

清空全部章节（允许，且不影响保留段）：

```bash
curl -s -X PUT http://localhost:8000/programs/$PID/chapters \
  -H 'Content-Type: application/json' \
  -d '{"expected_revision":3,"chapters":[]}'
# => {"revision":4,"chapters":[], "keep_segments":<沿用修订3>, ...}
```

用过期修订号提交（例如上一步成功后仍带 `expected_revision:3`）返回 `409 stale_revision`，章节与保留段都不变：

```bash
curl -s -X PUT http://localhost:8000/programs/$PID/chapters \
  -H 'Content-Type: application/json' \
  -d '{"expected_revision":3,"chapters":[{"start_frame":160000}]}'
# => {"current_revision":4,"error":"stale_revision", ...}（修订号未过期时该起点因右开越界会返回 400 invalid_chapters）
```

章节修改之后，后续 `PUT .../keeps` 与 `POST .../merges` 都会**沿用提交时的章节清单**（每个修订各自保存章节快照，原有删段冲突规则不变）；`GET /programs/$PID` 与 `GET /programs/$PID/download` 始终从同一修订同时取成品音频与章节，文件名中的修订号也随之推进（如 `program-$PID-rev4.zip`）。

## 环境变量

| 变量 | 默认值 | 说明 |
|---|---|---|
| `PORT` | `8000` | 本地直接运行时的监听端口 |
| `DB_PATH` | `data/app.db`（容器内 `/data/app.db`） | SQLite 数据库文件路径 |

## 项目结构

```
app.py               # 全部 API 与核心逻辑（校验、章节投影、裁接、打包、修订快照）
tests/test_app.py    # pytest 测试（规格校验、投影、提交/三方合入/章节修改、冲突、并发不丢修改、下载一致性、旧库迁移、持久化）
requirements.txt     # flask + gunicorn
Dockerfile           # python:3.12-slim + gunicorn
docker-compose.yml   # 单条命令启动，卷持久化
```
