# A7m4(Z4M) firmware for mac
基于以下项目适配mac端：https://github.com/Erlla/sony-a7m4-custom-firmware
<img width="1440" height="900" alt="截屏2026-11-15 22 26 30" src="https://github.com/user-attachments/assets/76fb0b77-fe70-4d6c-8b05-8718ae1f0e4d" />
脱敏导出的公开树：只做环境自检与 USB 连接检测。  
**不含**完整安装实现，也**不含**分阶段操作剧本。

## 导入滤镜
将滤镜包中的原生参数 .json 文件放入 SD 卡：

PRIVATE/M4ROOT/GENERAL/LUT/
最新v1.0.1 Lut请在这下载 v1.0.1 Lut
进入相机 曝光/颜色 → 颜色/色调 → 自定义 LUTS，选择存储卡和滤镜文件进行导入。

支持 16 个自定义槽位，每个滤镜可分别保存八项微调和强度。导入完成后，日常使用不依赖卡内原文件。普通 .cube 或 DCP 文件需先转换，不能只修改扩展名。

## 滤镜使用说明

导入自定义音频
将SOUNDS.zip中的音频文件文件放入 SD 卡：

PRIVATE/M4ROOT/GENERAL/SOUNDS/
进入相机 拍摄 → 快门/禁音 → 自定义快门声或自定义对焦声音，选择存储卡和音频文件进行导入。

## mac常见问题
-无法打开安装包提示安装包已损坏
①打开安装限制，mac ios默认不开启非应用商店来源安装权限
终端：sudo spctl --master-disable
②接着打开【系统偏好设置】，选择【安全性与隐私】，选择【通用】，可以看到【任何来源】选定
③重新打开文件进行安装。
<img width="1432" height="984" alt="20261116-161919" src="https://github.com/user-attachments/assets/107e2c73-db20-4511-9e36-bc545617a045" />
如果此时依然提示安装包损坏
①在终端粘贴复制输入命令（注意最后有一个空格）：
终端：sudo xattr -r -d com.apple.quarantine 先不要按回车！先不要按回车！先不要按回车！先不要按回车！
②然后打开 “访达”（Finder）进入 “应用程序” 目录，找到该软件图标，将图标拖到刚才的终端窗口里面，会得到如下组合：
sudo xattr -r -d com.apple.quarantine /Applications/XXXtrom.app
③回到终端窗口按回车，输入系统密码回车即可。
④重新打开文件进行安装。

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

