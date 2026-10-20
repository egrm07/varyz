#ifndef Payload
  #error Payload is required
#endif
#ifndef AppVersion
  #error AppVersion is required
#endif
#ifndef BuildId
  #error BuildId is required
#endif

[Setup]
AppId={{D151746E-801E-4E69-97C2-42ED91562AC7}
AppName=Varyz
AppVersion={#AppVersion}
AppPublisher=Egor Menshikov
AppPublisherURL=https://varyzai.com
AppSupportURL=https://varyzai.com/contact.html
AppUpdatesURL=https://varyzai.com/download.html
DefaultDirName={localappdata}\Programs\Varyz
DefaultGroupName=Varyz
PrivilegesRequired=lowest
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
MinVersion=10.0.17763
DisableDirPage=yes
DisableProgramGroupPage=yes
LicenseFile={#Payload}\LICENSE
OutputDir={#Output}
OutputBaseFilename=VaryzSetup-{#AppVersion}-windows-x64
Compression=lzma2/fast
SolidCompression=yes
WizardStyle=modern
WizardSizePercent=115
SetupIconFile={#Payload}\varyz.ico
UninstallDisplayIcon={app}\varyz.ico
CloseApplications=no
RestartApplications=no
SetupLogging=yes
Uninstallable=yes
#ifdef SignInstaller
SignTool=VaryzSign
SignedUninstaller=yes
#endif

[Files]
Source: "{#Payload}\*"; DestDir: "{tmp}\native"; Flags: dontcopy recursesubdirs createallsubdirs
Source: "{#Payload}\varyz.ico"; DestDir: "{app}"; Flags: ignoreversion

[Icons]
Name: "{group}\Varyz Setup"; Filename: "{app}\versions\{#BuildId}\runtime\pythonw.exe"; Parameters: "-I ""{app}\versions\{#BuildId}\native.py"" setup --app ""{app}"""; WorkingDir: "{app}"; IconFilename: "{app}\varyz.ico"
Name: "{group}\Uninstall Varyz"; Filename: "{uninstallexe}"

[Run]
Filename: "{app}\versions\{#BuildId}\runtime\pythonw.exe"; Parameters: "-I ""{app}\versions\{#BuildId}\native.py"" setup --app ""{app}"""; Description: "Open Varyz setup in your browser"; Flags: postinstall nowait skipifsilent runhidden

[UninstallDelete]
Type: filesandordirs; Name: "{app}\versions"
Type: filesandordirs; Name: "{app}\cache"
Type: files; Name: "{app}\native-state.json"
Type: files; Name: "{app}\setup.log"

[Code]
function PrepareToInstall(var NeedsRestart: Boolean): String;
var
  ResultCode: Integer;
  Status: AnsiString;
begin
  Result := '';
  WizardForm.StatusLabel.Caption := 'Installing Varyz and its bundled runtime...';
  ExtractTemporaryFiles('{tmp}\native\*');
  if not Exec(ExpandConstant('{tmp}\native\runtime\python.exe'),
    '-I "' + ExpandConstant('{tmp}\native\native.py') + '" install --app "' +
    WizardDirValue + '" --result "' + ExpandConstant('{tmp}\native-result.json') + '"',
    ExpandConstant('{tmp}\native'), SW_HIDE, ewWaitUntilTerminated, ResultCode) then
    Result := 'The installer worker could not start: ' + SysErrorMessage(ResultCode)
  else if ResultCode <> 0 then begin
    LoadStringFromFile(ExpandConstant('{tmp}\native-result.json'), Status);
    Result := 'Varyz could not be installed (worker exit ' + IntToStr(ResultCode) + '). Existing data was retained.' + #13#10 + String(Status);
  end;
end;

function InitializeUninstall(): Boolean;
var
  ResultCode: Integer;
  Status: AnsiString;
begin
  Result := False;
  if Exec(ExpandConstant('{app}\versions\{#BuildId}\runtime\python.exe'),
    '-I "' + ExpandConstant('{app}\versions\{#BuildId}\native.py') + '" uninstall-check --app "' +
    ExpandConstant('{app}') + '" --result "' + ExpandConstant('{tmp}\varyz-uninstall-result.json') + '"',
    ExpandConstant('{app}'), SW_HIDE, ewWaitUntilTerminated, ResultCode) and (ResultCode = 0) then
    Result := True
  else begin
    LoadStringFromFile(ExpandConstant('{tmp}\varyz-uninstall-result.json'), Status);
    MsgBox('Varyz was not removed.' +
      Chr(13) + Chr(10) + String(Status), mbError, MB_OK);
  end;
end;

procedure CurUninstallStepChanged(CurUninstallStep: TUninstallStep);
var
  ResultCode: Integer;
begin
  if CurUninstallStep = usUninstall then begin
    if not Exec(ExpandConstant('{app}\versions\{#BuildId}\runtime\python.exe'),
      '-I "' + ExpandConstant('{app}\versions\{#BuildId}\native.py') + '" uninstall --app "' +
      ExpandConstant('{app}') + '" --result "' + ExpandConstant('{tmp}\varyz-uninstall-result.json') + '"',
      ExpandConstant('{app}'), SW_HIDE, ewWaitUntilTerminated, ResultCode) then
      RaiseException('Varyz cleanup could not start; removal was stopped.');
    if ResultCode <> 0 then
      RaiseException('Varyz cleanup did not finish; removal was stopped.');
  end;
end;

procedure InitializeWizard();
begin
  WizardForm.WelcomeLabel1.Caption := 'Install Varyz';
  WizardForm.WelcomeLabel2.Caption := 'Your AI agents, briefed on your vision, taste, and creative perspective.' +
    Chr(13) + Chr(10) + Chr(13) + Chr(10) + 'Python and dependencies are included. After installation, choose your sources and agents in your browser.' +
    Chr(13) + Chr(10) + Chr(13) + Chr(10) + 'Close existing Varyz agents and stop background services before upgrading. Your data stays on your computer.';
end;
