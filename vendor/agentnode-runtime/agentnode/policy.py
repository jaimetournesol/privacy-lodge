"""Execution policy shared by HTTP, sockets, MCP and background delivery."""
import os
from . import config
from .delivery import DeliveryError


class PolicyError(DeliveryError):
    def __init__(self, message, status=409):
        super().__init__(message)
        self.status = status


def check_mutation():
    if (config.HOME / 'deployment-maintenance.json').exists():
        raise PolicyError('Deployment in progress; retry shortly', 503)


def approval(agent, project):
    return agent.get('approval', project.get('approval', 'approved'))


def check_execution(agent, project):
    check_mutation()
    if os.environ.get('LODGE_MODE') == '1' and agent.get('backend', project.get('backend', 'codex')) != 'codex':
        raise PolicyError('Privacy Lodge supports Codex agents only.', 422)
    if approval(agent, project) != 'approved':
        raise PolicyError('This agent needs human approval before it can run', 403)
