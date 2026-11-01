#!/usr/bin/env python3
"""Compatibility entry for existing QQ tasks; uses the shared IMAP engine."""
import sys
import imap_export

if __name__ == '__main__':
    if hasattr(sys.stdout, 'reconfigure'):
        sys.stdout.reconfigure(encoding='utf-8')
    raise SystemExit(imap_export.main())
else:
    # Preserve old imports and mock targets without a duplicate download engine.
    sys.modules[__name__] = imap_export
