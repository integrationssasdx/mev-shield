# MEV Shield

MEV 交易保护服务：交易打包排序、夹子检测与回滚保护。

## 范围

本仓库从零开始实现上述方向的可用工具，不依赖外部同类实现。

## 状态

初始基线：只有本说明，尚无实现。

## 约定

- 公开行为以 README 与源码为准。
- 后续需求在此基线上增量实现。

## 保护策略（policy）

输入顶层新增可选字符串 `policy`，缺省等价于 `reject`：

- `reject`：检测到夹子三元组时整体拒绝（`rejected` / `SANDWICH_DETECTED`），退出 0。
- `quarantine`：保留全部 hits，隔离命中三元组的 buy/sell 攻击腿（victim 保留），
  其余交易照常排序打包；被隔离腿记 `SANDWICH_DETECTED`，其余未打包交易按
  `REVERT` -> `DEPENDENT_NONCE` 顺序取回滚原因。有命中时状态为
  `mitigated` / `SANDWICH_MITIGATED`，退出 0；无命中时与无夹子结果一致（`ok`）。
- 其他取值（含非字符串）：输出 `error` / `BAD_POLICY`，各集合为空，退出 2，
  stderr 写一行 `BAD_POLICY`。
