@echo off
rem Painel de opcoes: sobe o Streamlit so em localhost e reinicia se cair.
rem Chamado ao entrar no Windows pelo "Painel de Opcoes.vbs" da pasta Inicializar.
rem Para parar: parar-painel.cmd
cd /d "%~dp0"
set "PY=C:\Users\Vitor\AppData\Local\Python\pythoncore-3.14-64\python.exe"
if not exist "%PY%" set "PY=python"

:laco
rem Log com rotacao simples: acima de 5 MB vira painel.old.log
if exist painel.log for %%A in (painel.log) do if %%~zA GTR 5000000 move /y painel.log painel.old.log >nul
echo [%date% %time%] iniciando o painel em http://127.0.0.1:8501>> painel.log
"%PY%" -m streamlit run app.py --server.address 127.0.0.1 --server.port 8501 --server.headless true --browser.gatherUsageStats false >> painel.log 2>&1
echo [%date% %time%] o painel encerrou; nova tentativa em 30 s>> painel.log
ping -n 31 127.0.0.1 >nul
goto laco
