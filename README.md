# MEV Shield

MEV 交易保护服务：交易打包排序、夹子检测与回滚保护。

## 范围

本仓库从零开始实现上述方向的可用工具，不依赖外部同类实现。

## 状态

已实现：JSON 批处理校验、双向夹子检测、reject / quarantine 策略、
fee / nonce 两种打包模式、回滚记录，以及区块期限保护。

## 夹子检测

- 正向夹子：攻击者先买入、victim 再买入、攻击者后卖出；反向夹子：
  攻击者先卖出、victim 再卖出、攻击者后买入。三笔交易 token 相同、
  `sim` 均为 `success`，victim 与攻击者发送者不同。
- 按原输入顺序列出所有 `i<j<k` 命中，重叠命中逐条保留，按攻击前置
  腿位置升序排列；过期交易先排除且不参与命中。
- 每条命中输出 `buy`、`victim`、`sell`、`token`、`at`：`buy` / `sell`
  固定表示攻击者的买入腿与卖出腿，`at` 按此字段顺序记录三者的输入
  位置，故反向夹子中 `buy` 的位置晚于 `sell`。

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

## 约定

- 公开行为以 README 与源码为准。
- 后续需求在此基线上增量实现。
