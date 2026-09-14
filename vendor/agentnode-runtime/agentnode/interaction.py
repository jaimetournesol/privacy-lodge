"""Response modality belongs to a request, never to lingering conversation history."""
from .delivery import DeliveryError

def channels(source=None, voice=None, reply=None):
    if source is None:source='voice' if voice and (voice.get('interactive') or voice.get('spoken')) else 'chat'
    if source not in ('chat','voice','automation','surface'):raise DeliveryError('Invalid request source')
    if reply is not None and reply not in ('chat','voice'):raise DeliveryError('Invalid response channel')
    channel=('voice' if source=='voice' else 'chat') if source!='automation' else (reply or 'chat')
    return source,channel

def instruction(source,channel):
    if channel=='voice':
        return (f'[request-context] Source: {source}. Response channel: voice. ' +
                ('For a new human request, your FIRST tool call must be voice say: briefly acknowledge what you will do BEFORE discovery, stage changes or execution. ' if source=='voice' else 'This is an update to an existing task: lead with its actual progress or outcome, not a new-task acknowledgement. ') +
                'After verified task acceptance, say it is underway; do not claim it is finished. Speak the actual outcome on completion or failure. '
                'Use voice ask when you need their answer. Put detailed material on the stage or in chat. '
                'Speech is routed to the originating phone/browser only; Presenter screens stay silent. This context applies to this request only. [/request-context]')
    return (f'[request-context] Source: {source}. Response channel: chat. '
            'Respond in text in this conversation. Do not call voice say or voice ask and do not generate spoken acknowledgements. '
            'Ask any necessary questions in chat. Earlier voice exchanges do not change this request. [/request-context]')
