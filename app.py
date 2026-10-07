"""录音删段交付 HTTP API。

功能概述：
- 创建节目：上传不超过 30 秒的 16 kHz 单声道 16 位 PCM WAV，
  同时登记原始采样帧坐标下的章节起点与必留口播区间；原始音频保持不变。
- 初始版本（修订号 0）保留全段；此后按预期修订号提交完整保留清单，
  各段左闭右开、非空、升序、不重叠且在音频范围内，必留口播须完整落入某个保留段。
- 查询当前版本：返回保留段与换算为成品帧坐标的章节
  （被删除区间内的章节消失，位于保留段起点的章节保留；始终按原始帧计算，不积累偏移）。
- 修改章节清单：录入后无须重传音频，按预期修订号用 JSON 提交完整章节清单（可清空），
  沿用创建时的章节校验；成功后保留段原样沿用、修订号 +1，响应描述本次写入的修订。
  此后删段提交与旧版建议合入都沿用提交时的章节清单（章节随修订做不可变快照）。
- 旧版建议合入：提交建议依据的基版修订号与完整保留清单，三方（建议 / 当前 / 基版）
  比较保留状态；两侧改动的帧区间互不重叠才合成新修订，区间相交即返回冲突区间
  （双方都删同一段也算冲突，不擅自覆盖），基版不存在、清单非法或合入结果违反
  必留口播完整落入单段时拒绝，当前版本保持不变。成功响应描述本请求自己写入的修订
  （修订号、保留段、成品帧数、章节投影彼此对应）；并发下另一请求随后写入更新的修订
  也不会让先完成者拿到别人的内容。
- 下载 ZIP：包含真正裁接好的 WAV 与 JSON 章节表，
  两者取自同一修订的不可变快照，并发编辑不会混用版本。
- 所有状态存于 SQLite，重启后仍可继续编辑节目。
"""

import io
import json
import os
import sqlite3
import uuid
import wave
import zipfile
from datetime import datetime, timezone

from flask import Flask, g, jsonify, request, send_file

# ---- 音频规格常量 -----------------------------------------------------------
SAMPLE_RATE = 16000          # Hz
CHANNELS = 1                 # 单声道
SAMPLE_WIDTH = 2             # 16 位 PCM
MAX_SECONDS = 30
MAX_FRAMES = SAMPLE_RATE * MAX_SECONDS

DB_PATH = os.environ.get("DB_PATH", os.path.join("data", "app.db"))

SCHEMA = """
CREATE TABLE IF NOT EXISTS programs (
    id               TEXT PRIMARY KEY,
    created_at       TEXT NOT NULL,
    sample_rate      INTEGER NOT NULL,
    channels         INTEGER NOT NULL,
    sample_width     INTEGER NOT NULL,
    total_frames     INTEGER NOT NULL,
    wav              BLOB NOT NULL,          -- 原始音频，永不修改
    current_revision INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS chapters (
    program_id  TEXT NOT NULL,
    idx         INTEGER NOT NULL,
    title       TEXT NOT NULL,
    start_frame INTEGER NOT NULL,            -- 原始帧坐标
    PRIMARY KEY (program_id, idx)
);
-- 章节的不可变修订快照：每次成功的删段提交、旧版合入或章节修改产生新修订时，
-- 都把当时生效的完整章节清单复制到该修订名下。任意修订的保留段与章节因此永远
-- 成对固定，章节被后续请求改写也不会影响历史修订、并发成功响应或 ZIP 快照。
CREATE TABLE IF NOT EXISTS revision_chapters (
    program_id  TEXT NOT NULL,
    revision    INTEGER NOT NULL,
    idx         INTEGER NOT NULL,
    title       TEXT NOT NULL,
    start_frame INTEGER NOT NULL,            -- 原始帧坐标
    PRIMARY KEY (program_id, revision, idx)
);
CREATE TABLE IF NOT EXISTS must_keeps (
    program_id  TEXT NOT NULL,
    idx         INTEGER NOT NULL,
    start_frame INTEGER NOT NULL,            -- 原始帧坐标，左闭
    end_frame   INTEGER NOT NULL,            -- 原始帧坐标，右开
    PRIMARY KEY (program_id, idx)
);
-- 每次成功提交追加一个不可变修订，历史修订永不改写，
-- 因此下载时按修订号取数即可得到一致快照。
CREATE TABLE IF NOT EXISTS revisions (
    program_id TEXT NOT NULL,
    revision   INTEGER NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY (program_id, revision)
);
CREATE TABLE IF NOT EXISTS revision_segments (
    program_id  TEXT NOT NULL,
    revision    INTEGER NOT NULL,
    idx         INTEGER NOT NULL,
    start_frame INTEGER NOT NULL,
    end_frame   INTEGER NOT NULL,
    PRIMARY KEY (program_id, revision, idx)
);
"""

app = Flask(__name__)
app.json.ensure_ascii = False
# 30 秒 16 kHz 16 bit 单声道 PCM 约 960 KB，留足余量
app.config["MAX_CONTENT_LENGTH"] = 2 * 1024 * 1024


# ---- 错误处理 ---------------------------------------------------------------
class ApiError(Exception):
    def __init__(self, status, code, message, **extra):
        super().__init__(message)
        self.status = status
        self.code = code
        self.message = message
        self.extra = extra


@app.errorhandler(ApiError)
def handle_api_error(err):
    payload = {"error": err.code, "message": err.message}
    payload.update(err.extra)
    return jsonify(payload), err.status


@app.errorhandler(404)
def handle_404(err):
    return jsonify(error="not_found", message="请求的资源不存在"), 404


@app.errorhandler(405)
def handle_405(err):
    return jsonify(error="method_not_allowed", message="该路径不支持此 HTTP 方法"), 405


@app.errorhandler(413)
def handle_413(err):
    return jsonify(error="payload_too_large", message="请求体过大（上限 2 MB）"), 413


@app.errorhandler(500)
def handle_500(err):
    return jsonify(error="internal_error", message="服务器内部错误"), 500


# ---- 数据库 -----------------------------------------------------------------
def init_db():
    directory = os.path.dirname(os.path.abspath(DB_PATH))
    os.makedirs(directory, exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    try:
        # 先判断是否为引入章节修订快照之前的旧库：旧库没有 revision_chapters 表。
        had_revision_chapters = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='revision_chapters'"
        ).fetchone() is not None
        conn.execute("PRAGMA journal_mode=WAL")
        conn.executescript(SCHEMA)
        if not had_revision_chapters:
            # 旧库迁移：章节表当时只对新库的创建流程写入，而旧版章节一旦创建即
            # 不可修改，所以每个既有修订的章节快照都等于当前章节表内容。
            # NOT EXISTS 使迁移可重入（INSERT 0 行也能正确区分“空章节”与“未回填”）。
            conn.execute(
                "INSERT INTO revision_chapters (program_id, revision, idx, title, start_frame) "
                "SELECT r.program_id, r.revision, c.idx, c.title, c.start_frame "
                "FROM revisions r "
                "JOIN chapters c ON c.program_id = r.program_id "
                "WHERE NOT EXISTS (SELECT 1 FROM revision_chapters rc "
                "                  WHERE rc.program_id = r.program_id AND rc.revision = r.revision)"
            )
        conn.commit()
    finally:
        conn.close()


def get_db():
    """每个请求一个连接；autocommit 模式，事务由代码显式控制。"""
    if "db" not in g:
        conn = sqlite3.connect(DB_PATH, timeout=30, isolation_level=None)
        conn.row_factory = sqlite3.Row
        g.db = conn
    return g.db


@app.teardown_appcontext
def close_db(exc):
    db = g.pop("db", None)
    if db is not None:
        db.close()


# ---- 输入解析与校验 ----------------------------------------------------------
def _is_int(value):
    return isinstance(value, int) and not isinstance(value, bool)


def parse_wav(data):
    """校验 WAV 规格，返回总帧数。"""
    try:
        with wave.open(io.BytesIO(data), "rb") as w:
            channels = w.getnchannels()
            sampwidth = w.getsampwidth()
            framerate = w.getframerate()
            total_frames = w.getnframes()
    except (wave.Error, EOFError) as exc:
        raise ApiError(400, "invalid_wav", f"无法解析 WAV 文件：{exc}")
    if channels != CHANNELS:
        raise ApiError(400, "invalid_wav", f"声道数必须为 1（单声道），实际为 {channels}")
    if sampwidth != SAMPLE_WIDTH:
        raise ApiError(400, "invalid_wav", f"采样位深必须为 16 位 PCM（2 字节），实际为 {sampwidth * 8} 位")
    if framerate != SAMPLE_RATE:
        raise ApiError(400, "invalid_wav", f"采样率必须为 {SAMPLE_RATE} Hz，实际为 {framerate} Hz")
    if total_frames <= 0:
        raise ApiError(400, "invalid_wav", "音频为空（0 帧）")
    if total_frames > MAX_FRAMES:
        raise ApiError(
            400, "invalid_wav",
            f"音频时长不得超过 {MAX_SECONDS} 秒（{MAX_FRAMES} 帧），实际为 {total_frames} 帧",
        )
    return total_frames


def _json_field(name, default):
    """解析 multipart 表单中的 JSON 字段。"""
    raw = request.form.get(name)
    if raw is None or raw.strip() == "":
        return default
    try:
        return json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ApiError(400, "invalid_json_field", f"表单字段 {name} 不是合法 JSON：{exc}")


def parse_chapters(raw, total_frames):
    """章节：{"title": ..., "start_frame": n} 或裸整数；按起点升序去重。"""
    if not isinstance(raw, list):
        raise ApiError(400, "invalid_chapters", "chapters 必须为数组")
    chapters = []
    for i, item in enumerate(raw):
        if _is_int(item):
            title, start = f"chapter-{i + 1}", item
        elif isinstance(item, dict):
            start = item.get("start_frame")
            title = item.get("title")
            title = str(title).strip() if title is not None else ""
            if not title:
                title = f"chapter-{i + 1}"
        else:
            raise ApiError(400, "invalid_chapters", "章节须为整数或 {\"title\":..,\"start_frame\":..} 对象")
        if not _is_int(start):
            raise ApiError(400, "invalid_chapters", f"第 {i} 个章节的 start_frame 必须为整数")
        if not 0 <= start < total_frames:
            raise ApiError(
                400, "invalid_chapters",
                f"章节起点 {start} 超出音频范围 [0, {total_frames})",
            )
        chapters.append({"title": title, "start_frame": start})
    chapters.sort(key=lambda c: c["start_frame"])
    for prev, cur in zip(chapters, chapters[1:]):
        if cur["start_frame"] == prev["start_frame"]:
            raise ApiError(400, "invalid_chapters", f"章节起点 {cur['start_frame']} 重复")
    return chapters


def _parse_interval(item, what):
    if isinstance(item, dict):
        start, end = item.get("start_frame"), item.get("end_frame")
    elif isinstance(item, (list, tuple)) and len(item) == 2:
        start, end = item
    else:
        raise ApiError(
            400, "invalid_interval",
            f"{what} 的每一项须为 {{\"start_frame\":..,\"end_frame\":..}} 或 [start, end]",
        )
    if not _is_int(start) or not _is_int(end):
        raise ApiError(400, "invalid_interval", f"{what} 的帧坐标必须为整数")
    return start, end


def parse_must_keeps(raw, total_frames):
    """必留口播区间：非空、在音频内；登记时排序规整。"""
    if not isinstance(raw, list):
        raise ApiError(400, "invalid_must_keep", "must_keep 必须为数组")
    intervals = []
    for i, item in enumerate(raw):
        start, end = _parse_interval(item, "must_keep")
        if not 0 <= start < end <= total_frames:
            raise ApiError(
                400, "invalid_must_keep",
                f"必留口播区间 [{start}, {end}) 无效：需满足 0 <= start_frame < end_frame <= {total_frames}",
            )
        intervals.append((start, end))
    intervals.sort()
    return intervals


def validate_keep_segments(raw, total_frames):
    """保留清单：左闭右开、非空、升序、不重叠、在音频内。顺序错误即拒绝，不代为排序。"""
    if not isinstance(raw, list):
        raise ApiError(400, "invalid_segments", "keep_segments 必须为数组")
    segments = []
    for i, item in enumerate(raw):
        start, end = _parse_interval(item, "keep_segments")
        if not 0 <= start < end <= total_frames:
            raise ApiError(
                400, "invalid_segments",
                f"保留段 [{start}, {end}) 无效：需满足 0 <= start_frame < end_frame <= {total_frames}（左闭右开、非空、在音频内）",
            )
        if segments and start < segments[-1][1]:
            prev_start, prev_end = segments[-1]
            raise ApiError(
                400, "invalid_segments",
                f"保留段 [{start}, {end}) 与前一段 [{prev_start}, {prev_end}) 重叠或未按升序排列",
            )
        segments.append((start, end))
    return segments


def check_must_keeps_covered(must_keeps, segments):
    for ms, me in must_keeps:
        if not any(s <= ms and me <= e for s, e in segments):
            raise ApiError(
                400, "must_keep_uncovered",
                f"必留口播区间 [{ms}, {me}) 未完整落入任何一个保留段",
                must_keep={"start_frame": ms, "end_frame": me},
            )


# ---- 核心换算 ----------------------------------------------------------------
def project_chapters(chapters, segments):
    """把原始帧坐标的章节换算为成品帧坐标。

    被删除区间内的章节不再出现；位于保留段起点（左闭）的章节保留。
    输入始终是原始帧坐标，因此任意多次编辑都不会积累偏移。
    """
    projected = []
    offset = 0  # 已确认落在当前章节之前的保留段总长（成品帧）
    j = 0
    for chapter in chapters:  # chapters 已按 start_frame 升序
        cf = chapter["start_frame"]
        while j < len(segments) and segments[j][1] <= cf:
            offset += segments[j][1] - segments[j][0]
            j += 1
        if j < len(segments) and segments[j][0] <= cf < segments[j][1]:
            projected.append({
                "title": chapter["title"],
                "start_frame": cf,
                "product_start_frame": offset + (cf - segments[j][0]),
            })
    return projected


def build_product_wav(wav_bytes, segments):
    """按保留段从原始音频真正裁接出成品 WAV（参数与原始一致）。"""
    with wave.open(io.BytesIO(wav_bytes), "rb") as src:
        pieces = []
        for start, end in segments:
            src.setpos(start)
            pieces.append(src.readframes(end - start))
    buf = io.BytesIO()
    with wave.open(buf, "wb") as out:
        out.setnchannels(CHANNELS)
        out.setsampwidth(SAMPLE_WIDTH)
        out.setframerate(SAMPLE_RATE)
        for piece in pieces:
            out.writeframes(piece)
    return buf.getvalue()


# ---- 三方合入 ----------------------------------------------------------------
def _contains(segments, point):
    """有序、不重叠的保留段是否包含该帧。"""
    for s, e in segments:
        if s <= point < e:
            return True
        if s > point:
            break
    return False


def _symmetric_difference(a, b, total_frames):
    """两份保留清单保留状态不同的帧区间（按帧的集合对称差），结果已规整为升序非空段。"""
    points = {0, total_frames}
    for s, e in a + b:
        points.add(s)
        points.add(e)
    points = sorted(points)
    diffs = []
    for x, y in zip(points, points[1:]):
        # 单元 [x, y) 内任何帧的归属都一致，取左端点判定即可
        if _contains(a, x) != _contains(b, x):
            diffs.append((x, y))
    return _coalesce(diffs)


def _coalesce(segments):
    """合并相邻（右端 == 下一段左端）的升序区间。"""
    out = []
    for s, e in segments:
        if out and out[-1][1] == s:
            out[-1] = (out[-1][0], e)
        else:
            out.append((s, e))
    return out


def _intersect(a, b):
    """两个升序、不重叠区间集的交集。"""
    i = j = 0
    out = []
    while i < len(a) and j < len(b):
        s = max(a[i][0], b[j][0])
        e = min(a[i][1], b[j][1])
        if s < e:
            out.append((s, e))
        if a[i][1] <= b[j][1]:
            i += 1
        else:
            j += 1
    return out


def three_way_merge(base, theirs, current, total_frames):
    """三方合入：基版 base、建议 theirs、当前 current（均为原始帧坐标的保留段）。

    帧在某一侧「被改动」定义为该侧与基版的保留状态不同。两侧改动区间相交即冲突
    （含双方都删/改同一段）；互不重叠时，改动的帧取改动方状态，未改动的帧维持基版。
    返回 (合并保留段, 冲突区间)；冲突时保留段为 None。
    """
    changed_theirs = _symmetric_difference(base, theirs, total_frames)
    changed_current = _symmetric_difference(base, current, total_frames)
    conflicts = _coalesce(_intersect(changed_theirs, changed_current))
    if conflicts:
        return None, conflicts

    points = sorted({0, total_frames}
                    | {p for seg in theirs + current + base for p in seg})
    merged = []
    for x, y in zip(points, points[1:]):
        in_theirs = _contains(theirs, x)
        in_current = _contains(current, x)
        in_base = _contains(base, x)
        # 两侧改动不相交，故同一帧至多一侧与基版不同；都未改动则沿用基版。
        kept = in_theirs if in_theirs != in_base else (
            in_current if in_current != in_base else in_base
        )
        if kept:
            merged.append((x, y))
    return _coalesce(merged), []


# ---- 数据访问 -----------------------------------------------------------------
def _fetch_program(db, pid):
    row = db.execute("SELECT * FROM programs WHERE id = ?", (pid,)).fetchone()
    if row is None:
        raise ApiError(404, "not_found", f"节目 {pid} 不存在")
    return row


def _fetch_segments(db, pid, revision):
    rows = db.execute(
        "SELECT start_frame, end_frame FROM revision_segments "
        "WHERE program_id = ? AND revision = ? ORDER BY idx",
        (pid, revision),
    ).fetchall()
    return [(r["start_frame"], r["end_frame"]) for r in rows]


def _fetch_chapters(db, pid, revision):
    """取某一修订名下的章节快照（原始帧坐标）。"""
    rows = db.execute(
        "SELECT title, start_frame FROM revision_chapters "
        "WHERE program_id = ? AND revision = ? ORDER BY idx",
        (pid, revision),
    ).fetchall()
    return [{"title": r["title"], "start_frame": r["start_frame"]} for r in rows]


def _fetch_must_keeps(db, pid):
    rows = db.execute(
        "SELECT start_frame, end_frame FROM must_keeps WHERE program_id = ? ORDER BY idx", (pid,)
    ).fetchall()
    return [(r["start_frame"], r["end_frame"]) for r in rows]


def _program_payload(db, row, revision=None, segments=None):
    """构造版本快照载荷。

    revision 默认取当前修订号；显式传入时，保留段、成品帧数与章节投影都按
    该次写入自己的修订计算——提交/合入事务提交后，当前修订号可能已被其它并发
    请求推进，成功响应必须描述本请求实际写入的那个修订，而不是此刻的最新修订。
    历史修订不可变，故按显式修订号取段与按提交时已算出的段是等价的。
    """
    pid = row["id"]
    if revision is None:
        revision = row["current_revision"]
    if segments is None:
        segments = _fetch_segments(db, pid, revision)
    # 章节同样按本次修订取不可变快照：章节修改产生新修订后，历史修订与
    # 「自己写入修订」的成功响应仍展示该修订对应的章节。
    chapters = _fetch_chapters(db, pid, revision)
    must_keeps = _fetch_must_keeps(db, pid)
    product_frames = sum(e - s for s, e in segments)
    return {
        "program_id": pid,
        "revision": revision,
        "sample_rate": row["sample_rate"],
        "channels": row["channels"],
        "sample_width": row["sample_width"],
        "total_frames": row["total_frames"],
        "duration_seconds": row["total_frames"] / SAMPLE_RATE,
        "keep_segments": [{"start_frame": s, "end_frame": e} for s, e in segments],
        "product_frames": product_frames,
        "product_seconds": product_frames / SAMPLE_RATE,
        "chapters": project_chapters(chapters, segments),
        "must_keep": [{"start_frame": s, "end_frame": e} for s, e in must_keeps],
    }


# ---- 路由 ---------------------------------------------------------------------
@app.get("/health")
def health():
    return jsonify(status="ok")


@app.post("/programs")
def create_program():
    if "audio" not in request.files:
        raise ApiError(400, "missing_audio", "缺少 multipart 文件字段 audio（WAV 文件）")
    data = request.files["audio"].read()
    if not data:
        raise ApiError(400, "missing_audio", "音频文件为空")
    total_frames = parse_wav(data)
    chapters = parse_chapters(_json_field("chapters", []), total_frames)
    must_keeps = parse_must_keeps(_json_field("must_keep", []), total_frames)

    pid = uuid.uuid4().hex
    now = datetime.now(timezone.utc).isoformat()
    db = get_db()
    db.execute("BEGIN")
    try:
        db.execute(
            "INSERT INTO programs (id, created_at, sample_rate, channels, sample_width,"
            " total_frames, wav, current_revision) VALUES (?,?,?,?,?,?,?,0)",
            (pid, now, SAMPLE_RATE, CHANNELS, SAMPLE_WIDTH, total_frames, data),
        )
        db.executemany(
            "INSERT INTO chapters (program_id, idx, title, start_frame) VALUES (?,?,?,?)",
            [(pid, i, c["title"], c["start_frame"]) for i, c in enumerate(chapters)],
        )
        db.executemany(
            "INSERT INTO revision_chapters (program_id, revision, idx, title, start_frame)"
            " VALUES (?,?,?,?,?)",
            [(pid, 0, i, c["title"], c["start_frame"]) for i, c in enumerate(chapters)],
        )
        db.executemany(
            "INSERT INTO must_keeps (program_id, idx, start_frame, end_frame) VALUES (?,?,?,?)",
            [(pid, i, s, e) for i, (s, e) in enumerate(must_keeps)],
        )
        # 修订 0：保留全段
        db.execute(
            "INSERT INTO revisions (program_id, revision, created_at) VALUES (?,?,?)",
            (pid, 0, now),
        )
        db.execute(
            "INSERT INTO revision_segments (program_id, revision, idx, start_frame, end_frame)"
            " VALUES (?,?,?,?,?)",
            (pid, 0, 0, 0, total_frames),
        )
        db.execute("COMMIT")
    except Exception:
        db.execute("ROLLBACK")
        raise

    row = _fetch_program(db, pid)
    return jsonify(_program_payload(db, row, revision=0)), 201


@app.get("/programs")
def list_programs():
    db = get_db()
    rows = db.execute(
        "SELECT id, created_at, total_frames, current_revision FROM programs ORDER BY created_at"
    ).fetchall()
    return jsonify(programs=[{
        "program_id": r["id"],
        "created_at": r["created_at"],
        "total_frames": r["total_frames"],
        "revision": r["current_revision"],
    } for r in rows])


@app.get("/programs/<pid>")
def get_program(pid):
    db = get_db()
    row = _fetch_program(db, pid)
    return jsonify(_program_payload(db, row))


@app.put("/programs/<pid>/keeps")
def put_keeps(pid):
    body = request.get_json(silent=True)
    if body is None:
        raise ApiError(400, "invalid_json", "请求体必须为 JSON（Content-Type: application/json）")
    if "expected_revision" not in body:
        raise ApiError(400, "missing_expected_revision", "缺少字段 expected_revision")
    expected = body["expected_revision"]
    if not _is_int(expected):
        raise ApiError(400, "invalid_expected_revision", "expected_revision 必须为整数")
    if "keep_segments" not in body:
        raise ApiError(400, "missing_keep_segments", "缺少字段 keep_segments（完整保留清单）")
    raw_segments = body["keep_segments"]

    db = get_db()
    # BEGIN IMMEDIATE 立即取得写锁，串行化并发提交：
    # “检查修订号 -> 写入新修订” 是原子的，并发下后到者会看到新修订号并按过期处理。
    db.execute("BEGIN IMMEDIATE")
    try:
        row = _fetch_program(db, pid)
        current = row["current_revision"]
        if expected != current:
            raise ApiError(
                409, "stale_revision",
                f"预期修订号 {expected} 与当前修订号 {current} 不一致；"
                "请重新查询当前版本后再提交，本次提交未生效",
                current_revision=current,
            )
        segments = validate_keep_segments(raw_segments, row["total_frames"])
        must_keeps = _fetch_must_keeps(db, pid)
        check_must_keeps_covered(must_keeps, segments)

        new_revision = current + 1
        now = datetime.now(timezone.utc).isoformat()
        db.execute(
            "INSERT INTO revisions (program_id, revision, created_at) VALUES (?,?,?)",
            (pid, new_revision, now),
        )
        db.executemany(
            "INSERT INTO revision_segments (program_id, revision, idx, start_frame, end_frame)"
            " VALUES (?,?,?,?,?)",
            [(pid, new_revision, i, s, e) for i, (s, e) in enumerate(segments)],
        )
        # 删段不改章节：把提交时生效的章节清单原样快照到新修订，
        # 这样与并发的章节修改互不串改，本笔响应永远对应本修订的章节。
        db.execute(
            "INSERT INTO revision_chapters (program_id, revision, idx, title, start_frame) "
            "SELECT ?, ?, idx, title, start_frame FROM revision_chapters "
            "WHERE program_id = ? AND revision = ?",
            (pid, new_revision, pid, current),
        )
        db.execute(
            "UPDATE programs SET current_revision = ? WHERE id = ?", (new_revision, pid)
        )
        db.execute("COMMIT")
    except Exception:
        db.execute("ROLLBACK")
        raise

    # 返回本次写入的 new_revision 快照，而非此刻的最新修订：
    # 并发下本请求提交后、响应生成前，可能已有另一个提交/合入/章节修改再推进修订号，
    # 成功响应中的修订号、保留段、成品帧数与章节投影必须彼此对应。
    return jsonify(_program_payload(db, row, revision=new_revision, segments=segments))


@app.put("/programs/<pid>/chapters")
def put_chapters(pid):
    """按预期修订号提交完整章节清单（无须重传音频）。

    - 章节校验沿用创建节目时的规则（升序、起点不重复、在音频范围内；允许空清单）。
    - 成功时保留段原样沿用当前修订，只把新章节清单写入新修订；修订号 +1。
    - 过期（expected_revision != 当前）或章节非法时返回原因，当前版本保持不变。
    - 与删段提交/旧版合入共用同一把 BEGIN IMMEDIATE 写锁：并发的章节修改、删段、
      合入被串行化，每笔成功响应只描述自己写入修订的章节与保留段；晚到的一笔
      会看到新修订号并按过期处理，不会覆盖先到者。
    """
    body = request.get_json(silent=True)
    if body is None:
        raise ApiError(400, "invalid_json", "请求体必须为 JSON（Content-Type: application/json）")
    if "expected_revision" not in body:
        raise ApiError(400, "missing_expected_revision", "缺少字段 expected_revision")
    expected = body["expected_revision"]
    if not _is_int(expected):
        raise ApiError(400, "invalid_expected_revision", "expected_revision 必须为整数")
    if "chapters" not in body:
        raise ApiError(400, "missing_chapters", "缺少字段 chapters（完整章节清单，可为空数组）")
    raw_chapters = body["chapters"]

    db = get_db()
    db.execute("BEGIN IMMEDIATE")
    try:
        row = _fetch_program(db, pid)
        current = row["current_revision"]
        if expected != current:
            raise ApiError(
                409, "stale_revision",
                f"预期修订号 {expected} 与当前修订号 {current} 不一致；"
                "请重新查询当前版本后再提交，本次提交未生效",
                current_revision=current,
            )
        # 沿用创建时的章节校验规则（含按起点升序、重复起点拒绝）；允许清空。
        chapters = parse_chapters(raw_chapters, row["total_frames"])
        segments = _fetch_segments(db, pid, current)

        new_revision = current + 1
        now = datetime.now(timezone.utc).isoformat()
        db.execute(
            "INSERT INTO revisions (program_id, revision, created_at) VALUES (?,?,?)",
            (pid, new_revision, now),
        )
        # 保留段原样沿用：复制当前修订的全部保留段到新修订。
        db.execute(
            "INSERT INTO revision_segments (program_id, revision, idx, start_frame, end_frame) "
            "SELECT ?, ?, idx, start_frame, end_frame FROM revision_segments "
            "WHERE program_id = ? AND revision = ?",
            (pid, new_revision, pid, current),
        )
        db.executemany(
            "INSERT INTO revision_chapters (program_id, revision, idx, title, start_frame)"
            " VALUES (?,?,?,?,?)",
            [(pid, new_revision, i, c["title"], c["start_frame"])
             for i, c in enumerate(chapters)],
        )
        # 当前章节镜像同步为新清单（仅供旧库迁移使用；读路径一律按修订取快照）。
        db.execute("DELETE FROM chapters WHERE program_id = ?", (pid,))
        db.executemany(
            "INSERT INTO chapters (program_id, idx, title, start_frame) VALUES (?,?,?,?)",
            [(pid, i, c["title"], c["start_frame"]) for i, c in enumerate(chapters)],
        )
        db.execute(
            "UPDATE programs SET current_revision = ? WHERE id = ?", (new_revision, pid)
        )
        db.execute("COMMIT")
    except Exception:
        db.execute("ROLLBACK")
        raise

    # 与删段提交/合入一致：即使提交后又有别的章节或剪辑修改写入更新的修订，
    # 本笔成功响应只展示自己写入修订的保留段、成品帧数与章节投影。
    return jsonify(_program_payload(db, row, revision=new_revision, segments=segments))


@app.post("/programs/<pid>/merges")
def merge_keeps(pid):
    """旧版剪辑建议合入：以 base_revision 为共同基版做三方合入。

    建议、当前修订、基版的「比较 -> 校验 -> 写入」在同一个 BEGIN IMMEDIATE
    事务内完成，因此并发合入/提交看到的都是受保护的当前状态，
    已生效的修改不会被后来者基于旧状态覆盖丢失。

    成功响应按本请求写入的新修订号构造：事务提交后当前修订号可能已被另一个
    并发合入推进，此时响应仍描述自己写入的修订（修订号、保留段、成品帧数、
    章节投影彼此对应），而不是响应生成时的最新修订。
    """
    body = request.get_json(silent=True)
    if body is None:
        raise ApiError(400, "invalid_json", "请求体必须为 JSON（Content-Type: application/json）")
    if "base_revision" not in body:
        raise ApiError(400, "missing_base_revision", "缺少字段 base_revision（建议所依据的既有修订号）")
    base_revision = body["base_revision"]
    if not _is_int(base_revision) or base_revision < 0:
        raise ApiError(400, "invalid_base_revision", "base_revision 必须为非负整数")
    if "keep_segments" not in body:
        raise ApiError(400, "missing_keep_segments", "缺少字段 keep_segments（完整保留清单）")
    raw_segments = body["keep_segments"]

    db = get_db()
    # 与 PUT 同一把写锁：基版比较、清单校验、冲突计算与新修订写入是原子的。
    db.execute("BEGIN IMMEDIATE")
    try:
        row = _fetch_program(db, pid)
        total_frames = row["total_frames"]
        current = row["current_revision"]

        base_exists = db.execute(
            "SELECT 1 FROM revisions WHERE program_id = ? AND revision = ?",
            (pid, base_revision),
        ).fetchone()
        if base_exists is None:
            raise ApiError(
                404, "base_revision_not_found",
                f"基版修订号 {base_revision} 不存在；当前修订号为 {current}，本次合入未生效",
                base_revision=base_revision,
                current_revision=current,
            )

        base_segments = _fetch_segments(db, pid, base_revision)
        current_segments = _fetch_segments(db, pid, current)
        # 建议清单非法：结构性校验失败即拒绝（左闭右开、非空、升序、不重叠、在音频内）
        suggested_segments = validate_keep_segments(raw_segments, total_frames)

        merged, conflicts = three_way_merge(
            base_segments, suggested_segments, current_segments, total_frames
        )
        if conflicts:
            raise ApiError(
                409, "merge_conflict",
                "建议与当前修订相对于基版改动了相交的帧区间，本次合入未生效；"
                "请拉取当前版本后重新生成建议（双方同时删除同一段也视为冲突）",
                base_revision=base_revision,
                current_revision=current,
                conflicts=[{"start_frame": s, "end_frame": e} for s, e in conflicts],
            )

        must_keeps = _fetch_must_keeps(db, pid)
        check_must_keeps_covered(must_keeps, merged)

        new_revision = current + 1
        now = datetime.now(timezone.utc).isoformat()
        db.execute(
            "INSERT INTO revisions (program_id, revision, created_at) VALUES (?,?,?)",
            (pid, new_revision, now),
        )
        db.executemany(
            "INSERT INTO revision_segments (program_id, revision, idx, start_frame, end_frame)"
            " VALUES (?,?,?,?,?)",
            [(pid, new_revision, i, s, e) for i, (s, e) in enumerate(merged)],
        )
        # 合入只处理保留段：章节沿用提交时当前修订的章节清单并快照到新修订。
        db.execute(
            "INSERT INTO revision_chapters (program_id, revision, idx, title, start_frame) "
            "SELECT ?, ?, idx, title, start_frame FROM revision_chapters "
            "WHERE program_id = ? AND revision = ?",
            (pid, new_revision, pid, current),
        )
        db.execute(
            "UPDATE programs SET current_revision = ? WHERE id = ?", (new_revision, pid)
        )
        db.execute("COMMIT")
    except Exception:
        db.execute("ROLLBACK")
        raise

    # 与 PUT 同理：返回本次合入自己写入的 new_revision 快照。
    # 并发合入/章节修改时本请求先提交修订 N、另一请求后提交修订 N+1 的情况下，
    # 这里也必须返回修订 N 的保留段、成品帧数与章节投影，而不是修订 N+1。
    return jsonify(_program_payload(db, row, revision=new_revision, segments=merged))


@app.get("/programs/<pid>/download")
def download(pid):
    db = get_db()
    # 在单个读事务内取出修订号、保留段、章节与原始音频，
    # 之后的一切计算都基于这份不可变快照：并发编辑不会导致 WAV 与 JSON 混用版本。
    db.execute("BEGIN")
    try:
        row = _fetch_program(db, pid)
        revision = row["current_revision"]
        segments = _fetch_segments(db, pid, revision)
        chapters = _fetch_chapters(db, pid, revision)
        wav_bytes = row["wav"]
        db.execute("COMMIT")
    except Exception:
        db.execute("ROLLBACK")
        raise

    product_wav = build_product_wav(wav_bytes, segments)
    projected = project_chapters(chapters, segments)
    chapters_doc = {
        "program_id": pid,
        "revision": revision,
        "sample_rate": SAMPLE_RATE,
        "channels": CHANNELS,
        "sample_width": SAMPLE_WIDTH,
        "product_frames": sum(e - s for s, e in segments),
        "chapters": [
            {"title": c["title"], "start_frame": c["product_start_frame"]}
            for c in projected
        ],
    }

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("audio.wav", product_wav)
        zf.writestr(
            "chapters.json",
            json.dumps(chapters_doc, ensure_ascii=False, indent=2),
        )
    buf.seek(0)
    return send_file(
        buf,
        mimetype="application/zip",
        as_attachment=True,
        download_name=f"program-{pid}-rev{revision}.zip",
    )


init_db()

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 8000)))
