# MEV Shield

MEV 交易保护服务：交易打包排序、夹子检测与回滚保护。

## 范围

本仓库从零开始实现上述方向的可用工具，不依赖外部同类实现。

## 状态

已实现：JSON 批处理输入校验、夹子（三明治）检测、reject / quarantine 策略、
打包排序、回滚记录与区块期限保护。仅依赖 Python 标准库。

## 用法

```
python -m mev_shield [--input IN] [--output OUT]
```

缺省从标准输入读取 JSON、向标准输出写确定性 JSON（固定键顺序、无多余空白、
末尾换行）；业务结果退出 0，输入/校验失败退出 2 并向 stderr 输出错误码。

## 区块期限保护

- 批次可选字段 `block`：当前区块高度，非负 JSON 整数（布尔值视为类型错误）。
- 交易可选字段 `deadline`：最后可执行区块，同为非负 JSON 整数；
  出现 `deadline` 时批次必须带 `block`。
- `deadline < block` 的交易过期：不参与夹子识别、`order` 与 `kept`，
  在非拒绝结果中按输入位置记一条 `DEADLINE_EXPIRED` 回滚并进入 `dropped`。
  过期交易即使 `sim` 为 revert 或同 from 有更大 nonce，也只记该原因，
  且不参与同 from 最大 nonce 统计；其余交易沿用原有原因优先级。
- 夹子检测只使用未过期交易的原相对位置：buy / victim / sell 任一过期即不成命中，
  剩余交易仍按既有条件产生命中，`at` 为原输入位置。
- 仅 reject / 缺省策略遇未过期命中时整批拒绝；quarantine 遇未过期命中时
  隔离攻击腿、保留 victim 与其他可执行交易，回滚按输入位置合并、每笔一次。
- `block`/`deadline` 类型错误或有 `deadline` 无 `block` 返回
  `BAD_DEADLINE`（五个列表为空、退出 2）；原有字段错误
  （`BAD_SCHEMA`/`DUP_HASH`/`DUP_NONCE`/`BAD_SIDE`/`BAD_SIM`/`BAD_POLICY`）
  优先级不变。无新字段的输入输出字节与旧版一致，额外字段一律忽略。

## 约定

- 公开行为以 README 与源码为准。
- 后续需求在此基线上增量实现。
