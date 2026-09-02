@echo off
rem Wrapper around manage.ps1: PowerShell refuses to run unsigned scripts under
rem the default Restricted policy, and this keeps the machine's policy untouched.
rem UTF-8 console, or the Russian output arrives as mojibake in cmd and Git Bash.
chcp 65001 >nul
powershell -NoProfile -ExecutionPolicy Bypass -Command ^
  "[Console]::OutputEncoding=[Text.Encoding]::UTF8; & '%~dp0manage.ps1' %*"
