#!/usr/bin/env python3
import argparse
import mmap
import os
import struct
import sys
from typing import Dict, List, Tuple

VIRTIO_PCI_CAP_ID = 0x09
VIRTIO_PCI_CAP_COMMON_CFG = 1

PCI_STATUS = 0x06
PCI_STATUS_CAP_LIST = 0x10
PCI_CAPABILITY_LIST = 0x34

COMMON_CFG_OFFSETS = {
    "device_feature_select": 0x00,
    "device_feature": 0x04,
    "driver_feature_select": 0x08,
    "driver_feature": 0x0C,
}

VIRTIO_NET_FEATURES: Dict[int, str] = {
    0: "VIRTIO_NET_F_CSUM",
    1: "VIRTIO_NET_F_GUEST_CSUM",
    5: "VIRTIO_NET_F_MAC",
    6: "VIRTIO_NET_F_GSO",
    7: "VIRTIO_NET_F_GUEST_TSO4",
    8: "VIRTIO_NET_F_GUEST_TSO6",
    9: "VIRTIO_NET_F_GUEST_ECN",
    10: "VIRTIO_NET_F_GUEST_UFO",
    11: "VIRTIO_NET_F_HOST_TSO4",
    12: "VIRTIO_NET_F_HOST_TSO6",
    13: "VIRTIO_NET_F_HOST_ECN",
    14: "VIRTIO_NET_F_HOST_UFO",
    15: "VIRTIO_NET_F_MRG_RXBUF",
    16: "VIRTIO_NET_F_STATUS",
    17: "VIRTIO_NET_F_CTRL_VQ",
    18: "VIRTIO_NET_F_CTRL_RX",
    19: "VIRTIO_NET_F_CTRL_VLAN",
    20: "VIRTIO_NET_F_CTRL_RX_EXTRA",
    21: "VIRTIO_NET_F_GUEST_ANNOUNCE",
    22: "VIRTIO_NET_F_MQ",
    23: "VIRTIO_NET_F_CTRL_MAC_ADDR",
    24: "VIRTIO_NET_F_NOTF_COAL",
    52: "VIRTIO_NET_F_GUEST_USO4",
    53: "VIRTIO_NET_F_GUEST_USO6",
    54: "VIRTIO_NET_F_HOST_USO",
    55: "VIRTIO_NET_F_HASH_REPORT",
    56: "VIRTIO_NET_F_GUEST_HDRLEN",
    57: "VIRTIO_NET_F_RSS",
    58: "VIRTIO_NET_F_RSC_EXT",
    59: "VIRTIO_NET_F_STANDBY",
    60: "VIRTIO_NET_F_SPEED_DUPLEX",
    61: "VIRTIO_NET_F_CTRL_GUEST_OFFLOADS",
    62: "VIRTIO_NET_F_MTU",
    63: "VIRTIO_NET_F_CTRL_COAL",
}

VIRTIO_COMMON_FEATURES: Dict[int, str] = {
    24: "VIRTIO_F_NOTIFY_ON_EMPTY",
    27: "VIRTIO_F_ANY_LAYOUT",
    28: "VIRTIO_F_RING_INDIRECT_DESC",
    29: "VIRTIO_F_RING_EVENT_IDX",
    32: "VIRTIO_F_VERSION_1",
    33: "VIRTIO_F_ACCESS_PLATFORM",
    34: "VIRTIO_F_RING_PACKED",
    35: "VIRTIO_F_IN_ORDER",
    36: "VIRTIO_F_ORDER_PLATFORM",
    37: "VIRTIO_F_SR_IOV",
    38: "VIRTIO_F_NOTIFICATION_DATA",
    39: "VIRTIO_F_NOTIF_CONFIG_DATA",
    40: "VIRTIO_F_RING_RESET",
}


def normalize_bdf(bdf: str) -> str:
    if bdf.count(":") == 1:
        bdf = f"0000:{bdf}"
    return bdf


def read_config(path: str) -> bytes:
    with open(path, "rb") as f:
        return f.read(4096)


def parse_vendor_device(config: bytes) -> Tuple[int, int]:
    vendor, device = struct.unpack_from("<HH", config, 0)
    return vendor, device


def find_virtio_common_cap(config: bytes) -> Tuple[int, int]:
    status = struct.unpack_from("<H", config, PCI_STATUS)[0]
    if not (status & PCI_STATUS_CAP_LIST):
        raise RuntimeError("PCI config space does not expose capability list")

    cap_ptr = config[PCI_CAPABILITY_LIST]
    seen = set()
    while cap_ptr and cap_ptr not in seen:
        seen.add(cap_ptr)
        cap_id = config[cap_ptr]
        next_ptr = config[cap_ptr + 1]
        if cap_id == VIRTIO_PCI_CAP_ID:
            cfg_type = config[cap_ptr + 3]
            bar = config[cap_ptr + 4]
            offset = struct.unpack_from("<I", config, cap_ptr + 8)[0]
            if cfg_type == VIRTIO_PCI_CAP_COMMON_CFG:
                return bar, offset
        cap_ptr = next_ptr

    raise RuntimeError("No VIRTIO_PCI_CAP_COMMON_CFG capability found")


def read_u32_from_bar(dev_dir: str, bar: int, offset: int) -> int:
    res_path = os.path.join(dev_dir, f"resource{bar}")
    with open(res_path, "rb") as f:
        mm = mmap.mmap(f.fileno(), offset + 4, access=mmap.ACCESS_READ)
        try:
            return struct.unpack_from("<I", mm, offset)[0]
        finally:
            mm.close()


def read_feature_bits(dev_dir: str, bar: int, common_off: int, select_reg: int, value_reg: int) -> int:
    res_path = os.path.join(dev_dir, f"resource{bar}")
    with open(res_path, "r+b", buffering=0) as f:
        mm = mmap.mmap(f.fileno(), common_off + value_reg + 4)
        try:
            struct.pack_into("<I", mm, common_off + select_reg, 0)
            low = struct.unpack_from("<I", mm, common_off + value_reg)[0]
            struct.pack_into("<I", mm, common_off + select_reg, 1)
            high = struct.unpack_from("<I", mm, common_off + value_reg)[0]
            return low | (high << 32)
        finally:
            mm.close()


def bits_to_names(bits: int) -> List[str]:
    names = []
    for bit in range(64):
        if bits & (1 << bit):
            name = VIRTIO_NET_FEATURES.get(bit) or VIRTIO_COMMON_FEATURES.get(bit) or f"UNKNOWN_BIT_{bit}"
            names.append(f"bit {bit:2d}: {name}")
    return names


def main() -> int:
    parser = argparse.ArgumentParser(description="Dump negotiated Virtio-Net features via PCI BDF")
    parser.add_argument("bdf", help="PCI BDF, e.g. 0000:00:05.0 or 00:05.0")
    args = parser.parse_args()

    bdf = normalize_bdf(args.bdf)
    dev_dir = os.path.join("/sys/bus/pci/devices", bdf)
    if not os.path.isdir(dev_dir):
        print(f"error: PCI device not found: {bdf}", file=sys.stderr)
        return 1

    config = read_config(os.path.join(dev_dir, "config"))
    vendor, device = parse_vendor_device(config)
    if vendor != 0x1AF4:
        print(f"warning: vendor id is 0x{vendor:04x}, expected virtio vendor 0x1af4", file=sys.stderr)

    bar, common_off = find_virtio_common_cap(config)

    device_features = read_feature_bits(
        dev_dir,
        bar,
        common_off,
        COMMON_CFG_OFFSETS["device_feature_select"],
        COMMON_CFG_OFFSETS["device_feature"],
    )
    driver_features = read_feature_bits(
        dev_dir,
        bar,
        common_off,
        COMMON_CFG_OFFSETS["driver_feature_select"],
        COMMON_CFG_OFFSETS["driver_feature"],
    )

    negotiated = device_features & driver_features

    print(f"BDF: {bdf}")
    print(f"PCI ID: vendor=0x{vendor:04x} device=0x{device:04x}")
    print(f"virtio common cfg: BAR{bar} + 0x{common_off:x}")
    print(f"device features : 0x{device_features:016x}")
    print(f"driver features : 0x{driver_features:016x}")
    print(f"negotiated      : 0x{negotiated:016x}")
    print("negotiated feature list:")
    for line in bits_to_names(negotiated):
        print(f"  - {line}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
