"""Minimal client for Claude Code's headless stream-json control protocol.

Mirrors what the Agent SDK does:
- spawn `claude -p --input-format stream-json --output-format stream-json --verbose
  --permission-prompt-tool stdio ...`
- send an `initialize` control_request, then user messages as JSON lines
- answer `can_use_tool` (permissions) and `mcp_message` (in-process "sdk" MCP servers)
"""

import asyncio
import itertools
import json
import os

import cvlog
import platform_util

_ids = itertools.count(1)
# Anything that would make claude bill an API account or another provider instead of the user's claude.ai
# subscription. cv strips these from every claude it starts, so it can only ever use the subscription.
NOT_SUBSCRIPTION = ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_BASE_URL", "CLAUDE_CODE_USE_BEDROCK",
                    "CLAUDE_CODE_USE_VERTEX", "CLAUDE_CODE_USE_FOUNDRY")


def subscription_env(extra=None):
    """os.environ for a claude process, minus NOT_SUBSCRIPTION, plus `extra`."""
    env = {k: v for k, v in os.environ.items() if k.upper() not in NOT_SUBSCRIPTION}
    return {**env, **(extra or {})}


class SdkMcpServer:
    """An MCP server served by the host over the control channel (config type "sdk")."""

    def __init__(self, name, tools):
        # tools: {tool_name: (description, input_schema, async handler(args) -> str)}
        self.name = name
        self.tools = tools

    async def handle(self, msg):
        method, mid = msg.get("method"), msg.get("id")
        if method == "initialize":
            result = {
                "protocolVersion": msg.get("params", {}).get("protocolVersion", "2025-06-18"),
                "capabilities": {"tools": {}},
                "serverInfo": {"name": self.name, "version": "0.1"},
            }
        elif method == "tools/list":
            result = {"tools": [
                {"name": n, "description": d, "inputSchema": s}
                for n, (d, s, _) in self.tools.items()
            ]}
        elif method == "tools/call":
            p = msg["params"]
            _, _, fn = self.tools[p["name"]]
            try:
                text = await fn(p.get("arguments", {}))
            except Exception as e:
                cvlog.log.event("TOOL-ERROR", f"{self.name}.{p['name']} {e!r}")
                raise
            cvlog.log.event("TOOL", f"{self.name}.{p['name']} {json.dumps(p.get('arguments', {}))[:500]} -> {text[:300]}")
            result = {"content": [{"type": "text", "text": text}]}
        elif mid is None:
            return None  # notification
        else:
            return {"jsonrpc": "2.0", "id": mid,
                    "error": {"code": -32601, "message": f"unknown method {method}"}}
        return {"jsonrpc": "2.0", "id": mid, "result": result}


class ClaudeProc:
    def __init__(self, args, cwd=None, can_use_tool=None, sdk_servers=(), on_event=None, env=None, name="claude"):
        self.name = name  # for the logs: "front", "task-t1", …
        self.args = args
        self.cwd = cwd
        self.env = subscription_env(env)
        self.can_use_tool = can_use_tool  # async (tool_name, input, request) -> response dict
        self.sdk_servers = {s.name: s for s in sdk_servers}
        self.on_event = on_event or (lambda ev: None)
        self.pending = {}
        self.results = asyncio.Queue()  # one item per finished turn ("result" message)
        self.session_id = None

    async def start(self):
        cmd = [platform_util.claude_exe(), "-p", "--input-format", "stream-json", "--output-format", "stream-json",
               "--verbose", "--permission-prompt-tool", "stdio", *self.args]
        if self.sdk_servers:
            cfg = {"mcpServers": {n: {"type": "sdk", "name": n} for n in self.sdk_servers}}
            cmd += ["--mcp-config", json.dumps(cfg)]
        try:
            self.proc = await asyncio.create_subprocess_exec(
                *cmd, cwd=self.cwd, env=self.env, limit=2**24,
                stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                creationflags=platform_util.new_group_flags())  # Windows: own process group, so the tree can be killed
        except FileNotFoundError as e:
            raise platform_util.not_found_error(e) from e
        cvlog.log.event(f"{self.name}:start", f"pid={self.proc.pid} cwd={self.cwd} args={' '.join(self.args)[:300]}")
        self._reader = asyncio.create_task(self._read())
        self._errs = asyncio.create_task(self._read_stderr())
        return await self.control({"subtype": "initialize", "hooks": None})

    async def _write(self, obj):
        cvlog.log.raw(self.name, "out", obj)
        self.proc.stdin.write((json.dumps(obj) + "\n").encode())
        await self.proc.stdin.drain()

    async def control(self, request, timeout=60):
        rid = f"req_{next(_ids)}"
        fut = asyncio.get_running_loop().create_future()
        self.pending[rid] = fut
        await self._write({"type": "control_request", "request_id": rid, "request": request})
        return await asyncio.wait_for(fut, timeout)

    async def send(self, text):
        cvlog.log.event(f"{self.name}:send", text[:600])
        await self._write({"type": "user", "session_id": "",
                           "message": {"role": "user", "content": text},
                           "parent_tool_use_id": None})

    async def interrupt(self):
        cvlog.log.event(f"{self.name}:interrupt")
        return await self.control({"subtype": "interrupt"})

    async def _read_stderr(self):
        async for line in self.proc.stderr:
            text = line.decode(errors="replace").rstrip()
            if text:
                cvlog.log.event(f"{self.name}:stderr", text[:400])
            self.on_event({"type": "_stderr", "text": text})

    async def _read(self):
        async for raw in self.proc.stdout:
            try:
                ev = json.loads(raw)
            except json.JSONDecodeError:
                cvlog.log.event(f"{self.name}:bad-json", raw[:300])
                self.on_event({"type": "_raw", "text": raw.decode(errors="replace")})
                continue
            cvlog.log.raw(self.name, "in", ev)
            t = ev.get("type")
            if t == "assistant":
                for b in ev.get("message", {}).get("content", []):
                    if b.get("type") == "text" and b.get("text", "").strip():
                        cvlog.log.event(f"{self.name}:text", b["text"][:500])
                    elif b.get("type") == "tool_use":
                        cvlog.log.event(f"{self.name}:tool", f"{b.get('name')} {json.dumps(b.get('input', {}))[:400]}")
            elif t == "result":
                u = ev.get("usage", {})
                cvlog.log.event(f"{self.name}:result", f"{ev.get('subtype')} turns={ev.get('num_turns')} "
                                f"in={u.get('input_tokens')} cache_read={u.get('cache_read_input_tokens')} "
                                f"out={u.get('output_tokens')} :: {(ev.get('result') or '')[:300]}")
            elif t == "system" and ev.get("subtype") not in (None, "init", "status", "thinking_tokens", "task_started",
                                                             "task_notification", "task_progress"):
                cvlog.log.event(f"{self.name}:system", f"{ev.get('subtype')} {json.dumps(ev)[:200]}")
            if t == "control_response":
                r = ev["response"]
                fut = self.pending.pop(r.get("request_id"), None)
                if fut and not fut.done():
                    if r.get("subtype") == "error":
                        fut.set_exception(RuntimeError(r.get("error")))
                    else:
                        fut.set_result(r.get("response"))
                continue
            if t == "control_request":
                asyncio.create_task(self._answer(ev))
                continue
            if t == "system" and ev.get("subtype") == "init":
                self.session_id = ev.get("session_id")
            self.on_event(ev)
            if t == "result":
                await self.results.put(ev)
        cvlog.log.event(f"{self.name}:exit", f"returncode={self.proc.returncode}")
        await self.results.put(None)  # process exited

    async def _answer(self, ev):
        rid, req = ev["request_id"], ev["request"]
        try:
            sub = req["subtype"]
            if sub == "can_use_tool":
                if not self.can_use_tool:
                    resp = {"behavior": "deny", "message": "no permission handler"}
                else:
                    resp = await self.can_use_tool(req["tool_name"], req["input"], req)
            elif sub == "mcp_message":
                out = await self.sdk_servers[req["server_name"]].handle(req["message"])
                resp = {"mcp_response": out or {"jsonrpc": "2.0", "result": {}}}
            else:
                raise RuntimeError(f"unsupported control request {sub}")
            msg = {"subtype": "success", "request_id": rid, "response": resp}
            if sub == "can_use_tool":
                cvlog.log.event(f"{self.name}:perm", f"{req['tool_name']} -> {resp.get('behavior')}"
                                + (f" ({resp.get('message')})" if resp.get("message") else "")
                                + (f" mode={resp['updatedPermissions'][0].get('mode')}" if resp.get("updatedPermissions") else ""))
        except Exception as e:  # noqa: BLE001 - report every failure back to the CLI
            cvlog.log.event(f"{self.name}:control-error", f"{req.get('subtype')} {e!r}")
            msg = {"subtype": "error", "request_id": rid, "error": str(e)}
        try:
            await self._write({"type": "control_response", "response": msg})
        except (ConnectionResetError, BrokenPipeError):
            pass  # the CLI already exited (e.g. during shutdown)

    async def close(self):
        if not hasattr(self, "proc"):
            return  # never started (e.g. startup failed early)
        if self.proc.returncode is None:
            self.proc.stdin.close()
            try:
                await asyncio.wait_for(self.proc.wait(), 10)
            except asyncio.TimeoutError:
                platform_util.kill_tree(self.proc)  # Windows: taskkill /T first, then kill()
