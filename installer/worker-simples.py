"""
worker-simples.py - Worker autocontido de sincronizacao
Marcelo Auto Pecas - versao simplificada para Task Scheduler

Faz duas coisas:
1. Processa firebird_sync_queue: aplica mudancas do EPP no Firebird
2. Delta sync: atualiza tabela 'pecas' no Supabase com produtos do Firebird

Nao depende de venv, FastAPI ou outros modulos. So precisa de:
- Python 3.x (qualquer versao recente)
- fdb (driver Firebird)
"""

import fdb
import fdb.fbcore as _fbcore
_fbcore.b2u = lambda st, cs: st.decode('cp1252', errors='replace') if isinstance(st, bytes) else st

import json, urllib.request, datetime, time, os, sys

# ─── Configuracao ────────────────────────────────────────────────────────────

SUPABASE_URL = "https://avxqyrkaddvtdogjsrtm.supabase.co"
SUPABASE_KEY = "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJpc3MiOiJzdXBhYmFzZSIsInJlZiI6ImF2eHF5cmthZGR2dGRvZ2pzcnRtIiwicm9sZSI6InNlcnZpY2Vfcm9sZSIsImlhdCI6MTc3ODcyMjI5NSwiZXhwIjoyMDk0Mjk4Mjk1fQ.Ahl4kmuANviO3Dvxw37dcGXL14225FTkmwAzoOl7_Po"

# Tenta varios caminhos comuns do fbclient.dll
FBDLL_CANDIDATES = [
    r"C:\Program Files\Firebird\Firebird_3_0\fbclient.dll",
    r"C:\Program Files (x86)\Firebird\Firebird_3_0\fbclient.dll",
    r"C:\Program Files (x86)\Firebird\Firebird_2_5\bin\fbclient.dll",
    r"C:\Program Files\Firebird\Firebird_2_5\bin\fbclient.dll",
]

FB_HOST     = "127.0.0.1"
FB_PORT     = 3050
FB_DATABASE = r"C:\Enfoque\ERP\Data\erp.fdb"
FB_USER     = "SYSDBA"
FB_PASS     = "masterkey"

EMPRESA_ID = "1c668e79-80a9-4d44-af1b-8b2ecce623aa"
WORKER_ID  = "WinSyncSvc-1"
HOSTNAME   = os.environ.get("COMPUTERNAME", "desconhecido")
VERSAO     = "WorkerSimples-v1.0"

_api_loaded = False
_fbdll_usado = None

# ─── Logs ────────────────────────────────────────────────────────────────────

def log(msg):
    ts = datetime.datetime.now().strftime('%H:%M:%S')
    line = f"[{ts}] {msg}"
    print(line, flush=True)
    try:
        log_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "logs", "worker.log")
        log_path = os.path.normpath(log_path)
        os.makedirs(os.path.dirname(log_path), exist_ok=True)
        with open(log_path, "a", encoding="utf-8") as f:
            f.write(f"[{datetime.datetime.now().isoformat()}] {msg}\n")
    except Exception:
        pass

# ─── Supabase helpers ────────────────────────────────────────────────────────

def supa(method, path, data=None):
    body = json.dumps(data).encode() if data else None
    req = urllib.request.Request(f"{SUPABASE_URL}{path}", data=body, method=method)
    req.add_header("apikey", SUPABASE_KEY)
    req.add_header("Authorization", f"Bearer {SUPABASE_KEY}")
    req.add_header("Content-Type", "application/json")
    req.add_header("Prefer", "resolution=merge-duplicates,return=minimal")
    with urllib.request.urlopen(req, timeout=30) as r:
        try: return json.loads(r.read())
        except: return {}

# ─── Heartbeat ───────────────────────────────────────────────────────────────

def heartbeat(cycle_count, ultimo_erro=None, produtos_fb=None):
    agora = datetime.datetime.now(datetime.timezone.utc).isoformat()
    try:
        payload = {
            "worker_id": WORKER_ID,
            "empresa_id": EMPRESA_ID,
            "last_beat": agora,
            "cycle_count": cycle_count,
            "versao_worker": VERSAO,
            "hostname": HOSTNAME,
            "updated_at": agora,
        }
        if produtos_fb is not None:
            payload["produtos_fb"] = produtos_fb
        if ultimo_erro:
            payload["ultimo_erro"] = ultimo_erro[:500]
        supa("POST", "/rest/v1/sync_heartbeat", payload)
    except Exception as e:
        log(f"  Erro heartbeat: {e}")

# ─── Firebird helpers ────────────────────────────────────────────────────────

def fb_connect():
    global _api_loaded, _fbdll_usado
    if not _api_loaded:
        ultimo_erro = None
        for dll in FBDLL_CANDIDATES:
            if os.path.exists(dll):
                try:
                    fdb.load_api(dll)
                    _fbdll_usado = dll
                    _api_loaded = True
                    log(f"  Firebird DLL: {dll}")
                    break
                except Exception as e:
                    ultimo_erro = str(e)
                    continue
        if not _api_loaded:
            raise Exception(f"Nenhuma DLL Firebird funcionou. Ultimo erro: {ultimo_erro}")
    return fdb.connect(host=FB_HOST, port=FB_PORT, database=FB_DATABASE,
                       user=FB_USER, password=FB_PASS, charset='NONE')

def _s(v):
    if v is None: return ""
    if isinstance(v, bytes): return v.decode('cp1252', errors='replace').strip()
    return str(v).strip()

def _f(v):
    try: return float(v or 0)
    except: return 0.0

def _parse_memo(memo):
    if not memo: return "", ""
    if "-------CONVERSAO-----------" in memo:
        parts = memo.split("-------CONVERSAO-----------", 1)
        return parts[0].replace("-------APLICACAO-----------", "").strip(), parts[1].strip()
    return memo.strip(), ""

# ─── DIRECAO 1: EPP -> Firebird (fila) ───────────────────────────────────────

def processar_fila():
    """Le firebird_sync_queue e aplica as alteracoes no Firebird."""
    req = urllib.request.Request(
        f"{SUPABASE_URL}/rest/v1/firebird_sync_queue?status=eq.pending&order=created_at&limit=20"
    )
    req.add_header("apikey", SUPABASE_KEY)
    req.add_header("Authorization", f"Bearer {SUPABASE_KEY}")
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            items = json.loads(r.read())
    except Exception as e:
        log(f"  Erro ao buscar fila: {e}")
        return 0

    if not items:
        return 0

    try:
        con = fb_connect()
    except Exception as e:
        log(f"  Firebird offline para fila: {e}")
        return 0

    cur = con.cursor()
    ok = 0

    for item in items:
        codigo = item["codigo_produto"]
        p = item.get("payload", {})
        if not isinstance(p, dict):
            p = {}
        agora = datetime.datetime.now()
        try:
            campos_p, vals_p = [], []
            campos_e, vals_e = [], []

            if p.get("descricao"):
                campos_p.append("PRO_NOME = ?")
                vals_p.append(p["descricao"][:120].encode("cp1252","replace"))
            if p.get("codigo_proprio") is not None:
                campos_p.append("PRO_CODPROPRIO = ?")
                vals_p.append(str(p["codigo_proprio"])[:60].encode("cp1252","replace"))
            if p.get("localizacao") is not None:
                campos_p.append("PRO_LOCALIZACAO = ?")
                vals_p.append(str(p["localizacao"])[:50].encode("cp1252","replace"))
            if p.get("aplicacao") is not None or p.get("conversao") is not None:
                ap = p.get("aplicacao", "") or ""
                co = p.get("conversao", "") or ""
                partes = []
                if ap: partes += ["-------APLICACAO-----------", ap.strip()]
                if co: partes += ["-------CONVERSAO-----------", co.strip()]
                memo = "\n".join(partes).encode("cp1252","replace")
                campos_p.append("PRO_MEMO = ?"); vals_p.append(memo)
            if p.get("preco_venda") is not None:
                campos_e.append("EST_VENDA = ?"); vals_e.append(float(p["preco_venda"]))
            if p.get("margem") is not None:
                campos_e.append("EST_MARGEM = ?"); vals_e.append(float(p["margem"]))
            if p.get("perc_comissao") is not None:
                campos_e.append("EST_PERCCOMISSAO = ?"); vals_e.append(float(p["perc_comissao"]))
            if p.get("perc_imposto") is not None:
                campos_e.append("EST_PERCIMPOSTO = ?"); vals_e.append(float(p["perc_imposto"]))
            if p.get("perc_fixo") is not None:
                campos_e.append("EST_PERCFIXO = ?"); vals_e.append(float(p["perc_fixo"]))
            if p.get("perc_outros") is not None:
                campos_e.append("EST_PERCOUTROS = ?"); vals_e.append(float(p["perc_outros"]))

            if campos_p:
                campos_p.append("PRO_DATAALTERACAO = ?"); vals_p.append(agora); vals_p.append(codigo)
                cur.execute(f"UPDATE PRODUTO SET {','.join(campos_p)} WHERE PRO_CODIGO = ?", vals_p)
            if campos_e:
                campos_e.append("EST_DATAALTERACAO = ?"); vals_e.append(agora); vals_e.append(codigo)
                cur.execute(f"UPDATE ESTOQUE SET {','.join(campos_e)} WHERE EST_PRODUTO = ?", vals_e)

            con.commit()
            ts = datetime.datetime.now(datetime.timezone.utc).isoformat()
            supa("PATCH", f"/rest/v1/firebird_sync_queue?id=eq.{item['id']}",
                 {"status": "done", "processed_at": ts})
            ok += 1
            log(f"  Fila: produto {codigo} -> Firebird OK")
        except Exception as e:
            con.rollback()
            ts = datetime.datetime.now(datetime.timezone.utc).isoformat()
            supa("PATCH", f"/rest/v1/firebird_sync_queue?id=eq.{item['id']}",
                 {"status": "error", "erro": str(e)[:500], "processed_at": ts})
            log(f"  Fila: produto {codigo} ERRO: {e}")

    cur.close()
    con.close()
    return ok

# ─── DIRECAO 2: Firebird -> EPP (delta) ──────────────────────────────────────

def delta_firebird_para_epp(desde=None):
    """Pega produtos alterados no Firebird e atualiza tabela 'pecas' no Supabase."""
    try:
        con = fb_connect()
    except Exception as e:
        log(f"  Firebird offline para delta: {e}")
        return 0, None, None

    cur = con.cursor()

    filtro = ""
    params = []
    if desde:
        filtro = """AND (
            p.PRO_DATAALTERACAO >= ?
            OR p.PRO_CODIGO IN (SELECT DISTINCT MOV_PRODUTO FROM MOVESTOQUE WHERE MOV_DATA >= ? AND MOV_ISEXCLUIDO = 0)
            OR p.PRO_CODIGO IN (SELECT DISTINCT EST_PRODUTO FROM ESTOQUE WHERE EST_DATAALTERACAO >= ?)
        )"""
        params = [desde, desde, desde]

    cur.execute("SELECT COUNT(*) FROM PRODUTO WHERE PRO_ISATIVO = 1 AND PRO_DATAEXCLUSAO IS NULL")
    total_ativos = cur.fetchone()[0] or 0

    cur.execute("SELECT GRU_CODIGO, GRU_NOME FROM GRUPO")
    grupos = {r[0]: _s(r[1]) for r in cur.fetchall()}
    cur.execute("SELECT SEC_CODIGO, SEC_NOME FROM SECAO")
    secoes = {r[0]: _s(r[1]) for r in cur.fetchall()}
    cur.execute("SELECT MAR_CODIGO, MAR_NOME FROM MARCA")
    marcas = {r[0]: _s(r[1]) for r in cur.fetchall()}

    cur.execute(f"""
        SELECT
            p.PRO_CODIGO, p.PRO_NOME, p.PRO_CODPROPRIO, p.PRO_CODBARRA,
            p.PRO_LOCALIZACAO, p.PRO_MARCA, p.PRO_GRUPO, p.PRO_SECAO,
            COALESCE(e.EST_QTDE, 0), COALESCE(e.EST_MINIMO, 0),
            COALESCE(e.EST_VENDA, 0), COALESCE(e.EST_CUSTO, 0),
            COALESCE(e.EST_CUSTOMEDIO, 0), COALESCE(e.EST_MARGEM, 0),
            COALESCE(e.EST_PERCFIXO, 0), COALESCE(e.EST_PERCIMPOSTO, 0),
            COALESCE(e.EST_PERCCOMISSAO, 0), COALESCE(e.EST_PERCOUTROS, 0),
            CAST(SUBSTRING(p.PRO_MEMO FROM 1 FOR 8000) AS VARCHAR(8000)),
            p.PRO_NCM, p.PRO_CODBARRA2, p.PRO_DATAALTERACAO
        FROM PRODUTO p
        LEFT JOIN ESTOQUE e ON e.EST_PRODUTO = p.PRO_CODIGO
        WHERE p.PRO_ISATIVO = 1
          AND p.PRO_ISMERCADORIA = 1
          AND p.PRO_DATAEXCLUSAO IS NULL
          {filtro}
        ORDER BY p.PRO_CODIGO
    """, params)

    agora = datetime.datetime.now(datetime.timezone.utc).isoformat()
    rows = cur.fetchall()
    con.close()

    if not rows:
        return 0, desde, total_ativos

    ultima_alteracao = desde
    ok = 0
    for row in rows:
        memo_raw = _s(row[18])
        aplicacao, conversao = _parse_memo(memo_raw)
        marc = marcas.get(row[5], "")
        grp  = grupos.get(row[6], "")
        sub  = secoes.get(row[7], "")
        alt  = row[21]
        if alt and (ultima_alteracao is None or alt > ultima_alteracao):
            ultima_alteracao = alt

        codigo_erp = str(row[0])
        rec = {
            "descricao":           _s(row[1]).replace(" (BRIDGE)", "").strip(),
            "codigo_fabricante":   _s(row[2]),
            "ean":                 _s(row[3]),
            "localizacao":         _s(row[4]),
            "marca":               marc,
            "categoria":           grp,
            "subcategoria":        sub,
            "estoque":             int(_f(row[8])),
            "estoque_minimo":      int(_f(row[9])),
            "preco_comercializacao": _f(row[10]),
            "preco_custo":         _f(row[11]),
            "markup":              _f(row[13]),
            "aplicacoes":          aplicacao,
            "conversoes":          conversao,
            "ncm":                 _s(row[19]),
            "sincronizado_erp":    True,
            "updated_at":          agora,
        }
        try:
            supa("PATCH", f"/rest/v1/pecas?codigo_erp=eq.{codigo_erp}", rec)
            ok += 1
        except Exception as e:
            log(f"  Erro PATCH pecas {codigo_erp}: {e}")

    return ok, ultima_alteracao, total_ativos

# ─── Loop principal ──────────────────────────────────────────────────────────

def main():
    log(f"=== {VERSAO} iniciado ===")
    log(f"  Hostname: {HOSTNAME}")
    log(f"  Worker ID: {WORKER_ID}")

    # Heartbeat inicial
    heartbeat(0)

    ultima_sync = datetime.datetime.now() - datetime.timedelta(minutes=5)
    cycle_count = 0

    while True:
        cycle_count += 1
        ultimo_erro = None
        produtos_fb = None
        try:
            # Direcao 1: EPP -> Firebird (fila)
            n_fila = processar_fila()
            if n_fila:
                log(f"  Fila: {n_fila} item(ns) processados")

            # Direcao 2: Firebird -> EPP (delta) - so a cada 5 ciclos (5 min)
            if cycle_count % 5 == 1:
                n_delta, nova_ultima, total = delta_firebird_para_epp(desde=ultima_sync)
                if n_delta:
                    log(f"  Delta: {n_delta} produto(s) sincronizados Firebird->EPP")
                if nova_ultima:
                    ultima_sync = nova_ultima
                produtos_fb = total

        except Exception as e:
            ultimo_erro = str(e)
            log(f"Erro no ciclo {cycle_count}: {e}")

        # Heartbeat a cada ciclo
        try:
            heartbeat(cycle_count, ultimo_erro=ultimo_erro, produtos_fb=produtos_fb)
        except Exception:
            pass

        time.sleep(60)

if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        log("Encerrado pelo usuario")
    except Exception as e:
        log(f"FATAL: {e}")
        # Re-raise para Task Scheduler detectar como falha e reiniciar
        raise
