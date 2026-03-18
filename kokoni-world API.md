# kokoni-world API 接入文档

## 1. 概览

`kokoni-world` 提供图片/视频输入的 3D 重建能力。

调用流程如下：

1. 调用 task manager 的恢复接口，获取本次可用的运行时服务地址 `service_url`
2. 使用 `service_url` 调用运行时接口 `/health`、`/queue_status`、`/reconstruct`
3. 重建完成后，调用 task manager 的暂停接口释放任务

本文档主要说明：

- task manager 地址与接口
- 如何获取运行时服务地址
- 运行时 API 的请求参数与返回字段
- 调用示例
- 常见错误响应

## 2. 地址说明

### 2.1 task manager 地址

当前 task manager 地址如下：

```text
http://36.133.236.108:8090
```

相关接口：

- `POST http://36.133.236.108:8090/api/task/recover`
- `POST http://36.133.236.108:8090/api/task/pause`

### 2.2 运行时服务地址

运行时服务地址需要先调用 task manager 的 `POST /api/task/recover` 获取。

下文统一使用以下占位符表示 recover 接口返回的运行时地址：

```text
{service_url}
```

接口示例：

- `GET {service_url}/health`
- `GET {service_url}/queue_status`
- `POST {service_url}/reconstruct`

## 3. 推荐调用流程

1. 调用 `POST /api/task/recover`
2. 从响应中读取 `service_url`
3. 调用 `GET {service_url}/health` 检查运行时服务是否可用
4. 调用 `POST {service_url}/reconstruct` 发起重建
5. 如需展示排队状态，可调用 `GET {service_url}/queue_status`
6. 重建完成后，调用 `POST /api/task/pause`

## 4. task manager 接口

### 4.1 `POST /api/task/recover`

用于恢复任务并返回可访问的运行时服务地址。

请求类型：`application/json`

请求体：

| 字段 | 类型 | 必填 | 说明 |
|---|---|---|---|
| `project` | string | 是 | 固定传 `kokoni-world` |

请求示例：

```bash
curl -X POST "http://36.133.236.108:8090/api/task/recover" \
  -H "Content-Type: application/json" \
  -d '{"project":"kokoni-world"}'
```

成功响应字段：

| 字段 | 类型 | 说明 |
|---|---|---|
| `service_url` | string | 本次可用的运行时服务地址 |
| `status` | string | 当前任务状态 |
| `recovered` | boolean | 本次请求是否执行了恢复动作 |

成功响应示例：

```json
{
  "service_url": "http://203.0.113.10:10085",
  "status": "running",
  "recovered": true
}
```

### 4.2 `POST /api/task/pause`

用于在调用完成后暂停任务。

请求类型：`application/json`

请求体：

| 字段 | 类型 | 必填 | 说明 |
|---|---|---|---|
| `project` | string | 是 | 固定传 `kokoni-world` |

请求示例：

```bash
curl -X POST "http://36.133.236.108:8090/api/task/pause" \
  -H "Content-Type: application/json" \
  -d '{"project":"kokoni-world"}'
```

成功响应示例：

```json
{
  "status": "paused"
}
```

## 5. 运行时接口概览

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/health` | 健康检查 |
| GET | `/queue_status` | 查询任务排队状态，支持按 `request_id` 查询 |
| POST | `/reconstruct` | 上传文件并执行 3D 重建，完成后返回结果链接 |

## 6. 公共约定

### 6.1 内容类型

- `GET` 接口使用标准 URL Query 参数
- `POST /reconstruct` 使用 `multipart/form-data`

### 6.2 `request_id`

`request_id` 用于标识一次客户请求，建议由调用方生成全局唯一值，例如 UUID。
如果你希望在任务执行期间查询排队状态，建议在调用 `/reconstruct` 时显式传入 `request_id`，并使用相同的值请求 `/queue_status`。

### 6.3 返回结果链接

重建成功后，接口会返回结果文件的下载链接，例如：

- `glb_url`
- `ply_url`
- `depth_urls`
- `normal_urls`
- `rgb_video_url`
- `depth_video_url`

这些链接可用于下载结果或在前端直接展示。

## 7. 运行时接口详情

### 7.1 `GET /health`

用于检查运行时服务是否可用。

请求参数：无

请求示例：

```bash
curl "{service_url}/health"
```

成功响应示例：

```json
{
  "status": "ok"
}
```

### 7.2 `GET /queue_status`

用于查询当前请求是否正在执行、是否在排队，以及排队位置。

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
| `position` | integer | 当前请求在队列中的位置 |

`position` 规则：

- `0`：该请求正在处理
- `>=1`：该请求正在排队，`1` 表示排队中的第一位
- `-1`：未找到该 `request_id`

请求示例：

```bash
curl "{service_url}/queue_status?request_id=req-001"
```

成功响应示例：

```json
{
  "processing": true,
  "pending": 2,
  "status": "pending",
  "position": 1
}
```

### 7.3 `POST /reconstruct`

上传输入文件并执行重建。接口执行完成后会直接返回结果。

请求类型：`multipart/form-data`

Form 参数：

| 参数 | 类型 | 必填 | 默认值 | 说明 |
|---|---|---|---|---|
| `files` | file[] | 是 | 无 | 输入文件列表，支持图片、视频，以及 `.heic` / `.heif` |
| `time_interval` | float | 否 | `1.0` | 视频抽帧间隔，单位秒 |
| `frame_selector` | string | 否 | `All` | 帧筛选值，通常为 `All` 或 `"<index>: <filename>"` |
| `show_camera` | boolean | 否 | `true` | 是否在导出的 GLB 中显示相机 |
| `show_mesh` | boolean | 否 | `true` | 是否导出网格；为 `false` 时导出点云场景 |
| `filter_sky_bg` | boolean | 否 | `false` | 是否过滤天空背景 |
| `filter_ambiguous` | boolean | 否 | `true` | 是否过滤低置信度及深度/法线边缘区域 |
| `request_id` | string | 否 | 自动生成 | 客户端请求 ID，建议由调用方传入 |

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

请求示例：

```bash
curl -X POST "{service_url}/reconstruct" \
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

## 8. 错误响应

常见错误码：

- `400`：处理后没有有效图像
- `422`：参数校验失败
- `500`：服务处理异常

错误响应示例：

```json
{
  "detail": "No valid images after processing."
}
```

## 9. 接入建议

- 建议始终传入自定义 `request_id`，便于链路追踪和状态查询
- 视频输入建议合理设置 `time_interval`，避免抽帧过密导致处理时间增加
- 如果只需要点云效果，可将 `show_mesh` 设为 `false`
- 如果你的前端需要结果预览，可以直接使用返回的文件链接
- 单次调用完成后，建议及时调用 `POST /api/task/pause`
