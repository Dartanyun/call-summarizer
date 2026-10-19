# Установка «Саммари созвонов» для текущего пользователя Windows — без прав администратора.
# Всё ставится в %LOCALAPPDATA%\CallSummarizer, ярлык — в ваше меню «Пуск».
$ErrorActionPreference = "Stop"
$ProgressPreference = "SilentlyContinue"   # иначе Invoke-WebRequest качает в разы медленнее
[Console]::OutputEncoding = [Text.Encoding]::UTF8

$Src  = $PSScriptRoot
$Root = Join-Path $env:LOCALAPPDATA "CallSummarizer"
$Venv = Join-Path $Root "venv"
$Py   = Join-Path $Venv "Scripts\python.exe"
New-Item -ItemType Directory -Force -Path $Root | Out-Null

# 1. uv — менеджер Python: ставит Python и библиотеки в папку пользователя
$uv = (Get-Command uv -ErrorAction SilentlyContinue).Source
if (-not $uv) {
    $uvDir = Join-Path $Root "uv"
    $uv = Join-Path $uvDir "uv.exe"
    if (-not (Test-Path $uv)) {
        Write-Host "→ Скачиваю uv (~20 МБ)…"
        $zip = Join-Path $env:TEMP "callsummarizer-uv.zip"
        Invoke-WebRequest "https://github.com/astral-sh/uv/releases/latest/download/uv-x86_64-pc-windows-msvc.zip" -OutFile $zip
        Expand-Archive $zip -DestinationPath $uvDir -Force
        Remove-Item $zip
    }
}

# 2. Python 3.12 и зависимости — внутри нашей папки, системный Python не нужен и не трогается
$env:UV_PYTHON_INSTALL_DIR = Join-Path $Root "python"
if (-not (Test-Path $Py)) {
    Write-Host "→ Ставлю Python 3.12…"
    & $uv venv $Venv --python 3.12 --python-preference only-managed
    if ($LASTEXITCODE) { throw "Не удалось создать окружение Python" }
}
Write-Host "→ Ставлю программу и библиотеки (~1,3 ГБ, несколько минут)…"
& $uv pip install --python $Py --reinstall-package call-summarizer $Src
if ($LASTEXITCODE) { throw "Не удалось установить библиотеки" }

# 3. Иконка и ярлык в меню «Пуск» (личная папка меню, права администратора не нужны)
$Ico = Join-Path $Root "call-summarizer.ico"
& $Py -m callsum.make_icon $Ico 256
$Exe = Join-Path $Venv "Scripts\call-summarizer.exe"
if (-not (Test-Path $Exe)) { $Exe = Join-Path $Venv "Scripts\pythonw.exe"; $LnkArgs = "-m callsum" } else { $LnkArgs = "" }
$Programs = [Environment]::GetFolderPath("Programs")
$Shell = New-Object -ComObject WScript.Shell
# WScript.Shell сохраняет ярлык в кодировке системы: на нерусской Windows кириллица в имени
# превратилась бы в «???». Сохраняем под латинским именем и переименовываем средствами PowerShell.
$LnkTmp = Join-Path $Programs "CallSummarizer.lnk"
$LnkFinal = Join-Path $Programs "Саммари созвонов.lnk"
$Lnk = $Shell.CreateShortcut($LnkTmp)
$Lnk.TargetPath = $Exe
$Lnk.Arguments = $LnkArgs
$Lnk.WorkingDirectory = $Root
$Lnk.IconLocation = "$Ico,0"
$Lnk.Description = "Записывает созвоны, распознаёт речь и делает краткое саммари"
$Lnk.Save()
Move-Item -Force -LiteralPath $LnkTmp -Destination $LnkFinal

$Version = & $Py -c "import callsum; print(callsum.__version__)"
Write-Host ""
Write-Host "Готово, установлена версия $Version. Запуск: меню «Пуск» → «Саммари созвонов»."
Write-Host "Записи будут храниться в $Root\calls"
Write-Host "Первое распознавание скачает модель Whisper (~1,6 ГБ)."
