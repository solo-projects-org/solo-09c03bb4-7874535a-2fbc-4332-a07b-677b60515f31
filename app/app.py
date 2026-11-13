"""林道巡护倒木采集系统（Flask + SQLite）

- /            采集页（巡护员）：离线可用，待传记录存浏览器 IndexedDB
- /records     清障队查看页：只展示已确认入库的记录，含撤照入口
- /api/records GET 列表 / POST 补传（uuid 幂等，同一笔只入库一次）
- /api/records/<id>/takedown  POST 撤照：撤下所有字节相同的已入库副本
- /photos/...  已入库的原照（已撤照的一律不再供图）

撤照语义（误拍路人正脸时，数据员撤下照片、清障队仍查得到记录）：
- 撤照按"字节相同"（SHA-256）生效：同一哈希的所有已入库副本一并撤下，
  记录本身（编号、坐标、备注）保留；
- 撤照生效（takedowns 登记哈希并提交）后，旧链接立即不再供图；
  此后补传的同字节照片（即使 uuid 不同）只收记录、不挂照片；
- 原照文件删除失败或进程中途退出：文件仍不可查看，记录标记"待清理"，
  重启后继续删除。
"""
import hashlib
import os
import sqlite3
import uuid as uuidlib
from datetime import datetime, timezone

from flask import (Flask, abort, g, jsonify, render_template, request,
                   send_from_directory)
from werkzeug.utils import secure_filename

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.environ.get("DATA_DIR", os.path.join(BASE_DIR, "data"))
PHOTO_DIR = os.path.join(DATA_DIR, "photos")
DB_PATH = os.path.join(DATA_DIR, "records.db")
ALLOWED_EXT = {".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp"}

# 照片状态
STATE_OK = "ok"                          # 正常供图
STATE_PENDING_CLEANUP = "pending_cleanup"  # 已撤照、文件待删除（不可查看）
STATE_PURGED = "purged"                  # 已撤照、文件已删除（或从未落盘）

app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = 30 * 1024 * 1024  # 原照最大 30MB

SCHEMA = """
CREATE TABLE IF NOT EXISTS records (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    uuid        TEXT NOT NULL UNIQUE,   -- 浏览器生成的幂等键
    lat         REAL NOT NULL,
    lng         REAL NOT NULL,
    note        TEXT NOT NULL DEFAULT '',
    photo       TEXT NOT NULL DEFAULT '',  -- photos 目录下的文件名；撤照后清空
    photo_sha   TEXT NOT NULL DEFAULT '',  -- 原照字节 SHA-256（撤照比对用）
    photo_state TEXT NOT NULL DEFAULT 'ok',-- ok / pending_cleanup / purged
    created_at  TEXT NOT NULL,          -- 现场采集时间（浏览器上报）
    uploaded_at TEXT NOT NULL           -- 入库时间（服务器）
);
CREATE TABLE IF NOT EXISTS takedowns (
    sha256      TEXT PRIMARY KEY,       -- 已撤照原照的字节哈希
    created_at  TEXT NOT NULL           -- 撤照生效时间
);
"""


def init_db():
    os.makedirs(PHOTO_DIR, exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.executescript(SCHEMA)
    # ---- 老库迁移：补列 + 回填已有原照的字节哈希 ----
    cols = {r[1] for r in conn.execute("PRAGMA table_info(records)")}
    if "photo_sha" not in cols:
        conn.execute(
            "ALTER TABLE records ADD COLUMN photo_sha TEXT NOT NULL DEFAULT ''")
    if "photo_state" not in cols:
        conn.execute(
            "ALTER TABLE records ADD COLUMN photo_state TEXT NOT NULL DEFAULT 'ok'")
    conn.commit()
    rows = conn.execute(
        "SELECT id, photo FROM records WHERE photo != '' AND photo_sha = ''"
    ).fetchall()
    for rid, photo in rows:
        try:
            with open(os.path.join(PHOTO_DIR, photo), "rb") as f:
                digest = hashlib.sha256(f.read()).hexdigest()
            conn.execute("UPDATE records SET photo_sha = ? WHERE id = ?",
                         (digest, rid))
        except OSError:
            # 文件已不在盘上：等同已撤照
            conn.execute(
                "UPDATE records SET photo = '', photo_state = ? WHERE id = ?",
                (STATE_PURGED, rid))
    conn.commit()
    conn.close()


def purge_photo_file(conn, rid, fname):
    """删除单个已撤照文件。

    成功（或文件本就不在）→ 记录改为 purged 并清空文件名；
    失败 → 保持 pending_cleanup（供图路由按状态拦截，重启后继续删）。
    """
    if not fname:
        conn.execute("UPDATE records SET photo_state = ? WHERE id = ?",
                     (STATE_PURGED, rid))
        conn.commit()
        return True
    try:
        os.remove(os.path.join(PHOTO_DIR, fname))
    except FileNotFoundError:
        pass  # 文件已不在，视为清理完成
    except OSError:
        return False  # 删盘失败：保持待清理
    conn.execute(
        "UPDATE records SET photo = '', photo_state = ? WHERE id = ?",
        (STATE_PURGED, rid))
    conn.commit()
    return True


def resume_takedowns():
    """启动时恢复未完成的撤照（上次删盘失败或中途退出）：

    1) 兜底：takedowns 已登记哈希但仍有 ok 记录的，先补齐待清理标记；
    2) 所有待清理文件重试删除。
    """
    conn = sqlite3.connect(DB_PATH)
    try:
        conn.execute(
            "UPDATE records SET photo_state = ? "
            "WHERE photo_state = ? AND photo_sha != '' "
            "AND photo_sha IN (SELECT sha256 FROM takedowns)",
            (STATE_PENDING_CLEANUP, STATE_OK))
        conn.commit()
        rows = conn.execute(
            "SELECT id, photo FROM records WHERE photo_state = ?",
            (STATE_PENDING_CLEANUP,)).fetchall()
        for rid, photo in rows:
            purge_photo_file(conn, rid, photo)
    finally:
        conn.close()


def get_db():
    if "db" not in g:
        g.db = sqlite3.connect(DB_PATH, timeout=10)
        g.db.row_factory = sqlite3.Row
        g.db.execute("PRAGMA journal_mode=WAL")
        g.db.execute("PRAGMA busy_timeout=5000")
    return g.db


@app.teardown_appcontext
def close_db(_exc):
    db = g.pop("db", None)
    if db is not None:
        db.close()


@app.after_request
def no_store_html(resp):
    # HTML 页面不做 HTTP 缓存，保证在线时拿到最新数据；
    # 离线可用性由 Service Worker 的 Cache Storage 负责。
    if resp.mimetype == "text/html":
        resp.headers["Cache-Control"] = "no-store"
    return resp


# ---------------- 页面 ----------------

@app.route("/")
def index():
    return render_template("index.html")


@app.route("/sw.js")
def service_worker():
    # Service Worker 必须挂在根作用域下：从 / 路径提供，控制整个站点
    resp = send_from_directory(os.path.join(BASE_DIR, "static"), "sw.js",
                               mimetype="application/javascript")
    resp.headers["Cache-Control"] = "no-cache"
    return resp


@app.route("/records")
def records_page():
    rows = get_db().execute(
        "SELECT * FROM records ORDER BY id DESC"
    ).fetchall()
    return render_template("records.html", records=rows)


# ---------------- API ----------------

@app.route("/api/records", methods=["GET"])
def api_list():
    rows = get_db().execute(
        "SELECT id, uuid, lat, lng, note, photo, photo_state, created_at, uploaded_at "
        "FROM records ORDER BY id DESC"
    ).fetchall()
    return jsonify([
        {
            "id": r["id"],
            "uuid": r["uuid"],
            "lat": r["lat"],
            "lng": r["lng"],
            "note": r["note"],
            "photo_state": r["photo_state"],
            # 已撤照的记录不再给出照片链接
            "photo_url": ("/photos/" + r["photo"]
                          if r["photo_state"] == STATE_OK and r["photo"]
                          else None),
            "created_at": r["created_at"],
            "uploaded_at": r["uploaded_at"],
        }
        for r in rows
    ])


@app.route("/api/records", methods=["POST"])
def api_create():
    # ---- 校验 ----
    client_uuid = (request.form.get("uuid") or "").strip()
    try:
        client_uuid = str(uuidlib.UUID(client_uuid))  # 规范化
    except (ValueError, AttributeError):
        return jsonify({"ok": False, "error": "uuid 缺失或格式非法"}), 400

    try:
        lat = float(request.form.get("lat", ""))
        lng = float(request.form.get("lng", ""))
    except ValueError:
        return jsonify({"ok": False, "error": "经纬度缺失或非法"}), 400
    if not (-90 <= lat <= 90 and -180 <= lng <= 180):
        return jsonify({"ok": False, "error": "经纬度超出范围"}), 400

    note = (request.form.get("note") or "").strip()[:500]
    created_at = (request.form.get("created_at") or "").strip() or \
        datetime.now(timezone.utc).isoformat()

    photo = request.files.get("photo")
    if photo is None or not photo.filename:
        return jsonify({"ok": False, "error": "缺少现场照片"}), 400

    db = get_db()

    # ---- 幂等：已入库则直接确认，不重复写入 ----
    existing = db.execute(
        "SELECT id FROM records WHERE uuid = ?", (client_uuid,)
    ).fetchone()
    if existing:
        return jsonify({"ok": True, "duplicate": True, "id": existing["id"]})

    # ---- 读入原照字节，计算哈希 ----
    data = photo.read()
    digest = hashlib.sha256(data).hexdigest()

    # ---- 撤照拦截：同字节照片只收记录，不挂照片（即使 uuid 不同）----
    blocked = db.execute(
        "SELECT 1 FROM takedowns WHERE sha256 = ?", (digest,)
    ).fetchone()

    fname = ""
    saved_path = None
    if not blocked:
        # 保存原照（文件名用 uuid，天然去重）
        ext = os.path.splitext(secure_filename(photo.filename))[1].lower()
        if ext not in ALLOWED_EXT:
            ext = ".jpg"
        fname = client_uuid + ext
        saved_path = os.path.join(PHOTO_DIR, fname)
        with open(saved_path, "wb") as f:
            f.write(data)

    # ---- 入库；唯一约束兜底并发/重试竞争 ----
    try:
        cur = db.execute(
            "INSERT INTO records (uuid, lat, lng, note, photo, photo_sha,"
            " photo_state, created_at, uploaded_at)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (client_uuid, lat, lng, note, fname, digest,
             STATE_PURGED if blocked else STATE_OK,
             created_at, datetime.now(timezone.utc).isoformat()),
        )
        db.commit()
    except sqlite3.IntegrityError:
        db.rollback()
        if saved_path:
            try:
                os.remove(saved_path)
            except OSError:
                pass
        row = db.execute(
            "SELECT id FROM records WHERE uuid = ?", (client_uuid,)
        ).fetchone()
        return jsonify({"ok": True, "duplicate": True,
                        "id": row["id"] if row else None})

    resp = {"ok": True, "duplicate": False, "id": cur.lastrowid}
    if blocked:
        resp["photo_blocked"] = True  # 同字节照片已撤照：只收记录
    return jsonify(resp)


@app.route("/api/records/<int:rid>/takedown", methods=["POST"])
def api_takedown(rid):
    """撤照：撤下与该记录原照字节相同的所有已入库副本。

    记录（编号、坐标、备注）保留；撤照生效后旧链接不再供图，
    此后同字节照片只收记录。文件删除失败的标记待清理，重启后续删。
    """
    db = get_db()
    row = db.execute("SELECT * FROM records WHERE id = ?", (rid,)).fetchone()
    if row is None:
        return jsonify({"ok": False, "error": "记录不存在"}), 404
    if row["photo_state"] != STATE_OK or not row["photo"]:
        return jsonify({"ok": False, "error": "该记录没有可撤下的照片"}), 400

    # 原照字节哈希（撤照按字节相同比对）；文件读不到就回退用入库时的哈希
    digest = row["photo_sha"]
    try:
        with open(os.path.join(PHOTO_DIR, row["photo"]), "rb") as f:
            digest = hashlib.sha256(f.read()).hexdigest()
    except OSError:
        pass

    if not digest:
        # 没有可比对的哈希（极端情况）：只撤本笔
        db.execute("UPDATE records SET photo_state = ? WHERE id = ?",
                   (STATE_PENDING_CLEANUP, rid))
        db.commit()
        cleaned = purge_photo_file(db, rid, row["photo"])
        return jsonify({"ok": True, "taken_down": 1,
                        "remaining_cleanup": 0 if cleaned else 1})

    now = datetime.now(timezone.utc).isoformat()
    # ---- 撤照生效点：同一事务登记哈希 + 所有同字节已入库副本标记待清理 ----
    # 提交后：旧链接立即不再供图；之后补传的同字节照片只收记录、不挂照片。
    db.execute(
        "INSERT OR IGNORE INTO takedowns (sha256, created_at) VALUES (?, ?)",
        (digest, now))
    db.execute(
        "UPDATE records SET photo_state = ? WHERE photo_sha = ? AND photo_state = ?",
        (STATE_PENDING_CLEANUP, digest, STATE_OK))
    db.commit()

    # ---- 删除原照文件；删盘失败的保持待清理，重启后继续 ----
    pend = db.execute(
        "SELECT id, photo FROM records WHERE photo_sha = ? AND photo_state = ?",
        (digest, STATE_PENDING_CLEANUP)).fetchall()
    remaining = 0
    for r in pend:
        if not purge_photo_file(db, r["id"], r["photo"]):
            remaining += 1

    return jsonify({"ok": True, "sha256": digest,
                    "taken_down": len(pend), "remaining_cleanup": remaining})


@app.route("/photos/<path:name>")
def photo_file(name):
    # 只有状态正常的已入库照片才供图；已撤照（含待清理）一律不再供图，
    # 即使文件因删盘失败仍留在盘上。
    row = get_db().execute(
        "SELECT photo_state FROM records WHERE photo = ?", (name,)
    ).fetchone()
    if row is None:
        abort(404)
    if row["photo_state"] != STATE_OK:
        abort(410)  # 已撤照
    return send_from_directory(PHOTO_DIR, name)


init_db()
resume_takedowns()

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8000)
