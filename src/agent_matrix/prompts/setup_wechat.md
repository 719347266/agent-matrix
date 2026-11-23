# 接入微信（给 AI 看的完整步骤）

心响不自己读微信：读微信全部交给开源项目 wechat-cli（它在本机读微信自己的聊天库，不登录、不调用微信接口，所以不碰风控）。
心响只是调用它的命令（`sessions`、`history`、`new-messages`），只读、不上传、不发消息。

- 源码与说明（安装和初始化以这里的 README 为准，下面是按我们踩过的坑补充的）：https://github.com/freestylefly/wechat-cli
- npm 包：`@canghe_ai/wechat-cli`（会按系统自动带上对应的程序：macOS Apple 芯片 / Intel、Windows x64、Linux）
- 数据位置：`~/.wechat-cli/config.json`（配置）、`~/.wechat-cli/all_keys.json`（读取钥匙，**绝不打印、不外传**）

先用一句话告诉本人：要做什么（让心响能读到他电脑上的微信聊天，只在本机读）、大概 5 分钟、中途要他做哪一两件事。

## 1. 先查电脑上有什么（你自己查，不用问他）

- `node --version`：没有就先装 Node.js（见配置检查里的步骤）。
- `wechat-cli --version`：有就跳到第 3 步。
- 微信要已经打开并登录（看进程或问他一句「微信现在开着吗」）。

## 2. 安装

```
npm install -g @canghe_ai/wechat-cli
```

- 提示没有权限（macOS）：不要用 sudo，先 `npm config set prefix ~/.npm-global`，把 `~/.npm-global/bin` 加进 PATH，再装。
- 装完 `wechat-cli` 找不到：重新打开终端；还不行就用 `npm root -g` 找到包，直接运行里面 `node_modules/@canghe_ai/wechat-cli-<系统>/bin/wechat-cli`。
- 提示 `platform package ... not installed`：`npm install -g --force @canghe_ai/wechat-cli`。

## 3. 提取读取钥匙（init，只做一次；要本人在场）

### macOS

1. 请本人打开「系统设置 → 隐私与安全性 → 完全磁盘访问权限」，把「终端」（或你所在的应用）打开，然后**重开终端**。一句话说清为什么：微信的聊天文件在受保护的目录里。
2. 在终端运行（会要本人输一次**电脑开机密码**；先告诉他，密码只输在终端里，不要发给你）：
   ```
   sudo env HOME="$HOME" "$(npm root -g)/@canghe_ai/wechat-cli/node_modules/@canghe_ai/wechat-cli-darwin-arm64/bin/wechat-cli" init --force
   ```
   Intel 芯片把 `darwin-arm64` 换成 `darwin-x64`。用这个原生程序，不用 `wechat-cli` 那层 node 外壳（sudo 下常缺库）。
3. 出现多个账号目录时，选数据库修改时间最新的那个（就是现在登录的号）；不要把账号标识说给本人以外的人。

### Windows

1. 保持微信打开并登录。
2. 用**管理员身份**打开 PowerShell（请本人：开始菜单搜 PowerShell → 右键「以管理员身份运行」），运行 `wechat-cli init --force`。
3. 被杀毒软件拦了：请本人在提示里选「允许」，或把 wechat-cli 加进白名单，再跑一次。

## 4. 确认真的能读（以这里为准）

- `wechat-cli sessions --limit 3` 能列出最近的聊天，就算成功。
- 0.2.4 版结尾可能写「提取到 0 个数据库密钥」，这句不准，以 `sessions` 能列出来为准。
- 然后打开心响里的微信：`xinxiang-setup set wechat_enabled=true`，再运行 `xinxiang-setup doctor --only wechat`。
- 告诉本人：「微信接好了，以后私聊和群里找你的事会自动收进来。只在这台电脑上读。」

## 出错怎么办

| 看到 | 怎么回事 | 做什么 |
|---|---|---|
| `已初始化 … 使用 --force` | 以前初始化过 | 加 `--force` 再跑 |
| `密钥文件不存在` | 还没 init | 做第 3 步 |
| `无法解密 session.db` | 钥匙过期或是别的账号的 | 微信重新登录后再 `init --force` |
| `Matched 0/0 keys` | 终端没有完全磁盘访问权限，或选错了账号目录 | 回到 3 的第 1 步，重开终端 |
| `task_for_pid failed`（macOS） | 系统不让读微信内存 | 工具可能建议**重新签名微信**：这会改动微信应用、可能影响自动更新。先把这一点讲清楚，本人同意了再做；不同意就停在这里，告诉他可以先不接微信 |
| 重新签名后微信打不开 | 签名权限不对 | 从官网重装微信即可恢复，不要自己加别的权限 |
| 最近的消息读不出来、只看到每个聊天最后一条 | 微信换了数据库钥匙 | 微信开着时再 `init --force` 一次 |

## 不要做

- 不打印、不上传 `all_keys.json`、数据库文件、聊天导出。
- 不替本人发微信消息，不读和接入无关的聊天内容。
- 不在对话里要他的开机密码：请他直接输在终端里。
