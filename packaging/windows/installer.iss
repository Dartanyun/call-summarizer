; Установщик для Windows (Inno Setup 6). Собирается из packaging/build.py:
;   ISCC /DAppVersion=1.2.0 /DSourceDir=dist\CallSummarizer /DOutputDir=dist /DIconFile=...ico installer.iss
; Ставит программу в профиль текущего пользователя (%LOCALAPPDATA%\Programs) — права
; администратора не нужны. Записи и настройки лежат отдельно и при удалении остаются.

#ifndef AppVersion
  #error Передайте версию: /DAppVersion=X.Y.Z
#endif

[Setup]
; постоянный код программы: по нему Windows узнаёт обновление, а не вторую копию
AppId={{4CC73186-B113-40A1-992D-7C8005E50B84}
AppName=Саммари созвонов
AppVersion={#AppVersion}
AppVerName=Саммари созвонов {#AppVersion}
AppPublisher=Dartanyun
AppPublisherURL=https://github.com/Dartanyun/call-summarizer
AppSupportURL=https://github.com/Dartanyun/call-summarizer/issues
AppUpdatesURL=https://github.com/Dartanyun/call-summarizer/releases
VersionInfoVersion={#AppVersion}
VersionInfoDescription=Установщик «Саммари созвонов»
PrivilegesRequired=lowest
DefaultDirName={autopf}\CallSummarizer
DisableProgramGroupPage=yes
DisableDirPage=auto
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
MinVersion=10.0
SetupIconFile={#IconFile}
UninstallDisplayIcon={app}\CallSummarizer.exe
UninstallDisplayName=Саммари созвонов
OutputDir={#OutputDir}
OutputBaseFilename=CallSummarizer-{#AppVersion}-windows-x64-setup
Compression=lzma2/max
SolidCompression=yes
LZMANumBlockThreads=4
WizardStyle=modern
; перед обновлением или удалением закрыть работающую программу
CloseApplications=force
RestartApplications=no

[Languages]
Name: "ru"; MessagesFile: "compiler:Languages\Russian.isl"
Name: "en"; MessagesFile: "compiler:Default.isl"

[Tasks]
Name: "desktopicon"; Description: "{cm:CreateDesktopIcon}"; GroupDescription: "{cm:AdditionalIcons}"; Flags: unchecked

[InstallDelete]
; файлы прошлой версии: библиотеки меняются от версии к версии
Type: filesandordirs; Name: "{app}\_internal"

[Files]
Source: "{#SourceDir}\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
; тот же AppUserModelID, что задаёт программа: уведомления подписаны её именем и значком
Name: "{autoprograms}\Саммари созвонов"; Filename: "{app}\CallSummarizer.exe"; AppUserModelID: "CallSummarizer.App"; Comment: "Записывает созвоны, распознаёт речь и делает краткое саммари"
Name: "{autodesktop}\Саммари созвонов"; Filename: "{app}\CallSummarizer.exe"; AppUserModelID: "CallSummarizer.App"; Tasks: desktopicon

[Registry]
; автозапуск программа включает сама (в настройках); при удалении — убрать
Root: HKCU; Subkey: "Software\Microsoft\Windows\CurrentVersion\Run"; ValueType: none; ValueName: "CallSummarizer"; Flags: uninsdeletevalue dontcreatekey

[Run]
Filename: "{app}\CallSummarizer.exe"; Description: "{cm:LaunchProgram,Саммари созвонов}"; Flags: nowait postinstall skipifsilent
; обновление из самой программы идёт тихо (/SILENT) — после него программа запускается снова
Filename: "{app}\CallSummarizer.exe"; Flags: nowait skipifnotsilent
