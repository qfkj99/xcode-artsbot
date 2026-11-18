# Xcode（astrbot_plugin_xcode）

群里有人发送状态关键词（默认 `状态检查`，**完全匹配，不需要 @ 机器人**），机器人会去请求 Xcode 分组监控快照接口，然后回复监控截图和一段状态摘要。

回复示例：

```
📊 分组状态
✅ 正常 11 ｜ ⚠️ 降级 3 ｜ ❌ 异常 3 ｜ ⏳ 暂无数据 2
❌ 异常：GLM-官方、Claude-Cursor、Gemini-Ultra
⚠️ 降级：Qwen-官方、ChatGPT-image、Grok-Heavy
⏳ 暂无数据：Grok-image、ChatGPT-Adobe
🕒 统计时段：11-18 21:33 ~ 22:33
[监控截图]
```

- 环境要求：AstrBot ≥ 4.29（在 4.29.4 上测试通过）
- 适用平台：QQ 官方机器人（Webhook / WebSocket）
- 依赖：无需额外安装（aiohttp 是 AstrBot 自带的）

## 一、安装（Docker 部署）

仓库是私有的，WebUI 的「从链接安装」没法直接下载，所以从下面两种方式里**选一种，并且以后一直用同一种**。混着用会互相覆盖，比如上传 ZIP 会把 git clone 出来的目录整个替换掉。

### 方式 A：服务器上 git clone（推荐，以后更新只要一条命令）

**1. 找到 AstrBot 数据目录在宿主机上的路径**

```bash
docker ps    # 找到 AstrBot 容器名，下面记作 <容器名>
docker inspect <容器名> --format '{{range .Mounts}}{{.Source}} -> {{.Destination}}{{println}}{{end}}'
```

找到 `-> /AstrBot/data` 那一行，箭头左边就是宿主机路径，下面记作 `<DATA>`。

**2. 给服务器配置仓库只读权限（Deploy Key，只需要做一次）**

```bash
ssh-keygen -t ed25519 -f ~/.ssh/xcode_artsbot -N "" -C "astrbot-server"
cat ~/.ssh/xcode_artsbot.pub
```

把输出的那一行公钥加到 GitHub 仓库 → Settings → Deploy keys → Add deploy key，**不要**勾选 Allow write access。

**3. 克隆到插件目录**

```bash
GIT_SSH_COMMAND="ssh -i ~/.ssh/xcode_artsbot -o IdentitiesOnly=yes" \
  git clone git@github.com:qfkj99/xcode-artsbot.git <DATA>/plugins/astrbot_plugin_xcode

cd <DATA>/plugins/astrbot_plugin_xcode
git config core.sshCommand "ssh -i ~/.ssh/xcode_artsbot -o IdentitiesOnly=yes"
```

> 如果 `<DATA>` 目录属于 root，没有写权限，就先执行 `sudo -i` 切换到 root，再把第 2、3 步全部做一遍（密钥也要生成在 root 下）。

**4. 重启容器加载插件**

```bash
docker restart <容器名>
```

然后打开 WebUI → 插件，确认列表里有 **Xcode**，并且状态是已启用。

**以后更新**

```bash
cd <DATA>/plugins/astrbot_plugin_xcode && git pull
```

然后在 WebUI → 插件 → Xcode → 重载插件（或者 `docker restart <容器名>`）。

### 方式 B：WebUI 上传 ZIP（不用在服务器上配置 git）

1. 浏览器登录 GitHub，打开本仓库 → Code → Download ZIP，会得到 `xcode-artsbot-main.zip`。
2. AstrBot WebUI → 插件 → 安装插件 → 从文件安装 → 上传这个 ZIP。
3. 插件会被装到 `data/plugins/astrbot_plugin_xcode`。

以后更新就重复上面三步：上传新的 ZIP 会覆盖旧版本，插件配置不会丢（配置单独保存在 `data/config/astrbot_plugin_xcode_config.json`）。

## 二、QQ 官方机器人设置（不 @ 触发的前提）

QQ 默认只把 **@ 机器人** 的群消息推给 AstrBot。要实现不 @ 就触发，下面两处都要设置：

1. **QQ 开放平台** → 机器人管理 → 开发 → 回调配置：勾选群事件 `GROUP_MESSAGE_CREATE`，保存后重启 AstrBot。
2. **手机 QQ** → 目标群 → 群设置 → 机器人设置：把「机器人可获取的群聊消息范围」改成「获取群内全部消息」。

如果暂时没开这两项，群成员发送 `@机器人 状态检查` 也能正常触发。

## 三、插件配置

WebUI → 插件 → Xcode → 配置：

| 配置项 | 说明 | 默认值 |
|---|---|---|
| API Token | 接口 Token，只填 Token 本身（不用带 `Bearer`） | 空，**必填** |
| New-Api-User | 请求头 `New-Api-User` 的值 | `2` |
| 快照接口地址 | 接口 URL | `https://xcodebest.com/api/group-monitor/snapshot/details` |
| 统计时间范围 | 作为 `range` 参数传给接口 | `24h` |
| 请求超时（秒） | 超时就回复失败提示 | `15` |
| 触发关键词 | 可以设多个，消息内容必须和其中一个完全一致（会忽略首尾空格） | `状态检查` |
| 仅在白名单群中响应 | 开启后只有「群配置」里的群才会响应 | 开启 |
| 群配置 | 每个群一条：群会话（下拉选择）、备注、是否启用、冷却秒数 | 空 |
| 默认冷却时间（秒） | 关闭白名单后，没单独配置的群用这个值 | `5` |
| 开头自定义文字 | 显示在摘要上方 | 空 |
| 显示状态摘要 | 关掉后只发自定义文字和截图 | 开启 |
| 结尾自定义文字 | 显示在摘要下方 | 空 |
| 查询失败时的回复 | 接口出错时回复的内容 | `状态查询失败，请稍后再试。` |

### 怎么添加群

不用手动查群 ID，直接从 AstrBot 记录过的会话里选：

1. 插件配置 →「群配置」→ 添加一条。
2. 点「群会话」，在弹出的「选择会话来源」里按群名搜索，选中目标群（蓝色「群聊」标签）。
3. 按需填写备注、冷却秒数，保存。

**列表里找不到目标群？** 只有和机器人有过互动的群（有人 @ 过机器人、或者触发过插件）才会被 AstrBot 记录。在目标群里发一次 `状态检查`（白名单外的群不会回复，但 AstrBot 会记下这个群），然后**刷新配置页面**，再打开选择器就能看到了。

如果选择器里只显示一串 ID、没有群名，可以在 WebUI 的会话管理里给这个会话设置别名；也可以去日志里搜索 `[Xcode]`，日志会打印群名和完整的会话 ID，复制后在选择器里手动粘贴。

说明：

- 开启了 AstrBot「会话隔离」时，同一个群会按成员拆成多个会话。选中其中任意一个，就等于选中了整个群。
- 会话里带着机器人实例 ID。如果你在 AstrBot 里删掉并重新创建了机器人，需要重新选择一次。

### 冷却规则

- 每个群单独计时，互不影响。
- 冷却期内重复发送关键词，机器人不会回复，也不会请求接口。
- 冷却秒数填 `0` 表示不限制。

## 四、注意事项

- QQ 官方机器人会拦截没有报备过的链接。如果自定义文字里带网址导致发送失败，请先在 QQ 开放平台报备该域名，或者去掉网址。
- 接口出错时，详细原因会写进日志（搜索 `[Xcode]`），日志里不会打印 Token；群里只显示「查询失败时的回复」。
- 接口返回成功但截图缺失时，机器人仍会发送文字摘要，并提示截图暂时获取失败。
