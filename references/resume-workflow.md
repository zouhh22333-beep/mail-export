# 简历任务：批量取一次、判一次、存一次

宿主负责内容识别，程序负责真实字节、命名、目录和索引。不要重新实现下载、整理和校验脚本。

创建 UTF-8 任务 JSON（日期、路径和命名按用户要求）：

```json
{
  "root": "D:\\目标目录",
  "query": {"mailbox": "Sent Messages", "month": "2026-08", "name_template": "{date}{ext}"},
  "folder_template": "{date}",
  "layout": "auto",
  "include_companions": true
}
```

默认 auto、include_companions=true：只有简历就放根目录；简历加其他随附文件才建目录，目录与独立简历同层。多份全是简历也不建目录。只有明确要求排除其他附件才设 false。folder_template 是文件夹名，query.name_template 是文件名；未要求改名时省略两者，分别采用邮件主题和文件原名。folder_template 支持 date/subject/sender/uid/index。query.directory 是共同相对父目录。显式 flat 全部平铺，message 每封都建目录。

## Prepare

```text
python "<skill>/scripts/resume_export.py" prepare --job "<任务JSON绝对路径>" --work "<任务私有缓存绝对目录>"
```

返回 snapshot 路径、snapshot_id、按内容哈希合并后的 candidates：短 id、原名列表、一个 source_path、出现次数。同内容只读这个路径一次，结论自动用于每封邮件的对应文件。完整身份和哈希留在磁盘，宿主不必复制。

download_complete=false 先处理失败。否则批量判断不同内容的候选；文件名和主题足以确定的结合实际依据判断，有歧义再读文件。必要时用 snapshot 内特定邮件的 context_eml 辅助，勿读整份 snapshot 填充模型上下文。不能把所有 PDF 都当简历。扫描件不清楚才 OCR。无法识别的保留未知，不静默排除。

## 批量判断后 Finish

只写短清单，编号以实际 candidates 为准：

```json
{"snapshot_id":"prepare返回值","resume_ids":["1","3"],"other_ids":["2"]}
```

无需逐文件 reason 或 sha256。编号缺失代表未知，auto 布局会返回待处理，不会错误完成。清单绑定快照，重复或不存在的编号报错；内容变化也会阻止归档。

```text
python "<skill>/scripts/resume_export.py" finish --snapshot "<prepare返回路径>" --decisions "<短清单JSON绝对路径>"
```

程序同时写最终路径和索引；重名追加序号，不覆盖原文件。同一清单重跑跳过完整文件。成功后根据返回结果直接汇报，不再读目录、查源码或另写平铺脚本。失败或 existing indexed path retained 才诊断；已有文件新命名需要单独同步重排，不能谎称已更新。

候选和清单是私人数据，不放仓库。保留缓存用于续跑。模型服务超时后，已有 snapshot 和决策可直接 finish，无需重新联网或重读文件。默认不自动修复旧版已移位的索引。
