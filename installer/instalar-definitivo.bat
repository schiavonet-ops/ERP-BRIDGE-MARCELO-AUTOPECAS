@echo off
title Instalador WinSync - Marcelo Auto Pecas
color 0A

REM ============================================================
REM  Verificar Administrador
REM ============================================================
net session >nul 2>&1
if errorlevel 1 (
    color 0C
    echo.
    echo  ERRO: Esse arquivo precisa ser executado como Administrador.
    echo.
    echo  1. Feche essa janela
    echo  2. Clique com o botao DIREITO no arquivo
    echo  3. Escolha "Executar como administrador"
    echo.
    pause
    exit /b 1
)

set BASE=C:\MarceloWebEPP
set PY=%BASE%\venv\Scripts\python.exe
set WORKER=%BASE%\monitor\worker.py
set WRAPPER=%BASE%\monitor\run-worker.bat
set LOGDIR=%BASE%\logs

echo.
echo ============================================================
echo   INSTALADOR WinSync - Marcelo Auto Pecas
echo ============================================================
echo.

REM ============================================================
REM  Verificar arquivos
REM ============================================================
echo [1/7] Verificando arquivos...

if not exist "%PY%" (
    color 0C
    echo.
    echo  ERRO: Python nao encontrado em:
    echo    %PY%
    echo.
    echo  Verifique se a pasta C:\MarceloWebEPP\venv existe.
    echo.
    pause
    exit /b 1
)
echo      OK: Python encontrado

if not exist "%WORKER%" (
    color 0C
    echo.
    echo  ERRO: worker.py nao encontrado em:
    echo    %WORKER%
    echo.
    pause
    exit /b 1
)
echo      OK: worker.py encontrado

if not exist "%LOGDIR%" mkdir "%LOGDIR%"

REM ============================================================
REM  Remover servico antigo (quebrado)
REM ============================================================
echo.
echo [2/7] Removendo servico Windows antigo...
sc query WinSyncSvc >nul 2>&1
if not errorlevel 1 (
    sc stop WinSyncSvc >nul 2>&1
    timeout /t 3 /nobreak >nul
    sc delete WinSyncSvc >nul 2>&1
    echo      OK: Servico WinSyncSvc removido
) else (
    echo      OK: Nenhum servico antigo
)

REM ============================================================
REM  Matar processos antigos do worker
REM ============================================================
echo.
echo [3/7] Encerrando processos antigos do worker...
wmic process where "name='python.exe' and commandline like '%%worker.py%%'" delete >nul 2>&1
wmic process where "name='pythonw.exe' and commandline like '%%worker.py%%'" delete >nul 2>&1
echo      OK

REM ============================================================
REM  Criar wrapper .bat
REM ============================================================
echo.
echo [4/7] Criando wrapper do worker...
(
echo @echo off
echo cd /d %BASE%
echo set PYTHONIOENCODING=utf-8
echo "%PY%" "%WORKER%" ^>^> "%LOGDIR%\worker.log" 2^>^&1
) > "%WRAPPER%"
echo      OK: %WRAPPER%

REM ============================================================
REM  Remover task antiga
REM ============================================================
echo.
echo [5/7] Removendo tarefa antiga (se existir)...
schtasks /Delete /TN "WinSyncWorker" /F >nul 2>&1
echo      OK

REM ============================================================
REM  Criar nova tarefa agendada
REM ============================================================
echo.
echo [6/7] Criando tarefa agendada...
schtasks /Create /TN "WinSyncWorker" /TR "\"%WRAPPER%\"" /SC ONSTART /DELAY 0000:30 /RU SYSTEM /RL HIGHEST /F >nul

if errorlevel 1 (
    color 0C
    echo.
    echo  ERRO: Falhou ao criar tarefa agendada.
    pause
    exit /b 1
)
echo      OK: Tarefa WinSyncWorker criada

REM Configurar restart-on-failure via PowerShell
powershell -NoProfile -ExecutionPolicy Bypass -Command "try { $t = Get-ScheduledTask -TaskName 'WinSyncWorker' -ErrorAction Stop; $s = $t.Settings; $s.RestartCount = 999; $s.RestartInterval = 'PT1M'; $s.ExecutionTimeLimit = 'PT0S'; $s.StartWhenAvailable = $true; $s.DisallowStartIfOnBatteries = $false; $s.StopIfGoingOnBatteries = $false; $s.MultipleInstances = 'IgnoreNew'; Set-ScheduledTask -TaskName 'WinSyncWorker' -Settings $s | Out-Null } catch { exit 1 }" >nul 2>&1
echo      OK: Restart automatico configurado (1 min, ate 999x)

REM ============================================================
REM  Iniciar agora
REM ============================================================
echo.
echo [7/7] Iniciando worker...
schtasks /Run /TN "WinSyncWorker" >nul 2>&1
timeout /t 5 /nobreak >nul

REM Verifica se realmente subiu
tasklist /FI "IMAGENAME eq python.exe" 2>nul | findstr /I "python.exe" >nul
if not errorlevel 1 (
    echo      OK: Worker rodando
) else (
    echo      AVISO: Worker pode estar inicializando. Verifique em 1 min.
)

echo.
echo ============================================================
echo   INSTALACAO CONCLUIDA
echo ============================================================
echo.
echo   Tarefa criada: WinSyncWorker
echo   Inicia: 30s apos ligar o PC
echo   Restart: a cada 1 min se cair (ate 999x)
echo   Logs: %LOGDIR%\worker.log
echo.
echo   Pode fechar essa janela.
echo.
echo   Se algo nao funcionar, abra %LOGDIR%\worker.log
echo   para ver o erro real do Python.
echo.
pause
