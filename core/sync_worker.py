"""
core/sync_worker.py — Sincronização Enfoque ↔ banco local
Estoque fica em PRODUTOINVENTARIO.PRO_QTDE (não em PRODUTO)
"""

import fdb
import os
from datetime import datetime
from core.local_db import upsert_produtos, pendentes, marcar_enviado, marcar_erro, log

FBCLIENT  = os.getenv("FB_DLL",      r"C:\Program Files\Firebird\Firebird_3_0\fbclient.dll")
HOST      = os.getenv("FB_HOST",     "168.205.222.164")
PORT      = int(os.getenv("FB_PORT", 3050))
DATABASE  = os.getenv("FB_DATABASE", r"C:\Enfoque\ERP\Data\erp.fdb")
USER      = os.getenv("FB_USER",     "SYSDBA")
PASSWORD  = os.getenv("FB_PASSWORD", "masterkey")

_api_loaded = False

def _conectar():
    global _api_loaded
    if not _api_loaded:
        fdb.load_api(FBCLIENT)
        _api_loaded = True
    return fdb.connect(host=HOST, port=PORT, database=DATABASE, user=USER, password=PASSWORD)

def enfoque_online() -> bool:
    try:
        con = _conectar()
        con.close()
        return True
    except Exception:
        return False

def puxar_enfoque(delta_desde=None) -> int:
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
    if delta_desde:
        filtro = "AND p.PRO_DATAALTERACAO >= ?"
        params.append(delta_desde)

    # Estoque vem de PRODUTOINVENTARIO via JOIN
    cur.execute(f"""
        SELECT
            p.PRO_CODIGO,
            p.PRO_NOME,
            p.PRO_CODPROPRIO,
            p.PRO_CODBARRA,
            p.PRO_LOCALIZACAO,
            COALESCE(inv.PRO_QTDE, 0)      AS ESTOQUE,
            COALESCE(inv.PRO_CUSTOUNI, 0)  AS CUSTO_UNI,
            p.PRO_MARCA,
            p.PRO_GRUPO,
            
            p.PRO_DATAALTERACAO
        FROM PRODUTO p
        LEFT JOIN (
            SELECT PRO_PRODUTO, SUM(PRO_QTDE) AS PRO_QTDE, MAX(PRO_CUSTOUNI) AS PRO_CUSTOUNI
            FROM PRODUTOINVENTARIO
            GROUP BY PRO_PRODUTO
        ) inv ON inv.PRO_PRODUTO = p.PRO_CODIGO
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
            "memo": "",
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

def _aplicar_firebird(cur, item):
    """Atualiza PRODUTOINVENTARIO no Enfoque."""
    codigo = item["codigo_produto"]
    qtde   = item["quantidade"]
    op     = item["operacao"]

    cur.execute(
        "SELECT PRO_CODIGO, PRO_QTDE FROM PRODUTOINVENTARIO WHERE PRO_PRODUTO = ? ORDER BY PRO_CODIGO DESC",
        (codigo,)
    )
    row = cur.fetchone()

    if row:
        inv_codigo = row[0]
        atual = float(row[1] or 0)
        if op == "baixar":
            novo = atual - qtde
        elif op == "entrada":
            novo = atual + qtde
        else:
            novo = qtde  # ajustar

        cur.execute(
            "UPDATE PRODUTOINVENTARIO SET PRO_QTDE = ? WHERE PRO_CODIGO = ?",
            (novo, inv_codigo)
        )
    else:
        # Cria registro de inventário se não existir
        if op in ("entrada", "ajustar"):
            cur.execute(
                "INSERT INTO PRODUTOINVENTARIO (PRO_PRODUTO, PRO_QTDE) VALUES (?, ?)",
                (codigo, qtde)
            )

    cur.execute(
        "UPDATE PRODUTO SET PRO_DATAALTERACAO = CURRENT_TIMESTAMP WHERE PRO_CODIGO = ?",
        (codigo,)
    )

if __name__ == "__main__":
    import sys
    cmd = sys.argv[1] if len(sys.argv) > 1 else "delta"
    if cmd == "completo":
        puxar_enfoque()
    elif cmd == "delta":
        from core.local_db import status_sync
        s = status_sync()
        puxar_enfoque(delta_desde=s.get("ultima_sync"))
        enviar_fila()
    elif cmd == "fila":
        enviar_fila()
    elif cmd == "status":
        print("Enfoque online:", enfoque_online())
        from core.local_db import total_produtos, status_sync
        print("Produtos:", total_produtos())
        print("Sync:", status_sync())
