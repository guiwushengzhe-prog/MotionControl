"""Which address the phone is told to use, and whether the cable took the internet.

These are the two ways the wired link goes wrong in practice.  Advertising an
address no phone can reach costs the player a timeout and a 连不上 with no
explanation; leaving a USB tether carrying the machine's internet costs them
mobile data with no warning at all.
"""

from __future__ import annotations

import pytest

import local_endpoints as le


# --- telling one kind of link from another --------------------------------


@pytest.mark.parametrize("description", [
    "Remote NDIS Compatible Device",
    "Samsung Mobile USB Ethernet Adapter",
    "Realtek USB NCM Device",
    "rndis host",
])
def test_usb_tethers_are_recognised(description):
    assert le.classify(description) == le.KIND_USB


@pytest.mark.parametrize("description", [
    "VMware Virtual Ethernet Adapter for VMnet8",
    "Software Loopback Interface 1",
    "Bluetooth Device (Personal Area Network)",
    "Hyper-V Virtual Ethernet Adapter",
    "TAP-Windows Adapter V9",
    "WAN Miniport (IP)",
])
def test_adapters_no_phone_can_reach_are_dropped(description):
    """报给手机只会让它白等一次超时，然后说连不上。"""
    assert le.classify(description) is None


def test_anything_else_counts_as_an_ordinary_network():
    assert le.classify("Intel(R) Wi-Fi 6E AX211 160MHz #2") == le.KIND_LAN
    assert le.classify("Realtek PCIe GbE Family Controller") == le.KIND_LAN


def test_a_virtual_adapter_wins_over_a_usb_looking_name():
    """先排除虚拟网卡再认 USB：VMware 那张叫什么都连不上。"""
    assert le.classify("VMware USB Ethernet Adapter") is None


# --- which addresses are worth advertising --------------------------------


@pytest.mark.parametrize("address", ["10.0.0.5", "192.168.1.20", "172.16.3.4"])
def test_private_addresses_are_usable(address):
    assert le._usable(address) is True


@pytest.mark.parametrize("address", [
    "127.0.0.1",        # 回环，手机连不到
    "169.254.51.36",    # 没拿到 DHCP 的自说自话地址
    "8.8.8.8",          # 公网，不该出现在这里
    "not-an-address",
])
def test_unreachable_addresses_are_not_advertised(address):
    assert le._usable(address) is False


# --- ordering -------------------------------------------------------------


def test_the_cable_is_offered_before_wifi(monkeypatch):
    """实测数据线 4.4ms、WiFi 8.3ms，插着线就该用线，不该让人自己选。"""
    monkeypatch.setattr(le, "_windows_endpoints", lambda: [
        {"address": "10.245.40.245", "kind": le.KIND_LAN, "adapter": "WLAN 2"},
        {"address": "10.119.231.59", "kind": le.KIND_USB, "adapter": "以太网 3"},
    ])
    monkeypatch.setattr(le.sys, "platform", "win32")
    assert le.addresses() == ["10.119.231.59", "10.245.40.245"]


def test_the_same_address_is_only_offered_once(monkeypatch):
    monkeypatch.setattr(le, "_windows_endpoints", lambda: [
        {"address": "10.0.0.5", "kind": le.KIND_LAN, "adapter": "a"},
        {"address": "10.0.0.5", "kind": le.KIND_LAN, "adapter": "b"},
    ])
    monkeypatch.setattr(le.sys, "platform", "win32")
    assert le.addresses() == ["10.0.0.5"]


def test_the_hostname_lookup_still_answers_when_windows_finds_nothing(monkeypatch):
    monkeypatch.setattr(le, "_windows_endpoints", lambda: [])
    monkeypatch.setattr(le, "_hostname_endpoints",
                        lambda: [{"address": "192.168.1.7", "kind": le.KIND_LAN, "adapter": ""}])
    monkeypatch.setattr(le.sys, "platform", "win32")
    assert le.addresses() == ["192.168.1.7"]


# --- the cable quietly taking the internet --------------------------------


def _both_links(monkeypatch, egress):
    monkeypatch.setattr(le, "endpoints", lambda: [
        {"address": "10.119.231.59", "kind": le.KIND_USB, "adapter": "以太网 3"},
        {"address": "10.245.40.245", "kind": le.KIND_LAN, "adapter": "WLAN 2"},
    ])
    monkeypatch.setattr(le, "internet_egress", lambda: egress)


def test_internet_going_out_the_cable_is_reported(monkeypatch):
    """Windows 按链路速率挑默认路由，USB 报得比 WiFi 快，于是整机上网悄悄走手机流量。"""
    _both_links(monkeypatch, "10.119.231.59")
    carrier = le.tether_carries_internet()
    assert carrier is not None
    assert carrier["address"] == "10.119.231.59"


def test_internet_on_wifi_is_not_reported(monkeypatch):
    _both_links(monkeypatch, "10.245.40.245")
    assert le.tether_carries_internet() is None


def test_no_route_at_all_is_not_reported_as_the_cable(monkeypatch):
    _both_links(monkeypatch, None)
    assert le.tether_carries_internet() is None
