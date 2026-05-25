"""core/nf_sync.py - Sincronizacao de Notas Fiscais de Entrada Enfoque -> Supabase

NAO altera o fluxo de produtos (sync_worker.py / local_db.py).
Busca NFs de compra do Firebird e faz upsert nas tabelas
pecas_entradas e pecas_entradas_itens do Supabase.
"""

import os
from datetime import datetime, timedelta, date
from core.sync_worker import _conectar
from core.local_db import log

try:
    import httpx
except ImportError:
    httpx = None

SUPABASE_URL = os.getenv("SUPABASE_URL", "")
SUPABASE_KEY = os.getenv("SUPABASE_SERVICE_ROLE_KEY", "")


def _s(v) -> str:
    if v is None:
        return ""
    if isinstance(v, bytes):
        return v.decode("cp1252", errors="replace").strip()
    return str(v).strip()


def _f(v) -> float:
    try:
        return float(v or 0)
    except Exception:
        return 0.0


def _to_date(v):
    """Converte qualquer valor para date, aceitando date, datetime ou string."""
    if v is None:
        return (datetime.now() - timedelta(days=90)).date()
    if isinstance(v, datetime):
        return v.date()
    if isinstance(v, date):
        return v
    try:
        return datetime.fromisoformat(str(v)[:10]).date()
    except Exception:
        return (datetime.now() - timedelta(days=90)).date()


def _supabase_upsert(tabela: str, registros: list, on_conflict: str) -> bool:
    if not registros or not SUPABASE_URL or not SUPABASE_KEY:
        return False
    if httpx is None:
        print("  httpx nao instalado - pip install httpx")
        return False
    url = f"{SUPABASE_URL}/rest/v1/{tabela}"
    headers = {
        "apikey": SUPABASE_KEY,
        "Authorization": f"Bearer {SUPABASE_KEY}",
        "Content-Type": "application/json",
        "Prefer": "resolution=merge-duplicates,return=minimal",
    }
    try:
        r = httpx.post(url, json=registros, headers=headers,
                       params={"on_conflict": on_conflict}, timeout=30)
        if r.status_code not in (200, 201):
            print(f"  Supabase {tabela}: {r.status_code} {r.text[:200]}")
            return False
        return True
    except Exception as e:
        print(f"  Supabase erro: {e}")
        return False


def puxar_nfs_enfoque(delta_desde=None) -> int:
    """
    Busca NFs de compra para revenda do Firebird e sincroniza com Supabase.
    delta_desde: datetime | date | None. Se None, busca ultimos 90 dias.
    Nao toca em sync_worker.py nem local_db.py.
    """
    print(f"[{datetime.now().strftime('%H:%M:%S')}] Sincronizando NFs de entrada...")

    # Sempre converte para date puro — Firebird nao aceita datetime com hora
    data_filtro = _to_date(delta_desde)
    print(f"  Buscando NFs a partir de {data_filtro}")

    try:
        con = _conectar()
    except Exception as e:
        print(f"  Enfoque offline: {e}")
        return 0

    cur = con.cursor()
    try:
        cur.execute("""
            SELECT
                ne.NOT_CODIGO,
                ne.NOT_COMPROVANTE,
                ne.NOT_DATA,
                ne.NOT_DATAEMISSAO,
                ne.NOT_VALORTOTAL,
                ne.NOT_FICHA,
                ne.NOT_TIPO,
                ne.NOT_DESCRICAO,
                f.FIC_NOME,
                f.FIC_CNPJ
            FROM NOTAENTRADA ne
            LEFT JOIN FICHA f ON f.FIC_CODIGO = ne.NOT_FICHA
            WHERE ne.NOT_ISATIVO = 1
              AND ne.NOT_TIPO IN (1, 2)
              AND ne.NOT_DATA >= ?
              AND ne.NOT_COMPROVANTE IS NOT NULL
              AND TRIM(ne.NOT_COMPROVANTE) != ''
              AND TRIM(ne.NOT_COMPROVANTE) != 'SN'
            ORDER BY ne.NOT_DATA DESC
        """, [data_filtro])

        notas_raw = cur.fetchall()
        if not notas_raw:
            print("  Nenhuma NF nova")
            con.close()
            return 0

        print(f"  {len(notas_raw)} NF(s) encontradas")

        notas_supabase = []
        itens_supabase = []

        for nota in notas_raw:
            not_codigo  = nota[0]
            not_numero  = _s(nota[1])
            not_data    = str(nota[2]) if nota[2] else None
            not_emissao = str(nota[3]) if nota[3] else not_data
            not_total   = _f(nota[4])
            fic_nome    = _s(nota[8])
            fic_cnpj    = _s(nota[9])

            cur.execute("""
                SELECT
                    m.MOV_PRODUTO,
                    m.MOV_QTDE,
                    m.MOV_VALORUNI,
                    m.MOV_VALORTOTAL,
                    p.PRO_NOME
                FROM MOVESTOQUE m
                LEFT JOIN PRODUTO p ON p.PRO_CODIGO = m.MOV_PRODUTO
                WHERE m.MOV_NOTAENTRADA = ?
                  AND m.MOV_ISEXCLUIDO = 0
                  AND m.MOV_QTDE > 0
            """, [not_codigo])

            itens_raw = cur.fetchall()
            if not itens_raw:
                continue

            notas_supabase.append({
                "numero_nota":     not_numero,
                "data_emissao":    not_emissao,
                "data_entrada":    not_data,
                "fornecedor_nome": fic_nome,
                "fornecedor_cnpj": fic_cnpj,
                "valor_total":     not_total,
                "qtd_itens":       len(itens_raw),
            })

            for item in itens_raw:
                itens_supabase.append({
                    "nota_numero":    not_numero,
                    "codigo_enfoque": item[0],
                    "quantidade":     _f(item[1]),
                    "custo_unitario": _f(item[2]),
                    "custo_total":    _f(item[3]),
                    "descricao":      _s(item[4]),
                    "status":         "pendente",
                })

        if not notas_supabase:
            print("  Nenhuma NF com itens validos")
            con.close()
            return 0

        _supabase_upsert("pecas_entradas", notas_supabase, "numero_nota")

        for i in range(0, len(itens_supabase), 100):
            _supabase_upsert("pecas_entradas_itens",
                             itens_supabase[i:i+100],
                             "nota_numero,codigo_enfoque")

        msg = f"NFs: {len(notas_supabase)} notas, {len(itens_supabase)} itens"
        print(f"  OK {msg}")
        log("nf_sync_ok", msg)
        con.close()
        return len(notas_supabase)

    except Exception as e:
        print(f"  Erro NF sync: {e}")
        log("erro", f"nf_sync: {e}")
        try:
            con.close()
        except Exception:
            pass
        return 0
