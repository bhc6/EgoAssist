# EgoAssist 记忆系统架构文档

> **架构定位**
>
> | 层次 | 状态 | 组件 |
> |---|---|---|
> | 两条评估路径（自研，benchmark 可测） | 已实现 | `HierarchicalMemory` + `TimelineMemory` + LiveKit + Temporal |
> | 统一 realtime 路径（自研，review 完成） | 完成 review | `RealtimeEpisodeMemory`（统一 realtime 路径，含 task-preference） |
>
> **三套组件均为自研**，处于前期探索阶段，非官方成熟科研基座。两条评估路径分别对应视觉层次检索（P1）与文本 BM25 检索（P2），均可独立接受 benchmark 测评。
>
> 本文档对齐源码精确描述三套记忆组件的数据结构、分层机制、写入/检索流程，供论文撰写参考。
> 源码路径：`egoassist/memory/store.py`、`egoassist/memory/timeline.py`、
> `egoassist/realtime/episodes.py`、`egoassist/realtime/engine.py`、`egoassist/retrieval/`

---

## 目录

1. [系统概览](#1-系统概览)
2. [Pipeline 1 — HierarchicalMemory](#2-pipeline-1--hierarchicalmemory)
   - 2.1 数据单元 MemoryItem
   - 2.2 三级分层结构
   - 2.3 向量索引层
   - 2.4 写入流程（ingest → consolidate）
   - 2.5 检索流程（route_and_retrieve → localize）
   - 2.6 超参配置（Config）
3. [Pipeline 2 — TimelineMemory](#3-pipeline-2--timelinememory)
   - 3.1 存储介质与 Schema
   - 3.2 memory_cards 字段全表
   - 3.3 Kind 分类体系
   - 3.4 Status 状态机
   - 3.5 FTS5 全文索引
   - 3.6 写入流程（remember → consolidate）
   - 3.7 检索打分公式（ScoringWeights）
4. [统一接口与适配器](#4-统一接口与适配器)
5. [RealtimeEpisodeMemory — 统一 realtime 路径](#5-realtimeepisodememory--统一-realtime-路径)
   - 5.1 统一动机
   - 5.2 懒加载委托（`_get_pref()`）
   - 5.3 七个委托方法
   - 5.4 realtime 路径数据流
6. [三组件对比总表](#6-三组件对比总表)
7. [端到端架构图](#7-端到端架构图)

---

## 1 系统概览

EgoAssist 记忆系统由两个层次、三个组件构成：

### 两条评估路径（自研，benchmark 可测）

| 组件 | 入口类 | 存储介质 | 主要场景 |
|---|---|---|---|
| `HierarchicalMemory` | `StreamingMemoryAssistant` | 进程内存（无持久化） | 视觉帧分层、benchmark 精确定位（Pipeline 1） |
| `TimelineMemory` | `TimelineAssistant` | SQLite WAL（持久化） | 跨会话记忆、benchmark 问答评测（Pipeline 2） |

两者均通过 `MemoryBackend` 协议对外暴露，共享相同的 `VLMBackend`、`ConformalAbstention` 和 `QAResult` 结构。

### 统一 realtime 路径（自研，review 完成）

| 组件 | 入口类 | 存储介质 | 主要场景 |
|---|---|---|---|
| `RealtimeEpisodeMemory` | `run_egoassist.py` | 委托 `TimelineMemory`（同一 SQLite） | realtime 路径统一入口，情节检索 + task-preference |

`RealtimeEpisodeMemory` 通过内部懒加载委托 `PreferenceMemory`，将情节记忆（BM25 检索）与任务级偏好（propose→confirm 循环）合并为单一对象，消除了 `run_egoassist.py` 中原有的 `active_memory` / `active_episodic` 双路径。详见 [第 5 节](#5-realtimeepisodememory--统一-realtime-路径)。

---

## 2 Pipeline 1 — HierarchicalMemory

### 2.1 数据单元 MemoryItem

源码：`egoassist/memory/store.py`，`@dataclass class MemoryItem`

```python
@dataclass
class MemoryItem:
    id            : int                   # 进程内自增整数（_next_id）
    t_start       : float                 # 视频相对起始时间（秒）
    t_end         : float                 # 视频相对结束时间（秒）
    embedding     : np.ndarray            # 稠密向量，由 backend.embed_frames() 产出
    caption       : str                   # VLM 生成文字描述；含音频则追加 "| heard: ..."
    keyframes     : List[np.ndarray]      # ≤8 帧 RGB 图像（_subsample_timed 均匀采样）
    keyframe_times: List[float]           # 每帧对应视频内绝对时间（秒）
    keyframe_meta : List[dict]            # 每帧元数据
    audio_events  : List[dict]            # [{source_time_sec: float, text: str}]
    level         : str = "short"         # "short" | "long"
    child_ids     : List[int] = []        # long-term 节点聚合的 short-term 子节点 id
    wall_start    : Optional[datetime] = None   # 真实挂钟时间（带时区 UTC），可为 None
    wall_end      : Optional[datetime] = None
```

**t_center 属性**：`0.5 * (t_start + t_end)`，供外部定位使用。

### 2.2 三级分层结构

源码：`HierarchicalMemory.__init__` 及 `_update_current`

```
HierarchicalMemory（进程内存）
│
├─ current : List[MemoryItem]
│    定义：t_end >= (最新 item.t_end − current_window)
│    current_window = 8.0 秒（Config.current_window）
│    维护：每次 ingest() 调用 _update_current()，追加新 item 后裁掉超出窗口的旧 item
│    作用：表征"此刻正在发生"的极短期记忆
│
├─ short_term : List[MemoryItem]       level = "short"
│    容量上限：short_term_capacity = 40 条（Config）
│    维护：ingest() 追加；超出容量或每 consolidate_every=24 条触发整理
│    作用：工作记忆，保留近期完整帧+向量，支持精细定位
│
└─ long_term : List[MemoryItem]        level = "long"
     无硬性上限，但每个节点是多个 short item 的合并体
     作用：长期情节记忆，向量已均值压缩，帧已重新下采样
```

**current 与 short_term 的关系**：同一个 item 对象同时存在于两个列表（Python 引用），
current 是 short_term 的时间滑窗子集。

### 2.3 向量索引层

源码：`egoassist/retrieval/index.py`

```
VectorIndex（Protocol）
├─ FlatIndex   — 纯 numpy，O(n) brute-force 余弦；无额外依赖
│    内部：List[int] ids + List[ndarray] vecs
│    search：mat @ q（矩阵乘法），argpartition top-k
│
└─ FAISSIndex  — 需要 faiss-cpu（egoassist[gpu] extra）
     < 1000 向量：IndexFlatIP
     ≥ 1000 向量：自动升级为 IVFFlat（nlist = √n，nprobe = √nlist）
     remove 操作：重建整个索引（_id_to_vec dict 保留副本）
     remove_batch：一次重建替代 O(n·k) 逐条删除
```

`build_index(dim)` 工厂函数：优先返回 FAISSIndex，import 失败则降级 FlatIndex。

索引与 `_items_by_id: dict[int, MemoryItem]` 保持严格同步：add/remove_batch 成对操作。

### 2.4 写入流程

源码：`HierarchicalMemory.ingest()` + `consolidate()` + `_merge()`

```
ingest(seg: Segment)
  │
  ├─① [锁外] backend.embed_frames(seg.frames)    → emb [D]      # GPU 调用
  ├─② [锁外] backend.caption(seg.frames)          → cap          # GPU 调用
  ├─③ 若 seg.audio 非空：cap += " | heard: " + audio_text
  ├─④ _subsample_timed(frames, frame_times, frame_meta, k=8)
  │
  └─⑤ [加锁]
       ├─ MemoryItem(id=_new_id(), ..., level="short",
       │             wall_start=seg.wall_start, wall_end=seg.wall_end)
       ├─ _items_by_id[item.id] = item
       ├─ _index.add(item.id, emb)
       ├─ short_term.append(item)
       ├─ _update_current(item)         ← 滑窗裁剪
       ├─ _since_consolidate += 1
       └─ 若需整理 → _schedule_consolidation()   [daemon thread，不阻塞]

_schedule_consolidation()
  仅当无存活 consolidation thread 时启动新线程（跳过重叠周期）

consolidate()  [后台线程，持锁执行]
  ├─ n_move = len(short_term) − short_term_capacity // 2   = len − 20
  ├─ movers = short_term[:n_move]        ← 最旧的一批
  ├─ short_term = short_term[n_move:]
  └─ for group in _greedy_group(movers, sim_th=0.6):
         merged = _merge(group)
         long_term.append(merged)
         _items_by_id[merged.id] = merged

_greedy_group(items, sim_th=0.6)
  相邻 item 余弦相似度 > 0.6 则合并入同组，否则开新组

_merge(group: List[MemoryItem]) → MemoryItem
  ├─ emb   = mean([m.embedding]) → L2-normalize
  ├─ caption = "; ".join(dict.fromkeys(captions)[:4])   # 去重保序，最多4条
  ├─ 合并所有 keyframes 按时间排序 → _subsample_timed(..., k=8)
  ├─ wall_start = min(非 None 的 wall_start)
  ├─ wall_end   = max(非 None 的 wall_end)
  ├─ level = "long"，child_ids = [m.id for m in group]
  ├─ _index.remove_batch(child_ids)    ← 子向量从索引删除
  ├─ _items_by_id.pop(child_id)        ← 子节点从注册表删除（内存释放）
  └─ _index.add(merged.id, merged.embedding)
```

### 2.5 检索流程

源码：`egoassist/retrieval/router.py` + `egoassist/retrieval/localize.py`

```
route_and_retrieve(query, mem, backend, cfg)
  │
  ├─① backend.embed_text(query)                    → q_vec [D]
  ├─② mem.retrieve_by_vector(q_vec, k=topk×3=15)  → candidates_raw
  │     （_index.search + _items_by_id 查找，索引空时自动重建）
  │
  ├─③ 时间路由（正则过滤候选集）
  │     recent_hints 匹配（just now / recently / 刚才…）
  │       → pool = candidates ∩ (current ∪ short_term)
  │     long_hints 匹配（yesterday / last week / 习惯…）
  │       → pool = candidates ∩ (long_term ∪ short_term)
  │     无命中 → pool = candidates_raw（不过滤）
  │     pool 为空 → 回退到 candidates_raw
  │     pool 去重（按 item.id）
  │
  ├─④ 打分（纯余弦，无偏置项）
  │     sim   = dot(q_vec, item.embedding)             # 余弦（已 L2 归一化）
  │     score = sim
  │
  ├─⑤ 按 score 降序排序，取 top-k=5
  │
  └─ 返回 List[Candidate(item, score)]

localize(query, item, backend, cfg)
  │
  ├─① backend.embed_text(query)                    → q_vec [D]
  ├─② 若 keyframes 为空或 q_vec 全零 → 直接返回 [t_start, t_end]，conf=0
  │
  ├─③ backend.embed_frames_each(item.keyframes)    → kf_embs [K, D]  # 一次 batch
  │
  ├─④ 滑窗打分（纯 numpy，零 GPU 调用）
  │     t ∈ [t_start, t_end)，步长 stride=0.5s
  │     每窗口 [t, t+win=2.0s]：
  │       mask  = (keyframe_times ∈ [t, t+win])
  │       若 mask 有命中：
  │         win_emb = mean(kf_embs[mask]) → L2-normalize
  │         score   = dot(q_vec, win_emb)
  │
  ├─⑤ best  = argmax(window_scores)
  │   conf  = softmax_margin(scores, temp=5.0)
  │             = max(softmax(5×scores))   ∈ (0, 1]
  │
  └─ 返回 Localization(t0, t1, confidence, frames, frame_times, caption_hint)
```

### 2.6 超参配置（Config）

源码：`egoassist/config.py`

| 参数 | 默认值 | 说明 |
|---|---|---|
| `current_window` | `8.0` s | current 层滑窗宽度 |
| `short_term_capacity` | `40` | short_term 条数上限，触发 consolidate |
| `consolidate_every` | `24` | 每摄入多少条触发一次整理（与容量条件取 OR）|
| `retrieve_topk` | `5` | 最终返回候选数；检索时取 `topk×3=15` 做路由前预选 |
| `localize_stride` | `0.5` s | 滑窗步长 |
| `localize_win` | `2.0` s | 证据窗口宽度 |
| `risk_alpha` | `0.1` | 拒答风险上界 α（conformal abstention）|
| `event_change_threshold` | `0.35` | 分段变化阈值 |
| `event_min_len` | `1.0` s | 最短 segment 时长 |
| `event_max_len` | `30.0` s | 最长 segment 时长（强制切分）|

---

## 3 Pipeline 2 — TimelineMemory

### 3.1 存储介质与 Schema

源码：`egoassist/memory/timeline.py`，`TimelineMemory._initialize()`

- 存储：SQLite 单文件，`PRAGMA journal_mode=WAL`，`PRAGMA foreign_keys=ON`
- Schema 版本：`TIMELINE_SCHEMA_VERSION = 2`（存储于 `timeline_meta` 表）
- 线程安全：`threading.RLock` + 每次操作独立 connection（`_ClosingConnection`）

**表结构**：

| 表名 | 类型 | 用途 |
|---|---|---|
| `memory_cards` | 主表 | 所有记忆卡片 |
| `memory_fts` | FTS5 虚拟表 | 全文索引（trigram tokenizer，content table） |
| `memory_drafts` | 辅助表 | 待确认的多模态输入草稿 |
| `memory_imports` | 辅助表 | 去重指纹（防止重复导入） |
| `timeline_meta` | 元信息表 | schema_version 等键值对 |

### 3.2 memory_cards 字段全表

源码：`TimelineMemory._initialize()` SQL + `_row_to_card()`

```sql
CREATE TABLE memory_cards (
  -- 主键与归属
  id                  TEXT PRIMARY KEY,          -- 16-char hex UUID（uuid4().hex[:16]）
  user_id             TEXT NOT NULL,
  case_id             TEXT,                       -- scope="case" 时必填
  scope               TEXT NOT NULL              -- CHECK: "case" | "participant"
                        CHECK(scope IN ('case','participant')),
  -- 层级与分类
  tier                TEXT NOT NULL              -- CHECK: "short" | "long"
                        CHECK(tier IN ('short','long')),
  kind                TEXT NOT NULL,             -- 见 3.3 Kind 分类
  status              TEXT NOT NULL,             -- 见 3.4 Status 状态机
  -- 内容
  text                TEXT NOT NULL,             -- 记忆正文（≤1000 chars，超出截断）
  normalized_text     TEXT NOT NULL,             -- NFC casefold 归一化（FTS5 索引列）
  memory_key          TEXT,                      -- 实体谓词键，如 "location:alice"
  -- 结构化三元组（可选）
  subject             TEXT,                      -- 归一化实体名
  predicate           TEXT,                      -- 归一化关系名
  value               TEXT,
  -- 时间轴
  happened_at         TEXT NOT NULL,             -- ISO 8601 UTC，事件发生时间
  day_key             TEXT NOT NULL,             -- YYYY-MM-DD（本地日历）
  recorded_at         TEXT,                      -- 写入系统时间
  effective_at        TEXT,                      -- 语义生效时间
  effective_end       TEXT,                      -- 语义失效时间（区间事实）
  recurrence          TEXT,                      -- JSON 重复规则，null 表示单次
  -- 本地时钟
  timezone_name       TEXT,                      -- 如 "Asia/Shanghai"，默认 "UTC"
  utc_offset_minutes  INTEGER,                   -- UTC+8 → 480，默认 0
  -- 向量
  embedding           BLOB NOT NULL,             -- float32 bytes（HashingEncoder 或注入）
  embedding_dim       INTEGER NOT NULL,
  -- 溯源
  source_ids          TEXT NOT NULL,             -- JSON 数组，来源 frame/card ID
  supersedes          TEXT,                      -- 被此 card 覆盖的上一版 id（自引用）
  importance          REAL NOT NULL,             -- [0, 1]，clamp 处理
  metadata            TEXT NOT NULL,             -- JSON，任意扩展；含 videoStartSeconds 等
  -- 审计
  created_at          TEXT NOT NULL,
  updated_at          TEXT NOT NULL
);
-- 索引
CREATE INDEX memory_cards_owner ON memory_cards(user_id, scope, case_id, status, happened_at DESC);
CREATE INDEX memory_cards_key   ON memory_cards(user_id, memory_key, status, happened_at DESC);
CREATE INDEX memory_cards_tier  ON memory_cards(user_id, tier, status, happened_at DESC);
```

### 3.3 Kind 分类体系

源码：`MEMORY_KINDS`、`RETRIEVABLE_KINDS`、`DEFAULT_RETRIEVABLE_KINDS`

```
MEMORY_KINDS = {
  "fact"            # 持久性事实，不随时间变化（支持 memory_key 去重）
  "state"           # 当前状态，可被同 key 新 card 覆盖（supersede=True）
  "event"           # 有时间戳的单次发生事件（不触发 supersede）
  "observation"     # 感知性观察，不推断语义（不触发 supersede）
  "scene"           # 视频片段视觉描述，TimelineAssistant 专用（不触发 supersede）
  "preference"      # 用户个人偏好（支持 memory_key 去重）
  "task-preference" # 任务/步骤级偏好，tier="long"（支持 memory_key 去重）
  "tool-effect"     # 工具调用副作用（不触发 supersede）
  "capture"         # 待确认输入（pending 状态，不参与检索）
  "legacy"          # 从旧 JSON 格式导入的历史记忆
}

RETRIEVABLE_KINDS       = MEMORY_KINDS − {"capture"}
DEFAULT_RETRIEVABLE_KINDS = RETRIEVABLE_KINDS − {"legacy", "scene"}
# TimelineAssistant 检索时传入 _BENCHMARK_KINDS = {"scene", "event"}
```

**supersede 触发条件**（`remember()` 中）：
`supersede=True AND memory_key 非空 AND status="active" AND kind NOT IN {"event","scene","observation"}`

### 3.4 Status 状态机

源码：`MEMORY_STATUSES`、`remember()`、`activate_pending()`、`consolidate()`

```
                  remember(status="pending")
                         │
               begin_draft() / begin_capture()
                         │
                      pending ◄──────────────────────┐
                         │                            │
              activate_pending()               update_draft()
                         │
                       active ──────────────────────────────────────┐
                    /    |    \                                      │
                   /     │     \                                     │
    remember(同 key)  consolidate()  delete()  consume_capture()   ...│
          │               │            │              │               │
      superseded     consolidated   deleted       consumed         expired
    （旧 card）      （tier 升级后）                           （draft 超时）
```

- `superseded`、`consolidated`、`deleted`、`consumed`、`expired` 均为终态，不参与检索
- `active` 且 `status="active"` 才出现在 `retrieve()` 的候选集中

### 3.5 FTS5 全文索引

源码：`TimelineMemory._initialize()` Step 3

```sql
CREATE VIRTUAL TABLE memory_fts USING fts5(
    text, normalized_text,
    content=memory_cards,       -- content table（行 rowid 对应 memory_cards.rowid）
    tokenize='trigram'          -- trigram tokenizer（支持中文、英文、任意语言）
);
-- 四个同步触发器（INSERT/UPDATE_before/UPDATE_after/DELETE_before）
-- 保证 memory_cards 与 memory_fts 实时同步
```

检索时批量 BM25 打分（`bm25()` 返回负值，更负 = 更相关）：

```python
fts_rows = conn.execute("""
    SELECT mc.id, bm25(mf) as score
    FROM memory_fts mf
    JOIN memory_cards mc ON mc.rowid = mf.rowid
    WHERE mf MATCH ?           -- 查询词 OR 连接：'"term1" OR "term2" OR ...'
      AND mc.id IN (...)       -- 仅对已通过 scope/kind/status 过滤的候选打分
""")
fts_score = min(1.0, max(0.0, -bm25_raw / fts_normalization_divisor))
# fts_normalization_divisor = 10.0（ScoringWeights 默认值）
```

### 3.6 写入流程

源码：`TimelineMemory.remember()` + `consolidate()`

```
remember(text, *, user_id, case_id, scope, kind, tier, ...)
  │
  ├─① text 清理：strip + 合并空白，截断至1000 chars；空文本直接返回 None
  ├─② 参数校验：scope/tier/kind/status 枚举检查
  ├─③ happened_at = effective_at ?? happened_at ?? now（ISO UTC）
  ├─④ memory_key 生成（若 subject+predicate 均存在）：
  │     phone/telephone  → "contact:{entity}:phone"
  │     location         → "location:{entity}"
  │     其他             → "{predicate}:{entity}"
  ├─⑤ normalized = NFC casefold + 合并空白
  ├─⑥ vector = encoder(text) → L2-normalize → float32
  │
  └─⑦ [加锁 + SQLite 事务]
       ├─ 若 card_id 已存在 → 直接返回已有 card（幂等）
       ├─ 若非 event/scene/observation/tool-effect 且 normalized_text 重复 → 返回已有 card
       ├─ 若 supersede 条件满足 → UPDATE 旧 card status="superseded"
       ├─ INSERT INTO memory_cards (...)
       └─ SELECT 回读 → _row_to_card() → 触发 event_sink

consolidate(*, user_id, case_id, scope)
  ├─ 查询所有 tier="short"、status="active" 的 card（排除 kind="capture"）
  ├─ 若数量 ≤ short_term_capacity=40 → 直接返回，不操作
  ├─ oldest = 超出容量的最旧一批
  └─ for source in oldest:
       promotion_id = "promoted-" + sha256(f"{user_id}|{scope}|{case_id}|{source['id']}")[:16]
       ├─ remember(source.text, tier="long", card_id=promotion_id, supersede=False, ...)
       │   （text/caption 原样保留，不做合并，与 P1 的均值合并形成鲜明对比）
       └─ update_status(source.id, "consolidated")
```

### 3.7 检索打分公式（ScoringWeights）

源码：`TimelineMemory.retrieve()` + `ScoringWeights`（默认值）

**第一步：候选筛选**

```python
# 1. SQL 过滤：user_id + scope + case_id + kind ∈ requested_kinds + status="active"
rows = conn.execute("SELECT * FROM memory_cards WHERE ...")

# 2. 快速路径（fast-path）：若 query_spec 含 subject+predicate
#    直接按 memory_key 精确查找。命中则 score=1.0 —— 但仍受查询时间窗
#    约束（time_eligible）：落在所问时间窗之外的规范事实不是答案。
fast_key = memory_key_for(query_subject, query_predicate)
```

**第二步：逐 card 打分**

P2 是**干净的纯词法 baseline**（与 P1 密集向量形成对照）。分数**就是**词法相似度本身——无 semantic 项、无结构化奖励、无 recency 衰减、无混合系数。这些都是会污染与 P1 对照的归纳偏置，已直接删去：

```
score(card, query) = max(jaccard, fts_bm25_norm)   # 已在 [0, 1]
    # jaccard      = |query_terms ∩ card_terms| / |query_terms ∪ card_terms|
    # fts_bm25_norm = min(1, -bm25() / 10.0)
```

subject / predicate / time 不进入打分，而是作为下一步的**硬性 eligibility 过滤**（查询约束，不是分数先验）。

**第三步：Eligibility 门控**（score 为 0 若不满足）

```python
eligible = (time_eligible
            and subject_eligible
            and predicate_eligible
            and (overlap > 0 or structured_match))   # 必须有词重叠或结构化匹配
score = score if eligible else 0.0
```

**第四步：排序与阈值**

```python
# 按 (score, happenedAt) 双键降序排序
# min_score = 0.05，取前 max(1, limit) 个作为 selected
#
# 为纯词法 baseline 校准：分数即原始词法相似度（无偏置抬升）。
# 真实的多词实体匹配落在 ~0.09–0.13（如共享人名贡献 bigram + trigram
# 重叠）；真负例被 eligibility 门控归零（kind 过滤 / 时间不符 / 零重叠），
# 根本到不了阈值。0.05 落在两者之间：接纳最弱的真正例，同时不与任何
# 相近的负例分数为邻——因此不是对测试的过拟合。旧的 0.18 是混合公式
# 时代的残留，高于每一个真正例，正是 P2 欠召回的根因。
```

---

## 4 统一接口与适配器

源码：`egoassist/memory/interface.py` + `egoassist/memory/adapters.py`

### MemoryBackend 协议

```python
@runtime_checkable
class MemoryBackend(Protocol):
    def ingest_text(self, text: str, *, user_id: str, kind: str,
                    metadata: dict) -> str: ...          # 返回 opaque id
    def retrieve_text(self, query: str, *, user_id: str,
                      limit: int = 8) -> list[dict]: ... # [{id, text, score, ...}]
```

### 两个适配器

| 适配器 | 包装对象 | ingest_text | retrieve_text |
|---|---|---|---|
| `TimelineMemoryAdapter` | `TimelineMemory` | 调 `remember(tier="short")` | 调 `retrieve()` 取 selected |
| `HierarchicalMemoryAdapter` | `HierarchicalMemory` | 构造合成 Segment（无帧）调 `ingest()` | 调 `route_and_retrieve()` |

### import_visual_items（跨系统迁移）

将 `HierarchicalMemory` 的 `MemoryItem` 批量写入 `TimelineMemory`：

```
MemoryItem(level="short") → kind="scene",  tier="short"
MemoryItem(level="long")  → kind="event",  tier="long"
card_id = "visual-" + sha256(identity_json)[:16]   # 内容寻址，幂等
metadata 含：videoStartSeconds, videoEndSeconds, keyframeTimes, childIds
```

## 5 RealtimeEpisodeMemory — 统一 realtime 路径

源码：`egoassist/realtime/episodes.py`、`egoassist/realtime/engine.py`

### 5.1 统一动机

此前 `run_egoassist.py` 同时维护两个对象：

```
active_episodic : RealtimeEpisodeMemory  # BM25 情节检索
active_memory   : PreferenceMemory       # task-preference propose/confirm
```

两者指向同一底层 `TimelineMemory` 数据库，但 API 各自独立，导致所有调用方必须携带双参数。
统一后，`RealtimeEpisodeMemory` 成为 realtime 路径的唯一记忆对象，通过内部懒加载委托 `PreferenceMemory`。

### 5.2 懒加载委托（`_get_pref()`）

源码：`RealtimeEpisodeMemory.__init__` + `_get_pref()`

```python
# __init__ 追加两行
self._pref_lock: threading.RLock = threading.RLock()
self._pref: "PreferenceMemory | None" = None

def _get_pref(self) -> "PreferenceMemory":
    with self._pref_lock:
        if self._pref is None:
            from egoassist.realtime.engine import PreferenceMemory  # 局部导入，避免循环
            self._pref = PreferenceMemory(
                self._timeline.path,
                event_sink=self._event_sink,
                timeline=self._timeline,          # 共享同一 DB 连接
                user_id=self.owner_user_id or "local-default",
                case_id=self.owner_case_id,
                scope=self.scope or "case",
            )
        return self._pref
```

**关键点**：
- `PreferenceMemory` 共享 `self._timeline`（同一 `TimelineMemory` 实例），零 I/O 开销
- `threading.RLock` 保证多线程安全，允许同一线程重入
- 局部导入规避 `episodes.py` ↔ `engine.py` 循环依赖

### 5.3 七个委托方法

| 方法 | 委托目标 | 作用 |
|---|---|---|
| `propose_preference(*, task, step, prompt, evidence, ...)` | `_get_pref().propose_preference(...)` | 提议新 task-preference，写 `pending` 卡片 |
| `resolve_candidate(accepted, *, candidate_scope=None)` | `_get_pref().resolve_candidate(...)` | 接受/拒绝 pending 偏好，激活或丢弃 |
| `retrieve_preference(task, step, *, emit=True)` | `_get_pref().retrieve(task, step)` | 检索当前 task/step 可用偏好 |
| `snapshot_preference(*, candidate_scope=None)` | `_get_pref().snapshot(...)` | 导出当前偏好快照（前端展示用） |
| `delete_preference(record_id, *, candidate_scope=None)` | `_get_pref().delete_memory(...)` | 按 ID 删除偏好卡片 |
| `next_session_preference()` | `_get_pref().next_session_preference()` | 跨 session 偏好提升 |
| `import_preference_legacy(legacy_path)` | `_get_pref().import_legacy(legacy_path)` | 导入旧 JSON 格式偏好 |

### 5.4 realtime 路径数据流

```
run_egoassist.py
  │
  ├─ episodic_for_user(uid) / scoped_episodic_memory(uid, sid, policy)
  │        │
  │        ▼
  │   RealtimeEpisodeMemory（单一对象）
  │     ├─ remember(text, kind=...)         → TimelineMemory.remember()
  │     ├─ retrieve(query)                  → TimelineMemory.retrieve() [BM25]
  │     ├─ mark_injected(ids)               → TimelineMemory.update_status()
  │     ├─ snapshot()                        → TimelineMemory.retrieve()
  │     ├─ consolidate()                     → TimelineMemory.promote_to_long()
  │     │
  │     └─ [task-preference 委托, 懒加载]
  │          ├─ propose_preference(...)      → _get_pref().propose_preference()
  │          ├─ resolve_candidate(...)       → _get_pref().resolve_candidate()
  │          ├─ retrieve_preference(...)     → _get_pref().retrieve()
  │          ├─ snapshot_preference(...)     → _get_pref().snapshot()
  │          └─ delete_preference(...)       → _get_pref().delete_memory()
  │                    │
  │                    ▼
  │             PreferenceMemory（内部私有，共享同一 TimelineMemory）
  │                    │
  │                    ▼
  │             TimelineMemory（timeline.sqlite3）
  │
  └─ benchmark / offline path
       TimelineAssistant → TimelineMemory（直接访问，不经 RealtimeEpisodeMemory）
```

---

## 6 三组件对比总表

| 维度 | Pipeline 1 — HierarchicalMemory | Pipeline 2 — TimelineMemory | RealtimeEpisodeMemory（realtime 路径） |
|---|---|---|---|
| **持久化** | 进程内存，重启丢失 | SQLite WAL，跨会话持久 | 委托 TimelineMemory，跨会话持久 |
| **层级数量** | 3（current / short / long） | 2（short / long） | 同 TimelineMemory |
| **current 层** | 滑窗 8 s，维护独立列表 | 无独立存储；retrieve() 实算 age | 同 TimelineMemory |
| **tier 容量** | short_term_capacity = 40 | short_term_capacity = 40 | 同 TimelineMemory |
| **层级升级** | 贪心合并：embedding 均值 + L2-norm | 原子提升 tier="long"，text 不变 | 同 TimelineMemory |
| **记忆单元** | Segment（含原始帧） | Card（text-only，帧 ingest 后丢弃）| Card（同 TimelineMemory） |
| **帧保留** | ≤8 关键帧持久保留（localize 用） | 不保留任何帧 | 不保留任何帧 |
| **向量嵌入** | 神经模型（CLIP-like，backend 调用） | HashingEncoder（随机投影，无语义）| HashingEncoder（同 TimelineMemory）|
| **检索核心** | 稠密余弦（纯余弦，无偏置） | BM25（FTS5 trigram）+ Jaccard | BM25（同 TimelineMemory） |
| **task-preference** | 不支持 | 通过 kind="task-preference" 存储 | ✅ 完整 propose→confirm API（7 方法） |
| **kind 分类** | 无 | 10 种 kind | 全部 10 种（写入时透传） |
| **status 管理** | 无 | 6 种 status | 同 TimelineMemory |
| **结构化记忆** | 不支持 | subject / predicate / value 三元组 | 同 TimelineMemory |
| **多用户隔离** | 无 | user_id + case_id + scope | user_id + case_id + scope |
| **GPU 依赖** | embed_frames/embed_text 必须 | 默认无 GPU | 默认无 GPU |
| **主要调用者** | StreamingMemoryAssistant / benchmark | TimelineAssistant / benchmark | run_egoassist.py（realtime 唯一路径） |
| **北京时间来源** | `MemoryItem.wall_start`（datetime） | `happened_at`（ISO UTC 字符串） | 同 TimelineMemory |

---

## 7 端到端架构图

### Pipeline 1 完整数据流

```
┌─ 摄入 ──────────────────────────────────────────────────────────────┐
│  VideoStream → segment_stream() → Segment                           │
│   [变化阈值 0.35 | min 1 s | max 30 s]                             │
│                    │                                                 │
│                    ▼                                                 │
│         HierarchicalMemory.ingest(seg)                              │
│          ├─ embed_frames(frames) ──────────────── emb [D]  [GPU]   │
│          ├─ caption(frames) ───────────────────── cap      [GPU]   │
│          ├─ subsample keyframes ≤8                                  │
│          ├─ MemoryItem(level="short")                               │
│          ├─ short_term.append + _index.add(id, emb)                │
│          ├─ current（滑窗 8 s 裁剪）                               │
│          └─ [达阈值] consolidate() [daemon thread]                  │
│               └─ _greedy_group(sim_th=0.6)                          │
│                    └─ _merge(group)                                  │
│                         ├─ emb = mean(child_embs) → L2-norm        │
│                         ├─ caption 合并 ≤4 条                      │
│                         ├─ keyframes 重采样 ≤8 帧                  │
│                         ├─ wall_start=min / wall_end=max            │
│                         ├─ _index.remove_batch(child_ids)          │
│                         └─ long_term.append(merged)                 │
└─────────────────────────────────────────────────────────────────────┘

┌─ 查询 ──────────────────────────────────────────────────────────────┐
│  query → route_and_retrieve()                                        │
│           ├─ embed_text(query) ──── q_vec [D]                       │
│           ├─ _index.search(q_vec, k=15)                             │
│           ├─ 时间路由正则 → recent/long 层过滤                      │
│           └─ score = cosine → top-5                                 │
│                    │                                                 │
│                    ▼                                                 │
│         localize(query, top_item)                                    │
│           ├─ embed_frames_each(keyframes) [1次 batch GPU]           │
│           ├─ 滑窗 [win=2s, stride=0.5s] → argmax cosine            │
│           └─ confidence = softmax_margin(temp=5) → [t0, t1]        │
│                    │                                                 │
│                    ▼                                                 │
│  backend.answer(query, captions, evidence_frames,                    │
│                 event_time="北京时间 YYYY-MM-DD HH:MM:SS")          │
└─────────────────────────────────────────────────────────────────────┘
```

### Pipeline 2 完整数据流

```
┌─ 摄入 ──────────────────────────────────────────────────────────────┐
│  VideoStream → segment_stream() → Segment                           │
│                    │                                                 │
│                    ▼                                                 │
│         TimelineAssistant.ingest(seg)                               │
│          ├─ caption(frames) ───── cap  [GPU/API]                    │
│          ├─ 帧完全丢弃（不保留 keyframes）                          │
│          └─ timeline.remember(cap, kind="scene", tier="short",      │
│                importance=0.7,                                       │
│                metadata={videoStartSeconds, videoEndSeconds, ...})  │
│               ├─ HashingEncoder(cap) → emb（随机投影，已归零）      │
│               ├─ FTS5 触发器自动同步                                │
│               └─ SQLite INSERT                                       │
│                                                                      │
│  [consolidate()] tier="short" 超 40 条                              │
│    → tier="long"，text 原样保留                                     │
│    → card_id = "promoted-" + sha256(...)[:16]（确定性）             │
└─────────────────────────────────────────────────────────────────────┘

┌─ 查询 ──────────────────────────────────────────────────────────────┐
│  query → timeline.retrieve()                                         │
│           ├─ SQL 过滤：user_id+scope+kind∈{scene,event}+status="active"│
│           ├─ fast-path：memory_key 精确匹配 ∧ time_eligible→score=1.0│
│           ├─ 时间路由：today 关键词 → time_eligible 门控            │
│           ├─ BM25 批量（FTS5 trigram）                              │
│           ├─ Jaccard 词重叠                                          │
│           ├─ 纯词法打分（§3.7）→ eligibility 门控 → min_score=0.05 │
│           └─ 排序取 top-k                                           │
│                    │                                                 │
│  top_card["happenedAt"] → format_beijing_iso(iso_str)               │
│                    ▼                                                 │
│  backend.answer(query, captions, [],                                 │
│                 event_time="北京时间 YYYY-MM-DD HH:MM:SS")          │
└─────────────────────────────────────────────────────────────────────┘
```

---

*文档生成时间：2026-08-01 | 对齐代码版本：当前 HEAD | realtime 记忆路径统一完成（review ✅）*