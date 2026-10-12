# Fab X 批量 Mint 控制台

针对 [fabx.tech](https://fabx.tech)（X Layer，chainId 196）的晶体管（Transistors，ERC1155）
批量铸造工具。合约 ABI 通过逆向站点前端产物获得，账号、任务与执行日志统一存放在 SQLite。

- **命令行**：`fabx`（基于 Typer），覆盖账号导入、CPU 同步、任务编排、资金分发与定时自动 mint；
- **Web 控制台**：FastAPI + Jinja2，单页无构建，浏览器里点选即可完成全流程；
- **配置**：`pydantic-settings`，全部通过 `FABX_` 前缀环境变量或 `.env` 注入；
- **依赖与环境**：`uv` + Python 3.12。

> 本工具仅供学习与自用研究。请遵守目标站点与合约的使用条款，并自行评估链上操作风险。

## 目录

- [功能概览](#功能概览)
- [快速开始](#快速开始)
- [安装](#安装)
- [配置](#配置)
- [准备账号与资金](#准备账号与资金)
- [命令行用法](#命令行用法)
- [定时调度（到点自动 mint）](#定时调度到点自动-mint)
- [mint 费用](#mint-费用)
- [Web 控制台](#web-控制台)
- [执行语义](#执行语义)
- [安全说明](#安全说明)
- [ABI 逆向来源](#abi-逆向来源)
- [合约状态备注](#合约状态备注)
- [常见问题](#常见问题)
- [测试](#测试)
- [项目结构](#项目结构)

## 功能概览

| 能力 | 说明 |
| --- | --- |
| 账号管理 | 私钥整批导入，AES-256-GCM 加密入 SQLite，支持分组、启停、查余额 |
| 批量 mint | 一个任务可对多账号 × 多目标（NAND / LATCH）批量铸造，按链上剩余额度自动封顶 |
| 并发模型 | 默认 5 个账号并行；同一账号内多笔严格按 nonce 串行，绝不互相顶替 |
| 自动对账 | `sent` 交易按 tx hash 对账链上回执，重跑不会重复广播，避免双花 |
| 资金分发 | 从主钱包按分组 / ID 给账号补足或追加 OKB |
| 定时调度 | `fabx auto` 到点自动定位最新或新增合约再批量 mint，支持提前抢跑 |
| Web 控制台 | 账号、CPU、任务、资金四个页签，实时查看逐账号结果 |

## 快速开始

```bash
# 0. 准备环境（需要 uv 与 Python 3.12）
git clone <你的仓库地址> && cd fabx-minter
uv sync

# 1. 准备口令与配置
cp .env.example .env
#   编辑 .env，至少设置 FABX_SECRET=<你的私钥加密口令>

# 2. 导入账号（文件每行一个私钥，可写 `私钥,备注`）
uv run fabx account import keys.txt --group batch-01

# 3. 给账号充 OKB（主钱包私钥只放在文件里，不落库）
uv run fabx fund --key-file master.key --amount 0.002 --group batch-01

# 4. 确认目标合约状态与价格
uv run fabx cpu sync && uv run fabx cpu list
uv run fabx cpu state 0x9d2e9B9c668877C34cd1F5836338adCF724d0Ec1

# 5. 先 dry-run 预览，再真实执行
uv run fabx job create --transistors 0x9d2e...0Ec1 --token-id 0 --amount 1 \
  --group batch-01 --dry-run
uv run fabx job run 1
uv run fabx job create --transistors 0x9d2e...0Ec1 --token-id 0 --amount 1 --group batch-01
uv run fabx job run 2
```

也可以直接开 Web 控制台操作：

```bash
uv run fabx web        # 打开 http://127.0.0.1:8787
```

## 安装

```bash
cd fabx-minter
uv sync
```

Python 版本由 `.python-version` 固定为 3.12（web3 尚不支持 3.13+）。

## 配置

复制 `.env.example` 为 `.env` 后按需修改，常用项：

| 变量 | 默认 | 说明 |
| --- | --- | --- |
| `FABX_SECRET` | 空 | 私钥加密口令；不设置则每次交互式输入 |
| `FABX_DB_PATH` | `var/fabx.db` | SQLite 路径（相对路径基于项目根目录） |
| `FABX_CONCURRENCY` | `5` | 并行处理的**账号数**，必须 ≥ 1 |
| `FABX_GAS_MULTIPLIER` | `1.15` | gas 价格上浮系数，必须 ≥ 1.0 |
| `FABX_GAS_PRICE_GWEI` | `0` | 固定 gas 价；0 表示取链上 `gasPrice` |
| `FABX_GAS_LIMIT` | `0` | 固定 gas 上限；0 表示链上估算 |
| `FABX_TX_TIMEOUT` | `180` | 等待回执超时（秒） |
| `FABX_MAX_RETRIES` | `2` | 单笔广播重试次数 |
| `FABX_WEB_HOST` / `FABX_WEB_PORT` | `127.0.0.1` / `8787` | Web 监听地址 |
| `FABX_WEB_TOKEN` | 空 | Web 访问令牌；监听非回环地址时必填 |

私钥以 AES-256-GCM 加密后存入 SQLite（scrypt 派生密钥，每个账号独立 salt/nonce），
明文只在签名瞬间出现在内存中，不会写日志。

`FABX_WEB_TOKEN` 是 Web 控制台的访问令牌：一旦设置，所有 `/api/*` 业务接口
（除健康检查 `/api/health`）都必须携带 `X-FABX-Token` 请求头或
`Authorization: Bearer <token>`，否则返回 401。页面顶部可直接填入该令牌，
保存在本机浏览器里。为避免控制台在公网裸奔，当 `--host` 不是回环地址
（127.0.0.1 / ::1 / localhost）却未配置令牌时，`fabx web` 会直接拒绝启动。

## 准备账号与资金

1. **导入账号**：`keys.txt` 每行一个私钥（可带 `0x` 前缀），也支持 `私钥,备注`；
   导入时用 `FABX_SECRET` 加密，并校验私钥能推导出地址。

   ```bash
   uv run fabx account import keys.txt --group batch-01 --label-prefix acc
   uv run fabx account list --balance
   ```

2. **给账号充 OKB**：把主钱包私钥写进单行文件（如 `master.key`），只经文件读取、不落库；
   `--amount` 是每个账号的目标余额，加 `--append` 则改为等额追加。

   ```bash
   uv run fabx fund --key-file master.key --amount 0.002 --group batch-01
   ```

3. **估算要充多少**：见 [mint 费用](#mint-费用)。简言之，每个账号至少要能覆盖
   `协议费 + 单价 × 数量 + gas`；余额不足的账号会被标为 `skipped`，不影响其他账号。

## 命令行用法

```bash
# 账号
uv run fabx account import keys.txt --group batch-01 --label-prefix acc
uv run fabx account list --balance
uv run fabx account toggle 3 --enabled
uv run fabx account remove 3

# CPU / 合约
uv run fabx cpu sync
uv run fabx cpu list
uv run fabx cpu state 0x9d2e9B9c668877C34cd1F5836338adCF724d0Ec1

# 任务：先 dry-run 预览，再真实广播
uv run fabx job create --transistors 0x9d2e...0Ec1 --token-id 0 --amount 1 \
  --name "FAB_No1 NAND x1" --group batch-01 --dry-run
uv run fabx job list
uv run fabx job run 1          # dry-run 任务只做标记，不广播
uv run fabx job show 1         # 逐账号结果与事件
uv run fabx job reset 1        # 把 failed/skipped 重置为 pending；sent 保留待对账

# 资金
uv run fabx fund --key-file master.key --amount 0.01 --group batch-01
uv run fabx balance 0x...

# 定时自动 mint（见下一节）
uv run fabx auto --at 22:00 --amount 1 --group batch-01

# Web
uv run fabx web --host 127.0.0.1 --port 8787
```

> `token-id` 取值见 `fabx cpu state`：`NAND()` 与 `LATCH()` 给出两个 id（当前部署为
> NAND=0、LATCH=1）。`--transistors` 是**晶体管合约**地址，不是 CPU / 电路合约地址。

## 定时调度（到点自动 mint）

`fabx auto` 到点后自动定位目标合约并批量铸造，口令在启动时先解析好（环境变量或
交互输入），避免到点再输入而错过开场。

```bash
# 今晚 22:00（上海时区）对分组里的账号各铸 1 份当前最新合约
uv run fabx auto --at 22:00 --amount 1 --group batch-01

# 活动会部署新合约：提前 5 秒开始探测，新合约一出现就抢跑
uv run fabx auto --at 22:00 --target new --lead-seconds 5 --amount 1 --group batch-01
```

参数：

| 参数 | 默认 | 说明 |
| --- | --- | --- |
| `--at` | 空 | `HH:MM`（上海时区，已过则顺延次日）或 ISO8601；留空=下一个 10:00 |
| `--amount` / `-n` | `1` | 每账号铸造数量 |
| `--accounts` / `-a` | 空 | 账号 ID，逗号分隔 |
| `--group` / `-g` | 空 | 按分组筛选账号 |
| `--target` | `latest` | `latest`=当前最新 CPU；`new`=等工厂新增 CPU 再动手 |
| `--token-ids` | 空 | token id，逗号分隔；留空=该合约 NAND + LATCH 都铸 |
| `--lead-seconds` | `0` | `target=new` 时提前多少秒进入密集探测（抢跑窗口） |
| `--poll-interval` | `1.0` | 探测 / 重试间隔秒数 |
| `--dry-run` | 关 | 只落库不广播 |
| `--name` | 自动 | 任务名称 |

到点只是「建任务 + 执行」，真正的签名广播仍走与手动模式完全相同的引擎：余额预检、
售罄跳过、nonce 串行、链上对账、`FABX_CONCURRENCY` 并发账号。若到点时没有可铸额度
（例如仍是已售罄的旧合约），任务会全部落到 `skipped` 而不是报错。

## mint 费用

`mint` 是 payable，单笔交易需要发送的 value 为：

```
value = protocolFee + mintPrice × amount
```

- **`protocolFee`**：合约协议费，由 `protocolFee()` 给出，**按每笔交易固定收取一次**，
  与数量无关。链上实测约 **0.00065 OKB / 笔**（以合约与回执为准）；
- **`mintPrice`**：单价，由 `mintPrice()` 给出；
- **gas**：单独消耗，不包含在 value 里。当前网络 gasPrice 很低，单笔 `mint` 约 89k gas。

举例（`FAB_No1`，`mintPrice = 0.0001 OKB`、`protocolFee = 0.00065 OKB`）：

| 每账号数量 | value = 协议费 + 单价×数量 | 折合 |
| --- | --- | --- |
| 1 | 0.00065 + 0.0001 | ≈ 0.00075 OKB |
| 10 | 0.00065 + 0.001 | ≈ 0.00165 OKB |
| 100 | 0.00065 + 0.01 | ≈ 0.01065 OKB |

**省费要点**：协议费是「每笔」收一次，拆成多笔就多付。默认对 NAND + LATCH 各发一笔，
即每账号 2 笔协议费；想省钱就用 `--token-ids` 只买需要的那个，并把数量合并到一笔。
随时可用 `fabx cpu state <地址>` 读出当前「单价」与「协议费」。

## Web 控制台

```bash
uv run fabx web
# 打开 http://127.0.0.1:8787
```

页面包含四个页签：

1. **账号**：粘贴私钥批量导入、启停、查看余额、删除；
2. **CPU / 合约**：同步官方列表，一键把某个晶体管合约设为铸造目标，并读取 NAND/LATCH id；
3. **任务**：预览花费、创建任务、后台执行、查看逐账号结果与事件；
4. **资金分发**：从主钱包向筛选出的账号补足 OKB。

页面顶部的“操作口令”即 `FABX_SECRET`，用于加密 / 解密私钥，仅缓存在本机浏览器；
若后端配置了 `FABX_WEB_TOKEN`，还需在同一区域填入“Web 访问令牌”。

口令只经请求体传递，绝不放进 URL 查询串——查询串会被服务器访问日志原样记录，
放入查询串等于把明文口令写进日志。新增账号（`POST /api/accounts`）与批量导入
（`POST /api/accounts/import`）都在请求体的 `password` 字段中携带口令，
未显式提供时回退到服务端 `FABX_SECRET`。

## 执行语义

- 规划阶段会读取链上 `mintPrice`、`protocolFee`、`typeCap`、`mintedOf`，按剩余额度自动下调数量；
  每笔交易的应付 value = `protocolFee + mintPrice * amount`：协议费按**每笔交易固定收取一次**
  （与数量无关），规划与余额预检都已把它计入，否则发出的交易会因 value 不足直接 revert；
  拆分出的每一笔独立交易（例如 NAND、LATCH 各发一笔）会各收一次协议费；
  剩余额度取「`typeCap - mintedOf`」与「`supplyCap - minted`」的较小值：
  `typeCap` 未单独设限（返回 0）时仍受全局 `supplyCap` 约束，避免在接近总上限时
  按满额批量放行、超出的交易白白 revert 烧 gas；剩余为 0 即视为**售罄**，
  该目标会被跳过且不发起任何交易；
  同一合约下多个目标（如 NAND + LATCH）共享全局 `supplyCap` 预算，规划时按合约
  累计已安排数量，跨目标合计也不会突破总上限；
  只会调用 ABI 中真实存在的只读方法，RPC 抖动导致的读取失败会直接报错，不会误判成“不限量”；
- Web 接口按错误性质区分状态码：参数 / 筛选条件等**客户端问题**返回 `400`；
  链上状态读不到（RPC 抖动 / 超时）属于**上游故障**，`/api/plan` 与 `/api/jobs`
  返回 `502`，避免让用户误以为是自己填错了参数；
- 账号筛选区分「不筛选」与「明确不选」：不传 `account_ids`（`null`）表示全部启用账号；
  显式传入空列表 `[]` 表示未选中任何账号，会直接报错（`400`）而**不会**退化成对全部账号
  mint / 转账，避免误操作波及整批账号；
- 同一账号的任务严格按 nonce 递增顺序发送，不同账号在线程池中并行（`FABX_CONCURRENCY`）；
- 同一进程内同一时刻只执行一个任务（执行闸门串行化），避免不同任务选中同一批账号时
  各自按 pending nonce 广播而相互顶替；Web 端第二个任务会得到 `409`；
- 每笔任务的发送 / 确认 / 失败都会写回 SQLite；`sent` 表示已广播但尚未确认，
  重跑时会先按 tx hash 对账链上回执：确认成功则记为 `confirmed`，确认失败则记为 `failed`，
  仍找不到回执则保持 `sent`、绝不重复广播，避免双花；
- `fabx job reset` 重置 `failed` 与 `skipped`（如余额不足、售罄）任务，便于充值或额度恢复后重跑；
  `sent` / `confirmed` 任务不重置，`sent` 因可能已上链而保留，需人工确认后再处理；
- 余额不足以支付 `protocolFee + mintPrice * amount + gas` 的账号会被标记为 `skipped`，不会阻塞其他账号；
  任务进度把 `confirmed` / `failed` / `skipped` 都算作已收尾，售罄或全部跳过时进度同样会归 100%；
- 资金分发里，只有**广播失败**（交易未进入内存池）才记 `failed` 并重新对齐 nonce；
  广播成功但等待回执超时会记 `pending`（保留 `tx_hash`），提示用户自行核对而不归入失败，
  避免误以为没转成功而重复转账。

## 安全说明

- 数据库里**从不保存明文私钥**：每个账号独立随机 salt/nonce，scrypt 派生密钥后 AES-256-GCM 加密。
- 私钥口令只来自环境变量 `FABX_SECRET` 或交互输入，**不落库、不写日志**；
  Web 场景下口令放在请求体，绝不放进 URL 查询串。
- `.env`、`*.key`、SQLite 数据库与运行日志已在 `.gitignore` 中排除，不会被提交。
- Web 控制台默认只监听 `127.0.0.1`；对外监听必须配置 `FABX_WEB_TOKEN`，否则拒绝启动。
- 请自行保管 `.env`、主钱包私钥文件与 `var/fabx.db`，它们等同于账号的控制权。

## ABI 逆向来源

站点前端打包产物里内联了 13 套 ABI：

- `https://fabx.tech/assets/abi.generated-CtIRDRwc.js`
- `https://fabx.tech/assets/config-DJaFdDfO.js`（合约地址）

提取结果保存在 [`src/fabx_minter/abi/fabx.abi.json`](src/fabx_minter/abi/fabx.abi.json)，
键名与前端导出一致（`transistorsAbi`、`circuitsAbi`、`circuitFactoryAbi` …）。

关键事实：

| 项目 | 值 |
| --- | --- |
| 网络 | X Layer，chainId `196` |
| RPC | `https://rpc.xmake.org/rpc/196` |
| 工厂合约 | `0x138fA3B38b1461B5908C455ABD30fC55029b7e71`（`cpuCount()` / `cpuAt(i)`） |
| 晶体管合约（mint 目标） | `0x9d2e9B9c668877C34cd1F5836338adCF724d0Ec1`（`FAB_No1`） |
| mint 方法 | `mint(uint256 id, uint256 amount)`，payable |
| 应付金额 | `protocolFee() + mintPrice() * amount` |
| token id | `NAND()` / `LATCH()` 分别给出 id 0 与 1 |
| 官方 CPU 列表 | `https://api.fabx.tech/v1/processors` |

一次性核验命令：

```bash
uv run fabx cpu state 0x9d2e9B9c668877C34cd1F5836338adCF724d0Ec1
```

## 合约状态备注

逆向时点（2026-10 上旬）核验：

- `FAB_No1`（`0x9d2e9B9c668877C34cd1F5836338adCF724d0Ec1`）**已售罄**：
  链上 `minted = 1,000,000`、`supplyCap = 1,000,000`，NAND / LATCH 剩余额度均为 0，
  累计 `Minted` 事件 1,000,000 笔、覆盖 743 个地址、实付 101.8642 OKB。
  该合约 ABI 不含 `startTime` / `paused` / `phase`，无“未开始”状态可言——除非 owner 提额，
  否则无法再 mint。
- `RefBench`（`0xBCcf156bCc711Ade5D62E6aCD4F0D4567Ada4C3D`）**已售罄**：
  链上 `minted = 14,000`、`supplyCap = 14,000`，NAND / LATCH 剩余额度均为 0。
  两个已知合约均已售罄，当前工厂 `cpuCount() = 2`；`fabx auto --target new` 就是为
  等待下一个新合约部署而准备的。

## 常见问题

- **是“已售罄”还是“没开始”？** 这两个合约的 ABI 里没有 `startTime` / `paused` / `phase`，
  不存在“未开始”这种状态。链上 `minted == supplyCap`、剩余额度为 0，就是**售罄**。
- **任务全部 `skipped`？** 常见原因是合约已售罄，或账号 OKB 不足以覆盖
  `协议费 + 单价×数量 + gas`。用 `fabx cpu state <地址>` 与 `fabx account list --balance` 排查。
- **报 value 不足 / 交易 revert？** 多半是漏算协议费。本工具已把 `protocolFee` 计入
  `value`，升级到最新版本即可。
- **`sent` 是失败吗？** 不是。它表示已广播但还没确认，重跑只会对账、不会重复广播；
  等网络确认，或手动核对 `tx_hash`。
- **能同时跑多个任务吗？** 同一进程内同一时刻只跑一个任务（防止 nonce 冲突）；
  并发体现在**同一任务内的多个账号**上，由 `FABX_CONCURRENCY` 控制。

## 测试

```bash
uv run pytest
```

覆盖私钥加解密与错误口令、ABI 完整性、SQLite 账号 / 任务状态机、Web 账号生命周期与任务编排、
定时调度的时间解析与抢跑探测、协议费计入花费（链上调用以替身拦截，测试不访问网络）。

## 项目结构

```
fabx-minter/
├── src/fabx_minter/
│   ├── abi/fabx.abi.json   # 逆向提取的 13 套 ABI
│   ├── chain.py            # 链上读写：状态、价格、协议费
│   ├── minter.py           # 批量 mint 引擎（规划 / 广播 / 对账）
│   ├── scheduler.py        # 定时调度与自动定位合约
│   ├── funding.py          # 主钱包资金分发
│   ├── crypto.py / accounts.py / db.py / models.py / config.py
│   ├── cli.py              # Typer 命令行入口
│   └── web/                # FastAPI + Jinja2 控制台
├── tests/                  # pytest（不访问网络）
└── pyproject.toml
```
