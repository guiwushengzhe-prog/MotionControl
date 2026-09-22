"""声明式动作保持链的纯单调时钟状态机。

识别层只提供布尔信号；本模块不读取摄像头、不注入按键，也不依赖输出后端。
内核可以把任意开始触发、接续条件、持续条件和结束条件组合成一条链。
"""

from __future__ import annotations

import copy
from dataclasses import dataclass
from typing import Mapping


IDLE = "IDLE"
PREPARE = "PREPARE"
CONTINUE = "CONTINUE"
CHARGING = "CHARGING"
RELEASED = "RELEASED"

DEFAULT_ACTION_CHAIN = {
    "enabled": False,
    "prepare_s": 0.16,
    "continue_window_s": 0.30,
    "chains": [
        {
            "id": "headJump_squat_stand",
            "start": "zone.headJump",
            "continue_condition": "motion.squat",
            "sustain_condition": "motion.squat",
            "end_condition": "motion.stand",
            "hold_action": "zone.headJump",
        },
    ],
}


def _seconds(value, default: float, *, maximum: float = 10.0) -> float:
    try:
        result = float(value)
    except (TypeError, ValueError):
        return default
    if result != result or result in (float("inf"), float("-inf")):
        return default
    return max(0.0, min(maximum, result))


def _boolean(value, default: bool = False) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        text = value.strip().lower()
        if text in {"true", "1", "yes", "on"}:
            return True
        if text in {"false", "0", "no", "off"}:
            return False
    return default


@dataclass(frozen=True)
class ChainSpec:
    identifier: str
    start: str
    continue_condition: str | None
    sustain_condition: str | None
    end_condition: str | None
    hold_action: str


@dataclass(frozen=True)
class HoldChainResult:
    state: str
    hold: bool
    hold_action: str | None
    chain_id: str | None
    transition: str | None = None


def normalize_config(config: Mapping | None) -> dict:
    """Return a safe, JSON-compatible configuration.

    A malformed or empty configuration falls back to the disabled default so a
    bad user settings file cannot change the legacy head-jump behavior.
    """
    if not isinstance(config, Mapping):
        return copy.deepcopy(DEFAULT_ACTION_CHAIN)
    result = copy.deepcopy(DEFAULT_ACTION_CHAIN)
    result["enabled"] = _boolean(config.get("enabled", False), False)
    result["prepare_s"] = _seconds(config.get("prepare_s", result["prepare_s"]), 0.16, maximum=2.0)
    result["continue_window_s"] = _seconds(
        config.get("continue_window_s", result["continue_window_s"]), 0.30, maximum=3.0
    )
    raw_chains = config.get("chains", result["chains"])
    chains = []
    if "chains" in config and not isinstance(raw_chains, list):
        return copy.deepcopy(DEFAULT_ACTION_CHAIN)
    if isinstance(raw_chains, list):
        for index, raw in enumerate(raw_chains):
            if not isinstance(raw, Mapping):
                continue
            identifier = str(raw.get("id", f"chain_{index}")).strip()
            start = str(raw.get("start", "")).strip()
            hold_action = str(raw.get("hold_action", start)).strip()
            if not identifier or not start or not hold_action:
                continue
            def optional(name: str) -> str | None:
                value = raw.get(name)
                value = str(value).strip() if value is not None else ""
                return value or None
            chains.append(ChainSpec(
                identifier, start, optional("continue_condition"),
                optional("sustain_condition"), optional("end_condition"), hold_action,
            ))
    if not chains and "chains" in config:
        return copy.deepcopy(DEFAULT_ACTION_CHAIN)
    if not chains:
        chains = [ChainSpec(
            DEFAULT_ACTION_CHAIN["chains"][0]["id"],
            DEFAULT_ACTION_CHAIN["chains"][0]["start"],
            DEFAULT_ACTION_CHAIN["chains"][0]["continue_condition"],
            DEFAULT_ACTION_CHAIN["chains"][0]["sustain_condition"],
            DEFAULT_ACTION_CHAIN["chains"][0]["end_condition"],
            DEFAULT_ACTION_CHAIN["chains"][0]["hold_action"],
        )]
    result["chains"] = [
        {
            "id": item.identifier,
            "start": item.start,
            "continue_condition": item.continue_condition,
            "sustain_condition": item.sustain_condition,
            "end_condition": item.end_condition,
            "hold_action": item.hold_action,
        }
        for item in chains
    ]
    return result


class HoldChain:
    """One active chain at a time; all timestamps are monotonic seconds."""

    def __init__(self, config: Mapping | None = None) -> None:
        self.configure(config or DEFAULT_ACTION_CHAIN)
        self.reset()

    def configure(self, config: Mapping | None) -> dict:
        normalized = normalize_config(config)
        self.config = normalized
        self.enabled = bool(normalized["enabled"])
        self.prepare_s = float(normalized["prepare_s"])
        self.continue_window_s = float(normalized["continue_window_s"])
        self.specs = [ChainSpec(
            item["id"], item["start"], item.get("continue_condition"),
            item.get("sustain_condition"), item.get("end_condition"), item["hold_action"],
        ) for item in normalized["chains"]]
        self.reset()
        return copy.deepcopy(self.config)

    def reset(self) -> HoldChainResult:
        self.state = IDLE
        self.chain: ChainSpec | None = None
        self.started_at = 0.0
        self.last_at = 0.0
        self.previous_signals: dict[str, bool] = {}
        self.last_transition: str | None = None
        return self.result()

    @staticmethod
    def _matches(signals: Mapping[str, bool], condition: str | None) -> bool:
        if not condition:
            return False
        condition = str(condition).strip()
        negate = condition.startswith("not:")
        name = condition[4:].strip() if negate else condition
        value = bool(signals.get(name, False))
        return not value if negate else value

    def _transition(self, state: str, now: float) -> str | None:
        if state == self.state:
            return None
        old = self.state
        self.state = state
        self.last_at = now
        return f"{old}->{state}"

    def result(self, transition: str | None = None) -> HoldChainResult:
        holding = self.enabled and self.state in {PREPARE, CHARGING}
        return HoldChainResult(
            state=self.state,
            hold=holding,
            hold_action=self.chain.hold_action if holding and self.chain else None,
            chain_id=self.chain.identifier if self.chain else None,
            transition=transition,
        )

    def status(self) -> dict:
        return {
            "enabled": self.enabled,
            "state": self.state,
            "chain_id": self.chain.identifier if self.chain else None,
            "started_at": self.started_at or None,
            "elapsed_s": max(0.0, self.last_at - self.started_at) if self.started_at else 0.0,
            "hold": self.result().hold,
            "last_transition": self.last_transition,
            "config": copy.deepcopy(self.config),
        }

    @property
    def managed_triggers(self) -> set[str]:
        """Starts/holds suppressed from legacy direct dispatch when enabled."""
        if not self.enabled:
            return set()
        return {name for spec in self.specs for name in (spec.start, spec.hold_action)}

    def update(self, now: float, signals: Mapping[str, bool] | None = None) -> HoldChainResult:
        now = float(now)
        if self.last_at:
            now = max(now, self.last_at)
        signals = {str(key): bool(value) for key, value in (signals or {}).items()}
        transition = None
        if not self.enabled:
            self.reset()
            self.previous_signals = signals
            self.last_at = now
            return self.result()

        start_edge = any(
            bool(signals.get(spec.start)) and not bool(self.previous_signals.get(spec.start, False))
            for spec in self.specs
        )
        if self.state == IDLE:
            if start_edge:
                self.chain = next(spec for spec in self.specs if signals.get(spec.start))
                self.started_at = now
                transition = self._transition(PREPARE, now)
        elif self.state == PREPARE and self.chain:
            continuation = self.chain.continue_condition or self.chain.sustain_condition
            if self._matches(signals, continuation):
                transition = self._transition(CHARGING, now)
            elif now - self.started_at >= self.prepare_s:
                transition = self._transition(CONTINUE, now)
        elif self.state == CONTINUE and self.chain:
            continuation = self.chain.continue_condition or self.chain.sustain_condition
            if self._matches(signals, continuation):
                transition = self._transition(CHARGING, now)
            elif now - self.started_at >= self.prepare_s + self.continue_window_s:
                transition = self._transition(RELEASED, now)
        elif self.state == CHARGING and self.chain:
            if (self.chain.end_condition and self._matches(signals, self.chain.end_condition)) or not self._matches(signals, self.chain.sustain_condition):
                transition = self._transition(RELEASED, now)
        elif self.state == RELEASED:
            if not any(bool(signals.get(spec.start)) for spec in self.specs):
                self.chain = None
                transition = self._transition(IDLE, now)

        self.previous_signals = signals
        self.last_at = now
        self.last_transition = transition
        return self.result(transition)


__all__ = [
    "CHARGING", "CONTINUE", "DEFAULT_ACTION_CHAIN", "HoldChain", "HoldChainResult",
    "IDLE", "PREPARE", "RELEASED", "normalize_config",
]
