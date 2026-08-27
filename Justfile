set shell := ["bash", "-eu", "-o", "pipefail", "-c"]

default:
    @just --list

# Install dependencies
install:
    uv sync --extra dev
    uv run pre-commit install

# Install dependencies for CI (no pre-commit hooks)
ci-install:
    uv sync --locked --extra dev

# Format code
format:
    uv run ruff format .
    uv run ruff check --fix .

# Lint and check formatting (no changes)
check:
    uv run ruff check .
    uv run ruff format --check .

# Run tests
test:
    uv run pytest

# Run extension locally (app mode, reads config.json)
dev:
    uv run python main.py

# Run against the built-in PLC simulator (no hardware required)
demo:
    uv run python main.py demo

# Package for Zelos marketplace
package:
    zelos extensions package .

# Run all CI checks
ci:
    uv sync --extra dev
    just check
    just test
    just package

# Release: bump version, format, check, test, commit, tag
release VERSION:
    #!/usr/bin/env bash
    set -euo pipefail
    git diff-index --quiet HEAD || (echo "Uncommitted changes! Commit or stash first." && exit 1)
    uv run python scripts/bump_version.py "{{VERSION}}"
    just format
    uv lock
    just check
    just test
    git add -A
    git commit -m "Release v{{VERSION}}"
    git tag -a "v{{VERSION}}" -m "Release v{{VERSION}}"
    echo ""
    echo "✓ Release v{{VERSION}} ready!"
    echo ""
    echo "Push with: git push --follow-tags"

# Delete a GitHub release, its remote+local tag, and force-push
delete-release VERSION:
    #!/usr/bin/env bash
    set -euo pipefail
    tag="v{{VERSION}}"
    gh release delete "$tag" --yes 2>/dev/null && echo "✓ release deleted" || echo "- no release found"
    git push origin --delete "$tag" 2>/dev/null && echo "✓ remote tag deleted" || echo "- no remote tag"
    git tag -d "$tag" 2>/dev/null && echo "✓ local tag deleted" || echo "- no local tag"
    git push --force-with-lease
    echo ""
    echo "✓ Cleaned up $tag, ready to re-release"

# Clean build artifacts
clean:
    rm -rf dist build .pytest_cache .ruff_cache *.tar.gz .artifacts actions.json
    find . -type d -name "__pycache__" -exec rm -rf {} + 2>/dev/null || true
