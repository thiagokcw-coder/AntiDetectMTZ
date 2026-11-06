"""Persistent local publication draft. Media is replaced only when changed."""
import asyncio
import json
import uuid
from pathlib import Path
from fastapi import APIRouter, File, Form, HTTPException, UploadFile
from fastapi.responses import FileResponse
from paths import APP_DIR

router = APIRouter()
ROOT = APP_DIR / 'publication_draft'
MAX_BYTES = 512 * 1024 * 1024
_lock = asyncio.Lock()


def read_draft():
    path = ROOT / 'draft.json'
    return json.loads(path.read_text(encoding='utf-8')) if path.exists() else None


@router.get('/publicacao/rascunho')
async def draft_get():
    return {'draft': read_draft()}


@router.get('/publicacao/rascunho/arquivo/{token}')
async def draft_file(token: str):
    draft = read_draft() or {}
    allowed = [v for v in draft.get('media', {}).values() if v and v['token'] == token]
    if not allowed:
        raise HTTPException(404, 'Arquivo não encontrado no rascunho')
    return FileResponse(ROOT / token, filename=allowed[0]['name'], media_type=allowed[0]['type'])


@router.post('/publicacao/rascunho')
async def draft_save(state: str = Form(...), changed: str = Form('[]'),
                     arquivo: UploadFile = File(None), capa: UploadFile = File(None)):
    try:
        data, changes = json.loads(state), json.loads(changed)
        if not isinstance(data, dict) or not isinstance(changes, list) or any(k not in ('arquivo', 'capa') for k in changes):
            raise ValueError()
    except (ValueError, TypeError):
        raise HTTPException(400, 'Rascunho inválido')
    async with _lock:
        ROOT.mkdir(parents=True, exist_ok=True)
        old = read_draft() or {}
        media = dict(old.get('media', {}))
        created = []
        try:
            for key, upload in (('arquivo', arquivo), ('capa', capa)):
                if key not in changes:
                    continue
                media[key] = None
                if upload is None:
                    continue
                token = uuid.uuid4().hex
                path = ROOT / token
                created.append(path)
                size = 0
                with path.open('wb') as out:
                    while chunk := await upload.read(1024 * 1024):
                        size += len(chunk)
                        if size > MAX_BYTES:
                            raise HTTPException(413, 'Limite local do rascunho: 512 MiB por arquivo')
                        out.write(chunk)
                media[key] = {'token': token, 'name': Path(upload.filename or key).name,
                              'type': upload.content_type or 'application/octet-stream'}
            draft = {'state': data, 'media': media}
            temp = ROOT / 'draft.tmp'
            temp.write_text(json.dumps(draft, ensure_ascii=False), encoding='utf-8')
            temp.replace(ROOT / 'draft.json')
        except Exception:
            for path in created:
                path.unlink(missing_ok=True)
            raise
        keep = {v['token'] for v in media.values() if v}
        for v in old.get('media', {}).values():
            if v and v['token'] not in keep:
                (ROOT / v['token']).unlink(missing_ok=True)
        return {'ok': True}
