"""Arquivos dos produtos no MinIO (spec 57 §8.6), um bucket por produto. Portável: trocar de MinIO (ou ir para outro S3) é
mudar S3_ENDPOINT / S3_PUBLICO / chaves no Infisical, sem mexer no produto.

Configuração (ambiente):
  S3_ENDPOINT   http://minio:9000 (rede interna do Docker)
  S3_PUBLICO    https://storage.rangeltech.net (endereço que o navegador alcança; usado só para assinar links)
  S3_BUCKET     bucket do produto
  S3_ACCESS_KEY / S3_SECRET_KEY   chave do produto (política só do bucket dele)
Sem S3_ENDPOINT, grava numa pasta local (desenvolvimento e testes): mesma interface.

Chaves no padrão  <usuario_id>/uploads/...,  <usuario_id>/resultados/...,  _temp/...  (§8.6 item 1).
"""
from __future__ import annotations

import io
import mimetypes
import os
import shutil
from datetime import timedelta
from pathlib import Path
from urllib.parse import urlparse


def _limpa(chave: str) -> str:
    chave = str(chave).replace('\\', '/').lstrip('/')
    if not chave or '..' in chave.split('/'):
        raise ValueError(f'chave inválida: {chave!r}')
    return chave


class Armazenamento:
    """enviar, baixar, listar, apagar e link temporário. Use `Armazenamento()` (lê o ambiente)."""

    def __init__(self, endpoint: str | None = None, bucket: str | None = None, chave: str | None = None,
                 segredo: str | None = None, publico: str | None = None, pasta_local: str | None = None):
        e = os.environ.get
        self.endpoint = endpoint if endpoint is not None else e('S3_ENDPOINT', '')
        self.bucket = bucket or e('S3_BUCKET', '')
        self.publico = publico if publico is not None else e('S3_PUBLICO', '')
        if self.endpoint:
            from minio import Minio
            u = urlparse(self.endpoint)
            ak, sk = chave or e('S3_ACCESS_KEY', ''), segredo or e('S3_SECRET_KEY', '')
            self._s3 = Minio(u.netloc, access_key=ak, secret_key=sk, secure=u.scheme == 'https', region='us-east-1')
            if self.publico:
                p = urlparse(self.publico)
                self._assinador = Minio(p.netloc, access_key=ak, secret_key=sk, secure=p.scheme == 'https', region='us-east-1')
            else:
                self._assinador = self._s3
            self.local = None
        else:
            self._s3 = None
            self.local = Path(pasta_local or e('ARMAZENAMENTO_LOCAL', '') or './dados_local/arquivos') / (self.bucket or 'local')
            self.local.mkdir(parents=True, exist_ok=True)

    @property
    def remoto(self) -> bool:
        return self._s3 is not None

    # ------------------------------------------------------------------ gravar
    def enviar(self, chave: str, dados, tipo: str | None = None) -> str:
        """dados: bytes, str (texto), arquivo aberto (binário) ou Path (arquivo). Devolve a chave."""
        chave = _limpa(chave)
        if isinstance(dados, Path):
            return self.enviar_arquivo(chave, dados, tipo)
        if isinstance(dados, str):
            dados = dados.encode()
        if isinstance(dados, (bytes, bytearray)):
            dados = io.BytesIO(dados)
        tipo = tipo or mimetypes.guess_type(chave)[0] or 'application/octet-stream'
        if self.remoto:
            self._s3.put_object(self.bucket, chave, dados, length=-1, part_size=16 * 1024 * 1024, content_type=tipo)
        else:
            destino = self.local / chave
            destino.parent.mkdir(parents=True, exist_ok=True)
            with open(destino, 'wb') as f:
                shutil.copyfileobj(dados, f)
        return chave

    def enviar_arquivo(self, chave: str, caminho, tipo: str | None = None) -> str:
        chave = _limpa(chave)
        tipo = tipo or mimetypes.guess_type(str(caminho))[0] or 'application/octet-stream'
        if self.remoto:
            self._s3.fput_object(self.bucket, chave, str(caminho), content_type=tipo)
        else:
            destino = self.local / chave
            destino.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(caminho, destino)
        return chave

    # ------------------------------------------------------------------ ler
    def baixar(self, chave: str) -> bytes:
        chave = _limpa(chave)
        if self.remoto:
            r = self._s3.get_object(self.bucket, chave)
            try:
                return r.read()
            finally:
                r.close(); r.release_conn()
        return (self.local / chave).read_bytes()

    def baixar_para(self, chave: str, caminho) -> Path:
        chave = _limpa(chave)
        Path(caminho).parent.mkdir(parents=True, exist_ok=True)
        if self.remoto:
            self._s3.fget_object(self.bucket, chave, str(caminho))
        else:
            shutil.copyfile(self.local / chave, caminho)
        return Path(caminho)

    def existe(self, chave: str) -> bool:
        chave = _limpa(chave)
        if self.remoto:
            from minio.error import S3Error
            try:
                self._s3.stat_object(self.bucket, chave)
                return True
            except S3Error as e:
                if e.code in ('NoSuchKey', 'NoSuchObject', 'NotFound'):
                    return False
                raise
        return (self.local / chave).is_file()

    def tamanho(self, chave: str) -> int:
        chave = _limpa(chave)
        if self.remoto:
            return self._s3.stat_object(self.bucket, chave).size
        return (self.local / chave).stat().st_size

    def listar(self, prefixo: str = '') -> list[str]:
        prefixo = prefixo.replace('\\', '/').lstrip('/')
        if self.remoto:
            return sorted(o.object_name for o in self._s3.list_objects(self.bucket, prefix=prefixo, recursive=True))
        base = self.local
        return sorted(str(p.relative_to(base)).replace('\\', '/') for p in base.rglob('*')
                      if p.is_file() and str(p.relative_to(base)).replace('\\', '/').startswith(prefixo))

    # ------------------------------------------------------------------ apagar
    def apagar(self, chave: str) -> None:
        chave = _limpa(chave)
        if self.remoto:
            self._s3.remove_object(self.bucket, chave)
        else:
            (self.local / chave).unlink(missing_ok=True)

    def apagar_prefixo(self, prefixo: str) -> int:
        """Apaga tudo sob o prefixo (ex.: '<usuario_id>/' ao apagar a conta, §10A.4). Devolve quantos apagou."""
        prefixo = _limpa(prefixo.rstrip('/')) + '/'
        chaves = self.listar(prefixo)
        for c in chaves:
            self.apagar(c)
        return len(chaves)

    # ------------------------------------------------------------------ link
    def link(self, chave: str, segundos: int = 3600, nome: str | None = None) -> str | None:
        """Link temporário para o navegador baixar direto do MinIO (sem passar pelo app). Local: None (o app serve)."""
        chave = _limpa(chave)
        if not self.remoto:
            return None
        extra = {'response-content-disposition': f'attachment; filename="{nome}"'} if nome else None
        return self._assinador.presigned_get_object(self.bucket, chave, expires=timedelta(seconds=segundos), response_headers=extra)


def registrar(app, arm: 'Armazenamento | None' = None) -> 'Armazenamento':
    """Liga o armazenamento no app (app.state.arquivos) e a rota /health/arquivos (grava, lê e apaga um objeto no
    bucket do produto; serve ao Uptime Kuma e ao QA). Devolve o Armazenamento."""
    import secrets as _s
    import time as _t
    from fastapi.responses import JSONResponse
    arm = arm or Armazenamento()
    app.state.arquivos = arm

    @app.get('/health/arquivos')
    def saude_arquivos():
        k = f'_saude/{int(_t.time())}-{_s.token_hex(3)}.txt'
        try:
            arm.enviar(k, 'ok')
            ok = arm.baixar(k) == b'ok'
            arm.apagar(k)
            return {'ok': ok, 'modo': 'minio' if arm.remoto else 'local', 'bucket': arm.bucket}
        except Exception as e:  # noqa: BLE001
            return JSONResponse({'ok': False, 'modo': 'minio' if arm.remoto else 'local', 'erro': type(e).__name__}, status_code=503)
    return arm


class Espelho:
    """Arquivos do produto com o MinIO como fonte oficial e o disco só como cache (spec 57 §8.6).

    O código do produto continua trabalhando com caminhos em `raiz` (ffmpeg, Pillow, FileResponse), e chama:
      guardar(caminho)     depois de gravar: sobe para o bucket com a chave = caminho relativo à raiz
      garantir(caminho)    antes de ler/servir: se o arquivo não está no disco (contêiner novo), baixa do bucket
      apagar(caminho) / apagar_pasta(pasta)   apaga no disco e no bucket
    Sem MinIO (desenvolvimento e testes) tudo vira no-op e o disco é a única cópia, como antes.
    """

    def __init__(self, arm: Armazenamento, raiz):
        self.arm = arm
        self.raiz = Path(raiz).resolve()
        # nos testes (S3_PREFIXO_TESTE=1) cada raiz ganha um prefixo próprio no bucket compartilhado de testes
        self.prefixo = ''
        if os.environ.get('S3_PREFIXO_TESTE'):
            import hashlib
            # o valor da variável entra no hash: cada rodada de testes passa um id próprio (o tmp_path do pytest se repete entre contêineres)
            self.prefixo = '_teste/' + hashlib.sha1((str(self.raiz) + os.environ['S3_PREFIXO_TESTE']).encode()).hexdigest()[:12] + '/'


    @property
    def ativo(self) -> bool:
        return self.arm.remoto

    def chave(self, caminho) -> str:
        p = Path(caminho)
        p = p.resolve() if p.is_absolute() or p.exists() else (self.raiz / p).resolve()
        return _limpa(self.prefixo + p.relative_to(self.raiz).as_posix())

    def guardar(self, caminho) -> str | None:
        if not self.ativo:
            return None
        return self.arm.enviar_arquivo(self.chave(caminho), caminho)

    def guardar_pasta(self, pasta) -> int:
        if not self.ativo:
            return 0
        n = 0
        for f in Path(pasta).rglob('*'):
            if f.is_file():
                self.guardar(f); n += 1
        return n

    def garantir(self, caminho) -> Path:
        p = Path(caminho)
        if p.is_file() or not self.ativo:
            return p
        k = self.chave(p)
        if self.arm.existe(k):
            self.arm.baixar_para(k, p)
        return p

    def garantir_pasta(self, pasta) -> Path:
        """Baixa a pasta inteira (ex.: plano com várias imagens) se faltar algo no disco."""
        pasta = Path(pasta)
        if self.ativo:
            for k in self.arm.listar(self.chave(pasta) + '/'):
                destino = self.raiz / k[len(self.prefixo):]
                if not destino.is_file():
                    self.arm.baixar_para(k, destino)
        return pasta

    def apagar(self, caminho) -> None:
        p = Path(caminho)
        if self.ativo:
            self.arm.apagar(self.chave(p))
        p.unlink(missing_ok=True)

    def apagar_pasta(self, pasta) -> None:
        pasta = Path(pasta)
        if self.ativo:
            self.arm.apagar_prefixo(self.chave(pasta))
        shutil.rmtree(pasta, ignore_errors=True)
