# HunyuanWorld-Mirror API

## 1. 地址配置

### 1.1 GPU 推理服务地址

GPU 推理接口基于 `service_url` 访问，`service_url` 由 task-manager 的 `recover` 接口返回。

- `GET {service_url}/health`
- `GET {service_url}/queue_status`
- `POST {service_url}/reconstruct`

后端默认监听端口：`10085`。

### 1.2 task-manager 地址（recover / pause）

`recover` 与 `pause` 请求目标为 task-manager。

- 默认地址：`http://36.133.236.108:8090`
- `POST http://36.133.236.108:8090/api/task/recover`
- `POST http://36.133.236.108:8090/api/task/pause`

可通过 URL 参数覆盖：
- `?task_manager=http://<your-ip>:<port>`
- `?project=<task-name>`

### 1.3 task-manager 项目标识（project）

本分支（`alpha`）前端默认会向 task-manager 发送：

- `project = hunyuanworld-mirror-cu128`

对应 recover / pause 请求体示例：

```json
{
  "project": "hunyuanworld-mirror-cu128"
}
```

如需切换任务名，可通过 URL 参数覆盖：

- `?project=<task-name>`

## 2. GPU 接口清单

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/health` | 健康检查 |
| GET | `/queue_status` | 查询队列状态（支持 `request_id`） |
| POST | `/reconstruct` | 上传文件并执行重建，完成后返回结果 URL |

## 3. 接口定义

### 3.1 `GET /health`

请求参数：无

响应示例：

```json
{
  "status": "ok"
}
```

### 3.2 `GET /queue_status`

Query 参数：

| 参数 | 类型 | 必填 | 说明 |
|---|---|---|---|
| `request_id` | string | 否 | 客户端请求 ID |

响应字段：

| 字段 | 类型 | 说明 |
|---|---|---|
| `processing` | boolean | 当前是否有任务在执行 |
| `pending` | integer | 当前排队任务数 |
| `status` | string | `processing` / `pending` / `idle` |
| `position` | integer | 排队位置标记 |

`position` 规则：
- `0`：请求正在处理
- `>=1`：请求在队列中（`1` 表示队首）
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

### 3.3 `POST /reconstruct`

请求类型：`multipart/form-data`

Form 参数：

| 参数 | 类型 | 必填 | 默认值 | 说明 |
|---|---|---|---|---|
| `files` | file[] | 是 | 无 | 文件列表，支持图片/视频（`image/*,video/*,.heic,.heif`） |
| `time_interval` | float | 否 | `1.0` | 视频抽帧间隔（秒） |
| `frame_selector` | string | 否 | `All` | 点云/网格帧选择 |
| `show_camera` | boolean | 否 | `true` | 是否显示相机 |
| `show_mesh` | boolean | 否 | `true` | 是否导出网格（否则点云） |
| `filter_sky_bg` | boolean | 否 | `false` | 是否过滤天空 |
| `filter_ambiguous` | boolean | 否 | `true` | 是否过滤模糊点 |
| `request_id` | string | 否 | 自动生成 | 客户端请求 ID |

成功响应字段：

| 字段 | 类型 | 说明 |
|---|---|---|
| `session_id` | string | 任务会话 ID |
| `frame_choices` | string[] | 可选帧列表，首项为 `All` |
| `num_views` | integer | 处理后的视图数量 |
| `glb_url` | string | 场景 GLB URL |
| `camera_params_url` | string \| null | 相机参数 URL |
| `ply_url` | string \| null | 高斯 PLY URL |
| `depth_urls` | string[] | 深度图 URL 列表 |
| `normal_urls` | string[] | 法线图 URL 列表 |
| `rgb_video_url` | string \| null | 渲染 RGB 视频 URL |
| `depth_video_url` | string \| null | 渲染深度视频 URL |

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
  -F "show_camera=false" \
  -F "show_mesh=false" \
  -F "filter_sky_bg=false" \
  -F "filter_ambiguous=true" \
  -F "request_id=req-001"
```

## 4. 调用流程

1. `POST {task_manager}/api/task/recover`（body 含 `project`）获取 `service_url`
2. `GET {service_url}/health` 等待后端就绪
3. `POST {service_url}/reconstruct` 发起重建
4. `GET {service_url}/queue_status?request_id=...` 查询排队状态
5. `POST {task_manager}/api/task/pause`（body 含 `project`）释放任务
