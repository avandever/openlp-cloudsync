; OpenLP Show Desktop on Startup plugin installer (NSIS 3).
;
; Compile on any OS with makensis:
;   makensis showdesktop-installer.nsi
; Produces ShowDesktop-Setup-<version>.exe (a real Windows installer,
; no admin rights needed -- installs into the user's own AppData).

!define PLUGIN_VERSION "1.0"
!define PAYLOAD_DIR "showdesktop"

!include "MUI2.nsh"

Name "OpenLP Show Desktop Plugin ${PLUGIN_VERSION}"
OutFile "ShowDesktop-Setup-${PLUGIN_VERSION}.exe"
InstallDir "$APPDATA\openlp\data\contrib\plugins\showdesktop"
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
  IfFileExists "$INSTDIR\showdesktopplugin.py" 0 +2
    Rename "$INSTDIR" "$INSTDIR.backup-${__DATE__}-${__TIME__}"

  File /r "${PAYLOAD_DIR}\*.*"

  FileOpen $0 "$INSTDIR\installed-by.txt" w
  FileWrite $0 "OpenLP Show Desktop plugin v${PLUGIN_VERSION}$\r$\n"
  FileWrite $0 "Installed by ShowDesktop-Setup-${PLUGIN_VERSION}.exe$\r$\n"
  FileClose $0

  WriteUninstaller "$INSTDIR\Uninstall.exe"
  WriteRegStr HKCU "Software\Microsoft\Windows\CurrentVersion\Uninstall\OpenLPShowDesktop" \
                   "DisplayName" "OpenLP Show Desktop Plugin ${PLUGIN_VERSION}"
  WriteRegStr HKCU "Software\Microsoft\Windows\CurrentVersion\Uninstall\OpenLPShowDesktop" \
                   "UninstallString" "$INSTDIR\Uninstall.exe"
SectionEnd

Section "Uninstall"
  RMDir /r "$INSTDIR"
  DeleteRegKey HKCU "Software\Microsoft\Windows\CurrentVersion\Uninstall\OpenLPShowDesktop"
SectionEnd
