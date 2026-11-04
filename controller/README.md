# ADB 控制端

Windows 上通过 Python 3.12 和 venv 运行，源码集中在 `main.py`，配置集中在 `config.json`。管理端通过 HTTP API 调用。

## 安装与启动

先将 Android Platform-Tools 放入 `controller/adb/`，连接手机并授权 USB 调试、解除锁屏密码。以下命令从仓库根目录执行：

```powershell
python -m venv controller/.venv
.\controller\.venv\Scripts\python.exe -m pip install -r controller/requirements.txt
.\controller\script\start.ps1 --init-config
.\controller\script\start.ps1
```

已有环境和配置时，只执行最后一条。`--init-config` 生成随机密钥并退出，不覆盖现有配置。ADB 工具放在 `controller/adb/`，或修改 `adb_dir` 指向已安装的目录。

默认监听 `0.0.0.0:8765`；本机访问 `http://127.0.0.1:8765`，其他设备使用 Windows 局域网 IP。所有接口均要求 Bearer API key，Windows 网络策略需允许管理端访问该端口。

入口脚本自动定位控制端目录。直接运行的等效命令为：

```powershell
.\controller\.venv\Scripts\python.exe -X utf8 controller/main.py --config controller/config.json
```

上面的命令按仓库根目录书写；ADB、数据目录中的相对路径始终以配置文件目录为基准。

## 本机文件

| 文件或目录 | 用途 |
| --- | --- |
| `config.example.json` | 入库配置示例；密钥占位值须通过初始化命令替换 |
| `config.json` | 实际配置和 API key，不入库 |
| `adb/` | Android Platform-Tools 本机依赖，不入库 |
| `.venv/` | Python 环境，不入库 |
| `data/requests.sqlite3` | 持久请求状态、参数及去重标记 |
| `data/screenshots/` | 短期截图 |
| `data/controller.log` | 滚动日志 |

保留运行数据目录，才能沿用历史 `request_id` 的去重记录。完整截图及结果默认保留 24 小时，请求编号的精简去重记录继续保留。

## 后台运行

可使用 Windows 任务计划程序启动：

- 程序：`controller/.venv/Scripts/python.exe` 的绝对路径。
- 参数：`-X utf8`、`controller/main.py` 的绝对路径，以及 `--config` 后的配置文件绝对路径；含空格的路径加双引号。
- 起始位置：`controller/` 的绝对路径。
- 使用已完成 USB 调试授权的账号，按需设置失败后重启。

同一数据目录只运行一个实例，启动时使用进程锁检查。正常退出会等待当前手机操作及收尾完成；强制终止后，未完成请求在下次启动时记为 `unknown`。

## 备份与恢复

停止服务后备份 `config.json` 和整个 `data/`，或使用 SQLite 备份接口备份数据库并保存配置、截图。运行时直接复制 SQLite 主文件可能遗漏 WAL 数据。

恢复时保留原请求账本及配置，继续使用历史编号的去重记录。Python 环境从依赖清单重新创建，ADB 按实际安装路径配置。

配置字段、操作流程、六个 API 和去重规则见[控制端设计](../docs/03-controller-design.md)。
