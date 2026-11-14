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

; The window and its --service collector are both BURNRATE.exe. Neither kill uses /T, and the
; installer is not called BURNRATE.exe, so an update started by the app does not end itself.
!macro DEFINE_STOP un
Function ${un}StopBurnrate
  StrCpy $R8 0
  stop_loop:
    nsExec::Exec '"$SYSDIR\taskkill.exe" /F /IM ${EXE_NAME}'
    Pop $R0
    nsExec::Exec '"$SYSDIR\cmd.exe" /c ""$SYSDIR\tasklist.exe" /NH /FI "IMAGENAME eq ${EXE_NAME}" | "$SYSDIR\find.exe" /I "${EXE_NAME}" >nul"'
    Pop $R0
    ; find exits 0 while a BURNRATE.exe is still listed
    ${If} $R0 != 0
      Return
    ${EndIf}
    IntOp $R8 $R8 + 1
    ${If} $R8 >= 20
      Return
    ${EndIf}
    Sleep 500
    Goto stop_loop
FunctionEnd
!macroend
!insertmacro DEFINE_STOP ""
!insertmacro DEFINE_STOP "un."

; BURNRATE.exe processes currently running (the window and the --service collector); pushes the count.
Function CountBurnrate
  nsExec::ExecToStack '"$SYSDIR\cmd.exe" /c ""$SYSDIR\tasklist.exe" /NH /FI "IMAGENAME eq ${EXE_NAME}" | "$SYSDIR\find.exe" /C /I "${EXE_NAME}""'
  Pop $R0
  Pop $R1
  IntOp $R1 $R1 + 0
  Push $R1
FunctionEnd

; In-app update only ("/S /UPDATE"): the app starts this installer and then quits by itself,
; saving the window position and removing its tray icon on the way out. Give it up to ~3 s to
; do that before StopBurnrate force-kills whatever is left. The --service collector is
; long-lived and will not exit here, so the wait ends as soon as the process count drops
; (the window left) or the grace period is over.
Function WaitForWindowExit
  Call CountBurnrate
  Pop $R6
  ${If} $R6 < 1
    Return
  ${EndIf}
  StrCpy $R7 0
  grace_loop:
    Sleep 250
    Call CountBurnrate
    Pop $R5
    ${If} $R5 < $R6
      Return
    ${EndIf}
    IntOp $R7 $R7 + 1
    ${If} $R7 < 12
      Goto grace_loop
    ${EndIf}
FunctionEnd

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
  Call CheckProgramDir
  Pop $R0
  ${If} $R0 == 1
    MessageBox MB_ICONSTOP|MB_OK "${APP_NAME}'s program folder cannot be its data folder ($LOCALAPPDATA\BURNRATE). Choose another folder." /SD IDOK
    Abort
  ${EndIf}

  ${If} ${Silent}
    ${GetParameters} $R0
    ${GetOptions} $R0 "/UPDATE" $R1
    ${IfNot} ${Errors}
      Call WaitForWindowExit
    ${EndIf}
  ${EndIf}
  Call StopBurnrate

  SetOutPath "$INSTDIR"
  ; An update replaces the runtime wholesale so no file from an older build lingers.
  RMDir /r "$INSTDIR\_internal"
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
  Call un.StopBurnrate

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
