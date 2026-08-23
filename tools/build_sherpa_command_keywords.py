from __future__ import annotations
import argparse, json, re, shutil, subprocess, sys
from pathlib import Path

PUNCT = re.compile(r"[\s\u3000，。！？、,.!?;；:：]+")

def compact(s: str) -> str:
    return PUNCT.sub("", str(s or "").strip().lower())

def load_registry(path: Path):
    data = json.loads(path.read_text(encoding="utf-8"))
    profiles = data["profiles"]
    commands = data["commands"]
    ids, phrases = set(), {}
    for c in commands:
        cid, phrase = str(c["id"]), str(c["phrase"]).strip()
        if cid in ids: raise ValueError(f"duplicate command id: {cid}")
        ids.add(cid)
        key = compact(phrase)
        if not key: raise ValueError(f"empty phrase: {cid}")
        if not key.startswith(compact(data.get("prefix", "体感"))):
            raise ValueError(f"phrase must start with prefix: {phrase}")
        if key in phrases: raise ValueError(f"duplicate phrase: {phrase} / {phrases[key]}")
        if c["profile"] not in profiles: raise ValueError(f"unknown profile: {c['profile']}")
        phrases[key] = cid
    # One default keyboard target = one voice command. Avoid redundant phrases
    # such as both “继续游戏” and “确认” mapping to ENTER.
    keyboard_targets = {}
    for c in commands:
        if c.get("kind") != "keyboard":
            continue
        target = compact(c.get("default_target", ""))
        if not target:
            continue
        if target in keyboard_targets:
            raise ValueError(
                f"duplicate keyboard target: {c.get('default_target')} / "
                f"{c['id']} / {keyboard_targets[target]}"
            )
        keyboard_targets[target] = c["id"]

    keys = sorted(phrases)
    for i, a in enumerate(keys):
        for b in keys[i+1:]:
            if a.startswith(b) or b.startswith(a):
                raise ValueError(f"prefix collision: {a} <-> {b}")
    return data

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--registry", type=Path, required=True)
    ap.add_argument("--model", type=Path, required=True)
    ap.add_argument("--out", type=Path, required=True)
    ap.add_argument("--no-tokenize", action="store_true")
    ns = ap.parse_args()
    data = load_registry(ns.registry)
    ns.out.mkdir(parents=True, exist_ok=True)
    raw = ns.out / "keywords_raw.txt"
    action_map = ns.out / "voice_action_map.json"
    lines, mapping = [], {}
    for c in data["commands"]:
        p = data["profiles"][c["profile"]]
        phrase = c["phrase"].replace(" ", "_")
        lines.append(f"{c['phrase']} :{float(p['boosting_score']):.2f} #{float(p['trigger_threshold']):.2f} @{phrase}")
        mapping[compact(c["phrase"])] = {"id": c["id"], "label": c["label"], "kind": c["kind"], "default_target": c.get("default_target"), "cooldown_ms": int(p.get("cooldown_ms", 700))}
    raw.write_text("\n".join(lines)+"\n", encoding="utf-8")
    action_map.write_text(json.dumps(mapping, ensure_ascii=False, indent=2)+"\n", encoding="utf-8")
    if ns.no_tokenize:
        print(f"OK registry={len(data['commands'])} raw={raw}")
        return
    cli = shutil.which("sherpa-onnx-cli")
    if not cli: raise SystemExit("sherpa-onnx-cli not found; install sherpa-onnx or use --no-tokenize")
    tokens = ns.model / "tokens.txt"; lexicon = ns.model / "en.phone"
    if not tokens.is_file() or not lexicon.is_file(): raise SystemExit("model tokens.txt/en.phone missing")
    out = ns.out / "keywords.txt"
    cmd = [cli, "text2token", "--tokens", str(tokens), "--tokens-type", "phone+ppinyin", "--lexicon", str(lexicon), str(raw), str(out)]
    subprocess.run(cmd, check=True)
    print(f"OK commands={len(data['commands'])} keywords={out}")

if __name__ == "__main__":
    main()
