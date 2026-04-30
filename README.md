# virtio-net negotiated feature dump

## Linux 版本（已有）

`virtio_net_features.py` 通过传入 virtio-net 设备的 PCI BDF，定位该设备在 PCI capability 里的 `VIRTIO_PCI_CAP_COMMON_CFG`，再从对应 BAR 的 common config 中读取：

- `device_feature`（设备支持特性）
- `driver_feature`（驱动已确认特性）

并输出两者交集（协商完成特性列表）。

### 用法

```bash
sudo ./virtio_net_features.py 0000:00:05.0
# 或
sudo ./virtio_net_features.py 00:05.0
```

> 通常需要 root 权限（访问 `/sys/bus/pci/devices/.../resourceN` 并对 common cfg 的 select 寄存器写入）。

---

## Windows 版本

新增 `virtio_net_features_windows.cpp`，实现同样流程：

1. 按 BDF 访问 PCI 配置空间。
2. 遍历 capability list 找到 `VIRTIO_PCI_CAP_COMMON_CFG`。
3. 根据 capability 里的 BAR + offset 定位 `common_cfg`。
4. 读取 `device_feature` 与 `driver_feature`，输出协商后的特性位图。

### 依赖

Windows 用户态默认不能直接访问 PCI config/MMIO，本程序依赖 **WinRing0/OpenLibSys** 驱动接口（`OlsApi.h` + 对应 DLL/driver）。

### 编译示例（MSVC）

```bat
cl /EHsc /std:c++17 virtio_net_features_windows.cpp /I path\to\WinRing0\include path\to\WinRing0\OlsLib.lib
```

### 运行示例

```bat
virtio_net_features_windows.exe 00:05.0
virtio_net_features_windows.exe 0000:00:05.0
```

> 需要“管理员权限”运行，并确保 WinRing0 驱动已正确加载。
