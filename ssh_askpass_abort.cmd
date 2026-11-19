@echo off
rem Windows counterpart of ssh_askpass_abort.sh, used by sshutil.py (see ssh_askpass_abort.py).
rem sshutil.py sets SSH_ASKPASS to this file, CM_ASKPASS_PYTHON to a python.exe and
rem CM_ASKPASS_HOLD to a little more than its ssh timeout (seconds).
rem As soon as the server asks for input, ssh.exe is killed and nothing is answered.
rem If this file exits while ssh is still alive, Windows OpenSSH sends an EMPTY answer,
rem so unless the python helper reports success (exit code 77) we never exit early:
rem we hold until the caller's timeout kills ssh (and this process tree).
rem Keep this file ASCII with CRLF line endings, or cmd.exe misparses it.
setlocal
if not defined CM_ASKPASS_PYTHON goto hold
if not exist "%CM_ASKPASS_PYTHON%" goto hold
if not exist "%~dp0ssh_askpass_abort.py" goto hold
"%CM_ASKPASS_PYTHON%" -I "%~dp0ssh_askpass_abort.py" %*
if errorlevel 77 if not errorlevel 78 exit /b 1
:hold
echo @@NEED_INPUT (askpass helper failed, not answering) %* 1>&2
set HOLD=%CM_ASKPASS_HOLD%
if not defined HOLD set HOLD=600
set /a HOLD=HOLD+1
ping -n %HOLD% 127.0.0.1 >nul
exit /b 1
