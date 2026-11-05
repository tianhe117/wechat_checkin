# 控制端设计

## 1. 配置与启动

控制端实现位于 [controller/main.py](../controller/main.py)，配置格式见 [config.example.json](../controller/config.example.json)。配置在启动时校验，修改后重启生效；相对路径以配置文件所在目录为基准。

| 配置项 | 默认值 | 含义与约束 |
| --- | --- | --- |
| `adb_dir` | `./adb` | ADB 工具目录，启动时检查可执行文件 |
| `api_key` | 无有效默认值 | 32～512 位不含空格的可打印 ASCII 字符，由初始化命令生成随机值 |
| `host`、`port` | `0.0.0.0`、`8765` | HTTP 监听地址与端口 |
| `default_serial` | 空 | 默认设备序列号，空时选择唯一可用设备 |
| `default_package` | `com.tencent.wework` | 默认应用包名 |
| `default_delay` | `10` | 默认等待秒数，整数，范围为 `0～max_delay` |
| `max_delay` | `120` | 请求可指定的最大等待秒数 |
| `command_timeout` | `20` | 单条 ADB 命令超时秒数，正整数，不包含流程中的等待 |
| `data_dir` | `./data` | 请求账本、短期截图与日志目录 |
| `result_retention_hours` | `24` | 完整结果与截图保留小时数，正整数 |

启动时锁定数据目录，恢复中断请求，再提供服务。正常退出等待正在执行的手机操作及收尾完成。安装和后台启动步骤见[控制端说明](../controller/README.md)。

## 2. 设备与执行流程

设备按“请求 `serial` → 配置 `default_serial` → 唯一可用设备”的顺序选择。明确指定的设备不可用时报错；自动选择时，无可用设备或多台可用设备均报错。首次受理后固定设备，后续命令始终使用同一 `adb -s <serial>`。

完整应用流程执行前检查参数、设备和应用可用性，然后执行下表步骤。命令省略统一的 ADB 前缀。

| 步骤 | 行为或命令 |
| --- | --- |
| 亮屏 | `shell input keyevent KEYCODE_WAKEUP` |
| 解锁并返回桌面 | `shell wm dismiss-keyguard`，再执行 `shell input keyevent KEYCODE_HOME` |
| 关闭目标应用 | `shell am force-stop --user current <package>` |
| 启动目标应用 | `shell monkey -p <package> -c android.intent.category.LAUNCHER 1` |
| 等待 | 启动命令返回后等待本次 `delay` 秒 |
| 截图 | `exec-out screencap -p`，校验 PNG 后保存 |
| 收尾 | 返回桌面、关闭目标应用、执行 `KEYCODE_SLEEP` 锁屏 |

独立截图执行“亮屏 → 解锁 → 等待 0.5 秒 → 截图”，保持当前界面及亮屏状态。设备测试执行“亮屏 → 解锁 → 返回桌面 → 等待 0.5 秒 → 截图”。这两种操作不启动或关闭目标应用，也不执行完整应用流程的收尾。

三个操作接口均接受可选 JSON 字段 `pin`，须为 4～16 位 ASCII 数字字符串，保留前导零。省略时使用上述无 PIN 流程。提供时，在 `dismiss-keyguard` 后等待 0.5 秒，读取 `dumpsys window policy` 的锁屏状态。已解锁则跳过输入，确认安全锁屏正在显示且未被遮挡时执行一次 `shell input text <pin>`，随后最多等待约 3 秒确认解除锁屏。此流程按已验证的数字输入后自动解锁行为实现。状态无法确认或解锁失败时终止主流程，完整应用操作仍执行收尾。

PIN 通过 POST JSON 请求体传入，仅保留在本次执行的内存中，不写入配置、请求账本、响应或日志。PIN 输入命令的错误详情使用固定文本，避免回显凭据。相同 `request_id` 继续返回原结果，PIN 不参与去重参数比较，修改 PIN 后重新执行须使用新的 `request_id`。重新启动控制端后沿用现有的中断恢复规则，不自动重放 PIN。

完整应用流程在手机操作开始后发生错误时，仍分别尝试三项收尾，一项失败不阻止后续收尾。主错误、收尾错误和已有截图分别保留；截图成功但收尾失败时，整体仍为失败。受理前校验失败不操作手机。

## 3. API 契约

### 3.1 通用约定

- 路径前缀为 `/api/v1`，所有接口要求 `Authorization: Bearer <api_key>`。
- POST 使用 JSON 对象，请求体上限为 16 KiB；不接受接口定义以外的字段。
- 手机操作要求 `request_id`，格式为 1～64 位字母、数字、`-` 或 `_`，同一控制端的所有操作共用编号空间。
- 可选字段省略时使用默认值，显式空字符串、错误类型及超出范围的参数返回校验错误。
- 时间使用带 `+08:00` 时区的 ISO 8601 字符串，响应禁止缓存。

### 3.2 接口与参数

| 方法与路径 | 输入 | 响应内容 |
| --- | --- | --- |
| `GET /health` | 无 | 版本、启动时间、运行秒数、是否忙及当前请求编号 |
| `GET /devices` | 无 | `devices` 数组，包含 `serial`、`state`、`usable`、`brand`、`model` |
| `POST /app/run` | `request_id`；可选 `serial`、`package`、`delay`、`pin` | 完整应用流程结果 |
| `POST /screenshot` | `request_id`；可选 `serial`、`pin` | 当前界面截图结果 |
| `POST /device/test` | `request_id`；可选 `serial`、`pin` | 桌面截图及测试结果 |
| `GET /requests/{request_id}` | 路径中的请求编号 | 已有状态或结果，只查询，不触发手机操作 |

表中路径均相对于 `/api/v1`。`delay` 为 `0～max_delay` 的整数秒数。无设备时设备接口返回 `200` 和空数组；ADB 无法运行或查询超时则返回错误。品牌和型号读取失败时可为 `null`，仅 ADB `device` 状态标记为可用。

应用操作请求示例：

```json
{
  "request_id": "9c635d0e8fa84728b0a994f7c29d6653",
  "package": "com.tencent.wework",
  "delay": 10
}
```

### 3.3 结果结构

首次操作采用同步响应，正常情况下完成执行及收尾后返回结果。独立工作线程在调用方断开连接后继续执行，调用方可通过查询接口取得结果。

| 字段 | 含义 |
| --- | --- |
| `request_id`、`operation`、`status` | 请求编号、操作类型和执行状态 |
| `serial`、`package`、`delay` | 首次受理实际采用的参数，应用参数仅用于应用操作 |
| `created_at`、`started_at`、`finished_at` | 受理、开始和结束时间，未发生的时间为 `null` |
| `current_step`、`steps` | 当前步骤，以及结果中的步骤列表；步骤记录名称、状态与错误 |
| `screenshot` | 结果中的 PNG 对象，含 `content_type`、`captured_at`、`base64`；未取得截图时为 `null` |
| `cleanup` | 应用操作的 `home`、`force_stop`、`lock` 收尾结果 |
| `error`、`cleanup_errors` | 主错误与收尾错误；错误对象含 `code`、`message`、`step` |

受理前错误仅返回请求编号（可为 `null`）及错误对象，不生成执行记录。终态 `succeeded` 表示手机操作、截图与所需收尾均完成，实际业务结果由用户查看截图确认。

### 3.4 状态与 HTTP 响应

| 请求状态 | 含义 |
| --- | --- |
| `accepted` | 已持久化受理记录，尚未执行 |
| `running` | 正在执行，可查询当前步骤 |
| `succeeded` | 操作、截图及所需收尾完成 |
| `failed` | 已知的操作或收尾失败 |
| `unknown` | 执行中断，无法确认最终结果 |

| HTTP 状态 | 场景与错误码 |
| --- | --- |
| `200` | 操作成功，或查询取得已有状态；必须同时检查业务 `status` |
| `202` | POST 使用已有编号，原请求仍处于 `accepted` 或 `running` |
| `400`、`413` | 参数、请求体格式或大小错误，`INVALID_ARGUMENT` |
| `401` | 密钥缺失或错误，`UNAUTHORIZED` |
| `404` | 查询编号不存在，`REQUEST_NOT_FOUND` |
| `409` | `DEVICE_BUSY`、`DEVICE_AMBIGUOUS` 或 `REQUEST_CONFLICT` |
| `410` | 完整结果过期，`RESULT_EXPIRED`，保留可得的终态与参数 |
| `422` | 目标应用不可用，`PACKAGE_UNAVAILABLE` |
| `500` | 内部错误或结果文件不可读，`INTERNAL_ERROR`、`RESULT_UNAVAILABLE` |
| `502` | ADB 执行、启动应用或截图失败，如 `ADB_FAILED`、`APP_LAUNCH_FAILED` |
| `503` | 设备、ADB 不可用或服务正在停止 |
| `504` | 控制端确认 ADB 命令超时，`ADB_TIMEOUT` |

查询已有失败或未知状态也返回 `200`；`410` 单独表示完整结果过期。调用方应同时读取 HTTP 状态、业务状态和错误对象。

## 4. 互斥与幂等

应用操作、独立截图和设备测试共用一把执行锁，同一时刻只执行一个操作。新编号在忙时返回 `409 DEVICE_BUSY`，不受理、不排队；健康检查、设备列表和结果查询仍可使用。

控制端先检查已有编号，再判断是否忙：

1. 新编号通过校验且设备空闲后，保存编号、操作类型和实际参数，再开始执行。
2. 相同编号、相同操作及兼容参数返回已有状态或结果，始终不重复执行。
3. 参数省略时沿用首次受理值；显式值与首次记录冲突时返回 `409 REQUEST_CONFLICT`。
4. 完整结果过期后仍返回 `410`，不将历史编号重新视为新操作。

编号绑定实际设备、包名和等待时间，因此修改默认配置不影响历史编号。管理端的超时查询及再次提交策略见[管理端设计](04-manager-design.md)。

## 5. 存储与恢复

SQLite `requests` 表保存编号、操作、参数快照、状态、当前步骤、时间、HTTP 状态、结果、截图路径及过期标记。请求编号为主键，是去重依据。

完整结果和截图默认保留 24 小时，后台每分钟检查清理，读取请求时也检查过期。过期后删除完整结果和截图，保留编号、参数绑定和终态。截图以 PNG 文件保存，通过 JSON Base64 返回。

重启时将历史 `accepted`、`running` 请求转为 `unknown`，保留已结束请求的原终态，不重做手机操作。进程中断、主机断电等情况可能无法完成收尾，恢复依赖原数据目录中的请求账本。

ADB 调用使用参数数组，设备和包名经过校验。API 只提供定义的手机操作，不接收任意 ADB 或 shell 命令。
