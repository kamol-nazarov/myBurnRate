; BURNRATE hooks for Tauri's NSIS installer.
;
; The desktop app starts burnrate-service.exe (and its collector worker) from
; the install folder; both must stop before files are replaced or removed.
; Neither kill uses /T: an update's installer is a child of the app.

!macro BURNRATE_STOP_PROCESSES
  nsExec::Exec 'taskkill /F /IM burnrate-desktop.exe'
  nsExec::Exec 'taskkill /F /IM burnrate-service.exe'
  Sleep 800
!macroend

!macro NSIS_HOOK_PREINSTALL
  !insertmacro BURNRATE_STOP_PROCESSES
!macroend

!macro NSIS_HOOK_PREUNINSTALL
  !insertmacro BURNRATE_STOP_PROCESSES
!macroend

; "Delete the application data" also removes BURNRATE's own data, by name:
; the usage database, settings, logs and backups. Anything else in the folder
; (and the folder itself, unless empty) stays.
!macro NSIS_HOOK_POSTUNINSTALL
  ${If} $DeleteAppDataCheckboxState = 1
  ${AndIf} $UpdateMode <> 1
    Delete "$LOCALAPPDATA\BURNRATE\spend.db"
    Delete "$LOCALAPPDATA\BURNRATE\spend.db-wal"
    Delete "$LOCALAPPDATA\BURNRATE\spend.db-shm"
    Delete "$LOCALAPPDATA\BURNRATE\.env"
    RMDir /r "$LOCALAPPDATA\BURNRATE\logs"
    RMDir /r "$LOCALAPPDATA\BURNRATE\run"
    RMDir /r "$LOCALAPPDATA\BURNRATE\backups"
    RMDir /r "$LOCALAPPDATA\BURNRATE\snapshots"
    RMDir "$LOCALAPPDATA\BURNRATE"
  ${EndIf}
!macroend
