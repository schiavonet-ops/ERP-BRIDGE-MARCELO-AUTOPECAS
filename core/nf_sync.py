"""core/nf_sync.py - Sync NFs Enfoque (Firebird) -> Supabase"""

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

CNPJS_BLOQUEADOS = {"91229252000223"}


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


def _headers():
    return {
        "apikey": SUPABASE_KEY,
        "Authorization": f"Bearer {SUPABASE_KEY}",
        "Content-Type": "application/json",
    }


def _get_cnpj_ficha(cur, ficha_codigo) -> str:
    if not ficha_codigo:
        return ""
    for campo in ("FIC_CGCCPF", "FIC_CNPJ", "FIC_CPF"):
        try:
            cur.execute(f"SELECT {campo} FROM FICHA WHERE FIC_CODIGO = ?", [ficha_codigo])
            row = cur.fetchone()
            if row:
                cnpj = _s(row[0]).replace(".", "").replace("/", "").replace("-", "").strip()
                return cnpj
        except Exception:
            continue
    return ""


def _batch_peca_ids(codigos: list) -> dict:
    if not codigos:
        return {}
    unicos = list(set(c for c in codigos if c))
    if not unicos:
        return {}
    try:
        filtro = "in.(" + ",".join(unicos) + ")"
        r = httpx.get(
            f"{SUPABASE_URL}/rest/v1/pecas",
            headers=_headers(),
            params={"codigo_erp": filtro, "empresa_id": f"eq.{EMPRESA_ID}", "select": "id,codigo_erp"},
            timeout=15,
        )
        if r.status_code == 200:
            return {row["codigo_erp"]: row["id"] for row in r.json() if row.get("codigo_erp")}
    except Exception as e:
        print(f"  Erro batch pecas: {e}")
    return {}


def _nf_ja_existe(numero_nf: str) -> bool:
    try:
        r = httpx.get(
            f"{SUPABASE_URL}/rest/v1/pecas_entradas",
            headers=_headers(),
            params={
                "numero_nf":     f"eq.{numero_nf}",
                "empresa_id":    f"eq.{EMPRESA_ID}",
                "origem_compra": "eq.enfoque",
                "select":        "id",
            },
            timeout=10,
        )
        return r.status_code == 200 and len(r.json()) > 0
    except Exception:
        return False


def _inserir_entrada(payload: dict) -> str | None:
    try:
        r = httpx.post(
            f"{SUPABASE_URL}/rest/v1/pecas_entradas",
            json=payload,
            headers={**_headers(), "Prefer": "return=representation"},
            timeout=15,
        )
        if r.status_code in (200, 201) and r.json():
            return r.json()[0]["id"]
        print(f"  Erro inserir NF: {r.status_code} {r.text[:200]}")
    except Exception as e:
        print(f"  Erro inserir NF: {e}")
    return None


def _inserir_itens(itens: list) -> None:
    if not itens:
        return
    try:
        r = httpx.post(
            f"{SUPABASE_URL}/rest/v1/pecas_entradas_itens",
            json=itens,
            headers={**_headers(), "Prefer": "return=minimal"},
            timeout=30,
        )
        if r.status_code not in (200, 201):
            print(f"  Erro inserir itens: {r.status_code} {r.text[:200]}")
    except Exception as e:
        print(f"  Erro inserir itens: {e}")


def puxar_nfs_enfoque(delta_desde=None) -> int:
    print(f"[{datetime.now().strftime('%H:%M:%S')}] Sincronizando NFs de entrada...")

    if not SUPABASE_URL or not SUPABASE_KEY or not EMPRESA_ID:
        print("  Faltam variaveis: SUPABASE_URL, SUPABASE_SERVICE_ROLE_KEY, EMPRESA_ID")
        return 0
    if httpx is None:
        print("  httpx nao instalado")
        return 0

    if delta_desde is None:
        data_corte = (datetime.now() - timedelta(days=1)).date()
    elif isinstance(delta_desde, datetime):
        data_corte = delta_desde.date()
    else:
        data_corte = delta_desde

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
                ne.NOT_VALORTOTAL,
                ne.NOT_SERIE,
                ne.NOT_FICHA
            FROM NOTAENTRADA ne
            WHERE ne.NOT_DATA >= ?
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
            comprovante  = _s(nota[1])
            data_emissao = str(nota[2])[:10] if nota[2] else None
            valor_total  = _f(nota[3])
            serie        = _s(nota[4]) or None
            ficha_codigo = nota[5]

            # Usa numero NF-e se tiver, senao usa codigo interno prefixado
            if comprovante and comprovante not in ("SN", "NE", ""):
                numero_nf = comprovante
            else:
                numero_nf = f"ENF-{not_codigo}"

            cnpj = _get_cnpj_ficha(cur, ficha_codigo)
            if cnpj in CNPJS_BLOQUEADOS:
                continue

            if _nf_ja_existe(numero_nf):
                continue

            cur.execute("""
                SELECT m.MOV_PRODUTO, m.MOV_QTDE, m.MOV_VALORUNI, p.PRO_NOME
                FROM MOVESTOQUE m
                LEFT JOIN PRODUTO p ON p.PRO_CODIGO = m.MOV_PRODUTO
                WHERE m.MOV_NOTAENTRADA = ?
                  AND m.MOV_ISEXCLUIDO = 0
                  AND m.MOV_QTDE > 0
            """, [not_codigo])

            itens_raw = cur.fetchall()
            if not itens_raw:
                continue

            codigos = [_s(i[0]) for i in itens_raw]
            mapa_pecas = _batch_peca_ids(codigos)

            entrada_id = _inserir_entrada({
                "empresa_id":       EMPRESA_ID,
                "numero_nf":        numero_nf,
                "serie":            serie,
                "data_emissao":     data_emissao,
                "valor_total_nota": valor_total,
                "origem_compra":    "enfoque",
                "status":           "ATIVA",
                "tipo":             "NF",
            })

            if not entrada_id:
                continue

            itens = []
            for item in itens_raw:
                codigo_erp = _s(item[0])
                itens.append({
                    "entrada_id":           entrada_id,
                    "peca_id":              mapa_pecas.get(codigo_erp),
                    "codigo_item":          codigo_erp,
                    "descricao_item":       _s(item[3]),
                    "quantidade":           _f(item[1]),
                    "valor_unitario":       _f(item[2]),
                    "custo_final_unitario": _f(item[2]),
                })

            _inserir_itens(itens)
            sincronizadas += 1
            print(f"    NF {numero_nf}: {len(itens)} item(s)")

        msg = f"{sincronizadas} NF(s) importadas"
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


if __name__ == "__main__":
    puxar_nfs_enfoque()
