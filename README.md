# Video Shot Analyzer

智能视频分析工具：自动切分场景、抽取关键帧，并生成导演向的运镜/构图/动作分析报告。  
支持 **成片分析模式** 与 **线稿分析模式**，可用于本地 WebUI 使用，也可集成到自有后端/API 服务。

## 功能特性

- 场景切分：自动识别镜头边界并生成镜头区间
- 关键帧抽取：按镜头抽取参考帧与辅助可视化帧
- AI 结构化分析：输出运镜、构图、动作等字段
- 并发优化：准备并发 + API 并发，提升整体吞吐
- 报告输出：JSON 与文本报告双格式
- 模式支持：成片分析模式 / 线稿分析模式

## 快速体验（Windows 安装包）

下载地址：

- Releases 页面：https://github.com/muzlin00-sketch/video-shot-analyzer/releases
- 直接下载：https://github.com/muzlin00-sketch/video-shot-analyzer/releases/download/v1.0.0/video-analyzer-setup-win11.exe

1. 打开仓库 **Releases** 页面  
2. 下载最新安装包：`video-analyzer-setup-win11.exe`
3. 安装后启动，填写你自己的 API Key / Base URL / Model

## 本地运行（开发模式）

### 1) 环境准备

- Python 3.10+（建议 3.11/3.12）
- Windows 10/11（当前打包脚本针对 Win11）

### 2) 安装依赖

在 `scripts` 目录中按你现有项目方式安装依赖（建议使用当前虚拟环境）。

### 3) 启动 WebUI

```bash
py -3 scripts\app_gui.py
```

默认访问地址：

- `http://127.0.0.1:7860`

## API/后端集成

项目已提供可直接调用的 Python 入口：

```python
run_video_analysis(video_path, output_base_dir, runtime, verify_connectivity=True)
```

完整对接说明见：

- [AI_API_INTEGRATION_BRIEF.md](./AI_API_INTEGRATION_BRIEF.md)

## 配置说明

优先级：命令行参数 > 环境变量 > 配置文件

- `GEMINI_API_KEY`
- `GEMINI_BASE_URL`
- `GEMINI_MODEL`

并发参数（环境变量）：

- `PREP_MAX_CONCURRENCY`（默认 2）
- `ANALYSIS_MAX_CONCURRENCY`（默认 3）
- `API_CHECK_TTL_SECONDS`（默认 300）

## 目录结构

- `scripts/analyze_video.py`：核心分析流程
- `scripts/app_gui.py`：WebUI 入口
- `scripts/video_frame_extractor.py`：抽帧能力
- `AI_API_INTEGRATION_BRIEF.md`：API 集成说明

## 常见问题

- Q: 为什么根 URL 访问返回 404？  
  A: 一些 OpenAI 兼容网关根路径会返回 404，但只要 `chat/completions` 路由可用，仍可正常调用。

- Q: 如何避免把密钥提交到 GitHub？  
  A: 不要在仓库保存真实 `app_config.json`，使用环境变量注入，仓库仅保留示例配置。

## 开源说明

- 许可证：见 [LICENSE](./LICENSE)
- 欢迎提交 Issue / PR 改进分析准确度、性能与跨平台支持
