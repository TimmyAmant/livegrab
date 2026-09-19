Unicode true
!include "MUI2.nsh"
!include "LogicLib.nsh"
!include "FileFunc.nsh"

!define APPNAME "LiveGrab for OBS"
!define APPVER "1.6.1"
!define UNKEY "Software\Microsoft\Windows\CurrentVersion\Uninstall\LiveGrab"

Name "${APPNAME}"
OutFile "LiveGrabSetup.exe"
InstallDir "$LOCALAPPDATA\LiveGrab"
RequestExecutionLevel user
SetCompressor /SOLID lzma
ShowInstDetails show
ShowUninstDetails show
BrandingText "LiveGrab ${APPVER}"
VIProductVersion "1.6.1.0"
VIAddVersionKey "ProductName" "${APPNAME}"
VIAddVersionKey "FileDescription" "${APPNAME} Setup"
VIAddVersionKey "FileVersion" "${APPVER}"
VIAddVersionKey "ProductVersion" "${APPVER}"
VIAddVersionKey "LegalCopyright" "LiveGrab"

!define MUI_ICON "icon.ico"
!define MUI_UNICON "icon.ico"
!define MUI_ABORTWARNING
!define MUI_WELCOMEPAGE_TITLE "LiveGrab for OBS"
!define MUI_WELCOMEPAGE_TEXT "This sets up everything to clip someone's live stream while they're live:$\r$\n$\r$\n  - Installs OBS, Python 3.12 and Streamlink if you don't have them$\r$\n  - Creates an OBS profile + scene collection called LiveGrab$\r$\n  - Adds the LiveGrab script, hotkeys and dock$\r$\n  - Turns on the Replay Buffer$\r$\n$\r$\nPlease close OBS before continuing. Your other profiles and scenes aren't changed, and your OBS settings are backed up first."
!define MUI_FINISHPAGE_TITLE "LiveGrab is ready"
!define MUI_FINISHPAGE_TEXT "Open OBS, type a channel in the LiveGrab dock and hit Start.$\r$\n$\r$\nHotkeys:  F9 = clip   |   F10 = save OBS replay   |   F8 = start/stop$\r$\n$\r$\nClips save to Videos\LiveGrab."
!define MUI_FINISHPAGE_RUN
!define MUI_FINISHPAGE_RUN_TEXT "Open OBS now"
!define MUI_FINISHPAGE_RUN_FUNCTION LaunchOBS
!define MUI_FINISHPAGE_SHOWREADME "$INSTDIR\README.txt"
!define MUI_FINISHPAGE_SHOWREADME_TEXT "Show quick guide"
!define MUI_FINISHPAGE_SHOWREADME_NOTCHECKED

!insertmacro MUI_PAGE_WELCOME
!insertmacro MUI_PAGE_INSTFILES
!insertmacro MUI_PAGE_FINISH
!insertmacro MUI_UNPAGE_CONFIRM
!insertmacro MUI_UNPAGE_INSTFILES
!insertmacro MUI_LANGUAGE "English"

Var PYDIR

Function WaitForOBSClosed
  loop:
    nsExec::ExecToStack 'powershell -NoProfile -NonInteractive -Command "if (Get-Process obs64,obs32 -ErrorAction SilentlyContinue) { exit 1 } else { exit 0 }"'
    Pop $0
    Pop $1
    ${If} $0 == "1"
      MessageBox MB_RETRYCANCEL|MB_ICONEXCLAMATION "OBS is open. Please close OBS, then click Retry.$\r$\n$\r$\n(OBS overwrites its settings when it closes, so it has to be closed first.)" /SD IDCANCEL IDRETRY loop
      Abort "Setup cancelled because OBS is still open."
    ${EndIf}
FunctionEnd

Function LaunchOBS
  ReadRegStr $0 HKLM "SOFTWARE\OBS Studio" ""
  ${If} $0 == ""
    StrCpy $0 "$PROGRAMFILES64\obs-studio"
  ${EndIf}
  ${If} ${FileExists} "$0\bin\64bit\obs64.exe"
    SetOutPath "$0\bin\64bit"
    Exec '"$0\bin\64bit\obs64.exe"'
  ${Else}
    MessageBox MB_OK "Couldn't find OBS to open. Start it from the Start menu."
  ${EndIf}
FunctionEnd

Section "Install"
  Call WaitForOBSClosed

  SetOutPath "$INSTDIR"
  File "livegrab.py"
  File "setup_obs.py"
  File "install_deps.ps1"
  File "README.txt"
  File "icon.ico"

  DetailPrint "Step 1 of 2: checking OBS, Python and Streamlink (downloads can take a few minutes)..."
  nsExec::ExecToLog 'powershell -NoProfile -NonInteractive -ExecutionPolicy Bypass -File "$INSTDIR\install_deps.ps1" -ResultFile "$INSTDIR\python_path.txt"'
  Pop $0
  ${If} $0 != "0"
    MessageBox MB_OK|MB_ICONSTOP "Python 3.12 couldn't be installed (code $0).$\r$\nCheck your internet connection and run the installer again." /SD IDOK
    Abort "Dependency install failed."
  ${EndIf}

  FileOpen $1 "$INSTDIR\python_path.txt" r
  FileRead $1 $PYDIR
  FileClose $1
  ; strip trailing CR/LF
  StrCpy $2 $PYDIR 1 -1
  ${If} $2 == "$\n"
    StrCpy $PYDIR $PYDIR -1
  ${EndIf}
  StrCpy $2 $PYDIR 1 -1
  ${If} $2 == "$\r"
    StrCpy $PYDIR $PYDIR -1
  ${EndIf}
  DetailPrint "Using Python at $PYDIR"

  Call WaitForOBSClosed
  DetailPrint "Step 2 of 2: setting up OBS (profile, scenes, audio, script, dock)..."
  StrCpy $3 ""
  IfSilent 0 +2
    StrCpy $3 "--update"
  nsExec::ExecToLog '"$PYDIR\python.exe" "$INSTDIR\setup_obs.py" --python-dir "$PYDIR" --script "$INSTDIR\livegrab.py" --clips-dir "$PROFILE\Videos\LiveGrab" $3'
  Pop $0
  ${If} $0 != "0"
    MessageBox MB_OK|MB_ICONSTOP "Setting up OBS failed (code $0). See the details list for what went wrong." /SD IDOK
    Abort "OBS setup failed."
  ${EndIf}

  WriteUninstaller "$INSTDIR\Uninstall.exe"
  WriteRegStr HKCU "${UNKEY}" "DisplayName" "${APPNAME}"
  WriteRegStr HKCU "${UNKEY}" "DisplayVersion" "${APPVER}"
  WriteRegStr HKCU "${UNKEY}" "Publisher" "LiveGrab"
  WriteRegStr HKCU "${UNKEY}" "DisplayIcon" "$INSTDIR\icon.ico"
  WriteRegStr HKCU "${UNKEY}" "InstallLocation" "$INSTDIR"
  WriteRegStr HKCU "${UNKEY}" "UninstallString" '"$INSTDIR\Uninstall.exe"'
  WriteRegDWORD HKCU "${UNKEY}" "NoModify" 1
  WriteRegDWORD HKCU "${UNKEY}" "NoRepair" 1
  DetailPrint "All set."
SectionEnd

Section "Uninstall"
  FileOpen $1 "$INSTDIR\python_path.txt" r
  FileRead $1 $PYDIR
  FileClose $1
  StrCpy $2 $PYDIR 1 -1
  ${If} $2 == "$\n"
    StrCpy $PYDIR $PYDIR -1
  ${EndIf}
  StrCpy $2 $PYDIR 1 -1
  ${If} $2 == "$\r"
    StrCpy $PYDIR $PYDIR -1
  ${EndIf}
  ${If} ${FileExists} "$PYDIR\python.exe"
    nsExec::ExecToLog '"$PYDIR\python.exe" "$INSTDIR\setup_obs.py" --uninstall'
    Pop $0
  ${EndIf}
  Delete "$INSTDIR\livegrab.py"
  Delete "$INSTDIR\setup_obs.py"
  Delete "$INSTDIR\install_deps.ps1"
  Delete "$INSTDIR\README.txt"
  Delete "$INSTDIR\icon.ico"
  Delete "$INSTDIR\python_path.txt"
  Delete "$INSTDIR\Uninstall.exe"
  RMDir /r "$INSTDIR\__pycache__"
  RMDir "$INSTDIR"
  DeleteRegKey HKCU "${UNKEY}"
  DetailPrint "Removed LiveGrab. Python, Streamlink, OBS and your LiveGrab profile were left installed."
SectionEnd
