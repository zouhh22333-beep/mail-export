import contextlib
import ctypes
import io
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent/'scripts'))
import mail_provider as provider
import setup_mailbox as setup
import setup_mailbox_mac as mac_setup
import mac_keychain


class MacSetupTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(dir=Path(__file__).resolve().parent)
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.status = self.base/'status.json'

    def dialog(self, value, returncode=0):
        return subprocess.CompletedProcess([], returncode, json.dumps(value), '')

    def test_native_dialog_saves_only_to_keychain_no_secret_argv_or_status(self):
        with patch.object(mac_setup.subprocess, 'run', return_value=self.dialog({'account': 'unit@163.com', 'auth_code': 'PRIVATE-CODE'})) as run, \
                patch.object(mac_keychain, 'save') as save:
            self.assertEqual(mac_setup.configure('netease', self.status), 0)
            save.assert_called_once_with('netease', 'unit@163.com', 'PRIVATE-CODE')
            self.assertNotIn('PRIVATE-CODE', repr(run.call_args))
        self.assertEqual(json.loads(self.status.read_text()), {'state': 'saved', 'authenticated': False})
        self.assertNotIn('PRIVATE-CODE', self.status.read_text())

    def test_cancel_invalid_account_and_keychain_denial_do_not_claim_saved(self):
        cases = [({'cancelled': True}, 'cancelled', None),
                 ({'account': 'unit@qq.com', 'auth_code': 'CODE'}, 'failed', None),
                 ({'account': 'unit@163.com', 'auth_code': ''}, 'failed', None),
                 ({'account': 'unit@163.com', 'auth_code': 'CODE'}, 'failed', mac_keychain.KeychainError('denied'))]
        for value, state, error in cases:
            with self.subTest(state=state, value=value), \
                    patch.object(mac_setup.subprocess, 'run', return_value=self.dialog(value)), \
                    patch.object(mac_keychain, 'save', side_effect=error) as save:
                mac_setup.configure('netease', self.status)
                self.assertEqual(json.loads(self.status.read_text())['state'], state)
                if error is None:
                    save.assert_not_called()

    def test_mac_launcher_detaches_worker_and_reports_nonsecret_status(self):
        output = io.StringIO()
        with patch.object(setup.sys, 'platform', 'darwin'), \
                patch.object(setup.sys, 'argv', ['setup_mailbox.py', '--provider', 'qq']), \
                patch.object(setup.tempfile, 'gettempdir', return_value=str(self.base)), \
                patch.object(setup.subprocess, 'Popen') as launch, contextlib.redirect_stdout(output):
            self.assertEqual(setup.main(), 0)
        args = launch.call_args.args[0]
        self.assertIn('--mac-worker', args)
        self.assertIn('qq', args)
        self.assertTrue(launch.call_args.kwargs['start_new_session'])
        self.assertEqual(launch.call_args.kwargs['stdout'], subprocess.DEVNULL)
        self.assertTrue(json.loads(output.getvalue())['launched'])

    def test_credentials_load_keychain_without_environment_and_respect_overrides(self):
        with patch.object(provider.sys, 'platform', 'darwin'), patch.object(provider, 'credential_value', return_value=''), \
                patch.object(mac_keychain, 'load', return_value={'account': 'unit@126.com', 'auth_code': 'KEYCHAIN-CODE'}) as load:
            self.assertEqual(provider.credentials({'provider': 'netease'}), ('unit@126.com', 'KEYCHAIN-CODE'))
            load.assert_called_once_with('netease')
        with patch.object(provider.sys, 'platform', 'darwin'), \
                patch.object(provider, 'credential_value', side_effect=['unit@qq.com', 'ENV-CODE']), patch.object(mac_keychain, 'load') as load:
            self.assertEqual(provider.credentials(), ('unit@qq.com', 'ENV-CODE'))
            load.assert_not_called()

    def test_keychain_denial_is_safe_failure(self):
        with patch.object(provider.sys, 'platform', 'darwin'), patch.object(provider, 'credential_value', return_value=''), \
                patch.object(mac_keychain, 'load', side_effect=mac_keychain.KeychainError('PRIVATE-DETAIL')), self.assertRaises(provider.ExportError) as error:
            provider.credentials({'provider': 'netease'})
        self.assertNotIn('PRIVATE-DETAIL', str(error.exception))


class KeychainContractTests(unittest.TestCase):
    def keychain(self):
        key = object.__new__(mac_keychain.Keychain)
        key.owned = []
        from unittest.mock import MagicMock
        key.cf, key.sec = MagicMock(), MagicMock()
        key.identity = lambda provider: [('identity', provider)]
        key.dictionary = lambda fields: fields
        key.constant = lambda name: name
        key.cf.CFDataCreate.return_value = 100
        return key

    def test_save_adds_one_atomic_record_and_updates_same_identity(self):
        key = self.keychain()
        key.sec.SecItemAdd.return_value = -25299
        key.sec.SecItemUpdate.return_value = 0
        key.save('netease', 'unit@163.com', 'CODE')
        raw = key.cf.CFDataCreate.call_args.args[1]
        self.assertEqual(json.loads(raw), {'account': 'unit@163.com', 'auth_code': 'CODE'})
        query, updates = key.sec.SecItemUpdate.call_args.args
        self.assertEqual(query, [('identity', 'netease')])
        self.assertEqual(updates, [('kSecValueData', 100)])
        key.close()
        key.cf.CFRelease.assert_called_once_with(100)

    def test_missing_record_and_access_denial_are_distinct(self):
        key = self.keychain()
        key.sec.SecItemCopyMatching.return_value = -25300
        self.assertEqual(key.load('qq'), {})
        key.sec.SecItemCopyMatching.return_value = -25293
        with self.assertRaises(mac_keychain.KeychainError):
            key.load('qq')

    def test_keychain_bytes_decoded_and_released(self):
        key = self.keychain()
        raw = b'{"account":"unit@163.com","auth_code":"CODE"}'
        buf = ctypes.create_string_buffer(raw)
        def copy(query, result):
            result._obj.value = 200
            return 0
        key.sec.SecItemCopyMatching.side_effect = copy
        key.cf.CFDataGetLength.return_value = len(raw)
        key.cf.CFDataGetBytePtr.return_value = ctypes.addressof(buf)
        self.assertEqual(key.load('netease')['auth_code'], 'CODE')
        key.close()
        key.cf.CFRelease.assert_called_once_with(200)


if __name__ == '__main__':
    unittest.main()
