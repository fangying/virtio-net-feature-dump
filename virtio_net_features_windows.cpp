#include <windows.h>
#include <cstdint>
#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <iomanip>
#include <iostream>
#include <map>
#include <sstream>
#include <string>
#include <vector>

// 需要 WinRing0 / OpenLibSys: OlsApi.h + WinRing0 驱动
#include "OlsApi.h"

static constexpr uint8_t VIRTIO_PCI_CAP_ID = 0x09;
static constexpr uint8_t VIRTIO_PCI_CAP_COMMON_CFG = 1;
static constexpr uint16_t PCI_STATUS = 0x06;
static constexpr uint8_t PCI_CAPABILITY_LIST = 0x34;

struct Bdf {
    uint32_t bus;
    uint32_t dev;
    uint32_t func;
};

bool parse_bdf(const std::string& s, Bdf& out) {
    // 支持 bb:dd.f 或 dddd:bb:dd.f
    unsigned dom = 0, bus = 0, dev = 0, fun = 0;
    if (std::sscanf(s.c_str(), "%x:%x:%x.%x", &dom, &bus, &dev, &fun) == 4) {
        (void)dom; // domain 在 WinRing0 的 PCI 访问接口中通常不可用
    } else if (std::sscanf(s.c_str(), "%x:%x.%x", &bus, &dev, &fun) == 3) {
    } else {
        return false;
    }
    if (bus > 0xff || dev > 31 || fun > 7) return false;
    out.bus = bus;
    out.dev = dev;
    out.func = fun;
    return true;
}

uint32_t pci_addr(const Bdf& bdf, uint8_t reg) {
    return (bdf.bus << 8) | (bdf.dev << 3) | bdf.func | ((uint32_t)reg << 24);
}

bool pci_read8(const Bdf& bdf, uint8_t off, uint8_t& out) {
    DWORD v = 0;
    if (!ReadPciConfigDwordEx(pci_addr(bdf, off & ~0x3), &v)) return false;
    out = (v >> ((off & 3) * 8)) & 0xff;
    return true;
}

bool pci_read16(const Bdf& bdf, uint8_t off, uint16_t& out) {
    DWORD v = 0;
    if (!ReadPciConfigDwordEx(pci_addr(bdf, off & ~0x3), &v)) return false;
    out = (v >> ((off & 2) * 8)) & 0xffff;
    return true;
}

bool pci_read32(const Bdf& bdf, uint8_t off, uint32_t& out) {
    DWORD v = 0;
    if (!ReadPciConfigDwordEx(pci_addr(bdf, off), &v)) return false;
    out = v;
    return true;
}

bool read_phys32(uint64_t phys, uint32_t& out) {
    DWORD v = 0;
    if (!ReadDmiMemory((DWORD)phys, (PBYTE)&v, sizeof(v))) return false;
    out = v;
    return true;
}

bool write_phys32(uint64_t phys, uint32_t val) {
    return WriteDmiMemory((DWORD)phys, (PBYTE)&val, sizeof(val));
}

int main(int argc, char** argv) {
    if (argc != 2) {
        std::cerr << "Usage: virtio_net_features_windows.exe <BDF>\n";
        return 1;
    }

    if (!InitializeOls()) {
        std::cerr << "InitializeOls failed. 请以管理员运行并确保 WinRing0 驱动已安装。\n";
        return 1;
    }

    Bdf bdf{};
    if (!parse_bdf(argv[1], bdf)) {
        std::cerr << "Invalid BDF format\n";
        DeinitializeOls();
        return 1;
    }

    uint32_t vd = 0;
    if (!pci_read32(bdf, 0x00, vd)) {
        std::cerr << "Read PCI vendor/device failed\n";
        DeinitializeOls();
        return 1;
    }
    uint16_t vendor = vd & 0xffff;
    uint16_t device = (vd >> 16) & 0xffff;

    uint16_t status = 0;
    if (!pci_read16(bdf, PCI_STATUS, status) || !(status & 0x10)) {
        std::cerr << "PCI capability list not present\n";
        DeinitializeOls();
        return 1;
    }

    uint8_t cap = 0;
    if (!pci_read8(bdf, PCI_CAPABILITY_LIST, cap)) {
        std::cerr << "Read cap pointer failed\n";
        DeinitializeOls();
        return 1;
    }

    uint8_t common_bar = 0xff;
    uint32_t common_off = 0;
    uint8_t guard = 0;
    while (cap && guard++ < 48) {
        uint8_t cap_id = 0, next = 0, cfg_type = 0, bar = 0;
        uint32_t off = 0;
        if (!pci_read8(bdf, cap + 0, cap_id) || !pci_read8(bdf, cap + 1, next) ||
            !pci_read8(bdf, cap + 3, cfg_type) || !pci_read8(bdf, cap + 4, bar) ||
            !pci_read32(bdf, cap + 8, off)) {
            std::cerr << "Read capability failed\n";
            DeinitializeOls();
            return 1;
        }
        if (cap_id == VIRTIO_PCI_CAP_ID && cfg_type == VIRTIO_PCI_CAP_COMMON_CFG) {
            common_bar = bar;
            common_off = off;
            break;
        }
        cap = next;
    }

    if (common_bar == 0xff || common_bar > 5) {
        std::cerr << "No VIRTIO_PCI_CAP_COMMON_CFG found\n";
        DeinitializeOls();
        return 1;
    }

    uint32_t bar_lo = 0;
    if (!pci_read32(bdf, static_cast<uint8_t>(0x10 + common_bar * 4), bar_lo)) {
        std::cerr << "Read BAR failed\n";
        DeinitializeOls();
        return 1;
    }

    uint64_t bar_base = (bar_lo & ~0xFULL);
    uint64_t common = bar_base + common_off;

    auto read_feat64 = [&](uint32_t sel_off, uint32_t val_off, uint64_t& bits) -> bool {
        if (!write_phys32(common + sel_off, 0)) return false;
        uint32_t lo = 0;
        if (!read_phys32(common + val_off, lo)) return false;
        if (!write_phys32(common + sel_off, 1)) return false;
        uint32_t hi = 0;
        if (!read_phys32(common + val_off, hi)) return false;
        bits = ((uint64_t)hi << 32) | lo;
        return true;
    };

    uint64_t dev_feat = 0, drv_feat = 0;
    if (!read_feat64(0x00, 0x04, dev_feat) || !read_feat64(0x08, 0x0c, drv_feat)) {
        std::cerr << "Read feature registers failed. 可能是权限/驱动限制。\n";
        DeinitializeOls();
        return 1;
    }

    uint64_t nego = dev_feat & drv_feat;

    std::cout << "vendor=0x" << std::hex << vendor << " device=0x" << device << "\n";
    std::cout << "common_cfg BAR" << std::dec << (int)common_bar << " + 0x" << std::hex << common_off << "\n";
    std::cout << "device_feature = 0x" << std::hex << dev_feat << "\n";
    std::cout << "driver_feature = 0x" << std::hex << drv_feat << "\n";
    std::cout << "negotiated     = 0x" << std::hex << nego << "\n";

    DeinitializeOls();
    return 0;
}
