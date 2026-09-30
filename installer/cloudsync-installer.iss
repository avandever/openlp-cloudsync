; OpenLP Cloud Sync plugin installer (Inno Setup 6).
;
; Build on Windows with Inno Setup (https://jrsoftware.org/isinfo.php):
;   1. Place this .iss file next to the "cloudsync" payload folder.
;   2. Open it in Inno Setup and press Compile.
;   3. The result is CloudSync-Setup-<version>.exe.
;
; Update PLUGIN_VERSION below for each release.

#define PLUGIN_VERSION "13.5"
#define PAYLOAD_DIR "cloudsync"

[Setup]
AppName=OpenLP Cloud Sync Plugin
AppVersion={#PLUGIN_VERSION}
AppPublisher=Andrew
DefaultDirName={userappdata}\openlp\data\contrib\plugins\cloudsync
DirExistsWarning=no
DisableDirPage=yes
OutputBaseFilename=CloudSync-Setup-{#PLUGIN_VERSION}
Compression=lzma2
SolidCompression=yes
PrivilegesRequired=lowest
UninstallDisplayName=OpenLP Cloud Sync Plugin

[Files]
Source: "{#PAYLOAD_DIR}\*"; DestDir: "{app}"; Flags: recursesubdirs createallsubdirs; BeforeInstall: BackupExisting()

[Code]
// Move any previous install aside before copying the new files, so no
// stale files survive an upgrade.  The sign-in token and sync state live
// in OpenLP's settings tree, not here, so they are never touched.
procedure BackupExisting();
var
  Stamp, BackupDir: String;
begin
  if DirExists(ExpandConstant('{app}')) then
  begin
    Stamp := GetDateTimeString('yyyymmdd-hhnnss', '-', ':');
    BackupDir := ExpandConstant('{app}') + '.backup-' + Stamp;
    if RenameFile(ExpandConstant('{app}'), BackupDir) then
      Log('Backed up previous install to ' + BackupDir)
    else
      Log('WARNING: could not back up previous install');
  end;
end;

function InitializeSetup(): Boolean;
var
  DataDir: String;
begin
  Result := True;
  DataDir := ExpandConstant('{userappdata}\openlp\data');
  if not DirExists(DataDir) then
  begin
    MsgBox('OpenLP data folder not found:' + #13#10 + DataDir + #13#10#13#10 +
           'Install OpenLP and run it once first, then re-run this installer.',
           mbError, MB_OK);
    Result := False;
  end;
end;
