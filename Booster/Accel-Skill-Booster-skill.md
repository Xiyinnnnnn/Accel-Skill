# Skill: Booster — 模块化工程流水线宿主
> 版本: v1.2.0 | 2026-08-16 | 文件: `booster.py`(宿主, 单文件纯标准库) + `skill.md`(本文档)
> 定位: 主agent分发任务N, 每模块X变体并发开发(N*X), 评分淘汰后按大目标方向组合(Y), 合并测到死, 多角度审查交付最优合并主体
> 核心逻辑: **模块化单元开发 + 测到死验收(一项不过不交付) + 多方向组合 + 最优交付**
> v1.1.0 变更: --goal注入4阶段 / VERIFIED+DENY结构化PASS / 评价解析容错 / 新增shell工具(危险命令授权) / 无限轮次+会话私有wrote+run_pool异常隔离
> v1.2.0 变更: shell路径越权检查覆盖 ~/$HOME/${VAR}(展开后校验) / 系统白名单收紧(去/proc /dev /sys) / verified语义简化(有PASS且无FAIL/ERROR/NOT PASS) / 评价支持多淘汰 / 组合编号连续化 / total_calls含合并重试 / --max-turns兜底

## 一、主代理调用时机
- 大任务拆N个模块并发开发, 每模块多变体对比择优 → N*X并发
- 需要"测到死"质量门槛 → 验收强制(无PASS标记的变体不参与组合)
- 需要按大目标多方向组合(最高分/最稳定/最安全/最轻量) → 自动组装Y组合
- 需要最终唯一最优主体 → 多角度审查(安全/性能/完整性/测试覆盖)选最高分

## 二、用法（3秒上手）
```bash
# 真实调用（key由主agent传入, 仅进程内不落盘）
python3 booster.py   # 技能目录内执行(或 python3 /绝对路径/booster.py) --docs 资料目录 --work 工作目录 \
  --modules N --per-module X --goal "大目标" --key <key> --api-url <API端点> --model <模型名>
  # 真调用必填; 或环境变量 BOOSTER_MODEL / BOOSTER_API_URL
# mock自测（不耗key, 工具链真实执行）
python3 booster.py --docs 任意资料目录 --work 任意工作目录 --modules N --per-module X --goal "x" --mock
# 组合方向(大目标决定, 默认全4)
--combos best,stability,safety,lightweight   # 可只传1-2个, Y=方向数
# 其他
--workers N    # 并发0-256, 0=全并行(默认)
--max-turns N  # 单子代理最大LLM轮次, 0=无限(默认)
--seed N       # 随机种子
--json         # 结构化输出
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
- **--goal 注入(v1.1)**: `--goal` 非空时, 4个阶段(1a开发/1b评价/2合并/3审查)的user指令开头均注入 `大目标: xxx` 行, 子代理全程对齐大目标
- 组合方向: best=总分最高 / stability=最稳定 / safety=最安全 / lightweight=最轻量
- 淘汰语义: 评价md标"淘汰: 文件名"的变体**不参与任何组合**(真正剔除)
- 审查选优: SCORE最高者晋升 `final.md` 交付主agent


## 四、测到死验收（程序层强制, 不靠子agent自觉）
| 检查点 | 规则 |
|---|---|
| 1a变体 | md含 **PASS 验收标记**且无 FAIL/ERROR/NOT PASS(**verified()**); 无→UNVERIFIED 不参与组合 |
| 2合并 | merged.md 含PASS且无FAIL/ERROR/NOT PASS(verified()); 无→组合排除, 不入审查 |
| 3审查 | review.md 含 `SCORE: 数字`; 解析失败→警告无效, 不终止 |
| 模块全灭 | 某模块无任何通过验收变体 → 宿主报错终止, 不产生脏final |
| 评价全灭 | 全部模块评价解析失败 → 终止; 单个失败仅该模块保留不淘汰 |
| 审查全灭 | 全部审查无有效SCORE → 终止 |

## 五、子代理权限（v1.1: 工具增至4个）
| 操作 | 权限 | 宿主强制 |
|---|---|---|
| 读资料目录 | list_dir/read_file | realpath前缀校验 |
| 读工作区全部轮次 | list_dir/read_file | 评估/合并/审查需要读他人产出 |
| 写工作目录md | write_md(唯一写工具) | 仅.md、限work内、覆盖他人拒绝 |
| 执行shell(v1.1) | shell | 限work cwd / 120s超时 / 4000字符截断 / 管道shell拦截 / 危险词需主agent授权 |
| 删除/移动 | 无 | 宿主管理 |

- **shell工具细则(v1.1)**:
  - 危险词表 BLOCK_WORDS: rm / mkfs / dd / su / reboot / shutdown / poweroff / halt / fdisk / parted / mount / umount / chmod / chown / kill / pkill / killall / mv
  - 命中危险词 → 打印 `[Booster] 主agent授权请求` 到stderr + `input()` 阻塞等待主agent批准(y/N); 拒绝则不执行
  - `| sh|bash|zsh` 管道执行shell → 直接拦截; 空命令拦截
  - 输出 = exit code + stdout + stderr, 超4000字符截断
  - 路径越权检查覆盖绝对路径 + **~/$HOME/${VAR} 展开后校验**(v1.2)
  - 系统路径白名单 `/usr/ /bin/ /sbin/ /lib /etc/` 放行; **/proc/ /dev/ /sys/ 不再放行**(v1.2)
- 工具sch写死(4个): `list_dir` / `read_file` / `write_md` / `shell`
- 子代理system prompt重型化写死于 `booster.py` 内 `SYSTEM` 常量（ROLE/IDENTITY/BOUNDARY/VERIFY/FILE_AUTHORITY/THINK/MUST/EXAMPLE/RULES）

## 六、内核健壮性（v1.1）
- **无限轮次工具调用**: agent_session while True, 无 tool_calls 即收敛（同Trial）
- **会话私有wrote**: `ctx = dict(ctx, wrote=[])`, 消除并发竞态(原booster:209)
- **run_pool异常隔离**: submit + try/except, 单代理异常→None标记失败, 调用方 `if not r: continue` 兼容
- **PASS结构化**: VERIFIED_RE + DENY_RE, 防 "NOT PASS"/FAIL/ERROR 糊弄; 合并验收与1a验收同用 verified()(有PASS且无FAIL/ERROR/NOT PASS即过, 不再强制清单全勾选)
- **--max-turns兜底(v1.2)**: 默认0=无限(设计); 真调用可设上限防失控
- **组合编号连续(v1.2)**: 去重后 combo_%d 按实际产出连续编号, 日志/JSON/目录一致
- **total_calls含重试(v1.2)**: 阶段2合并重试调用计入总数
- **多淘汰(v1.2)**: 评价md可标注多个"淘汰: 文件"行, 全部生效

## 七、输出与验收
- 非json: 各阶段统计 + 最终路径 + 各模块评分/淘汰/审查SCORE
- `--json`: `{final, best_combo, best_score, total_calls, modules, variants, combos, rounds:{...}, mock}`
- 主agent读回 `final.md` 即最优合并主体
- 退出码: 0成功 / 1失败


## 八、验证状态
- v1.0 基线: N=2 X=3 → 1a=6变体 → 4组合 → 合并4/4 → SCORE 8/9/10/8 → combo_2最优; total=16（补丁前）
- v1.1 回归 (2026-08-16, --mock): N=2 X=2 → 1a=4变体 → 1b淘汰 → 合并4/4过 → 审查SCORE=8/9/10/8 → final.md
- v1.2 回归 (2026-08-16, --mock): N2X3 → 组合有差异(combos=2) total=12; N2X2 → 淘汰后每模块单变体, 4组合去重为1属预期(X>=3才有多方向组合差异); verified新语义/多淘汰/`~`与`$HOME`越权拒绝/`/proc`拒绝 单测全过
- shell全测: 普通命令执行 / 管道拦截 / 危险命令授权拒绝 / 授权批准 全过
- 验收强制验证过: 无PASS变体被剔除 / 模块全灭终止 / NOT PASS不被糊弄
- 安全边界全过: 越权写/非md/覆盖他人/越权读全拒绝
- 真实调用前建议先 `--mock` 验证流程

## 九、边界与坑
1. 重复跑同一work触发覆盖保护 → 用新work或先清空(宿主不提供清理)
2. 无限轮次依赖API空tool_calls收敛, 默认无轮数上限(设计), 真调用可用 --max-turns 兜底
3. 某阶段全部失败 → 宿主报错终止
4. 限制: 1<=N<=256, 1<=X<=256, N*X<=256
5. key仅进程内用, 不落盘
6. 真调用必须提供 --api-url 与 --model (或环境变量 BOOSTER_API_URL/BOOSTER_MODEL); mock 不需要
7. **shell授权是阻塞式input()**: 真调用时主agent需在场应答; 无人应答会挂起 → 危险命令慎用

## 十、维护
- 改子代理prompt: 编辑 `booster.py` 内 `SYSTEM` 常量（写死, 非参数）
- 改组合方向: 编辑 `DIM_NAMES` 字典
- 改评分表格式: 编辑 `SCORE_RE`/`ELIM_RE`/`SCORE_LINE_RE` 正则(需与1b/3阶段指令同步)
- 改shell安全: 编辑 `BLOCK_WORDS` 表 / 超时 / 截断长度
- 换模型/API: `--model --api-url` 运行时传入

