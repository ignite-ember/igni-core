"""Tool calls that are dangerous by their shape, whatever a model thinks of them.

The first of the two layers behind Bypass mode's last check (see
:mod:`igni_core.core.hooks.bypass_guard`). A rule here is precise — it
names a known-dangerous action — so it never needs a probability, and it
covers exactly the actions a classifier is weakest on. Measured against
Laya (``laya-typed-decisions``) on forty labelled tool calls: at the
chosen threshold it missed ``npm publish``, a crontab persistence line,
``mkfs`` and ``chmod -R 777 /etc``. Each is a pattern below.

A match does not block. It asks: the person running Bypass decided to
trust the agent, and the point is that they see the few calls where that
trust matters before they run, not that igni overrules them.

Paths inside the project are ordinary work. ``rm -rf ./build`` is
routine; ``rm -rf ~`` is not. :func:`flag_shell` is given the project
directory so the deletion rule can tell them apart.
"""

from __future__ import annotations

import os
import re
import shlex
from fnmatch import fnmatch
from pathlib import Path

from igni_core.core.config.permission_eval.read_only_shell import command_segments, is_read_only
from igni_core.core.paths import CONFIG_DIR, LEGACY_CONFIG_DIR

#: Files that are almost never a legitimate thing for an agent to read on its
#: own — private keys, cloud and registry credentials, auth stores. Reading one
#: is the shape of credential theft (the first half of an exfiltration, and the
#: half a network rule misses when the send happens later or out of band), so
#: Bypass stops to ask even though the call only reads. Deliberately NARROWER
#: than ``safety.protected_paths``: that list gates *writes* and includes
#: routine files like ``.env`` a developer reads constantly; these are the
#: files whose *read* is itself suspicious.
_SECRET_NAME_GLOBS: tuple[str, ...] = (
    "id_rsa",
    "id_dsa",
    "id_ecdsa",
    "id_ed25519",
    "*.ppk",
    "*.pem",
    "*.p12",
    "*.pfx",
    ".netrc",
    ".npmrc",
    ".pypirc",
    ".git-credentials",
    "credentials.json",
    "service-account*.json",
)
_IGNI_HOME_DIRS: tuple[str, ...] = (CONFIG_DIR, LEGACY_CONFIG_DIR)
"""igni's home config directories, ``~/.igni`` and the legacy ``~/.ember``."""
_IGNI_SECRET_GLOBS: tuple[str, ...] = ("config.yaml*", "config.yml*", "credentials.json*")
"""igni's key stores there (API keys, the cloud sign-in token) and their backups."""
_SECRET_PATH_SUBSTR: tuple[str, ...] = (
    "/.ssh/",
    "/.aws/credentials",
    "/.docker/config.json",
    "/.kube/config",
    "/.config/gcloud/",
)
#: Utilities whose job is to emit a file's bytes — enough to lift a secret.
_READ_UTILS = r"cat|bat|less|more|head|tail|nl|tac|xxd|od|hexdump|strings|base64|cp|rsync|install|openssl|grep|egrep|rg|ag|ack"
#: Secret/key targets as they appear inside a shell command.
_SECRET_IN_SHELL = (
    r"(~?/?\.ssh/|\bid_rsa\b|\bid_ed25519\b|\bid_ecdsa\b|\bid_dsa\b|\.aws/credentials|"  # noqa: S105 — a pattern that detects secrets, not one
    r"\.netrc\b|\.git-credentials\b|\.pypirc\b|\.npmrc\b|\.kube/config|\.docker/config\.json|"
    r"\.pem\b|\.p12\b|\.pfx\b|\bcredentials\.json\b)"
)


def _is_igni_secret(path: str) -> bool:
    """True when ``path`` names one of igni's key stores in the home config directory."""
    candidate = Path(os.path.expandvars(os.path.expanduser(path)))
    home = Path.home()
    return any(candidate.parent == home / d for d in _IGNI_HOME_DIRS) and any(
        fnmatch(candidate.name, glob) for glob in _IGNI_SECRET_GLOBS
    )


def _igni_secret_in_shell() -> re.Pattern[str]:
    """A read utility aimed at igni's home key stores, a glob over them, or the directory itself."""
    homes = "|".join(re.escape(h) for h in ("~", "$HOME", "${HOME}", str(Path.home())))
    dirs = "|".join(re.escape(d) for d in _IGNI_HOME_DIRS)
    store = r"config\.ya?ml|credentials\.json|[^\s/'\"]*[*?\[][^\s/'\"]*"
    target = rf"(?:{homes})/(?:{dirs})(?:/(?:{store})|/?(?=[\s'\"]|$))"
    return re.compile(rf"\b({_READ_UTILS})\b[^|;&]*{target}", re.IGNORECASE)


def _is_secret_file(path: str) -> bool:
    """True when ``path`` names a credential, private key or auth store."""
    if not path:
        return False
    if _is_igni_secret(path):
        return True
    name = Path(path).name
    if any(fnmatch(name, glob) for glob in _SECRET_NAME_GLOBS):
        return True
    normalized = "/" + path.replace("\\", "/").lstrip("/")
    return any(sub in normalized for sub in _SECRET_PATH_SUBSTR)


def flag_file_read(path: str) -> str | None:
    """Why reading ``path`` needs a person's yes: it is a credential or key file.

    Covers the read-only file tools (``read_file`` and friends) that Bypass
    otherwise waves through — reading ``~/.ssh/id_rsa`` is not the ordinary
    read those exemptions were written for."""
    return "reads a credential or secret file" if _is_secret_file(path) else None


#: Read-only commands that report on a file without printing what is in it.
_METADATA_COMMANDS = frozenset(
    {
        "ls",
        "stat",
        "du",
        "df",
        "file",
        "find",
        "which",
        "basename",
        "dirname",
        "realpath",
        "readlink",
        "pwd",
        "whoami",
        "uname",
        "echo",
        "printf",
        "wc",
    }
)


def flag_shell_read(command: str) -> str | None:
    """:func:`flag_file_read` for a read-only shell command that prints a file's contents."""
    segments = command_segments(command)
    if segments is None or not all(is_read_only(part) for _joiner, part in segments):
        return None
    for _joiner, part in segments:
        name, *args = shlex.split(part)
        if name in _METADATA_COMMANDS:
            continue
        for arg in args:
            path = arg.split("=", 1)[1] if arg.startswith("-") and "=" in arg else arg
            # As a directory too: ``grep -r x ~/.ssh`` prints the keys in it.
            for candidate in (path, f"{path.rstrip('/')}/"):
                reason = flag_file_read(candidate)
                if reason:
                    return reason
    return None


#: (pattern, reason) for shell commands. Searched case-insensitively
#: anywhere in the command, so a chained ``cd x && …`` is still seen.
_SHELL_RULES: tuple[tuple[re.Pattern[str], str], ...] = tuple(
    (re.compile(p, re.IGNORECASE), why)
    for p, why in (
        (
            r"\b(curl|wget|fetch)\b[^|;&]*\|\s*(sudo\s+)?(ba|z|da|k)?sh\b",
            "pipes a download straight into a shell",
        ),
        (
            r"\b(curl|wget)\b[^|;&]*\|\s*(sudo\s+)?python[0-9.]*\b",
            "pipes a download straight into Python",
        ),
        (
            r"\bgit\s+push\b[^;&|]*(\s--force\b|\s-f\b|\s--force-with-lease\b)",
            "force-pushes, rewriting shared history",
        ),
        (r"\bgit\s+push\b[^;&|]*\s\+\S+", "force-pushes a refspec"),
        (
            r"\b(mkfs(\.\w+)?|fdisk|parted|diskutil\s+(erase|partition)\w*)\b",
            "formats or repartitions a disk",
        ),
        (r"\bdd\b[^;&|]*\bof=/dev/", "writes raw data to a device"),
        (r">\s*/dev/(sd|disk|nvme|hd)", "overwrites a block device"),
        (
            r"\bchmod\s+(-R\s+)?0?777\s+/(etc|usr|bin|sbin|var|System|Library)?(?=\s|$|/)",
            "opens system files to everyone",
        ),
        (
            r"\b(crontab\b[^;&|]*-|launchctl\s+load|systemctl\s+enable)",
            "installs something that runs on its own",
        ),
        (
            r">>?\s*(\S*/)?\.(bashrc|zshrc|profile|bash_profile|zprofile)\b",
            "edits a shell start-up file",
        ),
        (
            r"\b(npm|pnpm|yarn)\s+publish\b|\btwine\s+upload\b|\bcargo\s+publish\b|\bgem\s+push\b|\bdocker\s+push\b",
            "publishes a package or image",
        ),
        (
            r"\bterraform\s+(destroy|apply\b[^;&|]*-auto-approve)",
            "changes or destroys infrastructure without review",
        ),
        (r"\bkubectl\s+(delete|drain|cordon)\b", "removes or disrupts cluster resources"),
        (
            r"\baws\s+\S+\s+(rm|delete\S*|terminate\S*)\b|\bgcloud\b[^;&|]*\bdelete\b|\baz\b[^;&|]*\bdelete\b",
            "deletes cloud resources",
        ),
        (
            r"\b(drop|truncate)\s+(table|database|schema)\b|\bdelete\s+from\s+\w+\s*;?\s*['\"]?\s*$",
            "drops or wipes database data",
        ),
        (
            r"(\.ssh/|id_rsa|id_ed25519|\.aws/credentials|\.netrc|\.npmrc|\.pypirc|\.env\b)[^|]*\|\s*(curl|wget|nc|ncat|scp)\b",
            "sends credentials over the network",
        ),
        (
            r"\b(env|printenv|set)\s*\|\s*(curl|wget|nc|ncat)\b",
            "sends environment variables over the network",
        ),
        (
            rf"\b({_READ_UTILS})\b[^|;&]*{_SECRET_IN_SHELL}",
            "reads a credential or secret file",
        ),
        (
            # An inline interpreter one-liner that both reaches the network AND
            # touches env/secrets — the code-shaped exfil a `curl` rule misses.
            r"\b(python[0-9.]*|node|ruby|perl|php)\b(?=[^;&|]*\s-[ce]\b)"
            r"(?=[^&|]*(requests|urllib|urlopen|http\.client|httpx|net/http|axios|fetch\(|socket\.|Net::HTTP))"
            r"(?=[^&|]*(environ|getenv|process\.env|\.ssh|credentials|\.env\b|ENV\[))",
            "runs an inline script that sends secrets or environment out",
        ),
        (r"\bscp\b[^;&|]*\S+@\S+:", "copies files to another machine"),
        (
            r"\b(setenforce\s+0|ufw\s+disable|systemctl\s+(stop|disable)\s+(firewalld|ufw|apparmor))\b",
            "switches off system security",
        ),
        (r"\bsudo\s+rm\b", "deletes files as root"),
        (
            r"\bgit\s+(reset\s+--hard\b"
            r"|clean\b(?=[^;&|]*\s-[a-z]*f)(?=[^;&|]*\s-[a-z]*d)(?![^;&|]*(\s-[a-z]*n|--dry-run))"
            r"|checkout\s+--\s+\.(/)?(?=\s|$|[;&|]))",
            "discards uncommitted work",
        ),
    )
)

#: Delete-style commands whose targets are judged by where they point.
_DELETERS = {"rm", "rmdir", "shred", "unlink", "trash"}

#: The downloaders the pipe rules above know, and the shells and
#: interpreters they know how to run what comes out of one. The
#: download-then-run check below covers the same pairs: the pipe rules
#: catch ``curl … | sh``, and this catches the same call written as a
#: download to a file and a run of that file (campaign R128).
_DOWNLOADERS: frozenset[str] = frozenset({"curl", "wget", "fetch"})

#: The shells — and the shell builtins that run a file in the shell
#: itself — that count as running what was fetched. The names are
#: matched in lower case, as the pipe rules above match them.
_SHELLS: frozenset[str] = frozenset({"sh", "bash", "zsh", "dash", "ksh", "source", "."})

#: The interpreters the pipe rules know: ``python``, ``python3``,
#: ``python3.12``.
_INTERPRETER = re.compile(r"python[0-9.]*")

#: Where one shell command ends and the next begins — the separators a
#: chained command is split on. A lone ``&`` is a separator too, but not
#: the ``&`` of a ``2>&1``. The separator is kept: a pipe carries what
#: the command before it wrote into the command after it.
_SEGMENT_SPLIT = re.compile(r"(&&|\|\||;|\n|\||(?<![>&])&(?!>))")

#: The options that name the file a download is written to.
_OUTPUT_OPTIONS: frozenset[str] = frozenset({"-o", "-O", "--output", "--output-document"})

#: The redirection operators in a command's words. A file after one of
#: them is the command's input, not its argument.
_REDIRECT_OPERATORS: frozenset[str] = frozenset(
    {"<", ">", ">>", "2>", "2>>", "&>", "1>", "1>>", "<<", "<<-", ">|"}
)

#: A command longer than this is not a download-then-run: the shapes
#: that rule catches are one or two short commands chained, and a long
#: command is not worth splitting.
_MAX_LENGTH = 4096


def flag_shell(command: str, project_dir: Path | None) -> str | None:
    """Why ``command`` needs a person's yes, or None when nothing matched."""
    text = command or ""
    if _igni_secret_in_shell().search(text):
        return "reads a credential or secret file"
    for pattern, why in _SHELL_RULES:
        if pattern.search(text):
            return why
    fetched = _downloads_then_runs(text)
    if fetched:
        return fetched
    outside = _deletes_outside(text, project_dir)
    if outside:
        return f"deletes {outside}, outside this project"
    return None


def _downloads_then_runs(command: str) -> str | None:
    """Why a command that fetches a file and then runs it needs a yes.

    The pipe rules catch ``curl … | sh``; the same call written as a
    download to a file and a run of that file (``curl -o x.sh && sh
    x.sh``) was not caught, and the file was run (campaign R128).

    One file, one command: the download must come before the run, and
    the run must be of the file the download wrote — a download of one
    file and a run of another is ordinary work and is left alone.

    What counts as the download's file: the option that names it
    (``-o``, ``-O``, ``--output``, ``--output-document``), the file the
    output is redirected to, or — for ``wget``, which names the file
    after the URL — the URL's last part. A ``curl`` that writes to
    standard output writes no file, so there is nothing for a later
    command to run.
    """
    if not command or len(command) > _MAX_LENGTH:
        return None
    downloaded: list[str | None] = []
    previous_files: list[str] = []
    previous_fetched = False
    for separator, segment in _split(command):
        words = _words(segment)
        tool = _tool_of(words)
        fetched = _downloaded_file(segment, words)
        wrote = _files_written(fetched)
        is_tee = tool.lower() == "tee"
        if is_tee:
            wrote = _tee_files(words)
        # A pipe carries what the command before it wrote into this one.
        piped = previous_files if separator == "|" else []
        carried = previous_fetched if separator == "|" else False
        if is_tee:
            if carried:
                # ``curl … | tee x.sh``: the fetched bytes land in a file.
                downloaded.extend(wrote)
        elif isinstance(fetched, str):
            downloaded.append(fetched)
        elif fetched is None:
            # The file's name is not known, so any later run of a file
            # counts.
            downloaded.append(None)
        run = _run_target(words, piped=piped)
        if run is not None:
            name, kind = run
            if any(written is None or written == name for written in downloaded):
                return _RUN_REASON[kind]
        previous_files = wrote
        previous_fetched = fetched is not _NOT_A_DOWNLOAD or (is_tee and carried)
    return None


#: Why the call was stopped, per kind of run. The pipe rules' own
#: wording is left alone: these are the shapes they never saw.
_RUN_REASON: dict[str, str] = {
    "shell": "downloads a script and runs it with a shell",
    "python": "downloads a script and runs it with Python",
}


class _Sentinel:
    """A value that says something about a download that no name can.

    Two answers are not file names: the segment is not a download at
    all, and the segment downloads but writes no file (a ``curl`` with
    no output option writes to standard output, so there is nothing for
    a later command to run).
    """

    def __init__(self, what: str) -> None:
        self.what = what

    def __repr__(self) -> str:
        return self.what


#: The segment is not a download at all.
_NOT_A_DOWNLOAD = _Sentinel("not a download")

#: The segment downloads, but writes no file.
_NO_FILE = _Sentinel("no file")


def _split(command: str) -> list[tuple[str, str]]:
    """The command's segments, each with the separator that introduced it.

    The separator is kept because a pipe is not a chain: what the
    command before it wrote is what the command after it reads.
    """
    parts = _SEGMENT_SPLIT.split(command)
    segments: list[tuple[str, str]] = []
    separator = ""
    for index, part in enumerate(parts):
        if index % 2:
            separator = part
            continue
        if part.strip():
            segments.append((separator, part))
    return segments


def _words(segment: str) -> list[str]:
    """The words of one segment, redirections removed."""
    text = _REDIRECTION.sub(" ", segment)
    text = _FD_REDIRECTION.sub(" ", text)
    try:
        return shlex.split(text)
    except ValueError:
        return text.split()


#: A redirection in a segment: ``> file`` writes the command's output
#: to ``file``, which is what a download does with it.
_REDIRECTION = re.compile(r"(?<![0-9<>])>>?\s*(\S+)")

#: A file-descriptor redirection, ``2>&1``: it names no file.
_FD_REDIRECTION = re.compile(r"\d*>&?\d*")


def _tool_and_rest(words: list[str]) -> tuple[str, list[str]]:
    """The command's tool and the words after it.

    ``sudo`` is skipped: it runs the command that follows it, so the
    tool is the one after the ``sudo``.
    """
    if not words:
        return "", []
    tool = _basename(words[0])
    rest = words
    if tool == "sudo" and len(words) > 1:
        rest = words[1:]
        tool = _basename(rest[0])
    return tool, rest


def _basename(word: str) -> str:
    """The last part of a path.

    Not :meth:`pathlib.Path.name`, which answers nothing for ``.`` —
    and ``.`` is the shell's own way of running a file.
    """
    name = word.rstrip("/").rsplit("/", 1)[-1]
    return name or word


def _tool_of(words: list[str]) -> str:
    """The command's tool, without the ``sudo`` in front of it."""
    return _tool_and_rest(words)[0]


def _files_written(fetched: str | None | _Sentinel) -> list[str]:
    """The file a download wrote, when it names one."""
    if isinstance(fetched, str):
        return [fetched]
    return []


def _downloaded_file(segment: str, words: list[str]) -> str | None | _Sentinel:
    """The file a segment downloads, or what it writes instead.

    The file's last part is returned when it is known. ``None`` means
    the segment downloads but the file's name is not known (a ``wget``
    with no output option writes the URL's last part into the working
    directory, and the URL may not name one), so any later run of a
    file counts. :data:`_NO_FILE` means the segment downloads but
    writes no file at all.
    """
    tool, rest = _tool_and_rest(words)
    if not tool or tool.lower() not in _DOWNLOADERS:
        return _NOT_A_DOWNLOAD
    for index, word in enumerate(rest[1:], start=1):
        if word in _OUTPUT_OPTIONS and index + 1 < len(rest):
            return _output_file(rest[index + 1])
        for option in ("--output=", "--output-document="):
            if word.startswith(option):
                return _output_file(word[len(option) :])
        if word.startswith("--"):
            continue
        if word[:2] in ("-o", "-O") and len(word) > 2:
            # curl's and wget's short options: ``-ofile``, ``-Ofile``.
            return _output_file(word[2:])
        if _writes_to_stdout(word):
            # ``wget -qO-``: the download goes to standard output.
            return _NO_FILE
    redirected = _REDIRECTION.search(segment)
    if redirected is not None:
        target = redirected.group(1)
        if target.startswith("/dev/"):
            # The output is thrown away, not written to a file.
            return _NO_FILE
        return Path(target).name
    url = _url_of(words)
    if tool.lower() in ("curl", "fetch"):
        # A curl with no output option writes to standard output, so
        # there is no file for a later command to run.
        return _NO_FILE
    if url:
        # wget names the file after the URL's path — when the URL names
        # no path, the name is not known, and any later run of a file
        # counts.
        path = url.split("://", 1)[-1].partition("/")[2]
        return Path(path.split("?")[0].rstrip("/")).name or None
    # A downloader with no URL fetches nothing.
    return _NO_FILE


def _output_file(value: str) -> str | _Sentinel:
    """The file an output option names — or that there is none."""
    if value == "-" or value.startswith("/dev/"):
        return _NO_FILE
    return Path(value).name


def _writes_to_stdout(word: str) -> bool:
    """True for the options that send the download to standard output."""
    return word.startswith("-") and word.lower().endswith("o-")


def _url_of(words: list[str]) -> str:
    """The URL a download fetches, if it names one."""
    for word in words:
        if "://" in word:
            return word
    return ""


def _tee_files(words: list[str]) -> list[str]:
    """The files a ``tee`` writes."""
    tool, rest = _tool_and_rest(words)
    if tool.lower() != "tee":
        return []
    names: list[str] = []
    for word in rest[1:]:
        if word.startswith("-"):
            continue
        names.append(Path(word).name)
    return names


def _run_target(words: list[str], piped: list[str]) -> tuple[str, str] | None:
    """The file a segment runs, and how — ``(name, kind)`` or None.

    A segment that runs a shell or an interpreter on a file, or that
    runs a file directly (``./x.sh``), is a run. The name is the file's
    last part, so a download to ``/tmp/x.sh`` and a run of
    ``/tmp/x.sh`` are the same file.

    ``piped`` holds the files the command before it wrote, when the
    two were joined by a pipe: a shell with no file of its own runs
    what came down the pipe.
    """
    tool, rest = _tool_and_rest(words)
    if not tool:
        return None
    name = tool.lower()
    if name in _SHELLS:
        kind = "shell"
    elif _INTERPRETER.fullmatch(name):
        kind = "python"
    elif "/" in rest[0] or rest[0].startswith("."):
        # A path run as a program: the fetched file, made executable.
        return Path(rest[0]).name, "shell"
    else:
        return None
    index = 1
    while index < len(rest):
        word = rest[index]
        if word.startswith("-"):
            if _INLINE_OPTION.fullmatch(word):
                # An inline command, not a file: there is nothing to run.
                return None
            index += 1
            continue
        if word in _REDIRECT_OPERATORS:
            if index + 1 >= len(rest):
                return None
            target = rest[index + 1]
            index += 2
            if word.startswith("<"):
                # ``sh < x.sh`` runs the file it is given.
                return Path(target).name, kind
            continue
        return Path(word).name, kind
    if piped:
        # Nothing of its own to run: it runs what came down the pipe.
        return piped[0], kind
    return None


#: An option that carries the command itself rather than a file:
#: ``-c``, ``-lc``, ``-e`` and its friends.
_INLINE_OPTION = re.compile(r"-[a-zA-Z]*c[a-zA-Z]*")


#: A secret riding in a URL: a shell variable the shell would expand, or a
#: query parameter whose name says what it carries.
_SECRET_IN_URL = re.compile(
    r"\$\{?[A-Za-z_][A-Za-z0-9_]*|[?&](access_?token|token|api_?key|key|secret|password|passwd|auth)=",
    re.IGNORECASE,
)


def flag_url(url: str) -> str | None:
    """Why fetching ``url`` needs a person's yes: it carries what looks like a secret."""
    if url and _SECRET_IN_URL.search(url):
        return "sends what looks like a secret or token in a URL"
    return None


def flag_write(path: str, project_dir: Path | None) -> str | None:
    """Why writing ``path`` needs a person's yes: it lands outside the project."""
    if not path or project_dir is None:
        return None
    target = _resolve(path, project_dir)
    if target is None:
        return None
    if not _is_within(target, project_dir):
        return f"writes {path}, outside this project"
    return None


def _deletes_outside(command: str, project_dir: Path | None) -> str | None:
    if project_dir is None:
        return None
    for segment in re.split(r"&&|\|\||;|\|", command):
        try:
            words = shlex.split(segment)
        except ValueError:
            words = segment.split()
        if not words:
            continue
        if words[0] == "sudo":
            words = words[1:]
        if not words or Path(words[0]).name not in _DELETERS:
            continue
        for arg in words[1:]:
            if arg.startswith("-"):
                continue
            if arg in ("/", "~", "~/", "*", "/*", ".."):
                return arg
            target = _resolve(arg, project_dir)
            if target is not None and not _is_within(target, project_dir):
                return arg
    return None


def _resolve(path: str, project_dir: Path) -> Path | None:
    try:
        expanded = Path(path).expanduser()
        return (expanded if expanded.is_absolute() else project_dir / expanded).resolve()
    except (OSError, RuntimeError, ValueError):
        return None


def _is_within(target: Path, project_dir: Path) -> bool:
    try:
        target.relative_to(project_dir.resolve())
        return True
    except ValueError:
        return False
