#!/usr/bin/env python3
"""Archive locally staged mail artifacts. Python 3.10+, standard library only.

No mailbox/network access. See references/archive-rules.md for the manifest.
"""

import argparse
import hashlib
import io
import json
import os
from pathlib import Path
import re
import sqlite3
import sys
import tempfile
from datetime import datetime, timezone


class ArchiveError(Exception):
    pass


def required_string(value, label):
    if not isinstance(value, str) or not value.strip():
        raise ArchiveError(f"{label} must be a nonempty string")
    return value


def safe_name(value, fallback="untitled"):
    """Keep original spelling unless Windows compatibility requires a change."""
    value = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", value).rstrip(" .")
    value = value or fallback
    if re.fullmatch(r"(?i)(CON|PRN|AUX|NUL|COM[1-9¹²³]|LPT[1-9¹²³])(?:\..*)?", value):
        value = "_" + value
    # Bounded components also leave room for sequence suffixes on Windows.
    suffix = Path(value).suffix
    if len(suffix) > 24:
        suffix = ""
    stem = value[:-len(suffix)] if suffix else value
    while (len((stem + suffix).encode("utf-16-le")) // 2 > 240
           or len((stem + suffix).encode("utf-8")) > 240):
        stem = stem[:-1]
    return (stem.rstrip(" .") or fallback) + suffix


def digest_file(path):
    digest = hashlib.sha256()
    size = 0
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            size += len(block)
            digest.update(block)
    return size, digest.hexdigest()


def collision_name(name, number):
    if number == 1:
        return name
    suffix = Path(name).suffix
    stem = name[:-len(suffix)] if suffix else name
    return f"{stem} ({number}){suffix}"


class Store:
    def __init__(self, root):
        raw = Path(root)
        if not raw.is_absolute():
            raise ArchiveError("--root must be an absolute local path")
        raw.mkdir(parents=True, exist_ok=True)
        self.root = raw.resolve()
        self.db = None
        self.lock = None
        self.lock_fd = None
        try:
            meta = self.confined(self.root / ".mail-export")
            meta.mkdir(exist_ok=True)
            self.lock = self.confined(meta / "archive.lock")
            try:
                self.lock_fd = os.open(self.lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
            except FileExistsError:
                raise ArchiveError("archive is locked; confirm no run is active before removing .mail-export/archive.lock")
            os.write(self.lock_fd, str(os.getpid()).encode("ascii"))
            database = self.confined(meta / "archive.sqlite")
            if database.is_symlink():
                raise ArchiveError("index must not be a symbolic link")
            self.db = sqlite3.connect(database)
            self.db.row_factory = sqlite3.Row
            self.db.executescript("""
                CREATE TABLE IF NOT EXISTS messages (
                    account_id TEXT NOT NULL, message_id TEXT NOT NULL,
                    directory TEXT NOT NULL UNIQUE,
                    PRIMARY KEY (account_id, message_id)
                );
                CREATE TABLE IF NOT EXISTS artifacts (
                    account_id TEXT NOT NULL, message_id TEXT NOT NULL, part_id TEXT NOT NULL,
                    kind TEXT NOT NULL, subject TEXT, original_name TEXT, save_name TEXT,
                    relative_path TEXT, size INTEGER, sha256 TEXT, state TEXT NOT NULL,
                    error TEXT, updated_at TEXT NOT NULL,
                    PRIMARY KEY (account_id, message_id, part_id)
                );
            """)
        except Exception:
            self.close()
            raise

    def close(self):
        if self.db is not None:
            self.db.close()
            self.db = None
        if self.lock_fd is not None:
            os.close(self.lock_fd)
            self.lock_fd = None
            self.lock.unlink(missing_ok=True)

    def confined(self, path):
        resolved = path.resolve()
        if not resolved.is_relative_to(self.root):
            raise ArchiveError("destination escapes the archive root")
        return path

    def destination(self, path):
        self.confined(path)
        if path.resolve().is_relative_to((self.root / ".mail-export").resolve()):
            raise ArchiveError(".mail-export is reserved for the archive index")
        return path

    def target_directory(self, account, item):
        if "directory" in item:
            parts = item["directory"]
            if not isinstance(parts, list) or any(not isinstance(p, str) for p in parts):
                raise ArchiveError("directory must be an array of folder-name components")
            for part in parts:
                if not part.strip() or part in (".", "..") or any(c in part for c in "/\\:"):
                    raise ArchiveError("directory must not contain absolute paths, separators or traversal")
            parts = [safe_name(p) for p in parts]
            if parts and parts[0].casefold() == ".mail-export":
                raise ArchiveError(".mail-export is reserved for the archive index")
            directory = self.destination(self.root.joinpath(*parts))
            directory.mkdir(parents=True, exist_ok=True)
            return self.destination(directory)
        row = self.db.execute(
            "SELECT directory FROM messages WHERE account_id=? AND message_id=?",
            (account, item["message_id"]),
        ).fetchone()
        if row:
            directory = self.destination(self.root / row["directory"])
            directory.mkdir(parents=True, exist_ok=True)
        else:
            subject = item.get("subject") or ""
            name = safe_name(subject if subject.strip() else "无主题")
            if name.casefold() == ".mail-export":
                name = "_mail-export"
            number = 1
            while True:
                directory = self.destination(self.root / collision_name(name, number))
                # Case-insensitive collision handling keeps results portable.
                occupied = {p.name.casefold() for p in self.root.iterdir()}
                allocated = {r[0].casefold() for r in self.db.execute("SELECT directory FROM messages")}
                if directory.name.casefold() not in occupied | allocated:
                    try:
                        directory.mkdir()
                        break
                    except FileExistsError:
                        pass
                number += 1
            self.db.execute("INSERT INTO messages VALUES (?, ?, ?)", (account, item["message_id"], directory.name))
            self.db.commit()
        if item["kind"] == "attachment":
            directory = self.destination(directory / "attachments")
            directory.mkdir(exist_ok=True)
        return self.destination(directory)

    def row(self, key):
        return self.db.execute(
            "SELECT * FROM artifacts WHERE account_id=? AND message_id=? AND part_id=?", key
        ).fetchone()

    def valid(self, row):
        if row is None or row["state"] not in ("saved", "publishing") or not row["relative_path"]:
            return False
        path = self.destination(self.root / row["relative_path"])
        try:
            if not path.is_file() or path.stat().st_size != row["size"]:
                return False
            return digest_file(path)[1] == row["sha256"]
        except OSError:
            return False

    def record(self, key, item, state, path=None, size=None, checksum=None, error=None):
        def metadata(name):
            value = item.get(name)
            return value if isinstance(value, str) else None

        self.db.execute("""
            INSERT INTO artifacts VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(account_id, message_id, part_id) DO UPDATE SET
                kind=excluded.kind, subject=excluded.subject,
                original_name=excluded.original_name, save_name=excluded.save_name,
                relative_path=excluded.relative_path, size=excluded.size, sha256=excluded.sha256,
                state=excluded.state, error=excluded.error, updated_at=excluded.updated_at
        """, (*key, item["kind"], metadata("subject"), metadata("original_name"), metadata("save_name"),
              str(path.relative_to(self.root)) if path else None, size, checksum, state, error,
              datetime.now(timezone.utc).isoformat()))
        self.db.commit()

    def promote(self, key):
        self.db.execute("UPDATE artifacts SET state='saved', error=NULL WHERE account_id=? AND message_id=? AND part_id=?", key)
        self.db.commit()

    def import_one(self, account, item, check=False, *, content=None):
        result = {"message_id": item.get("message_id"), "part_id": item.get("part_id"),
                  "original_name": item.get("original_name"), "requested_name": item.get("save_name")}
        key = None
        try:
            message = required_string(item.get("message_id"), "message_id")
            part = required_string(item.get("part_id"), "part_id")
            if item.get("kind") not in ("email", "attachment"):
                raise ArchiveError("kind must be email or attachment")
            key = (account, message, part)
            if "subject" in item and not isinstance(item["subject"], str):
                raise ArchiveError("subject must be a string")
            row = self.row(key)
            if self.valid(row):
                if row["state"] == "publishing":
                    self.promote(key)
                result.update(state="skipped", path=str(self.root / row["relative_path"]))
                if "save_name" in item or "directory" in item:
                    result["warning"] = "existing indexed path retained; check/import do not rename saved artifacts"
                return result
            if check:
                result["state"] = "pending"
                return result
            if "error" in item:
                required_string(item["error"], "error")
                # Do not echo connector errors: they can contain credentials or content.
                raise ArchiveError("connector_error")
            source_fields = [field for field in ("source_path", "text") if field in item]
            if content is not None:
                if not isinstance(content, bytes):
                    raise ArchiveError("content must be bytes")
                source_fields.append("content")
            if len(source_fields) != 1:
                raise ArchiveError("provide exactly one of source_path or text")
            source = None
            if "source_path" in item:
                source = Path(required_string(item["source_path"], "source_path"))
                if not source.is_absolute() or not source.is_file():
                    raise ArchiveError("source_path must be an absolute existing file")
            elif content is None and not isinstance(item["text"], str):
                raise ArchiveError("text must be a string")
            original = item.get("original_name")
            chosen = item.get("save_name")
            fallback = item.get("fallback_name")
            if original is not None and not isinstance(original, str):
                raise ArchiveError("original_name must be a string or null")
            if chosen is not None:
                required_string(chosen, "save_name")
            if fallback is not None:
                required_string(fallback, "fallback_name")
            name = chosen or (original if original and original.strip() else None) or fallback
            if name is None:
                if item["kind"] == "attachment":
                    name = "附件_" + part
                else:
                    suffix = ".txt" if "text" in item else (".eml" if source and source.suffix.lower() == ".eml" else None)
                    if suffix is None:
                        raise ArchiveError("email source needs original_name, fallback_name or explicit save_name for its actual format")
                    subject = item.get("subject") or ""
                    name = (subject if subject.strip() else "无主题") + suffix
            name = safe_name(name)
            directory = self.target_directory(account, item)
            temp = None
            try:
                with tempfile.NamedTemporaryFile(mode="wb", prefix=".mail-export-", suffix=".tmp", dir=directory, delete=False) as output:
                    temp = Path(output.name)
                    digest, size = hashlib.sha256(), 0
                    with source.open("rb") if source else io.BytesIO(content if content is not None else item["text"].encode("utf-8")) as input_stream:
                        for block in iter(lambda: input_stream.read(1024 * 1024), b""):
                            output.write(block)
                            digest.update(block)
                            size += len(block)
                    output.flush()
                    os.fsync(output.fileno())
                checksum = digest.hexdigest()
                number = 1
                while True:
                    final = self.destination(directory / collision_name(name, number))
                    occupied = {p.name.casefold() for p in directory.iterdir()}
                    if final.name.casefold() in occupied:
                        number += 1
                        continue
                    self.record(key, item, "publishing", final, size, checksum)
                    try:
                        # Windows rename fails if destination exists. POSIX rename
                        # replaces, so use an exclusive same-directory hard link there.
                        if os.name == "nt":
                            os.rename(temp, final)
                        else:
                            try:
                                os.link(temp, final)
                            except FileExistsError:
                                raise
                            except OSError:
                                # Portable fallback: exclusive create prevents overwrite.
                                # A crash may leave a partial file; its hash will not pass
                                # resume verification, and the retry uses a fresh suffix.
                                created = False
                                try:
                                    with final.open("xb") as out:
                                        created = True
                                        with temp.open("rb") as inp:
                                            for block in iter(lambda: inp.read(1024 * 1024), b""):
                                                out.write(block)
                                        out.flush()
                                        os.fsync(out.fileno())
                                except Exception:
                                    if created:
                                        final.unlink(missing_ok=True)
                                    raise
                        break
                    except FileExistsError:
                        number += 1
                self.record(key, item, "saved", final, size, checksum)
                result.update(state="saved", path=str(final), size=size, sha256=checksum)
            finally:
                if temp is not None:
                    temp.unlink(missing_ok=True)
        except (ArchiveError, OSError, ValueError, sqlite3.Error) as exc:
            error = str(exc) if isinstance(exc, ArchiveError) else f"local_io_error:{type(exc).__name__}"
            if key is not None and not check:
                self.record(key, item, "failed", error=error)
            result.update(state="failed", error=error)
        return result

    def status(self):
        results = []
        for row in self.db.execute("SELECT * FROM artifacts ORDER BY account_id, message_id, part_id").fetchall():
            try:
                valid = self.valid(row)
            except ArchiveError:
                valid = False
            state = "saved" if valid else ("failed" if row["state"] == "failed" else "pending")
            results.append({"account_id": row["account_id"], "message_id": row["message_id"],
                            "part_id": row["part_id"], "state": state,
                            "original_name": row["original_name"], "requested_name": row["save_name"],
                            "path": str(self.root / row["relative_path"]) if row["relative_path"] else None,
                            "error": row["error"]})
        return results


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("check", "import", "status"))
    parser.add_argument("--root", required=True)
    parser.add_argument("--manifest")
    args = parser.parse_args()
    store = None
    try:
        manifest = None
        if args.command != "status":
            if not args.manifest:
                raise ArchiveError("check/import require --manifest")
            manifest = json.loads(Path(args.manifest).read_text(encoding="utf-8-sig"))
            if not isinstance(manifest, dict) or manifest.get("version") != 1:
                raise ArchiveError("manifest must be an object with version 1")
            required_string(manifest.get("account_id"), "account_id")
            if not isinstance(manifest.get("items"), list) or any(not isinstance(i, dict) for i in manifest["items"]):
                raise ArchiveError("items must be an array of objects")
        store = Store(args.root)
        if args.command == "status":
            results = store.status()
        else:
            results = [store.import_one(manifest["account_id"], item, args.command == "check") for item in manifest["items"]]
        counts = {state: sum(item["state"] == state for item in results)
                  for state in ("saved", "skipped", "pending", "failed")}
        print(json.dumps({"version": 1, "command": args.command, "root": str(store.root),
                          "counts": counts, "unit": "artifacts", "results": results}, ensure_ascii=False))
        return 1 if args.command != "status" and counts["failed"] else 0
    except (ArchiveError, OSError, ValueError, sqlite3.Error) as exc:
        error = str(exc) if isinstance(exc, ArchiveError) else f"local_io_error:{type(exc).__name__}"
        print(json.dumps({"error": error}, ensure_ascii=False))
        return 2
    finally:
        if store is not None:
            store.close()


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    raise SystemExit(main())
