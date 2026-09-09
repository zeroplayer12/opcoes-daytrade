@echo off
chcp 65001 >nul
cd /d "%~dp0"
title Opcoes Day Trade - link publico

echo.
echo  ================================================================
echo   OPCOES DAY TRADE - subindo painel + link publico
echo  ================================================================
echo.

if not exist "cloudflared.exe" (
    echo  [!] cloudflared.exe nao encontrado nesta pasta.
    echo      Baixe em: https://github.com/cloudflare/cloudflared/releases/latest
    echo      Arquivo:  cloudflared-windows-amd64.exe  ^(renomeie para cloudflared.exe^)
    echo.
    pause
    exit /b 1
)

echo  [1/2] Iniciando o Streamlit numa janela separada...
start "Streamlit - Opcoes Day Trade" cmd /c "streamlit run app.py --server.port 8501 --server.headless true --browser.gatherUsageStats false"

echo        aguardando a porta 8501 responder...
set /a tentativas=0
:esperar
set /a tentativas+=1
timeout /t 2 /nobreak >nul
curl -s -o nul --max-time 3 http://localhost:8501/_stcore/health && goto pronto
if %tentativas% lss 20 goto esperar
echo.
echo  [!] O Streamlit nao subiu em 40s. Veja a outra janela para o erro.
pause
exit /b 1

:pronto
echo        OK, painel no ar em http://localhost:8501
echo.
echo  [2/2] Abrindo o tunel publico da Cloudflare...
echo.
echo  ----------------------------------------------------------------
echo   O LINK aparece abaixo, dentro da moldura, terminando em
echo   .trycloudflare.com  -  copie e abra no celular ou compartilhe.
echo.
echo   Ele vive enquanto ESTA janela estiver aberta. Fechou, morreu,
echo   e da proxima vez o endereco sera outro.
echo.
echo   Se nao abrir no PC mas abrir no celular: e cache de DNS do
echo   provedor. Espere um minuto e recarregue, ou use o DNS 1.1.1.1.
echo  ----------------------------------------------------------------
echo.

cloudflared.exe tunnel --url http://localhost:8501 --no-autoupdate

echo.
echo  Tunel encerrado. O Streamlit continua rodando na outra janela.
pause
