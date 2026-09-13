"""Private, bounded chat uploads. Saving a file never starts an agent turn."""
import base64
import binascii
import os
from pathlib import Path
import re
import uuid
from fastapi import HTTPException
from .storage import locked

MAX_FILE_BYTES = 16 * 1024 * 1024
MAX_PROJECT_BYTES = 512 * 1024 * 1024


def save_attachment(project_data, body):
    name, encoded = body.get('name'), body.get('data')
    if not isinstance(name,str) or not name or len(name)>255 or not isinstance(encoded,str):
        raise HTTPException(422,'Supply a file name and base64 data.')
    if len(encoded)>4*((MAX_FILE_BYTES+2)//3):
        raise HTTPException(413,'Maximum file size is 16 MB.')
    try:data=base64.b64decode(encoded,validate=True)
    except (ValueError,binascii.Error):raise HTTPException(422,'Invalid file data.')
    if len(data)>MAX_FILE_BYTES:raise HTTPException(413,'Maximum file size is 16 MB.')
    name=re.sub(r'[^\w. ()-]','_',name.replace('\\','/').rsplit('/',1)[-1]).strip(' .')[:160] or 'attachment'
    root=Path(project_data)/'attachments'
    root.mkdir(parents=True,exist_ok=True,mode=0o700)
    with locked(root/'quota'):
        if sum(p.stat().st_size for p in root.glob('*/*') if p.is_file())+len(data)>MAX_PROJECT_BYTES:
            raise HTTPException(413,'This project has reached its attachment storage limit.')
        directory=root/uuid.uuid4().hex;directory.mkdir(mode=0o700)
        path=directory/name
        try:
            with os.fdopen(os.open(path,os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600),'wb') as file:file.write(data)
        except Exception:
            path.unlink(missing_ok=True);directory.rmdir();raise
        return {'path':str(path.resolve()),'name':name,'size':len(data)}
