import asyncio
import pathlib
import sys
import unittest
from unittest.mock import AsyncMock, patch
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))
import redes

class Autorizacao(unittest.IsolatedAsyncioTestCase):
    async def executar(self, autorizadas=None, simular=False, redes_pedidas=None):
        contas = [{'id': r, 'platform': r, 'status': 'active', 'username': 'teste'}
                  for r in ('instagram', 'x')]
        with patch.object(redes, '_compor_para', new_callable=AsyncMock) as compor, \
             patch.object(redes, '_publicar_instagram', new_callable=AsyncMock, return_value={}) as publicar, \
             patch.object(redes, '_abrir_compositor', new_callable=AsyncMock, return_value={'modo':'preenchido'}) as abrir:
            rotacionar = AsyncMock(return_value={'ok': True})
            resultado = await redes.publicar_em_todas({}, contas,
                {'arquivo':'video.mp4', 'capa':'capa.png', 'destino':'reels', 'legenda':'teste'},
                redes_pedidas=redes_pedidas, espalhar_segundos=0,
                simular=simular, auto_publicar=autorizadas, rotacionar=rotacionar)
            return resultado, compor, publicar, abrir, rotacionar

    async def test_sem_autorizacao(self):
        r, compor, publicar, abrir, _ = await self.executar()
        self.assertEqual(r['pendentes'], 1)
        self.assertEqual(r['publicadas'], 0)
        publicar.assert_not_awaited()
        self.assertEqual(compor.await_count, 1)
        self.assertFalse(abrir.await_args.args[2])

    async def test_autorizacao_por_rede(self):
        r, _, publicar, abrir, _ = await self.executar(['instagram'])
        self.assertEqual(r['publicadas'], 1)
        publicar.assert_awaited_once()
        self.assertFalse(abrir.await_args.args[2])
        r, _, publicar, abrir, _ = await self.executar(['x'])
        publicar.assert_not_awaited()
        self.assertTrue(abrir.await_args.args[2])

    async def test_simulacao_nao_executa(self):
        r, compor, publicar, abrir, rotacionar = await self.executar(['instagram', 'x'], True)
        for mock in (compor, publicar, abrir, rotacionar):
            mock.assert_not_awaited()
        self.assertTrue(all(d['desfecho']=='simulado' for d in r['plano']))

    async def test_so_instagram_pendente_nao_rotaciona(self):
        r, compor, publicar, abrir, rotacionar = await self.executar(redes_pedidas=['instagram'])
        for mock in (compor, publicar, abrir, rotacionar):
            mock.assert_not_awaited()
        self.assertEqual(r['pendentes'], 1)

if __name__ == '__main__':
    unittest.main()
