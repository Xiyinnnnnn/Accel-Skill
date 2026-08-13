#!/usr/bin/env python3
import json, os, sys, argparse, copy, random, re, shutil, time
import urllib.request, urllib.error
from concurrent.futures import ThreadPoolExecutor

API_URL   = os.environ.get("BOOSTER_API_URL", "")
MODEL     = os.environ.get("BOOSTER_MODEL", "")
MAX_AGENT = 256
TOOL_ROUNDS = 40
RETRY_MAX  = 3
DIM_KEYS  = ["score", "stability", "safety", "lightness"]
DIM_NAMES = {"best": "score", "stability": "stability", "safety": "safety", "lightweight": "lightness"}

SYSTEM = """[ROLE] 工程子代理（Booster Worker）| [LANG] zh-CN
[IDENTITY] 主代理任务流水线中的模块化工程单元; 按阶段指令交付md, 以文件为准

[BOUNDARY]
- 资料目录: 只读
- 工作目录: 可读全部轮次产出; 只写md(write_md); 禁止覆盖他人产出
- 删除/移动由宿主管理

[VERIFY]
- 交付前必须完成验收: 产出md必须含单元测试清单且全部 PASS
- 一项不过 = 不进入交付 = 任务失败

[FILE_AUTHORITY]
- 一切结论写入md; 无md = 无产出 = 失败
- 产出md结构: 标题 / 结论先行 / 依据来源 / 测试验收

[THINK] 推理协议(<think>包裹, 不进md):
  P1 拆解 -> P2 读资料 -> P3 推理/实现 -> P4 write_md落盘 -> P5 自检(验收必过)

[MUST]
- 工具仅三种: list_dir / read_file / write_md
- 先读再写, 禁止编造; 阶段指令(user消息)定义当前职责与产出格式
- 会话结束前必须 write_md 完成交付; 未落盘=失败

<EXAMPLE>
阶段指令: {阶段职责}
<think>P1拆解:{要点} P2读:{文件} P3实现:{要点} P4落盘:write_md {路径} P5验收:{全部PASS}</think>
最终必须调用 write_md 写入 {产出路径}
</EXAMPLE>

[RULES]
- 参数写死; 文件权威; 验收不过不交付
- 删除/覆盖由宿主管理
- 宁缺毋滥, 不产出=失败"""

TOOLS = [
 {"type":"function","function":{"name":"list_dir",
   "description":"列出指定目录内文件(只读, 限资料目录与工作目录)",
   "parameters":{"type":"object","properties":{"path":{"type":"string","description":"目录绝对路径"}},"required":["path"]}}},
 {"type":"function","function":{"name":"read_file",
   "description":"读取文件全部内容(只读, 限资料目录与工作目录)",
   "parameters":{"type":"object","properties":{"path":{"type":"string"}},"required":["path"]}}},
 {"type":"function","function":{"name":"write_md",
   "description":"写入markdown文件(唯一写工具, 限工作目录, 禁止覆盖他人产出)",
   "parameters":{"type":"object","properties":{"path":{"type":"string"},"content":{"type":"string"}},"required":["path","content"]}}},
]

def log(*a):
    print("[Booster]", *a, file=sys.stderr, flush=True)

def ensure(d):
    os.makedirs(d, exist_ok=True)

def list_files(d):
    if not os.path.isdir(d):
        return []
    return [n + "/" if os.path.isdir(os.path.join(d, n)) else n
            for n in sorted(os.listdir(d))]

def inside(root, p):
    rp, rr = os.path.realpath(p), os.path.realpath(root)
    return rp == rr or rp.startswith(rr + os.sep)

def first_md(d):
    if not d or not os.path.isdir(d):
        return None
    for n in sorted(os.listdir(d)):
        if n.endswith(".md"):
            return os.path.join(d, n)
    return None

def md_files(d):
    if not os.path.isdir(d):
        return []
    return [os.path.join(d, n) for n in sorted(os.listdir(d)) if n.endswith(".md")]

def t_list_dir(args, ctx):
    p = args.get("path", "")
    if not (inside(ctx["docs"], p) or inside(ctx["work"], p)):
        return "错误: 目录越权(仅限资料/工作目录): " + p
    if not os.path.isdir(p):
        return "错误: 不是目录: " + p
    r = list_files(p)
    return "\n".join(r) if r else "(空目录)"

def t_read_file(args, ctx):
    p = args.get("path", "")
    if not (inside(ctx["docs"], p) or inside(ctx["work"], p)):
        return "错误: 文件越权(仅限资料/工作目录): " + p
    if not os.path.isfile(p):
        return "错误: 不是文件: " + p
    try:
        with open(p, encoding="utf-8", errors="replace") as f:
            return f.read()
    except Exception as e:
        return "错误: 读取失败: %s" % e

def t_write_md(args, ctx):
    p, c = args.get("path", ""), args.get("content", "")
    if not inside(ctx["work"], p):
        return "错误: 写入越权(仅限工作目录): " + p
    if not p.endswith(".md"):
        return "错误: 仅允许写入.md: " + p
    if os.path.exists(p) and p not in ctx["wrote"]:
        return "错误: 目标已存在(禁止覆盖他人产出): " + p
    d = os.path.dirname(p)
    if d:
        ensure(d)
    with open(p, "w", encoding="utf-8") as f:
        f.write(c)
    ctx["wrote"].append(p)
    return "已写入: %s (%d字符)" % (p, len(c))

TOOL_IMPL = {"list_dir": t_list_dir, "read_file": t_read_file, "write_md": t_write_md}

def mock_llm(messages, cfg):
    n = sum(1 for m in messages if m["role"] == "tool")
    docs = work = out = None
    stage = ""
    for m in messages:
        if m["role"] != "user":
            continue
        d = re.search(r"资料目录: (\S+)", m["content"])
        if d: docs = d.group(1)
        w = re.search(r"工作目录: (\S+)", m["content"])
        if w: work = w.group(1)
        o = re.search(r"产出文件: (\S+)", m["content"])
        if o: out = o.group(1)
        s = re.search(r"\[阶段\] (\S+)", m["content"])
        if s: stage = s.group(1)
    plan = [("list_dir", docs), ("read_file", first_md(docs)),
            ("list_dir", work), ("read_file", first_md(work)),
            ("write_md", out)]
    while n < len(plan):
        name, arg = plan[n]
        if arg is not None:
            break
        n += 1
    if n >= len(plan):
        return {"choices": [{"message": {"content": "完成", "tool_calls": []}}]}
    name, arg = plan[n]
    if name == "write_md":
        content = mock_content(stage, cfg)
        args = {"path": arg, "content": content}
    else:
        args = {"path": arg}
    return {"choices": [{"message": {"content": "<think>mock决策</think>",
        "tool_calls": [{"id": "m%d" % n, "type": "function",
        "function": {"name": name, "arguments": json.dumps(args, ensure_ascii=False)}}]}}]}

def mock_content(stage, cfg):
    if stage == "模块开发":
        return ("# %s 模块方案 (变体 %s)\n\n> 单元测试清单\n- [x] 测试1 PASS\n"
                "- [x] 测试2 PASS\n- [x] 测试3 PASS\n\n验收: 全部通过 PASS\n"
                "【%s %s 产出】" % (cfg.module, cfg.variant, cfg.stage_tag, cfg.agent))
    if stage == "模块评价":
        rows = []
        for j in range(cfg.variants):
            rows.append("| agent_%d.md | %d | %d | %d | %d |" % (
                j, 10 - j, 6 + j, 8, 7 + j))
        return ("# 模块 %s 变体评价\n\n## 评分表(总分|稳定|安全|轻量)\n%s\n\n"
                "淘汰: agent_%d.md (最低分)\n【%s %s 产出】" % (
                cfg.module, "\n".join(rows), cfg.variants - 1, cfg.stage_tag, cfg.agent))
    if stage == "组合合并":
        return ("# %s 合并主体\n\n> 合并自: %s\n\n合并要点: 模块组合为整体, 职责明确。\n\n"
                "- [x] 集成测试1 PASS\n- [x] 集成测试2 PASS\n\n验收: 全部通过 PASS\n"
                "【%s %s 产出】" % (cfg.combo, cfg.combo_src, cfg.stage_tag, cfg.agent))
    if stage == "组合审查":
        return ("# %s 审查报告\n\n- 安全: 边界严格\n- 性能: 达标\n- 完整性: 完整\n"
                "- 测试覆盖: 全覆盖\nSCORE: %d\n【%s %s 产出】" % (
                cfg.combo, 8 + (cfg.combo_idx % 3), cfg.stage_tag, cfg.agent))
    return "# 产出\n\n【%s %s】" % (cfg.stage_tag, cfg.agent)

def llm_call(messages, cfg):
    if cfg.mock:
        return mock_llm(messages, cfg)
    body = {"model": cfg.model, "messages": messages,
            "tools": TOOLS, "tool_choice": "auto", "stream": False}
    req = urllib.request.Request(cfg.api, data=json.dumps(body).encode("utf-8"),
        headers={"Content-Type": "application/json",
                 "Authorization": "Bearer " + cfg.key,
                 "User-Agent": "curl/8.5.0"})
    last = None
    for i in range(RETRY_MAX):
        try:
            with urllib.request.urlopen(req, timeout=600) as r:
                return json.loads(r.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            last = "HTTP %s %s" % (e.code, e.read().decode("utf-8", "ignore")[:200])
        except Exception as e:
            last = str(e)
        time.sleep(1 + i)
    raise RuntimeError("LLM失败: " + last)

def agent_session(messages, cfg, ctx):
    ctx["wrote"] = []
    for _ in range(TOOL_ROUNDS):
        resp = llm_call(messages, cfg)
        msg = (resp.get("choices") or [{}])[0].get("message") or {}
        tcs = msg.get("tool_calls") or []
        if not tcs:
            return msg.get("content") or "", True
        messages.append({"role": "assistant", "content": msg.get("content"), "tool_calls": tcs})
        for tc in tcs:
            fn = tc.get("function") or {}
            name = fn.get("name", "")
            try:
                args = json.loads(fn.get("arguments") or "{}")
            except Exception:
                args = {}
            impl = TOOL_IMPL.get(name)
            result = impl(args, ctx) if impl else "未知工具: %s" % name
            messages.append({"role": "tool", "tool_call_id": tc.get("id"), "content": str(result)})
    return None, False

def phase1_dev(i, j, cfg, ctx):
    out = os.path.join(ctx["s1"], "module_%d" % i, "agent_%d.md" % j)
    cfg.stage_tag = "阶段1a"
    msgs = [
        {"role": "system", "content": SYSTEM},
        {"role": "user", "content": "资料目录: %s\n文件清单:\n%s\n规则: 只读" % (
            ctx["docs"], "\n".join(list_files(ctx["docs"])) or "(空)")},
        {"role": "user", "content": "[阶段] 模块开发\n你负责: module_%d 的变体 agent_%d\n"
                                     "产出文件: %s\n要求: 单元化模块设计 + 单元测试清单全PASS, 测到死(一项不过不交付)" % (
            i, j, out)},
    ]
    cfg.module, cfg.variant, cfg.agent = "module_%d" % i, "agent_%d" % j, "agent%d_%d" % (i, j)
    content, ok = agent_session(msgs, cfg, ctx)
    return i, j, out, ok

def phase1_eval(i, variants, cfg, ctx):
    out = os.path.join(ctx["s1e"], "module_%d.md" % i)
    cfg.stage_tag = "阶段1b"
    vlist = "\n".join("stage1/module_%d/%s" % (i, os.path.basename(v)) for v in variants)
    msgs = [
        {"role": "system", "content": SYSTEM},
        {"role": "user", "content": "工作目录: %s\n规则: 可读全部轮次产出(只读)" % ctx["work"]},
        {"role": "user", "content": "[阶段] 模块评价\n你评价: module_%d 的 %d 个变体:\n%s\n"
                                     "产出文件: %s\n要求: 输出评分表, 每行格式 | 文件名 | 总分 | 稳定 | 安全 | 轻量 |, "
                                     "并标注淘汰最低分行: 淘汰: 文件名" % (i, len(variants), vlist, out)},
    ]
    cfg.module, cfg.agent = "module_%d" % i, "eval%d" % i
    content, ok = agent_session(msgs, cfg, ctx)
    return i, out, ok

def phase2_merge(k, combo, cfg, ctx):
    out = os.path.join(ctx["s2m"], "combo_%d" % k, "merged.md")
    cfg.stage_tag = "阶段2"
    srcs = "\n".join("  %s" % p for p in combo["files"])
    msgs = [
        {"role": "system", "content": SYSTEM},
        {"role": "user", "content": "资料目录: %s\n文件清单:\n%s\n规则: 只读" % (
            ctx["docs"], "\n".join(list_files(ctx["docs"])) or "(空)")},
        {"role": "user", "content": "工作目录: %s\n组合清单: %s\n选定变体:\n%s\n规则: 可读全部" % (
            ctx["work"], combo["path"], srcs)},
        {"role": "user", "content": "[阶段] 组合合并\n你负责: %s\n产出文件: %s\n"
                                     "要求: 合并为整体方案 + 集成测试全PASS, 测到死, 不行则排查排错" % (
            combo["name"], out)},
    ]
    cfg.combo, cfg.combo_src, cfg.agent = combo["name"], combo["name"], "merge%d" % k
    content, ok = agent_session(msgs, cfg, ctx)
    return k, out, ok

def phase3_review(k, merged, cfg, ctx):
    out = os.path.join(ctx["s3"], "review_%d.md" % k)
    cfg.stage_tag = "阶段3"
    msgs = [
        {"role": "system", "content": SYSTEM},
        {"role": "user", "content": "工作目录: %s\n合并主体: %s\n规则: 可读全部(只读)" % (ctx["work"], merged)},
        {"role": "user", "content": "[阶段] 组合审查\n你审查: combo_%d 合并主体\n产出文件: %s\n"
                                     "要求: 多角度审查(安全/性能/完整性/测试覆盖), 结尾输出 SCORE: 数字" % (k, out)},
    ]
    cfg.combo, cfg.combo_idx, cfg.agent = "combo_%d" % k, k, "review%d" % k
    content, ok = agent_session(msgs, cfg, ctx)
    return k, out, ok

SCORE_RE = re.compile(r"\|\s*([\w.\-]+\.md)\s*\|\s*(\d+)\s*\|\s*(\d+)\s*\|\s*(\d+)\s*\|\s*(\d+)\s*\|")
SCORE_LINE_RE = re.compile(r"SCORE[:：]?\s*(\d+)")
ELIM_RE = re.compile(r"淘汰[:：]?\s*([\w.\-]+\.md)")

def parse_eval(path):
    with open(path, encoding="utf-8", errors="replace") as f:
        txt = f.read()
    scores = {}
    for m in SCORE_RE.finditer(txt):
        scores[m.group(1)] = {"score": int(m.group(2)), "stability": int(m.group(3)),
                              "safety": int(m.group(4)), "lightness": int(m.group(5))}
    elim = None
    m = ELIM_RE.search(txt)
    if m:
        elim = m.group(1)
    return scores, elim

def parse_review(path):
    with open(path, encoding="utf-8", errors="replace") as f:
        txt = f.read()
    m = SCORE_LINE_RE.search(txt)
    return int(m.group(1)) if m else None

def pick_variant(scores, dim):
    if not scores:
        return None
    return max(sorted(scores), key=lambda v: scores[v].get(dim, -1))

def build_combos(module_scores, combos, ctx, eliminated):
    out = []
    for k, cname in enumerate(combos):
        dim = DIM_NAMES.get(cname, "score")
        lines, files = [], []
        for mi in sorted(module_scores):
            v = pick_variant({f: s for f, s in module_scores[mi].items() if f not in eliminated.get(mi, set())}, dim)
            if not v:
                continue
            p = os.path.join(ctx["s1"], "module_%d" % mi, v)
            lines.append("- module_%d: %s" % (mi, p))
            files.append(p)
        combo_path = os.path.join(ctx["s2"], "combo_%d.md" % k)
        with open(combo_path, "w", encoding="utf-8") as f:
            f.write("# combo_%d: %s 方向组合\n\n%s\n" % (k, cname, "\n".join(lines)))
        out.append({"name": "combo_%d" % k, "strategy": cname, "path": combo_path, "files": files})
    return out

def calc_workers(n, cfg):
    w = cfg.workers
    if w is None or w <= 0:
        w = n
    return max(1, min(w, MAX_AGENT))

def run_pool(tasks, cfg):
    with ThreadPoolExecutor(max_workers=calc_workers(len(tasks), cfg)) as ex:
        return list(ex.map(lambda t: t[0](*t[1]), tasks))

def main():
    ap = argparse.ArgumentParser(description="Booster 模块化工程流水线: 并发开发->评分组合->合并测到死->审查交付", add_help=False)
    ap.add_argument("--docs", required=True, help="资料目录(只读)")
    ap.add_argument("--work", required=True, help="工作目录(md写入)")
    ap.add_argument("--modules", type=int, required=True, help="任务数N(模块数)")
    ap.add_argument("--per-module", type=int, default=1, help="每模块并发变体数X(默认1)")
    ap.add_argument("--goal", default="", help="主agent定下的大目标(写入组合评估指令)")
    ap.add_argument("--combos", default="best,stability,safety,lightweight",
                    help="组合方向: best,stability,safety,lightweight 逗号分隔(默认全4)")
    ap.add_argument("--workers", type=int, default=0, help="并发数0-256, 0=全并行(默认)")
    ap.add_argument("--key", help="用户授权API key(仅进程内, 不落盘)")
    ap.add_argument("--model", default=MODEL, help="模型名(真调用必填; 或环境变量 BOOSTER_MODEL)")
    ap.add_argument("--api-url", default=API_URL, help="API地址(真调用必填; 或环境变量 BOOSTER_API_URL)")
    ap.add_argument("--mock", action="store_true", help="模拟LLM, 不联网不耗key")
    ap.add_argument("--seed", type=int, default=None, help="随机种子")
    ap.add_argument("--json", action="store_true", help="输出结构化JSON")
    ap.add_argument("-h", "--help", action="help")
    args = ap.parse_args()

    n, x = args.modules, args.per_module
    if not (1 <= n <= MAX_AGENT and 1 <= x <= MAX_AGENT and n * x <= MAX_AGENT):
        sys.exit("错误: 需 1<=N<=%d, 1<=X<=%d, N*X<=%d; 当前 N=%d X=%d" % (MAX_AGENT, MAX_AGENT, MAX_AGENT, n, x))
    combos = [c.strip() for c in args.combos.split(",") if c.strip()]
    for c in combos:
        if c not in DIM_NAMES:
            sys.exit("错误: 未知组合方向 %s (可选: %s)" % (c, ",".join(DIM_NAMES)))
    if not args.mock:
        if not args.key:
            sys.exit("错误: 非mock模式必须 --key 传入用户授权API key")
        if not args.api_url:
            sys.exit("错误: 非mock模式必须 --api-url 或环境变量 BOOSTER_API_URL")
        if not args.model:
            sys.exit("错误: 非mock模式必须 --model 或环境变量 BOOSTER_MODEL")

    cfg = argparse.Namespace(mock=args.mock, key=args.key, model=args.model, api=args.api_url,
                             workers=args.workers, variants=x, goal=args.goal,
                             module="", variant="", combo="", combo_src="", combo_idx=0,
                             stage_tag="", agent="")
    docs, work = os.path.realpath(args.docs), os.path.realpath(args.work)
    if not os.path.isdir(docs):
        sys.exit("错误: 资料目录不存在: " + docs)
    ensure(work)
    s1, s1e, s2, s2m, s3 = (os.path.join(work, d) for d in
        ("stage1", "stage1_eval", "stage2", "stage2_merge", "stage3"))
    for d in (s1, s1e, s2, s2m, s3):
        ensure(d)
    ctx = {"docs": docs, "work": work, "s1": s1, "s1e": s1e, "s2": s2,
           "s2m": s2m, "s3": s3, "wrote": []}
    if args.mock:
        log("MOCK模式: LLM决策模拟, 工具链真实执行, 不耗key")

    log("阶段1a: %d模块 x %d变体 = %d 子代理并发" % (n, x, n * x))
    tasks = [(phase1_dev, (i, j, copy.copy(cfg), ctx)) for i in range(n) for j in range(x)]
    res1 = run_pool(tasks, cfg)
    ok1 = [r for r in res1 if r[3]]
    log("阶段1a产出: %d/%d 变体" % (len(ok1), n * x))

    variants_by_mod = {}
    unverified = []
    for i in range(n):
        vd = os.path.join(s1, "module_%d" % i)
        vs = []
        for f in md_files(vd):
            with open(f, encoding="utf-8", errors="replace") as fh:
                if "PASS" not in fh.read():
                    unverified.append(os.path.basename(f))
                    continue
            vs.append(f)
        variants_by_mod[i] = vs
    if unverified:
        log("验收强制: %d 个变体无PASS标记 -> UNVERIFIED 不参与组合: %s" % (len(unverified), unverified))
    if not all(variants_by_mod.values()):
        sys.exit("错误: 存在模块无通过验收的变体, 终止")

    log("阶段1b: %d 个评价子agent" % n)
    res2 = run_pool([(phase1_eval, (i, variants_by_mod[i], copy.copy(cfg), ctx)) for i in range(n)], cfg)
    ok2 = [r for r in res2 if r[2]]
    log("阶段1b产出: %d/%d 评价md" % (len(ok2), n))

    module_scores, eliminated = {}, {}
    for i, out, okk in res2:
        if not okk:
            continue
        scores, elim = parse_eval(out)
        if not scores:
            sys.exit("错误: 评价md评分表解析失败: %s" % out)
        module_scores[i] = scores
        eliminated[i] = {elim} if elim else set()
        log("module_%d 评分: %s | 淘汰: %s" % (i, scores, elim))
    if len(module_scores) < n:
        sys.exit("错误: 评价产出不足, 终止")
    combos_out = build_combos(module_scores, combos, ctx, eliminated)
    y = len(combos_out)
    log("阶段1c: 组装 %d 个组合方向: %s" % (y, [c["strategy"] for c in combos_out]))

    log("阶段2: %d 个合并子agent" % y)
    res3 = run_pool([(phase2_merge, (k, c, copy.copy(cfg), ctx)) for k, c in enumerate(combos_out)], cfg)
    ok3 = [r for r in res3 if r[2]]
    merged_ok = []
    for k, out, okk in res3:
        if not okk or not os.path.isfile(out):
            log("组合 %s 合并失败(无产出), 排除" % combos_out[k]["name"])
            continue
        with open(out, encoding="utf-8", errors="replace") as f:
            if "PASS" not in f.read():
                log("组合 %s 验收不过(无PASS), 排除" % combos_out[k]["name"])
                continue
        merged_ok.append((k, out))
    log("阶段2产出: %d/%d 组合通过验收" % (len(merged_ok), y))
    if not merged_ok:
        sys.exit("错误: 无组合通过合并验收, 终止")

    log("阶段3: %d 个审查子agent" % len(merged_ok))
    res4 = run_pool([(phase3_review, (k, out, copy.copy(cfg), ctx)) for k, out in merged_ok], cfg)
    reviews = []
    for k, out, okk in res4:
        if not okk:
            continue
        sc = parse_review(out)
        if sc is None:
            log("审查 %s 无SCORE, 无效" % out)
            continue
        reviews.append((k, sc, out))
        log("combo_%d 审查 SCORE=%d" % (k, sc))
    if not reviews:
        sys.exit("错误: 无有效审查结果, 终止")
    best_k, best_sc, best_out = max(reviews, key=lambda r: r[1])

    final = os.path.join(work, "final.md")
    shutil.copy2(best_out, final)
    total = n * x + n + y + len(merged_ok)
    log("完成: 最优主体 = %s (combo_%d, SCORE=%d, 总子代理调用 %d)" % (final, best_k, best_sc, total))

    if args.json:
        print(json.dumps({"final": final, "best_combo": "combo_%d" % best_k,
                          "best_score": best_sc, "total_calls": total,
                          "modules": n, "variants": x, "combos": y,
                          "rounds": {"stage1a": n * x, "stage1b": n, "stage2": y,
                                     "stage3": len(merged_ok)},
                          "mock": args.mock}, ensure_ascii=False, indent=2))
    else:
        print(final)

if __name__ == "__main__":
    main()
