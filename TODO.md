# TODO

Contributions welcome, but expect rough edges.

## Testing

- [ ] Unit tests for `brr/state.py` (config parsing, project discovery, provider resolution)
- [ ] Unit tests for `brr/templates.py` (template resolution, rendering, overrides, staging)
- [ ] Unit tests for `brr/commands/init.py` (project scaffolding)
- [ ] Integration tests for template rendering end-to-end
- [ ] pytest configuration (`pyproject.toml` or `conftest.py`)

## CI/CD

- [ ] Add pre-commit config (ruff for linting + formatting)
- [ ] Add GitHub Actions workflow for lint/test on PR
- [ ] Add ruff config to `pyproject.toml`

## Provider Abstraction

- [x] Define a `Provider` base class (`brr/providers.py`) with `get_provider()` registry
- [x] Replace `if provider == "aws"` branches in `cluster.py` with polymorphic dispatch
- [x] Move shared SSH utilities to `brr/ssh.py` (were confusingly in `aws/nodes.py`)

## Validation & Error Handling

- [ ] Validate config values (e.g. `AWS_REGION` is a real region, `max_workers` is an int)
- [ ] Warn on unrecognized config keys (catch typos)
- [ ] Block on unresolved `{{VAR}}` placeholders instead of just warning
- [ ] Type-check template overrides before passing to Ray

## UX Improvements

- [ ] `nuke --cluster <name>` for targeted teardown (instead of destroying everything)
- [ ] `nuke --dry-run` to preview what would be deleted
- [ ] Better error messages when templates fail to render
