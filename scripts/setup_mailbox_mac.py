"""macOS native dialogs; their private output is consumed only inside this process."""
import json
from pathlib import Path
import subprocess
import mac_keychain
from mail_provider import profile_for, ExportError


DIALOG = '''
function run(argv) {
    var app = Application.currentApplication();
    app.includeStandardAdditions = true;
    try {
        var account = app.displayDialog(argv[0] + "邮箱完整账号", {
            defaultAnswer: "", buttons: ["取消", "下一步"], defaultButton: "下一步", cancelButton: "取消"
        }).textReturned;
        var code = app.displayDialog("输入邮箱客户端授权码（不是网页登录密码）", {
            defaultAnswer: "", hiddenAnswer: true, buttons: ["取消", "保存到钥匙串"],
            defaultButton: "保存到钥匙串", cancelButton: "取消"
        }).textReturned;
        return JSON.stringify({account: account, auth_code: code});
    } catch (error) {
        if (error.errorNumber === -128) return JSON.stringify({cancelled: true});
        throw error;
    }
}
'''


def configure(provider, status_path):
    status_path = Path(status_path)
    try:
        result = subprocess.run(['/usr/bin/osascript', '-l', 'JavaScript', '-e', DIALOG,
                                 '网易' if provider == 'netease' else 'QQ / Foxmail'],
                                capture_output=True, text=True, encoding='utf-8')
        if result.returncode:
            status = {'state': 'failed', 'error': 'cannot_open_macos_dialog_or_cancelled'}
        else:
            value = json.loads(result.stdout)
            if value.get('cancelled'):
                status = {'state': 'cancelled'}
            else:
                account, code = value['account'].strip(), value['auth_code'].strip()
                profile_for(account, {'provider': provider})
                if not code or any(ord(c) < 32 or ord(c) == 127 for c in code):
                    raise ExportError('invalid_authorization_code')
                mac_keychain.save(provider, account, code)
                status = {'state': 'saved', 'authenticated': False}
    except (ExportError, mac_keychain.KeychainError, OSError, ValueError, KeyError, TypeError):
        status = {'state': 'failed', 'error': 'check_account_authorization_code_and_keychain_access'}
    status_path.write_text(json.dumps(status), encoding='utf-8')
    return 0 if status['state'] in ('saved', 'cancelled') else 2
