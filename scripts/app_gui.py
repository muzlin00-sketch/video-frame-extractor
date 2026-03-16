import gradio as gr
import os
import json
import time
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from analyze_video import (
    split_scenes,
    get_video_duration,
    extract_key_frames,
    analyze_with_gemini,
    get_default_config_path,
    DEFAULT_BASE_URL,
    DEFAULT_MODEL_NAME,
    DEFAULT_ANALYSIS_MODE,
    VALID_ANALYSIS_MODES,
    apply_scene_policy
)
from openai import OpenAI

UI_CSS = """
#mode-switch-wrap {
  border: 2px solid #4f7cff;
  border-radius: 12px;
  padding: 10px 12px 4px 12px;
  background: #eef3ff;
}
#mode-switch-wrap .gr-form {
  border: 0;
}
#settings-header {
  margin-top: 10px;
  margin-bottom: 6px;
  font-size: 16px;
  font-weight: 700;
  color: #1f3d8a;
  background: #eaf0ff;
  border: 1px solid #b8cbff;
  border-radius: 10px;
  padding: 10px 12px;
}
#settings-panel {
  border: 2px solid #90adff;
  border-radius: 12px;
  box-shadow: 0 4px 14px rgba(79, 124, 255, 0.18);
}
"""

# ---------------------------------------------------------
# 全局状态
# ---------------------------------------------------------
current_config = {
    "api_key": "",
    "base_url": DEFAULT_BASE_URL,
    "model": DEFAULT_MODEL_NAME,
    "analysis_mode": DEFAULT_ANALYSIS_MODE,
    "scene_threshold_live": 30.0,
    "scene_threshold_storyboard": 26.0,
    "min_scene_duration_storyboard": 0.0,
    "keep_short_scene_storyboard": True,
    "smart_profile": "auto",
    "motion_preference": "auto",
    "smart_window": 1.0
}
API_KEY_ENV_NAME = "GEMINI_API_KEY"
ANALYSIS_MAX_CONCURRENCY = max(1, int(os.environ.get("ANALYSIS_MAX_CONCURRENCY", "3")))
PREP_MAX_CONCURRENCY = max(1, int(os.environ.get("PREP_MAX_CONCURRENCY", "2")))
API_CHECK_TTL_SECONDS = max(0, int(os.environ.get("API_CHECK_TTL_SECONDS", "300")))
API_CHECK_CACHE = {}

ANALYSIS_MODE_ALIASES = {
    "live_action": "live_action",
    "storyboard": "storyboard",
    "成片分析模式": "live_action",
    "线稿分析模式": "storyboard",
    "实拍视频模式": "live_action",
    "导演分镜线稿模式": "storyboard",
    "导演分镜稿模式": "storyboard"
}

ANALYSIS_MODE_LABELS = {
    "live_action": "成片分析模式",
    "storyboard": "线稿分析模式"
}

ANALYSIS_MODE_CHOICES = [
    "成片分析模式",
    "线稿分析模式"
]

# ---------------------------------------------------------
# 核心逻辑封装
# ---------------------------------------------------------

def load_config_ui():
    """加载配置文件到 UI"""
    config_path = get_default_config_path()
    default_api_key = str(os.environ.get(API_KEY_ENV_NAME, "") or "").strip()
    if os.path.exists(config_path):
        try:
            with open(config_path, 'r', encoding='utf-8') as f:
                data = json.load(f)
                # 更新全局配置
                current_config.update(data)
                if not str(current_config.get("api_key", "") or "").strip() and default_api_key:
                    current_config["api_key"] = default_api_key
                mode_value = _normalize_analysis_mode(current_config.get("analysis_mode"))
                current_config["analysis_mode"] = mode_value
                return (
                    current_config["api_key"],
                    current_config["base_url"],
                    current_config["model"],
                    ANALYSIS_MODE_LABELS.get(mode_value, "成片分析模式"),
                    current_config["scene_threshold_live"],
                    current_config["scene_threshold_storyboard"],
                    current_config["min_scene_duration_storyboard"],
                    current_config["keep_short_scene_storyboard"],
                    current_config["smart_profile"],
                    current_config["motion_preference"],
                    current_config["smart_window"],
                    f"已加载配置：{config_path}（当前模式：{ANALYSIS_MODE_LABELS.get(mode_value, '成片分析模式')}）",
                    mode_value
                )
        except Exception as e:
            return default_api_key, DEFAULT_BASE_URL, DEFAULT_MODEL_NAME, ANALYSIS_MODE_LABELS.get(DEFAULT_ANALYSIS_MODE, "成片分析模式"), 30.0, 26.0, 0.0, True, "auto", "auto", 1.0, f"加载失败：{e}", DEFAULT_ANALYSIS_MODE
    else:
        return default_api_key, DEFAULT_BASE_URL, DEFAULT_MODEL_NAME, ANALYSIS_MODE_LABELS.get(DEFAULT_ANALYSIS_MODE, "成片分析模式"), 30.0, 26.0, 0.0, True, "auto", "auto", 1.0, "未找到配置文件，已使用默认值", DEFAULT_ANALYSIS_MODE

def save_config_ui(
    api_key,
    base_url,
    model,
    analysis_mode,
    scene_threshold_live,
    scene_threshold_storyboard,
    min_scene_duration_storyboard,
    keep_short_scene_storyboard,
    smart_profile,
    motion_preference,
    smart_window
):
    """保存配置到文件"""
    config_path = get_default_config_path()
    analysis_mode = _normalize_analysis_mode(analysis_mode)
    if analysis_mode not in VALID_ANALYSIS_MODES:
        return f"保存失败：分析模式无效，仅支持 {', '.join(VALID_ANALYSIS_MODES)}"
    if float(scene_threshold_live) <= 0 or float(scene_threshold_storyboard) <= 0:
        return "保存失败：切分阈值必须大于 0"
    if float(min_scene_duration_storyboard) < 0:
        return "保存失败：分镜最短镜头时长不能小于 0"
    new_config = {
        "api_key": api_key,
        "base_url": base_url,
        "model": model,
        "analysis_mode": analysis_mode,
        "scene_threshold_live": float(scene_threshold_live),
        "scene_threshold_storyboard": float(scene_threshold_storyboard),
        "min_scene_duration_storyboard": float(min_scene_duration_storyboard),
        "keep_short_scene_storyboard": bool(keep_short_scene_storyboard),
        "smart_profile": smart_profile,
        "motion_preference": motion_preference,
        "smart_window": float(smart_window)
    }
    try:
        config_dir = os.path.dirname(config_path)
        if config_dir:
            os.makedirs(config_dir, exist_ok=True)
        with open(config_path, 'w', encoding='utf-8') as f:
            json.dump(new_config, f, indent=2, ensure_ascii=False)
        current_config.update(new_config)
        return f"配置已保存：{config_path}（当前模式：{ANALYSIS_MODE_LABELS.get(analysis_mode, '成片分析模式')}）"
    except Exception as e:
        return f"保存失败：{e}"


def _normalize_analysis_mode(mode):
    if isinstance(mode, bool):
        return DEFAULT_ANALYSIS_MODE
    if isinstance(mode, int):
        if mode == 1:
            return "storyboard"
        if mode == 0:
            return "live_action"
        return DEFAULT_ANALYSIS_MODE
    if isinstance(mode, dict):
        mode = mode.get("value") or mode.get("label") or mode.get("name")
    elif isinstance(mode, (list, tuple)) and mode:
        mode = mode[-1]
    key = str(mode or "").strip()
    if key in ("1", "storyboard", "Storyboard"):
        return "storyboard"
    if key in ("0", "live_action", "LiveAction"):
        return "live_action"
    if key in ANALYSIS_MODE_ALIASES:
        return ANALYSIS_MODE_ALIASES[key]
    lowered = key.lower()
    if lowered in ANALYSIS_MODE_ALIASES:
        return ANALYSIS_MODE_ALIASES[lowered]
    if "分镜" in key or "线稿" in key:
        return "storyboard"
    if "实拍" in key:
        return "live_action"
    return DEFAULT_ANALYSIS_MODE


def on_mode_change(analysis_mode):
    normalized = _normalize_analysis_mode(analysis_mode)
    current_config["analysis_mode"] = normalized
    return f"当前模式：{ANALYSIS_MODE_LABELS.get(normalized, '成片分析模式')}", normalized


def _resolve_video_path(video_input):
    if isinstance(video_input, str):
        return video_input
    if isinstance(video_input, list) and video_input:
        first = video_input[0]
        if isinstance(first, str):
            return first
    if isinstance(video_input, dict):
        if isinstance(video_input.get("path"), str):
            return video_input["path"]
        if isinstance(video_input.get("video"), str):
            return video_input["video"]
        if isinstance(video_input.get("name"), str):
            return video_input["name"]
    return None

def _build_report_text(video_path, full_report, analysis_mode, scene_threshold):
    lines = []
    lines.append("视频分析报告")
    lines.append(f"视频文件：{os.path.basename(video_path)}")
    lines.append(f"镜头数量：{len(full_report)}")
    lines.append(f"分析模式：{'成片分析模式' if analysis_mode == 'live_action' else '线稿分析模式'}")
    lines.append(f"切分阈值：{scene_threshold}")
    lines.append("")
    for scene in full_report:
        analysis = scene.get("analysis", {})
        is_storyboard = analysis_mode == "storyboard"
        lines.append(f"【镜头 {scene.get('scene_index')}】{scene.get('time_range')}")
        
        if analysis.get("ai_generation_prompt"):
            lines.append(f"AI生成提示词：{analysis.get('ai_generation_prompt', '')}")
        
        lines.append(f"主体数量：{analysis.get('subject_count', '')}")
        if (not is_storyboard) and analysis.get("subjects"):
            lines.append(f"主体类型：{', '.join([str(x) for x in analysis.get('subjects', [])])}")
        lines.append(f"运镜：{analysis.get('camera_movement', '')}")
        lines.append(f"景别：{analysis.get('shot_size', '')}")
        lines.append(f"构图：{analysis.get('composition', '')}")
        lines.append(f"动作：{analysis.get('action_summary', analysis.get('action_notes', ''))}")
        if analysis.get("reusable_tags"):
            lines.append(f"通用标签：{', '.join([str(x) for x in analysis.get('reusable_tags', [])])}")
        if analysis.get("director_notes"):
            lines.append(f"执行建议：{analysis.get('director_notes', '')}")
        lines.append("")
    return "\n".join(lines).strip()


def clear_outputs():
    return "", None, None, ""


def run_analysis(
    video_path,
    api_key,
    base_url,
    model,
    analysis_mode,
    scene_threshold_live,
    scene_threshold_storyboard,
    min_scene_duration_storyboard,
    keep_short_scene_storyboard,
    smart_profile,
    motion_preference,
    smart_window,
    progress=gr.Progress()
):
    """执行视频分析流程"""
    video_path = _resolve_video_path(video_path)
    if not video_path:
        return "", None, None, "请先上传或选择视频文件"

    api_key = str(api_key or "").strip()
    if not api_key:
        api_key = str(
            current_config.get("api_key")
            or os.environ.get(API_KEY_ENV_NAME)
            or ""
        ).strip()
    base_url = str(base_url or "").strip()
    model = str(model or "").strip()
    if not api_key:
        return "", None, None, "错误：接口密钥不能为空"
    if not base_url:
        return "", None, None, "错误：接口地址不能为空"
    if not model:
        return "", None, None, "错误：模型名称不能为空"
    analysis_mode = _normalize_analysis_mode(analysis_mode)
    if analysis_mode not in VALID_ANALYSIS_MODES:
        return "", None, None, f"错误：分析模式无效，仅支持 {', '.join(VALID_ANALYSIS_MODES)}"
    if float(scene_threshold_live) <= 0 or float(scene_threshold_storyboard) <= 0:
        return "", None, None, "错误：切分阈值必须大于 0"
    if float(min_scene_duration_storyboard) < 0:
        return "", None, None, "错误：分镜最短镜头时长不能小于 0"

    try:
        client = OpenAI(
            api_key=api_key,
            base_url=base_url,
            timeout=120.0
        )
    except Exception as e:
        return "", None, None, f"错误：客户端初始化失败：{e}"

    output_base_dir = os.path.join(os.path.dirname(video_path), "analysis_output")
    os.makedirs(output_base_dir, exist_ok=True)
    
    log_buffer = []
    def log(msg):
        print(msg)
        log_buffer.append(msg)
    
    try:
        progress(0.1, desc="正在切分场景...")
        log(f"开始分析：{os.path.basename(video_path)}")
        log(f"分析模式：{ANALYSIS_MODE_LABELS.get(analysis_mode, analysis_mode)}")
        log(f"API配置: Model={model}, BaseURL={base_url}")
        
        # 0. 快速连通性检查（带TTL缓存）
        api_check_key = f"{base_url}|{model}|{api_key}"
        last_check_ts = API_CHECK_CACHE.get(api_check_key, 0.0)
        now_ts = time.time()
        if API_CHECK_TTL_SECONDS > 0 and (now_ts - last_check_ts) <= API_CHECK_TTL_SECONDS:
            log(f"⏭️ 跳过API连通性检查（{API_CHECK_TTL_SECONDS}s内已通过）")
        else:
            try:
                log("正在检查 API 连通性...")
                client.chat.completions.create(
                    model=model,
                    messages=[{"role": "user", "content": "Hello"}],
                    max_tokens=5,
                    timeout=10.0
                )
                API_CHECK_CACHE[api_check_key] = now_ts
                log("✅ API 连通性检查通过")
            except Exception as conn_err:
                log(f"❌ API 连接失败: {conn_err}")
                return "", None, None, "\n".join(log_buffer)

        scene_threshold = float(scene_threshold_live) if analysis_mode == "live_action" else float(scene_threshold_storyboard)
        scenes = split_scenes(video_path, threshold=scene_threshold, analysis_mode=analysis_mode)
        if analysis_mode == "storyboard":
            scenes = apply_scene_policy(
                scenes=scenes,
                min_duration=float(min_scene_duration_storyboard),
                keep_short_scene=bool(keep_short_scene_storyboard)
            )
        if not scenes:
            duration = get_video_duration(video_path)
            scenes = [{"index": 1, "start": 0.0, "end": duration}]
            log("未检测到场景，已按单镜头处理")
        else:
            log(f"检测到 {len(scenes)} 个场景")
        full_report = []
        failed_scenes = []
        total_scenes = max(1, len(scenes))
        prepared_scenes = []

        prep_workers = max(1, min(PREP_MAX_CONCURRENCY, total_scenes))
        log(f"并发准备已启用：最大并发={prep_workers}，任务数={total_scenes}")

        def _prepare_scene(scene):
            frames = extract_key_frames(
                video_path=video_path,
                scene=scene,
                output_base_dir=output_base_dir,
                smart_profile=smart_profile,
                motion_preference=motion_preference,
                smart_window=float(smart_window),
                analysis_mode=analysis_mode
            )
            return {"scene": scene, "frames": frames}

        prepared_count = 0
        with ThreadPoolExecutor(max_workers=prep_workers) as prep_executor:
            prep_future_map = {prep_executor.submit(_prepare_scene, scene): scene for scene in scenes}
            for prep_future in as_completed(prep_future_map):
                scene = prep_future_map[prep_future]
                prepared_count += 1
                progress(
                    0.2 + 0.25 * (prepared_count / total_scenes),
                    desc=f"准备镜头 {prepared_count}/{total_scenes}..."
                )
                log(f"\n准备镜头 {scene['index']}（{scene['start']:.2f}s - {scene['end']:.2f}s）")
                try:
                    item = prep_future.result()
                    prepared_scenes.append(item)
                    log("  抽帧完成，已加入并发分析队列")
                except Exception as scene_error:
                    failed_scenes.append({
                        "scene_index": scene["index"],
                        "time_range": f"{scene['start']:.2f}s - {scene['end']:.2f}s",
                        "error": f"抽帧失败：{scene_error}"
                    })
                    log(f"  抽帧失败：{scene_error}")

        prepared_scenes.sort(key=lambda x: int(x["scene"]["index"]))

        analyzable_total = len(prepared_scenes)
        if analyzable_total > 0:
            max_workers = max(1, min(ANALYSIS_MAX_CONCURRENCY, analyzable_total))
            log(f"并发API分析已启用：最大并发={max_workers}，任务数={analyzable_total}")
            local_pool = threading.local()

            def _analyze_scene(item):
                scene = item["scene"]
                local_client = getattr(local_pool, "client", None)
                if local_client is None:
                    local_client = OpenAI(
                        api_key=api_key,
                        base_url=base_url,
                        timeout=120.0
                    )
                    local_pool.client = local_client
                analysis = analyze_with_gemini(
                    frames=item["frames"],
                    client=local_client,
                    model_name=model,
                    analysis_mode=analysis_mode
                )
                if not analysis:
                    return {
                        "ok": False,
                        "scene_index": scene["index"],
                        "time_range": f"{scene['start']:.2f}s - {scene['end']:.2f}s",
                        "error": "分析失败或无响应"
                    }
                return {
                    "ok": True,
                    "scene_index": scene["index"],
                    "time_range": f"{scene['start']:.2f}s - {scene['end']:.2f}s",
                    "analysis": analysis,
                    "frames": item["frames"]
                }

            completed_count = 0
            with ThreadPoolExecutor(max_workers=max_workers) as executor:
                future_map = {}
                for item in prepared_scenes:
                    scene = item["scene"]
                    future = executor.submit(_analyze_scene, item)
                    future_map[future] = scene
                    log(f"已提交API任务：镜头 {scene['index']}")
                for future in as_completed(future_map):
                    scene = future_map[future]
                    completed_count += 1
                    progress(
                        0.45 + 0.47 * (completed_count / analyzable_total),
                        desc=f"并发分析完成 {completed_count}/{analyzable_total}..."
                    )
                    try:
                        result = future.result()
                        log(f"\n镜头 {scene['index']} API返回")
                        if result.get("ok"):
                            full_report.append({
                                "scene_index": result["scene_index"],
                                "time_range": result["time_range"],
                                "analysis": result["analysis"],
                                "frames": result["frames"]
                            })
                            analysis = result["analysis"]
                            log(f"  运镜：{analysis.get('camera_movement')}")
                            if analysis.get("system_motion_label"):
                                log(
                                    f"  系统运镜预检：{analysis.get('system_motion_label')} "
                                    f"(置信度{analysis.get('system_motion_confidence')}, "
                                    f"平移{analysis.get('system_motion_translation')}, "
                                    f"缩放{analysis.get('system_motion_zoom_delta')}, "
                                    f"跟踪点{analysis.get('system_motion_tracks')})"
                                )
                            log(f"  构图：{analysis.get('composition', '')}")
                            log(f"  动作：{analysis.get('action_summary', analysis.get('action_notes', ''))}")
                        else:
                            failed_scenes.append({
                                "scene_index": result["scene_index"],
                                "time_range": result["time_range"],
                                "error": result["error"]
                            })
                            log(f"  处理失败：{result['error']}")
                    except Exception as scene_error:
                        failed_scenes.append({
                            "scene_index": scene["index"],
                            "time_range": f"{scene['start']:.2f}s - {scene['end']:.2f}s",
                            "error": str(scene_error)
                        })
                        log(f"  处理失败：{scene_error}")

        full_report.sort(key=lambda x: int(x.get("scene_index", 0)))
        failed_scenes.sort(key=lambda x: int(x.get("scene_index", 0)))
        progress(0.92, desc="正在整理报告...")

        # 保存结果
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        report_path = os.path.join(output_base_dir, f"final_report_{timestamp}.json")
        with open(report_path, 'w', encoding='utf-8') as f:
            json.dump({
                "summary": {
                    "total_scenes": total_scenes,
                    "success_scenes": len(full_report),
                    "failed_scenes": len(failed_scenes),
                    "analysis_mode": analysis_mode
                },
                "scenes": full_report,
                "failed": failed_scenes
            }, f, indent=2, ensure_ascii=False)
        latest_json_path = os.path.join(output_base_dir, "final_report_latest.json")
        with open(latest_json_path, 'w', encoding='utf-8') as f:
            json.dump({
                "summary": {
                    "total_scenes": total_scenes,
                    "success_scenes": len(full_report),
                    "failed_scenes": len(failed_scenes),
                    "analysis_mode": analysis_mode
                },
                "scenes": full_report,
                "failed": failed_scenes
            }, f, indent=2, ensure_ascii=False)

        report_text = _build_report_text(
            video_path=video_path,
            full_report=full_report,
            analysis_mode=analysis_mode,
            scene_threshold=scene_threshold
        )
        if failed_scenes:
            report_text = (
                report_text
                + "\n\n未成功镜头：\n"
                + "\n".join(
                    [f"镜头{item['scene_index']} {item['time_range']}：{item['error']}" for item in failed_scenes]
                )
            )
        report_txt_path = os.path.join(output_base_dir, f"final_report_{timestamp}.txt")
        with open(report_txt_path, 'w', encoding='utf-8') as f:
            f.write(report_text)
        latest_txt_path = os.path.join(output_base_dir, "final_report_latest.txt")
        with open(latest_txt_path, 'w', encoding='utf-8') as f:
            f.write(report_text)

        log(f"\n分析完成，报告已保存：{report_path}")
        log(f"文本报告已保存：{report_txt_path}")
        log(f"最新JSON：{latest_json_path}")
        log(f"最新TXT：{latest_txt_path}")

        progress(1.0, desc="分析完成")
        return report_text, report_txt_path, report_path, "\n".join(log_buffer)

    except Exception as e:
        import traceback
        return "", None, None, f"运行时错误：{str(e)}\n{traceback.format_exc()}"

# ---------------------------------------------------------
# Gradio 界面构建
# ---------------------------------------------------------

with gr.Blocks(title="智能视频分析工具") as app:
    gr.Markdown("# 智能视频分析工具")
    gr.Markdown("第一步：上传视频文件。第二步：点击开始分析。第三步：直接查看报告并下载文件。")

    with gr.Row():
        with gr.Column(scale=1):
            with gr.Group(elem_id="mode-switch-wrap"):
                analysis_mode_dd = gr.Dropdown(
                    choices=ANALYSIS_MODE_CHOICES,
                    value=ANALYSIS_MODE_LABELS.get(DEFAULT_ANALYSIS_MODE, "成片分析模式"),
                    label="分析模式"
                )
                analysis_mode_state = gr.State(value=DEFAULT_ANALYSIS_MODE)
        with gr.Column(scale=3):
            gr.Markdown("")

    video_input = gr.File(
        label="选择视频文件（支持 mov/mp4/mkv/avi）",
        file_types=["video"],
        type="filepath"
    )
    with gr.Row():
        analyze_btn = gr.Button("开始分析", variant="primary", size="lg")
        clear_btn = gr.Button("清空结果")

    gr.Markdown("⚙️ 高级设置（点击展开）", elem_id="settings-header")
    with gr.Accordion("设置", open=False, elem_id="settings-panel"):
        api_key_input = gr.Textbox(label="接口密钥", type="password", placeholder="请输入接口密钥")
        base_url_input = gr.Textbox(label="接口地址", value=DEFAULT_BASE_URL)
        model_input = gr.Textbox(label="模型名称", value=DEFAULT_MODEL_NAME)
        scene_threshold_live_slider = gr.Slider(
            minimum=10.0, maximum=60.0, value=30.0, step=0.5,
            label="成片模式切分阈值"
        )
        scene_threshold_storyboard_slider = gr.Slider(
            minimum=10.0, maximum=60.0, value=26.0, step=0.5,
            label="线稿模式切分阈值"
        )
        min_scene_duration_storyboard_slider = gr.Slider(
            minimum=0.0, maximum=3.0, value=0.0, step=0.1,
            label="线稿模式最短镜头时长（秒）"
        )
        keep_short_scene_storyboard_checkbox = gr.Checkbox(
            value=True,
            label="线稿模式保留超短镜头"
        )
        smart_profile_dd = gr.Dropdown(
            choices=[("自动", "auto"), ("风景", "landscape"), ("人像", "portrait"), ("动作", "action")],
            value="auto",
            label="场景偏好"
        )
        motion_pref_dd = gr.Dropdown(
            choices=[("自动", "auto"), ("稳定", "stable"), ("动态", "dynamic")],
            value="auto",
            label="运动偏好"
        )
        smart_window_slider = gr.Slider(
            minimum=0.1, maximum=5.0, value=1.0, step=0.1,
            label="搜索窗口（秒）"
        )
        with gr.Row():
            load_btn = gr.Button("读取设置")
            save_btn = gr.Button("保存设置")
        config_status = gr.Textbox(label="设置状态", interactive=False)

    report_text_output = gr.Textbox(label="分析报告", lines=20, max_lines=40)
    with gr.Row():
        txt_file_output = gr.File(label="下载文本报告（TXT）")
        json_file_output = gr.File(label="下载结构化报告（JSON）")
    log_output = gr.Textbox(label="运行日志", lines=10, max_lines=24)

    # 事件绑定
    analysis_mode_dd.change(
        on_mode_change,
        inputs=[analysis_mode_dd],
        outputs=[config_status, analysis_mode_state]
    )

    load_btn.click(
        load_config_ui,
        outputs=[
            api_key_input, base_url_input, model_input, analysis_mode_dd,
            scene_threshold_live_slider, scene_threshold_storyboard_slider,
            min_scene_duration_storyboard_slider, keep_short_scene_storyboard_checkbox,
            smart_profile_dd, motion_pref_dd, smart_window_slider, config_status,
            analysis_mode_state
        ]
    )
    
    save_btn.click(
        save_config_ui,
        inputs=[
            api_key_input, base_url_input, model_input, analysis_mode_state,
            scene_threshold_live_slider, scene_threshold_storyboard_slider,
            min_scene_duration_storyboard_slider, keep_short_scene_storyboard_checkbox,
            smart_profile_dd, motion_pref_dd, smart_window_slider
        ],
        outputs=[config_status]
    )
    
    analyze_btn.click(
        run_analysis,
        inputs=[
            video_input, api_key_input, base_url_input, model_input, analysis_mode_state,
            scene_threshold_live_slider, scene_threshold_storyboard_slider,
            min_scene_duration_storyboard_slider, keep_short_scene_storyboard_checkbox,
            smart_profile_dd, motion_pref_dd, smart_window_slider
        ],
        outputs=[report_text_output, txt_file_output, json_file_output, log_output]
    )
    clear_btn.click(clear_outputs, outputs=[report_text_output, txt_file_output, json_file_output, log_output])

    # 初始化加载配置
    app.load(
        load_config_ui,
        outputs=[
            api_key_input, base_url_input, model_input, analysis_mode_dd,
            scene_threshold_live_slider, scene_threshold_storyboard_slider,
            min_scene_duration_storyboard_slider, keep_short_scene_storyboard_checkbox,
            smart_profile_dd, motion_pref_dd, smart_window_slider, config_status,
            analysis_mode_state
        ]
    )

def launch_app():
    for port in [7860, 7861, 7862, 7863]:
        try:
            app.queue().launch(
                inbrowser=True,
                css=UI_CSS,
                theme=gr.themes.Soft(),
                server_name="127.0.0.1",
                server_port=port,
                show_error=True
            )
            return
        except OSError:
            continue
    raise RuntimeError("7860-7863 端口均被占用，请关闭占用进程后重试。")


if __name__ == "__main__":
    launch_app()
