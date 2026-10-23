# 硬盘清理工具 (Disk Cleaner)

面向 Windows 的轻量磁盘清理工具: 纯 Python + tkinter 实现, 无第三方运行时依赖, 界面与控件全部自绘。

## 功能

- **临时文件与缓存清理**: 用户 / 系统临时文件、Windows 更新缓存、缩略图与图标缓存、浏览器缓存 (Chrome / Edge / Brave / Firefox)、崩溃转储、系统网络缓存、DirectX 着色器缓存、传递优化缓存、错误报告、Prefetch、Windows.old (默认不勾选)。
- **自定义清理目录**: 添加任意目录到清理列表, 配置保存在 `%LOCALAPPDATA%\DiskCleaner\settings.json`。
- **回收站**: 查看占用空间并可一键清空。
- **大文件扫描**: 按体积筛选, 结果可点击列头排序、导出 CSV。
- **重复文件查找**: 按大小 → 头部 64KB → 完整内容三阶段比对, 支持"每组保留一个"。

## 安全设计

| 机制 | 说明 |
| --- | --- |
| 占用检测 | 通过独占打开 (共享冲突) 与枚举进程已加载模块 (EXE/DLL 映像) 识别正在使用的文件, 清理时一律跳过 |
| 风险识别 | `.dll` / `.sys` / 数据库 / 邮件库 / 虚拟机磁盘 / Windows 关键目录 / 系统与程序目录内的文件列为受保护; 同时结合卸载注册表、开始菜单快捷方式与运行中进程的模块目录, 识别常规安装、绿色软件和便携软件的实际位置 (如 `D:\Games\Xxx`), 大文件与重复文件删除时自动排除 |
| 删除提示 | 安装包 / 日志 / 备份 / 镜像 / 邮件缓存等可删除类型以青色标注, 并在确认框中汇总提醒, 但不阻止删除 |
| 删除方式 | 大文件与重复文件移入回收站; 网络位置或部分移动磁盘不支持回收站时, 确认框会提前提示可能无法还原; 临时文件按系统惯例直接删除 |
| 目录校验 | 自定义目录拒绝盘符根目录、`Windows`、`Program Files`、`ProgramData` 等系统位置; 保存与加载配置时都会重新校验, 手工篡改配置文件同样无法绕过 |
| 导出安全 | CSV 导出对以 `=`、`+`、`-`、`@` 开头的文本加前导单引号, 避免 Excel 公式注入 |
| 遍历防护 | 不跟随 junction / 符号链接, 避免重复扫描或删除到目录之外 |
| 自身保护 | 打包运行时跳过 PyInstaller 自身的临时解压目录 |

## 快速开始

```powershell
# 直接运行源码 (需要 Python 3.10+)
python disk_cleaner.py

# 或双击
启动清理工具.bat
```

清理系统目录 (系统临时文件、更新缓存、Prefetch、错误报告等) 需要以管理员身份运行; 普通权限下这些项会因权限不足被跳过, 不会报错中断。

## 打包

```powershell
.\build.ps1
```

脚本会在缺少 PyInstaller 时自动安装, 然后生成单文件 `DiskCleaner.exe` (无控制台窗口, 内置图标)。

## 测试

```powershell
python -m unittest discover -s tests -t .
```

测试全部在系统临时目录中进行, 不会触碰真实文件, 也不会真的清空回收站。

## 项目结构

```
disk_cleaner.py          界面层: 自绘控件、页面、后台任务调度
cleaner_core.py          核心层: 扫描 / 清理 / 占用检测 / 重复文件, 与界面无关
tests/                   核心层单元测试
build.ps1                PyInstaller 打包脚本
app_icon.ico             应用图标
```

`cleaner_core.py` 可以脱离界面单独调用, 例如:

```python
import cleaner_core as core

result = core.scan_targets(core.default_targets())
print(core.human_size(result.deletable_bytes))
```

## 已知限制

- 仅支持 Windows; 非 Windows 平台上删除类操作会安全地返回失败。
- 休眠文件 (`hiberfil.sys`) 与页面文件不能通过普通文件删除, 需要使用系统电源设置。
- 超过 260 字符的长路径可能无法移入回收站 (受 Shell API 限制)。
