; OpenLP Cloud Sync plugin installer (NSIS 3).
;
; Compile on any OS with makensis:
;   makensis cloudsync-installer.nsi
; Produces CloudSync-Setup-<version>.exe (a real Windows installer,
; no admin rights needed -- installs into the user's own AppData).

!define PLUGIN_VERSION "13.5"
!define PAYLOAD_DIR "cloudsync"

!include "MUI2.nsh"

Name "OpenLP Cloud Sync Plugin ${PLUGIN_VERSION}"
OutFile "CloudSync-Setup-${PLUGIN_VERSION}.exe"
InstallDir "$APPDATA\openlp\data\contrib\plugins\cloudsync"
RequestExecutionLevel user

!insertmacro MUI_PAGE_INSTFILES
!insertmacro MUI_LANGUAGE "English"

; --- refuse to install if OpenLP has never been run -------------------------
Function .onInit
  IfFileExists "$APPDATA\openlp\data\*.*" +3 0
    MessageBox MB_OK|MB_ICONSTOP "OpenLP data folder not found:$\r$\n$APPDATA\openlp\data$\r$\n$\r$\nInstall OpenLP and run it once first, then re-run this installer."
    Abort
FunctionEnd

Section "Install"
  SetOutPath $INSTDIR

  ; Move any previous install aside so no stale files survive an upgrade.
  ; The sign-in token and sync state live in OpenLP's settings tree,
  ; not here, so they are never touched.
  IfFileExists "$INSTDIR\cloudsyncplugin.py" 0 +2
    Rename "$INSTDIR" "$INSTDIR.backup-${__DATE__}-${__TIME__}"

  File /r "${PAYLOAD_DIR}\*.*"

  FileOpen $0 "$INSTDIR\installed-by.txt" w
  FileWrite $0 "OpenLP Cloud Sync plugin v${PLUGIN_VERSION}$\r$\n"
  FileWrite $0 "Installed by CloudSync-Setup-${PLUGIN_VERSION}.exe$\r$\n"
  FileClose $0

  WriteUninstaller "$INSTDIR\Uninstall.exe"
  WriteRegStr HKCU "Software\Microsoft\Windows\CurrentVersion\Uninstall\OpenLPCloudSync" \
                   "DisplayName" "OpenLP Cloud Sync Plugin ${PLUGIN_VERSION}"
  WriteRegStr HKCU "Software\Microsoft\Windows\CurrentVersion\Uninstall\OpenLPCloudSync" \
                   "UninstallString" "$INSTDIR\Uninstall.exe"
SectionEnd

Section "Uninstall"
  RMDir /r "$INSTDIR"
  DeleteRegKey HKCU "Software\Microsoft\Windows\CurrentVersion\Uninstall\OpenLPCloudSync"
SectionEnd
