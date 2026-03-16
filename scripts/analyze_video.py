import os
import sys
import json
import re
import base64
import argparse
import traceback
import time
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from scenedetect import detect, ContentDetector, AdaptiveDetector
from openai import OpenAI
import cv2
import numpy as np
from video_frame_extractor import extract_frames

DEFAULT_BASE_URL = ""
DEFAULT_MODEL_NAME = ""
DEFAULT_ANALYSIS_MODE = "live_action"
VALID_ANALYSIS_MODES = ("live_action", "storyboard")
MAX_ANALYSIS_IMAGES = 8
STORYBOARD_GRID_ALPHA = 0.34
MOTION_MIN_TRACKS = 8
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


def _to_bool(value):
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        lowered = value.strip().lower()
        if lowered in ("true", "1", "yes", "y", "on"):
            return True
        if lowered in ("false", "0", "no", "n", "off"):
            return False
        raise ValueError("布尔配置仅支持 true/false/1/0/yes/no/on/off")
    if isinstance(value, (int, float)):
        return bool(value)
    raise ValueError("布尔配置值类型无效")


def _normalize_analysis_mode(mode):
    if isinstance(mode, bool):
        return DEFAULT_ANALYSIS_MODE
    if isinstance(mode, int):
        if mode == 1:
            return "storyboard"
        if mode == 0:
            return "live_action"
        return DEFAULT_ANALYSIS_MODE
    key = str(mode or "").strip()
    if key in ("1", "storyboard", "Storyboard"):
        return "storyboard"
    if key in ("0", "live_action", "LiveAction"):
        return "live_action"
    return ANALYSIS_MODE_ALIASES.get(key, DEFAULT_ANALYSIS_MODE)

def _scene_list_to_boundaries(scene_list):
    boundaries = []
    for scene in scene_list or []:
        end_time = float(scene[1].get_seconds())
        boundaries.append(end_time)
    return boundaries


def _merge_boundaries(boundary_groups, duration, min_gap_sec=0.45):
    merged = []
    for group in boundary_groups:
        for t in group:
            value = float(t)
            if value <= min_gap_sec or value >= max(duration - min_gap_sec, min_gap_sec):
                continue
            merged.append(value)
    merged.sort()
    result = []
    for t in merged:
        if not result or abs(t - result[-1]) >= min_gap_sec:
            result.append(t)
        else:
            result[-1] = (result[-1] + t) * 0.5
    return result


def _boundaries_to_scenes(boundaries, duration, min_scene_sec=0.45):
    points = [0.0]
    for t in boundaries:
        value = float(t)
        if value > points[-1] + min_scene_sec and value < duration - min_scene_sec:
            points.append(value)
    if duration > points[-1]:
        points.append(duration)
    scenes = []
    for i in range(len(points) - 1):
        start_time = points[i]
        end_time = points[i + 1]
        if end_time - start_time >= min_scene_sec:
            scenes.append({
                "index": len(scenes) + 1,
                "start": start_time,
                "end": end_time
            })
    return scenes


def split_scenes(video_path, threshold=20.0, analysis_mode="live_action"):
    print(f"🎬 正在检测场景: {video_path} (阈值: {threshold}) ...")
    duration = get_video_duration(video_path)
    scene_lists = []
    if analysis_mode == "live_action":
        try:
            adaptive_list = detect(
                video_path,
                AdaptiveDetector(adaptive_threshold=2.8, min_scene_len=12)
            )
            scene_lists.append(adaptive_list)
            print(f"  ➜ AdaptiveDetector 命中: {len(adaptive_list)}")
        except Exception as e:
            print(f"  ⚠️ AdaptiveDetector 失败: {e}")
        try:
            content_list = detect(
                video_path,
                ContentDetector(threshold=float(threshold), min_scene_len=12)
            )
            scene_lists.append(content_list)
            print(f"  ➜ ContentDetector 命中: {len(content_list)}")
        except Exception as e:
            print(f"  ⚠️ ContentDetector 失败: {e}")
        sensitive_threshold = max(8.0, float(threshold) * 0.72)
        try:
            sensitive_list = detect(
                video_path,
                ContentDetector(threshold=sensitive_threshold, min_scene_len=10)
            )
            scene_lists.append(sensitive_list)
            print(f"  ➜ SensitiveDetector({sensitive_threshold:.1f}) 命中: {len(sensitive_list)}")
        except Exception as e:
            print(f"  ⚠️ SensitiveDetector 失败: {e}")
    else:
        try:
            scene_lists.append(
                detect(video_path, ContentDetector(threshold=float(threshold), min_scene_len=12))
            )
        except Exception as e:
            print(f"  ⚠️ ContentDetector 失败: {e}")
    if not scene_lists:
        fallback_list = detect(video_path, ContentDetector(threshold=float(threshold)))
        scene_lists.append(fallback_list)
        print(f"  ➜ 回退检测器命中: {len(fallback_list)}")
    boundary_groups = [_scene_list_to_boundaries(scene_list) for scene_list in scene_lists]
    merged_boundaries = _merge_boundaries(boundary_groups, duration=duration, min_gap_sec=0.45)
    scenes = _boundaries_to_scenes(merged_boundaries, duration=duration, min_scene_sec=0.45)
    if not scenes:
        scenes = [{"index": 1, "start": 0.0, "end": duration}]
    print(f"✅ 检测到 {len(scenes)} 个场景。")
    return scenes

def get_video_duration(video_path):
    """获取视频总时长（秒）"""
    cap = cv2.VideoCapture(video_path)
    if not cap.isOpened():
        raise ValueError(f"无法打开视频文件: {video_path}")
    fps = cap.get(cv2.CAP_PROP_FPS)
    total_frames = cap.get(cv2.CAP_PROP_FRAME_COUNT)
    cap.release()
    if fps <= 0:
        raise ValueError("视频帧率无效（fps <= 0）")
    return float(total_frames / fps)

def get_default_config_path():
    if getattr(sys, 'frozen', False):
        return os.path.join(os.path.dirname(sys.executable), "app_config.json")
    return os.path.join(os.path.dirname(__file__), "app_config.json")


def write_default_config(config_path):
    config_data = {
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
    config_dir = os.path.dirname(config_path)
    if config_dir:
        os.makedirs(config_dir, exist_ok=True)
    with open(config_path, 'w', encoding='utf-8') as f:
        json.dump(config_data, f, indent=2, ensure_ascii=False)


def load_config(config_path):
    if not os.path.exists(config_path):
        return {}
    with open(config_path, 'r', encoding='utf-8') as f:
        return json.load(f)


def resolve_runtime_config(args):
    config_data = load_config(args.config)
    api_key = args.api_key or os.environ.get("GEMINI_API_KEY") or config_data.get("api_key")
    base_url = args.base_url or os.environ.get("GEMINI_BASE_URL") or config_data.get("base_url") or DEFAULT_BASE_URL
    model_name = args.model or os.environ.get("GEMINI_MODEL") or config_data.get("model") or DEFAULT_MODEL_NAME
    analysis_mode = _normalize_analysis_mode(args.analysis_mode or config_data.get("analysis_mode") or DEFAULT_ANALYSIS_MODE)
    scene_threshold_live = args.scene_threshold_live if args.scene_threshold_live is not None else config_data.get("scene_threshold_live", 30.0)
    scene_threshold_storyboard = args.scene_threshold_storyboard if args.scene_threshold_storyboard is not None else config_data.get("scene_threshold_storyboard", 26.0)
    min_scene_duration_storyboard = args.min_scene_duration_storyboard if args.min_scene_duration_storyboard is not None else config_data.get("min_scene_duration_storyboard", 0.0)
    keep_short_scene_storyboard_raw = args.keep_short_scene_storyboard if args.keep_short_scene_storyboard is not None else config_data.get("keep_short_scene_storyboard", True)
    smart_profile = args.smart_profile or config_data.get("smart_profile") or "auto"
    motion_preference = args.motion_preference or config_data.get("motion_preference") or "auto"
    smart_window = args.smart_window if args.smart_window is not None else config_data.get("smart_window", 1.0)
    if not api_key:
        raise ValueError(
            "缺少 API Key。请通过 --api_key 传入，或设置 GEMINI_API_KEY，或在配置文件填写 api_key。"
        )
    if not base_url:
        raise ValueError(
            "缺少 Base URL。请通过 --base_url 传入，或设置 GEMINI_BASE_URL，或在配置文件填写 base_url。"
        )
    if not model_name:
        raise ValueError(
            "缺少模型名称。请通过 --model 传入，或设置 GEMINI_MODEL，或在配置文件填写 model。"
        )
    if str(analysis_mode) not in VALID_ANALYSIS_MODES:
        raise ValueError(f"analysis_mode 仅支持: {', '.join(VALID_ANALYSIS_MODES)}")
    if float(scene_threshold_live) <= 0 or float(scene_threshold_storyboard) <= 0:
        raise ValueError("场景切分阈值必须大于 0")
    if float(min_scene_duration_storyboard) < 0:
        raise ValueError("分镜模式最短镜头时长不能小于 0")
    keep_short_scene_storyboard = _to_bool(keep_short_scene_storyboard_raw)
    return {
        "api_key": api_key,
        "base_url": base_url,
        "model_name": model_name,
        "analysis_mode": analysis_mode,
        "scene_threshold_live": float(scene_threshold_live),
        "scene_threshold_storyboard": float(scene_threshold_storyboard),
        "min_scene_duration_storyboard": float(min_scene_duration_storyboard),
        "keep_short_scene_storyboard": keep_short_scene_storyboard,
        "smart_profile": str(smart_profile),
        "motion_preference": str(motion_preference),
        "smart_window": float(smart_window)
    }


def apply_scene_policy(scenes, min_duration: float, keep_short_scene: bool):
    if keep_short_scene or min_duration <= 0:
        return scenes
    filtered = [s for s in scenes if (float(s["end"]) - float(s["start"])) >= min_duration]
    for i, item in enumerate(filtered):
        item["index"] = i + 1
    return filtered


def extract_key_frames(video_path, scene, output_base_dir, smart_profile, motion_preference, smart_window, analysis_mode):
    scene_dir = os.path.join(output_base_dir, f"scene_{scene['index']:03d}")
    os.makedirs(scene_dir, exist_ok=True)
    frame_mode = 'smart_key' if analysis_mode == 'live_action' else 'key'
    frame_result = extract_frames(
        video_path=video_path,
        output_dir=scene_dir,
        start_time=float(scene['start']),
        end_time=float(scene['end']),
        mode=frame_mode,
        smart_profile=str(smart_profile),
        motion_preference=str(motion_preference),
        smart_window=float(smart_window)
    )
    if not frame_result.get("success"):
        raise RuntimeError(
            f"抽帧失败(scene={scene['index']}): {'; '.join(frame_result.get('errors', []))}"
        )
    frames = {
        "start": os.path.join(scene_dir, "frame_start.jpg"),
        "end": os.path.join(scene_dir, "frame_end.jpg")
    }
    mid_path = os.path.join(scene_dir, "frame_mid.jpg")
    if os.path.exists(mid_path):
        frames["mid"] = mid_path
    frames["reference"] = _extract_reference_frames(
        video_path=video_path,
        scene_dir=scene_dir,
        start_time=float(scene["start"]),
        end_time=float(scene["end"]),
        max_images=MAX_ANALYSIS_IMAGES
    )
    return frames


def _frame_quality_score(frame):
    gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
    return float(cv2.Laplacian(gray, cv2.CV_64F).var())


def _frame_descriptor(frame):
    hsv = cv2.cvtColor(frame, cv2.COLOR_BGR2HSV)
    hist = cv2.calcHist([hsv], [0, 1], None, [16, 12], [0, 180, 0, 256])
    hist = cv2.normalize(hist, hist).flatten()
    return hist.astype(np.float32)


def _pick_diverse_frames(candidates, max_images):
    if not candidates:
        return []
    candidates = sorted(candidates, key=lambda x: x["quality"], reverse=True)
    selected = [candidates[0]]
    remaining = candidates[1:]
    while remaining and len(selected) < max_images:
        best_idx = 0
        best_score = -1.0
        for i, item in enumerate(remaining):
            distances = [
                float(np.linalg.norm(item["desc"] - chosen["desc"]))
                for chosen in selected
            ]
            diversity = min(distances) if distances else 0.0
            score = diversity * 0.85 + item["quality_norm"] * 0.15
            if score > best_score:
                best_score = score
                best_idx = i
        selected.append(remaining.pop(best_idx))
    selected.sort(key=lambda x: x["frame_idx"])
    return selected


def _extract_reference_frames(video_path, scene_dir, start_time, end_time, max_images=12):
    reference_dir = os.path.join(scene_dir, "reference")
    os.makedirs(reference_dir, exist_ok=True)
    cap = cv2.VideoCapture(video_path)
    if not cap.isOpened():
        return []
    saved = []
    try:
        fps = cap.get(cv2.CAP_PROP_FPS)
        if fps <= 0:
            return []
        start_frame = int(max(0.0, start_time) * fps)
        end_frame = int(max(start_time, end_time) * fps)
        if end_frame <= start_frame:
            end_frame = start_frame + 1
        total = end_frame - start_frame
        candidate_count = max(max_images * 4, 14)
        candidate_count = min(candidate_count, total + 1)
        step = max(1, total // max(1, candidate_count - 1))
        sample_frames = []
        for idx in range(candidate_count):
            frame_idx = min(end_frame, start_frame + idx * step)
            if not sample_frames or frame_idx != sample_frames[-1]:
                sample_frames.append(frame_idx)
        candidates = []
        for frame_idx in sample_frames:
            cap.set(cv2.CAP_PROP_POS_FRAMES, frame_idx)
            ret, frame = cap.read()
            if not ret:
                continue
            quality = _frame_quality_score(frame)
            candidates.append({
                "frame_idx": frame_idx,
                "frame": frame,
                "quality": quality,
                "desc": _frame_descriptor(frame)
            })
        if not candidates:
            return []
        max_quality = max(item["quality"] for item in candidates)
        min_quality = min(item["quality"] for item in candidates)
        quality_span = max(max_quality - min_quality, 1e-6)
        for item in candidates:
            item["quality_norm"] = (item["quality"] - min_quality) / quality_span
        selected = _pick_diverse_frames(candidates, max_images=max_images)
        for idx, item in enumerate(selected):
            frame_path = os.path.join(reference_dir, f"ref_{idx + 1:02d}.jpg")
            cv2.imwrite(frame_path, item["frame"], [int(cv2.IMWRITE_JPEG_QUALITY), 90])
            saved.append(frame_path)
    finally:
        cap.release()
    return saved

def encode_image(image_path, max_size=1024, jpeg_quality=85):
    """将图片调整大小并编码为 base64"""
    if not os.path.exists(image_path):
        return None
        
    try:
        # 读取图片
        img = cv2.imread(image_path)
        if img is None:
            return None
            
        # 计算缩放比例
        h, w = img.shape[:2]
        if max(h, w) > max_size:
            scale = max_size / float(max(h, w))
            new_w = int(w * scale)
            new_h = int(h * scale)
            img = cv2.resize(img, (new_w, new_h), interpolation=cv2.INTER_AREA)
            
        quality = int(max(40, min(95, int(jpeg_quality))))
        _, buffer = cv2.imencode('.jpg', img, [int(cv2.IMWRITE_JPEG_QUALITY), quality])
        return base64.b64encode(buffer).decode('utf-8')
    except Exception as e:
        print(f"❌ 图片编码失败 {image_path}: {e}")
        return None

def clean_response(content):
    """清理响应内容，移除 <think> 标签和其他 Markdown 标记"""
    # 移除 <think>...</think> 标签及其内容
    content = re.sub(r'<think>.*?</think>', '', content, flags=re.DOTALL)
    # 移除 Markdown 代码块标记
    content = content.replace('```json', '').replace('```', '')
    content = content.strip()
    start_idx = content.find('{')
    end_idx = content.rfind('}')
    if start_idx != -1 and end_idx != -1 and end_idx > start_idx:
        return content[start_idx:end_idx + 1]
    return content


def _resolve_payload_plan(analysis_mode, frame_change_score, motion_confidence):
    if analysis_mode == "storyboard":
        if frame_change_score < 8.0 and motion_confidence < 0.45:
            return {"max_images": 4, "max_size": 640, "jpeg_quality": 72, "max_tokens": 1500}
        if frame_change_score < 18.0 and motion_confidence < 0.65:
            return {"max_images": 6, "max_size": 768, "jpeg_quality": 78, "max_tokens": 1700}
        return {"max_images": MAX_ANALYSIS_IMAGES, "max_size": 896, "jpeg_quality": 82, "max_tokens": 1900}
    if frame_change_score < 6.0 and motion_confidence < 0.4:
        return {"max_images": 4, "max_size": 768, "jpeg_quality": 76, "max_tokens": 1500}
    if frame_change_score < 14.0 and motion_confidence < 0.7:
        return {"max_images": 6, "max_size": 896, "jpeg_quality": 80, "max_tokens": 1700}
    return {"max_images": MAX_ANALYSIS_IMAGES, "max_size": 1024, "jpeg_quality": 85, "max_tokens": 2000}


def _chat_completion_with_retry(client, request_kwargs, max_attempts=3):
    last_error = None
    for attempt in range(1, max_attempts + 1):
        try:
            return client.chat.completions.create(**request_kwargs)
        except Exception as err:
            last_error = err
            message = str(err).lower()
            retryable = (
                "429" in message
                or "rate limit" in message
                or "timeout" in message
                or "timed out" in message
                or "connection" in message
            )
            if attempt >= max_attempts or not retryable:
                raise
            sleep_seconds = min(6.0, 1.2 * (2 ** (attempt - 1)))
            print(f"⚠️ 第{attempt}次请求失败，{sleep_seconds:.1f}s后重试：{err}")
            time.sleep(sleep_seconds)
    if last_error:
        raise last_error


def _read_gray(path, max_side=720):
    if not path or not os.path.exists(path):
        return None
    image = cv2.imread(path, cv2.IMREAD_GRAYSCALE)
    if image is None:
        return None
    h, w = image.shape[:2]
    scale = min(1.0, float(max_side) / float(max(h, w)))
    if scale < 1.0:
        image = cv2.resize(image, (int(w * scale), int(h * scale)), interpolation=cv2.INTER_AREA)
    return image


def _frame_change_score(frames) -> float:
    start_gray = _read_gray(frames.get("start"))
    end_gray = _read_gray(frames.get("end"))
    if start_gray is None or end_gray is None:
        return 0.0
    if start_gray.shape != end_gray.shape:
        end_gray = cv2.resize(end_gray, (start_gray.shape[1], start_gray.shape[0]))
    return float(cv2.absdiff(start_gray, end_gray).mean())


def _estimate_motion_metrics(frames):
    start_gray = _read_gray(frames.get("start"))
    end_gray = _read_gray(frames.get("end"))
    frame_change = _frame_change_score(frames)
    base = {"label": "固定", "tx": 0.0, "ty": 0.0, "translation_norm": 0.0, "zoom_delta": 0.0, "track_points": 0, "confidence": 0.0, "frame_change": frame_change}
    if start_gray is None or end_gray is None:
        return base
    if start_gray.shape != end_gray.shape:
        end_gray = cv2.resize(end_gray, (start_gray.shape[1], start_gray.shape[0]))
    h, w = start_gray.shape[:2]
    p0 = cv2.goodFeaturesToTrack(start_gray, maxCorners=260, qualityLevel=0.01, minDistance=7, blockSize=7)
    if p0 is None:
        return base
    p1, st, _ = cv2.calcOpticalFlowPyrLK(start_gray, end_gray, p0, None, winSize=(21, 21), maxLevel=3)
    if p1 is None or st is None:
        return base
    good0 = p0[st.reshape(-1) == 1].reshape(-1, 2)
    good1 = p1[st.reshape(-1) == 1].reshape(-1, 2)
    n = int(good0.shape[0])
    sparse_tx, sparse_ty = 0.0, 0.0
    sparse_trans_norm = 0.0
    if n >= MOTION_MIN_TRACKS:
        flow = good1 - good0
        sparse_tx, sparse_ty = np.median(flow, axis=0).tolist()
        sparse_trans_norm = float(((sparse_tx * sparse_tx + sparse_ty * sparse_ty) ** 0.5) / max((w * w + h * h) ** 0.5, 1.0))
    dense = cv2.calcOpticalFlowFarneback(
        start_gray,
        end_gray,
        None,
        pyr_scale=0.5,
        levels=3,
        winsize=19,
        iterations=4,
        poly_n=5,
        poly_sigma=1.2,
        flags=0
    )
    dense_step = 4
    dense_u = dense[::dense_step, ::dense_step, 0]
    dense_v = dense[::dense_step, ::dense_step, 1]
    dense_tx = float(np.median(dense_u))
    dense_ty = float(np.median(dense_v))
    dense_diag = max((w * w + h * h) ** 0.5, 1.0)
    dense_trans_norm = float(((dense_tx * dense_tx + dense_ty * dense_ty) ** 0.5) / dense_diag)
    yy, xx = np.mgrid[0:start_gray.shape[0]:dense_step, 0:start_gray.shape[1]:dense_step]
    cx = (start_gray.shape[1] - 1) * 0.5
    cy = (start_gray.shape[0] - 1) * 0.5
    rx = xx - cx
    ry = yy - cy
    rr = np.sqrt(rx * rx + ry * ry) + 1e-6
    radial = (dense_u * (rx / rr)) + (dense_v * (ry / rr))
    zoom_dense = float(np.median(radial) / max(dense_diag, 1.0)) * 2.4
    if n >= MOTION_MIN_TRACKS:
        tx = float(0.65 * sparse_tx + 0.35 * dense_tx)
        ty = float(0.65 * sparse_ty + 0.35 * dense_ty)
        trans_norm = float(0.65 * sparse_trans_norm + 0.35 * dense_trans_norm)
    else:
        tx = dense_tx
        ty = dense_ty
        trans_norm = dense_trans_norm
    diag = max((w * w + h * h) ** 0.5, 1.0)
    trans_norm = float((tx * tx + ty * ty) ** 0.5 / diag) if trans_norm <= 0 else trans_norm
    zoom_delta = 0.0
    if n >= MOTION_MIN_TRACKS:
        m, _ = cv2.estimateAffinePartial2D(good0, good1, method=cv2.RANSAC, ransacReprojThreshold=3.0)
        if m is not None:
            zoom_delta = float(((m[0, 0] ** 2 + m[1, 0] ** 2) ** 0.5) - 1.0)
    if abs(zoom_delta) < 0.003:
        zoom_delta = zoom_dense
    label = "固定"
    if abs(zoom_delta) >= 0.012 and abs(zoom_delta) >= trans_norm * 0.9:
        label = "缓慢推近" if zoom_delta > 0 else "缓慢拉远"
    elif abs(float(tx)) / max(float(w), 1.0) >= 0.008:
        label = "水平摇移"
    elif abs(float(ty)) / max(float(h), 1.0) >= 0.008:
        label = "垂直摇移"
    elif trans_norm >= 0.006:
        label = "缓慢跟移"
    tracks_factor = min(1.0, max(n, MOTION_MIN_TRACKS) / 90.0)
    dense_energy = float(np.mean(np.sqrt(dense_u * dense_u + dense_v * dense_v))) / max(diag, 1.0)
    conf = tracks_factor * min(1.0, (trans_norm * 120.0) + abs(zoom_delta) * 25.0 + frame_change / 20.0 + dense_energy * 220.0)
    return {"label": label, "tx": float(tx), "ty": float(ty), "translation_norm": trans_norm, "zoom_delta": zoom_delta, "track_points": n, "confidence": round(float(conf), 3), "frame_change": frame_change}


def _collect_analysis_images(frames, max_images=12):
    ordered_paths = []
    for key in ("start", "mid", "end"):
        path = frames.get(key)
        if isinstance(path, str) and os.path.exists(path) and path not in ordered_paths:
            ordered_paths.append(path)
    for path in frames.get("reference", []):
        if isinstance(path, str) and os.path.exists(path) and path not in ordered_paths:
            ordered_paths.append(path)
        if len(ordered_paths) >= max_images:
            break
    return ordered_paths[:max_images]

def _overlay_grid(frame):
    h, w = frame.shape[:2]
    overlay = frame.copy()
    color = (255, 180, 0)
    for i in (1, 2):
        x = int(w * i / 3)
        y = int(h * i / 3)
        cv2.line(overlay, (x, 0), (x, h), color, 1)
        cv2.line(overlay, (0, y), (w, y), color, 1)
    cv2.line(overlay, (w // 2, 0), (w // 2, h), color, 1)
    cv2.line(overlay, (0, h // 2), (w, h // 2), color, 1)
    return cv2.addWeighted(overlay, STORYBOARD_GRID_ALPHA, frame, 1 - STORYBOARD_GRID_ALPHA, 0)


def _build_storyboard_visual_aids(frames, max_images=12):
    raw_paths = _collect_analysis_images(frames, max_images=max(3, max_images - 1))
    if not raw_paths:
        return []
    aid_dir = os.path.join(os.path.dirname(raw_paths[0]), "analysis_aids")
    os.makedirs(aid_dir, exist_ok=True)
    aided_paths = []
    for idx, src in enumerate(raw_paths):
        frame = cv2.imread(src)
        if frame is None:
            continue
        dst = os.path.join(aid_dir, f"grid_{idx + 1:02d}.jpg")
        cv2.imwrite(dst, _overlay_grid(frame), [int(cv2.IMWRITE_JPEG_QUALITY), 92])
        aided_paths.append(dst)
    start = cv2.imread(frames.get("start", ""))
    end = cv2.imread(frames.get("end", ""))
    if start is not None and end is not None:
        if start.shape != end.shape:
            end = cv2.resize(end, (start.shape[1], start.shape[0]))
        ghost = cv2.addWeighted(start, 0.5, end, 0.5, 0)
        ghost_path = os.path.join(aid_dir, "ghost_start_end.jpg")
        cv2.imwrite(ghost_path, ghost, [int(cv2.IMWRITE_JPEG_QUALITY), 92])
        aided_paths.append(ghost_path)
    return aided_paths[:max_images]


def analyze_with_gemini(frames, client, model_name, analysis_mode):
    """调用 Gemini (via OpenAI Proxy) 分析图片"""
    motion_metrics = _estimate_motion_metrics(frames)
    frame_change_score = motion_metrics["frame_change"]
    payload_plan = _resolve_payload_plan(
        analysis_mode=analysis_mode,
        frame_change_score=frame_change_score,
        motion_confidence=float(motion_metrics.get("confidence", 0.0))
    )
    if analysis_mode == 'storyboard':
        image_paths = _build_storyboard_visual_aids(frames, max_images=payload_plan["max_images"])
        if not image_paths:
            image_paths = _collect_analysis_images(frames, max_images=payload_plan["max_images"])
        motion_hint = f"系统运镜预检：{motion_metrics['label']}，平移强度={motion_metrics['translation_norm']:.4f}，缩放变化={motion_metrics['zoom_delta']:.4f}，跟踪点={motion_metrics['track_points']}，置信度={motion_metrics['confidence']:.3f}。"
        prompt_text = f"""
        你是一位电影分镜导演，请分析一组分镜参考帧并还原“最终成片画面”。
        输入图像可能是线稿、草图、分镜标注图。你必须把它们理解为导演意图，不要描述“线条/草稿纸/手绘痕迹”等中间媒介信息。
        请直接输出最终成片中观众会看到的内容：角色外观特征、服装、表情、动作、景别、构图、镜头运动、光影和环境氛围。
        图片已叠加网格，末尾含首尾叠影图；请结合网格中的相对位移进行运镜判断。
        {motion_hint}
        系统检测到起止帧变化强度约为 {frame_change_score:.3f}。
        规则：优先参考系统运镜预检；仅当网格位置与透视关系在多帧中几乎不变时才判固定。
        请输出简洁中文JSON（不要Markdown代码块）。
        重要：请在 `ai_generation_prompt` 字段中，输出“成片级”中文提示词，不要出现“线稿/草图/分镜图”等词。
        该提示词必须包含：人物/主体特征 + 动作行为 + 镜头景别与运镜 + 场景与光影风格。
        {{
            "subject_count": "主体数量（数字）",
            "subjects": ["主体类别"],
            "key_objects": ["关键物体或元素（3-8项）"],
            "shot_size": "景别",
            "composition": "构图",
            "action_summary": "动作概述",
            "camera_movement": "运镜（固定/缓慢推近/缓慢拉远/水平摇移/垂直摇移/跟移）",
            "movement_basis": "运镜判定依据（1句话）",
            "visual_description": "一句话简述，不超过30字",
            "ai_generation_prompt": "一段连贯的AI视频生成提示词，格式：[景别/构图]+[人物或主体特征]+[动作与运镜]+[场景材质/光影/色调/风格]。必须描述成片画面，不得出现线稿草图相关表述。",
            "reusable_tags": ["可复用标签，6-12项"]
        }}
        """
    else:
        image_paths = _collect_analysis_images(frames, max_images=payload_plan["max_images"])
        prompt_text = """
        你是一位镜头语言分析师，请分析一组镜头参考帧并提取可复用信息。
        请避免繁琐细节描写，只输出适合迁移到其它项目的通用镜头要素。
        请输出简洁中文，不要输出英文。严格返回JSON（不要使用Markdown代码块）。
        重要：请在 `ai_generation_prompt` 字段中生成一段连贯的AI视频生成提示词。
        {
            "subject_count": "主体数量（数字）",
            "subjects": ["主体类别（如单人、双人、多人、群像）"],
            "key_objects": ["关键物体或元素（3-8项）"],
            "shot_size": "景别（特写/中景/远景/大全景等）",
            "composition": "构图（如中景双人对峙、前后景层次、居中构图）",
            "action_summary": "动作概述（短句）",
            "camera_movement": "运镜（固定/缓慢推近/缓慢拉远/水平摇镜/跟拍等）",
            "movement_basis": "运镜判定依据（1句话）",
            "visual_description": "一句话简述，不超过30字",
            "ai_generation_prompt": "一段连贯的AI视频生成提示词，格式：[景别/构图]+[主体描述]+[动态/运镜]+[光影/氛围]。",
            "reusable_tags": ["可复用标签，6-12项"]
        }
        """

    messages = [{"role": "user", "content": [{"type": "text", "text": prompt_text}]}]

    for image_path in image_paths:
        if os.path.exists(image_path):
            base64_image = encode_image(
                image_path=image_path,
                max_size=payload_plan["max_size"],
                jpeg_quality=payload_plan["jpeg_quality"]
            )
            if base64_image:
                messages[0]["content"].append({
                    "type": "image_url",
                    "image_url": {
                        "url": f"data:image/jpeg;base64,{base64_image}"
                    }
                })
    
    if len(messages[0]["content"]) == 1:
        return None

    try:
        print(f"⏳ 正在请求 AI 分析 ({model_name})...")
        start_time = datetime.now()
        response = _chat_completion_with_retry(
            client=client,
            request_kwargs={
                "model": model_name,
                "messages": messages,
                "max_tokens": payload_plan["max_tokens"],
                "response_format": {"type": "json_object"},
                "timeout": 120.0
            },
            max_attempts=3
        )
        duration = (datetime.now() - start_time).total_seconds()
        print(f"✅ AI 响应成功 (耗时 {duration:.1f}s)")
        
        if not response or not response.choices:
            print(f"❌ API returned invalid response: {response}")
            return None
            
        content = response.choices[0].message.content
        if not content:
            print("❌ API returned empty content")
            return None
            
        cleaned = clean_response(content)
        if not cleaned:
            print(f"❌ Cleaned content is empty. Original: {content[:100]}...")
            return None
            
        result = json.loads(cleaned)
        
        if result is None:
            print("❌ JSON parsed to None")
            return None
            
        if analysis_mode == 'storyboard':
            result["system_motion_label"] = motion_metrics["label"]
            result["system_motion_confidence"] = motion_metrics["confidence"]
            result["system_motion_translation"] = round(motion_metrics["translation_norm"], 4)
            result["system_motion_zoom_delta"] = round(motion_metrics["zoom_delta"], 4)
            result["system_motion_tracks"] = motion_metrics["track_points"]
        return result
    except Exception as e:
        print(f"❌ 分析失败: {e}")
        print(traceback.format_exc())
        
        # 尝试非 JSON 模式解析
        try:
             print("⚠️ 尝试重试 (非 JSON 模式)...")
             response = _chat_completion_with_retry(
                client=client,
                request_kwargs={
                    "model": model_name,
                    "messages": messages,
                    "max_tokens": payload_plan["max_tokens"],
                    "timeout": 120.0
                },
                max_attempts=2
            )
             if not response or not response.choices:
                 return None
                 
             content = response.choices[0].message.content
             cleaned = clean_response(content)
             if not cleaned:
                 return None
                 
             result = json.loads(cleaned)
             if result is None:
                 return None
                 
             if analysis_mode == 'storyboard':
                 result["system_motion_label"] = motion_metrics["label"]
                 result["system_motion_confidence"] = motion_metrics["confidence"]
                 result["system_motion_translation"] = round(motion_metrics["translation_norm"], 4)
                 result["system_motion_zoom_delta"] = round(motion_metrics["zoom_delta"], 4)
                 result["system_motion_tracks"] = motion_metrics["track_points"]
             return result
        except Exception as e2:
             print(f"❌ 重试失败: {e2}")
             return None

def short_text(value, length=50):
    if value is None:
        return ""
    text = str(value)
    return text[:length]


def _check_connectivity(client, runtime):
    cache_key = f"{runtime['base_url']}|{runtime['model_name']}|{runtime['api_key']}"
    now_ts = time.time()
    last_ts = API_CHECK_CACHE.get(cache_key, 0.0)
    if API_CHECK_TTL_SECONDS > 0 and (now_ts - last_ts) <= API_CHECK_TTL_SECONDS:
        print(f"⏭️ 跳过 API 连通性检查（{API_CHECK_TTL_SECONDS}s 内已通过）")
        return
    client.chat.completions.create(
        model=runtime["model_name"],
        messages=[{"role": "user", "content": "Hello"}],
        max_tokens=5,
        timeout=10.0
    )
    API_CHECK_CACHE[cache_key] = now_ts
    print("✅ API 连通性检查通过")


def run_video_analysis(video_path, output_base_dir, runtime, verify_connectivity=True):
    os.makedirs(output_base_dir, exist_ok=True)
    print(f"🚀 开始分析视频: {video_path}")
    print(f"🔗 API Endpoint: {runtime['base_url']}")
    print(f"🤖 Model: {runtime['model_name']}")

    bootstrap_client = OpenAI(
        api_key=runtime["api_key"],
        base_url=runtime["base_url"],
        timeout=120.0
    )
    if verify_connectivity:
        print("🔌 正在检查 API 连通性...")
        _check_connectivity(bootstrap_client, runtime)

    scene_threshold = runtime["scene_threshold_live"] if runtime["analysis_mode"] == "live_action" else runtime["scene_threshold_storyboard"]
    scenes = split_scenes(video_path, threshold=scene_threshold, analysis_mode=runtime["analysis_mode"])
    if runtime["analysis_mode"] == "storyboard":
        scenes = apply_scene_policy(
            scenes=scenes,
            min_duration=runtime["min_scene_duration_storyboard"],
            keep_short_scene=runtime["keep_short_scene_storyboard"]
        )

    if not scenes:
        duration = get_video_duration(video_path)
        scenes = [{"index": 1, "start": 0.0, "end": duration}]
        print("⚠️ 未检测到明显场景变化，将作为单个镜头分析。")
    else:
        print(f"✅ 共检测到 {len(scenes)} 个场景。")

    total_scenes = max(1, len(scenes))
    prep_workers = max(1, min(PREP_MAX_CONCURRENCY, total_scenes))
    print(f"🧩 并发准备：worker={prep_workers}, scenes={total_scenes}")
    prepared_scenes = []
    failed = []

    def _prepare_scene(scene):
        frames = extract_key_frames(
            video_path=video_path,
            scene=scene,
            output_base_dir=output_base_dir,
            smart_profile=runtime["smart_profile"],
            motion_preference=runtime["motion_preference"],
            smart_window=runtime["smart_window"],
            analysis_mode=runtime["analysis_mode"]
        )
        return {"scene": scene, "frames": frames}

    with ThreadPoolExecutor(max_workers=prep_workers) as prep_executor:
        prep_futures = {prep_executor.submit(_prepare_scene, scene): scene for scene in scenes}
        for future in as_completed(prep_futures):
            scene = prep_futures[future]
            try:
                prepared_scenes.append(future.result())
                print(f"  ✅ 准备完成 镜头{scene['index']}")
            except Exception as e:
                failed.append({
                    "scene_index": scene["index"],
                    "time_range": f"{scene['start']:.2f}s - {scene['end']:.2f}s",
                    "error": f"抽帧失败: {e}"
                })
                print(f"  ❌ 准备失败 镜头{scene['index']}: {e}")
    prepared_scenes.sort(key=lambda x: int(x["scene"]["index"]))

    full_report = []
    analyzable_total = len(prepared_scenes)
    if analyzable_total > 0:
        analysis_workers = max(1, min(ANALYSIS_MAX_CONCURRENCY, analyzable_total))
        print(f"⚡ 并发分析：worker={analysis_workers}, tasks={analyzable_total}")
        local_pool = threading.local()

        def _analyze_scene(item):
            scene = item["scene"]
            client = getattr(local_pool, "client", None)
            if client is None:
                client = OpenAI(
                    api_key=runtime["api_key"],
                    base_url=runtime["base_url"],
                    timeout=120.0
                )
                local_pool.client = client
            analysis = analyze_with_gemini(
                frames=item["frames"],
                client=client,
                model_name=runtime["model_name"],
                analysis_mode=runtime["analysis_mode"]
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
                "analysis": analysis
            }

        with ThreadPoolExecutor(max_workers=analysis_workers) as analyze_executor:
            analyze_futures = {analyze_executor.submit(_analyze_scene, item): item["scene"] for item in prepared_scenes}
            for future in as_completed(analyze_futures):
                scene = analyze_futures[future]
                try:
                    result = future.result()
                    if result.get("ok"):
                        full_report.append({
                            "scene_index": result["scene_index"],
                            "time_range": result["time_range"],
                            "analysis": result["analysis"]
                        })
                        analysis = result["analysis"]
                        print(f"  📸 镜头{scene['index']} 运镜: {analysis.get('camera_movement')}")
                        print(f"  📝 描述: {short_text(analysis.get('visual_description'))}...")
                    else:
                        failed.append({
                            "scene_index": result["scene_index"],
                            "time_range": result["time_range"],
                            "error": result["error"]
                        })
                        print(f"  ❌ 镜头{scene['index']} 分析失败: {result['error']}")
                except Exception as e:
                    failed.append({
                        "scene_index": scene["index"],
                        "time_range": f"{scene['start']:.2f}s - {scene['end']:.2f}s",
                        "error": f"分析异常: {e}"
                    })
                    print(f"  ❌ 镜头{scene['index']} 分析异常: {e}")
    full_report.sort(key=lambda x: int(x.get("scene_index", 0)))
    failed.sort(key=lambda x: int(x.get("scene_index", 0)))

    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    report_path = os.path.join(output_base_dir, f"final_report_{timestamp}.json")
    with open(report_path, 'w', encoding='utf-8') as f:
        json.dump(full_report, f, indent=2, ensure_ascii=False)
    latest_report_path = os.path.join(output_base_dir, "final_report_latest.json")
    with open(latest_report_path, 'w', encoding='utf-8') as f:
        json.dump(full_report, f, indent=2, ensure_ascii=False)

    print(f"\n✅ 分析完成！完整报告已保存至: {report_path}")
    print(f"📌 最新报告同步至: {latest_report_path}")
    if failed:
        print(f"⚠️ 失败镜头数: {len(failed)}")

    return {
        "summary": {
            "total_scenes": total_scenes,
            "success_scenes": len(full_report),
            "failed_scenes": len(failed),
            "analysis_mode": runtime["analysis_mode"]
        },
        "scenes": full_report,
        "failed": failed,
        "report_path": report_path,
        "latest_report_path": latest_report_path
    }

def main():
    parser = argparse.ArgumentParser(description='视频镜头分析工具')
    parser.add_argument('video_path', nargs='?', help='视频文件路径')
    parser.add_argument('--output', type=str, default='analysis_output', help='输出目录')
    parser.add_argument('--config', type=str, default=get_default_config_path(), help='配置文件路径')
    parser.add_argument('--init-config', action='store_true', help='生成默认配置文件并退出')
    parser.add_argument('--api_key', type=str, default=None, help='模型 API Key')
    parser.add_argument('--base_url', type=str, default=None, help='模型 API Base URL')
    parser.add_argument('--model', type=str, default=None, help='模型名称')
    parser.add_argument('--analysis_mode', type=str, default=None, help='分析模式: live_action/storyboard')
    parser.add_argument('--scene_threshold_live', type=float, default=None, help='实拍模式切分阈值')
    parser.add_argument('--scene_threshold_storyboard', type=float, default=None, help='分镜模式切分阈值')
    parser.add_argument('--min_scene_duration_storyboard', type=float, default=None, help='分镜模式最短镜头时长(秒)')
    parser.add_argument('--keep_short_scene_storyboard', type=str, default=None, help='分镜模式是否保留超短镜头: true/false')
    parser.add_argument('--smart_profile', type=str, default=None, help='auto/landscape/portrait/action')
    parser.add_argument('--motion_preference', type=str, default=None, help='auto/stable/dynamic')
    parser.add_argument('--smart_window', type=float, default=None, help='智能抽帧窗口秒数')
    args = parser.parse_args()

    if args.init_config:
        write_default_config(args.config)
        print(f"✅ 已生成配置文件: {args.config}")
        return

    if not args.video_path:
        print("用法: python analyze_video.py <video_path> [--config xxx.json]")
        sys.exit(1)

    try:
        runtime = resolve_runtime_config(args)
    except Exception as e:
        print(f"❌ 配置错误: {e}")
        sys.exit(1)

    video_path = args.video_path
    output_base_dir = args.output
    print(f"⚙️ 配置文件: {args.config}")
    run_video_analysis(
        video_path=video_path,
        output_base_dir=output_base_dir,
        runtime=runtime,
        verify_connectivity=True
    )

if __name__ == "__main__":
    main()
