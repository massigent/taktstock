#!/usr/bin/env python3
"""
Unit Tests for Codex Error Classification in BrainstormManager
--------------------------------------------------------------
Verifica che brainstorm_manager.py distingua con precisione:
1. Binario assente (FileNotFoundError / codex not found / 127) -> messaggio di indisponibilità runtime
2. Timeout (TimeoutExpired / timed out) -> messaggio di timeout
3. Errore di autenticazione (401 / unauthorized / session expired) -> messaggio di sessione non valida/scaduta
4. Rate limit reale (429 / quota exceeded / usage limit) -> messaggio di quota esaurita
"""

import os
import sys
import subprocess
import unittest
from pathlib import Path
from unittest.mock import patch, MagicMock

SERVER_DIR = Path(__file__).resolve().parent.parent / "server"
sys.path.insert(0, str(SERVER_DIR))

from brainstorm_manager import classify_codex_error, BrainstormManager
from account_manager import CodexAccount, CodexAccountManager


class TestCodexErrorClassification(unittest.TestCase):
    def setUp(self):
        # Questi test verificano il percorso legacy con subprocess mockato: non devono
        # mai contattare sidecar o provider reali, né consumare quota del server.
        self._sidecar_env = patch.dict(os.environ, {
            "TAKTSTOCK_HOST_CODEX_SIDECAR": "0",
            "UFFICIO_HOST_CODEX_SIDECAR": "0",
            "PREFER_GEMINI": "0",
            "GEMINI_API_KEY": "",
            "GOOGLE_API_KEY": "",
            "OPENAI_API_KEY": "",
            "DEEPSEEK_API_KEY": "",
            "OPENROUTER_API_KEY": "",
            "ANTHROPIC_API_KEY": "",
        })
        self._sidecar_env.start()
        self._sidecar_mode_guard = patch(
            "infrastructure.agent_gateway.is_sidecar_mode_enabled",
            return_value=False,
        )
        self._sidecar_mode_guard.start()
        self._network_guard = patch(
            "urllib.request.urlopen",
            side_effect=AssertionError("I test unitari non possono contattare provider esterni."),
        )
        self._network_guard.start()

    def tearDown(self):
        self._network_guard.stop()
        self._sidecar_mode_guard.stop()
        self._sidecar_env.stop()

    def mock_account_manager(self, manager):
        return patch("account_manager.CodexAccountManager", return_value=manager)

    def test_classify_binary_missing_exception(self):
        """FileNotFoundError is classified as binary_missing with administrator notice."""
        exc = FileNotFoundError(2, "No such file or directory: 'codex'")
        category, msg = classify_codex_error(exc=exc)
        self.assertEqual(category, "binary_missing")
        self.assertIn("Codex CLI is not available in the Taktstock runtime", msg)
        self.assertNotIn("weekly quota", msg)

    def test_classify_binary_missing_exit_code_and_stderr(self):
        """Exit code 127 or 'codex: not found' is classified as binary_missing."""
        category, msg = classify_codex_error(returncode=127, output="sh: 1: codex: not found\n")
        self.assertEqual(category, "binary_missing")
        self.assertIn("Codex CLI is not available in the Taktstock runtime", msg)

        category2, msg2 = classify_codex_error(returncode=1, output="env: ‘codex’: No such file or directory\n")
        self.assertEqual(category2, "binary_missing")
        self.assertIn("Codex CLI is not available in the Taktstock runtime", msg2)

    def test_classify_timeout(self):
        """TimeoutExpired is classified as timeout with dedicated message."""
        exc = subprocess.TimeoutExpired(cmd=["codex", "exec"], timeout=90)
        category, msg = classify_codex_error(exc=exc)
        self.assertEqual(category, "timeout")
        self.assertIn("Codex request timed out", msg)
        self.assertNotIn("weekly quota", msg)

    def test_classify_auth_error(self):
        """401/unauthorized/session expired errors are classified as auth_error."""
        outputs = [
            "401 Unauthorized: Invalid API key or token",
            "Authentication failed: Session expired. Please login again.",
            "auth.json missing or token not authenticated",
        ]
        for out in outputs:
            category, msg = classify_codex_error(returncode=1, output=out)
            self.assertEqual(category, "auth_error")
            self.assertIn("Codex authentication error", msg)
            self.assertNotIn("weekly quota", msg)

    def test_classify_rate_limit(self):
        """429/quota exceeded errors are classified as rate_limit."""
        outputs = [
            "429 Too Many Requests: Rate limit reached for model",
            "insufficient_quota: Quota exceeded for this organization",
            "usage limit reached for token window",
        ]
        for out in outputs:
            category, msg = classify_codex_error(returncode=1, output=out)
            self.assertEqual(category, "rate_limit")
            self.assertIn("weekly quota exhausted", msg)

    def test_call_llm_binary_missing_returns_clear_runtime_error(self):
        """_query_agent_llm for @sol with missing codex binary returns runtime warning."""
        acc = CodexAccount(name="sol")
        am = CodexAccountManager(accounts=[acc])

        with tempfile_brainstorm_manager() as bm:
            bm.account_manager = am
            with self.mock_account_manager(am), patch("subprocess.run", side_effect=FileNotFoundError("No such file or directory: 'codex'")):
                response = bm._query_agent_llm(agent="sol", messages=[{"role": "user", "content": "Test"}])
                self.assertIn("Codex CLI is not available in the Taktstock runtime", response)
                self.assertNotIn("weekly quota", response)

    def test_call_llm_timeout_returns_timeout_error(self):
        """_query_agent_llm for @sol with TimeoutExpired returns timeout error."""
        acc = CodexAccount(name="sol")
        am = CodexAccountManager(accounts=[acc])

        with tempfile_brainstorm_manager() as bm:
            bm.account_manager = am
            with self.mock_account_manager(am), patch("subprocess.run", side_effect=subprocess.TimeoutExpired(cmd=["codex"], timeout=90)):
                response = bm._query_agent_llm(agent="sol", messages=[{"role": "user", "content": "Test"}])
                self.assertIn("Codex request timed out", response)

    def test_call_llm_auth_error_returns_auth_message(self):
        """_query_agent_llm for @sol with 401 returns authentication error."""
        acc = CodexAccount(name="sol")
        am = CodexAccountManager(accounts=[acc])
        mock_proc = MagicMock(returncode=1, stdout="", stderr="401 Unauthorized: Session token expired")

        with tempfile_brainstorm_manager() as bm:
            bm.account_manager = am
            with self.mock_account_manager(am), patch("subprocess.run", return_value=mock_proc):
                response = bm._query_agent_llm(agent="sol", messages=[{"role": "user", "content": "Test"}])
                self.assertIn("Codex authentication error", response)

    def test_sol_cli_args_standard_preset_uses_low_reasoning_effort(self):
        """Sol con preset standard (o default) invoca Codex con model_reasoning_effort='low'."""
        acc = CodexAccount(name="sol")
        am = CodexAccountManager(accounts=[acc])
        mock_proc = MagicMock(returncode=0, stdout="Risposta Sol standard", stderr="")

        with tempfile_brainstorm_manager() as bm:
            bm.account_manager = am
            with self.mock_account_manager(am), patch("subprocess.run", return_value=mock_proc) as mock_subproc:
                bm._query_agent_llm(agent="sol", messages=[{"role": "user", "content": "Test standard"}], preset="standard")
                self.assertTrue(mock_subproc.called)
                cmd_args = mock_subproc.call_args[0][0]
                self.assertIn("gpt-5.6-sol", cmd_args)
                self.assertIn('-c', cmd_args)
                self.assertIn('model_reasoning_effort="low"', cmd_args)

    def test_sol_cli_args_critical_preset_uses_high_reasoning_effort(self):
        """Sol con preset critical o escalation invoca Codex con model_reasoning_effort='high'."""
        acc = CodexAccount(name="sol")
        am = CodexAccountManager(accounts=[acc])
        mock_proc = MagicMock(returncode=0, stdout="Risposta Sol critical", stderr="")

        with tempfile_brainstorm_manager() as bm:
            bm.account_manager = am
            with self.mock_account_manager(am), patch("subprocess.run", return_value=mock_proc) as mock_subproc:
                bm._query_agent_llm(agent="sol", messages=[{"role": "user", "content": "Test critical"}], preset="critical")
                self.assertTrue(mock_subproc.called)
                cmd_args = mock_subproc.call_args[0][0]
                self.assertIn("gpt-5.6-sol", cmd_args)
                self.assertIn('-c', cmd_args)
                self.assertIn('model_reasoning_effort="high"', cmd_args)

    def test_luna_cli_args_preserves_default_without_effort_override(self):
        """Luna invoca gpt-5.6-terra senza alcun override di model_reasoning_effort."""
        acc = CodexAccount(name="luna")
        am = CodexAccountManager(accounts=[acc])
        mock_proc = MagicMock(returncode=0, stdout="Risposta Luna", stderr="")

        with tempfile_brainstorm_manager() as bm:
            bm.account_manager = am
            with self.mock_account_manager(am), patch("subprocess.run", return_value=mock_proc) as mock_subproc:
                bm._query_agent_llm(agent="luna", messages=[{"role": "user", "content": "Test luna"}], preset="critical")
                self.assertTrue(mock_subproc.called)
                cmd_args = mock_subproc.call_args[0][0]
                self.assertIn("gpt-5.6-terra", cmd_args)
                self.assertNotIn('model_reasoning_effort="low"', cmd_args)
                self.assertNotIn('model_reasoning_effort="high"', cmd_args)

    def test_orchestrator_call_codex_profile_sol_reasoning_effort(self):
        """MultiAgentRunner configura reasoning effort 'low' di default e 'high' per Sol critical."""
        from orchestrator_core import MultiAgentRunner
        runner_std = MultiAgentRunner(workspace_path="/tmp", preset="standard", mock_mode=False)
        runner_std.run_cmd = MagicMock(return_value='{"ok": true}')
        runner_std.account_manager = MagicMock(accounts=[CodexAccount(name="sol")], get_current_account=lambda: CodexAccount(name="sol"), get_account_for_role=lambda r: CodexAccount(name="sol"), apply_account_env=lambda env, account=None: env, handle_possible_error=lambda out: False)

        runner_std.call_codex_profile(profile="sol", prompt="Test prompt standard")
        std_cmd = runner_std.run_cmd.call_args[0][0]
        self.assertIn('-c', std_cmd)
        self.assertIn('model_reasoning_effort="low"', std_cmd)

        runner_crit = MultiAgentRunner(workspace_path="/tmp", preset="critical", mock_mode=False)
        runner_crit.run_cmd = MagicMock(return_value='{"ok": true}')
        runner_crit.account_manager = MagicMock(accounts=[CodexAccount(name="sol")], get_current_account=lambda: CodexAccount(name="sol"), get_account_for_role=lambda r: CodexAccount(name="sol"), apply_account_env=lambda env, account=None: env, handle_possible_error=lambda out: False)

        runner_crit.call_codex_profile(profile="sol", prompt="Test prompt critical")
        crit_cmd = runner_crit.run_cmd.call_args[0][0]
        self.assertIn('-c', crit_cmd)
        self.assertIn('model_reasoning_effort="high"', crit_cmd)

        runner_std.call_codex_profile(profile="ds-pro", prompt="Test ds-pro")
        dspro_cmd = runner_std.run_cmd.call_args[0][0]
        self.assertNotIn('model_reasoning_effort="low"', dspro_cmd)
        self.assertNotIn('model_reasoning_effort="high"', dspro_cmd)

    def test_brainstorm_run_round_standard_preset_uses_low_effort(self):
        """BrainstormManager.run_round con preset standard invoca call_director con high_effort=False."""
        mock_runner = MagicMock()
        mock_runner.call_director.return_value = {
            "phase": "brainstorm",
            "analysis": "Test analysis",
            "risks": [],
            "strategy": "Modulare",
            "reviewers": [],
            "questions_for_user": [],
            "recommended_option": "Modulare",
            "consensus": ["Ok"],
            "disagreements": [],
            "open_questions": []
        }
        mock_runner.preset_config = {"reviewers": []}
        mock_runner.agent_policy = {"reviewers": {"automatic_selection_by_sol": False}}

        with tempfile_brainstorm_manager() as bm:
            bs_id = bm.create_brainstorm(task="Task standard", preset="standard")
            bm.run_round(bs_id, runner=mock_runner)
            self.assertTrue(mock_runner.call_director.called)
            # Verifica che tutte le chiamate a call_director abbiano high_effort=False
            for call in mock_runner.call_director.call_args_list:
                _, kwargs = call
                self.assertFalse(kwargs.get("high_effort", False))

    def test_brainstorm_run_round_critical_preset_uses_high_effort(self):
        """BrainstormManager.run_round con preset critical invoca call_director con high_effort=True."""
        mock_runner = MagicMock()
        mock_runner.call_director.return_value = {
            "phase": "brainstorm",
            "analysis": "Test analysis",
            "risks": [],
            "strategy": "Modulare critico",
            "reviewers": [],
            "questions_for_user": [],
            "recommended_option": "Modulare critico",
            "consensus": ["Ok"],
            "disagreements": [],
            "open_questions": []
        }
        mock_runner.preset_config = {"reviewers": []}
        mock_runner.agent_policy = {"reviewers": {"automatic_selection_by_sol": False}}

        with tempfile_brainstorm_manager() as bm:
            bs_id = bm.create_brainstorm(task="Task critical", preset="critical")
            bm.run_round(bs_id, runner=mock_runner)
            self.assertTrue(mock_runner.call_director.called)
            # Verifica che le chiamate a call_director abbiano high_effort=True
            for call in mock_runner.call_director.call_args_list:
                _, kwargs = call
                self.assertTrue(kwargs.get("high_effort", False))

    def test_bash_orchestrator_call_director_reasoning_effort(self):
        """Test funzionale bash: call_director usa low di default e high per escalation."""
        import tempfile
        import stat

        with tempfile.TemporaryDirectory() as tmpdir:
            tmp_path = Path(tmpdir)
            log_file = tmp_path / "codex_calls.log"
            orch_home = tmp_path / "orch"
            (orch_home / "logs").mkdir(parents=True, exist_ok=True)
            (orch_home / "state").mkdir(parents=True, exist_ok=True)

            # Crea mock eseguibile di codex che logga gli argomenti e restituisce JSON
            mock_codex = tmp_path / "codex"
            mock_codex.write_text(f"""#!/bin/bash
echo "$@" >> "{log_file}"
if [[ "$*" == *"escalate_test"* ]]; then
  echo \x27{{"phase":"brainstorm","escalate":true,"analysis":"escalate needed"}}\x27
else
  echo \x27{{"phase":"brainstorm","escalate":false,"analysis":"standard ok"}}\x27
fi
""")
            mock_codex.chmod(mock_codex.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)

            # Crea mock eseguibile di jq per rendere il test auto-consistente su qualsiasi host
            mock_jq = tmp_path / "jq"
            mock_jq.write_text("""#!/usr/bin/env python3
import sys, json

try:
    data = json.load(sys.stdin)
    query = sys.argv[-1] if len(sys.argv) > 1 else ""
    if "escalate" in query:
        val = data.get("escalate", False)
        print("true" if val is True else "false")
    else:
        print("null")
except Exception:
    print("false")
""")
            mock_jq.chmod(mock_jq.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)

            orchestrator_sh = Path(__file__).resolve().parent.parent / "orchestrator.sh"
            test_script = f"""
set -euo pipefail
export PATH="{tmpdir}:$PATH"
export PROJECT_DIR="{tmpdir}"
export ORCH_HOME="{orch_home}"
[[ "$(command -v codex)" == "{mock_codex}" ]]
[[ "$(command -v jq)" == "{mock_jq}" ]]
source "{orchestrator_sh}"

# Test 1: Chiamata standard (Sol/director -> low reasoning)
PRESET="standard" DIR_PROFILE="director" SOL_PROFILE="sol" call_director "Test prompt standard" "director" >/dev/null

# Test 2: Chiamata con escalation (da director a sol -> high reasoning)
PRESET="standard" DIR_PROFILE="director" SOL_PROFILE="sol" call_director "escalate_test" "director" >/dev/null

# Test 3: Chiamata critical (preset critical -> high reasoning)
PRESET="critical" DIR_PROFILE="sol" SOL_PROFILE="sol" call_director "critical test" "sol" >/dev/null
"""
            proc = subprocess.run(["bash", "-c", test_script], capture_output=True, text=True, timeout=15)
            self.assertEqual(proc.returncode, 0, f"Bash execution failed: {proc.stderr}")

            self.assertTrue(log_file.exists(), "Il log delle chiamate a codex non è stato generato")
            calls = log_file.read_text().strip().split("\n")
            self.assertEqual(len(calls), 4, f"Numero chiamate inatteso: {calls}")
            # Call 1 (standard director): low reasoning
            self.assertIn('model_reasoning_effort="low"', calls[0])
            # Call 2 (director standard prima di escalation): low reasoning
            self.assertIn('model_reasoning_effort="low"', calls[1])
            # Call 3 (escalation a sol): high reasoning
            self.assertIn('model_reasoning_effort="high"', calls[2])
            self.assertIn('--profile sol', calls[2])
            # Call 4 (critical preset sol): high reasoning
            self.assertIn('model_reasoning_effort="high"', calls[3])


from contextlib import contextmanager

@contextmanager
def tempfile_brainstorm_manager():
    import tempfile
    import shutil
    tmp = tempfile.mkdtemp()
    try:
        yield BrainstormManager(state_dir=Path(tmp))
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    unittest.main()
