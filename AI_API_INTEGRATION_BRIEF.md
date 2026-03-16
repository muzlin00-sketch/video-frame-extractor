# 视频分析能力对接说明（供 AI 编程与多前端平台集成）

## 1. 目标与现状

本项目核心能力已可被后端代码直接调用，适合被其它平台（Web 前端、低代码平台、工作流系统、机器人平台）统一封装。

当前能力状态：

- 已具备完整流程：场景切分 → 抽帧 → AI 分析 → 报告输出
- 已具备并发优化：准备并发 + API 并发
- 已具备可复用 Python 入口函数：`run_video_analysis(...)`
- 当前默认是本地脚本/Gradio 调用，尚未内置 HTTP 路由层（可由你们团队二次封装）

---

## 2. 关键文件与入口

- 核心分析文件：`scripts/analyze_video.py`
- 主要调用入口函数：

```python
run_video_analysis(video_path, output_base_dir, runtime, verify_connectivity=True)
```

建议外部系统直接 import 此函数进行服务化封装。

---

## 3. 输入参数规范

### 3.1 `video_path`

- 类型：`str`
- 含义：本地视频绝对路径

### 3.2 `output_base_dir`

- 类型：`str`
- 含义：输出目录（报告与中间帧目录）

### 3.3 `runtime`

- 类型：`dict`
- 建议字段：

```json
{
  "api_key": "string",
  "base_url": "string",
  "model_name": "string",
  "analysis_mode": "live_action | storyboard",
  "scene_threshold_live": 30.0,
  "scene_threshold_storyboard": 26.0,
  "min_scene_duration_storyboard": 0.0,
  "keep_short_scene_storyboard": true,
  "smart_profile": "auto | landscape | portrait | action",
  "motion_preference": "auto | stable | dynamic",
  "smart_window": 1.0
}
```

### 3.4 `verify_connectivity`

- 类型：`bool`
- 含义：是否做 API 连通性检查
- 生产建议：保留 `True`，并利用内置 TTL 缓存减少重复探测

---

## 4. 返回结构（标准输出）

`run_video_analysis(...)` 返回结构：

```json
{
  "summary": {
    "total_scenes": 4,
    "success_scenes": 4,
    "failed_scenes": 0,
    "analysis_mode": "live_action"
  },
  "scenes": [
    {
      "scene_index": 1,
      "time_range": "0.00s - 3.91s",
      "analysis": {}
    }
  ],
  "failed": [],
  "report_path": "xxx/final_report_YYYYMMDD_HHMMSS.json",
  "latest_report_path": "xxx/final_report_latest.json"
}
```

---

## 5. AI API 配置方式

优先级：命令行参数 > 环境变量 > 配置文件

### 5.1 环境变量

- `GEMINI_API_KEY`
- `GEMINI_BASE_URL`
- `GEMINI_MODEL`

### 5.2 配置文件

- `scripts/app_config.json`

---

## 6. 并发与性能参数

通过环境变量控制：

- `PREP_MAX_CONCURRENCY`：准备并发（抽帧阶段），默认 `2`
- `ANALYSIS_MAX_CONCURRENCY`：API 并发（分析阶段），默认 `3`
- `API_CHECK_TTL_SECONDS`：连通性检查缓存秒数，默认 `300`

建议起步参数（通用）：

- `PREP_MAX_CONCURRENCY=2~3`
- `ANALYSIS_MAX_CONCURRENCY=3~4`

---

## 7. 模式说明（业务文案）

内部模式值：

- `live_action`
- `storyboard`

业务文案映射：

- `live_action` = 成片分析模式
- `storyboard` = 线稿分析模式

---

## 8. 面向 HTTP API 的封装建议

建议你们在独立服务层（FastAPI/Flask）封装以下接口：

- `POST /analyze`：提交分析任务
- `GET /task/{task_id}`：查询任务状态
- `GET /task/{task_id}/result`：获取最终结果

推荐任务状态：

- `pending`
- `running`
- `succeeded`
- `failed`

执行模型：

- Web 请求只负责创建任务
- 后台 worker 调用 `run_video_analysis(...)`
- 结果统一落库或对象存储

---

## 9. 直接调用示例

```python
from analyze_video import run_video_analysis

runtime = {
    "api_key": "...",
    "base_url": "https://your-openai-compatible-endpoint/v1",
    "model_name": "your_model_name",
    "analysis_mode": "live_action",
    "scene_threshold_live": 30.0,
    "scene_threshold_storyboard": 26.0,
    "min_scene_duration_storyboard": 0.0,
    "keep_short_scene_storyboard": True,
    "smart_profile": "auto",
    "motion_preference": "auto",
    "smart_window": 1.0
}

result = run_video_analysis(
    video_path=r"D:\videos\demo.mp4",
    output_base_dir=r"D:\analysis_output",
    runtime=runtime,
    verify_connectivity=True
)

print(result["summary"])
print(result["latest_report_path"])
```

---

## 10. 集成注意事项

- 不要把真实 `api_key` 写入仓库
- `base_url` 需要是可用的 OpenAI 兼容接口地址（若根路径返回 404 不代表一定不可用，关键是兼容的 chat/completions 路径是否可达）
- 输出目录建议使用业务可追踪目录，避免只落在临时目录
- 建议在上层服务增加超时控制、任务幂等键、失败重试策略

