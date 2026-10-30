# QQ 邮箱原始附件下载

这条路径绕过仅提取文字的附件工具，直接以 IMAP 协议取得真实邮件字节，并解码 MIME 中的二进制附件。脚本使用 Python 3.10+ 标准库，无额外 Python 包；TLS 校验证书和主机名，固定连接 `imap.qq.com:993`。

## 一次性本地配置

在 QQ 邮箱设置中启用 IMAP/SMTP，按页面流程生成第三方客户端授权码。授权码不是网页登录密码。由用户在 **Domi 实际执行脚本的环境** 中配置：

| 环境变量 | 内容 |
| --- | --- |
| `QQ_EMAIL_ACCOUNT` | 完整 QQ 或 Foxmail 邮箱地址 |
| `QQ_EMAIL_AUTH_CODE` | 邮箱生成的客户端授权码 |

Windows 桌面运行可在系统“编辑账户的环境变量”中添加这两个用户变量，然后完全退出并重新启动 Domi，让新进程继承配置。远端或容器运行则在相应运行环境的秘密配置中设置，不能只改本机变量。

不要求用户将授权码发到聊天，不把凭据写入任务文件或仓库，不尝试读取 Domi 的内部凭据存储。只检查是否配置：

```text
python "<skill目录>/scripts/qq_imap_export.py" --check-config
```

返回 `configured: true` 仅表示变量存在和账号格式正确，`network_checked: false`，不代表账号已通过实际登录验证。

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
python "<skill目录>/scripts/qq_imap_export.py" --config "<任务JSON绝对路径>" --root "<本地目标绝对目录>"
```

脚本从环境变量读取凭据。任务文件只能包含以下字段，拼错字段会报错，避免筛选失效却全量下载：

| 字段 | 含义 |
| --- | --- |
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
- 为保持既有 MIME 解码行为，本版仍读取完整邮件，没有实现 BODYSTRUCTURE 按附件部分下载、分段流式下载或 QQ 超大附件的外部网盘链接下载。原始 EML 字节和附件选择规则保持一致。

## 性能诊断

JSON 的 `performance` 包含 `connect_seconds`（CLI 登录）、`selection_seconds`（SEARCH 与邮件头缓存/补取）、`download_seconds`（并发批次等待及附加连接初始化）、`verify_seconds`（缓存文件核验）、`save_seconds`（MIME 解析及归档）、`total_seconds`，以及 `header_cache_hits`、`headers_fetched`、`body_fetches`、`body_bytes`、`workers_used`。总时间不包含进程启动、退出登出和 Domi 模型编排时间；download_seconds 不是各连接时间相加。重试的额外流量不计入 body_bytes。

比较提速时用相同查询范围、文件数和字节校验结果；首次下载和重复运行分别对比。重复运行的预期是仅 SEARCH 加本地校验，headers_fetched/body_fetches 为 0（无新邮件且文件完整时）。改变命名或目录仍不会自动重排已保存文件，应按原有规则单独处理。

输出只包含本地结果、文件身份、文件名及统计，不包含授权码或完整正文。`counts` 是文件数，`messages` 是邮件结果；`no_selected_files` 表示该邮件无目标附件，不等于附件保存成功。退出码 0 表示本次选择处理完成（仍需核对是否有匹配内容），1 表示部分失败，2 表示配置、认证、协议或全局错误。遇到错误先读取 JSON，不重复声称“连接器可以下载”。

QQ 邮箱服务器及授权码说明参考 [腾讯云邮件集成文档](https://main.qcloudimg.com/raw/document/product/pdf/1270_46586_cn.pdf)；协议与 MIME 行为参考 Python 官方 [imaplib](https://docs.python.org/3/library/imaplib.html) 和 [email.message](https://docs.python.org/3/library/email.message.html)。


