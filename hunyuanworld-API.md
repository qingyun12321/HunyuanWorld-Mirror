# kokoni-3d-world API

## 1. 接入概览

`kokoni-3d-world` 采用异步任务模式：

1. 提交重建任务
2. 获取 `task_id`
3. 轮询任务状态
4. 任务完成后读取结果文件 URL

## 2. 服务地址

当前服务地址（下文中的 `base_url`）：

```text
http://36.133.236.108:8090
```

## 3. 鉴权

API 使用 **Bearer Token** 机制进行访问控制。客户端需要在 Header 中传递 `Authorization` 字段。

| Header Field | Value Format | 说明 |
|---|---|---|
| `Authorization` | `Bearer <YOUR_API_KEY>` | 请将 `<YOUR_API_KEY>` 替换为实际分配的密钥 |

## 4. 接口清单

| 方法 | 路径 | 说明 |
|---|---|---|
| `POST` | `/api/v1/services/aigc/3d-generation/reconstruction` | 创建重建任务 |
| `GET` | `/api/v1/tasks/{task_id}` | 查询任务状态与结果 |

## 5. 创建重建任务

### 4.1 请求地址

```text
POST http://36.133.236.108:8090/api/v1/services/aigc/3d-generation/reconstruction
```

### 4.2 请求类型

`multipart/form-data`

### 4.3 请求参数

表单中包含两个部分：

| 参数名 | 类型 | 必填 | 说明 |
|---|---|---|---|
| `request` | string | 是 | JSON 字符串，外层结构固定为 `model / input / parameters` |
| `files` | file[] | 是 | 待重建的图片或视频文件，可多文件上传 |

### 4.4 `request` 字段说明

```json
{
  "model": "kokoni-3d-world",
  "input": {
    "request_id": "optional-client-id",
    "frame_selector": "All"
  },
  "parameters": {
    "time_interval": 1.0,
    "show_camera": true,
    "show_mesh": true,
    "filter_sky_bg": false,
    "filter_ambiguous": true
  }
}
```

#### 顶层字段

| 字段 | 类型 | 必填 | 说明 |
|---|---|---|---|
| `model` | string | 是 | 模型名称，当前使用 `kokoni-3d-world` |
| `input` | object | 是 | 输入参数 |
| `parameters` | object | 是 | 控制参数 |

#### `input` 字段

| 字段 | 类型 | 必填 | 默认值 | 说明 |
|---|---|---|---|---|
| `request_id` | string | 否 | 自动生成 | 客户端请求 ID，建议用于链路追踪 |
| `frame_selector` | string | 否 | `All` | 输出阶段使用的帧筛选方式 |

#### `parameters` 字段

| 字段 | 类型 | 必填 | 默认值 | 说明 |
|---|---|---|---|---|
| `time_interval` | number | 否 | `1.0` | 视频抽帧间隔，单位秒 |
| `show_camera` | boolean | 否 | `true` | 是否在导出结果中显示相机 |
| `show_mesh` | boolean | 否 | `true` | 是否导出网格；设为 `false` 时更偏向点云表现 |
| `filter_sky_bg` | boolean | 否 | `false` | 是否过滤天空背景 |
| `filter_ambiguous` | boolean | 否 | `true` | 是否过滤低置信度区域 |

### 4.5 请求示例

```bash
curl --location 'http://36.133.236.108:8090/api/v1/services/aigc/3d-generation/reconstruction' \
  -H 'Authorization: Bearer <YOUR_API_KEY>' \
  -F 'request={
    "model":"kokoni-3d-world",
    "input":{
      "request_id":"req-001",
      "frame_selector":"All"
    },
    "parameters":{
      "time_interval":1.0,
      "show_camera":true,
      "show_mesh":true,
      "filter_sky_bg":false,
      "filter_ambiguous":true
    }
  }' \
  -F 'files=@/path/to/image1.jpg' \
  -F 'files=@/path/to/video1.mp4'
```

### 4.6 成功响应示例

```json
{
  "status_code": 200,
  "request_id": "req-001",
  "code": null,
  "message": "",
  "output": {
    "task_id": "7d7d5167b6d4498ebad79dc58e11e4f7",
    "task_status": "PENDING",
    "submit_time": "2026-03-11 12:34:56.789"
  }
}
```

## 6. 查询任务状态

### 5.1 请求地址

```text
GET http://36.133.236.108:8090/api/v1/tasks/{task_id}
```

### 5.2 路径参数

| 参数 | 类型 | 必填 | 说明 |
|---|---|---|---|
| `task_id` | string | 是 | 创建任务接口返回的任务 ID |

### 5.3 任务状态说明

| 状态 | 说明 |
|---|---|
| `PENDING` | 任务已创建，等待可用算力 |
| `SCALING` | 任务正在准备执行，请继续轮询 |
| `RUNNING` | 任务正在执行 |
| `SUCCEEDED` | 任务完成，可读取结果 |
| `FAILED` | 任务失败，请查看 `message` |

### 5.4 查询响应字段

响应根级字段固定如下：

| 字段 | 类型 | 说明 |
|---|---|---|
| `status_code` | integer | 接口状态码 |
| `request_id` | string | 请求 ID |
| `code` | string \| null | 业务码 |
| `message` | string | 状态说明或错误信息 |
| `output` | object | 任务信息与结果 |

`output` 中固定包含：

| 字段 | 类型 | 说明 |
|---|---|---|
| `task_id` | string | 任务 ID |
| `task_status` | string | 任务状态 |
| `submit_time` | string | 提交时间 |
| `scheduled_time` | string \| null | 调度时间 |
| `start_time` | string \| null | 开始执行时间 |
| `end_time` | string \| null | 结束时间 |

任务成功后，`output` 中还会包含以下结果字段：

| 字段 | 类型 | 说明 |
|---|---|---|
| `session_id` | string | 会话 ID |
| `frame_choices` | string[] | 可选帧列表 |
| `num_views` | integer | 处理后的视图数量 |
| `glb_url` | string | GLB 场景文件下载地址 |
| `ply_url` | string \| null | Gaussian PLY 文件下载地址 |
| `camera_params_url` | string \| null | 相机参数文件地址 |
| `depth_urls` | string[] | 深度图列表 |
| `normal_urls` | string[] | 法线图列表 |
| `rgb_video_url` | string \| null | 渲染 RGB 视频地址 |
| `depth_video_url` | string \| null | 渲染深度视频地址 |

### 5.5 查询示例

```bash
curl --location 'http://36.133.236.108:8090/api/v1/tasks/7d7d5167b6d4498ebad79dc58e11e4f7' \
  -H 'Authorization: Bearer <YOUR_API_KEY>'
```

### 5.6 成功完成响应示例

```json
{
  "status_code": 200,
  "request_id": "req-001",
  "code": null,
  "message": "",
  "output": {
    "task_id": "7d7d5167b6d4498ebad79dc58e11e4f7",
    "task_status": "SUCCEEDED",
    "submit_time": "2026-03-11 12:34:56.789",
    "scheduled_time": "2026-03-11 12:35:00.100",
    "start_time": "2026-03-11 12:35:01.230",
    "end_time": "2026-03-11 12:37:18.456",
    "session_id": "20260311_123501_ab12cd34",
    "frame_choices": ["All", "0: image_0001.jpg"],
    "num_views": 1,
    "glb_url": "https://example.com/scene.glb",
    "ply_url": "https://example.com/gaussians.ply",
    "camera_params_url": "https://example.com/camera.json",
    "depth_urls": ["https://example.com/depth_0.png"],
    "normal_urls": ["https://example.com/normal_0.png"],
    "rgb_video_url": "https://example.com/rendered_rgb.mp4",
    "depth_video_url": "https://example.com/rendered_depth.mp4"
  }
}
```

## 7. 推荐调用方式

建议客户端按以下方式接入：

1. 调用创建任务接口，获取 `task_id`
2. 每 2 秒轮询一次任务状态接口
3. 当 `task_status=SUCCEEDED` 时读取结果文件 URL
4. 当 `task_status=FAILED` 时提示失败原因并决定是否重试

## 8. 错误处理建议

常见场景如下：

| 场景 | 建议处理方式 |
|---|---|
| 请求参数不合法 | 根据接口返回信息修正请求后重试 |
| 文件为空或格式不支持 | 检查上传内容 |
| 任务长时间处于 `SCALING` | 说明平台正在准备算力，可继续轮询 |
| 任务返回 `FAILED` | 展示 `message`，并根据业务决定是否重新提交 |
| 查询接口返回 `404` | 检查 `task_id` 是否正确 |

## 9. 结果文件说明

结果中的文件 URL 为可直接访问的下载地址，通常适合以下用途：

- `glb_url`：网页 3D 预览或模型下载
- `ply_url`：点云/高斯泼溅资产下载
- `depth_urls` / `normal_urls`：可视化结果展示
- `rgb_video_url` / `depth_video_url`：视频回放与结果演示

建议在业务侧自行管理下载、缓存和过期策略。
