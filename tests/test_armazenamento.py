"""Armazenamento (spec 57 §8.6): pasta local sempre; MinIO de verdade quando S3_ENDPOINT existe (bucket produtos-teste)."""
import os
import secrets
import urllib.request
from pathlib import Path

import pytest

from produtos_core.armazenamento import Armazenamento


def _modos(tmp_path):
    yield 'local', Armazenamento(endpoint='', bucket='t', pasta_local=str(tmp_path))
    if os.environ.get('S3_ENDPOINT'):
        yield 'minio', Armazenamento()


@pytest.fixture(params=['local', 'minio'])
def arm(request, tmp_path):
    modos = dict(_modos(tmp_path))
    if request.param not in modos:
        pytest.skip('sem S3_ENDPOINT')
    a = modos[request.param]
    pref = f'_teste/{secrets.token_hex(4)}'
    yield a, pref
    a.apagar_prefixo(pref)


def test_enviar_baixar_listar_apagar(arm, tmp_path):
    a, pref = arm
    assert a.enviar(f'{pref}/7/uploads/oi.txt', 'olá') == f'{pref}/7/uploads/oi.txt'
    assert a.baixar(f'{pref}/7/uploads/oi.txt') == 'olá'.encode()
    arq = tmp_path / 'video.bin'; arq.write_bytes(os.urandom(300_000))
    a.enviar(f'{pref}/7/resultados/video.bin', arq)
    assert a.tamanho(f'{pref}/7/resultados/video.bin') == 300_000
    assert a.baixar_para(f'{pref}/7/resultados/video.bin', tmp_path / 'volta.bin').read_bytes() == arq.read_bytes()
    assert a.listar(f'{pref}/7/') == [f'{pref}/7/resultados/video.bin', f'{pref}/7/uploads/oi.txt']
    assert a.existe(f'{pref}/7/uploads/oi.txt') and not a.existe(f'{pref}/7/nao.txt')
    a.apagar(f'{pref}/7/uploads/oi.txt')
    assert not a.existe(f'{pref}/7/uploads/oi.txt')
    assert a.apagar_prefixo(f'{pref}/7') == 1 and a.listar(f'{pref}/') == []


def test_chave_invalida(arm):
    a, _ = arm
    for ruim in ('', '../fora.txt', 'a/../../b'):
        with pytest.raises(ValueError):
            a.enviar(ruim, b'x')


def test_link_local_e_none(tmp_path):
    a = Armazenamento(endpoint='', bucket='t', pasta_local=str(tmp_path))
    a.enviar('x.txt', b'1')
    assert a.link('x.txt') is None


@pytest.mark.skipif(not os.environ.get('S3_PUBLICO_TESTE'), reason='só com internet e S3_PUBLICO_TESTE')
def test_link_publico_baixa_pela_internet():
    a = Armazenamento(publico=os.environ['S3_PUBLICO_TESTE'])
    k = f'_teste/{secrets.token_hex(4)}/link.txt'
    a.enviar(k, 'pelo link')
    try:
        url = a.link(k, 120)
        assert url.startswith(os.environ['S3_PUBLICO_TESTE'])
        with urllib.request.urlopen(url, timeout=20) as r:
            assert r.read() == 'pelo link'.encode()
    finally:
        a.apagar(k)


def test_rota_de_saude(tmp_path):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from produtos_core import armazenamento
    app = FastAPI()
    armazenamento.registrar(app, Armazenamento(endpoint='', bucket='t', pasta_local=str(tmp_path)))
    j = TestClient(app).get('/health/arquivos').json()
    assert j == {'ok': True, 'modo': 'local', 'bucket': 't'} and app.state.arquivos.listar('_saude/') == []


@pytest.mark.skipif(not os.environ.get('S3_ENDPOINT'), reason='precisa do MinIO')
def test_espelho_disco_vira_cache(tmp_path):
    from produtos_core.armazenamento import Espelho
    raiz = tmp_path / 'dados'
    pref = f'_teste/{secrets.token_hex(4)}'
    e = Espelho(Armazenamento(), raiz)
    f = raiz / pref / 'imagens' / 'a.png'; f.parent.mkdir(parents=True); f.write_bytes(b'png')
    assert e.guardar(f) == f'{pref}/imagens/a.png'
    f.unlink()                                             # contêiner novo: disco vazio
    assert e.garantir(f).read_bytes() == b'png'
    g = raiz / pref / 'planos' / 'p1' / 'x.png'; g.parent.mkdir(parents=True); g.write_bytes(b'1'); e.guardar(g)
    import shutil as sh; sh.rmtree(raiz / pref / 'planos')
    assert (e.garantir_pasta(raiz / pref / 'planos' / 'p1') / 'x.png').read_bytes() == b'1'
    e.apagar_pasta(raiz / pref / 'planos'); e.apagar(f)
    assert e.arm.listar(pref + '/') == [] and not f.exists()


def test_espelho_sem_minio_e_noop(tmp_path):
    from produtos_core.armazenamento import Espelho
    e = Espelho(Armazenamento(endpoint='', bucket='t', pasta_local=str(tmp_path / 'arm')), tmp_path / 'dados')
    f = tmp_path / 'dados' / 'x.txt'; f.parent.mkdir(parents=True); f.write_text('1')
    assert e.guardar(f) is None and e.garantir(f) == f
    e.apagar(f)
    assert not f.exists()
