# IMAP 原始邮件与附件下载

QQ、网易与自定义企业 IMAP 共用下载、缓存、简历归组和去重引擎；Python 3.10+ 标准库，无额外包。连接使用 TLS 并校验证书与主机名，不做明文降级。

## 账号与通道

Windows/macOS 推荐在 Domi 中说“帮我配置网易邮箱用于批量下载”，由 Domi 运行 `python "<skill>/scripts/setup_mailbox.py" --provider netease` 打开本机表单。也可双击包内“配置邮箱.cmd”，选择 QQ 或网易并输入账号、授权码。Windows 助手保存到当前用户环境变量；macOS 使用原生账号输入框和隐藏授权码输入框，将账号及授权码作为一条记录保存到系统钥匙串，服务名 com.mail-export.credentials，按 qq/netease 隔离；密钥不放任务文件、不进入聊天。status_path 仅含 waiting/saved/cancelled。保存后仍需实际登录验证，失败时回到配置窗口修正。

首次配置后，脚本在进程变量缺失时读取相应 Windows 用户环境变量，Domi 可直接续跑；更新旧配置时进程变量优先，需重启 Domi。Mac 不依赖终端变量或 Finder 启动方式，脚本直接读取钥匙串；系统可能要求允许当前 Python 进程访问。环境变量仍优先，可用于远端环境；若显式设置过旧值，需更新/移除覆盖后再用钥匙串。Linux 暂无图形助手。

| provider | 邮箱范围 | 服务器 | 环境变量 |
| --- | --- | --- | --- |
| qq（默认） | qq.com、foxmail.com | imap.qq.com:993 | QQ_EMAIL_ACCOUNT / QQ_EMAIL_AUTH_CODE |
| netease | 163.com、126.com、yeah.net | imap.163.com、imap.126.com、imap.yeah.net，端口 993 | NETEASE_EMAIL_ACCOUNT / NETEASE_EMAIL_AUTH_CODE |
| custom | 管理员确认支持本登录方式的企业邮箱 | 用户提供的 TLS IMAP 主机及端口 | MAIL_IMAP_ACCOUNT / MAIL_IMAP_AUTH_CODE，或指定变量名 |

provider 由用户要求确定，不能因网易凭据缺失就改拉 QQ。普通导出配置的顶层写 provider；简历任务写 query.provider。网易 VIP、企业自有域名不自动当作免费邮箱；先确认服务端配置。

在邮箱网页设置中开启 IMAP 并生成客户端授权码（不是网页登录密码）。由用户在 **Domi 实际执行脚本的环境** 中配置完整邮箱账号和授权码。Windows 首次配置可即时读取用户变量，更新旧配置后重启 Domi；macOS 推荐用图形助手保存到钥匙串；显式环境变量仍优先。远端/容器运行则在远端配置。本机 D 盘只对本机可访问的运行进程有效。

不要求用户把授权码发到聊天，不写任务文件、日志或仓库，不读取 Domi 内部凭据库。已连接 Domi 邮箱不代表 Python 进程能取得其授权码。

QQ 与网易日常导出均不依赖内置浏览器登录。缺少环境变量时 CLI 返回 action_required（type=configure_local_credentials、missing_env、browser_login_required=false）；只提示这些缺失项，不把“没有脚本凭据”解释成“请登录内置浏览器”。Windows/macOS 可使用图形助手完成首次配置后直接续跑；Windows 已有进程凭据需要更新时重启 Domi。网页登录只可能用于用户首次开启 IMAP、生成授权码或处理服务商安全验证；不自动作为附件下载通道。

只在首次配置或诊断时创建连接配置，例如：

```json
{"provider":"netease"}
```

```text
python "<skill>/scripts/imap_export.py" --config "<连接JSON绝对路径>" --check-config
```

configured=true 只代表本地变量存在且格式可用，network_checked=false，不是登录成功。普通任务直接执行 prepare/导出即可，不需每次先查配置。

## 网易文件夹与登录

已知实际 IMAP 文件夹名称时直接使用。首次下载发件箱而名称未知，才列出一次：

```text
python "<skill>/scripts/imap_export.py" --config "<连接JSON绝对路径>" --list-mailboxes
```

返回解码后的 name、flags、selectable。优先依据实际 Sent 标志或已确认名称选择发件箱，不假设所有邮箱都叫 Sent Messages；无法确定时询问用户。LIST 不下载邮件、不选择或改变邮箱。

网易每个新连接（含并发与重连）在登录后查询服务器能力；服务器声明 ID 时发送真实客户端标识 mail-export，再只读选择文件夹。若认证/客户端访问策略拒绝，返回失败并停下检查授权码、IMAP 开关和账户安全提示，不轮流尝试网页登录密码或伪造其他客户端身份。

## 企业邮箱配置与边界

第一阶段支持明确服务器的 IMAP over TLS + LOGIN（账号与授权码/服务商允许的应用密码）。以下 host 是示例，必须替换为管理员确认的实际主机，不能从公司邮箱域名猜出：

```json
{
  "provider": "custom",
  "connection": {
    "host": "imap.company.example",
    "port": 993,
    "account_env": "MAIL_IMAP_ACCOUNT",
    "auth_code_env": "MAIL_IMAP_AUTH_CODE",
    "send_id": false
  },
  "mailbox": "INBOX",
  "month": "2026-08"
}
```

简历任务将以上对象放入 query，其他归档参数不变。port 默认 993，始终是隐式 TLS，不是 STARTTLS。两个变量名可省略；账号与密钥的值只存在环境中。仅当实际服务商需要且支持客户端 ID 时设 send_id=true。配置中不接受 password/token/auth 等秘密字段，也不提供关闭证书验证的开关。

custom 的账号、服务器和端口共同隔离缓存；不同企业邮箱不会因相同 UID 或地址误跳过。QQ 的旧身份、缓存键和命令保留兼容；同一邮箱若改用不同通道身份，不承诺跨通道去重，应保持原配置续跑。

OAuth2、Exchange/Graph API、仅 STARTTLS 或禁用 IMAP 的企业账户尚未适配。后续需先确认服务商、认证方式与管理员策略，再补相应认证/下载通道；不能宣称所有企业邮箱已经支持。现有连接器若有原始附件下载能力，可使用 [连接器映射](connector-tools.md)，不能把其 ID 当作 IMAP UID。

## 任务与执行

由 agent 通过文件工具创建 UTF-8 JSON，以下示例下载指定时间内最多 50 封邮件中的 PDF 附件：

```json
{
  "mailbox": "INBOX",
  "since": "2026-10-01",
  "before": "2026-11-01",
  "limit": 50,
  "mode": "attachments",
  "filename": "*.pdf"
}
```

示例日期必须按用户实际要求替换。`limit` 选择通过邮件筛选后的最新 N 封邮件，**不是 N 封有 PDF 的邮件，也不是 N 个附件**；如果用户要 N 封含目标附件的邮件，当前脚本需先扩大或取消邮件数量限制，在处理结果中核对，不能承诺 limit 有不同含义。

```text
python "<skill目录>/scripts/imap_export.py" --config "<任务JSON绝对路径>" --root "<本地目标绝对目录>"
```

脚本从环境变量读取凭据。任务文件只能包含以下字段，拼错字段会报错，避免筛选失效却全量下载：

| 字段 | 含义 |
| --- | --- |
| `provider` | qq（省略兼容 QQ）、netease、custom |
| `connection` | 仅 custom 使用的服务器及环境变量名；见上文，不得存授权码 |
| `month` | YYYY-MM，自动转换当月1日至次月1日，不能与 since/before 同时使用 |
| `mailbox` | 默认 `INBOX`；支持 IMAP 修改版 UTF-7 编码的中文文件夹名称 |
| `since` / `before` | YYYY-MM-DD，按 IMAP 服务器 INTERNALDATE 日期，起点包含、终点不包含；不是邮件头发送日期 |
| `limit` | 正整数，按 INTERNALDATE 与 UID 倒序选择最新 N 封 |
| `uids` | 明确的 IMAP UID 数组；与日期范围取交集，缺失或越界时报错；不能填连接器自己的 ID |
| `all` | 无日期、UID 或数量范围时，只有用户明确要求全量导出才设为 true |
| `from` / `subject` | 解码后的 From / Subject 中不区分大小写的文字匹配，支持中文 |
| `mode` | `attachments`（默认）、`raw`（只保存原始 EML）、`both` |
| `filename` | 附件原名的通配符，例如 `*.pdf`，不区分大小写 |
| `include_inline` | 默认 false；true 时额外包含有名称的内嵌部分，可能包括邮件签名图片 |
| `directory` | 用户指定的完整相对子目录组件数组；语义同归档脚本 |
| `name_template` | 仅用户要求统一改名时填写；不填则原名 |
| `workers` | 默认 2；允许 1–4 个独立只读连接。1 为串行；附加连接失败则自动减少并发并报告警告 |

日期筛选是服务器邮箱内部日期语义。用户指定其他时区或精确到时分秒时，当前脚本不能直接表达，应说明限制并增加精确时间筛选实现后再执行；不能将 UTC、服务器日期和用户日期默认为同一种范围。

支持命名占位符 `{date}`、`{subject}`、`{sender}`、`{index}`、`{uid}`、`{part}`、`{original}`、`{stem}`、`{ext}`。例如 `{date}_{sender}_{index}{ext}`。不支持 Python 属性访问、表达式或格式说明。日期为 INTERNALDATE 的服务器时区日期；序号按本次选定邮件和 MIME 部分遍历顺序分配，续跑优先保留索引路径。如果任务范围变化导致序号变化，必须保留并使用原任务配置，不自动重命名已保存文件。

## 下载、校验与恢复

- 只读选择邮箱，读取使用 BODY.PEEK；不发送、删信、移信或修改标记。
- 附件由 MIME Content-Transfer-Encoding 解码取得，不经模型重建；普通 PDF、Word、图片、压缩包等按真实字节保存，原始 EML 直接保存服务器返回字节。
- 嵌套 `message/rfc822` 附件等无法直接取得解码字节的结构会记失败，不通过重序列化伪装成原文件。原始整封 EML 可用 `raw` 或 `both` 保留。
- 缺少附件名称时采用稳定 MIME 部分位置生成备用名，不猜扩展名。改名、冲突保护和 SHA-256 核验复用归档模块。
- `.mail-export/archive.sqlite` 增加成功扫描缓存，关联账号、文件夹、UIDVALIDITY、UID 和导出类型。全部所需文件仍通过校验时，跳过整封邮件正文下载；缺失或修改文件时重新取邮件，保留用户修改过的旧文件并另存。
- 连接中断最多进行三次读取尝试并重连；UIDVALIDITY 改变时停止当前选择，避免把不同邮件当成同一 UID。常规失败按邮件记录，下次运行同一任务继续。
- 邮件头缓存按账号、文件夹、UIDVALIDITY、UID 隔离；每次仍向服务器查询当前范围的 UID，只为未缓存的 UID 分批获取邮件头（每批最多 100 个），本地重新应用发件人/主题筛选和 INTERNALDATE 排序。新增邮件会补取，被删除或移出范围的邮件不会从旧缓存混入。UIDVALIDITY 改变会使用新的缓存空间。
- 首次下载使用少量独立连接并发获取完整邮件，各连接同一时间只执行一个命令；按固定邮件顺序解析和写索引，序号及同名后缀不受网络完成顺序影响。队列以 workers 为界分批处理，不一次排入所有邮件。大邮件仍占相应内存，内存不足时设 workers=1。
- 下载后的附件字节直接传给归档模块，一次写入同时计算 SHA-256，保留 fsync、发布中状态和不覆盖发布机制；不再先写额外中转附件文件再复制。完整缓存命中时每个文件只校验一次，普通成功项不重复更新数据库；中断的发布中状态仍会恢复。
- 为保持既有 MIME 解码行为，本版仍读取完整邮件，没有实现 BODYSTRUCTURE 按附件部分下载、分段流式下载或邮箱超大附件的外部网盘链接下载。原始 EML 字节和附件选择规则保持一致。

## 性能诊断

JSON 的 `performance` 包含 `connect_seconds`（CLI 登录）、`selection_seconds`（SEARCH 与邮件头缓存/补取）、`download_seconds`（并发批次等待及附加连接初始化）、`verify_seconds`（缓存文件核验）、`save_seconds`（MIME 解析及归档）、`total_seconds`，以及 `header_cache_hits`、`headers_fetched`、`body_fetches`、`body_bytes`、`workers_used`。总时间不包含进程启动、退出登出和 Domi 模型编排时间；download_seconds 不是各连接时间相加。重试的额外流量不计入 body_bytes。

比较提速时用相同查询范围、文件数和字节校验结果；首次下载和重复运行分别对比。重复运行的预期是仅 SEARCH 加本地校验，headers_fetched/body_fetches 为 0（无新邮件且文件完整时）。改变命名或目录仍不会自动重排已保存文件，应按原有规则单独处理。

输出只包含本地结果、文件身份、文件名及统计，不包含授权码或完整正文。`counts` 是文件数，`messages` 是邮件结果；`no_selected_files` 表示该邮件无目标附件，不等于附件保存成功。退出码 0 表示本次选择处理完成（仍需核对是否有匹配内容），1 表示部分失败，2 表示配置、认证、协议或全局错误。遇到错误先读取 JSON，不重复声称“连接器可以下载”。

QQ 邮箱服务器及授权码说明参考 [腾讯云邮件集成文档](https://main.qcloudimg.com/raw/document/product/pdf/1270_46586_cn.pdf)；协议与 MIME 行为参考 Python 官方 [imaplib](https://docs.python.org/3/library/imaplib.html) 和 [email.message](https://docs.python.org/3/library/email.message.html)。

网易配置依据：[官方服务器参数](https://help.mail.126.com/faqDetail.do?code=d7a5dc8471cd0c0e8b4b8f4f8e49998b374173cfe9171305fa1ce630d7f67ac22b85ac2e7c90cd63)、[开启客户端协议和授权码](https://help.mail.126.com/faqDetail.do?code=d7a5dc8471cd0c0e8b4b8f4f8e49998b374173cfe9171305fa1ce630d7f67ac2a5feb28b66796d3b)。客户端标识按 [RFC 2971](https://www.rfc-editor.org/rfc/rfc2971.html) 发送，仅在服务器公布 ID 能力时使用。





Mac 助手与钥匙串访问采用系统 osascript 原生对话框及 Security.framework 的 SecItem 接口，不依赖 tkinter 或第三方包。授权码不作为进程命令行参数、不写临时凭据文件，配置状态文件只含状态。钥匙串存储机制见 [Apple Keychain 文档](https://developer.apple.com/documentation/security/adding-a-password-to-the-keychain)，输入框见 [Apple 原生隐藏输入](https://developer.apple.com/library/archive/documentation/LanguagesUtilities/Conceptual/MacAutomationScriptingGuide/PromptforText.html)。目前在 Windows 上通过模拟测试，尚未在 Mac 真机验证 GUI、系统授权弹窗和登录；部署验收需要补此项。
