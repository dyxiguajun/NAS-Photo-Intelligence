"""Identify the checkout and configuration used to generate an artifact."""
from hashlib import sha256
from pathlib import Path
import subprocess


def checkout(root=None, *, release_tag=None):
    root = Path(__file__).resolve().parents[2] if root is None else root

    def git(*args):
        try:
            return subprocess.check_output(['git', '-C', str(root), *args], stderr=subprocess.PIPE)
        except subprocess.CalledProcessError as error:
            raise ValueError('Cannot identify the running Git checkout') from error

    head = git('rev-parse', 'HEAD').decode().strip()
    status = git('status', '--porcelain=v1', '-z', '--untracked-files=all')
    digest = sha256()
    for args in (('--cached', 'HEAD'), ()):
        diff = git('diff', '--no-ext-diff', '--no-textconv', '--binary', *args, '--')
        digest.update(len(diff).to_bytes(8, 'big'))
        digest.update(diff)
    result = {'git_head':head, 'git_dirty':bool(status), 'tracked_diff_sha256':digest.hexdigest()}
    if release_tag is not None:
        tags = git('tag', '--points-at', 'HEAD').decode().splitlines()
        if status or release_tag not in tags:
            raise ValueError('Formal planning requires a clean checkout at the specified release tag')
        result['release_tag'] = release_tag
    return result
