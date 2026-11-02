"""Open the local Windows mailbox setup form; never accept secrets on argv."""
import argparse
import json
import os
from pathlib import Path
import subprocess
import tempfile
import uuid


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--provider', choices=('qq', 'netease'), default='netease')
    args = parser.parse_args()
    if os.name != 'nt':
        print(json.dumps({'launched': False, 'error': 'graphical_setup_currently_Windows_only'}))
        return 2
    status = Path(tempfile.gettempdir()) / ('mail-export-setup-' + uuid.uuid4().hex + '.json')
    status.write_text('{"state":"waiting"}', encoding='utf-8')
    executable = Path(os.environ.get('SystemRoot', 'C:/Windows')) / 'System32/WindowsPowerShell/v1.0/powershell.exe'
    try:
        subprocess.Popen([str(executable), '-NoProfile', '-STA', '-ExecutionPolicy', 'Bypass',
                          '-File', str(Path(__file__).with_suffix('.ps1')), '-Provider', args.provider,
                          '-StatusPath', str(status)], creationflags=subprocess.CREATE_NO_WINDOW)
    except OSError:
        status.unlink(missing_ok=True)
        print(json.dumps({'launched': False, 'error': 'cannot_open_local_setup_window'}))
        return 2
    print(json.dumps({'launched': True, 'status_path': str(status), 'authenticated': False}))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
