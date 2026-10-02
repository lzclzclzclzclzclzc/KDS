# LangGraph 重构实施与验证

基线：`main` 的 `6013612`。实施分支：`langgraph`。设计核对与实施日期：2026-09-30。

## 计划核对

`langgraph_plan.md` 的核心业务合同与现有代码吻合，有限回合图可以直接调用同步模型客户端，无需引入 LangChain 模型适配器、Agent Server 或额外服务。实施前补充了同步检查点、未完成节点的 `invoke(None)` 恢复、结束投票中断后的冷却、用量事件标识、普通投票冻结历史时点与总结中断对账。计划引用的 `docs/multi-agent-plan.md` 未在本工作区提供，后续拓扑能力未纳入此次实现。

依赖固定为 LangGraph 1.2.12、SQLite checkpointer 3.1.1、checkpoint 4.2.0、langchain-core 1.6.6、langsmith 0.3.45。图及 SQLite 包声明支持 Python 3.10+，此次实际测试环境为 Windows/Python 3.13。LangSmith 是库依赖，运行不要求启用远程追踪。依赖检查未发现不兼容要求。

官方依据：[Graph API](https://docs.langchain.com/oss/python/langgraph/graph-api)、[Persistence](https://docs.langchain.com/oss/python/langgraph/persistence)、[Checkpointers](https://docs.langchain.com/oss/python/langgraph/checkpointers)。

## 已交付内容

| 范围 | 实现 |
| --- | --- |
| 业务规则 | 原上下文、权限与白板操作提取至 `app/domain/`，legacy 与图共用；原提示词逐字比对通过 |
| 调度与发言 | 策略接口保留两人轮流、强制目标、首发、softmax/热度；选人决定与游标落盘后才请求角色 |
| 工作流 | 有限 TurnGraph、Send 辅助批次、共用 VoteGraph、人工 SummaryGraph；公开发言顺序执行 |
| 服务与兼容 | ConversationService 选择与恢复引擎；新对话默认图，旧无标识数据为 legacy；显式暂停边界迁移接口 |
| 持久化 | 增量 schema、版本 CAS、epoch、command/operation/attempt、独立 usage ledger；消息与白板原子幂等提交 |
| 恢复 | 优先复用 operation/attempt 的确定结果；committed 回合仍结算后续投票；丢失或不兼容检查点不透明重跑未知工具 |
| 人工控制 | 单槽预约落盘与覆盖/消费，暂停不丢预约；投票/总结/继续互斥；删除使迟到回调失效 |
| DSH | 会话跨回合复用，暂停后关闭；用量事件包含 run/session/seq，JSON 修正单独编号；私有日志与公开历史分离 |

本发布版保留 legacy 主循环及同版本回退适配器，用于已有记录和灰度回退。实际历史数据的批量迁移、停服备份与最终删除 legacy 属于部署阶段，不在代码验证中操作真实讨论。回退拒绝未对账结果/不确定尝试；完整业务快照与未消费预约必须一起迁移。

结束投票复用同一个 VoteGraph，并使用独立的投票检查点线程；根推进操作通过父子 operation 与稳定 vote ID 保存后续待办。这是对原计划“继承父图检查点”的实施调整，便于普通/结束投票共用可靠入口。重启后两类投票都标记中断，已提交回合不重复发布，冷却仍由根图幂等结算。

## 验证结果

- 原始基线：150 passed，9 skipped。
- 重构全量离线回归：240 passed，10 skipped。跳过真实收费 API 与显式启用的 SDK 集成。
- DSH 验证组合：13 passed，其中 7 项真实 SDK 连接本地模拟 API，另 6 项为 GraphRunner 的模拟 Harness 回归；无真实模型请求。
- Node 倒计时、DSH 工具/预算/提示词/取消桥接、真实浏览器工具日志回归全部通过。
- 文件检查点测试覆盖多线程访问、关闭重开、custom 事件、成功兄弟分支复用、删除及迟到写入拒绝。
- 故障测试覆盖模型结果/attempt receipt/业务提交/checkpoint 窗口、白板重复提交、用量重复通知、旧 epoch、数据库写失败、版本不兼容、投票与总结重启对账。
- 兼容测试覆盖相同脚本模型结果的消息/白板/热度/用量对比、41 个有限推进单元、冻结投票历史、完成后投票、预约迁移与双向切换。
- 最后控制回归覆盖并发继续、运行中删除、人工改动废弃旧操作树、投票/总结记录写入失败与线程启动失败，不会留下无法解除的运行标志。

三角色、每次固定等待 80ms 的模拟评分批次：并发 1 为 0.246s，并发 3 为 0.083s，均执行 3 次调用。该数据验证分发并行有效，不代表真实服务商性能。

两个 direct 回合、独立临时业务库的写入观察：11 次对话 UPDATE，90 次业务锁获取，最大等待 0.003ms。该样本包括执行权、独立用量和各阶段提交；可靠性记录增加写次数，工具日志分表/批量写入仍是后续优化。

## 保留的边界

两份 SQLite 不提供跨库原子事务；操作回执负责去重。外部模型或工具执行后、确定结果保存前的崩溃仍存在不确定窗口，无法保证外部副作用恰好一次。并发评分/投票仍可能令讨论输出超过回合入口阈值，严格预算预留、SSE、助手持久化与多群聊等后续范围没有混入此次等价迁移。

所有恢复测试使用临时数据库和独立 DSH 目录。用户已有业务库未被测试批量迁移。
