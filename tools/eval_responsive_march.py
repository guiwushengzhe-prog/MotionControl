"""用已有骨架记录比较新旧踏步；关闭实际输出，不读写用户配置。

python tools/eval_responsive_march.py 记录.jsonl --output 报告.json
只比较同一组默认设置下的识别结果，不代表真人游戏验收。
"""

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from motioncontrol.control_kernel import ControlKernel
from motioncontrol.intent_library import _NullOutput
from motioncontrol.intent_recording import pose_from_frame
from motioncontrol_shared.pose_library import normalize_action


def replay(frames, algorithm):
    kernel = ControlKernel(_NullOutput(), persist=False)
    docs = [normalize_action(json.loads(p.read_text(encoding='utf-8')))
            for p in sorted((ROOT / 'cloud' / 'official_poses').glob('*.json'))
            if p.name != 'signatures.json']
    rows = []
    try:
        kernel.configure_pose_actions(docs)
        kernel.configure_motions([
            {'id': 'march', 'enabled': True, 'type': 'gamepad_axis', 'target': 'LS_UP'},
            {'id': 'calf_back', 'enabled': True, 'type': 'gamepad', 'target': 'B'},
            {'id': 'cross_knee_elbow', 'enabled': True, 'type': 'gamepad', 'target': 'X'},
        ])
        kernel.configure_march_algorithm(algorithm)
        for frame in frames:
            t = float(frame['t'])
            with kernel._lock:
                kernel.width, kernel.height = frame['w'], frame['h']
                kernel.pose_sample_at = 1000 + float(frame['c']) if 'c' in frame else None
                kernel._process_pose_locked(pose_from_frame(frame), 1000 + t)
                lifts = (kernel.feet or {}).get('lift')
                rows.append({'t': t, 'active': 'march' in kernel.motion_active,
                             'grounded': bool(lifts and all(v < .04 for v in lifts.values()))})
    finally:
        kernel.close()
    return rows


def measure(rows, step):
    start, end = step.get('start_t'), step.get('end_t')
    if start is None or end is None:
        return None
    selected = [row for row in rows if start <= row['t'] <= end]
    first_on = next((row['t'] for row in selected if row['active']), None)
    active_s = sum(b['t'] - a['t'] for a, b in zip(selected, selected[1:]) if a['active'])
    starts = sum(row['active'] and (i == 0 or not selected[i - 1]['active'])
                 for i, row in enumerate(selected))
    # 最后一次两脚落地后的持续静止段；只在之后确实停下的情况下报告尾延迟。
    tail_rows = [row for row in rows if start <= row['t'] <= end + 1.] if step['key'] == 'action:motion.march' else selected
    last_air = next((i for i in range(len(tail_rows) - 1, -1, -1) if not tail_rows[i]['grounded']), None)
    stop_ms = None
    if last_air is not None and last_air + 1 < len(tail_rows):
        grounded = tail_rows[last_air + 1]
        if tail_rows[last_air]['active']:
            stopped = next((row for row in tail_rows[last_air + 1:] if not row['active']), None)
            if stopped:
                stop_ms = round(max(0, stopped['t'] - grounded['t']) * 1000, 1)
    return {'key': step['key'], 'name': step['name'], 'duration_s': round(end - start, 3),
            'first_on_t': first_on, 'active_s': round(active_s, 3),
            'active_runs': starts, 'final_ground_stop_ms': stop_ms}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('recordings', nargs='+', type=Path)
    parser.add_argument('--output', type=Path)
    args = parser.parse_args()
    report = {'conditions': '关闭实际输出、独立临时内核、相同默认区域、小腿后抬及碰肘均有映射',
              'limits': '已有骨架离线回放；起动时刻是记录内时刻，不能当作端到端延迟', 'recordings': []}
    for path in args.recordings:
        data = [json.loads(line) for line in path.read_text(encoding='utf-8').splitlines() if line.strip()]
        item = {'file': path.name, 'frames': len(data) - 1}
        for algorithm in ('legacy', 'responsive'):
            rows = replay(data[1:], algorithm)
            item[algorithm] = [result for step in data[0]['steps'] if (result := measure(rows, step))]
        report['recordings'].append(item)
    encoded = json.dumps(report, ensure_ascii=False, indent=2)
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(encoded + '\n', encoding='utf-8')
    print(encoded)


if __name__ == '__main__':
    main()
