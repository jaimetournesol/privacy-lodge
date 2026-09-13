"""One capability-aware prompt for all CLI adapters, refreshed on every request."""
from pathlib import Path
from . import config


def prompt(name):
    return (config.REPO / 'prompts' / (name + '.md')).read_text().strip()


def render(session, servers):
    parts = [prompt('shared'), f"Machine: {session.node['name']}; project: {session.project['name']}; "
             f"agent: {session.agent['name']}; backend: {session.backend}.\n"
             f"Project working directory: {session.project['dir']}. Use this directory for relative project files.",
             'Configured MCP servers: ' + ', '.join(sorted(servers)) + '.',
             prompt('conductor' if 'conductor' in servers else 'worker')]
    if 'host' in servers:
        parts.append('host.computer controls THIS machine. Screenshot before acting and after visible changes. '
                     'Use coordinates in the returned image dimensions, at most 1280px per edge and 256 KiB JPEG. '
                     'Human live-stream quality does not increase the agent screenshot budget.')
    if 'surface' in servers:
        parts.append(prompt('surface'))
    if 'voice' in servers:
        parts.append('voice.say speaks text; voice.ask asks a spoken question and opens the microphone. '
                     'Both require this request to have response_channel=voice. Use text otherwise.')
    brief = session.project.get('instructions')
    if brief:
        parts.append('# Standing project brief\n' + brief)
    return '\n\n'.join(parts)
