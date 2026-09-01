"""MLD link-setup controls: model validation and supplicant conf generation."""

from pathlib import Path

import pytest
from pydantic import ValidationError as PydanticValidationError

from wlanpi_core.schemas.network.network import (
    MldOptions,
    NetSecurity,
    NetworkModeEnum,
    RootConfig,
    SecurityTypes,
)
from wlanpi_core.services.network_namespace_service import NetworkNamespaceService
from wlanpi_core.wpa.config import (
    generate_global_header,
    generate_network_block,
    write_wpa_config,
)


def _security(**kwargs) -> NetSecurity:
    base = dict(ssid="arooba-MLO", security=SecurityTypes.wpa3, psk="secret")
    base.update(kwargs)
    return NetSecurity(**base)


def _root(**kwargs) -> RootConfig:
    base = dict(
        mode=NetworkModeEnum.managed,
        iface_display_name="wlan0",
        phy="phy0",
        interface="wlan0",
        security=_security(),
    )
    base.update(kwargs)
    return RootConfig(**base)


class TestModels:
    def test_freq_list_validated(self):
        sec = _security(freq_list=[5640, 5975])
        assert sec.freq_list == [5640, 5975]

    @pytest.mark.parametrize(
        "freq_list",
        [[999], [2437, 0], [], [True]],
    )
    def test_freq_list_rejected(self, freq_list):
        with pytest.raises(PydanticValidationError):
            _security(freq_list=freq_list)

    def test_freq_list_none_allowed(self):
        assert _security(freq_list=None).freq_list is None

    def test_band_pref_literal(self):
        assert MldOptions(connect_band_pref=3).connect_band_pref == 3
        with pytest.raises(PydanticValidationError):
            MldOptions(connect_band_pref=4)

    def test_bssid_pref_normalized_lowercase(self):
        opts = MldOptions(connect_bssid_pref="98:8F:00:ED:D0:30")
        assert opts.connect_bssid_pref == "98:8f:00:ed:d0:30"

    def test_bssid_pref_rejects_garbage(self):
        with pytest.raises(PydanticValidationError):
            MldOptions(connect_bssid_pref="98-8f-00-ed-d0-30")

    def test_force_single_link_with_mlo_rejected(self):
        with pytest.raises(PydanticValidationError, match="conflicts"):
            _root(mlo=True, mld=MldOptions(force_single_link=True))

    def test_force_single_link_without_mlo_accepted(self):
        root = _root(mlo=False, mld=MldOptions(force_single_link=True))
        assert root.mld.force_single_link is True


class TestGlobalHeader:
    def test_no_mld_by_default(self):
        header = generate_global_header()
        assert "mld_" not in header

    def test_mld_options_emit_global_fields(self):
        header = generate_global_header(
            mld={
                "force_single_link": True,
                "connect_band_pref": 3,
                "connect_bssid_pref": "98:8f:00:ed:d0:30",
            }
        )
        assert "mld_force_single_link=1" in header
        assert "mld_connect_band_pref=3" in header
        assert "mld_connect_bssid_pref=98:8f:00:ed:d0:30" in header

    def test_unset_mld_options_emit_nothing(self):
        header = generate_global_header(
            mld={
                "force_single_link": False,
                "connect_band_pref": None,
                "connect_bssid_pref": None,
            }
        )
        assert "mld_" not in header


class TestNetworkBlock:
    def test_freq_list_written_inside_block(self):
        block = generate_network_block(
            _root(mlo=True, security=_security(freq_list=[5640, 5975]))
        )
        assert "    freq_list=5640 5975" in block
        assert "mlo=1" in block
        assert "mld_" not in block

    def test_no_freq_list_line_when_absent(self):
        block = generate_network_block(_root())
        assert "freq_list" not in block


class TestWriteWpaConfig:
    def test_mld_fields_land_in_header_not_blocks(self, tmp_path: Path):
        cfg = _root(
            security=_security(freq_list=[5640, 5975]),
            mld=MldOptions(
                force_single_link=True,
                connect_band_pref=2,
                connect_bssid_pref="98:8f:00:ed:d0:10",
            ),
        )
        write_wpa_config(cfg, tmp_path, {"ctrl_interface": "/run/wpa_supplicant"})
        text = (tmp_path / "wlan0.conf").read_text()

        network_start = text.index("network={")
        header = text[:network_start]
        block = text[network_start:]

        assert "mld_force_single_link=1" in header
        assert "mld_connect_band_pref=2" in header
        assert "mld_connect_bssid_pref=98:8f:00:ed:d0:10" in header
        assert "mld_" not in block
        assert "freq_list=5640 5975" in block

    def test_reload_drops_stale_mld_fields(self, tmp_path: Path):
        cfg = _root(mld=MldOptions(force_single_link=True))
        write_wpa_config(cfg, tmp_path, {"ctrl_interface": "/run/wpa_supplicant"})
        assert "mld_force_single_link=1" in (tmp_path / "wlan0.conf").read_text()

        write_wpa_config(_root(), tmp_path, {"ctrl_interface": "/run/wpa_supplicant"})
        assert "mld_" not in (tmp_path / "wlan0.conf").read_text()


class TestServiceValidation:
    def test_conflict_caught_even_when_bypassing_pydantic(self):
        cfg = RootConfig.model_construct(
            mode=NetworkModeEnum.managed,
            iface_display_name="wlan0",
            phy="phy0",
            interface="wlan0",
            default_route=False,
            autostart_app=None,
            security=None,
            mlo=True,
            mld={"force_single_link": True},
        )
        valid, error = NetworkNamespaceService()._validate_config(cfg)
        assert valid is False
        assert "mld.force_single_link conflicts with mlo=true" in error

    def test_force_single_link_without_mlo_passes(self):
        cfg = RootConfig.model_construct(
            mode=NetworkModeEnum.managed,
            iface_display_name="wlan0",
            phy="phy0",
            interface="wlan0",
            default_route=False,
            autostart_app=None,
            security=None,
            mlo=False,
            mld={"force_single_link": True},
        )
        valid, error = NetworkNamespaceService()._validate_config(cfg)
        assert valid is True
        assert error == ""
