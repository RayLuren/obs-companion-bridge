@echo off
call "%~dp0..\scripts\build_plugin.cmd" %*
exit /b %errorlevel%
