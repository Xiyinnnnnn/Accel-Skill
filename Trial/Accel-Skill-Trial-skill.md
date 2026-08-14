# Skill: Trial — 多子代理并发头脑风暴收敛宿主
> 版本: v1.1.0 | 2026-08-16 | 文件: `trial.py`(宿主, 单文件纯标准库) + `skill.md`(本文档)
> 定位: 主agent执行大并发任务**前**, 派多子代理并发头脑风暴, 三阶段文件权威收敛到唯一方案md
> 核心逻辑: **多并发 + 文件权威**（子代理产出以实际md为准, 无md=无产出=失败）
> v1.1.0 变更: 无限轮次工具调用 / 会话私有wrote / run_pool异常隔离 / 阶段3合并失败回退晋级

## 一、主代理调用时机
- 任务规模大 / 方向多 / 结果不确定 → 先头脑风暴收敛出唯一方案, 再执行
- 需要多样性方案对比 → 同一目标重复写入 `--goals` = 多子代理并发同向推理
- 需要最终唯一设计方案 → 阶段3自动减半淘汰至1份

## 二、用法（3秒上手）
```bash
# 真实调用（key由主agent传入, 仅进程内不落盘）
python3 trial.py   # 技能目录内执行(或 python3 /绝对路径/trial.py) --docs 资料目录 --work 工作目录 \
  --goals "架构设计|API设计|架构设计" --key <key> --api-url <API端点> --model <模型名>
  # 真调用必填; 或环境变量 TRIAL_MODEL / TRIAL_API_URL
# mock自测（不耗key, 工具链真实执行）
python3 trial.py --docs 任意资料目录 --work 任意工作目录 --goals "目标A|目标B" --mock
# 目标来源三选一
--goals "A|B"          # |分隔, 重复=同目标多并发
--goals-file f.txt     # 每行一个
--goals-json '["A","B"]'     # JSON数组
# 其他
--workers N    # 并发0-256, 0=全并行(默认)
--seed N       # 阶段3配对种子(可复现)
--json         # 结构化输出
```

## 三、三阶段流程
| 阶段 | 子agent数 | 消息构成 | 动作 | 产出 |
|---|---|---|---|---|
| 1 独立推理 | N(目标数) | system+资料user+目标user | 自由读资料, 独立思考 | `round1/agent_i.md` |
| 2 整合 | N(以r1实际md数为准) | system+资料+工作+目标 | 通读全部r1, 整合为统一方案 | `round2/agent_i.md` |
| 3 淘汰循环 | ceil(上轮md/2) | system+资料+工作+目标 | 随机配对A/B, 整合改进, 劣者抛弃 | 新轮md, 数量逐轮减半 |

- 阶段3循环至该轮仅剩1份 → 复制为 `final.md` 交付
- 奇数时最后1份**轮空晋级**（0调用）
- **合并失败回退晋级(v1.1)**: 某对子代理无产出/异常/未落盘 → 自动复制双亲中较优者(字节数粗筛)到下一轮, 流水线不中断
- 被淘汰md留在原轮次（证据链）, 下一轮不引用; **剔除=不引用, 不自动删除**


## 四、文件权威与目录约定

```text
工作目录/
  round1/agent_0.md ...   阶段1产出(每目标1份)
  round2/agent_0.md ...   阶段2整合
  round3/...               阶段3各轮
  final.md                 最终方案(交付主agent)
```

- 子agent必须 `write_md` 落盘才算产出; 无md = 失败
- 产出md结构: 标题 / 结论先行 / 依据来源 / 待整合项

## 五、子代理权限（程序层强制）
| 操作 | 权限 | 宿主强制 |
|---|---|---|
| 读资料目录 | list_dir/read_file | realpath前缀校验 |
| 读工作目录 | list_dir/read_file | 同上 |
| 写工作目录md | write_md(唯一写工具) | 仅.md后缀、限work内 |
| 覆盖他人产出 | 无 | 已存在且非本次产出→拒绝 |
| 删除/移动 | 无 | 宿主管理 |

- 工具sch写死(3个): `list_dir` / `read_file` / `write_md`
- 子代理system prompt重型化写死于 `trial.py` 内 `SYSTEM` 常量（ROLE/IDENTITY/BOUNDARY/FILE_AUTHORITY/THINK/MUST/EXAMPLE/RULES）

## 六、内核健壮性（v1.1）
- **无限轮次工具调用**: agent_session 用 while True 循环, 子代理返回无 tool_calls 即收敛返回; 不再有40轮上限
- **会话私有wrote**: 每会话 `ctx = dict(ctx, wrote=[])`, 消除共享列表并发竞态(原trial:184)
- **run_pool异常隔离**: ThreadPoolExecutor + submit + try/except, 单代理异常→标记失败(None)不炸穿; 调用方 okN 过滤兼容 None
- **阶段3回退**: 合并失败自动回退晋级(双亲较优者), 某轮0产出仅理论可能, 仍保留0产出→终止兜底

## 七、输出与验收
- 非json: 各轮统计 + 最终md绝对路径
- `--json`: `{"final":路径, "rounds":{轮次:[文件]}, "total_calls":N, "mock":bool}`
- 主agent读回 `final.md` 即最终设计方案
- 退出码: 0成功 / 1失败（资料目录不存在 / 非mock无key / 某轮零产出）


## 八、验证状态
- v1.0 基线: round1=3→round2=3→round3=2→round4=1, total_calls=8（补丁前）
- v1.1 回归 (2026-08-16, --mock): 4目标 → round1=4 → round2=4 → round3=2 → round4=1 → final.md（4→2→1 收敛）
- 安全边界全过: 越权写/非md/覆盖他人/越权读全拒绝
- 真实调用前建议先 `--mock` 验证流程

## 九、边界与坑
1. 重复跑同一work: round1已有md触发覆盖保护 → 用新work或先清空(宿主不提供清理)
2. 无限轮次依赖API返回空tool_calls收敛; 若模型持续发工具调用, 会话不结束（宿主无轮数兜底, 真调用注意超时）
3. 某轮产出0 → 宿主报错退出, 不产生脏final（回退机制下罕见）
4. 目标数1-256, 越界报错
5. key仅进程内用, 不落盘
6. 真调用必须提供 --api-url 与 --model (或环境变量 TRIAL_API_URL/TRIAL_MODEL); mock 不需要

## 十、维护
- 改子代理prompt: 编辑 `trial.py` 内 `SYSTEM` 常量（写死, 非参数）
- 改工具集: 编辑 `TOOLS` + `TOOL_IMPL`（增删需同步）
- 改会话收敛/健壮性: 编辑 `agent_session` / `run_pool`
- 换模型/API: `--model --api-url` 运行时传入

