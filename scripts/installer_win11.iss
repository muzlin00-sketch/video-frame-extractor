[Setup]
AppId={{A8B35B29-5E92-4758-A293-B8B2235BA261}
AppName=智能视频分析工具
AppVersion=1.0.2
AppPublisher=VideoAnalyzer Team
DefaultDirName={localappdata}\VideoAnalyzer
DefaultGroupName=智能视频分析工具
DisableProgramGroupPage=yes
OutputDir=dist
OutputBaseFilename=智能视频分析工具-安装包
Compression=lzma
SolidCompression=yes
WizardStyle=modern
PrivilegesRequired=lowest
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
UninstallDisplayIcon={app}\VideoAnalyzer.exe
SetupIconFile=C:\Users\linzhiqiang\Pictures\icon.ico

[Languages]
Name: "chinesesimp"; MessagesFile: "compiler:Default.isl"

[Tasks]
Name: "desktopicon"; Description: "创建桌面快捷方式"; GroupDescription: "附加任务"; Flags: unchecked

[Files]
Source: "dist\VideoAnalyzer\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs
Source: "C:\Users\linzhiqiang\Pictures\icon.ico"; DestDir: "{app}"; DestName: "app_icon.ico"; Flags: ignoreversion

[Icons]
Name: "{group}\智能视频分析工具"; Filename: "{app}\VideoAnalyzer.exe"; IconFilename: "{app}\app_icon.ico"
Name: "{group}\卸载 智能视频分析工具"; Filename: "{uninstallexe}"
Name: "{autodesktop}\智能视频分析工具"; Filename: "{app}\VideoAnalyzer.exe"; IconFilename: "{app}\app_icon.ico"; Tasks: desktopicon

[Run]
Filename: "{app}\VideoAnalyzer.exe"; Description: "启动 智能视频分析工具"; Flags: nowait postinstall skipifsilent
