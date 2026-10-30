# Mail Export Skill

按条件批量保存邮件与原始附件到本地文件夹。QQ 邮箱可通过自带的只读 IMAP 脚本直接下载真实文件；实际支持文件下载的连接器也可以复用。

## Domi / QQ 邮箱修复

用户反馈的 Domi `mailbox_read_attachment` 只能提取附件文字，不能下载原始 PDF 等文件。第一版只实现了本地归档，没有补齐这条获取通道。

现在新增 `scripts/qq_imap_export.py`：直接从 QQ 邮箱读取原始邮件、解码真实附件，并交给归档脚本保存。无需网页登录，也不需要依赖该文字提取接口。

使用前在 **Domi 实际运行脚本的环境** 本地配置 `QQ_EMAIL_ACCOUNT` 和 `QQ_EMAIL_AUTH_CODE`（邮箱客户端授权码）。仅在连接器中配置过，不代表脚本能访问这些凭据。完整步骤和任务参数见 [QQ IMAP 下载说明](references/qq-imap.md)。

## 功能

- 按账号、文件夹、时间、发件人或主题等条件选择邮件，实际可用条件取决于连接器。
- 用户指定命名规则优先；未指定时保留原名，缺少原名时使用稳定的备用名称。
- 同名文件追加序号，避免覆盖；记录来源和最终保存路径。
- 基于稳定邮件及附件标识去重，并通过文件大小与 SHA-256 核验本地文件。
- 支持中断续跑、部分失败记录和重试。
- 优先使用批量工具，只读取筛选、保存或命名所需内容。

## 使用

下载仓库中的 [mail-export.zip](mail-export.zip)，解压后得到 `mail-export/` skill 目录。将该目录按宿主 agent 支持的方式添加为 skill；具体导入入口以宿主产品为准。

宿主需要 Python 3.10+、目标本地文件夹的写入能力，以及 QQ IMAP 网络访问与本地凭据（或真正支持文件下载的其他邮箱工具）。两个 Python 脚本都只使用标准库，无需额外安装 Python 包。

示例指令：

> 把最近 30 天收到的发票附件下载到我指定的本地文件夹，保留原始文件名，跳过已经完整下载的文件。

> 下载指定发件人本月的附件，按照“日期_供应商_序号”统一命名。

## 文件

- [SKILL.md](SKILL.md)：触发条件、处理流程、命名规则和结果汇报。
- [references/connector-tools.md](references/connector-tools.md)：将现有工具映射到所需邮箱能力的约定。
- [references/archive-rules.md](references/archive-rules.md)：本地归档格式、JSON 清单和脚本接口。
- [scripts/archive_store.py](scripts/archive_store.py)：本地文件保存、索引、校验、去重及续跑。
- [scripts/qq_imap_export.py](scripts/qq_imap_export.py)：QQ 邮箱只读 IMAP、完整 EML 获取与真实 MIME 附件下载。
- [references/qq-imap.md](references/qq-imap.md)：本地凭据配置、下载任务、命名模板、能力边界。
- [mail-export.zip](mail-export.zip)：包含六个 skill 文件的分发包。

## 验证与限制

已完成本地命名、字节保存、重名保护、去重、续跑、部分失败及打包后命令行流程验证。原归档模块 16 项测试和新增 QQ IMAP 模块 10 项离线测试全部通过，包括路径穿越及符号链接保护。

2026-10-30 已使用本机环境变量中的凭据，只读连接真实 QQ 邮箱，下载一封邮件的 3 个 PDF；3 个文件均有 PDF 文件头，再次运行全部验证后跳过。还验证了另一封邮件的完整 EML 获取。邮件数据仅保存在本地测试工作目录，没有纳入本仓库或分发包。

这验证了 QQ IMAP 下载路径；Domi 内执行仍需要其运行环境具备 Python、网络权限和相同的本地凭据配置，不能自动继承连接器内部的授权信息。

仅提供文字摘要的连接器仍不能下载原始附件，QQ 路径通过直接 IMAP 补齐；超大附件的外部网盘链接、无法解码的嵌套邮件附件不在当前支持范围。远端运行目录也不等于用户电脑上的本地目录。

命名、接口及恢复流程的完整说明见 skill 内的文档。
