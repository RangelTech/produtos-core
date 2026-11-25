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
