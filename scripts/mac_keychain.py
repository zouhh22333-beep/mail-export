"""Scoped macOS Keychain access using Security.framework, without secret argv/files."""
import ctypes as c
import json


class KeychainError(Exception):
    pass


class Keychain:
    def __init__(self):
        self.cf = c.CDLL('/System/Library/Frameworks/CoreFoundation.framework/CoreFoundation')
        self.sec = c.CDLL('/System/Library/Frameworks/Security.framework/Security')
        self.owned = []
        signatures = [
            (self.cf, 'CFStringCreateWithCString', [c.c_void_p, c.c_char_p, c.c_uint32], c.c_void_p),
            (self.cf, 'CFDataCreate', [c.c_void_p, c.c_void_p, c.c_long], c.c_void_p),
            (self.cf, 'CFDataGetLength', [c.c_void_p], c.c_long),
            (self.cf, 'CFDataGetBytePtr', [c.c_void_p], c.c_void_p),
            (self.cf, 'CFDictionaryCreate', [c.c_void_p, c.c_void_p, c.c_void_p, c.c_long, c.c_void_p, c.c_void_p], c.c_void_p),
            (self.cf, 'CFRelease', [c.c_void_p], None),
            (self.sec, 'SecItemCopyMatching', [c.c_void_p, c.POINTER(c.c_void_p)], c.c_int32),
            (self.sec, 'SecItemAdd', [c.c_void_p, c.c_void_p], c.c_int32),
            (self.sec, 'SecItemUpdate', [c.c_void_p, c.c_void_p], c.c_int32),
        ]
        for lib, name, args, result in signatures:
            fn = getattr(lib, name)
            fn.argtypes, fn.restype = args, result

    def constant(self, name):
        return c.c_void_p.in_dll(self.cf if name == 'kCFBooleanTrue' else self.sec, name).value

    def own(self, pointer):
        if not pointer:
            raise KeychainError('keychain_allocation_failed')
        self.owned.append(pointer)
        return pointer

    def string(self, value):
        return self.own(self.cf.CFStringCreateWithCString(None, value.encode('utf-8'), 0x08000100))

    def dictionary(self, fields):
        # Values remain alive in owned until the operation completes.
        keys = (c.c_void_p * len(fields))(*(self.constant(k) for k, _ in fields))
        values = (c.c_void_p * len(fields))(*(v for _, v in fields))
        return self.own(self.cf.CFDictionaryCreate(None, keys, values, len(fields), None, None))

    def identity(self, provider):
        if provider not in ('qq', 'netease'):
            raise KeychainError('unsupported_keychain_provider')
        return [('kSecClass', self.constant('kSecClassGenericPassword')),
                ('kSecAttrService', self.string('com.mail-export.credentials')),
                ('kSecAttrAccount', self.string(provider))]

    def load(self, provider):
        fields = self.identity(provider) + [('kSecReturnData', self.constant('kCFBooleanTrue')),
                                          ('kSecMatchLimit', self.constant('kSecMatchLimitOne'))]
        result = c.c_void_p()
        status = self.sec.SecItemCopyMatching(self.dictionary(fields), c.byref(result))
        if status == -25300:
            return {}
        if status:
            raise KeychainError('keychain_access_denied_or_unavailable')
        self.own(result.value)
        raw = c.string_at(self.cf.CFDataGetBytePtr(result), self.cf.CFDataGetLength(result))
        value = json.loads(raw)
        if (not isinstance(value, dict) or set(value) != {'account', 'auth_code'} or
                not all(isinstance(v, str) for v in value.values())):
            raise KeychainError('invalid_keychain_record')
        return value

    def save(self, provider, account, auth_code):
        raw = json.dumps({'account': account, 'auth_code': auth_code}).encode('utf-8')
        data = self.own(self.cf.CFDataCreate(None, raw, len(raw)))
        identity = self.identity(provider)
        status = self.sec.SecItemAdd(self.dictionary(identity + [('kSecValueData', data)]), None)
        if status == -25299:
            status = self.sec.SecItemUpdate(self.dictionary(identity), self.dictionary([('kSecValueData', data)]))
        if status:
            raise KeychainError('cannot_save_to_keychain')

    def close(self):
        for value in reversed(self.owned):
            self.cf.CFRelease(value)
        self.owned.clear()


def load(provider):
    keychain = Keychain()
    try:
        return keychain.load(provider)
    finally:
        keychain.close()


def save(provider, account, auth_code):
    keychain = Keychain()
    try:
        keychain.save(provider, account, auth_code)
    finally:
        keychain.close()
