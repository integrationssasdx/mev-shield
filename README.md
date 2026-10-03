# MEV Shield

MEV 交易保护服务：交易打包排序、夹子检测与回滚保护。

## 范围

本仓库从零开始实现上述方向的可用工具，不依赖外部同类实现。

## 状态

已实现：JSON 批处理校验、夹子检测、reject / quarantine 策略、
fee / nonce 两种打包模式、打包排序与回滚记录，以及区块期限保护。

## 区块期限

- 批次可选 `block`（当前区块高度），交易可选 `deadline`（最后可执行
  区块），均须为非负 JSON 整数（不含布尔值）。
- 原输入有效但 `block` / `deadline` 类型错误，或有 `deadline` 无
  `block` 时，返回 `error` / `BAD_DEADLINE`，退出码 2。
- `deadline < block` 的交易过期：不参与夹子识别、`order` 与 `kept`，
  在非拒绝结果中按输入位置记一条 `DEADLINE_EXPIRED` 回滚并进入
  `dropped`；该原因优先于 `REVERT` 与 `DEPENDENT_NONCE`。

## 打包模式（packing）

- 批次可选 `packing`，只接受 `"fee"` 或 `"nonce"`；缺省等价于显式
  `"fee"`：fee 降序、hash 升序，并沿用现有 `DEPENDENT_NONCE` 规则
  （同 from 中 nonce 小于该 from 最大 nonce 的交易被排除）。
- 其他任何值（含错误类型、`null`）返回 `error` / `BAD_PACKING`，
  `order`、`hits`、`rollback`、`kept`、`dropped` 均为空，CLI 退出码 2，
  stderr 写 `BAD_PACKING`。`packing` 在所有其他字段之后校验，因此
  JSON、schema、重复 hash、重复 nonce、side、sim、policy、期限错误
  一律优先于 `BAD_PACKING`；参数与文件读写错误仍为 `BAD_ARGS`、
  `INPUT_IO`、`OUTPUT_IO`（退出码 2）。
- `"nonce"` 模式：
  - 先排除过期交易；reject 命中夹子仍整批拒绝；quarantine 命中仍隔离
    攻击腿并保留 victim 及其余交易，`hits` 不变。
  - 对每个 `from` 的剩余成功交易（不含过期、隔离攻击腿与 revert），
    以最大 nonce 为终点向下保留最长连续 nonce 后缀：后缀内保留，
    后缀外记 `DEPENDENT_NONCE`；`sim` 为 `revert` 的交易记 `REVERT`
    并排除，revert 造成的 nonce 缺口同样打断后缀。
  - 每笔交易只进 `rollback` 一次，原因优先级为
    `SANDWICH_DETECTED`、`DEADLINE_EXPIRED`、`REVERT`、
    `DEPENDENT_NONCE`；`SANDWICH_DETECTED` 仅记 quarantine 攻击腿，
    reject 不写 rollback。
  - 排序：按 `from` 分发送者道，道内 nonce 升序；发送者道按道内最高
    fee 降序排列，最高 fee 相同则按道内最小 hash 升序。
  - `order` 与 `kept` 同序，`dropped` 与 `rollback` 按输入位置排列。
- 无夹子仍为 `status: "ok"`、空 `code`；quarantine 实际缓解仍为
  `status: "mitigated"`、`code: "SANDWICH_MITIGATED"`。输出字段集合、
  紧凑 JSON 与末尾换行保持不变。

## 约定

- 公开行为以 README 与源码为准。
- 后续需求在此基线上增量实现。
