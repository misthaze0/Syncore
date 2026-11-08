@echo off
REM start.bat —— One-click launch for Syncore
REM
REM Why this .bat exists:
REM   1. Repo path contains Chinese characters and spaces — .bat handles it fine
REM   2. Bypasses PowerShell ExecutionPolicy
REM
REM Usage:
REM   start.bat                    normal launch (backend :8000 + frontend :5173)

setlocal enabledelayedexpansion

cd /d "%~dp0"

powershell -NoProfile -ExecutionPolicy Bypass -File ".\start.ps1"
