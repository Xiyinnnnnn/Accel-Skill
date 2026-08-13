# Skill: Booster — 模块化工程流水线宿主

> 版本: v1.0.0 | 2026-08-16 | 文件: `booster.py`(宿主, 单文件纯标准库) + `skill.md`(本文档)
> 定位: 主agent分发任务N, 每模块X变体并发开发(N*X), 评分淘汰后按大目标方向组合(Y), 合并测到死, 多角度审查交付最优合并主体
> 核心逻辑: **模块化单元开发 + 测到死验收(一项不过不交付) + 多方向组合 + 最优交付**

## 一、主代理调用时机

- 大任务拆N个模块并发开发, 每模块多变体对比择优 → N*X并发
- 需要"测到死"质量门槛 → 验收强制(无PASS标记的变体不参与组合)
- 需要按大目标多方向组合(最高分/最稳定/最安全/最轻量) → 自动组装Y组合
- 需要最终唯一最优主体 → 多角度审查(安全/性能/完整性/测试覆盖)选最高分

## 二、用法（3秒上手）

```bash
# 真实调用（key由主agent传入, 仅进程内不落盘）
python3 booster.py   # 技能目录内执行(或 python3 /绝对路径/booster.py) --docs 资料目录 --work 工作目录 \
  --modules N --per-module X --goal "大目标" --key <key> --api-url <API端点> --model <模型名>  # 真调用必填; 或环境变量 BOOSTER_MODEL

# mock自测（不耗key, 工具链真实执行）
python3 booster.py   # 技能目录内执行(或 python3 /绝对路径/booster.py) --docs 任意资料目录 --work 任意工作目录 \
  --modules N --per-module X --goal "x" --mock

# 组合方向(大目标决定, 默认全4)
--combos best,stability,safety,lightweight   # 可只传1-2个, Y=方向数

# 其他
--workers N    # 并发0-256, 0=全并行(默认)
--seed N       # 随机种子
--json         # 结构化输出
--api-url U    # API端点(真调用必填; 或环境变量 BOOSTER_API_URL)
```

## 三、五步流水线

| 步 | 子agent数 | 动作 | 产出 |
|---|---|---|---|
| 1a 模块开发 | N*X | 单元化开发, 测到死(单元测试清单全PASS) | `stage1/module_i/agent_j.md` |
| 1b 模块评价 | N | 多维度评分(总分/稳定/安全/轻量), 淘汰最低分 | `stage1_eval/module_i.md` |
| 1c 组合组装 | 0(宿主) | 淘汰者剔除, 按方向每模块选最优变体 | `stage2/combo_k.md` |
| 2 合并 | Y | 合并为整体+集成测试, 不过则排查排错 | `stage2_merge/combo_k/merged.md` |
| 3 审查 | Y | 多角度审查+SCORE | `stage3/review_k.md` → 最高分复制为 `final.md` |

- 总子agent调用 = N*X + N + Y + Y
- 组合方向: best=总分最高 / stability=最稳定 / safety=最安全 / lightweight=最轻量
- 淘汰语义: 评价md标"淘汰: 文件名"的变体**不参与任何组合**(真正剔除)
- 审查选优: SCORE最高者晋升 `final.md` 交付主agent

## 四、测到死验收（程序层强制, 不靠子agent自觉）

| 检查点 | 规则 |
|---|---|
| 1a变体 | md必须含单元测试清单且每项 `PASS`; 无PASS → UNVERIFIED 不参与组合 |
| 2合并 | merged.md 必须含 `PASS`; 无 → 该组合排除, 不入审查 |
| 3审查 | review.md 必须含 `SCORE: 数字`; 解析失败 → 无效 |
| 模块全灭 | 某模块无任何通过验收变体 → 宿主报错终止, 不产生脏final |

## 五、子代理权限（权限较高: 工作区全可读）

| 操作 | 权限 | 宿主强制 |
|---|---|---|
| 读资料目录 | list_dir/read_file | realpath前缀校验 |
| 读工作区全部轮次 | list_dir/read_file | 评估/合并/审查需要读他人产出 |
| 写工作目录md | write_md(唯一写工具) | 仅.md、限work内、覆盖他人拒绝 |
| 删除/移动 | 无 | 宿主管理 |

- 工具sch写死(3个): `list_dir` / `read_file` / `write_md`
- 子代理system prompt重型化写死于 `booster.py` 内 `SYSTEM` 常量（ROLE/IDENTITY/BOUNDARY/VERIFY/FILE_AUTHORITY/THINK/MUST/EXAMPLE/RULES）

## 六、输出与验收

- 非json: 各阶段统计 + 最终路径 + 各模块评分/淘汰/审查SCORE
- `--json`: `{final, best_combo, best_score, total_calls, modules, variants, combos, rounds:{...}, mock}`
- 主agent读回 `final.md` 即最优合并主体
- 退出码: 0成功 / 1失败

## 七、验证状态

- 2026-08-16 已通过 mock 全流程验证: N=2 X=3 → 1a=6变体 → 1b淘汰最低分 → 4组合有差异 → 合并4/4过 → 审查SCORE=8/9/10/8 → combo_2最优 → final.md; total=16
- 验收强制验证过: 无PASS变体被剔除 / 模块全灭终止
- 安全边界全过: 越权写/非md/覆盖他人/越权读全拒绝
- 真实调用前建议先 `--mock` 验证流程

## 八、边界与坑

1. 重复跑同一work触发覆盖保护 → 用新work或先清空(宿主不提供清理)
2. 子代理会话工具轮上限40, 超限=该代理无产出
3. 某阶段全部失败 → 宿主报错终止
4. 限制: 1<=N<=256, 1<=X<=256, N*X<=256
5. key仅进程内用, 不落盘
6. 真调用必须提供 --api-url 与 --model (或环境变量 BOOSTER_API_URL/BOOSTER_MODEL); mock 不需要

## 九、维护

- 改子代理prompt: 编辑 `booster.py` 内 `SYSTEM` 常量（写死, 非参数）
- 改组合方向: 编辑 `DIM_NAMES` 字典
- 改评分表格式: 编辑 `SCORE_RE`/`ELIM_RE`/`SCORE_LINE_RE` 正则(需与1b/3阶段指令同步)
- 换模型/API: `--model --api-url` 运行时传入
