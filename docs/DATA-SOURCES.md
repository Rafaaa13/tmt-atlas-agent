# TMT Atlas｜官方来源与数据使用说明

## 1. 来源策略

TMT Atlas 的公开演示优先使用公司官方投资者关系页面、监管披露门户和公司正式公告入口。当前来源注册表位于 `data/source_registry.json`，保存来源名称、URL、类型、访问状态和使用提醒。

项目公开仓库不镜像受限制的完整年报、公告或商业研究报告。使用真实资料时，系统应保存来源元数据、短引文和页码/段落定位，并把完整材料留在用户有权访问的本地环境中。

## 2. 当前来源入口

### 美国公司

NVIDIA、Microsoft、Amazon、Alphabet 和 Meta 使用公司官方 IR 页面及 SEC EDGAR 公司申报入口。SEC 申报应尽量使用 accession number 和具体 filing 页面作为证据锚点，遵守 SEC fair-access 和速率限制。

### 中国公司

Alibaba、Baidu、Tencent 和 SMIC 使用公司官方 IR、SEC 20-F/6-K、HKEXnews 或 SSE 公告入口。不同公司可能存在中英文披露、双重上市、财年错位、IFRS/中国会计准则和非 IFRS 指标差异，必须在事实中保留会计与期间元数据。

## 3. 建议的事实记录字段

正式导入一个事实时，至少记录：

```text
company_id
metric_key
value
raw_value
raw_unit
currency
period_start
period_end
period_type
accounting_basis
scope
source_id
source_url
filing_date
page_or_paragraph
extraction_method
review_status
reviewed_by
reviewed_at
notes
```

## 4. 许可与合规边界

官方页面和监管文件仍可能受到版权、访问条款和再分发限制。项目只保存来源入口和必要的短引文；用户需要自行判断是否有权下载、处理、导出和公开材料。不得绕过付费墙、登录限制、反爬挑战或速率限制。

商业数据库、内部实习材料、带个人信息的文件和未获授权的研报不得进入公开仓库。若需要测试复杂 PDF，可以在本地使用，不提交原文；公开测试应使用自造 fixture 或许可清晰的数据。

## 5. 数据事实与演示值

`data/demo_seed.json` 当前标记为 `illustrative_demo`，用于测试产品的搜索、指标卡、矩阵、事件和备忘录流程。它不表示已核验的实时财务数据，不构成投资建议，也不能作为面试中“真实市场结果”的证据。

以后替换为真实数据时，应先完成：

1. 来源页面和具体文件确认。
2. 期间、单位、币种和会计准则记录。
3. 原文页码/段落记录。
4. 事实与计算分离。
5. 另一人或另一轮复核。
6. 在评测报告中记录数据版本和核查日期。
