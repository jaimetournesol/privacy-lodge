"""Model choices from this node's configuration, without paid discovery calls."""
import re
from .backends import BACKENDS, model_for


def catalog(node, projects, agents):
    result = {}
    for backend in BACKENDS:
        try:
            default = model_for(backend, node)
        except ValueError:
            default = None
        models = {default} if default else set()
        for item in [*projects, *agents]:
            if (item.get('backend') or node.get('backend', 'claude')) == backend:
                model = item.get('model')
                if isinstance(model, str) and model and (backend != 'opencode' or re.fullmatch(r'\S+/\S+', model)):
                    models.add(model)
        result[backend] = {'default': default, 'models': sorted(models)}
    return {'backend': node.get('backend', 'claude'), 'backends': result,
            'source': 'configured', 'note': 'Configured models; provider access is not verified.'}
