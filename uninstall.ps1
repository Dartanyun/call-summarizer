# Удаление «Саммари созвонов». Записи и настройки остаются, если не указать -Purge.
param([switch]$Purge)
$ErrorActionPreference = "Continue"
[Console]::OutputEncoding = [Text.Encoding]::UTF8

$Root = Join-Path $env:LOCALAPPDATA "CallSummarizer"
$Venv = Join-Path $Root "venv"

# закрываем запущенную программу, иначе её файлы не удалить
Get-Process -ErrorAction SilentlyContinue | Where-Object { $_.Path -and $_.Path.StartsWith($Venv, "OrdinalIgnoreCase") } | Stop-Process -Force

Remove-Item -Recurse -Force -ErrorAction SilentlyContinue (Join-Path $Root "venv"), (Join-Path $Root "python"), (Join-Path $Root "uv"), (Join-Path $Root "call-summarizer.ico")
Remove-Item -Force -ErrorAction SilentlyContinue (Join-Path ([Environment]::GetFolderPath("Programs")) "Саммари созвонов.lnk")
Remove-ItemProperty -Path "HKCU:\Software\Microsoft\Windows\CurrentVersion\Run" -Name "CallSummarizer" -ErrorAction SilentlyContinue

if ($Purge) {
    Remove-Item -Recurse -Force -ErrorAction SilentlyContinue $Root, (Join-Path $env:APPDATA "CallSummarizer")
    Write-Host "Программа, записи и настройки удалены."
} else {
    Write-Host "Программа удалена. Записи остались в $Root\calls"
    Write-Host "(чтобы удалить и их: uninstall.cmd -Purge)"
}
