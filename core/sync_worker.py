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
from core.local_db import upsert_produtos, upsert_produtos_supabase_se_mudou, pendentes, marcar_enviado, marcar_erro, log

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


def _parse_memo_local(memo_raw: str):
    """Separa PRO_MEMO em (aplicacao, conversao)."""
    if not memo_raw or not memo_raw.strip():
        return "", ""
    if "-------CONVERSAO-----------" in memo_raw:
        parts = memo_raw.split("-------CONVERSAO-----------", 1)
        ap = parts[0].replace("-------APLICACAO-----------", "").strip()
        co = parts[1].strip()
        return ap, co
    import re
    match = re.search(r"-{4,}", memo_raw)
    if match:
        before = memo_raw[:match.start()].strip()
        after  = memo_raw[match.end():].strip()
        if before and after:
            return before, after
    lines = memo_raw.strip().splitlines()
    ap, co = [], []
    modo = "ap"
    for line in lines:
        s = line.strip()
        if not s: continue
        if modo == "ap" and "  " in line and any(c.isalpha() for c in s) and any(c.isdigit() for c in s):
            modo = "co"
        (co if modo == "co" else ap).append(s)
    return "\n".join(ap), "\n".join(co)

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
        filtro = """AND (
            p.PRO_DATAALTERACAO >= ?
            OR p.PRO_CODIGO IN (
                SELECT DISTINCT MOV_PRODUTO FROM MOVESTOQUE
                WHERE MOV_DATA >= ? AND MOV_ISEXCLUIDO = 0
            )
            OR p.PRO_CODIGO IN (
                SELECT DISTINCT EST_PRODUTO FROM ESTOQUE
                WHERE EST_DATAALTERACAO >= ?
            )
        )"""
        params.append(delta_desde)
        params.append(delta_desde)
        params.append(delta_desde)

    def _s(v):
        if v is None: return ""
        if isinstance(v, bytes): return v.decode("cp1252", errors="replace").strip()
        return str(v).strip()

    cur.execute("SELECT GRU_CODIGO, GRU_NOME FROM GRUPO")
    grupos = {r[0]: _s(r[1]) for r in cur.fetchall()}
    cur.execute("SELECT SEC_CODIGO, SEC_NOME FROM SECAO")
    secoes = {r[0]: _s(r[1]) for r in cur.fetchall()}
    cur.execute("SELECT MAR_CODIGO, MAR_NOME FROM MARCA")
    marcas = {r[0]: _s(r[1]) for r in cur.fetchall()}

    cur.execute(f"""
        SELECT
            p.PRO_CODIGO,
            p.PRO_NOME,
            p.PRO_CODPROPRIO,
            p.PRO_CODBARRA,
            p.PRO_LOCALIZACAO,
            COALESCE(e.EST_QTDE, 0)        AS ESTOQUE,
            COALESCE(e.EST_CUSTO, 0)       AS CUSTO,
            p.PRO_MARCA,
            p.PRO_GRUPO,
            p.PRO_DATAALTERACAO,
            CAST(SUBSTRING(p.PRO_MEMO FROM 1 FOR 8000) AS VARCHAR(8000)) AS PRO_MEMO,
            p.PRO_NCM,
            p.PRO_CODBARRA2,
            p.PRO_SECAO,
            COALESCE(e.EST_VENDA, 0)       AS PRECO_VENDA,
            COALESCE(e.EST_MARGEM, 0)      AS MARGEM,
            COALESCE(e.EST_PERCFIXO, 0)    AS PERC_FIXO,
            COALESCE(e.EST_PERCIMPOSTO, 0) AS PERC_IMPOSTO,
            COALESCE(e.EST_PERCCOMISSAO,0) AS PERC_COMISSAO,
            COALESCE(e.EST_PERCOUTROS, 0)  AS PERC_OUTROS,
            COALESCE(e.EST_CUSTOMEDIO, 0)  AS CUSTO_MEDIO
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
        memo_raw     = str(row[10] or "")
        aplicacao, conversao = _parse_memo_local(memo_raw)
        marca_cod    = row[7]
        grupo_cod    = row[8]
        subgrupo_cod = row[13]
        produtos.append({
            "codigo":         row[0],
            "nome":           (row[1] or "").replace(" (BRIDGE)", "").strip(),
            "cod_fabricante": row[2] or "",
            "cod_barras":     row[3] or "",
            "localizacao":    row[4] or "",
            "estoque":        float(row[5] or 0),
            "estoque_min":    0.0,
            "marca":          str(marca_cod or ""),
            "grupo":          str(grupo_cod or ""),
            "memo":           memo_raw,
            "ncm":            str(row[11] or ""),
            "cod_barras2":    str(row[12] or ""),
            "subgrupo":       str(subgrupo_cod or ""),
            "aplicacao":      aplicacao,
            "conversao":      conversao,
            "preco_venda":    float(row[14] or 0),
            "margem":         float(row[15] or 0),
            "perc_fixo":      float(row[16] or 0),
            "perc_imposto":   float(row[17] or 0),
            "perc_comissao":  float(row[18] or 0),
            "perc_outros":    float(row[19] or 0),
            "custo":          float(row[6] or 0),
            "custo_medio":    float(row[20] or 0),
            "marca_nome":     marcas.get(marca_cod, ""),
            "grupo_nome":     grupos.get(grupo_cod, ""),
            "subgrupo_nome":  secoes.get(subgrupo_cod, ""),
        })
    con.close()

    if produtos:
        # Salva no SQLite local sempre
        upsert_produtos(produtos)
        # Envia ao Supabase APENAS se valores mudaram — evita loop de egress
        enviados_supa = upsert_produtos_supabase_se_mudou(produtos)
        msg = f"Puxados {len(produtos)} produtos {'(delta)' if delta_desde else '(completo)'}, {enviados_supa} enviados ao Supabase"
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
