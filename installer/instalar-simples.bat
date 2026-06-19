@echo off
title Instalador WinSync Simples - Marcelo Auto Pecas
color 0A
setlocal enabledelayedexpansion

REM ============================================================
REM  Instalador autocontido - usa worker simples sem venv
REM ============================================================

net session >nul 2>&1
if errorlevel 1 (
    color 0C
    echo.
    echo  ERRO: Execute como Administrador.
    echo  Feche e clique com botao DIREITO no arquivo
    echo  e escolha "Executar como administrador".
    pause
    exit /b 1
)

set BASE=C:\MarceloWebEPP
set MONITOR=%BASE%\monitor
set LOGDIR=%BASE%\logs
set WORKER=%MONITOR%\worker-simples.py
set WRAPPER=%MONITOR%\run-worker-simples.bat

echo.
echo ============================================================
echo   INSTALADOR WORKER SIMPLES - Marcelo Auto Pecas
echo ============================================================
echo.

REM ============================================================
REM  1. Detectar Python
REM ============================================================
echo [1/8] Detectando Python...

set PY=
REM Tenta varios caminhos comuns
for %%P in (
    "%BASE%\venv\Scripts\python.exe"
    "C:\Python311\python.exe"
    "C:\Python310\python.exe"
    "C:\Python39\python.exe"
    "C:\Python38\python.exe"
    "C:\Python37\python.exe"
    "C:\Python32\python.exe"
    "C:\Python\python.exe"
) do (
    if exist %%P (
        if "!PY!"=="" set PY=%%~P
    )
)

REM Se ainda nao achou, procura no PATH
if "!PY!"=="" (
    for /f "delims=" %%i in ('where python 2^>nul') do (
        if "!PY!"=="" set PY=%%i
    )
)

if "!PY!"=="" (
    color 0C
    echo.
    echo  ERRO: Python nao encontrado.
    echo  Instale Python 3.x antes de continuar.
    pause
    exit /b 1
)
echo      OK: Python em !PY!

REM ============================================================
REM  2. Verificar / instalar fdb (driver Firebird)
REM ============================================================
echo.
echo [2/8] Verificando driver Firebird (fdb)...
"!PY!" -c "import fdb" >nul 2>&1
if errorlevel 1 (
    echo      Instalando fdb...
    "!PY!" -m pip install fdb --quiet
    if errorlevel 1 (
        color 0E
        echo      AVISO: nao foi possivel instalar fdb automaticamente.
        echo      Se o worker nao iniciar, instale manualmente.
    ) else (
        echo      OK: fdb instalado
    )
) else (
    echo      OK: fdb ja instalado
)

REM ============================================================
REM  3. Criar pastas
REM ============================================================
echo.
echo [3/8] Criando pastas...
if not exist "%MONITOR%" mkdir "%MONITOR%"
if not exist "%LOGDIR%" mkdir "%LOGDIR%"
echo      OK

REM ============================================================
REM  4. Baixar worker-simples.py do GitHub
REM ============================================================
echo.
echo [4/8] Baixando worker-simples.py...
powershell -NoProfile -Command "try { Invoke-WebRequest -Uri 'https://raw.githubusercontent.com/schiavonet-ops/ERP-BRIDGE-MARCELO-AUTOPECAS/main/installer/worker-simples.py' -OutFile '%WORKER%' -UseBasicParsing; exit 0 } catch { exit 1 }"
if errorlevel 1 (
    color 0C
    echo  ERRO: falhou ao baixar worker-simples.py
    pause
    exit /b 1
)
echo      OK: %WORKER%

REM ============================================================
REM  5. Remover servico/task antigos
REM ============================================================
echo.
echo [5/8] Limpando configuracoes antigas...
sc query WinSyncSvc >nul 2>&1
if not errorlevel 1 (
    sc stop WinSyncSvc >nul 2>&1
    timeout /t 3 /nobreak >nul
    sc delete WinSyncSvc >nul 2>&1
)
schtasks /Delete /TN "WinSyncWorker" /F >nul 2>&1

REM Mata processos python antigos do worker
wmic process where "name='python.exe' and commandline like '%%worker%%'" delete >nul 2>&1
wmic process where "name='pythonw.exe' and commandline like '%%worker%%'" delete >nul 2>&1
echo      OK

REM ============================================================
REM  6. Criar wrapper
REM ============================================================
echo.
echo [6/8] Criando wrapper...
(
echo @echo off
echo cd /d %MONITOR%
echo set PYTHONIOENCODING=utf-8
echo "!PY!" "%WORKER%" ^>^> "%LOGDIR%\worker.log" 2^>^&1
) > "%WRAPPER%"
echo      OK: %WRAPPER%

REM ============================================================
REM  7. Criar Task Scheduler
REM ============================================================
echo.
echo [7/8] Criando tarefa agendada...
schtasks /Create /TN "WinSyncWorker" /TR "\"%WRAPPER%\"" /SC ONSTART /DELAY 0000:30 /RU SYSTEM /RL HIGHEST /F >nul

if errorlevel 1 (
    color 0C
    echo  ERRO: falhou ao criar tarefa.
    pause
    exit /b 1
)

REM Configura restart automatico
powershell -NoProfile -ExecutionPolicy Bypass -Command "try { $t = Get-ScheduledTask -TaskName 'WinSyncWorker' -ErrorAction Stop; $s = $t.Settings; $s.RestartCount = 999; $s.RestartInterval = 'PT1M'; $s.ExecutionTimeLimit = 'PT0S'; $s.StartWhenAvailable = $true; $s.DisallowStartIfOnBatteries = $false; $s.StopIfGoingOnBatteries = $false; $s.MultipleInstances = 'IgnoreNew'; Set-ScheduledTask -TaskName 'WinSyncWorker' -Settings $s | Out-Null } catch { exit 1 }" >nul 2>&1
echo      OK

REM ============================================================
REM  8. Iniciar agora
REM ============================================================
echo.
echo [8/8] Iniciando worker...
schtasks /Run /TN "WinSyncWorker" >nul 2>&1
timeout /t 8 /nobreak >nul

REM Verifica se rodou
tasklist /FI "IMAGENAME eq python.exe" 2>nul | findstr /I "python" >nul
if not errorlevel 1 (
    echo      OK: Worker rodando
) else (
    echo      AVISO: Worker pode estar inicializando ainda.
)

echo.
echo ============================================================
echo   INSTALACAO CONCLUIDA
echo ============================================================
echo.
echo   Worker:  %WORKER%
echo   Wrapper: %WRAPPER%
echo   Logs:    %LOGDIR%\worker.log
echo.
echo   Configurado:
echo   - Inicia 30s apos boot
echo   - Reinicia em 1 min se cair (999x)
echo   - Roda como SYSTEM
echo.
echo   Aguarde 1 minuto para o worker estabilizar.
echo   Voce nao precisa mais rodar nada manualmente.
echo.
pause
