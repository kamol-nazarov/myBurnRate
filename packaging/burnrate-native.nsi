; BURNRATE native app: per-user installer for the PyInstaller bundle.
;
; Build (packaging/build_native.py does this when makensis is available):
;   makensis /DVERSION=<x.y.z> /DVI_VERSION=<x.y.z.0> /DSOURCE_DIR=<bundle folder> /DICON=<icon.ico> ^
;            /DOUTFILE=<installer.exe> packaging/burnrate-native.nsi
;
; Two folders, never mixed up:
;   program files  $LOCALAPPDATA\Programs\BURNRATE   written and removed by this installer
;   user data      $LOCALAPPDATA\BURNRATE            spend.db, .env, native-window.json, logs:
;                                                    never written by install or update; deleted
;                                                    by uninstall only if the person ticks the box
;
; Silent use: "/S" installs without a window. "/S /UPDATE" is what the app runs for an
; in-app update: it also starts BURNRATE again when the files are in place.

Unicode true
!include "MUI2.nsh"
!include "FileFunc.nsh"
!include "LogicLib.nsh"
!include "nsDialogs.nsh"

!ifndef VERSION
  !error "Pass /DVERSION=<x.y.z>"
!endif
!ifndef VI_VERSION
  !error "Pass /DVI_VERSION=<x.y.z.0>"
!endif
!ifndef SOURCE_DIR
  !error "Pass /DSOURCE_DIR=<PyInstaller BURNRATE folder>"
!endif
!ifndef OUTFILE
  !error "Pass /DOUTFILE=<installer path>"
!endif
!ifndef ICON
  !error "Pass /DICON=<icon.ico>"
!endif

; Presentation only (installer title, Apps list, shortcuts). The ids below stay BURNRATE.
!define APP_NAME "myBurnRate"
!define EXE_NAME "BURNRATE.exe"
!define MARKER_NAME "BURNRATE.installed"
; The older Tauri desktop app has its own entry ("BURNRATE"); this one must never touch it.
!define UNINSTALL_KEY "Software\Microsoft\Windows\CurrentVersion\Uninstall\BURNRATENative"
!define SETTINGS_KEY "Software\BURNRATE Native"
!define SHORTCUT_NAME "myBurnRate"
; Shortcut name used by dev builds before the rename; removed on install and uninstall.
!define LEGACY_SHORTCUT_NAME "BURNRATE Native"

Name "${APP_NAME}"
OutFile "${OUTFILE}"
Icon "${ICON}"
UninstallIcon "${ICON}"
RequestExecutionLevel user
InstallDir "$LOCALAPPDATA\Programs\BURNRATE"
InstallDirRegKey HKCU "${SETTINGS_KEY}" "InstallDir"
SetCompressor /SOLID lzma
ShowInstDetails nevershow
ShowUninstDetails nevershow
BrandingText "${APP_NAME} ${VERSION}"
ManifestDPIAware true

VIProductVersion "${VI_VERSION}"
VIAddVersionKey "ProductName" "${APP_NAME}"
VIAddVersionKey "ProductVersion" "${VERSION}"
VIAddVersionKey "FileVersion" "${VERSION}"
VIAddVersionKey "FileDescription" "${APP_NAME} installer"
VIAddVersionKey "LegalCopyright" "Apache-2.0"

Var DeleteData
Var DeleteDataCheck
Var GuardAction
Var GuardResult

!define MUI_ICON "${ICON}"
!define MUI_UNICON "${ICON}"
!define MUI_ABORTWARNING
!define MUI_FINISHPAGE_RUN "$INSTDIR\${EXE_NAME}"
!define MUI_FINISHPAGE_RUN_TEXT "Start ${APP_NAME}"

!insertmacro MUI_PAGE_DIRECTORY
!insertmacro MUI_PAGE_INSTFILES
!insertmacro MUI_PAGE_FINISH

!insertmacro MUI_UNPAGE_CONFIRM
UninstPage custom un.DataPageCreate un.DataPageLeave
!insertmacro MUI_UNPAGE_INSTFILES

!insertmacro MUI_LANGUAGE "English"

# Use fixed arguments to the embedded helper. Directory values travel as data
# through inherited environment variables and never enter a command line.
!macro DEFINE_GUARD un
Function ${un}RunInstallGuard
  InitPluginsDir
  File "/oname=$PLUGINSDIR\native-install-guard.ps1" "native-install-guard.ps1"
  StrCpy $0 "$INSTDIR"
  StrCpy $1 "$LOCALAPPDATA\BURNRATE"
  System::Call 'kernel32::SetEnvironmentVariableW(w "BURNRATE_NSIS_PROGRAM_DIR", w r0)i.r2'
  System::Call 'kernel32::SetEnvironmentVariableW(w "BURNRATE_NSIS_DATA_DIR", w r1)i.r3'
  StrCpy $GuardResult 1
  ${If} $2 != 0
  ${AndIf} $3 != 0
    nsExec::Exec '"$SYSDIR\WindowsPowerShell\v1.0\powershell.exe" -NoProfile -NonInteractive -ExecutionPolicy Bypass -File "$PLUGINSDIR\native-install-guard.ps1" -Action $GuardAction'
    Pop $GuardResult
  ${EndIf}
  System::Call 'kernel32::SetEnvironmentVariableW(w "BURNRATE_NSIS_PROGRAM_DIR", p 0)i'
  System::Call 'kernel32::SetEnvironmentVariableW(w "BURNRATE_NSIS_DATA_DIR", p 0)i'
FunctionEnd
!macroend
!insertmacro DEFINE_GUARD ""
!insertmacro DEFINE_GUARD "un."

; Refuse the data folder (or anything inside it) as the program folder.
Function CheckProgramDir
  StrLen $R1 "$LOCALAPPDATA\BURNRATE"
  StrCpy $R2 $INSTDIR $R1
  ${If} $R2 == "$LOCALAPPDATA\BURNRATE"
    StrCpy $R3 $INSTDIR 1 $R1
    ${If} $R3 == ""
    ${OrIf} $R3 == "\"
      Push 1
      Return
    ${EndIf}
  ${EndIf}
  Push 0
FunctionEnd

Function .onVerifyInstDir
  Call CheckProgramDir
  Pop $R0
  ${If} $R0 == 1
    Abort
  ${EndIf}
FunctionEnd

Section "Install"
  StrCpy $GuardAction Install
  ${If} ${Silent}
    ${GetParameters} $R0
    ${GetOptions} $R0 "/UPDATE" $R1
    ${IfNot} ${Errors}
      StrCpy $GuardAction Update
    ${EndIf}
  ${EndIf}
  Call RunInstallGuard
  ${If} $GuardResult != 0
  ${AndIf} $GuardResult != 2
    MessageBox MB_ICONSTOP|MB_OK "Choose an empty program folder or the registered ${APP_NAME} installation. The selected installation cannot be safely replaced." /SD IDOK
    SetErrorLevel 1
    Abort
  ${EndIf}

  SetOutPath "$INSTDIR"
  ${If} $GuardResult == 2
    ; Replace the runtime only after verifying this registered installation.
    RMDir /r "$INSTDIR\_internal"
  ${EndIf}
  File /r "${SOURCE_DIR}\*.*"

  ; Tells the app it was installed by this installer, so it may update itself.
  FileOpen $0 "$INSTDIR\${MARKER_NAME}" w
  FileWrite $0 '{"installer":"nsis","version":"${VERSION}"}'
  FileClose $0

  WriteUninstaller "$INSTDIR\uninstall.exe"
  Delete "$SMPROGRAMS\${LEGACY_SHORTCUT_NAME}.lnk"
  Delete "$DESKTOP\${LEGACY_SHORTCUT_NAME}.lnk"
  CreateShortCut "$SMPROGRAMS\${SHORTCUT_NAME}.lnk" "$INSTDIR\${EXE_NAME}" "" "$INSTDIR\${EXE_NAME}" 0
  CreateShortCut "$DESKTOP\${SHORTCUT_NAME}.lnk" "$INSTDIR\${EXE_NAME}" "" "$INSTDIR\${EXE_NAME}" 0

  WriteRegStr HKCU "${SETTINGS_KEY}" "InstallDir" "$INSTDIR"
  WriteRegStr HKCU "${SETTINGS_KEY}" "Version" "${VERSION}"
  WriteRegStr HKCU "${UNINSTALL_KEY}" "DisplayName" "${APP_NAME}"
  WriteRegStr HKCU "${UNINSTALL_KEY}" "DisplayVersion" "${VERSION}"
  WriteRegStr HKCU "${UNINSTALL_KEY}" "Publisher" "BURNRATE contributors"
  WriteRegStr HKCU "${UNINSTALL_KEY}" "DisplayIcon" "$INSTDIR\${EXE_NAME}"
  WriteRegStr HKCU "${UNINSTALL_KEY}" "InstallLocation" "$INSTDIR"
  WriteRegStr HKCU "${UNINSTALL_KEY}" "URLInfoAbout" "https://github.com/kamol-nazarov/myBurnRate"
  WriteRegStr HKCU "${UNINSTALL_KEY}" "UninstallString" '"$INSTDIR\uninstall.exe"'
  WriteRegStr HKCU "${UNINSTALL_KEY}" "QuietUninstallString" '"$INSTDIR\uninstall.exe" /S'
  WriteRegDWORD HKCU "${UNINSTALL_KEY}" "NoModify" 1
  WriteRegDWORD HKCU "${UNINSTALL_KEY}" "NoRepair" 1
  ${GetSize} "$INSTDIR" "/S=0K" $0 $1 $2
  WriteRegDWORD HKCU "${UNINSTALL_KEY}" "EstimatedSize" $0
SectionEnd

Function .onInstSuccess
  ${GetParameters} $R0
  ${GetOptions} $R0 "/UPDATE" $R1
  ${IfNot} ${Errors}
  ${AndIf} ${Silent}
    Exec '"$INSTDIR\${EXE_NAME}"'
  ${EndIf}
FunctionEnd

; --- uninstall ------------------------------------------------------------------------

Function un.DataPageCreate
  nsDialogs::Create 1018
  Pop $0
  ${If} $0 == error
    Abort
  ${EndIf}
  ${NSD_CreateLabel} 0 0 100% 36u "Uninstalling removes the program and its shortcuts. Your usage history and settings stay in $LOCALAPPDATA\BURNRATE unless you tick the box below."
  Pop $0
  ${NSD_CreateCheckbox} 0 44u 100% 24u "Also delete my ${APP_NAME} data (usage history, settings, logs). Other BURNRATE apps on this computer use the same data."
  Pop $DeleteDataCheck
  nsDialogs::Show
FunctionEnd

Function un.DataPageLeave
  ${NSD_GetState} $DeleteDataCheck $DeleteData
FunctionEnd

Section "Uninstall"
  StrCpy $GuardAction Uninstall
  Call un.RunInstallGuard
  ${If} $GuardResult != 2
    MessageBox MB_ICONSTOP|MB_OK "Run this uninstaller from its registered ${APP_NAME} installation folder." /SD IDOK
    SetErrorLevel 1
    Abort
  ${EndIf}

  Delete "$SMPROGRAMS\${SHORTCUT_NAME}.lnk"
  Delete "$DESKTOP\${SHORTCUT_NAME}.lnk"
  Delete "$SMPROGRAMS\${LEGACY_SHORTCUT_NAME}.lnk"
  Delete "$DESKTOP\${LEGACY_SHORTCUT_NAME}.lnk"

  ; Program files only, by name. The folder is removed only if nothing else is in it.
  RMDir /r "$INSTDIR\_internal"
  Delete "$INSTDIR\${EXE_NAME}"
  Delete "$INSTDIR\LICENSE"
  Delete "$INSTDIR\NOTICE"
  Delete "$INSTDIR\${MARKER_NAME}"
  Delete "$INSTDIR\uninstall.exe"
  RMDir "$INSTDIR"

  DeleteRegKey HKCU "${UNINSTALL_KEY}"
  DeleteRegKey HKCU "${SETTINGS_KEY}"

  ; Only an interactive uninstall that ticked the box reaches this; /S leaves $DeleteData at 0.
  ${If} $DeleteData == ${BST_CHECKED}
    StrCpy $GuardAction DeleteData
    Call un.RunInstallGuard
    ${If} $GuardResult != 0
      MessageBox MB_ICONSTOP|MB_OK "The selected data could not be removed safely. Your usage data was kept." /SD IDOK
      SetErrorLevel 1
      Abort
    ${EndIf}
    Delete "$LOCALAPPDATA\BURNRATE\spend.db"
    Delete "$LOCALAPPDATA\BURNRATE\spend.db-wal"
    Delete "$LOCALAPPDATA\BURNRATE\spend.db-shm"
    Delete "$LOCALAPPDATA\BURNRATE\.env"
    Delete "$LOCALAPPDATA\BURNRATE\native-window.json"
    RMDir /r "$LOCALAPPDATA\BURNRATE\logs"
    RMDir /r "$LOCALAPPDATA\BURNRATE\run"
    RMDir /r "$LOCALAPPDATA\BURNRATE\backups"
    RMDir /r "$LOCALAPPDATA\BURNRATE\snapshots"
    RMDir /r "$LOCALAPPDATA\BURNRATE\updates"
    RMDir "$LOCALAPPDATA\BURNRATE"
  ${EndIf}
SectionEnd
