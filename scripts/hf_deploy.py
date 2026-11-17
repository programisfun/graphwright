#!/usr/bin/env python
"""Deploy graphwright to a Hugging Face Space (Docker, free CPU, no card).

    # once: add HF_TOKEN=hf_... to scripts/.env  (write token, no card needed)
    uv run python scripts/hf_deploy.py                # create/update + deploy
    uv run python scripts/hf_deploy.py --status       # just report state

Secrets (NEO4J_*, MCP_AUTH_TOKEN) are pushed as Space secrets, never as repo files.
The Space listens on 7860, which is what HF routes to.
"""

from __future__ import annotations

import argparse
import pathlib
import sys
import time

from deploy_common import client_secret, read_env, require

SPACE_NAME = 'graphwright'
APP_PORT = 7860

# Never upload these: secrets and local state.
IGNORE = [
    '.env',
    'scripts/.env',
    'scripts/.env.deploy',
    '.git/**',
    '.venv/**',
    '**/__pycache__/**',
    '*.pyc',
    '.venv',
]


def token() -> str:
    return client_secret(
        'HF_TOKEN',
        [pathlib.Path(__file__).parent / '.env'],
        hint=(
            'Create a write token at https://huggingface.co/settings/tokens '
            '(no card required) and add HF_TOKEN=hf_... to scripts/.env'
        ),
    )


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument('--status', action='store_true', help='only report state')
    args = parser.parse_args()

    from huggingface_hub import HfApi

    api = HfApi(token=token())
    user = api.whoami()['name']
    repo_id = f'{user}/{SPACE_NAME}'

    if args.status:
        info = api.space_info(repo_id)
        print(f'space : {info.id}')
        print(f'stage : {getattr(info.runtime, "stage", None)}')
        print(f'url   : https://{user}-{SPACE_NAME}.hf.space')
        return

    env = read_env('.env')
    require(
        env,
        ('NEO4J_URI', 'NEO4J_USER', 'NEO4J_PASSWORD', 'NEO4J_DATABASE', 'MCP_AUTH_TOKEN'),
        source='.env',
    )

    print(f'Creating/updating Space {repo_id} (docker, free cpu-basic)')
    api.create_repo(
        repo_id=repo_id,
        repo_type='space',
        space_sdk='docker',
        exist_ok=True,
        private=False,
    )

    print('Uploading files (secrets excluded)')
    api.upload_folder(
        repo_id=repo_id,
        repo_type='space',
        folder_path='.',
        ignore_patterns=IGNORE,
        commit_message='Deploy graphwright',
    )

    uploaded = api.list_repo_files(repo_id, repo_type='space')
    leaked = [name for name in uploaded if name == '.env' or name.endswith('/.env')]
    if leaked:
        sys.exit(f'Refusing to continue: security-sensitive files were uploaded: {leaked}')

    # A Docker Space gets its SDK/port from README front matter. Our project README has
    # none, so write the Space's own README with the required metadata.
    space_readme = (
        '---\n'
        'title: graphwright\n'
        'emoji: 🕸️\n'
        'colorFrom: gray\n'
        'colorTo: indigo\n'
        'sdk: docker\n'
        f'app_port: {APP_PORT}\n'
        'pinned: false\n'
        '---\n\n'
        '# graphwright\n\n'
        'MCP server that reads and edits a Neo4j graph with plain Cypher.\n'
        'Endpoint: `/mcp` (bearer token required). Health: `/health`.\n'
    )
    api.upload_file(
        path_or_fileobj=space_readme.encode(),
        path_in_repo='README.md',
        repo_id=repo_id,
        repo_type='space',
        commit_message='Space metadata',
    )

    print('Setting secrets')
    for key in ('NEO4J_URI', 'NEO4J_USER', 'NEO4J_PASSWORD', 'NEO4J_DATABASE', 'MCP_AUTH_TOKEN'):
        api.add_space_secret(repo_id=repo_id, key=key, value=env[key])

    print('Setting variables')
    api.add_space_variable(repo_id=repo_id, key='MCP_HOST', value='0.0.0.0')
    api.add_space_variable(repo_id=repo_id, key='MCP_PORT', value=str(APP_PORT))
    api.add_space_variable(repo_id=repo_id, key='MCP_STATELESS', value='1')

    print('Waiting for the Space to build and start (first build takes a few minutes)')
    deadline = time.time() + 900
    last = None
    while time.time() < deadline:
        stage = getattr(api.space_info(repo_id).runtime, 'stage', None)
        if stage != last:
            print(f'  stage: {stage}', flush=True)
            last = stage
        if stage in ('RUNNING', 'ERROR', 'STOPPED', 'PAUSED'):
            break
        time.sleep(15)

    url = f'https://{user}-{SPACE_NAME}.hf.space'
    print(f'\nSpace URL : {url}')
    print(f'MCP       : {url}/mcp')
    print(f'Health    : {url}/health')
    if last == 'RUNNING':
        print('\nToken: the MCP_AUTH_TOKEN from your .env (also set as a Space secret).')
        sys.exit(0)
    sys.exit(f'Space did not reach RUNNING (last stage: {last}). Check the build logs on huggingface.co.')


if __name__ == '__main__':
    main()
