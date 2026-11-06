@echo off
title Worker SUNARP - Agente Local
color 0A
echo ========================================================
echo        INICIANDO WORKER DISTRIBUIDO SUNARP
echo ========================================================
echo Este worker conectara con tu VPS para procesar consultas.
echo Presiona Ctrl+C en cualquier momento para detenerlo.
echo ========================================================
echo.

python worker.py

pause

