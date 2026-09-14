"""Portable MCP configuration: stdio and Streamable HTTP on every backend."""
import re

RESERVED = frozenset(('host', 'surface', 'voice', 'conductor'))


def validate_servers(servers):
    if not isinstance(servers, dict):
        raise ValueError('mcpServers/mcps must be an object')
    for name, spec in servers.items():
        if not isinstance(name, str) or not re.fullmatch(r'[A-Za-z][A-Za-z0-9_-]{0,63}', name):
            raise ValueError('Invalid MCP server name')
        if not isinstance(spec, dict):
            raise ValueError(f'MCP {name}: expected a configuration object')
        remote = 'url' in spec
        allowed = {'type', 'url', 'headers'} if remote else {'type', 'command', 'args', 'env'}
        if set(spec) - allowed:
            raise ValueError(f'MCP {name}: unsupported fields; use portable command/args/env or url/headers')
        if spec.get('type', 'http' if remote else 'stdio') != ('http' if remote else 'stdio'):
            raise ValueError(f'MCP {name}: only stdio and Streamable HTTP are supported on all backends')
        if remote:
            if not isinstance(spec['url'], str) or not spec['url'].startswith(('http://', 'https://')):
                raise ValueError(f'MCP {name}: invalid HTTP URL')
        elif not isinstance(spec.get('command'), str) or not spec['command'].strip():
            raise ValueError(f'MCP {name}: command required')
        args = spec.get('args', [])
        if not isinstance(args, list) or any(not isinstance(a, str) for a in args):
            raise ValueError(f'MCP {name}: args must be an array of strings')
        for key in ('env', 'headers'):
            values = spec.get(key, {})
            if not isinstance(values, dict) or any(not isinstance(k, str) or not isinstance(v, str) for k, v in values.items()):
                raise ValueError(f'MCP {name}: {key} must contain string values')
    return servers


def opencode_servers(servers):
    result = {}
    for name, spec in validate_servers(servers).items():
        if 'url' in spec:
            result[name] = {'type': 'remote', 'url': spec['url'], 'headers': spec.get('headers', {}), 'oauth': False, 'enabled': True}
        else:
            result[name] = {'type': 'local', 'command': [spec['command'], *spec.get('args', [])],
                            'environment': spec.get('env', {}), 'enabled': True}
    return result
