"""Shared pytest fixtures for MXWbot tests."""

from __future__ import annotations

import tempfile
from pathlib import Path

import pytest


@pytest.fixture
def tmp_workspace() -> Path:
    """A temporary workspace directory that is cleaned up after the test."""
    with tempfile.TemporaryDirectory(prefix="mxwbot_test_") as td:
        yield Path(td)


@pytest.fixture
def sample_yaml_config() -> str:
    return """\
workspace: .mxwbot
log_level: DEBUG

providers:
  - name: openai
    api_key: sk-test-openai
    model: gpt-4
    max_tokens: 4096
    temperature: 0.7
  - name: anthropic
    api_key: sk-ant-test
    model: claude-opus-4-6
    max_tokens: 8192

channels:
  - type: wechat
    enabled: false
  - type: email
    enabled: true
    settings:
      imap_host: imap.example.com
      smtp_host: smtp.example.com
      username: test@example.com
      password: secret123

tools:
  filesystem_enabled: true
  allowed_dirs:
    - /tmp
    - ./workspace
  shell:
    enabled: true
    timeout_seconds: 30

agent:
  max_iterations: 5
  timeout_seconds: 120
"""
