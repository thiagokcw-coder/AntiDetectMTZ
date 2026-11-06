import pathlib
import sys
import tempfile
import unittest
from unittest.mock import patch
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))
from fastapi import FastAPI
from fastapi.testclient import TestClient
import publication_draft as draft

class DraftTests(unittest.TestCase):
    def test_persist_replace_restore_and_reject_large_file(self):
        with tempfile.TemporaryDirectory() as folder, patch.object(draft, 'ROOT', pathlib.Path(folder)):
            app=FastAPI(); app.include_router(draft.router)
            with TestClient(app) as client:
                self.assertIsNone(client.get('/publicacao/rascunho').json()['draft'])
                r=client.post('/publicacao/rascunho', data={'state':'{"fields":{"st-legenda":"Olá"}}','changed':'["arquivo","capa"]'},
                    files={'arquivo':('video.mp4',b'video','video/mp4'), 'capa':('cover.jpg',b'cover','image/jpeg')})
                self.assertEqual(r.status_code,200)
                saved=client.get('/publicacao/rascunho').json()['draft']
                token=saved['media']['arquivo']['token']
                self.assertEqual(client.get('/publicacao/rascunho/arquivo/'+token).content,b'video')
                self.assertEqual(saved['state']['fields']['st-legenda'],'Olá')
                client.post('/publicacao/rascunho',data={'state':'{"fields":{"st-legenda":"Editada"}}'})
                self.assertEqual(draft.read_draft()['media'],saved['media'])
                with patch.object(draft,'MAX_BYTES',2):
                    r=client.post('/publicacao/rascunho',data={'state':'{}','changed':'["arquivo"]'},files={'arquivo':('large.mp4',b'large')})
                    self.assertEqual(r.status_code,413)
                self.assertEqual(draft.read_draft()['media'],saved['media'])
                client.post('/publicacao/rascunho',data={'state':'{}','changed':'["capa"]'})
                self.assertIsNone(draft.read_draft()['media']['capa'])
                self.assertFalse((pathlib.Path(folder)/saved['media']['capa']['token']).exists())
                self.assertEqual(client.get('/publicacao/rascunho/arquivo/not-a-token').status_code,404)
                self.assertEqual(client.post('/publicacao/rascunho',data={'state':'[]'}).status_code,400)

if __name__=='__main__': unittest.main()
