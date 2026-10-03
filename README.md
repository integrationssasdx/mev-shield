# MEV Shield

MEV 交易保护服务：交易打包排序、夹子检测与回滚保护。

## 范围

本仓库从零开始实现上述方向的可用工具，不依赖外部同类实现。

## 状态

已实现：JSON 批处理校验、夹子检测、reject / quarantine 策略、
打包排序与回滚记录，以及区块期限保护。

## 区块期限

- 批次可选 `block`（当前区块高度），交易可选 `deadline`（最后可执行
  区块），均须为非负 JSON 整数（不含布尔值）。
- 原输入有效但 `block` / `deadline` 类型错误，或有 `deadline` 无
  `block` 时，返回 `error` / `BAD_DEADLINE`，退出码 2。
- `deadline < block` 的交易过期：不参与夹子识别、`order` 与 `kept`，
  在非拒绝结果中按输入位置记一条 `DEADLINE_EXPIRED` 回滚并进入
  `dropped`；该原因优先于 `REVERT` 与 `DEPENDENT_NONCE`。

## 约定

- 公开行为以 README 与源码为准。
- 后续需求在此基线上增量实现。
