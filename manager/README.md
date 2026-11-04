# WebUI 管理端

Flask / Jinja2 / SQLite 服务，通过 Linux Docker 部署，HTTPS 反向代理由用户自行配置。手机操作全部调用 Windows 控制端，运行依赖为 Flask 和 Waitress。

## 首次使用

1. 启动 Windows 控制端。
2. 部署管理端，初始密码为 `tianhe`。
3. 在“设置 → 设备”填写控制端根地址（例如 `http://192.168.1.128:8765`）及 API key，保存后刷新设备列表。
4. 设备序列号可直接填写、从列表选择或留空；留空沿用控制端选择规则，可先点击“测试手机连接”。
5. 在“应用”按需填写包名和等待秒数（0～120 秒），再设置自动计划和 Bark 通知。

包名可留空，手动和自动任务均使用控制端 `default_package`（示例为 `com.tencent.wework`）；填写包名则覆盖默认值。完整应用流程结束后锁屏；独立截图先亮屏、解锁并等待 0.5 秒，再采集当前界面。控制端与 Bark 密钥留空保存会保持原值，页面不回显密钥。

## Docker 部署

首次部署或从原部署切换到目录挂载时，在 Linux 的 `manager/` 目录执行：

```bash
docker compose up -d --build
docker compose logs -f
```

容器名为 `wechat-checkin`，默认绑定宿主机 `127.0.0.1:8000`。用户自行配置 Nginx 等反向代理，通过 HTTPS 访问管理端。

镜像只安装 Python 运行依赖，将当前 `manager/` 目录映射到容器 `/app`。代码、页面和资源直接使用宿主文件，运行数据保存在宿主 `manager/data/`。运行一个管理端实例即可。

### 修改宿主机端口

在 `manager/` 目录复制[覆盖配置示例](compose.override.example.yaml)：

```bash
cp compose.override.example.yaml compose.override.yaml
```

修改 `compose.override.yaml` 中的宿主机端口 `8080`，容器端口保持 `8000`，然后执行：

```bash
docker compose up -d
```

Compose 自动加载该覆盖文件，修改端口后无需重新构建镜像。本机 `compose.override.yaml` 已加入 Git 忽略规则。示例使用 `!override` 完整替换端口映射，需要 [Docker Compose 2.24.4 或以上版本](https://docs.docker.com/reference/compose-file/merge/#replace-value)。

## 更新

在 `manager/` 目录执行，普通代码更新只需拉取并重启：

```bash
git pull
docker restart wechat-checkin
```

`requirements.txt` 变化时重新构建并启动：

```bash
git pull
docker compose up -d --build
```

## 本机开发

本机开发使用 Python 3.12 或以上版本。Windows 下从仓库根目录创建环境、安装依赖并直接运行入口：

```powershell
python -m venv manager/.venv
.\manager\.venv\Scripts\python.exe -m pip install -r manager/requirements.txt
.\manager\.venv\Scripts\python.exe -X utf8 manager/main.py --http --data-dir manager/data/dev
```

访问 `http://127.0.0.1:8000`。`--dev` 保留为 `--http` 的别名。其他参数通过 `python main.py --help` 查看；`--trusted-proxy` 默认 `127.0.0.1`，Docker 启动使用 `*`，配合仅供 Nginx 访问的网络或端口。

## 数据维护

| 路径 | 用途 |
| --- | --- |
| `data/checkin.sqlite3` | 设置、账号、计划、业务记录、派发快照和通知状态 |
| `data/screenshots/` | 长期保存的业务截图 |
| `data/service.log` | 滚动运行日志 |

Docker 通过当前目录映射持久化 `data/`；本机开发可用 `--data-dir` 指定数据目录。同一数据目录只运行一个管理端实例。

备份或迁移数据时停止服务，复制整个数据目录，或使用 SQLite 备份接口并另存截图。运行中直接复制数据库主文件可能遗漏 WAL 数据。首次启动时创建数据库，后续启动自动补充所需结构。

停止管理端不会取消控制端已受理的操作。恢复后按原请求编号查询结果，保留在途任务数据和派发快照，避免丢失恢复依据。任务调度、状态和恢复规则见[管理端设计](../docs/04-manager-design.md)。

## 验证

在 `manager/` 执行：

```bash
.venv/bin/python -m unittest discover -s tests -v
```

Windows 使用 `.\.venv\Scripts\python.exe`。测试需要完整仓库，生产镜像无需 `controller/`；测试使用临时数据库、真实控制端实现和假 ADB，不读取真实控制端配置或操作手机。
