#ifndef VNTTS_BUNDLE_DIR
  #error VNTTS_BUNDLE_DIR must point to the PyInstaller one-folder bundle
#endif
#ifndef VNTTS_OUTPUT_DIR
  #define VNTTS_OUTPUT_DIR "..\\..\\dist"
#endif
#ifndef VNTTS_VERSION
  #define VNTTS_VERSION "0.1.0"
#endif
#ifndef VNTTS_FILE_VERSION
  #define VNTTS_FILE_VERSION "0.1.0.0"
#endif

#define AppName "Visual Novel Text to Speech"
#define AppPublisher "Visual Novel Text to Speech contributors"
#define AppExecutable "VisualNovelTextToSpeech.exe"

[Setup]
AppId={{F18AB9A5-025B-4B49-919B-41B7BCE64D3F}
AppName={#AppName}
AppVersion={#VNTTS_VERSION}
AppPublisher={#AppPublisher}
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
CloseApplications=yes
Compression=lzma2/max
DefaultDirName={localappdata}\Programs\VisualNovelTextToSpeech
DefaultGroupName={#AppName}
DisableProgramGroupPage=yes
OutputBaseFilename=VisualNovelTextToSpeech-{#VNTTS_VERSION}-windows-x64-setup
OutputDir={#VNTTS_OUTPUT_DIR}
PrivilegesRequired=lowest
PrivilegesRequiredOverridesAllowed=dialog
SetupLogging=yes
SolidCompression=yes
UninstallDisplayIcon={app}\{#AppExecutable}
UninstallDisplayName={#AppName}
VersionInfoCompany={#AppPublisher}
VersionInfoDescription={#AppName} installer
VersionInfoProductName={#AppName}
VersionInfoProductVersion={#VNTTS_VERSION}
VersionInfoVersion={#VNTTS_FILE_VERSION}
WizardStyle=modern

[Languages]
Name: "english"; MessagesFile: "compiler:Default.isl"

[Tasks]
Name: "startup"; Description: "Start {#AppName} when I sign in"; GroupDescription: "Additional shortcuts:"; Flags: unchecked

[Files]
Source: "{#VNTTS_BUNDLE_DIR}\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
Name: "{group}\{#AppName}"; Filename: "{app}\{#AppExecutable}"; WorkingDir: "{app}"
Name: "{userstartup}\{#AppName}"; Filename: "{app}\{#AppExecutable}"; WorkingDir: "{app}"; Tasks: startup

[Run]
Filename: "{app}\{#AppExecutable}"; Description: "Launch {#AppName}"; Flags: nowait postinstall skipifsilent
