import json

from motioncontrol.connection_code import connection_code
from motioncontrol.windows_firewall import WindowsFirewall


def test_code_contains_current_port_identity_and_no_credentials():
    data = connection_code("0123456789ab", "电脑", [
        {"host": "192.168.1.2", "port": 9876, "kind": "lan"},
        {"host": "192.168.1.2", "port": 9876, "kind": "lan"},
    ], 9876)
    payload = json.loads(data["text"])
    assert payload["instance"] == "0123456789ab"
    assert payload["candidates"] == [{"host": "127.0.0.1", "port": 9876, "kind": "usb"},
                                    {"host": "192.168.1.2", "port": 9876, "kind": "lan"}]
    assert set(payload) == {"type", "version", "instance", "name", "candidates"}
    assert "<svg" in data["svg"]


def test_firewall_reuses_existing_rules_without_permission_prompt(tmp_path, monkeypatch):
    firewall = WindowsFirewall(tmp_path / "firewall.json")
    monkeypatch.setattr(firewall, "_windows", True)
    calls = []
    def run(mode):
        calls.append(mode)
        return {"state": "ready", "message": ""}
    monkeypatch.setattr(firewall, "_run", run)
    firewall._ensure(False)
    assert calls == ["Check"]
    assert firewall.status()["state"] == "ready"
    assert not firewall.preference.exists()


def test_declined_firewall_prompt_does_not_repeat_until_manual_retry(tmp_path, monkeypatch):
    firewall = WindowsFirewall(tmp_path / "firewall.json")
    monkeypatch.setattr(firewall, "_windows", True)
    calls = []
    def run(mode):
        calls.append(mode)
        return {"state": "missing", "message": "未授权"}
    monkeypatch.setattr(firewall, "_run", run)
    firewall._ensure(False)
    firewall._ensure(False)
    assert calls == ["Check", "Ensure", "Check"]
    firewall._ensure(True)
    assert calls == ["Check", "Ensure", "Check", "Check", "Ensure"]


def test_configuration_failure_is_visible(tmp_path, monkeypatch):
    firewall = WindowsFirewall(tmp_path / "firewall.json")
    monkeypatch.setattr(firewall, "_windows", True)
    def broken(mode):
        raise RuntimeError("检查失败")
    monkeypatch.setattr(firewall, "_run", broken)
    firewall._ensure(False)
    assert firewall.status()["state"] == "error"
