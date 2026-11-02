param([ValidateSet('qq','netease')][string]$Provider = 'netease', [string]$StatusPath, [switch]$CheckUi)
$ErrorActionPreference = 'Stop'
if (-not $StatusPath) {
    $stateFolder = Join-Path ([Environment]::GetFolderPath('LocalApplicationData')) 'mail-export'
    [void][IO.Directory]::CreateDirectory($stateFolder)
    $StatusPath = Join-Path $stateFolder 'setup-status.json'
}
Add-Type -AssemblyName System.Windows.Forms
Add-Type -AssemblyName System.Drawing
[System.Windows.Forms.Application]::EnableVisualStyles()

$form = New-Object System.Windows.Forms.Form
$form.Text = '邮箱 IMAP 本地配置'
$form.ClientSize = New-Object System.Drawing.Size(540,310)
$form.StartPosition = 'CenterScreen'
$form.FormBorderStyle = 'FixedDialog'
$form.MaximizeBox = $false
$form.MinimizeBox = $false
$form.TopMost = $true
$intro = New-Object System.Windows.Forms.Label
$intro.Text = "仅保存到当前 Windows 用户环境变量，不上传到聊天。`r`n授权码请使用邮箱客户端授权码，不是网页登录密码。"
$intro.Location = New-Object System.Drawing.Point(20,18)
$intro.Size = New-Object System.Drawing.Size(500,45)
$form.Controls.Add($intro)
$providerBox = New-Object System.Windows.Forms.ComboBox
$providerBox.DropDownStyle = 'DropDownList'
$providerBox.Location = New-Object System.Drawing.Point(140,70)
$providerBox.Size = New-Object System.Drawing.Size(370,25)
[void]$providerBox.Items.Add('QQ / Foxmail')
[void]$providerBox.Items.Add('网易 163 / 126 / yeah.net')
$providerBox.SelectedIndex = $(if ($Provider -eq 'qq') { 0 } else { 1 })
$form.Controls.Add($providerBox)
$accountLabel = New-Object System.Windows.Forms.Label
$accountLabel.Text = '完整邮箱账号'
$accountLabel.Location = New-Object System.Drawing.Point(20,120)
$accountLabel.Size = New-Object System.Drawing.Size(115,25)
$form.Controls.Add($accountLabel)
$accountBox = New-Object System.Windows.Forms.TextBox
$accountBox.Location = New-Object System.Drawing.Point(140,117)
$accountBox.Size = New-Object System.Drawing.Size(370,25)
$form.Controls.Add($accountBox)
$codeLabel = New-Object System.Windows.Forms.Label
$codeLabel.Text = '客户端授权码'
$codeLabel.Location = New-Object System.Drawing.Point(20,165)
$codeLabel.Size = New-Object System.Drawing.Size(115,25)
$form.Controls.Add($codeLabel)
$codeBox = New-Object System.Windows.Forms.TextBox
$codeBox.Location = New-Object System.Drawing.Point(140,162)
$codeBox.Size = New-Object System.Drawing.Size(370,25)
$codeBox.UseSystemPasswordChar = $true
$form.Controls.Add($codeBox)
$hint = New-Object System.Windows.Forms.Label
$hint.Text = '保存到当前用户。首次配置保存后可继续原任务；更新旧配置后重启 Domi。'
$hint.Location = New-Object System.Drawing.Point(20,205)
$hint.Size = New-Object System.Drawing.Size(500,30)
$form.Controls.Add($hint)
$save = New-Object System.Windows.Forms.Button
$save.Text = '保存配置'
$save.Location = New-Object System.Drawing.Point(295,260)
$save.Size = New-Object System.Drawing.Size(100,30)
$form.Controls.Add($save)
$cancel = New-Object System.Windows.Forms.Button
$cancel.Text = '取消'
$cancel.Location = New-Object System.Drawing.Point(410,260)
$cancel.Size = New-Object System.Drawing.Size(100,30)
$cancel.DialogResult = [System.Windows.Forms.DialogResult]::Cancel
$form.Controls.Add($cancel)
$form.CancelButton = $cancel
$form.AcceptButton = $save
$save.Add_Click({
    $mailAccount = $accountBox.Text.Trim()
    $mailCode = $codeBox.Text.Trim()
    if (($providerBox.SelectedIndex -eq 0 -and $mailAccount -notmatch '^[^\s@]+@(qq\.com|foxmail\.com)$') -or ($providerBox.SelectedIndex -eq 1 -and $mailAccount -notmatch '^[^\s@]+@(163\.com|126\.com|yeah\.net)$') -or [string]::IsNullOrWhiteSpace($mailCode) -or $mailCode -match '[\x00-\x1f\x7f]') {
        [void][System.Windows.Forms.MessageBox]::Show('请填写匹配所选服务商的完整邮箱账号和有效的客户端授权码。','检查配置')
        return
    }
    try {
        $prefix = $(if ($providerBox.SelectedIndex -eq 0) { 'QQ_EMAIL' } else { 'NETEASE_EMAIL' })
        [Environment]::SetEnvironmentVariable(($prefix + '_ACCOUNT'),$mailAccount,'User')
        [Environment]::SetEnvironmentVariable(($prefix + '_AUTH_CODE'),$mailCode,'User')
        '{"state":"saved"}' | Set-Content -LiteralPath $statusPath -Encoding UTF8
        $codeBox.Clear()
        $mailCode = $null
        $form.DialogResult = [System.Windows.Forms.DialogResult]::OK
        $form.Close()
    } catch {
        [void][System.Windows.Forms.MessageBox]::Show('保存失败，请确认当前用户允许修改用户环境变量。','保存失败')
    }
})
if ($CheckUi) { $form.Dispose(); Write-Output 'ui_created'; exit 0 }
$form.Add_Shown({ $form.Activate(); $accountBox.Focus() })
try {
    $result = $form.ShowDialog()
    if ($result -ne [System.Windows.Forms.DialogResult]::OK) {
        '{"state":"cancelled"}' | Set-Content -LiteralPath $statusPath -Encoding UTF8
    }
} finally { $codeBox.Clear(); $form.Dispose() }
