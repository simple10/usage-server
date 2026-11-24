# usage-server tasks. `just` is optional; each recipe is a thin wrapper over scripts/.
default:
    @just --list

# Run plugin (in docker) and server tests
test:
    scripts/build-plugin.sh test
    cd server && uv run pytest -q

# Build the plugin .so only
plugin:
    scripts/build-plugin.sh

# Build image usage-server:<VERSION> and the plugin .so (what a host runs)
build *args:
    scripts/build-host.sh {{args}}

# Bump VERSION, test, commit, tag vX.Y.Z and push
release version *args:
    scripts/release.sh {{version}} {{args}}
