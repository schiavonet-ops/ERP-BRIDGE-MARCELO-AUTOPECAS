@echo off
REM ============================================================
REM  corrigir_inicio_automatico.bat
REM  Corrige servico existente para start=auto + restart em falha
REM  Executar como Administrador
REM ============================================================

net session >nul 2>&1
if %errorlevel% neq 0 (
    echo ERRO: Execute como Administrador.
    pause
    exit /b 1
)

echo Corrigindo configuracao do WinSyncSvc...

sc config WinSyncSvc start= auto
sc failure WinSyncSvc reset= 86400 actions= restart/30000/restart/60000/restart/120000

echo.
echo Pronto! Configuracoes aplicadas:
echo  - Inicio: Automatico
echo  - Falha 1: reinicia em 30s
echo  - Falha 2: reinicia em 60s
echo  - Falha 3+: reinicia em 120s
echo.

REM Iniciar agora se estiver parado
sc query WinSyncSvc | findstr "STOPPED" >nul 2>&1
if %errorlevel% equ 0 (
    echo Servico estava parado, iniciando...
    sc start WinSyncSvc
)

sc query WinSyncSvc | findstr STATE
echo.
pause
