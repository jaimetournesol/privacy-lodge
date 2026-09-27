"""agentnode CLI: serve, projects, nodes, token, tls, fleet import."""
import json
import os
import subprocess
import sys
from pathlib import Path

from . import config


def _ensure_tls():
    cert, key = config.TLS / "cert.pem", config.TLS / "key.pem"
    if cert.exists() and key.exists():
        return
    if cert.exists() or key.exists():
        raise ValueError('Incomplete TLS pair; restore the missing cert.pem/key.pem before setup')
    ip = ""
    try:
        import socket
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
        s.close()
    except Exception:
        pass
    san = f"subjectAltName=DNS:localhost,DNS:{config.node()['name']},IP:127.0.0.1" + (f",IP:{ip}" if ip else "")
    subprocess.run(["openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-days", "3650", "-subj", "/CN=agentnode",
                    "-addext", san, "-keyout", str(key), "-out", str(cert)], check=True, capture_output=True)
    key.chmod(0o600)
    print(f"generated TLS cert for {ip or 'localhost'} in {config.TLS}")


def cmd_serve(_):
    _ensure_tls()
    from .server import main
    main()


def cmd_ls(_):
    n = config.node()
    print(f"node {n['name']}  http://0.0.0.0:{n['port']}  control={n.get('control')}  wake='{n.get('wake_word')}'")
    for p in config.projects():
        print(f"  {p['id']:20} {p['dir']}  model={p.get('model') or n['model']}{'  [conductor]' if p.get('conductor') else ''}")
    if config.nodes():
        print("nodes:")
        for x in config.nodes():
            print(f"  {x['name']:20} {x['url']}")


def cmd_add(a):
    d = os.path.abspath(os.path.expanduser(a.dir))
    name = a.name or Path(d).name
    pid = config.validate_id(a.id or config.slug(name))
    items = config.projects()
    if any(p["id"] == pid for p in items):
        sys.exit(f"project '{pid}' exists")
    from .backends import model_for, effort_for, validate_effort
    backend = a.backend or config.node().get('backend', 'claude')
    model = model_for(backend, config.node(), {'backend': backend, 'model': a.model})
    validate_effort(backend, effort_for(backend, config.node(), {'backend': backend, 'reasoning_effort': a.reasoning_effort}), model, config.node())
    Path(d).mkdir(parents=True, exist_ok=True)
    items.append({"id": pid, "name": name, "dir": d, "backend": backend, "model": model, "host_tools": not a.no_host, "conductor": a.conductor})
    if a.reasoning_effort:
        items[-1]["reasoning_effort"] = a.reasoning_effort
    config.save_projects(items)
    print(f"added {pid} → {d} (restart the service, or POST /api/projects while it runs)")


def cmd_rm(a):
    config.save_projects([p for p in config.projects() if p["id"] != a.id])
    print(f"removed {a.id} (data kept in {config.DATA / a.id})")


def cmd_token(_):
    print(config.node()["token"])


def cmd_set(a):
    n = config.node()
    for kv in a.pairs:
        k, v = kv.split("=", 1)
        if v.lower() in ("true", "false"):
            v = v.lower() == "true"
        elif v.isdigit():
            v = int(v)
        elif v.lower() in ("none", "null", ""):
            v = None
        if k.endswith("reasoning_effort"):
            from .backends import validate_effort
            validate_effort(k[:-len("_reasoning_effort")] if k != "reasoning_effort" else n.get("backend", "claude"), v)
        n[k] = v
    config.save_node(n)
    print(json.dumps({k: v for k, v in n.items() if k != "token"}, indent=1))


def cmd_invite(a):
    from .join import create_invite
    n = config.node()
    invite = create_invite(n, url=a.url, ttl=a.ttl, note=a.note or '')
    import time
    print(invite['code'])
    print(f"# Single use; expires {time.strftime('%H:%M', time.localtime(invite['expires']))}. The machine must reach {invite['url']}.", file=sys.stderr)
    print("# On the new machine: python -m agentnode join CODE   (or AGENTNODE_JOIN_CODE=CODE for the worker container)", file=sys.stderr)


def cmd_join(a):
    from .join import worker_join
    worker_join(a.code, name=a.name, advertise=a.advertise, confirm=not a.no_confirm, restart=not a.no_restart)


def cmd_ca(a):
    from . import fleet_ca
    n = config.node()
    if a.action == 'init':
        print('fleet CA ready:', fleet_ca.ensure_ca())
    elif a.action == 'local':
        from .join import guess_local_ip
        sans = [*(a.san or []), guess_local_ip(), n['name']]
        backup = fleet_ca.issue_local(n['name'], sans)
        print('installed a fleet-CA-signed certificate for', ', '.join(dict.fromkeys(sans)),
              '(previous pair in ' + str(backup) + ')' if backup else '')
        from .setup import restart_services
        restart_services(n)
        print('trust ' + str(fleet_ca.ca_paths()[1]) + ' in each browser once; every fleet machine then loads without warnings')
    elif a.action == 'fingerprint':
        print(fleet_ca.fingerprint(config.TLS / 'cert.pem'))
    elif a.action == 'show':
        key, crt = fleet_ca.ca_paths()
        print(crt.read_text() if crt.is_file() else 'no fleet CA; run: python -m agentnode ca init')


def cmd_nodes_add(a):
    items = [x for x in config.nodes() if x["name"] != a.name]
    items.append({"name": a.name, "url": a.url.rstrip("/"), "token": a.token})
    config.save_nodes(items)
    print(f"node {a.name} → {a.url}")


def cmd_import_fleet(_):
    """Adopt agentfleet agents that live on this host (~/.agentfleet/fleet.json)."""
    reg = Path.home() / ".agentfleet" / "fleet.json"
    if not reg.exists():
        sys.exit("no ~/.agentfleet/fleet.json here")
    data = json.loads(reg.read_text())
    agents = data.get("agents", data) if isinstance(data, dict) else data
    items = config.projects()
    have = {p["id"] for p in items}
    n = 0
    for name, a in (agents.items() if isinstance(agents, dict) else ((x.get("name"), x) for x in agents)):
        if not name or a.get("host"):
            continue  # remote fleet agents belong to their own node
        d = a.get("dir") or a.get("workdir") or str(Path.home() / "AgentFleet" / name)
        pid = config.slug(name)
        if pid in have:
            continue
        items.append({"id": pid, "name": name, "dir": d, "model": a.get("model"), "host_tools": True, "fleet": True,
                      "description": a.get("description")})
        # resume the fleet agent's existing Claude session as this project's "main" agent
        pdata = config.project_data(pid)
        if a.get("session_id") and not (pdata / "agents.json").exists():
            import time
            (pdata / "agents.json").write_text(json.dumps({"active": "main", "running": False, "agents": [
                {"id": "main", "name": "main (fleet)", "session_id": a["session_id"], "total_cost": 0.0, "history_file": None,
                 "surface_ws": None, "created": time.time(), "last_used": time.time()}]}, indent=1))
        n += 1
    config.save_projects(items)
    print(f"imported {n} fleet agent(s)")


def main():
    import argparse
    ap = argparse.ArgumentParser(prog="agentnode")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("serve").set_defaults(f=cmd_serve)
    sub.add_parser("ls").set_defaults(f=cmd_ls)
    p = sub.add_parser("add"); p.add_argument("dir"); p.add_argument("--name"); p.add_argument("--id"); p.add_argument("--model"); p.add_argument("--backend", choices=("claude", "codex", "opencode"))
    p.add_argument("--reasoning-effort", help="explicit CLI reasoning effort, e.g. high; omit for the node/CLI default")
    p.add_argument("--no-host", action="store_true"); p.add_argument("--conductor", action="store_true"); p.set_defaults(f=cmd_add)
    p = sub.add_parser("rm"); p.add_argument("id"); p.set_defaults(f=cmd_rm)
    sub.add_parser("token").set_defaults(f=cmd_token)
    p = sub.add_parser("set", help="node settings, e.g. control=true wake_word=conductor surface_dir=/path hub_port=4500 codex_reasoning_effort=high"); p.add_argument("pairs", nargs="+"); p.set_defaults(f=cmd_set)
    p = sub.add_parser("nodes-add", help="register a machine by address and token (the join flow does this automatically)"); p.add_argument("name"); p.add_argument("url"); p.add_argument("token"); p.set_defaults(f=cmd_nodes_add)
    p = sub.add_parser("invite", help="control node: mint a single-use pairing code for a new machine")
    p.add_argument("--url", help="https origin machines use to reach this control node (default: this machine's LAN address)")
    p.add_argument("--ttl", type=int, default=900, help="seconds the code stays valid (default 900)"); p.add_argument("--note")
    p.set_defaults(f=cmd_invite)
    p = sub.add_parser("join", help="worker: join a control node with a pairing code")
    p.add_argument("code"); p.add_argument("--name", help="fleet name for this machine (default: hostname)")
    p.add_argument("--advertise", help="https origin the control node should use for this machine")
    p.add_argument("--confirm", action="store_true", help="confirm a previously issued certificate once the service is running")
    p.add_argument("--no-confirm", action="store_true", help="install the certificate only; confirm later with the same code")
    p.add_argument("--no-restart", action="store_true", help="do not restart installed services")
    p.set_defaults(f=cmd_join)
    p = sub.add_parser("ca", help="fleet certificate authority: init | local [--san NAME] | fingerprint | show")
    p.add_argument("action", choices=("init", "local", "fingerprint", "show")); p.add_argument("--san", action="append")
    p.set_defaults(f=cmd_ca)
    sub.add_parser("import-fleet").set_defaults(f=cmd_import_fleet)
    sub.add_parser("tls").set_defaults(f=lambda a: _ensure_tls())
    from .setup import add_commands
    add_commands(sub)
    a = ap.parse_args()
    try:
        a.f(a)
    except (ValueError, subprocess.CalledProcessError) as exc:
        ap.exit(1, str(exc) + '\n')


if __name__ == "__main__":
    main()
