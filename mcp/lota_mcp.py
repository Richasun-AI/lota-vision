#!/usr/bin/env python3
"""LOTA MCP 服务器（零依赖，标准库实现 stdio JSON-RPC）。

让 Claude Code / Claude Desktop / 任何支持 MCP 的 agent 直接操作 LOTA 引擎：
查子项目和模型、跑工具和模板、质检图片（流式找问题）、看质检记录、切换正式模型。

环境变量：
  LOTA_URL        引擎地址，默认 http://127.0.0.1:8795
  LOTA_API_TOKEN  访问口令（引擎 .env 里的 LOTA_API_TOKEN）

Claude Code 配置示例（.mcp.json）：
  {"mcpServers": {"lota": {"command": "python", "args": ["/path/to/lota/mcp/lota_mcp.py"],
                              "env": {"LOTA_URL": "http://<server>:8795", "LOTA_API_TOKEN": "你的口令"}}}}
"""
import base64
import json
import os
import sys
import urllib.error
import urllib.request

# 改名兼容（原名 AILabel）：没设 LOTA_* 时用旧的 AILABEL_*
for _k, _v in list(os.environ.items()):
    if _k.startswith("AILABEL_"):
        os.environ.setdefault("LOTA_" + _k[8:], _v)


URL = os.environ.get("LOTA_URL", "http://127.0.0.1:8795").rstrip("/")
TOKEN = os.environ.get("LOTA_API_TOKEN", "")


def call(method, path, body=None, timeout=1800):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(URL + path, data=data, method=method,
                                 headers={"Content-Type": "application/json", **({"Authorization": "Bearer " + TOKEN} if TOKEN else {})})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read() or b"null")
    except urllib.error.HTTPError as e:
        raise RuntimeError(f"HTTP {e.code}: {e.read().decode(errors='replace')[:800]}") from None


def _obj(props, required=()):
    return {"type": "object", "properties": props, "required": list(required)}


S = {"type": "string"}
TOOLS = {
    "list_projects": ("列出所有子项目（场景）、类别、模型别名和质检规则 / List projects, classes, model aliases and inspection pipelines.",
                      _obj({}), lambda a: call("GET", "/api/agent/manifest")["projects"]),
    "get_capabilities": ("完整能力清单：所有工具（含参数说明）、模板、接口 / Full manifest of tools, templates and endpoints.",
                         _obj({}), lambda a: call("GET", "/api/agent/manifest")),
    "project_status": ("子项目概况：图片数、已标注、数据集、模型、正式模型成绩、设置 / Project summary.",
                       _obj({"project": S}, ["project"]), lambda a: call("GET", f"/api/projects/{a['project']}/hub/summary")),
    "list_models": ("模型版本列表（成绩、别名、导出文件）/ List model versions with metrics.",
                    _obj({"project": S}, ["project"]), lambda a: call("GET", f"/api/projects/{a['project']}/hub/models")),
    "run_tool": ("运行一个引擎工具（如 dataset.publish、model.train、curation.find_by_model、model.export），默认等待完成 / Run an engine tool.",
                 _obj({"project": S, "tool": S, "args": {"type": "object"}, "wait": {"type": "boolean"}, "timeout": {"type": "number"}}, ["project", "tool"]),
                 lambda a: call("POST", "/api/agent/run", {"project": a["project"], "tool": a["tool"], "args": a.get("args") or {},
                                                          "wait": a.get("wait", True), "timeout": a.get("timeout", 900)})),
    "run_template": ("运行工作流模板（如 mcb-loop 主动学习一轮），可覆盖节点参数 / Run a workflow template.",
                     _obj({"project": S, "template": S, "overrides": {"type": "object"}, "wait": {"type": "boolean"}}, ["project", "template"]),
                     lambda a: call("POST", "/api/agent/template", {k: a[k] for k in ("project", "template", "overrides", "wait") if k in a})),
    "job_status": ("查询任务状态和结果 / Get job status.", _obj({"project": S, "job_id": S}, ["project", "job_id"]),
                   lambda a: call("GET", f"/api/agent/jobs/{a['project']}/{a['job_id']}")),
    "inspect_image": ("质检一张图（本机文件路径 local_path，或服务器路径 server_path，或 url）：按检测管线判定合格/不合格并给出原因和框 / Inspect one image.",
                      _obj({"project": S, "local_path": S, "server_path": S, "url": S, "photo_type": S}, ["project"]), lambda a: _inspect(a)),
    "list_inspections": ("最近的质检记录，可按 fail/warn/pass 过滤 / Recent inspection results.",
                         _obj({"project": S, "verdict": S, "limit": {"type": "integer"}}, ["project"]),
                         lambda a: _slim(call("GET", f"/api/projects/{a['project']}/inspections?limit={a.get('limit', 20)}&verdict={a.get('verdict', '')}"))),
    "inspect_photo_all": ("全子项目质检：不指定子项目，按照片类型（如 site_ground、Site Ground）交给所有适用的规则；可带工单号/站点 / Inspect with all matching projects.",
                          _obj({"local_path": S, "server_path": S, "photo_type": S, "order": S, "site": S}, []), lambda a: _inspect_all(a)),
    "list_orders": ("有质检记录的工单 / 站点列表（不合格、需注意、合格数）/ Orders and sites with inspection results.",
                    _obj({}), lambda a: call("GET", "/api/inspections/orders")),
    "get_report": ("工单或站点的巡检审核报告：总结论、按照片类型、问题清单 / Audit report for an order or site.",
                   _obj({"order": S, "site": S}), lambda a: call("GET", f"/api/inspections/report?order={a.get('order', '')}&site={a.get('site', '')}")),
    "set_production": ("把某个模型版本设为正式使用 / Promote a model version to production.",
                       _obj({"project": S, "version": S}, ["project", "version"]),
                       lambda a: call("POST", "/api/agent/run", {"project": a["project"], "tool": "model.alias", "args": {"alias": "production", "version": a["version"]}})),
}


def _inspect(a):
    body = {"project": a["project"], "photo_type": a.get("photo_type", "")}
    if a.get("local_path"):
        with open(a["local_path"], "rb") as f:
            body["image_base64"] = base64.b64encode(f.read()).decode()
        body["meta"] = {"filename": os.path.basename(a["local_path"]), "source": "mcp"}
    elif a.get("server_path"):
        body["path"] = a["server_path"]
    elif a.get("url"):
        body["url"] = a["url"]
    else:
        raise RuntimeError("需要 local_path、server_path 或 url")
    return _slim_one(call("POST", "/api/agent/inspect", body))


def _inspect_all(a):
    meta = {k: a[k] for k in ("order", "site") if a.get(k)}
    body = {"photo_type": a.get("photo_type", ""), "meta": {**meta, "photo_type": a.get("photo_type", ""), "source": "mcp"}}
    if a.get("local_path"):
        with open(a["local_path"], "rb") as f:
            body["image_base64"] = base64.b64encode(f.read()).decode()
        body["meta"]["file"] = os.path.basename(a["local_path"])
    elif a.get("server_path"):
        body["path"] = a["server_path"]
    else:
        raise RuntimeError("需要 local_path 或 server_path")
    r = call("POST", "/api/agent/inspect-all", body)
    return {"verdict": r["verdict"], "projects": [{"project": p["project"], **_slim_one(p)} for p in r.get("projects", [])]}


def _slim_one(r):
    return {"verdict": r.get("verdict"), "photo_type": r.get("photo_type"), "ms": r.get("ms"), "id": r.get("id"),
            "results": [{"pipeline": x["pipeline"], "model": x["model"], "verdict": x["verdict"], "vlm": x.get("vlm"),
                         "findings": [f for f in x["findings"] if f["hit"]], "detections": len(x["detections"])} for x in r.get("results", [])]}


def _slim(d):
    return {"stats": d.get("stats"), "items": [_slim_one(r) | {"at": r.get("at"), "meta": r.get("meta")} for r in d.get("items", [])]}


def handle(msg):
    m, mid, p = msg.get("method"), msg.get("id"), msg.get("params") or {}
    if m == "initialize":
        return {"protocolVersion": p.get("protocolVersion", "2025-06-18"), "capabilities": {"tools": {}},
                "serverInfo": {"name": "lota", "version": "0.3.0"},
                "instructions": "LOTA 视觉平台：先 list_projects 看有哪些子项目，再用 run_tool/run_template/inspect_image。"}
    if m == "tools/list":
        return {"tools": [{"name": k, "description": v[0], "inputSchema": v[1]} for k, v in TOOLS.items()]}
    if m == "tools/call":
        name, args = p.get("name"), p.get("arguments") or {}
        if name not in TOOLS:
            return {"content": [{"type": "text", "text": f"未知工具 {name}"}], "isError": True}
        try:
            out = TOOLS[name][2](args)
            return {"content": [{"type": "text", "text": json.dumps(out, ensure_ascii=False, indent=1)[:60000]}]}
        except Exception as e:
            return {"content": [{"type": "text", "text": f"出错：{e}"}], "isError": True}
    if m == "ping":
        return {}
    if mid is None:
        return None          # 通知，不回复
    raise LookupError(m)


def main():
    for line in sys.stdin:
        if not line.strip():
            continue
        try:
            msg = json.loads(line)
        except ValueError:
            continue
        try:
            result = handle(msg)
            if msg.get("id") is not None:
                out = {"jsonrpc": "2.0", "id": msg["id"], "result": result}
            else:
                continue
        except LookupError as e:
            out = {"jsonrpc": "2.0", "id": msg.get("id"), "error": {"code": -32601, "message": f"Method not found: {e}"}}
        except Exception as e:
            out = {"jsonrpc": "2.0", "id": msg.get("id"), "error": {"code": -32603, "message": str(e)}}
        sys.stdout.write(json.dumps(out, ensure_ascii=False) + "\n")
        sys.stdout.flush()


if __name__ == "__main__":
    main()
