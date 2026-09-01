"""Unit tests for per-link MLO statistics (debugfs + `iw dev link`)."""

from pathlib import Path

import pytest

from wlanpi_core.models.command_result import CommandResult
from wlanpi_core.models.runcommand_error import RunCommandError
from wlanpi_core.models.validation_error import ValidationError
from wlanpi_core.network import mlo_links
from wlanpi_core.network.mlo_links import _iw_link, get_mlo_links

MLD = "98:8f:00:ed:d0:30"

IW_LINK_MLO = (
    "Connected to 98:8f:00:ed:d0:30 (on wlan0)\n"
    "\tSSID: arooba-MLO\n"
    "\tLink 1 BSSID 98:8f:00:ed:d0:10\n"
    "\t\tfreq: 5640.0\n"
    "\tLink 0 BSSID 98:8f:00:ed:d0:30\n"
    "\t\tfreq: 5975.0\n"
    "MLD 98:8f:00:ed:d0:30 stats:\n"
    "\tRX: 19269 bytes (92 packets)\n"
    "\tTX: 1196 bytes (6 packets)\n"
    "\tsignal: -43 dBm\n"
)

IW_LINK_NOT_CONNECTED = "Not connected\n"

IW_LINK_SINGLE = (
    "Connected to 98:8f:00:ed:d0:30 (on wlan0)\n"
    "\tSSID: arooba-MLO\n"
    "\tfreq: 5975.0\n"
    "\tsignal: -43 dBm\n"
)


def _fake_iw(stdout: str, return_code: int = 0):
    calls: list = []

    def fake(cmd, namespace=None, no_output=False, raise_on_fail=True):
        calls.append((cmd, namespace))
        if return_code != 0:
            raise RunCommandError("command failed", return_code)
        return CommandResult(stdout, "", 0)

    fake.calls = calls
    return fake


def _build_sysfs(tmp_path: Path, iface: str = "wlan0", phy: str = "phy1") -> Path:
    root = tmp_path / "sys"
    phy_dir = root / iface / "phy80211"
    phy_dir.mkdir(parents=True)
    (phy_dir / "name").write_text(f"{phy}\n")
    return root


def _build_debugfs(
    tmp_path: Path,
    phy: str = "phy1",
    iface: str = "wlan0",
    peer: str = MLD,
    links: dict[int, dict[str, object]] | None = None,
) -> Path:
    root = tmp_path / "debugfs"
    link_tree = root / phy / f"netdev:{iface}" / "stations" / peer
    for link_id, files in (links or {}).items():
        link_dir = link_tree / f"link-{link_id}"
        link_dir.mkdir(parents=True)
        for name, value in files.items():
            (link_dir / name).write_text(str(value))
    return root


class TestIwLinkParser:
    def test_parses_two_link_mlo_inventory(self):
        parsed = _iw_link(IW_LINK_MLO)

        assert parsed["connected"] is True
        assert parsed["ssid"] == "arooba-MLO"
        assert parsed["mld_address"] == MLD
        assert parsed["signal_dbm"] == -43
        assert parsed["mld_stats"] == {
            "rx_bytes": 19269,
            "rx_packets": 92,
            "tx_bytes": 1196,
            "tx_packets": 6,
        }
        assert parsed["links"][0] == {
            "link_id": 0,
            "ap_link_bssid": MLD,
            "freq_mhz": 5975,
        }
        assert parsed["links"][1] == {
            "link_id": 1,
            "ap_link_bssid": "98:8f:00:ed:d0:10",
            "freq_mhz": 5640,
        }

    def test_not_connected_yields_no_links(self):
        parsed = _iw_link(IW_LINK_NOT_CONNECTED)

        assert parsed["connected"] is False
        assert parsed["links"] == {}

    def test_single_link_synthesizes_link_zero(self):
        parsed = _iw_link(IW_LINK_SINGLE)

        assert parsed["connected"] is True
        assert list(parsed["links"]) == [0]
        assert parsed["links"][0]["freq_mhz"] == 5975
        assert parsed["links"][0]["ap_link_bssid"] == MLD
        assert parsed["mld_stats"] is None


class TestGetMloLinks:
    def test_two_links_with_debugfs_counters(self, tmp_path, monkeypatch):
        sysfs = _build_sysfs(tmp_path)
        debugfs = _build_debugfs(
            tmp_path,
            links={
                0: {
                    "addr": "4A:C6:AA:CB:78:23",
                    "rx_fragments": "173948",
                    "rx_duplicates": "0",
                },
                1: {
                    "addr": "02:3d:b9:7f:a2:82",
                    "rx_fragments": "0",
                    "rx_duplicates": "12",
                },
            },
        )
        monkeypatch.setattr(mlo_links, "ns_exec", _fake_iw(IW_LINK_MLO))

        payload = get_mlo_links(
            "wlan0", "testns", sysfs_root=sysfs, debugfs_root=debugfs
        )

        assert payload["phy"] == "phy1"
        assert payload["namespace"] == "testns"
        assert payload["mac80211_debugfs"] is True
        assert payload["debugfs_reason"] is None
        assert payload["link_count"] == 2
        link0, link1 = payload["links"]
        assert link0["link_id"] == 0
        assert link0["freq_mhz"] == 5975
        assert link0["ap_link_bssid"] == MLD
        assert link0["local_address"] == "4a:c6:aa:cb:78:23"
        assert link0["rx_fragments"] == 173948
        assert link1["link_id"] == 1
        assert link1["freq_mhz"] == 5640
        assert link1["local_address"] == "02:3d:b9:7f:a2:82"
        assert link1["rx_fragments"] == 0
        assert link1["rx_duplicates"] == 12

    def test_iw_command_runs_in_resolved_namespace(self, tmp_path, monkeypatch):
        sysfs = _build_sysfs(tmp_path)
        debugfs = _build_debugfs(
            tmp_path,
            links={
                0: {
                    "addr": "02:00:00:00:00:01",
                    "rx_fragments": "5",
                    "rx_duplicates": "0",
                }
            },
        )
        fake = _fake_iw(IW_LINK_MLO)
        monkeypatch.setattr(mlo_links, "ns_exec", fake)

        get_mlo_links("wlan0", "testns", sysfs_root=sysfs, debugfs_root=debugfs)

        assert fake.calls[0][1] == "testns"

    def test_debugfs_not_mounted_still_reports_inventory(self, tmp_path, monkeypatch):
        sysfs = _build_sysfs(tmp_path)
        monkeypatch.setattr(mlo_links, "ns_exec", _fake_iw(IW_LINK_MLO))

        payload = get_mlo_links(
            "wlan0",
            None,
            sysfs_root=sysfs,
            debugfs_root=tmp_path / "debugfs",
        )

        assert payload["mac80211_debugfs"] is False
        assert payload["debugfs_reason"] == "debugfs_not_mounted"
        assert payload["link_count"] == 2
        assert payload["links"][0]["rx_fragments"] is None
        assert payload["links"][0]["freq_mhz"] == 5975

    def test_phy_dir_missing_reports_kernel_option(self, tmp_path, monkeypatch):
        sysfs = _build_sysfs(tmp_path)
        debugfs_root = tmp_path / "debugfs"
        debugfs_root.mkdir()
        monkeypatch.setattr(mlo_links, "ns_exec", _fake_iw(IW_LINK_MLO))

        payload = get_mlo_links(
            "wlan0", None, sysfs_root=sysfs, debugfs_root=debugfs_root
        )

        assert payload["mac80211_debugfs"] is False
        assert payload["debugfs_reason"] == "mac80211_debugfs_disabled"

    def test_station_entry_missing(self, tmp_path, monkeypatch):
        sysfs = _build_sysfs(tmp_path)
        debugfs = _build_debugfs(tmp_path)
        netdev = debugfs / "phy1" / "netdev:wlan0"
        netdev.mkdir(parents=True)
        monkeypatch.setattr(mlo_links, "ns_exec", _fake_iw(IW_LINK_MLO))

        payload = get_mlo_links("wlan0", None, sysfs_root=sysfs, debugfs_root=debugfs)

        assert payload["mac80211_debugfs"] is True
        assert payload["debugfs_reason"] == "station_entry_missing"
        assert payload["link_count"] == 2

    def test_not_connected_skips_debugfs_walk(self, tmp_path, monkeypatch):
        sysfs = _build_sysfs(tmp_path)
        debugfs = _build_debugfs(tmp_path)
        monkeypatch.setattr(mlo_links, "ns_exec", _fake_iw(IW_LINK_NOT_CONNECTED))

        payload = get_mlo_links("wlan0", None, sysfs_root=sysfs, debugfs_root=debugfs)

        assert payload["connected"] is False
        assert payload["links"] == []
        assert payload["link_count"] == 0
        assert payload["mld_address"] is None

    def test_iw_failure_maps_to_503(self, tmp_path, monkeypatch):
        sysfs = _build_sysfs(tmp_path)
        monkeypatch.setattr(mlo_links, "ns_exec", _fake_iw("", return_code=235))

        with pytest.raises(ValidationError) as excinfo:
            get_mlo_links(
                "wlan0", None, sysfs_root=sysfs, debugfs_root=tmp_path / "debugfs"
            )

        assert excinfo.value.status_code == 503

    def test_non_wireless_interface_maps_to_404(self, tmp_path, monkeypatch):
        monkeypatch.setattr(mlo_links, "ns_exec", _fake_iw(IW_LINK_MLO))

        with pytest.raises(ValidationError) as excinfo:
            get_mlo_links(
                "eth0", None, sysfs_root=tmp_path, debugfs_root=tmp_path / "debugfs"
            )

        assert excinfo.value.status_code == 404

    def test_invalid_interface_name_rejected(self, tmp_path):
        with pytest.raises(ValueError):
            get_mlo_links("wlan0; rm -rf", None, sysfs_root=tmp_path)
