@echo off
REM ---------------------------------------------------------------------------
REM Robo de COMPRAS do SGI - disparado pelo Agendador de Tarefas do Windows no
REM primeiro logon do dia (ver README: "Agendamento ao ligar o notebook").
REM
REM Por que um .bat e nao chamar o python direto na tarefa: aqui da pra fixar a
REM pasta do projeto (o Agendador inicia em C:\Windows\system32) e guardar a saida
REM num log por dia, que e como voce confere depois se rodou e o que entrou.
REM
REM Nao precisa de parametro de data: o robo olha o que ja foi sincronizado
REM (tabela sync_dias_compras) e busca sozinho os dias que faltam - entao os dias
REM em que o notebook ficou desligado entram no proximo logon.
REM ---------------------------------------------------------------------------

set PROJETO=C:\Users\Admin\Documents\estoque-defensivos\estoque-defensivos
set LOGS=%PROJETO%\logs
if not exist "%LOGS%" mkdir "%LOGS%"

REM Nome do log por dia: compras_AAAA-MM-DD.log
for /f "tokens=2 delims==" %%I in ('wmic os get localdatetime /value') do set DT=%%I
set HOJE=%DT:~0,4%-%DT:~4,2%-%DT:~6,2%
set LOG=%LOGS%\compras_%HOJE%.log

cd /d "%PROJETO%"
echo ============================================================ >> "%LOG%"
echo Inicio: %date% %time% >> "%LOG%"
py -3.11-32 scripts\sync_compras_sgi.py >> "%LOG%" 2>&1
echo Fim: %date% %time% (codigo de saida: %ERRORLEVEL%) >> "%LOG%"
exit /b %ERRORLEVEL%
