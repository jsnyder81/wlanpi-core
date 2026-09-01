"""Per-link MLO statistics from mac80211 debugfs and ``iw dev link``.

mac80211 keeps a ``link-N`` directory per setup link under the peer station
entry in debugfs. ``rx_fragments`` there is the only host-side counter that
resolves individual links of a Wi-Fi 7 MLD association (RX direction only;
there is no per-link TX counter). ``iw dev {iface} link`` supplies the link
inventory: link IDs, AP link BSSIDs and frequencies.
"""

from __future__ import annotations

import logging
import re
from pathlib import Path
from typing import Any, Optional

from wlanpi_core.constants import IW_FILE
from wlanpi_core.models.runcommand_error import RunCommandError
from wlanpi_core.models.validation_error import ValidationError
from wlanpi_core.utils.namespace_execution import ns_exec
from wlanpi_core.utils.validation import validate_interface_name

log = logging.getLogger(__name__)

SYSFS_NET_ROOT = Path("/sys/class/net")
DEBUGFS_IEEE80211_ROOT = Path("/sys/kernel/debug/ieee80211")

_IW_CONNECTED = re.compile(
    r"^Connected to\s+(?P<bssid>[0-9a-fA-F:]{17})\s+\(on\s+(?P<iface>\S+)\)"
)
_IW_SSID = re.compile(r"^\s+SSID:\s+(?P<ssid>.+?)\s*$")
_IW_LINK = re.compile(
    r"^\s+Link\s+(?P<link_id>\d+)\s+BSSID\s+(?P<bssid>[0-9a-fA-F:]{17})"
)
_IW_FREQ = re.compile(r"^\s+freq:\s+(?P<freq>[\d.]+)")
_IW_MLD_STATS = re.compile(r"^MLD\s+(?P<mld>[0-9a-fA-F:]{17})\s+stats:", re.IGNORECASE)
_IW_MLD_RX = re.compile(
    r"^\s+RX:\s+(?P<bytes>\d+)\s+bytes\s+\((?P<packets>\d+)\s+packets\)"
)
_IW_MLD_TX = re.compile(
    r"^\s+TX:\s+(?P<bytes>\d+)\s+bytes\s+\((?P<packets>\d+)\s+packets\)"
)
_IW_SIGNAL = re.compile(r"^\s+signal:\s*(?P<signal>-?\d+)")
_LINK_DIR = re.compile(r"^link-(?P<link_id>\d+)$")


def _to_freq(value: str) -> Optional[int]:
    try:
        return int(float(value))
    except ValueError:
        return None


def _to_int(value: str) -> Optional[int]:
    try:
        return int(value)
    except ValueError:
        return None


def _iw_link(output: str) -> dict[str, Any]:
    """Parse ``iw dev {iface} link`` output into a link inventory."""
    parsed: dict[str, Any] = {
        "connected": False,
        "ssid": None,
        "mld_address": None,
        "signal_dbm": None,
        "mld_stats": None,
        "links": {},
    }
    in_mld_stats = False
    pending_link: Optional[int] = None
    default_freq: Optional[int] = None

    for line in output.splitlines():
        if match := _IW_CONNECTED.match(line):
            parsed["connected"] = True
            parsed["mld_address"] = match.group("bssid").lower()
            continue
        if not parsed["connected"]:
            continue
        if match := _IW_LINK.match(line):
            link_id = int(match.group("link_id"))
            parsed["links"][link_id] = {
                "link_id": link_id,
                "ap_link_bssid": match.group("bssid").lower(),
                "freq_mhz": None,
            }
            pending_link = link_id
            in_mld_stats = False
            continue
        if match := _IW_MLD_STATS.match(line):
            parsed["mld_address"] = match.group("mld").lower()
            parsed["mld_stats"] = {
                "rx_bytes": None,
                "rx_packets": None,
                "tx_bytes": None,
                "tx_packets": None,
            }
            in_mld_stats = True
            continue
        if match := _IW_SSID.match(line):
            parsed["ssid"] = match.group("ssid")
            continue
        if match := _IW_FREQ.match(line):
            freq = _to_freq(match.group("freq"))
            if pending_link is not None and pending_link in parsed["links"]:
                parsed["links"][pending_link]["freq_mhz"] = freq
            elif default_freq is None:
                default_freq = freq
            pending_link = None
            continue
        if in_mld_stats and parsed["mld_stats"] is not None:
            if match := _IW_MLD_RX.match(line):
                parsed["mld_stats"]["rx_bytes"] = _to_int(match.group("bytes"))
                parsed["mld_stats"]["rx_packets"] = _to_int(match.group("packets"))
                continue
            if match := _IW_MLD_TX.match(line):
                parsed["mld_stats"]["tx_bytes"] = _to_int(match.group("bytes"))
                parsed["mld_stats"]["tx_packets"] = _to_int(match.group("packets"))
                continue
        if match := _IW_SIGNAL.match(line):
            parsed["signal_dbm"] = _to_int(match.group("signal"))

    if parsed["connected"] and not parsed["links"] and default_freq is not None:
        parsed["links"][0] = {
            "link_id": 0,
            "ap_link_bssid": parsed["mld_address"],
            "freq_mhz": default_freq,
        }

    return parsed


def _resolve_phy(iface: str, sysfs_root: Path) -> str:
    phy_file = sysfs_root / iface / "phy80211" / "name"
    try:
        phy = phy_file.read_text().strip()
    except OSError as exc:
        log.debug("No phy80211 name for %s: %r", iface, exc)
        raise ValidationError(
            f"Interface {iface} is not a wireless interface", status_code=404
        ) from exc
    if not phy:
        raise ValidationError(
            f"Interface {iface} has no phy association", status_code=404
        )
    return phy


def _iw_link_status(iface: str, namespace: Optional[str]) -> str:
    try:
        return ns_exec([IW_FILE, "dev", iface, "link"], namespace=namespace).stdout
    except (RunCommandError, FileNotFoundError) as exc:
        raise ValidationError(
            f"Unable to read link status for {iface}: {exc}", status_code=503
        ) from exc


def _read_link_dir(link_dir: Path) -> dict[str, Any]:
    entry: dict[str, Any] = {}
    try:
        entry["local_address"] = link_dir.joinpath("addr").read_text().strip().lower()
    except OSError:
        entry["local_address"] = None
    try:
        entry["rx_fragments"] = int(
            link_dir.joinpath("rx_fragments").read_text().strip()
        )
    except (OSError, ValueError):
        entry["rx_fragments"] = None
    try:
        entry["rx_duplicates"] = int(
            link_dir.joinpath("rx_duplicates").read_text().strip()
        )
    except (OSError, ValueError):
        entry["rx_duplicates"] = None
    return entry


def _debugfs_links(
    phy: str, iface: str, mld_address: Optional[str], debugfs_root: Path
) -> tuple[bool, Optional[str], dict[int, dict[str, Any]]]:
    """Return (debugfs_usable, reason, {link_id: counters})."""
    netdev_dir = debugfs_root / phy / f"netdev:{iface}"
    if not debugfs_root.is_dir():
        return False, "debugfs_not_mounted", {}
    if not netdev_dir.is_dir():
        return False, "mac80211_debugfs_disabled", {}

    stations: list[Path] = []
    try:
        stations = sorted((netdev_dir / "stations").glob("*"))
    except OSError as exc:
        log.debug("Cannot list debugfs stations: %r", exc)
    if not stations:
        return True, "station_entry_missing", {}

    peer: Optional[Path] = None
    if mld_address:
        candidate = netdev_dir / "stations" / mld_address
        if candidate.is_dir():
            peer = candidate
    if peer is None:
        peer = stations[0]

    link_dirs: dict[int, dict[str, Any]] = {}
    try:
        entries = list(peer.iterdir())
    except OSError as exc:
        log.debug("Cannot read debugfs station entry %s: %r", peer, exc)
        return True, "station_entry_unreadable", link_dirs

    for entry in entries:
        match = _LINK_DIR.match(entry.name)
        if match and entry.is_dir():
            link_dirs[int(match.group("link_id"))] = _read_link_dir(entry)
    return True, None, link_dirs


def get_mlo_links(
    iface: str,
    namespace: Optional[str] = None,
    *,
    sysfs_root: Path = SYSFS_NET_ROOT,
    debugfs_root: Path = DEBUGFS_IEEE80211_ROOT,
) -> dict[str, Any]:
    """Return per-link MLO statistics for a station-mode interface."""
    iface = validate_interface_name(iface)
    log.debug("get_mlo_links iface=%s namespace=%r", iface, namespace)

    phy = _resolve_phy(iface, sysfs_root)
    parsed = _iw_link(_iw_link_status(iface, namespace))

    payload: dict[str, Any] = {
        "interface": iface,
        "namespace": namespace,
        "phy": phy,
        "mac80211_debugfs": False,
        "debugfs_reason": None,
        "connected": parsed["connected"],
        "ssid": parsed["ssid"],
        "mld_address": parsed["mld_address"],
        "signal_dbm": parsed["signal_dbm"],
        "mld_stats": parsed["mld_stats"],
        "links": [],
        "link_count": 0,
    }

    if not parsed["connected"]:
        return payload

    counters: dict[int, dict[str, Any]] = {}
    usable, reason, counters = _debugfs_links(
        phy, iface, parsed["mld_address"], debugfs_root
    )
    payload["mac80211_debugfs"] = usable
    payload["debugfs_reason"] = reason

    link_ids = set(parsed["links"]) | set(counters)
    links: list[dict[str, Any]] = []
    for link_id in sorted(link_ids):
        iw_entry = parsed["links"].get(link_id)
        counters_entry = counters.get(link_id)
        links.append(
            {
                "link_id": link_id,
                "freq_mhz": iw_entry["freq_mhz"] if iw_entry else None,
                "ap_link_bssid": iw_entry["ap_link_bssid"] if iw_entry else None,
                "local_address": (
                    counters_entry["local_address"] if counters_entry else None
                ),
                "rx_fragments": (
                    counters_entry["rx_fragments"] if counters_entry else None
                ),
                "rx_duplicates": (
                    counters_entry["rx_duplicates"] if counters_entry else None
                ),
            }
        )

    payload["links"] = links
    payload["link_count"] = len(links)
    return payload
