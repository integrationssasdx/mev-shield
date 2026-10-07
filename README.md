# MEV Shield

MEV 交易保护服务：交易打包排序、夹子检测与回滚保护。

## 范围

本仓库从零开始实现上述方向的可用工具，不依赖外部同类实现。

## 状态

已实现：JSON 批处理校验、双向夹子检测、reject / quarantine 策略、
fee / nonce 两种打包模式、回滚记录，以及区块期限保护、统一决策入口、
最小隔离计划与预算约束隔离计划、安全重排计划、多区块排程计划、
双预算守卫计划、不可拆分交易捆绑联合排程。

## 夹子检测

对原输入顺序枚举所有 `i<j<k`：三笔交易 `token` 相同、`sim` 均为
`success`、`i` 与 `k` 同 `from`、`j` 的 `from` 与之不同，且：

- 正向夹子：`i` 与 `j` 为 `buy`、`k` 为 `sell`（攻击者先买、victim
  买、攻击者后卖）；
- 反向夹子：`i` 与 `j` 为 `sell`、`k` 为 `buy`（攻击者先卖、victim
  卖、攻击者后买）。

过期交易先排除且不参与命中。命中逐条保留（含重叠），按攻击前置腿
位置 `i` 升序排列。每条命中输出固定键 `buy`、`victim`、`sell`、
`token`、`at`：`buy` / `sell` 固定表示攻击者的买入腿与卖出腿，
`victim` 为中间交易，`at` 按字段顺序记录三者的输入位置。因此反向
夹子中 `buy` 的位置晚于 `sell`（`at[0] > at[2]`）。

- reject（含缺省策略）：任一命中返回 `rejected` /
  `SANDWICH_DETECTED`，`order`、`rollback`、`kept`、`dropped` 均空。
- quarantine：有命中时返回 `mitigated` / `SANDWICH_MITIGATED`，
  隔离所有攻击腿（每笔只记一次 `SANDWICH_DETECTED` 回滚），victim
  及其余交易继续按 deadline、sim、nonce 规则筛选；无命中时为
  `ok` 且 `code` 为空。

## 区块期限

- 批次可选 `block`（当前区块高度），交易可选 `deadline`（最后可执行
  区块），均须为非负 JSON 整数（不含布尔值）。
- 原输入有效但 `block` / `deadline` 类型错误，或有 `deadline` 无
  `block` 时，返回 `error` / `BAD_DEADLINE`，退出码 2。
- `deadline < block` 的交易过期：不参与夹子识别、`order` 与 `kept`，
  在非拒绝结果中按输入位置记一条 `DEADLINE_EXPIRED` 回滚并进入
  `dropped`；该原因优先于 `REVERT` 与 `DEPENDENT_NONCE`。

## 打包模式

- 批次可选 `packing`，只接受 `fee`（缺省）或 `nonce`；其他值返回
  `error` / `BAD_PACKING`，退出码 2，各列表字段为空。JSON、schema、
  重复 hash、重复 nonce、`side`、`sim`、`policy`、期限错误均优先于
  `BAD_PACKING`。
- `fee`（含缺省与显式指定）：沿用 fee 降序、hash 升序与既有
  `DEPENDENT_NONCE` 规则，行为不变。
- `nonce`：先排除过期交易；reject 命中夹子仍整批拒绝，quarantine
  命中仍隔离攻击腿、保留 victim 及其余交易。对每个 `from` 的剩余
  成功交易，`sim` 为 `revert` 的记 `REVERT` 并排除；以该道最大 nonce
  为终点向下保留最长连续 nonce 后缀，后缀内保留，后缀外记
  `DEPENDENT_NONCE`。
- `nonce` 排序：发送者道内按 nonce 升序；发送者道之间按道内最高
  fee 降序，最高 fee 相同按道内最小 hash 升序。
- 回滚原因优先级：`SANDWICH_DETECTED`（仅 quarantine 攻击腿，
  reject 不写 rollback）、`DEADLINE_EXPIRED`、`REVERT`、
  `DEPENDENT_NONCE`；每笔交易只记一次。`rollback` / `dropped`
  按输入位置排列，`order` 与 `kept` 同序。

## 统一决策入口（python -m mev_shield.decision）

在既有能力之上提供单交易包的统一结论；`python -m mev_shield`
入口与公开行为不变。参数与既有入口相同（`--input` / `--output`，
缺省标准输入 / 标准输出）。

输入（JSON 对象）：

- `id`：交易包标识，字符串；
- `transactions`：候选交易（数组顺序即候选顺序），每笔含
  `hash` / `from` / `nonce` / `fee` / `token` / `side` / `sim` /
  `price`（正数执行价）；
- `market`：市场上下文，对象，含 `prices`（token -> 正数参考价）；
- `basePrice`：基准价格，正数；
- `maxSlippage`：滑点上限，`[0, 1]` 含端点；
- `rollbackLimit`：回滚范围，`[0, 1]` 含端点；
- `policy`：可选策略，`reject`（缺省）/ `quarantine`；
- `slippageMode`：可选滑点口径，`base`（缺省）/ `market`。缺失按
  `base` 处理；类型或取值非法返回 `BAD_SLIPPAGE_MODE`（见校验优先级）。

处理流程：先校验交易包、顺序与市场上下文；沿用既有 fee 排序
语义（fee 降序、hash 升序）生成最终顺序，最终顺序恰好覆盖输入
交易，不增加、丢失或重复；在最终顺序上以相邻交易的价格变化、
买卖方向和发送者识别三段夹子（首尾同 `from` 为前置 / 后置腿，
中间不同 `from` 为受害交易，价格沿受害方向移动且在后置腿回落），
并输出位置、价格与变化率 `move` 作为可复核依据。

输出字段固定：`id`、`conclusion`（`ALLOW` / `BLOCK`）、
`finalOrder`、`sandwich`（`detected` / `front` / `victim` /
`back` / `evidence`）、`involved`（涉及交易，按最终顺序）、
`reasons`（原因码，固定优先级排列）、`rollbackAllowed`、
`basis`（决策依据：策略、限额与实测值）。相同输入逐字一致。

- 正常放行：`ALLOW` 且 `rollbackAllowed` 为 `true`，`reasons` 为空。
- 风险检查失败（滑点超限 `SLIPPAGE_EXCEEDED`）、价格上下文缺失
  （`PRICE_CONTEXT_MISSING`，token 无参考价）、排序不满足 nonce
  依赖（`NONCE_ORDER_VIOLATION`）、预计回滚（`sim` 为 `revert`
  的占比）超过范围（`ROLLBACK_LIMIT_EXCEEDED`）或确认夹子
  （`SANDWICH_DETECTED`）：结论 `BLOCK` 且 `rollbackAllowed` 为
  `false`。业务结论（含 BLOCK）退出码为 0。

滑点口径（`slippageMode`）：

- `base`（含缺省与显式指定）：沿用现有口径，每笔交易价格相对
  `basePrice` 取绝对偏离率 `abs(price-basePrice)/basePrice`，全部
  交易参与取值；token 缺市场参考价仍记 `PRICE_CONTEXT_MISSING`。
  原因码与排序、fee 降序与 hash 升序、nonce、夹子、回滚及失败关闭
  行为均不变。
- `market`：每笔交易用 `market.prices` 中同 token 的正数参考价取
  `abs(price-reference)/reference`；任一结果大于 `maxSlippage` 时
  产生 `SLIPPAGE_EXCEEDED`，等于上限仍放行；多个原因沿用现有优先
  级。token 缺少参考价时只产生 `PRICE_CONTEXT_MISSING`，该笔不参与
  滑点取值，也不追加 `SLIPPAGE_EXCEEDED`；
  `basis.maxSlippageObserved` 为所有可计算结果的最大值，无任何可
  计算结果时为 `0.0`。`basePrice` 在此模式仍校验并从 `basis` 返回。
- 回滚决策失败关闭：检测器、排序器或回滚评估不可用时分别记
  `DETECTION_UNAVAILABLE`、`ORDERING_UNAVAILABLE`、
  `ROLLBACK_EVALUATION_FAILED`，结论一律 `BLOCK` 且
  `rollbackAllowed` 为 `false`。

输入校验失败（退出码 2，stderr 输出错误码）只返回对应原因码，
结论为 `BLOCK`，不夹带允许结论。校验优先级固定：

1. 空交易包：`EMPTY_BUNDLE`；
2. 缺少可识别哈希（缺失、非字符串或为空）：`UNIDENTIFIED_TRANSACTION`；
3. 重复交易（hash 重复）：`DUPLICATE_TRANSACTION`；
4. 无法满足的相邻 nonce 依赖（同 `from` nonce 重复或不连续）：
   `ORDERING_CONFLICT`；
5. 缺少价格字段（缺 `market.prices` 或交易缺正数 `price`）：
   `MISSING_MARKET_CONTEXT`；
6. 非正基准价格：`INVALID_PRICE_BASE`；
7. 滑点上限不在 `[0, 1]`：`INVALID_RISK_LIMIT`；
8. 回滚范围不在 `[0, 1]`：`INVALID_ROLLBACK_LIMIT`；
9. 策略取值非法：`BAD_POLICY`；
10. `slippageMode` 类型或取值非法（缺失不算）：`BAD_SLIPPAGE_MODE`。
    该检查排在原有输入与 policy 校验之后，上述旧错误一律优先。
    `BAD_SLIPPAGE_MODE` 的 stdout 保持错误结果形状与固定键序：
    结论 `BLOCK`、`reasons` 只含 `BAD_SLIPPAGE_MODE`、
    `rollbackAllowed` 为 `false`、其余列表为空、`basis` 各字段为
    错误结果数值（`maxSlippageObserved` 为 `0`），stderr 只写该码。

## 最小隔离计划（python -m mev_shield.mitigation）

在统一决策入口的输入、校验与相邻三段夹子规则之上，从交易包的全部
子集中选出一个合法的最小隔离保留集合。参数与既有入口相同（仅
`--input` / `--output`，缺省标准输入 / 标准输出），不新增落盘要求；
输入 JSON、哈希、nonce、价格、策略与校验语义及错误码优先级完全沿用
统一决策入口，既有两个入口的输入、输出与退出码不变。

- 基线顺序：沿用 fee 降序、hash 升序，作为 `baselineOrder`。
- 合法性：保留集合按基线相对顺序排列后，顺序执行与统一决策入口
  相同的相邻三段夹子判定，须无任何夹子证据；且同一 `from` 的保留
  交易 nonce 在该顺序上严格递增。
- 枚举全部子集求全局最优（不逐笔贪心），择优目标依次为：保留 fee
  总和最高、保留笔数最多、被移除交易按输入位置形成的 hash 序列
  字典序最小。重叠或互相牵连的夹子同样得到全局最优隔离集合；无夹子
  时保留全部交易。

输出字段固定：`id`、`baselineOrder`、`selectedOrder`（最优保留集合
顺序）、`removed`（按输入位置列出每笔被移除交易的 `hash`、`at` 与
固定原因 `SANDWICH_REMOVED`）、`keptFee`、`removedFee`（相应 fee
总和）、`evidence`（`baselineOrder` 按统一夹子规则得到的全部证据，
按起始位置升序、同位按 victim hash 升序）。相同输入逐字一致。

输入校验失败退出 2、stderr 写原因码，stdout 仍输出上述固定字段，但
四个列表（`baselineOrder`、`selectedOrder`、`removed`、`evidence`）
为空、`keptFee` 与 `removedFee` 为 0；正常退出 0。

## 预算约束隔离计划（python -m mev_shield.bounded）

在统一决策入口的输入、校验与相邻三段夹子规则之上，从交易包的全部
子集中选出一个合法的隔离保留集合，且被移除笔数不超过批次级预算。
参数与既有入口相同（仅 `--input` / `--output`，缺省标准输入 /
标准输出），不新增落盘要求；既有入口的输入、输出与退出码不变。

- 输入在统一决策入口的根对象上新增必需字段 `isolationLimit`：最多
  移除笔数，非负 JSON 整数（排除布尔值）。缺失、类型错误、布尔值
  或小于零均返回 `BAD_ISOLATION_LIMIT`；该校验排在统一决策入口
  全部既有校验（含 `BAD_SLIPPAGE_MODE`）之后，旧错误一律优先。
- 基线顺序、合法性（无相邻三段夹子证据、同 `from` nonce 严格递增）
  与择优目标（保留 fee 总和最高、保留笔数最多、被移除交易按输入
  位置的 hash 序列字典序最小）沿用最小隔离计划，另要求被移除笔数
  不超过 `isolationLimit`；枚举预算内全部子集求全局最优。

输出字段固定：`id`、`baselineOrder`、`selectedOrder`、`removed`
（按输入位置列出 `hash`、`at` 与固定原因 `SANDWICH_REMOVED`）、
`keptFee`、`removedFee`、`evidence`（完整基线的全部夹子证据，按
起始位置升序、同位按 victim hash 升序）、`feasible`、
`isolationLimit`。相同输入逐字一致。

- `feasible` 为 true 时：`selectedOrder` 与 `removed` 不重不漏覆盖
  输入交易，`keptFee` 与 `removedFee` 之和等于输入 fee 总和。
- 预算内无解不是输入错误：退出 0，`feasible` 为 false，
  `selectedOrder` 与 `removed` 为空，`keptFee` 与 `removedFee`
  为 0，`baselineOrder` 与 `evidence` 仍取完整基线，stderr 不写码。
- 输入校验失败退出 2、stderr 写原因码，stdout 保持同形：
  `feasible` 为 false，四个列表为空，`keptFee`、`removedFee` 与
  `isolationLimit` 为 0。

## 安全重排计划（python -m mev_shield.reorder）

在统一决策入口的输入、校验、fee 降序 / hash 升序基线、相邻三段夹子
证据与 nonce 依赖之上，只调整交易顺序（不删除任何交易）给出无夹子
的安全重排计划。参数与既有入口相同（仅 `--input` / `--output`，
缺省标准输入 / 标准输出）；既有入口的输入、输出与退出码不变。

- 输入在统一决策入口的根对象上新增必需字段 `maxMoves`：可改变基线
  位置的交易数上限，非负 JSON 整数（排除布尔值）。缺失、类型错误、
  布尔值或小于零均返回 `BAD_MOVE_LIMIT`；该校验排在统一决策入口
  全部既有校验（含 `BAD_SLIPPAGE_MODE`）之后，旧错误一律优先。
- `safeOrder` 恰好排列输入交易：同一 `from` 的 nonce 严格递增，且
  按统一决策的相邻三段规则无任何夹子证据。滑点、价格上下文与回滚
  沿用既有校验，但不参与可行性判定。
- 择优目标依次为：位置变化数（最终下标与基线下标不同的交易数）
  最小、各交易最终下标与基线下标差的绝对值和最小、最终位置对应
  输入下标序列的字典序最小。枚举全部 nonce 合法交错求全局最优，
  相同输入逐字一致。

输出字段固定：`id`、`baselineOrder`（基线 hash 顺序）、`safeOrder`、
`moved`（按最终位置升序列出 `hash`、`from`、`to` 与固定原因
`REORDERED`）、`movedCount`、`displacement`、`evidence`（完整基线
的全部夹子证据，按起始位置升序、同位按 victim hash 升序）、
`blockers`（基线问题，仅 `SANDWICH_DETECTED`、
`NONCE_ORDER_VIOLATION`，顺序固定）、`result`、`feasible`、
`maxMoves`。

- 最少变化数不超过 `maxMoves`：`result` 为 `OK`，`feasible` 为
  true；超限时 `result` 为 `MOVE_LIMIT_EXCEEDED`、`feasible` 为
  false，仍输出最优方案；没有任何合法顺序时 `result` 为
  `SAFE_ORDER_NOT_FOUND`，`safeOrder` 与 `moved` 为空，
  `movedCount` 与 `displacement` 为 0。超限与无方案均为正常结论：
  退出 0，stderr 不写码。
- 输入校验失败退出 2、stderr 写唯一原因码，stdout 保持同形且
  `result` 为 `INPUT_ERROR`，列表为空、数值为 0。

## 双预算守卫计划（python -m mev_shield.guarded）

在统一决策入口的输入、校验与 fee 降序 / hash 升序基线之上，把预算
隔离与安全重排合并：既移除高风险交易（不超过 `isolationLimit` 笔），
又在受限的位置变化（不超过 `maxMoves` 笔）内重排保留集合。参数与
既有入口相同（仅 `--input` / `--output`，缺省标准输入 / 标准输出）；
既有入口的输入、输出与退出码不变。

- 输入在统一决策入口的根对象上新增必需字段 `isolationLimit`（最多
  移除笔数）与 `maxMoves`（相对基线可改变位置的交易数上限），均为
  非负 JSON 整数（排除布尔值）。校验排在统一决策入口全部既有校验
  （含 `BAD_SLIPPAGE_MODE`）之后，两者依次校验；非法依次唯一返回
  `BAD_ISOLATION_LIMIT`、`BAD_MOVE_LIMIT`，旧错误一律优先。
- 合法性：保留交易逐笔通过滑点与价格上下文检查（market 口径缺
  token 正数参考价的交易不可保留）；保留集合 revert 占比不超过
  `rollbackLimit`（空集合占比视为 0）；同一 `from` 的 nonce 在最终
  顺序上严格递增；最终顺序无任何相邻三段夹子证据。
- 择优目标依次为：保留 fee 总和最高、保留笔数最多、位置变化数
  （最终下标与基线下标不同的保留交易数）最少、位移绝对值和最小、
  被移除交易按输入位置的 hash 序列字典序最小、最终位置对应输入
  下标序列字典序最小。枚举全部预算内子集与 nonce 合法交错求全局
  最优，相同输入逐字一致。

输出字段固定：`id`、`baselineOrder`、`selectedOrder`、`removed`
（按输入位置列出 `hash`、`at` 与固定原因 `RISK_REMOVED`）、`moved`
（按最终位置列出 `hash`、`from`、`to` 与固定原因 `REORDERED`）、
`keptFee`、`removedFee`、`movedCount`、`displacement`、
`requiredMoves`、`evidence`（完整基线的全部夹子证据，按起始位置
升序、同位按 victim hash 升序）、`blockers`（基线统一决策的原因码，
按 riskplan 固定顺序去重）、`result`、`feasible`、`isolationLimit`、
`maxMoves`。

- `requiredMoves` 为只计 `isolationLimit` 与合法性（不计
  `maxMoves`）的最少位置变化数，无合法集合时为 0。
- 双预算均满足：`result` 为 `OK`，`feasible` 为 true；预算内无任何
  合法保留集合时为 `ISOLATION_LIMIT_EXCEEDED`；`requiredMoves`
  超过 `maxMoves` 时为 `MOVE_LIMIT_EXCEEDED`。后两者 `feasible`
  为 false，`selectedOrder` / `removed` / `moved` 为空，`keptFee` /
  `removedFee` / `movedCount` / `displacement` 为 0，超限时
  `requiredMoves` 仍保留实测最小值；均为正常结论：退出 0，stderr
  不写码。
- 输入校验失败退出 2、stderr 写唯一原因码，stdout 保持同形且
  `result` 为 `INPUT_ERROR`，列表为空、数值为 0。

## 多区块排程计划（python -m mev_shield.schedule）

在统一决策入口的输入、校验、fee 降序 / hash 升序基线、相邻三段夹子
证据与 nonce 依赖之上，把未过期交易排入一个多区块窗口：窗口为
`block` 到 `block + scheduleBlocks - 1`，每个区块至多容纳
`blockCapacity` 笔交易。参数与既有入口相同（仅 `--input` /
`--output`，缺省标准输入 / 标准输出）；既有入口的输入、输出与
退出码不变。

- 输入在统一决策交易包上新增 `block`（非负 JSON 整数）、
  `scheduleBlocks`（正 JSON 整数）、`blockCapacity`（正 JSON 整数）
  与交易级可选 `deadline`（非负 JSON 整数），均排除布尔值；非法
  依次返回 `BAD_BLOCK`、`BAD_SCHEDULE_WINDOW`、`BAD_BLOCK_CAPACITY`、
  `BAD_DEADLINE`，排在统一决策入口全部既有校验（含
  `BAD_SLIPPAGE_MODE`）之后，旧错误一律优先。
- `deadline < block` 的交易过期：不参与排程，也不参与夹子判定。
  未过期交易至多进一个不晚于自身 `deadline` 的窗口区块，无
  `deadline` 可进任意窗口区块。
- 各块内按 fee 降序、hash 升序，跨块按区块升序拼接得
  `scheduledOrder`；该顺序上同一 `from` 的 nonce 严格递增，且按
  统一决策的相邻三段规则无任何夹子证据。
- 择优目标依次为：排程笔数最多、排程 fee 总和最高、`totalDelay`
  （各交易所在区块减 `block` 之和）最小、`scheduledOrder` 对应输入
  下标序列字典序最小。枚举全部排程方案求全局最优，不做逐笔贪心；
  相同输入逐字一致。

输出字段固定：`id`、`block`、`scheduleBlocks`、`blockCapacity`、
`baselineOrder`（全部交易的 fee 降序、hash 升序基线顺序）、
`blocks`（窗口每个区块的 `block` / `order`）、`scheduledOrder`、
`unscheduled`（按输入位置列出 `hash`、`at` 与 `reason`，原因仅
`DEADLINE_EXPIRED` 与 `SCHEDULE_SKIPPED`）、`scheduledFee`、
`unscheduledFee`（相应 fee 总和）、`totalDelay`、`evidence`
（未过期基线子序列上的全部夹子证据，按起始位置升序、同位按
victim hash 升序）、`feasible`。

- `feasible` 仅在未过期交易全部排程时为 true；否则为 false，
  仍输出最优部分排程。无完整排程不是输入错误：退出 0，stderr
  不写码。
- 输入校验失败退出 2、stderr 写唯一原因码，stdout 保持同形：
  列表为空、数值为 0、`feasible` 为 false。

## 不可拆分交易捆绑联合排程（python -m mev_shield.bundleschedule）

在统一决策入口与多区块排程计划的输入、校验、fee 降序 / hash 升序
基线、相邻三段夹子证据与 nonce 依赖之上，把带非空 `bundle` 标识的
交易按同值标识组成不可拆分捆绑，整组排入多区块窗口。参数与既有入口
相同（仅 `--input` / `--output`，缺省标准输入 / 标准输出）；公开
入口不改，其余字段与错误码沿用多区块排程计划。

- 输入在多区块排程字段（`block` / `scheduleBlocks` / `blockCapacity`
  与交易级可选 `deadline`）之上，要求每笔交易携带非空字符串
  `bundle`；缺失或非非空字符串返回 `BAD_BUNDLE_ID`，排在多区块排程
  四项校验之后，统一决策入口全部既有校验（含 `BAD_SLIPPAGE_MODE`）
  一律优先。
- 同值交易组成捆绑：段内顺序取输入相对位置，捆绑初始次序取首笔
  交易的输入位置。每个捆绑只能整组进入一个区块并占连续段，段内
  顺序不变；全局顺序按区块和段顺序拼接，恰好覆盖已选交易。
- 任一成员 `deadline` 早于起始区块则整组过期；否则只能进入所有
  期限允许且容量足够（剩余空间能容纳整组）的区块。
- 最终全局顺序中同一 `from` 的 nonce 严格递增，并按统一决策的
  相邻三段规则无任何夹子证据。
- 择优目标依次为：排程交易数最多、排程 fee 总和最高、`totalDelay`
  （各交易所在区块与起始区块的差之和）最小、全局顺序对应输入下标
  序列字典序最小。枚举全部捆绑排程方案求全局最优，不做逐笔贪心；
  相同输入逐字一致。

输出字段固定：`id`、`baselineOrder`（全部交易的 fee 降序、hash
升序基线顺序）、`blocks`（窗口每个区块的 `block` / `order`）、
`unscheduled`（按捆绑首笔位置列出 `bundle`、`at`、`hashes` 与
`reason`，原因仅 `DEADLINE_EXPIRED`、`CAPACITY_EXCEEDED`、
`BUNDLE_SKIPPED`；整组过期优先，其次无任何区块容量足以容纳整组，
其余为 `BUNDLE_SKIPPED`）、`scheduledFee`、`unscheduledFee`、
`totalDelay`、`evidence`（未过期基线子序列上的全部夹子证据，按
起始位置升序、同位按 victim hash 升序）、`feasible`。

- `feasible` 仅在全部捆绑排程时为 true；否则返回最优部分排程并为
  false。部分排程不是输入错误：退出 0，stderr 为空。
- 输入校验失败退出 2、stderr 写唯一原因码，stdout 同形：列表为空、
  数值为 0、`feasible` 为 false。

## 不可拆分捆绑风险隔离计划（python -m mev_shield.bundleisolate）

在风险约束隔离计划（riskplan）的输入、校验、fee 降序 / hash 升序
基线、滑点 / 价格上下文 / nonce / 夹子与 `rollbackLimit` 口径之上，
结合 bundleschedule 的不可拆分捆绑语义：同值 `bundle` 的交易只能
整组保留或整组移除，并按交易笔数消耗 `isolationLimit` 预算。参数
与既有入口相同（仅 `--input` / `--output`，缺省标准输入 / 标准
输出）；公开入口不改，错误码仅新增 `BAD_BUNDLE_ID`。

- 输入沿用统一决策交易包加 `isolationLimit`（非负 JSON 整数，排除
  布尔值），另要求每笔交易携带非空字符串 `bundle`。校验顺序：先
  统一决策全部既有校验，再 `isolationLimit`（失败
  `BAD_ISOLATION_LIMIT`），最后逐笔 `bundle`（缺失或非非空字符串
  `BAD_BUNDLE_ID`）；旧错误一律优先，stderr 只写唯一码。
- 同值交易组成捆绑：成员保持输入相对位置，捆绑按首笔交易的输入
  位置定序。每个捆绑二选一：全部成员按基线相对顺序保留，或整组
  移除；保留集合沿 riskplan 逐笔滑点 / 价格上下文检查、同 `from`
  nonce 严格递增、无夹子证据、revert 占比不超 `rollbackLimit`
  （空集合占比视为 0）。
- 移除预算按交易笔数计：各被移除捆绑成员数之和不超过
  `isolationLimit`，而非按捆绑数。
- 择优目标依次为：保留 fee 总和最高、保留笔数最多、被移除捆绑按
  首笔输入位置形成的整数序列字典序最小。枚举全部原子方案求全局
  最优，不做逐笔贪心；相同输入逐字一致。

输出字段固定：`id`、`baselineOrder`（全部交易的 fee 降序、hash
升序基线顺序）、`selectedOrder`（最优保留集合按基线相对顺序）、
`removed`（按捆绑首笔位置列出 `bundle`、`at`、`hashes` 与固定原因
`BUNDLE_REMOVED`，`hashes` 保持输入相对位置）、`keptFee`、
`removedFee`（总 fee 减 keptFee）、`evidence`（完整基线的全部夹子
证据，按起始位置升序、同位按 victim hash 升序）、`blockers`（基线
统一决策原因码，按 riskplan 固定顺序去重）、`feasible`、
`isolationLimit`。

- `feasible` 为 true 时：`selectedOrder` 与 `removed` 中各捆绑
  `hashes` 不重不漏覆盖输入交易，`keptFee` 与 `removedFee` 之和
  等于输入 fee 总和，被移除交易笔数不超过 `isolationLimit`。
- 预算内无合法方案不是输入错误：退出 0，stderr 为空，`feasible`
  为 false，`selectedOrder`、`removed` 为空，`keptFee`、
  `removedFee` 为 0，`baselineOrder`、`evidence`、`blockers` 与
  `isolationLimit` 真实值仍保留。
- 输入校验失败退出 2、stderr 仅写唯一原因码，stdout 同形：列表
  为空、数值为 0、`feasible` 为 false。

## 约定

- 公开行为以 README 与源码为准。
- 后续需求在此基线上增量实现。
