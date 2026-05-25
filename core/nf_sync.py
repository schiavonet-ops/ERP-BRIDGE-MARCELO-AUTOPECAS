"""core/nf_sync.py - Sync NFs Enfoque (Firebird) -> Supabase
Completamente separado do sync_worker.py / local_db.py.
Nao toca em nenhum outro arquivo.
"""

import os
from datetime import datetime, timedelta
from core.sync_worker import _conectar
from core.local_db import log

try:
    import httpx
except ImportError:
    httpx = None

SUPABASE_URL = os.getenv("SUPABASE_URL", "")
SUPABASE_KEY = os.getenv("SUPABASE_SERVICE_ROLE_KEY", "")
EMPRESA_ID   = os.getenv("EMPRESA_ID", "")


# ── helpers ──────────────────────────────────────────────────────────────────

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


def _base_headers():
    return {
        "apikey": SUPABASE_KEY,
        "Authorization": f"Bearer {SUPABASE_KEY}",
        "Content-Type": "application/json",
    }


# ── lookups no Supabase ───────────────────────────────────────────────────────

def _get_fornecedor_id(cnpj: str) -> str | None:
    """Busca fornecedor_id pelo CNPJ."""
    if not cnpj:
        return None
    try:
        r = httpx.get(
            f"{SUPABASE_URL}/rest/v1/fornecedores",
            headers=_base_headers(),
            params={"cnpj": f"eq.{cnpj}", "empresa_id": f"eq.{EMPRESA_ID}", "select": "id"},
            timeout=10,
        )
        if r.status_code == 200 and r.json():
            return r.json()[0]["id"]
    except Exception:
        pass
    return None


def _batch_peca_ids(codigos: list[str]) -> dict[str, str]:
    """Retorna {codigo_erp: peca_id} para todos os codigos de uma vez."""
    if not codigos:
        return {}
    try:
        codigos_unicos = list(set(c for c in codigos if c))
        # Supabase REST: in.(a,b,c)
        filtro = "in.(" + ",".join(codigos_unicos) + ")"
        r = httpx.get(
            f"{SUPABASE_URL}/rest/v1/pecas",
            headers=_base_headers(),
            params={"codigo_erp": filtro, "empresa_id": f"eq.{EMPRESA_ID}", "select": "id,codigo_erp"},
            timeout=15,
        )
        if r.status_code == 200:
            return {row["codigo_erp"]: row["id"] for row in r.json() if row.get("codigo_erp")}
    except Exception as e:
        print(f"  Erro batch peca_ids: {e}")
    return {}


def _nf_ja_existe(numero_nf: str) -> bool:
    """Evita duplicar NFs ja sincronizadas."""
    try:
        r = httpx.get(
            f"{SUPABASE_URL}/rest/v1/pecas_entradas",
            headers=_base_headers(),
            params={
                "numero_nf":  f"eq.{numero_nf}",
                "empresa_id": f"eq.{EMPRESA_ID}",
                "origem_compra": "eq.enfoque",
                "select": "id",
            },
            timeout=10,
        )
        return r.status_code == 200 and len(r.json()) > 0
    except Exception:
        return False


def _inserir_entrada(payload: dict) -> str | None:
    """Insere cabecalho da NF, retorna o id gerado."""
    try:
        r = httpx.post(
            f"{SUPABASE_URL}/rest/v1/pecas_entradas",
            json=payload,
            headers={**_base_headers(), "Prefer": "return=representation"},
            timeout=15,
        )
        if r.status_code in (200, 201) and r.json():
            return r.json()[0]["id"]
        print(f"  Erro cabecalho: {r.status_code} {r.text[:200]}")
    except Exception as e:
        print(f"  Erro inserir entrada: {e}")
    return None


def _inserir_itens(itens: list) -> None:
    if not itens:
        return
    try:
        r = httpx.post(
            f"{SUPABASE_URL}/rest/v1/pecas_entradas_itens",
            json=itens,
            headers={**_base_headers(), "Prefer": "return=minimal"},
            timeout=30,
        )
        if r.status_code not in (200, 201):
            print(f"  Erro itens: {r.status_code} {r.text[:200]}")
    except Exception as e:
        print(f"  Erro inserir itens: {e}")


# ── funcao principal ──────────────────────────────────────────────────────────

def puxar_nfs_enfoque(delta_desde=None) -> int:
    print(f"[{datetime.now().strftime('%H:%M:%S')}] Sincronizando NFs de entrada...")

    if not SUPABASE_URL or not SUPABASE_KEY or not EMPRESA_ID:
        print("  Faltam variaveis: SUPABASE_URL, SUPABASE_SERVICE_ROLE_KEY, EMPRESA_ID")
        return 0
    if httpx is None:
        print("  httpx nao instalado - pip install httpx")
        return 0

    # Garante que delta_desde e sempre date (sem hora) para o Firebird aceitar
    if delta_desde is None:
        data_corte = (datetime.now() - timedelta(days=90)).date()
    elif isinstance(delta_desde, datetime):
        data_corte = delta_desde.date()
    else:
        data_corte = delta_desde  # ja e date

    try:
        con = _conectar()
    except Exception as e:
        print(f"  Enfoque offline: {e}")
        return 0

    cur = con.cursor()
    sincronizadas = 0

    try:
        cur.execute("""
            SELECT
                ne.NOT_CODIGO,
                ne.NOT_COMPROVANTE,
                ne.NOT_DATAEMISSAO,
                ne.NOT_DATA,
                ne.NOT_VALORTOTAL,
                ne.NOT_SERIE,
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
        """, [data_corte])

        notas = cur.fetchall()
        if not notas:
            print("  Nenhuma NF encontrada")
            con.close()
            return 0

        print(f"  {len(notas)} NF(s) encontradas no Firebird")

        for nota in notas:
            not_codigo   = nota[0]
            numero_nf    = _s(nota[1])
            data_emissao = str(nota[2])[:10] if nota[2] else None
            valor_total  = _f(nota[4])
            serie        = _s(nota[5]) or None
            cnpj         = _s(nota[6])

            if not numero_nf:
                continue

            if _nf_ja_existe(numero_nf):
                continue

            fornecedor_id = _get_fornecedor_id(cnpj) if cnpj else None

            # Buscar itens do Firebird
            cur.execute("""
                SELECT
                    m.MOV_PRODUTO,
                    m.MOV_QTDE,
                    m.MOV_VALORUNI
                FROM MOVESTOQUE m
                WHERE m.MOV_NOTAENTRADA = ?
                  AND m.MOV_ISEXCLUIDO = 0
                  AND m.MOV_QTDE > 0
            """, [not_codigo])

            itens_raw = cur.fetchall()
            if not itens_raw:
                continue

            # Batch lookup de pecas pelo codigo_erp
            codigos = [_s(i[0]) for i in itens_raw]
            mapa_pecas = _batch_peca_ids(codigos)

            # So insere a NF se tiver pelo menos 1 item vinculado
            itens_ok = []
            for item in itens_raw:
                codigo_erp = _s(item[0])
                peca_id = mapa_pecas.get(codigo_erp)
                if not peca_id:
                    continue  # peca nao cadastrada no EPP, pula
                itens_ok.append({
                    "peca_id":              peca_id,
                    "quantidade":           _f(item[1]),
                    "valor_unitario":       _f(item[2]),
                    "custo_final_unitario": _f(item[2]),
                })

            if not itens_ok:
                continue

            entrada_id = _inserir_entrada({
                "empresa_id":       EMPRESA_ID,
                "numero_nf":        numero_nf,
                "serie":            serie,
                "data_emissao":     data_emissao,
                "fornecedor_id":    fornecedor_id,
                "valor_total_nota": valor_total,
                "origem_compra":    "enfoque",
                "status":           "ATIVA",
                "tipo":             "NF",
            })

            if not entrada_id:
                continue

            # Adiciona entrada_id em cada item agora que temos o UUID
            for it in itens_ok:
                it["entrada_id"] = entrada_id

            _inserir_itens(itens_ok)
            sincronizadas += 1
            print(f"    NF {numero_nf}: {len(itens_ok)} item(s)")

        msg = f"{sincronizadas} NF(s) sincronizadas"
        print(f"  OK {msg}")
        log("nf_sync_ok", msg)

    except Exception as e:
        print(f"  Erro NF sync: {e}")
        log("erro", f"nf_sync: {e}")
    finally:
        try:
            con.close()
        except Exception:
            pass

    return sincronizadas
