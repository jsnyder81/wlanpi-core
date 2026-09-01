"""Unit tests for effective MLO config readback from the supplicant conf."""

from pathlib import Path

import pytest

from wlanpi_core.models.command_result import CommandResult
from wlanpi_core.wpa import mlo_config
from wlanpi_core.wpa.mlo_config import get_mlo_effective_config, parse_mlo_conf

CONF_WITH_MLD = """ctrl_interface=/run/wpa_supplicant
update_config=1
sae_pwe=2
mld_force_single_link=1
mld_connect_band_pref=3
mld_connect_bssid_pref=98:8F:00:ED:D0:30
network={
    ssid="arooba-MLO"
    priority=2
    psk="topsecret"
    key_mgmt=SAE
    ieee80211w=2
    freq_list=5640 5975
    mlo=1
}
network={
    ssid="other"
    priority=1
    key_mgmt=NONE
}
"""

CONF_WITHOUT_MLD = """ctrl_interface=/run/wpa_supplicant
update_config=1
sae_pwe=2
network={
    ssid="plain"
    priority=1
    key_mgmt=NONE
}
"""


def _fake_pgrep(return_code: int, calls: list):
    def fake(cmd, namespace=None, no_output=False, raise_on_fail=True):
        calls.append((cmd, namespace))
        return CommandResult("", "", return_code)

    return fake


class TestParseMloConf:
    def test_extracts_global_and_network_options(self):
        parsed = parse_mlo_conf(CONF_WITH_MLD)

        assert parsed["mld"] == {
            "force_single_link": True,
            "connect_band_pref": 3,
            "connect_bssid_pref": "98:8f:00:ed:d0:30",
        }
        assert parsed["networks"][0] == {
            "ssid": "arooba-MLO",
            "freq_list": [5640, 5975],
            "mlo": True,
        }
        assert parsed["networks"][1] == {
            "ssid": "other",
            "freq_list": [],
            "mlo": False,
        }

    def test_defaults_when_no_mld_fields(self):
        parsed = parse_mlo_conf(CONF_WITHOUT_MLD)

        assert parsed["mld"] == {
            "force_single_link": False,
            "connect_band_pref": None,
            "connect_bssid_pref": None,
        }
        assert parsed["networks"] == [{"ssid": "plain", "freq_list": [], "mlo": False}]

    def test_unterminated_block_is_still_reported(self):
        parsed = parse_mlo_conf('network={\n    ssid="half"\n    freq_list=2437\n')

        assert parsed["networks"] == [
            {"ssid": "half", "freq_list": [2437], "mlo": False}
        ]


class TestGetMloEffectiveConfig:
    def test_reads_conf_file_and_running_supplicant(self, tmp_path: Path, monkeypatch):
        (tmp_path / "wlan0.conf").write_text(CONF_WITH_MLD)
        calls: list = []
        monkeypatch.setattr(mlo_config, "ns_exec", _fake_pgrep(0, calls))

        payload = get_mlo_effective_config("wlan0", "testns", config_dir=tmp_path)

        assert payload["interface"] == "wlan0"
        assert payload["namespace"] == "testns"
        assert payload["config_path"] == str(tmp_path / "wlan0.conf")
        assert payload["file_exists"] is True
        assert payload["supplicant_running"] is True
        assert payload["mld"]["force_single_link"] is True
        assert payload["networks"][0]["freq_list"] == [5640, 5975]
        assert calls[0][1] == "testns"
        assert calls[0][0][0] == "pgrep"

    def test_missing_file_yields_defaults(self, tmp_path: Path, monkeypatch):
        calls: list = []
        monkeypatch.setattr(mlo_config, "ns_exec", _fake_pgrep(1, calls))

        payload = get_mlo_effective_config("wlan0", None, config_dir=tmp_path)

        assert payload["file_exists"] is False
        assert payload["supplicant_running"] is False
        assert payload["mld"] == {
            "force_single_link": False,
            "connect_band_pref": None,
            "connect_bssid_pref": None,
        }
        assert payload["networks"] == []

    def test_pgrep_failure_is_reported_as_not_running(
        self, tmp_path: Path, monkeypatch
    ):
        def boom(cmd, namespace=None, no_output=False, raise_on_fail=True):
            raise FileNotFoundError("pgrep missing")

        monkeypatch.setattr(mlo_config, "ns_exec", boom)

        payload = get_mlo_effective_config("wlan0", None, config_dir=tmp_path)

        assert payload["supplicant_running"] is False

    def test_invalid_interface_name_rejected(self, tmp_path: Path):
        with pytest.raises(ValueError):
            get_mlo_effective_config("-iface", None, config_dir=tmp_path)
