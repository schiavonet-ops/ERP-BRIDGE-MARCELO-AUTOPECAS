"""
core/sync_worker.py — Sincronização Enfoque ↔ banco local
Compatível com Linux (VPS) e Windows (PC local)
"""

import fdb
import fdb.fbcore as _fbcore
_fbcore.b2u = lambda st, cs: st.decode('cp1252', errors='replace') if isinstance(st, bytes) else st

import os
import sys
from datetime import date, datetime
from core.local_db import upsert_produtos, pendentes, marcar_enviado, marcar_erro, log

if sys.platform == "win32":
    FBCLIENT = os.getenv("FB_DLL", r"C:\Program Files\Firebird\Firebird_3_0\fbclient.dll")
else:
    FBCLIENT = os.getenv("FB_DLL", "/usr/lib/x86_64-linux-gnu/libfbclient.so.2")

HOST     = os.getenv("FB_HOST",     "168.205.222.164")
PORT     = int(os.getenv("FB_PORT", 3050))
DATABASE = os.getenv("FB_DATABASE", r"C:\Enfoque\ERP\Data\erp.fdb")
USER     = os.getenv("FB_USER",     "SYSDBA")
PASSWORD = os.getenv("FB_PASSWORD", "masterkey")

_api_loaded = False

def _conectar():
    global _api_loaded
    if not _api_loaded:
        fdb.load_api(FBCLIENT)
        _api_loaded = True
    return fdb.connect(
        host=HOST, port=PORT, database=DATABASE,
        user=USER, password=PASSWORD,
        charset='NONE'
    )

def enfoque_online() -> bool:
    try:
        con = _conectar()
        con.close()
        return True
    except Exception:
        return False

def puxar_enfoque(delta_desde=None, codigo=None) -> int:
    print(f"[{datetime.now().strftime('%H:%M:%S')}] Conectando ao Enfoque...")
    try:
        con = _conectar()
    except Exception as e:
        print(f"  ✗ Enfoque offline: {e}")
        log("erro", str(e))
        return 0

    cur = con.cursor()

    filtro = ""
    params = []
    if codigo is not None:
        filtro = "AND p.PRO_CODIGO = ?"
        params.append(codigo)
    elif delta_desde:
        filtro = "AND p.PRO_DATAALTERACAO >= ?"
        params.append(delta_desde)

    cur.execute(f"""
        SELECT
            p.PRO_CODIGO,
            p.PRO_NOME,
            p.PRO_CODPROPRIO,
            p.PRO_CODBARRA,
            p.PRO_LOCALIZACAO,
            COALESCE(e.EST_QTDE, 0) AS ESTOQUE,
            COALESCE(e.EST_CUSTO, 0) AS CUSTO_UNI,
            p.PRO_MARCA,
            p.PRO_GRUPO,
            p.PRO_DATAALTERACAO,
            CAST(SUBSTRING(p.PRO_MEMO FROM 1 FOR 8000) AS VARCHAR(8000)) AS PRO_MEMO,
            p.PRO_NCM,
            p.PRO_CODBARRA2,
            p.PRO_SECAO
        FROM PRODUTO p
        LEFT JOIN ESTOQUE e ON e.EST_PRODUTO = p.PRO_CODIGO
        WHERE p.PRO_ISATIVO = 1
          AND p.PRO_ISMERCADORIA = 1
          AND p.PRO_DATAEXCLUSAO IS NULL
          {filtro}
        ORDER BY p.PRO_NOME
    """, params)

    produtos = []
    for row in cur:
        produtos.append({
            "codigo":         row[0],
            "nome":           row[1] or "",
            "cod_fabricante": row[2] or "",
            "cod_barras":     row[3] or "",
            "localizacao":    row[4] or "",
            "estoque":        float(row[5] or 0),
            "estoque_min":    0.0,
            "marca":          str(row[7] or ""),
            "grupo":          str(row[8] or ""),
            "memo":           str(row[10] or ""),
            "ncm":            str(row[11] or ""),
            "cod_barras2":    str(row[12] or ""),
            "subgrupo":       str(row[13] or ""),
        })
    con.close()

    if produtos:
        upsert_produtos(produtos)
        msg = f"Puxados {len(produtos)} produtos {'(delta)' if delta_desde else '(completo)'}"
        print(f"  ✓ {msg}")
        log("sync_ok", msg)
    else:
        print("  — Nenhuma alteração")

    return len(produtos)

def enviar_fila() -> dict:
    itens = pendentes()
    if not itens:
        return {"enviados": 0, "erros": 0}

    print(f"[{datetime.now().strftime('%H:%M:%S')}] Enviando {len(itens)} item(ns)...")
    try:
        con = _conectar()
    except Exception as e:
        log("erro", f"Fila mantida: {e}")
        return {"enviados": 0, "erros": len(itens)}

    cur = con.cursor()
    enviados = erros = 0

    for item in itens:
        try:
            _aplicar_firebird(cur, item)
            con.commit()
            marcar_enviado(item["id"])
            enviados += 1
        except Exception as e:
            con.rollback()
            marcar_erro(item["id"], str(e))
            erros += 1
            print(f"  ✗ produto {item['codigo_produto']}: {e}")

    con.close()
    log("sync_ok" if erros == 0 else "erro", f"Fila: {enviados} enviados, {erros} erros")
    return {"enviados": enviados, "erros": erros}

def _get_estoque_atual(cur, codigo):
    """Retorna estoque atual via PRODUTOINVENTARIO (fonte de verdade do Enfoque)
    e custos/precos do MOVESTOQUE mais recente."""
    cur.execute("""
        SELECT FIRST 1
            MOV_CUSTO, MOV_CUSTOMEDIO, MOV_CUSTOPROPRIO,
            MOV_PRECO, MOV_PRECOMINIMO
        FROM MOVESTOQUE
        WHERE MOV_PRODUTO = ? AND MOV_ISEXCLUIDO = 0
        ORDER BY MOV_CODIGO DESC
    """, (codigo,))
    row = cur.fetchone()
    if row:
        custo       = float(row[0] or 0)
        custo_medio = float(row[1] or 0)
        custo_prop  = float(row[2] or 0)
        preco       = float(row[3] or 0)
        preco_min   = float(row[4] or 0)
    else:
        custo = custo_medio = custo_prop = preco = preco_min = 0.0

    cur.execute("""
        SELECT COALESCE(SUM(PRO_QTDE), 0)
        FROM PRODUTOINVENTARIO
        WHERE PRO_PRODUTO = ?
    """, (codigo,))
    estoque = float(cur.fetchone()[0] or 0)

    return estoque, custo, custo_medio, custo_prop, preco, preco_min

def _aplicar_firebird(cur, item):
    codigo = item["codigo_produto"]
    op     = item["operacao"]

    estoque_atual, custo, custo_medio, custo_prop, preco, preco_min = _get_estoque_atual(cur, codigo)

    if op == "ajustar":
        qtde_nova = float(item.get("quantidade_nova", item.get("quantidade", 0)))
        qtde_ajuste = qtde_nova - estoque_atual
    elif op == "entrada":
        qtde_ajuste = float(item["quantidade"])
    elif op == "baixar":
        qtde_ajuste = -float(item["quantidade"])
    elif op == "atualizar_memo":
        memo = item.get("referencia", "")
        cur.execute(
            "UPDATE PRODUTO SET PRO_MEMO = ?, PRO_DATAALTERACAO = CURRENT_TIMESTAMP WHERE PRO_CODIGO = ?",
            (memo, codigo)
        )
        return
    else:
        qtde_ajuste = float(item.get("quantidade", 0))
    if qtde_ajuste == 0:
        return

    hoje = date.today()

    # 1. Cria NOTAENTRADA
    cur.execute("SELECT GEN_ID(NOTAENTRADA, 1) FROM RDB" + chr(36) + "DATABASE")
    not_codigo = cur.fetchone()[0]

    descricao = f"Ajuste via bridge OS  data: {hoje.strftime('%d/%m/%Y')}  hora: {datetime.now().strftime('%H:%M:%S')}"
    cur.execute("""
        INSERT INTO NOTAENTRADA (
            NOT_CODIGO, NOT_FICHA, NOT_TIPO, NOT_COMPROVANTE,
            NOT_DATA, NOT_DATAEMISSAO, NOT_DATASAIDA,
            NOT_DESCRICAO, NOT_ISATIVO, NOT_DATAALTERACAO
        ) VALUES (?, 1, 2, 'SN', ?, ?, ?, ?, 1, ?)
    """, (not_codigo, hoje, hoje, hoje, descricao, datetime.now()))

    # 2. Cria MOVESTOQUE
    cur.execute("SELECT GEN_ID(MOVESTOQUE, 1) FROM RDB" + chr(36) + "DATABASE")
    mov_codigo = cur.fetchone()[0]

    cur.execute("""
        INSERT INTO MOVESTOQUE (
            MOV_CODIGO, MOV_NOTAENTRADA, MOV_PRODUTO, MOV_UNIDADE,
            MOV_DATA, MOV_HORAEMISSAO, MOV_QTDE, MOV_QTDEMOV,
            MOV_VALORUNI, MOV_VALORTOTAL, MOV_VALORTOTALLIQUIDO,
            MOV_ESTOQUE, MOV_CUSTO, MOV_CUSTOMEDIO, MOV_CUSTOPROPRIO,
            MOV_PRECO, MOV_PRECOMINIMO,
            MOV_ISESTOQUE, MOV_ISCUSTO, MOV_ISEXCLUIDO,
            MOV_ISTOTAL, MOV_ISLIVRE, MOV_ISDEVOLVIDO, MOV_QTDEDEV,
            MOV_MEUSIMPLES, MOV_ISLIVRERECALCULAR,
            MOV_CODPRODUTO, MOV_ORIGEM
        ) VALUES (
            ?, ?, ?, 1,
            ?, ?, ?, ?,
            0, 0, 0,
            ?, ?, ?, ?,
            ?, ?,
            1, 0, 0,
            0, 0, 0, 0,
            0, 0,
            ?, '0'
        )
    """, (
        mov_codigo, not_codigo, codigo,
        hoje, datetime.now(), qtde_ajuste, qtde_ajuste,
        estoque_atual, custo, custo_medio, custo_prop,
        preco, preco_min,
        codigo
    ))

    # 3. Atualiza PRO_DATAALTERACAO no PRODUTO
    cur.execute(
        "UPDATE PRODUTO SET PRO_DATAALTERACAO = CURRENT_TIMESTAMP WHERE PRO_CODIGO = ?",
        (codigo,)
    )

if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else "delta"
    if cmd == "completo":
        puxar_enfoque()
    elif cmd == "delta":
        from core.local_db import status_sync
        s = status_sync()
        from datetime import datetime, timedelta
        ultima = s.get("ultima_sync")
        if ultima:
            if isinstance(ultima, str):
                ultima = datetime.fromisoformat(ultima)
            # Enfoque usa BRT (UTC-3); VPS usa UTC: ajusta fuso
            ultima = ultima - timedelta(hours=3)
        puxar_enfoque(delta_desde=ultima)
        enviar_fila()
    elif cmd == "fila":
        enviar_fila()
    elif cmd == "status":
        print("Enfoque online:", enfoque_online())
        from core.local_db import total_produtos, status_sync
        print("Produtos:", total_produtos())
        print("Sync:", status_sync())
