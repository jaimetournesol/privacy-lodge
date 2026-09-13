#!/usr/bin/env python3
"""`voice` MCP server for the TVPC agent: one tool, `say`.

Zero dependencies, newline-delimited JSON-RPC over stdio. `say` posts a short
spoken note to the bridge, which the web UI reads aloud (browser TTS or
ElevenLabs). It is deliberately NOT the chat reply: a heads-up, one sentence.
"""
import json
import os
import sys
import urllib.request

BRIDGE = os.environ.get("AGENTNODE_URL", "http://127.0.0.1:8444")
PROJECT = os.environ.get("AGENTNODE_PROJECT", "")

TOOLS = [
    {
        "name": "say",
        "description": (
            "Use only when the current request has response channel voice. For chat requests, reply in text instead. "
            "Speak ONE short sentence aloud to the human and END the exchange (the microphone goes back to waiting for "
            "\u201cHey TVPC\u201d). Use it for acknowledgements (\"On it.\"), progress notes, and outcomes (\"Done, the doc is on the desktop.\"). "
            "Never read your chat reply or details aloud; under ~20 words, conversational, no markdown."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {"text": {"type": "string", "description": "What to say (one short sentence)"}},
            "required": ["text"],
        },
    },
    {
        "name": "ask",
        "description": (
            "Use only when the current request has response channel voice. For chat requests, ask in text instead. "
            "Ask the human ONE short question aloud and LISTEN for their answer: the microphone opens right after you speak, "
            "no wake word needed, and their reply arrives as your next message. Use it only when you genuinely need a decision or "
            "information (\"Firefox wants a password, should I skip it?\"). Don't ask when you can just do it or when a `say` closes the topic."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {"text": {"type": "string", "description": "The question (one short sentence)"}},
            "required": ["text"],
        },
    },
]


def write(msg):
    sys.stdout.write(json.dumps(msg) + "\n")
    sys.stdout.flush()


def say(text: str, expect_reply: bool = False) -> str:
    req = urllib.request.Request(BRIDGE + "/api/say", data=json.dumps({"text": text, "expect_reply": expect_reply, "project": PROJECT}).encode(),
                                 headers={"Content-Type": "application/json", "X-Agentnode-Token": os.environ.get("AGENTNODE_TOKEN", "")}, method="POST")
    with urllib.request.urlopen(req, timeout=5) as r:
        result=json.loads(r.read().decode() or '{}')
    if result.get('spoken') is False:
        return result.get('message') or 'This request requires a text reply. Use chat instead of voice.'
    return "Asked; their answer will arrive as the next message." if expect_reply else "Said it."


for line in sys.stdin:
    line = line.strip()
    if not line:
        continue
    try:
        msg = json.loads(line)
    except Exception:
        continue
    mid, method, params = msg.get("id"), msg.get("method"), msg.get("params") or {}
    if mid is None:
        continue  # notification
    try:
        if method == "initialize":
            write({"jsonrpc": "2.0", "id": mid, "result": {
                "protocolVersion": params.get("protocolVersion", "2025-06-18"),
                "capabilities": {"tools": {}}, "serverInfo": {"name": "voice", "version": "0.1.0"}}})
        elif method == "tools/list":
            write({"jsonrpc": "2.0", "id": mid, "result": {"tools": TOOLS}})
        elif method == "tools/call":
            name = params.get("name")
            if name not in ("say", "ask"):
                raise ValueError("unknown tool")
            text = str((params.get("arguments") or {}).get("text", "")).strip()
            if not text:
                raise ValueError("text is required")
            write({"jsonrpc": "2.0", "id": mid, "result": {"content": [{"type": "text", "text": say(text[:400], name == "ask")}]}})
        elif method == "ping":
            write({"jsonrpc": "2.0", "id": mid, "result": {}})
        else:
            write({"jsonrpc": "2.0", "id": mid, "error": {"code": -32601, "message": f"method not found: {method}"}})
    except Exception as e:
        write({"jsonrpc": "2.0", "id": mid, "result": {"content": [{"type": "text", "text": f"Error: {e}"}], "isError": True}})
