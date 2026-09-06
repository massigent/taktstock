#!/usr/bin/env python3
"""
Unit Tests for Sidecar Ready Protocol, Atomic Token Provisioner and Child Environment Sanitization
--------------------------------------------------------------------------------------------------
Verifica:
1. provision_runtime_token.py:
   - Scrittura atomica del token runtime con permessi 0400 (sola lettura owner).
   - Rifiuto di token mancanti o inferiori a 32 caratteri.
   - Fallimento pulito e sicuro se la directory di destinazione non esiste.
   - Protezione anti-symlink: se la destinazione è un symlink verso un file esterno, il provisioning
     fallisce categoricamente e il file target esterno resta intatto.
2. wait_for_sidecar.py:
   - Riconoscimento dello stato 'ready' su risposta positiva dal socket.
   - Rifiuto se socket assente, token assente o handshake fallito.
3. host_codex_socket_daemon.py:
   - Validazione azione 'ready' e 'health' senza richiedere worktree/prompt/profile.
   - Fail-closed su token non valido per azione 'ready'.
   - Risposta immediata al client senza acquisizione del lock dei job (job_lock).
   - Creazione del socket Unix con permessi 0600.
   - Sanitizzazione dell'ambiente figlio: rimozione di UFFICIO_SIDECAR_TOKEN, UFFICIO_SIDECAR_TOKEN_FILE,
     DASHBOARD_PASSWORD e UFFICO_AUTH_TOKEN prima di invocare il binario Codex.
   - Unica sorgente master /home/massimo/.config/ufficio-codex/sidecar.env (nessun fallback /etc).
"""

import os
import sys
import json
import stat
import uuid
import socket
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch, MagicMock

SERVER_DIR = Path(__file__).resolve().parent.parent / "server"
sys.path.insert(0, str(SERVER_DIR))
sys.path.insert(0, str(SERVER_DIR / "infrastructure"))

from infrastructure.host_codex_socket_daemon import (
    validate_sidecar_request,
    load_sidecar_token,
    HostCodexDaemon,
)
from infrastructure.provision_runtime_token import provision_token
from infrastructure.wait_for_sidecar import check_ready


class TestProvisionRuntimeToken(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.tmp_path = Path(self.temp_dir.name)
        self.dest_file = self.tmp_path / "token"
        self.env_file = self.tmp_path / "sidecar.env"
        self.valid_dummy_token = "dummy_secret_token_12345678901234567890123456"

    def tearDown(self):
        self.temp_dir.cleanup()

    def test_successful_provision_with_0400_permissions(self):
        """1. Scrittura atomica del token con permessi 0400 (sola lettura owner)."""
        self.env_file.write_text(f"UFFICIO_SIDECAR_TOKEN={self.valid_dummy_token}\n", encoding="utf-8")
        success = provision_token(self.env_file, self.dest_file)
        self.assertTrue(success)
        self.assertTrue(self.dest_file.exists())

        # Verifica contenuto
        self.assertEqual(self.dest_file.read_text(encoding="utf-8").strip(), self.valid_dummy_token)

        # Verifica permessi 0400
        mode = stat.S_IMODE(self.dest_file.stat().st_mode)
        self.assertEqual(mode, 0o400)

        # Verifica assenza di file temporanei residui
        tmp_files = list(self.tmp_path.glob(".token.tmp.*"))
        self.assertEqual(len(tmp_files), 0)

    def test_provision_fails_when_token_is_missing_or_short(self):
        """2. Fallimento e nessun file creato se il token è assente o < 32 caratteri."""
        self.env_file.write_text("UFFICIO_SIDECAR_TOKEN=short_weak_tok\n", encoding="utf-8")
        success = provision_token(self.env_file, self.dest_file)
        self.assertFalse(success)
        self.assertFalse(self.dest_file.exists())

    def test_provision_fails_when_dest_dir_missing(self):
        """3. Fallimento pulito se la directory di destinazione non esiste."""
        self.env_file.write_text(f"UFFICIO_SIDECAR_TOKEN={self.valid_dummy_token}\n", encoding="utf-8")
        non_existent_dest = self.tmp_path / "missing_dir" / "token"
        success = provision_token(self.env_file, non_existent_dest)
        self.assertFalse(success)
        self.assertFalse(non_existent_dest.exists())

    def test_provision_rejects_symlink_destination_and_preserves_target(self):
        """3b. Se la destinazione è un symlink verso un file esterno, il provisioning fallisce e il target resta intatto."""
        # Crea un file bersaglio esterno sensibile
        external_target = self.tmp_path / "external_target_file.txt"
        original_target_content = "ORIGINAL_UNMODIFIED_CONTENT_12345"
        external_target.write_text(original_target_content, encoding="utf-8")

        # Crea la cartella runtime fittizia e crea un symlink 'token' che punta a external_target
        runtime_dir = self.tmp_path / "runtime"
        runtime_dir.mkdir(parents=True, exist_ok=True)
        symlink_dest = runtime_dir / "token"
        symlink_dest.symlink_to(external_target)

        self.assertTrue(symlink_dest.is_symlink())

        self.env_file.write_text(f"UFFICIO_SIDECAR_TOKEN={self.valid_dummy_token}\n", encoding="utf-8")
        success = provision_token(self.env_file, symlink_dest)

        # Il provisioning DEVE fallire
        self.assertFalse(success)

        # Il file esterno DEVE essere rimasto esattamente invariato
        self.assertTrue(external_target.exists())
        self.assertEqual(external_target.read_text(encoding="utf-8"), original_target_content)


class TestSidecarReadyProtocolAndDaemon(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.tmp_path = Path(self.temp_dir.name)
        self.sock_path = Path("/tmp") / f"sc_ready_{uuid.uuid4().hex[:8]}.sock"
        self.token = "valid_sidecar_auth_token_1234567890123456"
        self.token_file = self.tmp_path / "token"
        self.token_file.write_text(self.token, encoding="utf-8")
        self.daemon = HostCodexDaemon(
            socket_path=self.sock_path,
            auth_token=self.token,
            allowed_roots=[self.tmp_path]
        )

    def tearDown(self):
        self.daemon.running = False
        if self.sock_path.exists():
            try:
                self.sock_path.unlink()
            except Exception:
                pass
        self.temp_dir.cleanup()

    def test_validate_sidecar_request_ready_and_health_actions(self):
        """4. Validazione rapida per action='ready' e action='health' senza worktree né prompt."""
        # Successo con token valido
        valid, err, sanitized = validate_sidecar_request({"auth_token": self.token, "action": "ready"}, self.token)
        self.assertTrue(valid)
        self.assertEqual(sanitized, {"action": "ready"})

        valid, err, sanitized = validate_sidecar_request({"auth_token": self.token, "action": "health"}, self.token)
        self.assertTrue(valid)
        self.assertEqual(sanitized, {"action": "ready"})

        # Rifiuto con token errato
        valid, err, _ = validate_sidecar_request({"auth_token": "wrong_token", "action": "ready"}, self.token)
        self.assertFalse(valid)
        self.assertIn("Autenticazione", err)

    def test_socket_creation_permissions_0600(self):
        """5. Il demone crea il socket Unix con permessi restrittivi 0600."""
        server_thread = threading.Thread(target=self.daemon.run, daemon=True)
        server_thread.start()

        import time
        mode = 0
        for _ in range(50):
            if self.sock_path.exists():
                mode = stat.S_IMODE(self.sock_path.stat().st_mode)
                if mode == 0o600:
                    break
            time.sleep(0.05)

        self.assertTrue(self.sock_path.exists())
        if sys.platform != "darwin":
            self.assertEqual(mode, 0o600)
        self.daemon.running = False

    def test_ready_action_ipc_response_and_zero_codex_execution(self):
        """6. L'azione 'ready' risponde immediatamente SUCCESS con ready: True senza occupare il lock dei job."""
        server_thread = threading.Thread(target=self.daemon.run, daemon=True)
        server_thread.start()

        import time
        connected = False
        client = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        for _ in range(50):
            try:
                client.connect(str(self.sock_path))
                connected = True
                break
            except (ConnectionRefusedError, FileNotFoundError):
                time.sleep(0.05)
        self.assertTrue(connected, "Impossibile connettersi al socket del demone")

        with patch.object(self.daemon, "execute_codex") as mock_exec:
            req = {"auth_token": self.token, "action": "ready"}
            client.sendall((json.dumps(req) + "\n").encode("utf-8"))
            resp_raw = client.recv(4096).decode("utf-8")
            client.close()


            resp = json.loads(resp_raw)
            self.assertEqual(resp.get("status"), "SUCCESS")
            self.assertTrue(resp.get("ready"))
            self.assertEqual(resp.get("service"), "taktstock-codex")

            # Verifica che execute_codex NON sia stato chiamato
            mock_exec.assert_not_called()
            # Verifica che il lock dei job sia rimasto libero
            self.assertFalse(self.daemon.job_lock.locked())

        self.daemon.running = False

    def test_wait_for_sidecar_check_ready_success_and_failures(self):
        """7. Test della utility wait_for_sidecar con socket reale e casi di errore."""
        # Fallimento se socket non esiste
        self.assertFalse(check_ready(Path("/tmp/nonexistent.sock"), self.token_file))

        # Fallimento se token file non esiste
        self.assertFalse(check_ready(self.sock_path, Path("/tmp/nonexistent_token")))

        # Avvia daemon
        server_thread = threading.Thread(target=self.daemon.run, daemon=True)
        server_thread.start()

        import time
        for _ in range(50):
            if self.sock_path.exists():
                break
            time.sleep(0.05)

        # Successo con socket attivo e token valido
        self.assertTrue(check_ready(self.sock_path, self.token_file))

        # Fallimento con token errato
        bad_token_file = self.tmp_path / "bad_token"
        bad_token_file.write_text("invalid_token_12345678901234567890", encoding="utf-8")
        self.assertFalse(check_ready(self.sock_path, bad_token_file))

        self.daemon.running = False

    def test_child_environment_sanitization_strips_sidecar_secrets(self):
        """8. execute_codex rimuove tutti i segreti e variabili interne sidecar dall'ambiente del processo figlio."""
        req = {
            "profile": "sol",
            "worktree": str(self.tmp_path),
            "sandbox": "read-only",
            "reasoning_effort": "low",
            "prompt": "Test prompt"
        }

        with patch.dict(os.environ, {
            "UFFICIO_SIDECAR_TOKEN": "super_secret_sidecar_token_123456",
            "UFFICIO_SIDECAR_TOKEN_FILE": "/run/ufficio-codex/token",
            "DASHBOARD_PASSWORD": "dashboard_secret_password_123456",
            "UFFICO_AUTH_TOKEN": "auth_secret_token_1234567890"
        }):
            with patch("subprocess.Popen") as mock_popen:
                mock_proc = MagicMock()
                mock_proc.stdout.read.return_value = b""
                mock_proc.stderr.read.return_value = b""
                mock_proc.wait.return_value = 0
                mock_proc.returncode = 0
                mock_popen.return_value = mock_proc

                self.daemon.execute_codex(req)

                mock_popen.assert_called_once()
                call_kwargs = mock_popen.call_args[1]
                child_env = call_kwargs["env"]

                # Verifica rimozione tassativa dei segreti
                self.assertNotIn("UFFICIO_SIDECAR_TOKEN", child_env)
                self.assertNotIn("UFFICIO_SIDECAR_TOKEN_FILE", child_env)
                self.assertNotIn("DASHBOARD_PASSWORD", child_env)
                self.assertNotIn("UFFICO_AUTH_TOKEN", child_env)

                # Verifica impostazione CODEX_HOME corretta
                self.assertIn("CODEX_HOME", child_env)
                self.assertIn("sol", child_env["CODEX_HOME"])

    def test_load_sidecar_token_fails_closed_without_master_source(self):
        """9. load_sidecar_token rifiuta fallback /etc e fallisce se la sorgente master non esiste."""
        non_existent_file = self.tmp_path / "non_existent_token"
        with patch.dict(os.environ, {
            "UFFICIO_SIDECAR_TOKEN": "",
            "UFFICIO_SIDECAR_TOKEN_FILE": str(non_existent_file)
        }):
            with patch("pathlib.Path.exists", return_value=False):
                with self.assertRaises(ValueError) as cm:
                    load_sidecar_token()
                self.assertIn("non configurato", str(cm.exception))


class TestDockerComposeSidecarMountAlignment(unittest.TestCase):
    def test_docker_compose_mounts_user_runtime_directories(self):
        """10. Verifica che docker-compose.yml monti le directory runtime utente (/run/user/1000/taktstock-*) e non root."""
        compose_path = Path(__file__).resolve().parent.parent / "docker-compose.yml"
        self.assertTrue(compose_path.exists(), "docker-compose.yml non trovato")
        content = compose_path.read_text(encoding="utf-8")

        # Deve contenere i mount verso le directory runtime utente (/run/user/1000/...)
        self.assertIn("/run/user/1000/taktstock-codex:/run/taktstock-codex:ro", content)
        self.assertIn("/run/user/1000/taktstock-agy:/run/taktstock-agy:ro", content)

        # Non deve contenere i vecchi mount errati verso /run/taktstock-* o /run/ufficio-*
        self.assertNotIn("- /run/taktstock-codex:", content)
        self.assertNotIn("- /run/taktstock-agy:", content)
        self.assertNotIn("- /run/ufficio-codex:", content)
        self.assertNotIn("- /run/ufficio-agy:", content)


if __name__ == "__main__":
    unittest.main()
