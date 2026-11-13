from voicetype import config


def test_existing_config_gets_new_ide_defaults(tmp_path, monkeypatch):
    example = config.ROOT / "config.example.toml"
    (tmp_path / "config.example.toml").write_text(example.read_text(), encoding="utf-8")
    (tmp_path / "config.toml").write_text(
        '[inject]\nmethod = "clipboard"\nterminal_classes = ["my-terminal"]\n',
        encoding="utf-8",
    )
    monkeypatch.setattr(config, "ROOT", tmp_path)
    monkeypatch.setattr(config, "load_env", lambda: {})
    cfg = config.load()
    assert cfg.inject["terminal_classes"] == ["my-terminal"]
    assert cfg.inject["paste_shortcut"] == "auto"
    assert "code" in cfg.inject["terminal_editor_classes"]
    assert "com.microsoft.vscode" in cfg.inject["terminal_editor_classes"]


def test_explicit_editor_opt_out_survives_config_merge(tmp_path, monkeypatch):
    example = config.ROOT / "config.example.toml"
    (tmp_path / "config.example.toml").write_text(example.read_text(), encoding="utf-8")
    (tmp_path / "config.toml").write_text(
        '[inject]\npaste_shortcut = "ctrl+v"\nterminal_editor_classes = []\n',
        encoding="utf-8",
    )
    monkeypatch.setattr(config, "ROOT", tmp_path)
    monkeypatch.setattr(config, "load_env", lambda: {})
    cfg = config.load()
    assert cfg.inject["paste_shortcut"] == "ctrl+v"
    assert cfg.inject["terminal_editor_classes"] == []
