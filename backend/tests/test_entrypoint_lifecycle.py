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


def test_source_restart_fails_closed_and_installs_checkpoint_schema_after_alembic():
    repository_root = Path(__file__).parents[2]
    restart = (repository_root / "restart.sh").read_text(encoding="utf-8")
    alembic = ".venv/bin/alembic upgrade head"
    checkpoint = ".venv/bin/python -m app.scripts.setup_langgraph_checkpoints"

    assert f"{alembic} 2>/dev/null || true" not in restart
    assert alembic in restart
    assert checkpoint in restart
    assert restart.index(alembic) < restart.index(checkpoint)


def test_source_restart_defaults_public_url_to_the_frontend_origin():
    repository_root = Path(__file__).parents[2]
    restart = (repository_root / "restart.sh").read_text(encoding="utf-8")

    assert ': "${PUBLIC_BASE_URL:=http://localhost:$FRONTEND_PORT}"' in restart
    assert "export PUBLIC_BASE_URL" in restart
