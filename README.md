# A7m4(Z4M) firmware for mac
基于大佬项目套壳适配mac端：https://github.com/Erlla/sony-a7m4-custom-firmware
<img width="1440" height="900" alt="截屏2026-11-15 22 26 30" src="https://github.com/user-attachments/assets/76fb0b77-fe70-4d6c-8b05-8718ae1f0e4d" />
脱敏导出的公开树：只做环境自检与 USB 连接检测。  
**不含**完整安装实现，也**不含**分阶段操作剧本。

## 许可与边界

- `LICENSE` — MIT（公开壳）
- `NOTICE` — open-core 边界（专有安装模块不在本仓）

公开：`m4probe` 诊断壳、模块加载契约（`install_api`）。  
不公开：相机写入与完整安装实现（官方完整版 / 专有模块）。  
请勿从本仓库推断或复现私有安装步骤。项目与 Sony 无关联。

运行时常见第三方：PyUSB（BSD）、libusb / libusb-package（LGPL-2.1）、Python 3（PSF）。协议相关表仅在闭源包中使用，不随本仓分发。

## 完整版下载（Releases）

请到本仓 **Releases → v1.0** 下载（不在源码树内）：

| 文件 | 说明 |
|---|---|
| `ZM4-release-v1.0.dmg` | Apple Silicon（M 系列）安装包，拖到「应用程序」 |
| `BODYDATA.DAT` | 固件相关数据文件（按 App「操作说明」放入相机卡槽 1 根目录） |

目标机无需另装 Python / Homebrew / Zig。首次打开若被拦截：右键 → 打开。

## 快速开始（源码诊断壳）

```bash
cd ZM4
python3 -m m4probe --doctor          # 环境自检
python3 -m m4probe.detect            # 连接检测（只读，不写相机）
python3 -m m4probe.detect --json
```

连接检测：判断是否以 USB 大容量存储（MSC）接到本机、是否为 ILCE-7M4。

## 目录

| 路径 | 说明 |
|---|---|
| `ZM4/m4probe/` | doctor / detect / install_api |
| `ZM4/zm4_install/` | 专有模块占位（无实现代码） |

## 自检

1. `python -m m4probe --doctor` 可运行  
2. `python -m m4probe.detect` 可报告连接状态  
3. 无 `m4install` / 专有实现源码  

