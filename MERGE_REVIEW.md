# 北闸合并勘正

按 README 的方式打开独立源库和镜像库，使用原初始化及空闲连接规则。原入口及已签材料继续可用，已有库重开可沿用原初始化。来件沿用同一资料中心的信任边界，镜像办理时只使用本库和来件。

## 发行与办理

- `lake.release.issues.issue_merge(db, request_id, publication_id, old_ids, replacement_id)` 返回具有 `number` 的固定勘正件。`old_ids` 为按传入顺序绑定的两个不同事实号，均须在所指普通发布内，同站的第三个冻结事实 `replacement_id` 为复核行。固定件绑定该发布、两笔原行及确切复核行的身份、读数和出处。每发布至多一张勘正，每个事实至多参加一张，三个角色均计入此限制。请求同号同参领取原件，改绑报 `RequestConflict`，不合资格的选件报 `MemberSelectionError`。源库 `replay(db, document.number)` 可领原件与发行回执，原件位于 `documents`。
- `lake.release.formats.encode_document` 和 `decode_document` 支持勘正件与 JSON 往返。原件按原号及规范内容识别，原分件与撤回件保持兼容。
- `lake.mirror.acceptance.apply_merge(db, document, *, fail_before_commit=False)` 接受勘正件对象或其 JSON 文本、字节，返回有 `status`、`receipt` 和 `adjustment` 的结果。两笔旧行都已从指定发布正式实收且发布仍有效时可首次生效，状态为 `merged`。欠件返回 `pending`，发布已撤的首次来件返回 `late`，二者的 `receipt` 和 `adjustment` 均为 `None`，无成功历史。已办同件可重投复领原结果，即使承载发布后来撤回。已办号收到异件或来件与已知冻结事实矛盾时抛 `DocumentConflict`，非法格式报 `ValueError`。同一已知事实号的站点、整升值和出处须一致，此规则继续适用于此后经普通分件收到的复核行，即使该行已退出当前账。

## 站账与回领

合并生效期间，两笔原行仍可通过其他发布获正式实收，相关首执保留这些实收事实和出处，当时没有新增可计量行则 `receipt.details['added_fact_ids']` 为空。这些普通发布仍可由原 `accept` 入口撤回。两笔原行在合并有效期间暂停计量。复核行只计一次，可以另有普通发布支持。

同一镜像撤回承载发布时，合并提供的复核行依据随发布退出。复核行若另有有效依据则留账。两笔原行各凭撤回时仍有效的其他普通发布依据恢复。原发布中的其他行照常结算。每次合并及撤回以本站本次办理前后的实际站账核定结果。

合并结果和撤回结果的 `adjustment` 提供按事实号排序的 `added`、`removed`，每行含事实号、站点、整升值和原出处。`row_delta`、`litres_delta` 和 `by_station` 均记办理后减办理前，逐站项沿用原格式，保留有实际增减的站点。原撤回历史仍可读取，其 `added` 为空。镜像 `replay(db, number)` 可按勘正件号或撤回件号取当时的 `receipt`、`adjustment` 和实际增减行的 `origins`。原实收历史保持当时的内容。账、办理结果和首执同次生效，失败保留上一状态与重投资格。`fail_before_commit=True` 沿用镜像提交前中断的语义。

## 双库样例

`python3 -B -m examples.merge_day` 预览原实收。夹具 `prepare_source` 返回 A、B、C 的冻结事实及普通发布 P、QA、QB、R。A=7 L 和 B=11 L 分处 P 的两件，C=13 L 来自复核页第 8 行。QA 单独提供 A，QB 单独提供 B，R 单独提供 C。向两库送 P/A、P/B 和 P/A、QB/B，各得 18 L。

1. 源库发行以 P 为承载的勘正及各场景要用的撤回件后关闭。同件先在东库生效，退出 A、B，新增 C，净减 1 行、5 L。西库待件，无勘正首执，仍为 18 L。西库补收 P/B 后再投，方可切到 13 L。
2. 合并后东库收 QA/A，现账仍是 13 L，这张普通实收的新增清单为空。两库办理撤 P，东库退出 C、恢复原日报第 1 行 A，净 0 行、−6 L。西库退出 C、恢复第 2 行 B，净 0 行、−2 L。重开仍能按原号领取合并及撤回时的记录。
3. 同一路若在撤 P 前已撤 QA，东库撤 P 时只退出 C，结果为 0 L。若保留 QA 且合并期间还正式实收了独立的 R/C，撤 P 后东库应有 A 和 C，合计 20 L。本次仅新增 A，净增 1 行、7 L。

用 `.pointwise-runtime.json` 的 `verify` 核验原回归及合并办理，用 `demo` 展示双库裁决与断源复账。清单沿用版本 1 和 `cli`，`setup` 为准备命令的参数数组列表，`verify` 与 `demo` 各为一条命令的参数数组。核验失败以非零状态退出。
