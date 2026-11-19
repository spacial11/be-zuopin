$ErrorActionPreference = 'Stop'
$demoDir = (Resolve-Path (Join-Path $PSScriptRoot '..\..\演示')).Path
$compiler = Join-Path $env:WINDIR 'Microsoft.NET\Framework64\v4.0.30319\csc.exe'
$compilerArgs = @(
    '/nologo', '/target:winexe', '/platform:x64', '/optimize+',
    "/out:$demoDir\be队作品.exe",
    "/win32icon:$PSScriptRoot\app.ico",
    '/reference:System.Windows.Forms.dll', '/reference:System.Drawing.dll',
    "/reference:$demoDir\Microsoft.Web.WebView2.Core.dll",
    "/reference:$demoDir\Microsoft.Web.WebView2.WinForms.dll",
    "$PSScriptRoot\LauncherWebView2.cs"
)
& $compiler @compilerArgs
if ($LASTEXITCODE -ne 0) { throw "C# 编译失败 ($LASTEXITCODE)" }
