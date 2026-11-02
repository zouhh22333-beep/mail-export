"""Open a local Windows/macOS mailbox setup form; never accept secrets on argv."""
import argparse
import json
import os
from pathlib import Path
import subprocess
import tempfile
import uuid
import sys


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--provider', choices=('qq', 'netease'), default='netease')
    parser.add_argument('--mac-worker', action='store_true', help=argparse.SUPPRESS)
    parser.add_argument('--status-path', help=argparse.SUPPRESS)
    args = parser.parse_args()
    if args.mac_worker:
        from setup_mailbox_mac import configure
        return configure(args.provider, Path(args.status_path))
    if os.name != 'nt' and sys.platform != 'darwin':
        print(json.dumps({'launched': False, 'error': 'graphical_setup_requires_Windows_or_macOS'}))
        return 2
    status = Path(tempfile.gettempdir()) / ('mail-export-setup-' + uuid.uuid4().hex + '.json')
    status.write_text('{"state":"waiting"}', encoding='utf-8')
    try:
        if sys.platform == 'darwin':
            subprocess.Popen([sys.executable, str(Path(__file__).resolve()), '--provider', args.provider,
                              '--mac-worker', '--status-path', str(status)],
                             stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                             start_new_session=True)
        else:
            executable = Path(os.environ.get('SystemRoot', 'C:/Windows')) / 'System32/WindowsPowerShell/v1.0/powershell.exe'
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
