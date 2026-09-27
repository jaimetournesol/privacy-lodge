"""Bundled TAG capability; credentials stay external and project overrides win."""
import shutil
from . import config


def root():
    return config.REPO / 'vendor' / 'tag-convert'


def servers():
    script = root() / 'scripts' / 'mcp-server.mjs'
    if not script.is_file():
        return {}
    return {'tag': {'command': shutil.which('node') or 'node', 'args': [str(script)]}}


def guidance():
    return ('\n\n# TAG workflows and tools\n'
            'The tag MCP provides workflow discovery, authoring, execution and monitoring. '
            'For existing workflows read ' + str(root() / 'skills/tag-workflows/SKILL.md') + '. '
            'For creating workflows, modules or MCP tools read ' + str(root() / 'skills/convert-to-tag/SKILL.md') + '. '
            'Discover current tools before use. Credentials are configured separately; never invent access or retry an uncertain run blindly.')
