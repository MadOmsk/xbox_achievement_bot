@echo off
rem Wrapper around manage.ps1. PowerShell refuses unsigned scripts under the
rem default Restricted policy; bypassing it here leaves the machine's policy
rem alone. Kept ASCII-only on purpose: cmd reads a .bat in the console codepage,
rem so Cyrillic in this file would break. All Russian text lives in manage.ps1.
chcp 65001 >nul
setlocal

rem A double click passes no arguments, and a window that ran one command and
rem vanished would be useless — show the menu instead.
if "%~1"=="" (
  call :ps menu
  exit /b %errorlevel%
)

call :ps %*
exit /b %errorlevel%

:ps
powershell -NoProfile -ExecutionPolicy Bypass -Command ^
  "[Console]::OutputEncoding=[Text.Encoding]::UTF8; & '%~dp0manage.ps1' %*"
exit /b %errorlevel%
