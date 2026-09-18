from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(sys.argv[1]).resolve() if len(sys.argv) > 1 else Path(r"F:\MotionControl-App")
sys.path.insert(0, str(ROOT))

from motioncontrol.sherpa_kws_backend import SherpaKwsCommandRecognizer, find_sherpa_kws_model

config = json.loads((ROOT / "config" / "voice_mappings.json").read_text(encoding="utf-8"))
model = find_sherpa_kws_model(ROOT)
if model is None:
    raise SystemExit("FAIL: sherpa KWS model not found")
rec = SherpaKwsCommandRecognizer(
    model,
    config.get("wake_word", "体感"),
    config.get("mappings", []),
    config.get("emergency_stop_phrases", ["体感紧急停止"]),
)
print("OK")
print("model:", model)
print("mode:", rec.mode)
print("supported:", len(rec.supported), rec.supported)
print("unsupported:", rec.unsupported)
