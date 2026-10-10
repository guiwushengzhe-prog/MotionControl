import hashlib
import importlib.util
import json
import math
import sys
from pathlib import Path

archive, destination = map(Path, sys.argv[1:])
reference_path = archive / 'frozen22/MotionControl_head_control_v186_stageA_frozen22_evidence.py'
spec = importlib.util.spec_from_file_location('audited_head_reference', reference_path)
reference = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = reference
spec.loader.exec_module(reference)
names = ('nose', 'left_eye_inner', 'left_eye', 'left_eye_outer',
         'right_eye_inner', 'right_eye', 'right_eye_outer', 'left_ear', 'right_ear',
         'mouth_left', 'mouth_right')
digest = lambda path: hashlib.sha256(path.read_bytes()).hexdigest()
document = {'source': 'r3_minimal_ab_20260901/expanded',
            'reference_sha256': digest(reference_path), 'streams': {}}
for tag in ('A', 'B'):
    source = archive / f'data/{tag}.pose.frames.jsonl'
    baseline_path = archive / f'frozen22/baseline_{tag}.json'
    baseline = json.loads(baseline_path.read_text(encoding='utf-8'))
    center, sigma, signature = (baseline[key] for key in ('calibration_center', 'calibration_sigma', 'signature'))
    rows = [json.loads(line) for line in source.read_text(encoding='utf-8').splitlines() if line.strip()]
    cases = []
    for index, row in enumerate(rows):
        pose = dict(zip(names, row['image_landmarks'][:11]))
        result = reference._head11_local_feature(pose, row['width'], row['height'])
        if result is None:
            continue
        feature, scale = result
        yaw = reference._personal22_matched_yaw(feature, center, sigma, signature)
        if math.isfinite(yaw):
            cases.append({'frame': index, 'pose': pose, 'width': row['width'], 'height': row['height'],
                          'feature': list(feature), 'scale': scale, 'yaw': yaw})
    positions = {round(i * (len(cases) - 1) / 15) for i in range(16)}
    positions.update((min(range(len(cases)), key=lambda i: cases[i]['yaw']),
                      max(range(len(cases)), key=lambda i: cases[i]['yaw'])))
    chosen = [cases[i] for i in sorted(positions)]
    document['streams'][tag] = {'source_sha256': digest(source), 'baseline_sha256': digest(baseline_path),
                              'total_frames': len(rows), 'calibration_center': center,
                              'calibration_sigma': sigma, 'signature': signature, 'cases': chosen}
destination.parent.mkdir(parents=True, exist_ok=True)
destination.write_text(json.dumps(document, ensure_ascii=False, allow_nan=False, indent=2) + '\n', encoding='utf-8')
print({tag: len(stream['cases']) for tag, stream in document['streams'].items()})
print(destination.stat().st_size)
