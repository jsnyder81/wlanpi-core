from typing import Any, Optional

from pydantic import BaseModel, Field


class RoutingTable(BaseModel):
    namespace: Optional[str] = None
    routes: list[dict[str, Any]] = Field(default_factory=list)


class LinkStats(BaseModel):
    interface: str
    namespace: Optional[str] = None
    link_detected: Optional[str] = None
    speed_mbps: Optional[int] = None
    duplex: Optional[str] = None
    port: Optional[str] = None
    driver: Optional[str] = None
    raw: dict[str, str] = Field(default_factory=dict)


class MloLinkEntry(BaseModel):
    link_id: int = Field(description="mac80211 link index (0-based)")
    freq_mhz: Optional[int] = Field(
        default=None, description="Link center frequency in MHz, from `iw dev link`"
    )
    ap_link_bssid: Optional[str] = Field(
        default=None, description="AP link BSSID for this link"
    )
    local_address: Optional[str] = Field(
        default=None,
        description=(
            "Client-side randomized link address, from debugfs `link-N/addr`. "
            "Use this address (not the MLD address) in capture filters."
        ),
    )
    rx_fragments: Optional[int] = Field(
        default=None,
        description=(
            "Monotonic per-link RX counter from debugfs. None when "
            "`mac80211_debugfs` is false. Take deltas across a transfer to "
            "compute per-link share; equal nonzero shares alongside a "
            "multi-link association indicates STR aggregation."
        ),
    )
    rx_duplicates: Optional[int] = Field(
        default=None, description="Monotonic per-link RX duplicate counter"
    )


class MldAggregateStats(BaseModel):
    rx_bytes: Optional[int] = Field(
        default=None, description="MLD-summed RX bytes (not per-link)"
    )
    rx_packets: Optional[int] = None
    tx_bytes: Optional[int] = None
    tx_packets: Optional[int] = None


class MloLinkStats(BaseModel):
    interface: str
    namespace: Optional[str] = None
    phy: Optional[str] = Field(default=None, description="Wireless phy, e.g. phy1")
    mac80211_debugfs: bool = Field(
        description=(
            "False (with `debugfs_reason`) when debugfs is not mounted or the "
            "kernel lacks CONFIG_MAC80211_DEBUGFS. Link inventory from `iw` "
            "is reported either way; per-link RX counters require debugfs."
        )
    )
    debugfs_reason: Optional[str] = Field(
        default=None,
        description=(
            "Why debugfs is unusable: `debugfs_not_mounted`, "
            "`mac80211_debugfs_disabled`, `station_entry_missing` or "
            "`station_entry_unreadable`"
        ),
    )
    connected: bool = False
    ssid: Optional[str] = None
    mld_address: Optional[str] = Field(
        default=None, description="MLD (peer) address of the association"
    )
    signal_dbm: Optional[int] = None
    mld_stats: Optional[MldAggregateStats] = Field(
        default=None, description="Aggregate MLD counters (summed across links)"
    )
    link_count: int = Field(
        default=0, description="Number of setup links; > 1 means MLO is associated"
    )
    links: list[MloLinkEntry] = Field(default_factory=list)


class MloNetworkConfig(BaseModel):
    ssid: Optional[str] = None
    freq_list: list[int] = Field(
        default_factory=list,
        description="Per-network `freq_list` bounding the link set (empty = unrestricted)",
    )
    mlo: bool = False


class MloGlobalConfig(BaseModel):
    force_single_link: bool = Field(
        default=False, description="`mld_force_single_link=1` in the global header"
    )
    connect_band_pref: Optional[int] = Field(
        default=None, description="`mld_connect_band_pref`: 1=2.4, 2=5, 3=6 GHz"
    )
    connect_bssid_pref: Optional[str] = Field(
        default=None, description="`mld_connect_bssid_pref` anchor BSSID"
    )


class MloEffectiveConfig(BaseModel):
    interface: str
    namespace: Optional[str] = None
    config_path: str = Field(
        description="On-disk supplicant conf file backing this interface"
    )
    file_exists: bool = False
    supplicant_running: bool = Field(
        default=False,
        description="True while a wpa_supplicant process serves this interface",
    )
    mld: MloGlobalConfig = Field(default_factory=MloGlobalConfig)
    networks: list[MloNetworkConfig] = Field(
        default_factory=list,
        description="network={...} blocks in the conf, highest priority first",
    )


class SocketConnection(BaseModel):
    protocol: str
    state: str
    recv_q: Any = None
    send_q: Any = None
    local: str
    peer: str


class ConnectionsResponse(BaseModel):
    namespace: Optional[str] = None
    connections: list[SocketConnection] = Field(default_factory=list)


class DhcpRenewResponse(BaseModel):
    interface: str
    namespace: Optional[str] = None
    status: str


class DhcpLeasesResponse(BaseModel):
    leases: list[dict[str, Any]] = Field(default_factory=list)
    source: str
    error: Optional[str] = None


class WlanAdapterDriver(BaseModel):
    interface: str = Field(description="Linux interface name from iw dev, e.g. wlan0")
    driver: Optional[str] = Field(
        default=None,
        description="Kernel driver from ethtool -i, e.g. iwlwifi, ath9k_htc",
    )
    bus: str = Field(
        description="Attachment bus for this interface: usb, pci, or platform (SDIO/on-board)",
    )


class WlanUsbDriversResponse(BaseModel):
    adapters: list[WlanAdapterDriver] = Field(
        default_factory=list,
        description=(
            "USB-attached WLAN interfaces only. "
            "Empty array is normal on devices with only PCI/on-board Wi-Fi — use "
            "GET /network/wlan/pci-drivers instead."
        ),
    )
    interfaces_scanned: int = Field(
        default=0,
        description="Wireless interfaces enumerated via iw dev before USB filtering",
    )


class PciDevice(BaseModel):
    pci_id: str = Field(description="lspci BDF prefix, e.g. 0000:01:00.0")
    description: str = Field(description="Human-readable lspci device line")


class WlanPciDriversResponse(BaseModel):
    adapters: list[WlanAdapterDriver] = Field(
        default_factory=list,
        description=(
            "WLAN interfaces on PCI or platform/SDIO buses. "
            "Multiple entries can share one PHY (e.g. wlan0 + wlanpi0)."
        ),
    )
    pci_devices: list[PciDevice] = Field(
        default_factory=list,
        description="Wireless PCI functions from lspci (may be non-empty when adapters is empty)",
    )
    interfaces_scanned: int = Field(
        default=0,
        description="Wireless interfaces enumerated via iw dev before bus filtering",
    )
