# MEV Shield

MEV 交易保护服务：交易打包排序、夹子检测与回滚保护。

## 范围

本仓库从零开始实现上述方向的可用工具，不依赖外部同类实现。

## 状态

已实现：JSON 批处理校验、双向夹子检测、reject / quarantine 策略、
fee / nonce 两种打包模式、回滚记录，以及区块期限保护、统一决策入口
与最小隔离计划。

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
- `policy`：可选策略，`reject`（缺省）/ `quarantine`。

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
8. 回滚范围不在 `[0, 1]`：`INVALID_ROLLBACK_LIMIT`。

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

## 约定

- 公开行为以 README 与源码为准。
- 后续需求在此基线上增量实现。
