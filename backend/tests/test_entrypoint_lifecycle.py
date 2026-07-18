from pathlib import Path


def test_start_command_replaces_shell_so_app_receives_container_signals():
    entrypoint = (Path(__file__).parents[1] / "entrypoint.sh").read_text(encoding="utf-8")

    assert 'exec /bin/bash -c "exec $START_COMMAND"' in entrypoint
    assert 'exec /bin/bash -lc "exec $START_COMMAND"' not in entrypoint
    assert "--ws-max-size 67108864" in entrypoint
    assert "python -m app.scripts.setup_langgraph_checkpoints" in entrypoint
    assert entrypoint.index("alembic upgrade head") < entrypoint.index(
        "python -m app.scripts.setup_langgraph_checkpoints"
    )


def test_compose_backends_do_not_insert_signal_intercepting_init_process():
    repository_root = Path(__file__).parents[2]
    compose_paths = (
        "docker-compose.yml",
        "docker-compose.ci.yml",
        "deploy/docker-compose.yml",
        "deploy/docker-compose-multi.yml",
        "deploy/astra-poc/docker-compose.prod.yml",
    )

    for relative_path in compose_paths:
        compose = (repository_root / relative_path).read_text(encoding="utf-8")
        assert "init: true" not in compose
        assert "stop_grace_period: 30s" in compose
