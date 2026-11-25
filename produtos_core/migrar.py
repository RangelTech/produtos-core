"""Copia os dados de um produto do SQLite para o schema dele no banco `produtos` (spec 57, §8.5) e confere.

Uso: python -m produtos_core.migrar <arquivo.db> <dsn> <schema> <modulo_db_do_produto> [--sem-testes]
  - cria as tabelas pelo próprio ESQUEMA do produto (traduzido),
  - copia tabela por tabela (colunas em comum), com os ids originais,
  - acerta as sequências de id,
  - confere a contagem de linhas de cada tabela (sai com erro se alguma não bater),
  - --sem-testes: NÃO copia usuários de teste (e-mails de QA automatizado), conforme a limpeza da spec (§6.8.5).
O SQLite original não é alterado (abre em modo somente leitura).
"""
from __future__ import annotations

import importlib
import re
import sqlite3
import sys

from .banco_pg import Banco

TESTE = re.compile(r'(^rtqa|@fldubai\.com$|@example\.(com|invalid)$|^qa\.|^teste)', re.I)


def migrar(sqlite_path: str, dsn: str, schema: str, esquema: str, sem_testes: bool = False) -> dict:
    origem = sqlite3.connect(f'file:{sqlite_path}?mode=ro', uri=True)
    origem.row_factory = sqlite3.Row
    destino = Banco(dsn, schema, esquema)
    tabelas = [r[0] for r in origem.execute("select name from sqlite_master where type='table' and name not like 'sqlite_%'")]
    ignorados = set()
    if sem_testes and 'usuarios' in tabelas:
        ignorados = {r['id'] for r in origem.execute('select id, email from usuarios') if TESTE.search(r['email'] or '')}
    rel = {}
    for t in tabelas:
        cols_pg = destino._cols(t)
        if not cols_pg:                                   # tabela criada em tempo de execucao pelo produto: usa o DDL do proprio SQLite
            ddl = origem.execute("select sql from sqlite_master where type='table' and name=?", (t,)).fetchone()[0]
            destino.con.execute(ddl.replace('CREATE TABLE ', 'CREATE TABLE IF NOT EXISTS ', 1) if 'IF NOT EXISTS' not in ddl.upper() else ddl)
            for (idx,) in origem.execute("select sql from sqlite_master where type='index' and tbl_name=? and sql is not null", (t,)):
                destino.con.execute(idx.replace('CREATE INDEX ', 'CREATE INDEX IF NOT EXISTS ', 1).replace('CREATE UNIQUE INDEX ', 'CREATE UNIQUE INDEX IF NOT EXISTS ', 1))
            cols_pg = destino._cols(t)
        cols = [c[1] for c in origem.execute(f'pragma table_info("{t}")') if c[1] in cols_pg]
        linhas = origem.execute(f'select {", ".join(chr(34) + c + chr(34) for c in cols)} from "{t}"').fetchall()
        col_usuario = 'usuario' if 'usuario' in cols else ('usuario_id' if 'usuario_id' in cols else None)
        if ignorados:
            if t == 'usuarios':
                linhas = [l for l in linhas if l['id'] not in ignorados]
            elif col_usuario:
                linhas = [l for l in linhas if l[col_usuario] not in ignorados]
        with destino._pg.cursor() as c:
            c.execute(f'set search_path to "{schema}"')
            c.execute(f'delete from "{t}"')
            if linhas:
                ph = ', '.join(['%s'] * len(cols))
                lista = ', '.join(f'"{x}"' for x in cols)
                c.executemany(f'insert into "{t}" ({lista}) overriding system value values ({ph})', [tuple(l) for l in linhas])
            if 'id' in cols:
                c.execute("select pg_get_serial_sequence(%s, 'id') as seq", (f'"{schema}"."{t}"',))
                seq = c.fetchone()['seq']
                if seq:                                       # so colunas id de identidade (numericas) tem sequencia
                    c.execute(f'select setval(%s, greatest(coalesce((select max(id) from "{t}"), 0), 1))', (seq,))
            c.execute(f'select count(*) as n from "{t}"')
            n_pg = c.fetchone()['n']
        rel[t] = ('ok' if n_pg == len(linhas) else 'DIFERENTE', len(linhas), n_pg)
    destino.fechar()
    return rel


if __name__ == '__main__':
    arq, dsn, schema, modulo = sys.argv[1:5]
    esquema = importlib.import_module(modulo).ESQUEMA
    r = migrar(arq, dsn, schema, esquema, sem_testes='--sem-testes' in sys.argv)
    ruins = {t: v for t, v in r.items() if v[0] not in ('ok',)}
    for t, (st, a, b) in r.items():
        print(f'{t:28s} {st:12s} sqlite={a} postgres={b}')
    sys.exit(1 if any(v[0] == 'DIFERENTE' for v in r.values()) else 0)
