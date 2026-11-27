"""The files whose contents are a secret: the list every read check in Bypass consults."""

from __future__ import annotations

import re
import shlex
from fnmatch import fnmatchcase
from pathlib import PurePosixPath
from typing import ClassVar


class SecretFiles:
    """Names and places of keys, credentials, password stores, cookie jars and shell history.

    Matching ignores case, as macOS's and Windows' file systems do, and reads
    a Windows path's backslashes as slashes. Only a file's contents are
    secret: listing the folder that holds one is not a read of it.
    """

    NAMES: ClassVar[tuple[str, ...]] = (
        # SSH and other private keys, certificates with their keys, keystores
        "id_rsa",
        "id_dsa",
        "id_ecdsa",
        "id_ed25519",
        "id_ecdsa_sk",
        "id_ed25519_sk",
        "?*.ppk",
        "?*.pem",
        "?*.key",
        "?*.p8",
        "?*.p12",
        "?*.pfx",
        "?*.jks",
        "?*.keystore",
        "?*.ovpn",
        "?*.kubeconfig",
        "kubeconfig",
        # GnuPG secret-key exports and keyrings; public keyrings and signatures are left alone
        "secring.*",
        "*secret*.asc",
        "*private*.asc",
        "*secret*.gpg",
        "*private*.gpg",
        # environment files
        ".env",
        ".env.*",
        # registry, cloud and database credentials stored under a fixed name
        ".netrc",
        "_netrc",
        ".npmrc",
        ".pypirc",
        ".git-credentials",
        ".dockercfg",
        "credentials.json",
        "service-account*.json",
        "client_secret*.json",
        ".terraformrc",
        "terraform.rc",
        ".pgpass",
        "pgpass.conf",
        ".my.cnf",
        ".mylogin.cnf",
        ".s3cfg",
        ".boto",
        ".htpasswd",
        "?*.token",
        # password managers' vaults and exports
        "?*.kdbx",
        "?*.kdb",
        "?*.1pif",
        "?*.1pux",
        "?*.psafe3",
        "wallet.dat",
        # macOS keychains
        "?*.keychain",
        "?*.keychain-db",
        # browsers' password and cookie stores whose names no project file shares
        "key3.db",
        "key4.db",
        "signons.sqlite",
        "cookies.sqlite",
        "cookies.sqlite-*",
        "?*.binarycookies",
        # shell and REPL history, which keeps every secret typed at a prompt
        ".bash_history",
        ".zsh_history",
        ".zhistory",
        ".histfile",
        ".sh_history",
        "fish_history",
        ".python_history",
        ".psql_history",
        ".mysql_history",
        ".sqlite_history",
        ".node_repl_history",
        ".rediscli_history",
        "consolehost_history.txt",
    )
    """Base names that are a secret wherever they are."""

    TOLD_STEMS: ClassVar[tuple[str, ...]] = (
        "*backup*code*",
        "*recovery*code*",
        "*recovery*key*",
        "*emergency*kit*",
        "*password",
        "*passwords*",
        "*secrets*",
        "*token",
        "*api?key",
        "*api?keys",
        "*apikey",
        "*apikeys",
        "*bitwarden*export*",
        "*1password*export*",
        "*lastpass*export*",
    )
    """Names that say the file holds codes, passwords, secrets or tokens; only a data file's."""

    DATA_SUFFIXES: ClassVar[frozenset[str]] = frozenset(
        {
            "",
            ".txt",
            ".text",
            ".csv",
            ".tsv",
            ".json",
            ".yaml",
            ".yml",
            ".toml",
            ".ini",
            ".cfg",
            ".conf",
            ".env",
            ".xml",
            ".plist",
            ".properties",
            ".pdf",
            ".rtf",
            ".doc",
            ".docx",
            ".odt",
            ".xls",
            ".xlsx",
            ".ods",
            ".numbers",
            ".png",
            ".jpg",
            ".jpeg",
            ".heic",
            ".bak",
            ".old",
        }
    )
    """Suffixes of files that hold data, not code or docs: ``passwords.csv``, never ``password_reset.py``."""

    TEMPLATE_PARTS: ClassVar[frozenset[str]] = frozenset({"example", "sample", "template", "dist"})
    """A dotted part of a name that marks a template: ``.env.example``, ``secrets.sample.yaml``."""

    FOLDERS: ClassVar[tuple[str, ...]] = (
        "/.ssh/",
        "/.gnupg/",
        "/.password-store/",
        "/library/keychains/",
        "/.local/share/keyrings/",
        "/microsoft/credentials/",
        "/microsoft/protect/",
        "/.azure/",
        "/.config/gcloud/",
        "/.config/doctl/",
        "/application support/doctl/",
        "/.config/op/",
        "/.op/",
        "/.aws/sso/cache/",
        "/.aws/cli/cache/",
        "/etc/wireguard/",
        ".agilekeychain/",
        ".opvault/",
    )
    """Folders whose every file is a secret."""

    PLACES: ClassVar[tuple[str, ...]] = (
        "/.aws/credentials",
        "/.docker/config.json",
        "/.kube/config",
        "/.config/gh/hosts.yml",
        "/github cli/hosts.yml",
        "/.config/hub",
        "/.config/git/credentials",
        "/.terraform.d/credentials.tfrc.json",
        "/.oci/config",
        "/.huggingface/token",
        "/.cache/huggingface/stored_tokens",
        "/.m2/settings.xml",
        "/.m2/settings-security.xml",
        "/.gradle/gradle.properties",
        "/.cargo/credentials",
    )
    """Files that are a secret at this place, with their backups; reading their folder reads them."""

    SYSTEM_FILES: ClassVar[tuple[str, ...]] = ("/etc/shadow", "/etc/gshadow", "/etc/master.passwd")
    """The system's password hashes, with their backups (``/etc/shadow-``)."""

    CHROMIUM_PROFILE: ClassVar[re.Pattern[str]] = re.compile(
        r"/(google/chrome[^/]*|google-chrome[^/]*|chromium|microsoft edge[^/]*|microsoft/edge[^/]*"
        r"|microsoft-edge[^/]*|bravesoftware/brave-browser[^/]*|vivaldi|opera software"
        r"|com\.operasoftware\.opera[^/]*|opera[^/]*|arc/user data|yandex/yandexbrowser)/"
    )
    """A Chromium-family browser's profile folder, on macOS, Linux or Windows."""

    CHROMIUM_STORES: ClassVar[tuple[str, ...]] = (
        "login data*",
        "web data*",
        "cookies",
        "cookies-journal",
        "local state",
    )
    """Its saved passwords, cookies, autofill and the key that decrypts them."""

    FIREFOX_PROFILE: ClassVar[re.Pattern[str]] = re.compile(
        r"/(\.?mozilla|firefox|librewolf|waterfox|\.?thunderbird|floorp)/|\.default[^/]*/"
    )
    """A Firefox-family profile folder."""

    FIREFOX_STORES: ClassVar[tuple[str, ...]] = ("logins.json", "logins-backup.json")
    """Its saved passwords."""

    READERS: ClassVar[frozenset[str]] = frozenset(
        {
            "cat",
            "bat",
            "less",
            "more",
            "head",
            "tail",
            "nl",
            "tac",
            "xxd",
            "od",
            "hexdump",
            "strings",
            "base64",
            "cp",
            "rsync",
            "install",
            "openssl",
            "grep",
            "egrep",
            "rg",
            "ag",
            "ack",
            "sqlite3",
            "awk",
            "gawk",
            "sed",
            "cut",
            "sort",
            "uniq",
            "diff",
            "cmp",
            "paste",
            "zcat",
            "tar",
            "zip",
            "7z",
            "gpg",
            "dd",
            "jq",
            "yq",
            "vi",
            "vim",
            "view",
            "nano",
            "emacs",
            "type",
            "get-content",
            "gc",
            "select-string",
            "findstr",
            "copy",
            "copy-item",
        }
    )
    """Commands that print, copy or pack a file's bytes: enough to lift a secret."""

    PATTERN_FIRST: ClassVar[frozenset[str]] = frozenset(
        {
            "grep",
            "egrep",
            "rg",
            "ag",
            "ack",
            "findstr",
            "select-string",
            "sed",
            "awk",
            "gawk",
            "jq",
            "yq",
        }
    )
    """Readers whose first operand is a pattern or a script, not a file, unless an option gives it."""

    PATTERN_OPTIONS: ClassVar[frozenset[str]] = frozenset({"--regexp", "--expression", "-pattern"})
    """Long options that give such a reader its pattern; the word after one is not a file."""

    SCRIPT_OPTIONS: ClassVar[frozenset[str]] = frozenset({"-f", "--file", "--from-file"})
    """Options that give such a reader its pattern from a file, which leaves no positional pattern."""

    DESTINATION_LAST: ClassVar[frozenset[str]] = frozenset(
        {"cp", "install", "rsync", "copy", "copy-item"}
    )
    """Copiers whose last operand is written, not read: ``cp .env.example .env`` reads no secret."""

    TARGET_OPTIONS: ClassVar[frozenset[str]] = frozenset({"-t", "--target-directory"})
    """Options that name a copier's destination, which leaves every operand a source."""

    STORE_COMMANDS: ClassVar[re.Pattern[str]] = re.compile(
        r"\bsecurity\s+(find-generic-password|find-internet-password|dump-keychain|export)\b"
        r"|\bsecret-tool\s+lookup\b|\bpass\s+show\b|\bop\s+(read|item\s+get)\b|\bbw\s+get\b"
        r"|\bgh\s+auth\s+token\b|\bgcloud\s+auth\s+(application-default\s+)?print-(access|identity)-token\b"
        r"|\baws\s+configure\s+(get\s+(aws_secret_access_key|aws_session_token)|export-credentials)\b"
        r"|\bgpg\b[^|;&]*--export-secret-(sub)?keys\b",
        re.IGNORECASE,
    )
    """Commands that read a keychain or a credential store and print what it holds."""

    _SEGMENTS: ClassVar[re.Pattern[str]] = re.compile(r"&&|\|\||;|\n|\||&")
    """Where one command of a chain ends."""

    _OUTPUT_REDIRECT: ClassVar[re.Pattern[str]] = re.compile(r"[\d&]*>>?\|?\s*[^\s<>]+")
    """An output redirection: the file it names is written, not read."""

    _WORD_JOINS: ClassVar[re.Pattern[str]] = re.compile(r"[<>=]")
    """What glues a path to a command, an option or a redirection: ``if=~/.netrc``, ``cat<.env``."""

    _BACKSLASH_PATH: ClassVar[re.Pattern[str]] = re.compile(r"\\(?=[^\s'\"\\])")
    """A backslash between a Windows path's parts, not one escaping a space."""

    @classmethod
    def is_secret(cls, path: str) -> bool:
        """True when ``path`` names a secret file, or a folder a secret file sits in."""
        return cls._secret(path, bare=True)

    @classmethod
    def read_in(cls, command: str) -> bool:
        """True when ``command`` prints, copies or packs a secret file, or reads a credential store."""
        if not command:
            return False
        if cls.STORE_COMMANDS.search(command):
            return True
        text = cls._OUTPUT_REDIRECT.sub(" ", command)
        return any(cls._segment_reads(segment) for segment in cls._SEGMENTS.split(text))

    @classmethod
    def _secret(cls, path: str, bare: bool) -> bool:
        """``bare`` lets a word with no folder and no suffix (``password``) count as a told name."""
        if not path:
            return False
        lowered = path.strip().strip("'\"").replace("\\", "/").lower()
        full = "/" + lowered.lstrip("/")
        folder = full.rstrip("/") + "/"
        name = folder[:-1].rsplit("/", 1)[-1]
        told = bare or "/" in lowered or "." in name.lstrip(".")
        return (
            cls._named(name, told)
            or cls._browser_store(full, name)
            or any(secret_folder in folder for secret_folder in cls.FOLDERS)
            or cls._at_place(full)
            or cls._holds_place(folder)
        )

    @classmethod
    def _named(cls, name: str, told: bool) -> bool:
        if cls.TEMPLATE_PARTS.intersection(name.split(".")[1:]):
            return False
        if any(fnmatchcase(name, glob) for glob in cls.NAMES):
            return True
        pure = PurePosixPath(name)
        if not told or pure.suffix not in cls.DATA_SUFFIXES:
            return False
        return any(fnmatchcase(pure.stem, glob) for glob in cls.TOLD_STEMS)

    @classmethod
    def _browser_store(cls, full: str, name: str) -> bool:
        if any(fnmatchcase(name, glob) for glob in cls.CHROMIUM_STORES):
            return cls.CHROMIUM_PROFILE.search(full) is not None
        if name in cls.FIREFOX_STORES:
            return cls.FIREFOX_PROFILE.search(full) is not None
        return False

    @classmethod
    def _at_place(cls, full: str) -> bool:
        for place in cls.PLACES + cls.SYSTEM_FILES:
            start = full.find(place)
            while start != -1:
                end = start + len(place)
                if not full[end : end + 1].isalnum():
                    return True
                start = full.find(place, start + 1)
        return False

    @classmethod
    def _holds_place(cls, folder: str) -> bool:
        return any(folder.endswith(place.rsplit("/", 1)[0] + "/") for place in cls.PLACES)

    @classmethod
    def _segment_reads(cls, segment: str) -> bool:
        words = cls._words(segment)
        for index, word in enumerate(words):
            tool = word.replace("\\", "/").rstrip("/").rsplit("/", 1)[-1].lower()
            if tool in cls.READERS:
                operands = cls._read_operands(tool, words[index + 1 :])
                return any(cls._secret(operand, bare=False) for operand in operands)
        return False

    @classmethod
    def _words(cls, segment: str) -> list[str]:
        text = cls._BACKSLASH_PATH.sub("/", segment)
        try:
            words = shlex.split(text)
        except ValueError:
            words = text.split()
        return [part for word in words for part in cls._WORD_JOINS.split(word) if part]

    @classmethod
    def _read_operands(cls, tool: str, words: list[str]) -> list[str]:
        """The words a reader reads: its pattern, script and destination left out."""
        pattern_first = tool in cls.PATTERN_FIRST
        pattern_given = skip_next = target_given = False
        operands: list[str] = []
        for word in words:
            if skip_next:
                skip_next = False
                continue
            lowered = word.lower()
            if pattern_first and (lowered in cls.PATTERN_OPTIONS or cls._ends_in_e(word)):
                pattern_given = skip_next = True
            elif pattern_first and (lowered in cls.SCRIPT_OPTIONS or cls._joins_e(word)):
                pattern_given = True
            elif lowered in cls.TARGET_OPTIONS:
                target_given = True
            elif not word.startswith("-"):
                operands.append(word)
        if pattern_first and not pattern_given:
            operands = operands[1:]
        if tool in cls.DESTINATION_LAST and not target_given:
            operands = operands[:-1]
        return operands

    @staticmethod
    def _ends_in_e(word: str) -> bool:
        """``-e`` or a cluster ending in it (``-ie``): the next word is the pattern."""
        return len(word) > 1 and word[0] == "-" and word[1] != "-" and word.endswith("e")

    @staticmethod
    def _joins_e(word: str) -> bool:
        """``-ePATTERN`` or ``-iePATTERN``: the pattern rides in the same word."""
        return len(word) > 2 and word[0] == "-" and word[1] != "-" and "e" in word[1:]
