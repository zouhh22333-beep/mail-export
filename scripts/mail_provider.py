"""Connection configuration only; download and archive logic is shared."""
from dataclasses import dataclass
import hashlib
import json
import os
import re


class ExportError(Exception):
    def __init__(self, message, *, action_required=None):
        super().__init__(message)
        self.action_required = action_required


NETEASE_HOSTS = {domain: 'imap.' + domain for domain in ('163.com', '126.com', 'yeah.net')}


def credential_value(name):
    value = os.environ.get(name, '').strip()
    if value or os.name != 'nt':
        return value
    return user_environment(name)


def user_environment(name):
    # A running Domi process may predate the user's GUI configuration.
    # Read only the explicitly selected variables, never the connector vault.
    try:
        import winreg
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, 'Environment') as key:
            value, kind = winreg.QueryValueEx(key, name)
            return value.strip() if kind in (winreg.REG_SZ, winreg.REG_EXPAND_SZ) and isinstance(value, str) else ''
    except OSError:
        return ''


def validate_connection(config):
    provider = config.get('provider', 'qq')
    if not isinstance(provider, str) or provider not in ('qq', 'netease', 'custom'):
        raise ExportError('provider_must_be_qq_netease_or_custom')
    connection = config.get('connection', {})
    if not isinstance(connection, dict):
        raise ExportError('connection_must_be_object')
    allowed = {'host', 'port', 'account_env', 'auth_code_env', 'send_id'}
    if set(connection) - allowed:
        raise ExportError('unknown_connection_fields_credentials_belong_in_environment')
    if provider != 'custom' and connection:
        raise ExportError('connection_overrides_require_provider_custom')
    if provider == 'custom':
        host = connection.get('host')
        if (not isinstance(host, str) or len(host) > 253 or '.' not in host or
                not all(re.fullmatch(r'[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?', label)
                        for label in host.split('.'))):
            raise ExportError('custom_requires_explicit_IMAP_hostname_no_URL')
        if type(connection.get('port', 993)) is not int or not 1 <= connection.get('port', 993) <= 65535:
            raise ExportError('invalid_IMAP_TLS_port')
        for field in ('account_env', 'auth_code_env'):
            if field in connection and (not isinstance(connection[field], str) or
                    not re.fullmatch(r'[A-Za-z_][A-Za-z0-9_]*', connection[field])):
                raise ExportError('invalid_credential_environment_variable_name')
        if type(connection.get('send_id', False)) is not bool:
            raise ExportError('send_id_must_be_boolean')
    return provider, connection


@dataclass(frozen=True)
class Profile:
    provider: str
    host: str
    port: int
    send_id: bool

    def account_key(self, account):
        # Keep existing QQ identity exactly, including existing archive indexes.
        if self.provider != 'custom':
            return self.provider + '-imap:' + account.casefold()
        endpoint = json.dumps([self.host, self.port], separators=(',', ':'))
        scope = hashlib.sha256(endpoint.encode()).hexdigest()
        return 'custom-imap:' + scope + ':' + account.casefold()


def profile_for(account, config=None):
    provider, connection = validate_connection(config or {})
    if not re.fullmatch(r'[^\s@\x00-\x1f\x7f]+@[^\s@\x00-\x1f\x7f]+', account):
        raise ExportError('account_must_be_full_email_address')
    domain = account.rsplit('@', 1)[1].lower()
    if provider == 'qq':
        if domain not in ('qq.com', 'foxmail.com'):
            raise ExportError('QQ_EMAIL_ACCOUNT_must_be_full_qq_or_foxmail_address')
        return Profile(provider, 'imap.qq.com', 993, False)
    if provider == 'netease':
        if domain not in NETEASE_HOSTS:
            raise ExportError('NETEASE_EMAIL_ACCOUNT_requires_163_126_or_yeah_address')
        return Profile(provider, NETEASE_HOSTS[domain], 993, True)
    return Profile(provider, connection['host'].lower(), connection.get('port', 993), connection.get('send_id', False))


def credentials(config=None):
    provider, connection = validate_connection(config or {})
    prefix = {'qq': 'QQ_EMAIL', 'netease': 'NETEASE_EMAIL', 'custom': 'MAIL_IMAP'}[provider]
    account_var = connection.get('account_env', prefix + '_ACCOUNT')
    secret_var = connection.get('auth_code_env', prefix + '_AUTH_CODE')
    account = credential_value(account_var)
    secret = credential_value(secret_var)
    if not account or not secret:
        missing = [name for name, value in ((account_var, account), (secret_var, secret)) if not value]
        raise ExportError('missing_' + account_var + '_or_' + secret_var,
                          action_required={'type': 'configure_local_credentials', 'provider': provider,
                                           'missing_env': missing, 'browser_login_required': False})
    profile_for(account, config)
    if any(ord(c) < 32 or ord(c) == 127 for c in secret):
        raise ExportError('invalid_authorization_code_control_character')
    return account, secret
