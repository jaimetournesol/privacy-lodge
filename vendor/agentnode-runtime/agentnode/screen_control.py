"""A human owns input through one authenticated socket; all adapters arbitrate here."""
import asyncio
import math
import time
from .policy import PolicyError
from .screenshots import dimensions


class ScreenControl:
    def __init__(self):
        self.owner = None
        self.expires = 0
        self.lock = asyncio.Lock()

    def busy(self):
        if self.owner and self.expires <= time.monotonic():self.owner=None
        return self.owner is not None

    def acquire(self, owner):
        if self.busy() and self.owner is not owner:raise PolicyError('Another person controls this machine',409)
        self.owner=owner;self.expires=time.monotonic()+15

    def release(self, owner):
        if self.owner is owner:self.owner=None;self.expires=0

    def check(self, owner=None):
        if owner is not None:
            if not self.busy() or self.owner is not owner:raise PolicyError('Screen control expired',409)
            self.expires=time.monotonic()+15
        elif self.busy():raise PolicyError('Human screen control is active; wait before using computer input',409)


def geometry(screen):
    if not screen or not screen.latest or screen.error or time.time()-getattr(screen,'frame_time',0)>5:
        raise PolicyError('No current screen frame; reconnect the screen first',409)
    w,h=screen.size
    return dict(id='primary',revision=getattr(screen,'geometry_revision',0),width=w,height=h)


def input_message(body, current):
    if body.get('screen')!=current['id'] or body.get('geometry')!=current['revision']:
        raise PolicyError('Display geometry changed; wait for the new frame',409)
    action=body.get('action')
    if action not in ('mouse_move','left_click','right_click','middle_click','double_click','scroll','type','key'):
        raise PolicyError('Unsupported input action',422)
    coord=body.get('coordinate')
    if action in ('mouse_move','left_click','right_click','middle_click','double_click','scroll'):
        if not isinstance(coord,list) or len(coord)!=2 or any(type(v) not in (int,float) or not math.isfinite(v) or not 0<=v<=1 for v in coord):
            raise PolicyError('Coordinates must be inside the screen',422)
        w,h=dimensions(current['width'],current['height'])
        coord=[min(w-1,round(coord[0]*w)),min(h-1,round(coord[1]*h))]
    text=body.get('text')
    if text is not None and (not isinstance(text,str) or len(text)>512):raise PolicyError('Text is limited to 512 characters',422)
    amount=body.get('amount',0)
    if type(amount) is not int or not -20<=amount<=20:raise PolicyError('Invalid scroll amount',422)
    return action,coord,text,amount
