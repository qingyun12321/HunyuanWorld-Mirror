# kokoni-world API

## 1. 总览

`kokoni-world` 的实际调用链路如下：

1. 前端页面 `hunyuanworld-mirror.html` 先调用 task-manager 的 `recover`
2. task-manager 根据 `project` 找到算力任务，返回 GPU 服务 `service_url`
3. 前端再调用 GPU 后端 `hunyuanworld_mirror_api.py` 的 `/health`、`/queue_status`、`/reconstruct`
4. 重建完成后，前端调用 task-manager 的 `pause` 释放任务

说明：

- 对外项目名统一使用 `kokoni-world`
- 在 `suanli-task-manager` 中，`kokoni-world` 会解析为内部项目 `hunyuanworld-mirror`
- GPU 后端默认监听端口为 `10085`

## 2. 地址配置

### 2.1 GPU 推理服务地址

GPU 推理接口基于 `service_url` 访问，`service_url` 由 task-manager 的 `POST /api/task/recover` 返回。

- `GET {service_url}/health`
- `GET {service_url}/queue_status`
- `POST {service_url}/reconstruct`

### 2.2 task-manager 地址

默认 task-manager 地址：

- `http://36.133.236.108:8090`

接口：

- `POST http://36.133.236.108:8090/api/task/recover`
- `POST http://36.133.236.108:8090/api/task/pause`
- `GET http://36.133.236.108:8090/api/projects`

前端页面支持通过 URL 参数覆盖：

- `?task_manager=http://<your-ip>:<port>`
- `?project=kokoni-world`

如果不传 `project`，当前前端默认值仍是 `hunyuanworld-mirror`。task-manager 现已支持将 `kokoni-world` 解析到同一后端任务。

## 3. task-manager 接口

### 3.1 `POST /api/task/recover`

请求类型：`application/json`

请求体：

| 字段 | 类型 | 必填 | 说明 |
|---|---|---|---|
| `project` | string | 是 | 推荐传 `kokoni-world` |

请求示例：

```bash
curl -X POST "http://36.133.236.108:8090/api/task/recover" \
  -H "Content-Type: application/json" \
  -d '{"project":"kokoni-world"}'
```

成功响应字段：

| 字段 | 类型 | 说明 |
|---|---|---|
| `service_url` | string | GPU 后端可访问地址 |
| `status` | string | 当前任务状态 |
| `recovered` | boolean | 是否在本次调用中执行了恢复 |

响应示例：

```json
{
  "service_url": "http://127.0.0.1:10085",
  "status": "running",
  "recovered": true
}
```

### 3.2 `POST /api/task/pause`

请求类型：`application/json`

请求体：

| 字段 | 类型 | 必填 | 说明 |
|---|---|---|---|
| `project` | string | 是 | 推荐传 `kokoni-world` |

请求示例：

```bash
curl -X POST "http://36.133.236.108:8090/api/task/pause" \
  -H "Content-Type: application/json" \
  -d '{"project":"kokoni-world"}'
```

响应示例：

```json
{
  "status": "paused"
}
```

## 4. GPU 接口清单

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/health` | 健康检查 |
| GET | `/queue_status` | 查询队列状态，支持按 `request_id` 查询 |
| POST | `/reconstruct` | 上传文件并执行重建，完成后返回 OSS 签名结果链接 |

## 5. GPU 接口定义

### 5.1 `GET /health`

请求参数：无

响应示例：

```json
{
  "status": "ok"
}
```

### 5.2 `GET /queue_status`

Query 参数：

| 参数 | 类型 | 必填 | 说明 |
|---|---|---|---|
| `request_id` | string | 否 | 客户端请求 ID |

响应字段：

| 字段 | 类型 | 说明 |
|---|---|---|
| `processing` | boolean | 当前是否存在正在执行的任务 |
| `pending` | integer | 当前排队任务数 |
| `status` | string | `processing` / `pending` / `idle` |
| `position` | integer | 队列位置标记 |

`position` 规则：

- `0`：该请求正在处理
- `>=1`：该请求在队列中，`1` 表示队首
- `-1`：未找到该 `request_id`

响应示例：

```json
{
  "processing": true,
  "pending": 2,
  "status": "pending",
  "position": 1
}
```

### 5.3 `POST /reconstruct`

请求类型：`multipart/form-data`

Form 参数：

| 参数 | 类型 | 必填 | 默认值 | 说明 |
|---|---|---|---|---|
| `files` | file[] | 是 | 无 | 文件列表，后端支持图片、视频，以及 `.heic` / `.heif` |
| `time_interval` | float | 否 | `1.0` | 视频抽帧间隔，单位秒 |
| `frame_selector` | string | 否 | `All` | 帧筛选值，通常为 `All` 或 `"<index>: <filename>"` |
| `show_camera` | boolean | 否 | `true` | 是否在导出的 GLB 中显示相机 |
| `show_mesh` | boolean | 否 | `true` | 是否导出网格；为 `false` 时导出点云场景 |
| `filter_sky_bg` | boolean | 否 | `false` | 是否过滤天空背景 |
| `filter_ambiguous` | boolean | 否 | `true` | 是否过滤低置信度及深度/法线边缘区域 |
| `request_id` | string | 否 | 自动生成 | 客户端请求 ID，用于轮询队列状态 |

成功响应字段：

| 字段 | 类型 | 说明 |
|---|---|---|
| `session_id` | string | 本次重建会话 ID |
| `frame_choices` | string[] | 可选帧列表，格式为 `All` 或 `"<index>: <filename>"` |
| `num_views` | integer | 处理后的视图数量 |
| `glb_url` | string | 场景 GLB 下载链接 |
| `camera_params_url` | string \| null | 相机参数下载链接 |
| `ply_url` | string \| null | 高斯 PLY 下载链接 |
| `depth_urls` | string[] | 深度图下载链接列表 |
| `normal_urls` | string[] | 法线图下载链接列表 |
| `rgb_video_url` | string \| null | 渲染 RGB 视频下载链接 |
| `depth_video_url` | string \| null | 渲染深度视频下载链接 |

错误码：

- `400`：处理后没有有效图像
- `422`：参数校验失败
- `500`：重建流程异常

请求示例：

```bash
curl -X POST "http://127.0.0.1:10085/reconstruct" \
  -F "files=@/path/to/image1.jpg" \
  -F "files=@/path/to/video1.mp4" \
  -F "time_interval=1.0" \
  -F "frame_selector=All" \
  -F "show_camera=true" \
  -F "show_mesh=true" \
  -F "filter_sky_bg=false" \
  -F "filter_ambiguous=true" \
  -F "request_id=req-001"
```

成功响应示例：

```json
{
  "session_id": "20260318_120000_req001ab",
  "frame_choices": ["All", "0: image1.jpg"],
  "num_views": 1,
  "glb_url": "https://example.com/scene.glb",
  "camera_params_url": "https://example.com/cameras.json",
  "ply_url": "https://example.com/gaussians.ply",
  "depth_urls": ["https://example.com/depth_0.png"],
  "normal_urls": ["https://example.com/normal_0.png"],
  "rgb_video_url": "https://example.com/rendered_rgb.mp4",
  "depth_video_url": "https://example.com/rendered_depth.mp4"
}
```

## 6. 标准调用流程

1. 前端调用 `POST {task_manager}/api/task/recover`，请求体为 `{"project":"kokoni-world"}`
2. 从返回值中取出 `service_url`
3. 调用 `GET {service_url}/health` 等待 GPU 后端就绪
4. 生成唯一 `request_id`，并开始轮询 `GET {service_url}/queue_status?request_id=...`
5. 调用 `POST {service_url}/reconstruct` 发起重建
6. 使用返回的 `glb_url`、`ply_url`、`depth_urls`、`normal_urls` 等结果链接展示或下载
7. 前端调用 `POST {task_manager}/api/task/pause`，请求体为 `{"project":"kokoni-world"}`
