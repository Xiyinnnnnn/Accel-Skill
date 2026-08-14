#!/usr/bin/env python3
import json, os, sys, argparse, copy, random, re, shutil, time
import urllib.request, urllib.error
from concurrent.futures import ThreadPoolExecutor

API_URL   = os.environ.get("TRIAL_API_URL", "")
MODEL     = os.environ.get("TRIAL_MODEL", "")
MAX_AGENT = 256
RETRY_MAX  = 3
MAX_READ  = 20000

SYSTEM = """[ROLE] 头脑风暴子代理（Trial Worker）| [LANG] zh-CN [IDENTITY] 主代理的独立推理单元; 仅对分配目标负责, 产出以md落盘为准
[BOUNDARY] - 资料目录: 只读(list_dir/read_file) - 工作目录: 只写md(write_md), 禁止覆盖他人产出 - 无删除/移动权限; 文件管理由宿主程序负责
[FILE_AUTHORITY] - 一切结论必须写入md文件; 无md = 无产出 = 任务失败 - 产出md结构: 标题 / 结论先行 / 依据来源 / 待整合项
[THINK] 推理协议(<think>包裹, 不进md):   P1 拆解目标 -> P2 读资料(不编造) -> P3 独立推理 -> P4 write_md落盘 -> P5 自检
[MUST] - 先读资料再推理, 禁止凭空编造 - 工具仅三种: list_dir / read_file / write_md - 写路径必须位于工作目录内; 读路径限资料目录与工作目录 - 会话结束前必须完成 write_md; 未落盘=失败
<EXAMPLE> 目标: {目标} <think>P1 拆解:{要点} P2 读资料:list_dir->read_file {文件} P3 推理:{要点} P4 落盘:write_md {路径}</think> 最终必须调用 write_md 写入 {产出路径} </EXAMPLE>
[RULES] - 参数写死; 文件权威; 禁止越权工具 - 删除/覆盖由宿主管理, 子代理无此权限 - 推理深度优先, 宁少勿空"""

TOOLS = [
  {"type":"function","function":{"name":"list_dir",    "description":"列出指定目录内文件(只读, 限资料目录与工作目录)",    "parameters":{"type":"object","properties":{"path":{"type":"string","description":"目录绝对路径"}},"required":["path"]}}},
  {"type":"function","function":{"name":"read_file",    "description":"读取文件内容(只读, 限资料目录与工作目录, 超长截断)",    "parameters":{"type":"object","properties":{"path":{"type":"string"}},"required":["path"]}}},
  {"type":"function","function":{"name":"write_md",    "description":"写入markdown文件(唯一写工具, 限工作目录, 禁止覆盖他人产出)",    "parameters":{"type":"object","properties":{"path":{"type":"string"},"content":{"type":"string"}},"required":["path","content"]}}},
]

def log(*a):
    print("[Trial]", *a, file=sys.stderr, flush=True)

def ensure(d):
    os.makedirs(d, exist_ok=True)

def list_files(d):
    if not os.path.isdir(d):
        return []
    out = []
    for n in sorted(os.listdir(d)):
        p = os.path.join(d, n)
        out.append(n + "/" if os.path.isdir(p) else n)
    return out

def inside(root, p):
    rp = os.path.realpath(p)
    rr = os.path.realpath(root)
    return rp == rr or rp.startswith(rr + os.sep)

def first_md(d):
    if not d or not os.path.isdir(d):
        return None
    for n in sorted(os.listdir(d)):
        if n.endswith(".md"):
            return os.path.join(d, n)
    return None

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
            s = f.read(MAX_READ)
            if f.read(1):
                s += "\n...(截断: 文件超长)"
            return s
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
    for m in messages:
        if m["role"] != "user":
            continue
        d = re.search(r"资料目录: (\S+)", m["content"])
        if d: docs = d.group(1)
        w = re.search(r"工作目录: (\S+)", m["content"])
        if w: work = w.group(1)
        o = re.search(r"产出文件: (\S+)", m["content"])
        if o: out = o.group(1)
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
        content = "# %s 方案\n\n> 依据: %s\n\n%s\n\n【%s 子代理 %s 产出】" % (
            cfg.agent, "mock资料", mock_src(messages), cfg.stage, cfg.agent)
        args = {"path": arg, "content": content}
    else:
        args = {"path": arg}
    return {"choices": [{"message": {"content": "<think>mock决策</think>",
        "tool_calls": [{"id": "m%d" % n, "type": "function",
        "function": {"name": name, "arguments": json.dumps(args, ensure_ascii=False)}}]}}]}

def mock_src(messages):
    for m in reversed(messages):
        if m["role"] == "tool" and str(m["content"]) and not str(m["content"]).startswith("错误"):
            return str(m["content"])[:200]
    return ""

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
    ctx = dict(ctx, wrote=[])
    msg = {}
    while True:
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

def phase1_agent(i, goal, cfg, ctx):
    out = os.path.join(ctx["r1"], "agent_%d.md" % i)
    msgs = [
        {"role": "system", "content": SYSTEM},
        {"role": "user", "content": "资料目录: %s\n文件清单:\n%s\n规则: 只读" % (
            ctx["docs"], "\n".join(list_files(ctx["docs"])) or "(空)")},
        {"role": "user", "content": "你的目标(第%d号):\n%s\n\n产出文件: %s\n必须用 write_md 写入" % (i + 1, goal, out)},
    ]
    cfg.agent, cfg.stage = "agent%d" % i, "阶段1"
    content, ok = agent_session(msgs, cfg, ctx)
    return i, out, ok

def phase2_agent(i, cfg, ctx):
    out = os.path.join(ctx["r2"], "agent_%d.md" % i)
    msgs = [
        {"role": "system", "content": SYSTEM},
        {"role": "user", "content": "资料目录: %s\n文件清单:\n%s\n规则: 只读" % (
            ctx["docs"], "\n".join(list_files(ctx["docs"])) or "(空)")},
        {"role": "user", "content": "工作目录: %s\n第一阶段产出md:\n%s\n规则: 可读全部第一阶段md用于整合" % (
            ctx["work"], "\n".join(list_files(ctx["r1"])) or "(空)")},
        {"role": "user", "content": "第二阶段目标: 通读工作目录全部第一阶段md, 整合修正为一份统一方案; "
                                     "可自由调整各子目标的实现方式与职责划分, 以整体方案最优为准。\n产出文件: %s" % out},
    ]
    cfg.agent, cfg.stage = "agent%d" % i, "阶段2"
    content, ok = agent_session(msgs, cfg, ctx)
    return i, out, ok

def phase3_agent(i, a, b, cfg, ctx):
    out = os.path.join(ctx["nxt"], "agent_%d.md" % i)
    msgs = [
        {"role": "system", "content": SYSTEM},
        {"role": "user", "content": "资料目录: %s\n文件清单:\n%s\n规则: 只读" % (
            ctx["docs"], "\n".join(list_files(ctx["docs"])) or "(空)")},
        {"role": "user", "content": "工作目录: %s\n当前方案md:\n%s" % (
            ctx["work"], "\n".join(list_files(ctx["cur"])) or "(空)")},
        {"role": "user", "content": "第三阶段目标: 比较 A=%s 与 B=%s 两份方案, "
                                     "整合/修正/改进为唯一方案; 明显劣势方可抛弃删减。\n产出文件: %s" % (a, b, out)},
    ]
    cfg.agent, cfg.stage = "agent%d" % i, "阶段3"
    content, ok = agent_session(msgs, cfg, ctx)
    return i, out, ok

def _sig(p):
    try:
        with open(p, encoding="utf-8", errors="replace") as f:
            t = f.read(4000)
    except Exception:
        return set()
    return set(re.findall(r"[\u4e00-\u9fffA-Za-z0-9]+", t))

def _sim(a, b):
    x, y = _sig(a), _sig(b)
    if not x or not y:
        return 0.0
    return len(x & y) / float(len(x | y))

def _pair(cur, cur_dir):
    pairs, pool = [], cur[:]
    while len(pool) > 1:
        a = pool.pop(0)
        j = max(range(len(pool)), key=lambda t: _sim(os.path.join(cur_dir, a), os.path.join(cur_dir, pool[t])))
        pairs.append((a, pool.pop(j)))
    if pool:
        pairs.append((pool[0],))
    return pairs

def _passable(p):
    try:
        s = open(p, encoding="utf-8", errors="replace").read().strip()
    except Exception:
        return False
    return len(s) > 20 and bool(re.search(r"结论|方案|PASS|通过", s))

def _final_src(cur, cur_dir):
    cand = [f for f in cur if _passable(os.path.join(cur_dir, f))]
    if not cand:
        log("警告: 无合格最终方案, 取最大文件兜底")
        cand = cur
    return os.path.join(cur_dir, max(cand, key=lambda f: os.path.getsize(os.path.join(cur_dir, f))))

def parse_goals(args):
    if args.goals:
        return [g.strip() for g in args.goals.split("|") if g.strip()]
    if args.goals_file:
        with open(args.goals_file, encoding="utf-8") as f:
            return [l.strip() for l in f if l.strip()]
    return [str(g).strip() for g in json.loads(args.goals_json) if str(g).strip()]

def calc_workers(n, cfg):
    w = cfg.workers
    if w is None or w <= 0:
        w = n
    return max(1, min(w, MAX_AGENT))

def run_pool(tasks, cfg):
    with ThreadPoolExecutor(max_workers=calc_workers(len(tasks), cfg)) as ex:
        futs = [ex.submit(t[0], *t[1]) for t in tasks]
        out = []
        for f in futs:
            try:
                out.append(f.result())
            except Exception as e:
                log("代理异常(标记失败): %s" % e)
                out.append(None)
        return out

def main():
    ap = argparse.ArgumentParser(description="Trial 头脑风暴宿主: 多子代理并发头脑风暴, 文件权威收敛", add_help=False)
    ap.add_argument("--docs", required=True, help="资料目录(只读)")
    ap.add_argument("--work", required=True, help="工作目录(md写入)")
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--goals", help="目标列表, | 分隔; 重复=同目标多并发")
    g.add_argument("--goals-file", help="目标文件, 每行一个")
    g.add_argument("--goals-json", help='目标JSON数组, 如 ["a","b","a"]')
    ap.add_argument("--workers", type=int, default=0, help="并发数0-256, 0=全并行(默认)")
    ap.add_argument("--key", default=os.environ.get("TRIAL_KEY", ""), help="用户授权API key(仅进程内, 不落盘; 或环境变量 TRIAL_KEY)")
    ap.add_argument("--model", default=MODEL, help="模型名(真调用必填; 或环境变量 TRIAL_MODEL)")
    ap.add_argument("--api-url", default=API_URL, help="API地址(真调用必填; 或环境变量 TRIAL_API_URL)")
    ap.add_argument("--mock", action="store_true", help="模拟LLM, 不联网不耗key")
    ap.add_argument("--seed", type=int, default=None, help="随机种子(配对可复现)")
    ap.add_argument("--json", action="store_true", help="输出结构化JSON")
    ap.add_argument("-h", "--help", action="help")
    args = ap.parse_args()

    goals = parse_goals(args)
    if not goals or len(goals) > MAX_AGENT:
        sys.exit("错误: 目标数须为1-%d, 当前%d" % (MAX_AGENT, len(goals)))
    if not args.mock:
        if not args.key:
            sys.exit("错误: 非mock模式必须 --key 或环境变量 TRIAL_KEY 传入用户授权API key")
        if not args.api_url:
            sys.exit("错误: 非mock模式必须 --api-url 或环境变量 TRIAL_API_URL")
        if not args.model:
            sys.exit("错误: 非mock模式必须 --model 或环境变量 TRIAL_MODEL")

    cfg = argparse.Namespace(mock=args.mock, key=args.key, model=args.model,
                             api=args.api_url, workers=args.workers,
                             agent="", stage="")
    docs, work = os.path.realpath(args.docs), os.path.realpath(args.work)
    if not os.path.isdir(docs):
        sys.exit("错误: 资料目录不存在: " + docs)
    ensure(work)
    r1 = os.path.join(work, "round1"); ensure(r1)
    r2 = os.path.join(work, "round2"); ensure(r2)
    ctx = {"docs": docs, "work": work, "r1": r1, "r2": r2, "wrote": []}

    if args.mock:
        log("MOCK模式: LLM决策模拟, 工具链真实执行, 不耗key")
    rounds_out = {"round1": [], "round2": []}

    log("阶段1: %d个子代理并发(%s)" % (len(goals), goals))
    res1 = run_pool([(phase1_agent, (i, g, copy.copy(cfg), ctx)) for i, g in enumerate(goals)], cfg)
    ok1 = [r for r in res1 if r and r[2]]
    log("阶段1产出: %d/%d 份md" % (len(ok1), len(goals)))
    rounds_out["round1"] = [os.path.basename(r[1]) for r in ok1]

    r1_files = [f for f in list_files(r1) if f.endswith(".md")]
    if not r1_files:
        sys.exit("错误: 阶段1无产出, 终止")
    log("阶段2: %d个子代理整合round1" % len(r1_files))
    res2 = run_pool([(phase2_agent, (i, copy.copy(cfg), ctx)) for i in range(len(r1_files))], cfg)
    ok2 = [r for r in res2 if r and r[2]]
    log("阶段2产出: %d/%d 份md" % (len(ok2), len(r1_files)))
    rounds_out["round2"] = [os.path.basename(r[1]) for r in ok2]

    cur_dir, cur = r2, [f for f in list_files(r2) if f.endswith(".md")]
    rnd = 2
    rng = random.Random(args.seed)
    total_calls = len(goals) + len(r1_files)
    while len(cur) > 1:  # 无限轮次: 收敛条件仅剩"文件数>1"
        rnd += 1
        nxt = os.path.join(work, "round%d" % rnd); ensure(nxt)
        rng.shuffle(cur)
        pairs = _pair(cur, cur_dir)
        tasks, calls, task_args = [], 0, []
        for pi, pair in enumerate(pairs):
            if len(pair) == 1:
                src = os.path.join(cur_dir, pair[0])
                if not _passable(src):
                    log("警告: 轮空晋级文件不合格: %s (仍晋级)" % pair[0])
                shutil.copy2(src, os.path.join(nxt, "agent_%d.md" % pi))
                log("轮空晋级: %s -> round%d" % (pair[0], rnd))
                continue
            a, b = os.path.join(cur_dir, pair[0]), os.path.join(cur_dir, pair[1])
            tasks.append((phase3_agent, (pi, a, b, copy.copy(cfg), ctx)))
            task_args.append((pi, a, b))
            calls += 1
        ctx["cur"], ctx["nxt"] = cur_dir, nxt
        res = run_pool(tasks, cfg) if tasks else []
        okn = []
        for (pi, a, b), r in zip(task_args, res):
            if r and r[2] and os.path.isfile(os.path.join(nxt, "agent_%d.md" % pi)):
                okn.append(r)
                continue
            src = a if os.path.getsize(a) >= os.path.getsize(b) else b
            shutil.copy2(src, os.path.join(nxt, "agent_%d.md" % pi))
            log("回退晋级(合并失败): %s -> round%d" % (os.path.basename(src), rnd))
            okn.append((pi, os.path.join(nxt, "agent_%d.md" % pi), True))
        total_calls += calls
        log("阶段3-第%d轮: 输入%d -> 子代理%d(调用%d) -> 产出%d (淘汰%d)" % (
            rnd - 2, len(cur), calls, calls, len(okn), len(cur) - len(pairs)))
        cur_dir, cur = nxt, [f for f in list_files(nxt) if f.endswith(".md")]
        rounds_out["round%d" % rnd] = [os.path.basename(f) for f in cur]
        if len(cur) == 0:
            sys.exit("错误: 阶段3某轮无产出, 终止")

    final = os.path.join(work, "final.md")
    shutil.copy2(_final_src(cur, cur_dir), final)
    log("完成: 最终方案 = %s (总子代理调用 %d)" % (final, total_calls))

    if args.json:
        print(json.dumps({"final": final, "rounds": rounds_out,
                          "total_calls": total_calls, "mock": args.mock},
                         ensure_ascii=False, indent=2))
    else:
        print(final)

if __name__ == "__main__":
    main()