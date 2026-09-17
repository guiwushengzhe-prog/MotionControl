"""Which addresses this PC can be reached at, and over what kind of link.

The phone has to be told where the PC is, and that answer used to come from
resolving the machine's own hostname.  Windows answers that question with
whichever addresses its resolver feels like: measured on this machine, with a
phone tethered over USB it returned *only* the USB address and hid the Wi-Fi
one, and it never saw the VMware adapter at all.  A missing address and an
unreachable one both end the same way -- the phone says 连不上.

GetAdaptersAddresses is what Windows itself uses, so it sees every adapter,
and it carries the adapter description.  That description is the only way to
tell a USB tether apart from Wi-Fi, and both apart from a virtual adapter no
phone can ever reach.

Ordering is not cosmetic.  Measured on this pair of devices, phone to PC:

    USB tether   4.4 ms average, 3.9-5.1 ms
    Wi-Fi        8.3 ms average, 7.0-9.7 ms

Half the latency and half the jitter, so when a cable is plugged in it should
win without the player having to know it exists.
"""

from __future__ import annotations

import ctypes
import ipaddress
import socket
import sys

KIND_USB = "usb"
KIND_LAN = "lan"

# Best first.  Anything not listed sorts last.
_KIND_ORDER = {KIND_USB: 0, KIND_LAN: 1}

# Matched against the adapter description, lowercased.  USB tethering shows up
# as RNDIS on Windows whatever the phone calls it; NCM is the newer protocol
# some Android builds negotiate instead.
_USB_MARKERS = ("remote ndis", "rndis", "usb ethernet", "ncm", "usb-c lan",
                "android", "usb network")

# Adapters a phone can never reach.  Advertising one wastes the phone's time
# on a probe that can only ever time out.
_VIRTUAL_MARKERS = ("vmware", "virtualbox", "hyper-v", "loopback", "tap-",
                    "tunnel", "wan miniport", "bluetooth", "wintun", "pseudo",
                    "wireguard", "zerotier", "tailscale", "npcap", "teredo")

_IF_OPER_STATUS_UP = 1
_ERROR_BUFFER_OVERFLOW = 111


class _SocketAddress(ctypes.Structure):
    _fields_ = [("lpSockaddr", ctypes.c_void_p), ("iSockaddrLength", ctypes.c_int)]


class _UnicastAddress(ctypes.Structure):
    pass


# Length and Flags together occupy the ULONGLONG alignment member the header
# declares, so the pointer that follows lands where Windows put it.
_UnicastAddress._fields_ = [
    ("Length", ctypes.c_ulong),
    ("Flags", ctypes.c_ulong),
    ("Next", ctypes.POINTER(_UnicastAddress)),
    ("Address", _SocketAddress),
    ("PrefixOrigin", ctypes.c_int),
    ("SuffixOrigin", ctypes.c_int),
    ("DadState", ctypes.c_int),
    ("ValidLifetime", ctypes.c_ulong),
    ("PreferredLifetime", ctypes.c_ulong),
    ("LeaseLifetime", ctypes.c_ulong),
    ("OnLinkPrefixLength", ctypes.c_ubyte),
]


class _AdapterAddresses(ctypes.Structure):
    pass


# Declared only as far as OperStatus: the buffer Windows fills is larger than
# this and the remaining fields are not read, so stopping early is safe and
# keeps the layout that does matter short enough to check by eye.
_AdapterAddresses._fields_ = [
    ("Length", ctypes.c_ulong),
    ("IfIndex", ctypes.c_ulong),
    ("Next", ctypes.POINTER(_AdapterAddresses)),
    ("AdapterName", ctypes.c_char_p),
    ("FirstUnicastAddress", ctypes.POINTER(_UnicastAddress)),
    ("FirstAnycastAddress", ctypes.c_void_p),
    ("FirstMulticastAddress", ctypes.c_void_p),
    ("FirstDnsServerAddress", ctypes.c_void_p),
    ("DnsSuffix", ctypes.c_wchar_p),
    ("Description", ctypes.c_wchar_p),
    ("FriendlyName", ctypes.c_wchar_p),
    ("PhysicalAddress", ctypes.c_ubyte * 8),
    ("PhysicalAddressLength", ctypes.c_ulong),
    ("Flags", ctypes.c_ulong),
    ("Mtu", ctypes.c_ulong),
    ("IfType", ctypes.c_ulong),
    ("OperStatus", ctypes.c_int),
]


def classify(description: str) -> str | None:
    """The link kind for an adapter description, or None to skip it."""
    lowered = (description or "").lower()
    if any(marker in lowered for marker in _VIRTUAL_MARKERS):
        return None
    if any(marker in lowered for marker in _USB_MARKERS):
        return KIND_USB
    return KIND_LAN


def _usable(address: str) -> bool:
    try:
        parsed = ipaddress.ip_address(address)
    except ValueError:
        return False
    return (parsed.version == 4 and parsed.is_private
            and not parsed.is_loopback and not parsed.is_link_local)


def _windows_endpoints() -> list[dict]:
    GAA_FLAG_SKIP_ANYCAST = 0x2
    GAA_FLAG_SKIP_MULTICAST = 0x4
    GAA_FLAG_SKIP_DNS_SERVER = 0x8
    flags = GAA_FLAG_SKIP_ANYCAST | GAA_FLAG_SKIP_MULTICAST | GAA_FLAG_SKIP_DNS_SERVER

    size = ctypes.c_ulong(15000)
    buffer = ctypes.create_string_buffer(size.value)
    call = ctypes.windll.iphlpapi.GetAdaptersAddresses
    result = call(socket.AF_INET, flags, None, buffer, ctypes.byref(size))
    if result == _ERROR_BUFFER_OVERFLOW:
        buffer = ctypes.create_string_buffer(size.value)
        result = call(socket.AF_INET, flags, None, buffer, ctypes.byref(size))
    if result != 0:
        return []

    found: list[dict] = []
    adapter = ctypes.cast(buffer, ctypes.POINTER(_AdapterAddresses))
    while adapter:
        entry = adapter.contents
        if entry.OperStatus == _IF_OPER_STATUS_UP:
            kind = classify(entry.Description or "")
            if kind is not None:
                unicast = entry.FirstUnicastAddress
                while unicast:
                    raw = unicast.contents.Address
                    if raw.lpSockaddr:
                        packed = ctypes.string_at(raw.lpSockaddr, 8)
                        if int.from_bytes(packed[0:2], "little") == socket.AF_INET:
                            address = socket.inet_ntoa(packed[4:8])
                            if _usable(address):
                                found.append({
                                    "address": address,
                                    "kind": kind,
                                    "adapter": entry.FriendlyName or entry.Description or "",
                                })
                    unicast = unicast.contents.Next
        adapter = entry.Next
    return found


def _hostname_endpoints() -> list[dict]:
    """The old hostname lookup, kept for platforms without the Windows call.

    It cannot tell one kind of link from another, so everything it finds is
    reported as an ordinary LAN address.
    """
    values: set[str] = set()
    try:
        for item in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            values.add(str(item[4][0]))
    except OSError:
        pass
    try:
        _, _, addresses = socket.gethostbyname_ex(socket.gethostname())
        values.update(str(address) for address in addresses)
    except OSError:
        pass
    return [{"address": address, "kind": KIND_LAN, "adapter": ""}
            for address in sorted(values) if _usable(address)]


def endpoints() -> list[dict]:
    """Every address a phone could reach this PC at, best link first."""
    found: list[dict] = []
    if sys.platform == "win32":
        try:
            found = _windows_endpoints()
        except OSError:
            found = []
    if not found:
        found = _hostname_endpoints()
    seen: set[str] = set()
    unique: list[dict] = []
    for item in sorted(found, key=lambda item: (_KIND_ORDER.get(item["kind"], 9),
                                                item["address"])):
        if item["address"] in seen:
            continue
        seen.add(item["address"])
        unique.append(item)
    return unique


def addresses() -> list[str]:
    """Just the addresses, best link first."""
    return [item["address"] for item in endpoints()]


# Somewhere off-link.  No packet is ever sent -- connecting a UDP socket only
# asks the routing table which local address it would use to get there -- so
# this costs nothing and works with the internet down.
_OFF_LINK_PROBE = ("223.5.5.5", 53)


def internet_egress() -> str | None:
    """The local address this PC would use to reach the internet."""
    probe = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        probe.connect(_OFF_LINK_PROBE)
        return str(probe.getsockname()[0])
    except OSError:
        return None
    finally:
        probe.close()


def tether_carries_internet() -> dict | None:
    """The USB endpoint carrying this PC's internet, if one is.

    Windows picks the default route partly by link speed, and a USB tether
    reports a higher speed than Wi-Fi -- measured here, 426 Mbps against 287.
    So plugging the cable in to get a fast link to the phone silently moves
    the whole machine's internet onto the phone's mobile data.

    Asking the routing table what it would actually do beats reading metrics
    and predicting: when Wi-Fi has no internet at all Windows moves everything
    onto the tether whatever the metrics say, and that is worth reporting
    honestly rather than "fixing" with a setting that cannot help.
    """
    egress = internet_egress()
    if egress is None:
        return None
    for item in endpoints():
        if item["kind"] == KIND_USB and item["address"] == egress:
            return item
    return None
