"""
core/scheduler.py — Scheduler ADAPTATIVO de sincronização

Estratégia híbrida: ajusta o intervalo de sync conforme atividade detectada.
Reduz drasticamente o egress quando não há mudanças.

Estados (ciclos):
  ATIVO  → 20s  (algo mudou no último ciclo)
  NORMAL → 2min (padrão)
  IDLE   → 5min (10 ciclos sem mudança)
  SLEEP  → 15min (30 ciclos sem mudança)

Acorda imediatamente se detectar:
  - Fila local com itens pendentes (OS baixou peça)
  - Hora comercial (8h-18h em dia útil) → nunca entra em SLEEP

CORREÇÃO: ciclos_sem_mudanca agora usa enviados_supabase (mudanças reais)
e não puxados (que sempre é > 0 mesmo sem mudança).
"""

import time
import sys
from datetime import datetime, timedelta
from core.sync_worker import puxar_enfoque, enviar_fila, enfoque_online
from core.local_db import pendentes, status_sync, log
from core.nf_sync import puxar_nfs_enfoque

# ─── Configuração dos estados ──────────────────────────────────

ESTADOS = {
    "ATIVO":  20,     # 20 segundos — mudança detectada
    "NORMAL": 120,    # 2 minutos   — padrão
    "IDLE":   300,    # 5 minutos   — sem mudança há algum tempo
    "SLEEP":  900,    # 15 minutos  — sem mudança há muito tempo
}

# Quantos ciclos sem mudança até trocar de estado
LIMIAR_IDLE  = 10   # 10 ciclos sem mudança → IDLE
LIMIAR_SLEEP = 30   # 30 ciclos sem mudança → SLEEP

# Horário comercial — nunca entra em SLEEP
HORA_COMERCIAL_INICIO = 8
HORA_COMERCIAL_FIM    = 18


def _eh_horario_comercial() -> bool:
    """Segunda a sexta, 8h-18h."""
    agora = datetime.now()
    if agora.weekday() >= 5:  # sábado=5, domingo=6
        return False
    return HORA_COMERCIAL_INICIO <= agora.hour < HORA_COMERCIAL_FIM


def _decidir_estado(ciclos_sem_mudanca: int, fila_pendente: int) -> str:
    """Decide o próximo estado do scheduler."""
    # Fila com itens → sempre ATIVO
    if fila_pendente > 0:
        return "ATIVO"

    # Mudou agora → ATIVO
    if ciclos_sem_mudanca == 0:
        return "ATIVO"

    # Horário comercial → no máximo IDLE
    if _eh_horario_comercial():
        if ciclos_sem_mudanca >= LIMIAR_IDLE:
            return "IDLE"
        return "NORMAL"

    # Fora do horário comercial → pode dormir
    if ciclos_sem_mudanca >= LIMIAR_SLEEP:
        return "SLEEP"
    if ciclos_sem_mudanca >= LIMIAR_IDLE:
        return "IDLE"
    return "NORMAL"


def rodar_loop():
    """Loop principal do scheduler adaptativo."""
    ciclos_sem_mudanca = 0
    estado_atual = "NORMAL"
    contador_nfs = 0  # NFs só a cada ~30 min

    print(f"[{datetime.now().strftime('%H:%M:%S')}] Scheduler adaptativo iniciado (v2 - fix I/O)")
    log("scheduler_start", "Scheduler adaptativo v2 iniciado - fix loop I/O")

    while True:
        try:
            ts_inicio = datetime.now()

            # 1. Verifica fila pendente (OS para enviar ao Enfoque)
            fila = len(pendentes())

            # 2. Decide estado
            novo_estado = _decidir_estado(ciclos_sem_mudanca, fila)
            if novo_estado != estado_atual:
                print(f"[{ts_inicio.strftime('%H:%M:%S')}] Estado: {estado_atual} → {novo_estado} (ciclos parados: {ciclos_sem_mudanca})")
                log("scheduler_estado", f"{estado_atual} → {novo_estado} (ciclos parados: {ciclos_sem_mudanca})")
                estado_atual = novo_estado

            # 3. Verifica se Enfoque está online
            if not enfoque_online():
                ciclos_sem_mudanca += 1
                intervalo = ESTADOS[estado_atual]
                print(f"[{ts_inicio.strftime('%H:%M:%S')}] Enfoque offline — aguardando {intervalo}s")
                time.sleep(intervalo)
                continue

            # 4. Sync delta de produtos
            s = status_sync()
            ultima = s.get("ultima_sync")
            if ultima:
                if isinstance(ultima, str):
                    ultima = datetime.fromisoformat(ultima)
                ultima = ultima - timedelta(hours=3)  # Ajuste BRT → UTC

            # puxar_enfoque retorna (puxados, enviados_supabase)
            resultado = puxar_enfoque(delta_desde=ultima)
            # Compatibilidade: se retornar só int (versão antiga), trata como (n, 0)
            if isinstance(resultado, tuple):
                puxados, enviados_supabase = resultado
            else:
                puxados, enviados_supabase = resultado, 0

            # 5. Envia fila (se houver)
            if fila > 0:
                enviar_fila()

            # 6. Sync de NFs a cada ~30 min
            contador_nfs += 1
            intervalo_nf = max(1, 1800 // ESTADOS[estado_atual])  # ~30 min em qualquer estado
            if contador_nfs >= intervalo_nf:
                try:
                    puxar_nfs_enfoque()
                except Exception as e:
                    log("erro", f"NFs sync: {e}")
                contador_nfs = 0

            # 7. CORREÇÃO CRÍTICA: usa enviados_supabase para detectar mudanças reais
            # puxados sempre > 0 (lê do Firebird sempre), mas enviados_supabase
            # só > 0 quando algo realmente mudou no hash
            if enviados_supabase > 0 or fila > 0:
                ciclos_sem_mudanca = 0
                print(f"[{ts_inicio.strftime('%H:%M:%S')}] {enviados_supabase} mudanças reais enviadas ao Supabase")
            else:
                ciclos_sem_mudanca += 1
                if ciclos_sem_mudanca % 5 == 0:
                    print(f"[{ts_inicio.strftime('%H:%M:%S')}] Sem mudanças há {ciclos_sem_mudanca} ciclos — estado: {estado_atual}")

            # 8. Aguarda intervalo do estado atual
            intervalo = ESTADOS[estado_atual]
            time.sleep(intervalo)

        except KeyboardInterrupt:
            print("\nScheduler encerrado pelo usuário.")
            log("scheduler_stop", "Encerrado manualmente")
            break

        except Exception as e:
            print(f"[ERRO] {e}")
            log("erro", f"Loop scheduler: {e}")
            time.sleep(60)


if __name__ == "__main__":
    rodar_loop()
