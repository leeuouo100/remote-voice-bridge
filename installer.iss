; Inno Setup 脚本 — 把 PyInstaller 输出打包成 Setup.exe
; 构建： iscc installer.iss   （需先跑 pyinstaller 产出 dist/RemoteVoiceBridge/）

#define MyAppName "Remote Voice Bridge"
#define MyAppVersion "1.0.0"
#define MyAppPublisher "remote-voice-bridge"
#define MyAppURL "https://github.com/leeuouo100/remote-voice-bridge"
#define MyAppExeName "RemoteVoiceBridge.exe"

[Setup]
AppId={{A7E3C1D4-5B82-4F60-9E1A-2C8D7B6F4A31}
AppName={#MyAppName}
AppVersion={#MyAppVersion}
AppVerName={#MyAppName} {#MyAppVersion}
AppPublisher={#MyAppPublisher}
AppPublisherURL={#MyAppURL}
AppSupportURL={#MyAppURL}
AppUpdatesURL={#MyAppURL}
DefaultDirName={autopf}\RemoteVoiceBridge
DefaultGroupName={#MyAppName}
DisableProgramGroupPage=yes
LicenseFile=LICENSE
OutputDir=installer
OutputBaseFilename=RemoteVoiceBridge-Setup-{#MyAppVersion}
SetupIconFile=app.ico
UninstallDisplayIcon={app}\{#MyAppExeName}
Compression=lzma2/max
SolidCompression=yes
WizardStyle=modern
ArchitecturesAllowed=x64
ArchitecturesInstallIn64BitMode=x64

[Languages]
; 语言包随仓库提供：GitHub runner 自带�� Inno Setup 没有中文包，
; 直接引用 compiler:Languages\ChineseSimplified.isl 会编译失败。
Name: "chinesesimplified"; MessagesFile: "Languages\ChineseSimplified.isl"
Name: "english"; MessagesFile: "compiler:Default.isl"

[Tasks]
Name: "desktopicon"; Description: "创建桌面快捷方式"; GroupDescription: "附加图标:"; Flags: unchecked

[Files]
Source: "dist\RemoteVoiceBridge\{#MyAppExeName}"; DestDir: "{app}"; Flags: ignoreversion
Source: "dist\RemoteVoiceBridge\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
Name: "{group}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"
Name: "{group}\卸载 {#MyAppName}"; Filename: "{uninstallexe}"
Name: "{autodesktop}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"; Tasks: desktopicon

[Run]
Filename: "{app}\{#MyAppExeName}"; Description: "立即启动 {#MyAppName}"; Flags: postinstall nowait skipifsilent
