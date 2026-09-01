"""Readback of the MLO link options in the effective supplicant config.

``write_wpa_config`` rewrites the per-interface conf file on every profile
activation, so the file on disk is what the running (or next-started)
wpa_supplicant for that interface will honor. A stale config is a known
failure mode when measuring link sets, so expose it for verification.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any, Optional

from wlanpi_core.constants import DEFAULT_CONFIG_DIR
from wlanpi_core.models.runcommand_error import RunCommandError
from wlanpi_core.models.validation_error import ValidationError
from wlanpi_core.utils.namespace_execution import ns_exec
from wlanpi_core.utils.validation import validate_interface_name

log = logging.getLogger(__name__)

_GLOBAL_KEYS = {
    "mld_force_single_link": "force_single_link",
    "mld_connect_band_pref": "connect_band_pref",
    "mld_connect_bssid_pref": "connect_bssid_pref",
}


def _unquote_wpa_value(value: str) -> str:
    if len(value) >= 2 and value[0] == '"' and value[-1] == '"':
        return value[1:-1].replace('\\"', '"').replace("\\\\", "\\")
    return value


def _empty_mld() -> dict[str, Any]:
    return {
        "force_single_link": False,
        "connect_band_pref": None,
        "connect_bssid_pref": None,
    }


def _empty_network() -> dict[str, Any]:
    return {"ssid": None, "freq_list": [], "mlo": False}


def parse_mlo_conf(text: str) -> dict[str, Any]:
    """Extract MLO-relevant global fields and network blocks from conf text."""
    mld = _empty_mld()
    networks: list[dict[str, Any]] = []
    current: Optional[dict[str, Any]] = None

    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue

        if current is None:
            if line.startswith("network={"):
                current = _empty_network()
                continue
            key, sep, value = line.partition("=")
            if not sep:
                continue
            key = key.strip()
            value = value.strip()
            if key == "mld_force_single_link":
                mld["force_single_link"] = value == "1"
            elif key == "mld_connect_band_pref":
                band = int(value) if value.isdigit() else None
                mld["connect_band_pref"] = band
            elif key == "mld_connect_bssid_pref":
                mld["connect_bssid_pref"] = value.lower() or None
            continue

        if line == "}":
            networks.append(current)
            current = None
            continue

        key, sep, value = line.partition("=")
        if not sep:
            continue
        key = key.strip()
        value = value.strip()
        if key == "ssid":
            current["ssid"] = _unquote_wpa_value(value)
        elif key == "freq_list":
            current["freq_list"] = [
                int(token) for token in value.split() if token.isdigit()
            ]
        elif key == "mlo":
            current["mlo"] = value == "1"

    if current is not None:
        networks.append(current)

    return {"mld": mld, "networks": networks}


def supplicant_running(iface: str, namespace: Optional[str] = None) -> bool:
    """True when a wpa_supplicant process serves ``iface`` in its namespace."""
    try:
        result = ns_exec(
            ["pgrep", "-f", f"wpa_supplicant -B -i {iface}"],
            namespace=namespace,
            no_output=True,
            raise_on_fail=False,
        )
    except (RunCommandError, FileNotFoundError) as exc:
        log.warning("pgrep failed for supplicant check on %s: %r", iface, exc)
        return False
    return result.return_code == 0


def get_mlo_effective_config(
    iface: str,
    namespace: Optional[str] = None,
    *,
    config_dir: Path | str = DEFAULT_CONFIG_DIR,
) -> dict[str, Any]:
    """Return the MLO options written into the interface's supplicant conf."""
    iface = validate_interface_name(iface)
    log.debug("get_mlo_effective_config iface=%s namespace=%r", iface, namespace)

    conf_path = Path(config_dir) / f"{iface}.conf"
    file_exists = conf_path.is_file()

    if file_exists:
        try:
            parsed = parse_mlo_conf(conf_path.read_text())
        except OSError as exc:
            raise ValidationError(
                f"Unable to read supplicant config for {iface}: {exc}",
                status_code=503,
            ) from exc
    else:
        parsed = {"mld": _empty_mld(), "networks": []}

    return {
        "interface": iface,
        "namespace": namespace,
        "config_path": str(conf_path),
        "file_exists": file_exists,
        "supplicant_running": supplicant_running(iface, namespace),
        "mld": parsed["mld"],
        "networks": parsed["networks"],
    }
