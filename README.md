# 林道巡护 · 倒木离线采集系统

巡护员在林道上（无信号）发现倒木时，用浏览器记录经纬度、拍摄现场照片，先存本机；
回站联网后一键补传，坐标与原照写入 SQLite，供清障队在网页上查看并安排清障。

## 功能与实现

| 需求 | 实现 |
| --- | --- |
| 首次在线打开后，断网刷新也能填写 | Service Worker 安装时缓存应用外壳（页面/JS/CSS/样例照片），断网时导航请求回退到缓存 |
| 待传记录不丢 | 记录（含照片 Blob）存入浏览器 IndexedDB，刷新、关机重启均在 |
| 回网点补传，坐标+原照入 SQLite | `POST /api/records`（multipart），照片落盘、元数据入库 |
| 收到确认才移出待传 | 浏览器仅在收到服务器 `{"ok": true}` 响应后才删除 IndexedDB 中的该笔 |
| 确认丢失，重传只入库一次 | 每笔记录保存时生成 UUID 作幂等键；`records.uuid` 唯一约束，重复提交返回 `duplicate: true` 直接确认，不重复写行、不重复存照片 |
| 入库后才供清障队查看 | `/records` 页面只渲染 SQLite 中已确认的记录 |
| 误拍路人正脸需撤照 | `/records` 撤照入口：按字节哈希（SHA-256）撤下所有字节相同的已入库副本，编号/坐标/备注保留；`takedowns` 表登记已撤哈希，撤照后同字节照片（即使 UUID 不同）只收记录、不挂照片；原照文件删除，删盘失败标"待清理"且不可查看，重启后续删 |
| 随附样例照片 | `app/static/sample-photo.png`（演示时无相机可点"使用样例照片"，离线也能取到） |

## 启动

```bash
docker compose up --build
```

浏览器打开 <http://localhost:8000>（Service Worker 要求 localhost 或 HTTPS 环境）。

SQLite 与已入库原照持久化在命名卷 `patrol-data`（容器内 `/data`）。

## 演示：断网采集 → 回网补传

1. **首次在线打开** `http://localhost:8000`，页面右下角无提示即已就绪（Service Worker 已缓存全部外壳资源）。
2. **模拟断网**（任选其一）：
   - 浏览器 DevTools → Network → 勾选 **Offline**；或
   - `docker compose stop`（更真实地模拟"林道上没信号"）。
3. **刷新页面** —— 页面照常打开。填写经纬度（或点"获取当前定位"），选择照片
   （演示可点"使用随附样例照片"），点 **保存到待传记录**。可多采几笔，再次刷新待传列表仍在。
4. **恢复网络**：取消 Offline，或 `docker compose start`。
5. 点 **📤 补传全部待传记录** —— 每笔收到服务器确认后才从待传列表移除。
6. 打开 **/records**（清障队查看页）即可看到已入库的坐标与原照。

**验证幂等**：补传途中若在服务器写入后断网（确认丢失），该笔仍留在待传；
再次补传时服务器按 UUID 判重，返回 `duplicate: true`，浏览器正常移出待传，
SQLite 中始终只有一行。

## 演示：撤照（误拍路人正脸）

1. 在采集页点 **使用随附样例照片** 保存并补传一笔记录；再用同一张样照补传第二笔
   （新 UUID，模拟字节相同的已入库副本）。
2. 打开 **/records**，任选一笔记有该样照的记录点 **撤照** 并确认：
   - 两笔同字节副本一并撤下，编号、坐标、备注保留，照片列显示"已撤照"；
   - 旧 `/photos/...` 链接立即不再供图。
3. 回到采集页，**再用同一张样照补传一笔新记录**（新 UUID）：记录照常入库，
   但不挂照片（只收记录），照片列直接显示"已撤照"。
4. 其他照片照常补传、查看，不受影响。

**删盘失败/中途退出**：未删掉的文件仍不可查看，`/records` 标记"待清理"，
重启（`docker compose restart`）后继续删除，清理完成自动转为"已撤照"。

## 本地开发（不用 Docker）

```bash
pip install -r requirements.txt
DATA_DIR=./data python3 app/app.py        # 或：gunicorn --chdir app -b 0.0.0.0:8000 app:app
```

## 目录结构

```
├── docker-compose.yml      # 一键启动，卷持久化 /data
├── Dockerfile              # python:3.12-slim + gunicorn
├── requirements.txt
├── app/
│   ├── app.py              # Flask 路由 + SQLite（uuid 唯一约束幂等）
│   ├── templates/
│   │   ├── index.html      # 采集页（巡护员）
│   │   └── records.html    # 已入库记录页（清障队）
│   └── static/
│       ├── app.js          # IndexedDB 待传队列 + 补传逻辑
│       ├── sw.js           # Service Worker（离线外壳缓存）
│       ├── style.css
│       └── sample-photo.png# 随附样例照片
└── tools/
    └── make_sample_photo.py# 样例照片生成脚本（纯标准库）
```

## API

- `GET  /api/records` — 已入库记录 JSON 列表（含 `photo_state`，已撤照的记录 `photo_url` 为 `null`）
- `POST /api/records` — 补传（multipart：`uuid`, `lat`, `lng`, `note`, `created_at`, `photo`）
  - 成功：`{"ok": true, "duplicate": false, "id": N}`
  - 重复（同一 uuid）：`{"ok": true, "duplicate": true, "id": N}` — 浏览器同样视为确认
  - 同字节照片已撤照：`{"ok": true, ..., "photo_blocked": true}` — 只收记录、不挂照片
  - 校验失败：`400 {"ok": false, "error": "..."}`
- `POST /api/records/<id>/takedown` — 撤照：撤下与该记录原照字节相同的所有已入库副本
  - 成功：`{"ok": true, "taken_down": N, "remaining_cleanup": M}`（M>0 表示有文件待清理，重启后续删）
- `GET  /photos/<file>` — 已入库原照（已撤照的返回 404/410，不再供图）
