#!/usr/bin/env python3
"""
视频抽帧脚本
支持间隔抽帧、均匀采样,输出关键帧图片
"""

import os
import sys
import cv2
import argparse
import math
from typing import Dict, Any


def _format_timestamp(seconds: float) -> str:
    return f"{int(seconds // 3600):02d}:{int((seconds % 3600) // 60):02d}:{int(seconds % 60):02d}"


def _clarity_score(gray_frame) -> float:
    laplacian = cv2.Laplacian(gray_frame, cv2.CV_64F)
    return float(laplacian.var())


def _entropy_score(gray_frame, bins: int = 64) -> float:
    hist = cv2.calcHist([gray_frame], [0], None, [bins], [0, 256])
    total = float(hist.sum())
    if total <= 0:
        return 0.0
    entropy = 0.0
    for value in hist.flatten():
        p = float(value) / total
        if p > 0:
            entropy -= p * math.log2(p)
    return entropy


def _motion_score(prev_gray_frame, gray_frame) -> float:
    if prev_gray_frame is None:
        return 0.0
    diff = cv2.absdiff(prev_gray_frame, gray_frame)
    return float(diff.mean())


def _normalize(values):
    if not values:
        return []
    if len(values) == 1:
        return [1.0] # 只有一个值时，直接给满分
    min_v = min(values)
    max_v = max(values)
    if max_v - min_v < 1e-9:
        return [0.5 for _ in values]
    return [(v - min_v) / (max_v - min_v) for v in values]


def _resolve_strategy(candidates, smart_profile: str, motion_preference: str):
    profile_weights = {
        'landscape': {'clarity': 0.40, 'entropy': 0.45, 'motion': 0.15, 'default_motion_mode': 'stable'},
        'portrait': {'clarity': 0.60, 'entropy': 0.25, 'motion': 0.15, 'default_motion_mode': 'stable'},
        'action': {'clarity': 0.45, 'entropy': 0.20, 'motion': 0.35, 'default_motion_mode': 'dynamic'}
    }
    if smart_profile == 'auto':
        avg_entropy = sum(c['entropy'] for c in candidates) / len(candidates)
        avg_motion = sum(c['motion'] for c in candidates) / len(candidates)
        # 调整阈值: motion 5.0 通常意味着有明显动作，entropy 4.5 通常意味着画面丰富
        if avg_motion >= 5.0:
            chosen_profile = 'action'
        elif avg_entropy >= 4.5:
            chosen_profile = 'landscape'
        else:
            chosen_profile = 'portrait'
    else:
        chosen_profile = smart_profile
    weights = profile_weights[chosen_profile]
    if motion_preference == 'auto':
        motion_mode = weights['default_motion_mode']
    else:
        motion_mode = motion_preference
    return chosen_profile, motion_mode, weights


def _rank_candidates(candidates, weights, motion_mode: str):
    clarity_norm = _normalize([c['clarity'] for c in candidates])
    entropy_norm = _normalize([c['entropy'] for c in candidates])
    motion_norm = _normalize([c['motion'] for c in candidates])
    ranked = []
    for i, item in enumerate(candidates):
        motion_term = motion_norm[i] if motion_mode == 'dynamic' else (1.0 - motion_norm[i])
        score = (
            weights['clarity'] * clarity_norm[i] +
            weights['entropy'] * entropy_norm[i] +
            weights['motion'] * motion_term
        )
        merged = dict(item)
        merged['score'] = score
        ranked.append(merged)
    return ranked


def _select_smart_frame(
    cap,
    fps: float,
    scene_start: float,
    scene_end: float,
    target_time: float,
    window_seconds: float,
    step_frames: int,
    smart_profile: str,
    motion_preference: str
):
    window_start = max(scene_start, target_time - window_seconds)
    window_end = min(scene_end, target_time + window_seconds)
    if window_end <= window_start:
        window_start = max(scene_start, min(target_time, scene_end))
        window_end = min(scene_end, window_start + (1.0 / fps))

    start_frame = max(0, int(window_start * fps))
    end_frame = max(start_frame + 1, int(window_end * fps))
    cap.set(cv2.CAP_PROP_POS_FRAMES, start_frame)

    candidates = []
    prev_gray = None
    frame_idx = start_frame
    while frame_idx <= end_frame:
        ret, frame = cap.read()
        if not ret:
            break
        if (frame_idx - start_frame) % step_frames == 0:
            gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
            candidates.append({
                'frame_idx': frame_idx,
                'time_seconds': cap.get(cv2.CAP_PROP_POS_MSEC) / 1000.0,
                'clarity': _clarity_score(gray),
                'entropy': _entropy_score(gray),
                'motion': _motion_score(prev_gray, gray)
            })
            prev_gray = gray
        frame_idx += 1

    if not candidates:
        return None

    chosen_profile, motion_mode, weights = _resolve_strategy(
        candidates=candidates,
        smart_profile=smart_profile,
        motion_preference=motion_preference
    )
    ranked = _rank_candidates(candidates, weights=weights, motion_mode=motion_mode)
    best = max(ranked, key=lambda x: x['score'])
    best['chosen_profile'] = chosen_profile
    best['motion_mode'] = motion_mode
    best['weights'] = {
        'clarity': weights['clarity'],
        'entropy': weights['entropy'],
        'motion': weights['motion']
    }
    return best


def extract_frames(
    video_path: str,
    output_dir: str,
    interval: float = 1.0,
    max_frames: int = 10,
    start_time: float = 0,
    end_time: float = None,
    resolution: tuple = None,
    mode: str = 'interval',
    smart_profile: str = 'auto',
    motion_preference: str = 'auto',
    smart_window: float = 1.0
) -> Dict[str, Any]:
    """
    从视频中抽取关键帧

    参数:
        video_path: 视频文件路径
        output_dir: 输出目录
        interval: 抽帧间隔(秒)
        max_frames: 最大抽帧数
        start_time: 开始时间(秒)
        end_time: 结束时间(秒),None表示到视频结尾
        resolution: 输出分辨率,如(1920, 1080),None表示保持原分辨率
        mode: 'interval' (间隔抽帧) 或 'key' (首中尾关键帧) 或 'smart_key' (智能首中尾)
        smart_profile: smart_key 模式下的场景偏好: auto/landscape/portrait/action
        motion_preference: smart_key 模式下运动偏好: auto/stable/dynamic
        smart_window: smart_key 模式窗口秒数上限

    返回:
        抽帧结果字典
    """
    results = {
        'success': True,
        'total_frames': 0,
        'output_files': [],
        'errors': []
    }

    cap = None
    extracted_count = 0
    try:
        # 创建输出目录
        os.makedirs(output_dir, exist_ok=True)

        # 打开视频文件
        cap = cv2.VideoCapture(video_path)

        if not cap.isOpened():
            raise ValueError(f"无法打开视频文件: {video_path}")

        # 获取视频信息
        fps = cap.get(cv2.CAP_PROP_FPS)
        total_frames = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))
        if fps <= 0:
            raise ValueError("视频帧率无效（fps <= 0），无法抽帧")

        video_duration = total_frames / fps

        print(f"视频信息:")
        print(f"  帧率: {fps:.2f} fps")
        print(f"  总帧数: {total_frames}")
        print(f"  时长: {video_duration:.2f} 秒")

        # 计算抽帧范围
        if end_time is None:
            end_time = video_duration

        if start_time >= end_time:
            raise ValueError(f"开始时间({start_time})必须小于结束时间({end_time})")

        if mode not in ('interval', 'key', 'smart_key'):
            raise ValueError(f"不支持的抽帧模式: {mode}，仅支持 interval、key 或 smart_key")
        if smart_profile not in ('auto', 'landscape', 'portrait', 'action'):
            raise ValueError(f"不支持的 smart_profile: {smart_profile}")
        if motion_preference not in ('auto', 'stable', 'dynamic'):
            raise ValueError(f"不支持的 motion_preference: {motion_preference}")
        if smart_window <= 0:
            raise ValueError("smart_window 必须大于 0")

        if mode in ('key', 'smart_key'):
            # 关键帧模式：首帧、中间帧、尾帧
            segment_duration = end_time - start_time
            epsilon = min(0.1, max(segment_duration * 0.1, 1.0 / fps))
            end_sample_time = max(start_time, end_time - epsilon)
            mid_sample_time = (start_time + end_sample_time) / 2
            target_times = [start_time, mid_sample_time, end_sample_time]
            if mode == 'smart_key':
                print("  抽帧模式: 智能关键帧 (局部窗口优选)")
            else:
                print("  抽帧模式: 关键帧 (首/中/尾)")

            for i, t in enumerate(target_times):
                if mode == 'smart_key':
                    window_seconds = min(smart_window, max(0.3, segment_duration * 0.2))
                    step_frames = max(1, int(fps // 8))
                    best = _select_smart_frame(
                        cap=cap,
                        fps=fps,
                        scene_start=start_time,
                        scene_end=end_time,
                        target_time=t,
                        window_seconds=window_seconds,
                        step_frames=step_frames,
                        smart_profile=smart_profile,
                        motion_preference=motion_preference
                    )
                    if best is not None:
                        cap.set(cv2.CAP_PROP_POS_FRAMES, best['frame_idx'])
                        ret, frame = cap.read()
                        current_time = best['time_seconds']
                    else:
                        cap.set(cv2.CAP_PROP_POS_MSEC, t * 1000)
                        ret, frame = cap.read()
                        current_time = cap.get(cv2.CAP_PROP_POS_MSEC) / 1000.0
                else:
                    cap.set(cv2.CAP_PROP_POS_MSEC, t * 1000)
                    ret, frame = cap.read()
                    current_time = cap.get(cv2.CAP_PROP_POS_MSEC) / 1000.0

                if ret:
                    if resolution:
                        frame = cv2.resize(frame, resolution)

                    suffix = ['start', 'mid', 'end'][i]
                    frame_filename = f"frame_{suffix}.jpg"
                    frame_path = os.path.join(output_dir, frame_filename)

                    cv2.imwrite(frame_path, frame, [int(cv2.IMWRITE_JPEG_QUALITY), 95])

                    frame_item = {
                        'filename': frame_filename,
                        'path': frame_path,
                        'timestamp': _format_timestamp(current_time),
                        'time_seconds': current_time,
                        'type': suffix
                    }
                    if mode == 'smart_key' and best is not None:
                        frame_item['quality_score'] = round(float(best['score']), 4)
                        frame_item['clarity_score'] = round(float(best['clarity']), 4)
                        frame_item['entropy_score'] = round(float(best['entropy']), 4)
                        frame_item['motion_score'] = round(float(best['motion']), 4)
                        frame_item['chosen_profile'] = best['chosen_profile']
                        frame_item['motion_mode'] = best['motion_mode']
                        frame_item['weights'] = best['weights']

                    results['output_files'].append(frame_item)
                    extracted_count += 1
                    if mode == 'smart_key' and best is not None:
                        print(
                            f"  抽取 {suffix} 帧: {frame_filename} "
                            f"(时间: {current_time:.2f}s, 评分: {best['score']:.3f})"
                        )
                    else:
                        print(f"  抽取 {suffix} 帧: {frame_filename} (时间: {current_time:.2f}s)")

        else:
            # 原有的间隔抽帧逻辑
            # 计算抽帧间隔(帧数)
            if interval <= 0:
                raise ValueError("interval 必须大于 0")
            frame_interval = max(1, int(interval * fps))

            # 计算起始和结束帧号
            start_frame = int(start_time * fps)
            end_frame = int(end_time * fps)

            # 设置起始帧
            cap.set(cv2.CAP_PROP_POS_FRAMES, start_frame)

            # 计算实际抽帧数
            available_frames = max(0, (end_frame - start_frame) // frame_interval)
            actual_max_frames = min(max_frames, available_frames)

            print(f"\n抽帧配置:")
            print(f"  抽帧间隔: {interval} 秒 ({frame_interval} 帧)")
            print(f"  抽帧范围: {start_time}s - {end_time}s")
            print(f"  最大抽帧数: {max_frames}")
            print(f"  实际抽帧数: {actual_max_frames}")

            if actual_max_frames == 0:
                print("  当前时间段无可抽取帧")

            # 抽帧
            frame_count = 0
            extracted_count = 0

            while extracted_count < actual_max_frames:
                ret, frame = cap.read()

                if not ret:
                    break

                current_time = cap.get(cv2.CAP_PROP_POS_MSEC) / 1000.0

                # 检查是否在时间范围内
                if current_time > end_time:
                    break

                # 按间隔抽帧
                if frame_count % frame_interval == 0:
                    # 调整分辨率
                    if resolution:
                        frame = cv2.resize(frame, resolution)

                    # 生成文件名
                    frame_filename = f"frame_{extracted_count + 1:05d}.jpg"
                    frame_path = os.path.join(output_dir, frame_filename)

                    # 保存图片
                    cv2.imwrite(frame_path, frame, [int(cv2.IMWRITE_JPEG_QUALITY), 95])

                    # 记录结果
                    results['output_files'].append({
                        'filename': frame_filename,
                        'path': frame_path,
                        'timestamp': f"{int(current_time // 3600):02d}:{int((current_time % 3600) // 60):02d}:{int(current_time % 60):02d}",
                        'time_seconds': current_time
                    })

                    extracted_count += 1
                    print(f"  抽取帧 {extracted_count}/{actual_max_frames}: {frame_filename} (时间: {current_time:.2f}s)")

                frame_count += 1

        results['total_frames'] = extracted_count

        print(f"\n[OK] 抽帧完成,共抽取 {extracted_count} 帧")
        print(f"输出目录: {output_dir}")

    except Exception as e:
        results['success'] = False
        results['errors'].append(str(e))
        print(f"[ERROR] 抽帧失败: {str(e)}")

    finally:
        if cap is not None:
            cap.release()

    return results


def main():
    # 设置控制台输出编码为 utf-8
    if sys.platform == 'win32':
        import io
        sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8')
        sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding='utf-8')

    parser = argparse.ArgumentParser(description='视频抽帧工具')
    parser.add_argument('--input', type=str, required=True, help='视频文件路径')
    parser.add_argument('--output', type=str, required=True, help='输出目录')
    parser.add_argument('--interval', type=float, default=1.0, help='抽帧间隔(秒),默认1秒')
    parser.add_argument('--max_frames', type=int, default=10, help='最大抽帧数,默认10')
    parser.add_argument('--start_time', type=float, default=0, help='开始时间(秒),默认0')
    parser.add_argument('--end_time', type=float, default=None, help='结束时间(秒),默认视频结尾')
    parser.add_argument('--resolution', type=str, default=None, help='输出分辨率,如1920x1080,默认保持原分辨率')
    parser.add_argument('--mode', type=str, default='interval', help='抽帧模式: interval(默认) / key / smart_key')
    parser.add_argument('--smart_profile', type=str, default='auto', help='smart_key 场景偏好: auto / landscape / portrait / action')
    parser.add_argument('--motion_preference', type=str, default='auto', help='smart_key 运动偏好: auto / stable / dynamic')
    parser.add_argument('--smart_window', type=float, default=1.0, help='smart_key 窗口秒数上限,默认1.0')

    args = parser.parse_args()

    # 解析分辨率
    resolution = None
    if args.resolution:
        try:
            width, height = map(int, args.resolution.split('x'))
            resolution = (width, height)
        except ValueError:
            print(f"[ERROR] 分辨率格式错误,应为'宽x高',如1920x1080")
            sys.exit(1)

    # 执行抽帧
    results = extract_frames(
        video_path=args.input,
        output_dir=args.output,
        interval=args.interval,
        max_frames=args.max_frames,
        start_time=args.start_time,
        end_time=args.end_time,
        resolution=resolution,
        mode=args.mode,
        smart_profile=args.smart_profile,
        motion_preference=args.motion_preference,
        smart_window=args.smart_window
    )

    # 输出结果
    if results['success']:
        print("\n抽帧文件列表:")
        for frame_info in results['output_files']:
            print(f"  {frame_info['filename']} - {frame_info['timestamp']}")

        # 保存结果JSON
        import json
        result_file = os.path.join(args.output, 'extraction_info.json')
        with open(result_file, 'w', encoding='utf-8') as f:
            json.dump(results, f, indent=2, ensure_ascii=False)
        print(f"\n抽帧信息已保存到: {result_file}")

        sys.exit(0)
    else:
        print("\n抽帧过程中出现错误:")
        for error in results['errors']:
            print(f"  - {error}")
        sys.exit(1)


if __name__ == '__main__':
    main()
