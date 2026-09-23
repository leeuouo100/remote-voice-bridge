; Inno Setup 脚本 — 把 PyInstaller 输出打包成 Setup.exe
; 构建： iscc installer.iss   （需先跑 pyinstaller 产出 dist/RemoteVoiceBridge/）
;
; 版本号有两个来源，刻意这样设计：
;   · 本地直接编译时用下面这个默认值，必须和 config.py 的 APP_VERSION 一致
;     （tools/check_version.py 会校验，CI 也会校验）
;   · CI 打 tag 构建时由 workflow 用 /DMyAppVersion=x.y.z 覆盖，
;     tag 就是唯一真源 —— 避免"tag 打的是 1.0.3、装出来的还是 1.0.2"这种事故。

#ifndef MyAppVersion
  #define MyAppVersion "1.0.16"
#endif

#define MyAppName "Remote Voice Bridge"
#define MyAppPublisher "remote-voice-bridge"
#define MyAppURL "https://github.com/leeuouo100/remote-voice-bridge"
#define MyAppExeName "RemoteVoiceBridge.exe"
; 诊断工具（spec 里打出来的第二个 exe）。
; 它必须随包装上：用户机器上没有 Python，仓库里的 diag-remote.bat 跑不起来。
; 它同时承担「蓝牙配对自检 / 修复」（--fix-pairing）。
#define MyDiagExeName "RemoteVoiceBridgeDiag.exe"
; 配对修复的入口脚本。它自己会判断"旁边是 exe 还是 .py"，两个环境通吃，
; 所以安装目录里也得有一份 —— 出问题时用户至少有个能双击的东西。
#define MyFixBatName "修复蓝牙配对.bat"

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
; 配对修复入口（内容会自己认出"旁边是 exe"并走 exe，不需要 Python）
Source: "{#MyFixBatName}"; DestDir: "{app}"; Flags: ignoreversion

[Icons]
Name: "{group}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"
Name: "{group}\遥控器诊断（按键没反应时跑这个）"; Filename: "{app}\{#MyDiagExeName}"
; 换过 USB 口之后「程序说没连接、Windows 说已配对」——开始菜单直接修，不用找日志
Name: "{group}\修复蓝牙配对（换过USB口/连不上时跑这个）"; Filename: "{app}\{#MyDiagExeName}"; Parameters: "--fix-pairing"
Name: "{group}\卸载 {#MyAppName}"; Filename: "{uninstallexe}"
Name: "{autodesktop}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"; Tasks: desktopicon

[Run]
Filename: "{app}\{#MyAppExeName}"; Description: "立即启动 {#MyAppName}"; Flags: postinstall nowait skipifsilent
