@echo off
rem Painel de opcoes: sobe o Streamlit so em localhost e reinicia se cair.
rem Chamado ao entrar no Windows pelo "Painel de Opcoes.vbs" da pasta Inicializar.
rem Para parar: parar-painel.cmd
cd /d "%~dp0"
set "PY=C:\Users\Vitor\AppData\Local\Python\pythoncore-3.14-64\python.exe"
if not exist "%PY%" set "PY=python"

rem Coletor das cotacoes do Profit (RTD) para a aba Operacoes. Uma instancia so:
rem se ja houver um rodando, o novo sai sozinho. Log em coletor.log.
start "" /b "%PY%" coletor_rtd.py

rem Vigia dos sinais: avisa no Windows e no Telegram (avisos.py). Uma instancia so.
start "" /b "%PY%" vigia.py

:laco
rem Uma instancia so: se a porta ja esta servida, esta sai em vez de insistir.
rem O aviso vai para painel.inicio.log porque o painel.log fica aberto pelo
rem servidor em execucao e recusa escrita de outro processo.
netstat -ano | findstr LISTENING | findstr /c:"127.0.0.1:8501 " >nul
if not errorlevel 1 (
  echo [%date% %time%] ja existe um painel na porta 8501; esta instancia vai sair>> painel.inicio.log
  exit /b 0
)
rem Log com rotacao simples: acima de 5 MB vira painel.old.log
if exist painel.log for %%A in (painel.log) do if %%~zA GTR 5000000 move /y painel.log painel.old.log >nul
echo [%date% %time%] iniciando o painel em http://127.0.0.1:8501>> painel.log
"%PY%" -m streamlit run app.py --server.address 127.0.0.1 --server.port 8501 --server.headless true --browser.gatherUsageStats false >> painel.log 2>&1
echo [%date% %time%] o painel encerrou; nova tentativa em 30 s>> painel.log
ping -n 31 127.0.0.1 >nul
goto laco
