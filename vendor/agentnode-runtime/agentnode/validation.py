"""Bounded request decoding and shared command validation."""
import json
import math
import re
from fastapi import HTTPException
from .delivery import DeliveryError

MAX_BODY=24*1024*1024

async def body_bytes(request, limit=MAX_BODY):
    content_length=request.headers.get('content-length')
    if content_length:
        try:size=int(content_length)
        except ValueError:raise HTTPException(400,'Invalid content length')
        if size<0:raise HTTPException(400,'Invalid content length')
        if size>limit:raise HTTPException(413,'Request is too large')
    data=bytearray()
    async for chunk in request.stream():
        if len(data)+len(chunk)>limit:raise HTTPException(413,'Request is too large')
        data.extend(chunk)
    return bytes(data)

async def json_object(request, allow_empty=False):
    raw=await body_bytes(request)
    if not raw and allow_empty:return {}
    try:value=json.loads(raw)
    except (ValueError,UnicodeError):raise HTTPException(422,'Expected a JSON object')
    if not isinstance(value,dict):raise HTTPException(422,'Expected a JSON object')
    return value

def project_fields(body):
    import os
    if os.environ.get('LODGE_MODE') == '1':
        if body.get('backend') not in (None, 'codex'):
            raise HTTPException(422, 'Privacy Lodge uses Codex only.')
        if body.get('backend') is None:
            body['backend'] = 'codex'
        if body.get('host_tools') is True:
            raise HTTPException(422, 'Host control is not available in Privacy Lodge.')
    for key,limit in [('dir',4096),('name',200),('id',64),('model',100),('instructions',100000),('session_id',200)]:
        value=body.get(key)
        if value is not None and (not isinstance(value,str) or len(value)>limit):
            raise HTTPException(422,f'Invalid {key}')
    for key in ('host_tools','conductor','workspace','start','codex_yolo','opencode_yolo'):
        if key in body and not isinstance(body[key],bool):raise HTTPException(422,f'{key} must be true or false')

    from .backends import validate_backend
    from .mcp_config import validate_servers, RESERVED
    try:
        if body.get('backend') is not None:
            validate_backend(body['backend'])
        if 'mcps' in body:
            extra = validate_servers(body['mcps'])
            if RESERVED.intersection(extra):
                raise ValueError('host, surface, voice and conductor are reserved MCP roles')
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc

def command_parts(files,annotations,voice):
    if files is not None and (not isinstance(files,list) or len(files)>8 or any(not isinstance(f,str) or not f or len(f)>4096 for f in files)):
        raise DeliveryError('Supply up to eight attachment paths')
    if voice is not None and (not isinstance(voice,dict) or any(
            (not isinstance(v,str) or not re.fullmatch(r'[A-Za-z0-9_-]{1,100}',v)) if k=='client_id' else not isinstance(v,bool)
            for k,v in voice.items())):
        raise DeliveryError('Invalid voice options')
    if annotations is None:return
    if not isinstance(annotations,list) or len(annotations)>8:raise DeliveryError('Supply up to eight annotations')
    for annotation in annotations:
        if not isinstance(annotation,dict):raise DeliveryError('Invalid annotation')
        for key,limit in [('componentId',128),('title',200),('componentType',40)]:
            value=annotation.get(key,'')
            if not isinstance(value,str) or len(value)>limit:raise DeliveryError('Invalid annotation label')
        image=annotation.get('imageDataUrl','')
        if not isinstance(image,str) or len(image)>2800000 or (image and not re.fullmatch(r'data:image/(?:png|jpeg|webp);base64,[A-Za-z0-9+/]+={0,2}',image)):
            raise DeliveryError('Invalid annotation image')
        marks=annotation.get('marks')
        if not isinstance(marks,list) or len(marks)>100:raise DeliveryError('Invalid annotation marks')
        for mark in marks:
            if not isinstance(mark,dict) or any(type(mark.get(k)) not in (int,float) or not math.isfinite(mark[k]) or not 0<=mark[k]<=1 for k in ('x','y','w','h')):
                raise DeliveryError('Invalid annotation coordinates')
            if mark['x']+mark['w']>1.001 or mark['y']+mark['h']>1.001:raise DeliveryError('Annotation lies outside the panel')
