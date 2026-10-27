; EUTONOS Setup: a normal Windows wizard (Inno Setup 6), per user, no administrator rights.
;
; Build it with  python tools/build_setup.py  (it stages the wheel, a bundled python.org Python and bootstrap.py,
; then runs ISCC with the defines below). Read installer\windows\README.md first.
;
; What the wizard does, in order: a splash, a welcome page that says what EUTONOS is, the install-location page with
; a recommended folder, a parts page, a "who uses this login" page, a page that shows the exact consent text with "I agree", the install (the
; EUTONOS engine runs with live steps), and a finish page (with an "Open dashboard" button after a good install).
; Setup installs every part this PC can run; a tick on the parts page only says "switch it on now". One more page asks
; "Only I use this Windows login": when ticked, a page after the install lets the person confirm their own Claude login
; emails, and the wizard helper then saves them (Crosstalk linked accounts). Nothing is saved when it is not ticked.
; Uninstall (Installed apps) runs the engine's uninstall
; first and stops, deleting nothing, if the engine says it failed.
;
; All the real work is done by the EUTONOS engine (eutonos.installer, run from the wheel Setup carries). This script
; only asks the questions, shows the engine's own consent text and starts it. It never writes the registry or the
; Startup folder itself, except the one Installed apps entry Inno Setup makes for its own uninstaller.

#ifndef AppVersion
  #define AppVersion "0.0.0"
#endif
#ifndef StageDir
  #define StageDir "..\..\build\setup\stage"
#endif
#ifndef OutputDir
  #define OutputDir "..\..\dist"
#endif
#ifndef OutputBaseFilename
  #define OutputBaseFilename "EUTONOS-Setup-" + AppVersion
#endif

[Setup]
AppId={{26E45B59-2A6F-498A-81AC-32B29F5F758E}
AppName=EUTONOS
AppVersion={#AppVersion}
AppPublisher=EUTONOS
VersionInfoDescription=EUTONOS Setup
VersionInfoProductName=EUTONOS
DefaultDirName={%USERPROFILE}\.eutonos
UsePreviousAppDir=yes
DirExistsWarning=no
DisableProgramGroupPage=yes
DisableReadyPage=yes
PrivilegesRequired=lowest
ArchitecturesAllowed=x64compatible
MinVersion=10.0
WizardStyle=modern
WizardSizePercent=120
Compression=lzma2/max
SolidCompression=yes
ExtraDiskSpaceRequired=104857600
UninstallDisplayName=EUTONOS
UninstallDisplayIcon={app}\python\pythonw.exe
CloseApplications=no
RestartApplications=no
SetupLogging=no
OutputDir={#OutputDir}
OutputBaseFilename={#OutputBaseFilename}

[Messages]
WelcomeLabel1=Welcome to EUTONOS
WelcomeLabel2=EUTONOS gives your AI helpers a shared memory.%n%nIt keeps project memory and task boards in one place, so your Claude, Codex and Hermes chats remember what happened and what comes next. With Crosstalk, those chats can also send each other letters and work together.%n%nThis Setup puts EUTONOS on this PC for your user only; it does not need administrator rights. It shows you exactly what it will change and asks first, and Uninstall puts everything back.%n%nClick Next to choose where to install it.
SelectDirLabel3=Setup will install EUTONOS into the following folder.
SelectDirBrowseLabel=The recommended place is your own EUTONOS folder, shown below. Click Next to continue, or Browse to pick another folder.

[Files]
; The bundled Python comes first: Setup extracts it early (before anything is installed) to run its own checks.
; notimestamp: the same stage gives the same Setup.exe whatever time the files were written (a build is reproducible).
Source: "{#StageDir}\python\*"; DestDir: "{app}\python"; Flags: recursesubdirs createallsubdirs ignoreversion notimestamp
Source: "{#StageDir}\bootstrap.py"; DestDir: "{app}\setup"; Flags: ignoreversion notimestamp
Source: "{#StageDir}\wheel\*.whl"; DestDir: "{app}\setup"; Flags: ignoreversion notimestamp

[Run]
Filename: "{win}\explorer.exe"; Parameters: """{app}"""; Description: "Show the EUTONOS folder"; Flags: postinstall nowait skipifsilent unchecked; Check: InstallWorked

[Code]
var
  TempPython, TempBoot, TempWheel, IniPath, ChosenHome, BuiltFor, RecommendedDir, Tail: String;
  Parts: TArrayOfString;
  PartsPage, SoloPage, UpdatePage, ConsentPage, LinkedPage: TWizardPage;
  PartList: TNewCheckListBox;
  PartsNote, SoloNote, LinkedHint, UpdateNote: TNewStaticText;
  UpdateBox: TNewComboBox;
  ChangeMemo, TextMemo, LinkedMemo: TNewMemo;
  AgreeBox, SoloBox: TNewCheckBox;
  RecLabel: TNewStaticText;
  RecButton, DashButton: TNewButton;
  Splash: TSetupForm;
  GitOk, Adopt, RcLauncher, Failed, RepairMode: Boolean;
  GitGuidance, RcName, SettingsReport, UpdateReport: String;

function Q(const S: String): String;
begin
  Result := '"' + S + '"';
end;

function ShouldSkipPage(PageID: Integer): Boolean;
begin
  Result := RepairMode and ((PageID = PartsPage.ID) or (PageID = SoloPage.ID) or (PageID = ConsentPage.ID));
  { The update choice is skipped on a support-file repair too (nothing is installed or changed then). }
  if PageID = UpdatePage.ID then
    Result := RepairMode;
  { The confirm-your-logins page comes after the install, and only for a good install of someone who ticked the box. }
  if PageID = LinkedPage.ID then
    Result := RepairMode or Failed or (not SoloBox.Checked);
end;

function InstallWorked: Boolean;
begin
  Result := not Failed;
end;

function IniGet(const Key, Def: String): String;
begin
  Result := GetIniString('setup', Key, Def, IniPath);
end;

function SplitParts(S: String): TArrayOfString;
var
  P, N: Integer;
  Item: String;
begin
  N := 0;
  while S <> '' do
  begin
    P := Pos(',', S);
    if P = 0 then
    begin
      Item := S;
      S := '';
    end else
    begin
      Item := Copy(S, 1, P - 1);
      S := Copy(S, P + 1, Length(S));
    end;
    if Item <> '' then
    begin
      SetArrayLength(Result, N + 1);
      Result[N] := Item;
      N := N + 1;
    end;
  end;
end;

function FindWheel(const Dir: String): String;
var
  R: TFindRec;
begin
  Result := Dir + '\eutonos-{#AppVersion}-py3-none-any.whl';
  if FileExists(Result) then
    Exit;
  Result := '';
  if FindFirst(Dir + '\eutonos-*.whl', R) then
  begin
    Result := Dir + '\' + R.Name;
    FindClose(R);
  end;
end;

procedure LogLine(const S: String; const Error, FirstLine: Boolean);
var
  Rest, Text: String;
  P, Step, Total: Integer;
begin
  Log(S);
  if Trim(S) <> '' then
    Tail := Tail + S + #13#10;
  if Length(Tail) > 2500 then
    Tail := Copy(Tail, Length(Tail) - 2499, 2500);
  if Pos('EUTONOS-STEP|', S) = 1 then
  begin
    Rest := Copy(S, 14, Length(S));
    P := Pos('|', Rest);
    if P > 0 then
    begin
      Step := StrToIntDef(Copy(Rest, 1, P - 1), 0);
      Rest := Copy(Rest, P + 1, Length(Rest));
      P := Pos('|', Rest);
      if P > 0 then
      begin
        Total := StrToIntDef(Copy(Rest, 1, P - 1), 0);
        Text := Trim(Copy(Rest, P + 1, Length(Rest)));
        WizardForm.StatusLabel.Caption := Text;
        WizardForm.FilenameLabel.Caption := 'Step ' + IntToStr(Step) + ' of ' + IntToStr(Total);
        if Total > 0 then
        begin
          WizardForm.ProgressGauge.Min := 0;
          WizardForm.ProgressGauge.Max := Total;
          WizardForm.ProgressGauge.Position := Step - 1;
        end;
        WizardForm.StatusLabel.Update;
        WizardForm.FilenameLabel.Update;
        WizardForm.ProgressGauge.Update;
      end;
    end;
  end;
end;

function RecoveryCommands: String;
var
  Prefix: String;
begin
  Prefix := Q(ChosenHome + '\python\python.exe') + ' -I -B ' + Q(ChosenHome + '\setup\bootstrap.py') + ' ' +
    Q(FindWheel(ChosenHome + '\setup')) + ' engine --home ' + Q(ChosenHome) + ' ';
  Result := 'In PowerShell, inspect first:' + #13#10 + '& ' + Prefix + 'status' + #13#10#13#10 +
    'Then choose one:' + #13#10 + '& ' + Prefix + 'resume' + #13#10 + '& ' + Prefix + 'uninstall --keep-projects';
end;

function TailText(Count: Integer): String;
var
  S: String;
  P, I: Integer;
begin
  S := Trim(Tail);
  for I := 1 to Count do
  begin
    P := 0;
    if Length(S) > 0 then
    begin
      P := Length(S);
      while (P > 0) and (S[P] <> #10) do
        P := P - 1;
    end;
    if P = 0 then
    begin
      Result := Trim(S) + #13#10 + Result;
      Exit;
    end;
    Result := Copy(S, P + 1, Length(S)) + #13#10 + Result;
    S := Trim(Copy(S, 1, P - 1));
  end;
  Result := Trim(Result);
end;

procedure UnLogLine(const S: String; const Error, FirstLine: Boolean);
begin
  Log(S);
  if Trim(S) <> '' then
    Tail := Tail + S + #13#10;
  if Length(Tail) > 2500 then
    Tail := Copy(Tail, Length(Tail) - 2499, 2500);
end;

{ Run Setup's own wizard helper (inspect, consent text) with the Python extracted to the temporary folder. }
function RunWizardTool(const Args: String): Boolean;
var
  Code: Integer;
begin
  Result := Exec(TempPython, '-I -B ' + Q(TempBoot) + ' ' + Q(TempWheel) + ' wizard ' + Args, ExpandConstant('{tmp}'),
    SW_HIDE, ewWaitUntilTerminated, Code) and (Code = 0);
  if not Result then
    Log('wizard tool failed: ' + Args);
end;

function Inspect(const Home: String): Boolean;
begin
  IniPath := ExpandConstant('{tmp}\inspect.ini');
  DeleteFile(IniPath);
  Result := RunWizardTool('inspect --home ' + Q(Home) + ' --ini ' + Q(IniPath)) and FileExists(IniPath);
  if Result then
  begin
    GitOk := IniGet('git_ok', '0') = '1';
    GitGuidance := IniGet('git_guidance', '');
    Adopt := IniGet('adopt', '0') = '1';
    RcLauncher := IniGet('rc_launcher', '0') = '1';
    RcName := IniGet('rc_launcher_name', '');
    Parts := SplitParts(IniGet('parts', ''));
  end;
end;

function PartDescription(const Id: String): String;
begin
  if Id = 'core' then
    Result := 'The shared project memory and task boards. Always installed and on.'
  else if Id = 'crosstalk' then
    Result := 'Lets your Claude, Codex and Hermes chats send each other letters.'
  else if Id = 'hermes' then
    Result := 'Connects Hermes to Crosstalk. Needs Crosstalk.'
  else if Id = 'remote-control' then
    Result := 'Starts the Claude Remote Control launcher when you sign in.'
  else if Id = 'companion' then
    Result := 'The little E badge beside your AI apps, with its popup.'
  else if Id = 'simon' then
    Result := 'Lets agents move the mouse and type on this desktop (Windows desktop-control helper). Off until you tick it.'
  else
    Result := '';
end;

function PartIndex(const Id: String): Integer;
var
  I: Integer;
begin
  Result := -1;
  for I := 0 to GetArrayLength(Parts) - 1 do
    if Parts[I] = Id then
      Result := I;
end;

procedure PartClickCheck(Sender: TObject);
var
  C, H: Integer;
begin
  C := PartIndex('crosstalk');
  H := PartIndex('hermes');
  if (C >= 0) and (H >= 0) then
  begin
    if not PartList.Checked[C] then
    begin
      PartList.Checked[H] := False;
      PartList.ItemEnabled[H] := False;
    end else
      PartList.ItemEnabled[H] := IniGet('hermes_available', '0') = '1';
  end;
end;

procedure BuildPartsList;
var
  I: Integer;
  Id, Note, Sub: String;
  IsOn, Avail: Boolean;
begin
  PartList.Items.Clear;
  for I := 0 to GetArrayLength(Parts) - 1 do
  begin
    Id := Parts[I];
    Avail := IniGet(Id + '_available', '1') = '1';
    IsOn := (IniGet(Id + '_default', 'on') = 'on') and Avail;
    Note := IniGet(Id + '_note', '');
    Sub := PartDescription(Id);
    if Note <> '' then
      Sub := Sub + ' Note: ' + Note + '.';
    PartList.AddCheckBox(IniGet(Id + '_label', IniGet(Id + '_name', Id)), Sub, 0, IsOn, (Id <> 'core') and Avail, False, True, nil);
  end;
  PartClickCheck(nil);
  { "Only I use this Windows login" needs Crosstalk to be there; a different folder starts the choice again. }
  SoloBox.Checked := False;
  SoloBox.Enabled := (PartIndex('crosstalk') >= 0) and (IniGet('crosstalk_available', '0') = '1');
  BuiltFor := ChosenHome;
  if Adopt then
    PartsNote.Caption := 'An earlier hand-made EUTONOS install was found on this PC. Setup will take it over: its files and ' +
      'settings are backed up first, and Uninstall puts them back. You will see this on the next page.'
  else
    PartsNote.Caption := '';
end;

function ChoiceArgs: String;
var
  I: Integer;
begin
  Result := '';
  for I := 0 to GetArrayLength(Parts) - 1 do
    if Parts[I] <> 'core' then
    begin
      if PartList.Checked[I] then
        Result := Result + ' --component ' + Parts[I] + '=on'
      else
        Result := Result + ' --component ' + Parts[I] + '=off';
    end;
end;

{ What both the consent text and the engine are told: the parts and adopt. }
function SharedArgs: String;
begin
  Result := ChoiceArgs;
  if Adopt then
    Result := Result + ' --adopt';
end;

{ Extra plain lines for the consent text. Only consent-prepare takes them: they travel to the engine inside the consent file
  the person accepted, which the engine checks against its own rendering. The engine's install command has no --note. }
function NoteArgs: String;
var
  Rc: Integer;
begin
  Result := '';
  if SoloBox.Checked and (IniGet('solo_note', '') <> '') then
    Result := Result + ' --note ' + Q(IniGet('solo_note', ''));
  Rc := PartIndex('remote-control');
  if RcLauncher and (Rc >= 0) then
  begin
    if PartList.Checked[Rc] then
      Result := Result + ' --note ' + Q('You chose to add a Remote Control launcher although you already have one at login (' + RcName + '), so two will start.')
    else
      Result := Result + ' --note ' + Q('You already have a Remote Control launcher at login (' + RcName + '). EUTONOS leaves it alone and adds no second one.');
  end;
end;

function PrepareConsent: Boolean;
begin
  DeleteFile(ExpandConstant('{tmp}\consent.txt'));
  Result := RunWizardTool('consent-prepare --home ' + Q(ChosenHome) + SharedArgs + NoteArgs + ' --python-dir ' + Q(ChosenHome + '\python') +
    ' --installed-apps --text-out ' + Q(ExpandConstant('{tmp}\consent.txt')) + ' --lines-out ' + Q(ExpandConstant('{tmp}\changes.txt')) +
    ' --template-out ' + Q(ExpandConstant('{tmp}\consent-template.json')));
  if Result then
  begin
    ChangeMemo.Lines.LoadFromFile(ExpandConstant('{tmp}\changes.txt'));
    TextMemo.Lines.LoadFromFile(ExpandConstant('{tmp}\consent.txt'));
  end;
end;

procedure AgreeClick(Sender: TObject);
begin
  WizardForm.NextButton.Enabled := AgreeBox.Checked;
end;

{ After the install: show the Claude emails Crosstalk has seen (a suggestion only, nothing is saved yet), for the person to
  keep or fix. Without any, the field is blank and says to add them later in the dashboard. }
procedure PrepareLinked;
var
  Suggested: String;
begin
  Suggested := ExpandConstant('{tmp}\suggested.txt');
  LinkedMemo.Lines.Clear;
  DeleteFile(Suggested);
  if RunWizardTool('suggest-accounts --home ' + Q(ChosenHome) + ' --out ' + Q(Suggested)) and FileExists(Suggested) then
    LinkedMemo.Lines.LoadFromFile(Suggested);
  if Trim(LinkedMemo.Text) <> '' then
    LinkedHint.Caption := 'Crosstalk has seen these Claude logins. Keep only your own, one email per line. Nothing is saved until you click Next.'
  else
    LinkedHint.Caption := 'Type your own Claude login emails, one per line, or leave this blank and add them later in the EUTONOS dashboard. ' +
      'Nothing is saved until you click Next.';
end;

{ Save what the person confirmed, through the wizard helper. A blank field saves nothing. A problem is shown on the finish
  page and never fails the install (the helper also leaves a step for the next agent). }
procedure ApplyLinked;
var
  Typed, Report: String;
  Raw: AnsiString;
begin
  SettingsReport := '';
  Typed := ExpandConstant('{tmp}\linked.txt');
  Report := ExpandConstant('{tmp}\settings-report.txt');
  DeleteFile(Typed);
  DeleteFile(Report);
  SaveStringToFile(Typed, LinkedMemo.Text, False);
  if RunWizardTool('apply-settings --home ' + Q(ChosenHome) + ' --linked-accounts-file ' + Q(Typed) + ' --result-out ' + Q(Report)) and
    LoadStringFromFile(Report, Raw) then
    SettingsReport := Trim(String(Raw))
  else
    SettingsReport := 'Linked accounts: Setup could not save them. You can add your own Claude logins later in the EUTONOS dashboard.';
end;

{ The update choice, as the word the engine takes (the order matches the list built in InitializeWizard). }
function UpdateModeWord: String;
begin
  case UpdateBox.ItemIndex of
    0: Result := 'manual';
    1: Result := 'notify';
  else
    Result := 'auto';
  end;
end;

{ Save the update choice after a good install. A problem is shown on the finish page and never fails the install. }
procedure ApplyUpdateMode;
var
  Report: String;
  Raw: AnsiString;
begin
  UpdateReport := '';
  Report := ExpandConstant('{tmp}\update-report.txt');
  DeleteFile(Report);
  if RunWizardTool('apply-settings --home ' + Q(ChosenHome) + ' --update-mode ' + UpdateModeWord + ' --result-out ' + Q(Report)) and
    LoadStringFromFile(Report, Raw) then
    UpdateReport := Trim(String(Raw))
  else
    UpdateReport := 'Updates: Setup could not save your choice. You can set it later in the EUTONOS dashboard, Features, Updates.';
end;

procedure UseRecommended(Sender: TObject);
begin
  WizardForm.DirEdit.Text := RecommendedDir;
end;

{ The finish page's "Open dashboard" button: run only on a click, from the files Setup just installed (not the
  temporary copy, which Setup deletes when it closes). The helper starts the installed dashboard when it is not already
  running and opens the browser once it answers; it says so itself if it cannot. It is not waited for, so the page
  stays usable while the dashboard starts. }
procedure OpenDashboardClick(Sender: TObject);
var
  Code: Integer;
begin
  DashButton.Enabled := False;
  DashButton.Caption := 'Opening the dashboard...';
  if not Exec(ChosenHome + '\python\python.exe', '-I -B ' + Q(ChosenHome + '\setup\bootstrap.py') + ' ' +
      Q(FindWheel(ChosenHome + '\setup')) + ' wizard open-dashboard --home ' + Q(ChosenHome),
      ChosenHome, SW_HIDE, ewNoWait, Code) then
  begin
    DashButton.Caption := 'Open dashboard';
    DashButton.Enabled := True;
    MsgBox('Setup could not start the dashboard. You can open it later with eutonos-dashboard serve, in the bin folder of ' +
      ChosenHome + '.', mbError, MB_OK);
  end;
end;

function GoodHome(const H: String): Boolean;
begin
  Result := (Length(H) > 3) and (H[2] = ':') and (H[3] = '\');
end;

procedure MakeSplash;
var
  Title, Line: TNewStaticText;
begin
  Splash := CreateCustomForm(ScaleX(460), ScaleY(230), False, False);
  Splash.BorderStyle := bsNone;
  Splash.Position := poScreenCenter;
  Splash.Color := $00402A1B;
  Title := TNewStaticText.Create(Splash);
  Title.Parent := Splash;
  Title.AutoSize := True;
  Title.Caption := 'EUTONOS';
  Title.Font.Size := 34;
  Title.Font.Style := [fsBold];
  Title.Font.Color := clWhite;
  Title.Left := ScaleX(40);
  Title.Top := ScaleY(60);
  Line := TNewStaticText.Create(Splash);
  Line.Parent := Splash;
  Line.AutoSize := True;
  Line.Caption := 'Shared memory and teamwork for your AI chats';
  Line.Font.Size := 11;
  Line.Font.Color := $00E0D0C0;
  Line.Left := ScaleX(42);
  Line.Top := ScaleY(130);
  Splash.Show;
  Splash.Update;
end;

function InitializeSetup: Boolean;
begin
  Result := True;
  if WizardSilent then
  begin
    SuppressibleMsgBox('EUTONOS Setup needs you to read and accept what it will change, so it cannot run silently.', mbError, MB_OK, IDOK);
    Result := False;
  end;
end;

procedure InitializeWizard;
var
  Wrapped: TNewStaticText;
begin
  MakeSplash;
  Tail := '';
  RecommendedDir := ExpandConstant('{%USERPROFILE}') + '\.eutonos';
  try
    ExtractTemporaryFiles('{app}\python\*');
    ExtractTemporaryFiles('{app}\setup\*');
    TempPython := ExpandConstant('{tmp}') + '\{app}\python\python.exe';
    TempBoot := ExpandConstant('{tmp}') + '\{app}\setup\bootstrap.py';
    TempWheel := FindWheel(ExpandConstant('{tmp}') + '\{app}\setup');
    if (not FileExists(TempPython)) or (not FileExists(TempBoot)) or (TempWheel = '') then
      RaiseException('Setup could not unpack its own files. Download Setup again, or free some disk space.');
    ChosenHome := RemoveBackslashUnlessRoot(WizardDirValue);
    RepairMode := False;
    if not Inspect(ChosenHome) then
      RaiseException('Setup could not look at this PC. Its own Python would not start; Setup may be damaged.');
  except
    Splash.Hide;
    MsgBox(GetExceptionMessage, mbError, MB_OK);
    Splash.Free;
    Abort;
  end;
  Sleep(1200);
  Splash.Hide;
  Splash.Free;

  { Location page: the recommended folder and a button to go back to it. }
  RecLabel := TNewStaticText.Create(WizardForm);
  RecLabel.Parent := WizardForm.SelectDirPage;
  RecLabel.Left := WizardForm.DirEdit.Left;
  RecLabel.Top := WizardForm.DirEdit.Top + WizardForm.DirEdit.Height + ScaleY(14);
  RecLabel.AutoSize := True;
  RecLabel.Caption := 'Recommended location: ' + RecommendedDir;
  RecButton := TNewButton.Create(WizardForm);
  RecButton.Parent := WizardForm.SelectDirPage;
  RecButton.Left := WizardForm.DirEdit.Left;
  RecButton.Top := RecLabel.Top + RecLabel.Height + ScaleY(8);
  RecButton.Caption := 'Use the recommended location';
  RecButton.Width := WizardForm.CalculateButtonWidth([RecButton.Caption]) + ScaleX(16);
  RecButton.Height := ScaleY(23);
  RecButton.OnClick := @UseRecommended;

  { Finish page: an "Open dashboard" button in the button row, bottom left (shown only after a good install). }
  DashButton := TNewButton.Create(WizardForm);
  DashButton.Parent := WizardForm;
  DashButton.Caption := 'Open dashboard';
  DashButton.Width := WizardForm.CalculateButtonWidth([DashButton.Caption, 'Opening the dashboard...']) + ScaleX(16);
  DashButton.Height := WizardForm.CancelButton.Height;
  DashButton.Left := ScaleX(20);
  DashButton.Top := WizardForm.CancelButton.Top;
  DashButton.Visible := False;
  DashButton.OnClick := @OpenDashboardClick;

  { Parts page. }
  PartsPage := CreateCustomPage(wpSelectDir, 'Choose what to switch on',
    'Setup installs every part this PC can run. Tick the ones to switch on now; you can switch any of them on or off later.');
  PartList := TNewCheckListBox.Create(PartsPage);
  PartList.Parent := PartsPage.Surface;
  PartList.Left := 0;
  PartList.Top := 0;
  PartList.Width := PartsPage.SurfaceWidth;
  PartList.Height := PartsPage.SurfaceHeight - ScaleY(48);
  PartList.OnClickCheck := @PartClickCheck;
  PartsNote := TNewStaticText.Create(PartsPage);
  PartsNote.Parent := PartsPage.Surface;
  PartsNote.Left := 0;
  PartsNote.Top := PartList.Height + ScaleY(6);
  PartsNote.Width := PartsPage.SurfaceWidth;
  PartsNote.WordWrap := True;
  PartsNote.AutoSize := True;
  PartsNote.Caption := '';

  { "Only I use this Windows login": off unless ticked. The note beside it says to leave it off on a shared login. }
  SoloPage := CreateCustomPage(PartsPage.ID, 'Who uses this Windows login?',
    'Crosstalk can keep your chat history when you switch between your own Claude accounts.');
  SoloBox := TNewCheckBox.Create(SoloPage);
  SoloBox.Parent := SoloPage.Surface;
  SoloBox.Left := 0;
  SoloBox.Top := 0;
  SoloBox.Width := SoloPage.SurfaceWidth;
  SoloBox.Height := ScaleY(18);
  SoloBox.Caption := 'Only I use this Windows login';
  SoloBox.Checked := False;
  SoloNote := TNewStaticText.Create(SoloPage);
  SoloNote.Parent := SoloPage.Surface;
  SoloNote.Left := 0;
  SoloNote.Top := ScaleY(28);
  SoloNote.Width := SoloPage.SurfaceWidth;
  SoloNote.WordWrap := True;
  SoloNote.AutoSize := True;
  SoloNote.Caption := IniGet('shared_login_note', '') + #13#10#13#10 +
    'If you tick this, after the install Setup asks you to confirm your own Claude login emails. Leave it unticked on a login other people use.';

  { How EUTONOS keeps itself up to date. The default is the third choice: it downloads and checks a newer release in the
    background, then asks before installing. Nothing is installed without a click. }
  UpdatePage := CreateCustomPage(SoloPage.ID, 'Keeping EUTONOS up to date',
    'Choose how EUTONOS finds newer releases. You can change this later in the EUTONOS dashboard, Features, Updates.');
  UpdateBox := TNewComboBox.Create(UpdatePage);
  UpdateBox.Parent := UpdatePage.Surface;
  UpdateBox.Left := 0;
  UpdateBox.Top := 0;
  UpdateBox.Width := UpdatePage.SurfaceWidth;
  UpdateBox.Style := csDropDownList;
  UpdateBox.Items.Add('Manual (nothing runs on its own)');
  UpdateBox.Items.Add('Check and notify');
  UpdateBox.Items.Add('Check, download, then ask before installing');
  UpdateBox.ItemIndex := 2;
  UpdateNote := TNewStaticText.Create(UpdatePage);
  UpdateNote.Parent := UpdatePage.Surface;
  UpdateNote.Left := 0;
  UpdateNote.Top := ScaleY(36);
  UpdateNote.Width := UpdatePage.SurfaceWidth;
  UpdateNote.WordWrap := True;
  UpdateNote.AutoSize := True;
  UpdateNote.Caption := 'Manual: nothing is checked unless you run the update yourself.' + #13#10#13#10 +
    'Check and notify: about once a day the EUTONOS companion looks for a newer release and tells you when one is available.' + #13#10#13#10 +
    'Check, download, then ask: the same daily check also downloads and verifies the release in the background, then asks ' +
    'whether to install it. EUTONOS never installs an update without your click.' + #13#10#13#10 +
    'The daily check runs inside the EUTONOS companion.';

  { Consent page. }
  ConsentPage := CreateCustomPage(UpdatePage.ID, 'Review and agree',
    'This is exactly what Setup will do on this PC. Nothing is changed until you agree.');
  Wrapped := TNewStaticText.Create(ConsentPage);
  Wrapped.Parent := ConsentPage.Surface;
  Wrapped.Caption := 'What this Setup will do:';
  Wrapped.Font.Style := [fsBold];
  Wrapped.AutoSize := True;
  ChangeMemo := TNewMemo.Create(ConsentPage);
  ChangeMemo.Parent := ConsentPage.Surface;
  ChangeMemo.Top := ScaleY(20);
  ChangeMemo.Width := ConsentPage.SurfaceWidth;
  ChangeMemo.Height := ScaleY(84);
  ChangeMemo.ReadOnly := True;
  ChangeMemo.ScrollBars := ssVertical;
  Wrapped := TNewStaticText.Create(ConsentPage);
  Wrapped.Parent := ConsentPage.Surface;
  Wrapped.Caption := 'The full consent text (this exact text is what gets recorded):';
  Wrapped.Top := ScaleY(112);
  Wrapped.AutoSize := True;
  TextMemo := TNewMemo.Create(ConsentPage);
  TextMemo.Parent := ConsentPage.Surface;
  TextMemo.Top := ScaleY(132);
  TextMemo.Width := ConsentPage.SurfaceWidth;
  TextMemo.Height := ConsentPage.SurfaceHeight - ScaleY(132) - ScaleY(30);
  TextMemo.ReadOnly := True;
  TextMemo.ScrollBars := ssVertical;
  AgreeBox := TNewCheckBox.Create(ConsentPage);
  AgreeBox.Parent := ConsentPage.Surface;
  AgreeBox.Top := ConsentPage.SurfaceHeight - ScaleY(20);
  AgreeBox.Width := ConsentPage.SurfaceWidth;
  AgreeBox.Height := ScaleY(18);
  AgreeBox.Caption := 'I agree';
  AgreeBox.OnClick := @AgreeClick;

  { After the install (only for "Only I use this Windows login"): confirm your own Claude login emails. }
  LinkedPage := CreateCustomPage(wpInstalling, 'Your Claude logins',
    'Confirm which Claude logins are yours, so Crosstalk can keep your chat history across a switch between them.');
  LinkedHint := TNewStaticText.Create(LinkedPage);
  LinkedHint.Parent := LinkedPage.Surface;
  LinkedHint.Left := 0;
  LinkedHint.Top := 0;
  LinkedHint.Width := LinkedPage.SurfaceWidth;
  LinkedHint.WordWrap := True;
  LinkedHint.AutoSize := True;
  LinkedHint.Caption := '';
  LinkedMemo := TNewMemo.Create(LinkedPage);
  LinkedMemo.Parent := LinkedPage.Surface;
  LinkedMemo.Top := ScaleY(48);
  LinkedMemo.Width := LinkedPage.SurfaceWidth;
  LinkedMemo.Height := LinkedPage.SurfaceHeight - ScaleY(48);
  LinkedMemo.ScrollBars := ssVertical;
end;

function NextButtonClick(CurPageID: Integer): Boolean;
begin
  Result := True;
  if CurPageID = wpSelectDir then
  begin
    RepairMode := False; { Going Back and choosing a different home starts a new choice. }
    ChosenHome := RemoveBackslashUnlessRoot(WizardDirValue);
    if not GoodHome(ChosenHome) then
    begin
      MsgBox('Please choose a folder on a drive, such as ' + RecommendedDir + '.', mbError, MB_OK);
      Result := False;
    end else if not Inspect(ChosenHome) then
    begin
      MsgBox('Setup could not look at this PC. Its own Python would not start; Setup may be damaged.', mbError, MB_OK);
      Result := False;
    end else if (IniGet('existing_install', '0') = '1') or (IniGet('pending', '0') = '1') then
    begin
      RepairMode := MsgBox('This folder has an active or unfinished EUTONOS install.' + #13#10#13#10 +
        'Repair only Setup''s bundled Python, bootstrap and wheel? Close EUTONOS apps first. This does not replay install, ' +
        'change app settings, accept consent, resume or uninstall. After repair, Setup shows commands for those choices.',
        mbConfirmation, MB_YESNO) = IDYES;
      Result := RepairMode;
    end else if (not GitOk) and (BuiltFor <> ChosenHome) then
    begin
      { Git is not a Setup requirement: only the parts that use it are unavailable, and the parts page says which. }
      MsgBox(IniGet('git_note', 'Git was not found, so the parts that use it are unavailable.') + #13#10#13#10 +
        GitGuidance + #13#10#13#10 + 'You can go on now and install Git later; run Setup again afterwards to add them.',
        mbInformation, MB_OK);
    end;
  end else if CurPageID = ConsentPage.ID then
  begin
    if not AgreeBox.Checked then
    begin
      MsgBox('Tick I agree to continue, or go back to change your choices.', mbInformation, MB_OK);
      Result := False;
    end;
  end else if CurPageID = LinkedPage.ID then
    ApplyLinked;
end;

procedure CurPageChanged(CurPageID: Integer);
begin
  DashButton.Visible := (CurPageID = wpFinished) and (not Failed) and (not RepairMode);
  if DashButton.Visible then
  begin
    DashButton.Caption := 'Open dashboard';
    DashButton.Enabled := True;
  end;
  if CurPageID = PartsPage.ID then
  begin
    if BuiltFor <> ChosenHome then
      BuildPartsList;
  end else if CurPageID = ConsentPage.ID then
  begin
    AgreeBox.Checked := False;
    if not PrepareConsent then
    begin
      MsgBox('Setup could not prepare the consent text. Go back and try again.', mbError, MB_OK);
      WizardForm.NextButton.Enabled := False;
    end else
      WizardForm.NextButton.Enabled := False;
    WizardForm.NextButton.Caption := SetupMessage(msgButtonInstall);
  end else if CurPageID = LinkedPage.ID then
  begin
    PrepareLinked;
    WizardForm.NextButton.Caption := SetupMessage(msgButtonNext);
  end else if CurPageID = wpFinished then
  begin
    if RepairMode then
    begin
      WizardForm.FinishedHeadingLabel.Caption := 'Setup support-file repair';
      if Failed then
        WizardForm.FinishedLabel.Caption := 'Repair could not finish. Close EUTONOS apps and retry. The install recovery state was kept.'
      else
        WizardForm.FinishedLabel.Caption := 'Setup support files were repaired. No install phase was replayed.' + #13#10#13#10 + RecoveryCommands;
    end else if Failed then
    begin
      WizardForm.FinishedHeadingLabel.Caption := 'EUTONOS was not installed';
      WizardForm.FinishedLabel.Caption := 'Setup could not finish. Cleanup may still need attention; inspect the recorded status before retrying. ' +
        'Setup''s own files remain in ' + ChosenHome + ' so recovery can run.' + #13#10#13#10 +
        RecoveryCommands + #13#10#13#10 + TailText(8);
    end else
    begin
      WizardForm.FinishedHeadingLabel.Caption := 'EUTONOS is installed';
      WizardForm.FinishedLabel.Caption := 'Restart Claude Desktop, Codex and Hermes (close each one completely, then open it again) ' +
        'so they pick up EUTONOS. Chats that were already open keep working the old way until you do.' + #13#10#13#10 +
        'To remove EUTONOS later, use Windows Settings, Installed apps. That undoes exactly what Setup added and keeps ' +
        'your backups and project data.';
      if UpdateReport <> '' then
        WizardForm.FinishedLabel.Caption := WizardForm.FinishedLabel.Caption + #13#10#13#10 + UpdateReport;
      if SettingsReport <> '' then
        WizardForm.FinishedLabel.Caption := WizardForm.FinishedLabel.Caption + #13#10#13#10 + SettingsReport;
    end;
  end else
    WizardForm.NextButton.Caption := SetupMessage(msgButtonNext);
end;

function PrepareToInstall(var NeedsRestart: Boolean): String;
begin
  Result := '';
  if RepairMode and not RunWizardTool('repair-support --home ' + Q(ChosenHome) + ' --python-dir ' + Q(ExtractFileDir(TempPython)) +
    ' --bootstrap ' + Q(TempBoot) + ' --wheel ' + Q(TempWheel)) then
    Result := 'Setup support repair preflight failed. An EUTONOS lifecycle operation may be running, or a support path is unsafe. Nothing was copied.';
end;

procedure CurStepChanged(CurStep: TSetupStep);
var
  Py, Boot, Wheel, Args: String;
  Code: Integer;
begin
  if CurStep <> ssPostInstall then
    Exit;
  if RepairMode then
    Exit; { Inno copied and recorded only its declared support files. No engine replay. }
  Py := ChosenHome + '\python\python.exe';
  Boot := ChosenHome + '\setup\bootstrap.py';
  Wheel := FindWheel(ChosenHome + '\setup');
  Tail := '';
  WizardForm.StatusLabel.Caption := 'Recording your agreement';
  WizardForm.StatusLabel.Update;
  if not Exec(Py, '-I -B ' + Q(Boot) + ' ' + Q(Wheel) + ' wizard consent-accept --template ' +
      Q(ExpandConstant('{tmp}\consent-template.json')) + ' --out ' + Q(ExpandConstant('{tmp}\consent.json')),
      ExpandConstant('{tmp}'), SW_HIDE, ewWaitUntilTerminated, Code) or (Code <> 0) then
  begin
    Failed := True;
    Tail := 'Setup could not write your agreement.';
    Exit;
  end;
  Args := '-I -B ' + Q(Boot) + ' ' + Q(Wheel) + ' engine --home ' + Q(ChosenHome) + ' install --wheel ' + Q(Wheel) +
    ' --for-this-user --consent-file ' + Q(ExpandConstant('{tmp}\consent.json')) + SharedArgs + ' --progress';
  WizardForm.StatusLabel.Caption := 'Starting the EUTONOS installer';
  WizardForm.StatusLabel.Update;
  try
    if (not ExecAndLogOutput(Py, Args, ExpandConstant('{tmp}'), SW_HIDE, ewWaitUntilTerminated, Code, @LogLine)) or (Code <> 0) then
      Failed := True;
  except
    Failed := True;
    Tail := Tail + GetExceptionMessage;
  end;
  if Failed then
    Log('engine install failed, exit ' + IntToStr(Code))
  else
  begin
    WizardForm.ProgressGauge.Position := WizardForm.ProgressGauge.Max;
    WizardForm.StatusLabel.Caption := 'Saving your update choice';
    WizardForm.StatusLabel.Update;
    ApplyUpdateMode;
    WizardForm.StatusLabel.Caption := 'EUTONOS is installed';
  end;
end;

{ Uninstall: the engine reverses what it recorded first. If it cannot, stop: nothing is deleted. }
procedure CurUninstallStepChanged(CurUninstallStep: TUninstallStep);
var
  Home, Py, Boot, Wheel: String;
  Code: Integer;
  Ran: Boolean;
begin
  if CurUninstallStep = usUninstall then
  begin
    Home := ExpandConstant('{app}');
    Py := Home + '\python\python.exe';
    Boot := Home + '\setup\bootstrap.py';
    Wheel := FindWheel(Home + '\setup');
    Tail := '';
    if (not FileExists(Py)) or (not FileExists(Boot)) or (Wheel = '') then
    begin
      SuppressibleMsgBox('EUTONOS could not be uninstalled because its Setup files are missing from ' + Home +
        '. Nothing was deleted. Run Setup again to repair them, then uninstall.', mbError, MB_OK, IDOK);
      Abort;
    end;
    Ran := False;
    try
      Ran := ExecAndLogOutput(Py, '-I -B ' + Q(Boot) + ' --installed-engine ' + Q(Home) + ' ' + Q(Wheel) + ' engine --home ' +
        Q(Home) + ' uninstall --keep-projects', ExpandConstant('{tmp}'), SW_HIDE, ewWaitUntilTerminated, Code, @UnLogLine);
    except
      Tail := Tail + GetExceptionMessage;
    end;
    if (not Ran) or (Code <> 0) then
    begin
      SuppressibleMsgBox('The EUTONOS uninstaller reported a problem, so nothing was deleted and EUTONOS is still installed.' + #13#10#13#10 +
        TailText(10) + #13#10#13#10 + 'Fix the problem and uninstall again.', mbError, MB_OK, IDOK);
      Abort;
    end;
  end else if CurUninstallStep = usDone then
    SuppressibleMsgBox('EUTONOS was removed. Your backups, hosts, RAM and project data were kept in ' + ExpandConstant('{app}') +
      '. Restart Claude Desktop, Codex and Hermes so they stop using EUTONOS.', mbInformation, MB_OK, IDOK);
end;
