@echo off
rem Duplo clique aqui para publicar o painel em https://painel.quantunlab.com.br.
rem Roda uma vez so; depois o tunel sobe sozinho junto com o Windows.
chcp 65001 >nul
cd /d "%~dp0"
title Publicar o painel em painel.quantunlab.com.br
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0publicar-painel.ps1"
